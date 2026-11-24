//! Portable EXE self-replacement: `--print-build-info` and `--apply-update <plan>`.
//!
//! 重启并升级 stops the hub gracefully (sessions are archived), stages the chosen EXE next to the
//! running one, copies this program into `%TEMP%`, and asks Windows to start it detached with
//! `--apply-update "<plan>"`; the shell then exits and releases the EXE lock. The helper waits for
//! the old app and hub to exit — verified by PID *and* creation time, never a bare PID — swaps the
//! staged file over the target with `ReplaceFileW` (keeping a `.bak`), re-hashes the target,
//! relaunches the app through `explorer.exe`, and waits for the new hub to answer.
//!
//! Anything that fails before the new hub is up restores the backup and relaunches the old EXE.
//! A hub that answers but reports failed session resumes is *not* rolled back: the sessions were
//! archived by the graceful stop, and the user asked for the new version.
//!
//! `RestartPlan` lives here because both sides of the hand-off use it: the shell writes it before
//! stopping the hub, the helper reads it after the shell exited.

use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::windows_process::{self, ProcessIdentity};

/// The plan version this build writes and accepts.
pub const PLAN_VERSION: u32 = 1;
/// How long a restart plan stays valid. The worst case (30 s waiting for the old app, 30 s of
/// replace retries, 300 s of hub bootstrap, 45 s of resume polling) is well inside it, and the
/// Python CLI uses the same 600 s window.
pub const RESTART_PLAN_TTL_SECS: f64 = 600.0;
const OLD_PROCESS_WAIT: Duration = Duration::from_secs(30);
const REPLACE_RETRY_WINDOW: Duration = Duration::from_secs(30);
const HUB_READY_WAIT: Duration = Duration::from_secs(120);
/// A live new app that has not answered yet may still be building its first venv.
const HUB_READY_WAIT_WHILE_BOOTSTRAPPING: Duration = Duration::from_secs(300);
/// `progress.json` written this recently proves the bootstrap is still moving.
const PROGRESS_FRESH_WINDOW: Duration = Duration::from_secs(60);
const RESTART_STATUS_WAIT: Duration = Duration::from_secs(45);
const HTTP_TIMEOUT: Duration = Duration::from_millis(2500);
const CMD_POLL: Duration = Duration::from_millis(500);
const HELPER_STALE_AGE: Duration = Duration::from_secs(60 * 60);
const HELPER_PREFIX: &str = "hub-update-helper-";
#[cfg(feature = "portable")]
const STAGED_PREFIX: &str = ".hub-update-";

/// 16 hex characters from the system RNG: names the staged EXE, the temp helper and the plan, so
/// two updates in the same session never collide.
#[cfg(feature = "portable")]
pub fn random_nonce() -> Result<String, String> {
    let token = windows_process::random_token().map_err(|e| e.to_string())?;
    Ok(token.chars().take(16).collect())
}

/// `<target dir>\.hub-update-<nonce>.exe` — same volume and directory as the EXE it will replace.
#[cfg(feature = "portable")]
pub fn staged_exe_path(target: &Path, nonce: &str) -> PathBuf {
    target.with_file_name(format!("{STAGED_PREFIX}{nonce}.exe"))
}

/// `%TEMP%\hub-update-helper-<nonce>.exe` — a copy of this program, outside the exiting app's tree.
#[cfg(feature = "portable")]
pub fn helper_exe_path(nonce: &str) -> PathBuf {
    std::env::temp_dir().join(format!("{HELPER_PREFIX}{nonce}.exe"))
}

/// `<target>.bak` next to the target, so `ReplaceFileW` can rename the old bytes beside it.
#[cfg(feature = "portable")]
pub fn backup_exe_path(target: &Path) -> PathBuf {
    let name = target
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "app.exe".to_string());
    target.with_file_name(format!("{name}.bak"))
}

/// What the shell asks the helper (or itself) to do, and what both sides must agree on before
/// touching an EXE.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RestartPlan {
    pub version: u32,
    pub restart_id: String,
    /// `"hub"` (in-place graceful restart) or `"replace-exe"` (swap the executable).
    pub mode: String,
    pub requested_at: f64,
    pub expires_at: f64,
    #[serde(default)]
    pub port: u16,
    #[serde(default)]
    pub app_pid: u32,
    #[serde(default)]
    pub app_creation_time: u64,
    #[serde(default)]
    pub hub_pid: u32,
    #[serde(default)]
    pub hub_creation_time: u64,
    #[serde(default)]
    pub data_dir: Option<PathBuf>,
    #[serde(default)]
    pub target_exe: Option<PathBuf>,
    #[serde(default)]
    pub old_exe_sha256: Option<String>,
    #[serde(default)]
    pub new_exe: Option<PathBuf>,
    #[serde(default)]
    pub new_exe_sha256: Option<String>,
    #[serde(default)]
    pub staged_exe: Option<PathBuf>,
    #[serde(default)]
    pub backup_exe: Option<PathBuf>,
}

impl RestartPlan {
    /// A plan at or past its deadline, or one with no deadline at all, must never move an EXE.
    pub fn is_expired(&self, now: f64) -> bool {
        self.expires_at <= 0.0 || now >= self.expires_at
    }

    /// The exact desktop-app instance this plan belongs to, when the plan names one.
    pub fn app_identity(&self) -> Option<ProcessIdentity> {
        if self.app_pid == 0 || self.app_creation_time == 0 {
            return None;
        }
        Some(ProcessIdentity {
            pid: self.app_pid,
            creation_time: self.app_creation_time,
        })
    }

    pub fn hub_identity(&self) -> Option<ProcessIdentity> {
        if self.hub_pid == 0 || self.hub_creation_time == 0 {
            return None;
        }
        Some(ProcessIdentity {
            pid: self.hub_pid,
            creation_time: self.hub_creation_time,
        })
    }
}

pub fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `restart_plan.json`: `<portable_root>/restart_plan.json`. An installed (non-portable) build has
/// no portable root, so its development runs keep the file next to the logs.
pub fn restart_plan_path() -> PathBuf {
    #[cfg(feature = "portable")]
    {
        crate::hub_config::portable_root().join("restart_plan.json")
    }
    #[cfg(not(feature = "portable"))]
    {
        crate::hub_config::log_dir().join("restart_plan.json")
    }
}

/// `<portable_root>/restart_plan.result.json` — always a sibling of the plan.
pub fn restart_result_path(plan_path: &Path) -> PathBuf {
    plan_path.with_file_name("restart_plan.result.json")
}

/// `None` for a missing, unreadable or malformed plan: a broken plan must only ever be ignored.
pub fn read_restart_plan(path: &Path) -> Option<RestartPlan> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(text.trim_start_matches('\u{feff}')).ok()
}

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn temp_sibling(path: &Path, tag: &str) -> PathBuf {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "restart_plan.json".to_string());
    let seq = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    path.with_file_name(format!(".{name}.{tag}-{}-{seq}", std::process::id()))
}

fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).map_err(|e| format!("无法创建 {}：{e}", parent.display()))?;
    let temp = temp_sibling(path, "tmp");
    let written = (|| -> std::io::Result<()> {
        let mut file = fs::File::create(&temp)?;
        file.write_all(bytes)?;
        file.sync_all()
    })();
    if let Err(error) = written {
        let _ = fs::remove_file(&temp);
        return Err(format!("无法写入 {}：{error}", temp.display()));
    }
    if let Err(error) = fs::rename(&temp, path) {
        let _ = fs::remove_file(&temp);
        return Err(format!("无法替换 {}：{error}", path.display()));
    }
    Ok(())
}

