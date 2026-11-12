"""Engine-neutral transcript feed regressions."""
import asyncio
import importlib
import logging
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from session_manager import AgentSession, HubSessionManager


class TranscriptViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-transcript-")
        root = Path(self.temp.name)
        profiles = root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=root)
        await self.hub.start()
        session = AgentSession("view", "codex", "Codex", "tui", "view", str(root), "unused", [])
        session.engine = "codex"
        session.profile_snapshot = {"engine": "codex"}
        session.last_task = "检查这个目录"
        session.status = "running"
        self.hub.sessions[session.session_id] = session

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def send_from_console(self, text):
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            server = importlib.import_module("server")

        class Body:
            async def json(self):
                return {"text": text}

        with patch.object(server, "hub", self.hub):
            return await server.api_send_message("view", Body())

    async def test_console_message_reaches_a_live_session_as_the_consoles(self):
        delivered = AsyncMock(return_value={"delivered": True, "injection": "live_write"})
        with patch.object(self.hub.sessions["view"], "is_process_alive", return_value=True), \
                patch.object(self.hub, "deliver_parent_message", delivered):
            reply = await self.send_from_console("再看一下日志")
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["resumed"])
        self.assertEqual(reply["session"]["session_id"], "view")
        kwargs = delivered.await_args.kwargs
        self.assertEqual((kwargs["text"], kwargs["press_enter"], kwargs["source"]), ("再看一下日志", True, "console"))

    async def test_console_message_resumes_an_ended_session(self):
        session = self.hub.sessions["view"]
        session.status = "exited"
        resumed = AsyncMock(return_value={"session": session, "resumed": True,
                                          "delivery": {"delivered": True, "injection": "pending"}})
        with patch.object(self.hub, "resume_subagent", resumed):
            reply = await self.send_from_console("接着做")
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["resumed"])
        self.assertEqual(resumed.await_args.kwargs, {"text": "接着做", "source": "console"})
        failed = AsyncMock(return_value={"delivered": False, "injection": "dsh_failed", "reason": "DSH 拒绝了这条消息"})
        with patch.object(session, "is_process_alive", return_value=True), \
                patch.object(self.hub, "deliver_parent_message", failed):
            reply = await self.send_from_console("再试一次")
        self.assertEqual((reply["ok"], reply["error"]), (False, "DSH 拒绝了这条消息"))
        empty = await self.send_from_console("   ")
        self.assertEqual(empty.status_code, 400)

    async def test_resume_text_is_recorded_with_its_sender(self):
        locked = AsyncMock(return_value={"session": self.hub.sessions["view"], "resumed": True})
        with patch.object(self.hub, "_resume_locked", locked):
            await self.hub.resume_subagent("view", text="继续", source="console")
        message = self.hub.parent_messages["view"][-1]
        self.assertEqual((message["content"], message["source"], message["read_by_worker"]), ("继续", "console", True))
        feed = await self.hub.get_transcript_view("view")
        self.assertIn(("console", "继续"), [(e.get("origin"), e["text"]) for e in feed["entries"] if e["kind"] == "parent"])

    async def test_feed_merges_events_and_telemetry_without_cli_specific_fields(self):
        await self.hub.record_worker_event(self.hub.sessions["view"].session_id, {
            "type": "progress", "category": "progress", "content": "正在读取文件",
        })
        await self.hub.record_telemetry(self.hub.sessions["view"].session_id, {
            "hook_event": "PreToolUse", "tool_name": "read_file", "tool_input": "README.md",
            "source": "test",
        })
        feed = await self.hub.get_transcript_view("view")
        self.assertEqual(feed["session_id"], "view")
        self.assertEqual(feed["entries"][0]["kind"], "task")
        tool = next(item["tool"] for item in feed["entries"] if item["kind"] == "tool")
        self.assertEqual((tool["name"], tool["input"]), ("read_file", "README.md"))
        self.assertTrue(any(item.get("text") == "正在读取文件" for item in feed["entries"]))
        self.assertNotIn("codex", {item.get("engine") for item in feed["entries"]})

        # The persisted tail keeps the view readable after a Hub restart or archive transition.
        await self.hub.db.flush()
        self.hub.sessions.pop("view")
        restored = await self.hub.get_transcript_view("view")
        self.assertTrue(any(item.get("text") == "正在读取文件" for item in restored["entries"]))
        # Tool details live in the event's structured data, which is persisted with it.
        restored_tool = next(item["tool"] for item in restored["entries"] if item["kind"] == "tool")
        self.assertEqual(restored_tool["input"], "README.md")

    async def test_unchanged_poll_is_answered_without_building_the_feed(self):
        first = await self.hub.get_transcript_view("view")
        version = first["version"]
        self.assertTrue(version)
        with patch("session_manager.build_transcript_feed", side_effect=AssertionError("feed built")), \
                patch.object(self.hub, "get_worker_events", side_effect=AssertionError("events read")):
            same = await self.hub.get_transcript_view("view", since=version)
        self.assertEqual(same, {"session_id": "view", "unchanged": True, "version": version})
        # Another page size is another transcript.
        self.assertNotIn("unchanged", await self.hub.get_transcript_view("view", limit=200, since=version))

    async def test_every_transcript_input_changes_the_version(self):
        session = self.hub.sessions["view"]

        async def changes(action):
            before = self.hub.transcript_version("view", 80)
            result = action()
            if asyncio.iscoroutine(result):
                await result
            after = self.hub.transcript_version("view", 80)
            self.assertNotEqual(before, after)
            feed = await self.hub.get_transcript_view("view", since=before)
            self.assertNotIn("unchanged", feed)
            self.assertEqual(feed["version"], after)

        await changes(lambda: self.hub.record_worker_event("view", {"type": "progress", "content": "第一步"}))
        await changes(lambda: self.hub.record_telemetry("view", {
            "hook_event": "AssistantMessage", "intermediate_messages": ["中间消息"], "source": "test"}))
        await changes(lambda: self.hub._add_transcript_message("view", 1, "只存了消息", source="agent"))
        await changes(lambda: self.hub.parent_messages.setdefault("view", []).append(
            {"id": "pmsg-1", "content": "再看看", "timestamp": 1.0}))
        await changes(lambda: setattr(session, "status", "completed"))
        await changes(lambda: setattr(session, "final_response", "完成了"))
        await changes(lambda: setattr(session, "pending_interaction", {"request_id": "q1", "prompt": "继续吗？"}))
        await changes(lambda: setattr(session, "last_task", "新的任务"))

    async def test_archived_session_has_no_version_and_is_always_built(self):
        await self.hub.record_worker_event("view", {"type": "progress", "content": "存档前的进展"})
        await self.hub.db.flush()
        self.hub.sessions.pop("view")
        feed = await self.hub.get_transcript_view("view", since="anything")
        self.assertIsNone(feed["version"])
        self.assertNotIn("unchanged", feed)

    def test_transcript_polls_stay_out_of_the_access_log(self):
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            server = importlib.import_module("server")
        quiet = server.TranscriptPollLogFilter()

        def access(path, status=200, method="GET"):
            return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                     ("127.0.0.1:1", method, path, "1.1", status), None)

        self.assertFalse(quiet.filter(access("/api/sessions/view/transcript?limit=80&since=abc")))
        self.assertTrue(quiet.filter(access("/api/sessions/view/transcript?limit=80", status=404)))
        self.assertTrue(quiet.filter(access("/api/sessions/view/transcript-summary")))
        self.assertTrue(quiet.filter(access("/api/sessions")))

    async def test_unknown_session_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.hub.get_transcript_view("missing")

    async def test_pending_interaction_is_exposed_for_transcript_controls(self):
        session = self.hub.sessions["view"]
        session.status = "waiting_parent"
        session.pending_interaction = {
            "request_id": "req-1",
            "kind": "question",
            "title": "选择方案",
            "prompt": "继续吗？",
            "options": ["继续", "停止"],
        }
        feed = await self.hub.get_transcript_view("view")
        self.assertEqual(feed["pending_interaction"]["request_id"], "req-1")
        self.assertEqual(feed["pending_interaction"]["options"], ["继续", "停止"])


if __name__ == "__main__":
    unittest.main()
