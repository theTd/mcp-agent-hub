#!/usr/bin/env python3
"""
主 Agent 后台阻塞监听器 (Parent Agent Blocking Poll CLI).

用法示例（主 Agent 在后台运行此命令，当 Sub-Agent 发来消息、提问或完成任务时，此命令会立即打印 JSON 并退出，从而第一时间唤醒主 Agent）：
  python hub_poll.py --session <session_id> --after-seq 0 --timeout 1800
  python hub_poll.py --session all --after-seq 0 --timeout 1800

K3: 这里不要再写死 <repo>/.venv 的解释器路径。后台轮询命令由 session_manager.build_poll_command()
用 sys.executable（Hub 自己运行时用的那个解释器）拼出来，工作树里没有 .venv 也能用。
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Blocking long-poll waiter for Sub-Agent events")
    parser.add_argument("--session", default="all", help="Sub-Agent session_id (or 'all' for any active session)")
    parser.add_argument("--after-seq", type=int, default=0, help="Only return events with seq > after_seq")
    parser.add_argument("--timeout", type=float, default=1800.0, help="Max total seconds to block before returning")
    parser.add_argument("--hub-url", default=os.environ.get("HUB_API_URL", "http://127.0.0.1:4444"), help="Hub base URL")
    args = parser.parse_args()

    deadline = time.time() + args.timeout
    slice_sec = 25.0  # Each HTTP long-poll slice is at most 25s to avoid socket idle drops

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            out = {
                "woke_reason": "timeout",
                "session": args.session,
                "after_seq": args.after_seq,
                "events": [],
                "next_seq": args.after_seq,
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            sys.exit(0)

        wait_slice = min(slice_sec, max(1.0, remaining))
        params = urllib.parse.urlencode({
            "session_id": args.session,
            "after_seq": args.after_seq,
            "timeout": round(wait_slice, 1),
        })
        url = f"{args.hub_url}/api/mailbox/poll?{params}"

        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=wait_slice + 10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                events = data.get("events", [])
                if events:
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                    sys.exit(0)
        except KeyboardInterrupt:
            sys.exit(130)
        except Exception as e:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
