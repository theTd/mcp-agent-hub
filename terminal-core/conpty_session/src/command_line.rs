//! The child's command line, built the way pywinpty built it before the core owned the PTY.
//!
//! Engines only ever see argv through their command line, so this must stay byte-identical to
//! `winpty/ptyprocess.py::PtyProcess.spawn` (pywinpty 3.1.2) on top of winpty-rs 1.0.8:
//!
//! 1. `argv[0]` is resolved like `shutil.which(argv[0], path=env["PATH"])` (PATHEXT expansion,
//!    current-directory rule, `F_OK | X_OK` check). An unresolvable command is a spawn failure.
//! 2. `cmdline = " " + subprocess.list2cmdline(argv[1:])`, omitted entirely when there are no args
//!    (ptyprocess.py builds that string itself; `list2cmdline` is CPython's MS C runtime quoting).
//! 3. The child command line is `quoted_argv0(app)` followed by that `cmdline`, so the child's
//!    `GetCommandLineW` carries **two** spaces before the first argument (one from winpty-rs's
//!    separator, one from ptyprocess.py's leading space), and `quoted_argv0` only wraps the path in
//!    quotes when it contains a space or tab and does not already start with a quote (winpty-rs 1.0.8
//!    `src/pty/base.rs::quoted_argv0`).
//!
//! `lpApplicationName` stays NULL, as winpty-rs passes it, so the resolved path in the command line is
//! the only thing naming the executable.
//!
//! The tests below pin `list2cmdline` with known vectors; `tests/command_line_parity.rs` pins the whole
//! rule against lines actually produced by pywinpty on this machine.

use std::collections::HashMap;
use std::path::Path;

/// `subprocess.list2cmdline`: args joined with single spaces, quoted only when needed, backslashes
/// before a quote doubled (CPython 3.15's implementation, ported literally).
pub fn list2cmdline(args: &[String]) -> String {
    let mut out = String::new();
    for (index, arg) in args.iter().enumerate() {
        if index > 0 {
            out.push(' ');
        }
        let need_quote = arg.is_empty() || arg.contains(' ') || arg.contains('\t');
        if need_quote {
            out.push('"');
        }
        let mut backslashes = 0usize;
        for ch in arg.chars() {
            match ch {
                '\\' => backslashes += 1,
                '"' => {
                    // Every pair of preceding backslashes stays two backslashes; the odd one escapes.
                    for _ in 0..backslashes * 2 {
                        out.push('\\');
                    }
                    backslashes = 0;
                    out.push('\\');
                    out.push('"');
                }
                other => {
                    for _ in 0..backslashes {
                        out.push('\\');
                    }
                    backslashes = 0;
                    out.push(other);
                }
            }
        }
        for _ in 0..backslashes {
            out.push('\\');
        }
        if need_quote {
            // Quoting doubles the trailing backslashes so the closing quote stays a delimiter.
            for _ in 0..backslashes {
                out.push('\\');
            }
            out.push('"');
        }
    }
    out
}

/// winpty-rs `quoted_argv0`: quote only when the path has whitespace and is not already quoted.
pub fn quote_application(app: &str) -> String {
    let needs_quotes = !app.starts_with('"') && app.chars().any(|c| c == ' ' || c == '\t');
    if needs_quotes {
        format!("\"{app}\"")
    } else {
        app.to_string()
    }
}

/// `ntpath.split`: like `os.path.split` on Windows, the directory part keeps **no** trailing separator
/// (unlike posixpath), and a bare drive (`C:cmd.exe`) is itself the directory part.
fn split_path(path: &str) -> (&str, &str) {
    let bytes = path.as_bytes();
    let drive_len = if bytes.len() >= 2 && bytes[1] == b':' && (bytes[0] as char).is_ascii_alphabetic()
    {
        2
    } else {
        0
    };
    let rest = &path[drive_len..];
    match rest.rfind(['\\', '/']) {
        Some(index) => (&path[..drive_len + index], &path[drive_len + index + 1..]),
        None => {
            if drive_len > 0 {
                (&path[..drive_len], rest)
            } else {
                ("", path)
            }
        }
    }
}

