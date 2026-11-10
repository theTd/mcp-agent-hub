"""Parent continuation regressions, with isolated storage and no real agent processes."""
import asyncio
import contextlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx
from fastmcp import Client

import hub_poll
from parent_waiting import build_wait_contract, session_targets
from session_manager import AgentSession, HubSessionManager


class ContractTests(unittest.TestCase):
    def test_group_continues_only_unfinished_members(self):
        result = build_wait_contract({
            "a": {"status": "completed"},
            "b": {"status": "running", "from_previous_turn": True},
            "c": {"status": "crashed"},
        }, [], 17)
        self.assertEqual(result["next_action"], "inspect")
        self.assertEqual(result["completed_session_ids"], ["a"])
        self.assertEqual(result["inspect_session_ids"], ["c"])
        self.assertEqual(result["next_wait_arguments"]["session_id"], "b")
        self.assertEqual(result["next_wait_arguments"]["after_seq"], 17)

    def test_targets_reject_ambiguous_scopes(self):
        self.assertEqual(session_targets(" a,b,a "), ["a", "b"])
        self.assertIsNone(session_targets("all"))
        for value in ("", ",", "a,all"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                session_targets(value)

    def test_empty_completion_is_terminal_even_with_previous_answer(self):
        result = build_wait_contract({"a": {"status": "completed", "from_previous_turn": True}}, [], 4)
        self.assertEqual(result["next_action"], "collect_results")
        self.assertFalse(result["wait_required"])
        self.assertIsNone(result["next_wait_arguments"])


class ParentWaitingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-wait-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()
        # Import the actual MCP registrations without opening the developer's database,
        # starting discovery, launching agents or binding a network port.
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")
        self.hub_patch = patch.object(self.server, "hub", self.hub)
        self.hub_patch.start()

    async def asyncTearDown(self):
        self.hub_patch.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    def session(self, sid):
        session = AgentSession(sid, "codex", "codex", "web_gui", "Wait fixture", str(self.root), "unused", [])
        session.engine = "codex"
        session.status = "running"
        session._loop = asyncio.get_running_loop()
        self.hub.sessions[sid] = session
        return session

    async def event(self, sid, kind, content=""):
        return await self.hub.record_worker_event(sid, {"type": kind, "content": content})

    async def test_progress_question_reply_completion_closed_loop(self):
        self.session("a")
        await self.event("a", "progress", "working")
        progress = await self.server.wait_subagent_events("a", 0, 1)
        self.assertEqual(progress["next_action"], "wait")
        self.assertTrue(progress["wait_required"])
        pending = await self.hub.register_pending_interaction(
            self.hub.sessions["a"], "question", "Branch", "Which branch?", [], "native", "q1",
        )
        question = await self.server.wait_subagent_events(**progress["next_wait_arguments"])
        self.assertEqual(question["next_action"], "reply")
        self.assertEqual(question["pending_interactions"][0]["request_id"], pending["request_id"])
        # Native delivery is already covered by native-record tests; here exercise the
        # public reply tool's continuation after that delivery clears the question.
        async def delivered(**kwargs):
            session = self.hub.sessions["a"]
            session.pending_interaction = None
            session.pending_question_id = None
            session.status = "running"
            return {"delivered": True, "session_id": "a", "injection": "none"}
        with patch.object(self.hub, "deliver_parent_message", side_effect=delivered):
            reply = await self.server.reply_to_subagent("a", "main", "q1")
        self.assertEqual(reply["next_action"], "wait")
        self.assertIn("next_poll_command", reply)
        await self.event("a", "turn_completed", "done")
        final = await self.server.wait_subagent_events(**reply["next_wait_arguments"])
        self.assertEqual(final["next_action"], "collect_results")
        self.assertFalse(final["wait_required"])

    async def test_consumed_pending_event_still_wakes_immediately(self):
        session = self.session("a")
        await self.hub.register_pending_interaction(session, "question", "Branch", "Which?", [], "native", "q1")
        cursor = self.hub.global_seq
        result = await asyncio.wait_for(self.server.wait_subagent_events("a", cursor, 60), .5)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["next_action"], "reply")

    async def test_timeout_and_status_never_skip_another_sessions_events(self):
        self.session("a")
        self.session("b")
        a_event = await self.event("a", "progress", "a first")
        b_event = await self.event("b", "progress", "b first")
        timeout = await self.server.wait_subagent_events("b", b_event["seq"], .01)
        self.assertEqual(timeout["next_seq"], b_event["seq"])
        self.assertEqual(timeout["next_action"], "wait")
        # A wait on a quiet scope must not adopt a later unrelated global sequence.
        await self.event("b", "progress", "b second")
        quiet = await self.server.wait_subagent_events("a", a_event["seq"], 0)
        self.assertEqual(quiet["next_seq"], a_event["seq"])
        with patch.object(self.hub, "get_subagent_status_and_trace", return_value={"session_id": "a"}):
            status = await self.server.get_subagent_status_and_trace("a")
        self.assertEqual(status["next_wait_arguments"]["after_seq"], 0)
        replay = await self.server.wait_subagent_events(**status["next_wait_arguments"])
        self.assertEqual(replay["events"][0]["seq"], a_event["seq"])

    async def test_mixed_group_rearms_without_completed_member(self):
        self.session("a")
        self.session("b")
        await self.event("a", "turn_completed", "done a")
        result = await self.server.wait_subagent_events("a,b", 0, 1)
        self.assertEqual(result["completed_session_ids"], ["a"])
        self.assertEqual(result["next_wait_arguments"]["session_id"], "b")
        self.assertTrue(result["wait_required"])
        quiet = await self.server.wait_subagent_events(**{**result["next_wait_arguments"], "timeout_seconds": 0})
        self.assertEqual(quiet["next_action"], "wait")
        await self.event("b", "turn_completed", "done b")
        final = await self.server.wait_subagent_events(**result["next_wait_arguments"])
        self.assertFalse(final["wait_required"])

    async def test_terminal_unknown_and_previous_answer_are_distinct(self):
        session = self.session("a")
        session.final_response = "old answer"
        session.final_response_from_previous_turn = True
        running = await self.server.wait_subagent_events("a", 0, 0)
        self.assertEqual(running["next_action"], "wait")
        session.status = "completed"
        done = await asyncio.wait_for(self.server.wait_subagent_events("a", 0, 60), .5)
        self.assertEqual(done["next_action"], "collect_results")
        unknown = await self.server.wait_subagent_events("missing", 0, 0)
        self.assertEqual(unknown["inspect_session_ids"], ["missing"])
        session.status = "crashed"
        failed = await self.server.wait_subagent_events("a", 0, 0)
        self.assertEqual(failed["next_action"], "inspect")

    async def test_event_during_initial_inspection_does_not_get_lost(self):
        self.session("a")
        original = self.hub.parent_wait_state
        calls = 0
        async def inspect(*args, **kwargs):
            nonlocal calls
            result = await original(*args, **kwargs)
            calls += 1
            if calls == 1:
                await self.event("a", "progress", "arrived during snapshot")
            return result
        with patch.object(self.hub, "parent_wait_state", side_effect=inspect):
            result = await asyncio.wait_for(self.hub.poll_worker_events("a", timeout_sec=10), .5)
        self.assertEqual(result["events"][0]["content"], "arrived during snapshot")
        self.assertFalse(self.hub._event_waiters)

    async def test_wait_cancellation_cleans_listener(self):
        self.session("a")
        task = asyncio.create_task(self.server.wait_subagent_events("a", 0, 60))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.hub._event_waiters)

    async def test_wait_input_validation(self):
        for timeout in (-1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await self.server.wait_subagent_events("a", timeout_seconds=timeout)

    async def test_all_mutating_tools_return_continuation_first(self):
        session = self.session("a")
        with patch.object(self.server, "ensure_profiles_loaded"), patch.object(
            self.hub, "summarize_transcript", return_value={},
        ), patch.object(self.hub, "create_session", return_value=session), patch.object(
            self.hub, "deliver_parent_message", return_value={"delivered": True},
        ), patch.object(self.server, "_session_can_receive", return_value=True), patch.object(
            self.hub, "resume_subagent", return_value={"session": session},
        ), patch.object(self.hub, "respond_to_interaction", return_value={}):
            results = [
                await self.server.spawn_subagent("codex", wait_seconds=0),
                await self.server.send_to_subagent("a", "continue", wait_seconds=0),
                await self.server.resume_subagent("a", wait_seconds=0),
                await self.server.reply_to_subagent("a", "main"),
                await self.server.respond_to_subagent_approval_or_choice("a", decision="deny", wait_seconds=0),
            ]
        for result in results:
            self.assertEqual(next(iter(result)), "pending_interactions")
            self.assertEqual(result["next_action"], "wait")
            self.assertEqual(result["next_wait_arguments"]["session_id"], "a")
            self.assertEqual(result["background_poll_command"], result["next_poll_command"])

    async def test_mcp_schema_and_serialized_response_expose_protocol(self):
        self.session("a")
        async with Client(self.server.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            self.assertIn("next_wait_arguments", tools["wait_subagent_events"].description)
            self.assertIn("next_action", tools["reply_to_subagent"].description)
            result = await client.call_tool("wait_subagent_events", {
                "session_id": "a", "after_seq": 0, "timeout_seconds": 0,
            })
        self.assertFalse(result.is_error)
        self.assertEqual(result.data["next_action"], "wait")
        self.assertEqual(result.data["next_wait_arguments"]["session_id"], "a")

    async def test_http_adapter_and_shutdown_return_continuation(self):
        session = self.session("a")
        session.status = "completed"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
            response = await client.get("/api/mailbox/poll", params={
                "session_id": "a", "after_seq": 0, "timeout": 60, "wake_on_state": True,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["next_action"], "collect_results")
        session.status = "running"
        self.hub._shutting_down = True
        result = await asyncio.wait_for(self.server.wait_subagent_events("a", 0, 60), .5)
        self.assertEqual(result["woke_reason"], "hub_shutdown")


class PollCliTests(unittest.TestCase):
    def response(self, events=(), **changes):
        result = {
            "events": list(events), "pending_interactions": [], "next_seq": 10,
            **build_wait_contract({"a": {"status": "running"}}, [], 10),
            "next_poll_command": "poll-again", "background_poll_command": "poll-again",
        }
        result.update(changes)
        reply = types.SimpleNamespace(read=lambda: json.dumps(result).encode())
        return contextlib.nullcontext(reply)

    def test_cli_progress_is_one_batch_with_rearm_contract(self):
        with patch.object(hub_poll.urllib.request, "urlopen", return_value=self.response([
            {"seq": 11, "type": "progress", "content": "working"},
        ], next_seq=11, next_wait_arguments={"session_id": "a", "after_seq": 11, "timeout_seconds": 60})) as request:
            result = hub_poll.poll_events("a", 10, 1, "http://fixture")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["next_action"], "wait")
        self.assertEqual(result["next_wait_arguments"]["after_seq"], 11)
        self.assertIn("wake_on_state=1", request.call_args.args[0].full_url)

    def test_cli_timeout_retains_continuation_and_does_not_adopt_global_cursor(self):
        clock = types.SimpleNamespace(now=0.0)
        def request(*args, **kwargs):
            clock.now += .6
            return self.response(next_seq=999, next_wait_arguments={"session_id": "a", "after_seq": 999, "timeout_seconds": 60})
        with patch.object(hub_poll.time, "monotonic", side_effect=lambda: clock.now), patch.object(
            hub_poll.urllib.request, "urlopen", side_effect=request,
        ):
            result = hub_poll.poll_events("a", 10, 1, "http://fixture")
        self.assertEqual(result["woke_reason"], "timeout")
        self.assertEqual(result["next_seq"], 10)
        self.assertEqual(result["next_poll_command"], "poll-again")
        self.assertEqual(result["next_wait_arguments"]["after_seq"], 10)

    def test_cli_connection_and_protocol_errors_are_explicit(self):
        for failure in (ConnectionError("offline"), self.response(events=[], pending_interactions=None)):
            with self.subTest(failure=failure), patch.object(hub_poll.time, "sleep"), patch.object(
                hub_poll.urllib.request, "urlopen", side_effect=failure if isinstance(failure, Exception) else None,
                return_value=failure,
            ) as request:
                result = hub_poll.poll_events("a", 10, 1, "http://fixture")
            self.assertEqual(request.call_count, 3)
            self.assertEqual(result["woke_reason"], "poll_error")
            self.assertEqual(result["next_action"], "retry_wait")
            self.assertIn("error", result)
            self.assertEqual(result["next_wait_arguments"]["after_seq"], 10)

    def test_cli_recovers_transient_failure(self):
        with patch.object(hub_poll.time, "sleep"), patch.object(hub_poll.urllib.request, "urlopen", side_effect=[
            ConnectionError("transient"), self.response([{ "seq": 11, "content": "done" }]),
        ]):
            result = hub_poll.poll_events("a", 10, 1, "http://fixture")
        self.assertNotIn("error", result)
        self.assertEqual(result["next_seq"], 11)

    def test_invalid_response_never_acknowledges_undelivered_events(self):
        with patch.object(hub_poll.time, "sleep"), patch.object(hub_poll.urllib.request, "urlopen", return_value=self.response(
            [{"seq": 11, "content": "not delivered"}], pending_interactions=None,
        )):
            result = hub_poll.poll_events("a", 10, 1, "http://fixture")
        self.assertEqual(result["next_seq"], 10)
        self.assertEqual(result["next_wait_arguments"]["after_seq"], 10)

    def test_cli_shutdown_is_failure_and_main_returns_nonzero(self):
        with patch.object(hub_poll.urllib.request, "urlopen", return_value=self.response(woke_reason="hub_shutdown")):
            result = hub_poll.poll_events("a", 10, 1, "http://fixture")
        self.assertIn("error", result)
        output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with patch.object(hub_poll, "poll_events", return_value=result), patch("sys.argv", ["hub_poll.py"]), patch("sys.stdout", output):
            code = hub_poll.main()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
