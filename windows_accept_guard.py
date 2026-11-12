"""Keeps the hub's listening socket open when a single accept fails on Windows.

asyncio's Proactor event loop (CPython 3.14) handles any OSError from an accept by logging
"Accept failed on a socket" and closing the listening socket. A client that drops its connection
while it waits to be accepted (WinError 64, "The specified network name is no longer available")
is enough: uvicorn is not told, so the hub process keeps running and keeps answering the
connections it already has, but refuses every new one until it is restarted.

`install()` replaces `BaseProactorEventLoop._start_serving` with a copy whose error branch logs the
failure and posts the next accept instead, pausing briefly while failures repeat. Everything else
(transport creation, closing on cancellation, closed sockets) is the standard library's behaviour.
"""
import inspect
import logging
import sys
from asyncio import exceptions, proactor_events, trsock

logger = logging.getLogger("hub.accept_guard")

# Pause before the next accept after 1, 2, 3, 4+ failures in a row.
RETRY_DELAYS = (0.0, 0.05, 0.2, 1.0)
# A socket that keeps failing is logged on the first failure and then every this many.
LOG_EVERY = 100

_EXPECTED_PARAMS = ("self", "protocol_factory", "sock", "sslcontext", "server", "backlog",
                    "ssl_handshake_timeout", "ssl_shutdown_timeout")

ORIGINAL_START_SERVING = proactor_events.BaseProactorEventLoop._start_serving


def _start_serving(self, protocol_factory, sock,
                   sslcontext=None, server=None, backlog=100,
                   ssl_handshake_timeout=None,
                   ssl_shutdown_timeout=None):
    failures = 0

    def loop(f=None):
        nonlocal failures
        try:
            if f is not None:
                conn, addr = f.result()
                failures = 0
                protocol = protocol_factory()
                if sslcontext is not None:
                    self._make_ssl_transport(
                        conn, protocol, sslcontext, server_side=True,
                        extra={'peername': addr}, server=server,
                        ssl_handshake_timeout=ssl_handshake_timeout,
                        ssl_shutdown_timeout=ssl_shutdown_timeout)
                else:
                    self._make_socket_transport(
                        conn, protocol,
                        extra={'peername': addr}, server=server)
            if self.is_closed():
                return
            f = self._proactor.accept(sock)
        except OSError as exc:
            if sock.fileno() != -1 and not self.is_closed():
                # The standard library closes the listening socket here. Keep it and accept again.
                failures += 1
                if failures == 1 or failures % LOG_EVERY == 0:
                    logger.warning("Accept failed on %r (%s; %d in a row); still listening",
                                   trsock.TransportSocket(sock), exc, failures)
                delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS)) - 1]
                self.call_later(delay, loop)
            elif self._debug:
                logger.debug("Accept failed on socket %r", sock, exc_info=True)
        except exceptions.CancelledError:
            sock.close()
        else:
            self._accept_futures[sock.fileno()] = f
            f.add_done_callback(loop)

    self.call_soon(loop)


def install() -> bool:
    """Patch the Proactor loop on Windows; False (and the standard behaviour) anywhere else.

    Skipped, with a warning, if this Python's `_start_serving` no longer has the signature the copy
    above was taken from — an untested copy is worse than the known bug.
    """
    if sys.platform != "win32":
        return False
    current = proactor_events.BaseProactorEventLoop._start_serving
    if current is _start_serving:
        return True
    params = tuple(inspect.signature(ORIGINAL_START_SERVING).parameters)
    if params != _EXPECTED_PARAMS:
        logger.warning("Accept guard not installed: asyncio's _start_serving has changed (%s)", params)
        return False
    proactor_events.BaseProactorEventLoop._start_serving = _start_serving
    return True
