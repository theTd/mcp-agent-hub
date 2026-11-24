//! Where the hub lives and how it is launched: env first, then `%APPDATA%\mcp-agent-hub-desktop\config.json`,
//! then the defaults (the repo this app was built from, its `.venv` python, port 4444).

#[cfg(any(not(feature = "portable"), test))]
use std::path::Component;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::Value;

pub const DEFAULT_PORT: u16 = 4444;
#[cfg(not(feature = "portable"))]
pub const APP_DIR_NAME: &str = "mcp-agent-hub-desktop";
#[cfg(feature = "portable")]
pub const APP_DIR_NAME: &str = "mcp-agent-hub-portable";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HubConfig {
    pub repo_dir: PathBuf,
    pub python: PathBuf,
    pub port: u16,
}

impl HubConfig {
    pub fn server_py(&self) -> PathBuf {
        self.repo_dir.join("server.py")
    }

    pub fn console_url(&self, session_id: Option<&str>) -> String {
        match session_id {
            Some(sid) => format!(
                "http://127.0.0.1:{}/?session={}",
                self.port,
                encode_query_value(sid)
            ),
            None => format!("http://127.0.0.1:{}/", self.port),
        }
    }
}

/// The optional `config.json`: `{"repo_dir", "python", "port"}`, every key optional.
#[derive(Debug, Default, Clone, PartialEq, Deserialize)]
pub struct FileConfig {
    #[serde(default)]
    pub repo_dir: Option<String>,
    #[serde(default)]
    pub python: Option<String>,
    #[serde(default)]
    pub port: Option<Value>,
}

pub fn parse_file_config(text: &str) -> Result<FileConfig, String> {
    serde_json::from_str(text).map_err(|e| format!("config.json 无法解析：{e}"))
}

/// The portable startup record next to `prepare.lock`. Version 0 is the old record with only
/// `python`, `repo_dir` and `port`; version 1 adds the hub's process identity and shutdown token so
/// a later start can tell a leftover hub from an unrelated process and stop it before reusing the
/// payload or the port.
#[cfg(any(feature = "portable", test))]
pub mod launch_record {
    use std::fs;
    use std::io::Write;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    use serde::{Deserialize, Serialize};
    use serde_json::Value;

    use crate::windows_process;

    pub const LAUNCH_RECORD_VERSION: u32 = 1;
    pub const LEGACY_LAUNCH_RECORD_VERSION: u32 = 0;

    #[derive(Debug, Clone, PartialEq, Eq, Serialize)]
    pub struct LaunchRecord {
        pub version: u32,
        pub python: PathBuf,
        pub repo_dir: PathBuf,
        #[serde(skip_serializing_if = "Option::is_none")]
        pub data_dir: Option<PathBuf>,
        pub port: u16,
        /// Root hub process (the venv launcher); 0 until a hub has been launched for this record.
        pub pid: u32,
        /// Windows creation time of `pid`; 0 together with `pid == 0`.
        pub creation_time: u64,
        #[serde(skip_serializing_if = "Option::is_none")]
        pub app_pid: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        pub app_creation_time: Option<u64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        pub spawn_path: Option<String>,
        pub state: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        pub shutdown_token: Option<String>,
    }

    impl LaunchRecord {
        pub fn prepared(python: PathBuf, repo_dir: PathBuf, port: u16) -> Self {
            LaunchRecord {
                version: LAUNCH_RECORD_VERSION,
                python,
                repo_dir,
                data_dir: None,
                port,
                pid: 0,
                creation_time: 0,
                app_pid: None,
                app_creation_time: None,
                spawn_path: None,
                state: "prepared".into(),
                shutdown_token: None,
            }
        }

        /// Pin the record to the process that was just spawned. Fails when that process is already
        /// gone, so a record never names a PID it did not observe.
        pub fn with_owner(
            mut self,
            pid: u32,
            shutdown_token: Option<String>,
        ) -> Result<Self, String> {
            let creation_time = windows_process::creation_time(pid)
                .ok_or_else(|| format!("无法读取刚启动的 Hub 进程 PID {pid} 的创建时间"))?;
            self.pid = pid;
            self.creation_time = creation_time;
            self.shutdown_token = shutdown_token.filter(|token| !token.trim().is_empty());
            self.state = "running".into();
            validate_launch_record(&self)?;
            Ok(self)
        }

