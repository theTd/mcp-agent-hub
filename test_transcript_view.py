"""Engine-neutral transcript feed regressions."""
import asyncio
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

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
