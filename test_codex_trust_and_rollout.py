"""Codex folder-trust page auto-dismiss/approval and rollout ownership regressions."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fake_terminal_core import FakeTerminal
from session_manager import (AgentSession, HubSessionManager, ScreenRead,
                             parse_codex_trust_prompt,
                             tui_trust_dialog_visible)


TRUST_SCREEN = """  Folder access

  E:\\misc\\mcp-agent-hub

  Trust this folder?

  › 1. Trust and continue
    2. Back to Agent Command Center

  enter continue · esc back
"""

TRUST_SCREEN_WRAPPED = """  Folder access

  Trust
  this
  folder?

  › 1. Trust and continue
    2. Back to Agent Command Center

  enter continue · esc back
"""

COMPOSER_SCREEN = "› \n\n  ? for shortcuts"


class TrustParserTests(unittest.TestCase):
    def test_real_folder_access_page(self):
        parsed = parse_codex_trust_prompt(TRUST_SCREEN)
        self.assertEqual(parsed["header"], "Folder access")
        self.assertEqual(parsed["folder"], "E:\\misc\\mcp-agent-hub")
        self.assertEqual([o["label"] for o in parsed["options"]],
                         ["Trust and continue", "Back to Agent Command Center"])
        self.assertEqual(parsed["option_actions"], ["approve_once", "deny"])
        self.assertIn("enter continue", parsed["footer"])

    def test_narrow_screen_wraps_the_question(self):
        parsed = parse_codex_trust_prompt(TRUST_SCREEN_WRAPPED)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["option_actions"], ["approve_once", "deny"])
        self.assertTrue(tui_trust_dialog_visible(TRUST_SCREEN_WRAPPED))
        # The question alone (no title) is the page too.
        only_question = "Trust\nthis\nfolder?\n1. Trust and continue\n2. Back to Agent Command Center"
        self.assertEqual(parse_codex_trust_prompt(only_question)["header"], "Trust this folder?")

    def test_ordinary_screen_is_not_a_trust_page(self):
        self.assertIsNone(parse_codex_trust_prompt(COMPOSER_SCREEN))
        self.assertIsNone(parse_codex_trust_prompt("Worked for 35s"))
        self.assertFalse(tui_trust_dialog_visible(COMPOSER_SCREEN))

    def test_missing_option_rows_fall_back_to_the_two_known_choices(self):
        parsed = parse_codex_trust_prompt("Folder access\n\nTrust this folder?\n")
        self.assertEqual([o["action"] for o in parsed["options"]], ["approve_once", "deny"])


class _HubCase(unittest.IsolatedAsyncioTestCase):
    create_default_session = True

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-codex-trust-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.fake = FakeTerminal()
        self.hub.core = self.fake
        await self.hub.start()
        self.written = []
        self.screen = TRUST_SCREEN
        if self.create_default_session:
            self.sess = self.session("codex-trust")
            self.sess.read_screen = lambda **kw: ScreenRead(self.screen, 0, 0, 120, 36, 0)
            self.sess.write_input = self.record_write

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    def session(self, sid, engine="codex"):
        sess = AgentSession(sid, engine, "Codex", "tui", "Test", str(self.root), "unused", [],
                            core=self.fake)
        sess.engine = engine
        sess.status = "starting"
        sess._loop = asyncio.get_running_loop()
        # The filesystem's birth clock and time.time() are different sources; a rollout written a
        # few ms after the object exists must still count as created after the spawn.
        sess.spawned_at = time.time() - 1.0
        self.hub.sessions[sid] = sess
        self.fake.spawn(sid, ["fixture"])
        self.hub._on_core_tick()
        return sess

    def record_write(self, text):
        self.written.append(text)
        return True

    def patched_trust_timing(self):
        return patch.multiple(
            "session_manager",
            TUI_DIALOG_POLL_SEC=0.01,
            TUI_TRUST_STABLE_SEC=0.01,
            TUI_TRUST_CONFIRM_SETTLE_SEC=0.01,
            TUI_TRUST_RETRY_INTERVAL_SEC=0.01,
        )


class TrustStartupWatcherTests(_HubCase):
    async def test_enter_dropped_before_the_page_settled_is_pressed_again(self):
        """Codex discards the first typeahead; the second Enter closes the page."""
        presses = {"n": 0}

        def write(text):
            self.written.append(text)
            if text == "\r":
                presses["n"] += 1
                if presses["n"] >= 2:
                    self.screen = COMPOSER_SCREEN
            return True

        self.sess.write_input = write
        with self.patched_trust_timing():
            task = self.hub._schedule(
                self.hub._watch_tui_startup_and_type(self.sess, None, 0.0, "\r")
            )
            try:
                await asyncio.wait_for(task, 5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(presses["n"], 2)
        self.assertEqual(self.written, ["\r", "\r"])

    async def test_exhausted_auto_dismiss_becomes_a_parent_approval(self):
        with self.patched_trust_timing(), patch("session_manager.TUI_TRUST_MAX_ATTEMPTS", 2):
            task = self.hub._schedule(
                self.hub._watch_tui_startup_and_type(self.sess, None, 0.0, "\r")
            )
            try:
                async with asyncio.timeout(5):
                    while not self.sess.trust_auto_dismiss_exhausted:
                        await asyncio.sleep(0.01)
                await asyncio.sleep(0.1)
                self.assertEqual(self.written.count("\r"), 2, "no Enter after giving up")
                interaction = await self.hub.poll_tui_screen_interactions_once(self.sess)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(interaction["kind"], "approval_request")
        self.assertEqual(interaction["metadata"]["codex_prompt"], "trust")
        self.assertEqual(interaction["metadata"]["option_actions"], ["approve_once", "deny"])
        self.assertEqual(self.sess.status, "waiting_parent")
        # The parent's "1" approves: one Enter, and the pending prompt is cleared.
        response = await self.hub.deliver_parent_message(self.sess.session_id, "1")
        self.assertTrue(response["delivered"])
        self.assertIn("\r", self.written[2:])
        self.assertIsNone(self.sess.pending_interaction)
        self.assertFalse(self.sess.trust_auto_dismiss_exhausted)

    async def test_deny_sends_escape_back_to_the_command_center(self):
        self.sess.trust_auto_dismiss_exhausted = True
        interaction = await self.hub.poll_tui_screen_interactions_once(self.sess)
        self.assertEqual(interaction["metadata"]["codex_prompt"], "trust")
        result = await self.hub.respond_to_interaction(self.sess.session_id, decision="deny")
        self.assertTrue(result["delivered"])
        self.assertEqual(self.written, ["\x1b"])
        self.assertIsNone(self.sess.pending_interaction)

    async def test_claude_style_dialog_moves_off_no_before_enter(self):
        """The old guard stays: never press Enter while "No, exit" is the focused row."""
        self.screen = "Do you trust the files in this folder?\n❯ 1. No, exit\n  2. Yes, I trust this folder"

        def write(text):
            self.written.append(text)
            if text == "\x1b[B":
                self.screen = ("Do you trust the files in this folder?\n  1. No, exit\n"
                               "❯ 2. Yes, I trust this folder")
            elif text == "\r":
                self.screen = COMPOSER_SCREEN
            return True

        self.sess.write_input = write
        with self.patched_trust_timing(), patch("session_manager.TUI_TRUST_OPEN_GUARD_SEC", 0.01):
            task = self.hub._schedule(
                self.hub._watch_tui_startup_and_type(self.sess, None, 0.0, "\r")
            )
            try:
                await asyncio.wait_for(task, 5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.written, ["\x1b[B", "\r"])

    async def test_running_startup_watcher_keeps_the_page_off_the_parent(self):
        self.sess._startup_watch_active = True
        self.sess._startup_watch_until = time.time() + 60
        self.assertIsNone(await self.hub.poll_tui_screen_interactions_once(self.sess))
        self.assertIsNone(self.sess.pending_interaction)
        # A watcher that already gave up or exited leaves the page to the parent.
        self.sess._startup_watch_active = False
        interaction = await self.hub.poll_tui_screen_interactions_once(self.sess)
        self.assertEqual(interaction["metadata"]["codex_prompt"], "trust")


class CodexSubmitReasonTests(_HubCase):
    async def test_submit_distinguishes_prompt_dialog_and_missing_composer(self):
        result = await self.hub._submit_codex_text(self.sess, "hello", "\r")
        self.assertEqual(result["reason"], "blocked_by_dialog")
        self.assertIn(result["dialog"], ("Folder access", "Trust this folder?"))

        self.sess.pending_interaction = {"request_id": "int-trust", "kind": "approval_request"}
        result = await self.hub._submit_codex_text(self.sess, "hello", "\r")
        self.assertEqual(result["reason"], "waiting_for_interaction")
        self.assertEqual(result["interaction_id"], "int-trust")

        self.sess.pending_interaction = None
        self.screen = "plain output\nnothing else here"
        result = await self.hub._submit_codex_text(self.sess, "hello", "\r")
        self.assertEqual(result["reason"], "composer_unavailable")
        self.assertEqual(self.written, [])

    async def test_recovery_hints_name_the_real_problem(self):
        for reason, needle in (
            ("waiting_for_interaction", "Answer it first"),
            ("blocked_by_dialog", "dialog owns the worker's screen"),
        ):
            hint = self.hub._undelivered_recovery_hint(self.sess, {"ok": False, "reason": reason})
            self.assertIn(needle, hint)


class CodexRolloutOwnershipTests(_HubCase):
    create_default_session = False

    def rollout(self, name, payload):
        path = self.root / f"rollout-{name}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n", encoding="utf-8")
        return path

    def own_meta(self, **extra):
        return {"id": "0199-own", "cwd": str(self.root), "originator": "codex_cli_rs", **extra}

    async def test_guardian_and_subagent_rollouts_are_never_claimed(self):
        cases = {
            "guardian": {"thread_source": "guardian_review"},
            "parent": {"parent_thread_id": "thread-1"},
            "subagent": {"source": {"subagent": {"other": "reviewer"}}},
            "other-cwd": {"cwd": str(self.root / "somewhere-else")},
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                sess = self.session(f"codex-own-{name}")
                try:
                    path = self.rollout(name, self.own_meta(**extra))
                    self.assertTrue(self.hub._codex_rollout_ownership_problem(sess, path))
                    self.assertIsNone(await self.hub._claim_conversation(sess, [path]))
                finally:
                    # Every case owns the engine's FIFO alone; a leftover session without a
                    # transcript would make the next one wait its turn instead of testing the rule.
                    self.hub.sessions.pop(sess.session_id, None)

    async def test_own_rollout_in_its_cwd_is_claimed(self):
        sess = self.session("codex-own-normal")
        path = self.rollout("normal", self.own_meta())
        self.assertEqual(self.hub._codex_rollout_ownership_problem(sess, path), "")
        self.assertEqual(await self.hub._claim_conversation(sess, [path]), path)

    async def test_session_id_taken_by_another_session_is_rejected(self):
        sess = self.session("codex-taken")
        path = self.rollout("taken", self.own_meta(id="0199-taken"))
        owner = self.session("codex-owner")
        owner.transcript_path = self.root / "owner.jsonl"
        owner.conversation_ref = {"session_id": "0199-taken", "rollout_path": "owner.jsonl"}
        self.assertEqual(
            self.hub._codex_rollout_ownership_problem(sess, path), "session_id_taken"
        )
        self.assertIsNone(await self.hub._claim_conversation(sess, [path]))

    async def test_unmarked_rollout_after_the_spawn_window_is_not_claimed(self):
        sess = self.session("codex-late")
        sess.spawned_at = time.time() - 3600
        path = self.rollout("late", self.own_meta())
        self.assertIsNone(await self.hub._claim_conversation(sess, [path]))

    async def test_marked_rollout_is_claimed_even_after_the_spawn_window(self):
        sess = self.session("codex-marked")
        sess.spawned_at = time.time() - 3600
        # A marker is direct evidence: a different folder spelling must not hide it.
        path = self.rollout("marked", self.own_meta(id="0199-marked", cwd=str(self.root / "elsewhere")))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text",
                             "text": self.hub._task_with_session_marker(sess.session_id, "task")}],
            }}) + "\n")
        self.assertEqual(await self.hub._claim_conversation(sess, [path]), path)

    async def test_empty_and_garbage_rollouts_do_not_raise(self):
        empty = self.root / "rollout-empty.jsonl"
        empty.write_text("", encoding="utf-8")
        garbage = self.root / "rollout-garbage.jsonl"
        garbage.write_text("not json\n{broken\n", encoding="utf-8")
        self.assertEqual(self.hub._codex_session_meta(empty), {})
        self.assertEqual(self.hub._codex_session_meta(garbage), {})
        self.assertIsNone(self.hub._codex_session_id_from_rollout(empty))
