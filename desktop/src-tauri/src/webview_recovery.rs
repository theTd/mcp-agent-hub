//! WebView2 health for the `"main"` window: what a dead renderer / GPU process means, a surface
//! resync for a wedged D3D compositor, and a clean rebuild of that window that leaves the hub and
//! its sessions untouched.
//!
//! The decision logic ([`WebViewHealth`]) is pure Rust and unit-tested: it classifies a
//! `ProcessFailed` event into "do nothing", "reload", or "rebuild", keeps a cooldown between
//! automated recoveries and caps automated rebuilds per 5 minutes so a permanently broken GPU
//! driver cannot ping-pong the window forever. The WebView2 and HWND work sits behind
//! `#[cfg(windows)]`; everything else (geometry bookkeeping, the rebuild guard) is portable.
//!
//! Why a rebuild at all: `GPU_PROCESS_EXITED` / `Tab1 RenderProcessGone` leave WebView2's D3D
//! compositor surface (`Intermediate D3D Window`) blank and wedged — `Reload` and
//! `ReloadBypassingCache` do not bring it back. Only a fresh `ICoreWebView2` (and therefore a fresh
//! `"main"` window) recovers the window, and that must not cost the user the running hub.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicU8, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

use tauri::{
    AppHandle, Manager, PhysicalPosition, PhysicalSize, Position, Size, WebviewWindow,
    WebviewWindowBuilder,
};

/// The window label everything here acts on.
pub const MAIN_LABEL: &str = "main";

/// A second automated recovery inside this interval is deferred: tearing the surface down again
/// before the previous attempt managed to paint only makes the window flicker.
pub const MIN_RECOVERY_INTERVAL: Duration = Duration::from_secs(2);
/// Two renderer failures this close together mean a reload cannot keep the renderer alive.
pub const RENDER_FAILURE_WINDOW: Duration = Duration::from_secs(60);
/// Renderer failures counted inside [`RENDER_FAILURE_WINDOW`] before a rebuild is chosen.
pub const RENDER_FAILURES_BEFORE_REBUILD: usize = 2;
/// Automated rebuilds are capped inside this window. The tray item 重建控制台窗口 is not capped:
/// the user may always ask for one more.
pub const REBUILD_BURST_WINDOW: Duration = Duration::from_secs(300);
/// Automated rebuilds allowed per [`REBUILD_BURST_WINDOW`].
pub const MAX_AUTOMATED_REBUILDS: usize = 3;

/// How long a rebuild waits for the destroyed window's label to be released by Tauri.
const LABEL_RELEASE_WAIT: Duration = Duration::from_secs(2);
const LABEL_RELEASE_POLL: Duration = Duration::from_millis(20);
/// How long a reload waits for the `ICoreWebView2::Reload()` call to report its `HRESULT` back.
const RELOAD_RESULT_WAIT: Duration = Duration::from_secs(2);
/// A remembered position/size only counts as usable while this much of it (in physical pixels)
/// stays on a monitor.
const MIN_VISIBLE_WIDTH: i32 = 80;
const MIN_VISIBLE_HEIGHT: i32 = 40;

/// Monotonic counter over `"main"` window *instances*: the startup window and every rebuilt one get
/// their own generation. A `ProcessFailed` event can outlive the window that reported it (the COM
/// callback is queued, and a destroy does not cancel it), so each handler stamps its events and
/// [`WebViewHealth`] only acts on the ones that still describe the live window.
#[derive(Debug, Default)]
pub struct WindowGenerations(AtomicU64);

impl WindowGenerations {
    pub const fn new() -> Self {
        WindowGenerations(AtomicU64::new(0))
    }

    /// Reserve the generation of a new `"main"` window and make it the live one.
    pub fn begin(&self) -> u64 {
        self.0.fetch_add(1, Ordering::SeqCst) + 1
    }

    /// The generation of the window that is live now (`0` before any window was registered).
    pub fn current(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }

    /// True when an event stamped `generation` still describes the live window. Anything older came
    /// from a window that has already been replaced and must be ignored, never recovered from.
    pub fn is_current(&self, generation: u64) -> bool {
        generation >= self.current()
    }
}

static GENERATIONS: WindowGenerations = WindowGenerations::new();

/// Reserve the generation of a new `"main"` window instance (startup or rebuilt).
pub fn begin_window_generation() -> u64 {
    GENERATIONS.begin()
}

/// The generation of the `"main"` window that is live now.
pub fn current_window_generation() -> u64 {
    GENERATIONS.current()
}

/// True when a failure stamped `generation` may still be acted on.
pub fn generation_is_current(generation: u64) -> bool {
    GENERATIONS.is_current(generation)
}

/// What one hook-installation attempt found, or achieved, for a window's `ProcessFailed` slot.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RegistrationClaim {
    /// The caller owns the installation and must report the outcome with `confirm` / `release`.
    Acquired,
    /// Another attempt is installing right now: wait a moment and look again.
    InProgress,
    /// The handler is installed; there is nothing left to do on this window instance.
    Installed,
}

const HOOK_UNCLAIMED: u8 = 0;
const HOOK_INSTALLING: u8 = 1;
const HOOK_INSTALLED: u8 = 2;

/// Registration state of the `ProcessFailed` handler on **one** `"main"` window instance.
///
/// `with_webview` cannot cancel a closure that was already dispatched, so the retry loop in
/// [`attach_process_failed_hook`] can leave several closures running for the same window — a
/// `recv_timeout` tells the loop "not yet", not the closure "never mind". Every closure claims this
/// slot first, so exactly one of them can ever reach `add_ProcessFailed`; a failed installation
/// releases the slot again so a later attempt may retry.
#[derive(Debug, Default)]
pub struct ProcessFailedRegistration {
    state: AtomicU8,
    installed: AtomicU32,
    skipped: AtomicU32,
}

impl ProcessFailedRegistration {
    pub fn new() -> Self {
        Self::default()
    }

    /// Try to become the one attempt allowed to call `add_ProcessFailed`.
    pub fn claim(&self) -> RegistrationClaim {
        match self
            .state
            .compare_exchange(HOOK_UNCLAIMED, HOOK_INSTALLING, Ordering::SeqCst, Ordering::SeqCst)
        {
            Ok(_) => RegistrationClaim::Acquired,
            Err(HOOK_INSTALLED) => {
                self.skipped.fetch_add(1, Ordering::SeqCst);
                RegistrationClaim::Installed
            }
            Err(_) => {
                self.skipped.fetch_add(1, Ordering::SeqCst);
                RegistrationClaim::InProgress
            }
        }
    }

    /// The claim holder registered the handler: the slot is taken for the rest of this window's life.
    pub fn confirm(&self) {
        self.installed.fetch_add(1, Ordering::SeqCst);
        self.state.store(HOOK_INSTALLED, Ordering::SeqCst);
    }

    /// The claim holder could not register (no `CoreWebView2` yet): let a later attempt retry.
    pub fn release(&self) {
        self.state.store(HOOK_UNCLAIMED, Ordering::SeqCst);
    }

    /// Test-only: whether the handler is in place.
    #[cfg(test)]
    pub fn is_installed(&self) -> bool {
        self.state.load(Ordering::SeqCst) == HOOK_INSTALLED
    }

    /// Successful registrations — never more than one per window instance.
    pub fn installed_count(&self) -> u32 {
        self.installed.load(Ordering::SeqCst)
    }

    /// Attempts that found the slot taken or being installed (diagnostics, and the duplicate
    /// registrations the retry loop used to produce).
    pub fn skipped_count(&self) -> u32 {
        self.skipped.load(Ordering::SeqCst)
    }
}

/// Run one installation attempt under the window's single registration claim.
///
/// Returns `true` when the handler is in place afterwards (either this attempt installed it, or
/// another one already had). `install` returns `true` when `add_ProcessFailed` succeeded.
fn install_once<F>(registration: &ProcessFailedRegistration, install: F) -> bool
where
    F: FnOnce() -> bool,
{
    match registration.claim() {
        RegistrationClaim::Acquired => {
            if install() {
                registration.confirm();
                true
            } else {
                registration.release();
                false
            }
        }
        RegistrationClaim::Installed => true,
        RegistrationClaim::InProgress => false,
    }
}

/// What the shell still owes the WebView2 surface. Set while the window is hidden in the tray (a
/// recovery there would be invisible) or while an automated recovery is on cooldown, and consumed
/// by the next reveal / tray click.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Default)]
pub enum PendingRecovery {
    #[default]
    None,
    Reload,
    Rebuild,
}

