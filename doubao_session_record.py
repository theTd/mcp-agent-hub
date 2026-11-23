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

_DOUBAO_APPROVAL_TITLES = (
    "Allow this command?",
    "Allow all operations?",
    "Allow access to these directories?",
    "Files will be deleted",
)
_DOUBAO_OPTION_RE = re.compile(r"^\s*(?:[❯›>]\s*)?(\d+)\.\s+(.+?)\s*$")
# The idle composer of the Doubao TUI (drawn in place of typed text).
DOUBAO_COMPOSER_PLACEHOLDER = "Ask Doubao to do anything"
_DOUBAO_PROMPT_RE = re.compile(r"^\s*❯\s?(.*)$")
# The card's own chrome: a real card carries a footer/hint row, never only numbered prose.
_CARD_FOOTER_HINTS = (
    "submit",
    "press enter",
    "enter / esc",
    "↑↓",
    "navigate",
    "to select",
    "number to select",
    "esc to cancel",
    "esc to exit",
)


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


def _last_numbered_options(lines: List[str]) -> List[Dict[str, Any]]:
    """The last block of `N. label` rows; wrapped continuation rows join the label before them.

    The block stops at horizontal rules, the `model · effort · cwd` status row, key-hint footers
    (`↑↓`, `Enter / Esc`, `number to select`) and the composer prompt, so the last option never
    swallows the screen chrome drawn under the card.
    """
    blocks: List[List[Dict[str, Any]]] = []
    current: Optional[List[Dict[str, Any]]] = None
    for row in lines:
        match = _DOUBAO_OPTION_RE.match(row)
        if match:
            number = int(match.group(1))
            label = match.group(2).strip()
            if current is None or number == 1:
                current = []
                blocks.append(current)
            current.append({"index": number, "label": label})
            continue
        stripped = row.strip()
        low = stripped.lower()
        if (
            not stripped
            or _RULE_ROW_RE.match(stripped)
            or _STATUS_ROW_RE.match(stripped)
            or stripped.startswith(("❯", "›", ">", "─", "━", "═"))
            or any(hint in low for hint in _CARD_FOOTER_HINTS)
        ):
            current = None
            continue
        if current:
            current[-1]["label"] = f"{current[-1]['label']} {stripped}".strip()
    if not blocks:
        return []
    # Drop repeated indexes inside one block (a redraw can repeat a row).
    seen = set()
    cleaned = []
    for item in blocks[-1]:
        if item["index"] in seen:
            continue
        seen.add(item["index"])
        cleaned.append(item)
    cleaned.sort(key=lambda item: item["index"])
    return cleaned


def classify_doubao_option(label: str) -> str:
    """approve_once | approve_session | deny | deny_feedback for one Doubao option label."""
    low = str(label or "").strip().lower()
    if "reject and stop" in low or "deny and stop" in low or "拒绝并停止" in low or "拒绝并终止" in low:
        return "deny_feedback"
    if low.startswith("allow once") or low.startswith("允许一次") or low.startswith("仅允许一次"):
        return "approve_once"
    if (
        low.startswith("allow similar")
        or low.startswith("allow all")
        or "always" in low
        or "允许类似" in low
        or "允许所有" in low
        or "全部允许" in low
    ):
        return "approve_session"
    if low.startswith(("reject", "deny", "cancel", "no", "拒绝", "取消")):
        return "deny"
    if low.startswith("submit"):
        return "submit"
    if low.startswith(("yes", "allow", "approve", "proceed", "delete", "continue", "允许", "批准", "继续", "删除")):
        return "approve_once"
    # A label the bundle did not describe is never read as an approval.
    return "deny"


def _assistant_block_mask(lines: List[str]) -> List[bool]:
    """True for rows that are `◆` assistant prose (the glyph row and its wrapped rows)."""
    mask = [False] * len(lines)
    index = 0
    while index < len(lines):
        text = lines[index].strip()
        if not text.startswith((ASSISTANT_GLYPH, USER_GLYPH)):
            index += 1
            continue
        mask[index] = True
        index += 1
        while index < len(lines):
            row = lines[index].strip()
            if (
                not row
                or row.startswith((ASSISTANT_GLYPH, USER_GLYPH, TOOL_GLYPH, "❯", "›", ">"))
                or _RULE_ROW_RE.match(lines[index])
            ):
                break
            mask[index] = True
            index += 1
    return mask


