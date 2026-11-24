//! The hub process this app owns, with no UI: probe the port, launch `server.py` in a kill-on-close
//! job, wait for `/api/hub/info`, stop gracefully. hub_supervisor drives these for the app; the
//! ignored tests below drive the same code headless against a real hub.

#[cfg(feature = "portable")]
use std::fs::{File, OpenOptions};
use std::path::Path;
#[cfg(feature = "portable")]
use std::path::PathBuf;
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
/// A planned restart lets the hub archive live sessions and close its MCP pool first.
pub const GRACEFUL_RESTART_WAIT: Duration = Duration::from_secs(15);
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
    /// Whether the planned stop went through the graceful-restart endpoint. A failure there is no
    /// longer downgraded here (see `stop_for_graceful_restart`), so this is true on every report
    /// that is actually returned.
    pub graceful_restart_supported: bool,
}

/// The hub process this app started, in its own kill-on-close job.
pub struct OwnedHub {
    child: Child,
    job: HubJob,
    token: String,
    port: u16,
    #[cfg(feature = "portable")]
    _data_lock: File,
    #[cfg(feature = "portable")]
    launch_path: PathBuf,
}

impl OwnedHub {
    #[cfg(feature = "portable")]
    pub fn owns_server(&self, pid: u32) -> bool { self.job.contains_pid(pid) }

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
        #[cfg(feature = "portable")]
        let (data_lock, launch_path) = {
            let portable_root = crate::hub_config::portable_root();
            let data = portable_root.join("data");
            std::fs::create_dir_all(&data).map_err(|e| e.to_string())?;
            let data_lock_path = portable_root.join("data.lock");
            let data_lock = OpenOptions::new()
                .create(true)
                .truncate(false)
                .read(true)
                .write(true)
                .open(&data_lock_path)
                .map_err(|e| format!("无法打开便携版数据锁：{e}"))?;
            match data_lock.try_lock() {
                Ok(()) => {}
                Err(std::fs::TryLockError::WouldBlock) => {
                    return Err("便携版数据目录已被另一个 Hub 实例占用，请先关闭它或使用恢复按钮".into())
                }
                Err(e) => return Err(format!("无法取得便携版数据锁：{e}")),
            }
            cmd.env("HUB_DATA_DIR", &data)
                .env("HUB_PROFILES_PATH", &data.join("agent_profiles.json"))
                .env("HUB_API_URL", format!("http://127.0.0.1:{}", cfg.port))
                .env_remove("AGENT_HUB_REPO")
                .env_remove("AGENT_HUB_PYTHON")
                .env_remove("AGENT_HUB_PORT")
                .env_remove("PYTHONHOME")
                .env_remove("PYTHONPATH");
            (data_lock, portable_root.join("launch.json"))
        };
        let child = windows_process::spawn_in_job(&mut cmd, &job).map_err(|e| {
            format!(
                "{} {}：{e}",
                cfg.python.display(),
                cfg.server_py().display()
            )
        })?;
        #[cfg(feature = "portable")]
        {
            let record = crate::hub_config::LaunchRecord::prepared(
                cfg.python.clone(),
                cfg.repo_dir.clone(),
                cfg.port,
            )
            .with_owner(child.id(), Some(token.clone()))
            .and_then(|record| {
                record.with_runtime_metadata(
                    std::process::id(),
                    crate::hub_config::portable_root().join("data"),
                    "OwnedHub::launch",
                )
            })
            .map_err(|error| {
                job.terminate();
                format!("无法记录 Hub 所有权：{error}")
            })?;
            if let Err(error) = crate::hub_config::write_launch_record(&launch_path, &record) {
                job.terminate();
                return Err(format!("无法写入 Hub 所有权记录：{error}"));
            }
        }
        log(&format!(
            "hub launched: parent pid {} spawn path OwnedHub::launch, pid {} = {} {} (cwd {}, port {})",
            std::process::id(),
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
            #[cfg(feature = "portable")]
            _data_lock: data_lock,
            #[cfg(feature = "portable")]
            launch_path,
        })
    }

    /// PID of the launched process (the venv launcher; the real python is its child, also in the job).
    #[cfg(test)]
    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    /// The exact process instance of the launcher: its PID plus creation time, so a plan can name
    /// it and a later check can tell it from a reused PID.
    pub fn hub_identity(&self) -> Option<windows_process::ProcessIdentity> {
        windows_process::process_identity(self.child.id())
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
        // Keep the launch record until the next startup can verify that the process tree is gone.
        // Removing it before termination would hide exactly the orphan this record is meant to recover.
        self.job.terminate();
    }

    /// `POST /api/hub/shutdown` with the token, up to 10 s for the process to exit, then terminate
    /// the job (whatever is left, including agents that outlived the hub) and wait for the port.
    pub async fn stop_gracefully(self, log: LogFn<'_>) -> StopReport {
        log(&format!("stopping hub pid {}", self.child.id()));
        let api = HubApi::new(self.port);
        match api.shutdown(&self.token).await {
            Ok(()) => log("shutdown requested (graceful)"),
            Err(e) => log(&format!("shutdown request failed: {e}")),
        }
        self.finish_stop(GRACEFUL_STOP_WAIT, log).await
    }

    /// 优雅重启 / 重启并升级: `POST /api/hub/graceful-restart`, and only a confirmed acceptance
    /// (2xx) lets the stop proceed. **Fail-closed**: a 403/404/409, a network error or any other
    /// rejection returns the untouched hub to the caller together with the reason — this method
    /// never falls back to a plain `/shutdown`, never terminates the job and never removes
    /// `launch.json`, because the hub has most likely not archived `resume_on_start.json` and a
    /// stop there would discard every live session.
    ///
    /// After the confirmation the hub is expected to archive the live sessions, lock its
    /// `_stop_live_at` fence and exit; from there on the usual wait / terminate / port-release /
    /// launch-record cleanup applies.
    pub async fn stop_for_graceful_restart(
        self,
        mode: &str,
        restart_id: Option<&str>,
        log: LogFn<'_>,
    ) -> Result<StopReport, (Self, String)> {
        log(&format!(
            "stopping hub pid {} (planned restart, mode {mode})",
            self.child.id()
        ));
        let api = HubApi::new(self.port);
        match api.graceful_restart(&self.token, mode, restart_id).await {
            Ok(_) => log(&format!(
                "graceful restart accepted (mode {mode}, restart_id {})",
                restart_id.unwrap_or("none")
            )),
            Err(error) => {
                log(&format!(
                    "graceful restart request failed: {error}; keeping the hub running (fail-closed)"
                ));
                let reason = format!(
                    "Hub 未确认优雅重启（{error}）。为保证会话不丢失，本次已中止{}：\
                     当前 Hub（PID {}）与全部会话继续运行，未发送普通关停请求，也未结束任何进程。",
                    if mode == "replace-exe" { "升级" } else { "重启" },
                    self.child.id()
                );
                return Err((self, reason));
            }
        }
        Ok(self.finish_stop(GRACEFUL_RESTART_WAIT, log).await)
    }

    /// The shared tail of a planned stop: wait for the process to exit by itself, then terminate
    /// whatever is left in the job, wait for the port to be released and drop `launch.json`.
    async fn finish_stop(mut self, wait: Duration, log: LogFn<'_>) -> StopReport {
        let deadline = Instant::now() + wait;
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
            None => log(&format!(
                "hub still running after {} s: terminating the job",
                wait.as_secs()
            )),
        }
        self.job.terminate();
        for _ in 0..50 {
            if let Ok(Some(_)) = self.child.try_wait() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let port = self.port;
        #[cfg(feature = "portable")]
        let launch_path = self.launch_path.clone();
        drop(self);
        wait_port_free(port, PORT_RELEASE_WAIT).await;
        #[cfg(feature = "portable")]
        let _ = std::fs::remove_file(launch_path);
        log("hub stopped");
        StopReport {
            graceful,
            exit,
            graceful_restart_supported: true,
        }
    }
}

