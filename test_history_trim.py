"""W9: trim_history_text caps the raw terminal history and keeps the newest resume banner whole."""
import unittest

from session_manager import (
    HISTORY_BANNER_LINE_MAX_BYTES,
    HISTORY_TRUNCATED_PREFIX,
    resume_banner,
    trim_history_text,
)

PREFIX_BYTES = len(HISTORY_TRUNCATED_PREFIX.encode("utf-8"))


def lines(tag: str, count: int) -> str:
    return "".join(f"{tag} line {i:03d}\r\n" for i in range(count))


class HistoryTrimTests(unittest.TestCase):
    def test_a_history_within_the_cap_is_returned_unchanged(self):
        text = lines("a", 3)
        self.assertEqual(trim_history_text(text, len(text.encode("utf-8"))), text)

    def test_no_budget_left_after_the_prefix(self):
        self.assertEqual(trim_history_text(lines("a", 50), 0), "")
        self.assertEqual(trim_history_text(lines("a", 50), PREFIX_BYTES), HISTORY_TRUNCATED_PREFIX)

    def test_the_cut_moves_to_the_next_line_break(self):
        text = lines("a", 50)
        trimmed = trim_history_text(text, 200)
        self.assertTrue(trimmed.startswith(HISTORY_TRUNCATED_PREFIX))
        body = trimmed[len(HISTORY_TRUNCATED_PREFIX):]
        self.assertTrue(text.endswith(body))
        self.assertTrue(body.startswith("a line "))
        self.assertLessEqual(len(trimmed.encode("utf-8")), 200)

    def _banner_case(self, cut_offset: int):
        """old output, the banner, new output; the budget cut lands cut_offset bytes into the banner."""
        old, banner, new = lines("old", 40), resume_banner(3), lines("new", 4)
        raw = (old + banner + new).encode("utf-8")
        banner_start = len(old.encode("utf-8"))
        budget = len(raw) - (banner_start + cut_offset)
        return old + banner + new, banner, new, budget + PREFIX_BYTES

    def test_a_cut_inside_the_banner_line_keeps_the_whole_banner(self):
        banner_bytes = len(resume_banner(3).encode("utf-8"))
        for offset in (1, 2, 5, banner_bytes // 2, banner_bytes - 1):
            with self.subTest(offset=offset):
                text, banner, new, max_bytes = self._banner_case(offset)
                trimmed = trim_history_text(text, max_bytes)
                self.assertEqual(trimmed, HISTORY_TRUNCATED_PREFIX + banner + new)
                over = len(trimmed.encode("utf-8")) - max_bytes
                self.assertEqual(over, offset)
                self.assertLess(over, HISTORY_BANNER_LINE_MAX_BYTES)

    def test_a_cut_before_the_banner_keeps_its_leading_line_break(self):
        text, banner, new, max_bytes = self._banner_case(0)
        self.assertEqual(trim_history_text(text, max_bytes), HISTORY_TRUNCATED_PREFIX + banner + new)
        # A cut a few bytes into the last old line moves to the banner's own leading line break.
        text, banner, new, max_bytes = self._banner_case(-3)
        trimmed = trim_history_text(text, max_bytes)
        self.assertEqual(trimmed, HISTORY_TRUNCATED_PREFIX + banner + new)
        self.assertLessEqual(len(trimmed.encode("utf-8")), max_bytes)

    def test_a_banner_that_ends_before_the_cut_is_dropped(self):
        banner_bytes = len(resume_banner(3).encode("utf-8"))
        text, _, _, max_bytes = self._banner_case(banner_bytes)
        trimmed = trim_history_text(text, max_bytes)
        self.assertNotIn("已恢复", trimmed)
        self.assertLessEqual(len(trimmed.encode("utf-8")), max_bytes)

    def test_only_the_newest_banner_is_protected(self):
        text = lines("a", 30) + resume_banner(1) + lines("b", 30) + resume_banner(2) + lines("c", 2)
        trimmed = trim_history_text(text, 300)
        self.assertNotIn(resume_banner(1), trimmed)
        self.assertIn(resume_banner(2), trimmed)

    def test_a_marker_on_an_overlong_line_is_not_treated_as_a_banner(self):
        # The marker text followed by a huge line with no break: protecting it would keep that line
        # whole and blow far past the cap, so the plain cut applies.
        marker_line = "\x1b[90m── 已恢复（" + "x" * 4000
        text = lines("a", 30) + "\r\n" + marker_line
        trimmed = trim_history_text(text, 1000)
        self.assertLessEqual(len(trimmed.encode("utf-8")), 1000)


if __name__ == "__main__":
    unittest.main()
