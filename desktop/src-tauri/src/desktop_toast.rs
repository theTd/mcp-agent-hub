//! Windows toasts using the app's own AppUserModelID (`local.mcp-agent-hub.desktop`), display name
//! (`MCP Agent Hub`), and icon (`toast-icon.png`). A single persistent WinRT worker thread creates
//! and shows `ToastNotification` instances and keeps them alive in a bounded ring buffer so
//! `ToastNotification::Activated` stays connected for the lifetime of the notification.

use std::collections::VecDeque;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager};
use windows::core::{h, IInspectable, Interface, HSTRING};
use windows::Data::Xml::Dom::XmlDocument;
use windows::Foundation::TypedEventHandler;
use windows::UI::Notifications::{
    ToastActivatedEventArgs, ToastNotification, ToastNotificationManager,
};
use windows_sys::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};
use windows_sys::Win32::System::Registry::{
    RegCloseKey, RegCreateKeyExW, RegDeleteTreeW, RegDeleteValueW, RegSetValueExW, HKEY,
    HKEY_CURRENT_USER, KEY_SET_VALUE, KEY_WRITE, REG_OPTION_NON_VOLATILE, REG_SZ,
};
use windows_sys::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID;

use crate::hub_config;
use crate::hub_supervisor::HubShell;

pub const APP_USER_MODEL_ID: &str = "local.mcp-agent-hub.desktop";
pub const APP_DISPLAY_NAME: &str = "MCP Agent Hub";
const LEGACY_ACTIVATOR_CLSID_KEY: &str =
    r"Software\Classes\CLSID\{D7A3B210-6C94-4A89-A2E1-95D4F18B4444}";

const ICON_PNG_BYTES: &[u8] = include_bytes!("../icons/128x128.png");
const LIVE_TOAST_CAPACITY: usize = 64;
const CLICK_DEDUP_WINDOW: Duration = Duration::from_millis(500);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToastClick {
    /// Show and focus the main window.
    ShowWindow,
    /// Show and focus the main window on `?session=<id>`.
    OpenSession(String),
}

impl ToastClick {
    pub fn to_launch_arg(&self) -> String {
        match self {
            ToastClick::ShowWindow => "show".to_string(),
            ToastClick::OpenSession(id) => format!("session={id}"),
        }
    }

    pub fn from_launch_arg(arg: &str) -> Option<Self> {
        let trimmed = arg.trim();
        if trimmed == "show" || trimmed == "mcp-agent-hub://show" {
            return Some(ToastClick::ShowWindow);
        }
        if let Some(id) = trimmed
            .strip_prefix("session=")
            .or_else(|| trimmed.strip_prefix("mcp-agent-hub://open?session="))
            .or_else(|| trimmed.strip_prefix("--open-session="))
        {
            let id = id.trim();
            if !id.is_empty() {
                return Some(ToastClick::OpenSession(id.to_string()));
            }
        }
        None
    }
}

struct ToastJob {
    app: AppHandle,
    title: String,
    line1: String,
    line2: Option<String>,
    click: ToastClick,
    icon_path: Option<PathBuf>,
}

static ICON_PATH: OnceLock<PathBuf> = OnceLock::new();
static TOAST_WORKER: OnceLock<Sender<ToastJob>> = OnceLock::new();

fn last_click() -> &'static Mutex<Option<(ToastClick, Instant)>> {
    static LAST: OnceLock<Mutex<Option<(ToastClick, Instant)>>> = OnceLock::new();
    LAST.get_or_init(|| Mutex::new(None))
}