        pub fn with_runtime_metadata(
            mut self,
            app_pid: u32,
            data_dir: PathBuf,
            spawn_path: impl Into<String>,
        ) -> Result<Self, String> {
            self.app_pid = Some(app_pid);
            self.app_creation_time = windows_process::creation_time(app_pid);
            self.data_dir = Some(data_dir);
            self.spawn_path = Some(spawn_path.into());
            validate_launch_record(&self)?;
            Ok(self)
        }

        /// The exact desktop-app process instance this record belongs to, when the record names
        /// one. `None` for a legacy record (version 0), for a record written before the app pinned
        /// itself, and for a record that only kept `app_pid`.
        pub fn owner_app_identity(&self) -> Option<windows_process::ProcessIdentity> {
            let pid = self.app_pid.filter(|pid| *pid != 0)?;
            let creation_time = self.app_creation_time.filter(|ticks| *ticks != 0)?;
            Some(windows_process::ProcessIdentity { pid, creation_time })
        }

        /// `Some(app_pid)` when the app that wrote this record is still that exact process. A
        /// reused PID (same number, different creation time) is a different program and reports
        /// `None`, so it can never keep an orphan hub alive by accident.
        pub fn owner_app_pid_if_alive(&self) -> Option<u32> {
            let identity = self.owner_app_identity()?;
            windows_process::process_is_alive(identity).then_some(identity.pid)
        }

        /// `Some(app_pid)` for an old record that carries only `app_pid`, with no creation time to
        /// pin the instance. The PID alone proves nothing, so a caller must verify the image name
        /// before treating it as a live app. `None` when the record has no usable app PID.
        pub fn owner_app_pid_without_identity(&self) -> Option<u32> {
            let pid = self.app_pid.filter(|pid| *pid != 0)?;
            self.owner_app_identity().is_none().then_some(pid)
        }
    }

    #[derive(Debug, Deserialize)]
    struct RawLaunchRecord {
        #[serde(default)]
        version: Option<u32>,
        #[serde(default)]
        python: Option<String>,
        #[serde(default)]
        repo_dir: Option<String>,
        #[serde(default)]
        data_dir: Option<String>,
        #[serde(default)]
        port: Option<Value>,
        #[serde(default)]
        pid: Option<u32>,
        #[serde(default)]
        creation_time: Option<u64>,
        #[serde(default)]
        app_pid: Option<u32>,
        #[serde(default)]
        app_creation_time: Option<u64>,
        #[serde(default)]
        spawn_path: Option<String>,
        #[serde(default)]
        state: Option<String>,
        #[serde(default)]
        shutdown_token: Option<String>,
    }

    fn required_path(value: Option<String>, field: &str) -> Result<PathBuf, String> {
        let text = value.map(|v| v.trim().to_string()).unwrap_or_default();
        if text.is_empty() {
            return Err(format!("launch.json 缺少 {field}"));
        }
        if text.contains('\0') {
            return Err(format!("launch.json 的 {field} 含非法字符"));
        }
        Ok(PathBuf::from(text))
    }

    pub fn parse_launch_record(text: &str) -> Result<LaunchRecord, String> {
        let raw: RawLaunchRecord = serde_json::from_str(text)
            .map_err(|e| format!("launch.json 无法解析：{e}"))?;
        let version = raw.version.unwrap_or(LEGACY_LAUNCH_RECORD_VERSION);
        if version > LAUNCH_RECORD_VERSION {
            return Err(format!(
                "launch.json 版本 {version} 比当前程序新，请使用较新的便携版"
            ));
        }
        let python = required_path(raw.python, "python")?;
        let repo_dir = required_path(raw.repo_dir, "repo_dir")?;
        let port = raw
            .port
            .as_ref()
            .ok_or_else(|| "launch.json 缺少 port".to_string())
            .and_then(|value| super::port_from_json(value))?
            .ok_or_else(|| "launch.json 的 port 为空".to_string())?;
        let record = LaunchRecord {
            version,
            python,
            repo_dir,
            data_dir: raw.data_dir.map(PathBuf::from),
            port,
            pid: raw.pid.unwrap_or(0),
            creation_time: raw.creation_time.unwrap_or(0),
            app_pid: raw.app_pid,
            app_creation_time: raw.app_creation_time,
            spawn_path: raw.spawn_path,
            state: raw.state.unwrap_or_else(|| "prepared".into()),
            shutdown_token: raw
                .shutdown_token
                .map(|token| token.trim().to_string())
                .filter(|token| !token.is_empty()),
        };
        validate_launch_record(&record)?;
        Ok(record)
    }

