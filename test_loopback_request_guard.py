"""The hub's loopback Host/Origin guard, exercised through the real `server.app`.

Every request here goes through the ASGI stack the live hub serves, so a passing test also proves the
guard is registered where it must be (outermost, above both `@app.middleware("http")` functions and the
MCP routes copied onto the app router).
"""
import asyncio
import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient, WebSocketDenialResponse

from loopback_request_guard import REFUSAL_TEXT
from session_manager import HubSessionManager

HUB_PORT = 4444
FOREIGN_HOST = {"host": "attacker.example:4444"}
FOREIGN_ORIGIN = {"origin": "http://attacker.example:4444"}
HUB_ORIGIN = {"origin": f"http://127.0.0.1:{HUB_PORT}"}
LOOPBACK_HOST = {"host": f"127.0.0.1:{HUB_PORT}"}


class LoopbackGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-guard-")
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles.json"
        self.profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda hub: hub._discovery_ready.set()):
            self.hub = HubSessionManager(self.profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        # The module-level hub is never started here: every passing route below answers from constants.
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def request(self, method="GET", path="/api/hub/info", **headers):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app),
            base_url=f"http://127.0.0.1:{HUB_PORT}",
        ) as client:
            return await client.request(method, path, headers=headers or None)

    def websocket(self, path="/ws/pty/missing", **headers):
        """Enter the handshake; raises `WebSocketDenialResponse` instead of accepting on a refusal.

        The client's context manager is deliberately not entered: that would run the app lifespan and
        start real storage, and the handshake needs none of it.
        """
        return TestClient(self.server.app).websocket_connect(path, headers=headers)

    async def test_foreign_host_is_refused_on_api_mcp_and_pty(self):
        api = await self.request(**FOREIGN_HOST)
        self.assertEqual(api.status_code, 403)
        self.assertTrue(api.headers["content-type"].startswith("text/plain"))
        mcp = await self.request("POST", "/mcp", **FOREIGN_HOST)
        self.assertEqual(mcp.status_code, 403)
        with self.assertRaises(WebSocketDenialResponse) as refused:
            with self.websocket(**FOREIGN_HOST):
                pass
        self.assertEqual(refused.exception.status_code, 403)
        self.assertIn(b"loopback", refused.exception.content)

    async def test_loopback_host_with_foreign_origin_is_refused(self):
        response = await self.request(**FOREIGN_ORIGIN)
        self.assertEqual(response.status_code, 403)
        with self.assertRaises(WebSocketDenialResponse):
            with self.websocket(**FOREIGN_ORIGIN):
                pass

    async def test_a_non_loopback_ip_literal_host_is_refused(self):
        # A name that is an IP literal is judged by the address, not by the text: only loopback addresses
        # pass, so a LAN address typed into the URL bar is refused too.
        for host in ("192.168.1.5:4444", "10.0.0.2:4444", "[fe80::1]:4444"):
            with self.subTest(host=host):
                self.assertEqual((await self.request(host=host)).status_code, 403)

    async def test_null_origin_is_refused(self):
        for origin in ("null", "http://127.0.0.1:4444.evil.example", "file://127.0.0.1"):
            with self.subTest(origin=origin):
                self.assertEqual((await self.request(origin=origin)).status_code, 403)

    async def test_another_loopback_port_is_refused(self):
        # A different local web app is a weaker trust level than the hub itself: its page could be
        # serving attacker-controlled content, and the hub would be the CSRF target.
        for origin in ("http://127.0.0.1:4445", "http://localhost:4445", "http://[::1]:4445"):
            with self.subTest(origin=origin):
                self.assertEqual((await self.request(origin=origin)).status_code, 403)

    async def test_the_hubs_own_origin_passes(self):
        for host, origin in (
            (f"127.0.0.1:{HUB_PORT}", f"http://127.0.0.1:{HUB_PORT}"),
            (f"localhost:{HUB_PORT}", f"http://localhost:{HUB_PORT}"),
            (f"[::1]:{HUB_PORT}", f"http://[::1]:{HUB_PORT}"),
            (f"LOCALHOST:{HUB_PORT}", f"HTTP://LocalHost:{HUB_PORT}"),
        ):
            with self.subTest(host=host, origin=origin):
                response = await self.request(host=host, origin=origin)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["service"], "mcp-agent-hub")

    async def test_requests_without_origin_pass(self):
        self.assertEqual((await self.request()).status_code, 200)
        for host in (f"127.0.0.1:{HUB_PORT}", f"localhost:{HUB_PORT}", f"[::1]:{HUB_PORT}", "127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual((await self.request(host=host)).status_code, 200)

    async def test_a_websocket_without_origin_passes_the_handshake(self):
        with self.websocket(**LOOPBACK_HOST):
            pass

    async def test_a_websocket_from_the_hubs_own_page_passes_the_handshake(self):
        with self.websocket(**LOOPBACK_HOST, **HUB_ORIGIN):
            pass

    async def test_the_mcp_endpoint_is_reached_without_an_origin(self):
        # Only the guard's own answer (403, plain text) counts as a refusal: whatever the stack behind it
        # says — the MCP app failing without a lifespan, or the shutdown middleware's 503 — proves the
        # request was handed on. A refusal by the guard always looks exactly like REFUSAL_TEXT below.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app, raise_app_exceptions=False),
                                     base_url=f"http://127.0.0.1:{HUB_PORT}") as client:
            response = await client.post("/mcp", headers={"content-type": "application/json",
                                                          "accept": "application/json, text/event-stream"})
        self.assertNotEqual(response.status_code, 403)
        self.assertNotEqual(response.text, REFUSAL_TEXT)

    async def test_the_guard_is_the_outermost_middleware(self):
        names = [middleware.cls.__name__ for middleware in self.server.app.user_middleware]
        self.assertEqual(names[0], "LoopbackRequestGuard", names)

    async def test_a_missing_host_is_refused(self):
        # HTTP/1.1 requires Host, so only a hand-built request can omit it; with no name to check there is
        # nothing to accept it on, so the guard refuses.
        status = await _raw_status(self.server.app, [(b"origin", b"http://127.0.0.1:4444")])
        self.assertEqual(status, 403)

    async def test_rebinding_request_cannot_read_sessions(self):
        response = await self.request("GET", "/api/sessions", **FOREIGN_HOST, **FOREIGN_ORIGIN)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("sessions", response.text)

    async def test_env_allowlists_admit_their_entries(self):
        spec = importlib.util.spec_from_file_location("guard_env_server", Path(__file__).with_name("server.py"))
        module = importlib.util.module_from_spec(spec)
        env = {"HUB_ALLOWED_HOSTS": "proxy.internal, console.internal",
               "HUB_ALLOWED_ORIGINS": "https://console.internal, http://127.0.0.1:8080"}
        # Starlette reads the environment when it builds its middleware stack, on the first request.
        with patch.dict(os.environ, env), \
                patch.object(HubSessionManager, "_initial_discovery", lambda hub: hub._discovery_ready.set()):
            spec.loader.exec_module(module)

            async def call(**headers):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                             base_url=f"http://127.0.0.1:{HUB_PORT}") as client:
                    return await client.get("/api/hub/info", headers=headers or None)

            self.assertEqual((await call(host="proxy.internal:8443")).status_code, 200)
            self.assertEqual((await call(host="PROXY.internal:8443")).status_code, 200)
            self.assertEqual((await call(origin="https://console.internal")).status_code, 200)
            self.assertEqual((await call(origin="http://127.0.0.1:8080")).status_code, 200)
            # The allowlists add names; they never turn off the loopback rule for anything else.
            self.assertEqual((await call(host="evil.example:4444")).status_code, 403)
            self.assertEqual((await call(origin="http://evil.example:4444")).status_code, 403)
            self.assertEqual((await call(origin="https://console.internal:8443")).status_code, 403)

    async def test_refusal_log_omits_the_query_string_and_repeats_once_a_minute(self):
        headers = {**FOREIGN_HOST, **FOREIGN_ORIGIN}
        headers["host"] = "log-check.example:4444"
        with self.assertLogs("mcp-agent-hub.request_guard", level="WARNING") as captured:
            for _ in range(3):
                self.assertEqual((await self.request("GET", "/api/sessions?ticket=supersecret", **headers)).status_code, 403)
        lines = [record.getMessage() for record in captured.records]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("log-check.example:4444", lines[0])
        self.assertIn("/api/sessions", lines[0])
        self.assertNotIn("supersecret", lines[0])
        self.assertNotIn("ticket", lines[0])


async def _raw_status(app, headers):
    """Run the app on a hand-built ASGI scope and return the status of the response it starts."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/hub/info",
        "raw_path": b"/api/hub/info",
        "root_path": "",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 5555),
        "server": ("127.0.0.1", HUB_PORT),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


if __name__ == "__main__":
    unittest.main()