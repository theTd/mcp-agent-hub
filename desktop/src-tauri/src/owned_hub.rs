//! The hub process this app owns, with no UI: probe the port, launch `server.py` in a kill-on-close
//! job, wait for `/api/hub/info`, stop gracefully. hub_supervisor drives these for the app; the
//! ignored tests below drive the same code headless against a real hub.

use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use crate::hub_api::{HubApi, HubInfo};
use crate::hub_config::HubConfig;
use crate::hub_log;
use crate::windows_process::{self, HubJob};

pub const READY_POLL: Duration = Duration::from_millis(500);
pub const READY_TIMEOUT: Duration = Duration::from_secs(60);
pub const GRACEFUL_STOP_WAIT: Duration = Duration::from_secs(10);
pub const PORT_RELEASE_WAIT: Duration = Duration::from_secs(10);
const PROBE_TIMEOUT: Duration = Duration::from_millis(1500);

/// A log sink usable across awaits.
pub type LogFn<'a> = &'a (dyn Fn(&str) + Sync);

pub fn exit_text(status: ExitStatus) -> String {
    match status.code() {
        Some(c) if c < 0 => format!("{c} / 0x{:08X}", c as u32),
        Some(c) => c.to_string(),
        None => "?".to_string(),
    }
}

/// What is on the port when the app starts.
#[derive(Debug)]
pub enum PortProbe {
    /// An MCP Agent Hub already answers: attach to it as external.
    ExternalHub(HubInfo),
    /// Something else listens there.
    Busy { pid: u32, name: Option<String> },
    /// Free: launch an owned hub.
    Free,
}

pub async fn probe_port(api: &HubApi, port: u16) -> PortProbe {
    if let Ok(info) = api.info(PROBE_TIMEOUT).await {
        if info.is_agent_hub() {
            return PortProbe::ExternalHub(info);
        }
    }
    match windows_process::listening_pid(port) {
        Some(pid) => PortProbe::Busy {
            pid,
            name: windows_process::process_name(pid),
        },
        None => PortProbe::Free,
    }
}

pub async fn wait_port_free(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if windows_process::listening_pid(port).is_none() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

pub enum ReadyError {
    Cancelled,
    Failed(String),
}

/// Poll `/api/hub/info` every 500 ms for up to 60 s. `exited` reports an early exit of the launched
/// process; `cancel` lets a quit or restart interrupt the wait.
pub async fn wait_ready(
    api: &HubApi,
    cancel: &AtomicBool,
    mut exited: impl FnMut() -> Option<String>,
) -> Result<HubInfo, ReadyError> {
    let deadline = Instant::now() + READY_TIMEOUT;
    loop {
        if cancel.load(Ordering::SeqCst) {
            return Err(ReadyError::Cancelled);
        }
        if let Some(code) = exited() {
            return Err(ReadyError::Failed(format!(
                "Hub 进程启动后退出了（退出码 {code}）。"
            )));
        }
        if let Ok(info) = api.info(Duration::from_secs(2)).await {
            if info.is_agent_hub() {
                return Ok(info);
            }
        }
        if Instant::now() >= deadline {
            return Err(ReadyError::Failed(format!(
                "Hub 在 {} 秒内没有响应 /api/hub/info。",
                READY_TIMEOUT.as_secs()
            )));
        }
        tokio::time::sleep(READY_POLL).await;
    }
}

/// How a graceful stop went.
#[derive(Debug)]
pub struct StopReport {
    /// The hub exited by itself within the grace period (no job termination needed for it).
    pub graceful: bool,
    pub exit: Option<String>,
}

/// The hub process this app started, in its own kill-on-close job.
pub struct OwnedHub {
    child: Child,
    job: HubJob,
    token: String,
    port: u16,
}

impl OwnedHub {
    /// `python server.py` with cwd = repo, `HUB_PORT`, a per-launch `HUB_SHUTDOWN_TOKEN`, no console
    /// window, stdout+stderr appended to `<log_dir>\hub.log` (rotated first when past 10 MB).
    pub fn launch(cfg: &HubConfig, log_dir: &Path, log: LogFn) -> Result<Self, String> {
        let log_path = hub_log::hub_log_path(log_dir);
        std::fs::create_dir_all(log_dir)
            .map_err(|e| format!("无法创建日志目录 {}：{e}", log_dir.display()))?;
        if let Ok(true) = hub_log::rotate_if_needed(&log_path, hub_log::ROTATE_LIMIT_BYTES) {
            log("hub.log passed 10 MB: kept as hub.log.1");
        }
        let mut out = hub_log::open_append(&log_path)
            .map_err(|e| format!("无法打开 {}：{e}", log_path.display()))?;
        hub_log::write_launch_banner(&mut out, cfg.port);
        let err = out.try_clone().map_err(|e| e.to_string())?;
        let token = windows_process::random_token().map_err(|e| e.to_string())?;
        let job = HubJob::new_kill_on_close().map_err(|e| format!("无法创建 Job Object：{e}"))?;
        let mut cmd = Command::new(&cfg.python);
        cmd.arg(cfg.server_py())
            .current_dir(&cfg.repo_dir)
            .env("HUB_PORT", cfg.port.to_string())
            .env("HUB_SHUTDOWN_TOKEN", &token)
            .stdin(Stdio::null())
            .stdout(out)
            .stderr(err);
        let child = windows_process::spawn_in_job(&mut cmd, &job).map_err(|e| {
            format!(
                "{} {}：{e}",
                cfg.python.display(),
                cfg.server_py().display()
            )
        })?;
        log(&format!(
            "hub launched: pid {} = {} {} (cwd {}, port {})",
            child.id(),
            cfg.python.display(),
            cfg.server_py().display(),
            cfg.repo_dir.display(),
            cfg.port
        ));
        Ok(OwnedHub {
            child,
            job,
            token,
            port: cfg.port,
        })
    }

    /// PID of the launched process (the venv launcher; the real python is its child, also in the job).
    #[cfg(test)]
    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    /// `Some(exit code)` once the launched process has exited.
    pub fn exit_text(&mut self) -> Option<String> {
        match self.child.try_wait() {
            Ok(Some(status)) => Some(exit_text(status)),
            _ => None,
        }
    }

    /// Kill everything in the job now (no graceful stop).
    pub fn terminate(self) {
        self.job.terminate();
    }

    /// `POST /api/hub/shutdown` with the token, up to 10 s for the process to exit, then terminate
    /// the job (whatever is left, including agents that outlived the hub) and wait for the port.
    pub async fn stop_gracefully(mut self, log: LogFn<'_>) -> StopReport {
        log(&format!("stopping hub pid {}", self.child.id()));
        match HubApi::new(self.port).shutdown(&self.token).await {
            Ok(()) => log("shutdown requested (graceful)"),
            Err(e) => log(&format!("shutdown request failed: {e}")),
        }
        let deadline = Instant::now() + GRACEFUL_STOP_WAIT;
        let mut exit = None;
        while Instant::now() < deadline {
            if let Some(code) = self.exit_text() {
                exit = Some(code);
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let graceful = exit.is_some();
        match &exit {
            Some(code) => log(&format!("hub exited ({code})")),
            None => log("hub still running after 10 s: terminating the job"),
        }
        self.job.terminate();
        for _ in 0..50 {
            if let Ok(Some(_)) = self.child.try_wait() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let port = self.port;
        drop(self);
        wait_port_free(port, PORT_RELEASE_WAIT).await;
        log("hub stopped");
        StopReport { graceful, exit }
    }
}

#[cfg(test)]
#[path = "owned_hub_live_tests.rs"]
mod tests;
