//! Windows toasts through WinRT (`tauri-winrt-notification`), because the notification plugin does
//! not report clicks on desktop. A click runs on the app's main thread.

use std::sync::Arc;

use tauri::{AppHandle, Manager};
use tauri_winrt_notification::{Duration as ToastDuration, Toast};

use crate::hub_supervisor::HubShell;

#[derive(Debug, Clone)]
pub enum ToastClick {
    /// Show and focus the window.
    ShowWindow,
    /// Show the window on the console with `?session=<id>`.
    OpenSession(String),
}

/// The AppUserModelID toasts are sent under. An unpackaged debug build has no registered AUMID, so
/// it borrows PowerShell's (the toast then names PowerShell as its source); the installed app uses
/// its identifier, which the NSIS Start-menu shortcut registers.
fn app_user_model_id(app: &AppHandle) -> String {
    if cfg!(debug_assertions) {
        Toast::POWERSHELL_APP_ID.to_string()
    } else {
        app.config().identifier.clone()
    }
}

pub fn show(app: &AppHandle, title: &str, line1: &str, line2: Option<&str>, click: ToastClick) {
    let app_id = app_user_model_id(app);
    let title = title.to_string();
    let line1 = line1.to_string();
    let line2 = line2.map(str::to_string);
    let on_click_app = app.clone();
    let log_app = app.clone();
    // WinRT calls block briefly; keep them off the async workers and the main thread.
    std::thread::spawn(move || {
        let click_app = on_click_app.clone();
        let mut toast = Toast::new(&app_id)
            .title(&title)
            .text1(&line1)
            .duration(ToastDuration::Long)
            .on_activated(move |_action| {
                let target = click.clone();
                let app = click_app.clone();
                let _ = click_app.run_on_main_thread(move || on_toast_clicked(&app, target));
                Ok(())
            });
        if let Some(l2) = line2.as_deref().filter(|l| !l.is_empty()) {
            toast = toast.text2(l2);
        }
        if let Err(e) = toast.show() {
            if let Some(shell) = log_app.try_state::<Arc<HubShell>>() {
                shell.log(&format!("toast failed: {e}"));
            }
        }
    });
}

fn on_toast_clicked(app: &AppHandle, click: ToastClick) {
    let Some(shell) = app.try_state::<Arc<HubShell>>() else {
        return;
    };
    let shell = shell.inner().clone();
    match click {
        ToastClick::ShowWindow => {
            shell.log("toast clicked: show window");
            crate::hub_supervisor::reveal_main_window(app);
        }
        ToastClick::OpenSession(session_id) => {
            shell.log(&format!("toast clicked: open session {session_id}"));
            shell.open_console(app, Some(&session_id));
        }
    }
}
