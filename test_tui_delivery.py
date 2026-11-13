"""Long Codex steering must never concatenate drafts or claim transcript growth as delivery."""
import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from session_manager import AgentSession, HubSessionManager
from test_native_records import FakePty


class CodexDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-delivery-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()
        self.sess = AgentSession("codex-delivery", "codex", "Codex", "tui", "Test", str(self.root), "unused", [])
        self.sess.engine = "codex"
        self.sess.status = "completed"
        self.sess.pty = FakePty()
        self.sess._loop = asyncio.get_running_loop()
        self.screen = "› \n\n  ? for shortcuts"
        self.sess.get_clean_screen_text = lambda **kw: self.screen
        self.hub.sessions[self.sess.session_id] = self.sess
        self.path = self.root / "rollout.jsonl"
        self.path.write_text("", encoding="utf-8")
        self.sess.transcript_path = self.path

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    def append_user(self, text):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}]}}, ensure_ascii=False) + "\n")

    async def test_exact_native_proof_rejects_growth_old_and_partial_messages(self):
        self.append_user("expected middle END")
        size = self.path.stat().st_size
        self.append_user("expected WRONG END")
        with self.path.open("a") as f:
            f.write('{"type":"event_msg","payload":{"type":"token_count"}}\n')
        self.assertFalse(self.hub._probe_transcript_for_user_submit(self.sess, size, "expected", "END",
                                                                  expected_text="expected middle END"))
        self.append_user("expected middle END")
        self.assertTrue(self.hub._probe_transcript_for_user_submit(self.sess, size, "", "",
                                                                 expected_text="expected middle END"))

    async def test_one_enter_and_exact_multiline_proof(self):
        text = "Long canary 中文\n" + "ordinary prose with spaces " * 100 + "END"
        def write(data):
            self.sess.pty.inputs.append(data)
            if data == "\r": self.append_user(text)
            return True
        with patch.object(self.sess, "write_input", write), patch.object(self.hub, "_await_typed_text_settled",
                AsyncMock(return_value=("› draft", True, True, 1))):
            result = await self.hub._submit_text_to_tui(self.sess, text, "\r")
        self.assertTrue(result["ok"])
        self.assertEqual(self.sess.pty.inputs, ["\x1b[200~" + text + "\x1b[201~", "\r"])
        self.assertIsNone(self.sess._tui_draft_pending)

    async def test_unsettled_text_never_presses_enter_and_blocks_next_send(self):
        with patch.object(self.hub, "_await_typed_text_settled", AsyncMock(return_value=("› bad", True, False, 10))):
            result = await self.hub._submit_text_to_tui(self.sess, "long original message", "\r")
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 0)
        self.assertNotIn("\r", self.sess.pty.inputs)
        self.assertTrue(Path(result["diagnostic_path"]).exists())
        before = list(self.sess.pty.inputs)
        response = await self.hub.deliver_parent_message(self.sess.session_id, "do not concatenate")
        self.assertFalse(response["delivered"])
        self.assertEqual(response["reason"], "input_recovery_required")
        self.assertEqual(self.sess.pty.inputs, before)

    async def test_unconfirmed_has_no_retry_even_when_composer_clears(self):
        with patch.object(self.hub, "_await_typed_text_settled", AsyncMock(return_value=(self.screen, True, True, 1))), \
             patch("session_manager.TUI_SUBMIT_EVIDENCE_SEC", .02):
            result = await self.hub._submit_text_to_tui(self.sess, "unconfirmed canary", "\r")
        self.assertIsNone(result["ok"])
        self.assertEqual(self.sess.pty.inputs.count("\r"), 1)
        self.append_user("unconfirmed canary")
        self.assertTrue(self.hub._reconcile_codex_submit(self.sess))

    async def test_existing_draft_and_empty_message_never_write(self):
        self.screen = "› leftover text\n\n  ? for shortcuts"
        result = await self.hub._submit_text_to_tui(self.sess, "another message", "\r")
        self.assertEqual(result["reason"], "input_recovery_required")
        self.sess.last_injection = {"ok": False, "reason": "previous_failure"}
        result = await self.hub.deliver_parent_message(self.sess.session_id, "", press_enter=False)
        self.assertEqual(result["reason"], "empty_text")
        self.assertFalse(self.sess.pty.inputs)

    async def test_clear_is_guarded_and_confirmed_without_submitting_draft(self):
        result = await self.hub._clear_tui_draft(self.sess)
        self.assertEqual(result["reason"], "input_already_empty")
        self.screen = "› leftover text\n\n  ? for shortcuts"
        self.sess._tui_draft_pending = {"reason": "failure"}
        self.sess.status = "running"
        with self.assertRaises(ValueError): await self.hub._clear_tui_draft(self.sess)
        self.assertFalse(self.sess.pty.inputs)
        self.sess.status = "completed"
        def write(data):
            self.sess.pty.inputs.append(data)
            self.screen = "› \n\n  ? for shortcuts"
            return True
        with patch.object(self.sess, "write_input", write):
            result = await self.hub.deliver_parent_message(self.sess.session_id, "", clear_input=True)
        self.assertTrue(result["input_cleared"])
        self.assertEqual(self.sess.pty.inputs, ["\x03"])
        self.assertIsNone(self.sess._tui_draft_pending)

    async def test_request_report_never_uses_another_injection(self):
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            server = importlib.import_module("server")
        self.sess.last_injection = {"submit_id": "first", "ok": False}
        self.sess.last_injection = {"submit_id": "second", "pending": True}
        self.assertFalse(server._injection_report(self.sess, submit_id="first")["last_injection"]["ok"])
        self.assertIsNone(server._injection_report(self.sess, submit_id="")["last_injection"])
        self.assertIsNone(server._injection_report(self.sess, submit_id="missing")["last_injection"])

    def test_split_conpty_paste_placeholder_counts_literal_prefix(self):
        self.assertTrue(self.hub._tui_paste_placeholder_holds("codex", "literal prefix [Pasted Content 2000 chars]", 2015))

    async def test_repeated_tail_never_counts_as_complete_paste(self):
        text = "Repeated tail. " * 50
        self.screen = "› Repeated tail.\n\n  ? for shortcuts"
        loop = asyncio.get_running_loop()
        update = loop.call_later(.08, lambda: setattr(self, "screen", "› " + text.strip() + "\n\n  ? for shortcuts"))
        started = loop.time()
        try:
            with patch("session_manager.TUI_TYPE_SETTLE_SEC", .02), patch("session_manager.TUI_TYPE_SETTLE_POLL_SEC", .01):
                result = await self.hub._await_typed_text_settled(self.sess, "codex", text[:30], text[-30:],
                                                               typed_len=len(text), expected_text=text)
            self.assertTrue(result[2])
            self.assertGreaterEqual(loop.time() - started, .08)
        finally:
            update.cancel()

    LONG = ("One follow-up, no game and no code change. The record says a call returned state, "
            "but no file holds that call or its answer. Write the raw file with each command and "
            "its full return, then add one line under the table. Report in under 5 lines.")

    def test_scrolled_composer_showing_the_end_counts_as_whole(self):
        # 80x24 shape: the top rows scrolled out of the composer, an arrow marks them.
        visible = self.LONG[self.LONG.index("but no file"):]
        line = visible[:60] + "   ↑ " + visible[60:]
        self.assertTrue(self.hub._codex_composer_shows_whole(line, self.LONG))
        self.assertTrue(self.hub._codex_composer_shows_whole(self.LONG, self.LONG))
        # A scrolled view whose end is not the text's end (still growing) never counts.
        middle = self.LONG[self.LONG.index("but no file"):self.LONG.index("then add")]
        self.assertFalse(self.hub._codex_composer_shows_whole(middle[:40] + " ↑ " + middle[40:], self.LONG))
        # Without the arrow a bare suffix is a partial draft, not a scrolled one.
        self.assertFalse(self.hub._codex_composer_shows_whole(visible, self.LONG))
        # Too little visible text is not trusted.
        self.assertFalse(self.hub._codex_composer_shows_whole("↑ " + self.LONG[-20:], self.LONG))

    async def test_scrolled_composer_settles(self):
        visible = self.LONG[self.LONG.index("but no file"):]
        self.screen = "› " + visible[:60] + "   ↑\n  " + visible[60:] + "\n\n  ? for shortcuts"
        with patch("session_manager.TUI_TYPE_SETTLE_SEC", .02), patch("session_manager.TUI_TYPE_SETTLE_POLL_SEC", .01):
            result = await self.hub._await_typed_text_settled(self.sess, "codex", self.LONG[:30], self.LONG[-30:],
                                                           typed_len=len(self.LONG), expected_text=self.LONG)
        self.assertTrue(result[2])

    async def test_text_still_growing_at_the_cap_keeps_waiting(self):
        # Send #2 shape: Codex takes the paste in a few words per frame, past the cap.
        loop = asyncio.get_running_loop()
        words = self.LONG.split(" ")
        state = {"n": 1}
        def grow():
            state["n"] += 1
            self.screen = "› " + " ".join(words[:state["n"]]) + "\n\n  ? for shortcuts"
            if state["n"] < len(words):
                handles.append(loop.call_later(.01, grow))
        handles = [loop.call_later(.01, grow)]
        try:
            with patch("session_manager.TUI_TYPE_SETTLE_SEC", .05), patch("session_manager.TUI_TYPE_SETTLE_POLL_SEC", .005), \
                 patch("session_manager.TUI_TYPE_SETTLE_CAP_SEC", .05), patch("session_manager.TUI_TYPE_STALL_SEC", .2):
                result = await self.hub._await_typed_text_settled(self.sess, "codex", self.LONG[:30], self.LONG[-30:],
                                                               typed_len=len(self.LONG), expected_text=self.LONG)
        finally:
            for h in handles: h.cancel()
        self.assertTrue(result[2])
        self.assertGreater(result[3], .05)

    async def test_stalled_partial_text_gives_up_after_the_stall(self):
        # typed_len=1 keeps the codex length floor (typed_len * .01 s) under the patched cap.
        self.screen = "› " + self.LONG[:80] + "\n\n  ? for shortcuts"
        with patch("session_manager.TUI_TYPE_SETTLE_POLL_SEC", .005), patch("session_manager.TUI_TYPE_SETTLE_CAP_SEC", .05), \
             patch("session_manager.TUI_TYPE_STALL_SEC", .1), patch("session_manager.TUI_TYPE_SETTLE_HARD_CAP_SEC", 5):
            result = await self.hub._await_typed_text_settled(self.sess, "codex", self.LONG[:30], self.LONG[-30:],
                                                           typed_len=1, expected_text=self.LONG)
        self.assertFalse(result[2])
        self.assertLess(result[3], 1)

    async def test_growth_stops_at_the_hard_cap(self):
        loop = asyncio.get_running_loop()
        state = {"n": 0}
        def churn():
            state["n"] += 1
            self.screen = "› partial " + "x" * (state["n"] % 7) + "\n\n  ? for shortcuts"
            handles.append(loop.call_later(.01, churn))
        handles = [loop.call_later(.01, churn)]
        try:
            with patch("session_manager.TUI_TYPE_SETTLE_POLL_SEC", .005), patch("session_manager.TUI_TYPE_SETTLE_CAP_SEC", .05), \
                 patch("session_manager.TUI_TYPE_STALL_SEC", 1), patch("session_manager.TUI_TYPE_SETTLE_HARD_CAP_SEC", .3):
                result = await self.hub._await_typed_text_settled(self.sess, "codex", self.LONG[:30], self.LONG[-30:],
                                                               typed_len=1, expected_text=self.LONG)
        finally:
            for h in handles: h.cancel()
        self.assertFalse(result[2])
        self.assertLess(result[3], 1)

    def test_placeholder_is_empty_only_with_native_cursor_at_start(self):
        self.sess.screen.cursor.x = 2
        self.assertTrue(self.hub._codex_composer_empty(self.sess, "Ask Codex to do anything"))
        self.sess.screen.cursor.x = 26
        self.assertFalse(self.hub._codex_composer_empty(self.sess, "Ask Codex to do anything"))

    async def test_failure_event_does_not_mutate_native_terminal(self):
        before = list(self.sess.raw_history)
        await self.hub._record_tui_injection(self.sess, {"ok": False, "reason": "unsettled", "attempts": 0})
        self.assertEqual(self.sess.raw_history, before)
        self.assertTrue(any(e["type"] == "input_delivery" for e in self.hub.get_worker_events(self.sess.session_id)))

    async def test_draft_only_write_does_not_start_a_turn(self):
        result = await self.hub.deliver_parent_message(self.sess.session_id, "draft only", press_enter=False)
        self.assertTrue(result["pty_written"])
        self.assertEqual(self.sess.status, "completed")
        self.assertEqual(self.sess.last_injection["attempts"], 0)
        self.assertEqual(result["submit_id"], self.sess.last_injection["submit_id"])

    async def test_missing_native_transcript_never_types_or_claims_old_message(self):
        self.sess.transcript_path = None
        result = await self.hub._submit_text_to_tui(self.sess, "canary", "\r")
        self.assertEqual(result["reason"], "native_transcript_unavailable")
        self.assertFalse(result["ok"])
        self.assertFalse(self.sess.pty.inputs)

    async def test_five_engines_confirm_from_native_records_without_reading_screen(self):
        text = "Native exact message 中文"
        records = {
            "codex": {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": text}]}},
            "claude": {"type": "user", "message": {"content": [{"type": "text", "text": text}]}},
            "omp": {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
            "kimi": {"type": "turn.prompt", "origin": {"kind": "user"}, "input": [{"text": text}]},
            "agy": {"type": "USER_INPUT", "content": text},
        }
        with patch.object(self.sess, "get_clean_screen_text", side_effect=AssertionError("Screen is not a submit proof")):
            for engine, record in records.items():
                with self.subTest(engine=engine):
                    self.path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
                    result = await self.hub._confirm_tui_submit(self.sess, text[:5], text[-5:], 0, engine,
                                                             marks={"expected_text": text})
                    self.assertEqual(result, ("confirmed", "transcript_user_message"))

    async def test_five_engines_do_not_fall_back_to_screen_or_transcript_growth(self):
        self.path.write_text('{"type":"event_msg","payload":{"type":"token_count"}}\n', encoding="utf-8")
        with patch.object(self.sess, "get_clean_screen_text", side_effect=AssertionError("Screen must not decide submission")):
            for engine in ("codex", "claude", "omp", "kimi", "agy"):
                with self.subTest(engine=engine):
                    result = await self.hub._confirm_tui_submit(self.sess, "native", "message", 0, engine,
                        deadline=time.time() + .01, marks={"expected_text": "native exact message"})
                    self.assertEqual(result, ("unknown", "unconfirmed"))

    async def test_failed_send_requires_inspection_instead_of_collecting_previous_turn(self):
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            server = importlib.import_module("server")
        self.sess._tui_draft_pending = {"reason": "unsubmitted_draft"}
        self.sess.last_injection = {"submit_id": "old", "ok": True}
        with patch.object(server, "hub", self.hub), patch.object(server, "ensure_profiles_loaded", AsyncMock()):
            result = await server.send_to_subagent(self.sess.session_id, "do not append", wait_seconds=0)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["delivery"]["reason"], "input_recovery_required")
        self.assertFalse(result["delivery"]["delivered"])


if __name__ == "__main__":
    unittest.main()
