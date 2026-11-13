//! Command-line parity against lines pywinpty 3.1.2 actually produced.
//!
//! `tests/fixtures/cmdline_parity.tsv` was captured by spawning a probe (`GetCommandLineW` printed as
//! UTF-8 hex) through pywinpty's `PtyProcess.spawn` with the argv tables `test_session_launch_args.py`
//! uses, plus spaces, quotes, backslashes before quotes, empty arguments and non-ASCII; the capture
//! script is not part of the tree. The probe's directory is stored as `<PLAIN>` / `<SPACED>` so the test
//! can recreate an equivalent path here.
//!
//! Rows: `RESOLVE <command> <hex resolved path>` and
//! `CASE <name> <hex argv0> <hex resolved app> <hex arg>|<hex arg>... <hex command line>`.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use conpty_session::command_line::{build_command_line, resolve_application};

struct Fixture {
    rows: Vec<Vec<String>>,
}

impl Fixture {
    fn load() -> Self {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/cmdline_parity.tsv");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("{}: {error}", path.display()));
        let rows = text
            .lines()
            .filter(|line| !line.trim().is_empty())
            .map(|line| line.split('\t').map(str::to_string).collect())
            .collect();
        Self { rows }
    }
}

fn unhex(text: &str) -> String {
    let bytes: Vec<u8> = (0..text.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&text[index..index + 2], 16).expect("hex"))
        .collect();
    String::from_utf8(bytes).expect("utf-8")
}

/// Creates a tool directory (optionally with a space in it) holding the empty probe file, and removes it
/// on drop.
struct ToolDir {
    path: PathBuf,
}

impl ToolDir {
    fn new(name: &str) -> Self {
        let root = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
        std::fs::create_dir_all(&root).expect("create tool dir");
        std::fs::write(root.join("cmdline_probe.exe"), b"").expect("probe file");
        Self { path: root }
    }

    fn text(&self) -> String {
        self.path.to_string_lossy().into_owned()
    }
}

impl Drop for ToolDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

#[test]
fn resolves_commands_like_shutil_which() {
    let fixture = Fixture::load();
    let env: HashMap<String, String> = std::env::vars().collect();
    let mut checked = 0;
    for row in &fixture.rows {
        if row[0] != "RESOLVE" {
            continue;
        }
        let command = &row[1];
        let expected = unhex(&row[2]);
        assert_eq!(
            resolve_application(command, &env).as_deref(),
            Some(expected.as_str()),
            "resolution of {command}"
        );
        checked += 1;
    }
    assert!(checked >= 1, "the fixture has no RESOLVE rows");
}

#[test]
fn command_line_matches_pywinpty_capture() {
    let fixture = Fixture::load();
    let env: HashMap<String, String> = std::env::vars().collect();
    let plain = ToolDir::new("parity-plain");
    let spaced = ToolDir::new("parity spaced");
    let plain_has_space = plain.text().contains(' ');
    let mut checked = 0;

    for row in &fixture.rows {
        if row[0] != "CASE" {
            continue;
        }
        let name = &row[1];
        let raw_argv0 = unhex(&row[2]);
        let uses_spaced = raw_argv0.contains("<SPACED>");
        let uses_plain = raw_argv0.contains("<PLAIN>");
        assert!(uses_spaced ^ uses_plain, "{name}: exactly one path token");
        if uses_plain && plain_has_space {
            // The plain variant only proves the no-quote case, which needs a path without whitespace.
            continue;
        }
        let dir = if uses_spaced { spaced.text() } else { plain.text() };
        let substitute = |field: &str| field.replace("<SPACED>", &dir).replace("<PLAIN>", &dir);

        let argv0 = substitute(&raw_argv0);
        let expected_app = substitute(&unhex(&row[3]));
        let args: Vec<String> = if row[4].is_empty() {
            Vec::new()
        } else {
            row[4].split('|').map(unhex).collect()
        };
        let expected_line = substitute(&unhex(&row[5]));

        let mut argv = vec![argv0];
        argv.extend(args);
        let (app, line) = match build_command_line(&argv, &env) {
            Ok(result) => result,
            Err(error) => panic!("{name}: {error}"),
        };
        assert_eq!(app, expected_app, "{name}: resolved application");
        assert_eq!(line, expected_line, "{name}: command line");
        checked += 1;
    }
    assert!(checked >= 10, "only {checked} cases were checked");
}
