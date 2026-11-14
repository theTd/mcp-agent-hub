//! One console socket (§6.4): the replay, `replay_end`, the session's frames in order, the browser's
//! input and the close.
//!
//! One thread per socket owns the [`WebSocket`] and does both directions. tungstenite has no `select`
//! and no way to split a synchronous socket safely (two `WebSocket`s over one TCP stream would
//! interleave partial writes), so the thread writes whatever the queue holds and then reads with a
//! [`READ_POLL`] timeout; every frame therefore lands within one poll of its delivery. A second thread
//! would remove that latency at the price of two threads and a shared socket per pane.
//!
//! The wire order is the contract `attach_sink` documents: the replay, then
//! `{"type":"replay_end"}`, then the frames — never a frame ahead of the replay, which is why the sink
//! is a queue ([`FrameQueue`], §5.4) and not a socket write from `deliver`.

use std::io::ErrorKind;
use std::net::{Shutdown, TcpStream};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use terminal_stream::{strip_terminal_replies, FrameSink};
use tungstenite::error::Error;
use tungstenite::protocol::frame::coding::CloseCode;
use tungstenite::protocol::frame::CloseFrame;
use tungstenite::{Message, WebSocket};

use super::input::{interpret, BrowserMessage};
use super::listener::WsServer;
use crate::frame_queue::FrameQueue;
use crate::sink_fanout::SinkFanout;
use crate::Core;

/// How long one socket thread waits for input before it looks at its frame queue again. Every frame
/// lands within this bound of its flush, and an idle pane costs `1/READ_POLL` wakeups a second.
pub(super) const READ_POLL: Duration = Duration::from_millis(10);
/// W7's normal close (`close_ws_frontends`): a pane whose session ended or whose hub removed it.
pub(super) const NORMAL_CLOSE: u16 = 1000;
/// What `ws_pty_endpoint` closed with for a session id it did not know (`code=1008`).
pub(super) const UNKNOWN_SESSION_CLOSE: u16 = 1008;
/// The endpoint's "the registry was busy, try again later" (`code=1013`).
pub(super) const TRY_AGAIN_CLOSE: u16 = 1013;
/// The frame the console's `isReplayEndFrame` recognises (V3). It is control data, never terminal
/// output.
pub(super) const REPLAY_END_FRAME: &str = "{\"type\":\"replay_end\"}";
/// §5.2's deadline for one input write; a full write queue is logged, not retried: the browser can
/// send more.
const INPUT_DEADLINE: Duration = Duration::from_millis(50);

/// The sink the session fans frames into: the socket's queue plus the flag the stall watchdog reads.
/// `deliver` runs under the id's delivery lock, so it only appends and never touches the socket.
pub(super) struct QueuedSink {
    pub(super) queue: Arc<FrameQueue>,
    pub(super) pending: Arc<AtomicBool>,
}
impl FrameSink for QueuedSink {
    fn deliver(&self, session_id: &str, frame: &[u8]) {
        <FrameQueue as FrameSink>::deliver(&*self.queue, session_id, frame);
        self.pending.store(true, Ordering::Release);
    }
}

/// One attached console socket: its queue, its close state and the counters the watchdog reads.
pub struct SocketConnection {
    /// The registry key and the thread name.
    pub(super) seq: u64,
    pub(super) session_id: String,
    /// The id's delivery state, so a socket can detach itself without the session table.
    fanout: Arc<SinkFanout>,
    sink_handle: u64,
    queue: Arc<FrameQueue>,
    /// A clone of the socket for the shutdown paths: `TcpStream::shutdown` is the only way to unblock a
    /// write the peer never accepts.
    socket: TcpStream,
    /// Set when the thread must close: the first code wins, so a stall close cannot be overwritten by
    /// the retire that follows it.
    close_code: AtomicU16,
    close_requested_ms: AtomicU64,
    /// "The queue holds bytes the socket has not taken yet" — the watchdog's precondition.
    pending: Arc<AtomicBool>,
    /// The last successful write, in milliseconds since the server started.
    last_progress_ms: AtomicU64,
    /// The queue's drop count already carried by a drain record (§5.4/§9: per handle, cumulative).
    /// `drain` walks the sockets every tick and compares, so a drop that no byte follows still dirties
    /// its session.
    reported_drops: AtomicU64,
    core: Weak<Core>,
    server: Weak<WsServer>,
    finished: AtomicBool,
    reported_failure: AtomicBool,
    thread: Mutex<Option<JoinHandle<()>>>,
    epoch: Instant,
    log: Arc<dyn Fn(&str) + Send + Sync>,
}

