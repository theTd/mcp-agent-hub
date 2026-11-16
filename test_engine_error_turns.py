"""Engine failures end a turn as a marked error: classifier, each engine watcher, parent views."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engine_errors import classify_engine_error, engine_error_summary
from fake_terminal_core import FakeTerminal
from session_manager import AgentSession, HubSessionManager, parent_turn_outcome_fields


class EngineErrorClassifierTests(unittest.TestCase):
    def kind(self, message, **hints):
        return classify_engine_error(message, **hints)

    def test_real_codex_failures(self):
        retry = self.kind("exceeded retry limit, last status: 429 Too Many Requests")
        self.assertEqual((retry["kind"], retry["retryable"]), ("rate_limit", True))
        quota = self.kind(
            "Quota exceeded. Check your plan and billing details.",
            code="usage_limit_exceeded",
        )
        self.assertEqual((quota["kind"], quota["retryable"]), ("quota", False))
        self.assertEqual(self.kind("quota", code="usage_limit_exceeded")["kind"], "quota")
        self.assertEqual(
            self.kind("exceeded retry limit", http_status=429)["kind"], "rate_limit"
        )
        self.assertEqual(
            self.kind(
                'unexpected status 413 Payload Too Large: <html>\r\n<head><title>413 Request '
                'Entity Too Large</title></head><body>nginx</body></html>, url: https://x/v1/responses'
            )["kind"],
            "request",
        )
        nginx = self.kind(
            'unexpected status 413 Payload Too Large: <html>\r\n<head><title>413 Request Entity Too '
            'Large</title></head></html>, url: https://x'
        )
        self.assertEqual(nginx["message"], "unexpected status 413 Payload Too Large")

    def test_real_claude_kimi_omp_dsh_failures(self):
        self.assertEqual(self.kind("Not logged in · Please run /login", code="authentication_failed")["kind"], "auth")
        self.assertEqual(self.kind("API Error: 429 too many requests")["kind"], "rate_limit")
        self.assertEqual(self.kind("invalid x-api-key")["kind"], "auth")
        weekly = self.kind(
            "You've reached your weekly (7-day) usage limit. Your quota will reset when the current "
            "7-day window ends.",
            code="provider.api_error",
            http_status=429,
        )
        self.assertEqual((weekly["kind"], weekly["retryable"]), ("quota", False))
        self.assertEqual(self.kind("502 status code (no body)", http_status=502)["kind"], "server")
        self.assertEqual(self.kind("terminated")["kind"], "network")
        self.assertEqual(self.kind("Connection failed: error sending request")["kind"], "network")
        self.assertEqual(self.kind("额度不足")["kind"], "quota")
        self.assertEqual(self.kind("余额不足")["kind"], "quota")
        self.assertEqual(self.kind("stream disconnected")["kind"], "network")
        self.assertEqual(self.kind("econnreset")["kind"], "network")
        agy = self.kind(
            'API error (attempt 8): request failed: Post "https://x/v1:streamGenerateContent": EOF'
        )
        self.assertEqual(agy["kind"], "network")
        self.assertEqual(self.kind("geoff reviewed it")["kind"], "other")
        self.assertEqual(self.kind("the model returned 400 bad request")["kind"], "request")

    def test_status_numbers_match_whole_numbers_and_messages_stay_one_line(self):
        self.assertEqual(self.kind("1500 tokens used")["kind"], "other")
        self.assertEqual(self.kind("14290 too many requests")["kind"], "rate_limit")
        self.assertEqual(self.kind("internal server error")["kind"], "server")
        self.assertEqual(self.kind("gateway timeout")["kind"], "server")
        self.assertEqual(self.kind("maximum context length exceeded")["kind"], "request")
        collapsed = self.kind("connection   failed\n\nplease retry")
        self.assertEqual(collapsed["message"], "connection failed please retry")
        long = self.kind("x" * 900)
        self.assertEqual(len(long["message"]), 500)

    def test_summary_names_kind_and_advice(self):
        info = self.kind("Quota exceeded. Check your plan.")
        summary = engine_error_summary("codex", info)
        self.assertTrue(summary.startswith("codex turn ended on an engine error (quota): Quota exceeded."))
        self.assertIn("This is not an answer; codex is idle at its prompt.", summary)
        self.assertIn("Retrying will not help", summary)
        retry = engine_error_summary("kimi", {"kind": "network", "message": "connection failed"})
        self.assertIn("Send a message to retry after a pause.", retry)
        rejected = engine_error_summary("codex", {"kind": "request", "message": "413"})
        self.assertIn("The request was rejected", rejected)


class EngineErrorTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temporary = tempfile.TemporaryDirectory(prefix="hub-engine-error-")
        self.root = Path(self.temporary.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.fake = FakeTerminal()
        self.hub.core = self.fake
        await self.hub.start()

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temporary.cleanup()

    def session(self, engine, suffix=""):
        sid = f"{engine}-error{suffix}"
        sess = AgentSession(sid, engine, engine, "tui", "Engine error fixture", str(self.root),
                            "unused", [], core=self.fake)
        sess.engine = engine
        sess.status = "running"
        self.fake.spawn(sid, ["fixture"])
        sess._loop = asyncio.get_running_loop()
        self.hub.sessions[sid] = sess
        self.hub._on_core_tick()
        return sess

    async def until(self, condition, timeout=6):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.02)

    @staticmethod
    def append(path, *rows):
        with Path(path).open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def events(self, sid, ev_type=None):
        return [e for e in self.hub.get_worker_events(session_id=sid, after_seq=0)
                if ev_type is None or e["type"] == ev_type]

    async def codex_rollout(self, suffix=""):
        sess = self.session("codex", suffix)
        work = self.root / f"codex{suffix}"
        work.mkdir(exist_ok=True)
        path = work / "native.jsonl"
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"rollout_path": str(path), "session_file": str(path)}
        return sess, path, self.hub._schedule(self.hub._watch_codex_rollout(sess, work))

    async def test_codex_task_complete_errors_are_errors_not_answers(self):
        cases = [
            ("quota", {"message": "Quota exceeded. Check your plan and billing details.",
                       "codex_error_info": "usage_limit_exceeded"}, "quota"),
            ("retry", {"message": "exceeded retry limit, last status: 429 Too Many Requests",
                       "codex_error_info": {"response_too_many_failed_attempts": {"http_status_code": 429}}},
             "rate_limit"),
            ("payload", {"message": "unexpected status 413 Payload Too Large: <html>\r\n<head>"
                                    "<title>413 Request Entity Too Large</title></head></html>, url: https://x"},
             "request"),
        ]
        for suffix, error, kind in cases:
            sess, path, task = await self.codex_rollout(f"-{suffix}")
            try:
                self.fake.sessions[sess.session_id].screen_text = "■ Quota exceeded. Check your plan\n> "
                self.append(path, {"type": "event_msg", "payload": {
                    "type": "task_complete", "last_agent_message": None, "error": error}})
                await self.until(lambda: sess.status == "completed")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(sess.final_response_source, "engine_error")
            self.assertTrue(sess.final_response.startswith(
                f"codex turn ended on an engine error ({kind}):"), sess.final_response)
            self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", kind))
            self.assertNotIn("<html>", sess.final_response)
            done = self.events(sess.session_id, "turn_completed")
            self.assertEqual((len(done), done[0]["data"]["outcome"]), (1, "error"))
            # The screen dump never replaces the summary, however often the view is rebuilt.
            view = await self.hub.get_subagent_status_and_trace(sess.session_id)
            self.assertEqual(view["final_response"], sess.final_response)
            self.assertEqual((view["turn_outcome"], view["turn_error"]["kind"]), ("error", kind))

    async def test_codex_task_complete_error_keeps_a_partial_message(self):
        sess, path, task = await self.codex_rollout("-partial")
        try:
            self.append(path, {"type": "event_msg", "payload": {
                "type": "task_complete", "last_agent_message": "half an answer",
                "error": "stream disconnected before completion"}})
            await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sess.last_turn_outcome, "error")
        self.assertEqual(self.events(sess.session_id, "turn_completed")[0]["data"]["partial_response"],
                         "half an answer")
        self.assertNotEqual(sess.final_response, "half an answer")

    async def test_codex_reconnect_stall_escalates_and_turn_aborted_names_the_error(self):
        sess, path, task = await self.codex_rollout("-stall")
        try:
            stalled_screen = (
                "• Reconnecting... waiting for network (9m 05s • esc to interrupt)\n"
                "  └ Connection failed: error sending request\n"
                "  └ Tip: Use /keymap to configure keyboard shortcuts.\n"
                "› "
            )
            self.fake.sessions[sess.session_id].screen_text = stalled_screen
            with patch("session_manager.CODEX_NETWORK_STALL_SEC", 0.05), \
                 patch("session_manager.CODEX_STALL_INTERRUPT_WAIT_SEC", 0.3):
                await self.hub.poll_tui_screen_interactions_once(sess)
                self.assertEqual(sess.current_activity, "Reconnecting — Connection failed: error sending request")
                self.assertEqual(len(self.events(sess.session_id, "error")), 1)
                await asyncio.sleep(0.08)
                await self.hub.poll_tui_screen_interactions_once(sess)
                self.assertIn("\x1b", self.fake.inputs_of(sess.session_id))
                # The interruption made the status line disappear; only turn_aborted (or the
                # timeout) may end the stall now.
                self.fake.sessions[sess.session_id].screen_text = "> "
                await self.hub.poll_tui_screen_interactions_once(sess)
                self.append(path, {"type": "event_msg", "payload": {
                    "type": "turn_aborted", "reason": "interrupted"}})
                await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sess.last_turn_outcome, "error")
        self.assertEqual(sess.last_turn_error["kind"], "network")
        done = self.events(sess.session_id, "turn_completed")
        self.assertEqual((len(done), done[0]["data"]["interrupted_by_hub"]), (1, True))
        self.assertTrue(sess.final_response.startswith(
            "codex turn ended on an engine error (network): Connection failed"))

    async def test_codex_stall_without_turn_aborted_ends_after_the_wait(self):
        sess = self.session("codex", "-stall-timeout")
        self.fake.sessions[sess.session_id].screen_text = (
            "• Reconnecting... 2/5\n  └ Connection failed: error sending request\n"
        )
        with patch("session_manager.CODEX_NETWORK_STALL_SEC", 0.05), \
             patch("session_manager.CODEX_STALL_INTERRUPT_WAIT_SEC", 0.15):
            await self.hub.poll_tui_screen_interactions_once(sess)
            await asyncio.sleep(0.08)
            await self.hub.poll_tui_screen_interactions_once(sess)
            self.assertIn("\x1b", self.fake.inputs_of(sess.session_id))
            await asyncio.sleep(0.2)
            await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertEqual(sess.status, "completed")
        self.assertEqual(sess.last_turn_outcome, "error")
        self.assertEqual(self.events(sess.session_id, "turn_completed")[0]["data"]["interrupted_by_hub"], True)

    async def test_codex_stall_that_clears_before_the_threshold_sends_nothing(self):
        sess = self.session("codex", "-stall-cleared")
        self.fake.sessions[sess.session_id].screen_text = (
            "• Reconnecting... waiting for network (4s • esc to interrupt)\n"
            "  └ Connection failed: error sending request\n"
        )
        with patch("session_manager.CODEX_NETWORK_STALL_SEC", 0.05):
            await self.hub.poll_tui_screen_interactions_once(sess)
            self.assertIsNotNone(sess._codex_reconnect)
            self.fake.sessions[sess.session_id].screen_text = "› Ask Codex to do anything\n"
            await self.hub.poll_tui_screen_interactions_once(sess)
            await asyncio.sleep(0.08)
            await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNone(sess._codex_reconnect)
        self.assertEqual(self.fake.inputs_of(sess.session_id), [])
        self.assertEqual(sess.status, "running")

    async def test_claude_api_error_message_ends_the_turn_as_an_error(self):
        sess = self.session("claude", "-api")
        work = self.root / "claude-api"
        work.mkdir()
        path = work / "native.jsonl"
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"transcript_path": str(path)}
        task = self.hub._schedule(self.hub._watch_claude_session(sess, work))
        try:
            self.append(path, {"type": "assistant", "isApiErrorMessage": True,
                               "error": "authentication_failed", "apiErrorStatus": None,
                               "failedResponseHadNoToolCall": True,
                               "message": {"model": "<synthetic>", "stop_reason": "stop_sequence",
                                           "content": [{"type": "text",
                                                        "text": "Not logged in · Please run /login"}]}})
            await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "auth"))
        self.assertEqual(sess.final_response_source, "engine_error")
        self.assertTrue(sess.final_response.startswith("claude turn ended on an engine error (auth):"))
        self.assertNotEqual(sess.final_response, "Not logged in · Please run /login")

    def kimi_wire(self, suffix=""):
        sess = self.session("kimi", suffix)
        work = self.root / f"kimi{suffix}"
        path = work / "agents/main/wire.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"session_dir": str(work)}
        return sess, path

    async def test_kimi_failed_turn_ends_as_quota_and_retries_are_activity_only(self):
        sess, path = self.kimi_wire("-failed")
        task = self.hub._schedule(self.hub._watch_kimi_session(sess, self.root))
        try:
            self.append(path, {"type": "turn.step.retrying", "agentId": "main", "turnId": 0,
                               "step": 1, "failedAttempt": 1, "nextAttempt": 2, "maxAttempts": 10,
                               "errorName": "APIConnectionError", "errorMessage": "terminated"})
            await self.until(lambda: sess.current_activity ==
                             "Retrying (2/10): APIConnectionError terminated")
            self.assertEqual(sess.status, "running")
            self.append(path, {"type": "turn.ended", "agentId": "main", "turnId": 0,
                               "reason": "failed",
                               "error": {"code": "provider.api_error",
                                         "message": "You've reached your weekly (7-day) usage limit. "
                                                    "Your quota will reset when the current 7-day window ends.",
                                         "name": "APIProviderQuotaExhaustedError",
                                         "details": {"statusCode": 429}, "retryable": False}})
            await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "quota"))
        # The engine's own retryable=false wins over the kind's default.
        self.assertFalse(sess.last_turn_error["retryable"])
        self.assertTrue(sess.final_response.startswith("kimi turn ended on an engine error (quota):"))

    @staticmethod
    def omp_error(message="502 status code (no body)", status=502):
        return {"type": "message", "message": {
            "role": "assistant", "stopReason": "error", "errorStatus": status,
            "errorMessage": message, "content": []}}

    @staticmethod
    def omp_final(text):
        return {"type": "message", "message": {"role": "assistant", "stopReason": "stop",
                                               "content": [{"type": "text", "text": text}]}}

    def omp_wire(self, suffix=""):
        sess = self.session("omp", suffix)
        work = self.root / f"omp{suffix}"
        work.mkdir(exist_ok=True)
        path = work / "native.jsonl"
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"session_file": str(path)}
        return sess, work, path

    async def test_omp_error_stop_reason_ends_the_turn_after_the_hold(self):
        sess, work, path = self.omp_wire("-error")
        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with patch("session_manager.OMP_ERROR_TURN_END_SEC", 0.2):
                self.append(path, self.omp_error())
                await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "server"))
        self.assertTrue(sess.final_response.startswith("omp turn ended on an engine error (server):"))
        self.assertEqual(self.events(sess.session_id, "turn_completed")[0]["data"]["outcome"], "error")

    async def test_omp_error_recovered_by_a_later_record_does_not_end_the_turn(self):
        sess, work, path = self.omp_wire("-recovered")
        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with patch("session_manager.OMP_ERROR_TURN_END_SEC", 0.2):
                self.append(path, self.omp_error())
                await self.until(lambda: "Engine error (server)" in sess.current_activity)
                self.append(path, self.omp_final("recovered answer"))
                await self.until(lambda: sess.final_response == "recovered answer")
                await asyncio.sleep(0.35)  # well past the hold: the retry cleared it
                self.assertEqual(sess.status, "completed")
                self.assertIsNone(sess.last_turn_outcome)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((self.events(sess.session_id, "turn_completed")[0].get("data") or {}).get("outcome"), None)
        self.assertEqual(sess.final_response_source, "native_transcript")

    async def test_omp_tail_sweep_ends_an_error_row_only_after_the_hold_and_never_twice(self):
        sess, _work, path = self.omp_wire("-sweep")
        self.append(path, self.omp_error("Connection reset by peer", 0))
        sess.transcript_path = path
        with patch("session_manager.OMP_ERROR_TURN_END_SEC", 0.2):
            self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)  # the row is still fresh
            old = time.time() - 5
            os.utime(path, (old, old))
            self.assertEqual(await self.hub._sweep_omp_turn_ends(), 1)
            self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)
        self.assertEqual(sess.status, "completed")
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "network"))
        self.assertEqual(len(self.events(sess.session_id, "turn_completed")), 1)

    async def test_dsh_failed_job_ends_the_turn_as_an_error(self):
        sess = self.session("dsh", "-failed")
        sess.dsh_job_id = "job-1"
        log = self.root / "dsh-1.log"
        log.touch()

        class Response:
            is_success = True
            def __init__(self, payload):
                self.payload = payload
            def json(self):
                return self.payload

        job = {"id": "job-1", "status": "failed", "error": "429 Too Many Requests",
               "step": 3, "toolCalls": 2, "tokens": {"input": 10, "output": 4}}

        class Client:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def get(self, url, params=None, headers=None):
                if url.endswith("/messages"):
                    return Response({"result": {"messages": []}})
                return Response({"job": job})

        with patch("session_manager.httpx.AsyncClient", Client):
            task = self.hub._schedule(self.hub._watch_dsh_hub_job(sess, "job-1", "tok", log_path=log))
            try:
                await self.until(lambda: sess.status == "crashed")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sess.status, "crashed")  # the existing abnormal-end status stays
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "rate_limit"))
        self.assertEqual(sess.final_response_source, "engine_error")
        self.assertNotEqual(sess.final_response, "429 Too Many Requests")
        done = self.events(sess.session_id, "turn_completed")
        self.assertEqual((len(done), done[0]["data"]["outcome"]), (1, "error"))

    def test_dsh_timeout_that_matches_nothing_is_other(self):
        info = self.hub._dsh_job_error_info("timeout", "")
        self.assertEqual((info["kind"], info["retryable"]), ("other", False))
        self.assertTrue(info["message"].startswith("DSH job timed out:"))
        matched = self.hub._dsh_job_error_info("timeout", "connection reset by peer")
        self.assertEqual(matched["kind"], "network")

    async def test_group_wait_results_carry_outcome_error_and_engine_errors(self):
        good = self.session("omp", "-group-good")
        bad = self.session("kimi", "-group-bad")
        for sess in (good, bad):
            sess.status = "completed"
            sess.completion_reported = True
        good.final_response = "real answer"
        bad.final_response = engine_error_summary("kimi", {"kind": "quota", "message": "usage limit"})
        bad.last_turn_outcome = "error"
        bad.last_turn_error = {"kind": "quota", "retryable": False, "message": "usage limit"}
        handle = (await self.hub.wait_inbox.create([good.session_id, bad.session_id]))["wait_handle"]
        done = await self.hub.wait_inbox.wait(handle, 0.3)
        self.assertEqual(done["state"], "done")
        entries = {r["session_id"]: r for r in done["results"]}
        self.assertEqual(entries[good.session_id]["outcome"], "answered")
        self.assertEqual(entries[bad.session_id]["outcome"], "error")
        self.assertEqual(entries[bad.session_id]["error"]["kind"], "quota")
        self.assertEqual(done["engine_errors"], [{"session_id": bad.session_id, "kind": "quota",
                                                  "retryable": False, "message": "usage limit"}])

    async def test_single_wait_event_carries_the_error_entry(self):
        sess = self.session("codex", "-single")
        handle = (await self.hub.wait_inbox.create([sess.session_id]))["wait_handle"]
        await self.hub.wait_inbox.publish_event({
            "session_id": sess.session_id, "type": "turn_completed", "turn_id": "t1",
            "content": "engine error summary",
            "data": {"outcome": "error", "error": "Quota exceeded", "error_kind": "quota", "retryable": False},
        })
        done = await self.hub.wait_inbox.wait(handle, 0.3)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["results"][0]["outcome"], "error")
        self.assertEqual(done["results"][0]["error"]["kind"], "quota")
        self.assertEqual(done["engine_errors"][0]["message"], "Quota exceeded")

    async def test_a_closed_member_that_reported_its_turn_is_settled_not_failed(self):
        reported = self.session("omp", "-closed")
        reported.status = "terminated"
        reported.final_response = "finished before the close"
        await self.hub.persist_session(reported)
        del self.hub.sessions[reported.session_id]
        still_running = self.session("kimi", "-running")
        snapshot = await self.hub._wait_state_snapshot([reported.session_id, still_running.session_id])
        self.assertEqual(snapshot["state"], "waiting")
        still_running.status = "completed"
        still_running.completion_reported = True
        still_running.final_response = "B"
        snapshot = await self.hub._wait_state_snapshot([reported.session_id, still_running.session_id])
        self.assertEqual(snapshot["state"], "done")
        self.assertEqual([r["final_response"] for r in snapshot["results"]],
                         ["finished before the close", "B"])

    async def test_a_member_that_died_without_reporting_its_turn_is_failed(self):
        silent = self.session("omp", "-silent")
        silent.status = "terminated"
        await self.hub.persist_session(silent)
        del self.hub.sessions[silent.session_id]
        snapshot = await self.hub._wait_state_snapshot([silent.session_id])
        self.assertEqual(snapshot["state"], "failed")

    def test_status_view_fields_exist_only_for_an_ended_error_or_abort(self):
        self.assertEqual(parent_turn_outcome_fields({}), {})
        error = parent_turn_outcome_fields({
            "last_turn_outcome": "error",
            "last_turn_error": {"kind": "quota", "retryable": False, "message": "q"},
        })
        self.assertEqual(error, {"turn_outcome": "error",
                                 "turn_error": {"kind": "quota", "retryable": False, "message": "q"}})
        self.assertEqual(parent_turn_outcome_fields({"last_turn_outcome": "aborted"}),
                         {"turn_outcome": "aborted"})
        archived = parent_turn_outcome_fields({"final_response_source": "engine_error",
                                               "final_response": "codex turn ended..."})
        self.assertEqual(archived["turn_outcome"], "error")
        self.assertEqual(archived["turn_error"]["kind"], "other")


if __name__ == "__main__":
    unittest.main()