/// Written before the hub is asked to stop, so a second window or a restarted shell can tell a
/// planned stop from a crash. Atomic: a reader sees the previous plan or the whole new one.
pub fn write_restart_plan(path: &Path, plan: &RestartPlan) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(plan)
        .map_err(|e| format!("restart_plan.json 无法序列化：{e}"))?;
    write_atomic(path, &bytes)
}

pub fn remove_restart_plan(path: &Path) {
    let _ = fs::remove_file(path);
}

pub fn write_restart_result(plan_path: &Path, result: &Value) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(result)
        .map_err(|e| format!("restart_plan.result.json 无法序列化：{e}"))?;
    write_atomic(&restart_result_path(plan_path), &bytes)
}

// ============================================================================
// SHA-256 (pure Rust: the crate's `sha2` is a build-dependency only, and this
// module must not add an external runtime dependency)
// ============================================================================

const SHA256_K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

struct Sha256 {
    state: [u32; 8],
    block: [u8; 64],
    block_len: usize,
    total: u64,
}

impl Sha256 {
    fn new() -> Self {
        Sha256 {
            state: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
                0x5be0cd19,
            ],
            block: [0u8; 64],
            block_len: 0,
            total: 0,
        }
    }

    fn compress(&mut self, chunk: &[u8]) {
        let mut w = [0u32; 64];
        for (i, word) in w.iter_mut().take(16).enumerate() {
            let base = i * 4;
            *word = u32::from_be_bytes([chunk[base], chunk[base + 1], chunk[base + 2], chunk[base + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = self.state;
        for (i, word) in w.iter().enumerate() {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = h
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(SHA256_K[i])
                .wrapping_add(*word);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        self.state[0] = self.state[0].wrapping_add(a);
        self.state[1] = self.state[1].wrapping_add(b);
        self.state[2] = self.state[2].wrapping_add(c);
        self.state[3] = self.state[3].wrapping_add(d);
        self.state[4] = self.state[4].wrapping_add(e);
        self.state[5] = self.state[5].wrapping_add(f);
        self.state[6] = self.state[6].wrapping_add(g);
        self.state[7] = self.state[7].wrapping_add(h);
    }

    fn update(&mut self, mut data: &[u8]) {
        self.total = self.total.wrapping_add(data.len() as u64);
        if self.block_len > 0 {
            let need = 64 - self.block_len;
            let take = need.min(data.len());
            self.block[self.block_len..self.block_len + take].copy_from_slice(&data[..take]);
            self.block_len += take;
            data = &data[take..];
            if self.block_len == 64 {
                let chunk = self.block;
                self.compress(&chunk);
                self.block_len = 0;
            }
        }
        while data.len() >= 64 {
            let (chunk, rest) = data.split_at(64);
            self.compress(chunk);
            data = rest;
        }
        if !data.is_empty() {
            self.block[..data.len()].copy_from_slice(data);
            self.block_len = data.len();
        }
    }

    fn finish(mut self) -> [u8; 32] {
        let bit_len = self.total.wrapping_mul(8);
        let mut tail = Vec::with_capacity(72);
        tail.push(0x80u8);
        let padded = (self.block_len + 1) % 64;
        let zeros = if padded <= 56 { 56 - padded } else { 120 - padded };
        tail.extend(std::iter::repeat(0u8).take(zeros));
        tail.extend_from_slice(&bit_len.to_be_bytes());
        // `total` already counts the padding bytes we are about to feed; keep finish() self-contained
        // by hashing the tail through a temporary copy of the counters.
        let saved_total = self.total;
        self.total = 0;
        self.update(&tail);
        self.total = saved_total;
        let mut out = [0u8; 32];
        for (i, word) in self.state.iter().enumerate() {
            out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
        }
        out
    }
}

#[cfg(test)]
pub fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hasher.finish().iter().map(|b| format!("{b:02x}")).collect()
}

pub fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file =
        fs::File::open(path).map_err(|e| format!("无法读取 {}：{e}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| format!("无法读取 {}：{e}", path.display()))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher.finish().iter().map(|b| format!("{b:02x}")).collect())
}

fn payload_id() -> Option<&'static str> {
    #[cfg(feature = "portable")]
    {
        Some(crate::portable_runtime::PAYLOAD_ID)
    }
    #[cfg(not(feature = "portable"))]
    {
        None
    }
}

/// One line of JSON describing this EXE: what a packaging script compares against the file it
/// just built. Deliberately ignores the data directory and single-instance plugin.
pub fn build_info_json() -> Result<String, String> {
    let exe = std::env::current_exe().map_err(|e| format!("无法定位当前程序：{e}"))?;
    let sha = sha256_file(&exe)?;
    Ok(json!({
        "version": env!("CARGO_PKG_VERSION"),
        "payload_id": payload_id(),
        "exe_sha256": sha,
        "portable": cfg!(feature = "portable"),
    })
    .to_string())
}

/// Writes the build info to stdout and returns the process exit code.
pub fn print_build_info() -> i32 {
    match build_info_json() {
        Ok(text) => {
            let mut out = std::io::stdout();
            let _ = writeln!(out, "{text}");
            let _ = out.flush();
            0
        }
        Err(error) => {
            let _ = writeln!(std::io::stderr(), "{error}");
            1
        }
    }
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

fn wide_path(path: &Path) -> Vec<u16> {
    wide(&path.to_string_lossy())
}

fn absolute(path: &Path) -> PathBuf {
    path.is_absolute()
        .then(|| path.to_path_buf())
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_default().join(path))
}

/// `ReplaceFileW(target, staged, backup, WRITE_THROUGH)`: one same-volume, durable swap that keeps
/// the previous bytes as `backup`.
pub fn replace_file_atomic(target: &Path, staged: &Path, backup: &Path) -> Result<(), String> {
    replace_file_atomic_inner(target, staged, backup, true)
}

/// `clear_backup` is true only for the very first attempt of a swap: `ReplaceFileW` needs the
/// backup name to be free, but a retry must never delete a `.bak` that a partially failed attempt
/// already filled with the previous bytes — that file is the only way back.
fn replace_file_atomic_inner(
    target: &Path,
    staged: &Path,
    backup: &Path,
    clear_backup: bool,
) -> Result<(), String> {
    use windows_sys::Win32::Storage::FileSystem::{ReplaceFileW, REPLACEFILE_WRITE_THROUGH};

    let target = absolute(target);
    let staged = absolute(staged);
    let backup = absolute(backup);
    for path in [&target, &staged] {
        if !path.exists() {
            return Err(format!("{} 不存在", path.display()));
        }
    }
    if clear_backup {
        // ReplaceFile requires the backup name to be free.
        let _ = fs::remove_file(&backup);
    }
    let target_w = wide_path(&target);
    let staged_w = wide_path(&staged);
    let backup_w = wide_path(&backup);
    // SAFETY: three valid NUL-terminated UTF-16 path buffers stay alive for the call; REPLACEFILE
    // needs no exclude/reserved handles.
    let ok = unsafe {
        ReplaceFileW(
            target_w.as_ptr(),
            staged_w.as_ptr(),
            backup_w.as_ptr(),
            REPLACEFILE_WRITE_THROUGH,
            std::ptr::null(),
            std::ptr::null(),
        )
    };
    if ok == 0 {
        let error = std::io::Error::last_os_error();
        let code = error.raw_os_error().unwrap_or(0);
        return Err(format!(
            "无法替换 {}：{error}（错误码 {code}）",
            target.display()
        ));
    }
    Ok(())
}

/// Same swap, but keeps retrying transient sharing/access errors for up to 30 s.
pub fn replace_file_atomic_with_retry(
    target: &Path,
    staged: &Path,
    backup: &Path,
) -> Result<(), String> {
    let deadline = Instant::now() + REPLACE_RETRY_WINDOW;
    let mut attempt = 0u32;
    loop {
        // Only the first attempt may clear a leftover `.bak`; every retry keeps whatever the last
        // attempt wrote there.
        let result = if attempt == 0 {
            replace_file_atomic(target, staged, backup)
        } else {
            replace_file_atomic_inner(target, staged, backup, false)
        };
        match result {
            Ok(()) => return Ok(()),
            Err(message) => {
                attempt += 1;
                let transient = [
                    32,   // ERROR_SHARING_VIOLATION
                    5,    // ERROR_ACCESS_DENIED
                    33,   // ERROR_LOCK_VIOLATION
                    19,   // ERROR_WRITE_PROTECT
                    21,   // ERROR_NOT_READY
                    1175, // ERROR_UNABLE_TO_REMOVE_REPLACED
                    1176, // ERROR_UNABLE_TO_MOVE_REPLACEMENT
                    1177, // ERROR_UNABLE_TO_MOVE_REPLACEMENT_2
                ]
                .iter()
                .any(|code| message.contains(&format!("（错误码 {code}）")));
                if !transient || Instant::now() >= deadline {
                    return Err(message);
                }
                // Retry only while both files are demonstrably still there: when `ReplaceFileW`
                // already moved one of them it partially swapped, and another attempt would
                // destroy the `.bak` holding the previous bytes.
                if !target.exists() || !staged.exists() {
                    return Err(format!(
                        "{message}（目标或暂存文件已不在，停止重试以保留 {}）",
                        backup.display()
                    ));
                }
                std::thread::sleep(Duration::from_millis(400));
            }
        }
    }
}

/// Puts the previous bytes back: `ReplaceFileW(target, backup, NULL)` consumes the backup, and a
/// plain copy covers the case where the target itself is gone.
pub fn rollback_from_backup(target: &Path, backup: &Path) -> Result<(), String> {
    use windows_sys::Win32::Storage::FileSystem::{ReplaceFileW, REPLACEFILE_WRITE_THROUGH};

    let target = absolute(target);
    let backup = absolute(backup);
    if !backup.exists() {
        return Err(format!("没有可回滚的备份：{}", backup.display()));
    }
    let target_w = wide_path(&target);
    let backup_w = wide_path(&backup);
    // SAFETY: valid NUL-terminated buffers for the duration of the call; no backup keeps the target
    // name (the backup is renamed onto it).
    let ok = unsafe {
        ReplaceFileW(
            target_w.as_ptr(),
            backup_w.as_ptr(),
            std::ptr::null(),
            REPLACEFILE_WRITE_THROUGH,
            std::ptr::null(),
            std::ptr::null(),
        )
    };
    if ok != 0 {
        return Ok(());
    }
    let replace_error = std::io::Error::last_os_error();
    fs::copy(&backup, &target).map_err(|e| {
        format!(
            "回滚失败：ReplaceFileW 报 {replace_error}，复制 {} 到 {} 也失败：{e}",
            backup.display(),
            target.display()
        )
    })?;
    let _ = fs::remove_file(&backup);
    Ok(())
}

#[cfg(feature = "portable")]
fn com_apartment() {
    use windows_sys::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED};
    // SAFETY: apartment-threaded COM init; a failure (already initialised, or a different mode)
    // leaves ShellExecuteW working as before.
    unsafe {
        let _ = CoInitializeEx(std::ptr::null(), COINIT_APARTMENTTHREADED as u32);
    }
}

/// Starts `helper_exe --apply-update "<plan>"` outside this process tree. ShellExecuteW delegates
/// the start to the shell, so the helper is not a child of the exiting app and survives it.
#[cfg(feature = "portable")]
pub fn launch_detached_helper(helper_exe: &Path, plan_path: &Path) -> Result<(), String> {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

    if !helper_exe.is_file() {
        return Err(format!("升级助手不存在：{}", helper_exe.display()));
    }
    com_apartment();
    let verb = wide("open");
    let file = wide_path(helper_exe);
    let params = wide(&format!(
        "\"{}\" --apply-update \"{}\"",
        helper_exe.display(),
        plan_path.display()
    ));
    let directory = wide_path(
        helper_exe
            .parent()
            .unwrap_or_else(|| Path::new(".")),
    );
    // SAFETY: NUL-terminated buffers outlive the call; a null owner window is allowed.
    let result = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            verb.as_ptr(),
            file.as_ptr(),
            params.as_ptr(),
            directory.as_ptr(),
            SW_SHOWNORMAL,
        )
    };
    if (result as isize) <= 32 {
        return Err(format!(
            "无法启动升级助手：ShellExecuteW 返回 {}（错误码 {}）",
            result as isize,
            std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
        ));
    }
    Ok(())
}

