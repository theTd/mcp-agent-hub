//! §5.4's order guarantee for one sink (review F5) and Q11's atomic attach (review F6).
//!
//! The reproductions these replace measured the two failures directly: a sink whose first `deliver`
//! takes 120 ms while another producer hands it a frame, and an attach that races an ingest between its
//! replay snapshot and its registration.

// `AtomicBool`/`Ordering` are only reached by the `test-hooks` attach case below.
#[cfg(feature = "test-hooks")]
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Duration;

use conpty_session::{Core, CoreOptions, SessionFlags, TextRequest};
use terminal_stream::FrameSink;

const FEED_DEADLINE: Duration = Duration::from_millis(500);

fn core() -> Arc<Core> {
    Core::new(
        Box::new(|| false),
        Box::new(|_line: &str| {}),
        CoreOptions::default(),
    )
    .expect("core")
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

/// A sink that records every delivery: how many were inside `deliver` at once, the order frames entered
/// and left, and the bytes in the order the frames left. `slow` widens the window a racing producer
/// needs, as a socket blocked on its own send would; `entered` is signalled on every entry so a test can
/// act while a delivery is in flight.
struct RecordingSink {
    slow: Duration,
    inside: Mutex<usize>,
    started: Condvar,
    delivered: Mutex<Recorder>,
}

#[derive(Default)]
struct Recorder {
    in_flight: usize,
    max_in_flight: usize,
    entered: Vec<String>,
    exited: Vec<String>,
    bytes: Vec<u8>,
}

impl RecordingSink {
    fn new(slow: Duration) -> Self {
        Self {
            slow,
            inside: Mutex::new(0),
            started: Condvar::new(),
            delivered: Mutex::new(Recorder::default()),
        }
    }

    /// The bytes delivered so far, as one string (the wire a socket would have written).
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.delivered.lock().unwrap_or_else(|e| e.into_inner()).bytes)
            .into_owned()
    }

    fn text_len(&self) -> usize {
        self.delivered
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .bytes
            .len()
    }

    /// Blocks until at least one delivery is inside `deliver`; `false` on the timeout.
    fn wait_until_delivering(&self, timeout: Duration) -> bool {
        let mut inside = self.inside.lock().unwrap_or_else(|e| e.into_inner());
        let end = std::time::Instant::now() + timeout;
        while *inside == 0 {
            let left = end.saturating_duration_since(std::time::Instant::now());
            if left.is_zero() {
                return false;
            }
            inside = self
                .started
                .wait_timeout(inside, left)
                .map(|(guard, _)| guard)
                .unwrap_or_else(|e| e.into_inner().0);
        }
        true
    }
}

impl FrameSink for RecordingSink {
    fn deliver(&self, _session_id: &str, frame: &[u8]) {
        let text = String::from_utf8_lossy(frame).into_owned();
        {
            let mut recorder = self.delivered.lock().unwrap_or_else(|e| e.into_inner());
            recorder.in_flight += 1;
            recorder.max_in_flight = recorder.max_in_flight.max(recorder.in_flight);
            recorder.entered.push(text.clone());
        }
        {
            let mut inside = self.inside.lock().unwrap_or_else(|e| e.into_inner());
            *inside += 1;
            self.started.notify_all();
        }
        if !self.slow.is_zero() {
            thread::sleep(self.slow);
        }
        {
            let mut inside = self.inside.lock().unwrap_or_else(|e| e.into_inner());
            *inside -= 1;
        }
        let mut recorder = self.delivered.lock().unwrap_or_else(|e| e.into_inner());
        recorder.in_flight -= 1;
        recorder.exited.push(text);
        recorder.bytes.extend_from_slice(frame);
    }
}

