//! Win32 process plumbing for the owned hub: a kill-on-close Job Object, a suspended start so the
//! hub (and the venv launcher's real python child) is inside the job before it runs a single
//! instruction, a process-tree kill for taking over an external hub, process identity and
//! command-line queries for telling an orphan from a reused PID or an unrelated program, the PID
//! that listens on a port, and a random per-launch token.

use std::collections::{HashMap, HashSet};
use std::io;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};

use windows_sys::Win32::Foundation::{CloseHandle, FILETIME, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::NetworkManagement::IpHelper::{
    GetExtendedTcpTable, MIB_TCP6ROW_OWNER_PID, MIB_TCPROW_OWNER_PID, TCP_TABLE_OWNER_PID_LISTENER,
};
use windows_sys::Win32::Networking::WinSock::{AF_INET, AF_INET6};
use windows_sys::Win32::Security::Cryptography::{
    BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, Thread32First, Thread32Next,
    PROCESSENTRY32W, TH32CS_SNAPPROCESS, TH32CS_SNAPTHREAD, THREADENTRY32,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Threading::{
    GetExitCodeProcess, GetProcessTimes, OpenProcess, OpenThread, QueryFullProcessImageNameW,
    ResumeThread, TerminateProcess, CREATE_NO_WINDOW, CREATE_SUSPENDED, PROCESS_NAME_WIN32,
    PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE, THREAD_SUSPEND_RESUME,
};

/// A Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: when the last handle closes (this app
/// exits, crashes or is killed), Windows kills every process in it.
pub struct HubJob {
    handle: HANDLE,
}

// SAFETY: a job handle is a kernel handle; every API used on it is thread-safe.
unsafe impl Send for HubJob {}
// SAFETY: as above; the handle is never mutated after creation.
unsafe impl Sync for HubJob {}

impl HubJob {
    #[cfg(feature = "portable")]
    pub fn contains_pid(&self, pid: u32) -> bool {
        // SAFETY: query-only process handle and valid job handle; always close our handle.
        unsafe {
            let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if process.is_null() { return false; }
            // An exited process whose handle is still open is not part of anything any more.
            if live_creation_time(process).is_none() {
                CloseHandle(process);
                return false;
            }
            let mut inside = 0;
            let ok = windows_sys::Win32::System::JobObjects::IsProcessInJob(process, self.handle, &mut inside);
            CloseHandle(process);
            ok != 0 && inside != 0
        }
    }

    pub fn new_kill_on_close() -> io::Result<Self> {
        // SAFETY: an anonymous job with default security; the handle is owned by the returned value.
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        let job = HubJob { handle };
        // SAFETY: an all-zero JOBOBJECT_EXTENDED_LIMIT_INFORMATION is valid; we set one flag.
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        // SAFETY: the pointer and size describe `info`.
        let ok = unsafe {
            SetInformationJobObject(
                job.handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(job)
    }

    /// Kill every process still in the job (the hub and anything it started).
    pub fn terminate(&self) {
        // SAFETY: valid job handle owned by self.
        unsafe {
            TerminateJobObject(self.handle, 1);
        }
    }
}

impl Drop for HubJob {
    fn drop(&mut self) {
        // SAFETY: closing our own handle; with KILL_ON_JOB_CLOSE this also ends what is left.
        unsafe {
            CloseHandle(self.handle);
        }
    }
}

/// Start `cmd` without a console window, suspended; put it in `job`; then let it run. A child the
/// hub (or the venv launcher) starts later is therefore always inside the job.
pub fn spawn_in_job(cmd: &mut Command, job: &HubJob) -> io::Result<Child> {
    cmd.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
    let mut child = cmd.spawn()?;
    let process = child.as_raw_handle() as HANDLE;
    // SAFETY: both handles are valid for the duration of the call.
    if unsafe { AssignProcessToJobObject(job.handle, process) } == 0 {
        let err = io::Error::last_os_error();
        let _ = child.kill();
        let _ = child.wait();
        return Err(err);
    }
    match resume_process_threads(child.id()) {
        Ok(n) if n > 0 => Ok(child),
        Ok(_) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(io::Error::other(
                "no thread of the new hub process could be resumed",
            ))
        }
        Err(e) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(e)
        }
    }
}

struct Snapshot(HANDLE);

impl Snapshot {
    fn new(flags: u32) -> io::Result<Self> {
        // SAFETY: a system-wide snapshot; the handle is owned by the returned value.
        let h = unsafe { CreateToolhelp32Snapshot(flags, 0) };
        if h == INVALID_HANDLE_VALUE || h.is_null() {
            return Err(io::Error::last_os_error());
        }
        Ok(Snapshot(h))
    }
}

impl Drop for Snapshot {
    fn drop(&mut self) {
        // SAFETY: our own snapshot handle.
        unsafe {
            CloseHandle(self.0);
        }
    }
}

fn resume_process_threads(pid: u32) -> io::Result<usize> {
    let snap = Snapshot::new(TH32CS_SNAPTHREAD)?;
    // SAFETY: THREADENTRY32 is plain data; dwSize is set as the API requires.
    let mut entry: THREADENTRY32 = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
    let mut resumed = 0;
    // SAFETY: snapshot handle and entry are valid; the loop follows the Thread32First/Next protocol.
    let mut more = unsafe { Thread32First(snap.0, &mut entry) } != 0;
    while more {
        if entry.th32OwnerProcessID == pid {
            // SAFETY: opening, resuming and closing a thread handle we own for this block.
            unsafe {
                let th = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                if !th.is_null() {
                    if ResumeThread(th) != u32::MAX {
                        resumed += 1;
                    }
                    CloseHandle(th);
                }
            }
        }
        // SAFETY: as above.
        more = unsafe { Thread32Next(snap.0, &mut entry) } != 0;
    }
    Ok(resumed)
}

#[derive(Debug, Clone)]
pub struct ProcessEntry {
    pub pid: u32,
    pub parent_pid: u32,
    pub exe: String,
}

pub fn list_processes() -> io::Result<Vec<ProcessEntry>> {
    let snap = Snapshot::new(TH32CS_SNAPPROCESS)?;
    // SAFETY: PROCESSENTRY32W is plain data; dwSize is set as the API requires.
    let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    let mut out = Vec::new();
    // SAFETY: valid snapshot and entry, standard iteration protocol.
    let mut more = unsafe { Process32FirstW(snap.0, &mut entry) } != 0;
    while more {
        let len = entry
            .szExeFile
            .iter()
            .position(|&c| c == 0)
            .unwrap_or(entry.szExeFile.len());
        out.push(ProcessEntry {
            pid: entry.th32ProcessID,
            parent_pid: entry.th32ParentProcessID,
            exe: String::from_utf16_lossy(&entry.szExeFile[..len]),
        });
        // SAFETY: as above.
        more = unsafe { Process32NextW(snap.0, &mut entry) } != 0;
    }
    Ok(out)
}

pub fn process_name(pid: u32) -> Option<String> {
    list_processes()
        .ok()?
        .into_iter()
        .find(|p| p.pid == pid)
        .map(|p| p.exe)
}

/// A process that exited with this code looks running to `GetExitCodeProcess`: the final code is
/// indistinguishable from "still active" there, so only the exit FILETIME can settle it.
const STILL_ACTIVE_CODE: u32 = 259;

/// The creation time of the process behind `handle`, or `None` once it has exited.
///
/// Windows keeps a process object alive while any handle to it is open, and that zombie still
/// answers `GetProcessTimes` with the original creation time and a non-zero `exited` FILETIME.
/// The exit FILETIME is therefore the authoritative "gone" signal; a final exit code other than
/// `STILL_ACTIVE` corroborates it, but cannot decide alone: a process may exit *with* 259.
fn live_creation_time(handle: HANDLE) -> Option<u64> {
    // SAFETY: `handle` carries PROCESS_QUERY_LIMITED_INFORMATION and is owned by the caller.
    unsafe {
        let zero = FILETIME {
            dwLowDateTime: 0,
            dwHighDateTime: 0,
        };
        let (mut created, mut exited, mut kernel, mut user) = (zero, zero, zero, zero);
        if GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user) == 0 {
            return None;
        }
        if exited.dwLowDateTime != 0 || exited.dwHighDateTime != 0 {
            return None;
        }
        let mut exit_code = 0u32;
        if GetExitCodeProcess(handle, &mut exit_code) != 0 && exit_code != STILL_ACTIVE_CODE {
            return None;
        }
        Some(((created.dwHighDateTime as u64) << 32) | created.dwLowDateTime as u64)
    }
}

/// The creation time of `pid`, in Windows FILETIME ticks (100 ns since 1601), or `None` when the
/// process is gone, has exited (even if an open handle keeps its zombie object around), or cannot
/// be queried. Two queries that return the same value describe the same process instance: Windows
/// does reuse PIDs, but never with the same creation time.
pub fn creation_time(pid: u32) -> Option<u64> {
    // SAFETY: query-only handle, closed before returning.
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return None;
        }
        let created = live_creation_time(h);
        CloseHandle(h);
        created
    }
}

