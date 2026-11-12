//! The app owns the hub: start it (or attach to one already on the port), wait until it answers,
//! show the splash / error view or the console, stop it gracefully, restart it, take over an
//! external one, and keep a 5 s watch for the tray and for a hub that died.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Manager, Url};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use crate::hub_api::{count_sessions, HubApi, SessionCounts};
#[cfg(not(feature = "portable"))]
use crate::hub_api::HubInfo;
use crate::hub_config::{self, HubConfig};
use crate::hub_log;
use crate::owned_hub::{self, OwnedHub, ReadyError, PORT_RELEASE_WAIT};
#[cfg(not(feature = "portable"))]
use crate::owned_hub::PortProbe;
use crate::tray_menu;
use crate::windows_process;

const STATUS_INTERVAL: Duration = Duration::from_secs(5);
const EXTERNAL_MISSES_BEFORE_ERROR: u32 = 3;
/// Checks in a row an owned hub may run without listening on its port before the error view.
const NOT_LISTENING_BEFORE_ERROR: u32 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HubMode {
    Starting,
    Owned,
    External,
    Failed,
    Stopping,
}

/// What the bundled splash page shows (it polls this through the `splash_state` command).
#[derive(Debug, Clone, Serialize)]
pub struct SplashView {
    pub phase: String,
    pub message: String,
    pub detail: String,
    pub log_tail: String,
    pub log_path: String,
}

struct ShellState {
    mode: HubMode,
    config: Option<HubConfig>,
    owned: Option<OwnedHub>,
    splash: SplashView,
    counts: SessionCounts,
    pending_session: Option<String>,
}

/// A hub that answers: who to talk to, and the generation the caller's cursor belongs to.
pub struct ReadyTarget {
    pub generation: u64,
    pub api: HubApi,
}

pub struct HubShell {
    state: Mutex<ShellState>,
    ops: tokio::sync::Mutex<()>,
    generation: tokio::sync::watch::Sender<u64>,
    cancel_start: AtomicBool,
    close_hint_shown: AtomicBool,
    splash_url: Mutex<Option<Url>>,
    log_dir: PathBuf,
}

pub fn reveal_main_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
        if let Ok(hwnd) = w.hwnd() {
            let raw = hwnd.0;
            // SAFETY: `raw` is the live main window HWND; restore if minimized and pulse topmost
            // so Windows brings the window in front of other windows when activated from a toast/tray.
            unsafe {
                use windows_sys::Win32::UI::WindowsAndMessaging::{
                    BringWindowToTop, IsIconic, SetForegroundWindow, SetWindowPos, ShowWindow,
                    HWND_NOTOPMOST, HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW,
                    SW_RESTORE, SW_SHOW,
                };
                if IsIconic(raw) != 0 {
                    ShowWindow(raw, SW_RESTORE);
                } else {
                    ShowWindow(raw, SW_SHOW);
                }
                let flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW;
                SetWindowPos(raw, HWND_TOPMOST, 0, 0, 0, 0, flags);
                SetForegroundWindow(raw);
                BringWindowToTop(raw);
                SetWindowPos(raw, HWND_NOTOPMOST, 0, 0, 0, 0, flags);
            }
        }
    }
}

async fn confirm(app: &AppHandle, title: &str, message: &str, ok_label: &str) -> bool {
    let (tx, rx) = tokio::sync::oneshot::channel();
    app.dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            ok_label.to_string(),
            "取消".to_string(),
        ))
        .show(move |answer| {
            let _ = tx.send(answer);
        });
    rx.await.unwrap_or(false)
}

impl HubShell {
    pub fn new(log_dir: PathBuf) -> Arc<Self> {
        let (generation, _) = tokio::sync::watch::channel(0u64);
        let log_path = hub_log::hub_log_path(&log_dir).display().to_string();
        Arc::new(HubShell {
            state: Mutex::new(ShellState {
                mode: HubMode::Starting,
                config: None,
                owned: None,
                splash: SplashView {
                    phase: "starting".into(),
                    message: "正在启动 Hub…".into(),
                    detail: String::new(),
                    log_tail: String::new(),
                    log_path,
                },
                counts: SessionCounts::default(),
                pending_session: None,
            }),
            ops: tokio::sync::Mutex::new(()),
            generation,
            cancel_start: AtomicBool::new(false),
            close_hint_shown: AtomicBool::new(false),
            splash_url: Mutex::new(None),
            log_dir,
        })
    }

