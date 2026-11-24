//! Session creation on the system ConPTY (§7.1): pipes, `CreatePseudoConsole`, `STARTUPINFOEXW` with the
//! pseudoconsole attribute, `CreateProcessW` suspended, the per-session job assigned, then the thread
//! resumed. No bundled `conpty.dll`, no `OpenConsole.exe`: these are the `kernel32` imports.

use std::collections::HashMap;
use std::sync::Arc;

use windows::core::PCWSTR;
use windows::Win32::Foundation::HANDLE;
use windows::Win32::System::Console::{CreatePseudoConsole, HPCON, COORD};
use windows::Win32::System::Pipes::CreatePipe;
use windows::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, InitializeProcThreadAttributeList, OpenProcess,
    ResumeThread, TerminateProcess, UpdateProcThreadAttribute, CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT, EXTENDED_STARTUPINFO_PRESENT, LPPROC_THREAD_ATTRIBUTE_LIST,
    PROCESS_INFORMATION, PROCESS_TERMINATE, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
    STARTF_USESTDHANDLES, STARTUPINFOEXW,
};

use crate::command_line::build_command_line;
use crate::handles::{OwnedHandle, OwnedPseudoConsole};
use crate::job::{JobObject, JobPolicy};
use crate::session::SessionFlags;

/// What to start. `argv[0]` is resolved like pywinpty does before the command line is built.
pub struct SpawnSpec<'a> {
    pub argv: &'a [String],
    pub cwd: Option<&'a str>,
    pub env: &'a HashMap<String, String>,
    pub cols: u16,
    pub rows: u16,
    /// The session's flags: `spawn` reads `job_policy` here, because the job has to exist with the right
    /// limit flags before the child is assigned to it.
    pub flags: SessionFlags,
}

/// A started console session: the process, its two usable pipe ends, the pseudoconsole and its job.
pub struct ConPtyProcess {
    pub pid: u32,
    pub command_line: String,
    pub process: OwnedHandle,
    pub input: OwnedHandle,
    pub output: OwnedHandle,
    pub console: OwnedPseudoConsole,
    pub job: Arc<JobObject>,
    /// False when `AssignProcessToJobObject` was refused (nested jobs, §12.8). What `kill` then does
    /// depends on the policy the job was created with: under `JobPolicy::KillTree` it falls back to the
    /// ToolHelp tree walk, while under `JobPolicy::ClientOnly` it terminates the client process only (it
    /// reopens the single pid with `PROCESS_TERMINATE`) and never walks descendants, because a walk would
    /// reach the client's children that deliberately live outside the job.
    pub job_assigned: bool,
    /// Set when the assignment failed, for the spawn log line.
    pub job_error: Option<String>,
}

