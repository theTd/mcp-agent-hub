import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class SessionViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="session-views-")
        os.environ["HUB_DATA_DIR"] = self.tmp.name
        self.server = importlib.import_module("server")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_compact_fields_and_filters(self):
        raw = {"session_id": "s1", "title": "T", "profile_id": "omp", "engine": "omp", "status": "running", "current_activity": "x" * 200, "step_count": 3, "pending_interaction": None, "final_response": "answer", "cwd": self.tmp.name, "created_at": 1, "last_output_at": 2}
        fake = SimpleNamespace(to_dict=lambda: dict(raw), effective_model="m")
        old = self.server.hub.sessions
        self.server.hub.sessions = {"s1": fake}
        self.addAsyncCleanup(self._restore, old)
        result = await self.server.list_active_sessions(status_filter=["running"])
        entry = result["active_sessions"][0]
        self.assertEqual(set(entry), {"session_id", "title", "agent_type", "model", "status", "current_activity", "step_count", "final_response_exists", "final_response_length", "cwd", "updated_at"})
        self.assertEqual(len(entry["current_activity"]), 120)
        self.assertEqual((await self.server.list_active_sessions(status_filter=["completed"]))["active_sessions"], [])

    async def test_all_filters_history_and_wait_handle(self):
        rows = []
        for sid, status in (("a", "running"), ("b", "completed")):
            rows.append(SimpleNamespace(to_dict=lambda sid=sid, status=status: {"session_id": sid, "title": sid, "profile_id": "omp", "status": status, "current_activity": "", "step_count": 1, "cwd": self.tmp.name}, effective_model="m"))
        old = self.server.hub.sessions
        self.server.hub.sessions = {"a": rows[0], "b": rows[1]}
        self.addAsyncCleanup(self._restore, old)
        with patch.object(self.server.hub.wait_inbox, "session_ids_for", return_value=["b"]):
            result = await self.server.list_active_sessions(wait_handle="/api/wait/wh_x")
        self.assertEqual([x["session_id"] for x in result["active_sessions"]], ["b"])
        self.assertEqual((await self.server.list_active_sessions(session_ids=["a"], status_filter=["running"]))["active_sessions"][0]["session_id"], "a")
        with patch.object(self.server.hub.db, "list_persisted_sessions", new=AsyncMock(return_value=[{"session_id": "old", "status": "completed", "title": "old", "profile_id": "omp", "cwd": self.tmp.name}])):
            history = await self.server.list_active_sessions(include_historical=True)
        self.assertEqual(history["historical_sessions"][0]["session_id"], "old")

    async def test_collect_group_skips_running_and_can_collect_all(self):
        def fake(sid, status, final=""):
            return SimpleNamespace(to_dict=lambda: {"session_id": sid, "status": status, "cwd": self.tmp.name, "final_response": final}, effective_model="m")
        old = self.server.hub.sessions
        self.server.hub.sessions = {"done": fake("done", "completed", "answer"), "failed": fake("failed", "failed", "bad"), "run": fake("run", "running")}
        self.addAsyncCleanup(self._restore, old)
        with patch.object(self.server.hub.wait_inbox, "session_ids_for", return_value=["done", "failed", "run"]), patch.object(self.server.hub, "get_subagent_final_response", new=AsyncMock(side_effect=lambda session_id: {"final_response": self.server.hub.sessions[session_id].to_dict().get("final_response", "")})), patch.object(self.server.hub, "close_session", new=AsyncMock()):
            result = await self.server.collect_and_terminate(wait_handle="wh_group")
            self.assertEqual(len(result["collected"]), 2)
            self.assertEqual(result["skipped"][0]["session_id"], "run")
            result = await self.server.collect_and_terminate(wait_handle="wh_group", only_completed=False)
            self.assertEqual(len(result["collected"]), 3)

    async def test_collect_truncates_already_closed_and_continues_after_error(self):
        old = self.server.hub.sessions
        self.server.hub.sessions = {"live": SimpleNamespace(to_dict=lambda: {"session_id": "live", "status": "completed", "cwd": self.tmp.name}, effective_model="m"), "bad": SimpleNamespace(to_dict=lambda: {"session_id": "bad", "status": "completed", "cwd": self.tmp.name}, effective_model="m")}
        self.addAsyncCleanup(self._restore, old)
        with patch.object(self.server.hub, "get_subagent_final_response", new=AsyncMock(return_value={"final_response": "x" * 20})), patch.object(self.server.hub, "close_session", new=AsyncMock(side_effect=[RuntimeError("close failed"), None])):
            result = await self.server.collect_and_terminate(session_ids=["live", "bad"], max_chars=5, only_completed=False)
        self.assertTrue(any(item.get("error") for item in result["collected"]))

        self.server.hub.sessions = {}
        with patch.object(self.server.hub.db, "get_session_record", new=AsyncMock(return_value={"session_id": "old", "status": "completed", "cwd": self.tmp.name, "final_response": "z" * 8})):
            result = await self.server.collect_and_terminate(session_ids=["old"])
        self.assertTrue(result["collected"][0]["already_closed"])
        self.assertTrue(result["collected"][0]["truncated"] is False)

    async def test_collect_tool_schema_is_exposed(self):
        from fastmcp import Client
        async with Client(self.server.mcp) as client:
            tool = next(item for item in await client.list_tools() if item.name == "collect_and_terminate")
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")
        self.assertTrue({"wait_handle", "session_ids", "only_completed", "max_chars"} <= set(schema["properties"]))

    async def _restore(self, sessions):
        self.server.hub.sessions = sessions

    async def test_profiles_compact_and_full(self):
        with patch.object(self.server, "ensure_profiles_loaded", new=AsyncMock()), \
             patch.object(self.server.hub, "build_routing_guide_markdown", return_value="guide"), \
             patch.object(self.server.hub, "get_mcp_engines_catalog", return_value={"e": {}}), \
             patch.object(self.server.hub, "get_mcp_enabled_profiles", return_value={"p": {"id": "p", "name": "P", "engine": "e", "default_model": "m", "default_thinking_level": "low", "when_to_use": "x", "env": {}, "description": "long"}}), \
             patch.object(self.server.hub, "global_routing_instructions", "duplicate", create=True):
            compact = await self.server.list_agent_profiles()
            full = await self.server.list_agent_profiles(view="full")
        self.assertNotIn("global_routing_instructions", compact)
        self.assertNotIn("engines_catalog", compact)
        self.assertIn("global_routing_instructions", full)
        self.assertIn("engines_catalog", full)

    async def test_historical_compact_reads_persisted_final_response_flag(self):
        from hub_database import HubDatabase
        db = HubDatabase(Path(self.tmp.name) / "persisted.db")
        db.upsert_session({
            "session_id": "persisted", "profile_id": "omp", "profile_name": "OMP", "kind": "worker",
            "title": "persisted", "cwd": self.tmp.name, "status": "completed", "created_at": 1,
            "step_count": 1, "current_activity": "", "engine": "omp", "effective_model": "m",
            "final_response": "stored answer", "final_response_source": "worker",
        })
        async def read_rows(**kwargs):
            kwargs.pop("consistent", None)
            return db.list_persisted_sessions(**kwargs)
        with patch.object(self.server.hub.db, "list_persisted_sessions", new=AsyncMock(side_effect=read_rows)):
            result = await self.server.list_active_sessions(include_historical=True)
        entry = next(x for x in result["historical_sessions"] if x["session_id"] == "persisted")
        self.assertTrue(entry["final_response_exists"])
        self.assertEqual(entry["final_response_length"], 0)


if __name__ == "__main__":
    unittest.main()