/// F5: two producers reach one sink — the ticker's `flush_frames` and the reader's 32 KiB trigger inside
/// `ingest`. While the first frame is inside `deliver`, the second must not be handed over: no two frames
/// overlap, and none leaves before the one that entered first.
#[test]
fn a_sink_never_sees_two_frames_at_once_or_out_of_order() {
    let core = core();
    core.attach_text(text_request("order")).expect("attach_text");
    let sink = Arc::new(RecordingSink::new(Duration::from_millis(120)));
    core.attach_sink("order", sink.clone()).expect("attach_sink");

    // Small enough that only the ticker's idle window can take it: the ticker is producer one.
    let first = "AAA1";
    core.feed("order", first, FEED_DEADLINE).expect("feed");
    assert!(
        sink.wait_until_delivering(Duration::from_secs(5)),
        "the ticker never delivered the first frame"
    );
    // Producer two: a frame the 32 KiB trigger takes from inside `ingest`, at the same moment.
    let second = format!("BBB2{}", "b".repeat(32 * 1024 - 4));
    core.feed("order", &second, FEED_DEADLINE).expect("feed");
    core.kill("order", Duration::from_millis(100)).expect("kill");

    let recorder = sink.delivered.lock().unwrap_or_else(|e| e.into_inner());
    assert_eq!(
        recorder.max_in_flight, 1,
        "two deliveries overlapped: entered={:?}",
        recorder
            .entered
            .iter()
            .map(|frame| &frame[..4])
            .collect::<Vec<_>>()
    );
    assert_eq!(
        recorder.exited, recorder.entered,
        "a frame left before the one that entered first"
    );
    let received = String::from_utf8_lossy(&recorder.bytes).into_owned();
    assert_eq!(received, format!("{first}{second}"));
    assert_eq!(
        recorder.entered.len(),
        2,
        "the seed and the 32 KiB chunk are two frames: {:?}",
        recorder
            .entered
            .iter()
            .map(|frame| frame.len())
            .collect::<Vec<_>>()
    );
}

/// F6/Q11: a sink that attaches while the stream is running sees every byte exactly once — in the replay
/// it is handed, or in the frames that follow, never both and never neither.
///
/// The window the review hit by chance is opened on purpose here: `attach_sink` sleeps 20 ms between the
/// replay snapshot and the registration, so a chunk that the reader delivers inside that window is in
/// neither unless the delivery lock covers both steps. A second thread attaches and detaches throwaway
/// sinks the whole time, as a console reconnect storm would.
#[cfg(feature = "test-hooks")]
#[test]
fn attach_sink_takes_its_replay_and_registers_in_one_step() {
    use conpty_session::test_hooks::set_attach_delay;

    let core = core();
    core.attach_text(text_request("attach")).expect("attach_text");
    let chunks: Vec<String> = (0..80).map(|index| format!("[{index:04}]")).collect();

    let stop = Arc::new(AtomicBool::new(false));
    let feeder = {
        let core = Arc::clone(&core);
        let chunks = chunks.clone();
        let stop = Arc::clone(&stop);
        thread::spawn(move || {
            for chunk in &chunks {
                if stop.load(Ordering::Relaxed) {
                    return;
                }
                core.feed("attach", chunk, FEED_DEADLINE).expect("feed");
                thread::sleep(Duration::from_millis(2));
            }
        })
    };

    let mut sinks: Vec<(String, Arc<RecordingSink>)> = Vec::new();
    for _ in 0..12 {
        let sink = Arc::new(RecordingSink::new(Duration::ZERO));
        set_attach_delay(Duration::from_millis(20));
        let (replay, _handle) = core.attach_sink("attach", sink.clone()).expect("attach_sink");
        set_attach_delay(Duration::ZERO);
        sinks.push((replay, sink));
        thread::sleep(Duration::from_millis(30));
    }
    feeder.join().expect("the feeder thread did not panic");
    stop.store(true, Ordering::Relaxed);
    core.kill("attach", Duration::from_millis(100)).expect("kill");

    let expected = chunks.concat();
    assert_eq!(sinks.len(), 12);
    for (index, (replay, sink)) in sinks.iter().enumerate() {
        let recorder = sink.delivered.lock().unwrap_or_else(|e| e.into_inner());
        let framed = String::from_utf8_lossy(&recorder.bytes).into_owned();
        let received = format!("{replay}{framed}");
        assert_eq!(
            received.len(),
            expected.len(),
            "sink attached in round {index}: replay {} + framed {} vs expected {}",
            replay.len(),
            framed.len(),
            expected.len()
        );
        assert_eq!(received, expected, "sink attached in round {index}");
    }
}
/// A sink that panics on its first delivery: step 4's socket sink with a bug in it.
struct PanickingSink {
    delivered: Mutex<usize>,
}

