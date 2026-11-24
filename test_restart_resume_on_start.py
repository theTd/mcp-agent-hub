"""A deliberate restart resumes the sessions it interrupted before callers can see them."""
import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import restart_resume_request as rrr
from restart_resume_request import (CONTINUE_TEXT, REQUEST_NAME, RESULT_NAME, TAKEN_NAME,
                                    RestartResumeGate, RestartResumeRequest)
from session_manager import AgentSession, HubSessionManager

KEPT_AT_STOP = Path("briefs") / "kept-at-stop.json"


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resume-req-")
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_request(self):
        self.assertIsNone(RestartResumeRequest.take(self.dir))

    def test_peek_reads_without_consuming(self):
        (self.dir / REQUEST_NAME).write_text(json.dumps({
            "restart_id": "r1", "requested_at": 1690000000.0, "resume_crashed": True,
            "sessions": {"codex-a": {"engine": "codex", "status": "running"}}}), encoding="utf-8")
        seen = RestartResumeRequest.peek(self.dir)
        self.assertEqual(seen.restart_id, "r1")
        self.assertEqual(seen.requested_at, 1690000000.0)
        self.assertTrue(seen.resume_crashed)
        self.assertEqual(seen.sessions, {"codex-a": {"engine": "codex", "status": "running"}})
        # The file is still there for the start that will take it.
        self.assertTrue((self.dir / REQUEST_NAME).is_file())
        taken = RestartResumeRequest.take(self.dir)
        self.assertEqual(taken.restart_id, "r1")
        self.assertIsNone(RestartResumeRequest.peek(self.dir), "a taken request is not peeked again")

    def test_peek_ignores_an_unreadable_file_as_planned(self):
        (self.dir / REQUEST_NAME).write_text("{not json", encoding="utf-8")
        with self.assertLogs("mcp-agent-hub.restart_resume", level="WARNING"):
            self.assertIsNone(RestartResumeRequest.peek(self.dir))
        self.assertTrue((self.dir / REQUEST_NAME).is_file())

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
        self.assertEqual(request.text_for("a", "doubao", "running"), CONTINUE_TEXT)
        self.assertIsNone(request.text_for("a", "codex", "completed"))
        (self.dir / REQUEST_NAME).write_text(json.dumps({"continue_unfinished_turns": False}), encoding="utf-8")
        self.assertIsNone(RestartResumeRequest.take(self.dir).text_for("a", "codex", "running"))
        (self.dir / REQUEST_NAME).write_text(json.dumps({"continue_session_ids": ["a", "d"]}), encoding="utf-8")
        request = RestartResumeRequest.take(self.dir)
        self.assertEqual(request.text_for("a", "codex", "interrupted"), CONTINUE_TEXT)
        self.assertIsNone(request.text_for("b", "codex", "running"))


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

    async def test_wait_bounded_says_whether_the_gate_opened(self):
        gate = RestartResumeGate()
        self.assertTrue(await gate.wait_bounded(0.1), "an open gate needs no wait")
        gate.close()
        started = time.monotonic()
        self.assertFalse(await gate.wait_bounded(0.2))
        self.assertLess(time.monotonic() - started, 1.0)
        gate.open()
        self.assertTrue(await gate.wait_bounded(0.2))

    async def test_wait_bounded_opens_within_its_own_budget(self):
        gate = RestartResumeGate()
        gate.close()
        waiter = asyncio.ensure_future(gate.wait_bounded(5.0))
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())
        gate.open()
        self.assertTrue(await asyncio.wait_for(waiter, 1))


class _HubCase(unittest.IsolatedAsyncioTestCase):
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
        await self.stop_windows(hub)

    async def stop_windows(self, hub):
        """Drop a hub whose database another hub is about to open (a process that died)."""
        if hub in self.hubs:
            self.hubs.remove(hub)
        for task in list(hub._background_tasks):
            task.cancel()
        await asyncio.gather(*hub._background_tasks, return_exceptions=True)
        await hub.db.close()


