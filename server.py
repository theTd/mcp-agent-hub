import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from contextlib import AsyncExitStack, asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import Context, FastMCP
import uvicorn

from session_manager import (
    HubSessionManager,
    Win32WindowHelper,
    hub_api_base_url,
    hub_port,
)
from transcript_summarizer import sanitize_tui_screen_text

BASE_DIR = Path(__file__).parent.resolve()
PROFILES_PATH = BASE_DIR / "agent_profiles.json"
STATIC_DIR = BASE_DIR / "static"
STATIC_INDEX = STATIC_DIR / "index.html"

hub = HubSessionManager(PROFILES_PATH)


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
    "文本将在 TUI 就绪后（或前一条输入完成后）输入并确认提交，调用尚未等到结果；"
    "最终结果可用 get_subagent_status_and_trace 查看 last_injection。"
)


def _injection_report(session, pending: bool = False) -> Dict[str, Any]:
    """W11: spawn/resume/send report the real injection, or say honestly that it is still pending.

    The call waits up to wait_seconds; a TUI that needs longer to become ready has not typed the
    text yet, so last_injection is still null. The CALLER knows whether text was sent in this call
    and whether it is still waiting (delivery.injection == "pending"), so it passes that in; this
    helper must not guess it from task_mode, or a resume without text would claim a pending typing.
    """
    injection = getattr(session, "last_injection", None)
    if isinstance(injection, dict) and injection.get("pending"):
        # K6 (fix 9c): a live message is still being typed and proved in the background; the
        # record says so ("submitting") and its real outcome replaces it.
        return {"injection": "pending", "last_injection": injection,
                "injection_hint": INJECTION_PENDING_HINT}
    if isinstance(injection, dict) and injection:
        return {"injection": "recorded", "last_injection": injection}
    if pending:
        return {
            "injection": "pending",
            "last_injection": None,
            "injection_hint": INJECTION_PENDING_HINT,
        }
    return {"injection": "none", "last_injection": None}


# ============================================================================
# 1. 北向 MCP Server (/mcp) —— 暴露给【主 Agent / Parent Agent】的调度、遥测与双向通信接口
# ============================================================================
mcp = FastMCP(
    "SubAgent-NonHeadless-Hub",
    instructions=(
        "统一的非无头 (Non-Headless) Sub-Agent 聚合网关，支持主从双向实时通信、全链路步骤/工具轨迹追踪与生命周期后持久化查询。\n"
        "核心能力：\n"
        "1. `spawn_subagent(agent_type, task, wait_seconds)`：在统一总控台中以非无头模式拉起 Sub-Agent，返回 `session_id` 与后台阻塞命令 `background_poll_command`；\n"
        "2. `get_subagent_status_and_trace(session_id)`：随时查询 Sub-Agent 当前执行到第几步 (`step_count`)、最近调用了哪些工具 (`recent_tool_calls`)、产生了哪些中间消息 (`intermediate_messages`)；\n"
        "3. `get_subagent_final_response(session_id)`：无论 Sub-Agent 仍在运行还是已经退出/销毁，均可从 SQLite 持久化库中获取其 `final_response`；\n"
        "4. `send_to_subagent` / `reply_to_subagent` / `wait_subagent_events`：主从双向发消息、回答 Sub-Agent 提问与阻塞监听。"
    ),
)


@mcp.tool()
async def list_agent_profiles(refresh_discovery: bool = False) -> Dict[str, Any]:
    """
    列出网关中所有可作为 Sub-Agent 调用的 Agent 引擎配置（包含 omp、kimi、agy、codex、dsh 等）。
    返回内容包含：
    1. `routing_guide_markdown`：用户编写的【Sub-Agent 调用与路由指南】，指导主 Agent 在什么场景下应该调用哪个 Sub-Agent Profile / 模型；
    2. 每个 Profile 的 `when_to_use`（何时调用注释）、已启用模型列表 (`available_models`) 与思考等级 (`available_thinking_levels`)；
    3. 已被用户禁用的模型 (`disabled_models`) 与 Profile (`disabled_profiles`) 不会在可用列表中暴露，请勿调用。
    """
    await ensure_profiles_loaded(refresh_discovery=refresh_discovery)
    base_url = hub_api_base_url()
    return {
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
        "global_routing_instructions": hub.global_routing_instructions,
        "hub_console_url": f"{base_url}/",
        "worker_mcp_http_url": f"{base_url}/worker-mcp",
        "worker_cli_script": str(BASE_DIR / "hub_worker.py"),
        "engines_catalog": hub.engines_catalog,
        "profiles": hub.get_enabled_profiles(),
        "disabled_profiles": [k for k, v in hub.profiles.items() if not v.get("enabled", True)],
    }


