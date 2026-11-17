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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
import fastmcp
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware
from fastmcp.utilities.logging import configure_logging as configure_fastmcp_logging
import uvicorn

from session_manager import (
    HubSessionManager,
    default_profiles_path,
    hub_api_base_url,
    hub_port,
    parent_interaction_view,
    parent_turn_outcome_fields,
    resolve_session_approval_fields,
)
from transcript_summarizer import sanitize_tui_screen_text
from hub_persistence import PersistenceUnavailable
from loopback_request_guard import LoopbackRequestGuard
from agent_guidance import SERVER_INSTRUCTIONS
from mcp_caller_identity import resolve_caller, same_caller
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


async def _mcp_caller() -> Optional[Dict[str, Any]]:
    """The process, and the hub session, behind the current MCP request when the hub can tell.

    {"pid", "process", "hub_session_id", "hub_session_title"}; None for an in-process client or
    when the loopback connection's owner cannot be found.
    """
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    client, server = request.client, request.scope.get("server")
    if not client or not server:
        return None
    session_pids = {int(s.pid): sid for sid, s in hub.sessions.items() if getattr(s, "pid", None)}
    caller = await asyncio.to_thread(resolve_caller, client.host, client.port, int(server[1]), session_pids)
    if caller and caller.get("hub_session_id"):
        owner = hub.sessions.get(str(caller["hub_session_id"]))
        caller["hub_session_title"] = owner.title if owner else None
    return caller


