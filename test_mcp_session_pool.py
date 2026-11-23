"""End-to-end tests for the session MCP connection pool.

Every test drives a *real* MCP server: stdio servers are real ``python``
subprocesses running FastMCP, the HTTP server is a real uvicorn instance on a
random loopback port.  Nothing in the connection layer is mocked, because the
failure modes this module exists for -- lost state, orphaned ``cmd -> node``
trees, dropped headers, leaked secrets -- only appear against a real transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import shutil
import time
import unittest
from pathlib import Path

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

import mcp_session_pool as pool_mod
from mcp_session_pool import PoolRegistry, SessionPool, ServerActor, clean_text, error_result

PY = sys.executable or "python"

PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
BIG_TEXT_SIZE = 40000

# A real FastMCP server.  Written verbatim into the temp workspace so the tests
# spawn it exactly like the Hub spawns a user configured MCP server.
SERVER_SOURCE = r'''"""Test MCP server: stateful counter, slow tool, errors, big/image payloads."""
import asyncio
import base64
import os
import sys
import threading

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

mcp = FastMCP("hub-pool-test")
_state = {"count": 0}

BIG_TEXT = "\x1b[31m" + ("RED" * 20) + "\x1b[0m\n" + ("B" * 40000) + "\x1b[32mTAIL\x1b[0m"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


@mcp.tool()
def inc() -> int:
    """Increment the in-process counter and return the new value."""
    _state["count"] += 1
    return _state["count"]


@mcp.tool()
async def slow(seconds: float) -> str:
    """Sleep for ``seconds`` and then answer."""
    await asyncio.sleep(float(seconds))
    return "slept %.2f" % float(seconds)


@mcp.tool()
def boom() -> str:
    """Always raise, so the caller sees an MCP tool error."""
    raise ValueError("boom from tool")


@mcp.tool()
def big() -> str:
    """Return 40KB of text wrapped in ANSI colour codes."""
    return BIG_TEXT


@mcp.tool()
def pic() -> Image:
    """Return a tiny PNG image."""
    return Image(data=PNG_BYTES, format="png")


@mcp.tool()
def cn() -> str:
    """Return Chinese text to prove UTF-8 survives the pipe."""
    return "你好，世界"


@mcp.tool()
def env_echo() -> dict:
    """Report the custom env var and the inherited system env."""
    return {
        "custom": os.environ.get("HUB_POOL_PROBE", ""),
        "path": bool(os.environ.get("PATH") or os.environ.get("Path")),
        "systemRoot": os.environ.get("SYSTEMROOT") or os.environ.get("SystemRoot") or "",
    }


@mcp.tool()
def pid() -> int:
    """Return the server process pid."""
    return os.getpid()


@mcp.tool()
def die() -> str:
    """Answer, then exit the process so the client sees a dead connection."""
    threading.Timer(0.25, lambda: os._exit(3)).start()
    return "bye"


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
'''

SLEEPER_SOURCE = """import sys, time
time.sleep(120)
"""

# A real MCP server whose tool echoes its own secret env value in an error message, the way an
# upstream API refusing a credential does.  A second script that never answers ``initialize``
# models a handshake that stalls forever (the pid ledger and the close budget both care).
LEAKY_SERVER_SOURCE = r'''"""Test MCP server: a tool that echoes the secret env var in its error."""
import os

from fastmcp import FastMCP

mcp = FastMCP("hub-pool-leaky")


@mcp.tool()
def leak() -> str:
    """Raise an error naming the credential the server was configured with."""
    raise ValueError("upstream rejected credential %s" % os.environ.get("HUB_SECRET", ""))


@mcp.tool()
def healthy() -> str:
    """Prove the connection itself is fine."""
    return "fine"


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
'''

STUBBORN_SOURCE = """import sys, time
# Ignores stdin EOF and never exits: only a kill can end it.
while True:
    time.sleep(0.5)
