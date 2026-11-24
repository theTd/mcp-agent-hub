//! The app owns the hub: start it (or attach to one already on the port), wait until it answers,
//! show the splash / error view or the console, stop it gracefully, restart it, take over an
//! external one, and keep a 5 s watch for the tray and for a hub that died.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Manager, Url};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use crate::desktop_toast::{self, ToastClick};
use crate::hub_api::{count_sessions, HubApi, RestartStatus, SessionCounts};
#[cfg(not(feature = "portable"))]
use crate::hub_api::HubInfo;
use crate::hub_config::{self, HubConfig};
use crate::hub_log;
use crate::owned_hub::{self, OwnedHub, ReadyError, PORT_RELEASE_WAIT};
#[cfg(not(feature = "portable"))]
use crate::owned_hub::PortProbe;
use crate::tray_menu;
use crate::update_helper::{self, RestartPlan};
use crate::webview_recovery::{self, PendingRecovery, Trigger};
use crate::windows_process;

const STATUS_INTERVAL: Duration = Duration::from_secs(5);
const EXTERNAL_MISSES_BEFORE_ERROR: u32 = 3;
/// Checks in a row an owned hub may run without listening on its port before the error view.
const NOT_LISTENING_BEFORE_ERROR: u32 = 3;
/// How long the shell waits for the restarted hub to report `ready` / `failed`.
const RESTART_STATUS_TIMEOUT: Duration = Duration::from_secs(45);
/// A brand-new hub may report `idle` before it begins resuming; it has to stay idle this long.
const RESTART_IDLE_POLLS: u32 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HubMode {
    Starting,
    Owned,
    External,
    Failed,
    Stopping,
    /// 优雅重启 / 重启并升级: a planned stop is in flight and the hub is expected to go away. A child
    /// exit here is never an error, and the splash / tray say so instead of "Hub 未运行".
    Restarting,
}

/// What an owned hub's exit means, decided from `restart_plan.json`. A stop this app (or an external
/// CLI hitting the same hub) asked for must never surface as "Hub 进程意外退出".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlannedExit {
    /// mode `hub`: restart the hub here and let it resume the archived sessions.
    Restart,
    /// mode `replace-exe`: exit so the detached helper can swap the EXE.
    ExitForUpgrade,
    /// No plan, an expired plan, a plan for another app instance, or an unknown mode.
    UnexpectedFailure,
}

pub fn classify_owned_exit(
    plan: Option<&RestartPlan>,
    app_pid: u32,
    app_creation_time: Option<u64>,
    now: f64,
) -> PlannedExit {
    let Some(plan) = plan else {
        return PlannedExit::UnexpectedFailure;
    };
    if plan.is_expired(now) || plan.app_pid == 0 || plan.app_pid != app_pid {
        return PlannedExit::UnexpectedFailure;
    }
    // Windows reuses PIDs within minutes. A plan left behind by an older app instance whose PID
    // this app happens to own must never excuse a real crash of this app's hub — and must never
    // make this app exit "for an upgrade" it never staged.
    if plan.app_creation_time != 0 {
        match app_creation_time {
            Some(current) if current == plan.app_creation_time => {}
            _ => return PlannedExit::UnexpectedFailure,
        }
    }
    match plan.mode.as_str() {
        "hub" => PlannedExit::Restart,
        "replace-exe" => PlannedExit::ExitForUpgrade,
        _ => PlannedExit::UnexpectedFailure,
    }
}

fn new_restart_id() -> String {
    windows_process::random_token()
        .map(|token| token.chars().take(16).collect())
        .unwrap_or_else(|_| format!("restart-{}", std::process::id()))
}

/// A `replace-exe` hand-off is only safe when the build the helper is going to swap in really
/// exists: either the staged copy with its planned hash, or a target that already carries the new
/// bytes (the helper finished the swap while this app was still running).
fn upgrade_handoff_is_ready(plan: &RestartPlan) -> bool {
    let expected = plan
        .new_exe_sha256
        .as_deref()
        .map(str::trim)
        .filter(|sha| sha.len() == 64 && sha.chars().all(|c| c.is_ascii_hexdigit()));
    let Some(expected) = expected else {
        return false;
    };
    if let Some(target) = plan.target_exe.as_deref() {
        if update_helper::sha256_file(target).is_ok_and(|sha| sha.eq_ignore_ascii_case(expected)) {
            return true;
        }
    }
    match plan.staged_exe.as_deref() {
        Some(staged) if staged.is_file() => {
            update_helper::sha256_file(staged).is_ok_and(|sha| sha.eq_ignore_ascii_case(expected))
        }
        _ => false,
    }
}

/// The files an EXE swap needs, prepared before anything is stopped.
#[cfg(feature = "portable")]
struct PreparedUpgrade {
    plan_path: PathBuf,
    helper_exe: PathBuf,
    staged_exe: PathBuf,
    target_exe: PathBuf,
}

/// Two paths naming the same file (`current_exe()` and a user-picked path can differ in case, `/`
/// vs `\\` or a `\\\\?\\` prefix).
#[cfg(feature = "portable")]
fn same_file(a: &Path, b: &Path) -> bool {
    match (std::fs::canonicalize(a), std::fs::canonicalize(b)) {
        (Ok(left), Ok(right)) => left == right,
        _ => a
            .to_string_lossy()
            .replace('/', "\\")
            .eq_ignore_ascii_case(&b.to_string_lossy().replace('/', "\\")),
    }
}

/// What the bundled splash page shows (it polls this through the `splash_state` command).
#[derive(Debug, Clone, Serialize)]
pub struct SplashView {
    pub phase: String,
    pub message: String,
    pub detail: String,
    pub log_tail: String,
    pub log_path: String,
    pub can_recover: bool,
    pub recovery_detail: String,
    /// Set while the hub answers: the splash page sends the window here itself, so a window that
    /// came back to the splash never stays on it.
    pub console_url: Option<String>,
}

struct ShellState {
    mode: HubMode,
    config: Option<HubConfig>,
    owned: Option<OwnedHub>,
    splash: SplashView,
    counts: SessionCounts,
    pending_session: PendingConsoleSession,
}

