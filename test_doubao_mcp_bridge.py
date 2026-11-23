"""The Doubao TUI MCP bridge, end to end.

Nothing in the connection path is mocked: a real ``HubSessionManager``, the real FastAPI app (served
by a real uvicorn on a random loopback port for the CLI cases), real ``fastmcp.FastMCP`` servers
(stdio subprocess and streamable HTTP) and the real ``tools/hub_mcp_cli.py`` child process.  Only the
PTY is patched away, because no terminal is needed to prove what the bridge does.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
import uvicorn
from fastmcp import FastMCP

import mcp_session_pool as pool_mod
from session_manager import AgentSession, HubSessionManager

ROOT = Path(__file__).resolve().parent
CLI = ROOT / "tools" / "hub_mcp_cli.py"
PY = sys.executable or "python"

# The same shape as a user's stdio MCP server: one stateful tool, one slow tool, UTF-8 and types.
SERVER_SOURCE = r'''"""Test MCP server: stateful counter, slow tool, UTF-8 and an argument type echo."""
import asyncio
import os

from fastmcp import FastMCP

mcp = FastMCP("doubao-bridge-test")
_state = {"count": 0}


@mcp.tool()
def inc() -> int:
    """Increment the in-process counter and return the new value."""
    _state["count"] += 1
    return _state["count"]


@mcp.tool()
def echo(text: str) -> str:
    """Return the text it was given."""
    return text


@mcp.tool()
def kind_of(value: str) -> str:
    """Report the received argument's Python type and value, so a numeric-looking id is visible."""
    return "%s:%s" % (type(value).__name__, value)


@mcp.tool()
async def slow(seconds: float) -> str:
    """Sleep for ``seconds`` and then answer."""
    await asyncio.sleep(float(seconds))
    return "slept %.2f" % float(seconds)


