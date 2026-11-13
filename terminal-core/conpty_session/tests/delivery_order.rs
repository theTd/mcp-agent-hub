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
    fn deliver(&self, _session_id: &str, frame: &[u8]) -> u64 {
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
        0
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