//! One console session: its pipes, its job, a reader thread, a writer thread and a reaper thread.
//!
//! The reader runs step 2's pipeline on every read (`terminal_stream`): decode, W8/OSC strips, the W9
//! ring, the parse. One mutex (`Session::text`) covers the ring append, the parse and the coalescing
//! batch, which is what makes Q11 hold: a sink that attaches sees every chunk exactly once, either in
//! the replay snapshot it is handed or in the frames that follow (the pending batch is flushed to the
//! sockets that existed before it registers).
//!
//! A text-only session (`attach_text`) has no pipes, no job and no threads: the same state, fed by
//! `feed` instead of a reader, which is what the corpus and the core's tests use.
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
use windows::Win32::System::Threading::{
    GetExitCodeProcess, OpenProcess, TerminateProcess, WaitForSingleObject, INFINITE,
    PROCESS_TERMINATE,
};

use terminal_stream::{
    trim_history_text, Coalescer, FrameSink, StreamOptions, TerminalStream,
};

use crate::conpty::ConPtyProcess;
use crate::handles::{OwnedHandle, OwnedPseudoConsole};
use crate::job::{JobObject, JobPolicy};
use crate::job_tree;
use crate::sink_fanout::{Delivery, SinkFanout};

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
    /// What the session's job owns (`job.rs`): the whole tree (the default, and the behaviour every
    /// session had before) or the client alone. A client that starts a machine-wide singleton — a shared
    /// `--type=service` process other sessions also use — needs `ClientOnly`, or closing this session
    /// terminates that singleton and every other session dies with it.
    pub job_policy: JobPolicy,
}

