import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from fastmcp import Client
import httpx

sys.stdout.reconfigure(encoding="utf-8")

HUB_DIR = Path(__file__).parent.resolve()
# K3: the interpreter this suite runs on, not a guessed <repo>/.venv (a worktree has none).
PYTHON_EXE = sys.executable
MCP_PARENT_URL = "http://127.0.0.1:4444/mcp"


def unwrap(res):
    if hasattr(res, "data") and res.data is not None:
        return res.data
    if hasattr(res, "content") and res.content:
        return json.loads(res.content[0].text)
    return res


async def main():
    print("=" * 78)
    print("[DSH 原生融合实机测试] 宿主内会话 + message_parent 阻塞提问唤醒 + 多轮 Continue + SQLite 持久化")
    print("=" * 78)

    async with Client(MCP_PARENT_URL) as parent_client:
        active_info = unwrap(await parent_client.call_tool("list_active_sessions", {}))
        base_seq = active_info["global_seq"]

        # 1. 通过 MCP spawn_subagent 拉起 dsh-flash-high，要求它先调用内置 message_parent(wait_for_reply=True) 提问
        task_prompt = (
            "Please call your `message_parent` tool with `message='What secret code should I include?'` and `wait_for_reply=true`. "
            "Once you receive the reply from the parent, include that exact secret code in your final response."
        )
        spawned = unwrap(
            await parent_client.call_tool(
                "spawn_subagent",
                {
                    "agent_type": "dsh-flash-high",
                    "title": "DSH-Native-Bridge-Test",
                    "task": task_prompt,
                    "cwd": str(HUB_DIR),
                    "wait_seconds": 1.5,
                },
            )
        )
        sid = spawned["session_id"]
        print(f"  🚀 已拉起 DSH 宿主内会话: {sid} | 控制台: {spawned['console_url']}")

        # 2. 重复同一个 wait URL，直到服务端把状态提升为 ask。
        async with httpx.AsyncClient(timeout=50.0) as http:
            poll_data = await http.get(spawned["wait_url"])
            poll_data.raise_for_status()
            poll_data = poll_data.json()
        assert poll_data["state"] == "ask", poll_data
        q_ev = poll_data["requests"][0]
        req_id = q_ev["request_id"]
        print(f"  ✓ wait URL returned question (request_id={req_id}): {q_ev.get('content', '')}")

        # 3. 主 Agent 调用统一 respond_to_subagent 回答 DSH Worker 的提问
        reply_res = unwrap(
            await parent_client.call_tool(
                "respond_to_subagent",
                {
                    "session_id": sid,
                    "request_id": req_id,
                    "answer": "DSH_SECRET_2026_OK",
                },
            )
        )
        print(f"  ✓ 主 Agent 已通过 respond_to_subagent 回复答案 'DSH_SECRET_2026_OK': {reply_res}")

        # 5. 等待第一轮完成
        for _ in range(30):
            await asyncio.sleep(1.0)
            trace = unwrap(await parent_client.call_tool("get_subagent_status_and_trace", {"session_id": sid}))
            if trace["lifecycle_status"] == "completed":
                break

        trace1 = unwrap(await parent_client.call_tool("get_subagent_status_and_trace", {"session_id": sid}))
        print(
            f"  ✓ 第一轮完成！状态={trace1['lifecycle_status']} | 步数={trace1['step_count']} | "
            f"工具={[t['tool_name'] for t in trace1['recent_tool_calls']]} | "
            f"Final={trace1['final_response']}"
        )
        assert "DSH_SECRET_2026_OK" in (trace1["final_response"] or ""), f"第一轮结果未包含密钥: {trace1['final_response']}"

        # 6. 测试多轮热会话续接 (send_to_subagent -> /_dsh/dsh-crew/jobs/:id/continue)
        print("  ↻ 测试向已完成的 DSH 会话发送追问 (send_to_subagent -> 热续接同一 DSH Session)...")
        await parent_client.call_tool(
            "send_to_subagent",
            {
                "session_id": sid,
                "text": "What was the exact secret code I gave you in the previous turn? Reply with ONLY the code and `_CONTINUED` appended.",
                "wait_seconds": 1.0,
            },
        )
        await asyncio.sleep(1.0)
        for _ in range(30):
            await asyncio.sleep(1.0)
            trace2 = unwrap(await parent_client.call_tool("get_subagent_status_and_trace", {"session_id": sid}))
            if trace2["lifecycle_status"] == "completed" and "_CONTINUED" in (trace2["final_response"] or ""):
                break

        trace2 = unwrap(await parent_client.call_tool("get_subagent_status_and_trace", {"session_id": sid}))
        print(f"  ✓ 第二轮 Continue 完成！Final={trace2['final_response']}")
        assert "DSH_SECRET_2026_OK_CONTINUED" in (trace2["final_response"] or ""), f"第二轮未记住上下文: {trace2['final_response']}"

        # 7. 销毁会话并验证跨生命周期 SQLite 查询
        await parent_client.call_tool("terminate_subagent", {"session_id": sid})
        final_after = unwrap(await parent_client.call_tool("get_subagent_final_response", {"session_id": sid}))
        trace_after = unwrap(await parent_client.call_tool("get_subagent_status_and_trace", {"session_id": sid}))
        assert trace_after["is_alive_in_memory"] is False
        assert "DSH_SECRET_2026_OK_CONTINUED" in (final_after["final_response"] or "")
        print(
            f"  ✓ [销毁后 SQLite 持久化验证通过] 内存存活={trace_after['is_alive_in_memory']} | "
            f"持久化步数={trace_after['step_count']} | Final 来源={final_after['final_response_source']} | "
            f"Final={final_after['final_response']}"
        )
        print("  === DSH 原生融合测试 100% 通过！ ===")


if __name__ == "__main__":
    asyncio.run(main())
