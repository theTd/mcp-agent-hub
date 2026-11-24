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
/// verify its command line, stop it, and report what was stopped. A live second copy of this app
/// is reported but never touched — wherever it was started from.
pub async fn recover_stale_hub(root: &Path) -> Result<RecoveryReport, PortableError> {
    let previous = hub_config::recover_launch_record(&root.join("launch.json"))
        .map_err(PortableError::from)?;
    let same_path_peer = live_app_peer();
    match plan_recovery(root, previous, same_path_peer, std::process::id()) {
        RecoveryPlan::LivePeer(peer) => Ok(RecoveryReport {
            live_peer: Some(peer),
            stopped: None,
        }),
        RecoveryPlan::Nothing => Ok(RecoveryReport::default()),
        RecoveryPlan::Stop { root_pid, record } => {
            let stopped = holder(root_pid);
            stop_hub(root_pid, &record).await.map_err(|message| {
                let holders = payload_holders(&root.join("resources").join(PAYLOAD_ID));
                PortableError::with_holders(message, holders)
            })?;
            Ok(RecoveryReport {
                live_peer: None,
                stopped,
            })
        }
    }
}

/// What `recover_stale_hub` will do, decided before anything is touched. Separating the decision
/// from the act keeps the two "never touch a live app's hub" guards testable without stopping a
/// process.
#[derive(Debug)]
enum RecoveryPlan {
    /// Another live app owns this root; its hub and its record are left exactly as they are.
    LivePeer(u32),
    /// No record, or a record that does not describe a hub of this payload.
    Nothing,
    /// A hub of this payload that nothing else owns: stop it, then extract.
    Stop {
        root_pid: u32,
        record: LaunchRecord,
    },
}

