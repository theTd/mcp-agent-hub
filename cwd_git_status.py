"""Bounded, non-blocking git status collection for session working directories."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, Tuple


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


async def _run_git(path: str, *args: str) -> Tuple[int, str]:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    kwargs = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.STDOUT, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec("git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", path, *args, **kwargs)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def _status(path: str, *, search_children: bool = True) -> Dict[str, object]:
    try:
        code, output = await _run_git(path, "status", "--porcelain=v1", "--branch")
        lines = output.splitlines()
        if code == 0:
            return {"cwd": path, "ok": True, "lines": lines[:60]}
        first = (lines[0] if lines else "git status failed").strip()
        is_nonrepo = "not a git repository" in first.lower()
        if not is_nonrepo or not search_children:
            return {"cwd": path, "ok": False, "error": first}
        roots = await asyncio.to_thread(_find_repository_children, path)
        return {"cwd": path, "ok": False, "error": "not a git repository", "repositories": [await _status(str(p), search_children=False) for p in roots]}
    except asyncio.TimeoutError:
        return {"cwd": path, "ok": False, "error": "git status timed out"}
    except Exception as exc:
        return {"cwd": path, "ok": False, "error": str(exc).splitlines()[0]}


def _find_repository_children(path: str) -> list[Path]:
    """Scan at most 500 entries, stopping after five repositories."""
    roots = []
    try:
        for index, child in enumerate(Path(path).iterdir()):
            if index >= 500:
                break
            try:
                if not child.is_dir():
                    continue
                if (child / ".git").exists() or (child / "HEAD").exists():
                    roots.append(child)
                    if len(roots) >= 5:
                        break
            except OSError:
                continue
    except OSError:
        return []
    return sorted(roots, key=lambda p: p.name.lower())


async def collect_git_status(paths: Iterable[str]) -> Dict[str, Dict[str, object]]:
    results = await asyncio.gather(*(_status(str(p)) for p in paths), return_exceptions=False)
    return {str(item["cwd"]): item for item in results}
