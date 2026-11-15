//! The frozen corpus, case for case: the gate of doc §9 step 2.
//!
//! Every folder under `tools/fixtures/terminal/` that holds a `meta.json` is a case, and the harness is
//! data-driven: both chunk encodings are read (`text`, and `utf8_bytes` as base64 of one 8 KiB read),
//! both case shapes the renderer knows are handled, and a new folder drops in without a code change.
//!
//! Byte-identical is required for `raw.txt`, `raw_tail.txt`, `replay.txt`, the `resumed/` variants, the
//! W8, W9, OSC and UTF-8 cases and `trigger.json`'s counts. The three rendered files (`screen_final.txt`,
//! `screen_at_resize.txt`, `scrollback_250.txt`) are compared per row against `screen_exceptions.jsonl`:
//! a difference not listed there fails the test, a listed difference that no longer occurs fails it too.
//!
//! A golden a case's kind and `meta.json` call for must exist: a missing one is a failure, not a skip
//! (step 2c, review F10), so a dropped file cannot remove coverage silently.

mod json;

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use base64::Engine;
use terminal_stream::{
    strip_terminal_replies, trim_history_text, StreamOptions, TerminalStream,
};

const FIXTURES: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tools/fixtures/terminal");
const EXCEPTIONS: &str =
    concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixture_corpus/screen_exceptions.jsonl");
/// `get_clean_screen_text`'s default (design §4: 80 lines).
const SCREEN_LINES: usize = 80;
/// `persist_session`/`snapshot_all` read 250 rows.
const SCROLLBACK_LINES: usize = 250;
/// `_capture_submit_diagnostic`'s tail is a character count: `"".join(raw_history)[-65536:]`.
const RAW_TAIL_CHARS: usize = 65536;
const HISTORY_ROWS: usize = 3000;

fn resume_banner(resume_count: usize) -> String {
    // `session_manager.resume_banner`; `test_terminal_fixtures` pins the hub's own source text.
    format!("\r\n\x1b[90m── 已恢复（第 {resume_count} 次）──\x1b[0m\r\n")
}

enum Chunk {
    Text(String),
    Bytes(Vec<u8>),
}

impl Chunk {
    fn len(&self) -> usize {
        match self {
            Chunk::Text(text) => text.len(),
            Chunk::Bytes(bytes) => bytes.len(),
        }
    }

    fn feed(&self, stream: &mut TerminalStream) {
        let mut out = Vec::new();
        match self {
            // A `text` chunk is already exactly what `pty.read` returned; its UTF-8 bytes are the same
            // bytes the pipeline decodes, so both encodings take one path.
            Chunk::Text(text) => stream.feed_bytes(text.as_bytes(), &mut out),
            Chunk::Bytes(bytes) => stream.feed_bytes(bytes, &mut out),
        }
    }
}

struct Case {
    dir: PathBuf,
    name: String,
    meta: json::Value,
    chunks: Vec<Chunk>,
}

impl Case {
    fn load(dir: &Path) -> Case {
        let name = dir
            .file_name()
            .and_then(|name| name.to_str())
            .expect("case directory name")
            .to_string();
        let meta = json::parse(&read_exact(&dir.join("meta.json"))).expect("meta.json parses");
        let encoding = meta
            .get("chunk_encoding")
            .and_then(|value| value.as_str())
            .unwrap_or("text");
        let records = read_exact(&dir.join("chunks.jsonl"));
        let mut chunks = Vec::new();
        for line in records.lines().filter(|line| !line.trim().is_empty()) {
            let record = json::parse(line).expect("a chunks.jsonl line parses");
            let payload = record
                .get("chunk")
                .and_then(|value| value.as_str())
                .expect("a chunk record has its text");
            chunks.push(match encoding {
                "text" => Chunk::Text(payload.to_string()),
                "utf8_bytes" => Chunk::Bytes(
                    base64::engine::general_purpose::STANDARD
                        .decode(payload)
                        .expect("base64 chunk"),
                ),
                other => panic!("{name}: unknown chunk_encoding {other:?}"),
            });
        }
        Case {
            dir: dir.to_path_buf(),
            name,
            meta,
            chunks,
        }
    }

    fn kind(&self) -> &str {
        self.meta
            .get("kind")
            .and_then(|value| value.as_str())
            .unwrap_or("session")
    }

    fn number(&self, key: &str, default: usize) -> usize {
        self.meta
            .get(key)
            .and_then(|value| value.as_usize())
            .unwrap_or(default)
    }

