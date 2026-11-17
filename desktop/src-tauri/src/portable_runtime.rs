//! Portable source payload and cancellable environment preparation. Python itself is never embedded.
use crate::{
    hub_api::HubApi,
    hub_config::{self, HubConfig, LaunchRecord, LaunchRecordOwner},
    hub_log,
    windows_process::{self, HubJob, ProcessIdentity},
};
use serde::Deserialize;
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicBool, AtomicU64, Ordering},
    time::{Duration, Instant, SystemTime},
};

include!(concat!(env!("OUT_DIR"), "/portable_resources.rs"));


const STAGING_PREFIX: &str = ".staging-";
const INCOMPLETE_PREFIX: &str = ".incomplete-";
const STALE_STAGING_AGE: Duration = Duration::from_secs(60 * 60);
const ORPHAN_STOP_WAIT: Duration = Duration::from_secs(10);
const PORT_RELEASE_WAIT: Duration = Duration::from_secs(10);

static EXTRACT_SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// A process verified to belong to this portable installation (its command line names the exact
/// payload path, or it is the process the OS says listens on our port). The shell shows these when
/// recovery cannot proceed; they are evidence, never a list of PIDs to kill blindly.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct ProcessHolder {
    pub pid: u32,
    pub creation_time: u64,
    pub name: String,
    pub command_line: String,
}

/// What `recover_stale_hub` observed and did before a new start. `live_peer` names another copy of
/// this EXE that is already running; its hub must not be touched.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct RecoveryReport {
    pub live_peer: Option<u32>,
    pub stopped: Option<ProcessHolder>,
}

/// A portable start failure that the shell can render without re-deriving anything. A payload
/// lock carries the verified holders of the directory; other failures just carry the message.
#[derive(Debug, Clone)]
pub struct PortableError {
    message: String,
    holders: Vec<ProcessHolder>,
}

impl PortableError {
    pub fn new(message: impl Into<String>) -> Self {
        PortableError {
            message: message.into(),
            holders: Vec::new(),
        }
    }

    pub fn with_holders(message: impl Into<String>, holders: Vec<ProcessHolder>) -> Self {
        PortableError {
            message: message.into(),
            holders,
        }
    }

    pub fn holders(&self) -> &[ProcessHolder] {
        &self.holders
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}

impl From<String> for PortableError {
    fn from(message: String) -> Self {
        PortableError::new(message)
    }
}

impl From<&str> for PortableError {
    fn from(message: &str) -> Self {
        PortableError::new(message)
    }
}

impl std::fmt::Display for PortableError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for PortableError {}

fn holder_for(pid: u32, name: Option<String>) -> Option<ProcessHolder> {
    let identity = windows_process::process_identity(pid)?;
    Some(ProcessHolder {
        pid,
        creation_time: identity.creation_time,
        name: name.unwrap_or_default(),
        // A protected or exiting process has no readable command line; it is still the verified
        // listener/holder, so keep the row and let the caller decide.
        command_line: windows_process::process_command_line(pid).unwrap_or_default(),
    })
}

fn holder(pid: u32) -> Option<ProcessHolder> {
    let name = windows_process::process_name(pid);
    holder_for(pid, name)
}

fn describe_holders(holders: &[ProcessHolder]) -> String {
    if holders.is_empty() {
        return "未找到可验证的占用者；目录可能仍被某个进程以句柄或工作目录方式占用".to_string();
    }
    holders
        .iter()
        .map(|holder| format!("PID {}（{}）", holder.pid, holder.name))
        .collect::<Vec<_>>()
        .join("、")
}

/// Verified payload holders: live processes whose command line names `path` (the extracted payload
/// directory, or one of its files). Sorted by PID, deduplicated.
pub fn payload_holders(path: &Path) -> Vec<ProcessHolder> {
    let needle = path_key(path);
    if needle.is_empty() {
        return Vec::new();
    }
    let needle = format!("{needle}\\");
    let mut holders: Vec<ProcessHolder> = windows_process::list_processes()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|process| {
            let holder = holder_for(process.pid, Some(process.exe.clone()))?;
            holder
                .command_line
                .replace('/', "\\")
                .to_lowercase()
                .contains(&needle)
                .then_some(holder)
        })
        .collect();
    holders.sort_by_key(|holder| holder.pid);
    holders.dedup_by_key(|holder| holder.pid);
    holders
}

