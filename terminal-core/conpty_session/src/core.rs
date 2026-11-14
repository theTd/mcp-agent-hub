//! The core: one session table, one ticker thread, the calls Python makes (§5.2) and the drain record the
//! ticker wakes the hub for (§5.3).
//!
//! Step 2 owns the parsed state (in `Session`), the coalescing flush per 5 ms tick (§5.4) and the render
//! calls. What step 1 established still holds: the core takes no lock while calling `notify` or `log`, and
//! every blocking call has a deadline.
//!
//! No core thread ever blocks waiting for the GIL while another thread can hold the GIL and wait on that
//! thread (review B2): `notify`/`log` are called through `Python::try_attach`, which skips the call
//! instead of waiting when the interpreter is finalizing, and `shutdown` only joins the ticker from a
//! caller that has released the GIL (the binding's `Drop` skips the join).

use std::collections::{HashMap, HashSet};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, RwLock, RwLockReadGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use terminal_stream::FrameSink;

use crate::conpty::{self, SpawnSpec};
use crate::frame_queue::{FrameQueue, Frames};
use crate::session::{
    DrainRecord, RawReply, ResizeError, ScreenReply, Session, SessionFlags,
    SessionOptions, SessionSnapshot, TextReply, TickFlags,
};
use crate::sink_fanout::SinkFanout;
use crate::ws::WsServer;

/// The answer `attach_socket` gives the listener: a registered console socket, an id the core never
/// had, one that ended and was retired, or one whose table read missed its deadline.
pub enum SocketAttach {
    Attached {
        session: Arc<Session>,
        /// The replay to write before the first frame (Q11: it ends exactly where the frames begin).
        replay: String,
        /// The sink's handle inside the id's fan-out.
        handle: u64,
    },
    /// No session under this id: `ws_pty_endpoint` closed with 1008.
    Unknown,
    /// The id was retired while the registration was in flight: the endpoint closed with 1000.
    Ended,
    /// The table was busy past its deadline: the endpoint closed with 1013.
    Busy,
}

/// Calls this step's Python layer makes. `drain_interval` is the 50 ms notify window; `tick_interval`
/// is how often the ticker looks (§5.3) and flushes due batches (§5.4).
#[derive(Clone, Copy, Debug)]
pub struct CoreOptions {
    /// Default session flags: a `spawn`'s `flags` dict overrides what it names.
    pub flags: SessionFlags,
    pub write_queue_bytes: usize,
    /// One socket queue's cap (§5.4's 2 MB of unsent frames); `attach_queue` may override it.
    pub sink_queue_bytes: usize,
    pub drain_interval: Duration,
    pub tick_interval: Duration,
}

impl Default for CoreOptions {
    fn default() -> Self {
        Self {
            flags: SessionFlags::default(),
            write_queue_bytes: 1024 * 1024,
            sink_queue_bytes: terminal_stream::SINK_QUEUE_BYTES,
            drain_interval: Duration::from_millis(50),
            tick_interval: Duration::from_millis(5),
        }
    }
}

/// What `spawn` needs. `argv[0]` is resolved inside; `env` is the whole session environment.
pub struct SpawnRequest {
    pub session_id: String,
    pub argv: Vec<String>,
    pub cwd: Option<String>,
    pub env: HashMap<String, String>,
    pub cols: u16,
    pub rows: u16,
    pub flags: SessionFlags,
    /// Archived scrollback of a resumed session (A1): put in the raw ring before the first byte.
    pub seed_history: Option<String>,
    /// The resume separator (A1): one frame to the sinks that exist before the reader starts.
    pub ws_prefix: Option<String>,
}

/// What `attach_text` needs: a session with a screen and a ring but no process (§5.2).
pub struct TextRequest {
    pub session_id: String,
    pub cols: u16,
    pub rows: u16,
    pub flags: SessionFlags,
    /// A virtual resume's archived scrollback: the ring's first entry, exactly as `spawn.seed_history`
    /// (A1, `_make_resumed_session` seeds it for every engine, dsh included).
    pub seed_history: Option<String>,
    /// The DSH virtual resume's separator frame (`attach_text(seed=…)`), the `spawn.ws_prefix` twin.
    pub seed: Option<String>,
}

#[derive(Clone, Debug)]
pub struct SpawnReply {
    pub pid: u32,
    pub cols: u16,
    pub rows: u16,
    /// The spawn's log lines, queued rather than delivered: `spawn` runs with the GIL released, so the
    /// binding logs them after it re-attaches (§5.2).
    pub log_lines: Vec<String>,
}

#[derive(Clone, Debug, Default)]
pub struct Stats {
    pub sessions: usize,
    pub alive: usize,
    pub crashed: usize,
    pub bytes_total: u64,
    pub chunks_total: u64,
    pub dropped_bytes: u64,
    pub notify_calls: u64,
    /// Every caught panic: reader, writer, reaper, ticker and close helper (should-fix 5).
    pub panics: u64,
}

/// The plain-Rust error surface; the binding maps it onto `KeyError`/`RuntimeError`/`ValueError`.
#[derive(Debug, Clone)]
pub enum CoreError {
    UnknownSession(String),
    /// A queue handle this core never issued, or one whose entry `detach_queue` removed.
    UnknownQueue(u64),
    Spawn(String),
    Invalid(String),
    Deadline(String),
    Internal(String),
}

impl std::fmt::Display for CoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CoreError::UnknownSession(id) => write!(f, "unknown session: {id}"),
            CoreError::UnknownQueue(handle) => write!(f, "unknown queue handle: {handle}"),
            CoreError::Spawn(message)
            | CoreError::Invalid(message)
            | CoreError::Deadline(message)
            | CoreError::Internal(message) => write!(f, "{message}"),
        }
    }
}

/// §5.2's deadline for the temporary bridge and `retire`: the queue registry and the table write are
/// short, so 50 ms is the same bound the other table reads use.
const BRIDGE_DEADLINE: Duration = Duration::from_millis(50);

/// §6.4's bound for closing every console socket and joining the listener and socket threads: local
/// sockets with their close frames, plus one cut-off for a peer that accepts nothing.
const SHUTDOWN_SOCKET_BUDGET: Duration = Duration::from_secs(2);