impl PendingRecovery {
    pub fn label(self) -> &'static str {
        match self {
            PendingRecovery::None => "none",
            PendingRecovery::Reload => "reload",
            PendingRecovery::Rebuild => "rebuild",
        }
    }
}

/// The recovery a failure calls for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum RecoveryAction {
    #[default]
    None,
    Reload,
    Rebuild,
}

impl RecoveryAction {
    pub fn label(self) -> &'static str {
        match self {
            RecoveryAction::None => "none",
            RecoveryAction::Reload => "reload",
            RecoveryAction::Rebuild => "rebuild",
        }
    }

    /// The plan to keep for the next reveal.
    pub fn pending(self) -> PendingRecovery {
        match self {
            RecoveryAction::None => PendingRecovery::None,
            RecoveryAction::Reload => PendingRecovery::Reload,
            RecoveryAction::Rebuild => PendingRecovery::Rebuild,
        }
    }
}

/// Who asked for the recovery: automated ones are cooled down and capped, a user's click is not.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Trigger {
    Automated,
    Manual,
}

/// The `COREWEBVIEW2_PROCESS_FAILED_KIND` families this shell cares about. `.0` is the raw WebView2
/// kind code, so the mapping stays testable without the COM type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProcessFailureKind {
    /// `GPU_PROCESS_EXITED` (and the unresponsive variant): the D3D surface stays wedged.
    GpuProcessExited,
    GpuProcessUnresponsive,
    /// `BROWSER_PROCESS_EXITED`: the whole WebView2 is gone, nothing can be reloaded.
    BrowserProcessExited,
    /// `RENDER_PROCESS_EXITED` / `FRAME_RENDER_PROCESS_EXITED`: a reload usually brings it back.
    RenderProcessExited,
    /// `RENDER_PROCESS_UNRESPONSIVE`: hung renderer, recover it before it gets killed.
    RenderProcessUnresponsive,
    /// Utility / PPAPI / sandbox helper / unknown: log it, never force a rebuild for it.
    Other,
}

impl ProcessFailureKind {
    pub fn label(self) -> &'static str {
        match self {
            ProcessFailureKind::GpuProcessExited => "gpu-process-exited",
            ProcessFailureKind::GpuProcessUnresponsive => "gpu-process-unresponsive",
            ProcessFailureKind::BrowserProcessExited => "browser-process-exited",
            ProcessFailureKind::RenderProcessExited => "render-process-exited",
            ProcessFailureKind::RenderProcessUnresponsive => "render-process-unresponsive",
            ProcessFailureKind::Other => "other",
        }
    }

    /// True for the families whose compositor surface survives neither a reload nor a
    /// cache-bypassing reload, so the window has to be rebuilt.
    pub fn forces_rebuild(self) -> bool {
        matches!(
            self,
            ProcessFailureKind::GpuProcessExited
                | ProcessFailureKind::GpuProcessUnresponsive
                | ProcessFailureKind::BrowserProcessExited
        )
    }

    pub fn is_render(self) -> bool {
        matches!(
            self,
            ProcessFailureKind::RenderProcessExited | ProcessFailureKind::RenderProcessUnresponsive
        )
    }

    /// Map a `COREWEBVIEW2_PROCESS_FAILED_KIND` / `_REASON` pair onto this enum. Codes:
    /// kind 0 browser, 1 render, 2 render-unresponsive, 3 frame-render, 6 gpu; reason 1 is
    /// "unresponsive" (the WebView2 API has no dedicated GPU-unresponsive kind, some runtime builds
    /// report a hung GPU process as an exited one with that reason — both force a rebuild).
    pub fn from_core(kind: i32, reason: i32) -> Self {
        match kind {
            0 => ProcessFailureKind::BrowserProcessExited,
            1 | 3 => ProcessFailureKind::RenderProcessExited,
            2 => ProcessFailureKind::RenderProcessUnresponsive,
            6 if reason == 1 => ProcessFailureKind::GpuProcessUnresponsive,
            6 => ProcessFailureKind::GpuProcessExited,
            _ => ProcessFailureKind::Other,
        }
    }
}

/// A `ProcessFailed` event, in the shape the health logic and the log line need.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessFailure {
    /// Raw `COREWEBVIEW2_PROCESS_FAILED_KIND`.
    pub core_kind: i32,
    /// Raw `COREWEBVIEW2_PROCESS_FAILED_REASON` (`-1` when the event carried no detail).
    pub core_reason: i32,
    pub kind: ProcessFailureKind,
    pub exit_code: Option<i32>,
    /// WebView2's own description of the failed process (e.g. `Tab1`).
    pub description: String,
}

impl ProcessFailure {
    pub fn new(core_kind: i32, core_reason: i32, exit_code: Option<i32>, description: String) -> Self {
        ProcessFailure {
            core_kind,
            core_reason,
            kind: ProcessFailureKind::from_core(core_kind, core_reason),
            exit_code,
            description,
        }
    }

    pub fn reason_label(&self) -> &'static str {
        match self.core_reason {
            0 => "unexpected",
            1 => "unresponsive",
            2 => "terminated",
            3 => "crashed",
            4 => "launch-failed",
            5 => "out-of-memory",
            6 => "profile-deleted",
            _ => "unknown",
        }
    }

    /// One `desktop.log` line: kind, reason, exit code and the process WebView2 named.
    pub fn log_line(&self) -> String {
        let exit = self
            .exit_code
            .map(|code| code.to_string())
            .unwrap_or_else(|| "-".to_string());
        let process = if self.description.trim().is_empty() {
            "-"
        } else {
            self.description.trim()
        };
        format!(
            "webview2 process failed: kind={} (core {}) reason={} (core {}) exit={exit} process={process}",
            self.kind.label(),
            self.core_kind,
            self.reason_label(),
            self.core_reason
        )
    }
}

/// Health, pending plan and burst accounting for one `"main"` WebView2. All decisions take `now`
/// as an argument so they are deterministic and testable.
#[derive(Debug, Default)]
pub struct WebViewHealth {
    pending: PendingRecovery,
    last_recovery: Option<Instant>,
    /// Timestamps of the renderer failures inside [`RENDER_FAILURE_WINDOW`].
    render_failures: Vec<Instant>,
    /// Timestamps of the automated rebuilds inside [`REBUILD_BURST_WINDOW`].
    automated_rebuilds: Vec<Instant>,
    /// Diagnostics only (logged with the rebuild): failures and rebuilds seen this session.
    failures_seen: u32,
    rebuilds_seen: u32,
}

impl WebViewHealth {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set_pending(&mut self, pending: PendingRecovery) {
        if pending > self.pending {
            self.pending = pending;
        }
    }

    /// Consume the pending plan: the caller is about to act on it.
    pub fn take_pending(&mut self) -> PendingRecovery {
        std::mem::take(&mut self.pending)
    }

    pub fn rebuilds_seen(&self) -> u32 {
        self.rebuilds_seen
    }

    /// Classify one failure and return the recovery it calls for, independent of the cooldown and
    /// the burst cap (the caller decides whether it may start now).
    pub fn classify(&mut self, failure: &ProcessFailure, now: Instant) -> RecoveryAction {
        self.failures_seen += 1;
        if failure.kind.is_render() {
            self.render_failures.push(now);
            self.render_failures
                .retain(|at| now.saturating_duration_since(*at) <= RENDER_FAILURE_WINDOW);
        }
        if failure.kind.forces_rebuild() {
            return RecoveryAction::Rebuild;
        }
        if failure.kind.is_render() {
            return if self.render_failures.len() >= RENDER_FAILURES_BEFORE_REBUILD {
                RecoveryAction::Rebuild
            } else {
                RecoveryAction::Reload
            };
        }
        RecoveryAction::None
    }

    /// Whether an automated recovery of `action` may start now: not inside the cooldown, and (for
    /// rebuilds) not above the automated burst cap.
    pub fn automated_allowed(&self, action: RecoveryAction, now: Instant) -> bool {
        if action == RecoveryAction::None {
            return false;
        }
        if let Some(last) = self.last_recovery {
            if now.saturating_duration_since(last) < MIN_RECOVERY_INTERVAL {
                return false;
            }
        }
        if action == RecoveryAction::Rebuild
            && self.automated_rebuilds_within(REBUILD_BURST_WINDOW, now) >= MAX_AUTOMATED_REBUILDS
        {
            return false;
        }
        true
    }

