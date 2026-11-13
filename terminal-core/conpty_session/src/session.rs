//! One console session: its pipes, its job, a reader thread, a writer thread and a reaper thread.
//!
//! Step 1 keeps the per-session work deliberately small: the reader appends the raw bytes to a bounded
//! buffer, counts them and marks the session dirty. Parsing, the W9 ring, coalescing and the screen
//! model arrive in step 2, so nothing here builds a `str` per chunk.
//!
//! Exit detection is the reaper's job. The reader's `ReadFile` cannot signal a natural exit: the write
//! end of the output pipe belongs to conhost, which stays alive while we hold the HPCON, so the read
//! never completes on its own (review B1). The reaper waits on the process handle instead, records the
//! exit code and closes the console on a helper thread so the pending read returns; the death record is
//! published only after that read has ended, so the last bytes are in the buffer before `alive = false`.
//!
//! A panic anywhere in a thread body ends **that** session (`crashed`, `alive = false`, buffer kept) and
//! nothing else (§6.3), and it is counted in `panics`.

use std::collections::VecDeque;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU32, AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use windows::Win32::Foundation::WAIT_OBJECT_0;
use windows::Win32::Storage::FileSystem::{ReadFile, WriteFile};
use windows::Win32::System::Console::{ResizePseudoConsole, COORD};
use windows::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject, INFINITE};

use crate::conpty::ConPtyProcess;
use crate::handles::{OwnedHandle, OwnedPseudoConsole};
use crate::job::JobObject;
use crate::job_tree;

/// How long the exit/kill teardown waits for a helper thread to close the pseudoconsole (§12.7).
pub const CONSOLE_CLOSE_DEADLINE: Duration = Duration::from_secs(2);
/// `STILL_ACTIVE`: GetExitCodeProcess's "has not exited" value.
const STILL_ACTIVE: i32 = 259;
/// One ReadFile / WriteFile step. 8 KiB matches the hub's old `pty.read(4096)` order of magnitude
/// while halving the number of syscalls for bulk output.
const IO_CHUNK: usize = 8192;
/// The reaper only blocks in `WaitForSingleObject` (and the close helper), so it needs no real stack.
const REAPER_STACK: usize = 64 * 1024;

/// Tunables a session needs from the core.
#[derive(Clone, Copy, Debug)]
pub struct SessionOptions {
    pub raw_cap_bytes: usize,
    pub write_queue_bytes: usize,
}

/// The core's ticker sees "something changed" through these two flags, never through a session lock.
#[derive(Debug, Default)]
pub struct TickFlags {
    pub pending: AtomicBool,
    pub urgent: AtomicBool,
}

/// What `drain` reports for one dirty session (§5.3's record minus the step-2 screen fields).
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
}

/// Handles a session's threads use, shared by ownership rather than copied.
struct Resources {
    process: OwnedHandle,
    input: OwnedHandle,
    output: OwnedHandle,
    job: Arc<JobObject>,
    job_assigned: bool,
    /// Taken exactly once, by whichever helper thread closes the pseudoconsole.
    console: Mutex<Option<OwnedPseudoConsole>>,
}

