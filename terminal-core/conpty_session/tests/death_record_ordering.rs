//! §5.3: the death record never precedes the session's last byte (review F2).
//!
//! `publish_death` sets `alive = false` and then delivers the pending batch. The window between the two
//! is what a 50 ms drain can land in, and a record that says "ended" arriving before the last chunk makes
//! a pane print the exit banner over output that arrives afterwards. The fix makes the death record wait
//! for its own flush, so this case parks the flush inside a sink (`deliver` runs under the id's delivery
//! lock) and drains while it is parked.
//!
//! No test hooks: `kill` on a text session ends it through `publish_death`, and the gate is an ordinary
//! `FrameSink`.

use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use conpty_session::{Core, CoreOptions, SessionFlags, TextRequest};
use terminal_stream::FrameSink;

const DEADLINE: Duration = Duration::from_millis(500);

static SERIAL: Mutex<()> = Mutex::new(());

fn serialized() -> MutexGuard<'static, ()> {
    SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn core() -> Arc<Core> {
    Core::new(
        Box::new(|| false),
        Box::new(|_line: &str| {}),
        CoreOptions::default(),
    )
    .expect("core")
}

/// A sink that records every frame and parks the first one until the test opens the gate; the first frame
/// is the death flush's batch, delivered under the id's delivery lock.
struct GateSink {
    bytes: Mutex<Vec<u8>>,
    entered: Mutex<bool>,
    entered_cv: Condvar,
    open: Mutex<bool>,
    open_cv: Condvar,
}

impl GateSink {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            bytes: Mutex::new(Vec::new()),
            entered: Mutex::new(false),
            entered_cv: Condvar::new(),
            open: Mutex::new(false),
            open_cv: Condvar::new(),
        })
    }

    fn text(&self) -> String {
        String::from_utf8_lossy(&self.bytes.lock().unwrap_or_else(|error| error.into_inner())).into_owned()
    }

    fn wait_entered(&self, within: Duration) -> bool {
        let end = Instant::now() + within;
        let mut entered = self.entered.lock().unwrap_or_else(|error| error.into_inner());
        while !*entered {
            let remaining = end.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return false;
            }
            entered = self
                .entered_cv
                .wait_timeout(entered, remaining)
                .unwrap_or_else(|error| error.into_inner())
                .0;
        }
        true
    }

    fn release(&self) {
        *self.open.lock().unwrap_or_else(|error| error.into_inner()) = true;
        self.open_cv.notify_all();
    }
}

impl FrameSink for GateSink {
    fn deliver(&self, _session_id: &str, frame: &[u8]) {
        let entered_now = {
            let mut entered = self.entered.lock().unwrap_or_else(|error| error.into_inner());
            let first = !*entered;
            *entered = true;
            first
        };
        self.entered_cv.notify_all();
        if entered_now {
            let mut open = self.open.lock().unwrap_or_else(|error| error.into_inner());
            while !*open {
                open = self
                    .open_cv
                    .wait(open)
                    .unwrap_or_else(|error| error.into_inner());
            }
        }
        self.bytes
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .extend_from_slice(frame);
    }
}

/// The death is published, its last delivery is still parked in a sink, and every drain inside that
/// window reports nothing for the session; the record that says "ended" comes only afterwards, with the
/// last bytes already delivered.
#[test]
fn no_drain_reports_the_death_before_the_last_byte_is_delivered() {
    let _serial = serialized();
    let core = core();
    core.attach_text(TextRequest {
        session_id: "dying".to_string(),
        cols: 40,
        rows: 6,
        flags: SessionFlags::default(),
        seed_history: None,
        seed: None,
    })
    .expect("attach_text");
    let gate = GateSink::new();
    core.attach_sink("dying", Arc::clone(&gate) as Arc<dyn FrameSink>)
        .expect("attach the gate sink");
    // One frame that is still in the coalescing window, so `publish_death`'s flush is what delivers it.
    core.feed("dying", "LAST-WORDS", DEADLINE)
        .expect("feed the last batch");

    let killer = {
        let core = Arc::clone(&core);
        thread::spawn(move || core.kill("dying", Duration::from_millis(200)))
    };
    assert!(
        gate.wait_entered(Duration::from_secs(3)),
        "the death flush never reached the sink"
    );

    // The flush is parked holding the delivery lock, so `death_flushed` is still false: no record may
    // report this session as ended yet, and the session must stay dirty for the next drain.
    let end = Instant::now() + Duration::from_millis(150);
    while Instant::now() < end {
        for record in core.drain() {
            assert!(
                record.alive,
                "a drain reported the death while the last batch was still undelivered: {record:?}"
            );
        }
        thread::sleep(Duration::from_millis(5));
    }
    assert!(
        !gate.text().contains("LAST-WORDS"),
        "the gate held the batch: {:?}",
        gate.text()
    );

    gate.release();
    killer
        .join()
        .expect("the kill thread")
        .expect("kill the text session");

    let death = core
        .drain()
        .into_iter()
        .find(|record| record.session_id == "dying" && !record.alive)
        .expect("the death record after the flush");
    assert!(
        gate.text().contains("LAST-WORDS"),
        "the sink got the last byte before the death record: {:?}",
        gate.text()
    );
    assert_eq!(death.bytes_total, "LAST-WORDS".len() as u64, "the record counts the batch");
    core.shutdown();
}