/// Bounded wait for a read lock, so a writer (a spawn inserting a session) cannot hold the hub's loop.
fn read_deadline<'a, T>(lock: &'a RwLock<T>, deadline: Duration) -> Option<RwLockReadGuard<'a, T>> {
    let end = Instant::now() + deadline;
    loop {
        match lock.try_read() {
            Ok(guard) => return Some(guard),
            Err(std::sync::TryLockError::Poisoned(poisoned)) => return Some(poisoned.into_inner()),
            Err(std::sync::TryLockError::WouldBlock) => {
                if Instant::now() >= end {
                    return None;
                }
                thread::sleep(Duration::from_micros(200));
            }
        }
    }
}

/// `read_deadline`'s writer twin: `retire` inserts no session, but a spawn's insert must not make it
/// wait past its deadline.
fn write_deadline<'a, T>(
    lock: &'a RwLock<T>,
    deadline: Duration,
) -> Option<std::sync::RwLockWriteGuard<'a, T>> {
    let end = Instant::now() + deadline;
    loop {
        match lock.try_write() {
            Ok(guard) => return Some(guard),
            Err(std::sync::TryLockError::Poisoned(poisoned)) => return Some(poisoned.into_inner()),
            Err(std::sync::TryLockError::WouldBlock) => {
                if Instant::now() >= end {
                    return None;
                }
                thread::sleep(Duration::from_micros(200));
            }
        }
    }
}

/// `lock_deadline`'s plain twin for the queue registry. Its critical sections are a `HashMap` insert or
/// remove and a walk that only closes queues; none of them waits on a sink, a process or a delivery, so a
/// deadline there could only turn "busy for a moment" into a wrong answer — `false` for a detach that did
/// not happen, a `retire` that reports success while leaving queues open (review F4).
fn lock_queues(queues: &Mutex<HashMap<u64, AttachedQueue>>) -> MutexGuard<'_, HashMap<u64, AttachedQueue>> {
    queues.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// One id's lifecycle claim: held by `spawn`, `attach_text` and `retire` from the predecessor lookup to
/// the table insert (or removal), so two lifecycle calls for the same id cannot interleave a handover
/// with a retirement (review F1: a resume that took over a fan-out `retire` was closing left the live
/// successor silent for good).
///
/// It is a set membership, not a lock on the session: taking one never waits on a process, a console or
/// a sink, and the loser fails fast with `ValueError` instead of blocking the hub's loop. The table lock
/// is not held across `CreateProcessW`; this claim is what makes the predecessor stable in that window.
struct Claim<'a> {
    claims: &'a Mutex<HashSet<String>>,
    id: String,
}

impl Drop for Claim<'_> {
    fn drop(&mut self) {
        self.claims
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .remove(&self.id);
    }
}

/// One Python socket's registration (§5.2's temporary bridge): the queue it drains, and where the sink
/// sits so a detach needs neither the session table nor the id's current incarnation.
struct AttachedQueue {
    session_id: String,
    fanout: Arc<SinkFanout>,
    /// `None` while the registration is still in flight (the entry exists first, so a failed or racing
    /// retire can never leave a registered sink nobody can detach).
    sink_handle: Option<u64>,
    queue: Arc<FrameQueue>,
}

pub struct Core {
    sessions: RwLock<HashMap<String, Arc<Session>>>,
    /// Step 3b's temporary bridge (§5.2): the Python socket's queue per handle. Handles are unique per
    /// core, not per id, because `take_frames`/`detach_queue` name the queue alone.
    queues: Mutex<HashMap<u64, AttachedQueue>>,
    next_queue_handle: AtomicU64,
    /// The ids a lifecycle call (`spawn`, `attach_text`, `retire`) is inside right now; see [`Claim`].
    lifecycle: Mutex<HashSet<String>>,
    /// Returns whether the wakeup actually ran: the binding's `try_attach` skips it at interpreter
    /// teardown, and a skipped call must not be counted as a notify (nit 7).
    notify: Box<dyn Fn() -> bool + Send + Sync>,
    log: Arc<dyn Fn(&str) + Send + Sync>,
    opts: CoreOptions,
    tick: Arc<TickFlags>,
    /// Ticker wake: set by `shutdown`, waited on with the tick interval.
    stop: Mutex<bool>,
    wake: Condvar,
    stopping: AtomicBool,
    notify_calls: AtomicU64,
    panics: Arc<AtomicU64>,
    ticker: Mutex<Option<JoinHandle<()>>>,
    /// The ticker's id: `shutdown` must not join the ticker from the ticker.
    ticker_id: Mutex<Option<std::thread::ThreadId>>,
    /// The console's sockets (§6.4): the loopback listener, the ticket table and one thread per
    /// attached pane. Python no longer owns a terminal socket in step 4.
    ws: Arc<WsServer>,
}

impl Core {
    /// Starts the ticker and the terminal WebSocket listener. `notify` is called at most every
    /// `drain_interval` and only when a session is dirty (§5.3); `log` is called on spawn, kill, panic
    /// and a refused or closed console socket only.
    ///
    /// `Err` when the loopback listener cannot bind or the ticket key cannot be minted: the console has
    /// no other path to a session (§6.4), so a core without its socket server must not start.
    pub fn new(
        notify: Box<dyn Fn() -> bool + Send + Sync>,
        log: Box<dyn Fn(&str) + Send + Sync>,
        opts: CoreOptions,
    ) -> Result<Arc<Self>, CoreError> {
        let log: Arc<dyn Fn(&str) + Send + Sync> = Arc::from(log);
        // §6.4: the listener is part of the core, so its port and its health are known before the
        // first session is spawned.
        let ws = WsServer::start(Arc::clone(&log), opts.sink_queue_bytes).map_err(|error| {
            CoreError::Internal(format!("terminal socket listener could not start: {error}"))
        })?;
        let tick = Arc::new(TickFlags::default());
        let panics = Arc::new(AtomicU64::new(0));
        let core = Arc::new(Self {
            sessions: RwLock::new(HashMap::new()),
            queues: Mutex::new(HashMap::new()),
            // Handle 0 is never handed out, so a missing handle and a real one are distinguishable.
            next_queue_handle: AtomicU64::new(1),
            lifecycle: Mutex::new(HashSet::new()),
            notify,
            log: Arc::clone(&log),
            opts,
            tick,
            stop: Mutex::new(false),
            wake: Condvar::new(),
            stopping: AtomicBool::new(false),
            notify_calls: AtomicU64::new(0),
            panics,
            ticker: Mutex::new(None),
            ticker_id: Mutex::new(None),
            ws: Arc::clone(&ws),
        });
        ws.spawn_accept(Arc::downgrade(&core))
            .map_err(|error| CoreError::Internal(format!("ws accept thread: {error}")))?;
        let ticker_core = Arc::clone(&core);
        let handle = thread::Builder::new()
            .name("core-ticker".to_string())
            .spawn(move || ticker_core.ticker_body())
            .expect("spawn ticker thread");
        if let Ok(mut slot) = core.ticker.lock() {
            if let Ok(mut ticker_id) = core.ticker_id.lock() {
                *ticker_id = Some(handle.thread().id());
            }
            *slot = Some(handle);
        }
        Ok(core)
    }

