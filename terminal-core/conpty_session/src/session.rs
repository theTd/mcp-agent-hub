//! One console session: its pipes, its job, a reader thread, a writer thread and a reaper thread.
//!
//! The reader runs step 2's pipeline on every read (`terminal_stream`): decode, W8/OSC strips, the W9
//! ring, the parse. One mutex (`Session::text`) covers the ring append, the parse and the coalescing
//! batch, which is what makes Q11 hold: a sink that attaches sees every chunk exactly once, either in
//! the replay snapshot it is handed or in the frames that follow (the pending batch is flushed to the
//! sockets that existed before it registers).
//!
//! A text-only session (`attach_text`) has no pipes, no job and no threads: the same state, fed by
//! `feed` instead of a reader, which is what the DSH virtual sessions need.
//!
//! Exit detection is the reaper's job. The reader's `ReadFile` cannot signal a natural exit: the write
//! end of the output pipe belongs to conhost, which stays alive while we hold the HPCON, so the read
//! never completes on its own (review B1). The reaper waits on the process handle instead, records the
//! exit code and closes the console on a helper thread so the pending read returns; the death record is
//! published only after that read has ended, so the last bytes are in the buffer before `alive = false`.
//!
//! `kill` and the reaper share one teardown (`finish_exit`), so that ordering rule exists in one place:
//! close the console, wait for the reader's end of output inside the remaining budget, then publish —
//! or publish on the deadline, because a wedged close must cost the last bytes, never the record. The
//! console slot is three-state (`Open`/`Closing`/`Closed`), never "taken ⇒ closed", so a second closer
//! that finds a peer inside `ClosePseudoConsole` waits for that peer's completion instead of publishing
//! early (B1).
//!
//! A panic anywhere in a thread body ends **that** session (`crashed`, `alive = false`, buffer kept) and
//! nothing else (§6.3), and it is counted in `panics`.

use std::collections::VecDeque;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use windows::Win32::Foundation::WAIT_OBJECT_0;
use windows::Win32::Storage::FileSystem::{ReadFile, WriteFile};
use windows::Win32::System::Console::{ResizePseudoConsole, COORD};
use windows::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject, INFINITE};

use terminal_stream::{
    trim_history_text, Coalescer, FrameSink, StreamOptions, TerminalStream,
};

use crate::conpty::ConPtyProcess;
use crate::handles::{OwnedHandle, OwnedPseudoConsole};
use crate::job::JobObject;
use crate::job_tree;

/// How long the exit/kill teardown waits for a helper thread to close the pseudoconsole (§12.7).
pub const CONSOLE_CLOSE_DEADLINE: Duration = Duration::from_secs(2);
/// `STILL_ACTIVE`: GetExitCodeProcess's "has not exited" value.
const STILL_ACTIVE: i32 = 259;
/// One ReadFile / WriteFile step. 8 KiB matches the hub's old `pty.read(4096)` order of magnitude
/// while halving the number of syscalls for bulk output. Public so the recorder tap reads with the size
/// the core reads with: a chunk boundary in the corpus is a chunk boundary here (step 2a).
pub const IO_CHUNK: usize = 8192;
/// The reaper blocks in `WaitForSingleObject`, then runs the teardown and finally calls `log` — an
/// arbitrary Python callback, not a bare wait — so it cannot stay on the 64 KiB a wait alone needed
/// (nit 3). 512 KiB is reserved, not committed; the thread touches a few pages.
const REAPER_STACK: usize = 512 * 1024;
/// The close helper only runs `ClosePseudoConsole` and the slot bookkeeping.
const CLOSE_HELPER_STACK: usize = 64 * 1024;

/// §5.2's `spawn`/`attach_text` flags, one struct because every call that starts a session takes them.
#[derive(Clone, Copy, Debug)]
pub struct SessionFlags {
    /// True when the session is a resume (`resume_count > 0`): W8's `ESC[3J` strip is on.
    pub strip_esc3j_on_output: bool,
    pub strip_osc_color_queries: bool,
    pub history_rows: usize,
    pub raw_cap_bytes: usize,
    pub raw_slack_bytes: usize,
}

impl Default for SessionFlags {
    fn default() -> Self {
        Self {
            strip_esc3j_on_output: false,
            strip_osc_color_queries: true,
            history_rows: 3000,
            raw_cap_bytes: 512 * 1024,
            raw_slack_bytes: 64 * 1024,
        }
    }
}

/// Tunables a session needs from the core.
#[derive(Clone, Copy, Debug)]
pub struct SessionOptions {
    pub write_queue_bytes: usize,
    pub flags: SessionFlags,
}

/// The core's ticker sees "something changed" through these two flags, never through a session lock.
#[derive(Debug, Default)]
pub struct TickFlags {
    pub pending: AtomicBool,
    pub urgent: AtomicBool,
}

/// What `drain` reports for one dirty session (§5.3).
#[derive(Clone, Debug)]
pub struct DrainRecord {
    pub session_id: String,
    pub alive: bool,
    pub exit_code: Option<i32>,
    pub pid: u32,
    pub bytes_total: u64,
    pub chunks_total: u64,
    pub last_output_at: Option<f64>,
    pub dropped_bytes: u64,
    pub crashed: bool,
    pub degraded: bool,
    /// Bumped once per read that reached the parser, so a reader can tell screens apart.
    pub screen_rev: u64,
    /// Only for a watched session whose `screen_rev` changed and whose interval is due.
    pub screen_text: Option<String>,
}

/// The pseudoconsole's single close point. Three states, because "somebody took the handle" and "the
/// console is closed" are different answers (B1): the thread that takes the handle flips the slot to
/// `Closing`, the helper that finishes `ClosePseudoConsole` flips it to `Closed` and wakes every waiter,
/// so a second closer can wait for the completion instead of publishing a death over a live read.
struct ConsoleSlot {
    state: Mutex<ConsoleState>,
    /// Signalled when `state` becomes `Closed`.
    done: Condvar,
    /// The core's panic counter, so a close-helper panic is counted like every other one (nit 1).
    panics: Arc<AtomicU64>,
}

enum ConsoleState {
    /// The handle is here; nobody has started the close.
    Open(OwnedPseudoConsole),
    /// A helper is inside `ClosePseudoConsole` (or the handle was leaked because that helper could not
    /// start, in which case the state never leaves `Closing`).
    Closing,
    /// `ClosePseudoConsole` returned: the reader's pending read has been released.
    Closed,
}

/// What closing the console did, so the callers can log the one case that needs a reason (nit 2).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CloseProgress {
    /// `ClosePseudoConsole` returned — now or by a peer this call waited for.
    Closed,
    /// Still inside `ClosePseudoConsole` at the deadline (this call's helper, or a peer's).
    Pending,
    /// The slot lock itself was busy at the deadline, so nothing was attempted.
    Busy,
    /// The helper thread could not be spawned; the handle was leaked rather than closed on a thread
    /// that may be the reader (§12.7).
    NoHelper,
}

impl ConsoleSlot {
    fn new(console: OwnedPseudoConsole, panics: Arc<AtomicU64>) -> Self {
        Self {
            state: Mutex::new(ConsoleState::Open(console)),
            done: Condvar::new(),
            panics,
        }
    }

    /// Starts the close if nobody did: `Ok(Some(handle))` to close on a helper, `Ok(None)` when a peer
    /// already owns it (or it is done), `Err(Busy)` when the slot lock missed `deadline` (SF1: never an
    /// unbounded wait, so the caller's deadline path still publishes).
    fn begin_close(&self, deadline: Duration) -> Result<Option<OwnedPseudoConsole>, CloseProgress> {
        let mut state = match lock_deadline(&self.state, deadline) {
            Some(state) => state,
            None => return Err(CloseProgress::Busy),
        };
        match std::mem::replace(&mut *state, ConsoleState::Closed) {
            ConsoleState::Open(console) => {
                *state = ConsoleState::Closing;
                Ok(Some(console))
            }
            // Put the state back: `Closed` stays closed, `Closing` stays closing.
            other => {
                *state = other;
                Ok(None)
            }
        }
    }

