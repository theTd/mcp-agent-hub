//! The delivery state of one session **id**: its delivery lock, the attached sinks and their handles.
//!
//! A resume replaces the `Arc<Session>` in the core's table but not this (§6.4 A1): the new incarnation
//! takes its predecessor's `Arc<SinkFanout>` over, so a socket attached to the id keeps its registration
//! and its handle, `detach_sink(id, handle)` works whichever incarnation registered the handle, and the
//! resume banner reaches the sockets that were already subscribed.
//!
//! Lock order is the one the state had when it lived on `Session`: `delivery` before `text`, and `sinks`
//! only under `delivery`. A frame is taken out of the coalescing batch and handed to the sinks inside
//! `delivery`, which is what keeps frame order (Q11, §5.4); `attach_sink`'s replay snapshot and the
//! registration that follows it sit in one gap between two deliveries for the same reason.
//!
//! One sink's failure is one sink's: `deliver` runs under `catch_unwind`, a panic removes that sink (§6.3:
//! the item is the socket) and is reported in `Delivery::panicked`, and the other sinks still get the
//! frame. No lock here skips its work on poison: a poisoned `sinks` or `delivery` is recovered with
//! `into_inner`, so a sink that panicked can never make registration silently stop registering.

use std::any::Any;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, Weak};

use terminal_stream::FrameSink;

use crate::session::Session;

struct SinkEntry {
    handle: u64,
    sink: Arc<dyn FrameSink>,
}

/// What one fan-out did: the bytes the sinks reported dropping, the sinks that a panic removed, and
/// whether the frame belonged to a superseded incarnation (dropped without reaching any sink).
#[derive(Debug, Default)]
pub struct Delivery {
    pub dropped: u64,
    /// `(handle, panic message)` for each sink removed by this fan-out.
    pub panicked: Vec<(u64, String)>,
    pub superseded: bool,
}

pub struct SinkFanout {
    delivery: Mutex<()>,
    sinks: Mutex<Vec<SinkEntry>>,
    /// Handles are unique for the id, not for one incarnation: a carried-over sink keeps its handle.
    next_sink: AtomicU64,
    /// The incarnation that owns the id now, set by each handover under the delivery lock. `Weak`,
    /// because the fan-out outlives the incarnations it describes and must not keep one alive.
    owner: Mutex<Weak<Session>>,
}

impl Default for SinkFanout {
    fn default() -> Self {
        Self {
            delivery: Mutex::new(()),
            sinks: Mutex::new(Vec::new()),
            // Handle 0 is never handed out, so `Option<u64>` can say "no sink to skip".
            next_sink: AtomicU64::new(1),
            owner: Mutex::new(Weak::new()),
        }
    }
}

impl SinkFanout {
    /// Takes the delivery lock; every frame and every registration happens under it. Returns a guard
    /// rather than running a closure, because the caller has other locks to take in the fixed order
    /// `delivery → text`.
    pub fn lock_delivery(&self) -> MutexGuard<'_, ()> {
        self.delivery.lock().unwrap_or_else(|error| error.into_inner())
    }

    /// Registers a sink and returns its handle. The caller holds the delivery lock, so the handle is
    /// registered before any later frame can be handed out — and before `attach_sink` returns it.
    pub fn register(&self, sink: Arc<dyn FrameSink>) -> u64 {
        let handle = self.next_sink.fetch_add(1, Ordering::Relaxed);
        self.sinks
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(SinkEntry { handle, sink });
        handle
    }

    /// Makes `owner` the incarnation whose ring a new sink's replay is taken from. Called by a handover
    /// under the delivery lock, after the predecessor was retired and before the separator goes out.
    pub fn set_owner(&self, owner: Weak<Session>) {
        *self
            .owner
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = owner;
    }

    /// `(pending batch, replay text)` of the owning incarnation. The caller holds the delivery lock, so
    /// neither can change while the new sink registers: the bytes are either in this replay or in the
    /// frames that follow, never both and never neither.
    ///
    /// `None` when the owner was released (the id has no live incarnation in the table); the caller then
    /// reads the ring of the `Session` object it was called on, which is the only one left.
    pub fn take_replay(&self) -> Option<(Arc<Session>, Option<Vec<u8>>, String)> {
        let owner = self
            .owner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .upgrade()?;
        let (pending, replay) = owner.take_replay();
        Some((owner, pending, replay))
    }

    /// Removes a handle, whoever's incarnation registered it; `false` when it is not attached (already
    /// detached, or removed by a panic).
    pub fn detach(&self, handle: u64) -> bool {
        let mut sinks = self.sinks.lock().unwrap_or_else(|error| error.into_inner());
        let before = sinks.len();
        sinks.retain(|entry| entry.handle != handle);
        sinks.len() != before
    }

    /// Hands one frame to every attached sink except `skip` (the sink being registered, whose replay
    /// already carries these bytes). The caller holds the delivery lock.
    ///
    /// Each `deliver` is isolated: a panicking sink is removed here, reported in the reply, and the rest
    /// of the frame still reaches the other sinks. A sink is dropped after the sink list is released, so
    /// a `Drop` that reaches back into the session cannot deadlock on this lock.
    pub fn fan_out(&self, session_id: &str, frame: &[u8], skip: Option<u64>) -> Delivery {
        let mut delivery = Delivery::default();
        let mut removed = Vec::new();
        {
            let mut sinks = self.sinks.lock().unwrap_or_else(|error| error.into_inner());
            let mut index = 0;
            while index < sinks.len() {
                if Some(sinks[index].handle) == skip {
                    index += 1;
                    continue;
                }
                let entry = &sinks[index];
                // `AssertUnwindSafe`: a sink that panics leaves only its own state behind, and it is
                // gone from the list before the next frame, so nothing observes a half-updated sink.
                match catch_unwind(AssertUnwindSafe(|| entry.sink.deliver(session_id, frame))) {
                    Ok(dropped) => {
                        delivery.dropped += dropped;
                        index += 1;
                    }
                    Err(payload) => {
                        let entry = sinks.remove(index);
                        delivery
                            .panicked
                            .push((entry.handle, panic_message(payload)));
                        removed.push(entry);
                    }
                }
            }
        }
        drop(removed);
        delivery
    }
}

