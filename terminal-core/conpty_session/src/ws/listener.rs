//! The terminal WebSocket listener (§6.4): one loopback port, one ticket check per handshake, one
//! thread per accepted socket.
//!
//! The core binds `127.0.0.1:0` and reports the port, so two hubs (4444 and 4445) cannot collide and no
//! Windows Firewall prompt appears. A failed `accept` is that one connection's failure (master
//! `4b06139`: on Windows a client that drops mid-accept fails the call): the loop logs the first and
//! every 100th, backs off 0/50/200 ms/1 s, and accepts again. Only `shutdown` ends it, and
//! [`WsServer::listening`] is false once it has.
//!
//! The handshake runs on this thread with a 2 s timeout: the ticket is verified there (an invalid one
//! gets an HTTP 4xx and no socket), and the session is looked up after it, because an unknown id must
//! close with `ws_pty_endpoint`'s code rather than an HTTP error.

use std::collections::HashMap;
use std::io;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use terminal_stream::FrameSink;
use tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tungstenite::http::{Response as HttpResponse, StatusCode};
use tungstenite::protocol::frame::coding::CloseCode;
use tungstenite::protocol::frame::CloseFrame;
use tungstenite::protocol::{WebSocket, WebSocketConfig};

use super::connection::{
    QueuedSink, SocketConnection, NORMAL_CLOSE, TRY_AGAIN_CLOSE, UNKNOWN_SESSION_CLOSE,
};
use super::ticket::{Minted, Tickets};
use crate::core::{Core, CoreError, SocketAttach};
use crate::frame_queue::FrameQueue;

/// The request must arrive within this, or the connection is another accept's problem.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
/// `windows_accept_guard`'s `RETRY_DELAYS`: the wait after the 1st, 2nd, 3rd and every later failure.
const RETRY_DELAYS: [Duration; 4] = [
    Duration::ZERO,
    Duration::from_millis(50),
    Duration::from_millis(200),
    Duration::from_secs(1),
];
/// A socket that accepts nothing for this long is closed with 1000 (§5.4, §6.4).
const STALL_LIMIT: Duration = Duration::from_secs(30);
/// After the close request, a socket whose write is still blocked is cut off: the peer is gone.
const STALL_CLOSE_GRACE: Duration = Duration::from_secs(1);
/// The path the hub's own endpoint used (`/ws/pty/<id>`), so the console's URL builder only gains a
/// ticket query.
pub(super) const PATH_PREFIX: &str = "/ws/pty/";
/// An input frame is keystrokes: the default 64 MiB ceiling would let one frame allocate freely.
const MAX_INBOUND_MESSAGE: usize = 1024 * 1024;

/// The listener, its ticket table and the sockets attached through it.
pub struct WsServer {
    local: SocketAddr,
    tickets: Tickets,
    connections: Mutex<HashMap<u64, Arc<SocketConnection>>>,
    next_seq: AtomicU64,
    /// The bound listener until `spawn_accept` moves it into the accept thread.
    pending_listener: Mutex<Option<TcpListener>>,
    stopping: Arc<AtomicBool>,
    listening: Arc<AtomicBool>,
    accept_thread: Mutex<Option<JoinHandle<()>>>,
    accept_failures: AtomicU64,
    log: Arc<dyn Fn(&str) + Send + Sync>,
    queue_bytes: usize,
    epoch: Instant,
    /// The stall limit, in milliseconds; the test hooks lower it so a 30 s case runs in a second.
    stall_limit_ms: AtomicU64,
    /// Accepts the test hooks make fail, each consumed by one accepted connection (§6.4).
    #[cfg(feature = "test-hooks")]
    injected_accept_failures: AtomicU64,
    /// The final drop count of sockets that left the registry between two drains, keyed by session id,
    /// so a slow pane's loss is still reported once after it detaches (§9 step 4). Merged into
    /// `take_socket_drops` and counted by `sessions_with_unreported_drops`.
    retired_drops: Mutex<HashMap<String, Vec<(u64, u64)>>>,
}

/// Why a connection was refused before it became a socket.
#[derive(Debug)]
struct Refused {
    status: u16,
    reason: String,
}

/// Detaches a sink whose socket thread never started, so an unwinding `serve` cannot leave a queue
/// registered on a session's fan-out that nothing reads. `disarm` marks the thread as owner.
struct PendingSink<F: FnOnce()> {
    detach: Option<F>,
}