@mcp.tool()
async def discover_agent_models_and_thinking_levels(refresh: bool = False) -> Dict[str, Any]:
    """
    [自动探测模型与思考等级] 实时扫描宿主机上安装的所有 Sub-Agent 引擎（omp / kimi / agy / codex / dsh），
    返回每个引擎当前已启用的模型列表 (`available_models`)、已禁用的模型列表 (`disabled_models`)、全部思考等级 (`available_thinking_levels`)，
    以及用户编写的 `routing_guide_markdown`（指示何时调用何种 Agent 与模型）。
    主 Agent 可将 `available_models` 中的任意 `model` 和 `thinking_level` 直接传入 `spawn_subagent(agent_type, task, model=..., thinking_level=...)`。
    """
    await ensure_profiles_loaded(refresh_discovery=refresh)
    return {
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
        "engines": hub.engines_catalog,
        "usage_hint": "Pass any enabled `model` (from `available_models`) and `thinking_level` directly to `spawn_subagent(agent_type, task, model=..., thinking_level=...)`. Follow `routing_guide_markdown` and each profile's `when_to_use` annotation when choosing an agent.",
    }


@mcp.tool()
async def spawn_subagent(
    agent_type: str,
    task: Optional[str] = None,
    model: Optional[str] = None,
    thinking_level: Optional[str] = None,
    approval_mode: str = "auto",
    cwd: Optional[str] = None,
    title: Optional[str] = None,
    wait_seconds: float = 5.0,
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """
    以非无头（Non-Headless 真实交互式 TUI / GUI）模式在统一总控台中拉起一个 Sub-Agent，并返回 `session_id`、`transcript_summary` 与后台阻塞监听命令 `background_poll_command`。
    - 支持通过 `model` 和 `thinking_level` 动态指定任意已启用模型与思考等级（如 `off` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` / `auto`）。
    - 支持通过 `approval_mode` 控制高风险权限与提问代批策略：
      * `"auto"`（默认）：常规命令自动放行；当 Sub-Agent 触发高风险安全规则（如 `dsh-security-mode` 的 `action: ask`）或主动调用交互式提问工具（`ask` / `AskUserQuestion` / `ask_question` / `request_user_input_async`）时，自动进入 `waiting_parent` 状态并上报给主 Agent 代批或代选；
      * `"delegate_to_parent"`：开启高风险行为代批模式（如 Kimi `-y` Ask When Needed、Codex `-a on-request`、AGY `--mode accept-edits`），所有敏感命令/修改及多选题均交由主 Agent 审批！
    """
    await ensure_profiles_loaded()
    start_seq = hub.global_seq
    session = await hub.create_session(
        profile_id=agent_type,
        task=task,
        cwd=cwd,
        title=title,
        model=model,
        thinking_level=thinking_level,
        approval_mode=approval_mode,
    )

    settle_info: Dict[str, Any] = {"settled": True, "reason": "no_wait"}
    if wait_seconds and wait_seconds > 0 and session.kind in ("tui", "web_gui"):
        async def _progress(elapsed: float, snippet: str):
            if ctx:
                await ctx.report_progress(
                    progress=min(elapsed, wait_seconds),
                    total=wait_seconds,
                    message=f"Sub-Agent [{session.session_id}] 正在启动执行 (step={session.step_count}, {int(elapsed)}s)...",
                )

        settle_info = await hub.wait_for_output_settle(
            session,
            timeout_sec=float(wait_seconds),
            quiet_window_sec=2.5,
            progress_cb=_progress,
        )

    events = hub.get_worker_events(session_id=session.session_id, after_seq=start_seq)
    next_seq = events[-1]["seq"] if events else start_seq
    transcript_summary = hub.summarize_transcript(session.session_id)
    # W11: the initial task travels through the TUI only for task_mode=pty_input, and only if the
    # wait did not see it typed yet.
    injection_pending = (
        bool(task)
        and session.last_injection is None
        and session.status in ("starting", "running")
        and str((session.profile_snapshot or {}).get("task_mode") or "") == "pty_input"
    )

    return {
        "session_id": session.session_id,
        "agent_type": session.profile_id,
        "engine": session.engine,
        "approval_mode": session.approval_mode,
        "effective_model": session.effective_model,
        "effective_thinking_level": session.effective_thinking_level,
        "kind": session.kind,
        "status": session.status,
        "step_count": max(session.step_count, transcript_summary.get("step_count", 0)),
        "current_activity": session.current_activity,
        "pending_interaction": session.pending_interaction,
        # A10/W11: the real outcome of typing an initial pty_input task when the wait covered it,
        # otherwise an honest "pending" plus a hint (never a bare last_injection:null).
        **_injection_report(session, injection_pending),
        "console_url": f"{hub_api_base_url()}/?session={session.session_id}",
        "current_seq": next_seq,
        "background_poll_command": hub.build_poll_command(session.session_id, next_seq),
        "worker_events": events,
        "wait_result": settle_info,
        "transcript_summary": transcript_summary,
    }


@mcp.tool()
async def get_subagent_status_and_trace(
    session_id: str,
    recent_tools_limit: int = 15,
    recent_messages_limit: int = 10,
) -> Dict[str, Any]:
    """
    [核心可观测性工具] 查询指定 Sub-Agent 的实时或历史工作情况：
    - 当前执行到了第几步 (`step_count`) 与当前活动描述 (`current_activity`)
    - 最近执行了哪些工具及其入参/结果摘要 (`recent_tool_calls`)
    - 产生了哪些中间思考或进度消息 (`intermediate_messages`)
    - 是否有正在等待主 Agent 回答的问题 (`pending_question_id`)
    - 最终响应 (`final_response`，即使 Sub-Agent 已经退出或被关闭，仍可从 SQLite 持久化存储中查到)
    """
    return hub.get_subagent_status_and_trace(
        session_id=session_id,
        recent_tools_limit=recent_tools_limit,
        recent_messages_limit=recent_messages_limit,
    )


@mcp.tool()
async def get_subagent_final_response(session_id: str) -> Dict[str, Any]:
    """
    [生命周期后持久化查询工具] 获取指定 Sub-Agent 的最终响应 (`final_response`) 与任务总结。
    即使该 Sub-Agent 进程已经自然退出 (`exited`) 或已被调用 `terminate_subagent` 销毁，
    本工具依然能从网关的 SQLite 数据库中完整检索出它的 `final_response`、修改文件列表和终端历史尾部。
    """
    return hub.get_subagent_final_response(session_id=session_id)


@mcp.tool()
async def send_to_subagent(
    session_id: str,
    text: str,
    press_enter: bool = True,
    wait_seconds: float = 10.0,
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """
    [主 Agent -> Sub-Agent] 向运行中的非无头 Sub-Agent 发送消息、补充指令或回答提问：
    1. 如果该 Sub-Agent 当前正阻塞在 `ask_parent` 等待主 Agent 回复，本调用会立即将 `text` 作为答案唤醒并传回给 Sub-Agent；
    2. 否则，会将 `text` 写入 Sub-Agent 的交互式虚拟终端（ConPTY）及主从邮箱，实现实时中途干预（Steering）或多轮对话。
    """
    await ensure_profiles_loaded()
    start_seq = hub.global_seq
    session = hub.sessions.get(session_id)
    resumed = False
    if session is None or not _session_can_receive(session):
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
        delivery = hub.deliver_parent_message(
            session_id=session_id,
            text=text,
            also_write_pty=True,
            press_enter=press_enter,
        )

    settle_info: Dict[str, Any] = {"settled": True, "reason": "no_wait"}
    if wait_seconds and wait_seconds > 0 and session.kind in ("tui", "web_gui"):
        async def _progress(elapsed: float, snippet: str):
            if ctx:
                await ctx.report_progress(
                    progress=min(elapsed, wait_seconds),
                    total=wait_seconds,
                    message=f"等待 Sub-Agent [{session_id}] 处理消息 (step={session.step_count}, {int(elapsed)}s)...",
                )

        settle_info = await hub.wait_for_output_settle(
            session,
            timeout_sec=float(wait_seconds),
            quiet_window_sec=2.5,
            progress_cb=_progress,
        )

    events = hub.get_worker_events(session_id=session_id, after_seq=start_seq)
    next_seq = events[-1]["seq"] if events else start_seq
    transcript_summary = hub.summarize_transcript(session_id)

    return {
        "session_id": session.session_id,
        "status": session.status,
        "resumed": resumed,
        "resume_count": session.resume_count,
        "step_count": max(session.step_count, transcript_summary.get("step_count", 0)),
        "current_activity": session.current_activity,
        "delivery": delivery,
        **_injection_report(
            session,
            bool(delivery.get("injection") == "pending" and session.last_injection is None),
        ),
        "worker_events": events,
        "next_seq": next_seq,
        "background_poll_command": hub.build_poll_command(session_id, next_seq),
        "wait_result": settle_info,
        "transcript_summary": transcript_summary,
    }


@mcp.tool()
async def resume_subagent(
    session_id: str,
    text: Optional[str] = None,
    wait_seconds: float = 10.0,
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """
    [会话恢复工具] 恢复一个已经结束或已归档的 Sub-Agent 会话，沿用同一个 `session_id`（主 Agent 的引用继续有效）：
    - 按引擎的恢复配方重新启动 CLI 并接上原来的对话（omp / kimi / agy / codex / dsh），工作目录、模型与思考等级沿用归档记录；
    - 传入 `text` 时，恢复后按该引擎的初始任务方式把消息交给它；不传则只恢复会话；
    - 会话仍在运行时不重复启动，直接返回当前状态；
    - 不可恢复时会抛出中文原因（例如 `PowerShell 会话结束后无法找回上下文`）。
    """
    await ensure_profiles_loaded()
    start_seq = hub.global_seq
    result = await hub.resume_subagent(session_id, text=text)
    session = result["session"]
    delivery = result.get("delivery") or {
        "delivered": bool(result.get("delivered")),
        "resumed": bool(result.get("resumed")),
        "already_live": bool(result.get("already_live")),
        "resume_count": session.resume_count,
        "injection": result.get("injection") or "none",
    }

    settle_info: Dict[str, Any] = {"settled": True, "reason": "no_wait"}
    if wait_seconds and wait_seconds > 0 and session.kind in ("tui", "web_gui"):
        async def _progress(elapsed: float, snippet: str):
            if ctx:
                await ctx.report_progress(
                    progress=min(elapsed, wait_seconds),
                    total=wait_seconds,
                    message=f"正在等待 Sub-Agent [{session_id}] 恢复并处理消息 (step={session.step_count}, {int(elapsed)}s)...",
                )

        settle_info = await hub.wait_for_output_settle(
            session,
            timeout_sec=float(wait_seconds),
            quiet_window_sec=2.5,
            progress_cb=_progress,
        )

    events = hub.get_worker_events(session_id=session_id, after_seq=start_seq)
    next_seq = events[-1]["seq"] if events else start_seq
    transcript_summary = hub.summarize_transcript(session_id)

    return {
        "session_id": session.session_id,
        "status": session.status,
        "resumed": bool(result.get("resumed")),
        "already_live": bool(result.get("already_live")),
        "resume_count": session.resume_count,
        "step_count": max(session.step_count, transcript_summary.get("step_count", 0)),
        "current_activity": session.current_activity,
        "delivery": delivery,
        **_injection_report(
            session,
            bool(delivery.get("injection") == "pending" and session.last_injection is None),
        ),
        "worker_events": events,
        "next_seq": next_seq,
        "background_poll_command": hub.build_poll_command(session_id, next_seq),
        "wait_result": settle_info,
        "transcript_summary": transcript_summary,
    }


@mcp.tool()
async def list_archived_sessions(query: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """
    [归档列表工具] 列出已结束并归档的 Sub-Agent 会话（最新在前），可按会话 ID、任务、工作目录或 Profile 过滤。
    每条记录返回：`session_id`、Profile、模型 · 思考等级、工作目录、任务首行、结束原因、结束时间，
    以及是否可恢复 (`resumable`) 与不可恢复的原因 (`resume_block_reason`)；用 `resume_subagent` 恢复。
    """
    payload = hub.list_archive(limit=limit, offset=0, query=query or "")
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
async def reply_to_subagent(
    session_id: str,
    answer: str,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """[主 Agent -> Sub-Agent] 专门用于回复 Sub-Agent 提出的问题或审批请求，立即解除 Sub-Agent 的等待阻塞。"""
    return hub.deliver_parent_message(
        session_id=session_id,
        text=answer,
        request_id=request_id,
        also_write_pty=True,
    )


@mcp.tool()
async def respond_to_subagent_approval_or_choice(
    session_id: str,
    decision: Optional[str] = None,
    option_index: Optional[int] = None,
    custom_input: Optional[str] = None,
    request_id: Optional[str] = None,
    wait_seconds: float = 8.0,
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """
    [主 Agent 代批高风险操作 & 代选交互选项工具]
    当 Sub-Agent 处于 `waiting_parent` 状态（收到 `approval_request` 或 `question` 事件，或 `pending_interaction` 非空）时，
    主 Agent 调用本工具按各 CLI 引擎（dsh / kimi / omp / codex / agy）的原生协议完成精准代批或代选：
    - `decision`：语义化审批决策，可选 `"approve"`（允许一次 / 选第1项）、`"approve_session"`（本会话允许 / 选第2项）、`"deny"`（拒绝）；
    - `option_index`：1-based 选项序号（如 `1`, `2`, `3`），直接选择 `pending_interaction.options` 中的第 N 项；
    - `custom_input`：自定义文本输入（当选择 Other 自由输入、提供拒绝原因反馈或回答开放式问题时传入）。
    """
    session = hub.sessions.get(session_id)
    if not session:
        raise ValueError(f"Session '{session_id}' not found in active sessions.")

    start_seq = hub.global_seq
    action_result = hub.respond_to_interaction(
        session_id=session_id,
        decision=decision,
        option_index=option_index,
        custom_input=custom_input,
        request_id=request_id,
    )

    settle_info: Dict[str, Any] = {"settled": True, "reason": "no_wait"}
    if wait_seconds and wait_seconds > 0 and session.kind in ("tui", "web_gui"):
        async def _progress(elapsed: float, snippet: str):
            if ctx:
                await ctx.report_progress(
                    progress=min(elapsed, wait_seconds),
                    total=wait_seconds,
                    message=f"已向 Sub-Agent [{session_id}] 发送代批/代选指令 ({action_result.get('chosen_label')})，等待继续执行 ({int(elapsed)}s)...",
                )

        settle_info = await hub.wait_for_output_settle(
            session,
            timeout_sec=float(wait_seconds),
            quiet_window_sec=2.5,
            progress_cb=_progress,
        )

    events = hub.get_worker_events(session_id=session_id, after_seq=start_seq)
    next_seq = events[-1]["seq"] if events else start_seq
    transcript_summary = hub.summarize_transcript(session_id)

    return {
        "session_id": session_id,
        "status": session.status,
        "step_count": max(session.step_count, transcript_summary.get("step_count", 0)),
        "current_activity": session.current_activity,
        "pending_interaction": session.pending_interaction,
        "action_result": action_result,
        "worker_events": events,
        "next_seq": next_seq,
        "background_poll_command": hub.build_poll_command(session_id, next_seq),
        "wait_result": settle_info,
        "transcript_summary": transcript_summary,
    }


@mcp.tool()
async def wait_subagent_events(
    session_id: str = "all",
    after_seq: int = 0,
    timeout_seconds: float = 60.0,
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """
    [主 Agent 阻塞监听工具] 阻塞等待指定 Sub-Agent（或 'all' 所有 Sub-Agent）发来新消息（`message_parent`）、
    提问（`ask_parent`）、回合完成（`turn_completed`）、任务完成报告（`report_complete`）或进程退出事件。
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    elapsed = 0.0
    slice_sec = 5.0

    while True:
        rem = deadline - asyncio.get_event_loop().time()
        if rem <= 0:
            break
        res = await hub.poll_worker_events(
            session_id=session_id,
            after_seq=after_seq,
            timeout_sec=min(slice_sec, rem),
        )
        if res.get("events"):
            return res
        elapsed += slice_sec
        if ctx:
            await ctx.report_progress(
                progress=min(elapsed, timeout_seconds),
                total=timeout_seconds,
                message=f"正在监听 Sub-Agent [{session_id}] 的消息与完成事件 ({int(elapsed)}s)...",
            )

    return await hub.poll_worker_events(session_id=session_id, after_seq=after_seq, timeout_sec=0.1)


@mcp.tool()
async def summarize_subagent_transcript(session_id: str) -> Dict[str, Any]:
    """
    [CLI 专属 Transcript 总结器] 针对不同 CLI 应用程序（omp / kimi / codex / agy / dsh）解析其原生结构化会话日志，
    过滤掉所有 TUI 边框、动画 Spinner、状态栏等视觉噪音，仅向主 Agent 返回最核心的思考摘要 (`key_thoughts`)、
    工具调用链 (`recent_actions`)、最新结论 (`latest_conclusion`) 与高信噪比 Markdown 简报 (`digest_markdown`)。
    """
    return hub.summarize_transcript(session_id=session_id)


@mcp.tool()
async def read_subagent_screen(
    session_id: str,
    max_lines: int = 80,
    include_raw_tui: bool = False,
) -> Dict[str, Any]:
    """
    读取指定非无头 Sub-Agent 的状态摘要与去噪后的关键输出行。
    默认 (`include_raw_tui=False`) 自动使用 CLI Transcript 总结器与 TUI 噪音过滤器剔除边框和状态栏，避免干扰主 Agent 注意力；
    仅当显式传入 `include_raw_tui=True` 时才返回原始虚拟终端屏幕字符。
    """
    transcript_summary = hub.summarize_transcript(session_id=session_id)
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
            "final_response": session.final_response or transcript_summary.get("latest_conclusion"),
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

    persisted = hub.db.get_session_record(session_id)
    if not persisted:
        raise ValueError(f"找不到会话 {session_id}")
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
    """列出当前总控台中所有正在运行的 Sub-Agent 会话，以及 SQLite 中持久化的历史已结束会话。"""
    active = [s.to_dict() for s in hub.sessions.values()]
    result: Dict[str, Any] = {
        "global_seq": hub.global_seq,
        "active_count": len(active),
        "active_sessions": active,
    }
    if include_historical:
        # Spec §6: list_active_sessions stays "live + ended-in-memory"; archived records are listed
        # by list_archived_sessions.
        result["persisted_sessions"] = hub.db.list_persisted_sessions(limit=limit, include_archived=False)
    return result


@mcp.tool()
async def terminate_subagent(session_id: str) -> Dict[str, Any]:
    """
    关闭指定的 Sub-Agent 会话进程。
    关闭前会自动将其最终响应 (`final_response`)、终端滚动历史和工具调用轨迹永久保存到 SQLite，
    关闭后主 Agent 依然可以随时调用 `get_subagent_final_response(session_id)` 或 `get_subagent_status_and_trace(session_id)` 查询其结果！
    """
    session = hub.sessions.get(session_id)
    if not session:
        persisted = hub.db.get_session_record(session_id)
        if persisted:
            return {
                "terminated": True,
                "session_id": session_id,
                "note": "Session was already closed; its final_response remains available in SQLite.",
                "final_response": persisted.get("final_response"),
            }
        raise ValueError(f"Session '{session_id}' not found.")

    # K5: the parent agent ended it, so the lifecycle event says 主 Agent, not 用户.
    result = hub.close_session(session_id, source="mcp")
    final_info = hub.get_subagent_final_response(session_id)
    return {
        "terminated": True,
        "session_id": session_id,
        "was_alive": result.get("was_alive"),
        "status": result.get("status"),
        "persisted_in_sqlite": True,
        "final_response": final_info.get("final_response"),
        "final_response_source": final_info.get("final_response_source"),
        "step_count": final_info.get("step_count"),
    }


@mcp.tool()
async def attach_desktop_gui_window(
    window_title_keyword: str,
    tuck_offscreen: bool = True,
) -> Dict[str, Any]:
    """查找宿主机桌面上标题包含指定关键词的原生 GUI 窗口，纳入统一 Web 总控台投屏管控，并可移至屏幕外隐藏。"""
    windows = Win32WindowHelper.list_top_windows()
    matched = [w for w in windows if window_title_keyword.lower() in w["title"].lower()]
    if not matched:
        return {
            "attached": False,
            "message": f"未找到标题包含 '{window_title_keyword}' 的桌面窗口",
            "available_windows": windows[:15],
        }
    target = matched[0]
    sess = await hub.create_session(
        profile_id="desktop-gui",
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


# ============================================================================
# 2. 南向窄权限 MCP Server (/worker-mcp) —— 专门暴露给【Sub-Agent】主动向主 Agent 通信与上报步骤
# ============================================================================
worker_http_mcp = FastMCP(
    "Parent-Agent-Narrow-Bridge",
    instructions=(
        "专门暴露给 Sub-Agent 的窄权限通信与遥测工具集：\n"
        "- `report_step`：上报当前执行步骤、使用的工具或中间消息\n"
        "- `message_parent`：主动向主 Agent 发送进度消息（立即唤醒主 Agent 监听器）\n"
        "- `ask_parent`：向主 Agent 提问并同步阻塞等待回答\n"
        "- `report_complete`：正式向主 Agent 交付最终结果 (`final_response`) 并持久化保存\n"
        "- `poll_parent_instructions`：读取主 Agent 发来的补充指令"
    ),
)


@worker_http_mcp.tool()
async def report_step(
    session_id: str,
    step_title: str,
    tool_name: Optional[str] = None,
    tool_input_summary: Optional[str] = None,
    intermediate_message: Optional[str] = None,
) -> Dict[str, Any]:
    """[Sub-Agent 专用] 上报当前执行步骤、调用的工具或产出的中间结论。"""
    return hub.record_telemetry(
        session_id,
        {
            "source": "worker_mcp",
            "hook_event": "ReportStep",
            "step_title": step_title,
            "tool_name": tool_name,
            "tool_input": tool_input_summary,
            "intermediate_messages": [intermediate_message] if intermediate_message else [],
        },
    )


@worker_http_mcp.tool()
async def message_parent(
    session_id: str,
    message: str,
    category: str = "progress",
) -> Dict[str, Any]:
    """[Sub-Agent 专用] 主动向主 Agent 发送一条非阻塞消息（立即唤醒主 Agent 的后台监听进程）。"""
    rec = hub.record_worker_event(
        session_id,
        {"type": "message", "category": category, "content": message},
    )
    return {"delivered_to_parent": True, "event": rec}


@worker_http_mcp.tool()
async def ask_parent(
    session_id: str,
    question: str,
    timeout_seconds: float = 300.0,
) -> Dict[str, Any]:
    """[Sub-Agent 专用] 向主 Agent 提问并同步阻塞等待主 Agent 回复。"""
    return await hub.ask_parent_and_wait(session_id, question, timeout_seconds)


@worker_http_mcp.tool()
async def report_complete(
    session_id: str,
    summary: str,
    status: str = "success",
    files_changed: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """[Sub-Agent 专用] 向主 Agent 正式宣告当前任务已完成并交付最终总结（永久持久化到 SQLite 并立即唤醒主 Agent）。"""
    rec = hub.record_worker_event(
        session_id,
        {
            "type": "completed",
            "category": "completion",
            "status": status,
            "content": summary,
            "files_changed": files_changed or [],
        },
    )
    return {"reported": True, "event": rec}


@worker_http_mcp.tool()
async def poll_parent_instructions(session_id: str) -> Dict[str, Any]:
    """[Sub-Agent 专用] 读取主 Agent 发给当前 Sub-Agent 的所有补充指令或纠偏消息。"""
    msgs = hub.parent_messages.get(session_id, [])
    unread = [m for m in msgs if not m.get("read_by_worker")]
    for m in unread:
        m["read_by_worker"] = True
    return {"session_id": session_id, "unread_count": len(unread), "unread_messages": unread, "all_messages": msgs}


# Build both ASGI MCP apps and combine lifespans
mcp_parent_app = mcp.http_app(path="/mcp", transport="streamable-http")
mcp_worker_app = worker_http_mcp.http_app(path="/worker-mcp", transport="streamable-http")


@asynccontextmanager
async def combined_lifespan(app_instance: FastAPI):
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(mcp_parent_app.lifespan(app_instance))
        await stack.enter_async_context(mcp_worker_app.lifespan(app_instance))
        yield
        # A6: a graceful hub stop archives every in-memory session — live ones become interrupted
        # (spec §1.5) and stay resumable, ended ones keep their own reason. The manager archives
        # each session in its own try/except, and a failure here never blocks the shutdown.
        try:
            hub.archive_all_sessions_on_shutdown()
        except Exception:
            pass


app = FastAPI(
    title="MCP Non-Headless Sub-Agent Hub (Bidirectional + Telemetry + SQLite)",
    lifespan=combined_lifespan,
)


# ============================================================================
# 3. 遥测与双向消息邮箱 HTTP API
# ============================================================================
@app.post("/api/telemetry/{session_id}")
async def api_record_telemetry(session_id: str, req: Request):
    body = await req.json()
    return hub.record_telemetry(session_id, body)


@app.get("/api/sessions/{session_id}/trace")
async def api_get_session_trace(session_id: str):
    try:
        return hub.get_subagent_status_and_trace(session_id)
    except ValueError as e:
        return not_found(f"找不到会话 {session_id}")


@app.get("/api/sessions/{session_id}/final")
async def api_get_session_final(session_id: str):
    try:
        return hub.get_subagent_final_response(session_id)
    except ValueError as e:
        return not_found(f"找不到会话 {session_id}")


@app.get("/api/history/sessions")
async def api_list_historical_sessions(limit: int = Query(30)):
    return {"sessions": hub.db.list_persisted_sessions(limit=limit)}


@app.get("/api/mailbox/poll")
async def api_mailbox_poll(
    session_id: str = Query("all"),
    after_seq: int = Query(0),
    timeout: float = Query(25.0),
):
    return await hub.poll_worker_events(
        session_id=session_id,
        after_seq=after_seq,
        timeout_sec=min(max(0.1, timeout), 120.0),
    )


@app.get("/api/mailbox/events")
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


@app.post("/api/mailbox/{session_id}/worker-event")
async def api_worker_emit_event(session_id: str, req: Request):
    body = await req.json()
    rec = hub.record_worker_event(session_id, body)
    return {"ok": True, "event": rec}


@app.post("/api/mailbox/{session_id}/worker-ask")
async def api_worker_ask_parent(session_id: str, req: Request):
    body = await req.json()
    question = body.get("question", "")
    timeout_seconds = float(body.get("timeout_seconds", 300.0))
    return await hub.ask_parent_and_wait(session_id, question, timeout_seconds)


@app.get("/api/mailbox/{session_id}/parent-messages")
async def api_get_parent_messages(session_id: str):
    msgs = hub.parent_messages.get(session_id, [])
    unread = [m for m in msgs if not m.get("read_by_worker")]
    for m in unread:
        m["read_by_worker"] = True
    return {"session_id": session_id, "unread_count": len(unread), "unread_messages": unread, "all_messages": msgs}


@app.post("/api/mailbox/{session_id}/reply")
async def api_parent_reply(session_id: str, req: Request):
    body = await req.json()
    text = body.get("text", "")
    request_id = body.get("request_id")
    also_write_pty = bool(body.get("also_write_pty", True))
    return hub.deliver_parent_message(
        session_id=session_id,
        text=text,
        request_id=request_id,
        also_write_pty=also_write_pty,
    )


@app.post("/api/mailbox/{session_id}/respond")
async def api_parent_respond_interaction(session_id: str, req: Request):
    body = await req.json()
    try:
        return hub.respond_to_interaction(
            session_id=session_id,
            decision=body.get("decision"),
            option_index=body.get("option_index"),
            custom_input=body.get("custom_input"),
            request_id=body.get("request_id"),
        )
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
        await asyncio.to_thread(hub.save_global_routing_instructions, text)
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
        "persisted_sessions": hub.db.list_persisted_sessions(limit=50),
        "archive_total": hub.db.count_archived(),
        "recent_events": hub.worker_events[-30:],
        "default_cwd": hub.hub_default_cwd(),
        "hub_port": hub_port(),
    }


# Archive spec §6: the console's archive list, one record, its raw scrollback, revive and delete.
@app.get("/api/archive")
async def api_list_archive(limit: int = Query(50), offset: int = Query(0), q: str = Query("")):
    # R8: a page refreshes up to 200 records with filesystem probes; that must not run on the loop.
    return await asyncio.to_thread(hub.list_archive, limit, offset, q)


@app.get("/api/archive/{session_id}")
async def api_get_archive(session_id: str):
    rec = hub.get_archive_record(session_id)
    if not rec:
        return not_found(f"找不到会话 {session_id}")
    return rec


@app.get("/api/archive/{session_id}/scrollback")
async def api_get_archive_scrollback(session_id: str):
    if hub.db.get_session_record(session_id) is None:
        return not_found(f"找不到会话 {session_id}")
    return PlainTextResponse(hub.get_archive_scrollback(session_id) or "")


@app.delete("/api/archive/{session_id}")
async def api_delete_archive(session_id: str):
    try:
        hub.delete_archive_record(session_id)
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
        result = await hub.resume_subagent(session_id, text=(str(text) if text is not None else None))
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
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    payload = {"ok": True}
    payload.update(preview)
    return payload


@app.get("/api/sessions/{session_id}/transcript-summary")
async def api_session_transcript_summary(session_id: str):
    try:
        return hub.summarize_transcript(session_id)
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
    approval_mode = body.get("approval_mode", "auto")
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
            approval_mode=approval_mode,
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
    hub.deliver_parent_message(session_id=session_id, text=text, also_write_pty=True, press_enter=False)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    """B9: removal never rewrites the final status an ended process already earned.

    K5: this is the console’s own removal (the pane’s 终止 button), so the lifecycle event says the
    user did it; only the MCP terminate_subagent tool passes source="mcp".
    """
    result = hub.close_session(session_id, source="console")
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
                mtype = msg.get("type")
                if mtype == "input" and live is not None:
                    live.write_input(msg.get("data", ""))
                elif mtype == "resize" and live is not None:
                    live.resize(int(msg.get("cols", 120)), int(msg.get("rows", 36)))
            except Exception:
                if live is not None:
                    live.write_input(raw_msg)
    except WebSocketDisconnect:
        pass
    finally:
        sess.detach_ws_queue(q)
        sender_task.cancel()


# Mount both Parent MCP (/mcp) and Narrow Worker MCP (/worker-mcp)
for route in mcp_worker_app.routes:
    app.router.routes.append(route)
for route in mcp_parent_app.routes:
    app.router.routes.append(route)


if __name__ == "__main__":
    port = hub_port()
    # B11: startup discovery runs in a thread, so the first /api/sessions poll answers at once.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