def _composer_rows(lines: List[str]) -> set:
    """Indexes of the CLI's composer (the bottom `❯` row, or the row framed by rules).

    A card's cursor row can start with `❯` too; it is kept when it is an option row and a card
    footer sits under it. Everything else at the bottom is the input box, not card content.
    """
    prompt_rows = [i for i, row in enumerate(lines) if _DOUBAO_PROMPT_RE.match(row)]
    excluded: set = set()
    if not prompt_rows:
        return excluded
    last = prompt_rows[-1]
    text = _DOUBAO_PROMPT_RE.match(lines[last]).group(1).strip()
    below = [row.strip().lower() for row in lines[last + 1:] if row.strip()]
    footer_below = any(hint in row for row in below for hint in _CARD_FOOTER_HINTS)
    # An option row stays when the card continues under it (another numbered row or a footer);
    # a lone `❯ 1. draft text` is the user's composer, not the card's first choice.
    option_below = any(_DOUBAO_OPTION_RE.match(row) for row in lines[last + 1:])
    is_card_row = bool(_DOUBAO_OPTION_RE.match(lines[last])) and (footer_below or option_below)
    if not (text and is_card_row):
        excluded.add(last)
    # A composer framed by rules is the input box, never a card row.
    if last > 0 and _RULE_ROW_RE.match(lines[last - 1]):
        for index in range(last + 1, min(len(lines), last + 4)):
            if _RULE_ROW_RE.match(lines[index]):
                excluded.update(range(last - 1, index + 1))
                break
    return excluded


def parse_doubao_prompt(screen_text: str) -> Optional[Dict[str, Any]]:
    """An approval card or a numbered question card on a Doubao screen, else None.

    Only the bottom active region counts: below the last `◆`/`▍` block and outside the composer.
    Approval titles are the bundle's own, and a question needs a `?`/`？` prompt row plus a
    numbered list whose cursor (`❯`/`›`) is drawn on an option row. A numbered list quoted inside
    an assistant answer, or the idle composer's own `1. …` draft, is never taken for a card.
    """
    if not str(screen_text or "").strip():
        return None
    lines = [line.rstrip() for line in str(screen_text).splitlines()]
    mask = _assistant_block_mask(lines)
    composer = _composer_rows(lines)
    last_content = max((i for i in range(len(lines)) if mask[i]), default=-1)
    scan = [
        lines[i]
        for i in range(last_content + 1, len(lines))
        if not mask[i] and i not in composer
    ]
    title = ""
    for row in scan:
        for phrase in _DOUBAO_APPROVAL_TITLES:
            if phrase in row:
                title = phrase
                break
        if title:
            break
    options = _last_numbered_options(scan)
    cursor_led = any(
        _DOUBAO_OPTION_RE.match(row) and row.strip().startswith(("❯", "›")) for row in scan
    )
    footer = any(
        any(hint in row.strip().lower() for hint in _CARD_FOOTER_HINTS) for row in scan
    )
    if title:
        # A quoted title in prose is not a card: a real card draws its cursor or a footer hint.
        if not (cursor_led or footer):
            return None
        labels = [item["label"] for item in options if item["label"].lower() != "submit"]
        actions = [classify_doubao_option(label) for label in labels]
        if not labels:
            return None
        return {
            "kind": "approval_request",
            "title": title,
            "prompt": title,
            "options": labels,
            "option_actions": actions,
            "doubao_prompt": "approval",
        }
    if not options:
        return None
    if not cursor_led:
        return None
    prompt = ""
    for row in reversed(scan):
        text = row.strip()
        if text.endswith(("?", "？")) and not _is_chrome(row):
            prompt = text
            break
    if not prompt:
        return None
    labels = [item["label"] for item in options]
    return {
        "kind": "question",
        "title": "Doubao Question",
        "prompt": prompt,
        "options": labels,
        "option_actions": [],
        "raw_options_count": len(labels),
    }


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