/// `ntpath.join` for the two shapes `which` produces, plus the `""` directory entry that an empty PATH
/// element yields. A bare drive joins without a separator (`C:` + `x.exe` -> `C:x.exe`), as ntpath does.
fn join_path(dir: &str, file: &str) -> String {
    if dir.is_empty() {
        file.to_string()
    } else if dir.ends_with('\\') || dir.ends_with('/') || dir.ends_with(':') {
        format!("{dir}{file}")
    } else {
        format!("{dir}\\{file}")
    }
}

/// `os.path.normcase` on Windows: lower case and forward slashes folded to backslashes.
fn normcase(path: &str) -> String {
    path.chars()
        .map(|c| match c {
            '/' => '\\',
            other => other.to_lowercase().next().unwrap_or(other),
        })
        .collect()
}

const DEFAULT_PATHEXT: &str = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.PY;.PYW";

/// `shutil.which`'s `os.getenv("PATHEXT") or _WIN_DEFAULT_PATHEXT`: the **process** environment, not the
/// session's. pywinpty passes only `PATH` to `which`, so a session that overrides `PATHEXT` does not
/// change resolution (nit: the session env used to win here).
fn pathext() -> Vec<String> {
    let source = std::env::var("PATHEXT")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_PATHEXT.to_string());
    source
        .split(';')
        .filter(|ext| !ext.is_empty())
        .map(|ext| ext.trim_end_matches('.').to_string())
        .collect()
}

/// `os.defpath` on Windows, verified on the installed interpreter (`ntpath.defpath == '.;C:\\bin'`).
/// `shutil.which(path=...)` replaces a falsy `path` with it, which is what `env.get('PATH',
/// os.defpath)` hands it when the session env has no `PATH` at all.
fn default_path() -> String {
    ".;C:\\bin".to_string()
}

/// `shutil._access_check` with `F_OK | X_OK`: an existing regular file. On Windows `os.access(X_OK)`
/// reduces to existence, so the mode bits add nothing.
fn access_check(path: &str) -> bool {
    Path::new(path).is_file()
}

/// `shutil._win_path_needs_curdir(cmd, os.X_OK)`: whether the current directory is searched too.
fn needs_curdir(command: &str) -> bool {
    use windows::core::PCWSTR;
    use windows::Win32::System::Environment::NeedCurrentDirectoryForExePathW;

    let wide: Vec<u16> = command.encode_utf16().chain(std::iter::once(0)).collect();
    // SAFETY: `wide` is NUL-terminated and outlives the call; the function only reads it.
    unsafe { NeedCurrentDirectoryForExePathW(PCWSTR(wide.as_ptr())).as_bool() }
}

/// `shutil.which(command, path=env.get("PATH", os.defpath))`, the resolution pywinpty performs on
/// `argv[0]` (pywinpty builds `path = env.get('PATH', os.defpath)`). A session env without `PATH` uses
/// `os.defpath`, not this process's `PATH` (nit); `PATHEXT` always comes from the process environment.
pub fn resolve_application(command: &str, env: &HashMap<String, String>) -> Option<String> {
    resolve_with(command, env.get("PATH").map(String::as_str), &pathext())
}