#[cfg(test)]
#[path = "owned_hub_live_tests.rs"]
mod tests;

/// P0-1: a rejected `/api/hub/graceful-restart` must never become a plain `/shutdown`, a job
/// termination or a lost `launch.json`; it has to hand the still-running hub back to the caller.
#[cfg(test)]
mod restart_guard_tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::{Shutdown, TcpListener, TcpStream};
    use std::os::windows::process::CommandExt;
    use std::process::{Child, Command, Stdio};
    use std::sync::atomic::AtomicBool;
    use std::sync::{Arc, Mutex};

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    /// Answer one request with `status`. Fully drained and half-closed on purpose: closing a socket
    /// that still holds unread request bytes makes Windows reset the connection, and the client
    /// would report a send error instead of the status line these tests assert on.
    fn serve_request(mut stream: TcpStream, status: u16, paths: &Arc<Mutex<Vec<String>>>) {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));
        let Ok(copy) = stream.try_clone() else {
            return;
        };
        let mut reader = BufReader::new(copy);
        let mut line = String::new();
        let _ = reader.read_line(&mut line);
        let path = line.split_whitespace().nth(1).unwrap_or("").to_string();
        paths.lock().unwrap().push(path);
        let mut length = 0usize;
        loop {
            let mut header = String::new();
            if reader.read_line(&mut header).is_err() || header.trim().is_empty() {
                break;
            }
            if let Some(value) = header.to_lowercase().strip_prefix("content-length:") {
                length = value.trim().parse().unwrap_or(0);
            }
        }
        let mut body = vec![0u8; length];
        let _ = reader.read_exact(&mut body);
        let text = "{\"detail\":\"test rejection\"}";
        let reply = format!(
            "HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",
            text.len()
        );
        let _ = stream.write_all(reply.as_bytes());
        let _ = stream.flush();
        let _ = stream.shutdown(Shutdown::Write);
        let mut sink = [0u8; 1024];
        while let Ok(n) = stream.read(&mut sink) {
            if n == 0 {
                break;
            }
        }
    }

    /// A socket that answers every request with one status and records the request paths it saw.
    struct FakeHub {
        port: u16,
        paths: Arc<Mutex<Vec<String>>>,
        stop: Arc<AtomicBool>,
        handle: Option<std::thread::JoinHandle<()>>,
    }

    impl FakeHub {
        fn new(status: u16) -> FakeHub {
            let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind a fake hub");
            let port = listener.local_addr().unwrap().port();
            let paths = Arc::new(Mutex::new(Vec::new()));
            let stop = Arc::new(AtomicBool::new(false));
            let seen = paths.clone();
            let stopping = stop.clone();
            let handle = std::thread::spawn(move || {
                // Blocking accept: the answer goes out the moment the kernel hands over a
                // connection, so a loaded machine cannot turn a rejection into a timeout.
                while !stopping.load(Ordering::SeqCst) {
                    match listener.accept() {
                        Ok((stream, _)) => serve_request(stream, status, &seen),
                        Err(_) => break,
                    }
                }
            });
            FakeHub {
                port,
                paths,
                stop,
                handle: Some(handle),
            }
        }
    }

    impl Drop for FakeHub {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            // Wake the blocking accept (the connection is dropped at once, so it costs no wait).
            let _ = TcpStream::connect(("127.0.0.1", self.port));
            if let Some(handle) = self.handle.take() {
                let _ = handle.join();
            }
        }
    }

    fn unused_port() -> u16 {
        // Bind and drop: nothing listens there afterwards, so a connect is refused.
        TcpListener::bind(("127.0.0.1", 0))
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
    }

    fn hub_stand_in(job: &HubJob) -> Child {
        let mut cmd = Command::new("cmd.exe");
        cmd.arg("/c")
            .arg("ping -n 45 127.0.0.1 >nul")
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        windows_process::spawn_in_job(&mut cmd, job).expect("spawn a stand-in hub")
    }

    fn owned_for_test(port: u16, launch_path: &Path) -> OwnedHub {
        let job = HubJob::new_kill_on_close().expect("job object");
        let child = hub_stand_in(&job);
        #[cfg(feature = "portable")]
        let data_lock = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(launch_path.with_file_name("data.lock"))
            .expect("data lock");
        #[cfg(not(feature = "portable"))]
        let _ = launch_path;
        OwnedHub {
            child,
            job,
            token: "test-token".to_string(),
            port,
            #[cfg(feature = "portable")]
            _data_lock: data_lock,
            #[cfg(feature = "portable")]
            launch_path: launch_path.to_path_buf(),
        }
    }

    /// Runs one failed graceful restart and asserts the hub was handed back still running.
    fn fail_closed_reason(port: u16, launch_path: &Path) -> Option<String> {
        let owned = owned_for_test(port, launch_path);
        let log = |line: &str| eprintln!("{line}");
        let result = tauri::async_runtime::block_on(owned.stop_for_graceful_restart(
            "hub",
            Some("test-restart"),
            &log,
        ));
        match result {
            Ok(_) => None,
            Err((mut owned, reason)) => {
                assert!(
                    owned.exit_text().is_none(),
                    "the hub must keep running after a rejected graceful restart: {reason}"
                );
                Some(reason)
            }
        }
    }

    #[test]
    fn a_rejected_graceful_restart_never_falls_back_to_a_plain_shutdown() {
        for status in [404u16, 403, 409] {
            let hub = FakeHub::new(status);
            let temp = tempfile::tempdir().unwrap();
            let launch_path = temp.path().join("launch.json");
            std::fs::write(&launch_path, "{}").unwrap();

            let reason = fail_closed_reason(hub.port, &launch_path)
                .unwrap_or_else(|| panic!("a {status} answer must never count as accepted"));

            assert!(
                reason.contains(status.to_string().as_str()),
                "the reason names the status: {reason}"
            );
            assert!(reason.contains("中止"), "{reason}");
            assert!(
                launch_path.is_file(),
                "launch.json is removed only after a confirmed graceful stop"
            );
            let paths = hub.paths.lock().unwrap().clone();
            assert_eq!(
                paths,
                vec!["/api/hub/graceful-restart".to_string()],
                "no /shutdown fallback and no second request"
            );
        }
    }

    #[test]
    fn an_unreachable_hub_is_left_alone_too() {
        let port = unused_port();
        let temp = tempfile::tempdir().unwrap();
        let launch_path = temp.path().join("launch.json");
        std::fs::write(&launch_path, "{}").unwrap();

        let reason = fail_closed_reason(port, &launch_path)
            .expect("a refused connection must fail closed instead of stopping the hub");

        assert!(reason.contains("中止"), "{reason}");
        assert!(launch_path.is_file());
    }
}
