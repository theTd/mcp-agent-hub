//! The hub's log file (rotation at start, tail for the error view) and the app's own short
//! diagnostics log (`desktop.log`, next to hub.log). The shutdown token is never written to either.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

/// hub.log passes this size → it becomes hub.log.1 at the next hub start.
pub const ROTATE_LIMIT_BYTES: u64 = 10 * 1024 * 1024;
/// Lines of hub.log shown in the error view.
pub const ERROR_TAIL_LINES: usize = 40;
const TAIL_READ_BYTES: u64 = 256 * 1024;

pub fn hub_log_path(log_dir: &Path) -> PathBuf {
    log_dir.join("hub.log")
}

pub fn rotated_path(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(".1");
    PathBuf::from(name)
}

/// Keep the previous file as `<name>.1` when it is larger than `limit`. Returns whether it rotated.
pub fn rotate_if_needed(path: &Path, limit: u64) -> io::Result<bool> {
    match fs::metadata(path) {
        Ok(meta) if meta.len() > limit => {
            let old = rotated_path(path);
            match fs::remove_file(&old) {
                Ok(()) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
            fs::rename(path, &old)?;
            Ok(true)
        }
        Ok(_) => Ok(false),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(e),
    }
}

pub fn open_append(path: &Path) -> io::Result<File> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    OpenOptions::new().create(true).append(true).open(path)
}

/// The last `n` lines of a log file ("" if it does not exist).
pub fn tail_lines(path: &Path, n: usize) -> String {
    let Ok(mut file) = File::open(path) else {
        return String::new();
    };
    let len = file.metadata().map(|m| m.len()).unwrap_or(0);
    let start = len.saturating_sub(TAIL_READ_BYTES);
    if file.seek(SeekFrom::Start(start)).is_err() {
        return String::new();
    }
    let mut bytes = Vec::new();
    if file.read_to_end(&mut bytes).is_err() {
        return String::new();
    }
    if start > 0 {
        // The read may begin mid-line (or mid-character): drop up to the first newline.
        match bytes.iter().position(|&b| b == b'\n') {
            Some(i) => {
                bytes.drain(..=i);
            }
            None => bytes.clear(),
        }
    }
    last_lines(&decode_log_bytes(&bytes), n)
}

pub fn last_lines(text: &str, n: usize) -> String {
    let lines: Vec<&str> = text.lines().collect();
    let from = lines.len().saturating_sub(n);
    lines[from..].join("\n")
}

/// Python writes redirected stdout/stderr in the ANSI code page unless told otherwise; accept
/// UTF-8 first, else decode as the system code page.
pub fn decode_log_bytes(bytes: &[u8]) -> String {
    match std::str::from_utf8(bytes) {
        Ok(s) => s.to_string(),
        Err(_) => decode_ansi(bytes).unwrap_or_else(|| String::from_utf8_lossy(bytes).into_owned()),
    }
}

#[cfg(windows)]
fn decode_ansi(bytes: &[u8]) -> Option<String> {
    use windows_sys::Win32::Globalization::{MultiByteToWideChar, CP_ACP};
    if bytes.is_empty() {
        return Some(String::new());
    }
    let len = i32::try_from(bytes.len()).ok()?;
    // SAFETY: the input pointer/length describe `bytes`; the first call only measures.
    let needed =
        unsafe { MultiByteToWideChar(CP_ACP, 0, bytes.as_ptr(), len, std::ptr::null_mut(), 0) };
    if needed <= 0 {
        return None;
    }
    let mut wide = vec![0u16; needed as usize];
    // SAFETY: `wide` has exactly `needed` u16 slots, as measured above.
    let written =
        unsafe { MultiByteToWideChar(CP_ACP, 0, bytes.as_ptr(), len, wide.as_mut_ptr(), needed) };
    if written <= 0 {
        return None;
    }
    wide.truncate(written as usize);
    Some(String::from_utf16_lossy(&wide))
}

#[cfg(not(windows))]
fn decode_ansi(_bytes: &[u8]) -> Option<String> {
    None
}

