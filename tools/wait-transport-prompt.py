"""Print the wait-transport prompt snapshot: SERVER_INSTRUCTIONS + wait_for_hub's docstring.

A parent reads both, so the stress spec `tools/wait-transport-stress.json` tests them together.
server.py is read as source with ast, so no hub data folder or MCP import is needed.

Usage: python tools/wait-transport-prompt.py > snapshot.txt
"""
import ast
import inspect
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_guidance import SERVER_INSTRUCTIONS  # noqa: E402


def tool_docstring(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return inspect.cleandoc(ast.get_docstring(node, clean=False) or "")
    raise SystemExit(f"{name} not found in {path}")


def main() -> None:
    doc = tool_docstring(ROOT / "server.py", "wait_for_hub")
    sys.stdout.write(SERVER_INSTRUCTIONS + "\n\n## wait_for_hub tool description\n\n" + doc + "\n")


if __name__ == "__main__":
    main()
