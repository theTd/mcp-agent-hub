//! `retire` against a resume of the same id (review F1/K1).
//!
//! The failure this file pins: `retire` removed a dead predecessor and closed the id's fan-out while a
//! resume of that id was building its successor. The successor took the closed fan-out over, so the live
//! session's screen kept updating while every socket attached to it received nothing and was never told
//! `closed` — a console frozen for good with no reason given.
//!
//! The contract now: a lifecycle call (`spawn`, `attach_text`, `retire`) claims the id for its whole
//! duration, so a `retire` that overlaps a resume is refused with `ValueError` and the resume keeps an
//! open fan-out; a resume that starts after a completed `retire` builds a fresh one. Either way a live
//! session always delivers to the id's sockets.
//!
//! The cases run one at a time (a process-wide lock) and need no test hooks: the gate is an ordinary
//! `FrameSink` whose `deliver` parks, and `deliver` runs under the id's delivery lock.

use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use conpty_session::{Core, CoreError, CoreOptions, SessionFlags, TextRequest};
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
}

fn text_request(id: &str, seed: Option<&str>) -> TextRequest {
    TextRequest {
        session_id: id.to_string(),
        cols: 60,
        rows: 10,
        flags: SessionFlags::default(),
        seed_history: None,
        seed: seed.map(str::to_string),
    }
}

/// Drains until the core reports a death record for `id`; `false` on the timeout.
fn wait_dead(core: &Core, id: &str, within: Duration) -> bool {
    let end = Instant::now() + within;
    while Instant::now() < end {
        if core
            .drain()
            .into_iter()
            .any(|record| record.session_id == id && !record.alive)
        {
            return true;
        }
        thread::sleep(Duration::from_millis(5));
    }
    false
}

/// Takes everything queued on `handle` for a quiet window of `within`, reporting the text, the queue's
/// own drop count and whether it said `closed`.
fn collect(core: &Core, handle: u64, within: Duration) -> (String, u64, bool) {
    let end = Instant::now() + within;
    let mut text = String::new();
    let mut dropped = 0;
    let mut closed = false;
    let mut stable_since = Instant::now();
    while Instant::now() < end {
        let taken = core.take_frames(handle).expect("take_frames");
        dropped = taken.dropped_bytes;
        closed = taken.closed;
        for frame in taken.frames {
            text.push_str(std::str::from_utf8(&frame).expect("a frame is text"));
            stable_since = Instant::now();
        }
        if stable_since.elapsed() >= Duration::from_millis(40) {
            break;
        }
        thread::sleep(Duration::from_millis(5));
    }
    (text, dropped, closed)
}

/// A sink that parks the id's delivery lock inside `deliver` until the test opens the gate, and records
/// that it was reached. `deliver` runs under that lock, so a parked gate is a held delivery lock.
struct GateSink {
    entered: Mutex<bool>,
    entered_cv: Condvar,
    open: Mutex<bool>,
    open_cv: Condvar,
}

impl GateSink {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            entered: Mutex::new(false),
            entered_cv: Condvar::new(),
            open: Mutex::new(false),
            open_cv: Condvar::new(),
        })
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
    fn deliver(&self, _session_id: &str, _frame: &[u8]) {
        *self.entered.lock().unwrap_or_else(|error| error.into_inner()) = true;
        self.entered_cv.notify_all();
        let mut open = self.open.lock().unwrap_or_else(|error| error.into_inner());
        while !*open {
            open = self
                .open_cv
                .wait(open)
                .unwrap_or_else(|error| error.into_inner());
        }
    }
}

/// Puts a resume of `id` inside its lifecycle claim with the id's delivery lock held: incarnation 1 is
/// dead and drained (so `retire` would accept it), a gate sink is attached, and the resume's handover
/// parks in the gate delivering the separator. Returns the gate and the resume thread.
fn park_a_resume_inside_its_claim(
    core: &Arc<Core>,
    id: &str,
) -> (Arc<GateSink>, thread::JoinHandle<Result<conpty_session::SpawnReply, CoreError>>) {
    core.attach_text(text_request(id, None))
        .expect("incarnation 1");
    let gate = GateSink::new();
    core.attach_sink(id, Arc::clone(&gate) as Arc<dyn FrameSink>)
        .expect("attach the gate sink");
    core.kill(id, Duration::from_millis(200)).expect("kill 1");
    assert!(wait_dead(core, id, Duration::from_secs(3)), "the exit record");

    let resume = {
        let core = Arc::clone(core);
        let id = id.to_string();
        thread::spawn(move || core.attach_text(text_request(&id, Some("SEED"))))
    };
    assert!(
        gate.wait_entered(Duration::from_secs(3)),
        "the handover never reached the gate sink"
    );
    (gate, resume)
}

