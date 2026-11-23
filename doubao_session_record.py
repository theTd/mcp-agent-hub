"""Doubao CLI (`doubao`) session records: binary, workspace sessions, trajectory rows and export.

The CLI keeps everything under ``%LOCALAPPDATA%\\doubao_cli``:

- ``app/current/doubao.exe`` — the native binary (the npm wrapper adds update-check stalls).
- ``agent_mode/<user_id>/workspace/.sessions/<conversation_id>/agents/<agent_id>/system/assignment.md``
  — the turn's prompt, marker included, written when the turn starts.
- ``.../system/trajectory.jsonl`` — OpenAI-format rows (assistant text/tool_calls, tool results).
- ``doubao-cli-pc-login/`` — credentials. Nothing here ever reads that folder.

``--export <conversation_id>`` is the cloud-side view (``[{"query", "response"}]``); it covers a
pure-text turn that the local trajectory file does not hold.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from engine_errors import classify_engine_error

DOUBAO_APP_DIR_NAME = "doubao_cli"
DOUBAO_NATIVE_RELATIVE = Path("app") / "current" / "doubao.exe"
NO_UPDATE_CHECK_ENV = "DOUBAO_CLI_NO_UPDATE_CHECK"
# `assignment.md` is written when a turn starts; its mtime is the only cheap ordering signal.
ASSIGNMENT_FRESH_SLACK_SEC = 5.0
MAX_ASSIGNMENT_BYTES = 1024 * 1024

HUB_SESSION_MARKER_RE = re.compile(r"\[Hub Session:\s*session_id=([^\]\s]+)\s*\]")
EXIT_RESUME_RE = re.compile(r"--resume\s+['\"]?([A-Za-z0-9][A-Za-z0-9_.:-]*)")
STATUS_SESSION_RE = re.compile(r"(?m)^\s*Session\s*:\s*['\"]?([A-Za-z0-9][A-Za-z0-9_.:-]*)\s*$")

# TUI glyphs the CLI draws in front of a message.
ASSISTANT_GLYPH = "◆"
USER_GLYPH = "▍"
TOOL_GLYPH = "◇"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RULE_ROW_RE = re.compile(r"^\s*[─━═]{3,}\s*$")
# `model · effort · cwd` under the composer; long answers that happen to hold " · " are kept.
_STATUS_ROW_RE = re.compile(r"^\S[^\n]{0,150}\s·\s\S[^\n]{0,80}\s·\s")
_CHROME_SUBSTRINGS = (
    "DOUBAO CLI",
    "Resume this session with",
    "sending is paused",
    "Responding…",
    "Responding...",
    "Processing",
    "Worked for",
    "Loading…",
    "Loading...",
    "esc to interrupt",
    "Ask Doubao to do anything",
)
# Code -> the classifier hint that names the same failure (the classifier is HTTP-worded).
_ERROR_CODES = {
    "invalid_arguments": "400",
    "auth_failed": "401",
    "session_expired": "401",
    "network_offline": "",
    "server_error": "500",
    "permission_denied": "403",
    "internal_error": "500",
    "too many requests": "429",
}
# The CLI's own failure formats, and nothing else: a line that starts with an error code and a
# colon, a known CLI banner, or a JSON error envelope. Ordinary prose that merely mentions one of
# these words (an answer explaining `internal_error`, a task titled "auth_failed") never matches.
_ERROR_CODE_LINE_RE = re.compile(
    r"^\s*(?:Error:\s*)?(invalid_arguments|auth_failed|session_expired|network_offline|"
    r"server_error|permission_denied|internal_error)\s*:\s*\S"
)
_ERROR_BANNERS = (
    (re.compile(r"^\s*Your session has expired\b", re.IGNORECASE), "session_expired"),
    (re.compile(r"^\s*Too many requests\b", re.IGNORECASE), "too many requests"),
    (re.compile(r"^\s*交互模式需要 TTY 终端"), "invalid_arguments"),
    (re.compile(r"^\s*SESSION_EXPIRED\b"), "session_expired"),
)
_JSON_ERROR_RE = re.compile(r'"is_error"\s*:\s*true', re.IGNORECASE)
_JSON_ERRORS_KEY_RE = re.compile(r'"errors"\s*:', re.IGNORECASE)

# The idle composer of the Doubao TUI (drawn in place of typed text).
DOUBAO_COMPOSER_PLACEHOLDER = "Ask Doubao to do anything"
# The card's busy footer while the CLI is closing it (`Submitting, waiting for confirmation…`).
DOUBAO_SUBMITTING_TEXT = "Submitting, waiting for confirmation"
# Negations that must beat every affirmative token in a rendered label (`不允许` holds `允许`).
_DOUBAO_NEGATION_TOKENS = (
    "不允许",
    "禁止",
    "不同意",
    "不批准",
    "不继续",
    "拒绝",
    "取消",
    "跳过",
)
# Ink wraps the card footer at narrow widths: at most this many trailing rows are one footer.
_DOUBAO_FOOTER_MAX_LINES = 3
_CURSOR_GLYPHS = ("❯", "›")
# One rendered card row: an optional Ink cursor, then nothing else this parser owns.
_CURSOR_PREFIX_RE = re.compile(r"^(?P<cursor>[❯›>])[ ]?(?P<rest>.*)$")
# The server sends the confirmation labels; the TUI never numbers them.
_DOUBAO_CONFIRM_LABELS = (
    "allow once",
    "allow similar commands",
    "allow similar actions",
    "allow folder access once",
    "allow listed folders once",
    "always allow",
    "allow always",
    "allow all",
    "reject and stop",
    "deny and stop",
    "reject",
    "deny",
    "skip",
    "cancel",
    "允许一次",
    "仅允许一次",
    "允许类似",
    "允许类似操作",
    "本会话允许",
    "允许所有",
    "全部允许",
    "始终允许",
    "总是允许",
    "拒绝并停止",
    "拒绝并终止",
    "拒绝",
    "取消",
    "跳过",
)
_DOUBAO_ACTION_LABELS = {
    "submit": "submit",
    "answer": "submit",
    "skip": "skip",
    "cancel": "deny",
    "reject": "deny",
    "reject and stop": "deny_feedback",
    "deny": "deny",
    "deny and stop": "deny_feedback",
    "提交": "submit",
    "回答": "submit",
    "跳过": "skip",
    "取消": "deny",
    "拒绝": "deny",
    "拒绝并停止": "deny_feedback",
    "拒绝并终止": "deny_feedback",
}
_DOUBAO_TOOL_NAMES = frozenset({
    "bash", "terminal", "powershell", "shell", "cmd", "read", "write", "edit",
    "glob", "grep", "webfetch", "websearch", "web_fetch", "web_search",
    "delete", "move", "copy", "task", "agent", "patch",
})
# Trailing `[Enter]` / `[Esc]` / `[1]` shortcut badge the server may attach to an action.
_SHORTCUT_BADGE_RE = re.compile(r"\s*\[[^\]\n]{1,16}\]\s*$")
# The dim placeholder the renderer appends to a text option: `Other:  [type your answer]`.
_TEXT_PLACEHOLDER_RE = re.compile(r"\s*\[[^\]\n]{0,80}\]\s*$")
_RADIO_MARK_RE = re.compile(r"^\(([ ●✓]?)\)\s?(.*)$")
_CHECKBOX_MARK_RE = re.compile(r"^\[([ ✓xX]?)\]\s?(.*)$")
# Rows the CLI draws above a card while a tool runs; the payload after the verb is the command.
_DOUBAO_COMMAND_VERB_RE = re.compile(
    r"^(?P<verb>正在执行代码|正在执行命令|执行终端命令|执行代码|执行命令|运行命令|"
    r"Running command|Running code|Executing command|Executing code)\s+(?P<cmd>.+)$",
    re.IGNORECASE,
)
_RUNNING_SUFFIX_RE = re.compile(r"\s*[·•]\s*Running\s*$")


def doubao_data_root() -> Path:
    """`%LOCALAPPDATA%\\doubao_cli`, the CLI's whole local state."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path(os.path.expanduser("~")) / "AppData" / "Local"
    return root / DOUBAO_APP_DIR_NAME