/// A process instance: the PID plus the creation time that distinguishes it from a later process
/// that happens to get the same PID.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub creation_time: u64,
}

/// `None` when `pid` is not running now.
pub fn process_identity(pid: u32) -> Option<ProcessIdentity> {
    creation_time(pid).map(|creation_time| ProcessIdentity { pid, creation_time })
}

/// Whether the exact process instance `identity` still exists.
pub fn process_is_alive(identity: ProcessIdentity) -> bool {
    creation_time(identity.pid) == Some(identity.creation_time)
}

#[cfg(any(feature = "portable", test))]
#[repr(C)]
struct UnicodeString {
    length: u16,
    maximum_length: u16,
    buffer: *mut u16,
}

// `PROCESSINFOCLASS` 60 (`ProcessCommandLineInformation`) has no kernel32 wrapper. With
// `PROCESS_QUERY_LIMITED_INFORMATION` it returns a UNICODE_STRING followed by the UTF-16 command
// line inside the caller's buffer.
#[cfg(any(feature = "portable", test))]
#[link(name = "ntdll")]
extern "system" {
    fn NtQueryInformationProcess(
        process_handle: HANDLE,
        process_information_class: u32,
        process_information: *mut core::ffi::c_void,
        process_information_length: u32,
        return_length: *mut u32,
    ) -> i32;
}

