"""Per-session auto-compaction threshold: parsing, per-engine translation, rejection and resume; no model calls."""
import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastmcp import Client

from compaction_threshold import normalize_compaction_threshold, omp_compaction_overlay_text
from session_manager import AgentSession, HubSessionManager


class NormalizeTests(unittest.TestCase):
    def test_percent_and_token_forms(self):
        cases = {
            "70%": {"unit": "percent", "value": 70},
            " 5 % ": {"unit": "percent", "value": 5},
            200000: {"unit": "tokens", "value": 200000},
            200000.0: {"unit": "tokens", "value": 200000},
            "150000": {"unit": "tokens", "value": 150000},
            "200k": {"unit": "tokens", "value": 200000},
            "1M": {"unit": "tokens", "value": 1000000},
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_compaction_threshold(value), expected)
        self.assertIsNone(normalize_compaction_threshold(None))
        self.assertIsNone(normalize_compaction_threshold("  "))

    def test_rejects_ambiguous_and_out_of_range(self):
        for value in (True, 1.5, "abc", "70 percent", "0%", "100%", "-5%", 999, "50", [70], {"percent": 70}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_compaction_threshold(value)
        with self.assertRaises(ValueError) as caught:
            normalize_compaction_threshold(70)
        self.assertIn('"70%"', str(caught.exception))

    def test_omp_overlay_turns_off_per_model_thresholds(self):
        text = omp_compaction_overlay_text({"unit": "tokens", "value": 120000}, "omp-1234")
        body = json.loads(text.split("\n", 1)[1])
        self.assertEqual(body, {"compaction": {"enabled": True, "thresholdPercent": -1,
                                               "thresholdTokens": 120000, "modelThresholdsEnabled": False}})
        self.assertTrue(text.startswith("# Written by mcp-agent-hub for session omp-1234"))


class CompactionLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-compaction-")
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
                              "agy": "agy_interactive_flag", "omp": "omp_positional",
                              "dsh": "dsh_hub_api"}.get(engine, "pty_input"),
            }

    async def asyncTearDown(self):
        self.hub_patch.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def _spawn(self, engine, **kwargs):
        with patch.object(AgentSession, "start_pty"), patch.object(self.hub, "_schedule", lambda coro: coro.close()):
            return await self.hub.create_session(engine, task="fixture task", cwd=str(self.root), **kwargs)

    def test_codex_and_claude_translation(self):
        codex = self.hub.resolve_launch("codex", cwd=str(self.root), compaction_threshold="200k")
        index = codex["args"].index("model_auto_compact_token_limit=200000")
        self.assertEqual(codex["args"][index - 1], "-c")
        self.assertEqual(codex["compaction_threshold"], {"unit": "tokens", "value": 200000})
        self.assertTrue(any("200000 tokens" in note for note in codex["notes"]))

        self.hub.profiles["claude"]["env"] = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "90"}
        claude = self.hub.resolve_launch("claude", cwd=str(self.root), compaction_threshold="60%")
        self.assertEqual(claude["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "60")
        self.assertNotIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", self.hub.resolve_launch("codex", cwd=str(self.root))["env"])

    def test_unsupported_engines_units_and_conflicts_rejected(self):
        for engine in ("kimi", "agy", "dsh", "pwsh"):
            with self.subTest(engine=engine), self.assertRaises(ValueError):
                self.hub.resolve_launch(engine, cwd=str(self.root), compaction_threshold="70%")
        with self.assertRaises(ValueError):
            self.hub.resolve_launch("claude", cwd=str(self.root), compaction_threshold=200000)
        with self.assertRaises(ValueError):
            self.hub.resolve_launch("codex", cwd=str(self.root), compaction_threshold="70%")
        with self.assertRaises(ValueError):
            self.hub.resolve_launch("codex", cwd=str(self.root), compaction_threshold=200000,
                                    extra_args=["-c", "model_auto_compact_token_limit=1000"])
        with self.assertRaises(ValueError):
            self.hub.resolve_launch("claude", cwd=str(self.root), compaction_threshold="70%",
                                    env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"})
        # Unset stays a no-op everywhere, including engines without support.
        self.assertIsNone(self.hub.resolve_launch("kimi", cwd=str(self.root), compaction_threshold=None)["compaction_threshold"])

    async def test_rejections_leave_no_session(self):
        for engine, kwargs in (("kimi", {}), ("dsh", {}), ("desktop-gui", {"hwnd": 123})):
            with self.subTest(engine=engine), self.assertRaises(ValueError):
                await self.hub.create_session(engine, compaction_threshold="70%", **kwargs)
        self.assertEqual(self.hub.sessions, {})
        self.assertEqual(list(self.hub.runtime_configs_dir.glob("omp_compaction_*")), [])

    async def test_omp_overlay_written_snapshotted_and_resumed(self):
        sess = await self._spawn("omp", compaction_threshold="65%")
        overlay = self.hub.runtime_configs_dir / f"omp_compaction_{sess.session_id}.yml"
        self.assertTrue(overlay.is_file())
        body = json.loads(overlay.read_text(encoding="utf-8").split("\n", 1)[1])
        self.assertEqual(body["compaction"]["thresholdPercent"], 65)
        self.assertEqual(body["compaction"]["thresholdTokens"], -1)
        flag = f"--config={overlay}"
        self.assertEqual(sess.args.count(flag), 1)
        self.assertLess(sess.args.index(flag), sess.args.index(next(a for a in sess.args if "fixture task" in a)))

        await self.hub.db.flush()
        record = await self.hub.db.get_session_record(sess.session_id)
        self.assertIn(flag, record["profile_snapshot"]["args"])
        self.assertEqual(record["profile_snapshot"]["compaction_threshold"], {"unit": "percent", "value": 65})
        record["conversation_ref"] = {"session_dir": str(self.root), "session_file": "fixture"}
        self.hub.profiles["omp"]["args"] = ["--changed-after-spawn"]
        resumed = self.hub.build_resume_launch(record)
        self.assertEqual(resumed["args"].count(flag), 1)

    async def test_codex_and_claude_settings_survive_resume(self):
        codex = await self._spawn("codex", compaction_threshold=150000)
        claude = await self._spawn("claude", compaction_threshold="55%")
        await self.hub.db.flush()
        codex_record = await self.hub.db.get_session_record(codex.session_id)
        codex_record["conversation_ref"] = {"session_id": "fixture"}
        self.assertIn("model_auto_compact_token_limit=150000", self.hub.build_resume_launch(codex_record)["args"])
        claude_record = await self.hub.db.get_session_record(claude.session_id)
        claude_record["conversation_ref"] = {"session_id": "fixture"}
        self.assertEqual(self.hub.build_resume_launch(claude_record)["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "55")

    async def test_http_and_mcp_forward_the_threshold(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as client:
            preview = await client.post("/api/launch-preview",
                                        json={"profile_id": "codex", "compaction_threshold": "300k"})
            self.assertTrue(preview.json()["ok"])
            self.assertIn("model_auto_compact_token_limit=300000", preview.json()["args"])
            rejected = await client.post("/api/launch-preview", json={"profile_id": "kimi", "compaction_threshold": "70%"})
            self.assertFalse(rejected.json()["ok"])
            bad = await client.post("/api/sessions", json={"profile_id": "kimi", "compaction_threshold": "70%"})
            self.assertEqual(bad.status_code, 400)
            with patch.object(AgentSession, "start_pty"), patch.object(self.hub, "_schedule", lambda coro: coro.close()):
                created = await client.post("/api/sessions", json={"profile_id": "omp", "compaction_threshold": "70%"})
                self.assertEqual(created.status_code, 200)
                async with Client(self.server.mcp) as mcp_client:
                    schema = next(t for t in await mcp_client.list_tools() if t.name == "spawn_subagent").input_schema
                    self.assertIn("compaction_threshold", schema["properties"])
                    result = await mcp_client.call_tool(
                        "spawn_subagent", {"agent_type": "omp", "compaction_threshold": 90000, "wait_seconds": 0})
                    data = result.data if result.data is not None else json.loads(result.content[0].text)
                    self.assertEqual(self.hub.sessions[data["session_id"]].profile_snapshot["compaction_threshold"],
                                     {"unit": "tokens", "value": 90000})
                    batch = await mcp_client.call_tool(
                        "spawn_subagents", {"specs": [{"agent_type": "codex", "compaction_threshold": "120k"}]})
                    batch_data = batch.data if batch.data is not None else json.loads(batch.content[0].text)
                    codex_id = batch_data["sessions"][0]["session_id"]
                    self.assertIn("model_auto_compact_token_limit=120000", self.hub.sessions[codex_id].args)


if __name__ == "__main__":
    unittest.main()
