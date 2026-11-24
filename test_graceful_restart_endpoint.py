"""The restart control plane: /api/hub/graceful-restart, /api/hub/restart-status, shutdown compat.

`POST /api/hub/graceful-restart` takes a token-guarded snapshot of the live sessions, writes
`resume_on_start.json` before answering 202, and triggers the planned stop in the background.
`GET /api/hub/restart-status` answers without a token and without waiting on the shutdown or
resume gate, and walks idle -> stopping -> resuming -> ready/failed.
"""
import asyncio
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import session_manager
from restart_resume_request import CONTINUE_TEXT, REQUEST_NAME, RESULT_NAME
from session_manager import AgentSession, HubSessionManager

TOKEN = "test-restart-token"
BASE_URL = "http://127.0.0.1:4444"
KEPT_AT_STOP = Path("briefs") / "kept-at-stop.json"


def load_server():
    """Import server without letting its module-level hub run CLI discovery or touch real data."""
    if "server" in sys.modules:
        return sys.modules["server"]
    data = tempfile.mkdtemp(prefix="hub-api-data-")
    previous = os.environ.get("HUB_DATA_DIR")
    os.environ["HUB_DATA_DIR"] = data
    try:
        with patch.object(HubSessionManager, "_initial_discovery",
                          lambda hub: hub._discovery_ready.set()):
            return importlib.import_module("server")
    finally:
        if previous is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = previous


server = load_server()


class _HubCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="graceful-api-")
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles.json"
        self.profiles.write_text('{"profiles":{}}', encoding="utf-8")
        self.hub = self.make_hub(self.root)
        self.bg = AsyncMock()
        self._patches = [
            patch.object(server, "hub", self.hub),
            patch.object(server, "_HUB_SHUTDOWN_TOKEN", TOKEN),
            patch.object(server, "_stop_hub_like_ctrl_c", self.bg),
        ]
        for item in self._patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self._patches):
            item.stop()
        for hub in getattr(self, "hubs", []):
            try:
                if hub._loop is not None:
                    await hub.archive_all_sessions_on_shutdown()
            except Exception:
                pass
            try:
                await hub.db.close(timeout=0.5)
            except Exception:
                pass
        self.temp.cleanup()

    def make_hub(self, data_dir=None):
        hubs = getattr(self, "hubs", None)
        if hubs is None:
            hubs = self.hubs = []
        with patch.object(HubSessionManager, "_initial_discovery",
                          lambda hub: hub._discovery_ready.set()):
            hub = HubSessionManager(self.profiles, base_dir=Path(__file__).parent,
                                    data_dir=data_dir or self.root)
        hubs.append(hub)
        return hub

    def live(self, hub, sid, engine, status, alive=True):
        sess = AgentSession(sid, engine, engine, "tui", sid, str(self.root), "", [])
        sess.engine = engine
        sess.status = status
        sess._alive = alive
        hub.sessions[sid] = sess
        return sess

    async def request(self, method, path, *, headers=None, json_body=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                     base_url=BASE_URL) as client:
            return await client.request(method, path, headers=headers, json=json_body)

    def resume_request(self) -> dict:
        return json.loads((self.root / REQUEST_NAME).read_text(encoding="utf-8"))


class TokenTests(_HubCase):
    async def test_a_hub_without_a_token_is_404(self):
        with patch.object(server, "_HUB_SHUTDOWN_TOKEN", ""):
            answer = await self.request("POST", "/api/hub/graceful-restart")
        self.assertEqual(answer.status_code, 404)

    async def test_a_wrong_token_is_403_and_nothing_is_written(self):
        answer = await self.request(
            "POST", "/api/hub/graceful-restart",
            headers={"x-hub-shutdown-token": TOKEN + "x"}, json_body={"mode": "hub"})
        self.assertEqual(answer.status_code, 403)
        self.assertFalse((self.root / REQUEST_NAME).exists())
        self.assertFalse(self.hub._planned_restart)

    async def test_the_status_endpoint_needs_no_token_and_holds_no_secret(self):
        answer = await self.request("GET", "/api/hub/restart-status")
        self.assertEqual(answer.status_code, 200)
        body = answer.json()
        self.assertEqual(body["phase"], "idle")
        self.assertIsNone(body["result"])
        self.assertNotIn(TOKEN, answer.text)
        self.assertNotIn("shutdown_token", body)


