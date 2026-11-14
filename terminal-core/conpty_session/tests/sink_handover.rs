//! §6.4 A1's deliveries across a resume: sinks belong to the session **id**, not to one incarnation.
//!
//! Every case starts from a session that died and is resumed under the same id, with a sink attached
//! before the death. That sink must see, in order and exactly once: the dead incarnation's last bytes
//! (including a batch still pending at its exit), the resume separator, and the new incarnation's first
//! bytes. Its handle must still detach, and a sink that attaches during the swap must be kept.
//!
//! Most cases use `attach_text`/`feed` (no process, no thread, the same delivery code); the order case
//! uses a real ConPTY spawn, because that is the path whose separator is `ws_prefix`. The hook that
//! holds a coalescing batch is process-wide, so the cases here run one at a time.

use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use conpty_session::{Core, CoreOptions, DrainRecord, SessionFlags, SpawnRequest, TextRequest};
use terminal_stream::FrameSink;

const DEADLINE: Duration = Duration::from_millis(500);

static SERIAL: Mutex<()> = Mutex::new(());

fn serialized() -> MutexGuard<'static, ()> {
    SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn core() -> Arc<Core> {
    core_logging(Arc::new(Mutex::new(Vec::new())))
}

/// A core whose log lines are kept, for the assertions that a retirement is logged once.
fn core_logging(lines: Arc<Mutex<Vec<String>>>) -> Arc<Core> {
    Core::new(
        Box::new(|| false),
        Box::new(move |line: &str| {
            lines
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .push(line.to_string())
        }),
        CoreOptions::default(),
    )
    .expect("core")
}

fn text_request(id: &str, seed: Option<&str>) -> TextRequest {
    TextRequest {
        session_id: id.to_string(),
        cols: 40,
        rows: 6,
        flags: SessionFlags::default(),
        seed_history: None,
        seed: seed.map(str::to_string),
    }
}

fn spawn_request(id: &str, argv: &[&str], ws_prefix: Option<&str>) -> SpawnRequest {
    SpawnRequest {
        session_id: id.to_string(),
        argv: argv.iter().map(|arg| arg.to_string()).collect(),
        cwd: None,
        env: std::env::vars().collect(),
        cols: 100,
        rows: 8,
        flags: SessionFlags::default(),
        seed_history: None,
        ws_prefix: ws_prefix.map(str::to_string),
    }
}

/// A sink that appends every delivered frame to one string: the exact wire text a socket would have
/// written for the session.
#[derive(Default)]
struct RecordingSink {
    bytes: Mutex<Vec<u8>>,
}

impl RecordingSink {
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.bytes.lock().unwrap_or_else(|e| e.into_inner())).into_owned()
    }
}

impl FrameSink for RecordingSink {
    fn deliver(&self, _session_id: &str, frame: &[u8]) {
        self.bytes
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .extend_from_slice(frame);
    }
}

fn wait_dead(core: &Core, id: &str, within: Duration) -> Option<DrainRecord> {
    let end = Instant::now() + within;
    while Instant::now() < end {
        if let Some(record) = core
            .drain()
            .into_iter()
            .find(|record| record.session_id == id && !record.alive)
        {
            return Some(record);
        }
        thread::sleep(Duration::from_millis(5));
    }
    None
}

/// Waits for a sink's wire text to contain `marker`; delivery can trail the `feed` by one coalescing
/// window (5 ms idle), and the ticker's own pass is what flushes it.
fn wait_for_marker(sink: &RecordingSink, marker: &str, within: Duration) -> String {
    let end = Instant::now() + within;
    loop {
        let text = sink.text();
        if text.contains(marker) || Instant::now() >= end {
            return text;
        }
        thread::sleep(Duration::from_millis(5));
    }
}

