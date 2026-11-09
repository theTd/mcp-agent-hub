"""Regression tests for resource/data separation; never start discovery or touch personal data."""
import importlib.util
import gc
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from session_manager import HubSessionManager


class PortableDataTests(unittest.TestCase):
    def test_source_mode_preserves_original_data_locations(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HUB_DATA_DIR": ""}), patch.object(threading.Thread, "start"):
            source = Path(directory)
            hub = HubSessionManager(source / "profiles.json", base_dir=source)
            self.assertEqual(hub.data_dir, source.resolve())
            self.assertTrue((source / "hub_state.db").is_file())
            gc.collect()

    def test_portable_mode_writes_only_to_data_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(threading.Thread, "start"):
            source = Path(directory) / "resources"
            source.mkdir()
            data = Path(directory) / "data"
            with patch.dict(os.environ, {"HUB_DATA_DIR": str(data)}):
                hub = HubSessionManager(data / "profiles.json", base_dir=source)
                self.assertEqual(hub.base_dir, source.resolve())
                self.assertEqual(hub.runtime_configs_dir, data / ".runtime_mcp_configs")
                self.assertTrue((data / "hub_state.db").is_file())
                self.assertEqual(list(source.iterdir()), [])
                gc.collect()

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