/// Starts a GUI program through the shell and waits for `explorer.exe` to hand the request over
///(`explorer.exe` exits by itself, usually within a second).
pub fn launch_app_detached(exe: &Path) -> Result<(), String> {
    if !exe.is_file() {
        return Err(format!("程序不存在：{}", exe.display()));
    }
    let mut child = Command::new("explorer.exe")
        .arg(exe)
        .spawn()
        .map_err(|e| format!("无法启动 explorer.exe 打开 {}：{e}", exe.display()))?;
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) | Err(_) => break,
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
        }
    }
    Ok(())
}

/// Best-effort cleanup of `%TEMP%\hub-update-helper-*.exe` older than an hour. A running helper
/// cannot be deleted on Windows, so an in-flight upgrade is never disturbed.
pub fn cleanup_stale_helpers() {
    let Ok(entries) = fs::read_dir(std::env::temp_dir()) else {
        return;
    };
    let own = std::env::current_exe().ok();
    let now = SystemTime::now();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if !name.starts_with(HELPER_PREFIX) || !name.ends_with(".exe") {
            continue;
        }
        let path = entry.path();
        if own.as_deref() == Some(path.as_path()) {
            continue;
        }
        let old = entry
            .metadata()
            .and_then(|meta| meta.modified())
            .ok()
            .and_then(|modified| now.duration_since(modified).ok())
            .map(|age| age > HELPER_STALE_AGE)
            .unwrap_or(false);
        if old {
            let _ = fs::remove_file(&path);
        }
    }
}

// ============================================================================
// Minimal blocking HTTP (the helper runs before the Tauri runtime exists)
// ============================================================================

fn decode_chunked(body: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    let mut at = 0usize;
    loop {
        let Some(line_end) = body[at..].windows(2).position(|w| w == b"\r\n").map(|i| at + i) else {
            return Err("chunked 响应缺少长度行".into());
        };
        let size_text = std::str::from_utf8(&body[at..line_end]).unwrap_or("").trim();
        let size = usize::from_str_radix(
            size_text.split(';').next().unwrap_or("").trim(),
            16,
        )
        .map_err(|_| format!("chunked 长度无法解析：{size_text}"))?;
        at = line_end + 2;
        if size == 0 {
            return Ok(out);
        }
        if at + size > body.len() {
            return Err("chunked 响应被截断".into());
        }
        out.extend_from_slice(&body[at..at + size]);
        at += size;
        if body[at..].starts_with(b"\r\n") {
            at += 2;
        }
    }
}