    pub fn validate_launch_record(record: &LaunchRecord) -> Result<(), String> {
        if record.version > LAUNCH_RECORD_VERSION {
            return Err(format!(
                "launch.json 版本 {} 比当前程序新，请使用较新的便携版",
                record.version
            ));
        }
        if record.port == 0 {
            return Err("launch.json 的 port 不是有效端口：0".into());
        }
        if record.python.as_os_str().is_empty() || !record.python.is_absolute() {
            return Err(format!(
                "launch.json 的 python 不是绝对路径：{}",
                record.python.display()
            ));
        }
        if record.repo_dir.as_os_str().is_empty() || !record.repo_dir.is_absolute() {
            return Err(format!(
                "launch.json 的 repo_dir 不是绝对路径：{}",
                record.repo_dir.display()
            ));
        }
        if let Some(data_dir) = &record.data_dir {
            if !data_dir.is_absolute() {
                return Err(format!("launch.json 的 data_dir 不是绝对路径：{}", data_dir.display()));
            }
        }
        match (record.pid, record.creation_time) {
            (0, 0) => {}
            (0, _) => return Err("launch.json 有 creation_time 但没有 pid".into()),
            (_, 0) => return Err("launch.json 有 pid 但没有 creation_time".into()),
            _ => {}
        }
        match (record.app_pid, record.app_creation_time) {
            (None, None) | (Some(0), None) => {}
            // An old record may name only `app_pid`. It stays readable instead of being moved
            // aside — possibly while the app that wrote it is still running — and its PID is
            // verified by image name where the record is used.
            (Some(_), None) => {}
            (Some(_), Some(0)) | (None, Some(_)) => return Err("launch.json 的 app_pid/app_creation_time 不一致".into()),
            (Some(_), Some(_)) => {}
        }
        if let Some(token) = &record.shutdown_token {
            if token.is_empty() || token.len() > 256 {
                return Err("launch.json 的 shutdown_token 长度无效".into());
            }
        }
        if record.state.trim().is_empty() {
            return Err("launch.json 的 state 为空".into());
        }
        Ok(())
    }

    static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    fn temp_sibling(path: &Path, tag: &str) -> PathBuf {
        let name = path
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| "launch.json".to_string());
        let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        path.with_file_name(format!(
            ".{name}.{tag}-{}-{sequence}",
            std::process::id()
        ))
    }

    /// Write the record with a rename: a reader sees the old record or the new one, never a prefix.
    pub fn write_launch_record(path: &Path, record: &LaunchRecord) -> Result<(), String> {
        validate_launch_record(record)?;
        let bytes = serde_json::to_vec(record)
            .map_err(|e| format!("launch.json 无法序列化：{e}"))?;
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent)
            .map_err(|e| format!("无法创建 {}：{e}", parent.display()))?;
        let temp = temp_sibling(path, "tmp");
        let write = (|| -> std::io::Result<()> {
            let mut file = fs::File::create(&temp)?;
            file.write_all(&bytes)?;
            file.sync_all()
        })();
        if let Err(error) = write {
            let _ = fs::remove_file(&temp);
            return Err(format!("无法写入 {}：{error}", temp.display()));
        }
        if let Err(error) = fs::rename(&temp, path) {
            let _ = fs::remove_file(&temp);
            return Err(format!("无法替换 {}：{error}", path.display()));
        }
        Ok(())
    }

    /// `None` when the record does not exist. An unreadable or invalid record is moved aside, so a
    /// crash during an old version's non-atomic write cannot block every later start.
    pub fn recover_launch_record(path: &Path) -> Result<Option<LaunchRecord>, String> {
        let bytes = match fs::read(path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(format!("无法读取 {}：{error}", path.display())),
        };
        let text = String::from_utf8_lossy(&bytes);
        match parse_launch_record(text.trim_start_matches('\u{feff}')) {
            Ok(record) => Ok(Some(record)),
            Err(error) => {
                let aside = temp_sibling(path, "invalid");
                fs::rename(path, &aside).map_err(|e| {
                    format!(
                        "{error}；且无法移走损坏的 {}（{e}），请手动删除",
                        path.display()
                    )
                })?;
                Ok(None)
            }
        }
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum LaunchRecordOwner {
        /// Version 0, or a version 1 record written before the hub was launched.
        Unrecorded,
        /// The exact process instance the record names is still running.
        Alive(windows_process::ProcessIdentity),
        /// The PID is gone, or Windows reused it for a different process.
        Stale,
    }

    pub fn launch_record_owner(record: &LaunchRecord) -> LaunchRecordOwner {
        if record.pid == 0 || record.creation_time == 0 {
            return LaunchRecordOwner::Unrecorded;
        }
        match windows_process::process_identity(record.pid) {
            Some(identity) if identity.creation_time == record.creation_time => {
                LaunchRecordOwner::Alive(identity)
            }
            _ => LaunchRecordOwner::Stale,
        }
    }
}

