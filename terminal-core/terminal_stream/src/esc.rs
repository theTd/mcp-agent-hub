//! The three strips that run before anything else: W8's `ESC[3J` (with a carry), the OSC colour-query
//! strip on output, and the OSC terminal-reply strip for browser input.
//!
//! Each is the hub's own algorithm (`_strip_saved_lines_erase`, `OSC_COLOR_QUERY_RE`,
//! `strip_osc_terminal_replies`), not a reimplementation of xterm's: the carry sizes, the guards and the
//! patterns are the ones whose behaviour the corpus froze.

use std::borrow::Cow;
use std::sync::LazyLock;

use regex::Regex;

/// `ESC[3J`, the erase-saved-lines sequence W8 drops on resumed sessions.
const ESC3J: &str = "\x1b[3J";

/// W8: strips `ESC[3J` with up to three trailing bytes carried into the next read, so a sequence split
/// over two reads is still dropped whole. `ESC[2J` is deliberately left alone.
#[derive(Default)]
pub struct Esc3jStrip {
    carry: String,
}

impl Esc3jStrip {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn strip(&mut self, data: &str) -> String {
        if data.is_empty() && self.carry.is_empty() {
            return String::new();
        }
        let mut out = if self.carry.is_empty() {
            data.to_string()
        } else {
            let mut joined = std::mem::take(&mut self.carry);
            joined.push_str(data);
            joined
        };
        if out.contains(ESC3J) {
            out = out.replace(ESC3J, "");
        }
        for size in [3usize, 2, 1] {
            if out.ends_with(&ESC3J[..size]) {
                self.carry = out[out.len() - size..].to_string();
                out.truncate(out.len() - size);
                break;
            }
        }
        out
    }
}

/// `OSC_COLOR_QUERY_RE`: OSC 10/11/12 (and 4;N) colour *queries* never reach the console or the history.
pub fn strip_color_queries(text: &str) -> Cow<'_, str> {
    static PATTERN: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\x1b\](?:4;\d+|1[0-9]);\?(?:\x07|\x1b\\)").expect("colour-query pattern")
    });
    PATTERN.replace_all(text, "")
}

/// `OSC_TERMINAL_REPLY_RE`: the automatic replies xterm.js sends for colour queries, a clipboard reply,
/// and the `1[01]\d` family. Applied to browser input before it can reach a PTY (step 4 wires it).
pub fn strip_terminal_replies(text: &str) -> Cow<'_, str> {
    static PATTERN: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\x1b\](?:4;\d+|1[0-9]|52|1[01]\d);[^\x07\x1b]*(?:\x07|\x1b\\)")
            .expect("terminal-reply pattern")
    });
    if !text.contains("\x1b]") {
        return Cow::Borrowed(text);
    }
    PATTERN.replace_all(text, "")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_esc3j_carry_drops_a_split_sequence_whole() {
        let mut strip = Esc3jStrip::new();
        assert_eq!(strip.strip("a\x1b[3"), "a");
        assert_eq!(strip.strip("Jb"), "b");
        let mut strip = Esc3jStrip::new();
        assert_eq!(strip.strip("a\x1b"), "a");
        assert_eq!(strip.strip("[3Jb"), "b");
        let mut strip = Esc3jStrip::new();
        assert_eq!(strip.strip("a\x1b[3Jb"), "ab");
        // ESC[2J is not the sequence W8 drops.
        let mut strip = Esc3jStrip::new();
        assert_eq!(strip.strip("\x1b[2J"), "\x1b[2J");
    }

    #[test]
    fn colour_queries_are_removed_but_other_osc_is_kept() {
        assert_eq!(strip_color_queries("\x1b]11;?\x07ok"), "ok");
        assert_eq!(strip_color_queries("\x1b]4;1;?\x1b\\x"), "x");
        assert_eq!(strip_color_queries("\x1b]0;title\x07"), "\x1b]0;title\x07");
        assert_eq!(strip_color_queries("\x1b]11;rgb:0000/0000/0000\x07"), "\x1b]11;rgb:0000/0000/0000\x07");
    }

    #[test]
    fn terminal_replies_cover_the_52_and_1x_families() {
        assert_eq!(strip_terminal_replies("\x1b]10;rgb:1/1/1\x07a"), "a");
        assert_eq!(strip_terminal_replies("\x1b]52;c;YWJj\x07b"), "b");
        assert_eq!(strip_terminal_replies("\x1b]110;rgb:0/0/0\x07c"), "c");
        assert_eq!(strip_terminal_replies("\x1b]119;rgb:0/0/0\x1b\\d"), "d");
        // A message that does not complete the reply keeps it: the strip is per message.
        assert_eq!(strip_terminal_replies("\x1b]10;rgb:1/1"), "\x1b]10;rgb:1/1");
        assert_eq!(strip_terminal_replies("plain"), "plain");
    }
}
