//! Per-session output coalescing into one frame per sink, and the bounded per-sink queue.
//!
//! §5.4: a session's output becomes **one** frame per attached sink when the batch reaches 32 KiB, when
//! 20 ms have passed since its first byte, or after 5 ms of silence. The ticker calls `flush_due`; the
//! reader only appends. This is the only batching in the system (DECISIONS.md `terminal-core-batching`).
//!
//! A sink is a console socket in step 4 and a test sink here. Backpressure is per sink: `FrameQueue` holds
//! whole frames up to its cap, drops the oldest to make room, and counts every dropped byte.

use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Flush once the batch holds this many bytes.
pub const FLUSH_BYTES: usize = 32 * 1024;
/// Flush 20 ms after the batch's first byte.
pub const FLUSH_AGE: Duration = Duration::from_millis(20);
/// Flush after this much silence with at least one byte queued.
pub const FLUSH_IDLE: Duration = Duration::from_millis(5);
/// One socket's queue (§5.4): unsent frames beyond this are dropped whole.
pub const SINK_QUEUE_BYTES: usize = 2 * 1024 * 1024;

struct Batch {
    data: Vec<u8>,
    first: Instant,
    last: Instant,
}

#[derive(Default)]
pub struct Coalescer {
    batch: Option<Batch>,
}

impl Coalescer {
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds one chunk's post-strip text; empty text never opens a batch (there is nothing to send).
    pub fn push(&mut self, bytes: &[u8], now: Instant) {
        if bytes.is_empty() {
            return;
        }
        match &mut self.batch {
            Some(batch) => {
                batch.data.extend_from_slice(bytes);
                batch.last = now;
            }
            None => {
                self.batch = Some(Batch {
                    data: bytes.to_vec(),
                    first: now,
                    last: now,
                })
            }
        }
    }

    /// Whether the batch must be flushed now: 32 KiB accumulated, 20 ms since its first byte, or 5 ms
    /// of silence after a byte.
    pub fn due(&self, now: Instant) -> bool {
        self.batch
            .as_ref()
            .map(|batch| {
                batch.data.len() >= FLUSH_BYTES
                    || now.saturating_duration_since(batch.first) >= FLUSH_AGE
                    || now.saturating_duration_since(batch.last) >= FLUSH_IDLE
            })
            .unwrap_or(false)
    }

    /// Takes the frame when it is due; `None` when there is nothing or it is not time yet.
    pub fn take_due(&mut self, now: Instant) -> Option<Vec<u8>> {
        if !self.due(now) {
            return None;
        }
        self.batch.take().map(|batch| batch.data)
    }

    /// Takes whatever is buffered, due or not (the session's last bytes before it dies).
    pub fn take_all(&mut self) -> Option<Vec<u8>> {
        self.batch.take().map(|batch| batch.data)
    }

    pub fn pending_bytes(&self) -> usize {
        self.batch.as_ref().map(|batch| batch.data.len()).unwrap_or(0)
    }
}

/// One console socket. `deliver` returns the bytes it had to drop to accept the frame.
pub trait FrameSink: Send + Sync {
    fn deliver(&self, session_id: &str, frame: &[u8]) -> u64;
}

/// A bounded ring of whole frames: the oldest leave first, and every dropped byte is counted.
pub struct FrameQueue {
    frames: VecDeque<Vec<u8>>,
    bytes: usize,
    cap: usize,
    dropped: u64,
}

impl FrameQueue {
    pub fn new(cap: usize) -> Self {
        Self {
            frames: VecDeque::new(),
            bytes: 0,
            cap,
            dropped: 0,
        }
    }