impl Default for SessionFlags {
    fn default() -> Self {
        Self {
            strip_esc3j_on_output: false,
            strip_osc_color_queries: true,
            history_rows: 3000,
            raw_cap_bytes: 512 * 1024,
            raw_slack_bytes: 64 * 1024,
            job_policy: JobPolicy::default(),
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

/// A size the console asked for through its socket (§6.4 "Input"). The core never applies one: it
/// records the request and the hub applies its 20x5 clamp and its single-window propagation, which is
/// policy and stays in Python (§8).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ResizeRequest {
    pub cols: u16,
    pub rows: u16,
    /// The console's `propagate` flag: `state.layoutMode === 'single'`.
    pub propagate: bool,
}

/// How many unapplied requests one session may hold before the oldest leave. The hub drains every
/// 50 ms, so a backlog this deep means it stopped draining; keeping the newest is what a user typing
/// (or a pane being refitted) would expect, and it bounds a socket's memory.
const MAX_PENDING_REQUESTS: usize = 1024;

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
    /// The raw ring's revision (`_scrollback_blob`'s change gate, the seam's `raw_revision`): the same
    /// number `raw`'s reply carries, available without joining the ring.
    pub raw_rev: u64,
    /// Only for a watched session whose `screen_rev` changed and whose interval is due.
    pub screen_text: Option<String>,
    /// The console's unapplied size requests, in arrival order and taken once (§6.4). `propagate`
    /// travels with each one: dropping it would resize every background session in matrix mode. A
    /// backlog over the cap has had its oldest removed.
    pub resize_requests: Vec<ResizeRequest>,
    /// Per console socket of this id, its sink handle and the cumulative bytes it dropped (§5.4, §9
    /// step 4). A slow pane's loss stays its own and is never added to `dropped_bytes`; only handles
    /// with a drop are listed, and a socket that detached since the last drain reports its final count
    /// once. Handles are unique per session id, so the pair (record's `session_id`, handle) identifies a
    /// socket across the whole hub.
    pub socket_drops: Vec<(u64, u64)>,
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

/// Terminates the one process a `ClientOnly` session owns: the client itself, through its handle, and
/// through a reopen by pid when that handle cannot terminate it.
///
/// This is deliberately *not* `job_tree::terminate_tree`: the walk selects descendants by parent pid
/// whatever their job membership is, so it would reach a child the client created outside the job — the
/// machine-wide service singleton the whole `ClientOnly` policy exists to protect.
fn terminate_client(process: &OwnedHandle, pid: u32) -> std::io::Result<()> {
    // SAFETY: the process handle is owned by the session and was created with PROCESS_TERMINATE.
    if unsafe { TerminateProcess(process.get(), 1) }.is_ok() {
        return Ok(());
    }
    // SAFETY: no inheritance; the returned handle is owned by the wrapper for the duration of the call.
    let reopened = unsafe { OpenProcess(PROCESS_TERMINATE, false, pid) };
    match reopened {
        Ok(handle) => {
            let handle = OwnedHandle::new(handle);
            // SAFETY: the handle was opened with PROCESS_TERMINATE and is closed by the wrapper.
            unsafe { TerminateProcess(handle.get(), 1) }
                .map_err(|error| std::io::Error::other(error.message()))
        }
        Err(error) => Err(std::io::Error::other(format!(
            "TerminateProcess failed and OpenProcess({pid}) failed: {}",
            error.message()
        ))),
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
    /// Frames reach the sinks one at a time, in stream order. This belongs to the session **id**, not to
    /// this incarnation (§6.4 A1): a resume hands this session's predecessor's structure over, so an
    /// attached socket keeps its registration and its handle, and the resume banner reaches it.
    ///
    /// Lock order is one-directional: `delivery` before `text`, and `sinks` only under `delivery`.
    /// Nothing takes `delivery` while holding `text`, `sinks` or `cached`. A frame is taken out of the
    /// batch and fanned out inside that lock, so the reader (`ingest`) and the ticker (`flush_frames`)
    /// cannot deliver frame n+1 before frame n, and `attach_sink`'s snapshot and registration sit in one
    /// gap between deliveries. `FrameSink::deliver` must not block, so holding it costs the reader no
    /// more than the sink's own queue lock would.
    fanout: Arc<SinkFanout>,
    /// Set by the successor's handover: whatever this incarnation would still deliver (a reader that
    /// outlived its exit record, i.e. B1's wedged close) is dropped and counted instead of reaching the
    /// sockets after the resume banner (§6.4 A1).
    superseded: AtomicBool,
    /// `true` once this incarnation has a reader that can deliver; `push_frame`'s precondition (§5.4).
    reader_started: AtomicBool,
    /// `true` once the superseded drop was logged, so a wedged reader logs it once, not per frame.
    superseded_drop_logged: AtomicBool,
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
    /// The ring's `raw_rev`, mirrored for the same reason: the seam's `raw_revision` must not join the
    /// ring (`_scrollback_blob`'s change gate).
    raw_rev: AtomicU64,
    /// `true` once `publish_death` has delivered the session's last batch. A drain never reports the
    /// death before it: the record that says "ended" must not precede the last byte (§5.3).
    death_flushed: AtomicBool,
    /// `true` once a drain reported this incarnation's death record. `retire` refuses before that, so
    /// the exit record (code, `crashed`) cannot be dropped with the session.
    death_reported: AtomicBool,
    /// `true` when an exit code was learned after the death record went out and no drain has carried it
    /// yet (SF3): `retire` waits for it the same way.
    code_pending: AtomicBool,
    dropped_bytes: AtomicU64,
    last_output_ms: AtomicU64,
    dirty: AtomicBool,
    crashed: AtomicBool,
    degraded: AtomicBool,
    cols: AtomicU32,
    rows: AtomicU32,
    tick: Arc<TickFlags>,
    /// The console's unapplied size requests (§6.4), set by a socket in arrival order, taken by the
    /// drain record. Ordered rather than latest-wins: the hub must apply each one in turn, because
    /// `propagate` is part of the request and an earlier single-window resize still moves the other
    /// sessions' screens even if a later request only resizes this one.
    resize_requests: Mutex<Vec<ResizeRequest>>,
    /// `true` once the input backlog overflowed, so the loss is logged once per incarnation, not per
    /// keystroke.
    request_backlog_logged: AtomicBool,
    panics: Arc<AtomicU64>,
    log: Arc<dyn Fn(&str) + Send + Sync>,
}

impl Session {
    /// The state both constructors share. `seed_history` is a resumed session's archived scrollback:
    /// history, not output, so the byte counters stay at zero until the new process prints (A1).
    /// `previous` is the incarnation this one resumes for the same id: its sink fan-out is carried over
    /// (sinks and handles belong to the id, §6.4 A1), and it is the old session `take_over` retires.
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
        previous: Option<&Arc<Session>>,
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
        // The mirrors start at the stream's own revisions, not at 0: a seeded attach's ring is already
        // at 1, and every record that reports a revision must report the number `raw()`/`screen`
        // carried (the drain record's `raw_rev` gates `_scrollback_blob`).
        let screen_rev = stream.screen_rev();
        let raw_rev = stream.raw_rev();
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
            // A predecessor whose fan-out is already closed was retired while this incarnation was
            // being built (`Core::retire` closes it): its sinks are gone, so adopting it would drop
            // every frame this session prints and no socket would ever be told `closed`. A spawn of a
            // retired id always starts on a fresh fan-out.
            fanout: match previous {
                Some(previous) if !previous.fanout.is_closed() => Arc::clone(&previous.fanout),
                _ => Arc::new(SinkFanout::default()),
            },
            superseded: AtomicBool::new(false),
            reader_started: AtomicBool::new(false),
            superseded_drop_logged: AtomicBool::new(false),
            writer: WriteQueue::new(options.write_queue_bytes),
            alive: AtomicBool::new(true),
            exit_code: AtomicI32::new(-1),
            output_ended: AtomicBool::new(false),
            bytes_total: AtomicU64::new(0),
            screen_rev: AtomicU64::new(screen_rev),
            raw_rev: AtomicU64::new(raw_rev),
            death_flushed: AtomicBool::new(false),
            death_reported: AtomicBool::new(false),
            code_pending: AtomicBool::new(false),
            chunks_total: AtomicU64::new(0),
            dropped_bytes: AtomicU64::new(0),
            last_output_ms: AtomicU64::new(0),
            dirty: AtomicBool::new(false),
            crashed: AtomicBool::new(false),
            degraded: AtomicBool::new(false),
            cols: AtomicU32::new(cols as u32),
            rows: AtomicU32::new(rows as u32),
            tick,
            resize_requests: Mutex::new(Vec::new()),
            request_backlog_logged: AtomicBool::new(false),
            panics,
            log,
        })
    }