/// Resolution with its two inputs explicit, so the parity test can pin the rule against a captured
/// layout without touching this process's environment.
pub fn resolve_with(command: &str, session_path: Option<&str>, pathext: &[String]) -> Option<String> {
    let (dirname, name) = split_path(command);
    let mut dirs: Vec<String> = Vec::new();
    if !dirname.is_empty() {
        dirs.push(dirname.to_string());
    } else {
        // `shutil.which` returns immediately for a falsy `path`: an empty session `PATH` matches
        // nothing, while a missing one falls back to `os.defpath`.
        let path_env = session_path
            .map(str::to_string)
            .unwrap_or_else(default_path);
        if path_env.is_empty() {
            return None;
        }
        dirs.extend(path_env.split(';').map(str::to_string));
        if needs_curdir(name) {
            dirs.insert(0, ".".to_string());
        }
    }

    // `X_OK` is in the mode, so a direct match only comes first when the name already ends in one of
    // PATHEXT (shutil.which's cmd.exe simulation); otherwise each extension is tried as-is.
    let upper = name.to_uppercase();
    let direct_match = pathext
        .iter()
        .any(|ext| upper.ends_with(&ext.to_uppercase()));
    let mut files: Vec<String> = Vec::with_capacity(pathext.len() + 1);
    if direct_match {
        files.push(name.to_string());
    }
    for ext in pathext {
        files.push(format!("{name}{ext}"));
    }

    let mut seen: Vec<String> = Vec::new();
    for dir in dirs {
        let key = normcase(&dir);
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        for file in &files {
            let candidate = join_path(&dir, file);
            if access_check(&candidate) {
                return Some(candidate);
            }
        }
    }
    None
}

/// Why a command line could not be built. The message keeps pywinpty's wording so the hub's
/// `RESUME_FAILED_PREFIX`/启动失败 path reads the same as today.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnresolvedCommand(pub String);

impl std::fmt::Display for UnresolvedCommand {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "The command was not found or was not executable: {}.",
            self.0
        )
    }
}

