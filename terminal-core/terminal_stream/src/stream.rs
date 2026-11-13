//! The per-chunk output pipeline, in the order `_on_pty_output` runs it.
//!
//! One read goes through: UTF-8 decode with the carry -> W8 `ESC[3J` strip (resumed sessions) -> OSC
//! colour-query strip -> append to the W9 ring -> parse into the screen -> bump the screen revision. What
//! a console socket must receive is written to `out` (the stripped, decoded text), which is the same
//! string Python pushed to every `ws_queues` entry.
//!
//! The ring is appended *before* the parser runs, as the brief orders it: the reader thread's exit
//! ordering (step 1) relies on "all bytes are in the ring before the death record", and a parser panic
//! must not cost the history (K4: one unparseable chunk costs itself).

use std::borrow::Cow;
use std::panic::{catch_unwind, AssertUnwindSafe};

use vte::Parser;

use crate::csi_marker::CsiMarkerQuirk;
use crate::decoder::Utf8Carry;
use crate::esc::{strip_color_queries, Esc3jStrip};
use crate::history::HistoryRing;
use crate::screen::Screen;

/// Per-session limits. Defaults are §5.2's spawn flags (`history_rows` 3000, `raw_cap_bytes` 512 KiB,
/// `raw_slack_bytes` 64 KiB); the strip flags come from Python.
#[derive(Clone, Copy, Debug)]
pub struct StreamOptions {
    pub cols: usize,
    pub rows: usize,
    pub history_rows: usize,
    pub raw_cap_bytes: usize,
    pub raw_slack_bytes: usize,
    pub strip_esc3j_on_output: bool,
    pub strip_osc_color_queries: bool,
}

impl Default for StreamOptions {
    fn default() -> Self {
        Self {
            cols: 80,
            rows: 24,
            history_rows: 3000,
            raw_cap_bytes: 512 * 1024,
            raw_slack_bytes: 64 * 1024,
            strip_esc3j_on_output: false,
            strip_osc_color_queries: true,
        }
    }
}

pub struct TerminalStream {
    parser: Parser,
    screen: Screen,
    ring: HistoryRing,
    esc3j: Esc3jStrip,
    marker: CsiMarkerQuirk,
    decoder: Utf8Carry,
    /// The decode scratch, reused so a read does not allocate a `String` per chunk.
    scratch: String,
    raw_cap_bytes: usize,
    strip_esc3j_on_output: bool,
    strip_osc_color_queries: bool,
    screen_rev: u64,
    parser_panics: u64,
}

impl TerminalStream {
    pub fn new(opts: StreamOptions) -> Self {
        Self {
            parser: Parser::new(),
            screen: Screen::new(opts.cols, opts.rows, opts.history_rows),
            ring: HistoryRing::new(opts.raw_cap_bytes, opts.raw_slack_bytes),
            esc3j: Esc3jStrip::new(),
            marker: CsiMarkerQuirk::new(),
            decoder: Utf8Carry::new(),
            scratch: String::new(),
            raw_cap_bytes: opts.raw_cap_bytes,
            strip_esc3j_on_output: opts.strip_esc3j_on_output,
            strip_osc_color_queries: opts.strip_osc_color_queries,
            screen_rev: 0,
            parser_panics: 0,
        }
    }

    /// A resumed session's raw ring starts from the archived history (`spawn.seed_history`, A1).
    pub fn seed_history(&mut self, seed: &str) {
        self.ring.seed(seed);
    }

    /// One pipe read: decode with the carry, then run the pipeline on the text.
    pub fn feed_bytes(&mut self, bytes: &[u8], out: &mut Vec<u8>) {
        let mut text = std::mem::take(&mut self.scratch);
        text.clear();
        self.decoder.decode(bytes, &mut text);
        self.feed_text(&text, out);
        self.scratch = text;
    }

    /// The stream ended: an incomplete trailing character becomes its replacement character.
    pub fn finish(&mut self, out: &mut Vec<u8>) {
        let mut text = std::mem::take(&mut self.scratch);
        text.clear();
        self.decoder.finish(&mut text);
        if !text.is_empty() {
            self.feed_text(&text, out);
        }
        let tail = self.marker.finish();
        if !tail.is_empty() {
            let Self {
                parser, screen, ..
            } = self;
            let _ = catch_unwind(AssertUnwindSafe(|| parser.advance(screen, tail.as_bytes())));
        }
        self.scratch = text;
    }

