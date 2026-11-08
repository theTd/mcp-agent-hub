//! Where the hub lives and how it is launched: env first, then `%APPDATA%\mcp-agent-hub-desktop\config.json`,
//! then the defaults (the repo this app was built from, its `.venv` python, port 4444).

use std::path::{Component, Path, PathBuf};

use serde::Deserialize;
use serde_json::Value;

pub const DEFAULT_PORT: u16 = 4444;
pub const APP_DIR_NAME: &str = "mcp-agent-hub-desktop";

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

/// The repo this app was built from: `CARGO_MANIFEST_DIR\..\..` (desktop\src-tauri → repo).
pub fn built_from_repo() -> PathBuf {
    normalize(&Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join(".."))
}

/// Lexical normalisation (drops `.` and resolves `..`), so paths read cleanly in the error view.
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

/// Load and resolve the live configuration (process env + config.json if present).
pub fn load() -> Result<HubConfig, String> {
    let path = config_file_path();
    let file = match std::fs::read(&path) {
        Ok(bytes) => {
            let text = String::from_utf8_lossy(&bytes);
            parse_file_config(text.trim_start_matches('\u{feff}'))?
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => FileConfig::default(),
        Err(e) => return Err(format!("无法读取 {}：{e}", path.display())),
    };
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
}