    fn st(&self) -> MutexGuard<'_, ShellState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn log(&self, line: &str) {
        hub_log::desktop_log(&self.log_dir, line);
    }

    pub fn log_dir(&self) -> &Path {
        &self.log_dir
    }

    pub fn subscribe_generation(&self) -> tokio::sync::watch::Receiver<u64> {
        self.generation.subscribe()
    }

    fn bump_generation(&self) {
        self.generation.send_modify(|g| *g += 1);
    }

    pub fn mode(&self) -> HubMode {
        self.st().mode
    }

    pub fn counts(&self) -> SessionCounts {
        self.st().counts.clone()
    }

    pub fn splash_view(&self) -> SplashView {
        self.st().splash.clone()
    }

    pub fn set_splash_url(&self, url: Url) {
        // A fresh WebView can still report about:blank during setup. Never save it as
        // the splash destination, or progress updates will navigate away from the UI.
        if url.scheme() != "tauri" && url.host_str() != Some("tauri.localhost") {
            return;
        }
        *self.splash_url.lock().unwrap_or_else(|e| e.into_inner()) = Some(url);
    }

    /// True only the first time the window is closed to the tray.
    pub fn first_close_hint(&self) -> bool {
        !self.close_hint_shown.swap(true, Ordering::SeqCst)
    }

    pub fn ready_target(&self) -> Option<ReadyTarget> {
        let st = self.st();
        match (st.mode, &st.config) {
            (HubMode::Owned | HubMode::External, Some(cfg)) => Some(ReadyTarget {
                generation: *self.generation.borrow(),
                api: HubApi::new(cfg.port),
            }),
            _ => None,
        }
    }

    fn ready_config(&self) -> Option<HubConfig> {
        let st = self.st();
        match st.mode {
            HubMode::Owned | HubMode::External => st.config.clone(),
            _ => None,
        }
    }

    /// Show the window; when the hub answers, load the console (optionally on one session).
    pub fn open_console(&self, app: &AppHandle, session_id: Option<&str>) {
        if let Some(sid) = session_id {
            self.st().pending_session = Some(sid.to_string());
        }
        reveal_main_window(app);
        let Some(cfg) = self.ready_config() else {
            return;
        };
        let target_session = self.st().pending_session.take();
        let effective_session = session_id.or(target_session.as_deref());
        if let (Some(w), Ok(url)) = (
            app.get_webview_window("main"),
            Url::parse(&cfg.console_url(effective_session)),
        ) {
            let current = w.url().ok();
            let on_console = current
                .as_ref()
                .map(|c| c.origin() == url.origin())
                .unwrap_or(false);
            if on_console {
                if let Some(sid) = effective_session {
                    self.log(&format!("activate session in console: {sid}"));
                    let script = format!(
                        "if (typeof window.openHubSession === 'function') {{ window.openHubSession({sid:?}); }} else {{ location.href = {url_str:?}; }}",
                        sid = sid,
                        url_str = url.as_str()
                    );
                    let _ = w.eval(&script);
                }
            } else {
                self.log(&format!("navigate main window: {url}"));
                let _ = w.navigate(url);
            }
        }
    }

    fn navigate_splash(&self, app: &AppHandle) {
        let Some(splash) = self
            .splash_url
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
        else {
            return;
        };
        if let Some(w) = app.get_webview_window("main") {
            let on_splash = w
                .url()
                .map(|c| c.origin() == splash.origin())
                .unwrap_or(false);
            if !on_splash {
                let _ = w.navigate(splash);
            }
        }
    }

    fn set_starting(&self, app: &AppHandle, message: &str, detail: &str) {
        {
            let mut st = self.st();
            st.mode = HubMode::Starting;
            st.splash.phase = "starting".into();
            st.splash.message = message.into();
            st.splash.detail = detail.into();
            st.splash.log_tail.clear();
        }
        self.navigate_splash(app);
        tray_menu::refresh(app);
    }

    fn fail(&self, app: &AppHandle, message: &str, with_log: bool) {
        self.log(&format!("error view: {message}"));
        let tail = if with_log {
            hub_log::tail_lines(
                &hub_log::hub_log_path(&self.log_dir),
                hub_log::ERROR_TAIL_LINES,
            )
        } else {
            String::new()
        };
        {
            let mut st = self.st();
            st.mode = HubMode::Failed;
            st.counts = SessionCounts::default();
            st.splash.phase = "error".into();
            st.splash.message = message.into();
            st.splash.detail.clear();
            st.splash.log_tail = tail;
        }
        self.bump_generation();
        self.navigate_splash(app);
        reveal_main_window(app);
        tray_menu::refresh(app);
    }

    fn owned_exit(&self) -> Option<String> {
        self.st().owned.as_mut()?.exit_text()
    }

    pub async fn start(self: &Arc<Self>, app: &AppHandle) {
        let _guard = self.ops.lock().await;
        self.start_locked(app).await;
    }

    /// The 重试 button: start again from the error view.
    pub fn retry(self: &Arc<Self>, app: &AppHandle) {
        if self.mode() != HubMode::Failed {
            return;
        }
        self.log("retry requested");
        let shell = self.clone();
        let app = app.clone();
        tauri::async_runtime::spawn(async move { shell.start(&app).await });
    }

    async fn start_locked(self: &Arc<Self>, app: &AppHandle) {
        self.cancel_start.store(false, Ordering::SeqCst);
        // An owned hub can still be running here (the error view for a hub that stopped listening
        // keeps it, and its sessions, until 重试). Stop it first: a second hub would take the free
        // port and share its database.
        if self.st().owned.is_some() {
            self.stop_owned_locked(app).await;
        }
        self.set_starting(app, "正在启动 Hub…", "");
        #[cfg(not(feature = "portable"))]
        let config_result = hub_config::load();
        #[cfg(feature = "portable")]
        let config_result = crate::portable_runtime::prepare(
            |message, detail| self.set_starting(app, message, detail),
            &self.cancel_start,
        )
        .await;
        let cfg = match config_result {
            Ok(cfg) => cfg,
            Err(e) => return self.fail(app, &e, false),
        };
        self.st().config = Some(cfg.clone());
        if let Err(e) = hub_config::validate(&cfg) {
            return self.fail(app, &e, false);
        }
        self.set_starting(
            app,
            "正在启动 Hub…",
            &format!("127.0.0.1:{} · {}", cfg.port, cfg.repo_dir.display()),
        );
        let api = HubApi::new(cfg.port);
        #[cfg(not(feature = "portable"))]
        match owned_hub::probe_port(&api, cfg.port).await {
            PortProbe::ExternalHub(info) => return self.attach_external(app, &cfg, &info).await,
            PortProbe::Busy { pid, name } => {
                let name = name.map(|n| format!("（{n}）")).unwrap_or_default();
                return self.fail(
                    app,
                    &format!(
                        "端口 {} 已被另一个程序占用：PID {pid}{name}，它不是 MCP Agent Hub。请关闭它，或用 AGENT_HUB_PORT / config.json 换一个端口。",
                        cfg.port
                    ),
                    false,
                );
            }
            PortProbe::Free => {}
        }
        let log = |line: &str| self.log(line);
        match OwnedHub::launch(&cfg, &self.log_dir, &log) {
            Ok(owned) => self.st().owned = Some(owned),
            Err(e) => return self.fail(app, &format!("无法启动 Hub：{e}"), true),
        }
        match owned_hub::wait_ready(&api, &self.cancel_start, || self.owned_exit()).await {
            Ok(info) => {
                #[cfg(feature = "portable")]
                if !self.st().owned.as_ref().is_some_and(|owned| owned.owns_server(info.pid)) {
                    if let Some(owned) = self.st().owned.take() { owned.terminate(); }
                    return self.fail(app, "启动期间端口被其他 Hub 占用，请重试。便携版不会连接外部 Hub。", false);
                }
                self.st().mode = HubMode::Owned;
                self.bump_generation();
                self.log(&format!(
                    "hub ready on port {} (hub pid {}, repo {}, started {})",
                    cfg.port, info.pid, info.repo, info.started_at
                ));
                let _ = self.refresh_counts().await;
                self.open_console_quietly(app, &cfg);
                tray_menu::refresh(app);
            }
            Err(ReadyError::Cancelled) => self.log("start cancelled by a stop"),
            Err(ReadyError::Failed(msg)) => {
                if let Some(owned) = self.st().owned.take() {
                    owned.terminate();
                }
                self.fail(app, &msg, true);
            }
        }
    }

    /// Load the console without forcing a hidden window into view.
    fn open_console_quietly(&self, app: &AppHandle, cfg: &HubConfig) {
        let pending = self.st().pending_session.take();
        if let (Some(w), Ok(url)) = (
            app.get_webview_window("main"),
            Url::parse(&cfg.console_url(pending.as_deref())),
        ) {
            self.log(&format!("navigate main window: {url}"));
            let _ = w.navigate(url);
        }
    }

    #[cfg(not(feature = "portable"))]
    async fn attach_external(&self, app: &AppHandle, cfg: &HubConfig, info: &HubInfo) {
        self.st().mode = HubMode::External;
        self.bump_generation();
        self.log(&format!(
            "attached to an external hub on port {} (pid {}, repo {}, started {}, shutdown_enabled {})",
            cfg.port, info.pid, info.repo, info.started_at, info.shutdown_enabled
        ));
        let _ = self.refresh_counts().await;
        self.open_console_quietly(app, cfg);
        tray_menu::refresh(app);
    }

    /// Graceful stop of an owned hub (owned_hub::OwnedHub::stop_gracefully): shutdown request with
    /// the token, up to 10 s for the process to exit, then terminate the job.
    async fn stop_owned_locked(&self, app: &AppHandle) {
        let Some(owned) = self.st().owned.take() else {
            return;
        };
        {
            let mut st = self.st();
            st.mode = HubMode::Stopping;
            st.counts = SessionCounts::default();
        }
        // Ends the notifier's long-poll, so the hub has no open request holding up its shutdown.
        self.bump_generation();
        tray_menu::refresh(app);
        let log = |line: &str| self.log(line);
        let report = owned.stop_gracefully(&log).await;
        self.log(&format!(
            "stop finished: graceful={}, exit={}",
            report.graceful,
            report.exit.as_deref().unwrap_or("terminated by the job")
        ));
    }

    async fn live_session_count(&self) -> usize {
        let Some(target) = self.ready_target() else {
            return 0;
        };
        match target.api.sessions().await {
            Ok(list) => count_sessions(&list).live,
            Err(_) => 0,
        }
    }

    /// Tray 退出: confirm if sessions are live, stop an owned hub gracefully, then exit. An external
    /// hub is left running.
    pub async fn quit(self: &Arc<Self>, app: &AppHandle) {
        if self.mode() != HubMode::External {
            let n = self.live_session_count().await;
            if n > 0
                && !confirm(
                    app,
                    "退出 MCP Agent Hub",
                    &format!("有 {n} 个会话正在运行。退出会停止 Hub 和这些会话。确定退出？"),
                    "退出",
                )
                .await
            {
                self.log(&format!("quit cancelled ({n} live sessions)"));
                return;
            }
        }
        self.cancel_start.store(true, Ordering::SeqCst);
        let _guard = self.ops.lock().await;
        self.log("quit");
        self.stop_owned_locked(app).await;
        app.exit(0);
    }

    /// Tray 重启 Hub: the same confirm and stop as 退出, then start again.
    pub async fn restart(self: &Arc<Self>, app: &AppHandle) {
        if self.mode() == HubMode::External {
            return;
        }
        let n = self.live_session_count().await;
        if n > 0
            && !confirm(
                app,
                "重启 Hub",
                &format!("有 {n} 个会话正在运行。重启会停止 Hub 和这些会话。确定重启？"),
                "重启",
            )
            .await
        {
            self.log(&format!("restart cancelled ({n} live sessions)"));
            return;
        }
        self.cancel_start.store(true, Ordering::SeqCst);
        let _guard = self.ops.lock().await;
        self.log("restart");
        self.set_starting(app, "正在重启 Hub…", "");
        self.stop_owned_locked(app).await;
        self.start_locked(app).await;
    }

    /// Tray 接管 Hub: stop the external hub (its pid from /api/hub/info and its process tree) and
    /// start one this app owns.
    pub async fn takeover(self: &Arc<Self>, app: &AppHandle) {
        if self.mode() != HubMode::External {
            return;
        }
        let Some(target) = self.ready_target() else {
            return;
        };
        let info = match target.api.info(Duration::from_secs(3)).await {
            Ok(info) if info.is_agent_hub() => info,
            _ => {
                self.log("takeover: the external hub no longer answers; starting an owned hub");
                let _guard = self.ops.lock().await;
                return self.start_locked(app).await;
            }
        };
        let n = self.live_session_count().await;
        if n > 0
            && !confirm(
                app,
                "接管 Hub",
                &format!("外部 Hub 有 {n} 个会话正在运行。接管会停止这个 Hub 和这些会话，再由应用重新启动 Hub。确定接管？"),
                "接管",
            )
            .await
        {
            self.log(&format!("takeover cancelled ({n} live sessions)"));
            return;
        }
        let _guard = self.ops.lock().await;
        self.log(&format!(
            "takeover: stopping external hub pid {} and its process tree",
            info.pid
        ));
        self.set_starting(
            app,
            "正在接管 Hub…",
            &format!("停止外部 Hub（PID {}）", info.pid),
        );
        self.bump_generation();
        match windows_process::kill_process_tree(info.pid) {
            Ok(n) => self.log(&format!("takeover: terminated {n} process(es)")),
            Err(e) => self.log(&format!("takeover: kill failed: {e}")),
        }
        let port = info.port;
        let port = if port == 0 {
            self.st().config.as_ref().map(|c| c.port).unwrap_or(0)
        } else {
            port
        };
        if port != 0 && !owned_hub::wait_port_free(port, PORT_RELEASE_WAIT).await {
            let holder = windows_process::listening_pid(port)
                .map(|p| p.to_string())
                .unwrap_or_default();
            return self.fail(
                app,
                &format!(
                    "外部 Hub（PID {}）已结束，但端口 {port} 仍被 PID {holder} 占用。",
                    info.pid
                ),
                false,
            );
        }
        self.start_locked(app).await;
    }

    /// Every 5 s: session counts for the tray, a crashed owned hub → error view, an owned hub that
    /// runs but no longer listens on its port → error view (its sessions keep running until 重试),
    /// an external hub that stopped answering → error view (重试 then starts an owned one).
    pub async fn watch(self: Arc<Self>, app: AppHandle) {
        let mut external_misses = 0u32;
        let mut not_listening = 0u32;
        loop {
            tokio::time::sleep(STATUS_INTERVAL).await;
            match self.mode() {
                HubMode::Owned => {
                    external_misses = 0;
                    if let Ok(_guard) = self.ops.try_lock() {
                        if let Some(code) = self.owned_exit() {
                            not_listening = 0;
                            if let Some(owned) = self.st().owned.take() {
                                owned.terminate();
                            }
                            self.fail(
                                &app,
                                &format!("Hub 进程意外退出了（退出码 {code}）。"),
                                true,
                            );
                            continue;
                        }
                        // Connections that were already open keep working, so session counts
                        // alone would not notice; the port itself says whether new ones can come in.
                        let port = self.st().config.as_ref().map(|c| c.port).unwrap_or(0);
                        if port != 0 && windows_process::listening_pid(port).is_none() {
                            not_listening += 1;
                        } else {
                            not_listening = 0;
                        }
                        if not_listening >= NOT_LISTENING_BEFORE_ERROR {
                            not_listening = 0;
                            self.fail(
                                &app,
                                &format!(
                                    "Hub 进程仍在运行，但已不再接受新连接（端口 {port} 没有在监听）。\
                                     点「重试」会重启 Hub：运行中的会话会中断，之后可在「归档」里恢复。"
                                ),
                                true,
                            );
                            continue;
                        }
                    }
                    self.refresh_counts().await;
                }
                HubMode::External => {
                    if self.refresh_counts().await {
                        external_misses = 0;
                    } else {
                        external_misses += 1;
                        if external_misses >= EXTERNAL_MISSES_BEFORE_ERROR {
                            external_misses = 0;
                            if let Ok(_guard) = self.ops.try_lock() {
                                let port = self.st().config.as_ref().map(|c| c.port).unwrap_or(0);
                                self.fail(
                                    &app,
                                    &format!("外部 Hub（端口 {port}）已停止响应。点「重试」由应用启动并托管 Hub。"),
                                    false,
                                );
                                continue;
                            }
                        }
                    }
                }
                _ => external_misses = 0,
            }
            tray_menu::refresh_tooltip(&app);
        }
    }

    async fn refresh_counts(&self) -> bool {
        let Some(target) = self.ready_target() else {
            return false;
        };
        match target.api.sessions().await {
            Ok(list) => {
                self.st().counts = count_sessions(&list);
                true
            }
            Err(_) => false,
        }
    }

    pub async fn refresh_counts_and_tray(&self, app: &AppHandle) {
        if self.refresh_counts().await {
            tray_menu::refresh_tooltip(app);
        }
    }

    /// The process is exiting by some other path than 退出: end the owned hub now (no wait).
    pub fn terminate_on_exit(&self) {
        if let Some(owned) = self.st().owned.take() {
            self.log("app exit: terminating the owned hub's job");
            owned.terminate();
        }
    }
}
