"""DSH sessions resumed after a hub restart: mailbox replay and jobs that ended while it was down."""
import asyncio
import datetime
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from dsh_mailbox_replay import DshMailboxReplay
from session_manager import AgentSession, HubSessionManager, _dsh_end_unrecorded

READY = {"allowed": True, "status": "ready", "reason": ""}


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class MailboxReplayTests(unittest.TestCase):
    def test_handled_entries_are_skipped_and_the_pending_question_comes_back(self):
        replay = DshMailboxReplay.from_history(
            {"requests": ["q-old", "q-pending"], "texts": ["Worker   note\n"]}, still_pending={"q-pending"})
        self.assertTrue(replay.already_handled("q-old", "Old question?", True))
        self.assertFalse(replay.already_handled("q-pending", "Still waiting?", True))
        self.assertFalse(replay.already_handled("q-new", "New question?", True))
        self.assertTrue(replay.already_handled("m2", "Worker note", False))
        self.assertFalse(replay.already_handled("m3", "Another note", False))
        self.assertFalse(replay.already_handled("m4", "", False))

    def test_end_unrecorded(self):
        ended = time.time() - 60
        job = {"endedAt": iso(ended)}
        self.assertTrue(_dsh_end_unrecorded(job, None))
        self.assertTrue(_dsh_end_unrecorded(job, ended - 600))
        self.assertFalse(_dsh_end_unrecorded(job, ended + 2))
        self.assertFalse(_dsh_end_unrecorded({}, ended - 600))
        self.assertFalse(_dsh_end_unrecorded({}, None))
        self.assertFalse(_dsh_end_unrecorded({"endedAt": "not a date"}, ended - 600))


class DshRestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-restart-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()
        self.sess = AgentSession("dsh-restart", "dsh", "DSH", "tui", "Test", str(self.root), "", [])
        self.sess.engine = "dsh"
        self.sess.pty = None
        self.sess.dsh_spec = {"tier": "flash"}
        self.sess.dsh_job_id = "old-job"
        self.sess.status = "running"
        self.sess.profile_snapshot = {"engine": "dsh", "cwd": str(self.root), "dsh_spec": self.sess.dsh_spec}
        self.sess._loop = asyncio.get_running_loop()
        self.hub.sessions[self.sess.session_id] = self.sess
        self.prep = patch("session_manager.prepare_dsh_workspace", return_value=dict(READY))
        self.prep.start()

    async def asyncTearDown(self):
        self.prep.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def until(self, condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            await asyncio.sleep(0.02)

    def events(self, kind=None):
        return [e for e in self.hub.get_worker_events(self.sess.session_id) if kind is None or e["type"] == kind]

    async def test_history_lists_what_the_hub_handled(self):
        await self.hub.register_pending_interaction(self.sess, "question", "Q", "Which?", ["A"], "test", request_id="q1")
        await self.hub.record_worker_event(self.sess.session_id, {"type": "message_to_parent", "content": "note"})
        await self.hub.record_worker_event(self.sess.session_id, {"type": "turn_completed", "content": ""})
        history = await self.hub.db.read("get_mailbox_history", self.sess.session_id)
        self.assertIn("q1", history["requests"])
        self.assertIn("note", history["texts"])
        self.assertIsNotNone(history["last_turn_completed_at"])

    async def test_reattached_watcher_skips_the_handled_part_of_the_mailbox(self):
        mailbox = [
            {"kind": "message", "sequence": 1, "message_id": "q-old", "text": "Old question?", "expects_reply": True},
            {"kind": "message", "sequence": 2, "message_id": "m2", "text": "Worker note", "expects_reply": False},
            {"kind": "message", "sequence": 3, "message_id": "q-pending", "text": "Still waiting?", "expects_reply": True},
        ]
        job = {"status": "running", "result": ""}

        class FakeDsh:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, params=None, **kwargs):
                await asyncio.sleep(0.01)
                if url.endswith("/messages"):
                    after = int((params or {}).get("after") or 0)
                    return httpx.Response(200, json={"result": {"messages": [m for m in mailbox if m["sequence"] > after]}})
                return httpx.Response(200, json={"job": dict(job)})

        replay = DshMailboxReplay.from_history(
            {"requests": ["q-old", "q-pending"], "texts": ["Worker note"]}, still_pending={"q-pending"})
        log = self.root / "job.log"
        log.touch()
        with patch("session_manager.httpx.AsyncClient", return_value=FakeDsh()):
            task = self.hub._schedule(self.hub._watch_dsh_hub_job(
                self.sess, "old-job", "token", log_path=log, reattach=True, replay=replay))
            await self.until(lambda: self.sess.pending_interaction is not None)
            self.assertEqual(self.sess.pending_interaction["request_id"], "q-pending")
            # After the replay the same text is a new message and reaches the parent.
            mailbox.append({"kind": "message", "sequence": 4, "message_id": "m4", "text": "Worker note", "expects_reply": False})
            await self.until(lambda: self.events("message_to_parent"))
            job.update(status="done", result="finished")
            await asyncio.wait_for(task, timeout=5)
        self.assertFalse([e for e in self.events() if e.get("request_id") == "q-old"])
        self.assertEqual(len(self.events("message_to_parent")), 1)
        self.assertEqual(self.sess.final_response, "finished")

    def record(self, **extra):
        return dict({"session_id": self.sess.session_id, "cwd": str(self.root),
                     "profile_snapshot": self.sess.profile_snapshot,
                     "conversation_ref": {"dsh_job_id": "old-job", "dsh_token": "token"}}, **extra)

    async def resume(self, job, record=None):
        response = httpx.Response(200, json={"ok": True, "job": dict({"id": "old-job"}, **job)})
        watch = AsyncMock()
        with patch.object(self.hub, "build_resume_launch", return_value={"cwd": str(self.root)}), \
                patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response), \
                patch.object(self.hub, "_watch_dsh_hub_job", watch):
            result = await self.hub._resume_dsh_session(self.sess.session_id, record or self.record(), None)
            await asyncio.sleep(0)
        return result["session"], watch

    async def test_job_that_ended_while_the_hub_was_down_is_read_once(self):
        sess, watch = await self.resume({"status": "done", "endedAt": iso(time.time() - 30)})
        self.assertEqual(sess.status, "running")
        watch.assert_called_once()
        self.assertTrue(watch.call_args.kwargs["reattach"])
        self.assertIsInstance(watch.call_args.kwargs["replay"], DshMailboxReplay)

    async def test_job_whose_end_was_recorded_is_not_read_again(self):
        ended = time.time() - 30
        await self.hub.record_worker_event(self.sess.session_id, {"type": "turn_completed", "content": ""})
        sess, watch = await self.resume({"status": "done", "endedAt": iso(ended - 60)})
        self.assertEqual(sess.status, "completed")
        watch.assert_not_called()

    async def test_running_job_keeps_the_question_pending_at_the_restart(self):
        await self.hub.register_pending_interaction(self.sess, "question", "Q", "Old?", ["A"], "test", request_id="q-old")
        await self.hub.clear_pending_interaction(self.sess, "q-old")
        await self.hub.register_pending_interaction(self.sess, "question", "Q", "Now?", ["A"], "test", request_id="q-now")
        record = self.record(pending_interaction={"request_id": "q-now"})
        sess, watch = await self.resume({"status": "running"}, record)
        replay = watch.call_args.kwargs["replay"]
        self.assertTrue(replay.already_handled("q-old", "", True))
        self.assertFalse(replay.already_handled("q-now", "", True))
        self.assertEqual(sess.status, "running")


if __name__ == "__main__":
    unittest.main()
