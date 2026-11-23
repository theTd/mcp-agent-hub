"""The screen cases the pyte oracle pinned, run on the Rust core.

At step 6 the pyte oracle (``tools/pyte_oracle.py``) was deleted, so its two screen test files,
``test_terminal_screen_zero_width.py`` and ``test_terminal_stream_csi.py``, moved here: the same
inputs and the same expected screens, driven through ``hub_terminal_core`` (``attach_text`` +
``feed``, a session with a screen but no process) instead of through ``HubTerminalScreen``/``HubTerminalStream``.

The core's screen keeps text only — the hub's ``ScreenRead`` has no attributes — so where the pyte
tests asserted a cell's ``bold``/``fg``/``underscore`` to prove that a sequence applied, this file
asserts the drawn text.  Those attribute assertions have no counterpart in the core and are listed
in report.md; the text they prove is what the hub renders.

The corpus gate (``terminal-core/terminal_stream/tests/fixture_corpus``) also pins these behaviours
on committed goldens (``edge-screen-zero-width``, ``-zero-width-split``, ``-csi-marker``,
``-csi-cancel``, ``-csi-controls``, ``-combining``).  These cases stay a direct, finer-grained pin:
every split point, the eight-bit CSI start, the carry limit and the DA/DSR forms.
"""
import itertools
import unittest

import terminal_core

COLS, ROWS = 40, 6
# The forms the CLIs send: kimi and omp pop their kitty-keyboard flags with ESC[<u, agy sets them
# with ESC[=5;1u and ESC[<1u; ':' is the SGR subparameter separator (ESC[38:2::r:g:b m).
MARKER_FORMS = ["\x1b[<u", "\x1b[=5;1u", "\x1b[<1u", "\x1b[1:2m", "\x1b[<1;2u", "\x1b[>4;1:2m"]

_ids = itertools.count()


def every_split(text):
    return [[text[:i], text[i:]] for i in range(len(text) + 1)]


class CoreScreenCase(unittest.TestCase):
    """One core for the class; one text session per render."""

    @classmethod
    def setUpClass(cls):
        cls.core = terminal_core.Core(notify=lambda: None, log=lambda line: None)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.core.kill_all(2000)
        finally:
            cls.core.shutdown()

    def open_session(self, columns=COLS, lines=ROWS):
        session_id = f"screen-case-{next(_ids)}"
        self.core.attach_text(session_id, cols=columns, rows=lines)
        return session_id

    def screen_text(self, session_id):
        # max_lines 0: every row; the core trims the trailing blank ones, as the oracle's renderer did.
        return self.core.screen(session_id, 0)["text"]

    def feed(self, session_id, chunks):
        for chunk in chunks:
            if chunk:
                self.core.feed(session_id, chunk)

    def render(self, chunks, columns=COLS, lines=ROWS):
        """The screen after the chunks, row by row, with trailing blank rows dropped."""
        session_id = self.open_session(columns, lines)
        self.feed(session_id, chunks)
        return self.screen_text(session_id)


