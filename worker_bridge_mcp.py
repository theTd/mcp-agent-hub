import os
from typing import List, Optional
import httpx
from fastmcp import FastMCP

HUB_API_URL = os.environ.get("HUB_API_URL", "http://127.0.0.1:4444")
DEFAULT_SESSION_ID = os.environ.get("HUB_SESSION_ID", "")

worker_mcp = FastMCP(
    "Parent-Agent-Bridge",
    instructions=(
        "这是专门暴露给当前 Sub-Agent 的窄权限通信与遥测桥接工具（Worker-to-Parent Bridge）。\n"
        "当你作为 Sub-Agent 执行任务时：\n"
        "1. 可调用 `report_step` 记录当前执行到了哪一步、使用了什么工具或产出了什么中间结论；\n"
        "2. 遇到需要主 Agent 拍板或补充信息的问题时，调用 `ask_parent` 提问并同步等待主 Agent 回复；\n"
        "3. 有重要阶段性进展需要立刻通知主 Agent 时，调用 `message_parent`；\n"
        "4. 任务彻底完成时，务必调用 `report_complete` 将最终结果 (`final_response`) 正式交付给主 Agent（结果会持久化保存，且立即唤醒主 Agent 的后台监听进程）。"
    ),
)


def _resolve_session_id(session_id: Optional[str]) -> str:
    sid = (session_id or DEFAULT_SESSION_ID or "").strip()
    if not sid:
        raise ValueError(
            "未提供 session_id 且环境变量 HUB_SESSION_ID 为空。请传入当前会话的 session_id。"
        )
    return sid


@worker_mcp.tool()
async def report_step(
    step_title: str,
    tool_name: Optional[str] = None,
    tool_input_summary: Optional[str] = None,
    intermediate_message: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """
    [Sub-Agent 专用] 上报当前正在执行的步骤、调用的工具或中间分析结论，供主 Agent 随时通过 get_subagent_status_and_trace 查询进度。
    """
    sid = _resolve_session_id(session_id)
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{HUB_API_URL}/api/telemetry/{sid}",
            json={
                "source": "worker_mcp",
                "hook_event": "ReportStep",
                "step_title": step_title,
                "tool_name": tool_name,
                "tool_input": tool_input_summary,
                "intermediate_messages": [intermediate_message] if intermediate_message else [],
            },
        )
        r.raise_for_status()
        return r.json()


@worker_mcp.tool()
async def message_parent(
    message: str,
    category: str = "progress",
    session_id: Optional[str] = None,
) -> dict:
    """
    [Sub-Agent 专用] 主动向主 Agent (Parent Agent) 发送一条非阻塞消息（如阶段性进度、重要发现、告警信息）。
    发送后会立即唤醒主 Agent 在后台挂起的 poll 监听进程，同时写入中间消息历史。
    """
    sid = _resolve_session_id(session_id)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{HUB_API_URL}/api/mailbox/{sid}/worker-event",
            json={
                "type": "message",
                "category": category,
                "content": message,
            },
        )
        r.raise_for_status()
        return r.json()


@worker_mcp.tool()
async def ask_parent(
    question: str,
    timeout_seconds: float = 300.0,
    session_id: Optional[str] = None,
) -> dict:
    """
    [Sub-Agent 专用] 向主 Agent (Parent Agent) 提出问题，并【同步阻塞等待】主 Agent 的回复。
    调用此工具会立刻唤醒主 Agent 的后台阻塞监听器；当主 Agent 调用 reply_to_subagent 或 send_to_subagent 回复后，
    本工具会立即返回主 Agent 的回答内容。
    """
    sid = _resolve_session_id(session_id)
    async with httpx.AsyncClient(timeout=timeout_seconds + 15.0) as client:
        r = await client.post(
            f"{HUB_API_URL}/api/mailbox/{sid}/worker-ask",
            json={
                "question": question,
                "timeout_seconds": timeout_seconds,
            },
        )
        r.raise_for_status()
        return r.json()


@worker_mcp.tool()
async def report_complete(
    summary: str,
    status: str = "success",
    files_changed: Optional[List[str]] = None,
    session_id: Optional[str] = None,
) -> dict:
    """
    [Sub-Agent 专用] 当你完成主 Agent 交代的任务时，调用此工具正式向主 Agent 交付最终总结报告 (final_response)。
    该结果会永久持久化到网关 SQLite 数据库（即使当前 Sub-Agent 进程退出后主 Agent 依然可以随时查询），并立即唤醒主 Agent 的后台监听命令。
    """
    sid = _resolve_session_id(session_id)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{HUB_API_URL}/api/mailbox/{sid}/worker-event",
            json={
                "type": "completed",
                "status": status,
                "content": summary,
                "files_changed": files_changed or [],
            },
        )
        r.raise_for_status()
        return r.json()


@worker_mcp.tool()
async def poll_parent_instructions(
    session_id: Optional[str] = None,
) -> dict:
    """
    [Sub-Agent 专用] 检查主 Agent 是否向你的邮箱发送了新的补充指令或中途纠偏消息（Steering Messages）。
    """
    sid = _resolve_session_id(session_id)
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{HUB_API_URL}/api/mailbox/{sid}/parent-messages")
        r.raise_for_status()
        return r.json()


if __name__ == "__main__":
    worker_mcp.run(transport="stdio")
