"""The hub's behaviour at the terminal core's contracts (design §5-§6, review step 3b-hub).

Each case here is a review finding: the exit-code mapping (F1/F2/F16), the seam's deadline
(F11), one write attempt (F9), the watched row count after a resize (F12) and the death path's
logged failure (F15). They run on the fake core, which scripts exactly what the real one reports.

Run: python -m unittest -v test_terminal_core_contracts
"""

import asyncio
import logging
import sqlite3
import threading
import time
import unittest
import zlib
from contextlib import closing
from unittest.mock import AsyncMock, patch

import httpx

import session_manager
from test_terminal_core_hub import _ManagerCase, _session


async def _settle(hub, times: int = 40) -> None:
    """Lets the hub's scheduled tasks (exit callbacks, retries) run."""
    for _ in range(times):
        await asyncio.sleep(0)
        await hub.db.flush()


async def _live(hub, session_id, engine="pwsh"):
    """A spawned, ticked and persisted session: a live pid, a loop, and a row to update."""
    sess = _session(hub, session_id)
    sess.engine = engine
    sess._loop = asyncio.get_running_loop()
    hub.core.spawn(session_id, ["cmd.exe"])
    hub._on_core_tick()
    await hub.persist_session(sess)
    return sess


async def _resumable(hub, root, session_id, engine="omp"):
    """A persisted omp record whose pre-checks pass, so `resume_subagent` really resumes it.

    The argv is only assembled here: the fake core's `spawn` never runs a process.
    """
    sess = _session(hub, session_id, command="omp")
    sess.engine = engine
    sess._loop = asyncio.get_running_loop()
    hub.profiles["probe"] = {"enabled": True, "command": "omp", "env": {}}
    conversation = root / f"{session_id}.jsonl"
    conversation.write_text("", encoding="utf-8")
    sess.profile_snapshot = {
        "profile_id": "probe", "command": "omp", "args": [], "cwd": str(root), "engine": engine,
    }
    sess.conversation_ref = {"session_file": str(conversation), "session_dir": str(root)}
    await hub.persist_session(sess)
    return sess


class ExitCodeTests(_ManagerCase):
    async def test_a_death_without_a_code_is_a_crash_not_a_clean_exit(self):
        # F1/kimi 2: a failed `GetExitCodeProcess` (None) or a reader panic (crashed) must never be
        # a clean exit for pwsh/generic either; the callback must not invent a code 0.
        sess = await _live(self.hub, "no-code")
        self.hub.core.set_exit("no-code", None, crashed=True)
        self.hub.core.die("no-code")
        self.hub._on_core_tick()
        await _settle(self.hub)

        self.assertIsNone(sess.exit_code, "a death with no code must keep None")
        self.assertEqual(sess.end_reason, "crashed")
        self.assertEqual(sess.status, "crashed")
        row = await self.hub.db.get_session_record("no-code")
        self.assertEqual((row["status"], row["end_reason"]), ("crashed", "crashed"))
        self.assertIsNone(row["exit_code"], "the archived row invented a code")
        events = [e for e in self.hub.get_worker_events("no-code") if e["type"] in ("exited", "crashed")]
        self.assertEqual([e["type"] for e in events], ["crashed"])
        self.assertIn("exit_code=None", events[0]["content"])

    async def test_a_late_code_rewrites_the_row_without_a_second_event(self):
        # F2/kimi 2: SF3's later code must reach the session and its row, once, with no second
        # death transition.
        sess = await _live(self.hub, "late-code")
        self.hub.core.set_exit("late-code", None, crashed=True)
        self.hub.core.die("late-code")
        self.hub._on_core_tick()
        await _settle(self.hub)
        self.assertIsNone((await self.hub.db.get_session_record("late-code"))["exit_code"])

        self.hub.core.late_code("late-code", 7)
        self.hub._on_core_tick()
        await _settle(self.hub)

        self.assertEqual(sess.exit_code, 7)
        row = await self.hub.db.get_session_record("late-code")
        self.assertEqual(row["exit_code"], 7, "the late code never reached the archived row")
        self.assertEqual((row["status"], row["end_reason"]), ("crashed", "crashed"))
        events = [e for e in self.hub.get_worker_events("late-code") if e["type"] in ("exited", "crashed")]
        self.assertEqual(len(events), 1, f"a second lifecycle event was emitted: {events}")

    async def test_shutdown_and_the_normal_path_agree_on_a_missing_code(self):
        # F16: the same death must archive the same row whether or not the hub was stopping.
        sess = await _live(self.hub, "no-code-shutdown")
        self.hub.core.set_exit("no-code-shutdown", None, crashed=True)
        self.hub.core.die("no-code-shutdown")
        self.hub._on_core_tick()
        await _settle(self.hub)
        before = await self.hub.db.get_session_record("no-code-shutdown")

        await self.hub.archive_all_sessions_on_shutdown()
        # The stop closes the store; the final row is read from the file it flushed.
        with closing(sqlite3.connect(self.root / "hub_state.db")) as conn:
            row = conn.execute(
                "SELECT status, end_reason, exit_code FROM sessions WHERE session_id='no-code-shutdown'"
            ).fetchone()
        self.assertEqual(row, ("crashed", "crashed", None))
        self.assertEqual(sess.exit_code, None)

    async def test_a_late_code_after_a_close_rewrites_the_archived_row(self):
        # r2-kimi 5: the object left `sessions` when the session closed, so the record carrying the
        # code the death lacked was applied to nothing and the row kept None forever; the retire
        # then took the core's answer away for good.
        await _live(self.hub, "closed-late-code")
        self.hub.core.set_exit("closed-late-code", None, crashed=True)
        self.hub.core.die("closed-late-code")
        self.hub._on_core_tick()
        await _settle(self.hub)
        self.assertIsNone((await self.hub.db.get_session_record("closed-late-code"))["exit_code"])
        self.hub.core.block_retire = 1
        await self.hub.close_session("closed-late-code", source="console")
        self.assertIn("closed-late-code", self.hub._retire_pending)

        self.hub.core.late_code("closed-late-code", 11)
        self.hub._on_core_tick()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if "closed-late-code" in self.hub.core.retired:
                break
        row = await self.hub.db.get_session_record("closed-late-code")
        events = [e for e in self.hub.get_worker_events("closed-late-code")
                  if e["type"] in ("exited", "crashed")]
        self.assertEqual(row["exit_code"], 11, "the late code never reached the archived row")
        self.assertIn("closed-late-code", self.hub.core.retired, "the id left the core unwritten")
        self.assertEqual(len(events), 1, f"a second lifecycle event was emitted: {events}")