    /// Queues one whole frame, dropping the oldest until it fits; returns the bytes dropped now. A frame
    /// larger than the whole queue is dropped by itself: a slow reader never gets half a frame.
    pub fn push(&mut self, frame: &[u8]) -> u64 {
        if frame.len() > self.cap {
            self.dropped += frame.len() as u64;
            return frame.len() as u64;
        }
        let mut dropped = 0u64;
        while self.bytes + frame.len() > self.cap {
            let Some(oldest) = self.frames.pop_front() else {
                break;
            };
            self.bytes -= oldest.len();
            dropped += oldest.len() as u64;
            self.dropped += oldest.len() as u64;
        }
        self.bytes += frame.len();
        self.frames.push_back(frame.to_vec());
        dropped
    }

    pub fn pop(&mut self) -> Option<Vec<u8>> {
        let frame = self.frames.pop_front()?;
        self.bytes -= frame.len();
        Some(frame)
    }

    pub fn len(&self) -> usize {
        self.frames.len()
    }

    pub fn is_empty(&self) -> bool {
        self.frames.is_empty()
    }

    pub fn bytes(&self) -> usize {
        self.bytes
    }

    pub fn dropped_bytes(&self) -> u64 {
        self.dropped
    }
}

/// The test sink (and the shape step 4's socket uses): a bounded queue plus the drop count.
pub struct QueueSink {
    queue: Mutex<FrameQueue>,
}

impl QueueSink {
    pub fn new(cap: usize) -> Self {
        Self {
            queue: Mutex::new(FrameQueue::new(cap)),
        }
    }

    pub fn pop(&self) -> Option<Vec<u8>> {
        self.queue
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .pop()
    }

    pub fn queued_bytes(&self) -> usize {
        self.queue
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .bytes()
    }

    pub fn dropped_bytes(&self) -> u64 {
        self.queue
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .dropped_bytes()
    }

    /// Everything queued so far, in arrival order.
    pub fn drain(&self) -> Vec<u8> {
        let mut queue = self.queue.lock().unwrap_or_else(|error| error.into_inner());
        let mut out = Vec::new();
        while let Some(frame) = queue.pop() {
            out.extend_from_slice(&frame);
        }
        out
    }
}

impl FrameSink for QueueSink {
    fn deliver(&self, _session_id: &str, frame: &[u8]) -> u64 {
        self.queue
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(frame)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(base: Instant, millis: u64) -> Instant {
        base + Duration::from_millis(millis)
    }

    #[test]
    fn a_batch_flushes_by_size_age_or_silence_and_keeps_every_byte_in_order() {
        let base = Instant::now();
        let mut coalescer = Coalescer::new();
        coalescer.push(b"one", at(base, 0));
        assert!(!coalescer.due(at(base, 4)));
        assert!(coalescer.due(at(base, 5)), "5 ms of silence with a byte queued");
        assert_eq!(coalescer.take_due(at(base, 5)).unwrap(), b"one");

        coalescer.push(b"two", at(base, 10));
        assert!(coalescer.due(at(base, 30)), "20 ms since the first byte");
        assert_eq!(coalescer.take_due(at(base, 30)).unwrap(), b"two");

        let big = vec![b'x'; FLUSH_BYTES];
        coalescer.push(&big, at(base, 40));
        assert!(coalescer.due(at(base, 40)), "32 KiB accumulated");
        assert_eq!(coalescer.take_due(at(base, 40)).unwrap().len(), FLUSH_BYTES);
        assert!(coalescer.take_due(at(base, 40)).is_none());
    }

    #[test]
    fn a_sink_queue_drops_the_oldest_whole_frames_and_counts_them() {
        let mut queue = FrameQueue::new(10);
        assert_eq!(queue.push(b"12345"), 0);
        assert_eq!(queue.push(b"67890"), 0);
        assert_eq!(queue.push(b"abc"), 5, "the oldest frame made room");
        assert_eq!(queue.pop().unwrap(), b"67890");
        assert_eq!(queue.pop().unwrap(), b"abc");
        assert!(queue.pop().is_none());
        assert_eq!(queue.dropped_bytes(), 5);
        // A frame larger than the whole queue is dropped alone, and nothing queued is disturbed.
        assert_eq!(queue.push(&[b'z'; 11]), 11);
        assert!(queue.is_empty());
    }
}
