//! Test support: a session client that starts one child detached from the session's console.
//!
//! `detached_client <program> [args...]` creates the child with `DETACHED_PROCESS` — no console at all,
//! so the pseudoconsole the client itself is attached to owns nothing the child depends on — prints
//! `detached-pid <pid>` on that console, then waits for the child so the session keeps a live client for
//! as long as its child runs. `detached_client --sleep <seconds>` is that child's mode: a process that
//! does nothing but live, with no console of its own.
//!
//! This is the shape of `doubao.exe` starting `doubao.exe --type=service`: the service is the same
//! executable started again in a service mode and detached (`AttachConsole` from it fails with
//! `ERROR_INVALID_HANDLE`), which is why closing one session's pseudoconsole cannot end it and only a job
//! object could — the case `tests/session_lifecycle.rs` pins for `JobPolicy::ClientOnly`.

use std::collections::HashMap;
use std::io::Write;

use windows::core::{PCWSTR, PWSTR};
use windows::Win32::Foundation::WAIT_OBJECT_0;
use windows::Win32::System::Threading::{
    CreateProcessW, WaitForSingleObject, GetExitCodeProcess, INFINITE, PROCESS_CREATION_FLAGS,
    PROCESS_INFORMATION, STARTUPINFOW,
};

use conpty_session::command_line::build_command_line;
use conpty_session::handles::OwnedHandle;

/// `DETACHED_PROCESS` (0x00000008): the child gets no console and inherits none. The `windows` crate gates
/// the named constant behind `Win32_System_Kernel`, which this crate does not enable; the flag is plain
/// data, so it is spelled here.
const DETACHED_PROCESS: PROCESS_CREATION_FLAGS = PROCESS_CREATION_FLAGS(0x0000_0008);

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

fn usage() -> ! {
    eprintln!("usage: detached_client <program> [args...]");
    eprintln!("       detached_client --sleep <seconds>   (service mode: sleeps with no console, no I/O)");
    std::process::exit(2)
}

/// `--sleep <seconds>`: the "service mode" of this binary, a process that does nothing but live.
///
/// It prints nothing on purpose: a `DETACHED_PROCESS` child has no console, so any `println!` here would be
/// a write to an invalid handle. Using this mode as the detached child makes the case the exact shape of
/// `doubao.exe --type=service` — the same executable as its client, started again in a service mode, with
/// no console of its own.
fn sleep_mode(seconds: u64) {
    std::thread::sleep(std::time::Duration::from_secs(seconds));
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.is_empty() {
        usage();
    }
    if argv[0] == "--sleep" {
        let seconds = argv.get(1).and_then(|value| value.parse().ok()).unwrap_or(30);
        sleep_mode(seconds);
        return;
    }
    // The same resolution the core's own spawn uses, so `ping.exe` becomes a full path the way a session's
    // `argv[0]` does.
    let env: HashMap<String, String> = std::env::vars().collect();
    let (app, command_line) = match build_command_line(&argv, &env) {
        Ok((app, command_line)) => (app, command_line),
        Err(error) => {
            eprintln!("detached_client: {error}");
            std::process::exit(3);
        }
    };
    let app_wide = wide(&app);
    let mut command_line_wide = wide(&command_line);
    let mut startup: STARTUPINFOW = unsafe { std::mem::zeroed() };
    startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
    let mut process_info = PROCESS_INFORMATION::default();
    // SAFETY: both wide buffers live for the call; the startup info is the plain `STARTUPINFOW` (no
    // attribute list), the environment and the working directory are inherited, and the returned handles
    // are owned by the wrappers below.
    let created = unsafe {
        CreateProcessW(
            PCWSTR(app_wide.as_ptr()),
            Some(PWSTR(command_line_wide.as_mut_ptr())),
            None,
            None,
            false,
            DETACHED_PROCESS,
            None,
            PCWSTR::null(),
            &startup,
            &mut process_info,
        )
    };
    if let Err(error) = created {
        eprintln!("detached_client: CreateProcessW({command_line:?}): {}", error.message());
        std::process::exit(3);
    }
    if process_info.hProcess.is_invalid() || process_info.hThread.is_invalid() {
        eprintln!("detached_client: CreateProcessW reported success without a process handle");
        std::process::exit(3);
    }
    let process = OwnedHandle::new(process_info.hProcess);
    drop(OwnedHandle::new(process_info.hThread));
    // The pid the caller asserts on: printed on the console the client is attached to, so it lands in the
    // session's output where a person (or the test) can read the exact process. The flush is not
    // decorative: a ConPTY reader sees this line through the console host, and the assertion starts
    // polling as soon as the client exists, so the announcement must leave this process immediately
    // instead of waiting for the buffer to fill or for the process to exit.
    println!("detached-pid {}", process_info.dwProcessId);
    let _ = std::io::stdout().flush();

    // Stay alive while the child runs: the session's client must be a live process when the case kills it,
    // exactly as the real client is while its service runs.
    // SAFETY: the process handle is owned and open, and INFINITE here is a bounded wait because the child
    // is a test process that exits by itself.
    let waited = unsafe { WaitForSingleObject(process.get(), INFINITE) };
    let mut code = 0u32;
    // SAFETY: as above; the handle is still open and `code` is a valid out pointer.
    let _ = unsafe { GetExitCodeProcess(process.get(), &mut code) };
    // Reported on the session's console, so a case that waited badly is visible instead of passing silently.
    if waited == WAIT_OBJECT_0 {
        println!("detached-exit {code}");
    } else {
        println!("detached-wait-failed {waited:?}");
    }
}
