"""Engine failures end a turn as a marked error: classifier, each engine watcher, parent views."""
import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engine_errors import classify_engine_error, engine_error_summary
from fake_terminal_core import FakeTerminal
from session_manager import AgentSession, HubSessionManager, parent_turn_outcome_fields
from transcript_summarizer import ENGINE_BUSY_LINE_RES


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
            # Finding 4: the real record's status sits in the nested variant object; without it
            # this message carries no number and would classify as "other".
            ("nested", {"message": "exceeded retry limit",
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

    async def test_codex_reconnecting_is_shown_once_and_never_interrupted(self):
        # Owner decision 2026-11-16 (item codex_network_stall): the status line is evidence for
        # the parent, never a reason for the hub to type Esc into the turn.
        sess = self.session("codex", "-stall-show")
        self.fake.sessions[sess.session_id].screen_text = (
            "• Reconnecting... waiting for network (9m 05s • esc to interrupt)\n"
            "  └ Connection failed: error sending request\n"
            "  └ Tip: Use /keymap to configure keyboard shortcuts.\n"
            "› "
        )
        for _ in range(6):
            await self.hub.poll_tui_screen_interactions_once(sess)
            await asyncio.sleep(0.05)
        self.assertEqual(sess.current_activity, "Reconnecting — Connection failed: error sending request")
        errors = self.events(sess.session_id, "error")
        self.assertEqual([(e["category"], e["content"]) for e in errors],
                         [("progress", "Connection failed: error sending request")])
        self.assertEqual(self.fake.inputs_of(sess.session_id), [])
        self.assertEqual(sess.status, "running")
        self.assertEqual(self.events(sess.session_id, "turn_completed"), [])

    async def test_codex_reconnecting_line_clears_and_a_new_appearance_is_a_new_episode(self):
        sess = self.session("codex", "-stall-episodes")
        numbered = "• Reconnecting... 2/5\n  └ Connection failed: error sending request\n"
        self.fake.sessions[sess.session_id].screen_text = numbered
        await self.hub.poll_tui_screen_interactions_once(sess)
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertEqual(len(self.events(sess.session_id, "error")), 1)
        self.fake.sessions[sess.session_id].screen_text = "› Ask Codex to do anything\n"
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNone(sess._codex_reconnect)
        self.fake.sessions[sess.session_id].screen_text = numbered
        await self.hub.poll_tui_screen_interactions_once(sess)
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertEqual(len(self.events(sess.session_id, "error")), 2)
        self.assertEqual(self.fake.inputs_of(sess.session_id), [])
        self.assertEqual(sess.status, "running")

    async def test_codex_turn_aborted_after_a_reconnect_episode_is_the_ordinary_abort(self):
        sess, path, task = await self.codex_rollout("-stall-aborted")
        try:
            self.fake.sessions[sess.session_id].screen_text = (
                "• Reconnecting... waiting for network (9m 05s • esc to interrupt)\n"
                "  └ Connection failed: error sending request\n"
            )
            await self.hub.poll_tui_screen_interactions_once(sess)
            await asyncio.sleep(0.1)
            await self.hub.poll_tui_screen_interactions_once(sess)
            self.assertEqual(self.fake.inputs_of(sess.session_id), [])
            self.assertEqual(len(self.events(sess.session_id, "error")), 1)
            self.append(path, {"type": "event_msg", "payload": {
                "type": "turn_aborted", "reason": "interrupted"}})
            await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sess.last_turn_outcome, "aborted")
        self.assertIsNone(sess.last_turn_error)
        done = self.events(sess.session_id, "turn_completed")
        self.assertEqual((len(done), done[0]["data"]["outcome"]), (1, "aborted"))
        self.assertNotIn("interrupted_by_hub", done[0]["data"])
        self.assertTrue(sess.final_response.startswith("Codex turn aborted (interrupted)"))

    async def test_a_codex_error_while_an_interaction_is_pending_ends_after_the_answer(self):
        # Finding 2: the one-shot task_complete error is consumed as it is read. A refused end
        # must keep it on the session and the screen poll must end the turn once the parent has
        # answered; before, the turn stayed running with nothing left that could end it.
        sess, path, task = await self.codex_rollout("-error-pending")
        try:
            sess.pending_interaction = {"request_id": "req-codex", "kind": "question", "prompt": "?"}
            sess.status = "waiting_parent"
            self.append(path, {"type": "event_msg", "payload": {
                "type": "task_complete", "last_agent_message": None,
                "error": {"message": "Quota exceeded. Check your plan and billing details.",
                          "codex_error_info": "usage_limit_exceeded"}}})
            await self.until(lambda: sess._pending_engine_error is not None)
            self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
            self.assertEqual(sess.status, "waiting_parent")
            sess.pending_interaction = None
            sess.status = "running"
            await self.hub.poll_tui_screen_interactions_once(sess)
            await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "quota"))
        self.assertIsNone(sess._pending_engine_error)
        done = self.events(sess.session_id, "turn_completed")
        self.assertEqual((len(done), done[0]["data"]["outcome"]), (1, "error"))

    async def test_omp_error_hold_keeps_the_row_until_the_parent_answers(self):
        # Finding 5: the error row may not be claimed (offset) while the turn cannot end.
        sess, work, path = self.omp_wire("-hold-pending")
        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with patch("session_manager.OMP_ERROR_TURN_END_SEC", 0.2):
                self.append(path, self.omp_error())
                await self.until(lambda: "Engine error (server)" in sess.current_activity)
                sess.pending_interaction = {"request_id": "req-1", "kind": "question", "prompt": "?"}
                await asyncio.sleep(0.35)
                self.assertEqual(sess.status, "running")
                self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
                self.assertFalse(getattr(sess, "_native_turn_end_offset", 0))
                sess.pending_interaction = None
                await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "server"))

    async def test_the_omp_hold_restarts_its_window_after_a_refused_end(self):
        # Nit: re-arming with the old start time ended the turn on the first pass after the parent
        # answered, before the engine's retry record could arrive. The window now restarts at each
        # refusal, so the answer buys it a full window from that moment.
        sess, work, path = self.omp_wire("-hold-window")
        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with patch("session_manager.OMP_ERROR_TURN_END_SEC", 1.2):
                self.append(path, self.omp_error())
                await self.until(lambda: "Engine error (server)" in sess.current_activity)
                sess.pending_interaction = {"request_id": "req-2", "kind": "question", "prompt": "?"}
                await asyncio.sleep(1.5)  # several refusals: each restarts the window
                self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
                sess.pending_interaction = None
                answered = time.monotonic()
                await self.until(lambda: sess.status == "completed")
                self.assertGreaterEqual(time.monotonic() - answered, 0.8)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "server"))

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

    async def test_a_kimi_failed_turn_is_retried_after_the_interaction_is_answered(self):
        # Finding 2 (kimi): the refused turn.ended is held, not dropped.
        sess, path = self.kimi_wire("-failed-held")
        task = self.hub._schedule(self.hub._watch_kimi_session(sess, self.root))
        try:
            sess.pending_interaction = {"request_id": "req-kimi", "kind": "question", "prompt": "?"}
            sess.status = "waiting_parent"
            self.append(path, {"type": "turn.ended", "agentId": "main", "turnId": 0,
                               "reason": "failed",
                               "error": {"code": "authentication_failed",
                                         "message": "Not logged in · Please run /login"}})
            await self.until(lambda: sess._pending_engine_error is not None)
            self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
            sess.pending_interaction = None
            sess.status = "running"
            await self.hub.poll_tui_screen_interactions_once(sess)
            await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "auth"))
        self.assertIsNone(sess._pending_engine_error)
        self.assertNotEqual(sess.final_response, "Not logged in · Please run /login")

    def agy_wire(self, suffix=""):
        sess = self.session("agy", suffix)
        work = self.root / f"agy{suffix}"
        path = work / ".system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"brain_dir": str(work)}
        return sess, path, self.hub._schedule(self.hub._watch_agy_transcript(sess, self.root))

    async def test_an_agy_error_tail_is_retried_after_the_interaction_is_answered(self):
        # Coverage: the tail is kept on a refused end, so the retry after the answer still ends
        # the turn as the engine's error (the row was already read and would otherwise be lost).
        sess, path, task = self.agy_wire("-error-retry")
        try:
            with patch("session_manager.AGY_ERROR_TURN_END_SEC", 0.2):
                sess.pending_interaction = {"request_id": "req-agy", "kind": "question", "prompt": "?"}
                sess.status = "waiting_parent"
                self.append(path, {"type": "ERROR_MESSAGE", "source": "SYSTEM", "status": "DONE",
                                   "error": 'API error (attempt 8): request failed: Post "https://x/v1": EOF'})
                await self.until(lambda: any(
                    e["type"] == "error" for e in self.events(sess.session_id)))
                await asyncio.sleep(0.5)  # past the window: the end was refused, the tail kept
                self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
                self.assertEqual(sess.status, "waiting_parent")
                sess.pending_interaction = None
                sess.status = "running"
                await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "network"))
        done = self.events(sess.session_id, "turn_completed")
        self.assertEqual((len(done), done[0]["data"]["outcome"]), (1, "error"))

    @staticmethod
    def omp_error(message="502 status code (no body)", status=502):
        return {"type": "message", "message": {
            "role": "assistant", "stopReason": "error", "errorStatus": status,
            "errorMessage": message, "content": []}}

    @staticmethod
    def omp_final(text):
        return {"type": "message", "message": {"role": "assistant", "stopReason": "stop",
                                               "content": [{"type": "text", "text": text}]}}

    # Rows of real live-omp captures (tools/fixtures/terminal/live-omp-approval-box): the status
    # row reads "Thinking"/"Working…" only while a model call is in flight; an idle omp settles to
    # the π row (tools/fixtures/terminal/live-omp/screen_final.txt).
    OMP_BUSY_SCREEN = (
        "❯ \n"
        "──────────────────────────────────────────────────────────────────────────────\n"
        "  ❉ Thinking ⠹ 3s · ⦸ DeepSeek Flash (clay) · 📁 …<SCRATCH> · ◫ 0.8%/1M ⟲ · $0.00 ↓\n"
    )
    OMP_IDLE_SCREEN = (
        "❯ \n"
        "──────────────────────────────────────────────────────────────────────────────\n"
        " π · ⦸ DeepSeek Flash (clay) · 📁 …<SCRATCH> · ◫ 0.8%/1M ⟲ · $0.00 ↓\n"
    )
    # The idle screen still shows the finished answer above the prompt. Its prose contains the
    # cross-engine active phrases, which are history here, never a live indicator.
    OMP_IDLE_WITH_HISTORY_SCREEN = (
        " [Hub Session: session_id=abc] list the files in this folder\n"
        " Worked for 12s · 3 tool calls\n"
        " Generating report...\n"
        " Running a command\n"
        "\n"
        " Three files, no subdirectories.\n"
        "\n"
        "❯ \n"
        "──────────────────────────────────────────────────────────────────────────────\n"
        " π · ⦸ DeepSeek Flash (clay) · 📁 …<SCRATCH> · ◫ 0.8%/1M ⟲ · $0.00 ↓\n"
    )

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

    async def test_omp_error_turn_is_deferred_while_the_live_screen_shows_work(self):
        # Incident 2026-11-23 (omp-9f5cc488): after stopReason "error" omp retried on its own and
        # the screen showed "❉ Thinking" for 126 s. The hold used to end the turn at +60 s and
        # report a false engine error although the retried record was still coming.
        sess, work, path = self.omp_wire("-busy")
        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with patch("session_manager.OMP_ERROR_TURN_END_SEC", 0.2):
                self.fake.sessions[sess.session_id].screen_text = self.OMP_BUSY_SCREEN
                self.append(path, self.omp_error())
                await self.until(lambda: "Engine error (server)" in sess.current_activity)
                await asyncio.sleep(0.9)  # several windows, each deferred by the busy screen
                self.assertEqual(sess.status, "running")
                self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
                self.assertFalse(getattr(sess, "_native_turn_end_offset", 0))
                # The screen clears: the quiet window restarts at that observation, so the turn
                # ends one full window later (not on the first silent pass).
                self.fake.sessions[sess.session_id].screen_text = self.OMP_IDLE_SCREEN
                await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "server"))

    async def test_omp_error_turn_ends_cleanly_when_history_says_worked_for_and_the_prompt_is_idle(self):
        # The incident's other half: the idle screen keeps the finished answer, and its prose
        # ("Worked for 12s", "Generating report...", "Running a command") used to match the
        # cross-engine active reader over the whole 40-line screen, so the error hold restarted
        # forever although omp sat at its prompt and never retried.
        sess, work, path = self.omp_wire("-idle-history")
        self.fake.sessions[sess.session_id].screen_text = self.OMP_IDLE_WITH_HISTORY_SCREEN
        self.assertFalse(self.hub._tui_screen_shows_work(sess))
        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with patch("session_manager.OMP_ERROR_TURN_END_SEC", 0.2):
                self.append(path, self.omp_error())
                await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "server"))
        self.assertEqual(len(self.events(sess.session_id, "turn_completed")), 1)

    async def test_omp_retry_after_a_long_busy_stretch_completes_with_the_real_answer(self):
        sess, work, path = self.omp_wire("-busy-retry")
        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with patch("session_manager.OMP_ERROR_TURN_END_SEC", 0.2):
                self.fake.sessions[sess.session_id].screen_text = self.OMP_BUSY_SCREEN
                self.append(path, self.omp_error())
                await self.until(lambda: "Engine error (server)" in sess.current_activity)
                await asyncio.sleep(0.8)  # well past several hold windows
                self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
                # The incident's own shape: the retried call first flushes an assistant toolUse
                # record (09:59:02.953Z), which must clear the hold, and only then the answer.
                self.append(path, {"type": "message", "message": {
                    "role": "assistant", "stopReason": "toolUse",
                    "content": [{"type": "toolCall", "id": "call-retry", "name": "bash",
                                 "arguments": {"command": "ls"}}]}})
                # The screen is idle now, so only the toolUse row itself can keep the turn alive:
                # past a full hold window it must still be running, not ended as the old error.
                self.fake.sessions[sess.session_id].screen_text = self.OMP_IDLE_SCREEN
                await asyncio.sleep(0.35)
                self.assertEqual(sess.status, "running")
                self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
                self.append(path, self.omp_final("retried answer"))
                await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sess.final_response, "retried answer")
        self.assertIsNone(sess.last_turn_outcome)
        done = self.events(sess.session_id, "turn_completed")
        self.assertEqual(len(done), 1)
        self.assertNotIn("outcome", done[0].get("data") or {})

    async def test_omp_watcher_and_tail_sweep_share_one_quiet_window(self):
        # The two paths must not each keep a private timer: the sweep's "still working" sighting
        # gives the watcher a full window too, so it cannot end the turn on its own older clock.
        sess, work, path = self.omp_wire("-shared-window")
        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with patch("session_manager.OMP_ERROR_TURN_END_SEC", 1.2):
                self.fake.sessions[sess.session_id].screen_text = self.OMP_BUSY_SCREEN
                self.append(path, self.omp_error("Connection reset by peer", 0))
                await self.until(lambda: "Engine error (network)" in sess.current_activity)
                await asyncio.sleep(0.55)
                # The sweep samples the busy screen later than the watcher's own error start, so
                # its mark is the newer of the two clocks.
                self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)
                self.assertIsNotNone(sess._omp_error_busy_at)
                # Idle now: the watcher's own window has elapsed (past the 1.2 s that would let it
                # end the turn alone), but the shared mark still holds it for another full window.
                self.fake.sessions[sess.session_id].screen_text = self.OMP_IDLE_SCREEN
                await asyncio.sleep(0.85)
                self.assertEqual(sess.status, "running")
                self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
                await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "network"))

    def test_omp_status_rows_are_work_only_while_thinking_or_working(self):
        thinking = "  ❉ Thinking ⠹ 3s · ⦸ DeepSeek Flash (clay) · 📁 … · ◫ 0.8%/1M ⟲ · $0.00 ↓"
        working = "  ⎋ Working… ⠦ 3s · ⦸ DeepSeek Flash (clay) · 📁 … · ◫ 0.8%/1M ⟲ · $0.00 ↓"
        idle = " π · ⦸ DeepSeek Flash (clay) · 📁 … · ◫ 0.8%/1M ⟲ · $0.00 ↓"
        answer = "❉ Thinking about the two options: the first one is simpler."
        for row in (thinking, working):
            self.assertTrue(HubSessionManager._tui_busy_indicator(row, "omp"), row)
        self.assertFalse(HubSessionManager._tui_busy_indicator(idle, "omp"))
        # A transcript line that merely starts with the word is still an answer, not a status row.
        self.assertFalse(HubSessionManager._tui_busy_indicator(answer, "omp"))

    def test_every_real_omp_status_glyph_and_row_shape_is_recognised(self):
        # The live captures cycle these frames (tools/fixtures/terminal/live-omp*-*/resumed):
        # ✸ ✹ ✺ ✻ ✼ ❉ ❊ around "Thinking", ⎋ around "Working…"/"Listing folder contents".
        frames = "✸✹✺✻✼❉❊⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        for glyph in frames:
            row = f"  {glyph} Thinking ⠹ 3s · ⦸ DeepSeek Flash (clay) · 📁 … · ◫ 0.8%/1M ⟲ · $0.00 ↓"
            self.assertTrue(ENGINE_BUSY_LINE_RES["omp"].match(row), row)
            self.assertTrue(HubSessionManager._tui_busy_indicator(row, "omp"), row)
        # The status row also drops its state word ("⠴ 4s · …") and, mid-redraw, its timer.
        for row in (
            "  ⠴ 4s · ⦸ DeepSeek Flash (clay) · 📁 … · ◫ 0.8%/1M ⟲ · $0.00 ↓",
            "  ⎋ Listing folder contents ⠇ 17s · ⦸ DeepSeek Flash (clay) · …",
            "  ✹ Thinking",
            "  ✻ Thinking",
            "  ⎋ Working…",
            "  ⎋ Listing folder contents",
        ):
            self.assertTrue(ENGINE_BUSY_LINE_RES["omp"].match(row), row)
            self.assertTrue(HubSessionManager._tui_busy_indicator(row, "omp"), row)
        for row in (
            " π · ⦸ DeepSeek Flash (clay) · 📁 … · ◫ 0.8%/1M ⟲ · $0.00 ↓",
            "❉ Thinking about the two options: the first one is simpler.",
            " Worked for 12s · 3 tool calls",
            " Generating report...",
            " Running a command",
        ):
            self.assertFalse(ENGINE_BUSY_LINE_RES["omp"].match(row), row)
            self.assertFalse(HubSessionManager._tui_busy_indicator(row, "omp"), row)

    async def test_a_session_that_resumes_working_rearms_its_parent_wait_handle(self):
        # The same incident's tail: the hub had already ended the turn as an error, then omp's retry
        # record flipped the session back to running with a new turn id. The parent's handle must
        # not answer the next poll with the stale error result.
        sess, work, path = self.omp_wire("-rearm")
        info = await self.hub.wait_inbox.create([sess.session_id], {sess.session_id: sess.turn_id})
        sess.wait_handle = info["wait_handle"]
        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with patch("session_manager.OMP_ERROR_TURN_END_SEC", 0.2):
                self.append(path, self.omp_error())
                await self.until(lambda: sess.status == "completed")
                self.append(path, {"type": "custom", "customType": "tool_execution_start",
                                   "data": {"toolCallId": "call-1", "toolName": "bash",
                                            "args": {"command": "ls"}}})
                await self.until(lambda: sess.status == "running")
                self.assertEqual((await self.hub.wait_inbox.wait(info["wait_handle"], 0.1))["state"],
                                 "waiting")
                self.append(path, self.omp_final("real answer after retry"))
                await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        done = await self.hub.wait_inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual((done["state"], done["final_response"]), ("done", "real answer after retry"))
        self.assertEqual(done["results"][0]["outcome"], "answered")
        self.assertEqual((sess.last_turn_outcome, sess.final_response), (None, "real answer after retry"))

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

    async def test_omp_tail_sweep_defers_an_error_row_while_the_live_screen_shows_work(self):
        sess, _work, path = self.omp_wire("-sweep-busy")
        self.append(path, self.omp_error("Connection reset by peer", 0))
        sess.transcript_path = path
        self.fake.sessions[sess.session_id].screen_text = self.OMP_BUSY_SCREEN
        with patch("session_manager.OMP_ERROR_TURN_END_SEC", 0.2):
            old = time.time() - 5
            os.utime(path, (old, old))
            self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)
            self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)
            self.assertEqual(sess.status, "running")
            self.assertEqual(self.events(sess.session_id, "turn_completed"), [])
            # Idle now: the quiet window restarts at the last busy observation, so the row is held
            # for one more full window before the turn may end on it.
            self.fake.sessions[sess.session_id].screen_text = self.OMP_IDLE_SCREEN
            self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)
            await asyncio.sleep(0.25)
            self.assertEqual(await self.hub._sweep_omp_turn_ends(), 1)
        self.assertEqual(sess.status, "completed")
        self.assertEqual((sess.last_turn_outcome, sess.last_turn_error["kind"]), ("error", "network"))

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
        # The per-turn evidence is persisted with the row; the answer alone is not evidence,
        # because D2 keeps it across turns.
        reported.completion_reported = True
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

    async def test_a_member_closed_mid_turn_after_a_previous_answer_is_failed(self):
        # Finding 1: turn 1 reported "A"; the parent's follow-up kept that answer (D2) and the
        # member died mid-turn. The stale answer must not read as this turn's completion.
        sess = self.session("omp", "-mid-turn")
        sess.status = "completed"
        sess.completion_reported = True
        sess.final_response = "A (turn 1)"
        await self.hub.persist_session(sess)
        self.hub.begin_new_turn(sess, status="running")
        await self.hub.persist_session(sess)
        sess.status = "crashed"
        await self.hub.persist_session(sess)
        record = await self.hub.db.get_session_record(sess.session_id)
        self.assertEqual((record["status"], record["final_response"], record["completion_reported"]),
                         ("crashed", "A (turn 1)", False))
        del self.hub.sessions[sess.session_id]
        snapshot = await self.hub._wait_state_snapshot([sess.session_id])
        self.assertEqual(snapshot["state"], "failed")

    async def test_a_row_without_stored_per_turn_state_keeps_master_behavior(self):
        # Rows written before the per-turn columns existed: a clean exit with an answer was done,
        # a crashed/terminated row with a stale answer failed — exactly what master returned.
        done = self.session("omp", "-old-done")
        done.status = "exited"
        done.final_response = "old answer"
        await self.hub.persist_session(done)
        gone = self.session("omp", "-old-crashed")
        gone.status = "crashed"
        gone.final_response = "stale answer"
        await self.hub.persist_session(gone)
        for sess in (done, gone):
            del self.hub.sessions[sess.session_id]
        # The queued upserts must land before the simulated pre-change row is written back.
        await self.hub.db.flush()
        conn = sqlite3.connect(str(self.hub.db.db_path))
        try:
            for sess in (done, gone):
                conn.execute(
                    "UPDATE sessions SET completion_reported=NULL, last_turn_outcome=NULL, "
                    "last_turn_error_json=NULL WHERE session_id=?", (sess.session_id,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual((await self.hub._wait_state_snapshot([done.session_id]))["state"], "done")
        self.assertEqual((await self.hub._wait_state_snapshot([gone.session_id]))["state"], "failed")

    async def test_a_running_follow_up_does_not_show_the_previous_error_turn(self):
        # Finding 2: after an error turn the parent's follow-up keeps the answer and its source
        # (D2); the whole new turn must not be reported as an error turn.
        sess = self.session("omp", "-follow-up")
        sess.status = "completed"
        sess.completion_reported = True
        sess.final_response = engine_error_summary("omp", {"kind": "quota", "message": "quota"})
        sess.final_response_source = "engine_error"
        sess.last_turn_outcome = "error"
        sess.last_turn_error = {"kind": "quota", "retryable": False, "message": "quota"}
        await self.hub.persist_session(sess)
        self.hub.begin_new_turn(sess, status="running")
        view = await self.hub.get_subagent_status_and_trace(sess.session_id)
        self.assertNotIn("turn_outcome", view)
        self.assertNotIn("turn_error", view)
        self.assertEqual(view["final_response"], sess.final_response)
        self.assertTrue(view["from_previous_turn"])
        # The archived row of that running turn keeps the flag's effect too.
        await self.hub.persist_session(sess)
        record = await self.hub.db.get_session_record(sess.session_id)
        self.assertEqual(parent_turn_outcome_fields(record), {})

    async def test_an_archived_empty_follow_up_does_not_report_the_previous_error(self):
        # Must-fix 1: turn N ended as quota; the follow-up (D2 keeps the answer and its source)
        # ends with an empty turn_completed, as codex/kimi really do. The archived row reports
        # completion of that empty turn, not the previous turn's engine error.
        sess = self.session("codex", "-empty-follow-up")
        info = classify_engine_error("Quota exceeded. Check your plan and billing details.",
                                     code="usage_limit_exceeded")
        self.assertTrue(await self.hub._end_engine_error_turn(sess, "codex", info))
        self.hub.begin_new_turn(sess, status="running")
        await self.hub.record_worker_event(sess.session_id, {
            "type": "turn_completed", "category": "turn_completed", "status": "turn_idle", "content": "",
        })
        self.assertEqual(sess.status, "completed")
        await self.hub.close_session(sess.session_id, source="mcp")
        record = await self.hub.db.get_session_record(sess.session_id)
        self.assertEqual((record["final_response_source"], record["completion_reported"]),
                         ("engine_error", True))
        self.assertEqual(parent_turn_outcome_fields(record), {})
        snapshot = await self.hub._wait_state_snapshot([sess.session_id])
        self.assertEqual(snapshot["state"], "done")
        self.assertEqual(snapshot["results"][0]["outcome"], "answered")
        self.assertNotIn("error", snapshot["results"][0])

    async def test_an_archived_error_row_keeps_its_stored_kind_and_message(self):
        # Round 1 item 7 / coverage: an ended error turn stores its classified kind, so the
        # archived row names rate_limit, not a kind guessed from the summary text.
        sess = self.session("claude", "-archived-kind")
        info = classify_engine_error("API Error: 429 too many requests", http_status=429)
        self.assertTrue(await self.hub._end_engine_error_turn(sess, "claude", info))
        await self.hub.close_session(sess.session_id, source="mcp")
        record = await self.hub.db.get_session_record(sess.session_id)
        self.assertEqual(record["last_turn_error"],
                         {"kind": "rate_limit", "retryable": True,
                          "message": "API Error: 429 too many requests"})
        self.assertEqual(parent_turn_outcome_fields(record)["turn_error"]["kind"], "rate_limit")
        self.assertTrue(record["final_response"].startswith(
            "claude turn ended on an engine error (rate_limit): API Error: 429 too many requests"))
        rows = await self.hub.db.list_persisted_sessions(limit=5)
        listed = next(row for row in rows if row["session_id"] == sess.session_id)
        self.assertEqual(listed["last_turn_error"]["kind"], "rate_limit")

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
        # Only a row written before completion_reported was stored reaches that fallback: a live
        # follow-up (a bool, from_previous_turn set) is not the previous turn's error.
        self.assertEqual(parent_turn_outcome_fields({
            "final_response_source": "engine_error", "final_response": "codex turn ended...",
            "from_previous_turn": True, "completion_reported": False}), {})
        self.assertEqual(parent_turn_outcome_fields({
            "final_response_source": "engine_error", "final_response": "codex turn ended...",
            "completion_reported": True}), {})


if __name__ == "__main__":
    unittest.main()