fn parse_http_json(raw: &[u8]) -> Result<Value, String> {
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| "响应缺少头部结束标记".to_string())?;
    let head = std::str::from_utf8(&raw[..split]).map_err(|e| e.to_string())?;
    let status: u16 = head
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| "无法解析 HTTP 状态行".to_string())?;
    let body = if head.to_ascii_lowercase().contains("transfer-encoding: chunked") {
        decode_chunked(&raw[split + 4..])?
    } else {
        raw[split + 4..].to_vec()
    };
    if !(200..300).contains(&status) {
        return Err(format!(
            "HTTP {status} {}",
            String::from_utf8_lossy(&body).trim()
        ));
    }
    serde_json::from_slice(&body).map_err(|e| format!("响应不是 JSON：{e}"))
}

fn http_get_json(port: u16, path: &str, timeout: Duration) -> Result<Value, String> {
    let addr = SocketAddr::from(([127u8, 0, 0, 1], port));
    let mut stream =
        TcpStream::connect_timeout(&addr, timeout).map_err(|e| format!("连接失败：{e}"))?;
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("写入失败：{e}"))?;
    let mut raw = Vec::new();
    let mut buf = [0u8; 4096];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&buf[..n]);
                if raw.len() > 4 * 1024 * 1024 {
                    break;
                }
            }
            Err(error)
                if error.kind() == std::io::ErrorKind::WouldBlock
                    || error.kind() == std::io::ErrorKind::TimedOut =>
            {
                if raw.is_empty() {
                    return Err(error.to_string());
                }
                break;
            }
            Err(error) => {
                if raw.is_empty() {
                    return Err(error.to_string());
                }
                break;
            }
        }
    }
    parse_http_json(&raw)
}

// ============================================================================
// apply-update
// ============================================================================

struct UpdatePaths {
    target: PathBuf,
    staged: PathBuf,
    backup: PathBuf,
}

fn plan_paths(plan: &RestartPlan) -> Result<UpdatePaths, String> {
    let target = plan
        .target_exe
        .clone()
        .ok_or_else(|| "计划缺少 target_exe".to_string())?;
    let staged = plan
        .staged_exe
        .clone()
        .ok_or_else(|| "计划缺少 staged_exe".to_string())?;
    let backup = plan
        .backup_exe
        .clone()
        .ok_or_else(|| "计划缺少 backup_exe".to_string())?;
    if !target.is_file() {
        return Err(format!("target_exe 不存在：{}", target.display()));
    }
    if !staged.is_file() {
        return Err(format!("staged_exe 不存在：{}", staged.display()));
    }
    Ok(UpdatePaths {
        target,
        staged,
        backup,
    })
}

/// Everything that must hold before the target may be touched at all.
fn validate_update_plan(plan: &RestartPlan, now: f64) -> Result<UpdatePaths, String> {
    if plan.version != PLAN_VERSION {
        // Only this build's plan layout is understood; another version may mean fields this code
        // would misread, so it is refused instead of guessed at.
        return Err(format!(
            "计划版本 {} 不受支持（本程序只接受 {PLAN_VERSION}）",
            plan.version
        ));
    }
    if plan.mode != "replace-exe" {
        return Err(format!("计划的 mode 不是 replace-exe：{}", plan.mode));
    }
    if plan.is_expired(now) {
        return Err("计划已过期".to_string());
    }
    if plan.port == 0 {
        // Without a port the helper could replace the EXE and never be able to tell whether the
        // new build came up; refuse instead of accepting an unverifiable swap.
        return Err("计划缺少 port，无法验证新版本是否启动".to_string());
    }
    let expected = plan
        .new_exe_sha256
        .as_deref()
        .map(str::trim)
        .filter(|sha| sha.len() == 64 && sha.chars().all(|c| c.is_ascii_hexdigit()))
        .ok_or_else(|| "计划缺少合法的 new_exe_sha256".to_string())?;
    let paths = plan_paths(plan)?;
    let staged_sha = sha256_file(&paths.staged)?;
    if !staged_sha.eq_ignore_ascii_case(expected) {
        return Err(format!(
            "暂存文件哈希不匹配：期望 {expected}，实际 {staged_sha}"
        ));
    }
    // The file about to be replaced must still be the build the plan was written for: anything else
    // means the EXE changed under us, and swapping it would destroy an unbacked-up program. When
    // the plan carries `old_exe_sha256` it must be a real hash and it must match — a malformed or
    // empty value is refused, never skipped.
    if let Some(raw) = plan.old_exe_sha256.as_deref() {
        let old = raw.trim();
        if old.len() != 64 || !old.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(format!(
                "计划的 old_exe_sha256 不是合法的 64 位十六进制：{raw:?}"
            ));
        }
        let target_sha = sha256_file(&paths.target)?;
        if !target_sha.eq_ignore_ascii_case(old) {
            return Err(format!(
                "当前程序的哈希与该计划的 old_exe_sha256 不一致：{target_sha} != {old}"
            ));
        }
    }
    Ok(paths)
}

/// The old build on disk is still intact: it exists and, when the plan records the hash it
/// replaced, its bytes still match.
fn target_exe_is_intact(plan: &RestartPlan) -> bool {
    let Some(target) = plan.target_exe.as_deref() else {
        return false;
    };
    if !target.is_file() {
        return false;
    }
    match plan.old_exe_sha256.as_deref().map(str::trim) {
        Some(old) if old.len() == 64 && old.chars().all(|c| c.is_ascii_hexdigit()) => {
            sha256_file(target).is_ok_and(|sha| sha.eq_ignore_ascii_case(old))
        }
        // No old hash to compare against: existence is all this plan lets us verify.
        _ => true,
    }
}

/// Whether a validation failure has to bring the old app back: only when its EXE is intact *and*
/// that exact process instance is gone. An app that is still running must never be started twice.
fn old_app_needs_relaunch(
    plan: &RestartPlan,
    target_intact: bool,
    is_alive: impl Fn(ProcessIdentity) -> bool,
    creation_time: impl Fn(u32) -> Option<u64>,
) -> bool {
    if !target_intact {
        return false;
    }
    match plan.app_identity() {
        Some(identity) => !is_alive(identity),
        // A plan without a verified app identity cannot prove the shell is still up. The helper
        // only runs after that shell exited, so relaunching is the safe choice here.
        None => plan.app_pid == 0 || creation_time(plan.app_pid).is_none(),
    }
}

fn wait_for_identity_exit(identity: ProcessIdentity, deadline: Instant) -> bool {
    while Instant::now() < deadline {
        if !windows_process::process_is_alive(identity) {
            return true;
        }
        std::thread::sleep(CMD_POLL);
    }
    !windows_process::process_is_alive(identity)
}

fn rollback_and_relaunch(
    plan_path: &Path,
    plan: &RestartPlan,
    paths: &UpdatePaths,
    phase: &str,
    message: &str,
) -> i32 {
    let restored = rollback_from_backup(&paths.target, &paths.backup);
    let relaunched = launch_app_detached(&paths.target);
    let (rolled_back, note) = match (restored, relaunched) {
        (Ok(()), Ok(())) => (true, message.to_string()),
        (Ok(()), Err(e)) => (true, format!("{message}；但旧程序没能自动拉起：{e}")),
        (Err(e), _) => (false, format!("{message}；回滚失败：{e}")),
    };
    let _ = write_restart_result(
        plan_path,
        &json!({
            "ok": false,
            "phase": phase,
            "rolled_back": rolled_back,
            "restart_id": plan.restart_id,
            "message": note,
            "updated_at": now_secs(),
        }),
    );
    let _ = writeln!(std::io::stderr(), "{note}");
    1
}

