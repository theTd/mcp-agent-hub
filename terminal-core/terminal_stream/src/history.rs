//! W9: the raw history as one byte ring of decoded text, `trim_history_text`, and the in-loop collapse.
//!
//! Python keeps `raw_history` as a list of chunk strings and joins it when it trims; the ring keeps the
//! joined bytes instead, so a read is never a memmove (step 1's open item 1) and `raw`/`raw_tail`/the
//! replay are slices rather than a `"".join`. The cap counts UTF-8 bytes of the decoded text, exactly as
//! `len(data.encode("utf-8", "replace"))` did, and the collapse keeps the entry count so the trigger's
//! record stays observable (`edge-w9-inloop-trigger`).

/// `HISTORY_TRUNCATED_PREFIX` (`session_manager.py`): the grey notice a trimmed history starts with.
pub const TRUNCATED_PREFIX: &str = "\x1b[90m[较早的输出已截断]\x1b[0m\r\n";
/// `HISTORY_BANNER_LINE_MAX_BYTES`: the longest newest-banner line the trim keeps whole.
pub const BANNER_LINE_MAX_BYTES: usize = 128;
/// The start of `resume_banner` with `resume_count` cut off; `rfind` of this finds the newest banner.
const BANNER_MARKER: &str = "\x1b[90m── 已恢复（";

/// `trim_history_text`: cut from the front to a line break, prefix the notice, and keep the newest
/// resume banner whole whenever the budget's own cut lands before the banner line ends.
///
/// The result may exceed `max_bytes` by less than `BANNER_LINE_MAX_BYTES` when the banner was kept
/// (the owner fixed the rule before the port, DECISIONS.md `oracle-bugs`).
pub fn trim_history_text(text: &str, max_bytes: usize) -> String {
    if max_bytes == 0 {
        return String::new();
    }
    let raw = text.as_bytes();
    if raw.len() <= max_bytes {
        return text.to_string();
    }
    let prefix = TRUNCATED_PREFIX;
    if max_bytes <= prefix.len() {
        return prefix.to_string();
    }
    let budget = max_bytes - prefix.len();
    let cut = raw.len() - budget;
    let mut start = cut;
    if let Some(offset) = raw[cut..].iter().position(|&byte| byte == b'\n') {
        start = cut + offset + 1;
    }
    if let Some(idx) = text.rfind(BANNER_MARKER) {
        let marker_start = idx;
        let banner_start = if marker_start >= 2 && &raw[marker_start - 2..marker_start] == b"\r\n" {
            marker_start - 2
        } else {
            marker_start
        };
        let banner_end = match raw[marker_start..].iter().position(|&byte| byte == b'\n') {
            Some(offset) => marker_start + offset + 1,
            None => raw.len(),
        };
        if banner_start < start
            && cut < banner_end
            && banner_end - banner_start <= BANNER_LINE_MAX_BYTES
        {
            start = banner_start;
        }
    }
    let mut out = String::with_capacity(prefix.len() + raw.len() - start);
    out.push_str(prefix);
    out.push_str(&String::from_utf8_lossy(&raw[start..]));
    out
}

/// The ring itself: contiguous bytes, one entry count, one revision.
pub struct HistoryRing {
    data: Vec<u8>,
    entries: usize,
    cap: usize,
    slack: usize,
    rev: u64,
    dropped_bytes: u64,
    collapses: u64,
}

impl HistoryRing {
    pub fn new(cap: usize, slack: usize) -> Self {
        Self {
            data: Vec::new(),
            entries: 0,
            cap,
            slack,
            rev: 0,
            dropped_bytes: 0,
            collapses: 0,
        }
    }

    /// A resumed session starts with the archived scrollback as its one entry (`_make_resumed_session`).
    pub fn seed(&mut self, seed: &str) {
        self.data.clear();
        self.data.extend_from_slice(seed.as_bytes());
        self.entries = 1;
        self.rev += 1;
    }

    /// Append one chunk and collapse the ring when it passed `cap + slack`. Returns whether it collapsed.
    pub fn append(&mut self, text: &str) -> bool {
        self.data.extend_from_slice(text.as_bytes());
        self.entries += 1;
        self.rev += 1;
        if self.data.len() <= self.cap.saturating_add(self.slack) {
            return false;
        }
        let joined = self.text();
        let trimmed = trim_history_text(joined, self.cap);
        if trimmed == joined {
            return false;
        }
        self.dropped_bytes += (self.data.len() as u64).saturating_sub(trimmed.len() as u64);
        self.data.clear();
        self.data.extend_from_slice(trimmed.as_bytes());
        self.entries = 1;
        self.rev += 1;
        self.collapses += 1;
        true
    }