    /// 5 ms ticks flush step-2 batches; the Python wakeup is gated to `drain_interval`, and one exit per
    /// session is delivered on the next tick even mid-window.
    fn ticker_body(self: Arc<Self>) {
        let mut last_notify: Option<Instant> = None;
        loop {
            {
                let guard = self.stop.lock().unwrap_or_else(|e| e.into_inner());
                let _ = self
                    .wake
                    .wait_timeout(guard, self.opts.tick_interval)
                    .unwrap_or_else(|e| e.into_inner());
            }
            if self.stopping.load(Ordering::Acquire) {
                return;
            }
            // The whole iteration is guarded, its error path included: a panic in `notify` or in the
            // `log` line that reports it must not take the ticker down (should-fix 5).
            let work = catch_unwind(AssertUnwindSafe(|| {
                // §5.4: the ticker is the one place a session's batch is flushed on its time window (the
                // reader only flushes a batch that already reached 32 KiB). A spawn's write lock makes a
                // tick skip its flush; the next tick is 5 ms away.
                //
                // The table lock is released before any flush: `flush_frames` waits for the id's delivery
                // lock, and a slow `deliver` would otherwise hold the table against a spawn insert or a
                // `retire` for as long as it is slow.
                let now = Instant::now();
                let sessions: Vec<Arc<Session>> = match self.sessions.try_read() {
                    Ok(map) => map.values().cloned().collect(),
                    Err(_) => Vec::new(),
                };
                for session in sessions {
                    let _ = catch_unwind(AssertUnwindSafe(|| session.flush_frames(now)));
                }
                // §6.4's slow client: a console socket that has not written for the stall limit is
                // asked to close with 1000 here, on the ticker, because a blocked write cannot time
                // itself out.
                let _ = catch_unwind(AssertUnwindSafe(|| self.ws.check_stalls()));
                let urgent = self.tick.urgent.swap(false, Ordering::AcqRel);
                let pending = self.tick.pending.swap(false, Ordering::AcqRel);
                let due = last_notify
                    .map(|at| at.elapsed() >= self.opts.drain_interval)
                    .unwrap_or(true);
                if urgent || (pending && due) {
                    last_notify = Some(Instant::now());
                    // No core lock is held here: Python may hold one and call back in (§6.5). The
                    // callback itself skips its call when it cannot attach (no waiting for the GIL), and
                    // only a call that ran is counted (nit 7).
                    if (self.notify)() {
                        self.notify_calls.fetch_add(1, Ordering::Relaxed);
                    }
                } else if pending {
                    self.tick.pending.store(true, Ordering::Release);
                }
            }));
            if work.is_err() {
                self.panics.fetch_add(1, Ordering::Relaxed);
                let _ = catch_unwind(AssertUnwindSafe(|| {
                    (self.log)("terminal core: notify callback panicked; ticker continues");
                }));
            }
        }
    }

    /// The session flags a `spawn`/`attach_text` keeps when its dict does not name them (§5.2).
    pub fn default_flags(&self) -> SessionFlags {
        self.opts.flags
    }

    pub fn spawn(&self, request: SpawnRequest) -> Result<SpawnReply, CoreError> {
        let SpawnRequest {
            session_id,
            argv,
            cwd,
            env,
            cols,
            rows,
            flags,
            seed_history,
            ws_prefix,
        } = request;
        if argv.is_empty() {
            return Err(CoreError::Invalid("spawn needs at least one argv element".into()));
        }
        if cols == 0 || rows == 0 {
            return Err(CoreError::Invalid("spawn needs cols and rows >= 1".into()));
        }
        // The whole resume is one lifecycle claim: the predecessor lookup, the handover and the table
        // insert happen with no `retire` of this id able to run, so no successor can take over a fan-out
        // that a `retire` is closing (review F1).
        let _claim = self.claim(&session_id)?;
        // The dead incarnation this spawn resumes for the same id, if any (§6.4 A1): its sink fan-out
        // and its pending batch are handed to the new session, so the sockets attached to the id keep
        // working and the resume banner reaches them.
        let previous = self
            .sessions
            .read()
            .ok()
            .and_then(|map| map.get(&session_id).cloned());
        if let Some(existing) = previous.as_ref() {
            // A session whose process has exited is replaceable even when its reader is still winding
            // down: `alive` is the only "still running" signal, and the reaper publishes it as soon as
            // the process is gone (B1).
            if existing.alive() {
                return Err(CoreError::Invalid(format!(
                    "session {session_id} is already running in the core"
                )));
            }
        }

        let spec = SpawnSpec {
            argv: &argv,
            cwd: cwd.as_deref(),
            env: &env,
            cols,
            rows,
        };
        let process = conpty::spawn(&spec).map_err(CoreError::Spawn)?;
        let (session, log_lines) = Session::start(
            session_id.clone(),
            process,
            cols,
            rows,
            SessionOptions {
                write_queue_bytes: self.opts.write_queue_bytes,
                flags,
            },
            Arc::clone(&self.tick),
            Arc::clone(&self.panics),
            Arc::clone(&self.log),
            seed_history,
            ws_prefix,
            previous,
        )
        // A thread that cannot start leaves a live process with no table entry, so `Session::start`
        // ends the tree and reports it here instead of returning a half-built session.
        .map_err(CoreError::Spawn)?;
        let reply = SpawnReply {
            pid: session.pid,
            cols,
            rows,
            log_lines,
        };
        // A dead session under the same id is replaced: resume spawns a new process for an id whose
        // previous session has exited, and the hub's screens come from `seed_history` (§8 A1). The new
        // session took the old one's sink fan-out over (`Session::start`), so the sockets attached to the
        // id were never detached; the old `Arc<Session>` is released here: its reader has hit EOF, so
        // `Session::drop` closes the pipes and `Resources::drop` the console, leaving no HPCON, conhost or
        // reader behind.
        let replaced = self
            .sessions
            .write()
            .map_err(|_| CoreError::Internal("session table lock poisoned".into()))?
            .insert(session_id, session);
        drop(replaced);
        Ok(reply)
    }

