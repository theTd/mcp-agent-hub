"""extra_roots: folders beyond cwd that a spawned worker may read AND write.

Owner decision `hub-friction-1116` (read-roots, 2026-11-16): the field is named for what it does.
Every engine flag that adds a folder (--add-dir on claude, codex, omp, kimi and agy) grants writes as
well as reads, so a read-only promise could not be kept. The checks here keep a parent from handing a
worker a drive root, the user's home, system or credential folders, or the hub's own data.
"""
import os
from typing import Any, Iterable, List, Optional

EXTRA_ROOT_ENGINES = ("claude", "codex", "omp", "kimi", "agy")
EXTRA_ROOTS_MAX = 8
# Credential and engine-config folders directly under the user's home: never a root, nor inside one.
HOME_PRIVATE_FOLDERS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config",
    ".codex", ".claude", ".kimi", ".kimi-code", ".omp", ".gemini", ".antigravity", "AppData",
)
# System and per-user application folders: never a root, inside one, or a folder holding one.
SYSTEM_FOLDER_VARS = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "APPDATA", "LOCALAPPDATA")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _within(path: str, folder: str) -> bool:
    """`path` is `folder` or inside it (both already normalized)."""
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


def _is_volume_root(path: str) -> bool:
    drive, rest = os.path.splitdrive(path)
    return rest in ("", "\\", "/") or _norm(os.path.dirname(path)) == _norm(path)


def _refusal(path: str, home: str, forbidden: List[str]) -> Optional[str]:
    key = _norm(path)
    if _is_volume_root(path):
        return "a drive or share root"
    if _within(home, key):
        return "the user's home folder or a folder holding it"
    for name in HOME_PRIVATE_FOLDERS:
        if _within(key, _norm(os.path.join(home, name))):
            return f"inside {os.path.join(home, name)} (credentials or engine settings)"
    for var in SYSTEM_FOLDER_VARS:
        value = os.environ.get(var)
        if value:
            folder = _norm(value)
            if _within(key, folder) or _within(folder, key):
                return f"the system or application folder {value}, inside it or holding it"
    for folder in forbidden:
        if _within(key, folder) or _within(folder, key):
            return "the hub's data folder, inside it or holding it"
    return None


def normalize_extra_roots(value: Any, *, forbidden: Iterable[str] = (), home: Optional[str] = None) -> List[str]:
    """The checked absolute folders, in order, duplicates dropped; raises ValueError naming the entry.

    Each entry must be an absolute path to an existing folder that is not reached through a link or
    junction, and is none of: a drive or share root, the user's home or a folder holding it, a folder
    inside HOME_PRIVATE_FOLDERS, a system or application folder (inside it or holding it), or a
    `forbidden` folder (the hub's data folder), inside it or holding it.
    """
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("extra_roots must be a list of absolute folder paths")
    if len(value) > EXTRA_ROOTS_MAX:
        raise ValueError(f"extra_roots takes at most {EXTRA_ROOTS_MAX} folders")
    home_dir = _norm(home or os.path.expanduser("~"))
    blocked = [_norm(str(folder)) for folder in forbidden if folder]
    roots: List[str] = []
    seen = set()
    for raw in value:
        text = raw.strip()
        if not text or "\x00" in text or not os.path.isabs(text):
            raise ValueError(f"extra_roots entry {raw!r} is not an absolute path")
        path = os.path.abspath(text)
        if not os.path.isdir(path):
            raise ValueError(f"extra_roots entry {path} is not an existing folder")
        if _norm(os.path.realpath(path)) != _norm(path):
            raise ValueError(f"extra_roots entry {path} goes through a link or junction; name the real folder")
        reason = _refusal(path, home_dir, blocked)
        if reason:
            raise ValueError(f"extra_roots entry {path} is refused: {reason}")
        if _norm(path) not in seen:
            seen.add(_norm(path))
            roots.append(path)
    return roots


def extra_root_args(engine: str, kind: str, roots: List[str]) -> List[str]:
    """The engine's argv for the roots: one --add-dir=<folder> each.

    The joined form is used for every engine: claude's --add-dir takes several values, and a
    separate value list could swallow the positional task that follows the options.
    """
    if not roots:
        return []
    if kind != "tui" or engine not in EXTRA_ROOT_ENGINES:
        raise ValueError(
            f"extra_roots works on {', '.join(EXTRA_ROOT_ENGINES)} only, not {engine or kind}: "
            "it has no flag that adds a folder; put the files under cwd"
        )
    return [f"--add-dir={root}" for root in roots]