    pub fn automated_rebuilds_within(&self, window: Duration, now: Instant) -> usize {
        self.automated_rebuilds
            .iter()
            .filter(|at| now.saturating_duration_since(**at) <= window)
            .count()
    }

    /// Record that a recovery really started. A rebuild drops the pending plan (it is executing
    /// now) and forgets the renderer failures of the WebView that is going away.
    pub fn begin(&mut self, action: RecoveryAction, trigger: Trigger, now: Instant) {
        if action == RecoveryAction::None {
            return;
        }
        self.last_recovery = Some(now);
        if action == RecoveryAction::Rebuild {
            self.pending = PendingRecovery::None;
            self.render_failures.clear();
            self.rebuilds_seen += 1;
            if trigger == Trigger::Automated {
                self.automated_rebuilds.push(now);
                self.automated_rebuilds
                    .retain(|at| now.saturating_duration_since(*at) <= REBUILD_BURST_WINDOW);
            }
        }
    }

    /// The rebuild did not produce a window (the old label was never released, or the replacement
    /// could not be built): put the work back as pending, so the next tray click / reveal retries it
    /// instead of leaving the app with no window at all.
    pub fn record_rebuild_failed(&mut self) -> PendingRecovery {
        self.pending = PendingRecovery::Rebuild;
        PendingRecovery::Rebuild
    }

    /// The reload could not even be started, so the surface is exactly as broken as before:
    /// escalate to the rebuild the reload was meant to avoid.
    pub fn record_reload_failed(&mut self) -> RecoveryAction {
        self.pending = PendingRecovery::Rebuild;
        RecoveryAction::Rebuild
    }
}

fn health_slot() -> &'static Mutex<WebViewHealth> {
    static SLOT: OnceLock<Mutex<WebViewHealth>> = OnceLock::new();
    SLOT.get_or_init(|| Mutex::new(WebViewHealth::new()))
}

/// The process-wide health of the `"main"` WebView2. One window, one health record.
pub fn health() -> MutexGuard<'static, WebViewHealth> {
    health_slot().lock().unwrap_or_else(|error| error.into_inner())
}

/// Take the pending plan, if any.
pub fn take_pending() -> PendingRecovery {
    health().take_pending()
}

/// Remember a recovery for the next reveal (never lowers what is already pending).
pub fn defer(action: RecoveryAction) {
    health().set_pending(action.pending());
}

/// Automated rebuilds allowed right now? (cooldown + burst cap)
pub fn automated_recovery_allowed(action: RecoveryAction) -> bool {
    health().automated_allowed(action, Instant::now())
}

/// Drop a recovery plan that belonged to the window being replaced.
///
/// Must run **before** the new window's generation is reserved and **before** its `ProcessFailed`
/// hook is attached: clearing afterwards can erase a failure the new window itself reported, which
/// used to hide a freshly broken window behind "a stale plan".
fn forget_stale_pending(app: &AppHandle) {
    let stale = take_pending();
    if stale != PendingRecovery::None {
        log_from(
            app,
            &format!(
                "webview2 rebuild: dropping a {} plan from the window that was replaced",
                stale.label()
            ),
        );
    }
}

/// A rebuild that produced no window re-arms the plan it consumed, so the app never stays tray-only.
fn record_rebuild_failed(app: &AppHandle, detail: &str) -> PendingRecovery {
    let pending = health().record_rebuild_failed();
    log_from(
        app,
        &format!(
            "webview2 rebuild failed: {detail}; {} kept pending so the next tray click / reveal retries it",
            pending.label()
        ),
    );
    pending
}

/// A reload that WebView2 refused leaves the surface unrepaired: record the rebuild it now needs.
pub fn record_reload_failure(app: &AppHandle) -> RecoveryAction {
    let action = health().record_reload_failed();
    log_from(
        app,
        &format!("webview2: reload failed; escalating to {}", action.label()),
    );
    action
}

/// A rectangle in **physical pixels**, used for "is this still on a monitor".
///
/// Windows answers both halves in physical pixels (`available_monitors` / `Monitor::position` /
/// `Monitor::size`, `Window::outer_position` / `Window::inner_size`), and that is the only space
/// where a window on monitor A and a monitor B at another DPI are comparable at all. Dividing by
/// one window's scale factor (the previous behaviour) put the monitors and the window into different
/// logical spaces and moved the window off its place on every mixed-DPI setup.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PhysicalRect {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

impl PhysicalRect {
    pub fn new(x: i32, y: i32, width: u32, height: u32) -> Self {
        PhysicalRect { x, y, width, height }
    }

    pub fn right(&self) -> i32 {
        self.x.saturating_add(self.width as i32)
    }

    pub fn bottom(&self) -> i32 {
        self.y.saturating_add(self.height as i32)
    }

    /// How much of this rectangle sits inside `other`, in pixels (never negative).
    pub fn overlap(&self, other: &PhysicalRect) -> (i32, i32) {
        let left = self.x.max(other.x);
        let top = self.y.max(other.y);
        let right = self.right().min(other.right());
        let bottom = self.bottom().min(other.bottom());
        (
            right.saturating_sub(left).max(0),
            bottom.saturating_sub(top).max(0),
        )
    }
}

/// True when enough of `window` sits on one of `monitors` for the user to see it. A window whose
/// saved position belongs to a monitor that is gone (or to a display that has been unplugged) must
/// fall back to the configured position instead of reappearing off screen.
pub fn on_screen(window: PhysicalRect, monitors: &[PhysicalRect]) -> bool {
    monitors.iter().any(|monitor| {
        let (width, height) = window.overlap(monitor);
        width >= MIN_VISIBLE_WIDTH && height >= MIN_VISIBLE_HEIGHT
    })
}

/// The geometry a rebuild reads from a window, kept exactly as Windows reported it.
fn window_geometry(position: PhysicalPosition<i32>, size: PhysicalSize<u32>) -> PhysicalRect {
    PhysicalRect::new(position.x, position.y, size.width, size.height)
}

/// The physical pair a builder gets back for a remembered geometry — never a logical
/// re-interpretation of it, and never a centred one.
fn restore_geometry(rect: PhysicalRect) -> (PhysicalSize<u32>, PhysicalPosition<i32>) {
    (
        PhysicalSize::new(rect.width.max(1), rect.height.max(1)),
        PhysicalPosition::new(rect.x, rect.y),
    )
}

fn geometry_slot() -> &'static Mutex<Option<PhysicalRect>> {
    static SLOT: OnceLock<Mutex<Option<PhysicalRect>>> = OnceLock::new();
    SLOT.get_or_init(|| Mutex::new(None))
}

pub fn remembered_geometry() -> Option<PhysicalRect> {
    *geometry_slot().lock().unwrap_or_else(|error| error.into_inner())
}

pub fn remember_geometry(rect: Option<PhysicalRect>) {
    *geometry_slot().lock().unwrap_or_else(|error| error.into_inner()) = rect;
}

/// Set while a `"main"` window rebuild is in flight. The `CloseRequested` handler must not
/// `prevent_close()` in that window (the rebuild is destroying it on purpose), and a second
/// recovery must not start on top of the first.
static REBUILDING: AtomicBool = AtomicBool::new(false);

pub fn is_rebuilding() -> bool {
    REBUILDING.load(Ordering::SeqCst)
}

/// Holds the rebuild flag for exactly one rebuild, including a panicking worker thread.
struct RebuildGuard(());

impl RebuildGuard {
    fn try_acquire() -> Option<Self> {
        REBUILDING
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .ok()
            .map(|_| RebuildGuard(()))
    }
}

impl Drop for RebuildGuard {
    fn drop(&mut self) {
        REBUILDING.store(false, Ordering::SeqCst);
    }
}

