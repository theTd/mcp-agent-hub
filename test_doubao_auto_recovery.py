"""Doubao auto-recovery: the `auth_unavailable` shared-service drop, retried by the hub itself.

Covered here, each at its own layer:

- the detector: the CLI's own row and nothing else (never `◆` prose, never a `▍` echo, never a
  `detect_doubao_error` failure that would end the turn as an engine error);
- the budget: per-turn cap, rolling cap across turns, a successful turn resetting only the per-turn
  counter, and the jittered backoff;
- the hub-wide service gate: serialized spawns, the minimum gap, and the bounded wait a follower
  does instead of stampeding the shared service;
- the lifecycle: a recoverable death never publishes `crashed`, the row becomes `recovering` with
  its turn id and wait handle intact, a spent budget settles it as `crashed` with the recovery's own
  facts, and a non-recoverable death (completed turn, disabled switch, frozen stop) keeps the
  ordinary exit path.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from doubao_session_record import (
    DOUBAO_SERVICE_LINK_LOSS_CODE,
    _ERROR_CODE_LINE_RE,
    detect_doubao_error,
    detect_doubao_service_link_loss,
)
from doubao_recovery import (
    DOUBAO_LINK_LOSS_CODE,
    RECOVERY_CONTINUATION_PROMPT,
    RECOVERING_STATUS,
    DoubaoServiceGate,
    RecoveryBudget,
    attempt_activity,
    continuation_prompt,
    doubao_auto_recovery_backoff,
    doubao_auto_recovery_enabled,
    doubao_auto_recovery_max_attempts,
    doubao_auto_recovery_rolling_max,
    exhaustion_message,
    sleep_with_abort,
)
from fake_terminal_core import FakeTerminal
from session_manager import AgentSession, DoubaoRecoveryAborted, HubSessionManager

LINK_LOSS_ROW = "❯ auth_unavailable: 运行时与共享服务断开连接，任务已停止。请重试"
LINK_LOSS = {"kind": "network", "retryable": True, "code": DOUBAO_LINK_LOSS_CODE,
             "message": LINK_LOSS_ROW}
COMPOSER = "❯  Ask Doubao to do anything"
BUSY_SCREEN = f"DOUBAO CLI\n◆ 正在整理日志\n◇ Processing command\n\n{COMPOSER}\nAuto · high · E:\\work"
LOSS_SCREEN = f"DOUBAO CLI\n◆ 正在整理日志\n\n{LINK_LOSS_ROW}\n"
# The live 180-column crash (2026-11-24): Ink repainted the `model · effort · cwd` status row over
# the failure line, losing the `❯ ` frame and clipping even the leading `a` (`uth_unavailable:`).
WIDE_STATUS_ROW = (
    "  DeepSeek V4.1 Flash · High · E:\\misc\\mcp-agent-hub\\.worktrees\\feat-doubao-auto-recovery"
    " uth_unavailable: 运行时与共享服务断开连接，任务已停止。请重试                  Ctrl+P plan 7/8"
)
WIDE_SCREEN = (
    "────────────────────────────────────────────────────────────────────────────────\n"
    "❯  Ask Doubao to do anything\n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    f"{WIDE_STATUS_ROW}"
)


class DetectorTests(unittest.TestCase):
    """`auth_unavailable` is recovery material, not an engine error and not prose."""

    def test_the_cli_row_is_detected(self):
        info = detect_doubao_service_link_loss(LOSS_SCREEN)
        self.assertIsNotNone(info)
        self.assertEqual(info["code"], DOUBAO_SERVICE_LINK_LOSS_CODE)
        self.assertEqual(info["kind"], "network")
        self.assertTrue(info["retryable"])
        self.assertIn("auth_unavailable", info["message"])

    def test_prose_and_user_echo_never_match(self):
        self.assertIsNone(detect_doubao_service_link_loss(
            "◆ 出现 auth_unavailable: 运行时与共享服务断开连接 时请重试"))
        self.assertIsNone(detect_doubao_service_link_loss(f"▍ {LINK_LOSS_ROW}"))
        self.assertIsNone(detect_doubao_service_link_loss(COMPOSER))

    def test_an_answer_quoting_the_row_under_the_cursor_never_matches(self):
        # P1-2: an assistant `◆` block continues across `❯` rows (the CLI keeps the submitted text
        # on its composer glyph while it answers), so an answer quoting the row is still prose.
        self.assertIsNone(detect_doubao_service_link_loss(
            "◆ example\n❯ auth_unavailable: quoted by assistant"))

    def test_a_multiline_user_echo_never_matches(self):
        # P1-2: a `▍` user block continues across its own `❯` continuation lines too.
        self.assertIsNone(detect_doubao_service_link_loss(
            "▍ first line\n❯ auth_unavailable: quoted by user"))

    def test_the_real_rule_framed_frame_still_matches(self):
        # P1-2: the CLI's own layout is a rule row, the row, another rule row; that frame is real.
        info = detect_doubao_service_link_loss(
            "◆ answer\n────────\n❯ auth_unavailable: 运行时与共享服务断开连接，任务已停止。请重试\n────────")
        self.assertIsNotNone(info)
        self.assertEqual(info["code"], DOUBAO_SERVICE_LINK_LOSS_CODE)
        # And a standalone row at the top / after a blank row is the CLI's frame as well.
        self.assertIsNotNone(detect_doubao_service_link_loss(LINK_LOSS_ROW))
        self.assertIsNotNone(detect_doubao_service_link_loss(f"DOUBAO CLI\n\n{LINK_LOSS_ROW}"))

    def test_a_wide_terminal_status_row_collision_is_detected(self):
        # Live crash: on 180 columns the status bar was repainted over the row, so the frame and the
        # leading `a` are gone. The banner and the clipped code are still the CLI's own message.
        for screen in (WIDE_STATUS_ROW, WIDE_SCREEN):
            info = detect_doubao_service_link_loss(screen)
            self.assertIsNotNone(info, screen)
            self.assertEqual(info["code"], DOUBAO_SERVICE_LINK_LOSS_CODE)
            self.assertEqual(info["kind"], "network")
            self.assertTrue(info["retryable"])
            self.assertIn("运行时与共享服务断开连接", info["message"])

    def test_the_same_collision_inside_a_block_is_still_prose(self):
        # P1-2 holds for the widened forms too: `◆` prose and a `▍` echo never match.
        self.assertIsNone(detect_doubao_service_link_loss(f"◆ 分析中\n{WIDE_STATUS_ROW}"))
        self.assertIsNone(detect_doubao_service_link_loss(f"▍ {WIDE_STATUS_ROW}"))

    def test_a_clipped_or_bare_row_outside_the_blocks_is_detected(self):
        # Ink may clip the left edge only (`uth_unavailable:`) or leave no cursor glyph at all;
        # inside the candidate rows both are still the CLI talking.
        for screen in (
            "uth_unavailable: 运行时与共享服务断开连接，任务已停止。请重试",
            "auth_unavailable: 运行时与共享服务断开连接，任务已停止。请重试",
            "   DeepSeek V4.1 Flash · High · E:\\work uth_unavailable: 请重试   Ctrl+P",
        ):
            self.assertIsNotNone(detect_doubao_service_link_loss(screen), screen)

    def test_detect_doubao_error_never_claims_the_link_loss(self):
        # An engine_error would outrank the recovered answer forever (FINAL_SOURCE_PRIORITY 7).
        self.assertIsNone(detect_doubao_error(LOSS_SCREEN))
        self.assertIsNone(detect_doubao_error(f"Error: auth_unavailable: 运行时与共享服务断开连接"))
        self.assertIsNotNone(detect_doubao_error("❯ network_offline: 网络已断开"))

    def test_the_prompt_glyph_is_stripped_before_a_known_code(self):
        # A row the Ink cursor leads is still the CLI's own failure, not a composer.
        self.assertTrue(_ERROR_CODE_LINE_RE.match("❯ network_offline: 网络已断开"))
        self.assertTrue(_ERROR_CODE_LINE_RE.match("❯ internal_error: 内部错误"))
        self.assertFalse(_ERROR_CODE_LINE_RE.match(COMPOSER))
        self.assertFalse(_ERROR_CODE_LINE_RE.match("✗ 没有失败"))

    def test_every_pair_ends_up_distinct(self):
        # Two sessions that crashed together must not read one another's row as their own.
        self.assertNotEqual(DOUBAO_LINK_LOSS_CODE, "auth_failed")


class BudgetTests(unittest.TestCase):
    """The caps that stop one worker from respawning a dead service forever."""

    def budget(self, **overrides) -> RecoveryBudget:
        options = {
            "max_attempts": 3,
            "rolling_max": 6,
            "rolling_window_sec": 1800.0,
            "delays": (1.5, 5.0, 15.0),
            "jitter_ratio": 0.2,
            "clock": lambda: 0.0,
            "rng": lambda: 0.5,
        }
        options.update(overrides)
        return RecoveryBudget(**options)

    def test_a_turn_spends_at_most_its_attempts(self):
        budget = self.budget()
        for expected in (1, 2, 3):
            self.assertEqual(budget.begin_attempt(), expected)
        self.assertTrue(budget.exhausted_reason())
        self.assertFalse(budget.can_attempt())
        with self.assertRaises(RuntimeError):
            budget.begin_attempt()

    def test_a_completed_turn_resets_the_counter_but_not_the_window(self):
        budget = self.budget()
        budget.begin_attempt()
        budget.begin_attempt()
        budget.note_turn_end(completion_reported=True)
        self.assertEqual(budget.attempts, 0)
        self.assertEqual(budget.rolling_attempts(), 2)
        # A turn that merely ended (interrupted, crashed) keeps its spent attempts.
        budget.begin_attempt()
        budget.note_turn_end(completion_reported=False)
        self.assertEqual(budget.attempts, 1)

    def test_the_rolling_window_forbids_a_sixth_attempt(self):
        now = 100.0
        budget = self.budget(rolling_max=6, clock=lambda: now, max_attempts=100)
        for _ in range(6):
            budget.begin_attempt()
            budget.note_turn_end(completion_reported=True)
        self.assertFalse(budget.can_attempt())
        self.assertIn("滚动上限", budget.exhausted_reason())
        # Once the window has passed, the counts are gone.
        now = 100.0 + 1801.0
        self.assertEqual(budget.rolling_attempts(), 0)
        self.assertTrue(budget.can_attempt())

    def test_the_backoff_is_jittered_within_the_documented_band(self):
        class ScriptedRandom:
            def __init__(self, value):
                self.value = value

            def __call__(self):
                return self.value

        low = RecoveryBudget(delays=(1.5, 5.0, 15.0), jitter_ratio=0.2,
                             rng=ScriptedRandom(0.0)).delay_for(1)
        high = RecoveryBudget(delays=(1.5, 5.0, 15.0), jitter_ratio=0.2,
                             rng=ScriptedRandom(1.0)).delay_for(1)
        self.assertAlmostEqual(low, 1.5 * 0.8)
        self.assertAlmostEqual(high, 1.5 * 1.2)
        self.assertAlmostEqual(RecoveryBudget(delays=(1.5,), rng=ScriptedRandom(0.5)).delay_for(3), 1.5)

    def test_a_zero_schedule_runs_without_any_wait(self):
        self.assertEqual(self.budget(delays=(0.0,)).delay_for(1), 0.0)

    def test_settings_are_read_from_the_environment_and_fall_back(self):
        self.assertTrue(doubao_auto_recovery_enabled({}))
        self.assertFalse(doubao_auto_recovery_enabled({"HUB_DOUBAO_AUTO_RECOVERY": "off"}))
        self.assertFalse(doubao_auto_recovery_enabled({"HUB_DOUBAO_AUTO_RECOVERY": "0"}))
        self.assertEqual(doubao_auto_recovery_max_attempts({}), 3)
        self.assertEqual(doubao_auto_recovery_max_attempts(
            {"HUB_DOUBAO_AUTO_RECOVERY_MAX_ATTEMPTS": "7"}), 7)
        self.assertEqual(doubao_auto_recovery_max_attempts(
            {"HUB_DOUBAO_AUTO_RECOVERY_MAX_ATTEMPTS": "lots"}), 3)
        self.assertEqual(doubao_auto_recovery_rolling_max({}), 6)
        self.assertEqual(doubao_auto_recovery_backoff({}), (1.5, 5.0, 15.0))
        self.assertEqual(doubao_auto_recovery_backoff({"HUB_DOUBAO_AUTO_RECOVERY_BACKOFF_SEC": "0,0"}),
                         (0.0, 0.0))
        self.assertEqual(doubao_auto_recovery_backoff({"HUB_DOUBAO_AUTO_RECOVERY_BACKOFF_SEC": "-1"}),
                         (1.5, 5.0, 15.0))
        self.assertEqual(RecoveryBudget.from_env({"HUB_DOUBAO_AUTO_RECOVERY_MAX_ATTEMPTS": "2"},
                                                delays=(0.0,)).max_attempts, 2)

    def test_the_budget_can_be_handed_to_a_successor(self):
        first = self.budget()
        first.begin_attempt()
        first.begin_attempt()
        second = self.budget()
        second.restore(first.snapshot())
        self.assertEqual(second.attempts, 2)
        self.assertEqual(second.rolling_attempts(), 2)

    def test_the_counters_carry_over_as_a_snapshot_only(self):
        budget = self.budget()
        budget.begin_attempt()
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["attempts"], 1)
        self.assertEqual(len(snapshot["rolling"]), 1)
        self.assertNotIn("clock", snapshot)


class MessageTests(unittest.TestCase):
    """Every parent-visible sentence lives here, so its facts are asserted here."""

    def test_the_continuation_prompt_never_asks_for_a_redo(self):
        self.assertEqual(continuation_prompt(), RECOVERY_CONTINUATION_PROMPT)
        self.assertIn("从断开处继续完成原任务", continuation_prompt())
        self.assertIn("已完成的部分不要重做", continuation_prompt())

    def test_the_activity_line_names_the_attempt(self):
        self.assertEqual(attempt_activity(2, 3), "豆包共享服务断开，正在自动恢复（第 2/3 次）...")

    def test_the_exhausted_text_names_every_fact_and_the_hint(self):
        text = exhaustion_message(
            "doubao-1", exit_code=3, attempts=3, max_attempts=3,
            error_detail="cannot start the CLI", resumable=True,
        )
        self.assertIn("auth_unavailable", text)
        self.assertIn("exit_code=3", text)
        self.assertIn("已尝试自动恢复 3 次", text)
        self.assertIn("单轮上限 3 次", text)
        self.assertIn("cannot start the CLI", text)
        self.assertIn("get_subagent_final_response('doubao-1')", text)
        self.assertIn("resume_subagent('doubao-1')", text)

    def test_a_non_resumable_session_gets_no_resume_hint(self):
        text = exhaustion_message("doubao-2", exit_code=None, attempts=1, max_attempts=1,
                                  resumable=False)
        self.assertIn("exit_code=None", text)
        self.assertNotIn("resume_subagent", text)


class ServiceGateTests(unittest.IsolatedAsyncioTestCase):
    """N crashes, one shared service: the gate serializes them and bounds the wait."""

    def gate(self, **overrides) -> DoubaoServiceGate:
        slept: list = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        options = {"min_spawn_gap_sec": 2.0, "service_ready_wait_sec": 12.0, "sleep": fake_sleep}
        options.update(overrides)
        gate = DoubaoServiceGate(**options)
        gate.slept = slept
        return gate

    async def test_spawns_are_serial_and_keep_the_minimum_gap(self):
        gate = self.gate(min_spawn_gap_sec=2.0)
        order: list = []

        async def spawn(name):
            await gate.lock.acquire()
            try:
                await gate.wait_gap()
                gate.note_spawn_started()
                order.append((name, "start"))
                await asyncio.sleep(0.05)
                order.append((name, "end"))
            finally:
                gate.release_spawn_slot()

        await asyncio.gather(spawn("a"), spawn("b"), spawn("c"))
        starts = [name for name, phase in order if phase == "start"]
        self.assertEqual(len(starts), 3)
        # No spawn overlapped another: every start is followed by its own end.
        self.assertEqual([phase for _, phase in order],
                         ["start", "end", "start", "end", "start", "end"])
        # The first spawn owed no gap; the other two each waited the 2 s the lead started.
        self.assertEqual(len(gate.slept), 2)
        self.assertTrue(all(value >= 1.9 for value in gate.slept))

    async def test_a_follower_waits_for_the_lead_instead_of_stampeding(self):
        gate = self.gate(service_ready_wait_sec=0.5)
        lead = asyncio.create_task(gate.acquire_spawn_slot())
        await asyncio.sleep(0.01)
        self.assertTrue(gate.lead_in_flight)
        follower = asyncio.create_task(gate.acquire_spawn_slot())
        await asyncio.sleep(0.05)
        self.assertFalse(follower.done())
        # The lead's recovered screen shows the composer: the follower may go.
        gate.note_service_ready()
        await asyncio.wait_for(lead, 1.0)
        gate.finish_spawn_slot()
        await asyncio.wait_for(follower, 1.0)
        gate.finish_spawn_slot()
        self.assertEqual(gate.spawn_calls, 2)

    async def test_a_wait_that_runs_out_proceeds_anyway(self):
        gate = self.gate(service_ready_wait_sec=0.05)
        gate.note_spawn_started()
        started = time.monotonic()
        ready = await gate.wait_service_ready()
        self.assertFalse(ready)
        self.assertLess(time.monotonic() - started, 1.0)

    async def test_a_failed_lead_releases_the_waiters(self):
        gate = self.gate(service_ready_wait_sec=0.1)
        async with gate.lock:
            gate.note_spawn_started()
            waiter = asyncio.create_task(gate.wait_service_ready())
            await asyncio.sleep(0.02)
            gate.note_spawn_failed()
            self.assertTrue(await asyncio.wait_for(waiter, 1.0))
        self.assertFalse(gate.lead_in_flight)

    async def test_no_lead_in_flight_means_no_wait(self):
        gate = self.gate(service_ready_wait_sec=30.0)
        self.assertTrue(await gate.wait_service_ready())
        self.assertEqual(gate.gap_remaining(), 0.0)

    async def test_a_sibling_that_never_led_cannot_release_the_active_lead(self):
        # P1-5: lead ownership is a token. Session B failing before it ever acquired the gate, or
        # being cancelled there, passes None (or a stale token) and must settle nothing: Session A
        # is still the lead and the waiters must stay held until A settles or reaches its composer.
        gate = self.gate(service_ready_wait_sec=0.2)
        slot = await gate.acquire_spawn_slot()      # Session A becomes the lead
        token = slot["lead_token"]
        self.assertTrue(gate.lead_in_flight)
        self.assertFalse(gate.service_ready.is_set())
        gate.note_attempt_settled(None)             # Session B, cancelled before the gate
        gate.note_attempt_settled(int(token) + 1)   # Session B with a stale token
        self.assertTrue(gate.lead_in_flight)
        self.assertFalse(gate.service_ready.is_set())
        # The follower is still held: it runs out of its bounded wait instead of going.
        self.assertFalse(await gate.wait_service_ready(timeout=0.05))
        gate.finish_spawn_slot()
        # Only A's own token releases the lead it took.
        gate.note_attempt_settled(token)
        self.assertFalse(gate.lead_in_flight)
        self.assertTrue(gate.service_ready.is_set())
        self.assertFalse(gate.note_attempt_settled(token))


class SleepTests(unittest.IsolatedAsyncioTestCase):
    async def test_sleep_stops_at_an_abort(self):
        calls: list = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            await asyncio.sleep(seconds)

        aborted = {"now": False}
        task = asyncio.create_task(
            sleep_with_abort(5.0, sleep=fake_sleep, abort=lambda: aborted["now"], poll_sec=0.1))
        await asyncio.sleep(0.05)
        aborted["now"] = True
        self.assertFalse(await asyncio.wait_for(task, 1.0))
        self.assertLess(sum(calls), 5.0)

    async def test_a_zero_delay_never_sleeps(self):
        calls: list = []

        async def fake_sleep(seconds):
            calls.append(seconds)

        self.assertTrue(await sleep_with_abort(0.0, sleep=fake_sleep))
        self.assertEqual(calls, [])


class RecoveryHubCase(unittest.IsolatedAsyncioTestCase):
    """A real HubSessionManager over the fake terminal core, with a doubao session fixture."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-doubao-recovery-")
        self.root = Path(self.temp.name)
        (self.root / "profiles.json").write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(self.root / "profiles.json",
                                         base_dir=Path(__file__).parent, data_dir=self.root)
        self.fake = FakeTerminal()
        self.hub.core = self.fake
        await self.hub.start()
        # No test may scan the machine's real Doubao store; the one that cares patches its own.
        self.finder = patch("session_manager.find_doubao_session_by_marker", return_value=None)
        self.finder.start()
        # A zero schedule and no gap: the tests exercise the policy, not the wall clock.
        self.env = patch.dict(os.environ, {
            "HUB_DOUBAO_AUTO_RECOVERY": "1",
            "HUB_DOUBAO_AUTO_RECOVERY_MAX_ATTEMPTS": "3",
            "HUB_DOUBAO_AUTO_RECOVERY_ROLLING_MAX": "6",
            "HUB_DOUBAO_AUTO_RECOVERY_BACKOFF_SEC": "0,0,0",
            "HUB_DOUBAO_AUTO_RECOVERY_SPAWN_GAP_SEC": "0",
            "HUB_DOUBAO_AUTO_RECOVERY_SERVICE_READY_WAIT_SEC": "0.2",
        })
        self.env.start()

    async def asyncTearDown(self):
        self.hub._recovery_frozen = True
        for task in list(self.hub._recovery_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.hub._recovery_tasks.values()), return_exceptions=True)
        self.finder.stop()
        self.env.stop()
        await self.hub.archive_all_sessions_on_shutdown(planned=False)
        self.temp.cleanup()

    def doubao_session(self, sid: str = "doubao-auto", status: str = "running",
                       prompt: str = "把日志整理成 CSV") -> AgentSession:
        sess = AgentSession(sid, "doubao", "doubao", "tui", "Doubao recovery", str(self.root),
                            "unused", [], core=self.fake, on_exit_cb=self.hub._exit_callback)
        sess.engine = "doubao"
        sess.status = status
        sess.wait_handle = f"wait-{sid}"
        sess.last_task = prompt
        sess.last_injection = {"mode": "startup_task", "ok": True, "reason": "submitted"}
        sess.profile_snapshot = {
            "profile_id": "doubao", "profile_name": "doubao", "engine": "doubao", "kind": "tui",
            "command": "cmd.exe", "args": [], "cwd": str(self.root), "task_mode": "pty_input",
            "startup_delay_sec": 0.0, "submit_key": "\r",
            "initial_prompt": f"{prompt}\n\n[Hub Session: session_id={sid}]",
            "env": {}, "mcp_servers": {}, "bypass_approvals": False, "approval_mode": "ask",
            "brief_path": None, "leaf_worker": True,
        }
        self.fake.spawn(sid, ["fixture"])
        sess._loop = asyncio.get_running_loop()
        self.hub.sessions[sid] = sess
        self.hub._on_core_tick()
        return sess

    def events_of(self, sid: str, kind: str | None = None) -> list:
        return [event for event in self.hub.worker_events
                if event.get("session_id") == sid and (kind is None or event.get("type") == kind)]

    async def until(self, condition, timeout: float = 10.0):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.05)

    def block_spawn(self):
        """Hold the first fresh spawn open; every attempt fails once released.

        Without it the whole recovery would run in a few milliseconds on the zero schedule and
        the test could never observe the state in the middle.
        """
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked(session_id, record, prompt):
            started.set()
            await release.wait()
            raise RuntimeError("共享服务仍不可用")

        patcher = patch.object(self.hub, "_respawn_doubao_session", blocked)
        patcher.start()
        self.addCleanup(patcher.stop)
        resumable = patch.object(self.hub, "compute_resumable",
                                 return_value=(False, "未记录到豆包会话 ID"))
        resumable.start()
        self.addCleanup(resumable.stop)
        return started, release

    async def crash(self, sess: AgentSession, *, exit_code: int = 3) -> None:
        """The link loss and the death the CLI reports right after it."""
        sess._doubao_link_loss = dict(LINK_LOSS)
        sess._doubao_link_loss_at = time.time()
        self.fake.set_exit(sess.session_id, exit_code, crashed=True)
        self.fake.die(sess.session_id)
        self.hub._on_core_tick()