    /// Takes the id's lifecycle claim, or fails fast: two lifecycle calls for one id would let a
    /// handover and a retirement interleave (review F1), and waiting would hold a hub thread on another
    /// hub thread's spawn. `ValueError` is the contract's "refused, retry" answer, never a `TimeoutError`
    /// the hub's tick loop would treat as a deadline to back off on.
    fn claim(&self, id: &str) -> Result<Claim<'_>, CoreError> {
        let mut claims = self
            .lifecycle
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if !claims.insert(id.to_string()) {
            return Err(CoreError::Invalid(format!(
                "session {id}: another spawn, attach_text or retire is in flight; retry"
            )));
        }
        Ok(Claim {
            claims: &self.lifecycle,
            id: id.to_string(),
        })
    }

    fn session(&self, id: &str) -> Result<Arc<Session>, CoreError> {
        let map = read_deadline(&self.sessions, Duration::from_millis(50))
            .ok_or_else(|| CoreError::Deadline(format!("session table busy, {id} not read")))?;
        map.get(id)
            .cloned()
            .ok_or_else(|| CoreError::UnknownSession(id.to_string()))
    }

    pub fn write(&self, id: &str, text: &[u8], deadline: Duration) -> Result<usize, CoreError> {
        let session = self.session(id)?;
        session
            .write(text, deadline)
            // The deadline error carries nothing queued, which is what the binding turns into
            // `TimeoutError`; a closed input pipe is an internal failure (§5.2, SF4).
            .map_err(|error| match error {
                crate::session::WriteError::Timeout => CoreError::Deadline(format!(
                    "session {id}: write queue full, nothing queued"
                )),
                crate::session::WriteError::Closed => {
                    CoreError::Internal(format!("session {id}: input is closed"))
                }
            })
    }

    pub fn resize(
        &self,
        id: &str,
        cols: u16,
        rows: u16,
        deadline: Duration,
    ) -> Result<(u16, u16), CoreError> {
        if cols < 20 || rows < 5 {
            return Err(CoreError::Invalid(format!(
                "resize refused: {cols}x{rows} is below the 20x5 floor"
            )));
        }
        let session = self.session(id)?;
        session
            .resize(cols, rows, deadline)
            // §5.1's families: a closed console is a refused call (`ValueError`), a missed deadline a
            // `TimeoutError`, a Win32 failure an internal error (`RuntimeError`).
            .map_err(|error| match error {
                ResizeError::Closed(message) => CoreError::Invalid(message),
                ResizeError::Deadline(message) => CoreError::Deadline(message),
                ResizeError::Failed(message) => CoreError::Internal(message),
            })
    }

    pub fn kill(&self, id: &str, budget: Duration) -> Result<Option<i32>, CoreError> {
        let session = self.session(id)?;
        session.kill(budget).map_err(CoreError::Internal)
    }

    /// `TerminateJobObject` for every session, in parallel, inside one `budget` (§5.2: 2 s for the whole
    /// call, not per session). Used by the selftest and by shutdown in step 5.
    pub fn kill_all(&self, budget: Duration) -> Result<(), CoreError> {
        // The snapshot is the one unbounded read this call used to have (rereview item 4): a spawn insert
        // holds the write lock, and a wedged shutdown must not wait on it. A miss reports instead of
        // pretending every session was killed; the jobs' kill-on-close still ends the trees (§6.6).
        let sessions: Vec<Arc<Session>> = match read_deadline(&self.sessions, budget) {
            Some(map) => map.values().cloned().collect(),
            None => {
                return Err(CoreError::Deadline(
                    "session table busy; kill_all could not snapshot its sessions".into(),
                ))
            }
        };
        let end = Instant::now() + budget;
        let failures: Vec<String> = thread::scope(|scope| {
            let mut handles = Vec::with_capacity(sessions.len());
            for session in &sessions {
                let remaining = end.saturating_duration_since(Instant::now());
                let session = Arc::clone(session);
                handles.push((
                    session.id.clone(),
                    scope.spawn(move || session.kill(remaining)),
                ));
            }
            handles
                .into_iter()
                .filter_map(|(id, handle)| match handle.join() {
                    Ok(Err(error)) => Some(format!("{id}: {error}")),
                    Ok(Ok(_)) => None,
                    Err(_) => Some(format!("{id}: kill panicked")),
                })
                .collect()
        });
        for failure in failures {
            (self.log)(&format!("terminal core: kill_all failed for {failure}"));
        }
        Ok(())
    }

    /// One record per session whose flags were set since the previous call, flags cleared (§5.3).
    pub fn drain(&self) -> Vec<DrainRecord> {
        let sessions = match read_deadline(&self.sessions, Duration::from_millis(50)) {
            Some(map) => map,
            None => return Vec::new(),
        };
        // §5.4/§9 step 4: a console socket's drop happens on the delivery path, where no output may
        // follow it, so a drop the hub has not seen dirties its session here. The record then carries
        // the count per handle, never in the session's own `dropped_bytes`.
        for id in self.ws.sessions_with_unreported_drops() {
            if let Some(session) = sessions.get(&id) {
                session.note_socket_drop();
            }
        }
        let mut records: Vec<DrainRecord> = sessions
            .values()
            .filter_map(|session| session.drain_record())
            .collect();
        drop(sessions);
        if !records.is_empty() {
            // One walk for the whole drain, not one per record.
            let drops = self.ws.take_socket_drops();
            for record in &mut records {
                if let Some(sockets) = drops.get(&record.session_id) {
                    record.socket_drops = sockets.clone();
                }
            }
        }
        records
    }

    pub fn raw_tail(
        &self,
        id: &str,
        max_chars: usize,
        deadline: Duration,
    ) -> Result<String, CoreError> {
        let session = self.session(id)?;
        Ok(session.raw_tail(max_chars, deadline))
    }

    /// §5.2's `screen`: text, cursor, size and revision; the cached reply on a missed deadline.
    pub fn screen(
        &self,
        id: &str,
        max_lines: usize,
        deadline: Duration,
    ) -> Result<ScreenReply, CoreError> {
        Ok(self.session(id)?.screen(max_lines, deadline))
    }

    /// §5.2's `scrollback`: the history rows above the screen.
    pub fn scrollback(
        &self,
        id: &str,
        max_lines: usize,
        deadline: Duration,
    ) -> Result<TextReply, CoreError> {
        Ok(self.session(id)?.scrollback(max_lines, deadline))
    }

    /// §5.2's `raw`: the trimmed history, its byte count and its revision.
    pub fn raw(&self, id: &str, deadline: Duration) -> Result<RawReply, CoreError> {
        Ok(self.session(id)?.raw(deadline))
    }

    /// The replay a console socket gets on connect (`replay-shape`); the socket itself is step 4.
    pub fn replay(&self, id: &str, deadline: Duration) -> Result<String, CoreError> {
        self.session(id)?
            .replay(deadline)
            .ok_or_else(|| CoreError::Deadline(format!("session {id}: replay missed its deadline")))
    }

    /// §5.2's `attach_text`: a session with a screen but no process (DSH virtual sessions).
    pub fn attach_text(&self, request: TextRequest) -> Result<SpawnReply, CoreError> {
        let TextRequest {
            session_id,
            cols,
            rows,
            flags,
            seed_history,
            seed,
        } = request;
        if cols == 0 || rows == 0 {
            return Err(CoreError::Invalid(
                "attach_text needs cols and rows >= 1".into(),
            ));
        }
        // One lifecycle claim for the whole resume, like `spawn` (§6.4 A1, review F1).
        let _claim = self.claim(&session_id)?;
        let previous = self
            .sessions
            .read()
            .ok()
            .and_then(|map| map.get(&session_id).cloned());
        if let Some(existing) = previous.as_ref() {
            if existing.alive() {
                return Err(CoreError::Invalid(format!(
                    "session {session_id} is already running in the core"
                )));
            }
        }
        // The seed (the DSH resume separator) and the handover happen inside `start_text`, under the
        // id's delivery lock and before the session is reachable: a sink attached to the id gets the old
        // session's pending bytes, then the seed, exactly once (§6.4 A1).
        let session = Session::start_text(
            session_id.clone(),
            cols,
            rows,
            SessionOptions {
                write_queue_bytes: self.opts.write_queue_bytes,
                flags,
            },
            Arc::clone(&self.tick),
            Arc::clone(&self.panics),
            Arc::clone(&self.log),
            seed_history.as_deref(),
            seed.as_deref(),
            previous,
        );
        let reply = SpawnReply {
            pid: 0,
            cols,
            rows,
            log_lines: vec![format!(
                "session {session_id}: attached as a text session ({cols}x{rows})"
            )],
        };
        let replaced = self
            .sessions
            .write()
            .map_err(|_| CoreError::Internal("session table lock poisoned".into()))?
            .insert(session_id, session);
        drop(replaced);
        Ok(reply)
    }

    /// §5.2's `feed`: text into a session with no process; returns the new screen revision.
    pub fn feed(&self, id: &str, text: &str, deadline: Duration) -> Result<u64, CoreError> {
        let session = self.session(id)?;
        session.feed(text, deadline).ok_or_else(|| {
            CoreError::Deadline(format!("session {id}: feed missed its deadline, nothing fed"))
        })
    }

    /// §5.2's `watch_screen`: a bounded render per changed revision, carried by `drain`.
    pub fn watch(
        &self,
        id: &str,
        max_lines: usize,
        interval: Duration,
    ) -> Result<(), CoreError> {
        self.session(id)?.watch(max_lines, interval);
        Ok(())
    }

    pub fn unwatch(&self, id: &str) -> Result<(), CoreError> {
        self.session(id)?.unwatch();
        Ok(())
    }

    /// Registers a frame sink for a session (step 4's socket, or a test sink) and returns the replay that
    /// belongs with the registration, so no chunk is both replayed and framed (Q11).
    pub fn attach_sink(
        &self,
        id: &str,
        sink: Arc<dyn FrameSink>,
    ) -> Result<(String, u64), CoreError> {
        match self.session(id)?.attach_sink(sink) {
            (replay, Some(handle)) => Ok((replay, handle)),
            // Retired while the registration was in flight: nothing was registered, so there is no
            // handle to hand back (review F1).
            (_, None) => Err(CoreError::Invalid(format!(
                "session {id}: retired; the sink was not registered"
            ))),
        }
    }

    pub fn detach_sink(&self, id: &str, handle: u64) -> Result<bool, CoreError> {
        Ok(self.session(id)?.detach_sink(handle))
    }

    /// Registers one console socket's sink on the id's fan-out and hands back the replay that goes with
    /// the registration (§6.4, Q11). The three refusals are the listener's close codes.
    pub(crate) fn attach_socket(&self, id: &str, sink: Arc<dyn FrameSink>) -> SocketAttach {
        let session = match self.session(id) {
            Ok(session) => session,
            Err(CoreError::UnknownSession(_)) => return SocketAttach::Unknown,
            Err(_) => return SocketAttach::Busy,
        };
        match session.attach_sink(sink) {
            (replay, Some(handle)) => SocketAttach::Attached {
                session,
                replay,
                handle,
            },
            // Retired while the registration was in flight: nothing was registered, so there is no
            // replay to send and the socket closes the way an ended pane always did.
            (_, None) => SocketAttach::Ended,
        }
    }

    /// Console input from a socket (§6.4 "Input"). A session with a PTY gets the bytes written; a
    /// text-only session has nothing to write to, so the input becomes an ordered drain-record request
    /// that the hub feeds to its own echo and line callback (§8: for a text session the core never owns
    /// the echo). Either way a failure is logged by the caller and costs only that frame.
    pub fn socket_input(
        &self,
        id: &str,
        text: &str,
        deadline: Duration,
    ) -> Result<(), CoreError> {
        let session = self.session(id)?;
        if session.is_text_only() {
            session.request_input(text.to_string());
            return Ok(());
        }
        session
            .write(text.as_bytes(), deadline)
            .map(|_| ())
            .map_err(|error| CoreError::Invalid(format!("session {id}: write failed: {error:?}")))
    }

    /// The console's size request (§6.4): recorded on the session, carried by the drain record, and
    /// applied by the hub with its own clamp and propagation — never by the core.
    pub fn request_resize(
        &self,
        id: &str,
        cols: u16,
        rows: u16,
        propagate: bool,
    ) -> Result<(), CoreError> {
        self.session(id)?.request_resize(cols, rows, propagate);
        Ok(())
    }

    /// The port the console connects to (§6.4); fixed once, at start.
    pub fn ws_port(&self) -> u16 {
        self.ws.port()
    }

    /// Whether the accept thread is still serving. False means every new console pane is refused, which
    /// the hub's health check reports (§6.4, `ws-listening-health`).
    pub fn ws_listening(&self) -> bool {
        self.ws.listening()
    }

    /// A console ticket for `id`: the URL to open and the unix second it expires in. An unknown id is
    /// `UnknownSession` (the hub 404s before it asks), because a ticket must never name a session that
    /// does not exist.
    pub fn ws_ticket(&self, id: &str, ttl: Duration) -> Result<(String, u64), CoreError> {
        self.session(id)?;
        let minted = self.ws.ticket(id, ttl)?;
        Ok((self.ws.ticket_url(&minted, id), minted.expires_at_unix))
    }

    /// Asks every socket of `id` to close with 1000 (§5.2, W7) and returns how many it asked. A retired
    /// id answers 0: its sockets are already gone.
    pub fn close_sockets(&self, id: &str) -> usize {
        self.ws.close_sockets(id)
    }

    /// How many console sockets the core holds (diagnostics and tests).
    pub fn socket_count(&self) -> usize {
        self.ws.socket_count()
    }

    /// Test-only: lowers the stall limit so a 30 s slow-client case runs in a second.
    #[cfg(any(test, feature = "test-hooks"))]
    #[doc(hidden)]
    pub fn set_socket_stall_limit(&self, limit: Duration) {
        self.ws.set_stall_limit(limit);
    }

    /// Test-only: how many accepts failed, real or injected.
    #[cfg(any(test, feature = "test-hooks"))]
    #[doc(hidden)]
    pub fn ws_accept_failures(&self) -> u64 {
        self.ws.accept_failures()
    }

    /// Test-only: makes the next `count` accepted connections fail like a client that dropped
    /// mid-accept (§6.4's `4b06139` case).
    #[cfg(feature = "test-hooks")]
    #[doc(hidden)]
    pub fn fail_next_accepts(&self, count: u64) {
        self.ws.fail_next_accepts(count);
    }

    /// **Temporary (step 4 deletes it).** Registers a bounded [`FrameQueue`] on the id's fan-out — the
    /// `FrameSink` step 4's socket thread drains itself — and returns the replay that belongs with the
    /// registration plus the queue's handle. The replay snapshot and the registration are one step
    /// inside `attach_sink` (Q11): every byte is in the replay or in the queue's frames, never both.
    ///
    /// `max_bytes` overrides §5.4's 2 MB (`CoreOptions::sink_queue_bytes`). Handles are unique per core
    /// and survive a resume of the id: the queue is registered on the **id's** fan-out (`take_over`
    /// carries it), so a handle taken before a resume keeps receiving after the predecessor's last bytes
    /// and the separator.
    pub fn attach_queue(
        &self,
        id: &str,
        max_bytes: Option<usize>,
    ) -> Result<(String, u64), CoreError> {
        let session = self.session(id)?;
        let cap = max_bytes.unwrap_or(self.opts.sink_queue_bytes);
        if cap == 0 {
            return Err(CoreError::Invalid(
                "attach_queue needs a byte cap >= 1".into(),
            ));
        }
        let queue = Arc::new(FrameQueue::new(cap));
        let fanout = session.fanout();
        let handle = self.next_queue_handle.fetch_add(1, Ordering::Relaxed);
        // The entry goes in before the registration, so a retire that lands in between closes this queue
        // and unregisters the sink instead of leaving a sink no handle can reach (review F4).
        lock_queues(&self.queues).insert(
            handle,
            AttachedQueue {
                session_id: id.to_string(),
                fanout: Arc::clone(&fanout),
                sink_handle: None,
                queue: Arc::clone(&queue),
            },
        );
        let (replay, sink_handle) = session.attach_sink(Arc::clone(&queue) as Arc<dyn FrameSink>);
        // The registration is complete here, so this call always answers with a usable handle: either
        // the queue is live and a detach unregisters the sink, or the id was retired while the
        // registration was in flight — then the sink is unregistered and the queue closed, and the
        // caller learns it from `take_frames`'s `closed` on the first take (review F1, F4).
        let mut unregistered = None;
        {
            let mut queues = lock_queues(&self.queues);
            match (queues.get_mut(&handle), sink_handle) {
                (Some(entry), sink_handle) if !entry.fanout.is_closed() => {
                    entry.sink_handle = sink_handle;
                }
                // The id was retired while the registration was in flight: the entry is kept (so the
                // handle answers) with its queue closed, and the sink — if `register` accepted it
                // before the fan-out closed — is taken back out. The socket's first take then reports
                // `closed` and it closes with 1000 instead of waiting for frames that never come
                // (review F1, F4: a registration must never outlive the fan-out it was made for).
                (Some(entry), sink_handle) => {
                    entry.queue.close();
                    unregistered = sink_handle;
                }
                (None, sink_handle) => {
                    // The entry was removed under the registration, which only a detach of this handle
                    // could do — impossible before it is returned. Put a closed entry back anyway, so
                    // the handle answers `closed` rather than `KeyError` below.
                    queues.insert(
                        handle,
                        AttachedQueue {
                            session_id: id.to_string(),
                            fanout: Arc::clone(&fanout),
                            sink_handle: None,
                            queue: Arc::clone(&queue),
                        },
                    );
                    queue.close();
                    unregistered = sink_handle;
                }
            }
        }
        if let Some(sink_handle) = unregistered {
            fanout.detach(sink_handle);
        }
        Ok((replay, handle))
    }

    /// **Temporary (step 4 deletes it).** Non-blocking: the frames queued since the last take, in
    /// arrival order, this queue's **cumulative** dropped bytes, and whether it is closed (the id was
    /// retired) — which is what lets the Python socket close with code 1000 as `close_ws_frontends`
    /// does today. A detached or never-issued handle is `KeyError`.
    pub fn take_frames(&self, handle: u64) -> Result<Frames, CoreError> {
        let (queue, fanout_closed) = {
            let queues = lock_queues(&self.queues);
            let entry = queues.get(&handle).ok_or(CoreError::UnknownQueue(handle))?;
            (Arc::clone(&entry.queue), entry.fanout.is_closed())
        };
        // The registry lock is released before the queue's own: a taker must not stall another socket's
        // take or a session's retire.
        let mut taken = queue.take();
        // A queue that was registered while the id's fan-out was open and whose id is retired now sees
        // `closed` even if `close_queues_of` closed it before this take started: the fan-out's flag is
        // the id's own, so the socket is told to close with 1000 either way (review F1).
        if fanout_closed {
            taken.closed = true;
        }
        Ok(taken)
    }

    /// **Temporary (step 4 deletes it).** Closes the queue, unregisters the sink and forgets the handle:
    /// `true` when this call detached it, `false` for a handle that was never issued or is already
    /// detached (a second detach). Works across a resume, like `detach_sink`.
    ///
    /// The registry lock is never missed by a deadline: its critical sections are short and always
    /// complete, so `false` always means what it says (review F4).
    pub fn detach_queue(&self, handle: u64) -> bool {
        let entry = lock_queues(&self.queues).remove(&handle);
        let Some(entry) = entry else {
            return false;
        };
        // The registry is released first: a detach only removes an entry, and closing the queue or
        // unregistering the sink must not stall another socket's take or a retire.
        entry.queue.close();
        if let Some(sink_handle) = entry.sink_handle {
            entry.fanout.detach(sink_handle);
        }
        true
    }

    /// Removes an ended session from the table, so its ring (512 KiB + slack) and its 3000-row screen stop
    /// living for the hub's lifetime. `Ok(false)` for an unknown id; `ValueError` for a live one (the hub
    /// kills first) and for an end whose record has not been drained yet — the exit record is the one
    /// report that cannot be re-created, so `retire` never drops it (the next drain carries it, then the
    /// hub retires).
    ///
    /// Every queue attached to the id is closed (`take_frames` reports `closed`) and the fan-out is
    /// retired; a later `spawn`/`attach_text` of the same id builds a fresh fan-out, so it carries
    /// nothing over from the incarnation this call removed.
    pub fn retire(&self, id: &str) -> Result<bool, CoreError> {
        // The claim spans the removal, the queue close and the fan-out close: a resume of this id that
        // is in flight is refused instead of taking over a fan-out this call is about to close, and one
        // that starts after this returns gets a fresh fan-out (review F1).
        let _claim = self.claim(id)?;
        let (session, fanout) = {
            let mut sessions = write_deadline(&self.sessions, BRIDGE_DEADLINE)
                .ok_or_else(|| CoreError::Deadline(format!("session table busy, {id} not retired")))?;
            let Some(session) = sessions.get(id).cloned() else {
                return Ok(false);
            };
            if let Some(reason) = session.retire_refusal() {
                return Err(CoreError::Invalid(format!(
                    "session {id} cannot be retired: {reason}"
                )));
            }
            sessions.remove(id);
            let fanout = session.fanout();
            (session, fanout)
        };
        // The table's reference goes first, so the incarnation is released here (pipes, a console close
        // helper) instead of waiting behind the fan-out. That is also what makes an `attach_sink`
        // racing this call able to hold the last reference: it drops its owner with no delivery lock
        // held (`Session::drop` asserts that in test builds).
        drop(session);
        self.close_queues_of(id);
        // §6.4: the id's console sockets close with 1000 here too, the way `close_ws_frontends` did on
        // archive; the fan-out close below keeps a socket that attached in between from staying open.
        self.ws.close_sockets(id);
        // No lock from here on: closing a fan-out hands back its sinks and a sink's `Drop` may wait on a
        // socket.
        drop(fanout.close());
        Ok(true)
    }

    /// Closes every queue registered for `id` and keeps its entry: `take_frames` must still answer
    /// `closed = true` until the socket detaches (§5.2 `retire`).
    fn close_queues_of(&self, id: &str) {
        // No deadline: `retire` either closes every queue of the id or it does not remove the session,
        // and a silent miss would let it report success while a socket never learns `closed` (review
        // F4). The hold is one hash walk with `FrameQueue::close` (a flag, a lock and a swap).
        let queues = lock_queues(&self.queues);
        for entry in queues.values() {
            if entry.session_id == id {
                entry.queue.close();
            }
        }
    }

    /// §5.2's `snapshot_all`: every session's archive-ready state inside one budget.
    pub fn snapshot_all(
        &self,
        max_lines: usize,
        max_bytes: usize,
        budget: Duration,
    ) -> Vec<SessionSnapshot> {
        let sessions: Vec<Arc<Session>> = match read_deadline(&self.sessions, budget) {
            Some(map) => map.values().cloned().collect(),
            None => return Vec::new(),
        };
        let end = Instant::now() + budget;
        sessions
            .into_iter()
            .map(|session| {
                let remaining = end.saturating_duration_since(Instant::now());
                session.snapshot(max_lines, max_bytes, remaining)
            })
            .collect()
    }

    /// Test-only: the session behind an id. The integration tests put a session into the state a thread
    /// panic leaves it in (`crashed`, `alive = false`, process still running) without panicking a thread.
    /// Compiled out unless the tests ask for it, so it cannot become part of the artifact's surface.
    #[cfg(any(test, feature = "test-hooks"))]
    #[doc(hidden)]
    pub fn session_for_test(&self, id: &str) -> Option<Arc<Session>> {
        self.sessions.read().ok()?.get(id).cloned()
    }

    pub fn stats(&self, deadline: Duration) -> Stats {
        let sessions: Vec<Arc<Session>> = match read_deadline(&self.sessions, deadline) {
            Some(map) => map.values().cloned().collect(),
            // A busy spawn insert costs one bounded call: report what the atomic counters hold.
            None => Vec::new(),
        };
        let mut stats = Stats {
            sessions: sessions.len(),
            notify_calls: self.notify_calls.load(Ordering::Relaxed),
            panics: self.panics.load(Ordering::Relaxed),
            ..Stats::default()
        };
        for session in &sessions {
            if session.alive() {
                stats.alive += 1;
            }
            if session.crashed() {
                stats.crashed += 1;
            }
            let (bytes, chunks, dropped) = session.counters();
            stats.bytes_total += bytes;
            stats.chunks_total += chunks;
            stats.dropped_bytes += dropped;
        }
        stats
    }

    /// Stops the ticker and the terminal socket server, and joins both. The caller must have released
    /// the GIL: the ticker may be inside a Python callback, and joining it while holding the GIL is
    /// exactly the deadlock B2 is about. Called from the ticker itself (a `notify` callback that drops
    /// the core), the ticker join is skipped; the socket threads are still joined, because they are
    /// never the ticker.
    pub fn shutdown(&self) {
        self.stopping.store(true, Ordering::Release);
        if let Ok(mut stop) = self.stop.lock() {
            *stop = true;
        }
        self.wake.notify_all();
        let on_ticker = self
            .ticker_id
            .lock()
            .ok()
            .and_then(|id| *id)
            .map(|id| id == thread::current().id())
            .unwrap_or(false);
        if !on_ticker {
            if let Ok(mut slot) = self.ticker.lock() {
                if let Some(handle) = slot.take() {
                    let _ = handle.join();
                }
            }
        }
        // §6.4: every socket closes, and the listener and socket threads are joined inside its budget.
        // Idempotent, so a second `shutdown` (the binding's `Drop` after Python's call) is cheap.
        self.ws.shutdown(SHUTDOWN_SOCKET_BUDGET);
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        self.shutdown();
    }
}

