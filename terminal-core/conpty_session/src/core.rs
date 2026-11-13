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

use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock, RwLockReadGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use terminal_stream::FrameSink;

use crate::conpty::{self, SpawnSpec};
use crate::session::{
    DrainRecord, RawReply, ResizeError, ScreenReply, Session, SessionFlags, SessionOptions,
    SessionSnapshot, TextReply, TickFlags,
};

/// Calls this step's Python layer makes. `drain_interval` is the 50 ms notify window; `tick_interval`
/// is how often the ticker looks (§5.3) and flushes due batches (§5.4).
#[derive(Clone, Copy, Debug)]
pub struct CoreOptions {
    /// Default session flags: a `spawn`'s `flags` dict overrides what it names.
    pub flags: SessionFlags,
    pub write_queue_bytes: usize,
    pub drain_interval: Duration,
    pub tick_interval: Duration,
}

impl Default for CoreOptions {
    fn default() -> Self {
        Self {
            flags: SessionFlags::default(),
            write_queue_bytes: 1024 * 1024,
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
    Spawn(String),
    Invalid(String),
    Deadline(String),
    Internal(String),
}

impl std::fmt::Display for CoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CoreError::UnknownSession(id) => write!(f, "unknown session: {id}"),
            CoreError::Spawn(message)
            | CoreError::Invalid(message)
            | CoreError::Deadline(message)
            | CoreError::Internal(message) => write!(f, "{message}"),
        }
    }
}

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

pub struct Core {
    sessions: RwLock<HashMap<String, Arc<Session>>>,
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
    ticker_id: Mutex<Option<thread::ThreadId>>,
}

impl Core {
    /// Starts the ticker thread. `notify` is called at most every `drain_interval` and only when a
    /// session is dirty (§5.3); `log` is called on spawn, kill and panic only.
    pub fn new(
        notify: Box<dyn Fn() -> bool + Send + Sync>,
        log: Box<dyn Fn(&str) + Send + Sync>,
        opts: CoreOptions,
    ) -> Arc<Self> {
        let tick = Arc::new(TickFlags::default());
        let panics = Arc::new(AtomicU64::new(0));
        let core = Arc::new(Self {
            sessions: RwLock::new(HashMap::new()),
            notify,
            log: Arc::from(log),
            opts,
            tick,
            stop: Mutex::new(false),
            wake: Condvar::new(),
            stopping: AtomicBool::new(false),
            notify_calls: AtomicU64::new(0),
            panics,
            ticker: Mutex::new(None),
            ticker_id: Mutex::new(None),
        });
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
        core
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
                let now = Instant::now();
                if let Ok(map) = self.sessions.try_read() {
                    for session in map.values() {
                        let _ = catch_unwind(AssertUnwindSafe(|| session.flush_frames(now)));
                    }
                }
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
        let sessions: Vec<Arc<Session>> = match read_deadline(&self.sessions, Duration::from_millis(50)) {
            Some(map) => map.values().cloned().collect(),
            None => return Vec::new(),
        };
        sessions
            .into_iter()
            .filter_map(|session| session.drain_record())
            .collect()
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
        Ok(self.session(id)?.attach_sink(sink))
    }

    pub fn detach_sink(&self, id: &str, handle: u64) -> Result<bool, CoreError> {
        Ok(self.session(id)?.detach_sink(handle))
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

    /// Stops the ticker and joins it. The caller must have released the GIL: the ticker may be inside a
    /// Python callback, and joining it while holding the GIL is exactly the deadlock B2 is about. Called
    /// from the ticker itself (a `notify` callback that drops the core), the join is skipped.
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
        if on_ticker {
            return;
        }
        if let Ok(mut slot) = self.ticker.lock() {
            if let Some(handle) = slot.take() {
                let _ = handle.join();
            }
        }
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        self.shutdown();
    }
}