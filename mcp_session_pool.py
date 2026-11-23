"""Session-scoped MCP connection pool for the Agent Hub.

One Hub session owns one :class:`SessionPool`; one pool owns one
:class:`ServerActor` per configured MCP server; one actor owns exactly one
long-lived ``fastmcp.Client`` connection.  Stateful MCP servers (a Playwright
browser, a counter, a database handle) therefore keep their state between two
``POST /api/sessions/<sid>/mcp`` calls, while the Hub HTTP API stays plain
request/response.

Design details that are easy to get wrong:

* ``async with client`` enters anyio cancel scopes, so it must be entered and
  exited by the *same* asyncio task.  The connection therefore lives inside the
  actor's own background task; every tool call is a child task of that task.
* ``keep_alive=False`` plus an explicit ``await transport.close()`` is what keeps
  Windows ``npx`` chains (``cmd -> node -> cmd -> node``) from leaking orphans;
  ``taskkill /PID <pid> /T /F`` on the recorded subprocess pid stays as a net.
* Values from ``spec["headers"]`` / ``spec["env"]`` never appear in a returned
  structure, an event or an error message.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.client.stdio import get_default_environment
from mcp.shared.exceptions import MCPError

__all__ = [
    "DEFAULT_MAX_INLINE_CHARS",
    "PoolRegistry",
    "ServerActor",
    "SessionPool",
    "clean_text",
    "classify_exception",
    "error_result",
    "install_spawn_hook",
    "kill_pid_tree",
    "pid_alive",
    "query_command_lines",
    "command_matches",
    "scrub_secrets",
]

logger = logging.getLogger(__name__)

# --- limits ----------------------------------------------------------------

DEFAULT_MAX_INLINE_CHARS = 24000
OUTPUT_DIR_FILE_LIMIT = 60
JOB_FILE_LIMIT = 20
JOB_MEMORY_LIMIT = 40
MAX_BATCH_CALLS = 50
SESSION_CONCURRENCY = 8
TRANSPORT_CLOSE_TIMEOUT_SEC = 5.0

# Sentinel outcomes of one connection round inside the actor task.
_CLOSE = object()
_BROKEN = object()
_RETRY = object()

ERROR_HINTS: Dict[str, str] = {
    "auth": "检查 MCP 服务器 headers / token 是否有效，或该 token 是否已过期。",
    "session_closed": "所在会话已结束，重新创建会话后再调用 MCP 工具。",
    "bad_request": "检查参数名称、类型与取值范围，必要时先运行 list 查看服务器。",
    "unknown_server": "先运行 list 查看当前会话可用的 MCP 服务器名称。",
    "unknown_tool": "先运行 tools <server> 查看工具清单，再按清单里的名称调用。",
    "connect": "MCP 连接已断开并只在本地重建；若持续失败，检查服务器命令、cwd 与网络。",
    "timeout": "把 timeoutMs 调到工具实际耗时之上，或用 waitMs 让调用在后台继续，再用 get_job 取结果。",
    "tool_error": "工具自身返回失败，检查 arguments 是否符合 inputSchema，或查看服务器日志。",
    "cancelled": "调用已被取消；如需结果请重新发起调用。",
    "internal": "Hub 内部错误，可重试一次；重复出现请查看 Hub 日志。",
}

# --- ANSI / text helpers ---------------------------------------------------

_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_MAX_ERROR_CHARS = 4000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_OUTPUT_FILE = re.compile(r"^\d{8}T\d{6}-\d{6}_")
_JOB_FILE = re.compile(r"^job_[0-9a-f]+\.json$")


def clean_text(value: Any) -> str:
    """Strip ANSI escapes so terminal-coloured server output stays readable."""
    text = value if isinstance(value, str) else str(value)
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OSC.sub("", text)
    return text.replace("\x1b", "")


def _clip(text: str, limit: int = _MAX_ERROR_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _safe_name(value: Any, default: str = "item") -> str:
    text = _SAFE_NAME.sub("_", str(value or "")).strip("_")
    return (text or default)[:48]


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S-%f")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _model_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# --- errors ----------------------------------------------------------------


def error_result(
    kind: str,
    message: Any,
    *,
    hint: Optional[str] = None,
    server: Optional[str] = None,
) -> Dict[str, Any]:
    """The one structured failure shape the HTTP contract promises."""
    return {
        "ok": False,
        "error": {
            "kind": kind,
            "message": _clip(clean_text(message)),
            "hint": hint or ERROR_HINTS.get(kind, ""),
            "server": server,
        },
    }


def _exception_message(exc: BaseException) -> str:
    text = clean_text(exc).strip()
    if not text:
        text = type(exc).__name__
    return text


def _auth_failure(message: str) -> bool:
    low = message.lower()
    return any(token in low for token in ("401", "403", "unauthorized", "forbidden", "invalid token", "authentication"))


def classify_exception(exc: BaseException, server: Optional[str] = None) -> Dict[str, Any]:
    """Map a client exception onto the public error kinds.

    A ``connect`` verdict marks the actor's connection as broken; the actor drops
    the client and rebuilds it on the next request.
    """
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            return classify_exception(inner, server)
    message = _exception_message(exc)
    if isinstance(exc, asyncio.CancelledError):
        kind = "cancelled"
    elif isinstance(exc, asyncio.TimeoutError):
        kind = "timeout"
    elif isinstance(exc, ToolError):
        kind = "unknown_tool" if "unknown tool" in message.lower() else "tool_error"
    elif isinstance(exc, MCPError):
        low = message.lower()
        if "timed out" in low or "timeout" in low:
            kind = "timeout"
        elif _auth_failure(message):
            kind = "auth"
        elif "connection closed" in low or "closed" in low or "broken pipe" in low or "end of file" in low:
            kind = "connect"
        else:
            kind = "tool_error"
    elif isinstance(exc, RuntimeError):
        kind = "auth" if _auth_failure(message) else "connect"
    elif isinstance(exc, (FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError, OSError)):
        kind = "connect"
    else:
        kind = "internal"
    return error_result(kind, message, server=server)


def _first_text(blocks: Sequence[Dict[str, Any]]) -> str:
    for block in blocks or ():
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return ""


# --- secret hygiene --------------------------------------------------------

REDACTED = "[REDACTED]"
# Shorter values ("1", "true", "on") are usually a switch, not a credential; redacting them would
# mangle ordinary error text without protecting anything.  Everything at or above this length is
# treated as a secret and removed wherever it could reach the caller.
SECRET_MIN_LEN = 4
_AUTH_PREFIXES = ("bearer ", "basic ")


def spec_secrets(spec: Optional[Dict[str, Any]]) -> List[str]:
    """Every string value of this server's own ``headers`` / ``env``, longest first.

    The token body behind ``Bearer`` / ``Basic`` is added too: an upstream server often echoes
    only the credential, never the scheme.
    """
    raw: List[str] = []
    for key in ("headers", "env"):
        section = (spec or {}).get(key)
        if not isinstance(section, dict):
            continue
        for value in section.values():
            if isinstance(value, bool) or value is None:
                continue
            text = value if isinstance(value, str) else str(value)
            text = text.strip()
            if not text:
                continue
            raw.append(text)
            lowered = text.lower()
            for prefix in _AUTH_PREFIXES:
                if lowered.startswith(prefix):
                    body = text[len(prefix):].strip()
                    if body:
                        raw.append(body)
    unique: List[str] = []
    for text in raw:
        if len(text) < SECRET_MIN_LEN or text in unique:
            continue
        unique.append(text)
    unique.sort(key=len, reverse=True)
    return unique


def scrub_secrets(text: Any, secrets: Iterable[str]) -> Any:
    """Replace every known secret value in ``text`` with ``[REDACTED]``.

    Longest value first, so ``Bearer abc123`` is replaced as a whole rather than leaving the
    scheme and a half-redacted tail behind.  A non-string is returned unchanged (callers pass
    the error text only, never the payload itself).
    """
    if not isinstance(text, str) or not text or not secrets:
        return text
    ordered = sorted({str(secret) for secret in secrets if secret and len(str(secret)) >= 1},
                     key=len, reverse=True)
    out = text
    for secret in ordered:
        out = out.replace(secret, REDACTED)
    return out


# --- subprocess bookkeeping ------------------------------------------------

_SPAWN_LOCK = threading.Lock()
_SPAWN_INSTALLED = False
_SPAWN_ORIGINAL: Optional[Callable[..., Any]] = None
_SPAWN_RECORDER: contextvars.ContextVar[Optional["_SpawnRecorder"]] = contextvars.ContextVar(
    "mcp_session_pool_spawn_recorder", default=None
)


class _SpawnRecorder:
    """Collects the pids of stdio subprocesses spawned inside one actor's context.

    ``on_pid`` is called the instant a child is recorded, not after the handshake: the pid
    ledger must already list the child while a slow ``initialize`` is still in flight, or a
    Hub crash during that window leaves an unaccounted orphan.
    """

    __slots__ = ("pids", "on_pid")

    def __init__(self, on_pid: Optional[Callable[[int], Any]] = None) -> None:
        self.pids: List[int] = []
        self.on_pid = on_pid

    def record(self, pid: Any) -> None:
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        self.pids.append(value)
        if self.on_pid is not None:
            try:
                self.on_pid(value)
            except Exception:  # pragma: no cover - a bookkeeping callback never breaks a dial
                pass

    @property
    def last(self) -> Optional[int]:
        return self.pids[-1] if self.pids else None


def install_spawn_hook() -> None:
    """Learn the pid of every stdio subprocess this process spawns.

    ``StdioTransport`` keeps its pid private, and the Hub needs it to (a) report
    live servers and (b) kill a whole ``cmd -> node`` tree at shutdown.  The
    hook wraps ``mcp.client.stdio._create_platform_compatible_process`` once and
    reads the recorder from a :class:`contextvars.ContextVar`, so concurrent
    actors each learn only their own child.  Idempotent and cheap.
    """
    global _SPAWN_INSTALLED, _SPAWN_ORIGINAL
    if _SPAWN_INSTALLED:
        return
    with _SPAWN_LOCK:
        if _SPAWN_INSTALLED:
            return
        import mcp.client.stdio as mcp_stdio

        original = getattr(mcp_stdio, "_create_platform_compatible_process")
        _SPAWN_ORIGINAL = original

        async def _recorded_spawn(command, args, env=None, errlog=sys.stderr, cwd=None):
            process = await original(command, args, env, errlog, cwd)
            recorder = _SPAWN_RECORDER.get()
            if recorder is not None:
                recorder.record(getattr(process, "pid", None))
            return process

        mcp_stdio._create_platform_compatible_process = _recorded_spawn
        _SPAWN_INSTALLED = True


def pid_alive(pid: Any) -> bool:
    """True when ``pid`` names a live process (cheap, no subprocess on Windows)."""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        # Access denied still means "exists"; any other failure means "gone".
        return kernel32.GetLastError() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def kill_pid_tree(pid: Any, timeout_sec: float = 10.0) -> bool:
    """Kill a process and its children.  Blocking; callers use ``asyncio.to_thread``."""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False


def query_command_lines(pids: Iterable[Any]) -> Dict[int, str]:
    """Read the command line of live processes, keyed by pid.

    Used to prove a leftover pid still belongs to the recorded MCP server before
    killing it: Windows recycles pids aggressively, so a bare pid is never enough.
    """
    wanted: List[int] = []
    for pid in pids:
        try:
            value = int(pid or 0)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in wanted:
            wanted.append(value)
    if not wanted:
        return {}
    if os.name == "nt":
        return _windows_command_lines(wanted)
    try:
        completed = subprocess.run(
            ["ps", "-o", "pid=,command=", "-p", ",".join(str(p) for p in wanted)],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    lines: Dict[int, str] = {}
    for row in completed.stdout.splitlines():
        parts = row.strip().split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        lines[pid] = parts[1] if len(parts) > 1 else ""
    return lines


def _windows_command_lines(pids: Sequence[int]) -> Dict[int, str]:
    filter_expr = " OR ".join(f"ProcessId={pid}" for pid in pids)
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f'Get-CimInstance Win32_Process -Filter "{filter_expr}" | '
        "Select-Object @{n='pid';e={$_.ProcessId}},@{n='cmd';e={$_.CommandLine}} | "
        "ConvertTo-Json -Compress"
    )
    output = ""
    for executable in ("powershell", "pwsh"):
        try:
            completed = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.stdout.strip():
            output = completed.stdout.strip()
            break
    if not output:
        return {}
    try:
        payload = json.loads(output)
    except ValueError:
        return {}
    rows = payload if isinstance(payload, list) else [payload]
    lines: Dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        lines[pid] = str(row.get("cmd") or "")
    return lines


def _normalize_command(text: str) -> str:
    text = clean_text(text).replace("\\", "/").replace('"', "").replace("'", "")
    return re.sub(r"\s+", " ", text).strip().lower()


def command_matches(recorded: str, actual: str) -> bool:
    """Does the live command line still look like the recorded MCP command?"""
    left = _normalize_command(recorded)
    right = _normalize_command(actual)
    if not left or not right:
        return False
    return left in right


# --- output materialisation ------------------------------------------------

_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/webm": "webm",
    "application/pdf": "pdf",
    "application/json": "json",
    "text/plain": "txt",
    "text/html": "html",
    "text/csv": "csv",
}


def _extension_for(mime: str) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[mime]
    subtype = mime.split("/")[-1]
    if subtype and re.fullmatch(r"[a-z0-9.+-]{1,8}", subtype):
        return subtype.replace("+", "")
    return "bin"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _prune_output_dir(output_dir: Path, limit: int = OUTPUT_DIR_FILE_LIMIT) -> None:
    """Keep the auto-saved result directory bounded.

    Only files this module named (``<timestamp>_...``) are candidates, so a user
    supplied ``output_dir`` never loses unrelated files.
    """
    try:
        entries = [p for p in Path(output_dir).iterdir() if p.is_file() and _OUTPUT_FILE.match(p.name)]
    except OSError:
        return
    if len(entries) <= limit:
        return
    entries.sort(key=_mtime)
    for path in entries[: len(entries) - limit]:
        try:
            path.unlink()
        except OSError:
            continue


def _write_bytes(output_dir: Path, server: str, tool: str, index: int, mime: str, raw: bytes) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    name = (
        f"{_timestamp()}_{_safe_name(server)}_up_{_safe_name(tool)}_"
        f"{index}_{secrets.token_hex(2)}.{_extension_for(mime)}"
    )
    path = directory / name
    path.write_bytes(raw)
    _prune_output_dir(directory)
    return path


def _write_json(output_dir: Path, server: str, tool: str, payload: Dict[str, Any]) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{_timestamp()}_{_safe_name(server)}_{_safe_name(tool)}_{secrets.token_hex(2)}.json"
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _prune_output_dir(directory)
    return path


def _materialize_payload(
    block_type: str,
    data: Any,
    mime: str,
    output_dir: Optional[Path],
    server: str,
    tool: str,
    index: int,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    raw = b""
    if isinstance(data, str) and data:
        try:
            raw = base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            raw = b""
    payload: Dict[str, Any] = {"type": block_type, "mimeType": mime or "", "bytes": len(raw)}
    if extra:
        payload.update(extra)
    if raw and output_dir is not None:
        try:
            payload["savedTo"] = str(_write_bytes(output_dir, server, tool, index, mime, raw))
        except OSError as exc:
            payload["omitted"] = True
            payload["saveError"] = clean_text(exc)
    else:
        # Never hand a megabyte of base64 to the model.
        payload["omitted"] = True
    return payload


def _normalize_resource(
    resource: Any,
    output_dir: Optional[Path],
    server: str,
    tool: str,
    index: int,
) -> Dict[str, Any]:
    uri = _model_field(resource, "uri") or _model_field(resource, "uri_template") or ""
    mime = _model_field(resource, "mime_type") or _model_field(resource, "mimeType") or ""
    text = _model_field(resource, "text")
    if isinstance(text, str):
        return {"type": "resource", "uri": str(uri), "mimeType": mime, "text": clean_text(text)}
    blob = _model_field(resource, "blob")
    if blob:
        return _materialize_payload("resource", blob, mime, output_dir, server, tool, index, {"uri": str(uri)})
    return {"type": "resource", "uri": str(uri), "mimeType": mime}


def normalize_content_blocks(
    blocks: Sequence[Any],
    *,
    server: str,
    tool: str,
    output_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Turn MCP content blocks into plain JSON, saving big binaries to disk."""
    result: List[Dict[str, Any]] = []
    for index, block in enumerate(blocks or ()):
        block_type = str(_model_field(block, "type") or "")
        if block_type == "text":
            result.append({"type": "text", "text": clean_text(_model_field(block, "text") or "")})
        elif block_type in ("image", "audio"):
            mime = _model_field(block, "mime_type") or _model_field(block, "mimeType") or ""
            result.append(
                _materialize_payload(
                    block_type, _model_field(block, "data"), mime, output_dir, server, tool, index
                )
            )
        elif block_type == "resource":
            result.append(_normalize_resource(_model_field(block, "resource"), output_dir, server, tool, index))
        elif block_type == "resource_link":
            result.append(
                {
                    "type": "resource_link",
                    "uri": str(_model_field(block, "uri") or ""),
                    "name": _model_field(block, "name"),
                    "title": _model_field(block, "title"),
                    "mimeType": _model_field(block, "mime_type") or _model_field(block, "mimeType"),
                    "description": _model_field(block, "description"),
                }
            )
        else:
            dumped: Any = {}
            if isinstance(block, dict):
                dumped = {k: v for k, v in block.items() if k not in ("data", "blob")}
            else:
                dump = getattr(block, "model_dump", None)
                if callable(dump):
                    try:
                        dumped = {k: v for k, v in dump(by_alias=True).items() if k not in ("data", "blob")}
                    except Exception:
                        dumped = {}
            entry = {"type": block_type or "unknown"}
            if isinstance(dumped, dict):
                entry.update({k: v for k, v in dumped.items() if k != "type"})
            result.append(entry)
    return result