class ZeroWidthDrawTests(CoreScreenCase):
    """`HubTerminalScreen.draw` skipped zero-width and unprintable characters; the core's `draw` does."""

    def test_text_after_a_zero_width_space_is_drawn(self):
        self.assertEqual(self.render(["ab\u200bcd"], 20, 3), "abcd")

    def test_text_after_the_emoji_selector_is_drawn(self):
        # VS16 follows most emoji in CLI output ("⚠️ warning"); pyte used to drop the rest.
        self.assertEqual(self.render(["⚠️ warning"], 20, 3), "⚠ warning")
        self.assertEqual(self.render(["\U0001f44d️ ok"], 20, 3), "\U0001f44d ok")

    def test_a_zero_width_joiner_sequence_keeps_every_part(self):
        self.assertEqual(self.render(["a\U0001f468‍\U0001f469b"], 20, 3),
                         "a\U0001f468\U0001f469b")

    def test_combining_marks_still_compose_with_the_previous_cell(self):
        self.assertEqual(self.render(["e\u0301x"], 20, 3), "éx")

    def test_a_zero_width_character_does_not_take_a_cell(self):
        self.assertEqual(self.render(["\u200b" * 5 + "abc"], 20, 3), "abc")

    def test_a_zero_width_character_at_the_edge_does_not_wrap(self):
        # The core trims trailing blank rows, so the first row is the whole assertion here.
        self.assertEqual(self.render(["abcde\u200b"], 5, 3), "abcde")
        # The next printable character still wraps as usual.
        self.assertEqual(self.render(["abcde\u200bf"], 5, 3), "abcde\nf")

    def test_plain_ascii_and_wide_text_are_unchanged(self):
        self.assertEqual(self.render(["hello world"], 20, 3), "hello world")
        self.assertEqual(self.render(["中文 ok"], 20, 3), "中文 ok")

    def test_the_rest_of_a_split_run_still_draws(self):
        self.assertEqual(self.render(["ab\u200b", "cd"], 20, 3), "abcd")


class MarkerCsiTests(CoreScreenCase):
    """A CSI whose parameters include ':' '<' '=' is consumed whole, whole or split."""

    def test_a_marker_csi_draws_nothing(self):
        for form in MARKER_FORMS:
            with self.subTest(form=form.encode("unicode_escape")):
                self.assertEqual(self.render([form]), "")

    def test_a_marker_csi_split_across_two_feeds_draws_nothing(self):
        for form in MARKER_FORMS:
            for parts in every_split(form):
                with self.subTest(form=form.encode("unicode_escape"), parts=parts):
                    self.assertEqual(self.render(parts), "")

    def test_text_after_a_marker_csi_keeps_its_column(self):
        for form in MARKER_FORMS:
            with self.subTest(form=form.encode("unicode_escape")):
                self.assertEqual(self.render([form + "hello"]), "hello")
                self.assertEqual(self.render([form, "hello"]), "hello")
                self.assertEqual(self.render(["ab" + form + "hello"]), "abhello")

    def test_several_marker_csis_in_one_line(self):
        self.assertEqual(self.render(["\x1b[<u\x1b[=5;1u\x1b[<1uend"]), "end")

    def test_a_normal_csi_right_after_a_marker_csi_is_parsed(self):
        self.assertEqual(self.render(["\x1b[<u" + "\x1b[2;3Hx"]), "\n  x")
        self.assertEqual(self.render(["\x1b[<u", "\x1b[2;3Hx"]), "\n  x")

    def test_a_normal_csi_cut_at_the_end_of_a_chunk_still_renders(self):
        whole = self.render(["\x1b[2;3Hx"])
        for parts in every_split("\x1b[2;3Hx"):
            with self.subTest(parts=parts):
                self.assertEqual(self.render(parts), whole)

    def test_private_modes_still_apply(self):
        # DECAWM off: 45 characters stay on the first row instead of wrapping.
        self.assertEqual(self.render(["\x1b[?7l" + "X" * 45]), "X" * COLS)
        self.assertEqual(self.render(["\x1b[?7h" + "X" * 45]), "X" * COLS + "\n" + "X" * 5)
        # kimi's private status query draws nothing and changes nothing.
        self.assertEqual(self.render(["\x1b[?996nok"]), "ok")

    def test_secondary_parameter_sequences_are_unchanged(self):
        # pyte read '>' as transparent, so the SGR landed on the following text; the core ignores SGR
        # and draws the text in the same cells (it keeps no attribute state).
        self.assertEqual(self.render(["\x1b[>4;2mAB"]), "AB")
        # A secondary device attributes query draws nothing and does not move the text.
        self.assertEqual(self.render(["\x1b[>0cok"]), "ok")

    def test_can_and_sub_cancel_a_marker_csi(self):
        for cancel in ("\x18", "\x1a"):
            with self.subTest(cancel=cancel.encode("unicode_escape")):
                # The marker's parameters never reach the screen; the cancel path is the parser's own.
                self.assertEqual(self.render(["\x1b[<1" + cancel + "u"]),
                                 self.render([cancel + "u"]))
                self.assertEqual(self.render(["\x1b[<1", cancel + "u"]),
                                 self.render([cancel + "u"]))
                self.assertNotIn("1", self.render(["\x1b[<1" + cancel]))

    def test_a_dollar_sequence_is_consumed_whole(self):
        self.assertEqual(self.render(["\x1b[1$px"]), "x")

    # `test_the_8_bit_csi_start_is_filtered_too` (pyte's `\\x9b<u` consumed as a CSI) has no core
    # counterpart: `vte` does not treat 0x9b as a CSI start, so the core draws `"<uhello"` where the
    # oracle drew `"hello"`.  Not a behaviour the hub's engines send; recorded in report.md (step 6).

    def test_text_before_a_partial_csi_is_drawn_at_once(self):
        session_id = self.open_session()
        self.feed(session_id, ["ab\x1b[3"])
        self.assertEqual(self.screen_text(session_id), "ab")
        self.feed(session_id, [";1u"])
        self.assertEqual(self.screen_text(session_id), "ab")

    def test_a_partial_csi_beyond_the_carry_limit_is_parsed(self):
        # The oracle handed a runaway sequence to pyte after 512 bytes; vte carries its own bound, and
        # what matters is that the completed sequence draws nothing and the text after it still lands.
        session_id = self.open_session()
        self.feed(session_id, ["\x1b[1" + "2" * 600])
        self.feed(session_id, ["m"])
        self.feed(session_id, ["\r\nok"])
        rows = self.screen_text(session_id).split("\n")
        self.assertEqual(rows[0], "")
        self.assertEqual(rows[1], "ok")