    /// Waits up to `deadline` for a `Closing` slot to reach `Closed`. `true` = closed.
    fn wait_closed(&self, deadline: Duration) -> bool {
        let end = Instant::now() + deadline;
        let mut state = match lock_deadline(&self.state, deadline) {
            Some(state) => state,
            None => return false,
        };
        loop {
            if matches!(*state, ConsoleState::Closed) {
                return true;
            }
            let remaining = end.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return false;
            }
            let (guard, _) = self
                .done
                .wait_timeout(state, remaining)
                .unwrap_or_else(|error| error.into_inner());
            state = guard;
        }
    }

    /// Counts a close-helper panic (its payload is a bug in this crate, not a session condition).
    fn note_panic(&self) {
        self.panics.fetch_add(1, Ordering::Relaxed);
    }

    /// `ClosePseudoConsole` returned (or the helper panicked unwinding through it): the read is released
    /// and every waiter may proceed. Bounded, because a resize may hold the slot (§12.7).
    fn mark_closed(&self) {
        if let Some(mut state) = lock_deadline(&self.state, CONSOLE_CLOSE_DEADLINE) {
            *state = ConsoleState::Closed;
        }
        self.done.notify_all();
    }
}

/// Handles a session's threads use, shared by ownership rather than copied.
struct Resources {
    process: OwnedHandle,
    input: OwnedHandle,
    output: OwnedHandle,
    job: Arc<JobObject>,
    job_assigned: bool,
    console: Arc<ConsoleSlot>,
}

impl Drop for Resources {
    fn drop(&mut self) {
        // Never wait on the slot here: this can run on the Python GC thread, and a blocked drop stalls
        // the interpreter. `try_lock` separates "nobody is closing it" from "a peer is"; the peer owns
        // the close in that case, and on any other miss the HPCON is leaked instead of closed on this
        // thread (§12.7, SF1).
        let console = match self.console.state.try_lock() {
            Ok(mut state) => match std::mem::replace(&mut *state, ConsoleState::Closing) {
                ConsoleState::Open(console) => Some(console),
                other => {
                    *state = other;
                    None
                }
            },
            Err(_) => None,
        };
        if let Some(console) = console {
            let _ = close_console_off_thread(console, CONSOLE_CLOSE_DEADLINE, &self.console);
        }
    }
}

/// Why `write` refused to queue anything. Both variants mean "nothing was queued", so the caller can
/// retry the whole text without duplicating bytes (§5.2).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteError {
    /// The queue stayed full until the deadline.
    Timeout,
    /// The input pipe failed; the session's input is closed for good.
    Closed,
}

/// Why `resize` failed. `core.rs` maps these onto §5.1's families: a closed console is a refused call
/// (`ValueError`, the family a dead session gets), a missed deadline is `TimeoutError`, and a Win32
/// failure is `RuntimeError`.
#[derive(Debug, Clone)]
pub enum ResizeError {
    /// The pseudoconsole is closing or closed, so there is nothing to resize.
    Closed(String),
    /// The console slot's lock missed its deadline.
    Deadline(String),
    /// `ResizePseudoConsole` itself failed.
    Failed(String),
}

#[derive(Debug, Default)]
struct WriterState {
    chunks: VecDeque<Vec<u8>>,
    pending: usize,
    closed: bool,
}

/// The per-session write queue: whole chunks in, whole chunks out, one writer thread consuming them so
/// D7/D1 arrival order is the pipe's order.
#[derive(Debug)]
struct WriteQueue {
    state: Mutex<WriterState>,
    ready: Condvar,
    cap: usize,
}

impl WriteQueue {
    fn new(cap: usize) -> Self {
        Self {
            state: Mutex::new(WriterState::default()),
            ready: Condvar::new(),
            cap,
        }
    }

    /// Whether a write of `len` bytes fits now. A text larger than the cap is accepted whole when the
    /// queue is empty (SF2): the hub writes one WebSocket frame per call, un-chunked, so the fit test on
    /// `pending + len <= cap` alone made a paste larger than the cap a permanent `Timeout`, while
    /// pywinpty wrote it. The all-or-nothing contract and `TimeoutError` stay for everything else.
    fn fits(&self, state: &WriterState, len: usize) -> bool {
        state.pending + len <= self.cap || state.pending == 0
    }

    /// Queues `text` as one chunk or nothing at all: empty text queues nothing (and returns 0), a queue
    /// that stays over its cap until `deadline` raises `Timeout` with nothing queued.
    fn push(&self, text: &[u8], deadline: Duration) -> Result<usize, WriteError> {
        if text.is_empty() {
            return Ok(0);
        }
        let end = Instant::now() + deadline;
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            if state.closed {
                return Err(WriteError::Closed);
            }
            if self.fits(&state, text.len()) {
                state.pending += text.len();
                state.chunks.push_back(text.to_vec());
                self.ready.notify_all();
                return Ok(text.len());
            }
            let remaining = end.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(WriteError::Timeout);
            }
            let (guard, wait) = self
                .ready
                .wait_timeout(state, remaining)
                .unwrap_or_else(|e| e.into_inner());
            state = guard;
            if wait.timed_out() && !self.fits(&state, text.len()) {
                return if state.closed {
                    Err(WriteError::Closed)
                } else {
                    Err(WriteError::Timeout)
                };
            }
        }
    }

    /// Next chunk, blocking until one arrives; `None` once the queue is closed and empty.
    fn pop(&self) -> Option<Vec<u8>> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            if let Some(chunk) = state.chunks.pop_front() {
                return Some(chunk);
            }
            if state.closed {
                return None;
            }
            state = self.ready.wait(state).unwrap_or_else(|e| e.into_inner());
        }
    }

    /// The chunk is in the pipe: free its bytes for the next `push`.
    fn complete(&self, len: usize) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.pending = state.pending.saturating_sub(len);
        self.ready.notify_all();
    }

    /// Closes the queue and returns the bytes that will never be written.
    fn close(&self) -> usize {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.closed = true;
        let dropped = state.pending;
        state.chunks.clear();
        state.pending = 0;
        self.ready.notify_all();
        dropped
    }

    #[cfg(test)]
    fn pending(&self) -> usize {
        self.state.lock().map(|state| state.pending).unwrap_or(0)
    }
}

/// Polling deadline lock: a wedged holder costs one bounded wait, never an unbounded block (§5.2).
pub(crate) fn lock_deadline<'a, T>(
    mutex: &'a Mutex<T>,
    deadline: Duration,
) -> Option<MutexGuard<'a, T>> {
    let end = Instant::now() + deadline;
    loop {
        match mutex.try_lock() {
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

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as u64)
        .unwrap_or(0)
}

/// `WaitForSingleObject`'s timeout from a `Duration`, clamped below `INFINITE`: `grace_ms` comes from
/// Python, and an unclamped `as u32` turns a large value into a tiny wait while exactly `u32::MAX`
/// becomes an unbounded one (nit 6).
fn wait_timeout(remaining: Duration) -> u32 {
    const BELOW_INFINITE: u128 = INFINITE as u128 - 1;
    remaining.as_millis().min(BELOW_INFINITE) as u32
}

/// Closes a pseudoconsole on its own thread and waits at most `deadline`; `Pending` means the helper is
/// still inside `ClosePseudoConsole` and will finish on its own (§12.7). The slot is `Closing` while the
/// helper runs and `Closed` once it returned, so a second caller waits on the slot's completion (B1)
/// instead of mistaking "closing" for "closed".
fn close_console_off_thread(
    console: OwnedPseudoConsole,
    deadline: Duration,
    slot: &Arc<ConsoleSlot>,
) -> CloseProgress {
    // The handle travels through a shared slot rather than the closure's capture, so a failed spawn can
    // still take it back and leak it instead of dropping it — a dropped value closes the console on the
    // calling thread, which is the reader thread on the panic path (nit 2).
    let holder = Arc::new(Mutex::new(Some(console)));
    let helper_holder = Arc::clone(&holder);
    let helper_slot = Arc::clone(slot);
    let spawned = thread::Builder::new()
        .name("core-close".to_string())
        .stack_size(CLOSE_HELPER_STACK)
        .spawn(move || {
            #[cfg(feature = "test-hooks")]
            crate::test_hooks::close_delay();
            let console = helper_holder
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .take();
            let Some(console) = console else { return };
            if catch_unwind(AssertUnwindSafe(|| console.close())).is_err() {
                helper_slot.note_panic();
            }
            helper_slot.mark_closed();
        });
    if spawned.is_err() {
        // No helper exists, so nobody will close the handle: leak it. Closing here would run
        // `ClosePseudoConsole` on this thread, and this thread is the reader on the panic path.
        let leaked = holder.lock().unwrap_or_else(|error| error.into_inner()).take();
        std::mem::forget(leaked);
        return CloseProgress::NoHelper;
    }
    if slot.wait_closed(deadline) {
        CloseProgress::Closed
    } else {
        CloseProgress::Pending
    }
}

