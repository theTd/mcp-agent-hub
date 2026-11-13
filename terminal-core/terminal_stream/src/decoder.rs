//! UTF-8 decoding with a carry of at most three bytes, so a character split over two ConPTY reads
//! arrives whole (doc §5.4).
//!
//! `pty.read()` handed Python a `str`; the system-ConPTY recordings are the raw pipe bytes, so the core
//! owns the decoder. `std::str::from_utf8`'s error split (`valid_up_to` + `error_len`) follows the same
//! maximal-subpart rule as CPython's `codecs` "replace" handler, which the oracle used, and the tests
//! below pin the shapes where that matters (an overlong lead, a surrogate, an out-of-range lead).

pub struct Utf8Carry {
    pending: Vec<u8>,
}

impl Default for Utf8Carry {
    fn default() -> Self {
        Self::new()
    }
}

impl Utf8Carry {
    pub fn new() -> Self {
        Self {
            pending: Vec::with_capacity(4),
        }
    }

    /// Decodes `bytes` into `out`, holding back an incomplete trailing sequence for the next read.
    pub fn decode(&mut self, bytes: &[u8], out: &mut String) {
        if self.pending.is_empty() {
            self.drain(bytes, out);
            return;
        }
        let mut joined = std::mem::take(&mut self.pending);
        joined.extend_from_slice(bytes);
        self.drain(&joined, out);
    }

    /// The stream ended: an incomplete carry can only become one replacement character.
    pub fn finish(&mut self, out: &mut String) {
        if self.pending.is_empty() {
            return;
        }
        out.push_str(&String::from_utf8_lossy(&self.pending));
        self.pending.clear();
    }

    fn drain(&mut self, mut data: &[u8], out: &mut String) {
        self.pending.clear();
        loop {
            match std::str::from_utf8(data) {
                Ok(text) => {
                    out.push_str(text);
                    return;
                }
                Err(error) => {
                    let valid = error.valid_up_to();
                    if valid > 0 {
                        out.push_str(std::str::from_utf8(&data[..valid]).unwrap_or_default());
                    }
                    match error.error_len() {
                        Some(len) => {
                            out.push('\u{FFFD}');
                            data = &data[valid + len..];
                        }
                        None => {
                            self.pending.extend_from_slice(&data[valid..]);
                            return;
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decode_all(chunks: &[&[u8]]) -> String {
        let mut decoder = Utf8Carry::new();
        let mut out = String::new();
        for chunk in chunks {
            decoder.decode(chunk, &mut out);
        }
        decoder.finish(&mut out);
        out
    }

    #[test]
    fn a_character_split_over_two_reads_arrives_whole() {
        assert_eq!(decode_all(&[b"a\xe4\xb8", b"\xadb"]), "a中b");
        assert_eq!(decode_all(&[b"\xf0\x9f\x98", b"\x80"]), "😀");
        assert_eq!(decode_all(&[b"\xe4", b"\xb8", b"\xad"]), "中");
    }

    #[test]
    fn replacements_match_pythons_replace_handler() {
        // Expected values are CPython's: b'\xe0\x80'.decode('utf-8', 'replace') etc.
        assert_eq!(decode_all(&[b"\xe0\x80"]), "\u{FFFD}\u{FFFD}");
        assert_eq!(decode_all(&[b"\xed\xa0\x80"]), "\u{FFFD}\u{FFFD}\u{FFFD}");
        assert_eq!(decode_all(&[b"\xf4\x90\x80\x80"]), "\u{FFFD}\u{FFFD}\u{FFFD}\u{FFFD}");
        assert_eq!(decode_all(&[b"\xc0\xaf"]), "\u{FFFD}\u{FFFD}");
        assert_eq!(decode_all(&[b"a\xe0\x80b"]), "a\u{FFFD}\u{FFFD}b");
        assert_eq!(decode_all(&[b"\xe4\xb8"]), "\u{FFFD}");
    }
}
