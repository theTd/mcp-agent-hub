//! OS randomness for the terminal ticket (§6.4): the 32-byte key minted once per `Core` and the
//! 16-byte nonce of every ticket. Python can reach neither.
//!
//! `BCryptGenRandom` with the system-preferred RNG needs no algorithm handle, so there is nothing to
//! release and nothing to initialize before the first call.

use windows::Win32::Security::Cryptography::{BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG};

/// Fills `buffer` from the OS CSPRNG. `Err` when the system refuses, which the caller turns into a
/// failed `Core` start rather than a predictable key.
pub fn fill_random(buffer: &mut [u8]) -> Result<(), String> {
    if buffer.is_empty() {
        return Ok(());
    }
    // SAFETY: `buffer` is a live mutable slice and its length is handed over with it, so
    // BCryptGenRandom writes only inside the slice; `None` selects the system-preferred RNG, which
    // takes no algorithm handle and therefore leaves no handle to free.
    let status = unsafe { BCryptGenRandom(None, buffer, BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
    if status.is_ok() {
        Ok(())
    } else {
        Err(format!("BCryptGenRandom failed: 0x{:08x}", status.0))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn two_calls_do_not_return_the_same_bytes() {
        let mut first = [0u8; 32];
        let mut second = [0u8; 32];
        fill_random(&mut first).expect("random");
        fill_random(&mut second).expect("random");
        assert_ne!(first, second, "the OS RNG returned the same key twice");
        assert_ne!(first, [0u8; 32], "the buffer was left untouched");
    }

    #[test]
    fn an_empty_request_is_not_an_error() {
        assert!(fill_random(&mut []).is_ok());
    }
}
