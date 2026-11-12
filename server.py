import asyncio
import hmac
import json
import logging
import math
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from contextlib import AsyncExitStack, asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
import fastmcp
from fastmcp import Context, FastMCP
from fastmcp.utilities.logging import configure_logging as configure_fastmcp_logging
import uvicorn

from session_manager import (
    HubSessionManager,
    Win32WindowHelper,
    default_profiles_path,
    hub_api_base_url,
    hub_port,
    strip_osc_terminal_replies,
    resolve_session_approval_fields,
)
from transcript_summarizer import sanitize_tui_screen_text
from hub_persistence import PersistenceUnavailable
from agent_guidance import SERVER_INSTRUCTIONS
import windows_accept_guard

BASE_DIR = Path(__file__).parent.resolve()
PROFILES_PATH = default_profiles_path(BASE_DIR)
STATIC_DIR = BASE_DIR / "static"
STATIC_INDEX = STATIC_DIR / "index.html"

DATA_DIR = Path(os.environ.get("HUB_DATA_DIR") or BASE_DIR).resolve()
hub = HubSessionManager(PROFILES_PATH, base_dir=BASE_DIR, data_dir=DATA_DIR)


# B11: discovery shells out to five CLIs and talks to the DSH host over HTTP, so it never runs on
# the event loop. Concurrent refreshes share one in-flight task, and a route that only needs the
# current data answers from the cache at once.
_refresh_task: Optional[asyncio.Task] = None
_refresh_lock: Optional[asyncio.Lock] = None


def _get_refresh_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


def _load_profiles_from_cache_blocking() -> None:
    """Loads profiles off the event loop, using only the discovery cache (R3).

    If the cache is still empty the startup discovery thread is running: wait for it in this worker
    thread, never on the loop. `load_profiles(discovery=None)` would run discovery synchronously, which
    is exactly what made the first poll crawl.
    """
    discovery = hub.model_discovery.peek_cache()
    if discovery is None:
        hub.wait_for_discovery(180.0)
        discovery = hub.model_discovery.peek_cache()
        if discovery is None:
            # Still discovering; keep the state the last load produced instead of wiping it.
            return
    hub.load_profiles(refresh_discovery=False, discovery=discovery)


async def ensure_profiles_loaded(refresh_discovery: bool = False) -> None:
    """Refresh profiles in a worker thread (shared in-flight task per refresh)."""
    global _refresh_task
    if not refresh_discovery:
        await asyncio.to_thread(_load_profiles_from_cache_blocking)
        return

    async with _get_refresh_lock():
        task = _refresh_task
        if task is None or task.done():
            task = asyncio.create_task(asyncio.to_thread(hub.load_profiles, True))
            _refresh_task = task
    try:
        await asyncio.shield(task)
    except Exception:
        # A failed refresh must not take the route down; the cached catalog still answers.
        pass
    if not hub.engines_catalog:
        await asyncio.to_thread(_load_profiles_from_cache_blocking)


async def profiles_payload() -> Dict[str, Any]:
    return {
        "profiles": hub.profiles,
        "engines_catalog": hub.engines_catalog,
        "global_routing_instructions": hub.global_routing_instructions,
        "subagent_idle_timeout_minutes": hub.subagent_idle_timeout_minutes,
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
        "default_cwd": hub.hub_default_cwd(),
        "hub_port": hub_port(),
    }


def bad_request(detail: str):
    return JSONResponse({"detail": detail}, status_code=400)


def not_found(detail: str):
    return JSONResponse({"detail": detail}, status_code=404)


def _session_can_receive(session) -> bool:
    """A live process, or a warm DSH job that its continue API can still take."""
    if session.is_process_alive():
        return True
    return session.status == "completed" and str(session.engine or "").lower() == "dsh"


INJECTION_PENDING_HINT = (
    "Text will be submitted once the TUI is ready and any earlier input has finished. "
    "Submission is still pending; inspect last_injection with get_subagent_status_and_trace."
)