#[cfg(any(feature = "portable", test))]
const PROCESS_COMMAND_LINE_INFORMATION: u32 = 60;

/// The full command line of `pid` (UTF-16 decoded lossily), or `None` for a protected, exited or
/// inaccessible process. Used only to tell our own hub processes from unrelated ones.
#[cfg(any(feature = "portable", test))]
pub fn process_command_line(pid: u32) -> Option<String> {
    // SAFETY: every pointer describes a live local buffer; the process handle is closed here.
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return None;
        }
        if live_creation_time(handle).is_none() {
            CloseHandle(handle);
            return None;
        }
        let mut needed = 0u32;
        // Any failure that reports the required size is usable; the second call is authoritative.
        NtQueryInformationProcess(
            handle,
            PROCESS_COMMAND_LINE_INFORMATION,
            std::ptr::null_mut(),
            0,
            &mut needed,
        );
        if needed == 0 {
            CloseHandle(handle);
            return None;
        }
        // Two trailing bytes keep a NUL terminator available even if the API omits one.
        let mut buffer = vec![0u8; needed as usize + 2];
        let mut written = 0u32;
        let status = NtQueryInformationProcess(
            handle,
            PROCESS_COMMAND_LINE_INFORMATION,
            buffer.as_mut_ptr() as *mut core::ffi::c_void,
            needed,
            &mut written,
        );
        CloseHandle(handle);
        if status != 0 {
            return None;
        }
        let header = std::ptr::read_unaligned(buffer.as_ptr() as *const UnicodeString);
        let chars = header.length as usize / 2;
        if header.buffer.is_null() || chars == 0 {
            return None;
        }
        let start = buffer.as_ptr() as usize;
        let text = header.buffer as usize;
        if text < start || text + chars * 2 > start + buffer.len() {
            return None;
        }
        Some(String::from_utf16_lossy(std::slice::from_raw_parts(
            header.buffer,
            chars,
        )))
    }
}

