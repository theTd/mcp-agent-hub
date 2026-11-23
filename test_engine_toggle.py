"""Engine-level enable switch: persistence, default-on, launch/MCP/archive filtering and HTTP validation."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from session_manager import HubSessionManager

DISCOVERY = {
    "engines": {
        "codex": {
            "engine": "codex", "name": "Codex", "installed": True, "binary": "codex",
            "available_models": ["gpt-5", "gpt-4"], "default_model": "gpt-5",
            "available_thinking_levels": ["low", "high"],
        },
        "claude": {
            "engine": "claude", "name": "Claude Code", "installed": True, "binary": "claude",
            "available_models": ["claude-a"], "default_model": "claude-a",
            "available_thinking_levels": ["low", "high"],
        },
        "doubao": {
            "engine": "doubao", "name": "豆包 CLI", "installed": True, "binary": "doubao",
            "available_models": ["9", "seed-lite-7b"], "default_model": "9",
            "default_thinking_level": "high", "available_thinking_levels": ["low", "medium", "high"],
            "model_details": [
                {"id": "9", "display_name": "Auto", "supported_thinking_levels": ["low", "medium", "high"],
                 "default_thinking_level": "high"},
            ],
        },
    }
}


class EngineToggleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-engine-toggle-")
        self.root = Path(self.temp.name)
        self.profiles = self.root / "agent_profiles.json"
        self._write_profiles({
            "profiles": {
                "codex": {"engine": "codex", "enabled": True, "default_model": "gpt-5"},
                "claude": {"engine": "claude", "enabled": True, "default_model": "claude-a"},
            },
            "engine_policies": {"codex": {"disabled_models": ["gpt-4"], "model_notes": {"gpt-5": "fast"}}},
            "global_routing_instructions": "keep me",
            "subagent_idle_timeout_minutes": 5,
        })
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(self.profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.hub.load_profiles(discovery=DISCOVERY)

        os.environ["HUB_DATA_DIR"] = str(self.root)
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")
        self.hub_patch = patch.object(self.server, "hub", self.hub)
        self.hub_patch.start()

    async def asyncTearDown(self):
        self.hub_patch.stop()
        self.temp.cleanup()

    def _write_profiles(self, data):
        self.profiles.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _read_profiles(self):
        return json.loads(self.profiles.read_text(encoding="utf-8"))

    def test_doubao_engine_toggles_and_persists(self):
        self.assertTrue(self.hub.engine_enabled("doubao"))
        self.assertIn("doubao", self.hub.get_enabled_profiles())
        self.hub.set_engine_enabled("doubao", False)
        self.assertFalse(self.hub.engine_enabled("doubao"))
        self.assertFalse(self.hub.engines_catalog["doubao"]["enabled"])
        self.assertNotIn("doubao", self.hub.get_enabled_profiles())
        self.hub.set_engine_enabled("doubao", True)
        self.assertTrue(self.hub.engine_enabled("doubao"))
        launch = self.hub.resolve_launch("doubao", cwd=str(self.root))
        self.assertEqual(launch["engine"], "doubao")

    def test_default_is_enabled_and_catalog_reports_it(self):
        self.assertTrue(self.hub.engine_enabled("codex"))
        self.assertTrue(self.hub.engines_catalog["codex"]["enabled"])
        self.assertIn("codex", self.hub.get_enabled_profiles())
        self.hub.load_profiles(discovery=DISCOVERY)
        self.assertTrue(self.hub.engine_enabled("codex"))

        self._write_profiles({"profiles": {"codex": {"engine": "codex", "enabled": True}}})
        self.hub.load_profiles(discovery=DISCOVERY)
        self.assertTrue(self.hub.engine_enabled("codex"))
        self.assertTrue(self.hub.engines_catalog["codex"]["enabled"])
        self.assertIn("codex", self.hub.get_enabled_profiles())

    def test_toggle_persists_and_preserves_model_policy(self):
        info = self.hub.set_engine_enabled("codex", False)
        self.assertFalse(info["enabled"])
        self.assertFalse(self.hub.engine_enabled("codex"))
        self.assertFalse(self.hub.engines_catalog["codex"]["enabled"])

        saved = self._read_profiles()
        policy = saved["engine_policies"]["codex"]
        self.assertIs(policy["enabled"], False)
        self.assertEqual(policy["disabled_models"], ["gpt-4"])
        self.assertEqual(policy["model_notes"], {"gpt-5": "fast"})
        self.assertEqual(saved["global_routing_instructions"], "keep me")
        self.assertEqual(saved["subagent_idle_timeout_minutes"], 5)

        with self.assertRaisesRegex(ValueError, "布尔值"):
            self.hub.set_engine_enabled("codex", 0)
        self.assertIs(self._read_profiles()["engine_policies"]["codex"]["enabled"], False)
        self.assertFalse(self.hub.engine_enabled("codex"))

        self.hub.set_engine_enabled("codex", True)
        self.assertTrue(self.hub.engine_enabled("codex"))
        self.assertIs(self._read_profiles()["engine_policies"]["codex"]["enabled"], True)
        self.assertEqual(self._read_profiles()["engine_policies"]["codex"]["disabled_models"], ["gpt-4"])

    def test_disabled_engine_is_filtered_from_mcp_routing_and_launch(self):
        self.hub.set_engine_enabled("codex", False)

        self.assertNotIn("codex", self.hub.get_enabled_profiles())
        self.assertIn("claude", self.hub.get_enabled_profiles())
        self.assertNotIn("codex", self.hub.get_mcp_engines_catalog())
        self.assertNotIn("codex", self.hub.get_mcp_enabled_profiles())

        guide = self.hub.build_routing_guide_markdown()
        self.assertNotIn("`codex`", guide)
        self.assertNotIn("### codex", guide)
        self.assertIn("`claude`", guide)
        self.assertNotIn("`codex`", self.hub.build_routing_only_markdown())

        with self.assertRaisesRegex(ValueError, "已停用"):
            self.hub.resolve_launch("codex", cwd=str(self.root))

    def test_disabled_engine_blocks_archive_resume_but_keeps_live_sessions(self):
        live = object()
        self.hub.sessions["codex-live"] = live
        record = {
            "session_id": "codex-old",
            "engine": "codex",
            "profile_snapshot": {"profile_id": "codex", "cwd": str(self.root)},
            "conversation_ref": {"session_id": "abc", "rollout_path": str(self.root / "rollout.jsonl")},
        }
        self.hub.set_engine_enabled("codex", False)

        ok, reason = self.hub.compute_resumable(record)
        self.assertFalse(ok)
        self.assertIn("已停用", reason)
        with self.assertRaisesRegex(ValueError, "已停用"):
            self.hub.build_resume_launch(record)
        self.assertIs(self.hub.sessions["codex-live"], live)

    def test_old_or_malformed_policy_shapes_default_to_enabled(self):
        profiles = {
            "profiles": {"codex": {"engine": "codex", "enabled": True}},
            "engine_policies": [],
        }
        self._write_profiles(profiles)
        self.hub.load_profiles(discovery=DISCOVERY)
        self.assertTrue(self.hub.engine_enabled("codex"))
        self.assertTrue(self.hub.engines_catalog["codex"]["enabled"])

        profiles["engine_policies"] = {"codex": {"enabled": "no", "disabled_models": ["gpt-4"]}}
        self._write_profiles(profiles)
        self.hub.load_profiles(discovery=DISCOVERY)
        self.assertTrue(self.hub.engine_enabled("codex"))
        self.assertEqual(self.hub.engines_catalog["codex"]["disabled_models"], ["gpt-4"])

        info = self.hub.set_engine_enabled("codex", False)
        self.assertIs(info["enabled"], False)
        self.assertEqual(self._read_profiles()["engine_policies"]["codex"]["disabled_models"], ["gpt-4"])

    async def test_toggle_api_rejects_bad_input_and_accepts_bool(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            for body in ({}, {"enabled": "false"}, {"enabled": 1}, {"enabled": None}, []):
                response = await client.post("/api/engines/codex/toggle", json=body)
                self.assertEqual(response.status_code, 400, body)
                self.assertTrue(self.hub.engine_enabled("codex"))

            response = await client.post("/api/engines/nope/toggle", json={"enabled": False})
            self.assertEqual(response.status_code, 400)
            self.assertIn("未知引擎", response.json()["detail"])

            response = await client.post("/api/engines/codex/toggle", json={"enabled": False})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertIs(payload["engine_info"]["enabled"], False)
            self.assertFalse(payload["engines_catalog"]["codex"]["enabled"])
            self.assertEqual(self._read_profiles()["engine_policies"]["codex"]["disabled_models"], ["gpt-4"])

            response = await client.post("/api/engines/codex/toggle", json={"enabled": True})
            self.assertEqual(response.status_code, 200)
            self.assertIs(response.json()["engine_info"]["enabled"], True)
