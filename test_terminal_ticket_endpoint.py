"""§6.4: the ticket route and the health line, on the hub's own HTTP surface.

The core is the fake: this module pins the hub's decisions, not the core's ticket (which has its
own Rust cases and `test_terminal_core_client.TerminalSocketTests`). The console connects to the
core's listener itself, so the hub's only job is to answer one URL per call, refuse an id it cannot
serve, and never let the token reach a log line.

Run: python -m unittest -v test_terminal_ticket_endpoint
"""

import asyncio
import importlib.util
import logging
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from fake_terminal_core import FakeTerminal
import session_manager


class TicketEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        import uvicorn
        self.temporary = tempfile.TemporaryDirectory(prefix="hub-ticket-")
        self.root = Path(self.temporary.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        settings = {"HUB_DATA_DIR": str(self.root), "HUB_PROFILES_PATH": str(profiles)}
        spec = importlib.util.spec_from_file_location("ticket_hub_server",
                                                      Path(__file__).with_name("server.py"))
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, settings), patch.object(
                session_manager.HubSessionManager, "_initial_discovery",
                lambda manager: manager._discovery_ready.set()):
            spec.loader.exec_module(self.module)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        # The access log is on for this module: one case asserts the token never reaches it.
        self.server = uvicorn.Server(uvicorn.Config(self.module.app, log_level="error",
                                                    access_log=True, lifespan="on"))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        deadline = time.monotonic() + 5
        while not self.server.started:
            if self.task.done():
                await self.task
            if time.monotonic() >= deadline:
                raise RuntimeError("isolated hub startup timed out")
            await asyncio.sleep(0.01)
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}",
                                        trust_env=False, timeout=3)
        self.hub = self.module.hub
        self.hub.core = FakeTerminal()
        self.sessions = {}
        for session_id, kind, command in (("starting", "tui", "cmd.exe"),
                                          ("ended", "tui", "cmd.exe"),
                                          ("text", "virtual", "")):
            sess = session_manager.AgentSession(
                session_id, "pwsh", "Probe", kind, "Probe", str(self.root), command, [],
                core=self.hub.core,
            )
            sess.engine = "dsh" if kind == "virtual" else "pwsh"
            sess._loop = asyncio.get_running_loop()
            sess.status = "starting"
            self.hub.sessions[session_id] = sess
            self.sessions[session_id] = sess
        # A starting session and a text session own a core entry but no drained record yet; the
        # ended one has its entry and its ending, which the console still replays.
        self.hub.core.spawn("starting", ["cmd.exe"])
        self.hub.core.attach_text("text", cols=120, rows=36)
        self.hub.core.spawn("ended", ["cmd.exe"])
        self.hub.core.set_exit("ended", 0)
        self.hub.core.die("ended")
        self.hub._on_core_tick()
        self.sessions["ended"].status = "exited"
        self.sessions["ended"].ended_at = time.time()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.should_exit = True
        await asyncio.wait_for(self.task, timeout=10)
        self.socket.close()
        self.temporary.cleanup()

    async def test_an_unknown_id_is_a_404_before_anything_is_minted(self):
        response = await self.client.get("/api/sessions/no-such-session/terminal-ticket")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.hub.core.ticket_calls, [], "a ticket was minted for an unknown id")

    async def test_a_listed_id_the_core_does_not_have_is_a_404(self):
        # A failed spawn still listed, a desktop window, an id whose entry a retire removed: the
        # console's path is "closed", not a reconnect loop.
        ghost = session_manager.AgentSession(
            "ghost", "pwsh", "Ghost", "tui", "Ghost", str(self.root), "cmd.exe", [],
            core=self.hub.core,
        )
        ghost.status = "running"
        self.hub.sessions["ghost"] = ghost
        response = await self.client.get("/api/sessions/ghost/terminal-ticket")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.hub.core.ticket_calls, [], "a ticket was minted for a core-less id")

    async def test_starting_ended_and_text_sessions_all_get_a_ticket(self):
        # The owner's medium tier: a listed session with a core entry gets one, liveness aside —
        # the console's pane attaches an ended session's terminal and shows the final replay.
        for session_id in ("starting", "ended", "text"):
            response = await self.client.get(f"/api/sessions/{session_id}/terminal-ticket")
            self.assertEqual(response.status_code, 200, (session_id, response.text))
            body = response.json()
            self.assertEqual(set(body), {"url", "expires_in"}, body)
            self.assertIn(f"/ws/pty/{session_id}?ticket=", body["url"])
            self.assertTrue(body["url"].startswith("ws://127.0.0.1:"), body)
            self.assertGreaterEqual(body["expires_in"], 1, body)
            self.assertLessEqual(body["expires_in"], 30, body)

    async def test_a_delayed_mint_does_not_hold_the_loop(self):
        # kimi 4b #3: `ws_ticket` reads the core's session table, which a spawn/retire/snapshot can
        # hold past its deadline. The route awaits it in a worker, so the loop keeps beating; a mint
        # on the loop shows up as one long gap here.
        core = self.hub.core
        ticket = core.ws_ticket
        held = []

        def slow_ticket(session_id, ttl_ms=None):
            held.append(session_id)
            time.sleep(0.15)
            return ticket(session_id, ttl_ms)

        core.ws_ticket = slow_ticket
        loop = asyncio.get_running_loop()
        worst = 0.0
        done = asyncio.Event()

        async def beat():
            nonlocal worst
            last = loop.time()
            while not done.is_set():
                await asyncio.sleep(0.005)
                now = loop.time()
                worst = max(worst, now - last)
                last = now

        beating = asyncio.create_task(beat())
        await asyncio.sleep(0)
        try:
            response = await self.client.get("/api/sessions/starting/terminal-ticket")
        finally:
            done.set()
            await asyncio.wait([beating])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(held, ["starting"], "the mint did not run")
        self.assertLess(worst, 0.08, f"the ticket mint held the loop for {worst:.3f}s")

    async def test_a_table_deadline_answers_a_retryable_503(self):
        # Gaps 8: the core's session table can miss its read deadline. That is the hub being busy,
        # not a fault: the console retries any non-404 answer on its backoff, so answer 503.
        self.hub.core.read_timeouts = 1
        response = await self.client.get("/api/sessions/starting/terminal-ticket")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers.get("retry-after"), "1")
        self.assertTrue(response.json().get("detail"), "a 503 without a message")
        self.assertEqual(self.hub.core.ticket_calls, [], "a busy table still minted a ticket")

    async def test_a_hub_without_a_core_answers_its_decided_404(self):
        # F14: the hub builds its core lazily. With none built the route answers 404 without
        # building one, so a health-only hub never starts the terminal core.
        self.hub.core = None
        try:
            response = await self.client.get("/api/sessions/starting/terminal-ticket")
        finally:
            self.hub.core = FakeTerminal()
        self.assertEqual(response.status_code, 404, response.text)

    async def test_every_call_mints_a_fresh_single_use_ticket(self):
        first = (await self.client.get("/api/sessions/starting/terminal-ticket")).json()["url"]
        second = (await self.client.get("/api/sessions/starting/terminal-ticket")).json()["url"]
        self.assertNotEqual(first, second, "a ticket URL was reused or cached")
        self.assertEqual(self.hub.core.ticket_calls, ["starting", "starting"])

    async def test_the_token_never_reaches_a_log_line(self):
        # gaps 1/14: the token is a bearer credential; it may exist in the response body only, so
        # neither the hub's log nor uvicorn's access log may carry it.
        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = Collect()
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with self.assertLogs("uvicorn.access", level="INFO") as access:
                response = await self.client.get("/api/sessions/starting/terminal-ticket")
                await asyncio.sleep(0.05)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["url"].split("ticket=", 1)[1]
        self.assertTrue(token)
        access_text = "\n".join(access.output)
        self.assertIn("/api/sessions/starting/terminal-ticket", access_text,
                      "the access log did not record the request this test checks")
        self.assertNotIn(token, access_text)
        self.assertTrue(records, "the capture saw no log lines at all")
        self.assertNotIn(token, "\n".join(records))
        self.assertNotIn(token, str(response.request.url))

    async def test_health_reports_the_listener_and_never_builds_a_core(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["terminal_ws"], {"listening": True, "port": 45999})
        # The accept thread ended (`ws_listening()` false) is reported, not hidden.
        self.hub.core.listening = False
        self.assertEqual((await self.client.get("/health")).json()["terminal_ws"]["listening"], False)
        # F14: with no core built yet, health stays null and builds nothing.
        self.hub.core = None
        body = (await self.client.get("/health")).json()
        self.assertEqual(body["terminal_ws"], {"listening": None, "port": None})
        self.assertIsNone(self.hub.core, "/health built the terminal core")


if __name__ == "__main__":
    unittest.main(verbosity=2)