    /// Wraps a spawned process and starts its reader, writer and reaper threads. The returned lines are
    /// the spawn's log lines: the caller logs them after it re-attaches, because this runs with the GIL
    /// released (§5.2: no Python callback inside the detached spawn path).
    ///
    /// `previous` is the dead session currently in the table for this id, if any: its sinks are carried
    /// over and its pending batch is delivered before `ws_prefix` goes out (§6.4 A1).
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
        previous: Option<Arc<Session>>,
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
            // What a refused assignment means for the cleanup depends on the policy: the tree walk is
            // only ever a `KillTree` fallback (`terminate_processes`).
            let fallback = match job.policy() {
                JobPolicy::ClientOnly => "kill will terminate the client process only",
                JobPolicy::KillTree => "kill will walk the tree with ToolHelp",
            };
            spawn_log.push(format!(
                "session {id}: job assignment failed, {fallback}: {error}"
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
            previous.as_ref(),
        );

        // A1's handover, before the reader exists: the old incarnation's pending bytes, then the resume
        // separator, both to the sinks that are attached to the **id** (carried over by `new_state`).
        // This is why the separator reaches a socket at all, and why it cannot outrun the old process's
        // last frame — one delivery lock covers both steps, and a sink that attaches during the swap
        // lands on either side of them, never between.
        session.take_over(previous.as_ref(), ws_prefix.as_deref());

        // Thread starts are the last step that can fail, and a failure here used to leave a live process
        // with no table entry: unreachable, unreaped (rereview item 3). The error path ends the tree and
        // closes the console, then reports the failure so `core.spawn` raises instead of hiding it.
        // Past this point the incarnation is meant to have a reader, so `push_frame` (a prefix or a
        // seed) is no longer legal for it (§5.4's precondition); set before the first thread so there is
        // no window in which a reader exists and the flag does not.
        session.reader_started.store(true, Ordering::Release);
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
            // The handover already flushed the predecessor's batch and sent the banner, but the
            // predecessor is still the session the table names and nobody else will resume it: letting
            // it deliver again keeps its sockets working instead of dropping everything its reader
            // still has after a failed spawn (edge case: this is the only failure after the handover).
            if let Some(previous) = previous.as_ref() {
                if previous.superseded.swap(false, Ordering::AcqRel) {
                    previous.log(&format!(
                        "session {}: the successor could not start; this incarnation keeps delivering",
                        session.id
                    ));
                }
            }
            let resources = session.resources.clone().expect("a spawned session has resources");
            // The same policy-aware termination `kill` runs: under `ClientOnly` a failed thread start
            // must not walk the tree either, or it would take the client's shared children with it.
            let (_method, terminated) = session.terminate_processes(&resources);
            // Close on a helper so a reader that did start leaves its `ReadFile`; no session-level wait,
            // because the node is not in the table and nobody drains it.
            let _ = session.close_console(CONSOLE_CLOSE_DEADLINE);
            return Err(format!(
                "session {}: could not start its threads ({error}); the session's processes were terminated{}",
                session.id,
                match terminated {
                    Ok(()) => String::new(),
                    Err(error) => format!(" (job terminate failed: {error})"),
                }
            ));
        }