/// Terminates a child that `CreateProcessW` created suspended and that must not run: the spawn path
/// failed after the process existed (should-fix 1). The caller's handle closes with `OwnedHandle`.
///
/// The fallback honours the session's `JobPolicy`: `KillTree` may walk the tree with ToolHelp, while
/// `ClientOnly` must never walk it — the walk selects descendants by parent pid regardless of job
/// membership, so it would terminate a child the client started outside the job (the machine-wide
/// service singleton the policy exists to protect). `ClientOnly` therefore reopens the single client pid
/// at most, a zero-tree-walk kill that reaches nothing but the client itself.
fn terminate_suspended(process: &OwnedHandle, pid: u32, policy: JobPolicy) {
    // SAFETY: the process handle is owned by the caller and has PROCESS_TERMINATE.
    if unsafe { TerminateProcess(process.get(), 1) }.is_ok() {
        return;
    }
    match policy {
        // The handle can lack PROCESS_TERMINATE if the security policy narrowed it; the tree walk with
        // PROCESS_TERMINATE is the fallback `kill` uses as well.
        JobPolicy::KillTree => {
            crate::job_tree::terminate_tree(pid);
        }
        // Client-only: reopen this one pid and terminate it; never enumerate, never recurse.
        JobPolicy::ClientOnly => {
            if pid == 0 {
                return;
            }
            // SAFETY: no inheritance; the handle is closed by `OwnedHandle` at the end of the block.
            if let Ok(handle) = unsafe { OpenProcess(PROCESS_TERMINATE, false, pid) } {
                let handle = OwnedHandle::new(handle);
                // SAFETY: the handle was opened with PROCESS_TERMINATE and is closed by the wrapper.
                let _ = unsafe { TerminateProcess(handle.get(), 1) };
            }
        }
    }
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

/// `CREATE_UNICODE_ENVIRONMENT` block: `K=V\0K=V\0\0`, sorted by key (case-insensitively) so the same
/// environment always yields the same block.
fn environment_block(env: &HashMap<String, String>) -> Vec<u16> {
    let mut entries: Vec<(&String, &String)> = env.iter().collect();
    entries.sort_by_key(|(key, _)| key.to_lowercase());
    let mut block = Vec::new();
    for (key, value) in entries {
        block.extend(key.encode_utf16());
        block.push(b'=' as u16);
        block.extend(value.encode_utf16());
        block.push(0);
    }
    block.push(0);
    block
}

fn win32_message(error: &windows::core::Error) -> String {
    let message = error.message();
    if message.is_empty() {
        format!("0x{:08X}", error.code().0 as u32)
    } else {
        message
    }
}

/// Creates the pipes, the pseudoconsole, the job and the suspended child, then resumes it.
///
/// Every handle is owned from the moment it exists, so a failure at any step closes exactly what was
/// created up to that point.
pub fn spawn(spec: &SpawnSpec<'_>) -> Result<ConPtyProcess, String> {
    let (app, command_line) = build_command_line(spec.argv, spec.env).map_err(|e| e.to_string())?;

    // The console's own ends: input pipe (we write into it) and output pipe (we read out of it).
    let mut input_read = HANDLE::default();
    let mut input_write = HANDLE::default();
    // SAFETY: two valid out pointers; no security attributes; default pipe buffer size.
    unsafe { CreatePipe(&mut input_read, &mut input_write, None, 0) }
        .map_err(|e| format!("CreatePipe(input): {}", win32_message(&e)))?;
    let input_read = OwnedHandle::new(input_read);
    let input = OwnedHandle::new(input_write);

    let mut output_read = HANDLE::default();
    let mut output_write = HANDLE::default();
    // SAFETY: as above.
    unsafe { CreatePipe(&mut output_read, &mut output_write, None, 0) }
        .map_err(|e| format!("CreatePipe(output): {}", win32_message(&e)))?;
    let output = OwnedHandle::new(output_read);
    let console_output = OwnedHandle::new(output_write);

    let size = COORD {
        X: spec.cols as i16,
        Y: spec.rows as i16,
    };
    // SAFETY: both pipe ends are open and stay open until the console has them; dwFlags = 0 is the OS
    // default.
    let console = unsafe { CreatePseudoConsole(size, input_read.get(), console_output.get(), 0) }
        .map_err(|e| format!("CreatePseudoConsole: {}", win32_message(&e)))?;
    let console = OwnedPseudoConsole::new(console);

    let (process, thread, pid) = create_process(&app, &command_line, spec, console.get())?;

    // Everything from here to the return is an error scope: the child exists but has never run, so a
    // failure must terminate it. Dropping its handle alone leaves a permanently suspended process, and
    // `console` (an `OwnedPseudoConsole`) closes the HPCON on the same path (should-fix 1).
    let started = (|| -> Result<(Arc<JobObject>, bool, Option<String>), String> {
        // The limit flags come from the session's policy: `ClientOnly` sets SILENT_BREAKAWAY_OK, so a
        // child the client starts later (a machine-wide service singleton) is created outside this job
        // instead of inside it. The child is still suspended here and assigned below, before
        // `ResumeThread`, so nothing it spawns can predate the assignment.
        let job = Arc::new(
            JobObject::new_kill_on_close_with(spec.flags.job_policy)
                .map_err(|e| format!("CreateJobObject: {e}"))?,
        );
        let (job_assigned, job_error) = match job.assign(process.get()) {
            Ok(()) => (true, None),
            Err(error) => (false, Some(error.to_string())),
        };
        // SAFETY: the thread was created suspended and is owned by `thread`. A failed resume returns
        // `u32::MAX` (not an error code), so the return value is the only failure signal.
        let resumed = unsafe { ResumeThread(thread.get()) };
        if resumed == u32::MAX {
            // SAFETY: GetLastError reads this thread's last error, set by the failed ResumeThread.
            let last = unsafe { windows::Win32::Foundation::GetLastError() };
            let error = windows::core::Error::from_hresult(windows::core::HRESULT::from_win32(last.0));
            return Err(format!("ResumeThread: {}", win32_message(&error)));
        }
        Ok((job, job_assigned, job_error))
    })();
    let (job, job_assigned, job_error) = match started {
        Ok(started) => started,
        Err(error) => {
            terminate_suspended(&process, pid, spec.flags.job_policy);
            return Err(error);
        }
    };
    drop(thread);

    // Our copies of the console's ends are no longer needed once the child has the attribute.
    drop(input_read);
    drop(console_output);

    Ok(ConPtyProcess {
        pid,
        command_line,
        process,
        input,
        output,
        console,
        job,
        job_assigned,
        job_error,
    })
}

/// `CreateProcessW` with the pseudoconsole attribute. `lpApplicationName` stays NULL, as winpty-rs
/// passes it, so the child's `GetCommandLineW` is exactly the string built by `command_line`.
fn create_process(
    app: &str,
    command_line: &str,
    spec: &SpawnSpec<'_>,
    console: HPCON,
) -> Result<(OwnedHandle, OwnedHandle, u32), String> {
    let command_line_text = command_line;
    let mut command_line = wide(command_line);
    let cwd = spec.cwd.map(wide);
    let env_block = environment_block(spec.env);

    // SAFETY: the first call only asks for the size the attribute list needs.
    let mut size = 0usize;
    let _ = unsafe { InitializeProcThreadAttributeList(None, 1, None, &mut size) };
    let mut attribute_buffer = vec![0u8; size];
    let attribute_list =
        LPPROC_THREAD_ATTRIBUTE_LIST(attribute_buffer.as_mut_ptr() as *mut core::ffi::c_void);
    // SAFETY: the buffer is at least `size` bytes and outlives the list's use.
    unsafe { InitializeProcThreadAttributeList(Some(attribute_list), 1, None, &mut size) }
        .map_err(|e| format!("InitializeProcThreadAttributeList: {}", win32_message(&e)))?;

    let mut startup: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
    // STARTF_USESTDHANDLES with all three hStd* left NULL is what Microsoft Terminal's
    // ConptyConnection and winpty-rs both pass. Without it CreateProcessW copies the parent's std
    // handles into the child regardless of bInheritHandles, and a console client whose std handles
    // point at the parent's (or at nothing) dies in console init with STATUS_DLL_INIT_FAILED
    // (0xC0000142); with it, the child's console init builds fresh ConIn/ConOut on the pseudoconsole.
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.lpAttributeList = attribute_list;

    let mut process_info = PROCESS_INFORMATION::default();
    // PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE takes the HPCON itself as `lpValue`, not a pointer to it, and
    // `cbSize` is `sizeof(HPCON)`. Verified on this machine: passing `&hpc` makes every console client
    // die in console init with STATUS_DLL_INIT_FAILED (0xC0000142) and emits nothing on the pipe, while
    // the value form yields the expected `ESC[?9001h ESC[?1004h` init sequence; Windows Terminal's
    // ConptyConnection and winpty-rs both pass the value as well.
    // SAFETY: the attribute list was initialised for exactly one attribute; the HPCON is alive for the
    // whole session.
    let mut result = unsafe {
        UpdateProcThreadAttribute(
            attribute_list,
            0,
            PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE as usize,
            Some(console.0 as *const core::ffi::c_void),
            std::mem::size_of::<HPCON>(),
            None,
            None,
        )
    }
    .map_err(|e| format!("UpdateProcThreadAttribute: {}", win32_message(&e)));

    if result.is_ok() {
        let null = PCWSTR::null();
        let current_dir = match &cwd {
            Some(path) => PCWSTR(path.as_ptr()),
            None => null,
        };
        let env_ptr = if env_block.is_empty() {
            None
        } else {
            Some(env_block.as_ptr() as *const core::ffi::c_void)
        };
        // SAFETY: every pointer lives for the call: the mutable command line buffer, the environment
        // block, the working directory and the startup info with its attribute list. The child is
        // created suspended so the job assignment happens before it runs a single instruction.
        result = unsafe {
            CreateProcessW(
                null,
                Some(windows::core::PWSTR(command_line.as_mut_ptr())),
                None,
                None,
                false,
                EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED,
                env_ptr,
                current_dir,
                &startup.StartupInfo,
                &mut process_info,
            )
        }
        .map_err(|e| {
            format!(
                "CreateProcessW({app}, {command_line_text:?}): {}",
                win32_message(&e)
            )
        });
    }

    // SAFETY: the list was initialised above and is not used after this point.
    unsafe { DeleteProcThreadAttributeList(attribute_list) };

    result?;
    if process_info.hProcess.is_invalid() || process_info.hThread.is_invalid() {
        return Err("CreateProcessW reported success without a process handle".to_string());
    }
    let process = OwnedHandle::new(process_info.hProcess);
    let thread = OwnedHandle::new(process_info.hThread);
    Ok((process, thread, process_info.dwProcessId))
}