impl Drop for Resources {
    fn drop(&mut self) {
        // Never close synchronously on the dropping thread: `del core` drops sessions on the Python GC
        // thread, and closing a console with a read pending is the §12.7 hang. The slot is empty when
        // the exit/kill teardown already closed it, so this only runs for a session that was abandoned.
        if let Ok(mut slot) = self.console.lock() {
            if let Some(console) = slot.take() {
                close_console_off_thread(console, CONSOLE_CLOSE_DEADLINE);
            }
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
            if state.pending + text.len() <= self.cap {
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
            if wait.timed_out() && state.pending + text.len() > self.cap {
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

/// Closes a pseudoconsole on its own thread and waits at most `deadline`; `false` means the helper is
/// still inside `ClosePseudoConsole` and will finish on its own (§12.7).
fn close_console_off_thread(console: OwnedPseudoConsole, deadline: Duration) -> bool {
    let (done, wait) = mpsc::channel();
    let spawned = thread::Builder::new()
        .name("core-close".to_string())
        .stack_size(REAPER_STACK)
        .spawn(move || {
            let _ = catch_unwind(AssertUnwindSafe(|| console.close()));
            let _ = done.send(());
        });
    match spawned {
        Ok(_) => matches!(wait.recv_timeout(deadline), Ok(())),
        Err(_) => false,
    }
}

pub struct Session {
    pub id: String,
    pub pid: u32,
    resources: Arc<Resources>,
    buffer: Mutex<VecDeque<u8>>,
    /// The last `raw_tail` reply, handed back when the buffer lock misses its deadline (§5.2).
    last_tail: Mutex<Vec<u8>>,
    writer: WriteQueue,
    alive: AtomicBool,
    exit_code: AtomicI32,
    /// Set once the output pipe stopped being read, so a later exit does not wait for a reader that is
    /// already gone before publishing the death record.
    output_ended: AtomicBool,
    bytes_total: AtomicU64,
    chunks_total: AtomicU64,
    dropped_bytes: AtomicU64,
    last_output_ms: AtomicU64,
    dirty: AtomicBool,
    crashed: AtomicBool,
    degraded: AtomicBool,
    cols: AtomicU32,
    rows: AtomicU32,
    options: SessionOptions,
    tick: Arc<TickFlags>,
    panics: Arc<AtomicU64>,
    log: Arc<dyn Fn(&str) + Send + Sync>,
}

impl Session {
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
        seed_history: Option<Vec<u8>>,
    ) -> (Arc<Self>, Vec<String>) {
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

        let session = Arc::new(Self {
            id,
            pid,
            resources: Arc::new(Resources {
                process,
                input,
                output,
                job,
                job_assigned,
                console: Mutex::new(Some(console)),
            }),
            buffer: Mutex::new(VecDeque::new()),
            last_tail: Mutex::new(Vec::new()),
            writer: WriteQueue::new(options.write_queue_bytes),
            alive: AtomicBool::new(true),
            exit_code: AtomicI32::new(-1),
            output_ended: AtomicBool::new(false),
            bytes_total: AtomicU64::new(0),
            chunks_total: AtomicU64::new(0),
            dropped_bytes: AtomicU64::new(0),
            last_output_ms: AtomicU64::new(0),
            dirty: AtomicBool::new(false),
            crashed: AtomicBool::new(false),
            degraded: AtomicBool::new(false),
            cols: AtomicU32::new(cols as u32),
            rows: AtomicU32::new(rows as u32),
            options,
            tick,
            panics,
            log,
        });

        // A resumed session starts with the archived scrollback in its raw ring (A1); it is history,
        // not output, so the byte counters stay at zero until the new process prints.
        if let Some(seed) = seed_history {
            if !seed.is_empty() {
                let mut buffer = session.buffer.lock().unwrap_or_else(|e| e.into_inner());
                buffer.extend(seed);
                trim_buffer(&mut buffer, options.raw_cap_bytes, &session.dropped_bytes);
            }
        }

        let reader = Arc::clone(&session);
        let writer = Arc::clone(&session);
        let reaper = Arc::clone(&session);
        thread::Builder::new()
            .name(format!("core-read-{}", session.id))
            .spawn(move || reader.reader_body())
            .expect("spawn reader thread");
        thread::Builder::new()
            .name(format!("core-write-{}", session.id))
            .spawn(move || writer.writer_body())
            .expect("spawn writer thread");
        // 64 KiB is enough: the body only waits on the process handle.
        thread::Builder::new()
            .name(format!("core-reap-{}", session.id))
            .stack_size(REAPER_STACK)
            .spawn(move || reaper.reaper_body())
            .expect("spawn reaper thread");

        (session, spawn_log)
    }

    /// One place that reaches the core's `log` callable, so every line goes through the same path.
    fn log(&self, message: &str) {
        (self.log)(message);
    }

    /// ReadFile until the console's output pipe stops producing; a panic here crashes only this session.
    fn reader_body(self: Arc<Self>) {
        let result = catch_unwind(AssertUnwindSafe(|| {
            let mut chunk = [0u8; IO_CHUNK];
            loop {
                let mut read = 0u32;
                // SAFETY: the pipe handle is open for the session's lifetime and `chunk` is a valid
                // writable buffer; no overlapped I/O.
                let status = unsafe {
                    ReadFile(
                        self.resources.output.get(),
                        Some(&mut chunk),
                        Some(&mut read),
                        None,
                    )
                };
                if status.is_err() || read == 0 {
                    break;
                }
                self.record_output(&chunk[..read as usize]);
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
                        self.resources.input.get(),
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
        let result = catch_unwind(AssertUnwindSafe(|| {
            // SAFETY: the process handle is owned by the session and open for waiting.
            let waited = unsafe { WaitForSingleObject(self.resources.process.get(), INFINITE) };
            if waited == WAIT_OBJECT_0 {
                self.on_process_exit();
            }
        }));
        if result.is_err() {
            self.note_panic();
            self.mark_crashed("reaper thread panicked");
        }
    }

    /// The process is gone. Record its code, then run the same teardown `kill` runs after its
    /// job-termination step: close the console on a helper thread so the pending read returns.
    fn on_process_exit(&self) {
        let mut code = 0u32;
        // SAFETY: the process handle is owned by the session and open for query.
        match unsafe { GetExitCodeProcess(self.resources.process.get(), &mut code) } {
            Ok(()) if code as i32 != STILL_ACTIVE => {
                self.set_exit_code(code as i32);
                self.log(&format!(
                    "session {}: exited with code {}",
                    self.id, code as i32
                ));
            }
            Ok(()) => return,
            Err(error) => {
                // Leave `exit_code` as None: a fake 0 would archive a crash as a clean exit.
                self.log(&format!(
                    "session {}: GetExitCodeProcess failed after the process exited: {}",
                    self.id,
                    error.message()
                ));
            }
        }
        if !self.close_console(CONSOLE_CLOSE_DEADLINE) {
            // The close deadline passed: the hub learns of the death without waiting for the read.
            self.publish_death();
            return;
        }
        // The close returned, so the reader's read is ending; publish here only if it is already gone.
        // Otherwise the reader publishes the record after its last bytes (the ordering §5.3 wants).
        if self.output_ended.load(Ordering::Acquire) {
            self.publish_death();
        }
    }

    /// Append one read to the bounded buffer, count it and wake the ticker.
    fn record_output(&self, chunk: &[u8]) {
        {
            let mut buffer = self.buffer.lock().unwrap_or_else(|e| e.into_inner());
            buffer.extend(chunk);
            trim_buffer(&mut buffer, self.options.raw_cap_bytes, &self.dropped_bytes);
        }
        self.bytes_total
            .fetch_add(chunk.len() as u64, Ordering::Relaxed);
        self.chunks_total.fetch_add(1, Ordering::Relaxed);
        self.last_output_ms.store(now_unix_ms(), Ordering::Relaxed);
        self.dirty.store(true, Ordering::Release);
        self.tick.pending.store(true, Ordering::Release);
    }

    /// The output pipe stopped producing. Either the exit/kill teardown closed the console (then the
    /// exit code is already recorded) or the child closed its own side of the pipe.
    fn on_output_closed(&self) {
        if self.exit_code().is_some() || self.crashed() {
            self.publish_death();
            return;
        }
        let mut code = 0u32;
        // SAFETY: the process handle is owned by the session and open for query.
        match unsafe { GetExitCodeProcess(self.resources.process.get(), &mut code) } {
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
    fn set_exit_code(&self, code: i32) {
        let _ = self
            .exit_code
            .compare_exchange(-1, code, Ordering::AcqRel, Ordering::Relaxed);
    }

    /// Publishes the death record: `alive = false`, one dirty record and an urgent wakeup (§5.3). Called
    /// after the reader's last bytes (or when the console-close deadline passes), never before.
    fn publish_death(&self) {
        if self.alive.swap(false, Ordering::AcqRel) {
            self.dirty.store(true, Ordering::Release);
            self.tick.urgent.store(true, Ordering::Release);
            // Nothing can reach a dead process: closing the queue ends its writer thread, so the
            // session's last `Arc` is released instead of being held by a thread parked on a condvar
            // (B1: the replaced session must release its handles, not leak a writer).
            self.writer.close();
        }
    }

    /// Counts a caught panic: `stats.panics` covers reader, writer, reaper, ticker and close helpers.
    fn note_panic(&self) {
        self.panics.fetch_add(1, Ordering::Relaxed);
    }

    /// Marks the session crashed. The thread bodies call it when their own body panics; the tests call it
    /// to reproduce "marked dead, tree still alive" without panicking a real thread (should-fix 2:
    /// `kill` must still terminate the tree).
    #[doc(hidden)]
    pub fn mark_crashed(&self, reason: &str) {
        self.crashed.store(true, Ordering::Release);
        self.dirty.store(true, Ordering::Release);
        self.tick.urgent.store(true, Ordering::Release);
        self.log(&format!("session {}: crashed ({reason})", self.id));
        // A crashed session has no reader to publish at EOF, so the death is published here.
        self.publish_death();
        // The pseudoconsole is closed on a helper thread, never by the panicking thread's own read path.
        self.close_console(CONSOLE_CLOSE_DEADLINE);
    }

    /// Takes the pseudoconsole and closes it on a helper thread, waiting at most `deadline` (§12.7:
    /// closing a pseudoconsole with a pending read is a known hang, so the reader never does it).
    fn close_console(&self, deadline: Duration) -> bool {
        let console = match self.resources.console.lock() {
            Ok(mut slot) => slot.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        let Some(console) = console else {
            return true;
        };
        let closed = close_console_off_thread(console, deadline);
        if !closed {
            self.log(&format!(
                "session {}: pseudoconsole close still pending after {:?}",
                self.id, deadline
            ));
        }
        closed
    }

    /// Queues one write as a whole or not at all (SF4, §5.2): empty text returns 0 with nothing queued, a
    /// full queue at the deadline is `Timeout` with nothing queued, so a retry cannot duplicate bytes.
    pub fn write(&self, text: &[u8], deadline: Duration) -> Result<usize, WriteError> {
        self.writer.push(text, deadline)
    }

    /// `ResizePseudoConsole`. The core never propagates a size (that is hub policy); it only applies it.
    pub fn resize(&self, cols: u16, rows: u16, deadline: Duration) -> Result<(u16, u16), String> {
        let console = match lock_deadline(&self.resources.console, deadline) {
            Some(slot) => slot,
            None => return Err(format!("session {}: resize missed its deadline", self.id)),
        };
        let Some(console) = console.as_ref() else {
            return Err(format!("session {}: pseudoconsole is closed", self.id));
        };
        let size = COORD {
            X: cols as i16,
            Y: rows as i16,
        };
        // SAFETY: the HPCON is owned and still open (the Option is None once closed).
        unsafe { ResizePseudoConsole(console.get(), size) }
            .map_err(|error| format!("ResizePseudoConsole: {}", error.message()))?;
        self.cols.store(cols as u32, Ordering::Relaxed);
        self.rows.store(rows as u32, Ordering::Relaxed);
        Ok((cols, rows))
    }

    /// Terminates the session's tree and waits up to `budget` for the exit and the console close. The
    /// tree is always terminated, even when the session is already marked dead: a reader panic leaves the
    /// CLI running (should-fix 2), and `TerminateJobObject` is idempotent. Only the wait is skipped when
    /// the exit is already confirmed. `budget` bounds the whole call; a close helper that outlives it
    /// keeps running on its own thread (should-fix 3).
    pub fn kill(&self, budget: Duration) -> Result<Option<i32>, String> {
        let end = Instant::now() + budget;
        let mut method = "TerminateJobObject";
        let result = if self.resources.job_assigned {
            self.resources.job.terminate(1)
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

        if self.exit_code().is_none() {
            // SAFETY: the process handle is owned by the session.
            unsafe {
                WaitForSingleObject(
                    self.resources.process.get(),
                    end.saturating_duration_since(Instant::now()).as_millis() as u32,
                );
            }
            let mut code = 0u32;
            // SAFETY: as above; a still-running process reports STILL_ACTIVE and has no code.
            match unsafe { GetExitCodeProcess(self.resources.process.get(), &mut code) } {
                Ok(()) if code as i32 != STILL_ACTIVE => {
                    self.set_exit_code(code as i32);
                    self.log(&format!(
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

        let remaining = end.saturating_duration_since(Instant::now());
        self.close_console(remaining.min(CONSOLE_CLOSE_DEADLINE));
        if self.exit_code().is_some() {
            // The exit is confirmed: the death record may go out, after the console was asked to close so
            // the reader can file its last bytes (§5.3).
            self.publish_death();
        } else {
            self.log(&format!(
                "session {}: pid {} did not exit within {:?} after {}",
                self.id, self.pid, budget, method
            ));
        }
        Ok(self.exit_code())
    }

    /// Last `max_bytes` of the bounded raw buffer. On a missed deadline the previous reply is returned
    /// and the session is marked `degraded`, which is itself a dirty flag so the hub's next drain sees
    /// it without waiting for more output.
    pub fn raw_tail(&self, max_bytes: usize, deadline: Duration) -> Vec<u8> {
        let taken = match lock_deadline(&self.buffer, deadline) {
            Some(buffer) => {
                let start = buffer.len().saturating_sub(max_bytes);
                let bytes: Vec<u8> = buffer.range(start..).copied().collect();
                drop(buffer);
                if let Ok(mut last) = self.last_tail.lock() {
                    *last = bytes.clone();
                }
                Some(bytes)
            }
            None => None,
        };
        match taken {
            Some(bytes) => bytes,
            None => {
                self.degraded.store(true, Ordering::Release);
                self.dirty.store(true, Ordering::Release);
                self.tick.pending.store(true, Ordering::Release);
                self.last_tail
                    .lock()
                    .map(|last| last.clone())
                    .unwrap_or_default()
            }
        }
    }

    /// The dirty session's counters, or `None` when nothing changed since the last drain. Reading only
    /// atomics is what makes the 50 ms drain cheap (§5.3).
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

/// Keeps the raw buffer at `cap` bytes by dropping the oldest bytes and counting them.
fn trim_buffer(buffer: &mut VecDeque<u8>, cap: usize, dropped: &AtomicU64) {
    if cap == 0 {
        let len = buffer.len();
        buffer.clear();
        dropped.fetch_add(len as u64, Ordering::Relaxed);
        return;
    }
    if buffer.len() > cap {
        let excess = buffer.len() - cap;
        buffer.drain(..excess);
        dropped.fetch_add(excess as u64, Ordering::Relaxed);
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
        // A text bigger than the cap can never fit: it is refused as a whole, not split.
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