"""The hub's behaviour at the terminal core's contracts (design §5-§6, review step 3b-hub).

Each case here is a review finding: the exit-code mapping (F1/F2/F16), the seam's deadline
(F11), one write attempt (F9), the watched row count after a resize (F12) and the death path's
logged failure (F15). They run on the fake core, which scripts exactly what the real one reports.

Run: python -m unittest -v test_terminal_core_contracts
"""

import asyncio
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

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
        # F3: close → retire refused → resume inside that window. The retry used to fire when the
        # resumed session died on its own, removing a session the hub still lists.
        await _resumable(self.hub, self.root, "retire-resume")
        self.hub.core.spawn("retire-resume", ["cmd.exe"])
        self.hub._on_core_tick()
        self.hub.core.kill("retire-resume", 200)
        self.hub._on_core_tick()
        self.hub.core.block_retire = 1
        await self.hub._retire_ended_session("retire-resume",
                                             self.hub._incarnations.get("retire-resume", 0))
        self.assertIn("retire-resume", self.hub._retire_pending)

        result = await self.hub.resume_subagent("retire-resume", source="console")
        self.assertTrue(result["resumed"], result)
        self.assertNotIn("retire-resume", self.hub._retire_pending,
                         "the predecessor's drop survived the resume")
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


class BridgeTests(_ManagerCase):
    async def test_a_closed_handle_is_detached_exactly_once(self):
        # F7/kimi 7: the core keeps a closed sink's handle until `detach_queue`; popping it first
        # leaked the handle and its 2 MB queue for the hub's life.
        sess = await _live(self.hub, "bridge-closed")
        _replay, q = sess.attach_ws_queue()
        handle = sess.sockets.handles[q]
        self.hub.core.handles[handle][1].closed = True  # the fan-out closed under the sink
        self.hub._on_core_tick()
        self.assertEqual(self.hub.core.detach_calls, [handle], "detach ran zero or twice")
        self.assertEqual(self.hub.core.handles, {}, "the core kept the handle")
        self.assertEqual(sess.sockets.handles, {})

    async def test_a_detach_that_misses_its_deadline_is_retried_off_the_loop(self):
        # F8: the coming core raises TimeoutError on a busy registry and keeps the sink registered.
        sess = await _live(self.hub, "bridge-orphan")
        _replay, q = sess.attach_ws_queue()
        handle = sess.sockets.handles[q]
        self.hub.core.block_detach = 1
        self.hub.core.handles[handle][1].closed = True
        self.hub._on_core_tick()
        self.assertIn(handle, sess.sockets.orphans)
        for _ in range(80):
            await asyncio.sleep(0.02)
            if not self.hub.core.handles:
                break
        self.assertEqual(self.hub.core.handles, {}, "the orphaned handle was never detached")
        self.assertEqual(sess.sockets.orphans, set())

    async def test_a_full_python_queue_keeps_the_rest_of_the_batch(self):
        # F13: take_frames already removed the batch from the core; a QueueFull used to discard the
        # remainder without a count. It now waits for the next tick.
        sess = await _live(self.hub, "bridge-full")
        with patch.object(session_manager, "WS_QUEUE_MAX_FRAMES", 2):
            _replay, q = sess.attach_ws_queue()
            q.put_nowait("held")
            q.put_nowait("held")
        self.hub.core.output("bridge-full", "ONE")
        self.hub.core.output("bridge-full", "TWO")
        self.hub._on_core_tick()
        self.assertEqual(q.qsize(), 2)  # nothing fitted; both frames are pending
        pending = list(sess.sockets.pending.values())
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(pending[0]), 2)
        q.get_nowait()
        q.get_nowait()
        self.hub._on_core_tick()
        self.assertEqual([q.get_nowait() for _ in range(2)], ["ONE", "TWO"],
                         "the rest of the taken batch was dropped")
        self.assertEqual(sess.sockets.pending, {})

    async def test_a_backlog_past_the_socket_window_is_counted_as_overflow(self):
        # F13's other half: what cannot be kept is counted per handle in frames and logged once,
        # never mixed with the core's byte count.
        sess = await _live(self.hub, "bridge-overflow")
        with patch.object(session_manager, "WS_QUEUE_MAX_FRAMES", 1):
            _replay, q = sess.attach_ws_queue()
            q.put_nowait("held")
            for text in ("ONE", "TWO", "THREE"):
                self.hub.core.output("bridge-overflow", text)
            with self.assertLogs("mcp-agent-hub.session_manager", level="WARNING") as logs:
                self.hub._on_core_tick()
        handle = next(iter(sess.sockets.handles.values()))
        self.assertEqual(sess.sockets.overflow[handle], 2)
        self.assertTrue(any("dropped 2 frames" in line for line in logs.output), logs.output)

    async def test_the_core_s_socket_drop_is_reported_per_handle(self):
        # The coming contract: `dropped_bytes` in the drain record is the ring's loss only; a
        # socket's loss comes from its own `take_frames` count.
        sess = await _live(self.hub, "bridge-drops")
        _replay, q = sess.attach_ws_queue()
        handle = sess.sockets.handles[q]
        self.hub.core.handles[handle][1].dropped = 5
        with self.assertLogs("mcp-agent-hub.session_manager", level="WARNING") as logs:
            self.hub._on_core_tick()
        self.assertEqual(sess.sockets.dropped[handle], 5)
        self.assertTrue(any("dropped 5 bytes" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