    /// The pipeline minus the UTF-8 step, for text that is already decoded (`feed`, the fixtures' `text`
    /// corpora).
    pub fn feed_text(&mut self, text: &str, out: &mut Vec<u8>) {
        let mut data: Cow<'_, str> = Cow::Borrowed(text);
        if self.strip_esc3j_on_output {
            data = Cow::Owned(self.esc3j.strip(&data));
        }
        if self.strip_osc_color_queries && data.contains("\x1b]") {
            data = Cow::Owned(strip_color_queries(&data).into_owned());
        }
        self.ring.append(&data);
        out.extend_from_slice(data.as_bytes());
        // The parser sees pyte's private-marker rewrite; the ring and the socket frames keep the raw text.
        let parsed = self.marker.rewrite(&data);
        let Self {
            parser, screen, ..
        } = self;
        // K4: vte cannot fail on bad input, so this only fires on a bug in the model itself; the chunk
        // costs itself (the ring already has it) and the parser restarts, as `HubTerminalStream` does.
        if catch_unwind(AssertUnwindSafe(|| parser.advance(screen, parsed.as_bytes()))).is_err() {
            self.parser = Parser::new();
            self.parser_panics += 1;
        }
        self.screen_rev += 1;
    }

    pub fn resize(&mut self, cols: usize, rows: usize) {
        self.screen.resize(cols, rows);
        self.screen_rev += 1;
    }

    pub fn cols(&self) -> usize {
        self.screen.cols()
    }

    pub fn rows(&self) -> usize {
        self.screen.rows()
    }

    pub fn cursor(&self) -> (usize, usize) {
        self.screen.cursor()
    }

    pub fn screen_text(&self, max_lines: usize) -> String {
        self.screen.screen_text(max_lines)
    }

    pub fn scrollback_text(&self, max_lines: usize) -> String {
        self.screen.scrollback_text(max_lines)
    }

    /// `_scrollback_blob`'s read: the newest ≤ 512 KiB of the raw stream, cut at a line break.
    pub fn raw_text(&self) -> String {
        self.ring.trimmed()
    }

    /// The joined ring as it stands (the untrimmed `"".join(raw_history)`).
    pub fn raw_join(&self) -> &str {
        self.ring.text()
    }

    pub fn raw_bytes(&self) -> u64 {
        self.ring.bytes() as u64
    }

    pub fn raw_rev(&self) -> u64 {
        self.ring.rev()
    }

    pub fn raw_entries(&self) -> usize {
        self.ring.entries()
    }

    /// `raw_ansi_tail`: the last `max_chars` characters of the *untrimmed* ring, on a character boundary.
    pub fn raw_tail(&self, max_chars: usize) -> String {
        self.ring.tail(max_chars).to_string()
    }

    /// What `attach_ws_queue` hands a new console: the joined ring, then one more colour-query strip
    /// (`replay-shape`). Not the 512 KiB trim.
    pub fn replay_text(&self) -> String {
        let joined = self.ring.text();
        if joined.contains("\x1b]") {
            strip_color_queries(joined).into_owned()
        } else {
            joined.to_string()
        }
    }

    pub fn screen_rev(&self) -> u64 {
        self.screen_rev
    }

    pub fn ring_dropped_bytes(&self) -> u64 {
        self.ring.dropped_bytes()
    }

    pub fn parser_panics(&self) -> u64 {
        self.parser_panics
    }

    pub fn raw_cap_bytes(&self) -> usize {
        self.raw_cap_bytes
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stream() -> TerminalStream {
        TerminalStream::new(StreamOptions {
            cols: 20,
            rows: 3,
            history_rows: 10,
            ..StreamOptions::default()
        })
    }

    #[test]
    fn the_pipeline_publishes_what_it_keeps() {
        let mut stream = stream();
        let mut out = Vec::new();
        stream.feed_text("hello\x1b]11;?\x07 world", &mut out);
        assert_eq!(out, b"hello world");
        assert_eq!(stream.screen_text(0), "hello world");
        assert_eq!(stream.raw_text(), "hello world");
        assert_eq!(stream.screen_rev(), 1);
    }

    #[test]
    fn the_resumed_strip_drops_a_sequence_split_over_two_reads() {
        let mut stream = TerminalStream::new(StreamOptions {
            cols: 10,
            rows: 2,
            strip_esc3j_on_output: true,
            ..StreamOptions::default()
        });
        let mut out = Vec::new();
        stream.feed_text("a\x1b[3", &mut out);
        stream.feed_text("Jb", &mut out);
        assert_eq!(String::from_utf8(out.clone()).unwrap(), "ab");
        assert_eq!(stream.raw_text(), "ab");
    }

    #[test]
    fn a_split_utf8_character_reaches_the_ring_and_the_screen_whole() {
        let mut stream = stream();
        let mut out = Vec::new();
        stream.feed_bytes(b"\xe4\xb8", &mut out);
        stream.feed_bytes(b"\xad", &mut out);
        assert_eq!(stream.screen_text(0), "中");
        assert_eq!(stream.raw_tail(1), "中");
    }
}
