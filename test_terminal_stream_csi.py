"""pyte ends a CSI at ':' '<' '='; the hub consumes such a sequence whole, whole or split."""
import unittest

from session_manager import HubTerminalScreen, HubTerminalStream, _split_marker_csi

COLS, ROWS = 40, 6
# The forms the CLIs send: kimi and omp pop their kitty-keyboard flags with ESC[<u, agy sets them
# with ESC[=5;1u and ESC[<1u; ':' is the SGR subparameter separator (ESC[38:2::r:g:b m).
MARKER_FORMS = ["\x1b[<u", "\x1b[=5;1u", "\x1b[<1u", "\x1b[1:2m", "\x1b[<1;2u", "\x1b[>4;1:2m"]


def render(chunks):
    """The screen after the chunks, row by row, with trailing blank rows dropped."""
    screen = HubTerminalScreen(COLS, ROWS)
    stream = HubTerminalStream(screen)
    for chunk in chunks:
        stream.feed(chunk)
    lines = [line.rstrip() for line in screen.display]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def every_split(text):
    return [[text[:i], text[i:]] for i in range(len(text) + 1)]


class MarkerCsiTests(unittest.TestCase):
    def test_a_marker_csi_draws_nothing(self):
        for form in MARKER_FORMS:
            with self.subTest(form=form.encode("unicode_escape")):
                self.assertEqual(render([form]), "")

    def test_a_marker_csi_split_across_two_feeds_draws_nothing(self):
        for form in MARKER_FORMS:
            for parts in every_split(form):
                with self.subTest(form=form.encode("unicode_escape"), parts=parts):
                    self.assertEqual(render(parts), "")

    def test_text_after_a_marker_csi_keeps_its_column(self):
        for form in MARKER_FORMS:
            with self.subTest(form=form.encode("unicode_escape")):
                self.assertEqual(render([form + "hello"]), "hello")
                self.assertEqual(render([form, "hello"]), "hello")
                self.assertEqual(render(["ab" + form + "hello"]), "abhello")

    def test_several_marker_csis_in_one_line(self):
        self.assertEqual(render(["\x1b[<u\x1b[=5;1u\x1b[<1uend"]), "end")

    def test_a_normal_csi_right_after_a_marker_csi_is_parsed(self):
        self.assertEqual(render(["\x1b[<u" + "\x1b[2;3Hx"]), "\n  x")
        self.assertEqual(render(["\x1b[<u", "\x1b[2;3Hx"]), "\n  x")

    def test_a_normal_csi_cut_at_the_end_of_a_chunk_still_renders(self):
        whole = render(["\x1b[2;3Hx"])
        for parts in every_split("\x1b[2;3Hx"):
            with self.subTest(parts=parts):
                self.assertEqual(render(parts), whole)

    def test_private_modes_still_apply(self):
        # DECAWM off: 45 characters stay on the first row instead of wrapping.
        self.assertEqual(render(["\x1b[?7l" + "X" * 45]), "X" * COLS)
        self.assertEqual(render(["\x1b[?7h" + "X" * 45]), "X" * COLS + "\n" + "X" * 5)
        # kimi's private status query is still read by pyte and still draws nothing.
        self.assertEqual(render(["\x1b[?996nok"]), "ok")

    def test_secondary_parameter_sequences_are_unchanged(self):
        # pyte reads '>' as transparent, so the SGR still lands on the following text.
        screen = HubTerminalScreen(COLS, ROWS)
        stream = HubTerminalStream(screen)
        stream.feed("\x1b[>4;2mAB")
        self.assertTrue(screen.buffer[0][0].underscore)
        self.assertTrue(screen.buffer[0][1].underscore)
        # ... and a secondary device attributes query draws nothing and does not move the text.
        self.assertEqual(render(["\x1b[>0cok"]), "ok")

    def test_can_and_sub_cancel_a_marker_csi(self):
        for cancel in ("\x18", "\x1a"):
            with self.subTest(cancel=cancel.encode("unicode_escape")):
                # The marker's parameters never reach the screen; the cancel path is pyte's own.
                self.assertEqual(render(["\x1b[<1" + cancel + "u"]), render([cancel + "u"]))
                self.assertEqual(render(["\x1b[<1", cancel + "u"]), render([cancel + "u"]))
                self.assertNotIn("1", render(["\x1b[<1" + cancel]))

    def test_a_dollar_sequence_is_left_to_pyte(self):
        self.assertEqual(render(["\x1b[1$px"]), "x")

    def test_the_8_bit_csi_start_is_filtered_too(self):
        self.assertEqual(render(["\x9b<uhello"]), "hello")
        for parts in every_split("\x9b<uhello"):
            with self.subTest(parts=parts):
                self.assertEqual(render(parts), "hello")

    def test_text_before_a_partial_csi_is_drawn_at_once(self):
        screen = HubTerminalScreen(COLS, ROWS)
        stream = HubTerminalStream(screen)
        stream.feed("ab\x1b[3")
        self.assertEqual(screen.display[0].rstrip(), "ab")
        stream.feed(";1u")
        self.assertEqual(screen.display[0].rstrip(), "ab")

    def test_a_partial_csi_beyond_the_carry_limit_is_handed_to_pyte(self):
        screen = HubTerminalScreen(COLS, ROWS)
        stream = HubTerminalStream(screen)
        stream.feed("\x1b[1" + "2" * 600)
        stream.feed("m")
        stream.feed("\r\nok")
        self.assertEqual(screen.display[0].rstrip(), "")
        self.assertEqual(screen.display[1].rstrip(), "ok")

    def test_the_split_helper_reports_the_kept_tail(self):
        self.assertEqual(_split_marker_csi("abc"), ("abc", ""))
        self.assertEqual(_split_marker_csi("a\x1b[<"), ("a", "\x1b[<"))
        self.assertEqual(_split_marker_csi("\x1b[<uX"), ("X", ""))
        self.assertEqual(_split_marker_csi("\x1b[2;3"), ("", "\x1b[2;3"))
        self.assertEqual(_split_marker_csi("ab\x1b"), ("ab", "\x1b"))


if __name__ == "__main__":
    unittest.main()
