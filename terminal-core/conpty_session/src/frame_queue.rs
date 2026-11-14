//! One console socket's bounded frame queue: step 4's socket sink, drained from Python until then.
//!
//! §5.4 and §6.4 (`rtc-sink-contract`): `deliver` runs on the session's reader thread (or on `feed`'s
//! caller) while the id's delivery lock is held, so it only appends — it never waits for a consumer and
//! never writes a socket. The socket's own thread drains the queue and writes the replay and
//! `replay_end` **before** the first queued frame. In step 3b that consumer is `Core::take_frames`.
//!
//! The cap counts bytes of unsent frames (§5.4's 2 MB, `CoreOptions::sink_queue_bytes`). On overflow the
//! **oldest whole frames** leave first, so a reader always gets whole frames in order and one slow
//! client's loss is counted in that queue's own `dropped_bytes` — not summed into the session's, which is
//! what §12.6 leaves to step 4.
//!
//! A taker holds the queue's mutex only while it moves the frames out; `deliver` therefore waits at most
//! for that copy, never for the socket write. `closed` is set when the session id is retired or the
//! handle is detached: a closed queue keeps no bytes, and later `deliver` calls discard theirs.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use terminal_stream::{FrameQueue as FrameRing, FrameSink};

/// What one `take` found: the frames in arrival order, this queue's cumulative dropped bytes, and
/// whether it has been closed (the id retired, or the handle detached).
#[derive(Debug, Default)]
pub struct Frames {
    pub frames: Vec<Vec<u8>>,
    /// Cumulative since the queue was created — not since the last take — so a caller that skips a poll
    /// cannot lose a count.
    pub dropped_bytes: u64,
    pub closed: bool,
}

struct QueueState {
    ring: FrameRing,
    closed: bool,
}

pub struct FrameQueue {
    state: Mutex<QueueState>,
    /// The closed flag of `state`, readable without the mutex (a retire marks it and then reports).
    closed: AtomicBool,
}

impl FrameQueue {
    pub fn new(cap: usize) -> Self {
        Self {
            state: Mutex::new(QueueState {
                ring: FrameRing::new(cap),
                closed: false,
            }),
            closed: AtomicBool::new(false),
        }
    }

    /// Closes the queue and discards what it still held: the socket these bytes were meant for is gone,
    /// and the drop is intentional (the hub closes a retired session's panes with code 1000 today).
    pub fn close(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        state.closed = true;
        while state.ring.pop().is_some() {}
        self.closed.store(true, Ordering::Release);
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    /// Takes every queued frame in arrival order and clears the queue. Never waits for a producer: the
    /// list is moved out and the mutex released before the caller writes anything.
    pub fn take(&self) -> Frames {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut frames = Vec::with_capacity(state.ring.len());
        while let Some(frame) = state.ring.pop() {
            frames.push(frame);
        }
        Frames {
            frames,
            dropped_bytes: state.ring.dropped_bytes(),
            closed: state.closed,
        }
    }

    /// Cumulative dropped bytes, without taking the frames.
    pub fn dropped_bytes(&self) -> u64 {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .ring
            .dropped_bytes()
    }

    /// Bytes queued and not yet taken.
    pub fn queued_bytes(&self) -> usize {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .ring
            .bytes()
    }
}

impl FrameSink for FrameQueue {
    fn deliver(&self, _session_id: &str, frame: &[u8]) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if state.closed {
            // The socket is gone: nothing to count, nothing to queue (§5.4's drop accounting is for a
            // live client that falls behind).
            return;
        }
        state.ring.push(frame);
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::thread;
    use std::time::{Duration, Instant};

    use super::*;

    fn sink(queue: &Arc<FrameQueue>, frame: &[u8]) {
        queue.deliver("s", frame)
    }

    /// Order is kept and every whole frame arrives exactly once.
    #[test]
    fn frames_leave_in_arrival_order() {
        let queue = Arc::new(FrameQueue::new(1024));
        for index in 0..5u8 {
            sink(&queue, &[b'f', index]);
        }
        let taken = queue.take();
        assert!(!taken.closed);
        assert_eq!(taken.dropped_bytes, 0);
        let text: Vec<u8> = taken.frames.concat();
        assert_eq!(text, b"f\x00f\x01f\x02f\x03f\x04");
        assert!(queue.take().frames.is_empty(), "take clears the queue");
    }

    /// §5.4: the byte cap holds, the oldest whole frames go first, and every dropped byte is counted in
    /// the queue (the trait reports nothing back, so one socket's loss cannot reach the session).
    #[test]
    fn the_cap_drops_the_oldest_whole_frames_and_counts_them() {
        let queue = Arc::new(FrameQueue::new(10));
        sink(&queue, b"12345");
        sink(&queue, b"67890");
        sink(&queue, b"abc");
        assert_eq!(queue.queued_bytes(), 8);
        assert_eq!(queue.dropped_bytes(), 5, "the oldest whole frame made room");
        let taken = queue.take();
        assert_eq!(taken.frames, vec![b"67890".to_vec(), b"abc".to_vec()]);
        assert_eq!(taken.dropped_bytes, 5);
        // A frame larger than the whole queue is dropped by itself: a reader never gets half a frame.
        sink(&queue, &[b'z'; 11]);
        assert!(queue.take().frames.is_empty());
        assert_eq!(queue.dropped_bytes(), 16, "the count is cumulative");
    }

    /// `deliver` never waits for a taker: a thread draining as fast as it can does not stretch a
    /// delivery past the frame copy, so a slow consumer costs its own queue and not the reader.
    #[test]
    fn deliver_does_not_wait_for_a_taker() {
        let queue = Arc::new(FrameQueue::new(64 * 1024));
        let stop = Arc::new(AtomicBool::new(false));
        let taker = {
            let queue = Arc::clone(&queue);
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                let mut worst = Duration::ZERO;
                let mut taken = 0usize;
                while !stop.load(Ordering::Relaxed) {
                    let started = Instant::now();
                    taken += queue.take().frames.len();
                    worst = worst.max(started.elapsed());
                }
                (taken, worst)
            })
        };
        let frame = vec![b'a'; 1024];
        let mut worst = Duration::ZERO;
        for _ in 0..4000 {
            let started = Instant::now();
            sink(&queue, &frame);
            worst = worst.max(started.elapsed());
        }
        stop.store(true, Ordering::Relaxed);
        let (taken, worst_take) = taker.join().expect("the taker thread");
        assert!(
            worst < Duration::from_millis(50),
            "a delivery waited {worst:?} on the taker (worst take {worst_take:?})"
        );
        // The queue's cap is honoured throughout: what the taker missed was dropped and counted.
        let _ = taken;
        assert!(queue.queued_bytes() <= 64 * 1024);
    }

    /// A closed queue takes nothing more and discards what it held: the socket is gone.
    #[test]
    fn a_closed_queue_reports_closed_and_receives_nothing() {
        let queue = Arc::new(FrameQueue::new(64));
        sink(&queue, b"before");
        queue.close();
        assert!(queue.is_closed());
        sink(&queue, b"after");
        assert_eq!(queue.dropped_bytes(), 0, "a closed queue counts no drops");
        let taken = queue.take();
        assert!(taken.closed, "take reports the close");
        assert!(
            taken.frames.is_empty(),
            "close discards the frames of a socket that is gone"
        );
        assert_eq!(taken.dropped_bytes, 0);
    }
}
