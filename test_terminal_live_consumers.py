"""The two screen readers master added after the design, run on recorded bytes through both models.

`_codex_composer_shows_whole` (the Codex typed-text settle loop, master `0e17c54`) and the omp
approval-box parse (`tool_call_for_approval_box`, `screen_command_clipped`, `_omp_box_shows_call`,
master `c527749`/`6276c63`) arrived with their tests passing a screen in as a string, so no test fed
a recorded stream through a screen model.  The recorded decision `live-capture-new-consumers` closes
that: two live cases were recorded with `tools/record_terminal_corpus.py`, each stopping at the
screen its consumer reads (`meta.json`'s `consumer.reads_at_chunk`), and this file replays those
chunks through the core (`attach_text` + `feed`, as a DSH virtual session does) and through the pyte
oracle (the hub's own reader path, `renderer.new_session` + `_on_pty_output`) and runs the real
consumers on both screens.

What each case pins:

- ``live-codex-scrolled-paste``: Codex at 80x24 with a ~945-character follow-up on the composer; the
  composer scrolled, so its top visible row carries the scroll mark `↑`.  The settle loop must accept
  the settled composer as the whole text, which it can only do from the scrolled branch.
- ``live-omp-approval-box``: omp's approval box for a command wider than the box, which omp cuts with
  `…`.  The box parse must yield the same command and the same `command_truncated` flag from the
  core's screen as from pyte's; the recorder denied the box (Escape) and never approved it.

Needs the built extension beside this file (``hub_terminal_core.pyd``); it skips, with the build
command, on an unbuilt tree.
"""
import unittest
from pathlib import Path

from session_manager import HubSessionManager, parse_omp_approval_prompt
from tools import render_terminal_fixtures as renderer

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "tools" / "fixtures" / "terminal"
# The hub's screen read in `_await_typed_text_settled` is 40 rows; the goldens and this parity check
# use the same 80, so a row the consumer walks up from cannot hide above either one.
SCREEN_LINES = renderer.SCREEN_LINES

try:
    from terminal_core import Core
except ImportError as _import_error:  # an unbuilt tree: skip with the build command, never pass
    Core = None
    IMPORT_ERROR = _import_error
else:
    IMPORT_ERROR = None

TEXT_CASE = "live-codex-scrolled-paste"
BOX_CASE = "live-omp-approval-box"


class StopPointReplay:
    """One case's chunk stream, replayed to the stop point its consumer read, on both screens."""

    def __init__(self, core, name):
        self.name = name
        self.case = renderer.Case(FIXTURES / name)
        consumer = self.case.meta.get("consumer") or {}
        if "reads_at_chunk" not in consumer:
            raise AssertionError(f"{name}: meta.json carries no consumer.reads_at_chunk stop point")
        self.consumer = consumer
        self.stop = int(consumer["reads_at_chunk"])
        self.chunks = self.case.chunks[: self.stop]

        # pyte: the hub's own reader path, the one the goldens are rendered with.
        self.session = renderer.new_session(self.case.engine, self.case.cols, self.case.rows)
        for chunk in self.chunks:
            self.session._on_pty_output(chunk)

        # The core: a text session without a process, fed the same chunk texts.
        self.core_id = f"live-consumer-{name}"
        core.attach_text(self.core_id, cols=self.case.cols, rows=self.case.rows)
        for chunk in self.chunks:
            if chunk:
                core.feed(self.core_id, chunk)
        self.core = core

    def pyte_screen(self) -> str:
        return self.session.read_screen(SCREEN_LINES).text

    def core_screen(self) -> str:
        return self.core.screen(self.core_id, SCREEN_LINES)["text"]

    def expected_text(self) -> str:
        text = self.consumer.get("expected_text")
        if not text:
            raise AssertionError(f"{self.name}: a follow-up stop point records consumer.expected_text")
        return str(text)


class LiveConsumerTests(unittest.TestCase):
    def setUp(self):
        if Core is None:
            # terminal_core's own message carries the expected path and both build commands.
            self.skipTest(f"the core's screen cannot be compared: {IMPORT_ERROR}")
        self.core = Core(notify=lambda: None, log=lambda line: None)

    def tearDown(self):
        core = getattr(self, "core", None)
        if core is None:
            return
        try:
            core.kill_all(2000)
        finally:
            core.shutdown()

    def replay(self, name) -> StopPointReplay:
        return StopPointReplay(self.core, name)

    def test_the_stop_point_reproduces_the_recorded_screen_on_both_models(self):
        # The consumer results below are only about the consumer if both models drew the same screen:
        # the recorder's stop point is a chunk index, and both sides replay exactly that prefix.
        for name in (TEXT_CASE, BOX_CASE):
            replay = self.replay(name)
            self.assertIs(bool(replay.consumer.get("settled")), True,
                          f"{name}: the screen the consumer read never stood still")
            self.assertFalse(replay.case.meta.get("resize"),
                             f"{name}: a stop point replaces the mid-recording resize")
            self.assertEqual(replay.core_screen(), replay.pyte_screen(),
                             f"{name}: the core's screen differs from pyte's at the stop point")

    def test_codex_settle_loop_accepts_the_scrolled_composer_on_both_screens(self):
        replay = self.replay(TEXT_CASE)
        expected = replay.expected_text()
        accepted = {}
        for model, screen in (("pyte", replay.pyte_screen()), ("core", replay.core_screen())):
            line = HubSessionManager._tui_input_line(screen, "codex")
            self.assertIsNotNone(line, f"{replay.name}: no composer on the {model} screen")
            # The case exists for the scrolled branch: the composer's top visible row carries the
            # mark, so the exact full-text match cannot be what accepts it.
            self.assertIn("↑", line, f"{replay.name}: the {model} composer is not scrolled")
            accepted[model] = HubSessionManager._codex_composer_shows_whole(line, expected)
        self.assertEqual(accepted["core"], accepted["pyte"],
                         f"{replay.name}: the settle loop disagrees between the two screens")
        self.assertTrue(accepted["core"],
                        f"{replay.name}: the settle loop does not accept the settled composer")

    def test_omp_box_parse_yields_the_same_command_and_flag_on_both_screens(self):
        replay = self.replay(BOX_CASE)
        read = {}
        for model, screen in (("pyte", replay.pyte_screen()), ("core", replay.core_screen())):
            box = parse_omp_approval_prompt(screen)
            self.assertIsNotNone(box, f"{replay.name}: no approval box on the {model} screen")
            fields = HubSessionManager._omp_approval_fields(str(box.get("tool") or ""), box, None)
            # command / command_truncated are what the parent is shown and what the box is matched
            # against the transcript by (`tool_call_for_approval_box`), so they are what must agree.
            read[model] = (box.get("tool"), fields["metadata"]["command"],
                           fields["metadata"]["command_truncated"])
        self.assertEqual(read["core"], read["pyte"],
                         f"{replay.name}: the box parses differently on the two screens")
        tool, command, truncated = read["core"]
        self.assertEqual(tool, "bash")
        self.assertTrue(command)
        self.assertTrue(truncated, f"{replay.name}: the box did not cut the command")
        self.assertIn("…", command)
        # Wider than the box: its `Command:` row runs to the frame's last column and the box cut it
        # there, which is why the command comes from the transcript in the hub and is marked here.
        row = next((line for line in replay.core_screen().splitlines()
                    if line.lstrip().startswith("│ Command:")), None)
        self.assertIsNotNone(row, f"{replay.name}: no Command: row in the box")
        self.assertGreaterEqual(len(row), replay.case.cols - 1,
                                f"{replay.name}: the command row does not fill the box")


if __name__ == "__main__":
    unittest.main()
