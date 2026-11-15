"""Spawn wiring for approve-once prefixes and brief files, and the parent-facing interaction view.

No model calls and no real terminal: PTY start is patched for spawns, and the interaction tests use
the fake core plus a directly registered omp approval box.
"""
import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastmcp import Client

from agent_guidance import LEAF_WORKER_NOTE, SERVER_INSTRUCTIONS
from fake_terminal_core import FakeTerminal
from session_manager import AgentSession, HubSessionManager

PREFIX = r"C:\tools\adb.exe shell"
BRIEF = "# Brief\n\nA task description longer than a few lines.\n"


def _data(result):
    return result.data if result.data is not None else json.loads(result.content[0].text)


class SpawnWiringCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-spawn-wiring-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.fake = FakeTerminal()
        self.hub.core = self.fake
        await self.hub.start()
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")
        self.hub_patch = patch.object(self.server, "hub", self.hub)
        self.hub_patch.start()
        self.hub.profiles["omp"] = {
            "name": "omp", "engine": "omp", "enabled": True, "kind": "tui",
            "command": "unused", "args": [], "task_mode": "omp_positional",
        }
        self.patches = [
            patch.object(AgentSession, "start_pty"),
            patch.object(self.hub, "_schedule", lambda coro: coro.close()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.hub_patch.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def spawn_via_mcp(self, **arguments):
        payload = {"agent_type": "omp", "leaf_worker": True, "task": "Do the work"}
        payload.update(arguments)
        async with Client(self.server.mcp) as client:
            return _data(await client.call_tool("spawn_subagent", payload))

    async def spawn_via_rest(self, client, **body):
        return await client.post("/api/sessions", json={"profile_id": "omp", "task": "Do the work", **body})

    async def register_omp_box(self, selected_index=0):
        sess = AgentSession("omp-box", "omp", "omp", "tui", "OMP box", str(self.root), "unused", [],
                            core=self.fake)
        sess.engine = "omp"
        sess.status = "running"
        sess.profile_snapshot = {"engine": "omp", "auto_approve_once_prefixes": [], "auto_approve_once_uses": 0}
        sess._loop = asyncio.get_running_loop()
        self.fake.spawn(sess.session_id, ["fixture"])
        self.hub.sessions[sess.session_id] = sess
        interaction = await self.hub.register_pending_interaction(
            sess,
            kind="approval_request",
            title="OMP Tool Approval (bash)",
            prompt="Allow tool: bash\nCommand: adb devices",
            options=["Approve", "Approve for this session", "Deny"],
            source="omp_tui_screen_detector",
            request_id="int-omp-box",
            metadata={
                "omp_prompt": "tool",
                "omp_options": ["Approve", "Approve for this session", "Deny"],
                "selected_index": selected_index,
                "command": "adb devices",
                "command_source": "screen",
                "command_truncated": False,
            },
        )
        return sess, interaction


class SpawnParameterTests(SpawnWiringCase):
    async def test_spawn_subagent_passes_prefixes_and_brief_through(self):
        data = await self.spawn_via_mcp(auto_approve_once_prefixes=[PREFIX], brief_markdown=BRIEF)
        sess = self.hub.sessions[data["session_id"]]
        self.assertEqual(sess.profile_snapshot["auto_approve_once_prefixes"], [PREFIX])
        brief_path = self.root / "briefs" / f"{sess.session_id}.md"
        self.assertEqual(sess.profile_snapshot["brief_path"], str(brief_path))
        self.assertEqual(brief_path.read_text(encoding="utf-8"), BRIEF)
        self.assertEqual(
            sess.args[-1],
            f"{LEAF_WORKER_NOTE}\n\nRead the full brief first: {brief_path.absolute()}\n\nDo the work"
            f"\n\n[Hub Session: session_id={sess.session_id}]",
        )

    async def test_spawn_subagents_spec_passes_both_through(self):
        async with Client(self.server.mcp) as client:
            data = _data(await client.call_tool("spawn_subagents", {"specs": [
                {"agent_type": "omp", "leaf_worker": True, "task": "Grouped",
                 "auto_approve_once_prefixes": [PREFIX], "brief_markdown": BRIEF},
            ]}))
        (entry,) = data["sessions"]
        sess = self.hub.sessions[entry["session_id"]]
        self.assertEqual(sess.profile_snapshot["auto_approve_once_prefixes"], [PREFIX])
        brief_path = self.root / "briefs" / f"{sess.session_id}.md"
        self.assertTrue(brief_path.exists())
        self.assertIn(f"{LEAF_WORKER_NOTE}\n\nRead the full brief first: {brief_path.absolute()}",
                      sess.args[-1])

    async def test_rest_create_session_passes_both_through(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            ok = await self.spawn_via_rest(client, auto_approve_once_prefixes=[PREFIX], brief_markdown=BRIEF)
        self.assertEqual(ok.status_code, 200)
        sess = self.hub.sessions[ok.json()["session"]["session_id"]]
        self.assertEqual(sess.profile_snapshot["auto_approve_once_prefixes"], [PREFIX])
        self.assertTrue((self.root / "briefs" / f"{sess.session_id}.md").exists())

    async def test_bad_prefix_is_a_clear_tool_error(self):
        async with Client(self.server.mcp) as client:
            with self.assertRaises(Exception) as caught:
                await client.call_tool("spawn_subagent", {
                    "agent_type": "omp", "leaf_worker": True, "task": "Do the work",
                    "auto_approve_once_prefixes": ["adb.exe shell"],
                })
        self.assertIn("auto approve prefix", str(caught.exception))
        self.assertIn("absolute path", str(caught.exception))
        self.assertEqual(self.hub.sessions, {})

    async def test_oversized_brief_is_a_clear_tool_error(self):
        async with Client(self.server.mcp) as client:
            with self.assertRaises(Exception) as caught:
                await client.call_tool("spawn_subagent", {
                    "agent_type": "omp", "leaf_worker": True, "task": "Do the work",
                    "brief_markdown": "x" * 200_001,
                })
        self.assertIn("brief_markdown", str(caught.exception))
        self.assertIn("200000", str(caught.exception))
        self.assertEqual(self.hub.sessions, {})

    async def test_bad_values_are_a_400_over_rest(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            bad_prefix = await self.spawn_via_rest(client, auto_approve_once_prefixes=["adb.exe shell"])
            oversized = await self.spawn_via_rest(client, brief_markdown="x" * 200_001)
        self.assertEqual(bad_prefix.status_code, 400)
        self.assertIn("auto approve prefix", bad_prefix.json()["detail"])
        self.assertEqual(oversized.status_code, 400)
        self.assertIn("brief_markdown", oversized.json()["detail"])
        self.assertEqual(self.hub.sessions, {})

    async def test_prefixes_for_another_engine_are_refused(self):
        self.hub.profiles["codex"] = {
            "name": "codex", "engine": "codex", "enabled": True, "kind": "tui",
            "command": "unused", "args": [], "task_mode": "pty_input",
        }
        async with Client(self.server.mcp) as client:
            with self.assertRaises(Exception) as caught:
                await client.call_tool("spawn_subagent", {
                    "agent_type": "codex", "leaf_worker": True, "task": "Do the work",
                    "auto_approve_once_prefixes": [PREFIX],
                })
        self.assertIn("omp and agy only, not codex", str(caught.exception))
        self.assertEqual(self.hub.sessions, {})
        # Without prefixes the same profile still spawns.
        data = await self.spawn_via_mcp(agent_type="codex", disabled_mcp_servers=["agent-hub"])
        self.assertIn(data["session_id"], self.hub.sessions)

    async def test_both_parameters_are_in_the_mcp_tool_schema(self):
        async with Client(self.server.mcp) as client:
            schema = next(t for t in await client.list_tools() if t.name == "spawn_subagent").inputSchema
        self.assertIn("auto_approve_once_prefixes", schema["properties"])
        self.assertIn("brief_markdown", schema["properties"])


class ParentInteractionViewTests(SpawnWiringCase):
    async def test_status_tool_hides_the_omp_cursor(self):
        sess, _ = await self.register_omp_box(selected_index=1)
        self.assertEqual(sess.pending_interaction["metadata"]["selected_index"], 1)
        async with Client(self.server.mcp) as client:
            data = _data(await client.call_tool("get_subagent_status_and_trace",
                                                {"session_id": sess.session_id}))
        self.assertIsNotNone(data["pending_interaction"])
        self.assertNotIn("selected_index", data["pending_interaction"]["metadata"])
        self.assertEqual(data["pending_interaction"]["metadata"]["command"], "adb devices")

    async def test_list_active_sessions_full_view_hides_the_cursor(self):
        sess, _ = await self.register_omp_box()
        await self.hub.persist_session(sess)
        async with Client(self.server.mcp) as client:
            data = _data(await client.call_tool("list_active_sessions",
                                                {"view": "full", "include_historical": True}))
        live = next(s for s in data["active_sessions"] if s["session_id"] == sess.session_id)
        self.assertNotIn("selected_index", live["pending_interaction"]["metadata"])
        self.assertEqual(live["pending_interaction"]["metadata"]["command"], "adb devices")

    async def test_rest_session_paths_hide_the_cursor(self):
        sess, _ = await self.register_omp_box()
        await self.hub.persist_session(sess)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            listing = await client.get("/api/sessions")
            one = await client.get(f"/api/sessions/{sess.session_id}")
            trace = await client.get(f"/api/sessions/{sess.session_id}/trace")
            history = await client.get("/api/history/sessions")
            responded = await client.post(f"/api/sessions/{sess.session_id}/respond",
                                          json={"decision": "deny", "request_id": "int-omp-box"})
        listed = next(s for s in listing.json()["sessions"] if s["session_id"] == sess.session_id)
        self.assertNotIn("selected_index", listed["pending_interaction"]["metadata"])
        self.assertNotIn("selected_index", one.json()["pending_interaction"]["metadata"])
        self.assertNotIn("selected_index", trace.json()["pending_interaction"]["metadata"])
        self.assertNotIn("selected_index", json.dumps(listing.json()))
        self.assertNotIn("selected_index", json.dumps(history.json()))
        self.assertNotIn("selected_index", json.dumps(responded.json()))
        self.assertEqual(responded.status_code, 200)

    async def test_respond_results_hand_back_no_cursor(self):
        sess, _ = await self.register_omp_box()
        async with Client(self.server.mcp) as client:
            data = _data(await client.call_tool("respond_to_subagent", {
                "session_id": sess.session_id, "request_id": "int-omp-box", "decision": "deny",
            }))
        self.assertNotIn("selected_index", json.dumps(data))
        self.assertEqual(data["response"]["resolved_request_id"], "int-omp-box")


class GuidanceTests(unittest.TestCase):
    def test_guidance_names_both_parameters(self):
        self.assertIn("auto_approve_once_prefixes", SERVER_INSTRUCTIONS)
        self.assertIn("brief_markdown", SERVER_INSTRUCTIONS)
        self.assertIn("approval_auto_resolved", SERVER_INSTRUCTIONS)
        self.assertIn("200 000", SERVER_INSTRUCTIONS)

    def test_auto_approval_rules_line_points_at_prefixes(self):
        line = next(rule for rule in SERVER_INSTRUCTIONS.splitlines()
                    if "Never set auto_approval_rules" in rule)
        self.assertIn("auto_approve_once_prefixes", line)
        self.assertIn("omp or agy", line)


if __name__ == "__main__":
    unittest.main()
