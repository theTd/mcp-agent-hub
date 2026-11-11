#!/usr/bin/env python3
"""
主 Agent 后台阻塞监听器 (Parent Agent Blocking Poll CLI).

用法示例（仅在宿主能发送后台完成通知时后台运行；每次收到一批事件就打印 JSON 并退出，任务未结束时需按返回参数续等）：
  python hub_poll.py --session <session_id> --after-seq 0 --timeout 1800
  python hub_poll.py --session <id1>,<id2>,<id3> --after-seq 0 --timeout 1800
  python hub_poll.py --session all --after-seq 0 --timeout 1800

K3: 这里不要再写死 <repo>/.venv 的解释器路径。后台轮询命令由 session_manager.build_poll_command()
用 sys.executable（Hub 自己运行时用的那个解释器）拼出来，工作树里没有 .venv 也能用。
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

from agent_guidance import CONTINUATION_HINT
from parent_waiting import session_targets


def poll_events(session_id, after_seq, timeout, hub_url):
    """Return one batch or an explicit failure; never hide transport failure as idle time."""
    session_targets(session_id)
    if after_seq < 0 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("after_seq must be nonnegative and timeout must be finite and positive.")
    deadline = time.monotonic() + timeout
    cur_seq = after_seq
    first_slice = True
    failures = 0
    last_error = None
    last_result = {}

    def finish(reason, error=None):
        arguments = last_result.get("next_wait_arguments") or {
            "session_id": session_id, "after_seq": cur_seq, "timeout_seconds": 60.0,
        }
        # Retain the server's state and continuation fields even on a quiet timeout.
        result = {
            **last_result,
            "woke_reason": reason,
            "session_id": session_id,
            "session": session_id,  # Legacy CLI alias.
            "after_seq": after_seq,
            "next_seq": cur_seq,
            "events": [],
            "pending_interactions": last_result.get("pending_interactions", []),
            "delivery_updates": last_result.get("delivery_updates", []),
            "results": last_result.get("results", []),
            "next_wait_arguments": arguments,
            "continuation_hint": CONTINUATION_HINT,
        }
        if error:
            result.update(error=error, next_action="retry_wait", wait_required=True,
                          next_wait_arguments=arguments,
                          continuation_hint="The listener failed. Check Hub connectivity/version before retrying next_wait_arguments. Delegated tasks may still be running.")
        return result

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_error or not last_result:
                return finish("poll_error", last_error or "No successful Hub response before the deadline.")
            return finish("timeout")

        wait_slice = min(1.0 if first_slice else 25.0, remaining)
        first_slice = False
        params = urllib.parse.urlencode({
            "session_id": session_id,
            "after_seq": cur_seq,
            "timeout": round(wait_slice, 1),
            "wake_on_pending": "1",
            "wake_on_state": "1",
            "include_results": "1",
            "include_deliveries": "1",
        })
        url = f"{hub_url.rstrip('/')}/api/mailbox/poll?{params}"

        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=min(wait_slice + 10.0, remaining)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                    raise ValueError("Hub poll response must contain an events array.")
                raw_events = data.get("events", [])
                # Only returned events acknowledge a cursor, including intentionally hidden noise.
                delivered_seq = max([cur_seq, *(int(event["seq"]) for event in raw_events)])
                events = [
                    ev for ev in raw_events
                    if not str(ev.get("content") or "").lstrip().startswith("[approval granted without asking")
                ]
                pending_interactions = data.get("pending_interactions")
                delivery_updates = data.get("delivery_updates", [])
                results = data.get("results", [])
                if (not isinstance(pending_interactions, list)
                        or not isinstance(delivery_updates, list)
                        or not isinstance(results, list)
                        or data.get("next_action") not in ("reply", "wait", "inspect", "collect_results", "done")):
                    raise ValueError("Hub poll response lacks the parent continuation contract; update the Hub.")
                arguments = data.get("next_wait_arguments")
                if data.get("wait_required") and not isinstance(arguments, dict):
                    raise ValueError("Hub poll response lacks next_wait_arguments for unfinished work.")
                # A malformed response has not been delivered to the parent. Preserve its
                # cursor for retry rather than silently acknowledging the omitted events.
                cur_seq = delivered_seq
                if isinstance(arguments, dict):
                    data["next_wait_arguments"] = {**arguments, "after_seq": cur_seq}
                data.update(events=events, pending_interactions=pending_interactions,
                            delivery_updates=delivery_updates, results=results, next_seq=cur_seq)
                last_result = data
                failures, last_error = 0, None
                if data.get("woke_reason") == "hub_shutdown":
                    return finish("poll_error", "Hub shut down while waiting.")
                if events or pending_interactions or delivery_updates or results or data["next_action"] != "wait":
                    return data
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            failures += 1
            last_error = f"{type(exc).__name__}: {exc}"
            if failures >= 3:
                return finish("poll_error", last_error)
            time.sleep(min(float(failures), max(0, deadline - time.monotonic())))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Wait for one Sub-Agent event batch; rearm while work remains")
    parser.add_argument("--session", default="all", help="Explicit comma-separated session IDs (preferred), or legacy 'all'")
    parser.add_argument("--after-seq", type=int, default=0, help="Only return events with seq > after_seq")
    parser.add_argument("--timeout", type=float, default=1800.0, help="Max total seconds to block before returning")
    parser.add_argument("--hub-url", default=os.environ.get("HUB_API_URL", "http://127.0.0.1:4444"), help="Hub base URL")
    args = parser.parse_args()
    try:
        result = poll_events(args.session, args.after_seq, args.timeout, args.hub_url)
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