class GracefulRestartTests(_HubCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.live(self.hub, "codex-run", "codex", "running")
        self.live(self.hub, "claude-idle", "claude", "completed")
        self.live(self.hub, "codex-old", "codex", "exited", alive=False)

    async def test_202_snapshots_live_sessions_and_writes_the_request(self):
        answer = await self.request(
            "POST", "/api/hub/graceful-restart",
            headers={"x-hub-shutdown-token": TOKEN},
            json_body={"mode": "hub", "restart_id": "r-1",
                       "continue_unfinished_turns": False, "resume_crashed": True})
        self.assertEqual(answer.status_code, 202, answer.text)
        body = answer.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["restart_id"], "r-1")
        self.assertEqual(body["mode"], "hub")
        self.assertIsInstance(body["pid"], int)
        self.assertEqual(body["port"], 4444)
        self.assertTrue(body["started_at"])
        self.assertEqual(set(body["sessions"]), {"codex-run", "claude-idle"})
        self.assertEqual(body["sessions"]["codex-run"], {"engine": "codex", "status": "running"})
        self.assertEqual(body["session_count"], 2)
        self.bg.assert_awaited_once_with(planned=True)

        request = self.resume_request()
        self.assertEqual(request["restart_id"], "r-1")
        self.assertEqual(request["mode"], "hub")
        self.assertIsInstance(request["requested_at"], float)
        self.assertEqual(request["sessions"], body["sessions"])
        self.assertFalse(request["continue_unfinished_turns"])
        self.assertTrue(request["resume_crashed"])
        self.assertTrue(self.hub._planned_restart)
        self.assertEqual(self.hub._restart_phase, "stopping")
        self.assertEqual(self.hub._restart_id, "r-1")

    async def test_an_empty_body_starts_a_plain_restart(self):
        answer = await self.request("POST", "/api/hub/graceful-restart",
                                    headers={"x-hub-shutdown-token": TOKEN})
        self.assertEqual(answer.status_code, 202, answer.text)
        body = answer.json()
        self.assertTrue(body["restart_id"], "the server mints the id")
        self.assertEqual(body["mode"], "hub")
        request = self.resume_request()
        self.assertEqual(request["continue_unfinished_turns"], True)
        self.assertFalse(request["resume_crashed"])
        self.assertNotIn("session_ids", request, "every live session is the default")

    async def test_a_second_request_while_restarting_is_409(self):
        first = await self.request("POST", "/api/hub/graceful-restart",
                                   headers={"x-hub-shutdown-token": TOKEN}, json_body={})
        self.assertEqual(first.status_code, 202)
        second = await self.request("POST", "/api/hub/graceful-restart",
                                    headers={"x-hub-shutdown-token": TOKEN}, json_body={})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], "Hub 已在关闭或重启中")
        self.assertEqual(first.json()["restart_id"], self.resume_request()["restart_id"],
                         "the refused request never rewrote the snapshot")

    async def test_a_second_request_while_shutting_down_is_409(self):
        self.hub._shutting_down = True
        answer = await self.request("POST", "/api/hub/graceful-restart",
                                    headers={"x-hub-shutdown-token": TOKEN}, json_body={})
        self.assertEqual(answer.status_code, 409)

    async def test_an_unknown_mode_is_400(self):
        answer = await self.request(
            "POST", "/api/hub/graceful-restart",
            headers={"x-hub-shutdown-token": TOKEN}, json_body={"mode": "reboot"})
        self.assertEqual(answer.status_code, 400)
        self.assertIn("mode", answer.json()["detail"])

    async def test_a_non_list_session_ids_is_400(self):
        answer = await self.request(
            "POST", "/api/hub/graceful-restart",
            headers={"x-hub-shutdown-token": TOKEN}, json_body={"session_ids": "codex-run"})
        self.assertEqual(answer.status_code, 400)

    async def test_explicit_lists_and_text_reach_the_request_file(self):
        answer = await self.request(
            "POST", "/api/hub/graceful-restart",
            headers={"x-hub-shutdown-token": TOKEN},
            json_body={"session_ids": ["codex-run"], "continue_session_ids": ["codex-run"],
                       "continue_text": "继续这个任务"})
        self.assertEqual(answer.status_code, 202)
        request = self.resume_request()
        self.assertEqual(request["session_ids"], ["codex-run"])
        self.assertEqual(request["continue_session_ids"], ["codex-run"])
        self.assertEqual(request["continue_text"], "继续这个任务")
        self.assertEqual(sorted(request["sessions"]), ["claude-idle", "codex-run"],
                         "the snapshot still records everything live, the list narrows the resume")

    async def test_the_planned_stop_is_a_restart_not_the_final_stop(self):
        await self.hub.start()
        self.live(self.hub, "kimi-run", "kimi", "waiting_parent")
        await self.hub.prepare_graceful_restart(mode="hub", restart_id="r-2")
        await self.hub.archive_all_sessions_on_shutdown(planned=True)
        self.assertTrue(self.hub._planned_restart)
        self.assertEqual(self.hub._restart_phase, "stopping")
        manifest = json.loads((self.root / KEPT_AT_STOP).read_text(encoding="utf-8"))
        self.assertEqual(manifest["sessions"]["kimi-run"]["status"], "waiting_parent")
        with closing(sqlite3.connect(str(self.root / "hub_state.db"))) as conn:
            row = conn.execute("SELECT status, end_reason FROM sessions WHERE session_id=?",
                               ("kimi-run",)).fetchone()
        self.assertEqual(row, ("interrupted", "interrupted"))


