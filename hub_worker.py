#!/usr/bin/env python3
"""
Sub-Agent 窄权限通信 CLI 桥接工具 (Universal Worker-to-Parent CLI Bridge).
与 worker_bridge_mcp.py 共享同一套网关 Mailbox & Telemetry 后端。
由于网关在通过 ConPTY 拉起任何非无头 Sub-Agent 时都会自动注入环境变量 HUB_SESSION_ID 和 HUB_API_URL，
Sub-Agent 既可以通过 MCP 工具调用，也可以直接执行本脚本与主 Agent 进行双向通信：

  python hub_worker.py step "正在扫描目录结构" --tool "glob" --msg "已找到 12 个模块"
  python hub_worker.py message "阶段性汇报：已完成核心模块解析"
  python hub_worker.py ask "发现两个配置版本，应该以 v1 还是 v2 为准？"
  python hub_worker.py complete "任务全部完成，最终结论如下：..."
  python hub_worker.py poll
"""
import argparse
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def post_json(url: str, payload: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Sub-Agent to Parent Agent Bridge CLI")
    parser.add_argument("action", choices=["step", "message", "ask", "complete", "poll"])
    parser.add_argument("text", nargs="?", default="", help="Message / Question / Step title / Completion summary")
    parser.add_argument("--session", default=os.environ.get("HUB_SESSION_ID", ""), help="Session ID (defaults to $HUB_SESSION_ID)")
    parser.add_argument("--hub-url", default=os.environ.get("HUB_API_URL", "http://127.0.0.1:4444"), help="Hub URL")
    parser.add_argument("--tool", default=None, help="Tool name for 'step'")
    parser.add_argument("--input", default=None, help="Tool input summary for 'step'")
    parser.add_argument("--msg", default=None, help="Intermediate message for 'step'")
    parser.add_argument("--timeout", type=float, default=300.0, help="Timeout seconds for 'ask'")
    args = parser.parse_args()

    sid = args.session.strip()
    if not sid:
        print(json.dumps({"error": "Missing session_id and HUB_SESSION_ID env var is not set."}, ensure_ascii=False))
        sys.exit(1)

    base = args.hub_url.rstrip("/")

    if args.action == "step":
        res = post_json(
            f"{base}/api/telemetry/{sid}",
            {
                "source": "worker_cli",
                "hook_event": "ReportStep",
                "step_title": args.text,
                "tool_name": args.tool,
                "tool_input": args.input or args.text,
                "intermediate_messages": [args.msg] if args.msg else ([args.text] if args.text else []),
            },
        )
        print(json.dumps(res, ensure_ascii=False))

    elif args.action == "message":
        res = post_json(
            f"{base}/api/mailbox/{sid}/worker-event",
            {
                "type": "message",
                "category": "progress",
                "content": args.text,
            },
        )
        print(json.dumps(res, ensure_ascii=False))

    elif args.action == "ask":
        res = post_json(
            f"{base}/api/mailbox/{sid}/worker-ask",
            {
                "question": args.text,
                "timeout_seconds": args.timeout,
            },
            timeout=args.timeout + 15.0,
        )
        print(json.dumps(res, ensure_ascii=False))

    elif args.action == "complete":
        res = post_json(
            f"{base}/api/mailbox/{sid}/worker-event",
            {
                "type": "completed",
                "status": "success",
                "content": args.text,
            },
        )
        print(json.dumps(res, ensure_ascii=False))

    elif args.action == "poll":
        res = get_json(f"{base}/api/mailbox/{sid}/parent-messages")
        print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