class DeathSnapshotFailureTests(_ManagerCase):
    async def test_a_failed_death_snapshot_is_logged_with_its_session(self):
        # F15/kimi 9: the drain handler's own try cannot see a failed 80-row read, so the exit path
        # logs it instead of swallowing it.
        sess = await _live(self.hub, "snapshot-fails")

        def boom(*_args, **_kwargs):
            raise OSError("the scrollback read failed")

        sess.read_scrollback = boom
        self.hub.core.set_exit("snapshot-fails", 3)
        self.hub.core.die("snapshot-fails")
        with self.assertLogs("mcp-agent-hub.session_manager", level="ERROR") as logs:
            self.hub._on_core_tick()
        self.assertTrue(any("snapshot-fails" in line for line in logs.output), logs.output)
        self.assertEqual(sess.end_reason, "crashed", "the exit path must still classify the death")


class SeamDeadlineTests(_ManagerCase):
    async def test_a_table_deadline_is_an_empty_read_not_an_exception(self):
        # F11: the core raises TimeoutError when its table misses the 50 ms read deadline; every
        # seam read must answer like an unknown id instead of raising into a status poll.
        sess = _session(self.hub, "seam-deadline")
        self.hub.core.spawn("seam-deadline", ["cmd.exe"])
        self.hub._on_core_tick()
        self.hub.core.read_timeouts = 4
        self.assertEqual(sess.read_screen(max_lines=10).text, "")
        self.assertEqual(sess.read_scrollback(max_lines=10).text, "")
        self.assertEqual(sess.read_raw().text, "")
        self.assertEqual(sess.read_raw_tail(10), "")
        self.hub.core.set_screen("seam-deadline", "readable again")
        self.assertEqual(sess.read_screen(max_lines=10).text, "readable again")


class InputTests(_ManagerCase):
    async def test_a_write_deadline_is_one_attempt_and_a_false_result(self):
        # F9/kimi 8: a full write queue is one 50 ms attempt that answers False; a second blocking
        # attempt would hold the loop and could still fail.
        sess = _session(self.hub, "write-once")
        self.hub.core.spawn("write-once", ["cmd.exe"])
        self.hub._on_core_tick()
        self.hub.core.set_write_timeouts("write-once", 1)
        self.assertFalse(sess.write_input("键"))
        self.assertEqual(self.hub.core.session("write-once").inputs, [], "the text was delivered twice")
        self.assertEqual(self.hub.core.session("write-once").write_timeouts, 0, "a second attempt ran")
        self.assertTrue(sess.write_input("键"))
        self.assertEqual(self.hub.core.session("write-once").inputs, ["键"])


class WatchGeometryTests(_ManagerCase):
    async def test_a_resize_re_watches_the_generic_tool_line_scan(self):
        # F12: rows that only became visible after the pane grew must reach the watched snapshot.
        sess = _session(self.hub, "watch-rows")
        sess.start_pty(asyncio.get_running_loop())
        self.assertEqual(self.hub.core.session("watch-rows").watch_calls, [36])
        sess.resize(120, 60)
        self.assertEqual(self.hub.core.session("watch-rows").watch_calls, [36, 60])


async def _worst_loop_lag(action) -> float:
    """Runs `action` while a 5 ms heartbeat measures the worst gap the loop suffered."""
    loop = asyncio.get_running_loop()
    worst = 0.0
    done = asyncio.Event()

    async def beat():
        nonlocal worst
        last = loop.time()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = loop.time()
            worst = max(worst, now - last)
            last = now

    task = asyncio.create_task(beat())
    await asyncio.sleep(0)
    try:
        await action()
    finally:
        done.set()
        await asyncio.wait([task])
    return worst


