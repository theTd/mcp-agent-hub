"""A deliberate restart resumes the sessions it interrupted before callers can see them."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import restart_resume_request as rrr
from restart_resume_request import CONTINUE_TEXT, REQUEST_NAME, RESULT_NAME, TAKEN_NAME, RestartResumeGate, RestartResumeRequest
from session_manager import AgentSession, HubSessionManager


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resume-req-")
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_request(self):
        self.assertIsNone(RestartResumeRequest.take(self.dir))

    def test_request_is_used_once(self):
        (self.dir / REQUEST_NAME).write_text(json.dumps({"session_ids": ["a", "b"]}), encoding="utf-8")
        request = RestartResumeRequest.take(self.dir)
        self.assertTrue(request.wants("a"))
        self.assertFalse(request.wants("c"))
        self.assertFalse((self.dir / REQUEST_NAME).exists())
        self.assertTrue((self.dir / TAKEN_NAME).exists())
        self.assertIsNone(RestartResumeRequest.take(self.dir))

    def test_unreadable_request_is_dropped(self):
        (self.dir / REQUEST_NAME).write_text("{not json", encoding="utf-8")
        with self.assertLogs("mcp-agent-hub.restart_resume", level="WARNING"):
            self.assertIsNone(RestartResumeRequest.take(self.dir))
        self.assertFalse((self.dir / REQUEST_NAME).exists())

    def test_only_cut_off_terminal_turns_get_the_message(self):
        (self.dir / REQUEST_NAME).write_text("{}", encoding="utf-8")
        request = RestartResumeRequest.take(self.dir)
        self.assertTrue(request.wants("anything"))
        self.assertEqual(request.text_for("a", "codex", "running"), CONTINUE_TEXT)
        self.assertEqual(request.text_for("a", "claude", "waiting_parent"), CONTINUE_TEXT)
        self.assertIsNone(request.text_for("a", "codex", "completed"))
        self.assertIsNone(request.text_for("a", "dsh", "running"))
        (self.dir / REQUEST_NAME).write_text(json.dumps({"continue_unfinished_turns": False}), encoding="utf-8")
        self.assertIsNone(RestartResumeRequest.take(self.dir).text_for("a", "codex", "running"))
        (self.dir / REQUEST_NAME).write_text(json.dumps({"continue_session_ids": ["a", "d"]}), encoding="utf-8")
        request = RestartResumeRequest.take(self.dir)
        self.assertEqual(request.text_for("a", "codex", "interrupted"), CONTINUE_TEXT)
        self.assertIsNone(request.text_for("b", "codex", "running"))
        self.assertIsNone(request.text_for("d", "dsh", "interrupted"))


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_wait_until_the_gate_opens(self):
        gate = RestartResumeGate()
        self.assertTrue(gate.is_open)
        gate.close()
        waiter = asyncio.ensure_future(gate.wait())
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())
        gate.open()
        await asyncio.wait_for(waiter, 1)

    async def test_wait_is_bounded(self):
        gate = RestartResumeGate()
        gate.close()
        with patch.object(rrr, "GATE_MAX_WAIT_SEC", 0.1):
            started = time.monotonic()
            await gate.wait()
        self.assertLess(time.monotonic() - started, 1.0)


class HubStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resume-start-")
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles.json"
        self.profiles.write_text('{"profiles":{}}', encoding="utf-8")
        self.hubs = []

    async def asyncTearDown(self):
        for hub in self.hubs:
            await hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    def make_hub(self):
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            hub = HubSessionManager(self.profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.hubs.append(hub)
        return hub

    async def stopped_hard(self, sessions):
        """A hub whose live rows stay live: the process was killed, nothing was archived."""
        hub = self.make_hub()
        await hub.start()
        for sid, engine, status in sessions:
            sess = AgentSession(sid, engine, engine, "tui", sid, str(self.root), "", [])
            sess.engine = engine
            sess.status = status
            await hub.persist_session(sess)
        await hub.db.flush()
        self.hubs.remove(hub)
        for task in list(hub._background_tasks):
            task.cancel()
        await asyncio.gather(*hub._background_tasks, return_exceptions=True)
        await hub.db.close()

    async def test_start_resumes_live_sessions_behind_the_gate(self):
        await self.stopped_hard([
            ("codex-run", "codex", "running"),
            ("claude-idle", "claude", "completed"),
            ("dsh-run", "dsh", "running"),
            ("codex-old", "codex", "exited"),
        ])
        (self.root / REQUEST_NAME).write_text("{}", encoding="utf-8")
        hub = self.make_hub()
        calls = []
        port_open = asyncio.Event()

        async def resume(sid, text=None, auto_approval_rules=None, source="parent"):
            calls.append((sid, text, source, hub.restart_resume_gate.is_open))
            if sid == "claude-idle":
                raise ValueError("cannot")
            return {"resumed": True}

        async def wait_port(port):
            await port_open.wait()
            return True

        with patch.object(hub, "resume_subagent", side_effect=resume), \
                patch("session_manager.wait_for_port", side_effect=wait_port):
            await hub.start()
            self.assertFalse(hub.restart_resume_gate.is_open)
            await asyncio.sleep(0.05)
            self.assertEqual(calls, [], "nothing is resumed before the port is open")
            port_open.set()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        self.assertEqual([c[0] for c in calls], ["dsh-run", "claude-idle", "codex-run"])
        self.assertEqual(dict((c[0], c[1]) for c in calls),
                         {"dsh-run": None, "claude-idle": None, "codex-run": CONTINUE_TEXT})
        self.assertTrue(all(c[2] == "console" and not c[3] for c in calls))
        await asyncio.sleep(0.05)
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))["sessions"]
        self.assertEqual(set(result), {"dsh-run", "claude-idle", "codex-run"})
        self.assertFalse(result["claude-idle"]["ok"])
        self.assertTrue(result["codex-run"]["continued"])
        self.assertEqual(result["codex-run"]["status_at_stop"], "running")

    async def test_listed_sessions_an_earlier_start_archived_are_resumed(self):
        await self.stopped_hard([
            ("codex-run", "codex", "running"),
            ("kimi-idle", "kimi", "completed"),
            ("codex-done", "codex", "exited"),
        ])
        early = self.make_hub()  # an earlier start without the request archived them already
        await early.start()
        await early.db.flush()
        self.hubs.remove(early)
        for task in list(early._background_tasks):
            task.cancel()
        await asyncio.gather(*early._background_tasks, return_exceptions=True)
        await early.db.close()
        (self.root / REQUEST_NAME).write_text(json.dumps({
            "session_ids": ["codex-run", "kimi-idle", "codex-done", "missing"],
            "continue_session_ids": ["codex-run"]}), encoding="utf-8")
        hub = self.make_hub()
        calls = []

        async def resume(sid, text=None, auto_approval_rules=None, source="parent"):
            calls.append((sid, text))
            return {"resumed": True}

        with patch.object(hub, "resume_subagent", side_effect=resume),                 patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        self.assertEqual(sorted(calls), [("codex-run", CONTINUE_TEXT), ("kimi-idle", None)])

    async def test_start_without_request_leaves_the_gate_open(self):
        await self.stopped_hard([("codex-run", "codex", "running")])
        hub = self.make_hub()
        with patch.object(hub, "resume_subagent", new_callable=AsyncMock) as resume:
            await hub.start()
            await asyncio.sleep(0.05)
        self.assertTrue(hub.restart_resume_gate.is_open)
        resume.assert_not_called()
        rec = await hub.db.get_session_record("codex-run")
        self.assertEqual(rec["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
