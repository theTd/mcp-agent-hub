//! The terminal WebSocket server (§6.4): the ticket, the listener's accept loop, the wire order, input,
//! the resize request, slow clients and the three closes.
//!
//! Every case goes through a real loopback socket with tungstenite's own client, because the contract is
//! the wire, not an internal call. Sessions are `attach_text` + `feed` wherever a process is not the
//! subject, so a case costs milliseconds; the input case spawns a headless `cmd.exe`, which is what the
//! PTY write contract needs.

use std::io::Write;
#[cfg(feature = "test-hooks")]
use std::io::Read;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
#[cfg(feature = "test-hooks")]
use std::sync::MutexGuard;
use std::thread;
use std::time::{Duration, Instant};

use conpty_session::{
    Core, CoreError, CoreOptions, ResizeRequest, SessionFlags, SpawnRequest, TextRequest,
};
use tungstenite::error::Error as WsError;
use tungstenite::protocol::frame::coding::CloseCode;
use tungstenite::{Message, WebSocket};

use std::net::TcpStream;

use tungstenite::stream::MaybeTlsStream;

const IO_TIMEOUT: Duration = Duration::from_millis(300);
/// §5.2's `feed` deadline, the hub's own.
const FEED_DEADLINE: Duration = Duration::from_millis(50);

/// The write gate, the stall limit and the injected panic are process-wide test seams, and cargo runs
/// this binary's tests in parallel threads: the cases that use them take this lock so one case's gate
/// cannot park (or unpark) another's socket. Only the hook-enabled cases use a seam.
#[cfg(feature = "test-hooks")]
static SERIAL: Mutex<()> = Mutex::new(());

#[cfg(feature = "test-hooks")]
fn serialized() -> MutexGuard<'static, ()> {
    SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// `tungstenite::connect` wraps the TCP stream: `ws://` is `Plain`.
type Client = WebSocket<MaybeTlsStream<TcpStream>>;

fn core() -> Arc<Core> {
    Core::new(
        Box::new(|| false),
        Box::new(|_line: &str| {}),
        CoreOptions::default(),
    )
    .expect("core")
}

fn core_with_options(opts: CoreOptions, log: Arc<dyn Fn(&str) + Send + Sync>) -> Arc<Core> {
    Core::new(Box::new(|| false), Box::new(move |line: &str| log(line)), opts).expect("core")
}

fn text_request(id: &str, cols: u16, rows: u16) -> TextRequest {
    TextRequest {
        session_id: id.to_string(),
        cols,
        rows,
        flags: SessionFlags::default(),
        seed_history: None,
        seed: None,
    }
}

/// Connects with a freshly minted ticket for `id`, the way the console does. The TCP connect and the
/// handshake both carry the test's timeouts: `tungstenite::connect` would do them with none, and a busy
/// accept thread would then hang a case instead of failing it.
fn connect(core: &Core, id: &str) -> Client {
    let (url, expires_at) = core
        .ws_ticket(id, Duration::from_secs(30))
        .expect("a ticket for a live id");
    assert!(expires_at > 0);
    let (authority, _path) = url
        .strip_prefix("ws://")
        .expect("a ws url")
        .split_once('/')
        .expect("host and path");
    let address: std::net::SocketAddr = authority.parse().expect("loopback host:port");
    let stream = TcpStream::connect_timeout(&address, IO_TIMEOUT).expect("the tcp connect");
    stream.set_read_timeout(Some(IO_TIMEOUT)).expect("read timeout");
    stream.set_write_timeout(Some(IO_TIMEOUT)).expect("write timeout");
    let stream = MaybeTlsStream::Plain(stream);
    let (ws, _response) = tungstenite::client::client_with_config(url.as_str(), stream, None)
        .expect("the handshake is accepted");
    ws
}

/// The client's own read/write timeouts, so a case never blocks on a socket that says nothing.
fn set_client_timeouts(ws: &Client) {
    if let MaybeTlsStream::Plain(stream) = ws.get_ref() {
        stream.set_read_timeout(Some(IO_TIMEOUT)).expect("read timeout");
        stream.set_write_timeout(Some(IO_TIMEOUT)).expect("write timeout");
    }
}

/// Reads one message, retrying through the poll timeout; `None` when the deadline passes.
fn next_message(ws: &mut Client, deadline: Instant) -> Option<Message> {
    loop {
        match ws.read() {
            Ok(message) => return Some(message),
            Err(WsError::Io(error))
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                ) =>
            {
                if Instant::now() >= deadline {
                    return None;
                }
            }
            Err(WsError::ConnectionClosed | WsError::AlreadyClosed) => return None,
            Err(error) => panic!("the socket failed: {error}"),
        }
    }
}

/// Everything the socket says until the text contains `needle`, or the deadline passes. The replay and
/// `replay_end` are returned with it, so the order can be asserted on one string.
fn read_until(ws: &mut Client, needle: &str, timeout: Duration) -> String {
    let deadline = Instant::now() + timeout;
    let mut seen = String::new();
    while !seen.contains(needle) {
        let Some(message) = next_message(ws, deadline) else {
            return seen;
        };
        match message {
            Message::Text(text) => seen.push_str(text.as_str()),
            Message::Binary(bytes) => seen.push_str(&String::from_utf8_lossy(&bytes)),
            Message::Close(_) => return seen,
            _ => {}
        }
    }
    seen
}

/// The close code the socket ends with, and every text frame before it.
fn read_to_close(ws: &mut Client, timeout: Duration) -> (u16, String) {
    let deadline = Instant::now() + timeout;
    let mut seen = String::new();
    loop {
        let Some(message) = next_message(ws, deadline) else {
            panic!("the socket was never closed; it said {seen:?}");
        };
        match message {
            Message::Text(text) => seen.push_str(text.as_str()),
            Message::Close(frame) => {
                let code = frame
                    .map(|frame| u16::from(frame.code))
                    .unwrap_or(CloseCode::Normal.into());
                return (code, seen);
            }
            _ => {}
        }
    }
}

/// The socket's text frames until at least one has arrived, or the deadline passes.
fn read_some_frames(ws: &mut Client, timeout: Duration) -> String {
    let deadline = Instant::now() + timeout;
    let mut seen = String::new();
    while seen.is_empty() {
        match next_message(ws, deadline) {
            Some(Message::Text(text)) => seen.push_str(text.as_str()),
            Some(_) => {}
            None => break,
        }
    }
    seen
}

/// Joins a test thread with a deadline: a case must fail, never hang, when the code under it stalls.
fn join_within<T>(handle: thread::JoinHandle<T>, timeout: Duration, what: &str) -> T {
    let deadline = Instant::now() + timeout;
    while !handle.is_finished() {
        if Instant::now() >= deadline {
            panic!("{what} did not finish within {timeout:?}");
        }
        thread::sleep(Duration::from_millis(5));
    }
    handle.join().expect("the thread panicked")
}