def _sanitize_json_floats(value: Any) -> Any:
    """Replaces non-finite floats (NaN, Infinity, -Infinity) with None for strict RFC 8259 JSON compliance."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _sanitize_json_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_floats(v) for v in value]
    if isinstance(value, tuple):
        return [_sanitize_json_floats(v) for v in value]
    return value


def _injection_report(session, pending: bool = False, submit_id: Optional[str] = None) -> Dict[str, Any]:
    """W11: spawn/resume/send report the real injection, or say honestly that it is still pending.

    MCP callers receive the current injection record immediately. A TUI that is not ready leaves
    the record pending; HTTP poll reconciles the eventual outcome. The CALLER knows whether text
    was submitted in this call and whether it remains pending, so this helper must not guess it.
    K8 (fix 9d): with any record, the session's recent submit records (startup text, live messages,
    newest last) come along as recent_injections, so an earlier outcome stays visible.
    """
    injection = getattr(session, "last_injection", None)
    recent = list(getattr(session, "recent_injections", None) or [])
    if submit_id is not None:
        injection = next((item for item in reversed(recent) if item.get("submit_id") == submit_id), None)
        if not submit_id:
            return {"injection": "none", "last_injection": None}
    history = {"recent_injections": recent} if recent else {}
    if isinstance(injection, dict) and injection.get("pending"):
        # K6 (fix 9c): a live message is still being typed and proved in the background; the
        # record says so ("submitting") and its real outcome replaces it.
        return {"injection": "pending", "submit_id": injection.get("submit_id"), "last_injection": injection,
                "injection_hint": INJECTION_PENDING_HINT, **history}
    if isinstance(injection, dict) and injection:
        return {"injection": "recorded", "submit_id": injection.get("submit_id"), "last_injection": injection, **history}
    if pending:
        return {
            "injection": "pending",
            "last_injection": None,
            "injection_hint": INJECTION_PENDING_HINT,
        }
    return {"injection": "none", "last_injection": None}


HUB_VERSION = "4.1.7"


async def _wait_contract(session_ids, *, turn_ids=None, existing_handle=None, mode="all_settled"):
    """Create or reuse the opaque parent wait handle for one session set."""
    if existing_handle:
        handle = existing_handle
    else:
        info = await hub.wait_inbox.ensure_for_sessions(session_ids, turn_ids, mode=mode)
        handle = info["wait_handle"]
    for sid in session_ids:
        session = hub.sessions.get(sid)
        if session is not None:
            session.wait_handle = handle
    return {
        "wait_handle": handle,
        "wait_url": f"{hub_api_base_url()}/api/wait/{handle}",
    }


def _control_status(session, *, accepted=True):
    return {
        "accepted": bool(accepted),
        "session_id": session.session_id,
        "turn_id": session.turn_id,
        "wait_handle": session.wait_handle,
        "wait_url": f"{hub_api_base_url()}/api/wait/{session.wait_handle}" if session.wait_handle else None,
        "status": session.status,
        "approval": {
            "approval_mode": resolve_session_approval_fields(session.profile_snapshot)["approval_mode"],
            "high_risk_approval": session.high_risk_approval,
            "approval_source": (session.profile_snapshot or {}).get("approval_source"),
        },
    }

# ============================================================================
# 1. 北向 MCP Server (/mcp) —— 暴露给【主 Agent / Parent Agent】的调度、遥测与双向通信接口
# ============================================================================
mcp = FastMCP(
    "SubAgent-NonHeadless-Hub",
    version=HUB_VERSION,
    instructions=SERVER_INSTRUCTIONS,
)


@mcp.tool()
async def list_agent_profiles(refresh_discovery: bool = False) -> Dict[str, Any]:
    """
    List enabled profiles, engines, models and thinking levels.
    Read routing_guide_markdown and each profile's when_to_use before choosing an agent.
    Profiles include defaults; engines_catalog excludes disabled profiles and models.
    Set refresh_discovery=true to refresh the installed engine catalog.
    """
    await ensure_profiles_loaded(refresh_discovery=refresh_discovery)
    base_url = hub_api_base_url()
    return {
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
        "global_routing_instructions": hub.global_routing_instructions,
        "hub_console_url": f"{base_url}/",
        "engines_catalog": hub.get_mcp_engines_catalog(),
        "profiles": hub.get_mcp_enabled_profiles(),
    }


@mcp.tool()
async def discover_agent_models_and_thinking_levels(refresh: bool = False) -> Dict[str, Any]:
    """
    Discover enabled models and thinking levels for installed engines.
    Pass a listed model and thinking_level to spawn_subagent; follow routing_guide_markdown.
    Set refresh=true to refresh discovery rather than use cached results.
    """
    await ensure_profiles_loaded(refresh_discovery=refresh)
    return {
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
        "engines": hub.get_mcp_engines_catalog(),
        "usage_hint": "Pass any enabled `model` (from `available_models`) and `thinking_level` directly to `spawn_subagent(agent_type, task, model=..., thinking_level=...)`. Follow `routing_guide_markdown` first; use a profile's `when_to_use` only as a supplemental hint for new or not-yet-benchmarked routes.",
    }


@mcp.tool()
async def spawn_subagent(
    agent_type: str,
    task: Optional[str] = None,
    model: Optional[str] = None,
    thinking_level: Optional[str] = None,
    cwd: Optional[str] = None,
    title: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    bypass_approvals: Optional[bool] = None,
    extra_args: Optional[List[str]] = None,
    mcp_servers: Optional[Dict[str, Any]] = None,
    disabled_mcp_servers: Optional[List[str]] = None,
    strict_mcp_config: bool = False,
    auto_approval_rules: Optional[List[Dict[str, Any]]] = None,
    wait_seconds: float = 5.0,  # deprecated compatibility field; never blocks
    approval_mode: Optional[str] = None,
    compaction_threshold: Optional[Union[int, str]] = None,
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """Start a subagent and return an opaque wait handle immediately.

    ``approval_mode`` decides who approves the worker's risky actions:
    - omitted (the default): the engine's own auto-approval reviewer where it has one (Codex
      ``--approve-for-me``, Claude auto mode); only what the reviewer escalates comes to you. Other
      engines run ``"ask"``. Leave it omitted, even to be explicit or careful: auto is the safe choice.
    - ``"ask"``: high-risk actions stop and come to you (respond_to_subagent); only when you or the
      user want to approve every one yourselves.
    - ``"bypass"``: no checks at all; only when the user asked for it or the task needs it.
    ``bypass_approvals`` is the legacy switch (true = ``"bypass"``, false = the default); never pass
    both. A profile with ``bypass_approvals`` on defaults to ``"bypass"``.

    For every engine, create the task's own worktree, scratch, logs and results under ``cwd``:
    sandboxes and auto-accept cover only ``cwd`` (Codex workspace-write, Claude acceptEdits in ask
    mode, DSH workspace root), so work outside it turns into approvals (the engine reviewer's in
    auto mode, yours in ask mode and for DSH); for DSH every call, reads included, and the hub
    cannot add roots. Only what the task creates belongs there: leave files it did not create
    (sources, shared or others' data, credentials) in place, and never widen ``cwd`` to a parent,
    home or drive root to cover them.
    ``compaction_threshold`` sets this session's auto-compaction point: ``"70%"`` of the context
    window or a token count (``200000``, ``"200k"``). omp takes both, claude only a percent (it can
    only compact earlier than its default), codex only tokens; other engines reject it.
    ``wait_seconds`` is deprecated and ignored for waiting; use ``wait_url``.
    """
    await ensure_profiles_loaded()
    session = await hub.create_session(
        profile_id=agent_type,
        launch_source="agent",
        bypass_approvals=bypass_approvals,
        approval_mode=approval_mode,
        compaction_threshold=compaction_threshold,
        extra_args=extra_args,
        mcp_servers=mcp_servers,
        disabled_mcp_servers=disabled_mcp_servers,
        strict_mcp_config=strict_mcp_config,
        auto_approval_rules=auto_approval_rules,
        task=task,
        cwd=cwd,
        title=title,
        model=model,
        thinking_level=thinking_level,
        env=env,
    )
    wait = await _wait_contract([session.session_id], turn_ids={session.session_id: session.turn_id})
    session.wait_handle = wait["wait_handle"]
    await hub.persist_session(session)
    return {**_control_status(session), **wait, "title": session.title, "agent_type": session.profile_id,
            "engine": session.engine, "effective_model": session.effective_model,
            "effective_thinking_level": session.effective_thinking_level,
            "delivery": {"accepted": True, "state": "queued"}}


@mcp.tool()
async def spawn_subagents(specs: List[Dict[str, Any]], all_settled: bool = True) -> Dict[str, Any]:
    """Start several subagents and return one group wait handle (all_settled by default).

    Each spec takes spawn_subagent's fields; approval_mode follows the same default (auto)."""
    if not specs:
        raise ValueError("specs must contain at least one subagent")
    sessions = []
    for spec in specs:
        item = dict(spec)
        agent_type = item.pop("agent_type", item.pop("profile_id", None))
        if not agent_type:
            raise ValueError("each spec requires agent_type")
        sessions.append(await hub.create_session(profile_id=agent_type, launch_source="agent", **item))
    wait = await _wait_contract([s.session_id for s in sessions],
                                turn_ids={s.session_id: s.turn_id for s in sessions},
                                mode="all_settled" if all_settled else "any_settled")
    for session in sessions:
        session.wait_handle = wait["wait_handle"]
        await hub.persist_session(session)
    return {
        "accepted": True,
        "mode": "all_settled" if all_settled else "any_settled",
        "sessions": [{"session_id": s.session_id, "turn_id": s.turn_id, "status": s.status} for s in sessions],
        **wait,
    }


@mcp.tool()
async def set_subagent_approval_rules(
    session_id: str,
    auto_approval_rules: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Store bounded, session-scoped automatic approval rules and return immediately.

    A rule answers approve_once only for an approval request that reports a structured argv
    matching ``match.command_argv`` inside the session cwd, without shell composition, network,
    write, delete, credential, upload, process-control or escalation. No engine reports a
    structured argv yet (DSH approvals are free text), so stored rules currently match nothing
    and ``rules_applied`` confirms storage only. For approvals outside cwd, fix the cwd layout.

    Rule shape: {"id": "read-logs", "match": {"tool": ["Bash"], "command_argv": [["cat", "logs/*.log"]]},
    "max_uses": 20, "expires_in_seconds": 1800}; argv tokens are fnmatch patterns and the optional
    match.path_glob also requires a request path. effect must be approve_once, match.cwd_scope
    workspace and every constraint false; other values are rejected.
    Rules never enable the engine-wide bypass switch and expire with the session or their TTL.
    """
    result = await hub.set_auto_approval_rules(session_id, auto_approval_rules)
    session = hub.sessions.get(session_id)
    response = {
        "accepted": True,
        "session_id": session_id,
        "rules_applied": result.get("rules_applied", True),
    }
    if session is not None:
        response.update(_control_status(session))
    return response


@mcp.tool()
async def get_subagent_status_and_trace(
    session_id: str,
    recent_tools_limit: int = 15,
    recent_messages_limit: int = 10,
) -> Dict[str, Any]:
    """Read a compact status snapshot when diagnosis is explicitly needed."""
    session = hub.sessions.get(session_id)
    if session is None:
        result = await hub.get_subagent_status_and_trace(session_id=session_id, recent_tools_limit=0, recent_messages_limit=0)
        return {key: result.get(key) for key in ("session_id", "status", "current_activity", "pending_interaction", "final_response")}
    return {
        "session_id": session.session_id,
        "turn_id": session.turn_id,
        "status": session.status,
        "current_activity": session.current_activity,
        "pending_interaction": session.pending_interaction,
    }


@mcp.tool()
async def get_subagent_final_response(session_id: str) -> Dict[str, Any]:
    """Compatibility read for callers that did not use the wait URL."""
    return await hub.get_subagent_final_response(session_id=session_id)


@mcp.tool()
async def send_to_subagent(
    session_id: str,
    text: str,
    press_enter: bool = True,
    wait_seconds: float = 10.0,  # deprecated compatibility field; never blocks
    ctx: Optional[Context] = None,
    clear_input: bool = False,
) -> Dict[str, Any]:
    """Deliver a message immediately and return the session's existing opaque wait handle.

    ``wait_seconds`` is deprecated and ignored for waiting; use ``wait_url``.
    """
    await ensure_profiles_loaded()
    if not text.strip() and not clear_input:
        session = hub.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session '{session_id}' not found.")
        return {**_control_status(session, accepted=False), "delivery": {"delivered": False, "reason": "empty_text"}}
    start_seq = hub.global_seq
    session = hub.sessions.get(session_id)
    resumed = False
    if session is None or not _session_can_receive(session):
        if clear_input:
            raise ValueError("clear_input requires a live Codex TUI session")
        # Archive spec §5: an ended or archived session is revived automatically, then the message
        # is delivered the way an initial task would be. A non-resumable record raises its Chinese
        # reason instead of a silent `pty_written:false`.
        # A2: the resume reports whether IT really resumed and whether the text was delivered; a
        # caller that lost the race gets its text delivered to the now-live session instead of a
        # canned `delivered: true`.
        result = await hub.resume_subagent(session_id, text=text)
        session = result["session"]
        resumed = bool(result.get("resumed"))
        delivery = result.get("delivery") or {
            "delivered": bool(result.get("delivered")),
            "session_id": session_id,
            "resumed": resumed,
            "resume_count": session.resume_count,
            "pty_written": False,
            "injection": result.get("injection") or "none",
        }
        if result.get("already_live"):
            if not session.last_task:
                session.last_task = text
            session.current_activity = f"Steering message from parent: {text[:80]}"
    else:
        # B9: last_task is the initial task only; a steering message is activity, not history.
        if not session.last_task:
            session.last_task = text
        session.current_activity = f"Steering message from parent: {text[:80]}"
        delivery = await hub.deliver_parent_message(
            session_id=session_id,
            text=text,
            also_write_pty=True,
            press_enter=press_enter,
            clear_input=clear_input,
        )

    if not session.wait_handle:
        wait = await _wait_contract([session.session_id], turn_ids={session.session_id: session.turn_id})
        session.wait_handle = wait["wait_handle"]
    else:
        # A follow-up message starts a fresh turn on the same session.  Re-arm the
        # durable handle so a previous done result cannot be returned immediately.
        await hub.wait_inbox.update_turn(session.wait_handle, session.session_id, session.turn_id)
        wait = {"wait_handle": session.wait_handle, "wait_url": f"{hub_api_base_url()}/api/wait/{session.wait_handle}"}
    return {**_control_status(session), **wait, "resumed": resumed, "delivery": delivery}


@mcp.tool()
async def resume_subagent(
    session_id: str,
    text: Optional[str] = None,
    auto_approval_rules: Optional[List[Dict[str, Any]]] = None,
    wait_seconds: float = 10.0,  # deprecated compatibility field; never blocks
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """Resume a native conversation and return a new turn id plus its existing wait handle.

    ``wait_seconds`` is deprecated and ignored for waiting; use ``wait_url``.
    """
    await ensure_profiles_loaded()
    result = await hub.resume_subagent(
        session_id,
        text=text,
        auto_approval_rules=auto_approval_rules,
    )
    session = result["session"]
    delivery = result.get("delivery") or {
        "delivered": bool(result.get("delivered")),
        "resumed": bool(result.get("resumed")),
        "already_live": bool(result.get("already_live")),
        "resume_count": session.resume_count,
        "injection": result.get("injection") or "none",
    }

    if not session.wait_handle:
        wait = await _wait_contract([session.session_id], turn_ids={session.session_id: session.turn_id})
        session.wait_handle = wait["wait_handle"]
    else:
        await hub.wait_inbox.update_turn(session.wait_handle, session.session_id, session.turn_id)
        wait = {"wait_handle": session.wait_handle, "wait_url": f"{hub_api_base_url()}/api/wait/{session.wait_handle}"}
    return {**_control_status(session), **wait, "resumed": bool(result.get("resumed")),
            "already_live": bool(result.get("already_live")), "delivery": delivery}


@mcp.tool()
async def list_archived_sessions(query: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """
    List archived sessions, newest first; filter by session ID, task, working directory or profile.
    Entries include model, thinking level, end reason/time, resumable and resume_block_reason.
    Restore a resumable entry with resume_subagent.
    """
    payload = await hub.list_archive(limit=limit, offset=0, query=query or "")
    sessions = [
        {
            "session_id": rec.get("session_id"),
            "profile": rec.get("profile_name") or rec.get("profile_id"),
            "profile_id": rec.get("profile_id"),
            "model": rec.get("model_line") or "",
            "cwd": rec.get("cwd"),
            "task": rec.get("task_first_line") or "",
            "end_reason": rec.get("end_reason"),
            "end_reason_label": rec.get("end_reason_label"),
            "ended_at": rec.get("ended_at"),
            "resumable": bool(rec.get("resumable")),
            "resume_block_reason": rec.get("resume_block_reason") or "",
        }
        for rec in payload.get("sessions") or []
    ]
    return {"total": payload.get("total", len(sessions)), "sessions": sessions}


@mcp.tool()
async def respond_to_subagent(
    session_id: str,
    answer: Optional[str] = None,
    decision: Optional[str] = None,
    option_index: Optional[int] = None,
    custom_input: Optional[str] = None,
    request_id: Optional[str] = None,
    wait_seconds: float = 8.0,  # deprecated compatibility field; never blocks
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """Answer a question/approval once, or deliver ordinary feedback, by request_id.

    ``wait_seconds`` is deprecated and ignored for waiting; use ``wait_url``.
    """
    session = hub.sessions.get(session_id)
    if not session:
        raise ValueError(f"Session '{session_id}' not found in active sessions.")
    if answer is not None and decision is None and option_index is None and custom_input is None:
        action_result = await hub.deliver_parent_message(
            session_id=session_id, text=answer, request_id=request_id, also_write_pty=True,
        )
    else:
        action_result = await hub.respond_to_interaction(
            session_id=session_id, decision=decision, option_index=option_index,
            custom_input=custom_input if custom_input is not None else answer,
            request_id=request_id,
        )
        if request_id and session.wait_handle:
            await hub.wait_inbox.resolve_request(session.wait_handle, request_id)
    if not session.wait_handle:
        wait = await _wait_contract([session_id], turn_ids={session_id: session.turn_id})
        session.wait_handle = wait["wait_handle"]
    else:
        wait = {"wait_handle": session.wait_handle, "wait_url": f"{hub_api_base_url()}/api/wait/{session.wait_handle}"}
    return {**_control_status(session, accepted=bool(action_result.get("ok", True))), **wait,
            "response": {key: action_result.get(key) for key in ("delivered", "response_state", "resolved_request_id", "idempotent") if key in action_result}}


@mcp.tool()
async def summarize_subagent_transcript(session_id: str) -> Dict[str, Any]:
    """
    Summarize native records as key_thoughts, recent_actions, latest_conclusion and digest_markdown.
    Terminal borders, spinners and status bars are filtered. A summary alone does not prove completion.
    """
    return await hub.summarize_transcript(session_id=session_id)


@mcp.tool()
async def read_subagent_screen(
    session_id: str,
    max_lines: int = 80,
    include_raw_tui: bool = False,
) -> Dict[str, Any]:
    """
    Read status, a transcript summary and sanitized terminal output.
    Set include_raw_tui=true only when raw terminal characters are needed for diagnosis.
    """
    transcript_summary = await hub.summarize_transcript(session_id=session_id)
    session = hub.sessions.get(session_id)
    if session:
        raw_txt = session.get_clean_screen_text(max_lines=max_lines)
        res: Dict[str, Any] = {
            "session_id": session.session_id,
            "profile_id": session.profile_id,
            "status": session.status,
            "step_count": max(session.step_count, transcript_summary.get("step_count", 0)),
            "current_activity": session.current_activity,
            "pending_question_id": session.pending_question_id,
            "final_response": session.final_response,
            "recent_events": hub.get_worker_events(session_id=session_id, after_seq=0)[-10:],
            "transcript_summary": transcript_summary,
            "sanitized_screen_lines": sanitize_tui_screen_text(raw_txt, max_lines=25, engine=session.engine),
            # K6: the noisy line filter strips the composer, so a parent could not see text that was
            # typed but never sent. Report that one row separately; the sanitising stays as it was.
            "input_line": HubSessionManager._tui_input_line(raw_txt, session.engine),
            # K1: whether the engine is still starting up, so "the text is in the box and Enter did
            # nothing" can be read as the known boot race instead of a broken session.
            "busy_indicator": HubSessionManager._tui_busy_indicator(raw_txt, session.engine),
        }
        if include_raw_tui:
            res["raw_screen_text"] = raw_txt
        return res

    persisted = await hub.db.get_session_record(session_id)
    if not persisted:
        raise ValueError(f"Session '{session_id}' not found.")
    # Archive spec §5: an archived session's screen is the last_screen the final record kept.
    last_screen = persisted.get("last_screen") or ""
    if last_screen:
        raw_txt = last_screen
        sanitized_lines = last_screen.splitlines()[-25:]
    else:
        raw_txt = "\n".join((persisted.get("scrollback_text") or "").splitlines()[-max_lines:])
        sanitized_lines = sanitize_tui_screen_text(
            raw_txt, max_lines=25, engine=persisted.get("engine") or ""
        )
    res = {
        "session_id": session_id,
        "profile_id": persisted.get("profile_id"),
        "status": persisted.get("status"),
        "end_reason": persisted.get("end_reason"),
        "resumable": bool(persisted.get("resumable")),
        "resume_block_reason": persisted.get("resume_block_reason") or "",
        "step_count": max(persisted.get("step_count", 0), transcript_summary.get("step_count", 0)),
        "current_activity": persisted.get("current_activity", ""),
        "final_response": persisted.get("final_response") or transcript_summary.get("latest_conclusion"),
        "final_response_source": persisted.get("final_response_source"),
        "transcript_summary": transcript_summary,
        "sanitized_screen_lines": sanitized_lines,
        # K6: an ended record has no live composer to report; the field stays null so a caller can
        # read it the same way on both paths, and the archived final answer is named as its source.
        "input_line": None,
    }
    if include_raw_tui:
        res["raw_screen_text"] = raw_txt
    return res


@mcp.tool()
async def list_active_sessions(include_historical: bool = True, limit: int = 25) -> Dict[str, Any]:
    """List live console sessions and persisted historical sessions."""
    active = [s.to_dict() for s in hub.sessions.values()]
    result: Dict[str, Any] = {
        "global_seq": hub.global_seq,
        "active_count": len(active),
        "active_sessions": active,
    }
    if include_historical:
        # Spec §6: list_active_sessions stays "live + ended-in-memory"; archived records are listed
        # by list_archived_sessions.
        result["persisted_sessions"] = await hub.db.list_persisted_sessions(limit=limit, include_archived=False, consistent=False)
    return result


@mcp.tool()
async def terminate_subagent(session_id: str) -> Dict[str, Any]:
    """
    Stop a subagent process and queue its answer, terminal history and tool trace for persistence.
    Results remain readable with get_subagent_final_response or get_subagent_status_and_trace.
    """
    session = hub.sessions.get(session_id)
    if not session:
        persisted = await hub.db.get_session_record(session_id)
        if persisted:
            return {
                "terminated": True,
                "session_id": session_id,
                "note": "Session was already closed; its final_response remains available in SQLite.",
                "final_response": persisted.get("final_response"),
            }
        raise ValueError(f"Session '{session_id}' not found.")

    # K5: the parent agent ended it, so the lifecycle event says 主 Agent, not 用户.
    result = await hub.close_session(session_id, source="mcp")
    final_info = await hub.get_subagent_final_response(session_id)
    return {
        "terminated": True,
        "session_id": session_id,
        "was_alive": result.get("was_alive"),
        "status": result.get("status"),
        "persisted_in_sqlite": hub.db.status()["pending_writes"] == 0,
        "final_response": final_info.get("final_response"),
        "final_response_source": final_info.get("final_response_source"),
        "step_count": final_info.get("step_count"),
    }


@mcp.tool()
async def attach_desktop_gui_window(
    window_title_keyword: str,
    tuck_offscreen: bool = True,
) -> Dict[str, Any]:
    """Attach a desktop window matching the title keyword to the console; optionally tuck it offscreen."""
    windows = Win32WindowHelper.list_top_windows()
    matched = [w for w in windows if window_title_keyword.lower() in w["title"].lower()]
    if not matched:
        return {
            "attached": False,
            "message": f"No desktop window title contains '{window_title_keyword}'",
            "available_windows": windows[:15],
        }
    target = matched[0]
    sess = await hub.create_session(
        profile_id="desktop-gui",
        launch_source="agent",
        title=target["title"],
        hwnd=target["hwnd"],
    )
    if tuck_offscreen:
        Win32WindowHelper.tuck_window_offscreen(sess, True)
    return {
        "attached": True,
        "session_id": sess.session_id,
        "window": target,
        "tucked_offscreen": sess.tucked_offscreen,
        "console_url": f"{hub_api_base_url()}/?session={sess.session_id}",
    }


# Parent MCP app and Hub lifespan
mcp_parent_app = mcp.http_app(path="/mcp", transport="streamable-http")


@asynccontextmanager
async def combined_lifespan(app_instance: FastAPI):
    async with AsyncExitStack() as stack:
        await hub.start()
        stack.push_async_callback(hub.archive_all_sessions_on_shutdown)
        await stack.enter_async_context(mcp_parent_app.lifespan(app_instance))
        try:
            yield
        finally:
            await hub.archive_all_sessions_on_shutdown()


app = FastAPI(
    title="MCP Non-Headless Sub-Agent Hub (Bidirectional + Telemetry + SQLite)",
    lifespan=combined_lifespan,
)


@app.exception_handler(PersistenceUnavailable)
async def persistence_unavailable_handler(request: Request, exc: PersistenceUnavailable):
    return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Retry-After": "1"})


@app.middleware("http")
async def reject_work_during_shutdown(request: Request, call_next):
    if hub._shutting_down and request.url.path not in {"/health", "/api/hub/info", "/api/hub/shutdown"}:
        return JSONResponse({"detail": "Hub 正在关闭"}, status_code=503)
    return await call_next(request)


@app.middleware("http")
async def hold_requests_during_restart_resume(request: Request, call_next):
    # After a deliberate restart (resume_on_start.json) the hub resumes the interrupted sessions
    # once its port is open. Until that is done (at most 30 s) callers wait here instead of
    # seeing those sessions interrupted or racing the resume; the desktop app's probes do not.
    if not hub.restart_resume_gate.is_open and request.url.path not in {"/health", "/api/hub/info", "/api/hub/shutdown"}:
        await hub.restart_resume_gate.wait()
    return await call_next(request)


# ============================================================================
# 3. Durable parent wait URL and independent console telemetry API
# ============================================================================
@app.get("/api/sessions/{session_id}/trace")
async def api_get_session_trace(session_id: str):
    try:
        return await hub.get_subagent_status_and_trace(session_id)
    except ValueError as e:
        return not_found(f"找不到会话 {session_id}")


@app.get("/api/sessions/{session_id}/final")
async def api_get_session_final(session_id: str):
    try:
        return await hub.get_subagent_final_response(session_id)
    except ValueError as e:
        return not_found(f"找不到会话 {session_id}")


@app.get("/api/history/sessions")
async def api_list_historical_sessions(limit: int = Query(30)):
    return {"sessions": await hub.db.list_persisted_sessions(limit=limit)}


@app.get("/api/wait/{wait_handle}")
async def api_wait(wait_handle: str, timeout: float = Query(25.0, ge=0.1, le=120.0)):
    """Long-poll one opaque handle.  Repeating this URL is the complete parent protocol."""
    try:
        return await hub.wait_inbox.wait(wait_handle, timeout)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/telemetry/events")
async def api_mailbox_events(
    session_id: str = Query("all"),
    after_seq: int = Query(0),
):
    events = hub.get_worker_events(session_id=session_id, after_seq=after_seq)
    return {
        "global_seq": hub.global_seq,
        "events": events,
        "parent_messages": hub.parent_messages.get(session_id, []) if session_id != "all" else hub.parent_messages,
    }


@app.get("/api/telemetry/snapshot")
async def api_telemetry_snapshot():
    """Console-only snapshot; it never advances a parent wait handle."""
    return {"global_seq": hub.global_seq, "events": hub.worker_events[-200:], "sessions": [s.to_dict() for s in hub.sessions.values()]}


@app.post("/api/sessions/{session_id}/respond")
async def api_parent_respond(session_id: str, req: Request):
    body = await req.json()
    try:
        session = hub.sessions.get(session_id)
        if not session:
            raise ValueError(f"Active session '{session_id}' not found.")
        answer = body.get("answer", body.get("text"))
        if answer is not None and not any(body.get(k) is not None for k in ("decision", "option_index", "custom_input")):
            result = await hub.deliver_parent_message(session_id=session_id, text=answer, request_id=body.get("request_id"))
        else:
            result = await hub.respond_to_interaction(
                session_id=session_id, decision=body.get("decision"), option_index=body.get("option_index"),
                custom_input=body.get("custom_input", answer), request_id=body.get("request_id"),
            )
        return {**_control_status(session), "response": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ============================================================================
# 4. Web 总控台与 ConPTY / Win32 路由
# ============================================================================
class RevalidatingStaticFiles(StaticFiles):
    """Console assets: always revalidate (cheap 304) so an edited module never mixes with a cached one."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
async def serve_console():
    return HTMLResponse(STATIC_INDEX.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/api/profiles")
async def api_list_profiles(refresh: bool = Query(False)):
    await ensure_profiles_loaded(refresh_discovery=refresh)
    return await profiles_payload()


@app.post("/api/profiles")
async def api_save_profile(req: Request):
    body = await req.json()
    try:
        saved = await asyncio.to_thread(hub.save_or_update_profile, body)
    except ValueError as e:
        # B8: a validation failure is the client's mistake, not a server fault.
        return bad_request(str(e))
    payload = await profiles_payload()
    payload.update({"ok": True, "profile": saved})
    return payload


@app.post("/api/routing-guide")
async def api_save_routing_guide(req: Request):
    """R2: persist the global routing instructions every parent agent reads. 400 on bad input."""
    body = await req.json()
    text = body.get("global_routing_instructions")
    if not isinstance(text, str):
        return bad_request("global_routing_instructions 必须是字符串。")
    try:
        settings = {key: body[key] for key in ("subagent_idle_timeout_minutes",) if key in body}
        await asyncio.to_thread(hub.save_global_routing_instructions, text, **settings)
    except ValueError as e:
        return bad_request(str(e))
    payload = await profiles_payload()
    payload["ok"] = True
    payload["global_routing_instructions"] = hub.global_routing_instructions
    return payload


@app.delete("/api/profiles/{profile_id}")
async def api_delete_profile(profile_id: str):
    try:
        await asyncio.to_thread(hub.delete_profile, profile_id)
    except ValueError as e:
        return bad_request(str(e))
    payload = await profiles_payload()
    payload["ok"] = True
    return payload


@app.post("/api/engines/{engine}/models/toggle")
async def api_toggle_engine_model(engine: str, req: Request):
    body = await req.json()
    model_id = body.get("model") or body.get("model_id") or ""
    note = body.get("note")
    enabled_field = body.get("enabled")
    # R6: a missing `enabled` is a note-only update; the model keeps its current state.
    enabled = None if enabled_field is None else bool(enabled_field)
    try:
        eng_info = await asyncio.to_thread(
            hub.toggle_engine_model, engine, model_id, enabled, note
        )
    except ValueError as e:
        return bad_request(str(e))
    return {
        "ok": True,
        "engine": engine,
        "engine_info": eng_info,
        "profiles": hub.profiles,
        "engines_catalog": hub.engines_catalog,
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
        "default_cwd": hub.hub_default_cwd(),
        "hub_port": hub_port(),
    }

@app.get("/api/sessions")
async def api_list_sessions():
    # B11: never block the first poll on discovery; answer from whatever is already loaded.
    return {
        "global_seq": hub.global_seq,
        "sessions": [s.to_dict() for s in hub.sessions.values()],
        "persisted_sessions": await hub.db.list_persisted_sessions(limit=50, consistent=False),
        "archive_total": await hub.db.count_archived(consistent=False),
        "recent_events": hub.worker_events[-30:],
        "default_cwd": hub.hub_default_cwd(),
        "hub_port": hub_port(),
    }


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    sess = hub.sessions.get(session_id)
    if sess is not None:
        return sess.to_dict()
    rec = await hub.get_archive_record(session_id)
    if rec:
        return rec
    return not_found(f"找不到会话 {session_id}")


# Archive spec §6: the console's archive list, one record, its raw scrollback, revive and delete.
@app.get("/api/archive")
async def api_list_archive(limit: int = Query(50), offset: int = Query(0), q: str = Query("")):
    # R8: a page refreshes up to 200 records with filesystem probes; that must not run on the loop.
    return await hub.list_archive(limit, offset, q)


@app.get("/api/archive/{session_id}")
async def api_get_archive(session_id: str):
    rec = await hub.get_archive_record(session_id)
    if not rec:
        return not_found(f"找不到会话 {session_id}")
    return rec


@app.get("/api/archive/{session_id}/scrollback")
async def api_get_archive_scrollback(session_id: str):
    if await hub.db.get_session_record(session_id) is None:
        return not_found(f"找不到会话 {session_id}")
    return PlainTextResponse(await hub.get_archive_scrollback(session_id) or "")


@app.delete("/api/archive/{session_id}")
async def api_delete_archive(session_id: str):
    try:
        await hub.delete_archive_record(session_id)
    except ValueError as e:
        message = str(e)
        if message.startswith("找不到会话"):
            return not_found(message)
        return bad_request(message)
    return {"ok": True, "session_id": session_id}


@app.post("/api/sessions/{session_id}/resume")
async def api_resume_session(session_id: str, req: Request):
    body: Dict[str, Any] = {}
    try:
        raw = await req.body()
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        body = {}
    text = body.get("text")
    try:
        result = await hub.resume_subagent(
            session_id,
            text=(str(text) if text is not None else None),
            auto_approval_rules=body.get("auto_approval_rules"),
            source="console",
        )
    except ValueError as e:
        # A9: every resume failure is a Chinese 400 (or a 404 for an unknown id), never a 500.
        message = str(e)
        if message.startswith("找不到会话"):
            return not_found(message)
        return bad_request(message)
    session = result["session"]
    return {
        "ok": True,
        "resumed": bool(result.get("resumed")),
        "already_live": bool(result.get("already_live")),
        "resume_count": session.resume_count,
        "delivery": result.get("delivery"),
        **_injection_report(
            session,
            bool((result.get("delivery") or {}).get("injection") == "pending"
                 and session.last_injection is None),
        ),
        "session": session.to_dict(),
    }


@app.put("/api/sessions/{session_id}/approval-rules")
async def api_set_approval_rules(session_id: str, req: Request):
    body = await req.json()
    rules = body.get("auto_approval_rules") if isinstance(body, dict) else None
    try:
        return await hub.set_auto_approval_rules(session_id, rules)
    except ValueError as e:
        return bad_request(str(e))


@app.post("/api/launch-preview")
async def api_launch_preview(req: Request):
    """B7: what a launch would really run. Spawns nothing."""
    body = await req.json()
    await ensure_profiles_loaded()
    try:
        preview = hub.resolve_launch(
            profile_id=body.get("profile_id") or "",
            model=body.get("model"),
            thinking_level=body.get("thinking_level"),
            cwd=body.get("cwd"),
            env=body.get("env"),
            bypass_approvals=body.get("bypass_approvals"),
            approval_mode=body.get("approval_mode"),
            compaction_threshold=body.get("compaction_threshold"),
            extra_args=body.get("extra_args"),
            mcp_servers=body.get("mcp_servers"),
            disabled_mcp_servers=body.get("disabled_mcp_servers"),
            strict_mcp_config=body.get("strict_mcp_config", False),
        )
        if preview.get("engine") == "dsh":
            preview["sandbox_preparation"] = await hub._prepare_dsh_workspace(preview["cwd"], repair=False)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    payload = {"ok": True}
    payload.update(preview)
    return payload


@app.get("/api/sessions/{session_id}/transcript-summary")
async def api_session_transcript_summary(session_id: str):
    try:
        return await hub.summarize_transcript(session_id)
    except ValueError as e:
        return not_found(f"找不到会话 {session_id}")


@app.get("/api/sessions/{session_id}/transcript")
async def api_session_transcript(session_id: str, limit: int = Query(80, ge=10, le=400),
                                 since: str = Query("", max_length=64)):
    """Lightweight engine-neutral transcript feed for the console's default view.

    `since` is the `version` of the caller's last full answer; while nothing changed the answer
    is just `{"unchanged": true, "version": ...}`.
    """
    try:
        return await hub.get_transcript_view(session_id, limit=limit, since=since or None)
    except ValueError as e:
        return not_found(f"找不到会话 {session_id}")


@app.post("/api/sessions")
async def api_create_session(req: Request):
    body = await req.json()
    profile_id = body.get("profile_id", "pwsh")
    task = body.get("task")
    cwd = body.get("cwd")
    title = body.get("title")
    hwnd = body.get("hwnd")
    model = body.get("model")
    thinking_level = body.get("thinking_level")
    env = body.get("env")
    await ensure_profiles_loaded()
    try:
        sess = await hub.create_session(
            profile_id=profile_id,
            task=task,
            cwd=cwd,
            title=title,
            hwnd=hwnd,
            model=model,
            thinking_level=thinking_level,
            env=env,
            bypass_approvals=body.get("bypass_approvals"),
            approval_mode=body.get("approval_mode"),
            compaction_threshold=body.get("compaction_threshold"),
            extra_args=body.get("extra_args"),
            mcp_servers=body.get("mcp_servers"),
            disabled_mcp_servers=body.get("disabled_mcp_servers"),
            strict_mcp_config=body.get("strict_mcp_config", False),
            auto_approval_rules=body.get("auto_approval_rules"),
        )
    except ValueError as e:
        return bad_request(str(e))
    return {"session": sess.to_dict()}


@app.post("/api/sessions/{session_id}/input")
async def api_send_input(session_id: str, req: Request):
    sess = hub.sessions.get(session_id)
    if not sess:
        return not_found(f"找不到会话 {session_id}")
    body = await req.json()
    text = body.get("text", "")
    try:
        delivery = await hub.deliver_parent_message(session_id=session_id, text=text, also_write_pty=True,
            press_enter=bool(body.get("press_enter", False)), clear_input=bool(body.get("clear_input", False)),
            source="console")
    except ValueError as e:
        return bad_request(str(e))
    return {"ok": delivery.get("delivered") is True or delivery.get("input_cleared") is True,
            "delivery": delivery}


@app.post("/api/sessions/{session_id}/message")
async def api_send_message(session_id: str, req: Request):
    """The transcript view's message box: what send_to_subagent does, sent by the console user.

    A live session gets the checked delivery with Enter (TUI submit / DSH steer or continue); an
    ended or archived one is resumed with the text.  Parent wait handles are left alone, as for
    text typed into the TUI.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    text = str((body or {}).get("text") or "")
    if not text.strip():
        return bad_request("消息为空")
    session = hub.sessions.get(session_id)
    try:
        if session is not None and _session_can_receive(session):
            if not session.last_task:
                session.last_task = text
            session.current_activity = f"Message from the console: {text[:80]}"
            delivery = await hub.deliver_parent_message(session_id=session_id, text=text, also_write_pty=True,
                                                        press_enter=True, source="console")
            resumed = False
        else:
            result = await hub.resume_subagent(session_id, text=text, source="console")
            session = result.get("session") or hub.sessions.get(session_id)
            delivery = result.get("delivery") or {"delivered": bool(result.get("delivered")),
                                                  "injection": result.get("injection") or "none"}
            resumed = bool(result.get("resumed"))
    except ValueError as e:
        message = str(e)
        return not_found(message) if message.startswith("找不到会话") else bad_request(message)
    # "pending": typed once the TUI is ready (resume) or handed to the checked background submit.
    accepted = delivery.get("delivered") is True or delivery.get("injection") in ("pending", "argv")
    reply = {"ok": bool(accepted), "resumed": resumed, "delivery": delivery,
             "session": session.to_dict() if session is not None else None}
    if not accepted:
        reply["error"] = str(delivery.get("reason") or "消息未送达")
    return reply


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    """B9: removal never rewrites the final status an ended process already earned.

    K5: this is the console’s own removal (the pane’s 终止 button), so the lifecycle event says the
    user did it; only the MCP terminate_subagent tool passes source="mcp".
    """
    result = await hub.close_session(session_id, source="console")
    if not result.get("found"):
        return not_found(f"找不到会话 {session_id}")
    return {
        "ok": True,
        "session_id": session_id,
        "was_alive": result.get("was_alive"),
        "status": result.get("status"),
    }


@app.get("/api/win32/windows")
async def api_list_win32_windows():
    return {"windows": Win32WindowHelper.list_top_windows()}


@app.get("/api/sessions/{session_id}/frame")
async def api_get_window_frame(session_id: str):
    sess = hub.sessions.get(session_id)
    if not sess or not sess.hwnd:
        return not_found(f"找不到会话 {session_id}")
    frame = Win32WindowHelper.capture_window_jpeg_b64(sess.hwnd)
    if not frame:
        raise HTTPException(status_code=410, detail="窗口已关闭或不可用")
    return frame


@app.post("/api/sessions/{session_id}/tuck")
async def api_tuck_window(session_id: str, req: Request):
    sess = hub.sessions.get(session_id)
    # S2: an unknown session is a 404 with the same wording every other session route uses.
    if not sess:
        return not_found(f"找不到会话 {session_id}")
    if not sess.hwnd:
        return bad_request(f"会话 {session_id} 不是桌面窗口会话。")
    body = await req.json()
    tuck = bool(body.get("tuck", True))
    Win32WindowHelper.tuck_window_offscreen(sess, tuck)
    return {"ok": True, "tucked_offscreen": sess.tucked_offscreen}


@app.post("/api/sessions/{session_id}/click")
async def api_click_window(session_id: str, req: Request):
    sess = hub.sessions.get(session_id)
    if not sess:
        return not_found(f"找不到会话 {session_id}")
    if not sess.hwnd:
        return bad_request(f"会话 {session_id} 不是桌面窗口会话。")
    body = await req.json()
    Win32WindowHelper.send_click(sess.hwnd, int(body.get("x", 0)), int(body.get("y", 0)))
    return {"ok": True}


@app.websocket("/ws/pty/{session_id}")
async def ws_pty_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    sess = hub.sessions.get(session_id)
    if not sess:
        await websocket.close(code=1008)
        return

    # Q11: snapshot the replay and register this socket's live queue in one atomic step, with the
    # same lock the PTY reader uses for "append to history + snapshot the fan-out targets". A chunk
    # is therefore either inside this replay or in the queue — never lost, never sent twice. The
    # queue is registered first, so output produced while the replay is on the wire waits in order.
    replay, q = sess.attach_ws_queue()

    # W7: removing a session from the live list closes its sockets with a normal code instead of
    # leaving the server side open and silent. The hook travels with the queue when a resume
    # replaces the session object (AgentSession.adopt_ws_frontends).
    def _close_on_archive() -> None:
        try:
            asyncio.create_task(websocket.close(code=1000))
        except Exception:
            pass

    sess.register_ws_closer(q, _close_on_archive)
    if replay:
        await websocket.send_text(replay)
    # V3: an explicit end-of-replay marker. Output frames stay raw text; the console drops the terminal
    # query responses xterm generates while parsing this replay and keeps forwarding the live ones.
    await websocket.send_text(json.dumps({"type": "replay_end"}))

    async def sender():
        try:
            while True:
                data = await q.get()
                await websocket.send_text(data)
        except Exception:
            pass

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            raw_msg = await websocket.receive_text()
            # A1: a resume replaces the AgentSession object under the same session id (the sockets
            # are handed over to the new object), so every input frame must go to the session that
            # is live under this id now, not to the object this socket connected to.
            live = hub.sessions.get(session_id)
            try:
                msg = json.loads(raw_msg)
            except Exception:
                msg = None
            if isinstance(msg, dict):
                mtype = msg.get("type")
                try:
                    if mtype == "input" and live is not None:
                        cleaned_input = strip_osc_terminal_replies(str(msg.get("data") or ""))
                        if cleaned_input:
                            live.write_input(cleaned_input)
                    elif mtype == "resize":
                        hub.resize_session_from_client(
                            session_id,
                            int(msg.get("cols", 120)),
                            int(msg.get("rows", 36)),
                            propagate_background=bool(msg.get("propagate", True)),
                        )
                except Exception:
                    pass
            elif live is not None:
                cleaned_raw = strip_osc_terminal_replies(raw_msg)
                if cleaned_raw:
                    live.write_input(cleaned_raw)
    except WebSocketDisconnect:
        pass
    finally:
        sess.detach_ws_queue(q)
        sender_task.cancel()


# ============================================================================
# 5. Desktop shell (desktop/): hub identity and a token-guarded graceful stop
# ============================================================================
HUB_STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")
# Set only by a launcher that owns this hub (the desktop app). Read once at start and removed from
# the environment, so no agent process the hub spawns inherits it. Never logged.
_HUB_SHUTDOWN_TOKEN = os.environ.pop("HUB_SHUTDOWN_TOKEN", "")


@app.get("/health")
async def api_health():
    return {
        "ok": True,
        "status": "healthy",
        "service": "SubAgent-NonHeadless-Hub",
        "version": HUB_VERSION,
        "active_sessions": len(hub.sessions),
        "hub_port": hub_port(),
        "started_at": HUB_STARTED_AT,
        "persistence": hub.db.status(),
    }


@app.get("/api/hub/info")
async def api_hub_info():
    return {
        "service": "mcp-agent-hub",
        "version": HUB_VERSION,
        "pid": os.getpid(),
        "port": hub_port(),
        "repo": str(BASE_DIR),
        "started_at": HUB_STARTED_AT,
        "shutdown_enabled": bool(_HUB_SHUTDOWN_TOKEN),
    }


async def _stop_hub_like_ctrl_c() -> None:
    # Persist/archive every live session and unblock long-pollers before uvicorn begins tearing
    # down HTTP/MCP streams, so even if an open stream hits timeout_graceful_shutdown no session
    # record is ever lost.
    try:
        await hub.archive_all_sessions_on_shutdown()
    except Exception:
        logging.getLogger(__name__).exception("Hub shutdown failed before SIGINT")
    signal.raise_signal(signal.SIGINT)


@app.post("/api/hub/shutdown")
async def api_hub_shutdown(req: Request):
    if not _HUB_SHUTDOWN_TOKEN:
        return not_found("此 Hub 未启用远程停止")
    given = req.headers.get("x-hub-shutdown-token", "")
    if not hmac.compare_digest(given.encode("utf-8"), _HUB_SHUTDOWN_TOKEN.encode("utf-8")):
        return JSONResponse({"detail": "停止令牌无效"}, status_code=403)
    # The answer goes out first; the stop runs after the response is sent.
    return JSONResponse({"ok": True}, background=BackgroundTask(_stop_hub_like_ctrl_c))


# Mount Parent MCP (/mcp)
for route in mcp_parent_app.routes:
    app.router.routes.append(route)


class TranscriptPollLogFilter(logging.Filter):
    """Leaves the console's transcript polls (several a second per visible pane) out of the access log.

    uvicorn logs each request as (client, method, path with query, HTTP version, status); failed
    polls are still logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            method, path, status = args[1], str(args[2]), args[4]
            if method == "GET" and "/transcript?" in path and isinstance(status, int) and status < 400:
                return False
        return True


# uvicorn's logging setup keeps filters already on its loggers.
logging.getLogger("uvicorn.access").addFilter(TranscriptPollLogFilter())

# fastmcp logs each failed tool call through rich: a boxed traceback with syntax-highlighted
# source, rendered synchronously on the event loop. In the live hub on 2026-11-12 one failed
# set_subagent_approval_rules call held the loop for 1.75 s that way (pygments lexing, regex
# compiles, imports). Plain logging writes the same traceback in milliseconds.
fastmcp.settings.enable_rich_logging = False
configure_fastmcp_logging(level=fastmcp.settings.log_level)


if __name__ == "__main__":
    # One client dropping its connection mid-accept must not take the hub off the network
    # (asyncio's Windows loop would close the listening socket); see windows_accept_guard.
    windows_accept_guard.install()
    port = hub_port()
    # B11: startup discovery runs in a thread, so the first /api/sessions poll answers at once.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        timeout_graceful_shutdown=2,
    )