/// The queue registry's answers, at the level the bridge's Python callers depend on: a `true`/`false`
/// from `retire`/`detach_queue` is always exactly what happened, and an attach that loses a race with
/// `retire` never leaves a sink nobody can reach.
///
/// The registry is a plain mutex (review F4): its critical sections are one hash insert or remove and a
/// walk that closes queues, so "busy for a moment" cannot be turned into a wrong answer. These cases
/// therefore pin *completion*, not a deadline: a call that finds the registry held finishes once it is
/// released, and never returns a shorter answer first.
#[cfg(test)]
mod retire_gates {
    use super::*;

    fn core() -> Arc<Core> {
        Core::new(
            Box::new(|| false),
            Box::new(|_line: &str| {}),
            CoreOptions::default(),
        )
        .expect("core")
    }

    fn attach_text(core: &Core, id: &str) {
        core.attach_text(TextRequest {
            session_id: id.to_string(),
            cols: 40,
            rows: 6,
            flags: SessionFlags::default(),
            seed_history: None,
            seed: None,
        })
        .expect("attach_text");
    }

    fn drain_death(core: &Core, id: &str) {
        let end = Instant::now() + Duration::from_secs(2);
        while Instant::now() < end {
            if core
                .drain()
                .into_iter()
                .any(|record| record.session_id == id && !record.alive)
            {
                return;
            }
            thread::sleep(Duration::from_millis(5));
        }
        panic!("no death record for {id}");
    }