impl<F: FnOnce()> PendingSink<F> {
    fn disarm(mut self) {
        self.detach = None;
    }
}

impl<F: FnOnce()> Drop for PendingSink<F> {
    fn drop(&mut self) {
        if let Some(detach) = self.detach.take() {
            detach();
        }
    }
}

impl Refused {
    fn new(status: u16, reason: impl Into<String>) -> Self {
        Self {
            status,
            reason: reason.into(),
        }
    }

    /// The HTTP answer tungstenite writes instead of a `101`: no socket exists for this connection.
    fn into_response(self) -> ErrorResponse {
        let status = StatusCode::from_u16(self.status).unwrap_or(StatusCode::UNAUTHORIZED);
        let mut response = HttpResponse::new(Some(self.reason));
        *response.status_mut() = status;
        response
    }
}

impl WsServer {
    /// Binds the loopback listener and mints the ticket key. `spawn_accept` starts the thread once the
    /// `Core` it serves exists. No ticket key means no trustworthy socket, so that is an error.
    pub fn start(
        log: Arc<dyn Fn(&str) + Send + Sync>,
        queue_bytes: usize,
    ) -> io::Result<Arc<Self>> {
        let listener = TcpListener::bind(("127.0.0.1", 0))?;
        let local = listener.local_addr()?;
        let tickets = Tickets::new()
            .map_err(|error| io::Error::other(format!("ticket key: {error}")))?;
        Ok(Arc::new(Self {
            local,
            tickets,
            connections: Mutex::new(HashMap::new()),
            next_seq: AtomicU64::new(1),
            pending_listener: Mutex::new(Some(listener)),
            stopping: Arc::new(AtomicBool::new(false)),
            listening: Arc::new(AtomicBool::new(false)),
            accept_thread: Mutex::new(None),
            accept_failures: AtomicU64::new(0),
            log,
            queue_bytes,
            epoch: Instant::now(),
            stall_limit_ms: AtomicU64::new(STALL_LIMIT.as_millis() as u64),
            #[cfg(feature = "test-hooks")]
            injected_accept_failures: AtomicU64::new(0),
            retired_drops: Mutex::new(HashMap::new()),
        }))
    }

    /// Moves the bound listener into the accept thread. Called once, right after the `Core` exists.
    pub fn spawn_accept(self: &Arc<Self>, core: Weak<Core>) -> io::Result<()> {
        let listener = self
            .pending_listener
            .lock()
            .ok()
            .and_then(|mut slot| slot.take())
            .ok_or_else(|| io::Error::other("the listener was already started"))?;
        self.listening.store(true, Ordering::Release);
        let server = Arc::clone(self);
        let handle = thread::Builder::new()
            .name("ws-accept".to_string())
            .spawn(move || server.accept_loop(listener, core))?;
        if let Ok(mut slot) = self.accept_thread.lock() {
            *slot = Some(handle);
        }
        Ok(())
    }

    /// The port the hub hands the console. Fixed for the process's life.
    pub fn port(&self) -> u16 {
        self.local.port()
    }

    /// Whether the accept thread is serving (§6.4: the hub's health check reports a listener that
    /// ended).
    pub fn listening(&self) -> bool {
        self.listening.load(Ordering::Acquire)
    }

    /// The URL for `session_id` and its expiry. The hub exposes it and nothing else about the ticket.
    pub fn ticket(&self, session_id: &str, ttl: Duration) -> Result<Minted, CoreError> {
        self.tickets.mint(session_id, ttl).map_err(CoreError::Internal)
    }

    /// The URL a console connects to, with the token in the query (`?ticket=`).
    pub fn ticket_url(&self, minted: &Minted, session_id: &str) -> String {
        format!(
            "ws://{}{}{}?ticket={}",
            self.local,
            PATH_PREFIX,
            encode_path_segment(session_id),
            minted.token
        )
    }

    /// Asks every socket of `id` to close with 1000 and reports how many it asked (§5.2).
    pub fn close_sockets(&self, session_id: &str) -> usize {
        let sockets: Vec<Arc<SocketConnection>> = {
            let connections = self.connections.lock().unwrap_or_else(|e| e.into_inner());
            connections
                .values()
                .filter(|connection| connection.session_id == session_id)
                .cloned()
                .collect()
        };
        let mut closed = 0;
        for connection in sockets {
            if connection.request_close(NORMAL_CLOSE) {
                closed += 1;
            }
        }
        closed
    }

