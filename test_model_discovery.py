"""Claude Code model discovery tests: wrapper binaries, model-catalog cache, embedded binary seeds, and effort resolution."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from model_discovery import AgentModelDiscovery
from session_manager import HubSessionManager


def _clean_env():
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_BIN", "CLAUDE_CONFIG_DIR")
    }


class ClaudeModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-claude-discovery-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def _new_discovery(self) -> AgentModelDiscovery:
        disc = AgentModelDiscovery()
        disc.home = self.home
        return disc

    def test_wrapper_binary_with_cached_catalog_and_model_settings(self):
        reclaude_exe = self.root / "reclaude.exe"
        reclaude_exe.write_bytes(b"wrapper-without-embedded-catalog")

        cfg_dir = self.root / "cfg-44468"
        cat_dir = cfg_dir / "cache" / "model-catalog"
        cat_dir.mkdir(parents=True)

        (cfg_dir / "settings.json").write_text(
            json.dumps(
                {
                    "model": "fable",
                    "modelSettings": {
                        "claude-fable-5-2": {"effortLevel": "xhigh"},
                    },
                }
            ),
            encoding="utf-8",
        )

        # Older catalog without Fable 5.2
        (cat_dir / "older-cc.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "fetchedAt": 1000,
                    "catalog": {
                        "surface": "cc",
                        "config": {
                            "id": "cc",
                            "models": [
                                {"id": "claude-opus-5-5", "name": "Opus 5.5", "section": "main"},
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        # Newer catalog with Fable 5.2, Fable 5.1, zero-effort Haiku 4.5, 4-effort Opus 4.6, plus disabled/deprecated rows
        (cat_dir / "newest-cc.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "fetchedAt": 2000,
                    "catalog": {
                        "surface": "cc",
                        "config": {
                            "id": "cc",
                            "models": [
                                {
                                    "id": "claude-opus-5-5",
                                    "name": "Opus 5.5",
                                    "section": "main",
                                    "runtime": {
                                        "effort_levels": ["low", "medium", "high", "xhigh", "max"],
                                        "default_effort": "medium",
                                    },
                                },
                                {
                                    "id": "claude-fable-5-2",
                                    "name": "Fable 5.2",
                                    "section": "main",
                                    "runtime": {
                                        "effort_levels": ["low", "medium", "high", "xhigh", "max"],
                                        "default_effort": "medium",
                                    },
                                },
                                {
                                    "id": "claude-fable-5-1",
                                    "name": "Fable 5.1",
                                    "section": "overflow",
                                    "runtime": {
                                        "effort_levels": ["low", "medium", "high", "xhigh", "max"],
                                        "default_effort": "high",
                                    },
                                },
                                {
                                    "id": "claude-haiku-4-5-20251001",
                                    "name": "Haiku 4.5",
                                    "section": "overflow",
                                    "thinking": {"type": "none"},
                                    "runtime": {},
                                },
                                {
                                    "id": "claude-opus-4-6",
                                    "name": "Opus 4.6",
                                    "section": "overflow",
                                    "runtime": {
                                        "effort_levels": ["low", "medium", "high", "max"],
                                        "default_effort": "high",
                                    },
                                },
                                {
                                    "id": "claude-deprecated-1",
                                    "name": "Deprecated 1",
                                    "section": "deprecated",
                                },
                                {
                                    "id": "claude-disabled-1",
                                    "name": "Disabled 1",
                                    "section": "main",
                                    "disabled": True,
                                },
                            ],
                            "provider_alias_targets": {
                                "fable": {"default": "claude-fable-5-2"},
                                "opus": {"default": "claude-opus-5-5"},
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        disc = self._new_discovery()
        with patch.dict(os.environ, _clean_env(), clear=True), patch("model_discovery.shutil.which", return_value=None):
            res = disc._discover_claude(
                binary_override=str(reclaude_exe),
                config_dir_override=str(cfg_dir),
            )

        avail = res["available_models"]
        self.assertEqual(avail[0], "fable")
        for expected in ("fable", "fable[1m]", "claude-fable-5-2", "claude-fable-5-1", "claude-haiku-4-5"):
            self.assertIn(expected, avail)
        self.assertNotIn("claude-deprecated-1", avail)
        self.assertNotIn("claude-disabled-1", avail)
        self.assertNotIn("claude-haiku-4-5-20251001", avail)

        by_id = {m["id"]: m for m in res["model_details"]}
        self.assertEqual(by_id["claude-fable-5-2"]["display_name"], "Claude Fable 5.2")
        self.assertEqual(by_id["claude-fable-5-1"]["display_name"], "Claude Fable 5.1")
        self.assertEqual(by_id["fable"]["default_thinking_level"], "xhigh")
        self.assertEqual(by_id["fable[1m]"]["default_thinking_level"], "xhigh")
        self.assertEqual(by_id["claude-fable-5-2"]["default_thinking_level"], "xhigh")
        self.assertEqual(res["default_thinking_level"], "xhigh")
        self.assertEqual(by_id["claude-haiku-4-5"]["supported_thinking_levels"], [])
        self.assertIsNone(by_id["claude-haiku-4-5"]["default_thinking_level"])
        self.assertEqual(by_id["claude-opus-4-6"]["supported_thinking_levels"], ["low", "medium", "high", "max"])

    def test_reclaude_resolves_current_account_config_dir(self):
        reclaude_exe = self.root / "reclaude.exe"
        reclaude_exe.write_bytes(b"wrapper")

        rec_dir = self.home / ".rec" / "claude"
        acct_cfg = rec_dir / "accounts" / "44468" / "config"
        acct_cfg.mkdir(parents=True)
        (rec_dir / "current").write_text("44468\n", encoding="utf-8")
        (acct_cfg / "settings.json").write_text(json.dumps({"model": "fable"}), encoding="utf-8")

        disc = self._new_discovery()
        with patch.dict(os.environ, _clean_env(), clear=True), patch("model_discovery.shutil.which", return_value=None):
            res = disc._discover_claude(binary_override=str(reclaude_exe))

        self.assertEqual(Path(res["config_dir"]), acct_cfg)
        self.assertEqual(res["default_model"], "fable")
        self.assertEqual(res["available_models"][0], "fable")

    def test_binary_catalog_fallback_and_stat_cache(self):
        reclaude_exe = self.root / "reclaude.exe"
        reclaude_exe.write_bytes(b"")

        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        claude_exe = local_bin / "claude.exe"

        padding = b"X" * 320
        ue_blob = (
            b'var UE={$schema:"https://downloads.claude.ai/model-catalog/v1/schema.json",'
            b'surfaces:{cc:{model_selector_config:[{id:"cc",models:['
            b'{id:"claude-fable-5-2",name:"Fable 5.2",section:"main",runtime:{effort_levels:["low","medium","high","xhigh","max"],default_effort:"medium"}},'
            b'{id:"claude-haiku-4-5-20251001",name:"Haiku 4.5",section:"overflow",thinking:{type:"none"}},'
            b'{id:"claude-old-disabled",name:"Old",section:"main",disabled:!0}'
            b'],provider_alias_targets:{fable:{default:"claude-fable-5-2"}}}]}}};'
        )
        em_blob = (
            b'var Em={FABLE_ID:"claude-fable-5-2",FABLE_NAME:"Claude Fable 5.2",'
            b'MYTHOS_ID:"claude-mythos-5-2",MYTHOS_NAME:"Claude Mythos 5.2",'
            + padding
            + b'OPUS_ID:"claude-opus-5-5",OPUS_NAME:"Claude Opus 5.5"};'
        )
        claude_exe.write_bytes(b"HEADER" + ue_blob + b"MIDDLE" + em_blob + b"TAIL")

        disc = self._new_discovery()
        with patch.dict(os.environ, _clean_env(), clear=True), patch("model_discovery.shutil.which", return_value=None):
            with patch.object(Path, "read_bytes", autospec=True, wraps=Path.read_bytes) as mock_read:
                first = disc._discover_claude(binary_override=str(reclaude_exe))
                second = disc._discover_claude(binary_override=str(reclaude_exe))
                claude_reads = [c for c in mock_read.call_args_list if c.args[0] == claude_exe]
                self.assertEqual(len(claude_reads), 1)

                # Mutate size/mtime to invalidate cache
                claude_exe.write_bytes(b"HEADER" + ue_blob + b"MIDDLE" + em_blob + b"TAIL_UPDATED")
                third = disc._discover_claude(binary_override=str(reclaude_exe))
                claude_reads_after = [c for c in mock_read.call_args_list if c.args[0] == claude_exe]
                self.assertEqual(len(claude_reads_after), 2)

        for res in (first, second, third):
            self.assertIn("claude-fable-5-2", res["available_models"])
            self.assertIn("claude-opus-5-5", res["available_models"])
            self.assertIn("claude-haiku-4-5", res["available_models"])
            self.assertNotIn("claude-old-disabled", res["available_models"])
            self.assertNotIn("claude-mythos-5-2", res["available_models"])
            by_id = {m["id"]: m for m in res["model_details"]}
            self.assertEqual(by_id["claude-fable-5-2"]["display_name"], "Claude Fable 5.2")
            self.assertEqual(by_id["claude-haiku-4-5"]["supported_thinking_levels"], [])

    def test_em_only_binary_with_distant_fable_constant_and_no_mythos_leak(self):
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        claude_exe = local_bin / "claude.exe"

        # FABLE_ID sits >300 bytes before OPUS_ID; no UE catalog present
        em_only = (
            b'var Em={FABLE_ID:"claude-fable-5-2",FABLE_NAME:"Claude Fable 5.2",'
            b'PREV_FABLE_ID:"claude-fable-5-1",PREV_FABLE_NAME:"Claude Fable 5.1",'
            b'MYTHOS_ID:"claude-mythos-5-2",MYTHOS_NAME:"Claude Mythos 5.2",'
            + b"Z" * 350
            + b'OPUS_ID:"claude-opus-5-5",OPUS_NAME:"Claude Opus 5.5"};'
        )
        claude_exe.write_bytes(em_only)

        disc = self._new_discovery()
        with patch.dict(os.environ, _clean_env(), clear=True), patch("model_discovery.shutil.which", return_value=None):
            res = disc._discover_claude(binary_override=str(claude_exe))

        self.assertIn("claude-fable-5-2", res["available_models"])
        self.assertIn("claude-fable-5-1", res["available_models"])
        self.assertIn("claude-opus-5-5", res["available_models"])
        self.assertNotIn("claude-mythos-5-2", res["available_models"])

    def test_offline_fallback_includes_fable_models(self):
        disc = self._new_discovery()
        with patch.dict(os.environ, _clean_env(), clear=True), patch("model_discovery.shutil.which", return_value=None):
            res = disc._discover_claude(
                binary_override=str(self.root / "missing-claude.exe"),
                config_dir_override=str(self.root / "missing-cfg"),
            )

        self.assertFalse(res["installed"])
        for expected in ("fable", "fable[1m]", "claude-fable-5-2", "claude-fable-5-1"):
            self.assertIn(expected, res["available_models"])
        by_id = {m["id"]: m for m in res["model_details"]}
        self.assertEqual(by_id["claude-fable-5-2"]["display_name"], "Claude Fable 5.2")
        self.assertEqual(by_id["claude-fable-5-1"]["display_name"], "Claude Fable 5.1")


class ClaudeDiscoverySessionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-claude-int-")
        self.root = Path(self.temp.name)
        self.reclaude_exe = self.root / "reclaude.exe"
        self.reclaude_exe.write_bytes(b"wrapper")
        self.cfg_dir = self.root / "cfg"
        cat_dir = self.cfg_dir / "cache" / "model-catalog"
        cat_dir.mkdir(parents=True)
        (self.cfg_dir / "settings.json").write_text(
            json.dumps(
                {
                    "model": "fable",
                    "modelSettings": {
                        "claude-fable-5-2": {"effortLevel": "xhigh"},
                    },
                }
            ),
            encoding="utf-8",
        )
        (cat_dir / "cc.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "fetchedAt": 2000,
                    "catalog": {
                        "surface": "cc",
                        "config": {
                            "id": "cc",
                            "models": [
                                {
                                    "id": "claude-fable-5-2",
                                    "name": "Fable 5.2",
                                    "section": "main",
                                    "runtime": {
                                        "effort_levels": ["low", "medium", "high", "xhigh", "max"],
                                        "default_effort": "medium",
                                    },
                                },
                                {
                                    "id": "claude-haiku-4-5-20251001",
                                    "name": "Haiku 4.5",
                                    "section": "overflow",
                                    "thinking": {"type": "none"},
                                },
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        profiles = self.root / "profiles.json"
        profiles.write_text(
            json.dumps(
                {
                    "profiles": {
                        "claude": {
                            "id": "claude",
                            "name": "Claude Code",
                            "engine": "claude",
                            "kind": "tui",
                            "command": str(self.reclaude_exe),
                            "config_dir": str(self.cfg_dir),
                            "args": [],
                            "task_mode": "claude_positional",
                            "enabled": True,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.hub.model_discovery.home = self.root / "empty-home"
        self.hub.model_discovery.home.mkdir()
        with patch("model_discovery.shutil.which", return_value=None):
            claude_info = self.hub.model_discovery._discover_claude(
                binary_override=str(self.reclaude_exe),
                config_dir_override=str(self.cfg_dir),
            )
            self.hub.load_profiles(discovery={"engines": {"claude": claude_info}})
        await self.hub.start()

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def test_catalog_and_resolve_launch_fable_and_zero_effort_haiku(self):
        models = self.hub.engines_catalog["claude"]["models"]
        by_id = {m["id"]: m for m in models}
        self.assertIn("claude-fable-5-2", by_id)
        self.assertEqual(by_id["claude-fable-5-2"]["label"], "Claude Fable 5.2")
        self.assertEqual(by_id["claude-haiku-4-5"]["thinking_levels"], [])

        fable_launch = self.hub.resolve_launch("claude", model="claude-fable-5-2", cwd=str(self.root))
        self.assertEqual(fable_launch["args"][-4:], ["--model", "claude-fable-5-2", "--effort", "xhigh"])

        haiku_launch = self.hub.resolve_launch("claude", model="claude-haiku-4-5", cwd=str(self.root))
        self.assertEqual(haiku_launch["args"][-2:], ["--model", "claude-haiku-4-5"])
        self.assertNotIn("--effort", haiku_launch["args"])

        with self.assertRaises(ValueError) as ctx:
            self.hub.resolve_launch("claude", model="claude-haiku-4-5", thinking_level="high", cwd=str(self.root))
        self.assertIn("无", str(ctx.exception))


class CodexModelCatalogJsonTests(unittest.TestCase):
    """`model_catalog_json` replaces Codex's bundled catalog, so discovery must mirror it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-codex-catalog-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.catalog_path = self.home / ".codex" / "catalog.json"

    def tearDown(self):
        self.temp.cleanup()

    def _write_config(self, catalog_path: str) -> None:
        (self.home / ".codex" / "config.toml").write_text(
            'model = "gpt-6.2-sol"\n'
            'model_provider = "clay"\n'
            f"model_catalog_json = '{catalog_path}'\n",
            encoding="utf-8",
        )

    def _write_catalog(self, models) -> None:
        self.catalog_path.write_text(json.dumps({"models": models}), encoding="utf-8")

    def _discover(self):
        disc = AgentModelDiscovery()
        disc.home = self.home
        with patch("model_discovery.shutil.which", return_value=None):
            return disc._discover_codex()

    def test_custom_catalog_models_are_listed(self):
        self._write_catalog([
            {
                "slug": "gpt-6.2-sol",
                "display_name": "GPT-6.2-Sol",
                "visibility": "list",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                "context_window": 300000,
            },
            {
                "slug": "gpt-6.2-astra",
                "display_name": "GPT-6.2-Astra",
                "visibility": "list",
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                "context_window": "272K",
            },
        ])
        self._write_config("catalog.json")

        info = self._discover()

        self.assertIn("gpt-6.2-sol", info["available_models"])
        detail = next(d for d in info["model_details"] if d["id"] == "gpt-6.2-sol")
        self.assertEqual(detail["display_name"], "GPT-6.2-Sol")
        self.assertEqual(detail["supported_thinking_levels"], ["low", "high"])
        self.assertEqual(detail["default_thinking_level"], "medium")
        self.assertEqual(detail["context_window_tokens"], 300000)
        self.assertIn("gpt-6.2-astra", info["available_models"])
        self.assertTrue(info["custom_catalog_loaded"])
        self.assertEqual(info["custom_catalog_path"], str(self.catalog_path))

    def test_custom_catalog_replaces_bundled_catalog(self):
        embedded_path = self.home / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        embedded_path.parent.mkdir(parents=True)
        embedded_path.write_bytes(
            b'prefix {"models": [{"slug": "embedded-only", "display_name": "Embedded", '
            b'"supported_reasoning_levels": [{"effort": "low"}]}]} suffix'
        )
        self._write_catalog([
            {"slug": "custom-only", "display_name": "Custom", "supported_reasoning_levels": [{"effort": "low"}]}
        ])
        self._write_config(str(self.catalog_path))

        info = self._discover()

        self.assertIn("custom-only", info["available_models"])
        self.assertNotIn("embedded-only", info["available_models"])
        self.assertFalse(info["embedded_catalog_loaded"])
        self.assertTrue(info["custom_catalog_loaded"])

    def test_invalid_catalog_falls_back_to_embedded_catalog(self):
        embedded_path = self.home / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        embedded_path.parent.mkdir(parents=True)
        embedded_path.write_bytes(
            b'prefix {"models": [{"slug": "embedded-only", "display_name": "Embedded", '
            b'"supported_reasoning_levels": [{"effort": "low"}]}]} suffix'
        )
        self.catalog_path.write_text("{invalid", encoding="utf-8")
        self._write_config(str(self.catalog_path))

        info = self._discover()

        self.assertFalse(info["custom_catalog_loaded"])
        self.assertTrue(info["embedded_catalog_loaded"])
        self.assertIn("embedded-only", info["available_models"])
        self.assertEqual(info["default_model"], "gpt-6.2-sol")


