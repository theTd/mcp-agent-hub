//! Test-only hooks (`feature = "test-hooks"`), compiled out of every release artifact.
//!
//! Two knobs the integration tests need and cannot get any other way: a delay before the close helper
//! runs `ClosePseudoConsole` (which holds the "closing" window open long enough for a test to race the
//! reaper), and a gate under which `resize` holds the console slot (so a test can pin "another thread is
//! inside the console" for the §12.7/SF1 case). Both are inert unless a test sets them.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

/// How long the close helper sleeps before `ClosePseudoConsole`; 0 in every normal run.
static CLOSE_DELAY_MS: AtomicU64 = AtomicU64::new(0);

/// How long `attach_sink` sleeps after it drops the text lock and before it registers the sink; 0 in
/// every normal run. Widens the Q11 window for the test that proves the delivery lock closes it.
static ATTACH_DELAY_MS: AtomicU64 = AtomicU64::new(0);

/// `true` while `pump` takes no frame, so a coalescing batch stays pending.
///
/// The resume tests (`tests/sink_handover.rs`) need the predecessor to die with bytes still in its
/// batch: with the batch pending, the handover's flush is the thing that delivers them, and the order
/// "old bytes, separator, new bytes" is observable in one sink. `take_over` calls `pump_locked` and is
/// not held by this.
static HOLD_FRAMES: AtomicU64 = AtomicU64::new(0);

/// `true` while `resize` should keep the console slot after taking it.
static HOLD_RESIZE: Mutex<bool> = Mutex::new(false);
/// Wakes `resize` when the test releases the gate.
static RESIZE_RELEASED: Condvar = Condvar::new();
/// `true` while a `resize` call is inside the gate (so a test knows the slot is really held).
static RESIZE_INSIDE: Mutex<bool> = Mutex::new(false);
/// Wakes `wait_until_resize_holds` when a `resize` enters or leaves the gate.
static RESIZE_INSIDE_CHANGED: Condvar = Condvar::new();
/// A test that forgets to release the gate stalls one resize for at most this long.
const HOLD_LIMIT: Duration = Duration::from_secs(30);

/// Delays the next close helpers by `delay` (0 restores the normal path).
pub fn set_close_delay(delay: Duration) {
    CLOSE_DELAY_MS.store(delay.as_millis() as u64, Ordering::Release);
}

/// Called by the close helper before it closes: the opening of the "closing" window.
pub(crate) fn close_delay() {
    let millis = CLOSE_DELAY_MS.load(Ordering::Acquire);
    if millis > 0 {
        std::thread::sleep(Duration::from_millis(millis));
    }
}

/// Delays the next `attach_sink` calls by `delay` between the snapshot and the registration (0 restores
/// the normal path). Only a test that is proving the delivery lock ever sets this.
pub fn set_attach_delay(delay: Duration) {
    ATTACH_DELAY_MS.store(delay.as_millis() as u64, Ordering::Release);
}

/// Called by `attach_sink` between the replay snapshot and the sink registration.
pub(crate) fn attach_delay() {
    let millis = ATTACH_DELAY_MS.load(Ordering::Acquire);
    if millis > 0 {
        std::thread::sleep(Duration::from_millis(millis));
    }
}

/// Makes `pump` leave the batch pending (a batch survives to the exit while it is held).
pub fn hold_frames() {
    HOLD_FRAMES.fetch_add(1, Ordering::AcqRel);
}

/// Undoes one `hold_frames`.
pub fn release_frames() {
    let _ = HOLD_FRAMES.try_update(Ordering::AcqRel, Ordering::Acquire, |held| {
        Some(held.saturating_sub(1))
    });
}

/// Called by `pump` before it takes a frame: `true` means "leave it in the batch".
pub(crate) fn frames_held() -> bool {
    HOLD_FRAMES.load(Ordering::Acquire) > 0
}

/// Makes the next `resize` calls hold the console slot until `release_resize`.
pub fn hold_resize() {
    *HOLD_RESIZE.lock().unwrap_or_else(|error| error.into_inner()) = true;
}

