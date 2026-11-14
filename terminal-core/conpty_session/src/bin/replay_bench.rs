//! The one measurement §9's step 4 row asks for: `attach_sink` renders a console socket's replay
//! **under the id's delivery lock**, so its cost at the 512 KiB cap decides whether that render can stay
//! inside the lock (Q11: the replay and the registration must be one gap between two deliveries).
//!
//! Not part of any gate. Run it with a release build, as the design's number was:
//!
//! ```text
//! cargo run --release --bin replay_bench
//! ```

use std::sync::Arc;
use std::time::Instant;

use conpty_session::{Core, CoreOptions, TextRequest};
use terminal_stream::QueueSink;

/// The design's cap: `raw_cap_bytes` (512 KiB) plus no slack, so one full ring is the worst case a
/// console ever replays.
const CAP_BYTES: usize = 512 * 1024;
const RUNS: usize = 300;

fn main() {
    let log = |_line: &str| {};
    let core = Core::new(Box::new(|| false), Box::new(log), CoreOptions::default())
        .expect("the core starts");

    // A realistic ring: many short lines, the shape a printing session produces, at the cap.
    let line = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789\r\n";
    let mut seed = String::with_capacity(CAP_BYTES + line.len());
    while seed.len() < CAP_BYTES {
        seed.push_str(line);
    }
    core.attach_text(TextRequest {
        session_id: "replay-bench".to_string(),
        cols: 120,
        rows: 36,
        flags: Default::default(),
        seed_history: Some(seed),
        seed: None,
    })
    .expect("attach_text");
    let text_bytes = core
        .raw("replay-bench", std::time::Duration::from_millis(50))
        .expect("raw")
        .bytes;

    let mut samples: Vec<u128> = Vec::with_capacity(RUNS);
    let mut replay_bytes = 0;
    for _ in 0..RUNS {
        let sink = Arc::new(QueueSink::new(4096));
        let started = Instant::now();
        let (replay, handle) = core
            .attach_sink("replay-bench", sink)
            .expect("attach_sink");
        samples.push(started.elapsed().as_micros());
        replay_bytes = replay.len();
        assert!(core.detach_sink("replay-bench", handle).expect("detach_sink"));
    }
    samples.sort_unstable();
    let median = samples[RUNS / 2];
    let worst = *samples.last().expect("samples");
    let mean = samples.iter().sum::<u128>() as f64 / RUNS as f64;
    let p95 = samples[RUNS * 95 / 100];

    println!("ring bytes   : {text_bytes}");
    println!("replay bytes : {replay_bytes}");
    println!("attach_sink  : median {median} us, p95 {p95} us, worst {worst} us, mean {mean:.1} us ({RUNS} runs)");
    core.shutdown();
}
