//! Session mechanics that the gates in step 1 do not otherwise reach, each pinned to one review finding.
//!
//! Every case starts `cmd.exe` (or a loop of them) headless through the system ConPTY: no window is
//! opened and nothing is typed that a person would see.

use std::collections::HashMap;
use std::sync::{mpsc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use conpty_session::job_tree::{count_processes_named_by_parent, current_handle_count};
use conpty_session::{Core, CoreError, CoreOptions, SpawnRequest};

/// The handle and conhost counts are process-wide, so the cases in this file run one at a time: a
/// parallel spawn would otherwise look exactly like the leak these tests exist to catch.
static SERIAL: Mutex<()> = Mutex::new(());

fn serialized() -> MutexGuard<'static, ()> {
    SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn core() -> std::sync::Arc<Core> {
    Core::new(
        Box::new(|| true),
        Box::new(|_message: &str| {}),
        CoreOptions {
            drain_interval: Duration::from_millis(50),
            tick_interval: Duration::from_millis(5),
            ..CoreOptions::default()
        },
    )
}

fn request(id: &str, argv: &[&str]) -> SpawnRequest {
    SpawnRequest {
        session_id: id.to_string(),
        argv: argv.iter().map(|arg| arg.to_string()).collect(),
        cwd: None,
        env: std::env::vars().collect(),
        cols: 80,
        rows: 24,
        seed_history: None,
    }
}

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
        std::thread::sleep(Duration::from_millis(10));
    }
    None
}

