import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


# Patterns of TUI chrome, ASCII borders, spinners, and status bars that distract the Parent Agent
BOX_AND_BORDER_RE = re.compile(r"^[\s╭╮╯╰┌┐┘└├┤┬┴┼─━│┃═║╔╗╚╝╠╣╦╩╬▀▄█▌▐░▒▓·•∙⋅◦▪▫◊◆◇❖✦✧★☆⚡⚙❯›»>]+$")
BRAILLE_SPINNER_RE = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒⣾⣽⣻⢿⡿⣟⣯⣷]")
# K1/F3: the startup/busy indicators, keyed by engine. One list serves two readers: the hub's
# readiness gate (session_manager) refuses to type while one is on screen, and the screen filter
# below refuses to treat one as an answer. Read off the real captures: kimi draws its empty box
# under "⠋ Preparing Kimi Code…" and "⠋ Restoring session…"; omp shows "Connecting to MCP servers:" /
# "Still connecting:" / "⎋ Working…" while its MCP servers start; codex prints a bare "loading" line
# and "Starting MCP servers (n/m)"; agy shows "Signing in…", "⣷ Generating..." and
# "Accessing workspace:".
ENGINE_BUSY_PHRASES = {
    "kimi": ("Preparing Kimi Code", "Restoring session"),
    "omp": ("Connecting to MCP servers", "Still connecting", "Working…", "Working..."),
    "codex": ("Starting MCP servers", "esc to interrupt"),
    "claude": ("esc to interrupt", "Thinking…", "Thinking...", "Starting MCP servers"),
    "agy": ("Signing in", "Generating", "Accessing workspace:"),
}
# K1/F3: a spinner glyph at the START of a status line is a busy indicator for the engines whose
# captures show one, plus codex's bare "loading" line. A braille glyph anywhere else (a box border,
# a logo) is not: the rule is anchored to the start of the line. No box bar is allowed in front of
# the glyph: a glyph INSIDE a box row is the box's own content (kimi's composer can hold one as
# text), while every spinner in the captures starts the row.
ENGINE_BUSY_LINE_RES = {
    "kimi": re.compile(r"^\s*[\u2800-\u28FF]\s+\S"),
    "agy": re.compile(r"^\s*[\u2800-\u28FF]\s+\S"),
    "codex": re.compile(r"^\s*loading\s*$", re.IGNORECASE),
    "claude": re.compile(r"^\s*[\u2800-\u28FF✻✽✶✢]\s+\S.*(?:…|\.\.\.)"),
}
TUI_NOISE_PATTERNS = [
    re.compile(r"Welcome to Kimi Code!", re.IGNORECASE),
    re.compile(r"Send /help for help information", re.IGNORECASE),
    re.compile(r"Send /compact to compact", re.IGNORECASE),
    re.compile(r"--\s*(NORMAL|INSERT|VISUAL)\s*--", re.IGNORECASE),
    re.compile(r"ctrl\+c to exit|shift\+tab|alt\+enter|ctrl\+r", re.IGNORECASE),
    re.compile(r"^\s*╭───.*───╮\s*$"),
    re.compile(r"^\s*╰───.*───╯\s*$"),
    re.compile(r"^\s*│.*│\s*$"),
    re.compile(r"^\s*\d+%\s+context\b", re.IGNORECASE),
    re.compile(r"Trust this folder\?|Yes, I trust this folder", re.IGNORECASE),
    # F3: the hub's own banners and notes are not the agent's answer.
    re.compile(r"──\s*已恢复"),
    re.compile(r"启动文本注入"),
    re.compile(r"\[较早的输出已截断\]"),
    re.compile(r"\[Sub-Agent Bridge Info:"),
    re.compile(r"\(When done run:"),
    re.compile(r"可调用\s+get_subagent_final_response"),
    # F3: TUI chrome and status lines. The round-4 kimi crash recorded "Preparing Kimi Code…" as the
    # final response; the same screen also carried these rows, which must never be read as an answer.
    re.compile(r"^\s*MCP server\b.*(?:connecting|connected|failed|error)", re.IGNORECASE),
    re.compile(r"^\s*context:\s*\d+%", re.IGNORECASE),
    re.compile(r"^\s*Never Ask\b", re.IGNORECASE),
    re.compile(r"\bctrl\+o\b", re.IGNORECASE),
    re.compile(r"·\s*Tip:", re.IGNORECASE),
    re.compile(r"\bis ready\s*-\s*use the model\b", re.IGNORECASE),
    re.compile(r"^Standard: .*Plus/Moderato", re.IGNORECASE),
    re.compile(r"^No session yet\b", re.IGNORECASE),
    re.compile(r"^\s*[>❯›»]\s"),
]