class RecoveryLifecycleTests(RecoveryHubCase):
    async def test_a_link_loss_starts_a_recovery_without_a_crash_event(self):
        sess = self.doubao_session()
        turn_id = sess.turn_id
        started, release = self.block_spawn()
        await self.crash(sess)
        await asyncio.wait_for(started.wait(), 10.0)
        self.assertEqual(sess.status, RECOVERING_STATUS)
        self.assertEqual([event["type"] for event in self.events_of(sess.session_id)],
                         ["auto_recovery"])
        event = self.events_of(sess.session_id, "auto_recovery")[0]
        self.assertEqual(event["data"]["attempt"], 1)
        self.assertEqual(event["data"]["code"], DOUBAO_LINK_LOSS_CODE)
        self.assertEqual(event["data"]["exit_code"], 3)
        # The turn the parent awaits keeps its identity: same id, same handle.
        self.assertEqual(sess.turn_id, turn_id)
        self.assertEqual(sess.wait_handle, f"wait-{sess.session_id}")
        record = await self.hub.db.get_session_record(sess.session_id)
        self.assertEqual(record.get("status"), RECOVERING_STATUS)
        self.assertIsNone(record.get("archived_at"))
        # Released, the three attempts fail and the turn still ends as one crash. (The status is
        # written before the final row/persist, so wait for the event itself, not just the status.)
        release.set()
        await self.until(lambda: len(self.events_of(sess.session_id, "crashed")) == 1)
        self.assertEqual(sess.status, "crashed")
        self.assertEqual(len(self.events_of(sess.session_id, "crashed")), 1)

    async def test_the_recovery_event_is_never_a_wake(self):
        from wait_inbox import WAKE_EVENT_TYPES
        self.assertNotIn("auto_recovery", WAKE_EVENT_TYPES)

    async def test_an_exhausted_budget_settles_the_turn_as_crashed_with_facts(self):
        sess = self.doubao_session()
        started, release = self.block_spawn()
        with patch.dict(os.environ, {"HUB_DOUBAO_AUTO_RECOVERY_MAX_ATTEMPTS": "1"}):
            self.hub._doubao_recovery.pop(sess.session_id, None)
            await self.crash(sess)
            await asyncio.wait_for(started.wait(), 10.0)
            self.assertEqual(sess.status, RECOVERING_STATUS)
            self.assertEqual(self.events_of(sess.session_id, "crashed"), [])
            release.set()
            await self.until(lambda: sess.status == "crashed")
        crashed = self.events_of(sess.session_id, "crashed")
        self.assertEqual(len(crashed), 1)
        text = crashed[0]["content"]
        self.assertIn("auth_unavailable", text)
        self.assertIn("exit_code=3", text)
        self.assertIn("已尝试自动恢复 1 次", text)
        self.assertIn("get_subagent_final_response", text)
        self.assertEqual(crashed[0]["data"]["auto_recovery_attempts"], 1)
        self.assertTrue(crashed[0]["data"]["auto_recovery_exhausted"])
        # Every recovery progress row stays behind it; none of them claimed the turn.
        self.assertEqual([event["type"] for event in self.events_of(sess.session_id)],
                         ["auto_recovery", "crashed"])

    async def test_a_clean_turn_gives_the_next_turn_its_attempts_back(self):
        sess = self.doubao_session()
        budget = self.hub._doubao_budget(sess.session_id)
        budget.begin_attempt()
        old_turn = sess.turn_id
        sess.completion_reported = True
        with patch.object(self.hub.wait_inbox, "rearm_session_turn"):
            self.hub.begin_new_turn(sess)
        self.assertEqual(budget.attempts, 0)
        # The window is what caps a service that keeps flapping: the attempt stays on record.
        self.assertEqual(budget.rolling_attempts(), 1)
        self.assertNotEqual(sess.turn_id, old_turn)

    async def test_a_completed_turn_is_never_recovered(self):
        sess = self.doubao_session(status="completed")
        sess.completion_reported = True
        sess.final_response = "已经交付"
        await self.crash(sess)
        await asyncio.sleep(0.2)
        self.assertNotIn(sess.session_id, self.hub._recovery_tasks)
        self.assertEqual(sess.status, "crashed")
        self.assertEqual(len(self.events_of(sess.session_id, "crashed")), 1)

    async def test_the_switch_turns_recovery_off(self):
        sess = self.doubao_session()
        with patch.dict(os.environ, {"HUB_DOUBAO_AUTO_RECOVERY": "0"}):
            await self.crash(sess)
            await asyncio.sleep(0.2)
        self.assertNotIn(sess.session_id, self.hub._recovery_tasks)
        self.assertEqual(sess.status, "crashed")
        self.assertNotIn(RECOVERING_STATUS, [event.get("status") for event in
                                             self.events_of(sess.session_id)])

    async def test_a_frozen_hub_never_spawns(self):
        sess = self.doubao_session()
        self.hub._recovery_frozen = True
        await self.crash(sess)
        await asyncio.sleep(0.2)
        self.assertNotIn(sess.session_id, self.hub._recovery_tasks)
        self.assertEqual(sess.status, "crashed")

    async def test_a_terminate_during_the_backoff_emits_its_one_event(self):
        sess = self.doubao_session()
        with patch.dict(os.environ, {"HUB_DOUBAO_AUTO_RECOVERY_BACKOFF_SEC": "0.4"}):
            self.hub._doubao_recovery.pop(sess.session_id, None)
            await self.crash(sess)
            await self.until(lambda: sess.session_id in self.hub._recovery_tasks)
            await asyncio.sleep(0.05)
            result = await self.hub.close_session(sess.session_id, source="mcp")
        self.assertTrue(result["found"])
        self.assertEqual(sess.status, "terminated")
        terminated = self.events_of(sess.session_id, "terminated")
        self.assertEqual(len(terminated), 1)
        self.assertIn("自动恢复", terminated[0]["content"])
        self.assertNotIn(sess.session_id, self.hub._recovery_tasks)

    async def test_a_late_death_record_never_queues_a_second_recovery(self):
        sess = self.doubao_session()
        blocked = asyncio.Event()

        async def stuck(session_id, record, prompt):
            await blocked.wait()
            raise RuntimeError("never reached")

        with patch.object(self.hub, "compute_resumable", return_value=(False, "no conversation")), \
                patch.object(self.hub, "_respawn_doubao_session", stuck):
            await self.crash(sess)
            await self.until(lambda: sess.session_id in self.hub._recovery_tasks)
            # The same dead object is handed to the exit handler again, as a second drain would.
            await self.hub._on_session_exit(sess, 3)
            self.assertEqual(len([event for event in self.events_of(sess.session_id)
                                  if event["type"] == "auto_recovery"]), 1)
            self.assertEqual(sess.status, RECOVERING_STATUS)
            blocked.set()