class ShutdownCompatTests(_HubCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.live(self.hub, "codex-run", "codex", "running")

    async def test_for_restart_reuses_the_restart_path(self):
        answer = await self.request("POST", "/api/hub/shutdown",
                                    headers={"x-hub-shutdown-token": TOKEN},
                                    json_body={"for_restart": True, "restart_id": "r-3"})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertTrue(answer.json()["ok"])
        self.assertEqual(answer.json()["restart_id"], "r-3")
        self.assertTrue((self.root / REQUEST_NAME).is_file())
        self.assertTrue(self.hub._planned_restart)
        self.assertEqual(self.hub._restart_phase, "stopping")
        self.bg.assert_awaited_once_with(planned=True)

    async def test_a_plain_shutdown_is_still_the_final_stop(self):
        answer = await self.request("POST", "/api/hub/shutdown",
                                    headers={"x-hub-shutdown-token": TOKEN})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json(), {"ok": True})
        self.assertFalse((self.root / REQUEST_NAME).exists(), "a final stop writes no request")
        self.assertFalse(self.hub._planned_restart)
        # Created without `planned`: the hub decides from disk at stop time, as it always did.
        self.assertEqual(self.bg.await_count, 1)
        self.assertEqual(self.bg.await_args.kwargs, {})

    async def test_a_body_without_the_flag_is_a_final_stop(self):
        answer = await self.request("POST", "/api/hub/shutdown",
                                    headers={"x-hub-shutdown-token": TOKEN},
                                    json_body={"for_restart": False})
        self.assertEqual(answer.status_code, 200)
        self.assertFalse((self.root / REQUEST_NAME).exists())
        self.assertEqual(self.bg.await_args.kwargs, {})

    async def test_shutdown_still_needs_its_token(self):
        answer = await self.request("POST", "/api/hub/shutdown",
                                    headers={"x-hub-shutdown-token": "nope"})
        self.assertEqual(answer.status_code, 403)


