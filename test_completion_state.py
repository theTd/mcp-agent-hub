"""Two completion-state bugs, each a turn that is not really over (or really is).

omp: a turn whose last record the tail watcher never applied stayed "running" for good, so a
group wait never settled and the final answer, already in the session file, was never read.
agy: a turn that ended only to wait for its own background task was reported "completed" with the
"I have launched ..." line as the final answer, and the task's callback then started a new step.
"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from session_manager import AgentSession, HubSessionManager
from fake_terminal_core import FakeTerminal


class CompletionStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temporary = tempfile.TemporaryDirectory(prefix="hub-completion-")
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

    def session(self, engine):
        sid = f"{engine}-completion"
        sess = AgentSession(sid, engine, engine, "tui", "Completion fixture", str(self.root),
                            "unused", [], core=self.fake)
        sess.engine = engine
        sess.status = "running"
        self.fake.spawn(sid, ["fixture"])
        sess._loop = asyncio.get_running_loop()
        self.hub.sessions[sid] = sess
        self.hub._on_core_tick()
        return sess

    async def until(self, condition, timeout=10):
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

    # ---- omp ------------------------------------------------------------------------------

    @staticmethod
    def omp_tool_start(call_id, command="echo ok"):
        return {"type": "custom", "customType": "tool_execution_start",
                "data": {"toolName": "bash", "toolCallId": call_id, "args": {"command": command}}}

    @staticmethod
    def omp_tool_result(call_id, text="ok"):
        return {"type": "message", "message": {"role": "toolResult", "toolCallId": call_id,
                                               "toolName": "bash",
                                               "content": [{"type": "text", "text": text}]}}

    @staticmethod
    def omp_final(text):
        return {"type": "message", "message": {"role": "assistant", "stopReason": "stop",
                                               "content": [{"type": "text", "text": text}]}}

    async def test_a_record_that_fails_costs_that_record_only(self):
        """One record the watcher cannot apply must not throw away the rows read after it."""
        sess = self.session("omp")
        work = self.root / "omp-session"
        work.mkdir()
        original = HubSessionManager.record_telemetry
        failed = {"done": False}

        async def flaky(self, session_id, data):
            if (data.get("hook_event") == "PostToolUse" and data.get("call_id") == "call-1"
                    and not failed["done"]):
                failed["done"] = True
                raise RuntimeError("synthetic record failure")
            return await original(self, session_id, data)

        with patch.object(HubSessionManager, "record_telemetry", flaky):
            watch = self.hub._schedule(self.hub._watch_omp_session(sess, work))
            try:
                await asyncio.sleep(0.3)
                path = work / "session.jsonl"
                self.append(path, self.omp_tool_start("call-1"), self.omp_tool_result("call-1"))
                self.append(path, self.omp_tool_start("call-2"), self.omp_tool_result("call-2"),
                            self.omp_final("second answer"))
                await self.until(lambda: sess.status == "completed")
            finally:
                watch.cancel()
                await asyncio.gather(watch, return_exceptions=True)

        self.assertTrue(failed["done"])
        # call-2's start and result were read after the failed record, so the turn is step 2.
        self.assertEqual(sess.step_count, 2)
        self.assertEqual(sess.final_response, "second answer")

    async def test_the_tail_sweep_completes_a_turn_the_watcher_never_read(self):
        sess = self.session("omp")
        path = self.root / "omp-tail.jsonl"
        self.append(path, self.omp_tool_start("call-1"), self.omp_tool_result("call-1"),
                    self.omp_final("answer from the file"))
        sess.transcript_path = path

        self.assertEqual(await self.hub._sweep_omp_turn_ends(), 1)
        self.assertEqual(sess.status, "completed")
        self.assertEqual(sess.final_response, "answer from the file")
        done = self.events(sess.session_id, "turn_completed")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["data"]["source"], "omp_tail_sweep")
        # the same row is never completed twice
        self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)
        self.assertEqual(len(self.events(sess.session_id, "turn_completed")), 1)

    async def test_the_sweep_completes_only_a_turn_end_row_of_this_turn(self):
        sess = self.session("omp")
        path = self.root / "omp-tail.jsonl"
        self.append(path, self.omp_final("first answer"))
        sess.transcript_path = path
        self.assertEqual(await self.hub._sweep_omp_turn_ends(), 1)

        # A new turn's answer is not the previous turn's row: nothing new means nothing to end.
        self.hub.begin_new_turn(sess, status="running")
        self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)
        self.assertEqual(sess.status, "running")

        # A tool start in the new turn means it is not over either.
        self.append(path, self.omp_tool_start("call-1"))
        self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)

        self.append(path, self.omp_tool_result("call-1"), self.omp_final("second answer"))
        self.assertEqual(await self.hub._sweep_omp_turn_ends(), 1)
        self.assertEqual(sess.final_response, "second answer")

    async def test_a_turn_that_starts_during_the_read_is_not_completed_with_the_old_answer(self):
        sess = self.session("omp")
        path = self.root / "omp-tail.jsonl"
        self.append(path, self.omp_final("first answer"))
        sess.transcript_path = path
        first_turn = sess.turn_id
        original = HubSessionManager._omp_turn_end_record

        def read_then_start_a_turn(file_path, after_offset):
            found = original(file_path, after_offset)
            # A follow-up message arrives while the sweep's read runs in its worker.
            self.hub.begin_new_turn(sess, status="running")
            return found

        with patch.object(HubSessionManager, "_omp_turn_end_record",
                          staticmethod(read_then_start_a_turn)):
            self.assertEqual(await self.hub._sweep_omp_turn_ends(), 0)
        self.assertNotEqual(sess.turn_id, first_turn)
        self.assertEqual(sess.status, "running")
        self.assertFalse(sess.final_response)
        self.assertEqual(self.events(sess.session_id, "turn_completed"), [])

    # ---- agy ------------------------------------------------------------------------------

    @staticmethod
    def agy_planner(tool_calls=None, content=""):
        return {"type": "PLANNER_RESPONSE", "content": content,
                "tool_calls": json.dumps(tool_calls or []) if tool_calls is not None else "[]"}

    async def agy_session(self, name="brain"):
        sess = self.session("agy")
        brain = self.root / name
        logs = brain / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        path = logs / "transcript.jsonl"
        path.write_text("", encoding="utf-8")
        sess.resume_attach = True
        sess.attach_offset = 0
        sess.conversation_ref = {"brain_dir": str(brain), "conversation_id": name}
        watch = self.hub._schedule(self.hub._watch_agy_transcript(sess, self.root))
        return sess, path, watch

    async def test_agy_turn_waiting_for_its_background_task_is_not_complete(self):
        sess, path, watch = await self.agy_session()
        try:
            self.append(path, {"type": "USER_INPUT", "content": "run the demo"})
            self.append(path, self.agy_planner([{"name": "run_command",
                                                 "args": {"CommandLine": "python -m demo"}}]))
            self.append(path, {"type": "GENERIC", "status": "RUNNING", "content": (
                "Created At: 2026-11-15T00:00:00+00:00\n"
                "Tool is running as a background task with task id: conversation-1/task-7\n"
                "Task Description: python -m demo")})
            self.append(path, self.agy_planner(content="I have launched task-7 and will wait for it."))
            await asyncio.sleep(0.8)

            # Ending that planner step only waits for the task's callback: not the turn's end.
            self.assertEqual(sess.status, "running")
            self.assertFalse(sess.final_response)
            self.assertEqual(self.events(sess.session_id, "turn_completed"), [])

            # The callback starts the next step; the turn is still running.
            self.append(path, {"type": "SYSTEM_MESSAGE", "content": (
                "[Message] timestamp=2026-11-15T00:00:01Z sender=conversation-1/task-7 "
                'priority=MESSAGE_PRIORITY_HIGH content=Task id "conversation-1/task-7" '
                "finished with result:\nCommand exited with code 0.")})
            self.append(path, self.agy_planner([{"name": "read_file",
                                                 "args": {"Path": "report.json"}}]))
            await asyncio.sleep(0.6)
            self.assertEqual(sess.status, "running")
            self.assertFalse(sess.final_response)

            # The turn after the callback ends: its text is the final response.
            self.append(path, self.agy_planner(content="All checks passed."))
            await self.until(lambda: sess.status == "completed")
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

        self.assertEqual(sess.final_response, "All checks passed.")
        done = self.events(sess.session_id, "turn_completed")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["content"], "All checks passed.")

    async def test_agy_turn_with_no_background_task_still_completes_at_once(self):
        sess, path, watch = await self.agy_session("brain-plain")
        try:
            self.append(path, {"type": "USER_INPUT", "content": "run the demo"})
            self.append(path, self.agy_planner([{"name": "read_file",
                                                 "args": {"Path": "report.json"}}]))
            self.append(path, self.agy_planner(content="All checks passed."))
            await self.until(lambda: sess.status == "completed")
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

        self.assertEqual(sess.final_response, "All checks passed.")
        self.assertEqual(len(self.events(sess.session_id, "turn_completed")), 1)


    async def test_agy_new_turn_drops_an_unmatched_background_task_id(self):
        sess, path, watch = await self.agy_session("brain-unmatched")
        try:
            self.append(path, {"type": "USER_INPUT", "content": "run the demo"})
            self.append(path, self.agy_planner([{"name": "run_command",
                                                 "args": {"CommandLine": "python -m demo"}}]))
            self.append(path, {"type": "GENERIC", "status": "RUNNING", "content": (
                "Tool is running as a background task with task id: conversation-1/task-9")})
            self.append(path, self.agy_planner(content="I have launched task-9."))
            await asyncio.sleep(0.6)
            self.assertEqual(self.events(sess.session_id, "turn_completed"), [])

            # No finish message ever names that id; the next turn must not inherit it.
            self.append(path, {"type": "USER_INPUT", "content": "never mind, just answer"})
            self.append(path, self.agy_planner(content="Done anyway."))
            await self.until(lambda: sess.status == "completed")
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

        self.assertEqual(sess.final_response, "Done anyway.")
        self.assertEqual(len(self.events(sess.session_id, "turn_completed")), 1)

    async def test_agy_held_turn_end_is_released_after_the_limit(self):
        sess, path, watch = await self.agy_session("brain-held")
        clock = {"offset": 0.0}
        real_time = time

        class Clock:
            def __getattr__(self, name):
                return getattr(real_time, name)

            def monotonic(self):
                return real_time.monotonic() + clock["offset"]

        try:
            with patch("session_manager.time", Clock()):
                self.append(path, {"type": "USER_INPUT", "content": "run the demo"})
                self.append(path, self.agy_planner([{"name": "run_command",
                                                     "args": {"CommandLine": "python -m demo"}}]))
                self.append(path, {"type": "GENERIC", "status": "RUNNING", "content": (
                    "Tool is running as a background task with task id: conversation-1/task-7")})
                self.append(path, self.agy_planner(content="I have launched task-7."))
                await asyncio.sleep(0.6)
                self.assertEqual(self.events(sess.session_id, "turn_completed"), [])

                # The finish never arrives; the hold limit passes on the watcher's own tick.
                clock["offset"] = 31 * 60.0
                await self.until(lambda: sess.status == "completed")
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

        self.assertEqual(sess.final_response, "I have launched task-7.")
        done = self.events(sess.session_id, "turn_completed")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["data"]["outcome"], "background_task_unreported")
        self.assertEqual(done[0]["data"]["task_ids"], ["conversation-1/task-7"])


if __name__ == "__main__":
    unittest.main()