/// Resolves `argv[0]` and builds the exact command line `CreateProcessW` receives.
///
/// pywinpty's own two spaces are deliberate: `ptyprocess.py` builds `cmdline = " " + list2cmdline(...)`
/// and winpty-rs appends `" " + cmdline`, so the child sees `"<app>"  <args>` (verified by capturing
/// `GetCommandLineW` from a child pywinpty spawned; see `tests/fixtures/cmdline_parity.tsv`).
pub fn build_command_line(
    argv: &[String],
    env: &HashMap<String, String>,
) -> Result<(String, String), UnresolvedCommand> {
    let command = argv
        .first()
        .ok_or_else(|| UnresolvedCommand(String::new()))?
        .clone();
    let app =
        resolve_application(&command, env).ok_or_else(|| UnresolvedCommand(command.clone()))?;
    let mut line = quote_application(&app);
    if argv.len() > 1 {
        // winpty-rs: quoted app + " " + cmdline, and cmdline already starts with " ".
        line.push_str("  ");
        line.push_str(&list2cmdline(&argv[1..]));
    }
    Ok((app, line))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn list2cmdline_matches_the_ms_c_runtime_rules() {
        assert_eq!(list2cmdline(&args(&["a", "b"])), "a b");
        assert_eq!(list2cmdline(&args(&["a b"])), "\"a b\"");
        assert_eq!(list2cmdline(&args(&[""])), "\"\"");
        assert_eq!(list2cmdline(&args(&["a\tb"])), "\"a\tb\"");
        // A quote is escaped; runs of backslashes before it are doubled.
        assert_eq!(list2cmdline(&args(&[r#"a"b"#])), r#"a\"b"#);
        assert_eq!(list2cmdline(&args(&[r#"a\"b"#])), r#"a\\\"b"#);
        assert_eq!(list2cmdline(&args(&[r#"sp ace\"b"#])), r#""sp ace\\\"b""#);
        // Trailing backslashes are doubled only when the argument gets quoted.
        assert_eq!(list2cmdline(&args(&[r"a\b\"])), r"a\b\");
        assert_eq!(list2cmdline(&args(&[r"a b\"])), r#""a b\\""#);
        // Non-ASCII is passed through untouched.
        assert_eq!(list2cmdline(&args(&["配置 with spaces"])), "\"配置 with spaces\"");
        // The argv shapes test_session_launch_args.py exercises.
        assert_eq!(
            list2cmdline(&args(&[
                "--mcp-config",
                r"E:\configs with spaces\mcp.json",
                r#"{"mcpServers":{}}"#,
                "",
                "$(whoami)"
            ])),
            r#"--mcp-config "E:\configs with spaces\mcp.json" {\"mcpServers\":{}} "" $(whoami)"#
        );
    }

    #[test]
    fn application_is_quoted_only_for_whitespace() {
        assert_eq!(quote_application(r"C:\Windows\System32\cmd.exe"), r"C:\Windows\System32\cmd.exe");
        assert_eq!(
            quote_application(r"C:\Program Files\node\node.exe"),
            r#""C:\Program Files\node\node.exe""#
        );
        assert_eq!(quote_application(r#""C:\already\quoted.exe""#), r#""C:\already\quoted.exe""#);
    }

    #[test]
    fn path_helpers_follow_python() {
        assert_eq!(split_path(r"E:\a\b.exe"), (r"E:\a", "b.exe"));
        assert_eq!(split_path("cmd.exe"), ("", "cmd.exe"));
        assert_eq!(split_path("C:cmd.exe"), ("C:", "cmd.exe"));
        assert_eq!(join_path("", "x.exe"), "x.exe");
        assert_eq!(join_path(r"E:\a", "x.exe"), r"E:\a\x.exe");
        assert_eq!(join_path("C:", "x.exe"), "C:x.exe");
        assert_eq!(normcase(r"C:/A/B"), r"c:\a\b");
    }

    #[test]
    fn unresolvable_command_is_a_spawn_failure() {
        let env = HashMap::new();
        let error = build_command_line(&args(&["hub-terminal-core-no-such-command"]), &env)
            .expect_err("must fail");
        assert_eq!(
            error.to_string(),
            "The command was not found or was not executable: hub-terminal-core-no-such-command."
        );
        assert!(build_command_line(&[], &env).is_err());
    }

    #[test]
    fn pathext_comes_from_the_process_environment() {
        // `shutil.which` reads PATHEXT from the process env; a session override never reaches it.
        let system32 = r"C:\Windows\System32";
        let clean: HashMap<String, String> =
            [("PATH".to_string(), system32.to_string())].into_iter().collect();
        let with_bogus: HashMap<String, String> = [
            ("PATH".to_string(), system32.to_string()),
            ("PATHEXT".to_string(), ".ZZZ".to_string()),
        ]
        .into_iter()
        .collect();
        let expected = resolve_with("cmd.exe", Some(system32), &pathext());
        assert!(expected.is_some(), "cmd.exe must resolve from System32");
        assert_eq!(resolve_application("cmd.exe", &clean), expected);
        assert_eq!(
            resolve_application("cmd.exe", &with_bogus),
            expected,
            "the session env's PATHEXT must not change resolution"
        );
    }

    #[test]
    fn a_session_env_without_path_falls_back_to_os_defpath() {
        // pywinpty passes `env.get('PATH', os.defpath)`; the fallback searches `.` and `C:\bin`, not
        // this process's PATH, and an empty PATH entry matches nothing.
        let no_path = HashMap::new();
        assert_eq!(
            resolve_application("hub-terminal-core-no-such-command", &no_path),
            None
        );
        let empty_path: HashMap<String, String> =
            [("PATH".to_string(), String::new())].into_iter().collect();
        assert_eq!(
            resolve_application("hub-terminal-core-no-such-command", &empty_path),
            None,
            "an empty PATH must not fall back to os.defpath"
        );
        // The fallback contains the current directory, so a file that exists there resolves even with
        // no PATH at all: `.` is the first entry of `os.defpath`.
        assert_eq!(default_path(), ".;C:\\bin");
    }

    #[test]
    fn cmd_exe_resolves_through_pathext_and_keeps_the_rest_verbatim() {
        let env: HashMap<String, String> = std::env::vars().collect();
        let (app, line) = build_command_line(&args(&["cmd.exe", "/c", "echo", "a b"]), &env)
            .expect("cmd.exe resolves");
        assert!(app.ends_with("cmd.exe"), "{app}");
        assert_eq!(line, format!("{app}  /c echo \"a b\""));
        // No args: the line is the application alone, with no trailing space (ptyprocess.py omits
        // `cmdline` entirely for a one-element argv).
        let (app, line) = build_command_line(&args(&["cmd.exe"]), &env).expect("cmd.exe resolves");
        assert_eq!(line, app);
    }
}