def resolve_doubao_binary(configured: Optional[str] = None) -> str:
    """The native `doubao.exe`, else the configured command, else `doubao` on PATH."""
    native = doubao_data_root() / DOUBAO_NATIVE_RELATIVE
    if native.is_file():
        return str(native)
    text = str(configured or "").strip()
    if text:
        expanded = os.path.expandvars(os.path.expanduser(text))
        found = shutil.which(expanded)
        return found or expanded
    return shutil.which("doubao") or "doubao"


def _read_head(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(MAX_ASSIGNMENT_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""


def _trajectories_in(conversation_dir: Path) -> List[Path]:
    try:
        paths = sorted(conversation_dir.glob("agents/*/system/trajectory.jsonl"))
    except OSError:
        return []
    return [path for path in paths if path.is_file()]


def find_doubao_session_by_marker(
    hub_session_id: str,
    *,
    created_at: Optional[float],
    data_root: Optional[Path] = None,
) -> Optional[Tuple[str, List[Path]]]:
    """`(conversation_id, trajectory_paths)` for the conversation whose prompt carries this marker.

    Only an exact `[Hub Session: session_id=<id>]` match counts, so two hub sessions that start
    together can never claim each other's conversation. `assignment.md` is written when a turn
    starts; a copy older than the spawn (minus a small filesystem slack) is not this session's.
    """
    marker = str(hub_session_id or "").strip()
    if not marker:
        return None
    root = Path(data_root) if data_root is not None else doubao_data_root()
    agents_root = root / "agent_mode"
    if not agents_root.is_dir():
        return None
    floor = float(created_at or 0.0) - ASSIGNMENT_FRESH_SLACK_SEC
    try:
        assignments = list(agents_root.glob("*/workspace/.sessions/*/agents/*/system/assignment.md"))
    except OSError:
        return None

    best_mtime = float("-inf")
    best_conversation: Optional[Path] = None
    for path in assignments:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if floor > 0.0 and mtime < floor:
            continue
        text = _read_head(path)
        if not text or marker not in text:
            continue
        if not any(match.group(1).strip() == marker for match in HUB_SESSION_MARKER_RE.finditer(text)):
            continue
        conversation = path.parents[3]
        if conversation.parent.name != ".sessions":
            continue
        if mtime > best_mtime:
            best_mtime, best_conversation = mtime, conversation
    if best_conversation is None:
        return None
    return best_conversation.name, _trajectories_in(best_conversation)


def parse_doubao_exit_resume_id(text: str) -> Optional[str]:
    """The id in the exit banner `Resume this session with: doubao --resume '<id>'`."""
    match = EXIT_RESUME_RE.search(str(text or ""))
    return match.group(1) if match else None


def parse_doubao_status_session_id(text: str) -> Optional[str]:
    """The id in `/status`'s `Session: <id>` row."""
    match = STATUS_SESSION_RE.search(str(text or ""))
    return match.group(1) if match else None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("text"):
                parts.append(str(part["text"]))
        return "\n".join(text.strip() for text in parts if text.strip()).strip()
    if content is None:
        return ""
    return str(content).strip()


def _tool_arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ""
        try:
            return json.loads(text)
        except Exception:
            return raw
    return raw


def parse_doubao_trajectory_record(record: Any) -> List[Dict[str, Any]]:
    """One OpenAI-format trajectory row as normalized events.

    Events: `{"kind": "assistant_text" | "reasoning" | "tool_call" | "tool_result" | "user_text"}`.
    A row that names no role (or an unknown one) yields nothing.
    """
    if not isinstance(record, dict):
        return []
    role = str(record.get("role") or "").strip().lower()
    events: List[Dict[str, Any]] = []
    if role == "assistant":
        reasoning = record.get("reasoning_content") or record.get("reasoning")
        if reasoning:
            events.append({"kind": "reasoning", "text": str(reasoning).strip()})
        text = _content_text(record.get("content"))
        if text:
            events.append({"kind": "assistant_text", "text": text})
        calls = record.get("tool_calls")
        for call in calls if isinstance(calls, list) else []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else call
            events.append(
                {
                    "kind": "tool_call",
                    "call_id": str(call.get("id") or call.get("tool_call_id") or ""),
                    "name": str(function.get("name") or call.get("name") or "tool"),
                    "arguments": _tool_arguments(
                        function.get("arguments") if "arguments" in function else call.get("arguments")
                    ),
                }
            )
    elif role == "tool":
        events.append(
            {
                "kind": "tool_result",
                "call_id": str(record.get("tool_call_id") or record.get("id") or ""),
                "name": str(record.get("name") or ""),
                "content": _content_text(record.get("content")),
            }
        )
    elif role == "user":
        text = _content_text(record.get("content"))
        if text:
            events.append({"kind": "user_text", "text": text})
    return events


def _json_array_payload(text: str) -> Optional[List[Dict[str, Any]]]:
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    rows: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append({"query": str(item.get("query") or ""), "response": str(item.get("response") or "")})
    return rows


def export_doubao_session(
    binary: str,
    conversation_id: str,
    *,
    timeout: float = 15.0,
) -> Optional[List[Dict[str, str]]]:
    """Run `doubao --export <id>`; the `[{"query", "response"}]` rows, or None on any failure."""
    cid = str(conversation_id or "").strip()
    command = str(binary or "").strip()
    if not cid or not command:
        return None
    env = dict(os.environ)
    env.setdefault(NO_UPDATE_CHECK_ENV, "1")
    try:
        result = subprocess.run(
            [command, "--export", cid],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _json_array_payload(str(result.stdout or ""))


def _screen_lines(lines: Any) -> List[str]:
    if isinstance(lines, str):
        raw = lines.splitlines()
    elif isinstance(lines, Iterable):
        raw = [str(line) for line in lines]
    else:
        raw = []
    return [_ANSI_RE.sub("", line).rstrip() for line in raw]


def _is_chrome(line: str) -> bool:
    text = line.strip()
    if not text:
        return False
    if text.startswith((USER_GLYPH, TOOL_GLYPH, "❯", ">", "?")):
        return True
    if _RULE_ROW_RE.match(text) or _STATUS_ROW_RE.match(text):
        return True
    return any(needle in text for needle in _CHROME_SUBSTRINGS)


def extract_doubao_screen_reply(lines: Any) -> str:
    """The latest assistant (`◆`) block from sanitized TUI rows, chrome and status rows dropped."""
    rows = _screen_lines(lines)
    start = None
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].strip().startswith(ASSISTANT_GLYPH):
            start = index
            break
    if start is None:
        return ""
    parts: List[str] = []
    first = rows[start].strip()[len(ASSISTANT_GLYPH):].strip()
    if first:
        parts.append(first)
    for row in rows[start + 1:]:
        text = row.strip()
        if not text:
            if parts:
                break
            continue
        if text.startswith(ASSISTANT_GLYPH) or _is_chrome(row):
            break
        parts.append(text)
    return "\n".join(parts).strip()


def _non_assistant_rows(text: str) -> List[str]:
    """Rows of a screen outside every `◆` assistant block and without `▍` user echoes.

    An assistant answer may quote `internal_error`, `auth_failed` or a whole approval card in its
    prose; only the CLI's own status/error/exit rows can identify an engine failure.
    """
    rows: List[str] = []
    in_block = False
    for raw in str(text or "").splitlines():
        row = raw.strip()
        if not row:
            in_block = False
            continue
        if row.startswith(ASSISTANT_GLYPH):
            in_block = True
            continue
        if in_block:
            if row.startswith((USER_GLYPH, TOOL_GLYPH, "❯")):
                in_block = False
            else:
                continue
        if row.startswith(USER_GLYPH):
            continue
        rows.append(row)
    return rows


def detect_doubao_error(text: Any) -> Optional[Dict[str, Any]]:
    """`classify_engine_error` for a Doubao CLI error code/message on screen; None when absent.

    Only the CLI's own failure formats count (a line-start `<code>:` row, a CLI banner, or a JSON
    error envelope), and only outside `◆` assistant prose. A reply that merely mentions a code
    name is an answer, never an engine error.
    """
    rows = _non_assistant_rows(str(text or ""))
    if not rows:
        return None
    for row in rows:
        match = _ERROR_CODE_LINE_RE.match(row)
        if match:
            return _doubao_error_info(row, match.group(1))
    for row in rows:
        for pattern, code in _ERROR_BANNERS:
            if pattern.search(row):
                return _doubao_error_info(row, code)
    payload = "\n".join(rows)
    if _JSON_ERROR_RE.search(payload) and _JSON_ERRORS_KEY_RE.search(payload):
        row = next(row for row in rows if _JSON_ERROR_RE.search(row))
        return _doubao_error_info(row, "server_error")
    return None


def _doubao_error_info(line: str, code: str) -> Dict[str, Any]:
    info = classify_engine_error(line or code, code=_ERROR_CODES.get(code) or None)
    info["code"] = code
    return info


def _doubao_clean_label(label: str) -> str:
    """A rendered row label without its cursor, radio/checkbox marker or shortcut badge."""
    text = str(label or "").strip()
    match = _CURSOR_PREFIX_RE.match(text)
    if match:
        text = match.group("rest").strip()
    for pattern in (_RADIO_MARK_RE, _CHECKBOX_MARK_RE):
        match = pattern.match(text)
        if match:
            text = match.group(2).strip()
            break
    return _SHORTCUT_BADGE_RE.sub("", text).strip()


def _doubao_question_label(raw: str) -> str:
    """Display label of a question row: placeholder and the renderer's ` · description` gone."""
    text = _doubao_clean_label(raw)
    text = _TEXT_PLACEHOLDER_RE.sub("", text).strip()
    if " · " in text:
        text = text.split(" · ", 1)[0].strip()
    if text.lower().rstrip(":：") == "other":
        return "Other"
    return text


def _doubao_question_detail(raw: str) -> str:
    """Signature material of a question row: cursor, checked mark, typed text and placeholder gone.

    The renderer draws `(●) label · description`, `Other: <typed>`/`Answer: <typed>`, so both the
    checked state and whatever the user typed must not reach the card identity; the description
    and the label itself must.
    """
    text = _SHORTCUT_BADGE_RE.sub("", str(raw or "").strip()).strip()
    low = text.lower()
    if low.startswith("answer:"):
        return "Answer"
    if low.startswith("other:"):
        return "Other"
    return _TEXT_PLACEHOLDER_RE.sub("", text).strip()


def _doubao_action_decision(raw: str) -> Optional[str]:
    """The decision (`submit` / `skip` / `deny` / `deny_feedback`) of a trailing action row."""
    low = _SHORTCUT_BADGE_RE.sub("", str(raw or "").strip()).strip().rstrip(":：").lower()
    return _DOUBAO_ACTION_LABELS.get(low)


def _doubao_question_row(raw: str) -> Tuple[Optional[str], bool, bool, str, str]:
    """(kind, cursor, checked, text, marker) for one rendered card row; kind may be None."""
    body = str(raw or "").lstrip()
    cursor = body[:1] in _CURSOR_GLYPHS
    if cursor:
        body = body[1:].lstrip(" ")
    radio = _RADIO_MARK_RE.match(body)
    check = _CHECKBOX_MARK_RE.match(body)
    marker = radio or check
    if marker:
        text = marker.group(2).strip()
        checked = marker.group(1).strip().lower() in ("●", "✓", "x")
        kind = "option"
        low = text.lower()
        if low.startswith(("other:", "answer:")):
            kind = "text"
        return kind, cursor, checked, text, "checkbox" if check else "radio"
    text = body.strip()
    if text.lower().startswith(("other:", "answer:")):
        return "text", cursor, False, text, ""
    if _doubao_action_decision(text):
        return "action", cursor, False, text, ""
    return None, cursor, False, text, ""


def _doubao_confirm_row(raw: str) -> Optional[Tuple[bool, str]]:
    """(cursor, label) for one confirmation-card row, else None.

    A real confirmation card draws only the server's own action labels, unnumbered, optionally
    with a trailing `[Enter]`/`[Esc]` shortcut badge. Numbered prose never matches.
    """
    body = str(raw or "").lstrip()
    cursor = body[:1] in _CURSOR_GLYPHS
    if cursor:
        body = body[1:].lstrip(" ")
    text = _SHORTCUT_BADGE_RE.sub("", body.strip()).strip()
    low = text.lower()
    if not text or not any(low.startswith(label) for label in _DOUBAO_CONFIRM_LABELS):
        return None
    return cursor, text


def classify_doubao_option(label: str) -> str:
    """approve_once | approve_session | persist | deny | deny_feedback | submit for one label.

    A negation wins over every affirmative token: `不允许` / `禁止删除` carry `允许` / `删除` as
    substrings, and loose substring matching would read them as approvals. English affirmatives are
    the label's leading word, Chinese ones its opening characters.

    `persist` (`Always allow` / `Allow all`) is the CLI's global, account-wide allow setting: it is
    reported so the responder can refuse it, never folded into a session approval. An unknown label
    is a deny, never an approval.
    """
    low = _doubao_clean_label(label).lower()
    if low.startswith(("reject and stop", "deny and stop")) or any(
        token in low for token in ("拒绝并停止", "拒绝并终止")
    ):
        return "deny_feedback"
    if any(token in low for token in _DOUBAO_NEGATION_TOKENS) or low.startswith(
        ("reject", "deny", "cancel", "skip", "no")
    ):
        return "deny"
    if low.startswith(("always allow", "allow always", "allow all")) or any(
        token in low for token in ("始终允许", "总是允许", "允许所有", "全部允许")
    ):
        return "persist"
    if low.startswith("allow similar") or any(token in low for token in ("允许类似", "本会话允许")):
        return "approve_session"
    if low.startswith(("allow once", "allow folder access once", "allow listed folders once")) or any(
        token in low for token in ("允许一次", "仅允许一次")
    ):
        return "approve_once"
    if low.startswith(("submit", "提交")):
        return "submit"
    if low.startswith(
        ("yes", "allow", "approve", "proceed", "delete", "continue",
         "允许", "批准", "同意", "继续", "删除")
    ):
        return "approve_once"
    return "deny"


def _doubao_signature(kind: str, *parts: Any) -> str:
    """Card identity: the cursor (and checked state) are deliberately not part of it."""
    payload = json.dumps([kind, *parts], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _doubao_collapsed(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _doubao_running_command(rows: List[str]) -> str:
    """The command the CLI drew above the card (`◇ 正在执行代码 … · Running`), wrapped rows kept."""
    for pos in range(len(rows) - 1, -1, -1):
        text = rows[pos].strip()
        if not text.startswith(TOOL_GLYPH):
            continue
        match = _DOUBAO_COMMAND_VERB_RE.match(text[len(TOOL_GLYPH):].strip())
        if not match:
            continue
        parts = [_RUNNING_SUFFIX_RE.sub("", match.group("cmd").strip())]
        for continuation in rows[pos + 1:]:
            stripped = continuation.strip()
            if (
                not stripped
                or stripped.startswith((TOOL_GLYPH, ASSISTANT_GLYPH, USER_GLYPH))
                or _RULE_ROW_RE.match(stripped)
            ):
                break
            parts.append(_RUNNING_SUFFIX_RE.sub("", stripped))
        return "\n".join(part for part in parts if part).strip()
    return ""


def _doubao_last_question(rows: List[str]) -> str:
    """The `◇ …？` question above a card, including a wrap that Ink re-prefixed with `◇`."""
    blocks = [
        row.strip()[len(TOOL_GLYPH):].strip()
        for row in rows
        if row.strip().startswith(TOOL_GLYPH)
    ]
    for end in range(len(blocks) - 1, -1, -1):
        payload = blocks[end]
        if not payload.endswith(("?", "？")):
            continue
        parts = [payload]
        for start in range(end - 1, max(-1, end - 4), -1):
            previous = blocks[start]
            if (
                not previous
                or previous.endswith(("?", "？", "。", "！", "!", ".", "· Running"))
                or _DOUBAO_COMMAND_VERB_RE.match(previous)
            ):
                break
            parts.insert(0, previous)
        return "\n".join(parts)
    return ""


def _doubao_permissions_menu(rows: List[str]) -> bool:
    """The `/permissions` setting menu is a numbered list, never a card."""
    texts = [row.strip() for row in rows]
    if any("Permissions ·" in text or text.startswith("Permissions") for text in texts):
        if any(re.match(r"^[❯›>]?\s*[1-9]\.\s", text) for text in texts):
            return True
    return False


def _parse_doubao_confirm_card(
    lines: List[str], mask: List[bool], rule_line: int, footer_line: int, footer: str
) -> Optional[Dict[str, Any]]:
    body = [lines[i] for i in range(rule_line + 1, footer_line) if not mask[i]]
    while body and not body[-1].strip():
        body.pop()
    block: List[Tuple[bool, str]] = []
    index = len(body) - 1
    while index >= 0:
        if not body[index].strip():
            break
        row = _doubao_confirm_row(body[index])
        if row is None:
            break
        block.insert(0, row)
        index -= 1
    if not block:
        return None
    selected = next((i + 1 for i, (cursor, _text) in enumerate(block) if cursor), None)
    if selected is None:
        return None
    options = [text for _cursor, text in block]
    actions = [classify_doubao_option(text) for text in options]
    header_rows = [row.strip() for row in body[: index + 1] if row.strip()]
    # The renderer draws the environment and tool name as dim rows above the server's title.
    tool = ""
    while header_rows and header_rows[0].lower() in _DOUBAO_TOOL_NAMES:
        name = header_rows.pop(0)
        if name.lower() not in ("terminal", "shell", "cmd"):
            tool = tool or name
    question_text = "\n".join(header_rows).strip()
    above = [lines[i] for i in range(0, rule_line) if not mask[i]]
    command = _doubao_running_command(above)
    fallback = _doubao_last_question(above)
    details = question_text or fallback
    title = ""
    if question_text:
        title = question_text.splitlines()[0].strip()
    elif fallback:
        title = fallback.splitlines()[0].strip()
    if not title:
        title = "Doubao Confirmation"
    prompt_parts: List[str] = []
    if command and _doubao_collapsed(command) not in _doubao_collapsed(details):
        prompt_parts.append(command)
    if details:
        prompt_parts.append(details)
    if not prompt_parts:
        prompt_parts.append(title)
    return {
        "kind": "approval_request",
        "title": title,
        "prompt": "\n".join(prompt_parts),
        "options": options,
        "option_actions": actions,
        "selected_index": selected,
        "tool": tool,
        "command": command,
        "esc_action": _doubao_esc_action(footer),
        # The card's own details are part of its identity: two folder-access cards that differ only
        # in the directory (or the consequence text) they name must never share a signature.
        "signature": _doubao_signature(
            "approval_request",
            _doubao_collapsed(title),
            # `details` (header text or the whole multi-line `◇` fallback question), never just the
            # first line: two cards differing only in a later fallback line are different cards.
            _doubao_collapsed(details),
            _doubao_collapsed(command),
            options,
        ),
        "doubao_prompt": "approval",
        "raw_options_count": len(options),
    }


def _doubao_question_header_row(text: str) -> bool:
    """A `◆`-less body row that reads as a question title rather than a wrapped row's tail."""
    low = str(text or "").strip().lower()
    return low.startswith("(required)") or bool(re.search(r"[?？]\s*$", low))


def _parse_doubao_question_card(
    lines: List[str], mask: List[bool], rule_line: int, footer_line: int, footer: str
) -> Optional[Dict[str, Any]]:
    if rule_line >= 0:
        start = rule_line + 1
    else:
        last_tool = max(
            (
                i
                for i in range(0, footer_line)
                if not mask[i] and lines[i].strip().startswith(TOOL_GLYPH)
            ),
            default=-1,
        )
        start = last_tool + 1
    body = [lines[i] for i in range(start, footer_line) if not mask[i]]
    first = next(
        (i for i, row in enumerate(body) if _doubao_question_row(row)[0] is not None), None
    )
    if first is None:
        return None
    header_rows = [row.strip() for row in body[:first] if row.strip()]
    entries: List[Dict[str, Any]] = []
    # Rows between two card rows: a wrapped label's tail, or the next question's title.
    interleaved: List[str] = []
    for row in body[first:]:
        if not row.strip():
            if entries:
                break
            continue
        kind, cursor, checked, text, marker = _doubao_question_row(row)
        if kind is None:
            if not entries:
                continue
            if _doubao_question_header_row(text):
                interleaved.append(text)
            else:
                entries[-1]["text"] = f"{entries[-1]['text']} {text}".strip()
            continue
        entries.append(
            {"kind": kind, "cursor": cursor, "checked": checked, "text": text, "marker": marker}
        )
    # The renderer flattens every question of the card into ONE list in visual order: each
    # question's options, then that question's text row (`Other:`/`Answer:`), then the actions.
    # Row numbers must come from that list, never from `len(options) + 1`.
    nav_rows: List[Dict[str, Any]] = []
    options: List[str] = []
    actions: List[str] = []
    action_decisions: List[str] = []
    checked_indices: List[int] = []
    other_index: Optional[int] = None
    answer_rows = 0
    option_rows = 0
    multi_select = False
    selected: Optional[int] = None
    for entry in entries:
        detail = _doubao_question_detail(entry["text"])
        if entry["kind"] == "option":
            label = _doubao_question_label(entry["text"])
            option_rows += 1
            options.append(label)
            nav_rows.append(
                {"kind": "option", "label": label, "detail": detail,
                 "checked": bool(entry["checked"]), "marker": entry["marker"]}
            )
            if entry["marker"] == "checkbox":
                multi_select = True
            if entry["checked"]:
                checked_indices.append(len(nav_rows))
        elif entry["kind"] == "text":
            kind = "Answer" if entry["text"].lower().startswith("answer") else "Other"
            if kind == "Answer":
                answer_rows += 1
            else:
                options.append("Other")
                if other_index is None:
                    other_index = len(nav_rows) + 1
            nav_rows.append({"kind": "text", "label": kind, "detail": kind})
        else:
            label = _doubao_question_label(entry["text"])
            decision = _doubao_action_decision(entry["text"]) or ""
            actions.append(label)
            action_decisions.append(decision)
            nav_rows.append(
                {"kind": "action", "label": label, "detail": label, "decision": decision}
            )
        if entry["cursor"]:
            selected = len(nav_rows)
    prompt_text = "\n".join(header_rows).strip()
    if prompt_text.startswith("(required)"):
        prompt_text = prompt_text[len("(required)"):].strip()
    if not prompt_text:
        return None
    # A text row BEFORE a later option row, an `Answer:` row beside radio/checkbox rows, two
    # `Answer:` rows or two question titles can only come from several questions flattened into
    # one card: an answer keyed to the wrong question would be submitted otherwise.
    text_before_option = any(
        row["kind"] == "text"
        and any(later["kind"] == "option" for later in nav_rows[pos + 1:])
        for pos, row in enumerate(nav_rows)
    )
    header_count = 1 + sum(1 for row in interleaved if _doubao_question_header_row(row))
    multi_question = bool(
        answer_rows > 1
        or (answer_rows >= 1 and option_rows >= 1)
        or text_before_option
        or header_count > 1
    )
    # Structural, never from the footer: the footer switches to `Type to answer` only while the
    # cursor sits on the text row, and moving onto `Skip` must keep the same card identity.
    free_text_only = answer_rows > 0 and not options
    if not (nav_rows or free_text_only):
        return None
    if not (options or free_text_only):
        return None
    if selected is None:
        selected = 1
    return {
        "kind": "question",
        "title": "Doubao Question",
        "prompt": prompt_text,
        "options": options,
        "option_actions": [],
        "actions": actions,
        "action_decisions": action_decisions,
        "nav_rows": nav_rows,
        "selected_index": selected,
        "other_option_index": other_index,
        "multi_select": multi_select,
        "multi_question": multi_question,
        "free_text_only": free_text_only,
        "checked_indices": checked_indices,
        "esc_action": _doubao_esc_action(footer),
        # Identity: the prompt, every row's label/description and its kind. The cursor, the
        # checked marks, the typed text and the footer's `Type to answer` switch all move without
        # changing which card this is.
        "signature": _doubao_signature(
            "question",
            _doubao_collapsed(prompt_text),
            [[row["kind"], _doubao_collapsed(row["detail"])] for row in nav_rows],
            # Every later question's heading is part of the card's identity too.
            [_doubao_collapsed(row) for row in interleaved],
        ),
        "doubao_prompt": "question",
        "raw_options_count": len(options) - (1 if other_index else 0),
    }


def _doubao_esc_action(footer: str) -> str:
    match = re.search(r"\bEsc\s+(stop|skip|back)\b", str(footer or ""), re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _doubao_footer_kind(text: str) -> str:
    """`confirm` / `question` / "" for one footer text (a joined wrapped footer included)."""
    low = str(text or "").lower()
    if "number to select" in low:
        return ""
    if (
        ("↑↓" in text or "navigate" in low)
        and ("enter / shortcut confirm" in low or "enter confirm" in low)
        and "esc" in low
        and "ctrl+s submit" not in low
    ):
        return "confirm"
    if (
        "ctrl+s submit" in low
        and "esc" in low
        and ("space/enter select" in low or "type to answer" in low or "↑↓ move" in low)
    ):
        return "question"
    return ""


def _doubao_footer(lines: List[str], active: List[int]) -> Tuple[Optional[int], str]:
    """`(first row of the footer, joined footer text)` for the bottom of the active region.

    Ink wraps the footer at narrow widths (`↑↓ navigate | Enter / shortcut confirm` /
    `| Esc stop`), so the last 1..3 contiguous non-empty rows are joined; a blank row, a card row,
    a rule, the idle composer or a row that is already a whole footer above them ends the footer.
    """
    indexes = [i for i in active if lines[i].strip()]
    if not indexes:
        return None, ""
    end = len(indexes) - 1
    start = end
    while end - start + 1 < _DOUBAO_FOOTER_MAX_LINES and start - 1 >= 0:
        previous = indexes[start - 1]
        if indexes[start] - previous != 1:
            break
        text = lines[previous].strip()
        if (
            _RULE_ROW_RE.match(text)
            or text[:1] in _CURSOR_GLYPHS
            or _RADIO_MARK_RE.match(text)
            or _CHECKBOX_MARK_RE.match(text)
            or _doubao_confirm_row(text) is not None
            or _doubao_action_decision(text) is not None
            or DOUBAO_COMPOSER_PLACEHOLDER in text
            or bool(_doubao_footer_kind(text))
        ):
            break
        start -= 1
    return indexes[start], " ".join(lines[i].strip() for i in indexes[start:end + 1])


def _doubao_active_lines(lines: List[str]) -> List[int]:
    """Rows below the newest `◆`/`▍` block: the region the CLI draws its live UI in."""
    mask = _assistant_block_mask(lines)
    last_content = max((i for i in range(len(lines)) if mask[i]), default=-1)
    return [i for i in range(last_content + 1, len(lines)) if not mask[i]]


def doubao_card_submitting(screen_text: str) -> bool:
    """True while a card's busy footer (`Submitting, waiting for confirmation…`) is on screen.

    The busy footer replaces the card's own footer, so the card no longer parses; it is still the
    same card being closed by the CLI, never proof that the answer landed.
    """
    lines = [line.rstrip() for line in str(screen_text or "").splitlines()]
    needle = DOUBAO_SUBMITTING_TEXT.lower()
    return any(
        needle in lines[i].lower() for i in _doubao_active_lines(lines)
    )


def doubao_has_idle_composer(screen_text: str) -> bool:
    """True only when the LIVE region (below the last answer) draws the idle composer.

    An answer that quotes `Ask Doubao to do anything` must not look like the composer: that phrase
    above a live card is prose, not proof that the card is gone.
    """
    lines = [line.rstrip() for line in str(screen_text or "").splitlines()]
    return any(
        lines[i].strip()[:1] in _CURSOR_GLYPHS
        and DOUBAO_COMPOSER_PLACEHOLDER in lines[i]
        for i in _doubao_active_lines(lines)
    )


_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
# A row that starts a genuine top-level TUI region, so a `◆` block ends above it.
_BLOCK_BOUNDARY_PREFIXES = (ASSISTANT_GLYPH, USER_GLYPH, TOOL_GLYPH, "❯", "›", ">")


def _doubao_top_level_row(line: str) -> bool:
    """A row the CLI draws as chrome around the conversation rather than inside an answer."""
    text = line.strip()
    return (
        not text
        or text.startswith(_BLOCK_BOUNDARY_PREFIXES)
        or bool(_RULE_ROW_RE.match(line))
        or _doubao_question_row(text)[0] is not None
        or _doubao_confirm_row(text) is not None
        or _doubao_action_decision(text) is not None
        or bool(re.search(r"(?:↑↓.*Esc|Ctrl\+S submit|Enter / shortcut confirm)", text))
        or DOUBAO_SUBMITTING_TEXT.lower() in text.lower()
    )


def _assistant_block_mask(lines: List[str]) -> List[bool]:
    """True for rows that are `◆` assistant prose (the glyph row, its wrapped rows, its fences).

    An answer may quote a whole card — rule, `❯ Allow once`, even its footer — and it may be
    separated by a single blank line. Inside a Markdown code fence nothing is a boundary; outside
    one, a real top-level row (`▍`, `◇`, `◆`, the composer cursor, a card row, a card footer, a
    horizontal rule) ends the block. A card the CLI draws BELOW an answer therefore still parses:
    its rows are outside every fence.
    """
    mask = [False] * len(lines)
    index = 0
    while index < len(lines):
        text = lines[index].strip()
        if not text.startswith((ASSISTANT_GLYPH, USER_GLYPH)):
            index += 1
            continue
        mask[index] = True
        in_fence = False
        index += 1
        while index < len(lines):
            row = lines[index].strip()
            if _FENCE_RE.match(lines[index]):
                in_fence = not in_fence
                mask[index] = True
                index += 1
                continue
            if in_fence:
                mask[index] = True
                index += 1
                continue
            if not row:
                # One blank line inside a paragraph; a genuine boundary below still ends the block.
                following = next(
                    (lines[i] for i in range(index + 1, len(lines)) if lines[i].strip()), ""
                )
                mask[index] = True
                index += 1
                if not following or _doubao_top_level_row(following):
                    break
                continue
            if _doubao_top_level_row(lines[index]):
                break
            mask[index] = True
            index += 1
    return mask


def parse_doubao_prompt(screen_text: str) -> Optional[Dict[str, Any]]:
    """The live Ink card on a Doubao screen, or None.

    Verified against `doubao` 2.37.2 (`entry-cli-router/main.js`, module 5501): a real card ends
    with its own footer row and replaces the composer while it is open —

    - confirmation: `↑↓ navigate | Enter / shortcut confirm | Esc stop|skip|back`, options are the
      server's unnumbered labels (`Allow once`, `Reject`, …) with the Ink cursor on one of them;
    - question (`ask`): `Ctrl+S submit | Space/Enter select | ↑↓ move | Esc skip` (or
      `Type to answer | Ctrl+S submit`), radio `( )`/`(●)` and checkbox `[ ]`/`[✓]` rows, then
      `Submit` / `Skip` / `Reject and stop` action rows.

    The footer must be the bottom row of the active region (below the last `◆`/`▍` block): a card
    that scrolled above the idle composer, a half-drawn frame, assistant prose quoting a card and
    the numbered `/permissions` menu all fail that anchor and return None. A footer Ink wrapped
    over 2–3 rows at a narrow width is joined before it is matched.
    """
    if not str(screen_text or "").strip():
        return None
    lines = [line.rstrip() for line in str(screen_text).splitlines()]
    mask = _assistant_block_mask(lines)
    last_content = max((i for i in range(len(lines)) if mask[i]), default=-1)
    active = [i for i in range(last_content + 1, len(lines)) if not mask[i]]
    if not active:
        return None
    footer_line, footer = _doubao_footer(lines, active)
    if footer_line is None:
        return None
    kind = _doubao_footer_kind(footer)
    if not kind:
        return None
    if _doubao_permissions_menu([lines[i] for i in active]):
        return None
    rule_line = next(
        (i for i in reversed(active) if i < footer_line and _RULE_ROW_RE.match(lines[i])), -1
    )
    if kind == "confirm":
        if rule_line < 0:
            return None
        return _parse_doubao_confirm_card(lines, mask, rule_line, footer_line, footer)
    return _parse_doubao_question_card(lines, mask, rule_line, footer_line, footer)


def doubao_screen_reply_after_user_prompt(lines: Any) -> bool:
    """True when the screen's newest `◆` block was drawn below its newest `▍` user prompt."""
    rows = _screen_lines(lines)
    last_user = -1
    last_assistant = -1
    for index, row in enumerate(rows):
        text = row.strip()
        if text.startswith(USER_GLYPH):
            last_user = index
        elif text.startswith(ASSISTANT_GLYPH):
            last_assistant = index
    return last_assistant > last_user >= 0
