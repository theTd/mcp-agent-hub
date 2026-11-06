import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from contextlib import AsyncExitStack, asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastmcp import Context, FastMCP
import uvicorn

from session_manager import HubSessionManager, Win32WindowHelper
from transcript_summarizer import sanitize_tui_screen_text

BASE_DIR = Path(__file__).parent.resolve()
PROFILES_PATH = BASE_DIR / "agent_profiles.json"
STATIC_INDEX = BASE_DIR / "static" / "index.html"

hub = HubSessionManager(PROFILES_PATH)

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
    hub.load_profiles(refresh_discovery=refresh_discovery)
    return {
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
        "global_routing_instructions": hub.global_routing_instructions,
        "hub_console_url": "http://127.0.0.1:4444/",
        "worker_mcp_http_url": "http://127.0.0.1:4444/worker-mcp",
        "worker_cli_script": str(BASE_DIR / "hub_worker.py"),
        "engines_catalog": hub.engines_catalog,
        "profiles": hub.get_enabled_profiles(),
        "disabled_profiles": [k for k, v in hub.profiles.items() if not v.get("enabled", True)],
    }


@mcp.tool()
async def discover_agent_models_and_thinking_levels(refresh: bool = True) -> Dict[str, Any]:
    """
    [自动探测模型与思考等级] 实时扫描宿主机上安装的所有 Sub-Agent 引擎（omp / kimi / agy / codex / dsh），
    返回每个引擎当前已启用的模型列表 (`available_models`)、已禁用的模型列表 (`disabled_models`)、全部思考等级 (`available_thinking_levels`)，
    以及用户编写的 `routing_guide_markdown`（指示何时调用何种 Agent 与模型）。
    主 Agent 可将 `available_models` 中的任意 `model` 和 `thinking_level` 直接传入 `spawn_subagent(agent_type, task, model=..., thinking_level=...)`。
    """
    hub.load_profiles(refresh_discovery=refresh)
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
    hub.load_profiles()
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
        "console_url": f"http://127.0.0.1:4444/?session={session.session_id}",
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
    session = hub.sessions.get(session_id)
    if not session:
        raise ValueError(f"Session '{session_id}' is not active in memory. Use get_subagent_final_response('{session_id}') to read its persisted result.")

    session.last_task = text
    start_seq = hub.global_seq

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
        "step_count": max(session.step_count, transcript_summary.get("step_count", 0)),
        "current_activity": session.current_activity,
        "delivery": delivery,
        "worker_events": events,
        "next_seq": next_seq,
        "background_poll_command": hub.build_poll_command(session_id, next_seq),
        "wait_result": settle_info,
        "transcript_summary": transcript_summary,
    }


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
            "sanitized_screen_lines": sanitize_tui_screen_text(raw_txt, max_lines=25),
        }
        if include_raw_tui:
            res["raw_screen_text"] = raw_txt
        return res

    persisted = hub.db.get_session_record(session_id)
    if not persisted:
        raise ValueError(f"Session '{session_id}' not found.")
    raw_txt = "\n".join((persisted.get("scrollback_text") or "").splitlines()[-max_lines:])
    res = {
        "session_id": session_id,
        "profile_id": persisted.get("profile_id"),
        "status": persisted.get("status"),
        "step_count": max(persisted.get("step_count", 0), transcript_summary.get("step_count", 0)),
        "current_activity": persisted.get("current_activity", ""),
        "final_response": persisted.get("final_response") or transcript_summary.get("latest_conclusion"),
        "transcript_summary": transcript_summary,
        "sanitized_screen_lines": sanitize_tui_screen_text(raw_txt, max_lines=25),
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
        result["persisted_sessions"] = hub.db.list_persisted_sessions(limit=limit)
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

    session.terminate()
    hub.persist_session(session)
    final_info = hub.get_subagent_final_response(session_id)
    del hub.sessions[session_id]
    return {
        "terminated": True,
        "session_id": session_id,
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
        "console_url": f"http://127.0.0.1:4444/?session={sess.session_id}",
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
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/sessions/{session_id}/final")
async def api_get_session_final(session_id: str):
    try:
        return hub.get_subagent_final_response(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
async def serve_console():
    return HTMLResponse(STATIC_INDEX.read_text(encoding="utf-8"))


@app.get("/api/profiles")
async def api_list_profiles(refresh: bool = Query(False)):
    hub.load_profiles(refresh_discovery=refresh)
    return {
        "profiles": hub.profiles,
        "engines_catalog": hub.engines_catalog,
        "global_routing_instructions": hub.global_routing_instructions,
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
    }


@app.post("/api/profiles")
async def api_save_profile(req: Request):
    body = await req.json()
    try:
        saved = hub.save_or_update_profile(body)
        return {
            "ok": True,
            "profile": saved,
            "profiles": hub.profiles,
            "engines_catalog": hub.engines_catalog,
            "routing_guide_markdown": hub.build_routing_guide_markdown(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/profiles/{profile_id}")
async def api_delete_profile(profile_id: str):
    deleted = hub.delete_profile(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    return {
        "ok": True,
        "profiles": hub.profiles,
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
    }


@app.post("/api/engines/{engine}/models/toggle")
async def api_toggle_engine_model(engine: str, req: Request):
    body = await req.json()
    model_id = body.get("model") or body.get("model_id") or ""
    enabled = bool(body.get("enabled", True))
    note = body.get("note")
    try:
        eng_info = hub.toggle_engine_model(engine=engine, model_id=model_id, enabled=enabled, note=note)
        return {
            "ok": True,
            "engine": engine,
            "engine_info": eng_info,
            "profiles": hub.profiles,
            "engines_catalog": hub.engines_catalog,
            "routing_guide_markdown": hub.build_routing_guide_markdown(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/routing-guide")
async def api_save_routing_guide(req: Request):
    body = await req.json()
    instructions = body.get("global_routing_instructions", "")
    saved_text = hub.save_global_routing_instructions(instructions)
    return {
        "ok": True,
        "global_routing_instructions": saved_text,
        "routing_guide_markdown": hub.build_routing_guide_markdown(),
    }


@app.get("/api/sessions")
async def api_list_sessions():
    return {
        "global_seq": hub.global_seq,
        "sessions": [s.to_dict() for s in hub.sessions.values()],
        "persisted_sessions": hub.db.list_persisted_sessions(limit=20),
        "recent_events": hub.worker_events[-30:],
    }


@app.get("/api/sessions/{session_id}/transcript-summary")
async def api_session_transcript_summary(session_id: str):
    try:
        return hub.summarize_transcript(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    return {"session": sess.to_dict()}


@app.post("/api/sessions/{session_id}/input")
async def api_send_input(session_id: str, req: Request):
    sess = hub.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    body = await req.json()
    text = body.get("text", "")
    hub.deliver_parent_message(session_id=session_id, text=text, also_write_pty=True, press_enter=False)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    sess = hub.sessions.get(session_id)
    if sess:
        sess.terminate()
        hub.persist_session(sess)
        del hub.sessions[session_id]
    return {"ok": True}


@app.get("/api/win32/windows")
async def api_list_win32_windows():
    return {"windows": Win32WindowHelper.list_top_windows()}


@app.get("/api/sessions/{session_id}/frame")
async def api_get_window_frame(session_id: str):
    sess = hub.sessions.get(session_id)
    if not sess or not sess.hwnd:
        raise HTTPException(status_code=404, detail="GUI session not found")
    frame = Win32WindowHelper.capture_window_jpeg_b64(sess.hwnd)
    if not frame:
        raise HTTPException(status_code=410, detail="Window closed or unavailable")
    return frame


@app.post("/api/sessions/{session_id}/tuck")
async def api_tuck_window(session_id: str, req: Request):
    sess = hub.sessions.get(session_id)
    if not sess or not sess.hwnd:
        raise HTTPException(status_code=404, detail="GUI session not found")
    body = await req.json()
    tuck = bool(body.get("tuck", True))
    Win32WindowHelper.tuck_window_offscreen(sess, tuck)
    return {"ok": True, "tucked_offscreen": sess.tucked_offscreen}


@app.post("/api/sessions/{session_id}/click")
async def api_click_window(session_id: str, req: Request):
    sess = hub.sessions.get(session_id)
    if not sess or not sess.hwnd:
        raise HTTPException(status_code=404, detail="GUI session not found")
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

    if sess.raw_history:
        await websocket.send_text("".join(sess.raw_history))

    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    sess.ws_queues.add(q)

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
            try:
                msg = json.loads(raw_msg)
                mtype = msg.get("type")
                if mtype == "input":
                    sess.write_input(msg.get("data", ""))
                elif mtype == "resize":
                    sess.resize(int(msg.get("cols", 120)), int(msg.get("rows", 36)))
            except Exception:
                sess.write_input(raw_msg)
    except WebSocketDisconnect:
        pass
    finally:
        sess.ws_queues.discard(q)
        sender_task.cancel()


# Mount both Parent MCP (/mcp) and Narrow Worker MCP (/worker-mcp)
for route in mcp_worker_app.routes:
    app.router.routes.append(route)
for route in mcp_parent_app.routes:
    app.router.routes.append(route)


if __name__ == "__main__":
    port = int(os.environ.get("HUB_PORT", "4444"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