/// The console session a `"main"` window should open next.
///
/// The *latest* request wins, and the request is **consumed** by whoever navigates the window. That
/// is what keeps a stale session from being replayed: a rebuild for session A that is overtaken by a
/// request for session B navigates the new window to B, and a later "just open the console" cannot
/// resurrect A out of a snapshot nobody consumed.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct PendingConsoleSession {
    session: Option<String>,
}

impl PendingConsoleSession {
    pub fn new() -> Self {
        Self::default()
    }

    /// Remember the session a window should open. Only a real session id overwrites; a plain
    /// "open the console" (`None`) must not erase a session that is still waiting for its window.
    pub fn request(&mut self, session: Option<&str>) {
        if let Some(session) = session {
            self.session = Some(session.to_string());
        }
    }

    /// Consume the session: the window that navigates now owns it.
    pub fn take(&mut self) -> Option<String> {
        self.session.take()
    }

    pub fn peek(&self) -> Option<&str> {
        self.session.as_deref()
    }
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

/// The bundled splash page's URL: the app's own origin, on either platform form.
pub fn is_splash_url(url: &Url) -> bool {
    url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost")
}

/// Append a line to `desktop.log` through the app's shell, from places that only hold the app.
pub fn log_from(app: &AppHandle, line: &str) {
    if let Some(shell) = app.try_state::<Arc<HubShell>>() {
        shell.log(line);
    }
}

/// The URL a **newly built** `"main"` window should load right now, consuming the session that was
/// waiting for it: the console (on the latest pending session) while the hub answers, otherwise
/// `None` — the bundled splash / error view.
///
/// Called by the rebuild worker *after* the replacement window exists and before it navigates, so a
/// session that arrived while the rebuild was in flight wins over the snapshot taken when it
/// started, and a stale session is left nowhere to be replayed from.
pub fn take_main_target(app: &AppHandle) -> Option<Url> {
    app.try_state::<Arc<HubShell>>()
        .and_then(|shell| shell.take_console_target())
}

/// Bring the `"main"` window back — and repair its WebView2 surface on the way, because a window
/// that was merely shown again after a GPU/renderer crash stays blank forever.
///
/// Order matters: a pending `Rebuild` (or a missing window) is rebuilt instead of revealed; a
/// pending `Reload` reloads the page first and escalates to a rebuild when WebView2 refuses it.
/// Returns `false` when a rebuild was started, so the caller knows there is no live window to
/// navigate right now — the rebuilt one loads the console itself.
pub fn reveal_main_window(app: &AppHandle) -> bool {
    if webview_recovery::is_rebuilding() {
        // The rebuild reveals the window when it is done; nothing to do on this path.
        return false;
    }
    match webview_recovery::take_pending() {
        PendingRecovery::Rebuild => {
            log_from(app, "reveal: a WebView2 failure still needs a rebuild; rebuilding \"main\"");
            webview_recovery::rebuild_main_window(app, true, Trigger::Manual, "reveal_pending_rebuild");
            return false;
        }
        PendingRecovery::Reload => {
            if let Some(window) = app.get_webview_window("main") {
                log_from(app, "reveal: reloading \"main\" for a pending renderer recovery");
                match webview_recovery::reload_webview(app, &window) {
                    Ok(()) => {
                        show_main_window(&window);
                        webview_recovery::resync_surface(app, &window);
                        return true;
                    }
                    Err(error) => {
                        // The reload could not even be started: a rebuild is the only repair left.
                        log_from(app, &format!("reveal: reloading \"main\" failed ({error}); rebuilding it"));
                        webview_recovery::record_reload_failure(app);
                        webview_recovery::rebuild_main_window(
                            app,
                            true,
                            Trigger::Manual,
                            "reveal_reload_failed",
                        );
                        return false;
                    }
                }
            }
            log_from(app, "reveal: pending reload but \"main\" is gone; rebuilding it");
            webview_recovery::rebuild_main_window(
                app,
                true,
                Trigger::Manual,
                "reveal_reload_without_window",
            );
            return false;
        }
        PendingRecovery::None => {}
    }
    let Some(window) = app.get_webview_window("main") else {
        log_from(app, "reveal: \"main\" does not exist; rebuilding it");
        webview_recovery::rebuild_main_window(app, true, Trigger::Manual, "reveal_missing_window");
        return false;
    };
    show_main_window(&window);
    // Re-create the D3D surface and re-apply the host rect: the repair a wedged compositor needs.
    webview_recovery::resync_surface(app, &window);
    true
}

/// Show, restore and raise the window. Raw Win32 on top of the Tauri calls so a minimized window
/// really comes back in front of whatever the user was looking at.
fn show_main_window(w: &tauri::WebviewWindow) {
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
                HWND_NOTOPMOST, HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW, SW_RESTORE,
                SW_SHOW,
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
                    can_recover: false,
                    recovery_detail: String::new(),
                    console_url: None,
                },
                counts: SessionCounts::default(),
                pending_session: PendingConsoleSession::new(),
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
        if !is_splash_url(&url) {
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
    ///
    /// The window is revealed first and that step is authoritative: it may reload the page, rebuild
    /// a wedged `"main"` window, or just repair the WebView2 surface (see `reveal_main_window`).
    pub fn open_console(&self, app: &AppHandle, session_id: Option<&str>) {
        self.st().pending_session.request(session_id);
        if !reveal_main_window(app) {
            // A rebuild owns the window from here: it builds the new one and navigates it to the
            // console itself — consuming the pending session at that moment, so a session asked
            // for while the rebuild runs (including this one) is what the new window loads.
            return;
        }
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

    /// The console URL a *newly built* window should load, consuming the session that was waiting
    /// for it (see [`take_main_target`]) — or `None` while the hub is not answering, where the
    /// window shows the splash / the error view.
    pub fn take_console_target(&self) -> Option<Url> {
        let cfg = self.ready_config()?;
        let session = self.st().pending_session.take();
        Url::parse(&cfg.console_url(session.as_deref())).ok()
    }

    /// Tray 重建控制台窗口 / the 1-click escape hatch for a blank window: throw the WebView2 host
    /// away, build a fresh `"main"` window pointed at the current state, and reveal it.
    ///
    /// The hub backend, its sessions and the owned process are never touched — a running subagent
    /// keeps running while the window heals.
    pub fn rebuild_console_window(&self, app: &AppHandle, reason: &str) {
        self.log(&format!("rebuild console window requested ({reason})"));
        // Informational: the worker consumes the request once the new window exists, so a session
        // asked for in the meantime replaces this one.
        if let Some(session) = self.st().pending_session.peek() {
            self.log(&format!(
                "rebuild console window: the new window will open session {session} unless a newer one arrives"
            ));
        }
        let scheduled = webview_recovery::rebuild_main_window(app, true, Trigger::Manual, reason);
        if !scheduled {
            self.log("rebuild console window: another rebuild is already in flight");
        }
    }

    /// A window that landed back on the splash while the hub answers: send it to the console
    /// without showing or unminimizing it (the splash page can be reloaded by a stray navigation).
    pub fn reconcile_console_if_ready(&self, app: &AppHandle) {
        let Some(cfg) = self.ready_config() else {
            return;
        };
        let Ok(url) = Url::parse(&cfg.console_url(None)) else {
            return;
        };
        let Some(w) = app.get_webview_window("main") else {
            return;
        };
        let on_console = w
            .url()
            .map(|c| c.origin() == url.origin())
            .unwrap_or(false);
        if on_console {
            return;
        }
        self.log(&format!("reconcile main window: {url}"));
        let _ = w.navigate(url);
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
        self.set_starting_state(message, detail);
        self.navigate_splash(app);
        tray_menu::refresh(app);
    }

    /// 优雅重启 / 重启并升级: the splash keeps its "starting" phase (the page knows how to render a
    /// spinner) while the message, the tray tooltip and the tray menu say restart.
    fn set_restarting(&self, app: &AppHandle, message: &str, detail: &str) {
        self.set_phase_state(HubMode::Restarting, "starting", message, detail);
        self.navigate_splash(app);
        tray_menu::refresh(app);
    }

    fn set_phase_state(&self, mode: HubMode, phase: &str, message: &str, detail: &str) {
        let mut st = self.st();
        st.mode = mode;
        st.splash.phase = phase.into();
        st.splash.message = message.into();
        st.splash.detail = detail.into();
        st.splash.log_tail.clear();
        st.splash.can_recover = false;
        st.splash.recovery_detail.clear();
        st.splash.console_url = None;
    }

    fn set_starting_state(&self, message: &str, detail: &str) {
        self.set_phase_state(HubMode::Starting, "starting", message, detail);
    }

    /// The hub answers: switch the mode and the splash view in one step, so the splash page can
    /// send the window to the console itself even when the shell's own navigation is not applied.
    fn mark_ready_state(&self, mode: HubMode, cfg: &HubConfig) {
        let mut st = self.st();
        st.mode = mode;
        st.splash.phase = "ready".into();
        st.splash.message = "Hub 已就绪，正在打开总控台…".into();
        st.splash.detail = format!("127.0.0.1:{} · {}", cfg.port, cfg.repo_dir.display());
        st.splash.log_tail.clear();
        st.splash.can_recover = false;
        st.splash.recovery_detail.clear();
        st.splash.console_url = Some(cfg.console_url(None));
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
        self.set_failed_state(message, tail);
        self.bump_generation();
        self.navigate_splash(app);
        reveal_main_window(app);
        tray_menu::refresh(app);
    }

    fn set_failed_state(&self, message: &str, tail: String) {
        let mut st = self.st();
        st.mode = HubMode::Failed;
        st.counts = SessionCounts::default();
        st.splash.phase = "error".into();
        st.splash.message = message.into();
        st.splash.detail.clear();
        st.splash.log_tail = tail;
        st.splash.console_url = None;
        #[cfg(feature = "portable")]
        {
            st.splash.can_recover = message.contains("占用") || message.contains("残留 Hub");
            st.splash.recovery_detail = if st.splash.can_recover {
                "仅会结束已验证属于本便携版的残留 Hub；无法验证归属的进程不会被结束".into()
            } else {
                String::new()
            };
        }
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

    #[cfg(feature = "portable")]
    pub fn recover_stale(self: &Arc<Self>, app: &AppHandle) {
        if self.mode() != HubMode::Failed {
            return;
        }
        let shell = self.clone();
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            let _guard = shell.ops.lock().await;
            shell.set_starting(&app, "正在清理已验证的残留 Hub…", "不会结束无法验证归属的进程");
            match crate::portable_runtime::recover_stale().await {
                Ok(report) => {
                    shell.log(&format!("stale hub recovery: live_peer={:?}, stopped={:?}", report.live_peer, report.stopped));
                    if report.live_peer.is_some() {
                        shell.fail(&app, "另一个便携版实例仍在运行，请先关闭它", false);
                        return;
                    }
                    let payload = crate::hub_config::portable_root().join("resources").join(crate::portable_runtime::PAYLOAD_ID);
                    let holders = crate::portable_runtime::payload_holders(&payload);
                    match crate::portable_runtime::stop_holders(&holders).await {
                        Ok(_) => shell.start_locked(&app).await,
                        Err(error) => shell.fail(&app, &error.to_string(), false),
                    }
                }
                Err(error) => shell.fail(&app, &error.to_string(), false),
            }
        });
    }

    #[cfg(not(feature = "portable"))]
    pub fn recover_stale(self: &Arc<Self>, _app: &AppHandle) {}

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
        let config_result = crate::portable_runtime::prepare_detailed(
            |message, detail| self.set_starting(app, message, detail),
            &self.cancel_start,
        )
        .await;
        let cfg = match config_result {
            Ok(cfg) => cfg,
            Err(e) => return self.fail(app, &e.to_string(), false),
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
                self.mark_ready_state(HubMode::Owned, &cfg);
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
        let pending = self.st().pending_session.take();        if let (Some(w), Ok(url)) = (
            app.get_webview_window("main"),
            Url::parse(&cfg.console_url(pending.as_deref())),
        ) {
            self.log(&format!("navigate main window: {url}"));
            let _ = w.navigate(url);
        }
    }

    #[cfg(not(feature = "portable"))]
    async fn attach_external(&self, app: &AppHandle, cfg: &HubConfig, info: &HubInfo) {
        self.mark_ready_state(HubMode::External, cfg);
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

    /// Tray 优雅重启 Hub（保留会话）: write `restart_plan.json`, ask the hub to stop with
    /// `/api/hub/graceful-restart` (it archives the live sessions and plans to resume them), start a
    /// new hub, then wait until it reports how many sessions it resumed.
    pub async fn restart(self: &Arc<Self>, app: &AppHandle) {
        if !matches!(self.mode(), HubMode::Owned | HubMode::Failed) {
            return;
        }
        let n = self.live_session_count().await;
        if n > 0
            && !confirm(
                app,
                "优雅重启 Hub",
                &format!("当前有 {n} 个活跃会话。优雅重启会重启 Hub 并自动恢复这些会话，是否继续？"),
                "优雅重启",
            )
            .await
        {
            self.log(&format!("restart cancelled ({n} live sessions)"));
            return;
        }
        let _guard = self.ops.lock().await;
        // The confirmation dialog may have run while a start was finishing.
        if !matches!(self.mode(), HubMode::Owned | HubMode::Failed) {
            return;
        }
        self.log("graceful restart requested");
        self.set_restarting(app, "正在优雅重启并恢复会话…", "");
        let restart_id = new_restart_id();
        let port = self.write_restart_plan(&restart_id, "hub");
        // Fail-closed: without a confirmed graceful stop the hub has not archived the sessions in
        // `resume_on_start.json`, so starting a new one would silently drop every live session.
        // Keep the running hub, its sessions and the shell exactly as they are.
        if let Err(reason) = self.stop_owned_for_restart("hub", Some(&restart_id)).await {
            update_helper::remove_restart_plan(&update_helper::restart_plan_path());
            self.abort_planned_stop(app, "优雅重启已中止", &reason);
            return;
        }
        self.start_locked(app).await;
        self.settle_restart(app, &restart_id, port).await;
        update_helper::remove_restart_plan(&update_helper::restart_plan_path());
        tray_menu::refresh(app);
    }

    /// `restart_plan.json` for an in-place restart. Returns the port the new hub will listen on.
    fn write_restart_plan(&self, restart_id: &str, mode: &str) -> u16 {
        let (port, hub) = {
            let st = self.st();
            (
                st.config.as_ref().map(|c| c.port).unwrap_or(0),
                st.owned.as_ref().and_then(|owned| owned.hub_identity()),
            )
        };
        let app_pid = std::process::id();
        let now = update_helper::now_secs();
        let plan = RestartPlan {
            version: update_helper::PLAN_VERSION,
            restart_id: restart_id.to_string(),
            mode: mode.to_string(),
            requested_at: now,
            expires_at: now + update_helper::RESTART_PLAN_TTL_SECS,
            port,
            app_pid,
            app_creation_time: windows_process::creation_time(app_pid).unwrap_or(0),
            hub_pid: hub.map(|identity| identity.pid).unwrap_or(0),
            hub_creation_time: hub.map(|identity| identity.creation_time).unwrap_or(0),
            data_dir: self.data_dir_for_plan(),
            target_exe: None,
            old_exe_sha256: None,
            new_exe: None,
            new_exe_sha256: None,
            staged_exe: None,
            backup_exe: None,
        };
        let path = update_helper::restart_plan_path();
        match update_helper::write_restart_plan(&path, &plan) {
            Ok(()) => self.log(&format!(
                "restart plan written: {} (mode {mode}, restart_id {restart_id}, port {port})",
                path.display()
            )),
            Err(error) => self.log(&format!("restart plan write failed: {error}")),
        }
        port
    }

    fn data_dir_for_plan(&self) -> Option<PathBuf> {
        #[cfg(feature = "portable")]
        {
            Some(hub_config::portable_root().join("data"))
        }
        #[cfg(not(feature = "portable"))]
        {
            None
        }
    }

    /// Stop an owned hub as part of a planned restart: the same wait / terminate / port release as
    /// 退出, but the request goes to the graceful-restart endpoint so sessions are archived first.
    ///
    /// **Fail-closed**: when the hub does not confirm the graceful restart, this puts the untouched
    /// hub back into the shell state (mode `Owned`, so this app keeps supervising it) and returns
    /// the reason. The caller must then abort the restart / upgrade and leave the live hub alone.
    async fn stop_owned_for_restart(
        &self,
        mode: &str,
        restart_id: Option<&str>,
    ) -> Result<(), String> {
        let Some(owned) = self.st().owned.take() else {
            self.log("planned restart: there is no owned hub to stop");
            return Ok(());
        };
        {
            let mut st = self.st();
            st.mode = HubMode::Restarting;
            st.counts = SessionCounts::default();
        }
        // Ends the notifier's long-poll, so the hub has no open request holding up its shutdown.
        self.bump_generation();
        let log = |line: &str| self.log(line);
        match owned.stop_for_graceful_restart(mode, restart_id, &log).await {
            Ok(report) => {
                self.log(&format!(
                    "planned stop finished: graceful={}, restart_endpoint={}, exit={}",
                    report.graceful,
                    report.graceful_restart_supported,
                    report
                        .exit
                        .as_deref()
                        .unwrap_or("terminated by the job")
                ));
                Ok(())
            }
            Err((owned, reason)) => {
                // Not a single file was touched and the hub never got a stop request: keep owning it.
                {
                    let mut st = self.st();
                    st.owned = Some(owned);
                    st.mode = HubMode::Owned;
                }
                Err(reason)
            }
        }
    }

    /// A planned stop was refused: restore the shell to a normal owned-hub state (splash, console,
    /// notifier), then tell the user why nothing happened. The live hub keeps serving its sessions.
    fn abort_planned_stop(&self, app: &AppHandle, title: &str, reason: &str) {
        self.log(&format!("{title}: {reason}"));
        self.bump_generation();
        if let Some(cfg) = self.st().config.clone() {
            self.mark_ready_state(HubMode::Owned, &cfg);
            self.open_console_quietly(app, &cfg);
        } else {
            self.set_starting_state("正在启动 Hub…", "");
        }
        tray_menu::refresh(app);
        app.dialog()
            .message(reason)
            .title(title)
            .kind(MessageDialogKind::Error)
            .buttons(MessageDialogButtons::Ok)
            .show(|_| {});
    }

    /// Poll `/api/hub/restart-status` until the restarted hub says the resume finished, then log it,
    /// toast the counts and refresh the tray. `idle` only counts after a few polls, because a
    /// brand-new hub answers `idle` before it starts resuming.
    async fn settle_restart(&self, app: &AppHandle, restart_id: &str, port: u16) {
        if port == 0 {
            self.log("restart: no port to probe for restart status");
            return;
        }
        let api = HubApi::new(port);
        let deadline = Instant::now() + RESTART_STATUS_TIMEOUT;
        let mut last: Option<RestartStatus> = None;
        let mut idle_polls = 0u32;
        loop {
            match api.restart_status(Duration::from_secs(2)).await {
                Ok(status) => {
                    self.log(&format!("restart {restart_id} status: {status:?}"));
                    idle_polls = if status.phase == "idle" { idle_polls + 1 } else { 0 };
                    let finished = status.is_terminal()
                        && (status.phase != "idle" || idle_polls >= RESTART_IDLE_POLLS);
                    last = Some(status);
                    if finished {
                        break;
                    }
                }
                Err(error) => self.log(&format!("restart status unavailable: {error}")),
            }
            if Instant::now() >= deadline {
                self.log(&format!(
                    "restart status still not final after {} s",
                    RESTART_STATUS_TIMEOUT.as_secs()
                ));
                break;
            }
            tokio::time::sleep(Duration::from_millis(1000)).await;
        }
        self.report_restart_result(app, restart_id, last.as_ref());
        self.refresh_counts_and_tray(app).await;
    }

    fn report_restart_result(&self, app: &AppHandle, restart_id: &str, status: Option<&RestartStatus>) {
        let Some(status) = status else {
            self.log(&format!(
                "restart {restart_id}: the new hub never reported a restart status"
            ));
            desktop_toast::show(
                app,
                desktop_toast::APP_DISPLAY_NAME,
                "Hub 已重启",
                Some("会话恢复状态未知，可在总控台查看"),
                ToastClick::ShowWindow,
            );
            return;
        };
        match status.phase.as_str() {
            "ready" => {
                self.log(&format!(
                    "restart {restart_id} ready: {}/{} resumed, {} skipped, {} failed",
                    status.resumed_count,
                    status.target_count,
                    status.skipped_count,
                    status.failed_count
                ));
                let line1 = format!(
                    "已恢复 {} / {} 个会话",
                    status.resumed_count, status.target_count
                );
                let line2 = (status.skipped_count > 0 || status.failed_count > 0).then(|| {
                    format!(
                        "{} 个未能恢复（见「归档」）",
                        status.skipped_count + status.failed_count
                    )
                });
                desktop_toast::show(
                    app,
                    desktop_toast::APP_DISPLAY_NAME,
                    &line1,
                    line2.as_deref(),
                    ToastClick::ShowWindow,
                );
            }
            "failed" => {
                self.log(&format!(
                    "restart {restart_id} failed: {}/{} resumed, {} failed",
                    status.resumed_count, status.target_count, status.failed_count
                ));
                desktop_toast::show(
                    app,
                    desktop_toast::APP_DISPLAY_NAME,
                    "Hub 已重启，但会话恢复失败",
                    Some("可在「归档」里手动恢复"),
                    ToastClick::ShowWindow,
                );
            }
            other => {
                self.log(&format!("restart {restart_id} ended in phase {other}"));
            }
        }
    }

    /// 重启并升级…: validate and stage the chosen EXE, stop the hub gracefully, hand the swap to a
    /// detached helper, then exit so the helper is the only thing touching the file.
    #[cfg(feature = "portable")]
    pub async fn restart_and_upgrade(self: &Arc<Self>, app: &AppHandle, new_exe: PathBuf) {
        if self.mode() != HubMode::Owned {
            self.log("upgrade ignored: this app does not own the hub");
            return;
        }
        let name = new_exe
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| new_exe.display().to_string());
        if !confirm(
            app,
            "重启并升级",
            &format!(
                "将用 {name} 替换当前程序。Hub 会先优雅停止，随后由新版本恢复会话，应用会自动退出。确定继续？"
            ),
            "升级",
        )
        .await
        {
            self.log("upgrade cancelled");
            return;
        }
        let _guard = self.ops.lock().await;
        if self.mode() != HubMode::Owned {
            return;
        }
        let restart_id = new_restart_id();
        let prepared = match self.prepare_upgrade(&new_exe, &restart_id) {
            Ok(prepared) => prepared,
            Err(error) => {
                // Nothing was stopped and no file was touched.
                self.log(&format!("upgrade aborted before any change: {error}"));
                app.dialog()
                    .message(&error)
                    .title("无法升级")
                    .kind(MessageDialogKind::Error)
                    .buttons(MessageDialogButtons::Ok)
                    .show(|_| {});
                return;
            }
        };
        self.log(&format!(
            "upgrade staged: target {}, staged {}, helper {}, plan {}",
            prepared.target_exe.display(),
            prepared.staged_exe.display(),
            prepared.helper_exe.display(),
            prepared.plan_path.display()
        ));
        self.set_restarting(app, "正在重启并升级…", &format!("新版本：{name}"));
        // Fail-closed: when the hub does not confirm the graceful restart, the sessions were never
        // archived. Handing the EXE to the helper here would lose them, so drop every staged
        // artefact of this attempt and keep the current build running.
        if let Err(reason) = self
            .stop_owned_for_restart("replace-exe", Some(&restart_id))
            .await
        {
            update_helper::remove_restart_plan(&prepared.plan_path);
            let _ = std::fs::remove_file(&prepared.staged_exe);
            let _ = std::fs::remove_file(&prepared.helper_exe);
            self.abort_planned_stop(app, "升级已中止", &reason);
            return;
        }
        if let Err(error) = update_helper::launch_detached_helper(&prepared.helper_exe, &prepared.plan_path) {
            self.log(&format!("upgrade helper did not start: {error}"));
            let _ = std::fs::remove_file(&prepared.plan_path);
            let _ = std::fs::remove_file(&prepared.staged_exe);
            app.dialog()
                .message(&format!(
                    "无法启动升级助手：{error}\n\n当前版本会重新启动。"
                ))
                .title("升级中止")
                .kind(MessageDialogKind::Warning)
                .show(|_| {});
            self.start_locked(app).await;
            return;
        }
        self.log("upgrade helper launched; exiting so the EXE is no longer locked");
        app.exit(0);
    }

    #[cfg(not(feature = "portable"))]
    pub async fn restart_and_upgrade(self: &Arc<Self>, app: &AppHandle, new_exe: PathBuf) {
        let _ = new_exe;
        self.log("upgrade ignored: the installed build does not replace its EXE");
        app.dialog()
            .message("只有便携版（单个 EXE）支持“重启并升级…”。安装版请用新的安装包升级。")
            .title("重启并升级")
            .kind(MessageDialogKind::Info)
            .show(|_| {});
    }

    /// Precheck + staging for an EXE swap. Every step fails closed: a rejected file leaves the hub
    /// running and no file touched.
    #[cfg(feature = "portable")]
    fn prepare_upgrade(&self, new_exe: &Path, restart_id: &str) -> Result<PreparedUpgrade, String> {
        if !new_exe.is_file() {
            return Err(format!("找不到所选文件：{}", new_exe.display()));
        }
        let current = std::env::current_exe().map_err(|e| format!("无法定位当前程序：{e}"))?;
        if same_file(&current, new_exe) {
            return Err("所选文件就是当前正在运行的程序".to_string());
        }
        let new_sha = update_helper::sha256_file(new_exe)?;
        let current_sha = update_helper::sha256_file(&current)?;
        let (port, hub) = {
            let st = self.st();
            (
                st.config.as_ref().map(|c| c.port).unwrap_or(0),
                st.owned.as_ref().and_then(|owned| owned.hub_identity()),
            )
        };
        if port == 0 {
            // The helper verifies the upgrade through `/api/hub/info`; without a port it must not
            // swap anything, so refuse here rather than stage a plan it will reject.
            return Err("Hub 端口未知，无法校验升级结果".to_string());
        }
        let sidecar = PathBuf::from(format!("{}.sha256", new_exe.display()));
        if sidecar.is_file() {
            let text = std::fs::read_to_string(&sidecar)
                .map_err(|e| format!("无法读取 {}：{e}", sidecar.display()))?;
            let expected = text
                .split_whitespace()
                .next()
                .unwrap_or("")
                .to_lowercase();
            if expected.len() != 64 || !expected.chars().all(|c| c.is_ascii_hexdigit()) {
                return Err(format!("{} 不是有效的 SHA-256 清单", sidecar.display()));
            }
            if expected != new_sha.to_lowercase() {
                return Err(format!(
                    "新程序的 SHA-256（{new_sha}）与 {} 不一致",
                    sidecar.display()
                ));
            }
            self.log(&format!("upgrade: sha256 sidecar verified ({new_sha})"));
        }
        let nonce = update_helper::random_nonce()?;
        let staged = update_helper::staged_exe_path(&current, &nonce);
        let helper = update_helper::helper_exe_path(&nonce);
        let backup = update_helper::backup_exe_path(&current);
        std::fs::copy(new_exe, &staged)
            .map_err(|e| format!("无法暂存到 {}：{e}", staged.display()))?;
        let staged_sha = update_helper::sha256_file(&staged)?;
        if !staged_sha.eq_ignore_ascii_case(&new_sha) {
            let _ = std::fs::remove_file(&staged);
            return Err(format!("暂存副本校验失败：{staged_sha} != {new_sha}"));
        }
        std::fs::copy(&current, &helper)
            .map_err(|e| format!("无法复制升级助手到 {}：{e}", helper.display()))?;
        let helper_sha = update_helper::sha256_file(&helper)?;
        if helper_sha != current_sha {
            let _ = std::fs::remove_file(&helper);
            let _ = std::fs::remove_file(&staged);
            return Err("复制到临时目录的升级助手与当前程序不一致".to_string());
        }
        let app_pid = std::process::id();
        let now = update_helper::now_secs();
        let plan = RestartPlan {
            version: update_helper::PLAN_VERSION,
            restart_id: restart_id.to_string(),
            mode: "replace-exe".to_string(),
            requested_at: now,
            expires_at: now + update_helper::RESTART_PLAN_TTL_SECS,
            port,
            app_pid,
            app_creation_time: windows_process::creation_time(app_pid).unwrap_or(0),
            hub_pid: hub.map(|identity| identity.pid).unwrap_or(0),
            hub_creation_time: hub.map(|identity| identity.creation_time).unwrap_or(0),
            data_dir: self.data_dir_for_plan(),
            target_exe: Some(current.clone()),
            old_exe_sha256: Some(current_sha),
            new_exe: Some(new_exe.to_path_buf()),
            new_exe_sha256: Some(new_sha),
            staged_exe: Some(staged.clone()),
            backup_exe: Some(backup),
        };
        let plan_path = update_helper::restart_plan_path();
        update_helper::write_restart_plan(&plan_path, &plan)?;
        Ok(PreparedUpgrade {
            plan_path,
            helper_exe: helper,
            staged_exe: staged,
            target_exe: current,
        })
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
    ///
    /// A hub that exited because `restart_plan.json` says so was *not* a crash: `mode="hub"` restarts
    /// it here (the sessions were archived and the new hub resumes them), `mode="replace-exe"` exits
    /// the shell so the detached helper can swap the EXE.
    pub async fn watch(self: Arc<Self>, app: AppHandle) {
        let mut external_misses = 0u32;
        let mut not_listening = 0u32;
        loop {
            tokio::time::sleep(STATUS_INTERVAL).await;
            match self.mode() {
                HubMode::Restarting | HubMode::Stopping => {
                    // A planned stop is in flight: the child is supposed to exit. Whoever started the
                    // restart owns the transition out of this mode; the watch must not react at all.
                    external_misses = 0;
                    not_listening = 0;
                }
                HubMode::Owned => {
                    external_misses = 0;
                    let mut planned = false;
                    if let Ok(_guard) = self.ops.try_lock() {
                        if let Some(code) = self.owned_exit() {
                            not_listening = 0;
                            let plan =
                                update_helper::read_restart_plan(&update_helper::restart_plan_path());
                            match classify_owned_exit(
                                plan.as_ref(),
                                std::process::id(),
                                // The plan names a process *instance*: a reused PID is another app.
                                windows_process::process_identity(std::process::id())
                                    .map(|identity| identity.creation_time),
                                update_helper::now_secs(),
                            ) {
                                PlannedExit::Restart => {
                                    let restart_id = plan
                                        .as_ref()
                                        .map(|p| p.restart_id.clone())
                                        .unwrap_or_default();
                                    let port =
                                        self.st().config.as_ref().map(|c| c.port).unwrap_or(0);
                                    self.log(&format!(
                                        "planned restart detected via restart_plan.json (hub exited with {code}); starting a new hub"
                                    ));
                                    self.bump_generation();
                                    if let Some(owned) = self.st().owned.take() {
                                        owned.terminate();
                                    }
                                    #[cfg(feature = "portable")]
                                    let _ = std::fs::remove_file(
                                        hub_config::portable_root().join("launch.json"),
                                    );
                                    self.set_restarting(
                                        &app,
                                        "正在优雅重启并恢复会话…",
                                        "",
                                    );
                                    self.start_locked(&app).await;
                                    self.settle_restart(&app, &restart_id, port).await;
                                    update_helper::remove_restart_plan(
                                        &update_helper::restart_plan_path(),
                                    );
                                    tray_menu::refresh(&app);
                                    planned = true;
                                }
                                PlannedExit::ExitForUpgrade => {
                                    // Only hand the EXE over when the staged build really exists (or
                                    // the swap already happened). A plan whose staged file is missing
                                    // or whose hash does not match must not make this app exit and
                                    // leave the desktop with no shell and no hub.
                                    let handoff_ready =
                                        plan.as_ref().is_some_and(upgrade_handoff_is_ready);
                                    if !handoff_ready {
                                        self.log("planned EXE upgrade is incomplete: staged build missing or hash mismatch; keeping the shell");
                                        update_helper::remove_restart_plan(
                                            &update_helper::restart_plan_path(),
                                        );
                                        if let Some(owned) = self.st().owned.take() {
                                            owned.terminate();
                                        }
                                        self.fail(
                                            &app,
                                            "升级计划不完整（暂存文件缺失或哈希不一致），已取消升级并保留当前程序。\
                                             点「重试」可重新启动 Hub。",
                                            false,
                                        );
                                        continue;
                                    }
                                    self.log(&format!(
                                        "planned EXE upgrade detected (hub exited with {code}); exiting shell cleanly so the helper can replace it"
                                    ));
                                    if let Some(owned) = self.st().owned.take() {
                                        owned.terminate();
                                    }
                                    #[cfg(feature = "portable")]
                                    let _ = std::fs::remove_file(
                                        hub_config::portable_root().join("launch.json"),
                                    );
                                    planned = true;
                                    app.exit(0);
                                }
                                PlannedExit::UnexpectedFailure => {
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
                            }
                        }
                        if planned {
                            tray_menu::refresh_tooltip(&app);
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
                    if self.mode() == HubMode::Owned {
                        self.refresh_counts().await;
                    }
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

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(port: u16) -> HubConfig {
        HubConfig {
            repo_dir: PathBuf::from("C:/repo"),
            python: PathBuf::from("C:/repo/.venv/Scripts/python.exe"),
            port,
        }
    }

    #[test]
    fn splash_url_matches_only_the_app_origin() {
        for url in ["http://tauri.localhost/", "tauri://localhost/"] {
            assert!(is_splash_url(&Url::parse(url).unwrap()), "{url}");
        }
        for url in ["about:blank", "http://127.0.0.1:4444/"] {
            assert!(!is_splash_url(&Url::parse(url).unwrap()), "{url}");
        }
    }

    #[test]
    fn splash_view_follows_the_hub_state() {
        let shell = HubShell::new(std::env::temp_dir());
        let starting = shell.splash_view();
        assert_eq!(starting.phase, "starting");
        assert_eq!(starting.console_url, None);

        shell.mark_ready_state(HubMode::Owned, &cfg(4444));
        let ready = shell.splash_view();
        assert_eq!(shell.mode(), HubMode::Owned);
        assert_eq!(ready.phase, "ready");
        assert_eq!(ready.console_url.as_deref(), Some("http://127.0.0.1:4444/"));
        assert!(ready.message.contains("总控台"));

        shell.set_starting_state("正在重启 Hub…", "");
        let restarted = shell.splash_view();
        assert_eq!(shell.mode(), HubMode::Starting);
        assert_eq!(restarted.phase, "starting");
        assert_eq!(restarted.console_url, None);

        shell.mark_ready_state(HubMode::External, &cfg(4465));
        assert_eq!(
            shell.splash_view().console_url.as_deref(),
            Some("http://127.0.0.1:4465/")
        );

        shell.set_failed_state("端口 4444 已被另一个程序占用", String::new());
        let failed = shell.splash_view();
        assert_eq!(shell.mode(), HubMode::Failed);
        assert_eq!(failed.phase, "error");
        assert_eq!(failed.console_url, None);
    }

    #[test]
    fn restarting_is_its_own_mode_with_a_restart_message() {
        let shell = HubShell::new(std::env::temp_dir());
        shell.mark_ready_state(HubMode::Owned, &cfg(4444));
        // A restart is not a failure: the mode, the splash message and the console target all change.
        let mut st = shell.st();
        st.mode = HubMode::Restarting;
        st.splash.phase = "starting".into();
        st.splash.message = "正在优雅重启并恢复会话…".into();
        st.splash.console_url = None;
        drop(st);
        assert_eq!(shell.mode(), HubMode::Restarting);
        assert_ne!(shell.mode(), HubMode::Failed);
        assert!(shell.splash_view().message.contains("优雅重启"));
        // No ready target while restarting: the notifier and the console must not talk to a hub
        // that is going away.
        assert!(shell.ready_target().is_none());
    }

    #[test]
    fn pending_session_is_consumed_by_the_window_that_navigates() {
        let mut pending = PendingConsoleSession::new();
        assert!(pending.peek().is_none());
        // "just open the console" never creates a request of its own.
        pending.request(None);
        assert_eq!(pending.take(), None);
        pending.request(Some("session-a"));
        assert_eq!(pending.peek(), Some("session-a"));
        // A rebuilt window navigates: it consumes the session, so a later open_console(None) has
        // nothing stale left to replay.
        assert_eq!(pending.take(), Some("session-a".to_string()));
        assert_eq!(pending.take(), None, "a stale session is never replayed");
        assert!(pending.peek().is_none());
    }

    #[test]
    fn a_session_that_arrives_in_flight_wins_over_the_one_being_rebuilt() {
        let mut pending = PendingConsoleSession::new();
        // A rebuild for session A starts and records its request ...
        pending.request(Some("session-a"));
        // ... and session B arrives while the window is still being rebuilt: the latest wins.
        pending.request(Some("session-b"));
        // The new window resolves its target *after* it exists and consumes B — never A.
        assert_eq!(pending.take(), Some("session-b".to_string()));
        assert_eq!(pending.take(), None);
        // A plain `None` never erases a session that is still waiting for its window.
        pending.request(Some("session-c"));
        pending.request(None);
        assert_eq!(pending.peek(), Some("session-c"));
        assert_eq!(pending.take(), Some("session-c".to_string()));
    }

    #[test]
    fn a_new_window_target_consumes_the_pending_session_exactly_once() {
        let shell = HubShell::new(std::env::temp_dir());
        shell.st().config = Some(cfg(4444));
        shell.mark_ready_state(HubMode::Owned, &cfg(4444));
        shell.st().pending_session.request(Some("session-a"));
        let target = shell.take_console_target().expect("the hub answers");
        assert_eq!(target.as_str(), "http://127.0.0.1:4444/?session=session-a");
        // Consumed: a second window would open the plain console, not session A again.
        let next = shell.take_console_target().expect("the hub still answers");
        assert_eq!(next.as_str(), "http://127.0.0.1:4444/");
        // While the hub does not answer there is no console URL, and the request is left alone.
        shell.set_starting_state("正在启动 Hub…", "");
        shell.st().pending_session.request(Some("session-b"));
        assert_eq!(shell.take_console_target(), None);
        assert_eq!(shell.st().pending_session.peek(), Some("session-b"));
    }

    fn plan(mode: &str, app_pid: u32, expires_at: f64) -> RestartPlan {
        RestartPlan {
            version: update_helper::PLAN_VERSION,
            restart_id: "r".into(),
            mode: mode.into(),
            requested_at: expires_at - 600.0,
            expires_at,
            port: 4444,
            app_pid,
            app_creation_time: 1,
            hub_pid: 2,
            hub_creation_time: 3,
            data_dir: None,
            target_exe: None,
            old_exe_sha256: None,
            new_exe: None,
            new_exe_sha256: None,
            staged_exe: None,
            backup_exe: None,
        }
    }

    #[test]
    fn a_planned_exit_is_told_apart_from_a_crash() {
        let me = std::process::id();
        let now = 1_795_470_500.0;

        // The plan this app wrote for itself: restart the hub, never an error.
        assert_eq!(
            classify_owned_exit(Some(&plan("hub", me, 1_795_470_600.0)), me, Some(1), now),
            PlannedExit::Restart
        );
        // The same for an EXE swap: exit so the helper can take over.
        assert_eq!(
            classify_owned_exit(
                Some(&plan("replace-exe", me, 1_795_471_800.0)),
                me,
                Some(1),
                now
            ),
            PlannedExit::ExitForUpgrade
        );
        // Expired plan, another app instance, no plan, unknown mode: all real failures.
        assert_eq!(
            classify_owned_exit(Some(&plan("hub", me, now - 10.0)), me, Some(1), now),
            PlannedExit::UnexpectedFailure
        );
        assert_eq!(
            classify_owned_exit(Some(&plan("hub", me + 1, 1_795_470_600.0)), me, Some(1), now),
            PlannedExit::UnexpectedFailure
        );
        assert_eq!(
            classify_owned_exit(Some(&plan("hub", 0, 1_795_470_600.0)), me, Some(1), now),
            PlannedExit::UnexpectedFailure
        );
        assert_eq!(
            classify_owned_exit(None, me, Some(1), now),
            PlannedExit::UnexpectedFailure
        );
        assert_eq!(
            classify_owned_exit(Some(&plan("shutdown", me, 1_795_470_600.0)), me, Some(1), now),
            PlannedExit::UnexpectedFailure
        );
        // A plan with no deadline at all can never justify skipping the error view.
        assert_eq!(
            classify_owned_exit(Some(&plan("hub", me, 0.0)), me, Some(1), now),
            PlannedExit::UnexpectedFailure
        );
    }

    #[test]
    fn a_reused_app_pid_never_turns_a_crash_into_a_planned_exit() {        let me = std::process::id();
        let now = 1_795_470_500.0;
        let mut reuse = plan("replace-exe", me, now + 100.0);
        reuse.app_creation_time = 7;
        // Same PID, different process instance: the plan belongs to an app that is already gone,
        // so this app's hub exit is a real crash and this app must not exit "for the upgrade".
        assert_eq!(
            classify_owned_exit(Some(&reuse), me, Some(9), now),
            PlannedExit::UnexpectedFailure
        );
        assert_eq!(
            classify_owned_exit(Some(&reuse), me, Some(7), now),
            PlannedExit::ExitForUpgrade
        );
        // A plan that names an instance can never be honoured without our own creation time.
        assert_eq!(
            classify_owned_exit(Some(&reuse), me, None, now),
            PlannedExit::UnexpectedFailure
        );
    }

    #[test]
    fn an_upgrade_hand_off_needs_the_staged_build_it_promises() {
        let temp = tempfile::tempdir().unwrap();
        let target = temp.path().join("app.exe");
        let staged = temp.path().join(".hub-update-x.exe");
        std::fs::write(&target, b"OLD").unwrap();
        std::fs::write(&staged, b"NEW").unwrap();
        let mut handoff = plan("replace-exe", 1, 1_795_471_800.0);
        handoff.target_exe = Some(target.clone());
        handoff.staged_exe = Some(staged.clone());
        handoff.new_exe_sha256 = Some("0".repeat(64));
        assert!(!upgrade_handoff_is_ready(&handoff), "staged hash mismatch");

        handoff.new_exe_sha256 = Some(update_helper::sha256_file(&staged).unwrap());
        assert!(upgrade_handoff_is_ready(&handoff));

        std::fs::remove_file(&staged).unwrap();
        assert!(!upgrade_handoff_is_ready(&handoff), "the staged build is gone");
        // The helper may have finished the swap already: a target with the new bytes is ready too.
        std::fs::write(&target, b"NEW").unwrap();
        assert!(upgrade_handoff_is_ready(&handoff));

        handoff.new_exe_sha256 = Some("zz".into());
        assert!(!upgrade_handoff_is_ready(&handoff), "a malformed hash never counts");
    }
}
