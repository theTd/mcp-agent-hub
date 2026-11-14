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
from typing import FrozenSet, Iterable, Optional, Set, Tuple
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("mcp-agent-hub.request_guard")

REFUSAL_TEXT = "只接受本机（loopback）Host 与 Origin"
_REFUSAL_BODY = REFUSAL_TEXT.encode("utf-8")

# A browser always sends `Origin` on a WebSocket and on any cross-origin request, so a refusal repeats
# only while a page keeps retrying. One line per pair per window, and a hard budget per window across
# all pairs: a page can mint a fresh `Host` per request from its own DNS, so the per-pair rule alone
# bounds memory but not the size of the log file.
_LOG_WINDOW = 60.0
_LOG_BUDGET = 20
_LOG_PATH_MAX = 200

# What the refusal line says instead of a header the guard could not validate.
_INVALID = "<invalid>"

# `ws:` travels over plain HTTP and `wss:` over TLS, so an origin scheme is judged against the transport
# the request actually arrived on.
_ORIGIN_SCHEME = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
_DEFAULT_PORT = {"http": 80, "https": 443}

# The log window's clock, aliased so a test can move it without touching every other user of `time`.
_monotonic = time.monotonic


def _split_csv(raw: Optional[str]) -> Iterable[str]:
    return (part.strip() for part in (raw or "").split(",") if part.strip())


