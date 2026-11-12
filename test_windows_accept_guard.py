"""The hub keeps listening after one accept fails (Windows Proactor loop)."""
import asyncio
import sys
import types
import unittest
from asyncio import proactor_events

import windows_accept_guard

NETNAME_DELETED = OSError(22, "The specified network name is no longer available", None, 64)


class Hello(asyncio.Protocol):
    def connection_made(self, transport):
        transport.write(b"hello")
        transport.close()


async def serve_with_one_failed_accept(start_serving=None):
    """Start a server whose first accept fails as WinError 64, then try one real connection."""
    loop = asyncio.get_running_loop()
    if start_serving is not None:
        loop._start_serving = types.MethodType(start_serving, loop)
    real_accept = loop._proactor.accept
    calls = 0

    def accept(sock):
        nonlocal calls
        calls += 1
        if calls == 1:
            failed = loop.create_future()
            failed.set_exception(NETNAME_DELETED)
            return failed
        return real_accept(sock)

    loop._proactor.accept = accept
    server = await loop.create_server(Hello, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await asyncio.sleep(0.2)
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
        data = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        return data
    finally:
        server.close()
        loop._proactor.accept = real_accept


@unittest.skipUnless(sys.platform == "win32", "Proactor event loop is Windows-only")
class AcceptGuardTests(unittest.TestCase):
    def run_on_proactor(self, coro):
        with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
            return runner.run(coro)

    def test_standard_loop_stops_listening_after_one_failed_accept(self):
        # The bug the guard exists for: the listening socket is closed, new connections are refused.
        with self.assertRaises(OSError):
            self.run_on_proactor(serve_with_one_failed_accept(windows_accept_guard.ORIGINAL_START_SERVING))

    def test_guarded_loop_keeps_listening(self):
        self.assertTrue(windows_accept_guard.install())
        self.assertIs(proactor_events.BaseProactorEventLoop._start_serving, windows_accept_guard._start_serving)
        self.assertEqual(self.run_on_proactor(serve_with_one_failed_accept()), b"hello")

    def test_closing_the_server_still_closes_the_socket(self):
        windows_accept_guard.install()

        async def main():
            loop = asyncio.get_running_loop()
            server = await loop.create_server(Hello, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server.close()
            await server.wait_closed()
            await asyncio.sleep(0.1)
            with self.assertRaises(OSError):
                await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)

        self.run_on_proactor(main())

    def test_install_is_idempotent(self):
        self.assertTrue(windows_accept_guard.install())
        self.assertTrue(windows_accept_guard.install())


if __name__ == "__main__":
    unittest.main()