fn plan_recovery(
    root: &Path,
    previous: Option<LaunchRecord>,
    same_path_peer: Option<u32>,
    self_pid: u32,
) -> RecoveryPlan {
    // Two independent probes: a peer started from this very path, and a peer the record names.
    // The second one is what catches a copy (or a build output) running from another directory.
    let recorded_peer = previous
        .as_ref()
        .and_then(|record| live_record_app_peer(record, self_pid));
    if let Some(peer) = same_path_peer.or(recorded_peer) {
        return RecoveryPlan::LivePeer(peer);
    }
    let Some(record) = previous else {
        return RecoveryPlan::Nothing;
    };
    if !is_within(&record.repo_dir, &root.join("resources")) {
        return RecoveryPlan::Nothing;
    }
    let pid = match hub_config::launch_record_owner(&record) {
        LaunchRecordOwner::Alive(identity) => {
            if !command_line_is_hub(identity.pid, &record) {
                // The record is inconsistent; never kill a process we cannot identify.
                return RecoveryPlan::Nothing;
            }
            identity.pid
        }
        LaunchRecordOwner::Unrecorded | LaunchRecordOwner::Stale => {
            let Some(pid) = windows_process::listening_pid(record.port) else {
                return RecoveryPlan::Nothing;
            };
            if !command_line_is_hub(pid, &record) {
                return RecoveryPlan::Nothing;
            }
            pid
        }
    };
    RecoveryPlan::Stop {
        root_pid: hub_tree_root(pid, &record),
        record,
    }
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

/// Fragment of the shell's image name, used only to sanity-check a record that names `app_pid`
/// without a creation time.
const APP_IMAGE_STEM: &str = "mcp-agent-hub";

/// The live desktop app that `launch.json` says owns this portable root, if any.
///
/// `live_app_peer` compares image paths, so it only sees a second copy started from the same file.
/// The same app can also run from a copy or from a build output (`desktop\dist\…`), and then that
/// comparison misses it — while the hub it owns is right here, under this root. The record's
/// `app_pid`/`app_creation_time` identify the owner by process instance instead of by path, so a
/// hub it owns is never mistaken for an orphan.
fn live_record_app_peer(record: &LaunchRecord, self_pid: u32) -> Option<u32> {
    live_record_app_peer_with(
        record,
        self_pid,
        windows_process::process_is_alive,
        windows_process::process_name,
    )
}

fn live_record_app_peer_with(
    record: &LaunchRecord,
    self_pid: u32,
    is_alive: impl Fn(ProcessIdentity) -> bool,
    name_of: impl Fn(u32) -> Option<String>,
) -> Option<u32> {
    // This record may be our own: we are alive by definition, but we are not a peer.
    if record.app_pid == Some(self_pid) {
        return None;
    }
    if let Some(identity) = record.owner_app_identity() {
        return is_alive(identity).then_some(identity.pid);
    }
    // Defensive: an old record with only `app_pid`. That PID proves nothing on its own, so the
    // image name must still be this shell; a reused PID of any other program never matches.
    let pid = record.owner_app_pid_without_identity()?;
    let name = name_of(pid)?.to_lowercase();
    name.contains(APP_IMAGE_STEM).then_some(pid)
}

/// The live app `launch.json` currently names, read fresh from disk (never a value computed minutes
/// ago). `None` when there is no record, it is unreadable, it names this process, or its app is gone.
fn recorded_live_peer(root: &Path) -> Option<u32> {
    let record = hub_config::recover_launch_record(&root.join("launch.json"))
        .ok()
        .flatten()?;
    live_record_app_peer(&record, std::process::id())
}

/// Every live app that may already own this root's port and `launch.json`: a copy started from
/// this very image, and the app instance the on-disk record names (which also covers a copy or a
/// build output started from another directory). Used as a *fresh* probe wherever the answer must
/// reflect the world right now, never a value computed minutes ago.
fn detect_live_peer(root: &Path) -> Option<u32> {
    live_app_peer().or_else(|| recorded_live_peer(root))
}

/// What a prepare run may still do once the slow part of the preparation is over.
#[derive(Debug, PartialEq, Eq)]
enum PeerGate {
    /// Keep the peer this run already knew about (Some only for a diagnostic prepare run).
    Proceed(Option<u32>),
    /// A live app appeared during preparation: a real start must abort instead of taking its port
    /// and overwriting its `launch.json`.
    Abort(String),
}

/// Decide from a *fresh* detection, so a peer that started during the minutes of preparation is
/// never mistaken for an orphan. A diagnostic `--portable-prepare-only` run owns nothing anyway,
/// so it only records the peer and goes on without touching the port or the record.
fn peer_gate(detected: Option<u32>, previous: Option<u32>, prepare_only: bool) -> PeerGate {
    match detected {
        Some(pid) if prepare_only => PeerGate::Proceed(Some(pid)),
        Some(pid) => PeerGate::Abort(format!(
            "另一个便携版实例正在运行（PID {pid}），本次启动已中止；请使用该窗口，或先完全退出它"
        )),
        None => PeerGate::Proceed(previous),
    }
}

/// The port chosen by `prepare` must still be ours to take when the supervisor launches the hub.
/// A payload-owned orphan is stopped; any other listener fails closed. Binding is checked too: a
/// listener check alone can lose the race. When a live peer owns this root, its port is its own:
/// nothing here may stop it, fail on it or claim its launch record.
async fn ensure_port_free(
    root: &Path,
    port: u16,
    python: &Path,
    repo_dir: &Path,
    live_peer: Option<u32>,
) -> Result<PortClaim, PortableError> {
    if let Some(peer) = live_peer {
        return Ok(PortClaim::OwnedByPeer(peer));
    }
    if let Some(pid) = windows_process::listening_pid(port) {
        // Second line of defence for the TOCTOU window: preparation can take minutes, and another
        // path's live app may have started a hub here and written its own `launch.json` in the
        // meantime. The record names that app by process instance, so it is checked *before* any
        // stop, and its hub — and its record — are left exactly as they are.
        if let Some(peer) = recorded_live_peer(root) {
            return Ok(PortClaim::OwnedByPeer(peer));
        }
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
        .map(|_| PortClaim::Free)
        .map_err(|error| PortableError::new(format!("端口 {port} 当前不可绑定：{error}")))
}

/// Who owns the port after the final check.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PortClaim {
    /// Free: this run may claim the port *and* `launch.json`.
    Free,
    /// A live app owns this root's port (and its record): nothing was stopped, nothing may be claimed.
    OwnedByPeer(u32),
}

/// Whether this run may claim the port and the launch record of this portable root. A live peer
/// already owns both: its record names its hub, and its hub is serving that port.
fn owns_portable_endpoint(live_peer: Option<u32>) -> bool {
    live_peer.is_none()
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
    // The recovery reads `launch.json`; it never touches a live peer's hub or its record.
    let recovery = recover_stale_hub(&root).await?;
    let prepare_only = std::env::args().any(|arg| arg == "--portable-prepare-only");
    if recovery.live_peer.is_some() && !prepare_only {
        return Err(PortableError::new(format!(
            "另一个便携版实例正在运行（PID {}），请使用该窗口，或先完全退出它",
            recovery.live_peer.unwrap()
        )));
    }
    // A diagnostic `--portable-prepare-only` may run next to a live app, but it owns nothing of
    // that app: not its port, not its launch record.
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
    // Second look for a live app, now that the slow part is done: another path's copy can have
    // started its hub during those minutes, and the probe taken before `unpack` is stale by
    // construction. Without this the port of that live app would be "reclaimed" and its
    // `launch.json` overwritten.
    let live_peer = match peer_gate(detect_live_peer(&root), live_peer, prepare_only) {
        PeerGate::Proceed(peer) => peer,
        PeerGate::Abort(reason) => return Err(PortableError::new(reason)),
    };
    // Fail closed here: preparation can take minutes, so the port that was free when selected may
    // now belong to someone else. The supervisor's post-ready job check remains the final gate.
    match ensure_port_free(&root, cfg.port, &cfg.python, &cfg.repo_dir, live_peer).await? {
        PortClaim::Free => {
            // A live peer's record names its own hub; never clobber it with this run.
            write_prepared_record(&launch_path, &cfg, live_peer)?;
        }
        PortClaim::OwnedByPeer(pid) => {
            if !prepare_only {
                return Err(PortableError::new(format!(
                    "另一个便携版实例正在运行（PID {pid}），本次启动已中止；请使用该窗口，或先完全退出它"
                )));
            }
            // A diagnostic run owns nothing of a live app: neither its port nor its record.
        }
    }
    Ok(cfg)
}

/// Claim `launch.json` for the hub this run is about to start. A live peer already owns the record
/// under this root, so a prepare or diagnostic run leaves it untouched.
fn write_prepared_record(
    launch_path: &Path,
    cfg: &HubConfig,
    live_peer: Option<u32>,
) -> Result<(), String> {
    if !owns_portable_endpoint(live_peer) {
        return Ok(());
    }
    let record = LaunchRecord::prepared(cfg.python.clone(), cfg.repo_dir.clone(), cfg.port);
    hub_config::write_launch_record(launch_path, &record)
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

    // --- A hub is never stopped while the app that owns it is still running ----------------------

    use std::os::windows::process::CommandExt;

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    /// Long enough for any recovery gate in these tests to run, short enough to expire on its own.
    const STAND_IN_SECONDS: u32 = 45;

    fn block_on<F: std::future::Future>(f: F) -> F::Output {
        tauri::async_runtime::block_on(f)
    }

    /// A stand-in app process that stays alive for `STAND_IN_SECONDS`.
    fn spawn_sleeper() -> std::process::Child {
        let script = format!("ping -n {STAND_IN_SECONDS} 127.0.0.1 >nul");
        Command::new("cmd.exe")
            .arg("/c")
            .arg(script)
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn a stand-in process")
    }

    /// A stand-in hub: it stays alive, and its command line names the record's python and
    /// `server.py`, so `command_line_is_hub` identifies it exactly as the real thing.
    fn spawn_hub_stand_in(python: &Path, repo_dir: &Path) -> std::process::Child {
        let script = format!(
            "ping -n {STAND_IN_SECONDS} 127.0.0.1 >nul & rem {} {}",
            python.display(),
            repo_dir.join("server.py").display()
        );
        Command::new("cmd.exe")
            .arg("/c")
            .arg(script)
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn a hub stand-in")
    }

    /// End a stand-in and release its handle. Windows keeps a process object — and its creation
    /// time — alive while any handle is open, so a leaked handle would make the PID look alive.
    fn stop(mut child: std::process::Child) {
        let identity = windows_process::process_identity(child.id());
        let _ = child.kill();
        let _ = child.wait();
        // Release the handle so the process object — and its creation time — can be destroyed.
        drop(child);
        let deadline = Instant::now() + Duration::from_secs(5);
        if let Some(identity) = identity {
            while windows_process::process_is_alive(identity) && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }

    /// Release a stand-in's handle without ending it: the recovery under test must be the only
    /// thing able to stop it.
    fn detach(child: std::process::Child) -> ProcessIdentity {
        let identity = windows_process::process_identity(child.id()).expect("a live stand-in");
        drop(child);
        identity
    }

    fn free_port() -> u16 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.local_addr().unwrap().port()
    }

    /// A portable root with a payload directory the record can point at.
    fn portable_root(temp: &Path) -> (PathBuf, PathBuf) {
        let repo_dir = temp.join("resources").join("payload");
        fs::create_dir_all(&repo_dir).unwrap();
        (temp.join("venvs").join("python.exe"), repo_dir)
    }

    fn record_of(python: &Path, repo_dir: &Path, port: u16) -> LaunchRecord {
        LaunchRecord::prepared(python.to_path_buf(), repo_dir.to_path_buf(), port)
    }

    /// A PID that no process has right now: the app that wrote the record has exited, and Windows
    /// may hand its number to another program later. Killing a real stand-in cannot stand in for
    /// "gone" on this host, which keeps terminated process objects visible for a while.
    fn exited_pid() -> u32 {
        let mut candidate = 0xF000_0001u32;
        for _ in 0..1024 {
            if windows_process::process_identity(candidate).is_none() {
                return candidate;
            }
            candidate = candidate.wrapping_add(1);
        }
        panic!("no unused PID found");
    }

    /// The root PID `recover_stale_hub` would stop for this record, if any.
    ///
    /// The act itself is `stop_hub`, which then waits for the process object to disappear. This
    /// host keeps terminated process objects (and their creation time) visible for a while, so the
    /// kill cannot be observed here; the decision — clean up, or leave a live app's hub alone —
    /// is what these tests assert, and it is the only thing that can lead to a stop.
    fn planned_stop(root: &Path, record: LaunchRecord) -> Option<u32> {
        match plan_recovery(root, Some(record), None, std::process::id()) {
            RecoveryPlan::Stop { root_pid, .. } => Some(root_pid),
            RecoveryPlan::LivePeer(_) | RecoveryPlan::Nothing => None,
        }
    }

    #[test]
    fn a_live_app_recorded_from_another_path_protects_its_hub() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let (python, repo_dir) = portable_root(root);
        let port = free_port();
        let hub_identity = detach(spawn_hub_stand_in(&python, &repo_dir));
        let app = spawn_sleeper();
        // The app that owns the hub runs from a different image than this test binary, exactly
        // like a copy or `desktop\dist\…` next to the installed one. `live_app_peer` cannot see
        // it; the recorded app identity can.
        let record = record_of(&python, &repo_dir, port)
            .with_owner(hub_identity.pid, None)
            .unwrap()
            .with_runtime_metadata(app.id(), root.join("data"), "test")
            .unwrap();
        let record_path = root.join("launch.json");
        hub_config::write_launch_record(&record_path, &record).unwrap();

        let report = block_on(recover_stale_hub(root)).unwrap();

        assert_eq!(report.live_peer, Some(app.id()), "{report:?}");
        assert!(report.stopped.is_none(), "{report:?}");
        assert!(
            windows_process::process_is_alive(hub_identity),
            "the live app's hub must still be running"
        );
        assert_eq!(
            hub_config::recover_launch_record(&record_path).unwrap(),
            Some(record),
            "the live app's record must be neither overwritten nor moved aside"
        );
        windows_process::kill_identities(&[hub_identity]);
        stop(app);
    }

    #[test]
    fn a_reused_app_pid_no_longer_protects_the_hub() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let (python, repo_dir) = portable_root(root);
        let port = free_port();
        let app = spawn_sleeper();
        let app_identity = windows_process::process_identity(app.id()).unwrap();
        let hub_identity = detach(spawn_hub_stand_in(&python, &repo_dir));
        let mut record = record_of(&python, &repo_dir, port)
            .with_owner(hub_identity.pid, None)
            .unwrap()
            .with_runtime_metadata(app_identity.pid, root.join("data"), "test")
            .unwrap();
        // Windows reused the PID for another program: the number matches, the instance does not.
        record.app_creation_time = Some(app_identity.creation_time + 1);
        hub_config::write_launch_record(&root.join("launch.json"), &record).unwrap();

        assert_eq!(live_record_app_peer(&record, std::process::id()), None);
        assert_eq!(
            planned_stop(&root, record),
            Some(hub_identity.pid),
            "the hub of a replaced app is an orphan and must be selected for cleanup"
        );
        assert!(
            windows_process::process_is_alive(app_identity),
            "a process that merely holds the old PID is never stopped"
        );
        windows_process::kill_identities(&[hub_identity]);
        stop(app);
    }

    #[test]
    fn a_hub_of_a_dead_app_record_is_cleaned_up() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let (python, repo_dir) = portable_root(root);
        let port = free_port();
        let hub_identity = detach(spawn_hub_stand_in(&python, &repo_dir));
        let mut record = record_of(&python, &repo_dir, port)
            .with_owner(hub_identity.pid, None)
            .unwrap();
        // The app that wrote this record has exited; its PID is nobody's now.
        record.app_pid = Some(exited_pid());
        record.app_creation_time = windows_process::creation_time(std::process::id());
        hub_config::write_launch_record(&root.join("launch.json"), &record).unwrap();

        assert_eq!(live_record_app_peer(&record, std::process::id()), None);
        assert_eq!(
            planned_stop(&root, record),
            Some(hub_identity.pid),
            "the hub of an app that is gone is an orphan and must be selected for cleanup"
        );
        windows_process::kill_identities(&[hub_identity]);
    }

    #[test]
    fn live_record_app_peer_matches_only_the_recorded_app_instance() {
        let temp = tempfile::tempdir().unwrap();
        let (python, repo_dir) = portable_root(temp.path());
        let app = spawn_sleeper();
        let identity = windows_process::process_identity(app.id()).unwrap();
        let mut record = record_of(&python, &repo_dir, 4500);
        record.app_pid = Some(identity.pid);
        record.app_creation_time = Some(identity.creation_time);

        assert_eq!(live_record_app_peer(&record, std::process::id()), Some(identity.pid));
        record.app_creation_time = Some(identity.creation_time + 1);
        assert_eq!(live_record_app_peer(&record, std::process::id()), None);
        record.app_creation_time = Some(identity.creation_time);
        assert_eq!(
            live_record_app_peer(&record, identity.pid),
            None,
            "a record naming this process is our own, never a peer"
        );
        let mut record_without_app = record_of(&python, &repo_dir, 4500);
        record_without_app.app_pid = None;
        record_without_app.app_creation_time = None;
        assert_eq!(live_record_app_peer(&record_without_app, std::process::id()), None);
        stop(app);
    }

    #[test]
    fn a_legacy_app_pid_without_a_creation_time_is_checked_by_image_name() {
        let temp = tempfile::tempdir().unwrap();
        let (python, repo_dir) = portable_root(temp.path());
        let mut record = record_of(&python, &repo_dir, 4500);
        record.app_pid = Some(4321);
        record.app_creation_time = None;
        let alive = |_: ProcessIdentity| true;
        let shell = |_pid: u32| Some("mcp-agent-hub-0.1.0-windows-x64-portable.exe".to_string());
        assert_eq!(live_record_app_peer_with(&record, 1, alive, shell), Some(4321));
        // The PID exists, but it is some other program that reused it.
        assert_eq!(
            live_record_app_peer_with(&record, 1, alive, |_| Some("notepad.exe".into())),
            None
        );
        // The PID is gone.
        assert_eq!(live_record_app_peer_with(&record, 1, alive, |_| None), None);
        // Our own record is never a peer, even without a creation time.
        assert_eq!(
            live_record_app_peer_with(&record, 4321, alive, |_| {
                Some("mcp-agent-hub.exe".into())
            }),
            None
        );
    }

    // --- A prepare/diagnostic run must not take a live peer's port or record --------------------

    #[test]
    fn a_prepare_run_next_to_a_live_peer_leaves_the_port_and_record_alone() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let python = PathBuf::from(r"C:\portable\venvs\abc\Scripts\python.exe");
        let repo_dir = PathBuf::from(r"C:\portable\resources\payload");

        assert_eq!(
            block_on(ensure_port_free(root, port, &python, &repo_dir, Some(4321)))
                .expect("a live peer's own port is never stopped or reported as foreign"),
            PortClaim::OwnedByPeer(4321)
        );
        assert!(
            block_on(ensure_port_free(root, port, &python, &repo_dir, None)).is_err(),
            "without a live peer an occupied port still fails closed"
        );
        drop(listener);
        assert_eq!(
            block_on(ensure_port_free(root, port, &python, &repo_dir, None)).unwrap(),
            PortClaim::Free
        );
    }

    /// P0-4: there is no `launch.json` when the preparation starts, and another path's live app
    /// starts its hub *during* the slow part. The fresh re-check must find it, and neither its
    /// port nor its record may be taken.
    #[test]
    fn a_live_app_that_appears_during_preparation_is_found_by_the_second_look() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let (python, repo_dir) = portable_root(root);
        let app = spawn_sleeper();
        let app_identity = windows_process::process_identity(app.id()).unwrap();
        // The peer: a hub that stays alive, a socket bound to its port, and its own record — all
        // written while this run was preparing.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let hub_identity = detach(spawn_hub_stand_in(&python, &repo_dir));
        let record = record_of(&python, &repo_dir, port)
            .with_owner(hub_identity.pid, None)
            .unwrap()
            .with_runtime_metadata(app_identity.pid, root.join("data"), "peer")
            .unwrap();
        let record_path = root.join("launch.json");
        hub_config::write_launch_record(&record_path, &record).unwrap();
        let published = fs::read_to_string(&record_path).unwrap();

        assert_eq!(
            detect_live_peer(root),
            Some(app_identity.pid),
            "the second look must see the app that started after the first probe"
        );
        match peer_gate(detect_live_peer(root), None, false) {
            PeerGate::Abort(reason) => assert!(
                reason.contains(&format!("PID {}", app_identity.pid)),
                "{reason}"
            ),
            other => panic!("a real start must abort instead of taking a live peer's port: {other:?}"),
        }

        let claim = block_on(ensure_port_free(root, port, &python, &repo_dir, None))
            .expect("a port whose record names a live app is never reclaimed");
        assert_eq!(claim, PortClaim::OwnedByPeer(app_identity.pid));
        assert!(
            windows_process::process_is_alive(hub_identity),
            "the live app's hub is never stopped"
        );
        assert_eq!(
            fs::read_to_string(&record_path).unwrap(),
            published,
            "the live app's launch.json is never overwritten"
        );

        // A diagnostic `--portable-prepare-only` run owns nothing: it records the peer and stops.
        assert_eq!(
            peer_gate(detect_live_peer(root), None, true),
            PeerGate::Proceed(Some(app_identity.pid))
        );

        windows_process::kill_identities(&[hub_identity]);
        drop(listener);
        stop(app);
    }

    #[test]
    fn a_prepare_run_next_to_a_live_peer_never_rewrites_launch_json() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let (python, repo_dir) = portable_root(root);
        let path = root.join("launch.json");
        let app = spawn_sleeper();
        let record = record_of(&python, &repo_dir, 4500)
            .with_runtime_metadata(app.id(), root.join("data"), "test")
            .unwrap();
        hub_config::write_launch_record(&path, &record).unwrap();
        let published = fs::read_to_string(&path).unwrap();
        let cfg = HubConfig {
            repo_dir: repo_dir.clone(),
            python: python.clone(),
            port: 4444,
        };

        write_prepared_record(&path, &cfg, Some(app.id())).unwrap();

        assert_eq!(fs::read_to_string(&path).unwrap(), published);
        // Without a live peer the record is this run's to claim.
        write_prepared_record(&path, &cfg, None).unwrap();
        let claimed = hub_config::recover_launch_record(&path).unwrap().unwrap();
        assert_eq!(claimed.port, 4444);
        assert_eq!(claimed.state, "prepared");
        assert_eq!(claimed.app_pid, None);
        stop(app);
    }
}
