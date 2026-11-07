"""Where the DeepSeek Harness (DSH) host listens.

dsh-crew keeps the host's address in its own config (`hub_url` in ~/.config/dsh-crew/config.json).
On 2026-11-07 the host moved from 127.0.0.1:3080 to 127.0.0.1:5149 while the hub still called the
old port. Every hub call to DSH now builds its URL here, so the hub follows the address dsh-crew
itself uses; the old port stays the fallback when the file or the key is missing.
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

DSH_DEFAULT_BASE_URL = "http://127.0.0.1:3080"

# (path, mtime_ns, size) of the config last read -> its base URL; re-read only when the file changes.
_cache: Dict[str, Any] = {"key": None, "url": DSH_DEFAULT_BASE_URL}


def dsh_crew_config_path() -> Path:
    """dsh-crew's config file (`HUB_DSH_CREW_CONFIG` overrides it, for tests)."""
    raw = os.environ.get("HUB_DSH_CREW_CONFIG")
    if raw and raw.strip():
        return Path(os.path.expanduser(raw.strip()))
    return Path(os.path.expanduser("~")) / ".config" / "dsh-crew" / "config.json"


def dsh_base_url(config_path: Optional[Union[str, Path]] = None) -> str:
    """The DSH host's base URL without a trailing slash, e.g. `http://127.0.0.1:5149`."""
    path = Path(config_path) if config_path is not None else dsh_crew_config_path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return DSH_DEFAULT_BASE_URL
    if _cache["key"] == key:
        return _cache["url"]
    url = DSH_DEFAULT_BASE_URL
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        raw = str(data.get("hub_url") or "").strip() if isinstance(data, dict) else ""
        if raw.lower().startswith(("http://", "https://")):
            url = raw.rstrip("/")
    except Exception:
        url = DSH_DEFAULT_BASE_URL
    _cache["key"] = key
    _cache["url"] = url
    return url


def dsh_url(path: str, config_path: Optional[Union[str, Path]] = None) -> str:
    """A full DSH URL for `path` (e.g. `/_dsh/dsh-crew/jobs/<id>`)."""
    return f"{dsh_base_url(config_path)}/{str(path).lstrip('/')}"