/// Throw the current `"main"` window away and build a fresh one, then (optionally) reveal it.
///
/// Runs on a worker thread on purpose: `WebviewWindow::destroy` is a queued message for the UI
/// event loop, so waiting for the label to disappear *on* the UI thread would wait for itself.
/// Building the replacement also lets Tauri do its own marshalling, so nothing here blocks the
/// event loop. The hub, its sessions and the owned process are never touched.
///
/// The navigation target is resolved by the worker *after* the new window exists, never here: the
/// session a window should open is only decided once the window can take it (see
/// `hub_supervisor::take_main_target`).
///
/// Returns `false` when another rebuild is already in flight (the caller then simply does not
/// navigate anywhere).
pub fn rebuild_main_window(app: &AppHandle, reveal: bool, trigger: Trigger, reason: &str) -> bool {
    let Some(guard) = RebuildGuard::try_acquire() else {
        log_from(app, &format!("webview2 rebuild ignored ({reason}): another rebuild is in flight"));
        return false;
    };
    {
        let mut health = health();
        health.begin(RecoveryAction::Rebuild, trigger, Instant::now());
        log_from(
            app,
            &format!(
                "webview2 rebuild requested ({reason}, {}, rebuilds so far {}, failures seen {})",
                match trigger {
                    Trigger::Automated => "automated",
                    Trigger::Manual => "manual",
                },
                health.rebuilds_seen(),
                health.failures_seen
            ),
        );
    }
    let worker_app = app.clone();
    let worker_reason = reason.to_string();
    match std::thread::Builder::new()
        .name("webview-rebuild".into())
        .spawn(move || rebuild_on_worker(worker_app, reveal, &worker_reason, guard))
    {
        Ok(_) => true,
        Err(error) => {
            log_from(app, &format!("webview2 rebuild: worker thread did not start: {error}"));
            record_rebuild_failed(app, "the rebuild worker thread did not start");
            false
        }
    }
}

fn rebuild_on_worker(app: AppHandle, reveal: bool, reason: &str, guard: RebuildGuard) {
    let _guard = guard;
    // A plan recorded while the old window was being torn down belongs to *that* window: drop it
    // before the new generation exists, so a failure the new window reports can never be erased
    // afterwards. Then reserve this window's generation, which makes every late event from the old
    // window stale.
    forget_stale_pending(&app);
    let generation = begin_window_generation();
    remember_old_geometry(&app);
    match app.get_webview_window(MAIN_LABEL) {
        Some(old) => {
            let _ = old.hide();
            if let Err(error) = old.destroy() {
                log_from(&app, &format!("webview2 rebuild: destroying the old window failed: {error}"));
            }
        }
        None => log_from(&app, "webview2 rebuild: there was no \"main\" window to destroy"),
    }
    let deadline = Instant::now() + LABEL_RELEASE_WAIT;
    while app.get_webview_window(MAIN_LABEL).is_some() && Instant::now() < deadline {
        std::thread::sleep(LABEL_RELEASE_POLL);
    }
    if app.get_webview_window(MAIN_LABEL).is_some() {
        record_rebuild_failed(&app, "the old window is still registered after destroy");
        return;
    }
    let window = match build_main_window(&app, generation) {
        Ok(window) => window,
        Err(error) => {
            log_from(&app, &format!("webview2 rebuild failed ({reason}): {error}"));
            record_rebuild_failed(&app, "the replacement window could not be built");
            return;
        }
    };
    log_from(&app, &format!("webview2 rebuild: \"main\" window is back ({reason})"));
    // The new window decides what to load *now*: consuming the pending session at this point lets a
    // session that arrived while the rebuild was in flight win over the snapshot taken when it
    // started, and means a stale session can never be replayed by a later "just open the console".
    if let Some(url) = crate::hub_supervisor::take_main_target(&app) {
        log_from(&app, &format!("webview2 rebuild: navigating the new window to {url}"));
        if let Err(error) = window.navigate(url.clone()) {
            log_from(&app, &format!("webview2 rebuild: navigation to {url} failed: {error}"));
        }
    }
    drop(_guard);
    if reveal {
        let reveal_app = app.clone();
        if let Err(error) = app.run_on_main_thread(move || {
            crate::hub_supervisor::reveal_main_window(&reveal_app);
        }) {
            log_from(&app, &format!("webview2 rebuild: reveal could not be scheduled: {error}"));
        }
    }
}

/// Read the current window's geometry **in physical pixels** and keep it only while enough of it is
/// on a monitor, so a rebuild never reappears on a display that is gone.
///
/// Position is the outer top-left, size is the *inner* size — the same pair the replacement takes
/// back (`set_position` / `set_size`, both physical), so repeated rebuilds neither drift nor grow.
/// Nothing is ever divided by a scale factor: one window's DPI cannot describe another monitor.
fn remember_old_geometry(app: &AppHandle) {
    let Some(window) = app.get_webview_window(MAIN_LABEL) else {
        return;
    };
    let (Ok(position), Ok(size)) = (window.outer_position(), window.inner_size()) else {
        return;
    };
    let rect = window_geometry(position, size);
    let monitors = monitor_rects(&window);
    if on_screen(rect, &monitors) {
        remember_geometry(Some(rect));
    } else {
        remember_geometry(None);
        log_from(app, "webview2 rebuild: the old position is off screen; using the configured one");
    }
}

/// The monitors in physical pixels — the same space `outer_position` / `inner_size` answer in.
fn monitor_rects(window: &WebviewWindow) -> Vec<PhysicalRect> {
    let Ok(monitors) = window.available_monitors() else {
        return Vec::new();
    };
    monitors
        .iter()
        .map(|monitor| {
            let position = monitor.position();
            let size = monitor.size();
            PhysicalRect::new(position.x, position.y, size.width, size.height)
        })
        .collect()
}

/// Build the `"main"` window from the app's own window configuration (title, minimum size,
/// decorations, start URL), restoring the remembered geometry and attaching the `ProcessFailed`
/// hook for `generation`.
///
/// The caller owns the generation (see [`begin_window_generation`]) so it can reserve it *before*
/// the window being replaced is destroyed, and clear the old plan before that. The window is left
/// hidden and un-navigated: the caller resolves the target once the window exists.
pub fn build_main_window(app: &AppHandle, generation: u64) -> Result<WebviewWindow, String> {
    let config = app
        .config()
        .app
        .windows
        .iter()
        .find(|window| window.label == MAIN_LABEL)
        .cloned()
        .ok_or_else(|| "tauri.conf.json 中没有 label 为 main 的窗口配置".to_string())?;
    let mut builder = WebviewWindowBuilder::from_config(app, &config).map_err(|e| e.to_string())?;
    builder = builder.visible(false);
    // The window is created exactly as configured — including its `center: true` — and the
    // remembered geometry is then restored in physical pixels below, while it is still hidden. The
    // builder only takes logical sizes, and converting a physical rectangle through one scale
    // factor is what broke mixed-DPI restores, so the authoritative placement never goes through it.
    let remembered = remembered_geometry();
    let window = builder.build().map_err(|e| format!("无法创建 main 窗口：{e}"))?;
    if let Some(rect) = remembered {
        // `center: true` from tauri.conf.json is applied while the window is created — on whatever
        // monitor Windows picks, with that monitor's DPI. Put the remembered geometry back in
        // physical pixels once the window exists, still hidden and before it is ever shown.
        let (size, position) = restore_geometry(rect);
        let _ = window.set_size(Size::Physical(size));
        let _ = window.set_position(Position::Physical(position));
    }
    attach_process_failed_hook(app, &window, generation);
    Ok(window)
}

/// Register the WebView2 `ProcessFailed` hook on a `"main"` window instance. Every failure is
/// logged; the planned recovery is dispatched immediately when the window is on screen and kept
/// pending when it is hidden in the tray or minimized (repairing an invisible surface would only
/// waste a rebuild).
///
/// The hook is installed from a worker thread because `with_webview` from the setup thread runs
/// inline, where `CoreWebView2` may not exist yet — and a hook that misses the startup window is
/// exactly the one the user needs after an upgrade. Off the main thread the request is queued, so
/// the closure runs once the event loop is up; a few short retries cover a WebView2 that is still
/// spinning up its browser process.
///
/// A `recv_timeout` does not cancel a closure that `with_webview` already dispatched, so several
/// of them can still run for this window. They all register through one
/// [`ProcessFailedRegistration`], which lets exactly one of them call `add_ProcessFailed` — the
/// duplicate handlers (and their duplicate recoveries) are impossible by construction now.
pub fn attach_process_failed_hook(app: &AppHandle, window: &WebviewWindow, generation: u64) {
    let hook_app = app.clone();
    let hook_window = window.clone();
    let report_app = app.clone();
    let registration = Arc::new(ProcessFailedRegistration::new());
    let spawned = std::thread::Builder::new()
        .name("webview-hook".into())
        .spawn(move || {
            const ATTEMPTS: usize = 5;
            const ATTEMPT_TIMEOUT: Duration = Duration::from_millis(700);
            for attempt in 1..=ATTEMPTS {
                let (installed_tx, installed_rx) = std::sync::mpsc::channel();
                let attempt_app = hook_app.clone();
                let attempt_registration = Arc::clone(&registration);
                let dispatched = hook_window.clone().with_webview(move |platform| {
                    let installed = install_once(&attempt_registration, || {
                        install_process_failed_hook(&attempt_app, platform, generation)
                    });
                    let _ = installed_tx.send(installed);
                });
                if let Err(error) = dispatched {
                    log_from(
                        &report_app,
                        &format!("webview2 ProcessFailed hook: with_webview failed: {error}"),
                    );
                    return;
                }
                match installed_rx.recv_timeout(ATTEMPT_TIMEOUT) {
                    Ok(true) => {
                        log_from(
                            &report_app,
                            &format!(
                                "webview2 ProcessFailed hook: ready for window generation {generation} \
                                 (registered {}, duplicate attempt(s) skipped {})",
                                registration.installed_count(),
                                registration.skipped_count()
                            ),
                        );
                        return;
                    }
                    Ok(false) if attempt < ATTEMPTS => {
                        std::thread::sleep(Duration::from_millis(250));
                    }
                    Ok(false) => {
                        log_from(
                            &report_app,
                            "webview2 ProcessFailed hook: the webview never became ready; \
                             automatic recovery stays off for this window",
                        );
                        return;
                    }
                    Err(_) if attempt < ATTEMPTS => {
                        std::thread::sleep(Duration::from_millis(250));
                    }
                    Err(_) => return,
                }
            }
        });
    if let Err(error) = spawned {
        log_from(app, &format!("webview2 ProcessFailed hook: thread did not start: {error}"));
    }
}