/// Waits until the collected log carries `needle`, and returns all of it either way, so a failing
/// assertion can print what was actually logged.
fn wait_for_log(lines: &Arc<Mutex<Vec<String>>>, needle: &str, timeout: Duration) -> String {
    let deadline = Instant::now() + timeout;
    loop {
        let logged = lines
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .join("\n");
        if logged.contains(needle) || Instant::now() >= deadline {
            return logged;
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn drain_until(core: &Core, mut predicate: impl FnMut(&conpty_session::DrainRecord) -> bool) -> Option<conpty_session::DrainRecord> {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        for record in core.drain() {
            if predicate(&record) {
                return Some(record);
            }
        }
        thread::sleep(Duration::from_millis(5));
    }
    None
}

/// §6.4 "Frames": the replay, `replay_end`, then the session's frames — every fed byte exactly once and
/// in order (Q11), even though the socket attaches while the session is printing.
#[test]
fn the_replay_then_replay_end_then_every_frame_exactly_once() {
    let core = core();
    core.attach_text(text_request("order", 40, 8)).expect("attach_text");
    let mut ws = connect(&core, "order");
    let opened = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(opened.contains("{\"type\":\"replay_end\"}"), "{opened:?}");

    // Feed from another thread while the socket reads: the attach happened before this, so every byte
    // is either in the replay (none of it) or in a frame.
    let feed = {
        let core = Arc::clone(&core);
        thread::spawn(move || {
            for index in 0..400 {
                core.feed("order", &format!("line {index}\r\n"), FEED_DEADLINE).expect("feed");
            }
        })
    };
    join_within(feed, Duration::from_secs(15), "the feeder thread");
    let expected: String = (0..400).map(|index| format!("line {index}\r\n")).collect();
    let seen = read_until(&mut ws, &expected[expected.len() - 12..], Duration::from_secs(5));
    assert_eq!(seen, expected, "the frames were not the fed bytes, once and in order");
    core.shutdown();
}

/// The same rule under a storm: a socket that connects *during* the feed sees the ring as it stands and
/// then every later frame, with no byte in both and none missing.
#[test]
fn a_socket_that_connects_during_a_write_storm_loses_nothing() {
    let core = core();
    core.attach_text(text_request("storm", 40, 8)).expect("attach_text");
    let stop = Arc::new(AtomicBool::new(false));
    let feeder = {
        let core = Arc::clone(&core);
        let stop = Arc::clone(&stop);
        thread::spawn(move || {
            let mut index = 0;
            while !stop.load(Ordering::Acquire) {
                core.feed("storm", &format!("{index}\r\n"), FEED_DEADLINE).expect("feed");
                index += 1;
            }
            index
        })
    };
    thread::sleep(Duration::from_millis(30));
    // Scheduling under a loaded suite is not a clock: wait for the ring to hold something before
    // connecting, or the attach's replay is legitimately empty and the case proves nothing.
    let warm = Instant::now() + Duration::from_secs(2);
    while Instant::now() < warm
        && core
            .raw("storm", Duration::from_millis(50))
            .map(|raw| raw.text.is_empty())
            .unwrap_or(true)
    {
        thread::sleep(Duration::from_millis(5));
    }
    let mut ws = connect(&core, "storm");
    let seen = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    let replay = seen
        .trim_end_matches("{\"type\":\"replay_end\"}")
        .to_string();
    // The replay must be a run of whole lines, and the frames must continue that run.
    let (replay_lines, frames) = (replay, read_some_frames(&mut ws, Duration::from_secs(2)));
    stop.store(true, Ordering::Release);
    let fed = join_within(feeder, Duration::from_secs(15), "the feeder thread");
    assert!(
        !frames.is_empty(),
        "no frame arrived after the replay while the feeder was still running"
    );

    let joined = format!("{replay_lines}{frames}");
    let numbers: Vec<u64> = joined
        .lines()
        .map(|line| line.trim().parse::<u64>().expect("a fed line"))
        .collect();
    assert!(!numbers.is_empty(), "the replay and the frames were both empty");
    for (index, number) in numbers.iter().enumerate() {
        assert_eq!(
            *number, index as u64,
            "the stream is not the fed lines in order: {numbers:?}"
        );
    }
    assert!(
        numbers.len() as u64 <= fed,
        "more lines arrived than were fed: {} > {fed}",
        numbers.len()
    );
    core.shutdown();
}

/// A1: a socket attached to the id keeps working through a resume and sees the banner between the old
/// output and the new.
#[test]
fn a_resume_banner_reaches_an_attached_socket() {
    let core = core();
    core.attach_text(text_request("carry", 40, 8)).expect("attach one");
    let mut ws = connect(&core, "carry");
    let opened = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(opened.contains("replay_end"), "{opened:?}");

    core.feed("carry", "OLD-OUTPUT", FEED_DEADLINE).expect("feed");
    let seen = read_until(&mut ws, "OLD-OUTPUT", Duration::from_secs(2));
    assert!(seen.contains("OLD-OUTPUT"), "{seen:?}");

    core.kill("carry", Duration::from_millis(100)).expect("kill");
    assert!(drain_until(&core, |record| !record.alive).is_some(), "no death record");
    core.attach_text(TextRequest {
        session_id: "carry".to_string(),
        cols: 40,
        rows: 8,
        flags: SessionFlags::default(),
        seed_history: None,
        seed: Some("RESUME-BANNER".to_string()),
    })
    .expect("attach two");
    let seen = read_until(&mut ws, "RESUME-BANNER", Duration::from_secs(2));
    assert!(seen.contains("RESUME-BANNER"), "the banner missed the attached socket: {seen:?}");
    core.feed("carry", "NEW-OUTPUT", FEED_DEADLINE).expect("feed");
    let seen = read_until(&mut ws, "NEW-OUTPUT", Duration::from_secs(2));
    assert!(seen.contains("NEW-OUTPUT"), "{seen:?}");
    assert_eq!(core.socket_count(), 1, "the socket survived the resume");
    core.shutdown();
}

/// §6.4 "Input": a browser frame reaches the PTY, and the OSC replies xterm.js generates never do.
#[test]
fn input_reaches_the_pty_and_osc_replies_do_not() {
    let core = core();
    core.spawn(SpawnRequest {
        session_id: "pty".to_string(),
        argv: vec!["cmd.exe".to_string()],
        cwd: None,
        env: std::env::vars().collect(),
        cols: 100,
        rows: 30,
        flags: SessionFlags::default(),
        seed_history: None,
        ws_prefix: None,
    })
    .expect("spawn");
    wait_for_text(&core, "pty", ">", Duration::from_secs(15));

    let mut ws = connect(&core, "pty");
    let opened = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(opened.contains("replay_end"), "{opened:?}");

    // A colour reply wrapped around a command, as xterm.js writes it, then bare text.
    ws.send(Message::text(
        "{\"type\":\"input\",\"data\":\"\\u001b]11;rgb:0/0/0\\u0007echo wired-ok\\r\\n\"}",
    ))
    .expect("send input");
    let tail = wait_for_text(&core, "pty", "wired-ok", Duration::from_secs(10));
    assert!(
        !tail.contains("]11;rgb"),
        "the OSC reply reached the PTY and was echoed: {tail:?}"
    );
    ws.send(Message::text("echo bare-ok\r\n")).expect("send bare text");
    wait_for_text(&core, "pty", "bare-ok", Duration::from_secs(10));
    // No process of ours outlives the case.
    core.kill_all(Duration::from_millis(2000)).expect("kill_all");
    core.shutdown();
}

fn wait_for_text(core: &Core, id: &str, needle: &str, timeout: Duration) -> String {
    let deadline = Instant::now() + timeout;
    let mut tail = String::new();
    while Instant::now() < deadline {
        tail = core
            .raw(id, Duration::from_millis(50))
            .map(|raw| raw.text)
            .unwrap_or_default();
        if tail.contains(needle) {
            return tail;
        }
        thread::sleep(Duration::from_millis(10));
    }
    panic!("{needle:?} never reached the ring; tail was {tail:?}");
}

/// §6.4: a resize frame is recorded, not applied, and the record carries it exactly once. Two frames
/// between two drains keep their arrival order, because `propagate` travels with each one.
#[test]
fn a_resize_frame_becomes_a_drain_request_and_is_not_applied() {
    let core = core();
    core.attach_text(text_request("size", 40, 8)).expect("attach_text");
    let mut ws = connect(&core, "size");
    let opened = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(opened.contains("replay_end"), "{opened:?}");
    ws.send(Message::text(
        "{\"type\":\"resize\",\"cols\":140,\"rows\":44,\"propagate\":false}",
    ))
    .expect("send resize");
    ws.send(Message::text(
        "{\"type\":\"resize\",\"cols\":120,\"rows\":30,\"propagate\":true}",
    ))
    .expect("send resize");

    let mut seen: Vec<ResizeRequest> = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline && seen.len() < 2 {
        for record in core.drain() {
            seen.extend(record.resize_requests);
        }
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(
        seen,
        vec![
            ResizeRequest { cols: 140, rows: 44, propagate: false },
            ResizeRequest { cols: 120, rows: 30, propagate: true },
        ],
        "both requests, in arrival order, with their propagate flags"
    );
    // The core did not apply it: the screen still has the size the hub gave it.
    let screen = core.screen("size", 10, Duration::from_millis(50)).expect("screen");
    assert_eq!((screen.cols, screen.rows), (40, 8));
    // Taken once.
    assert!(
        core.drain().iter().all(|record| record.resize_requests.is_empty()),
        "a request was reported twice"
    );
    core.shutdown();
}

/// The three closes of §6.4: `close_sockets`, `retire` and `shutdown` all end the socket with 1000.
/// Two sockets of one id are both asked and both closed; a respawn of a retired id starts without them.
#[test]
fn close_sockets_retire_and_shutdown_all_close_with_1000() {
    let core = core();
    core.attach_text(text_request("closes", 40, 8)).expect("attach_text");

    let mut first = connect(&core, "closes");
    read_until(&mut first, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    let mut sibling = connect(&core, "closes");
    read_until(&mut sibling, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert_eq!(core.socket_count(), 2);
    assert_eq!(core.close_sockets("closes"), 2, "both sockets were asked to close");
    let (code, _) = read_to_close(&mut first, Duration::from_secs(3));
    assert_eq!(code, 1000);
    let (code, _) = read_to_close(&mut sibling, Duration::from_secs(3));
    assert_eq!(code, 1000, "the second socket of the id closes too");
    assert_eq!(core.close_sockets("closes"), 0, "a closed socket is not asked twice");

    // `retire` closes the id's sockets too (it already closes its queues).
    let mut second = connect(&core, "closes");
    read_until(&mut second, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    core.kill("closes", Duration::from_millis(100)).expect("kill");
    assert!(drain_until(&core, |record| !record.alive).is_some(), "no death record");
    assert!(core.retire("closes").expect("retire"));
    let (code, _) = read_to_close(&mut second, Duration::from_secs(3));
    assert_eq!(code, 1000);

    // A respawn of the retired id starts fresh: the old socket stays closed and receives nothing.
    core.attach_text(text_request("closes", 40, 8)).expect("attach_text again");
    let mut successor = connect(&core, "closes");
    read_until(&mut successor, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert_eq!(core.socket_count(), 1, "only the successor's socket is registered");
    core.feed("closes", "AFTER-RESPAWN\n", FEED_DEADLINE).expect("feed");
    assert!(
        read_until(&mut successor, "AFTER-RESPAWN", Duration::from_secs(2)).contains("AFTER-RESPAWN"),
        "the successor's socket did not get the new output"
    );
    // The retired socket was closed with 1000 and is gone from the registry, not reused.
    assert!(core.close_sockets("closes") >= 1);

    // `shutdown` closes what is left and ends the listener.
    core.attach_text(text_request("last", 40, 8)).expect("attach_text");
    let mut third = connect(&core, "last");
    read_until(&mut third, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    let started = Instant::now();
    core.shutdown();
    assert!(started.elapsed() < Duration::from_secs(3), "shutdown outlived its budget");
    let (code, _) = read_to_close(&mut third, Duration::from_secs(3));
    assert_eq!(code, 1000);
    assert!(!core.ws_listening(), "the accept thread ended");
    assert_eq!(core.socket_count(), 0);
}

/// §6.4: the ticket is the handshake's only credential. A broken one is an HTTP 4xx and no socket; a
/// second use of a good one is refused the same way.
#[test]
fn a_tampered_or_reused_ticket_is_refused_at_the_handshake() {
    let core = core();
    core.attach_text(text_request("ticket", 40, 8)).expect("attach_text");
    let (url, _) = core.ws_ticket("ticket", Duration::from_secs(30)).expect("ticket");

    let mut tampered = url.clone();
    let last = tampered.pop().expect("a token");
    tampered.push(if last == 'A' { 'B' } else { 'A' });
    let refusal = tungstenite::connect(tampered.as_str()).expect_err("a broken ticket must not connect");
    match refusal {
        WsError::Http(response) => {
            assert_eq!(response.status().as_u16(), 401, "a bad MAC is 401");
        }
        other => panic!("expected an HTTP refusal, got {other}"),
    }

    // A valid ticket works once, and only once.
    let (mut ws, _response) =
        tungstenite::connect(url.as_str()).expect("the handshake is accepted");
    set_client_timeouts(&ws);
    read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    let refusal = tungstenite::connect(url.as_str()).expect_err("a used ticket must not connect");
    match refusal {
        WsError::Http(response) => assert_eq!(response.status().as_u16(), 401),
        other => panic!("expected an HTTP refusal, got {other}"),
    }
    assert_eq!(core.socket_count(), 1, "only the honest socket exists");
    core.shutdown();
}

/// §6.4: an id the core never had closes with the code `ws_pty_endpoint` used (1008), not an HTTP
/// error; `ws_ticket` itself refuses an unknown id.
#[test]
fn an_unknown_session_closes_with_1008_and_takes_no_ticket() {
    let core = core();
    assert!(matches!(
        core.ws_ticket("no-such-session", Duration::from_secs(30)),
        Err(CoreError::UnknownSession(_))
    ));
    core.attach_text(text_request("gone", 40, 8)).expect("attach_text");
    let (url, _) = core.ws_ticket("gone", Duration::from_secs(30)).expect("ticket");
    core.kill("gone", Duration::from_millis(100)).expect("kill");
    assert!(drain_until(&core, |record| !record.alive).is_some());
    assert!(core.retire("gone").expect("retire"));

    let (mut ws, _response) =
        tungstenite::connect(url.as_str()).expect("the handshake itself is valid");
    set_client_timeouts(&ws);
    let (code, _) = read_to_close(&mut ws, Duration::from_secs(3));
    assert_eq!(code, 1008, "an id with no core entry is the endpoint's 1008");
    core.shutdown();
}

/// §6.4/master `4b06139`: a failed accept is that connection's, and the next one is served.
#[cfg(feature = "test-hooks")]
#[test]
fn a_failed_accept_keeps_the_listener_serving() {
    let lines: Arc<std::sync::Mutex<Vec<String>>> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let collected = Arc::clone(&lines);
    let core = core_with_options(
        CoreOptions::default(),
        Arc::new(move |line: &str| {
            collected
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(line.to_string())
        }),
    );
    core.attach_text(text_request("accept", 40, 8)).expect("attach_text");
    let (url, _) = core.ws_ticket("accept", Duration::from_secs(30)).expect("ticket");

    // One accept fails, as a client that drops mid-accept makes it fail; the connection that fails is
    // never served.
    core.fail_next_accepts(1);
    let refused = tungstenite::connect(url.as_str());
    assert!(refused.is_err(), "the injected failure served a connection anyway");

    // The listener survived: `connect` mints a fresh ticket for the next attempt. The ticket the
    // injected failure's connection carried was never read, so it was never consumed.
    let mut ws = connect(&core, "accept");
    let opened = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(5));
    assert!(
        opened.contains("replay_end"),
        "the connection after the failure was not served: {opened:?}"
    );
    assert!(core.ws_accept_failures() >= 1, "the injected failure did not reach the loop");
    assert!(core.ws_listening(), "one connection's failure ended the listener");
    let logged = lines.lock().unwrap_or_else(|error| error.into_inner()).join("\n");
    assert!(logged.contains("ws accept failed"), "the first failure was not logged: {logged}");
    core.shutdown();
}

/// §5.4/§9: one slow socket's loss is counted on **its** handle and nowhere else.
#[cfg(feature = "test-hooks")]
#[test]
fn a_slow_socket_drops_on_its_own_handle_only() {
    let _serial = serialized();
    let core = core_with_options(
        CoreOptions {
            // Small enough that a handful of feeds overflows it while the writer is parked.
            sink_queue_bytes: 1024,
            ..CoreOptions::default()
        },
        Arc::new(|_line: &str| {}),
    );
    core.attach_text(text_request("slow", 40, 8)).expect("attach_text");
    conpty_session::test_hooks::hold_socket_writes();
    let mut ws = connect(&core, "slow");
    let opened = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(opened.contains("replay_end"), "{opened:?}");
    assert!(
        conpty_session::test_hooks::wait_until_socket_write_held(Duration::from_secs(2)),
        "the socket never parked its write"
    );

    for index in 0..40 {
        core.feed(
            "slow",
            &format!("frame {index} padded out to a few dozen bytes\r\n"),
            FEED_DEADLINE,
        )
        .expect("feed");
    }
    let record = drain_until(&core, |record| !record.socket_drops.is_empty())
        .expect("no record reported the socket's drops");
    assert_eq!(record.socket_drops.len(), 1, "one socket, one handle: {:?}", record.socket_drops);
    let (handle, dropped) = record.socket_drops[0];
    assert!(handle > 0);
    assert!(dropped > 0, "the parked socket dropped nothing");
    assert_eq!(
        record.dropped_bytes, 0,
        "a slow socket's loss must not be added to the session's own count"
    );
    conpty_session::test_hooks::release_socket_writes();
    core.shutdown();
}

/// §6.4: a socket that takes nothing for the stall limit is closed with 1000, and its thread ends.
#[cfg(feature = "test-hooks")]
#[test]
fn a_socket_that_takes_nothing_is_closed_with_1000() {
    let _serial = serialized();
    let core = core();
    core.set_socket_stall_limit(Duration::from_millis(150));
    core.attach_text(text_request("stalled", 40, 8)).expect("attach_text");
    conpty_session::test_hooks::hold_socket_writes();
    let mut ws = connect(&core, "stalled");
    let opened = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(opened.contains("replay_end"), "{opened:?}");
    assert!(
        conpty_session::test_hooks::wait_until_socket_write_held(Duration::from_secs(2)),
        "the socket never parked its write"
    );

    core.feed("stalled", "a frame the socket will not take\n", FEED_DEADLINE)
        .expect("feed");
    let (code, _) = read_to_close(&mut ws, Duration::from_secs(5));
    assert_eq!(code, 1000, "a stalled socket is closed with 1000");
    conpty_session::test_hooks::release_socket_writes();
    let deadline = Instant::now() + Duration::from_secs(3);
    while core.socket_count() > 0 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(core.socket_count(), 0, "the stalled socket's thread did not end");
    core.shutdown();
}

/// §6.4 "Input": a socket's input to a **text-only** session is accepted and discarded. No hub
/// session is a text one since DSH was dropped (2026-11-15), and the core never echoes, so every
/// frame form must be consumed without reaching the screen or a drain record: bare text, JSON input,
/// an OSC-only reply, an unknown type, and a `{` that is not JSON.
#[test]
fn text_session_input_is_discarded_not_echoed_or_recorded() {
    let core = core();
    core.attach_text(text_request("virtual", 40, 8)).expect("attach_text");
    let mut ws = connect(&core, "virtual");
    read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    ws.send(Message::text("hello ")).expect("bare text");
    ws.send(Message::text(
        "{\"type\":\"input\",\"data\":\"\\u001b]11;rgb:0/0/0\\u0007world\"}",
    ))
    .expect("json input");
    // Not input at all: an unknown type is ignored, and a `{` that is not JSON is typed literally, the
    // same rule the Python endpoint had.
    ws.send(Message::text("{\"type\":\"nope\",\"data\":\"ignored\"}"))
        .expect("unknown type");
    ws.send(Message::text("{\"type\":\"input\",\"data\":\"x\""))
        .expect("truncated json");
    ws.send(Message::text(r#"{"type":"input","data":7}"#)).expect("numeric data");

    // The core has nothing to deliver: no screen echo, no pending record, and the socket is still
    // attached (the frames were consumed, not refused).
    thread::sleep(Duration::from_millis(150));
    let screen = core.screen("virtual", 10, Duration::from_millis(100)).expect("screen");
    for typed in ["hello", "world", "ignored"] {
        assert!(
            !screen.text.contains(typed),
            "the core echoed input it does not own ({typed}): {:?}",
            screen.text
        );
    }
    assert_eq!(core.socket_count(), 1, "a text-session input frame closed its socket");
    core.shutdown();
}

/// §6.4 (gap 10): a pane that prints nothing keeps its socket; only a queue that stops moving is a
/// stall. The ping is a second direction of proof: the peer is still answered.
#[cfg(feature = "test-hooks")]
#[test]
fn an_idle_pane_keeps_its_socket() {
    let core = core();
    core.set_socket_stall_limit(Duration::from_millis(150));
    core.attach_text(text_request("idle", 40, 8)).expect("attach_text");
    let mut ws = connect(&core, "idle");
    read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));

    // Three stall limits with nothing flowing at all.
    thread::sleep(Duration::from_millis(500));
    assert_eq!(core.socket_count(), 1, "an idle pane's socket was closed");

    ws.send(Message::Ping(vec![1, 2, 3].into())).expect("ping");
    let reply = next_message(&mut ws, Instant::now() + Duration::from_secs(2)).expect("a pong");
    assert!(matches!(reply, Message::Pong(_)), "expected a pong, got {reply:?}");

    core.feed("idle", "STILL-HERE\n", FEED_DEADLINE).expect("feed");
    assert!(
        read_until(&mut ws, "STILL-HERE", Duration::from_secs(2)).contains("STILL-HERE"),
        "the idle socket stopped receiving frames"
    );
    core.shutdown();
}

/// §5.4/§9 step 4 (gap 10): a socket that detaches between two drains reports its final count once,
/// so a slow pane's loss cannot disappear with it.
#[cfg(feature = "test-hooks")]
#[test]
fn a_detached_socket_reports_its_final_drops() {
    let _serial = serialized();
    let debug_lines: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let collected = Arc::clone(&debug_lines);
    let core = core_with_options(
        CoreOptions {
            sink_queue_bytes: 1024,
            ..CoreOptions::default()
        },
        Arc::new(move |line: &str| {
            collected
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(line.to_string())
        }),
    );
    core.attach_text(text_request("detached", 40, 8)).expect("attach_text");
    conpty_session::test_hooks::hold_socket_writes();
    let mut ws = connect(&core, "detached");
    read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(
        conpty_session::test_hooks::wait_until_socket_write_held(Duration::from_secs(2)),
        "the socket never parked its write"
    );
    for index in 0..40 {
        core.feed(
            "detached",
            &format!("frame {index} padded out to a few dozen bytes\r\n"),
            FEED_DEADLINE,
        )
        .expect("feed");
    }
    // No drain before the socket leaves: its count is only in the registry until then. The wait is the
    // ticker's 50 ms flush: the feeds are still in the coalescer's batch, and a socket closed before
    // that flush reaches the queue has nothing to have dropped.
    thread::sleep(Duration::from_millis(200));
    conpty_session::test_hooks::release_socket_writes();
    assert_eq!(core.close_sockets("detached"), 1);
    let (code, _) = read_to_close(&mut ws, Duration::from_secs(3));
    assert_eq!(code, 1000);
    let deadline = Instant::now() + Duration::from_secs(3);
    while core.socket_count() > 0 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(core.socket_count(), 0, "the socket did not leave the registry");

    let record = match drain_until(&core, |record| !record.socket_drops.is_empty()) {
        Some(record) => record,
        None => {
            let logged = debug_lines.lock().unwrap_or_else(|e| e.into_inner()).join("\n");
            panic!("the detached socket's final drops never reached a record; log: {logged}")
        }
    };
    assert_eq!(record.socket_drops.len(), 1, "one socket, one handle");
    let (handle, dropped) = record.socket_drops[0];
    assert!(handle > 0 && dropped > 0, "dropped={dropped} handle={handle}");
    assert_eq!(record.dropped_bytes, 0, "a socket's loss is never the session's");
    core.shutdown();
}

/// §5.4/§9 step 4 (gap 10): sink handles are allocated per **id**, so two sessions' sockets can carry
/// the same handle number. The record therefore keys a socket by its session id plus that handle, and
/// each session reports only its own loss.
#[cfg(feature = "test-hooks")]
#[test]
fn two_sessions_can_share_a_sink_handle_number() {
    let _serial = serialized();
    let core = core_with_options(
        CoreOptions {
            sink_queue_bytes: 1024,
            ..CoreOptions::default()
        },
        Arc::new(|_line: &str| {}),
    );
    core.attach_text(text_request("left", 40, 8)).expect("attach left");
    core.attach_text(text_request("right", 40, 8)).expect("attach right");
    conpty_session::test_hooks::hold_socket_writes();
    let mut left = connect(&core, "left");
    read_until(&mut left, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    let mut right = connect(&core, "right");
    read_until(&mut right, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(
        conpty_session::test_hooks::wait_until_socket_write_held(Duration::from_secs(2)),
        "a socket never parked its write"
    );
    for index in 0..40 {
        let line = format!("frame {index} padded out to a few dozen bytes\r\n");
        core.feed("left", &line, FEED_DEADLINE).expect("feed left");
        core.feed("right", &line, FEED_DEADLINE).expect("feed right");
    }
    thread::sleep(Duration::from_millis(200));
    conpty_session::test_hooks::release_socket_writes();

    let mut drops: std::collections::HashMap<String, Vec<(u64, u64)>> = Default::default();
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline && drops.len() < 2 {
        for record in core.drain() {
            if !record.socket_drops.is_empty() {
                drops.insert(record.session_id, record.socket_drops);
            }
        }
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(drops.len(), 2, "each session reports its own socket: {drops:?}");
    for id in ["left", "right"] {
        assert_eq!(drops[id].len(), 1, "{id} reported one handle: {:?}", drops[id]);
        assert!(drops[id][0].1 > 0, "{id} reported no dropped bytes");
    }
    assert_eq!(
        drops["left"][0].0, drops["right"][0].0,
        "each id's fan-out numbers its handles, so the numbers collide: the record's key is the pair"
    );
    core.shutdown();
}

/// §6.4 (gap 11/12): one peer vanishing costs that socket only — the sibling keeps its frames and the
/// listener keeps accepting.
#[test]
fn a_socket_error_costs_one_socket_and_the_listener_keeps_serving() {
    let core = core();
    core.attach_text(text_request("errors", 40, 8)).expect("attach_text");
    let first = connect(&core, "errors");
    let mut first = first;
    read_until(&mut first, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    let mut second = connect(&core, "errors");
    read_until(&mut second, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert_eq!(core.socket_count(), 2);

    // The first peer's TCP connection ends under its thread: its next write fails, only it ends.
    drop(first);
    core.feed("errors", "STILL-SERVED\n", FEED_DEADLINE).expect("feed");
    assert!(
        read_until(&mut second, "STILL-SERVED", Duration::from_secs(3)).contains("STILL-SERVED"),
        "the surviving socket lost its frames"
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    while core.socket_count() > 1 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(core.socket_count(), 1, "the dead peer's socket was not removed");

    let mut third = connect(&core, "errors");
    let opened = read_until(&mut third, "{\"type\":\"replay_end\"}", Duration::from_secs(3));
    assert!(opened.contains("replay_end"), "the listener stopped accepting: {opened:?}");
    core.shutdown();
}

/// §6.3/§6.4 (gap 12): a panic in one socket's thread costs that socket, and the next pane still
/// connects. The panic is injected, and only into this case's session.
#[cfg(feature = "test-hooks")]
#[test]
fn a_panicking_socket_costs_one_socket() {
    let lines: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let collected = Arc::clone(&lines);
    let core = core_with_options(
        CoreOptions::default(),
        Arc::new(move |line: &str| {
            collected
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(line.to_string())
        }),
    );
    core.attach_text(text_request("panic", 40, 8)).expect("attach_text");
    conpty_session::test_hooks::panic_socket_writes_for("panic");
    let _ws = connect(&core, "panic");
    // A panicking socket dies without a close frame, and the RST can discard what it had already
    // written, so the log line is the proof: the thread caught the panic and closed that socket.
    let logged = wait_for_log(&lines, "panicked and was closed", Duration::from_secs(3));
    assert!(
        logged.contains("panicked and was closed"),
        "the panic was not logged: {logged}"
    );
    assert_eq!(core.socket_count(), 0, "the panicking socket was not removed");

    let mut second = connect(&core, "panic");
    let opened = read_until(&mut second, "{\"type\":\"replay_end\"}", Duration::from_secs(3));
    assert!(opened.contains("replay_end"), "the listener did not survive the panic");
    core.shutdown();
}

/// §6.4 (gap 13): the port is per core, so two hubs (4444 and 4445) cannot collide, and each core's
/// tickets and sessions belong to it alone.
#[test]
fn two_cores_get_different_ports_and_own_their_sessions() {
    let first = core();
    let second = core();
    assert_ne!(first.ws_port(), second.ws_port(), "two cores bound the same port");
    assert!(first.ws_port() > 0 && second.ws_port() > 0);
    assert!(first.ws_listening() && second.ws_listening());

    first.attach_text(text_request("only-first", 40, 8)).expect("attach_text");
    assert!(
        matches!(
            second.ws_ticket("only-first", Duration::from_secs(30)),
            Err(CoreError::UnknownSession(_))
        ),
        "a session of one core was visible to another"
    );
    // The other core's port refuses this core's ticket: it never minted it.
    let (url, _) = first.ws_ticket("only-first", Duration::from_secs(30)).expect("ticket");
    let foreign = url.replacen(
        &format!("127.0.0.1:{}", first.ws_port()),
        &format!("127.0.0.1:{}", second.ws_port()),
        1,
    );
    let refusal = tungstenite::connect(foreign.as_str()).expect_err("a foreign ticket must be refused");
    match refusal {
        WsError::Http(response) => assert_eq!(response.status().as_u16(), 401),
        other => panic!("expected an HTTP refusal, got {other}"),
    }
    first.shutdown();
    assert!(!first.ws_listening(), "a stopped accept thread reports not listening");
    assert!(second.ws_listening(), "the other core's listener is untouched");
    second.shutdown();
}

/// §6.4 (gap 14): the ticket is a credential. Neither a refused handshake's log line nor a served
/// socket's may contain it.
#[test]
fn the_ticket_never_reaches_a_log_line() {
    let lines: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let collected = Arc::clone(&lines);
    let core = core_with_options(
        CoreOptions::default(),
        Arc::new(move |line: &str| {
            collected
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(line.to_string())
        }),
    );
    core.attach_text(text_request("secret", 40, 8)).expect("attach_text");
    let (url, _) = core.ws_ticket("secret", Duration::from_secs(30)).expect("ticket");
    let token = url
        .split("ticket=")
        .nth(1)
        .expect("the url carries its token")
        .to_string();

    let mut tampered = url.clone();
    let last = tampered.pop().expect("a token");
    tampered.push(if last == 'A' { 'B' } else { 'A' });
    let _ = tungstenite::connect(tampered.as_str());
    let mut ws = connect(&core, "secret");
    read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert_eq!(core.close_sockets("secret"), 1);
    let _ = read_to_close(&mut ws, Duration::from_secs(3));

    let logged = lines.lock().unwrap_or_else(|error| error.into_inner()).join("\n");
    assert!(logged.contains("401"), "the refused handshake was not logged: {logged}");
    assert!(
        !logged.contains(&token),
        "the ticket reached a log line: {logged}"
    );
    core.shutdown();
}

/// A raw TCP connection to the core's listener, with nothing written on it.
fn raw_connect(core: &Core) -> TcpStream {
    let address: std::net::SocketAddr = format!("127.0.0.1:{}", core.ws_port())
        .parse()
        .expect("a loopback address");
    let stream = TcpStream::connect_timeout(&address, IO_TIMEOUT).expect("the raw connect");
    stream
        .set_read_timeout(Some(IO_TIMEOUT))
        .expect("a read timeout");
    stream
}

/// The first HTTP line each peer answered within `timeout`; an empty string for a peer that said
/// nothing (a handshake still waiting for its request). All peers are polled together, so N silent
/// peers cost one timeout, not N.
#[cfg(feature = "test-hooks")]
fn read_statuses(peers: &mut [TcpStream], timeout: Duration) -> Vec<String> {
    let mut seen: Vec<String> = peers.iter().map(|_| String::new()).collect();
    let mut done: Vec<bool> = peers.iter().map(|_| false).collect();
    for peer in peers.iter() {
        peer.set_nonblocking(true).expect("nonblocking");
    }
    let end = Instant::now() + timeout;
    let mut buffer = [0u8; 256];
    while Instant::now() < end && done.iter().any(|settled| !settled) {
        for (index, peer) in peers.iter_mut().enumerate() {
            if done[index] {
                continue;
            }
            match peer.read(&mut buffer) {
                Ok(0) => done[index] = true,
                Ok(count) => {
                    seen[index].push_str(&String::from_utf8_lossy(&buffer[..count]));
                    done[index] = seen[index].contains('\n');
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(_) => done[index] = true,
            }
        }
        thread::sleep(Duration::from_millis(5));
    }
    seen
}

/// The `/ws/pty/<id>` upgrade request a browser sends, for the peers that speak HTTP by hand.
fn upgrade_request(url: &str) -> String {
    let target = url.strip_prefix("ws://").expect("a ws url");
    let (authority, path) = target.split_once('/').expect("host and path");
    format!(
        "GET /{path} HTTP/1.1\r\nHost: {authority}\r\nUpgrade: websocket\r\n\
         Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\
         Sec-WebSocket-Version: 13\r\n\r\n"
    )
}

/// Brief item 1: the handshake runs off the accept thread, so a peer that connects and says nothing —
/// or dribbles its request — cannot stop a console attach. On `ba88183` the accept thread ran the
/// silent peer's handshake and this `connect` timed out.
#[test]
fn a_silent_or_dribbling_peer_does_not_block_a_console_attach() {
    let core = core();
    core.attach_text(text_request("slow-peer", 40, 8)).expect("attach_text");

    let silent = raw_connect(&core);
    let mut dribbling = raw_connect(&core);
    let (url, _) = core.ws_ticket("slow-peer", Duration::from_secs(30)).expect("ticket");
    let request = upgrade_request(&url);
    let writer = thread::spawn(move || {
        for byte in request.as_bytes() {
            if dribbling.write_all(&[*byte]).is_err() {
                return;
            }
            thread::sleep(Duration::from_millis(5));
        }
        // Keep the connection (and its slot) open while the attach below has to be served.
        thread::sleep(Duration::from_secs(1));
    });

    let mut ws = connect(&core, "slow-peer");
    let opened = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(
        opened.contains("replay_end"),
        "a hanging handshake blocked the console attach: {opened:?}"
    );
    assert!(core.ws_listening(), "the listener is serving while the peers hang");
    drop(silent);
    join_within(writer, Duration::from_secs(10), "the dribbling peer");
    core.shutdown();
}

/// Brief item 1: the handshake cap refuses the excess at once (HTTP 503) instead of queueing it, and
/// the listener serves again once the slots are free.
#[cfg(feature = "test-hooks")]
#[test]
fn a_flood_of_half_open_handshakes_is_refused_at_the_cap() {
    let core = core();
    core.attach_text(text_request("cap", 40, 8)).expect("attach_text");
    // More peers than the listener's handshake cap, none of which sends a byte.
    let mut peers: Vec<TcpStream> = (0..24).map(|_| raw_connect(&core)).collect();
    let statuses = read_statuses(&mut peers, Duration::from_secs(2));
    let refused = statuses.iter().filter(|line| line.contains("503")).count();
    assert!(refused >= 1, "not one peer over the cap was refused: {statuses:?}");
    assert!(core.ws_listening(), "a full cap stopped the listener");
    drop(peers);

    // The slots come back as the peers leave, and the listener serves a real attach again.
    let deadline = Instant::now() + Duration::from_secs(10);
    while core.ws_pending_handshakes() > 0 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    let mut ws = connect(&core, "cap");
    let opened = read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(5));
    assert!(opened.contains("replay_end"), "the listener stopped serving: {opened:?}");
    core.shutdown();
}

/// §5.4/§6.4, brief item 2: the replay is a write like any other. A peer that completes the handshake
/// and then takes nothing must be closed after the stall limit; before this fix the watchdog only
/// looked at a non-empty frame queue, so the replay blocked a thread forever.
#[cfg(feature = "test-hooks")]
#[test]
fn a_peer_that_takes_no_replay_is_closed_after_the_stall_limit() {
    let _serial = serialized();
    let core = core();
    attach_with_a_large_ring(&core, "stalled-replay");
    core.set_socket_stall_limit(Duration::from_millis(200));

    // The handshake is all this client ever reads: the replay stays in tungstenite's buffer and in the
    // stream's own buffers, and the write path makes no progress.
    let (url, _) = core.ws_ticket("stalled-replay", Duration::from_secs(30)).expect("ticket");
    let (ws, _response) = tungstenite::connect(url.as_str()).expect("the handshake is accepted");
    // The 101 is written before the attach registers the socket, so the registration is polled for.
    let registered = Instant::now() + Duration::from_secs(2);
    while core.socket_count() == 0 && Instant::now() < registered {
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(core.socket_count(), 1);
    let deadline = Instant::now() + Duration::from_secs(8);
    while core.socket_count() > 0 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        core.socket_count(),
        0,
        "a peer that took none of its replay kept its socket past the stall limit"
    );
    drop(ws);
    core.shutdown();
}

/// Brief item 3: `close_sockets` and `shutdown` wake a socket blocked in a write and stay inside their
/// budgets; before this fix the blocked write ignored both and shutdown spent its whole budget on it.
#[cfg(feature = "test-hooks")]
#[test]
fn a_close_and_a_shutdown_wake_a_blocked_write() {
    let _serial = serialized();
    let core = core();
    attach_with_a_large_ring(&core, "blocked-write");

    let open_blocked = |core: &Core| {
        let (url, _) = core
            .ws_ticket("blocked-write", Duration::from_secs(30))
            .expect("ticket");
        let (ws, _response) =
            tungstenite::connect(url.as_str()).expect("the handshake is accepted");
        // Past one write slice, so the server's replay write is parked on this peer.
        thread::sleep(Duration::from_millis(400));
        assert_eq!(core.socket_count(), 1);
        ws
    };

    let peer = open_blocked(&core);
    let started = Instant::now();
    assert_eq!(core.close_sockets("blocked-write"), 1);
    assert!(
        started.elapsed() < Duration::from_millis(300),
        "close_sockets waited for the blocked write: {:?}",
        started.elapsed()
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    while core.socket_count() > 0 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(core.socket_count(), 0, "the blocked socket survived its close request");
    drop(peer);

    let peer = open_blocked(&core);
    let started = Instant::now();
    core.shutdown();
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "shutdown outlived its socket budget: {:?}",
        started.elapsed()
    );
    assert!(!core.ws_listening(), "the accept thread ended");
    assert_eq!(core.socket_count(), 0);
    drop(peer);
}

/// A session whose ring holds several megabytes, so its replay cannot fit any socket buffer: the peer
/// that never reads must block the writer.
#[cfg(feature = "test-hooks")]
fn attach_with_a_large_ring(core: &Core, id: &str) {
    core.attach_text(TextRequest {
        session_id: id.to_string(),
        cols: 40,
        rows: 8,
        flags: SessionFlags {
            raw_cap_bytes: 6 * 1024 * 1024,
            raw_slack_bytes: 64 * 1024,
            ..SessionFlags::default()
        },
        seed_history: None,
        seed: None,
    })
    .expect("attach_text");
    let line = format!("{}\n", "x".repeat(1023));
    for _ in 0..4096 {
        core.feed(id, &line, FEED_DEADLINE).expect("feed");
    }
}

/// Brief item 5: refused handshakes log the first and every hundredth of each status (the accept loop's
/// cadence), the status is the one the wire carries, and the token never appears.
#[test]
fn refused_handshakes_are_logged_at_the_accept_cadence() {
    let lines: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let collected = Arc::clone(&lines);
    let core = core_with_options(
        CoreOptions::default(),
        Arc::new(move |line: &str| {
            collected
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(line.to_string())
        }),
    );
    core.attach_text(text_request("cadence", 40, 8)).expect("attach_text");
    let (url, _) = core.ws_ticket("cadence", Duration::from_secs(30)).expect("ticket");
    let mut tampered = url.clone();
    let last = tampered.pop().expect("a token");
    tampered.push(if last == 'A' { 'B' } else { 'A' });

    let refusals = |lines: &Arc<Mutex<Vec<String>>>| {
        lines
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .iter()
            .filter(|line| line.contains("console socket refused"))
            .cloned()
            .collect::<Vec<_>>()
    };
    for _ in 0..3 {
        let _ = tungstenite::connect(tampered.as_str());
    }
    let logged = wait_for_log(&lines, "console socket refused", Duration::from_secs(3));
    let first = refusals(&lines);
    assert_eq!(first.len(), 1, "three refusals wrote {} lines: {logged}", first.len());
    assert!(first[0].contains("401"), "the wrong status was logged: {}", first[0]);

    for _ in 0..97 {
        let _ = tungstenite::connect(tampered.as_str());
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while refusals(&lines).len() < 2 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    let at_hundred = refusals(&lines);
    assert_eq!(at_hundred.len(), 2, "the hundredth refusal was not logged: {at_hundred:?}");
    assert!(at_hundred[1].contains("100"), "the line does not name the count: {}", at_hundred[1]);

    // A valid ticket for another id is the one 403, and it logs its own first line.
    let elsewhere = url.replacen("/ws/pty/cadence", "/ws/pty/elsewhere", 1);
    let refusal = tungstenite::connect(elsewhere.as_str()).expect_err("another id must be refused");
    match refusal {
        WsError::Http(response) => assert_eq!(response.status().as_u16(), 403),
        other => panic!("expected an HTTP refusal, got {other}"),
    }
    let deadline = Instant::now() + Duration::from_secs(3);
    while !refusals(&lines).iter().any(|line| line.contains("403")) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    let all = refusals(&lines);
    assert_eq!(
        all.iter().filter(|line| line.contains("403")).count(),
        1,
        "the 403 refusal was not logged with its status: {all:?}"
    );
    assert!(
        !all.iter().any(|line| line.contains("ticket=")),
        "a refusal line carried the token: {all:?}"
    );
    core.shutdown();
}

/// Brief item 6/5: a non-string `data` (Python wrote `str(data)`) is ignored and logged once per
/// socket; the string frames around it are untouched.
#[test]
fn a_non_string_data_frame_is_ignored_and_logged_once() {
    let lines: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let collected = Arc::clone(&lines);
    let core = core_with_options(
        CoreOptions::default(),
        Arc::new(move |line: &str| {
            collected
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(line.to_string())
        }),
    );
    core.attach_text(text_request("odd-data", 40, 8)).expect("attach_text");
    let mut ws = connect(&core, "odd-data");
    read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));

    ws.send(Message::text(r#"{"type":"input","data":7}"#)).expect("numeric data");
    ws.send(Message::text(r#"{"type":"input","data":[]}"#)).expect("array data");
    ws.send(Message::text(r#"{"type":"input","data":"kept"}"#)).expect("string data");

    wait_for_log(&lines, "was not a string", Duration::from_secs(2));
    let logged = lines.lock().unwrap_or_else(|error| error.into_inner()).join("\n");
    assert_eq!(
        logged.matches("was not a string").count(),
        1,
        "the frame was logged once per socket, not once per frame: {logged}"
    );
    core.shutdown();
}

/// Brief item 7: a drop that lands between a drain's record collection and its socket-drop take has no
/// record to ride in. It must stay unreported, so the next drain carries it; before this fix the take
/// marked every socket and the count was lost.
#[cfg(feature = "test-hooks")]
#[test]
fn a_drop_that_lands_during_a_drain_is_not_marked_reported() {
    let _serial = serialized();
    let core = core_with_options(
        CoreOptions {
            sink_queue_bytes: 512,
            ..CoreOptions::default()
        },
        Arc::new(|_line: &str| {}),
    );
    core.attach_text(text_request("drain-a", 40, 8)).expect("attach a");
    core.attach_text(text_request("drain-b", 40, 8)).expect("attach b");
    conpty_session::test_hooks::hold_socket_writes();
    let mut socket_b = connect(&core, "drain-b");
    read_until(&mut socket_b, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(
        conpty_session::test_hooks::wait_until_socket_write_held(Duration::from_secs(2)),
        "the socket never parked its write"
    );

    // A is the only session with output when the drain collects its records.
    core.feed("drain-a", "A\n", FEED_DEADLINE).expect("feed a");
    conpty_session::test_hooks::hold_drain_before_drops();
    let drainer = {
        let core = Arc::clone(&core);
        thread::spawn(move || core.drain())
    };
    assert!(
        conpty_session::test_hooks::wait_until_drain_held(Duration::from_secs(3)),
        "the drain never parked between its records and its drops"
    );
    // The drop lands now, after the scan: B has no record in this drain.
    for index in 0..40 {
        core.feed(
            "drain-b",
            &format!("frame {index} padded out to a few dozen bytes\r\n"),
            FEED_DEADLINE,
        )
        .expect("feed b");
    }
    conpty_session::test_hooks::release_drain_before_drops();
    let records = join_within(drainer, Duration::from_secs(5), "the drain");
    assert!(
        records.iter().any(|record| record.session_id == "drain-a"),
        "the drain carried no record for A: {records:?}"
    );
    assert!(
        !records.iter().any(|record| record.session_id == "drain-b"),
        "B had no reason to be in this drain: {records:?}"
    );

    // The count is still unreported: the next record for B carries it.
    let record = drain_until(&core, |record| {
        record.session_id == "drain-b" && !record.socket_drops.is_empty()
    })
    .expect("B's drop was marked reported with no record to carry it");
    assert!(record.socket_drops[0].1 > 0, "{record:?}");
    conpty_session::test_hooks::release_socket_writes();
    core.shutdown();
}

/// Brief item 5 (kimi 5): the Python log runs with no core lock held. A callback that calls back into
/// the core would deadlock the request backlog (which holds its mutex while appending) if the log ran
/// under it; before this fix the overflow logged inside the lock.
#[test]
fn a_log_callback_may_call_back_into_the_core() {
    let slot: Arc<Mutex<Option<Arc<Core>>>> = Arc::new(Mutex::new(None));
    let target = Arc::clone(&slot);
    let log = Arc::new(move |_line: &str| {
        let core = target
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone();
        if let Some(core) = core {
            // Both take a lock the caller could be holding: the registry and the drain path.
            let _ = core.socket_count();
            let _ = core.drain();
        }
    });
    let core = core_with_options(CoreOptions::default(), log);
    *slot.lock().unwrap_or_else(|error| error.into_inner()) = Some(Arc::clone(&core));
    core.attach_text(text_request("reentrant", 40, 8)).expect("attach_text");

    // One more than the backlog cap (1024) overflows it and logs.
    let run = {
        let core = Arc::clone(&core);
        thread::spawn(move || {
            for _ in 0..1100 {
                let _ = core.request_resize("reentrant", 80, 20, false);
            }
        })
    };
    join_within(run, Duration::from_secs(5), "the overflowing resize loop");
    core.shutdown();
}

/// Brief item 5 (kimi 5), the ticker's path: `check_stalls` decides under the registry lock and logs
/// after it. A callback that reads the registry would block the ticker forever if the log ran inside.
#[cfg(feature = "test-hooks")]
#[test]
fn a_stall_log_runs_without_the_registry_lock() {
    let _serial = serialized();
    let slot: Arc<Mutex<Option<Arc<Core>>>> = Arc::new(Mutex::new(None));
    let target = Arc::clone(&slot);
    let log = Arc::new(move |_line: &str| {
        let core = target
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone();
        if let Some(core) = core {
            let _ = core.socket_count();
        }
    });
    let core = core_with_options(CoreOptions::default(), log);
    *slot.lock().unwrap_or_else(|error| error.into_inner()) = Some(Arc::clone(&core));
    core.set_socket_stall_limit(Duration::from_millis(150));
    core.attach_text(text_request("stall-log", 40, 8)).expect("attach_text");
    conpty_session::test_hooks::hold_socket_writes();
    let mut ws = connect(&core, "stall-log");
    read_until(&mut ws, "{\"type\":\"replay_end\"}", Duration::from_secs(2));
    assert!(
        conpty_session::test_hooks::wait_until_socket_write_held(Duration::from_secs(2)),
        "the socket never parked its write"
    );
    core.feed("stall-log", "a frame the socket will not take\n", FEED_DEADLINE)
        .expect("feed");
    // The close reaches the peer only if the ticker survived its own log call.
    let (code, _) = read_to_close(&mut ws, Duration::from_secs(5));
    assert_eq!(code, 1000, "the stalled socket was not closed");
    conpty_session::test_hooks::release_socket_writes();
    core.shutdown();
}