/// The full image path of `pid`, or `None` when the process is gone or inaccessible.
pub fn process_image_path(pid: u32) -> Option<PathBuf> {
    // SAFETY: query-only handle; the wide buffer is sized by the API's required-length protocol.
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return None;
        }
        if live_creation_time(handle).is_none() {
            CloseHandle(handle);
            return None;
        }
        let mut size = 512u32;
        loop {
            let mut buffer = vec![0u16; size as usize];
            let mut len = size;
            if QueryFullProcessImageNameW(handle, PROCESS_NAME_WIN32, buffer.as_mut_ptr(), &mut len)
                != 0
            {
                CloseHandle(handle);
                return Some(PathBuf::from(String::from_utf16_lossy(
                    &buffer[..len as usize],
                )));
            }
            // ERROR_INSUFFICIENT_BUFFER: double the buffer within a sane bound.
            if io::Error::last_os_error().raw_os_error() != Some(122) || size >= 32_768 {
                CloseHandle(handle);
                return None;
            }
            size *= 2;
        }
    }
}

/// Case-insensitive, verbatim-prefix-free form of an image path, so a path from `current_exe()`
/// and one from `QueryFullProcessImageNameW` compare equal.
fn normalize_image(path: &Path) -> String {
    let text = path.to_string_lossy().replace('/', "\\").to_lowercase();
    text.strip_prefix("\\\\?\\")
        .map(str::to_string)
        .unwrap_or(text)
}

/// The first running process whose image path is exactly `exe`. Used by the upgrade helper to tell
/// whether the app it just launched is alive; the desktop shell's own process therefore never
/// matches an app it did not start.
pub fn find_process_by_image(exe: &Path) -> Option<u32> {
    let wanted = normalize_image(exe);
    let name = exe.file_name()?.to_string_lossy().to_lowercase();
    for entry in list_processes().ok()? {
        if entry.exe.to_lowercase() != name {
            continue;
        }
        if let Some(image) = process_image_path(entry.pid) {
            if normalize_image(&image) == wanted {
                return Some(entry.pid);
            }
        }
    }
    None
}

/// `root` and every descendant, root first. A process only counts as a child if it was created
/// after its parent, so a reused parent PID never pulls in an unrelated process.
pub fn process_tree(
    root: u32,
    procs: &[ProcessEntry],
    created: &dyn Fn(u32) -> Option<u64>,
) -> Vec<u32> {
    let mut children: HashMap<u32, Vec<u32>> = HashMap::new();
    for p in procs {
        if p.pid != p.parent_pid {
            children.entry(p.parent_pid).or_default().push(p.pid);
        }
    }
    let mut out = vec![root];
    let mut seen: HashSet<u32> = HashSet::from([root]);
    let mut i = 0;
    while i < out.len() {
        let parent = out[i];
        let parent_created = created(parent);
        for &child in children.get(&parent).map(Vec::as_slice).unwrap_or(&[]) {
            if seen.contains(&child) {
                continue;
            }
            let newer = match (parent_created, created(child)) {
                (Some(p), Some(c)) => c >= p,
                _ => false,
            };
            if newer {
                seen.insert(child);
                out.push(child);
            }
        }
        i += 1;
    }
    out
}

/// `root` and its live descendants right now (root first). Test/lived evidence only; runtime kills
/// use identities so a reused PID between the snapshot and the kill is skipped.
#[cfg(test)]
pub fn live_process_tree(root: u32) -> io::Result<Vec<ProcessEntry>> {
    let procs = list_processes()?;
    let tree = process_tree(root, &procs, &creation_time);
    Ok(tree
        .into_iter()
        .filter_map(|pid| procs.iter().find(|p| p.pid == pid).cloned())
        .collect())
}

/// Identities of `root` and its live descendants. Capture this before a graceful stop: a child the
/// hub spawned can outlive (and be reparented after) its parent, so PID ancestry is no longer
/// enough once the root has exited.
pub fn live_process_identities(root: u32) -> io::Result<Vec<ProcessIdentity>> {
    let procs = list_processes()?;
    let tree = process_tree(root, &procs, &creation_time);
    Ok(tree.into_iter().filter_map(process_identity).collect())
}

/// Terminate exactly the process instances in `identities` that still exist. A PID that was
/// reused by an unrelated process is skipped, not killed.
pub fn kill_identities(identities: &[ProcessIdentity]) -> usize {
    let mut killed = 0;
    for identity in identities {
        if !process_is_alive(*identity) {
            continue;
        }
        // SAFETY: terminate-only handle, closed right after use.
        unsafe {
            let h = OpenProcess(PROCESS_TERMINATE, 0, identity.pid);
            if !h.is_null() {
                if TerminateProcess(h, 1) != 0 {
                    killed += 1;
                }
                CloseHandle(h);
            }
        }
    }
    killed
}