    fn cols(&self) -> usize {
        self.number("cols", 120)
    }

    fn rows(&self) -> usize {
        self.number("rows", 36)
    }

    fn resume_count(&self) -> usize {
        self.number("resume_count", 1)
    }

    /// `resize`: `{at_ms, chunk_index, from, to}`; the screen is captured *after* the resize.
    fn resize(&self) -> Option<(usize, usize, usize)> {
        let resize = self.meta.get("resize")?;
        let index = resize.get("chunk_index")?.as_usize()?;
        let to = resize.get("to")?.as_array()?;
        Some((index, to.first()?.as_usize()?, to.get(1)?.as_usize()?))
    }

    fn stream(&self, resume_count: usize, seed: Option<&str>) -> TerminalStream {
        let mut stream = TerminalStream::new(StreamOptions {
            cols: self.cols(),
            rows: self.rows(),
            history_rows: HISTORY_ROWS,
            strip_esc3j_on_output: resume_count > 0,
            strip_osc_color_queries: true,
            ..StreamOptions::default()
        });
        if let Some(seed) = seed {
            stream.seed_history(seed);
        }
        stream
    }
}

fn read_exact(path: &Path) -> String {
    let bytes = fs::read(path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    String::from_utf8(bytes).unwrap_or_else(|error| panic!("{}: not UTF-8: {error}", path.display()))
}

fn case_dirs(root: &Path) -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = fs::read_dir(root)
        .unwrap_or_else(|error| panic!("{}: {error}", root.display()))
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.is_dir() && path.join("meta.json").is_file())
        .collect();
    dirs.sort();
    dirs
}

/// One accepted pyte-vs-model difference: exactly one row of one golden.
struct Exception {
    case: String,
    file: String,
    row: usize,
    pyte: String,
    rust: String,
    reason: String,
    used: bool,
}

fn load_exceptions() -> Vec<Exception> {
    let path = Path::new(EXCEPTIONS);
    if !path.is_file() {
        return Vec::new();
    }
    read_exact(path)
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| {
            let record = json::parse(line).expect("an exception line parses");
            let text = |key: &str| {
                record
                    .get(key)
                    .and_then(|value| value.as_str())
                    .unwrap_or_default()
                    .to_string()
            };
            Exception {
                case: text("case"),
                file: text("file"),
                row: record
                    .get("row")
                    .and_then(|value| value.as_usize())
                    .expect("an exception names its row"),
                pyte: text("pyte"),
                rust: text("rust"),
                reason: text("reason"),
                used: false,
            }
        })
        .collect()
}

fn finish(stream: &mut TerminalStream) {
    let mut out = Vec::new();
    stream.finish(&mut out);
}

fn compare(
    case: &str,
    file: &str,
    expected: &str,
    actual: &str,
    exceptions: &mut [Exception],
    failures: &mut Vec<String>,
) {
    if expected == actual {
        return;
    }
    let expected_rows: Vec<&str> = expected.split('\n').collect();
    let actual_rows: Vec<&str> = actual.split('\n').collect();
    let mut unmatched = Vec::new();
    for row in 0..expected_rows.len().max(actual_rows.len()) {
        let expected_row = expected_rows.get(row).copied().unwrap_or("<missing row>");
        let actual_row = actual_rows.get(row).copied().unwrap_or("<missing row>");
        if expected_row == actual_row {
            continue;
        }
        let accepted = exceptions.iter_mut().find(|exception| {
            !exception.used
                && exception.case == case
                && exception.file == file
                && exception.row == row
                && exception.pyte == expected_row
                && exception.rust == actual_row
        });
        match accepted {
            Some(exception) => {
                exception.used = true;
                eprintln!(
                    "accepted {case}/{file} row {row}: {} (pyte {expected_row:?}, model {actual_row:?})",
                    exception.reason
                );
            }
            None => unmatched.push(format!(
                "  row {row}: pyte {expected_row:?} / model {actual_row:?}"
            )),
        }
    }
    if !unmatched.is_empty() {
        failures.push(format!(
            "{case}/{file}: {} unexplained row difference(s)\n{}",
            unmatched.len(),
            unmatched.join("\n")
        ));
    }
}

fn golden(case: &Case, name: &str) -> Option<String> {
    let path = case.dir.join(name);
    if path.is_file() {
        Some(read_exact(&path))
    } else {
        None
    }
}