fn wide_null(s: &str) -> Vec<u16> {
    std::ffi::OsStr::new(s)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

fn reg_set_sz(subkey: &str, value_name: Option<&str>, data: &str) -> Result<(), String> {
    let subkey_w = wide_null(subkey);
    let name_w = value_name.map(wide_null);
    let data_w = wide_null(data);
    let mut hkey: HKEY = std::ptr::null_mut();
    // SAFETY: valid wide strings and output HKEY pointer.
    let rc = unsafe {
        RegCreateKeyExW(
            HKEY_CURRENT_USER,
            subkey_w.as_ptr(),
            0,
            std::ptr::null(),
            REG_OPTION_NON_VOLATILE,
            KEY_WRITE,
            std::ptr::null(),
            &mut hkey,
            std::ptr::null_mut(),
        )
    };
    if rc != 0 {
        return Err(format!("RegCreateKeyExW({subkey}) failed: {rc}"));
    }
    let name_ptr = name_w
        .as_ref()
        .map(|v| v.as_ptr())
        .unwrap_or(std::ptr::null());
    // SAFETY: `hkey` is open for KEY_WRITE and `data_w` is NUL-terminated UTF-16.
    let set_rc = unsafe {
        RegSetValueExW(
            hkey,
            name_ptr,
            0,
            REG_SZ,
            data_w.as_ptr() as *const u8,
            (data_w.len() * 2) as u32,
        )
    };
    // SAFETY: closing the key opened above.
    unsafe {
        RegCloseKey(hkey);
    }
    if set_rc != 0 {
        return Err(format!("RegSetValueExW({subkey}) failed: {set_rc}"));
    }
    Ok(())
}

fn reg_delete_value(subkey: &str, value_name: &str) {
    let subkey_w = wide_null(subkey);
    let name_w = wide_null(value_name);
    let mut hkey: HKEY = std::ptr::null_mut();
    // SAFETY: opening existing HKCU subkey to delete a value if present.
    unsafe {
        if RegCreateKeyExW(
            HKEY_CURRENT_USER,
            subkey_w.as_ptr(),
            0,
            std::ptr::null(),
            REG_OPTION_NON_VOLATILE,
            KEY_SET_VALUE,
            std::ptr::null(),
            &mut hkey,
            std::ptr::null_mut(),
        ) == 0
        {
            let _ = RegDeleteValueW(hkey, name_w.as_ptr());
            RegCloseKey(hkey);
        }
    }
}

fn reg_delete_tree(subkey: &str) {
    let subkey_w = wide_null(subkey);
    // SAFETY: deleting our own legacy HKCU subkey if present.
    unsafe {
        let _ = RegDeleteTreeW(HKEY_CURRENT_USER, subkey_w.as_ptr());
    }
}

fn ensure_toast_icon_file() -> Option<PathBuf> {
    let dir = hub_config::log_dir()
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(hub_config::log_dir);
    let _ = std::fs::create_dir_all(&dir);
    let icon_path = dir.join("toast-icon.png");
    let needs_write = match std::fs::metadata(&icon_path) {
        Ok(m) => m.len() != ICON_PNG_BYTES.len() as u64,
        Err(_) => true,
    };
    if needs_write && std::fs::write(&icon_path, ICON_PNG_BYTES).is_err() {
        return None;
    }
    Some(icon_path)
}

/// Register `DisplayName` and `IconUri` under `HKCU\Software\Classes\AppUserModelId\<AUMID>`
/// without `CustomActivator` (a `CustomActivator` without a registered proxy/stub DLL forces
/// Windows to bypass in-process `ToastNotification::Activated`).
fn register_aumid_in_hkcu(icon_path: Option<&Path>) -> Result<(), String> {
    let aumid_key = format!(r"Software\Classes\AppUserModelId\{APP_USER_MODEL_ID}");
    reg_delete_value(&aumid_key, "CustomActivator");
    reg_delete_tree(LEGACY_ACTIVATOR_CLSID_KEY);
    reg_set_sz(&aumid_key, Some("DisplayName"), APP_DISPLAY_NAME)?;
    reg_set_sz(&aumid_key, Some("IconBackgroundColor"), "00000000")?;
    if let Some(icon) = icon_path {
        reg_set_sz(&aumid_key, Some("IconUri"), &icon.display().to_string())?;
    }
    Ok(())
}

fn ensure_worker() -> &'static Sender<ToastJob> {
    TOAST_WORKER.get_or_init(|| {
        let (tx, rx) = mpsc::channel::<ToastJob>();
        std::thread::Builder::new()
            .name("hub-toast-worker".into())
            .spawn(move || {
                // SAFETY: initialize MTA on this persistent worker thread so WinRT objects and
                // their Activated event handlers stay alive in a live apartment.
                unsafe {
                    let _ = CoInitializeEx(std::ptr::null(), COINIT_MULTITHREADED as u32);
                }
                let mut pool: VecDeque<ToastNotification> = VecDeque::new();
                while let Ok(job) = rx.recv() {
                    let fallback_click = job.click.clone();
                    let click_app = job.app.clone();
                    let log_app = job.app.clone();
                    let res: windows::core::Result<()> = (|| {
                        let notification = create_toast_notification(
                            &job.title,
                            &job.line1,
                            job.line2.as_deref(),
                            &job.click,
                            job.icon_path.as_deref(),
                        )?;
                        let handler = TypedEventHandler::<ToastNotification, IInspectable>::new(
                            move |_sender, insp| {
                                unsafe {
                                    let _ = windows_sys::Win32::UI::WindowsAndMessaging::AllowSetForegroundWindow(
                                        windows_sys::Win32::UI::WindowsAndMessaging::ASFW_ANY,
                                    );
                                }
                                let parsed = insp
                                    .as_ref()
                                    .and_then(|i| i.cast::<ToastActivatedEventArgs>().ok())
                                    .and_then(|a| a.Arguments().ok())
                                    .and_then(|s| ToastClick::from_launch_arg(&s.to_string()));
                                let target = parsed.unwrap_or_else(|| fallback_click.clone());
                                dispatch_click(&click_app, target, "winrt_activated");
                                Ok(())
                            },
                        );
                        notification.Activated(&handler)?;
                        let notifier = ToastNotificationManager::CreateToastNotifierWithId(
                            &HSTRING::from(APP_USER_MODEL_ID),
                        )?;
                        notifier.Show(&notification)?;
                        if pool.len() >= LIVE_TOAST_CAPACITY {
                            pool.pop_front();
                        }
                        pool.push_back(notification);
                        Ok(())
                    })();
                    if let Err(e) = res {
                        if let Some(shell) = log_app.try_state::<Arc<HubShell>>() {
                            shell.log(&format!("toast failed: {e}"));
                        }
                    }
                }
            })
            .expect("failed to spawn hub-toast-worker");
        tx
    })
}