fn panic_message(payload: Box<dyn Any + Send>) -> String {
    if let Some(message) = payload.downcast_ref::<&str>() {
        (*message).to_string()
    } else if let Some(message) = payload.downcast_ref::<String>() {
        message.clone()
    } else {
        "non-string panic payload".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::panic::{catch_unwind, AssertUnwindSafe};

    struct Fixed(u64);
    impl FrameSink for Fixed {
        fn deliver(&self, _session_id: &str, _frame: &[u8]) -> u64 {
            self.0
        }
    }

    struct Exploding;
    impl FrameSink for Exploding {
        fn deliver(&self, _session_id: &str, _frame: &[u8]) -> u64 {
            panic!("sink out of order");
        }
    }

    /// A panicking sink costs itself: the frame still reaches the sink registered after it, the panic is
    /// reported once, and the panicking handle is gone.
    #[test]
    fn one_panicking_sink_is_removed_and_the_rest_still_get_the_frame() {
        let fanout = SinkFanout::default();
        let before = fanout.register(Arc::new(Fixed(3)));
        let bad = fanout.register(Arc::new(Exploding));
        let after = fanout.register(Arc::new(Fixed(5)));

        let delivery = catch_unwind(AssertUnwindSafe(|| fanout.fan_out("s", b"x", None)))
            .expect("a panicking sink must not unwind into the caller");
        assert_eq!(delivery.dropped, 8, "both healthy sinks reported their drops");
        assert_eq!(delivery.panicked.len(), 1);
        assert_eq!(delivery.panicked[0].0, bad);
        assert!(delivery.panicked[0].1.contains("sink out of order"));

        assert!(fanout.detach(before));
        assert!(fanout.detach(after));
        assert!(!fanout.detach(bad), "the panicking sink is already gone");
    }

    /// Registration does not depend on the sink lock being healthy: a poisoned one still registers.
    #[test]
    fn registration_recovers_a_poisoned_sink_list() {
        let fanout = SinkFanout::default();
        let _ = catch_unwind(AssertUnwindSafe(|| {
            let _guard = fanout.sinks.lock().unwrap();
            panic!("poison the sink list");
        }));
        assert!(fanout.sinks.is_poisoned());
        let handle = fanout.register(Arc::new(Fixed(0)));
        assert!(fanout.detach(handle));
    }

    /// Handles keep counting across incarnations, so one never names two sinks.
    #[test]
    fn handles_are_unique_across_takeovers() {
        let first = SinkFanout::default();
        let a = first.register(Arc::new(Fixed(0)));
        let second = first; // a resume carries the same structure: same counter, same sinks
        let b = second.register(Arc::new(Fixed(0)));
        assert!(a < b);
        assert!(second.detach(a), "a handle registered before the takeover detaches after it");
    }
}