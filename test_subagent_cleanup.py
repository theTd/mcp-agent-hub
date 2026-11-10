"""Idle cleanup regressions with fake clocks/processes and isolated storage."""
import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from agent_guidance import CLEANUP_HINT, SERVER_INSTRUCTIONS
from parent_waiting import build_wait_contract
from session_manager import AgentSession, HubSessionManager


class FakePty:
    def __init__(self):
        self.alive = True
        self.stops = 0

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.stops += 1
        self.alive = False


class SubagentCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-cleanup-")
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles.json"
        self.profiles.write_text('{"profiles":{},"global_routing_instructions":"keep"}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(self.profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")
        self.hub_patch = patch.object(self.server, "hub", self.hub)
        self.hub_patch.start()

    async def asyncTearDown(self):
        self.hub_patch.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    def session(self, sid="fixture", source="agent", status="completed", alive=True):
        sess = AgentSession(sid, "codex", "Codex", "tui", sid, str(self.root), "unused", [])
        sess.engine = "codex"
        sess.profile_snapshot = {"launch_source": source, "engine": "codex", "cwd": str(self.root)}
        sess.pty = FakePty() if alive else None
        sess.status = status
        sess.completion_reported = status == "completed"
        sess.set_final_response("saved answer", "native_transcript")
        if sess._inactive_since is not None:
            sess._inactive_since -= 3601
        self.hub.sessions[sid] = sess
        return sess

    async def test_timeout_kills_once_archives_and_preserves_answer(self):
        sess = self.session()
        self.assertEqual(self.hub.subagent_idle_timeout_minutes, 60)
        self.assertEqual(await self.hub.cleanup_idle_subagents(), 1)
        self.assertEqual(await self.hub.cleanup_idle_subagents(), 0)
        self.assertEqual(sess.pty.stops, 1)
        self.assertNotIn(sess.session_id, self.hub.sessions)
        await self.hub.db.flush()
        record = await self.hub.db.get_session_record(sess.session_id)
        self.assertTrue(record["archived_at"])
        self.assertEqual(record["end_reason"], "terminated")
        self.assertIn("自动关闭", record["current_activity"])
        info = await self.hub.get_subagent_final_response(sess.session_id)
        self.assertEqual(info["final_response"], "saved answer")
        events = self.hub.get_worker_events(session_id=sess.session_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "terminated")
        self.assertIn("Hub 自动关闭", events[0]["content"])
        self.assertNotIn("用户在", events[0]["content"])

    async def test_only_inactive_agent_sessions_are_eligible(self):
        for status in ("starting", "running", "waiting_parent"):
            self.session(status, status=status)
        self.session("manual", source="user")
        self.session("legacy", source="")
        question = self.session("question")
        question.pending_question_id = "q"
        approval = self.session("approval")
        approval.pending_interaction = {"kind": "approval_request"}
        writer = self.session("writer")
        claim = writer.tui_writer.claim("parent_message")
        try:
            self.assertEqual(await self.hub.cleanup_idle_subagents(), 0)
        finally:
            claim.release()

    async def test_exact_deadline_no_reset_from_polling_output_or_duplicate_completion(self):
        sess = self.session()
        since = sess._inactive_since
        self.assertFalse(self.hub._subagent_cleanup_due(sess, since + 3599.99))
        sess.to_dict()
        sess.last_output_at += 10000
        await self.hub.record_worker_event(sess.session_id, {"type": "turn_completed", "content": "saved answer"})
        await self.hub.record_worker_event(sess.session_id, {"type": "turn_completed"})
        self.assertEqual(sess._inactive_since, since)
        self.assertTrue(self.hub._subagent_cleanup_due(sess, since + 3600))

    async def test_followup_and_resume_start_fresh_countdown(self):
        sess = self.session()
        self.hub.begin_new_turn(sess, status="running")
        self.assertIsNone(sess._inactive_since)
        self.assertEqual(await self.hub.cleanup_idle_subagents(), 0)
        await self.hub.record_worker_event(sess.session_id, {"type": "turn_completed", "content": "new answer"})
        self.assertIsNotNone(sess._inactive_since)
        self.assertEqual(await self.hub.cleanup_idle_subagents(), 0)
        # Resume creates a fresh AgentSession even when created_at belongs to the old session.
        replacement = self.session("resumed")
        replacement.created_at = 1
        replacement.status = "running"
        replacement.status = "completed"
        self.assertEqual(await self.hub.cleanup_idle_subagents(), 0)

    async def test_exited_and_crashed_sessions_are_archived_without_changing_reason(self):
        for status in ("exited", "crashed"):
            sess = self.session(status, status=status, alive=False)
            sess.end_reason = status
        self.assertEqual(await self.hub.cleanup_idle_subagents(), 2)
        await self.hub.db.flush()
        for status in ("exited", "crashed"):
            record = await self.hub.db.get_session_record(status)
            self.assertEqual(record["end_reason"], status)
            self.assertEqual(self.hub.get_worker_events(session_id=status)[0]["type"], "closed")

    async def test_settings_persist_disable_reload_and_apply_to_existing_sessions(self):
        sess = self.session()
        self.hub.save_global_routing_instructions("updated", subagent_idle_timeout_minutes=0)
        self.assertEqual(await self.hub.cleanup_idle_subagents(), 0)
        data = json.loads(self.profiles.read_text(encoding="utf-8"))
        self.assertEqual(data["subagent_idle_timeout_minutes"], 0)
        self.hub.subagent_idle_timeout_minutes = 60
        self.hub.load_profiles(discovery={"engines": {}})
        self.assertEqual(self.hub.subagent_idle_timeout_minutes, 0)
        self.hub.save_global_routing_instructions("updated", subagent_idle_timeout_minutes=120)
        self.assertEqual(await self.hub.cleanup_idle_subagents(), 0)
        self.hub.save_global_routing_instructions("updated", subagent_idle_timeout_minutes=1)
        self.assertEqual(await self.hub.cleanup_idle_subagents(), 1)
        self.assertNotIn(sess.session_id, self.hub.sessions)

    async def test_http_setting_validation_is_atomic_and_old_clients_preserve_timeout(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as client:
            for value in (None, True, -1, 1.5, "60", {}, []):
                response = await client.post("/api/routing-guide", json={
                    "global_routing_instructions": "must not save", "subagent_idle_timeout_minutes": value})
                self.assertEqual(response.status_code, 400, value)
                self.assertEqual(self.hub.global_routing_instructions, "keep")
            response = await client.post("/api/routing-guide", json={
                "global_routing_instructions": "new", "subagent_idle_timeout_minutes": 15})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["subagent_idle_timeout_minutes"], 15)
            response = await client.post("/api/routing-guide", json={"global_routing_instructions": "old client"})
            self.assertEqual(response.json()["subagent_idle_timeout_minutes"], 15)
            response = await client.get("/api/profiles")
            self.assertEqual(response.json()["subagent_idle_timeout_minutes"], 15)

    async def test_cleanup_rechecks_state_after_waiting_for_resume_lock(self):
        sess = self.session()
        lock = self.hub._resume_lock(sess.session_id)
        async with lock:
            cleanup = asyncio.create_task(self.hub.cleanup_idle_subagents())
            await asyncio.sleep(0)
            self.hub.begin_new_turn(sess, status="running")
        self.assertEqual(await cleanup, 0)
        self.assertTrue(sess.pty.alive)

    async def test_manual_and_automatic_close_race_emits_one_event(self):
        sess = self.session()
        await asyncio.gather(self.hub.cleanup_idle_subagents(), self.hub.close_session(sess.session_id, source="mcp"))
        self.assertEqual(sess.pty.stops, 1)
        self.assertEqual(len(self.hub.get_worker_events(session_id=sess.session_id)), 1)

    async def test_background_sweep_and_shutdown(self):
        self.session()
        with patch("session_manager.SUBAGENT_CLEANUP_INTERVAL_SEC", 0.01):
            # The production task is already sleeping for 30 s. Exercise the same loop at
            # a short interval without sleeping for a real timeout in this regression.
            sweep = self.hub._schedule(self.hub._subagent_cleanup_loop())
            for _ in range(50):
                if not self.hub.sessions:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(self.hub.sessions, {})
        await self.hub.archive_all_sessions_on_shutdown()
        self.assertTrue(sweep.done())
        self.assertTrue(all(task.done() for task in self.hub._background_tasks))

    async def test_cleanup_guidance_reaches_server_routing_and_every_wait(self):
        self.assertIn(CLEANUP_HINT, SERVER_INSTRUCTIONS)
        self.assertIn(CLEANUP_HINT, self.hub.build_routing_guide_markdown())
        result = build_wait_contract({"done": {"status": "completed"}}, [], 1)
        self.assertIn("terminate_subagent", result["continuation_hint"])
        self.assertIn("60 minutes", self.hub.build_routing_guide_markdown())


if __name__ == "__main__":
    unittest.main()
