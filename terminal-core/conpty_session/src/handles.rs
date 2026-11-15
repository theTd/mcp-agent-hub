//! RAII ownership for the Win32 handles a session holds. Every `unsafe` block in this crate is a Win32
//! FFI call and carries a `// SAFETY:` note; this module is the only place that closes handles, so a
//! handle is closed exactly once and from whichever thread drops the last owner.

use windows::Win32::Foundation::{CloseHandle, HANDLE};
use windows::Win32::System::Console::{ClosePseudoConsole, HPCON};

/// An owned Win32 handle (process, thread, pipe end, job).
#[derive(Debug)]
pub struct OwnedHandle(HANDLE);

// SAFETY: a HANDLE is a reference to a kernel object; the Win32 API allows opening, using and closing
// one from any thread of the process, which is exactly what the reader/writer/kill threads do.
unsafe impl Send for OwnedHandle {}
unsafe impl Sync for OwnedHandle {}

impl OwnedHandle {
    /// Takes ownership of a handle that is no longer closed anywhere else.
    pub fn new(handle: HANDLE) -> Self {
        Self(handle)
    }

    pub fn get(&self) -> HANDLE {
        self.0
    }
}

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if !self.0.is_invalid() {
            // SAFETY: the handle is owned by this value and this is its only close.
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }
}

/// An owned pseudoconsole. `ClosePseudoConsole` must never run on the session's reader thread (§12.7),
/// so a live session only ever takes it out and closes it on a helper thread. Dropping this value
/// closes it as well: that is the spawn-error path, where no read is pending yet (should-fix 1).
#[derive(Debug)]
pub struct OwnedPseudoConsole(Option<HPCON>);

// SAFETY: HPCON is an opaque kernel handle value; closing it from a helper thread is the documented
// way to avoid the pending-read hang.
unsafe impl Send for OwnedPseudoConsole {}

impl OwnedPseudoConsole {
    pub fn new(handle: HPCON) -> Self {
        Self(Some(handle))
    }

    /// The raw handle. Panics only if called after `close`, which consumes the value.
    pub fn get(&self) -> HPCON {
        self.0.expect("pseudoconsole handle used after close")
    }

    /// Closes the pseudoconsole. Consumes the value: a second close is impossible by type.
    pub fn close(mut self) {
        self.close_in_place();
    }

    fn close_in_place(&mut self) {
        if let Some(handle) = self.0.take() {
            // SAFETY: owned, and the handle is taken out here, so no other close can follow.
            unsafe { ClosePseudoConsole(handle) }
        }
    }
}

impl Drop for OwnedPseudoConsole {
    fn drop(&mut self) {
        // A spawn that failed after `CreatePseudoConsole` never started a reader, so closing here cannot
        // hit the pending-read hang; anywhere else the value is taken by `close` on a helper thread.
        self.close_in_place();
    }
}