#[cfg(windows)]
fn install_process_failed_hook(
    app: &AppHandle,
    platform: tauri::webview::PlatformWebview,
    generation: u64,
) -> bool {
    use webview2_com::Microsoft::Web::WebView2::Win32::{
        ICoreWebView2, ICoreWebView2ProcessFailedEventArgs,
    };
    use webview2_com::ProcessFailedEventHandler;

    let controller = platform.controller();
    let webview = match unsafe { controller.CoreWebView2() } {
        Ok(webview) => webview,
        Err(error) => {
            log_from(app, &format!("webview2 ProcessFailed hook: CoreWebView2 is not ready: {error}"));
            return false;
        }
    };
    let event_app = app.clone();
    let handler = ProcessFailedEventHandler::create(Box::new(
        move |_sender: Option<ICoreWebView2>,
              args: Option<ICoreWebView2ProcessFailedEventArgs>|
              -> windows::core::Result<()> {
            if let Some(args) = args {
                let failure = read_process_failure(&args);
                log_from(&event_app, &failure.log_line());
                dispatch_failure(&event_app, failure, generation);
            }
            Ok(())
        },
    ));
    let mut token = 0i64;
    match unsafe { webview.add_ProcessFailed(&handler, &mut token) } {
        Ok(()) => {
            log_from(
                app,
                &format!("webview2 ProcessFailed hook installed (token {token}, generation {generation})"),
            );
            true
        }
        Err(error) => {
            log_from(app, &format!("webview2 ProcessFailed hook failed: {error}"));
            false
        }
    }
}

#[cfg(not(windows))]
fn install_process_failed_hook(
    _app: &AppHandle,
    _platform: tauri::webview::PlatformWebview,
    _generation: u64,
) -> bool {
    false
}

#[cfg(windows)]
fn read_process_failure(
    args: &webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2ProcessFailedEventArgs,
) -> ProcessFailure {
    use webview2_com::Microsoft::Web::WebView2::Win32::{
        ICoreWebView2ProcessFailedEventArgs2, COREWEBVIEW2_PROCESS_FAILED_KIND,
        COREWEBVIEW2_PROCESS_FAILED_REASON,
    };
    use windows::core::Interface;

    let mut kind = COREWEBVIEW2_PROCESS_FAILED_KIND::default();
    let core_kind = match unsafe { args.ProcessFailedKind(&mut kind) } {
        Ok(()) => kind.0,
        Err(_) => -1,
    };
    let mut core_reason = -1i32;
    let mut exit_code = None;
    let mut description = String::new();
    // Args2 carries reason / exit code / process description. An older runtime only implements
    // Args itself, and the kind alone is still enough to decide the recovery.
    if let Ok(details) = args.cast::<ICoreWebView2ProcessFailedEventArgs2>() {
        let mut reason = COREWEBVIEW2_PROCESS_FAILED_REASON::default();
        if unsafe { details.Reason(&mut reason) }.is_ok() {
            core_reason = reason.0;
        }
        let mut code = 0i32;
        if unsafe { details.ExitCode(&mut code) }.is_ok() {
            exit_code = Some(code);
        }
        let mut text = windows::core::PWSTR::null();
        if unsafe { details.ProcessDescription(&mut text) }.is_ok() {
            description = webview2_com::take_pwstr(text);
        }
    }
    ProcessFailure::new(core_kind, core_reason, exit_code, description)
}

/// Hand the failure back to the UI event loop before acting on it: destroying or rebuilding the
/// window from inside a WebView2 callback would tear down the COM object the callback runs on, and
/// reloading re-enters the WebView2 browser process from its own handler.
///
/// The post goes through a worker thread on purpose: `run_on_main_thread` from the main thread
/// runs the closure *inline*, which would put the recovery right back inside the COM callback.
#[cfg(windows)]
fn dispatch_failure(app: &AppHandle, failure: ProcessFailure, generation: u64) {
    let thread_app = app.clone();
    let report_app = app.clone();
    let spawned = std::thread::Builder::new()
        .name("webview-failure".into())
        .spawn(move || {
            let event_app = thread_app.clone();
            if let Err(error) = thread_app
                .run_on_main_thread(move || on_process_failed(&event_app, &failure, generation))
            {
                log_from(&report_app, &format!("webview2 recovery could not be scheduled: {error}"));
            }
        });
    if let Err(error) = spawned {
        log_from(app, &format!("webview2 recovery thread did not start: {error}"));
    }
}

/// Decide what one failure costs: nothing, a reload, or a full rebuild.
///
/// `generation` is the window instance that reported it. A failure whose window has since been
/// destroyed must never drive a recovery: the new window is healthy by definition, and acting on the
/// old one's event would rebuild the replacement for nothing.
fn on_process_failed(app: &AppHandle, failure: &ProcessFailure, generation: u64) {
    if !generation_is_current(generation) {
        log_from(
            app,
            &format!(
                "webview2: ignoring {} from a replaced window (generation {generation} < {})",
                failure.kind.label(),
                current_window_generation()
            ),
        );
        return;
    }
    let now = Instant::now();
    let desired = health().classify(failure, now);
    if desired == RecoveryAction::None {
        // Utility / sandbox-helper / unknown failures: logged above, never a rebuild.
        return;
    }
    // "The user can see it" — a window hidden in the tray or minimized has no blank surface to
    // repair right now, and rebuilding it would pop the window up at a random moment. The plan is
    // kept and executed by the reveal that brings it back.
    let visible = app
        .get_webview_window(MAIN_LABEL)
        .map(|window| {
            window.is_visible().unwrap_or(false) && !window.is_minimized().unwrap_or(false)
        })
        .unwrap_or(false);
    if !visible {
        defer(desired);
        log_from(
            app,
            &format!(
                "webview2: window is not on screen; {} kept pending until the next reveal",
                desired.label()
            ),
        );
        return;
    }
    if !automated_recovery_allowed(desired) {
        defer(desired);
        log_from(
            app,
            &format!(
                "webview2: {} deferred (cooldown or automated rebuild cap reached); the next reveal still repairs it",
                desired.label()
            ),
        );
        return;
    }
    match desired {
        RecoveryAction::Reload => {
            health().begin(RecoveryAction::Reload, Trigger::Automated, now);
            if let Some(window) = app.get_webview_window(MAIN_LABEL) {
                log_from(app, "webview2: reloading the console window after a renderer failure");
                match reload_webview(app, &window) {
                    Ok(()) => resync_surface(app, &window),
                    Err(error) => {
                        log_from(app, &format!("webview2: reload failed: {error}"));
                        escalate_reload_failure(app, &window);
                    }
                }
            }
        }
        RecoveryAction::Rebuild => {
            // The failure happened while the window is visible: show the rebuilt one too.
            rebuild_main_window(app, true, Trigger::Automated, "process_failed");
        }
        RecoveryAction::None => {}
    }
}

