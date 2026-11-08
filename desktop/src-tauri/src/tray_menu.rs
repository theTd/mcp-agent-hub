//! The tray icon: dynamic badge with the active agent count, tooltip with session counts, left
//! click shows the window, and the menu 打开总控台 · 重启 Hub · 开机启动 · 打开日志目录 · (external only:
//! 接管 Hub) · 退出.

use std::sync::{Arc, Mutex, OnceLock};

use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Wry};
use tauri_plugin_autostart::ManagerExt;

use crate::hub_api::SessionCounts;
use crate::hub_supervisor::{reveal_main_window, HubMode, HubShell};

pub const TRAY_ID: &str = "hub-tray";
pub const EXTERNAL_LABEL: &str = "外部 Hub（不随应用退出）";

const BASE_ICON_PNG: &[u8] = include_bytes!("../icons/32x32.png");

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BadgeState {
    None,
    Active { live: usize, waiting: bool },
    Failed,
}

pub fn badge_state(mode: HubMode, counts: &SessionCounts) -> BadgeState {
    match mode {
        HubMode::Owned | HubMode::External => BadgeState::Active {
            live: counts.live,
            waiting: counts.waiting > 0,
        },
        HubMode::Failed => BadgeState::Failed,
        HubMode::Starting | HubMode::Stopping => BadgeState::None,
    }
}

/// 4x7 bitmap glyphs for '0'..='9', '+', and '!'. Each row is 4 bits (MSB on the left).
fn glyph_4x7(ch: char) -> [u8; 7] {
    match ch {
        '0' => [0b0110, 0b1001, 0b1001, 0b1001, 0b1001, 0b1001, 0b0110],
        '1' => [0b0010, 0b0110, 0b0010, 0b0010, 0b0010, 0b0010, 0b0111],
        '2' => [0b0110, 0b1001, 0b0001, 0b0010, 0b0100, 0b1000, 0b1111],
        '3' => [0b1110, 0b0001, 0b0001, 0b0110, 0b0001, 0b0001, 0b1110],
        '4' => [0b1001, 0b1001, 0b1001, 0b1111, 0b0001, 0b0001, 0b0001],
        '5' => [0b1111, 0b1000, 0b1000, 0b1110, 0b0001, 0b0001, 0b1110],
        '6' => [0b0111, 0b1000, 0b1000, 0b1110, 0b1001, 0b1001, 0b0110],
        '7' => [0b1111, 0b0001, 0b0010, 0b0010, 0b0100, 0b0100, 0b0100],
        '8' => [0b0110, 0b1001, 0b1001, 0b0110, 0b1001, 0b1001, 0b0110],
        '9' => [0b0110, 0b1001, 0b1001, 0b0111, 0b0001, 0b0001, 0b1110],
        '+' => [0b0000, 0b0100, 0b0100, 0b1110, 0b0100, 0b0100, 0b0000],
        '!' => [0b0110, 0b0110, 0b0110, 0b0110, 0b0000, 0b0110, 0b0110],
        _ => [0; 7],
    }
}

pub fn badge_label(state: BadgeState) -> Option<String> {
    match state {
        BadgeState::None => None,
        BadgeState::Failed => Some("!".to_string()),
        BadgeState::Active { live, .. } => {
            if live > 99 {
                Some("99".to_string())
            } else {
                Some(live.to_string())
            }
        }
    }
}

fn badge_fill(state: BadgeState) -> [u8; 4] {
    match state {
        BadgeState::None => [0, 0, 0, 0],
        BadgeState::Failed => [220, 38, 38, 255],
        BadgeState::Active { waiting: true, .. } => [217, 119, 6, 255],
        BadgeState::Active {
            live: 0,
            waiting: false,
        } => [51, 65, 85, 255],
        BadgeState::Active { waiting: false, .. } => [5, 150, 105, 255],
    }
}

fn set_px(rgba: &mut [u8], width: u32, x: i32, y: i32, color: [u8; 4]) {
    if x < 0 || y < 0 || x >= width as i32 || y >= width as i32 {
        return;
    }
    let idx = ((y as u32 * width + x as u32) * 4) as usize;
    if idx + 4 <= rgba.len() {
        rgba[idx..idx + 4].copy_from_slice(&color);
    }
}

