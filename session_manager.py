import asyncio
import base64
import ctypes
from ctypes import wintypes
import datetime
import fnmatch
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

import httpx
import pyte
from PIL import Image
from winpty import PtyProcess

# R6: the real class is kept apart from the module-level name so a test can replace
# session_manager.PtyProcess with a fake without making the shutdown tree-kill touch a fake PID.
REAL_PTY_PROCESS = PtyProcess

from transcript_tail import TranscriptTail, read_complete_lines
from transcript_feed import (
    FEED_DEFAULT_LIMIT, FEED_MAX_LIMIT, build_transcript_feed, tool_output_text,
    interaction_view as transcript_interaction_view,
)
from dsh_session_record import (
    DshSessionRecordReader, RecordDecodeError, locate_dsh_session_record, native_events_to_telemetry,
)
from native_session_launch import normalize_extra_args, strip_legacy_worker_bridge_args
from session_mcp_config import (
    normalize_mcp_config, mcp_config_active, validate_mcp_engine, check_mcp_arg_conflicts,
    build_mcp_args, merge_claude_settings, replace_claude_settings,
)
from agent_guidance import PROFILE_DESCRIPTIONS, ROUTING_RULES
from parent_waiting import session_targets
from hub_database import HubDatabase, END_REASON_LABELS, ARCHIVE_COLUMNS
from hub_persistence import HubPersistence, PersistenceUnavailable, logical_write
from wait_inbox import WaitInbox, WAKE_EVENT_TYPES

from dsh_endpoint import dsh_base_url, dsh_url
from dsh_workspace_permissions import prepare_dsh_workspace
from model_discovery import AgentModelDiscovery
from transcript_summarizer import (
    ENGINE_BUSY_LINE_RES,
    ENGINE_BUSY_PHRASES,
    TranscriptSummarizerRegistry,
    sanitize_tui_screen_text,
)

logger = logging.getLogger("mcp-agent-hub.session_manager")

# Win32 API constants for Desktop GUI window capture & off-screen tucking
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# S4: a window's process name is read through a limited-information handle; a failure is not an error.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
try:
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
except Exception:  # pragma: no cover - only on a non-Windows host
    pass

PW_RENDERFULLCONTENT = 0x00000002
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]




# Native answers outrank derived summaries and screen snapshots.
FINAL_SOURCE_PRIORITY = {
    None: 0,
    "terminal_turn_snapshot": 1,
    "transcript_summarizer": 2,
    # Keep the source label of a stronger native record when multiple observers see the same turn.
    "turn_completed": 3,
    "native_transcript": 4,
    "codex_task_complete": 5,
    "dsh_hub_completion": 5,
    "report_complete_mcp": 6,  # Historical saved results only; no reporting interface remains.
}

NATIVE_TRANSCRIPT_DRAIN_SEC = 3.0
# Tool arguments/output kept on a worker event for the console transcript (characters).
TOOL_DETAIL_LIMIT = 4000


def _clip_detail(text: str) -> str:
    """Keep the head of a long tool detail and say that the rest was cut."""
    if len(text) <= TOOL_DETAIL_LIMIT:
        return text
    return f"{text[:TOOL_DETAIL_LIMIT]}\n…（已截断，原文 {len(text)} 字符）"

# Heuristic regex for detecting TUI tool calls on terminal lines when native hooks aren't present
TUI_TOOL_PATTERN = re.compile(
    r"^\s*[●•✦⚡▶]\s*(Bash|Read|Edit|Write|Grep|Glob|LS|WebSearch|Fetch|Task|MultiEdit|NotebookEdit|cmd|powershell)\s*(?:\((.+?)\)|:\s*(.+))?$",
    re.IGNORECASE,
)

DEFAULT_HUB_PORT = 4444
KNOWN_ENGINES = ("omp", "kimi", "agy", "codex", "claude", "dsh", "pwsh")
AGY_THINKING_LEVELS = ("low", "medium", "high")
AGY_SUFFIX_RE = re.compile(r"-(low|medium|high)$")
# A profile with an absurdly long argument list is a mistake, not a launch recipe.
MAX_LAUNCH_ARGS = 4096
MAX_ENV_VARS = 256
_ENV_KEY_INVALID_RE = re.compile(r"[\s=\x00]")

AUTO_APPROVAL_MAX_RULES = 64
AUTO_APPROVAL_MAX_USES = 10000
AUTO_APPROVAL_MAX_TTL_SEC = 7 * 24 * 60 * 60
AUTO_APPROVAL_SHELL_META_RE = re.compile(r"[;&|<>`]|\$\(|\b(?:bash|sh|cmd|powershell|pwsh)\s+-c\b", re.IGNORECASE)
AUTO_APPROVAL_HARD_RISK_RE = re.compile(
    r"\b(?:rm|del|erase|rmdir|remove-item|format|diskpart|shutdown|restart-computer|"
    r"stop-computer|stop-process|kill|taskkill|git\s+(?:reset\s+--hard|clean\s+-\S*f|push\s+(?:--force|-f|\+)|branch\s+-D)|"
    r"sudo|runas|publish|invoke-webrequest|curl|wget|invoke-restmethod|irm)\b",
    re.IGNORECASE,
)


def normalize_auto_approval_rules(raw_rules: Any) -> List[Dict[str, Any]]:
    """Validate and normalize session-scoped automatic approval rules."""
    if raw_rules is None:
        return []
    if not isinstance(raw_rules, list):
        raise ValueError("auto_approval_rules must be a list")
    if len(raw_rules) > AUTO_APPROVAL_MAX_RULES:
        raise ValueError(f"auto_approval_rules accepts at most {AUTO_APPROVAL_MAX_RULES} rules")
    normalized: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise ValueError("each auto approval rule must be an object")
        rule_id = str(raw.get("id") or "").strip()
        if not rule_id or rule_id in seen:
            raise ValueError("auto approval rule ids must be non-empty and unique")
        effect = str(raw.get("effect") or "approve_once").strip().lower()
        if effect != "approve_once":
            raise ValueError("automatic rules currently support only effect=approve_once")
        match = raw.get("match")
        if not isinstance(match, dict):
            raise ValueError(f"auto approval rule {rule_id!r} requires a match object")
        raw_tools = match.get("tool") or []
        if (not isinstance(raw_tools, list) or not raw_tools
                or any(not isinstance(item, str) or not item.strip() for item in raw_tools)):
            raise ValueError(f"auto approval rule {rule_id!r} match.tool must be a list of names")
        command_argv = match.get("command_argv") or []
        if not isinstance(command_argv, list) or any(
            not isinstance(pattern, list) or not pattern or any(not isinstance(token, str) for token in pattern)
            for pattern in command_argv
        ):
            raise ValueError(f"auto approval rule {rule_id!r} command_argv must be a list of argv patterns")
        constraints = raw.get("constraints") or {}
        if not isinstance(constraints, dict):
            raise ValueError(f"auto approval rule {rule_id!r} constraints must be an object")
        cwd_scope = str(match.get("cwd_scope") or "workspace").strip().lower()
        if cwd_scope != "workspace":
            raise ValueError(f"auto approval rule {rule_id!r} cwd_scope must be 'workspace'")
        if any(bool(constraints.get(key)) for key in ("network", "outside_workspace", "shell_composition")):
            raise ValueError(
                f"auto approval rule {rule_id!r} may only permit network=false, "
                "outside_workspace=false and shell_composition=false"
            )
        try:
            max_uses = int(raw.get("max_uses", 1))
            ttl = int(raw.get("expires_in_seconds", 0))
        except (TypeError, ValueError):
            raise ValueError(f"auto approval rule {rule_id!r} max_uses/expires_in_seconds must be integers")
        if max_uses < 1 or max_uses > AUTO_APPROVAL_MAX_USES:
            raise ValueError(f"auto approval rule {rule_id!r} max_uses is out of range")
        if ttl < 0 or ttl > AUTO_APPROVAL_MAX_TTL_SEC:
            raise ValueError(f"auto approval rule {rule_id!r} expires_in_seconds is out of range")
        normalized.append({
            "id": rule_id,
            "effect": effect,
            "match": {
                "tool": [str(item).strip() for item in raw_tools],
                "command_argv": [list(pattern) for pattern in command_argv],
                "cwd_scope": cwd_scope,
                "path_glob": str(match.get("path_glob") or ""),
            },
            "constraints": {
                "network": bool(constraints.get("network", False)),
                "outside_workspace": bool(constraints.get("outside_workspace", False)),
                "shell_composition": bool(constraints.get("shell_composition", False)),
            },
            "max_uses": max_uses,
            "expires_in_seconds": ttl,
            "_uses": 0,
            "_created_at": time.time(),
        })
        seen.add(rule_id)
    return normalized


def normalize_env_map(raw_env: Any, *, field_label: str = "环境变量（env）") -> Dict[str, str]:
    """Normalizes and validates a profile or launch environment variable map.

    Accepts a `Dict[str, str]` (or scalar values), a list of `KEY=VALUE` strings, or a multiline
    `KEY=VALUE` string (ignoring empty lines and `#` comment lines). Raises a Chinese ValueError on
    malformed keys or unsupported value types.
    """
    if raw_env is None or raw_env == "" or raw_env == {} or raw_env == []:
        return {}

    pairs: List[tuple[Any, Any]] = []
    if isinstance(raw_env, dict):
        pairs = list(raw_env.items())
    elif isinstance(raw_env, (str, list)):
        lines = raw_env.splitlines() if isinstance(raw_env, str) else list(raw_env)
        for raw_line in lines:
            if not isinstance(raw_line, str):
                raise ValueError(f"{field_label} 的每一项都必须是 KEY=VALUE 字符串。")
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"{field_label} 格式无效（缺少 '='，应为 KEY=VALUE）：{line}")
            k_part, v_part = line.split("=", 1)
            pairs.append((k_part.strip(), v_part.strip()))
    else:
        raise ValueError(f"{field_label} 必须是键值字典（Dict[str, str]）或每行一个 KEY=VALUE。")

    if len(pairs) > MAX_ENV_VARS:
        raise ValueError(f"{field_label} 数量过多（最多 {MAX_ENV_VARS} 项）。")

    result: Dict[str, str] = {}
    for raw_key, raw_val in pairs:
        if not isinstance(raw_key, str):
            raise ValueError(f"{field_label} 的变量名必须是字符串：{raw_key!r}")
        key = raw_key.strip()
        if not key:
            raise ValueError(f"{field_label} 的变量名不能为空。")
        if _ENV_KEY_INVALID_RE.search(key):
            raise ValueError(f"{field_label} 的变量名「{key}」不能包含空格、等号或空字符。")
        if raw_val is None:
            result.pop(key, None)
            continue
        if isinstance(raw_val, bool):
            val = "true" if raw_val else "false"
        elif isinstance(raw_val, (str, int, float)):
            val = str(raw_val)
        else:
            raise ValueError(f"{field_label} 中「{key}」的值必须是字符串。")
        if "\x00" in val:
            raise ValueError(f"{field_label} 中「{key}」的值不能包含空字符（\\0）。")
        result[key] = val
    return result

# Q6: Windows refuses `os.replace` / an open-for-write while another process holds the file. A short
# retry covers the millisecond window of a concurrent reader; a lock that survives it is reported as
# a 400 (「配置文件被其他程序占用，请稍后重试」), never as a 500 and never as a parse error.
PROFILE_FILE_RETRY_ATTEMPTS = 10
PROFILE_FILE_RETRY_DELAY_SEC = 0.03
PROFILE_FILE_LOCKED_MESSAGE = "配置文件被其他程序占用，请稍后重试"

# Q8: keep looking for a session's own transcript for as long as its process lives, backing off to
# one scan every 5 s after the first minute instead of giving up after 30/40/90 s.
TRANSCRIPT_SCAN_FAST_WINDOW_SEC = 60.0
TRANSCRIPT_SCAN_FAST_INTERVAL_SEC = 1.0
TRANSCRIPT_SCAN_SLOW_INTERVAL_SEC = 5.0
# Q2: the hub session marker travels in the task text, i.e. near the beginning of the transcript.
# Codex 0.167+ writes ~160 KB of tool schemas, skills, and AGENTS.md before the first user message,
# so scan up to 512 KB to never miss the session_id marker.
TRANSCRIPT_MARKER_SCAN_BYTES = 512 * 1024
TRANSCRIPT_MARKER_RE = re.compile(r"session_id\s*=\s*([A-Za-z0-9_.\-]+)")

# ---- Archive & resume (spec archive-spec.md §1-§5) ------------------------------------------
# Live statuses keep their meaning; an ended session carries one of these end reasons.
LIVE_STATUSES = ("starting", "running", "waiting_parent", "completed")
END_REASONS = ("exited", "crashed", "terminated", "interrupted")
# K5: the `terminated` event says who ended the session. The console pane and an MCP caller are two
# different actors and must not share one sentence.
TERMINATED_MESSAGES = {
    "console": "用户在总控台终止了会话",
    "mcp": "主 Agent 终止了会话",
    "auto_cleanup": "Sub Agent 完成后闲置超时，Hub 自动关闭了会话",
}
DEFAULT_SUBAGENT_IDLE_TIMEOUT_MINUTES = 60
SUBAGENT_CLEANUP_INTERVAL_SEC = 30.0
INACTIVE_SESSION_STATUSES = frozenset({"completed", *END_REASONS})


def normalize_subagent_idle_timeout(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("subagent_idle_timeout_minutes 必须是非负整数（分钟，0 表示禁用自动关闭）。")
    return value
# Engines with a revive recipe (spec §4). Anything else cannot be resumed.
RESUME_ENGINES = ("omp", "kimi", "agy", "codex", "claude", "dsh")
ENGINE_RESUME_BLOCK_REASONS = {
    "pwsh": "PowerShell 会话结束后无法找回上下文",
    "desktop_gui": "桌面窗口会话结束后无法找回上下文",
}
DEFAULT_RESUME_BLOCK_REASON = "该引擎的会话结束后无法恢复上下文"
RESUME_SCROLLBACK_MAX_BYTES = 512 * 1024
RESUME_SCROLLBACK_INTERVAL_SEC = 60.0
# D1: archive records are kept forever. There is no age/count limit and no pruning loop; the only
# way a record leaves the hub is the manual delete (console 删除记录 / DELETE /api/archive/{id}).
# R5/R6: a graceful stop must not hang on a CLI that ignores the first signal or a DSH host that
# takes its time; each session is bounded and so is the whole loop.
SHUTDOWN_MAX_SEC = 8.0
SHUTDOWN_JOB_CANCEL_TIMEOUT_SEC = 2.0
AGY_RESUME_FAILURE_MESSAGE = "恢复失败：agy 没有接上原会话，开了一个新会话"
CODEX_ARCHIVED_MESSAGE = "codex 已把该会话归档，请先运行 codex unarchive {sid}"
KIMI_LOCKED_MESSAGE = "该 Kimi 会话正被另一个进程使用"
DSH_UNKNOWN_JOB_MESSAGE = "DSH 服务已不认识这个任务（可能重启过）"
DSH_STATE_UNAVAILABLE_MESSAGE = "DSH 任务状态不可用：{reason}"
# R3: these are ENDED jobs. Re-attaching one as "completed" lets the next message /continue it;
# refusing them made the archive say 可恢复 while 恢复 without text failed.
DSH_ENDED_JOB_STATUSES = ("done", "failed", "timeout", "cancelled")
RESUME_FAILED_PREFIX = "恢复失败："
# A4: a resume failure that no re-check can undo is sticky. agy opened a different conversation
# (only a real resume can fix it) and codex archived the conversation (only codex unarchive can);
# _finalize_session and compute_resumable must keep these instead of recomputing "resumable".
# The codex message carries the session id, so it is matched by its prefix.
STICKY_RESUME_BLOCK_REASONS = (AGY_RESUME_FAILURE_MESSAGE,)
STICKY_RESUME_BLOCK_PREFIXES = ("codex 已把该会话归档",)
# A3: after a resumed agy process starts, a brand-new conversation dir can only be that process
# opening an empty conversation for an id it does not know. The window keeps the scan bounded.
AGY_RESUME_VERIFY_WINDOW_SEC = 30.0
# The hub's anchor (time.time()) and the filesystem's birth time are different clock sources and can
# round against each other; a dir born in the same tick as the spawn is still the new conversation.
AGY_CONVERSATION_ANCHOR_SLACK_SEC = 0.05
# A11: the archive thread keeps every worker→parent message per session, outside the 2000-event ring.
MAILBOX_EVENT_TYPES = frozenset({
    "message", "message_to_parent", "question", "approval_request", "completed", "turn_completed",
})
WORKER_MESSAGE_LOG_CAP = 2000
# V2: the startup injection waits for a settled TUI with a visible prompt instead of a fixed delay,
# then proves the Enter really submitted the text.
TUI_READY_STABLE_SEC = 1.5
TUI_STARTUP_WINDOW_SEC = 90.0
# W2: the composer rows found in the real captures. kimi draws a boxed row; omp and codex (and
# agy, which uses the same Go TUI shape) draw a bare prompt glyph with status lines below it.
TUI_INPUT_BOX_RE = re.compile(r"^\s*[│┃|]\s*>\s?(.*?)\s*[│┃|]\s*$")
TUI_INPUT_BOX_OPEN_RE = re.compile(r"^\s*[│┃|]\s*>\s?(.*?)\s*$")
TUI_INPUT_GLYPH_RE = re.compile(r"^\s*(?:[│┃|]\s*)?(?:[>❯›»])\s?(.*)$")
# K2 (fix 9): how each engine draws its composer. "box" is a framed box whose first content row is
# "│ > …" and whose height grows with the text; every other engine draws a bare prompt glyph row.
# K6 (fix 9c): the table is also the list of engines whose composer the hub KNOWS from real
# captures. A live message is typed through the checked submit only for an engine listed here AND in
# TUI_LIVE_SUBMIT_CHECKED; any other TUI (pwsh, a generic CLI, where a "> " output row is not a
# composer) keeps the old unchecked write + Enter.
TUI_COMPOSER_SHAPES = {
    "kimi": "box",
    "omp": "glyph",
    "codex": "glyph",
    "agy": "glyph",
    "claude": "glyph",
}
# Owner decision engines-checked (2026-11-06, decision page kimi-injection-2026-11-06; revised
# 2026-11-07 to kimi + codex): only these engines type a LIVE message through the checked submit
# (K5 settle -> Enter -> K3 proof). kimi lost its Enter first; the approval live check of 2026-11-07
# saw codex leave a live message in its composer 2 of 5 times. omp and agy keep the old write +
# Enter, recorded as ok:null "live_write_unchecked". Their composers stay in TUI_COMPOSER_SHAPES for
# reading the screen.
TUI_LIVE_SUBMIT_CHECKED = frozenset({"kimi", "codex", "claude", "omp", "agy"})
# K7 (fix 9d): a "glyph" composer wraps a long text onto the rows under its prompt row, and those
# rows end at the engine's own closing row (real screens of the 22b0573 live check):
#   omp    rule, "❯ …", wrapped rows indented two columns, rule, status line   (o1l2_screens.txt)
#   agy    rule, "> …", wrapped rows indented two columns, rule, status line   (a1l_screens.txt)
#   codex  "› …", wrapped rows indented two columns, a blank row, status lines (c1l_screens.txt)
# "top" is the row that must sit right above the prompt row, "bottom" the row that must follow the
# wrapped rows ("rule" = a run of ─, "blank" = an empty row, "" = not checked). The wrapped rows are
# read only when that closing row follows them; a prompt row above the bottom TUI_PROMPT_SCAN_ROWS
# rows is accepted only when its whole frame is there and the closing row lies in those rows, so a
# history row higher up (agy draws its submitted messages as "> …" rows too) is never the composer.
TUI_GLYPH_COMPOSER_FRAMES = {
    "omp": {"top": "rule", "bottom": "rule"},
    "agy": {"top": "rule", "bottom": "rule"},
    "codex": {"top": "", "bottom": "blank"},
    "claude": {"top": "rule", "bottom": "rule", "require_frame": True},
}
TUI_RULE_ROW_RE = re.compile(r"^\s*[─━═]{8,}")
# K7 (fix 9d): a row under a glyph composer's closing row that starts an answer or a tool line
# means the "composer" above it is submitted history, not the input row.
TUI_GLYPH_HISTORY_BELOW_RE = re.compile(r"^\s*[•●✗⏺]")
# K7 (fix 9d): codex shows a long typed burst as "[Pasted Content N chars]" instead of the text, so
# the text's end never appears. That row with N (summed over all such rows) within a few chars of
# the typed length counts as the typed text being on the composer (live check: 1,027 for the
# 1,027-char longc.txt).
TUI_PASTE_PLACEHOLDER_RES = {
    "codex": re.compile(r"\[Pasted Content (\d+) chars\]"),
    "claude": re.compile(
        r"\[(?:Pasted (?:text|Content)|\.{3}Truncated text)\s*#\d+(?:\s+\+\d+\s*(?:lines|chars)(?:\.{3})?)?\]"
        r"|\[Pasted (?:text|Content)[^\]]*?(\d+)\s*(?:chars|lines)\]",
        re.IGNORECASE,
    ),
    "agy": re.compile(
        r"\[Pasted text #\d+(?:\s+(?:(\d+)\s*chars|\+\d+\s*lines))?\]",
        re.IGNORECASE,
    ),
}
TUI_PASTE_PLACEHOLDER_SLACK_CHARS = 4
TUI_BOX_TOP_RE = re.compile(r"^\s*╭─.*╮\s*$")
TUI_BOX_BOTTOM_RE = re.compile(r"^\s*╰─.*╯\s*$")
TUI_BOX_SIDE_RE = re.compile(r"^\s*[│┃]")
TUI_SUBMIT_CONFIRM_SEC = 3.0
TUI_SUBMIT_CONFIRM_POLL_SEC = 0.25
TUI_SUBMIT_MAX_TRIES = 3
TUI_SUBMIT_RETRY_STEP_SEC = 1.0
# K5 (fix 9b): after typing, Enter waits until the typed text has SETTLED on the composer: the
# composer (or the rows it wrapped onto) holds the text's last chars and has not changed for
# TUI_TYPE_SETTLE_SEC. The live check of 30a638e pressed Enter 0.8 s after typing kimi's 1,073-char
# task while its box was still empty; kimi took that Enter as a newline inside the typed burst and
# every fresh spawn needed 2-3 Enters (probe k5-fixed-01: Enter at 0.83 s, tail drawn at 1.03 s,
# Enter lost; k5-settle-01..05: tail at 0.4-2.2 s, Enter after it, submitted on the first press).
# A text that never shows is pressed after TUI_TYPE_SETTLE_CAP_SEC anyway (the old behaviour) and
# the result records that the echo never settled.
TUI_TYPE_SETTLE_SEC = 0.8
TUI_TYPE_SETTLE_POLL_SEC = 0.1
TUI_TYPE_SETTLE_CAP_SEC = 10.0
# D3: when the evidence window ends with ok:null/"unconfirmed" the hub presses Enter once more and
# watches this short window. The press counts toward TUI_SUBMIT_MAX_TRIES.
TUI_SUBMIT_EXTRA_PRESS_SEC = 5.0
# K6 (fix 9c): a live message to a TUI whose composer the hub cannot locate (an engine it does not
# know, or a screen without a composer) keeps the old unchecked delivery: the text, then Enter this
# long after it, and last_injection says it was not checked.
TUI_UNCHECKED_ENTER_DELAY_SEC = 0.25
# K6 (fix 9c): a live message the engine refused (kimi "Error: LLM not set") is typed again after
# this pause, within the same attempt limit.
TUI_LIVE_RETYPE_PAUSE_SEC = 1.5
# K8 (fix 9d): how many recent submit records (startup text, live messages) a session keeps beside
# last_injection, newest last. In memory only, like last_injection.
TUI_RECENT_INJECTIONS_MAX = 5
# W2: engine input boxes do not live on the last line (kimi draws two status lines under its
# composer), so the prompt is searched in the bottom rows of the screen, per engine.
TUI_PROMPT_SCAN_ROWS = 10
# R1/W4: the dialog watcher ticks at this interval for the whole startup window.
TUI_DIALOG_POLL_SEC = 0.5
TUI_TRUST_CONFIRM_SETTLE_SEC = 0.5
# The trust dialogs seen in the real captures (agy, codex, omp, kimi). Enter confirms the
# highlighted "Yes, I trust this folder"-style option; the same handler must be alive for the
# whole startup window, not only for the first half second (R1/W4).
TUI_TRUST_DIALOG_PHRASES = (
    "Trust this folder?",
    "Trust and continue",
    "Do you trust the contents of this project?",
    "Do you trust the authors of the files in this folder?",
    "Do you trust the files in this folder?",
    "Yes, I trust this folder",
    "Yes, I trust these settings",
    "Allow external CLAUDE.md file imports?",
    "WARNING: Claude Code running in Bypass Permissions mode",
)
# Claude Code 2.1+ startup dialogs (workspace trust, external CLAUDE.md imports, bypass permissions
# warning) render `Jn` with `cancelFirst: true, focus: "cancel"` so the cursor `❯` starts on row 0
# ("No, exit" / "No, continue..." / "No, disable...") and also remount `Jn` 150ms after opening
# (`remountKey: "held" -> "settled"`), resetting focus to "cancel" if Down was pressed too early.
TUI_TRUST_NO_FOCUSED_RE = re.compile(r"^\s*[❯>›▶]\s*(?:\d+\.\s*)?No\b", re.MULTILINE | re.IGNORECASE)
TUI_TRUST_OPEN_GUARD_SEC = 0.35
# D3: a known dialog must never receive the extra Enter. The omp ready capture carries this banner
# right above the composer ("Update Available / New version 18.17.0 is available"), so it counts.
TUI_UPDATE_DIALOG_PHRASES = (
    "Update Available",
    "New version ",
)
# W3/K2: the screen proves the agent is working or answering when one of these is visible.
# K2 removed the words that are NOT evidence, and every one of them came from a real capture:
#   - a bare "thinking": kimi's status bar permanently reads "K3.1 Pro thinking: low";
#   - a bare braille glyph: omp's status bar opens with one (" ⠧ 0s · ◒ DeepSeek Flash …"), so a
#     spinner-shaped rule would call omp busy forever.
# The phrases below were checked against the round-3 replays; each is only drawn while working.
# kimi "⠋ Working…" / "⠋ thinking…", omp "⎋ Working…", agy "⣷  Generating...",
# codex "Worked for 35s" / "esc to interrupt".
TUI_AGENT_ACTIVE_RE = re.compile(
    r"(?:Working…|Working\.\.\.|Working \(|Generating|Running a command|esc to interrupt|esc to cancel|Worked for"
    r"|thinking…|thinking\.\.\.)",
    re.IGNORECASE,
)
# K1: a visible composer does not mean the engine can take input yet. Every engine draws its
# prompt while it is still starting up and swallows the first Enter until that startup ends, so the
# readiness gate also waits for these indicators to leave the screen. F3: the lists live in
# transcript_summarizer, beside the screen filter that must ignore the same rows, so readiness and
# final-response extraction can never disagree about what "busy" or "still starting" looks like.
TUI_BUSY_PHRASES = ENGINE_BUSY_PHRASES
TUI_BUSY_LINE_RES = ENGINE_BUSY_LINE_RES
# K1 (fix 9): the banner an engine draws once a FRESH spawn can really take input. kimi draws a bare
# composer ~10 s before that, then "Preparing Kimi Code…", then (22-48 s after spawn in the probes)
# the Welcome box. An Enter before the Welcome box is swallowed or answered with "Error: LLM not
# set" (runs hub-03, early-01/02); one Enter after it submitted every time (welcome-01/02/03).
# A resume is not gated on it: kimi -S draws no composer until its Welcome box, the box scrolls off
# within seconds, and an Enter typed right at Welcome was still swallowed (resume probes, fix 9),
# while the ordinary composer + busy + stable gate submitted with one Enter.
TUI_READY_BANNERS = {
    "kimi": "Welcome to Kimi Code!",
}
# K1 (fix 9): a banner that never shows (a kimi update that changes the text) must not hang the
# spawn: this long after the trust dialog was answered (or after spawn without one) the gate falls
# back to composer + busy + stable, with a warning in the log. The fallback always leaves the
# reserve below inside the startup window, so the old gate still gets its chance to type.
TUI_READY_BANNER_FALLBACK_SEC = 60.0
TUI_READY_BANNER_RESERVE_SEC = 10.0
# K1 (fix 9): the banner may scroll above the visible screen before a poll sees it, so the recent
# scrollback is searched too, this many rows deep.
TUI_READY_BANNER_HISTORY_ROWS = 200
# K3 (fix 9): what proves (or refutes) one Enter, per engine. The defaults are the W3/K2 proofs.
#   cleared_composer  a composer that held the text and is now empty proves the submit. Not for
#                     kimi: it clears its box when it REJECTS an Enter ("Error: LLM not set", and no
#                     session is ever created -- early-01/02).
#   transcript        "growth": the transcript file grew after the Enter. "user_message": the
#                     transcript watcher recorded a new user message holding the text -- kimi's
#                     resumed wire.jsonl grows by itself at startup (MCP tool lists), so growth alone
#                     proves nothing there.
#   history_glyph     a submitted message is redrawn as a history row starting with this glyph; a
#                     new such row holding the text counts as screen evidence.
#   reject_phrases    the engine's refusal of an Enter; a new one on screen means "not submitted, the
#                     box was cleared" -- the caller waits for readiness and types the text again.
TUI_DEFAULT_SUBMIT_PROOF = {
    "cleared_composer": True,
    "transcript": "growth",
    "history_glyph": "",
    "reject_phrases": (),
}
TUI_SUBMIT_PROOF_RULES = {
    "kimi": {
        "cleared_composer": False,
        "transcript": "exact_user_message",
        "history_glyph": "",
        "reject_phrases": ("LLM not set",),
    },
    "claude": {
        "cleared_composer": False,
        "transcript": "exact_user_message",
        "history_glyph": "",
        "reject_phrases": (),
    },
    "omp": {
        "cleared_composer": False,
        "transcript": "exact_user_message",
        "history_glyph": "",
        "reject_phrases": (),
    },
    "agy": {
        "cleared_composer": False,
        "transcript": "exact_user_message",
        "history_glyph": "",
        "reject_phrases": (),
    },
    "codex": {
        "cleared_composer": False,
        "transcript": "exact_user_message",
        "history_glyph": "",
        "reject_phrases": (),
    },
}
# K2: how long a submit whose text was never seen on the composer keeps waiting for evidence. The
# kimi spawn in evidence round 3 typed into a box that was still booting, so the first look proved
# nothing; a redraw can also clear the bottom rows and put the text back a moment later.
TUI_SUBMIT_EVIDENCE_SEC = 15.0
# W9: the raw history is a bounded 512 KB window. Trimming rebuilds the joined text, so it runs
# with a little slack instead of on every 4 KB PTY chunk (the cap is still enforced on the tail).
HISTORY_TRIM_SLACK_BYTES = 64 * 1024
HISTORY_TRUNCATED_PREFIX = "\x1b[90m[较早的输出已截断]\x1b[0m\r\n"
# R7: an agy resume whose archived transcript is still locked by another watcher gets this long
# to become claimable, then the watcher stops instead of spinning for the session's lifetime.
AGY_RESUME_TRANSCRIPT_WAIT_SEC = 120.0


def resume_banner(resume_count: int) -> str:
    """The separator a revived pane shows between the archived output and the new one (spec §5.3)."""
    return f"\r\n\x1b[90m── 已恢复（第 {int(resume_count)} 次）──\x1b[0m\r\n"


def trim_history_text(text: str, max_bytes: int) -> str:
    """W9: cap a raw terminal history at max_bytes without cutting a line or the newest banner.

    The old buffer dropped whole PTY chunks, so the archived scrollback and every resume banner
    left in one step. This cuts bytes from the front, moves to the next line break, prefixes a
    grey notice, and never eats the newest "── 已恢复（第 N 次）──" line (nor what follows it)
    unless that part alone is larger than the whole budget.
    """
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text
    prefix = HISTORY_TRUNCATED_PREFIX
    budget = max_bytes - len(prefix.encode("utf-8"))
    if budget <= 0:
        return prefix
    start = len(raw) - budget
    next_break = raw.find(b"\n", start)
    if next_break >= 0:
        start = next_break + 1
    # Keep the newest banner and everything after it when that tail alone fits.
    marker = "\x1b[90m── 已恢复（"
    idx = text.rfind(marker)
    if idx >= 0:
        banner_start = len(text[:idx].encode("utf-8", "replace"))
        if banner_start >= 2 and text[:idx].endswith("\r\n"):
            banner_start -= 2
        if len(raw) - banner_start <= budget and banner_start < start:
            start = banner_start
    body = raw[start:].decode("utf-8", "replace")
    return prefix + body


def _store_root(env_name: str, default: Path) -> Path:
    """A CLI store root; the HUB_* override lets tests point at a synthetic copy (never the real store)."""
    raw = os.environ.get(env_name)
    if raw and raw.strip():
        return Path(os.path.expanduser(raw.strip()))
    return default


def _directory_is_writable(path: Path) -> bool:
    """Check whether a child process can create files below *path*.

    ``os.access`` is not sufficient on Windows when the Hub runs under a restricted token, so
    use the same create/delete operation the CLI will perform.  The probe is deliberately tiny
    and is removed immediately.
    """
    probe = None
    try:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".hub-write-probe-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except (OSError, PermissionError):
        try:
            if probe is not None:
                probe.unlink(missing_ok=True)
        except Exception:
            pass
        return False
def codex_sessions_root() -> Path:
    return _store_root("HUB_CODEX_SESSIONS_ROOT", Path(os.path.expanduser("~")) / ".codex" / "sessions")


def kimi_store_root() -> Path:
    return _store_root("HUB_KIMI_STORE_ROOT", Path(os.path.expanduser("~")) / ".kimi-code")


def claude_config_dir(override: Optional[str] = None) -> Path:
    raw = (override or os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if raw:
        return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(raw))))
    return Path(os.path.expanduser("~")) / ".claude"


def claude_projects_root(config_dir: Optional[str] = None) -> Path:
    env_override = (os.environ.get("HUB_CLAUDE_PROJECTS_ROOT") or "").strip()
    if env_override:
        return Path(os.path.expanduser(env_override))
    if config_dir and str(config_dir).strip():
        return claude_config_dir(str(config_dir)) / "projects"
    return claude_config_dir() / "projects"


def agy_brain_root() -> Path:
    """V3: agy 1.5.2 writes conversations to ~/.gemini/antigravity-cli/brain.

    The old ~/.gemini/antigravity/brain is only the fallback for an install that still uses it;
    HUB_AGY_BRAIN_ROOT keeps overriding either for tests.
    """
    raw = os.environ.get("HUB_AGY_BRAIN_ROOT")
    if raw and raw.strip():
        return Path(os.path.expanduser(raw.strip()))
    cli_root = Path(os.path.expanduser("~")) / ".gemini" / "antigravity-cli"
    if (cli_root / "brain").is_dir():
        return cli_root / "brain"
    return Path(os.path.expanduser("~")) / ".gemini" / "antigravity" / "brain"


_READ_EXEC_RE = re.compile(
    r"^read\s+(?:[\"'`]?[A-Za-z]:[\\/][^\s\"'`]+[\"'`]?|[\"'`]?[^\s\"'`:]+[\"'`]?)"
    r"(?:\s+whole)?\s+and\s+(?:execute|run|implement|verify|fix|complete)\b\s*[:：]?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_LEADING_QUOTED_SUBJECT_RE = re.compile(r"^[\"'“”‘’《》]([^\"'“”‘’《》\r\n]{2,36})[\"'“”‘’《》]")
_INLINE_PATH_RE = re.compile(r"[\"'`]?(?:[A-Za-z]:[\\/]|/[A-Za-z0-9._-]+/)[^\s\"'`，,。；;)]+[\"'`]?")
_POLITE_PREFIX_RE = re.compile(
    r"^(?:请你|请帮我|帮我|麻烦你|麻烦|请先|现在请|请|你需要|需要你|任务[:：]|目标[:：]|背景[:：]|please\s+(?:help\s+me\s+)?)\s*",
    re.IGNORECASE,
)


def derive_readable_session_title(
    title: Optional[str],
    task: Optional[str],
    prof_name: str = "",
    session_id: str = "",
    max_len: int = 24,
) -> str:
    """Returns a short, human-readable session title instead of raw prompt prefixes or bare IDs."""
    raw_title = " ".join(str(title or "").splitlines()).strip()
    auto_default = f"{prof_name} ({session_id})".strip() if (prof_name or session_id) else ""
    if (
        raw_title
        and raw_title != auto_default
        and raw_title != session_id
        and not (session_id and raw_title.endswith(f"({session_id})"))
        and not re.search(r"\([a-z0-9_-]+-[0-9a-f]{6,}\)\s*$", raw_title, re.IGNORECASE)
    ):
        if len(raw_title) > 40:
            return raw_title[:39].rstrip() + "…"
        return raw_title

    raw_task = re.sub(r"\[Sub-Agent Bridge Info[\s\S]*$", "", str(task or ""), flags=re.IGNORECASE).strip()
    if not raw_task:
        return prof_name or "交互会话"

    m_exec = _READ_EXEC_RE.match(raw_task.strip())
    if m_exec:
        rest = (m_exec.group(1) or "").strip()
        m_quote = _LEADING_QUOTED_SUBJECT_RE.match(rest)
        if m_quote:
            subject = m_quote.group(1).strip()
            return subject[: max_len - 1].rstrip() + "…" if len(subject) > max_len else subject
        if rest:
            raw_task = rest

    chosen = ""
    for line in raw_task.splitlines():
        cleaned = re.sub(r"^[#>*\-\d.)\s`]+", "", line.strip()).replace("`", "").strip()
        if not cleaned:
            continue
        m_line_exec = _READ_EXEC_RE.match(cleaned)
        if m_line_exec:
            rest = (m_line_exec.group(1) or "").strip()
            m_quote = _LEADING_QUOTED_SUBJECT_RE.match(rest)
            if m_quote:
                chosen = m_quote.group(1).strip()
                break
            cleaned = rest
        cleaned = _INLINE_PATH_RE.sub("", cleaned).strip(" ，,。；;:-—·")
        cleaned = _POLITE_PREFIX_RE.sub("", cleaned).strip(" ，,。；;:-—·")
        if cleaned:
            chosen = cleaned
            break

    if not chosen:
        chosen = _INLINE_PATH_RE.sub("", re.sub(r"\s+", " ", raw_task)).strip()

    first_clause = re.split(r"[。！？!?；;\n]|，|,\s+", chosen, maxsplit=1)[0].strip() or chosen
    if len(first_clause) > max_len:
        return first_clause[: max_len - 1].rstrip() + "…"
    return first_clause or prof_name or "交互会话"


def _pid_alive(pid: int) -> bool:
    """True while a process with this pid exists (used for the kimi per-session lock)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _is_file_lock_error(exc: BaseException) -> bool:
    """True for the Windows errors a held file raises: sharing/lock violations (32/33) and EACCES."""
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in (32, 33)


def hub_port() -> int:
    """The port this hub really listens on (`HUB_PORT`, default 4444)."""
    try:
        return int(os.environ.get("HUB_PORT") or DEFAULT_HUB_PORT)
    except ValueError:
        return DEFAULT_HUB_PORT


def hub_api_base_url() -> str:
    return os.environ.get("HUB_API_URL") or f"http://127.0.0.1:{hub_port()}"


def default_profiles_path(base_dir: Optional[Path] = None) -> Path:
    """User-local profile storage path outside the git repo (`%APPDATA%\\mcp-agent-hub\\agent_profiles.json`).

    `HUB_PROFILES_PATH` overrides the path for tests. On first run (when the user-local file does
    not exist yet), seeds it from `<repo>/agent_profiles.json` if present so existing local edits
    migrate seamlessly without dirtying the git working tree.
    """
    override = (os.environ.get("HUB_PROFILES_PATH") or "").strip()
    if override:
        target = Path(override).expanduser().resolve()
    else:
        appdata = os.environ.get("APPDATA")
        config_root = Path(appdata) if appdata else (Path.home() / ".config")
        target = config_root / "mcp-agent-hub" / "agent_profiles.json"

    target.parent.mkdir(parents=True, exist_ok=True)
    repo_root = (base_dir or Path(__file__).parent).resolve()
    legacy_seed = repo_root / "agent_profiles.json"
    if not target.exists() and legacy_seed.exists() and legacy_seed.resolve() != target.resolve():
        try:
            shutil.copyfile(legacy_seed, target)
        except OSError:
            pass
    return target


def split_agy_model_id(model_id: str) -> tuple:
    """`gpt-oss-120b-medium` -> ("gpt-oss-120b", "medium"); a bare base id keeps a None level."""
    mid = model_id or ""
    m = AGY_SUFFIX_RE.search(mid)
    if not m:
        return mid, None
    return mid[: m.start()], m.group(1)


def _injected_flag_tokens(engine: str, args: List[str]):
    """Yields (index, flag label) for every argument the hub injects itself, for one engine."""
    eng = (engine or "").strip().lower()
    i = 0
    total = len(args)
    while i < total:
        a = str(args[i])
        if eng == "omp":
            if a.startswith("--model=") or a.startswith("--thinking="):
                yield i, a.split("=", 1)[0] + "="
                i += 1
                continue
        elif eng == "kimi":
            if a in ("-m", "--model"):
                yield i, a
                if i + 1 < total:
                    yield i + 1, a
                i += 2
                continue
        elif eng == "agy":
            if a in ("--model", "--effort"):
                yield i, a
                if i + 1 < total:
                    yield i + 1, a
                i += 2
                continue
        elif eng == "codex":
            if a in ("-m", "--model"):
                yield i, a
                if i + 1 < total:
                    yield i + 1, a
                i += 2
                continue
            if a == "-c" and i + 1 < total:
                nxt = str(args[i + 1]).strip().lstrip('"').lstrip("'")
                if nxt.startswith("model_reasoning_effort") or nxt.startswith("model="):
                    yield i, "-c"
                    yield i + 1, "-c"
                    i += 2
                    continue
        elif eng == "claude":
            if a in ("--model", "--effort"):
                yield i, a
                if i + 1 < total:
                    yield i + 1, a
                i += 2
                continue
            if a.startswith("--model=") or a.startswith("--effort="):
                yield i, a.split("=", 1)[0] + "="
                i += 1
                continue
        i += 1


def injected_flags_in_args(engine: str, args: List[str]) -> List[str]:
    """Every distinct flag in `args` that the hub injects at launch time, in first-appearance order."""
    labels: List[str] = []
    for _idx, label in _injected_flag_tokens(engine, args):
        if label not in labels:
            labels.append(label)
    return labels


def strip_injected_flags(engine: str, args: List[str]) -> List[str]:
    """Removes every hub-injected model/thinking flag, so old profile files still launch cleanly."""
    drop = {idx for idx, _label in _injected_flag_tokens(engine, args)}
    return [str(a) for i, a in enumerate(args) if i not in drop]


# ── Approval by default (owner decision 2026-11-07, DECISIONS.md D1-D5/D9) ─────────────────────
# Every engine runs in the mode where routine work runs on its own and high-risk operations stop
# for approval (the main agent answers through respond_to_subagent). The hub
# strips each engine's bypass/mode flags from the profile (or the archived snapshot on resume) and
# injects its own, in the same place as the model/thinking injection. The only way out is the
# profile's own `bypass_approvals: true` (round 2, see bypass_launch_flags below).
ENGINE_POLICIES_DIR = Path(__file__).resolve().parent / "engine_policies"
OMP_APPROVAL_OVERLAY_NAME = "omp-approval.yml"
CLAUDE_APPROVAL_SETTINGS_NAME = "claude-approval.json"
# D5: agy's `--mode auto` falls back to `default` at launch (its check runs before sign-in ends);
# `--settings` (this session only, never saved) applies agentMode after sign-in, before turn one.
AGY_SESSION_SETTINGS: Dict[str, Any] = {"agentMode": "auto"}


def omp_approval_overlay_path() -> Path:
    """D2: the omp settings overlay shipped in the repo (bash allowed, high-risk patterns prompt)."""
    return ENGINE_POLICIES_DIR / OMP_APPROVAL_OVERLAY_NAME


def claude_approval_settings_path() -> Path:
    """The Claude Code settings overlay shipped in the repo (high-risk PowerShell/Bash patterns ask)."""
    template_path = ENGINE_POLICIES_DIR / CLAUDE_APPROVAL_SETTINGS_NAME
    hook_script = ENGINE_POLICIES_DIR / "claude_pre_tool_hook.py"
    if template_path.is_file() and hook_script.is_file():
        expected_cmd = f'"{sys.executable}" "{hook_script}"'
        try:
            data = json.loads(template_path.read_text(encoding="utf-8"))
            hooks_map = data.setdefault("hooks", {})
            for event_key, default_matcher in (("PreToolUse", "Bash|PowerShell"), ("PermissionRequest", "")):
                entries = hooks_map.get(event_key)
                if not isinstance(entries, list) or not entries:
                    hooks_map[event_key] = [
                        {
                            "matcher": default_matcher,
                            "hooks": [{"type": "command", "command": expected_cmd}],
                        }
                    ]
                elif isinstance(entries[0], dict):
                    inner = entries[0].get("hooks") or []
                    if inner and isinstance(inner[0], dict):
                        inner[0]["command"] = expected_cmd
            runtime_dir = ENGINE_POLICIES_DIR.parent / ".runtime_mcp_configs"
            runtime_dir.mkdir(exist_ok=True)
            runtime_path = runtime_dir / CLAUDE_APPROVAL_SETTINGS_NAME
            rendered = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
            if not runtime_path.is_file() or runtime_path.read_text(encoding="utf-8", errors="replace") != rendered:
                runtime_path.write_text(rendered, encoding="utf-8")
            return runtime_path
        except Exception:
            pass
    return template_path


def _is_omp_overlay_path(value: str) -> bool:
    raw = str(value or "").strip().strip('"').strip("'")
    return bool(raw) and os.path.basename(raw.replace("\\", "/")).lower() == OMP_APPROVAL_OVERLAY_NAME


def _is_claude_approval_settings_path(value: str) -> bool:
    raw = str(value or "").strip().strip('"').strip("'")
    return bool(raw) and os.path.basename(raw.replace("\\", "/")).lower() == CLAUDE_APPROVAL_SETTINGS_NAME


def _approval_flag_tokens(engine: str, args: List[str]):
    """Yields (index, flag label) for every approval/bypass/mode argument of one engine (D1)."""
    eng = (engine or "").strip().lower()
    total = len(args)
    i = 0

    def nxt(k: int) -> str:
        return str(args[k + 1]) if k + 1 < total else ""

    while i < total:
        a = str(args[i])
        width = 0
        label = a
        if eng == "omp":
            if a == "--auto-approve" or a.startswith("--approval-mode="):
                width, label = 1, a.split("=", 1)[0]
            elif a == "--approval-mode":
                width = 2
            elif a.startswith("--config=") and _is_omp_overlay_path(a.split("=", 1)[1]):
                width, label = 1, "--config="
            elif a == "--config" and _is_omp_overlay_path(nxt(i)):
                width = 2
        elif eng == "kimi":
            if a in ("--auto", "--yolo", "-y"):
                width = 1
        elif eng == "codex":
            if a in ("--dangerously-bypass-approvals-and-sandbox", "--full-auto", "--approve-for-me"):
                width = 1
            elif a in ("-a", "--ask-for-approval", "-s", "--sandbox"):
                width = 2
            elif a.startswith("--ask-for-approval=") or a.startswith("--sandbox="):
                width, label = 1, a.split("=", 1)[0]
            elif a in ("-c", "--config"):
                value = nxt(i).strip().lstrip('"').lstrip("'")
                if value.startswith("approval_policy") or value.startswith("sandbox_mode"):
                    width, label = 2, f"{a} {value.split('=', 1)[0].strip()}"
        elif eng == "agy":
            if a == "--dangerously-skip-permissions":
                width = 1
            elif a in ("--mode", "--settings"):
                width = 2
            elif a.startswith("--mode=") or a.startswith("--settings="):
                width, label = 1, a.split("=", 1)[0]
        elif eng == "claude":
            if a in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"):
                width = 1
            elif a == "--permission-mode":
                width = 2
            elif a.startswith("--permission-mode="):
                width, label = 1, "--permission-mode"
            elif a.startswith("--settings=") and _is_claude_approval_settings_path(a.split("=", 1)[1]):
                width, label = 1, "--settings="
            elif a == "--settings" and _is_claude_approval_settings_path(nxt(i)):
                width = 2
        if width:
            for k in range(i, min(i + width, total)):
                yield k, label
            i += width
            continue
        i += 1


def approval_flags_in_args(engine: str, args: List[str]) -> List[str]:
    """The approval/bypass/mode flags present in `args` (labels, first-appearance order)."""
    labels: List[str] = []
    for _idx, label in _approval_flag_tokens(engine, args):
        if label not in labels:
            labels.append(label)
    return labels


def _agy_settings_object(raw: str) -> Optional[Dict[str, Any]]:
    """A `--settings` value (inline JSON object or a JSON file path) as a dict; None if unreadable."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.startswith("{"):
            obj = json.loads(text)
        else:
            obj = json.loads(Path(os.path.expanduser(text.strip('"').strip("'"))).read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _agy_profile_settings(args: List[str]) -> Dict[str, Any]:
    """Every readable `--settings` object in the profile args, merged in order (later keys win)."""
    merged: Dict[str, Any] = {}
    total = len(args)
    i = 0
    while i < total:
        a = str(args[i])
        value: Optional[str] = None
        if a == "--settings" and i + 1 < total:
            value = str(args[i + 1])
            i += 1
        elif a.startswith("--settings="):
            value = a.split("=", 1)[1]
        if value is not None:
            merged.update(_agy_settings_object(value) or {})
        i += 1
    return merged


def approval_launch_flags(engine: str, profile_args: Optional[List[str]] = None) -> List[str]:
    """The flags the hub injects so `engine` runs with high-risk approval on (D2-D5)."""
    eng = (engine or "").strip().lower()
    if eng == "omp":
        return ["--approval-mode=write", f"--config={omp_approval_overlay_path()}"]
    if eng == "kimi":
        return ["-y"]
    if eng == "codex":
        return ["-a", "on-request", "-s", "workspace-write"]
    if eng == "claude":
        return [
            "--permission-mode",
            "acceptEdits",
            f"--settings={claude_approval_settings_path()}",
        ]
    if eng == "agy":
        settings = _agy_profile_settings(list(profile_args or []))
        settings.update(AGY_SESSION_SETTINGS)
        return ["--settings", json.dumps(settings, ensure_ascii=False, separators=(",", ":"))]
    return []


# Round 2 (owner, decision page approval-by-default-2026-11-07): a profile may switch approval off
# with `bypass_approvals: true`. The hub then injects that engine's full-bypass flag instead of the
# approval flags. Only the engines below have approval flags; dsh and pwsh ignore the key.
APPROVAL_ENGINES = ("omp", "kimi", "codex", "claude", "agy")
BYPASS_APPROVALS_KEY = "bypass_approvals"
BYPASS_ON_NOTE = "bypass_approvals is on：本次会话已关闭高风险审批（全自动），高风险操作不会暂停等待审批"
BYPASS_NO_EFFECT_NOTE = "bypass_approvals 对 {engine} 引擎不适用（该引擎没有 Hub 注入的审批参数），已忽略"


def profile_bypasses_approvals(profile: Optional[Dict[str, Any]]) -> bool:
    """True only for a stored boolean true; a missing key (or anything else) means approval stays on."""
    return isinstance(profile, dict) and profile.get(BYPASS_APPROVALS_KEY) is True


def resolve_session_approval(engine: str, profile: Dict[str, Any], override: Optional[bool]) -> Dict[str, Any]:
    """Resolve a session-only override without changing the stored profile."""
    if override is not None and type(override) is not bool:
        raise ValueError("bypass_approvals 只能是 true、false 或 null")
    applicable = engine in APPROVAL_ENGINES
    return {
        "bypass_approvals_override": override,
        "bypass_approvals": (profile_bypasses_approvals(profile) if override is None else override) if applicable else False,
        "approval_source": ("profile" if override is None else "session") if applicable else "not_applicable",
    }


def high_risk_approval_state(engine: Optional[str], bypass: bool) -> Optional[bool]:
    """What the session runs with: True = high-risk approval on, False = bypassed, None = the engine
    has no hub-injected approval (dsh, pwsh, desktop GUI)."""
    eng = (engine or "").strip().lower()
    if eng not in APPROVAL_ENGINES:
        return None
    return not bypass


def resolve_session_approval_fields(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Public launch policy fields; old snapshots keep inheriting the profile on resume."""
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot) if snapshot else {}
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    snapshot = snapshot or {}
    return {"bypass_approvals": snapshot.get("bypass_approvals") is True,
            "bypass_approvals_override": snapshot.get("bypass_approvals_override"),
            "approval_source": snapshot.get("approval_source") or (
                "profile" if snapshot.get("engine") in APPROVAL_ENGINES else "not_applicable")}


def bypass_launch_flags(engine: str, profile_args: Optional[List[str]] = None) -> List[str]:
    """The engine's old full-bypass flag, injected instead of the approval flags when bypassed."""
    eng = (engine or "").strip().lower()
    if eng == "omp":
        return ["--auto-approve"]
    if eng == "kimi":
        return ["--auto"]
    if eng == "codex":
        return ["--dangerously-bypass-approvals-and-sandbox"]
    if eng == "claude":
        return ["--dangerously-skip-permissions"]
    if eng == "agy":
        # The profile's own --settings keys survive; agentMode is the bypass flag's business now.
        settings = _agy_profile_settings(list(profile_args or []))
        settings.pop("agentMode", None)
        extra = ["--settings", json.dumps(settings, ensure_ascii=False, separators=(",", ":"))] if settings else []
        return ["--dangerously-skip-permissions"] + extra
    return []


def apply_approval_flags(engine: str, args: List[str], bypass: bool = False) -> List[str]:
    """D1/D9: strip every approval/bypass/mode flag, then inject the hub's own at the front: the
    approval flags, or the engine's full-bypass flag when the profile has bypass_approvals on."""
    raw = [str(a) for a in args]
    drop = {idx for idx, _label in _approval_flag_tokens(engine, raw)}
    kept = [a for i, a in enumerate(raw) if i not in drop]
    inject = bypass_launch_flags(engine, raw) if bypass else approval_launch_flags(engine, raw)
    return inject + kept


# ── D7: reading the engines' approval prompts and answering them ──────────────────────────────
# What an option of an approval prompt does when chosen. "persist" writes a permanent rule (agy
# option 3 -> settings.json, codex `p` -> ~/.codex/rules/default.rules): the hub never sends it.
APPROVAL_APPROVE_DECISIONS = ("approve", "allow", "allow_once", "approve_once", "yes", "y", "ok", "accept")
APPROVAL_SESSION_DECISIONS = (
    "approve_session", "allow_session", "always", "a", "session",
    "approve_for_this_session", "allow_for_this_session",
)
APPROVAL_DENY_DECISIONS = ("deny", "reject", "no", "n", "cancel")
# omp ignores keys for 500 ms after its prompt opens (research 2026-11-07); the hub waits longer.
OMP_PROMPT_KEY_GUARD_SEC = 0.6
# agy: after a deny the turn ends; the feedback is typed as the next message once the composer is back.
AGY_DENY_FEEDBACK_DELAY_SEC = 1.2
# A machine record (kimi wire) of a prompt the screen detector already registered and the parent
# already answered is the same prompt if it arrives this soon after the screen registration.
SCREEN_ADOPT_WINDOW_SEC = 20.0
# After an interaction is answered or resolved, the screen detector waits this long before it
# reads prompts again, so the closing prompt still on screen is not registered a second time.
TUI_PROMPT_REDETECT_QUIET_SEC = 2.5
_OPTION_LINE_RE = re.compile(r"^\s*[>❯›▶]?\s*(\d+)\.\s+(.*\S)\s*$")


def classify_approval_label(label: str) -> str:
    """approve_once | approve_session | persist | deny | deny_feedback for one option label."""
    low = (
        str(label or "")
        .strip()
        .lower()
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u02bc", "'")
    )
    if "with feedback" in low or "deny and send" in low:
        return "deny_feedback"
    # Round 3: checked before the deny prefixes, because agy also has deny options that persist
    # (`No, and always deny tool '…' (Persist to settings.json)`); those are never sent either.
    if "persist" in low or "settings.json" in low or low.endswith("(p)"):
        return "persist"
    if low.startswith("no") or low.startswith("reject") or low.startswith("deny") or "cancel" == low:
        # agy's `No, and always deny … in this conversation` is a conversation-wide deny rule, not
        # the plain deny the parent asks for: it is kept apart and not offered.
        if "always deny" in low:
            return "deny_session"
        return "deny"
    if ("for this session" in low or "during this session" in low or "in this conversation" in low
            or "don't ask again for these files" in low):
        return "approve_session"
    if "always" in low or "don't ask again" in low:
        return "persist"
    return "approve_once"


def _numbered_options(lines: List[str]) -> List[Dict[str, Any]]:
    """The last block of `N. label` lines; a wrapped label's continuation lines are joined to it."""
    blocks: List[List[Dict[str, Any]]] = []
    current: Optional[List[Dict[str, Any]]] = None
    for raw in lines:
        m = _OPTION_LINE_RE.match(raw)
        if m:
            key, label = m.group(1), m.group(2).strip()
            if current is None or key == "1":
                current = []
                blocks.append(current)
            current.append({"key": key, "label": label})
            continue
        stripped = raw.strip()
        # A continuation of the previous label (agy wraps long ones at the screen edge); a blank
        # line or the prompt's own key hint line ends the block.
        if current and stripped and not any(
            h in stripped for h in ("Navigate", "Press enter", "choose", "esc to cancel")
        ):
            current[-1]["label"] = f"{current[-1]['label']} {stripped}"
            continue
        current = None
    return blocks[-1] if blocks else []


def parse_omp_approval_prompt(screen: str) -> Optional[Dict[str, Any]]:
    """omp's tool approval box: `Allow tool: <name>` header, `Approve…` and `Deny` rows (D7).

    Returns {tool, reason, command, options, session_label, deny_label} or None. The optional
    `Approve <subject> for this session` row only shows for plain write-mode prompts, not for
    pattern / critical-pattern prompts.
    """
    lines = str(screen or "").splitlines()
    head = None
    for idx, line in enumerate(lines):
        if "Allow tool:" in line:
            head = idx
    if head is None:
        return None
    tool = lines[head].split("Allow tool:", 1)[1].strip(" ─━│╮╭").strip()
    rows: List[str] = []
    for line in lines[head + 1:]:
        s = line.strip()
        if s.startswith(("├", "╰", "└", "┣", "┗")):
            break
        s = s.strip("│┃║").strip()
        rows.append(s)
    options: List[str] = []
    details: List[str] = []
    selected_index = 0
    for s in rows:
        label = s.lstrip("❯>›").strip()
        if label == "Approve" or label.startswith(("Approve ", "Always Approve")) or label.startswith("Deny"):
            if s[:1] in ("❯", ">", "›"):
                selected_index = len(options)
            options.append(label)
        elif not options and s:
            details.append(s)
    approve = [o for o in options if o.startswith(("Approve", "Always Approve"))]
    deny = [o for o in options if o.startswith("Deny")]
    if not approve or not deny:
        return None
    reason = ""
    command_lines: List[str] = []
    for s in details:
        if s.startswith("Reason:"):
            reason = s.split(":", 1)[1].strip()
        elif s.startswith("Command:"):
            command_lines.append(s.split(":", 1)[1].strip())
        elif command_lines:
            command_lines.append(s)
    session_label = next(
        (o for o in approve if o.startswith("Approve ") and o.endswith("for this session")), None
    )
    return {
        "tool": tool,
        "reason": reason,
        "command": " ".join(command_lines).strip(),
        "details": details,
        "options": options,
        "selected_index": selected_index,
        "session_label": session_label,
        "deny_label": deny[0],
    }


def parse_agy_approval_prompt(screen: str) -> Optional[Dict[str, Any]]:
    """agy's permission prompt (`Run this command?` + numbered Yes/No options, digits act at once).

    The options are read from the screen and mapped by label, so a prompt with another option
    count still gets the right key. Returns {header, reason, command, options:[{key,label,action}]}.
    """
    lines = str(screen or "").splitlines()
    text = "\n".join(lines)
    head = None
    for idx, line in enumerate(lines):
        s = line.strip()
        # Round 3: agy's other prompt questions (its string table): `Allow calling this tool?`
        # (MCP tools not covered by an allow rule, 6 options), `Allow access to this file?`,
        # `Allow creation of this file?`, `Allow sandbox bypass for command execution?`, ...
        if s.startswith(("Run this command?", "Requesting permission for", "Allow access to",
                         "Accept this file edit")) or (s.startswith("Allow ") and s.endswith("?")):
            head = idx
    if head is None:
        # A prompt worded differently (file / URL access, not captured yet): Yes/No options with
        # agy's "esc to cancel" hint, and not its ask_question selector.
        if "esc to cancel" not in text or "Question 1/" in text:
            return None
        head = 0
    opts = _numbered_options(lines[head:])
    if not opts and "Yes, allow" in text and "No, deny" in text:
        # The older two-row prompt without numbers: Enter takes the first row, Down+Enter the second.
        opts = [{"key": "\r", "label": "Yes, allow"}, {"key": "\x1b[B\r", "label": "No, deny"}]
    if not opts:
        return None
    for o in opts:
        o["action"] = classify_approval_label(o["label"])
    if not any(o["action"] == "approve_once" for o in opts) or not any(o["action"] == "deny" for o in opts):
        return None
    reason = ""
    command_lines: List[str] = []
    collecting = False
    for line in lines:
        s = line.strip()
        if s.startswith("Reason:"):
            reason = s.split(":", 1)[1].strip()
        elif s.startswith("Requesting permission for"):
            collecting = True
            command_lines = []
        elif collecting:
            if not s or s.startswith("Run this command?"):
                collecting = False
            else:
                command_lines.append(s)
    header = lines[head].strip() if lines else ""
    command = " ".join(command_lines).strip()
    if not command and header.startswith("Allow ") and header.endswith("?"):
        # The subject of an `Allow …?` prompt is the block between the section's rule line and the
        # question: the MCP tool (`server/tool`) or the file path (captured 2026-11-07, agy2\run6).
        subject: List[str] = []
        for line in reversed(lines[:head]):
            s = line.strip()
            if (s and set(s) <= set("─━-═")) or len(subject) >= 4:
                break
            if s:
                subject.insert(0, s)
        command = " ".join(subject).strip()
    return {
        "header": header,
        "reason": reason,
        "command": command,
        "options": opts,
    }


# A line that ends the `$ <command>` block of an approval prompt (besides a blank line or the first
# numbered option): the prompt's own field labels and key hints.
_PROMPT_FIELD_PREFIXES = (
    "Reason:", "Environment:", "Destination:", "Description:", "cwd:", "Dangerous:",
    "Press enter", "Run this command?", "Would you like",
)


def _dollar_command(lines: List[str]) -> str:
    """The command of the first `$ <command>` line, with its wrapped continuation lines joined.

    A long command wraps on screen (live check 2026-11-07: codex cut `Remove-Item … -LiteralPath`
    from its target on the next line); the continuation runs up to a blank line, the first numbered
    option, or the prompt's next field. The TUIs wrap at word boundaries, so the pieces are joined
    with one space.
    """
    parts: List[str] = []
    for raw in lines:
        s = raw.strip().strip("│┃║").strip()
        if not parts:
            if s.startswith("$ "):
                parts.append(s[2:].strip())
            continue
        if not s or _OPTION_LINE_RE.match(s) or s.startswith(_PROMPT_FIELD_PREFIXES):
            break
        parts.append(s)
    return " ".join(p for p in parts if p).strip()


_CODEX_SHORTCUT_RE = re.compile(r"\(([A-Za-z]+)\)\s*$")


def _codex_option_action(label: str, shortcut: str) -> str:
    """What a codex option does, by its shortcut key read from the screen: y once, p (command
    prompt: permanent allow rule for the command prefix) and a (edit prompt: these files) are the
    prompt's "don't ask again" choice, esc / n deny."""
    key = (shortcut or "").lower()
    if key == "y":
        return "approve_once"
    if key in ("p", "a"):
        return "approve_session"
    if key in ("esc", "n"):
        return "deny"
    return classify_approval_label(label)


def parse_codex_approval_prompt(screen: str) -> Optional[Dict[str, Any]]:
    """codex's approval modal: a command prompt or an edit prompt, with its numbered options.

    Each option carries the shortcut key shown at the end of its label (`(y)`, `(p)`, `(a)`,
    `(esc)`); session_key is the shortcut of the prompt's "don't ask again" option as read from the
    screen (`p` on command prompts, `a` on edit prompts), or "" when the screen shows none.
    """
    text = str(screen or "")
    # MCP approvals use the native selector rather than the command/edit y/n shortcuts.
    mcp_header = re.search(r'Allow the\s+(.+?)\s+MCP server to run tool\s+"([^"]+)"\?', text, re.DOTALL)
    if mcp_header:
        body = text[mcp_header.end():]
        opts = _numbered_options(body.splitlines())
        for option in opts:
            label = option["label"].strip().lower()
            option["action"] = "deny" if label.startswith("cancel") else (
                "approve_once" if label.startswith("allow") else "unknown"
            )
        # Require a complete modal, not an echoed task or a partially drawn title.
        if {o["action"] for o in opts} >= {"approve_once", "deny"} and "enter to submit" in body.lower():
            arguments = re.split(r"(?m)^\s*[›>]?\s*\d+\.\s", body, maxsplit=1)[0].strip()
            return {"prompt_kind": "mcp", "header": " ".join(mcp_header.group(0).split()),
                    "server": " ".join(mcp_header.group(1).split()), "tool": mcp_header.group(2),
                    "arguments": arguments, "options": opts, "session_key": ""}
        return None
    if "Would you like to run the following command?" in text:
        kind, marker = "command", "Would you like to run the following command?"
    elif "Would you like to make the following edits?" in text:
        kind, marker = "edit", "Would you like to make the following edits?"
    else:
        return None
    lines = text.splitlines()
    head = max(i for i, line in enumerate(lines) if marker in line)
    opts = _numbered_options(lines[head + 1:])
    session_key = ""
    for o in opts:
        m = _CODEX_SHORTCUT_RE.search(o["label"])
        o["shortcut"] = m.group(1).lower() if m else ""
        o["action"] = _codex_option_action(o["label"], o["shortcut"])
        if o["action"] == "approve_session" and o["shortcut"] in ("p", "a") and not session_key:
            session_key = o["shortcut"]
    reason_parts: List[str] = []
    destination = ""
    in_reason = False
    for line in lines[head + 1:]:
        s = line.strip()
        if s.startswith("Reason:"):
            reason_parts = [s.split(":", 1)[1].strip()]
            in_reason = True
            continue
        if in_reason:
            # A long reason wraps like a long command does.
            if s and not s.startswith("$ ") and not _OPTION_LINE_RE.match(s) \
                    and not s.startswith(_PROMPT_FIELD_PREFIXES):
                reason_parts.append(s)
                continue
            in_reason = False
        if s.startswith("Destination:"):
            destination = s.split(":", 1)[1].strip()
    reason = " ".join(p for p in reason_parts if p).strip()
    command = _dollar_command(lines[head + 1:])
    return {"prompt_kind": kind, "reason": reason, "command": command,
            "destination": destination, "options": opts, "session_key": session_key}


def kimi_panel_command(screen: str) -> str:
    """The `$ <command>` of kimi's approval panel, wrapped lines joined (empty when there is none)."""
    return _dollar_command(str(screen or "").splitlines())


_CLAUDE_APPROVAL_HEADERS = (
    "Do you want to proceed?",
    "Do you want to make this edit",
    "Do you want to create",
    "Do you want to overwrite",
    "Do you want to delete",
    "Do you want to run",
    "Do you want to allow",
)
_CLAUDE_SOLID_RULE_CHARS = set("─━-═╭╮╰╯┌┐└┘")
_CLAUDE_DASHED_RULE_CHARS = set("╌╍┄┅┈┉")
_CLAUDE_ALL_RULE_CHARS = _CLAUDE_SOLID_RULE_CHARS | _CLAUDE_DASHED_RULE_CHARS
_CLAUDE_TOOL_TITLES = (
    "powershell command",
    "bash command",
    "command",
    "edit file",
    "create file",
    "tool use",
)
_CLAUDE_REASON_PREFIXES = (
    "permission rule ",
    "command contains ",
    "command invokes ",
    "this command ",
)


def parse_claude_approval_prompt(screen: str) -> Optional[Dict[str, Any]]:
    """Claude Code's permission prompt (`Do you want to proceed?` + numbered options, digits act at once).

    Returns {header, reason, command, options:[{key,label,action,persists?}]} or None.
    """
    lines = str(screen or "").splitlines()
    head = None
    for idx, line in enumerate(lines):
        s = line.strip().strip("│┃║").strip()
        if any(s.startswith(h) for h in _CLAUDE_APPROVAL_HEADERS):
            head = idx
    if head is None:
        return None
    body_after_head: List[str] = []
    for raw in lines[head + 1:]:
        s = raw.strip()
        if (
            s.startswith(("╰", "└", "┗", "Esc to cancel", "esc to cancel"))
            or "Tab to amend" in s
            or (s and set(s) <= _CLAUDE_ALL_RULE_CHARS)
        ):
            break
        body_after_head.append(s.strip("│┃║").strip())
    opts = _numbered_options(body_after_head)
    for o in opts:
        low_lbl = o["label"].lower()
        if "tell claude what to do differently" in low_lbl and not low_lbl.startswith("no"):
            o["action"] = "deny"
        else:
            act = classify_approval_label(o["label"])
            if act == "persist":
                # M4: Claude Code's option 2 (`Yes, and don't ask again for: <cmd>`) is its session/local
                # allow choice (writes to `.claude/settings.local.json`), analogous to codex's `p`.
                o["action"] = "approve_session"
                o["persists"] = True
            else:
                o["action"] = act

    # N1: tolerate partially rendered / clipped option lists when the header is already visible
    # (e.g. a 6-line wrapped option 2 pushes `3. No` onto the footer row on a 36-row terminal, or
    # cursor redraws overwrite `1. Yes` leaving only `2. No`).
    if not opts:
        opts = [
            {"key": "1", "label": "Yes", "action": "approve_once"},
            {"key": "2", "label": "No", "action": "deny"},
        ]
    else:
        if not any(o["action"] == "approve_once" for o in opts):
            has_key_3 = any(str(o.get("key")) == "3" for o in opts)
            has_key_2 = any(str(o.get("key")) == "2" for o in opts)
            if has_key_3 and not has_key_2:
                opts.insert(0, {
                    "key": "2",
                    "label": "Yes, and don't ask again",
                    "action": "approve_session",
                    "persists": True,
                })
            opts.insert(0, {"key": "1", "label": "Yes", "action": "approve_once"})
        if not any(o["action"] in ("deny", "deny_feedback") for o in opts):
            numeric_keys = [int(o["key"]) for o in opts if str(o.get("key", "")).isdigit()]
            next_key = str((max(numeric_keys) if numeric_keys else len(opts)) + 1)
            opts.append({"key": next_key, "label": "No", "action": "deny"})

    header = lines[head].strip().strip("│┃║").strip()
    box_top: Optional[int] = None
    for idx in range(head - 1, -1, -1):
        s = lines[idx].strip()
        if s and set(s) <= _CLAUDE_SOLID_RULE_CHARS:
            box_top = idx
            break

    region = lines[(box_top + 1 if box_top is not None else max(0, head - 22)):head]
    sections: List[List[str]] = [[]]
    for raw in region:
        s_raw = raw.strip()
        if s_raw and set(s_raw) <= _CLAUDE_DASHED_RULE_CHARS:
            sections.append([])
            continue
        s = s_raw.strip("│┃║").strip()
        if not s or s.startswith("/permissions to update rules"):
            continue
        if s.lower() in _CLAUDE_TOOL_TITLES:
            continue
        sections[-1].append(s)

    command = ""
    reason = ""
    non_empty_secs = [sec for sec in sections if sec]
    if len(sections) >= 3:
        cmd_sec = sections[1]
        reason_sec = [ln for sec in sections[2:] for ln in sec]
        command = " ".join(cmd_sec).strip()
        reason = " ".join(reason_sec).strip()
    elif len(sections) == 2 and sections[1]:
        cmd_parts: List[str] = []
        reason_parts: List[str] = []
        in_reason = False
        for ln in sections[1]:
            if not in_reason and ln.lower().startswith(_CLAUDE_REASON_PREFIXES):
                in_reason = True
            if in_reason:
                reason_parts.append(ln)
            else:
                cmd_parts.append(ln)
        command = " ".join(cmd_parts).strip()
        reason = " ".join(reason_parts).strip()
    elif non_empty_secs:
        command = " ".join(non_empty_secs[-1]).strip()

    if not command:
        command = _dollar_command(lines[:head])
    return {
        "header": header,
        "reason": reason,
        "command": command,
        "options": opts,
    }


def _collapsed(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _one_line(text: Optional[str]) -> str:
    """Feedback typed into an approval prompt: a line break there would press Enter too early."""
    return re.sub(r"[\r\n]+", " ", str(text or "")).strip()


class HubTerminalScreen(pyte.HistoryScreen):
    """K4 (fix 9): the hub's screen model of a PTY.

    kimi sends a private status query (CSI ? 996 n). pyte dispatches every private CSI with
    private=True, but its report_device_status takes no such flag, so the query raised a TypeError
    in the middle of the PTY chunk. A private DSR asks the terminal something and draws nothing, so
    the whole CSI ? … n family is ignored here.
    """

    def report_device_status(self, mode: int, **kwargs: Any) -> None:
        if kwargs.get("private"):
            return
        super().report_device_status(mode)

    @property
    def display(self) -> List[str]:
        """Safe display renderer that tolerates orphaned wide-char stubs (Char.data == '').

        When a TUI cursor-positions over the first cell of a 2-column CJK character without writing
        the second cell, pyte leaves Char(data='') in the second cell while is_wide_char is False.
        Stock pyte.Screen.display indexes char[0] unconditionally and raises IndexError.
        """
        wcwidth = pyte.screens.wcwidth

        def render(line):
            is_wide_char = False
            for x in range(self.columns):
                if is_wide_char:
                    is_wide_char = False
                    continue
                char = line[x].data
                if not char:
                    yield " "
                    continue
                is_wide_char = wcwidth(char[0]) == 2
                yield char

        return ["".join(render(self.buffer[y])) for y in range(self.lines)]


class HubTerminalStream(pyte.Stream):
    """K4 (fix 9): one escape sequence pyte cannot handle costs only itself, not the rest of the chunk.

    pyte resets its parser when a handler raises but re-raises, and Stream.feed then abandons every
    byte after it (the raw scrollback kept them, the screen model did not). Here the failed
    character is dropped and the next one is parsed by the fresh parser.
    """

    def _send_to_parser(self, data: str) -> Optional[bool]:
        try:
            return super()._send_to_parser(data)
        except Exception:
            # pyte's own handler already re-initialised the parser; report its plain-text state.
            return self._taking_plain_text


# OSC 10/11/12/4 color queries emitted by TUI apps (e.g. Codex CLI) at startup, and the OSC
# replies xterm.js generates when it parses them. Forwarding late OSC replies into a ConPTY
# session triggers TUI "esc to interrupt" via the leading ESC and types "]10;rgb:..." into the
# prompt composer.
OSC_COLOR_QUERY_RE = re.compile(r"\x1b\](?:4;\d+|1[0-9]);\?(?:\x07|\x1b\\)")
OSC_TERMINAL_REPLY_RE = re.compile(r"\x1b\](?:4;\d+|1[0-9]|52|1[01]\d);[^\x07\x1b]*(?:\x07|\x1b\\)")
NPM_CMD_SHIM_JS_RE = re.compile(r'"%(?:~dp0|dp0%)\\([^"]+?\.m?c?js)"\s+%\*', re.IGNORECASE)


def strip_osc_terminal_replies(text: str) -> str:
    """Removes automatic xterm OSC color/selection replies from browser-forwarded input."""
    if not text or "\x1b]" not in text:
        return text
    return OSC_TERMINAL_REPLY_RE.sub("", text)


def unwrap_windows_cmd_shim(
    command: str,
    args: List[str],
    env: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Unwraps Windows npm .cmd/.bat shims (e.g. codex.cmd) into direct [node.exe, script.js, *args].

    When winpty spawns a .cmd/.bat file, Windows wraps it in `cmd.exe /c ...`, whose `%*` batch
    expansion truncates multiline arguments at the first `\\n` (losing everything after line 1 of a
    multiline task and its passive Hub session marker). Launching `node.exe` directly
    bypasses `cmd.exe /c` so `CreateProcessW` preserves newlines verbatim. For non-npm .cmd/.bat
    scripts that cannot be unwrapped, newlines in arguments are collapsed to spaces so `cmd.exe`
    never drops trailing lines.
    """
    cmd_str = str(command or "").strip()
    arg_list = [str(a) for a in (args or [])]
    if os.name != "nt" or not cmd_str:
        return [cmd_str] + arg_list

    path_env = (env or os.environ).get("PATH", os.defpath)
    resolved = cmd_str
    if not os.path.isabs(resolved) or not os.path.exists(resolved):
        found = shutil.which(resolved, path=path_env)
        if found:
            resolved = found

    suffix = Path(resolved).suffix.lower()
    if suffix not in (".cmd", ".bat"):
        return [cmd_str] + arg_list

    cmd_path = Path(resolved)
    if cmd_path.is_file():
        try:
            content = cmd_path.read_text(encoding="utf-8", errors="replace")
            m = NPM_CMD_SHIM_JS_RE.search(content)
            if m:
                rel_js = m.group(1).replace("\\", os.sep)
                script_path = (cmd_path.parent / rel_js).resolve()
                local_node = cmd_path.parent / "node.exe"
                node_exe = (
                    str(local_node)
                    if local_node.is_file()
                    else (shutil.which("node.exe", path=path_env) or shutil.which("node", path=path_env))
                )
                if node_exe and script_path.is_file():
                    return [str(node_exe), str(script_path)] + arg_list
        except OSError:
            pass

    safe_args = [re.sub(r"[\r\n]+", " ", a) for a in arg_list]
    return [cmd_str] + safe_args


class TuiWriterClaim:
    """K6 (fix 9c): one writer's place in a session's TuiWriterQueue."""

    def __init__(self, queue: "TuiWriterQueue", ahead: Optional["TuiWriterClaim"], mode: str):
        self.queue = queue
        self.ahead = ahead
        self.behind: Optional["TuiWriterClaim"] = None
        self.mode = mode
        self.released = False
        self.done = asyncio.Event()
        if ahead is not None:
            ahead.behind = self

    @property
    def first(self) -> bool:
        """Nobody ahead of this claim is still typing, so it may type now."""
        return self.ahead is None or self.ahead.done.is_set()

    async def wait_turn(self) -> None:
        while not self.first:
            await self.ahead.done.wait()

    def release(self) -> None:
        """Ends this writer's turn. A claim released before its turn came (its wait was cancelled)
        hands the turn on only once the writers ahead of it are done, so the order always holds."""
        self.released = True
        claim: Optional[TuiWriterClaim] = self
        while claim is not None and claim.released and claim.first and not claim.done.is_set():
            claim.done.set()
            claim.ahead = None
            claim.queue._open.discard(claim)
            claim = claim.behind


class TuiWriterQueue:
    """K6 (fix 9c): one writer at a time types into a session's TUI, in the order they arrived.

    The startup text and every live message with an Enter go through it, so two back-to-back
    messages (or a message sent while the startup text is still waiting for readiness) can never
    interleave their keystrokes. claim() is synchronous, so deliver_parent_message (not a coroutine)
    takes its place in line at once and, when nobody is ahead, may type right away.
    """

    def __init__(self) -> None:
        self._tail: Optional[TuiWriterClaim] = None
        self._open: Set[TuiWriterClaim] = set()

    def claim(self, mode: str) -> TuiWriterClaim:
        ahead = self._tail if self._tail is not None and not self._tail.done.is_set() else None
        claim = TuiWriterClaim(self, ahead, mode)
        self._tail = claim
        self._open.add(claim)
        return claim

    def busy(self, mode: Optional[str] = None) -> bool:
        """A writer (of this mode) holds or waits for the TUI."""
        return any(mode is None or c.mode == mode for c in self._open)


# Lines of a DSH worker question that instruct the parent agent how to reply; the console shows
# the options as buttons, so its copy of the prompt leaves them out.
DSH_PARENT_INSTRUCTION_PREFIXES = (
    "[approval request from the worker", "[question from the worker", "Options:",
    "Reply with exactly one option label",
)


def _dsh_display_prompt(text: str) -> str:
    lines = [line for line in str(text or "").splitlines()
             if not line.strip().startswith(DSH_PARENT_INSTRUCTION_PREFIXES)]
    return "\n".join(lines).strip()


DSH_RECORD_SEQ_KEY = "dsh_record_seq"
DSH_RECORD_OFFSET_KEY = "dsh_record_offset"
# DSH creates the record a moment after the job starts (about 1.7 s measured); the watcher holds
# the job log back this long while it looks, so no tool is recorded from both sources.
DSH_RECORD_LOCATE_WAIT_SEC = 20.0
# After DSH reports the job finished, its last frames can still be on their way to disk.
DSH_RECORD_SETTLE_INTERVAL_SEC = 0.5
DSH_RECORD_SETTLE_MAX_PASSES = 6


class _DshRecordCursor:
    """Read position of one DSH watcher in the session record (see _drain_dsh_session_record)."""

    def __init__(self, reader: DshSessionRecordReader, *, after_seq: int, since_ms: Optional[float]):
        self.reader = reader
        self.after_seq = after_seq
        self.since_ms = since_ms
        self.active = True
        self.tool_names: Dict[str, str] = {}  # call id -> tool name, for results read later


def _keep_dsh_record_seq(old_ref: Optional[Dict[str, Any]], new_ref: Dict[str, Any]) -> Dict[str, Any]:
    """A rebuilt DSH conversation reference keeps how far the session record was read."""
    for key in (DSH_RECORD_SEQ_KEY, DSH_RECORD_OFFSET_KEY):
        value = old_ref.get(key) if isinstance(old_ref, dict) else None
        if value is not None:
            new_ref[key] = value
    return new_ref


class AgentSession:
    """Represents a single managed Sub-Agent session (TUI via ConPTY, Web GUI, or Win32 Desktop GUI)."""

    def __init__(
        self,
        session_id: str,
        profile_id: str,
        profile_name: str,
        kind: str,
        title: str,
        cwd: str,
        command: str,
        args: List[str],
        gui_url: Optional[str] = None,
        hwnd: Optional[int] = None,
        cols: int = 120,
        rows: int = 36,
        # A7: the callback carries the session object, so an exit of an object a resume already
        # replaced can never be mistaken for the live session's own exit.
        on_exit_cb: Optional[Callable[["AgentSession", int], None]] = None,
        on_tui_tool_cb: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.session_id = session_id
        # Parent-facing protocol identity.  It is opaque to callers except as a correlation value.
        self.turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        self.wait_handle: Optional[str] = None
        self.profile_id = profile_id
        self.profile_name = profile_name
        self.kind = kind  # "tui" | "web_gui" | "desktop_gui"
        self.title = title
        self.cwd = cwd
        self.command = command
        self.args = args
        self.gui_url = gui_url
        self.hwnd = hwnd
        self.tucked_offscreen = False
        self.original_rect: Optional[tuple] = None
        self.on_exit_cb = on_exit_cb
        self.on_tui_tool_cb = on_tui_tool_cb

        self.cols = cols
        self.rows = rows
        self.created_at = time.time()
        # Q2: the moment the child process really starts; transcript ownership is measured from here,
        # not from the hub's bookkeeping time of create_session.
        self.spawned_at: Optional[float] = None
        self.last_output_at = time.time()
        self.ended_at: Optional[float] = None
        self.status = "starting"  # starting | running | waiting_parent | completed | exited | terminated
        self.exit_code: Optional[int] = None
        # V-5: set by terminate(); the PTY reader must not report a second lifecycle event for it.
        self.terminated_by_user: bool = False
        # B9: the initial task, never overwritten by follow-up messages or by status text.
        self.last_task: str = ""

        # Rich Telemetry & Lifecycle State
        self.step_count: int = 0
        self.current_activity: str = "Initializing session..."
        self.final_response: Optional[str] = None
        self.final_response_source: Optional[str] = None
        # D2: True while the stored answer belongs to the turn that ended (set by begin_new_turn on
        # a resume and on a follow-up). The next turn's first source overwrites it whatever its rank.
        self.final_response_from_previous_turn: bool = False
        self.files_changed: List[str] = []
        self.pending_question_id: Optional[str] = None
        self.pending_interaction: Optional[Dict[str, Any]] = None
        self._responded_request_ids: Dict[str, Dict[str, Any]] = {}
        self.engine: str = "generic"
        self.transcript_path: Optional[Path] = None
        # K3 (fix 9): user messages the transcript watcher has read from this session's transcript
        # (a counter and the newest text), so a typed submit can be proved by the transcript itself.
        self.transcript_user_seq: int = 0
        self.transcript_user_text: str = ""
        self.effective_model: Optional[str] = None
        self.effective_thinking_level: Optional[str] = None

        # Archive spec §3: everything the final record needs in order to view and revive this session.
        self.end_reason: Optional[str] = None
        self.archived_at: Optional[float] = None
        self.last_screen: str = ""
        self.profile_snapshot: Dict[str, Any] = {}
        # Round 2: the Chinese notes of the launch this object ran (resolve_launch / resume).
        self.launch_notes: List[str] = []
        self.conversation_ref: Dict[str, Any] = {}
        self.resume_count: int = 0
        self.last_resumed_at: Optional[float] = None
        self.resumable: Optional[bool] = None
        self.resume_block_reason: Optional[str] = None
        # Resume (§5.3): attach the watcher to the known conversation and tail only what is new.
        self.resume_attach: bool = False
        self.attach_offset: Optional[int] = None
        self.completion_reported: bool = False
        # A10: the real outcome of typing a startup/resume text into the TUI (never guessed).
        # K8 (fix 9d): every record set here is also kept in recent_injections (see the property).
        self._recent_injections: List[Dict[str, Any]] = []
        self.last_injection = None
        # K6 (fix 9c): the startup text and the live messages type into the TUI one at a time.
        self.tui_writer = TuiWriterQueue()
        # A failed/unchecked draft must be explicitly cleared before another message is typed.
        self._tui_draft_pending: Optional[Dict[str, Any]] = None
        self._scrollback_written_at: float = 0.0
        self._scrollback_rev_written: int = -1
        self._seen_tui_tool_lines: Set[str] = set()
        self._seen_tui_prompt_signatures: Set[str] = set()
        # D7: the last screen-detected interaction no machine record has adopted yet, and the
        # moment until which the screen detector leaves a just-answered prompt alone.
        self._unadopted_screen_interaction: Optional[Dict[str, Any]] = None
        self._prompt_quiet_until: float = 0.0
        self._inflight_transcript_tool: Optional[Dict[str, Any]] = None
        self._claude_hook_offset: int = 0
        self._kimi_wire_interaction_offset: int = 0

        # Headless VT100 terminal screen buffer WITH 3,000-line scrollback history!
        # K4 (fix 9): the hub's subclasses keep one bad escape sequence from losing the rest of a chunk.
        self.screen = HubTerminalScreen(cols, rows, history=3000)
        self.stream = HubTerminalStream(self.screen)

        # Raw ANSI history buffer for Web Console xterm.js replay
        self.raw_history: List[str] = []
        self.raw_history_bytes = 0
        # Archive spec §3: bumped on every append, so the 60 s scrollback write can tell "changed"
        # even when the capped buffer's byte count happens to be the same.
        self.raw_history_rev = 0
        self.max_history_bytes = RESUME_SCROLLBACK_MAX_BYTES

        # Connected WebSocket queues for live browser xterm.js streaming
        self.ws_queues: Set[asyncio.Queue] = set()
        # W7: one closer per browser socket, so removing the session from the live list also ends
        # the sockets with a normal close code instead of leaving them open and silent forever.
        self.ws_close_hooks: Dict[asyncio.Queue, Callable[[], Any]] = {}
        # W8: a resumed omp session clears saved lines with ESC[3J; a split sequence must not slip
        # through between two PTY chunks.
        self._esc3j_carry: str = ""
        self.pty: Optional[PtyProcess] = None
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # DeepSeek Harness (dsh-crew in-host) native session bridge fields
        self.dsh_job_id: Optional[str] = None
        self.dsh_session_id: Optional[str] = None
        self.dsh_token: Optional[str] = None
        self.dsh_spec: Dict[str, Any] = {}
        self.sandbox_preparation: Optional[Dict[str, Any]] = None
        self._virtual_line_buf: str = ""
        self.on_virtual_line_cb: Optional[Callable[[str, str], Any]] = None

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        # Measure completed/ended inactivity, never terminal output silence or status polling.
        # Repeated completion observations must not postpone cleanup. A new turn resets it.
        if value in INACTIVE_SESSION_STATUSES:
            if getattr(self, "_status", None) not in INACTIVE_SESSION_STATUSES:
                self._inactive_since = time.monotonic()
        else:
            self._inactive_since = None
        self._status = value

    def start_virtual(
        self,
        loop: asyncio.AbstractEventLoop,
        on_virtual_line_cb: Optional[Callable[[str, str], Any]] = None,
    ):
        self._loop = loop
        self.on_virtual_line_cb = on_virtual_line_cb
        self.status = "running"
        self.current_activity = "Connected to DeepSeek Harness Host"
        self.last_output_at = time.time()

    def emit_virtual_ansi(self, data: str):
        self._on_pty_output(data)

    def set_final_response(self, text: Optional[str], source: str):
        text = (text or "").strip()
        if not text:
            return
        new_prio = FINAL_SOURCE_PRIORITY.get(source, 1)
        if self.final_response_from_previous_turn:
            # D2: the previous turn's answer is kept only until this turn writes one. The new turn's
            # first source wins whatever its rank, so an old report_complete_mcp can no longer
            # outrank the turn that is running now; the flag clears with that first write.
            self.final_response = text
            self.final_response_source = source
            self.final_response_from_previous_turn = False
        else:
            # Within one turn the source priority stays exactly as the K4 table defines it.
            cur_prio = FINAL_SOURCE_PRIORITY.get(self.final_response_source, 0)
            if new_prio >= cur_prio:
                self.final_response = text
                self.final_response_source = source
        # Spec §1.4: "reported completion" is what separates a clean exit from an abnormal one.
        if str(source) in ("report_complete_mcp", "codex_task_complete", "dsh_hub_completion"):
            self.completion_reported = True

    def start_pty(
        self,
        loop: asyncio.AbstractEventLoop,
        env_overrides: Optional[Dict[str, str]] = None,
        ws_prefix: Optional[str] = None,
    ):
        self._loop = loop
        env = os.environ.copy()
        # Prevent broken host PYTHONHOME from poisoning child agent shells (e.g. `py -3` inside Codex).
        env.pop("PYTHONHOME", None)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        if env_overrides:
            env.update(env_overrides)

        if PtyProcess is REAL_PTY_PROCESS:
            cmd_list = unwrap_windows_cmd_shim(self.command, list(self.args), env=env)
        else:
            cmd_list = [self.command] + list(self.args)
        # Q2: immediately before the process is spawned, so a transcript created at (or after) this
        # instant can be this session's and nothing older can.
        self.spawned_at = time.time()
        self.pty = PtyProcess.spawn(
            cmd_list,
            cwd=self.cwd,
            env=env,
            dimensions=(self.rows, self.cols),
        )
        # A1: a resumed object may inherit sockets attached to the object it replaced. The separator
        # goes out between the spawn and the reader thread, so it can never land after the new
        # process's first output, and a failed spawn (which raised above) pushed nothing.
        if ws_prefix:
            self.push_ws_frame(ws_prefix)
        self.status = "running"
        self.current_activity = "Interactive session running"
        self.last_output_at = time.time()

        reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        reader_thread.start()

    def _read_loop(self):
        while self.pty and self.pty.isalive():
            try:
                data = self.pty.read(4096)
                if not data:
                    time.sleep(0.02)
                    continue
                self._on_pty_output(data)
            except EOFError:
                break
            except Exception:
                break

        try:
            exit_code = self.pty.exitstatus if self.pty else 0
        except Exception:
            exit_code = 0
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._finish_read, exit_code)

    def _finish_read(self, exit_code):
        self.ended_at = time.time()
        self.exit_code = exit_code
        # Snapshot sanitized terminal output into final_response only if no higher-priority response was recorded
        try:
            scrollback = self.get_full_scrollback_text(max_lines=80)
            clean_lines = sanitize_tui_screen_text(scrollback, max_lines=10, engine=self.engine)
            if clean_lines and not self.final_response_from_previous_turn:
                self.set_final_response("\n".join(clean_lines), "terminal_turn_snapshot")
        except Exception:
            pass

        # Archive spec §1.4/§2: the process's end gets a named reason. A user termination already
        # decided its status in terminate(); the reader thread must not overwrite it.
        if self.terminated_by_user:
            self.end_reason = "terminated"
        else:
            self.end_reason = self.classify_end_reason(self.exit_code)
            self.status = self.end_reason
        self.current_activity = f"Session {self.status} (exit_code={self.exit_code})"

        if self.on_exit_cb and self._loop and not self.terminated_by_user:
            # V-5: a user termination already has its one lifecycle event; do not queue a second one.
            # A7: carry this object, so the manager can tell it apart from a later resume's object.
            self.on_exit_cb(self, self.exit_code or 0)

    def _on_pty_output(self, data: str):
        detected_tools = []
        # W8: omp clears the screen AND its saved lines when it starts (ESC[2J + ESC[3J). On a
        # resumed session the saved lines are the archived output and the resume banner, so the
        # erase-saved-lines half never reaches the history or a client.
        if self.resume_count:
            data = self._strip_saved_lines_erase(data)
        if "\x1b]" in data:
            data = OSC_COLOR_QUERY_RE.sub("", data)
        with self._lock:
            self.last_output_at = time.time()
            try:
                self.stream.feed(data)
            except Exception:
                pass

            # Heuristic detection of TUI tool lines only for generic terminals without native transcript watchers
            if (self.engine or "").strip().lower() not in ("claude", "codex", "omp", "kimi", "agy", "dsh"):
                try:
                    for line in self.screen.display:
                        clean = line.strip()
                        if not clean or clean in self._seen_tui_tool_lines:
                            continue
                        m = TUI_TOOL_PATTERN.match(clean)
                        if m:
                            self._seen_tui_tool_lines.add(clean)
                            t_name = m.group(1)
                            t_arg = (m.group(2) or m.group(3) or "").strip()
                            detected_tools.append((t_name, t_arg))
                except Exception:
                    pass

            self.raw_history.append(data)
            # W9: count UTF-8 bytes (the cap is 512 KB, not 512k characters) and trim the JOINED
            # history at a line break instead of dropping whole chunks; the slack keeps the
            # 512 KB rebuild off every PTY read once the buffer is full.
            self.raw_history_bytes += len(data.encode("utf-8", "replace"))
            self.raw_history_rev += 1
            if self.raw_history_bytes > self.max_history_bytes + HISTORY_TRIM_SLACK_BYTES:
                joined = "".join(self.raw_history)
                trimmed = trim_history_text(joined, self.max_history_bytes)
                if trimmed != joined:
                    self.raw_history = [trimmed]
                    self.raw_history_bytes = len(trimmed.encode("utf-8", "replace"))
            # Q11: the fan-out targets belong to the same critical section as the history append.
            # A WebSocket registered afterwards gets this chunk inside its replay snapshot, never
            # twice (append + live frame) and never zero times (the old snapshot/register gap).
            ws_targets = list(self.ws_queues)

        if detected_tools and self.on_tui_tool_cb and self._loop:
            for t_name, t_arg in detected_tools:
                self._loop.call_soon_threadsafe(self.on_tui_tool_cb, self.session_id, t_name, t_arg)

        if self._loop and ws_targets:
            for q in ws_targets:
                self._loop.call_soon_threadsafe(self._push_ws_safe, q, data)

    @staticmethod
    def _push_ws_safe(q: asyncio.Queue, data: str):
        try:
            q.put_nowait(data)
        except Exception:
            pass

    def _strip_saved_lines_erase(self, data: str) -> str:
        """W8: removes ESC[3J (erase saved lines) from a resumed session's output.

        The sequence can be split across two PTY reads, so up to three trailing bytes are carried
        into the next call. ESC[2J (erase display) is deliberately left alone: it only clears the
        viewport, and the bundled xterm.js 5.5.0 has no scrollOnEraseInDisplay option.
        """
        if not data:
            return data
        buf = self._esc3j_carry + data
        self._esc3j_carry = ""
        out = buf.replace("\x1b[3J", "")
        for size in (3, 2, 1):
            suffix = "\x1b[3J"[:size]
            if out.endswith(suffix):
                self._esc3j_carry = suffix
                out = out[:-size]
                break
        return out

    def register_ws_closer(self, q: asyncio.Queue, closer: Callable[[], Any]) -> None:
        """W7: the server registers how to close the browser socket that owns this queue."""
        with self._lock:
            self.ws_close_hooks[q] = closer

    def close_ws_frontends(self) -> None:
        """W7: normal-close every browser socket still attached to this session (normal code 1000)."""
        with self._lock:
            hooks = list(self.ws_close_hooks.values())
            self.ws_close_hooks.clear()
        for closer in hooks:
            try:
                closer()
            except Exception:
                pass

    def attach_ws_queue(self) -> tuple:
        """Q11: snapshot the replay and register the live queue in one atomic step.

        The PTY reader appends to `raw_history` and snapshots its fan-out targets under the same
        lock, so every output chunk is either inside the replay this socket receives or in its
        queue — never both, never neither.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            replay = "".join(self.raw_history)
            if "\x1b]" in replay:
                replay = OSC_COLOR_QUERY_RE.sub("", replay)
            self.ws_queues.add(q)
        return replay, q

    def detach_ws_queue(self, q: asyncio.Queue) -> None:
        with self._lock:
            self.ws_queues.discard(q)
            self.ws_close_hooks.pop(q, None)

    def adopt_ws_frontends(self, previous: "AgentSession") -> None:
        """A1: an open browser socket keeps streaming after a resume replaces the session object.

        The queue set is handed over by identity, so the server's disconnect path — which still holds
        the old object — detaches from the very set the new session fans out to. The resume separator
        is pushed separately (push_ws_frame) because the seed in raw_history is replay-only.
        """
        self.ws_queues = previous.ws_queues
        # W7: the closers travel with the sockets, or a terminate after the resume could no longer
        # close them.
        self.ws_close_hooks = previous.ws_close_hooks

    def push_ws_frame(self, frame: str) -> None:
        """A1: writes one frame to every browser socket already attached to this session id."""
        for q in list(self.ws_queues):
            self._push_ws_safe(q, frame)

    def write_input(self, text: str):
        if self.pty and self.pty.isalive():
            self.pty.write(text)
            return True
        if self.on_virtual_line_cb is not None:
            for ch in text:
                if ch in ("\r", "\n"):
                    self.emit_virtual_ansi("\r\n")
                    line = self._virtual_line_buf.strip()
                    self._virtual_line_buf = ""
                    if line:
                        self.on_virtual_line_cb(self.session_id, line)
                elif ch in ("\x7f", "\b"):
                    if self._virtual_line_buf:
                        self._virtual_line_buf = self._virtual_line_buf[:-1]
                        self.emit_virtual_ansi("\b \b")
                else:
                    self._virtual_line_buf += ch
                    self.emit_virtual_ansi(ch)
            return True
        return False

    def resize(self, cols: int, rows: int):
        with self._lock:
            self.cols = max(20, int(cols))
            self.rows = max(5, int(rows))
            try:
                self.screen.resize(self.rows, self.cols)
            except Exception:
                pass
            if self.pty and self.pty.isalive():
                try:
                    self.pty.setwinsize(self.rows, self.cols)
                except Exception:
                    pass

    def get_clean_screen_text(self, max_lines: int = 80) -> str:
        with self._lock:
            try:
                lines = [line.rstrip() for line in self.screen.display]
            except Exception:
                lines = []
            while lines and not lines[-1]:
                lines.pop()
            if max_lines and len(lines) > max_lines:
                lines = lines[-max_lines:]
            return "\n".join(lines)

    def note_transcript_user_message(self, text: str) -> None:
        """K3 (fix 9): the transcript watcher read a user message; a typed submit may cite it."""
        self.transcript_user_seq += 1
        self.transcript_user_text = str(text or "")

    def get_full_scrollback_text(self, max_lines: int = 300) -> str:
        """Returns combined scrollback history + active screen lines from pyte.HistoryScreen."""
        with self._lock:
            try:
                curr_lines = [line.rstrip() for line in self.screen.display]
            except Exception:
                curr_lines = []
            # Render only the requested tail. Rebuilding all 10,000 history rows for
            # every accepted tool record used to starve HTTP and terminal heartbeats.
            all_lines = curr_lines
            while all_lines and not all_lines[-1]:
                all_lines.pop()
            newest_history = []
            try:
                for row in reversed(self.screen.history.top):
                    if max_lines and len(newest_history) + len(all_lines) >= max_lines:
                        break
                    text = "".join(row[col].data if col in row else " " for col in range(self.cols)).rstrip()
                    if text or all_lines or newest_history:
                        newest_history.append(text)
            except Exception:
                pass
            all_lines = list(reversed(newest_history)) + all_lines
            if max_lines:
                all_lines = all_lines[-max_lines:]
            return "\n".join(all_lines)

    def classify_end_reason(self, exit_code: Optional[int]) -> str:
        """Spec §1.4/§2: name an abnormal end instead of calling everything "exited".

        A non-zero code is always a crash. Code 0 after the agent reported completion is a clean exit;
        a plain shell's 0 is a clean quit; an agent TUI that dies with 0 before ever reporting
        completion is the "exit before completion" case the spec calls 异常退出.
        """
        if self.terminated_by_user:
            return "terminated"
        if exit_code is not None and int(exit_code) != 0:
            return "crashed"
        if self.completion_reported:
            return "exited"
        if (self.engine or "").strip().lower() in ("pwsh", "generic") or self.kind == "desktop_gui":
            return "exited"
        return "crashed"

    def is_process_alive(self) -> bool:
        """B9: "live" means the process is alive (PTY alive; dsh virtual sessions by status), never the status alone."""
        if self.pty is not None:
            try:
                return bool(self.pty.isalive())
            except Exception:
                return False
        if self.dsh_job_id or self.dsh_spec:
            return self.status in ("starting", "running", "waiting_parent")
        return self.status in ("running", "waiting_parent")

    def terminate(self) -> bool:
        """Closes the session. An ended session keeps its own final status (B9); only a live one becomes terminated.

        Returns True when this call really terminated a live process.
        """
        was_alive = self.is_process_alive()
        self.ended_at = time.time()
        try:
            scrollback = self.get_full_scrollback_text(max_lines=80)
            clean_lines = sanitize_tui_screen_text(scrollback, max_lines=10, engine=self.engine)
            if clean_lines and not self.final_response_from_previous_turn:
                self.set_final_response("\n".join(clean_lines), "terminal_turn_snapshot")
        except Exception:
            pass
        if was_alive:
            # R14: a live process always ends as terminated, even when telemetry had already marked it
            # completed. V-5: remember the cause so the PTY reader does not also emit an `exited` event.
            self.terminated_by_user = True
            self.status = "terminated"
            self.end_reason = "terminated"
            self.current_activity = "Terminated by parent/user"
        if self.pty and was_alive:
            pid = getattr(self.pty, "pid", None)
            if pid and isinstance(self.pty, REAL_PTY_PROCESS):
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=1.5,
                    )
                except Exception:
                    pass
            try:
                self.pty.terminate(force=True)
            except Exception:
                pass
        if self.dsh_job_id and was_alive:
            job_id = self.dsh_job_id
            threading.Thread(
                target=lambda: httpx.post(dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}/cancel"), timeout=3.0),
                daemon=True,
            ).start()
        if was_alive:
            return True
        # The process had already ended: keep its real final status and activity untouched.
        return False

    @property
    def last_injection(self) -> Optional[Dict[str, Any]]:
        """A10/K6: the latest submit record (startup text, live message), or None before any."""
        return self.__dict__.get("_last_injection")

    @last_injection.setter
    def last_injection(self, record: Optional[Dict[str, Any]]) -> None:
        """K8 (fix 9d): setting the latest record also keeps it in recent_injections."""
        self.__dict__["_last_injection"] = record
        if isinstance(record, dict) and record:
            self._remember_injection(record)

    @property
    def recent_injections(self) -> List[Dict[str, Any]]:
        """K8 (fix 9d): the last TUI_RECENT_INJECTIONS_MAX submit records, in typing order (newest last)."""
        return list(self.__dict__.get("_recent_injections") or [])

    def _remember_injection(self, record: Dict[str, Any]) -> None:
        """K8 (fix 9d): one entry per submit, keyed by its submit_id.

        A record whose submit_id is already kept replaces that entry in place (a live message's
        "submitting" record by its outcome), so one submit never shows twice. A new finished record
        goes in front of the still-pending ones: those wait in the writer queue behind it, so it
        was typed first (the startup text's outcome landing while a live message waits for it).
        A record without a submit_id gets a fresh one.
        """
        history = self.__dict__.setdefault("_recent_injections", [])
        submit_id = record.get("submit_id")
        if not submit_id:
            submit_id = record["submit_id"] = f"inj-{uuid.uuid4().hex[:8]}"
        for idx, kept in enumerate(history):
            if kept.get("submit_id") == submit_id:
                history[idx] = record
                return
        if record.get("pending"):
            history.append(record)
        else:
            first_pending = next((i for i, kept in enumerate(history) if kept.get("pending")), len(history))
            history.insert(first_pending, record)
        del history[:-TUI_RECENT_INJECTIONS_MAX]

    @property
    def high_risk_approval(self) -> Optional[bool]:
        """Round 2: what this launch runs with (see high_risk_approval_state)."""
        snap = self.profile_snapshot if isinstance(self.profile_snapshot, dict) else {}
        return high_risk_approval_state(
            self.engine or snap.get("engine"), snap.get(BYPASS_APPROVALS_KEY) is True
        )

    @property
    def launch_source(self) -> str:
        # Stored with the resolved launch so archive/resume keeps notification ownership.
        # Older sessions have no provenance; preserve their existing notification behavior.
        return str(self.profile_snapshot.get("launch_source") or "user")

    def to_dict(self) -> Dict[str, Any]:
        alive = self.is_process_alive()
        if not alive and self.pty is not None and self.status not in (
            "completed", "exited", "crashed", "terminated", "interrupted"
        ):
            self.status = "exited"
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "wait_handle": self.wait_handle,
            "launch_source": self.launch_source,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "engine": self.engine,
            "kind": self.kind,
            "title": self.title,
            "cwd": self.cwd,
            "gui_url": self.gui_url,
            "hwnd": self.hwnd,
            "tucked_offscreen": self.tucked_offscreen,
            "status": self.status,
            "process_alive": alive,
            "step_count": self.step_count,
            "current_activity": self.current_activity,
            "pending_question_id": self.pending_question_id,
            "pending_interaction": self.pending_interaction,
            "final_response": self.final_response,
            "final_response_source": self.final_response_source,
            # D2: true while the answer above still belongs to the turn before the current one.
            "from_previous_turn": self.final_response_from_previous_turn,
            "files_changed": self.files_changed,
            "pid": getattr(self.pty, "pid", None) if self.pty else None,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "last_output_at": self.last_output_at,
            "idle_seconds": round(time.time() - self.last_output_at, 1),
            "last_task": self.last_task,
            "cols": self.cols,
            "rows": self.rows,
            "dsh_job_id": self.dsh_job_id,
            "dsh_session_id": self.dsh_session_id,
            "sandbox_preparation": self.sandbox_preparation,
            "effective_model": self.effective_model,
            "effective_thinking_level": self.effective_thinking_level,
            # Round 2: true = high-risk approval on, false = the profile's bypass_approvals was on
            # for this launch, null = the engine has no hub approval (dsh, pwsh).
            "high_risk_approval": self.high_risk_approval,
            **resolve_session_approval_fields(self.profile_snapshot),
            "transcript_path": str(self.transcript_path) if self.transcript_path else "",
            "end_reason": self.end_reason,
            "archived_at": self.archived_at,
            "resume_count": self.resume_count,
            "last_resumed_at": self.last_resumed_at,
            "resumable": self.resumable,
            "resume_block_reason": self.resume_block_reason,
            # A10: "pending" until the TUI really took the text; the outcome is recorded here.
            "last_injection": self.last_injection,
            # K8 (fix 9d): the recent submit records, newest last (last_injection is one of them).
            "recent_injections": self.recent_injections,
        }


class Win32WindowHelper:
    """Manages native Windows GUI Agent windows so they can be viewed & controlled inside the Web Console without cluttering the desktop."""

    @staticmethod
    def _process_exe_name(pid: int) -> str:
        """S4: the window owner's executable file name, or "" when Windows refuses the query."""
        if not pid:
            return ""
        handle = None
        try:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return ""
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value)
        except Exception:
            return ""
        finally:
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass

    @staticmethod
    def list_top_windows() -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title or title in ("Program Manager", "Settings", "Windows Input Experience"):
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 100 or h < 80:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            results.append({
                "hwnd": int(hwnd),
                "title": title,
                "pid": int(pid.value),
                "process_name": Win32WindowHelper._process_exe_name(int(pid.value)),
                "x": rect.left,
                "y": rect.top,
                "width": w,
                "height": h,
            })
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return results

    @staticmethod
    def capture_window_jpeg_b64(hwnd: int) -> Optional[Dict[str, Any]]:
        if not user32.IsWindow(hwnd):
            return None
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 10 or height <= 10:
            return None

        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        old_bmp = gdi32.SelectObject(mem_dc, bmp)

        user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buf_len = width * height * 4
        buf = ctypes.create_string_buffer(buf_len)
        gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(bmi), 0)

        gdi32.SelectObject(mem_dc, old_bmp)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

        img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1).convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=78)
        b64 = base64.b64encode(out.getvalue()).decode("ascii")
        return {"width": width, "height": height, "jpeg_b64": b64}

    @staticmethod
    def tuck_window_offscreen(session: AgentSession, tuck: bool) -> bool:
        hwnd = session.hwnd
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if tuck and not session.tucked_offscreen:
            session.original_rect = (rect.left, rect.top, w, h)
            user32.MoveWindow(hwnd, -3200, 100, w, h, True)
            session.tucked_offscreen = True
        elif not tuck and session.tucked_offscreen:
            orig_x, orig_y = (session.original_rect[0], session.original_rect[1]) if session.original_rect else (120, 120)
            user32.MoveWindow(hwnd, orig_x, orig_y, w, h, True)
            session.tucked_offscreen = False
        return True

    @staticmethod
    def send_click(hwnd: int, x: int, y: int):
        if not user32.IsWindow(hwnd):
            return False
        lparam = (int(y) << 16) | (int(x) & 0xFFFF)
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
        return True

    @staticmethod
    def send_text(hwnd: int, text: str):
        if not user32.IsWindow(hwnd):
            return False
        for ch in text:
            if ch in ("\n", "\r"):
                user32.PostMessageW(hwnd, WM_KEYDOWN, 0x0D, 0)
                user32.PostMessageW(hwnd, WM_KEYUP, 0x0D, 0)
            else:
                user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 0)
        return True


class HubSessionManager:
    """Central manager for Sub-Agent profiles, ConPTY sessions, 4-Layer Telemetry, and SQLite Post-Lifecycle Persistence."""

    def __init__(self, profiles_path: Optional[Path] = None, base_dir: Optional[Path] = None,
                 data_dir: Optional[Path] = None):
        self.base_dir = (base_dir or Path(__file__).parent).resolve()
        data_override = (os.environ.get("HUB_DATA_DIR") or "").strip()
        self.data_dir = (Path(data_dir) if data_dir is not None else
                         Path(data_override) if data_override else self.base_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_path = (profiles_path or default_profiles_path(self.base_dir)).resolve()
        self.profiles_path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_configs_dir = self.data_dir / ".runtime_mcp_configs"
        self.runtime_configs_dir.mkdir(exist_ok=True)

        self.db = HubPersistence(self.data_dir / "hub_state.db")
        self.wait_inbox = WaitInbox(self.data_dir / "wait_inbox.db")
        self._loop = None
        self._background_tasks = set()
        self._shutdown_task = None
        self.summarizer_registry = TranscriptSummarizerRegistry()
        self.model_discovery = AgentModelDiscovery()
        self.profiles: Dict[str, Dict[str, Any]] = {}
        self.engines_catalog: Dict[str, Any] = {}
        self.engine_policies: Dict[str, Dict[str, Any]] = {}
        self.global_routing_instructions: str = ""
        self.subagent_idle_timeout_minutes = DEFAULT_SUBAGENT_IDLE_TIMEOUT_MINUTES
        self.sessions: Dict[str, AgentSession] = {}
        self.default_cols: int = 120
        self.default_rows: int = 36
        # V2: one transcript file belongs to exactly one hub session. Two watchers must never tail
        # the same conversation, and a file that existed before a session started is not its own.
        self._claimed_transcripts: Dict[str, str] = {}
        self._transcript_lock = threading.RLock()
        # Q3: marker-less conversations are assigned in launch order per engine, so "pick candidate
        # + claim" has to be one step per engine, not one step per watcher.
        self._engine_transcript_locks: Dict[str, asyncio.Lock] = {}
        # Q2: a marker scan of the first TRANSCRIPT_MARKER_SCAN_BYTES, cached until the file grows.
        self._marker_scan_cache: Dict[str, tuple] = {}
        # Q5: profile mutations rebuild the in-memory state from the discovery already in hand. This
        # snapshot is the only discovery they are allowed to use; they never run discovery themselves.
        self._cached_discovery: Dict[str, Any] = {"engines": {}}
        # V-5: ids whose one lifecycle event is the terminated event, even if the PTY reader
        # reported its own exit just before close_session removed the session from the live list.
        self._user_terminated: Set[str] = set()
        # Archive spec §5: only one resume per session at a time; the second call waits for the first
        # and then behaves as "already live".
        self._resume_locks: Dict[str, asyncio.Lock] = {}

        # Bidirectional Mailbox & Sequenced Event Bus
        self.global_seq: int = 0
        self.worker_events: List[Dict[str, Any]] = []
        self.parent_messages: Dict[str, List[Dict[str, Any]]] = {}
        # A11: worker→parent messages kept per session, outside the global 2000-event ring, so the
        # archive thread is complete even for a long-running session.
        self._worker_message_logs: Dict[str, List[Dict[str, Any]]] = {}
        self.pending_questions: Dict[str, Dict[str, Any]] = {}
        self._event_waiters: Set[asyncio.Event] = set()
        self._shutting_down: bool = False
        self.wait_inbox.set_state_provider(self._wait_state_snapshot)

        # B11: the constructor must never shell out to five CLIs. It loads the profile file at once
        # (using whatever discovery already has) and finishes discovery on a background thread, so
        # the very first /api/sessions poll answers immediately.
        self._discovery_ready = threading.Event()
        self.load_profiles(discovery=self.model_discovery.peek_cache() or {"engines": {}})
        # Archive spec §1.5/§5: rows a stopped hub left live become interrupted + archived, and ended
        # rows get their final record. D1: nothing is pruned here or anywhere else — records are kept
        # forever. Profiles are loaded first, because "still resumable" depends on the profile still
        # existing and being enabled.
        threading.Thread(target=self._initial_discovery, name="hub-initial-discovery", daemon=True).start()

    async def start(self):
        """Open storage off the loop before accepting requests or starting watchers."""
        if self._loop is not None:
            return
        self._loop = asyncio.get_running_loop()
        try:
            await self.db.start()
            self.global_seq = await self.db.get_max_seq()
            await self._normalize_archive_on_startup()
            await self.db.flush()
        except BaseException:
            await self.db.close(timeout=0.5)
            raise
        self._schedule(self._scrollback_flush_loop())
        self._schedule(self._subagent_cleanup_loop())

    async def _wait_state_snapshot(self, session_ids: List[str]) -> Dict[str, Any]:
        """Compact state provider used by WaitInbox after a restart or a quiet turn."""
        pending = []
        states = []
        for sid in session_ids:
            sess = self.sessions.get(sid)
            if sess is not None:
                if sess.pending_interaction:
                    pending.append(dict(sess.pending_interaction))
                states.append({
                    "session_id": sid,
                    "status": sess.status,
                    "turn_id": sess.turn_id,
                    "final_response": sess.final_response,
                    "completion_reported": bool(sess.completion_reported),
                })
            else:
                rec = await self.db.get_session_record(sid)
                states.append({
                    "session_id": sid,
                    "status": (rec or {}).get("status", "unknown"),
                    "turn_id": (rec or {}).get("turn_id"),
                    "final_response": (rec or {}).get("final_response"),
                    "completion_reported": bool((rec or {}).get("final_response")),
                })
        if pending:
            return {"state": "ask", "requests": pending, "result_id": pending[0].get("request_id")}
        if states and all(s["status"] in ("completed", "exited") and s.get("completion_reported") for s in states):
            results = [
                {"session_id": s["session_id"], "turn_id": s.get("turn_id"), "final_response": s.get("final_response") or ""}
                for s in states
            ]
            rid = "group:" + ":".join(f"{r['session_id']}:{r.get('turn_id') or 'current'}" for r in results)
            return {"state": "done", "result_id": rid, "final_response": results[0]["final_response"] if len(results) == 1 else None, "results": results}
        if any(s["status"] in ("crashed", "terminated", "interrupted") for s in states):
            return {"state": "failed", "error": "one or more sessions failed", "results": states}
        return {"state": "waiting"}

    def _subagent_cleanup_due(self, sess: AgentSession, now: float) -> bool:
        timeout = self.subagent_idle_timeout_minutes
        since = getattr(sess, "_inactive_since", None)
        return bool(
            timeout > 0 and sess.launch_source == "agent"
            and sess.status in INACTIVE_SESSION_STATUSES and since is not None
            and now - since >= timeout * 60
            and not sess.pending_interaction and not sess.pending_question_id
            and not sess.tui_writer.busy()
        )

    async def cleanup_idle_subagents(self) -> int:
        """Archive expired parent-owned sessions through the normal close path."""
        closed = 0
        for sid, sess in list(self.sessions.items()):
            if self._shutting_down:
                break
            if not self._subagent_cleanup_due(sess, time.monotonic()):
                continue
            # A resume may replace the session while we wait for its lock.
            async with self._resume_lock(sid):
                if (self.sessions.get(sid) is not sess or self._shutting_down
                        or not self._subagent_cleanup_due(sess, time.monotonic())):
                    continue
                try:
                    result = await self._close_session(sid, source="auto_cleanup")
                    closed += int(result["found"])
                except Exception:
                    logger.exception("Automatic cleanup failed for %s; will retry", sid)
        return closed

    async def _subagent_cleanup_loop(self):
        while not self._shutting_down:
            await asyncio.sleep(SUBAGENT_CLEANUP_INTERVAL_SEC)
            await self.cleanup_idle_subagents()

    def _schedule(self, coroutine):
        """Track callback/watch tasks so shutdown cannot race their final snapshots."""
        if self._shutting_down:
            coroutine.close()
            return None
        task = asyncio.create_task(self.db.run_internal(coroutine))
        self._background_tasks.add(task)
        def finished(done):
            coroutine.close()  # Also release a coroutine cancelled before run_internal started.
            self._background_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                logger.error("Hub background task failed", exc_info=(type(error), error, error.__traceback__))
        task.add_done_callback(finished)
        return task

    def _exit_callback(self, session, exit_code):
        self._schedule(self._on_session_exit(session, exit_code))

    def _tool_callback(self, session_id, tool_name, tool_arg):
        self._schedule(self._on_tui_tool_detected(session_id, tool_name, tool_arg))

    def _initial_discovery(self) -> None:
        try:
            self.load_profiles()
        except Exception:
            pass
        finally:
            self._discovery_ready.set()

    def wait_for_discovery(self, timeout: float = 60.0) -> bool:
        """Blocks until the startup discovery thread finished (used by scripts and tests)."""
        return self._discovery_ready.wait(timeout)

    def resize_session_from_client(
        self,
        session_id: str,
        cols: int,
        rows: int,
        propagate_background: bool = True,
    ) -> None:
        """Resizes the target session and tracks the console's viewport dimensions.

        When the console is in single-window mode (`propagate_background=True`), all live TUI
        sessions share the same single-pane viewport dimensions so background sessions stay sized
        to the main window instead of remaining at the initial 120x36 default.
        """
        c = max(20, int(cols or 120))
        r = max(5, int(rows or 36))
        self.default_cols = c
        self.default_rows = r
        target = self.sessions.get(session_id)
        if target is not None:
            target.resize(c, r)
        if propagate_background:
            for sid, sess in list(self.sessions.items()):
                if sid == session_id or sess is None:
                    continue
                if sess.kind == "tui" and (
                    sess.is_process_alive() or sess.status in ("starting", "running", "waiting_parent")
                ):
                    sess.resize(c, r)

    # ---- V2: transcript ownership ---------------------------------------------------------

    @staticmethod
    def _session_spawn_anchor(session: AgentSession) -> float:
        """Q2: when the child process really started (falls back to the session's bookkeeping time)."""
        return float(getattr(session, "spawned_at", None) or session.created_at)

    @staticmethod
    def _conversation_creation_time(path: Path, levels_up: int = 0) -> tuple:
        """(creation timestamp, is_a_real_birth_time) of the conversation a transcript belongs to.

        Windows has a real creation time: `st_birthtime` on Python 3.12+. `levels_up` walks from the
        transcript file to its conversation directory.
        """
        target = Path(path)
        for _ in range(max(0, int(levels_up))):
            target = target.parent
        try:
            st = target.stat()
        except OSError:
            return None, False
        birth = getattr(st, "st_birthtime", None)
        if birth is not None:
            return float(birth), True
        return float(st.st_ctime), False

    @classmethod
    def _conversation_created_after(cls, path: Path, session: AgentSession, levels_up: int = 0) -> bool:
        """True when the conversation this file belongs to was created after the process started.

        A transcript that already existed before the launch belongs to another conversation; attaching
        to it is the V2 bug (a second session reporting the first session's thoughts). Q2: with a real
        creation time there is no negative slack; the 1 s slack only covers the fallback where
        `st_ctime` is metadata-change time rather than creation time.
        """
        created, exact = cls._conversation_creation_time(path, levels_up)
        if created is None:
            return False
        anchor = cls._session_spawn_anchor(session)
        if exact:
            return created >= anchor
        return created >= (anchor - 1.0)

    @staticmethod
    def _transcript_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def _transcript_claimed_by_other(self, path: Path, session: AgentSession) -> bool:
        key = self._transcript_key(path)
        with self._transcript_lock:
            owner = self._claimed_transcripts.get(key)
        return bool(owner) and owner != session.session_id

    def _transcript_marker_ids(self, path: Path) -> Set[str]:
        """Q2: hub session ids named in the transcript's first chunk (the task carries session_id=<id>).

        Only the head of the file is read, because the session marker travels with the initial task; the
        answer is cached until the file grows, so a steady-state scan costs one stat.
        """
        try:
            size = Path(path).stat().st_size
        except OSError:
            return set()
        key = self._transcript_key(path)
        cached = self._marker_scan_cache.get(key)
        if cached and cached[0] == size:
            return set(cached[1])
        try:
            with open(path, "rb") as fh:
                chunk = fh.read(TRANSCRIPT_MARKER_SCAN_BYTES)
        except OSError:
            return set()
        ids = set(TRANSCRIPT_MARKER_RE.findall(chunk.decode("utf-8", "replace")))
        self._marker_scan_cache[key] = (size, frozenset(ids))
        return ids

    def _engine_transcript_lock(self, engine: str) -> asyncio.Lock:
        eng = (engine or "").strip().lower()
        with self._transcript_lock:
            lock = self._engine_transcript_locks.get(eng)
            if lock is None:
                lock = asyncio.Lock()
                self._engine_transcript_locks[eng] = lock
            return lock

    def _session_has_transcript(self, session: AgentSession) -> bool:
        if session.transcript_path:
            return True
        with self._transcript_lock:
            return session.session_id in self._claimed_transcripts.values()

    _CODEX_ROLLOUT_UUID_RE = re.compile(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.IGNORECASE
    )

    @classmethod
    def _codex_session_id_from_rollout(cls, path: Path) -> Optional[str]:
        """The uuid of a codex rollout: filename tail, else session_meta.payload.id (research report)."""
        match = cls._CODEX_ROLLOUT_UUID_RE.search(Path(path).name)
        if match:
            return match.group(1)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for _ in range(20):
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "session_meta":
                        value = (obj.get("payload") or {}).get("id")
                        return str(value) if value else None
        except OSError:
            return None
        return None

    @classmethod
    def _conversation_ref_for(cls, engine: str, path: Path) -> Dict[str, Any]:
        """The engine conversation reference the watcher just claimed (spec §4), or {} when unknown."""
        eng = (engine or "").strip().lower()
        p = Path(path)
        try:
            if eng == "omp":
                # The hub-private omp_sess_<session> dir and its <ts>_<uuid>.jsonl.
                return {"session_dir": str(p.parent), "session_file": str(p)}
            if eng == "kimi":
                # .../sessions/wd_*/session_<uuid>/agents/main/wire.jsonl
                session_dir = p.parent.parent.parent
                return {"session_id": session_dir.name, "session_dir": str(session_dir)}
            if eng == "agy":
                # .../brain/<uuid>/.system_generated/logs/transcript.jsonl
                brain_dir = p.parent.parent.parent
                return {"conversation_id": brain_dir.name, "brain_dir": str(brain_dir)}
            if eng == "codex":
                sid = cls._codex_session_id_from_rollout(p)
                return {"session_id": sid or "", "rollout_path": str(p)}
            if eng == "claude":
                return {"session_id": p.stem, "transcript_path": str(p)}
        except Exception:
            return {}
        return {}

    async def _claim_transcript(self, session: AgentSession, path: Path) -> bool:
        """Claims one transcript file for this session; False when another live session owns it."""
        key = self._transcript_key(path)
        with self._transcript_lock:
            owner = self._claimed_transcripts.get(key)
            if owner and owner != session.session_id:
                return False
            self._claimed_transcripts[key] = session.session_id
        session.transcript_path = Path(path)
        ref = self._conversation_ref_for(session.engine, Path(path))
        if ref:
            # Spec §3: filled as soon as the watcher claims a conversation.
            session.conversation_ref = ref
        await self.persist_session(session)
        return True

    def _release_transcripts(self, session_id: str) -> None:
        with self._transcript_lock:
            for key, owner in list(self._claimed_transcripts.items()):
                if owner == session_id:
                    del self._claimed_transcripts[key]

    async def _claim_conversation(self, session: AgentSession, candidates: List[Path], levels_up: int = 0) -> Optional[Path]:
        """Q2/Q3: marker first, then FIFO in launch order, all under one lock per engine.

        A transcript that names a hub session id belongs to that session, and no other session may
        claim it. Without a marker the earliest-spawned session of the engine that has no transcript
        yet takes the earliest-created unclaimed conversation; a later session waits its turn instead
        of grabbing the newest file.
        """
        paths = [Path(p) for p in candidates]
        if not paths:
            return None
        marked_for_me: List[Path] = []
        unmarked: List[Path] = []
        for path in paths:
            ids = self._transcript_marker_ids(path)
            if session.session_id in ids:
                marked_for_me.append(path)
            elif ids:
                # It names another hub session: never claim it, whatever its timestamps say.
                continue
            else:
                unmarked.append(path)

        if marked_for_me:
            for path in sorted(
                marked_for_me,
                key=lambda p: ((self._conversation_creation_time(p, levels_up)[0] or 0.0), str(p)),
            ):
                if self._transcript_claimed_by_other(path, session):
                    continue
                if await self._claim_transcript(session, path):
                    return path

        engine = (session.engine or "").strip().lower()
        async with self._engine_transcript_lock(engine):
            live = [
                s for s in list(self.sessions.values())
                if (s.engine or "").strip().lower() == engine and s.is_process_alive()
            ]
            live.sort(key=lambda s: (self._session_spawn_anchor(s), s.session_id))
            waiting = [s for s in live if not self._session_has_transcript(s)]
            if not waiting or waiting[0].session_id != session.session_id:
                # A session launched earlier has no conversation yet: this one waits its turn.
                return None
            ordered = [
                p for p in unmarked
                if self._conversation_created_after(p, session, levels_up)
            ]
            ordered.sort(
                key=lambda p: ((self._conversation_creation_time(p, levels_up)[0] or 0.0), str(p))
            )
            for path in ordered:
                if self._transcript_claimed_by_other(path, session):
                    continue
                if await self._claim_transcript(session, path):
                    return path
        return None

    @staticmethod
    def _transcript_scan_interval(elapsed: float) -> float:
        """Q8: look every second for the first minute, then every 5 s, for as long as the session lives."""
        if elapsed < TRANSCRIPT_SCAN_FAST_WINDOW_SEC:
            return TRANSCRIPT_SCAN_FAST_INTERVAL_SEC
        return TRANSCRIPT_SCAN_SLOW_INTERVAL_SEC

    def _infer_engine(self, profile_id: str, prof: Dict[str, Any]) -> str:
        if prof.get("engine"):
            return str(prof["engine"]).lower()
        pid = profile_id.lower()
        for prefix in ("omp", "kimi", "agy", "codex", "claude", "dsh", "pwsh"):
            if pid.startswith(prefix):
                return prefix
        return "generic"

    def _build_engine_model_options(
        self,
        eng: str,
        engine_info: Dict[str, Any],
        all_models: List[str],
        disabled_models: List[str],
        model_notes: Dict[str, str],
        details: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """B3: the normalised `models` list the console and the routing guide use instead of raw CLI ids."""
        engine_levels = [str(x) for x in (engine_info.get("available_thinking_levels") or [])]
        detail_by_id: Dict[str, Dict[str, Any]] = {}
        for d in details:
            mid = str(d.get("id") or "")
            if mid and mid not in detail_by_id:
                detail_by_id[mid] = d
        disabled = set(disabled_models or [])
        notes = model_notes or {}

        def _levels_for(mid: str) -> List[str]:
            d = detail_by_id.get(mid) or {}
            supported = d.get("supported_thinking_levels")
            if supported:
                return [str(x) for x in supported]
            if eng == "omp":
                omp_levels = d.get("omp_cli_thinking_levels")
                if omp_levels:
                    return [str(x) for x in omp_levels]
            return list(engine_levels)

        result: List[Dict[str, Any]] = []
        if eng == "agy":
            # agy carries its thinking level in the model name suffix: group the raw ids by base.
            order: List[str] = []
            groups: Dict[str, Dict[str, Any]] = {}
            for mid in all_models:
                base, suffix = split_agy_model_id(mid)
                if base not in groups:
                    groups[base] = {"ids": [], "levels": [], "first": mid, "enabled": False}
                    order.append(base)
                grp = groups[base]
                grp["ids"].append(mid)
                if suffix and suffix not in grp["levels"]:
                    grp["levels"].append(suffix)
                if mid not in disabled:
                    grp["enabled"] = True
            for base in order:
                grp = groups[base]
                # Keep the engine's own order (low, medium, high), then anything unexpected.
                levels = [l for l in AGY_THINKING_LEVELS if l in grp["levels"]]
                levels += [l for l in grp["levels"] if l not in AGY_THINKING_LEVELS]
                first_detail = detail_by_id.get(grp["first"]) or {}
                label = str(first_detail.get("display_name") or base)
                label = re.sub(r"\s*\((low|medium|high)\)\s*$", "", label, flags=re.IGNORECASE).strip() or base
                _first_base, first_suffix = split_agy_model_id(grp["first"])
                result.append(
                    {
                        "id": base,
                        "label": label,
                        "thinking_levels": levels,
                        "default_thinking_level": first_suffix if first_suffix in levels else None,
                        "enabled": grp["enabled"],
                        "note": str(notes.get(base) or ""),
                    }
                )
        else:
            for mid in all_models:
                d = detail_by_id.get(mid) or {}
                levels = _levels_for(mid)
                default_level = d.get("default_thinking_level")
                if default_level not in levels:
                    default_level = None
                label = str(d.get("display_name") or mid)
                result.append(
                    {
                        "id": mid,
                        "label": label or mid,
                        "thinking_levels": levels,
                        "default_thinking_level": default_level,
                        "enabled": mid not in disabled,
                        "note": str(notes.get(mid) or ""),
                    }
                )

        # Enabled first, discovery order preserved inside each group.
        return [m for m in result if m["enabled"]] + [m for m in result if not m["enabled"]]

    def load_profiles(self, refresh_discovery: bool = False, discovery: Optional[Dict[str, Any]] = None):
        """Rebuilds profiles from disk + discovery. Only the explicit refresh paths may discover here.

        Q5: profile-mutating methods never call this without a discovery snapshot, so they never run
        a CLI on the event loop; `_reload_after_write` is their entry point.
        """
        if discovery is None:
            discovery = self.model_discovery.discover_all(force_refresh=refresh_discovery)
        self._cached_discovery = discovery or {"engines": {}}
        data = self._read_profiles_for_load()
        if data is None:
            # Q6: the file is held by another program; keep the profiles already in memory instead of
            # replacing them with {} (the console must never show an empty list because of a lock).
            return
        self._apply_profile_data(data, self._cached_discovery)

    def _read_profiles_for_load(self) -> Optional[Dict[str, Any]]:
        """Reads the profile file for the console; None means "locked, keep what is in memory"."""
        if not self.profiles_path.exists():
            return {"profiles": {}}
        try:
            text = self._read_text_with_retries(self.profiles_path)
        except ValueError:
            return None
        except OSError:
            return {}
        try:
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _default_engine_binary(engine: str) -> str:
        home = Path(os.path.expanduser("~"))
        if engine == "omp":
            return shutil.which("omp") or str(home / "AppData" / "Local" / "omp" / "omp.exe")
        if engine == "kimi":
            return shutil.which("kimi") or str(home / ".kimi-code" / "bin" / "kimi.exe")
        if engine == "agy":
            return shutil.which("agy") or str(home / "AppData" / "Local" / "agy" / "bin" / "agy.EXE")
        if engine == "codex":
            return (
                shutil.which("codex.cmd")
                or shutil.which("codex")
                or str(home / "AppData" / "Roaming" / "npm" / "codex.cmd")
            )
        if engine == "claude":
            return (
                shutil.which("claude.exe")
                or shutil.which("claude")
                or shutil.which("claude.cmd")
                or str(home / ".local" / "bin" / "claude.exe")
            )
        return "pwsh.exe"

    def _builtin_profiles(self) -> Dict[str, Dict[str, Any]]:
        return {
            "omp": {
                "id": "omp",
                "name": "OMP（Oh-My-Pi）",
                "engine": "omp",
                "kind": "tui",
                "command": self._default_engine_binary("omp"),
                "args": [],
                "task_mode": "omp_positional",
                "startup_delay_sec": 2.0,
                "submit_key": "\r",
                "enabled": True,
                "description": "Oh-My-Pi 终端 Agent，模型与思考等级在启动时传入（--model / --thinking）。",
            },
            "kimi": {
                "id": "kimi",
                "name": "Kimi Code",
                "engine": "kimi",
                "kind": "tui",
                "command": self._default_engine_binary("kimi"),
                "args": [],
                "task_mode": "pty_input",
                "startup_delay_sec": 3.5,
                "submit_key": "\r",
                "enabled": True,
                "description": "Kimi Code CLI。模型用 -m 传入；思考等级通过环境变量 KIMI_MODEL_THINKING_EFFORT 只作用于本会话（Kimi 状态栏显示的仍是全局设置，实际请求按本会话等级）。",
            },
            "agy": {
                "id": "agy",
                "name": "Antigravity（AGY）",
                "engine": "agy",
                "kind": "tui",
                "command": self._default_engine_binary("agy"),
                "args": [],
                "task_mode": "agy_interactive_flag",
                "startup_delay_sec": 2.5,
                "submit_key": "\r",
                "enabled": True,
                "description": "Antigravity CLI。思考等级是模型名后缀（如 gemini-4-argon-high），由 Hub 按所选等级拼接。",
            },
            "codex": {
                "id": "codex",
                "name": "Codex",
                "engine": "codex",
                "kind": "tui",
                "command": self._default_engine_binary("codex"),
                "args": ["-c", "check_for_update_on_startup=false"],
                "task_mode": "codex_positional",
                "startup_delay_sec": 2.5,
                "submit_key": "\r",
                "enabled": True,
                "description": "OpenAI Codex CLI。模型用 -m 传入，思考等级用 -c model_reasoning_effort 传入。",
            },
            "claude": {
                "id": "claude",
                "name": "Claude Code",
                "engine": "claude",
                "kind": "tui",
                "command": self._default_engine_binary("claude"),
                "args": [],
                "task_mode": "claude_positional",
                "startup_delay_sec": 2.0,
                "submit_key": "\r",
                "enabled": True,
                "description": "Anthropic Claude Code CLI。模型与思考等级在启动时传入（--model / --effort），高风险命令默认暂停审批。",
            },
            "dsh": {
                "id": "dsh",
                "name": "DeepSeek Harness",
                "engine": "dsh",
                "kind": "tui",
                "command": "pwsh.exe",
                "args": ["-NoLogo", "-NoProfile"],
                "task_mode": "dsh_hub_api",
                "startup_delay_sec": 0.5,
                "submit_key": "\r",
                "enabled": True,
                "description": "通过 DSH Hub API 创建的原生会话，总控台显示其实时事件流。",
                "dsh_tier": "flash",
            },
            "pwsh": {
                "id": "pwsh",
                "name": "PowerShell 7",
                "engine": "pwsh",
                "kind": "tui",
                "command": "pwsh.exe",
                "args": ["-NoLogo"],
                "startup_delay_sec": 1.0,
                "submit_key": "\r",
                "enabled": True,
                "description": "交互式 PowerShell 7 终端。",
            },
        }

    def _apply_profile_data(self, data: Dict[str, Any], discovery: Dict[str, Any]):
        """Rebuilds the in-memory catalog and profiles from one file document and one discovery snapshot."""
        raw_engines = dict((discovery or {}).get("engines", {}) or {})
        file_profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
        claude_prof = file_profiles.get("claude") if isinstance(file_profiles.get("claude"), dict) else None
        if claude_prof is None:
            for p_entry in file_profiles.values():
                if isinstance(p_entry, dict) and str(p_entry.get("engine") or "").strip().lower() == "claude":
                    claude_prof = p_entry
                    break
        if isinstance(claude_prof, dict) and "claude" in raw_engines:
            claude_env = claude_prof.get("env") if isinstance(claude_prof.get("env"), dict) else {}
            claude_cfg_override = str(
                claude_prof.get("config_dir") or claude_env.get("CLAUDE_CONFIG_DIR") or ""
            ).strip() or None
            claude_bin_override = str(claude_prof.get("command") or "").strip() or None
            if claude_bin_override or claude_cfg_override:
                try:
                    raw_engines["claude"] = self.model_discovery._discover_claude(
                        binary_override=claude_bin_override,
                        config_dir_override=claude_cfg_override,
                    )
                except Exception:
                    pass

        self.engine_policies = data.get("engine_policies") or {}
        self.global_routing_instructions = str(data.get("global_routing_instructions") or "").strip()
        try:
            self.subagent_idle_timeout_minutes = normalize_subagent_idle_timeout(
                data.get("subagent_idle_timeout_minutes", DEFAULT_SUBAGENT_IDLE_TIMEOUT_MINUTES))
        except ValueError:
            logger.warning("Invalid subagent_idle_timeout_minutes; keeping the current timeout")

        # Apply per-engine model enable/disable policies
        processed_engines: Dict[str, Any] = {}
        for eng, eng_info in raw_engines.items():
            e_copy = dict(eng_info)
            all_mods = list(e_copy.get("all_discovered_models") or e_copy.get("available_models", []))
            pol = self.engine_policies.get(eng) or {}
            disabled_mods = list(pol.get("disabled_models") or [])
            mod_notes = dict(pol.get("model_notes") or {})
            enabled_mods = [m for m in all_mods if m not in disabled_mods]

            e_copy["all_discovered_models"] = all_mods
            e_copy["disabled_models"] = disabled_mods
            e_copy["model_notes"] = mod_notes
            e_copy["available_models"] = enabled_mods

            details = []
            for md in (e_copy.get("model_details") or []):
                md_copy = dict(md)
                mid = md_copy.get("id", "")
                md_copy["enabled"] = mid not in disabled_mods
                if mid in mod_notes:
                    md_copy["note"] = mod_notes[mid]
                details.append(md_copy)
            e_copy["model_details"] = details
            e_copy["models"] = self._build_engine_model_options(
                eng=eng,
                engine_info=e_copy,
                all_models=all_mods,
                disabled_models=disabled_mods,
                model_notes=mod_notes,
                details=details,
            )
            e_copy["available_models"] = [
                str(m.get("id")) for m in e_copy["models"] if m.get("enabled") and m.get("id")
            ]
            if e_copy["available_models"] and e_copy.get("default_model") not in e_copy["available_models"]:
                first_enabled = next((m for m in e_copy["models"] if m.get("enabled")), None)
                if first_enabled:
                    e_copy["default_model"] = first_enabled.get("id")
                    if first_enabled.get("default_thinking_level"):
                        e_copy["default_thinking_level"] = first_enabled["default_thinking_level"]
            enabled_level_union: List[str] = []
            for m_opt in e_copy["models"]:
                if not m_opt.get("enabled"):
                    continue
                for lvl in (m_opt.get("thinking_levels") or []):
                    if lvl and str(lvl) not in enabled_level_union:
                        enabled_level_union.append(str(lvl))
            if enabled_level_union:
                e_copy["available_thinking_levels"] = self._sorted_levels_by_engine_order(
                    eng, enabled_level_union
                )
            processed_engines[eng] = e_copy

        self.engines_catalog = processed_engines

        file_profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
        deleted_ids = {str(x) for x in (data.get("deleted_profiles") or [])}
        builtins = self._builtin_profiles()
        raw_profiles: Dict[str, Dict[str, Any]] = {}
        for pid, base_prof in builtins.items():
            if pid in deleted_ids and pid not in file_profiles:
                continue
            merged_prof = dict(base_prof)
            if isinstance(file_profiles.get(pid), dict):
                merged_prof.update(file_profiles[pid])
            raw_profiles[pid] = merged_prof
        for pid, custom_prof in file_profiles.items():
            if pid not in raw_profiles and isinstance(custom_prof, dict):
                raw_profiles[pid] = dict(custom_prof)

        enriched: Dict[str, Dict[str, Any]] = {}
        for pid, p in raw_profiles.items():
            prof = dict(p)
            eng = self._infer_engine(pid, prof)
            prof["engine"] = eng
            prof["enabled"] = bool(prof.get("enabled", True))
            # The global routing guide is authoritative; when_to_use remains a supplemental
            # hint for new or not-yet-benchmarked route cases.
            prof["when_to_use"] = prof.get("when_to_use") or ""
            try:
                prof["env"] = normalize_env_map(prof.get("env"))
            except ValueError:
                prof["env"] = {}
            # Round 2: the console and the launch read a plain bool (a hand-edited non-bool is off).
            prof[BYPASS_APPROVALS_KEY] = profile_bypasses_approvals(prof)
            eng_info = self.engines_catalog.get(eng) or {}
            file_entry = file_profiles.get(pid) if isinstance(file_profiles.get(pid), dict) else {}
            if not file_entry.get("command") and eng in ("omp", "kimi", "agy", "codex", "claude") and eng_info.get("binary"):
                prof["command"] = eng_info["binary"]
            # B4: remember whether the default came from the profile file or from the engine.
            explicit_model = str(prof.get("default_model") or "").strip()
            explicit_thinking = str(prof.get("default_thinking_level") or "").strip()
            if eng == "agy" and explicit_model:
                # agy ids carry a thinking suffix: store the base, keep the suffix as the thinking level.
                base_model, suffix_level = split_agy_model_id(explicit_model)
                explicit_model = base_model
                if not explicit_thinking and suffix_level:
                    explicit_thinking = suffix_level
            if explicit_model:
                prof["default_model"] = explicit_model
            elif eng_info.get("default_model"):
                prof["default_model"] = eng_info["default_model"]
            if explicit_thinking:
                prof["default_thinking_level"] = explicit_thinking
            elif eng_info.get("default_thinking_level"):
                prof["default_thinking_level"] = eng_info["default_thinking_level"]
            prof["default_model_source"] = "profile" if explicit_model else "engine"
            prof["default_thinking_source"] = "profile" if explicit_thinking else "engine"

            all_mods = eng_info.get("all_discovered_models", [])
            prof_extra_disabled = set(prof.get("disabled_models") or [])
            prof_disabled = list(
                dict.fromkeys(list(eng_info.get("disabled_models", [])) + list(prof.get("disabled_models") or []))
            )
            prof["all_discovered_models"] = all_mods
            prof["disabled_models"] = prof_disabled
            prof["available_models"] = [
                str(m.get("id"))
                for m in (eng_info.get("models") or [])
                if m.get("enabled") and m.get("id") and str(m.get("id")) not in prof_extra_disabled
            ]
            prof["available_thinking_levels"] = eng_info.get("available_thinking_levels") or prof.get(
                "available_thinking_levels", []
            )
            prof["model_details"] = eng_info.get("model_details", [])
            enriched[pid] = prof

        # Keep engines_catalog defaults aligned with each engine's canonical profile so MCP callers
        # and the console's engine cards never see conflicting default_model / default_thinking_level.
        for eng, e_copy in processed_engines.items():
            canonical = enriched.get(eng)
            if not canonical:
                continue
            if canonical.get("default_model"):
                e_copy["default_model"] = canonical["default_model"]
            if canonical.get("default_thinking_level"):
                e_copy["default_thinking_level"] = canonical["default_thinking_level"]

        # B2: discovery may still report ~/.codex [profiles.*] and recent configurations in the
        # catalog, but they no longer synthesise profiles. A Profile is a launch recipe, not a combo.

        self.profiles = enriched

    def _reload_after_write(self, data: Dict[str, Any]) -> None:
        """Q5: the mutation already holds the document it just wrote — apply it with the cached discovery.

        No discovery run, no disk re-read: the console stays responsive while a profile is saved, and
        the catalog-dependent validation reports 「模型列表尚未就绪，请稍后再试」 when nothing is cached.
        """
        self._apply_profile_data(data, self._cached_discovery)

    def get_enabled_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Returns only enabled profiles where the default model is not disabled (or dynamic base profiles)."""
        result: Dict[str, Dict[str, Any]] = {}
        for pid, p in self.profiles.items():
            if not p.get("enabled", True):
                continue
            result[pid] = p
        return result

    def _active_engine_ids(self) -> set[str]:
        """Returns the set of engine IDs that have at least one enabled profile."""
        return {
            str(p.get("engine") or "").strip()
            for p in self.get_enabled_profiles().values()
            if p.get("engine")
        }

    def get_mcp_engines_catalog(self) -> Dict[str, Dict[str, Any]]:
        """Returns a compact, enabled-only view of engines_catalog for MCP callers (only engines with an enabled profile)."""
        active_engines = self._active_engine_ids()
        result: Dict[str, Dict[str, Any]] = {}
        for eng, info in self.engines_catalog.items():
            if eng not in active_engines:
                continue
            enabled_models: List[Dict[str, Any]] = []
            for m in (info.get("models") or []):
                if not m.get("enabled"):
                    continue
                m_item: Dict[str, Any] = {
                    "id": m.get("id"),
                    "thinking_levels": list(m.get("thinking_levels") or []),
                }
                if m.get("default_thinking_level"):
                    m_item["default_thinking_level"] = m["default_thinking_level"]
                note = (m.get("note") or "").strip()
                if note:
                    m_item["note"] = note
                enabled_models.append(m_item)
            result[eng] = {
                "engine": info.get("engine") or eng,
                "name": info.get("name") or eng,
                "installed": bool(info.get("installed", True)),
                "default_model": info.get("default_model"),
                "default_thinking_level": info.get("default_thinking_level"),
                "available_models": list(info.get("available_models") or []),
                "available_thinking_levels": list(info.get("available_thinking_levels") or []),
                "models": enabled_models,
            }
        return result

    def get_mcp_enabled_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Returns a compact, enabled-only view of profiles for MCP callers (no disabled models or internal launch details)."""
        result: Dict[str, Dict[str, Any]] = {}
        builtins = self._builtin_profiles()
        for pid, p in self.get_enabled_profiles().items():
            description = p.get("description") or ""
            if pid in builtins and description == builtins[pid].get("description"):
                description = PROFILE_DESCRIPTIONS[pid]
            prof_item: Dict[str, Any] = {
                "id": p.get("id") or pid,
                "name": p.get("name") or pid,
                "engine": p.get("engine"),
                "kind": p.get("kind") or "agent",
                "default_model": p.get("default_model"),
                "default_thinking_level": p.get("default_thinking_level"),
                "available_models": list(p.get("available_models") or []),
                "available_thinking_levels": list(p.get("available_thinking_levels") or []),
                "when_to_use": p.get("when_to_use") or "",
                "description": description,
                "env": dict(p.get("env") or {}),
                BYPASS_APPROVALS_KEY: bool(p.get(BYPASS_APPROVALS_KEY, False)),
            }
            result[pid] = prof_item
        return result

    def build_routing_guide_markdown(self) -> str:
        """B12: the guide parent agents read. Only enabled profiles and their engines' enabled models appear."""
        active_engines = self._active_engine_ids()
        lines: List[str] = ["# Subagent routing guide"]
        instructions = (self.global_routing_instructions or "").strip()
        if instructions:
            lines.append(instructions)
        lines.append("")
        lines.append("## Dispatch and waiting")
        lines.extend(f"- {rule}" for rule in ROUTING_RULES)
        timeout = self.subagent_idle_timeout_minutes
        lines.append(
            f"- Hub automatically closes agent-started sessions after {timeout} minutes "
            "of completed/ended inactivity (checked every 30 seconds). Running sessions and "
            "pending questions/approvals are excluded. A new turn resets the timer. "
            "Console-started sessions are excluded."
            if timeout else "- Automatic subagent cleanup is disabled; close unused sessions yourself."
        )
        lines.append("")
        lines.append("## Profiles (agent_type)")
        for pid, p in self.profiles.items():
            if not p.get("enabled", True):
                continue
            name = p.get("name") or pid
            def_mod = p.get("default_model") or "none"
            def_thk = p.get("default_thinking_level") or "none"
            lines.append(f"- `{pid}` — {name} | defaults: {def_mod} / {def_thk}")
            when_lines = [ln.strip() for ln in (p.get("when_to_use") or "").splitlines() if ln.strip()]
            if when_lines:
                lines.append("  When to use (supplemental): " + "\n  ".join(when_lines))
        lines.append("")
        lines.append("## Available models and thinking levels (model / thinking_level)")
        for eng, info in self.engines_catalog.items():
            if eng not in active_engines:
                continue
            models = [m for m in (info.get("models") or []) if m.get("enabled")]
            if not models:
                continue
            header = f"### {eng}"
            if eng == "agy":
                header += ' (thinking level is a model-name suffix; pass model="gemini-4-argon", thinking_level="high" or model="gemini-4-argon-high")'
            lines.append(header)
            for m in models:
                levels = ", ".join(m.get("thinking_levels") or []) or "none"
                note = (m.get("note") or "").strip()
                model_line = f"- `{(m.get('id') or '')}`"
                if note:
                    model_line += f" — {note}"
                model_line += f" | thinking levels: {levels}"
                lines.append(model_line)
        return "\n".join(lines)

    # ---- B13: model enable/disable + per-model notes -------------------------------------

    PROFILE_PERSIST_KEYS = (
        "id",
        "name",
        "engine",
        "kind",
        "command",
        "config_dir",
        "args",
        "env",
        "default_model",
        "default_thinking_level",
        "task_mode",
        "startup_delay_sec",
        "submit_key",
        "enabled",
        "when_to_use",
        "description",
        "gui_url",
        "dsh_tier",
        "disabled_models",
        # Round 2: the per-profile approval off switch (bool; missing = false).
        "bypass_approvals",
    )

    # B5 / R11: every persistable key except these three can be cleared with null or "". The stored
    # key is deleted, so `startup_delay_sec` falls back to 2.5 and `task_mode` to pty_input.
    PROFILE_REQUIRED_KEYS = ("name", "engine", "command")

    def _read_text_with_retries(self, path: Path) -> str:
        """Q6: a read retries a Windows file lock the same way a write does (30 ms, 10 attempts)."""
        last: Optional[BaseException] = None
        for _attempt in range(PROFILE_FILE_RETRY_ATTEMPTS):
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                if not _is_file_lock_error(exc):
                    raise
                last = exc
                time.sleep(PROFILE_FILE_RETRY_DELAY_SEC)
        raise ValueError(PROFILE_FILE_LOCKED_MESSAGE) from last

    def _read_profiles_file(self) -> Dict[str, Any]:
        """Reads the profile file for a caller that is about to write it back.

        R16: a parse error raises instead of returning an empty document. Q6: a file another program
        holds open raises 「配置文件被其他程序占用，请稍后重试」 after the retry loop, never the
        parse-error message and never a bare 500.
        """
        if not self.profiles_path.exists():
            return {"profiles": {}}
        text = self._read_text_with_retries(self.profiles_path)
        try:
            data = json.loads(text)
        except Exception as exc:
            raise ValueError(f"agent_profiles.json 解析失败，本次修改未写入：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("agent_profiles.json 顶层不是 JSON 对象，本次修改未写入。")
        return data

    def _write_profiles_file(self, raw_data: Dict[str, Any]) -> None:
        """R16: write a temp file in the same folder and replace, so a crash never leaves a half file.

        LF line endings, UTF-8, Chinese kept readable (`ensure_ascii=False`), indent 2. Q6: the temp
        open and the replace both retry a Windows lock (10 × 30 ms) before the save reports the
        「配置文件被其他程序占用」 ValueError.
        """
        text = json.dumps(raw_data, ensure_ascii=False, indent=2) + "\n"
        target = self.profiles_path
        last: Optional[BaseException] = None
        for _attempt in range(PROFILE_FILE_RETRY_ATTEMPTS):
            fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, target)
                return
            except OSError as exc:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                if not _is_file_lock_error(exc):
                    raise
                last = exc
                time.sleep(PROFILE_FILE_RETRY_DELAY_SEC)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        raise ValueError(PROFILE_FILE_LOCKED_MESSAGE) from last

    def engine_model_options(self, engine: str) -> List[Dict[str, Any]]:
        info = self.engines_catalog.get((engine or "").strip().lower()) or {}
        return list(info.get("models") or [])

    def engine_model_option(self, engine: str, model_id: str) -> Optional[Dict[str, Any]]:
        mid = (model_id or "").strip()
        for m in self.engine_model_options(engine):
            if m.get("id") == mid:
                return m
        return None

    def _sorted_levels_by_engine_order(self, engine: str, levels: List[str]) -> List[str]:
        info = self.engines_catalog.get(engine) or {}
        order = [str(x) for x in (info.get("available_thinking_levels") or [])]
        seen = [l for l in order if l in levels]
        return seen + [l for l in levels if l not in seen]

    def toggle_engine_model(
        self,
        engine: str,
        model_id: str,
        enabled: Optional[bool] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """B13: enable/disable one model (agy: a base id toggles every suffixed id of that base) and edit its note.

        R6: `enabled=None` means a note-only update; the model's enable/disable state is left exactly as
        it is. An empty note deletes it.
        """
        if enabled is None and note is None:
            raise ValueError("缺少 enabled 或 note，无法更新模型。")
        eng = (engine or "").strip().lower()
        mid = (model_id or "").strip()
        if not eng or not mid:
            raise ValueError("缺少引擎或模型标识。")
        if eng not in KNOWN_ENGINES:
            raise ValueError(f"未知引擎「{eng}」。可用：{', '.join(KNOWN_ENGINES)}")

        # Accept either a raw discovered id or a B3 base id (agy groups its suffix variants).
        catalog_ids = set(str(x) for x in (self.engines_catalog.get(eng, {}).get("all_discovered_models") or []))
        base_ids = set(str(m.get("id")) for m in self.engine_model_options(eng) if m.get("id"))
        base, _suffix = split_agy_model_id(mid) if eng == "agy" else (mid, None)
        if catalog_ids and mid not in catalog_ids and mid not in base_ids and base not in base_ids:
            raise ValueError(f"引擎「{eng}」里没有模型「{mid}」。")

        # Every raw id this call must write policy for (agy: all suffixes of the base).
        if eng == "agy":
            targets = sorted(m for m in catalog_ids if split_agy_model_id(m)[0] == base)
            if not targets:
                # Policy may key a base id directly when discovery reported no suffix variants.
                targets = [base]
        else:
            targets = [mid]

        raw_data = self._read_profiles_file()
        policies = raw_data.setdefault("engine_policies", {})
        eng_pol = policies.setdefault(eng, {"disabled_models": [], "model_notes": {}})
        disabled_list: List[str] = [str(m) for m in (eng_pol.get("disabled_models") or [])]

        if enabled is not None:
            for target in targets:
                if enabled:
                    disabled_list = [m for m in disabled_list if m != target]
                elif target not in disabled_list:
                    disabled_list.append(target)
            eng_pol["disabled_models"] = disabled_list

        note_engine_key = base if eng == "agy" else mid
        if note is not None:
            notes_map = eng_pol.setdefault("model_notes", {})
            cleaned = str(note).strip()
            if cleaned:
                notes_map[note_engine_key] = cleaned
            else:
                notes_map.pop(note_engine_key, None)

        self._write_profiles_file(raw_data)
        self._reload_after_write(raw_data)
        return self.engines_catalog.get(eng, {})

    def save_global_routing_instructions(self, instructions: str, **settings) -> str:
        """Persists global routing instructions for Parent Agents into agent_profiles.json."""
        raw_data = self._read_profiles_file()
        if "subagent_idle_timeout_minutes" in settings:
            raw_data["subagent_idle_timeout_minutes"] = normalize_subagent_idle_timeout(
                settings["subagent_idle_timeout_minutes"])
        raw_data["global_routing_instructions"] = instructions.strip()
        self._write_profiles_file(raw_data)
        self._reload_after_write(raw_data)
        return self.global_routing_instructions

    # ---- B5: saving and deleting profiles ------------------------------------------------

    def _validate_profile_input(
        self,
        eng: str,
        args: List[str],
        kind: str,
        default_model: Optional[str],
        default_thinking: Optional[str],
    ) -> None:
        bad = injected_flags_in_args(eng, args)
        if bad:
            raise ValueError(
                f"参数里不要写模型或思考等级（{bad[0]}）：Hub 会按启动时的选择自动注入。"
            )
        if kind not in ("tui", "web_gui"):
            raise ValueError(f"会话形式「{kind}」无效，只能是 tui 或 web_gui。")
        if kind == "web_gui" and eng != "dsh":
            raise ValueError("只有 dsh 引擎可以使用 web_gui 会话形式。")

        options = self.engine_model_options(eng)
        if not options:
            if eng in ("pwsh", "generic") or self.engines_catalog.get(eng) is not None:
                # pwsh really has no model choice; a discovered engine with zero models is a discovery
                # gap (R4), and its profile defaults are accepted unvalidated.
                return
            if default_model or default_thinking:
                # Q5: the catalog is not cached yet (startup discovery is still running). Say so
                # instead of blocking the event loop on a discovery run just to validate this save.
                raise ValueError("模型列表尚未就绪，请稍后再试")
            return
        ids = [str(m.get("id")) for m in options]
        enabled_ids = [str(m.get("id")) for m in options if m.get("enabled")]
        chosen = self.engine_model_option(eng, default_model) if default_model else None
        if default_model and not chosen:
            if eng == "agy":
                base, _suffix = split_agy_model_id(str(default_model))
                chosen = self.engine_model_option(eng, base)
            if chosen is None:
                raise ValueError(f"模型「{default_model}」不在引擎「{eng}」的可用模型里。可用：{', '.join(enabled_ids) or '无'}")
        if chosen is not None and not chosen.get("enabled"):
            raise ValueError(f"模型「{chosen.get('id')}」已被停用，不能设为默认模型。")
        if default_thinking and chosen is not None:
            levels = [str(x) for x in (chosen.get("thinking_levels") or [])]
            if str(default_thinking) not in levels:
                raise ValueError(
                    f"{chosen.get('id')} 支持的思考等级：{', '.join(levels) or '无'}"
                )
        if default_model and str(default_model) not in ids and not chosen:
            raise ValueError(f"模型「{default_model}」不在引擎「{eng}」的可用模型里。")

    def save_or_update_profile(self, profile_data: Dict[str, Any]) -> Dict[str, Any]:
        """B5: persists one Profile as a launch recipe, dropping every enrichment key."""
        pid = str(profile_data.get("id") or "").strip()
        if not pid:
            raise ValueError("Profile 的 id 不能为空。")

        raw_data = self._read_profiles_file()
        profiles_map = raw_data.setdefault("profiles", {})
        builtins = self._builtin_profiles()
        existing = dict(builtins.get(pid) or {})
        if isinstance(profiles_map.get(pid), dict):
            existing.update(profiles_map[pid])
        deleted_list = [str(x) for x in (raw_data.get("deleted_profiles") or []) if str(x) != pid]
        if deleted_list:
            raw_data["deleted_profiles"] = deleted_list
        else:
            raw_data.pop("deleted_profiles", None)

        incoming = dict(profile_data)
        if BYPASS_APPROVALS_KEY in incoming and incoming[BYPASS_APPROVALS_KEY] is not None \
                and not isinstance(incoming[BYPASS_APPROVALS_KEY], bool):
            raise ValueError(
                f"bypass_approvals 只能是 true 或 false（布尔值），收到的是 "
                f"{json.dumps(incoming[BYPASS_APPROVALS_KEY], ensure_ascii=False)}。"
            )
        merged = dict(existing)
        for key in self.PROFILE_PERSIST_KEYS:
            if key not in incoming:
                continue
            value = incoming[key]
            if key not in self.PROFILE_REQUIRED_KEYS and (
                value is None
                or (isinstance(value, str) and not value.strip())
                or (key == "env" and value == {})
            ):
                merged.pop(key, None)
                continue
            merged[key] = value

        for required in ("name", "engine", "command"):
            value = merged.get(required)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"Profile 的 {required} 不能清空。")

        eng = self._infer_engine(pid, merged)
        merged["engine"] = eng
        merged["id"] = pid

        raw_args = merged.get("args")
        if raw_args is None:
            # R11: a cleared args key means "no arguments", not a validation failure.
            raw_args = []
        if not isinstance(raw_args, list) or any(not isinstance(a, str) for a in raw_args):
            raise ValueError("参数（args）必须是字符串列表，每行一个参数。")
        args = [a for a in raw_args]
        if len(args) > MAX_LAUNCH_ARGS:
            raise ValueError("参数（args）过多。")

        env_map = normalize_env_map(merged.get("env"), field_label="环境变量（env）")
        if env_map:
            merged["env"] = env_map
        else:
            merged.pop("env", None)

        kind = str(merged.get("kind") or "tui")
        default_model = merged.get("default_model")
        default_model = str(default_model).strip() if default_model else None
        if not default_model:
            default_model = None
        default_thinking = merged.get("default_thinking_level")
        default_thinking = str(default_thinking).strip() if default_thinking else None
        if not default_thinking:
            default_thinking = None

        self._validate_profile_input(eng, args, kind, default_model, default_thinking)

        if eng == "agy" and default_model:
            default_model, suffix_level = split_agy_model_id(default_model)
            merged["default_model"] = default_model
            # R8: a suffixed model id only supplies the level when the caller did not supply one.
            if suffix_level and not default_thinking:
                merged["default_thinking_level"] = suffix_level

        clean_entry: Dict[str, Any] = {}
        for key in self.PROFILE_PERSIST_KEYS:
            if key in merged:
                clean_entry[key] = merged[key]
        clean_entry["id"] = pid
        clean_entry["engine"] = eng
        clean_entry["args"] = args
        if not clean_entry.get("env"):
            clean_entry.pop("env", None)
        # R11: a key the caller cleared is not written back. The readers below default what is missing:
        # load_profiles fills enabled / when_to_use, resolve_launch defaults kind, create_session `\r`
        # for submit_key and 2.5 for a missing startup_delay_sec.
        if not clean_entry.get("name"):
            clean_entry["name"] = pid
        if clean_entry.get("default_model") in (None, ""):
            clean_entry.pop("default_model", None)
        if clean_entry.get("default_thinking_level") in (None, ""):
            clean_entry.pop("default_thinking_level", None)
        # Round 2: only a true switch is written (missing = false), so saving a profile with the
        # switch off leaves the file exactly as before this key existed.
        if clean_entry.get(BYPASS_APPROVALS_KEY) is not True:
            clean_entry.pop(BYPASS_APPROVALS_KEY, None)

        profiles_map[pid] = clean_entry
        self._write_profiles_file(raw_data)
        self._reload_after_write(raw_data)
        return self.profiles.get(pid, {})

    def delete_profile(self, profile_id: str) -> bool:
        """B5: deletes one Profile; an unknown id raises a Chinese ValueError."""
        pid = (profile_id or "").strip()
        raw_data = self._read_profiles_file()
        profiles_map = raw_data.get("profiles") or {}
        builtins = self._builtin_profiles()
        deleted_list = [str(x) for x in (raw_data.get("deleted_profiles") or [])]
        in_file = pid in profiles_map
        in_builtin = pid in builtins and pid not in deleted_list
        if not in_file and not in_builtin:
            raise ValueError(f"找不到 Profile「{pid}」。")
        if in_file:
            del profiles_map[pid]
        if pid in builtins and pid not in deleted_list:
            deleted_list.append(pid)
            raw_data["deleted_profiles"] = deleted_list
        self._write_profiles_file(raw_data)
        self._reload_after_write(raw_data)
        return True


    def _notify_waiters(self):
        for ev in list(self._event_waiters):
            ev.set()

    @logical_write
    async def persist_session(
        self, session: AgentSession, force_scrollback: bool = False, final: bool = False
    ):
        """Writes the row. `final` marks the exit/archive write, the only one that rebuilds the
        mailbox thread (A11) instead of leaving the stored one alone."""
        scrollback = session.get_full_scrollback_text(max_lines=250)
        await self.db.upsert_session(
            session.to_dict(),
            scrollback_text=scrollback,
            archive=self._archive_payload(session, force_scrollback=force_scrollback, final=final),
        )

    # ---- Archive & resume (spec archive-spec.md §2-§5) --------------------------------------

    @staticmethod
    def _last_screen_text(session: AgentSession) -> str:
        """The last 20 non-empty screen lines, as the final record keeps them (spec §3)."""
        try:
            lines = [ln for ln in session.get_clean_screen_text(max_lines=80).splitlines() if ln.strip()]
        except Exception:
            lines = []
        return "\n".join(lines[-20:])

    @staticmethod
    def _first_line_text(text: Any) -> str:
        for line in str(text or "").splitlines():
            if line.strip():
                return line.strip()
        return ""

    def _mailbox_thread(self, session_id: str) -> List[Dict[str, Any]]:
        """The mailbox thread in both directions, as the archive record stores it (spec §3, A11)."""
        thread: List[Dict[str, Any]] = []
        for msg in self.parent_messages.get(session_id, []):
            thread.append({
                "direction": "parent",
                "timestamp": msg.get("timestamp"),
                "content": msg.get("content", ""),
                "message_id": msg.get("id"),
                "resolved_question_id": msg.get("resolved_question_id"),
            })
        # A11: the worker side comes from the per-session log, not from the bounded event ring.
        for ev in getattr(self, "_worker_message_logs", {}).get(session_id, []):
            thread.append({
                "direction": "worker",
                "timestamp": ev.get("timestamp"),
                "content": ev.get("content", ""),
                "type": ev.get("type"),
                "seq": ev.get("seq"),
            })
        thread.sort(key=lambda item: float(item.get("timestamp") or 0))
        return thread

    def _remember_worker_message(self, session_id: str, record: Dict[str, Any]) -> None:
        """A11: keeps the worker→parent side of the archive thread outside the 2000-event ring."""
        if record.get("type") not in MAILBOX_EVENT_TYPES:
            return
        logs = getattr(self, "_worker_message_logs", None)
        if logs is None:
            logs = {}
            self._worker_message_logs = logs
        entries = logs.setdefault(session_id, [])
        entries.append({
            "direction": "worker",
            "timestamp": record.get("timestamp"),
            "content": record.get("content", ""),
            "type": record.get("type"),
            "seq": record.get("seq"),
        })
        if len(entries) > WORKER_MESSAGE_LOG_CAP:
            del entries[: len(entries) - WORKER_MESSAGE_LOG_CAP]

    def _scrollback_blob(self, session: AgentSession, force: bool = False) -> Optional[bytes]:
        """zlib-compressed newest ≤512 KB of the raw terminal stream (spec §1.6/§3).

        While the session is live it is written at most once every 60 s and only when the stream
        changed; the final record always writes it.
        """
        now = time.time()
        if not force:
            if now - session._scrollback_written_at < RESUME_SCROLLBACK_INTERVAL_SEC:
                return None
            if session.raw_history_rev == session._scrollback_rev_written:
                return None
            if session.raw_history_rev == getattr(session, "_scrollback_pending_rev", -1):
                return None
        # W9: cut at a line break and keep the newest resume banner (a raw byte slice can split an
        # escape sequence or drop the banner the pane is supposed to show above the new output).
        with session._lock:
            text = trim_history_text("".join(session.raw_history), RESUME_SCROLLBACK_MAX_BYTES)
            revision = session.raw_history_rev
        raw = text.encode("utf-8", "replace")
        self.db.on_accept(lambda: setattr(session, "_scrollback_pending_rev", revision))
        def committed():
            session._scrollback_written_at = time.time()
            session._scrollback_rev_written = revision
            if getattr(session, "_scrollback_pending_rev", -1) == revision:
                session._scrollback_pending_rev = -1
        self.db.on_commit(committed)
        return zlib.compress(raw, 6)

    def _archive_payload(
        self, session: AgentSession, force_scrollback: bool = False, final: bool = False
    ) -> Dict[str, Any]:
        # A11: the thread is built on the exit/archive write only; every other persist passes "" and
        # the COALESCE upsert keeps what the final record already stored.
        thread = self._mailbox_thread(session.session_id) if final else []
        return {
            "end_reason": session.end_reason,
            "archived_at": session.archived_at,
            "spawned_at": session.spawned_at,
            "profile_snapshot": (
                json.dumps(session.profile_snapshot, ensure_ascii=False) if session.profile_snapshot else ""
            ),
            "conversation_ref": (
                json.dumps(session.conversation_ref, ensure_ascii=False) if session.conversation_ref else ""
            ),
            "scrollback": self._scrollback_blob(session, force=force_scrollback),
            "last_screen": session.last_screen or "",
            "parent_messages": json.dumps(thread, ensure_ascii=False) if thread else "",
            "resume_count": session.resume_count,
            "last_resumed_at": session.last_resumed_at,
            "resumable": None if session.resumable is None else (1 if session.resumable else 0),
            "resume_block_reason": session.resume_block_reason,
        }

    @staticmethod
    def _record_from_session(session: AgentSession) -> Dict[str, Any]:
        rec = session.to_dict()
        rec["profile_snapshot"] = session.profile_snapshot
        rec["conversation_ref"] = session.conversation_ref
        return rec

    @staticmethod
    def _sticky_resume_block(record: Dict[str, Any]) -> str:
        """A4: a stored failure that re-checking must never turn back into "resumable".

        agy opened a different conversation (the archived one is not attached) and codex archived
        the conversation (only `codex unarchive` fixes that). Both are facts about the engine's own
        store, not about a file that may reappear.
        """
        reason = str(record.get("resume_block_reason") or "")
        if not reason:
            return ""
        if reason in STICKY_RESUME_BLOCK_REASONS:
            return reason
        for prefix in STICKY_RESUME_BLOCK_PREFIXES:
            if reason.startswith(prefix):
                return reason
        return ""

    def _finalize_session(self, session: AgentSession, end_reason: Optional[str] = None) -> AgentSession:
        """Writes the parts of the final record that only exist once the session has stopped."""
        if end_reason:
            session.end_reason = end_reason
        if session.ended_at is None:
            session.ended_at = time.time()
        if not session.end_reason:
            session.end_reason = session.classify_end_reason(session.exit_code)
        session.last_screen = self._last_screen_text(session)
        sticky = self._sticky_resume_block(self._record_from_session(session))
        if sticky:
            # A4: never recompute a sticky failure into "resumable".
            session.resumable = False
            session.resume_block_reason = sticky
            return session
        ok, reason = self.compute_resumable(self._record_from_session(session))
        session.resumable = ok
        session.resume_block_reason = None if ok else reason
        return session

    @logical_write
    async def _archive_session(self, session: AgentSession, end_reason: Optional[str] = None) -> AgentSession:
        """The final record, written when a session leaves the live list or at hub shutdown/start."""
        session.archived_at = time.time()
        self._finalize_session(session, end_reason=end_reason)
        # V5: an archived record must never report a live status; its end state is the truth.
        if session.end_reason:
            session.status = session.end_reason
        await self.persist_session(session, force_scrollback=True, final=True)
        return session

    def _resume_check_context(self):
        """Value snapshot for filesystem probes; worker threads never inspect live sessions."""
        with self._transcript_lock:
            claimed = dict(self._claimed_transcripts)
        return {
            "profiles": {key: {"enabled": value.get("enabled", True)} for key, value in self.profiles.items()},
            "claimed": claimed,
            "live_ids": {sid for sid, sess in self.sessions.items() if sess.is_process_alive()},
        }

    def compute_resumable(self, record: Dict[str, Any], *, context=None) -> tuple:
        """(can_resume, Chinese reason) for one hub record — the §4 pre-checks, re-checked on demand."""
        sticky = self._sticky_resume_block(record)
        if sticky:
            # A4/A5: a sticky failure stays a failure however often this is re-checked.
            return False, sticky
        engine = str(record.get("engine") or "").strip().lower()
        snapshot = record.get("profile_snapshot") or {}
        ref = record.get("conversation_ref") or {}
        session_id = str(record.get("session_id") or "")
        if not isinstance(snapshot, dict) or not snapshot:
            return False, "未记录启动参数，无法恢复"
        if engine not in RESUME_ENGINES:
            return False, ENGINE_RESUME_BLOCK_REASONS.get(engine, DEFAULT_RESUME_BLOCK_REASON)
        cwd = str(snapshot.get("cwd") or record.get("cwd") or "")
        if not cwd or not os.path.isdir(cwd):
            return False, f"工作目录已不存在：{cwd}"
        profile_id = str(snapshot.get("profile_id") or record.get("profile_id") or "")
        prof = (self.profiles if context is None else context["profiles"]).get(profile_id)
        if not prof or not prof.get("enabled", True):
            return False, f"Profile「{profile_id}」不存在或已停用"
        if not isinstance(ref, dict) or not ref:
            return False, "未记录到该会话的对话位置，无法恢复"
        if engine == "omp":
            path = str(ref.get("session_file") or "")
            if not path or not os.path.isfile(path):
                return False, f"omp 会话文件已不存在：{path}"
            if context is None:
                with self._transcript_lock:
                    owner = self._claimed_transcripts.get(self._transcript_key(Path(path)))
            else:
                owner = context["claimed"].get(self._transcript_key(Path(path)))
            if owner and owner != session_id:
                alive = owner in context["live_ids"] if context is not None else (
                    owner in self.sessions and self.sessions[owner].is_process_alive()
                )
                if alive:
                    return False, "该 omp 会话正被另一个运行中的会话使用"
        elif engine == "kimi":
            sid = str(ref.get("session_id") or "")
            sdir = str(ref.get("session_dir") or "")
            if not sid or not sdir or not os.path.isdir(sdir):
                return False, f"Kimi 会话目录已不存在：{sdir or sid}"
            lock_path = kimi_store_root() / "locks" / "sessions" / f"{sid}.lock"
            if lock_path.exists():
                holder = 0
                try:
                    holder = int((lock_path.read_text(encoding="utf-8", errors="replace").split(":", 1)[0] or "0").strip())
                except Exception:
                    holder = 0
                if holder and holder != os.getpid() and _pid_alive(holder):
                    return False, KIMI_LOCKED_MESSAGE
        elif engine == "agy":
            # V3/A3: the durable store is conversations/<uuid>.db or the brain dir itself. Keying the
            # check on transcript.jsonl was wrong: the real store has brain dirs without one.
            # The conversations store is a SIBLING of the brain dir: <root>/conversations/<uuid>.db
            # beside <root>/brain/<uuid> (confirmed against the real antigravity-cli layout).
            brain = str(ref.get("brain_dir") or "")
            conv_id = str(ref.get("conversation_id") or (Path(brain).name if brain else ""))
            durable_db = Path(brain).parent.parent / "conversations" / f"{conv_id}.db" if brain else None
            brain_ok = bool(brain) and os.path.isdir(brain)
            if not brain_ok and not (durable_db is not None and durable_db.is_file()):
                return False, f"AGY 会话目录已不存在：{brain}"
        elif engine == "codex":
            sid = str(ref.get("session_id") or "")
            path = str(ref.get("rollout_path") or "")
            if not sid:
                return False, "未记录到 codex 会话 ID，无法恢复"
            if not path or not os.path.isfile(path):
                return False, f"codex 会话记录已不存在：{path}"
        elif engine == "claude":
            sid = str(ref.get("session_id") or "")
            path = str(ref.get("transcript_path") or "")
            if not sid:
                return False, "未记录到 claude 会话 ID，无法恢复"
            if not path or not os.path.isfile(path):
                return False, f"claude 会话文件已不存在：{path}"
            if context is None:
                with self._transcript_lock:
                    owner = self._claimed_transcripts.get(self._transcript_key(Path(path)))
            else:
                owner = context["claimed"].get(self._transcript_key(Path(path)))
            if owner and owner != session_id:
                alive = owner in context["live_ids"] if context is not None else (
                    owner in self.sessions and self.sessions[owner].is_process_alive()
                )
                if alive:
                    return False, "该 claude 会话正被另一个运行中的会话使用"
        elif engine == "dsh":
            if not ref.get("dsh_job_id"):
                return False, "DSH 任务 ID 缺失，无法恢复"
        return True, ""

    def build_resume_launch(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """The revive command for one record (spec §4), built from profile_snapshot + conversation_ref.

        Pure: it never spawns anything and never touches the engine's store beyond what the caller
        already checked. The message is appended later by resume_subagent, per the engine's task mode.
        """
        engine = str(record.get("engine") or "").strip().lower()
        snapshot = record.get("profile_snapshot") or {}
        ref = record.get("conversation_ref") or {}
        cwd = str(snapshot.get("cwd") or record.get("cwd") or "")
        command = str(snapshot.get("command") or "")
        base_args = strip_legacy_worker_bridge_args([str(a) for a in (snapshot.get("args") or [])], self.runtime_configs_dir)
        structured_claude = engine == "claude" and "mcp_settings_base" in snapshot
        if structured_claude:
            # The saved argv has the old merged approval overlay. Rebuild settings from the
            # saved user/MCP settings before the current approval policy is applied.
            base_args = replace_claude_settings(base_args, snapshot["mcp_settings_base"])
        profile_id = str(snapshot.get("profile_id") or record.get("profile_id") or "")
        prof_cur = self.profiles.get(profile_id) or {}
        env = normalize_env_map(prof_cur.get("env"))
        env.update({str(k): str(v) for k, v in (snapshot.get("env") or {}).items()})
        built: Dict[str, Any] = {
            "command": command, "args": [], "env": env, "cwd": cwd,
            "message_mode": "none", "attach_offset": 0,
        }
        # D9 (2026-11-07): the snapshot goes through the same strip+inject as a fresh launch, so a
        # session archived before approval-by-default (with --auto-approve, --auto, ...) resumes in
        # the safe mode too. omp's in-memory "approve for this session" is gone after a resume anyway.
        # Explicit session overrides survive; legacy/inherited sessions follow the current profile.
        policy = resolve_session_approval(engine, prof_cur, snapshot.get("bypass_approvals_override"))
        built.update(policy)
        bypass = policy["bypass_approvals"]
        built["notes"] = []
        if engine in APPROVAL_ENGINES:
            base_args = apply_approval_flags(engine, base_args, bypass=bypass)
            if bypass:
                built["notes"].append(BYPASS_ON_NOTE)
        elif bypass:
            built["notes"].append(BYPASS_NO_EFFECT_NOTE.format(engine=engine))
        if structured_claude:
            base_args, _ = merge_claude_settings(
                base_args, cwd, approval_path=claude_approval_settings_path() if not bypass else None)
        if engine == "omp":
            built["args"] = [
                f"--session-dir={ref.get('session_dir')}",
                f"--resume={ref.get('session_file')}",
            ] + base_args
            built["message_mode"] = "positional"
        elif engine == "kimi":
            # Research: --agent / --agent-file / --environment are rejected together with -S.
            cleaned = [a for a in base_args if a not in ("--agent", "--agent-file", "--environment")]
            built["args"] = ["-S", str(ref.get("session_id"))] + cleaned
            built["message_mode"] = "pty_input"
        elif engine == "agy":
            built["args"] = [f"--conversation={ref.get('conversation_id')}"] + base_args
            built["message_mode"] = "agy_interactive_flag"
        elif engine == "codex":
            built["args"] = ["resume", str(ref.get("session_id")), "-C", cwd] + base_args
            built["message_mode"] = "positional"
        elif engine == "claude":
            prof_cur = self.profiles.get(profile_id) or {}
            if prof_cur.get("command"):
                built["command"] = os.path.expandvars(os.path.expanduser(str(prof_cur["command"]).strip()))
            cfg_dir = str(prof_cur.get("config_dir") or snapshot.get("config_dir") or "").strip()
            if cfg_dir:
                built["env"]["CLAUDE_CONFIG_DIR"] = os.path.abspath(
                    os.path.expandvars(os.path.expanduser(cfg_dir))
                )
            built["args"] = ["--resume", str(ref.get("session_id"))] + base_args
            built["message_mode"] = "positional"
        elif engine == "dsh":
            built["args"] = []
            built["message_mode"] = "dsh_continue"
        else:
            raise ValueError(ENGINE_RESUME_BLOCK_REASONS.get(engine, DEFAULT_RESUME_BLOCK_REASON))
        return built

    @staticmethod
    def _resume_attach_offset(engine: str, ref: Dict[str, Any]) -> int:
        """Byte offset the resumed watcher starts at, so the old turns are not re-read (spec §4)."""
        path = ""
        if engine == "omp":
            path = str(ref.get("session_file") or "")
        elif engine == "kimi":
            sdir = str(ref.get("session_dir") or "")
            path = os.path.join(sdir, "agents", "main", "wire.jsonl") if sdir else ""
        elif engine == "agy":
            brain = str(ref.get("brain_dir") or "")
            path = os.path.join(brain, ".system_generated", "logs", "transcript.jsonl") if brain else ""
        elif engine == "codex":
            path = str(ref.get("rollout_path") or "")
        elif engine == "claude":
            path = str(ref.get("transcript_path") or "")
        try:
            return os.path.getsize(path) if path else 0
        except OSError:
            return 0

    async def _load_scrollback_text(self, session_id: str) -> str:
        blob = await self.db.get_scrollback_blob(session_id)
        if blob:
            try:
                return zlib.decompress(blob).decode("utf-8", "replace")
            except Exception:
                pass
        # A row written before this feature has only the plain text tail; better than an empty pane.
        rec = await self.db.get_session_record(session_id) or {}
        return str(rec.get("scrollback_text") or "")

    async def _load_record_for_resume(self, session_id: str) -> Optional[Dict[str, Any]]:
        rec = await self.db.get_session_record(session_id)
        sess = self.sessions.get(session_id)
        if sess is None:
            return rec
        live_rec = self._record_from_session(sess)
        if not rec:
            return live_rec
        for key in ("profile_snapshot", "conversation_ref", "last_task", "title", "kind", "cwd", "engine"):
            if not rec.get(key) and live_rec.get(key):
                rec[key] = live_rec[key]
        return rec

    async def _mark_resumable(self, session_id: str, ok: bool, reason: str = "") -> None:
        sess = self.sessions.get(session_id)
        if sess is not None:
            sess.resumable = ok
            sess.resume_block_reason = None if ok else reason
        await self.db.update_archive_fields(
            session_id,
            resumable=1 if ok else 0,
            resume_block_reason=None if ok else reason,
        )

    def _resume_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._resume_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._resume_locks[session_id] = lock
        return lock

    async def _make_resumed_session(
        self,
        session_id: str,
        record: Dict[str, Any],
        snapshot: Dict[str, Any],
        ref: Dict[str, Any],
        built: Dict[str, Any],
    ) -> AgentSession:
        engine = str(snapshot.get("engine") or record.get("engine") or "").strip().lower()
        profile_id = str(snapshot.get("profile_id") or record.get("profile_id") or "")
        prof = self.profiles.get(profile_id) or {}
        count = int(record.get("resume_count") or 0) + 1
        prof_name = str(record.get("profile_name") or prof.get("name") or profile_id)
        sess = AgentSession(
            session_id=session_id,
            profile_id=profile_id,
            profile_name=prof_name,
            kind=str(snapshot.get("kind") or record.get("kind") or "tui"),
            title=derive_readable_session_title(
                record.get("title"), record.get("last_task"), prof_name, session_id
            ),
            cwd=str(built.get("cwd") or snapshot.get("cwd") or record.get("cwd") or self.hub_default_cwd()),
            command=str(built.get("command") or snapshot.get("command") or ""),
            args=list(built.get("args") or []),
            gui_url=prof.get("gui_url"),
            cols=self.default_cols,
            rows=self.default_rows,
            on_exit_cb=self._exit_callback,
            on_tui_tool_cb=self._tool_callback,
        )
        sess.engine = engine
        sess.turn_id = str(record.get("turn_id") or sess.turn_id)
        sess.wait_handle = record.get("wait_handle")
        sess.effective_model = snapshot.get("model") or record.get("effective_model")
        sess.effective_thinking_level = snapshot.get("thinking_level") or record.get("effective_thinking_level")
        sess.last_task = str(record.get("last_task") or "")
        sess.step_count = int(record.get("step_count") or 0)
        sess.created_at = float(record.get("created_at") or time.time())
        sess.profile_snapshot = dict(snapshot)
        sess.profile_snapshot["args"] = strip_legacy_worker_bridge_args(
            list(snapshot.get("args") or []), self.runtime_configs_dir
        )
        # Round 2: the resume ran with the profile's current switch; record that, not the old one.
        sess.profile_snapshot[BYPASS_APPROVALS_KEY] = bool(built.get("bypass_approvals"))
        sess.profile_snapshot.update(resolve_session_approval_fields(built))
        sess.launch_notes = list(built.get("notes") or [])
        sess.conversation_ref = dict(ref)
        known_tp = (
            ref.get("transcript_path")
            or ref.get("session_file")
            or ref.get("rollout_path")
            or record.get("transcript_path")
        )
        if known_tp and os.path.isfile(str(known_tp)):
            sess.transcript_path = Path(str(known_tp))
        sess.resume_attach = True
        sess.attach_offset = int(built.get("attach_offset") or 0)
        sess.resume_count = count
        sess.last_resumed_at = time.time()
        old_live = self.sessions.get(session_id)
        if old_live is not None:
            if old_live.recent_injections:
                sess._recent_injections = list(old_live.recent_injections)
            if old_live.last_injection:
                sess.__dict__["_last_injection"] = dict(old_live.last_injection)
        # D2: the ended turn's answer comes back with the revived object, and begin_new_turn marks it
        # as from_previous_turn at the resume call site. It is kept only until the revived turn
        # writes its own; set_final_response lets that first source win whatever its rank. The
        # archived pane still replays the scrollback, so the old answer is visible there too.
        sess.final_response = record.get("final_response") or None
        sess.final_response_source = record.get("final_response_source") or None
        # Spec §5.3: the pane starts with the archived output, then the resume separator.
        archived = await self._load_scrollback_text(session_id)
        seed = f"{archived}{resume_banner(count)}" if archived else resume_banner(count)
        sess.raw_history = [seed]
        sess.raw_history_bytes = len(seed.encode("utf-8", "replace"))
        sess.raw_history_rev = 1
        if engine == "dsh":
            sess.dsh_spec = dict(snapshot.get("dsh_spec") or {})
            sess.sandbox_preparation = snapshot.get("sandbox_preparation")
            sess.dsh_job_id = ref.get("dsh_job_id")
            sess.dsh_session_id = ref.get("dsh_session_id")
        self._restore_parent_mailbox(session_id, record)
        return sess

    def _restore_parent_mailbox(self, session_id: str, record: Dict[str, Any]) -> None:
        """A11: both directions come back, or a second archive of the same record loses one side."""
        if session_id in self.parent_messages:
            return
        thread = record.get("parent_messages") or []
        restored = []
        worker_entries: List[Dict[str, Any]] = []
        for item in thread:
            if not isinstance(item, dict):
                continue
            if item.get("direction") == "parent":
                restored.append({
                    "id": item.get("message_id") or f"pmsg-{uuid.uuid4().hex[:8]}",
                    "session_id": session_id,
                    "timestamp": item.get("timestamp"),
                    "content": item.get("content", ""),
                    "resolved_question_id": item.get("resolved_question_id"),
                    "read_by_worker": True,
                })
            elif item.get("direction") == "worker":
                worker_entries.append({
                    "direction": "worker",
                    "timestamp": item.get("timestamp"),
                    "content": item.get("content", ""),
                    "type": item.get("type"),
                    "seq": item.get("seq"),
                })
        if restored:
            self.parent_messages[session_id] = restored
        if worker_entries:
            logs = getattr(self, "_worker_message_logs", None)
            if logs is None:
                logs = {}
                self._worker_message_logs = logs
            logs.setdefault(session_id, []).extend(worker_entries)

    async def resume_subagent(
        self,
        session_id: str,
        text: Optional[str] = None,
        auto_approval_rules: Any = None,
    ) -> Dict[str, Any]:
        """Revive an ended session under the same id (spec §5).

        Returns {"session", "resumed", "already_live", "delivered", "delivery", "injection"}:
        A2 needs the honest answer to "did this call really resume it, and did the text arrive",
        because a caller that lost the resume race must have its text delivered to the now-live
        session instead of being handed a canned `delivered: true`.
        Raises ValueError with the Chinese `resume_block_reason` when the record cannot be revived.
        """
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("找不到会话 ")
        normalized_rules = None if auto_approval_rules is None else normalize_auto_approval_rules(auto_approval_rules)
        lock = self._resume_lock(sid)
        async with lock:
            try:
                return await self._resume_locked(sid, text, normalized_rules)
            except ValueError:
                raise
            except PersistenceUnavailable:
                raise
            except Exception as e:
                # A9: a spawn OSError / a DB error / a non-JSON DSH reply is a resume failure with
                # a Chinese 400 detail, never a 500.
                raise ValueError(f"{RESUME_FAILED_PREFIX}{e}") from e

    async def _resume_locked(
        self,
        sid: str,
        text: Optional[str],
        auto_approval_rules: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        existing = self.sessions.get(sid)
        if existing is not None and existing.is_process_alive():
            if auto_approval_rules is not None:
                existing.profile_snapshot["auto_approval_rules"] = auto_approval_rules
                await self.persist_session(existing, final=True)
            # A2: another resume (or the session itself) won the race. The text still has to be
            # delivered — through the ordinary parent→child path, not the resume injection.
            delivery = None
            if text and str(text).strip():
                delivery = await self.deliver_parent_message(sid, str(text), also_write_pty=True)
            return {
                "session": existing,
                "resumed": False,
                "already_live": True,
                "delivered": bool(delivery and delivery.get("delivered")),
                "delivery": delivery,
                "injection": (delivery or {}).get("injection") or "none",
            }
        record = await self._load_record_for_resume(sid)
        if record is None:
            raise ValueError(f"找不到会话 {sid}")
        engine = str(record.get("engine") or "").strip().lower()
        ok, reason = self.compute_resumable(record)
        if ok and engine == "dsh":
            job_id = str((record.get("conversation_ref") or {}).get("dsh_job_id") or "")
            if not await self._dsh_job_exists(job_id):
                ok, reason = False, DSH_UNKNOWN_JOB_MESSAGE
        if not ok:
            await self._mark_resumable(sid, False, reason)
            raise ValueError(reason)
        if engine == "dsh":
            if auto_approval_rules is not None:
                record["profile_snapshot"] = dict(record.get("profile_snapshot") or {})
                record["profile_snapshot"]["auto_approval_rules"] = auto_approval_rules
            return await self._resume_dsh_session(sid, record, text)
        if auto_approval_rules is not None:
            record["profile_snapshot"] = dict(record.get("profile_snapshot") or {})
            record["profile_snapshot"]["auto_approval_rules"] = auto_approval_rules
        return await self._resume_process_session(sid, record, text)

    @staticmethod
    def _task_with_session_marker(session_id: str, task: str) -> str:
        return f"{task}\n\n[Hub Session: session_id={session_id}]"

    def _resume_task_text(self, session_id: str, text: str, engine: str) -> str:
        clean = str(text).replace("\r", " ").strip()
        return self._task_with_session_marker(session_id, clean)

    async def _resume_process_session(
        self, session_id: str, record: Dict[str, Any], text: Optional[str]
    ) -> AgentSession:
        loop = asyncio.get_running_loop()
        snapshot = record.get("profile_snapshot") or {}
        ref = record.get("conversation_ref") or {}
        engine = str(snapshot.get("engine") or record.get("engine") or "").strip().lower()
        built = self.build_resume_launch(record)
        built["attach_offset"] = self._resume_attach_offset(engine, ref)
        message = str(text) if text is not None and str(text).strip() else None
        prompt_for_tui: Optional[str] = None
        # A10: how the text really travels. "argv" is handed over at spawn, "pending" is typed after
        # the TUI is ready and only then has an outcome (last_injection); pty_written is never faked.
        injection = "none"
        if message:
            if built["message_mode"] == "positional":
                built["args"].append(self._resume_task_text(session_id, message, engine))
                injection = "argv"
            elif built["message_mode"] == "agy_interactive_flag":
                built["args"].extend(["-i", self._resume_task_text(session_id, message, engine)])
                injection = "argv"
            elif built["message_mode"] == "pty_input":
                prompt_for_tui = self._resume_task_text(session_id, message, engine)
                injection = "pending"

        sess = await self._make_resumed_session(session_id, record, snapshot, ref, built)
        if message and injection == "argv":
            sess.last_injection = {
                "at": round(time.time(), 3),
                "ok": True,
                "reason": "argv_positional",
                "attempts": 1,
                "mode": "resume_task",
                "label": "成功",
            }
        startup_delay = 2.5
        try:
            startup_delay = float(snapshot.get("startup_delay_sec") or 2.5)
        except (TypeError, ValueError):
            startup_delay = 2.5
        submit_key = str(snapshot.get("submit_key") or "\r")

        old = self.sessions.get(session_id)
        if old is not None and old is not sess:
            # A1: browser sockets opened on the old object keep streaming — the queue set is handed
            # over by identity, and start_pty pushes the separator to them before any new output.
            sess.adopt_ws_frontends(old)
        # A7: install the new object before the spawn. A resume that dies immediately then reports
        # its own exit (the callback carries the object), never the old dead object's.
        self.sessions[session_id] = sess
        resume_env = dict(built.get("env") or {})
        if engine == "claude":
            hook_log = self._claude_hook_log_path(sess)
            if hook_log is not None:
                resume_env.setdefault("HUB_CLAUDE_HOOK_LOG", str(hook_log))
                try:
                    sess._claude_hook_offset = hook_log.stat().st_size if hook_log.is_file() else 0
                except OSError:
                    sess._claude_hook_offset = 0
        try:
            # A9: a failed spawn (bad command, missing binary) must leave the archived record exactly
            # as it was — no zombie row, and the old ended object back in the live list.
            sess.start_pty(
                loop,
                env_overrides=resume_env or None,
                ws_prefix=resume_banner(sess.resume_count) if old is not None else None,
            )
        except Exception as e:
            # R9: a spawn that raised after the PTY had already started must not leave that process
            # behind while the old (archived) object is put back in the live list.
            if sess.pty is not None:
                try:
                    sess.pty.terminate(force=True)
                except Exception:
                    pass
            if old is not None:
                self.sessions[session_id] = old
            else:
                self.sessions.pop(session_id, None)
            raise ValueError(f"{RESUME_FAILED_PREFIX}{e}") from e
        if old is not None:
            self._release_transcripts(session_id)
        await self.db.reset_session_after_resume(session_id)
        # D2: the same shared helper the live follow-up uses marks the archived answer as the
        # previous turn's; it stays readable until the resumed turn writes its own.
        self.begin_new_turn(sess)
        await self.persist_session(sess)

        if engine == "codex":
            self._schedule(self._watch_codex_rollout(sess))
        elif engine == "omp":
            session_dir = Path(str(ref.get("session_dir") or self.runtime_configs_dir / f"omp_sess_{session_id}"))
            self._schedule(self._watch_omp_session(sess, session_dir))
        elif engine == "kimi":
            self._schedule(self._watch_kimi_session(sess))
        elif engine == "agy":
            self._schedule(self._watch_agy_transcript(sess))
        elif engine == "claude":
            self._schedule(self._watch_claude_session(sess))

        if sess.kind == "tui":
            self._schedule(self._watch_tui_interactive_prompts(sess))
            self._schedule(
                self._auto_handle_tui_startup_and_trust(
                    sess,
                    task_to_inject=prompt_for_tui,
                    startup_delay=startup_delay,
                    submit_key=submit_key,
                )
            )

        await self.record_worker_event(
            session_id,
            {
                "type": "resumed",
                "category": "lifecycle",
                "status": "resumed",
                "content": f"会话已恢复（第 {sess.resume_count} 次）",
            },
        )
        delivery = {
            "delivered": bool(message),
            "session_id": session_id,
            "resumed": True,
            "resume_count": sess.resume_count,
            "pty_written": False,
            "injection": injection,
            "note": "文本将在 TUI 就绪后输入，结果见 last_injection" if injection == "pending" else "",
        }
        return {
            "session": sess,
            "resumed": True,
            "already_live": False,
            "delivered": bool(message),
            "delivery": delivery,
            "injection": injection,
        }

    async def _dsh_job_exists(self, job_id: str) -> bool:
        """A8: only a real job object counts as "DSH still knows this job" — never a bare ok."""
        if not job_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}"), params={"wait": 0})
                if resp.status_code == 404:
                    return False
                data = resp.json()
        except PersistenceUnavailable:
            raise
        except Exception:
            return False
        if not resp.is_success or not isinstance(data, dict):
            return False
        job = data.get("job")
        return isinstance(job, dict) and bool(job.get("id"))

    @staticmethod
    def _dsh_continue_says_running(reason: str) -> bool:
        """R5: the /continue refusal that means "this job is running, steer it instead"."""
        text = str(reason or "").lower()
        return any(marker in text for marker in (
            "worker is running", "already running", "is running", "正在运行", "运行中",
        ))

    async def _dsh_post_message(
        self, job_id: str, token: str, text: str, timeout: float = 10.0
    ):
        """R5: posts text to a RUNNING dsh job through /messages (returns the response or None)."""
        clean = str(text or "").strip()
        if not job_id or not token or not clean:
            return None
        try:
            async with httpx.AsyncClient(timeout=max(1.0, float(timeout))) as client:
                return await client.post(
                    dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}/messages"),
                    headers={"x-dsh-crew-job-token": token},
                    json={"message": clean},
                )
        except PersistenceUnavailable:
            raise
        except Exception:
            return None

    async def _resume_dsh_session(
        self, session_id: str, record: Dict[str, Any], text: Optional[str]
    ) -> Dict[str, Any]:
        """DSH has no process: continue the recorded job, or only re-attach the virtual terminal (§4)."""
        loop = asyncio.get_running_loop()
        snapshot = record.get("profile_snapshot") or {}
        ref = dict(record.get("conversation_ref") or {})
        job_id = str(ref.get("dsh_job_id") or "")
        built = self.build_resume_launch(record)
        # R5: the job's own messaging token is kept in the conversation reference so the
        # /messages fallback (and a later steering message) still works after a hub restart.
        old_token = str(ref.get("dsh_token") or "")
        token = str(uuid.uuid4())
        clean = str(text or "").strip()
        new_job = ""
        cjob: Dict[str, Any] = {}
        jstatus: Optional[str] = None
        dsh_send_mode = "attach"

        preparation = await self._prepare_dsh_workspace(
            str(built.get("cwd") or snapshot.get("cwd") or record.get("cwd") or ""),
            repair=bool(clean),
        )
        if clean and not preparation["allowed"]:
            raise ValueError(preparation["reason"])

        # Talk to DSH first: a failed continue/re-attach must not replace the archived record.
        if clean:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}/continue"),
                        json={"text": clean, "messaging_token": token},
                    )
            except PersistenceUnavailable:
                raise
            except Exception as e:
                raise ValueError(f"DSH 恢复失败：{e}") from e
            try:
                data = resp.json()
            except Exception:
                raise ValueError("DSH 恢复失败：DSH 返回了非 JSON 内容") from None
            if not resp.is_success or not isinstance(data, dict) or not data.get("ok"):
                err = data.get("error") if isinstance(data, dict) else None
                err_text = str(err or f"HTTP {resp.status_code}")
                # R5: a running job refuses /continue ("worker is running"); a message reaches it.
                if old_token and self._dsh_continue_says_running(err_text):
                    fallback = await self._dsh_post_message(job_id, old_token, clean)
                    if fallback is None or not fallback.is_success:
                        raise ValueError(
                            f"DSH 恢复失败：{err_text}（改用 /messages 发送也失败）"
                        )
                    jstatus = "running"
                    new_job = job_id
                    dsh_send_mode = "message"
                else:
                    raise ValueError(f"DSH 恢复失败：{err_text}")
            else:
                cjob = data.get("job") or {}
                new_job = str(cjob.get("id") or "")
                if not new_job:
                    raise ValueError("DSH 恢复失败：continue 没有返回新的任务 ID")
                dsh_send_mode = "continue"
        else:
            # No text: nothing is sent, only the virtual terminal is re-attached (§4). A8: an
            # unreadable / failed / cancelled / unknown job fails the resume with a Chinese reason
            # instead of leaving a pseudo-live "starting" session behind.
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(
                        dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}"), params={"wait": 0}
                    )
            except PersistenceUnavailable:
                raise
            except Exception as e:
                raise ValueError(DSH_STATE_UNAVAILABLE_MESSAGE.format(reason=e)) from e
            if not resp.is_success:
                raise ValueError(DSH_STATE_UNAVAILABLE_MESSAGE.format(reason=f"HTTP {resp.status_code}"))
            try:
                data = resp.json()
            except Exception:
                raise ValueError(DSH_STATE_UNAVAILABLE_MESSAGE.format(reason="DSH 返回了非 JSON 内容")) from None
            jview = (data.get("job") if isinstance(data, dict) else None) or {}
            if not isinstance(jview, dict) or not jview.get("id"):
                raise ValueError(DSH_STATE_UNAVAILABLE_MESSAGE.format(reason="DSH 返回的任务信息无法识别"))
            jstatus = str(jview.get("status") or "")
            # R3: running / needs_input / every ended status re-attach; only a missing or
            # unreadable status refuses (A8's message).
            if jstatus not in ("running", "needs_input") + DSH_ENDED_JOB_STATUSES:
                raise ValueError(
                    DSH_STATE_UNAVAILABLE_MESSAGE.format(reason=f"任务状态为 {jstatus or '未知'}")
                )

        sess = await self._make_resumed_session(session_id, record, snapshot, ref, built)
        sess.sandbox_preparation = preparation
        sess.profile_snapshot["sandbox_preparation"] = preparation
        old = self.sessions.get(session_id)
        # A1: a dsh pane's open socket survives the object replacement too, and typed input has to
        # reach the virtual terminal — which needs the loop and the parent-delivery callback.
        sess._loop = loop
        if sess.on_virtual_line_cb is None:
            sess.on_virtual_line_cb = lambda sid, line: self._schedule(self.deliver_parent_message(
                sid, line, also_write_pty=False
            ))
        if old is not None and old is not sess:
            sess.adopt_ws_frontends(old)
            sess.push_ws_frame(resume_banner(sess.resume_count))
        self.sessions[session_id] = sess
        if old is not None:
            self._release_transcripts(session_id)
        await self.db.reset_session_after_resume(session_id)
        # D2: same shared helper as every other new turn (resume keeps the archived answer).
        self.begin_new_turn(sess)
        if clean:
            # R5: a /messages delivery keeps the job and its original token; a /continue delivery
            # gets the new job and the new token. Both are persisted with the conversation ref.
            active_job = new_job
            active_token = token if dsh_send_mode == "continue" else old_token
            sess.dsh_job_id = active_job
            sess.dsh_token = active_token
            sess.conversation_ref = _keep_dsh_record_seq(sess.conversation_ref, {
                "dsh_job_id": active_job,
                "dsh_session_id": sess.dsh_session_id or cjob.get("sessionId") or "",
                "dsh_token": active_token,
            })
            sess.status = "running"
            if dsh_send_mode == "message":
                sess.current_activity = f"Messaged running DSH job ({active_job}): {clean[:60]}"
                sess.emit_virtual_ansi(
                    "\r\n\x1b[1;36m➤ [Parent Steer Injected into running DSH job " + str(active_job) + "]: " + str(clean[:120]) + "\x1b[0m\r\n"
                )
            else:
                sess.current_activity = f"Continuing DSH session ({active_job}): {clean[:60]}"
        elif jstatus in ("running", "needs_input"):
            sess.dsh_token = old_token or token
            sess.status = "running"
            sess.current_activity = f"Re-attached DSH job {job_id}"
            if old_token:
                sess.conversation_ref = dict(sess.conversation_ref or {}, dsh_token=old_token)
        elif jstatus in DSH_ENDED_JOB_STATUSES:
            # R3: ended-but-live. The next message goes through /continue, exactly like the
            # send_to_subagent path that always accepted a done/failed/cancelled job.
            sess.dsh_token = old_token or ""
            sess.status = "completed"
            sess.current_activity = f"Re-attached ended DSH job {job_id} ({jstatus})"
            if old_token:
                sess.conversation_ref = dict(sess.conversation_ref or {}, dsh_token=old_token)
        await self.persist_session(sess)
        await self.record_worker_event(
            session_id,
            {
                "type": "resumed",
                "category": "lifecycle",
                "status": "resumed",
                "content": f"会话已恢复（第 {sess.resume_count} 次）",
            },
        )
        if clean:
            self._schedule(self._watch_dsh_hub_job(
                sess, new_job, sess.dsh_token or token
            ))
        elif jstatus in ("running", "needs_input"):
            self._schedule(self._watch_dsh_hub_job(sess, job_id, sess.dsh_token or token, reattach=True))
        injection = "dsh_continue" if clean else "dsh_attach"
        delivery = {
            "delivered": bool(clean),
            "session_id": session_id,
            "resumed": True,
            "resume_count": sess.resume_count,
            "pty_written": False,
            "injection": injection,
            "note": "" if clean else "仅重新挂接虚拟终端，没有发送文本",
        }
        return {
            "session": sess,
            "resumed": True,
            "already_live": False,
            "delivered": bool(clean),
            "delivery": delivery,
            "injection": injection,
        }

    async def list_archive(self, limit: int = 50, offset: int = 0, query: str = "") -> Dict[str, Any]:
        """The console's and the parent agent's archive view (spec §6).

        W1/R8: the page is refreshed from the same complete rows the single-record path uses, and
        all changed rows are written in one transaction instead of one commit per row.
        """
        rows = await self.db.list_archived(limit=limit, offset=offset, query=query)
        context = self._resume_check_context()
        def recheck():
            changed = []
            for rec in rows:
                if self._recheck_resumable(rec, context=context):
                    changed.append((str(rec.get("session_id") or ""), {
                        "resumable": 1 if rec.get("resumable") else 0,
                        "resume_block_reason": rec.get("resume_block_reason") or None,
                    }))
            return changed
        changed = await asyncio.to_thread(recheck)
        try:
            await self.db.update_archive_fields_bulk(changed)
        except PersistenceUnavailable:
            raise
        except Exception:
            # A failed refresh write must not take the archive page down; the next read retries.
            pass
        return {
            "sessions": [self._archive_summary(rec) for rec in rows],
            "total": await self.db.count_archived(query),
            "limit": max(1, min(int(limit or 50), 200)),
            "offset": max(0, int(offset or 0)),
        }

    def _recheck_resumable(self, rec: Dict[str, Any], *, context=None) -> bool:
        """A5/W1: re-checks the cheap filesystem facts and updates the record in place.

        Returns True when the stored verdict must be written. A record that never selected the
        snapshot and the conversation reference (a partial list row, or an older caller) is left
        exactly as stored: a "not resumable" verdict computed from missing columns is a lie that
        W1 showed will be persisted for every row.
        """
        if "profile_snapshot" not in rec or "conversation_ref" not in rec:
            return False
        end_reason = str(rec.get("end_reason") or "")
        if not rec.get("archived_at") and not end_reason:
            return False
        ok, reason = self.compute_resumable(rec, context=context)
        stored_ok = bool(rec.get("resumable"))
        stored_reason = str(rec.get("resume_block_reason") or "")
        rec["resumable"] = ok
        rec["resume_block_reason"] = None if ok else reason
        return stored_ok != ok or stored_reason != ("" if ok else reason)

    async def _refresh_resumable(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """A5: the single-record archive endpoint re-checks and persists a change."""
        session_id = str(rec.get("session_id") or "")
        if self._recheck_resumable(rec):
            try:
                await self.db.update_archive_fields(
                    session_id,
                    resumable=1 if rec.get("resumable") else 0,
                    resume_block_reason=rec.get("resume_block_reason") or None,
                )
            except PersistenceUnavailable:
                raise
            except Exception:
                pass
        return rec

    @staticmethod
    def _decoded_conversation_ref(rec: Dict[str, Any]) -> Dict[str, Any]:
        """V5: the archive API returns the engine conversation reference the record really holds."""
        ref = rec.get("conversation_ref")
        if isinstance(ref, str):
            try:
                ref = json.loads(ref) if ref else None
            except Exception:
                ref = None
        return ref if isinstance(ref, dict) else {}

    def _archive_summary(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        model = " · ".join([x for x in (rec.get("effective_model"), rec.get("effective_thinking_level")) if x])
        status = rec.get("status")
        end_reason = rec.get("end_reason")
        # V5: an archived record must never report a live status; its end state is what happened.
        if (rec.get("archived_at") or end_reason) and str(status or "") in LIVE_STATUSES:
            status = end_reason or status
        return {
            "session_id": rec.get("session_id"),
            "profile_id": rec.get("profile_id"),
            "profile_name": rec.get("profile_name"),
            "engine": rec.get("engine"),
            "kind": rec.get("kind"),
            "title": rec.get("title"),
            "cwd": rec.get("cwd"),
            "last_task": rec.get("last_task") or "",
            "task_first_line": self._first_line_text(rec.get("last_task")),
            "effective_model": rec.get("effective_model"),
            "effective_thinking_level": rec.get("effective_thinking_level"),
            "model_line": model,
            "status": status,
            "end_reason": end_reason,
            "end_reason_label": END_REASON_LABELS.get(str(end_reason or ""), ""),
            "ended_at": rec.get("ended_at"),
            "archived_at": rec.get("archived_at"),
            "created_at": rec.get("created_at"),
            "exit_code": rec.get("exit_code"),
            "step_count": rec.get("step_count") or 0,
            "resume_count": rec.get("resume_count") or 0,
            "last_resumed_at": rec.get("last_resumed_at"),
            "conversation_ref": self._decoded_conversation_ref(rec),
            "resumable": bool(rec.get("resumable")),
            "resume_block_reason": rec.get("resume_block_reason") or "",
            "archived": bool(rec.get("archived_at")),
            "has_final_response": bool(rec.get("has_final_response") or rec.get("final_response")),
        }

    async def get_archive_record(self, session_id: str) -> Optional[Dict[str, Any]]:
        rec = await self.db.get_session_record(session_id)
        if not rec:
            return None
        return self._archive_summary(await self._refresh_resumable(rec))

    async def get_archive_scrollback(self, session_id: str) -> Optional[str]:
        rec = await self.db.get_session_record(session_id)
        if not rec:
            return None
        return await self._load_scrollback_text(session_id)

    async def delete_archive_record(self, session_id: str) -> bool:
        """Deletes the hub's archived row only; the engine's own conversation files stay (§6)."""
        rec = await self.db.get_session_record(session_id)
        if not rec:
            raise ValueError(f"找不到会话 {session_id}")
        if session_id in self.sessions:
            raise ValueError("会话仍在列表中，请先移除再删除归档记录")
        if not rec.get("archived_at"):
            raise ValueError("会话仍在列表中，请先移除再删除归档记录")
        deleted = await self.db.delete_session_record(session_id)
        return deleted

    async def _scrollback_flush_loop(self) -> None:
        """Write each live session's scrollback once it is 60 s old (spec §1.6/§3).

        The tick is short and the 60 s throttle lives in _scrollback_blob, so the first write lands
        a few seconds after the session's first minute instead of drifting one further minute.
        """
        while not self._shutting_down:
            await asyncio.sleep(5.0)
            try:
                now = time.time()
                for sess in list(self.sessions.values()):
                    if not sess.is_process_alive():
                        continue
                    if now - sess._scrollback_written_at < RESUME_SCROLLBACK_INTERVAL_SEC:
                        continue
                    await self.persist_session(sess)
            except PersistenceUnavailable:
                logger.warning("Scrollback persistence delayed; will retry")

    async def _normalize_archive_on_startup(self) -> None:
        """Spec §1.5/§5: a stopped hub's live rows become interrupted + archived at the next start."""
        now = time.time()
        status_to_reason = {
            "exited": "exited", "crashed": "crashed", "terminated": "terminated",
            "completed": "exited", "interrupted": "interrupted",
        }
        for sid in await self.db.list_session_ids():
            rec = await self.db.get_session_record(sid)
            if not rec:
                continue
            if rec.get("archived_at") and rec.get("resumable") is not None:
                continue
            fields: Dict[str, Any] = {}
            status = str(rec.get("status") or "")
            if not rec.get("archived_at"):
                fields["archived_at"] = now
                fields["ended_at"] = rec.get("ended_at") or now
                if status in LIVE_STATUSES:
                    fields["status"] = "interrupted"
                    fields["end_reason"] = "interrupted"
                    fields["current_activity"] = "Hub 停止时会话仍在运行（已中断）"
                else:
                    fields["end_reason"] = rec.get("end_reason") or status_to_reason.get(status, "crashed")
                if not rec.get("last_screen"):
                    lines = [ln for ln in str(rec.get("scrollback_text") or "").splitlines() if ln.strip()]
                    fields["last_screen"] = "\n".join(lines[-20:])
            merged = dict(rec)
            merged.update(fields)
            ok, reason = self.compute_resumable(merged)
            fields["resumable"] = 1 if ok else 0
            fields["resume_block_reason"] = None if ok else reason
            await self.db.update_archive_fields(sid, **fields)

    async def close_session(self, session_id: str, source: str = "console") -> Dict[str, Any]:
        async with self._resume_lock(session_id):
            return await self._close_session(session_id, source)

    async def _close_session(self, session_id: str, source: str) -> Dict[str, Any]:
        """B9: removes a session from the live list without rewriting the status an ended process already earned.

        A session whose process was alive is terminated and emits a `terminated` worker event; one
        that had already ended keeps its own final status and activity. Either way the session leaves
        the live list as an archived record (spec §5).
        K5: `source` says who did it -- "console" (the 总控台 pane) or "mcp" (the parent agent through
        terminate_subagent), because the event text used to claim the user did it even when the parent
        had. "auto_cleanup" identifies the Hub's completed-session timeout.
        Returns {"found": bool, "was_alive": bool, "status": str}.
        """
        sess = self.sessions.get(session_id)
        if not sess:
            return {"found": False, "was_alive": False, "status": None}
        was_alive = sess.is_process_alive()
        terminated = sess.terminate()
        if terminated:
            sess.end_reason = "terminated"
        if source == "auto_cleanup":
            sess.current_activity = TERMINATED_MESSAGES["auto_cleanup"]
        await self._archive_session(sess)
        if self.sessions.get(session_id) is sess:
            del self.sessions[session_id]
        # W7: the pane the console is about to remove still owns a socket; end it normally instead
        # of leaving the server side open and silent for the rest of the hub's life.
        sess.close_ws_frontends()
        if terminated:
            self._user_terminated.add(session_id)
        self._release_transcripts(session_id)
        if terminated or (was_alive and sess.status == "terminated") or source == "auto_cleanup":
            await self.record_worker_event(
                session_id,
                {
                    "type": "terminated" if was_alive else "closed",
                    "category": "lifecycle",
                    "status": sess.status,
                    "content": TERMINATED_MESSAGES.get(
                        str(source or "").lower(),
                        TERMINATED_MESSAGES["console"],
                    ),
                },
            )
        return {"found": True, "was_alive": bool(was_alive), "status": sess.status}

    @staticmethod
    def _cancel_dsh_job(job_id: str, timeout: float = SHUTDOWN_JOB_CANCEL_TIMEOUT_SEC) -> bool:
        """R5: stops a live DSH job at hub shutdown so a restart can /continue it, not meet /cancel."""
        if not job_id:
            return False
        try:
            resp = httpx.post(
                dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}/cancel"),
                timeout=max(0.2, float(timeout)),
            )
            return bool(resp.is_success)
        except Exception:
            return False

    @staticmethod
    def _terminate_pty_tree(sess: AgentSession, budget: float = 2.0) -> None:
        """R6: kills a live PTY's process tree (not just the root) within a bounded time.

        A Windows PTY's children (MCP servers, shells) survive a plain terminate of the root; a
        surviving kimi keeps its per-session lock, which makes the archived row refuse to resume.
        The taskkill is only used for a real winpty process, never in tests with a fake PID.
        """
        pty = sess.pty
        if pty is None:
            return
        pid = getattr(pty, "pid", None)
        if pid and isinstance(pty, REAL_PTY_PROCESS):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=max(0.2, min(2.0, float(budget))),
                )
            except Exception:
                pass
        try:
            pty.terminate(force=True)
        except Exception:
            pass

    async def archive_all_sessions_on_shutdown(self) -> int:
        """Share one shutdown operation between the HTTP stop and lifespan exit."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())
        return await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> int:
        self._shutting_down = True
        self.db.stop_accepting()
        deadline = time.monotonic() + SHUTDOWN_MAX_SEC
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=0.3)
            except asyncio.TimeoutError:
                logger.error("Timed out cancelling hub producers")
        sessions = list(self.sessions.values())
        reasons = {}
        stops = []
        for sess in sessions:
            if sess.is_process_alive():
                reasons[sess.session_id] = "interrupted"
                sess.status = "interrupted"
                sess.current_activity = "Hub 停止时会话仍在运行（已中断）"
                if sess.dsh_job_id:
                    stops.append(asyncio.to_thread(self._cancel_dsh_job, str(sess.dsh_job_id), timeout=2.0))
                elif sess.pty is not None:
                    stops.append(asyncio.to_thread(self._terminate_pty_tree, sess, budget=2.0))
            else:
                reasons[sess.session_id] = sess.end_reason or sess.classify_end_reason(sess.exit_code)
        if stops:
            try:
                await asyncio.wait_for(asyncio.gather(*stops, return_exceptions=True), timeout=2.0)
            except asyncio.TimeoutError:
                logger.error("Timed out stopping session processes")
        archived = 0
        try:
            async with asyncio.timeout(max(0.001, deadline - time.monotonic() - 0.25)):
                for sess in sessions:
                    await self.db.run_internal(self._archive_session(sess, end_reason=reasons[sess.session_id]))
                    sess.close_ws_frontends()
                    archived += 1
        except (asyncio.TimeoutError, PersistenceUnavailable):
            logger.exception("Shutdown archived only %d/%d sessions", archived, len(sessions))
        finally:
            self.sessions.clear()
            with self._transcript_lock:
                self._claimed_transcripts.clear()
            self._notify_waiters()
            # Parent waiters receive one durable terminal signal and then reconnect to the same
            # opaque URL after the replacement Hub has restored the inbox.
            try:
                await self.wait_inbox.publish_shutdown()
                await self.wait_inbox.close()
            except Exception:
                logger.exception("Could not publish Hub shutdown to wait inbox")
            for entry in self.pending_questions.values():
                event = entry.get("event")
                if isinstance(event, asyncio.Event):
                    event.set()
            try:
                await self.db.close(timeout=max(0.001, deadline - time.monotonic()))
            except PersistenceUnavailable:
                logger.exception("Shutdown could not flush every accepted write")
        return archived

    async def record_reasoning_parts(self, session_id: str, source: str, thoughts: Iterable[Any]) -> None:
        """Thinking from a native record: a console-only reasoning entry, never an intermediate message.

        Joined into the reply text it reached parent agents as progress, showed in the transcript as
        an ordinary message and hid the final reply's duplicate from the transcript's de-duplication.
        """
        for thought in thoughts:
            text = str(thought or "").strip()
            if text:
                await self.record_telemetry(session_id, {
                    "source": source, "hook_event": "AssistantReasoning", "reasoning": text,
                })

    @logical_write
    async def record_telemetry(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Processes telemetry from Claude Hooks, Codex JSONL Tailer, native engine events, or TUI Heuristic Parser."""
        sess = self.sessions.get(session_id)
        if sess and data.get("transcript_path"):
            sess.transcript_path = Path(str(data["transcript_path"]))
        hook_event = data.get("hook_event", "")
        source = data.get("source", "telemetry")
        tool_name = data.get("tool_name")
        tool_input = data.get("tool_input")
        tool_response = data.get("tool_response")
        step_title = data.get("step_title")
        intermediate_list = data.get("intermediate_messages") or []
        final_text = data.get("final_assistant_text")

        input_str = ""
        if isinstance(tool_input, (dict, list)):
            input_str = json.dumps(tool_input, ensure_ascii=False)[:600]
        elif tool_input is not None:
            input_str = str(tool_input)[:600]

        resp_str = ""
        if isinstance(tool_response, (dict, list)):
            resp_str = json.dumps(tool_response, ensure_ascii=False)[:600]
        elif tool_response is not None:
            resp_str = str(tool_response)[:600]

        # The transcript feed shows a tool's arguments and output; the 600-character summaries
        # above stay as they were for status/trace consumers.
        call_id = str(data.get("call_id") or "").strip()
        if isinstance(tool_input, (dict, list)):
            input_detail = json.dumps(tool_input, ensure_ascii=False, indent=2)
        else:
            input_detail = "" if tool_input is None else str(tool_input)
        output_detail = tool_output_text(tool_response) if tool_response is not None else ""
        tool_data: Dict[str, Any] = {"tool": tool_name or "", "call_id": call_id}
        if hook_event == "PostToolUse":
            tool_data["output"] = _clip_detail(output_detail)
            if isinstance(data.get("tool_ok"), bool):
                tool_data["ok"] = data["tool_ok"]
        else:
            tool_data["input"] = _clip_detail(input_detail)
            if hook_event == "ReportStep":
                tool_data["step_marker"] = True  # a step counter (`llm_inference`), not a real tool
            if data.get("tool_title"):
                tool_data["title"] = str(data["tool_title"])[:300]

        step_num = sess.step_count if sess else 0

        if hook_event in ("PreToolUse", "ToolCall", "ReportStep", "TuiToolDetected"):
            if sess:
                if sess.status == "completed":
                    self.begin_new_turn(sess, status="running")
                elif sess.status == "starting":
                    sess.status = "running"
                sess.step_count += 1
                step_num = sess.step_count
                if step_title:
                    sess.current_activity = f"Step {step_num}: {step_title}"
                elif tool_name:
                    sess.current_activity = f"Step {step_num}: Executing tool [{tool_name}] {input_str[:80]}"
            if tool_name:
                await self.db.add_tool_call(
                    session_id=session_id,
                    step_num=step_num,
                    tool_name=tool_name,
                    input_summary=input_str or (step_title or ""),
                    status="completed" if hook_event != "PreToolUse" else "running",
                    source=source,
                )

        elif hook_event == "PostToolUse":
            await self.db.complete_latest_tool_call(session_id, tool_name, resp_str)
            if sess and tool_name:
                sess.current_activity = f"Step {sess.step_count}: Completed tool [{tool_name}]"

        for msg in intermediate_list:
            if msg and str(msg).strip():
                await self.db.add_intermediate_message(session_id, step_num, str(msg).strip(), source=source)

        if (tool_name and hook_event in ("PreToolUse", "ToolCall", "ReportStep", "TuiToolDetected")) or (
            hook_event == "PostToolUse" and (tool_name or tool_response is not None)
        ):
            # A result without a tool name (Claude, Kimi, AGY records) still closes its call in the
            # transcript; the feed pairs it by call id or call order.
            body = resp_str if hook_event == "PostToolUse" else input_str
            await self.record_worker_event(session_id, {
                "type": "tool_result" if hook_event == "PostToolUse" else "tool_call",
                "category": "progress",
                "status": "running",
                "content": f"{tool_name}: {body}" if tool_name else body,
                "data": tool_data,
            })
        elif hook_event == "AssistantReasoning" and str(data.get("reasoning") or "").strip():
            # Reasoning is console-only: it is not an intermediate message, so status/trace
            # readers (parent agents) do not receive it.
            await self.record_worker_event(session_id, {
                "type": "reasoning",
                "category": "progress",
                "status": "running",
                "content": _clip_detail(str(data["reasoning"]).strip()),
            })
        elif hook_event == "AssistantMessage" and intermediate_list:
            await self.record_worker_event(session_id, {
                "type": "progress",
                "category": "progress",
                "status": "running",
                "content": "\n".join(str(msg).strip() for msg in intermediate_list if msg and str(msg).strip()),
            })

        if hook_event in ("Stop", "SubagentStop", "TurnComplete") and final_text and str(final_text).strip():
            f_source = "native_transcript"
            if sess:
                if sess.step_count == 0:
                    sess.step_count = 1
                    step_num = 1
                sess.set_final_response(str(final_text).strip(), f_source)
                if hook_event in ("Stop", "SubagentStop", "TurnComplete"):
                    sess.current_activity = f"Step {sess.step_count}: Turn completed (final response captured)"

        if hook_event in ("Stop", "SubagentStop", "TurnComplete"):
            await self.record_worker_event(
                session_id,
                {
                    "type": "turn_completed",
                    "category": "turn_completed",
                    "status": "turn_idle",
                    "content": str(final_text or "").strip(),
                },
            )

        if sess:
            await self.persist_session(sess)

        return {"ok": True, "session_id": session_id, "step_count": step_num}

    async def _on_tui_tool_detected(self, session_id: str, tool_name: str, tool_arg: str):
        await self.record_telemetry(
            session_id,
            {
                "source": "tui_screen_parser",
                "hook_event": "TuiToolDetected",
                "tool_name": tool_name,
                "tool_input": tool_arg,
            },
        )

    @logical_write
    async def record_worker_event(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Persist an observed native/lifecycle event and wake parent listeners."""
        sess = self.sessions.get(session_id)
        ev_type = payload.get("type", "message")
        content = (payload.get("content") or "").strip()
        if sess:
            if ev_type == "turn_completed" and getattr(sess, "_turn_completion_emitted", False):
                if content:
                    sess.set_final_response(content, "turn_completed")
                await self.persist_session(sess)
                return {"session_id": session_id, "type": ev_type, "duplicate": True}
            if ev_type in ("question", "approval_request"):
                sess.status = "waiting_parent"
                prefix = "Waiting for parent approval" if ev_type == "approval_request" else "Waiting for parent answer"
                sess.current_activity = f"{prefix}: {content[:80]}"
            elif ev_type == "message" and content:
                await self.db.add_intermediate_message(session_id, sess.step_count, content, source="native_message")
            if ev_type == "turn_completed":
                if sess.step_count == 0:
                    sess.step_count = 1
                if content:
                    # Preserve the engine's native answer source when completion is also observed
                    # through a hook or another watcher.
                    sess.set_final_response(content, "turn_completed")
                if sess.status in ("starting", "running") and sess.pending_interaction is None:
                    sess.status = "completed"
                    if not str(sess.current_activity or "").startswith(
                        ("Task completed", "Turn completed", f"Step {sess.step_count}: Turn completed")
                    ):
                        sess.current_activity = f"Step {sess.step_count}: Turn completed (final response captured)"
            if ev_type == "turn_completed":
                # A native turn end makes a later process exit a clean one.
                sess.completion_reported = True
                sess._turn_completion_emitted = True

        record = {
            "session_id": session_id,
            "launch_source": sess.launch_source if sess else None,
            "timestamp": round(time.time(), 3),
            "type": ev_type,
            "category": payload.get("category", ev_type),
            "content": content,
            "status": payload.get("status", "info"),
            "files_changed": payload.get("files_changed", []),
            "request_id": payload.get("request_id"),
            "interaction": payload.get("interaction"),
            "turn_id": getattr(sess, "turn_id", None),
        }
        data = dict(payload["data"]) if isinstance(payload.get("data"), dict) else {}
        if isinstance(payload.get("interaction"), dict):
            # The persisted tail has no interaction column; keep its display fields (never the
            # engine metadata with tokens) so a restored transcript still shows the question.
            data["interaction"] = transcript_interaction_view(payload["interaction"])
        if data:
            record["data"] = data
        seq = await self.db.insert_worker_event(record)
        record["seq"] = seq
        if ev_type == "turn_completed":
            record["result_id"] = f"result:{session_id}:{record.get('turn_id') or seq}"
        # The parent inbox has its own durable cursor and only receives wake-worthy events.  Tool,
        # progress and ordinary delivery telemetry remains in worker_events for the console/audit.
        if ev_type in WAKE_EVENT_TYPES:
            await self.wait_inbox.publish_event(record)
        if sess:
            await self.persist_session(sess)
        def publish():
            self.global_seq = max(self.global_seq, seq)
            self.worker_events.append(record)
            if len(self.worker_events) > 2000:
                self.worker_events = self.worker_events[-2000:]
            self._remember_worker_message(session_id, record)
            self._notify_waiters()
        self.db.on_accept(publish)
        return record

    async def summarize_transcript(self, session_id: str) -> Dict[str, Any]:
        """
        Produces a structured, CLI-specific transcript summary (for omp, kimi, codex, agy, dsh, claude)
        so the Parent Agent never needs to read noisy raw TUI screen dumps.
        """
        sess = self.sessions.get(session_id)
        raw_fallback = ""
        if sess:
            meta = sess.to_dict()
            t_path = sess.transcript_path
            raw_fallback = sess.get_clean_screen_text(max_lines=30)
        else:
            persisted = await self.db.get_session_record(session_id)
            if not persisted:
                raise ValueError(f"Session '{session_id}' not found in live memory or SQLite history.")
            raw_fallback = persisted.pop("scrollback_text", "")
            meta = persisted
            tp_str = meta.get("transcript_path") or ""
            t_path = Path(tp_str) if tp_str else None

        tools = await self.db.get_recent_tool_calls(session_id, limit=20)
        inter_msgs = await self.db.get_recent_intermediate_messages(session_id, limit=15)

        summary = await asyncio.to_thread(self.summarizer_registry.summarize_session,
            profile_id=meta.get("profile_id", "generic"),
            transcript_path=t_path,
            session_meta=meta,
            db_tools=tools,
            db_messages=inter_msgs,
            raw_screen_fallback=raw_fallback,
        )

        # Only inactive sessions can acquire a derived conclusion. During a new turn the native
        # end record must replace the kept answer; a summary may still describe the previous turn.
        if sess:
            changed = False
            if int(summary.get("step_count") or 0) > sess.step_count:
                sess.step_count = int(summary["step_count"])
                changed = True
            if summary.get("latest_conclusion") and sess.status not in ("starting", "running", "waiting_parent") and not sess.final_response_from_previous_turn:
                sess.set_final_response(summary["latest_conclusion"], "transcript_summarizer")
                changed = True
            if changed:
                await self.persist_session(sess)

        return summary

    async def get_subagent_status_and_trace(
        self,
        session_id: str,
        recent_tools_limit: int = 15,
        recent_messages_limit: int = 10,
    ) -> Dict[str, Any]:
        """Queries full live OR post-lifecycle telemetry, tool execution trace, intermediate messages, and CLI-specific transcript summary."""
        sess = self.sessions.get(session_id)
        if sess:
            if sess.kind == "tui" and sess.status in LIVE_STATUSES:
                await self.poll_tui_screen_interactions_once(sess)
            await self.persist_session(sess)
            meta = sess.to_dict()
        else:
            persisted = await self.db.get_session_record(session_id)
            if not persisted:
                raise ValueError(f"Session '{session_id}' not found in live memory or SQLite history.")
            persisted.pop("scrollback_text", "")
            meta = persisted

        tools = await self.db.get_recent_tool_calls(session_id, limit=recent_tools_limit)
        inter_msgs = await self.db.get_recent_intermediate_messages(session_id, limit=recent_messages_limit)
        transcript_summary = await self.summarize_transcript(session_id)

        final_resp = meta.get("final_response")
        final_src = meta.get("final_response_source")
        if (not final_resp or final_src == "terminal_turn_snapshot") and (sess is None or sess.status not in ("starting", "running", "waiting_parent")) and transcript_summary.get("latest_conclusion"):
            final_resp = transcript_summary["latest_conclusion"]
            final_src = "transcript_summarizer"

        return {
            "session_id": session_id,
            "is_alive_in_memory": sess is not None and (
                sess.is_process_alive() or sess.status in ("starting", "running", "waiting_parent")
            ),
            "lifecycle_status": meta.get("status"),
            "profile_id": meta.get("profile_id"),
            "engine": meta.get("engine"),
            "title": meta.get("title"),
            "cwd": meta.get("cwd"),
            "step_count": max(meta.get("step_count", 0), transcript_summary.get("step_count", 0), len(tools)),
            "current_activity": meta.get("current_activity", ""),
            "last_task": meta.get("last_task", ""),
            "pending_question_id": meta.get("pending_question_id"),
            "pending_interaction": sess.pending_interaction if sess else meta.get("pending_interaction"),
            "recent_tool_calls": tools,
            "intermediate_messages": inter_msgs,
            "final_response": final_resp,
            "final_response_source": final_src,
            # D2: the answer is still the previous turn's while the flag says so.
            "from_previous_turn": bool(meta.get("from_previous_turn", False)),
            "files_changed": meta.get("files_changed", []),
            # A10: the real outcome of the startup/resume text, when there was one.
            "last_injection": meta.get("last_injection"),
            # K8 (fix 9d): the last few submit records (startup text, live messages), newest last.
            "recent_injections": meta.get("recent_injections") or [],
            # Round 2: false only when the profile's bypass_approvals was on for this launch;
            # null for engines without hub approval (dsh, pwsh).
            "high_risk_approval": (
                sess.high_risk_approval if sess else self._record_high_risk_approval(meta)
            ),
            **resolve_session_approval_fields(sess.profile_snapshot if sess else meta.get("profile_snapshot")),
            "sandbox_preparation": (
                sess.sandbox_preparation if sess else
                (meta.get("profile_snapshot") or {}).get("sandbox_preparation")
                if isinstance(meta.get("profile_snapshot"), dict) else None
            ),
            "transcript_summary": transcript_summary,
        }

    @staticmethod
    def _record_high_risk_approval(rec: Dict[str, Any]) -> Optional[bool]:
        """Round 2: high_risk_approval of a stored record (from its launch snapshot)."""
        snap = rec.get("profile_snapshot")
        if isinstance(snap, str):
            try:
                snap = json.loads(snap) if snap else {}
            except Exception:
                snap = {}
        snap = snap if isinstance(snap, dict) else {}
        return high_risk_approval_state(
            rec.get("engine") or snap.get("engine"), snap.get(BYPASS_APPROVALS_KEY) is True
        )

    async def get_subagent_final_response(self, session_id: str) -> Dict[str, Any]:
        """Retrieves the Sub-Agent's clean final response and transcript summary both during and after its lifecycle."""
        transcript_summary = await self.summarize_transcript(session_id)
        sess = self.sessions.get(session_id)
        if sess:
            if not sess.final_response and sess.status not in ("starting", "running", "waiting_parent") and transcript_summary.get("latest_conclusion"):
                sess.set_final_response(transcript_summary["latest_conclusion"], "transcript_summarizer")
            await self.persist_session(sess)

        rec = await self.db.get_session_record(session_id)
        if not rec:
            raise ValueError(f"Session '{session_id}' not found in live memory or SQLite history.")

        final_resp = rec.get("final_response")
        final_src = rec.get("final_response_source")
        if (not final_resp or final_src == "terminal_turn_snapshot") and (sess is None or sess.status not in ("starting", "running", "waiting_parent")) and transcript_summary.get("latest_conclusion"):
            final_resp = transcript_summary["latest_conclusion"]
            final_src = "transcript_summarizer"

        return {
            "session_id": session_id,
            "profile_id": rec.get("profile_id"),
            "lifecycle_status": rec.get("status"),
            "step_count": max(rec.get("step_count", 0), transcript_summary.get("step_count", 0)),
            "created_at": rec.get("created_at"),
            "ended_at": rec.get("ended_at"),
            "exit_code": rec.get("exit_code"),
            "final_response": final_resp,
            "final_response_source": final_src,
            # D2: a live session knows the answer above is the previous turn's; an archived record
            # has no live flag and shows the stored value exactly as before.
            "from_previous_turn": bool(
                sess is not None and getattr(sess, "final_response_from_previous_turn", False)
            ),
            "files_changed": rec.get("files_changed", []),
            "transcript_summary": transcript_summary,
        }

    @staticmethod
    def _auto_approval_argv_matches(argv: List[str], pattern: List[str]) -> bool:
        return len(argv) == len(pattern) and all(fnmatch.fnmatchcase(actual, wanted) for actual, wanted in zip(argv, pattern))

    def _auto_approval_rule_matches(self, session: AgentSession, interaction: Dict[str, Any], rule: Dict[str, Any]) -> bool:
        """Match only structured, simple, low-risk requests against a session rule."""
        metadata = interaction.get("metadata") or {}
        argv = metadata.get("argv")
        command = str(metadata.get("command") or metadata.get("raw_command") or "").strip()
        # Automatic approval is intentionally limited to requests with a structured argv.  A
        # display string is not sufficient evidence: splitting shell text would make wrappers,
        # quoting and redirections ambiguous and could turn a safe-looking rule into execution.
        if not isinstance(argv, list) or not argv \
                or any(not isinstance(item, str) for item in argv) \
                or AUTO_APPROVAL_SHELL_META_RE.search(command or " ".join(argv)) \
                or AUTO_APPROVAL_HARD_RISK_RE.search(command or " ".join(argv)):
            return False
        if any(bool(metadata.get(key)) for key in (
                "force_manual", "hard_deny", "requires_manual", "elevated", "credential_access",
                "write", "delete", "upload", "process_control")):
            return False
        match = rule.get("match") or {}
        tools = [str(item).lower() for item in (match.get("tool") or [])]
        tool_name = str(metadata.get("tool") or metadata.get("tool_name") or "").lower()
        if tools and tool_name not in tools:
            return False
        patterns = match.get("command_argv") or []
        if not patterns:
            return False
        if not any(self._auto_approval_argv_matches([str(item) for item in argv], pattern) for pattern in patterns):
            return False
        constraints = rule.get("constraints") or {}
        if bool(metadata.get("outside_workspace")):
            return False
        if constraints.get("network") is False and bool(metadata.get("network")):
            return False
        if constraints.get("shell_composition") is False and AUTO_APPROVAL_SHELL_META_RE.search(command):
            return False
        # Ordinary session rules never authorize workspace escapes.  The caller may include a
        # path even for a read-only tool; rejecting it here keeps an underspecified rule from
        # becoming an outside-workspace write grant.
        try:
            cwd = Path(session.cwd).resolve()
            requested_cwd = metadata.get("cwd")
            if requested_cwd and not Path(str(requested_cwd)).resolve().is_relative_to(cwd):
                return False
            requested_path = metadata.get("path") or metadata.get("destination")
            if requested_path and not Path(str(requested_path)).resolve().is_relative_to(cwd):
                return False
        except (OSError, ValueError):
            return False
        path_glob = str(match.get("path_glob") or "")
        if path_glob:
            path_value = str(metadata.get("path") or metadata.get("destination") or "")
            relative_value = ""
            if path_value:
                try:
                    relative_value = str(Path(path_value).resolve().relative_to(cwd)).replace("\\", "/")
                except (OSError, ValueError):
                    relative_value = ""
            if not path_value or not (
                    fnmatch.fnmatchcase(path_value, path_glob)
                    or fnmatch.fnmatchcase(relative_value, path_glob)):
                return False
        return True

    def _consume_auto_approval_rule(self, session: AgentSession, interaction: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        snapshot = session.profile_snapshot if isinstance(session.profile_snapshot, dict) else {}
        rules = snapshot.get("auto_approval_rules") or []
        now = time.time()
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("effect") != "approve_once":
                continue
            created = float(rule.get("_created_at") or now)
            ttl = int(rule.get("expires_in_seconds") or 0)
            uses = int(rule.get("_uses") or 0)
            if (ttl and now >= created + ttl) or uses >= int(rule.get("max_uses") or 1):
                continue
            if not self._auto_approval_rule_matches(session, interaction, rule):
                continue
            rule["_uses"] = uses + 1
            return {
                "rule_id": rule.get("id"),
                "decision": "approve_once",
                "remaining_uses": max(0, int(rule.get("max_uses") or 1) - rule["_uses"]),
                "expires_at": (created + ttl) if ttl else None,
            }
        return None

    @logical_write
    async def set_auto_approval_rules(self, session_id: str, rules: Any) -> Dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session '{session_id}' not found in active sessions.")
        normalized = normalize_auto_approval_rules(rules)
        session.profile_snapshot["auto_approval_rules"] = normalized
        await self.persist_session(session, final=True)
        return {"session_id": session_id, "auto_approval_rules": normalized}

    @logical_write
    async def register_pending_interaction(
        self,
        session: AgentSession,
        kind: str,
        title: str,
        prompt: str,
        options: List[str],
        source: str,
        request_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Registers a structured high-risk approval request (`kind="approval_request"`) or interactive
        multi-choice question (`kind="question"`) detected from Transcript JSONL or TUI screen,
        transitions the session to `waiting_parent`, and emits a sequenced event to wake the Parent Agent.
        """
        req_id = request_id or f"int-{uuid.uuid4().hex[:8]}"
        resolved = getattr(session, "_resolved_interactions", {})
        if req_id in resolved:
            return resolved[req_id]
        if session.pending_interaction and req_id in self._interaction_ids(session.pending_interaction):
            return session.pending_interaction
        from_screen = str(source or "").endswith("_screen_detector")
        if request_id and not from_screen:
            # D7: one prompt = one pending interaction. A machine record (kimi wire, agy transcript)
            # of a prompt the screen detector registered first adopts that registration.
            adopted = await self._adopt_screen_interaction(
                session, kind, req_id, title, prompt, source, dict(metadata or {})
            )
            if adopted is not None:
                return adopted

        interaction: Dict[str, Any] = {
            "request_id": req_id,
            "session_id": session.session_id,
            "engine": session.engine or self._infer_engine(session.profile_id, {}),
            "kind": kind,  # "approval_request" or "question"
            "title": title,
            "prompt": prompt,
            "options": list(options),
            "source": source,
            "created_at": round(time.time(), 3),
            "metadata": dict(metadata or {}),
        }
        if from_screen:
            interaction["origin"] = "screen"
            # D7: remembered until a machine record adopts it (also after it has been answered).
            session._unadopted_screen_interaction = interaction
        session.pending_interaction = interaction
        session.pending_question_id = req_id
        session.status = "waiting_parent"

        if req_id not in self.pending_questions:
            self.pending_questions[req_id] = {
                "request_id": req_id,
                "session_id": session.session_id,
                "question": prompt,
                "asked_at": time.time(),
                "answered": False,
                "answer": None,
                "event": asyncio.Event(),
                "interaction": interaction,
                **(metadata or {}),
            }
        else:
            self.pending_questions[req_id]["interaction"] = interaction

        auto_match = None
        if kind == "approval_request":
            auto_match = self._consume_auto_approval_rule(session, interaction)
        if auto_match is not None:
            interaction["auto_approval"] = auto_match
            try:
                action_result = await self.respond_to_interaction(
                    session.session_id,
                    decision="approve",
                    request_id=req_id,
                )
            except Exception:
                logger.exception("[%s] automatic approval failed for %s", session.session_id, req_id)
                for candidate in session.profile_snapshot.get("auto_approval_rules") or []:
                    if isinstance(candidate, dict) and candidate.get("id") == auto_match.get("rule_id"):
                        candidate["_uses"] = max(0, int(candidate.get("_uses") or 1) - 1)
                        break
                interaction.pop("auto_approval", None)
            else:
                await self.record_worker_event(
                    session.session_id,
                    {
                        "type": "approval_auto_resolved",
                        "category": "approval_auto_resolved",
                        "status": action_result.get("response_state") or "sent",
                        "decision": "approve_once",
                        "request_id": req_id,
                        "rule_id": auto_match["rule_id"],
                        "remaining_uses": auto_match.get("remaining_uses"),
                        "expires_at": auto_match.get("expires_at"),
                        "content": f"Automatic approval rule {auto_match['rule_id']} matched",
                        "interaction": interaction,
                    },
                )
                return interaction

        opts_lines = "\n".join(f"  [{i + 1}] {opt}" for i, opt in enumerate(options)) if options else "  (free-text reply)"
        ev_type = "approval_request" if kind == "approval_request" else "question"
        formatted_content = (
            f"{'Approval request' if kind == 'approval_request' else 'Interactive question'}\n"
            f"- Engine: `{interaction['engine']}` (`{source}`) | request_id=`{req_id}`\n"
            f"- Title: {title}\n"
            f"- Details: {prompt}\n"
            f"- Options:\n{opts_lines}\n"
            f"Use respond_to_subagent(session_id='{session.session_id}', "
            f"request_id='{req_id}') with decision='approve' / 'approve_session' / 'deny' for approvals, "
            "option_index=1..N for choices, or custom_input for free text. "
            "Free text alone denies an approval; approve_session may persist an engine allow rule."
        )
        await self.record_worker_event(
            session.session_id,
            {
                "type": ev_type,
                "category": "approval_waiting_reply" if kind == "approval_request" else "question_waiting_reply",
                "status": "waiting_parent",
                "request_id": req_id,
                "content": formatted_content,
                "interaction": interaction,
            },
        )
        return interaction

    @staticmethod
    def _interaction_ids(interaction: Optional[Dict[str, Any]]) -> Set[str]:
        """The request id of an interaction plus the ids it was known by before an adoption (D7)."""
        if not interaction:
            return set()
        ids = {str(i) for i in (interaction.get("aliases") or []) if i}
        if interaction.get("request_id"):
            ids.add(str(interaction["request_id"]))
        return ids

    async def _adopt_screen_interaction(
        self,
        session: AgentSession,
        kind: str,
        request_id: str,
        title: str,
        prompt: str,
        source: str,
        metadata: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """D7: a machine record of the prompt the screen detector already registered.

        Pending: the screen registration takes the record's id (the old id stays an alias, so an
        answer that quotes either id works) and no second event wakes the parent. Already answered
        within SCREEN_ADOPT_WINDOW_SEC: the record is that prompt, already handled; nothing is
        registered. Anything else (another kind, another command) is a new prompt: None.
        """
        cand = getattr(session, "_unadopted_screen_interaction", None)
        if not cand or cand.get("kind") != kind:
            return None
        record_cmd = _collapsed(metadata.get("command") or "")[:40]
        screen_text = _collapsed(
            f"{(cand.get('metadata') or {}).get('command') or ''} {cand.get('prompt') or ''}"
        )
        if record_cmd and record_cmd not in screen_text:
            return None
        pending = session.pending_interaction
        if pending is cand:
            old_id = str(cand.get("request_id") or "")
            entry = self.pending_questions.get(old_id)
            cand["aliases"] = [i for i in (list(cand.get("aliases") or []) + [old_id]) if i]
            cand["request_id"] = request_id
            cand["title"] = title or cand.get("title")
            cand["prompt"] = prompt or cand.get("prompt")
            cand["source"] = f"{source} (adopted {cand.get('source')})"
            cand["metadata"] = {**(cand.get("metadata") or {}), **metadata}
            cand.pop("origin", None)
            session.pending_question_id = request_id
            if entry is not None:
                entry["request_id"] = request_id
                entry["interaction"] = cand
                self.pending_questions[request_id] = entry
            session._unadopted_screen_interaction = None
            await self.persist_session(session)
            return cand
        if pending is None and (time.time() - float(cand.get("created_at") or 0)) <= SCREEN_ADOPT_WINDOW_SEC:
            # Answered before its machine record was read: the same prompt, already handled.
            session._unadopted_screen_interaction = None
            resolved = dict(cand, request_id=request_id, aliases=[cand.get("request_id")], already_resolved=True)
            remembered = getattr(session, "_resolved_interactions", {})
            remembered[request_id] = resolved
            while len(remembered) > 128:
                remembered.pop(next(iter(remembered)))
            session._resolved_interactions = remembered
            return resolved
        return None

    @logical_write
    async def clear_pending_interaction(self, session: AgentSession, request_id: Optional[str] = None):
        """Clears an active pending interaction when resolved via Parent response or native Transcript event."""
        if not session.pending_interaction:
            return
        ids = self._interaction_ids(session.pending_interaction)
        if request_id and request_id not in ids:
            return
        metadata = session.pending_interaction.get("metadata") or {}
        if metadata.get("response_state") in ("sent", "unconfirmed"):
            metadata["response_state"] = "confirmed"
        resolved = getattr(session, "_resolved_interactions", {})
        for qid in ids:
            resolved[qid] = session.pending_interaction
        while len(resolved) > 128:
            resolved.pop(next(iter(resolved)))
        session._resolved_interactions = resolved
        session.pending_interaction = None
        if session.pending_question_id in ids:
            session.pending_question_id = None
        for qid in ids:
            if qid in self.pending_questions:
                self.pending_questions[qid]["answered"] = True
                self.db.on_accept(self.pending_questions[qid]["event"].set)
        # D7: the answered prompt may still be drawn for a moment; the screen detector waits.
        session._prompt_quiet_until = time.time() + TUI_PROMPT_REDETECT_QUIET_SEC
        session._seen_tui_prompt_signatures.clear()
        if session.status == "waiting_parent":
            session.status = "running"
        await self.persist_session(session)

    @staticmethod
    def _resolve_approval_choice(
        dec_norm: str,
        option_index: Optional[int],
        custom_input: Optional[str],
        options: List[str],
        option_actions: Optional[List[str]] = None,
    ) -> tuple:
        """D7: (action, chosen label, option index) for an approval prompt of kimi / omp / codex / agy.

        action is approve_once | approve_session | deny | deny_feedback. An option picked by number
        or by its exact label acts as that option; free text without a decision denies the call and
        is sent as feedback (the engines' "type to deny and steer"). No decision at all is refused:
        a high-risk call is never approved by default. `option_actions` (aligned with `options`)
        is what the detector read for each option (codex: from its shortcut key); without it the
        label is classified.
        """
        feedback = (custom_input or "").strip()
        known = list(option_actions or [])
        if len(known) != len(options):
            known = []

        def action_of(idx: int) -> str:
            return str(known[idx]) if known else classify_approval_label(options[idx])

        def by_label(idx: int, with_feedback: bool) -> tuple:
            label = options[idx]
            action = action_of(idx)
            if action == "persist":
                # Never sent: a permanent rule is the owner's call, not a sub-agent approval.
                # Round 3: agy also has persisting deny options; those fall back to a plain deny.
                low = str(label or "").strip().lower()
                action = "deny" if low.startswith(("no", "deny", "reject")) else "approve_once"
            elif action == "deny_session":
                # Not offered (round 3); a plain deny if a caller names it anyway.
                action = "deny"
            if action == "deny" and with_feedback and feedback:
                action = "deny_feedback"
            if action == "deny_feedback" and not (with_feedback and feedback):
                action = "deny"
            return action, label, idx + 1

        def label_for(action: str) -> str:
            want = "deny" if action == "deny_feedback" else action
            for i, opt in enumerate(options):
                if action_of(i) == want:
                    return opt
            return action

        idx = option_index
        if idx is None and dec_norm.isdigit():
            idx = int(dec_norm)
        if idx is not None:
            if not 1 <= int(idx) <= len(options):
                raise ValueError(f"option_index 超出范围：可选 1..{len(options)}")
            return by_label(int(idx) - 1, True)
        if dec_norm:
            if dec_norm in APPROVAL_APPROVE_DECISIONS:
                action = "approve_once"
            elif dec_norm in APPROVAL_SESSION_DECISIONS:
                action = "approve_session"
            elif dec_norm in APPROVAL_DENY_DECISIONS:
                action = "deny_feedback" if feedback else "deny"
            else:
                raise ValueError(
                    f"无法识别的审批决定「{dec_norm}」：请用 approve / approve_session / deny，"
                    f"或 option_index / custom_input"
                )
            return action, label_for(action), None
        if feedback:
            low = feedback.lower()
            for i, opt in enumerate(options):
                if low == opt.strip().lower():
                    return by_label(i, False)
            as_decision = low.replace(" ", "_")
            if as_decision in APPROVAL_APPROVE_DECISIONS:
                return "approve_once", label_for("approve_once"), None
            if as_decision in APPROVAL_SESSION_DECISIONS:
                return "approve_session", label_for("approve_session"), None
            if as_decision in APPROVAL_DENY_DECISIONS:
                return "deny", label_for("deny"), None
            return "deny_feedback", label_for("deny_feedback"), None
        raise ValueError(
            "审批请求需要明确的决定：decision=approve / approve_session / deny，或 option_index，"
            "或 custom_input（自由文本＝拒绝并把文本作为反馈）"
        )

    @staticmethod
    def _write_keys_scheduled(sess: AgentSession, loop, steps: List[tuple]) -> None:
        """Writes (offset_sec, text) steps to the PTY: on the loop's timer, or in order with sleeps."""
        if loop is not None:
            for offset, text in steps:
                if offset <= 0:
                    sess.write_input(text)
                else:
                    loop.call_later(offset, sess.write_input, text)
            return
        elapsed = 0.0
        for offset, text in steps:
            if offset > elapsed:
                time.sleep(offset - elapsed)
                elapsed = offset
            sess.write_input(text)

    def _answer_omp_approval(
        self,
        sess: AgentSession,
        loop,
        inter: Dict[str, Any],
        meta: Dict[str, Any],
        action: str,
        custom_input: Optional[str],
    ) -> tuple:
        """D7: omp's tool approval box. Ctrl+Q approves once; the session row (only when it exists)
        is reached with arrow keys from the selected row, then Enter; Escape denies, once only (the
        owner's doubleEscapeAction is rewind); typed text + Enter denies and steers. omp ignores
        keys for 500 ms after the box opens, so nothing is sent before 0.6 s have passed."""
        feedback = _one_line(custom_input)
        created = float(inter.get("created_at") or 0.0)
        guard = max(0.0, OMP_PROMPT_KEY_GUARD_SEC - (time.time() - created)) if created else 0.0
        note = ""
        if meta.get("omp_prompt") == "settings":
            # The settings-change confirm (Allow once / Always for this session / Deny).
            keys = {"approve_once": [(0.0, "\r")], "approve_session": [(0.0, "\x1b[B"), (0.15, "\r")],
                    "deny": [(0.0, "\x1b")], "deny_feedback": [(0.0, "\x1b")]}[action]
            self._write_keys_scheduled(sess, loop, [(guard + off, k) for off, k in keys])
            if action == "deny_feedback" and feedback:
                note = "omp 设置变更确认不接受反馈文本：已拒绝，反馈请另用 send_to_subagent 发送"
            return f"OMP settings confirm {action} ({keys!r})", note
        if action == "approve_once":
            steps = [(0.0, "\x11")]
            trace = "OMP approval Ctrl+Q (Approve once)"
        elif action == "approve_session":
            screen_opts = list(meta.get("omp_options") or [])
            label = meta.get("session_label")
            if label and label in screen_opts:
                move = screen_opts.index(label) - int(meta.get("selected_index") or 0)
                arrows = ("\x1b[B" * move) if move >= 0 else ("\x1b[A" * -move)
                steps = [(0.0, arrows), (0.15, "\r")] if arrows else [(0.0, "\r")]
                trace = f"OMP approval {abs(move)}x{'Down' if move >= 0 else 'Up'} + Enter ({label})"
            else:
                steps = [(0.0, "\x11")]
                trace = "OMP approval Ctrl+Q (Approve once; no session row)"
                note = "这个 omp 审批框没有「本会话允许」选项（规则触发的提示只有 Approve / Deny）：本次只允许了一次"
        elif action == "deny_feedback":
            steps = [(0.0, feedback), (0.4, "\r")]
            trace = f"OMP approval typed feedback {feedback!r} + Enter (Deny and send message)"
        else:
            steps = [(0.0, "\x1b")]
            trace = "OMP approval Escape (Deny)"
        self._write_keys_scheduled(sess, loop, [(guard + off, k) for off, k in steps])
        return trace, note

    def _answer_agy_approval(
        self,
        sess: AgentSession,
        loop,
        meta: Dict[str, Any],
        action: str,
        chosen_label: str,
        custom_input: Optional[str],
    ) -> tuple:
        """D7: agy picks an option with its digit at once (no Enter). The key is looked up by label
        among the options read from the screen; the option that persists to settings.json is never
        sent. Deny with feedback = deny, then the feedback is typed as the next message."""
        feedback = (custom_input or "").strip()
        opts = [o for o in (meta.get("agy_options") or []) if isinstance(o, dict)]
        if not opts:
            # A registration without the screen's options: the layout captured on 2026-11-07.
            opts = [
                {"key": "1", "label": "Yes, run command", "action": "approve_once"},
                {"key": "2", "label": "Yes, and always allow in this conversation", "action": "approve_session"},
                {"key": "4", "label": "No, cancel", "action": "deny"},
            ]
        # Never sent: an option that persists to settings.json (allow or deny), nor the
        # conversation-wide deny rule (round 3: the 6-option MCP tool prompt has both).
        sendable = [o for o in opts if o.get("action") not in ("persist", "deny_session")]
        want = "deny" if action == "deny_feedback" else action
        chosen = next((o for o in sendable if o.get("label") == chosen_label and o.get("action") == want), None)
        if chosen is None:
            chosen = next((o for o in sendable if o.get("action") == want), None)
        note = ""
        if chosen is None and want == "approve_session":
            chosen = next((o for o in sendable if o.get("action") == "approve_once"), None)
            note = "这个 agy 提示没有「本对话允许」选项：本次只允许了一次"
        if chosen is None and want == "deny":
            chosen = {"key": "\x1b", "label": "esc to cancel", "action": "deny"}
        if chosen is None:
            raise ValueError("agy 审批提示里没有可用的允许选项，未发送任何按键")
        sess.write_input(str(chosen["key"]))
        trace = f"AGY permission prompt key {chosen['key']!r} ({chosen.get('label')})"
        if action == "deny_feedback" and feedback:
            trace += f" + feedback as next message {feedback!r}"
        return trace, note

    def _answer_claude_approval(
        self,
        sess: AgentSession,
        loop,
        meta: Dict[str, Any],
        action: str,
        chosen_label: str,
        custom_input: Optional[str],
    ) -> tuple:
        """Claude Code permission prompt: digits select an option at once; Escape cancels."""
        feedback = (custom_input or "").strip()
        opts = [o for o in (meta.get("claude_options") or []) if isinstance(o, dict)]
        if not opts:
            opts = [
                {"key": "1", "label": "Yes", "action": "approve_once"},
                {"key": "2", "label": "Yes, allow for this session", "action": "approve_session"},
                {"key": "3", "label": "No, and tell Claude what to do differently (esc)", "action": "deny"},
            ]
        sendable = [o for o in opts if o.get("action") not in ("persist", "deny_session")]
        want = "deny" if action == "deny_feedback" else action
        chosen = next((o for o in sendable if o.get("label") == chosen_label and o.get("action") == want), None)
        if chosen is None:
            chosen = next((o for o in sendable if o.get("action") == want), None)
        note = ""
        downgraded = False
        if chosen is None and want == "approve_session":
            chosen = next((o for o in sendable if o.get("action") == "approve_once"), None)
            downgraded = True
            note = "这个 claude 提示没有「本会话允许/不再询问」选项（由高风险审批规则触发的提示只有 Yes / No）：本次只允许了一次"
        elif chosen is not None and want == "approve_session" and chosen.get("persists"):
            note = f"已发送 {str(chosen['key'])!r}（不再询问该命令）：claude 已将该放行规则写入项目 .claude/settings.local.json"
        if chosen is None and want == "deny":
            chosen = {"key": "\x1b", "label": "esc to cancel", "action": "deny"}
        if chosen is None:
            raise ValueError("claude 审批提示里没有可用的允许选项，未发送任何按键")
        key_to_send = "\x1b" if action == "deny_feedback" else str(chosen["key"])
        sess.write_input(key_to_send)
        trace = f"Claude permission prompt key {key_to_send!r} ({chosen.get('label')})"
        if action == "deny_feedback" and feedback:
            trace += f" + feedback as next message {feedback!r}"
        return trace, note, downgraded

    async def _deliver_deny_feedback(self, session_id: str, text: str) -> None:
        """D7 (agy): the feedback of a deny, typed as the next message once the prompt has closed.

        A new interaction pending by then would take the text as its own answer (a deny), so the
        feedback is dropped instead, and the drop is recorded."""
        sess = self.sessions.get(session_id)
        if sess is None or not sess.is_process_alive():
            return
        if sess.pending_interaction is not None:
            logger.warning("[%s] deny feedback not typed: a new interaction is pending", session_id)
            await self.db.add_intermediate_message(
                session_id, sess.step_count,
                f"[Parent deny feedback NOT delivered: a new interaction was pending] {text}",
                source="parent_approval_proxy",
            )
            return
        await self.deliver_parent_message(session_id, text, also_write_pty=True)

    @logical_write
    async def respond_to_interaction(
        self,
        session_id: str,
        decision: Optional[str] = None,
        option_index: Optional[int] = None,
        custom_input: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes the Parent Agent's approval decision (`approve`, `approve_session`, `deny`) or
        multi-choice selection (`option_index` 1-based, or `custom_input`) using the exact protocol
        verified for each CLI engine (`dsh`, `kimi`, `omp`, `codex`, `agy`).
        """
        sess = self.sessions.get(session_id)
        if not sess:
            raise ValueError(f"Active session '{session_id}' not found.")

        # request_id is the idempotency key for approvals/questions.  A retried deny or answer
        # returns the original compact outcome and never writes another native key.
        if request_id and request_id in getattr(sess, "_responded_request_ids", {}):
            return dict(sess._responded_request_ids[request_id], idempotent=True)

        eng = (sess.engine or self._infer_engine(sess.profile_id, {})).lower()
        await self.poll_tui_screen_interactions_once(sess)
        inter = sess.pending_interaction or {}
        if not inter:
            raise ValueError("没有待处理的审批或选择请求；未发送按键")
        if request_id and request_id not in self._interaction_ids(inter):
            raise ValueError("request_id 已过期或不匹配当前请求；未发送按键")
        if (inter.get("metadata") or {}).get("response_state") in ("sent", "unconfirmed"):
            raise ValueError("该请求已发送回应，正在等待原生确认；不会重复发送按键")
        target_qid = request_id or inter.get("request_id") or sess.pending_question_id
        q_entry = self.pending_questions.get(target_qid) if target_qid else None
        if not inter and q_entry and q_entry.get("interaction"):
            inter = q_entry["interaction"]

        kind = inter.get("kind") or "approval_request"
        options: List[str] = list(inter.get("options") or [])
        meta: Dict[str, Any] = dict(inter.get("metadata") or (q_entry or {}))

        dec_norm = (decision or "").strip().lower().replace(" ", "_")
        chosen_idx_1based: Optional[int] = option_index
        chosen_label: str = ""
        # D7: an approval prompt of a TUI engine is answered by what the choice does (approve once,
        # approve for the session, deny, deny with feedback), mapped to that engine's own keys.
        approval_action: Optional[str] = None
        requested_approval_action: Optional[str] = None
        notes: List[str] = []
        if kind == "approval_request" and eng in ("kimi", "omp", "codex", "claude", "agy") and not sess.dsh_spec:
            approval_action, chosen_label, chosen_idx_1based = self._resolve_approval_choice(
                dec_norm, option_index, custom_input, options, meta.get("option_actions")
            )
            requested_approval_action = approval_action

        # Map semantic decision or custom_input matching onto 1-based option_index
        if approval_action is not None:
            pass
        elif chosen_idx_1based is None:
            if dec_norm in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
                chosen_idx_1based = int(dec_norm)
            elif custom_input and options:
                ci_clean = custom_input.strip().lower()
                for idx, opt_txt in enumerate(options):
                    if ci_clean == opt_txt.strip().lower() or ci_clean in opt_txt.strip().lower():
                        chosen_idx_1based = idx + 1
                        break

        if approval_action is None and chosen_idx_1based is None and dec_norm:
            if dec_norm in ("approve", "allow", "allow_once", "yes", "y", "ok", "accept"):
                chosen_idx_1based = 1
            elif dec_norm in ("approve_session", "allow_session", "always", "a", "session"):
                chosen_idx_1based = 2 if len(options) >= 2 else 1
            elif dec_norm in ("deny", "reject", "no", "n", "cancel"):
                # Find option containing Reject / Deny / No, otherwise 3 or last
                deny_idx = None
                for idx, opt_txt in enumerate(options):
                    if any(w in opt_txt.lower() for w in ("reject", "deny", "no,", "no ")):
                        deny_idx = idx + 1
                        break
                chosen_idx_1based = deny_idx or (3 if len(options) >= 3 else max(1, len(options)))

        if approval_action is not None:
            pass
        elif chosen_idx_1based is not None and 1 <= chosen_idx_1based <= len(options):
            chosen_label = options[chosen_idx_1based - 1]
        elif custom_input:
            chosen_label = custom_input.strip()
        elif dec_norm:
            chosen_label = dec_norm
        else:
            chosen_idx_1based = 1
            chosen_label = options[0] if options else "Approve once"

        action_trace = ""
        delivered = False
        response_state = "sent"

        try:
            loop = asyncio.get_running_loop()
        except Exception:
            loop = None

        # ── Engine 1: DSH (`@zseven-w/dsh-crew` + `dsh-security-mode`) ──────────────────────────
        if eng == "dsh" or sess.dsh_spec:
            dsh_job_id = meta.get("dsh_job_id") or sess.dsh_job_id
            dsh_token = meta.get("dsh_token") or sess.dsh_token or ""
            dsh_msg_id = meta.get("dsh_message_id")
            dsh_needs_continue = bool(meta.get("dsh_needs_continue"))
            reply_str = custom_input.strip() if (custom_input and not option_index and not decision) else chosen_label
            if not reply_str:
                if dec_norm in ("deny", "reject", "no"):
                    reply_str = "Reject"
                elif dec_norm in ("approve_session", "allow_session", "always"):
                    reply_str = "Allow for this session"
                else:
                    reply_str = "Allow once"

            if dsh_job_id and dsh_msg_id and not dsh_needs_continue:
                r = httpx.post(
                    dsh_url(f"/_dsh/dsh-crew/jobs/{dsh_job_id}/messages"),
                    headers={"x-dsh-crew-job-token": dsh_token},
                    json={"message": reply_str, "reply_to": dsh_msg_id},
                    timeout=6.0,
                )
                delivered = r.is_success
                action_trace = f"DSH mailbox reply_to={dsh_msg_id} message={reply_str!r} (HTTP {r.status_code})"
                sess.emit_virtual_ansi(
                    f"\r\n\x1b[1;32m↩ [Parent Approved/Replied to DSH ({dsh_msg_id})]: {reply_str}\x1b[0m\r\n"
                )
            elif dsh_job_id:
                await self._require_dsh_workspace(sess)
                new_token = str(uuid.uuid4())
                r = httpx.post(
                    dsh_url(f"/_dsh/dsh-crew/jobs/{dsh_job_id}/continue"),
                    json={"text": reply_str, "messaging_token": new_token},
                    timeout=10.0,
                )
                cjob = r.json().get("job", {}) if r.is_success else {}
                if cjob.get("id"):
                    sess.dsh_job_id = cjob["id"]
                    sess.dsh_token = new_token
                    sess.status = "running"
                    if loop:
                        self._schedule(self._watch_dsh_hub_job(sess, cjob["id"], new_token))
                    delivered = True
                action_trace = f"DSH /continue job={dsh_job_id}->{cjob.get('id')} text={reply_str!r}"
                sess.emit_virtual_ansi(
                    f"\r\n\x1b[1;32m↻ [Parent Continued DSH needs_input with]: {reply_str}\x1b[0m\r\n"
                )

        # ── Engine 2: Kimi CLI (`ApprovalPanelComponent` & `QuestionDialogComponent`) ──────────
        elif eng == "kimi":
            if kind == "approval_request":
                # In Kimi ApprovalPanelComponent:
                # 1 = Approve once, 2 = Approve for this session, 3 = Reject, 4 = Reject with feedback
                feedback = _one_line(custom_input)
                if approval_action == "deny_feedback":
                    self._write_keys_scheduled(sess, loop, [(0.0, "4"), (0.25, feedback + "\r")])
                    action_trace = f"Kimi ApprovalPanel key '4' + feedback {feedback!r}"
                else:
                    key_num = {"approve_once": "1", "approve_session": "2", "deny": "3"}[approval_action]
                    sess.write_input(key_num)
                    action_trace = f"Kimi ApprovalPanel key {key_num!r} ({chosen_label})"
                    if approval_action == "approve_session":
                        notes.append(
                            "kimi 的「本会话允许」对危险命令不生效，Bash 也只记住完全相同的命令文本：同类操作下次仍会询问"
                        )
                delivered = True
            else:
                # In Kimi QuestionDialogComponent:
                # Single-select: pressing number key 1..N selects option AND advances to Submit tab!
                # Then sending "\r" after 250ms triggers executeSubmitAction(0) ("Submit").
                raw_opts_count = int(meta.get("raw_options_count") or len(options) or 2)
                is_multi = bool(meta.get("multi_select", False))
                if custom_input and (chosen_idx_1based is None or chosen_idx_1based > raw_opts_count):
                    other_key = str(raw_opts_count + 1)
                    sess.write_input(other_key)
                    if loop:
                        loop.call_later(0.25, sess.write_input, custom_input.strip() + "\r")
                        loop.call_later(0.55, sess.write_input, "\r")
                    else:
                        time.sleep(0.25)
                        sess.write_input(custom_input.strip() + "\r")
                        time.sleep(0.25)
                        sess.write_input("\r")
                    action_trace = f"Kimi QuestionDialog Other key {other_key!r} + text {custom_input.strip()!r} + Submit('\\r')"
                else:
                    key_num = str(chosen_idx_1based or 1)
                    sess.write_input(key_num)
                    if is_multi:
                        # In multi-select, Tab advances to Submit tab, then Enter submits
                        if loop:
                            loop.call_later(0.2, sess.write_input, "\t")
                            loop.call_later(0.45, sess.write_input, "\r")
                        else:
                            time.sleep(0.2)
                            sess.write_input("\t")
                            time.sleep(0.25)
                            sess.write_input("\r")
                    else:
                        if loop:
                            loop.call_later(0.25, sess.write_input, "\r")
                        else:
                            time.sleep(0.25)
                            sess.write_input("\r")
                    action_trace = f"Kimi QuestionDialog option key {key_num!r} ({chosen_label}) + Submit('\\r')"
                delivered = True

        # ── Engine 3: OMP (`ask` tool selector `class qR` & `showHookConfirm`) ────────────────
        elif eng == "omp" and kind == "approval_request":
            action_trace, note = self._answer_omp_approval(sess, loop, inter, meta, approval_action, custom_input)
            if note:
                notes.append(note)
                if approval_action == "approve_session":
                    approval_action = "approve_once"
            delivered = True

        elif eng == "omp":
            raw_opts_count = int(meta.get("raw_options_count") or max(1, len(options) - 1))
            clamp_ups = "\x1b[A" * (len(options) + 4)
            if custom_input and (chosen_idx_1based is None or "other" in chosen_label.lower()):
                # "Other (type your own)" is at 0-based index `raw_opts_count`
                downs = "\x1b[B" * raw_opts_count
                sess.write_input(clamp_ups)
                if loop:
                    loop.call_later(0.15, sess.write_input, downs + "\r")
                    loop.call_later(0.45, sess.write_input, custom_input.strip() + "\r")
                else:
                    time.sleep(0.15)
                    sess.write_input(downs + "\r")
                    time.sleep(0.25)
                    sess.write_input(custom_input.strip() + "\r")
                action_trace = f"OMP selector clamp_top + {raw_opts_count}xDown (Other) + text {custom_input.strip()!r}"
            else:
                target_0_idx = max(0, (chosen_idx_1based or 1) - 1)
                downs = "\x1b[B" * target_0_idx
                sess.write_input(clamp_ups)
                if loop:
                    loop.call_later(0.15, sess.write_input, downs + "\r")
                else:
                    time.sleep(0.15)
                    sess.write_input(downs + "\r")
                action_trace = f"OMP selector clamp_top + {target_0_idx}xDown + '\\r' ({chosen_label})"
            delivered = True

        # ── Engine 4: Codex CLI (Approval modal & `request_user_input_async`) ─────────────────
        elif eng == "codex":
            if kind == "approval_request":
                feedback = _one_line(custom_input)
                if meta.get("codex_prompt") == "mcp":
                    parsed = parse_codex_approval_prompt(sess.get_clean_screen_text(max_lines=max(38, sess.rows)))
                    if not parsed or self._codex_mcp_identity(parsed) != tuple(meta.get("prompt_identity") or ()):
                        raise ValueError("Codex MCP 审批弹窗已变化或关闭；未发送按键")
                    wanted = "deny" if approval_action in ("deny", "deny_feedback") else "approve_once"
                    choices = list(meta.get("option_actions") or [])
                    if wanted not in choices:
                        raise ValueError("当前 Codex MCP 弹窗没有对应选项；未发送按键")
                    idx = choices.index(wanted)
                    chosen_idx_1based, chosen_label = idx + 1, options[idx]
                    if approval_action == "approve_session":
                        notes.append("当前 MCP 弹窗没有会话放行选项，本次仅批准一次")
                    approval_action = wanted
                    meta["response_state"] = "sent"
                    inter["metadata"] = meta
                    sess.write_input("\x1b[A" * len(options) + "\x1b[B" * idx + "\r")
                    action_trace = f"Codex MCP selector option {idx + 1} + Enter"
                    # Make one immediate, non-waiting reconciliation pass.  Any later native
                    # confirmation is observed by the watcher and surfaced by HTTP poll.
                    await self._refresh_codex_mcp_response(sess)
                    response_state = "confirmed" if meta.get("response_state") == "confirmed" else "unconfirmed"
                    meta["response_state"] = response_state
                    if response_state == "unconfirmed":
                        notes.append("输入已送达，原生审批确认将由 HTTP poll 返回；不会自动重发")
                    if feedback:
                        notes.append("MCP Cancel 不支持拒绝反馈输入；未向终端追加反馈")
                elif approval_action in ("deny", "deny_feedback"):
                    steps = [(0.0, "n")]
                    if approval_action == "deny_feedback":
                        steps.append((0.25, feedback + "\r"))
                    self._write_keys_scheduled(sess, loop, steps)
                    action_trace = "Codex approval key 'n' (Reject)" + (
                        f" + feedback {feedback!r}" if approval_action == "deny_feedback" else ""
                    )
                elif approval_action == "approve_session" and meta.get("codex_session_key") == "p":
                    # Round 2 (owner): a command prompt's "don't ask again" is p, read from the
                    # screen. codex writes a permanent allow rule for the command prefix.
                    sess.write_input("p")
                    action_trace = "Codex approval key 'p' (don't ask again for this command prefix; permanent)"
                    notes.append(
                        "已发送 p：永久放行。codex 已把这个命令前缀的放行规则写入 "
                        "~/.codex/rules/default.rules，之后所有 codex 会话（包括你自己用的 codex）都不再询问；"
                        "要撤销请删除该文件里对应的规则"
                    )
                elif approval_action == "approve_session" and (
                    meta.get("codex_session_key") == "a"
                    # a record registered before round 2 carries only the prompt kind
                    or ("codex_session_key" not in meta and meta.get("codex_prompt") == "edit")
                ):
                    sess.write_input("a")
                    action_trace = "Codex approval key 'a' (don't ask again for these files)"
                else:
                    sess.write_input("y")
                    action_trace = "Codex approval key 'y' (Approve once)"
                    if approval_action == "approve_session":
                        approval_action = "approve_once"
                        notes.append(
                            "这个 codex 提示上没有读到「不再询问」选项：本次只允许了一次"
                        )
                delivered = True
            else:
                # For Codex `request_user_input_async` / question prompt:
                reply_txt = custom_input.strip() if custom_input else chosen_label
                sess.write_input(reply_txt)
                if loop:
                    loop.call_later(0.25, sess.write_input, "\r")
                else:
                    time.sleep(0.25)
                    sess.write_input("\r")
                action_trace = f"Codex question reply {reply_txt!r} + '\\r'"
                delivered = True

        # ── Engine 5: AGY CLI (Permission prompt & `ask_question` selector) ───────────────────
        elif eng == "agy":
            if kind == "approval_request":
                action_trace, note = self._answer_agy_approval(
                    sess, loop, meta, approval_action, chosen_label, custom_input
                )
                if note:
                    notes.append(note)
                    if approval_action == "approve_session":
                        approval_action = "approve_once"
                delivered = True
            else:
                clamp_ups = "\x1b[A" * (len(options) + 3)
                if custom_input and not option_index:
                    # Select 'Other' at bottom of options list
                    downs = "\x1b[B" * len(options)
                    sess.write_input(clamp_ups)
                    if loop:
                        loop.call_later(0.15, sess.write_input, downs + "\r")
                        loop.call_later(0.4, sess.write_input, custom_input.strip() + "\r")
                    else:
                        time.sleep(0.15)
                        sess.write_input(downs + "\r")
                        time.sleep(0.25)
                        sess.write_input(custom_input.strip() + "\r")
                    action_trace = f"AGY ask_question Other + {custom_input.strip()!r}"
                else:
                    target_0_idx = max(0, (chosen_idx_1based or 1) - 1)
                    downs = "\x1b[B" * target_0_idx
                    sess.write_input(clamp_ups)
                    if loop:
                        loop.call_later(0.15, sess.write_input, downs + "\r")
                    else:
                        time.sleep(0.15)
                        sess.write_input(downs + "\r")
                    action_trace = f"AGY ask_question clamp_top + {target_0_idx}xDown + '\\r' ({chosen_label})"
                delivered = True

        # ── Engine 6: Claude Code CLI (Permission prompt & `AskUserQuestion` selector) ──────
        elif eng == "claude":
            if kind == "approval_request":
                action_trace, note, downgraded = self._answer_claude_approval(
                    sess, loop, meta, approval_action, chosen_label, custom_input
                )
                if note:
                    notes.append(note)
                if downgraded and approval_action == "approve_session":
                    approval_action = "approve_once"
                delivered = True
            else:
                raw_opts_count = int(meta.get("raw_options_count") or max(1, len(options) - 1))
                if custom_input and (chosen_idx_1based is None or chosen_idx_1based > raw_opts_count):
                    other_key = str(raw_opts_count + 1)
                    steps = [(0.0, other_key), (0.2, custom_input.strip() + "\r")]
                    self._write_keys_scheduled(sess, loop, steps)
                    action_trace = f"Claude AskUserQuestion Other key {other_key!r} + {custom_input.strip()!r}"
                else:
                    key_num = str(chosen_idx_1based or 1)
                    self._write_keys_scheduled(sess, loop, [(0.0, key_num), (0.2, "\r")])
                    action_trace = f"Claude AskUserQuestion option key {key_num!r} ({chosen_label}) + '\\r'"
                delivered = True

        # ── Generic / Fallback TUI ─────────────────────────────────
        else:
            if q_entry and not q_entry.get("answered"):
                q_entry["answered"] = True
                q_entry["answer"] = chosen_label
                self.db.on_accept(q_entry["event"].set)
            sess.write_input(chosen_label)
            if loop:
                loop.call_later(0.25, sess.write_input, "\r")
            else:
                sess.write_input("\r")
            action_trace = f"Generic PTY write {chosen_label!r} + '\\r'"
            delivered = True

        # Mark pending question/interaction as resolved
        self.parent_messages.setdefault(session_id, []).append({
            "id": f"pmsg-{uuid.uuid4().hex[:8]}",
            "session_id": session_id,
            "timestamp": round(time.time(), 3),
            "content": custom_input.strip() if custom_input and custom_input.strip() else chosen_label,
            "resolved_question_id": target_qid,
            "read_by_worker": bool(delivered),
        })
        if response_state != "unconfirmed":
            await self.clear_pending_interaction(sess, target_qid)
        sess.current_activity = ("Awaiting native approval confirmation" if response_state == "unconfirmed"
                                 else f"Parent responded ({chosen_label[:50]})")
        await self.db.add_intermediate_message(
            session_id,
            sess.step_count,
            f"[Parent Delegated Response -> {chosen_label}] ({action_trace})",
            source="parent_approval_proxy",
        )
        await self._record_tui_injection(
            sess,
            {
                "at": round(time.time(), 3),
                "ok": bool(delivered),
                "reason": f"interaction_{kind}",
                "attempts": 1,
                "mode": "interaction_response",
                "label": "未确认" if response_state == "unconfirmed" else ("成功" if delivered else "失败"),
            },
        )
        await self.persist_session(sess, final=True)

        if eng in ("agy", "claude") and approval_action == "deny_feedback" and (custom_input or "").strip():
            # D7: agy and claude end the turn on a deny; the feedback goes in as the next message once
            # the composer is back (after the interaction above is cleared, never into the prompt).
            feedback_text = (custom_input or "").strip()
            if loop is not None:
                loop.call_later(AGY_DENY_FEEDBACK_DELAY_SEC, lambda: self._schedule(self._deliver_deny_feedback(session_id, feedback_text)))
            else:
                await asyncio.sleep(AGY_DENY_FEEDBACK_DELAY_SEC)
                await self._deliver_deny_feedback(session_id, feedback_text)

        response = {
            "ok": True,
            "delivered": delivered,
            "response_state": response_state,
            "session_id": session_id,
            "engine": eng,
            "resolved_request_id": target_qid,
            "interaction_kind": kind,
            "chosen_option_index": chosen_idx_1based,
            "chosen_label": chosen_label,
            "approval_action": approval_action,
            **(
                {"requested_approval_action": requested_approval_action}
                if requested_approval_action and requested_approval_action != approval_action
                else {}
            ),
            "action_trace": action_trace,
            "notes": notes,
        }
        if target_qid:
            sess._responded_request_ids[target_qid] = dict(response)
        return response

    def begin_new_turn(self, session: AgentSession, status: Optional[str] = None) -> None:
        """T1/D2: a message to a completed session (or a resume) starts a new turn.

        This is the one definition of the decision "the previous final reply is kept until the new
        turn overwrites it". Both paths call it: a live follow-up from deliver_parent_message and
        both resume paths (§5.3), right after db.reset_session_after_resume. The answer and its
        source stay in place, and the flag marks them as belonging to the ended turn:
        set_final_response lets the new turn's first source replace them whatever its rank. The
        priority table still governs the rest of that turn. completion_reported restarts with the
        turn, so a later exit is judged on this turn's evidence.
        """
        session._turn_completion_emitted = False
        session.turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        session.final_response_from_previous_turn = bool(session.final_response)
        session.completion_reported = False
        session._seen_tui_prompt_signatures.clear()
        if status is not None:
            session.status = status

    @staticmethod
    def _tui_writer_queue(session: Any) -> TuiWriterQueue:
        """K6 (fix 9c): the session's one-writer-at-a-time queue (made on first use)."""
        writer = getattr(session, "tui_writer", None)
        if writer is None:
            writer = TuiWriterQueue()
            session.tui_writer = writer
        return writer

    @staticmethod
    def _unchecked_live_result() -> Dict[str, Any]:
        """K6 (fix 9c): last_injection for a live message sent without any check of its submit."""
        return {
            "at": round(time.time(), 3),
            "ok": None,
            "reason": "live_write_unchecked",
            "attempts": 1,
            "mode": "parent_message",
            "label": "未确认",
        }

    def _tui_composer_located(self, session: AgentSession) -> bool:
        """K6 (fix 9c): the hub knows the engine's composer and finds it on the current screen."""
        if getattr(session, "kind", "tui") != "tui":
            return False
        engine = str(getattr(session, "engine", "") or "").strip().lower()
        if engine not in TUI_COMPOSER_SHAPES:
            return False
        screen = session.get_clean_screen_text(max_lines=40)
        return self._tui_input_line(screen, session.engine) is not None

    def _live_submit_checked(self, session: AgentSession) -> bool:
        """A live message goes through the checked submit: an engine in TUI_LIVE_SUBMIT_CHECKED
        (owner decision engines-checked) whose composer is on the current screen."""
        engine = str(getattr(session, "engine", "") or "").strip().lower()
        return engine in TUI_LIVE_SUBMIT_CHECKED and self._tui_composer_located(session)

    async def _deliver_live_text_to_tui(
        self, sess: AgentSession, text: str, turn_before: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """K6 (fix 9c): a live message with Enter to a TUI session; {pty_written, injection, queued}.

        The message takes its place in the session's writer queue at once. Then:
          - nobody ahead of it and no composer the hub can locate (an unknown engine, a screen
            without a composer): today's unchecked delivery right now -- the text, Enter
            TUI_UNCHECKED_ENTER_DELAY_SEC later, last_injection ok:null "live_write_unchecked";
          - otherwise a background task waits for its turn and types it: through
            _submit_text_to_tui (K5 settle -> Enter -> K3 proof -> retry/D3) when the composer is
            located then, else the unchecked way. Its real outcome replaces the "submitting" record
            in last_injection. The caller's reply is not held for it.
        Outside a running event loop (scripts, tests) the old synchronous unchecked write stays.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            written = sess.write_input(text)
            if written:
                sess.last_injection = self._unchecked_live_result()
            sess.write_input("\r")
            return {"pty_written": written, "injection": "live_write" if written else "none",
                    "queued": False}
        claim = self._tui_writer_queue(sess).claim("parent_message")
        eng = str(getattr(sess, "engine", "") or "").strip().lower()
        if claim.first and eng not in TUI_LIVE_SUBMIT_CHECKED and not self._live_submit_checked(sess):
            written = sess.write_input(text)
            if not written:
                claim.release()
                return {"pty_written": False, "injection": "none", "queued": False}
            sess.last_injection = self._unchecked_live_result()
            loop.call_later(TUI_UNCHECKED_ENTER_DELAY_SEC, self._press_unchecked_enter, sess, claim)
            return {"pty_written": True, "injection": "live_write", "queued": False}
        submit_key = str((getattr(sess, "profile_snapshot", None) or {}).get("submit_key") or "\r")
        # K8 (fix 9d): one id for this message's "submitting" record and its outcome, so the outcome
        # replaces that entry in recent_injections instead of adding a second one.
        submit_id = f"inj-{uuid.uuid4().hex[:8]}"
        sess.last_injection = self._live_submitting_record(submit_id)
        self._schedule(
            self._submit_queued_live_text(sess, text, submit_key, claim, turn_before, submit_id)
        )
        return {"pty_written": False, "injection": "pending", "queued": True,
                "submit_id": submit_id}

    @staticmethod
    def _live_submitting_record(submit_id: str = "") -> Dict[str, Any]:
        """K6 (fix 9c): last_injection while a live message waits for its turn or is being proved.

        K8 (fix 9d): `submit_id` ties it to the outcome that replaces it in recent_injections.
        """
        return {
            "submit_id": submit_id or f"inj-{uuid.uuid4().hex[:8]}",
            "at": round(time.time(), 3),
            "ok": None,
            "reason": "submitting",
            "pending": True,
            "attempts": 0,
            "mode": "parent_message",
            "label": "提交中",
        }

    @staticmethod
    def _press_unchecked_enter(sess: AgentSession, claim: TuiWriterClaim) -> None:
        """K6 (fix 9c): the Enter of an unchecked live message; the next writer may type after it."""
        try:
            sess.write_input("\r")
        finally:
            claim.release()

    async def _submit_queued_live_text(
        self,
        sess: AgentSession,
        text: str,
        submit_key: str,
        claim: TuiWriterClaim,
        turn_before: Optional[Dict[str, Any]],
        submit_id: str = "",
    ) -> None:
        """K6 (fix 9c): type one live message once its turn in the writer queue comes, and record it.

        The composer is looked for again when the turn comes (the startup text may have been ahead
        of it). An Enter the engine refused (K3, kimi "Error: LLM not set") is typed again after a
        short pause within the same attempt limit. A submit that failed for certain (ok:false) on a
        message that had started a new turn puts the completed turn back, so the session is not
        left "running" with no turn behind it.
        """
        result: Dict[str, Any]
        try:
            await claim.wait_turn()
            # The writer ahead (the startup text, an earlier message) recorded its own outcome
            # meanwhile; this message is the one being submitted now.
            submit_id = submit_id or f"inj-{uuid.uuid4().hex[:8]}"
            sess.last_injection = self._live_submitting_record(submit_id)
            size_before = 0
            if sess.transcript_path:
                try:
                    size_before = Path(sess.transcript_path).stat().st_size
                except OSError:
                    pass
            if not sess.is_process_alive():
                result = {"ok": False, "reason": "process_gone", "attempts": 0, "label": "失败"}
            elif sess.engine == "codex" or self._live_submit_checked(sess):
                result = await self._submit_text_to_tui(sess, text, submit_key)
                retypes = 0
                while result.pop("retype", False) and sess.is_process_alive():
                    retypes += 1
                    logger.warning(
                        "[%s] the engine refused a live message (%s) after %s Enter(s); typing it again",
                        sess.session_id, result.get("rejected"), result.get("attempts"),
                    )
                    await asyncio.sleep(TUI_LIVE_RETYPE_PAUSE_SEC)
                    result = await self._submit_text_to_tui(
                        sess, text, submit_key, prior=dict(result, retyped=retypes)
                    )
            elif sess.write_input(
                f"\x1b[200~{str(text).replace(chr(13), ' ').replace(chr(10), ' ').strip()}\x1b[201~"
                if str(getattr(sess, "engine", "") or "").strip().lower() in ("claude", "agy")
                else text
            ):
                clean = str(text).replace("\r", " ").replace("\n", " ").strip()
                probe = clean[:30].strip()
                tail = clean[-30:].strip()
                eng = str(getattr(sess, "engine", "") or "").strip().lower()
                marks = self._tui_submit_marks(sess, sess.get_clean_screen_text(max_lines=40), eng, probe)
                marks["expected_text"] = clean
                await asyncio.sleep(TUI_UNCHECKED_ENTER_DELAY_SEC)
                sess.write_input(submit_key or "\r")
                if eng in TUI_LIVE_SUBMIT_CHECKED and clean:
                    state, reason = await self._confirm_tui_submit(
                        sess,
                        probe,
                        tail,
                        size_before,
                        eng,
                        echoed=False,
                        deadline=time.time() + TUI_SUBMIT_CONFIRM_SEC,
                        marks=marks,
                        typed_len=len(clean),
                    )
                    if state == "confirmed":
                        result = {
                            "ok": True,
                            "reason": reason,
                            "attempts": 1,
                            "echoed": False,
                            "label": "成功",
                        }
                    else:
                        result = self._unchecked_live_result()
                else:
                    result = self._unchecked_live_result()
            else:
                result = {"ok": False, "reason": "write_failed", "attempts": 0, "label": "失败"}
        except asyncio.CancelledError:
            claim.release()
            raise
        except PersistenceUnavailable:
            raise
        except Exception as e:
            logger.exception("[%s] live message submit failed", getattr(sess, "session_id", "?"))
            result = {"ok": False, "reason": "submit_error", "detail": str(e)[:200], "attempts": 0,
                      "label": "失败"}
        try:
            result["mode"] = "parent_message"
            result["at"] = round(time.time(), 3)
            if submit_id:
                result["submit_id"] = submit_id
            if result.get("ok") is not True and result.get("recovery_required") and turn_before is not None:
                self._restore_turn_after_failed_message(sess, turn_before)
            elif result.get("ok") is False and turn_before is not None:
                self._restore_turn_after_failed_message(sess, turn_before)
            await self._record_tui_injection(sess, result)
        finally:
            claim.release()

    def _restore_turn_after_failed_message(
        self, sess: AgentSession, turn_before: Dict[str, Any]
    ) -> None:
        """K6 (fix 9c): undo begin_new_turn for a message that certainly never reached the engine.

        Only while nothing has moved the session on since (still "running", no completion reported).
        """
        if sess.status != "running" or sess.completion_reported:
            return
        sess.status = turn_before["status"]
        sess.completion_reported = bool(turn_before["completion_reported"])
        sess._turn_completion_emitted = bool(turn_before["turn_completion_emitted"])
        sess.final_response_from_previous_turn = bool(turn_before["from_previous_turn"])

    @logical_write
    async def deliver_parent_message(
        self,
        session_id: str,
        text: str,
        request_id: Optional[str] = None,
        also_write_pty: bool = True,
        press_enter: bool = True,
        clear_input: bool = False,
    ) -> Dict[str, Any]:
        sess = self.sessions.get(session_id)
        if clear_input:
            if str(text).strip():
                raise ValueError("clear_input must be sent alone with empty text")
            return await self._clear_tui_draft(sess)
        if not str(text).strip():
            return {"delivered": False, "reason": "empty_text", "session_id": session_id,
                    "pty_written": False, "injection": "none"}
        if sess and sess.engine == "codex" and getattr(sess, "_tui_draft_pending", None):
            if not self._reconcile_codex_submit(sess):
                return {"delivered": False, "reason": "input_recovery_required", "session_id": session_id,
                        "pty_written": False, "injection": "none",
                        "recovery_hint": "Inspect the draft, then send empty text with clear_input=true; do not resend blindly."}
        # T1: this message may start a new turn on a session that already reported completion. The
        # status is captured before the delivery branches change it, and the reset itself runs at
        # the end, only when the text really reached the session — a failed delivery must not leave
        # the session "running" with no turn behind it.
        starts_new_turn = bool(sess is not None and sess.status == "completed")
        # K6 (fix 9c): what begin_new_turn changes, so a live message whose submit later fails for
        # certain can put the completed turn back (see _submit_queued_live_text).
        turn_before = (
            {
                "status": sess.status,
                "completion_reported": sess.completion_reported,
                "turn_completion_emitted": getattr(sess, "_turn_completion_emitted", False),
                "from_previous_turn": sess.final_response_from_previous_turn,
            }
            if starts_new_turn else None
        )
        # If there is an active structured approval/question interaction on this session,
        # route through our engine-aware responder automatically!
        if sess and sess.pending_interaction:
            t_clean = text.strip()
            opt_idx: Optional[int] = None
            dec: Optional[str] = None
            if t_clean.isdigit():
                opt_idx = int(t_clean)
            elif t_clean.lower() in (
                "approve",
                "allow",
                "allow once",
                "yes",
                "y",
                "approve_session",
                "allow for this session",
                "always",
                "deny",
                "reject",
                "no",
                "n",
            ):
                dec = t_clean.lower().replace(" ", "_")
            res = await self.respond_to_interaction(
                session_id=session_id,
                decision=dec,
                option_index=opt_idx,
                custom_input=None if (opt_idx or dec) else t_clean,
                request_id=request_id,
            )
            return {
                "delivered": res.get("delivered", True),
                "session_id": session_id,
                "resolved_question_id": res.get("resolved_request_id"),
                "pty_written": True,
                "injection": "interaction",
                "interaction_response": res,
            }

        resolved_question_id = None

        target_qid = request_id or (sess.pending_question_id if sess else None)
        dsh_replied = False
        if target_qid and target_qid in self.pending_questions:
            q_entry = self.pending_questions[target_qid]
            if not q_entry["answered"]:
                q_entry["answered"] = True
                q_entry["answer"] = text
                self.db.on_accept(q_entry["event"].set)
                resolved_question_id = target_qid
                if q_entry.get("dsh_message_id") and q_entry.get("dsh_job_id"):
                    try:
                        httpx.post(
                            dsh_url(f"/_dsh/dsh-crew/jobs/{q_entry['dsh_job_id']}/messages"),
                            headers={"x-dsh-crew-job-token": q_entry["dsh_token"]},
                            json={"message": text, "reply_to": q_entry["dsh_message_id"]},
                            timeout=5.0,
                        )
                        dsh_replied = True
                        if sess:
                            sess.pending_question_id = None
                            sess.status = "running"
                            sess.emit_virtual_ansi(
                                f"\r\n\x1b[1;32m↩ [Parent Replied to DSH Question {resolved_question_id}]: {text}\x1b[0m\r\n"
                            )
                    except Exception as e:
                        if sess:
                            sess.emit_virtual_ansi(f"\r\n\x1b[31m✖ Failed to deliver reply to DSH: {e}\x1b[0m\r\n")

        box = self.parent_messages.setdefault(session_id, [])
        msg_rec = {
            "id": f"pmsg-{uuid.uuid4().hex[:8]}",
            "session_id": session_id,
            "timestamp": round(time.time(), 3),
            "content": text,
            "resolved_question_id": resolved_question_id,
            "read_by_worker": bool(resolved_question_id),
        }
        box.append(msg_rec)

        pty_written = False
        # K6 (fix 9c): a live message handed to the background submit (typed and proved there).
        live_queued = False
        live_injection: Optional[str] = None
        live_submit_id: Optional[str] = None
        dsh_error: Optional[str] = None
        if sess and sess.dsh_spec and not resolved_question_id and not dsh_replied:
            clean_txt = text.strip()
            if not clean_txt:
                dsh_error = "消息为空，未发送给 DSH"
            else:
                try:
                    loop = asyncio.get_running_loop()
                    if sess.dsh_job_id and sess.status == "running":
                        r = httpx.post(
                            dsh_url(f"/_dsh/dsh-crew/jobs/{sess.dsh_job_id}/messages"),
                            headers={"x-dsh-crew-job-token": sess.dsh_token or ""},
                            json={"message": clean_txt},
                            timeout=5.0,
                        )
                        if not r.is_success:
                            dsh_error = f"DSH 拒绝了这条消息（HTTP {r.status_code}）"
                        else:
                            sess.emit_virtual_ansi(f"\r\n\x1b[1;36m➤ [Parent Steer Injected into DSH]: {clean_txt}\x1b[0m\r\n")
                            pty_written = True
                    elif sess.dsh_job_id and sess.status in ("completed", "exited"):
                        await self._require_dsh_workspace(sess)
                        new_token = str(uuid.uuid4())
                        r = httpx.post(
                            dsh_url(f"/_dsh/dsh-crew/jobs/{sess.dsh_job_id}/continue"),
                            json={"text": clean_txt, "messaging_token": new_token},
                            timeout=10.0,
                        )
                        cjob = (r.json() or {}).get("job", {}) if r.is_success else {}
                        if cjob.get("id"):
                            sess.dsh_job_id = cjob["id"]
                            sess.dsh_token = new_token
                            sess.conversation_ref = _keep_dsh_record_seq(sess.conversation_ref, {
                                "dsh_job_id": cjob["id"],
                                "dsh_session_id": sess.dsh_session_id or cjob.get("sessionId") or "",
                                "dsh_token": new_token,
                            })
                            sess.status = "running"
                            # B9: last_task stays the initial task; follow-ups live in current_activity.
                            if not sess.last_task:
                                sess.last_task = clean_txt
                            sess.current_activity = f"Continuing DSH session ({cjob['id']}): {clean_txt[:60]}"
                            sess.emit_virtual_ansi(
                                f"\r\n\x1b[1;36m↻ [Continuing DSH Session {sess.dsh_session_id} -> {cjob['id']}]: {clean_txt}\x1b[0m\r\n"
                            )
                            self._schedule(self._watch_dsh_hub_job(sess, cjob["id"], new_token))
                            pty_written = True
                        else:
                            dsh_error = f"DSH 无法继续该任务（HTTP {r.status_code}）"
                    elif not sess.dsh_job_id:
                        self._schedule(self._spawn_dsh_hub_job(sess, clean_txt))
                        pty_written = True
                    else:
                        # A8: a DSH session in any other status cannot take the text; say so.
                        dsh_error = f"DSH 会话当前状态（{sess.status}）无法接收消息"
                except Exception as e:
                    dsh_error = f"DSH 消息发送失败：{e}"
                    sess.emit_virtual_ansi(f"\r\n\x1b[31m✖ DSH message delivery error: {e}\x1b[0m\r\n")
        elif sess and also_write_pty and not resolved_question_id:
            if sess.kind == "desktop_gui" and sess.hwnd:
                payload = text + ("\r" if press_enter and not text.endswith("\r") else "")
                pty_written = Win32WindowHelper.send_text(sess.hwnd, payload)
            elif (press_enter and sess.kind == "tui" and text.rstrip("\r\n").strip()
                  and sess.is_process_alive()):
                # K6 (fix 9c): a live message with Enter goes through the same checked submit as
                # the startup text when the composer can be located, one writer at a time.
                live = await self._deliver_live_text_to_tui(sess, text.rstrip("\r\n"), turn_before)
                pty_written = live["pty_written"]
                live_injection = live["injection"]
                live_queued = live["queued"]
                live_submit_id = live.get("submit_id")
            else:
                clean_txt = text.rstrip("\r\n") if press_enter else text
                if sess.engine == "codex":
                    claim = self._tui_writer_queue(sess).claim("draft_input")
                    try:
                        await claim.wait_turn()
                        if getattr(sess, "_tui_draft_pending", None) or sess.pending_interaction:
                            raise ValueError("Input is pending; inspect or clear the draft before writing")
                        line = self._tui_input_line(sess.get_clean_screen_text(max_lines=40), "codex")
                        if not self._codex_composer_empty(sess, line):
                            raise ValueError("Cannot write into a nonempty or unavailable composer")
                        pty_written = sess.write_input(clean_txt)
                        if pty_written:
                            sess._tui_draft_pending = {"reason": "unsubmitted_draft"}
                    finally:
                        claim.release()
                else:
                    pty_written = sess.write_input(clean_txt)
                if pty_written and clean_txt:
                    live_submit_id = f"inj-{uuid.uuid4().hex[:8]}"
                    # W11: a live steering write is the last injection too, so the tool result no
                    # longer shows last_injection:null after a send_to_subagent. K6 (fix 9c): a
                    # text sent with Enter was never checked here, so it does not claim ok:true.
                    sess.last_injection = (
                        dict(self._unchecked_live_result(), submit_id=live_submit_id) if press_enter else {
                            "submit_id": live_submit_id,
                            "at": round(time.time(), 3),
                            "ok": True,
                            "reason": "live_write",
                            "attempts": 0,
                            "mode": "parent_message",
                            "label": "已写入",
                        }
                    )
                if press_enter:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.call_later(TUI_UNCHECKED_ENTER_DELAY_SEC, sess.write_input, "\r")
                    except Exception:
                        sess.write_input("\r")

        if dsh_error is not None:
            # A8: a message DSH could not take is reported with its reason, never as a canned true.
            if sess:
                await self.persist_session(sess, final=True)
            return {
                "delivered": False,
                "reason": dsh_error,
                "session_id": session_id,
                "resolved_question_id": resolved_question_id,
                "pty_written": False,
                "injection": "dsh_failed",
                "mailbox_message_id": msg_rec["id"],
            }

        if sess and starts_new_turn and press_enter and (pty_written or live_queued) and not resolved_question_id:
            # T1: send_to_subagent's live write, the console's send and a parent reply all land
            # here. The completed turn's answer is cleared and the status goes back to running, so
            # the wait in send_to_subagent sees a turn in progress instead of status_completed.
            # K6 (fix 9c): a message handed to the background submit starts the turn now too, so
            # that wait does not return at once; a submit that then fails for certain undoes it.
            self.begin_new_turn(sess, status="running")

        if sess:
            await self.persist_session(sess, final=True)

        return {
            "delivered": None if live_queued else True,
            "session_id": session_id,
            "resolved_question_id": resolved_question_id,
            "pty_written": pty_written,
            # K6 (fix 9c): "pending" = handed to the background submit; its outcome lands in
            # last_injection (pty_written stays false until then, as for a pending startup text).
            "injection": live_injection or ("live_write" if pty_written else "none"),
            "mailbox_message_id": msg_rec["id"],
            **({"submit_id": live_submit_id} if live_submit_id else {}),
        }

    def get_worker_events(self, session_id: str = "all", after_seq: int = 0) -> List[Dict[str, Any]]:
        target_ids = session_targets(session_id)

        results = []
        for ev in self.worker_events:
            if ev["seq"] <= after_seq:
                continue
            if target_ids and ev["session_id"] not in target_ids:
                continue
            results.append(ev)
        return results

    async def get_transcript_view(self, session_id: str, limit: int = FEED_DEFAULT_LIMIT) -> Dict[str, Any]:
        """Build the engine-neutral transcript the console shows by default (see transcript_feed).

        This deliberately does not invoke the CLI-specific transcript summarizer.  The console
        polls this endpoint every second for visible panes, so it only gathers the normalized
        worker events and small persisted tails; `transcript_feed` does the shaping.
        """
        try:
            limit = max(10, min(FEED_MAX_LIMIT, int(limit)))
        except (TypeError, ValueError):
            limit = FEED_DEFAULT_LIMIT
        sess = self.sessions.get(session_id)
        if sess is not None:
            meta = sess.to_dict()
        else:
            meta = await self.db.get_session_record(session_id)
            if not meta:
                raise ValueError(f"找不到会话 {session_id}")

        # A tool is two events and the feed hides plumbing, so read a wider event window than the
        # entry limit.  The memory ring is freshest; SQLite fills the gap after a restart or once
        # the session has left the live map.  Sequence numbers make the merge deterministic.
        window = limit * 3 + 60
        persisted_events = await self.db.get_recent_worker_events(session_id, window)
        events_by_seq = {int(item.get("seq")): item for item in persisted_events if item.get("seq") is not None}
        for item in self.get_worker_events(session_id=session_id, after_seq=0):
            if item.get("seq") is not None:
                events_by_seq[int(item["seq"])] = item
        events = sorted(events_by_seq.values(), key=lambda item: int(item.get("seq") or 0))[-window:]
        messages = await self.db.get_recent_intermediate_messages(session_id, 40)

        parent_messages = self.parent_messages.get(session_id)
        if parent_messages is None:
            parent_messages = meta.get("parent_messages") or []
        return build_transcript_feed(
            session_id=session_id,
            meta=meta,
            events=events,
            messages=messages,
            parent_messages=parent_messages,
            pending_interaction=sess.pending_interaction if sess else meta.get("pending_interaction"),
            limit=limit,
            window_saturated=len(persisted_events) >= window,
        )

    async def get_pending_interactions(self, session_id: str = "all") -> List[Dict[str, Any]]:
        """Returns active unresolved pending interactions (`waiting_parent`) for the target session(s)."""
        target_ids = session_targets(session_id)
        pending: List[Dict[str, Any]] = []
        for sid, sess in list(self.sessions.items()):
            if target_ids and sid not in target_ids:
                continue
            if sess is not None and sess.kind == "tui" and sess.status in LIVE_STATUSES:
                await self.poll_tui_screen_interactions_once(sess)
            if sess is not None and sess.status == "waiting_parent" and sess.pending_interaction:
                item = dict(sess.pending_interaction)
                item.setdefault("session_id", sid)
                pending.append(item)
        return pending

    def _transcript_watch_alive(self, session: AgentSession) -> bool:
        """Follow all turns and briefly drain delayed native records after process exit."""
        if self.sessions.get(session.session_id) is not session:
            return False
        try:
            if session.is_process_alive():
                return True
        except Exception:
            return False
        if session.terminated_by_user:
            return False
        if session.ended_at is None:
            session.ended_at = time.time()
        return time.time() < session.ended_at + NATIVE_TRANSCRIPT_DRAIN_SEC

    async def _watch_codex_rollout(self, session: AgentSession, root: Optional[Path] = None):
        """Live tails the Codex rollout jsonl files for a spawned Codex session.

        `root` points the watcher at a synthetic copy of the layout for tests; production uses
        ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.
        """
        codex_root = Path(root) if root is not None else codex_sessions_root()
        if not codex_root.exists():
            return

        target_file: Optional[Path] = None
        offset = 0
        scan_start = time.time()

        if session.resume_attach and session.conversation_ref:
            # Spec §4: attach directly to the known rollout (no discovery heuristic), tail from its end.
            ref_path = str(session.conversation_ref.get("rollout_path") or "")
            if ref_path and os.path.isfile(ref_path):
                target_file = Path(ref_path)
                offset = int(session.attach_offset or 0)
                await self._claim_transcript(session, target_file)
        else:
            while self._transcript_watch_alive(session):
                await asyncio.sleep(self._transcript_scan_interval(time.time() - scan_start))
                if not self._transcript_watch_alive(session):
                    break
                candidates = []
                date_dirs = [
                    d for d in self._codex_date_dirs(
                        codex_root, self._session_spawn_anchor(session), time.time()
                    )
                    if d.is_dir()
                ]
                rollout_iter = (
                    (p for d in date_dirs for p in d.glob("rollout-*.jsonl"))
                    if date_dirs
                    else codex_root.rglob("rollout-*.jsonl")
                )
                for candidate in rollout_iter:
                    try:
                        # V2/Q2: only a rollout created after this session's process started. The marker
                        # check and the launch-order fallback live in _claim_conversation.
                        if not self._conversation_created_after(candidate, session):
                            continue
                        candidates.append(candidate)
                    except OSError:
                        continue
                target_file = await self._claim_conversation(session, candidates, levels_up=0)
                if target_file:
                    break

        if not target_file:
            return

        turn_has_step = False
        while self._transcript_watch_alive(session):
            if session.resume_attach:
                # A resumed codex may start a new rollout for the same session id; accept it when it
                # appears and tail it from the beginning (everything in it is new).
                switched = await self._codex_resume_new_rollout(session, codex_root, target_file, scan_start)
                if switched is not None:
                    target_file = switched
                    offset = 0
            elif session.session_id not in self._transcript_marker_ids(target_file):
                upgraded = await self._codex_upgrade_to_marked_rollout(session, codex_root, target_file, scan_start)
                if upgraded is not None:
                    target_file = upgraded
                    offset = 0
            try:
                lines, offset = read_complete_lines(target_file, offset)
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    otype = obj.get("type")
                    payload = obj.get("payload") or {}
                    ptype = payload.get("type")

                    if otype == "response_item":
                        if ptype in ("custom_tool_call", "function_call"):
                            turn_has_step = True
                            session._codex_last_tool_call_id = payload.get("call_id")
                            tname = payload.get("name") or payload.get("tool_name") or ptype
                            targs = payload.get("arguments") or payload.get("input") or ""
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "codex_jsonl",
                                    "hook_event": "ToolCall",
                                    "tool_name": tname,
                                    "tool_input": targs,
                                    "call_id": payload.get("call_id"),
                                },
                            )
                            if tname in ("request_user_input", "request_user_input_async"):
                                try:
                                    parsed_args = json.loads(targs) if isinstance(targs, str) else (targs or {})
                                except Exception:
                                    parsed_args = {}
                                qs = parsed_args.get("questions") or []
                                q0 = qs[0] if qs and isinstance(qs[0], dict) else {}
                                q_title = str(q0.get("header") or q0.get("id") or "Codex Question")
                                q_prompt = str(q0.get("question") or q0.get("title") or targs)
                                raw_opts = q0.get("options") or []
                                opt_labels = [
                                    str(o.get("label") or o.get("value") or o) if isinstance(o, dict) else str(o)
                                    for o in raw_opts
                                ]
                                await self.register_pending_interaction(
                                    session=session,
                                    kind="question",
                                    title=q_title,
                                    prompt=q_prompt,
                                    options=opt_labels,
                                    source="codex_jsonl_request_user_input",
                                    request_id=payload.get("call_id") or f"codex-q-{uuid.uuid4().hex[:6]}",
                                    metadata={"raw_options_count": len(opt_labels), "tool_name": tname},
                                )
                        elif ptype in ("custom_tool_call_output", "function_call_output"):
                            out_val = payload.get("output") or ""
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "codex_jsonl",
                                    "hook_event": "PostToolUse",
                                    "tool_name": payload.get("name") or payload.get("call_id") or ptype,
                                    "tool_response": out_val,
                                    "call_id": payload.get("call_id"),
                                },
                            )
                            if (
                                session.pending_interaction
                                and (
                                    (
                                        payload.get("call_id") in self._interaction_ids(session.pending_interaction)
                                        and (session.pending_interaction.get("metadata") or {}).get("tool_name") == "request_user_input"
                                    )
                                    or (
                                        ptype == "custom_tool_call_output"
                                        and session.pending_interaction.get("kind") == "approval_request"
                                        and (
                                            (session.pending_interaction.get("metadata") or {}).get("codex_prompt") != "mcp"
                                            or payload.get("call_id") == (session.pending_interaction.get("metadata") or {}).get("native_call_id")
                                        )
                                    )
                                )
                            ):
                                await self.clear_pending_interaction(session)
                        elif ptype == "message" and payload.get("role") == "user":
                            parts = payload.get("content") or []
                            user_txts = [
                                str(p.get("text") or "")
                                for p in parts
                                if isinstance(p, dict)
                                and p.get("text")
                                and "<environment_context>" not in str(p.get("text"))
                            ]
                            if user_txts:
                                session.note_transcript_user_message(" ".join(user_txts).strip())
                                if session.pending_interaction:
                                    await self.clear_pending_interaction(session)
                                turn_has_step = False
                                if session.status == "completed":
                                    self.begin_new_turn(session, status="running")
                        elif ptype == "message" and payload.get("role") == "assistant":
                            parts = payload.get("content") or []
                            texts = [
                                p.get("text", "")
                                for p in parts
                                if isinstance(p, dict) and p.get("type") in ("output_text", "text") and p.get("text")
                            ]
                            if texts:
                                msg_txt = "\n".join(texts).strip()
                                if not turn_has_step:
                                    turn_has_step = True
                                    await self.record_telemetry(
                                        session.session_id,
                                        {
                                            "source": "codex_jsonl",
                                            "hook_event": "ReportStep",
                                            "tool_name": "llm_inference",
                                            "tool_input": msg_txt[:200],
                                        },
                                    )
                                await self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "codex_jsonl",
                                        "hook_event": "AssistantMessage",
                                        "intermediate_messages": [msg_txt],
                                        "final_assistant_text": msg_txt,
                                    },
                                )
                    elif otype == "event_msg" and ptype in ("exec_approval_request", "apply_patch_approval_request"):
                        cmd_desc = payload.get("command") or payload.get("reason") or json.dumps(payload, ensure_ascii=False)[:300]
                        if isinstance(cmd_desc, list):
                            cmd_desc = " ".join(str(x) for x in cmd_desc)
                        # D7: codex 0.165 no longer writes these to the rollout (the screen detector
                        # catches its prompts); kept for older builds, with the current labels.
                        is_edit = ptype == "apply_patch_approval_request"
                        await self.register_pending_interaction(
                            session=session,
                            kind="approval_request",
                            title=f"Codex {ptype}",
                            prompt=f"Would you like to run/apply: {cmd_desc}",
                            options=(
                                ["Yes, proceed (y)", "Yes, and don't ask again for these files (a)",
                                 "No, and tell Codex what to do differently (esc)"]
                                if is_edit else
                                ["Yes, proceed (y)", "No, and tell Codex what to do differently (esc)"]
                            ),
                            source="codex_jsonl_approval",
                            request_id=payload.get("call_id") or f"codex-appr-{uuid.uuid4().hex[:6]}",
                            # No screen was read here, so no `p` is ever sent from this record:
                            # a command prompt answers approve_session with y (approve once).
                            metadata={
                                "codex_prompt": "edit" if is_edit else "command",
                                "codex_session_key": "a" if is_edit else "",
                                "option_actions": (
                                    ["approve_once", "approve_session", "deny"] if is_edit
                                    else ["approve_once", "deny"]
                                ),
                            },
                        )
                    elif otype == "event_msg" and ptype == "task_complete":
                        last_msg = payload.get("last_agent_message")
                        if last_msg:
                            if not turn_has_step:
                                turn_has_step = True
                                await self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "codex_jsonl",
                                        "hook_event": "ReportStep",
                                        "tool_name": "llm_inference",
                                        "tool_input": str(last_msg)[:200],
                                    },
                                )
                            session.set_final_response(str(last_msg), "codex_task_complete")
                        await self.record_worker_event(
                            session.session_id,
                            {
                                "type": "turn_completed",
                                "category": "turn_completed",
                                "status": "turn_idle",
                                "content": str(last_msg or ""),
                            },
                        )
                    elif otype == "event_msg" and ptype == "turn_aborted":
                        abort_reason = str(payload.get("reason") or "interrupted")
                        if session.pending_interaction:
                            await self.clear_pending_interaction(session)
                        await self.db.complete_latest_tool_call(
                            session.session_id, None, f"Turn aborted ({abort_reason})"
                        )
                        if session.status == "running":
                            session.status = "waiting_parent"
                        session.current_activity = f"Turn aborted ({abort_reason}) — idle at prompt"
                        await self.persist_session(session)
                        await self.record_worker_event(
                            session.session_id,
                            {
                                "type": "turn_aborted",
                                "category": "turn_completed",
                                "status": "waiting_parent",
                                "content": f"Codex turn aborted ({abort_reason})",
                            },
                        )
            except PersistenceUnavailable:
                raise
            except Exception:
                pass
            await asyncio.sleep(1.0)

        if session.resume_attach and session.status in ("exited", "crashed"):
            # Research: an archived codex session refuses to resume; surface its own instruction.
            screen = session.get_clean_screen_text(max_lines=40).lower()
            if "is archived" in screen or "unarchive" in screen:
                await self._mark_codex_archived(session)

    @staticmethod
    def _codex_date_dirs(root: Path, anchor: float, now: float) -> List[Path]:
        """A13: the sessions/<Y>/<M>/<D> folders a resume can possibly write into.

        The spawn date, plus every day up to today when the resume crosses midnight — never a
        recursive walk over the whole store, which is what made the check slow on a year of rollouts.
        """
        try:
            start = datetime.datetime.fromtimestamp(float(anchor))
            end = datetime.datetime.fromtimestamp(float(now))
        except (OverflowError, OSError, ValueError):
            return []
        days: List[Path] = []
        day = start.date()
        last = end.date()
        for _ in range(4):
            days.append(Path(root) / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}")
            if day >= last:
                break
            day = day + datetime.timedelta(days=1)
        return days

    async def _codex_resume_new_rollout(
        self, session: AgentSession, root: Path, current: Path, scan_start: float
    ) -> Optional[Path]:
        ref = session.conversation_ref or {}
        sid = str(ref.get("session_id") or "")
        if not sid or (time.time() - scan_start) > 60.0:
            return None
        candidates: List[Path] = []
        for folder in self._codex_date_dirs(
            Path(root), self._session_spawn_anchor(session), time.time()
        ):
            if not folder.is_dir():
                continue
            for candidate in folder.glob("rollout-*.jsonl"):
                try:
                    if candidate == Path(current) or not self._conversation_created_after(candidate, session):
                        continue
                    if self._codex_session_id_from_rollout(candidate) != sid:
                        continue
                    candidates.append(candidate)
                except OSError:
                    continue
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0)
        chosen = candidates[-1]
        if not await self._claim_transcript(session, chosen):
            return None
        session.conversation_ref = {"session_id": sid, "rollout_path": str(chosen)}
        await self.persist_session(session)
        return chosen

    async def _codex_upgrade_to_marked_rollout(
        self, session: AgentSession, root: Path, current: Path, scan_start: float
    ) -> Optional[Path]:
        """If an unmarked rollout was claimed before Codex flushed the prompt containing our session_id,
        upgrade to the rollout that explicitly carries our session_id marker once it appears."""
        if (time.time() - scan_start) > 120.0:
            return None
        for folder in self._codex_date_dirs(
            Path(root), self._session_spawn_anchor(session), time.time()
        ):
            if not folder.is_dir():
                continue
            for candidate in folder.glob("rollout-*.jsonl"):
                try:
                    if candidate == Path(current) or not self._conversation_created_after(candidate, session):
                        continue
                    if session.session_id not in self._transcript_marker_ids(candidate):
                        continue
                    if self._transcript_claimed_by_other(candidate, session):
                        continue
                    with self._transcript_lock:
                        old_key = self._transcript_key(current)
                        if self._claimed_transcripts.get(old_key) == session.session_id:
                            self._claimed_transcripts.pop(old_key, None)
                    if await self._claim_transcript(session, candidate):
                        return candidate
                except OSError:
                    continue
        return None

    async def _mark_codex_archived(self, session: AgentSession) -> None:
        reason = CODEX_ARCHIVED_MESSAGE.format(sid=(session.conversation_ref or {}).get("session_id") or "")
        session.resumable = False
        session.resume_block_reason = reason
        await self.db.update_archive_fields(session.session_id, resumable=0, resume_block_reason=reason)
        session.emit_virtual_ansi(f"\r\n\x1b[1;31m✖ {reason}\x1b[0m\r\n")
        await self.record_worker_event(
            session.session_id,
            {"type": "error", "category": "lifecycle", "status": "failed", "content": reason},
        )

    async def _watch_omp_session(self, session: AgentSession, omp_session_dir: Path):
        """Live tails OMP's dedicated session JSONL file inside omp_session_dir for tool calls, thoughts, and final responses."""
        target_file: Optional[Path] = None
        offset = 0
        scan_start = time.time()
        if session.resume_attach and session.conversation_ref:
            # Spec §4: omp appends to the same session file, so attach to it and tail from the end.
            ref_path = str(session.conversation_ref.get("session_file") or "")
            if ref_path and os.path.isfile(ref_path):
                target_file = Path(ref_path)
                offset = int(session.attach_offset or 0)
                await self._claim_transcript(session, target_file)
        else:
            while self._transcript_watch_alive(session):
                await asyncio.sleep(self._transcript_scan_interval(time.time() - scan_start))
                if not self._transcript_watch_alive(session):
                    break
                # This directory is created by the hub for this one session (`--session-dir`), so a
                # cross-session attach is already impossible; claim it anyway so the rule is uniform.
                files = sorted(omp_session_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                for candidate in files:
                    if not self._conversation_created_after(candidate, session):
                        continue
                    if await self._claim_transcript(session, candidate):
                        target_file = candidate
                        break
                if target_file:
                    break
        if not target_file:
            return

        turn_has_step = False
        while self._transcript_watch_alive(session):
            try:
                lines, offset = read_complete_lines(target_file, offset)
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    otype = obj.get("type")
                    ctype = obj.get("customType")
                    if otype == "custom" and ctype == "tool_execution_start":
                        turn_has_step = True
                        data = obj.get("data") or {}
                        tname = data.get("toolName", "tool")
                        targs = data.get("args") or {}
                        cmd_val = ""
                        if isinstance(targs, dict):
                            cmd_val = str(
                                targs.get("command")
                                or targs.get("cmd")
                                or targs.get("path")
                                or targs.get("file_path")
                                or ""
                            )
                        elif isinstance(targs, str):
                            cmd_val = targs
                        session._inflight_transcript_tool = {
                            "tool_name": str(tname or "tool"),
                            "command": cmd_val,
                            "args": targs,
                            "call_id": str(data.get("toolCallId") or ""),
                        }
                        await self.record_telemetry(
                            session.session_id,
                            {
                                "source": "omp_jsonl",
                                "hook_event": "PreToolUse",
                                "tool_name": tname,
                                "tool_input": targs,
                            },
                        )
                        if tname == "ask" and isinstance(targs, dict):
                            qs = targs.get("questions") or []
                            q0 = qs[0] if qs and isinstance(qs[0], dict) else targs
                            q_header = str(q0.get("header") or q0.get("id") or "OMP Ask")
                            q_prompt = str(q0.get("question") or "Sub-Agent is asking a question")
                            raw_opts = q0.get("options") or []
                            opt_labels = [
                                str(o.get("label") or o) if isinstance(o, dict) else str(o)
                                for o in raw_opts
                            ]
                            display_opts = list(opt_labels) + ["Other (type your own)"]
                            await self.register_pending_interaction(
                                session=session,
                                kind="question",
                                title=q_header,
                                prompt=q_prompt,
                                options=display_opts,
                                source="omp_jsonl_ask_tool",
                                request_id=str(data.get("toolCallId") or f"omp-ask-{uuid.uuid4().hex[:6]}"),
                                metadata={
                                    "raw_options_count": len(opt_labels),
                                    "recommended": q0.get("recommended"),
                                    "multi": bool(q0.get("multi", False)),
                                },
                            )
                    elif otype == "message":
                        msg = obj.get("message") or {}
                        role = msg.get("role")
                        content = msg.get("content") or []
                        if role == "user":
                            user_txts = []
                            if isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                                        user_txts.append(str(part["text"]))
                            elif isinstance(content, str) and content.strip():
                                user_txts.append(content.strip())
                            if user_txts:
                                session.note_transcript_user_message(" ".join(user_txts).strip())
                            session._inflight_transcript_tool = None
                            if session.pending_interaction:
                                await self.clear_pending_interaction(session)
                            turn_has_step = False
                            if session.status == "completed":
                                self.begin_new_turn(session, status="running")
                        elif role == "toolResult":
                            session._inflight_transcript_tool = None
                            if session.pending_interaction:
                                await self.clear_pending_interaction(session)
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "omp_jsonl",
                                    "hook_event": "PostToolUse",
                                    "tool_name": msg.get("toolName"),
                                    "tool_response": content,
                                },
                            )
                        elif role == "assistant" and isinstance(content, list):
                            thoughts = []
                            texts = []
                            has_tool_use = False
                            for part in content:
                                if not isinstance(part, dict):
                                    continue
                                ptype = part.get("type")
                                if ptype == "thinking" and part.get("thinking"):
                                    thoughts.append(str(part["thinking"]).strip())
                                elif ptype == "text" and part.get("text"):
                                    texts.append(str(part["text"]).strip())
                                elif ptype in ("toolCall", "tool_use"):
                                    has_tool_use = True
                                    turn_has_step = True
                                    if part.get("name") == "ask" and isinstance(part.get("arguments"), dict):
                                        targs = part["arguments"]
                                        qs = targs.get("questions") or []
                                        q0 = qs[0] if qs and isinstance(qs[0], dict) else targs
                                        q_header = str(q0.get("header") or q0.get("id") or "OMP Ask")
                                        q_prompt = str(q0.get("question") or "Sub-Agent is asking a question")
                                        raw_opts = q0.get("options") or []
                                        opt_labels = [
                                            str(o.get("label") or o) if isinstance(o, dict) else str(o)
                                            for o in raw_opts
                                        ]
                                        display_opts = list(opt_labels) + ["Other (type your own)"]
                                        await self.register_pending_interaction(
                                            session=session,
                                            kind="question",
                                            title=q_header,
                                            prompt=q_prompt,
                                            options=display_opts,
                                            source="omp_jsonl_ask_tool",
                                            request_id=str(part.get("id") or f"omp-ask-{uuid.uuid4().hex[:6]}"),
                                            metadata={
                                                "raw_options_count": len(opt_labels),
                                                "recommended": q0.get("recommended"),
                                                "multi": bool(q0.get("multi", False)),
                                            },
                                        )
                            inter = texts
                            is_end_turn = not has_tool_use and msg.get("stopReason") in ("stop", "end_turn", "stop_sequence")
                            final_txt = "\n".join(texts).strip() if is_end_turn else None
                            if final_txt and not turn_has_step:
                                turn_has_step = True
                                await self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "omp_jsonl",
                                        "hook_event": "ReportStep",
                                        "tool_name": "llm_inference",
                                        "tool_input": final_txt[:200],
                                    },
                                )
                            await self.record_reasoning_parts(session.session_id, "omp_jsonl", thoughts)
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "omp_jsonl",
                                    "hook_event": "TurnComplete" if is_end_turn else "AssistantMessage",
                                    "intermediate_messages": inter,
                                    "final_assistant_text": final_txt,
                                },
                            )
            except PersistenceUnavailable:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.8)

    @staticmethod
    def _kimi_resolved_interaction_ids(lines: List[str]) -> Set[str]:
        """D7: ids of the `interaction.resolved` records in one read of kimi's wire.jsonl."""
        ids: Set[str] = set()
        for raw in lines:
            if '"interaction.resolved"' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("type") == "interaction.resolved" and obj.get("id"):
                ids.add(str(obj["id"]))
        return ids

    async def _drain_kimi_wire_interactions_once(self, session: AgentSession) -> Optional[Dict[str, Any]]:
        """Synchronously drains Kimi's wire.jsonl for interaction.request / interaction.resolved."""
        tpath = getattr(session, "transcript_path", None)
        if not tpath:
            return session.pending_interaction
        target_file = Path(tpath)
        if not target_file.is_file():
            return session.pending_interaction
        offset = int(getattr(session, "_kimi_wire_interaction_offset", 0) or 0)
        try:
            lines, session._kimi_wire_interaction_offset = read_complete_lines(target_file, offset)
            if not lines:
                return session.pending_interaction
            resolved_in_batch = self._kimi_resolved_interaction_ids(lines)
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "interaction.request":
                    req_id = str(obj.get("id") or f"kimi-int-{uuid.uuid4().hex[:6]}")
                    ikind = obj.get("kind") or "question"
                    req_obj = obj.get("request") or {}
                    if req_id in resolved_in_batch:
                        cand = getattr(session, "_unadopted_screen_interaction", None)
                        if cand and session.pending_interaction is not cand and cand.get("kind") == (
                            "approval_request" if ikind == "approval" else "question"
                        ):
                            session._unadopted_screen_interaction = None
                        continue
                    if ikind == "approval":
                        disp = req_obj.get("display") or {}
                        cmd_str = disp.get("command") or req_obj.get("description") or req_obj.get("action") or "action"
                        await self.register_pending_interaction(
                            session=session,
                            kind="approval_request",
                            title=f"Kimi Approval: {req_obj.get('toolName') or 'Shell'} ({req_obj.get('action') or 'run'})",
                            prompt=f"Command/Action requiring approval: {cmd_str}",
                            options=[
                                "Approve once",
                                "Approve for this session",
                                "Reject",
                                "Reject with feedback",
                            ],
                            source="kimi_wire_jsonl_interaction_request",
                            request_id=req_id,
                            metadata={"command": str(disp.get("command") or "")},
                        )
                    else:
                        qs = req_obj.get("questions") or []
                        q0 = qs[0] if qs and isinstance(qs[0], dict) else req_obj
                        q_header = str(q0.get("header") or "Kimi Question")
                        q_prompt = str(q0.get("question") or "Sub-Agent is asking a question")
                        raw_opts = q0.get("options") or []
                        opt_labels = [
                            f"{o.get('label', '')}" + (f" — {o.get('description')}" if o.get("description") else "")
                            if isinstance(o, dict)
                            else str(o)
                            for o in raw_opts
                        ]
                        display_opts = list(opt_labels) + ["Other (custom text input)"]
                        await self.register_pending_interaction(
                            session=session,
                            kind="question",
                            title=q_header,
                            prompt=q_prompt,
                            options=display_opts,
                            source="kimi_wire_jsonl_interaction_request",
                            request_id=req_id,
                            metadata={
                                "raw_options_count": len(opt_labels),
                                "multi_select": bool(q0.get("multi_select", False)),
                            },
                        )
                elif obj.get("type") == "interaction.resolved":
                    await self.clear_pending_interaction(session, str(obj.get("id") or ""))
        except PersistenceUnavailable:
            raise
        except Exception:
            pass
        return session.pending_interaction

    async def _watch_kimi_session(self, session: AgentSession, root: Optional[Path] = None):
        """Live tails Kimi Code CLI's wire.jsonl for toolCalls, assistant thoughts/messages, and agent.turn.ended.

        `root` points the watcher at a synthetic copy of the layout for tests; production uses
        ~/.kimi-code/sessions/<session>/agents/main/wire.jsonl.
        """
        kimi_root = (
            Path(root) if root is not None else kimi_store_root() / "sessions"
        )
        if not kimi_root.exists():
            return

        target_file: Optional[Path] = None
        offset = 0
        scan_start = time.time()

        if session.resume_attach and session.conversation_ref:
            # Spec §4: kimi appends to the same wire.jsonl; attach and tail from the end.
            session_dir = str(session.conversation_ref.get("session_dir") or "")
            ref_path = os.path.join(session_dir, "agents", "main", "wire.jsonl") if session_dir else ""
            if ref_path and os.path.isfile(ref_path):
                target_file = Path(ref_path)
                offset = int(session.attach_offset or 0)
                await self._claim_transcript(session, target_file)

        while target_file is None and self._transcript_watch_alive(session):
            await asyncio.sleep(self._transcript_scan_interval(time.time() - scan_start))
            if not self._transcript_watch_alive(session):
                break
            # First try exact match via Kimi TUI banner: Session: session_<uuid>
            scr_txt = session.get_clean_screen_text(max_lines=35)
            m = re.search(r"(session_[0-9a-f\-]{20,})", scr_txt)
            if m:
                # The TUI banner names the exact conversation, so this path needs no time heuristic;
                # it still must not be a transcript another hub session is already tailing (V2).
                kimi_sid = m.group(1)
                exact = list(kimi_root.rglob(f"{kimi_sid}/agents/main/wire.jsonl"))
                if exact and exact[0].exists() and await self._claim_transcript(session, exact[0]):
                    target_file = exact[0]
                    break

            candidates = []
            for p in kimi_root.rglob("wire.jsonl"):
                try:
                    # V2/Q2: a wire.jsonl that already existed is not this session's; only a session
                    # directory created after the launch. The marker check lives in _claim_conversation.
                    if not self._conversation_created_after(p, session, levels_up=2):
                        continue
                    candidates.append(p)
                except OSError:
                    pass
            target_file = await self._claim_conversation(session, candidates, levels_up=2)
            if target_file:
                break

        if not target_file:
            return
        session.transcript_path = target_file
        session._kimi_wire_interaction_offset = offset
        await self.persist_session(session)

        latest_assistant_text: Optional[str] = None
        turn_has_step = False
        # Current Kimi writes every step twice: streamed loop events as it works, then the whole
        # assistant / tool messages (agent.message.appended, batched at the end of a multi-step
        # turn).  Once the stream is seen, the messages are a second copy of the same step.
        streamed_steps = False
        step_texts: List[str] = []  # streamed text parts of the current step
        while self._transcript_watch_alive(session):
            try:
                await self._drain_kimi_wire_interactions_once(session)
                lines, offset = read_complete_lines(target_file, offset)
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    if obj.get("type") in ("interaction.request", "interaction.resolved"):
                        continue

                    if obj.get("type") == "turn.prompt" and (obj.get("origin") or {}).get("kind") == "user":
                        # K3 (fix 9): kimi's record of a user message the TUI really accepted. The
                        # typed-submit check reads it from the session; no second reader of the file.
                        parts = obj.get("input") or []
                        session.note_transcript_user_message(" ".join(
                            str(p.get("text") or "") for p in parts if isinstance(p, dict)
                        ))
                        turn_has_step = False
                        latest_assistant_text = None
                        step_texts = []
                        if session.status == "completed":
                            self.begin_new_turn(session, status="running")
                        continue

                    if obj.get("kind") == "event" and obj.get("type") == "agent.turn.ended":
                        if latest_assistant_text:
                            if not turn_has_step:
                                turn_has_step = True
                                await self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "kimi_wire_jsonl",
                                        "hook_event": "ReportStep",
                                        "tool_name": "llm_inference",
                                        "tool_input": latest_assistant_text[:200],
                                    },
                                )
                            session.set_final_response(latest_assistant_text, "native_transcript")
                        await self.record_worker_event(
                            session.session_id,
                            {
                                "type": "turn_completed",
                                "category": "turn_completed",
                                "status": "turn_idle",
                                "content": latest_assistant_text or "",
                            },
                        )
                        step_texts = []
                        continue

                    if obj.get("type") == "context.append_loop_event":
                        ev = obj.get("event") or {}
                        ev_type = ev.get("type")
                        if ev_type in ("content.part", "tool.call", "tool.result"):
                            streamed_steps = True
                        if ev_type == "step.begin":
                            step_texts = []
                        elif ev_type == "content.part":
                            part = ev.get("part") or {}
                            if part.get("type") == "think" and part.get("think"):
                                await self.record_reasoning_parts(session.session_id, "kimi_wire_jsonl", [part["think"]])
                            elif part.get("type") == "text" and part.get("text"):
                                txt = str(part["text"]).strip()
                                # The step's whole text is the answer, as the skipped whole message had it.
                                step_texts.append(txt)
                                latest_assistant_text = "\n".join(step_texts)
                                await self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "kimi_wire_jsonl",
                                        "hook_event": "AssistantMessage",
                                        "intermediate_messages": [txt],
                                        "final_assistant_text": txt,
                                    },
                                )
                        elif ev_type == "tool.call":
                            turn_has_step = True
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "kimi_wire_jsonl",
                                    "hook_event": "PreToolUse",
                                    "tool_name": ev.get("name") or "kimi_tool",
                                    "tool_input": ev.get("args") or "",
                                },
                            )
                        elif ev_type == "tool.result":
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "kimi_wire_jsonl",
                                    "hook_event": "PostToolUse",
                                    "tool_name": ev.get("name"),
                                    "tool_response": ev.get("output") or ev.get("result") or "",
                                },
                            )
                        continue

                    outer_msg = obj.get("message")
                    if not isinstance(outer_msg, dict):
                        continue
                    inner = outer_msg.get("message") if isinstance(outer_msg.get("message"), dict) else outer_msg
                    role = inner.get("role")
                    content = inner.get("content") or []
                    tool_calls = inner.get("toolCalls") or []
                    if streamed_steps and role in ("assistant", "tool"):
                        continue

                    if role == "assistant":
                        thoughts = []
                        texts = []
                        if isinstance(content, list):
                            for part in content:
                                if not isinstance(part, dict):
                                    continue
                                if part.get("type") == "think" and part.get("think"):
                                    thoughts.append(str(part["think"]).strip())
                                elif part.get("type") == "text" and part.get("text"):
                                    texts.append(str(part["text"]).strip())
                        if texts:
                            latest_assistant_text = "\n".join(texts)
                        await self.record_reasoning_parts(session.session_id, "kimi_wire_jsonl", thoughts)
                        if texts:
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "kimi_wire_jsonl",
                                    "hook_event": "AssistantMessage",
                                    "intermediate_messages": texts,
                                    "final_assistant_text": latest_assistant_text,
                                },
                            )
                        for tc in tool_calls:
                            turn_has_step = True
                            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                            tname = fn.get("name") or tc.get("name") or "kimi_tool"
                            targs = fn.get("arguments") or tc.get("arguments") or ""
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "kimi_wire_jsonl",
                                    "hook_event": "PreToolUse",
                                    "tool_name": tname,
                                    "tool_input": targs,
                                },
                            )
                    elif role == "tool":
                        await self.record_telemetry(
                            session.session_id,
                            {
                                "source": "kimi_wire_jsonl",
                                "hook_event": "PostToolUse",
                                "tool_response": content,
                            },
                        )
            except PersistenceUnavailable:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.8)

    async def _watch_agy_transcript(self, session: AgentSession, root: Optional[Path] = None):
        """Live tails AGY CLI's transcript.jsonl.

        `root` points the watcher at a synthetic copy of the layout for tests; production is
        ~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript.jsonl (V3).
        """
        brain_root = Path(root) if root is not None else agy_brain_root()
        if not brain_root.exists():
            return

        target_file: Optional[Path] = None
        offset = 0
        scan_start = time.time()
        ref_path = ""

        if session.resume_attach and session.conversation_ref:
            # Spec §4: attach directly to the archived brain dir. agy silently starts an EMPTY
            # conversation for an unknown id, so a new conversation dir here means the resume failed.
            brain_dir = str(session.conversation_ref.get("brain_dir") or "")
            ref_path = os.path.join(brain_dir, ".system_generated", "logs", "transcript.jsonl") if brain_dir else ""
            if ref_path and os.path.isfile(ref_path):
                target_file = Path(ref_path)
                offset = int(session.attach_offset or 0)
                await self._claim_transcript(session, target_file)

        if target_file is None and session.resume_attach and ref_path:
            # A3/V3: the archived brain dir is the reference. Some brain dirs hold no
            # transcript.jsonl until the resumed turn writes one, so wait for that file INSIDE the
            # same dir — and keep looking for a different new conversation, which is the failure.
            resume_attach_offset = int(session.attach_offset or 0)
            verify_deadline = self._session_spawn_anchor(session) + AGY_RESUME_VERIFY_WINDOW_SEC
            # R7: the wait for the transcript file gets its own 120 s deadline. The 30 s verify
            # window is only about stray conversations; the old loop spun for the session's whole
            # life when the file never appeared, and gave up at the first failed claim.
            transcript_deadline = time.time() + AGY_RESUME_TRANSCRIPT_WAIT_SEC
            while target_file is None and self._transcript_watch_alive(session):
                if time.time() > transcript_deadline:
                    logger.warning(
                        "agy 恢复：%.0f 秒内未能接管 transcript %s，停止监听会话 %s",
                        AGY_RESUME_TRANSCRIPT_WAIT_SEC, ref_path, session.session_id,
                    )
                    return
                if os.path.isfile(ref_path):
                    candidate = Path(ref_path)
                    if await self._claim_transcript(session, candidate):
                        target_file = candidate
                        offset = resume_attach_offset
                        break
                    # R7: a failed claim means another watcher still owns the file; keep waiting
                    # (until the deadline above) instead of abandoning the resume.
                if time.time() <= verify_deadline:
                    stray = self._agy_new_conversation_dir(session, brain_root)
                    if stray is not None:
                        await self._mark_agy_resume_failed(session)
                        return
                await asyncio.sleep(0.5)
            if target_file is None:
                return

        while target_file is None and not session.resume_attach and self._transcript_watch_alive(session):
            await asyncio.sleep(self._transcript_scan_interval(time.time() - scan_start))
            if not self._transcript_watch_alive(session):
                break
            candidates = []
            for p in brain_root.rglob("transcript.jsonl"):
                try:
                    # V2/Q2: the conversation directory (brain/<uuid>) must be younger than this
                    # session's process. A transcript that already existed belongs to somebody else.
                    if not self._conversation_created_after(p, session, levels_up=2):
                        continue
                    candidates.append(p)
                except OSError:
                    continue
            target_file = await self._claim_conversation(session, candidates, levels_up=2)
            if target_file:
                break

        if not target_file:
            return

        attach_size = int(session.attach_offset or 0)
        resume_confirmed = not session.resume_attach
        # A3: the stray scan is bounded to the first 30 s after the resume spawn; past that it stops.
        verify_deadline = self._session_spawn_anchor(session) + AGY_RESUME_VERIFY_WINDOW_SEC
        turn_has_step = False
        while self._transcript_watch_alive(session):
            if session.resume_attach and not resume_confirmed:
                try:
                    if os.path.getsize(target_file) > attach_size:
                        resume_confirmed = True
                except OSError:
                    pass
                if not resume_confirmed and time.time() <= verify_deadline:
                    stray = self._agy_new_conversation_dir(session, brain_root)
                    if stray is not None:
                        await self._mark_agy_resume_failed(session)
                        return
            try:
                lines, offset = read_complete_lines(target_file, offset)
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "USER_INPUT":
                        user_txt = str(obj.get("content") or "").strip()
                        if user_txt:
                            session.note_transcript_user_message(user_txt)
                        session._inflight_transcript_tool = None
                        if session.pending_interaction is not None:
                            await self.clear_pending_interaction(session)
                        turn_has_step = False
                        if session.status == "completed":
                            self.begin_new_turn(session, status="running")
                        continue
                    if obj.get("type") == "GENERIC":
                        session._inflight_transcript_tool = None
                        if session.pending_interaction is not None:
                            await self.clear_pending_interaction(session)
                        gen_content = str(obj.get("content") or "").strip()
                        await self.record_telemetry(
                            session.session_id,
                            {
                                "source": "agy_transcript",
                                "hook_event": "PostToolUse",
                                "tool_response": gen_content[:2000],
                            },
                        )
                        continue
                    if obj.get("type") == "PLANNER_RESPONSE":
                        thinking = (obj.get("thinking") or "").strip()
                        content = (obj.get("content") or "").strip()
                        tcalls = obj.get("tool_calls") or []
                        # The real store writes tool_calls as a JSON string; an empty list (or an
                        # absent key) is the end-of-turn marker W6 needs.
                        if isinstance(tcalls, str):
                            try:
                                tcalls = json.loads(tcalls) if tcalls.strip() else []
                            except Exception:
                                tcalls = []
                        if not isinstance(tcalls, list):
                            tcalls = []
                        await self.record_reasoning_parts(session.session_id, "agy_transcript", [thinking])
                        if content:
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "agy_transcript",
                                    "hook_event": "AssistantMessage",
                                    "intermediate_messages": [content],
                                    "final_assistant_text": content or None,
                                },
                            )
                        for tc in tcalls:
                            if isinstance(tc, dict):
                                turn_has_step = True
                                tname = tc.get("name") or tc.get("tool_name") or "agy_tool"
                                targs = tc.get("args") or tc.get("arguments") or ""
                                parsed_tc_args = targs if isinstance(targs, dict) else {}
                                if isinstance(targs, str):
                                    try:
                                        parsed_tc_args = json.loads(targs) if targs.strip() else {}
                                    except Exception:
                                        parsed_tc_args = {}
                                if not isinstance(parsed_tc_args, dict):
                                    parsed_tc_args = {}
                                tc_cmd = str(
                                    parsed_tc_args.get("CommandLine")
                                    or parsed_tc_args.get("command")
                                    or parsed_tc_args.get("Path")
                                    or parsed_tc_args.get("path")
                                    or (targs if isinstance(targs, str) else "")
                                    or ""
                                )
                                session._inflight_transcript_tool = {
                                    "tool_name": str(tname),
                                    "command": tc_cmd,
                                    "args": parsed_tc_args or targs,
                                    "call_id": str(tc.get("id") or obj.get("step_index") or ""),
                                }
                                await self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "agy_transcript",
                                        "hook_event": "PreToolUse",
                                        "tool_name": tname,
                                        "tool_input": targs,
                                    },
                                )
                                if tname in ("ask_question", "default_api:ask_question"):
                                    parsed_args = targs if isinstance(targs, dict) else {}
                                    if isinstance(targs, str):
                                        try:
                                            parsed_args = json.loads(targs)
                                        except Exception:
                                            parsed_args = {}
                                    qs = parsed_args.get("questions") or []
                                    q0 = qs[0] if qs and isinstance(qs[0], dict) else parsed_args
                                    q_prompt = str(q0.get("question") or "AGY Sub-Agent is asking a question")
                                    raw_opts = [str(o) for o in (q0.get("options") or [])]
                                    await self.register_pending_interaction(
                                        session=session,
                                        kind="question",
                                        title="AGY ask_question",
                                        prompt=q_prompt,
                                        options=raw_opts + ["Other (custom text input)"],
                                        source="agy_transcript_ask_question",
                                        request_id=f"agy-ask-{obj.get('step_index', uuid.uuid4().hex[:6])}",
                                        metadata={
                                            "raw_options_count": len(raw_opts),
                                            "multi_select": bool(q0.get("is_multi_select", False)),
                                        },
                                    )
                        if not tcalls:
                            # W6: a planner response with no pending tool calls is AGY's end of
                            # turn; the next transcript row is a new USER_INPUT (or the file ends).
                            # On a resume the watcher attaches at the old file size, so every event
                            # here belongs to a new turn and only one is emitted per turn.
                            final_text = content or ""
                            if final_text:
                                if not turn_has_step:
                                    turn_has_step = True
                                    await self.record_telemetry(
                                        session.session_id,
                                        {
                                            "source": "agy_transcript",
                                            "hook_event": "ReportStep",
                                            "tool_name": "llm_inference",
                                            "tool_input": final_text[:200],
                                        },
                                    )
                                session.set_final_response(final_text, "native_transcript")
                            await self.record_worker_event(
                                session.session_id,
                                {
                                    "type": "turn_completed",
                                    "category": "turn_completed",
                                    "status": "turn_idle",
                                    "content": final_text,
                                },
                            )
            except PersistenceUnavailable:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.8)

    @staticmethod
    def _agy_conversation_is_durable(brain_dir: Path) -> bool:
        """A3/V3: a real agy conversation dir — or its conversations/<uuid>.db — not a bare dir.

        The old check keyed on transcript.jsonl existing, which the real store disproves: 2 of the
        60 brain dirs there hold no transcript at all. The brain dir (with content) and the SQLite
        conversation store are the durable signals.
        """
        p = Path(brain_dir)
        try:
            if not p.is_dir():
                return False
            # The store is a sibling of the brain dir: <root>/conversations/<uuid>.db beside
            # <root>/brain/<uuid> (confirmed against the real antigravity-cli layout), never a child
            # of the brain dir — a "conversations" folder inside brain/ is not a conversation.
            db = p.parent.parent / "conversations" / f"{p.name}.db"
            if db.is_file():
                return True
            return any(p.iterdir())
        except OSError:
            return False

    def _agy_new_conversation_dir(self, session: AgentSession, brain_root: Path) -> Optional[Path]:
        """A3: a conversation dir that proves the resumed agy opened a NEW conversation.

        Only within AGY_RESUME_VERIFY_WINDOW_SEC after the resume spawn, only a dir that is not the
        archived one, and only when it cannot belong to another agy session that is still starting
        (such a dir is "unknown", never a failure — one resume must not fail another's).
        """
        anchor = self._session_spawn_anchor(session)
        now = time.time()
        if now - anchor > AGY_RESUME_VERIFY_WINDOW_SEC:
            return None
        ref = session.conversation_ref or {}
        ref_id = str(ref.get("conversation_id") or "")
        claimed_dirs = {
            str((s.conversation_ref or {}).get("brain_dir") or "")
            for s in list(self.sessions.values())
            if s is not session
        }
        claimed_dirs.discard("")
        pending_elsewhere = False
        for other in list(self.sessions.values()):
            if other is session or (other.engine or "").strip().lower() != "agy":
                continue
            if other.conversation_ref or not other.is_process_alive():
                continue
            other_anchor = self._session_spawn_anchor(other)
            if (anchor - 1.0) <= other_anchor <= now:
                pending_elsewhere = True
                break
        try:
            children = list(Path(brain_root).iterdir())
        except OSError:
            return None
        for child in children:
            try:
                if not child.is_dir() or child.name == ref_id or str(child) in claimed_dirs:
                    continue
                created, exact = self._conversation_creation_time(child, levels_up=0)
                if created is None:
                    continue
                slack = AGY_CONVERSATION_ANCHOR_SLACK_SEC if exact else 1.0
                if created < (anchor - slack):
                    continue
                if not self._agy_conversation_is_durable(child):
                    continue
                if pending_elsewhere:
                    # Another agy session spawned in the same window has not claimed a conversation
                    # yet, so this new dir may be its own: answer "unknown", do not fail this resume.
                    continue
                return child
            except OSError:
                continue
        return None

    async def _mark_agy_resume_failed(self, session: AgentSession) -> None:
        session.resumable = False
        session.resume_block_reason = AGY_RESUME_FAILURE_MESSAGE
        await self.db.update_archive_fields(
            session.session_id, resumable=0, resume_block_reason=AGY_RESUME_FAILURE_MESSAGE
        )
        session.emit_virtual_ansi(f"\r\n\x1b[1;31m✖ {AGY_RESUME_FAILURE_MESSAGE}\x1b[0m\r\n")
        await self.record_worker_event(
            session.session_id,
            {
                "type": "error",
                "category": "lifecycle",
                "status": "failed",
                "content": AGY_RESUME_FAILURE_MESSAGE,
            },
        )

    def _claude_session_projects_root(self, session: Optional[AgentSession] = None) -> Path:
        if session is not None:
            snap = getattr(session, "profile_snapshot", None) or {}
            env_map = snap.get("env") if isinstance(snap.get("env"), dict) else {}
            prof = self.profiles.get(session.profile_id) or {}
            prof_env = prof.get("env") if isinstance(prof.get("env"), dict) else {}
            cfg = (
                snap.get("config_dir")
                or env_map.get("CLAUDE_CONFIG_DIR")
                or prof.get("config_dir")
                or prof_env.get("CLAUDE_CONFIG_DIR")
            )
            if cfg and str(cfg).strip():
                return claude_projects_root(str(cfg).strip())
        return claude_projects_root()

    async def _watch_claude_session(self, session: AgentSession, root: Optional[Path] = None):
        """Live tails Claude Code CLI's session JSONL (`~/.claude/projects/<slug>/<session-uuid>.jsonl`).

        `root` points the watcher at a synthetic copy of the layout for tests; production uses
        `claude_projects_root()` (honoring the profile's `config_dir` / `CLAUDE_CONFIG_DIR`).
        """
        claude_root = Path(root) if root is not None else self._claude_session_projects_root(session)

        target_file: Optional[Path] = None
        offset = 0
        scan_start = time.time()

        if session.resume_attach and session.conversation_ref:
            ref_path = str(session.conversation_ref.get("transcript_path") or "")
            if ref_path and os.path.isfile(ref_path):
                target_file = Path(ref_path)
                offset = int(session.attach_offset or 0)
                await self._claim_transcript(session, target_file)

        while target_file is None and self._transcript_watch_alive(session):
            await asyncio.sleep(self._transcript_scan_interval(time.time() - scan_start))
            if not self._transcript_watch_alive(session):
                break
            if not claude_root.exists():
                continue

            expected_uuid = str((session.conversation_ref or {}).get("session_id") or "").strip()
            if expected_uuid:
                try:
                    exact = list(claude_root.rglob(f"{expected_uuid}.jsonl"))
                except OSError:
                    exact = []
                if exact and exact[0].is_file() and await self._claim_transcript(session, exact[0]):
                    target_file = exact[0]
                    break

            candidates: List[Path] = []
            try:
                for p in claude_root.rglob("*.jsonl"):
                    try:
                        if not self._conversation_created_after(p, session, levels_up=0):
                            continue
                        candidates.append(p)
                    except OSError:
                        continue
            except OSError:
                pass
            target_file = await self._claim_conversation(session, candidates, levels_up=0)
            if target_file:
                break

        if not target_file:
            return

        turn_last_text: Optional[str] = None
        turn_completed_emitted: bool = False
        turn_has_step: bool = False
        seen_topics: Set[str] = set()
        last_screen_probe_at: float = 0.0
        spinner_stat_re = re.compile(
            r"[✻✽✢✳✶·]\s+([^\n(]{2,45}?)\s*\(([^)]*(?:tokens|thought|\ds\b)[^)]*)\)"
        )

        while self._transcript_watch_alive(session):
            try:
                await self._drain_claude_hook_events(session)
                lines, offset = read_complete_lines(target_file, offset)
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    otype = obj.get("type")
                    if otype == "user":
                        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                        content = msg.get("content")
                        if isinstance(content, str) and content.strip():
                            session._inflight_transcript_tool = None
                            if session.pending_interaction is not None:
                                await self.clear_pending_interaction(session)
                            turn_last_text = None
                            turn_completed_emitted = False
                            turn_has_step = False
                            if session.status == "completed":
                                self.begin_new_turn(session, status="running")
                                await self.persist_session(session)
                            session.note_transcript_user_message(content.strip())
                        elif isinstance(content, list):
                            user_texts = [
                                str(b.get("text") or "")
                                for b in content
                                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                            ]
                            if user_texts:
                                session._inflight_transcript_tool = None
                                if session.pending_interaction is not None:
                                    await self.clear_pending_interaction(session)
                                turn_last_text = None
                                turn_completed_emitted = False
                                turn_has_step = False
                                if session.status == "completed":
                                    self.begin_new_turn(session, status="running")
                                    await self.persist_session(session)
                                session.note_transcript_user_message(" ".join(user_texts))
                            for b in content:
                                if not isinstance(b, dict) or b.get("type") != "tool_result":
                                    continue
                                session._inflight_transcript_tool = None
                                if session.pending_interaction is not None:
                                    await self.clear_pending_interaction(session)
                                res_content = b.get("content")
                                if isinstance(res_content, list):
                                    res_content = "\n".join(
                                        str(x.get("text") or x) if isinstance(x, dict) else str(x)
                                        for x in res_content
                                    )
                                await self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "claude_jsonl",
                                        "hook_event": "PostToolUse",
                                        "tool_response": res_content or "",
                                        "call_id": b.get("tool_use_id"),
                                        "tool_ok": False if b.get("is_error") is True else None,
                                    },
                                )
                    elif otype == "queue-operation":
                        op = obj.get("operation")
                        if op == "enqueue" and obj.get("content"):
                            q_txt = str(obj["content"]).strip()
                            if q_txt:
                                session.note_transcript_user_message(q_txt)
                                await self.db.add_intermediate_message(
                                    session.session_id,
                                    max(1, session.step_count),
                                    f"[Steer queued mid-turn] {q_txt[:240]}",
                                    source="claude_jsonl",
                                )
                                session.current_activity = (
                                    f"Step {max(1, session.step_count)}: Steer message queued ({q_txt[:70]})"
                                )
                                await self.persist_session(session)
                        elif op == "dequeue":
                            turn_last_text = None
                            turn_completed_emitted = False
                            turn_has_step = False
                            await self.db.add_intermediate_message(
                                session.session_id,
                                max(1, session.step_count),
                                "[Steer dequeued] Processing queued parent instruction",
                                source="claude_jsonl",
                            )
                            session.current_activity = (
                                f"Step {max(1, session.step_count)}: Processing queued steer message"
                            )
                            await self.persist_session(session)
                    elif otype == "ai-title" and obj.get("aiTitle"):
                        ai_title = str(obj["aiTitle"]).strip()
                        if ai_title and ai_title not in seen_topics:
                            seen_topics.add(ai_title)
                            await self.db.add_intermediate_message(
                                session.session_id,
                                max(1, session.step_count),
                                f"[Topic] {ai_title}",
                                source="claude_jsonl",
                            )
                    elif otype == "api-request":
                        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
                        req_model = str(params.get("model") or session.effective_model or "claude")
                        out_cfg = (
                            params.get("output_config")
                            if isinstance(params.get("output_config"), dict)
                            else {}
                        )
                        req_effort = str(out_cfg.get("effort") or session.effective_thinking_level or "")
                        turn_has_step = True
                        turn_completed_emitted = False
                        step_desc = f"LLM inference ({req_model}{':' + req_effort if req_effort else ''})"
                        await self.record_telemetry(
                            session.session_id,
                            {
                                "source": "claude_jsonl",
                                "hook_event": "ReportStep",
                                "tool_name": "llm_inference",
                                "tool_input": {
                                    "model": req_model,
                                    **({"effort": req_effort} if req_effort else {}),
                                },
                                "step_title": step_desc,
                            },
                        )
                    elif otype == "assistant":
                        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                        content = msg.get("content") if isinstance(msg.get("content"), list) else []
                        stop_reason = msg.get("stop_reason")
                        think_dur_ms = obj.get("thinkingDurationMs")
                        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                        out_details = (
                            usage.get("output_tokens_details")
                            if isinstance(usage.get("output_tokens_details"), dict)
                            else {}
                        )
                        think_toks = out_details.get("thinking_tokens")
                        out_toks = usage.get("output_tokens")
                        thoughts: List[str] = []
                        readable_thoughts: List[str] = []  # thoughts without the redacted-thinking placeholders
                        texts: List[str] = []
                        has_tool_use = False
                        for part in content:
                            if not isinstance(part, dict):
                                continue
                            ptype = part.get("type")
                            if ptype == "thinking":
                                if part.get("thinking"):
                                    thoughts.append(str(part["thinking"]).strip())
                                    readable_thoughts.append(thoughts[-1])
                                else:
                                    meta_parts: List[str] = []
                                    if think_dur_ms is not None:
                                        meta_parts.append(f"{int(think_dur_ms)}ms")
                                    if think_toks is not None:
                                        meta_parts.append(f"thinking_tokens={think_toks}")
                                    if out_toks is not None:
                                        meta_parts.append(f"output_tokens={out_toks}")
                                    thoughts.append(
                                        f"[Thinking completed ({', '.join(meta_parts) or 'encrypted signature'})]"
                                    )
                            elif ptype == "text" and part.get("text"):
                                texts.append(str(part["text"]).strip())
                            elif ptype == "tool_use":
                                has_tool_use = True
                                turn_has_step = True
                                tname = str(part.get("name") or "claude_tool")
                                tinput = part.get("input") if isinstance(part.get("input"), dict) else (part.get("input") or {})
                                cmd_val = ""
                                if isinstance(tinput, dict):
                                    cmd_val = str(
                                        tinput.get("command")
                                        or tinput.get("file_path")
                                        or tinput.get("path")
                                        or tinput.get("url")
                                        or ""
                                    )
                                session._inflight_transcript_tool = {
                                    "tool_name": tname,
                                    "command": cmd_val,
                                    "args": tinput,
                                    "call_id": str(part.get("id") or ""),
                                }
                                await self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "claude_jsonl",
                                        "hook_event": "PreToolUse",
                                        "tool_name": tname,
                                        "tool_input": tinput,
                                        "call_id": part.get("id"),
                                    },
                                )
                                if tname == "AskUserQuestion" and isinstance(tinput, dict):
                                    qs = tinput.get("questions") or []
                                    q0 = qs[0] if qs and isinstance(qs[0], dict) else tinput
                                    q_header = str(q0.get("header") or "Claude Question")
                                    q_prompt = str(q0.get("question") or "Claude Sub-Agent is asking a question")
                                    raw_opts = q0.get("options") or []
                                    opt_labels = [
                                        str(o.get("label") or o) if isinstance(o, dict) else str(o)
                                        for o in raw_opts
                                    ]
                                    await self.register_pending_interaction(
                                        session=session,
                                        kind="question",
                                        title=q_header,
                                        prompt=q_prompt,
                                        options=opt_labels + ["Other (custom text input)"],
                                        source="claude_jsonl_ask_user_question",
                                        request_id=str(part.get("id") or f"claude-q-{uuid.uuid4().hex[:6]}"),
                                        metadata={
                                            "raw_options_count": len(opt_labels),
                                            "multi_select": bool(q0.get("multiSelect", False)),
                                        },
                                    )
                        if not turn_has_step and (thoughts or texts):
                            turn_has_step = True
                            req_model = str(msg.get("model") or session.effective_model or "claude")
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "claude_jsonl",
                                    "hook_event": "ReportStep",
                                    "tool_name": "llm_inference",
                                    "tool_input": {"model": req_model},
                                    "step_title": f"LLM inference ({req_model})",
                                },
                            )
                        if texts:
                            turn_last_text = "\n".join(texts).strip()
                        elif thoughts and session.status == "running":
                            session.current_activity = (
                                f"Step {max(1, session.step_count)}: {thoughts[-1].strip('[]')} — streaming response"
                            )
                        inter = texts
                        is_end_turn = (
                            not has_tool_use
                            and stop_reason in ("end_turn", "stop_sequence")
                        )
                        if is_end_turn:
                            turn_completed_emitted = True
                        final_txt = "\n".join(texts).strip() if is_end_turn else None
                        await self.record_reasoning_parts(session.session_id, "claude_jsonl", readable_thoughts)
                        if inter or is_end_turn:
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "claude_jsonl",
                                    "hook_event": "TurnComplete" if is_end_turn else "AssistantMessage",
                                    "intermediate_messages": inter,
                                    "final_assistant_text": final_txt,
                                },
                            )
                    elif otype == "system" and obj.get("subtype") == "turn_duration":
                        dur_ms = obj.get("durationMs")
                        if not turn_completed_emitted and session.pending_interaction is None:
                            turn_completed_emitted = True
                            fallback_final = turn_last_text or ""
                            await self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "claude_jsonl",
                                    "hook_event": "TurnComplete",
                                    "intermediate_messages": [],
                                    "final_assistant_text": fallback_final,
                                },
                            )
                        if dur_ms is not None and session.status == "completed":
                            session.current_activity = (
                                f"Step {max(1, session.step_count)}: Turn completed in {float(dur_ms) / 1000.0:.1f}s (final response captured)"
                            )
                            await self.persist_session(session)

                now_ts = time.time()
                if (
                    session.status == "running"
                    and not turn_completed_emitted
                    and (now_ts - last_screen_probe_at) >= 2.4
                ):
                    last_screen_probe_at = now_ts
                    scr_txt = session.get_clean_screen_text(max_lines=35)
                    if scr_txt:
                        sp_match = spinner_stat_re.search(scr_txt)
                        clean_lines = [
                            ln.strip()
                            for ln in sanitize_tui_screen_text(scr_txt, max_lines=10, engine="claude")
                            if ln.strip() and not ln.strip().startswith((">", "❯"))
                        ]
                        latest_line = clean_lines[-1] if clean_lines else ""
                        if sp_match:
                            verb = sp_match.group(1).strip()
                            stats = sp_match.group(2).strip()
                            tail_preview = f" — {latest_line[:60]}" if latest_line else ""
                            session.current_activity = (
                                f"Step {max(1, session.step_count)}: {verb} ({stats}){tail_preview}"
                            )
                        elif latest_line:
                            session.current_activity = (
                                f"Step {max(1, session.step_count)}: Streaming response — {latest_line[:70]}"
                            )
            except PersistenceUnavailable:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.8)

    @classmethod
    def _tui_input_line(cls, screen_text: str, engine: str = "") -> Optional[str]:
        """W2/K2: the text on the engine's composer ("" when it is empty), or None when none is drawn.

        The real captures show three different composers:
          kimi   a framed box: "│ > …" on its first row, the wrapped rest of the text on the rows
                 below it, then "╰───╯" (height varies with the task text)
          omp    a bare ❯ row under a rule, with status lines below it
          codex  a bare › row (placeholder "Ask Codex to do anything") above its status line
        Looking only at the last non-empty line (the old code) always found a status line instead.
        K2 (fix 9): a box is located by its frame, so the whole text of a composer of any height is
        returned; the old bottom-rows search missed the "│ > " row of a tall box altogether.
        K7 (fix 9d): the same for omp/agy/codex, whose long text wraps onto the rows under the
        prompt row (_tui_wrapped_glyph_composer).
        """
        lines = [ln.rstrip() for ln in str(screen_text or "").splitlines()]
        span = cls._tui_composer_span(lines, engine)
        return span[0] if span is not None else None

    @classmethod
    def _tui_composer_span(cls, lines: List[str], engine: str = "") -> Optional[tuple]:
        """K2 (fix 9): (text, first_row, last_row, wrapped_rows) of the composer, or None.

        first_row/last_row index `lines` and cover the composer's whole drawing (a box's borders
        included); wrapped_rows counts the text rows below its first one, which is how much taller
        it is than a one-line composer. The composer is searched from the bottom: its lowest row
        (a box's bottom border) must lie in the bottom TUI_PROMPT_SCAN_ROWS rows, as before, while
        the composer itself may reach any height above that. K7 (fix 9d): for a glyph composer
        (TUI_GLYPH_COMPOSER_FRAMES) the span is the prompt row and its wrapped rows; its closing
        row is the one that must lie in the bottom rows.
        """
        eng = (engine or "").strip().lower()
        window = max(0, len(lines) - TUI_PROMPT_SCAN_ROWS)
        if TUI_COMPOSER_SHAPES.get(eng) == "box":
            framed = cls._tui_framed_box_composer(lines, window)
            if framed is not None:
                return framed
            # An early or synthetic screen whose box is not closed yet: the "│ > " row in the
            # bottom rows, plus the box rows that follow it.
            for i in range(len(lines) - 1, window - 1, -1):
                match = TUI_INPUT_BOX_RE.match(lines[i]) or TUI_INPUT_BOX_OPEN_RE.match(lines[i])
                if match:
                    last = i
                    while last + 1 < len(lines) and TUI_BOX_SIDE_RE.match(lines[last + 1]):
                        last += 1
                    parts = [match.group(1).rstrip()] + [
                        cls._tui_box_row_text(row) for row in lines[i + 1:last + 1]
                    ]
                    return " ".join(p for p in parts if p), i, last, last - i
            # Only the boxed composer is kimi's input row; a ❯ row above it is submitted history.
            # A synthetic/early screen may show only a bare ❯ row. Accept the LAST ❯ row when
            # every non-empty row below it is wrapped text (a long task wraps onto the following
            # screen rows); never accept it when chrome follows, because then the ❯ is restored
            # history in the middle of the transcript.
            for i in range(len(lines) - 1, window - 1, -1):
                if not lines[i].strip():
                    continue
                match = TUI_INPUT_GLYPH_RE.match(lines[i])
                if match is None:
                    # Possibly a wrapped continuation row of a ❯ above it: keep scanning upward.
                    continue
                below = [ln.strip() for ln in lines[i + 1:] if ln.strip()]
                if any(not cls._tui_plain_continuation(ln) for ln in below):
                    return None
                return match.group(1).rstrip(), i, i, 0
            return None
        frame = TUI_GLYPH_COMPOSER_FRAMES.get(eng)
        if frame is not None:
            return cls._tui_wrapped_glyph_composer(lines, window, frame)
        for i in range(len(lines) - 1, window - 1, -1):
            match = TUI_INPUT_GLYPH_RE.match(lines[i])
            if match:
                return match.group(1).rstrip(), i, i, 0
        return None

    @classmethod
    def _tui_wrapped_glyph_composer(
        cls, lines: List[str], window: int, frame: Dict[str, str]
    ) -> Optional[tuple]:
        """K7 (fix 9d): a glyph composer (omp/agy/codex) with the rows its text wrapped onto.

        Only the LOWEST prompt row on screen is a candidate; every prompt row above it is submitted
        history. A prompt glyph indented like a wrapped row (column 2 or more) is part of the text
        ("a > b" broken before the ">"), not a prompt. The rows under the prompt row that are
        indented at least as far as its text are its wrapped rows, and they count only when the
        engine's closing row (TUI_GLYPH_COMPOSER_FRAMES "bottom") follows them with no answer row
        (TUI_GLYPH_HISTORY_BELOW_RE) further down -- a history row is followed by its answer.
        A prompt row in the bottom TUI_PROMPT_SCAN_ROWS rows is the composer as before (one row when
        no closed frame is found); one above them only with its whole frame ("top" row right above
        it as well) and the closing row inside the bottom rows, the K2 rule for kimi's box.
        """
        for i in range(len(lines) - 1, -1, -1):
            match = TUI_INPUT_GLYPH_RE.match(lines[i])
            if match is None or len(lines[i]) - len(lines[i].lstrip()) >= 2:
                continue
            indent = max(2, match.start(1))
            opened = not frame.get("top") or (i > 0 and cls._tui_frame_row_is(lines[i - 1], frame["top"]))
            j = i + 1
            if opened and frame.get("bottom") == "rule":
                while (
                    j < len(lines)
                    and (j - i) <= 40
                    and bool(lines[j].strip())
                    and not TUI_RULE_ROW_RE.match(lines[j])
                    and not TUI_GLYPH_HISTORY_BELOW_RE.match(lines[j])
                ):
                    j += 1
            else:
                while j < len(lines) and cls._tui_glyph_wrapped_row(lines[j], indent):
                    j += 1
            closed = (
                j < len(lines)
                and cls._tui_frame_row_is(lines[j], frame.get("bottom") or "")
                and not any(TUI_GLYPH_HISTORY_BELOW_RE.match(ln) for ln in lines[j + 1:])
            )
            if closed and (not frame.get("require_frame") or opened) and (i >= window or (opened and j >= window)):
                parts = [match.group(1).rstrip()] + [ln.strip() for ln in lines[i + 1:j]]
                return " ".join(p for p in parts if p), i, j - 1, j - 1 - i
            # C3: when a tall wrapped message (e.g. agy 29-row paste with `> ↑ 58 more lines`) has a
            # valid top rule (`opened`) and fills all remaining rows to the bottom of the screen
            # (`j == len(lines)`), its closing bottom rule has scrolled below the last visible row.
            if (
                not closed
                and opened
                and frame.get("bottom") == "rule"
                and j == len(lines)
                and (j - 1 - i) >= 3
                and j >= window
            ):
                parts = [match.group(1).rstrip()] + [ln.strip() for ln in lines[i + 1:j]]
                return " ".join(p for p in parts if p), i, j - 1, j - 1 - i
            if i >= window and not frame.get("require_frame"):
                return match.group(1).rstrip(), i, i, 0
            break

        # C3: when a tall wrapped message (>36 rows) overflows the 40-row screen, the opening rule
        # and prompt row scroll above row 0 while the closing rule row still sits in the bottom
        # `window` rows and every row above it is a wrapped continuation row of the composer.
        if frame.get("bottom") == "rule":
            for j in range(len(lines) - 1, max(window - 1, 9), -1):
                if not cls._tui_frame_row_is(lines[j], "rule"):
                    continue
                if any(TUI_GLYPH_HISTORY_BELOW_RE.match(ln) for ln in lines[j + 1:]):
                    break
                all_wrapped = all(
                    bool(lines[k].strip())
                    and not TUI_RULE_ROW_RE.match(lines[k])
                    and not TUI_GLYPH_HISTORY_BELOW_RE.match(lines[k])
                    for k in range(j)
                )
                if all_wrapped:
                    parts = [ln.strip() for ln in lines[:j] if ln.strip()]
                    return " ".join(parts), 0, j - 1, j - 1
                break
        return None

    @staticmethod
    def _tui_glyph_wrapped_row(row: str, indent: int) -> bool:
        """K7 (fix 9d): a row the composer's text wrapped onto: text indented at least `indent`."""
        return bool(row.strip()) and len(row) - len(row.lstrip()) >= indent and not TUI_RULE_ROW_RE.match(row)

    @staticmethod
    def _tui_frame_row_is(row: str, kind: str) -> bool:
        """K7 (fix 9d): the row is the frame row TUI_GLYPH_COMPOSER_FRAMES names ("" matches any)."""
        if kind == "rule":
            return bool(TUI_RULE_ROW_RE.match(row))
        if kind == "blank":
            return not row.strip()
        return not kind

    @classmethod
    def _tui_framed_box_composer(cls, lines: List[str], window: int) -> Optional[tuple]:
        """K2 (fix 9): the lowest closed box whose first row is "│ > …", read from its frame.

        Every bottom border inside the bottom rows is tried from the bottom up; a box whose first
        row is not a "│ > " row (kimi's Welcome box) is skipped. Its text rows are joined with one
        space: kimi wraps at spaces and drops the space it wrapped at.
        """
        for bottom in range(len(lines) - 1, window - 1, -1):
            if not TUI_BOX_BOTTOM_RE.match(lines[bottom]):
                continue
            top = bottom - 1
            while top >= 0 and TUI_BOX_SIDE_RE.match(lines[top]):
                top -= 1
            if top < 0 or top + 1 >= bottom or not TUI_BOX_TOP_RE.match(lines[top]):
                continue
            body = lines[top + 1:bottom]
            first = TUI_INPUT_BOX_RE.match(body[0]) or TUI_INPUT_BOX_OPEN_RE.match(body[0])
            if first is None:
                continue
            parts = [first.group(1).rstrip()] + [cls._tui_box_row_text(row) for row in body[1:]]
            return " ".join(p for p in parts if p), top, bottom, len(body) - 1
        return None

    @staticmethod
    def _tui_box_row_text(row: str) -> str:
        """K2 (fix 9): the text inside one box row, without its side bars and padding."""
        text = row.strip()
        if text[:1] in ("│", "┃", "|"):
            text = text[1:]
        if text[-1:] in ("│", "┃", "|"):
            text = text[:-1]
        return text.strip()

    @staticmethod
    def _tui_plain_continuation(line: str) -> bool:
        """True for a wrapped continuation row, False for chrome (response bullet, box, status)."""
        return not line.startswith((
            "●", "✗", "•", "╭", "╰", "│", "┃", "|", "Never Ask", "context:",
            "MCP server", "Working", "thinking", "? for shortcuts",
        ))

    @classmethod
    def _tui_prompt_visible(cls, screen_text: str, engine: str = "") -> bool:
        """V2(a)/W2: the engine's input row is on screen, so typing lands in its composer."""
        return cls._tui_input_line(screen_text, engine) is not None

    @classmethod
    def _tui_busy_indicator(cls, screen_text: str, engine: str = "") -> str:
        """K1: the startup/busy indicator currently on screen, or "" when the engine is settled.

        Only the bottom rows are read: a startup banner still visible far above the composer (kimi's
        welcome box, omp's MCP list) does not mean the engine is still starting, and a transcript row
        that merely quotes "Working…" must not hold readiness back forever.
        K2 (fix 9): the bottom rows are counted as if the composer were one line tall, so the rows
        right above a tall composer ("⠋ Preparing Kimi Code…" over kimi's 12-row box) are still
        read; the composer's own rows are not (they hold the typed text, not an indicator).
        """
        eng = (engine or "").strip().lower()
        lines = [ln.rstrip() for ln in str(screen_text or "").splitlines()]
        span = cls._tui_composer_span(lines, eng)
        wrapped = span[3] if span is not None else 0
        first = max(0, len(lines) - TUI_PROMPT_SCAN_ROWS - wrapped)
        rows = [
            ln for idx, ln in enumerate(lines)
            if idx >= first and (span is None or not span[1] <= idx <= span[2])
        ]
        for phrase in TUI_BUSY_PHRASES.get(eng, ()):
            if any(phrase in ln for ln in rows):
                return phrase
        line_re = TUI_BUSY_LINE_RES.get(eng)
        if line_re is not None:
            for ln in rows:
                if line_re.match(ln):
                    return "spinner"
        return ""

    @classmethod
    def _tui_ready(cls, screen_text: str, engine: str = "") -> tuple:
        """V2(a)/W2/K1: (ready, reason) for the composer AND against the startup indicators.

        K1 is the second half of readiness: every engine draws its composer before it accepts input,
        so a visible composer is necessary but never sufficient. The caller still owns the "screen
        did not change for TUI_READY_STABLE_SEC" half.
        """
        if cls._tui_input_line(screen_text, engine) is None:
            return False, "no_prompt"
        busy = cls._tui_busy_indicator(screen_text, engine)
        if busy:
            return False, f"busy:{busy}"
        return True, "stable_prompt"

    @staticmethod
    def _tui_agent_active(screen_text: str) -> bool:
        """W3/K2: the screen shows the agent working or answering, which confirms a submit."""
        return bool(TUI_AGENT_ACTIVE_RE.search(str(screen_text or "")))

    @classmethod
    def _tui_blocking_dialog(cls, screen_text: str, session: Any = None) -> str:
        """D3: the known trust/update dialog on screen ("" when there is none).

        A dialog the hub already registered as a structured interaction counts as known too: an
        Enter there would answer that dialog for the agent instead of submitting the task.
        """
        if session is not None and getattr(session, "pending_interaction", None):
            return "pending_interaction"
        text = str(screen_text or "")
        for phrase in TUI_TRUST_DIALOG_PHRASES + TUI_UPDATE_DIALOG_PHRASES:
            if phrase in text:
                return phrase
        return ""

    @staticmethod
    def _composer_holds(line: Optional[str], probe: str, tail: str) -> bool:
        """True when the located composer row still shows part of the typed text.

        A long task wraps: its START sits on the composer's first row, while a horizontally
        scrolled composer (codex) may instead show its END. Either proves the text is still there.
        K2 (fix 9): whitespace is ignored on both sides, because a wrapped composer's rows are joined
        with one space wherever the engine broke the line, which may not be where the text had one.
        """
        if line is None:
            return False
        flat = re.sub(r"\s+", "", line)
        for part in (probe, tail):
            key = re.sub(r"\s+", "", part or "")
            if key and (key in flat or (len(key) >= 12 and key[:-2] in flat)):
                return True
        return False

    @staticmethod
    def _tui_paste_placeholder_holds(engine: str, line: Optional[str], typed_len: int) -> bool:
        """K7 (fix 9d): the composer shows the engine's paste placeholder for the typed text.

        codex draws a long typed burst as "[Pasted Content N chars]"; N summed over every such
        placeholder on the composer must be within TUI_PASTE_PLACEHOLDER_SLACK_CHARS of the length
        the hub typed (after its newline flattening). claude draws bracketed paste bursts > 800
        chars as "[Pasted text #1]" (single-line) or "[Pasted text #1 +N lines]" without a char
        count. Engines without a placeholder never match.
        """
        eng = (engine or "").strip().lower()
        pattern = TUI_PASTE_PLACEHOLDER_RES.get(eng)
        if pattern is None or line is None or typed_len <= 0:
            return False
        if eng == "claude":
            return bool(pattern.search(line))
        if eng == "agy":
            if not pattern.search(line):
                return False
            counts = [int(n) for n in pattern.findall(line) if str(n).isdigit()]
            return (
                not counts
                or any(abs(c - typed_len) <= max(TUI_PASTE_PLACEHOLDER_SLACK_CHARS, 16) for c in counts)
                or abs(sum(counts) - typed_len) <= max(TUI_PASTE_PLACEHOLDER_SLACK_CHARS, 16)
            )
        counts = [int(n) for n in pattern.findall(line) if str(n).isdigit()]
        if eng == "codex" and counts:
            # ConPTY may split a paste: the composer contains a literal prefix plus a placeholder.
            # Counting the placeholder alone never reaches the expected total in that case.
            literal_len = len(pattern.sub("", line).strip())
            return abs(sum(counts) + literal_len - typed_len) <= TUI_PASTE_PLACEHOLDER_SLACK_CHARS
        return bool(counts) and abs(sum(counts) - typed_len) <= TUI_PASTE_PLACEHOLDER_SLACK_CHARS

    @classmethod
    def _composer_holds_typed(
        cls, line: Optional[str], probe: str, tail: str, engine: str = "", typed_len: int = 0
    ) -> bool:
        """K7 (fix 9d): _composer_holds, or the engine's paste placeholder standing for the text."""
        return cls._composer_holds(line, probe, tail) or cls._tui_paste_placeholder_holds(
            engine, line, typed_len
        )

    @staticmethod
    def _tui_submit_proof_rules(engine: str = "") -> Dict[str, Any]:
        """K3 (fix 9): the engine's submit-proof rules (TUI_SUBMIT_PROOF_RULES over the defaults)."""
        eng = (engine or "").strip().lower()
        return {**TUI_DEFAULT_SUBMIT_PROOF, **TUI_SUBMIT_PROOF_RULES.get(eng, {})}

    @staticmethod
    def _tui_reject_count(screen_text: str, rules: Dict[str, Any]) -> int:
        """K3 (fix 9): how many of the engine's refusals of an Enter are on screen."""
        text = str(screen_text or "")
        return sum(text.count(phrase) for phrase in rules.get("reject_phrases") or ())

    @classmethod
    def _tui_history_rows_holding(cls, screen_text: str, glyph: str, probe: str) -> int:
        """K3 (fix 9): submitted-message rows ("❯ …" for kimi) on screen holding the text's start."""
        if not glyph or not probe:
            return 0
        count = 0
        for row in str(screen_text or "").splitlines():
            row = row.strip()
            if row.startswith(glyph) and cls._composer_holds(row[len(glyph):], probe, ""):
                count += 1
        return count

    @classmethod
    def _tui_submit_marks(
        cls, session: Any, screen_text: str, engine: str, probe: str
    ) -> Dict[str, int]:
        """K3 (fix 9): the evidence already present before an Enter; only what the Enter adds counts.

        An "Error: LLM not set" line from an earlier refused Enter stays on kimi's screen, a resumed
        kimi redraws the old "❯ …" rows, and the transcript may already hold user messages.
        """
        rules = cls._tui_submit_proof_rules(engine)
        return {
            "rejects": cls._tui_reject_count(screen_text, rules),
            "history": cls._tui_history_rows_holding(screen_text, rules.get("history_glyph") or "", probe),
            "user_seq": int(getattr(session, "transcript_user_seq", 0) or 0),
            # K6 (fix 9c): a working indicator already up before the Enter (a live message to an
            # agent mid-turn, codex's "Worked for" row of the last turn) proves nothing about it.
            "active": cls._tui_agent_active(screen_text),
        }

    def _probe_transcript_for_user_submit(
        self,
        session: Any,
        size_before: int,
        probe: str,
        tail: str,
        expected_text: Optional[str] = None,
    ) -> bool:
        """Directly scans bytes appended to session.transcript_path since size_before for a matching user message."""
        tpath = getattr(session, "transcript_path", None)
        if not tpath:
            return False
        try:
            p = Path(tpath)
            if not p.is_file() or p.stat().st_size <= max(0, int(size_before)):
                return False
            with open(p, "rb") as f:
                f.seek(max(0, int(size_before)))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return False

        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            otype = obj.get("type")
            candidates: List[str] = []
            # 1) Claude: type == "user" or queue-operation enqueue
            if otype == "user":
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                c = msg.get("content")
                if isinstance(c, str) and c.strip():
                    candidates.append(c.strip())
                elif isinstance(c, list):
                    txts = [
                        str(b.get("text") or "")
                        for b in c
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                    ]
                    if txts:
                        candidates.append(" ".join(txts).strip())
            elif otype == "queue-operation" and obj.get("operation") == "enqueue" and obj.get("content"):
                candidates.append(str(obj.get("content")).strip())
            # 2) AGY: type == "USER_INPUT"
            elif otype == "USER_INPUT" and obj.get("content"):
                candidates.append(str(obj.get("content")).strip())
            # 3) OMP: type == "message" and message.role == "user"
            elif otype == "message":
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                if msg.get("role") == "user":
                    c = msg.get("content")
                    if isinstance(c, str) and c.strip():
                        candidates.append(c.strip())
                    elif isinstance(c, list):
                        txts = [
                            str(b.get("text") or "")
                            for b in c
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                        ]
                        if txts:
                            candidates.append(" ".join(txts).strip())
            # 4) Codex: type == "response_item" and payload.type == "message" and payload.role == "user"
            elif otype == "response_item":
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                if payload.get("type") == "message" and payload.get("role") == "user":
                    parts = payload.get("content") or []
                    txts = [
                        str(b.get("text") or "")
                        for b in parts
                        if isinstance(b, dict)
                        and b.get("text")
                        and "<environment_context>" not in str(b.get("text"))
                    ]
                    if txts:
                        candidates.extend(txts)
            # 5) Kimi: type == "turn.prompt" and origin.kind == "user"
            elif otype == "turn.prompt" and (obj.get("origin") or {}).get("kind") == "user":
                parts = obj.get("input") or []
                txts = [str(b.get("text") or "") for b in parts if isinstance(b, dict) and b.get("text")]
                if txts:
                    candidates.append(" ".join(txts).strip())

            for txt in candidates:
                matches = (txt.replace("\r\n", "\n").strip() == expected_text.replace("\r\n", "\n").strip()
                           if expected_text is not None else self._composer_holds(txt, probe, tail))
                if txt and matches:
                    if hasattr(session, "note_transcript_user_message"):
                        session.note_transcript_user_message(txt)
                    return True
        return False

    def _reconcile_codex_submit(self, session: AgentSession) -> bool:
        draft = getattr(session, "_tui_draft_pending", None) or {}
        if draft.get("expected_text") and draft.get("attempts") == 1 and self._probe_transcript_for_user_submit(
            session, draft["size_before"], "", "", expected_text=draft["expected_text"]
        ):
            session._tui_draft_pending = None
            return True
        return False

    def _capture_submit_diagnostic(self, session: AgentSession, reason: str) -> Optional[str]:
        """Keep bounded ANSI evidence locally; never return private screen contents in the tool reply."""
        try:
            target = self.data_dir / "diagnostics" / f"submit-{uuid.uuid4().hex}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            with session._lock:
                evidence = {"session_id": session.session_id, "reason": reason, "at": time.time(),
                            "cols": session.cols, "rows": session.rows,
                            "screen_cols": session.screen.columns, "screen_rows": session.screen.lines,
                            "raw_ansi_tail": "".join(session.raw_history)[-65536:]}
            target.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            return str(target)
        except OSError:
            logger.warning("Could not save submit diagnostic for %s", session.session_id)
            return None

    @staticmethod
    def _codex_composer_empty(session: AgentSession, line: Optional[str]) -> bool:
        if line is None:
            return False
        if not line.strip():
            return True
        # The native placeholder is painted after a cursor at column 3. A typed copy of
        # the same words has its cursor after the text, so must still count as a draft.
        return line.strip() == "Ask Codex to do anything" and session.screen.cursor.x == 2

    async def _clear_tui_draft(self, session: Optional[AgentSession]) -> Dict[str, Any]:
        if session is None or session.engine != "codex" or not session.is_process_alive():
            raise ValueError("clear_input requires a live Codex TUI session")
        claim = self._tui_writer_queue(session).claim("clear_input")
        try:
            await claim.wait_turn()
            await self.poll_tui_screen_interactions_once(session)
            if session.pending_interaction or session.status not in ("completed", "idle"):
                raise ValueError("Cannot clear input during an approval or active turn")
            screen = session.get_clean_screen_text(max_lines=40)
            line = self._tui_input_line(screen, "codex")
            if line is None or self._tui_blocking_dialog(screen, session):
                raise ValueError("Cannot locate a safe Codex composer to clear")
            if self._codex_composer_empty(session, line):
                if getattr(session, "_tui_draft_pending", None):
                    raise ValueError("Draft state is uncertain; inspect the terminal before clearing")
                return {"delivered": False, "reason": "input_already_empty", "pty_written": False,
                        "injection": "none", "session_id": session.session_id}
            # Only a visible nonempty idle draft receives Ctrl+C. Never send it to an empty box.
            if not session.write_input("\x03"):
                raise ValueError("Failed to write clear-input key")
            blank_since = None
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                await asyncio.sleep(.1)
                line = self._tui_input_line(session.get_clean_screen_text(max_lines=40), "codex")
                if self._codex_composer_empty(session, line) and session.is_process_alive():
                    blank_since = blank_since or time.monotonic()
                    if time.monotonic() - blank_since >= .5:
                        session._tui_draft_pending = None
                        return {"delivered": False, "reason": "input_cleared", "pty_written": True,
                                "injection": "none", "session_id": session.session_id,
                                "input_cleared": True}
                else:
                    blank_since = None
            session._tui_draft_pending = {"reason": "clear_unconfirmed"}
            return {"delivered": False, "reason": "clear_unconfirmed", "pty_written": True,
                    "injection": "none", "session_id": session.session_id,
                    "input_cleared": False}
        finally:
            claim.release()

    async def _submit_codex_text(self, session: AgentSession, text: str, submit_key: str) -> Dict[str, Any]:
        """One paste, at most one Enter, and exact native user-message proof. Never retry Enter."""
        if getattr(session, "_tui_draft_pending", None) and not self._reconcile_codex_submit(session):
            return {"ok": False, "reason": "input_recovery_required", "attempts": 0, "label": "失败"}
        clean = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not clean:
            return {"ok": False, "reason": "empty_text", "attempts": 0, "label": "失败"}
        screen = session.get_clean_screen_text(max_lines=40)
        line = self._tui_input_line(screen, "codex")
        if session.pending_interaction or self._tui_blocking_dialog(screen, session) or line is None:
            return {"ok": False, "reason": "composer_unavailable", "attempts": 0, "label": "失败"}
        if not self._codex_composer_empty(session, line):
            session._tui_draft_pending = {"reason": "existing_draft"}
            return {"ok": False, "reason": "input_recovery_required", "attempts": 0, "label": "失败"}
        try:
            if not session.transcript_path:
                raise FileNotFoundError("Native transcript is not attached")
            size = Path(session.transcript_path).stat().st_size
        except OSError:
            return {"ok": False, "reason": "native_transcript_unavailable", "attempts": 0, "label": "失败"}
        draft = {"expected_text": clean, "size_before": size, "attempts": 0}
        session._tui_draft_pending = draft
        if not session.write_input(f"\x1b[200~{clean}\x1b[201~"):
            session._tui_draft_pending = None
            return {"ok": False, "reason": "write_failed", "attempts": 0, "label": "失败"}
        screen, echoed, settled, waited = await self._await_typed_text_settled(
            session, "codex", clean[:30], clean[-30:], typed_len=len(clean), expected_text=clean)
        extra = {"echo_settled": settled, "echo_wait_sec": round(waited, 2), "echoed": echoed,
                 "pty_written": True, "extra_press": False}
        reason = "typed_text_not_settled"
        if settled and not self._tui_blocking_dialog(screen, session) and not session.pending_interaction:
            if session.write_input(submit_key):
                draft["attempts"] = 1
                deadline = time.monotonic() + TUI_SUBMIT_EVIDENCE_SEC
                while time.monotonic() < deadline:
                    if self._reconcile_codex_submit(session):
                        return {"ok": True, "reason": "transcript_user_message", "attempts": 1,
                                "label": "成功", **extra}
                    await asyncio.sleep(TUI_SUBMIT_CONFIRM_POLL_SEC)
                reason = "unconfirmed"
            else:
                reason = "submit_write_failed"
        return {"ok": None if draft["attempts"] else False, "reason": reason,
                "attempts": draft["attempts"], "label": "未确认", "recovery_required": True,
                "diagnostic_path": self._capture_submit_diagnostic(session, reason), **extra}

    def _tui_transcript_proof(
        self,
        session: Any,
        rules: Dict[str, Any],
        size_before: int,
        marks: Dict[str, Any],
        probe: str,
        tail: str,
    ) -> str:
        """K3 (fix 9): the transcript's proof of this submit ("" when it shows none yet)."""
        if rules.get("transcript") == "exact_user_message":
            expected = marks.get("expected_text")
            return "transcript_user_message" if expected is not None and self._probe_transcript_for_user_submit(
                session, size_before, probe, tail, expected_text=expected) else ""
        seq = int(getattr(session, "transcript_user_seq", 0) or 0)
        if seq > int(marks.get("user_seq") or 0) and self._composer_holds(
            str(getattr(session, "transcript_user_text", "") or ""), probe, tail
        ):
            return "transcript_user_message"
        if self._probe_transcript_for_user_submit(session, size_before, probe, tail):
            return "transcript_user_message"
        if rules.get("transcript") == "user_message":
            return ""
        if session.transcript_path:
            try:
                # transcript_grew also covers "the claimed conversation gained the user turn":
                # on a resume the conversation was claimed before the text was typed, so only
                # bytes appended after this Enter prove this turn arrived.
                if Path(session.transcript_path).stat().st_size > size_before:
                    return "transcript_grew"
            except OSError:
                pass
        return ""

    async def _confirm_tui_submit(
        self,
        session: AgentSession,
        probe: str,
        tail: str,
        size_before: int,
        engine: str = "",
        echoed: bool = True,
        deadline: Optional[float] = None,
        marks: Optional[Dict[str, Any]] = None,
        typed_len: int = 0,
    ) -> tuple:
        """V2(b)/W3/K2/K3: confirmation of one Enter, on positive evidence only.

        ("confirmed", reason) needs one of these proofs (per engine, TUI_SUBMIT_PROOF_RULES):
          (a) the text was SEEN on the composer after typing (echoed) and that composer is now
              located and no longer holds it -- an empty composer proves nothing on its own when
              the text was never seen there, which is exactly how round 3 reported success while
              the whole task still sat in kimi's box. K3 (fix 9): never for kimi, which clears
              its box when it refuses an Enter;
          (b) the transcript: it grew, or (kimi) its watcher read a new user message with the text;
          (c) K3 (fix 9): a new submitted-message row ("❯ …") holding the text; or
          (d) the engine's working indicator is on screen (K6, fix 9c: and was not before the Enter).
        ("rejected", phrase) K3 (fix 9): a new refusal (kimi "Error: LLM not set") is on screen.
        ("unsubmitted", reason) only when the text is POSITIVELY still on the located input row.
        ("unknown", reason) when nothing decides it yet. D3 then lets the caller press Enter once
        more, but only with no known trust/update dialog on screen (see _submit_text_to_tui).
        `marks` holds what was there before the Enter (_tui_submit_marks); the last screen read is
        written back into it, as the next Enter's starting point.
        K7 (fix 9d): `typed_len` lets codex's "[Pasted Content N chars]" row count as the text
        still sitting on its composer, so that row is never taken for a cleared composer.
        """
        rules = self._tui_submit_proof_rules(engine)
        marks = marks if marks is not None else {}
        # K2: when the text was never seen on the composer, an empty composer proves nothing, so the
        # look keeps watching for ~15 s instead of deciding. A seen echo keeps the shorter W3 window,
        # except where a cleared composer is no proof at all (K3): then only the transcript, a history
        # row or activity can decide, and those take seconds to show (1.7-7 s for kimi's wire file).
        # The caller may pass a deadline, so retrying cannot stack one full window on top of another.
        long_window = (not echoed) or (not rules.get("cleared_composer"))
        window = TUI_SUBMIT_EVIDENCE_SEC if long_window else TUI_SUBMIT_CONFIRM_SEC
        started = time.time()
        limit = started + window
        if deadline is not None:
            limit = min(limit, deadline)
        if rules.get("transcript") == "exact_user_message":
            # Supported engines have native user records. Do not read screen contents to
            # confirm their submission, even when a screen would offer an earlier answer.
            while time.time() < limit:
                proof = self._tui_transcript_proof(session, rules, size_before, marks, probe, tail)
                if proof:
                    return "confirmed", proof
                await asyncio.sleep(TUI_SUBMIT_CONFIRM_POLL_SEC)
            return "unknown", "unconfirmed"
        last_line: Optional[str] = None
        while time.time() < limit:
            await asyncio.sleep(TUI_SUBMIT_CONFIRM_POLL_SEC)
            screen = session.get_clean_screen_text(max_lines=40)
            marks["screen"] = screen
            last_line = self._tui_input_line(screen, engine)
            if self._tui_reject_count(screen, rules) > int(marks.get("rejects") or 0):
                phrase = next((p for p in rules.get("reject_phrases") or () if p in screen), "rejected")
                return "rejected", phrase
            holds = self._composer_holds_typed(last_line, probe, tail, engine, typed_len)
            if echoed and rules.get("cleared_composer") and last_line is not None and not holds:
                return "confirmed", "input_line_cleared"
            proof = self._tui_transcript_proof(session, rules, size_before, marks, probe, tail)
            if proof:
                return "confirmed", proof
            glyph = rules.get("history_glyph") or ""
            if glyph and self._tui_history_rows_holding(screen, glyph, probe) > int(marks.get("history") or 0):
                return "confirmed", "history_row"
            if rules.get("transcript") != "exact_user_message" and not marks.get("active") and self._tui_agent_active(screen):
                return "confirmed", "agent_active"
            if holds and (not echoed or (long_window and time.time() - started >= TUI_SUBMIT_CONFIRM_SEC)):
                # K2: the text the hub typed is visibly sitting on the composer while nothing
                # indicates work -- "seen still unsent", so the caller may press Enter again (the
                # first Enter was swallowed because the engine was still starting up). A text that
                # was seen before the Enter gets the W3 window first, as it always had.
                return "unsubmitted", "still_in_input_line"
        if last_line is None:
            screen = session.get_clean_screen_text(max_lines=40)
            marks["screen"] = screen
            last_line = self._tui_input_line(screen, engine)
        if self._composer_holds_typed(last_line, probe, tail, engine, typed_len):
            return "unsubmitted", "still_in_input_line"
        return "unknown", "unconfirmed"

    @classmethod
    def _tui_typed_tail_shown(cls, lines: List[str], span: Optional[tuple], tail: str) -> bool:
        """K5 (fix 9b): the END of the typed text is drawn on the composer or the rows it wrapped onto.

        The end is what proves the engine took in the whole typed burst: a long text's start scrolls
        out of kimi's box ("↑ 2 more" on its top border). K7 (fix 9d): a glyph composer's span holds
        its wrapped rows when its frame is closed; otherwise it is only its first row, so the rows
        below it are read too. Rows above the
        composer (submitted history) never count. Whitespace is ignored, as in _composer_holds.
        """
        key = re.sub(r"\s+", "", tail or "")
        if span is None or not key:
            return False
        below = "".join(lines[span[2] + 1:])
        haystack = re.sub(r"\s+", "", span[0] + below)
        return key in haystack or (len(key) >= 12 and key[:-2] in haystack)

    async def _await_typed_text_settled(
        self, session: AgentSession, engine: str, probe: str, tail: str, typed_len: int = 0,
        expected_text: Optional[str] = None,
    ) -> tuple:
        """K5 (fix 9b): wait until the typed text has settled on the composer; (screen, echoed, settled, waited).

        Settled = the end of the text is shown (_tui_typed_tail_shown) and the composer text has not
        changed for TUI_TYPE_SETTLE_SEC. Only the composer is watched, not the whole screen: a
        spinner or status timer elsewhere (omp's working row, kimi's MCP lines) would otherwise keep
        the wait running until the cap on every submit. After TUI_TYPE_SETTLE_CAP_SEC the caller
        presses Enter anyway, as it always did, with settled=False. `echoed` is whether the last
        look held the text (its start or its end) on the composer.
        K7 (fix 9d): the composer of every known engine is read whole (kimi's box, the wrapped rows
        of omp/agy/codex), and codex's paste placeholder for the typed length counts as the end
        shown, so the cap is a safety net only: reaching it means detection failed, and it warns.
        """
        started = time.time()
        cap = started + max(TUI_TYPE_SETTLE_CAP_SEC, TUI_TYPE_SETTLE_SEC,
                            min(30.0, typed_len * .01) if engine == "codex" else 0)
        quiet_key: Any = object()
        quiet_since = started
        while True:
            await asyncio.sleep(TUI_TYPE_SETTLE_POLL_SEC)
            screen = session.get_clean_screen_text(max_lines=40)
            lines = [ln.rstrip() for ln in str(screen or "").splitlines()]
            span = self._tui_composer_span(lines, engine)
            line = span[0] if span is not None else None
            shown = self._tui_typed_tail_shown(lines, span, tail) or self._tui_paste_placeholder_holds(
                engine, line, typed_len
            )
            if engine == "codex" and expected_text is not None:
                # Repeated tails can appear before ConPTY has consumed the whole write.
                shown = (re.sub(r"\s+", "", line or "") == re.sub(r"\s+", "", expected_text)
                         or self._tui_paste_placeholder_holds(engine, line, typed_len))
            now = time.time()
            key = (line, shown)
            if key != quiet_key:
                quiet_key, quiet_since = key, now
            echoed = shown or self._composer_holds(line, probe, tail)
            if shown and now - quiet_since >= TUI_TYPE_SETTLE_SEC:
                return screen, echoed, True, now - started
            if now >= cap:
                logger.warning(
                    "K5: %s typed text never settled on the composer after %.1fs",
                    getattr(session, "session_id", "?"), now - started,
                )
                return screen, echoed, False, now - started

    async def _submit_text_to_tui(
        self,
        session: AgentSession,
        text: str,
        submit_key: str,
        prior: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """V2(b)(c)/W3/K2: type the text, press Enter, and accept the submit only on evidence.

        K2: the first look at the composer after typing is not allowed to decide anything on its own.
        `echoed` records whether the text was really SEEN on the located composer; an empty composer
        with echoed=False (kimi's box was still booting, then the screen redrew) proves nothing, so
        the check keeps watching for TUI_SUBMIT_EVIDENCE_SEC. If the text then appears on the
        composer with nothing indicating work, one more Enter is pressed (within the same retry
        budget).

        D3: when the evidence window ends with nothing proved (unknown), the hub presses Enter once
        more and watches TUI_SUBMIT_EXTRA_PRESS_SEC. That press counts toward
        TUI_SUBMIT_MAX_TRIES, is skipped entirely while a known trust/update dialog is on screen,
        and if the outcome is still unknown the result stays ok:null / "unconfirmed" with
        extra_press=true so last_injection shows that the extra press really happened.

        K3 (fix 9): when the engine refuses an Enter (kimi "Error: LLM not set") the text is gone
        from its composer. The result then carries retype=true while Enters remain: the caller waits
        for readiness again and calls this once more with `prior` (the earlier result), so the
        attempts, the one D3 extra press and the retype count all continue across the retype.

        K5 (fix 9b): the first Enter is pressed only once the typed text has settled on the composer
        (_await_typed_text_settled), not after a fixed pause; an Enter that reaches kimi while it is
        still taking in a long typed burst becomes a newline in its box. The result carries
        echo_settled / echo_wait_sec, and `echoed` is that settled look.
        """
        if str(session.engine or "").lower() == "codex":
            return await self._submit_codex_text(session, text, submit_key)
        clean = str(text).replace("\r", " ").replace("\n", " ").strip()
        if not clean:
            return {"ok": False, "reason": "empty_text", "attempts": 0, "label": "失败"}
        size_before = 0
        if session.transcript_path:
            try:
                size_before = Path(session.transcript_path).stat().st_size
            except OSError:
                size_before = 0
        engine = session.engine
        payload_to_write = (
            f"\x1b[200~{clean}\x1b[201~"
            if str(engine or "").strip().lower() in ("claude", "agy")
            else clean
        )
        session.write_input(payload_to_write)
        probe = clean[:30].strip()
        tail = clean[-30:].strip()
        # K5 (fix 9b): the first Enter waits for the typed text to settle on the composer.
        screen, echoed, settled, waited = await self._await_typed_text_settled(
            session, engine, probe, tail, typed_len=len(clean)
        )
        prior = prior or {}
        attempts = int(prior.get("attempts") or 0)
        extra_press = bool(prior.get("extra_press"))
        retyped = int(prior.get("retyped") or 0)
        extra: Dict[str, Any] = {"retyped": retyped} if retyped else {}
        # K5: how long the first Enter waited, and whether the echo settled or the cap ran out.
        extra["echo_settled"] = settled
        extra["echo_wait_sec"] = round(waited, 2)
        # K2/K5: echoed is the settled look right before the first Enter, and it is what the result
        # reports. The later presses may additionally be judged by (a) once the text has really been
        # seen sitting on the composer -- that is what turns "the box looked empty while kimi was
        # still booting" into one more Enter and a real success instead of one never earned.
        seen = echoed
        # K2: one evidence budget for the whole submit, not one per Enter.
        evidence_deadline = time.time() + TUI_SUBMIT_EVIDENCE_SEC
        # K3 (fix 9): the screen last read before each Enter is that Enter's starting point.
        marks: Dict[str, Any] = {"screen": screen, "expected_text": clean}
        while attempts < TUI_SUBMIT_MAX_TRIES:
            attempts += 1
            marks.update(self._tui_submit_marks(session, marks.get("screen") or "", engine, probe))
            session.write_input(submit_key)
            state, reason = await self._confirm_tui_submit(
                session, probe, tail, size_before, engine, echoed=seen,
                deadline=evidence_deadline, marks=marks, typed_len=len(clean),
            )
            if state == "confirmed":
                return {"ok": True, "reason": reason, "attempts": attempts, "echoed": echoed,
                        "extra_press": extra_press, "label": "成功", **extra}
            if state == "rejected":
                # K3 (fix 9): the engine refused this Enter and cleared its composer; the text is
                # not on screen any more, so pressing Enter again would submit nothing.
                result = {"ok": False, "reason": "rejected_by_engine", "rejected": reason,
                          "attempts": attempts, "echoed": echoed, "extra_press": extra_press,
                          "label": "失败", **extra}
                if attempts < TUI_SUBMIT_MAX_TRIES:
                    result["retype"] = True
                return result
            if state == "unknown":
                if self._tui_submit_proof_rules(engine).get("transcript") == "exact_user_message":
                    return {"ok": None, "reason": "unconfirmed", "attempts": attempts,
                            "echoed": echoed, "extra_press": False, "label": "未确认", **extra}
                # D3: nothing proved either way. Press Enter once more and watch a short window --
                # but never while a known trust/update dialog is on screen, because that Enter
                # would answer the dialog instead of submitting the task.
                marks["screen"] = session.get_clean_screen_text(max_lines=40)
                dialog = self._tui_blocking_dialog(marks["screen"], session)
                if extra_press or attempts >= TUI_SUBMIT_MAX_TRIES or dialog:
                    result = {"ok": None, "reason": "unconfirmed", "attempts": attempts,
                              "echoed": echoed, "extra_press": extra_press,
                              "dialog": dialog or None, "label": "未确认", **extra}
                    if extra_press:
                        # The extra press was made and the short D3 window still proved nothing.
                        result["detail"] = "checked_after_extra_press"
                    return result
                extra_press = True
                # The next loop iteration is that one extra Enter; it counts toward the attempt
                # limit and gets the short D3 window instead of the long evidence one.
                evidence_deadline = time.time() + TUI_SUBMIT_EXTRA_PRESS_SEC
                continue
            # "unsubmitted": the text is visibly still on the input row, so one more Enter is safe.
            # K2's one evidence budget for the whole submit stays in force here; only the D3 extra
            # press gets a window of its own.
            seen = True
            await asyncio.sleep(TUI_SUBMIT_RETRY_STEP_SEC * attempts)
        return {"ok": False, "reason": "text_still_in_input_line", "attempts": attempts,
                "echoed": echoed, "extra_press": extra_press, "label": "失败", **extra}

    @staticmethod
    def _tui_ready_banner(session: Any) -> str:
        """K1 (fix 9): the banner this startup waits for ("" for none): fresh spawns only.

        A resume is never gated on it (see TUI_READY_BANNERS): kimi -S draws no composer before its
        Welcome box, which then scrolls away within seconds.
        """
        if int(getattr(session, "resume_count", 0) or 0) > 0:
            return ""
        return TUI_READY_BANNERS.get(str(getattr(session, "engine", "") or "").strip().lower(), "")

    @staticmethod
    def _tui_banner_visible(session: Any, screen_text: str, banner: str) -> bool:
        """K1 (fix 9): the banner is on screen or in the recent scrollback (it may scroll off fast)."""
        if banner in str(screen_text or ""):
            return True
        scrollback = getattr(session, "get_full_scrollback_text", None)
        if scrollback is None:
            return False
        try:
            return banner in scrollback(max_lines=TUI_READY_BANNER_HISTORY_ROWS)
        except Exception:
            return False

    async def _record_tui_injection(self, session: AgentSession, result: Dict[str, Any]) -> None:
        """W3/K6: last_injection, the pane's red/grey line and the persisted row for one typed text.

        K6 (fix 9c): a live message (mode "parent_message") is recorded here too, under its own name;
        an unchecked one (an unknown composer) gets no grey line, as nothing was checked.
        """
        session.last_injection = result
        what = "消息提交" if result.get("mode") == "parent_message" else "启动文本注入"
        delivery_status = "confirmed" if result.get("ok") is True else (
            "failed" if result.get("ok") is False else "unconfirmed"
        )
        await self.record_worker_event(session.session_id, {
            "type": "input_delivery",
            "category": f"delivery_{delivery_status}",
            "status": delivery_status,
            "content": str(result.get("reason") or result.get("label") or delivery_status),
            "request_id": result.get("submit_id"),
        })
        if session.engine == "codex" and session.pty and result.get("ok") is not True:
            # Native TUIs redraw differences against their own screen. Hub-added ANSI moves
            # the emulator's cursor without moving that native screen and can introduce drift.
            logger.warning("[%s] %s: %s", session.session_id, what, result.get("reason"))
            await self.record_worker_event(session.session_id, {
                "type": "input_delivery", "status": "warning", "content": result.get("reason", "unconfirmed"),
                "request_id": result.get("submit_id"),
            })
        elif result.get("ok") is False:
            # W3: the red line is for a CONFIRMED failure only.
            session.emit_virtual_ansi(
                f"\r\n\x1b[1;31m✖ {what}失败（{result.get('reason')}，尝试 {result.get('attempts')} 次）\x1b[0m\r\n"
            )
        elif result.get("ok") is None and result.get("reason") != "live_write_unchecked":
            session.emit_virtual_ansi(
                f"\r\n\x1b[90m… {what}未确认（unconfirmed）\x1b[0m\r\n"
            )
        try:
            await self.persist_session(session)
        except PersistenceUnavailable:
            raise
        except Exception:
            pass

    async def _auto_handle_tui_startup_and_trust(
        self,
        session: AgentSession,
        task_to_inject: Optional[str],
        startup_delay: float = 2.5,
        submit_key: str = "\r",
    ):
        """R1/W4: one dialog watcher for every TUI engine for the whole startup window.

        R1 restored the pre-04dee6f behaviour: an engine with nothing to type (agy/codex/omp argv
        tasks) still watches the trust dialog instead of returning after 500 ms (agy then processed
        its -i prompt behind the dialog forever). W4: the same watcher runs during the readiness
        wait, so a dialog that appears later is answered, and the task text is never typed while a
        known dialog is on screen. W3: the injection outcome is recorded honestly in last_injection.

        K1 (fix 9): a fresh spawn of an engine with a ready banner (kimi's Welcome box) also waits
        for that banner; if it has not shown TUI_READY_BANNER_FALLBACK_SEC after the trust dialog
        was answered (or after spawn), the gate warns and falls back to composer + busy + stable.
        K3 (fix 9): an Enter the engine refused is followed by a new wait for readiness and the
        whole text typed again, within the same attempt limit.
        K6 (fix 9c): with a text to type, the startup holds the session's writer queue from the
        start until that text's outcome is recorded, so a live message sent meanwhile is typed
        after it, never into the middle of it or into a TUI that is still booting.
        """
        claim = self._tui_writer_queue(session).claim("startup_task") if task_to_inject else None
        try:
            if claim is not None:
                await claim.wait_turn()
            await self._watch_tui_startup_and_type(session, task_to_inject, startup_delay, submit_key)
        finally:
            if claim is not None:
                claim.release()

    async def _watch_tui_startup_and_type(
        self,
        session: AgentSession,
        task_to_inject: Optional[str],
        startup_delay: float,
        submit_key: str,
    ):
        """The startup window itself (see _auto_handle_tui_startup_and_trust)."""
        start = time.time()
        deadline = start + TUI_STARTUP_WINDOW_SEC
        typed = not task_to_inject
        trust_dismissed = 0
        # The same dialog is often redrawn (resize, spinner); press Enter once per distinct
        # dialog text, not once per poll, so a slow redraw can never receive a second Enter.
        last_dialog: Optional[str] = None
        last_text: Optional[str] = None
        stable_since = time.time()
        ready: Dict[str, Any] = {"ready": False, "reason": "no_screen"}
        banner = "" if typed else self._tui_ready_banner(session)
        banner_anchor = start
        banner_seen_at: Optional[float] = None
        banner_fallback = False
        # K3 (fix 9): the refused submit that is waiting to be typed again, if any.
        rejected: Optional[Dict[str, Any]] = None
        retypes = 0

        while session.status in ("starting", "running"):
            await asyncio.sleep(TUI_DIALOG_POLL_SEC)
            if session.status not in ("starting", "running"):
                return
            now = time.time()
            screen_txt = session.get_clean_screen_text(max_lines=40)
            if any(phrase in screen_txt for phrase in TUI_TRUST_DIALOG_PHRASES):
                # R1/W4: answer it wherever it appears in the window; never type behind it.
                if TUI_TRUST_NO_FOCUSED_RE.search(screen_txt):
                    # Wait past Claude's 150ms held->settled remount guard before moving focus down
                    # from "No, exit" to "Yes, I trust this folder", and NEVER press Enter while
                    # "No" is still the focused option.
                    await asyncio.sleep(TUI_TRUST_OPEN_GUARD_SEC)
                    if session.status not in ("starting", "running"):
                        return
                    session.write_input("\x1b[B")
                    await asyncio.sleep(0.25)
                    if session.status not in ("starting", "running"):
                        return
                    screen_txt = session.get_clean_screen_text(max_lines=40)
                if (
                    any(phrase in screen_txt for phrase in TUI_TRUST_DIALOG_PHRASES)
                    and not TUI_TRUST_NO_FOCUSED_RE.search(screen_txt)
                    and screen_txt != last_dialog
                ):
                    session.write_input("\r")
                    last_dialog = screen_txt
                    trust_dismissed += 1
                last_text = None
                stable_since = time.time()
                # K1 (fix 9): the banner's fallback clock starts when the last dialog is answered.
                banner_anchor = time.time()
                await asyncio.sleep(TUI_TRUST_CONFIRM_SETTLE_SEC)
                continue
            last_dialog = None
            if screen_txt != last_text:
                last_text = screen_txt
                stable_since = now
            input_line = self._tui_input_line(screen_txt, session.engine)
            stable = (now - stable_since) >= TUI_READY_STABLE_SEC
            prompted, prompt_reason = self._tui_ready(screen_txt, session.engine)
            if banner and banner_seen_at is None and not banner_fallback:
                if self._tui_banner_visible(session, screen_txt, banner):
                    banner_seen_at = now
                elif now >= min(banner_anchor + TUI_READY_BANNER_FALLBACK_SEC,
                                deadline - TUI_READY_BANNER_RESERVE_SEC):
                    # K1 (fix 9): an engine update may have changed the banner; do not hang the spawn.
                    banner_fallback = True
                    logger.warning(
                        "[%s] %r did not appear within %.0f s of startup; typing on the composer/busy/"
                        "stable gate instead", session.session_id, banner, now - banner_anchor,
                    )
            if not screen_txt.strip():
                ready = {"ready": False, "reason": "no_screen"}
            elif not prompted:
                # K1: a composer alone is not readiness. The engine draws it while still booting
                # ("⠋ Preparing Kimi Code…", "Connecting to MCP servers:", "Starting MCP servers")
                # and swallows the Enter typed then, so the reason names the indicator that is up.
                ready = {"ready": False, "reason": prompt_reason}
            elif banner and banner_seen_at is None and not banner_fallback:
                # K1 (fix 9): kimi's bare composer before its Welcome box is not ready either.
                ready = {"ready": False, "reason": "waiting_banner"}
            elif not stable:
                ready = {"ready": False, "reason": "waiting_stable"}
            else:
                ready = {"ready": True, "reason": "stable_prompt", "elapsed_sec": round(now - start, 1)}
            if banner:
                ready["banner"] = banner
                ready["banner_seen_sec"] = (
                    round(banner_seen_at - start, 1) if banner_seen_at is not None else None
                )
                if banner_fallback:
                    ready["banner_fallback"] = True

            if not typed and ready["ready"] and (now - start) >= startup_delay:
                prior = None if rejected is None else dict(rejected, retyped=retypes)
                result = await self._submit_text_to_tui(session, task_to_inject, submit_key, prior=prior)
                result["ready"] = ready
                result["trust_dismissed"] = trust_dismissed
                result["at"] = round(time.time(), 3)
                result["mode"] = "startup_task"
                if result.pop("retype", False):
                    # K3 (fix 9): the engine refused the Enter and cleared its box. Wait for the
                    # readiness gate again (a fresh stable period at least), then type it all anew.
                    rejected = result
                    retypes += 1
                    logger.warning(
                        "[%s] the engine refused the startup text (%s) after %s Enter(s); typing it "
                        "again once ready", session.session_id, result.get("rejected"),
                        result.get("attempts"),
                    )
                    last_text = None
                    stable_since = time.time()
                    continue
                await self._record_tui_injection(session, result)
                # W4: the text is typed; the readiness wait it could have been lost in is over.
                return

            # R1: stop early only once the agent is clearly past the dialogs (prompt visible or
            # conversation claimed). With text still to type the ready branch above owns the exit.
            # For claude, conversation_ref is pre-seeded with --session-id before launch, so only a
            # claimed transcript file (or visible composer) proves it has passed startup dialogs.
            if typed and (
                input_line is not None
                or (session.conversation_ref and (session.engine != "claude" or self._session_has_transcript(session)))
            ):
                return
            if now >= deadline:
                if not typed:
                    if rejected is not None:
                        # K3 (fix 9): refused, and never ready again for the retype: a real failure.
                        result = dict(rejected)
                        result.update({
                            "label": "失败", "at": round(time.time(), 3), "ready": ready,
                            "detail": "not_ready_again_before_deadline",
                        })
                        await self._record_tui_injection(session, result)
                        return
                    await self._record_tui_injection(session, {
                        "ok": None, "reason": "unconfirmed", "attempts": 0,
                        "mode": "startup_task", "at": round(time.time(), 3),
                        "label": "未确认", "ready": ready, "trust_dismissed": trust_dismissed,
                    })
                return

    def _crash_tail(self, sess: AgentSession) -> str:
        """The last ≤15 lines / ≤1500 chars of output, as the crash event carries them (spec §5)."""
        text = sess.last_screen or self._last_screen_text(sess)
        lines = [ln for ln in text.splitlines() if ln.strip()][-15:]
        tail = "\n".join(lines)
        if not tail:
            return "(no output)"
        return tail[-1500:]

    def _exit_event_content(self, session_id: str, exit_code: int, sess: Optional[AgentSession]) -> str:
        resumable = bool(sess.resumable) if sess is not None else False
        hint = f"Call get_subagent_final_response('{session_id}') for the persisted answer and trace."
        if resumable:
            hint += f" Call resume_subagent('{session_id}') to resume this session."
        if sess is not None and sess.end_reason == "crashed":
            return f"Session crashed (exit_code={exit_code}). Last output:\n{self._crash_tail(sess)}\n{hint}"
        return f"Session exited (exit_code={exit_code}). {hint}"

    async def _on_session_exit(self, session: Any, exit_code: int):
        """One exited process → at most one lifecycle event for its session id (A7).

        The callback carries the session object: after a resume replaced it, the old object's exit
        must not touch the new session's row, transcript claim or status. A string id is still
        accepted for an older caller.
        """
        obj = session if isinstance(session, AgentSession) else None
        session_id = obj.session_id if obj is not None else str(session)
        sess = self.sessions.get(session_id)
        if obj is not None and sess is not obj:
            if sess is None:
                rec = await self.db.get_session_record(session_id)
                if rec and rec.get("archived_at"):
                    # close_session already wrote the final record and emitted its one event.
                    self._release_transcripts(session_id)
                    self._user_terminated.discard(session_id)
            # Either a resume already owns this id or the session is gone: this exit is history.
            return
        if sess is not None and sess.engine in ("codex", "claude", "omp", "kimi", "agy") and not sess.terminated_by_user:
            # Engines can flush the last JSONL row just after the PTY closes. Let the existing
            # watcher drain it before classifying/persisting the exit and releasing ownership.
            if sess.ended_at is None:
                sess.ended_at = time.time()
            remaining = sess.ended_at + NATIVE_TRANSCRIPT_DRAIN_SEC + 0.1 - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            if self.sessions.get(session_id) is not sess:
                return
            sess.end_reason = sess.classify_end_reason(exit_code)
        if session_id in self._user_terminated or (sess and sess.terminated_by_user):
            # V-5: the user termination already emitted its one lifecycle event.
            # Q12: the process is gone; let go of its transcript and of the one-event marker.
            self._release_transcripts(session_id)
            self._user_terminated.discard(session_id)
            return
        if sess is None:
            rec = await self.db.get_session_record(session_id)
            if rec and rec.get("archived_at"):
                # close_session already wrote the final record and emitted its one event.
                self._release_transcripts(session_id)
                self._user_terminated.discard(session_id)
                return
        end_reason = "exited"
        if sess is not None:
            sess.exit_code = exit_code
            self._finalize_session(sess)
            # A7: the object is the live one, so its status follows its own end state (the PTY reader
            # normally did this already; doing it here keeps the callback self-contained).
            if sess.end_reason:
                sess.status = sess.end_reason
            # The final record writes the scrollback and the mailbox thread once, here (spec §3, A11).
            await self.persist_session(sess, force_scrollback=True, final=True)
            end_reason = sess.end_reason or "exited"
        ev_type = "crashed" if end_reason == "crashed" else "exited"
        await self.record_worker_event(
            session_id,
            {
                "type": ev_type,
                "category": "lifecycle",
                "status": end_reason,
                "content": self._exit_event_content(session_id, exit_code, sess),
            },
        )
        # Q12: the exit event is out. The process ended on its own, so its conversation is nobody's
        # live transcript any more and the one-lifecycle-event marker must not outlive it.
        self._release_transcripts(session_id)
        self._user_terminated.discard(session_id)

    async def _prepare_dsh_workspace(self, cwd: str, *, repair: bool = True) -> Dict[str, Any]:
        return await asyncio.to_thread(
            prepare_dsh_workspace, cwd, dsh_base_url(), self.data_dir, repair=repair,
        )

    async def _require_dsh_workspace(self, session: AgentSession) -> Dict[str, Any]:
        preparation = await self._prepare_dsh_workspace(session.cwd)
        session.sandbox_preparation = preparation
        session.profile_snapshot["sandbox_preparation"] = preparation
        await self.persist_session(session)
        if not preparation["allowed"]:
            raise ValueError(preparation["reason"])
        return preparation

    async def _spawn_dsh_hub_job(self, session: AgentSession, task: Optional[str]):
        """Spawns a first-class in-host DSH session via <DSH host>/_dsh/dsh-crew/jobs (dsh_base_url)."""
        if session.terminated_by_user:
            # Q4: the user terminated the session before this task got its first turn.
            return
        spec = session.dsh_spec
        provider = spec.get("provider", "clay")
        model = spec.get("model", "deepseek-flash")
        tier = spec.get("tier", "flash")
        effort = spec.get("effort", "high")

        dsh_host = dsh_base_url().split("://", 1)[-1]
        box_w = max(60, min(int(session.cols or 120) - 2, 140))
        inner_w = max(40, box_w - 18)
        banner_head = f"╭─── DeepSeek Harness (In-Host Session @ {dsh_host}) "
        model_info = f"{provider} / {model} (tier={tier}, effort={effort})"
        banner = (
            f"\x1b[1;36m{banner_head}{'─' * max(1, box_w - len(banner_head))}╮\x1b[0m\r\n"
            f"\x1b[1;36m│\x1b[0m Hub Session : \x1b[1;33m{session.session_id:<{inner_w}}\x1b[1;36m│\x1b[0m\r\n"
            f"\x1b[1;36m│\x1b[0m Model Route : \x1b[1;32m{model_info[:inner_w]:<{inner_w}}\x1b[1;36m│\x1b[0m\r\n"
            f"\x1b[1;36m│\x1b[0m Workspace   : {session.cwd[:inner_w]:<{inner_w}}\x1b[1;36m│\x1b[0m\r\n"
            f"\x1b[1;36m╰{'─' * (box_w - 1)}╯\x1b[0m\r\n"
        )
        session.emit_virtual_ansi(banner)

        if not task or not task.strip():
            session.sandbox_preparation = await self._prepare_dsh_workspace(session.cwd, repair=False)
            session.profile_snapshot["sandbox_preparation"] = session.sandbox_preparation
            session.emit_virtual_ansi(
                "\x1b[90m✦ Ready. Type a task below or call send_to_subagent to dispatch into DSH Web Host.\x1b[0m\r\n> "
            )
            await self.persist_session(session)
            return

        try:
            await self._require_dsh_workspace(session)
        except ValueError as error:
            if session.terminated_by_user:
                return
            session.status = "exited"
            session.end_reason = "crashed"
            session.ended_at = time.time()
            session.current_activity = str(error)
            session.emit_virtual_ansi(f"\x1b[1;31m✖ {error}\x1b[0m\r\n")
            await self.persist_session(session)
            await self.record_worker_event(session.session_id, {
                "type": "error", "category": "lifecycle", "status": "failed",
                "content": str(error), "sandbox_preparation": session.sandbox_preparation,
            })
            return
        if session.terminated_by_user:
            return

        token = str(uuid.uuid4())
        payload = {
            "task": task.strip(),
            "tier": tier,
            "effort": effort,
            "provider": provider,
            "model": model,
            "cwd": session.cwd,
            "messaging_token": token,
            "source": "mcp-agent-hub",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(dsh_url("/_dsh/dsh-crew/jobs"), json=payload)
                data = r.json()
                if not r.is_success or not data.get("ok"):
                    err_msg = data.get("error") or f"HTTP {r.status_code}"
                    if session.terminated_by_user:
                        # Q4: a failed spawn must not rewrite a session the user already terminated.
                        return
                    session.emit_virtual_ansi(f"\x1b[1;31m✖ DSH Spawn Error: {err_msg}\x1b[0m\r\n")
                    session.status = "exited"
                    await self.persist_session(session)
                    return
                job = data["job"]
                job_id = job["id"]
                dsh_sid = job.get("sessionId", "")
                if session.terminated_by_user:
                    # Q4: the termination raced the spawn request. Clean up the job that was created,
                    # but leave the session's status, persistence and events exactly as terminate()
                    # left them.
                    threading.Thread(
                        target=lambda: httpx.post(
                            dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}/cancel"), timeout=3.0
                        ),
                        daemon=True,
                    ).start()
                    return
                session.dsh_job_id = job_id
                session.dsh_session_id = dsh_sid
                session.dsh_token = token
                # Spec §3/§4: the dsh conversation reference is the job handle the continue API
                # takes; R5 also keeps the job's messaging token so /messages still works after a
                # hub restart.
                session.conversation_ref = {
                    "dsh_job_id": job_id,
                    "dsh_session_id": dsh_sid,
                    "dsh_token": token,
                }
                session.status = "running"
                session.current_activity = f"DSH Job {job_id} running in {dsh_sid}"
                session.emit_virtual_ansi(
                    f"\x1b[1;35m🚀 Spawned DSH Job:\x1b[0m \x1b[1m{job_id}\x1b[0m  "
                    f"\x1b[90m(DSH Web Session: {dsh_sid})\x1b[0m\r\n"
                    f"\x1b[90m❯ {task.strip()}\x1b[0m\r\n"
                )
                await self.persist_session(session)
                await self._watch_dsh_hub_job(session, job_id, token)
        except PersistenceUnavailable:
            raise
        except Exception as e:
            if session.terminated_by_user:
                # Q4: a spawn that fails after the user terminated must not resurrect the session.
                return
            session.emit_virtual_ansi(f"\x1b[1;31m✖ Failed to reach DSH Host on {dsh_host}: {e}\x1b[0m\r\n")
            session.status = "exited"
            await self.persist_session(session)

    async def _open_dsh_session_record(
        self, session: AgentSession, job_id: str, *, reattach: bool = False
    ) -> Optional["_DshRecordCursor"]:
        """This DSH session's native record, or None while DSH has not created it (yet)."""
        if not session.dsh_session_id:
            return None
        path = await asyncio.to_thread(locate_dsh_session_record, session.dsh_session_id)
        if path is None:
            return None
        ref = session.conversation_ref if isinstance(session.conversation_ref, dict) else {}
        try:
            after_seq = int(ref.get(DSH_RECORD_SEQ_KEY, -1))
            # The byte position only saves re-reading; it is meaningless without the sequence.
            start_offset = int(ref.get(DSH_RECORD_OFFSET_KEY) or 0) if after_seq >= 0 else 0
        except (TypeError, ValueError):
            after_seq, start_offset = -1, 0
        since_ms = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}"), params={"wait": 0})
            started = (resp.json().get("job") or {}).get("startedAt") if resp.is_success else None
            if started:
                # A continued job shares the record with earlier turns that are already recorded.
                since_ms = datetime.datetime.fromisoformat(str(started).replace("Z", "+00:00")).timestamp() * 1000 - 2000
        except Exception:
            since_ms = None
        cursor = _DshRecordCursor(DshSessionRecordReader(path, start_offset=start_offset),
                                  after_seq=after_seq, since_ms=since_ms)
        if reattach and after_seq < 0:
            # A Hub that never stored a position (an older version, or a crash before the first
            # batch was saved) recorded this job from its log: only what DSH writes from now on
            # is new.  Reading the existing events once also learns the open calls' tool names.
            try:
                existing = await asyncio.to_thread(cursor.reader.read_new_events)
            except Exception as error:
                logger.warning("[%s] DSH session record unreadable, using the job log: %s", session.session_id, error)
                return None
            seqs = [int(ev["seq"]) for ev in existing if isinstance(ev.get("seq"), int)]
            cursor.after_seq = max(seqs, default=-1)
            native_events_to_telemetry(existing, after_seq=cursor.after_seq, tool_names=cursor.tool_names)
        return cursor

    async def _drain_dsh_session_record(self, session: AgentSession, cursor: "_DshRecordCursor") -> bool:
        """Record tool calls, results, text and reasoning appended to the DSH session record.

        Returns whether anything new was recorded."""
        if not cursor.active or session.terminated_by_user or self._shutting_down:
            return False
        try:
            events = await asyncio.to_thread(cursor.reader.read_new_events)
            payloads = native_events_to_telemetry(events, after_seq=cursor.after_seq, since_ms=cursor.since_ms,
                                                  tool_names=cursor.tool_names)
        except Exception as error:
            # Corrupt data, a missing decoder or an unexpected record shape: the job log keeps the
            # transcript going (tool names only) from here on, and the watcher keeps running.
            logger.warning("[%s] DSH session record unreadable, using the job log: %s", session.session_id, error)
            cursor.active = False
            return False
        try:
            for payload in payloads:
                if session.terminated_by_user or self._shutting_down:
                    break
                seq = int(payload.pop("native_seq"))
                payload.pop("timestamp", None)
                await self.record_telemetry(session.session_id, payload)
                cursor.after_seq = max(cursor.after_seq, seq)
                # Kept current per payload, so a stop mid-batch never records one twice.
                session.conversation_ref = dict(session.conversation_ref or {}, **{DSH_RECORD_SEQ_KEY: cursor.after_seq})
        finally:
            if payloads or events:
                session.conversation_ref = dict(session.conversation_ref or {}, **{
                    DSH_RECORD_SEQ_KEY: cursor.after_seq, DSH_RECORD_OFFSET_KEY: cursor.reader.resume_offset})
                await self.persist_session(session)
        return bool(payloads)

    async def _settle_dsh_session_record(self, session: AgentSession, cursor: "_DshRecordCursor") -> None:
        """Read the frames DSH writes just after it reports the job finished."""
        await self._drain_dsh_session_record(session, cursor)
        for _ in range(DSH_RECORD_SETTLE_MAX_PASSES):
            if not cursor.active:
                return
            await asyncio.sleep(DSH_RECORD_SETTLE_INTERVAL_SEC)
            if not await self._drain_dsh_session_record(session, cursor):
                return

    async def _watch_dsh_hub_job(self, session: AgentSession, job_id: str, token: str, *,
                                 log_path: Optional[Path] = None, reattach: bool = False):
        """
        Tails ~/.config/dsh-crew/logs/<job_id>.log for real-time steps/tools/thoughts AND
        polls /_dsh/dsh-crew/jobs/<job_id>/messages for bidirectional worker->parent messages & questions!
        """
        if session.terminated_by_user:
            # Q4: never attach, persist or emit for a session the user already terminated.
            return
        if log_path is not None:
            log_file = Path(log_path)
        else:
            default_log = Path(os.path.expanduser("~")) / ".config" / "dsh-crew" / "logs" / f"{job_id}.log"
            # A restricted desktop token may be able to call the DSH API while it cannot read the
            # host's default log directory.  In that case keep the watcher alive and rely on the
            # job/messages endpoints; a writable Hub-local path also gives the transcript a stable
            # location if the host is configured to mirror logs there.
            if _directory_is_writable(default_log.parent):
                log_file = default_log
            else:
                log_file = self.data_dir / "dsh-logs" / f"{job_id}.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
        session.transcript_path = log_file
        await self.persist_session(session)
        tail = TranscriptTail(log_file)
        msg_cursor = 0
        # The DSH session record carries what the job log leaves out (tool arguments and output,
        # full assistant text, reasoning).  The source is chosen once per watcher so a tool is never
        # recorded from both; the job log still drives steps and errors either way.  DSH creates the
        # record shortly after the job starts, so the job log waits until it is found or given up.
        record = await self._open_dsh_session_record(session, job_id, reattach=reattach)
        record_deadline = None if record is not None else time.monotonic() + DSH_RECORD_LOCATE_WAIT_SEC

        async def look_for_record(final: bool = False):
            nonlocal record, record_deadline
            if record is not None or record_deadline is None:
                return
            record = await self._open_dsh_session_record(session, job_id, reattach=reattach)
            if record is not None:
                record_deadline = None
            elif final or time.monotonic() >= record_deadline:
                record_deadline = None
                logger.info("[%s] DSH session record not found; transcript uses the job log", session.session_id)

        async def drain_log_lines():
            try:
                lines = await tail.read_batch()
            except (OSError, PermissionError):
                # Log access is auxiliary; DSH job status and mailbox polling remain authoritative.
                lines = []
            for next_offset, line in lines:
                if session.terminated_by_user or self._shutting_down:
                    return False
                try:
                    ev = json.loads(line)
                except (ValueError, TypeError):
                    logger.warning("Skipping malformed DSH log line for %s", session.session_id)
                    tail.acknowledge(next_offset)
                    continue
                kind = ev.get("kind")
                summary = ev.get("summary", "")
                if kind == "step":
                    s_num = int(ev.get("step") or (session.step_count + 1))
                    t_num = ev.get("turn", 1)
                    session.step_count = max(session.step_count, s_num)
                    session.current_activity = f"Step {s_num} (Turn {t_num})"
                    session.emit_virtual_ansi(f"\r\n\x1b[1;36m▶ [Step {s_num} · Turn {t_num}]\x1b[0m\r\n")
                    await self.persist_session(session)
                elif kind in ("tool_call", "tool_result", "text") and record is not None and record.active:
                    pass  # recorded from the session record with full detail
                elif kind == "tool_call":
                    tname = ev.get("tool") or summary or "dsh_tool"
                    await self.record_telemetry(
                        session.session_id,
                        {
                            "source": "dsh_job_log",
                            "hook_event": "PreToolUse",
                            "tool_name": tname,
                            "tool_input": summary,
                        },
                    )
                    session.emit_virtual_ansi(f"\x1b[33m  ⚙ Tool Call: {tname}\x1b[0m\r\n")
                elif kind == "tool_result":
                    await self.record_telemetry(
                        session.session_id,
                        {
                            "source": "dsh_job_log",
                            "hook_event": "PostToolUse",
                            "tool_response": summary,
                        },
                    )
                    session.emit_virtual_ansi(f"\x1b[32m  ✓ {summary}\x1b[0m\r\n")
                elif kind == "text" and summary:
                    await self.record_telemetry(
                        session.session_id,
                        {
                            "source": "dsh_job_log",
                            "hook_event": "AssistantMessage",
                            "intermediate_messages": [summary],
                            "final_assistant_text": summary,
                        },
                    )
                    session.emit_virtual_ansi(f"\x1b[37m  💬 {summary}\x1b[0m\r\n")
                elif kind == "error" and summary:
                    session.emit_virtual_ansi(f"\x1b[1;31m  ✖ {summary}\x1b[0m\r\n")
                tail.acknowledge(next_offset)
                # A single pyte/history snapshot can be costly. Let socket I/O run
                # between records as well as between the bounded file-read batches.
                await asyncio.sleep(0.001)
            await asyncio.sleep(0)
            return len(lines) == tail.batch_lines

        async with httpx.AsyncClient(timeout=10.0) as client:
            while (
                session.dsh_job_id == job_id
                and not session.terminated_by_user
                and session.status in ("starting", "running", "waiting_parent")
            ):
                await look_for_record()
                if record is not None:
                    await self._drain_dsh_session_record(session, record)
                if record_deadline is None and await drain_log_lines():
                    continue

                # Poll DSH WorkerMailbox for worker->parent messages or blocking questions
                try:
                    m_resp = await client.get(
                        dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}/messages"),
                        params={"after": msg_cursor, "wait": 1},
                        headers={"x-dsh-crew-job-token": token},
                    )
                    if session.terminated_by_user:
                        # Q4: the user terminated while this poll was in flight; stop before any
                        # status / persistence / event change.
                        return
                    if m_resp.is_success:
                        m_res = m_resp.json().get("result") or {}
                        for m in m_res.get("messages") or []:
                            seq = int(m.get("sequence", 0))
                            mkind = m.get("kind")
                            mtext = m.get("text", "")
                            mid = m.get("message_id") or f"dsh-msg-{seq}"
                            if mkind == "message":
                                if m.get("expects_reply"):
                                    # Parse options from DSH worker-question handoff format: `Options: A | B | C`
                                    parsed_opts: List[str] = []
                                    for mline in mtext.splitlines():
                                        if mline.strip().startswith("Options:"):
                                            raw_part = mline.split("Options:", 1)[1]
                                            parsed_opts = [x.strip() for x in raw_part.split("|") if x.strip()]
                                            break
                                    is_sec_approval = (
                                        "[Security mode]" in mtext
                                        or "Allow this call?" in mtext
                                        or ("Allow once" in parsed_opts and "Reject" in parsed_opts)
                                    )
                                    int_kind = "approval_request" if is_sec_approval else "question"
                                    int_title = "DSH Security Mode High-Risk Approval" if is_sec_approval else "DSH Worker Question"
                                    if not parsed_opts and is_sec_approval:
                                        parsed_opts = ["Allow once", "Allow for this session", "Reject"]
                                    session.emit_virtual_ansi(
                                        f"\r\n\x1b[1;35m{'🛡️' if is_sec_approval else '❓'} [DSH Worker {'Approval Request' if is_sec_approval else 'Asking Parent'} (request_id={mid})]:\x1b[0m\r\n"
                                        f"\x1b[35m   {mtext}\x1b[0m\r\n"
                                    )
                                    await self.register_pending_interaction(
                                        session=session,
                                        kind=int_kind,
                                        title=int_title,
                                        prompt=mtext,
                                        options=parsed_opts,
                                        source="dsh_worker_question_handoff",
                                        request_id=mid,
                                        metadata={
                                            "dsh_job_id": job_id,
                                            "dsh_token": token,
                                            "dsh_message_id": mid,
                                            "display_prompt": _dsh_display_prompt(mtext),
                                        },
                                    )
                                else:
                                    session.emit_virtual_ansi(
                                        f"\r\n\x1b[1;34m📨 [DSH Worker Message to Parent]: {mtext}\x1b[0m\r\n"
                                    )
                                    if mtext.lstrip().startswith("[approval granted without asking"):
                                        await self.db.add_intermediate_message(
                                            session.session_id,
                                            session.step_count,
                                            mtext.strip(),
                                            source="dsh_auto_approval_notice",
                                        )
                                    else:
                                        await self.record_worker_event(
                                            session.session_id,
                                            {
                                                "type": "message_to_parent",
                                                "category": "message",
                                                "status": "running",
                                                "content": mtext,
                                            },
                                        )
                            msg_cursor = max(msg_cursor, seq)
                except PersistenceUnavailable:
                    raise
                except Exception:
                    pass

                # Check job status & final result
                try:
                    j_resp = await client.get(dsh_url(f"/_dsh/dsh-crew/jobs/{job_id}"), params={"wait": 0})
                    if session.terminated_by_user:
                        # Q4: same rule for the job-status poll.
                        return
                    if j_resp.is_success:
                        jview = j_resp.json().get("job") or {}
                        jstatus = jview.get("status")
                        if jview.get("step"):
                            session.step_count = max(session.step_count, int(jview["step"]))
                        if jstatus == "needs_input":
                            qs = jview.get("questions") or []
                            if qs and not session.pending_interaction:
                                q0 = qs[0] if isinstance(qs[0], dict) else {}
                                q_header = str(q0.get("header") or "DSH Input Required")
                                q_prompt = str(q0.get("question") or "DSH worker paused waiting for parent input")
                                if q0.get("detail"):
                                    q_prompt += f"\n{q0.get('detail')}"
                                raw_opts = q0.get("options") or []
                                opt_labels = [
                                    str(o.get("label") or o) if isinstance(o, dict) else str(o)
                                    for o in raw_opts
                                ]
                                is_sec = q_header == "Security mode" or "Allow this call?" in q_prompt
                                await self.register_pending_interaction(
                                    session=session,
                                    kind="approval_request" if is_sec else "question",
                                    title=q_header,
                                    prompt=q_prompt,
                                    options=opt_labels,
                                    source="dsh_job_needs_input",
                                    request_id=f"dsh-ni-{job_id}",
                                    metadata={
                                        "dsh_job_id": job_id,
                                        "dsh_token": token,
                                        "dsh_needs_continue": True,
                                    },
                                )
                        elif jstatus in ("done", "failed", "cancelled", "timeout"):
                            if session.terminated_by_user:
                                # Q4: the job ended, but the user's termination is the final word on
                                # this session's status, persistence and events.
                                return
                            await look_for_record(final=True)
                            while await drain_log_lines():
                                pass  # Each batch yields; preserve every trailing telemetry record.
                            if record is not None:
                                await self._settle_dsh_session_record(session, record)
                            full_res = (jview.get("result") or jview.get("error") or "").strip()
                            if full_res:
                                session.set_final_response(full_res, "dsh_hub_completion")
                                await self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "dsh_hub_api",
                                        "hook_event": "TurnComplete",
                                        "intermediate_messages": [full_res],
                                        "final_assistant_text": full_res,
                                    },
                                )
                            session.ended_at = time.time()
                            if jstatus == "done":
                                session.status = "completed"
                                session.end_reason = "exited"
                                session.exit_code = 0
                                session.completion_reported = True
                            else:
                                # Spec §2: a failed/cancelled/timeout DSH job is an abnormal end.
                                session.status = "crashed"
                                session.end_reason = "crashed"
                                session.exit_code = 1
                            session.conversation_ref = _keep_dsh_record_seq(session.conversation_ref, {
                                "dsh_job_id": job_id,
                                "dsh_session_id": session.dsh_session_id or "",
                            })
                            tokens_info = jview.get("tokens") or {}
                            session.current_activity = (
                                f"DSH {jstatus} (steps={session.step_count}, tools={jview.get('toolCalls', 0)}, "
                                f"tokens={tokens_info.get('input', 0)}/{tokens_info.get('output', 0)})"
                            )
                            formatted_res = full_res.replace("\r\n", "\n").replace("\n", "\r\n")
                            done_w = max(60, min(int(session.cols or 120) - 2, 140))
                            done_head = f"╭─── DSH Turn Completed ({jstatus.upper()}) "
                            session.emit_virtual_ansi(
                                f"\r\n\x1b[1;32m{done_head}{'─' * max(1, done_w - len(done_head))}╮\x1b[0m\r\n"
                                f"{formatted_res}\r\n"
                                f"\x1b[1;32m╰{'─' * (done_w - 1)}╯\x1b[0m\r\n"
                                f"\x1b[90m(Session {session.dsh_session_id} stays warm in DSH Host — send input to continue)\x1b[0m\r\n> "
                            )
                            await self.persist_session(session)
                            # TurnComplete above emitted the native result. An empty result still
                            # ends the turn without creating an answer from a status sentence.
                            if not full_res:
                                await self.record_worker_event(
                                    session.session_id,
                                    {
                                        "type": "turn_completed",
                                        "category": "turn_completed",
                                        "status": "turn_idle",
                                        "content": "",
                                    },
                                )
                            break
                except PersistenceUnavailable:
                    raise
                except Exception:
                    pass

                await asyncio.sleep(0.4)

    async def _register_codex_screen_approval(self, session: AgentSession, parsed: Dict[str, Any], scr: str):
        """D7 + round 2 (owner): every option on the screen is offered, each with the action its
        shortcut key means. approve_session sends the key the screen shows for the "don't ask again"
        option: `p` on a command prompt (codex writes a permanent allow rule for that command prefix
        into ~/.codex/rules/default.rules), `a` on an edit prompt (these files, this session)."""
        kind = parsed.get("prompt_kind") or "command"
        if kind == "mcp":
            opts = parsed["options"]
            return await self.register_pending_interaction(
                session=session, kind="approval_request", title="Codex MCP Tool Approval",
                prompt=parsed["header"] + "\n" + parsed["arguments"],
                options=[o["label"] for o in opts], source="codex_tui_screen_detector",
                metadata={"codex_prompt": "mcp", "server": parsed["server"], "tool": parsed["tool"],
                          "arguments": parsed["arguments"], "option_actions": [o["action"] for o in opts],
                          "prompt_identity": self._codex_mcp_identity(parsed),
                          "native_call_id": getattr(session, "_codex_last_tool_call_id", None)},
            )
        opts = [o for o in parsed.get("options") or [] if o.get("action") != "persist"]
        offered = [o["label"] for o in opts]
        actions = [o["action"] for o in opts]
        if not offered:
            offered = ["Yes, proceed (y)", "No, and tell Codex what to do differently (esc)"]
            actions = ["approve_once", "deny"]
        session_key = str(parsed.get("session_key") or "")
        lines = ["Would you like to run the following command?" if kind == "command"
                 else "Would you like to make the following edits?"]
        if parsed.get("reason"):
            lines.append(f"Reason: {parsed['reason']}")
        if parsed.get("command"):
            lines.append(f"$ {parsed['command']}")
        if parsed.get("destination"):
            lines.append(f"Destination: {parsed['destination']}")
        if session_key == "p":
            lines.append(
                "approve_session sends p: permanently allow this command prefix in "
                "~/.codex/rules/default.rules for all Codex sessions."
            )
        elif not session_key:
            lines.append("No session approval option is offered; approve_session approves once.")
        return await self.register_pending_interaction(
            session=session,
            kind="approval_request",
            title="Codex TUI Approval Modal" if kind == "command" else "Codex TUI Edit Approval",
            prompt="\n".join(lines) if len(lines) > 1 else scr.strip()[-600:],
            options=offered,
            source="codex_tui_screen_detector",
            metadata={
                "codex_prompt": kind,
                "command": parsed.get("command") or "",
                "codex_session_key": session_key,
                "option_actions": actions,
            },
        )

    async def _register_agy_screen_approval(self, session: AgentSession, parsed: Dict[str, Any], scr: str):
        """D7: agy's options are offered by label; the one that persists to settings.json never is."""
        opts = [dict(o) for o in parsed.get("options") or []]
        # Round 3: the conversation-wide deny rule (`No, and always deny … in this conversation`) is
        # not offered either; the parent's choices are approve once / for this conversation / deny.
        offered_opts = [o for o in opts if o.get("action") not in ("persist", "deny_session")]
        offered = [o["label"] for o in offered_opts]
        inflight = getattr(session, "_inflight_transcript_tool", None) or {}
        inflight_cmd = str(inflight.get("command") or "").strip()
        screen_cmd = str(parsed.get("command") or "").strip()
        best_cmd = inflight_cmd if (inflight_cmd and len(inflight_cmd) >= len(screen_cmd)) else screen_cmd
        lines: List[str] = []
        if parsed.get("reason"):
            lines.append(f"Reason: {parsed['reason']}")
        if best_cmd:
            what = "MCP tool" if str(parsed.get("header") or "").startswith("Allow calling this tool") \
                else "Requesting permission for"
            lines.append(f"{what}: {best_cmd}")
        if parsed.get("header"):
            lines.append(parsed["header"])
        return await self.register_pending_interaction(
            session=session,
            kind="approval_request",
            title="AGY TUI Permission Prompt",
            prompt="\n".join(lines) if lines else scr.strip()[-600:],
            options=offered,
            source="agy_tui_screen_detector",
            metadata={
                "agy_options": opts,
                "option_actions": [o.get("action") for o in offered_opts],
                "command": best_cmd,
                "transcript_tool": inflight.get("tool_name") if inflight else None,
            },
        )

    async def _register_omp_screen_approval(self, session: AgentSession, parsed: Dict[str, Any]):
        """D7: omp's tool approval box; "Always Approve …" grants persist and are not offered."""
        offered = [o for o in parsed.get("options") or [] if classify_approval_label(o) != "persist"]
        inflight = getattr(session, "_inflight_transcript_tool", None) or {}
        inflight_cmd = str(inflight.get("command") or "").strip()
        screen_cmd = str(parsed.get("command") or "").strip()
        best_cmd = inflight_cmd if (inflight_cmd and len(inflight_cmd) >= len(screen_cmd)) else screen_cmd
        lines = [f"Allow tool: {parsed.get('tool') or inflight.get('tool_name') or '?'}"] + list(parsed.get("details") or [])
        if best_cmd and best_cmd not in "\n".join(lines):
            lines.append(f"Command: {best_cmd}")
        return await self.register_pending_interaction(
            session=session,
            kind="approval_request",
            title=f"OMP Tool Approval ({parsed.get('tool') or inflight.get('tool_name') or 'tool'})",
            prompt="\n".join(lines),
            options=offered,
            source="omp_tui_screen_detector",
            metadata={
                "omp_prompt": "tool",
                "omp_options": list(parsed.get("options") or []),
                "selected_index": int(parsed.get("selected_index") or 0),
                "session_label": parsed.get("session_label"),
                "command": best_cmd,
                "transcript_tool": inflight.get("tool_name") if inflight else None,
            },
        )

    async def _register_claude_screen_approval(self, session: AgentSession, parsed: Dict[str, Any], scr: str):
        """Claude Code permission prompt fallback: options are offered by label."""
        opts = [dict(o) for o in parsed.get("options") or []]
        offered_opts = [o for o in opts if o.get("action") not in ("persist", "deny_session")]
        offered = [o["label"] for o in offered_opts]
        inflight = getattr(session, "_inflight_transcript_tool", None) or {}
        inflight_cmd = str(inflight.get("command") or "").strip()
        screen_cmd = str(parsed.get("command") or "").strip()
        best_cmd = inflight_cmd if (inflight_cmd and len(inflight_cmd) >= len(screen_cmd)) else screen_cmd
        lines: List[str] = []
        if parsed.get("reason"):
            lines.append(f"Reason: {parsed['reason']}")
        if best_cmd:
            lines.append(f"Command: {best_cmd}")
        if parsed.get("header"):
            lines.append(parsed["header"])
        persists_opt = next((o for o in offered_opts if o.get("action") == "approve_session" and o.get("persists")), None)
        has_session_opt = any(o.get("action") == "approve_session" for o in offered_opts)
        if persists_opt is not None:
            lines.append(
                f"approve_session sends {persists_opt.get('key', '2')}: persist this command's "
                "allow rule in the project's .claude/settings.local.json."
            )
        elif not has_session_opt:
            lines.append("No session approval option is offered; approve_session approves once.")
        return await self.register_pending_interaction(
            session=session,
            kind="approval_request",
            title="Claude Code Permission Prompt",
            prompt="\n".join(lines) if lines else scr.strip()[-600:],
            options=offered,
            source="claude_tui_screen_detector",
            metadata={
                "claude_options": opts,
                "option_actions": [o.get("action") for o in offered_opts],
                "command": best_cmd,
            },
        )

    def _claude_hook_log_path(self, session: AgentSession) -> Optional[Path]:
        snap = getattr(session, "profile_snapshot", None) or {}
        env_map = snap.get("env") if isinstance(snap.get("env"), dict) else {}
        env_log = str(env_map.get("HUB_CLAUDE_HOOK_LOG") or "").strip()
        if env_log:
            return Path(env_log)
        claude_uuid = str((session.conversation_ref or {}).get("session_id") or "").strip()
        if not claude_uuid and getattr(session, "transcript_path", None):
            try:
                claude_uuid = Path(session.transcript_path).stem
            except Exception:
                claude_uuid = ""
        if claude_uuid and re.match(r"^[A-Za-z0-9_.\-]+$", claude_uuid):
            return self.runtime_configs_dir / f"claude_hooks_{claude_uuid}.jsonl"
        return None

    async def _register_claude_hook_approval(self, session: AgentSession, hook_ev: Dict[str, Any]):
        """Registers a Claude Code permission prompt directly from the native PermissionRequest hook."""
        tname = str(hook_ev.get("tool_name") or "Tool").strip()
        tinput = hook_ev.get("tool_input") if isinstance(hook_ev.get("tool_input"), dict) else {}
        inflight = getattr(session, "_inflight_transcript_tool", None) or {}
        cmd = str(
            tinput.get("command")
            or tinput.get("file_path")
            or tinput.get("path")
            or tinput.get("url")
            or inflight.get("command")
            or ""
        ).strip()
        if not cmd and tinput:
            cmd = json.dumps(tinput, ensure_ascii=False)
        desc = str(tinput.get("description") or "").strip()
        suggestions = (
            hook_ev.get("permission_suggestions")
            if isinstance(hook_ev.get("permission_suggestions"), list)
            else []
        )
        rule_parts: List[str] = []
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            for r in (s.get("rules") or []):
                if isinstance(r, dict):
                    rc = str(r.get("ruleContent") or r.get("toolName") or "").strip()
                    if rc and rc not in rule_parts:
                        rule_parts.append(rc)
            if not rule_parts and s.get("type"):
                rule_parts.append(str(s.get("type")))
        if suggestions:
            rule_desc = ", ".join(rule_parts) if rule_parts else (f"{cmd.split()[0]}:*" if cmd else tname)
            opts = [
                {"key": "1", "label": "Yes", "action": "approve_once"},
                {
                    "key": "2",
                    "label": f"Yes, and don't ask again for: {rule_desc}",
                    "action": "approve_session",
                    "persists": True,
                },
                {"key": "3", "label": "No", "action": "deny"},
            ]
        else:
            opts = [
                {"key": "1", "label": "Yes", "action": "approve_once"},
                {"key": "2", "label": "No", "action": "deny"},
            ]
        offered_opts = [o for o in opts if o.get("action") not in ("persist", "deny_session")]
        offered = [o["label"] for o in offered_opts]
        lines: List[str] = [f"Tool: {tname}"]
        if desc:
            lines.append(f"Reason: {desc}")
        if cmd:
            lines.append(f"Command: {cmd}")
        lines.append("Do you want to proceed?")
        persists_opt = next(
            (o for o in offered_opts if o.get("action") == "approve_session" and o.get("persists")),
            None,
        )
        has_session_opt = any(o.get("action") == "approve_session" for o in offered_opts)
        if persists_opt is not None:
            lines.append(
                f"approve_session sends {persists_opt.get('key', '2')}: persist this command's "
                "allow rule in the project's .claude/settings.local.json."
            )
        elif not has_session_opt:
            lines.append("No session approval option is offered; approve_session approves once.")
        req_id = (
            str(hook_ev.get("tool_use_id") or "").strip()
            or f"claude-perm-{int(float(hook_ev.get('timestamp') or time.time()) * 1000)}"
        )
        return await self.register_pending_interaction(
            session=session,
            kind="approval_request",
            title=f"Claude Code Permission Prompt ({tname})",
            prompt="\n".join(lines),
            options=offered,
            source="claude_permission_request_hook",
            request_id=req_id,
            metadata={
                "claude_options": opts,
                "option_actions": [o.get("action") for o in offered_opts],
                "command": cmd,
                "tool_name": tname,
                "permission_suggestions": suggestions,
            },
        )

    async def _drain_claude_hook_events(self, session: AgentSession) -> Optional[Dict[str, Any]]:
        """Reads any newly appended PermissionRequest records from claude_hooks_<uuid>.jsonl."""
        log_path = self._claude_hook_log_path(session)
        if log_path is None or not log_path.is_file():
            return session.pending_interaction
        offset = int(getattr(session, "_claude_hook_offset", 0) or 0)
        try:
            lines, session._claude_hook_offset = read_complete_lines(log_path, offset)
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("hook_event_name") == "PermissionRequest":
                    if time.time() < float(getattr(session, "_prompt_quiet_until", 0.0) or 0.0):
                        session._prompt_quiet_until = 0.0
                    await self._register_claude_hook_approval(session, ev)
        except PersistenceUnavailable:
            raise
        except Exception:
            pass
        return session.pending_interaction

    @staticmethod
    def _codex_mcp_identity(parsed: Dict[str, Any]) -> tuple:
        return (parsed.get("server"), parsed.get("tool"), parsed.get("arguments"))

    async def _refresh_codex_mcp_response(self, session: AgentSession) -> None:
        inter = session.pending_interaction
        meta = (inter or {}).get("metadata") or {}
        if meta.get("codex_prompt") != "mcp":
            return
        screen = session.get_clean_screen_text(max_lines=max(38, session.rows))
        if not screen.strip():
            return
        parsed = parse_codex_approval_prompt(screen)
        if not parsed and "MCP server to run tool" in screen:
            return  # An incomplete redraw is not an acknowledgement.
        if not parsed or self._codex_mcp_identity(parsed) != tuple(meta.get("prompt_identity") or ()):
            if meta.get("response_state") in ("sent", "unconfirmed"):
                absent_since = meta.setdefault("absent_since", time.monotonic())
                if time.monotonic() - absent_since < 0.1:
                    return
                meta["response_state"] = "confirmed"
            await self.clear_pending_interaction(session, inter["request_id"])
            # The MCP modal has actually left the screen; an identical following call is new.
            session._prompt_quiet_until = 0
        else:
            meta.pop("absent_since", None)

    async def poll_tui_screen_interactions_once(self, session: AgentSession) -> Optional[Dict[str, Any]]:
        """Synchronously checks a live TUI session's hooks/transcript/screen once for approval or multi-choice prompts."""
        eng = (session.engine or self._infer_engine(session.profile_id, {})).lower()
        if eng == "codex":
            await self._refresh_codex_mcp_response(session)
        if eng == "claude":
            await self._drain_claude_hook_events(session)
        elif eng == "kimi":
            await self._drain_kimi_wire_interactions_once(session)
        if session.pending_interaction is not None:
            return session.pending_interaction
        # D7: an answered prompt can stay on screen for a moment; it is not a new one.
        if time.time() < float(getattr(session, "_prompt_quiet_until", 0.0) or 0.0):
            return None
        max_rows = max(38, int(getattr(session, "rows", 38) or 38))
        scr = session.get_clean_screen_text(max_lines=max_rows)
        if not scr.strip():
            return None

        # 1) Codex TUI approval modal: a command prompt (y / p / esc) or an edit prompt (y / a / esc).
        codex_prompt = parse_codex_approval_prompt(scr) if eng == "codex" else None
        if codex_prompt is not None:
            sig = (f"codex_mcp:{self._codex_mcp_identity(codex_prompt)}" if codex_prompt.get("prompt_kind") == "mcp"
                   else f"codex_appr:{hash(scr.strip()[-400:])}")
            if sig not in session._seen_tui_prompt_signatures:
                session._seen_tui_prompt_signatures.add(sig)
                return await self._register_codex_screen_approval(session, codex_prompt, scr)
            return None

        # 2) AGY TUI permission prompt ("Run this command?" + numbered options, mapped by label)
        agy_prompt = parse_agy_approval_prompt(scr) if eng == "agy" else None
        if agy_prompt is not None:
            sig = f"agy_appr:{hash(scr.strip()[-400:])}"
            if sig not in session._seen_tui_prompt_signatures:
                session._seen_tui_prompt_signatures.add(sig)
                return await self._register_agy_screen_approval(session, agy_prompt, scr)
            return None

        # 2b) AGY TUI `ask_question` multi-choice selector ("Question 1/" or "Navigate · enter Select").
        # Only used if transcript.jsonl has not been claimed yet; once session.transcript_path is bound,
        # _watch_agy_transcript handles ask_question from PLANNER_RESPONSE.tool_calls losslessly.
        if eng == "agy" and not session.transcript_path and (
            "Question 1/" in scr
            or "Navigate · enter Select" in scr
            or "Write-in..." in scr
        ):
            sig = f"agy_q:{hash(scr.strip()[-350:])}"
            if sig not in session._seen_tui_prompt_signatures:
                session._seen_tui_prompt_signatures.add(sig)
                q_text = "AGY Sub-Agent Question"
                opts: List[str] = []
                for sline in scr.splitlines():
                    sclean = sline.strip()
                    if sclean.startswith("Question 1/") and ":" in sclean:
                        q_text = sclean.split(":", 1)[1].strip()
                    elif sclean.startswith("? ") and len(sclean) > 4:
                        q_text = sclean[2:].strip()
                    m_opt = re.match(r"^[>\s]*(\d+)\.\s+(.+)$", sclean)
                    if m_opt:
                        opt_lbl = m_opt.group(2).strip()
                        # Clean any pyte line-overlap artifacts from long prompt lines
                        for noise_tok in ("mini/antigravity", "complete:", "hub_worker.py"):
                            if noise_tok in opt_lbl:
                                opt_lbl = opt_lbl.split(noise_tok)[0].strip()
                        opts.append(opt_lbl)
                if not opts:
                    opts = ["Option 1", "Option 2", "Write-in..."]
                return await self.register_pending_interaction(
                    session=session,
                    kind="question",
                    title="AGY TUI ask_question",
                    prompt=q_text,
                    options=opts,
                    source="agy_tui_screen_detector",
                    metadata={"raw_options_count": max(1, len(opts) - 1)},
                )
            return None

        # 3) Kimi TUI ApprovalPanel fallback ("Approve once" + "Approve for this session").
        # Only used if wire.jsonl has not been claimed yet; once session.transcript_path is bound,
        # _drain_kimi_wire_interactions_once handles Kimi approvals and questions directly from wire.jsonl.
        if eng == "kimi" and not session.transcript_path and ("Approve once" in scr and "Approve for this session" in scr):
            sig = f"kimi_appr:{hash(scr.strip()[-400:])}"
            if sig not in session._seen_tui_prompt_signatures:
                session._seen_tui_prompt_signatures.add(sig)
                return await self.register_pending_interaction(
                    session=session,
                    kind="approval_request",
                    title="Kimi TUI Approval Panel",
                    prompt=scr.strip()[-600:],
                    options=[
                        "Approve once",
                        "Approve for this session",
                        "Reject",
                        "Reject with feedback",
                    ],
                    source="kimi_tui_screen_detector",
                    metadata={"command": kimi_panel_command(scr)},
                )
            return None

        # 4) OMP tool approval box ("Allow tool: <name>" + "Approve…" rows + "Deny").
        omp_prompt = parse_omp_approval_prompt(scr) if eng == "omp" else None
        if omp_prompt is not None:
            sig = f"omp_appr:{hash(scr.strip()[-400:])}"
            if sig not in session._seen_tui_prompt_signatures:
                session._seen_tui_prompt_signatures.add(sig)
                return await self._register_omp_screen_approval(session, omp_prompt)
            return None

        # 4b) OMP settings-change confirm ("Allow once" + "Always for this session" + "Deny").
        if eng == "omp" and ("Allow once" in scr and "Always for this session" in scr and "Deny" in scr):
            sig = f"omp_settings:{hash(scr.strip()[-400:])}"
            if sig not in session._seen_tui_prompt_signatures:
                session._seen_tui_prompt_signatures.add(sig)
                return await self.register_pending_interaction(
                    session=session,
                    kind="approval_request",
                    title="OMP Settings Change Confirm",
                    prompt=scr.strip()[-600:],
                    options=["Allow once", "Always for this session", "Deny"],
                    source="omp_tui_screen_detector",
                    metadata={"raw_options_count": 3, "omp_prompt": "settings"},
                )
            return None

        # 5) Claude Code TUI permission prompt fallback ("Do you want to proceed?" + numbered options).
        # Primary path is _drain_claude_hook_events above (native PermissionRequest hook); screen
        # parsing only runs if no hook log file exists for this session.
        hook_log = self._claude_hook_log_path(session)
        claude_prompt = (
            parse_claude_approval_prompt(scr)
            if (eng == "claude" and (hook_log is None or not hook_log.is_file()))
            else None
        )
        if claude_prompt is not None:
            sig = f"claude_appr:{hash(scr.strip()[-400:])}"
            if sig not in session._seen_tui_prompt_signatures:
                session._seen_tui_prompt_signatures.add(sig)
                return await self._register_claude_screen_approval(session, claude_prompt, scr)
            return None

        # N1: when no interactive prompt is on screen anymore, clear seen signatures so a later
        # identical command prompt in the same or next turn is never suppressed.
        if session._seen_tui_prompt_signatures:
            session._seen_tui_prompt_signatures.clear()
        return None

    async def _watch_tui_interactive_prompts(self, session: AgentSession):
        """
        Secondary TUI screen fallback detector for high-risk approval modals and multi-choice questions
        across Codex, Kimi, OMP, Claude, and AGY (catches prompts that only render in the TUI without a separate JSONL event).
        """
        # Round 3 live check: "completed" (the worker reported completion, the TUI is still alive)
        # keeps the watch going. Leaving the loop there stopped all prompt detection for the rest
        # of the session, so a follow-up turn's risky command sat at agy's prompt unseen.
        while session.status in LIVE_STATUSES:
            await asyncio.sleep(0.9)
            await self.poll_tui_screen_interactions_once(session)

    @staticmethod
    def _middle_level(levels) -> Optional[str]:
        """The middle of a level list, used when neither the profile nor the model names a default."""
        if not levels:
            return None
        return levels[len(levels) // 2]

    def hub_default_cwd(self) -> str:
        """B10: the hub's own working directory, absolute."""
        return os.path.abspath(os.getcwd())

    def resolve_launch(
        self,
        profile_id: str,
        model: Optional[str] = None,
        thinking_level: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[Any] = None,
        bypass_approvals: Optional[bool] = None,
        extra_args: Optional[List[str]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        disabled_mcp_servers: Optional[List[str]] = None,
        strict_mcp_config: bool = False,
    ) -> Dict[str, Any]:
        """B6: the one place that turns (profile, model, thinking, cwd, env) into a real command line.

        Returns exactly what will run; raises a Chinese ValueError for invalid input. Spawns nothing.
        """
        pid = (profile_id or '').strip()
        prof = self.profiles.get(pid)
        enabled_ids = [k for k, v in self.profiles.items() if v.get('enabled', True)]
        if not prof or not prof.get('enabled', True):
            raise ValueError(
                f"Profile「{pid}」不存在或已停用。可用：{', '.join(enabled_ids) or '无'}"
            )

        eng = str(prof.get('engine') or 'generic').strip().lower()
        name = str(prof.get('name') or pid)
        kind = str(prof.get('kind') or 'tui')
        mcp_config = normalize_mcp_config(mcp_servers, disabled_mcp_servers, strict_mcp_config)
        validate_mcp_engine(eng, kind, mcp_config)
        command = str(prof.get('command') or '')
        base_args = strip_legacy_worker_bridge_args([str(a) for a in (prof.get('args') or [])], self.runtime_configs_dir)
        caller_args = normalize_extra_args(extra_args)
        if caller_args and (eng == 'dsh' or kind != 'tui'):
            raise ValueError(f"{name} 不通过 TUI 命令行启动，不支持 extra_args")
        base_args.extend(caller_args)
        notes = []  # Chinese notes, e.g. substitutions
        ignored_model = injected_flags_in_args(eng, caller_args)
        if ignored_model:
            notes.append(f"已忽略启动参数里的模型或思考等级参数（{'、'.join(ignored_model)}），请使用模型和思考等级字段")
        profile_env = normalize_env_map(prof.get('env'), field_label='Profile 环境变量（env）')
        caller_env = normalize_env_map(env, field_label='启动环境变量（env）')
        resolved_env: Dict[str, str] = dict(profile_env)

        # The desktop app can run the Hub under a restricted Windows token.  Several CLIs write
        # their first-run state before rendering a prompt and otherwise exit immediately when the
        # inherited user home is read-only (Kimi: ``storage write failed``; OMP: daemon state).
        # Keep normal installations on their existing home, while giving those two engines a
        # per-Hub writable home in the data directory when the inherited one cannot be written.
        if eng in ("omp", "kimi"):
            inherited_home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
            if not _directory_is_writable(inherited_home):
                runtime_home = self.data_dir / ".agent_runtime" / eng
                runtime_home.mkdir(parents=True, exist_ok=True)
                resolved_env.setdefault("USERPROFILE", str(runtime_home))
                resolved_env.setdefault("HOME", str(runtime_home))
                resolved_env.setdefault("APPDATA", str(runtime_home / "AppData" / "Roaming"))
                resolved_env.setdefault("LOCALAPPDATA", str(runtime_home / "AppData" / "Local"))
                resolved_env.setdefault("TEMP", str(runtime_home / "Temp"))
                resolved_env.setdefault("TMP", str(runtime_home / "Temp"))
                if eng == "kimi":
                    resolved_env.setdefault("KIMI_CODE_HOME", str(runtime_home))
                notes.append(f"默认用户目录不可写，已为 {name} 切换到 Hub 运行目录")

        options = self.engine_model_options(eng)
        explicit_model = str(model).strip() if model and str(model).strip() else None
        explicit_level = str(thinking_level).strip() if thinking_level and str(thinking_level).strip() else None

        resolved_model: Optional[str] = None
        model_source: Optional[str] = None
        resolved_level: Optional[str] = None
        thinking_source: Optional[str] = None
        cli_model: Optional[str] = None

        # R13: the profile's own disabled_models apply on top of the engine policy. For agy a disabled
        # suffixed id removes just that level from the base id; a disabled base removes the whole model.
        profile_disabled = {str(x) for x in (prof.get('disabled_models') or [])}
        enabled_options: List[Dict[str, Any]] = []
        for catalog_entry in options:
            if not catalog_entry.get('enabled'):
                continue
            entry_id = str(catalog_entry.get('id'))
            entry = dict(catalog_entry)
            if eng == 'agy':
                blocked_levels = set()
                blocked_entirely = False
                for blocked in profile_disabled:
                    blocked_base, blocked_level = split_agy_model_id(blocked)
                    if blocked_base != entry_id:
                        continue
                    if blocked_level:
                        blocked_levels.add(blocked_level)
                    else:
                        blocked_entirely = True
                if blocked_entirely:
                    continue
                remaining = [str(x) for x in (entry.get('thinking_levels') or []) if str(x) not in blocked_levels]
                if not remaining:
                    continue
                entry['thinking_levels'] = remaining
                if entry.get('default_thinking_level') not in remaining:
                    entry['default_thinking_level'] = None
            elif entry_id in profile_disabled:
                continue
            enabled_options.append(entry)

        option: Optional[Dict[str, Any]] = None
        if not options:
            if self.engines_catalog.get(eng) is not None and (
                prof.get('default_model') or prof.get('default_thinking_level') or explicit_model or explicit_level
            ):
                # R4: discovery found the engine but zero models (a discovery gap). Launching with the
                # profile's own defaults, unvalidated, beats silently injecting nothing.
                resolved_model = explicit_model or (str(prof.get('default_model') or '').strip() or None)
                resolved_level = explicit_level or (str(prof.get('default_thinking_level') or '').strip() or None)
                if eng == 'agy' and resolved_model:
                    base_model, suffix_level = split_agy_model_id(resolved_model)
                    resolved_model = base_model
                    if not resolved_level and suffix_level:
                        resolved_level = suffix_level
                    cli_model = f'{resolved_model}-{resolved_level}' if resolved_level else resolved_model
                model_source = 'explicit' if explicit_model else ('profile' if resolved_model else None)
                thinking_source = 'explicit' if explicit_level else ('profile' if resolved_level else None)
                notes.append(
                    '未能探测到模型列表，按启动时指定的模型与思考等级启动'
                    if (explicit_model or explicit_level)
                    else '未能探测到模型列表，按 Profile 默认值启动'
                )
            else:
                # pwsh and engines without a catalog skip model and thinking entirely.
                explicit_model = None
                explicit_level = None
        elif not enabled_options:
            # R4: the engine has models, but every one is disabled by policy or by this profile.
            raise ValueError(f'{eng} 的模型都已停用，请先在「引擎与模型」里启用一个。')
        else:
            enabled_option_ids = [str(m.get('id')) for m in enabled_options]
            by_id = {str(m.get('id')): m for m in enabled_options}
            if explicit_model:
                option = by_id.get(explicit_model)
                if option is None and eng == 'agy':
                    # agy also accepts a suffixed id: the suffix counts as explicit thinking,
                    # unless an explicit thinking_level was given too, in which case that wins.
                    agy_base, agy_suffix = split_agy_model_id(explicit_model)
                    option = by_id.get(agy_base)
                    if option is not None and agy_suffix and explicit_level is None:
                        explicit_level = agy_suffix
                if option is None:
                    raise ValueError(
                        f"模型「{explicit_model}」不可用（可能已停用或不存在）。可用："
                        f"{', '.join(enabled_option_ids) or '无'}"
                    )
                resolved_model = str(option.get('id'))
                model_source = 'explicit'
            else:
                default_model = str(prof.get('default_model') or '').strip() or None
                if default_model:
                    default_source = 'profile' if prof.get('default_model_source') != 'engine' else 'engine'
                    candidate = by_id.get(default_model)
                    if eng == 'agy' and candidate is None:
                        candidate = by_id.get(split_agy_model_id(default_model)[0])
                    if candidate is not None:
                        resolved_model = str(candidate.get('id'))
                        model_source = default_source
                        option = candidate
                if option is None and enabled_options:
                    option = enabled_options[0]
                    resolved_model = str(option.get('id'))
                    model_source = 'substituted'
                    notes.append(
                        f"默认模型 {default_model or '（未设置）'} 已停用，改用 {resolved_model}"
                    )

            if option is not None:
                levels = [str(x) for x in (option.get('thinking_levels') or [])]
                levels = self._sorted_levels_by_engine_order(eng, levels)
                if True:
                    if explicit_level:
                        if explicit_level not in levels:
                            raise ValueError(
                                f"{resolved_model} 支持的思考等级：{', '.join(levels) or '无'}"
                            )
                        resolved_level = explicit_level
                        thinking_source = 'explicit'
                    else:
                        profile_level = str(prof.get('default_thinking_level') or '').strip() or None
                        if profile_level and profile_level in levels:
                            resolved_level = profile_level
                            thinking_source = 'profile'
                        elif profile_level:
                            model_default = option.get('default_thinking_level')
                            if model_default in levels:
                                resolved_level = str(model_default)
                                thinking_source = 'model'
                            else:
                                resolved_level = self._middle_level(levels)
                                thinking_source = 'model' if resolved_level else None
                            notes.append(
                                f"Profile 默认思考等级 {profile_level} 不被 {resolved_model} 支持，改用 {resolved_level}"
                            )
                        else:
                            model_default = option.get('default_thinking_level')
                            if model_default and str(model_default) in levels:
                                resolved_level = str(model_default)
                                thinking_source = 'model'
                            elif levels:
                                resolved_level = self._middle_level(levels)
                                thinking_source = 'model'
                            else:
                                engine_levels = self._sorted_levels_by_engine_order(
                                    eng, [str(x) for x in ((self.engines_catalog.get(eng) or {}).get('available_thinking_levels') or [])]
                                )
                                resolved_level = engine_levels[0] if engine_levels else None
                                thinking_source = 'engine' if resolved_level else None

        # Rule 4: start from the stored args, strip injected flags (old files still work), inject at the front.
        args = strip_injected_flags(eng, base_args)

        if eng == 'omp':
            injected = []
            if resolved_model:
                injected.append(f'--model={resolved_model}')
            if resolved_level:
                injected.append(f'--thinking={resolved_level}')
            args = injected + args
        elif eng == 'kimi':
            if resolved_model:
                args = ['-m', resolved_model] + args
            if resolved_level:
                # Only this child process; never the hub's own environment.
                resolved_env['KIMI_MODEL_THINKING_EFFORT'] = resolved_level
        elif eng == 'agy':
            if resolved_model:
                if resolved_level:
                    cli_model = f'{resolved_model}-{resolved_level}'
                else:
                    cli_model = resolved_model
                args = ['--model', cli_model] + args
        elif eng == 'codex':
            injected = []
            if resolved_model:
                injected.extend(['-m', resolved_model])
            if resolved_level:
                injected.extend(['-c', f'model_reasoning_effort="{resolved_level}"'])
            args = injected + args
        elif eng == 'claude':
            if command:
                command = os.path.expandvars(os.path.expanduser(command))
            raw_cfg_dir = str(prof.get('config_dir') or '').strip()
            if raw_cfg_dir:
                resolved_env['CLAUDE_CONFIG_DIR'] = os.path.abspath(
                    os.path.expandvars(os.path.expanduser(raw_cfg_dir))
                )
            injected = []
            if resolved_model:
                injected.extend(['--model', resolved_model])
            if resolved_level:
                injected.extend(['--effort', resolved_level])
            args = injected + args
        # dsh injects no args at all: provider/model split and effort travel in the hub API payload.

        # Caller-supplied env vars override both profile defaults and engine-computed defaults.
        if caller_env:
            resolved_env.update(caller_env)
        if eng == 'claude' and resolved_env.get('CLAUDE_CONFIG_DIR'):
            resolved_env['CLAUDE_CONFIG_DIR'] = os.path.abspath(
                os.path.expandvars(os.path.expanduser(str(resolved_env['CLAUDE_CONFIG_DIR']).strip()))
            )

        # D1 (2026-11-07): high-risk approval is on for every engine unless the profile switches it
        # off (bypass_approvals, round 2). The profile's own bypass/mode flags (old files still carry
        # --auto-approve, --auto, ...) are stripped either way; the hub injects what the switch says.
        policy = resolve_session_approval(eng, prof, bypass_approvals)
        bypass = policy["bypass_approvals"]
        high_risk_approval = high_risk_approval_state(eng, bypass)
        ignored_approval = [
            label for label in approval_flags_in_args(eng, base_args)
            if label not in ("--settings", "--settings=")
        ]
        args = apply_approval_flags(eng, args, bypass=bypass)
        if ignored_approval:
            notes.append(
                f"已忽略启动参数里的审批相关参数（{'、'.join(ignored_approval)}）："
                + ("由 bypass_approvals 开关决定，Hub 注入全自动参数" if bypass
                   else "Hub 统一以高风险操作需审批的模式启动")
            )
        if bypass and high_risk_approval is False:
            notes.append(BYPASS_ON_NOTE)
        elif bypass:
            notes.append(BYPASS_NO_EFFECT_NOTE.format(engine=eng))
        if eng not in APPROVAL_ENGINES and (bypass_approvals is not None or profile_bypasses_approvals(prof)):
            notes.append(BYPASS_NO_EFFECT_NOTE.format(engine=eng))

        # Rule 5: cwd.
        if cwd is None or str(cwd).strip() == '':
            work_dir = self.hub_default_cwd()
        else:
            candidate_cwd = str(cwd).strip()
            expanded = os.path.expandvars(os.path.expanduser(candidate_cwd))
            resolved_cwd = os.path.abspath(expanded)
            if not os.path.isdir(resolved_cwd):
                raise ValueError(f'工作目录不存在：{candidate_cwd}')
            work_dir = resolved_cwd

        if eng == 'agy' and cli_model is None and resolved_model:
            cli_model = f'{resolved_model}-{resolved_level}' if resolved_level else resolved_model

        # V-3: dsh travels as an API payload, not as CLI args, so the preview must carry the same spec
        # the session will really use.
        dsh_spec: Optional[Dict[str, Any]] = None
        if eng == 'dsh':
            dsh_provider = 'clay'
            dsh_model_name = 'deepseek-flash'
            if resolved_model:
                if '/' in resolved_model:
                    dsh_provider, dsh_model_name = resolved_model.split('/', 1)
                else:
                    dsh_model_name = resolved_model
            dsh_spec = {
                'tier': str(prof.get('dsh_tier') or 'flash'),
                'provider': dsh_provider,
                'model': dsh_model_name,
                'effort': resolved_level or 'high',
            }

        mcp_settings = {}
        if mcp_config_active(mcp_config):
            check_mcp_arg_conflicts(eng, base_args, work_dir)
            args.extend(build_mcp_args(eng, mcp_config))
            if eng == 'claude':
                args, settings_base = merge_claude_settings(
                    args, work_dir, approval_path=claude_approval_settings_path() if not bypass else None,
                    disabled=mcp_config['disabled_mcp_servers'])
                mcp_settings['mcp_settings_base'] = settings_base
            notes.append('本次 MCP 配置已生成；连接、认证和工具发现由 Agent 启动后执行')

        return {
            'profile_id': pid,
            'profile_name': name,
            'engine': eng,
            'kind': kind,
            'command': command,
            'config_dir': resolved_env.get('CLAUDE_CONFIG_DIR') if eng == 'claude' else None,
            'args': args,
            'extra_args': caller_args,
            **mcp_config,
            **mcp_settings,
            'env': resolved_env,
            'cwd': work_dir,
            'model': resolved_model,
            'model_source': model_source,
            'thinking_level': resolved_level,
            'thinking_source': thinking_source,
            'cli_model': cli_model,
            'dsh_spec': dsh_spec,
            'bypass_approvals': bypass,
            **policy,
            'high_risk_approval': high_risk_approval,
            'notes': notes,
        }

    async def create_session(
        self,
        profile_id: str,
        task: Optional[str] = None,
        cwd: Optional[str] = None,
        title: Optional[str] = None,
        hwnd: Optional[int] = None,
        model: Optional[str] = None,
        thinking_level: Optional[str] = None,
        env: Optional[Any] = None,
        launch_source: str = "user",
        bypass_approvals: Optional[bool] = None,
        extra_args: Optional[List[str]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        disabled_mcp_servers: Optional[List[str]] = None,
        strict_mcp_config: bool = False,
        auto_approval_rules: Any = None,
    ) -> AgentSession:
        if launch_source not in ("user", "agent"):
            raise ValueError("launch_source must be user or agent")
        if bypass_approvals is not None and type(bypass_approvals) is not bool:
            raise ValueError("bypass_approvals 只能是 true、false 或 null")
        extra_args = normalize_extra_args(extra_args)
        mcp_config = normalize_mcp_config(mcp_servers, disabled_mcp_servers, strict_mcp_config)
        auto_approval_rules = normalize_auto_approval_rules(auto_approval_rules)
        if profile_id == "desktop-gui":
            validate_mcp_engine("desktop_gui", "desktop_gui", mcp_config)
        if extra_args and profile_id == "desktop-gui":
            raise ValueError("桌面 GUI 会话不支持 extra_args")
        loop = asyncio.get_running_loop()

        if profile_id == "desktop-gui" and hwnd:
            session_id = f"gui-{uuid.uuid4().hex[:8]}"
            sess = AgentSession(
                session_id=session_id,
                profile_id="desktop-gui",
                profile_name="Desktop GUI Window",
                kind="desktop_gui",
                title=title or f"HWND-{hwnd}",
                cwd=cwd or os.getcwd(),
                command="",
                args=[],
                hwnd=int(hwnd),
                on_exit_cb=self._exit_callback,
                on_tui_tool_cb=self._tool_callback,
            )
            sess.status = "running"
            sess.engine = "desktop_gui"
            # Spec §3: even a non-resumable session keeps the launch it ran with, so the archive can
            # say why it cannot be revived instead of "no launch recorded".
            sess.profile_snapshot = {
                "launch_source": launch_source,
                "profile_id": "desktop-gui", "engine": "desktop_gui", "kind": "desktop_gui",
                "command": "", "args": [], "env": {}, "model": None, "thinking_level": None,
                "cli_model": None, "cwd": sess.cwd, "task_mode": "desktop_gui",
                "startup_delay_sec": 0, "submit_key": "\r", "dsh_spec": None,
                "auto_approval_rules": auto_approval_rules,
                **resolve_session_approval("desktop_gui", {}, bypass_approvals),
            }
            if bypass_approvals is not None:
                sess.launch_notes = [BYPASS_NO_EFFECT_NOTE.format(engine="desktop_gui")]
            self.sessions[session_id] = sess
            await self.persist_session(sess)
            return sess

        launch = self.resolve_launch(profile_id, model=model, thinking_level=thinking_level, cwd=cwd, env=env,
                                     bypass_approvals=bypass_approvals, extra_args=extra_args, **mcp_config)

        prof = self.profiles[profile_id]
        engine = launch['engine']
        # Q7: one reading of `kind` for the whole launch path; a stored null means tui, never None.
        kind = str(prof.get("kind") or "tui")
        work_dir = launch['cwd']
        effective_model = launch['model']
        effective_thinking = launch['thinking_level']
        args = list(launch['args'])
        env_overrides: Dict[str, str] = dict(launch.get('env') or {})
        if engine == 'agy' and launch.get('cli_model'):
            effective_model = launch['cli_model']

        # Q12: 8 hex characters, wide enough that a reused profile prefix never collides.
        session_id = f'{profile_id}-{uuid.uuid4().hex[:8]}'
        # Archive spec §3: the resolved launch, as it will really run. The task itself is added below
        # by the task mode and is deliberately NOT part of the snapshot (a resume adds its own message).
        launch_args = list(args)
        task_mode = str(prof.get("task_mode") or "pty_input")
        # R1: everything that can raise runs before the PTY starts and before the session is stored,
        # so a bad profile value leaves no orphan session behind.
        try:
            startup_delay = float(prof.get("startup_delay_sec") or 2.5)
        except (TypeError, ValueError):
            raise ValueError(
                f"Profile「{profile_id}」的启动等待（startup_delay_sec）不是数字：{prof.get('startup_delay_sec')!r}"
            )
        if startup_delay < 0:
            startup_delay = 0.0
        submit_key = str(prof.get("submit_key") or "\r")
        omp_sess_dir: Optional[Path] = None
        claude_session_uuid: Optional[str] = None

        if task_mode == "omp_positional":
            omp_sess_dir = self.runtime_configs_dir / f"omp_sess_{session_id}"
            omp_sess_dir.mkdir(exist_ok=True)
            args.extend([
                f"--session-dir={omp_sess_dir}",
            ])
            if task:
                args.append(self._task_with_session_marker(session_id, task))
        elif task_mode == "claude_positional":
            claude_session_uuid = str(uuid.uuid4())
            args.extend([
                "--session-id",
                claude_session_uuid,
            ])
            if task:
                args.append(self._task_with_session_marker(session_id, task))
        elif task_mode == "agy_interactive_flag":
            if task:
                args.extend(["-i", self._task_with_session_marker(session_id, task)])
        elif task_mode == "codex_positional":
            if task:
                args.append(self._task_with_session_marker(session_id, task))

        prof_name = str(prof.get("name", profile_id))
        sess = AgentSession(
            session_id=session_id,
            profile_id=profile_id,
            profile_name=prof_name,
            kind=kind,
            title=derive_readable_session_title(title, task, prof_name, session_id),
            cwd=work_dir,
            command=launch["command"],
            args=args,
            gui_url=prof.get("gui_url"),
            cols=self.default_cols,
            rows=self.default_rows,
            on_exit_cb=self._exit_callback,
            on_tui_tool_cb=self._tool_callback,
        )
        sess.effective_model = effective_model
        sess.effective_thinking_level = effective_thinking
        sess.engine = engine
        if claude_session_uuid:
            sess.conversation_ref = {"session_id": claude_session_uuid}
        sess.profile_snapshot = {
            "launch_source": launch_source,
            "profile_id": profile_id,
            "engine": engine,
            "kind": kind,
            "command": launch["command"],
            "config_dir": launch.get("config_dir"),
            "args": launch_args,
            "extra_args": list(launch.get("extra_args") or []),
            **{key: launch[key] for key in ("mcp_servers", "disabled_mcp_servers", "strict_mcp_config")},
            **({"mcp_settings_base": launch["mcp_settings_base"]} if "mcp_settings_base" in launch else {}),
            "env": dict(launch.get("env") or {}),
            "model": launch.get("model"),
            "thinking_level": launch.get("thinking_level"),
            "cli_model": launch.get("cli_model"),
            "cwd": work_dir,
            "task_mode": task_mode,
            "startup_delay_sec": startup_delay,
            "submit_key": submit_key,
            "dsh_spec": dict(launch.get("dsh_spec") or {}) or None,
            "auto_approval_rules": auto_approval_rules,
            # Round 2: whether this launch had the profile's approval off switch on (a resume
            # re-reads the profile's current value and overwrites this).
            "bypass_approvals": bool(launch.get("bypass_approvals")),
            **resolve_session_approval_fields(launch),
        }
        sess.launch_notes = list(launch.get("notes") or [])

        if kind == "web_gui":
            if task:
                sess.args = ["-NoLogo", "-NoProfile", "-Command", f"dsh headless {json.dumps(task)}; pwsh -NoLogo"]
            else:
                sess.args = ["-NoLogo", "-NoProfile"]
            # "args (as launched)": web_gui really runs the dsh headless command, not the profile args.
            sess.profile_snapshot["args"] = list(sess.args)

        if task:
            # B9: last_task is the initial task and is never overwritten afterwards.
            sess.last_task = task
            sess.current_activity = f"Running initial task: {task[:80]}"
            if task_mode in ("claude_positional", "omp_positional", "agy_interactive_flag", "codex_positional"):
                sess.last_injection = {
                    "at": round(time.time(), 3),
                    "ok": True,
                    "reason": "argv_positional",
                    "attempts": 1,
                    "mode": "startup_task",
                    "label": "成功",
                }

        if task_mode == "dsh_hub_api":
            # B6 rule 4 / V-3: dsh injects no args. resolve_launch already built the dsh spec
            # (tier, provider/model split, effort), so the session and the preview use one value.
            sess.dsh_spec = dict(launch.get("dsh_spec") or {})
            sess.start_virtual(
                loop,
                on_virtual_line_cb=lambda sid, line: self._schedule(self.deliver_parent_message(sid, line, also_write_pty=False)),
            )
            self.sessions[session_id] = sess
            await self.persist_session(sess)
            self._schedule(self._spawn_dsh_hub_job(sess, task))
            return sess

        if engine == "claude":
            hook_log = self._claude_hook_log_path(sess)
            if hook_log is not None:
                env_overrides.setdefault("HUB_CLAUDE_HOOK_LOG", str(hook_log))
                try:
                    sess._claude_hook_offset = hook_log.stat().st_size if hook_log.is_file() else 0
                except OSError:
                    sess._claude_hook_offset = 0
        sess.start_pty(loop, env_overrides=env_overrides or None)
        self.sessions[session_id] = sess
        await self.persist_session(sess)

        if engine == "codex":
            self._schedule(self._watch_codex_rollout(sess))
        elif omp_sess_dir is not None:
            self._schedule(self._watch_omp_session(sess, omp_sess_dir))
        elif engine == "kimi":
            self._schedule(self._watch_kimi_session(sess))
        elif engine == "agy":
            self._schedule(self._watch_agy_transcript(sess))
        elif engine == "claude":
            self._schedule(self._watch_claude_session(sess))

        if kind == "tui":
            self._schedule(self._watch_tui_interactive_prompts(sess))
            prompt_to_send: Optional[str] = None
            if task and task_mode == "pty_input":
                prompt_to_send = self._task_with_session_marker(sess.session_id, task)
            self._schedule(
                self._auto_handle_tui_startup_and_trust(
                    sess,
                    task_to_inject=prompt_to_send,
                    startup_delay=startup_delay,
                    submit_key=submit_key,
                )
            )

        return sess

    async def wait_for_output_settle(
        self,
        session: AgentSession,
        timeout_sec: float = 25.0,
        quiet_window_sec: float = 2.5,
        progress_cb: Optional[Callable[[float, str], Any]] = None,
    ) -> Dict[str, Any]:
        start = time.time()
        initial_output_at = session.last_output_at
        has_seen_new_output = False

        while (time.time() - start) < timeout_sec:
            await asyncio.sleep(0.5)
            now = time.time()
            elapsed = now - start

            if session.last_output_at > initial_output_at:
                has_seen_new_output = True

            if progress_cb and int(elapsed * 2) % 6 == 0:
                try:
                    res = progress_cb(elapsed, session.get_clean_screen_text(max_lines=15))
                    if asyncio.iscoroutine(res):
                        await res
                except PersistenceUnavailable:
                    raise
                except Exception:
                    pass

            # K6 (fix 9c): a live message still being typed and proved in the background is not a
            # settled screen: the K5 settle and the proof can leave the output quiet for a while
            # before the Enter has even been pressed.
            submitting = self._tui_writer_queue(session).busy("parent_message")
            if not submitting and session.status in ("completed", "waiting_parent", "exited", "terminated"):
                await self.persist_session(session)
                return {"settled": True, "reason": f"status_{session.status}", "elapsed_sec": round(elapsed, 1)}

            if (has_seen_new_output and not submitting
                    and (now - session.last_output_at) >= quiet_window_sec and elapsed >= 3.0):
                try:
                    await self.summarize_transcript(session.session_id)
                except PersistenceUnavailable:
                    raise
                except Exception:
                    pass
                await self.persist_session(session)
                return {"settled": True, "reason": "output_settled", "elapsed_sec": round(elapsed, 1)}

        await self.persist_session(session)
        return {
            "settled": False,
            "reason": "timeout_still_running_in_console",
            "elapsed_sec": round(time.time() - start, 1),
        }