impl FrameSink for PanickingSink {
    fn deliver(&self, _session_id: &str, _frame: &[u8]) {
        let mut delivered = self.delivered.lock().unwrap_or_else(|e| e.into_inner());
        *delivered += 1;
        panic!("sink out of order");
    }
}

/// §6.3 + review finding 2: one sink's panic costs that sink, never the session. The reader's own path
/// (`feed`, which calls `ingest` and then `pump`) must survive it: the session stays alive and keeps
/// taking text, the healthy sink gets every byte in order, the panicked handle is gone so `detach_sink`
/// answers `false`, a sink attached afterwards registers and receives, and the panic counter rose by one.
#[test]
fn a_panicking_sink_costs_that_sink_and_not_the_session() {
    let core = core();
    core.attach_text(text_request("panic")).expect("attach_text");
    let healthy = Arc::new(RecordingSink::new(Duration::ZERO));
    let (_healthy_replay, healthy_handle) = core
        .attach_sink("panic", healthy.clone())
        .expect("attach_sink healthy");
    let bad = Arc::new(PanickingSink {
        delivered: Mutex::new(0),
    });
    let (_bad_replay, panic_handle) = core
        .attach_sink("panic", bad.clone())
        .expect("attach_sink bad");
    let before = core.stats(FEED_DEADLINE).panics;

    core.feed("panic", "AAA", FEED_DEADLINE).expect("feed");
    // Delivery is coalesced, so drive the ticker's own pass too; `pump` is what runs the fan-out.
    let end = std::time::Instant::now() + Duration::from_secs(2);
    while healthy.text_len() < 3 && std::time::Instant::now() < end {
        core.feed("panic", "", FEED_DEADLINE).expect("empty feed is a no-op");
        thread::sleep(Duration::from_millis(5));
    }

    assert_eq!(core.stats(FEED_DEADLINE).panics, before + 1, "the panic must be counted once");
    assert_eq!(
        *bad.delivered.lock().unwrap_or_else(|e| e.into_inner()),
        1,
        "the sink panicked on its first delivery and was removed, so it must not be called again"
    );
    let text = healthy.text();
    assert!(text.starts_with("AAA"), "healthy sink got {text:?}");

    // The session is untouched: alive, not crashed, still takes output, and still renders a screen.
    let record = core
        .drain()
        .into_iter()
        .find(|record| record.session_id == "panic")
        .expect("a drain record after the panic");
    assert!(record.alive, "record: {record:?}");
    assert!(!record.crashed, "record: {record:?}");
    assert!(!record.degraded, "record: {record:?}");

    core.feed("panic", "BBB", FEED_DEADLINE).expect("feed after the panic");
    let end = std::time::Instant::now() + Duration::from_secs(2);
    while healthy.text_len() < 6 && std::time::Instant::now() < end {
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(healthy.text(), "AAABBB", "the healthy sink lost or reordered bytes");

    // A sink attached after the panic registers (the sink list is not silently dead on poison) and
    // receives; the panicked handle is not attached, so a detach of it answers false.
    let later = Arc::new(RecordingSink::new(Duration::ZERO));
    let (_replay, later_handle) = core.attach_sink("panic", later.clone()).expect("attach_sink after");
    core.feed("panic", "CCC", FEED_DEADLINE).expect("feed CCC");
    let end = std::time::Instant::now() + Duration::from_secs(2);
    while later.text_len() < 3 && std::time::Instant::now() < end {
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(
        later.text(),
        "CCC",
        "a sink attached after the panic must receive the bytes that follow it"
    );
    assert!(
        core.detach_sink("panic", later_handle).expect("detach later"),
        "a sink attached after the panic must detach"
    );
    assert!(
        !core.detach_sink("panic", panic_handle).expect("detach panicked"),
        "the panicking sink was removed by its panic, so its handle must not detach"
    );
    assert!(core.detach_sink("panic", healthy_handle).expect("detach healthy"));
    core.kill("panic", Duration::from_millis(100)).expect("kill");
}