class ShutdownTests(_ManagerCase):
    async def test_no_blocking_core_call_of_the_stop_runs_on_the_loop(self):
        # F5/kimi 3: snapshot_all, kill_all and Core.shutdown all block their caller; the loop must
        # keep answering `/health` and the console while they run.
        await _live(self.hub, "slow-stop")
        self.hub.core.snapshot_delay = 0.3
        self.hub.core.shutdown_delay = 0.2
        worst = await _worst_loop_lag(self.hub.archive_all_sessions_on_shutdown)
        self.assertLess(worst, 0.08, f"a core call of the stop held the loop for {worst:.3f}s")
        self.assertEqual(self.hub.core.calls, ["snapshot_all", "kill_all", "shutdown"])

    async def test_the_snapshot_gets_its_own_sub_budget_not_the_whole_stop(self):
        # kimi 3: a slow reader handed the whole 8 s leaves nothing for the kill and the archive.
        await _live(self.hub, "budget")
        await self.hub.archive_all_sessions_on_shutdown()
        self.assertEqual(len(self.hub.core.snapshot_budgets), 1)
        self.assertLessEqual(self.hub.core.snapshot_budgets[0], 2100,
                             "the snapshot was handed more than its sub-budget")

    async def test_a_death_before_the_stop_keeps_its_ordinary_finalization(self):
        # kimi 4: the exit callback was already running (the native transcript drain) when the stop
        # began; cancelling it away loses the final answer, the final row and the one event.
        sess = await _live(self.hub, "pre-death", engine="omp")
        self.hub.core.set_exit("pre-death", 3)
        self.hub.core.die("pre-death")
        self.hub._on_core_tick()
        with patch.object(session_manager, "NATIVE_TRANSCRIPT_DRAIN_SEC", 0.4):
            await asyncio.sleep(0.05)  # the exit task is inside its drain
            await self.hub.archive_all_sessions_on_shutdown()
        events = [e for e in self.hub.get_worker_events("pre-death") if e["type"] in ("exited", "crashed")]
        self.assertEqual([e["type"] for e in events], ["crashed"], "the natural exit lost its one event")
        with closing(sqlite3.connect(self.root / "hub_state.db")) as conn:
            row = conn.execute(
                "SELECT status, exit_code FROM sessions WHERE session_id='pre-death'"
            ).fetchone()
        self.assertEqual(row, ("crashed", 3))
        self.assertIsNotNone(sess.ended_at)

    async def test_a_death_whose_record_lags_one_tick_keeps_its_classification(self):
        # r2-kimi 2: the fixed core may withhold a death record for one drain (contract 5). One
        # pre-stop drain then still sees the session as alive, marks it interrupted, and the real
        # death lands after `_shutting_down`, where its ordinary lifecycle event is suppressed.
        sess = await _live(self.hub, "lagged-stop", engine="omp")
        self.hub.core.set_exit("lagged-stop", 5)
        self.hub.core.withhold_death_records = 1
        self.hub.core.die("lagged-stop")
        with patch.object(session_manager, "NATIVE_TRANSCRIPT_DRAIN_SEC", 0.0):
            await self.hub.archive_all_sessions_on_shutdown()
        with closing(sqlite3.connect(self.root / "hub_state.db")) as conn:
            row = conn.execute(
                "SELECT status, end_reason, exit_code FROM sessions WHERE session_id='lagged-stop'"
            ).fetchone()
        events = [e for e in self.hub.get_worker_events("lagged-stop")
                  if e["type"] in ("exited", "crashed")]
        self.assertEqual(row, ("crashed", "crashed", 5),
                         "a death that preceded the stop was archived as an interruption")
        self.assertEqual([e["type"] for e in events], ["crashed"],
                         "the natural death lost its ordinary lifecycle event")
        self.assertFalse(sess.is_process_alive())

    async def test_the_forced_final_row_is_really_written(self):
        # r2-kimi 1: the last-resort fallback passed coroutines to `asyncio.wait`, which rejects
        # them, so a stop that ran out of archive budget wrote no final row at all.
        await _live(self.hub, "forced-final")
        await self.hub.db.flush()

        async def stalled(*_args, **_kwargs):
            await asyncio.sleep(5)

        with patch.object(self.hub, "_archive_session", new=stalled), patch.multiple(
            session_manager,
            SHUTDOWN_MAX_SEC=1.0,
            SHUTDOWN_SNAPSHOT_MAX_SEC=0.0,
            SHUTDOWN_KILL_MAX_SEC=0.05,
            SHUTDOWN_CORE_STOP_MAX_SEC=0.05,
        ):
            await self.hub.archive_all_sessions_on_shutdown()
        with closing(sqlite3.connect(self.root / "hub_state.db")) as conn:
            row = conn.execute(
                "SELECT status, archived_at FROM sessions WHERE session_id='forced-final'"
            ).fetchone()
        self.assertIsNotNone(row, "the forced path wrote no row for the session")
        self.assertIsNotNone(row[1], "the forced path left the session without a final row")
        self.assertEqual(row[0], "interrupted")

    async def test_the_kill_grace_fits_inside_the_stop_s_wait(self):
        # R5a: the core was handed a fresh 1 s grace whenever less than that was left for the wait,
        # so a kill that used it outlived the stop and logged a timeout on the normal path.
        await _live(self.hub, "kill-grace")
        messages = []
        handler = logging.Handler()
        handler.emit = lambda record: messages.append(record.getMessage())
        manager_log = logging.getLogger("mcp-agent-hub.session_manager")
        manager_log.addHandler(handler)

        def wedged(grace_ms):
            self.hub.core.kill_graces.append(int(grace_ms))
            time.sleep(int(grace_ms) / 1000.0)  # a tree that uses every bit of its grace

        try:
            with patch.object(session_manager, "SHUTDOWN_KILL_MAX_SEC", 0.3), \
                    patch.object(self.hub.core, "kill_all", wedged):
                await self.hub.archive_all_sessions_on_shutdown()
        finally:
            manager_log.removeHandler(handler)
        self.assertEqual(self.hub.core.kill_graces, [200],
                         "the grace did not leave room for the stop's own wait")
        self.assertFalse([m for m in messages if "Timed out stopping session processes" in m],
                         messages)

    async def test_a_session_the_snapshot_missed_is_archived_off_the_loop(self):
        # R5b: with no snapshot the archive reads the core itself, and on the loop those reads held
        # it for their deadline. The stop takes that snapshot off the loop, inside its budget.
        sess = await _live(self.hub, "snapshot-miss")
        self.hub.core.output("snapshot-miss", "ARCHIVED-BLOB\r\n")
        self.hub.core.set_exit("snapshot-miss", 0)
        self.hub.core.die("snapshot-miss")
        self.hub._on_core_tick()  # the death takes its own path now; the stop is only the archive
        await _settle(self.hub)
        self.assertFalse(sess.is_process_alive())
        core = self.hub.core
        screen, scrollback = core.screen, core.scrollback

        def slow_screen(*args, **kwargs):
            time.sleep(0.12)
            return screen(*args, **kwargs)

        def slow_scrollback(*args, **kwargs):
            time.sleep(0.12)
            return scrollback(*args, **kwargs)

        with patch.object(core, "snapshot_all", return_value=[]), \
                patch.object(core, "screen", slow_screen), \
                patch.object(core, "scrollback", slow_scrollback):
            worst = await _worst_loop_lag(self.hub.archive_all_sessions_on_shutdown)
        self.assertLess(worst, 0.08, f"the archive read the core on the loop for {worst:.3f}s")
        with closing(sqlite3.connect(self.root / "hub_state.db")) as conn:
            row = conn.execute(
                "SELECT archived_at, scrollback FROM sessions WHERE session_id='snapshot-miss'"
            ).fetchone()
        self.assertIsNotNone(row[0], "the missed session was never archived")
        self.assertIn("ARCHIVED-BLOB", zlib.decompress(bytes(row[1])).decode("utf-8"),
                      "the fallback snapshot lost the raw ring the archive stores")


