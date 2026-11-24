"""Aggregator model groups (`google/…`, `ollama-cloud/…`): recognition, persistence and batch toggles."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from session_manager import HubSessionManager, extract_model_group

DISCOVERY = {
    "engines": {
        "omp": {
            "engine": "omp", "name": "OMP", "installed": True, "binary": "omp",
            "available_models": [
                "google/gemini-3.1-pro", "google/gemini-3.0-flash",
                "ollama-cloud/llama-4", "ollama-cloud/qwen-3",
                "clay/kimi-k2",
                "gemini-4-argon",
            ],
            "default_model": "google/gemini-3.1-pro",
            "available_thinking_levels": ["low", "high"],
        },
        "kimi": {
            "engine": "kimi", "name": "Kimi Code", "installed": True, "binary": "kimi",
            "available_models": ["kimi-code/k3.1-pro", "kimi-code/k3-mini"],
            "default_model": "kimi-code/k3.1-pro",
            "available_thinking_levels": ["low", "high"],
        },
        "agy": {
            "engine": "agy", "name": "Antigravity", "installed": True, "binary": "agy",
            "available_models": ["gemini-4-argon", "gemini-4-argon-high", "gemini-4-pro"],
            "default_model": "gemini-4-argon",
            "available_thinking_levels": ["low", "medium", "high", "xhigh"],
        },
        "codex": {
            "engine": "codex", "name": "Codex", "installed": True, "binary": "codex",
            "available_models": ["gpt-5", "gpt-4"], "default_model": "gpt-5",
            "available_thinking_levels": ["low", "high"],
        },
    }
}

# The same engines after one more discovery run: a brand-new model shows up inside a known group.
DISCOVERY_WITH_NEW_MODEL = {
    "engines": {
        **DISCOVERY["engines"],
        "omp": {
            **DISCOVERY["engines"]["omp"],
            "available_models": DISCOVERY["engines"]["omp"]["available_models"]
            + ["google/gemini-3.2-ultra"],
        },
    }
}

# agy carries its thinking level in the id suffix, so a grouped agy model has NO bare base id in
# discovery: only `vendor/foo-low` / `vendor/foo-high`, which fold into the option `vendor/foo`.
AGY_SUFFIX_DISCOVERY = {
    "engines": {
        "agy": {
            "engine": "agy", "name": "Antigravity", "installed": True, "binary": "agy",
            "available_models": [
                "vendor/foo-low", "vendor/foo-high", "vendor/baz-medium",
                "other/bar-medium", "gemini-4-argon",
            ],
            "default_model": "vendor/foo",
            "available_thinking_levels": ["low", "medium", "high", "xhigh"],
        }
    }
}


class ExtractModelGroupTests(unittest.TestCase):
    def test_first_slash_only(self):
        cases = {
            "google/gemini-3.1-pro": "google",
            "ollama-cloud/llama-4": "ollama-cloud",
            "zenmux/anthropic/claude-sonnet-5": "zenmux",
            "cloudflare-ai-gateway/workers-ai/@cf/deepseek-ai/deepseek-v4": "cloudflare-ai-gateway",
            "kimi-code/k3.1-pro": "kimi-code",
            "  spaced/prefix  ": "spaced",
            "gemini-4-argon": "",
            "/foo": "",
            "foo/": "",
            "/": "",
            "": "",
        }
        for model_id, expected in cases.items():
            self.assertEqual(extract_model_group(model_id), expected, model_id)
        self.assertEqual(extract_model_group(None), "")


def count_io(hub):
    """Counts file reads, file writes and catalog reloads a mutation performs inside the `with` block."""
    calls = {"read": 0, "write": 0, "reload": 0}
    original_read = hub._read_profiles_file
    original_write = hub._write_profiles_file
    original_reload = hub._reload_after_write

    def read():
        calls["read"] += 1
        return original_read()

    def write(data):
        calls["write"] += 1
        return original_write(data)

    def reload(data):
        calls["reload"] += 1
        return original_reload(data)

    return calls, (
        patch.object(hub, "_read_profiles_file", side_effect=read),
        patch.object(hub, "_write_profiles_file", side_effect=write),
        patch.object(hub, "_reload_after_write", side_effect=reload),
    )


class ModelGroupToggleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-model-group-")
        self.root = Path(self.temp.name)
        self.profiles = self.root / "agent_profiles.json"
        self._write_profiles({
            "profiles": {
                "omp": {"engine": "omp", "enabled": True, "default_model": "google/gemini-3.1-pro"},
                "kimi": {"engine": "kimi", "enabled": True, "default_model": "kimi-code/k3.1-pro"},
                "agy": {"engine": "agy", "enabled": True, "default_model": "gemini-4-argon"},
                "codex": {"engine": "codex", "enabled": True, "default_model": "gpt-5"},
            },
            "engine_policies": {
                "omp": {"model_notes": {"google/gemini-3.0-flash": "cheap"}},
            },
        })
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(self.profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.hub.load_profiles(discovery=DISCOVERY)

        self._prev_hub_data_dir = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = str(self.root)
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")
        self.hub_patch = patch.object(self.server, "hub", self.hub)
        self.hub_patch.start()

    async def asyncTearDown(self):
        self.hub_patch.stop()
        if self._prev_hub_data_dir is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self._prev_hub_data_dir
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def _write_profiles(self, data):
        self.profiles.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _read_profiles(self):
        return json.loads(self.profiles.read_text(encoding="utf-8"))

    def _policy(self, engine):
        return (self._read_profiles().get("engine_policies") or {}).get(engine) or {}

    def _model(self, engine, model_id):
        return self.hub.engine_model_option(engine, model_id)

    def _count_io(self):
        """Counts file reads, file writes and catalog reloads performed inside the `with` block."""
        return count_io(self.hub)

    # ------------------------------------------------------------ catalog output
    def test_every_option_carries_its_group(self):
        for engine, expected in (
            ("omp", {"google/gemini-3.1-pro": "google", "ollama-cloud/llama-4": "ollama-cloud",
                     "gemini-4-argon": ""}),
            ("kimi", {"kimi-code/k3.1-pro": "kimi-code"}),
            ("codex", {"gpt-5": ""}),
        ):
            for model_id, group in expected.items():
                option = self._model(engine, model_id)
                self.assertIsNotNone(option, (engine, model_id))
                self.assertEqual(option["group"], group, (engine, model_id))

    def test_model_groups_summary_counts_and_order(self):
        groups = {g["name"]: g for g in self.hub.engines_catalog["omp"]["model_groups"]}
        self.assertEqual(set(groups), {"google", "ollama-cloud", "clay"})
        self.assertEqual(groups["google"]["total"], 2)
        self.assertEqual(groups["google"]["enabled_count"], 2)
        self.assertIs(groups["google"]["enabled"], True)
        self.assertIs(groups["google"]["partial"], False)
        self.assertIs(groups["google"]["group_disabled"], False)
        self.assertEqual(groups["clay"]["total"], 1)

        # All three groups start fully enabled, so discovery order decides: google, ollama-cloud, clay.
        self.assertEqual([g["name"] for g in self.hub.engines_catalog["omp"]["model_groups"]],
                         ["google", "ollama-cloud", "clay"])

    def test_ungrouped_engine_reports_no_groups(self):
        self.assertEqual(self.hub.engines_catalog["codex"]["model_groups"], [])
        self.assertEqual(self.hub.engines_catalog["agy"]["model_groups"], [])
        # agy keeps its suffixed variants folded into base ids, and a base id has no group.
        self.assertTrue(all(m["group"] == "" for m in self.hub.engines_catalog["agy"]["models"]))

    def test_single_group_engine(self):
        groups = self.hub.engines_catalog["kimi"]["model_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "kimi-code")
        self.assertEqual(groups[0]["total"], 2)
        self.assertIs(groups[0]["enabled"], True)

    # ----------------------------------------------------------- group toggling
    def test_group_off_persists_group_and_stays_compact(self):
        calls, (read_patch, write_patch, reload_patch) = self._count_io()
        with read_patch, write_patch, reload_patch:
            result = self.hub.toggle_engine_model_group("omp", "google", False)
        self.assertEqual(calls, {"read": 1, "write": 1, "reload": 1})
        self.assertTrue(result["ok"])
        self.assertEqual(result["group"], "google")
        self.assertIs(result["enabled"], False)
        self.assertEqual(result["affected_count"], 2)
        self.assertIsNotNone(result["engine_info"])

        policy = self._policy("omp")
        self.assertEqual(policy["disabled_model_groups"], ["google"])
        # The group policy carries the models: no per-model ids are written for it.
        self.assertEqual(policy.get("disabled_models") in (None, []), True)
        self.assertNotIn("enabled_models", policy)
        # A note on a model inside the group survives the group toggle.
        self.assertEqual(policy["model_notes"], {"google/gemini-3.0-flash": "cheap"})

        google = [m for m in self.hub.engines_catalog["omp"]["models"] if m["group"] == "google"]
        self.assertTrue(google)
        self.assertFalse(any(m["enabled"] for m in google))
        summary = next(g for g in self.hub.engines_catalog["omp"]["model_groups"]
                       if g["name"] == "google")
        self.assertEqual(summary["enabled_count"], 0)
        self.assertEqual(summary["disabled_count"], 2)
        self.assertIs(summary["group_disabled"], True)
        self.assertIn("google", self.hub.engines_catalog["omp"]["disabled_model_groups"])
        # A fully-off group sinks below the ones that still have an enabled model.
        self.assertEqual([g["name"] for g in self.hub.engines_catalog["omp"]["model_groups"]],
                         ["ollama-cloud", "clay", "google"])

    def test_newly_discovered_model_in_disabled_group_is_off(self):
        self.hub.toggle_engine_model_group("omp", "google", False)
        self.hub.load_profiles(discovery=DISCOVERY_WITH_NEW_MODEL)
        newcomer = self._model("omp", "google/gemini-3.2-ultra")
        self.assertIsNotNone(newcomer)
        self.assertIs(newcomer["enabled"], False)
        # …and it is not written into the file: the group name already covers it.
        self.assertEqual(self._policy("omp").get("disabled_models") in (None, []), True)

    def test_cherry_pick_inside_a_disabled_group(self):
        self.hub.toggle_engine_model_group("omp", "google", False)
        self.hub.toggle_engine_model("omp", "google/gemini-3.1-pro", enabled=True)

        policy = self._policy("omp")
        self.assertEqual(policy["disabled_model_groups"], ["google"])
        self.assertEqual(policy["enabled_models"], ["google/gemini-3.1-pro"])
        self.assertEqual(policy.get("disabled_models") in (None, []), True)

        self.assertTrue(self._model("omp", "google/gemini-3.1-pro")["enabled"])
        self.assertFalse(self._model("omp", "google/gemini-3.0-flash")["enabled"])
        summary = next(g for g in self.hub.engines_catalog["omp"]["model_groups"]
                       if g["name"] == "google")
        self.assertEqual(summary["enabled_count"], 1)
        self.assertIs(summary["partial"], True)
        self.assertIs(summary["group_disabled"], True)

        # A model discovered later stays off unless explicitly allow-listed.
        self.hub.load_profiles(discovery=DISCOVERY_WITH_NEW_MODEL)
        self.assertFalse(self._model("omp", "google/gemini-3.2-ultra")["enabled"])
        self.assertTrue(self._model("omp", "google/gemini-3.1-pro")["enabled"])

        # Toggling the cherry-picked model off drops it from the allow-list (the group still holds it off).
        self.hub.toggle_engine_model("omp", "google/gemini-3.1-pro", enabled=False)
        self.assertNotIn("enabled_models", self._policy("omp"))
        self.assertFalse(self._model("omp", "google/gemini-3.1-pro")["enabled"])

    def test_group_on_clears_group_and_leftovers(self):
        self.hub.toggle_engine_model_group("omp", "google", False)
        self.hub.toggle_engine_model("omp", "google/gemini-3.0-flash", enabled=True)
        self.hub.toggle_engine_model_group("omp", "google", True)

        policy = self._policy("omp")
        self.assertNotIn("disabled_model_groups", policy)
        self.assertNotIn("enabled_models", policy)
        self.assertEqual(policy.get("disabled_models") in (None, []), True)
        self.assertTrue(all(m["enabled"] for m in self.hub.engines_catalog["omp"]["models"]
                            if m["group"] == "google"))

    def test_group_toggle_validation(self):
        with self.assertRaisesRegex(ValueError, "布尔值"):
            self.hub.toggle_engine_model_group("omp", "google", "false")
        with self.assertRaisesRegex(ValueError, "分组名"):
            self.hub.toggle_engine_model_group("omp", "  ", False)
        with self.assertRaisesRegex(ValueError, "没有模型分组"):
            self.hub.toggle_engine_model_group("omp", "nope", False)
        with self.assertRaisesRegex(ValueError, "未知引擎"):
            self.hub.toggle_engine_model_group("nope", "google", False)
        self.assertEqual(self.hub.engines_catalog["omp"]["disabled_model_groups"], [])

    def test_stale_group_can_be_cleared_even_with_no_models(self):
        data = self._read_profiles()
        data["engine_policies"]["omp"]["disabled_model_groups"] = ["gone-vendor"]
        self._write_profiles(data)
        self.hub.load_profiles(discovery=DISCOVERY)
        self.assertEqual(self.hub.engines_catalog["omp"]["disabled_model_groups"], ["gone-vendor"])

        result = self.hub.toggle_engine_model_group("omp", "gone-vendor", True)
        self.assertEqual(result["affected_count"], 0)
        self.assertNotIn("disabled_model_groups", self._policy("omp"))

    def test_cherry_picked_model_absent_from_discovery_is_forgotten_when_group_goes_off(self):
        self.hub.toggle_engine_model_group("omp", "google", False)
        # A new google model shows up and the user cherry-picks it inside the disabled group.
        self.hub.load_profiles(discovery=DISCOVERY_WITH_NEW_MODEL)
        self.hub.toggle_engine_model("omp", "google/gemini-3.2-ultra", enabled=True)
        self.assertEqual(self._policy("omp")["enabled_models"], ["google/gemini-3.2-ultra"])

        # It disappears from discovery for a while; the allow-list entry is still on disk.
        self.hub.load_profiles(discovery=DISCOVERY)
        self.assertEqual(self._policy("omp")["enabled_models"], ["google/gemini-3.2-ultra"])

        # Turning the group off again must forget it instead of arming it for the next discovery.
        self.hub.toggle_engine_model_group("omp", "google", False)
        policy = self._policy("omp")
        self.assertNotIn("enabled_models", policy)
        self.assertEqual(policy["disabled_model_groups"], ["google"])

        self.hub.load_profiles(discovery=DISCOVERY_WITH_NEW_MODEL)
        self.assertFalse(self._model("omp", "google/gemini-3.2-ultra")["enabled"])

    # ---------------------------------------------------------------- batch API
    def test_batch_disable_is_one_write_and_compacts_groups(self):
        calls, (read_patch, write_patch, reload_patch) = self._count_io()
        with read_patch, write_patch, reload_patch:
            result = self.hub.toggle_engine_models_batch(
                "omp", ["google/gemini-3.1-pro", "google/gemini-3.0-flash"], False
            )
        self.assertEqual(calls, {"read": 1, "write": 1, "reload": 1})
        self.assertEqual(result["count"], 2)
        self.assertFalse(self._model("omp", "google/gemini-3.1-pro")["enabled"])
        self.assertEqual(self._policy("omp")["disabled_models"],
                         ["google/gemini-3.1-pro", "google/gemini-3.0-flash"])

    def test_batch_enable_all_clears_disabled_group(self):
        self.hub.toggle_engine_model_group("omp", "google", False)
        self.hub.toggle_engine_model("omp", "google/gemini-3.1-pro", enabled=True)
        google_ids = [m["id"] for m in self.hub.engines_catalog["omp"]["models"] if m["group"] == "google"]

        calls, (read_patch, write_patch, reload_patch) = self._count_io()
        with read_patch, write_patch, reload_patch:
            result = self.hub.toggle_engine_models_batch("omp", google_ids, True)
        self.assertEqual(calls, {"read": 1, "write": 1, "reload": 1})
        self.assertEqual(result["count"], len(google_ids))
        self.assertNotIn("disabled_model_groups", self._policy("omp"))
        self.assertNotIn("enabled_models", self._policy("omp"))
        self.assertTrue(all(m["enabled"] for m in self.hub.engines_catalog["omp"]["models"]
                            if m["group"] == "google"))

    def test_batch_validation(self):
        with self.assertRaisesRegex(ValueError, "布尔值"):
            self.hub.toggle_engine_models_batch("omp", ["google/gemini-3.1-pro"], 1)
        with self.assertRaisesRegex(ValueError, "模型标识的数组"):
            self.hub.toggle_engine_models_batch("omp", "google/gemini-3.1-pro", True)
        with self.assertRaisesRegex(ValueError, "缺少模型标识"):
            self.hub.toggle_engine_models_batch("omp", ["  "], True)
        with self.assertRaisesRegex(ValueError, "没有模型"):
            self.hub.toggle_engine_models_batch("omp", ["google/nope"], True)
        with self.assertRaisesRegex(ValueError, "未知引擎"):
            self.hub.toggle_engine_models_batch("nope", ["gpt-5"], True)

    def test_batch_dedupes_models(self):
        result = self.hub.toggle_engine_models_batch(
            "omp", ["google/gemini-3.1-pro", "google/gemini-3.1-pro", "google/gemini-3.1-pro"], False
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(self._policy("omp")["disabled_models"], ["google/gemini-3.1-pro"])

    def test_partial_batch_enable_keeps_group_and_writes_allow_list(self):
        self.hub.toggle_engine_model_group("omp", "google", False)

        calls, (read_patch, write_patch, reload_patch) = self._count_io()
        with read_patch, write_patch, reload_patch:
            result = self.hub.toggle_engine_models_batch("omp", ["google/gemini-3.1-pro"], True)
        self.assertEqual(calls, {"read": 1, "write": 1, "reload": 1})
        self.assertEqual(result["count"], 1)

        policy = self._policy("omp")
        # A subset of the group is a cherry-pick, not a whole-group enable.
        self.assertEqual(policy["disabled_model_groups"], ["google"])
        self.assertEqual(policy["enabled_models"], ["google/gemini-3.1-pro"])
        self.assertEqual(policy.get("disabled_models") in (None, []), True)
        self.assertTrue(self._model("omp", "google/gemini-3.1-pro")["enabled"])
        self.assertFalse(self._model("omp", "google/gemini-3.0-flash")["enabled"])

    def test_partial_batch_disable_keeps_group_policy(self):
        self.hub.toggle_engine_model_group("omp", "google", False)
        self.hub.toggle_engine_models_batch(
            "omp", ["google/gemini-3.1-pro", "google/gemini-3.0-flash"], False
        )
        policy = self._policy("omp")
        self.assertEqual(policy["disabled_model_groups"], ["google"])
        self.assertNotIn("enabled_models", policy)
        # The group policy already holds the models: no per-model ids are written.
        self.assertEqual(policy.get("disabled_models") in (None, []), True)

    def test_batch_toggle_keeps_model_notes(self):
        for enabled in (False, True):
            self.hub.toggle_engine_models_batch("omp", ["google/gemini-3.0-flash"], enabled)
            self.assertEqual(self._policy("omp").get("model_notes"),
                             {"google/gemini-3.0-flash": "cheap"}, enabled)

    def test_batch_enable_all_clears_stale_empty_group(self):
        data = self._read_profiles()
        data["engine_policies"]["omp"]["disabled_model_groups"] = ["gone-vendor"]
        data["engine_policies"]["omp"]["enabled_models"] = ["gone-vendor/old-model"]
        self._write_profiles(data)
        self.hub.load_profiles(discovery=DISCOVERY)
        self.assertEqual(self.hub.engines_catalog["omp"]["disabled_model_groups"], ["gone-vendor"])

        # A partial batch leaves a stale group alone…
        self.hub.toggle_engine_models_batch("omp", ["google/gemini-3.1-pro"], True)
        self.assertEqual(self._policy("omp")["disabled_model_groups"], ["gone-vendor"])

        # …while 「全部启用」 (every discovered option) retires it and its stale allow-list entry.
        all_ids = [m["id"] for m in self.hub.engines_catalog["omp"]["models"]]
        self.hub.toggle_engine_models_batch("omp", all_ids, True)
        policy = self._policy("omp")
        self.assertNotIn("disabled_model_groups", policy)
        self.assertNotIn("enabled_models", policy)

    # ------------------------------------------------------- downstream filtering
    def test_disabled_group_is_filtered_everywhere(self):
        self.hub.toggle_engine_model_group("omp", "google", False)

        mcp = self.hub.get_mcp_engines_catalog()
        self.assertIn("omp", mcp)
        self.assertNotIn("google/gemini-3.1-pro", mcp["omp"]["available_models"])
        self.assertNotIn("google/gemini-3.1-pro", [m["id"] for m in mcp["omp"]["models"]])
        self.assertIn("ollama-cloud/llama-4", mcp["omp"]["available_models"])

        guide = self.hub.build_routing_guide_markdown()
        self.assertIn("ollama-cloud/llama-4", guide)
        # The model catalog section never offers a model of a disabled group.
        catalog_section = guide.split("## Available models and thinking levels")[1]
        self.assertNotIn("google/gemini-3.1-pro", catalog_section)
        self.assertNotIn("google/gemini-3.0-flash", catalog_section)

        self.assertIn("ollama-cloud/llama-4", self.hub.profiles["omp"]["available_models"])
        self.assertNotIn("google/gemini-3.1-pro", self.hub.profiles["omp"]["available_models"])
        self.assertNotIn("google/gemini-3.1-pro", self.hub.engines_catalog["omp"]["available_models"])

        # The profile default (a google model) is substituted, not silently launched.
        launch = self.hub.resolve_launch("omp", cwd=str(self.root))
        self.assertNotEqual(launch["model"], "google/gemini-3.1-pro")

    def test_all_models_of_an_engine_disabled_blocks_launch(self):
        for group in ("google", "ollama-cloud", "clay"):
            self.hub.toggle_engine_model_group("omp", group, False)
        self.hub.toggle_engine_model("omp", "gemini-4-argon", enabled=False)
        with self.assertRaisesRegex(ValueError, "模型都已停用"):
            self.hub.resolve_launch("omp", cwd=str(self.root))

    def test_cherry_picked_model_launches(self):
        self.hub.toggle_engine_model_group("omp", "google", False)
        self.hub.toggle_engine_model("omp", "google/gemini-3.1-pro", enabled=True)
        launch = self.hub.resolve_launch("omp", cwd=str(self.root), model="google/gemini-3.1-pro")
        self.assertEqual(launch["model"], "google/gemini-3.1-pro")

    def test_engine_default_model_stays_on_an_enabled_model(self):
        # Both the engine default and the canonical profile default are google models.
        self.assertEqual(self.hub.engines_catalog["omp"]["default_model"], "google/gemini-3.1-pro")
        self.hub.toggle_engine_model_group("omp", "google", False)

        info = self.hub.engines_catalog["omp"]
        self.assertNotIn(info["default_model"], ("google/gemini-3.1-pro", "google/gemini-3.0-flash"))
        self.assertIn(info["default_model"], info["available_models"])
        self.assertIs(self._model("omp", info["default_model"])["enabled"], True)

        # …and enabling the group back re-aligns the defaults on the canonical profile model.
        self.hub.toggle_engine_model_group("omp", "google", True)
        self.assertEqual(self.hub.engines_catalog["omp"]["default_model"], "google/gemini-3.1-pro")

    # ----------------------------------------------------------------- malformed
    def test_malformed_group_policy_is_tolerated(self):
        data = self._read_profiles()
        data["engine_policies"] = {
            "omp": {
                "disabled_model_groups": "google",
                "enabled_models": {"google/gemini-3.1-pro": True},
                "disabled_models": ["google/gemini-3.0-flash"],
            },
            "codex": {"disabled_model_groups": 7, "enabled_models": [1, "", "  ", None]},
        }
        self._write_profiles(data)
        self.hub.load_profiles(discovery=DISCOVERY)

        self.assertEqual(self.hub.engines_catalog["omp"]["disabled_model_groups"], [])
        self.assertEqual(len(self.hub.engines_catalog["omp"]["model_groups"]), 3)
        self.assertFalse(self._model("omp", "google/gemini-3.0-flash")["enabled"])
        self.assertTrue(self._model("omp", "google/gemini-3.1-pro")["enabled"])
        self.assertEqual(self.hub.engines_catalog["codex"]["disabled_model_groups"], [])
        self.assertFalse(any(m["enabled"] is False for m in self.hub.engines_catalog["codex"]["models"]))

        # A malformed list is cleaned up by the next mutation instead of crashing it.
        self.hub.toggle_engine_model_group("omp", "google", False)
        self.assertEqual(self._policy("omp")["disabled_model_groups"], ["google"])

    def test_non_dict_engine_policy_is_reset_instead_of_crashing(self):
        data = self._read_profiles()
        data["engine_policies"]["omp"] = "bad"
        data["engine_policies"]["kimi"] = 7
        self._write_profiles(data)
        self.hub.load_profiles(discovery=DISCOVERY)

        # Group toggle: previously an AttributeError (HTTP 500).
        result = self.hub.toggle_engine_model_group("omp", "google", False)
        self.assertEqual(result["affected_count"], 2)
        self.assertIsInstance(self._policy("omp"), dict)
        self.assertEqual(self._policy("omp")["disabled_model_groups"], ["google"])

        # Batch toggle on a second malformed engine.
        self.hub.toggle_engine_models_batch("kimi", ["kimi-code/k3.1-pro"], False)
        self.assertIsInstance(self._policy("kimi"), dict)
        self.assertEqual(self._policy("kimi")["disabled_models"], ["kimi-code/k3.1-pro"])

        # Single-model toggle, including a note on a malformed block, and its malformed notes map.
        data = self._read_profiles()
        data["engine_policies"]["omp"] = "bad"
        data["engine_policies"]["codex"] = {"model_notes": "nope"}
        self._write_profiles(data)
        self.hub.load_profiles(discovery=DISCOVERY)
        self.hub.toggle_engine_model("omp", "google/gemini-3.1-pro", enabled=False)
        self.assertEqual(self._policy("omp")["disabled_models"], ["google/gemini-3.1-pro"])
        self.hub.toggle_engine_model("codex", "gpt-5", note=" 主线 ")
        self.assertEqual(self._policy("codex")["model_notes"], {"gpt-5": "主线"})

    # -------------------------------------------------------------------- HTTP
    async def test_group_toggle_api(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            for body in ({}, {"group": "google", "enabled": "false"}, {"enabled": False},
                         {"group": "  ", "enabled": False}, []):
                response = await client.post("/api/engines/omp/model-groups/toggle", json=body)
                self.assertEqual(response.status_code, 400, body)
                self.assertEqual(self.hub.engines_catalog["omp"]["disabled_model_groups"], [])

            response = await client.post("/api/engines/omp/model-groups/toggle",
                                         json={"group": "nope", "enabled": False})
            self.assertEqual(response.status_code, 400)
            self.assertIn("没有模型分组", response.json()["detail"])

            response = await client.post("/api/engines/nope/model-groups/toggle",
                                         json={"group": "google", "enabled": False})
            self.assertEqual(response.status_code, 400)
            self.assertIn("未知引擎", response.json()["detail"])

            response = await client.post("/api/engines/omp/model-groups/toggle",
                                         json={"group": "google", "enabled": False})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertIs(payload["enabled"], False)
            self.assertEqual(payload["affected_count"], 2)
            self.assertEqual(payload["group"], "google")
            # Same response convention as /models/toggle: the console can re-render from this payload alone.
            self.assertEqual(payload["engines_catalog"]["omp"]["disabled_model_groups"], ["google"])
            self.assertNotIn("google/gemini-3.1-pro", payload["profiles"]["omp"]["available_models"])
            self.assertIn("routing_guide_markdown", payload)

            response = await client.post("/api/engines/omp/model-groups/toggle",
                                         json={"group": "google", "enabled": True})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("disabled_model_groups", self._policy("omp"))

    async def test_batch_toggle_api(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            for body in ({}, {"models": ["google/gemini-3.1-pro"], "enabled": "yes"},
                         {"models": "google/gemini-3.1-pro", "enabled": True},
                         {"models": [1, 2], "enabled": True}):
                response = await client.post("/api/engines/omp/models/toggle-batch", json=body)
                self.assertEqual(response.status_code, 400, body)

            response = await client.post("/api/engines/omp/models/toggle-batch",
                                         json={"models": ["google/nope"], "enabled": False})
            self.assertEqual(response.status_code, 400)
            self.assertIn("没有模型", response.json()["detail"])

            response = await client.post("/api/engines/omp/models/toggle-batch",
                                         json={"models": ["google/gemini-3.1-pro",
                                                          "google/gemini-3.0-flash"],
                                               "enabled": False})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["count"], 2)
            self.assertIs(payload["enabled"], False)
            self.assertEqual(payload["engines_catalog"]["omp"]["disabled_models"],
                             ["google/gemini-3.1-pro", "google/gemini-3.0-flash"])

            response = await client.post("/api/engines/omp/models/toggle-batch",
                                         json={"models": ["google/gemini-3.1-pro",
                                                          "google/gemini-3.0-flash"],
                                               "enabled": True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self._policy("omp").get("disabled_models") or [], [])

    async def test_batch_toggle_api_rejects_an_empty_models_list(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            response = await client.post("/api/engines/omp/models/toggle-batch",
                                         json={"models": [], "enabled": True})
            self.assertEqual(response.status_code, 400)
            self.assertIn("缺少模型标识", response.json()["detail"])
            self.assertEqual(self._policy("omp").get("disabled_models") or [], [])

    async def test_group_toggle_api_strips_a_padded_group_name(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            response = await client.post("/api/engines/omp/model-groups/toggle",
                                         json={"group": "  google  ", "enabled": False})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["group"], "google")
            self.assertEqual(payload["affected_count"], 2)
            self.assertEqual(self._policy("omp")["disabled_model_groups"], ["google"])

            response = await client.post("/api/engines/omp/model-groups/toggle",
                                         json={"group": " google ", "enabled": True})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("disabled_model_groups", self._policy("omp"))

    async def test_malformed_engine_policy_does_not_500_over_http(self):
        data = self._read_profiles()
        data["engine_policies"]["omp"] = "bad"
        self._write_profiles(data)
        self.hub.load_profiles(discovery=DISCOVERY)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            response = await client.post("/api/engines/omp/model-groups/toggle",
                                         json={"group": "google", "enabled": False})
            self.assertEqual(response.status_code, 200)
            self.assertIn("google", response.json()["engines_catalog"]["omp"]["disabled_model_groups"])

            response = await client.post("/api/engines/omp/models/toggle-batch",
                                         json={"models": ["google/gemini-3.1-pro",
                                                          "google/gemini-3.0-flash"],
                                               "enabled": True})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("disabled_model_groups", self._policy("omp"))


class AgySuffixGroupTests(unittest.IsolatedAsyncioTestCase):
    """agy folds its thinking suffix into the id, so a grouped option has no bare base id in discovery."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-agy-group-")
        self.root = Path(self.temp.name)
        self.profiles = self.root / "agent_profiles.json"
        self.profiles.write_text(json.dumps({
            "profiles": {"agy": {"engine": "agy", "enabled": True, "default_model": "vendor/foo"}},
        }, ensure_ascii=False), encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(self.profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.hub.load_profiles(discovery=AGY_SUFFIX_DISCOVERY)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def _policy(self):
        data = json.loads(self.profiles.read_text(encoding="utf-8"))
        return (data.get("engine_policies") or {}).get("agy") or {}

    def test_group_is_reported_from_the_folded_base_id(self):
        groups = {g["name"]: g for g in self.hub.engines_catalog["agy"]["model_groups"]}
        self.assertEqual(set(groups), {"vendor", "other"})
        self.assertEqual(groups["vendor"]["total"], 2)  # the folded options `vendor/foo`, `vendor/baz`
        self.assertEqual(groups["vendor"]["enabled_count"], 2)
        self.assertIs(groups["vendor"]["enabled"], True)

    def test_suffix_only_group_off_then_batch_enable_all_clears_it(self):
        result = self.hub.toggle_engine_model_group("agy", "vendor", False)
        self.assertEqual(result["affected_count"], 2)
        policy = self._policy()
        self.assertEqual(policy["disabled_model_groups"], ["vendor"])
        # Policy is keyed on the raw suffixed ids, exactly as discovery reports them.
        self.assertEqual(
            sorted(self.hub.engines_catalog["agy"]["disabled_models"]),
            ["vendor/baz-medium", "vendor/foo-high", "vendor/foo-low"],
        )
        self.assertEqual(policy.get("disabled_models") in (None, []), True)
        self.assertEqual(policy.get("enabled_models") in (None, []), True)
        self.assertIs(self.hub.engine_model_option("agy", "vendor/foo")["enabled"], False)
        self.assertIs(self.hub.engine_model_option("agy", "vendor/baz")["enabled"], False)

        option_ids = [m["id"] for m in self.hub.engines_catalog["agy"]["models"]]
        calls, (read_patch, write_patch, reload_patch) = count_io(self.hub)
        with read_patch, write_patch, reload_patch:
            batch = self.hub.toggle_engine_models_batch("agy", option_ids, True)
        self.assertEqual(calls, {"read": 1, "write": 1, "reload": 1})
        self.assertEqual(batch["count"], len(option_ids))

        policy = self._policy()
        # 「全部启用」 must retire the group even though only the suffixed ids were allow-listed.
        self.assertNotIn("disabled_model_groups", policy)
        self.assertNotIn("enabled_models", policy)
        self.assertEqual(self.hub.engines_catalog["agy"]["disabled_models"], [])
        self.assertIs(self.hub.engine_model_option("agy", "vendor/foo")["enabled"], True)
        groups = {g["name"]: g for g in self.hub.engines_catalog["agy"]["model_groups"]}
        self.assertIs(groups["vendor"]["enabled"], True)

    def test_group_on_clears_suffixed_allow_entries(self):
        self.hub.toggle_engine_model_group("agy", "vendor", False)
        # A cherry-pick of one folded option records its raw suffixed ids as allow-list entries.
        self.hub.toggle_engine_models_batch("agy", ["vendor/foo"], True)
        policy = self._policy()
        self.assertEqual(policy["disabled_model_groups"], ["vendor"])
        self.assertEqual(sorted(policy["enabled_models"]), ["vendor/foo-high", "vendor/foo-low"])
        self.assertIs(self.hub.engine_model_option("agy", "vendor/baz")["enabled"], False)

        self.hub.toggle_engine_model_group("agy", "vendor", True)
        policy = self._policy()
        self.assertNotIn("disabled_model_groups", policy)
        self.assertNotIn("enabled_models", policy)
        self.assertIs(self.hub.engine_model_option("agy", "vendor/foo")["enabled"], True)
        self.assertIs(self.hub.engine_model_option("agy", "vendor/baz")["enabled"], True)


if __name__ == "__main__":
    unittest.main()
