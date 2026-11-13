//! Dev-only recorder tap: one child on the **system** ConPTY, every `ReadFile` result handed back as
//! its own frame (design doc §9 step 0/1, `tools/record_terminal_corpus.py`).
//!
//! The terminal corpus is recorded through this binary instead of `pywinpty`, so the fixtures come from
//! the same producer the core uses: the same spawn (`conpty_session::conpty::spawn`: command line
//! parity, pipes, pseudoconsole, job) and the same blocking `ReadFile` shape the core's reader thread
//! runs, with the same 8 KiB buffer. The library keeps that buffer size private to `session.rs`, and a
//! session there buffers and parses its own reads, so the tap cannot reuse the reader itself; it
//! repeats the one call with the same size rather than adding a recording hook to the shipped session.
//!
//! Framing (both pipes, little-endian):
//!
//! ```text
//! frame   = kind:u8, length:u32, payload:length bytes
//! tap -> recorder  C  time_us:u64 since the tap started, then the exact bytes of one ReadFile
//!                  S  pid:u32, cols:u16, rows:u16                      (sent once, after the spawn)
//!                  E  exit_code:i32                                   (last frame; then the tap exits)
//!                  F  utf8 message                                     (spawn failed; exit code 3)
//!                  L  utf8 message                                     (diagnostic only)
//! recorder -> tap  I  raw bytes to write to the child's input pipe
//!                  R  cols:u16, rows:u16                                (ResizePseudoConsole)
//!                  K  (empty)                                          (terminate the child's job)
//! ```
//!
//! A byte chunk is never decoded here: the recorder stores exactly what the read returned, which is
//! what the core's reader sees and what its UTF-8 carry works on. Times are taken when the read
//! returns, so the recorded inter-chunk delays are the tap's own.
//!
//! Nothing opens a window: the child is a ConPTY client by construction, and the recorder launches this
//! process with `CREATE_NO_WINDOW`. A panic anywhere ends the tap with a message on stderr; the child
//! tree dies with the tap's job handle (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) and with the EOF rule
//! below, so a dead recorder never strands an engine.
//!
//! EOF on stdin (the recorder is gone) terminates the child's job; the tap then reaps, reports the exit
//! and exits like it would after a normal `K`.

use std::io::{Read, Write};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use conpty_session::conpty::{spawn, ConPtyProcess, SpawnSpec};
use conpty_session::handles::OwnedPseudoConsole;
use conpty_session::job_tree;

use windows::Win32::Storage::FileSystem::{ReadFile, WriteFile};
use windows::Win32::System::Console::{ResizePseudoConsole, COORD};
use windows::Win32::System::Threading::{
    GetExitCodeProcess, WaitForSingleObject, INFINITE,
};

/// The core reader's read size (`session.rs::IO_CHUNK`), so a chunk boundary here is the boundary the
/// core's reader would have produced for the same bytes.
const READ_BUFFER: usize = 8192;
/// `STILL_ACTIVE`: `GetExitCodeProcess`'s "has not exited" value.
const STILL_ACTIVE: i32 = 259;
/// The §12.7 deadline: closing a pseudoconsole with a read pending can hang, so it happens elsewhere.
const CONSOLE_CLOSE_DEADLINE: Duration = Duration::from_secs(2);
/// How long the tap waits for the reader to file its last bytes after the console was closed.
const READER_DRAIN_DEADLINE: Duration = Duration::from_secs(3);

const FRAME_CHUNK: u8 = b'C';
const FRAME_INPUT: u8 = b'I';
const FRAME_RESIZE: u8 = b'R';
const FRAME_KILL: u8 = b'K';
const FRAME_SPAWNED: u8 = b'S';
const FRAME_EXIT: u8 = b'E';
const FRAME_FAIL: u8 = b'F';
const FRAME_LOG: u8 = b'L';

/// One writer for both the reader thread and the main thread: a frame reaches the pipe whole.
struct Frames {
    out: Mutex<std::io::Stdout>,
}

impl Frames {
    fn new() -> Self {
        Self {
            out: Mutex::new(std::io::stdout()),
        }
    }

    fn send(&self, kind: u8, payload: &[u8]) {
        let mut frame = Vec::with_capacity(5 + payload.len());
        frame.push(kind);
        frame.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        frame.extend_from_slice(payload);
        let mut out = self.out.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        // A closed stdout means the recorder is gone; every later send fails the same way and the
        // stdin EOF path ends the tap, so there is nothing to report here.
        let _ = out.write_all(&frame);
        let _ = out.flush();
    }

    fn chunk(&self, elapsed: Duration, bytes: &[u8]) {
        let mut payload = Vec::with_capacity(8 + bytes.len());
        payload.extend_from_slice(&(elapsed.as_micros() as u64).to_le_bytes());
        payload.extend_from_slice(bytes);
        self.send(FRAME_CHUNK, &payload);
    }

