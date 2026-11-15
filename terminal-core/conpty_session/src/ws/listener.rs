//! The terminal WebSocket listener (§6.4): one loopback port, one ticket check per handshake, one
//! thread per accepted socket.
//!
//! The core binds `127.0.0.1:0` and reports the port, so two hubs (4444 and 4445) cannot collide and no
//! Windows Firewall prompt appears. A failed `accept` is that one connection's failure (master
//! `4b06139`: on Windows a client that drops mid-accept fails the call): the loop logs the first and
//! every 100th, backs off 0/50/200 ms/1 s, and accepts again. Only `shutdown` ends it, and
//! [`WsServer::listening`] is false once it has.
//!
//! The handshake does **not** run on the accept thread. Each accepted connection is handed to a
//! `ws-handshake` thread under a bounded number of simultaneous handshakes, with one total deadline
//! (a per-read timeout would let a peer that dribbles one byte at a time hold its slot forever), and a
//! peer over the cap is answered 503 at once instead of queueing. A handshake verifies the ticket (an
//! invalid one gets an HTTP 4xx and no socket) and the session is looked up after it, because an
//! unknown id must close with `ws_pty_endpoint`'s code rather than an HTTP error.

use std::collections::HashMap;
use std::io::{self, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use terminal_stream::FrameSink;
use tungstenite::handshake::server::{ErrorResponse, Request, Response, ServerHandshake};
use tungstenite::handshake::HandshakeError;
use tungstenite::http::{Response as HttpResponse, StatusCode};
use tungstenite::protocol::frame::coding::CloseCode;
use tungstenite::protocol::frame::CloseFrame;
use tungstenite::protocol::{WebSocket, WebSocketConfig};

use super::connection::{
    QueuedSink, SocketConnection, TrackedStream, NORMAL_CLOSE, TRY_AGAIN_CLOSE,
    UNKNOWN_SESSION_CLOSE,
};
use super::ticket::{Minted, Tickets};
use crate::core::{Core, CoreError, SocketAttach};
use crate::frame_queue::FrameQueue;

/// One handshake from start to finish. The socket's read timeout bounds one `read`, not the request: a
/// peer that sends a byte just before every timeout never trips it, so the handshake thread is cut off
/// by a clock instead.
const HANDSHAKE_DEADLINE: Duration = Duration::from_secs(5);
/// Handshakes in progress at once. Each costs a socket and two threads (the handshake and its cut-off)
/// until it finishes; a console attach uses one of them for milliseconds. This is what a peer that
/// connects and says nothing can hold, so it is small, and the excess is refused rather than queued.
const MAX_PENDING_HANDSHAKES: usize = 16;
/// `windows_accept_guard`'s `RETRY_DELAYS`: the wait after the 1st, 2nd, 3rd and every later failure.
const RETRY_DELAYS: [Duration; 4] = [
    Duration::ZERO,
    Duration::from_millis(50),
    Duration::from_millis(200),
    Duration::from_secs(1),
];
/// A socket that takes no byte for this long, while it still owes the peer some, is closed with 1000
/// (§5.4, §6.4).
const STALL_LIMIT: Duration = Duration::from_secs(30);
/// After the close request, a socket whose write is still blocked is cut off: the peer is gone.
const STALL_CLOSE_GRACE: Duration = Duration::from_secs(1);
/// The path the hub's own endpoint used (`/ws/pty/<id>`), so the console's URL builder only gains a
/// ticket query.
pub(super) const PATH_PREFIX: &str = "/ws/pty/";
/// How many sessions' final socket-drop counts may wait for a drain (diagnostics, one pair per
/// socket). An id the core no longer has can never be carried by a record, so the backlog is capped.
const RETIRED_DROPS_CAP: usize = 256;
/// An input frame is keystrokes: the default 64 MiB ceiling would let one frame allocate freely.
const MAX_INBOUND_MESSAGE: usize = 1024 * 1024;

/// The listener, its ticket table and the sockets attached through it.
pub struct WsServer {
    local: SocketAddr,
    tickets: Tickets,
    connections: Mutex<HashMap<u64, Arc<SocketConnection>>>,
    next_seq: AtomicU64,
    next_handshake: AtomicU64,
    /// The bound listener until `spawn_accept` moves it into the accept thread.
    pending_listener: Mutex<Option<TcpListener>>,
    stopping: Arc<AtomicBool>,
    listening: Arc<AtomicBool>,
    accept_thread: Mutex<Option<JoinHandle<()>>>,
    accept_failures: AtomicU64,
    /// Simultaneous handshakes, capped at [`MAX_PENDING_HANDSHAKES`].
    handshakes: Arc<HandshakeSlots>,
    /// Refused handshakes per HTTP status: the log names the first and every hundredth of each, the
    /// cadence the accept loop keeps, so a local process with bad tickets cannot flood the hub's log.
    refusals: Mutex<HashMap<u16, u64>>,
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

    /// The status tungstenite answered with, so the refusal's log line says 401 or 403 instead of a
    /// blanket 400. The wire already carries it (the callback's response); this is the log's copy.
    fn from_handshake<S, C>(error: HandshakeError<ServerHandshake<S, C>>) -> Self
    where
        S: std::io::Read + std::io::Write,
        C: tungstenite::handshake::server::Callback,
    {
        let status = match &error {
            HandshakeError::Failure(tungstenite::Error::Http(response)) => {
                response.status().as_u16()
            }
            _ => 400,
        };
        Self::new(status, format!("handshake failed: {error}"))
    }

    /// The HTTP answer tungstenite writes instead of a `101`: no socket exists for this connection.
    fn into_response(self) -> ErrorResponse {
        let status = StatusCode::from_u16(self.status).unwrap_or(StatusCode::UNAUTHORIZED);
        let mut response = HttpResponse::new(Some(self.reason));
        *response.status_mut() = status;
        response
    }
}

/// The bounded set of handshakes in progress. A slot is taken before a connection gets a thread and
/// released when its handshake is over (or its thread never started), so the cap is what an
/// unauthenticated peer can hold, not what a session can.
struct HandshakeSlots {
    active: AtomicUsize,
    limit: usize,
}

impl HandshakeSlots {
    fn new(limit: usize) -> Arc<Self> {
        Arc::new(Self {
            active: AtomicUsize::new(0),
            limit,
        })
    }

    /// `None` when every slot is taken: the caller refuses the connection now, never queues it.
    fn try_acquire(self: &Arc<Self>) -> Option<HandshakeSlot> {
        let mut active = self.active.load(Ordering::Acquire);
        loop {
            if active >= self.limit {
                return None;
            }
            match self.active.compare_exchange_weak(
                active,
                active + 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return Some(HandshakeSlot { slots: Arc::clone(self) }),
                Err(seen) => active = seen,
            }
        }
    }
}

struct HandshakeSlot {
    slots: Arc<HandshakeSlots>,
}

impl Drop for HandshakeSlot {
    fn drop(&mut self) {
        self.slots.active.fetch_sub(1, Ordering::AcqRel);
    }
}

/// Cuts the stream off at one absolute deadline, because a socket read timeout only bounds the silence
/// between two reads. Dropping it disarms the timer and waits for the thread (it wakes at once).
struct HandshakeCutoff {
    state: Arc<(Mutex<bool>, Condvar)>,
    fired: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl HandshakeCutoff {
    fn start(stream: &TcpStream, deadline: Instant) -> Option<Self> {
        let stream = stream.try_clone().ok()?;
        let state = Arc::new((Mutex::new(false), Condvar::new()));
        let fired = Arc::new(AtomicBool::new(false));
        let armed = Arc::clone(&state);
        let cut = Arc::clone(&fired);
        let thread = thread::Builder::new()
            .name("ws-handshake-cut".to_string())
            .spawn(move || {
                let (lock, wake) = &*armed;
                let mut done = lock.lock().unwrap_or_else(|error| error.into_inner());
                while !*done {
                    let now = Instant::now();
                    if now >= deadline {
                        break;
                    }
                    let (guard, timeout) = wake
                        .wait_timeout(done, deadline - now)
                        .unwrap_or_else(|error| error.into_inner());
                    done = guard;
                    if timeout.timed_out() {
                        break;
                    }
                }
                if !*done {
                    cut.store(true, Ordering::Release);
                    let _ = stream.shutdown(Shutdown::Both);
                }
            })
            .ok()?;
        Some(Self {
            state,
            fired,
            thread: Some(thread),
        })
    }

    /// `true` when the deadline shut the stream down: the refusal is a timeout, not a bad request.
    fn fired(&self) -> bool {
        self.fired.load(Ordering::Acquire)
    }
}

impl Drop for HandshakeCutoff {
    fn drop(&mut self) {
        let (lock, wake) = &*self.state;
        *lock.lock().unwrap_or_else(|error| error.into_inner()) = true;
        wake.notify_all();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
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
            next_handshake: AtomicU64::new(1),
            pending_listener: Mutex::new(Some(listener)),
            stopping: Arc::new(AtomicBool::new(false)),
            listening: Arc::new(AtomicBool::new(false)),
            accept_thread: Mutex::new(None),
            accept_failures: AtomicU64::new(0),
            handshakes: HandshakeSlots::new(MAX_PENDING_HANDSHAKES),
            refusals: Mutex::new(HashMap::new()),
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
    /// with a drop are listed, and only for `sessions` — the ids the drain is actually returning a
    /// record for. Marking any other socket would lose its count: it has no record to ride in, and the
    /// next drain would find nothing unreported (review omp 11 / kimi 4).
    pub fn take_socket_drops(&self, sessions: &[String]) -> HashMap<String, Vec<(u64, u64)>> {
        // Only the ids a record is going out for are taken, so a count that has no record to ride in
        // keeps waiting (in `retired_drops`, or on its live socket) and the next drain carries it.
        let mut drops: HashMap<String, Vec<(u64, u64)>> = HashMap::new();
        {
            let mut retired = self
                .retired_drops
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            for id in sessions {
                if let Some(counts) = retired.remove(id) {
                    drops.insert(id.clone(), counts);
                }
            }
        }
        let connections = self.connections.lock().unwrap_or_else(|e| e.into_inner());
        for connection in connections.values() {
            if !sessions.contains(&connection.session_id) {
                continue;
            }
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

    /// The ticker's 5 ms pass: a socket that still owes the peer bytes and has made no write progress
    /// for the stall limit is asked to close with 1000, and one that is still blocked a second later is
    /// cut off. The queue and a write already in flight (the replay included) both count as owed.
    pub fn check_stalls(&self) {
        let now = self.now_ms();
        let limit = self.stall_limit_ms.load(Ordering::Acquire);
        // The registry walk decides and collects; the Python log runs after the guard is gone (§6.5: no
        // callback while a core lock is held).
        let mut lines: Vec<String> = Vec::new();
        {
            let connections = self.connections.lock().unwrap_or_else(|e| e.into_inner());
            for connection in connections.values() {
                if !connection.has_output() {
                    continue;
                }
                if now.saturating_sub(connection.last_progress_ms()) <= limit {
                    continue;
                }
                if connection.close_code() == 0 {
                    connection.request_close(NORMAL_CLOSE);
                    lines.push(format!(
                        "session {}: console socket {} took nothing for {} ms; closing it with 1000",
                        connection.session_id, connection.seq, limit
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
        for line in lines {
            (self.log)(&line);
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
        // A socket whose write was blocked is cut off by `request_close` itself: it cannot be woken any
        // other way and shutdown may not wait a slice for one. The rest are reading, so they see the
        // code and send the close frame within one read poll. Half the budget for those handshakes, the
        // rest for the cut-off sockets to notice.
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
            let mut backlog = self
                .retired_drops
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            // A pane the core no longer has cannot be carried by any record, so its backlog entry waits
            // forever: keep the backlog bounded by the session ids a drain can still answer for.
            if backlog.len() >= RETIRED_DROPS_CAP {
                if let Some(oldest) = backlog.keys().next().cloned() {
                    backlog.remove(&oldest);
                }
            }
            backlog
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
                    // No slot: this peer is answered 503 and dropped here, at once. Queueing it behind
                    // the others would turn the cap into an unbounded wait.
                    let Some(slot) = self.handshakes.try_acquire() else {
                        refuse_busy(stream);
                        self.note_refusal(Refused::new(
                            503,
                            "too many handshakes in progress".to_string(),
                        ));
                        continue;
                    };
                    let server = Arc::clone(&self);
                    let core = core.clone();
                    let seq = self.next_handshake.fetch_add(1, Ordering::Relaxed);
                    let started = thread::Builder::new()
                        .name(format!("ws-handshake-{seq}"))
                        .spawn(move || {
                            // The slot lives exactly as long as the handshake: the thread itself
                            // becomes the socket thread's launcher and exits right after.
                            let _slot = slot;
                            server.serve_handshake(stream, &core);
                        });
                    if let Err(error) = started {
                        // The closure, with its slot and its stream, is dropped with the error.
                        self.accept_failures.fetch_add(1, Ordering::Relaxed);
                        (self.log)(&format!(
                            "terminal core: a console handshake thread did not start: {error}"
                        ));
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

    /// Handshake, session lookup, attach and the socket thread. Runs on its own `ws-handshake` thread,
    /// so anything slow or silent here costs one bounded slot and never the accept loop.
    // The callback's `Err` type is tungstenite's `ErrorResponse`, which is large by design; the
    // signature is the library's, not ours (clippy::result_large_err).
    #[allow(clippy::result_large_err)]
    fn serve(self: &Arc<Self>, stream: TcpStream, core: &Weak<Core>) -> Result<(), Refused> {
        let deadline = Instant::now() + HANDSHAKE_DEADLINE;
        let stream = TrackedStream::new(stream, self.epoch);
        let progress = stream.progress_handle();
        let blocked = stream.blocked_handle();
        let _ = stream.stream().set_read_timeout(Some(HANDSHAKE_DEADLINE));
        let _ = stream.stream().set_write_timeout(Some(HANDSHAKE_DEADLINE));
        let socket = stream
            .stream()
            .try_clone()
            .map_err(|error| Refused::new(400, format!("socket clone: {error}")))?;
        // The read timeout above bounds one read, not the request; this bounds the handshake.
        let cutoff = HandshakeCutoff::start(stream.stream(), deadline);
        let mut authorized: Option<String> = None;
        let config = WebSocketConfig::default().max_message_size(Some(MAX_INBOUND_MESSAGE));
        let handshake = tungstenite::accept_hdr_with_config(
            stream,
            |request: &Request, response: Response| match self.authorize(request) {
                Ok(id) => {
                    authorized = Some(id);
                    Ok(response)
                }
                Err(refusal) => Err(refusal.into_response()),
            },
            Some(config),
        );
        let timed_out = cutoff
            .as_ref()
            .map(HandshakeCutoff::fired)
            .unwrap_or(false);
        drop(cutoff);
        let mut ws = handshake.map_err(|error| {
            if timed_out {
                Refused::new(
                    408,
                    "handshake did not finish within its deadline".to_string(),
                )
            } else {
                Refused::from_handshake(error)
            }
        })?;
        let Some(session_id) = authorized else {
            // `authorize` returns the id whenever the handshake is accepted; this is unreachable and
            // refusing is still better than panicking on the handshake thread.
            return Err(Refused::new(400, "handshake carried no session id".to_string()));
        };
        let Some(core) = core.upgrade() else {
            close_now(&mut ws, TRY_AGAIN_CLOSE);
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
                close_now(&mut ws, UNKNOWN_SESSION_CLOSE);
                return Err(Refused::new(404, format!("unknown session {session_id}")));
            }
            SocketAttach::Ended => {
                close_now(&mut ws, NORMAL_CLOSE);
                return Err(Refused::new(410, format!("session {session_id} has ended")));
            }
            SocketAttach::Busy => {
                close_now(&mut ws, TRY_AGAIN_CLOSE);
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
            progress,
            blocked,
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

    /// One connection's handshake on its own thread. A panic or a refusal costs that connection only
    /// (§6.3, item 12): the accept loop never sees it.
    fn serve_handshake(self: &Arc<Self>, stream: TcpStream, core: &Weak<Core>) {
        match catch_unwind(AssertUnwindSafe(|| self.serve(stream, core))) {
            Ok(Ok(())) => {}
            Ok(Err(refused)) => self.note_refusal(refused),
            Err(_) => {
                self.accept_failures.fetch_add(1, Ordering::Relaxed);
                (self.log)(
                    "terminal core: a console socket panicked while being set up and was closed; \
                     the listener keeps accepting",
                );
            }
        }
    }

    /// The accept loop's cadence for refused handshakes: the first and every hundredth of each HTTP
    /// status. A local process with bad tickets must not be able to write a hub log line per try.
    fn note_refusal(&self, refused: Refused) {
        let count = {
            let mut counts = self.refusals.lock().unwrap_or_else(|e| e.into_inner());
            let count = counts.entry(refused.status).or_insert(0);
            *count += 1;
            *count
        };
        if count == 1 || count.is_multiple_of(100) {
            (self.log)(&format!(
                "terminal core: console socket refused ({count} at {}): {}",
                refused.status, refused.reason
            ));
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
    /// How many handshakes are in flight (the cap's current use).
    pub fn pending_handshakes(&self) -> usize {
        self.handshakes.active.load(Ordering::Acquire)
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
fn close_now(ws: &mut WebSocket<TrackedStream>, code: u16) {
    let _ = ws.close(Some(CloseFrame {
        code: CloseCode::from(code),
        reason: "closed by the hub".into(),
    }));
}

/// Answers a handshake the cap could not take without reading a byte of it: a bare HTTP 503, then the
/// stream goes. The console treats it like any failed ticket fetch (its reconnect path).
fn refuse_busy(mut stream: TcpStream) {
    let _ = stream.set_write_timeout(Some(Duration::from_millis(100)));
    let _ = stream.write_all(
        b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
    );
    let _ = stream.shutdown(Shutdown::Both);
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