/// Releases the gate set by `hold_resize`.
pub fn release_resize() {
    *HOLD_RESIZE.lock().unwrap_or_else(|error| error.into_inner()) = false;
    RESIZE_RELEASED.notify_all();
}

/// Waits until a `resize` call is inside the gate, i.e. is holding the console slot. `false` when no
/// resize entered within `timeout`.
pub fn wait_until_resize_holds(timeout: Duration) -> bool {
    let end = Instant::now() + timeout;
    let mut inside = RESIZE_INSIDE.lock().unwrap_or_else(|error| error.into_inner());
    loop {
        if *inside {
            return true;
        }
        let remaining = end.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return false;
        }
        inside = RESIZE_INSIDE_CHANGED
            .wait_timeout(inside, remaining)
            .map(|(guard, _)| guard)
            .unwrap_or_else(|error| error.into_inner().0);
    }
}

/// Called by `resize` after it took the console slot, so a held gate is a held slot.
pub(crate) fn hold_resize_slot() {
    {
        let held = HOLD_RESIZE.lock().unwrap_or_else(|error| error.into_inner());
        if !*held {
            return;
        }
    }
    *RESIZE_INSIDE.lock().unwrap_or_else(|error| error.into_inner()) = true;
    RESIZE_INSIDE_CHANGED.notify_all();
    let end = Instant::now() + HOLD_LIMIT;
    let mut held = HOLD_RESIZE.lock().unwrap_or_else(|error| error.into_inner());
    while *held {
        let remaining = end.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        held = RESIZE_RELEASED
            .wait_timeout(held, remaining)
            .map(|(guard, _)| guard)
            .unwrap_or_else(|error| error.into_inner().0);
    }
    drop(held);
    *RESIZE_INSIDE.lock().unwrap_or_else(|error| error.into_inner()) = false;
    RESIZE_INSIDE_CHANGED.notify_all();
}

/// `true` while a console socket's frame write is parked, so a test can make its queue overflow or
/// trip the stall watchdog without filling a real TCP buffer.
static HOLD_WRITES: Mutex<bool> = Mutex::new(false);
/// Wakes a parked write when the test releases it.
static WRITES_RELEASED: Condvar = Condvar::new();
/// `true` while a socket is inside the gate (so a test knows its queue is really not being drained).
static WRITE_INSIDE: Mutex<bool> = Mutex::new(false);
/// Wakes `wait_until_socket_write_held` when a socket enters or leaves the gate.
static WRITE_INSIDE_CHANGED: Condvar = Condvar::new();

/// The session whose next socket frame write panics; `None` in every normal run. One socket's panic
/// must cost that socket only (§6.3), and no other way reaches that path without a real fault.
static PANIC_WRITE_SESSION: Mutex<Option<String>> = Mutex::new(None);

/// Makes the next frame write of `session_id`'s sockets panic, and only that session's: other sessions'
/// sockets in the same test binary keep running, parallel tests included.
pub fn panic_socket_writes_for(session_id: &str) {
    *PANIC_WRITE_SESSION
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = Some(session_id.to_string());
}

/// Called by the socket thread before it writes frames: panics while the test asked it to.
pub(crate) fn panic_socket_write(session_id: &str) {
    let armed = {
        let mut slot = PANIC_WRITE_SESSION
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        match slot.as_deref() {
            Some(id) if id == session_id => {
                *slot = None;
                true
            }
            _ => false,
        }
    };
    if armed {
        panic!("injected socket write panic (test hook)");
    }
}

/// Parks every console socket's frame write until `release_socket_writes`.
pub fn hold_socket_writes() {
    *HOLD_WRITES
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = true;
}

/// Unparks them.
pub fn release_socket_writes() {
    *HOLD_WRITES
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = false;
    WRITES_RELEASED.notify_all();
}