class RetireIncarnationTests(_ManagerCase):
    async def test_a_refused_retire_is_retried_without_a_new_record(self):
        # kimi 6: a quiet id publishes no record, so a pending retire that waits for one is stranded.
        await _live(self.hub, "retire-retry")
        self.hub.core.kill("retire-retry", 200)
        self.hub._on_core_tick()
        self.hub.core.block_retire = 1  # the first attempt misses the table deadline
        await self.hub._retire_ended_session("retire-retry", self.hub._incarnations.get("retire-retry", 0))
        self.assertIn("retire-retry", self.hub._retire_pending)
        for _ in range(80):
            await asyncio.sleep(0.02)
            if self.hub.core.stats()["sessions"] == 0:
                break
        self.assertEqual(self.hub.core.stats()["sessions"], 0, "no retry happened without a record")
        self.assertNotIn("retire-retry", self.hub._retire_pending)

    async def test_a_pending_retire_never_removes_the_resumed_incarnation(self):
        # F3/R1: close → retire refused → resume inside that window. The retry used to fire while
        # the resume ran, removing the id from the core: the resumed session was unlisted and the
        # sockets attached before it were closed instead of handed the successor's banner.
        await _resumable(self.hub, self.root, "retire-resume")
        self.hub.core.spawn("retire-resume", ["cmd.exe"])
        self.hub._on_core_tick()
        self.hub.core.kill("retire-resume", 200)
        self.hub._on_core_tick()
        self.hub.core.block_retire = 1
        # The exit callback is still inside its transcript drain, so `_end_predecessor` has to wait
        # for it; that wait is the window the scheduled retire used to fire in.
        with patch.object(session_manager, "NATIVE_TRANSCRIPT_DRAIN_SEC", 0.2):
            await self.hub._retire_ended_session(
                "retire-resume", self.hub._incarnations.get("retire-resume", 0))
            self.assertIn("retire-resume", self.hub._retire_pending)

            result = await self.hub.resume_subagent("retire-resume", source="console")
        self.assertTrue(result["resumed"], result)
        self.assertNotIn("retire-resume", self.hub._retire_pending,
                         "the predecessor's drop survived the resume")
        # A1/Q11: the sockets belong to the id inside the core, so the hub's part of the handover is
        # the banner; a retried retire must never take the successor's entry away.
        self.hub._on_core_tick()
        state = self.hub.core.session("retire-resume")
        self.assertEqual(state.ws_prefix, session_manager.resume_banner(1),
                         "the resume banner never reached the id's fan-out")
        resumed = self.hub.sessions["retire-resume"]
        self.hub.core.output("retire-resume", "STILL-LIVE\r\n")
        self.hub.core.set_exit("retire-resume", 0)
        self.hub.core.die("retire-resume")
        self.hub._on_core_tick()
        for _ in range(80):
            await asyncio.sleep(0.02)
        self.assertIn("retire-resume", self.hub.sessions)
        self.assertIn("STILL-LIVE", resumed.read_raw().text,
                      "the retried retire removed the resumed incarnation from the core")

    async def test_a_refused_drop_keeps_retrying_past_the_cap(self):
        # r2-kimi 6: the cap cleared the pending drop and armed nothing, so a core entry the table
        # kept refusing stayed (ring, screen and all) until the hub restarted. The cap now only
        # slows the retries down, and the one warning states the real refusal count.
        await _live(self.hub, "retire-cap")
        self.hub.core.kill("retire-cap", 200)
        self.hub._on_core_tick()
        self.hub.core.block_retire = 1000
        with patch.object(session_manager, "RETIRE_RETRY_MAX_ATTEMPTS", 3), \
                patch.object(session_manager, "RETIRE_RETRY_INTERVAL_SEC", 0.0):
            # The first refusal arms the timer; the timer's own attempts are what the cap counts.
            await self.hub._retire_ended_session(
                "retire-cap", self.hub._incarnations.get("retire-cap", 0))
            with self.assertLogs("mcp-agent-hub.session_manager", level="WARNING") as logs:
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if logs.output:
                        break
        warnings = [line for line in logs.output if "retire(retire-cap)" in line]
        self.assertEqual(len(warnings), 1, logs.output)
        self.assertIn("refused 4 times", warnings[0],
                      "the warning counts the refusals it saw, not the cap")
        self.assertIn("retrying every", warnings[0],
                      "the cap gave up on a drop the core still holds")
        self.assertIn("retire-cap", self.hub._retire_pending,
                      "a permanent drop was forgotten while the core entry still exists")
        self.assertGreater(self.hub._retire_backoff.get("retire-cap", 0.0) - time.monotonic(),
                           1.0, "the drop did not back off after the cap")

    async def test_a_refusal_that_lands_after_a_resume_is_not_queued_again(self):
        # r2-kimi 7: a retire already inside the core when a resume bumped the incarnation wrote
        # its old generation back into the pending map, and every later timer attempt returned at
        # the stale guard without removing it: a permanent 250 ms no-op.
        await _live(self.hub, "stale-retire")
        self.hub.core.kill("stale-retire", 200)
        self.hub._on_core_tick()
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def delayed_refusal(session_id):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise AssertionError("the probe did not release retire")
            raise ValueError("session stale-retire's exit record has not been drained")

        original = self.hub.core.retire
        self.hub.core.retire = delayed_refusal
        try:
            task = asyncio.create_task(self.hub._retire_ended_session("stale-retire", 0))
            await asyncio.wait_for(entered.wait(), 1)
            self.hub._next_incarnation("stale-retire")
            release.set()
            await task
        finally:
            self.hub.core.retire = original
        self.assertNotIn("stale-retire", self.hub._retire_pending,
                         "a refusal of the old incarnation was queued against the new one")
        self.assertIsNone(self.hub._retry_timer, "a timer was armed for a stale drop")


