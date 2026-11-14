"""The hub runs on the Rust terminal core alone (decision terminal-core-replaces).

Step 3b moved pyte out of the hub and step 6 deleted it: ``tools/pyte_oracle.py``, its renderer and
the tests that imported them are gone, and the goldens are the spec. A fresh interpreter that
imports the hub's two entry modules must load neither ``pyte`` nor ``winpty``, so a stray import
cannot quietly bring the old terminal path back; a tracked ``*.py`` that imports ``pyte`` anywhere
(including the offline tools) fails the second test. Prose mentions of the oracle do not count.

The browser→PTY reply strip is the core socket's now (step 4), covered end to end by `test_hub_persistence.py`.
"""
import ast
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


def tracked_python_files() -> list:
    """Every tracked ``*.py`` path, relative to the repo root, in git's own order."""
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_DIR, capture_output=True, check=True)
    names = listing.stdout.decode("utf-8", "replace").split("\0")
    return [name for name in names if name.endswith(".py")]


def pyte_imports(source: str, filename: str = "<source>") -> list:
    """Every ``import pyte`` / ``from pyte… import`` in the source, as (line, module)."""
    hits = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "pyte":
                    hits.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] == "pyte":
                hits.append((node.lineno, module))
    return sorted(hits)


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

    def test_no_tracked_python_file_imports_pyte(self):
        offenders = []
        for relative in tracked_python_files():
            path = REPO_DIR / relative
            if not path.is_file():
                continue  # tracked but deleted in the working tree (the caller reviews the change)
            source = path.read_text(encoding="utf-8", errors="replace")
            for line, module in pyte_imports(source, relative):
                offenders.append(f"{relative}:{line}: imports {module}")
        self.assertEqual(offenders, [],
                         "pyte is deleted (step 6); these tracked files still import it:\n"
                         + "\n".join(offenders))

    def test_the_scan_flags_an_import_and_ignores_a_prose_mention(self):
        source = (
            "import pyte\n"
            "from pyte.screens import Screen\n"
            "from tools.pyte_oracle import nothing\n"
            "# The pyte oracle rendered these goldens; it is gone.\n"
        )
        self.assertEqual(pyte_imports(source), [(1, "pyte"), (2, "pyte.screens")])


if __name__ == "__main__":
    unittest.main()
