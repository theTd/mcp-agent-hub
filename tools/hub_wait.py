"""Wait on an Agent Hub wait_url until it reports something other than ``waiting``.

Usage: python hub_wait.py <wait_url | wh_handle> [--max-seconds N] [--retry-seconds N] [--hub URL]

Run it as one background command after spawn / send / resume / respond:
  exit 0  the hub answered ask, undelivered, done or failed; the JSON is printed on stdout
  exit 2  --max-seconds passed while still waiting (the last waiting JSON is printed);
          use the cap as a heartbeat and start the command again
  exit 3  the hub refused the handle (HTTP error) or stayed unreachable for --retry-seconds;
          the error is printed as JSON {"state": "error", ...}
A refused or cut connection (hub restarting), or an answer that is not a JSON object, is
retried for up to --retry-seconds; wait handles survive a hub restart. So are the answers a
restarting hub gives on purpose: HTTP 429/502/503/504, and a 200 whose state is ``superseded``
(a long poll that lost its lease when the hub went down). Both are retried inside the same
``--retry-seconds`` budget instead of ending the wait with a failure the parent would act on.
Standard library only, so any Python 3.8+ runs it; proxies are bypassed because the hub listens
on loopback.
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

DEFAULT_HUB = "http://127.0.0.1:4444"
POLL_TIMEOUT_MAX = 300.0
EXIT_SETTLED, EXIT_TIMEOUT, EXIT_ERROR = 0, 2, 3
# A hub that is shutting down for a restart, is restarting, or is shedding load: none of these is
# the answer to the wait, so each one is retried rather than reported as a failure.
RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})
RETRY_AFTER_CAP_SEC = 5.0
SUPERSEDED_BACKOFF_SEC = 0.75
HTTP_BACKOFF_SEC = 1.0


def resolve_wait_url(target: str, hub: str) -> str:
    """A full wait_url is used as given; a bare wh_ handle is joined to --hub."""
    target = target.strip()
    if target.startswith(("http://", "https://")):
        return target.split("?", 1)[0].rstrip("/")
    if target.startswith("wh_"):
        return f"{hub.rstrip('/')}/api/wait/{target}"
    raise ValueError("expected a wait_url (http://.../api/wait/wh_...) or a wh_... handle")


def poll_once(opener: urllib.request.OpenerDirector, url: str, timeout: float) -> Dict[str, Any]:
    """One long-poll GET; the hub holds it up to `timeout` seconds and then answers waiting."""
    query = urllib.parse.urlencode({"timeout": f"{timeout:g}"})
    with opener.open(f"{url}?{query}", timeout=timeout + 30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def retry_after_seconds(exc: urllib.error.HTTPError) -> float:
    """The server's Retry-After, capped: a hub answering "come back in a minute" must not park us."""
    headers = getattr(exc, "headers", None)
    value = None
    if headers is not None:
        try:
            value = headers.get("Retry-After")
        except Exception:
            value = None
    try:
        seconds = float(str(value).strip()) if value not in (None, "") else HTTP_BACKOFF_SEC
    except ValueError:
        seconds = HTTP_BACKOFF_SEC
    return max(0.0, min(RETRY_AFTER_CAP_SEC, seconds))


def wait_until_settled(url: str, max_seconds: float, retry_seconds: float,
                       opener: Optional[urllib.request.OpenerDirector] = None,
                       clock=time.monotonic, sleep=time.sleep) -> tuple:
    """Loop the long-poll; return (exit_code, payload)."""
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = clock() + max_seconds
    last: Dict[str, Any] = {"state": "waiting"}
    # The first moment of the current run of hub trouble (a refused connection, a retryable HTTP
    # answer, a superseded poll). It is the one budget all of them share, and a clean answer
    # clears it.
    trouble_since: Optional[float] = None
    trouble_wait: Dict[str, Any] = {"state": "error", "detail": "hub unreachable"}

    def over_budget(now: float) -> bool:
        return trouble_since is not None and now - trouble_since >= retry_seconds

    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return EXIT_TIMEOUT, last
        try:
            payload = poll_once(opener, url, max(1.0, min(POLL_TIMEOUT_MAX, remaining)))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:2000]
            if exc.code in RETRYABLE_HTTP_STATUSES:
                now = clock()
                trouble_since = trouble_since if trouble_since is not None else now
                trouble_wait = {"state": "error", "http_status": exc.code, "detail": body}
                if over_budget(now):
                    return EXIT_ERROR, trouble_wait
                sleep(min(retry_after_seconds(exc), max(0.0, deadline - now)))
                continue
            return EXIT_ERROR, {"state": "error", "http_status": exc.code, "detail": body}
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            # A refused or cut connection, or a body cut mid-way (IncompleteRead) or not JSON
            # (ValueError covers JSON and UTF-8 decode errors): a restarting hub, so retry.
            now = clock()
            trouble_since = trouble_since if trouble_since is not None else now
            trouble_wait = {"state": "error",
                            "detail": f"hub unreachable or not answering JSON: {exc}"}
            if over_budget(now):
                return EXIT_ERROR, trouble_wait
            sleep(min(5.0, max(0.0, deadline - now)))
            continue
        if payload.get("state") == "superseded":
            # A long poll that lost its lease because the hub went down under it. The handle is
            # still valid; settling here would report a failure for a worker that is coming back.
            now = clock()
            trouble_since = trouble_since if trouble_since is not None else now
            trouble_wait = {"state": "error", "detail": payload.get("detail")
                            or "hub restarted while this wait was held (superseded)"}
            if over_budget(now):
                return EXIT_ERROR, trouble_wait
            sleep(min(SUPERSEDED_BACKOFF_SEC, max(0.0, deadline - now)))
            continue
        trouble_since = None
        trouble_wait = {"state": "error", "detail": "hub unreachable"}
        if payload.get("state") != "waiting":
            return EXIT_SETTLED, payload
        last = payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Wait on an Agent Hub wait_url.")
    parser.add_argument("target", help="wait_url, or a wh_... handle")
    parser.add_argument("--max-seconds", type=float, default=3000.0,
                        help="give up after this long and exit 2 (default 3000 = 50 min)")
    parser.add_argument("--retry-seconds", type=float, default=300.0,
                        help="how long a refused or restarting hub is retried before exit 3 (default 300)")
    parser.add_argument("--hub", default=DEFAULT_HUB, help=f"hub base URL for a bare handle ({DEFAULT_HUB})")
    args = parser.parse_args(argv)
    try:
        url = resolve_wait_url(args.target, args.hub)
    except ValueError as exc:
        print(json.dumps({"state": "error", "detail": str(exc)}))
        return EXIT_ERROR
    code, payload = wait_until_settled(url, max(1.0, args.max_seconds), max(0.0, args.retry_seconds))
    # A Windows console code page cannot carry every character an answer may hold.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