class PredecessorResumeTests(_ManagerCase):
    async def test_a_deferred_predecessor_exit_is_finalized_before_the_successor(self):
        # kimi 5: the predecessor died and its exit callback is still draining the transcript when
        # the resume installs the successor. The old object used to return early and lose its final
        # row and its one lifecycle event.
        await _resumable(self.hub, self.root, "deferred-exit")
        self.hub.core.spawn("deferred-exit", ["cmd.exe"])
        self.hub._on_core_tick()
        self.hub.core.set_exit("deferred-exit", 5)
        self.hub.core.die("deferred-exit")
        with patch.object(session_manager, "NATIVE_TRANSCRIPT_DRAIN_SEC", 0.4):
            self.hub._on_core_tick()
            await asyncio.sleep(0.05)  # the old exit task is inside its drain
            old = self.hub.sessions["deferred-exit"]
            result = await self.hub.resume_subagent("deferred-exit", source="console")
        self.assertTrue(result["resumed"], result)
        self.assertIsNot(self.hub.sessions["deferred-exit"], old)
        self.assertIsNotNone(old.ended_at, "the predecessor was never finalized")
        self.assertEqual((old.status, old.exit_code), ("crashed", 5))
        events = [e for e in self.hub.get_worker_events("deferred-exit")
                  if e["type"] in ("exited", "crashed")]
        self.assertEqual([e["type"] for e in events], ["crashed"],
                         "the predecessor's exit event was lost to the resume")
        successor = self.hub.sessions["deferred-exit"]
        self.assertTrue(successor.is_process_alive(), "the successor is not live")
        self.assertIsNone(successor.exit_code, "the predecessor's death hit the successor")

    async def test_a_predecessor_s_late_code_is_applied_before_the_successor(self):
        # kimi 5 (second half): a record for the predecessor that is still queued when the resume
        # runs would be dropped by the core's table insert. It is drained into the old object.
        await _resumable(self.hub, self.root, "predecessor-code")
        self.hub.core.spawn("predecessor-code", ["cmd.exe"])
        self.hub._on_core_tick()
        self.hub.core.set_exit("predecessor-code", None, crashed=True)
        self.hub.core.die("predecessor-code")
        self.hub._on_core_tick()
        await _settle(self.hub)
        old = self.hub.sessions["predecessor-code"]
        self.assertIsNone(old.exit_code)
        self.hub.core.late_code("predecessor-code", 9)  # queued, not drained

        result = await self.hub.resume_subagent("predecessor-code", source="console")
        self.assertTrue(result["resumed"], result)
        self.assertEqual(old.exit_code, 9, "the predecessor's late code was lost to the resume")
        successor = self.hub.sessions["predecessor-code"]
        self.assertIsNot(successor, old)
        self.assertIsNone(successor.exit_code)

    async def test_a_withheld_death_record_still_finalizes_the_predecessor_once(self):
        # R6/contract 5: the core may publish the predecessor's death one drain late, and the
        # successor's spawn drops whatever record the table still holds for the id. A single drain
        # in `_end_predecessor` then loses the predecessor's final row and its one lifecycle event.
        sess = await _live(self.hub, "withheld-exit")
        self.hub.core.set_exit("withheld-exit", 4)
        self.hub.core.withhold_death_records = 1
        self.hub.core.die("withheld-exit")
        with patch.object(session_manager, "NATIVE_TRANSCRIPT_DRAIN_SEC", 0.0):
            await self.hub._end_predecessor("withheld-exit", sess)
            self.assertIsNotNone(sess.ended_at, "the withheld death record was lost")
            self.assertEqual((sess.status, sess.exit_code), ("crashed", 4))
            events = [e for e in self.hub.get_worker_events("withheld-exit")
                      if e["type"] in ("exited", "crashed")]
            self.assertEqual([e["type"] for e in events], ["crashed"],
                             "the predecessor's one event was lost")
            # The resume's spawn is what drops an unread record: run it and check the ending held.
            self.hub.core.spawn("withheld-exit", ["cmd.exe"])
            self.hub._on_core_tick()
            await _settle(self.hub)
        events = [e for e in self.hub.get_worker_events("withheld-exit")
                  if e["type"] in ("exited", "crashed")]
        self.assertEqual([e["type"] for e in events], ["crashed"],
                         "the successor's arrival emitted a second lifecycle event")
        self.assertIsNone(self.hub.core.session("withheld-exit").exit_code,
                          "the predecessor's death hit the successor")

    async def test_a_predecessor_late_code_reaches_its_final_write(self):
        # r2-kimi 4: the late code updated the old object and scheduled a rewrite, but the resume
        # installed the successor first and the rewrite's identity guard dropped the write.
        await _resumable(self.hub, self.root, "late-predecessor")
        self.hub.core.spawn("late-predecessor", ["cmd.exe"])
        self.hub._on_core_tick()
        self.hub.core.set_exit("late-predecessor", None, crashed=True)
        self.hub.core.die("late-predecessor")
        # No transcript drain: the exit task is finished before the resume, so `_end_predecessor`
        # has no await window in which the scheduled rewrite could still find the old object.
        with patch.object(session_manager, "NATIVE_TRANSCRIPT_DRAIN_SEC", -1.0):
            self.hub._on_core_tick()
            await _settle(self.hub)
            old = self.hub.sessions["late-predecessor"]
            self.assertIsNone(old.exit_code)
            self.hub.core.late_code("late-predecessor", 9)  # queued, not drained
            writes = []
            original = self.hub.persist_session

            async def spy(session, **kwargs):
                writes.append((session, bool(kwargs.get("final")), session.exit_code))
                await original(session, **kwargs)

            with patch.object(self.hub, "persist_session", new=spy):
                result = await self.hub.resume_subagent("late-predecessor", source="console")
        self.assertTrue(result["resumed"], result)
        self.assertTrue(any(session is old and final and code == 9
                            for session, final, code in writes),
                        "the predecessor's late code never reached its final row")
        self.assertIsNone(self.hub.sessions["late-predecessor"].exit_code)


