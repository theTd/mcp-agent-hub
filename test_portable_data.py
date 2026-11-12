"""Regression tests for resource/data separation; never start discovery or touch personal data."""
import importlib.util
import ast
import json
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from session_manager import HubSessionManager


class PortableDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_mode_preserves_original_data_locations(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HUB_DATA_DIR": ""}), patch.object(HubSessionManager, "_initial_discovery", lambda manager: manager._discovery_ready.set()):
            source = Path(directory)
            hub = HubSessionManager(source / "profiles.json", base_dir=source)
            self.assertEqual(hub.data_dir, source.resolve())
            self.assertFalse((source / "hub_state.db").exists(), "constructor must not open SQLite")
            try:
                await hub.start()
                self.assertTrue((source / "hub_state.db").is_file())
            finally:
                await hub.archive_all_sessions_on_shutdown()

    async def test_portable_mode_writes_only_to_data_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(HubSessionManager, "_initial_discovery", lambda manager: manager._discovery_ready.set()):
            source = Path(directory) / "resources"
            source.mkdir()
            data = Path(directory) / "data"
            with patch.dict(os.environ, {"HUB_DATA_DIR": str(data)}):
                hub = HubSessionManager(data / "profiles.json", base_dir=source)
                self.assertEqual(hub.base_dir, source.resolve())
                self.assertEqual(hub.runtime_configs_dir, data / ".runtime_mcp_configs")
                try:
                    await hub.start()
                    self.assertTrue((data / "hub_state.db").is_file())
                    self.assertEqual(list(source.iterdir()), [])
                finally:
                    await hub.archive_all_sessions_on_shutdown()

    def test_payload_includes_local_python_imports(self):
        root = Path(__file__).parent
        payload = set((root / "desktop/portable/payload.txt").read_text(encoding="utf-8").splitlines())
        for filename in payload:
            if not filename.endswith(".py"):
                continue
            tree = ast.parse((root / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                )
                for module in modules:
                    local = module.split(".")[0] + ".py"
                    if (root / local).is_file():
                        self.assertIn(local, payload, f"{filename} imports an unpackaged module")

    def test_payload_includes_console_modules_and_styles(self):
        root = Path(__file__).parent
        payload = set((root / "desktop/portable/payload.txt").read_text(encoding="utf-8").splitlines())
        index = (root / "static/index.html").read_text(encoding="utf-8")
        needed = {f"static/{path}" for path in re.findall(r'(?:href|src)="/static/([\w/.-]+\.(?:css|js))"', index)}
        for filename in [name for name in payload if name.startswith("static/js/") and name.endswith(".js")]:
            source = (root / filename).read_text(encoding="utf-8")
            needed |= {f"static/js/{name}" for name in re.findall(r"from '\./([\w.-]+\.js)'", source)}
        self.assertTrue(needed)
        self.assertEqual(sorted(needed - payload), [], "the console loads files the portable build leaves out")

    def test_claude_hook_uses_same_data_root(self):
        path = Path(__file__).parent / "engine_policies" / "claude_pre_tool_hook.py"
        spec = importlib.util.spec_from_file_location("portable_hook_test", path)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HUB_DATA_DIR": directory, "HUB_CLAUDE_HOOK_LOG": ""}):
            hook._record_permission_request({"session_id": "portable-test", "tool_name": "Bash"})
            log = Path(directory) / ".runtime_mcp_configs" / "claude_hooks_portable-test.jsonl"
            self.assertEqual(json.loads(log.read_text(encoding="utf-8"))["tool_name"], "Bash")


if __name__ == "__main__":
    unittest.main()