@mcp.tool()
def pid() -> int:
    """Return the server process pid."""
    return os.getpid()


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
'''


def run_cli(arguments, timeout=90):
    """Run the real CLI in a child process with a clean HUB_MCP_* environment."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("HUB_MCP_")}
    proc = subprocess.run([PY, str(CLI), *arguments], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=environment)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = None
    return proc.returncode, payload, proc


async def _return_none():
    return None


class _BridgeCase(unittest.IsolatedAsyncioTestCase):
    """A temp Hub whose PATY never starts, plus helpers that drive the real bridge."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self._temp = tempfile.TemporaryDirectory(prefix="doubao-bridge-")
        self.root = Path(self._temp.name)
        self.workspace = self.root / "work"
        self.workspace.mkdir()
        self.script = self.workspace / "bridge_server.py"
        self.script.write_text(SERVER_SOURCE, encoding="utf-8")
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=ROOT, data_dir=self.root)
        await self.hub.start()
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")
        self.hub_patch = patch.object(self.server, "hub", self.hub)
        self.hub_patch.start()
        self.hub.profiles["doubao"] = {
            "name": "doubao", "engine": "doubao", "enabled": True, "kind": "tui",
            "command": "unused", "args": ["--profile-arg"], "task_mode": "pty_input",
        }
        self._servers = []
        self._env_before = os.environ.get("HUB_API_URL")
        self.injected = []
        self.noop_watches = [
            patch.object(self.hub, "_watch_doubao_session", self._noop_watch),
            patch.object(self.hub, "_watch_tui_interactive_prompts", self._noop_watch),
            patch.object(self.hub, "_auto_handle_tui_startup_and_trust", self._recording_watch),
        ]
        for item in self.noop_watches:
            item.start()

    async def asyncTearDown(self):
        for item in self.noop_watches:
            item.stop()
        self.hub_patch.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        for instance, task in self._servers:
            instance.should_exit = True
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._env_before is None:
            os.environ.pop("HUB_API_URL", None)
        else:
            os.environ["HUB_API_URL"] = self._env_before
        shutil.rmtree(self.root, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def _noop_watch(self, *args, **kwargs):
        return _return_none()

    def _recording_watch(self, *args, **kwargs):
        self.injected.append(kwargs)
        return _return_none()

    def servers(self, *, with_http: bool = False, http_url: str = "") -> dict:
        specs = {"counter": {"command": PY, "args": [str(self.script)]}}
        if with_http:
            specs["docs"] = {"url": http_url, "headers": {"Authorization": "Bearer upstream-secret"}}
        return specs

    def bridge_file(self, session_id: str) -> Path:
        return self.hub.runtime_configs_dir / f"mcp_{session_id}" / "bridge.json"

    def bridge_payload(self, session_id: str) -> dict:
        return json.loads(self.bridge_file(session_id).read_text(encoding="utf-8"))

    def bridge_dirs(self) -> list:
        root = self.hub.runtime_configs_dir
        return [p for p in root.iterdir() if p.is_dir() and p.name.startswith("mcp_")]

    async def start_hub_server(self) -> str:
        """Serve the real Hub app on a random loopback port (no lifespan: the Hub is already up)."""
        config = uvicorn.Config(self.server.app, host="127.0.0.1", port=0, log_level="error",
                                lifespan="off")
        instance = uvicorn.Server(config)
        task = asyncio.create_task(instance.serve())
        self._servers.append((instance, task))
        for _ in range(500):
            if instance.started:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(instance.started, "the Hub's uvicorn never started")
        return f"http://127.0.0.1:{instance.servers[0].sockets[0].getsockname()[1]}"

    async def start_mcp_http_server(self) -> str:
        """Serve a real FastMCP streamable-HTTP server on a random loopback port."""
        server = FastMCP("doubao-bridge-http")

        @server.tool()
        def echo_cn() -> str:
            """Return Chinese over HTTP."""
            return "中文往返✅"

        config = uvicorn.Config(server.http_app(), host="127.0.0.1", port=0, log_level="error")
        instance = uvicorn.Server(config)
        task = asyncio.create_task(instance.serve())
        self._servers.append((instance, task))
        for _ in range(500):
            if instance.started:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(instance.started, "the MCP HTTP server never started")
        return f"http://127.0.0.1:{instance.servers[0].sockets[0].getsockname()[1]}/mcp"

    async def create_bridged(self, *, task: str = "检查 MCP 桥接", servers=None, **kwargs):
        with patch.object(AgentSession, "start_pty"):
            return await self.hub.create_session(
                "doubao", task=task, cwd=str(self.workspace),
                mcp_servers=servers or self.servers(), **kwargs)

    async def wait_until(self, predicate, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return bool(predicate())

    @staticmethod
    def text_of(result: dict) -> str:
        for block in result.get("content") or ():
            if block.get("type") == "text":
                return block["text"]
        return ""


# ==================================================== preview / first turn ==


class PreviewAndSpawnTests(_BridgeCase):
    async def test_preview_is_pure_and_creates_no_pool(self):
        servers = self.servers()
        preview = self.hub.resolve_launch("doubao", cwd=str(self.workspace), mcp_servers=servers)
        self.assertTrue(preview["args"])
        # No MCP flag may ever reach doubao.exe.
        for arg in preview["args"]:
            self.assertFalse(arg.startswith("--mcp"), arg)
            self.assertNotEqual(arg, "--strict-mcp-config")
            self.assertNotIn("mcp_servers", arg)
        self.assertEqual(preview["mcp_servers"], servers)
        self.assertTrue(any("Hub 会话级连接池" in note for note in preview["notes"]))
        # Purity: a preview writes no bridge file and registers no pool.
        self.assertEqual(self.hub.mcp_pool_registry.sessions(), [])
        self.assertEqual(self.bridge_dirs(), [])
        self.assertFalse((self.hub.data_dir / ".mcp_token_secret").exists())
        self.assertEqual(self.hub.sessions, {})

    async def test_create_session_mounts_pool_and_first_turn_note(self):
        servers = self.servers()
        sess = await self.create_bridged(servers=servers)

        bridge = self.bridge_file(sess.session_id)
        self.assertTrue(bridge.is_file())
        payload = self.bridge_payload(sess.session_id)
        self.assertEqual(set(payload), {"hub_url", "session_id", "token", "output_dir"})
        self.assertEqual(payload["session_id"], sess.session_id)
        text = bridge.read_text(encoding="utf-8")
        for secret in ("upstream-secret", "Authorization", "Bearer"):
            self.assertNotIn(secret, text)
        self.assertIsNotNone(self.hub.mcp_pool_registry.get(sess.session_id))

        # The worker's first text is the note plus the parent's own task, unchanged.
        self.assertEqual(len(self.injected), 1)
        injected = self.injected[0]["task_to_inject"]
        self.assertTrue(injected.startswith("[Hub MCP]"), injected[:200])
        self.assertIn("检查 MCP 桥接", injected)
        self.assertIn(str(CLI), injected)
        # Title, last_task and the activity keep the raw task.
        self.assertNotIn("[Hub MCP]", sess.last_task)
        self.assertNotIn("[Hub MCP]", sess.current_activity)
        self.assertNotIn("[Hub MCP]", sess.title)
        self.assertEqual(sess.last_task, "检查 MCP 桥接")

        # Snapshot keeps the MCP fields; argv stays MCP-free.
        self.assertEqual(sess.profile_snapshot["mcp_servers"], servers)
        self.assertFalse([a for a in sess.args if a.startswith("--mcp") or "mcp_servers" in a])

        # The bridge itself answers before any CLI is involved: one real stdio server, connected.
        pool = self.hub.mcp_pool_registry.get(sess.session_id)
        self.assertIsNotNone(pool.actors.get("counter"))

    async def test_cli_round_trip_over_real_http(self):
        http_url = await self.start_mcp_http_server()
        hub_url = await self.start_hub_server()
        os.environ["HUB_API_URL"] = hub_url
        sess = await self.create_bridged(servers=self.servers(with_http=True, http_url=http_url))
        bridge = str(self.bridge_file(sess.session_id))

        code, payload, proc = await asyncio.to_thread(run_cli, ["--bridge", bridge, "list"])
        self.assertEqual(code, 0, proc.stderr)
        names = {item["name"]: item for item in payload["servers"]}
        self.assertEqual(names["counter"]["transport"], "stdio")
        self.assertEqual(names["docs"]["transport"], "http")
        self.assertNotIn("upstream-secret", json.dumps(payload, ensure_ascii=False))

        code, payload, _ = await asyncio.to_thread(run_cli, ["--bridge", bridge, "ping", "counter"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["toolCount"], 5)

        code, payload, _ = await asyncio.to_thread(run_cli, ["--bridge", bridge, "tools", "counter"])
        self.assertEqual(code, 0)
        tool_names = {item["name"] for item in payload["tools"]}
        self.assertEqual(tool_names, {"inc", "echo", "kind_of", "slow", "pid"})

        # Three stateful calls on one connection: the counter keeps counting in the same process.
        values = []
        for _ in range(3):
            code, payload, _ = await asyncio.to_thread(
                run_cli, ["--bridge", bridge, "call", "counter", "inc"])
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            values.append(self.text_of(payload))
        self.assertEqual(values, ["1", "2", "3"], "the stdio server must keep its state")

        code, payload, _ = await asyncio.to_thread(
            run_cli, ["--bridge", bridge, "call", "counter", "pid"])
        pid = int(self.text_of(payload))
        self.assertTrue(pool_mod.pid_alive(pid), "the CLI reached a live server process")

        # UTF-8 round trip and a numeric-looking --arg that must stay a string.
        code, payload, _ = await asyncio.to_thread(
            run_cli, ["--bridge", bridge, "call", "counter", "echo", "--arg", "text=你好，世界"])
        self.assertEqual(code, 0)
        self.assertEqual(self.text_of(payload), "你好，世界")
        code, payload, _ = await asyncio.to_thread(
            run_cli, ["--bridge", bridge, "call", "counter", "kind_of", "--arg", "value=00123"])
        self.assertEqual(code, 0)
        self.assertEqual(self.text_of(payload), "str:00123")

        # The HTTP server answers too, through the same bridge.
        code, payload, _ = await asyncio.to_thread(
            run_cli, ["--bridge", bridge, "call", "docs", "echo_cn"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.text_of(payload), "中文往返✅")

        # batch
        batch_file = self.hub.runtime_configs_dir / f"mcp_{sess.session_id}" / "outputs" / "batch.json"
        batch_file.parent.mkdir(parents=True, exist_ok=True)
        batch_file.write_text(json.dumps({"calls": [
            {"server": "counter", "tool": "inc", "arguments": {}},
            {"server": "counter", "tool": "echo", "arguments": {"text": "批"}},
        ]}, ensure_ascii=False), encoding="utf-8")
        code, payload, _ = await asyncio.to_thread(
            run_cli, ["--bridge", bridge, "batch", "--file", str(batch_file)])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["results"]), 2)

        # A slow tool overflows the wait: exit 2 and a job id, then `job` finishes it.
        code, payload, _ = await asyncio.to_thread(
            run_cli, ["--bridge", bridge, "call", "counter", "slow",
                      "--arg-json", "seconds=5", "--wait", "1"])
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["status"], "running")
        job = payload["job"]
        self.assertTrue(job)
        code, payload, _ = await asyncio.to_thread(
            run_cli, ["--bridge", bridge, "job", job, "--wait", "30"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(self.text_of(payload), "slept 5.00")

        # Progress events reached record_worker_event, and they carry no secret.
        def has_event():
            return any(e.get("type") == "mcp_bridge_call"
                       for e in self.hub.get_worker_events(session_id=sess.session_id))
        self.assertTrue(await self.wait_until(has_event), "no mcp_bridge_call event was recorded")
        events = [e for e in self.hub.get_worker_events(session_id=sess.session_id)
                  if e.get("type") == "mcp_bridge_call"]
        dumped = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("upstream-secret", dumped)
        self.assertNotIn("你好，世界", dumped)
        self.assertEqual(events[-1]["category"], "progress")
        self.assertEqual({"server", "tool", "ok", "durationMs"} & set(events[-1]["data"]),
                          {"server", "tool", "ok", "durationMs"})
        self.assertTrue(any(e["data"].get("tool") == "echo" for e in events))

    async def test_auth_and_close(self):
        hub_url = await self.start_hub_server()
        os.environ["HUB_API_URL"] = hub_url
        sess = await self.create_bridged(servers=self.servers())
        pool = self.hub.mcp_pool_registry.get(sess.session_id)
        bridge = self.bridge_payload(sess.session_id)
        url = f"{hub_url}/api/sessions/{sess.session_id}/mcp"

        async with httpx.AsyncClient() as client:
            self.assertEqual((await client.post(url, json={"action": "list"})).status_code, 401)
            wrong = await client.post(url, json={"action": "list"},
                                      headers={"X-Hub-Mcp-Token": "0" * 64})
            self.assertEqual(wrong.status_code, 401)
            self.assertEqual(wrong.json()["error"]["kind"], "auth")
            good = await client.post(url, json={"action": "list"},
                                     headers={"X-Hub-Mcp-Token": bridge["token"]})
            self.assertEqual(good.status_code, 200)
            self.assertTrue(good.json()["ok"])
            bad_body = await client.post(url, content=b"not json",
                                         headers={"X-Hub-Mcp-Token": bridge["token"],
                                                  "Content-Type": "application/json"})
            self.assertEqual(bad_body.status_code, 400)
            self.assertEqual(bad_body.json()["error"]["kind"], "bad_request")
            bad_wait = await client.get(
                f"{url}/jobs/nope", params={"wait_ms": 999999},
                headers={"X-Hub-Mcp-Token": bridge["token"]})
            self.assertEqual(bad_wait.status_code, 422)  # pydantic bound, not a 200 with a lie

        # The prefetch is best effort (2 s), so the child may be recorded a moment later; the close
        # must take it with the session either way.
        self.assertTrue(await self.wait_until(lambda: pool.actors["counter"].pid, timeout=15),
                        "the stdio server never connected")
        pid = pool.actors["counter"].pid
        self.assertTrue(pool_mod.pid_alive(pid))

        await self.hub.close_session(sess.session_id)
        self.assertIsNone(self.hub.mcp_pool_registry.get(sess.session_id))
        self.assertFalse(self.bridge_file(sess.session_id).parent.exists())
        self.assertTrue(await self.wait_until(lambda: not pool_mod.pid_alive(pid), timeout=15),
                        "the stdio server process outlived the closed session")
        async with httpx.AsyncClient() as client:
            gone = await client.post(url, json={"action": "list"},
                                     headers={"X-Hub-Mcp-Token": bridge["token"]})
            self.assertEqual(gone.status_code, 404)
            self.assertEqual(gone.json()["error"]["kind"], "session_closed")

    async def test_resume_rebuilds_the_pool_with_the_live_url(self):
        # Created while the Hub's URL is not reachable, so bridge.json names the default port.
        sess = await self.create_bridged(servers=self.servers())
        self.assertEqual(self.bridge_payload(sess.session_id)["hub_url"], "http://127.0.0.1:4444")
        sess.conversation_ref = {"session_id": "doubao-conv-1"}
        await self.hub.persist_session(sess, final=True)
        await self.hub.close_session(sess.session_id)
        self.assertFalse(self.bridge_file(sess.session_id).parent.exists())

        hub_url = await self.start_hub_server()
        os.environ["HUB_API_URL"] = hub_url
        self.injected.clear()
        with patch.object(AgentSession, "start_pty"):
            result = await self.hub.resume_subagent(
                sess.session_id, text="继续桥接检查", source="parent")
        self.assertTrue(result["resumed"])

        # A live click of the URL rewrote bridge.json and rebuilt the pool.
        resumed = self.hub.sessions[sess.session_id]
        self.assertEqual(self.bridge_payload(sess.session_id)["hub_url"], hub_url)
        self.assertIsNotNone(self.hub.mcp_pool_registry.get(sess.session_id))

        # The startup text carries the note; the parent's mailbox keeps the raw words.
        self.assertTrue(self.injected, "the resume injected nothing")
        injected = self.injected[-1]["task_to_inject"]
        self.assertTrue(injected.startswith("[Hub MCP]"), injected[:200])
        self.assertIn("继续桥接检查", injected)
        mailbox = self.hub.parent_messages[sess.session_id][-1]["content"]
        self.assertEqual(mailbox, "继续桥接检查")
        self.assertEqual(resumed.last_task, "检查 MCP 桥接")

        bridge = str(self.bridge_file(sess.session_id))
        code, payload, proc = await asyncio.to_thread(run_cli, ["--bridge", bridge, "list"])
        self.assertEqual(code, 0, proc.stderr)
        self.assertTrue(payload["ok"])
        code, payload, _ = await asyncio.to_thread(
            run_cli, ["--bridge", bridge, "call", "counter", "inc"])
        self.assertEqual(code, 0)
        self.assertEqual(self.text_of(payload), "1", "the rebuilt pool starts a fresh server")

    async def test_start_pty_failure_rolls_the_bridge_back(self):
        captured: dict = {}

        def boom(*args, **kwargs):
            # The bridge is already live at this point: its stdio child must be recorded.
            ledger = json.loads((self.hub.runtime_configs_dir / "mcp_pool_pids.json")
                                .read_text(encoding="utf-8"))
            captured["pids"] = [int(record["pid"]) for record in ledger["pids"]]
            raise RuntimeError("no shell here")

        with patch.object(AgentSession, "start_pty", boom):
            with self.assertRaises(RuntimeError):
                await self.hub.create_session(
                    "doubao", task="检查 MCP 桥接", cwd=str(self.workspace),
                    mcp_servers=self.servers())
        self.assertEqual(self.hub.mcp_pool_registry.sessions(), [])
        self.assertEqual(self.bridge_dirs(), [])
        self.assertEqual(self.hub.sessions, {})
        self.assertTrue(captured.get("pids"), "the prefetch never spawned the stdio server")
        self.assertTrue(await self.wait_until(
            lambda: not any(pool_mod.pid_alive(pid) for pid in captured["pids"]), timeout=15),
            "a refused spawn left a live stdio server behind (%s)" % captured["pids"])
        ledger = json.loads((self.hub.runtime_configs_dir / "mcp_pool_pids.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(ledger["pids"], [])

    async def test_a_failed_resume_read_rolls_the_bridge_back(self):
        """_make_resumed_session (a DB/scrollback read) failing must not leave a pool behind."""
        sess = await self.create_bridged(servers=self.servers())
        session_id = sess.session_id
        sess.conversation_ref = {"session_id": "doubao-conv-2"}
        await self.hub.persist_session(sess, final=True)
        await self.hub.close_session(session_id)
        self.assertFalse(self.bridge_file(session_id).parent.exists())

        async def broken(*args, **kwargs):
            raise RuntimeError("scrollback unreadable")

        with patch.object(HubSessionManager, "_make_resumed_session", broken):
            with self.assertRaises(ValueError) as raised:
                with patch.object(AgentSession, "start_pty"):
                    await self.hub.resume_subagent(session_id, text="继续", source="parent")
        self.assertIn("恢复失败：", str(raised.exception), str(raised.exception))
        self.assertIsNone(self.hub.mcp_pool_registry.get(session_id),
                          "a resume that never reached its PTY kept the pool alive")
        self.assertFalse(self.bridge_file(session_id).parent.exists())
        self.assertNotIn(session_id, self.hub.sessions)
        ledger = json.loads((self.hub.runtime_configs_dir / "mcp_pool_pids.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(ledger["pids"], [])


# ================================================ the pending bridge note ==


class PendingBridgeNoteTests(_BridgeCase):
    """Scheme B: a bridge session created without a task keeps the [Hub MCP] block until it lands."""

    @staticmethod
    def record_writes(sess) -> list:
        """Capture what the hub types into the PTY instead of writing it."""
        writes: list = []
        sess.write_input = lambda text: (writes.append(text), True)[1]
        return writes

    async def test_a_failed_delivery_keeps_the_note_for_the_next_message(self):
        sess = await self.create_bridged(task=None)
        self.assertIsNone(self.injected[-1]["task_to_inject"],
                          "a session without a task must not inject a startup text")
        note = sess._pending_mcp_bridge_note
        self.assertTrue(note and "[Hub MCP]" in note, note)

        # A session with no live process refuses the write: a definite failure (ok is False).
        first = await self.hub.deliver_parent_message(sess.session_id, "第一次投递")
        self.assertFalse(first["pty_written"])
        self.assertEqual(first["injection"], "none")
        self.assertEqual(sess._pending_mcp_bridge_note, note,
                         "a write that never happened may not swallow the bridge note")

        # The next message really lands: it still teaches the bridge, exactly once.
        writes = self.record_writes(sess)
        second = await self.hub.deliver_parent_message(sess.session_id, "第二次投递")
        self.assertTrue(second["pty_written"])
        self.assertTrue(writes and writes[0].startswith("[Hub MCP]"), writes[:1])
        self.assertIn("第二次投递", writes[0])
        self.assertIsNone(sess._pending_mcp_bridge_note)

        # A third message carries no note any more.
        writes = self.record_writes(sess)
        third = await self.hub.deliver_parent_message(sess.session_id, "第三次投递")
        self.assertTrue(third["pty_written"])
        self.assertNotIn("[Hub MCP]", writes[0])
        # The mailbox keeps the caller's own words, never the note.
        self.assertEqual(self.hub.parent_messages[sess.session_id][-1]["content"], "第三次投递")

    async def test_a_queued_submit_that_fails_for_certain_restores_the_note(self):
        """A definite failure in the background submit (write refused) hands the note back."""
        sess = await self.create_bridged(task=None)
        note = sess._pending_mcp_bridge_note
        self.assertTrue(note)
        sess.is_process_alive = lambda: True
        sess.write_input = lambda text: False  # the engine refuses every write, for certain
        queued = await self.hub.deliver_parent_message(sess.session_id, "后台投递")
        self.assertIsNone(queued["delivered"])
        self.assertEqual(queued["injection"], "pending")
        self.assertTrue(await self.wait_until(
            lambda: (sess.last_injection or {}).get("ok") is False, timeout=10), sess.last_injection)
        self.assertEqual(sess._pending_mcp_bridge_note, note,
                         "nothing was typed, so the next message must still teach the bridge")

    async def test_an_unproved_queued_submit_does_not_duplicate_the_note(self):
        """The background submit path: ok None means "may be typed", so the note must not return."""
        sess = await self.create_bridged(task=None)
        self.assertTrue(sess._pending_mcp_bridge_note)
        sess.is_process_alive = lambda: True
        writes = self.record_writes(sess)

        async def unproved(*args, **kwargs):
            return ("timeout", "no evidence on screen")

        with patch.object(HubSessionManager, "_confirm_tui_submit", unproved):
            queued = await self.hub.deliver_parent_message(sess.session_id, "后台投递")
            self.assertIsNone(queued["delivered"])
            self.assertEqual(queued["injection"], "pending")
            self.assertTrue(await self.wait_until(
                lambda: (sess.last_injection or {}).get("pending") is not True
                and (sess.last_injection or {}).get("ok") is None, timeout=10), sess.last_injection)
        self.assertTrue(writes and writes[0].startswith("[Hub MCP]"), writes[:1])
        # The text may be sitting in the composer: re-adding the note would print it twice.
        self.assertIsNone(sess._pending_mcp_bridge_note)




# ================================================================ frontend ==


class FrontendGatingTests(unittest.TestCase):
    def test_launcher_and_html_gate_doubao(self):
        launcher = (ROOT / "static" / "js" / "session-launcher.js").read_text(encoding="utf-8")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("['codex', 'claude', 'doubao']", launcher)
        self.assertIn("doubaoMcpHint", launcher)
        self.assertIn("id=\"doubaoMcpHint\"", html)
        self.assertIn("id=\"disabledMcpField\"", html)
        self.assertIn("$('#disabledMcpField').hidden", launcher)
        # The submit path mirrors the backend: no disable list and no bare shell for a bridge.
        self.assertIn("$('#disabledMcpField').hidden ? [] :", launcher)
        self.assertIn("bareShell", launcher)
        self.assertIn("bridged", launcher)


if __name__ == "__main__":
    unittest.main()