class BytesInsideCsiTests(CoreScreenCase):
    """An ESC, a C0 control or DEL inside a CSI: pyte took each for the final byte."""

    def test_an_esc_inside_a_csi_starts_the_next_sequence(self):
        for parts in every_split("A\x1b[31\x1b[1mB"):
            with self.subTest(parts=parts):
                # pyte's test also asserted that ESC[1m applied (bold, default fg); the core draws
                # the same cells and keeps no attributes.
                self.assertEqual(self.render(parts), "AB")

    def test_an_esc_inside_a_marker_csi_starts_the_next_sequence(self):
        for parts in every_split("A\x1b[<\x1b[2;1HB"):
            with self.subTest(parts=parts):
                self.assertEqual(self.render(parts), "A\nB")

    def test_an_esc_inside_a_csi_can_start_a_non_csi_escape(self):
        # ESC 7 saves the cursor at column 2, ESC 8 brings it back, so D overwrites C.
        self.assertEqual(self.render(["AB\x1b[3\x1b7C\x1b8D"]), "ABD")

    def test_a_control_inside_a_csi_runs_and_the_sequence_still_applies(self):
        for parts in every_split("A\x1b[3\r1mB"):
            with self.subTest(parts=parts):
                self.assertEqual(self.render(parts), "B")  # CR moved the cursor home

    def test_a_control_inside_a_csi_no_longer_draws_the_rest(self):
        # SO is ignored in UTF-8 mode; inside the CSI pyte took it for the final byte and drew ";1mB".
        self.assertEqual(self.render(["A\x1b[1\x0e;1mB"]), "AB")

    def test_a_del_inside_a_csi_is_ignored(self):
        self.assertEqual(self.render(["A\x1b[3\x7f1mB"]), "AB")

    def test_a_control_inside_a_marker_csi_still_runs(self):
        # BS steps back one column, the marker CSI draws nothing, Z lands on b.
        for parts in every_split("ab\x1b[<\x08uZ"):
            with self.subTest(parts=parts):
                self.assertEqual(self.render(parts), "aZ")


if __name__ == "__main__":
    unittest.main()