/// `<portable_root>/progress.json`: the bootstrap step file the prepare run keeps rewriting.
fn bootstrap_progress_path(plan: &RestartPlan) -> Option<PathBuf> {
    #[cfg(feature = "portable")]
    {
        let _ = plan;
        Some(crate::hub_config::portable_root().join("progress.json"))
    }
    #[cfg(not(feature = "portable"))]
    {
        // The plan's data dir is `<root>/data`, so its parent is the root the bootstrap writes to.
        plan.data_dir
            .as_ref()?
            .parent()
            .map(|root| root.join("progress.json"))
    }
}

/// `progress.json` written within the last `window`: the new build is demonstrably still building
/// its first environment (venv, dependencies), not stuck.
fn progress_is_fresh(path: &Path, window: Duration) -> bool {
    let Ok(modified) = fs::metadata(path).and_then(|meta| meta.modified()) else {
        return false;
    };
    SystemTime::now()
        .duration_since(modified)
        .map(|age| age < window)
        .unwrap_or(false)
}

fn wait_for_new_hub(plan: &RestartPlan) -> StartOutcome {
    if plan.port == 0 {
        return StartOutcome::Ready(Value::Null);
    }
    let progress = bootstrap_progress_path(plan);
    let started = Instant::now();
    let mut deadline = started + HUB_READY_WAIT;
    let mut extended = false;
    loop {
        if let Ok(info) = http_get_json(plan.port, "/api/hub/info", HTTP_TIMEOUT) {
            if info.get("service").and_then(Value::as_str) == Some(crate::hub_api::SERVICE_NAME) {
                return StartOutcome::Ready(info);
            }
        }
        if !extended && Instant::now() >= deadline {
            // A brand-new install may still be building its venv. Fresh `progress.json` movements
            // prove the bootstrap is alive, and a live app process proves it is not simply dead.
            let bootstrapping = progress
                .as_deref()
                .is_some_and(|path| progress_is_fresh(path, PROGRESS_FRESH_WINDOW));
            let app_alive = plan
                .target_exe
                .as_deref()
                .and_then(windows_process::find_process_by_image)
                .is_some();
            if bootstrapping || app_alive {
                extended = true;
                // Hard cap on the whole wait, however lively the bootstrap looks.
                deadline = started + HUB_READY_WAIT_WHILE_BOOTSTRAPPING;
            }
        }
        if Instant::now() >= deadline {
            let app = plan
                .target_exe
                .as_deref()
                .and_then(windows_process::find_process_by_image)
                .and_then(windows_process::process_identity);
            return match app {
                Some(identity) => StartOutcome::AppAliveNoHub(identity),
                None => StartOutcome::NeverStarted,
            };
        }
        std::thread::sleep(CMD_POLL);
    }
}

enum StartOutcome {
    Ready(Value),
    /// The new app process exists (it may be sitting on its error page) but its hub never answered.
    AppAliveNoHub(ProcessIdentity),
    NeverStarted,
}

/// P1-3: the new app is alive but never brought a hub up — typically stuck on the error page after
/// a failed bootstrap. End *that exact* instance, put the backup back, start the old build again and
/// report what happened; the desktop must never be left with a shell but no hub.
fn recover_from_no_hub(
    plan_path: &Path,
    plan: &RestartPlan,
    paths: &UpdatePaths,
    app: ProcessIdentity,
    kill: impl Fn(&[ProcessIdentity]) -> usize,
    relaunch: impl Fn(&Path) -> Result<(), String>,
) -> i32 {
    let mut note = format!(
        "新程序已启动，但 Hub 在 {} 秒内没有响应 /api/hub/info（新程序可能停在错误页）",
        HUB_READY_WAIT_WHILE_BOOTSTRAPPING.as_secs()
    );
    // Only the instance the plan's own EXE was just started from may be ended, never a bare PID.
    let _ = writeln!(
        std::io::stderr(),
        "apply-update: new app PID {} (created {}) has no hub; ending it and rolling back",
        app.pid,
        app.creation_time
    );
    kill(&[app]);
    let gone = wait_for_identity_exit(app, Instant::now() + Duration::from_secs(10));
    let mut rolled_back = false;
    let mut old_app_relaunched = false;
    if !gone {
        note.push_str(&format!(
            "；故障的新程序（PID {}，创建时间 {}）无法结束，未替换任何文件",
            app.pid, app.creation_time
        ));
    } else {
        match rollback_from_backup(&paths.target, &paths.backup) {
            Ok(()) => {
                rolled_back = true;
                match relaunch(&paths.target) {
                    Ok(()) => {
                        old_app_relaunched = true;
                        note.push_str("；已回滚到旧版本并重新拉起");
                    }
                    Err(error) => {
                        note.push_str(&format!("；已回滚到旧版本，但未能自动拉起：{error}"))
                    }
                }
            }
            Err(error) => note.push_str(&format!(
                "；回滚失败，备份保留在 {}：{error}",
                paths.backup.display()
            )),
        }
    }
    let backup_exe = if rolled_back {
        Value::Null
    } else {
        json!(paths.backup.to_string_lossy())
    };
    let _ = write_restart_result(
        plan_path,
        &json!({
            "ok": false,
            "phase": "new_hub_timeout",
            "rolled_back": rolled_back,
            "old_app_relaunched": old_app_relaunched,
            "backup_exe": backup_exe,
            "restart_id": plan.restart_id,
            "message": note,
            "updated_at": now_secs(),
        }),
    );
    let _ = writeln!(std::io::stderr(), "{note}");
    1
}

fn wait_restart_status(port: u16) -> Option<Value> {
    if port == 0 {
        return None;
    }
    let deadline = Instant::now() + RESTART_STATUS_WAIT;
    loop {
        if let Ok(status) = http_get_json(port, "/api/hub/restart-status", HTTP_TIMEOUT) {
            let phase = status.get("phase").and_then(Value::as_str).unwrap_or("");
            if matches!(phase, "ready" | "failed" | "idle") {
                return Some(status);
            }
        }
        if Instant::now() >= deadline {
            return None;
        }
        std::thread::sleep(CMD_POLL);
    }
}