pub fn init(app: &AppHandle) {
    let aumid_w = wide_null(APP_USER_MODEL_ID);
    // SAFETY: valid NUL-terminated UTF-16 AUMID.
    unsafe {
        let _ = SetCurrentProcessExplicitAppUserModelID(aumid_w.as_ptr());
    }
    let icon_path = ensure_toast_icon_file();
    if let Some(ref p) = icon_path {
        let _ = ICON_PATH.set(p.clone());
    }
    if let Err(e) = register_aumid_in_hkcu(icon_path.as_deref()) {
        if let Some(shell) = app.try_state::<Arc<HubShell>>() {
            shell.log(&format!("toast registry init warning: {e}"));
        }
    }
    let _ = ensure_worker();
}

fn create_toast_notification(
    title: &str,
    line1: &str,
    line2: Option<&str>,
    click: &ToastClick,
    icon_path: Option<&Path>,
) -> windows::core::Result<ToastNotification> {
    let xml_doc = XmlDocument::new()?;
    let launch_arg = HSTRING::from(click.to_launch_arg());

    let xml_toast = xml_doc.CreateElement(h!("toast"))?;
    xml_toast.SetAttribute(h!("duration"), h!("long"))?;
    xml_toast.SetAttribute(h!("launch"), &launch_arg)?;

    let xml_visual = xml_doc.CreateElement(h!("visual"))?;
    let xml_binding = xml_doc.CreateElement(h!("binding"))?;
    xml_binding.SetAttribute(h!("template"), h!("ToastGeneric"))?;

    if let Some(icon) = icon_path {
        let xml_img = xml_doc.CreateElement(h!("image"))?;
        xml_img.SetAttribute(h!("id"), h!("1"))?;
        let uri = format!("file:///{}", icon.display().to_string().replace('\\', "/"));
        xml_img.SetAttribute(h!("src"), &HSTRING::from(uri))?;
        xml_img.SetAttribute(h!("alt"), h!("MCP Agent Hub"))?;
        xml_img.SetAttribute(h!("placement"), h!("appLogoOverride"))?;
        xml_binding.AppendChild(&xml_img)?;
    }

    for (idx, text) in [Some(title), Some(line1), line2.filter(|s| !s.is_empty())]
        .into_iter()
        .flatten()
        .enumerate()
    {
        let xml_text = xml_doc.CreateElement(h!("text"))?;
        xml_text.SetAttribute(h!("id"), &HSTRING::from((idx + 1).to_string()))?;
        xml_text.SetInnerText(&HSTRING::from(text))?;
        xml_binding.AppendChild(&xml_text)?;
    }

    xml_visual.AppendChild(&xml_binding)?;
    xml_toast.AppendChild(&xml_visual)?;

    if matches!(click, ToastClick::OpenSession(_)) {
        let xml_actions = xml_doc.CreateElement(h!("actions"))?;
        let xml_action = xml_doc.CreateElement(h!("action"))?;
        xml_action.SetAttribute(h!("content"), h!("查看并处理"))?;
        xml_action.SetAttribute(h!("arguments"), &launch_arg)?;
        xml_actions.AppendChild(&xml_action)?;
        xml_toast.AppendChild(&xml_actions)?;
    }

    xml_doc.AppendChild(&xml_toast)?;
    ToastNotification::CreateToastNotification(&xml_doc)
}