/// Finding F1/K1, the refusal: `retire` that lands while a resume of the id holds the claim answers
/// `ValueError` (retry) instead of removing the predecessor and closing the fan-out the successor is
/// about to take over. The successor then keeps the id's sockets alive: it is live, its queue is open,
/// and its output arrives.
#[test]
fn a_resume_inside_its_claim_refuses_retire_and_keeps_delivering() {
    let _serial = serialized();
    let core = core();
    let (gate, resume) = park_a_resume_inside_its_claim(&core, "claimed");

    let refused = core
        .retire("claimed")
        .expect_err("retire must not run while a resume of the id is in flight");
    assert!(matches!(refused, CoreError::Invalid(_)), "refused={refused:?}");
    assert!(
        refused.to_string().contains("in flight"),
        "the refusal says what to retry: {refused}"
    );
    // The predecessor is still in the table: a refused retire removed nothing.
    assert_eq!(core.stats(DEADLINE).sessions, 1, "the predecessor was left in place");

    gate.release();
    let spawned = resume.join().expect("the resume thread");
    assert!(spawned.is_ok(), "the resume finished: {spawned:?}");

    // The live successor is on an open fan-out: its output reaches the id's sockets and no queue is told
    // to close over a session that is still running.
    let (_replay, handle) = core.attach_queue("claimed", None).expect("attach_queue");
    core.feed("claimed", "NEW-OUTPUT", DEADLINE)
        .expect("feed the successor");
    let (text, dropped, closed) = collect(&core, handle, Duration::from_secs(2));
    let screen = core
        .screen("claimed", 10, DEADLINE)
        .expect("the live successor's screen");
    assert!(
        screen.text.contains("NEW-OUTPUT"),
        "the session is live: {:?}",
        screen.text
    );
    assert!(
        text.contains("NEW-OUTPUT"),
        "the live successor's socket got {text:?} (dropped {dropped}, closed {closed})"
    );
    assert!(!closed, "a live session's queue never reports closed");
    assert!(core.detach_queue(handle), "the handle still detaches");

    // And the id is retirable once it really has ended: kill, drain the exit record, retire.
    core.kill("claimed", Duration::from_millis(200))
        .expect("kill the successor");
    assert!(wait_dead(&core, "claimed", Duration::from_secs(3)), "the successor's exit record");
    assert!(core.retire("claimed").expect("retire the ended successor"));
    core.shutdown();
}

/// Finding F1/K1, the other order: `retire` completed before the resume began, so the successor found no
/// predecessor. It builds a fresh fan-out and its sockets work — an id can never stay dark because a
/// fan-out of an earlier incarnation was closed.
#[test]
fn a_resume_after_a_completed_retire_builds_a_fresh_fanout() {
    let _serial = serialized();
    let core = core();
    core.attach_text(text_request("fresh", None)).expect("incarnation 1");
    core.feed("fresh", "OLD", DEADLINE).expect("feed OLD");
    core.kill("fresh", Duration::from_millis(200)).expect("kill 1");
    assert!(wait_dead(&core, "fresh", Duration::from_secs(3)), "the exit record");
    assert!(core.retire("fresh").expect("retire the predecessor"));

    core.attach_text(text_request("fresh", Some("BANNER")))
        .expect("incarnation 2");
    let (replay, handle) = core.attach_queue("fresh", None).expect("attach_queue");
    core.feed("fresh", "AFTER", DEADLINE).expect("feed the successor");
    let (text, dropped, closed) = collect(&core, handle, Duration::from_secs(2));
    assert!(!closed, "a fresh incarnation's queue is open");
    assert_eq!(dropped, 0, "nothing was dropped");
    assert!(
        text.contains("AFTER"),
        "the successor's socket got {replay:?}{text:?}"
    );
    assert!(
        !text.contains("OLD"),
        "a retire ends the id's history: no predecessor bytes survived it: {text:?}"
    );
    assert!(core.detach_queue(handle));
    core.kill("fresh", Duration::from_millis(200)).expect("kill 2");
    core.shutdown();
}