/// B1: a child that exits on its own is reported, its last output is in the buffer first, and the id can
/// be spawned again — releasing the replaced session's console instead of leaking an HPCON, a conhost and
/// a thread. On `0a4111a` the reader blocked in `ReadFile` forever, so no record ever arrived.
#[test]
fn natural_exit_is_reported_with_its_code_and_output() {
    let _serial = serialized();
    let core = core();
    core.spawn(request("natural", &["cmd.exe", "/c", "echo natural-exit", "&", "exit", "7"]))
        .expect("spawn");

    let record = wait_dead(&core, "natural", Duration::from_secs(1))
        .expect("no dead record within 1 s of a child that exited on its own");
    assert_eq!(record.exit_code, Some(7), "record: {record:?}");
    assert!(!record.crashed, "record: {record:?}");

    let tail = String::from_utf8_lossy(&core.raw_tail("natural", 4096, Duration::from_millis(50)).unwrap())
        .to_string();
    assert!(record.bytes_total > 0, "record: {record:?}");
    assert!(
        tail.contains("natural-exit"),
        "the last output must be in the raw buffer before or with the death record, tail: {tail:?}"
    );

    // The natural exit left `alive = false`, so the id is spawnable again, and the old session (its reader
    // hit EOF) is released instead of leaking an HPCON, a conhost and a thread.
    let again = core
        .spawn(request("natural", &["cmd.exe", "/c", "exit", "0"]))
        .expect("respawn of a naturally exited id");
    assert!(again.pid != 0);
    // Under load the second spawn's teardown can take longer than the first; the assertion is about
    // being reported at all, not about 1 s.
    let record = wait_dead(&core, "natural", Duration::from_secs(3)).expect("second death");
    assert_eq!(record.exit_code, Some(0));

    // Both dead sessions' consoles must be gone; after a third spawn exactly one conhost belongs to this
    // process. A replaced session whose reader never returned would keep its console (and its handle).
    core.spawn(request("natural", &["cmd.exe"]))
        .expect("respawn of a naturally exited id");
    let self_pid = std::process::id();
    let end = Instant::now() + Duration::from_secs(3);
    while count_processes_named_by_parent(self_pid, "conhost.exe") > 1 && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(
        count_processes_named_by_parent(self_pid, "conhost.exe") <= 1,
        "the replaced sessions kept their consoles: {} conhost processes",
        count_processes_named_by_parent(self_pid, "conhost.exe")
    );
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// B1 + should-fix 1: a failed spawn leaks nothing. On `0a4111a` the HPCON and the suspended child were
/// left behind: +27 handles and +5 `conhost.exe` per 8 failed spawns.
#[test]
fn failed_spawns_leak_no_handles_and_no_conhost() {
    let _serial = serialized();
    let core = core();
    // Warm up once so first-use lazily loaded modules are not counted as a leak.
    let _ = core.spawn(SpawnRequest {
        cwd: Some("Z:\\hub-terminal-core-no-such-dir".to_string()),
        ..request("warm-bad-cwd", &["cmd.exe"])
    });
    std::thread::sleep(Duration::from_millis(200));
    let handles_before = current_handle_count();
    // Counted by parent: `conhost.exe` is created by the process that opened the console, so another
    // app starting one at the same moment cannot be mistaken for our leak.
    let self_pid = std::process::id();
    let conhosts_before = count_processes_named_by_parent(self_pid, "conhost.exe");

    for index in 0..8 {
        let failed = core.spawn(SpawnRequest {
            cwd: Some("Z:\\hub-terminal-core-no-such-dir".to_string()),
            ..request(&format!("bad-cwd-{index}"), &["cmd.exe"])
        });
        assert!(failed.is_err(), "a bad cwd must fail the spawn");
    }
    std::thread::sleep(Duration::from_millis(500));

    // Thread-pool and console bookkeeping handles settle a moment after the close; poll instead of
    // sampling once, and allow the two handles a warm run can hold beyond the baseline.
    let end = Instant::now() + Duration::from_secs(3);
    loop {
        let settled = current_handle_count() <= handles_before + 2
            && count_processes_named_by_parent(self_pid, "conhost.exe") <= conhosts_before;
        if settled || Instant::now() >= end {
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let handles_after = current_handle_count();
    let conhosts_after = count_processes_named_by_parent(self_pid, "conhost.exe");
    assert!(
        handles_after <= handles_before + 4,
        "8 failed spawns raised the handle count from {handles_before} to {handles_after}"
    );
    assert!(
        conhosts_after <= conhosts_before,
        "8 failed spawns left conhost processes behind: {conhosts_before} -> {conhosts_after}"
    );
    core.shutdown();
}

/// Should-fix 2: a session marked dead by a thread panic still loses its tree when `kill` runs. On
/// `0a4111a` `kill` returned the cached code and never touched the job.
#[cfg(feature = "test-hooks")]
#[test]
fn kill_still_terminates_a_tree_that_was_marked_dead() {
    let _serial = serialized();
    let core = core();
    let reply = core.spawn(request("dead-then-kill", &["cmd.exe"])).expect("spawn");
    let pid = reply.pid;
    assert!(job_tree_alive(pid), "cmd.exe {pid} did not start");

    // The shape of a reader panic: crashed, alive = false, exit untouched, process still running.
    let session = {
        let core_session = session_of(&core, "dead-then-kill");
        core_session.mark_crashed_for_test("reader thread panicked");
        core_session
    };
    assert!(!session.alive());
    assert!(job_tree_alive(pid), "the crash mark must not have killed the tree by itself");

    let code = core.kill("dead-then-kill", Duration::from_millis(2000)).expect("kill");
    let end = Instant::now() + Duration::from_secs(2);
    while job_tree_alive(pid) && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(!job_tree_alive(pid), "cmd.exe {pid} survived kill of a crashed session");
    // The dead marker had no code, so the kill's termination code is what the hub sees.
    assert!(code.is_some(), "a killed session reports its code: {code:?}");
    core.shutdown();
}

/// Should-fix 3: `kill_all` over 10 live sessions finishes inside one budget instead of serially.
#[test]
fn kill_all_over_ten_sessions_returns_within_its_budget() {
    let _serial = serialized();
    let core = core();
    let mut pids = Vec::new();
    for index in 0..10 {
        let reply = core
            .spawn(request(&format!("bulk-{index}"), &["cmd.exe"]))
            .expect("spawn");
        pids.push(reply.pid);
    }
    let started = Instant::now();
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    let elapsed = started.elapsed();
    assert!(
        elapsed <= Duration::from_millis(2500),
        "kill_all of 10 sessions took {elapsed:?}"
    );
    for pid in pids {
        assert!(!job_tree_alive(pid), "cmd.exe {pid} survived kill_all");
    }
    core.shutdown();
}

/// Should-fix 4 as a session-level check: the queue is shared by all callers of `write`, so a full queue
/// must raise the timeout instead of reporting "0 bytes queued" (which an empty write also returns).
#[test]
fn a_full_write_queue_times_out_with_nothing_queued() {
    let _serial = serialized();
    let core = core();
    // `ping` never reads stdin: the input pipe backs up as soon as the console's buffer is full.
    core.spawn(request("full-queue", &["cmd.exe", "/c", "ping", "-n", "60", "127.0.0.1"]))
        .expect("spawn");
    let payload = vec![b'x'; 64 * 1024];
    let mut refused = None;
    for _ in 0..128 {
        match core.write("full-queue", &payload, Duration::from_millis(100)) {
            Ok(_) => {}
            Err(error) => {
                refused = Some(error);
                break;
            }
        }
    }
    match refused {
        Some(CoreError::Deadline(message)) => {
            assert!(message.contains("nothing queued"), "{message}")
        }
        Some(other) => panic!("expected a deadline error, got {other:?}"),
        None => panic!("the write queue never filled"),
    }
    // Nothing was queued by the refused call, so the same bytes can be retried without duplication.
    assert!(
        matches!(
            core.write("full-queue", b"", Duration::from_millis(5)),
            Ok(0)
        ),
        "an empty write is not a timeout"
    );
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// B2, core side: the ticker keeps notifying while another thread holds a lock the ticker does not
/// need, and `shutdown` still returns. The Python-level case lives in `test_terminal_core_client.py`,
/// where the GIL is the contended lock.
#[test]
fn shutdown_does_not_wait_for_a_notify_in_flight() {
    let _serial = serialized();
    let (started, started_rx) = mpsc::channel::<()>();
    let (release, release_rx) = mpsc::channel::<()>();
    let release_rx = std::sync::Mutex::new(Some(release_rx));
    let core = Core::new(
        Box::new(move || {
            let _ = started.send(());
            // One slow notify, then return: shutdown must not wait for a second one.
            if let Some(rx) = release_rx.lock().unwrap_or_else(|e| e.into_inner()).take() {
                let _ = rx.recv_timeout(Duration::from_secs(5));
            }
            true
        }),
        Box::new(|_message: &str| {}),
        CoreOptions {
            drain_interval: Duration::from_millis(10),
            tick_interval: Duration::from_millis(5),
            ..CoreOptions::default()
        },
    );
    core.spawn(request("notify", &["cmd.exe", "/c", "echo tick & ping -n 5 127.0.0.1"]))
        .expect("spawn");
    started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("the ticker never notified");
    let started_at = Instant::now();
    release.send(()).expect("the notify callback is waiting");
    core.shutdown();
    assert!(
        started_at.elapsed() < Duration::from_secs(2),
        "shutdown took {:?}",
        started_at.elapsed()
    );
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
}

/// B1: the death record carries every byte the session ever produced, and no later record reports more.
/// `stats` reads the live counters, so after the reader is done it is the settled total.
fn assert_death_keeps_every_byte(core: &Core, id: &str) -> conpty_session::DrainRecord {
    let record = wait_dead(core, id, Duration::from_secs(3)).expect("no death record");
    assert!(!record.crashed, "record: {record:?}");
    // A late flush would still raise the live counter; give the reader a moment before comparing.
    std::thread::sleep(Duration::from_millis(300));
    let settled = core.stats(Duration::from_millis(50)).bytes_total;
    assert_eq!(
        record.bytes_total, settled,
        "the death record was published before the reader filed its last bytes: {record:?}"
    );
    let end = Instant::now() + Duration::from_millis(300);
    while Instant::now() < end {
        for later in core.drain() {
            assert!(
                later.bytes_total <= record.bytes_total,
                "a record after the death grew: {later:?} after {record:?}"
            );
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    record
}

/// B1: `kill` during a natural-exit teardown. The child exits on its own and the reaper is already
/// closing the console when `kill` arrives, which is the interleaving that published an empty record on
/// `1b593ba` (the reaper's empty slot answered "closed").
#[test]
fn kill_during_a_natural_exit_keeps_every_byte() {
    let _serial = serialized();
    let core = core();
    // A delayed close helper holds the "closing" window open, so `kill` really races the teardown.
    #[cfg(feature = "test-hooks")]
    conpty_session::test_hooks::set_close_delay(Duration::from_millis(400));
    let reply = core
        .spawn(request(
            "kill-at-exit",
            &["cmd.exe", "/c", "echo sentinel & exit 3"],
        ))
        .expect("spawn");
    let end = Instant::now() + Duration::from_secs(5);
    while job_tree_alive(reply.pid) && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(!job_tree_alive(reply.pid), "cmd.exe {} never exited", reply.pid);
    let code = core
        .kill("kill-at-exit", Duration::from_millis(2000))
        .expect("kill");
    assert_eq!(code, Some(3), "kill reported {code:?}");
    assert_death_keeps_every_byte(&core, "kill-at-exit");
    #[cfg(feature = "test-hooks")]
    conpty_session::test_hooks::set_close_delay(Duration::ZERO);
    core.shutdown();
}

/// B1, with bytes in flight: an exit right after a ~64 KiB burst, `kill` racing the teardown. This is the
/// case where publishing before the reader's last read loses a visible amount of output.
#[test]
fn an_exit_right_after_a_burst_keeps_every_byte() {
    let _serial = serialized();
    let core = core();
    #[cfg(feature = "test-hooks")]
    conpty_session::test_hooks::set_close_delay(Duration::from_millis(400));
    let reply = core
        .spawn(request(
            "burst",
            &[
                "cmd.exe",
                "/c",
                "for /L %i in (1,1,2000) do @echo 0123456789012345678901234567890123456789",
            ],
        ))
        .expect("spawn");
    let end = Instant::now() + Duration::from_secs(20);
    while job_tree_alive(reply.pid) && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(!job_tree_alive(reply.pid), "the burst child never exited");
    let _ = core.kill("burst", Duration::from_millis(2000));
    let record = assert_death_keeps_every_byte(&core, "burst");
    assert!(
        record.bytes_total >= 64 * 1024,
        "the burst never reached 64 KiB: {record:?}"
    );
    #[cfg(feature = "test-hooks")]
    conpty_session::test_hooks::set_close_delay(Duration::ZERO);
    core.shutdown();
}

/// SF2: a single write larger than the queue cap (1 MiB by default) is accepted whole when nothing is
/// queued — a paste behaves as it did under pywinpty — and a second one while the first is still queued
/// is a `Timeout` with nothing queued. On `1b593ba` the first one could never succeed.
#[test]
fn an_oversize_write_is_accepted_on_an_idle_session() {
    let _serial = serialized();
    let core = core();
    // `ping` never reads stdin, so the input pipe backs up and the chunk stays queued.
    core.spawn(request("paste", &["cmd.exe", "/c", "ping", "-n", "60", "127.0.0.1"]))
        .expect("spawn");
    std::thread::sleep(Duration::from_millis(300));
    let payload = vec![b'x'; 2 * 1024 * 1024];
    match core.write("paste", &payload, Duration::from_millis(200)) {
        Ok(queued) => assert_eq!(
            queued,
            payload.len(),
            "a 2 MiB write into an idle queue must be accepted whole"
        ),
        Err(error) => panic!("a 2 MiB write into an idle queue was refused: {error}"),
    }
    match core.write("paste", &payload, Duration::from_millis(200)) {
        Err(CoreError::Deadline(message)) => {
            assert!(message.contains("nothing queued"), "{message}")
        }
        other => panic!("expected a deadline refusal while the first write is queued, got {other:?}"),
    }
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// SF1 + B1: one session whose console slot is held (a `resize` stalled inside the test hook) must not
/// stop `kill_all` from returning inside its budget, and every session's death must still publish.
#[cfg(feature = "test-hooks")]
#[test]
fn kill_all_with_a_held_console_slot_still_returns_and_publishes() {
    let _serial = serialized();
    let core = core();
    let mut ids = Vec::new();
    for index in 0..3 {
        let id = format!("held-kill-all-{index}");
        core.spawn(request(&id, &["cmd.exe"])).expect("spawn");
        ids.push(id);
    }
    conpty_session::test_hooks::hold_resize();
    let resizing = std::sync::Arc::clone(&core);
    let holder = std::thread::spawn(move || {
        resizing.resize("held-kill-all-0", 90, 30, Duration::from_secs(20))
    });
    let entered = conpty_session::test_hooks::wait_until_resize_holds(Duration::from_secs(5));
    let started = Instant::now();
    core.kill_all(Duration::from_millis(500)).expect("kill_all");
    let elapsed = started.elapsed();
    conpty_session::test_hooks::release_resize();
    let _ = holder.join();
    assert!(entered, "the resize never took the console slot");
    assert!(
        elapsed <= Duration::from_millis(2500),
        "kill_all of 3 sessions took {elapsed:?} with one console slot held"
    );
    // Every death must be in a drain by the time `kill_all` returned. Collected in one pass: a `drain`
    // clears every session's record, so waiting per id would consume the others.
    let mut records = HashMap::new();
    let settle = Instant::now() + Duration::from_secs(2);
    while records.len() < ids.len() && Instant::now() < settle {
        for record in core.drain() {
            if !record.alive {
                records.insert(record.session_id.clone(), record);
            }
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    for id in &ids {
        assert!(records.contains_key(id), "{id} never published its death: {records:?}");
    }
    core.shutdown();
}

/// SF1: a held console slot does not stop the death record of a process that exits on its own. On
/// `1b593ba` the reaper blocked on the console mutex before publishing anything.
#[cfg(feature = "test-hooks")]
#[test]
fn a_held_console_slot_does_not_stop_the_death_record() {
    let _serial = serialized();
    let core = core();
    core.spawn(request("held-exit", &["cmd.exe"])).expect("spawn");
    conpty_session::test_hooks::hold_resize();
    let resizing = std::sync::Arc::clone(&core);
    let holder = std::thread::spawn(move || resizing.resize("held-exit", 100, 30, Duration::from_secs(20)));
    assert!(
        conpty_session::test_hooks::wait_until_resize_holds(Duration::from_secs(5)),
        "the resize never took the console slot"
    );
    // The child exits by itself while the slot is held: the reaper must publish on its close deadline.
    core.write("held-exit", b"exit 5\r\n", Duration::from_millis(50))
        .expect("write");
    let dead = wait_dead(&core, "held-exit", Duration::from_secs(6))
        .expect("the reaper never published under a held console slot");
    assert_eq!(dead.exit_code, Some(5), "{dead:?}");
    conpty_session::test_hooks::release_resize();
    let _ = holder.join();
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

/// SF3: an exit code learned after the death record went out must still reach the hub, whose only
/// exit-code source is the next drain. On `1b593ba` the second drain returned nothing.
#[cfg(feature = "test-hooks")]
#[test]
fn a_late_exit_code_reaches_the_next_drain() {
    let _serial = serialized();
    let core = core();
    core.spawn(request("late-code", &["cmd.exe"])).expect("spawn");
    // The shape of a reader panic: crashed and dead, no code, process still alive.
    session_of(&core, "late-code").mark_crashed_for_test("reader thread panicked");
    let first = wait_dead(&core, "late-code", Duration::from_secs(3)).expect("no death record");
    assert_eq!(first.exit_code, None, "{first:?}");
    let code = core.kill("late-code", Duration::from_millis(2000)).expect("kill");
    assert!(code.is_some(), "the kill learned no code: {code:?}");
    let carried = wait_dead(&core, "late-code", Duration::from_secs(2))
        .expect("the late exit code never reached a drain record");
    assert_eq!(carried.exit_code, code, "{carried:?}");
    core.shutdown();
}

fn job_tree_alive(pid: u32) -> bool {
    conpty_session::job_tree::process_alive(pid)
}

/// The session behind an id, for the tests that must put it in a state a real thread panic would. A
/// `test-hooks` item: `session_for_test` only exists when the crate is built with that feature.
#[cfg(feature = "test-hooks")]
fn session_of(core: &Core, id: &str) -> std::sync::Arc<conpty_session::session::Session> {
    core.session_for_test(id).expect("session present")
}

#[allow(dead_code)]
fn environment() -> HashMap<String, String> {
    std::env::vars().collect()
}