    fn text(&self, kind: u8, message: &str) {
        self.send(kind, message.as_bytes());
    }
}

struct TapArgs {
    cols: u16,
    rows: u16,
    cwd: Option<String>,
    argv: Vec<String>,
}

fn parse_args() -> Result<TapArgs, String> {
    let mut cols = 120u16;
    let mut rows = 36u16;
    let mut cwd: Option<String> = None;
    let mut argv: Vec<String> = Vec::new();
    let mut raw = std::env::args().skip(1);
    while let Some(arg) = raw.next() {
        match arg.as_str() {
            "--" => {
                argv.extend(raw.by_ref());
                break;
            }
            "--cols" => {
                cols = raw
                    .next()
                    .and_then(|value| value.parse().ok())
                    .ok_or_else(|| "--cols needs a number".to_string())?;
            }
            "--rows" => {
                rows = raw
                    .next()
                    .and_then(|value| value.parse().ok())
                    .ok_or_else(|| "--rows needs a number".to_string())?;
            }
            "--cwd" => {
                cwd = Some(raw.next().ok_or_else(|| "--cwd needs a path".to_string())?);
            }
            other => return Err(format!("unknown argument {other:?}; usage: \
                conpty_record_tap [--cols N] [--rows N] [--cwd DIR] -- <argv...>")),
        }
    }
    if argv.is_empty() {
        return Err("no child argv after `--`".to_string());
    }
    Ok(TapArgs {
        cols,
        rows,
        cwd,
        argv,
    })
}

/// The child's environment is the tap's own: the recorder builds it exactly as the hub would.
fn environment() -> std::collections::HashMap<String, String> {
    std::env::vars_os()
        .filter_map(|(key, value)| {
            Some((
                key.into_string().ok()?,
                value.to_string_lossy().into_owned(),
            ))
        })
        .collect()
}

/// Closes a pseudoconsole on its own thread, waiting at most `deadline` (§12.7).
fn close_console(console: OwnedPseudoConsole, deadline: Duration) {
    let (done, wait) = mpsc::channel();
    let spawned = thread::Builder::new()
        .name("tap-close".to_string())
        .spawn(move || {
            console.close();
            let _ = done.send(());
        });
    if spawned.is_ok() {
        let _ = wait.recv_timeout(deadline);
    }
}

fn read_exact_or_eof(reader: &mut impl Read, buffer: &mut [u8]) -> std::io::Result<bool> {
    let mut filled = 0usize;
    while filled < buffer.len() {
        let read = reader.read(&mut buffer[filled..])?;
        if read == 0 {
            return Ok(false);
        }
        filled += read;
    }
    Ok(true)
}

fn main() {
    let result = catch_unwind(AssertUnwindSafe(run));
    let code = match result {
        Ok(code) => code,
        Err(_) => {
            eprintln!("conpty_record_tap: panicked");
            4
        }
    };
    std::process::exit(code);
}