    /// Per session, its sockets' cumulative dropped bytes keyed by sink handle, and marks each count as
    /// reported (§9 step 4: per handle, never summed into the session's `dropped_bytes`). Only handles
    /// with a drop are listed.
    pub fn take_socket_drops(&self) -> HashMap<String, Vec<(u64, u64)>> {
        // The retired sockets first, with no lock held across the two maps: a socket that finishes
        // between the two takes lands its count in the backlog, its session is dirtied by
        // `sessions_with_unreported_drops`, and the next drain carries it.
        let mut drops: HashMap<String, Vec<(u64, u64)>> = std::mem::take(
            &mut *self
                .retired_drops
                .lock()
                .unwrap_or_else(|e| e.into_inner()),
        );
        let connections = self.connections.lock().unwrap_or_else(|e| e.into_inner());
        for connection in connections.values() {
            let dropped = connection.dropped_bytes();
            if dropped > 0 {
                connection.mark_drops_reported(dropped);
                drops
                    .entry(connection.session_id.clone())
                    .or_default()
                    .push((connection.sink_handle(), dropped));
            }
        }
        drops
    }

    /// The sessions with a socket drop that no drain record has carried yet: a drop happens in the
    /// delivery path, so without this its session could stay clean and the hub would never hear about
    /// it (§5.4). A socket that detached with an unreported count counts too (`retired_drops`).
    pub fn sessions_with_unreported_drops(&self) -> Vec<String> {
        let mut sessions: Vec<String> = self
            .retired_drops
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .keys()
            .cloned()
            .collect();
        let connections = self.connections.lock().unwrap_or_else(|e| e.into_inner());
        for connection in connections.values() {
            if connection.dropped_bytes() > connection.reported_drops() {
                sessions.push(connection.session_id.clone());
            }
        }
        sessions.sort_unstable();
        sessions.dedup();
        sessions
    }

    /// How many sockets the server currently holds (tests and diagnostics).
    pub fn socket_count(&self) -> usize {
        self.connections
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .len()
    }

    /// The ticker's 5 ms pass: a socket whose queue holds bytes it has not written for the stall limit
    /// is asked to close with 1000, and one that is still blocked a second later is cut off.
    pub fn check_stalls(&self) {
        let now = self.now_ms();
        let limit = self.stall_limit_ms.load(Ordering::Acquire);
        let connections = self.connections.lock().unwrap_or_else(|e| e.into_inner());
        for connection in connections.values() {
            if !connection.has_pending() {
                continue;
            }
            if now.saturating_sub(connection.last_progress_ms()) <= limit {
                continue;
            }
            if connection.close_code() == 0 {
                connection.request_close(NORMAL_CLOSE);
                (self.log)(&format!(
                    "session {}: console socket {} took nothing for {} ms; closing it with 1000",
                    connection.session_id,
                    connection.seq,
                    limit
                ));
            } else if now.saturating_sub(connection.close_requested_ms())
                > STALL_CLOSE_GRACE.as_millis() as u64
            {
                // The peer accepted nothing, so it cannot read the close frame either: cut the socket
                // so the blocked write returns and the thread ends.
                connection.abort();
            }
        }
    }

    /// Closes every socket, ends the listener and joins the accept and socket threads inside `budget`
    /// (§6.4). A thread that misses the budget is cut off and logged; none is awaited forever.
    pub fn shutdown(&self, budget: Duration) {
        self.stopping.store(true, Ordering::Release);
        // A blocked `accept` has no cancel on Windows: connect to ourselves so it returns, then the
        // loop sees `stopping` and leaves.
        let _ = TcpStream::connect_timeout(&self.local, Duration::from_millis(100));
        let end = Instant::now() + budget;
        let accept = self.accept_thread.lock().ok().and_then(|mut slot| slot.take());
        if let Some(handle) = accept {
            wait_thread(&handle, end);
            // Join only a thread that has ended. One that missed the budget is left to finish on its
            // own (it is named `ws-accept`); an unbounded join here would hang the hub's shutdown.
            if handle.is_finished() {
                let _ = handle.join();
            } else {
                (self.log)("terminal core: the accept thread outlived shutdown's budget; left to end on its own");
            }
        }
        let connections: Vec<Arc<SocketConnection>> = {
            let map = self.connections.lock().unwrap_or_else(|e| e.into_inner());
            map.values().cloned().collect()
        };
        for connection in &connections {
            connection.request_close(NORMAL_CLOSE);
        }
        // Half the budget for the close handshakes, the rest for the cut-off sockets to notice.
        let cut_off_at = Instant::now() + budget / 2;
        for connection in &connections {
            if !connection.wait_finished(cut_off_at) {
                connection.abort();
            }
        }
        for connection in &connections {
            if !connection.wait_finished(end) {
                (self.log)(&format!(
                    "session {}: console socket {} outlived shutdown's budget",
                    connection.session_id, connection.seq
                ));
            }
            connection.join_finished();
        }
        self.connections
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clear();
    }