def _recorded_caller(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The spawned_by a live session or a stored row keeps in its launch snapshot.

    Summary rows (list_persisted_sessions) carry it as spawned_by_caller instead of the snapshot.
    """
    if isinstance(record.get("spawned_by_caller"), dict):
        return record["spawned_by_caller"]
    snapshot = record.get("profile_snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot) if snapshot else {}
        except ValueError:
            snapshot = {}
    caller = snapshot.get("spawned_by") if isinstance(snapshot, dict) else None
    return caller if isinstance(caller, dict) else None


def _spawned_by_view(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Who spawned a session, as list_active_sessions shows it."""
    caller = _recorded_caller(record)
    if caller is None:
        return None
    if caller.get("hub_session_id"):
        return {"session_id": caller["hub_session_id"], "title": caller.get("hub_session_title")}
    return {"process": caller.get("process"), "pid": caller.get("pid")}


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


# A parent connected before a hub update may still hold a tool list with these names; its call
# reaches the server and would otherwise get a bare "Unknown tool".
RETIRED_TOOL_HINTS = {
    "respond_to_subagent_approval_or_choice": (
        "respond_to_subagent",
        "Until then, answer the pending request with send_to_subagent(session_id, text='approve' / "
        "'deny' / an option number); it resolves the same request.",
    ),
    "reply_to_subagent": (
        "send_to_subagent or respond_to_subagent",
        "Until then, send_to_subagent(session_id, text) delivers a reply.",
    ),
    "wait_subagent_events": (
        "the wait_url that every spawn/send/respond result returns",
        "Until then, GET that wait_url; it needs no other tool.",
    ),
    # Removed with the desktop GUI window sessions; nothing replaces it.
    "attach_desktop_gui_window": (None, "The hub no longer attaches desktop windows."),
}


class RetiredToolMiddleware(Middleware):
    """Turn a call to a renamed tool into a hint instead of a bare unknown-tool error."""

    async def on_call_tool(self, context, call_next):
        hint = RETIRED_TOOL_HINTS.get(context.message.name)
        if hint is None:
            return await call_next(context)
        replacement, fallback = hint
        instead = f"use {replacement} instead" if replacement else "it has no replacement"
        raise ToolError(
            f"{context.message.name} is retired; {instead}. Your client's tool list predates "
            f"a hub update, so reconnect the agent-hub MCP server to refresh it. {fallback}"
        )


mcp.add_middleware(RetiredToolMiddleware())


class LeafWorkerArgumentMiddleware(Middleware):
    """Explain a spawn_subagent call without leaf_worker: the parent's tool list may predate the field."""

    async def on_call_tool(self, context, call_next):
        if context.message.name == "spawn_subagent" and "leaf_worker" not in (context.message.arguments or {}):
            raise ToolError(
                "spawn_subagent requires leaf_worker (true or false): true tells the worker to do the task "
                "itself and never spawn subagents; false sends the task unchanged. If your tool list has no "
                "leaf_worker field, it predates a hub update: reconnect the agent-hub MCP server to refresh "
                "it, or pass the field anyway."
            )
        return await call_next(context)


mcp.add_middleware(LeafWorkerArgumentMiddleware())


@mcp.tool()
async def list_agent_profiles(refresh_discovery: bool = False, view: str = "compact") -> Dict[str, Any]:
    """
    List enabled profiles and routing guidance. Models and thinking levels are in
    routing_guide_markdown; view="full" adds the engine catalog.
    Read routing_guide_markdown and each profile's when_to_use before choosing an agent.
    Profiles include defaults; engines_catalog excludes disabled profiles and models.
    view="routing" returns only the routing rules, the fallbacks and the profile ids (about a
    third of the compact size), for re-reading the routing after a compaction.
    Set refresh_discovery=true to refresh the installed engine catalog.
    """
    await ensure_profiles_loaded(refresh_discovery=refresh_discovery)
    if view not in {"compact", "full", "routing"}:
        raise ValueError("view must be compact, full or routing")
    if view == "routing":
        return {"routing_guide_markdown": hub.build_routing_only_markdown()}
    base_url = hub_api_base_url()
    payload = {
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
        "global_routing_instructions": hub.global_routing_instructions,
        "hub_console_url": f"{base_url}/",
        "engines_catalog": hub.get_mcp_engines_catalog(),
        "profiles": hub.get_mcp_enabled_profiles(),
    }
    if view == "full":
        return payload
    profiles = {}
    for pid, profile in payload["profiles"].items():
        profiles[pid] = {key: profile.get(key) for key in ("id", "name", "engine", "default_model", "default_thinking_level", "when_to_use")}
        if profile.get("env"):
            profiles[pid]["env"] = profile["env"]
    return {"routing_guide_markdown": payload["routing_guide_markdown"], "hub_console_url": payload["hub_console_url"], "profiles": profiles}


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
    leaf_worker: bool,
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
    auto_approve_once_prefixes: Optional[List[str]] = None,
    brief_markdown: Optional[str] = None,
    wait_seconds: float = 5.0,  # deprecated compatibility field; never blocks
    approval_mode: Optional[str] = None,
    compaction_threshold: Optional[Union[int, str]] = None,
    extra_roots: Optional[List[str]] = None,
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """Start a subagent and return an opaque wait handle immediately.

    ``leaf_worker`` is required, decide it on every spawn: true (the usual choice for a
    self-contained job) puts a note before ``task`` telling the worker it is a leaf (do the task
    itself, never spawn subagents or hand work to another agent); false, only for a worker meant
    to dispatch others, sends the task unchanged. true needs a ``task`` or a ``brief_markdown``
    (the note then goes before the brief's pointer line). The note is advice, not a
    block: for Codex and Claude, add ``disabled_mcp_servers=["agent-hub"]`` to take the hub's tools
    away too.

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
    ``extra_roots`` (claude, codex, omp, kimi, agy) names up to 8 absolute folders beyond ``cwd``
    that the worker may read AND write, as it does ``cwd`` (each becomes ``--add-dir``; no engine
    offers a read-only folder). List each folder the task must use there, such as a shared lock
    folder, never a parent that holds them (it grants everything inside). Refused: a drive root,
    the user's home or a folder holding it, credential and engine settings folders, system and
    application folders, the hub's data; DSH takes none.
    ``compaction_threshold`` sets this session's auto-compaction point: ``"70%"`` of the context
    window or a token count (``200000``, ``"200k"``). omp takes both, claude only a percent (it can
    only compact earlier than its default), codex only tokens; other engines reject it.
    ``auto_approve_once_prefixes`` (omp and agy only) names up to 32 command prefixes, each starting
    with the program's absolute path: the hub answers approve once, never approve for the session,
    for a high-risk command that starts with one. Each automatic approval leaves an
    ``approval_auto_resolved`` event.
    ``brief_markdown`` carries a task description longer than a few lines: the hub writes it under
    its data folder, starts the task with a one-line pointer to the file, and deletes it when the
    session closes (limit 200 000 characters).
    ``wait_seconds`` is deprecated and ignored for waiting; use ``wait_url``.
    """
    await ensure_profiles_loaded()
    session = await hub.create_session(
        profile_id=agent_type,
        launch_source="agent",
        spawned_by=await _mcp_caller(),
        bypass_approvals=bypass_approvals,
        approval_mode=approval_mode,
        compaction_threshold=compaction_threshold,
        leaf_worker=leaf_worker,
        extra_args=extra_args,
        mcp_servers=mcp_servers,
        disabled_mcp_servers=disabled_mcp_servers,
        strict_mcp_config=strict_mcp_config,
        auto_approval_rules=auto_approval_rules,
        auto_approve_once_prefixes=auto_approve_once_prefixes,
        brief_markdown=brief_markdown,
        extra_roots=extra_roots,
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

    Each spec takes spawn_subagent's fields, leaf_worker (true or false) required in every spec;
    approval_mode follows the same default (auto). A spec may also carry
    auto_approve_once_prefixes (omp and agy, approve once for a high-risk command starting with
    one), brief_markdown (a task description longer than a few lines, written to a file the hub
    deletes when the session closes) and extra_roots (folders beyond cwd the worker may read and
    write)."""
    if not specs:
        raise ValueError("specs must contain at least one subagent")
    for spec in specs:
        if type(spec.get("leaf_worker")) is not bool:
            raise ValueError(
                "each spec requires leaf_worker (true or false): true tells the worker to do the task "
                "itself and never spawn subagents; false sends the task unchanged"
            )
    sessions = []
    caller = await _mcp_caller()
    for spec in specs:
        item = dict(spec)
        item.pop("spawned_by", None)  # the hub records the caller itself; a spec cannot name one
        agent_type = item.pop("agent_type", item.pop("profile_id", None))
        if not agent_type:
            raise ValueError("each spec requires agent_type")
        sessions.append(await hub.create_session(profile_id=agent_type, launch_source="agent",
                                                 spawned_by=caller, **item))
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
        "pending_interaction": parent_interaction_view(session.pending_interaction),
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

    Keep ``text`` short: put a longer follow-up in a file under the session cwd and send a
    one-line pointer to it. ``wait_seconds`` is deprecated and ignored for waiting; use ``wait_url``.
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
async def wait_for_hub(
    wait_handle: str,
    timeout_seconds: float = 50.0,
    ask_coalesce_ms: int = 800,
) -> Dict[str, Any]:
    """Wait on a wait_handle or complete wait_url, returning waiting, ask, undelivered, done or failed.

    Hosts that cannot keep a background GET running (including shells capped at 10 seconds)
    should use this tool. A timeout is a normal ``state=waiting`` result. When asking, ``requests``
    contains every currently open request; answer all of them before waiting again.
    """
    handle = str(wait_handle or "").rstrip("/").rsplit("/", 1)[-1]
    if not handle.startswith("wh_"):
        raise ValueError("wait_handle must be a wh_... handle or wait_url")
    if not 1 <= float(timeout_seconds) <= 300:
        raise ValueError("timeout_seconds must be between 1 and 300")
    if not 0 <= int(ask_coalesce_ms) <= 5000:
        raise ValueError("ask_coalesce_ms must be between 0 and 5000")
    return await hub.wait_inbox.wait(handle, timeout_seconds, ask_coalesce_ms=ask_coalesce_ms)


async def _respond_to_subagent_impl(
    session_id: str,
    answer: Optional[str] = None,
    decision: Optional[str] = None,
    option_index: Optional[int] = None,
    custom_input: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
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
    if not session.wait_handle:
        wait = await _wait_contract([session_id], turn_ids={session_id: session.turn_id})
        session.wait_handle = wait["wait_handle"]
    else:
        wait = {"wait_handle": session.wait_handle, "wait_url": f"{hub_api_base_url()}/api/wait/{session.wait_handle}"}
    return {**_control_status(session, accepted=bool(action_result.get("ok", True))), **wait,
            "response": {key: action_result.get(key) for key in RESPOND_REPLY_KEYS if key in action_result}}


# What a respond reply echoes from the interaction answer. The chosen option (1-based index, label,
# the action it maps to, and the asked-for action when the hub downgraded it) lets the parent check
# what was really selected; the wait-handle refactor (0168939) had dropped them.
RESPOND_REPLY_KEYS = (
    "delivered", "response_state", "resolved_request_id", "idempotent",
    "chosen_option_index", "chosen_label", "approval_action", "requested_approval_action",
)


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
    return await _respond_to_subagent_impl(
        session_id=session_id, answer=answer, decision=decision,
        option_index=option_index, custom_input=custom_input, request_id=request_id,
    )


@mcp.tool()
async def respond_to_subagents(
    responses: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Answer up to 50 pending requests independently and report each outcome.

    Each item has session_id, request_id and the same answer fields as respond_to_subagent:
    decision, option_index, answer or custom_input. One invalid item does not stop the others.
    """
    if len(responses) > 50:
        raise ValueError("responses may contain at most 50 entries")
    outcomes = []
    wait_handles = []
    for item in responses:
        try:
            if not isinstance(item, dict):
                raise ValueError("response entry must be an object")
            session_id = item.get("session_id")
            request_id = item.get("request_id")
            if not session_id or not request_id:
                raise ValueError("session_id and request_id are required")
            result = await _respond_to_subagent_impl(
                session_id=str(session_id), request_id=str(request_id),
                answer=item.get("answer"), decision=item.get("decision"),
                option_index=item.get("option_index"), custom_input=item.get("custom_input"),
            )
            if result.get("wait_handle"):
                wait_handles.append(result["wait_handle"])
            outcomes.append({"session_id": session_id, "request_id": request_id, "ok": True, "result": result})
        except Exception as exc:
            outcomes.append({"session_id": item.get("session_id") if isinstance(item, dict) else None,
                             "request_id": item.get("request_id") if isinstance(item, dict) else None,
                             "ok": False, "error": str(exc)})
    return {"responses": outcomes, "wait_handles": list(dict.fromkeys(wait_handles))}


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
        raw_txt = session.read_screen(max_lines=max_lines).text
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
async def list_active_sessions(
    include_historical: bool = False,
    limit: int = 25,
    view: str = "compact",
    wait_handle: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
    status_filter: Optional[List[str]] = None,
    mine: bool = False,
) -> Dict[str, Any]:
    """List sessions; compact is the default and full preserves the legacy payload.

    Each compact entry names spawned_by: {"session_id", "title"} when a hub session spawned it,
    {"process", "pid"} for a parent outside the hub, null when unknown (console launches, older
    sessions). mine=true keeps only sessions the calling parent spawned: the same hub session, or
    the same external process (a restarted parent is a new process).
    """
    if view not in {"compact", "full"}:
        raise ValueError("view must be compact or full")
    caller = await _mcp_caller() if mine else None
    if mine and caller is None:
        raise ValueError("mine=true needs a caller the hub can identify (a loopback MCP connection on Windows)")
    active = []
    for session in hub.sessions.values():
        item = session.to_dict()
        item.setdefault("effective_model", getattr(session, "effective_model", None) or getattr(session, "model", None))
        snapshot_only = {"profile_snapshot": getattr(session, "profile_snapshot", None)}
        item["spawned_by"] = _spawned_by_view(snapshot_only)
        if mine and not same_caller(caller, _recorded_caller(snapshot_only)):
            continue
        active.append(item)
    wanted = set(str(x) for x in session_ids or [])
    if wait_handle:
        handle = str(wait_handle).rstrip("/").rsplit("/", 1)[-1]
        group_ids = set(hub.wait_inbox.session_ids_for(handle))
        wanted = (wanted & group_ids) if wanted else group_ids
    if wanted:
        active = [s for s in active if s.get("session_id") in wanted]
    if status_filter:
        allowed = set(status_filter)
        active = [s for s in active if s.get("status") in allowed]
    def compact(s):
            pending = s.get("pending_interaction") or {}
            prompt = pending.get("prompt") or pending.get("command_or_prompt") or pending.get("command") or ""
            final = s.get("final_response")
            has_final = bool(s.get("has_final_response")) if "has_final_response" in s else bool(final)
            entry = {"session_id": s.get("session_id"), "title": s.get("title"), "agent_type": s.get("profile_id") or s.get("engine"), "model": s.get("effective_model") or s.get("model") or "", "status": s.get("status"), "current_activity": str(s.get("current_activity") or "")[:120], "step_count": s.get("step_count", 0), "final_response_exists": has_final, "final_response_length": len(final or "") if final is not None else 0, "cwd": s.get("cwd"), "updated_at": s.get("last_output_at") or s.get("ended_at") or s.get("created_at"), "spawned_by": s["spawned_by"] if "spawned_by" in s else _spawned_by_view(s)}
            if pending:
                entry["open_request"] = {"request_id": pending.get("request_id"), "kind": pending.get("kind"), "prompt": str(prompt)[:120]}
            entry.update(parent_turn_outcome_fields(s))
            return entry
    if view == "compact":
        historical = []
        if include_historical:
            historical = await hub.db.list_persisted_sessions(limit=limit, include_archived=False, consistent=False)
            if wanted:
                historical = [s for s in historical if s.get("session_id") in wanted]
            if status_filter:
                historical = [s for s in historical if s.get("status") in allowed]
            if mine:
                historical = [s for s in historical if same_caller(caller, _recorded_caller(s))]
        return {"global_seq": hub.global_seq, "active_count": len(active), "active_sessions": [compact(s) for s in active], "historical_sessions": [compact(s) for s in historical]}
    result: Dict[str, Any] = {
        "global_seq": hub.global_seq,
        "active_count": len(active),
        "active_sessions": active,
    }
    if include_historical:
        # Spec §6: list_active_sessions stays "live + ended-in-memory"; archived records are listed
        # by list_archived_sessions.
        persisted = await hub.db.list_persisted_sessions(limit=limit, include_archived=False, consistent=False)
        if mine:
            persisted = [s for s in persisted if same_caller(caller, _recorded_caller(s))]
        result["persisted_sessions"] = persisted
    return result


@mcp.tool()
async def collect_and_terminate(
    wait_handle: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
    only_completed: bool = True,
    max_chars: int = 3000,
) -> Dict[str, Any]:
    """Collect final answers and close sessions; only_completed=False also closes running sessions.

    A running session closed this way has no answer: its entry says interrupted=true,
    final_response="" and carries a progress block (how far the worker got, as terminate_subagent
    reports it).
    """
    if bool(wait_handle) == bool(session_ids):
        raise ValueError("provide exactly one of wait_handle or session_ids")
    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
    if wait_handle:
        handle = str(wait_handle).rstrip("/").rsplit("/", 1)[-1]
        ids = hub.wait_inbox.session_ids_for(handle)
    else:
        ids = list(dict.fromkeys(str(x) for x in session_ids or []))
    collected, skipped, cwd_dirs = [], [], set()
    done_states = {"completed", "failed", "ended", "exited", "crashed", "terminated", "interrupted"}
    for sid in ids:
        try:
            sess = hub.sessions.get(sid)
            rec = sess.to_dict() if sess else await hub.db.get_session_record(sid)
            if not rec:
                skipped.append({"session_id": sid, "status": "missing"})
                continue
            status = rec.get("status")
            if only_completed and status not in done_states:
                skipped.append({"session_id": sid, "status": status})
                continue
            already_closed = sess is None
            if sess is not None and status in MID_TURN_STATUSES:
                stopped = await _terminate_mid_turn(sess)
                if rec.get("cwd"):
                    cwd_dirs.add(str(rec["cwd"]))
                if stopped["interrupted"]:
                    collected.append({"session_id": sid, "status": status, "interrupted": True,
                                      "final_response": "", "full_length": 0, "truncated": False,
                                      "already_closed": False, "progress": stopped["progress"]})
                    continue
                # The turn ended on its own while it was being stopped: its answer is collected.
                rec, full = sess.to_dict(), stopped.get("final_response") or ""
                status = rec.get("status")
            elif already_closed:
                full = rec.get("final_response") or ""
            else:
                info = await hub.get_subagent_final_response(sid)
                full = info.get("final_response") or ""
                await hub.close_session(sid, source="mcp")
            if rec.get("cwd"):
                cwd_dirs.add(str(rec["cwd"]))
            entry = {"session_id": sid, "status": status, "final_response": full[:max_chars], "full_length": len(full), "truncated": len(full) > max_chars, "already_closed": already_closed}
            entry.update(parent_turn_outcome_fields(rec))
            if entry["truncated"]:
                entry["hint"] = "Call get_subagent_final_response for the rest."
            collected.append(entry)
        except Exception as exc:
            collected.append({"session_id": sid, "error": str(exc)})
    from cwd_git_status import collect_git_status
    return {"collected": collected, "skipped": skipped, "git_status": await collect_git_status(sorted(cwd_dirs))}


# A session in one of these is in the middle of a turn: its newest text is progress, not an answer.
MID_TURN_STATUSES = ("starting", "running", "waiting_parent")


def _this_turns_answer(session) -> Optional[str]:
    """The answer the current turn produced, if it has one (an earlier turn's answer is not it)."""
    if session.final_response and not session.final_response_from_previous_turn:
        return session.final_response
    return None


async def _close_with_answer(session_id: str, closed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Close a session that is between turns (unless `closed` says it already is) and return its answer."""
    result = closed if closed is not None else await hub.close_session(session_id, source="mcp")
    final_info = await hub.get_subagent_final_response(session_id)
    return {
        "terminated": True,
        "session_id": session_id,
        "was_alive": result.get("was_alive"),
        "status": result.get("status"),
        "persisted_in_sqlite": hub.db.status()["pending_writes"] == 0,
        "interrupted": False,
        "final_response": final_info.get("final_response"),
        "final_response_source": final_info.get("final_response_source"),
        "step_count": final_info.get("step_count"),
    }


async def _terminate_mid_turn(session) -> Dict[str, Any]:
    """Stop a session mid-turn and report how far it got, never as its answer.

    The turn can still end on its own while this runs (the summary read and the close both wait):
    then the reply is the normal one, interrupted=false with the answer.
    """
    session_id = session.session_id
    status_before, activity = session.status, session.current_activity
    previous_answer = session.final_response if session.final_response_from_previous_turn else None
    answer_before = _this_turns_answer(session)
    # Summarised before the close: a running session's summary never becomes its final response.
    try:
        summary = await hub.summarize_transcript(session_id)
    except Exception as exc:
        summary = {"summary_error": str(exc)}
    if session.status not in MID_TURN_STATUSES or _this_turns_answer(session) != answer_before:
        return await _close_with_answer(session_id)
    if "last_assistant_text" in summary:
        last_text = summary.get("last_assistant_text")
    else:
        # Engines without a separate field: their conclusion, unless it is a stored earlier answer.
        last_text = summary.get("latest_conclusion")
        if last_text and last_text == session.final_response:
            last_text = None
    result = await hub.close_session(session_id, source="mcp")
    if _this_turns_answer(session) != answer_before:
        return await _close_with_answer(session_id, closed=result)
    from cwd_git_status import collect_git_status
    progress = {
        "note": "Stopped mid-turn: this is how far the worker got, not its answer.",
        "status_before": status_before,
        "current_activity": activity,
        "step_count": max(int(session.step_count or 0), int(summary.get("step_count") or 0)),
        "last_assistant_text": last_text,
        "assistant_messages_after_last_text": summary.get("assistant_messages_after_last_text"),
        "recent_actions": list(summary.get("recent_actions") or [])[-8:],
        "last_thought": (list(summary.get("key_thoughts") or []) or [None])[-1],
        "files_changed": list(session.files_changed or [])[-30:],
    }
    if previous_answer:
        progress["previous_turn_answer"] = previous_answer
    if summary.get("summary_error"):
        progress["summary_error"] = summary["summary_error"]
    return {
        "terminated": True,
        "session_id": session_id,
        "was_alive": result.get("was_alive"),
        "status": result.get("status"),
        "persisted_in_sqlite": hub.db.status()["pending_writes"] == 0,
        "interrupted": True,
        "final_response": None,
        "final_response_source": None,
        "progress": progress,
        "git_status": await collect_git_status([str(session.cwd)]) if session.cwd else {},
    }


@mcp.tool()
async def terminate_subagent(session_id: str) -> Dict[str, Any]:
    """
    Stop a subagent process and queue its answer, terminal history and tool trace for persistence.
    Results remain readable with get_subagent_final_response or get_subagent_status_and_trace.
    A session stopped mid-turn returns interrupted=true, final_response=null and a progress block
    (last assistant text and how old it is, recent actions, activity, files changed, the cwd's git
    status): how far the worker got, not its answer. If the turn ends on its own during the call,
    the reply is the normal one: interrupted=false with the answer.
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

    if session.status in MID_TURN_STATUSES:
        return await _terminate_mid_turn(session)
    # K5: the parent agent ended it, so the lifecycle event says 主 Agent, not 用户.
    return await _close_with_answer(session_id)




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


# Outermost of the app's middlewares: `add_middleware` inserts at the front, so registering the guard
# last means a DNS-rebinding or cross-site request is refused before any other middleware, route or
# MCP session handles it. Env allowlists: HUB_ALLOWED_HOSTS, HUB_ALLOWED_ORIGINS.
app.add_middleware(LoopbackRequestGuard)


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
async def api_wait(wait_handle: str, timeout: float = Query(25.0, ge=0.1, le=300.0), ask_coalesce_ms: int = Query(800, ge=0, le=5000)):
    """Long-poll one opaque handle.  Repeating this URL is the complete parent protocol."""
    try:
        return await hub.wait_inbox.wait(wait_handle, timeout, ask_coalesce_ms=ask_coalesce_ms)
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
# 4. Web 总控台与 ConPTY 路由
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

@app.post("/api/engines/{engine}/toggle")
async def api_toggle_engine(engine: str, req: Request):
    """Enable or disable one engine; only model policy stays untouched, live sessions keep running."""
    try:
        body = await req.json()
    except Exception:
        return bad_request("请求体必须是 JSON 对象。")
    if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
        return bad_request("enabled 必须是布尔值 true 或 false。")
    try:
        eng_info = await asyncio.to_thread(hub.set_engine_enabled, engine, body["enabled"])
    except ValueError as e:
        return bad_request(str(e))
    payload = await profiles_payload()
    payload.update({"ok": True, "engine": eng_info.get("engine") or engine.strip().lower(),
                    "engine_info": eng_info})
    return payload


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
            extra_roots=body.get("extra_roots"),
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
            auto_approve_once_prefixes=body.get("auto_approve_once_prefixes"),
            brief_markdown=body.get("brief_markdown"),
            leaf_worker=body.get("leaf_worker", False),
            extra_roots=body.get("extra_roots"),
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


@app.get("/api/sessions/{session_id}/terminal-ticket")
async def api_terminal_ticket(session_id: str):
    """§6.4: one single-use ticket for the console's terminal socket, minted by the core.

    The console cannot reach the core's listener without it, and the token is a bearer credential:
    it travels only in this response body (never in a URL the hub serves, so never in an access-log
    line) and is never logged here. A fresh ticket per call, never cached, because a ticket is
    single use and a suspended tab's has expired.

    A session the hub no longer lists, or one whose core entry is gone (retired, closed for good),
    is a 404 before anything is minted. A listed session that already ended but whose core entry
    still exists does get a ticket: its pane attaches the terminal and shows the final replay, as
    the socket always allowed (`mountPaneBody`). A starting session (no first alive record yet) and
    a DSH text session do too.

    r4b #2/kimi 4b #3: the mint reads the core's session table, which can miss its 50 ms deadline
    while a spawn, a retire or a snapshot holds it; the call and its ticket store run in a worker so
    this route never holds the loop, and a deadline answers the retryable 503 the console's backoff
    already waits out. Only a genuine core fault is a 500.
    """
    if hub.sessions.get(session_id) is None:
        return not_found(f"找不到会话 {session_id}")
    core = hub.core
    if core is None:
        # F14: the hub builds its core lazily, but a health-polled or never-launched hub has no
        # terminal to serve. Answering 404 keeps the console's not-found path; no core is built here.
        return not_found(f"找不到会话 {session_id}")
    try:
        ticket = await asyncio.to_thread(core.ws_ticket, session_id)
    except KeyError:
        # The hub still lists the id, but the core's entry is gone: a closed pane, not a retry.
        return not_found(f"找不到会话 {session_id}")
    except TimeoutError:
        # Gaps 8: the table missed its read deadline, which the old `/ws/pty` answered as 1013.
        # The console retries any non-404 answer on its backoff, so say "busy", never 500.
        return JSONResponse({"detail": "终端服务忙，请稍后重试"}, status_code=503,
                            headers={"Retry-After": "1"})
    expires_at = float(ticket.get("expires_at") or 0.0)
    return {
        "url": str(ticket.get("url") or ""),
        "expires_in": max(0, int(math.ceil(expires_at - time.time()))),
    }


# ============================================================================
# 5. Desktop shell (desktop/): hub identity and a token-guarded graceful stop
# ============================================================================
HUB_STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")
# Set only by a launcher that owns this hub (the desktop app). Read once at start and removed from
# the environment, so no agent process the hub spawns inherits it. Never logged.
_HUB_SHUTDOWN_TOKEN = os.environ.pop("HUB_SHUTDOWN_TOKEN", "")


@app.get("/health")
async def api_health():
    # §6.4: the terminal listener's own state, so a dead accept thread is visible before a pane
    # fails. Null until the core exists: health polls must never build it (F14's lazy start).
    core = hub.core
    terminal_ws = {
        "listening": bool(core.ws_listening()) if core is not None else None,
        "port": int(core.ws_port) if core is not None else None,
    }
    return {
        "ok": True,
        "status": "healthy",
        "service": "SubAgent-NonHeadless-Hub",
        "version": HUB_VERSION,
        "active_sessions": len(hub.sessions),
        "hub_port": hub_port(),
        "started_at": HUB_STARTED_AT,
        "persistence": hub.db.status(),
        "terminal_ws": terminal_ws,
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