/// Local time as `YYYY-MM-DD HH:MM:SS.mmm`.
pub fn local_timestamp() -> String {
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::SystemInformation::GetLocalTime;
        // SAFETY: GetLocalTime only writes the SYSTEMTIME it is given.
        let t = unsafe {
            let mut t = std::mem::zeroed();
            GetLocalTime(&mut t);
            t
        };
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:03}",
            t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond, t.wMilliseconds
        )
    }
    #[cfg(not(windows))]
    {
        format!("{:?}", std::time::SystemTime::now())
    }
}

/// A banner written into hub.log before each launch, so a tail shows where this run starts.
pub fn write_launch_banner(file: &mut File, port: u16) {
    let _ = writeln!(
        file,
        "\n===== {} desktop shell: starting hub on 127.0.0.1:{port} =====",
        local_timestamp()
    );
    let _ = file.flush();
}

/// One line in `desktop.log` (best effort; the app never fails because of its own log).
pub fn desktop_log(log_dir: &Path, line: &str) {
    let path = log_dir.join("desktop.log");
    if let Ok(mut f) = open_append(&path) {
        let _ = writeln!(f, "{} {line}", local_timestamp());
    }
}

/// Rotate desktop.log the same way as hub.log, once per app start.
pub fn rotate_desktop_log(log_dir: &Path) {
    let _ = rotate_if_needed(&log_dir.join("desktop.log"), ROTATE_LIMIT_BYTES);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_bytes(path: &Path, n: usize, fill: u8) {
        fs::write(path, vec![fill; n]).unwrap();
    }

    #[test]
    fn rotated_name_appends_dot_one() {
        assert_eq!(
            rotated_path(Path::new(r"C:\x\hub.log")),
            PathBuf::from(r"C:\x\hub.log.1")
        );
    }

    #[test]
    fn missing_file_is_not_rotated() {
        let dir = tempfile::tempdir().unwrap();
        assert!(!rotate_if_needed(&dir.path().join("hub.log"), 10).unwrap());
    }

    #[test]
    fn file_at_the_limit_stays() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("hub.log");
        write_bytes(&log, 10, b'a');
        assert!(!rotate_if_needed(&log, 10).unwrap());
        assert_eq!(fs::metadata(&log).unwrap().len(), 10);
        assert!(!rotated_path(&log).exists());
    }

    #[test]
    fn file_past_the_limit_becomes_dot_one_and_replaces_the_old_one() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("hub.log");
        write_bytes(&rotated_path(&log), 3, b'o');
        write_bytes(&log, 11, b'n');
        assert!(rotate_if_needed(&log, 10).unwrap());
        assert!(!log.exists());
        assert_eq!(fs::read(rotated_path(&log)).unwrap(), vec![b'n'; 11]);
        // A new run appends to a fresh hub.log.
        let mut f = open_append(&log).unwrap();
        writeln!(f, "fresh").unwrap();
        drop(f);
        assert_eq!(fs::read_to_string(&log).unwrap(), "fresh\n");
    }

    #[test]
    fn tail_returns_the_last_lines() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("hub.log");
        let text: String = (1..=100).map(|i| format!("line {i}\r\n")).collect();
        fs::write(&log, text).unwrap();
        let tail = tail_lines(&log, 40);
        let lines: Vec<&str> = tail.lines().collect();
        assert_eq!(lines.len(), 40);
        assert_eq!(lines[0], "line 61");
        assert_eq!(lines[39], "line 100");
    }

    #[test]
    fn tail_of_a_short_or_missing_file() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("hub.log");
        assert_eq!(tail_lines(&log, 40), "");
        fs::write(&log, "only\n").unwrap();
        assert_eq!(tail_lines(&log, 40), "only");
    }

    #[test]
    fn tail_of_a_large_file_skips_the_partial_first_line() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("hub.log");
        let mut text = "中".repeat(200_000); // > 256 KiB, no newline: the window starts mid-character
        text.push('\n');
        for i in 0..5 {
            text.push_str(&format!("末尾 {i}\n"));
        }
        fs::write(&log, text).unwrap();
        assert_eq!(tail_lines(&log, 3), "末尾 2\n末尾 3\n末尾 4");
    }

    #[test]
    fn utf8_logs_decode_as_utf8() {
        assert_eq!(decode_log_bytes("Hub 已启动".as_bytes()), "Hub 已启动");
    }
}