class HubStartTests(_HubCase):

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
                patch("session_manager.wait_for_port", side_effect=wait_port), \
                patch("session_manager.purge_retired_engine_data", new=lambda *a, **k: {
                    "database": {"sessions": 0, "rows": 0, "backup": None},
                    "profiles": {"removed": 0, "backup": None},
                }):
            # The purge is a no-op here on purpose: this test keeps a DSH row across the start to
            # pin the guard that covers a start whose purge was skipped (a failed backup).
            with self.assertLogs("mcp-agent-hub.session_manager", level="INFO") as logs:
                await hub.start()
                self.assertFalse(hub.restart_resume_gate.is_open)
                await asyncio.sleep(0.05)
                self.assertEqual(calls, [], "nothing is resumed before the port is open")
                port_open.set()
                await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        self.assertTrue(any("skipped 1 DSH session(s)" in line for line in logs.output), logs.output)
        self.assertEqual([c[0] for c in calls], ["claude-idle", "codex-run"])
        self.assertEqual(dict((c[0], c[1]) for c in calls),
                         {"claude-idle": None, "codex-run": CONTINUE_TEXT})
        self.assertTrue(all(c[2] == "console" and not c[3] for c in calls))
        await asyncio.sleep(0.05)
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))["sessions"]
        self.assertEqual(set(result), {"claude-idle", "codex-run"})
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