/// The process the OS reports listening on `port`, with its command line when readable.
pub fn port_holders(port: u16) -> Vec<ProcessHolder> {
    windows_process::listening_pid(port)
        .and_then(holder)
        .into_iter()
        .collect()
}

fn path_key(path: &Path) -> String {
    let text = path.to_string_lossy().replace('/', "\\");
    text.trim_start_matches("\\\\?\\")
        .trim_end_matches('\\')
        .to_lowercase()
}

fn is_within(path: &Path, dir: &Path) -> bool {
    let path = path_key(path);
    let dir = path_key(dir);
    path == dir || path.starts_with(&(dir + "\\"))
}

/// Compare paths the way Windows does for our own files: case-insensitive, either separator, and
/// with or without the `\\?\` prefix.
fn match_key(value: &str) -> String {
    value
        .replace('/', "\\")
        .trim_start_matches("\\\\?\\")
        .to_lowercase()
}

/// A payload directory is complete only when every embedded file matches its embedded bytes. The
/// directory name is the content hash of those files (`PAYLOAD_ID`), so a published directory is
/// immutable and is never repaired in place while a Hub may still be serving it.
fn payload_complete(dest: &Path) -> bool {
    FILES.iter().all(|(name, bytes)| {
        fs::read(dest.join(name)).is_ok_and(|actual| actual == *bytes)
    })
}

fn write_payload_file(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut file = fs::File::create(path).map_err(|e| format!("无法展开程序资源：{e}"))?;
    file.write_all(bytes)
        .and_then(|()| file.sync_all())
        .map_err(|e| format!("无法展开程序资源：{e}"))
}

fn stage_path(resources: &Path) -> PathBuf {
    let sequence = EXTRACT_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    resources.join(format!(
        "{STAGING_PREFIX}{PAYLOAD_ID}-{}-{sequence}",
        std::process::id()
    ))
}

fn aside_path(resources: &Path) -> PathBuf {
    let sequence = EXTRACT_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    resources.join(format!(
        "{INCOMPLETE_PREFIX}{PAYLOAD_ID}-{}-{sequence}",
        std::process::id()
    ))
}

/// Best-effort removal of staging directories left by a crash. Only old ones are touched: a live
/// extraction in another thread or process must keep its work.
fn cleanup_stale_staging(resources: &Path) {
    let Ok(entries) = fs::read_dir(resources) else {
        return;
    };
    let now = SystemTime::now();
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !name.starts_with(STAGING_PREFIX) && !name.starts_with(INCOMPLETE_PREFIX) {
            continue;
        }
        let old = entry
            .metadata()
            .and_then(|meta| meta.modified())
            .ok()
            .and_then(|modified| now.duration_since(modified).ok())
            .is_some_and(|age| age > STALE_STAGING_AGE);
        if old {
            let _ = fs::remove_dir_all(entry.path());
        }
    }
}

fn payload_lock_error(dest: &Path, detail: impl std::fmt::Display) -> PortableError {
    let holders = payload_holders(dest);
    PortableError::with_holders(
        format!(
            "无法展开程序资源：{} 被占用（{detail}）。{}",
            dest.display(),
            describe_holders(&holders)
        ),
        holders,
    )
}

