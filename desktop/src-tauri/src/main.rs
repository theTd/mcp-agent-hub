//! MCP Agent Hub desktop shell: a window on the hub's own console, a tray, one instance, toasts for
//! sessions that wait, and a start-at-login switch. The app owns the hub process (see
//! hub_supervisor); all native behaviour lives here, none in the console (`static/`).

// Release builds have no console window.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod desktop_toast;
mod hub_api;
mod hub_config;
mod hub_log;
mod hub_supervisor;
mod tray_menu;
mod waiting_notifier;
mod windows_process;

use std::sync::Arc;

use tauri::{AppHandle, Manager, RunEvent, State, WindowEvent};

use crate::desktop_toast::ToastClick;
use crate::hub_supervisor::{reveal_main_window, HubShell, SplashView};

/// The splash page's state (starting / error with the hub.log tail). Local splash page only.
#[tauri::command]
fn splash_state(shell: State<'_, Arc<HubShell>>) -> SplashView {
    shell.splash_view()
}

/// The error view's 重试 button. Local splash page only.
#[tauri::command]
fn retry_hub(app: AppHandle, shell: State<'_, Arc<HubShell>>) {
    shell.retry(&app);
}

fn main() {
    let start_hidden = std::env::args().skip(1).any(|a| a == "--hidden");
    let log_dir = hub_config::log_dir();
    hub_log::rotate_desktop_log(&log_dir);
    let shell = HubShell::new(log_dir);
    shell.log(&format!(
        "app start: pid {}, hidden={start_hidden}",
        std::process::id()
    ));

    let setup_shell = shell.clone();
    let window_shell = shell.clone();
    let instance_shell = shell.clone();
    let app = tauri::Builder::default()
        // Must be first: a second launch hands over to this one and exits.
        .plugin(tauri_plugin_single_instance::init(
            move |app, argv, _cwd| {
                instance_shell.log(&format!("second launch: {argv:?}"));
                if !argv.iter().skip(1).any(|a| a == "--hidden") {
                    reveal_main_window(app);
                }
            },
        ))
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .args(["--hidden"])
                .build(),
        )
        .plugin(tauri_plugin_dialog::init())
        .manage(shell.clone())
        .invoke_handler(tauri::generate_handler![splash_state, retry_hub])
        .setup(move |app| {
            let handle = app.handle().clone();
            if let Some(window) = app.get_webview_window("main") {
                if let Ok(url) = window.url() {
                    setup_shell.set_splash_url(url);
                }
                if !start_hidden {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
            tray_menu::create(&handle)?;
            let s = setup_shell.clone();
            let h = handle.clone();
            tauri::async_runtime::spawn(async move { s.start(&h).await });
            tauri::async_runtime::spawn(waiting_notifier::run(handle.clone(), setup_shell.clone()));
            tauri::async_runtime::spawn(setup_shell.clone().watch(handle));
            Ok(())
        })
        .on_window_event(move |window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    // Closing the window is not quitting: the hub keeps running (tray 退出 quits).
                    api.prevent_close();
                    let _ = window.hide();
                    if window_shell.first_close_hint() {
                        desktop_toast::show(
                            window.app_handle(),
                            "MCP Agent Hub",
                            "已最小化到托盘，Hub 仍在运行",
                            None,
                            ToastClick::ShowWindow,
                        );
                    }
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build the MCP Agent Hub app");

    app.run(move |_app, event| match event {
        RunEvent::ExitRequested {
            code: None, api, ..
        } => api.prevent_exit(),
        RunEvent::Exit => shell.terminate_on_exit(),
        _ => {}
    });
}