fn session_goldens(
    case: &Case,
    prefix: &str,
    stream: &TerminalStream,
    at_resize: Option<String>,
    failures: &mut Vec<String>,
    exceptions: &mut [Exception],
) {
    let mut compare_file = |name: String, actual: String, failures: &mut Vec<String>| {
        let file = format!("{prefix}{name}");
        match golden(case, &file) {
            Some(expected) => compare(&case.name, &file, &expected, &actual, exceptions, failures),
            None => failures.push(format!("{}/{}: golden missing", case.name, file)),
        }
    };
    compare_file(
        "screen_final.txt".to_string(),
        stream.screen_text(SCREEN_LINES),
        failures,
    );
    compare_file(
        "scrollback_250.txt".to_string(),
        stream.scrollback_text(SCROLLBACK_LINES),
        failures,
    );
    if let Some(screen) = at_resize {
        compare_file("screen_at_resize.txt".to_string(), screen, failures);
    }
    compare_file("raw.txt".to_string(), stream.raw_text(), failures);
    compare_file(
        "raw_tail.txt".to_string(),
        stream.raw_tail(RAW_TAIL_CHARS),
        failures,
    );
    compare_file("replay.txt".to_string(), stream.replay_text(), failures);
}

fn run_session_case(case: &Case, failures: &mut Vec<String>, exceptions: &mut [Exception]) {
    let mut stream = case.stream(0, None);
    let resize = case.resize();
    let mut at_resize = None;
    for (index, chunk) in case.chunks.iter().enumerate() {
        if let Some((at, cols, rows)) = resize {
            if index == at {
                stream.resize(cols, rows);
                at_resize = Some(stream.screen_text(SCREEN_LINES));
            }
        }
        chunk.feed(&mut stream);
    }
    finish(&mut stream);
    if let Some((_, cols, rows)) = resize {
        if at_resize.is_none() {
            stream.resize(cols, rows);
            at_resize = Some(stream.screen_text(SCREEN_LINES));
        }
    }
    session_goldens(case, "", &stream, at_resize, failures, exceptions);

    // A resumed pane replays the archived (trimmed) output, then the separator, and W8 is on from the
    // first byte. `render_case` renders `resumed/` from exactly these two inputs.
    // Every session case has its `resumed/` goldens (`render_case` renders them unconditionally) and a
    // missing one is a failure, not a skip.
    {
        let count = case.resume_count();
        let seed = format!("{}{}", stream.raw_text(), resume_banner(count));
        let mut resumed = case.stream(count, Some(&seed));
        let mut resumed_at_resize = None;
        for (index, chunk) in case.chunks.iter().enumerate() {
            if let Some((at, cols, rows)) = resize {
                if index == at {
                    resumed.resize(cols, rows);
                    resumed_at_resize = Some(resumed.screen_text(SCREEN_LINES));
                }
            }
            chunk.feed(&mut resumed);
        }
        finish(&mut resumed);
        if let Some((_, cols, rows)) = resize {
            if resumed_at_resize.is_none() {
                resumed.resize(cols, rows);
                resumed_at_resize = Some(resumed.screen_text(SCREEN_LINES));
            }
        }
        session_goldens(
            case,
            "resumed/",
            &resumed,
            resumed_at_resize,
            failures,
            exceptions,
        );
    }
}

/// The trigger case's corpus, regenerated from `meta.generator` (`render_terminal_fixtures.generated_chunks`).
fn generated_chunks(generator: &json::Value, name: &str) -> Vec<String> {
    let template = generator
        .get("line_format")
        .and_then(|value| value.as_str())
        .expect("a generator has its line template");
    if !template.contains("{i:05d}") {
        panic!("{name}: the harness knows the {{i:05d}} placeholder only, got {template:?}");
    }
    let terminator = generator
        .get("line_terminator")
        .and_then(|value| value.as_str())
        .unwrap_or("\r\n");
    let first = generator
        .get("first_index")
        .and_then(|value| value.as_usize())
        .unwrap_or(0);
    let count = generator
        .get("count")
        .and_then(|value| value.as_usize())
        .expect("a generator has a count");
    let chunk_bytes = generator
        .get("chunk_bytes")
        .and_then(|value| value.as_usize())
        .unwrap_or(4096);
    let mut chunks = Vec::new();
    let mut pending = String::new();
    for index in first..first + count {
        let line = format!("{}{}", template.replace("{i:05d}", &format!("{index:05}")), terminator);
        let size = line.len();
        if !pending.is_empty() && pending.len() + size > chunk_bytes {
            chunks.push(std::mem::take(&mut pending));
        }
        pending.push_str(&line);
    }
    if !pending.is_empty() {
        chunks.push(pending);
    }
    chunks
}