#[cfg(any(feature = "portable", test))]
pub use launch_record::{
    launch_record_owner, recover_launch_record, write_launch_record, LaunchRecord,
    LaunchRecordOwner,
};
#[cfg(test)]
pub use launch_record::{parse_launch_record, validate_launch_record};

/// The repo this app was built from: `CARGO_MANIFEST_DIR\..\..` (desktop\src-tauri → repo).
#[cfg(any(not(feature = "portable"), test))]
pub fn built_from_repo() -> PathBuf {
    normalize(&Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join(".."))
}

/// Lexical normalisation (drops `.` and resolves `..`), so paths read cleanly in the error view.
#[cfg(any(not(feature = "portable"), test))]
pub fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::CurDir => {}
            Component::ParentDir => {
                if !out.pop() {
                    out.push("..");
                }
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

fn non_empty(value: Option<String>) -> Option<String> {
    value
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

fn parse_port(text: &str, source: &str) -> Result<u16, String> {
    match text.trim().parse::<u16>() {
        Ok(p) if p > 0 => Ok(p),
        _ => Err(format!("{source} 不是有效端口：{text}")),
    }
}

fn port_from_json(value: &Value) -> Result<Option<u16>, String> {
    match value {
        Value::Null => Ok(None),
        Value::Number(n) => match n.as_u64() {
            Some(p) if (1..=65535).contains(&p) => Ok(Some(p as u16)),
            _ => Err(format!("config.json 的 port 不是有效端口：{n}")),
        },
        Value::String(s) if s.trim().is_empty() => Ok(None),
        Value::String(s) => parse_port(s, "config.json 的 port").map(Some),
        other => Err(format!("config.json 的 port 不是有效端口：{other}")),
    }
}

/// Resolve each setting: env var, else config.json, else the default. The python default follows the
/// resolved repo (`<repo>\.venv\Scripts\python.exe`).
#[cfg(any(not(feature = "portable"), test))]
pub fn resolve(
    env: &dyn Fn(&str) -> Option<String>,
    file: &FileConfig,
    default_repo: &Path,
) -> Result<HubConfig, String> {
    let repo_dir = non_empty(env("AGENT_HUB_REPO"))
        .or_else(|| non_empty(file.repo_dir.clone()))
        .map(PathBuf::from)
        .unwrap_or_else(|| default_repo.to_path_buf());
    let python = non_empty(env("AGENT_HUB_PYTHON"))
        .or_else(|| non_empty(file.python.clone()))
        .map(PathBuf::from)
        .unwrap_or_else(|| repo_dir.join(".venv").join("Scripts").join("python.exe"));
    let port = match non_empty(env("AGENT_HUB_PORT")) {
        Some(text) => parse_port(&text, "AGENT_HUB_PORT")?,
        None => match &file.port {
            Some(v) => port_from_json(v)?.unwrap_or(DEFAULT_PORT),
            None => DEFAULT_PORT,
        },
    };
    Ok(HubConfig {
        repo_dir,
        python,
        port,
    })
}

/// Portable configuration never consults the installed edition's environment or repository.
#[cfg(feature = "portable")]
pub fn resolve_portable(file: &FileConfig, resources: &Path) -> Result<HubConfig, String> {
    Ok(HubConfig {
        repo_dir: resources.to_path_buf(),
        python: non_empty(file.python.clone()).map(PathBuf::from).unwrap_or_default(),
        port: file.port.as_ref().map(port_from_json).transpose()?.flatten().unwrap_or(DEFAULT_PORT),
    })
}

#[cfg(feature = "portable")]
pub fn select_port(file: &FileConfig) -> Result<u16, String> {
    let explicit = file.port.as_ref().map(port_from_json).transpose()?.flatten();
    // Portable mode owns one configured endpoint. Silently moving to 4445 would allow a second
    // Hub to open the same data directory while the original Hub is still alive.
    Ok(explicit.unwrap_or(DEFAULT_PORT))
}

#[cfg(feature = "portable")]
fn port_owner_text(port: u16) -> String {
    match crate::windows_process::listening_pid(port) {
        Some(pid) => {
            let name = crate::windows_process::process_name(pid)
                .map(|name| format!("（{name}）"))
                .unwrap_or_default();
            format!("PID {pid}{name}")
        }
        None => "另一个程序".to_string(),
    }
}

/// Fail closed: the caller is about to launch into `port`, so a listener now means another program
/// won the race. The message names it; the caller must not attach to it.
#[cfg(feature = "portable")]
pub fn require_free_port(port: u16) -> Result<(), String> {
    if crate::windows_process::listening_pid(port).is_some() {
        return Err(format!("端口 {port} 已被{}占用", port_owner_text(port)));
    }
    Ok(())
}

/// Both launch inputs must exist; the message names the missing path.
pub fn validate(cfg: &HubConfig) -> Result<(), String> {
    let server = cfg.server_py();
    if !server.is_file() {
        return Err(format!(
            "找不到 Hub 入口 server.py：{}（可用 AGENT_HUB_REPO 或 config.json 的 repo_dir 指定仓库）",
            server.display()
        ));
    }
    if !cfg.python.is_file() {
        return Err(format!(
            "找不到 Python：{}（可用 AGENT_HUB_PYTHON 或 config.json 的 python 指定）",
            cfg.python.display()
        ));
    }
    Ok(())
}

fn env_dir(var: &str) -> PathBuf {
    std::env::var_os(var)
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
}

/// `%APPDATA%\mcp-agent-hub-desktop\config.json`
pub fn config_file_path() -> PathBuf {
    env_dir("APPDATA").join(APP_DIR_NAME).join("config.json")
}

/// `%LOCALAPPDATA%\mcp-agent-hub-desktop\logs`
pub fn log_dir() -> PathBuf {
    env_dir("LOCALAPPDATA").join(APP_DIR_NAME).join("logs")
}

#[cfg(feature = "portable")]
pub fn portable_root() -> PathBuf {
    env_dir("LOCALAPPDATA").join(APP_DIR_NAME).join("portable")
}

/// Load and resolve the live configuration (process env + config.json if present).
pub fn load_file() -> Result<FileConfig, String> {
    let path = config_file_path();
    match std::fs::read(&path) {
        Ok(bytes) => {
            let text = String::from_utf8_lossy(&bytes);
            parse_file_config(text.trim_start_matches('\u{feff}'))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(FileConfig::default()),
        Err(e) => Err(format!("无法读取 {}：{e}", path.display())),
    }
}

#[cfg(not(feature = "portable"))]
pub fn load() -> Result<HubConfig, String> {
    let file = load_file()?;
    let env = |name: &str| std::env::var(name).ok();
    resolve(&env, &file, &built_from_repo())
}

fn encode_query_value(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for b in value.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |name: &str| map.get(name).cloned()
    }

    fn repo() -> PathBuf {
        PathBuf::from(r"C:\built\repo")
    }

    #[test]
    fn defaults_when_nothing_is_set() {
        let cfg = resolve(&env_of(&[]), &FileConfig::default(), &repo()).unwrap();
        assert_eq!(cfg.repo_dir, repo());
        assert_eq!(
            cfg.python,
            repo().join(".venv").join("Scripts").join("python.exe")
        );
        assert_eq!(cfg.port, 4444);
    }

    #[test]
    fn file_overrides_defaults() {
        let file = parse_file_config(
            r#"{"repo_dir": "D:\\hub", "python": "D:\\py\\python.exe", "port": 4500}"#,
        )
        .unwrap();
        let cfg = resolve(&env_of(&[]), &file, &repo()).unwrap();
        assert_eq!(cfg.repo_dir, PathBuf::from(r"D:\hub"));
        assert_eq!(cfg.python, PathBuf::from(r"D:\py\python.exe"));
        assert_eq!(cfg.port, 4500);
    }

    #[test]
    fn env_overrides_file_per_key() {
        let file = parse_file_config(
            r#"{"repo_dir": "D:\\hub", "python": "D:\\py\\python.exe", "port": 4500}"#,
        )
        .unwrap();
        let env = env_of(&[("AGENT_HUB_PORT", "4465"), ("AGENT_HUB_REPO", r"E:\wt")]);
        let cfg = resolve(&env, &file, &repo()).unwrap();
        assert_eq!(cfg.repo_dir, PathBuf::from(r"E:\wt"));
        assert_eq!(
            cfg.python,
            PathBuf::from(r"D:\py\python.exe"),
            "python still from the file"
        );
        assert_eq!(cfg.port, 4465);
    }

    #[test]
    fn python_default_follows_the_resolved_repo() {
        let cfg = resolve(
            &env_of(&[("AGENT_HUB_REPO", r"E:\wt")]),
            &FileConfig::default(),
            &repo(),
        )
        .unwrap();
        assert_eq!(cfg.python, PathBuf::from(r"E:\wt\.venv\Scripts\python.exe"));
    }

    #[test]
    fn blank_values_are_ignored() {
        let file = parse_file_config(r#"{"repo_dir": "  ", "port": ""}"#).unwrap();
        let env = env_of(&[("AGENT_HUB_PYTHON", ""), ("AGENT_HUB_PORT", " ")]);
        let cfg = resolve(&env, &file, &repo()).unwrap();
        assert_eq!(cfg.repo_dir, repo());
        assert_eq!(cfg.port, 4444);
    }

    #[test]
    fn port_accepts_a_string_in_the_file() {
        let file = parse_file_config(r#"{"port": "4470"}"#).unwrap();
        assert_eq!(resolve(&env_of(&[]), &file, &repo()).unwrap().port, 4470);
    }

    #[test]
    fn invalid_ports_are_errors() {
        assert!(resolve(
            &env_of(&[("AGENT_HUB_PORT", "abc")]),
            &FileConfig::default(),
            &repo()
        )
        .is_err());
        assert!(resolve(
            &env_of(&[("AGENT_HUB_PORT", "0")]),
            &FileConfig::default(),
            &repo()
        )
        .is_err());
        assert!(resolve(
            &env_of(&[("AGENT_HUB_PORT", "70000")]),
            &FileConfig::default(),
            &repo()
        )
        .is_err());
        let file = parse_file_config(r#"{"port": 99999}"#).unwrap();
        assert!(resolve(&env_of(&[]), &file, &repo()).is_err());
        let file = parse_file_config(r#"{"port": true}"#).unwrap();
        assert!(resolve(&env_of(&[]), &file, &repo()).is_err());
    }

    #[test]
    fn unknown_keys_and_bad_json() {
        assert!(parse_file_config(r#"{"other": 1}"#).is_ok());
        assert!(parse_file_config("{not json").is_err());
    }

    #[test]
    fn built_from_repo_points_at_the_repo_root() {
        let root = built_from_repo();
        assert!(root.join("server.py").is_file(), "{}", root.display());
        assert!(!root.to_string_lossy().contains(".."));
    }

    #[test]
    fn normalize_resolves_parent_components() {
        assert_eq!(
            normalize(Path::new(r"C:\a\b\..\c\.\d")),
            PathBuf::from(r"C:\a\c\d")
        );
    }

    #[test]
    fn validate_names_the_missing_path() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = HubConfig {
            repo_dir: dir.path().to_path_buf(),
            python: dir.path().join("python.exe"),
            port: 4465,
        };
        let err = validate(&cfg).unwrap_err();
        assert!(
            err.contains(&cfg.server_py().display().to_string()),
            "{err}"
        );
        std::fs::write(cfg.server_py(), "").unwrap();
        let err = validate(&cfg).unwrap_err();
        assert!(err.contains(&cfg.python.display().to_string()), "{err}");
        std::fs::write(&cfg.python, "").unwrap();
        assert!(validate(&cfg).is_ok());
    }

    #[test]
    fn console_url_encodes_the_session() {
        let cfg = HubConfig {
            repo_dir: repo(),
            python: repo(),
            port: 4465,
        };
        assert_eq!(cfg.console_url(None), "http://127.0.0.1:4465/");
        assert_eq!(
            cfg.console_url(Some("ab-12")),
            "http://127.0.0.1:4465/?session=ab-12"
        );
        assert_eq!(
            cfg.console_url(Some("a b&c")),
            "http://127.0.0.1:4465/?session=a%20b%26c"
        );
    }

    #[cfg(feature = "portable")]
    #[test]
    fn portable_ignores_external_repository_and_has_its_own_identity() {
        let file = parse_file_config(r#"{"repo_dir":"E:\\old-hub","port":4500}"#).unwrap();
        let cfg = resolve_portable(&file, &repo()).unwrap();
        assert_eq!(cfg.repo_dir, repo());
        assert_eq!(cfg.port, 4500);
        assert_eq!(APP_DIR_NAME, "mcp-agent-hub-portable");
    }

    #[cfg(feature = "portable")]
    #[test]
    fn portable_keeps_the_configured_port_instead_of_falling_back() {
        let file = parse_file_config(r#"{}"#).unwrap();
        assert_eq!(select_port(&file).unwrap(), DEFAULT_PORT);
        let file = parse_file_config(r#"{"port": 4501}"#).unwrap();
        assert_eq!(select_port(&file).unwrap(), 4501);
    }

    fn launch_record(dir: &std::path::Path) -> LaunchRecord {
        LaunchRecord::prepared(
            dir.join("python.exe"),
            dir.join("resources").join("payload"),
            4500,
        )
    }

    #[test]
    fn launch_record_reads_the_legacy_shape_as_version_zero() {
        let text = r#"{"python":"C:\\hub\\python.exe","repo_dir":"C:\\hub\\resources","port":4444}"#;
        let record = parse_launch_record(text).unwrap();
        assert_eq!(record.version, 0);
        assert_eq!(record.port, 4444);
        assert_eq!(record.pid, 0);
        assert_eq!(record.creation_time, 0);
        assert_eq!(record.shutdown_token, None);
    }

    #[test]
    fn launch_record_without_app_fields_has_no_owner_app() {
        // Both a version 0 record and a version 1 record written before the hub was launched
        // predate the app fields; they must still parse and simply report no owner app.
        let legacy =
            r#"{"python":"C:\\hub\\python.exe","repo_dir":"C:\\hub\\resources","port":4444}"#;
        let record = parse_launch_record(legacy).unwrap();
        assert_eq!(record.app_pid, None);
        assert_eq!(record.app_creation_time, None);
        assert_eq!(record.owner_app_identity(), None);
        assert_eq!(record.owner_app_pid_if_alive(), None);
        assert_eq!(record.owner_app_pid_without_identity(), None);

        let prepared = r#"{"version":1,"python":"C:\\hub\\python.exe","repo_dir":"C:\\hub\\resources","port":4444,"state":"prepared"}"#;
        let record = parse_launch_record(prepared).unwrap();
        assert_eq!(record.owner_app_identity(), None);
    }

    #[test]
    fn launch_record_reads_an_old_app_pid_without_a_creation_time() {
        // A record from a build that pinned only the app PID stays readable: moving it aside
        // could destroy a record a live app is still using.
        let text = r#"{"version":1,"python":"C:\\hub\\python.exe","repo_dir":"C:\\hub\\resources","port":4500,"pid":0,"creation_time":0,"app_pid":777}"#;
        let record = parse_launch_record(text).unwrap();
        assert_eq!(record.app_pid, Some(777));
        assert_eq!(record.app_creation_time, None);
        assert_eq!(record.owner_app_identity(), None);
        assert_eq!(record.owner_app_pid_if_alive(), None);
        assert_eq!(record.owner_app_pid_without_identity(), Some(777));

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("launch.json");
        std::fs::write(&path, text).unwrap();
        assert_eq!(recover_launch_record(&path).unwrap(), Some(record));
        assert!(path.exists(), "the record is kept, not moved aside");
    }

    #[test]
    fn owner_app_identity_distinguishes_this_app_from_a_reused_pid() {
        let dir = tempfile::tempdir().unwrap();
        let record = launch_record(dir.path())
            .with_runtime_metadata(std::process::id(), dir.path().join("data"), "test")
            .unwrap();
        let identity = record.owner_app_identity().unwrap();
        assert_eq!(identity.pid, std::process::id());
        assert_eq!(record.owner_app_pid_if_alive(), Some(identity.pid));
        // Same number, different creation time: Windows reused the PID for another program.
        let mut reused = record.clone();
        reused.app_creation_time = Some(identity.creation_time + 1);
        assert_eq!(reused.owner_app_pid_if_alive(), None);
        assert_eq!(reused.owner_app_pid_without_identity(), None);
    }

    #[test]
    fn launch_record_carries_a_versioned_process_identity() {
        let text = r#"{"version":1,"python":"C:\\hub\\python.exe","repo_dir":"C:\\hub\\resources","port":"4501","pid":12,"creation_time":99,"shutdown_token":"ab12"}"#;
        let record = parse_launch_record(text).unwrap();
        assert_eq!(record.version, 1);
        assert_eq!(record.port, 4501);
        assert_eq!(record.pid, 12);
        assert_eq!(record.creation_time, 99);
        assert_eq!(record.shutdown_token.as_deref(), Some("ab12"));
        assert_eq!(launch_record_owner(&record), LaunchRecordOwner::Stale);
    }

    #[test]
    fn launch_record_rejects_newer_versions_and_inconsistent_records() {
        for text in [
            r#"{"version":2,"python":"C:\\p.exe","repo_dir":"C:\\r","port":4444}"#,
            r#"{"python":"relative\\python.exe","repo_dir":"C:\\r","port":4444}"#,
            r#"{"python":"C:\\p.exe","repo_dir":"relative","port":4444}"#,
            r#"{"python":"C:\\p.exe","repo_dir":"C:\\r","port":0}"#,
            r#"{"python":"C:\\p.exe","repo_dir":"C:\\r"}"#,
            r#"{"version":1,"python":"C:\\p.exe","repo_dir":"C:\\r","port":4444,"pid":7}"#,
            r#"{"version":1,"python":"C:\\p.exe","repo_dir":"C:\\r","port":4444,"creation_time":7}"#,
        ] {
            assert!(parse_launch_record(text).is_err(), "{text}");
        }
    }

    #[test]
    fn launch_record_owner_matches_this_process_and_not_a_reused_pid() {
        let dir = tempfile::tempdir().unwrap();
        let mut record = launch_record(dir.path());
        let pid = std::process::id();
        record = record.with_owner(pid, Some("token".into())).unwrap();
        assert_eq!(record.pid, pid);
        assert!(matches!(
            launch_record_owner(&record),
            LaunchRecordOwner::Alive(identity) if identity.pid == pid
        ));
        record.creation_time += 1;
        assert_eq!(launch_record_owner(&record), LaunchRecordOwner::Stale);
        record.pid = 0;
        record.creation_time = 0;
        assert_eq!(launch_record_owner(&record), LaunchRecordOwner::Unrecorded);
    }

    #[test]
    fn launch_record_write_replaces_atomically_and_recovers_a_bad_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("launch.json");
        let record = launch_record(dir.path());
        write_launch_record(&path, &record).unwrap();
        assert_eq!(recover_launch_record(&path).unwrap(), Some(record.clone()));
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.contains("\"version\":1"), "{text}");
        assert!(!text.contains("shutdown_token"), "{text}");

        // A rejected record never touches the file that is already published.
        let mut invalid = record.clone();
        invalid.port = 0;
        assert!(write_launch_record(&path, &invalid).is_err());
        assert_eq!(std::fs::read_to_string(&path).unwrap(), text);

        std::fs::write(&path, "{not json").unwrap();
        assert_eq!(recover_launch_record(&path).unwrap(), None);
        assert!(!path.exists());
        assert!(std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(Result::ok)
            .any(|entry| entry.file_name().to_string_lossy().contains("invalid")));
    }
}