    pub fn text(&self) -> &str {
        std::str::from_utf8(&self.data).unwrap_or_default()
    }

    pub fn bytes(&self) -> usize {
        self.data.len()
    }

    pub fn entries(&self) -> usize {
        self.entries
    }

    pub fn rev(&self) -> u64 {
        self.rev
    }

    pub fn dropped_bytes(&self) -> u64 {
        self.dropped_bytes
    }

    pub fn collapses(&self) -> u64 {
        self.collapses
    }

    /// `_scrollback_blob`'s read: the joined ring trimmed to the cap (the ring may hold cap + slack).
    pub fn trimmed(&self) -> String {
        trim_history_text(self.text(), self.cap)
    }

    /// The last `max_chars` characters, the way `"".join(raw_history)[-65536:]` slices them: on a
    /// character boundary, so a CJK character is never split (`raw_tail`).
    pub fn tail(&self, max_chars: usize) -> &str {
        let bytes = &self.data;
        let mut index = bytes.len();
        let mut chars = 0;
        while index > 0 && chars < max_chars {
            index -= 1;
            if bytes[index] & 0xC0 != 0x80 {
                chars += 1;
            }
        }
        std::str::from_utf8(&bytes[index..]).unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const BANNER: &str = "\r\n\x1b[90m── 已恢复（第 1 次）──\x1b[0m\r\n";

    #[test]
    fn a_short_history_is_returned_unchanged() {
        assert_eq!(trim_history_text("abc", 16), "abc");
        assert_eq!(trim_history_text("abc", 0), "");
        assert_eq!(trim_history_text("", 16), "");
    }

    #[test]
    fn the_cut_moves_to_the_next_line_break_and_prefixes_the_notice() {
        // CPython: trim_history_text("0123456789\r\n" * 40, 200).
        let text = "0123456789\r\n".repeat(40);
        let trimmed = trim_history_text(&text, 200);
        assert_eq!(
            trimmed,
            format!("{}{}", TRUNCATED_PREFIX, "0123456789\r\n".repeat(13))
        );
        assert_eq!(trimmed.len(), 193);
    }

    #[test]
    fn a_budget_inside_the_prefix_returns_the_prefix_alone() {
        assert_eq!(trim_history_text(&"x".repeat(400), 20), TRUNCATED_PREFIX);
    }

    #[test]
    fn a_cut_inside_the_banner_line_keeps_the_banner_whole() {
        // The fixed rule (`oracle-bugs`, 2026-11-13): the cut lands inside the banner line, so the banner
        // and its leading CRLF are kept and the result passes max_bytes by less than the line.
        let text = format!("{}{}", "0123456789\r\n".repeat(40), BANNER);
        let trimmed = trim_history_text(&text, 67);
        assert_eq!(trimmed, format!("{TRUNCATED_PREFIX}{BANNER}"));
        assert_eq!(trimmed.len(), 87);
        assert!(trimmed.len() > 67);
    }

    #[test]
    fn a_banner_before_the_cut_is_dropped_with_the_rest() {
        let text = format!("{}{}{}", BANNER, "0123456789\r\n".repeat(40), "tail\r\n");
        let trimmed = trim_history_text(&text, 200);
        assert!(!trimmed.contains("已恢复"));
        assert!(trimmed.ends_with("tail\r\n"));
    }

    #[test]
    fn the_ring_collapses_and_keeps_counting_bytes() {
        let mut ring = HistoryRing::new(256, 64);
        let mut collapsed_during_a_run = false;
        for _ in 0..40 {
            let before = ring.collapses();
            ring.append(&"x".repeat(40));
            if ring.collapses() > before {
                assert_eq!(ring.entries(), 1, "a collapse leaves one entry");
                collapsed_during_a_run = true;
            }
        }
        assert!(collapsed_during_a_run);
        assert!(ring.bytes() <= 256 + 64 + 40);
        assert!(ring.rev() > 40);
    }

    #[test]
    fn the_tail_is_cut_on_a_character_boundary() {
        let mut ring = HistoryRing::new(1024, 0);
        ring.append("ab中文😀");
        assert_eq!(ring.tail(3), "中文😀");
        assert_eq!(ring.tail(2), "文😀");
        assert_eq!(ring.tail(100), "ab中文😀");
        assert_eq!(ring.tail(0), "");
    }
}