/// A reload that WebView2 refused leaves the surface exactly as broken as before, so the reload
/// cannot be the whole recovery: escalate to the rebuild it was meant to avoid, keeping the plan
/// pending while the window is off screen (the next reveal then rebuilds it).
fn escalate_reload_failure(app: &AppHandle, window: &WebviewWindow) {
    let action = record_reload_failure(app);
    let visible = window.is_visible().unwrap_or(false) && !window.is_minimized().unwrap_or(false);
    if visible {
        rebuild_main_window(app, true, Trigger::Automated, "reload_failed");
    } else {
        log_from(
            app,
            &format!(
                "webview2: window is not on screen; {} kept pending until the next reveal",
                action.label()
            ),
        );
    }
}

/// Reload the page through `ICoreWebView2::Reload()` and report what actually happened.
///
/// Tauri's `window.reload()` only reports whether the message was *queued*: a reload that the
/// WebView2 controller refuses (dead browser process, wedged controller) looks like a success there
/// and the window stays blank. `Reload()`'s `HRESULT` is the only honest answer, so the call runs
/// inside `with_webview` and its result is handed back through a channel.
pub fn reload_webview(app: &AppHandle, window: &WebviewWindow) -> Result<(), String> {
    let (result_tx, result_rx) = std::sync::mpsc::channel();
    let report_app = app.clone();
    let dispatched = window.with_webview(move |platform| {
        let result = reload_now(&report_app, platform);
        let _ = result_tx.send(result);
    });
    if let Err(error) = dispatched {
        log_from(app, &format!("webview2 reload: with_webview failed: {error}"));
        return Err(format!("with_webview failed: {error}"));
    }
    match result_rx.recv_timeout(RELOAD_RESULT_WAIT) {
        Ok(Ok(())) => Ok(()),
        Ok(Err(error)) => {
            log_from(app, &format!("webview2 reload: {error}"));
            Err(error)
        }
        Err(_) => {
            let error = "the WebView2 reload never reported back".to_string();
            log_from(app, &format!("webview2 reload: {error}"));
            Err(error)
        }
    }
}

#[cfg(windows)]
fn reload_now(_app: &AppHandle, platform: tauri::webview::PlatformWebview) -> Result<(), String> {
    let controller = platform.controller();
    // SAFETY: the controller belongs to this window's WebView2 and this runs on its owning thread.
    let webview = unsafe { controller.CoreWebView2() }
        .map_err(|error| format!("CoreWebView2 is not available: {error}"))?;
    unsafe { webview.Reload() }.map_err(|error| format!("ICoreWebView2::Reload failed: {error}"))
}

#[cfg(not(windows))]
fn reload_now(_app: &AppHandle, _platform: tauri::webview::PlatformWebview) -> Result<(), String> {
    Err("the WebView2 reload is Windows only".to_string())
}

/// Force WebView2 to re-create its composition surface without losing the page: toggle the
/// controller's visibility and re-apply the host HWND's client rect. Called on every reveal — after
/// a GPU crash a window that was merely shown again stays blank forever.
///
/// Only unexpected failures are logged, so a normal tray click stays quiet.
pub fn resync_surface(app: &AppHandle, window: &WebviewWindow) {
    let report_app = app.clone();
    let dispatch_app = app.clone();
    let surface = window.clone();
    let dispatched = window.with_webview(move |platform| {
        #[cfg(windows)]
        {
            let controller = platform.controller();
            // Off then on: WebView2 drops and re-creates the D3D surface, which is the whole point.
            let _ = unsafe { controller.SetIsVisible(false) };
            if let Err(error) = unsafe { controller.SetIsVisible(true) } {
                log_from(&report_app, &format!("webview2 surface resync: SetIsVisible(true) failed: {error}"));
                return;
            }
            match host_client_rect(&surface) {
                Ok(rect) => {
                    if let Err(error) = unsafe { controller.SetBounds(rect) } {
                        log_from(&report_app, &format!("webview2 surface resync: SetBounds failed: {error}"));
                    }
                    if let Err(error) = unsafe { controller.NotifyParentWindowPositionChanged() } {
                        log_from(
                            &report_app,
                            &format!("webview2 surface resync: NotifyParentWindowPositionChanged failed: {error}"),
                        );
                    }
                }
                Err(error) => log_from(&report_app, &format!("webview2 surface resync: {error}")),
            }
        }
        #[cfg(not(windows))]
        {
            let _ = (&report_app, platform);
        }
    });
    if let Err(error) = dispatched {
        log_from(&dispatch_app, &format!("webview2 surface resync: with_webview failed: {error}"));
    }
}

#[cfg(not(windows))]
fn host_client_rect(_window: &WebviewWindow) -> Result<(), String> {
    Err("WebView2 host rect is Windows only".to_string())
}

#[cfg(windows)]
fn host_client_rect(
    window: &WebviewWindow,
) -> Result<windows::Win32::Foundation::RECT, String> {
    use windows_sys::Win32::UI::WindowsAndMessaging::GetClientRect;

    let hwnd = window.hwnd().map_err(|error| format!("no host HWND: {error}"))?;
    let mut raw = windows_sys::Win32::Foundation::RECT {
        left: 0,
        top: 0,
        right: 0,
        bottom: 0,
    };
    // SAFETY: the HWND belongs to this window and `raw` is a live local RECT.
    if unsafe { GetClientRect(hwnd.0, &mut raw) } == 0 {
        return Err(format!(
            "GetClientRect failed (error {})",
            std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
        ));
    }
    Ok(windows::Win32::Foundation::RECT {
        left: raw.left,
        top: raw.top,
        right: raw.right,
        bottom: raw.bottom,
    })
}

