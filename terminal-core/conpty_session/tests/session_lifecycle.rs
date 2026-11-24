//! Session mechanics that the gates in step 1 do not otherwise reach, each pinned to one review finding.
//!
//! Every case runs a console client headless through the system ConPTY — `cmd.exe`, or the crate's own
//! `detached_client` for the cases about a client that starts a console-less child: no window is opened
//! that a person would see.

use std::collections::HashMap;
use std::sync::{mpsc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use conpty_session::job_tree::{
    children_of, count_processes_named_by_parent, current_handle_count, process_name,
};
use conpty_session::{Core, CoreError, CoreOptions, JobPolicy, SessionFlags, SpawnRequest};

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
    .expect("core")
}

fn request(id: &str, argv: &[&str]) -> SpawnRequest {
    SpawnRequest {
        session_id: id.to_string(),
        argv: argv.iter().map(|arg| arg.to_string()).collect(),
        cwd: None,
        env: std::env::vars().collect(),
        cols: 80,
        rows: 24,
        flags: conpty_session::SessionFlags::default(),
        seed_history: None,
        ws_prefix: None,
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

    let tail = core
        .raw_tail("natural", 4096, Duration::from_millis(50))
        .unwrap();
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
    )
    .expect("core");
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

/// The same request with a job policy: `SessionFlags` carries it, and `conpty::spawn` reads it before the
/// child is assigned, so the job exists with the policy's limit flags.
fn request_with_policy(id: &str, argv: &[&str], job_policy: JobPolicy) -> SpawnRequest {
    SpawnRequest {
        flags: SessionFlags {
            job_policy,
            ..SessionFlags::default()
        },
        ..request(id, argv)
    }
}

/// The crate's test client (`src/bin/detached_client.rs`): a session client that starts one child with
/// `DETACHED_PROCESS` and waits for it.
const DETACHED_CLIENT: &str = env!("CARGO_BIN_EXE_detached_client");

/// That client, asked for a long-lived child in its own service mode: `[client, program, args...]` — the
/// same executable started again with no console, which is the shape of `doubao.exe` starting
/// `doubao.exe --type=service`, the process `AttachConsole` finds console-less (`ERROR_INVALID_HANDLE`)
/// and which therefore outlives the session's pseudoconsole by itself. Only a job object can reach such a
/// child, which is exactly what these two cases measure.
const DETACHED_CLIENT_WITH_A_SERVICE: [&str; 4] = [DETACHED_CLIENT, DETACHED_CLIENT, "--sleep", "30"];

/// The pid the client announced on the session's console (`detached-pid <pid>`): the exact child under
/// test, read out of the session's own output instead of guessed from a snapshot.
///
/// Matched as a substring, not token by token: ConPTY prepends its own escape sequences
/// (`ESC[?25l ESC[2J ESC[m ESC[H` and the like) straight onto the first character of the tail, so the
/// marker can arrive glued to `\x1b[...` on its left, and trailing escapes can touch the digits on the
/// right. The digits are therefore the run of ASCII digits that follows the marker, and nothing after
/// them is parsed.
fn announced_pid(tail: &str) -> Option<u32> {
    let digits: String = tail
        .split("detached-pid ")
        .nth(1)?
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .collect();
    digits.parse().ok()
}

/// The client's detached child, once the client has announced it, with the model's facts checked: the
/// process runs, its image is the client's own executable (the service mode of the same binary), and it is
/// a child of the session's client — so a job that owned the client would own it too, which is the premise
/// both cases measure.
fn detached_child_of(core: &Core, id: &str, client: u32) -> u32 {
    let exe = std::path::Path::new(DETACHED_CLIENT)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default()
        .to_string();
    let end = Instant::now() + Duration::from_secs(15);
    while Instant::now() < end {
        let tail = core
            .raw_tail(id, 4096, Duration::from_millis(20))
            .unwrap_or_default();
        if let Some(child) = announced_pid(&tail) {
            assert!(
                job_tree_alive(child),
                "the child {child} the client announced is not running"
            );
            assert!(
                process_name(child).is_some_and(|name| name.eq_ignore_ascii_case(&exe)),
                "the announced pid {child} is not {exe:?}"
            );
            assert!(
                child != client && children_of(client).contains(&child),
                "the announced pid {child} is not a child of the client {client}: the case would not model \
                 a process the client starts"
            );
            return child;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("session {id}: client {client} never announced the detached child it started");
}

/// The announcement survives the escape sequences ConPTY writes around it: the init sequence is glued to
/// the first character of the tail (`\x1b[?25l\x1b[2J\x1b[m\x1b[Hdetached-pid …`), and a cursor or colour
/// escape can follow the digits. Needs no console, so it runs everywhere, including hosts where ConPTY
/// itself is unavailable.
#[test]
fn the_announced_pid_survives_conpty_escapes() {
    assert_eq!(
        announced_pid("\x1b[?25l\x1b[2J\x1b[m\x1b[Hdetached-pid 4242\x1b[m"),
        Some(4242u32)
    );
    assert_eq!(announced_pid("C:\\> detached-pid 77\r\n"), Some(77u32));
    assert_eq!(announced_pid("\x1b[mdetached-pid 9\x1b[?25h"), Some(9u32));
    // Only the leading digits count, and a marker without digits is not an announcement.
    assert_eq!(announced_pid("detached-pid 1x2"), Some(1u32));
    assert_eq!(announced_pid("detached-pid \x1b[m"), None);
    assert_eq!(announced_pid("no announcement here"), None);
}

/// Ends the test's child on every path, so a failed assertion cannot leave a sleeping process behind.
struct KillChild(u32);

impl Drop for KillChild {
    fn drop(&mut self) {
        if self.0 != 0 {
            conpty_session::job_tree::terminate_tree(self.0);
        }
    }
}

/// Ends the core's sessions and stops its ticker on every path, panicking included.
///
/// A case that panics between `spawn` and `kill` would otherwise leave its client, its job and its
/// conhost alive for the rest of the run — and the counts these cases compare (`conhost.exe` by parent,
/// open handles) are process-wide, so one leaked console makes the *next* case fail. `kill_all` is what
/// terminates the clients (the job's kill-on-close only follows the last job handle, which the session's
/// own threads hold), and `shutdown` stops the ticker; the core is then dropped with the guard.
struct ShutdownOnDrop(std::sync::Arc<Core>);

impl Drop for ShutdownOnDrop {
    fn drop(&mut self) {
        let _ = self.0.kill_all(Duration::from_millis(2000));
        self.0.shutdown();
    }
}

/// `JobPolicy::ClientOnly`: `kill` ends the client and only the client. The child the client started is
/// outside the session's job — the policy sets `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`, so a child of a job
/// member is created outside it — and it is console-detached on top of that, so neither
/// `TerminateJobObject` nor `ClosePseudoConsole` can reach it. Surviving a `TerminateJobObject` is exactly
/// what "not a member of the job" means: that call ends every member synchronously.
#[test]
fn client_only_kills_the_client_and_spares_its_grandchild() {
    let _serial = serialized();
    let core = core();
    let _shutdown = ShutdownOnDrop(std::sync::Arc::clone(&core));
    let reply = core
        .spawn(request_with_policy(
            "client-only",
            &DETACHED_CLIENT_WITH_A_SERVICE,
            JobPolicy::ClientOnly,
        ))
        .expect("spawn");
    let client = reply.pid;
    let grandchild = detached_child_of(&core, "client-only", client);
    let _guard = KillChild(grandchild);

    core.kill("client-only", Duration::from_millis(2000))
        .expect("kill");
    let end = Instant::now() + Duration::from_secs(2);
    while job_tree_alive(client) && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        !job_tree_alive(client),
        "the client {client} survived kill under ClientOnly"
    );

    // A member of the job would already be gone; the settle window only keeps a dying process from being
    // read as alive.
    std::thread::sleep(Duration::from_millis(300));
    assert!(
        job_tree_alive(grandchild),
        "the grandchild {grandchild} died with the client: ClientOnly's job still owned it, or the \
         pseudo console still owned its console"
    );
}

/// `JobPolicy::KillTree` (the default): the same detached child is inside the session's job, so `kill`
/// takes it down with the client. Paired with the case above, the two runs differ in nothing but the
/// policy, which is what pins that the policy — not the console — decides what a kill reaches.
#[test]
fn kill_tree_still_terminates_the_clients_descendants() {
    let _serial = serialized();
    let core = core();
    let _shutdown = ShutdownOnDrop(std::sync::Arc::clone(&core));
    let reply = core
        .spawn(request("kill-tree-policy", &DETACHED_CLIENT_WITH_A_SERVICE))
        .expect("spawn");
    let client = reply.pid;
    let grandchild = detached_child_of(&core, "kill-tree-policy", client);
    let _guard = KillChild(grandchild);

    core.kill("kill-tree-policy", Duration::from_millis(2000))
        .expect("kill");
    let end = Instant::now() + Duration::from_secs(3);
    while job_tree_alive(grandchild) && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(!job_tree_alive(client), "the client {client} survived kill");
    assert!(
        !job_tree_alive(grandchild),
        "the grandchild {grandchild} outlived kill under KillTree: the job no longer owns the tree"
    );
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