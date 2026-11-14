//! Step 3b-core's surface: the per-socket frame queue bridge (`attach_queue`/`take_frames`/
//! `detach_queue`) and `retire`.
//!
//! The bridge exists only until step 4 moves the console sockets into the core, so these cases pin the
//! three properties the Python `/ws/pty` endpoint depends on: Q11 (a feed racing an attach lands in the
//! replay or in the frames, exactly once), A1 (a handle survives a resume and receives the predecessor's
//! last bytes, then the separator, then the new output) and `closed` (the socket learns the id was
//! retired so it can close with code 1000).
//!
//! `retire`'s rule is the exit record: a session whose death no drain has reported is refused, because
//! that record is the only thing that cannot be re-created once the session leaves the table.
//!
//! The cases use `attach_text`/`feed`, which run the same delivery code as a real spawn without a
//! process; the hooks (a delayed attach, held batches) are process-wide, so the file runs one case at a
//! time.

use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use conpty_session::{Core, CoreError, CoreOptions, SessionFlags, TextRequest};

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

/// Drains until the core reports nothing for a quiet window, so a case that waits for "the next record"
/// does not read a predecessor's leftover. (Only the hook cases need it.)
#[cfg(feature = "test-hooks")]
fn drain_until_quiet(core: &Core) {
    let end = Instant::now() + Duration::from_secs(2);
    let mut quiet_since = Instant::now();
    while Instant::now() < end {
        if core.drain().is_empty() {
            if quiet_since.elapsed() >= Duration::from_millis(80) {
                return;
            }
        } else {
            quiet_since = Instant::now();
        }
        thread::sleep(Duration::from_millis(10));
    }
    panic!("the core never went quiet");
}