def _omp_models_stdout(rows):
    """Builds `omp models` output with one provider table; rows are (model, thinking column)."""
    lines = [
        f"clay ({len(rows)})",
        "┌───────┬─────────┬─────────┬──────────┬────────┐",
        "│ model │ context │ max-out │ thinking │ images │",
        "├───────┼─────────┼─────────┼──────────┼────────┤",
    ]
    lines.extend(f"│ {model} │ 1M │ 66K │ {thinking} │ yes │" for model, thinking in rows)
    lines.append("└───────┴─────────┴─────────┴──────────┴────────┘")
    return "\n".join(lines)


class OmpThinkingColumnTests(unittest.TestCase):
    """`omp models` prints one effort (`high`) for single-level models, not a comma list or `yes`."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-omp-discovery-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.omp_exe = self.home / "AppData" / "Local" / "omp" / "omp.exe"
        self.omp_exe.parent.mkdir(parents=True)
        self.omp_exe.write_bytes(b"fake")

    def tearDown(self):
        self.temp.cleanup()

    def _discover(self, rows):
        stdout = _omp_models_stdout(rows)
        disc = AgentModelDiscovery()
        disc.home = self.home
        with patch("model_discovery.shutil.which", return_value=str(self.omp_exe)), \
                patch("model_discovery.subprocess.run", return_value=SimpleNamespace(stdout=stdout)):
            return disc._discover_omp()

    def _levels(self, rows):
        return {m["id"]: m["supported_thinking_levels"] for m in self._discover(rows)["model_details"]}

    def test_single_effort_token_is_preserved(self):
        levels = self._levels([
            ("hy4-flash-free", "high"),
            ("probe-minimal", "minimal"),
            ("probe-xhigh", "xhigh"),
        ])

        self.assertEqual(levels["clay/hy4-flash-free"], ["high"])
        self.assertEqual(levels["clay/probe-minimal"], ["minimal"])
        self.assertEqual(levels["clay/probe-xhigh"], ["xhigh"])

    def test_comma_separated_levels_preserve_order(self):
        levels = self._levels([
            ("a", "low,high,max"),
            ("b", "minimal, low , medium, high, xhigh"),
        ])

        self.assertEqual(levels["clay/a"], ["low", "high", "max"])
        self.assertEqual(levels["clay/b"], ["minimal", "low", "medium", "high", "xhigh"])

    def test_disabled_tokens_map_to_off(self):
        rows = [("dash", "-"), ("no", "no"), ("none", "NONE"), ("off", "Off"), ("blank", "")]
        levels = self._levels(rows)

        for model, _thinking in rows:
            with self.subTest(model=model):
                self.assertEqual(levels[f"clay/{model}"], ["off"])

    def test_legacy_yes_expands_to_classic_levels(self):
        self.assertEqual(self._levels([("legacy", "YES")])["clay/legacy"], ["low", "medium", "high", "max"])

    def test_models_yml_efforts_are_used_when_cli_table_is_unavailable(self):
        agent_dir = self.home / ".omp" / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "models.yml").write_text(
            "providers:\n"
            "  clay:\n"
            "    models:\n"
            "      - id: hy4-flash-free\n"
            "        thinking:\n"
            "          mode: effort\n"
            "          efforts: [high]\n"
            "        contextWindow: 1048576\n"
            "      - id: deepseek-flash\n"
            "        contextWindow: 1048576\n",
            encoding="utf-8",
        )
        disc = AgentModelDiscovery()
        disc.home = self.home
        with patch("model_discovery.shutil.which", return_value=None), \
                patch("model_discovery.subprocess.run", return_value=SimpleNamespace(stdout="")):
            info = disc._discover_omp()

        levels = {m["id"]: m["supported_thinking_levels"] for m in info["model_details"]}
        self.assertEqual(levels["clay/hy4-flash-free"], ["high"])
        self.assertEqual(levels["clay/deepseek-flash"], ["low", "medium", "high", "max"])


class OmpSingleLevelLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-omp-single-level-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.omp_exe = self.home / "AppData" / "Local" / "omp" / "omp.exe"
        self.omp_exe.parent.mkdir(parents=True)
        self.omp_exe.write_bytes(b"fake")
        profiles = self.root / "profiles.json"
        profiles.write_text(
            json.dumps({
                "profiles": {
                    "omp": {
                        "id": "omp", "name": "OMP", "engine": "omp", "kind": "tui",
                        "command": str(self.omp_exe), "args": [], "enabled": True,
                    }
                }
            }),
            encoding="utf-8",
        )
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()

        stdout = _omp_models_stdout([("hy4-flash-free", "high")])
        disc = AgentModelDiscovery()
        disc.home = self.home
        with patch("model_discovery.shutil.which", return_value=str(self.omp_exe)), \
                patch("model_discovery.subprocess.run", return_value=SimpleNamespace(stdout=stdout)):
            omp_info = disc._discover_omp()
        self.hub.load_profiles(discovery={"engines": {"omp": omp_info}})

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def test_single_level_model_accepts_high_and_rejects_unsupported_low(self):
        launch = self.hub.resolve_launch(
            "omp", model="clay/hy4-flash-free", thinking_level="high", cwd=str(self.root)
        )
        self.assertEqual(launch["model"], "clay/hy4-flash-free")
        self.assertEqual(launch["thinking_level"], "high")
        self.assertIn("--model=clay/hy4-flash-free", launch["args"])
        self.assertIn("--thinking=high", launch["args"])

        default_launch = self.hub.resolve_launch("omp", model="clay/hy4-flash-free", cwd=str(self.root))
        self.assertEqual(default_launch["thinking_level"], "high")
        self.assertIn("--thinking=high", default_launch["args"])

        with self.assertRaises(ValueError) as caught:
            self.hub.resolve_launch(
                "omp", model="clay/hy4-flash-free", thinking_level="low", cwd=str(self.root)
            )
        self.assertIn("high", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