/// Overlay a high-contrast pill badge on a 32x32 RGBA icon buffer. Each 4x7 glyph is drawn at 2x
/// scale (8x14 px) so digits stay crisp even when Windows scales the tray icon down to 16x16.
pub fn render_badge_rgba(base_rgba: &[u8], width: u32, height: u32, state: BadgeState) -> Vec<u8> {
    let mut out = base_rgba.to_vec();
    let Some(label) = badge_label(state) else {
        return out;
    };
    if width < 32 || height < 32 || out.len() < (width * height * 4) as usize {
        return out;
    }
    let chars: Vec<char> = label.chars().take(2).collect();
    let two_chars = chars.len() == 2;
    let pill_w: i32 = if two_chars { 24 } else { 16 };
    let pill_h: i32 = 18;
    let x0: i32 = width as i32 - pill_w;
    let y0: i32 = height as i32 - pill_h;
    let fill = badge_fill(state);
    let border = [11u8, 14, 20, 255];

    for dy in -1..=pill_h {
        for dx in -1..=pill_w {
            let px = x0 + dx;
            let py = y0 + dy;
            // Cut corners for a rounded pill shape.
            let in_outer = !(dx == -1 && (dy <= 1 || dy >= pill_h - 1)
                || dx == pill_w && (dy <= 1 || dy >= pill_h - 1)
                || dy == -1 && (dx <= 1 || dx >= pill_w - 1)
                || dy == pill_h && (dx <= 1 || dx >= pill_w - 1));
            if !in_outer {
                continue;
            }
            let in_inner = dx >= 0
                && dx < pill_w
                && dy >= 0
                && dy < pill_h
                && !((dx == 0 || dx == pill_w - 1) && (dy == 0 || dy == pill_h - 1));
            set_px(
                &mut out,
                width,
                px,
                py,
                if in_inner { fill } else { border },
            );
        }
    }

    let text_w = chars.len() as i32 * 8 + (chars.len().saturating_sub(1) as i32) * 2;
    let start_x = x0 + (pill_w - text_w) / 2;
    let start_y = y0 + (pill_h - 14) / 2;
    let fg = [255u8, 255, 255, 255];

    for (ci, &ch) in chars.iter().enumerate() {
        let rows = glyph_4x7(ch);
        let gx = start_x + ci as i32 * 10;
        for (ry, bits) in rows.iter().enumerate() {
            for rx in 0..4 {
                if (bits >> (3 - rx)) & 1 != 0 {
                    let px = gx + rx * 2;
                    let py = start_y + (ry as i32) * 2;
                    set_px(&mut out, width, px, py, fg);
                    set_px(&mut out, width, px + 1, py, fg);
                    set_px(&mut out, width, px, py + 1, fg);
                    set_px(&mut out, width, px + 1, py + 1, fg);
                }
            }
        }
    }
    out
}

fn build_tray_image(state: BadgeState) -> Option<Image<'static>> {
    let base = Image::from_bytes(BASE_ICON_PNG).ok()?;
    let rgba = render_badge_rgba(base.rgba(), base.width(), base.height(), state);
    Some(Image::new_owned(rgba, base.width(), base.height()))
}

fn last_badge_cache() -> &'static Mutex<Option<BadgeState>> {
    static CACHE: OnceLock<Mutex<Option<BadgeState>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(None))
}

pub fn tooltip_text(mode: HubMode, counts: &SessionCounts) -> String {
    match mode {
        HubMode::Owned => format!(
            "MCP Agent Hub — {} 个会话，{} 个等待",
            counts.live, counts.waiting
        ),
        HubMode::External => format!(
            "MCP Agent Hub — {} 个会话，{} 个等待\n{EXTERNAL_LABEL}",
            counts.live, counts.waiting
        ),
        HubMode::Starting => "MCP Agent Hub — 正在启动 Hub…".to_string(),
        HubMode::Stopping => "MCP Agent Hub — 正在停止 Hub…".to_string(),
        HubMode::Failed => "MCP Agent Hub — Hub 未运行".to_string(),
    }
}

fn shell(app: &AppHandle) -> Option<Arc<HubShell>> {
    app.try_state::<Arc<HubShell>>().map(|s| s.inner().clone())
}

fn build_menu(app: &AppHandle, mode: HubMode) -> tauri::Result<Menu<Wry>> {
    let autostart_on = app.autolaunch().is_enabled().unwrap_or(false);
    let external = mode == HubMode::External;
    let menu = Menu::new(app)?;
    if external {
        menu.append(&MenuItem::with_id(
            app,
            "external",
            EXTERNAL_LABEL,
            false,
            None::<&str>,
        )?)?;
        menu.append(&PredefinedMenuItem::separator(app)?)?;
    }
    menu.append(&MenuItem::with_id(
        app,
        "open",
        "打开总控台",
        true,
        None::<&str>,
    )?)?;
    menu.append(&MenuItem::with_id(
        app,
        "restart",
        "重启 Hub",
        mode == HubMode::Owned,
        None::<&str>,
    )?)?;
    menu.append(&CheckMenuItem::with_id(
        app,
        "autostart",
        "开机启动",
        true,
        autostart_on,
        None::<&str>,
    )?)?;
    menu.append(&MenuItem::with_id(
        app,
        "logs",
        "打开日志目录",
        true,
        None::<&str>,
    )?)?;
    if external {
        menu.append(&MenuItem::with_id(
            app,
            "takeover",
            "接管 Hub（重启为应用托管）",
            true,
            None::<&str>,
        )?)?;
    }
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    menu.append(&MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?)?;
    Ok(menu)
}

