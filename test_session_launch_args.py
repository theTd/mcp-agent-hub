"""Session argv contract across preview, spawn, persistence and native resume; no model calls."""
import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastmcp import Client

from native_session_launch import normalize_extra_args
from session_manager import AgentSession, HubSessionManager, OMP_APPROVAL_EXTENSION_NAME, OMP_APPROVAL_LOG_ENV


class ExtraArgsValidationTests(unittest.TestCase):
    def test_tokens_are_literal_and_copied(self):
        tokens = ["--mcp-config", r"E:\configs with spaces\mcp.json", '{"mcpServers":{}}', "", "$(whoami)"]
        result = normalize_extra_args(tokens)
        self.assertEqual(result, tokens)
        self.assertIsNot(result, tokens)
        self.assertEqual(normalize_extra_args(None), [])

    def test_rejects_shell_strings_coercion_and_nul(self):
        for value in ('--flag value', {}, False, [None], [1], [True], ["a\x00b"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_extra_args(value)


class SessionLaunchArgsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-launch-args-")
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
        for engine in ("codex", "claude", "agy", "kimi", "omp", "pwsh", "dsh"):
            self.hub.profiles[engine] = {
                "name": engine, "engine": engine, "enabled": True, "kind": "tui",
                "command": "unused", "args": ["--profile-arg"],
                "task_mode": {"codex": "codex_positional", "claude": "claude_positional",
                              "agy": "agy_interactive_flag", "omp": "omp_positional"}.get(engine, "pty_input"),
            }

    async def asyncTearDown(self):
        self.hub_patch.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def test_preview_order_policy_and_no_profile_mutation(self):
        extra = ["-c", "mcp_servers.hidden.enabled=false", "--dangerously-bypass-approvals-and-sandbox"]
        launch = self.hub.resolve_launch("codex", cwd=str(self.root), extra_args=extra)
        self.assertEqual(launch["args"], ["--approve-for-me",
                                           "--profile-arg", "-c", "mcp_servers.hidden.enabled=false"])
        ask = self.hub.resolve_launch("codex", cwd=str(self.root), extra_args=extra, approval_mode="ask")
        self.assertEqual(ask["args"][:4], ["-a", "on-request", "-s", "workspace-write"])
        self.assertEqual(launch["extra_args"], extra)
        self.assertTrue(launch["notes"])
        self.assertEqual(self.hub.profiles["codex"]["args"], ["--profile-arg"])
        self.assertNotIn("extra_args", self.hub.profiles["codex"])

    async def test_creation_persistence_and_resume_do_not_duplicate_tokens(self):
        extra = ["--custom-option", r"E:\配置 with spaces\mcp.json", '{"mcpServers":{}}']
        for engine in ("codex", "claude", "agy", "kimi", "omp"):
            with self.subTest(engine=engine):
                # Suppress native processes/watchers, retaining the real creation and persistence path.
                with patch.object(AgentSession, "start_pty"), patch.object(self.hub, "_schedule", lambda coro: coro.close()):
                    sess = await self.hub.create_session(engine, task="fixture task", cwd=str(self.root), extra_args=extra)
                self.assertEqual(sess.args[sess.args.index("--custom-option"):][:3], extra)
                await self.hub.db.flush()
                record = await self.hub.db.get_session_record(sess.session_id)
                self.assertEqual(record["profile_snapshot"]["extra_args"], extra)
                self.assertNotIn("fixture task", str(record["profile_snapshot"]["args"]))
                record["conversation_ref"] = {"session_id": "fixture", "conversation_id": "fixture",
                                               "session_dir": str(self.root), "session_file": "fixture"}
                self.hub.profiles[engine]["args"] = ["--changed-after-spawn"]
                resumed = self.hub.build_resume_launch(record)
                self.assertEqual(resumed["args"].count("--custom-option"), 1)
                self.assertIn(extra[1], resumed["args"])
                self.assertIn(extra[2], resumed["args"])
                self.assertNotIn("--changed-after-spawn", resumed["args"])

    async def test_omp_starts_with_its_approval_event_log(self):
        """omp loads the hub's approval event extension once and is told where to write."""
        started = {}
        with patch.object(AgentSession, "start_pty", lambda s, loop, env_overrides=None: started.update(env_overrides or {})), \
                patch.object(self.hub, "_schedule", lambda coro: coro.close()):
            sess = await self.hub.create_session("omp", task="fixture task", cwd=str(self.root))
        extension = [a for a in sess.args if a.startswith("--extension=")]
        self.assertEqual(len(extension), 1)
        self.assertTrue(extension[0].endswith(OMP_APPROVAL_EXTENSION_NAME))
        self.assertEqual(started[OMP_APPROVAL_LOG_ENV], str(self.hub._omp_approval_log_path(sess)))
        await self.hub.db.flush()
        record = await self.hub.db.get_session_record(sess.session_id)
        record["conversation_ref"] = {"session_id": "fixture", "session_file": "fixture"}
        resumed = self.hub.build_resume_launch(record)
        self.assertEqual(sum(a.startswith("--extension=") for a in resumed["args"]), 1)

    async def test_non_cli_and_invalid_input_rejected_before_spawn(self):
        for profile, kwargs in (("dsh", {}), ("desktop-gui", {"hwnd": 123})):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                await self.hub.create_session(profile, extra_args=["--flag"], **kwargs)
        with self.assertRaises(ValueError):
            await self.hub.create_session("codex", extra_args="--flag value")
        self.assertEqual(self.hub.sessions, {})
        self.hub.profiles["pwsh"]["kind"] = "web_gui"
        with self.assertRaises(ValueError):
            self.hub.resolve_launch("pwsh", extra_args=["--flag"])

    async def test_http_and_mcp_forward_same_argv(self):
        extra = ["-c", "mcp_servers.hidden.enabled=false"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as client:
            preview = await client.post("/api/launch-preview", json={"profile_id": "codex", "extra_args": extra})
            self.assertTrue(preview.json()["ok"])
            self.assertEqual(preview.json()["args"][-2:], extra)
            bad = await client.post("/api/sessions", json={"profile_id": "codex", "extra_args": [1]})
            self.assertEqual(bad.status_code, 400)
            bad_preview = await client.post("/api/launch-preview", json={"profile_id": "dsh", "extra_args": extra})
            self.assertFalse(bad_preview.json()["ok"])
            def no_watchers(coro):
                coro.close()
            with patch.object(AgentSession, "start_pty"), patch.object(self.hub, "_schedule", no_watchers):
                response = await client.post("/api/sessions", json={"profile_id": "codex", "extra_args": extra})
                self.assertEqual(response.status_code, 200)
                async with Client(self.server.mcp) as mcp_client:
                    schema = next(t for t in await mcp_client.list_tools() if t.name == "spawn_subagent").input_schema
                    self.assertIn("extra_args", schema["properties"])
                    result = await mcp_client.call_tool("spawn_subagent", {"agent_type": "codex", "extra_args": extra, "wait_seconds": 0})
                    data = result.data if result.data is not None else json.loads(result.content[0].text)
                    self.assertIn(data["session_id"], self.hub.sessions)
        for sess in self.hub.sessions.values():
            self.assertEqual(sess.profile_snapshot["extra_args"], extra)
            self.assertEqual(sess.args[-2:], extra)


if __name__ == "__main__":
    unittest.main()