/// (a) A real spawn resume: the sink attached to incarnation 1 sees its last bytes, then `ws_prefix`,
/// then incarnation 2's output, each marker exactly once and in that order.
#[test]
fn a_spawn_resume_delivers_the_old_bytes_then_the_prefix_then_the_new_output() {
    let _serial = serialized();
    let core = core();
    core.spawn(spawn_request("resume", &["cmd.exe", "/c", "echo OLD"], None))
        .expect("first spawn");
    let sink = Arc::new(RecordingSink::default());
    let (replay, handle) = core
        .attach_sink("resume", sink.clone())
        .expect("attach_sink on incarnation 1");
    assert!(replay.is_empty(), "nothing printed yet: {replay:?}");
    let record = wait_dead(&core, "resume", Duration::from_secs(5)).expect("first death");
    assert_eq!(record.exit_code, Some(0), "record: {record:?}");

    // The resume: a new process for the same id, while the sink is attached to it.
    core.spawn(spawn_request(
        "resume",
        &["cmd.exe", "/c", "echo NEW"],
        Some("PREFIX"),
    ))
    .expect("resume spawn");
    wait_dead(&core, "resume", Duration::from_secs(5)).expect("second death");

    let text = sink.text();
    let old = text.find("OLD").unwrap_or_else(|| panic!("no incarnation 1 output: {text:?}"));
    let prefix = text
        .find("PREFIX")
        .unwrap_or_else(|| panic!("the resume separator never reached the sink: {text:?}"));
    let new = text.find("NEW").unwrap_or_else(|| panic!("no incarnation 2 output: {text:?}"));
    assert!(old < prefix && prefix < new, "wire order: {text:?}");
    assert_eq!(text.matches("OLD").count(), 1, "wire text: {text:?}");
    assert_eq!(text.matches("PREFIX").count(), 1, "wire text: {text:?}");
    assert_eq!(text.matches("NEW").count(), 1, "wire text: {text:?}");

    // (b) The handle was registered on incarnation 1 and belongs to the id, so it detaches against the
    // session that is in the table now.
    assert!(
        core.detach_sink("resume", handle).expect("detach_sink"),
        "a handle registered on incarnation 1 must detach against incarnation 2"
    );
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// (b)+(c) The `attach_text` resume, with the dead incarnation's batch still pending at its exit: the
/// handover delivers it, then the seed; a handle from incarnation 1 detaches against incarnation 2, and
/// nothing reaches that sink afterwards.
#[cfg(feature = "test-hooks")]
#[test]
fn an_attach_text_resume_delivers_the_pending_batch_then_the_seed() {
    use conpty_session::test_hooks::{hold_frames, release_frames};

    let _serial = serialized();
    let core = core();
    // The frame hold keeps v1's batch pending, so the handover's flush is what delivers these bytes.
    hold_frames();
    core.attach_text(text_request("text-resume", None))
        .expect("attach_text incarnation 1");
    let sink = Arc::new(RecordingSink::default());
    let (_replay, handle) = core
        .attach_sink("text-resume", sink.clone())
        .expect("attach_sink");
    core.feed("text-resume", "OLD", DEADLINE)
        .expect("feed into incarnation 1");
    assert!(
        sink.text().is_empty(),
        "the batch is held, so nothing can have been delivered: {:?}",
        sink.text()
    );
    core.kill("text-resume", Duration::from_millis(100))
        .expect("kill incarnation 1");
    assert!(
        sink.text().is_empty(),
        "the held batch is still pending at the exit: {:?}",
        sink.text()
    );

    core.attach_text(text_request("text-resume", Some("PREFIX")))
        .expect("attach_text incarnation 2");
    release_frames();
    core.feed("text-resume", "NEW", DEADLINE)
        .expect("feed into incarnation 2");
    let text = wait_for_marker(&sink, "NEW", Duration::from_secs(2));
    assert_eq!(
        text, "OLDPREFIXNEW",
        "expected the old batch, the seed and the new output, in that order"
    );
    assert!(
        core.detach_sink("text-resume", handle).expect("detach_sink"),
        "a handle registered on incarnation 1 must detach against incarnation 2"
    );

    // Detached means nothing more, however much the session prints: a fresh sink proves the session is
    // still delivering while this one stays at its last byte.
    let later = Arc::new(RecordingSink::default());
    let (replay, later_handle) = core
        .attach_sink("text-resume", later.clone())
        .expect("attach_sink on incarnation 2");
    core.feed("text-resume", "TAIL", DEADLINE)
        .expect("feed TAIL");
    let later_text = wait_for_marker(&later, "TAIL", Duration::from_secs(2));
    assert_eq!(
        sink.text(),
        "OLDPREFIXNEW",
        "a detached sink must not receive anything more"
    );
    assert_eq!(
        format!("{replay}{later_text}"),
        "NEWTAIL",
        "the replay and the frames of a sink attached after the swap are the stream from the swap on {}",
        "(the old incarnation's bytes and the seed went to the sinks attached before it)"
    );
    assert!(core.detach_sink("text-resume", later_handle).expect("detach"));
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// A1's other half: once the successor's banner is out, whatever the old incarnation would still file
/// (B1's wedged reader, here a `feed` straight into the retired object) reaches no sink and is counted
/// in the old incarnation's `dropped_bytes` instead.
#[cfg(feature = "test-hooks")]
#[test]
fn a_superseded_incarnation_drops_its_late_bytes_and_counts_them() {
    let _serial = serialized();
    let log = Arc::new(Mutex::new(Vec::new()));
    let core = core_logging(Arc::clone(&log));
    let superseded_lines = |log: &Arc<Mutex<Vec<String>>>| -> usize {
        log.lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .iter()
            .filter(|line| line.contains(": superseded"))
            .count()
    };
    core.attach_text(text_request("late", None))
        .expect("attach_text incarnation 1");
    let old = core.session_for_test("late").expect("incarnation 1");
    let sink = Arc::new(RecordingSink::default());
    let (_replay, handle) = core.attach_sink("late", sink.clone()).expect("attach_sink");
    core.feed("late", "OLD", DEADLINE).expect("feed OLD");
    core.kill("late", Duration::from_millis(100)).expect("kill");

    core.attach_text(text_request("late", Some("BANNER")))
        .expect("attach_text incarnation 2");
    let before = old.counters().2;
    let logged_before = superseded_lines(&log);
    assert!(logged_before >= 1, "the retirement itself is not logged once: {log:?}");
    // Due, or not: a final flush takes the frame anyway, and `fan_out` is what refuses it.
    for _ in 0..2 {
        old.feed("LATE", DEADLINE).expect("feed into the retired incarnation");
        old.flush_frames(Instant::now() + Duration::from_secs(1));
    }
    let after = old.counters().2;
    assert_eq!(
        after,
        before + 8,
        "the retired incarnation's late frames must be counted in its own dropped_bytes"
    );
    assert_eq!(
        superseded_lines(&log),
        logged_before + 1,
        "the drop is logged once, not per frame: {log:?}"
    );

    core.feed("late", "NEW", DEADLINE).expect("feed NEW");
    let text = wait_for_marker(&sink, "NEW", Duration::from_secs(2));
    assert_eq!(
        text, "OLDBANNERNEW",
        "a superseded incarnation must not write after its successor's banner"
    );
    assert!(
        !text.contains("LATE"),
        "a superseded incarnation delivered a frame after the banner: {text:?}"
    );
    assert!(core.detach_sink("late", handle).expect("detach"));
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// (d) A sink attached during the swap is kept: whatever side of the handover its registration lands
/// on, it gets the stream from the point its replay was taken, once, and it keeps receiving after that.
/// The separator and the old incarnation's bytes can only be in the replay when the registration came
/// first (a socket that connects after the banner does not get it, as `push_ws_frame` behaves today).
#[cfg(feature = "test-hooks")]
#[test]
fn a_sink_attached_during_the_swap_is_kept_exactly_once() {
    let _serial = serialized();
    let core = core();
    core.attach_text(text_request("swap", None))
        .expect("attach_text incarnation 1");
    core.feed("swap", "OLD", DEADLINE).expect("feed OLD");
    core.kill("swap", Duration::from_millis(100)).expect("kill");

    // Attach through incarnation 1's own `Session`, in parallel with the resume, so the registration and
    // the handover can land in either order.
    let old_session = core.session_for_test("swap").expect("incarnation 1");
    let sink = Arc::new(RecordingSink::default());
    let attacher = {
        let sink = Arc::clone(&sink);
        thread::spawn(move || old_session.attach_sink(sink))
    };
    core.attach_text(text_request("swap", Some("PREFIX")))
        .expect("attach_text incarnation 2");
    core.feed("swap", "NEW", DEADLINE).expect("feed NEW");
    let (replay, handle) = attacher.join().expect("the attaching thread");
    let handle = handle.expect("a live id's fan-out registers the sink");
    let mut framed = wait_for_marker(&sink, "NEW", Duration::from_secs(2));
    let whole = format!("{replay}{framed}");
    assert!(
        ["OLDPREFIXNEW", "OLDNEW", "NEW"].contains(&whole.as_str()),
        "replay {replay:?} + frames {framed:?}: the sink must see the stream from its replay on, once"
    );

    // Kept: it receives what the id prints after the swap too, and nothing twice.
    core.feed("swap", "TAIL", DEADLINE).expect("feed TAIL");
    framed = wait_for_marker(&sink, "TAIL", Duration::from_secs(2));
    let after = format!("{replay}{framed}");
    assert!(
        after.ends_with("TAIL"),
        "the sink attached during the swap stopped receiving: {after:?}"
    );
    let mut seen: Vec<&str> = Vec::new();
    for marker in ["OLD", "PREFIX", "NEW", "TAIL"] {
        let count = after.matches(marker).count();
        assert!(count <= 1, "{marker} arrived {count} times: {after:?}");
        if count == 1 {
            seen.push(marker);
        }
    }
    assert!(
        seen.ends_with(&["NEW", "TAIL"]),
        "the sink attached during the swap must see the new incarnation's output and what follows it, \
         in order: {seen:?} of {after:?}"
    );
    assert!(
        core.detach_sink("swap", handle).expect("detach_sink"),
        "a sink registered through incarnation 1 must detach against incarnation 2"
    );
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}