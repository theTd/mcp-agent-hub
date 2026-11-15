//! The W9 trim cases the pyte oracle pinned, ported from `test_history_trim.py` at step 6.
//!
//! The oracle's `trim_history_text` and its own test file were deleted with it; `history.rs` already
//! carries the same rule with unit tests for the short-history, line-break, prefix-only, and
//! banner-dropped cases. These are the cases those unit tests do not cover exactly: the budget at
//! the prefix's own length, every cut offset inside the banner line, a cut before the banner's
//! leading CRLF, only the newest banner being protected, and the banner-line length bound.

use terminal_stream::{trim_history_text, BANNER_LINE_MAX_BYTES, TRUNCATED_PREFIX};

fn lines(tag: &str, count: usize) -> String {
    (0..count)
        .map(|index| format!("{tag} line {index:03}\r\n"))
        .collect()
}

fn banner(count: usize) -> String {
    format!("\r\n\x1b[90m── 已恢复（第 {count} 次）──\x1b[0m\r\n")
}

/// `test_history_trim._banner_case`: the budget's own cut lands `cut_offset` bytes into the banner.
fn banner_case(cut_offset: isize) -> (String, String, String, usize) {
    let old = lines("old", 40);
    let banner = banner(3);
    let new = lines("new", 4);
    let text = format!("{old}{banner}{new}");
    let banner_start = old.len() as isize;
    let budget = text.len() as isize - (banner_start + cut_offset);
    let max_bytes = budget as usize + TRUNCATED_PREFIX.len();
    (text, banner, new, max_bytes)
}

#[test]
fn a_budget_inside_the_prefix_returns_the_prefix_alone() {
    let text = lines("a", 50);
    assert_eq!(trim_history_text(&text, 0), "");
    // Exactly the prefix's length is the boundary the unit test's smaller budget does not pin.
    assert_eq!(
        trim_history_text(&text, TRUNCATED_PREFIX.len()),
        TRUNCATED_PREFIX
    );
}

#[test]
fn a_cut_inside_the_banner_line_keeps_the_whole_banner() {
    let banner_bytes = banner(3).len();
    for offset in [1, 2, 5, banner_bytes / 2, banner_bytes - 1] {
        let (text, banner, new, max_bytes) = banner_case(offset as isize);
        let trimmed = trim_history_text(&text, max_bytes);
        assert_eq!(
            trimmed,
            format!("{TRUNCATED_PREFIX}{banner}{new}"),
            "offset {offset}"
        );
        let over = trimmed.len() - max_bytes;
        assert_eq!(over, offset, "offset {offset}");
        assert!(over < BANNER_LINE_MAX_BYTES, "offset {offset}");
    }
}

#[test]
fn a_cut_before_the_banner_keeps_its_leading_line_break() {
    let (text, banner, new, max_bytes) = banner_case(0);
    assert_eq!(
        trim_history_text(&text, max_bytes),
        format!("{TRUNCATED_PREFIX}{banner}{new}")
    );
    // A cut a few bytes into the last old line moves to the banner's own leading line break.
    let (text, banner, new, max_bytes) = banner_case(-3);
    let trimmed = trim_history_text(&text, max_bytes);
    assert_eq!(trimmed, format!("{TRUNCATED_PREFIX}{banner}{new}"));
    assert!(trimmed.len() <= max_bytes);
}

#[test]
fn only_the_newest_banner_is_protected() {
    let text = format!(
        "{}{}{}{}{}",
        lines("a", 30),
        banner(1),
        lines("b", 30),
        banner(2),
        lines("c", 2)
    );
    let trimmed = trim_history_text(&text, 300);
    assert!(!trimmed.contains(banner(1).as_str()));
    assert!(trimmed.contains(banner(2).as_str()));
}

#[test]
fn a_marker_on_an_overlong_line_is_not_treated_as_a_banner() {
    // The marker text followed by a huge line with no break: protecting it would keep that line
    // whole and blow far past the cap, so the plain cut applies.
    let marker_line = format!("\x1b[90m── 已恢复（{}", "x".repeat(4000));
    let text = format!("{}\r\n{marker_line}", lines("a", 30));
    let trimmed = trim_history_text(&text, 1000);
    assert!(trimmed.len() <= 1000);
}