/// Waits for a death record of `id`; `None` on the timeout.
fn wait_dead(core: &Core, id: &str, within: Duration) -> Option<conpty_session::DrainRecord> {
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

/// Takes frames until the received text stops growing: delivery trails a `feed` by one coalescing window.
fn collect(core: &Core, handle: u64, within: Duration) -> (String, u64, bool) {
    let end = Instant::now() + within;
    let mut text = String::new();
    let mut dropped = 0u64;
    let mut closed = false;
    let mut last_len = 0usize;
    let mut stable_since = Instant::now();
    while Instant::now() < end {
        let taken = core.take_frames(handle).expect("take_frames");
        dropped = taken.dropped_bytes;
        closed = taken.closed;
        if !taken.frames.is_empty() {
            for frame in taken.frames {
                text.push_str(std::str::from_utf8(&frame).expect("a frame is text"));
            }
            stable_since = Instant::now();
        }
        if text.len() != last_len {
            last_len = text.len();
            stable_since = Instant::now();
        } else if stable_since.elapsed() >= Duration::from_millis(40) {
            break;
        }
        thread::sleep(Duration::from_millis(5));
    }
    (text, dropped, closed)
}

/// Q11 through the bridge: an attach that races a stream sees every chunk exactly once. The hook parks
/// `attach_sink` between its replay snapshot and its registration, so a chunk delivered inside that
/// window would be lost (or duplicated) unless the delivery lock covers both steps.
#[cfg(feature = "test-hooks")]
#[test]
fn an_attach_racing_a_feed_lands_each_chunk_exactly_once() {
    use conpty_session::test_hooks::set_attach_delay;

    let _serial = serialized();
    let core = core();
    core.attach_text(text_request("race", None)).expect("attach_text");
    let chunks: Vec<String> = (0..80).map(|index| format!("[{index:04}]")).collect();
    let expected = chunks.concat();

    let feeder = {
        let core = Arc::clone(&core);
        let chunks = chunks.clone();
        thread::spawn(move || {
            for chunk in &chunks {
                core.feed("race", chunk, DEADLINE).expect("feed");
                thread::sleep(Duration::from_millis(2));
            }
        })
    };

    let mut sinks: Vec<(String, u64)> = Vec::new();
    for _ in 0..6 {
        set_attach_delay(Duration::from_millis(20));
        let (replay, handle) = core.attach_queue("race", None).expect("attach_queue");
        set_attach_delay(Duration::ZERO);
        sinks.push((replay, handle));
        thread::sleep(Duration::from_millis(25));
    }
    feeder.join().expect("the feeder thread");

    for (index, (replay, handle)) in sinks.iter().enumerate() {
        let (framed, dropped, closed) = collect(&core, *handle, Duration::from_secs(2));
        let received = format!("{replay}{framed}");
        assert_eq!(dropped, 0, "sink {index} was never over its cap");
        assert!(!closed, "sink {index} was not closed");
        assert!(
            expected.ends_with(&received),
            "sink {index}: replay {} + frames {} is not the stream's suffix",
            replay.len(),
            framed.len()
        );
        assert!(!received.is_empty(), "sink {index} received nothing");
        assert!(core.detach_queue(*handle), "detach sink {index}");
    }
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// A1 through the bridge: a handle taken before a resume keeps receiving after the handover — the
/// predecessor's last batch, then the separator, then the successor's output, each exactly once.
#[cfg(feature = "test-hooks")]
#[test]
fn a_handle_carries_over_a_resume() {
    use conpty_session::test_hooks::{hold_frames, release_frames};

    let _serial = serialized();
    let core = core();
    // The hold keeps incarnation 1's batch pending, so the handover's own flush is what delivers "OLD".
    hold_frames();
    core.attach_text(text_request("carry", None)).expect("incarnation 1");
    let (replay, handle) = core.attach_queue("carry", None).expect("attach_queue");
    assert!(replay.is_empty(), "nothing printed yet: {replay:?}");
    core.feed("carry", "OLD", DEADLINE).expect("feed OLD");
    core.kill("carry", Duration::from_millis(100)).expect("kill incarnation 1");
    drain_until_quiet(&core);

    core.attach_text(text_request("carry", Some("PREFIX")))
        .expect("incarnation 2");
    release_frames();
    core.feed("carry", "NEW", DEADLINE).expect("feed NEW");
    let (text, dropped, closed) = collect(&core, handle, Duration::from_secs(2));
    assert_eq!(text, "OLDPREFIXNEW", "old batch, separator, new output, in that order");
    assert_eq!(dropped, 0);
    assert!(!closed);

    // The handle belongs to the id, so it detaches against the successor; nothing more reaches it.
    assert!(core.detach_queue(handle), "the handle must detach after a resume");
    assert!(!core.detach_queue(handle), "the second detach reports false");
    assert!(
        matches!(core.take_frames(handle), Err(CoreError::UnknownQueue(_))),
        "a detached handle is an unknown handle"
    );
    core.feed("carry", "TAIL", DEADLINE).expect("feed TAIL");
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// The cap and the drop count are one socket's: `attach_queue`'s `max_bytes` and
/// `CoreOptions::sink_queue_bytes` both reach the queue, and the oldest whole frames leave first.
#[cfg(feature = "test-hooks")]
#[test]
fn the_queue_cap_and_its_drop_count_belong_to_one_socket() {
    let _serial = serialized();
    let core = Core::new(
        Box::new(|| false),
        Box::new(|_line: &str| {}),
        CoreOptions {
            sink_queue_bytes: 120,
            ..CoreOptions::default()
        },
    )
    .expect("core");
    core.attach_text(text_request("cap", None)).expect("attach_text");
    let (_replay, small) = core.attach_queue("cap", Some(120)).expect("the explicit cap");
    let (_replay, wide) = core.attach_queue("cap", Some(16 * 1024)).expect("a wide cap");
    // Two frames, 25 ms apart so the coalescing window flushes each on its own.
    core.feed("cap", &"x".repeat(100), DEADLINE).expect("feed x");
    thread::sleep(Duration::from_millis(100));
    core.feed("cap", &"y".repeat(100), DEADLINE).expect("feed y");
    thread::sleep(Duration::from_millis(100));

    let (small_text, dropped, _closed) = collect(&core, small, Duration::from_secs(2));
    assert_eq!(dropped, 100, "the oldest whole frame made room, and only it");
    assert_eq!(small_text, "y".repeat(100), "the newest frame is what is left");
    let (wide_text, wide_dropped, _closed) = collect(&core, wide, Duration::from_secs(2));
    assert_eq!(wide_dropped, 0, "the wide queue lost nothing");
    assert_eq!(wide_text.len(), 200, "the wide queue keeps both frames");

    assert!(core.detach_queue(small));
    assert!(core.detach_queue(wide));
    core.kill("cap", Duration::from_millis(100)).expect("kill");
    core.shutdown();
}

/// `retire`, the whole contract: an unknown handle and an unknown id, refused while the session lives,
/// refused until the exit record was drained, then gone from every read with its queues closed.
#[test]
fn retire_removes_an_ended_session_and_keeps_its_exit_record() {
    let _serial = serialized();
    let core = core();
    assert!(
        matches!(
            core.attach_queue("no-such-session", None),
            Err(CoreError::UnknownSession(_))
        ),
        "attach_queue of an unknown id is a KeyError"
    );
    assert!(
        matches!(core.take_frames(9999), Err(CoreError::UnknownQueue(9999))),
        "an unknown handle is a KeyError"
    );
    assert!(!core.detach_queue(9999), "an unknown handle does not detach");
    assert!(!core.retire("no-such-session").expect("retire unknown"), "unknown id is false");

    core.attach_text(text_request("gone", None)).expect("attach_text");
    let (_replay, handle) = core.attach_queue("gone", None).expect("attach_queue");
    core.feed("gone", "LAST WORDS", DEADLINE).expect("feed");
    let live = core.retire("gone").expect_err("a live session cannot be retired");
    assert!(matches!(live, CoreError::Invalid(_)), "{live:?}");
    assert!(live.to_string().contains("still running"), "{live}");

    core.kill("gone", Duration::from_millis(100)).expect("kill");
    let undrained = core
        .retire("gone")
        .expect_err("a session whose exit record has not been drained cannot be retired");
    assert!(
        undrained.to_string().contains("exit record"),
        "the refusal names the record: {undrained}"
    );
    let death = wait_dead(&core, "gone", Duration::from_secs(2)).expect("the exit record");
    assert_eq!(death.session_id, "gone");

    assert!(core.retire("gone").expect("retire an ended, drained session"));
    assert!(
        matches!(core.screen("gone", 10, DEADLINE), Err(CoreError::UnknownSession(_))),
        "a retired id is unknown to the reads"
    );
    assert_eq!(core.stats(DEADLINE).sessions, 0, "a retired id leaves the counters");
    assert!(
        core.snapshot_all(10, 1024, DEADLINE).is_empty(),
        "a retired id leaves snapshot_all"
    );
    let taken = core.take_frames(handle).expect("take on a retired id");
    assert!(taken.closed, "a retired id's queue reports closed");
    assert!(taken.frames.is_empty(), "the queue kept no bytes");
    assert!(core.detach_queue(handle), "the closed queue still detaches");
    assert!(!core.detach_queue(handle), "a second detach is false");
    core.shutdown();
}

/// After `retire` the id starts fresh: a new incarnation has its own fan-out (nothing carried over) and
/// its own queue, and the old handle stays closed.
#[test]
fn a_respawn_after_retire_carries_nothing_over() {
    let _serial = serialized();
    let core = core();
    core.attach_text(text_request("again", None)).expect("attach_text");
    let (_replay, old_handle) = core.attach_queue("again", None).expect("attach_queue");
    core.feed("again", "OLD", DEADLINE).expect("feed OLD");
    core.kill("again", Duration::from_millis(100)).expect("kill");
    wait_dead(&core, "again", Duration::from_secs(2)).expect("the exit record");
    assert!(core.retire("again").expect("retire"));
    core.take_frames(old_handle).expect("the old handle still answers closed");

    core.attach_text(text_request("again", None)).expect("a fresh incarnation");
    core.feed("again", "NEW", DEADLINE).expect("feed NEW");
    let (replay, handle) = core.attach_queue("again", None).expect("attach_queue");
    let (framed, _, closed) = collect(&core, handle, Duration::from_secs(2));
    assert!(!closed);
    assert_eq!(format!("{replay}{framed}"), "NEW", "no predecessor bytes carried over");
    assert!(
        core.retire("again").is_err(),
        "the new incarnation is still running"
    );
    assert_eq!(core.stats(DEADLINE).sessions, 1);
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// A seeded attach's ring is already at revision 1 when the session is built, so the drain record must
/// report that number from the first record on — not a mirror that starts at 0 and only catches up with
/// the first ingest (review F5/K4).
#[test]
fn a_seeded_attach_reports_the_rings_raw_revision_from_the_first_record() {
    let _serial = serialized();
    let core = core();
    core.attach_text(TextRequest {
        session_id: "seeded-rev".to_string(),
        cols: 40,
        rows: 6,
        flags: SessionFlags::default(),
        seed_history: Some("ARCHIVED-HISTORY\r\n".to_string()),
        seed: None,
    })
    .expect("a seeded attachment");
    // `resize` dirties the session without touching the ring: the record below carries the ring as the
    // seed left it, not as an ingest moved it.
    core.resize("seeded-rev", 41, 6, DEADLINE).expect("resize");
    let record = core
        .drain()
        .into_iter()
        .find(|record| record.session_id == "seeded-rev")
        .expect("a drain record");
    let raw = core.raw("seeded-rev", DEADLINE).expect("raw");
    let snapshot = core
        .snapshot_all(10, 4096, DEADLINE)
        .into_iter()
        .find(|snapshot| snapshot.session_id == "seeded-rev")
        .expect("a snapshot");
    assert_eq!(raw.rev, 1, "the seeded ring is at revision 1");
    assert_eq!(snapshot.raw_rev, raw.rev, "snapshot_all reads the ring itself");
    assert_eq!(
        record.raw_rev, raw.rev,
        "the first record after a seeded attach carries the ring's revision"
    );
    core.kill("seeded-rev", Duration::from_millis(100)).expect("kill");
    core.shutdown();
}

/// The same number while the id's delivery lock is held: `ingest` stores both revisions under the text
/// lock, before it waits for a delivery, so a drain taken inside that window cannot report the previous
/// revision while `raw()` already answers the new one (review F5/K4, `step3b-raw-rev`).
#[cfg(feature = "test-hooks")]
#[test]
fn the_drain_record_keeps_up_while_a_delivery_is_in_flight() {
    use conpty_session::test_hooks::set_attach_delay;

    let _serial = serialized();
    let core = core();
    core.attach_text(text_request("held-rev", None)).expect("attach_text");
    core.feed("held-rev", "ONE", DEADLINE).expect("feed ONE");

    // The attach parks holding the delivery lock, so the second feed's pump cannot deliver yet; its
    // ingest has already moved the ring and both mirrors.
    set_attach_delay(Duration::from_millis(400));
    let attacher = {
        let core = Arc::clone(&core);
        thread::spawn(move || core.attach_queue("held-rev", None).expect("attach_queue"))
    };
    thread::sleep(Duration::from_millis(80));
    let feeder = {
        let core = Arc::clone(&core);
        thread::spawn(move || core.feed("held-rev", "TWO", DEADLINE).expect("feed TWO"))
    };

    let end = Instant::now() + Duration::from_secs(1);
    let raw = loop {
        let raw = core.raw("held-rev", DEADLINE).expect("raw");
        if raw.text == "ONETWO" {
            break raw;
        }
        assert!(Instant::now() < end, "the second feed never reached the ring");
        thread::sleep(Duration::from_millis(2));
    };
    let record = core
        .drain()
        .into_iter()
        .find(|record| record.session_id == "held-rev")
        .expect("the first feed left a dirty record");
    set_attach_delay(Duration::ZERO);
    attacher.join().expect("attacher");
    feeder.join().expect("feeder");
    core.kill("held-rev", Duration::from_millis(100)).expect("kill");
    core.shutdown();
    assert_eq!(
        record.raw_rev, raw.rev,
        "the record reports the ring's revision while a delivery is still held: raw={raw:?}, \
         record={record:?}"
    );
}

/// The seam's `raw_revision` comes from the drain record: the same number `raw`'s reply carries, without
/// joining the ring.
#[test]
fn the_drain_record_carries_the_raw_revision() {
    let _serial = serialized();
    let core = core();
    core.attach_text(text_request("rev", None)).expect("attach_text");
    core.feed("rev", "hello", DEADLINE).expect("feed");
    let record = core
        .drain()
        .into_iter()
        .find(|record| record.session_id == "rev")
        .expect("a drain record");
    let raw = core.raw("rev", DEADLINE).expect("raw");
    assert!(record.raw_rev > 0, "record: {record:?}");
    assert_eq!(record.raw_rev, raw.rev, "the record and the raw reply share one revision");
    assert!(record.alive);
    core.kill("rev", Duration::from_millis(100)).expect("kill");
    core.shutdown();
}

/// §5.4: a socket's own loss is its own count. One queue over its cap drops the oldest whole frames and
/// reports them from its handle; the session's `dropped_bytes` (drain record, `stats`) stays at zero,
/// because the ring lost nothing (review F6).
#[test]
fn a_sockets_dropped_bytes_stay_out_of_the_sessions_count() {
    let _serial = serialized();
    let core = core();
    core.attach_text(text_request("socket-loss", None)).expect("attach_text");
    let (_replay, handle) = core.attach_queue("socket-loss", Some(8)).expect("a tiny queue");
    core.feed("socket-loss", &"x".repeat(16), DEADLINE).expect("feed");
    // The ticker's window flushes the batch into the tiny queue, which cannot hold the whole frame.
    thread::sleep(Duration::from_millis(100));
    let taken = core.take_frames(handle).expect("take_frames");
    assert_eq!(taken.dropped_bytes, 16, "the queue counted its own loss");
    assert!(taken.frames.is_empty(), "an oversize frame is dropped whole, never split");

    let record = core
        .drain()
        .into_iter()
        .find(|record| record.session_id == "socket-loss")
        .expect("a drain record for the fed session");
    assert_eq!(
        record.dropped_bytes, 0,
        "a socket's overflow is not the session's loss"
    );
    assert_eq!(
        core.stats(DEADLINE).dropped_bytes,
        0,
        "one slow socket's drop never reaches the session's counters"
    );
    assert!(core.detach_queue(handle));
    core.kill("socket-loss", Duration::from_millis(100)).expect("kill");
    core.shutdown();
}

/// The ring's own loss is the session's: a frame too large for the raw ring is counted in the drain
/// record and in `stats`, which is the count §5.4 keeps there. The per-socket count above is separate.
#[test]
fn the_rings_own_loss_is_counted_by_the_session() {
    let _serial = serialized();
    let core = core();
    core.attach_text(TextRequest {
        session_id: "ring-loss".to_string(),
        cols: 40,
        rows: 6,
        flags: SessionFlags {
            raw_cap_bytes: 64,
            raw_slack_bytes: 0,
            ..SessionFlags::default()
        },
        seed_history: None,
        seed: None,
    })
    .expect("attach_text");
    core.feed("ring-loss", &"y".repeat(4096), DEADLINE).expect("feed");
    let record = core
        .drain()
        .into_iter()
        .find(|record| record.session_id == "ring-loss")
        .expect("a drain record");
    assert!(
        record.dropped_bytes > 0,
        "the ring dropped the oversize chunk and the record says so: {record:?}"
    );
    assert_eq!(
        core.stats(DEADLINE).dropped_bytes,
        record.dropped_bytes,
        "the session's counter is the ring's loss, cumulative"
    );
    core.kill("ring-loss", Duration::from_millis(100)).expect("kill");
    core.shutdown();
}

/// The owner `Arc<Session>` an `attach_sink` takes for the replay must be released **after** the id's
/// delivery lock: with `retire` dropping the table's reference first, that Arc can be the last one, and
/// dropping a session inside the lock closes its pipes and may wait for a console helper while every
/// delivery waits. `Session::drop` asserts the invariant; this case reaches the interleaving.
#[cfg(feature = "test-hooks")]
#[test]
fn the_owner_arc_is_released_after_the_delivery_lock() {
    use conpty_session::test_hooks::set_attach_delay;
    use conpty_session::FrameQueue;
    use terminal_stream::FrameSink;

    let _serial = serialized();
    // No tick during the case: a tick's session snapshot holds a reference of its own while it waits
    // for the delivery lock, which would hide the interleaving this test exists to reach.
    let core = Core::new(
        Box::new(|| false),
        Box::new(|_line: &str| {}),
        CoreOptions {
            tick_interval: Duration::from_secs(10),
            ..CoreOptions::default()
        },
    )
    .expect("core");
    core.attach_text(text_request("owner", None)).expect("incarnation 1");
    let old = core.session_for_test("owner").expect("incarnation 1 in the table");
    core.kill("owner", Duration::from_millis(100)).expect("kill 1");
    drain_until_quiet(&core);
    core.attach_text(text_request("owner", Some("SEED")))
        .expect("incarnation 2");
    core.kill("owner", Duration::from_millis(100)).expect("kill 2");
    wait_dead(&core, "owner", Duration::from_secs(2)).expect("the exit record");

    // The attach through the predecessor takes its replay from incarnation 2 (the fan-out's owner) and
    // parks in the hook while holding the delivery lock.
    set_attach_delay(Duration::from_millis(300));
    let attacher = {
        let old = Arc::clone(&old);
        thread::spawn(move || {
            let sink: Arc<dyn FrameSink> = Arc::new(FrameQueue::new(1024));
            old.attach_sink(sink)
        })
    };
    thread::sleep(Duration::from_millis(120));
    // The table's reference goes first, so the attacher's owner Arc is the last one when the hook ends.
    assert!(core.retire("owner").expect("retire incarnation 2"));
    let joined = attacher.join();
    set_attach_delay(Duration::ZERO);
    assert!(
        joined.is_ok(),
        "the owner session was dropped while the delivery lock was held"
    );
    assert!(core.session_for_test("owner").is_none());
    core.shutdown();
}