    /// Drops a finished socket from the registry; the socket's own thread calls it. Its final drop
    /// count moves to `retired_drops` first: the socket detaches in the same step, so a later drain can
    /// no longer read it, and a slow pane's loss would disappear with it (§9 step 4).
    pub(super) fn forget(&self, seq: u64) {
        let removed = self
            .connections
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&seq);
        let Some(connection) = removed else {
            return;
        };
        let dropped = connection.dropped_bytes();
        if dropped > connection.reported_drops() {
            self.retired_drops
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .entry(connection.session_id.clone())
                .or_default()
                .push((connection.sink_handle(), dropped));
        }
    }

    /// Milliseconds since the server started (monotonic, shared with the sockets).
    fn now_ms(&self) -> u64 {
        self.epoch.elapsed().as_millis() as u64
    }

    fn accept_loop(self: Arc<Self>, listener: TcpListener, core: Weak<Core>) {
        let mut failures: u64 = 0;
        loop {
            if self.stopping.load(Ordering::Acquire) {
                break;
            }
            // A client that drops mid-accept fails the call on Windows (WinError 64, master
            // `4b06139`); the test hook reproduces exactly that by discarding an accepted connection.
            let accepted = listener
                .accept()
                .and_then(|(stream, peer)| match self.injected_accept_failure() {
                    Some(error) => {
                        drop(stream);
                        Err(error)
                    }
                    None => Ok((stream, peer)),
                });
            match accepted {
                Ok((stream, _peer)) => {
                    failures = 0;
                    if self.stopping.load(Ordering::Acquire) {
                        // The connection `shutdown` made to wake this thread.
                        break;
                    }
                    // A panic while serving one connection must cost that connection, never the
                    // listener (§6.3, item 12): the thread catches it, logs it, and accepts again.
                    match catch_unwind(AssertUnwindSafe(|| self.serve(stream, &core))) {
                        Ok(Ok(())) => {}
                        Ok(Err(refused)) => (self.log)(&format!(
                            "terminal core: console socket refused: {} ({})",
                            refused.reason, refused.status
                        )),
                        Err(_) => {
                            self.accept_failures.fetch_add(1, Ordering::Relaxed);
                            (self.log)(
                                "terminal core: a console socket panicked while being set up and was \
                                 closed; the listener keeps accepting",
                            );
                        }
                    }
                }
                Err(error) => {
                    failures += 1;
                    self.accept_failures.fetch_add(1, Ordering::Relaxed);
                    // One client's failure never ends the listener (master `4b06139`): log the first
                    // and every hundredth, back off, and accept again.
                    if failures == 1 || failures.is_multiple_of(100) {
                        (self.log)(&format!(
                            "terminal core: ws accept failed ({failures} in a row): {error}"
                        ));
                    }
                    let delay = RETRY_DELAYS[(failures as usize - 1).min(RETRY_DELAYS.len() - 1)];
                    if !delay.is_zero() {
                        thread::sleep(delay);
                    }
                }
            }
        }
        self.listening.store(false, Ordering::Release);
    }

    #[cfg(feature = "test-hooks")]
    fn injected_accept_failure(&self) -> Option<io::Error> {
        let remaining = self.injected_accept_failures.load(Ordering::Acquire);
        if remaining == 0 {
            return None;
        }
        self.injected_accept_failures
            .store(remaining - 1, Ordering::Release);
        Some(io::Error::new(
            io::ErrorKind::ConnectionReset,
            "injected accept failure (test hook)",
        ))
    }

    #[cfg(not(feature = "test-hooks"))]
    fn injected_accept_failure(&self) -> Option<io::Error> {
        None
    }

    /// Handshake, session lookup, attach and the socket thread.
    // The callback's `Err` type is tungstenite's `ErrorResponse`, which is large by design; the
    // signature is the library's, not ours (clippy::result_large_err).
    #[allow(clippy::result_large_err)]
    fn serve(self: &Arc<Self>, stream: TcpStream, core: &Weak<Core>) -> Result<(), Refused> {
        let _ = stream.set_read_timeout(Some(HANDSHAKE_TIMEOUT));
        let _ = stream.set_write_timeout(Some(HANDSHAKE_TIMEOUT));
        let mut authorized: Option<String> = None;
        let config = WebSocketConfig::default().max_message_size(Some(MAX_INBOUND_MESSAGE));
        let ws = tungstenite::accept_hdr_with_config(
            stream,
            |request: &Request, response: Response| match self.authorize(request) {
                Ok(id) => {
                    authorized = Some(id);
                    Ok(response)
                }
                Err(refusal) => Err(refusal.into_response()),
            },
            Some(config),
        )
        .map_err(|error| Refused::new(400, format!("handshake failed: {error}")))?;
        let Some(session_id) = authorized else {
            // `authorize` returns the id whenever the handshake is accepted; this is unreachable and
            // refusing is still better than panicking on the accept thread.
            return Err(Refused::new(400, "handshake carried no session id".to_string()));
        };
        let socket = ws
            .get_ref()
            .try_clone()
            .map_err(|error| Refused::new(400, format!("socket clone: {error}")))?;
        let Some(core) = core.upgrade() else {
            close_now(ws, TRY_AGAIN_CLOSE);
            return Err(Refused::new(503, "the core is shutting down".to_string()));
        };

        let queue = Arc::new(FrameQueue::new(self.queue_bytes));
        let pending = Arc::new(AtomicBool::new(true));
        let sink: Arc<dyn FrameSink> = Arc::new(QueuedSink {
            queue: Arc::clone(&queue),
            pending: Arc::clone(&pending),
        });
        let (session, replay, handle) = match core.attach_socket(&session_id, sink) {
            SocketAttach::Attached {
                session,
                replay,
                handle,
            } => (session, replay, handle),
            // `ws_pty_endpoint`'s two refusals: an id the core never had (1008) and one that ended and
            // was retired (1000). The ticket itself was valid, so this is a socket close, not an HTTP
            // error.
            SocketAttach::Unknown => {
                close_now(ws, UNKNOWN_SESSION_CLOSE);
                return Err(Refused::new(404, format!("unknown session {session_id}")));
            }
            SocketAttach::Ended => {
                close_now(ws, NORMAL_CLOSE);
                return Err(Refused::new(410, format!("session {session_id} has ended")));
            }
            SocketAttach::Busy => {
                close_now(ws, TRY_AGAIN_CLOSE);
                return Err(Refused::new(503, format!("session {session_id} is busy")));
            }
        };
        let seq = self.next_seq.fetch_add(1, Ordering::Relaxed);
        // A panic between the registration and the running thread would otherwise leave a queue
        // registered on the fan-out that nothing ever reads. The thread's own tail detaches on the
        // normal and the panicking path, so this guard is disarmed once it is started.
        let sink_guard = PendingSink {
            detach: Some({
                let fanout = session.fanout();
                move || {
                    fanout.detach(handle);
                }
            }),
        };
        let connection = Arc::new(SocketConnection::new(
            seq,
            session_id.clone(),
            session.fanout(),
            handle,
            queue,
            pending,
            socket,
            Arc::downgrade(&core),
            Arc::downgrade(self),
            self.epoch,
            Arc::clone(&self.log),
        ));
        self.connections
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(seq, Arc::clone(&connection));
        let started = {
            let connection = Arc::clone(&connection);
            thread::Builder::new()
                .name(format!("ws-{seq}"))
                .spawn(move || connection.run(ws, replay))
        };
        match started {
            Ok(thread) => {
                connection.set_thread(thread);
                sink_guard.disarm();
                Ok(())
            }
            Err(error) => {
                self.forget(seq);
                connection.request_close(NORMAL_CLOSE);
                session.detach_sink(handle);
                Err(Refused::new(500, format!("socket thread: {error}")))
            }
        }
    }

    /// The ticket check the handshake performs: the path names the id, the query carries the token, and
    /// only a ticket this core minted for that id, unexpired and unused, passes (§6.4).
    fn authorize(&self, request: &Request) -> Result<String, Refused> {
        let path = request.uri().path();
        let Some(raw_id) = path.strip_prefix(PATH_PREFIX) else {
            return Err(Refused::new(404, format!("no such path: {path}")));
        };
        let Some(session_id) = decode_path_segment(raw_id) else {
            return Err(Refused::new(400, "the session id is not valid UTF-8".to_string()));
        };
        if session_id.is_empty() {
            return Err(Refused::new(404, "no session id in the path".to_string()));
        }
        let Some(token) = query_ticket(request.uri().query()) else {
            return Err(Refused::new(401, "no ticket in the query".to_string()));
        };
        self.tickets
            .consume(&session_id, token)
            .map_err(|refusal| Refused::new(refusal.status(), refusal.to_string()))?;
        Ok(session_id)
    }

    #[cfg(feature = "test-hooks")]
    /// Makes the next `count` accepted connections fail as if their client had dropped mid-accept.
    pub fn fail_next_accepts(&self, count: u64) {
        self.injected_accept_failures
            .store(count, Ordering::Release);
    }

    #[cfg(any(test, feature = "test-hooks"))]
    /// How many accepts failed, in total: the injected failures and the real ones.
    pub fn accept_failures(&self) -> u64 {
        self.accept_failures.load(Ordering::Relaxed)
    }

    #[cfg(any(test, feature = "test-hooks"))]
    /// Lowers the stall limit so a 30 s case runs in a second.
    pub fn set_stall_limit(&self, limit: Duration) {
        self.stall_limit_ms
            .store(limit.as_millis() as u64, Ordering::Release);
    }
}