pub fn show(app: &AppHandle, title: &str, line1: &str, line2: Option<&str>, click: ToastClick) {
    let icon_path = ICON_PATH.get().cloned().or_else(ensure_toast_icon_file);
    let _ = ensure_worker().send(ToastJob {
        app: app.clone(),
        title: title.to_string(),
        line1: line1.to_string(),
        line2: line2.map(str::to_string),
        click,
        icon_path,
    });
}

pub fn dispatch_click(app: &AppHandle, click: ToastClick, source: &'static str) {
    {
        let mut guard = last_click().lock().unwrap_or_else(|e| e.into_inner());
        if let Some((prev, when)) = guard.as_ref() {
            if *prev == click && when.elapsed() < CLICK_DEDUP_WINDOW {
                return;
            }
        }
        *guard = Some((click.clone(), Instant::now()));
    }
    let app_clone = app.clone();
    let _ = app.run_on_main_thread(move || on_toast_clicked(&app_clone, click, source));
}

fn on_toast_clicked(app: &AppHandle, click: ToastClick, source: &'static str) {
    let Some(shell) = app.try_state::<Arc<HubShell>>() else {
        return;
    };
    let shell = shell.inner().clone();
    match click {
        ToastClick::ShowWindow => {
            shell.log(&format!("toast clicked ({source}): show window"));
            crate::hub_supervisor::reveal_main_window(app);
        }
        ToastClick::OpenSession(session_id) => {
            shell.log(&format!(
                "toast clicked ({source}): open session {session_id}"
            ));
            shell.open_console(app, Some(&session_id));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn launch_arg_roundtrip() {
        let show = ToastClick::ShowWindow;
        assert_eq!(
            ToastClick::from_launch_arg(&show.to_launch_arg()),
            Some(show)
        );
        let open = ToastClick::OpenSession("claude-1a2b3c4d".to_string());
        assert_eq!(
            ToastClick::from_launch_arg(&open.to_launch_arg()),
            Some(open.clone())
        );
        assert_eq!(
            ToastClick::from_launch_arg("mcp-agent-hub://open?session=claude-1a2b3c4d"),
            Some(open)
        );
        assert_eq!(ToastClick::from_launch_arg(""), None);
    }

    #[test]
    fn toast_xml_contains_launch_and_icon() {
        let click = ToastClick::OpenSession("pwsh-99887766".to_string());
        let icon = PathBuf::from(r"C:\Temp\toast-icon.png");
        let n = create_toast_notification(
            "子 Agent 在提问",
            "pwsh · 99887766",
            Some("Which branch?"),
            &click,
            Some(&icon),
        )
        .unwrap();
        let xml = n.Content().unwrap().GetXml().unwrap().to_string();
        assert!(xml.contains(r#"launch="session=pwsh-99887766""#), "{xml}");
        assert!(xml.contains("appLogoOverride"), "{xml}");
        assert!(xml.contains("查看并处理"), "{xml}");
    }
}