    /// `retire` closes every queue of the id before it reports success, even when another thread held the
    /// registry when it started: `true` never comes with an open queue behind it (review F4).
    #[test]
    fn retire_completes_under_a_held_registry_and_closes_every_queue() {
        let core = core();
        attach_text(&core, "retire-open-queue");
        let (_replay, first) = core.attach_queue("retire-open-queue", None).expect("attach");
        let (_replay, second) = core.attach_queue("retire-open-queue", None).expect("attach two");
        core.kill("retire-open-queue", Duration::from_millis(100))
            .expect("kill");
        drain_death(&core, "retire-open-queue");

        let registry = core.queues.lock().expect("queue registry");
        let retired = {
            let core = Arc::clone(&core);
            thread::spawn(move || core.retire("retire-open-queue"))
        };
        // The registry is held here, so `retire` is parked in `close_queues_of`; it must not answer yet.
        thread::sleep(Duration::from_millis(50));
        assert!(!retired.is_finished(), "retire answered while the registry was held");
        drop(registry);
        assert!(
            retired.join().expect("retire thread").expect("retire"),
            "retire answered without an error"
        );
        for handle in [first, second] {
            let taken = core.take_frames(handle).expect("a retired queue still answers");
            assert!(taken.closed, "retire reported success and left a queue open");
            assert!(taken.frames.is_empty(), "a closed queue holds no bytes");
            assert!(core.detach_queue(handle), "a closed queue still detaches");
        }
        core.shutdown();
    }

