//! One console session: its pipes, its job, one reader thread and one writer thread.
//!
//! Step 1 keeps the per-session work deliberately small: the reader appends the raw bytes to a bounded
//! buffer, counts them and marks the session dirty. Parsing, the W9 ring, coalescing and the screen
//! model arrive in step 2, so nothing here builds a `str` per chunk.
//!
//! A panic anywhere in a thread body ends **that** session (`crashed`, `alive = false`, buffer kept) and
//! nothing else (§6.3).

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
use windows::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject};

use crate::conpty::ConPtyProcess;
use crate::handles::{OwnedHandle, OwnedPseudoConsole};
use crate::job::JobObject;
use crate::job_tree;

/// How long `kill` waits for a helper thread to close the pseudoconsole (§12.7).
const CONSOLE_CLOSE_DEADLINE: Duration = Duration::from_secs(2);
/// How long the reader waits for the process to be reaped once its output pipe ends.
const EXIT_WAIT: Duration = Duration::from_secs(2);
/// `STILL_ACTIVE`: GetExitCodeProcess's "has not exited" value.
const STILL_ACTIVE: i32 = 259;
/// One ReadFile / WriteFile step. 8 KiB matches the hub's old `pty.read(4096)` order of magnitude
/// while halving the number of syscalls for bulk output.
const IO_CHUNK: usize = 8192;

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
        // The last owner closes a console that no `kill`/exit path took: the process is already gone
        // (or the interpreter is tearing down), so there is no live read waiting on it.
        if let Ok(mut slot) = self.console.lock() {
            if let Some(console) = slot.take() {
                console.close();
            }
        }
    }
}

#[derive(Debug, Default)]
struct WriterQueue {
    chunks: VecDeque<Vec<u8>>,
    pending: usize,
    closed: bool,
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

pub struct Session {
    pub id: String,
    pub pid: u32,
    resources: Arc<Resources>,
    buffer: Mutex<VecDeque<u8>>,
    /// The last `raw_tail` reply, handed back when the buffer lock misses its deadline (§5.2).
    last_tail: Mutex<Vec<u8>>,
    writer: Mutex<WriterQueue>,
    writer_ready: Condvar,
    alive: AtomicBool,
    exit_code: AtomicI32,
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
    log: Arc<dyn Fn(&str) + Send + Sync>,
}

impl Session {
    /// Wraps a spawned process and starts its reader and writer threads.
    pub fn start(
        id: String,
        process: ConPtyProcess,
        cols: u16,
        rows: u16,
        options: SessionOptions,
        tick: Arc<TickFlags>,
        log: Arc<dyn Fn(&str) + Send + Sync>,
        seed_history: Option<Vec<u8>>,
    ) -> Arc<Self> {
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
        if let Some(error) = job_error {
            log(&format!(
                "session {id}: job assignment failed, kill will walk the tree with ToolHelp: {error}"
            ));
        }
        log(&format!(
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
            writer: Mutex::new(WriterQueue::default()),
            writer_ready: Condvar::new(),
            alive: AtomicBool::new(true),
            exit_code: AtomicI32::new(-1),
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
        thread::Builder::new()
            .name(format!("core-read-{}", session.id))
            .spawn(move || reader.reader_body())
            .expect("spawn reader thread");
        thread::Builder::new()
            .name(format!("core-write-{}", session.id))
            .spawn(move || writer.writer_body())
            .expect("spawn writer thread");

        session
    }

    /// One place that reaches the core's `log` callable, so every line goes through the same path.
    fn log(&self, message: &str) {
        (self.log)(message);
    }

    /// ReadFile until the console's output pipe closes; a panic here crashes only this session.
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
            self.mark_crashed("reader thread panicked");
        } else {
            self.on_output_closed();
        }
    }

