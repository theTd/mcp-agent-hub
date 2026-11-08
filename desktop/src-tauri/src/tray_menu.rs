//! The tray icon: tooltip with session counts, left click shows the window, and the menu
//! 打开总控台 · 重启 Hub · 开机启动 · 打开日志目录 · (external only: 接管 Hub) · 退出.

use std::sync::Arc;

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Wry};
use tauri_plugin_autostart::ManagerExt;

use crate::hub_api::SessionCounts;
use crate::hub_supervisor::{reveal_main_window, HubMode, HubShell};

pub const TRAY_ID: &str = "hub-tray";
pub const EXTERNAL_LABEL: &str = "外部 Hub（不随应用退出）";

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
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

/// Rebuild the menu for the current mode and refresh the tooltip.
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
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_tooltip(Some(tooltip_text(shell.mode(), &shell.counts())));
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
}
