import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastmcp import Client

sys.stdout.reconfigure(encoding="utf-8")

HUB_DIR = Path(__file__).parent.resolve()
PYTHON_EXE = str(HUB_DIR / ".venv" / "Scripts" / "python.exe")
HUB_POLL_SCRIPT = str(HUB_DIR / "hub_poll.py")
HUB_WORKER_SCRIPT = str(HUB_DIR / "hub_worker.py")
MCP_PARENT_URL = "http://127.0.0.1:4444/mcp"
MCP_WORKER_URL = "http://127.0.0.1:4444/worker-mcp"


def unwrap(res):
    if hasattr(res, "data") and res.data is not None:
        return res.data
    if hasattr(res, "structured_content") and res.structured_content is not None:
        return res.structured_content
    if hasattr(res, "content") and res.content:
        txt = res.content[0].text
        try:
            return json.loads(txt)
        except Exception:
            return txt
    return res


async def test_scenario_1_to_3_bidirectional_and_poll(parent_client: Client, worker_client: Client):
    print("\n" + "=" * 76)
    print("[压力测试 1~3] 双向通信闭环 + 后台阻塞命令 hub_poll.py 实时唤醒 + ask_parent 问答握手")
    print("=" * 76)

    spawn_res = unwrap(
        await parent_client.call_tool(
            "spawn_subagent",
            {
                "agent_type": "pwsh",
                "title": "Stress-Test-Bidirectional-Worker",
                "cwd": str(HUB_DIR),
                "wait_seconds": 1.5,
            },
        )
    )
    sid = spawn_res["session_id"]
    seq0 = spawn_res["current_seq"]
    print(f"  ✓ 已拉起非无头会话: {sid} (初始游标 seq={seq0})")

    # 1. 启动主 Agent 后台阻塞命令 hub_poll.py
    poll_proc = subprocess.Popen(
        [PYTHON_EXE, HUB_POLL_SCRIPT, "--session", sid, "--after-seq", str(seq0), "--timeout", "30"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    # 2. Sub-Agent 调用窄 MCP 工具 report_step 与 message_parent
    await asyncio.sleep(0.4)
    t0 = time.time()
    await worker_client.call_tool(
        "report_step",
        {
            "session_id": sid,
            "step_title": "解析项目依赖与配置文件",
            "tool_name": "ReadFile",
            "tool_input_summary": "agent_profiles.json",
            "intermediate_message": "已成功读取 11 个预置 Agent 引擎配置。",
        },
    )
    await worker_client.call_tool(
        "message_parent",
        {
            "session_id": sid,
            "message": "汇报进度：已完成第 1 阶段配置校验，准备请求端口分配。",
            "category": "progress",
        },
    )

    stdout_data, _ = poll_proc.communicate(timeout=10)
    wake_ms = round((time.time() - t0) * 1000, 1)
    poll_json = json.loads(stdout_data)
    next_seq = poll_json["next_seq"]
    print(f"  ✓ 后台阻塞命令 hub_poll.py 在 {wake_ms} ms 内被立即唤醒并退出！收到事件: {poll_json['events'][0]['content']}")

    # 3. Sub-Agent 调用 ask_parent 同步阻塞提问，主 Agent 通过 send_to_subagent 回答并瞬间解除阻塞
    async def subagent_asks():
        return unwrap(
            await worker_client.call_tool(
                "ask_parent",
                {
                    "session_id": sid,
                    "question": "Sub-Agent 请示：压测报告应该输出为 JSON 还是 Markdown？",
                    "timeout_seconds": 30.0,
                },
            )
        )

    ask_task = asyncio.create_task(subagent_asks())
    await asyncio.sleep(0.5)

    trace_while_waiting = unwrap(
        await parent_client.call_tool("get_subagent_status_and_trace", {"session_id": sid})
    )
    assert trace_while_waiting["lifecycle_status"] == "waiting_parent", f"Expected waiting_parent, got {trace_while_waiting['lifecycle_status']}"
    print(f"  ✓ 主 Agent 查询到 Sub-Agent 状态为 '{trace_while_waiting['lifecycle_status']}'，挂起问题 ID: {trace_while_waiting['pending_question_id']}")

    # 主 Agent 通过 send_to_subagent 直接回复答案
    await parent_client.call_tool(
        "send_to_subagent",
        {
            "session_id": sid,
            "text": "请使用 Markdown 格式输出，并附带表格。",
            "wait_seconds": 0.5,
        },
    )
    ask_result = await asyncio.wait_for(ask_task, timeout=5.0)
    assert ask_result["answered"] is True
    print(f"  ✓ Sub-Agent 的 ask_parent 立即收到主 Agent 回复: '{ask_result['answer']}'")

    # 4. Sub-Agent 调用 report_complete 汇报完成，随后主 Agent 销毁该会话并测试【生命周期后查询】
    await worker_client.call_tool(
        "report_complete",
        {
            "session_id": sid,
            "summary": "## 双向通信压测完成报告\n- 阻塞唤醒延迟: 极低\n- 问答握手: 成功",
            "status": "success",
            "files_changed": ["stress_test_suite.py"],
        },
    )
    await parent_client.call_tool("terminate_subagent", {"session_id": sid})

    # 会话已从内存中销毁，验证从 SQLite 中查询 final_response 与完整工具轨迹
    post_final = unwrap(await parent_client.call_tool("get_subagent_final_response", {"session_id": sid}))
    post_trace = unwrap(await parent_client.call_tool("get_subagent_status_and_trace", {"session_id": sid}))
    assert post_trace["is_alive_in_memory"] is False and len(post_trace["recent_tool_calls"]) >= 1
    assert "双向通信压测完成报告" in post_final["final_response"]
    print(
        f"  ✓ [生命周期结束后的持久化验证] 会话 {sid} 已销毁 (is_alive={post_trace['is_alive_in_memory']})，"
        f"仍成功从 SQLite 查出 step_count={post_trace['step_count']}、工具记录 {len(post_trace['recent_tool_calls'])} 条、"
        f"中间消息 {len(post_trace['intermediate_messages'])} 条，以及完整 final_response (来源: {post_final['final_response_source']})！"
    )


async def run_real_cli_agent_stress_test(parent_client: Client):
    print("\n" + "=" * 76)
    print("[压力测试 4~5] 真实多 CLI Agent 并发非无头拉起 + 原生轨迹抓取 + 跨生命周期 Final Response 查询")
    print("测试阵容: omp (dsflash low & high) | kimi (k3.1-pro low) | agy (gemini-3.8-flash-low) | codex")
    print("=" * 76)

    test_cases = [
        {
            "agent_type": "omp-dsflash-low",
            "title": "OMP-DSFlash-Low-Stress",
            "marker": "OMP_LOW_OK_901",
            "task": (
                f'Run a bash/shell command to execute: `{PYTHON_EXE} "{HUB_WORKER_SCRIPT}" step "OMP-Low checking files" --tool "bash"` '
                f'and then `{PYTHON_EXE} "{HUB_WORKER_SCRIPT}" complete "OMP_LOW_OK_901: Verified workspace has server.py and session_manager.py"`. '
                f'Finally reply with OMP_LOW_OK_901.'
            ),
        },
        {
            "agent_type": "omp-dsflash-high",
            "title": "OMP-DSFlash-High-Stress",
            "marker": "OMP_HIGH_OK_902",
            "task": (
                f'Run a bash/shell command to execute: `{PYTHON_EXE} "{HUB_WORKER_SCRIPT}" step "OMP-High analyzing hub" --tool "bash"` '
                f'and then `{PYTHON_EXE} "{HUB_WORKER_SCRIPT}" complete "OMP_HIGH_OK_902: DeepSeek Flash High completed verification"`. '
                f'Finally reply with OMP_HIGH_OK_902.'
            ),
        },
        {
            "agent_type": "kimi-k3.1-pro-low",
            "title": "Kimi-K3.1-Pro-Low-Stress",
            "marker": "KIMI_K31_OK_903",
            "task": (
                f"Run a shell command: {PYTHON_EXE} {HUB_WORKER_SCRIPT} complete KIMI_K31_OK_903"
            ),
        },
        {
            "agent_type": "agy-flash",
            "title": "AGY-Flash-Stress",
            "marker": "AGY_FLASH_OK_904",
            "task": (
                f'Run the command `{PYTHON_EXE} "{HUB_WORKER_SCRIPT}" complete "AGY_FLASH_OK_904: AGY CLI finished task"` '
                f'and reply with AGY_FLASH_OK_904.'
            ),
        },
        {
            "agent_type": "codex",
            "title": "Codex-CLI-Stress",
            "marker": "CODEX_OK_905",
            "task": (
                f'Run `{PYTHON_EXE} "{HUB_WORKER_SCRIPT}" complete "CODEX_OK_905: Codex CLI finished task"` '
                f'and reply with CODEX_OK_905.'
            ),
        },
    ]

    # 并发拉起所有 5 个真实非无头 CLI Agent！
    spawn_coros = [
        parent_client.call_tool(
            "spawn_subagent",
            {
                "agent_type": tc["agent_type"],
                "title": tc["title"],
                "task": tc["task"],
                "cwd": str(HUB_DIR),
                "wait_seconds": 2.0,
            },
        )
        for tc in test_cases
    ]
    spawned_raw = await asyncio.gather(*spawn_coros)
    spawned = [unwrap(r) for r in spawned_raw]

    session_map = {}
    for tc, sp in zip(test_cases, spawned):
        sid = sp["session_id"]
        session_map[sid] = tc
        print(f"  🚀 已并发拉起 [{tc['agent_type']}] -> session_id={sid} | 控制台: {sp['console_url']}")

    # 轮询观察各 Sub-Agent 的实时进度、工具调用轨迹与完成情况（最长等待 95 秒）
    start_time = time.time()
    completed_sids = set()

    while (time.time() - start_time) < 95.0 and len(completed_sids) < len(session_map):
        await asyncio.sleep(4.0)
        elapsed = int(time.time() - start_time)
        status_lines = []
        for sid, tc in session_map.items():
            trace = unwrap(
                await parent_client.call_tool(
                    "get_subagent_status_and_trace",
                    {"session_id": sid, "recent_tools_limit": 5, "recent_messages_limit": 5},
                )
            )
            st = trace["lifecycle_status"]
            steps = trace["step_count"]
            tools_cnt = len(trace["recent_tool_calls"])
            msgs_cnt = len(trace["intermediate_messages"])
            f_resp = trace.get("final_response") or ""
            f_src = trace.get("final_response_source") or ""

            if st == "completed" or (f_src != "terminal_turn_snapshot" and tc["marker"] in f_resp):
                completed_sids.add(sid)

            status_lines.append(
                f"{tc['agent_type']}({sid}): status={st}, step={steps}, tools={tools_cnt}, msgs={msgs_cnt}"
            )
        print(f"  [{elapsed}s 实时监控] " + " | ".join(status_lines))

    print("\n" + "-" * 76)
    print("开始销毁所有压测 Sub-Agent 进程，并验证【生命周期结束后的 SQLite 持久化查询】：")
    print("-" * 76)

    summary_rows = []
    for sid, tc in session_map.items():
        # 先抓取一次实时状态，然后调用 terminate_subagent 销毁进程
        await parent_client.call_tool("terminate_subagent", {"session_id": sid})

        # 进程已彻底销毁，现在通过 MCP 查询它的历史轨迹和 final_response
        trace_after = unwrap(
            await parent_client.call_tool("get_subagent_status_and_trace", {"session_id": sid})
        )
        final_after = unwrap(
            await parent_client.call_tool("get_subagent_final_response", {"session_id": sid})
        )

        f_text = (final_after.get("final_response") or "").strip()
        f_preview = f_text.replace("\n", " ")[:110]
        tools_list = [t["tool_name"] for t in trace_after.get("recent_tool_calls", [])]

        row = {
            "agent_type": tc["agent_type"],
            "session_id": sid,
            "alive_after_terminate": trace_after["is_alive_in_memory"],
            "steps_recorded": trace_after["step_count"],
            "tools_recorded": tools_list,
            "intermediate_msgs_count": len(trace_after.get("intermediate_messages", [])),
            "final_response_source": final_after.get("final_response_source"),
            "marker_matched": (tc["marker"] in f_text) or (tc["marker"] in (final_after.get("scrollback_tail") or "")),
            "final_preview": f_preview,
        }
        summary_rows.append(row)
        print(
            f"  ✓ [{row['agent_type']}] ({sid})\n"
            f"    - 销毁后内存存活: {row['alive_after_terminate']} | SQLite 持久化步数: {row['steps_recorded']} | 记录工具: {row['tools_recorded']}\n"
            f"    - 中间消息数: {row['intermediate_msgs_count']} | Final 来源: {row['final_response_source']} | 校验标记命中: {row['marker_matched']}\n"
            f"    - Final 摘要: {row['final_preview']}"
        )

    return summary_rows


async def main():
    async with Client(MCP_PARENT_URL) as parent_client, Client(MCP_WORKER_URL) as worker_client:
        await test_scenario_1_to_3_bidirectional_and_poll(parent_client, worker_client)
        await run_real_cli_agent_stress_test(parent_client)


if __name__ == "__main__":
    asyncio.run(main())