fn run_trigger_case(case: &Case, failures: &mut Vec<String>) {
    let seed_spec = case.meta.get("seed");
    let archived = seed_spec
        .and_then(|seed| seed.get("archived_repeat"))
        .and_then(|value| value.as_str())
        .unwrap_or("")
        .repeat(
            seed_spec
                .and_then(|seed| seed.get("archived_count"))
                .and_then(|value| value.as_usize())
                .unwrap_or(0),
        );
    let seed = format!("{archived}{}", resume_banner(case.resume_count()));
    let generated = generated_chunks(
        case.meta.get("generator").expect("a trigger case has its generator"),
        &case.name,
    );
    let recorded: Vec<String> = case
        .chunks
        .iter()
        .map(|chunk| match chunk {
            Chunk::Text(text) => text.clone(),
            Chunk::Bytes(bytes) => String::from_utf8(bytes.clone()).expect("a text corpus"),
        })
        .collect();
    if generated != recorded {
        failures.push(format!(
            "{}/chunks.jsonl: the generator block no longer reproduces the file ({} vs {} chunks)",
            case.name,
            generated.len(),
            recorded.len()
        ));
        return;
    }
    let mut stream = case.stream(case.resume_count(), Some(&seed));
    let mut fired_at: Option<usize> = None;
    let mut record = json::Value::Null;
    for (index, text) in generated.iter().enumerate() {
        let entries_before = stream.raw_entries();
        let bytes_before = stream.raw_bytes();
        let mut out = Vec::new();
        stream.feed_bytes(text.as_bytes(), &mut out);
        if fired_at.is_none() && stream.raw_entries() < entries_before {
            fired_at = Some(index);
            let mut map = std::collections::BTreeMap::new();
            map.insert("fired_at_chunk".to_string(), json::Value::Number(index as f64));
            map.insert(
                "entries_before".to_string(),
                json::Value::Number(entries_before as f64),
            );
            map.insert(
                "entries_after".to_string(),
                json::Value::Number(stream.raw_entries() as f64),
            );
            map.insert(
                "bytes_before".to_string(),
                json::Value::Number(bytes_before as f64),
            );
            map.insert(
                "bytes_after".to_string(),
                json::Value::Number(stream.raw_bytes() as f64),
            );
            map.insert("max_bytes".to_string(), json::Value::Number(524288.0));
            map.insert("slack_bytes".to_string(), json::Value::Number(65536.0));
            record = json::Value::Object(map);
        }
    }
    finish(&mut stream);
    // The trigger case's `raw.txt` is the joined ring (already collapsed), not `_scrollback_blob`'s trim.
    match golden(case, "raw.txt") {
        Some(expected) => compare(&case.name, "raw.txt", &expected, stream.raw_join(), &mut [], failures),
        None => failures.push(format!("{}/raw.txt: golden missing", case.name)),
    }
    match golden(case, "raw_tail.txt") {
        Some(expected) => compare(
            &case.name,
            "raw_tail.txt",
            &expected,
            &stream.raw_tail(RAW_TAIL_CHARS),
            &mut [],
            failures,
        ),
        None => failures.push(format!("{}/raw_tail.txt: golden missing", case.name)),
    }
    let Some(expected) = golden(case, "trigger.json") else {
        failures.push(format!("{}/trigger.json: golden missing", case.name));
        return;
    };
    let expected = json::parse(&expected).expect("trigger.json parses");
    let referred = [
        "fired_at_chunk",
        "entries_before",
        "entries_after",
        "bytes_before",
        "bytes_after",
        "max_bytes",
        "slack_bytes",
    ];
    for key in referred {
        let want = expected.get(key).and_then(|value| value.as_usize());
        let got = record.get(key).and_then(|value| value.as_usize());
        if want != got {
            failures.push(format!(
                "{}/trigger.json: {key} expected {:?}, got {:?}",
                case.name, want, got
            ));
        }
    }
    let final_entries = expected
        .get("final_entries")
        .and_then(|value| value.as_usize());
    if final_entries != Some(stream.raw_entries()) {
        failures.push(format!(
            "{}/trigger.json: final_entries expected {:?}, got {}",
            case.name,
            final_entries,
            stream.raw_entries()
        ));
    }
    let final_bytes = expected.get("final_bytes").and_then(|value| value.as_usize());
    if final_bytes != Some(stream.raw_bytes() as usize) {
        failures.push(format!(
            "{}/trigger.json: final_bytes expected {:?}, got {}",
            case.name,
            final_bytes,
            stream.raw_bytes()
        ));
    }
    if fired_at.is_none() {
        failures.push(format!("{}/trigger.json: the ring never collapsed", case.name));
    }
}

