"""Long Codex steering must never concatenate drafts or claim transcript growth as delivery."""
import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fake_terminal_core import FakeTerminal
from session_manager import AgentSession, HubSessionManager, ScreenRead


class CodexDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-delivery-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        self.fake = FakeTerminal()
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.hub.core = self.fake
        await self.hub.start()
        self.sess = AgentSession("codex-delivery", "codex", "Codex", "tui", "Test", str(self.root), "unused", [],
                                 core=self.fake)
        self.sess.engine = "codex"
        self.sess.status = "completed"
        self.sess._loop = asyncio.get_running_loop()
        self.hub.sessions[self.sess.session_id] = self.sess
        self.fake.spawn(self.sess.session_id, ["fixture"])
        self.hub._on_core_tick()
        # The keystrokes the seam's `write` queued, in order (what the old FakePty recorded).
        self.pty_inputs = self.fake.inputs_of(self.sess.session_id)
        self.screen = "› \n\n  ? for shortcuts"
        self.cursor_x = 0
        # §10: the screen is the core's `screen` reply (the fake's), not a pyte object's display.
        self.sess.read_screen = lambda **kw: ScreenRead(self.screen, self.cursor_x, 0, 120, 36, 0)
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
            self.pty_inputs.append(data)
            if data == "\r": self.append_user(text)
            return True
        with patch.object(self.sess, "write_input", write), patch.object(self.hub, "_await_typed_text_settled",
                AsyncMock(return_value=("› draft", True, True, 1))):
            result = await self.hub._submit_text_to_tui(self.sess, text, "\r")
        self.assertTrue(result["ok"])
        self.assertEqual(self.pty_inputs, ["\x1b[200~" + text + "\x1b[201~", "\r"])
        self.assertIsNone(self.sess._tui_draft_pending)

    async def test_unsettled_text_never_presses_enter_and_blocks_next_send(self):
        with patch.object(self.hub, "_await_typed_text_settled", AsyncMock(return_value=("› bad", True, False, 10))):
            result = await self.hub._submit_text_to_tui(self.sess, "long original message", "\r")
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 0)
        self.assertNotIn("\r", self.pty_inputs)
        self.assertTrue(Path(result["diagnostic_path"]).exists())
        before = list(self.pty_inputs)
        response = await self.hub.deliver_parent_message(self.sess.session_id, "do not concatenate")
        self.assertFalse(response["delivered"])
        self.assertEqual(response["reason"], "input_recovery_required")
        self.assertIn("clear_input=true", response["recovery_hint"])
        self.assertEqual(self.pty_inputs, before)
        # Mid-turn clear_input is refused, so the hint must not send the parent there first
        # (codex-624fde89: the parent was left with no way forward).
        self.sess.status = "running"
        response = await self.hub.deliver_parent_message(self.sess.session_id, "STOP")
        self.assertEqual(response["reason"], "input_recovery_required")
        self.assertIn("has not read it", response["recovery_hint"])
        self.assertIn("only between turns", response["recovery_hint"])
        self.assertIn("terminate_subagent", response["recovery_hint"])
        self.assertEqual(self.pty_inputs, before)

    async def test_a_queued_live_message_says_it_has_not_been_read(self):
        with patch.object(self.hub, "_submit_queued_live_text", AsyncMock()):
            response = await self.hub.deliver_parent_message(self.sess.session_id, "a steer")
        self.assertIsNone(response["delivered"])
        self.assertEqual(response["injection"], "pending")
        self.assertEqual(response["state"], "queued")
        self.assertIn("not read yet", response["note"])

    async def test_unconfirmed_has_no_retry_even_when_composer_clears(self):
        with patch.object(self.hub, "_await_typed_text_settled", AsyncMock(return_value=(self.screen, True, True, 1))), \
             patch("session_manager.TUI_SUBMIT_EVIDENCE_SEC", .02):
            result = await self.hub._submit_text_to_tui(self.sess, "unconfirmed canary", "\r")
        self.assertIsNone(result["ok"])
        self.assertEqual(self.pty_inputs.count("\r"), 1)
        self.append_user("unconfirmed canary")
        self.assertTrue(self.hub._reconcile_codex_submit(self.sess))

    async def test_existing_draft_and_empty_message_never_write(self):
        self.screen = "› leftover text\n\n  ? for shortcuts"
        result = await self.hub._submit_text_to_tui(self.sess, "another message", "\r")
        self.assertEqual(result["reason"], "input_recovery_required")
        self.sess.last_injection = {"ok": False, "reason": "previous_failure"}
        result = await self.hub.deliver_parent_message(self.sess.session_id, "", press_enter=False)
        self.assertEqual(result["reason"], "empty_text")
        self.assertFalse(self.pty_inputs)

    async def test_clear_is_guarded_and_confirmed_without_submitting_draft(self):
        result = await self.hub._clear_tui_draft(self.sess)
        self.assertEqual(result["reason"], "input_already_empty")
        self.screen = "› leftover text\n\n  ? for shortcuts"
        self.sess._tui_draft_pending = {"reason": "failure"}
        self.sess.status = "running"
        with self.assertRaises(ValueError): await self.hub._clear_tui_draft(self.sess)
        self.assertFalse(self.pty_inputs)
        self.sess.status = "completed"
        def write(data):
            self.pty_inputs.append(data)
            self.screen = "› \n\n  ? for shortcuts"
            return True
        with patch.object(self.sess, "write_input", write):
            result = await self.hub.deliver_parent_message(self.sess.session_id, "", clear_input=True)
        self.assertTrue(result["input_cleared"])
        self.assertEqual(self.pty_inputs, ["\x03"])
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

    def test_scroll_mark_flush_against_a_full_row_counts(self):
        # Live (live-codex-scrolled-paste): a full top row ends "…without changing↑", no space before the mark.
        visible = self.LONG[self.LONG.index("but no file"):]
        cut = visible.rindex(" ", 0, 60)  # the top row ends on a word, the mark right after it
        self.assertTrue(self.hub._codex_composer_shows_whole(visible[:cut] + "↑ " + visible[cut + 1:], self.LONG))
        # An arrow inside a word is text, not the mark.
        self.assertFalse(self.hub._codex_composer_shows_whole(visible.replace("file", "fi↑le", 1), self.LONG))

    STOP = ("STOP — do not start another run. Reply now with what you measured so far "
            "and where the raw files are.")

    def test_an_em_dash_the_composer_does_not_draw_still_counts_as_whole(self):
        # codex-624fde89 (2026-11-16): the composer drew the typed em dash as nothing, the exact
        # match never held, so Enter was never pressed and the steer sat in the composer.
        shown = self.STOP.replace("—", "")
        self.assertTrue(self.hub._codex_composer_shows_whole(shown, self.STOP))
        self.assertTrue(self.hub._codex_composer_shows_whole(self.STOP, self.STOP))
        # A draft still arriving is never whole, dash or no dash.
        self.assertFalse(self.hub._codex_composer_shows_whole(shown[:-12], self.STOP))
        self.assertFalse(self.hub._codex_composer_shows_whole(self.STOP[:-12], self.STOP))

    async def test_an_em_dash_the_composer_does_not_draw_settles(self):
        self.screen = "› " + self.STOP.replace("—", "") + "\n\n  ? for shortcuts"
        with patch("session_manager.TUI_TYPE_SETTLE_SEC", .02), patch("session_manager.TUI_TYPE_SETTLE_POLL_SEC", .01):
            result = await self.hub._await_typed_text_settled(self.sess, "codex", self.STOP[:30], self.STOP[-30:],
                                                           typed_len=len(self.STOP), expected_text=self.STOP)
        self.assertTrue(result[2])

    async def test_scrolled_composer_with_a_flush_mark_settles(self):
        visible = self.LONG[self.LONG.index("but no file"):]
        cut = visible.rindex(" ", 0, 60)
        self.screen = "› " + visible[:cut] + "↑\n  " + visible[cut + 1:] + "\n\n  ? for shortcuts"
        with patch("session_manager.TUI_TYPE_SETTLE_SEC", .02), patch("session_manager.TUI_TYPE_SETTLE_POLL_SEC", .01):
            result = await self.hub._await_typed_text_settled(self.sess, "codex", self.LONG[:30], self.LONG[-30:],
                                                           typed_len=len(self.LONG), expected_text=self.LONG)
        self.assertTrue(result[2])

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
        self.cursor_x = 2
        self.assertTrue(self.hub._codex_composer_empty(self.sess, "Ask Codex to do anything"))
        self.cursor_x = 26
        self.assertFalse(self.hub._codex_composer_empty(self.sess, "Ask Codex to do anything"))

    async def test_failure_event_does_not_mutate_native_terminal(self):
        # The terminal the seam would read: a failure record must not write to it.
        before = self.fake.session(self.sess.session_id).ring
        await self.hub._record_tui_injection(self.sess, {"ok": False, "reason": "unsettled", "attempts": 0})
        self.assertEqual(self.fake.session(self.sess.session_id).ring, before)
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
        self.assertFalse(self.pty_inputs)

    async def test_five_engines_confirm_from_native_records_without_reading_screen(self):
        text = "Native exact message 中文"
        records = {
            "codex": {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": text}]}},
            "claude": {"type": "user", "message": {"content": [{"type": "text", "text": text}]}},
            "omp": {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
            "kimi": {"type": "turn.prompt", "origin": {"kind": "user"}, "input": [{"text": text}]},
            "agy": {"type": "USER_INPUT", "content": text},
        }
        with patch.object(self.sess, "read_screen", side_effect=AssertionError("Screen is not a submit proof")):
            for engine, record in records.items():
                with self.subTest(engine=engine):
                    self.path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
                    result = await self.hub._confirm_tui_submit(self.sess, text[:5], text[-5:], 0, engine,
                                                             marks={"expected_text": text})
                    self.assertEqual(result, ("confirmed", "transcript_user_message"))

    async def test_five_engines_do_not_fall_back_to_screen_or_transcript_growth(self):
        self.path.write_text('{"type":"event_msg","payload":{"type":"token_count"}}\n', encoding="utf-8")
        with patch.object(self.sess, "read_screen", side_effect=AssertionError("Screen must not decide submission")):
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


class UndeliveredWakeTests(unittest.IsolatedAsyncioTestCase):
    """Owner decision hub-friction-1116 (undelivered-wake): a live message the worker never read wakes
    wait_url with state=undelivered, leaves it waiting, and is listed in the turn's done result."""

    # The Codex fixture without re-running its tests.
    asyncTearDown = CodexDeliveryTests.asyncTearDown
    append_user = CodexDeliveryTests.append_user

    async def asyncSetUp(self):
        await CodexDeliveryTests.asyncSetUp(self)
        self.sess.status = "running"  # a steer in the middle of a turn
        self.handle = (await self.hub.wait_inbox.create([self.sess.session_id]))["wait_handle"]

    async def test_a_steer_stuck_in_the_composer_wakes_the_wait_and_lands_in_the_result(self):
        with patch.object(self.hub, "_await_typed_text_settled", AsyncMock(return_value=("› STOP", True, False, 10))):
            reply = await self.hub.deliver_parent_message(self.sess.session_id, "STOP now")
            woke = await self.hub.wait_inbox.wait(self.handle, 5)
        self.assertEqual(woke["state"], "undelivered", woke)
        [message] = woke["messages"]
        self.assertEqual(message["message_id"], reply["submit_id"])
        self.assertEqual(message["reason"], "typed_text_not_settled")
        self.assertTrue(message["confirmed_failure"])
        self.assertIn("still in the worker's composer", message["recovery_hint"])
        self.assertIn("terminate_subagent, then resume_subagent", message["recovery_hint"])
        self.assertEqual(self.hub.wait_inbox._row(self.handle)["state"], "waiting")
        self.assertEqual((await self.hub.wait_inbox.wait(self.handle, 0.1))["state"], "waiting")
        await self.hub.record_worker_event(self.sess.session_id, {"type": "turn_completed", "content": "Stopped."})
        done = await self.hub.wait_inbox.wait(self.handle, 1)
        self.assertEqual(done["state"], "done")
        self.assertEqual([m["message_id"] for m in done["results"][0]["undelivered_messages"]], [reply["submit_id"]])
        # The next turn starts clean.
        self.hub.begin_new_turn(self.sess, status="running")
        self.assertEqual(self.sess.undelivered_messages, [])

    async def test_an_unconfirmed_submit_that_lands_later_is_dropped_from_the_result(self):
        with patch.object(self.hub, "_await_typed_text_settled", AsyncMock(return_value=(self.screen, True, True, 1))), \
             patch("session_manager.TUI_SUBMIT_EVIDENCE_SEC", .02):
            result = await self.hub._submit_text_to_tui(self.sess, "queued steer", "\r")
        self.assertIsNone(result["ok"])
        await self.hub._record_tui_injection(self.sess, dict(result, mode="parent_message", submit_id="inj-late"))
        woke = await self.hub.wait_inbox.wait(self.handle, 1)
        self.assertEqual(woke["state"], "undelivered")
        self.assertFalse(woke["messages"][0]["confirmed_failure"])
        self.assertIn("Do not resend", woke["messages"][0]["recovery_hint"])
        self.append_user("queued steer")  # Codex submitted it after its next tool call
        await self.hub.record_worker_event(self.sess.session_id, {"type": "turn_completed", "content": "Done."})
        done = await self.hub.wait_inbox.wait(self.handle, 1)
        self.assertNotIn("undelivered_messages", done["results"][0])
        self.assertIsNone(self.sess._tui_draft_pending)

    async def test_unchecked_writes_and_startup_text_never_wake(self):
        await self.hub._record_tui_injection(self.sess, dict(self.hub._unchecked_live_result(), submit_id="inj-u"))
        await self.hub._record_tui_injection(self.sess, {"ok": False, "reason": "write_failed", "attempts": 0})
        self.assertEqual((await self.hub.wait_inbox.wait(self.handle, 0.1))["state"], "waiting")
        self.assertEqual(self.sess.undelivered_messages, [])

    async def test_a_group_result_lists_it_from_the_live_session(self):
        await self.hub._record_tui_injection(self.sess, {"ok": False, "reason": "write_failed", "attempts": 0,
                                                         "mode": "parent_message", "submit_id": "inj-g"})
        self.sess.status = "completed"
        self.sess.completion_reported = True
        state = await self.hub._wait_state_snapshot([self.sess.session_id])
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["results"][0]["undelivered_messages"][0]["message_id"], "inj-g")
        json.dumps(state)  # nothing but plain data reaches the parent


class ScreenEchoKeyTests(unittest.TestCase):
    def test_drops_what_terminals_draw_differently_and_keeps_letters(self):
        from session_manager import screen_echo_key
        self.assertEqual(screen_echo_key("a — b… “c” → d×e │"), "abcde")
        self.assertEqual(screen_echo_key("x\u200dy\u0301z"), "xyz")  # zero-width joiner, combining acute
        # Ambiguous-width letters are text, compared as typed; CJK and full-width punctuation too.
        self.assertEqual(screen_echo_key("да αβ"), "даαβ")
        self.assertEqual(screen_echo_key("停止，回复"), "停止，回复")
        # Nothing but such characters: compared as typed, whitespace aside.
        self.assertEqual(screen_echo_key(" → — "), "→—")
        self.assertEqual(screen_echo_key(None), "")


if __name__ == "__main__":
    unittest.main()