class RestartStatusTests(_HubCase):
    async def status(self):
        answer = await self.request("GET", "/api/hub/restart-status")
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()

    async def test_idle_then_stopping(self):
        body = await self.status()
        self.assertEqual(body["phase"], "idle")
        self.assertEqual(body["target_count"], 0)
        self.assertEqual(body["resumed_count"], 0)
        self.bg.assert_not_awaited()

        self.live(self.hub, "codex-run", "codex", "running")
        await self.hub.prepare_graceful_restart(mode="replace-exe", restart_id="r-stop")
        body = await self.status()
        self.assertEqual(body["phase"], "stopping")
        self.assertEqual(body["restart_id"], "r-stop")
        self.assertEqual(body["mode"], "replace-exe")
        self.assertIsInstance(body["requested_at"], float)
        self.assertEqual(body["pid"], os.getpid())
        self.assertEqual(body["port"], 4444)
        self.assertTrue(body["started_at"])

    async def test_the_status_answers_while_the_shutdown_gate_is_closed(self):
        self.hub._shutting_down = True
        body = await self.status()
        self.assertIn(body["phase"], ("idle", "stopping"))
        # A normal route is refused by the gate at the same time, so the exemption is real.
        other = await self.request("GET", "/api/sessions")
        self.assertEqual(other.status_code, 503)

    async def test_stopping_to_resuming_to_ready_with_live_counts(self):
        await self.hub.start()
        self.live(self.hub, "codex-run", "codex", "running")
        self.live(self.hub, "claude-idle", "claude", "completed")
        await self.hub.prepare_graceful_restart(mode="hub", restart_id="r-full")
        await self.hub.archive_all_sessions_on_shutdown(planned=True)
        hub = self.make_hub()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def resume(sid, text=None, auto_approval_rules=None, source="parent"):
            calls.append((sid, text))
            started.set()
            await release.wait()
            return {"resumed": True}

        async def wait_port(port):
            return True

        with patch.object(hub, "resume_subagent", side_effect=resume), \
                patch("session_manager.wait_for_port", side_effect=wait_port):
            await hub.start()
            await asyncio.wait_for(started.wait(), 5)
            self.assertFalse(hub.restart_resume_gate.is_open)
            with patch.object(server, "hub", hub):
                body = await self.status()
                self.assertEqual(body["phase"], "resuming", "a closed gate does not hold the status")
                self.assertEqual(body["restart_id"], "r-full")
                # Counts move while the pass runs: codex-run is already back or back in progress.
                self.assertEqual(body["target_count"], 2)
                self.assertLessEqual(body["resumed_count"], 2)
                release.set()
                await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
            with patch.object(server, "hub", hub):
                body = await self.status()
        self.assertEqual(body["phase"], "ready")
        self.assertEqual(body["target_count"], 2)
        self.assertEqual(body["resumed_count"], 2)
        self.assertEqual(body["failed_count"], 0)
        self.assertEqual(body["skipped_count"], 0)
        self.assertEqual(set(body["result"]["sessions"]), {"codex-run", "claude-idle"})
        self.assertIsNone(body["result"]["sessions"]["claude-idle"].get("error"))
        self.assertFalse(body["result"]["sessions"]["claude-idle"]["continued"],
                         "an idle session comes back without a message")
        result = json.loads((self.root / RESULT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(result["restart_id"], "r-full")
        self.assertEqual(result["sessions"]["codex-run"]["status_at_stop"], "running")

    async def test_a_failed_resume_reports_failed(self):
        await self.hub.start()
        self.live(self.hub, "codex-run", "codex", "running")
        await self.hub.prepare_graceful_restart(mode="hub", restart_id="r-fail")
        await self.hub.archive_all_sessions_on_shutdown(planned=True)
        hub = self.make_hub()
        with patch.object(hub, "resume_subagent", side_effect=ValueError("工作目录已不存在")), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        with patch.object(server, "hub", hub):
            body = await self.status()
        self.assertEqual(body["phase"], "failed")
        self.assertEqual(body["failed_count"], 1)
        self.assertEqual(body["resumed_count"], 0)
        self.assertEqual(body["target_count"], 1)
        self.assertIn("工作目录已不存在", body["result"]["sessions"]["codex-run"]["error"])

    async def test_a_skip_is_not_a_failure(self):
        await self.hub.start()
        self.live(self.hub, "pwsh-shell", "pwsh", "running")
        await self.hub.prepare_graceful_restart(mode="hub", restart_id="r-skip")
        await self.hub.archive_all_sessions_on_shutdown(planned=True)
        hub = self.make_hub()
        with patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
        with patch.object(server, "hub", hub):
            body = await self.status()
        self.assertEqual(body["phase"], "ready")
        self.assertEqual(body["skipped_count"], 1)
        self.assertEqual(body["failed_count"], 0)
        self.assertEqual(body["resumed_count"], 0)
        self.assertEqual(body["target_count"], 1)

    async def test_a_stale_result_file_does_not_paint_a_cold_start(self):
        (self.root / RESULT_NAME).write_text(json.dumps({"sessions": {"x": {"ok": True}}}),
                                            encoding="utf-8")
        body = await self.status()
        self.assertEqual(body["phase"], "idle")
        self.assertIsNone(body["result"])


class CrashedPolicyTests(_HubCase):
    """`live_only_opt_in_crashed`: a plain restart never revives history's crashed rows."""

    async def hard_stop(self, sessions):
        hub = self.make_hub()
        await hub.start()
        for sid, engine, status in sessions:
            sess = AgentSession(sid, engine, engine, "tui", sid, str(self.root), "", [])
            sess.engine = engine
            sess.status = status
            await hub.persist_session(sess)
        await hub.db.flush()
        self.hubs.remove(hub)
        await self.drop(hub)

    async def drop(self, hub):
        for task in list(hub._background_tasks):
            task.cancel()
        await asyncio.gather(*hub._background_tasks, return_exceptions=True)
        await hub.db.close()

    async def resumed(self, request, *, failure=()):
        (self.root / REQUEST_NAME).write_text(json.dumps(request), encoding="utf-8")
        hub = self.make_hub()
        calls = []

        async def resume(sid, text=None, auto_approval_rules=None, source="parent"):
            if sid in failure:
                raise ValueError("cannot")
            calls.append((sid, text))
            return {"resumed": True}

        with patch.object(hub, "resume_subagent", side_effect=resume), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        return hub, sorted(calls)

    async def test_a_plain_restart_resumes_only_what_was_live(self):
        await self.hard_stop([("codex-run", "codex", "running"),
                              ("codex-boom", "codex", "crashed")])
        hub, calls = await self.resumed({})
        self.assertEqual(calls, [("codex-run", CONTINUE_TEXT)])
        self.assertEqual(hub.restart_status()["phase"], "ready")

    async def test_resume_crashed_fetches_history_on_purpose(self):
        await self.hard_stop([("codex-run", "codex", "running"),
                              ("codex-boom", "codex", "crashed")])
        hub, calls = await self.resumed({"resume_crashed": True})
        self.assertEqual([c[0] for c in calls], ["codex-boom", "codex-run"])
        self.assertEqual(hub.restart_status()["resumed_count"], 2)

    async def test_a_named_crashed_session_is_resumed(self):
        await self.hard_stop([("codex-boom", "codex", "crashed")])
        hub, calls = await self.resumed({"session_ids": ["codex-boom"]})
        self.assertEqual([c[0] for c in calls], ["codex-boom"])
        self.assertIsNone(calls[0][1], "a crashed row's status at the stop is no longer known, "
                                       "so it gets no continue message unless listed by name")


class ResumeSecretTests(_HubCase):
    """A resume error is published three ways; none of them may carry an upstream credential."""

    SS_HEADER = "Bearer super-secret-xyz"
    ENV_VALUE = "env-secret-999"

    async def failing_resume(self, message: str, sessions):
        hub = self.make_hub()
        await hub.start()
        for sid, engine, status in sessions:
            sess = self.live(hub, sid, engine, status)
            sess.profile_snapshot = {
                "engine": engine,
                "mcp_servers": {"upstream": {
                    "type": "http", "url": "https://mcp.example.com",
                    "headers": {"Authorization": self.SS_HEADER},
                    "env": {"API_KEY": self.ENV_VALUE},
                }},
            }
            await hub.persist_session(sess)
        await hub.db.flush()
        handle = (await hub.wait_inbox.create([sid for sid, _e, _s in sessions]))["wait_handle"]
        await hub.prepare_graceful_restart(mode="hub", restart_id="r-secret")
        await hub.archive_all_sessions_on_shutdown(planned=True)

        hub = self.make_hub()
        with patch.object(hub, "resume_subagent", side_effect=RuntimeError(message)), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        return hub, handle

    async def test_the_error_is_redacted_in_the_status_the_result_and_the_event(self):
        leak = ("upstream refused 401: Authorization: " + self.SS_HEADER
                + f"; env API_KEY={self.ENV_VALUE}")
        hub, handle = await self.failing_resume(leak, [("codex-bridged", "codex", "running")])

        with patch.object(server, "hub", hub):
            answer = await self.request("GET", "/api/hub/restart-status")
        self.assertEqual(answer.status_code, 200)
        status_text = answer.text
        self.assertIn("[REDACTED]", status_text)
        self.assertNotIn("super-secret-xyz", status_text)
        self.assertNotIn(self.ENV_VALUE, status_text)
        body = answer.json()
        self.assertEqual(body["phase"], "failed")
        self.assertEqual(body["failed_count"], 1)
        error = body["result"]["sessions"]["codex-bridged"]["error"]
        self.assertIn("upstream refused 401", error)
        self.assertIn("[REDACTED]", error)

        result_text = (self.root / RESULT_NAME).read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", result_text)
        self.assertNotIn("super-secret-xyz", result_text)
        self.assertNotIn(self.ENV_VALUE, result_text)
        self.assertEqual(json.loads(result_text)["restart_id"], "r-secret")

        woke = await hub.wait_inbox.wait(handle, 0.2)
        self.assertEqual(woke["state"], "failed")
        self.assertEqual(woke["errors"][0]["type"], "restart_resume_failed")
        wanted = json.dumps(woke, ensure_ascii=False)
        self.assertIn("[REDACTED]", wanted)
        self.assertNotIn("super-secret-xyz", wanted)
        self.assertNotIn(self.ENV_VALUE, wanted)

    async def test_a_session_without_mcp_servers_keeps_its_real_error(self):
        hub, _handle = await self.failing_resume_plain(ValueError("工作目录已不存在"))
        with patch.object(server, "hub", hub):
            body = (await self.request("GET", "/api/hub/restart-status")).json()
        self.assertIn("工作目录已不存在", body["result"]["sessions"]["codex-plain"]["error"])

    async def failing_resume_plain(self, exc):
        hub = self.make_hub()
        await hub.start()
        self.live(hub, "codex-plain", "codex", "running")
        await hub.prepare_graceful_restart(mode="hub", restart_id="r-plain")
        await hub.archive_all_sessions_on_shutdown(planned=True)
        hub = self.make_hub()
        with patch.object(hub, "resume_subagent", side_effect=exc), \
                patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await hub.start()
            await asyncio.wait_for(hub.restart_resume_gate._open.wait(), 5)
        return hub, None


if __name__ == "__main__":
    unittest.main()