class PlannedRestartTests(_HubCase):
    """A graceful stop is a restart: the sessions it cut off come back with their own status."""

    async def live_session(self, hub, sid, engine, status, alive=True):
        sess = AgentSession(sid, engine, engine, "tui", sid, str(self.root), "", [])
        sess.engine = engine
        sess.status = status
        sess._alive = alive
        hub.sessions[sid] = sess
        await hub.persist_session(sess)
        return sess

    @staticmethod
    def resume_with(hub, calls, deaths=()):
        async def resume(sid, text=None, auto_approval_rules=None, source="parent"):
            if sid in deaths:
                raise asyncio.CancelledError()
            calls[sid] = text
            return {"resumed": True}
        return patch.object(hub, "resume_subagent", side_effect=resume)

    def manifest(self) -> dict:
        return json.loads((self.root / KEPT_AT_STOP).read_text(encoding="utf-8"))

    def stored_row(self, session_id: str) -> dict:
        """The archived row as the next start will read it (the stopping hub's store is closed)."""
        conn = sqlite3.connect(str(self.root / "hub_state.db"))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else {}

    async def test_a_graceful_stop_records_the_real_status_and_resumes_every_session(self):
        hub = self.make_hub()
        await hub.start()
        await self.live_session(hub, "codex-run", "codex", "running")
        await self.live_session(hub, "claude-idle", "claude", "completed")
        await self.live_session(hub, "kimi-start", "kimi", "starting", alive=False)
        await self.live_session(hub, "codex-old", "codex", "exited", alive=False)
        self.assertEqual(await hub.archive_all_sessions_on_shutdown(planned=True), 4)
        self.assertTrue(hub._planned_restart)
        self.assertEqual(self.manifest()["session_ids"],
                         ["claude-idle", "codex-run", "kimi-start"])
        self.assertEqual(self.manifest()["sessions"]["claude-idle"],
                         {"engine": "claude", "status": "completed"})
        self.assertEqual(self.manifest()["sessions"]["codex-run"],
                         {"engine": "codex", "status": "running"})

        # No session_ids in the request, and no live rows left: the manifest alone must drive it.
        (self.root / REQUEST_NAME).write_text("{}", encoding="utf-8")
        hub = self.make_hub()
        calls = {}
        with self.resume_with(hub, calls), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        self.assertEqual(list(calls), ["claude-idle", "codex-run", "kimi-start"])
        self.assertIsNone(calls["claude-idle"], "an idle session comes back without a message")
        self.assertEqual(calls["codex-run"], CONTINUE_TEXT)
        self.assertEqual(calls["kimi-start"], CONTINUE_TEXT)
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))["sessions"]
        self.assertEqual(result["claude-idle"]["status_at_stop"], "completed")
        self.assertEqual(result["codex-run"]["status_at_stop"], "running")
        self.assertTrue(all(result[sid]["ok"] for sid in result))
        self.assertFalse((self.root / KEPT_AT_STOP).exists(),
                         "a finished resume drops the manifest")

    async def test_a_request_on_disk_makes_the_stop_planned_by_itself(self):
        hub = self.make_hub()
        await hub.start()
        await self.live_session(hub, "codex-run", "codex", "running")
        (self.root / REQUEST_NAME).write_text(json.dumps({"restart_id": "r7"}), encoding="utf-8")
        await hub.archive_all_sessions_on_shutdown()  # no planned argument: the file decides
        self.assertTrue(hub._planned_restart)
        self.assertEqual(self.manifest()["sessions"]["codex-run"]["status"], "running")
        rec = self.stored_row("codex-run")
        self.assertEqual((rec["status"], rec["end_reason"]), ("interrupted", "interrupted"))

        hub = self.make_hub()
        calls = {}
        with self.resume_with(hub, calls), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        self.assertEqual(calls, {"codex-run": CONTINUE_TEXT})
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(result["restart_id"], "r7")

    async def test_a_real_stop_still_settles_the_waiters(self):
        hub = self.make_hub()
        await hub.start()
        await self.live_session(hub, "codex-run", "codex", "running")
        handle = (await hub.wait_inbox.create(["codex-run"]))["wait_handle"]
        await hub.archive_all_sessions_on_shutdown(planned=False)
        self.assertFalse(hub._planned_restart)
        events = await hub.wait_inbox._take_events(handle, "hub_shutdown")
        self.assertEqual(len(events), 1, "a real stop writes its one durable signal")
        self.assertEqual(events[0]["data"]["planned"], False)
        self.assertTrue(events[0]["data"]["stop_id"])

    async def test_a_death_inside_the_stop_window_is_interrupted_not_crashed(self):
        hub = self.make_hub()
        await hub.start()
        sess = await self.live_session(hub, "codex-run", "codex", "running")
        # The stop has begun and already took its live snapshot; the child then dies on its own.
        hub._planned_restart = True
        hub._stop_live_at = {"codex-run": {"engine": "codex", "status": "running"}}
        sess.status = "crashed"
        sess.end_reason = "crashed"
        await hub._on_session_exit(sess, 1)
        types = [e.get("type") for e in hub.get_worker_events(session_id="codex-run")]
        self.assertNotIn("crashed", types, "the stop window must not publish a crash")
        self.assertNotIn("exited", types)
        rec = await hub.db.get_session_record("codex-run")
        self.assertFalse(rec.get("archived_at"), "the stop archives it, not the exit path")

        # The same death, now inside the real stop: the snapshot was taken before it.
        await self.live_session(hub, "codex-dead", "codex", "running")
        sess2 = hub.sessions["codex-dead"]

        def die_during_stop():
            sess2.status = "crashed"
            sess2.end_reason = "crashed"
            sess2._alive = False

        original = hub.db.stop_accepting

        def stop_accepting():
            # Runs right after the stop's snapshot and before the archive decides its reasons.
            die_during_stop()
            original()

        hub.db.stop_accepting = stop_accepting
        await hub.archive_all_sessions_on_shutdown(planned=True)
        self.assertEqual(self.manifest()["sessions"]["codex-dead"]["status"], "running",
                         "the status at the stop survives the death in the window")
        rec = self.stored_row("codex-dead")
        self.assertEqual((rec["status"], rec["end_reason"]), ("interrupted", "interrupted"))

        (self.root / REQUEST_NAME).write_text("{}", encoding="utf-8")
        hub = self.make_hub()
        calls = {}
        with self.resume_with(hub, calls), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        self.assertEqual(calls.get("codex-dead"), CONTINUE_TEXT)

    async def test_an_engine_without_a_revive_recipe_is_skipped_not_failed(self):
        hub = self.make_hub()
        await hub.start()
        await self.live_session(hub, "pwsh-shell", "pwsh", "running")
        await self.live_session(hub, "codex-run", "codex", "running")
        await hub.archive_all_sessions_on_shutdown(planned=True)
        (self.root / REQUEST_NAME).write_text("{}", encoding="utf-8")
        hub = self.make_hub()
        calls = {}
        with self.resume_with(hub, calls), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        self.assertEqual(calls, {"codex-run": CONTINUE_TEXT})
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(result["skipped"], {"pwsh-shell": "引擎「pwsh」不支持自动恢复"})
        self.assertEqual(result["sessions"]["pwsh-shell"]["ok"], True)
        self.assertEqual(result["sessions"]["pwsh-shell"]["skipped"], True)
        self.assertIn("pwsh", result["sessions"]["pwsh-shell"]["reason"])

    async def test_a_resume_that_fails_wakes_the_parent_instead_of_leaving_it_waiting(self):
        hub = self.make_hub()
        await hub.start()
        await self.live_session(hub, "codex-run", "codex", "running")
        handle = (await hub.wait_inbox.create(["codex-run"]))["wait_handle"]
        await hub.archive_all_sessions_on_shutdown(planned=True)
        (self.root / REQUEST_NAME).write_text("{}", encoding="utf-8")

        hub = self.make_hub()
        with patch.object(hub, "resume_subagent",
                          side_effect=ValueError("工作目录已不存在")), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))["sessions"]
        self.assertFalse(result["codex-run"]["ok"])
        self.assertIn("工作目录已不存在", result["codex-run"]["error"])

        woke = await hub.wait_inbox.wait(handle, 0.1)
        self.assertEqual(woke["state"], "failed")
        self.assertEqual(woke["errors"][0]["type"], "restart_resume_failed")
        self.assertEqual(woke["errors"][0]["session_id"], "codex-run")
        self.assertIn("工作目录已不存在", woke["errors"][0]["message"])
        self.assertEqual(hub.wait_inbox._row(handle)["state"], "failed")

    async def test_the_fence_closes_before_the_request_reaches_the_disk(self):
        """A death in the window between "restart accepted" and "stop began" is part of the stop."""
        hub = self.make_hub()
        await hub.start()
        sess = await self.live_session(hub, "codex-run", "codex", "running")
        handle = (await hub.wait_inbox.create(["codex-run"]))["wait_handle"]
        entered = threading.Event()
        release = threading.Event()
        original = hub._write_resume_request

        def slow_write(body):
            # The write is in flight (its thread blocked), so no `_shutdown` has run yet.
            entered.set()
            release.wait(5)
            original(body)

        hub._write_resume_request = slow_write
        task = asyncio.ensure_future(
            hub.prepare_graceful_restart(mode="hub", restart_id="r-fence"))
        deadline = time.monotonic() + 5
        while not entered.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertTrue(entered.is_set(), "the request write must be in flight")
        self.assertIn("codex-run", hub._stop_live_at,
                      "the snapshot is taken before any await, not by `_shutdown`")

        # The session dies right here: before the skip, before the archive, before the stop.
        # The exit callback is what would apply the death to `status`; the fence stops it here.
        sess._alive = False
        sess.end_reason = "crashed"
        await hub._on_session_exit(sess, 1)
        types = [e.get("type") for e in hub.get_worker_events(session_id="codex-run")]
        self.assertNotIn("crashed", types, "the fence must keep the parent from a false failure")
        self.assertNotIn("exited", types)
        woke = await hub.wait_inbox.wait(handle, 0.1)
        self.assertEqual(woke["state"], "waiting", "no terminal event may settle the parent")

        release.set()
        prepared = await asyncio.wait_for(task, 5)
        self.assertEqual(prepared["sessions"]["codex-run"],
                         {"engine": "codex", "status": "running"})
        await hub.archive_all_sessions_on_shutdown(planned=True)
        manifest = self.manifest()
        self.assertEqual(manifest["session_ids"], ["codex-run"])
        self.assertEqual(manifest["sessions"]["codex-run"]["status"], "running",
                         "the status taken when the restart was accepted survives the death")
        self.assertEqual(self.stored_row("codex-run")["end_reason"], "interrupted")

    async def test_a_hard_kill_mid_resume_continues_on_the_next_start(self):
        """No `finally` runs on a hard kill: the consumed request must be picked up again."""
        hub = self.make_hub()
        await hub.start()
        for sid in ("codex-a", "codex-b", "codex-c"):
            await self.live_session(hub, sid, "codex", "running")
        await hub.archive_all_sessions_on_shutdown(planned=True)
        (self.root / REQUEST_NAME).write_text("{}", encoding="utf-8")
        # The next start took the request and was killed before it wrote anything: the consumed
        # file and the manifest remain, and no result exists.
        dead_request = RestartResumeRequest.take(self.root, owner="the-start-that-was-killed")
        self.assertTrue((self.root / TAKEN_NAME).is_file())
        self.assertFalse((self.root / REQUEST_NAME).exists())
        self.assertFalse((self.root / RESULT_NAME).exists())
        self.assertTrue(dead_request.restart_id, "a take mints the id the result will carry")

        hub2 = self.make_hub()
        calls = {}
        with self.resume_with(hub2, calls), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub2.start()
            await asyncio.wait_for(hub2.restart_resume_gate._open.wait(), 5)
        self.assertEqual(sorted(calls), ["codex-a", "codex-b", "codex-c"],
                         "every session the dead pass never reached comes back")
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(result["restart_id"], dead_request.restart_id,
                         "the answer proves which take it belongs to")
        self.assertTrue(result["completed"])
        taken = json.loads((self.root / TAKEN_NAME).read_text(encoding="utf-8"))
        self.assertTrue(taken["completed"], "the answered take stays for a diagnosis, stamped")
        self.assertFalse((self.root / KEPT_AT_STOP).exists())
        await self.stop_windows(hub2)

        # A third cold start has nothing left to do and must not resume anything again.
        hub3 = self.make_hub()
        with patch.object(hub3, "resume_subagent", new_callable=AsyncMock) as resume3:
            await hub3.start()
            await asyncio.sleep(0.05)
        resume3.assert_not_called()
        self.assertEqual(hub3._restart_phase, "idle", "an answered take arms no resume")

    async def test_a_plain_cold_start_clears_a_manifest_and_never_revives_it(self):
        hub = self.make_hub()
        await hub.start()
        await self.live_session(hub, "codex-run", "codex", "running")
        await hub.archive_all_sessions_on_shutdown(planned=True)
        self.assertTrue((self.root / KEPT_AT_STOP).exists(), "the stop left its manifest")

        # No request file: this start resumes nothing, so the manifest has no reader any more.
        hub2 = self.make_hub()
        with patch.object(hub2, "resume_subagent", new_callable=AsyncMock) as resume2:
            await hub2.start()
            await asyncio.sleep(0.05)
        self.assertFalse((self.root / KEPT_AT_STOP).exists(), "a stale manifest must not survive")
        resume2.assert_not_called()

        # A restart accepted while nothing is live must not revive the archived session either.
        hub3 = self.make_hub()
        await hub3.start()
        await hub3.prepare_graceful_restart(mode="hub", restart_id="r-quiet")
        self.assertEqual(hub3._restart_live_snapshot(), {})
        await hub3.archive_all_sessions_on_shutdown(planned=True)
        self.assertFalse((self.root / KEPT_AT_STOP).exists(), "no kept session, no manifest")
        (self.root / REQUEST_NAME).write_text("{}", encoding="utf-8")
        hub4 = self.make_hub()
        with patch.object(hub4, "resume_subagent", new_callable=AsyncMock) as resume4:
            await hub4.start()
            await asyncio.sleep(0.05)
        resume4.assert_not_called()
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(result["sessions"], {})

    async def test_a_resume_pass_that_died_continues_on_the_next_start(self):
        hub = self.make_hub()
        await hub.start()
        for sid, engine in (("codex-a", "codex"), ("codex-b", "codex"), ("codex-c", "codex")):
            await self.live_session(hub, sid, engine, "running")
        await hub.archive_all_sessions_on_shutdown(planned=True)
        (self.root / REQUEST_NAME).write_text("{}", encoding="utf-8")

        hub = self.make_hub()
        calls = {}
        with self.resume_with(hub, calls, deaths={"codex-b"}), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        self.assertEqual(calls, {"codex-a": CONTINUE_TEXT})
        # The pass died inside b: the manifest and the request both stay for the next start.
        self.assertTrue((self.root / KEPT_AT_STOP).exists())
        self.assertTrue((self.root / REQUEST_NAME).is_file(), "the request goes back")
        await self.stop_windows(hub)

        hub = self.make_hub()
        port_open = asyncio.Event()
        calls = {}

        async def wait_port(port):
            await port_open.wait()
            return True

        with self.resume_with(hub, calls), \
                patch("session_manager.wait_for_port", side_effect=wait_port):
            await hub.start()
            # A session an earlier pass already brought back: the runner must not resume it again.
            await self.live_session(hub, "codex-a", "codex", "running")
            port_open.set()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        self.assertEqual(sorted(calls), ["codex-b", "codex-c"])
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))["sessions"]
        self.assertTrue(result["codex-a"]["already_live"])
        self.assertFalse(result["codex-a"]["continued"])
        self.assertFalse((self.root / KEPT_AT_STOP).exists())


if __name__ == "__main__":
    unittest.main()
