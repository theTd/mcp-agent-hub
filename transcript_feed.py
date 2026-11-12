"""Engine-neutral transcript feed for the console's default session view.

The Hub already normalizes every engine into worker events (tool calls and results, assistant
text, reasoning, questions, approvals, lifecycle) plus a few persisted side tables.  This module
turns those records into a reading-order list of entries for `GET /api/sessions/{id}/transcript`:

* a tool call and its result become one `tool` entry (paired by call id, else in call order);
* engine wrappers around tool output (`[{"type": "input_text", ...}]`) are unwrapped to text;
* an answered question or approval carries its answer instead of a loose parent bubble;
* delivery receipts and approval proxy traces stay out of the reading flow;
* an assistant message repeated as the turn's final reply is shown once.

It never reads engine-specific files or fields, and it does no I/O: `HubSessionManager` gathers
the records and calls `build_transcript_feed`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

FEED_DEFAULT_LIMIT = 80
FEED_MAX_LIMIT = 400
TEXT_LIMIT = 6000
# hub_database keeps this many characters of an intermediate message; the worker event that
# carries the same text keeps more, so the two copies are matched on this head.
STORED_MESSAGE_HEAD = 4000

TOOL_EVENT_TYPES = ("tool_call", "tool_result")
STEP_MARKER_TOOL = "llm_inference"  # pre-data_json events marked Hub step counts with this name
LIVE_STATUSES = ("starting", "running", "waiting_parent")
ASSISTANT_EVENT_TYPES = ("progress", "message", "message_to_parent")
INTERACTION_EVENT_TYPES = ("question", "approval_request")
NOTICE_EVENTS = {
    "exited": ("info", "进程已结束"),
    "session_exited": ("info", "会话已结束"),
    "terminated": ("warn", "会话已终止"),
    "crashed": ("error", "进程异常退出"),
    "turn_aborted": ("warn", "本轮已中断"),
    "error": ("error", "错误"),
    "resumed": ("info", "会话已恢复"),
    "delivery_failed": ("error", "消息投递失败"),
}
# Intermediate-message sources that only trace Hub plumbing; their content reaches the feed
# through the parent message or the interaction resolution instead.
HIDDEN_MESSAGE_SOURCES = frozenset({"parent_approval_proxy"})
# Engines whose record readers used to join the model's thinking and its reply into one message
# (now recorded as separate reasoning events): in older records the text in front of a repeated
# final reply is that thinking.
THINKING_JOINED_ENGINES = frozenset({"claude", "kimi", "omp", "agy"})
# The note the hub appends to text it clipped before storing it (session_manager._clip_detail).
CLIP_NOTE = re.compile(r"\s*…（已截断，原文 \d+ 字符）\s*$")
TASK_SUFFIXES = (
    re.compile(r"\s*\[Sub-Agent Bridge Info[\s\S]*$", re.IGNORECASE),
    re.compile(r"\s*\[Hub Session: session_id=[^\]]+\]"),
)
FAILED_OUTPUT = re.compile(
    r"^(?:Script failed|Error\b|error:|Traceback \(most recent call last\))"
    r"|\b(?:exit code|exited with code|Exit code)[:\s]+-?[1-9]\d*",
    re.MULTILINE,
)
# The two alternatives must not overlap (a backslash is only ever the start of an escape):
# otherwise a Windows path full of backslashes backtracks exponentially.
COMMAND_IN_CODE = re.compile(r"\b(?:cmd|command)\s*:\s*([\"'`])((?:\\.|(?!\1)[^\\])+)\1", re.DOTALL)
COMMAND_LIST_IN_CODE = re.compile(
    r"\b(?:cmds|commands)\s*=\s*\[\s*([\"'`])((?:\\.|(?!\1)[^\\])+)\1\s*(,\s*[\"'`])?")
TOOL_CALLS_IN_CODE = re.compile(r"\btools\.([A-Za-z_][\w]*)\s*\(")
# The first string argument of a call, positional or the first field of an options object.
TOOL_CALL_FIRST_ARG = re.compile(
    r"\btools\.[A-Za-z_]\w*\s*\(\s*\{?\s*(?:\w+\s*:\s*)?([\"'`])((?:\\.|(?!\1)[^\\])+)\1")
# An absolute path with at least three directories before its last part (see _compact_paths).
LONG_PATH = re.compile(r"(?<![\w:/.\\])(?:[A-Za-z]:)?[\\/](?:[^\s\"'`\\/]+[\\/]){3,}(?=[^\s\"'`\\/])")
# SessionManager's worker->parent notice (see register_pending_interaction); the stored copy may
# be clipped, so everything after the details is optional.
PARENT_NOTICE = re.compile(
    r"(?:Approval request|Interactive question)\n- Engine: [^\n]*\n- Title: (?P<title>[^\n]*)\n"
    r"- Details: (?P<details>.*?)(?:\n- Options:\n(?P<options>.*?))?(?:\nUse respond_to_\w+\(.*)?\Z",
    re.DOTALL,
)
NOTICE_OPTION = re.compile(r"^\s*\[\d+\]\s+(.+)$", re.MULTILINE)
# A Hub-written intermediate message starts with a bracketed tag ("[Parent replied] ...");
# a Markdown link at the start of an agent message is not one.
NOTICE_TAG = re.compile(r"\[[^\]\n]{1,58}\](?!\()")
MARKDOWN_EMPHASIS = re.compile(r"\*\*|`")
# Lead-ins that say nothing about the request itself (see interaction_summary).
GENERIC_ASK_LINES = frozenset({"Would you like to run the following command?"})
# apply_patch input; inside a code cell the patch is a string literal with escaped newlines.
PATCH_FILE_OP = re.compile(r"\*\*\* (Add|Update|Delete) File: (.+?)(?=\\n|\n|\"|$)")
TITLE_KEYS = ("description", "title", "command", "cmd", "file_path", "path", "filePath",
              "pattern", "query", "url", "prompt", "text")


def _text(value: Any, limit: int = TEXT_LIMIT) -> str:
    text = str(value if value is not None else "").replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（已截断，原文 {len(text)} 字符）"


def _ts(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _seq(event: Dict[str, Any]) -> int:
    try:
        return int(event.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _one_line(text: str, limit: int = 160) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line and line not in ("{", "[", "(", "```"):
            return line if len(line) <= limit else f"{line[:limit - 1]}…"
    return ""


def tool_output_text(value: Any) -> str:
    """Plain text of a tool output, unwrapping `[{"type": "...", "text": ...}]` content lists."""
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, dict):
                if part.get("text"):
                    parts.append(str(part["text"]))
                elif "image" in str(part.get("type") or ""):
                    parts.append("[图片]")
            elif part is not None:
                parts.append(str(part))
        return "\n".join(parts).strip()
    text = str(value if value is not None else "").strip()
    if text.startswith("[{") and '"type"' in text[:80]:
        try:
            parsed = json.loads(text)
        except ValueError:
            # Older records clipped the JSON; keep every complete or partial text part.
            parts = []
            for match in re.finditer(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)("?)', text):
                raw = match.group(1)
                try:
                    parts.append(json.loads(f'"{raw}"'))
                except ValueError:
                    parts.append(raw.replace("\\n", "\n"))
                if not match.group(2):
                    parts.append("…")
                    break
            if '"input_image"' in text or '"image_url"' in text:
                parts.append("[图片]")
            return "\n".join(parts).strip() if parts else text
        if isinstance(parsed, list):
            return tool_output_text(parsed)
    return text


def _compact_paths(text: str) -> str:
    r"""`C:\Users\me\a\b\tool.py run` -> `…\tool.py run`: in a one-line title the file name is what counts."""
    return LONG_PATH.sub(lambda match: "…\\" if "\\" in match.group(0) else "…/", text)


def tool_title(name: str, tool_input: str, explicit: str = "") -> str:
    """A one-line description of what a tool call does, from its own arguments (`name` is the tool)."""
    return _compact_paths(_tool_title(tool_input, explicit))


def _tool_title(tool_input: str, explicit: str) -> str:
    if explicit and explicit.strip():
        return _one_line(explicit, 200)
    text = str(tool_input or "").strip()
    if not text:
        return ""
    if text[:1] in "{[":
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for key in TITLE_KEYS:
                value = parsed.get(key)
                if isinstance(value, list):
                    value = " ".join(str(item) for item in value)
                if isinstance(value, str) and value.strip():
                    return _one_line(value, 200)
            # No describing argument: name the first few scalar ones (e.g. `cell_id=88`).
            scalars = [f"{key}={value}" for key, value in parsed.items()
                       if isinstance(value, (str, int, float)) and not isinstance(value, bool)
                       and str(value).strip()][:3]
            if scalars:
                return _one_line(" · ".join(scalars), 200)
    if "*** Begin Patch" in text:
        files = PATCH_FILE_OP.findall(text)
        if files:
            verb, path = files[0]
            more = f" 等 {len(files)} 个文件" if len(files) > 1 else ""
            return _one_line(f"apply_patch · {verb} {path.replace(chr(92) * 2, chr(92))}{more}", 200)
    commands = COMMAND_LIST_IN_CODE.search(text)
    if commands:
        first = commands.group(2)
        if commands.group(1) == '"':
            try:
                first = json.loads(f'"{first}"')
            except ValueError:
                pass
        return _one_line(first, 200) + (" 等" if commands.group(3) else "")
    command = COMMAND_IN_CODE.search(text)
    if command:
        raw = command.group(2)
        try:
            raw = json.loads(f'"{raw}"') if command.group(1) == '"' else raw
        except ValueError:
            pass
        return _one_line(raw, 200)
    called = list(dict.fromkeys(TOOL_CALLS_IN_CODE.findall(text)))
    if len(called) == 1:
        argument = TOOL_CALL_FIRST_ARG.search(text)
        if argument:
            value = argument.group(2)
            if argument.group(1) == '"':
                try:
                    value = json.loads(f'"{value}"')
                except ValueError:
                    pass
            return _one_line(f"{called[0]} · {value}", 200)
    if called:
        return "调用 " + "、".join(called[:4]) + (" 等" if len(called) > 4 else "")
    return _one_line(text, 200)


def pretty_tool_input(text: str) -> str:
    """Indent JSON arguments so the expanded view is readable; leave code and prose alone."""
    stripped = str(text or "").strip()
    if stripped[:1] in "{[":
        try:
            return json.dumps(json.loads(stripped), ensure_ascii=False, indent=2)
        except ValueError:
            pass
    return stripped


def _split_legacy(content: str) -> tuple:
    """`name: body` as written by older Hub versions; the name never contains spaces."""
    head, sep, body = str(content or "").partition(": ")
    if sep and head and " " not in head and len(head) <= 120:
        return head, body
    return "", str(content or "")


def _looks_failed(output: str) -> bool:
    return bool(FAILED_OUTPUT.search(output[:400]))


def _clean_task(text: str) -> str:
    for pattern in TASK_SUFFIXES:
        text = pattern.sub("", text)
    return text.strip()


def parse_parent_notice(text: str) -> Optional[Dict[str, Any]]:
    """Split the Hub's worker->parent notice of an approval or question into its parts.

    Events recorded before the interaction itself was stored with the event carry only this
    notice, which is written for the parent agent (engine, request id, reply instructions)."""
    match = PARENT_NOTICE.match(str(text or ""))
    if not match:
        return None
    options = [line.group(1).strip() for line in NOTICE_OPTION.finditer(match.group("options") or "")]
    return {"title": match.group("title").strip(), "prompt": match.group("details").strip(), "options": options}


def interaction_view(interaction: Dict[str, Any]) -> Dict[str, Any]:
    """The display fields of an interaction; engine metadata (tokens, ids) stays server-side."""
    metadata = _as_dict(interaction.get("metadata"))
    options = [str(option) for option in (interaction.get("options") or []) if str(option).strip()]
    prompt = _text(metadata.get("display_prompt") or interaction.get("prompt")
                   or interaction.get("question") or interaction.get("text"))
    notice = parse_parent_notice(prompt)
    if notice:
        prompt = notice["prompt"]
        options = options or notice["options"]
    return {
        "request_id": str(interaction.get("request_id") or interaction.get("id") or ""),
        "kind": "approval_request" if interaction.get("kind") == "approval_request" else "question",
        "title": _text(interaction.get("title"), 300),
        "summary": interaction_summary(prompt),
        "prompt": prompt,
        "options": options,
    }


def interaction_summary(prompt: str) -> str:
    """One line saying what is asked: the command an approval would run, else the first line."""
    lines = [line.strip() for line in str(prompt or "").splitlines() if line.strip()]
    for line in lines:
        if line.startswith("$ "):
            return _compact_paths(_one_line(line[2:], 200))
    for line in lines:
        plain = MARKDOWN_EMPHASIS.sub("", line)
        if plain and plain not in GENERIC_ASK_LINES:
            return _compact_paths(_one_line(plain, 200))
    return ""


def _same_or_clipped(text: str, final: str) -> bool:
    """`text` repeats the final reply, or previews its start (job logs collapse line breaks)."""
    if not text or not final:
        return False
    if text == final:
        return True
    head = " ".join(CLIP_NOTE.sub("", text).split()).rstrip("…").rstrip(".").rstrip()
    return len(head) >= 40 and " ".join(final.split()).startswith(head)


def _split_streamed_thoughts(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Older Kimi records hold each step twice: the streamed thought, later thought + text joined.

    When a later, longer message starts with an earlier one, the earlier one is that step's
    thinking; the later one keeps only its own text and goes away when that text is already shown.
    """
    assistants = [item for item in entries if item["kind"] == "assistant" and not item.get("channel")]
    drop = set()
    for index, early in enumerate(assistants):
        head = early["text"].strip()
        if len(head) < 40 or id(early) in drop:
            continue
        for later in assistants[index + 1:]:
            body = later["text"].strip()
            if id(later) in drop or len(body) <= len(head) or not body.startswith(head):
                continue
            early["kind"] = "thinking"
            rest = body[len(head):].strip()
            later["text"] = rest
            if not rest or any(other is not later and other["text"].strip() == rest for other in assistants):
                drop.add(id(later))
            break
    return [item for item in entries if id(item) not in drop]


