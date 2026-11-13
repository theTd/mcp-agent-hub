//! Command-line parity against what pywinpty 3.1.2 actually produced.
//!
//! `tests/fixtures/cmdline_parity.tsv` has two row kinds:
//!
//! * `PATHEXT <hex>` and `RESOLVE <command> <hex path>` come from `tests/capture_cmdline_parity.py`,
//!   which runs `winpty.ptyprocess.which` (the function pywinpty's `PtyProcess.spawn` resolves `argv[0]`
//!   with) over a stub layout of `.exe`/`.cmd`/`.bat`/no-extension files with a controlled `PATH` and
//!   `PATHEXT`. The stub directory is stored as `<TOOLS>`, so no absolute path from the capture machine
//!   is in the tree and the test recreates the same layout here.
//! * `CASE <name> <hex argv0> <hex resolved app> <hex arg>|... <hex command line>` are `GetCommandLineW`
//!   captures from a real pywinpty child, with the probe directory stored as `<PLAIN>` / `<SPACED>`; the
//!   test recreates both directories, so these rows are machine-independent as well.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use conpty_session::command_line::{build_command_line, resolve_with};

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

    fn row(&self, kind: &str) -> Option<&Vec<String>> {
        self.rows.iter().find(|row| row[0] == kind)
    }

    fn of_kind<'a>(&'a self, kind: &'a str) -> impl Iterator<Item = &'a Vec<String>> {
        self.rows.iter().filter(move |row| row[0] == kind)
    }
}

fn unhex(text: &str) -> String {
    let bytes: Vec<u8> = (0..text.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&text[index..index + 2], 16).expect("hex"))
        .collect();
    String::from_utf8(bytes).expect("utf-8")
}

/// Creates a tool directory holding the files the capture's stub layout had, and removes it on drop.
struct ToolDir {
    path: PathBuf,
}

impl ToolDir {
    fn new(name: &str, files: &[&str]) -> Self {
        let root = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("create tool dir");
        for file in files {
            std::fs::write(root.join(file), b"").expect("stub file");
        }
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

/// `shutil.which`'s rule over the captured `PATH`/`PATHEXT`: `<TOOLS>` is this test's tool directory.
#[test]
fn resolves_commands_like_pywinpty_on_a_controlled_layout() {
    let fixture = Fixture::load();
    let pathext_row = fixture
        .row("PATHEXT")
        .expect("the fixture has no PATHEXT row");
    let pathext: Vec<String> = unhex(&pathext_row[1])
        .split(';')
        .filter(|ext| !ext.is_empty())
        .map(|ext| ext.trim_end_matches('.').to_string())
        .collect();
    let tools = ToolDir::new("parity tools", &["probe.exe", "probe.cmd", "probe.bat", "bare"]);
    let tools_text = tools.text();

    let mut checked = 0;
    for row in fixture.of_kind("RESOLVE") {
        let command = &row[1];
        let expected = if row[2].is_empty() {
            None
        } else {
            Some(unhex(&row[2]).replace("<TOOLS>", &tools_text))
        };
        assert_eq!(
            resolve_with(command, Some(&tools_text), &pathext),
            expected,
            "resolution of {command}"
        );
        checked += 1;
    }
    assert!(checked >= 4, "only {checked} RESOLVE rows were checked");

    // A session env's PATH is the only input the caller controls; the resolver must not add this
    // process's PATH behind it.
    let session_path = format!("Z:\\no-such-dir;{tools_text}");
    assert_eq!(
        resolve_with("probe", Some(&session_path), &pathext),
        Some(format!("{tools_text}\\probe.EXE")),
        "the second PATH entry must be searched, and no process PATH entry added"
    );
}

#[test]
fn command_line_matches_pywinpty_capture() {
    let fixture = Fixture::load();
    let env: HashMap<String, String> = std::env::vars().collect();
    let plain = ToolDir::new("parity-plain", &["cmdline_probe.exe"]);
    let spaced = ToolDir::new("parity spaced", &["cmdline_probe.exe"]);
    let plain_has_space = plain.text().contains(' ');
    let mut checked = 0;

    for row in fixture.of_kind("CASE") {
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
    assert!(checked >= 10, "only {checked} CASE rows were checked");
}