/// Kill `root` and its process tree. Returns how many processes were terminated.
pub fn kill_process_tree(root: u32) -> io::Result<usize> {
    Ok(kill_identities(&live_process_identities(root)?))
}

fn port_from_table(dw_local_port: u32) -> u16 {
    // The low 16 bits hold the port in network byte order.
    u16::from_be((dw_local_port & 0xFFFF) as u16)
}

fn tcp_table(family: u16) -> Option<Vec<u8>> {
    let mut size: u32 = 0;
    // SAFETY: first call with a null buffer only reports the size needed.
    unsafe {
        GetExtendedTcpTable(
            std::ptr::null_mut(),
            &mut size,
            0,
            family as u32,
            TCP_TABLE_OWNER_PID_LISTENER,
            0,
        );
    }
    for _ in 0..4 {
        let mut buf = vec![0u8; size as usize + 4096];
        let mut len = buf.len() as u32;
        // SAFETY: buffer pointer/length describe `buf`.
        let rc = unsafe {
            GetExtendedTcpTable(
                buf.as_mut_ptr() as *mut core::ffi::c_void,
                &mut len,
                0,
                family as u32,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            )
        };
        if rc == 0 {
            buf.truncate(len as usize);
            return Some(buf);
        }
        size = len;
    }
    None
}

fn listeners<Row>(family: u16, port_of: impl Fn(&Row) -> (u32, u32)) -> Vec<(u16, u32)> {
    let Some(buf) = tcp_table(family) else {
        return Vec::new();
    };
    if buf.len() < 4 {
        return Vec::new();
    }
    let count = u32::from_ne_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
    // The rows follow dwNumEntries, aligned to the row type (both row types are 4-byte aligned).
    let rows_offset = 4;
    let row_size = std::mem::size_of::<Row>();
    let mut out = Vec::with_capacity(count);
    for i in 0..count {
        let off = rows_offset + i * row_size;
        if off + row_size > buf.len() {
            break;
        }
        // SAFETY: bounds checked above; read_unaligned tolerates the Vec<u8> alignment.
        let row: Row = unsafe { std::ptr::read_unaligned(buf.as_ptr().add(off) as *const Row) };
        let (local_port, pid) = port_of(&row);
        out.push((port_from_table(local_port), pid));
    }
    out
}

/// The PID of the process listening on TCP `port` (IPv4 or IPv6), if any.
pub fn listening_pid(port: u16) -> Option<u32> {
    let v4 = listeners::<MIB_TCPROW_OWNER_PID>(AF_INET, |r| (r.dwLocalPort, r.dwOwningPid));
    if let Some((_, pid)) = v4.into_iter().find(|(p, _)| *p == port) {
        return Some(pid);
    }
    let v6 = listeners::<MIB_TCP6ROW_OWNER_PID>(AF_INET6, |r| (r.dwLocalPort, r.dwOwningPid));
    v6.into_iter().find(|(p, _)| *p == port).map(|(_, pid)| pid)
}