/// Waits until a socket is parked inside the gate. `false` when none entered within `timeout`.
pub fn wait_until_socket_write_held(timeout: Duration) -> bool {
    let end = Instant::now() + timeout;
    let mut inside = WRITE_INSIDE
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    loop {
        if *inside {
            return true;
        }
        let remaining = end.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return false;
        }
        inside = WRITE_INSIDE_CHANGED
            .wait_timeout(inside, remaining)
            .map(|(guard, _)| guard)
            .unwrap_or_else(|error| error.into_inner().0);
    }
}

/// Called by the socket thread before it writes frames, so a held gate means "nothing is leaving the
/// queue". `closing` lets a close request through, or a watchdog close would wait out the gate.
pub(crate) fn hold_socket_write(mut closing: impl FnMut() -> bool) {
    {
        let held = HOLD_WRITES.lock().unwrap_or_else(|error| error.into_inner());
        if !*held {
            return;
        }
    }
    *WRITE_INSIDE
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = true;
    WRITE_INSIDE_CHANGED.notify_all();
    let end = Instant::now() + HOLD_LIMIT;
    let mut held = HOLD_WRITES.lock().unwrap_or_else(|error| error.into_inner());
    while *held && Instant::now() < end && !closing() {
        held = WRITES_RELEASED
            .wait_timeout(held, Duration::from_millis(5))
            .map(|(guard, _)| guard)
            .unwrap_or_else(|error| error.into_inner().0);
    }
    drop(held);
    *WRITE_INSIDE
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = false;
    WRITE_INSIDE_CHANGED.notify_all();
}

/// `true` while `Core::drain` is parked between collecting its records and taking the socket drops:
/// the window a drop has to fall into to be lost (review omp 11 / kimi 4).
static HOLD_DRAIN: Mutex<bool> = Mutex::new(false);
/// `true` while a drain sits in the gate.
static DRAIN_INSIDE: Mutex<bool> = Mutex::new(false);
/// Wakes the drain when the test opens the gate.
static DRAIN_RELEASED: Condvar = Condvar::new();
/// Wakes `wait_until_drain_held` when a drain enters or leaves the gate.
static DRAIN_INSIDE_CHANGED: Condvar = Condvar::new();

/// Parks the next drain between its record collection and the socket-drop take.
pub fn hold_drain_before_drops() {
    *HOLD_DRAIN.lock().unwrap_or_else(|error| error.into_inner()) = true;
}

/// Opens the gate again.
pub fn release_drain_before_drops() {
    *HOLD_DRAIN.lock().unwrap_or_else(|error| error.into_inner()) = false;
    DRAIN_RELEASED.notify_all();
}

/// Waits until a drain is parked inside the gate. `false` when none entered within `timeout`.
pub fn wait_until_drain_held(timeout: Duration) -> bool {
    let end = Instant::now() + timeout;
    let mut inside = DRAIN_INSIDE
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    loop {
        if *inside {
            return true;
        }
        let remaining = end.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return false;
        }
        inside = DRAIN_INSIDE_CHANGED
            .wait_timeout(inside, remaining)
            .map(|(guard, _)| guard)
            .unwrap_or_else(|error| error.into_inner().0);
    }
}

/// Called by `drain` after its records are collected and before the drops are taken, so a test can land
/// a socket drop in exactly the window that used to mark it reported with no record to carry it.
pub(crate) fn drain_before_drops_gate() {
    {
        let held = HOLD_DRAIN.lock().unwrap_or_else(|error| error.into_inner());
        if !*held {
            return;
        }
    }
    *DRAIN_INSIDE
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = true;
    DRAIN_INSIDE_CHANGED.notify_all();
    let end = Instant::now() + HOLD_LIMIT;
    let mut held = HOLD_DRAIN.lock().unwrap_or_else(|error| error.into_inner());
    while *held && Instant::now() < end {
        held = DRAIN_RELEASED
            .wait_timeout(held, Duration::from_millis(5))
            .map(|(guard, _)| guard)
            .unwrap_or_else(|error| error.into_inner().0);
    }
    drop(held);
    *DRAIN_INSIDE
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = false;
    DRAIN_INSIDE_CHANGED.notify_all();
}
