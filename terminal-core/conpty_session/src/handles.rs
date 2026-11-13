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
/// so it is only ever taken out of the session and closed on a helper thread.
#[derive(Debug)]
pub struct OwnedPseudoConsole(HPCON);

// SAFETY: HPCON is an opaque kernel handle value; closing it from a helper thread is the documented
// way to avoid the pending-read hang.
unsafe impl Send for OwnedPseudoConsole {}

impl OwnedPseudoConsole {
    pub fn new(handle: HPCON) -> Self {
        Self(handle)
    }

    pub fn get(&self) -> HPCON {
        self.0
    }

    /// Closes the pseudoconsole. Consumes the value: a second close is impossible by type.
    pub fn close(self) {
        // SAFETY: owned, and the value is dropped here, so no other close can follow.
        unsafe { ClosePseudoConsole(self.0) }
    }
}