class LifecycleClaimTests(_ManagerCase):
    def _refusing_spawn(self, refusals):
        """Injects the core's exact lifecycle refusal (contract 1) into `spawn` this many times."""
        original = self.hub.core.spawn
        calls = []

        def refuse(*args, **kwargs):
            calls.append(args[0] if args else "")
            if len(calls) <= refusals:
                raise ValueError(
                    f"session {args[0]}: another spawn, attach_text or retire is in flight; retry")
            return original(*args, **kwargs)

        self.hub.core.spawn = refuse
        return calls

    async def test_a_lifecycle_claim_refusal_is_retried_before_the_resume_fails(self):
        # R4/r2-kimi 3: the hub's own retried retire holds the id's claim exactly when a resume
        # reaches its spawn; the refusal says "retry" and used to become a permanent 恢复失败.
        await _resumable(self.hub, self.root, "claim-retry")
        calls = self._refusing_spawn(1)
        try:
            result = await self.hub.resume_subagent("claim-retry", source="console")
        except ValueError as error:
            self.fail(f"a transient claim refusal failed the resume: {error}")
        self.assertTrue(result["resumed"], result)
        self.assertEqual(len(calls), 2, "the refusal was not retried")
        self.assertIn("claim-retry", self.hub.sessions)

    async def test_three_lifecycle_claim_refusals_still_fail_the_resume(self):
        # The retry is bounded: a claim that never frees fails as it did before, with the existing
        # Chinese message, and no other ValueError is retried.
        old = await _resumable(self.hub, self.root, "claim-fail")
        calls = self._refusing_spawn(3)
        with self.assertRaises(ValueError) as raised:
            await self.hub.resume_subagent("claim-fail", source="console")
        self.assertIn("恢复失败", str(raised.exception))
        self.assertEqual(len(calls), 3, "the retry count is not the two more the contract allows")
        self.assertIs(self.hub.sessions["claim-fail"], old,
                      "the failed resume did not put the archived object back")

    async def test_another_value_error_is_not_retried(self):
        # kimi 3: only the exact in-flight refusal is transient. A core refusal that means "this id
        # is live" must fail the resume at once, not after three attempts.
        await _resumable(self.hub, self.root, "claim-live")
        calls = []

        def refuse(*args, **kwargs):
            calls.append(args[0] if args else "")
            raise ValueError(f"session {args[0]} is already running in the core")

        self.hub.core.spawn = refuse
        with self.assertRaises(ValueError) as raised:
            await self.hub.resume_subagent("claim-live", source="console")
        self.assertIn("恢复失败", str(raised.exception))
        self.assertEqual(len(calls), 1, "a non-transient ValueError was retried")


