import asyncio
import importlib
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client


class WaitToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_tools_are_listed_with_parameters(self):
        data_dir = tempfile.TemporaryDirectory(prefix="wait-tools-")
        self.addCleanup(data_dir.cleanup)
        os.environ["HUB_DATA_DIR"] = data_dir.name
        server = importlib.import_module("server")
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
        self.assertIn("wait_for_hub", tools)
        self.assertIn("respond_to_subagents", tools)
        for name, required in {
            "wait_for_hub": {"wait_handle", "timeout_seconds", "ask_coalesce_ms"},
            "respond_to_subagents": {"responses"},
        }.items():
            schema = getattr(tools[name], "input_schema", None) or getattr(tools[name], "inputSchema")
            self.assertTrue(required <= set(schema.get("properties", {})), name)

    def test_guidance_mentions_both_paths(self):
        from agent_guidance import SERVER_INSTRUCTIONS
        self.assertIn("wait_for_hub", SERVER_INSTRUCTIONS)
        self.assertIn("respond_to_subagents", SERVER_INSTRUCTIONS)

    async def test_batch_response_keeps_good_entries_when_one_fails(self):
        data_dir = tempfile.TemporaryDirectory(prefix="wait-tools-batch-")
        self.addCleanup(data_dir.cleanup)
        os.environ["HUB_DATA_DIR"] = data_dir.name
        server = importlib.import_module("server")
        with patch.object(server, "_respond_to_subagent_impl", new=AsyncMock(side_effect=[
            {"wait_handle": "wh_good"}, ValueError("bad request")
        ])):
            result = await server.respond_to_subagents([
                {"session_id": "good", "request_id": "r1", "answer": "yes"},
                {"session_id": "bad", "request_id": "r2", "answer": "no"},
            ])
        self.assertEqual([entry["ok"] for entry in result["responses"]], [True, False])
        self.assertEqual(result["wait_handles"], ["wh_good"])
        with self.assertRaises(ValueError):
            await server.respond_to_subagents([{}] * 51)

    async def test_wait_tool_timeout_and_validation(self):
        data_dir = tempfile.TemporaryDirectory(prefix="wait-tools-timeout-")
        self.addCleanup(data_dir.cleanup)
        os.environ["HUB_DATA_DIR"] = data_dir.name
        server = importlib.import_module("server")
        fake_wait = AsyncMock(return_value={"state": "waiting", "wait_handle": "wh_test"})
        with patch.object(server.hub, "wait_inbox") as inbox:
            inbox.wait = fake_wait
            result = await server.wait_for_hub("https://127.0.0.1/api/wait/wh_test", timeout_seconds=1)
        self.assertEqual(result["state"], "waiting")
        with self.assertRaises(ValueError):
            await server.wait_for_hub("bad", timeout_seconds=1)
        with self.assertRaises(ValueError):
            await server.wait_for_hub("wh_test", timeout_seconds=0)
