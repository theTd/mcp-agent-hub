"""Loopback-only Host/Origin guard for every HTTP and WebSocket route of the hub.

The hub listens on 127.0.0.1, which the browser does not treat as a boundary:

* DNS rebinding: a page at `http://attacker.example:4444` whose name is re-pointed to 127.0.0.1 is
  same-origin with the hub, so it can read `/api/...` answers and drive the MCP endpoint.
* WebSockets ignore the same-origin policy outright, so any page can open `/ws/pty/<id>`.

Both attacks arrive with a `Host` the browser was forced to build from the attacker's URL, and (for
any cross-origin request, plus every WebSocket) an `Origin` naming the attacker's page. The guard
rejects those two headers; the routes behind it never see the request.

Local processes are not the threat model — they can read the hub's files anyway — so a request with no
`Origin` is a non-browser client (MCP client, the desktop shell, an engine hook, a `wait_url` poll) and
passes on its `Host` alone.
"""

import logging
import os
import time
from ipaddress import ip_address
from typing import Dict, FrozenSet, Iterable, Optional, Tuple
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("mcp-agent-hub.request_guard")

REFUSAL_TEXT = "只接受本机（loopback）Host 与 Origin"
_REFUSAL_BODY = REFUSAL_TEXT.encode("utf-8")

# A browser always sends `Origin` on a WebSocket and on any cross-origin request, so a refusal repeats
# only while a page keeps retrying; one line per pair per minute is enough to notice it.
_LOG_INTERVAL = 60.0
_LOG_MAX_PAIRS = 64

# `ws:` travels over plain HTTP and `wss:` over TLS, so an origin scheme is judged against the transport
# the request actually arrived on.
_ORIGIN_SCHEME = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
_DEFAULT_PORT = {"http": 80, "https": 443}


def _split_csv(raw: Optional[str]) -> Iterable[str]:
    return (part.strip() for part in (raw or "").split(",") if part.strip())


def _split_host_value(value: str) -> Optional[Tuple[str, Optional[int]]]:
    """Split a `Host` header into (name, port) or None when it is not a bare host[:port]."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = urlsplit("//" + value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host or parsed.username or parsed.password:
        return None
    if parsed.path or parsed.query or parsed.fragment:
        return None
    return host, port


def _split_origin(value: str) -> Optional[Tuple[str, str, Optional[int]]]:
    """Split an `Origin` header into (scheme, host, port) or None for `null` and anything not http(s)."""
    try:
        parsed = urlsplit((value or "").strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORT or not parsed.hostname:
        return None
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return None
    return scheme, parsed.hostname, port


def _is_loopback(host: str) -> bool:
    """True for this machine's own names: `localhost` and any loopback IP literal (`127.0.0.1`, `::1`)."""
    name = host.strip().rstrip(".").lower()
    if name == "localhost":
        return True
    try:
        return ip_address(name).is_loopback
    except ValueError:
        return False


def _origin_key(scheme: str, host: str, port: Optional[int]) -> Tuple[str, str, int]:
    """An origin reduced to what it names, with its default port filled in (`http://a` == `http://a:80`)."""
    return scheme, host.strip().rstrip(".").lower(), port if port is not None else _DEFAULT_PORT[scheme]


def _allowed_origin_key(origin: str) -> Optional[Tuple[str, str, int]]:
    """`HUB_ALLOWED_ORIGINS` entry → comparable key; an unparsable entry admits nothing."""
    parts = _split_origin(origin)
    return _origin_key(*parts) if parts else None


class LoopbackRequestGuard:
    """Reject a non-loopback `Host` and a foreign `Origin` on both `http` and `websocket` scopes.

    `HUB_ALLOWED_HOSTS` (comma-separated names, matched against the Host name only) and
    `HUB_ALLOWED_ORIGINS` (comma-separated origins) admit a proxy fronting the hub; the defaults are
    loopback only. Starlette builds its middleware stack at the first request, so the environment is
    read once there — before the hub serves anything — and may also be passed in directly.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Optional[Iterable[str]] = None,
        allowed_origins: Optional[Iterable[str]] = None,
    ) -> None:
        self.app = app
        hosts = _split_csv(os.environ.get("HUB_ALLOWED_HOSTS")) if allowed_hosts is None else allowed_hosts
        origins = _split_csv(os.environ.get("HUB_ALLOWED_ORIGINS")) if allowed_origins is None else allowed_origins
        self.allowed_hosts: FrozenSet[str] = frozenset(
            host.strip().strip("[]").rstrip(".").lower() for host in hosts)
        self.allowed_origins: FrozenSet[Tuple[str, str, int]] = frozenset(
            key for key in map(_allowed_origin_key, origins) if key is not None)
        self._logged: Dict[Tuple[str, str], float] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host_header = headers.get("host", "")
        origin_header = headers.get("origin", "")
        if self._accepted(scope, host_header, origin_header):
            await self.app(scope, receive, send)
            return
        self._log_refusal(host_header, origin_header, scope.get("path", ""))
        if scope["type"] == "http":
            await PlainTextResponse(REFUSAL_TEXT, status_code=403)(scope, receive, send)
        else:
            await _refuse_handshake(scope, send)

    def _accepted(self, scope: Scope, host_header: str, origin_header: str) -> bool:
        host = _split_host_value(host_header)
        if host is None:
            # HTTP/1.1 requires `Host`, and a browser cannot omit it on a rebound request either, so this
            # only ever meets a hand-written client. Refused: nothing legitimate loses, and a missing Host
            # leaves no name to check, so accepting it would be the one hole a rebinding page could aim at.
            return False
        host_name, host_port = host
        if not (_is_loopback(host_name) or host_name.strip().rstrip(".").lower() in self.allowed_hosts):
            return False
        if not origin_header:
            return True
        origin = _split_origin(origin_header)
        if origin is None:
            return False
        scheme, origin_host, origin_port = origin
        if _origin_key(scheme, origin_host, origin_port) in self.allowed_origins:
            return True
        # The hub's own page is the only page on this port, so the origin must BE the hub: same
        # transport, loopback name, and the very port the request was addressed to. Any other port
        # would admit any web app that happens to run on this machine.
        transport = _ORIGIN_SCHEME.get(scope.get("scheme") or "http")
        if transport is None or scheme != transport or not _is_loopback(origin_host):
            return False
        request_port = host_port if host_port is not None else _DEFAULT_PORT[transport]
        origin_effective_port = origin_port if origin_port is not None else _DEFAULT_PORT[scheme]
        return request_port == origin_effective_port

    def _log_refusal(self, host_header: str, origin_header: str, path: str) -> None:
        key = (host_header, origin_header)
        now = time.monotonic()
        last = self._logged.get(key)
        if last is not None and now - last < _LOG_INTERVAL:
            return
        if len(self._logged) >= _LOG_MAX_PAIRS:
            self._logged.clear()
        self._logged[key] = now
        # The path only: a query string can carry a ticket or a token, and hub.log is a shared file.
        logger.warning("Blocked request: host=%r origin=%r path=%s", host_header, origin_header, path)


async def _refuse_handshake(scope: Scope, send: Send) -> None:
    """Refuse before accepting: a 403 HTTP response when the server advertises the extension, else a close."""
    if "websocket.http.response" in scope.get("extensions", {}):
        await send({
            "type": "websocket.http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(_REFUSAL_BODY)).encode("ascii")),
            ],
        })
        await send({"type": "websocket.http.response.body", "body": _REFUSAL_BODY})
    else:
        # 1008: policy violation. uvicorn turns a close before accept into a 403 handshake rejection.
        await send({"type": "websocket.close", "code": 1008})