/// `--apply-update <plan>`: the whole swap, verified at every step, with a rollback that restores
/// the previous EXE. Returns 0 only when the new hub answers on the port.
pub fn apply_update(plan_path: &Path) -> i32 {
    let now = now_secs();
    let plan = match fs::read_to_string(plan_path) {
        Ok(text) => serde_json::from_str::<RestartPlan>(text.trim_start_matches('\u{feff}')),
        Err(error) => {
            let _ = write_restart_result(
                plan_path,
                &json!({
                    "ok": false,
                    "phase": "validate_failed",
                    "rolled_back": false,
                    "message": format!("无法读取计划 {}：{error}", plan_path.display()),
                    "updated_at": now,
                }),
            );
            return 1;
        }
    };
    let plan = match plan {
        Ok(plan) => plan,
        Err(error) => {
            let _ = write_restart_result(
                plan_path,
                &json!({
                    "ok": false,
                    "phase": "validate_failed",
                    "rolled_back": false,
                    "message": format!("计划无法解析：{error}"),
                    "updated_at": now,
                }),
            );
            return 1;
        }
    };
    let paths = match validate_update_plan(&plan, now) {
        Ok(paths) => paths,
        Err(message) => {
            // Nothing has been touched: the original EXE is still in place. But the shell that
            // started this helper has already exited in the normal flow, so leaving the machine
            // with no app and no hub is worse than bringing the old build back.
            let mut note = message;
            let mut relaunched = false;
            if old_app_needs_relaunch(
                &plan,
                target_exe_is_intact(&plan),
                windows_process::process_is_alive,
                windows_process::creation_time,
            ) {
                if let Some(target) = plan.target_exe.as_deref() {
                    match launch_app_detached(target) {
                        Ok(()) => {
                            relaunched = true;
                            note.push_str("；已把原程序重新拉起");
                        }
                        Err(error) => {
                            note.push_str(&format!("；原程序未能重新拉起：{error}"));
                        }
                    }
                }
            }
            let _ = write_restart_result(
                plan_path,
                &json!({
                    "ok": false,
                    "phase": "validate_failed",
                    "rolled_back": false,
                    "old_app_relaunched": relaunched,
                    "restart_id": plan.restart_id,
                    "message": note,
                    "updated_at": now,
                }),
            );
            return 1;
        }
    };
    let _ = writeln!(
        std::io::stderr(),
        "apply-update: {} -> {}",
        paths.staged.display(),
        paths.target.display()
    );

    // 1. The old app and its hub must be gone before the EXE can be replaced, and only the exact
    //    instances named by the plan count: a reused PID is a different program.
    let deadline = Instant::now() + OLD_PROCESS_WAIT;
    for identity in [plan.hub_identity(), plan.app_identity()].into_iter().flatten() {
        if !wait_for_identity_exit(identity, deadline) {
            let message = format!(
                "PID {}（创建时间 {}）在 {} 秒内没有退出，未替换任何文件",
                identity.pid,
                identity.creation_time,
                OLD_PROCESS_WAIT.as_secs()
            );
            let _ = write_restart_result(
                plan_path,
                &json!({
                    "ok": false,
                    "phase": "wait_exit_timeout",
                    "rolled_back": false,
                    "restart_id": plan.restart_id,
                    "message": message,
                    "updated_at": now_secs(),
                }),
            );
            let _ = writeln!(std::io::stderr(), "{message}");
            return 1;
        }
    }

    // 2. The swap itself.
    if let Err(message) = replace_file_atomic_with_retry(&paths.target, &paths.staged, &paths.backup) {
        return rollback_and_relaunch(plan_path, &plan, &paths, "replace_failed", &message);
    }
    let expected = plan.new_exe_sha256.clone().unwrap_or_default();
    let target_sha = sha256_file(&paths.target).unwrap_or_default();
    if !target_sha.eq_ignore_ascii_case(expected.trim()) {
        return rollback_and_relaunch(
            plan_path,
            &plan,
            &paths,
            "verify_failed",
            &format!("替换后哈希不匹配：期望 {expected}，实际 {target_sha}"),
        );
    }
    let _ = writeln!(std::io::stderr(), "apply-update: target verified {target_sha}");

    // 3. Start the new app and wait for its hub.
    if let Err(message) = launch_app_detached(&paths.target) {
        return rollback_and_relaunch(plan_path, &plan, &paths, "launch_failed", &message);
    }
    match wait_for_new_hub(&plan) {
        StartOutcome::Ready(info) => {
            let status = wait_restart_status(plan.port);
            let hub_pid = info.get("pid").and_then(Value::as_u64).unwrap_or(0);
            let resume_failed = status
                .as_ref()
                .and_then(|s| s.get("failed_count"))
                .and_then(Value::as_u64)
                .unwrap_or(0);
            let _ = fs::remove_file(&paths.backup);
            let _ = fs::remove_file(plan_path);
            let _ = write_restart_result(
                plan_path,
                &json!({
                    "ok": true,
                    "phase": "done",
                    "rolled_back": false,
                    "restart_id": plan.restart_id,
                    "new_hub_pid": hub_pid,
                    "resume_failed": resume_failed,
                    "restart_status": status,
                    "message": if resume_failed > 0 {
                        "新版本已启动；个别会话恢复失败，未回滚 EXE".to_string()
                    } else {
                        "新版本已启动".to_string()
                    },
                    "updated_at": now_secs(),
                }),
            );
            let _ = writeln!(std::io::stderr(), "apply-update: done (hub pid {hub_pid})");
            0
        }
        StartOutcome::AppAliveNoHub(app) => recover_from_no_hub(
            plan_path,
            &plan,
            &paths,
            app,
            windows_process::kill_identities,
            launch_app_detached,
        ),
        StartOutcome::NeverStarted => rollback_and_relaunch(
            plan_path,
            &plan,
            &paths,
            "never_started",
            "新程序没有启动起来，已回滚到旧版本",
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir() -> tempfile::TempDir {
        tempfile::tempdir().unwrap()
    }

    fn write(path: &Path, bytes: &[u8]) {
        std::fs::write(path, bytes).unwrap();
    }

    #[test]
    fn sha256_matches_the_fips_vectors() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            sha256_hex(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
        // Two-block and multi-block inputs through the streaming path.
        assert_eq!(
            sha256_hex(
                b"abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu"
            ),
            "cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1"
        );
        assert_eq!(
            sha256_hex(&vec![b'a'; 600]),
            "ba35c170729417f1499e0886e7e12fcdb4ab00ad411110ae1e888c766d4ed70d"
        );
    }

    #[test]
    fn build_info_is_one_line_json_with_a_real_hash() {
        let text = build_info_json().unwrap();
        assert!(!text.contains('\n'), "{text}");
        let value: Value = serde_json::from_str(&text).unwrap();
        let sha = value["exe_sha256"].as_str().unwrap();
        assert_eq!(sha.len(), 64, "{sha}");
        assert!(sha.chars().all(|c| c.is_ascii_hexdigit()), "{sha}");
        let current = sha256_file(&std::env::current_exe().unwrap()).unwrap();
        assert_eq!(sha, current);
        assert_eq!(value["version"].as_str(), Some(env!("CARGO_PKG_VERSION")));
        assert_eq!(value["portable"].as_bool(), Some(cfg!(feature = "portable")));
        if cfg!(feature = "portable") {
            assert!(value["payload_id"].is_string(), "{text}");
        } else {
            assert!(value["payload_id"].is_null(), "{text}");
        }
    }

    #[test]
    fn replace_file_atomic_swaps_and_keeps_the_old_bytes() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-1.exe");
        let backup = dir.path().join("app.exe.bak");
        write(&target, b"OLD");
        write(&staged, b"NEW");

        replace_file_atomic(&target, &staged, &backup).unwrap();

        assert_eq!(std::fs::read(&target).unwrap(), b"NEW");
        assert_eq!(std::fs::read(&backup).unwrap(), b"OLD");
        assert!(!staged.exists(), "the staged file became the target");
        assert_eq!(
            sha256_file(&target).unwrap(),
            sha256_hex(b"NEW"),
            "the swapped file keeps its bytes"
        );
    }

    #[test]
    fn replace_file_atomic_replaces_an_existing_backup() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-2.exe");
        let backup = dir.path().join("app.exe.bak");
        write(&target, b"OLD");
        write(&staged, b"NEW");
        write(&backup, b"ANOLD-OLD-BACKUP");

        replace_file_atomic(&target, &staged, &backup).unwrap();
        assert_eq!(std::fs::read(&backup).unwrap(), b"OLD");
    }

    #[test]
    fn rollback_from_backup_restores_the_old_bytes() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let backup = dir.path().join("app.exe.bak");
        let old: Vec<u8> = (0..4096).map(|i| (i % 251) as u8).collect();
        write(&target, &old);
        write(&backup, &old);
        // Simulate a swap that produced the wrong bytes.
        write(&target, b"HALF-WRITTEN-NEW-BUILD");

        rollback_from_backup(&target, &backup).unwrap();

        assert_eq!(std::fs::read(&target).unwrap(), old);
        assert_eq!(sha256_file(&target).unwrap(), sha256_hex(&old));
        assert!(!backup.exists(), "the backup is consumed by the rollback");
    }

    fn plan_json(dir: &Path, staged: &Path, target: &Path) -> RestartPlan {
        // This test process stands in for the old app and it is alive, so a validation failure can
        // never make a unit test start a real program.
        let me = std::process::id();
        RestartPlan {
            version: PLAN_VERSION,
            restart_id: "test-restart".into(),
            mode: "replace-exe".into(),
            requested_at: 1000.0,
            expires_at: now_secs() + 600.0,
            port: 4444,
            app_pid: me,
            app_creation_time: windows_process::creation_time(me).unwrap_or(1),
            hub_pid: 0,
            hub_creation_time: 0,
            data_dir: None,
            target_exe: Some(target.to_path_buf()),
            old_exe_sha256: Some(sha256_hex(b"OLD")),
            new_exe: None,
            new_exe_sha256: Some("0".repeat(64)),
            staged_exe: Some(staged.to_path_buf()),
            backup_exe: Some(dir.join("app.exe.bak")),
        }
    }

    fn read_result(plan_path: &Path) -> Value {
        serde_json::from_str(
            &std::fs::read_to_string(restart_result_path(plan_path)).expect("a result file"),
        )
        .expect("a JSON result")
    }

    #[test]
    fn apply_update_refuses_a_hash_mismatch_and_keeps_the_target() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-3.exe");
        write(&target, b"OLD");
        write(&staged, b"NEW");
        let plan = plan_json(dir.path(), &staged, &target);
        let plan_path = dir.path().join("restart_plan.json");
        write_restart_plan(&plan_path, &plan).unwrap();

        let code = apply_update(&plan_path);

        assert_eq!(code, 1);
        assert_eq!(std::fs::read(&target).unwrap(), b"OLD", "target untouched");
        assert_eq!(std::fs::read(&staged).unwrap(), b"NEW", "staged untouched");
        assert!(!dir.path().join("app.exe.bak").exists());
        let result: Value =
            serde_json::from_str(&std::fs::read_to_string(restart_result_path(&plan_path)).unwrap())
                .unwrap();
        assert_eq!(result["ok"], json!(false));
        assert_eq!(result["phase"], json!("validate_failed"));
        assert_eq!(result["rolled_back"], json!(false));
        assert!(result["message"].as_str().unwrap().contains("哈希不匹配"));
    }

    #[test]
    fn apply_update_rejects_an_expired_plan() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-4.exe");
        write(&target, b"OLD");
        write(&staged, b"NEW");
        let mut plan = plan_json(dir.path(), &staged, &target);
        plan.new_exe_sha256 = Some(sha256_hex(b"NEW"));
        plan.expires_at = now_secs() - 1.0;
        let plan_path = dir.path().join("restart_plan.json");
        write_restart_plan(&plan_path, &plan).unwrap();

        assert_eq!(apply_update(&plan_path), 1);
        assert_eq!(std::fs::read(&target).unwrap(), b"OLD");
        let result: Value =
            serde_json::from_str(&std::fs::read_to_string(restart_result_path(&plan_path)).unwrap())
                .unwrap();
        assert_eq!(result["phase"], json!("validate_failed"));
        assert!(result["message"].as_str().unwrap().contains("过期"));
    }

    #[test]
    fn apply_update_refuses_a_plan_without_a_port() {
        // A swap that could not be verified afterwards must be refused before any file moves.
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-5.exe");
        write(&target, b"OLD");
        write(&staged, b"NEW");
        let mut plan = plan_json(dir.path(), &staged, &target);
        plan.new_exe_sha256 = Some(sha256_hex(b"NEW"));
        plan.port = 0;
        let plan_path = dir.path().join("restart_plan.json");
        write_restart_plan(&plan_path, &plan).unwrap();

        assert_eq!(apply_update(&plan_path), 1);
        assert_eq!(std::fs::read(&target).unwrap(), b"OLD");
        let result: Value =
            serde_json::from_str(&std::fs::read_to_string(restart_result_path(&plan_path)).unwrap())
                .unwrap();
        assert_eq!(result["phase"], json!("validate_failed"));
        assert!(result["message"].as_str().unwrap().contains("port"));
    }

    #[test]
    fn apply_update_refuses_a_target_that_is_not_the_planned_build() {
        // The file on disk changed since the plan was written: replacing it would destroy a
        // program no backup covers.
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-6.exe");
        write(&target, b"SOMETHING-ELSE");
        write(&staged, b"NEW");
        let mut plan = plan_json(dir.path(), &staged, &target);
        plan.new_exe_sha256 = Some(sha256_hex(b"NEW"));
        let plan_path = dir.path().join("restart_plan.json");
        write_restart_plan(&plan_path, &plan).unwrap();

        assert_eq!(apply_update(&plan_path), 1);
        assert_eq!(std::fs::read(&target).unwrap(), b"SOMETHING-ELSE");
        let result: Value =
            serde_json::from_str(&std::fs::read_to_string(restart_result_path(&plan_path)).unwrap())
                .unwrap();
        assert_eq!(result["phase"], json!("validate_failed"));
        assert!(result["message"].as_str().unwrap().contains("old_exe_sha256"));
    }

    #[test]
    fn apply_update_refuses_an_unsupported_plan_version() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-9.exe");
        write(&target, b"OLD");
        write(&staged, b"NEW");
        let mut plan = plan_json(dir.path(), &staged, &target);
        plan.new_exe_sha256 = Some(sha256_hex(b"NEW"));
        plan.version = PLAN_VERSION + 1;
        let plan_path = dir.path().join("restart_plan.json");
        write_restart_plan(&plan_path, &plan).unwrap();

        assert_eq!(apply_update(&plan_path), 1);
        assert_eq!(std::fs::read(&target).unwrap(), b"OLD", "target untouched");
        assert_eq!(std::fs::read(&staged).unwrap(), b"NEW", "staged untouched");
        let result = read_result(&plan_path);
        assert_eq!(result["phase"], json!("validate_failed"));
        assert!(result["message"].as_str().unwrap().contains("版本"), "{result}");
        assert_eq!(result["old_app_relaunched"], json!(false));
    }

    #[test]
    fn a_malformed_or_empty_old_exe_sha256_is_refused() {
        for bad in ["".to_string(), "not-a-hash".to_string(), "z".repeat(64)] {
            let dir = temp_dir();
            let target = dir.path().join("app.exe");
            let staged = dir.path().join(".hub-update-10.exe");
            write(&target, b"OLD");
            write(&staged, b"NEW");
            let mut plan = plan_json(dir.path(), &staged, &target);
            plan.new_exe_sha256 = Some(sha256_hex(b"NEW"));
            plan.old_exe_sha256 = Some(bad.clone());
            let plan_path = dir.path().join("restart_plan.json");
            write_restart_plan(&plan_path, &plan).unwrap();

            assert_eq!(apply_update(&plan_path), 1, "{bad:?}");
            assert_eq!(std::fs::read(&target).unwrap(), b"OLD", "{bad:?}");
            let result = read_result(&plan_path);
            assert_eq!(result["phase"], json!("validate_failed"), "{bad:?}");
            let message = result["message"].as_str().unwrap();
            assert!(message.contains("old_exe_sha256"), "{bad:?}: {message}");
        }
    }

    #[test]
    fn a_validate_failure_relaunches_the_old_app_only_when_it_is_gone() {
        let mut plan = plan_json(Path::new("."), Path::new("staged.exe"), Path::new("app.exe"));
        let gone = |_: ProcessIdentity| false;
        let alive = |_: ProcessIdentity| true;
        // The EXE is intact and this exact app instance is gone: bring it back.
        assert!(old_app_needs_relaunch(&plan, true, gone, |_| Some(1)));
        // A running shell is never started a second time.
        assert!(!old_app_needs_relaunch(&plan, true, alive, |_| Some(1)));
        // Nothing to start when the EXE is not intact.
        assert!(!old_app_needs_relaunch(&plan, false, gone, |_| Some(1)));
        // A plan that names no app at all cannot prove anything: relaunch is the safe choice.
        plan.app_pid = 0;
        plan.app_creation_time = 0;
        assert!(old_app_needs_relaunch(&plan, true, gone, |_| None));
        // A legacy plan with only `app_pid`: an unverifiable PID counts as gone, a live one does not.
        plan.app_pid = 4242;
        plan.app_creation_time = 0;
        assert!(old_app_needs_relaunch(&plan, true, gone, |_| None));
        assert!(!old_app_needs_relaunch(&plan, true, gone, |_| Some(99)));
    }

    #[test]
    fn a_failed_swap_keeps_the_only_copy_of_the_old_build() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-11.exe");
        let backup = dir.path().join("app.exe.bak");
        write(&staged, b"NEW");
        write(&backup, b"OLD");
        // A partially failed swap already lost the target: the `.bak` is the only copy left.
        assert!(!target.exists());

        let error = replace_file_atomic_with_retry(&target, &staged, &backup)
            .expect_err("there is nothing to replace");
        assert!(error.contains("不存在"), "{error}");
        assert_eq!(std::fs::read(&backup).unwrap(), b"OLD", "the .bak is never deleted");

        rollback_from_backup(&target, &backup).unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"OLD");
        assert_eq!(sha256_file(&target).unwrap(), sha256_hex(b"OLD"));
        assert!(!backup.exists(), "the rollback consumed the backup");
    }

    #[test]
    fn only_the_first_attempt_of_a_swap_clears_the_backup() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-12.exe");
        let backup = dir.path().join("app.exe.bak");
        write(&target, b"OLD");
        // A directory exists but can never replace an EXE: this keeps the swap failing deterministically.
        std::fs::create_dir(&staged).unwrap();
        write(&backup, b"GOLD");

        // A retry (`clear_backup = false`) leaves the existing `.bak` exactly as it is.
        let error = replace_file_atomic_inner(&target, &staged, &backup, false)
            .expect_err("a directory can never replace an EXE");
        assert!(error.contains("无法替换"), "{error}");
        assert_eq!(std::fs::read(&backup).unwrap(), b"GOLD");
        assert_eq!(std::fs::read(&target).unwrap(), b"OLD", "target untouched");

        // The very first attempt frees the backup name `ReplaceFileW` requires.
        let _ = replace_file_atomic_inner(&target, &staged, &backup, true);
        assert!(!backup.exists(), "the first attempt clears the backup name");
        assert_eq!(std::fs::read(&target).unwrap(), b"OLD", "target untouched");
    }

    #[test]
    fn a_new_app_without_a_hub_is_rolled_back_and_relaunched() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-13.exe");
        let backup = dir.path().join("app.exe.bak");
        let old: Vec<u8> = (0..4096).map(|i| (i % 251) as u8).collect();
        write(&target, b"NEW-BUILD");
        write(&staged, b"NEW-BUILD");
        write(&backup, &old);
        let plan = plan_json(dir.path(), &staged, &target);
        let plan_path = dir.path().join("restart_plan.json");
        write_restart_plan(&plan_path, &plan).unwrap();
        let paths = UpdatePaths {
            target: target.clone(),
            staged: staged.clone(),
            backup: backup.clone(),
        };
        // A PID no process has: the faulty new app is gone as far as this assertion cares.
        let app = ProcessIdentity {
            pid: 0xF000_0042,
            creation_time: 1,
        };

        let code = recover_from_no_hub(&plan_path, &plan, &paths, app, |_| 1, |_| Ok(()));

        assert_eq!(code, 1);
        assert_eq!(std::fs::read(&target).unwrap(), old, "the old build is back");
        assert!(!backup.exists(), "the rollback consumed the backup");
        let result = read_result(&plan_path);
        assert_eq!(result["ok"], json!(false));
        assert_eq!(result["phase"], json!("new_hub_timeout"));
        assert_eq!(result["rolled_back"], json!(true));
        assert_eq!(result["old_app_relaunched"], json!(true));
        assert_eq!(result["backup_exe"], Value::Null);
    }

    #[test]
    fn a_rollback_that_cannot_happen_still_names_the_backup() {
        let dir = temp_dir();
        let target = dir.path().join("app.exe");
        let staged = dir.path().join(".hub-update-14.exe");
        let backup = dir.path().join("app.exe.bak");
        write(&target, b"NEW-BUILD");
        write(&staged, b"NEW-BUILD");
        // No backup at all: the rollback cannot restore anything.
        let plan = plan_json(dir.path(), &staged, &target);
        let plan_path = dir.path().join("restart_plan.json");
        write_restart_plan(&plan_path, &plan).unwrap();
        let paths = UpdatePaths {
            target: target.clone(),
            staged: staged.clone(),
            backup: backup.clone(),
        };
        let app = ProcessIdentity {
            pid: 0xF000_0043,
            creation_time: 1,
        };

        let code = recover_from_no_hub(&plan_path, &plan, &paths, app, |_| 1, |_| Ok(()));

        assert_eq!(code, 1);
        assert_eq!(std::fs::read(&target).unwrap(), b"NEW-BUILD", "nothing was restored");
        let result = read_result(&plan_path);
        assert_eq!(result["rolled_back"], json!(false));
        assert_eq!(result["old_app_relaunched"], json!(false));
        assert_eq!(
            result["backup_exe"],
            json!(backup.to_string_lossy()),
            "the operator has to know where the backup should have been"
        );
    }

    #[test]
    fn restart_plan_round_trips_and_reports_expiry() {
        let dir = temp_dir();
        let path = dir.path().join("restart_plan.json");
        let plan = RestartPlan {
            version: PLAN_VERSION,
            restart_id: "abc".into(),
            mode: "hub".into(),
            requested_at: 1795470000.0,
            expires_at: 1795470600.0,
            port: 4444,
            app_pid: 11648,
            app_creation_time: 134399000000000000,
            hub_pid: 73584,
            hub_creation_time: 134399000000000000,
            data_dir: Some(PathBuf::from(r"C:\hub\portable\data")),
            target_exe: None,
            old_exe_sha256: None,
            new_exe: None,
            new_exe_sha256: None,
            staged_exe: None,
            backup_exe: None,
        };
        write_restart_plan(&path, &plan).unwrap();
        let read = read_restart_plan(&path).unwrap();
        assert_eq!(read, plan);
        assert!(!read.is_expired(1795470500.0));
        assert!(read.is_expired(1795470600.0));
        assert_eq!(read.app_identity().map(|i| i.pid), Some(11648));
        assert_eq!(read.hub_identity().map(|i| i.creation_time), Some(134399000000000000));
        remove_restart_plan(&path);
        assert!(read_restart_plan(&path).is_none());
    }
}
