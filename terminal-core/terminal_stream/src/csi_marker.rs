//! pyte's CSI private-marker quirk, kept for the screen because the hub's screens are the oracle
//! (DECISIONS.md `oracle-bugs`: today's Python is the oracle, bugs included).
//!
//! pyte's `Stream` ends a CSI at the first byte that is not a digit, `;`, `?`, a space or `>`, and
//! dispatches *that* byte as the final character. For `<` and `=` the handler is unknown, so the sequence
//! itself draws nothing and the bytes after the marker become plain text: `ESC[<u` prints `u`, and
//! `ESC[=5;1u` prints `5;1u`. Both are valid private CSIs (kitty keyboard, DEC private), which `vte`
//! parses correctly and prints nothing for — that alone moves every row the kimi, omp and agy recordings
//! paint afterwards, and no screen row of theirs would match pyte.
//!
//! So the parser is fed a rewritten stream while the ring and the socket frames keep the raw text: a
//! sequence with such a marker loses everything up to and including the marker, and its remaining bytes
//! are handed to the parser as text. Deleting this module (and re-rendering the goldens) is the whole
//! change once pyte is gone, which is why it is one file with one job.

/// A CSI longer than this cannot be a real one; the candidate is emitted unchanged so a broken stream
/// cannot pin the carry forever.
const MAX_CANDIDATE: usize = 4096;

pub struct CsiMarkerQuirk {
    /// Bytes of an undecided sequence (`ESC [ …` with no final byte yet).
    pending: String,
}

impl Default for CsiMarkerQuirk {
    fn default() -> Self {
        Self::new()
    }
}

impl CsiMarkerQuirk {
    pub fn new() -> Self {
        Self {
            pending: String::new(),
        }
    }

    /// Rewrites one chunk for the parser. The returned text is what `vte` sees; an incomplete trailing
    /// sequence is kept for the next call.
    pub fn rewrite(&mut self, text: &str) -> String {
        let mut joined = std::mem::take(&mut self.pending);
        joined.push_str(text);
        let bytes = joined.as_bytes();
        let mut out = String::with_capacity(joined.len());
        let mut at = 0usize;
        while at < bytes.len() {
            if bytes[at] != 0x1b {
                let start = at;
                while at < bytes.len() && bytes[at] != 0x1b {
                    at += 1;
                }
                out.push_str(&joined[start..at]);
                continue;
            }
            // `ESC`: the sequence matters only when `[` follows.
            if at + 1 >= bytes.len() {
                self.pending = joined[at..].to_string();
                return out;
            }
            if bytes[at + 1] != b'[' {
                out.push_str(&joined[at..at + 2]);
                at += 2;
                continue;
            }
            let body = at + 2;
            let mut scan = body;
            let mut marker = None;
            while scan < bytes.len() {
                let byte = bytes[scan];
                if (0x20..=0x3f).contains(&byte) {
                    if !byte.is_ascii_digit() && !matches!(byte, b';' | b'?' | b' ' | b'>') && marker.is_none()
                    {
                        marker = Some(scan);
                    }
                    scan += 1;
                    continue;
                }
                break;
            }
            if scan >= bytes.len() || !(0x40..=0x7e).contains(&bytes[scan]) {
                // No final byte yet (or a control byte inside the CSI, which pyte treats differently and
                // no recorded stream contains): hand the undecided tail over.
                let tail = joined[at..].to_string();
                if tail.len() > MAX_CANDIDATE {
                    out.push_str(&tail);
                    return out;
                }
                self.pending = tail;
                return out;
            }
            match marker {
                Some(index) => {
                    // pyte consumed `ESC [ params marker` and drew the rest: emit that rest as text.
                    out.push_str(&joined[index + 1..=scan]);
                }
                None => out.push_str(&joined[at..=scan]),
            }
            at = scan + 1;
        }
        out
    }

    /// The stream ended inside a candidate: emit it unchanged (pyte's own parser would have dropped it at
    /// the sequence end, but a truncated sequence at EOF draws nothing either way).
    pub fn finish(&mut self) -> String {
        std::mem::take(&mut self.pending)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rewrite_all(chunks: &[&str]) -> String {
        let mut quirk = CsiMarkerQuirk::new();
        let mut out = String::new();
        for chunk in chunks {
            out.push_str(&quirk.rewrite(chunk));
        }
        out.push_str(&quirk.finish());
        out
    }

    #[test]
    fn a_marker_sequence_leaves_its_residue_as_text() {
        assert_eq!(rewrite_all(&["a\x1b[<ub"]), "aub");
        assert_eq!(rewrite_all(&["a\x1b[=5;1ub"]), "a5;1ub");
        assert_eq!(rewrite_all(&["a\x1b[<1ub"]), "a1ub");
        assert_eq!(rewrite_all(&["\x1b[?"]), "\x1b[?");
    }

    #[test]
    fn ordinary_and_skipped_marker_sequences_are_untouched() {
        for kept in [
            "\x1b[31m",
            "\x1b[?25h",
            "\x1b[>4;2m",
            "\x1b[1;2H",
            "\x1b[?1049h",
            "\x1b]0;title\x07",
            "plain",
        ] {
            assert_eq!(rewrite_all(&[kept]), kept, "{kept:?}");
        }
    }

    #[test]
    fn a_sequence_split_over_two_chunks_is_decided_once() {
        assert_eq!(rewrite_all(&["a\x1b[<", "ub"]), "aub");
        assert_eq!(rewrite_all(&["a\x1b[3", "1mb"]), "a\x1b[31mb");
        assert_eq!(rewrite_all(&["tail\x1b"]), "tail\x1b");
    }
}