    /// WriteFile each queued chunk in arrival order; one writer thread per session is what keeps D7/D1
    /// byte order intact.
    fn writer_body(self: Arc<Self>) {
        let result = catch_unwind(AssertUnwindSafe(|| loop {
            let next = {
                let mut queue = self.writer.lock().unwrap_or_else(|e| e.into_inner());
                while queue.chunks.is_empty() && !queue.closed {
                    queue = self
                        .writer_ready
                        .wait(queue)
                        .unwrap_or_else(|e| e.into_inner());
                }
                queue.chunks.pop_front()
            };
            let Some(mut chunk) = next else {
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
                    let mut queue = self.writer.lock().unwrap_or_else(|e| e.into_inner());
                    queue.pending = queue.pending.saturating_sub(chunk.len() - written_total);
                    queue.closed = true;
                    self.writer_ready.notify_all();
                    self.log(&format!(
                        "session {}: input pipe closed, {} queued bytes dropped",
                        self.id,
                        queue.pending
                    ));
                    return;
                }
                written_total += written as usize;
            }
            let mut queue = self.writer.lock().unwrap_or_else(|e| e.into_inner());
            queue.pending = queue.pending.saturating_sub(chunk.len());
            self.writer_ready.notify_all();
            chunk.clear();
        }));
        if result.is_err() {
            self.mark_crashed("writer thread panicked");
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

    /// The output pipe ended: reap the process (bounded) and report the exit.
    fn on_output_closed(&self) {
        // SAFETY: the process handle is owned by the session.
        let waited = unsafe { WaitForSingleObject(self.resources.process.get(), EXIT_WAIT.as_millis() as u32) };
        let mut code = 0u32;
        // SAFETY: the handle is owned and open for query.
        let read_ok = unsafe { GetExitCodeProcess(self.resources.process.get(), &mut code) }.is_ok();
        if waited == WAIT_OBJECT_0 && read_ok {
            self.set_exit(code as i32);
            self.log(&format!(
                "session {}: exited with code {}",
                self.id, code as i32
            ));
        } else if read_ok && code as i32 == STILL_ACTIVE {
            self.log(&format!(
                "session {}: output closed while pid {} is still running",
                self.id, self.pid
            ));
            return;
        } else {
            self.set_exit(code as i32);
        }
        self.close_console(CONSOLE_CLOSE_DEADLINE);
    }

    fn set_exit(&self, code: i32) {
        // The exit is one event: `kill` and the reader thread can both observe it, but only the first
        // caller sets the code, marks the session dirty and wakes the ticker, so `drain` reports one
        // dead record and a second call returns nothing new (§5.3).
        if self
            .exit_code
            .compare_exchange(-1, code, Ordering::AcqRel, Ordering::Relaxed)
            .is_ok()
        {
            self.alive.store(false, Ordering::Release);
            self.dirty.store(true, Ordering::Release);
            self.tick.urgent.store(true, Ordering::Release);
        } else {
            self.alive.store(false, Ordering::Release);
        }
    }

    fn mark_crashed(&self, reason: &str) {
        self.crashed.store(true, Ordering::Release);
        self.alive.store(false, Ordering::Release);
        self.dirty.store(true, Ordering::Release);
        self.tick.urgent.store(true, Ordering::Release);
        self.log(&format!("session {}: crashed ({reason})", self.id));
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
        let (done, wait) = mpsc::channel();
        thread::Builder::new()
            .name(format!("core-close-{}", self.id))
            .spawn(move || {
                let _ = catch_unwind(AssertUnwindSafe(|| console.close()));
                let _ = done.send(());
            })
            .expect("spawn pseudoconsole close thread");
        match wait.recv_timeout(deadline) {
            Ok(()) => true,
            Err(_) => {
                self.log(&format!(
                    "session {}: pseudoconsole close still pending after {:?}",
                    self.id, deadline
                ));
                false
            }
        }
    }

    /// Enqueues one write as a whole or not at all: a keystroke's bytes are never split across two
    /// writes. Returns the bytes accepted (0 when the deadline expired with the queue full).
    pub fn write(&self, text: &[u8], deadline: Duration) -> Result<usize, &'static str> {
        if text.is_empty() {
            return Ok(0);
        }
        let cap = self.options.write_queue_bytes;
        let end = Instant::now() + deadline;
        let mut queue = self.writer.lock().unwrap_or_else(|e| e.into_inner());
        if queue.closed {
            return Err("session input is closed");
        }
        while queue.pending + text.len() > cap {
            let remaining = end.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(0);
            }
            let (guard, timeout) = self
                .writer_ready
                .wait_timeout(queue, remaining)
                .unwrap_or_else(|e| e.into_inner());
            queue = guard;
            if queue.closed {
                return Err("session input is closed");
            }
            if timeout.timed_out() && queue.pending + text.len() > cap {
                return Ok(0);
            }
        }
        queue.pending += text.len();
        queue.chunks.push_back(text.to_vec());
        self.writer_ready.notify_all();
        Ok(text.len())
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

    /// Terminates the session's tree (job first, ToolHelp fallback), waits up to `grace` for the root
    /// process and closes the pseudoconsole on a helper thread.
    pub fn kill(&self, grace: Duration) -> Result<Option<i32>, String> {
        if !self.alive.load(Ordering::Acquire) {
            return Ok(self.exit_code());
        }
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
        // SAFETY: the process handle is owned by the session.
        unsafe {
            WaitForSingleObject(self.resources.process.get(), grace.as_millis() as u32);
        }
        let mut code = 0u32;
        // SAFETY: as above; a still-running process reports STILL_ACTIVE and is treated as "not exited".
        let read_ok = unsafe { GetExitCodeProcess(self.resources.process.get(), &mut code) }.is_ok();
        if read_ok && code as i32 != STILL_ACTIVE {
            self.set_exit(code as i32);
            self.close_console(CONSOLE_CLOSE_DEADLINE);
            self.log(&format!(
                "session {}: killed pid {} ({}), exit code {}",
                self.id,
                self.pid,
                method,
                code as i32
            ));
            return Ok(Some(code as i32));
        }
        self.log(&format!(
            "session {}: pid {} did not exit within {:?} after {}",
            self.id, self.pid, grace, method
        ));
        Ok(None)
    }

    /// Last `max_bytes` of the bounded raw buffer. On a missed deadline the previous reply is returned
    /// and the session is marked `degraded`, so a wedged buffer cannot hold the hub's loop.
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

    pub fn size(&self) -> (u16, u16) {
        (
            self.cols.load(Ordering::Relaxed) as u16,
            self.rows.load(Ordering::Relaxed) as u16,
        )
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
        // Unblocks the writer thread and lets it drain or exit; the pipes close with `Resources`.
        if let Ok(mut queue) = self.writer.lock() {
            queue.closed = true;
        }
        self.writer_ready.notify_all();
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
