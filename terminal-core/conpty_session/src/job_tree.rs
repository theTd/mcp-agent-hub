//! Process-tree queries and the tree-kill fallback (§12.8): when a session's job object could not be
//! assigned (an outer job refused the nesting), `kill` walks the children with ToolHelp and terminates
//! them, which is what `taskkill /T /F` did for the hub before the core owned the PTY.

use windows::Win32::Foundation::HANDLE;
use windows::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
};
use windows::Win32::System::Threading::{
    GetCurrentProcess, GetExitCodeProcess, GetProcessHandleCount, OpenProcess, TerminateProcess,
    PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
};

use crate::handles::OwnedHandle;

/// Live `(pid, parent pid)` pairs from one snapshot; the exit code is not consulted here.
fn process_snapshot() -> Vec<(u32, u32)> {
    // SAFETY: a system-wide process snapshot; the handle is closed by the wrapper.
    let snapshot = match unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) } {
        Ok(handle) => OwnedHandle::new(handle),
        Err(_) => return Vec::new(),
    };
    // SAFETY: PROCESSENTRY32W is a plain-old-data struct; dwSize must be set before the first call.
    let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    let mut pairs = Vec::new();
    // SAFETY: `entry` is a valid out pointer with dwSize set.
    if unsafe { Process32FirstW(snapshot.get(), &mut entry) }.is_err() {
        return pairs;
    }
    loop {
        pairs.push((entry.th32ProcessID, entry.th32ParentProcessID));
        // SAFETY: same struct, same size, as Process32FirstW requires.
        if unsafe { Process32NextW(snapshot.get(), &mut entry) }.is_err() {
            break;
        }
    }
    pairs
}

/// Direct children of `parent`.
pub fn children_of(parent: u32) -> Vec<u32> {
    process_snapshot()
        .into_iter()
        .filter(|(pid, parent_pid)| *parent_pid == parent && *pid != 0)
        .map(|(pid, _)| pid)
        .collect()
}

/// `parent` plus every descendant, parents before their children.
pub fn tree_of(root: u32) -> Vec<u32> {
    let pairs = process_snapshot();
    let mut tree = vec![root];
    let mut index = 0;
    while index < tree.len() {
        let current = tree[index];
        index += 1;
        for (pid, parent) in &pairs {
            if *parent == current && *pid != current && !tree.contains(pid) {
                tree.push(*pid);
            }
        }
    }
    tree
}

/// Running processes whose executable name matches `name` (case-insensitive). The leak tests count
/// `conhost.exe` with it: a pseudoconsole that is never closed keeps its conhost alive.
pub fn count_processes_named(name: &str) -> usize {
    let wanted = name.to_ascii_lowercase();
    process_snapshot_entries()
        .into_iter()
        .filter(|(_, _, exe)| exe.eq_ignore_ascii_case(&wanted))
        .count()
}

/// Running processes named `name` whose parent is `parent`: `conhost.exe` is created by the process that
/// opened the pseudoconsole, so this counts *our* consoles and ignores another app's (or another
/// session's) console starting at the same moment.
pub fn count_processes_named_by_parent(parent: u32, name: &str) -> usize {
    let wanted = name.to_ascii_lowercase();
    process_snapshot_entries()
        .into_iter()
        .filter(|(pid, parent_pid, exe)| {
            *pid != 0 && *parent_pid == parent && exe.eq_ignore_ascii_case(&wanted)
        })
        .count()
}

/// `(pid, parent pid, executable name)` for every process in one snapshot.
fn process_snapshot_entries() -> Vec<(u32, u32, String)> {
    // SAFETY: a system-wide process snapshot; the handle is closed by the wrapper.
    let snapshot = match unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) } {
        Ok(handle) => OwnedHandle::new(handle),
        Err(_) => return Vec::new(),
    };
    // SAFETY: PROCESSENTRY32W is a plain-old-data struct; dwSize must be set before the first call.
    let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    let mut entries = Vec::new();
    // SAFETY: `entry` is a valid out pointer with dwSize set.
    if unsafe { Process32FirstW(snapshot.get(), &mut entry) }.is_err() {
        return entries;
    }
    loop {
        let end = entry
            .szExeFile
            .iter()
            .position(|unit| *unit == 0)
            .unwrap_or(entry.szExeFile.len());
        entries.push((
            entry.th32ProcessID,
            entry.th32ParentProcessID,
            String::from_utf16_lossy(&entry.szExeFile[..end]),
        ));
        // SAFETY: same struct, same size, as Process32FirstW requires.
        if unsafe { Process32NextW(snapshot.get(), &mut entry) }.is_err() {
            break;
        }
    }
    entries
}

/// Open handles of this process. A failed spawn must not raise it: the error path closed the console,
/// both pipe ends and the process handle it created (should-fix 1).
pub fn current_handle_count() -> u32 {
    let mut count = 0u32;
    // SAFETY: GetCurrentProcess returns a borrowed pseudo handle (never closed) and `count` is a valid
    // out pointer.
    let ok = unsafe { GetProcessHandleCount(GetCurrentProcess(), &mut count) }.is_ok();
    if ok {
        count
    } else {
        0
    }
}

/// Opens a process with the rights a liveness check needs.
fn open_for_query(pid: u32) -> Option<OwnedHandle> {
    if pid == 0 {
        return None;
    }
    // SAFETY: no inheritance, the returned handle is owned by the wrapper.
    unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) }
        .ok()
        .map(|handle| OwnedHandle::new(handle as HANDLE))
}

/// True while the process exists and has not exited (B9: liveness is the process, never a status).
pub fn process_alive(pid: u32) -> bool {
    const STILL_ACTIVE: u32 = 259;
    let Some(process) = open_for_query(pid) else {
        return false;
    };
    let mut code = 0u32;
    // SAFETY: the handle is open for query and `code` is a valid out pointer.
    match unsafe { GetExitCodeProcess(process.get(), &mut code) } {
        Ok(()) => code == STILL_ACTIVE,
        Err(_) => false,
    }
}

/// Force-terminates every process in `root`'s tree; parents first so a dying parent cannot respawn a
/// child in the window between the walk and the kill. Returns the pids it reached.
pub fn terminate_tree(root: u32) -> Vec<u32> {
    let mut killed = Vec::new();
    for pid in tree_of(root) {
        if pid == 0 {
            continue;
        }
        // SAFETY: no inheritance; the handle is closed by the wrapper after the call.
        if let Ok(handle) = unsafe { OpenProcess(PROCESS_TERMINATE, false, pid) } {
            let process = OwnedHandle::new(handle);
            // SAFETY: the handle has PROCESS_TERMINATE and is closed below by `OwnedHandle`.
            if unsafe { TerminateProcess(process.get(), 1) }.is_ok() {
                killed.push(pid);
            }
        }
    }
    killed
}