class RecoverySpawnTests(RecoveryHubCase):
    """The two spawn shapes: resume the conversation, or replay the exact first text."""

    async def test_a_mid_turn_turn_resumes_the_same_conversation_in_the_same_turn(self):
        sess = self.doubao_session()
        turn_id, handle = sess.turn_id, sess.wait_handle
        sess.conversation_ref = {"session_id": "conv-77", "cwd": str(self.root)}
        sess.last_injection = {"mode": "startup_task", "ok": True, "reason": "submitted"}
        captured = []

        async def fake_startup(session, task_to_inject, startup_delay=2.5, submit_key="\r"):
            captured.append((session, task_to_inject))
            return None

        def fake_build(record):
            return {"command": "cmd.exe", "args": [], "env": {}, "cwd": str(self.root),
                    "message_mode": "pty_input", "attach_offset": 0, "bypass_approvals": False,
                    "approval_mode": "ask", "notes": []}

        with patch.object(self.hub, "compute_resumable", return_value=(True, "")), \
                patch.object(self.hub, "build_resume_launch", side_effect=fake_build), \
                patch.object(self.hub, "engine_enabled", return_value=True), \
                patch.object(self.hub, "_auto_handle_tui_startup_and_trust", fake_startup):
            await self.crash(sess)
            await self.until(lambda: self.hub.sessions.get(sess.session_id) is not sess)
            await self.until(lambda: captured)
        new = self.hub.sessions[sess.session_id]
        self.assertIsNot(new, sess)
        self.assertEqual(new.turn_id, turn_id)
        self.assertEqual(new.wait_handle, handle)
        self.assertEqual(new.status, "running")
        self.assertEqual(len(captured), 1)
        typed = captured[0][1]
        self.assertIn(RECOVERY_CONTINUATION_PROMPT, typed)
        self.assertEqual(typed.count(f"[Hub Session: session_id={sess.session_id}]"), 1)
        # A second crash before its Enter replays exactly this text, never the archived task.
        self.assertEqual(new.profile_snapshot.get("initial_prompt"), typed)
        self.assertEqual(self.events_of(sess.session_id, "crashed"), [])

    async def test_an_unsubmitted_first_turn_replays_the_exact_prompt(self):
        sess = self.doubao_session()  # last_injection says submitted
        sess.last_injection = None
        exact = sess.profile_snapshot["initial_prompt"]
        captured = []

        async def fake_respawn(session_id, record, prompt):
            captured.append(prompt)
            successor = AgentSession(session_id, "doubao", "doubao", "tui", "Doubao recovery",
                                     str(self.root), "unused", [], core=self.fake)
            successor.engine = "doubao"
            successor.status = "running"
            successor.profile_snapshot = dict(record.get("profile_snapshot") or {})
            successor.turn_id = str(record.get("turn_id") or successor.turn_id)
            successor.wait_handle = record.get("wait_handle")
            self.hub.sessions[session_id] = successor
            return successor

        with patch.object(self.hub, "compute_resumable", return_value=(False, "未记录到豆包会话 ID")), \
                patch.object(self.hub, "_respawn_doubao_session", fake_respawn):
            await self.crash(sess)
            await self.until(lambda: self.hub.sessions.get(sess.session_id) is not sess)
        self.assertEqual(captured, [exact])
        self.assertEqual(captured[0].count(f"[Hub Session: session_id={sess.session_id}]"), 1)

    async def test_a_failed_spawn_spends_its_attempt_and_the_next_one_recovers(self):
        sess = self.doubao_session()
        sess.last_injection = None
        calls: list = []

        async def flaky(session_id, record, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                raise RuntimeError("the CLI refused to start")
            successor = AgentSession(session_id, "doubao", "doubao", "tui", "Doubao recovery",
                                     str(self.root), "unused", [], core=self.fake)
            successor.engine = "doubao"
            successor.status = "running"
            successor.profile_snapshot = dict(record.get("profile_snapshot") or {})
            successor.turn_id = str(record.get("turn_id") or successor.turn_id)
            self.hub.sessions[session_id] = successor
            return successor

        with patch.dict(os.environ, {"HUB_DOUBAO_AUTO_RECOVERY_MAX_ATTEMPTS": "2"}), \
                patch.object(self.hub, "compute_resumable", return_value=(False, "no conversation")), \
                patch.object(self.hub, "_respawn_doubao_session", flaky):
            self.hub._doubao_recovery.pop(sess.session_id, None)
            await self.crash(sess)
            await self.until(lambda: len(calls) == 2)
            await self.until(lambda: self.hub.sessions.get(sess.session_id) is not sess)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.hub._doubao_budget(sess.session_id).attempts, 2)
        self.assertEqual(self.events_of(sess.session_id, "crashed"), [])
        self.assertEqual(self.hub.sessions[sess.session_id].status, "running")

    async def test_the_marker_scan_finds_the_conversation_when_the_ref_is_empty(self):
        sess = self.doubao_session()
        sess.conversation_ref = {}
        used: list = []

        async def fake_resume(session_id, record, text, **kwargs):
            used.append((text, kwargs))
            successor = AgentSession(session_id, "doubao", "doubao", "tui", "Doubao recovery",
                                     str(self.root), "unused", [], core=self.fake)
            successor.engine = "doubao"
            successor.status = "running"
            successor.profile_snapshot = dict(record.get("profile_snapshot") or {})
            successor.turn_id = str(record.get("turn_id") or successor.turn_id)
            self.hub.sessions[session_id] = successor
            return {"session": successor}

        def fake_compute(record):
            ref = record.get("conversation_ref") or {}
            return (True, "") if ref.get("session_id") else (False, "未记录到豆包会话 ID")

        with patch.object(self.hub, "compute_resumable", side_effect=fake_compute), \
                patch("session_manager.find_doubao_session_by_marker",
                      return_value=("conv-scan", [str(self.root / "trajectory.jsonl")])), \
                patch.object(self.hub, "_resume_process_session", fake_resume):
            await self.crash(sess)
            await self.until(lambda: used)
            await self.until(lambda: self.hub.sessions.get(sess.session_id) is not sess)
        self.assertEqual(len(used), 1)
        self.assertTrue(used[0][1].get("same_turn"))
        self.assertEqual(used[0][1].get("source"), "auto_recovery")
        self.assertIn(RECOVERY_CONTINUATION_PROMPT, used[0][0])

    async def test_a_truncated_brief_is_never_replayed_blindly(self):
        sess = self.doubao_session()
        record = {"profile_snapshot": {"brief_path": str(self.root / "brief.md"),
                                       "initial_prompt": None},
                  "last_task": "整理日志…"}
        with self.assertRaises(ValueError):
            self.hub._doubao_initial_prompt(sess.session_id, record)
        record["last_task"] = "整理日志"
        self.assertEqual(
            self.hub._doubao_initial_prompt(sess.session_id, record),
            f"整理日志\n\n[Hub Session: session_id={sess.session_id}]")

    async def test_the_prompt_is_raw_when_the_snapshot_has_it(self):
        sess = self.doubao_session()
        record = {"profile_snapshot": dict(sess.profile_snapshot)}
        self.assertEqual(self.hub._doubao_initial_prompt(sess.session_id, record),
                         sess.profile_snapshot["initial_prompt"])


