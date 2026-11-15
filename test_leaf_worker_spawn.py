"""leaf_worker on spawn: required from MCP, puts the leaf-worker note before the delivered task only."""
import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastmcp import Client

from agent_guidance import LEAF_WORKER_NOTE, SERVER_INSTRUCTIONS
from session_manager import AgentSession, HubSessionManager

TASK = "Fix the parser and report back."


def _data(result):
    return result.data if result.data is not None else json.loads(result.content[0].text)


class LeafWorkerSpawnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-leaf-worker-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")
        self.hub_patch = patch.object(self.server, "hub", self.hub)
        self.hub_patch.start()
        for engine in ("codex", "omp", "pwsh"):
            self.hub.profiles[engine] = {
                "name": engine, "engine": engine, "enabled": True, "kind": "tui",
                "command": "unused", "args": [],
                "task_mode": {"codex": "codex_positional", "omp": "omp_positional"}.get(engine, "pty_input"),
            }
        self.injected = []

        def record_startup(sess, task_to_inject=None, **kwargs):
            self.injected.append(task_to_inject)

            async def nothing():
                return None
            return nothing()

        def no_watchers(coro):
            coro.close()
        self.patches = [
            patch.object(AgentSession, "start_pty"),
            patch.object(self.hub, "_schedule", no_watchers),
            patch.object(self.hub, "_auto_handle_tui_startup_and_trust", record_startup),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.hub_patch.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def test_schema_requires_leaf_worker(self):
        async with Client(self.server.mcp) as client:
            schema = next(t for t in await client.list_tools() if t.name == "spawn_subagent").inputSchema
        self.assertIn("leaf_worker", schema["required"])
        self.assertEqual(schema["properties"]["leaf_worker"]["type"], "boolean")
        self.assertIn("leaf_worker", SERVER_INSTRUCTIONS)

    async def test_missing_leaf_worker_is_refused_with_a_hint(self):
        async with Client(self.server.mcp) as client:
            with self.assertRaises(Exception) as caught:
                await client.call_tool("spawn_subagent", {"agent_type": "codex", "task": TASK})
            self.assertIn("requires leaf_worker", str(caught.exception))
            self.assertIn("reconnect the agent-hub MCP server", str(caught.exception))
            with self.assertRaises(Exception) as caught:
                await client.call_tool("spawn_subagents", {"specs": [
                    {"agent_type": "codex", "leaf_worker": True, "task": TASK},
                    {"agent_type": "omp", "task": TASK},
                ]})
            self.assertIn("requires leaf_worker", str(caught.exception))
        self.assertEqual(self.hub.sessions, {})

    async def test_true_puts_the_note_before_the_delivered_task_only(self):
        async with Client(self.server.mcp) as client:
            data = _data(await client.call_tool(
                "spawn_subagent", {"agent_type": "codex", "leaf_worker": True, "task": TASK}))
        sess = self.hub.sessions[data["session_id"]]
        self.assertEqual(sess.args[-1], f"{LEAF_WORKER_NOTE}\n\n{TASK}\n\n[Hub Session: session_id={sess.session_id}]")
        self.assertEqual(sess.last_task, TASK)
        self.assertNotIn("agent-hub", sess.title)
        self.assertIs(sess.profile_snapshot["leaf_worker"], True)
        self.assertNotIn(LEAF_WORKER_NOTE, " ".join(sess.profile_snapshot["args"]))

    async def test_false_sends_the_task_unchanged(self):
        async with Client(self.server.mcp) as client:
            data = _data(await client.call_tool(
                "spawn_subagent", {"agent_type": "codex", "leaf_worker": False, "task": TASK}))
        sess = self.hub.sessions[data["session_id"]]
        self.assertEqual(sess.args[-1], f"{TASK}\n\n[Hub Session: session_id={sess.session_id}]")
        self.assertIs(sess.profile_snapshot["leaf_worker"], False)

    async def test_true_without_a_task_is_refused(self):
        async with Client(self.server.mcp) as client:
            with self.assertRaises(Exception):
                await client.call_tool("spawn_subagent", {"agent_type": "codex", "leaf_worker": True})
        self.assertEqual(self.hub.sessions, {})

    async def test_group_spawn_and_typed_task_carry_the_note(self):
        async with Client(self.server.mcp) as client:
            data = _data(await client.call_tool("spawn_subagents", {"specs": [
                {"agent_type": "omp", "leaf_worker": True, "task": TASK},
                {"agent_type": "pwsh", "leaf_worker": True, "task": TASK},
                {"agent_type": "codex", "leaf_worker": False, "task": TASK},
            ]}))
        omp, pwsh, codex = (self.hub.sessions[s["session_id"]] for s in data["sessions"])
        self.assertTrue(omp.args[-1].startswith(LEAF_WORKER_NOTE + "\n\n" + TASK))
        self.assertEqual([text for text in self.injected if text], [f"{LEAF_WORKER_NOTE}\n\n{TASK}\n\n[Hub Session: session_id={pwsh.session_id}]"])
        self.assertTrue(codex.args[-1].startswith(TASK))

    async def test_console_launch_defaults_to_false_and_checks_the_type(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            ok = await client.post("/api/sessions", json={"profile_id": "codex", "task": TASK})
            self.assertEqual(ok.status_code, 200)
            bad = await client.post("/api/sessions", json={"profile_id": "codex", "task": TASK, "leaf_worker": "yes"})
            self.assertEqual(bad.status_code, 400)
        (sess,) = self.hub.sessions.values()
        self.assertIs(sess.profile_snapshot["leaf_worker"], False)
        self.assertNotIn(LEAF_WORKER_NOTE, sess.args[-1])


if __name__ == "__main__":
    unittest.main()