/// How often a watched session hands its screen to `drain` (§5.2 `watch_screen`), and which render it
/// last sent.
struct Watch {
    max_lines: usize,
    interval: Duration,
    last_sent: Option<Instant>,
    last_rev: u64,
}

/// The output pipeline's state behind one mutex: ring, parser and coalescing batch. Q11 needs them under
/// the same lock, because "what the replay holds" and "what the next frame holds" must not both include a
/// chunk.
struct TextState {
    stream: TerminalStream,
    batch: Coalescer,
    watch: Option<Watch>,
    /// The post-strip text of the read in flight, reused so a read does not allocate.
    frame: Vec<u8>,
}

/// What one call feeds the pipeline: a pipe read, or text a virtual session was handed.
enum Ingest<'a> {
    Bytes(&'a [u8]),
    Text(&'a str),
}

struct SinkEntry {
    handle: u64,
    sink: Arc<dyn FrameSink>,
}

/// `screen`'s reply (§5.2): the rendered rows, the cursor, the size and the revision.
#[derive(Clone, Debug)]
pub struct ScreenReply {
    pub text: String,
    pub cursor_x: u16,
    pub cursor_y: u16,
    pub cols: u16,
    pub rows: u16,
    pub rev: u64,
}

/// A rendered text reply with the revision it reflects (`scrollback`, `replay`'s caller).
#[derive(Clone, Debug)]
pub struct TextReply {
    pub text: String,
    pub rev: u64,
}

/// `raw`'s reply: the trimmed history, its byte count and its revision (`_scrollback_blob`'s inputs).
#[derive(Clone, Debug)]
pub struct RawReply {
    pub text: String,
    pub bytes: u64,
    pub rev: u64,
}

/// One session in `snapshot_all`'s reply: what `persist_session` archives at shutdown (§6.6).
#[derive(Clone, Debug)]
pub struct SessionSnapshot {
    pub session_id: String,
    pub alive: bool,
    pub exit_code: Option<i32>,
    pub pid: u32,
    pub crashed: bool,
    pub degraded: bool,
    pub screen_text: String,
    pub scrollback_text: String,
    pub raw_text: String,
    pub raw_rev: u64,
    pub raw_bytes: u64,
}

/// The last successful reply of each render call, returned when its lock misses the deadline (the call
/// then marks the session `degraded`, §5.2).
#[derive(Default)]
struct CachedText {
    screen: String,
    screen_rev: u64,
    scrollback: String,
    raw: String,
    raw_rev: u64,
    raw_bytes: u64,
    tail: String,
}

pub struct Session {
    pub id: String,
    pub pid: u32,
    /// Absent for a text-only session (`attach_text`): no pipes, no job, no threads.
    resources: Option<Arc<Resources>>,
    text: Mutex<TextState>,
    cached: Mutex<CachedText>,
    sinks: Mutex<Vec<SinkEntry>>,
    next_sink: AtomicU64,
    writer: WriteQueue,
    alive: AtomicBool,
    exit_code: AtomicI32,
    /// Set once the output pipe stopped being read, so a later exit does not wait for a reader that is
    /// already gone before publishing the death record.
    output_ended: AtomicBool,
    bytes_total: AtomicU64,
    chunks_total: AtomicU64,
    /// The stream's `screen_rev`, mirrored so `drain` reads atomics only (§5.3).
    screen_rev: AtomicU64,
    dropped_bytes: AtomicU64,
    last_output_ms: AtomicU64,
    dirty: AtomicBool,
    crashed: AtomicBool,
    degraded: AtomicBool,
    cols: AtomicU32,
    rows: AtomicU32,
    tick: Arc<TickFlags>,
    panics: Arc<AtomicU64>,
    log: Arc<dyn Fn(&str) + Send + Sync>,
}

impl Session {
    /// The state both constructors share. `seed_history` is a resumed session's archived scrollback:
    /// history, not output, so the byte counters stay at zero until the new process prints (A1).
    #[allow(clippy::too_many_arguments)]
    fn new_state(
        id: String,
        pid: u32,
        resources: Option<Arc<Resources>>,
        cols: u16,
        rows: u16,
        options: SessionOptions,
        tick: Arc<TickFlags>,
        panics: Arc<AtomicU64>,
        log: Arc<dyn Fn(&str) + Send + Sync>,
        seed_history: Option<&str>,
    ) -> Arc<Self> {
        let mut stream = TerminalStream::new(StreamOptions {
            cols: cols as usize,
            rows: rows as usize,
            history_rows: options.flags.history_rows,
            raw_cap_bytes: options.flags.raw_cap_bytes,
            raw_slack_bytes: options.flags.raw_slack_bytes,
            strip_esc3j_on_output: options.flags.strip_esc3j_on_output,
            strip_osc_color_queries: options.flags.strip_osc_color_queries,
        });
        if let Some(seed) = seed_history {
            if !seed.is_empty() {
                stream.seed_history(seed);
            }
        }
        Arc::new(Self {
            id,
            pid,
            resources,
            text: Mutex::new(TextState {
                stream,
                batch: Coalescer::new(),
                watch: None,
                frame: Vec::with_capacity(8192),
            }),
            cached: Mutex::new(CachedText::default()),
            sinks: Mutex::new(Vec::new()),
            next_sink: AtomicU64::new(1),
            writer: WriteQueue::new(options.write_queue_bytes),
            alive: AtomicBool::new(true),
            exit_code: AtomicI32::new(-1),
            output_ended: AtomicBool::new(false),
            bytes_total: AtomicU64::new(0),
            screen_rev: AtomicU64::new(0),
            chunks_total: AtomicU64::new(0),
            dropped_bytes: AtomicU64::new(0),
            last_output_ms: AtomicU64::new(0),
            dirty: AtomicBool::new(false),
            crashed: AtomicBool::new(false),
            degraded: AtomicBool::new(false),
            cols: AtomicU32::new(cols as u32),
            rows: AtomicU32::new(rows as u32),
            tick,
            panics,
            log,
        })
    }

    /// Wraps a spawned process and starts its reader, writer and reaper threads. The returned lines are
    /// the spawn's log lines: the caller logs them after it re-attaches, because this runs with the GIL
    /// released (§5.2: no Python callback inside the detached spawn path).
    pub fn start(
        id: String,
        process: ConPtyProcess,
        cols: u16,
        rows: u16,
        options: SessionOptions,
        tick: Arc<TickFlags>,
        panics: Arc<AtomicU64>,
        log: Arc<dyn Fn(&str) + Send + Sync>,
        seed_history: Option<String>,
        ws_prefix: Option<String>,
    ) -> Result<(Arc<Self>, Vec<String>), String> {
        let ConPtyProcess {
            pid,
            command_line,
            process,
            input,
            output,
            console,
            job,
            job_assigned,
            job_error,
        } = process;
        let mut spawn_log = Vec::new();
        if let Some(error) = job_error {
            spawn_log.push(format!(
                "session {id}: job assignment failed, kill will walk the tree with ToolHelp: {error}"
            ));
        }
        spawn_log.push(format!(
            "session {id}: spawned pid {pid} (job {}) cmdline {command_line}",
            if job_assigned { "assigned" } else { "not assigned" }
        ));

        let session = Self::new_state(
            id,
            pid,
            Some(Arc::new(Resources {
                process,
                input,
                output,
                job,
                job_assigned,
                console: Arc::new(ConsoleSlot::new(console, Arc::clone(&panics))),
            })),
            cols,
            rows,
            options,
            tick,
            panics,
            log,
            seed_history.as_deref(),
        );

        // A1: the resume separator reaches every socket already attached to this id before the first
        // byte of the new process, which is why it goes out here rather than from Python's spawn path.
        if let Some(prefix) = ws_prefix.as_deref() {
            session.push_frame(prefix);
        }

        // Thread starts are the last step that can fail, and a failure here used to leave a live process
        // with no table entry: unreachable, unreaped (rereview item 3). The error path ends the tree and
        // closes the console, then reports the failure so `core.spawn` raises instead of hiding it.
        let started = (|| -> std::io::Result<()> {
            let reader = Arc::clone(&session);
            thread::Builder::new()
                .name(format!("core-read-{}", session.id))
                .spawn(move || reader.reader_body())?;
            let writer = Arc::clone(&session);
            thread::Builder::new()
                .name(format!("core-write-{}", session.id))
                .spawn(move || writer.writer_body())?;
            let reaper = Arc::clone(&session);
            thread::Builder::new()
                .name(format!("core-reap-{}", session.id))
                .stack_size(REAPER_STACK)
                .spawn(move || reaper.reaper_body())?;
            Ok(())
        })();
        if let Err(error) = started {
            let resources = session.resources.clone().expect("a spawned session has resources");
            let terminated = if resources.job_assigned {
                resources.job.terminate(1)
            } else {
                job_tree::terminate_tree(session.pid);
                Ok(())
            };
            // Close on a helper so a reader that did start leaves its `ReadFile`; no session-level wait,
            // because the node is not in the table and nobody drains it.
            let _ = session.close_console(CONSOLE_CLOSE_DEADLINE);
            return Err(format!(
                "session {}: could not start its threads ({error}); the tree was terminated{}",
                session.id,
                match terminated {
                    Ok(()) => String::new(),
                    Err(error) => format!(" (job terminate failed: {error})"),
                }
            ));
        }

        Ok((session, spawn_log))
    }

    /// A session with no process (`attach_text`, §5.2): the DSH virtual sessions' screen and raw history,
    /// fed through `feed`, alive for as long as the hub keeps it. `kill`/`resize` never touch a console
    /// that does not exist.
    pub fn start_text(
        id: String,
        cols: u16,
        rows: u16,
        options: SessionOptions,
        tick: Arc<TickFlags>,
        panics: Arc<AtomicU64>,
        log: Arc<dyn Fn(&str) + Send + Sync>,
        seed_history: Option<&str>,
    ) -> Arc<Self> {
        Self::new_state(id, 0, None, cols, rows, options, tick, panics, log, seed_history)
    }


    /// One place that reaches the core's `log` callable, so every line goes through the same path.
    fn log(&self, message: &str) {
        (self.log)(message);
    }

    /// The PTY half of a session; `None` for a text-only session.
    fn resources(&self) -> Option<&Resources> {
        self.resources.as_deref()
    }

    /// ReadFile until the console's output pipe stops producing; a panic here crashes only this session.
    fn reader_body(self: Arc<Self>) {
        let Some(resources) = self.resources.clone() else {
            return;
        };
        let result = catch_unwind(AssertUnwindSafe(|| {
            let mut chunk = [0u8; IO_CHUNK];
            loop {
                let mut read = 0u32;
                // SAFETY: the pipe handle is open for the session's lifetime and `chunk` is a valid
                // writable buffer; no overlapped I/O.
                let status = unsafe {
                    ReadFile(
                        resources.output.get(),
                        Some(&mut chunk),
                        Some(&mut read),
                        None,
                    )
                };
                if status.is_err() || read == 0 {
                    break;
                }
                // The reader never gives up on the text lock: a missed byte here is a lost byte, and the
                // exit ordering (death record after the last byte) depends on this append.
                self.ingest(Ingest::Bytes(&chunk[..read as usize]), None);
            }
        }));
        if result.is_err() {
            self.note_panic();
            self.output_ended.store(true, Ordering::Release);
            self.mark_crashed("reader thread panicked");
        } else {
            // The read is over: any byte it returned is in the buffer, so a death record published from
            // here cannot overtake the last output.
            self.output_ended.store(true, Ordering::Release);
            self.on_output_closed();
        }
    }

    /// WriteFile each queued chunk in arrival order; one writer thread per session is what keeps D7/D1
    /// byte order intact.
    fn writer_body(self: Arc<Self>) {
        let Some(resources) = self.resources.clone() else {
            return;
        };
        let result = catch_unwind(AssertUnwindSafe(|| loop {
            let Some(mut chunk) = self.writer.pop() else {
                return;
            };
            let mut written_total = 0usize;
            while written_total < chunk.len() {
                let mut written = 0u32;
                // SAFETY: the input pipe handle is open for the session's lifetime and the slice is
                // valid for the call.
                let status = unsafe {
                    WriteFile(
                        resources.input.get(),
                        Some(&chunk[written_total..]),
                        Some(&mut written),
                        None,
                    )
                };
                if status.is_err() || written == 0 {
                    let dropped = self.writer.close();
                    self.log(&format!(
                        "session {}: input pipe closed, {} queued bytes dropped",
                        self.id, dropped
                    ));
                    return;
                }
                written_total += written as usize;
            }
            self.writer.complete(chunk.len());
            chunk.clear();
        }));
        if result.is_err() {
            self.note_panic();
            self.mark_crashed("writer thread panicked");
        }
    }

    /// Waits on the process handle for the whole session's life: this is the only exit signal a natural
    /// end of the child has, because the output pipe stays open while conhost lives (review B1).
    fn reaper_body(self: Arc<Self>) {
        let Some(resources) = self.resources.clone() else {
            return;
        };
        let result = catch_unwind(AssertUnwindSafe(|| {
            // SAFETY: the process handle is owned by the session and open for waiting.
            let waited = unsafe { WaitForSingleObject(resources.process.get(), INFINITE) };
            if waited == WAIT_OBJECT_0 {
                self.on_process_exit();
            } else {
                // Not reachable with an infinite wait and a valid handle, but a silent return here would
                // be a session that never dies and never publishes (rereview item 1, nit).
                self.log(&format!(
                    "session {}: reaper wait returned {waited:?}; no exit was recorded",
                    self.id
                ));
            }
        }));
        if result.is_err() {
            self.note_panic();
            self.mark_crashed("reaper thread panicked");
        }
    }

    /// The process is gone. Record its code, then run the one teardown `kill` runs too: close the console
    /// on a helper so the pending read returns, wait for the reader's last bytes, publish.
    fn on_process_exit(&self) {
        let Some(resources) = self.resources() else {
            return;
        };
        let mut code = 0u32;
        // SAFETY: the process handle is owned by the session and open for query.
        let recorded = match unsafe { GetExitCodeProcess(resources.process.get(), &mut code) } {
            Ok(()) if code as i32 != STILL_ACTIVE => {
                self.set_exit_code(code as i32);
                Some(code as i32)
            }
            Ok(()) => {
                // The wait was signalled, so the process is gone; a `STILL_ACTIVE` here would make a
                // silent return a session that never publishes (nit: the two silent reaper returns).
                self.log(&format!(
                    "session {}: the reaper's wait was signalled but GetExitCodeProcess still reports \
                     STILL_ACTIVE; no exit code was recorded",
                    self.id
                ));
                None
            }
            Err(error) => {
                // Leave `exit_code` as None: a fake 0 would archive a crash as a clean exit.
                self.log(&format!(
                    "session {}: GetExitCodeProcess failed after the process exited: {}",
                    self.id,
                    error.message()
                ));
                None
            }
        };
        self.finish_exit(CONSOLE_CLOSE_DEADLINE);
        // `log` reaches an arbitrary Python callback, so it runs after the record went out and cannot
        // delay the death (nit 3).
        if let Some(code) = recorded {
            self.log(&format!("session {}: exited with code {code}", self.id));
        }
    }

    /// The one exit teardown, shared by `kill` and the reaper (B1): close the console, wait for the
    /// reader's end of output inside `budget`, then publish the death; on the deadline publish anyway, so
    /// a wedged close costs the last bytes, never the record (§12.7).
    fn finish_exit(&self, budget: Duration) {
        if self.resources().is_none() {
            // A text-only session has no console and no reader: the record is the whole teardown.
            self.publish_death();
            return;
        }
        let end = Instant::now() + budget;
        // `close_console` already logged the reason when it could not even start or wait for a close.
        let closed = self.close_console(end.saturating_duration_since(Instant::now()));
        if !closed {
            self.log(&format!(
                "session {}: publishing the death on the console-close deadline ({budget:?})",
                self.id
            ));
            self.publish_death();
            return;
        }
        if !self.wait_output_ended(end) {
            self.log(&format!(
                "session {}: the reader did not file its last bytes within {:?}; publishing on the \
                 deadline",
                self.id, budget
            ));
        }
        self.publish_death();
    }

    /// Blocks up to `end` for the reader to file its last byte. Polling is enough: the flag is set once,
    /// and the wait is bounded by the exit budget.
    fn wait_output_ended(&self, end: Instant) -> bool {
        while !self.output_ended.load(Ordering::Acquire) {
            if Instant::now() >= end {
                return false;
            }
            thread::sleep(Duration::from_millis(1));
        }
        true
    }

    /// The pipeline for one input (§5.2's order, `_on_pty_output`'s effect): decode, W8, OSC, ring,
    /// parse, then the coalescing batch. The reader passes no deadline — it must never drop a byte —
    /// while `feed` uses the call's deadline. Returns the new screen revision, or `None` when the deadline
    /// passed with nothing done.
    fn ingest(&self, input: Ingest<'_>, deadline: Option<Duration>) -> Option<u64> {
        let now = Instant::now();
        let mut guard = match deadline {
            None => Some(self.text.lock().unwrap_or_else(|e| e.into_inner())),
            Some(deadline) => lock_deadline(&self.text, deadline),
        };
        let state = guard.as_mut()?;
        let ring_before = state.stream.ring_dropped_bytes();
        let TextState {
            stream,
            batch,
            frame,
            ..
        } = &mut **state;
        frame.clear();
        match input {
            Ingest::Bytes(bytes) => stream.feed_bytes(bytes, frame),
            Ingest::Text(text) => stream.feed_text(text, frame),
        }
        if !frame.is_empty() {
            batch.push(frame, now);
        }
        // §5.4's first trigger is the batch's own size: a 32 KiB frame does not wait for the 5 ms tick.
        let ready = if batch.due(now) { batch.take_all() } else { None };
        let rev = stream.screen_rev();
        let processed = frame.len() as u64;
        let ring_dropped = stream.ring_dropped_bytes();
        drop(guard);
        if let Some(frame) = ready {
            self.deliver(&frame);
        }
        self.dropped_bytes.fetch_add(
            ring_dropped.saturating_sub(ring_before),
            Ordering::Relaxed,
        );
        self.screen_rev.store(rev, Ordering::Relaxed);
        self.bytes_total.fetch_add(processed, Ordering::Relaxed);
        self.chunks_total.fetch_add(1, Ordering::Relaxed);
        self.last_output_ms.store(now_unix_ms(), Ordering::Relaxed);
        self.dirty.store(true, Ordering::Release);
        self.tick.pending.store(true, Ordering::Release);
        Some(rev)
    }

    /// The output pipe stopped producing. Either the exit/kill teardown closed the console (then the
    /// exit code is already recorded) or the child closed its own side of the pipe.
    fn on_output_closed(&self) {
        if self.exit_code().is_some() || self.crashed() {
            self.publish_death();
            return;
        }
        let Some(resources) = self.resources() else {
            self.publish_death();
            return;
        };
        let mut code = 0u32;
        // SAFETY: the process handle is owned by the session and open for query.
        match unsafe { GetExitCodeProcess(resources.process.get(), &mut code) } {
            Ok(()) if code as i32 != STILL_ACTIVE => {
                self.set_exit_code(code as i32);
                self.log(&format!(
                    "session {}: exited with code {}",
                    self.id, code as i32
                ));
                self.publish_death();
            }
            _ => {
                // Console closed but the process is alive: the reaper reports the eventual exit. The
                // session stays alive, which is what §5.3's `on_output_closed` case is for.
                self.log(&format!(
                    "session {}: output closed while pid {} is still running",
                    self.id, self.pid
                ));
            }
        }
    }

    /// Records the exit code; the first caller wins, so `kill`, the reaper and the reader all observe the
    /// same code. The death record itself is published separately (see `publish_death`).
    ///
    /// A code learned *after* the death record went out — a crashed session the hub already drained, then
    /// `kill`, or a late `GetExitCodeProcess` in `on_output_closed` — must reach the hub, whose only
    /// exit-code source is the drain record (§5.3): the winner marks the session dirty and pending so the
    /// next drain carries it (SF3).
    fn set_exit_code(&self, code: i32) {
        if self
            .exit_code
            .compare_exchange(-1, code, Ordering::AcqRel, Ordering::Relaxed)
            .is_ok()
        {
            self.dirty.store(true, Ordering::Release);
            self.tick.pending.store(true, Ordering::Release);
        }
    }

    /// Publishes the death record: `alive = false`, one dirty record and an urgent wakeup (§5.3). Called
    /// after the reader's last bytes (or when the console-close deadline passes), never before.
    fn publish_death(&self) {
        if self.alive.swap(false, Ordering::AcqRel) {
            self.dirty.store(true, Ordering::Release);
            self.tick.urgent.store(true, Ordering::Release);
            // The console's last bytes go out before the death record: a pane that sees the end must
            // already have everything the process printed (§5.3).
            self.flush_frames_all();
            // Nothing can reach a dead process: closing the queue ends its writer thread, so the
            // session's last `Arc` is released instead of being held by a thread parked on a condvar
            // (B1: the replaced session must release its handles, not leak a writer). The count is
            // logged: input typed just before the exit used to vanish silently (nit 4).
            let dropped = self.writer.close();
            if dropped > 0 {
                self.log(&format!(
                    "session {}: {} queued input bytes dropped at the exit",
                    self.id, dropped
                ));
            }
        }
    }

    /// Counts a caught panic: `stats.panics` covers reader, writer, reaper, ticker and close helpers.
    fn note_panic(&self) {
        self.panics.fetch_add(1, Ordering::Relaxed);
    }

    /// Marks the session crashed; the thread bodies call it when their own body panics. Always compiled:
    /// a panic must work in a release build.
    fn mark_crashed(&self, reason: &str) {
        self.crashed.store(true, Ordering::Release);
        self.dirty.store(true, Ordering::Release);
        self.tick.urgent.store(true, Ordering::Release);
        self.log(&format!("session {}: crashed ({reason})", self.id));
        // A crashed session has no reader to publish at EOF, so the death is published here.
        self.publish_death();
        // The pseudoconsole is closed on a helper thread, never by the panicking thread's own read path.
        self.close_console(CONSOLE_CLOSE_DEADLINE);
    }

    /// The tests' way into `mark_crashed`: "marked dead, tree still alive" without panicking a real
    /// thread (should-fix 2: `kill` must still terminate the tree). A test hook, not a contract —
    /// compiled out unless the tests ask for it, so it cannot become part of the artifact's surface.
    #[cfg(any(test, feature = "test-hooks"))]
    #[doc(hidden)]
    pub fn mark_crashed_for_test(&self, reason: &str) {
        self.mark_crashed(reason);
    }

    /// Takes the pseudoconsole and closes it on a helper thread, waiting at most `deadline` (§12.7:
    /// closing a pseudoconsole with a pending read is a known hang, so the reader never does it).
    ///
    /// `true` means the console **is** closed — by this call when it took the handle, or by a peer whose
    /// `Closing` slot it waited out (B1). `false` means the close was still pending at the deadline, or
    /// the slot lock itself was busy, or no helper could start; the caller's deadline path publishes
    /// anyway. The slot lock is taken with the same deadline, so no call here can block unbounded (SF1).
    fn close_console(&self, deadline: Duration) -> bool {
        let Some(resources) = self.resources() else {
            // A text-only session has no console: the exit path's "console closed" answer is true by
            // construction, so `finish_exit` publishes without waiting.
            return true;
        };
        let end = Instant::now() + deadline;
        match resources.console.begin_close(deadline) {
            Err(progress) => {
                self.log(&format!(
                    "session {}: console slot busy ({progress:?}); treating the close as pending",
                    self.id
                ));
                false
            }
            Ok(None) => {
                // A peer is closing it (or it is already closed): wait for that completion, which is the
                // difference B1 is about.
                resources
                    .console
                    .wait_closed(end.saturating_duration_since(Instant::now()))
            }
            Ok(Some(console)) => {
                let progress = close_console_off_thread(
                    console,
                    end.saturating_duration_since(Instant::now()),
                    &resources.console,
                );
                match progress {
                    CloseProgress::Closed => true,
                    CloseProgress::Pending => {
                        self.log(&format!(
                            "session {}: pseudoconsole close still pending after {:?}",
                            self.id, deadline
                        ));
                        false
                    }
                    CloseProgress::Busy => false,
                    CloseProgress::NoHelper => {
                        // Closing on this thread would run `ClosePseudoConsole` where a read may be
                        // pending — the reader thread, when the reader is the caller (nit 2). The handle
                        // is leaked and the slot stays `Closing`; every later caller waits out its own
                        // deadline and publishes on it.
                        self.log(&format!(
                            "session {}: no close helper could start; the pseudoconsole handle is leaked \
                             and the reader stays blocked",
                            self.id
                        ));
                        false
                    }
                }
            }
        }
    }

    /// Queues one write as a whole or not at all (SF4, §5.2): empty text returns 0 with nothing queued, a
    /// full queue at the deadline is `Timeout` with nothing queued, so a retry cannot duplicate bytes.
    pub fn write(&self, text: &[u8], deadline: Duration) -> Result<usize, WriteError> {
        self.writer.push(text, deadline)
    }

    /// `ResizePseudoConsole`. The core never propagates a size (that is hub policy); it only applies it.
    ///
    /// The slot lock stays held across the call on purpose: releasing it first would let a concurrent
    /// close take the HPCON out and leave this call resizing a closed value. A resize that stalls inside
    /// the FFI call can therefore hold the slot — and that is harmless, because nothing waits on that
    /// slot without a deadline: `close_console` takes it with the close deadline and reports "busy", the
    /// reaper and `kill` publish on their close deadline, and `kill_all`'s joins are bounded by each
    /// `kill`'s budget (`held_console_slot_does_not_stop_the_death_record`).
    pub fn resize(&self, cols: u16, rows: u16, deadline: Duration) -> Result<(u16, u16), ResizeError> {
        let Some(resources) = self.resources() else {
            // A text-only session has no pseudoconsole; the screen is the whole size story.
            if let Some(mut text) = lock_deadline(&self.text, deadline) {
                text.stream.resize(cols as usize, rows as usize);
            } else {
                return Err(ResizeError::Deadline(format!(
                    "session {}: resize missed its deadline",
                    self.id
                )));
            }
            self.cols.store(cols as u32, Ordering::Relaxed);
            self.rows.store(rows as u32, Ordering::Relaxed);
            self.dirty.store(true, Ordering::Release);
            self.tick.pending.store(true, Ordering::Release);
            return Ok((cols, rows));
        };
        let console = match lock_deadline(&resources.console.state, deadline) {
            Some(state) => state,
            None => {
                return Err(ResizeError::Deadline(format!(
                    "session {}: resize missed its deadline",
                    self.id
                )))
            }
        };
        let console = match &*console {
            ConsoleState::Open(console) => console,
            // A dead session's resize is a refused call, not an internal failure (nit 5, §5.1).
            ConsoleState::Closing | ConsoleState::Closed => {
                return Err(ResizeError::Closed(format!(
                    "session {}: the pseudoconsole is closed, so it cannot be resized",
                    self.id
                )))
            }
        };
        #[cfg(feature = "test-hooks")]
        crate::test_hooks::hold_resize_slot();
        let size = COORD {
            X: cols as i16,
            Y: rows as i16,
        };
        // SAFETY: the HPCON is owned and still open: the slot lock is held, so no close can take it out.
        if let Err(error) = unsafe { ResizePseudoConsole(console.get(), size) } {
            return Err(ResizeError::Failed(format!(
                "ResizePseudoConsole: {}",
                error.message()
            )));
        }
        // `resize` is PTY + screen (§4): the model follows the pseudoconsole so the rows match what the
        // child sees. Lock order is console -> text, and nothing takes them the other way round.
        if let Some(mut text) = lock_deadline(&self.text, deadline) {
            text.stream.resize(cols as usize, rows as usize);
            drop(text);
            // A watched session must see the new geometry even when no byte follows (§5.2).
            self.dirty.store(true, Ordering::Release);
            self.tick.pending.store(true, Ordering::Release);
        }
        self.cols.store(cols as u32, Ordering::Relaxed);
        self.rows.store(rows as u32, Ordering::Relaxed);
        Ok((cols, rows))
    }

    /// Reads and records the exit code when the process has already exited. `true` when a code is known
    /// (already recorded, or read now).
    ///
    /// Read *before* `TerminateJobObject`: a process that has exited but is still terminating can have
    /// its exit code replaced by the job's termination code (1), so a `kill` that arrives in that window
    /// would otherwise record 1 for a child that exited with its own code (measured: `cmd /c "exit 3"`
    /// reports 1 when the job is terminated inside that window).
    fn record_exit_code_if_exited(&self) -> bool {
        if self.exit_code().is_some() {
            return true;
        }
        let Some(resources) = self.resources() else {
            return false;
        };
        let mut code = 0u32;
        // SAFETY: the process handle is owned by the session and open for query.
        match unsafe { GetExitCodeProcess(resources.process.get(), &mut code) } {
            Ok(()) if code as i32 != STILL_ACTIVE => {
                self.set_exit_code(code as i32);
                true
            }
            _ => false,
        }
    }

    /// Terminates the session's tree and waits up to `budget` for the exit, then runs the one exit
    /// teardown (`finish_exit`): the tree is always terminated, even when the session is already marked
    /// dead, because a reader panic leaves the CLI running (should-fix 2) and `TerminateJobObject` is
    /// idempotent. `budget` bounds the whole call; a close helper that outlives it keeps running on its
    /// own thread (should-fix 3), and the death is published on the deadline instead of being lost (B1).
    pub fn kill(&self, budget: Duration) -> Result<Option<i32>, String> {
        let Some(resources) = self.resources() else {
            // A text-only session has no process: ending it is the record, with no exit code.
            self.finish_exit(Duration::ZERO);
            return Ok(self.exit_code());
        };
        let end = Instant::now() + budget;
        // The code of a process that has already exited must be read before the job is terminated, or
        // the termination can overwrite it (see `record_exit_code_if_exited`).
        self.record_exit_code_if_exited();
        let mut method = "TerminateJobObject";
        let result = if resources.job_assigned {
            resources.job.terminate(1)
        } else {
            method = "ToolHelp tree kill";
            job_tree::terminate_tree(self.pid);
            Ok(())
        };
        if let Err(error) = &result {
            self.log(&format!(
                "session {}: {} failed ({}), falling back to the tree walk",
                self.id, method, error
            ));
            job_tree::terminate_tree(self.pid);
        }

        let mut announcement = None;
        if self.exit_code().is_none() {
            // SAFETY: the process handle is owned by the session. The timeout is clamped below
            // `INFINITE`, because `grace_ms` comes from Python and `u32::MAX` would mean "wait forever"
            // (nit 6).
            unsafe {
                WaitForSingleObject(
                    resources.process.get(),
                    wait_timeout(end.saturating_duration_since(Instant::now())),
                );
            }
            let mut code = 0u32;
            // SAFETY: as above; a still-running process reports STILL_ACTIVE and has no code.
            match unsafe { GetExitCodeProcess(resources.process.get(), &mut code) } {
                Ok(()) if code as i32 != STILL_ACTIVE => {
                    self.set_exit_code(code as i32);
                    announcement = Some(format!(
                        "session {}: killed pid {} ({}), exit code {}",
                        self.id,
                        self.pid,
                        method,
                        code as i32
                    ));
                }
                Ok(()) => {}
                Err(error) => self.log(&format!(
                    "session {}: GetExitCodeProcess failed after {}: {}",
                    self.id,
                    method,
                    error.message()
                )),
            }
        }

        if self.exit_code().is_some() {
            // The exit is confirmed: hand the rest of the budget to the teardown `kill` and the reaper
            // share, so the record comes after the reader's last bytes (B1) or on the deadline.
            let remaining = end.saturating_duration_since(Instant::now());
            self.finish_exit(remaining.min(CONSOLE_CLOSE_DEADLINE));
        } else {
            // The process outlived its budget. No close is attempted here: the job was already
            // terminated, so the reaper owns the death and its teardown (§5.3).
            self.log(&format!(
                "session {}: pid {} did not exit within {:?} after {}",
                self.id, self.pid, budget, method
            ));
        }
        if let Some(line) = announcement {
            self.log(&line);
        }
        Ok(self.exit_code())
    }

    /// `screen` (§5.2): the visible rows up to `max_lines`, the cursor, the size and the revision. On a
    /// missed deadline the previous render is returned and the session is marked `degraded`, which is
    /// itself a dirty flag so the hub's next drain sees it without waiting for more output; the next call
    /// that gets its lock clears the flag (nit 7).
    pub fn screen(&self, max_lines: usize, deadline: Duration) -> ScreenReply {
        let rendered = lock_deadline(&self.text, deadline).map(|text| {
            (
                text.stream.screen_text(max_lines),
                text.stream.cursor(),
                text.stream.cols() as u16,
                text.stream.rows() as u16,
                text.stream.screen_rev(),
            )
        });
        match rendered {
            Some((text, (x, y), cols, rows, rev)) => {
                if let Ok(mut cached) = self.cached.lock() {
                    cached.screen = text.clone();
                    cached.screen_rev = rev;
                }
                self.degraded.store(false, Ordering::Release);
                ScreenReply {
                    text,
                    cursor_x: x as u16,
                    cursor_y: y as u16,
                    cols,
                    rows,
                    rev,
                }
            }
            None => {
                self.note_degraded();
                let cached = self.cached.lock().unwrap_or_else(|e| e.into_inner());
                ScreenReply {
                    text: cached.screen.clone(),
                    cursor_x: 0,
                    cursor_y: 0,
                    cols: self.cols.load(Ordering::Relaxed) as u16,
                    rows: self.rows.load(Ordering::Relaxed) as u16,
                    rev: cached.screen_rev,
                }
            }
        }
    }

    /// `scrollback` (§5.2): the history rows above the screen, newest first up to `max_lines`.
    pub fn scrollback(&self, max_lines: usize, deadline: Duration) -> TextReply {
        let rendered = lock_deadline(&self.text, deadline).map(|text| {
            (
                text.stream.scrollback_text(max_lines),
                text.stream.raw_rev(),
            )
        });
        match rendered {
            Some((text, rev)) => {
                if let Ok(mut cached) = self.cached.lock() {
                    cached.scrollback = text.clone();
                }
                self.degraded.store(false, Ordering::Release);
                TextReply { text, rev }
            }
            None => {
                self.note_degraded();
                TextReply {
                    text: self
                        .cached
                        .lock()
                        .map(|cached| cached.scrollback.clone())
                        .unwrap_or_default(),
                    rev: self.cached.lock().map(|cached| cached.raw_rev).unwrap_or(0),
                }
            }
        }
    }

    /// `raw` (§5.2 / `_scrollback_blob`): the raw history trimmed to the 512 KiB cap, with the byte count
    /// and the ring's revision, so the hub's "already written this rev" gate keeps working.
    pub fn raw(&self, deadline: Duration) -> RawReply {
        let rendered = lock_deadline(&self.text, deadline).map(|text| {
            (
                text.stream.raw_text(),
                text.stream.raw_bytes(),
                text.stream.raw_rev(),
            )
        });
        match rendered {
            Some((text, bytes, rev)) => {
                if let Ok(mut cached) = self.cached.lock() {
                    cached.raw = text.clone();
                    cached.raw_rev = rev;
                    cached.raw_bytes = bytes;
                }
                self.degraded.store(false, Ordering::Release);
                RawReply { text, bytes, rev }
            }
            None => {
                self.note_degraded();
                let cached = self.cached.lock().unwrap_or_else(|e| e.into_inner());
                RawReply {
                    text: cached.raw.clone(),
                    bytes: cached.raw_bytes,
                    rev: cached.raw_rev,
                }
            }
        }
    }

    /// `raw_tail` (`raw_ansi_tail`): the last `max_chars` characters of the ring, cut on a character
    /// boundary so a split CJK character or emoji stays whole (step 1 decoded lossily here).
    pub fn raw_tail(&self, max_chars: usize, deadline: Duration) -> String {
        let rendered = lock_deadline(&self.text, deadline).map(|text| text.stream.raw_tail(max_chars));
        match rendered {
            Some(text) => {
                if let Ok(mut cached) = self.cached.lock() {
                    cached.tail = text.clone();
                }
                self.degraded.store(false, Ordering::Release);
                text
            }
            None => {
                self.note_degraded();
                self.cached
                    .lock()
                    .map(|cached| cached.tail.clone())
                    .unwrap_or_default()
            }
        }
    }

    /// What `attach_ws_queue` sends a new console: the joined ring plus one more colour-query strip
    /// (`replay-shape`), under the same lock as the ring append (Q11).
    pub fn replay(&self, deadline: Duration) -> Option<String> {
        lock_deadline(&self.text, deadline).map(|text| text.stream.replay_text())
    }

    /// `feed` (§5.2): text into a session with no process (the DSH virtual sessions). Returns the new
    /// screen revision, or `None` when the deadline passed with nothing done.
    pub fn feed(&self, text: &str, deadline: Duration) -> Option<u64> {
        self.ingest(Ingest::Text(text), Some(deadline))
    }

    /// Registers a sink and hands back the replay that goes with the registration (Q11).
    ///
    /// The pending batch is delivered to the sinks that existed before this one *first*, so a chunk is
    /// never in both the replay and a frame; only then is the snapshot taken and the sink added, all
    /// under the text lock.
    pub fn attach_sink(&self, sink: Arc<dyn FrameSink>) -> (String, u64) {
        let mut text = self.text.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(frame) = text.batch.take_all() {
            self.deliver(&frame);
        }
        let replay = text.stream.replay_text();
        let handle = self.next_sink.fetch_add(1, Ordering::Relaxed);
        drop(text);
        if let Ok(mut sinks) = self.sinks.lock() {
            sinks.push(SinkEntry { handle, sink });
        }
        (replay, handle)
    }

    pub fn detach_sink(&self, handle: u64) -> bool {
        let Ok(mut sinks) = self.sinks.lock() else {
            return false;
        };
        let before = sinks.len();
        sinks.retain(|entry| entry.handle != handle);
        sinks.len() != before
    }

    /// One frame straight to the attached sinks, not through the batch: `spawn(ws_prefix=…)` and
    /// `attach_text(seed=…)` (§5.2, A1). No sink is attached unless a socket already follows the id.
    pub fn push_frame(&self, frame: &str) {
        if !frame.is_empty() {
            self.deliver(frame.as_bytes());
        }
    }

    /// Delivers one coalesced frame to every attached sink; the drops each one reports land in
    /// `dropped_bytes` (§5.4).
    fn deliver(&self, frame: &[u8]) {
        let sinks: Vec<Arc<dyn FrameSink>> = match self.sinks.lock() {
            Ok(sinks) => sinks.iter().map(|entry| Arc::clone(&entry.sink)).collect(),
            Err(error) => error.into_inner().iter().map(|entry| Arc::clone(&entry.sink)).collect(),
        };
        let mut dropped = 0u64;
        for sink in sinks {
            dropped += sink.deliver(&self.id, frame);
        }
        if dropped > 0 {
            self.dropped_bytes.fetch_add(dropped, Ordering::Relaxed);
        }
    }

    /// The ticker's 5 ms pass: flush this session's batch when §5.4's window is over.
    pub fn flush_frames(&self, now: Instant) {
        let frame = {
            let mut text = self.text.lock().unwrap_or_else(|e| e.into_inner());
            text.batch.take_due(now)
        };
        if let Some(frame) = frame {
            self.deliver(&frame);
        }
    }

    /// The session's last bytes before it dies: flush whatever the batch holds, due or not.
    fn flush_frames_all(&self) {
        let frame = {
            let mut text = self.text.lock().unwrap_or_else(|e| e.into_inner());
            text.batch.take_all()
        };
        if let Some(frame) = frame {
            self.deliver(&frame);
        }
    }

    /// `watch_screen`: at most one render per changed revision and per `interval`, carried by `drain`.
    pub fn watch(&self, max_lines: usize, interval: Duration) {
        if let Ok(mut text) = self.text.lock() {
            text.watch = Some(Watch {
                max_lines,
                interval,
                last_sent: None,
                last_rev: 0,
            });
        }
    }

    pub fn unwatch(&self) {
        if let Ok(mut text) = self.text.lock() {
            text.watch = None;
        }
    }

    /// The screen for this drain, when a watch is set, the revision changed and its interval is due.
    fn watched_screen(&self, now: Instant) -> Option<String> {
        let mut text = lock_deadline(&self.text, Duration::from_millis(5))?;
        let TextState {
            stream, watch, ..
        } = &mut *text;
        let watch = watch.as_mut()?;
        let rev = stream.screen_rev();
        if rev == watch.last_rev {
            return None;
        }
        if let Some(last) = watch.last_sent {
            if now.saturating_duration_since(last) < watch.interval {
                return None;
            }
        }
        let screen = stream.screen_text(watch.max_lines);
        watch.last_rev = rev;
        watch.last_sent = Some(now);
        Some(screen)
    }

    /// `snapshot_all` (§5.2/§6.6): what `persist_session` archives, taken under the call's deadline.
    pub fn snapshot(&self, max_lines: usize, max_bytes: usize, deadline: Duration) -> SessionSnapshot {
        let mut snapshot = SessionSnapshot {
            session_id: self.id.clone(),
            alive: self.alive(),
            exit_code: self.exit_code(),
            pid: self.pid,
            crashed: self.crashed(),
            degraded: self.degraded.load(Ordering::Acquire),
            screen_text: String::new(),
            scrollback_text: String::new(),
            raw_text: String::new(),
            raw_rev: 0,
            raw_bytes: 0,
        };
        if let Some(text) = lock_deadline(&self.text, deadline) {
            snapshot.screen_text = text.stream.screen_text(max_lines);
            snapshot.scrollback_text = text.stream.scrollback_text(max_lines);
            snapshot.raw_text = trim_history_text(text.stream.raw_join(), max_bytes);
            snapshot.raw_rev = text.stream.raw_rev();
            snapshot.raw_bytes = text.stream.raw_bytes();
        } else {
            snapshot.degraded = true;
        }
        snapshot
    }

    /// Marks the session `degraded` for a missed deadline: the flag is itself dirty, so the hub's next
    /// drain reports it without waiting for more output (nit 7).
    fn note_degraded(&self) {
        self.degraded.store(true, Ordering::Release);
        self.dirty.store(true, Ordering::Release);
        self.tick.pending.store(true, Ordering::Release);
    }

    /// The dirty session's counters, or `None` when nothing changed since the last drain. Reading only
    /// atomics is what makes the 50 ms drain cheap (§5.3); a watched session adds one render.
    pub fn drain_record(&self) -> Option<DrainRecord> {
        if !self.dirty.swap(false, Ordering::AcqRel) {
            return None;
        }
        let last_ms = self.last_output_ms.load(Ordering::Relaxed);
        Some(DrainRecord {
            session_id: self.id.clone(),
            alive: self.alive.load(Ordering::Acquire),
            exit_code: self.exit_code(),
            pid: self.pid,
            bytes_total: self.bytes_total.load(Ordering::Relaxed),
            chunks_total: self.chunks_total.load(Ordering::Relaxed),
            last_output_at: if last_ms == 0 {
                None
            } else {
                Some(last_ms as f64 / 1000.0)
            },
            dropped_bytes: self.dropped_bytes.load(Ordering::Relaxed),
            crashed: self.crashed.load(Ordering::Acquire),
            degraded: self.degraded.load(Ordering::Acquire),
            screen_rev: self.screen_rev.load(Ordering::Relaxed),
            screen_text: self.watched_screen(Instant::now()),
        })
    }

    pub fn alive(&self) -> bool {
        self.alive.load(Ordering::Acquire)
    }

    pub fn crashed(&self) -> bool {
        self.crashed.load(Ordering::Acquire)
    }

    pub fn exit_code(&self) -> Option<i32> {
        match self.exit_code.load(Ordering::Acquire) {
            -1 => None,
            code => Some(code),
        }
    }

    /// `(bytes, chunks, dropped)` without touching the dirty flag, so `stats` cannot steal a record
    /// from the next `drain` (§5.3).
    pub fn counters(&self) -> (u64, u64, u64) {
        (
            self.bytes_total.load(Ordering::Relaxed),
            self.chunks_total.load(Ordering::Relaxed),
            self.dropped_bytes.load(Ordering::Relaxed),
        )
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        // Unblocks the writer thread and lets it exit; the pipes close with `Resources`. No `log` call
        // here: this can run on the Python GC thread during interpreter teardown.
        self.writer.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn write_queues_the_whole_text_or_nothing() {
        let queue = WriteQueue::new(16);
        assert_eq!(queue.push(b"", Duration::from_millis(5)), Ok(0));
        assert_eq!(queue.push(b"abc", Duration::from_millis(5)), Ok(3));
        assert_eq!(queue.pending(), 3);
        // A text bigger than the cap while the queue holds bytes is refused as a whole, not split.
        assert_eq!(
            queue.push(&[b'x'; 32], Duration::from_millis(20)),
            Err(WriteError::Timeout)
        );
        assert_eq!(queue.pending(), 3, "a refused write queues nothing");
        // The writer takes the chunk out, so the same text now fits.
        let popped = queue.pop().expect("one chunk");
        assert_eq!(popped, b"abc");
        queue.complete(popped.len());
        assert_eq!(queue.push(&[b'y'; 16], Duration::from_millis(5)), Ok(16));
    }

    /// SF2: the cap bounds what waits in the queue, not what one call may hand it — a single call larger
    /// than the cap is accepted whole when nothing is queued (the hub sends one frame per call, and a
    /// paste larger than the cap used to be a permanent `Timeout`). A second one, while the first is
    /// still queued, times out with nothing queued.
    #[test]
    fn an_oversize_write_is_accepted_on_an_empty_queue() {
        let queue = WriteQueue::new(16);
        let oversize = vec![b'z'; 2 * 1024 * 1024];
        assert_eq!(
            queue.push(&oversize, Duration::from_millis(5)),
            Ok(oversize.len())
        );
        assert_eq!(queue.pending(), oversize.len());
        // The writer has taken the chunk but not completed it (WriteFile is blocked): still not empty.
        let popped = queue.pop().expect("the queued chunk");
        assert_eq!(popped.len(), oversize.len());
        let started = Instant::now();
        assert_eq!(
            queue.push(&oversize, Duration::from_millis(30)),
            Err(WriteError::Timeout)
        );
        assert!(
            started.elapsed() >= Duration::from_millis(20),
            "the oversized push returned before its deadline"
        );
        // Once the chunk is in the pipe, the next oversized call fits again.
        queue.complete(popped.len());
        assert_eq!(queue.pending(), 0);
        assert_eq!(
            queue.push(&oversize, Duration::from_millis(5)),
            Ok(oversize.len())
        );
    }

    #[test]
    fn write_to_a_full_queue_times_out_and_closed_refuses() {
        let queue = WriteQueue::new(8);
        assert_eq!(queue.push(b"12345678", Duration::from_millis(5)), Ok(8));
        let started = Instant::now();
        assert_eq!(
            queue.push(b"x", Duration::from_millis(40)),
            Err(WriteError::Timeout)
        );
        assert!(
            started.elapsed() >= Duration::from_millis(30),
            "the push returned before its deadline"
        );
        assert_eq!(queue.pending(), 8);
        assert_eq!(queue.close(), 8);
        assert_eq!(
            queue.push(b"x", Duration::from_millis(5)),
            Err(WriteError::Closed)
        );
        assert!(queue.pop().is_none());
    }
}