fn unpack(root: &Path) -> Result<PathBuf, PortableError> {
    let resources = root.join("resources");
    fs::create_dir_all(&resources).map_err(|e| e.to_string())?;
    let dest = resources.join(PAYLOAD_ID);
    // Published payloads are never rewritten: a stale hub may still serve this exact directory.
    if payload_complete(&dest) {
        return Ok(dest);
    }
    cleanup_stale_staging(&resources);
    let staging = stage_path(&resources);
    let _ = fs::remove_dir_all(&staging);
    for (name, bytes) in FILES {
        let path = staging.join(name);
        fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
        write_payload_file(&path, bytes)?;
    }
    if !payload_complete(&staging) {
        let _ = fs::remove_dir_all(&staging);
        return Err(PortableError::new("无法展开程序资源：展开后的文件不完整"));
    }
    // The rename publishes a complete directory in one step; a crash leaves only a staging dir.
    match fs::rename(&staging, &dest) {
        Ok(()) => Ok(dest),
        Err(error) => {
            if payload_complete(&dest) {
                let _ = fs::remove_dir_all(&staging);
                return Ok(dest);
            }
            let aside = aside_path(&resources);
            match fs::rename(&dest, &aside) {
                Ok(()) => match fs::rename(&staging, &dest) {
                    Ok(()) => {
                        let _ = fs::remove_dir_all(&aside);
                        Ok(dest)
                    }
                    Err(replace_error) => {
                        let _ = fs::remove_dir_all(&staging);
                        let _ = fs::rename(&aside, &dest);
                        Err(payload_lock_error(&dest, replace_error))
                    }
                },
                Err(move_error) => {
                    let _ = fs::remove_dir_all(&staging);
                    Err(payload_lock_error(
                        &dest,
                        format!("{move_error}；{error}"),
                    ))
                }
            }
        }
    }
}

/// True when this command line is our python running `server.py` from `repo_dir`. Used to identify
/// an orphan before stopping it: an unrelated python must never be killed.
fn hub_command_line_matches(command_line: &str, python: &Path, repo_dir: &Path) -> bool {
    let line = match_key(command_line);
    let python = match_key(&python.to_string_lossy());
    let server = match_key(&repo_dir.join("server.py").to_string_lossy());
    !python.is_empty() && !server.is_empty() && line.contains(&python) && line.contains(&server)
}

fn command_line_is_hub(pid: u32, record: &LaunchRecord) -> bool {
    windows_process::process_command_line(pid)
        .is_some_and(|line| hub_command_line_matches(&line, &record.python, &record.repo_dir))
}

/// Walk up while the parent is also our hub process, so a venv launcher and its python child are
/// stopped as one tree and no parent is left holding the payload directory.
fn hub_tree_root(pid: u32, record: &LaunchRecord) -> u32 {
    let Ok(processes) = windows_process::list_processes() else {
        return pid;
    };
    let mut current = pid;
    for _ in 0..32 {
        let Some(entry) = processes.iter().find(|process| process.pid == current) else {
            break;
        };
        let parent = entry.parent_pid;
        if parent == 0 || parent == current || !command_line_is_hub(parent, record) {
            break;
        }
        current = parent;
    }
    current
}

