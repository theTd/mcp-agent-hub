//! Portable source payload and cancellable environment preparation. Python itself is never embedded.
use crate::{
    hub_config::{self, HubConfig},
    hub_log,
    windows_process::{self, HubJob},
};
use serde::Deserialize;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

include!(concat!(env!("OUT_DIR"), "/portable_resources.rs"));

fn unpack(root: &Path) -> Result<PathBuf, String> {
    let dest = root.join("resources").join(PAYLOAD_ID);
    if FILES
        .iter()
        .all(|(name, bytes)| fs::read(dest.join(name)).is_ok_and(|b| b == *bytes))
    {
        return Ok(dest);
    }
    // Recreate only our embedded files; no user data lives in this directory.
    for (name, bytes) in FILES {
        let path = dest.join(name);
        fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
        fs::write(path, bytes).map_err(|e| format!("无法展开程序资源：{e}"))?;
    }
    Ok(dest)
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

pub async fn prepare(
    progress: impl Fn(&str, &str),
    cancel: &AtomicBool,
) -> Result<HubConfig, String> {
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
            Err(e) => return Err(e.to_string()),
        }
    }
    let _ = fs::remove_file(root.join("launch.json"));
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
                ));
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
    fs::write(root.join("launch.json"), serde_json::to_vec(&serde_json::json!({
        "python":cfg.python, "repo_dir":cfg.repo_dir, "port":cfg.port
    })).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    Ok(cfg)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn payload_is_complete_clean_and_repairs_missing_assets() {
        let temp = tempfile::tempdir().unwrap();
        let path = unpack(temp.path()).unwrap();
        for name in [
            "server.py",
            "native_session_launch.py",
            "hub_poll.py",
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
    }
}
