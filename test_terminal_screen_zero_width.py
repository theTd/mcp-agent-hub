"""HubTerminalScreen.draw skips zero-width and unprintable characters instead of dropping the rest of the run."""
import unittest

from session_manager import HubTerminalScreen, HubTerminalStream


def render(*chunks: str, columns: int = 20, lines: int = 3) -> list:
    screen = HubTerminalScreen(columns, lines)
    stream = HubTerminalStream(screen)
    for chunk in chunks:
        stream.feed(chunk)
    return [row.rstrip() for row in screen.display]


class ZeroWidthDrawTests(unittest.TestCase):
    def test_text_after_a_zero_width_space_is_drawn(self):
        self.assertEqual(render("ab​cd")[0], "abcd")

    def test_text_after_the_emoji_selector_is_drawn(self):
        # VS16 follows most emoji in CLI output ("⚠️ warning"); pyte used to drop the rest.
        self.assertEqual(render("⚠️ warning")[0], "⚠ warning")
        self.assertEqual(render("\U0001f44d️ ok")[0], "\U0001f44d ok")

    def test_a_zero_width_joiner_sequence_keeps_every_part(self):
        self.assertEqual(render("a\U0001f468‍\U0001f469b")[0], "a\U0001f468\U0001f469b")

    def test_combining_marks_still_compose_with_the_previous_cell(self):
        self.assertEqual(render("éx")[0], "éx")

    def test_a_zero_width_character_does_not_take_a_cell(self):
        self.assertEqual(render("​" * 5 + "abc")[0], "abc")

    def test_a_zero_width_character_at_the_edge_does_not_wrap(self):
        rows = render("abcde​", columns=5)
        self.assertEqual(rows[:2], ["abcde", ""])
        # The next printable character still wraps as usual.
        self.assertEqual(render("abcde​f", columns=5)[:2], ["abcde", "f"])

    def test_plain_ascii_and_wide_text_are_unchanged(self):
        self.assertEqual(render("hello world")[0], "hello world")
        self.assertEqual(render("中文 ok")[0], "中文 ok")

    def test_the_rest_of_a_split_run_still_draws(self):
        self.assertEqual(render("ab​", "cd")[0], "abcd")


if __name__ == "__main__":
    unittest.main()
