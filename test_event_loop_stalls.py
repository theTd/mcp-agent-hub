"""Guards against the event-loop stalls measured in the live hub on 2026-11-12.

Run: python -m unittest -v test_event_loop_stalls
"""
import asyncio
import importlib
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hub_database import HubDatabase

HUB_DIR = Path(__file__).resolve().parent


class PortableDependencyTests(unittest.TestCase):
    def test_the_portable_lock_ships_sniffio(self):
        # Without it httpcore's import attempt searches sys.path on disk at every lock and close.
        lock = (HUB_DIR / "desktop" / "portable" / "requirements.lock").read_text(encoding="utf-8")
        self.assertRegex(lock, r"(?m)^sniffio==")


class WorkerEventsIndexTests(unittest.TestCase):
    def test_session_reads_use_the_index(self):
        with tempfile.TemporaryDirectory(prefix="hub-index-", ignore_cleanup_errors=True) as root:
            path = Path(root) / "hub_state.db"
            HubDatabase(path)
            with sqlite3.connect(path) as conn:
                queries = (
                    "SELECT seq, content FROM worker_events WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                    "SELECT DISTINCT request_id FROM worker_events WHERE session_id=? "
                    "AND type IN ('approval_request', 'question') AND request_id IS NOT NULL AND request_id != ''",
                    "SELECT MAX(timestamp) FROM worker_events WHERE session_id=? AND type='turn_completed'",
                )
                for query in queries:
                    params = ("s", 80) if query.count("?") == 2 else ("s",)
                    plan = " ".join(row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + query, params))
                    with self.subTest(query=query):
                        self.assertIn("idx_worker_events_session", plan)
            conn.close()


class FastmcpLoggingTests(unittest.TestCase):
    def test_tool_errors_are_logged_without_rich_rendering(self):
        # A rich traceback (syntax-highlighted source) held the loop for 1.75 s per failed tool call.
        with patch("session_manager.HubSessionManager"):
            importlib.import_module("server")
        handlers = logging.getLogger("fastmcp").handlers
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertNotIn("rich", type(handler).__module__)
        try:
            raise RuntimeError("boom")
        except RuntimeError as error:
            record = logging.LogRecord("fastmcp", logging.ERROR, __file__, 1, "Error calling tool 'x'", None,
                                       (type(error), error, error.__traceback__))
        text = handlers[0].format(record)
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("RuntimeError: boom", text)


class TerminalDrainTickTests(unittest.IsolatedAsyncioTestCase):
    async def _shut_down(self, hub):
        await hub.archive_all_sessions_on_shutdown()
        hub.core.shutdown()
        self.temporary.cleanup()

    async def test_twenty_ticks_of_fifty_sessions_keep_the_loop_responsive(self):
        # Design §5.3: one wakeup per drain interval however much output arrived, so one tick with
        # 50 sessions' records must stay far below that interval and let the loop run between ticks.
        import time

        import session_manager
        from fake_terminal_core import FakeTerminal
        from session_manager import AgentSession, HubSessionManager

        self.temporary = tempfile.TemporaryDirectory(prefix="hub-tick-")
        root = Path(self.temporary.name)
        profiles = root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            hub = HubSessionManager(profiles, base_dir=HUB_DIR, data_dir=root)
        hub.core = FakeTerminal()
        await hub.start()
        self.addAsyncCleanup(self._shut_down, hub)

        sessions = []
        for index in range(50):
            session_id = f"tick-{index}"
            sess = AgentSession(session_id, "pwsh", "Pwsh", "tui", "tick", str(root), "cmd.exe", [],
                                core=hub.core)
            sess.engine = "pwsh"
            sess.status = "running"
            hub.sessions[session_id] = sess
            hub.core.spawn(session_id, ["cmd.exe"])
            sessions.append(sess)

        turns, done = [], asyncio.Event()

        async def ticker():
            while not done.is_set():
                turns.append(len(turns))
                await asyncio.sleep(0)

        task = asyncio.create_task(ticker())
        apply_seconds = []
        started = time.perf_counter()
        for round_index in range(20):
            for session_id in hub.sessions:
                hub.core.output(session_id, f"行{round_index}\r\n")
            tick_started = time.perf_counter()
            hub._on_core_tick()
            apply_seconds.append(time.perf_counter() - tick_started)
            await asyncio.sleep(0)
        total = time.perf_counter() - started
        done.set()
        await task

        self.assertEqual(sessions[0].read_raw().text.count("\r\n"), 20)
        self.assertEqual(sessions[-1].raw_revision, hub.core.session("tick-49").rev)
        self.assertTrue(turns, "the tick never let another task run")
        self.assertLess(max(apply_seconds), 0.25,
                        f"one tick of 50 records took {max(apply_seconds):.3f}s")
        self.assertLess(total, 2.0, f"20 ticks took {total:.3f}s")


if __name__ == "__main__":
    unittest.main()