    /// `detach_queue`'s `false` means the handle is already gone — never "the registry was busy". A
    /// second take after it says `UnknownQueue` (review F4).
    #[test]
    fn detach_false_means_the_handle_is_gone() {
        let core = core();
        attach_text(&core, "detach-once");
        let (_replay, handle) = core.attach_queue("detach-once", None).expect("attach");

        let registry = core.queues.lock().expect("queue registry");
        let detacher = {
            let core = Arc::clone(&core);
            thread::spawn(move || core.detach_queue(handle))
        };
        thread::sleep(Duration::from_millis(50));
        assert!(!detacher.is_finished(), "a detach answered while the registry was held");
        drop(registry);
        assert!(detacher.join().expect("detach thread"), "the first detach is true");
        assert!(!core.detach_queue(handle), "the second detach is false");
        assert!(
            matches!(core.take_frames(handle), Err(CoreError::UnknownQueue(_))),
            "false said the handle was gone, so it must not take frames any more"
        );
        core.kill("detach-once", Duration::from_millis(100))
            .expect("kill");
        core.shutdown();
    }

    /// An `attach_queue` whose registration loses the race with a `retire` reports a closed queue and
    /// leaves nothing registered: no sink without a handle, no entry without a queue (review F1, F4).
    #[test]
    fn an_attach_that_loses_to_a_retire_reports_closed_and_leaves_no_sink() {
        let core = core();
        attach_text(&core, "attach-vs-retire");
        core.kill("attach-vs-retire", Duration::from_millis(100))
            .expect("kill");
        drain_death(&core, "attach-vs-retire");
        // The registry is held by this thread while the attach runs on another, so the attach's
        // registration and the retire's close cannot be ordered by luck: whichever wins, the handle the
        // attach returns must answer `closed` and there must be no sink left behind afterwards.
        let registry = core.queues.lock().expect("queue registry");
        let attacher = {
            let core = Arc::clone(&core);
            thread::spawn(move || core.attach_queue("attach-vs-retire", None))
        };
        thread::sleep(Duration::from_millis(50));
        let retired = {
            let core = Arc::clone(&core);
            thread::spawn(move || core.retire("attach-vs-retire"))
        };
        drop(registry);
        let (result, retire_result) = (
            attacher.join().expect("attach thread"),
            retired.join().expect("retire thread"),
        );
        assert!(retire_result.expect("retire"), "the ended session was retired");
        let (_replay, handle) = result.expect("the attach still answers with a handle");
        let taken = core.take_frames(handle).expect("a handle from the losing attach answers");
        assert!(taken.closed, "a queue attached to a retired id reports closed");
        assert!(core.detach_queue(handle));
        // Nothing of the attach survives the retire: no queue answers, no entry is left.
        assert!(matches!(core.take_frames(handle), Err(CoreError::UnknownQueue(_))));
        assert_eq!(core.stats(Duration::from_millis(100)).sessions, 0);
        core.shutdown();
    }
}