async fn wait_identity_gone(identity: ProcessIdentity, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while windows_process::process_is_alive(identity) {
        if Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    true
}

async fn wait_port_free(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if windows_process::listening_pid(port).is_none() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// Stop a hub this portable installation started, gracefully when the record carries its shutdown
/// token, then by terminating every captured process instance. Fails closed when the processes or
/// the port survive: extraction must not overwrite a directory that is still in use.
async fn stop_hub(root_pid: u32, record: &LaunchRecord) -> Result<(), String> {
    let tree = windows_process::live_process_identities(root_pid).unwrap_or_default();
    let root_identity = tree
        .iter()
        .copied()
        .find(|identity| identity.pid == root_pid)
        .or_else(|| windows_process::process_identity(root_pid));
    if let Some(token) = record.shutdown_token.as_deref() {
        let _ = HubApi::new(record.port).shutdown(token).await;
        if let Some(identity) = root_identity {
            let _ = wait_identity_gone(identity, ORPHAN_STOP_WAIT).await;
        }
    }
    // Graceful shutdown may leave a child behind, and the root may already be gone (PID ancestry no
    // longer links the survivors). Every captured identity is checked on its own.
    let survivors: Vec<ProcessIdentity> = tree
        .into_iter()
        .filter(|identity| windows_process::process_is_alive(*identity))
        .collect();
    if !survivors.is_empty() {
        windows_process::kill_identities(&survivors);
    }
    // Anything spawned between the snapshot and now is still under the (possibly live) root.
    if windows_process::process_identity(root_pid).is_some() {
        let _ = windows_process::kill_process_tree(root_pid);
    }
    let deadline = Instant::now() + ORPHAN_STOP_WAIT;
    while windows_process::process_identity(root_pid).is_some()
        || survivors
            .iter()
            .any(|identity| windows_process::process_is_alive(*identity))
    {
        if Instant::now() >= deadline {
            return Err(format!(
                "残留 Hub 进程 PID {root_pid} 无法结束，请手动结束该进程后重试"
            ));
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    if !wait_port_free(record.port, PORT_RELEASE_WAIT).await {
        let owner = match windows_process::listening_pid(record.port) {
            Some(pid) => format!("PID {pid}"),
            None => "未知进程".to_string(),
        };
        return Err(format!(
            "残留 Hub 停止后端口 {} 仍被 {owner} 占用，便携版不会连接外部 Hub",
            record.port
        ));
    }
    Ok(())
}

/// Before extraction: load the record, and when it names a running hub that this payload started,
/// verify its command line, stop it, and report what was stopped. A live second copy of this EXE
/// is reported but never touched. Idempotent: a second call finds nothing to stop.
pub async fn recover_stale_hub(root: &Path) -> Result<RecoveryReport, PortableError> {
    let previous = hub_config::recover_launch_record(&root.join("launch.json"))
        .map_err(PortableError::from)?;
    let live_peer = live_app_peer();
    let mut report = RecoveryReport {
        live_peer,
        stopped: None,
    };
    if live_peer.is_some() {
        return Ok(report);
    }
    let Some(record) = previous else {
        return Ok(report);
    };
    if !is_within(&record.repo_dir, &root.join("resources")) {
        return Ok(report);
    }
    let pid = match hub_config::launch_record_owner(&record) {
        LaunchRecordOwner::Alive(identity) => {
            if !command_line_is_hub(identity.pid, &record) {
                // The record is inconsistent; never kill a process we cannot identify.
                return Ok(report);
            }
            identity.pid
        }
        LaunchRecordOwner::Unrecorded | LaunchRecordOwner::Stale => {
            let Some(pid) = windows_process::listening_pid(record.port) else {
                return Ok(report);
            };
            if !command_line_is_hub(pid, &record) {
                return Ok(report);
            }
            pid
        }
    };
    let root_pid = hub_tree_root(pid, &record);
    let stopped = holder(root_pid);
    stop_hub(root_pid, &record).await.map_err(|message| {
        let holders = payload_holders(&root.join("resources").join(PAYLOAD_ID));
        PortableError::with_holders(message, holders)
    })?;
    report.stopped = stopped;
    Ok(report)
}

/// Recovery for the app's own portable root, for the shell's "结束残留 Hub" action.
pub async fn recover_stale() -> Result<RecoveryReport, PortableError> {
    recover_stale_hub(&hub_config::portable_root()).await
}

/// Stop exactly the verified holders returned by `payload_holders`/`port_holders`, identity-checked
/// so a reused PID is skipped. This backs the shell's explicit "结束残留 Hub 并重试" action; the
/// automatic path only stops processes whose command line matches this payload.
pub async fn stop_holders(holders: &[ProcessHolder]) -> Result<usize, PortableError> {
    let identities: Vec<ProcessIdentity> = holders
        .iter()
        .filter_map(|holder| {
            let identity = ProcessIdentity {
                pid: holder.pid,
                creation_time: holder.creation_time,
            };
            windows_process::process_is_alive(identity).then_some(identity)
        })
        .collect();
    let killed = windows_process::kill_identities(&identities);
    let deadline = Instant::now() + ORPHAN_STOP_WAIT;
    while identities
        .iter()
        .any(|identity| windows_process::process_is_alive(*identity))
    {
        if Instant::now() >= deadline {
            return Err(PortableError::with_holders(
                "残留 Hub 进程无法结束，请以管理员身份结束这些进程后重试",
                holders.to_vec(),
            ));
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Ok(killed)
}

/// A second copy of this EXE may be a live app (which legitimately owns its hub) or a diagnostic
/// `--portable-prepare-only`. Its hub must not be killed and its launch record must not be
/// overwritten. Comparing the image path is stronger than the process name alone.
fn live_app_peer() -> Option<u32> {
    let exe = std::env::current_exe().ok()?;
    let exe_path = match_key(&exe.to_string_lossy());
    let exe_name = exe.file_name()?.to_string_lossy().to_lowercase();
    windows_process::list_processes()
        .ok()?
        .into_iter()
        .filter(|process| process.pid != std::process::id())
        .find(|process| {
            if windows_process::process_identity(process.pid).is_none() {
                return false;
            }
            // A parallel `--portable-prepare-only` is serialized by prepare.lock; it owns no hub.
            if windows_process::process_command_line(process.pid)
                .is_some_and(|line| line.to_lowercase().contains("--portable-prepare-only"))
            {
                return false;
            }
            match windows_process::process_image_path(process.pid) {
                Some(image) => match_key(&image.to_string_lossy()) == exe_path,
                None => process.exe.to_lowercase() == exe_name,
            }
        })
        .map(|process| process.pid)
}

/// The port chosen by `prepare` must still be ours to take when the supervisor launches the hub.
/// A payload-owned orphan is stopped; any other listener fails closed. Binding is checked too: a
/// listener check alone can lose the race.
async fn ensure_port_free(
    port: u16,
    python: &Path,
    repo_dir: &Path,
) -> Result<(), PortableError> {
    if let Some(pid) = windows_process::listening_pid(port) {
        let record = LaunchRecord::prepared(python.to_path_buf(), repo_dir.to_path_buf(), port);
        if command_line_is_hub(pid, &record) {
            let root_pid = hub_tree_root(pid, &record);
            stop_hub(root_pid, &record).await.map_err(PortableError::from)?;
        }
    }
    if windows_process::listening_pid(port).is_some() {
        let holders = port_holders(port);
        return Err(PortableError::with_holders(
            format!(
                "端口 {port} 已被{}占用，便携版不会连接外部 Hub",
                describe_holders(&holders)
            ),
            holders,
        ));
    }
    hub_config::require_free_port(port).map_err(PortableError::from)?;
    std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port))
        .map(|_| ())
        .map_err(|error| PortableError::new(format!("端口 {port} 当前不可绑定：{error}")))
}

#[derive(Deserialize)]
struct Progress {
    message: String,
    #[serde(default)]
    detail: String,
}
#[derive(Deserialize)]
struct Prepared {
    python: PathBuf,
}

fn localized_message(message: &str) -> &str {
    match message {
        "Checking Python environment" => "正在检查 Python 环境…",
        "Checking local Python" => "正在探测本机 Python…",
        "Creating isolated Hub environment" => "正在创建 Hub 专用环境…",
        "Downloading Hub dependencies" => "正在下载 Hub 依赖…",
        "Installing Hub dependencies" => "正在安装 Hub 依赖…",
        "Downloading Python runtime" => "本机无兼容 Python，正在下载运行时…",
        "Extracting verified Python runtime" => "正在展开已验证的 Python 运行时…",
        "Python environment ready" => "Python 环境已就绪",
        "Environment preparation failed" => "Python 环境准备失败",
        other => other,
    }
}

/// Compatibility wrapper: the shell's diagnostics log and error view take a string. The supervisor
/// uses `prepare_detailed` to get the verified holders of a locked payload as data.
pub async fn prepare(
    progress: impl Fn(&str, &str),
    cancel: &AtomicBool,
) -> Result<HubConfig, String> {
    prepare_detailed(progress, cancel)
        .await
        .map_err(|error| error.to_string())
}

pub async fn prepare_detailed(
    progress: impl Fn(&str, &str),
    cancel: &AtomicBool,
) -> Result<HubConfig, PortableError> {
    let root = hub_config::portable_root();
    fs::create_dir_all(&root).map_err(|e| e.to_string())?;
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(root.join("prepare.lock"))
        .map_err(|e| e.to_string())?;
    let lock_started = Instant::now();
    loop {
        match lock.try_lock() {
            Ok(()) => break,
            Err(std::fs::TryLockError::WouldBlock) => {
                if cancel.load(Ordering::SeqCst)
                    || lock_started.elapsed() > Duration::from_secs(1800)
                {
                    return Err("等待其他环境准备进程超时，可重试".into());
                }
                progress("正在等待 Python 环境准备…", "另一个进程正在使用环境缓存");
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
            Err(e) => return Err(e.to_string().into()),
        }
    }
    let launch_path = root.join("launch.json");
    // The recovery reads and, when needed, rewrites launch.json; it never touches a live peer.
    let recovery = recover_stale_hub(&root).await?;
    if recovery.live_peer.is_some()
        && !std::env::args().any(|arg| arg == "--portable-prepare-only")
    {
        return Err(PortableError::new(format!(
            "另一个便携版实例正在运行（PID {}），请使用该窗口，或先完全退出它",
            recovery.live_peer.unwrap()
        )));
    }
    let live_peer = recovery.live_peer;
    let file = hub_config::load_file()?;
    let resources = unpack(&root)?;
    let mut cfg = hub_config::resolve_portable(&file, &resources)?;
    let explicit_python = file.python.clone().filter(|v| !v.trim().is_empty());
    fs::create_dir_all(&root).map_err(|e| e.to_string())?;
    let progress_path = root.join("progress.json");
    let result_path = root.join("prepared.json");
    let _ = fs::remove_file(&result_path);
    let _ = fs::remove_file(&progress_path);
    let log_dir = hub_config::log_dir();
    fs::create_dir_all(&log_dir).map_err(|e| e.to_string())?;
    let log = hub_log::open_append(&log_dir.join("bootstrap.log")).map_err(|e| e.to_string())?;
    let stderr = log.try_clone().map_err(|e| e.to_string())?;
    // Use the same kill-on-close process wrapper as the Hub; shutdown also stops pip children.
    let job = HubJob::new_kill_on_close().map_err(|e| e.to_string())?;
    let powershell = std::env::var_os("SystemRoot")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\Windows"))
        .join("System32/WindowsPowerShell/v1.0/powershell.exe");
    let mut cmd = Command::new(powershell);
    cmd.args([
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    ])
    .arg(resources.join("bootstrap.ps1"))
    .arg("-ResourceDir")
    .arg(&resources)
    .arg("-RuntimeRoot")
    .arg(&root)
    .arg("-ProgressPath")
    .arg(&progress_path)
    .arg("-ResultPath")
    .arg(&result_path)
    .current_dir(&root)
    .env_remove("PSModulePath")
    .stdin(Stdio::null())
    .stdout(log)
    .stderr(stderr);
    if let Some(python) = explicit_python {
        cmd.arg("-PythonOverride").arg(python);
    }
    progress("正在检查 Python 环境…", "首次启动可能需要联网准备依赖");
    let mut child = windows_process::spawn_in_job(&mut cmd, &job).map_err(|e| e.to_string())?;
    let started = Instant::now();
    loop {
        if cancel.load(Ordering::SeqCst) || started.elapsed() > Duration::from_secs(1800) {
            job.terminate();
            let _ = child.wait();
            return Err("环境准备已取消或超时，可点击重试".into());
        }
        if let Ok(bytes) = fs::read(&progress_path) {
            if let Ok(state) = serde_json::from_slice::<Progress>(&bytes) {
                let detail = if state.detail.starts_with("Python itself is not downloaded") {
                    "正在使用本机 Python，不会下载额外解释器"
                } else {
                    &state.detail
                };
                progress(localized_message(&state.message), detail);
            }
        }
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(status) if !status.success() => {
                return Err(format!(
                    "Python 环境准备失败（{status}）。{}",
                    hub_log::tail_lines(&log_dir.join("bootstrap.log"), 12)
                )
                .into());
            }
            Some(_) => break,
            None => tokio::time::sleep(Duration::from_millis(250)).await,
        }
    }
    let result: Prepared =
        serde_json::from_slice(&fs::read(&result_path).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
    cfg.python = result.python;
    cfg.port = hub_config::select_port(&file)?;
    // Fail closed here: preparation can take minutes, so the port that was free when selected may
    // now belong to someone else. The supervisor's post-ready job check remains the final gate.
    ensure_port_free(cfg.port, &cfg.python, &cfg.repo_dir).await?;
    // A live peer's record names its own hub; never clobber it with this diagnostic/prepare run.
    if live_peer.is_none() {
        let record = LaunchRecord::prepared(cfg.python.clone(), cfg.repo_dir.clone(), cfg.port);
        hub_config::write_launch_record(&launch_path, &record)?;
    }
    Ok(cfg)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn resources_dir(root: &Path) -> PathBuf {
        root.join("resources")
    }

    fn staging_entries(root: &Path) -> Vec<String> {
        fs::read_dir(resources_dir(root))
            .unwrap()
            .filter_map(Result::ok)
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .filter(|name| name.starts_with(STAGING_PREFIX) || name.starts_with(INCOMPLETE_PREFIX))
            .collect()
    }

    #[test]
    fn payload_is_complete_clean_and_repairs_missing_assets() {
        let temp = tempfile::tempdir().unwrap();
        let path = unpack(temp.path()).unwrap();
        for name in [
            "server.py",
            "native_session_launch.py",
            "wait_inbox.py",
            "static/index.html",
            "requirements.lock",
        ] {
            assert!(path.join(name).is_file(), "{name}");
        }
        assert!(!FILES.iter().any(|(name, _)| name.contains("python.exe")
            || name.ends_with("hub_worker.py")
            || name.ends_with("worker_bridge_mcp.py")
            || name.ends_with(".db")
            || name.contains("agent_profiles")));
        fs::write(path.join("server.py"), "broken").unwrap();
        assert_eq!(unpack(temp.path()).unwrap(), path);
        assert!(fs::read_to_string(path.join("server.py"))
            .unwrap()
            .starts_with("import asyncio"));
        assert!(
            staging_entries(temp.path()).is_empty(),
            "a successful publish leaves no staging or aside directory"
        );
    }

    #[test]
    fn a_complete_payload_is_never_rewritten() {
        let temp = tempfile::tempdir().unwrap();
        let path = unpack(temp.path()).unwrap();
        fs::write(path.join("user-marker"), b"keep").unwrap();
        assert_eq!(unpack(temp.path()).unwrap(), path);
        assert_eq!(fs::read(path.join("user-marker")).unwrap(), b"keep");
        assert!(staging_entries(temp.path()).is_empty());
    }

    #[test]
    fn an_incomplete_payload_is_replaced_as_a_whole_directory() {
        let temp = tempfile::tempdir().unwrap();
        let path = unpack(temp.path()).unwrap();
        fs::remove_file(path.join("server.py")).unwrap();
        fs::write(path.join("left-by-a-crash"), b"old").unwrap();
        assert_eq!(unpack(temp.path()).unwrap(), path);
        assert!(fs::read_to_string(path.join("server.py"))
            .unwrap()
            .starts_with("import asyncio"));
        assert!(
            !path.join("left-by-a-crash").exists(),
            "the incomplete directory is replaced, not merged"
        );
        assert!(staging_entries(temp.path()).is_empty());
    }

    #[test]
    fn hub_command_line_match_needs_the_recorded_python_and_server() {
        let python = Path::new(r"C:\Users\me\portable\venvs\abc\Scripts\python.exe");
        let repo = Path::new(r"C:\Users\me\portable\resources\payload");
        let good = r#""C:\Users\me\portable\venvs\abc\Scripts\python.exe" "C:\Users\me\portable\resources\payload\server.py""#;
        assert!(hub_command_line_matches(good, python, repo));
        assert!(hub_command_line_matches(
            r"C:/Users/me/PORTABLE/venvs/abc/Scripts/python.exe C:/Users/me/portable/resources/payload/server.py",
            python,
            repo
        ));
        assert!(!hub_command_line_matches(
            r#""C:\Python\python.exe" "C:\Users\me\portable\resources\payload\server.py""#,
            python,
            repo
        ));
        assert!(!hub_command_line_matches(
            r#""C:\Users\me\portable\venvs\abc\Scripts\python.exe" "C:\Other\server.py""#,
            python,
            repo
        ));
    }

    #[test]
    fn payload_paths_are_case_and_separator_insensitive() {
        let root = Path::new(r"C:\Users\me\portable");
        assert!(is_within(
            Path::new("c:/users/me/portable/resources/payload"),
            &root.join("resources")
        ));
        assert!(!is_within(
            Path::new(r"C:\Users\me\portable-other\resources\payload"),
            &root.join("resources")
        ));
        assert_ne!(path_key(Path::new(r"C:\A\B\")), path_key(Path::new(r"C:\A\BC")));
    }
}