def _split_host_value(value: str) -> Optional[Tuple[str, Optional[int]]]:
    """Split a `Host` header into (name, port) or None when it is not a bare host[:port]."""
    value = (value or "").strip()
    # A header value is one token: whitespace, controls and other unprintables never make it through,
    # so nothing the guard logs from a parsed value can forge or break a log line.
    if not value or any(char.isspace() or not char.isprintable() for char in value):
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
    """Split an `Origin` header (or a `HUB_ALLOWED_ORIGINS` entry) into (scheme, host, port).

    None for `null`, anything not http(s), and anything carrying a path beyond one trailing `/`, a
    query, a fragment or userinfo. Entries are normalised like a request, so both go through here.
    """
    value = (value or "").strip()
    if value.endswith("/"):
        value = value[:-1]
    if not value or any(not char.isprintable() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORT or not parsed.hostname:
        return None
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return None
    return scheme, parsed.hostname, port


def _name_key(host: str) -> str:
    """The name a host is judged by: case-insensitive and without a trailing root dot."""
    return host.strip().rstrip(".").lower()


def _is_loopback(host: str) -> bool:
    """True for this machine's own names: `localhost` and any loopback IP literal (`127.0.0.1`, `::1`)."""
    name = _name_key(host)
    if name == "localhost":
        return True
    try:
        return ip_address(name).is_loopback
    except ValueError:
        return False


def _origin_key(scheme: str, host: str, port: Optional[int]) -> Tuple[str, str, int]:
    """An origin reduced to what it names, with its default port filled in (`http://a` == `http://a:80`)."""
    return scheme, _name_key(host), port if port is not None else _DEFAULT_PORT[scheme]


def _format_host(name: str, port: Optional[int]) -> str:
    """A parsed host as the log states it, or `<invalid>` if it could not be printed safely."""
    name = _name_key(name)
    if not name or not name.isprintable():
        return _INVALID
    if ":" in name:
        name = f"[{name}]"
    return f"{name}:{port}" if port is not None else name


def _log_host(values: Iterable[str]) -> str:
    """What the refusal line says about `Host`: one normalised host[:port], else a fixed placeholder."""
    values = list(values)
    parsed = _split_host_value(values[0]) if len(values) == 1 else None
    return _format_host(*parsed) if parsed else _INVALID


def _log_origin(values: Iterable[str]) -> str:
    """What the refusal line says about `Origin`: one normalised origin, `none`, or a placeholder."""
    values = list(values)
    if not values:
        return "none"
    parsed = _split_origin(values[0]) if len(values) == 1 else None
    if parsed is None:
        return _INVALID
    scheme, host, port = parsed
    return f"{scheme}://{_format_host(host, port if port is not None else _DEFAULT_PORT[scheme])}"


def _allowed_host_names(entries: Iterable[str]) -> FrozenSet[str]:
    """`HUB_ALLOWED_HOSTS` entries → host names, parsed and normalised like a request's `Host`.

    An entry's port is ignored (a fronting proxy may present any port, and the name is what a rebinding
    page cannot forge); an entry that does not parse as one `host[:port]`, or names `*`, admits nothing
    and is reported once so an owner is never left wondering why the proxy is refused.
    """
    names: Set[str] = set()
    for entry in entries:
        parsed = _split_host_value(entry)
        name = _name_key(parsed[0]) if parsed else ""
        if not name or "*" in name:
            logger.warning("Ignoring unusable HUB_ALLOWED_HOSTS entry %r (expected host[:port])", entry)
            continue
        names.add(name)
    return frozenset(names)


def _allowed_origin_keys(entries: Iterable[str]) -> FrozenSet[Tuple[str, str, int]]:
    """`HUB_ALLOWED_ORIGINS` entries → comparable keys, parsed and normalised like a request's Origin."""
    keys: Set[Tuple[str, str, int]] = set()
    for entry in entries:
        parts = _split_origin(entry)
        if parts is None or "*" in entry:
            logger.warning(
                "Ignoring unusable HUB_ALLOWED_ORIGINS entry %r (expected scheme://host[:port])", entry)
            continue
        keys.add(_origin_key(*parts))
    return frozenset(keys)


class LoopbackRequestGuard:
    """Reject a non-loopback `Host` and a foreign `Origin` on both `http` and `websocket` scopes.

    Exactly one `Host` is required and at most one `Origin` is read, both through the multi-value API:
    duplicate or empty headers are refused, since two parsers could disagree about which value counts.

    `HUB_ALLOWED_HOSTS` (comma-separated `host[:port]`, matched by name; the port is ignored) and
    `HUB_ALLOWED_ORIGINS` (comma-separated origins) admit a proxy fronting the hub; the defaults are
    loopback only, `*` is not a wildcard, and an entry that cannot be parsed is dropped with one
    warning. Starlette builds its middleware stack at the first request, so the environment is read
    once there — before the hub serves anything — and may also be passed in directly.
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
        self.allowed_hosts: FrozenSet[str] = _allowed_host_names(hosts)
        self.allowed_origins: FrozenSet[Tuple[str, str, int]] = _allowed_origin_keys(origins)
        self._window_started = _monotonic()
        self._window_lines = 0
        self._suppressed = 0
        self._logged: Set[Tuple[str, str]] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host_values = headers.getlist("host")
        origin_values = headers.getlist("origin")
        if self._accepted(scope, host_values, origin_values):
            await self.app(scope, receive, send)
            return
        self._log_refusal(host_values, origin_values, scope.get("path", ""))
        if scope["type"] == "http":
            await PlainTextResponse(REFUSAL_TEXT, status_code=403)(scope, receive, send)
        else:
            await _refuse_handshake(scope, send)

    def _accepted(self, scope: Scope, host_values: list, origin_values: list) -> bool:
        if len(host_values) != 1:
            # HTTP/1.1 requires exactly one `Host`, and with two the guard and the router could read
            # different names. A missing Host leaves no name to check either, so both are refused: no
            # hub client, MCP client or browser omits it.
            return False
        host = _split_host_value(host_values[0])
        if host is None:
            return False
        host_name, host_port = host
        if not (_is_loopback(host_name) or _name_key(host_name) in self.allowed_hosts):
            return False
        if not origin_values:
            return True
        if len(origin_values) != 1:
            return False
        # An empty or whitespace-only Origin is present, not absent, and _split_origin rejects it: the
        # sender meant to name a page and did not.
        origin = _split_origin(origin_values[0])
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

    def _log_refusal(self, host_values: list, origin_values: list, path: str) -> None:
        now = _monotonic()
        elapsed = now - self._window_started
        if elapsed >= _LOG_WINDOW or elapsed < 0:
            self._start_window(now)
        # The key is what the line would say, so header spellings that normalise alike share one slot.
        key = (_log_host(host_values), _log_origin(origin_values))
        if key in self._logged:
            return
        if self._window_lines >= _LOG_BUDGET:
            self._suppressed += 1
            return
        self._logged.add(key)
        self._window_lines += 1
        # Only validated values reach the line, and the path goes in with `%r`: uvicorn hands the app a
        # percent-decoded path, so a `%0a` in it would otherwise start a forged line in the shared log.
        # No query string is ever part of it, and the line is capped so a long path cannot flood.
        logger.warning(
            "Blocked request: host=%r origin=%r path=%r", key[0], key[1], path[:_LOG_PATH_MAX])

    def _start_window(self, now: float) -> None:
        """Open a fresh logging window, reporting what the budget kept out of the previous one."""
        if self._suppressed:
            logger.warning("%d refusals not logged in the last minute", self._suppressed)
        self._window_started = now
        self._window_lines = 0
        self._suppressed = 0
        self._logged.clear()


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