def _text_before_reply(text: str, final: str) -> str:
    """The text in front of a (possibly clipped) copy of the final reply inside `text`, else ""."""
    reply = final.strip()
    probe = reply[:200]
    if len(probe) < 40:
        return ""
    at = text.find(probe)
    if at <= 0 or not _same_or_clipped(text[at:].strip(), reply):
        return ""
    return text[:at].rstrip()


def build_transcript_feed(
    *,
    session_id: str,
    meta: Dict[str, Any],
    events: Iterable[Dict[str, Any]],
    messages: Iterable[Dict[str, Any]] = (),
    parent_messages: Iterable[Any] = (),
    pending_interaction: Optional[Dict[str, Any]] = None,
    limit: int = FEED_DEFAULT_LIMIT,
    window_saturated: bool = False,
) -> Dict[str, Any]:
    """Build the console transcript.  `window_saturated` says older events were not fetched."""
    try:
        limit = max(10, min(FEED_MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = FEED_DEFAULT_LIMIT

    entries: List[Dict[str, Any]] = []
    resolutions: Dict[str, Dict[str, Any]] = {}
    seen_text = set()  # heads (see STORED_MESSAGE_HEAD) of message texts already shown

    task = _clean_task(_text(meta.get("last_task"), TEXT_LIMIT))
    task_entry = {"id": "task", "kind": "task", "text": task, "timestamp": _ts(meta.get("created_at"))} if task else None

    for index, message in enumerate(parent_messages or ()):
        if isinstance(message, dict):
            if message.get("direction") == "worker":
                # An archived thread keeps the Hub's worker->parent notices too; each one copies a
                # worker event (approval, question, report, final answer) the feed already shows.
                continue
            body = _text(message.get("content"))
            mid = str(message.get("id") or message.get("message_id") or f"p{index}")
            stamp = _ts(message.get("timestamp"))
            resolved = str(message.get("resolved_question_id") or "")
        else:
            body, mid, stamp, resolved = _text(message), f"p{index}", None, ""
        if not body:
            continue
        if resolved:
            resolutions[resolved] = {"text": body, "timestamp": stamp, "by": "parent"}
            continue
        entries.append({"id": f"parent:{mid}", "kind": "parent", "text": body, "timestamp": stamp})

    open_tools: List[Dict[str, Any]] = []
    interactions: Dict[str, Dict[str, Any]] = {}
    ordered = sorted(events, key=lambda ev: (_ts(ev.get("timestamp")) or 0.0, _seq(ev)))
    for ev in ordered:
        ev_type = str(ev.get("type") or "message")
        seq = _seq(ev)
        stamp = _ts(ev.get("timestamp"))
        content = _text(ev.get("content"))
        data = _as_dict(ev.get("data"))

        if ev_type == "tool_call":
            legacy_name, legacy_body = _split_legacy(content)
            if data.get("step_marker") or (not data and legacy_name == STEP_MARKER_TOOL):
                continue  # the Hub's step counter, not a tool the agent ran
            name = str(data.get("tool") or legacy_name or "tool")
            raw_input = data.get("input") if data.get("input") is not None else legacy_body
            tool_input = pretty_tool_input(_text(raw_input))
            entry = {
                "id": f"tool:{seq}", "kind": "tool", "timestamp": stamp,
                "tool": {
                    "name": name, "title": tool_title(name, tool_input, str(data.get("title") or "")),
                    "input": tool_input, "output": "", "status": "running",
                    "started_at": stamp, "ended_at": None,
                },
                "_call_id": str(data.get("call_id") or ""),
            }
            entries.append(entry)
            open_tools.append(entry)
        elif ev_type == "tool_result":
            legacy_name, legacy_body = _split_legacy(content)
            call_id = str(data.get("call_id") or "")
            if not call_id and legacy_name.startswith("call_"):
                call_id = legacy_name
            name = str(data.get("tool") or ("" if legacy_name.startswith("call_") else legacy_name))
            output = tool_output_text(data.get("output") if data.get("output") is not None else legacy_body)
            match = next((item for item in open_tools if call_id and item["_call_id"] == call_id), None)
            if match is None and name:
                match = next((item for item in open_tools if item["tool"]["name"] == name and not item["_call_id"]), None)
            if match is None:
                match = next((item for item in open_tools if not (call_id and item["_call_id"])), None)
            if match is None:
                match = {
                    "id": f"tool:{seq}", "kind": "tool", "timestamp": stamp, "_call_id": call_id,
                    "tool": {"name": name or "tool", "title": "", "input": "", "output": "",
                             "status": "running", "started_at": stamp, "ended_at": None},
                }
                entries.append(match)
            else:
                open_tools.remove(match)
            ok = data.get("ok")
            failed = ok is False or (ok is None and _looks_failed(output))
            match["tool"].update({"output": _text(output), "status": "error" if failed else "ok", "ended_at": stamp})
        elif ev_type == "reasoning":
            if content:
                entries.append({"id": f"think:{seq}", "kind": "thinking", "text": content, "timestamp": stamp})
        elif ev_type in ASSISTANT_EVENT_TYPES:
            if content:
                seen_text.add(content[:STORED_MESSAGE_HEAD])
                entry = {"id": f"msg:{seq}", "kind": "assistant", "text": content, "timestamp": stamp}
                if ev_type == "message_to_parent":
                    entry["channel"] = "report"
                entries.append(entry)
        elif ev_type == "turn_completed":
            if content:
                seen_text.add(content[:STORED_MESSAGE_HEAD])
                entries.append({"id": f"final:{seq}", "kind": "final", "text": content, "timestamp": stamp})
        elif ev_type in INTERACTION_EVENT_TYPES:
            interaction = ev.get("interaction") if isinstance(ev.get("interaction"), dict) else None
            if interaction is None and isinstance(data.get("interaction"), dict):
                interaction = data["interaction"]
            if interaction is None:
                interaction = {"request_id": ev.get("request_id") or f"event-{seq}", "kind": ev_type,
                               "title": "需要审批" if ev_type == "approval_request" else "需要回答",
                               "prompt": content}
            view = interaction_view(interaction)
            key = view["request_id"] or f"event-{seq}"
            if key in interactions:
                continue
            entry = {"id": f"ask:{key}", "kind": "interaction", "timestamp": stamp,
                     "interaction": view, "pending": False, "resolution": None}
            interactions[key] = entry
            entries.append(entry)
        elif ev_type == "approval_auto_resolved":
            request_id = str(ev.get("request_id") or "")
            # An automatic approval never raises an approval_request event of its own.
            if isinstance(data.get("interaction"), dict):
                view = interaction_view(data["interaction"])
                key = view["request_id"] or request_id or f"event-{seq}"
                if key not in interactions:
                    entry = {"id": f"ask:{key}", "kind": "interaction", "timestamp": stamp,
                             "interaction": view, "pending": False, "resolution": None}
                    interactions[key] = entry
                    entries.append(entry)
                request_id = request_id or key
            if request_id:
                resolutions.setdefault(request_id, {"text": "已按自动批准规则放行", "timestamp": stamp, "by": "auto"})
        elif ev_type == "input_delivery":
            if str(ev.get("status") or "") == "failed":
                entries.append({"id": f"note:{seq}", "kind": "notice", "tone": "error",
                                "text": f"消息投递失败：{content}" if content else "消息投递失败", "timestamp": stamp})
        elif ev_type in NOTICE_EVENTS:
            tone, label = NOTICE_EVENTS[ev_type]
            text = content if content and content != label else label
            entries.append({"id": f"note:{seq}", "kind": "notice", "tone": tone, "text": text, "timestamp": stamp})
        elif content:
            entries.append({"id": f"note:{seq}", "kind": "notice", "tone": "info", "text": content, "timestamp": stamp})

    for message in messages or ():
        body = _text(message.get("content"))
        source = str(message.get("source") or "")
        if not body or body[:STORED_MESSAGE_HEAD] in seen_text or source in HIDDEN_MESSAGE_SOURCES:
            continue
        seen_text.add(body[:STORED_MESSAGE_HEAD])
        stamp = _ts(message.get("timestamp"))
        mid = f"im:{message.get('timestamp') or len(entries)}"
        if NOTICE_TAG.match(body) or source.endswith("_notice"):
            entries.append({"id": mid, "kind": "notice", "tone": "info", "text": body, "timestamp": stamp})
        elif re.search(r"(?:think|reason)", source.lower()):
            entries.append({"id": mid, "kind": "thinking", "text": body, "timestamp": stamp})
        else:
            entries.append({"id": mid, "kind": "assistant", "text": body, "timestamp": stamp})

    final_text = _text(meta.get("final_response"))
    if final_text and not any(item["kind"] == "final" and item.get("text") == final_text for item in entries):
        # Engines that end a turn without a native completion event still have a stored answer.
        for item in entries:
            if item["kind"] == "assistant" and item.get("text") == final_text:
                item["kind"] = "final"
                break
        else:
            entries.append({"id": "final:stored", "kind": "final", "text": final_text,
                            "timestamp": _ts(meta.get("ended_at")) or _ts(meta.get("updated_at"))})
    if meta.get("from_previous_turn"):
        for item in entries:
            if item["kind"] == "final" and item.get("text") == final_text:
                item["previous_turn"] = True

    pending = pending_interaction if isinstance(pending_interaction, dict) else None
    if pending:
        view = interaction_view(pending)
        key = view["request_id"]
        entry = interactions.get(key)
        if entry is None:
            entry = {"id": f"ask:{key or 'pending'}", "kind": "interaction",
                     "timestamp": _ts(pending.get("created_at")), "interaction": view,
                     "pending": True, "resolution": None}
            interactions[key] = entry
            entries.append(entry)
        entry["pending"] = True
    for key, entry in interactions.items():
        if not entry["pending"] and key in resolutions:
            entry["resolution"] = resolutions[key]

    entries.sort(key=lambda item: (item.get("timestamp") or 0.0))
    engine = str(meta.get("engine") or "").strip().lower()
    if engine in THINKING_JOINED_ENGINES:
        entries = _split_streamed_thoughts(entries)
    # Drop an assistant message that only repeats (or previews) the final reply; one that holds
    # text in front of the reply keeps that text (older records: the thinking).
    finals = [item for item in entries if item["kind"] == "final"]
    if finals:
        drop = set()
        turn_final = None  # the final reply of the current turn, once it has been shown
        for index, item in enumerate(entries):
            if item["kind"] == "final":
                turn_final = item
                continue
            if item["kind"] == "parent":
                turn_final = None
                continue
            if item["kind"] != "assistant":
                continue
            following = next((later for later in entries[index + 1:] if later["kind"] == "final"), None)
            # A stored copy of the reply can be timestamped just after the final itself.
            if any(final and _same_or_clipped(item["text"], final["text"]) for final in (following, turn_final)):
                drop.add(id(item))
                continue
            before = _text_before_reply(item["text"], following["text"]) if following and not item.get("channel") else ""
            if before:
                item["text"] = before
                if engine in THINKING_JOINED_ENGINES:
                    item["kind"] = "thinking"
        entries = [item for item in entries if id(item) not in drop]

    # A call whose result never arrives (screen-detected tools, engines without result records)
    # stops spinning once the agent has moved on or the session is no longer live.
    live = str(meta.get("status") or "") in LIVE_STATUSES
    last_progress = max((i for i, item in enumerate(entries) if item["kind"] not in ("tool", "parent")), default=-1)
    for index, item in enumerate(entries):
        if item["kind"] == "tool" and item["tool"]["status"] == "running" and (not live or index < last_progress):
            item["tool"]["status"] = "unknown"
    for item in entries:
        item.pop("_call_id", None)
    total = len(entries)
    visible = entries[-limit:]
    result = [task_entry] + visible if task_entry else visible
    return {
        "session_id": session_id,
        "status": meta.get("status"),
        "step_count": meta.get("step_count", 0),
        "current_activity": meta.get("current_activity", ""),
        "pending_interaction": interaction_view(pending) if pending else None,
        "final_response": final_text,
        "updated_at": meta.get("updated_at"),
        "entries": result,
        "limit": limit,
        "has_more": total > limit or window_saturated,
        "hidden_count": max(0, total - limit),
    }