def _compact_str(val: Any, max_len: int = 160) -> str:
    """Flattens and truncates arbitrary tool arguments/outputs into a single high-signal line."""
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        if isinstance(val, dict):
            for key in (
                "command",
                "cmd",
                "CommandLine",
                "path",
                "file_path",
                "Path",
                "pattern",
                "query",
                "message",
                "question",
                "summary",
            ):
                if key in val and isinstance(val[key], (str, int, float)):
                    prefix = f"{key}={val[key]}"
                    s = re.sub(r"\s+", " ", str(prefix)).strip()
                    return s[:max_len] + ("..." if len(s) > max_len else "")
        try:
            s = json.dumps(val, ensure_ascii=False)
        except Exception:
            s = str(val)
    else:
        s = str(val)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def _dedup_preserve_order(items: List[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        s = (item or "").strip()
        if not s:
            continue
        if not out or out[-1] != s:
            out.append(s)
    return out


def _strip_bridge_hint(text: str) -> str:
    """Removes the injected [Sub-Agent Bridge Info: ...] system hint from user task strings."""
    if not text:
        return ""
    idx = text.find("[Sub-Agent Bridge Info:")
    if idx != -1:
        text = text[:idx]
    text = re.sub(r"\s*\[Hub Session: session_id=[^\]]+\]", "", text)
    idx2 = text.find("(When done run:")
    if idx2 != -1:
        text = text[:idx2]
    return text.strip()


def _screen_engine_keys(engine: str) -> tuple:
    """F3: the engine whose K1 indicators apply to this screen; unknown means every engine's."""
    eng = (engine or "").strip().lower()
    if not eng:
        return tuple(ENGINE_BUSY_PHRASES)
    for known in ENGINE_BUSY_PHRASES:
        if eng == known or eng.startswith(known):
            return (known,)
    return tuple(ENGINE_BUSY_PHRASES)


def _screen_busy_line(line: str, engine: str) -> bool:
    """F3: True when a screen row is one of the engine's startup/busy indicators."""
    for key in _screen_engine_keys(engine):
        line_re = ENGINE_BUSY_LINE_RES.get(key)
        if line_re is not None and line_re.match(line):
            return True
        if any(phrase in line for phrase in ENGINE_BUSY_PHRASES.get(key, ())):
            return True
    return False


def sanitize_tui_screen_text(raw_text: str, max_lines: int = 20, engine: str = "") -> List[str]:
    """
    Filters out TUI box-drawing frames, spinners, welcome banners, and status bars
    from raw terminal text, returning only meaningful content lines.

    `engine` selects the K1 startup/busy indicators to drop (F3); when it is empty or unknown,
    every engine's indicators are dropped, because "no answer" is the safe reading for a fallback.
    """
    if not raw_text:
        return []
    clean_lines: List[str] = []
    for line in raw_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _screen_busy_line(s, engine):
            continue
        s = BRAILLE_SPINNER_RE.sub("", s).strip()
        if not s or BOX_AND_BORDER_RE.match(s):
            continue
        if any(pat.search(s) for pat in TUI_NOISE_PATTERNS):
            continue
        s = s.strip("│┃║╭╮╯╰─━").strip()
        if not s or len(s) < 2:
            continue
        if not clean_lines or clean_lines[-1] != s:
            clean_lines.append(s)
    return clean_lines[-max_lines:]


class BaseTranscriptSummarizer:
    """Base interface for CLI-specific transcript summarizers."""

    cli_type: str = "generic"

    def summarize(
        self,
        transcript_path: Optional[Path],
        session_meta: Dict[str, Any],
        db_tools: List[Dict[str, Any]],
        db_messages: List[Dict[str, Any]],
        raw_screen_fallback: str = "",
    ) -> Dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def build_digest_markdown(
        cli_type: str,
        status: str,
        step_count: int,
        task: str,
        recent_actions: List[str],
        key_thoughts: List[str],
        latest_conclusion: Optional[str],
        pending_question: Optional[str] = None,
    ) -> str:
        """Renders a concise, zero-TUI-noise Markdown digest tailored for the Parent Agent."""
        lines = [
            f"### [{cli_type.upper()}] Sub-Agent Transcript Summary (status=`{status}`, steps={step_count})"
        ]
        clean_task = _strip_bridge_hint(task)
        if clean_task:
            lines.append(f"- **Task**: {_compact_str(clean_task, 220)}")
        if pending_question:
            lines.append(f"- **⚠️ Blocking Question for Parent**: {pending_question}")
        dedup_thoughts = _dedup_preserve_order(key_thoughts)
        if dedup_thoughts and latest_conclusion:
            # Exclude thoughts that are identical to the final conclusion
            dedup_thoughts = [t for t in dedup_thoughts if t.strip() != latest_conclusion.strip()]
        if dedup_thoughts:
            lines.append("- **Recent Reasoning / Intermediate Updates**:")
            for th in dedup_thoughts[-3:]:
                lines.append(f"  - {_compact_str(th, 220)}")
        if recent_actions:
            lines.append("- **Key Tool Actions**:")
            for act in recent_actions[-6:]:
                lines.append(f"  - {act}")
        if latest_conclusion:
            lines.append(f"- **Latest Response / Conclusion**:\n{latest_conclusion.strip()[:1200]}")
        return "\n".join(lines)


class OMPTranscriptSummarizer(BaseTranscriptSummarizer):
    """
    Summarizer for `oh-my-pi` (`omp`, `omp-dsflash-low`, `omp-dsflash-high`).
    Parses `.runtime_mcp_configs/omp_sess_<id>/*.jsonl`.
    """

    cli_type = "omp"

    def summarize(
        self,
        transcript_path: Optional[Path],
        session_meta: Dict[str, Any],
        db_tools: List[Dict[str, Any]],
        db_messages: List[Dict[str, Any]],
        raw_screen_fallback: str = "",
    ) -> Dict[str, Any]:
        model_info = ""
        thinking_level = ""
        auto_title = ""
        user_prompts: List[str] = []
        thoughts: List[str] = []
        assistant_texts: List[str] = []
        tool_actions: List[str] = []
        latest_conclusion: Optional[str] = None

        if transcript_path and transcript_path.exists():
            try:
                for raw in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    otype = obj.get("type")
                    ctype = obj.get("customType")

                    if otype == "model_change":
                        model_info = str(obj.get("model") or "")
                    elif otype == "thinking_level_change":
                        thinking_level = str(obj.get("thinkingLevel") or "")
                    elif otype in ("title", "title_change") and obj.get("title"):
                        auto_title = str(obj.get("title"))
                    elif otype == "custom" and ctype == "tool_execution_start":
                        data = obj.get("data") or {}
                        tname = data.get("toolName", "tool")
                        targs = _compact_str(data.get("args"), 140)
                        tool_actions.append(f"`{tname}`({targs})")
                    elif otype == "message":
                        msg = obj.get("message") or {}
                        role = msg.get("role")
                        content = msg.get("content") or []
                        if role == "user" and isinstance(content, list):
                            for p in content:
                                if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                                    user_prompts.append(_strip_bridge_hint(str(p["text"])))
                        elif role == "toolResult" and tool_actions:
                            is_err = bool(msg.get("isError"))
                            out_txt = ""
                            if isinstance(content, list):
                                for p in content:
                                    if isinstance(p, dict) and p.get("text"):
                                        out_txt = _compact_str(p["text"], 90)
                                        break
                            status_tag = "ERR" if is_err else "OK"
                            if out_txt:
                                tool_actions[-1] = f"{tool_actions[-1]} -> [{status_tag}] {out_txt}"
                        elif role == "assistant" and isinstance(content, list):
                            has_tool_call = False
                            turn_texts: List[str] = []
                            for p in content:
                                if not isinstance(p, dict):
                                    continue
                                ptype = p.get("type")
                                if ptype == "thinking" and p.get("thinking"):
                                    thoughts.append(str(p["thinking"]).strip())
                                elif ptype == "text" and p.get("text"):
                                    t = str(p["text"]).strip()
                                    if t:
                                        assistant_texts.append(t)
                                        turn_texts.append(t)
                                elif ptype in ("toolCall", "tool_use"):
                                    has_tool_call = True
                            if turn_texts and not has_tool_call:
                                latest_conclusion = "\n".join(turn_texts)
            except Exception:
                pass

        if not tool_actions and db_tools:
            tool_actions = [f"`{t['tool_name']}`({_compact_str(t.get('input_summary'), 120)})" for t in db_tools]
        if not latest_conclusion:
            if session_meta.get("final_response") and session_meta.get("final_response_source") != "terminal_turn_snapshot":
                latest_conclusion = session_meta["final_response"]
            elif assistant_texts:
                latest_conclusion = assistant_texts[-1]

        thoughts = _dedup_preserve_order(thoughts)
        step_count = max(
            int(session_meta.get("step_count") or 0),
            len(tool_actions),
            len(assistant_texts),
            len(db_tools),
            1 if latest_conclusion else 0,
        )
        if not tool_actions and assistant_texts:
            tool_actions = [f"`llm_inference`({_compact_str(t, 120)})" for t in assistant_texts[-5:]]
        task = user_prompts[-1] if user_prompts else session_meta.get("last_task", "")
        label = f"omp ({model_info}:{thinking_level})" if (model_info and thinking_level) else f"omp ({model_info or session_meta.get('profile_id', 'omp')})"
        digest = self.build_digest_markdown(
            cli_type=label,
            status=session_meta.get("status", "unknown"),
            step_count=step_count,
            task=task,
            recent_actions=tool_actions,
            key_thoughts=thoughts,
            latest_conclusion=latest_conclusion,
        )
        return {
            "cli_type": "omp",
            "transcript_file": str(transcript_path) if transcript_path else None,
            "model": model_info,
            "thinking_level": thinking_level,
            "session_title": auto_title or session_meta.get("title"),
            "step_count": step_count,
            "recent_actions": tool_actions[-10:],
            "key_thoughts": [_compact_str(t, 300) for t in thoughts[-5:]],
            "latest_conclusion": latest_conclusion,
            "digest_markdown": digest,
        }


class KimiTranscriptSummarizer(BaseTranscriptSummarizer):
    """
    Summarizer for Kimi Code CLI (`kimi`, `kimi-k3.1-pro-low`).
    Parses `~/.kimi-code/sessions/<workspace>/session_<uuid>/agents/main/wire.jsonl`,
    filtering out MCP discovery/binding noise.
    """

    cli_type = "kimi"

    def summarize(
        self,
        transcript_path: Optional[Path],
        session_meta: Dict[str, Any],
        db_tools: List[Dict[str, Any]],
        db_messages: List[Dict[str, Any]],
        raw_screen_fallback: str = "",
    ) -> Dict[str, Any]:
        prompts: List[str] = []
        thoughts: List[str] = []
        assistant_texts: List[str] = []
        tool_actions: List[str] = []
        turns_ended = 0
        steps_count = 0

        if transcript_path and transcript_path.exists():
            try:
                for raw in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue

                    otype = obj.get("type")
                    if otype == "turn.prompt":
                        inp = obj.get("input") or []
                        if isinstance(inp, list):
                            for part in inp:
                                if isinstance(part, dict) and part.get("text"):
                                    prompts.append(_strip_bridge_hint(str(part["text"])))
                        elif isinstance(obj.get("data"), dict) and obj["data"].get("user_input"):
                            prompts.append(_strip_bridge_hint(str(obj["data"]["user_input"])))
                    elif obj.get("kind") == "event" and otype == "agent.turn.ended":
                        turns_ended += 1
                    elif otype == "context.append_loop_event":
                        ev = obj.get("event") or {}
                        ev_type = ev.get("type")
                        if ev_type == "step.begin":
                            steps_count += 1
                        elif ev_type == "content.part":
                            part = ev.get("part") or {}
                            if part.get("type") == "think" and part.get("think"):
                                thoughts.append(str(part["think"]).strip())
                            elif part.get("type") == "text" and part.get("text"):
                                assistant_texts.append(str(part["text"]).strip())
                        elif ev_type == "tool.call":
                            tname = ev.get("name") or "kimi_tool"
                            targs = _compact_str(ev.get("args"), 140)
                            tool_actions.append(f"`{tname}`({targs})")
                        elif ev_type == "tool.result" and tool_actions:
                            res_obj = ev.get("result") if isinstance(ev.get("result"), dict) else {}
                            out_txt = _compact_str(res_obj.get("output") or ev.get("output") or "", 90)
                            if out_txt:
                                tool_actions[-1] = f"{tool_actions[-1]} -> {out_txt}"
                    elif isinstance(obj.get("message"), dict):
                        outer = obj["message"]
                        inner = outer.get("message") if isinstance(outer.get("message"), dict) else outer
                        if inner.get("role") == "assistant":
                            for part in inner.get("content") or []:
                                if isinstance(part, dict):
                                    if part.get("type") == "think" and part.get("think"):
                                        thoughts.append(str(part["think"]).strip())
                                    elif part.get("type") == "text" and part.get("text"):
                                        assistant_texts.append(str(part["text"]).strip())
                            for tc in inner.get("toolCalls") or []:
                                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                                tname = fn.get("name") or tc.get("name") or "kimi_tool"
                                targs = _compact_str(fn.get("arguments") or tc.get("arguments"), 140)
                                tool_actions.append(f"`{tname}`({targs})")
            except Exception:
                pass

        if not tool_actions and db_tools:
            tool_actions = [f"`{t['tool_name']}`({_compact_str(t.get('input_summary'), 120)})" for t in db_tools]

        thoughts = _dedup_preserve_order(thoughts)
        assistant_texts = _dedup_preserve_order(assistant_texts)

        latest_conclusion = None
        if session_meta.get("final_response") and session_meta.get("final_response_source") != "terminal_turn_snapshot":
            latest_conclusion = session_meta["final_response"]
        elif assistant_texts:
            latest_conclusion = assistant_texts[-1]

        step_count = max(
            int(session_meta.get("step_count") or 0),
            steps_count,
            len(tool_actions),
            len(assistant_texts),
            len(db_tools),
            1 if latest_conclusion else 0,
        )
        if not tool_actions and assistant_texts:
            tool_actions = [f"`llm_inference`({_compact_str(t, 120)})" for t in assistant_texts[-5:]]
        task = prompts[-1] if prompts else session_meta.get("last_task", "")
        digest = self.build_digest_markdown(
            cli_type="kimi-code",
            status=session_meta.get("status", "unknown"),
            step_count=step_count,
            task=task,
            recent_actions=tool_actions,
            key_thoughts=thoughts,
            latest_conclusion=latest_conclusion,
        )
        return {
            "cli_type": "kimi",
            "transcript_file": str(transcript_path) if transcript_path else None,
            "turns_completed": turns_ended,
            "step_count": step_count,
            "recent_actions": tool_actions[-10:],
            "key_thoughts": [_compact_str(t, 300) for t in thoughts[-5:]],
            "latest_conclusion": latest_conclusion,
            "digest_markdown": digest,
        }


class CodexTranscriptSummarizer(BaseTranscriptSummarizer):
    """
    Summarizer for OpenAI Codex CLI (`codex`).
    Parses `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, ignoring developer/system dumps.
    """

    cli_type = "codex"

    def summarize(
        self,
        transcript_path: Optional[Path],
        session_meta: Dict[str, Any],
        db_tools: List[Dict[str, Any]],
        db_messages: List[Dict[str, Any]],
        raw_screen_fallback: str = "",
    ) -> Dict[str, Any]:
        model_info = ""
        user_prompts: List[str] = []
        thoughts: List[str] = []
        assistant_texts: List[str] = []
        tool_actions: List[str] = []
        task_complete_msg: Optional[str] = None

        if transcript_path and transcript_path.exists():
            try:
                for raw in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    otype = obj.get("type")
                    payload = obj.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    ptype = payload.get("type")

                    if otype == "response_item":
                        if ptype == "reasoning":
                            for s in payload.get("summary") or []:
                                if isinstance(s, dict) and s.get("text"):
                                    thoughts.append(str(s["text"]).strip())
                        elif ptype in ("custom_tool_call", "function_call"):
                            tname = payload.get("name") or payload.get("tool_name") or ptype
                            targs = _compact_str(payload.get("arguments") or payload.get("input"), 140)
                            tool_actions.append(f"`{tname}`({targs})")
                        elif ptype in ("custom_tool_call_output", "function_call_output") and tool_actions:
                            out_val = payload.get("output")
                            if isinstance(out_val, list):
                                for p in out_val:
                                    if isinstance(p, dict) and p.get("text"):
                                        tool_actions[-1] = f"{tool_actions[-1]} -> {_compact_str(p['text'], 90)}"
                                        break
                            elif out_val:
                                tool_actions[-1] = f"{tool_actions[-1]} -> {_compact_str(out_val, 90)}"
                        elif ptype == "message":
                            role = payload.get("role")
                            parts = payload.get("content") or []
                            if role == "user":
                                for p in parts:
                                    if isinstance(p, dict) and p.get("text") and "<environment_context>" not in str(p["text"]):
                                        user_prompts.append(_strip_bridge_hint(str(p["text"])))
                            elif role == "assistant":
                                txts = [
                                    p.get("text", "")
                                    for p in parts
                                    if isinstance(p, dict) and p.get("type") in ("output_text", "text") and p.get("text")
                                ]
                                if txts:
                                    joined = "\n".join(txts).strip()
                                    assistant_texts.append(joined)
                                    thoughts.append(joined)
                    elif otype == "event_msg":
                        if ptype == "thread_settings_applied":
                            ts = payload.get("thread_settings") or {}
                            model_info = str(ts.get("model") or "")
                        elif ptype == "agent_reasoning" and payload.get("text"):
                            thoughts.append(str(payload["text"]).strip())
                        elif ptype == "task_complete" and payload.get("last_agent_message"):
                            task_complete_msg = str(payload["last_agent_message"]).strip()
            except Exception:
                pass

        if not tool_actions and db_tools:
            tool_actions = [f"`{t['tool_name']}`({_compact_str(t.get('input_summary'), 120)})" for t in db_tools]

        thoughts = _dedup_preserve_order(thoughts)
        latest_conclusion = (
            task_complete_msg
            or (
                session_meta.get("final_response")
                if session_meta.get("final_response_source") != "terminal_turn_snapshot"
                else None
            )
            or (assistant_texts[-1] if assistant_texts else None)
        )
        step_count = max(
            int(session_meta.get("step_count") or 0),
            len(tool_actions),
            len(assistant_texts),
            len(db_tools),
            1 if latest_conclusion else 0,
        )
        if not tool_actions and assistant_texts:
            tool_actions = [f"`llm_inference`({_compact_str(t, 120)})" for t in assistant_texts[-5:]]
        task = user_prompts[-1] if user_prompts else session_meta.get("last_task", "")
        label = f"codex ({model_info})" if model_info else "codex"
        digest = self.build_digest_markdown(
            cli_type=label,
            status=session_meta.get("status", "unknown"),
            step_count=step_count,
            task=task,
            recent_actions=tool_actions,
            key_thoughts=thoughts,
            latest_conclusion=latest_conclusion,
        )
        return {
            "cli_type": "codex",
            "model": model_info,
            "transcript_file": str(transcript_path) if transcript_path else None,
            "step_count": step_count,
            "recent_actions": tool_actions[-10:],
            "key_thoughts": [_compact_str(t, 300) for t in thoughts[-5:]],
            "latest_conclusion": latest_conclusion,
            "digest_markdown": digest,
        }


class AGYTranscriptSummarizer(BaseTranscriptSummarizer):
    """
    Summarizer for Antigravity CLI (`agy`, `agy-flash`).
    Parses `~/.gemini/antigravity-cli/brain/<conv_id>/.system_generated/logs/transcript.jsonl`
    (the pre-1.5 `~/.gemini/antigravity/brain` is only a fallback in session_manager).
    """

    cli_type = "agy"

    def summarize(
        self,
        transcript_path: Optional[Path],
        session_meta: Dict[str, Any],
        db_tools: List[Dict[str, Any]],
        db_messages: List[Dict[str, Any]],
        raw_screen_fallback: str = "",
    ) -> Dict[str, Any]:
        thoughts: List[str] = []
        assistant_texts: List[str] = []
        tool_actions: List[str] = []

        if transcript_path and transcript_path.exists():
            try:
                for raw in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    if obj.get("type") == "PLANNER_RESPONSE":
                        th = (obj.get("thinking") or "").strip()
                        if th:
                            thoughts.append(th)
                        cnt = (obj.get("content") or "").strip()
                        if cnt:
                            assistant_texts.append(cnt)
                        for tc in obj.get("tool_calls") or []:
                            if isinstance(tc, dict):
                                tname = tc.get("name") or tc.get("tool_name") or "agy_tool"
                                targs = _compact_str(tc.get("args") or tc.get("arguments"), 140)
                                tool_actions.append(f"`{tname}`({targs})")
            except Exception:
                pass

        if not tool_actions and db_tools:
            tool_actions = [f"`{t['tool_name']}`({_compact_str(t.get('input_summary'), 120)})" for t in db_tools]

        thoughts = _dedup_preserve_order(thoughts)
        sanitized_fallback = None
        fallback_src = raw_screen_fallback or str(session_meta.get("final_response") or "")
        if fallback_src:
            clean_lines = sanitize_tui_screen_text(
                fallback_src, max_lines=12, engine=session_meta.get("engine") or "agy"
            )
            if clean_lines:
                sanitized_fallback = "\n".join(clean_lines)
        non_proxy_msgs = [
            m["content"] for m in db_messages if m.get("source") != "parent_approval_proxy"
        ]
        latest_conclusion = (
            (
                session_meta.get("final_response")
                if session_meta.get("final_response_source") not in ("terminal_turn_snapshot", "transcript_summarizer")
                else None
            )
            or (assistant_texts[-1] if assistant_texts else None)
            or sanitized_fallback
            or (non_proxy_msgs[-1] if non_proxy_msgs else None)
        )
        step_count = max(
            int(session_meta.get("step_count") or 0),
            len(tool_actions),
            len(assistant_texts),
            len(db_tools),
            1 if latest_conclusion else 0,
        )
        if not tool_actions and assistant_texts:
            tool_actions = [f"`llm_inference`({_compact_str(t, 120)})" for t in assistant_texts[-5:]]
        digest = self.build_digest_markdown(
            cli_type="agy",
            status=session_meta.get("status", "unknown"),
            step_count=step_count,
            task=session_meta.get("last_task", ""),
            recent_actions=tool_actions,
            key_thoughts=thoughts,
            latest_conclusion=latest_conclusion,
        )
        return {
            "cli_type": "agy",
            "transcript_file": str(transcript_path) if transcript_path else None,
            "step_count": step_count,
            "recent_actions": tool_actions[-10:],
            "key_thoughts": [_compact_str(t, 300) for t in thoughts[-5:]],
            "latest_conclusion": latest_conclusion,
            "digest_markdown": digest,
        }


class DSHTranscriptSummarizer(BaseTranscriptSummarizer):
    """
    Summarizer for DeepSeek Harness (`dsh-flash-high`, `dsh-flash-max`, `dsh-minimax-flash`, `dsh-tui`, `dsh-gui`).
    Parses `~/.config/dsh-crew/logs/<job_id>.log`.
    """

    cli_type = "dsh"

    def summarize(
        self,
        transcript_path: Optional[Path],
        session_meta: Dict[str, Any],
        db_tools: List[Dict[str, Any]],
        db_messages: List[Dict[str, Any]],
        raw_screen_fallback: str = "",
    ) -> Dict[str, Any]:
        thoughts: List[str] = []
        tool_actions: List[str] = []
        errors: List[str] = []
        max_step = session_meta.get("step_count", 0)

        if transcript_path and transcript_path.exists():
            try:
                for raw in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    kind = ev.get("kind")
                    summary = (ev.get("summary") or "").strip()
                    if kind == "step":
                        max_step = max(max_step, int(ev.get("step") or 0))
                    elif kind == "text" and summary:
                        thoughts.append(summary)
                    elif kind == "tool_call":
                        tname = ev.get("tool") or summary or "dsh_tool"
                        tool_actions.append(f"`{tname}`({_compact_str(summary, 120)})")
                    elif kind == "tool_result" and summary and tool_actions:
                        tool_actions[-1] = f"{tool_actions[-1]} -> {_compact_str(summary, 100)}"
                    elif kind == "error" and summary:
                        errors.append(summary)
            except Exception:
                pass

        if not tool_actions and db_tools:
            tool_actions = [f"`{t['tool_name']}`({_compact_str(t.get('input_summary'), 120)})" for t in db_tools]

        thoughts = _dedup_preserve_order(thoughts)
        latest_conclusion = (
            (
                session_meta.get("final_response")
                if session_meta.get("final_response_source") != "terminal_turn_snapshot"
                else None
            )
            or (thoughts[-1] if thoughts else None)
            or (db_messages[-1]["content"] if db_messages else None)
        )
        digest = self.build_digest_markdown(
            cli_type=f"dsh ({session_meta.get('profile_id', 'dsh')})",
            status=session_meta.get("status", "unknown"),
            step_count=max_step,
            task=session_meta.get("last_task", ""),
            recent_actions=tool_actions,
            key_thoughts=thoughts,
            latest_conclusion=latest_conclusion,
        )
        return {
            "cli_type": "dsh",
            "dsh_job_id": session_meta.get("dsh_job_id"),
            "dsh_session_id": session_meta.get("dsh_session_id"),
            "transcript_file": str(transcript_path) if transcript_path else None,
            "step_count": max_step,
            "recent_actions": tool_actions[-10:],
            "key_thoughts": [_compact_str(t, 300) for t in thoughts[-5:]],
            "errors": errors[-3:],
            "latest_conclusion": latest_conclusion,
            "digest_markdown": digest,
        }


class ClaudeTranscriptSummarizer(BaseTranscriptSummarizer):
    """
    Summarizer for Claude Code CLI (`claude`).
    Parses `~/.claude/projects/<project-slug>/<session-uuid>.jsonl`.
    """

    cli_type = "claude"

    def summarize(
        self,
        transcript_path: Optional[Path],
        session_meta: Dict[str, Any],
        db_tools: List[Dict[str, Any]],
        db_messages: List[Dict[str, Any]],
        raw_screen_fallback: str = "",
    ) -> Dict[str, Any]:
        model_info = str(
            session_meta.get("effective_model") or session_meta.get("resolved_model") or ""
        )
        thinking_level = str(
            session_meta.get("effective_thinking_level")
            or session_meta.get("resolved_thinking_level")
            or ""
        )
        user_prompts: List[str] = []
        thoughts: List[str] = []
        assistant_texts: List[str] = []
        tool_actions: List[str] = []
        inference_actions: List[str] = []
        latest_conclusion: Optional[str] = None
        tool_idx_by_id: Dict[str, int] = {}
        api_requests = 0

        if transcript_path and transcript_path.exists():
            try:
                for raw in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    otype = obj.get("type")
                    if otype == "api-request":
                        api_requests += 1
                        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
                        if params.get("model"):
                            model_info = str(params["model"])
                        out_cfg = (
                            params.get("output_config")
                            if isinstance(params.get("output_config"), dict)
                            else {}
                        )
                        req_effort = str(out_cfg.get("effort") or thinking_level or "")
                        if req_effort and not thinking_level:
                            thinking_level = req_effort
                        req_model = model_info or "claude"
                        inference_actions.append(
                            f"`llm_inference`(model={req_model}{', effort=' + req_effort if req_effort else ''})"
                        )
                    elif otype == "ai-title" and obj.get("aiTitle"):
                        thoughts.append(f"[Topic] {str(obj['aiTitle']).strip()}")
                    elif otype == "queue-operation":
                        op = obj.get("operation")
                        if op == "enqueue" and obj.get("content"):
                            q_txt = _strip_bridge_hint(str(obj["content"]).strip())
                            if q_txt:
                                user_prompts.append(q_txt)
                                inference_actions.append(f"`steer_enqueue`({_compact_str(q_txt, 100)})")
                                thoughts.append(f"[Steer queued] {_compact_str(q_txt, 160)}")
                        elif op == "dequeue":
                            inference_actions.append("`steer_dequeue`(processing queued parent message)")
                    elif otype == "last-prompt" and obj.get("lastPrompt"):
                        user_prompts.append(_strip_bridge_hint(str(obj["lastPrompt"])))
                    elif otype == "user":
                        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                        content = msg.get("content")
                        if isinstance(content, str) and content.strip():
                            user_prompts.append(_strip_bridge_hint(content))
                        elif isinstance(content, list):
                            for blk in content:
                                if not isinstance(blk, dict):
                                    continue
                                btype = blk.get("type")
                                if btype == "text" and blk.get("text"):
                                    txt = str(blk["text"]).strip()
                                    if not txt.startswith("<system-reminder>"):
                                        user_prompts.append(_strip_bridge_hint(txt))
                                elif btype == "tool_result":
                                    tid = str(blk.get("tool_use_id") or "")
                                    is_err = bool(blk.get("is_error"))
                                    rc = blk.get("content")
                                    out_txt = _compact_str(rc, 90)
                                    status_tag = "ERR" if is_err else "OK"
                                    idx = tool_idx_by_id.get(tid)
                                    if idx is not None and 0 <= idx < len(tool_actions) and out_txt:
                                        tool_actions[idx] = f"{tool_actions[idx]} -> [{status_tag}] {out_txt}"
                                    elif tool_actions and out_txt:
                                        tool_actions[-1] = f"{tool_actions[-1]} -> [{status_tag}] {out_txt}"
                    elif otype == "assistant":
                        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                        m_model = msg.get("model")
                        if isinstance(m_model, str) and m_model and m_model != "<synthetic>":
                            model_info = m_model
                        think_dur_ms = obj.get("thinkingDurationMs")
                        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                        out_details = (
                            usage.get("output_tokens_details")
                            if isinstance(usage.get("output_tokens_details"), dict)
                            else {}
                        )
                        think_toks = out_details.get("thinking_tokens")
                        out_toks = usage.get("output_tokens")
                        content = msg.get("content")
                        if isinstance(content, list):
                            has_tool_call = False
                            turn_texts: List[str] = []
                            for blk in content:
                                if not isinstance(blk, dict):
                                    continue
                                btype = blk.get("type")
                                if btype == "thinking":
                                    if blk.get("thinking"):
                                        thoughts.append(str(blk["thinking"]).strip())
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
                                elif btype == "text" and blk.get("text"):
                                    t = str(blk["text"]).strip()
                                    if t:
                                        assistant_texts.append(t)
                                        turn_texts.append(t)
                                elif btype == "tool_use":
                                    has_tool_call = True
                                    tname = str(blk.get("name") or "tool")
                                    targs = _compact_str(blk.get("input"), 140)
                                    tid = str(blk.get("id") or "")
                                    tool_idx_by_id[tid] = len(tool_actions)
                                    tool_actions.append(f"`{tname}`({targs})")
                            if turn_texts and not has_tool_call:
                                latest_conclusion = "\n".join(turn_texts)
                    elif otype == "system" and obj.get("subtype") == "turn_duration":
                        dur_ms = obj.get("durationMs")
                        if dur_ms is not None:
                            inference_actions.append(
                                f"`turn_completed`(duration={float(dur_ms) / 1000.0:.1f}s)"
                            )
            except Exception:
                pass

        combined_actions = list(tool_actions)
        if not combined_actions:
            combined_actions = list(inference_actions)
        elif inference_actions:
            combined_actions = list(tool_actions) + [
                a for a in inference_actions if a.startswith(("`steer_", "`turn_completed`"))
            ]
        if not combined_actions and db_tools:
            combined_actions = [
                f"`{t['tool_name']}`({_compact_str(t.get('input_summary'), 120)})" for t in db_tools
            ]

        for m in db_messages:
            mc = str(m.get("content") or "").strip()
            if mc and mc not in assistant_texts:
                thoughts.append(mc)

        if assistant_texts:
            latest_txt = assistant_texts[-1]
            headings = [
                ln.strip()
                for ln in latest_txt.splitlines()
                if ln.strip().startswith("#") and len(ln.strip()) <= 120
            ][:5]
            if headings:
                thoughts.append("[Section outline] " + " | ".join(headings))
            elif len(thoughts) <= 1:
                first_line = next((ln.strip() for ln in latest_txt.splitlines() if ln.strip()), "")
                if first_line:
                    thoughts.append(f"[Summary] {_compact_str(first_line, 200)}")

        if session_meta.get("status") in ("starting", "running") and raw_screen_fallback:
            clean_scr = sanitize_tui_screen_text(raw_screen_fallback, max_lines=6, engine="claude")
            if clean_scr:
                thoughts.append("[Live screen] " + " / ".join(clean_scr[-3:]))

        if not latest_conclusion:
            if session_meta.get("final_response") and session_meta.get("final_response_source") != "terminal_turn_snapshot":
                latest_conclusion = session_meta["final_response"]
            elif assistant_texts:
                latest_conclusion = assistant_texts[-1]

        thoughts = _dedup_preserve_order(thoughts)
        step_count = max(
            int(session_meta.get("step_count") or 0),
            len(tool_actions) + api_requests,
            len(assistant_texts),
            len(db_tools),
            1 if latest_conclusion else 0,
        )
        task = user_prompts[-1] if user_prompts else session_meta.get("last_task", "")
        label = (
            f"claude ({model_info}:{thinking_level})"
            if (model_info and thinking_level)
            else f"claude ({model_info or session_meta.get('profile_id', 'claude')})"
        )
        digest = self.build_digest_markdown(
            cli_type=label,
            status=session_meta.get("status", "unknown"),
            step_count=step_count,
            task=task,
            recent_actions=combined_actions,
            key_thoughts=thoughts,
            latest_conclusion=latest_conclusion,
        )
        return {
            "cli_type": "claude",
            "transcript_file": str(transcript_path) if transcript_path else None,
            "model": model_info,
            "thinking_level": thinking_level,
            "step_count": step_count,
            "recent_actions": combined_actions[-10:],
            "key_thoughts": [_compact_str(t, 300) for t in thoughts[-5:]],
            "latest_conclusion": latest_conclusion,
            "digest_markdown": digest,
        }


class GenericTelemetrySummarizer(BaseTranscriptSummarizer):
    """Fallback summarizer that synthesizes structured telemetry + sanitized non-chrome output."""

    cli_type = "generic"

    def summarize(
        self,
        transcript_path: Optional[Path],
        session_meta: Dict[str, Any],
        db_tools: List[Dict[str, Any]],
        db_messages: List[Dict[str, Any]],
        raw_screen_fallback: str = "",
    ) -> Dict[str, Any]:
        tool_actions = [f"`{t['tool_name']}`({_compact_str(t.get('input_summary'), 120)})" for t in db_tools]
        thoughts = _dedup_preserve_order([m["content"] for m in db_messages])
        latest_conclusion = (
            session_meta.get("final_response")
            if session_meta.get("final_response_source") != "terminal_turn_snapshot"
            else None
        )
        if not latest_conclusion and thoughts:
            latest_conclusion = thoughts[-1]
        if not latest_conclusion and raw_screen_fallback:
            clean_lines = sanitize_tui_screen_text(
                raw_screen_fallback, max_lines=8, engine=session_meta.get("engine") or ""
            )
            if clean_lines:
                latest_conclusion = "\n".join(clean_lines)

        step_count = max(session_meta.get("step_count", 0), len(tool_actions))
        digest = self.build_digest_markdown(
            cli_type=session_meta.get("profile_id", "generic"),
            status=session_meta.get("status", "unknown"),
            step_count=step_count,
            task=session_meta.get("last_task", ""),
            recent_actions=tool_actions,
            key_thoughts=thoughts,
            latest_conclusion=latest_conclusion,
        )
        return {
            "cli_type": session_meta.get("profile_id", "generic"),
            "transcript_file": str(transcript_path) if transcript_path else None,
            "step_count": step_count,
            "recent_actions": tool_actions[-10:],
            "key_thoughts": [_compact_str(t, 300) for t in thoughts[-5:]],
            "latest_conclusion": latest_conclusion,
            "digest_markdown": digest,
        }


class TranscriptSummarizerRegistry:
    """Dispatches to the appropriate CLI-specific transcript summarizer based on profile_id."""

    def __init__(self):
        self._omp = OMPTranscriptSummarizer()
        self._kimi = KimiTranscriptSummarizer()
        self._codex = CodexTranscriptSummarizer()
        self._claude = ClaudeTranscriptSummarizer()
        self._agy = AGYTranscriptSummarizer()
        self._dsh = DSHTranscriptSummarizer()
        self._generic = GenericTelemetrySummarizer()

    def get_summarizer(self, profile_id: str, engine: str = "") -> BaseTranscriptSummarizer:
        pid = (profile_id or "").lower()
        eng = (engine or "").lower()
        if pid.startswith("omp") or eng == "omp":
            return self._omp
        if pid.startswith("kimi") or eng == "kimi":
            return self._kimi
        if pid.startswith("codex") or eng == "codex":
            return self._codex
        if pid.startswith("claude") or eng == "claude":
            return self._claude
        if pid.startswith("agy") or eng == "agy":
            return self._agy
        if pid.startswith("dsh") or eng == "dsh":
            return self._dsh
        return self._generic

    def summarize_session(
        self,
        profile_id: str,
        transcript_path: Optional[Path],
        session_meta: Dict[str, Any],
        db_tools: List[Dict[str, Any]],
        db_messages: List[Dict[str, Any]],
        raw_screen_fallback: str = "",
    ) -> Dict[str, Any]:
        summarizer = self.get_summarizer(profile_id, str(session_meta.get("engine") or ""))
        if isinstance(summarizer, (GenericTelemetrySummarizer, AGYTranscriptSummarizer)):
            res = summarizer.summarize(transcript_path, session_meta, db_tools, db_messages, raw_screen_fallback)
        else:
            res = summarizer.summarize(transcript_path, session_meta, db_tools, db_messages)
            if not res.get("latest_conclusion") and db_messages:
                res["latest_conclusion"] = db_messages[-1]["content"]
            if not res.get("latest_conclusion") and raw_screen_fallback:
                clean_lines = sanitize_tui_screen_text(
                    raw_screen_fallback,
                    max_lines=10,
                    engine=session_meta.get("engine") or profile_id or "",
                )
                if clean_lines:
                    res["latest_conclusion"] = "\n".join(clean_lines)
        return res
