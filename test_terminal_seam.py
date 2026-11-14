"""Step 3a's guard: terminal state is read and written only through `AgentSession`'s seam.

The seam's point is that step 3b replaces method bodies instead of chasing callers. A direct
`sess.screen`/`sess.raw_history`/`sess.pty` somewhere in the manager would compile, pass its own
test, and then quietly keep the pyte-era behaviour after the core lands. This scan fails on such
an access outside `AgentSession`, and on the retired read names.
"""
import ast
from pathlib import Path
import unittest

import server
import session_manager

FIELDS = ("screen", "stream", "raw_history", "raw_history_bytes", "raw_history_rev", "pty",
          "ws_queues", "ws_handles")
OLD_READERS = ("get_clean_screen_text", "get_full_scrollback_text")
OWNER = "AgentSession"
HUB_FILES = ("session_manager.py", "server.py")


def offenders(source: str, filename: str = "<source>") -> list:
    """Every terminal-state access outside `AgentSession`, as (kind, name, where, line)."""
    hits = []

    class Scan(ast.NodeVisitor):
        def __init__(self):
            self.classes = []
            self.functions = []

        def visit_ClassDef(self, node):
            self.classes.append(node.name)
            self.generic_visit(node)
            self.classes.pop()

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node):
            where = ".".join(self.classes + self.functions) or "<module>"
            if node.attr in OLD_READERS:
                hits.append(("retired reader", node.attr, where, node.lineno))
            elif node.attr in FIELDS and OWNER not in self.classes:
                hits.append(("direct access", node.attr, where, node.lineno))
            self.generic_visit(node)

    Scan().visit(ast.parse(source, filename))
    return hits


class TerminalSeamScanTests(unittest.TestCase):
    def test_the_hub_touches_terminal_state_only_inside_agent_session(self):
        failures = []
        for name in HUB_FILES:
            path = Path(__file__).parent / name
            for kind, attr, where, line in offenders(path.read_text(encoding="utf-8"), name):
                failures.append(f"{name}:{line} {where} {kind} of {attr}")
        self.assertEqual(failures, [],
                         "terminal state reached outside the seam (design §5.2):\n" + "\n".join(failures))

    def test_the_scan_flags_a_new_direct_access(self):
        source = (
            "class AgentSession:\n"
            "    def read_screen(self):\n"
            "        return self.core.screen(self.session_id)\n"
            "class HubSessionManager:\n"
            "    def peek(self, session):\n"
            "        return session.raw_history\n"
        )
        self.assertEqual(offenders(source),
                         [("direct access", "raw_history", "HubSessionManager.peek", 6)])

    def test_the_scan_flags_the_retired_reader_names(self):
        source = "def peek(session):\n    return session.get_clean_screen_text(80)\n"
        self.assertEqual(offenders(source),
                         [("retired reader", "get_clean_screen_text", "peek", 2)])

    def test_the_producers_are_gone_and_the_seam_is_what_calls_the_core(self):
        # 3b deleted the reader thread, the chunk handler and the pyte state behind the seam: the
        # core's reader owns the screen and the raw ring, and Python only applies drain records.
        for name in ("_on_pty_output", "_read_loop", "_strip_saved_lines_erase",
                     "adopt_ws_frontends", "push_ws_frame", "_push_ws_safe"):
            self.assertFalse(hasattr(session_manager.AgentSession, name), name)
        for name in ("read_screen", "read_scrollback", "read_raw", "read_raw_tail", "seed_history",
                     "write_input", "resize", "kill", "is_process_alive", "last_output_time",
                     "apply_core_record", "drain_ws_frames", "start_pty", "start_virtual"):
            self.assertTrue(callable(getattr(session_manager.AgentSession, name, None)), name)
        for name in ("has_process", "pid", "raw_revision"):
            self.assertIsInstance(getattr(session_manager.AgentSession, name, None), property, name)


if __name__ == "__main__":
    unittest.main()