"""A session stopped mid-turn reports how far it got, never an early line as its final answer."""
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fake_terminal_core import FakeTerminal
from session_manager import HubSessionManager
from transcript_summarizer import OMPTranscriptSummarizer


def _omp_message(parts):
    return {"type": "message", "message": {"role": "assistant", "content": parts}}


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class OmpConclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="omp-conclusion-")
        self.path = Path(self.tmp.name) / "t.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def summarize(self, rows, meta=None):
        _write_jsonl(self.path, rows)
        return OMPTranscriptSummarizer().summarize(self.path, meta or {"status": "terminated"}, [], [])

    def test_an_early_line_followed_by_tool_calls_is_not_a_conclusion(self):
        call = {"type": "toolCall", "name": "read", "arguments": {}}
        rows = [_omp_message([{"type": "text", "text": "I'll start by reading the brief."}, call])]
        rows += [_omp_message([{"type": "thinking", "thinking": f"step {i}"}, call]) for i in range(5)]
        summary = self.summarize(rows)
        self.assertIsNone(summary["latest_conclusion"])
        self.assertEqual(summary["last_assistant_text"], "I'll start by reading the brief.")
        self.assertEqual(summary["assistant_messages_after_last_text"], 5)

    def test_a_text_only_last_message_is_the_conclusion(self):
        call = {"type": "toolCall", "name": "read", "arguments": {}}
        rows = [_omp_message([{"type": "text", "text": "Reading."}, call]),
                _omp_message([{"type": "text", "text": "Done: fixed it."}])]
        summary = self.summarize(rows)
        self.assertEqual(summary["latest_conclusion"], "Done: fixed it.")
        self.assertEqual(summary["assistant_messages_after_last_text"], 0)

    def test_text_beside_a_tool_call_in_the_newest_message_is_still_the_fallback(self):
        call = {"type": "toolCall", "name": "read", "arguments": {}}
        rows = [_omp_message([call]), _omp_message([{"type": "text", "text": "Now checking the log."}, call])]
        self.assertEqual(self.summarize(rows)["latest_conclusion"], "Now checking the log.")


class InterruptedTerminateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A hub of the test's own, patched into server (as test_spawn_wiring does): the module-level
        # hub outlives this test, so pointing it at this temp folder would let later tests recreate it.
        self.tmp = tempfile.TemporaryDirectory(prefix="interrupted-terminate-")
        root = Path(self.tmp.name)
        profiles = root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=root)
        self.hub.core = FakeTerminal()
        await self.hub.start()
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")
        self.hub_patch = patch.object(self.server, "hub", self.hub)
        self.hub_patch.start()

    async def asyncTearDown(self):
        self.hub_patch.stop()
        self.hub.sessions = {}
        await self.hub.archive_all_sessions_on_shutdown()
        self.tmp.cleanup()

    def _session(self, status, final_response=None, previous=False):
        return SimpleNamespace(
            session_id="omp-1", status=status, current_activity="Editing parser.py",
            final_response=final_response, final_response_from_previous_turn=previous,
            step_count=101, files_changed=["a.py", "b.py"], cwd=self.tmp.name,
            to_dict=lambda: {"session_id": "omp-1", "status": status, "cwd": self.tmp.name},
        )

    def _patches(self, summary):
        hub = self.server.hub
        return (
            patch.object(hub, "summarize_transcript", new=AsyncMock(return_value=summary)),
            patch.object(hub, "close_session", new=AsyncMock(return_value={"was_alive": True, "status": "terminated"})),
            patch.object(hub, "get_subagent_final_response", new=AsyncMock(side_effect=AssertionError("not a final answer"))),
        )

    async def test_mid_turn_terminate_reports_progress_not_a_final_answer(self):
        self.server.hub.sessions = {"omp-1": self._session("running")}
        summary = {"step_count": 90, "latest_conclusion": None,
                   "last_assistant_text": "I'll start by reading the brief.",
                   "assistant_messages_after_last_text": 100,
                   "recent_actions": [f"`edit`({i})" for i in range(12)], "key_thoughts": ["t1", "t2"]}
        p1, p2, p3 = self._patches(summary)
        order = []
        with p1 as summarize, p2 as close, p3:
            summarize.side_effect = lambda sid: order.append("summarize") or summary
            close.side_effect = lambda sid, source: order.append("close") or {"was_alive": True, "status": "terminated"}
            result = await self.server.terminate_subagent("omp-1")
        # The summary is read while the session still counts as running, then the session closes.
        self.assertEqual(order, ["summarize", "close"])
        self.assertTrue(result["interrupted"])
        self.assertIsNone(result["final_response"])
        progress = result["progress"]
        self.assertEqual(progress["last_assistant_text"], "I'll start by reading the brief.")
        self.assertEqual(progress["assistant_messages_after_last_text"], 100)
        self.assertEqual(progress["current_activity"], "Editing parser.py")
        self.assertEqual(progress["step_count"], 101)
        self.assertEqual(len(progress["recent_actions"]), 8)
        self.assertEqual(progress["last_thought"], "t2")
        self.assertEqual(progress["files_changed"], ["a.py", "b.py"])
        self.assertIn(self.tmp.name, result["git_status"])

    async def test_previous_turn_answer_is_named_as_such(self):
        self.server.hub.sessions = {"omp-1": self._session("running", "turn 1 answer", previous=True)}
        summary = {"latest_conclusion": "turn 1 answer", "recent_actions": [], "key_thoughts": []}
        p1, p2, p3 = self._patches(summary)
        with p1, p2, p3:
            result = await self.server.terminate_subagent("omp-1")
        self.assertIsNone(result["progress"]["last_assistant_text"])
        self.assertEqual(result["progress"]["previous_turn_answer"], "turn 1 answer")

    async def test_a_completed_session_still_returns_its_final_response(self):
        self.server.hub.sessions = {"omp-1": self._session("completed", "the answer")}
        hub = self.server.hub
        final = {"final_response": "the answer", "final_response_source": "native", "step_count": 3}
        with patch.object(hub, "close_session", new=AsyncMock(return_value={"was_alive": True, "status": "completed"})), \
             patch.object(hub, "get_subagent_final_response", new=AsyncMock(return_value=final)):
            result = await self.server.terminate_subagent("omp-1")
        self.assertFalse(result["interrupted"])
        self.assertEqual(result["final_response"], "the answer")

    def _racing_session(self):
        session = self._session("running")
        session.to_dict = lambda: {"session_id": "omp-1", "status": session.status, "cwd": self.tmp.name}
        return session

    @staticmethod
    def _answer(session, text="the answer"):
        session.status, session.final_response, session.final_response_from_previous_turn = "completed", text, False

    async def test_a_turn_that_ends_during_the_summary_read_returns_its_answer(self):
        session = self._racing_session()
        self.server.hub.sessions = {"omp-1": session}
        hub = self.server.hub
        final = {"final_response": "the answer", "final_response_source": "native", "step_count": 3}
        summarize = AsyncMock(side_effect=lambda sid: self._answer(session) or {"recent_actions": []})
        close = AsyncMock(return_value={"was_alive": True, "status": "terminated"})
        with patch.object(hub, "summarize_transcript", new=summarize),              patch.object(hub, "close_session", new=close),              patch.object(hub, "get_subagent_final_response", new=AsyncMock(return_value=final)):
            result = await self.server.terminate_subagent("omp-1")
        self.assertFalse(result["interrupted"])
        self.assertEqual(result["final_response"], "the answer")
        close.assert_awaited_once()

    async def test_an_answer_that_lands_during_the_close_is_returned_not_hidden(self):
        session = self._racing_session()
        self.server.hub.sessions = {"omp-1": session}
        hub = self.server.hub
        final = {"final_response": "late answer", "final_response_source": "native", "step_count": 3}

        async def close_as_the_answer_lands(sid, source):
            self._answer(session, "late answer")
            session.status = "terminated"
            return {"was_alive": True, "status": "terminated"}

        close = AsyncMock(side_effect=close_as_the_answer_lands)
        with patch.object(hub, "summarize_transcript", new=AsyncMock(return_value={"recent_actions": []})),              patch.object(hub, "close_session", new=close),              patch.object(hub, "get_subagent_final_response", new=AsyncMock(return_value=final)):
            result = await self.server.terminate_subagent("omp-1")
        self.assertFalse(result["interrupted"])
        self.assertEqual(result["final_response"], "late answer")
        close.assert_awaited_once()

    async def test_collect_returns_the_answer_of_a_turn_that_ended_while_it_was_stopped(self):
        session = self._racing_session()
        self.server.hub.sessions = {"omp-1": session}
        hub = self.server.hub
        final = {"final_response": "the answer", "final_response_source": "native", "step_count": 3}
        summarize = AsyncMock(side_effect=lambda sid: self._answer(session) or {"recent_actions": []})
        close = AsyncMock(return_value={"was_alive": True, "status": "terminated"})
        with patch.object(hub, "summarize_transcript", new=summarize),              patch.object(hub, "close_session", new=close),              patch.object(hub, "get_subagent_final_response", new=AsyncMock(return_value=final)):
            result = await self.server.collect_and_terminate(session_ids=["omp-1"], only_completed=False)
        (entry,) = result["collected"]
        self.assertNotIn("interrupted", entry)
        self.assertEqual(entry["final_response"], "the answer")
        close.assert_awaited_once()

    async def test_collect_and_terminate_labels_a_running_session_interrupted(self):
        self.server.hub.sessions = {"omp-1": self._session("running")}
        summary = {"last_assistant_text": "x", "recent_actions": [], "key_thoughts": []}
        p1, p2, p3 = self._patches(summary)
        with p1, p2, p3:
            result = await self.server.collect_and_terminate(session_ids=["omp-1"], only_completed=False)
        (entry,) = result["collected"]
        self.assertTrue(entry["interrupted"])
        self.assertEqual(entry["final_response"], "")
        self.assertEqual(entry["progress"]["last_assistant_text"], "x")


if __name__ == "__main__":
    unittest.main()