impl SocketConnection {
    #[allow(clippy::too_many_arguments)]
    pub(super) fn new(
        seq: u64,
        session_id: String,
        fanout: Arc<SinkFanout>,
        sink_handle: u64,
        queue: Arc<FrameQueue>,
        pending: Arc<AtomicBool>,
        socket: TcpStream,
        core: Weak<Core>,
        server: Weak<WsServer>,
        epoch: Instant,
        log: Arc<dyn Fn(&str) + Send + Sync>,
    ) -> Self {
        let now = epoch.elapsed().as_millis() as u64;
        Self {
            seq,
            session_id,
            fanout,
            sink_handle,
            queue,
            socket,
            close_code: AtomicU16::new(0),
            close_requested_ms: AtomicU64::new(0),
            pending,
            last_progress_ms: AtomicU64::new(now),
            reported_drops: AtomicU64::new(0),
            core,
            server,
            finished: AtomicBool::new(false),
            reported_failure: AtomicBool::new(false),
            thread: Mutex::new(None),
            epoch,
            log,
        }
    }

    pub(super) fn set_thread(&self, thread: JoinHandle<()>) {
        if let Ok(mut slot) = self.thread.lock() {
            *slot = Some(thread);
        }
    }

    /// Asks the socket to close with `code` and returns whether this call set it. The first request
    /// wins: the socket then reports the code it actually sent.
    pub(super) fn request_close(&self, code: u16) -> bool {
        if self
            .close_code
            .compare_exchange(0, code, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            self.close_requested_ms
                .store(self.now_ms(), Ordering::Release);
            true
        } else {
            false
        }
    }

    pub(super) fn close_code(&self) -> u16 {
        self.close_code.load(Ordering::Acquire)
    }

    pub(super) fn close_requested_ms(&self) -> u64 {
        self.close_requested_ms.load(Ordering::Acquire)
    }

    pub(super) fn has_pending(&self) -> bool {
        self.pending.load(Ordering::Acquire)
    }

    pub(super) fn last_progress_ms(&self) -> u64 {
        self.last_progress_ms.load(Ordering::Acquire)
    }

    pub(super) fn dropped_bytes(&self) -> u64 {
        self.queue.dropped_bytes()
    }

    /// The last drop count a drain record carried for this socket.
    pub(super) fn reported_drops(&self) -> u64 {
        self.reported_drops.load(Ordering::Acquire)
    }

    /// Marks a drop count as carried by a record.
    pub(super) fn mark_drops_reported(&self, dropped: u64) {
        self.reported_drops.store(dropped, Ordering::Release);
    }

    pub(super) fn sink_handle(&self) -> u64 {
        self.sink_handle
    }

    pub(super) fn finished(&self) -> bool {
        self.finished.load(Ordering::Acquire)
    }

    /// Cuts the socket off without a close handshake: the answer to a write the peer never accepts.
    pub(super) fn abort(&self) {
        let _ = self.socket.shutdown(Shutdown::Both);
    }

    /// Waits until the thread has finished, up to `deadline`. Never joins: the caller may be on the
    /// ticker, and a join without a deadline is what the budget forbids.
    pub(super) fn wait_finished(&self, deadline: Instant) -> bool {
        while !self.finished() {
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        true
    }

    /// Joins the thread if it has finished, so `shutdown` leaves no thread of ours behind. A thread
    /// that missed the shutdown budget is left running (it is named `ws-<seq>`) and the caller already
    /// logged it: joining it here would turn a logged overrun into a hung shutdown (§6.4).
    pub(super) fn join_finished(&self) {
        let handle = self.thread.lock().ok().and_then(|mut slot| slot.take());
        if let Some(handle) = handle {
            if handle.is_finished() {
                let _ = handle.join();
            }
        }
    }

    fn now_ms(&self) -> u64 {
        self.epoch.elapsed().as_millis() as u64
    }

    fn note_progress(&self) {
        self.last_progress_ms.store(self.now_ms(), Ordering::Release);
    }

    fn log_once(&self, message: String) {
        if !self.reported_failure.swap(true, Ordering::AcqRel) {
            (self.log)(&message);
        }
    }

    /// The socket's thread: replay, `replay_end`, frames, input, close. A panic here costs this socket
    /// only (§6.3: the item is the socket).
    pub(super) fn run(self: Arc<Self>, mut ws: WebSocket<TcpStream>, replay: String) {
        let outcome = catch_unwind(AssertUnwindSafe(|| self.serve(&mut ws, &replay)));
        match outcome {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                if self.close_code() == 0 {
                    self.log_once(format!(
                        "session {}: console socket {} closed by the transport ({error})",
                        self.session_id, self.seq
                    ));
                }
            }
            Err(_) => (self.log)(&format!(
                "session {}: console socket {} panicked and was closed",
                self.session_id, self.seq
            )),
        }
        // The queue is dead with the socket, and the sink must leave the id's fan-out: a retired
        // session's `close_sockets` already did both, and doing it twice is harmless.
        self.queue.close();
        self.fanout.detach(self.sink_handle);
        self.finished.store(true, Ordering::Release);
        if let Some(server) = self.server.upgrade() {
            server.forget(self.seq);
        }
    }