fn run() -> i32 {
    let frames = Arc::new(Frames::new());
    let args = match parse_args() {
        Ok(args) => args,
        Err(message) => {
            frames.text(FRAME_FAIL, &message);
            return 3;
        }
    };
    let env = environment();
    let spec = SpawnSpec {
        argv: &args.argv,
        cwd: args.cwd.as_deref(),
        env: &env,
        cols: args.cols,
        rows: args.rows,
    };
    let process = match spawn(&spec) {
        Ok(process) => process,
        Err(message) => {
            frames.text(FRAME_FAIL, &message);
            return 3;
        }
    };
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

    let mut spawned = Vec::with_capacity(8);
    spawned.extend_from_slice(&pid.to_le_bytes());
    spawned.extend_from_slice(&args.cols.to_le_bytes());
    spawned.extend_from_slice(&args.rows.to_le_bytes());
    frames.send(FRAME_SPAWNED, &spawned);
    frames.text(
        FRAME_LOG,
        &format!(
            "spawned pid {pid} (job {}), cmdline {command_line}{}",
            if job_assigned { "assigned" } else { "not assigned" },
            job_error
                .map(|error| format!("; job assignment failed: {error}"))
                .unwrap_or_default()
        ),
    );

    let started = Instant::now();
    // The console is taken exactly once, by whichever path closes it (exit, kill, EOF).
    let console = Arc::new(Mutex::new(Some(console)));
    let job = Arc::new(job);

    // Reader: the core's own call shape (blocking ReadFile, 8 KiB) and nothing between it and a frame.
    let reader_frames = Arc::clone(&frames);
    let (reader_done, reader_drained) = mpsc::channel();
    let reader = thread::Builder::new()
        .name("tap-read".to_string())
        .spawn(move || {
            let mut buffer = [0u8; READ_BUFFER];
            loop {
                let mut read = 0u32;
                // SAFETY: the output pipe handle is open until this thread returns (the main thread
                // closes the console only after the process exited) and `buffer` is writable for its
                // whole length; no overlapped I/O.
                let status = unsafe {
                    ReadFile(output.get(), Some(&mut buffer), Some(&mut read), None)
                };
                if status.is_err() || read == 0 {
                    break;
                }
                reader_frames.chunk(started.elapsed(), &buffer[..read as usize]);
            }
            let _ = reader_done.send(());
            drop(output);
        });

    // Control: one reader of recorder frames owns the input pipe and the resize/terminate calls.
    let control_frames = Arc::clone(&frames);
    let control_console = Arc::clone(&console);
    let control_job = Arc::clone(&job);
    let control = thread::Builder::new()
        .name("tap-control".to_string())
        .spawn(move || {
            let mut stdin = std::io::stdin().lock();
            let mut header = [0u8; 5];
            loop {
                match read_exact_or_eof(&mut stdin, &mut header) {
                    Ok(true) => {}
                    // The recorder is gone: nothing may outlive it.
                    _ => break,
                }
                let length = u32::from_le_bytes([header[1], header[2], header[3], header[4]]) as usize;
                let mut payload = vec![0u8; length];
                if length > 0 {
                    match read_exact_or_eof(&mut stdin, &mut payload) {
                        Ok(true) => {}
                        _ => break,
                    }
                }
                match header[0] {
                    FRAME_INPUT => {
                        let mut written_total = 0usize;
                        while written_total < payload.len() {
                            let mut written = 0u32;
                            // SAFETY: the input pipe handle is owned by this thread and the slice is
                            // valid for the call.
                            let status = unsafe {
                                WriteFile(
                                    input.get(),
                                    Some(&payload[written_total..]),
                                    Some(&mut written),
                                    None,
                                )
                            };
                            if status.is_err() || written == 0 {
                                control_frames.text(
                                    FRAME_LOG,
                                    "input pipe closed; further input is dropped",
                                );
                                break;
                            }
                            written_total += written as usize;
                        }
                    }
                    FRAME_RESIZE => {
                        if payload.len() < 4 {
                            continue;
                        }
                        let cols = u16::from_le_bytes([payload[0], payload[1]]);
                        let rows = u16::from_le_bytes([payload[2], payload[3]]);
                        let slot = control_console
                            .lock()
                            .unwrap_or_else(|poisoned| poisoned.into_inner());
                        if let Some(console) = slot.as_ref() {
                            let size = COORD {
                                X: cols as i16,
                                Y: rows as i16,
                            };
                            // SAFETY: the HPCON is still open (the slot is cleared exactly once, by
                            // the close path, under this same lock).
                            if let Err(error) = unsafe { ResizePseudoConsole(console.get(), size) } {
                                control_frames.text(
                                    FRAME_LOG,
                                    &format!(
                                        "ResizePseudoConsole({cols}x{rows}): {}",
                                        error.message()
                                    ),
                                );
                            }
                        }
                    }
                    FRAME_KILL => {
                        let terminated = control_job.terminate(1);
                        if terminated.is_err() {
                            job_tree::terminate_tree(pid);
                        }
                        control_frames.text(FRAME_LOG, "kill asked; job terminated");
                    }
                    _ => {}
                }
            }
            // EOF: the recorder died, so the tree must not outlive it.
            if control_job.terminate(1).is_err() {
                job_tree::terminate_tree(pid);
            }
        });

    // Wait for the process itself: the output pipe stays open while conhost lives, so the read cannot
    // signal a natural exit (the same B1 note the session module carries).
    // SAFETY: the process handle is owned here and open for waiting.
    let waited = unsafe { WaitForSingleObject(process.get(), INFINITE) };
    let mut code = 0u32;
    // SAFETY: the process exited (or the wait failed); the handle is still owned and queryable.
    let _ = unsafe { GetExitCodeProcess(process.get(), &mut code) };
    let exit_code = if waited == windows::Win32::Foundation::WAIT_OBJECT_0
        && code as i32 != STILL_ACTIVE
    {
        code as i32
    } else {
        -1
    };

    // Close the console from a helper thread so the pending read returns, then let the reader file its
    // last bytes before the exit frame goes out.
    let closing = console
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .take();
    if let Some(console) = closing {
        close_console(console, CONSOLE_CLOSE_DEADLINE);
    }
    let _ = reader_drained.recv_timeout(READER_DRAIN_DEADLINE);

    frames.send(FRAME_EXIT, &exit_code.to_le_bytes());
    // The control thread blocks on stdin and the reader is done; `process::exit` takes both down with
    // the job handle, which is the last thing that could keep the tree alive.
    let _ = control;
    let _ = reader;
    0
}