class DshResumeTests(_ManagerCase):
    async def test_a_completed_text_predecessor_is_not_a_crash(self):
        # R2: the core's kill of a text entry publishes `exit_code=None, crashed=false`, and F1
        # turned that into a crash: a completed DSH session was archived as crashed and the parent
        # was told a session it is resuming had crashed.
        sess = _session(self.hub, "dsh-resume", command="", args=[])
        sess.engine = "dsh"
        sess.kind = "virtual"
        sess.dsh_job_id = "job-1"
        sess.profile_snapshot = {"engine": "dsh", "kind": "virtual", "cwd": str(self.root),
                                 "dsh_spec": {}}
        sess.conversation_ref = {"dsh_job_id": "job-1", "dsh_token": "tok"}
        sess._loop = asyncio.get_running_loop()
        sess.start_virtual(sess._loop)
        self.hub._on_core_tick()
        # The job watcher saw the job finish; the core's text entry stays live until a resume ends it.
        sess.status = "completed"
        sess.end_reason = "exited"
        sess.exit_code = 0
        sess.completion_reported = True
        sess.ended_at = time.time()
        await self.hub.persist_session(sess)
        record = {
            "session_id": "dsh-resume", "engine": "dsh", "cwd": str(self.root),
            "profile_snapshot": dict(sess.profile_snapshot),
            "conversation_ref": dict(sess.conversation_ref),
        }
        answer = httpx.Response(200, json={"job": {"id": "job-1", "status": "done"}})
        with patch.object(session_manager, "prepare_dsh_workspace",
                          return_value={"allowed": True, "status": "ready", "reason": ""}), \
                patch.object(self.hub, "build_resume_launch", return_value={"cwd": str(self.root)}), \
                patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=answer):
            await self.hub._resume_dsh_session("dsh-resume", record, None)
        events = [e for e in self.hub.get_worker_events("dsh-resume")
                  if e["type"] in ("exited", "crashed")]
        self.assertEqual(events, [], "the completed predecessor was announced as crashed")
        self.assertEqual((sess.status, sess.end_reason, sess.exit_code),
                         ("completed", "exited", 0),
                         "the supersede overwrote the end state the session had earned")
        row = await self.hub.db.get_session_record("dsh-resume")
        self.assertEqual(row["status"], "completed")
        self.assertNotEqual(row["end_reason"], "crashed",
                            "the completed predecessor's row was archived as a crash")


class DshLifecycleTests(_ManagerCase):
    async def test_a_completed_text_session_closes_and_can_be_attached_again(self):
        # F4/kimi 1: a DSH session's liveness is its remote job, so `terminate` never ended the core
        # text entry; the id could neither be retired nor attached again.
        sess = _session(self.hub, "dsh-done", command="", args=[])
        sess.engine = "dsh"
        sess.kind = "virtual"
        sess.dsh_job_id = "job-1"
        sess._loop = asyncio.get_running_loop()
        sess.start_virtual(sess._loop)
        self.hub._on_core_tick()
        # T1: the fake gives a text session no process, like the real core's `attach_text`.
        self.assertFalse(sess.has_process, "a text session claimed a process")

        sess.emit_virtual_ansi("远程任务已完成\r\n")
        self.hub._on_core_tick()
        sess.status = "completed"
        await self.hub.persist_session(sess)
        with patch.object(self.hub, "_cancel_dsh_job", new_callable=AsyncMock) as cancel:
            closed = await self.hub.close_session("dsh-done", source="console")
            cancel.assert_not_awaited()  # the remote job is not live; nothing to cancel there
        self.assertTrue(closed["found"])
        self.assertEqual(self.hub.core.stats()["sessions"], 0,
                         "the core text entry outlived its session")
        self.assertEqual(self.hub.core.retired, ["dsh-done"])
        self.assertNotIn("dsh-done", self.hub._retire_pending)
        # the freed id can carry a later incarnation again
        self.hub.core.attach_text("dsh-done", cols=120, rows=36)