    fn serve(&self, ws: &mut WebSocket<TcpStream>, replay: &str) -> Result<(), Error> {
        // The read poll is how this one thread serves both directions; the write stays blocking, so a
        // peer that accepts nothing parks its own thread until the watchdog cuts the socket off.
        ws.get_ref().set_read_timeout(Some(READ_POLL))?;
        ws.get_ref().set_write_timeout(None)?;
        if !replay.is_empty() {
            self.send_text(ws, replay)?;
        }
        self.send_text(ws, REPLAY_END_FRAME)?;
        self.pending.store(false, Ordering::Release);
        loop {
            let code = self.close_code();
            if code != 0 {
                // 1000 by the hub's request, 1013/1008 by a refusal: send it and stop reading.
                let _ = self.send_close(ws, code);
                return Ok(());
            }
            if self.queue.is_closed() || self.fanout.is_closed() {
                let _ = self.send_close(ws, NORMAL_CLOSE);
                return Ok(());
            }
            self.write_frames(ws)?;
            let message = match ws.read() {
                Ok(message) => message,
                // A poll timeout is a normal turn around the loop: the queue above is what matters.
                Err(Error::Io(error))
                    if matches!(error.kind(), ErrorKind::TimedOut | ErrorKind::WouldBlock) =>
                {
                    continue
                }
                Err(Error::ConnectionClosed | Error::AlreadyClosed) => return Ok(()),
                Err(error) => return Err(error),
            };
            match message {
                Message::Text(text) => self.on_browser_frame(text.as_str()),
                Message::Close(_) => {
                    let _ = self.send_close(ws, NORMAL_CLOSE);
                    return Ok(());
                }
                // Ping/pong are answered by tungstenite inside `read`; binary is not part of the
                // terminal protocol and is ignored rather than typed into the PTY.
                _ => {}
            }
        }
    }

    fn write_frames(&self, ws: &mut WebSocket<TcpStream>) -> Result<(), Error> {
        // The test seam: a parked write is what makes a queue overflow (or a stall) happen on demand.
        #[cfg(feature = "test-hooks")]
        crate::test_hooks::hold_socket_write(|| self.close_code() != 0);
        // And an injected panic here is the only way to prove one socket's panic costs one socket.
        #[cfg(feature = "test-hooks")]
        crate::test_hooks::panic_socket_write(&self.session_id);
        let taken = self.queue.take();
        for frame in taken.frames {
            match String::from_utf8(frame) {
                Ok(text) => self.send_text(ws, &text)?,
                Err(_) => self.log_once(format!(
                    "session {}: a frame was not valid UTF-8 and was dropped for socket {}",
                    self.session_id, self.seq
                )),
            }
        }
        // Exact: a frame that arrived while these were written keeps the socket "pending", so the
        // stall watchdog still sees it.
        let pending = self.queue.queued_bytes() > 0;
        self.pending.store(pending, Ordering::Release);
        if !pending {
            // The stall clock measures a queue that is not moving, not the time since the last write: a
            // socket that was simply quiet (no output for a while) must not look stalled the moment its
            // next frame arrives. An empty queue refreshes it on every pass of a quiet socket.
            self.note_progress();
        }
        Ok(())
    }

    fn send_text(&self, ws: &mut WebSocket<TcpStream>, text: &str) -> Result<(), Error> {
        ws.send(Message::text(text.to_string()))?;
        self.note_progress();
        Ok(())
    }

    fn send_close(&self, ws: &mut WebSocket<TcpStream>, code: u16) -> Result<(), Error> {
        ws.close(Some(CloseFrame {
            code: CloseCode::from(code),
            reason: "closed by the hub".into(),
        }))
    }

    fn on_browser_frame(&self, text: &str) {
        match interpret(text) {
            None => {}
            Some(BrowserMessage::Input(data)) => {
                // §6.4/§8: the replies xterm.js generates never reach a PTY.
                let cleaned = strip_terminal_replies(&data);
                if cleaned.is_empty() {
                    return;
                }
                let Some(core) = self.core.upgrade() else {
                    return;
                };
                // A PTY session gets the bytes; a text-only one gets an ordered drain request, because
                // the hub owns its echo and line callback and nothing may write it anywhere.
                if let Err(error) = core.socket_input(&self.session_id, &cleaned, INPUT_DEADLINE) {
                    self.log_once(format!(
                        "session {}: console input was not delivered ({error})",
                        self.session_id
                    ));
                }
            }
            Some(BrowserMessage::Resize {
                cols,
                rows,
                propagate,
            }) => {
                // §6.4: the core records the request; the hub applies the 20x5 clamp and propagation.
                let Some(core) = self.core.upgrade() else {
                    return;
                };
                if let Err(error) = core.request_resize(&self.session_id, cols, rows, propagate) {
                    self.log_once(format!(
                        "session {}: console resize request ignored ({error})",
                        self.session_id
                    ));
                }
            }
        }
    }
}