def _truncate_blocks(blocks: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Cut inline content down to ``limit`` characters, keeping block structure."""
    limit = max(200, int(limit))
    kept: List[Dict[str, Any]] = []
    used = 0
    for block in blocks:
        piece = _dump(block)
        if used + len(piece) <= limit:
            kept.append(block)
            used += len(piece)
            continue
        text = block.get("text")
        if isinstance(text, str):
            room = max(0, limit - used - 120)
            if room:
                trimmed = dict(block)
                trimmed["text"] = text[:room]
                kept.append(trimmed)
                used += len(_dump(trimmed))
        else:
            kept.append({"type": block.get("type") or "unknown", "truncated": True})
            used += 40
        break
    return kept


def normalize_call_result(
    result: Any,
    *,
    server: str,
    tool: str,
    output_dir: Optional[Path] = None,
    max_inline_chars: int = DEFAULT_MAX_INLINE_CHARS,
) -> Dict[str, Any]:
    """Normalise a ``CallToolResult`` into the JSON the HTTP layer returns."""
    blocks = normalize_content_blocks(
        getattr(result, "content", None) or [], server=server, tool=tool, output_dir=output_dir
    )
    structured = getattr(result, "structured_content", None)
    is_error = bool(getattr(result, "is_error", False))
    payload: Dict[str, Any] = {
        "ok": True,
        "isError": is_error,
        "content": blocks,
        "structuredContent": structured,
        "truncated": False,
        "savedTo": None,
    }
    inline_len = len(_dump({"content": blocks, "structuredContent": structured}))
    if inline_len > max_inline_chars:
        saved_to: Optional[str] = None
        if output_dir is not None:
            try:
                saved_to = str(
                    _write_json(
                        output_dir,
                        server,
                        tool,
                        {
                            "server": server,
                            "tool": tool,
                            "isError": is_error,
                            "savedAt": _now_iso(),
                            "content": blocks,
                            "structuredContent": structured,
                        },
                    )
                )
            except OSError:
                saved_to = None
        trimmed = _truncate_blocks(blocks, max_inline_chars - 600)
        marker = (
            f"[result truncated: full output saved to {saved_to}]"
            if saved_to
            else "[result truncated: output exceeded the inline limit and output_dir was not provided]"
        )
        trimmed.append({"type": "text", "text": marker})
        payload["content"] = trimmed
        payload["truncated"] = True
        payload["savedTo"] = saved_to
        if structured is not None and len(_dump(structured)) > max_inline_chars:
            payload["structuredContent"] = None
            payload["structuredTruncated"] = True
    return payload


# --- actor ----------------------------------------------------------------


class _Command:
    """One queued actor request. ``future`` carries the caller's result."""

    __slots__ = ("kind", "future", "payload")

    def __init__(
        self,
        kind: str,
        future: Optional[asyncio.Future] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.kind = kind
        self.future = future
        self.payload = payload or {}


class _ConnectFailed(Exception):
    """A connection round could not be established."""

    def __init__(self, error: Dict[str, Any], *, fatal: bool = False) -> None:
        super().__init__(error["error"]["message"])
        self.error = error
        self.fatal = fatal


def _consume_task_result(task: Any) -> None:
    """Never let an abandoned task log "exception was never retrieved"."""
    try:
        if not task.cancelled():
            task.exception()
    except BaseException:
        pass


async def _bounded(awaitable: Any, timeout: float) -> bool:
    """Run ``awaitable`` for at most ``timeout`` seconds; True when it finished on its own.

    ``asyncio.wait_for`` is not enough here: it *awaits* the cancellation of a coroutine that
    ignores cancellation (a cancel scope stuck behind a half-finished dial, a transport close
    waiting for the server), so a "bounded" close can still hang.  This cancels and moves on.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=max(0.0, float(timeout)))
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        _consume_task_result(task)
        return True
    task.add_done_callback(_consume_task_result)
    task.cancel()
    try:
        await asyncio.wait({task}, timeout=0.2)
    except asyncio.CancelledError:
        pass
    return False


async def _gather_within(tasks: Sequence[Any], deadline: float) -> bool:
    """Await every task inside the remaining budget; cancelled (not awaited) when it runs out."""
    pending = [task for task in tasks if task is not None]
    if not pending:
        return True
    remaining = max(0.0, deadline - time.monotonic())
    gather = asyncio.gather(*pending, return_exceptions=True)
    return await _bounded(gather, remaining)


async def _close_transport(transport: Any, timeout: Optional[float] = None) -> None:
    """Close a transport without letting a half-finished dial hold the caller hostage.

    ``StdioTransport.close()`` waits for its connect task; that task cannot return
    until the *server* finishes the handshake.  Bounding the wait keeps ``close``
    and ``reap_idle`` responsive; the pid net below reaps anything still alive.
    """
    if transport is None:
        return
    budget = TRANSPORT_CLOSE_TIMEOUT_SEC if timeout is None else max(0.2, float(timeout))
    try:
        await _bounded(transport.close(), budget)
    except asyncio.CancelledError:
        pass


class ServerActor:
    """Owns exactly one long-lived connection to one MCP server.

    The connection lives inside :meth:`_actor_main`'s task: entering and exiting
    ``async with client`` from the same task keeps anyio cancel scopes balanced.
    Requests arrive through a queue, run as child tasks (bounded by
    ``max_concurrency``) and answer their own future.
    """

    def __init__(
        self,
        name: str,
        spec: Dict[str, Any],
        cwd: Optional[str] = None,
        init_timeout_sec: float = 120.0,
        default_call_timeout_sec: float = 60.0,
        idle_timeout_sec: float = 1800.0,
        max_concurrency: int = 4,
        on_pid_change: Optional[Callable[[], Any]] = None,
        output_dir: Optional[Path] = None,
        max_inline_chars: int = DEFAULT_MAX_INLINE_CHARS,
    ) -> None:
        self.name = str(name)
        self.spec = dict(spec or {})
        # Never let a header/env value reach a caller: an upstream server can echo the credential
        # it was given in its own error text, and the pool must not hand that text over as-is.
        self._secrets = spec_secrets(self.spec)
        self.cwd = cwd
        self.init_timeout_sec = float(init_timeout_sec)
        self.default_call_timeout_sec = float(default_call_timeout_sec)
        self.idle_timeout_sec = float(idle_timeout_sec)
        self.max_concurrency = max(1, int(max_concurrency))
        self.on_pid_change = on_pid_change
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.max_inline_chars = int(max_inline_chars)

        self.status = "idle"
        self.tool_count: Optional[int] = None
        self.last_error: Optional[str] = None
        self.last_used_at = time.time()
        self.pid: Optional[int] = None

        self._queue: asyncio.Queue = asyncio.Queue()
        self._break_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[Client] = None
        self._transport: Optional[Any] = None
        self._inflight: Set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._close_requested = False
        self._stash: Optional[_Command] = None
        self._recorder = _SpawnRecorder(self._on_spawned_pid)
        # The command line this actor really spawns (after Windows command resolution), so a
        # leftover pid can be proved to belong to this server before anything is killed.
        self._recorded_cmd: str = ""
        self._handled_pids: Set[int] = set()
        self._reap_tasks: Dict[int, asyncio.Task] = {}
        self._verdicts: Dict[int, asyncio.Task] = {}
        self._verdict_results: Dict[int, bool] = {}
        self._tools: List[Dict[str, Any]] = []
        self._last_transport_error: Optional[str] = None
        self._stale_failures = 0
        install_spawn_hook()

    # -- introspection -----------------------------------------------------

    @property
    def transport_kind(self) -> str:
        return "http" if "url" in self.spec else "stdio"

    @property
    def command_line(self) -> str:
        if self.transport_kind == "http":
            return str(self.spec.get("url") or "")
        parts = [str(self.spec.get("command") or "")]
        parts.extend(str(arg) for arg in (self.spec.get("args") or []))
        return " ".join(part for part in parts if part)

    @property
    def inflight(self) -> int:
        return sum(1 for task in self._inflight if not task.done())

    def snapshot_status(self, shared: bool = False) -> Dict[str, Any]:
        """Public status view.  Never carries ``headers``/``env`` values."""
        return {
            "name": self.name,
            "transport": self.transport_kind,
            "status": self.status,
            "tool_count": self.tool_count,
            "last_error": _clip(clean_text(self._scrub(self.last_error))) if self.last_error else None,
            "shared": bool(shared),
            "pid": self.pid,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ServerActor {self.name} {self.transport_kind} {self.status} pid={self.pid}>"

    # -- client facing -----------------------------------------------------

    async def list_tools(self) -> Dict[str, Any]:
        return await self._submit("list_tools")

    async def call_tool(
        self,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        return await self._submit(
            "call_tool",
            {
                "tool": str(tool),
                "arguments": dict(arguments or {}),
                "timeout_sec": float(timeout_sec or self.default_call_timeout_sec),
            },
        )

    async def _submit(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self._close_requested or self.status == "closed":
            return error_result("session_closed", "MCP 连接池已关闭，请重新建立会话池。", server=self.name)
        self._ensure_task()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put(_Command(kind, future, payload))
        try:
            return await future
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise

    def _ensure_task(self) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.get_running_loop().create_task(
            self._actor_main(), name=f"mcp-actor-{self.name}"
        )
        return self._task

    # -- transports --------------------------------------------------------

    def _build_transport(self) -> Any:
        if self.transport_kind == "http":
            headers = self.spec.get("headers") if isinstance(self.spec.get("headers"), dict) else None
            return StreamableHttpTransport(url=str(self.spec.get("url")), headers=dict(headers or {}) or None)
        command = str(self.spec.get("command") or "")
        if os.name == "nt" and not os.path.isabs(command):
            try:
                from mcp.os.win32.utilities import get_windows_executable_command

                command = get_windows_executable_command(command) or command
            except Exception:  # pragma: no cover - resolution is best effort
                pass
        args = [str(arg) for arg in (self.spec.get("args") or [])]
        # Recorded *after* resolution: the live process shows the resolved executable, and an
        # ownership check compares substring-normalised command lines.
        self._recorded_cmd = " ".join(part for part in [command, *args] if part)
        env = dict(get_default_environment())
        extra_env = self.spec.get("env") if isinstance(self.spec.get("env"), dict) else None
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
        return StdioTransport(
            command=command,
            args=args,
            env=env,
            cwd=self.cwd,
            keep_alive=False,
        )

    # -- actor loop --------------------------------------------------------

    async def _actor_main(self) -> None:
        try:
            while True:
                command = self._stash
                self._stash = None
                if command is None:
                    command = await self._queue.get()
                if command.kind == "close":
                    self._resolve(command, error_result("session_closed", "MCP 连接池已关闭。", server=self.name))
                    break
                if self._close_requested:
                    self._resolve(command, error_result("session_closed", "MCP 连接池已关闭。", server=self.name))
                    break
                outcome = await self._run_round(command)
                if outcome == _CLOSE:
                    break
                # _RETRY/_BROKEN: keep the error visible and wait for the next request.
        finally:
            await self._cancel_inflight()
            await self._shutdown_transport()
            self._drain_queue("MCP 连接池已关闭。")
            if self.status != "closed":
                self.status = "closed"

    async def _run_round(self, first: _Command) -> object:
        """Try to serve ``first`` on one connection, retrying a failed dial."""
        attempts = 0
        while True:
            if self._close_requested:
                self._resolve(first, error_result("session_closed", "MCP 连接池已关闭。", server=self.name))
                return _CLOSE
            try:
                return await self._hold_connection(first)
            except _ConnectFailed as failed:
                await self._shutdown_transport()
                self.status = "error"
                self.last_error = self._scrub(failed.error["error"]["message"])
                attempts += 1
                if failed.fatal or attempts >= 3:
                    self._resolve(first, self._scrub_result(failed.error))
                    return _RETRY
                await asyncio.sleep(0.25 * attempts)

    async def _hold_connection(self, first: _Command) -> object:
        transport = self._build_transport()
        client = Client(
            transport,
            name=self.name,
            timeout=self.default_call_timeout_sec,
            init_timeout=self.init_timeout_sec,
        )
        # Publish the transport before dialling: a cancel/close that lands while the
        # handshake is still running must still be able to close it (and then the pid
        # net below kills whatever the cancelled dial left behind).
        self._transport = transport
        token = _SPAWN_RECORDER.set(self._recorder)
        try:
            await client.__aenter__()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _ConnectFailed(
                classify_exception(exc, self.name),
                fatal=isinstance(exc, (FileNotFoundError, PermissionError, ValueError)),
            )
        finally:
            _SPAWN_RECORDER.reset(token)

        self._client = client
        self._break_event.clear()
        self.status = "connected"
        self._last_transport_error = None
        self._stale_failures = 0
        self._record_pid(announce=True)
        try:
            tools = await client.list_tools()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = self._scrub(_exception_message(exc))
            await self._exit_client(client, transport)
            self._client = None
            self._transport = None
            raise _ConnectFailed(classify_exception(exc, self.name))
        self._set_tools(tools)
        try:
            await self._dispatch(first)
            while True:
                event = await self._next_event()
                if event is _CLOSE:
                    return _CLOSE
                if event is _BROKEN:
                    self.status = "error"
                    self.last_error = self._last_transport_error or "MCP 连接已断开"
                    return _BROKEN
                await self._dispatch(event)
        finally:
            await self._cancel_inflight()
            await self._exit_client(client, transport)
            self._client = None
            self._transport = None

    async def _next_event(self) -> object:
        """Wait for the next command, a broken connection, or a close request."""
        command_task: asyncio.Task = asyncio.get_running_loop().create_task(self._queue.get())
        broken_task: asyncio.Task = asyncio.get_running_loop().create_task(self._break_event.wait())
        try:
            done, _ = await asyncio.wait({command_task, broken_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (command_task, broken_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(command_task, broken_task, return_exceptions=True)

        command: Optional[_Command] = None
        if command_task.done() and not command_task.cancelled() and command_task.exception() is None:
            command = command_task.result()

        if self._close_requested:
            if command is not None:
                self._resolve(command, error_result("session_closed", "MCP 连接池已关闭。", server=self.name))
            return _CLOSE
        if broken_task in done or self._break_event.is_set():
            if command is not None:
                self._stash = command
            return _BROKEN
        if command is None:  # pragma: no cover - defensive
            return _BROKEN
        if command.kind == "close":
            self._close_requested = True
            self._resolve(command, error_result("session_closed", "MCP 连接池已关闭。", server=self.name))
            return _CLOSE
        return command

    async def _dispatch(self, command: _Command) -> None:
        task: asyncio.Task = asyncio.get_running_loop().create_task(self._execute(command))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _execute(self, command: _Command) -> None:
        try:
            async with self._semaphore:
                connected_at_start = self.status == "connected"
                try:
                    result = await self._perform(command)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive
                    result = classify_exception(exc, self.name)
                stale = (
                    not result.get("ok")
                    and (result.get("error") or {}).get("kind") == "connect"
                    and connected_at_start
                )
                # Answer the caller *before* flagging the break, so the parent's
                # teardown can never turn a delivered failure into "cancelled".
                self.last_used_at = time.time()
                result = self._scrub_result(result)
                self._resolve(command, result)
                if stale:
                    self._last_transport_error = result["error"]["message"]
                    self._stale_failures += 1
                    self._break_event.set()
        except asyncio.CancelledError:
            self._resolve(command, error_result("cancelled", "调用已取消。", server=self.name))
            raise

    def claim_stale_failure(self) -> bool:
        """One stale connection can be retried once; the pool asks before retrying."""
        if self._stale_failures <= 0:
            return False
        self._stale_failures -= 1
        return True

    async def _perform(self, command: _Command) -> Dict[str, Any]:
        client = self._client
        if client is None:
            return error_result("connect", "MCP 客户端尚未连接。", server=self.name)
        if command.kind == "list_tools":
            tools = await client.list_tools()
            self._set_tools(tools)
            return {"ok": True, "server": self.name, "toolCount": len(self._tools), "tools": list(self._tools)}
        if command.kind == "call_tool":
            tool = str(command.payload.get("tool") or "")
            arguments = command.payload.get("arguments") or {}
            timeout_sec = float(command.payload.get("timeout_sec") or self.default_call_timeout_sec)
            raw = await client.call_tool(tool, arguments, timeout=timeout_sec, raise_on_error=False)
            body = normalize_call_result(
                raw, server=self.name, tool=tool, output_dir=self.output_dir, max_inline_chars=self.max_inline_chars
            )
            result: Dict[str, Any] = {
                "ok": True,
                "server": self.name,
                "tool": tool,
                "isError": body["isError"],
                "content": body["content"],
                "structuredContent": body["structuredContent"],
                "truncated": body["truncated"],
                "savedTo": body["savedTo"],
            }
            if body.get("structuredTruncated"):
                result["structuredTruncated"] = True
            if body["isError"]:
                text = _first_text(body["content"])
                kind = "unknown_tool" if "unknown tool" in text.lower() else "tool_error"
                result["error"] = {
                    "kind": kind,
                    "message": _clip(text or f"工具 {tool} 返回错误"),
                    "hint": ERROR_HINTS[kind],
                    "server": self.name,
                }
            return result
        return error_result("bad_request", f"未知的 actor 命令：{command.kind}", server=self.name)

    def _set_tools(self, tools: Sequence[Any]) -> None:
        entries: List[Dict[str, Any]] = []
        for tool in tools or ():
            schema = _model_field(tool, "input_schema")
            if schema is None:
                schema = _model_field(tool, "inputSchema")
            entries.append(
                {
                    "name": _model_field(tool, "name"),
                    "title": _model_field(tool, "title"),
                    "description": _model_field(tool, "description"),
                    "inputSchema": schema,
                }
            )
        self._tools = entries
        self.tool_count = len(entries)

    def _resolve(self, command: Optional[_Command], result: Dict[str, Any]) -> None:
        if command is None or command.future is None:
            return
        if not command.future.done():
            command.future.set_result(result)

    def _drain_queue(self, message: str) -> None:
        while True:
            try:
                command = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._resolve(command, error_result("session_closed", message, server=self.name))
        if self._stash is not None:
            self._resolve(self._stash, error_result("session_closed", message, server=self.name))
            self._stash = None

    async def _cancel_inflight(self) -> None:
        tasks = [task for task in self._inflight if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _exit_client(self, client: Client, transport: Any, timeout: Optional[float] = None) -> None:
        budget = TRANSPORT_CLOSE_TIMEOUT_SEC if timeout is None else max(0.2, float(timeout))
        try:
            # anyio's cancel scopes make a handshake-stuck exit unbounded; the strict budget and
            # the pid net are what keep a shutdown finite.
            await _bounded(client.__aexit__(None, None, None), budget)
        except asyncio.CancelledError:
            pass
        await _close_transport(transport, budget)
        await self._kill_leftover()

    async def _shutdown_transport(self, timeout: Optional[float] = None) -> None:
        transport, self._transport = self._transport, None
        client, self._client = self._client, None
        budget = TRANSPORT_CLOSE_TIMEOUT_SEC if timeout is None else max(0.2, float(timeout))
        if client is not None:
            try:
                await _bounded(client.__aexit__(None, None, None), budget)
            except asyncio.CancelledError:
                pass
        await _close_transport(transport, budget)
        await self._kill_leftover()

    def _verdict_task(self, pid: int, recorded: str) -> asyncio.Task:
        """One ``_owns_pid`` query per pid, shared by the early kick-off and the kill."""
        task = self._verdicts.get(pid)
        if task is None or task.done():
            task = asyncio.ensure_future(self._owns_pid(pid, recorded))
            self._verdicts[pid] = task
            task.add_done_callback(_consume_task_result)
        return task

    def _start_verdict(self) -> None:
        """Kick off the ownership check early: its PowerShell query is the slow part of a reap."""
        pid = self.pid or self._recorder.last
        if not pid or self.transport_kind != "stdio":
            return
        pid = int(pid)
        if pid in self._verdict_results:
            return
        self._verdict_task(pid, self._recorded_cmd or self.command_line)

    async def _ownership(self, pid: int, recorded: str) -> bool:
        """The verdict, queried at most once per pid (a hard-budget cancel must not re-query)."""
        if pid in self._verdict_results:
            return self._verdict_results[pid]
        task = self._verdict_task(pid, recorded)
        owned = bool(await asyncio.shield(task))
        self._verdict_results[pid] = owned
        return owned

    async def _kill_leftover(self) -> None:
        """Kill the child this actor spawned, but only after proving the pid is still its own.

        Windows recycles pids quickly: after the child exited, the recorded pid can belong to an
        unrelated process, and ``taskkill /T /F`` would then take out an innocent tree.  The live
        command line must therefore still contain the command this actor spawned.

        The verdict runs as its own task: one query per pid, started as soon as a close begins so
        the slow PowerShell lookup overlaps the transport teardown instead of following it.
        """
        pid = self.pid or self._recorder.last
        if not pid or self.transport_kind != "stdio":
            return
        pid = int(pid)
        if pid in self._handled_pids:
            return
        task = self._reap_tasks.get(pid)
        if task is None or task.done():
            task = asyncio.ensure_future(self._verify_and_kill(pid))
            self._reap_tasks[pid] = task
        try:
            # Shielded: the verdict outlives a budget-driven cancellation of this caller.
            await asyncio.shield(task)
        finally:
            if task.done():
                self._reap_tasks.pop(pid, None)

    async def _verify_and_kill(self, pid: int) -> None:
        try:
            owned = await self._ownership(pid, self._recorded_cmd or self.command_line)
            if not owned:
                return
            # A pid can die (and be reused) while the query ran: re-check before killing.
            if not await asyncio.to_thread(pid_alive, pid):
                return
            await asyncio.to_thread(kill_pid_tree, pid)
        except (RuntimeError, OSError):  # pragma: no cover - loop shutting down
            pass
        finally:
            self._handled_pids.add(pid)

    async def _owns_pid(self, pid: int, recorded: str) -> bool:
        """True only when the pid is alive and its command line still matches what we spawned."""
        try:
            if not await asyncio.to_thread(pid_alive, pid):
                return False
            lines = await asyncio.to_thread(query_command_lines, [pid])
        except (RuntimeError, OSError, subprocess.SubprocessError):
            lines = {}
        actual = str((lines or {}).get(int(pid)) or "")
        if command_matches(recorded, actual):
            return True
        logger.warning(
            "[%s] refusing to kill pid %s: its command line no longer matches %r (live: %r)",
            self.name, pid, recorded, _clip(actual, 200) or "<unavailable>",
        )
        return False

    def _record_pid(self, *, announce: bool) -> None:
        if self.transport_kind != "stdio":
            return
        new_pid = self._recorder.last
        if new_pid is None or new_pid == self.pid:
            return
        self.pid = new_pid
        if announce:
            self._announce_pid()

    def _on_spawned_pid(self, pid: int) -> None:
        """Called by the spawn hook the moment a stdio child exists (before any handshake)."""
        if self.transport_kind != "stdio":
            return
        if self.pid == pid:
            return
        self.pid = int(pid)
        self._handled_pids.discard(int(pid))
        self._announce_pid()

    def _announce_pid(self) -> None:
        """Persist the pid ledger right now, so a Hub crash cannot lose a spawned child."""
        if self.on_pid_change is None:
            return
        try:
            outcome = self.on_pid_change()
        except Exception:
            return
        if asyncio.iscoroutine(outcome):
            try:
                asyncio.get_running_loop().create_task(outcome)
            except RuntimeError:  # pragma: no cover
                pass

    def _scrub(self, text: Any) -> Any:
        """This server's own header/env values removed from any text the caller may read."""
        return scrub_secrets(text, self._secrets)

    def _scrub_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Strip secrets from the error fields (and a failed tool's own text) before answering."""
        if not self._secrets or not isinstance(result, dict):
            return result
        error = result.get("error")
        if isinstance(error, dict):
            for key in ("message", "hint"):
                value = error.get(key)
                if isinstance(value, str):
                    error[key] = scrub_secrets(value, self._secrets)
        if result.get("isError") or not result.get("ok"):
            for block in result.get("content") or ():
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = scrub_secrets(block["text"], self._secrets)
        return result

    # -- shutdown ----------------------------------------------------------

    async def close(self, timeout: float = 5.0) -> None:
        """Stop the actor inside a hard monotonic budget; a stuck close ends in a verified kill."""
        if self.status == "closed" and self._task is None:
            return
        deadline = time.monotonic() + max(0.3, float(timeout))
        self._close_requested = True
        self._break_event.set()
        # Start the (slow, read-only) ownership query now; the kill itself waits for teardown.
        self._start_verdict()
        await self._queue.put(_Command("close", None, None))
        task, self._task = self._task, None
        if task is not None and not task.done():
            # Graceful first: the actor answers the close command and exits by itself.  A dial
            # that never answered has nothing to be graceful about -- cancel it right away.
            graceful = 0.0
            if self.status != "connecting":
                graceful = max(0.0, min((deadline - time.monotonic()) * 0.5, 1.0))
            if graceful:
                await asyncio.wait({task}, timeout=graceful)
            if not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=min(0.3, max(0.0, deadline - time.monotonic())))
        await self._shutdown_transport(timeout=max(0.2, deadline - time.monotonic()))
        self._drain_queue("MCP 连接池已关闭。")
        self.status = "closed"

    async def force_reap(self) -> None:
        """Hard-budget last resort: stop waiting for the actor and kill the child now."""
        self._close_requested = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        try:
            await self._kill_leftover()
        except asyncio.CancelledError:
            pass
        self._drain_queue("MCP 连接池已关闭。")
        self.status = "closed"


# --- pool -----------------------------------------------------------------


@dataclass
class _Job:
    id: str
    server: str
    tool: str
    kind: str
    status: str = "running"
    created_at: float = 0.0
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    task: Optional[asyncio.Task] = None


class SessionPool:
    """Every MCP server plus the async jobs of one Hub session."""

    def __init__(
        self,
        session_id: str,
        servers: Dict[str, Dict[str, Any]],
        cwd: Optional[str] = None,
        output_dir: Optional[Path] = None,
        jobs_dir: Optional[Path] = None,
        init_timeout_sec: float = 120.0,
        default_call_timeout_sec: float = 60.0,
        idle_timeout_sec: float = 1800.0,
        job_max_sec: float = 900.0,
        on_pid_change: Optional[Callable[[], Any]] = None,
        on_call_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
        max_inline_chars: int = DEFAULT_MAX_INLINE_CHARS,
    ) -> None:
        self.session_id = str(session_id)
        self.servers = {str(name): dict(spec or {}) for name, spec in (servers or {}).items()}
        self.cwd = cwd
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.jobs_dir = Path(jobs_dir) if jobs_dir is not None else None
        self.init_timeout_sec = float(init_timeout_sec)
        self.default_call_timeout_sec = float(default_call_timeout_sec)
        self.idle_timeout_sec = float(idle_timeout_sec)
        self.job_max_sec = float(job_max_sec)
        self.on_pid_change = on_pid_change
        self.on_call_event = on_call_event
        self.max_inline_chars = int(max_inline_chars)
        # The session's own credential values, unioned: a pool-level error or event must never
        # carry a value one of this session's servers was configured with.
        secrets: List[str] = []
        for spec in self.servers.values():
            for secret in spec_secrets(spec):
                if secret not in secrets:
                    secrets.append(secret)
        self._secrets = sorted(secrets, key=len, reverse=True)
        self.status = "open"

        self.actors: Dict[str, ServerActor] = {}
        # Actors a running close_all already detached from ``actors``: a hard-budget backstop
        # still has to be able to reap their children.
        self._closing_actors: List[ServerActor] = []
        self._semaphore = asyncio.Semaphore(SESSION_CONCURRENCY)
        self._jobs: Dict[str, _Job] = {}
        self._job_order: List[str] = []
        self._background: Set[asyncio.Task] = set()
        self.shared_urls_provider: Optional[Callable[[], Set[str]]] = None

    # -- introspection -----------------------------------------------------

    def http_urls(self) -> Set[str]:
        return {
            str(spec.get("url"))
            for spec in self.servers.values()
            if isinstance(spec, dict) and spec.get("url")
        }

    def _actor(self, name: str) -> ServerActor:
        actor = self.actors.get(name)
        if actor is None:
            actor = ServerActor(
                name,
                self.servers[name],
                cwd=self.cwd,
                init_timeout_sec=self.init_timeout_sec,
                default_call_timeout_sec=self.default_call_timeout_sec,
                idle_timeout_sec=self.idle_timeout_sec,
                on_pid_change=self.on_pid_change,
                output_dir=self.output_dir,
                max_inline_chars=self.max_inline_chars,
            )
            self.actors[name] = actor
        return actor

    def _status_for(self, name: str, shared_urls: Optional[Set[str]] = None) -> Dict[str, Any]:
        actor = self.actors.get(name)
        if actor is not None:
            url = self.servers[name].get("url")
            shared = bool(shared_urls and url and str(url) in shared_urls)
            return actor.snapshot_status(shared=shared)
        spec = self.servers[name]
        url = spec.get("url")
        return {
            "name": name,
            "transport": "http" if "url" in spec else "stdio",
            "status": "idle",
            "tool_count": None,
            "last_error": None,
            "shared": bool(shared_urls and url and str(url) in shared_urls),
            "pid": None,
        }

    def list_servers(self, shared_urls: Optional[Set[str]] = None) -> Dict[str, Any]:
        if shared_urls is None and self.shared_urls_provider is not None:
            try:
                shared_urls = self.shared_urls_provider()
            except Exception:
                shared_urls = None
        return {
            "ok": True,
            "sessionId": self.session_id,
            "servers": [self._status_for(name, shared_urls) for name in self.servers],
        }

    def active_pid_records(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for name, actor in list(self.actors.items()):
            pid = actor.pid
            if not pid:
                continue
            try:
                if not pid_alive(pid):
                    continue
            except Exception:  # pragma: no cover - defensive
                continue
            records.append(
                {
                    "pid": int(pid),
                    "session_id": self.session_id,
                    "server": name,
                    "command": actor.command_line,
                }
            )
        return records

    # -- warm start --------------------------------------------------------

    async def prefetch(
        self, budget_sec: float = 2.0, start_connections: bool = True
    ) -> Dict[str, Optional[int]]:
        """Best-effort warm-up.

        A slow ``npx`` server (2s+) is *not* killed when the budget runs out: the
        dial keeps running in the background and the call reports ``None`` until
        it knows better.
        """
        if self.status == "closed":
            return {name: None for name in self.servers}
        tasks: List[asyncio.Task] = []
        if start_connections:
            for name in self.servers:
                actor = self._actor(name)
                if actor.status == "connected" and actor.tool_count is not None:
                    continue
                task: asyncio.Task = asyncio.get_running_loop().create_task(
                    actor.list_tools(), name=f"mcp-prefetch-{name}"
                )
                self._background.add(task)
                task.add_done_callback(self._background.discard)
                tasks.append(task)
        if tasks and budget_sec and budget_sec > 0:
            await asyncio.wait(tasks, timeout=float(budget_sec))
        return {name: (self.actors[name].tool_count if name in self.actors else None) for name in self.servers}

    # -- simple actions ----------------------------------------------------

    async def ping_server(self, server: str, timeout_ms: Optional[int] = None) -> Dict[str, Any]:
        if server not in self.servers:
            return error_result("unknown_server", f"未知的 MCP 服务器：{server}", server=server)
        actor = self.actors.get(server) or self._actor(server)
        started = time.perf_counter()
        try:
            result = await self._with_timeout(actor.list_tools(), timeout_ms)
        except asyncio.TimeoutError:
            return error_result("timeout", f"连接服务器 {server} 超时。", server=server)
        if not result.get("ok"):
            result.setdefault("error", {})["server"] = server
            return result
        return {
            "ok": True,
            "server": server,
            "toolCount": int(result.get("toolCount") or 0),
            "durationMs": int((time.perf_counter() - started) * 1000),
        }

    async def list_tools(self, server: str, timeout_ms: Optional[int] = None) -> Dict[str, Any]:
        if server not in self.servers:
            return error_result("unknown_server", f"未知的 MCP 服务器：{server}", server=server)
        actor = self._actor(server)
        try:
            result = await self._with_timeout(actor.list_tools(), timeout_ms)
        except asyncio.TimeoutError:
            return error_result("timeout", f"读取 {server} 的工具清单超时。", server=server)
        if not result.get("ok"):
            result.setdefault("error", {})["server"] = server
            return result
        return {"ok": True, "server": server, "tools": list(result.get("tools") or [])}

    async def _with_timeout(self, awaitable: Any, timeout_ms: Optional[int]) -> Any:
        if timeout_ms is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=max(0.001, float(int(timeout_ms)) / 1000.0))

    # -- calls and jobs ----------------------------------------------------

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        timeout_ms: Optional[int] = None,
        wait_ms: int = 30000,
    ) -> Dict[str, Any]:
        if not isinstance(server, str) or server not in self.servers:
            return error_result("unknown_server", f"未知的 MCP 服务器：{server}", server=server if isinstance(server, str) else None)
        if not isinstance(tool, str) or not tool.strip():
            return error_result("bad_request", "字段 tool 必须是非空字符串。", server=server)
        if arguments is not None and not isinstance(arguments, dict):
            return error_result("bad_request", "字段 arguments 必须是 JSON 对象。", server=server)
        if timeout_ms is not None and not _is_int(timeout_ms):
            return error_result("bad_request", "字段 timeoutMs 必须是正整数毫秒。", server=server)
        if not _is_int(wait_ms) or int(wait_ms) < 0:
            return error_result("bad_request", "字段 waitMs 必须是非负整数毫秒。", server=server)
        if self.status == "closed":
            return error_result("session_closed", "会话已结束，无法再调用 MCP 工具。", server=server)

        timeout_sec = self._call_timeout(timeout_ms)
        job = self._new_job(server, tool, "call")
        job.task = asyncio.get_running_loop().create_task(
            self._run_call_job(job, arguments or {}, timeout_sec), name=f"mcp-job-{job.id}"
        )
        return await self._await_job(job, wait_ms)

    async def _run_call_job(self, job: _Job, arguments: Dict[str, Any], timeout_sec: float) -> None:
        try:
            async with self._semaphore:
                result = await self._execute_single(job.server, job.tool, arguments, timeout_sec)
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.finished_at = time.time()
            job.result = {
                "ok": False,
                "status": "cancelled",
                "server": job.server,
                "tool": job.tool,
                "error": {
                    "kind": "cancelled",
                    "message": "调用已取消。",
                    "hint": ERROR_HINTS["cancelled"],
                    "server": job.server,
                },
            }
            raise
        except Exception as exc:  # pragma: no cover - defensive
            result = self._scrub_error(classify_exception(exc, job.server))
            result.update({"status": "done", "server": job.server, "tool": job.tool})
        else:
            result = dict(result, status="done")
            if result.get("ok") and "server" not in result:
                result["server"] = job.server
            if result.get("ok") and "tool" not in result:
                result["tool"] = job.tool
        job.result = result
        job.status = "done"
        job.finished_at = time.time()
        self._persist_job(job)
        self._emit_call_event(job, result)

    async def _execute_single(
        self, server: str, tool: str, arguments: Dict[str, Any], timeout_sec: float
    ) -> Dict[str, Any]:
        if server not in self.servers:
            return error_result("unknown_server", f"未知的 MCP 服务器：{server}", server=server)
        actor = self._actor(server)
        started = time.perf_counter()
        result = await actor.call_tool(tool, arguments, timeout_sec=timeout_sec)
        if (
            not result.get("ok")
            and (result.get("error") or {}).get("kind") == "connect"
            and actor.claim_stale_failure()
        ):
            # The long-lived connection died while idle (server crash / restart).  One
            # transparent retry on the freshly rebuilt client keeps the call successful
            # instead of surfacing an error the caller cannot act on.
            result = await actor.call_tool(tool, arguments, timeout_sec=timeout_sec)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if result.get("ok"):
            payload: Dict[str, Any] = {
                "ok": True,
                "isError": bool(result.get("isError")),
                "content": result.get("content") or [],
                "structuredContent": result.get("structuredContent"),
                "truncated": bool(result.get("truncated")),
                "savedTo": result.get("savedTo"),
                "durationMs": duration_ms,
                "server": server,
                "tool": tool,
                **({"error": result["error"]} if result.get("error") else {}),
            }
            return self._scrub_error(payload)
        error = result.get("error") or {}
        error.setdefault("server", server)
        return self._scrub_error({"ok": False, "error": error, "server": server, "tool": tool,
                                  "durationMs": duration_ms})

    def _scrub_error(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Session-wide belt and braces: secrets out of an error's text and a failed tool's text."""
        if not self._secrets or not isinstance(result, dict):
            return result
        error = result.get("error")
        if isinstance(error, dict):
            for key in ("message", "hint"):
                value = error.get(key)
                if isinstance(value, str):
                    error[key] = scrub_secrets(value, self._secrets)
        if result.get("isError") or not result.get("ok"):
            for block in result.get("content") or ():
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = scrub_secrets(block["text"], self._secrets)
        return result

    def _call_timeout(self, timeout_ms: Optional[int]) -> float:
        if timeout_ms is None:
            return max(1.0, min(self.default_call_timeout_sec, self.job_max_sec))
        return max(0.05, min(float(int(timeout_ms)) / 1000.0, self.job_max_sec))

    def _new_job(self, server: str, tool: str, kind: str) -> _Job:
        job = _Job(
            id=f"job_{secrets.token_hex(6)}",
            server=server,
            tool=tool,
            kind=kind,
            created_at=time.time(),
        )
        self._jobs[job.id] = job
        self._job_order.append(job.id)
        self._trim_jobs()
        return job

    def _trim_jobs(self) -> None:
        while len(self._job_order) > JOB_MEMORY_LIMIT:
            job_id = self._job_order.pop(0)
            job = self._jobs.get(job_id)
            if job is not None and job.task is not None and not job.task.done():
                self._job_order.append(job_id)
                return
            self._jobs.pop(job_id, None)

    async def _await_job(self, job: _Job, wait_ms: int = 0) -> Dict[str, Any]:
        wait = max(0, int(wait_ms or 0)) / 1000.0
        task = job.task
        if task is not None and not task.done() and wait > 0:
            # ``asyncio.wait`` neither cancels the job nor turns its cancellation
            # into the caller's cancellation, which ``wait_for(shield(...))`` would.
            await asyncio.wait({task}, timeout=wait)
        return self._job_view(job)

    def _job_view(self, job: _Job, *, include_job: bool = False) -> Dict[str, Any]:
        finished = job.task is not None and job.task.done()
        if job.status == "cancelled" or (finished and job.status == "cancelled"):
            view = {
                "ok": True,
                "status": "cancelled",
                "server": job.server,
                "tool": job.tool,
                "durationMs": int(((job.finished_at or time.time()) - job.created_at) * 1000),
            }
        elif finished and job.result is not None:
            view = dict(job.result)
            view.setdefault("status", "done")
        else:
            view = {
                "ok": True,
                "status": "running",
                "server": job.server,
                "tool": job.tool,
                "elapsedMs": int((time.time() - job.created_at) * 1000),
            }
        if include_job:
            view["job"] = job.id
        elif view.get("status") == "running":
            view["job"] = job.id
        return view

    async def get_job(self, job_id: str, wait_ms: int = 0) -> Dict[str, Any]:
        if not isinstance(job_id, str) or not job_id.strip():
            return error_result("bad_request", "字段 job 必须是非空字符串。")
        if not _is_int(wait_ms) or int(wait_ms) < 0:
            return error_result("bad_request", "字段 waitMs 必须是非负整数毫秒。")
        job = self._jobs.get(job_id)
        if job is None:
            return error_result("bad_request", f"未知的 job：{job_id}", hint="job 来自 status=running 的调用结果。")
        await self._await_job(job, min(int(wait_ms), 60000))
        return self._job_view(job, include_job=True)

    async def cancel_job(self, job_id: str) -> Dict[str, Any]:
        if not isinstance(job_id, str) or not job_id.strip():
            return error_result("bad_request", "字段 job 必须是非空字符串。")
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": True, "job": job_id, "status": "not_found"}
        if job.task is not None and not job.task.done():
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            job.status = "cancelled"
            job.finished_at = time.time()
            return {"ok": True, "job": job.id, "status": "cancelled"}
        if job.status == "cancelled":
            return {"ok": True, "job": job.id, "status": "cancelled"}
        return {"ok": True, "job": job.id, "status": "done"}

    # -- batch -------------------------------------------------------------

    async def call_batch(self, calls: List[Dict[str, Any]], *, wait_ms: int = 30000) -> Dict[str, Any]:
        if not isinstance(calls, list) or not calls:
            return error_result("bad_request", "字段 calls 必须是非空数组。")
        if len(calls) > MAX_BATCH_CALLS:
            return error_result("bad_request", f"一次最多 {MAX_BATCH_CALLS} 个调用，收到 {len(calls)} 个。")
        if not _is_int(wait_ms) or int(wait_ms) < 0:
            return error_result("bad_request", "字段 waitMs 必须是非负整数毫秒。")
        if self.status == "closed":
            return error_result("session_closed", "会话已结束，无法再调用 MCP 工具。")
        job = self._new_job("*", f"batch({len(calls)})", "batch")
        job.task = asyncio.get_running_loop().create_task(self._run_batch_job(job, calls), name=f"mcp-job-{job.id}")
        return await self._await_job(job, wait_ms)

    async def _run_batch_job(self, job: _Job, calls: Sequence[Dict[str, Any]]) -> None:
        started = time.perf_counter()
        results: List[Dict[str, Any]] = []
        ok_count = 0
        error_count = 0
        try:
            async with self._semaphore:
                for index, call in enumerate(calls):
                    item = await self._execute_batch_item(call)
                    item["index"] = index
                    results.append(item)
                    if item.get("ok") and not item.get("isError"):
                        ok_count += 1
                    else:
                        error_count += 1
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.finished_at = time.time()
            job.result = {
                "ok": False,
                "status": "cancelled",
                "error": {
                    "kind": "cancelled",
                    "message": "批量调用已取消。",
                    "hint": ERROR_HINTS["cancelled"],
                    "server": None,
                },
                "results": results,
                "okCount": ok_count,
                "errorCount": error_count,
                "durationMs": int((time.perf_counter() - started) * 1000),
            }
            raise
        job.result = {
            "ok": True,
            "status": "done",
            "results": results,
            "okCount": ok_count,
            "errorCount": error_count,
            "durationMs": int((time.perf_counter() - started) * 1000),
        }
        job.status = "done"
        job.finished_at = time.time()
        self._persist_job(job)
        self._emit_call_event(job, job.result)

    async def _execute_batch_item(self, call: Any) -> Dict[str, Any]:
        if not isinstance(call, dict):
            return error_result("bad_request", "calls 的每一项必须是对象。")
        server = call.get("server")
        tool = call.get("tool")
        if not isinstance(server, str) or not server:
            return error_result("bad_request", "calls 的每一项都需要 server 字符串。")
        if not isinstance(tool, str) or not tool:
            return error_result("bad_request", "calls 的每一项都需要 tool 字符串。", server=server)
        arguments = call.get("arguments") or {}
        if not isinstance(arguments, dict):
            return error_result("bad_request", "calls 的 arguments 必须是 JSON 对象。", server=server)
        timeout_ms = call.get("timeoutMs")
        if timeout_ms is not None and not _is_int(timeout_ms):
            return error_result("bad_request", "calls 的 timeoutMs 必须是正整数毫秒。", server=server)
        timeout_sec = self._call_timeout(timeout_ms)
        return await self._execute_single(server, tool, arguments, timeout_sec)

    # -- side effects ------------------------------------------------------

    def _persist_job(self, job: _Job) -> None:
        if self.jobs_dir is None:
            return
        directory = Path(self.jobs_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{job.id}.json"
            payload = {
                "job": job.id,
                "kind": job.kind,
                "server": job.server,
                "tool": job.tool,
                "status": job.status,
                "createdAt": job.created_at,
                "finishedAt": job.finished_at,
                "result": job.result,
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            entries = [p for p in directory.iterdir() if p.is_file() and _JOB_FILE.match(p.name)]
            entries.sort(key=lambda p: p.stat().st_mtime)
            for stale in entries[: max(0, len(entries) - JOB_FILE_LIMIT)]:
                stale.unlink()
        except OSError:
            return

    def _emit_call_event(self, job: _Job, result: Dict[str, Any]) -> None:
        callback = self.on_call_event
        if callback is None:
            return
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        event = {
            "sessionId": self.session_id,
            "server": job.server,
            "tool": job.tool,
            "job": job.id,
            "kind": job.kind,
            "ok": bool(result.get("ok")),
            "isError": bool(result.get("isError")),
            "errorKind": error.get("kind"),
            "status": result.get("status"),
            "durationMs": result.get("durationMs"),
            "truncated": bool(result.get("truncated")),
            "at": _now_iso(),
        }
        if self._secrets:
            # The payload is ids and flags only today; a future field must not leak a credential.
            event = {key: (scrub_secrets(value, self._secrets) if isinstance(value, str) else value)
                     for key, value in event.items()}
        try:
            outcome = callback(event)
        except Exception:
            return
        if asyncio.iscoroutine(outcome):
            try:
                asyncio.get_running_loop().create_task(outcome)
            except RuntimeError:  # pragma: no cover
                pass

    # -- lifecycle ---------------------------------------------------------

    async def reap_idle(self) -> int:
        if self.status == "closed":
            return 0
        now = time.time()
        closed = 0
        for name, actor in list(self.actors.items()):
            if actor.inflight:
                continue
            if actor.status not in ("connected", "error"):
                continue
            if now - actor.last_used_at < actor.idle_timeout_sec:
                continue
            # Drop the actor before closing it: a call that lands during the close
            # dials a fresh connection instead of being told the session is closed.
            self.actors.pop(name, None)
            await actor.close()
            closed += 1
        return closed

    async def close_all(self, timeout_sec: float = 5.0) -> None:
        """Close every job, actor and connection inside ONE hard budget.

        A stubborn child (a TUI-like process that ignores EOF, a dial that never answers
        ``initialize``) must not make ``await client.__aexit__`` hang forever: when the budget
        runs out the actors are force-reaped, so the call returns in bounded time and leaves no
        live child behind.
        """
        deadline = time.monotonic() + max(0.3, float(timeout_sec))
        self.status = "closed"
        for job in list(self._jobs.values()):
            if job.task is not None and not job.task.done():
                job.task.cancel()
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        if tasks:
            await _gather_within(tasks, deadline)
        for job in self._jobs.values():
            if job.status == "running" and job.result is None:
                # A job cancelled before its coroutine ever ran still deserves a verdict.
                job.status = "cancelled"
                job.finished_at = job.finished_at or time.time()
        for task in list(self._background):
            task.cancel()
        if self._background:
            await _gather_within(list(self._background), deadline)
        self._background.clear()
        actors = list(self.actors.values())
        self.actors.clear()
        self._closing_actors = list(actors)
        try:
            if actors:
                remaining = max(0.05, deadline - time.monotonic())
                closing = asyncio.gather(
                    *(actor.close(timeout=remaining) for actor in actors), return_exceptions=True
                )
                if not await _bounded(closing, remaining + 0.2):
                    logger.warning(
                        "[%s] close budget exhausted; force-reaping %s actor(s)",
                        self.session_id, len(actors),
                    )
                    await self._reap(actors)
        finally:
            self._closing_actors = []
        self._jobs.clear()
        self._job_order.clear()

    async def force_reap_actors(self) -> None:
        """Kill whatever an unfinished ``close_all`` still owns (a hard-budget backstop)."""
        await self._reap([*self.actors.values(), *self._closing_actors])

    async def _reap(self, actors: Sequence[ServerActor]) -> None:
        for actor in actors:
            try:
                await actor.force_reap()
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - a save here is never fatal
                logger.exception("[%s] force-reaping actor %s failed", self.session_id, actor.name)

    # -- HTTP action dispatch ----------------------------------------------

    async def handle_action(
        self, payload: Dict[str, Any], *, shared_urls: Optional[Set[str]] = None
    ) -> Dict[str, Any]:
        """Serve ``POST /api/sessions/<sid>/mcp``; never raises to the route.

        ``shared_urls`` is the registry's own answer, so a `list` reports the live `shared` flag
        even though the caller never sends it (the CLI has no way to know).
        """
        try:
            if not isinstance(payload, dict):
                return error_result("bad_request", "请求体必须是 JSON 对象。")
            action = payload.get("action")
            if action == "list":
                shared = payload.get("sharedUrls")
                if shared is not None and not isinstance(shared, (list, tuple, set)):
                    return error_result("bad_request", "字段 sharedUrls 必须是字符串数组。")
                if shared is None and shared_urls is not None:
                    shared = shared_urls
                return self.list_servers(set(map(str, shared)) if shared else None)
            if action == "ping":
                server, failure = self._field(payload, "server")
                if failure:
                    return failure
                timeout_ms, failure = self._optional_ms(payload, "timeoutMs")
                if failure:
                    return failure
                return await self.ping_server(server, timeout_ms)
            if action == "tools":
                server, failure = self._field(payload, "server")
                if failure:
                    return failure
                timeout_ms, failure = self._optional_ms(payload, "timeoutMs")
                if failure:
                    return failure
                return await self.list_tools(server, timeout_ms)
            if action == "call":
                server, failure = self._field(payload, "server")
                if failure:
                    return failure
                tool, failure = self._field(payload, "tool")
                if failure:
                    return failure
                timeout_ms, failure = self._optional_ms(payload, "timeoutMs")
                if failure:
                    return failure
                wait_ms, failure = self._optional_ms(payload, "waitMs", default=30000)
                if failure:
                    return failure
                arguments = payload.get("arguments")
                return await self.call_tool(
                    server,
                    tool,
                    arguments,
                    timeout_ms=timeout_ms,
                    wait_ms=wait_ms if wait_ms is not None else 30000,
                )
            if action == "batch":
                calls = payload.get("calls")
                if not isinstance(calls, list):
                    return error_result("bad_request", "字段 calls 必须是数组。")
                wait_ms, failure = self._optional_ms(payload, "waitMs", default=30000)
                if failure:
                    return failure
                return await self.call_batch(calls, wait_ms=wait_ms if wait_ms is not None else 30000)
            if action == "cancel":
                job_id = payload.get("job") if payload.get("job") is not None else payload.get("jobId")
                if not isinstance(job_id, str) or not job_id.strip():
                    return error_result("bad_request", "字段 job 必须是非空字符串。")
                return await self.cancel_job(job_id)
            return error_result(
                "bad_request",
                f"不支持的 action：{action!r}",
                hint="action 必须是 list / ping / tools / call / batch / cancel 之一。",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            return error_result("internal", f"处理 {payload.get('action') if isinstance(payload, dict) else ''} 时出错：{exc}")

    @staticmethod
    def _field(payload: Dict[str, Any], key: str) -> tuple[str, Optional[Dict[str, Any]]]:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value, None
        return "", error_result("bad_request", f"字段 {key} 必须是非空字符串。")

    @staticmethod
    def _optional_ms(
        payload: Dict[str, Any], key: str, default: Optional[int] = None
    ) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
        if key not in payload or payload.get(key) is None:
            return default, None
        value = payload.get(key)
        if not _is_int(value) or int(value) < 0:
            return None, error_result("bad_request", f"字段 {key} 必须是非负整数毫秒。")
        return int(value), None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_pid(value: Any) -> int:
    try:
        pid = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


# --- registry -------------------------------------------------------------


class PoolRegistry:
    """Process-wide map of ``session_id -> SessionPool`` plus the pid ledger.

    The ledger exists for one reason: the Hub can die while a stdio MCP child is
    still running.  At the next start :meth:`sweep_orphan_pids` kills only the
    leftovers whose *command line* still matches the recorded command, so a
    recycled pid never costs an unrelated process its life.
    """

    def __init__(self, runtime_configs_dir: Path, idle_reap_interval_sec: float = 60.0) -> None:
        self.runtime_configs_dir = Path(runtime_configs_dir)
        self.ledger_path = self.runtime_configs_dir / "mcp_pool_pids.json"
        self.idle_reap_interval_sec = float(idle_reap_interval_sec)
        self._pools: Dict[str, SessionPool] = {}
        # A replaced or closing pool keeps its pids in the ledger until its children are gone:
        # dropping it from ``_pools`` first would lose exactly the processes a crash could orphan.
        self._retiring_pools: Set[SessionPool] = set()
        self._close_tasks: Set[asyncio.Task] = set()
        self._reaper_task: Optional[asyncio.Task] = None
        self._reaper_stop: Optional[asyncio.Event] = None

    # -- sessions ----------------------------------------------------------

    def register_session(
        self,
        session_id: str,
        servers: Dict[str, Dict[str, Any]],
        *,
        cwd: Optional[str] = None,
        output_dir: Optional[Path] = None,
        jobs_dir: Optional[Path] = None,
        on_call_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> SessionPool:
        pool = SessionPool(
            session_id,
            servers,
            cwd=cwd,
            output_dir=output_dir,
            jobs_dir=jobs_dir,
            on_call_event=on_call_event,
            on_pid_change=self.write_pid_ledger,
        )
        pool.shared_urls_provider = self.shared_http_urls
        previous = self._pools.get(str(session_id))
        self._pools[str(session_id)] = pool
        if previous is not None:
            self._retire(previous)
        self.write_pid_ledger()
        return pool

    def _retire(self, pool: SessionPool) -> Optional[asyncio.Task]:
        """Hand a replaced pool to the background close, keeping its pids in the ledger meanwhile."""
        self._retiring_pools.add(pool)
        # Written now, while the old pool is still in the ledger, not only after it closed.
        self.write_pid_ledger()
        return self._schedule_close(pool)

    def _schedule_close(self, pool: SessionPool) -> Optional[asyncio.Task]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop to schedule on
            self._retiring_pools.discard(pool)
            return None

        async def _closing() -> None:
            try:
                await pool.close_all()
            finally:
                # Only the close that really finished may take its pids out of the ledger.
                self._retiring_pools.discard(pool)
                self.write_pid_ledger()

        task = loop.create_task(_closing(), name=f"mcp-pool-close-{pool.session_id}")
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)
        return task

    def get(self, session_id: str) -> Optional[SessionPool]:
        return self._pools.get(str(session_id))

    def sessions(self) -> List[str]:
        return list(self._pools.keys())

    def shared_http_urls(self) -> Set[str]:
        counts: Dict[str, int] = {}
        for pool in list(self._pools.values()):
            if pool.status == "closed":
                continue
            for url in pool.http_urls():
                counts[url] = counts.get(url, 0) + 1
        return {url for url, count in counts.items() if count >= 2}

    async def close_session(self, session_id: str, timeout_sec: float = 5.0) -> bool:
        pool = self._pools.pop(str(session_id), None)
        if pool is None:
            return False
        # Still retiring until its children are gone: a crash mid-close must not lose the pids.
        self._retiring_pools.add(pool)
        try:
            await pool.close_all(timeout_sec)
        finally:
            self._retiring_pools.discard(pool)
            self.write_pid_ledger()
        return True

    async def close_all(self, timeout_sec: float = 5.0) -> None:
        """Close every pool (live and retiring) inside one hard budget, then rewrite the ledger."""
        deadline = time.monotonic() + max(0.3, float(timeout_sec))
        self.stop_reaper()
        for task in list(self._close_tasks):
            if not task.done():
                task.cancel()
        if self._close_tasks:
            await _gather_within(list(self._close_tasks), deadline)
        self._close_tasks.clear()
        pools = list(self._pools.values())
        pools.extend(pool for pool in self._retiring_pools if pool not in pools)
        self._pools.clear()
        self._retiring_pools.update(pools)
        try:
            if pools:
                remaining = max(0.05, deadline - time.monotonic())
                closing = asyncio.gather(
                    *(pool.close_all(remaining) for pool in pools), return_exceptions=True
                )
                if not await _bounded(closing, remaining + 0.2):
                    logger.warning("registry close budget exhausted; force-reaping %s pool(s)", len(pools))
                    for pool in pools:
                        await pool.force_reap_actors()
        finally:
            self._retiring_pools.clear()
            self.write_pid_ledger()

    # -- pid ledger --------------------------------------------------------

    def _ledger_records(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        pools = list(self._pools.values()) + [pool for pool in self._retiring_pools
                                             if pool not in self._pools.values()]
        for pool in pools:
            try:
                records.extend(pool.active_pid_records())
            except Exception:  # pragma: no cover - one broken pool is not fatal
                continue
        return records

    def write_pid_ledger(self) -> Path:
        payload = {
            "version": 1,
            "hubPid": os.getpid(),
            "writtenAt": _now_iso(),
            "pids": self._ledger_records(),
        }
        try:
            self.runtime_configs_dir.mkdir(parents=True, exist_ok=True)
            temp = self.ledger_path.with_name(self.ledger_path.name + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.ledger_path)
        except OSError:  # pragma: no cover - read-only config dir
            pass
        return self.ledger_path

    def read_pid_ledger(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        pids = data.get("pids") if isinstance(data, dict) else None
        return [record for record in pids if isinstance(record, dict)] if isinstance(pids, list) else []

    def sweep_orphan_pids(self) -> int:
        """Kill leftovers recorded by a previous Hub run.  Called once at startup."""
        records = self.read_pid_ledger()
        live: Set[int] = set()
        for record in self._ledger_records():
            if _as_pid(record.get("pid")):
                live.add(_as_pid(record["pid"]))
        killed = 0
        if records:
            candidates = []
            for record in records:
                pid = _as_pid(record.get("pid"))
                if not pid or pid in live:
                    continue
                candidates.append((pid, str(record.get("command") or "")))
            command_lines = query_command_lines(pid for pid, _ in candidates) if candidates else {}
            for pid, recorded in candidates:
                if not command_matches(recorded, command_lines.get(pid, "")):
                    continue
                if not pid_alive(pid):
                    continue
                if kill_pid_tree(pid):
                    killed += 1
        self.write_pid_ledger()
        return killed

    # -- idle reaper -------------------------------------------------------

    def start_reaper(self) -> Optional[asyncio.Task]:
        if self._reaper_task is not None and not self._reaper_task.done():
            return self._reaper_task
        self._reaper_stop = asyncio.Event()
        self._reaper_task = asyncio.get_running_loop().create_task(self._reaper_loop(), name="mcp-pool-reaper")
        return self._reaper_task

    def stop_reaper(self) -> None:
        if self._reaper_stop is not None:
            self._reaper_stop.set()
        task = self._reaper_task
        self._reaper_task = None
        if task is not None and not task.done():
            task.cancel()
            self._close_tasks.add(task)
            task.add_done_callback(self._close_tasks.discard)

    async def _reaper_loop(self) -> None:
        stop = self._reaper_stop
        while stop is not None and not stop.is_set():
            for pool in list(self._pools.values()):
                try:
                    await pool.reap_idle()
                except Exception:  # pragma: no cover - reaper never dies
                    continue
            self.write_pid_ledger()
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1.0, self.idle_reap_interval_sec))
            except asyncio.TimeoutError:
                continue