fn joined_text(case: &Case) -> String {
    let mut text = String::new();
    for chunk in &case.chunks {
        match chunk {
            Chunk::Text(part) => text.push_str(part),
            Chunk::Bytes(bytes) => {
                text.push_str(std::str::from_utf8(bytes).expect("a text corpus"))
            }
        }
    }
    text
}

fn main_test() {
    let root = Path::new(FIXTURES);
    let mut exceptions = load_exceptions();
    let mut failures = Vec::new();
    let mut checked = 0;
    for dir in case_dirs(root) {
        let case = Case::load(&dir);
        checked += 1;
        match case.kind() {
            "session" => run_session_case(&case, &mut failures, &mut exceptions),
            "trigger" => run_trigger_case(&case, &mut failures),
            "trim" => {
                let max_bytes = case.number("max_bytes", 512 * 1024);
                let actual = trim_history_text(&joined_text(&case), max_bytes);
                match golden(&case, "trimmed.txt") {
                    Some(expected) => {
                        compare(&case.name, "trimmed.txt", &expected, &actual, &mut [], &mut failures)
                    }
                    None => failures.push(format!("{}/trimmed.txt: golden missing", case.name)),
                }
            }
            "input" => {
                let mut actual = String::new();
                for chunk in &case.chunks {
                    let text = match chunk {
                        Chunk::Text(text) => text.as_str(),
                        Chunk::Bytes(bytes) => std::str::from_utf8(bytes).expect("a text corpus"),
                    };
                    actual.push_str(&strip_terminal_replies(text));
                }
                match golden(&case, "stripped.txt") {
                    Some(expected) => {
                        compare(&case.name, "stripped.txt", &expected, &actual, &mut [], &mut failures)
                    }
                    None => failures.push(format!("{}/stripped.txt: golden missing", case.name)),
                }
            }
            other => failures.push(format!("{}/meta.json: unknown kind {other:?}", case.name)),
        }
    }
    for exception in &exceptions {
        if !exception.used {
            failures.push(format!(
                "{}/{}: stale screen exception at row {} ({})",
                exception.case, exception.file, exception.row, exception.reason
            ));
        }
    }
    assert!(checked >= 20, "the corpus lost cases: {checked} found");
    assert!(
        failures.is_empty(),
        "{} fixture difference(s):\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn the_corpus_matches_the_hub() {
    main_test();
}

/// Doc §9 step 2: parse throughput of the largest live case, 20 passes on this thread.
#[test]
fn parse_throughput_of_the_largest_live_case() {
    let root = Path::new(FIXTURES);
    let mut largest: Option<Case> = None;
    let mut largest_bytes = 0;
    for dir in case_dirs(root) {
        let case = Case::load(&dir);
        if case.kind() != "session" || !case.name.starts_with("live-") {
            continue;
        }
        let bytes: usize = case.chunks.iter().map(|chunk| chunk.len()).sum();
        if bytes > largest_bytes {
            largest_bytes = bytes;
            largest = Some(case);
        }
    }
    let case = largest.expect("the corpus still has its live recordings");
    // At least 20 passes, and enough more to keep the measurement from being a stopwatch tick.
    const MIN_PASSES: usize = 20;
    const MIN_SECONDS: f64 = 0.25;
    let mut stream = case.stream(0, None);
    let started = Instant::now();
    let mut passes = 0usize;
    loop {
        for chunk in &case.chunks {
            chunk.feed(&mut stream);
        }
        passes += 1;
        if passes >= MIN_PASSES && started.elapsed().as_secs_f64() >= MIN_SECONDS {
            break;
        }
    }
    finish(&mut stream);
    let elapsed = started.elapsed();
    let megabytes = (largest_bytes * passes) as f64 / (1024.0 * 1024.0);
    println!(
        "parse throughput: {} x{passes} = {megabytes:.2} MiB in {:?} -> {:.1} MiB/s ({} chunks/pass, {} bytes/pass)",
        case.name,
        elapsed,
        megabytes / elapsed.as_secs_f64(),
        case.chunks.len(),
        largest_bytes
    );
}
