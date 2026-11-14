"""The hub runs on the Rust terminal core alone (decision terminal-core-replaces).

Step 3b moved pyte into `tools/pyte_oracle.py` (offline fixture oracle, until step 6) and removed pywinpty from the
hub. A fresh interpreter that imports the hub's two entry modules must therefore load neither, so a stray import
cannot quietly bring the old terminal path back.

The browser→PTY reply strip is the core socket's now (step 4), covered end to end by `test_hub_persistence.py`.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
PROBE = (
    "import json, sys\n"
    "import session_manager, server\n"
    "print(json.dumps({\n"
    "    'old_terminal': sorted(m for m in sys.modules if m.split('.')[0] in ('pyte', 'winpty')),\n"
    "}))\n"
)


class HubTerminalDependencyTests(unittest.TestCase):
    def _probe(self) -> dict:
        # Importing server builds the hub, which opens its databases under HUB_DATA_DIR (default: this folder).
        with tempfile.TemporaryDirectory(dir=REPO_DIR, prefix="_import_probe_") as data_dir:
            proc = subprocess.run(
                [sys.executable, "-B", "-c", PROBE], cwd=REPO_DIR, capture_output=True, text=True,
                encoding="utf-8", timeout=120, env=dict(os.environ, HUB_DATA_DIR=data_dir),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_importing_the_hub_loads_neither_pyte_nor_winpty(self):
        loaded = self._probe()["old_terminal"]
        self.assertEqual(loaded, [], f"the hub imported {loaded}")


if __name__ == "__main__":
    unittest.main()