"""


class _PoolCase(unittest.IsolatedAsyncioTestCase):
    """Temp workspace + registry that always tears connections down."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-pool-test-"))
        self.runtime_dir = self.tmp / "runtime"
        self.output_dir = self.tmp / "out"
        self.jobs_dir = self.tmp / "jobs"
        self._http: list = []

    async def asyncSetUp(self) -> None:
        self.registry = PoolRegistry(self.runtime_dir)

    async def asyncTearDown(self) -> None:
        try:
            await self.registry.close_all()
        finally:
            for instance, task in self._http:
                instance.should_exit = True
                try:
                    await asyncio.wait_for(task, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self._http.clear()
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------

    def server_script(self, name: str = "hub_pool_server.py") -> Path:
        path = self.tmp / name
        path.write_text(SERVER_SOURCE, encoding="utf-8")
        return path

    def stdio_spec(self, script: Path, env: dict | None = None) -> dict:
        spec = {"command": PY, "args": [str(script)]}
        if env is not None:
            spec["env"] = env
        return spec

    def make_pool(self, servers: dict, session_id: str = "s1", **kwargs) -> SessionPool:
        return self.registry.register_session(
            session_id,
            servers,
            cwd=str(self.tmp),
            output_dir=kwargs.pop("output_dir", self.output_dir),
            jobs_dir=kwargs.pop("jobs_dir", self.jobs_dir),
            **kwargs,
        )

    async def start_http_server(self) -> str:
        """Serve a real FastMCP streamable-HTTP app on a free loopback port."""
        server = FastMCP("hub-pool-http")

        @server.tool()
        def header_check(expected: str) -> dict:
            """Report whether the request carried ``expected`` as a header value."""
            headers = get_http_headers(include_all=True) or {}
            return {
                "matched": expected in set(headers.values()),
                "authorization": headers.get("authorization") == expected,
                "names": sorted(headers.keys()),
            }

        @server.tool()
        def echo_cn() -> str:
            """Return Chinese over HTTP."""
            return "中文往返✅"

        app = server.http_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        instance = uvicorn.Server(config)
        task = asyncio.create_task(instance.serve())
        self._http.append((instance, task))
        for _ in range(400):
            if instance.started:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(instance.started, "uvicorn never started")
        port = instance.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/mcp"

    async def start_rejecting_http_server(self) -> str:
        """A real HTTP ``/mcp`` endpoint that refuses every request with 401.

        Its JSON-RPC error names the ``Authorization`` header it received, the way an upstream
        gateway that logs the bad credential does -- the exact leak the pool must redact.
        """

        async def reject(request):
            presented = request.headers.get("authorization", "")
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": f"invalid credential presented: {presented}"},
                }
            )
            return Response(body, status_code=401, media_type="application/json")

        app = Starlette(routes=[Route("/mcp", reject, methods=["GET", "POST", "DELETE"])])
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        instance = uvicorn.Server(config)
        task = asyncio.create_task(instance.serve())
        self._http.append((instance, task))
        for _ in range(400):
            if instance.started:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(instance.started, "uvicorn never started")
        port = instance.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/mcp"

    async def wait_dead(self, pid: int, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not pool_mod.pid_alive(pid):
                return True
            await asyncio.sleep(0.1)
        return not pool_mod.pid_alive(pid)

    async def wait_until(self, predicate, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return bool(predicate())

    @staticmethod
    def text_of(result: dict) -> str:
        for block in result.get("content") or []:
            if block.get("type") == "text":
                return block["text"]
        return ""


# =============================================================== helpers ==


class PoolHelperTests(unittest.TestCase):
    def test_clean_text_strips_ansi(self) -> None:
        raw = "\x1b[31mred\x1b[0m normal \x1b[2K\x1b[1;33mwarn\x1b[0m"
        self.assertEqual(clean_text(raw), "red normal warn")
        self.assertNotIn("\x1b", clean_text(raw))

    def test_error_result_shape(self) -> None:
        payload = error_result("unknown_server", "未知的 MCP 服务器：x", server="x")
        self.assertEqual(set(payload), {"ok", "error"})
        self.assertFalse(payload["ok"])
        self.assertEqual(set(payload["error"]), {"kind", "message", "hint", "server"})
        self.assertEqual(payload["error"]["kind"], "unknown_server")
        self.assertTrue(payload["error"]["hint"])

    def test_command_matches_tolerates_windows_quoting(self) -> None:
        recorded = r"C:\Python\python.exe C:\srv\server.py"
        actual = r'"C:\Python\python.exe" "C:\srv\server.py" --flag'
        self.assertTrue(pool_mod.command_matches(recorded, actual))
        self.assertFalse(pool_mod.command_matches(recorded, r"C:\Python\python.exe C:\other\thing.py"))

    def test_command_matches_requires_both_sides(self) -> None:
        self.assertFalse(pool_mod.command_matches("", "python srv.py"))
        self.assertFalse(pool_mod.command_matches("python srv.py", ""))

    def test_spec_secrets_are_meaningful_values_only(self) -> None:
        self.assertEqual(pool_mod.spec_secrets({}), [])
        self.assertEqual(pool_mod.spec_secrets({"headers": {"A": "1", "B": "y"}}), [],
                         "values shorter than SECRET_MIN_LEN are switches, not credentials")
        secrets = pool_mod.spec_secrets(
            {"headers": {"Authorization": "Bearer sk-abcdef"}, "env": {"TOKEN": "ghp_zzzz", "FLAG": "y"}}
        )
        self.assertEqual(sorted(secrets), sorted(["sk-abcdef", "ghp_zzzz", "Bearer sk-abcdef"]))
        self.assertEqual(secrets[0], "Bearer sk-abcdef", "longest first, so the whole header value goes")

    def test_scrub_secrets_replaces_every_occurrence(self) -> None:
        secrets = ["Bearer sk-abcdef", "sk-abcdef"]
        self.assertEqual(pool_mod.scrub_secrets("bad Bearer sk-abcdef here", secrets), "bad [REDACTED] here")
        self.assertEqual(pool_mod.scrub_secrets("sk-abcdef / sk-abcdef", ["sk-abcdef"]),
                         "[REDACTED] / [REDACTED]")
        self.assertEqual(pool_mod.scrub_secrets("nothing to hide", []), "nothing to hide")
        self.assertIsNone(pool_mod.scrub_secrets(None, secrets))


# ================================================= stdio long connection ==


class StdioSessionTests(_PoolCase):
    async def test_stateful_connection_utf8_and_env(self) -> None:
        script = self.server_script()
        pool = self.make_pool({"counter": self.stdio_spec(script, {"HUB_POOL_PROBE": "值-中文"})})

        counts = await pool.prefetch(budget_sec=30)
        self.assertEqual(counts, {"counter": 9})

        values = []
        for _ in range(3):
            result = await pool.call_tool("counter", "inc", wait_ms=30000)
            self.assertTrue(result["ok"], result)
            values.append(self.text_of(result))
        self.assertEqual(values, ["1", "2", "3"], "the counter must survive between calls")

        actor = pool.actors["counter"]
        server_pid = int(self.text_of(await pool.call_tool("counter", "pid")))
        self.assertEqual(server_pid, actor.pid, "the recorded pid must be the real child pid")
        self.assertEqual(len(actor._recorder.pids), 1, "three calls must reuse one subprocess")

        self.assertEqual(self.text_of(await pool.call_tool("counter", "cn")), "你好，世界")

        env = json.loads(self.text_of(await pool.call_tool("counter", "env_echo")))
        self.assertEqual(env["custom"], "值-中文", "spec['env'] must reach the child")
        self.assertTrue(env["path"], "the inherited system environment must not be dropped")
        self.assertTrue(env["systemRoot"], "SYSTEMROOT must survive the env whitelist")

        status = pool.list_servers()["servers"][0]
        self.assertEqual(status["status"], "connected")
        self.assertEqual(status["tool_count"], 9)
        self.assertEqual(status["pid"], server_pid)
        self.assertNotIn("headers", status)
        self.assertNotIn("env", status)

    async def test_prefetch_budget_does_not_kill_slow_dial(self) -> None:
        fast = self.server_script()
        slow = self.tmp / "hub_pool_slow_start.py"
        slow.write_text(
            "import time\n"
            "time.sleep(3.0)\n" + SERVER_SOURCE,
            encoding="utf-8",
        )
        pool = self.make_pool({"fast": self.stdio_spec(fast), "slow": self.stdio_spec(slow)},
                              output_dir=None, jobs_dir=None)

        # Deterministic warm server first, so the budget only has to cover the slow dial.
        self.assertTrue((await pool.list_tools("fast", timeout_ms=30000))["ok"])
        counts = await pool.prefetch(budget_sec=0.5)
        self.assertEqual(counts["fast"], 9)
        self.assertIsNone(counts["slow"], "a slow dial reports None instead of blocking")

        # The dial was not cancelled: the same actor still answers, with one child.
        tools = await pool.list_tools("slow", timeout_ms=60000)
        self.assertTrue(tools["ok"], tools)
        self.assertEqual(len(tools["tools"]), 9)
        actor = pool.actors["slow"]
        self.assertEqual(len(actor._recorder.pids), 1, "the warming connection must not be restarted")

    async def test_ping_and_tools_shapes(self) -> None:
        script = self.server_script()
        pool = self.make_pool({"counter": self.stdio_spec(script)})
        ping = await pool.ping_server("counter")
        self.assertEqual(ping["ok"], True)
        self.assertEqual(ping["toolCount"], 9)
        self.assertIsInstance(ping["durationMs"], int)

        tools = await pool.list_tools("counter")
        self.assertTrue(tools["ok"])
        names = {tool["name"] for tool in tools["tools"]}
        self.assertIn("inc", names)
        sample = next(tool for tool in tools["tools"] if tool["name"] == "slow")
        self.assertEqual(set(sample), {"name", "title", "description", "inputSchema"})
        self.assertEqual(sample["inputSchema"]["type"], "object")

        self.assertEqual((await pool.ping_server("nope"))["error"]["kind"], "unknown_server")
        self.assertEqual((await pool.list_tools("nope"))["error"]["kind"], "unknown_server")

    async def test_concurrent_calls_share_one_connection(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        await pool.prefetch(budget_sec=30)
        started = time.perf_counter()
        results = await asyncio.gather(
            *[pool.call_tool("counter", "slow", {"seconds": 0.4}, wait_ms=30000) for _ in range(3)]
        )
        self.assertLess(time.perf_counter() - started, 1.2, "three 0.4s calls must overlap")
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(len(pool.actors["counter"]._recorder.pids), 1,
                         "concurrent calls must not spawn extra servers")
        # The counter stays consistent under concurrency: three increments, values 1..3.
        values = [self.text_of(await pool.call_tool("counter", "inc")) for _ in range(3)]
        self.assertEqual(values, ["1", "2", "3"])

    async def test_tool_error_and_unknown_tool_contract(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        failed = await pool.call_tool("counter", "boom", wait_ms=30000)
        self.assertTrue(failed["ok"], "a tool level failure is a completed call, not a transport error")
        self.assertTrue(failed["isError"])
        self.assertEqual(failed["error"]["kind"], "tool_error")
        self.assertIn("boom from tool", self.text_of(failed))

        unknown = await pool.call_tool("counter", "no_such_tool")
        self.assertTrue(unknown["isError"])
        self.assertEqual(unknown["error"]["kind"], "unknown_tool")
        self.assertIn("tools", unknown["error"]["hint"])

        ok = await pool.call_tool("counter", "inc")
        self.assertFalse(ok["isError"])
        self.assertEqual(ok["structuredContent"], {"result": 1})

        self.assertEqual((await pool.call_tool("ghost", "inc"))["error"]["kind"], "unknown_server")
        self.assertEqual((await pool.call_tool("counter", ""))["error"]["kind"], "bad_request")


# ================================================ streamable HTTP + headers ==


class HttpSessionTests(_PoolCase):
    async def test_headers_utf8_and_shared_urls(self) -> None:
        url = await self.start_http_server()
        token = "SECRET-TOKEN-3344"
        auth = "Bearer SECRET-AUTH-1122"
        pool_a = self.make_pool(
            {"web": {"url": url, "headers": {"X-Hub-Token": token, "Authorization": auth}}},
            session_id="session-a",
        )
        pool_b = self.make_pool({"web": {"url": url}}, session_id="session-b")

        self.assertEqual(self.registry.shared_http_urls(), {url})

        status_a = pool_a.list_servers()["servers"][0]
        status_b = pool_b.list_servers()["servers"][0]
        self.assertEqual(status_a["transport"], "http")
        self.assertTrue(status_a["shared"])
        self.assertTrue(status_b["shared"])
        self.assertIsNone(status_a["pid"])

        checked = await pool_a.call_tool("web", "header_check", {"expected": token}, wait_ms=30000)
        self.assertTrue(checked["ok"], checked)
        payload = json.loads(self.text_of(checked))
        self.assertTrue(payload["matched"], "the custom header must reach the HTTP server")
        self.assertIn("x-hub-token", payload["names"])

        auth_checked = await pool_a.call_tool("web", "header_check", {"expected": auth}, wait_ms=30000)
        self.assertTrue(json.loads(self.text_of(auth_checked))["authorization"],
                        "the Authorization header must reach the HTTP server")

        self.assertEqual(self.text_of(await pool_b.call_tool("web", "echo_cn")), "中文往返✅")

        # Stateful across two calls on the same HTTP session.
        tools_first = await pool_a.list_tools("web")
        tools_second = await pool_a.list_tools("web")
        self.assertEqual([t["name"] for t in tools_first["tools"]], [t["name"] for t in tools_second["tools"]])
        self.assertEqual(pool_a.actors["web"].pid, None)

        # One live session is no longer "shared".
        await self.registry.close_session("session-b")
        self.assertEqual(self.registry.shared_http_urls(), set())


# ========================================================== timeout / jobs ==


class TimeoutAndJobTests(_PoolCase):
    async def test_timeout_returns_timeout_kind_and_connection_survives(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        # Warm the connection first: waitMs must bound the call, not the dial.
        await pool.prefetch(budget_sec=30)
        started = time.perf_counter()
        result = await pool.call_tool("counter", "slow", {"seconds": 2.0}, timeout_ms=500, wait_ms=8000)
        self.assertLess(time.perf_counter() - started, 1.6)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["kind"], "timeout")
        self.assertEqual(result["error"]["server"], "counter")
        # A timed out request must not poison the long-lived connection.
        followed = await pool.call_tool("counter", "inc")
        self.assertTrue(followed["ok"], followed)
        self.assertEqual(self.text_of(followed), "1")

    async def test_wait_ms_overflow_and_get_job(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        started = time.perf_counter()
        result = await pool.call_tool("counter", "slow", {"seconds": 1.5}, wait_ms=200)
        self.assertLess(time.perf_counter() - started, 1.2, "waitMs must not block for the whole call")
        self.assertEqual(result["status"], "running")
        self.assertRegex(result["job"], r"^job_[0-9a-f]+$")
        self.assertGreaterEqual(result["elapsedMs"], 0)

        done = await pool.get_job(result["job"], wait_ms=5000)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["job"], result["job"])
        self.assertTrue(done["ok"])
        self.assertEqual(self.text_of(done), "slept 1.50")
        self.assertTrue((self.jobs_dir / f"{result['job']}.json").exists(),
                        "a lazily awaited job must be persisted")

        unknown = await pool.get_job("job_deadbeef")
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["error"]["kind"], "bad_request")

    async def test_job_files_are_bounded(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script()), }, output_dir=None)
        for _ in range(24):
            await pool.call_tool("counter", "inc")
        files = [p for p in self.jobs_dir.iterdir() if p.suffix == ".json"]
        self.assertLessEqual(len(files), 20, "jobs_dir must not grow without bound")

    async def test_cancel_job(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        running = await pool.call_tool("counter", "slow", {"seconds": 5.0}, wait_ms=150, timeout_ms=8000)
        self.assertEqual(running["status"], "running")
        cancelled = await pool.cancel_job(running["job"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual((await pool.get_job(running["job"]))["status"], "cancelled")
        self.assertEqual((await pool.cancel_job("job_doesnotexist"))["status"], "not_found")
        # Cancelling a call must not kill the connection.
        self.assertTrue((await pool.call_tool("counter", "inc"))["ok"])


# ================================================================= batch ==


class BatchTests(_PoolCase):
    async def test_sequential_batch_keeps_order_and_counts(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        result = await pool.call_batch(
            [
                {"server": "counter", "tool": "inc"},
                {"server": "counter", "tool": "inc"},
                {"server": "counter", "tool": "boom"},
            ],
            wait_ms=30000,
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["okCount"], 2)
        self.assertEqual(result["errorCount"], 1)
        self.assertEqual([item["index"] for item in result["results"]], [0, 1, 2])

        first, second, failed = result["results"]
        self.assertEqual(self.text_of(first), "1")
        self.assertEqual(self.text_of(second), "2", "batch items must run in order on one connection")
        self.assertTrue(failed["isError"])
        self.assertIn("boom from tool", self.text_of(failed))
        self.assertEqual(failed["error"]["kind"], "tool_error")

    async def test_batch_validation(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        self.assertEqual((await pool.call_batch([]))["error"]["kind"], "bad_request")
        self.assertEqual(
            (await pool.call_batch([{"server": "ghost", "tool": "inc"}]))["results"][0]["error"]["kind"],
            "unknown_server",
        )
        oversized = [{"server": "counter", "tool": "inc"} for _ in range(51)]
        self.assertEqual((await pool.call_batch(oversized))["error"]["kind"], "bad_request")

    async def test_batch_overflow_to_job(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        running = await pool.call_batch(
            [{"server": "counter", "tool": "slow", "arguments": {"seconds": 1.2}}, {"server": "counter", "tool": "inc"}],
            wait_ms=200,
        )
        self.assertEqual(running["status"], "running")
        done = await pool.get_job(running["job"], wait_ms=5000)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["okCount"], 2)


# ================================================== output materialisation ==


class OutputTests(_PoolCase):
    async def test_large_result_is_truncated_and_saved(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        result = await pool.call_tool("counter", "big", wait_ms=30000)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["savedTo"])
        self.assertEqual(result["savedTo"], str(Path(result["savedTo"])))

        saved = json.loads(Path(result["savedTo"]).read_text(encoding="utf-8"))
        saved_text = saved["content"][0]["text"]
        self.assertIn("B" * BIG_TEXT_SIZE, saved_text, "the saved file keeps the whole result")
        self.assertNotIn("\x1b", saved_text, "saved text is ANSI-clean too")

        inline = json.dumps(result["content"], ensure_ascii=False)
        self.assertIn("truncated", inline)
        self.assertNotIn("\x1b[", inline)
        self.assertLess(len(inline), pool_mod.DEFAULT_MAX_INLINE_CHARS + 2000)

    async def test_image_is_saved_and_base64_never_inlined(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        result = await pool.call_tool("counter", "pic", wait_ms=30000)
        block = result["content"][0]
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["mimeType"], "image/png")
        self.assertGreater(block["bytes"], 0)
        self.assertIn("savedTo", block)
        self.assertNotIn(PNG_BASE64[:16], json.dumps(result["content"]),
                         "raw base64 must never reach the model")
        payload = Path(block["savedTo"]).read_bytes()
        self.assertTrue(payload.startswith(b"\x89PNG"))
        self.assertEqual(payload, __import__("base64").b64decode(PNG_BASE64))

    async def test_image_is_omitted_without_output_dir(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())}, output_dir=None)
        result = await pool.call_tool("counter", "pic")
        block = result["content"][0]
        self.assertTrue(block["omitted"])
        self.assertNotIn("savedTo", block)

    async def test_output_dir_stays_bounded(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        for _ in range(64):
            result = await pool.call_tool("counter", "pic", wait_ms=30000)
            self.assertTrue(result["content"][0].get("savedTo"), result)
        saved = [p for p in self.output_dir.iterdir() if p.is_file()]
        self.assertLessEqual(len(saved), pool_mod.OUTPUT_DIR_FILE_LIMIT)
        self.assertGreaterEqual(len(saved), pool_mod.OUTPUT_DIR_FILE_LIMIT - 1)

    async def test_large_result_without_output_dir_is_still_truncated(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())}, output_dir=None)
        result = await pool.call_tool("counter", "big", wait_ms=30000)
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["savedTo"])
        inline = json.dumps(result["content"], ensure_ascii=False)
        self.assertIn("truncated", inline)
        self.assertLess(len(inline), pool_mod.DEFAULT_MAX_INLINE_CHARS + 2000)


# ============================================================== reconnect ==


class ReconnectTests(_PoolCase):
    async def test_crashed_server_is_rebuilt_transparently(self) -> None:
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())})
        self.assertEqual(self.text_of(await pool.call_tool("counter", "inc")), "1")
        first_pid = pool.actors["counter"].pid
        self.assertTrue(first_pid)

        self.assertEqual(self.text_of(await pool.call_tool("counter", "die")), "bye")
        self.assertTrue(await self.wait_dead(first_pid, timeout=8), "the server should have exited")

        recovered = await pool.call_tool("counter", "inc", wait_ms=30000)
        self.assertTrue(recovered["ok"], recovered)
        second_pid = pool.actors["counter"].pid
        self.assertNotEqual(second_pid, first_pid, "a new subprocess must be spawned")
        self.assertEqual(self.text_of(recovered), "1", "the fresh process starts from zero")
        self.assertEqual(pool.actors["counter"].status, "connected")
        self.assertFalse(pool_mod.pid_alive(first_pid))

    async def test_bad_command_reports_connect_without_retrying_forever(self) -> None:
        pool = self.make_pool({"broken": {"command": str(self.tmp / "definitely-not-here.exe")}})
        started = time.perf_counter()
        result = await pool.call_tool("broken", "inc", wait_ms=20000, timeout_ms=20000)
        self.assertLess(time.perf_counter() - started, 15)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["kind"], "connect")
        self.assertEqual(pool.actors["broken"].status, "error")
        self.assertTrue(pool.actors["broken"].last_error)


