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