/// Append a diagnostic line to `desktop.log` through the shell that owns it.
fn log_from(app: &AppHandle, line: &str) {
    crate::hub_supervisor::log_from(app, line);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The primary monitor: 2560x1440 physical pixels, scaled at 150 %.
    const PHYSICAL_PRIMARY_150: PhysicalRect = PhysicalRect { x: 0, y: 0, width: 2560, height: 1440 };
    /// A second monitor to the left of it: 1920x1080 physical pixels, scaled at 100 %.
    const PHYSICAL_SECONDARY_100: PhysicalRect =
        PhysicalRect { x: -1920, y: 0, width: 1920, height: 1080 };

    fn gpu() -> ProcessFailure {
        ProcessFailure::new(6, 3, Some(1073741819), "GPU Process".into())
    }

    fn render() -> ProcessFailure {
        ProcessFailure::new(1, 2, Some(1), "Tab1".into())
    }

    fn utility() -> ProcessFailure {
        ProcessFailure::new(4, 2, Some(0), "Utility Process".into())
    }

    fn base() -> Instant {
        Instant::now()
    }

    #[test]
    fn gpu_and_browser_failures_always_rebuild() {
        let now = base();
        let mut health = WebViewHealth::new();
        // A GPU death wedges the D3D surface: reload would keep the blank window.
        assert_eq!(health.classify(&gpu(), now), RecoveryAction::Rebuild);
        assert_eq!(
            health.classify(&ProcessFailure::new(0, 3, None, "Browser".into()), now),
            RecoveryAction::Rebuild
        );
        // The unresponsive GPU variant exists in WebView2 but has no dedicated kind code.
        assert_eq!(
            health.classify(&ProcessFailure::new(6, 1, None, "GPU".into()), now),
            RecoveryAction::Rebuild
        );
        assert_eq!(
            ProcessFailure::new(6, 1, None, String::new()).kind,
            ProcessFailureKind::GpuProcessUnresponsive
        );
    }

    #[test]
    fn one_renderer_failure_reloads_and_a_second_one_rebuilds() {
        let now = base();
        let mut health = WebViewHealth::new();
        assert_eq!(health.classify(&render(), now), RecoveryAction::Reload);
        // A second renderer death inside the window means the reload did not hold.
        assert_eq!(
            health.classify(&render(), now + Duration::from_secs(3)),
            RecoveryAction::Rebuild
        );
        // The frame-render variant counts as a renderer failure too.
        assert_eq!(
            ProcessFailure::new(3, 3, None, "Frame".into()).kind,
            ProcessFailureKind::RenderProcessExited
        );
    }

    #[test]
    fn renderer_failures_outside_the_window_start_over() {
        let now = base();
        let mut health = WebViewHealth::new();
        assert_eq!(health.classify(&render(), now), RecoveryAction::Reload);
        assert_eq!(
            health.classify(&render(), now + Duration::from_secs(61)),
            RecoveryAction::Reload
        );
        // Right after the second one, the window holds two failures again.
        assert_eq!(
            health.classify(&render(), now + Duration::from_secs(62)),
            RecoveryAction::Rebuild
        );
    }

    #[test]
    fn unresponsive_renderer_is_a_renderer_failure() {
        let mut health = WebViewHealth::new();
        let now = base();
        let failure = ProcessFailure::new(2, 1, None, "Tab1".into());
        assert_eq!(failure.kind, ProcessFailureKind::RenderProcessUnresponsive);
        assert_eq!(health.classify(&failure, now), RecoveryAction::Reload);
    }

    #[test]
    fn utility_and_unknown_failures_only_get_logged() {
        let now = base();
        let mut health = WebViewHealth::new();
        assert_eq!(health.classify(&utility(), now), RecoveryAction::None);
        assert_eq!(
            health.classify(&ProcessFailure::new(5, 3, None, "Sandbox".into()), now),
            RecoveryAction::None
        );
        assert_eq!(
            health.classify(&ProcessFailure::new(9, 3, None, String::new()), now),
            RecoveryAction::None
        );
        assert_eq!(
            health.classify(&ProcessFailure::new(-1, -1, None, String::new()), now),
            RecoveryAction::None
        );
        assert_eq!(health.failures_seen, 4);
        assert!(!health.automated_allowed(RecoveryAction::None, now));
        assert_eq!(health.take_pending(), PendingRecovery::None);
    }

    #[test]
    fn automated_recoveries_are_cooled_down() {
        let now = base();
        let mut health = WebViewHealth::new();
        assert!(health.automated_allowed(RecoveryAction::Reload, now));
        health.begin(RecoveryAction::Reload, Trigger::Automated, now);
        // 1 s later the surface is still settling.
        assert!(!health.automated_allowed(RecoveryAction::Reload, now + Duration::from_secs(1)));
        assert!(health.automated_allowed(RecoveryAction::Reload, now + MIN_RECOVERY_INTERVAL));
        // The classification is unaffected; only starting it now is refused.
        assert_eq!(
            health.classify(&render(), now + Duration::from_secs(1)),
            RecoveryAction::Reload
        );
    }

    #[test]
    fn automated_rebuilds_are_capped_per_five_minutes() {
        let now = base();
        let mut health = WebViewHealth::new();
        for step in 0..MAX_AUTOMATED_REBUILDS {
            let at = now + Duration::from_secs(step as u64 * 10);
            assert!(health.automated_allowed(RecoveryAction::Rebuild, at));
            health.begin(RecoveryAction::Rebuild, Trigger::Automated, at);
        }
        assert_eq!(
            health.automated_rebuilds_within(REBUILD_BURST_WINDOW, now + Duration::from_secs(30)),
            MAX_AUTOMATED_REBUILDS
        );
        // A permanently broken GPU driver must not loop the window forever.
        assert!(!health.automated_allowed(
            RecoveryAction::Rebuild,
            now + Duration::from_secs(40)
        ));
        // A reload is still allowed: it is cheap and may be all the surface needs.
        assert!(health.automated_allowed(
            RecoveryAction::Reload,
            now + Duration::from_secs(40)
        ));
        // Once the oldest rebuild leaves the window the budget comes back.
        assert!(health.automated_allowed(
            RecoveryAction::Rebuild,
            now + REBUILD_BURST_WINDOW + Duration::from_secs(1)
        ));
    }

    #[test]
    fn a_manual_rebuild_bypasses_the_automated_cap() {
        let now = base();
        let mut health = WebViewHealth::new();
        for step in 0..MAX_AUTOMATED_REBUILDS {
            health.begin(
                RecoveryAction::Rebuild,
                Trigger::Automated,
                now + Duration::from_secs(step as u64 * 10),
            );
        }
        // The tray item is allowed (nothing consults the cap for it), but it never eats the
        // automated budget and so cannot lock the shell out of its own safety valve.
        health.begin(RecoveryAction::Rebuild, Trigger::Manual, now + Duration::from_secs(31));
        assert_eq!(
            health.automated_rebuilds_within(REBUILD_BURST_WINDOW, now + Duration::from_secs(31)),
            MAX_AUTOMATED_REBUILDS
        );
        assert_eq!(
            health.rebuilds_seen() as usize,
            MAX_AUTOMATED_REBUILDS + 1
        );
        assert!(!health.automated_allowed(
            RecoveryAction::Rebuild,
            now + Duration::from_secs(32)
        ));
    }

    #[test]
    fn a_rebuild_executes_the_pending_plan_and_clears_the_renderer_window() {
        let now = base();
        let mut health = WebViewHealth::new();
        health.set_pending(PendingRecovery::Rebuild);
        health.classify(&render(), now);
        health.begin(RecoveryAction::Rebuild, Trigger::Automated, now);
        assert_eq!(health.take_pending(), PendingRecovery::None);
        // Failures of the WebView that just went away do not count against the new one.
        assert_eq!(
            health.classify(&render(), now + Duration::from_secs(5)),
            RecoveryAction::Reload
        );
    }

    #[test]
    fn pending_only_ever_escalates() {
        let mut health = WebViewHealth::new();
        assert_eq!(health.take_pending(), PendingRecovery::None);
        health.set_pending(PendingRecovery::Reload);
        // Clearing the plan (a nothing-to-do failure) must not throw the reload away.
        health.set_pending(PendingRecovery::None);
        assert_eq!(health.take_pending(), PendingRecovery::Reload);
        health.set_pending(PendingRecovery::Reload);
        health.set_pending(PendingRecovery::Rebuild);
        assert_eq!(health.take_pending(), PendingRecovery::Rebuild);
        assert_eq!(health.take_pending(), PendingRecovery::None);
        assert_eq!(RecoveryAction::Rebuild.pending(), PendingRecovery::Rebuild);
        assert_eq!(RecoveryAction::None.pending(), PendingRecovery::None);
    }

    #[test]
    fn recovery_actions_describe_themselves() {
        assert_eq!(RecoveryAction::Reload.label(), "reload");
        assert_eq!(RecoveryAction::Rebuild.label(), "rebuild");
        assert_eq!(ProcessFailureKind::GpuProcessExited.label(), "gpu-process-exited");
        assert_eq!(ProcessFailureKind::Other.label(), "other");
        assert!(ProcessFailureKind::BrowserProcessExited.forces_rebuild());
        assert!(ProcessFailureKind::GpuProcessUnresponsive.forces_rebuild());
        assert!(!ProcessFailureKind::Other.forces_rebuild());
        assert!(!ProcessFailureKind::RenderProcessExited.forces_rebuild());
    }

    #[test]
    fn a_failure_line_names_kind_reason_exit_and_process() {
        let failure = ProcessFailure::new(6, 5, Some(-1073741819), "Tab1 GPU".into());
        let line = failure.log_line();
        assert!(line.contains("kind=gpu-process-exited"), "{line}");
        assert!(line.contains("reason=out-of-memory"), "{line}");
        assert!(line.contains("exit=-1073741819"), "{line}");
        assert!(line.contains("process=Tab1 GPU"), "{line}");
        // Missing detail must not degrade the line.
        let bare = ProcessFailure::new(1, 2, None, String::new());
        let bare_line = bare.log_line();
        assert!(bare_line.contains("reason=terminated"), "{bare_line}");
        assert!(bare_line.contains("exit=-"), "{bare_line}");
        assert!(bare_line.contains("process=-"), "{bare_line}");
        assert_eq!(ProcessFailure::new(7, 0, None, String::new()).kind, ProcessFailureKind::Other);
    }

    #[test]
    fn a_window_that_is_still_on_a_monitor_keeps_its_place() {
        let monitors = [PHYSICAL_PRIMARY_150, PHYSICAL_SECONDARY_100];
        assert!(on_screen(
            PhysicalRect::new(300, 150, 2000, 1200),
            &monitors
        ));
        // Straddling the seam between the two monitors is still visible.
        assert!(on_screen(
            PhysicalRect::new(2400, 20, 2000, 1200),
            &monitors
        ));
        // Off to the right of every monitor: the configured position must win.
        assert!(!on_screen(
            PhysicalRect::new(4700, 0, 1400, 900),
            &monitors
        ));
        // Only a sliver left: not enough to be usable.
        assert!(!on_screen(
            PhysicalRect::new(2540, -1430, 1400, 1400),
            &monitors
        ));
        assert!(!on_screen(
            PhysicalRect::new(0, 0, 100, 100),
            &[]
        ));
    }

    #[test]
    fn mixed_dpi_monitors_overlap_in_physical_pixels() {
        // The real mixed-DPI case: a 150 % monitor next to a 100 % one, side by side, the 100 %
        // one to the *left* of the primary with negative coordinates.
        let monitors = [PHYSICAL_PRIMARY_150, PHYSICAL_SECONDARY_100];
        // 800 physical px of the window sit on the 100 % monitor: visible, no scale factor involved.
        let straddling = PhysicalRect::new(-720, 0, 2000, 1080);
        assert_eq!(straddling.overlap(&PHYSICAL_SECONDARY_100), (720, 1080));
        assert!(on_screen(straddling, &monitors));
        // A window whose logical position used to be "on the second monitor" but which only leaves
        // 60 physical px there is not usable.
        let sliver = PhysicalRect::new(-60, 0, 2000, 1080);
        assert_eq!(sliver.overlap(&PHYSICAL_SECONDARY_100), (60, 1080));
        assert!(!on_screen(sliver, &[PHYSICAL_SECONDARY_100]));
        // Overlap is never negative, even for rectangles that are far apart.
        assert_eq!(
            PhysicalRect::new(0, 0, 100, 100).overlap(&PhysicalRect::new(400, 400, 100, 100)),
            (0, 0)
        );
    }

    #[test]
    fn mixed_dpi_geometry_is_stored_and_restored_exactly_as_read() {
        // All values are physical pixels. Nothing is divided by a scale factor, so a 150 % monitor
        // cannot shrink the window to two thirds of its place on a rebuild.
        let read = window_geometry(
            PhysicalPosition::new(-1700, 240),
            PhysicalSize::new(2800, 1700),
        );
        assert_eq!(read, PhysicalRect::new(-1700, 240, 2800, 1700));
        let (size, position) = restore_geometry(read);
        assert_eq!(size, PhysicalSize::new(2800, 1700));
        assert_eq!(position, PhysicalPosition::new(-1700, 240));
        // The restored window is still on the 100 % monitor it came from.
        assert!(on_screen(read, &[PHYSICAL_PRIMARY_150, PHYSICAL_SECONDARY_100]));
        // A degenerate size is never passed through as zero.
        let zero = restore_geometry(PhysicalRect::new(10, 20, 0, 0));
        assert_eq!(zero.0, PhysicalSize::new(1, 1));
    }

    #[test]
    fn geometry_memory_round_trips() {
        remember_geometry(None);
        assert_eq!(remembered_geometry(), None);
        let rect = PhysicalRect::new(-1920, 40, 2800, 1700);
        remember_geometry(Some(rect));
        assert_eq!(remembered_geometry(), Some(rect));
        remember_geometry(None);
        assert_eq!(remembered_geometry(), None, "an unusable position is forgotten");
    }

    #[test]
    fn a_stale_window_generation_is_ignored_and_the_current_one_is_accepted() {
        let generations = WindowGenerations::new();
        assert_eq!(generations.current(), 0, "no window registered yet");
        let first = generations.begin();
        assert_eq!(first, 1);
        assert!(generations.is_current(first));
        let second = generations.begin();
        assert_eq!(second, 2);
        assert!(generations.is_current(second), "the live window's event is accepted");
        assert!(
            !generations.is_current(first),
            "a late event from the replaced window must be ignored"
        );
        assert!(!generations.is_current(0));
    }

    #[test]
    fn a_failure_from_the_new_window_is_not_discarded_by_the_handoff() {
        let generations = WindowGenerations::new();
        let mut health = WebViewHealth::new();
        let today = base();
        let old = generations.begin();
        // The old window's GPU death is why a rebuild is happening at all.
        assert_eq!(health.classify(&gpu(), today), RecoveryAction::Rebuild);
        health.set_pending(PendingRecovery::Rebuild);
        // The handoff in the order the worker performs it: the replaced window's plan is cleared
        // *before* the new generation is reserved and *before* its hook is attached ...
        assert_eq!(health.take_pending(), PendingRecovery::Rebuild);
        let new = generations.begin();
        assert!(new > old);
        // ... so a failure the new window reports right after this is recorded, never erased.
        assert_eq!(health.classify(&render(), today), RecoveryAction::Reload);
        assert_eq!(health.failures_seen, 2);
        health.set_pending(RecoveryAction::Reload.pending());
        assert_eq!(health.take_pending(), PendingRecovery::Reload);
        // And the replaced window's late event is stale: it cannot drive anything.
        assert!(!generations.is_current(old));
    }

    #[test]
    fn a_window_instance_registers_the_process_failed_handler_exactly_once() {
        let registration = ProcessFailedRegistration::new();
        let mut installs = 0;
        // The first closure to run installs the handler.
        assert!(install_once(&registration, || {
            installs += 1;
            true
        }));
        // Every delayed closure (the ones a `recv_timeout` left behind) finds the slot taken and
        // must not call `add_ProcessFailed` again.
        for _ in 0..4 {
            assert!(install_once(&registration, || {
                installs += 1;
                true
            }));
        }
        assert_eq!(installs, 1, "ProcessFailed must be registered at most once per window");
        assert_eq!(registration.installed_count(), 1);
        assert_eq!(registration.skipped_count(), 4);
        assert!(registration.is_installed());
    }

    #[test]
    fn a_failed_registration_is_released_for_a_later_attempt() {
        let registration = ProcessFailedRegistration::new();
        // No `CoreWebView2` yet: the closure releases the slot.
        assert!(!install_once(&registration, || false));
        assert!(!registration.is_installed());
        assert_eq!(registration.installed_count(), 0);
        // The retry installs it.
        assert!(install_once(&registration, || true));
        assert_eq!(registration.installed_count(), 1);
        assert!(registration.is_installed());
        // And again no second handler can be added afterwards.
        assert!(install_once(&registration, || true));
        assert_eq!(registration.installed_count(), 1);
    }

    #[test]
    fn a_concurrently_installing_closure_never_installs_on_top() {
        let registration = ProcessFailedRegistration::new();
        // Another closure is already inside `add_ProcessFailed`.
        assert_eq!(registration.claim(), RegistrationClaim::Acquired);
        let mut installs = 0;
        assert!(!install_once(&registration, || {
            installs += 1;
            true
        }));
        assert_eq!(installs, 0, "the second closure must not register");
        assert_eq!(registration.skipped_count(), 1);
        // Once the owner confirms, later attempts see an installed handler.
        registration.confirm();
        assert_eq!(registration.claim(), RegistrationClaim::Installed);
    }

    #[test]
    fn a_failed_rebuild_keeps_the_work_pending() {
        let mut health = WebViewHealth::new();
        let now = base();
        health.set_pending(PendingRecovery::Rebuild);
        health.begin(RecoveryAction::Rebuild, Trigger::Automated, now);
        assert_eq!(health.take_pending(), PendingRecovery::None, "a rebuild consumes the plan");
        // The old label was never released, or the replacement could not be built: the app must not
        // be left tray-only.
        assert_eq!(health.record_rebuild_failed(), PendingRecovery::Rebuild);
        assert_eq!(
            health.take_pending(),
            PendingRecovery::Rebuild,
            "the next tray click / reveal retries the rebuild"
        );
        assert_eq!(health.record_rebuild_failed(), PendingRecovery::Rebuild);
        assert!(health.automated_allowed(
            RecoveryAction::Reload,
            now + MIN_RECOVERY_INTERVAL
        ));
    }

    #[test]
    fn a_reload_that_cannot_start_escalates_to_a_rebuild() {
        let mut health = WebViewHealth::new();
        assert_eq!(health.record_reload_failed(), RecoveryAction::Rebuild);
        assert_eq!(health.take_pending(), PendingRecovery::Rebuild);
        // Even a pending reload plan is escalated, never left as a reload.
        health.set_pending(PendingRecovery::Reload);
        assert_eq!(health.record_reload_failed(), RecoveryAction::Rebuild);
        assert_eq!(health.take_pending(), PendingRecovery::Rebuild);
    }

    #[test]
    fn the_rebuild_flag_is_exclusive_and_always_released() {
        let first = RebuildGuard::try_acquire().expect("first rebuild");
        assert!(is_rebuilding());
        assert!(RebuildGuard::try_acquire().is_none(), "no second rebuild on top");
        drop(first);
        assert!(!is_rebuilding());
        {
            let _second = RebuildGuard::try_acquire().expect("after the first finished");
            assert!(is_rebuilding());
        }
        assert!(!is_rebuilding());
    }
}