class WatchLinkLossTests(RecoveryHubCase):
    async def test_the_watcher_records_the_row_and_ends_a_client_that_keeps_drawing_it(self):
        sess = self.doubao_session()
        self.fake.set_screen(sess.session_id, BUSY_SCREEN)
        with patch.dict(os.environ, {"HUB_DOUBAO_AUTO_RECOVERY": "0"}), \
                patch("session_manager.DOUBAO_LINK_LOSS_KILL_AFTER_SEC", 0.01):
            task = self.hub._schedule(self.hub._watch_doubao_session(
                sess, self.root / "doubao_cli"))
            self.fake.set_screen(sess.session_id, LOSS_SCREEN)
            await self.until(lambda: sess._doubao_link_loss is not None, timeout=6.0)
            self.assertEqual(sess._doubao_link_loss["code"], DOUBAO_LINK_LOSS_CODE)
            # Nothing was published for the loss itself: it is evidence, not a turn end.
            self.assertEqual([event["type"] for event in self.events_of(sess.session_id)
                              if event.get("category") == "lifecycle"], [])
            await self.until(lambda: self.fake.kill_calls, timeout=6.0)
            self.assertEqual(self.fake.kill_calls[0][0], sess.session_id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_a_composer_screen_proves_the_shared_service_ready(self):
        self.hub.doubao_service_gate.note_spawn_started()
        await self.hub._observe_doubao_service_state(None, COMPOSER)
        self.assertTrue(self.hub.doubao_service_gate.service_ready.is_set())


class SubmissionStateTests(RecoveryHubCase):
    """P0-1: submission is tracked per TURN, so turn 2+ never replays turn 1's initial prompt."""

    async def test_a_follow_up_turn_continues_instead_of_replaying_turn_one(self):
        sess = self.doubao_session()
        sess.conversation_ref = {"session_id": "conv-77", "cwd": str(self.root)}
        # Turn 2: the parent's follow-up started it (begin_new_turn minted a new id) and its Enter
        # was proved, so last_injection's mode is "parent_message" now -- never "startup_task".
        self.hub.begin_new_turn(sess, status="running")
        sess.last_injection = {"mode": "parent_message", "ok": True,
                               "reason": "transcript_user_message", "at": time.time()}
        turn_two = sess.turn_id
        self.assertTrue(self.hub._doubao_turn_was_submitted(sess))
        used: list = []

        async def fake_resume(session_id, record, text, **kwargs):
            used.append(text)
            successor = AgentSession(session_id, "doubao", "doubao", "tui", "Doubao recovery",
                                     str(self.root), "unused", [], core=self.fake)
            successor.engine = "doubao"
            successor.status = "running"
            successor.profile_snapshot = dict(record.get("profile_snapshot") or {})
            successor.turn_id = str(record.get("turn_id") or successor.turn_id)
            self.hub.sessions[session_id] = successor
            return {"session": successor}

        with patch.object(self.hub, "compute_resumable", return_value=(True, "")), \
                patch.object(self.hub, "_resume_process_session", fake_resume):
            await self.crash(sess)
            await self.until(lambda: used)
            await self.until(lambda: self.hub.sessions.get(sess.session_id) is not sess)
        text = used[0]
        self.assertIn(RECOVERY_CONTINUATION_PROMPT, text)
        # Turn 1's task is NEVER typed again into the conversation that already has it.
        self.assertNotIn("把日志整理成 CSV", text)
        self.assertEqual(sess.turn_id, turn_two)
        self.assertEqual(self.events_of(sess.session_id, "crashed"), [])

    async def test_the_per_turn_marker_alone_proves_a_submission(self):
        sess = self.doubao_session()
        sess.last_injection = None
        self.assertFalse(self.hub._doubao_turn_was_submitted(sess))
        sess._turn_submitted_for_turn_id = sess.turn_id
        self.assertTrue(self.hub._doubao_turn_was_submitted(sess))
        # A new turn has submitted nothing yet: the previous turn's marker must not claim it.
        self.hub.begin_new_turn(sess, status="running")
        self.assertFalse(self.hub._doubao_turn_was_submitted(sess))

    async def test_a_brand_new_turn_one_still_replays_its_own_prompt(self):
        # The one case that must still replay: a fresh turn 1 whose startup text never landed.
        sess = self.doubao_session()
        sess.last_injection = None
        sess.conversation_ref = {}
        self.assertFalse(self.hub._doubao_turn_was_submitted(sess))
        self.assertEqual(self.hub._doubao_initial_prompt(
            sess.session_id, {"profile_snapshot": dict(sess.profile_snapshot)}),
            sess.profile_snapshot["initial_prompt"])


class CloseDuringRecoveryTests(RecoveryHubCase):
    """P0-2: a terminate/close is never hostage to a recovery that holds the resume lock."""

    async def test_a_close_cancels_a_recovery_blocked_mid_spawn(self):
        sess = self.doubao_session()
        started = asyncio.Event()
        release = asyncio.Event()
        self.addCleanup(release.set)

        async def blocked_spawn(session_id, record, prompt):
            # This runs INSIDE `async with self._resume_lock(session_id)`: the recovery holds the
            # lock while the spawn blocks, which is exactly the state the close must survive.
            started.set()
            await release.wait()
            raise RuntimeError("共享服务仍不可用")

        with patch.object(self.hub, "compute_resumable", return_value=(False, "无会话 ID")), \
                patch.object(self.hub, "_respawn_doubao_session", blocked_spawn):
            await self.crash(sess)
            await asyncio.wait_for(started.wait(), 10.0)
            self.assertEqual(sess.status, RECOVERING_STATUS)
            self.assertIn(sess.session_id, self.hub._recovery_tasks)
            result = await asyncio.wait_for(
                self.hub.close_session(sess.session_id, source="mcp"), 5.0)
        self.assertTrue(result["found"])
        self.assertEqual(sess.status, "terminated")
        # Exactly one lifecycle event: the close's own terminated row.
        self.assertEqual(len(self.events_of(sess.session_id, "terminated")), 1)
        self.assertIn("自动恢复", self.events_of(sess.session_id, "terminated")[0]["content"])
        # The recovery's progress row is what came before; the close publishes no crashed/closed row.
        self.assertEqual([event["type"] for event in self.events_of(sess.session_id)
                          if event.get("category") == "lifecycle"],
                         ["auto_recovery", "terminated"])
        # The recovery is gone and the dead object is not a live session any more.
        self.assertNotIn(sess.session_id, self.hub._recovery_tasks)
        self.assertNotIn(sess.session_id, self.hub.sessions)


class FreshRespawnHandoffTests(RecoveryHubCase):
    """P1-3: the fresh respawn keeps the bridge note and the answer that is not this turn's."""

    async def test_a_fresh_respawn_keeps_the_bridge_note_and_the_old_answer(self):
        sess = self.doubao_session()
        note = "[Hub MCP] 用 hub_call 联系父会话"
        sess._pending_mcp_bridge_note = note
        sess.final_response = "上一轮的答案"
        sess.final_response_source = "screen"
        sess._turn_submitted_for_turn_id = sess.turn_id
        record = {"profile_snapshot": dict(sess.profile_snapshot), "turn_id": sess.turn_id,
                  "created_at": sess.created_at, "step_count": 0, "resume_count": 0,
                  "title": sess.title, "last_task": sess.last_task, "cols": 120, "rows": 36}
        launch = {"engine": "doubao", "command": "cmd.exe", "args": [], "cwd": str(self.root),
                  "env": {}, "model": None, "thinking_level": None, "cli_model": None,
                  "bypass_approvals": False, "notes": []}

        async def no_start(*args, **kwargs):
            return None

        def drop(coro):
            coro.close()
            return None

        with patch.object(self.hub, "resolve_launch", return_value=dict(launch)), \
                patch.object(self.hub, "_start_with_claim_retry", no_start), \
                patch.object(self.hub, "_schedule", drop):
            new = await self.hub._respawn_doubao_session(
                sess.session_id, record, "把日志整理成 CSV\n\n[Hub Session: session_id=doubao-auto]")
        self.assertIs(new, self.hub.sessions[sess.session_id])
        self.assertEqual(new._pending_mcp_bridge_note, note)
        self.assertEqual(new.final_response, "上一轮的答案")
        self.assertEqual(new.final_response_source, "screen")
        self.assertTrue(new.final_response_from_previous_turn)
        # P0-1 rides the same handoff: the successor knows this turn's text already landed.
        self.assertEqual(new._turn_submitted_for_turn_id, sess.turn_id)


class StartupLivenessGuardTests(RecoveryHubCase):
    """P1-4: no PTY write, Enter or stale persist once a recovery replaced the object."""

    async def test_a_replaced_session_never_gets_the_enter(self):
        sess = self.doubao_session()
        writes = self.fake.inputs_of(sess.session_id)

        async def settle_during_replacement(*args, **kwargs):
            # A Doubao recovery abandons the object while the settle await is in flight.
            sess._recovery_abandoned = True
            return ("❯ ", True, True, 0.1)

        with patch.object(self.hub, "_await_typed_text_settled", settle_during_replacement):
            result = await self.hub._submit_text_to_tui(sess, "继续整理日志", "\r")
        self.assertEqual(result["reason"], "session_superseded")
        self.assertFalse(result["ok"])
        self.assertEqual(writes, ["继续整理日志"])          # the text, and never the Enter

    async def test_a_successor_in_the_sessions_map_never_gets_the_enter(self):
        sess = self.doubao_session()
        writes = self.fake.inputs_of(sess.session_id)
        successor = AgentSession(sess.session_id, "doubao", "doubao", "tui", "Doubao recovery",
                                 str(self.root), "unused", [], core=self.fake)
        successor.engine = "doubao"
        successor.status = "running"

        async def settle_during_swap(*args, **kwargs):
            self.hub.sessions[sess.session_id] = successor
            return ("❯ ", True, True, 0.1)

        with patch.object(self.hub, "_await_typed_text_settled", settle_during_swap):
            result = await self.hub._submit_text_to_tui(sess, "继续整理日志", "\r")
        self.assertEqual(result["reason"], "session_superseded")
        self.assertEqual(writes, ["继续整理日志"])
        self.assertIs(self.hub.sessions[sess.session_id], successor)

    async def test_a_replaced_object_never_persists_its_stale_row(self):
        sess = self.doubao_session()
        successor = AgentSession(sess.session_id, "doubao", "doubao", "tui", "Doubao recovery",
                                 str(self.root), "unused", [], core=self.fake)
        successor.engine = "doubao"
        successor.status = "recovering"
        self.hub.sessions[sess.session_id] = successor
        persisted: list = []

        async def record_persist(session=None, *args, **kwargs):
            persisted.append(session)

        with patch.object(self.hub, "persist_session", record_persist):
            await self.hub._record_tui_injection(
                sess, {"ok": True, "mode": "startup_task", "reason": "submitted"})
        self.assertNotIn(sess, persisted)

    async def test_the_watcher_types_nothing_after_the_object_is_abandoned(self):
        sess = self.doubao_session()
        writes = self.fake.inputs_of(sess.session_id)
        self.fake.set_screen(sess.session_id, COMPOSER)
        # Another writer holds the TUI, so the startup text must wait for its turn -- the await a
        # recovery can replace the object behind.
        held = self.hub._tui_writer_queue(sess).claim("parent_message")
        task = asyncio.create_task(
            self.hub._auto_handle_tui_startup_and_trust(sess, "把日志整理成 CSV", 0.0, "\r"))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        sess._recovery_abandoned = True       # the recovery abandoned the object meanwhile
        held.release()
        await asyncio.wait_for(task, 2.0)
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