        Ok((session, spawn_log))
    }

    /// A session with no process (`attach_text`, §5.2): a text session's screen and raw history,
    /// fed through `feed`, alive for as long as the hub keeps it. `kill`/`resize` never touch a console
    /// that does not exist.
    ///
    /// `seed` is the resume's separator (§6.4 A1) and goes out under the same handover `spawn`'s
    /// `ws_prefix` uses, so it follows the predecessor's pending batch to the same carried-over sinks.
    pub fn start_text(
        id: String,
        cols: u16,
        rows: u16,
        options: SessionOptions,
        tick: Arc<TickFlags>,
        panics: Arc<AtomicU64>,
        log: Arc<dyn Fn(&str) + Send + Sync>,
        seed_history: Option<&str>,
        seed: Option<&str>,
        previous: Option<Arc<Session>>,
    ) -> Arc<Self> {
        let session = Self::new_state(
            id,
            0,
            None,
            cols,
            rows,
            options,
            tick,
            panics,
            log,
            seed_history,
            previous.as_ref(),
        );
        session.take_over(previous.as_ref(), seed);
        session
    }

    /// A1's handover from the previous incarnation of this id, run once before this incarnation can
    /// deliver anything (§6.4 A1). Under the id's delivery lock: the predecessor's pending batch goes to
    /// the shared sinks, then the predecessor is superseded, then the separator (`ws_prefix` for a
    /// spawn, the seed for `attach_text`) goes out. A sink attached to the id therefore sees the old
    /// incarnation's last bytes, the separator, and the new incarnation's output, each exactly once.
    ///
    /// The delivery lock is what makes a sink attached during the swap safe: `attach_sink` either takes
    /// its replay and registers before this begins (and gets the separator), or after it ends (and its
    /// replay already holds the old bytes and the separator).
    fn take_over(self: &Arc<Self>, previous: Option<&Arc<Session>>, separator: Option<&str>) {
        let (delivery, retired) = {
            let _delivery = self.fanout.lock_delivery();
            let retired = match previous {
                Some(previous) => Some((previous, previous.retire())),
                None => None,
            };
            // From here on this incarnation owns the id: a sink that attaches (through either object)
            // takes its replay from this ring, so the successor's already delivered frames cannot fall
            // between an old ring's end and the registration.
            self.fanout.set_owner(Arc::downgrade(self));
            let delivery = match separator {
                Some(separator) => self.push_frame(separator),
                None => Delivery::default(),
            };
            (delivery, retired)
        };
        // Reporting logs, and a log call reaches Python: it happens with no core lock held (§6.5).
        if let Some((previous, (flushed, first))) = retired {
            previous.report_delivery(flushed);
            if first {
                previous.log(&format!(
                    "session {}: superseded by a new incarnation for this id; its later output is \
                     dropped, not delivered",
                    previous.id
                ));
            }
        }
        self.report_delivery(delivery);
    }

    /// Retires this incarnation under the id's delivery lock, as its successor's handover requires: its
    /// pending batch goes to the shared sinks first (so the old process's last bytes reach the sockets
    /// before the resume banner), then it is marked superseded, so anything its reader still files is
    /// dropped and counted instead of landing after the banner.
    ///
    /// Returns the flush's report and whether this call is the one that retired it, so the caller logs
    /// the retirement once, outside the lock.
    fn retire(&self) -> (Delivery, bool) {
        let flushed = self.pump_locked(None);
        let first = !self.superseded.swap(true, Ordering::AcqRel);
        (flushed, first)
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
        let rev = stream.screen_rev();
        let raw_rev = stream.raw_rev();
        // The mirrors are stored here, under the text lock, not after `pump`: `pump` waits for the
        // id's delivery lock, and a drain taken in that window would otherwise report the *previous*
        // revision while `raw()` already carries the new one (`step3b-raw-rev`: the record's `raw_rev`
        // is the same number `raw` reports). `fetch_max` because a revision is monotonic: two
        // concurrent ingests can only ever move it forward.
        self.screen_rev.fetch_max(rev, Ordering::Relaxed);
        self.raw_rev.fetch_max(raw_rev, Ordering::Relaxed);
        let processed = frame.len() as u64;
        let ring_dropped = stream.ring_dropped_bytes();
        drop(guard);
        // §5.4's first trigger is the batch's own size: a 32 KiB frame does not wait for the 5 ms tick.
        // The frame is taken and fanned out inside `pump`, i.e. under the delivery lock: taking it here
        // would let two readers hand frames to the sinks in the wrong order.
        self.pump(Some(now));
        self.dropped_bytes.fetch_add(
            ring_dropped.saturating_sub(ring_before),
            Ordering::Relaxed,
        );
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
            // Unconditional: the record that carries the code clears this again, and a code is *pending*
            // until one does. Testing `death_reported` here instead lost a code learned inside a drain's
            // own window — the drain had already read the code as absent, and nothing told `retire` to
            // wait (§5.3 SF3, review F3).
            self.code_pending.store(true, Ordering::Release);
            self.dirty.store(true, Ordering::Release);
            self.tick.pending.store(true, Ordering::Release);
        }
    }

    /// Publishes the death record: `alive = false`, one dirty record and an urgent wakeup (§5.3). Called
    /// after the reader's last bytes (or when the console-close deadline passes), never before.
    ///
    /// The last batch is delivered **inside** the `alive` gate and `death_flushed` follows it, so no drain
    /// can report the death before that delivery finished: the record that says "ended" never precedes
    /// the session's last byte (§5.3). Every caller flushes — `kill` and the reaper may publish after the
    /// reader published, and the bytes they learned about last belong to the same console.
    fn publish_death(&self) {
        let first = self.alive.swap(false, Ordering::AcqRel);
        if first {
            // `alive = false` is visible before this flush ends, so `drain_record` skips the record
            // until `death_flushed` says the delivery finished: the next tick carries the death.
            self.flush_frames_all();
            self.death_flushed.store(true, Ordering::Release);
            self.dirty.store(true, Ordering::Release);
            self.tick.urgent.store(true, Ordering::Release);
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
        } else {
            // A concurrent publish owns the record; whatever this caller learned goes out too.
            self.flush_frames_all();
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

    /// The console's size request, recorded for the hub instead of applied (§6.4 "Input"): `resize` is
    /// policy (the 20x5 clamp and the single-window propagation), so the core only carries the request
    /// on the drain record. Requests keep their arrival order — an earlier one whose `propagate` was set
    /// still moves the other sessions' screens, so the hub has to see both — and the flag dirties the
    /// session so the record goes out even when no byte follows.
    pub fn request_resize(&self, cols: u16, rows: u16, propagate: bool) {
        if cols == 0 || rows == 0 {
            // A zero size is not a size: the console's `fit()` never sends one, and the hub's clamp
            // would refuse it anyway.
            return;
        }
        self.push_bounded(
            &self.resize_requests,
            ResizeRequest {
                cols,
                rows,
                propagate,
            },
        );
        self.dirty.store(true, Ordering::Release);
        self.tick.pending.store(true, Ordering::Release);
    }

    /// Whether this session has no PTY at all: console input has nowhere to go and is dropped, since
    /// no hub session is a text one (only the corpus and the core's tests attach text sessions).
    pub(crate) fn is_text_only(&self) -> bool {
        self.resources.is_none()
    }

    /// Appends to an ordered backlog, dropping the oldest past the cap. The hub drains every 50 ms, so
    /// reaching the cap means it stopped draining; keeping the newest is what a user typing expects,
    /// and the loss is logged once per session rather than silently every time.
    fn push_bounded<T>(&self, backlog: &Mutex<Vec<T>>, item: T) {
        let mut dropped = false;
        {
            let mut requests = backlog.lock().unwrap_or_else(|e| e.into_inner());
            if requests.len() >= MAX_PENDING_REQUESTS {
                requests.remove(0);
                dropped = true;
            }
            requests.push(item);
        }
        // §6.5: the Python log runs with no core lock held, so a callback that drains or resizes cannot
        // deadlock the socket thread that hit the cap.
        if dropped && !self.request_backlog_logged.swap(true, Ordering::AcqRel) {
            self.log(&format!(
                "session {}: more than {} console requests waiting for a drain; the oldest were dropped",
                self.id, MAX_PENDING_REQUESTS
            ));
        }
    }

    /// Takes the pending resize requests. Called by `drain_record` only once it is sure the record
    /// goes out, so a request is never dropped with a record that was withheld.
    fn take_requests(&self) -> Vec<ResizeRequest> {
        std::mem::take(&mut *self.resize_requests.lock().unwrap_or_else(|e| e.into_inner()))
    }

    /// A console socket dropped bytes for itself (§5.4): the drain record reports the count per handle,
    /// so a drop with no output after it still reaches the hub.
    pub(crate) fn note_socket_drop(&self) {
        self.dirty.store(true, Ordering::Release);
        self.tick.pending.store(true, Ordering::Release);
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

    /// Ends this incarnation's processes according to the job's policy, returning the method name for the
    /// log and the call's result.
    ///
    /// `KillTree` keeps the original two-step: `TerminateJobObject` when the client is in the job, the
    /// ToolHelp walk when the assignment was refused, and the walk again when the job call fails.
    ///
    /// `ClientOnly` never walks the tree. Its descendants are outside the job by design
    /// (`JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK` in `job.rs`), and `job_tree::terminate_tree` selects by
    /// parent pid regardless of job membership, so the walk would still terminate the client's children —
    /// the shared `doubao.exe --type=service` among them, which is the crash this policy removes. The
    /// client is terminated through its own handle (or a reopen by pid) instead, and that is the only
    /// fallback: not even "`job_assigned == false`" or "`TerminateJobObject` failed" is a reason to walk.
    fn terminate_processes(&self, resources: &Resources) -> (&'static str, std::io::Result<()>) {
        if resources.job.policy() == JobPolicy::ClientOnly {
            if resources.job_assigned {
                // Only the client is in the job (its children broke away when they were created), so
                // terminating the job cannot reach a grandchild.
                match resources.job.terminate(1) {
                    Ok(()) => return ("TerminateJobObject", Ok(())),
                    Err(error) => self.log(&format!(
                        "session {}: TerminateJobObject failed ({}), terminating the client process \
                         only (the job's children break away)",
                        self.id, error
                    )),
                }
            }
            return (
                "client-only TerminateProcess",
                terminate_client(&resources.process, self.pid),
            );
        }

        if resources.job_assigned {
            match resources.job.terminate(1) {
                Ok(()) => ("TerminateJobObject", Ok(())),
                Err(error) => {
                    self.log(&format!(
                        "session {}: TerminateJobObject failed ({}), falling back to the tree walk",
                        self.id, error
                    ));
                    job_tree::terminate_tree(self.pid);
                    ("TerminateJobObject", Ok(()))
                }
            }
        } else {
            job_tree::terminate_tree(self.pid);
            ("ToolHelp tree kill", Ok(()))
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
        // The policy lives on the job, so `kill` cannot walk a tree that belongs to someone else by
        // accident: only `KillTree` reaches `job_tree::terminate_tree` (see `terminate_processes`).
        let (method, result) = self.terminate_processes(&resources);
        if let Err(error) = &result {
            // Only a `ClientOnly` session can reach this line: its fallback is the client itself, and a
            // tree walk here is exactly what the policy forbids.
            self.log(&format!(
                "session {}: {} failed ({}), the client process is still running",
                self.id, method, error
            ));
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

    /// `feed` (§5.2): text into a session with no process (a text session's screen). Returns the new
    /// screen revision, or `None` when the deadline passed with nothing done.
    pub fn feed(&self, text: &str, deadline: Duration) -> Option<u64> {
        self.ingest(Ingest::Text(text), Some(deadline))
    }

    /// Registers a sink and hands back the replay that goes with the registration (Q11).
    ///
    /// Atomic against `pump`/`push_frame`: the id's delivery lock is held from the pending batch
    /// through the snapshot and the registration, so no frame can be taken and fanned out in between (a
    /// chunk would otherwise be in neither the replay nor any frame). The pending batch is fanned out to
    /// the sinks that existed before this one only, because the replay already carries those bytes; the
    /// new sink gets them once, from the replay.
    ///
    /// Wire order for the caller: the sink must write the returned replay and the `replay_end` marker
    /// **before** draining the sink's own queue. A direct write from `deliver` puts a frame on the wire
    /// before the replay, because the registration is complete when this returns and frames start landing
    /// immediately (§6.4: replay, one more strip, the marker, then frames). Step 4's socket sink is a
    /// bounded queue drained by the socket's own thread for exactly this reason.
    ///
    /// Never returns a handle it did not register: a sink that panicked earlier leaves a poisoned sink
    /// list behind, and `register` recovers it with `into_inner`.
    /// `(replay, handle)`, where `handle` is `None` when the id was retired before the registration:
    /// a registered sink in a closed fan-out would receive no frame and no `closed`, so nothing is
    /// registered and the caller answers `closed` at once (review F1).
    pub fn attach_sink(&self, sink: Arc<dyn FrameSink>) -> (String, Option<u64>) {
        // The owner `Arc<Session>` taken for the replay is released **after** the delivery lock: once
        // `retire` exists, an attach through a predecessor can hold the last reference to the owner, and
        // dropping it here would run `Session::drop` (pipes, console close helper) under the lock that
        // every delivery needs. So it is parked outside the locked block and dropped below.
        let owner_slot;
        let (replay, handle, delivery) = {
            let _delivery = self.fanout.lock_delivery();
            // The replay, and the pending batch that goes with it, must come from the incarnation whose
            // frames will follow: during a resume that is the successor (set by the handover before the
            // separator), not this session, whose ring stops at the takeover. A retired incarnation
            // refuses to fan out, so handing its batch back through it would lose those bytes.
            let (pending, replay, owner) = match self.fanout.take_replay() {
                Some((owner, pending, replay)) => (pending, replay, Some(owner)),
                None => {
                    let (pending, replay) = self.take_replay();
                    (pending, replay, None)
                }
            };
            #[cfg(feature = "test-hooks")]
            crate::test_hooks::attach_delay();
            let handle = self.fanout.register(sink);
            // The pending batch already reached the other sinks, except when the frame that carried it
            // was taken by a sink that has since detached; `skip` is this sink, whose replay holds those
            // bytes.
            let delivery = match (pending, &owner, handle) {
                (Some(frame), Some(owner), handle) => owner.fan_out(&frame, handle),
                (Some(frame), None, handle) => self.fan_out(&frame, handle),
                (None, _, _) => Delivery::default(),
            };
            owner_slot = owner;
            (replay, handle, delivery)
        };
        drop(owner_slot);
        self.report_delivery(delivery);
        (replay, handle)
    }

    /// Removes a handle from the id's sink set, whichever incarnation registered it; `false` when it is
    /// not attached (never registered here, already detached, or removed by its own panic).
    pub fn detach_sink(&self, handle: u64) -> bool {
        self.fanout.detach(handle)
    }

    /// One frame straight to the attached sinks, not through the batch: `spawn(ws_prefix=…)` and
    /// `attach_text(seed=…)` (§5.2, A1). Returns what the fan-out cost for `report_delivery`.
    ///
    /// Preconditions, all asserted in debug builds: the caller holds the id's delivery lock; this
    /// incarnation's reader has not started, so no `ingest` can have queued a batch whose bytes this
    /// frame would then overtake; and the batch is empty for the same reason. The only caller is
    /// `take_over`, which runs before the reader thread starts and under that lock.
    fn push_frame(&self, frame: &str) -> Delivery {
        if frame.is_empty() {
            return Delivery::default();
        }
        debug_assert!(
            !self.reader_started.load(Ordering::Acquire),
            "push_frame on {} after its reader started: it would overtake a pending frame",
            self.id
        );
        debug_assert!(
            self.text
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .batch
                .pending_bytes()
                == 0,
            "push_frame on {} with a pending batch: it would jump the batch's bytes",
            self.id
        );
        self.fan_out(frame.as_bytes(), None)
    }

    /// Takes the frame that is due (or whatever the batch holds, for the final flush) and fans it out —
    /// both inside the delivery lock, which is what keeps frame order. The report is delivered after the
    /// lock is released, because reporting may log and `log` reaches Python.
    fn pump(&self, now: Option<Instant>) {
        // The test seam: a held batch is what the resume tests need to see delivered by the handover,
        // so only the handover's own `pump_locked` bypasses it.
        #[cfg(feature = "test-hooks")]
        if crate::test_hooks::frames_held() {
            return;
        }
        let delivery = {
            let _delivery = self.fanout.lock_delivery();
            self.pump_locked(now)
        };
        self.report_delivery(delivery);
    }

    /// `pump`'s body; the caller holds the id's delivery lock. Lock order is delivery → text: `ingest`
    /// releases the text lock before calling `pump`, so the reader never holds `text` while waiting for
    /// a delivery another thread is making.
    fn pump_locked(&self, now: Option<Instant>) -> Delivery {
        let frame = {
            let mut text = self.text.lock().unwrap_or_else(|e| e.into_inner());
            match now {
                Some(now) => text.batch.take_due(now),
                None => text.batch.take_all(),
            }
        };
        match frame {
            Some(frame) => self.fan_out(&frame, None),
            None => Delivery::default(),
        }
    }

    /// What one fan-out cost, carried out of the delivery lock and reported by `report_delivery`: a `log`
    /// call reaches Python, and no core lock is held across one (§6.5, "Python may hold a lock and call
    /// back in"). `superseded` marks a frame this incarnation was not allowed to deliver.
    fn fan_out(&self, frame: &[u8], skip: Option<u64>) -> Delivery {
        if self.superseded.load(Ordering::Acquire) {
            return Delivery {
                dropped: frame.len() as u64,
                panicked: Vec::new(),
                superseded: true,
            };
        }
        self.fanout.fan_out(&self.id, frame, skip)
    }

    /// Counts and logs what `fan_out` reported. Called with the delivery lock released: the counters are
    /// atomics, and the log line reaches a Python callback.
    fn report_delivery(&self, delivery: Delivery) {
        if delivery.dropped > 0 {
            self.dropped_bytes
                .fetch_add(delivery.dropped, Ordering::Relaxed);
        }
        for (handle, message) in delivery.panicked {
            self.note_panic();
            self.log(&format!(
                "session {}: sink {handle} panicked in deliver and was detached ({message})",
                self.id
            ));
        }
        if delivery.superseded && !self.superseded_drop_logged.swap(true, Ordering::AcqRel) {
            self.log(&format!(
                "session {}: superseded; later output is dropped, not delivered",
                self.id
            ));
        }
    }

    /// The ticker's 5 ms pass: flush this session's batch when §5.4's window is over.
    pub fn flush_frames(&self, now: Instant) {
        self.pump(Some(now));
    }

    /// The session's last bytes before it dies: flush whatever the batch holds, due or not.
    fn flush_frames_all(&self) {
        self.pump(None);
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
        let alive = self.alive.load(Ordering::Acquire);
        let exit_code = self.exit_code();
        let mut record = DrainRecord {
            session_id: self.id.clone(),
            alive,
            exit_code,
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
            raw_rev: self.raw_rev.load(Ordering::Relaxed),
            screen_text: self.watched_screen(Instant::now()),
            // Filled in by the caller: the resize requests below (only once the record goes out) and
            // the per-socket drop counts, which belong to the core's socket registry.
            resize_requests: Vec::new(),
            socket_drops: Vec::new(),
        };
        if !alive && !self.death_flushed.load(Ordering::Acquire) {
            // The death is published but its last delivery is still running (or pending): this tick
            // reports nothing for the session and keeps it dirty, so the record that says "ended"
            // never precedes the session's last byte (§5.3, review F2).
            self.dirty.store(true, Ordering::Release);
            return None;
        }
        // Only now, when the record is sure to go out, are the requests taken: one that arrived during
        // a withheld record must survive to the next one.
        let resize_requests = self.take_requests();
        record.resize_requests = resize_requests;
        #[cfg(test)]
        death_record_tests::drain_snapshot_gate(&self.id);
        // This record is the report `retire` waits for: the death (code, crashed) is in it, and a code
        // learned after the death record is carried by the first record that has one.
        if !alive {
            self.death_reported.store(true, Ordering::Release);
        }
        if exit_code.is_some() {
            self.code_pending.store(false, Ordering::Release);
        }
        Some(record)
    }

    /// Why this incarnation must not leave the table yet, or `None` when the hub has everything it can
    /// still get from it. The exit record is the one report that cannot be re-created once the session
    /// is gone, so `retire` refuses until a drain has carried it — and until a code learned after it has
    /// gone out too (§5.2 `retire`, §5.3 SF3).
    pub(crate) fn retire_refusal(&self) -> Option<&'static str> {
        if self.alive() {
            return Some("it is still running; kill it first");
        }
        if !self.death_reported.load(Ordering::Acquire) || self.code_pending.load(Ordering::Acquire) {
            return Some("its exit record has not been drained yet");
        }
        None
    }

    /// The id's delivery state; socket and test sinks keep their handle here so detachment is scoped to
    /// the current incarnation.
    pub(crate) fn fanout(&self) -> Arc<SinkFanout> {
        Arc::clone(&self.fanout)
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
        // Once `retire` takes the table's reference away before it closes the fan-out, a drop under the
        // id's delivery lock is reachable (an `attach_sink` through the predecessor holding the last
        // owner `Arc`), and it would close pipes and wait on a console helper while every delivery
        // waits. `attach_sink` therefore drops the owner after the lock; this catches a regression.
        #[cfg(debug_assertions)]
        debug_assert!(
            !self.fanout.delivery_held_here(),
            "session {} dropped while its delivery lock was held",
            self.id
        );
        // Unblocks the writer thread and lets it exit; the pipes close with `Resources`. No `log` call
        // here: this can run on the Python GC thread during interpreter teardown.
        self.writer.close();
    }
}

impl Session {
    /// The ring and the pending batch a new sink's replay is taken from. Called by `attach_sink` while
    /// it holds the id's delivery lock (and `delivery → text` is the one lock order), so no frame can be
    /// taken from this batch between the snapshot and the registration that follows it.
    pub(crate) fn take_replay(&self) -> (Option<Vec<u8>>, String) {
        let mut text = self.text.lock().unwrap_or_else(|e| e.into_inner());
        let pending = text.batch.take_all();
        (pending, text.stream.replay_text())
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

/// §5.3's ordering rules for the record that says "ended".
///
/// A code learned inside a drain's own window must not be lost: the record that carried no code is
/// already out, so `retire` has to wait for the next one (review F3/K2). The gate below parks a drain
/// between its snapshot and its `death_reported`/`code_pending` update, which is exactly that window.
#[cfg(test)]
mod death_record_tests {
    use super::*;
    use crate::{Core, CoreError, CoreOptions, TextRequest};

    /// The armed gate: `id` names the one session a drain may park on, so a drain of another core
    /// running in parallel (cargo runs a binary's tests on many threads) can never consume it — that
    /// would park the wrong drain and let this test's own drain through.
    static DRAIN_GATE: Mutex<(Option<String>, u8)> = Mutex::new((None, 0));
    static DRAIN_GATE_CHANGED: Condvar = Condvar::new();

    /// Called by `drain_record` after its snapshot is built; inert unless a test armed the gate for
    /// this session.
    pub(super) fn drain_snapshot_gate(session_id: &str) {
        let mut gate = DRAIN_GATE.lock().unwrap_or_else(|error| error.into_inner());
        if gate.1 != 1 || gate.0.as_deref() != Some(session_id) {
            return;
        }
        gate.1 = 2;
        DRAIN_GATE_CHANGED.notify_all();
        while gate.1 == 2 {
            gate = DRAIN_GATE_CHANGED
                .wait(gate)
                .unwrap_or_else(|error| error.into_inner());
        }
    }

    /// The exit code arrives after the death record was built but before the drain finished with it. The
    /// record goes out without a code, `retire` refuses (the code is not carried by any record yet), the
    /// next drain carries it, and then `retire` accepts.
    #[test]
    fn a_code_learned_inside_the_first_death_drain_gates_retire() {
        let core = Core::new(
            Box::new(|| false),
            Box::new(|_line: &str| {}),
            CoreOptions::default(),
        )
        .expect("core");
        core.attach_text(TextRequest {
            session_id: "late-code".to_string(),
            cols: 40,
            rows: 6,
            flags: SessionFlags::default(),
            seed_history: None,
            seed: None,
        })
        .expect("attach_text");
        let session = core.session_for_test("late-code").expect("session");
        session.publish_death();

        *DRAIN_GATE.lock().unwrap_or_else(|error| error.into_inner()) =
            (Some("late-code".to_string()), 1);
        let drainer = {
            let core = Arc::clone(&core);
            thread::spawn(move || core.drain())
        };
        let mut gate = DRAIN_GATE.lock().unwrap_or_else(|error| error.into_inner());
        while gate.1 != 2 {
            gate = DRAIN_GATE_CHANGED
                .wait(gate)
                .unwrap_or_else(|error| error.into_inner());
        }
        drop(gate);
        session.set_exit_code(9);
        let records = {
            gate = DRAIN_GATE.lock().unwrap_or_else(|error| error.into_inner());
            gate.1 = 3;
            DRAIN_GATE_CHANGED.notify_all();
            drop(gate);
            drainer.join().expect("drain")
        };
        assert_eq!(records.len(), 1);
        assert_eq!(
            records[0].exit_code, None,
            "the race needs the code to arrive after this snapshot"
        );

        let refused = core
            .retire("late-code")
            .expect_err("retire must wait for the record that carries the code");
        assert!(matches!(refused, CoreError::Invalid(_)), "{refused:?}");
        assert!(
            refused.to_string().contains("exit record"),
            "the refusal names the record: {refused}"
        );

        let carried = core
            .drain()
            .into_iter()
            .find(|record| record.session_id == "late-code")
            .expect("the code is its own record");
        assert_eq!(carried.exit_code, Some(9), "the second record carries the code");
        assert!(core.retire("late-code").expect("retire after the code went out"));
        core.shutdown();
    }
}