/// 32 random bytes from the system RNG, hex encoded.
pub fn random_token() -> io::Result<String> {
    let mut buf = [0u8; 32];
    // SAFETY: buffer pointer/length describe `buf`; the system-preferred RNG needs no algorithm handle.
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            buf.as_mut_ptr(),
            buf.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status != 0 {
        return Err(io::Error::other(format!(
            "BCryptGenRandom failed: 0x{status:08x}"
        )));
    }
    Ok(buf.iter().map(|b| format!("{b:02x}")).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    fn entry(pid: u32, parent_pid: u32) -> ProcessEntry {
        ProcessEntry {
            pid,
            parent_pid,
            exe: format!("p{pid}.exe"),
        }
    }

    #[test]
    fn tree_walks_descendants_root_first() {
        let procs = vec![
            entry(10, 1),
            entry(11, 10),
            entry(12, 11),
            entry(13, 10),
            entry(20, 1),
        ];
        let created = |pid: u32| Some(pid as u64);
        assert_eq!(process_tree(10, &procs, &created), vec![10, 11, 13, 12]);
    }

    #[test]
    fn tree_skips_children_older_than_their_parent() {
        // 11 names 10 as parent but was created before 10 existed: PID reuse, not a child.
        let procs = vec![entry(10, 1), entry(11, 10), entry(12, 10)];
        let created = |pid: u32| {
            Some(match pid {
                10 => 100,
                11 => 50,
                _ => 150,
            })
        };
        assert_eq!(process_tree(10, &procs, &created), vec![10, 12]);
    }

    #[test]
    fn port_byte_order() {
        // 4465 = 0x1171, stored big-endian in the low word.
        let stored = u32::from(u16::from_ne_bytes(4465u16.to_be_bytes()));
        assert_eq!(port_from_table(stored), 4465);
    }

    #[test]
    fn listening_pid_finds_our_own_listener() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert_eq!(listening_pid(port), Some(std::process::id()));
        drop(listener);
        assert_eq!(listening_pid(port), None);
    }

    #[test]
    fn tokens_are_random_hex() {
        let a = random_token().unwrap();
        let b = random_token().unwrap();
        assert_eq!(a.len(), 64);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    fn identity_is_stable_for_this_process_and_rejects_a_reused_pid() {
        let pid = std::process::id();
        let identity = process_identity(pid).expect("our own process is queryable");
        assert_eq!(identity.pid, pid);
        assert_eq!(Some(identity.creation_time), creation_time(pid));
        assert!(process_is_alive(identity));
        let reused = ProcessIdentity {
            pid,
            creation_time: identity.creation_time + 1,
        };
        assert!(!process_is_alive(reused), "a different creation time is another process");
    }

    #[test]
    fn command_line_and_image_path_describe_this_process() {
        let pid = std::process::id();
        let command_line = process_command_line(pid).expect("our command line is readable");
        let image = process_image_path(pid).expect("our image path is readable");
        let current = std::env::current_exe().unwrap();
        let normalize = |value: &str| value.to_lowercase().replace("\\\\?\\", "");
        assert_eq!(normalize(&image.to_string_lossy()), normalize(&current.to_string_lossy()));
        let command_line = command_line.to_lowercase().replace('/', "\\");
        let image_name = image
            .file_name()
            .unwrap()
            .to_string_lossy()
            .to_lowercase();
        assert!(command_line.contains(&image_name), "{command_line}");
    }

    /// A child that blocks on its piped stdin: guaranteed to be running when the test reads its
    /// identity, and gone as soon as the pipe is closed.
    fn spawn_stdin_blocked_child() -> Child {
        Command::new("cmd.exe")
            .args(["/C", "more >nul"])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .expect("spawn a stdin-blocked child")
    }

    /// Reap the child's exit *without* dropping it: the `Child` keeps its process handle open,
    /// which is exactly the zombie state production hit after the app had already exited.
    fn wait_keeping_handle(child: &mut Child) {
        let deadline = Instant::now() + Duration::from_secs(60);
        while child.try_wait().expect("try_wait").is_none() {
            assert!(Instant::now() < deadline, "the child did not exit");
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    fn assert_zombie_handle_is_open(pid: u32) {
        // SAFETY: query-only probe; a non-null handle is closed right away.
        let probe = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        assert!(!probe.is_null(), "the exited child must still have an open process object");
        unsafe { CloseHandle(probe) };
    }

    #[test]
    fn an_exited_child_with_an_open_handle_is_not_alive() {
        let mut child = spawn_stdin_blocked_child();
        let pid = child.id();
        let identity = process_identity(pid).expect("a running child has an identity");
        drop(child.stdin.take()); // closing the pipe lets `more` exit
        wait_keeping_handle(&mut child);
        assert_zombie_handle_is_open(pid);
        assert_eq!(creation_time(pid), None, "a zombie handle is not a running process");
        assert!(!process_is_alive(identity));
        assert_eq!(process_image_path(pid), None);
        assert_eq!(process_command_line(pid), None);
        drop(child);
    }

    #[test]
    fn an_exit_code_of_still_active_is_still_dead() {
        let mut child = Command::new("cmd.exe")
            .args(["/C", "exit 259"])
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .expect("spawn a child that exits with 259");
        let pid = child.id();
        wait_keeping_handle(&mut child);
        assert_zombie_handle_is_open(pid);
        assert_eq!(creation_time(pid), None, "the exit FILETIME, not the code, decides");
        drop(child);
    }
}