# ========================================================== close hard budget ==


class CloseBudgetTests(_PoolCase):
    async def test_stubborn_child_does_not_stall_close_all(self) -> None:
        """A child that ignores EOF must neither hang the close nor survive it."""
        script = self.tmp / "hub_pool_stubborn.py"
        script.write_text(STUBBORN_SOURCE, encoding="utf-8")
        pool = self.make_pool({"stubborn": self.stdio_spec(script)}, session_id="stubborn",
                              output_dir=None, jobs_dir=None)

        dial = asyncio.create_task(pool.list_tools("stubborn", timeout_ms=20000))
        pid = None
        for _ in range(160):
            actor = pool.actors.get("stubborn")
            if actor is not None and actor.pid:
                pid = actor.pid
                break
            await asyncio.sleep(0.05)
        self.assertTrue(pid, "the stubborn child never spawned")
        self.assertTrue(pool_mod.pid_alive(pid))

        started = time.perf_counter()
        await self.registry.close_all(timeout_sec=0.8)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3.0, "close_all must honour its hard budget, not wait for the dial")
        dial.cancel()
        await asyncio.gather(dial, return_exceptions=True)

        self.assertTrue(await self.wait_dead(pid, timeout=10),
                        "a stubborn child left alive would leak on every stubborn session")
        ledger = json.loads((self.runtime_dir / "mcp_pool_pids.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["pids"], [])
        self.assertEqual(pool.status, "closed")

    async def test_close_session_honours_a_short_budget_and_kills_the_child(self) -> None:
        slow = self.tmp / "hub_pool_slow_dial_budget.py"
        slow.write_text("import time\ntime.sleep(4.0)\n" + SERVER_SOURCE, encoding="utf-8")
        pool = self.make_pool({"slow": self.stdio_spec(slow)}, session_id="budget",
                              output_dir=None, jobs_dir=None)
        dial = asyncio.create_task(pool.list_tools("slow", timeout_ms=20000))
        pid = None
        for _ in range(80):
            actor = pool.actors.get("slow")
            if actor is not None and actor.pid:
                pid = actor.pid
                break
            await asyncio.sleep(0.05)
        self.assertTrue(pid)
        started = time.perf_counter()
        self.assertTrue(await self.registry.close_session("budget", timeout_sec=0.8))
        self.assertLess(time.perf_counter() - started, 2.5,
                        "a 0.8s budget must not turn into an unbounded wait on the dial")
        dial.cancel()
        await asyncio.gather(dial, return_exceptions=True)
        self.assertTrue(await self.wait_dead(pid, timeout=10))


# ============================================================== pid ledger ==


class PidLedgerTests(_PoolCase):
    def ledger_pids(self) -> list:
        path = self.runtime_dir / "mcp_pool_pids.json"
        if not path.is_file():
            return []
        return [int(record["pid"]) for record in json.loads(path.read_text(encoding="utf-8"))["pids"]]

    async def test_the_pid_is_on_disk_before_the_handshake_finishes(self) -> None:
        """A Hub crash during a slow initialize must not leave an unaccounted child."""
        slow = self.tmp / "hub_pool_slow_handshake.py"
        slow.write_text("import time\ntime.sleep(6.0)\n" + SERVER_SOURCE, encoding="utf-8")
        pool = self.make_pool({"slow": self.stdio_spec(slow)}, session_id="slow-ledger",
                              output_dir=None, jobs_dir=None)

        dial = asyncio.create_task(pool.list_tools("slow", timeout_ms=20000))
        pid = None
        for _ in range(100):
            actor = pool.actors.get("slow")
            if actor is not None and actor.pid:
                pid = actor.pid
                break
            await asyncio.sleep(0.05)
        self.assertTrue(pid, "the child never spawned")
        self.assertEqual(self.ledger_pids(), [pid],
                         "the pid must be recorded the instant it exists, not after initialize")
        dial.cancel()
        await asyncio.gather(dial, return_exceptions=True)

    async def test_a_replaced_pool_keeps_its_pids_until_it_really_closed(self) -> None:
        script = self.server_script()
        first = self.make_pool({"counter": self.stdio_spec(script)}, session_id="dup")
        self.assertEqual(self.text_of(await first.call_tool("counter", "inc")), "1")
        pid = first.actors["counter"].pid
        self.assertTrue(pid)

        gate = asyncio.Event()
        original = first.close_all

        async def held(timeout_sec: float = 5.0) -> None:
            await gate.wait()
            await original(timeout_sec)

        first.close_all = held  # type: ignore[method-assign]
        second = self.make_pool({"counter": self.stdio_spec(script)}, session_id="dup")
        self.assertIs(self.registry.get("dup"), second)
        self.assertIn(pid, self.ledger_pids(),
                      "a replaced pool still closing must keep its pid in the ledger")

        gate.set()
        self.assertTrue(await self.wait_dead(pid, timeout=15))
        self.assertTrue(await self.wait_until(lambda: pid not in self.ledger_pids(), timeout=15),
                        "the pid leaves the ledger only once the old pool really closed")


# ========================================================= shutdown / pids ==


class ShutdownAndPidTests(_PoolCase):
    async def test_close_session_kills_children_and_clears_ledger(self) -> None:
        script = self.server_script()
        pool = self.make_pool(
            {"one": self.stdio_spec(script), "two": self.stdio_spec(script)}, session_id="closing"
        )
        await pool.call_tool("one", "inc")
        await pool.call_tool("two", "inc")
        pids = sorted(actor.pid for actor in pool.actors.values() if actor.pid)
        self.assertEqual(len(pids), 2)

        ledger = json.loads((self.runtime_dir / "mcp_pool_pids.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(record["pid"] for record in ledger["pids"]), pids)
        self.assertEqual({record["session_id"] for record in ledger["pids"]}, {"closing"})
        self.assertEqual({record["server"] for record in ledger["pids"]}, {"one", "two"})
        self.assertTrue(all(record["command"] for record in ledger["pids"]))

        self.assertTrue(await self.registry.close_session("closing"))
        for pid in pids:
            self.assertTrue(await self.wait_dead(pid), f"pid {pid} leaked")
        cleared = json.loads((self.runtime_dir / "mcp_pool_pids.json").read_text(encoding="utf-8"))
        self.assertEqual(cleared["pids"], [])
        self.assertFalse(await self.registry.close_session("closing"))

    async def test_close_all_leaves_no_children(self) -> None:
        script = self.server_script()
        pool = self.make_pool({"one": self.stdio_spec(script)}, session_id="a")
        other = self.make_pool({"two": self.stdio_spec(script)}, session_id="b")
        await pool.call_tool("one", "inc")
        await other.call_tool("two", "inc")
        pids = [pool.actors["one"].pid, other.actors["two"].pid]
        self.assertTrue(all(pids))

        await self.registry.close_all()
        for pid in pids:
            self.assertTrue(await self.wait_dead(pid), f"pid {pid} leaked after close_all")
        self.assertEqual(pool.status, "closed")
        self.assertEqual(other.status, "closed")
        ledger = json.loads((self.runtime_dir / "mcp_pool_pids.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["pids"], [])
        self.assertEqual((await pool.call_tool("one", "inc"))["error"]["kind"], "session_closed")

    async def test_close_while_a_slow_dial_is_in_flight_leaves_no_process(self) -> None:
        slow = self.tmp / "hub_pool_slow_dial.py"
        slow.write_text("import time\ntime.sleep(4.0)\n" + SERVER_SOURCE, encoding="utf-8")
        pool = self.make_pool({"slow": self.stdio_spec(slow)}, output_dir=None, jobs_dir=None)

        dial = asyncio.create_task(pool.list_tools("slow", timeout_ms=20000))
        pid = None
        for _ in range(80):
            actor = pool.actors.get("slow")
            if actor is not None and actor._recorder.pids:
                pid = actor._recorder.pids[0]
                break
            await asyncio.sleep(0.05)
        self.assertTrue(pid, "the dial must have spawned the subprocess")
        self.assertTrue(pool_mod.pid_alive(pid))

        started = time.perf_counter()
        await self.registry.close_session("s1", timeout_sec=0.5)
        self.assertLess(time.perf_counter() - started, 6.0, "closing must not wait for the handshake")
        dial.cancel()
        await asyncio.gather(dial, return_exceptions=True)
        self.assertTrue(await self.wait_dead(pid, timeout=10), "a cancelled dial must not leave an orphan")

    async def test_leftover_kill_refuses_a_pid_that_now_belongs_to_something_else(self) -> None:
        """A recycled Windows pid must never cost an unrelated process its life."""
        victim = subprocess.Popen(
            [PY, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def clean_up() -> None:
            if victim.poll() is None:
                victim.kill()
            victim.wait(timeout=10)

        self.addCleanup(clean_up)
        self.assertTrue(await self.wait_until(lambda: pool_mod.pid_alive(victim.pid)), "victim never started")

        actor = ServerActor("counter", self.stdio_spec(self.server_script()), cwd=str(self.tmp))
        actor.status = "connected"
        actor.pid = victim.pid  # the pid the actor once recorded, now reused by an innocent process

        started = time.perf_counter()
        await actor.close()
        self.assertLess(time.perf_counter() - started, 10.0)
        await asyncio.sleep(0.2)
        self.assertTrue(pool_mod.pid_alive(victim.pid),
                        "close() killed an unrelated process whose pid only looked familiar")
        self.assertEqual(actor.status, "closed")

    async def test_leftover_kill_still_takes_its_own_child(self) -> None:
        """The ownership check must not turn the net off for the actor's real child."""
        pool = self.make_pool({"counter": self.stdio_spec(self.server_script())}, session_id="owner")
        self.assertEqual(self.text_of(await pool.call_tool("counter", "inc")), "1")
        actor = pool.actors["counter"]
        pid = actor.pid
        self.assertTrue(pid)
        await actor.close()
        self.assertTrue(await self.wait_dead(pid), "the actor's own child must still be killed")
        self.assertEqual(actor.status, "closed")

    async def test_sweep_orphan_pids_only_kills_matching_command_lines(self) -> None:
        sleeper_a = self.tmp / "sleeper_alpha.py"
        sleeper_b = self.tmp / "sleeper_beta.py"
        sleeper_a.write_text(SLEEPER_SOURCE, encoding="utf-8")
        sleeper_b.write_text(SLEEPER_SOURCE, encoding="utf-8")
        proc_a = subprocess.Popen(
            [PY, str(sleeper_a), "--tag", "alpha"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc_b = subprocess.Popen(
            [PY, str(sleeper_b), "--tag", "beta"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: [proc.poll() is None and proc.kill() for proc in (proc_a, proc_b)])
        try:
            self.assertTrue(pool_mod.pid_alive(proc_a.pid))
            self.assertTrue(pool_mod.pid_alive(proc_b.pid))

            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            (self.runtime_dir / "mcp_pool_pids.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "hubPid": 1234,
                        "writtenAt": "2026-01-01T00:00:00+00:00",
                        "pids": [
                            {
                                "pid": proc_a.pid,
                                "session_id": "old-session",
                                "server": "alpha",
                                "command": f"{PY} {sleeper_a}",
                            },
                            {
                                "pid": proc_b.pid,
                                "session_id": "old-session",
                                "server": "beta",
                                "command": r"C:\Program Files\Other\server.exe --port 1234",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            killed = self.registry.sweep_orphan_pids()
            self.assertEqual(killed, 1)
            self.assertTrue(await self.wait_dead(proc_a.pid), "the matching orphan must be killed")
            await asyncio.sleep(0.2)
            self.assertTrue(pool_mod.pid_alive(proc_b.pid), "a pid whose command line does not match must survive")
            cleared = json.loads((self.runtime_dir / "mcp_pool_pids.json").read_text(encoding="utf-8"))
            self.assertEqual(cleared["pids"], [], "the ledger is cleared after a sweep")
        finally:
            for proc in (proc_a, proc_b):
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)

    async def test_reap_idle_closes_only_idle_connections(self) -> None:
        pool = SessionPool(
            "reap",
            {"counter": self.stdio_spec(self.server_script())},
            cwd=str(self.tmp),
            idle_timeout_sec=0.001,
        )
        self.registry._pools["reap"] = pool
        await pool.call_tool("counter", "inc")
        pid = pool.actors["counter"].pid
        await asyncio.sleep(0.05)
        self.assertEqual(await pool.reap_idle(), 1)
        self.assertNotIn("counter", pool.actors)
        self.assertTrue(await self.wait_dead(pid))
        self.assertEqual(pool.list_servers()["servers"][0]["status"], "idle")


# ========================================================== secret hygiene ==


class SecretHygieneTests(_PoolCase):
    async def test_secrets_never_appear_in_status_errors_or_events(self) -> None:
        env_secret = "SECRET-ENV-998877"
        header_secret = "SECRET-HEADER-5566"
        secrets = (env_secret, header_secret, "Bearer SECRET-AUTH-1122")
        events: list = []
        url = await self.start_http_server()

        pool = self.make_pool(
            {
                "local": self.stdio_spec(self.server_script(), {"HUB_POOL_PROBE": "值-中文", "HUB_SECRET": env_secret}),
                "web": {"url": url, "headers": {"X-Hub-Token": header_secret, "Authorization": secrets[2]}},
            },
            on_call_event=events.append,
        )
        await pool.prefetch(budget_sec=30)
        await pool.call_tool("local", "inc")
        await pool.call_tool("web", "echo_cn")
        await pool.call_tool("local", "nosuch")

        snapshots = json.dumps(pool.list_servers(shared_urls=self.registry.shared_http_urls()), ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, snapshots)
        self.assertNotIn("headers", snapshots)
        self.assertNotIn("command", snapshots)

        errors = json.dumps(
            [
                await pool.call_tool("local", "nosuch"),
                await pool.call_tool("web", "missing_tool"),
                await pool.call_tool("local", "boom"),
                await pool.handle_action({"action": "ping", "server": "ghost"}),
            ],
            ensure_ascii=False,
        )
        for secret in secrets:
            self.assertNotIn(secret, errors)

        event_dump = json.dumps(events, ensure_ascii=False)
        self.assertTrue(events)
        for secret in secrets:
            self.assertNotIn(secret, event_dump)
        self.assertEqual(
            {"sessionId", "server", "tool", "ok", "isError", "durationMs", "errorKind", "at"},
            {"sessionId", "server", "tool", "ok", "isError", "durationMs", "errorKind", "at"} & set(events[0]),
        )
        self.assertTrue(all(event["sessionId"] == "s1" for event in events))

        for actor in pool.actors.values():
            self.assertNotIn(env_secret, repr(actor))
            self.assertNotIn(header_secret, repr(actor))

    async def test_http_error_echoing_the_authorization_header_is_redacted(self) -> None:
        """A 401 body that names the presented credential must never reach the caller."""
        header_secret = "Bearer SECRET-HDR-9f3a-4242"
        events: list = []
        url = await self.start_rejecting_http_server()
        pool = self.make_pool(
            {"gateway": {"url": url, "headers": {"Authorization": header_secret}}},
            session_id="leaky-http",
            on_call_event=events.append,
        )

        tools = await pool.list_tools("gateway", timeout_ms=20000)
        self.assertFalse(tools["ok"], tools)
        self.assertIn("[REDACTED]", tools["error"]["message"], tools)
        self.assertNotIn(header_secret, json.dumps(tools, ensure_ascii=False))
        self.assertNotIn("SECRET-HDR-9f3a-4242", json.dumps(tools, ensure_ascii=False),
                         "the bare token body must be redacted too, not only the Bearer prefix")

        status = pool.list_servers()["servers"][0]
        self.assertEqual(status["status"], "error")
        self.assertIn("[REDACTED]", status["last_error"] or "", status)
        self.assertNotIn("SECRET-HDR-9f3a-4242", json.dumps(status, ensure_ascii=False))

        called = await pool.call_tool("gateway", "anything", wait_ms=20000)
        self.assertFalse(called["ok"], called)
        self.assertIn("[REDACTED]", called["error"]["message"])
        self.assertNotIn("SECRET-HDR-9f3a-4242", json.dumps(called, ensure_ascii=False))
        self.assertNotIn("SECRET-HDR-9f3a-4242", json.dumps(events, ensure_ascii=False))

    async def test_stdio_tool_error_echoing_an_env_secret_is_redacted(self) -> None:
        """An env credential a stdio server echoes in a tool error must not surface either."""
        env_secret = "SECRET-ENV-998877"
        events: list = []
        script = self.tmp / "hub_pool_leaky.py"
        script.write_text(LEAKY_SERVER_SOURCE, encoding="utf-8")
        pool = self.make_pool(
            {"local": self.stdio_spec(script, {"HUB_SECRET": env_secret, "SWITCH": "on"})},
            session_id="leaky-stdio",
            on_call_event=events.append,
        )

        called = await pool.call_tool("local", "leak", wait_ms=30000)
        self.assertTrue(called["ok"], called)
        self.assertTrue(called["isError"])
        self.assertIn("[REDACTED]", called["error"]["message"], called)
        self.assertNotIn(env_secret, json.dumps(called, ensure_ascii=False))

        # Writing last_error elsewhere still yields a scrubbed view.
        actor = pool.actors["local"]
        actor.last_error = f"handshake refused: {env_secret}"
        status = pool.list_servers()["servers"][0]
        self.assertIn("[REDACTED]", status["last_error"] or "")
        self.assertNotIn(env_secret, json.dumps(pool.list_servers(), ensure_ascii=False))

        self.assertTrue(events)
        self.assertNotIn(env_secret, json.dumps(events, ensure_ascii=False))
        # The connection and a healthy tool are untouched by the redaction.
        self.assertEqual(self.text_of(await pool.call_tool("local", "healthy")), "fine")


# ========================================================= registry / reaper ==


class RegistryLifecycleTests(_PoolCase):
    async def test_registering_replaces_and_closes_the_previous_pool(self) -> None:
        script = self.server_script()
        first = self.make_pool({"counter": self.stdio_spec(script)}, session_id="dup")
        await first.call_tool("counter", "inc")
        first_pid = first.actors["counter"].pid

        second = self.make_pool({"counter": self.stdio_spec(script)}, session_id="dup")
        self.assertIs(self.registry.get("dup"), second)
        self.assertTrue(await self.wait_dead(first_pid), "the replaced pool must release its subprocess")
        self.assertTrue(await self.wait_until(lambda: first.status == "closed"))

        self.assertEqual(self.text_of(await second.call_tool("counter", "inc")), "1")
        self.assertEqual(second.actors["counter"].status, "connected")

    async def test_reaper_closes_idle_connections(self) -> None:
        registry = PoolRegistry(self.runtime_dir / "reaper", idle_reap_interval_sec=0.2)
        pool = SessionPool(
            "reaper",
            {"counter": self.stdio_spec(self.server_script())},
            cwd=str(self.tmp),
            idle_timeout_sec=0.1,
        )
        registry._pools["reaper"] = pool
        try:
            self.assertEqual(self.text_of(await pool.call_tool("counter", "inc")), "1")
            pid = pool.actors["counter"].pid
            registry.start_reaper()
            self.assertTrue(await self.wait_dead(pid, timeout=10), "the reaper must close the idle connection")
            self.assertTrue(
                await self.wait_until(lambda: "counter" not in pool.actors),
                "the reaped actor must be dropped so the next call dials again",
            )
            self.assertEqual(pool.list_servers()["servers"][0]["status"], "idle")
        finally:
            registry.stop_reaper()
            await registry.close_all()


# ======================================================== action contract ==


class HandleActionTests(_PoolCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.url = await self.start_http_server()
        self.pool = self.make_pool(
            {"counter": self.stdio_spec(self.server_script()), "web": {"url": self.url}},
            session_id="actions",
        )

    async def test_list_tools_ping(self) -> None:
        listing = await self.pool.handle_action({"action": "list"})
        self.assertTrue(listing["ok"])
        self.assertEqual({server["name"] for server in listing["servers"]}, {"counter", "web"})
        self.assertIn("shared", listing["servers"][0])

        ping = await self.pool.handle_action({"action": "ping", "server": "counter", "timeoutMs": 30000})
        self.assertEqual(ping["toolCount"], 9)

        tools = await self.pool.handle_action({"action": "tools", "server": "counter"})
        self.assertTrue(tools["ok"])
        self.assertTrue(tools["tools"])

    async def test_call_and_bad_requests(self) -> None:
        called = await self.pool.handle_action(
            {"action": "call", "server": "counter", "tool": "inc", "arguments": {}, "waitMs": 30000}
        )
        self.assertEqual(called["status"], "done")
        self.assertEqual(self.text_of(called), "1")

        missing_tool = await self.pool.handle_action({"action": "call", "server": "counter"})
        self.assertEqual(missing_tool["error"]["kind"], "bad_request")

        bad_wait = await self.pool.handle_action(
            {"action": "call", "server": "counter", "tool": "inc", "waitMs": "30"}
        )
        self.assertEqual(bad_wait["error"]["kind"], "bad_request")

        unknown_server = await self.pool.handle_action({"action": "call", "server": "ghost", "tool": "inc"})
        self.assertEqual(unknown_server["error"]["kind"], "unknown_server")

        unknown_action = await self.pool.handle_action({"action": "teleport"})
        self.assertEqual(unknown_action["error"]["kind"], "bad_request")
        self.assertIn("list", unknown_action["error"]["hint"])

        not_a_dict = await self.pool.handle_action(["action", "list"])  # type: ignore[arg-type]
        self.assertEqual(not_a_dict["error"]["kind"], "bad_request")

        cancel = await self.pool.handle_action({"action": "cancel", "job": "job_000000000000"})
        self.assertEqual(cancel["status"], "not_found")

    async def test_handle_action_never_raises(self) -> None:
        weird = await self.pool.handle_action(
            {"action": "call", "server": "counter", "tool": "inc", "arguments": "not-an-object"}
        )
        self.assertEqual(weird["error"]["kind"], "bad_request")
        empty = await self.pool.handle_action({})
        self.assertEqual(empty["error"]["kind"], "bad_request")

    async def test_batch_action(self) -> None:
        result = await self.pool.handle_action(
            {
                "action": "batch",
                "calls": [
                    {"server": "counter", "tool": "inc"},
                    {"server": "counter", "tool": "boom"},
                ],
                "waitMs": 30000,
            }
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual((result["okCount"], result["errorCount"]), (1, 1))
        self.assertEqual(self.text_of(result["results"][0]), "1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