/// Waits up to `deadline` for a thread to finish, without joining (`JoinHandle::join` has no timeout).
fn wait_thread(handle: &JoinHandle<()>, deadline: Instant) {
    // The thread ends by itself if it misses the budget; it is named (`ws-accept`), so a leftover is
    // findable.
    while !handle.is_finished() {
        if Instant::now() >= deadline {
            return;
        }
        thread::sleep(Duration::from_millis(1));
    }
}

/// Closes a socket that never became a session's: a refusal close frame, then the stream.
fn close_now(mut ws: WebSocket<TcpStream>, code: u16) {
    let _ = ws.close(Some(CloseFrame {
        code: CloseCode::from(code),
        reason: "closed by the hub".into(),
    }));
}

/// `?ticket=<token>` out of the request's query string. A token is base64url, so it needs no decoding.
fn query_ticket(query: Option<&str>) -> Option<&str> {
    query?
        .split('&')
        .find_map(|pair| pair.strip_prefix("ticket="))
        .filter(|token| !token.is_empty())
}

/// Percent-encodes one path segment, so an id that is not plain ASCII still round-trips through the
/// URL the console opens.
pub(super) fn encode_path_segment(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(*byte as char)
            }
            byte => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// The inverse of [`encode_path_segment`]; `None` when the escapes are not UTF-8.
fn decode_path_segment(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            let hex = std::str::from_utf8(bytes.get(index + 1..index + 3)?).ok()?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            index += 3;
        } else {
            out.push(bytes[index]);
            index += 1;
        }
    }
    String::from_utf8(out).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_path_segment_round_trips() {
        for id in ["omp-4f90176a", "omp:1", "会话", "a b/c?d"] {
            let encoded = encode_path_segment(id);
            assert!(encoded
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "-._~%".contains(c)));
            assert_eq!(decode_path_segment(&encoded).as_deref(), Some(id));
        }
        assert_eq!(decode_path_segment("%ZZ"), None);
        assert_eq!(decode_path_segment("%"), None);
        assert_eq!(decode_path_segment("%E4"), None, "a truncated character is refused");
    }

    #[test]
    fn the_ticket_comes_out_of_the_query() {
        assert_eq!(query_ticket(Some("ticket=abc")), Some("abc"));
        assert_eq!(query_ticket(Some("a=1&ticket=abc&b=2")), Some("abc"));
        assert_eq!(query_ticket(Some("a=1")), None);
        assert_eq!(query_ticket(Some("ticket=")), None);
        assert_eq!(query_ticket(None), None);
    }
}