class DrainRequestTests(_ManagerCase):
    """§6.4/§5.2: what the console's socket recorded is applied by the hub, in order, once."""

    async def test_resize_requests_apply_in_order_with_the_console_rules(self):
        # The socket no longer calls the core's resize: the request travels in the drain record and
        # the hub applies its own clamp and single-window propagation, in arrival order.
        target = await _live(self.hub, "resize-target")
        background = await _live(self.hub, "resize-background")
        self.hub.default_cols, self.hub.default_rows = 120, 36
        self.hub.core.request_resize("resize-target", 100, 30, propagate=False)
        self.hub.core.request_resize("resize-target", 140, 44, propagate=False)
        self.hub._on_core_tick()
        self.assertEqual((target.cols, target.rows), (140, 44), "the last request in order did not win")
        self.assertEqual((self.hub.core.session("resize-target").cols,
                          self.hub.core.session("resize-target").rows), (140, 44),
                         "the applied size never reached the core")
        self.assertEqual((background.cols, background.rows), (120, 36),
                         "propagate=false resized a background session")
        # A single-window request propagates; an earlier `false` must not survive it.
        self.hub.core.request_resize("resize-target", 96, 24, propagate=True)
        self.hub._on_core_tick()
        self.assertEqual((background.cols, background.rows), (96, 24),
                         "the propagated request never reached the background session")
        self.assertEqual((self.hub.default_cols, self.hub.default_rows), (96, 24))

    async def test_a_resize_request_is_clamped_and_taken_once(self):
        target = await _live(self.hub, "resize-clamp")
        self.hub.core.request_resize("resize-clamp", 4, 2, propagate=False)
        self.hub._on_core_tick()
        self.assertEqual((target.cols, target.rows), (20, 5), "the 20x5 clamp is missing")
        self.hub._on_core_tick()  # nothing was recorded again: the request is consumed
        self.assertEqual((target.cols, target.rows), (20, 5))
        self.assertEqual(self.hub.core.session("resize-clamp").resize_requests, [])

    async def test_text_input_requests_reach_the_echo_and_the_line_callback_once(self):
        # A session with no PTY: the core records the socket's input and never writes it, because
        # the DSH echo and its line callback are the hub's (design §8, gaps 9).
        sess = _session(self.hub, "text-input", command="", args=[])
        sess.engine = "dsh"
        sess.kind = "virtual"
        lines = []
        sess.start_virtual(asyncio.get_running_loop(),
                           on_virtual_line_cb=lambda sid, line: lines.append((sid, line)))
        self.hub._on_core_tick()
        self.hub.core.request_input("text-input", "hi")
        self.hub.core.request_input("text-input", "there\r")
        self.hub._on_core_tick()
        self.assertEqual(lines, [("text-input", "hithere")], "the line callback missed the typed text")
        self.assertIn("hithere", sess.read_raw().text, "the echo never reached the text screen")
        self.assertEqual(self.hub.core.inputs_of("text-input"), [],
                         "text-session input was written to the core")
        self.hub._on_core_tick()
        self.assertEqual(lines, [("text-input", "hithere")], "the request was delivered twice")

    async def test_a_text_session_echoes_a_backspace_and_delivers_one_line(self):
        sess = _session(self.hub, "text-edit", command="", args=[])
        sess.kind = "virtual"
        lines = []
        sess.start_virtual(asyncio.get_running_loop(),
                           on_virtual_line_cb=lambda sid, line: lines.append(line))
        self.hub.core.request_input("text-edit", "ab\x7f\r")
        self.hub._on_core_tick()
        self.assertEqual(lines, ["a"], "backspace did not edit the virtual line")

    async def test_a_bad_request_costs_that_request_only(self):
        # master 3874299: one record (or request) that raises must not stop the next one.
        target = await _live(self.hub, "request-bad")
        sess = _session(self.hub, "request-bad-2", command="", args=[])
        sess.start_virtual(asyncio.get_running_loop())
        self.hub._on_core_tick()
        self.hub.core.request_resize("request-bad", "not-a-number", 24, propagate=False)
        self.hub.core.request_resize("request-bad-2", 88, 22, propagate=False)
        with self.assertLogs("mcp-agent-hub.session_manager", level="ERROR") as logs:
            self.hub._on_core_tick()
        self.assertTrue(any("resize request for request-bad failed" in line for line in logs.output),
                        logs.output)
        self.assertEqual((target.cols, target.rows), (120, 36), "the bad request was applied")
        self.assertEqual((sess.cols, sess.rows), (88, 22),
                         "a bad request stopped the next session's record")

    async def test_a_socket_s_loss_is_kept_per_handle_and_apart_from_the_ring(self):
        # §5.4: the per-handle count is that socket's own; handles are per id and collide, so the
        # key is (session id, handle). It never joins the ring's `dropped_bytes`.
        first = await _live(self.hub, "drops-first")
        second = await _live(self.hub, "drops-second")
        self.hub.core.note_socket_drops("drops-first", 7, 5)
        self.hub.core.note_socket_drops("drops-second", 7, 9)
        with self.assertLogs("mcp-agent-hub.session_manager", level="WARNING") as logs:
            self.hub._on_core_tick()
        self.assertEqual(self.hub._socket_drops, {("drops-first", 7): 5, ("drops-second", 7): 9})
        warnings = [line for line in logs.output if "dropped" in line]
        self.assertEqual(len(warnings), 2, warnings)
        # The ring's own loss and the socket's stay apart: the record's `dropped_bytes` is the ring's.
        self.assertEqual((self.hub.core.session("drops-first").dropped_bytes,
                          self.hub.core.session("drops-second").dropped_bytes), (0, 0))
        self.assertEqual((first.read_raw().bytes, second.read_raw().bytes), (0, 0))

    async def test_the_same_socket_reports_a_growing_count_once_per_increase(self):
        await _live(self.hub, "drops-grow")
        self.hub.core.note_socket_drops("drops-grow", 3, 5)
        self.hub._on_core_tick()
        with self.assertLogs("mcp-agent-hub.session_manager", level="WARNING") as logs:
            self.hub.core.note_socket_drops("drops-grow", 3, 9)
            self.hub._on_core_tick()
        self.assertEqual(self.hub._socket_drops[("drops-grow", 3)], 9)
        self.assertEqual(len([line for line in logs.output if "dropped" in line]), 1, logs.output)
        # A detached socket's final count is reported once and is not reported again.
        self.hub.core.note_socket_drops("drops-grow", 3, 9)
        self.hub._on_core_tick()
        self.assertEqual(self.hub._socket_drops[("drops-grow", 3)], 9)

    async def test_closing_a_session_closes_its_sockets_and_forgets_them(self):
        await _live(self.hub, "close-sockets")
        self.hub.core.open_socket("close-sockets", 2)
        self.hub.core.note_socket_drops("close-sockets", 1, 4)
        self.hub._on_core_tick()
        await self.hub.close_session("close-sockets", source="console")
        self.assertEqual(self.hub.core.close_socket_calls.count("close-sockets"), 1,
                         "the close path did not ask the core to close the pane's sockets")
        self.assertEqual(self.hub.core.socket_count(), 0, "a console socket outlived its session")
        self.assertEqual(self.hub._socket_drops, {}, "the closed id's diagnostics stayed")
        self.assertNotIn("close-sockets", self.hub.sessions)
        self.assertNotIn("close-sockets", self.hub.core.sessions,
                         "the core's entry outlived the close's retire")


if __name__ == "__main__":
    unittest.main()