pub fn create(app: &AppHandle) -> tauri::Result<()> {
    let menu = build_menu(app, HubMode::Starting)?;
    let mut builder = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip(tooltip_text(HubMode::Starting, &SessionCounts::default()))
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| on_menu(app, event.id().as_ref()))
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                reveal_main_window(tray.app_handle());
            }
        });
    if let Some(icon) =
        build_tray_image(BadgeState::None).or_else(|| app.default_window_icon().cloned())
    {
        builder = builder.icon(icon);
    }
    builder.build(app)?;
    Ok(())
}

/// Rebuild the menu for the current mode and refresh the tooltip + icon badge.
pub fn refresh(app: &AppHandle) {
    let Some(shell) = shell(app) else {
        return;
    };
    let mode = shell.mode();
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        if let Ok(menu) = build_menu(app, mode) {
            let _ = tray.set_menu(Some(menu));
        }
    }
    refresh_tooltip(app);
}

pub fn refresh_tooltip(app: &AppHandle) {
    let Some(shell) = shell(app) else {
        return;
    };
    let mode = shell.mode();
    let counts = shell.counts();
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_tooltip(Some(tooltip_text(mode, &counts)));
        let next_badge = badge_state(mode, &counts);
        let changed = {
            let mut cache = last_badge_cache().lock().unwrap_or_else(|e| e.into_inner());
            if *cache != Some(next_badge) {
                *cache = Some(next_badge);
                true
            } else {
                false
            }
        };
        if changed {
            if let Some(icon) = build_tray_image(next_badge) {
                let _ = tray.set_icon(Some(icon));
            }
        }
    }
}

fn on_menu(app: &AppHandle, id: &str) {
    let Some(shell) = shell(app) else {
        return;
    };
    match id {
        "open" => shell.open_console(app, None),
        "restart" => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move { shell.restart(&app).await });
        }
        "takeover" => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move { shell.takeover(&app).await });
        }
        "quit" => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move { shell.quit(&app).await });
        }
        "autostart" => toggle_autostart(app, &shell),
        "logs" => {
            let dir = shell.log_dir().to_path_buf();
            let _ = std::fs::create_dir_all(&dir);
            if let Err(e) = std::process::Command::new("explorer.exe").arg(&dir).spawn() {
                shell.log(&format!("open log dir failed: {e}"));
            }
        }
        _ => {}
    }
}

fn toggle_autostart(app: &AppHandle, shell: &HubShell) {
    let launcher = app.autolaunch();
    let was_on = launcher.is_enabled().unwrap_or(false);
    let result = if was_on {
        launcher.disable()
    } else {
        launcher.enable()
    };
    match result {
        Ok(()) => shell.log(&format!(
            "autostart {}",
            if was_on { "off" } else { "on (--hidden)" }
        )),
        Err(e) => shell.log(&format!("autostart toggle failed: {e}")),
    }
    refresh(app);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tooltip_shows_counts() {
        let counts = SessionCounts {
            live: 3,
            waiting: 1,
        };
        assert_eq!(
            tooltip_text(HubMode::Owned, &counts),
            "MCP Agent Hub — 3 个会话，1 个等待"
        );
        assert!(tooltip_text(HubMode::External, &counts).ends_with(EXTERNAL_LABEL));
        assert_eq!(
            tooltip_text(HubMode::Failed, &counts),
            "MCP Agent Hub — Hub 未运行"
        );
    }

    #[test]
    fn badge_renders_distinct_icons_for_agent_counts() {
        let base = Image::from_bytes(BASE_ICON_PNG).unwrap();
        assert_eq!((base.width(), base.height()), (32, 32));
        let none = render_badge_rgba(base.rgba(), 32, 32, BadgeState::None);
        let zero = render_badge_rgba(
            base.rgba(),
            32,
            32,
            BadgeState::Active {
                live: 0,
                waiting: false,
            },
        );
        let one = render_badge_rgba(
            base.rgba(),
            32,
            32,
            BadgeState::Active {
                live: 1,
                waiting: false,
            },
        );
        let waiting = render_badge_rgba(
            base.rgba(),
            32,
            32,
            BadgeState::Active {
                live: 1,
                waiting: true,
            },
        );
        let twelve = render_badge_rgba(
            base.rgba(),
            32,
            32,
            BadgeState::Active {
                live: 12,
                waiting: false,
            },
        );
        assert_eq!(none, base.rgba());
        assert_ne!(zero, none);
        assert_ne!(one, zero);
        assert_ne!(waiting, one);
        assert_ne!(twelve, one);
        assert_eq!(
            badge_label(BadgeState::Active {
                live: 120,
                waiting: false
            }),
            Some("99".to_string())
        );
    }
}
