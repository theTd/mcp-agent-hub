//! §10 "Rust, mechanics": Q11 under a write storm, a slow sink's drop accounting, and a crashed session
//! that leaves its neighbours alone.
//!
//! The sessions here are text-only (`attach_text`), so the pipeline, the ring and the coalescing are
//! exercised without a console; the PTY side is step 1's lifecycle suite.

use std::sync::Arc;
use std::time::Duration;

use conpty_session::{Core, CoreOptions, SessionFlags, TextRequest};
use terminal_stream::QueueSink;

const FEED_DEADLINE: Duration = Duration::from_millis(50);

fn core() -> Arc<Core> {
    Core::new(
        Box::new(|| false),
        Box::new(|_line: &str| {}),
        CoreOptions::default(),
    )
}

fn text_request(id: &str) -> TextRequest {
    TextRequest {
        session_id: id.to_string(),
        cols: 40,
        rows: 6,
        flags: SessionFlags::default(),
        seed_history: None,
        seed: None,
    }
}

/// Q11: a sink that attaches during a write storm sees every chunk exactly once — in the replay it is
/// handed, or in the frames that follow, never both and never neither.
#[test]
fn q11_every_chunk_reaches_each_sink_exactly_once() {
    let core = core();
    core.attach_text(text_request("q11")).expect("attach_text");
    let chunks: Vec<String> = (0..40).map(|index| format!("[{index:04}]")).collect();
    let mut sinks: Vec<(usize, String, Arc<QueueSink>)> = Vec::new();
    for (index, chunk) in chunks.iter().enumerate() {
        core.feed("q11", chunk, FEED_DEADLINE).expect("feed");
        if index % 7 == 3 {
            let sink = Arc::new(QueueSink::new(2 * 1024 * 1024));
            let (replay, _handle) = core
                .attach_sink("q11", sink.clone())
                .expect("attach_sink");
            sinks.push((index, replay, sink));
        }
        std::thread::sleep(Duration::from_millis(6));
    }
    // The death flushes the batch that is still open, so every sink has all of its bytes.
    core.kill("q11", Duration::from_millis(100)).expect("kill");
    assert_eq!(sinks.len(), 6, "the storm attached a sink every seventh chunk");
    // The replay is the whole ring (what a console gets today), so every sink must end up with the whole
    // stream: a chunk in the replay must not arrive again as a frame, and none may be missing.
    let expected = chunks.concat();
    for (index, replay, sink) in &sinks {
        let frames = sink.drain();
        let received = format!("{replay}{}", String::from_utf8(frames).expect("utf-8 frames"));
        assert_eq!(received, expected, "sink attached at chunk {index}");
    }
}

/// §5.4: a sink whose queue cannot hold a frame drops it whole and the drop is counted in the drain
/// record.
#[test]
fn a_slow_sink_drops_whole_frames_and_counts_them() {
    let core = core();
    core.attach_text(text_request("slow")).expect("attach_text");
    let sink = Arc::new(QueueSink::new(8));
    core.attach_sink("slow", sink.clone()).expect("attach_sink");
    let payload = "0123456789abcdef";
    core.feed("slow", payload, FEED_DEADLINE).expect("feed");
    core.kill("slow", Duration::from_millis(100)).expect("kill");
    assert_eq!(sink.dropped_bytes(), payload.len() as u64);
    let record = core
        .drain()
        .into_iter()
        .find(|record| record.session_id == "slow")
        .expect("a death record");
    assert!(!record.alive);
    assert!(
        record.dropped_bytes >= payload.len() as u64,
        "record: {record:?}"
    );
}

/// §6.3: a panic ends one session (`crashed`, `alive = false`, state kept) and nothing else.
#[cfg(feature = "test-hooks")]
#[test]
fn a_crashed_session_leaves_the_others_alive() {
    let core = core();
    core.attach_text(text_request("crashed")).expect("attach_text");
    core.attach_text(text_request("healthy")).expect("attach_text");
    let session = core
        .session_for_test("crashed")
        .expect("the session is in the table");
    session.mark_crashed_for_test("test panic");

    let records = core.drain();
    let crashed = records
        .iter()
        .find(|record| record.session_id == "crashed")
        .expect("the crashed session reports");
    assert!(crashed.crashed && !crashed.alive, "record: {crashed:?}");
    let healthy = records.iter().find(|record| record.session_id == "healthy");
    assert!(
        healthy.map(|record| record.alive).unwrap_or(true),
        "the neighbour must stay alive: {records:?}"
    );

    core.feed("healthy", "still here", FEED_DEADLINE).expect("feed");
    let screen = core
        .screen("healthy", 0, FEED_DEADLINE)
        .expect("screen");
    assert_eq!(screen.text, "still here");
    assert_eq!(
        core.kill("crashed", Duration::from_millis(100)).ok(),
        Some(None),
        "no exit code was ever read"
    );
}
