import asyncio
import tempfile
import unittest
from pathlib import Path

from wait_inbox import WaitInbox


class WaitInboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wait-inbox-")
        self.inbox = WaitInbox(Path(self.temp.name) / "wait.db")
        self.states = {"a": {"status": "running", "final_response": None}, "b": {"status": "running", "final_response": None}}

        async def provider(ids):
            values = [self.states[sid] for sid in ids]
            if any(v["status"] == "waiting_parent" for v in values):
                return {"state": "ask", "requests": [{"request_id": "ask-1", "kind": "question"}], "result_id": "ask-1"}
            if all(v["status"] == "completed" for v in values):
                return {"state": "done", "result_id": "group-result", "final_response": values[0]["final_response"], "results": values}
            return {"state": "waiting"}
        self.inbox.set_state_provider(provider)

    async def asyncTearDown(self):
        await self.inbox.close()
        self.temp.cleanup()

    async def test_timeout_reuses_same_url_without_cursor(self):
        info = await self.inbox.create(["a"])
        first = await self.inbox.wait(info["wait_handle"], 0.1)
        second = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual(first["state"], "waiting")
        self.assertEqual(second["state"], "waiting")
        self.assertNotIn("cursor", info["wait_url"])
        self.assertNotIn("session_id", info["wait_url"])

    async def test_only_wake_events_enter_parent_inbox(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "progress", "content": "noise"})
        waiter = asyncio.create_task(self.inbox.wait(info["wait_handle"], 0.2))
        await asyncio.sleep(0.03)
        self.assertFalse(waiter.done())
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q1", "content": "Which?"})
        result = await waiter
        self.assertEqual(result["state"], "ask")
        self.assertEqual(result["requests"][0]["request_id"], "q1")

    async def test_result_is_stable_and_group_wait_is_all_settled(self):
        info = await self.inbox.create(["a", "b"])
        self.states["a"] = {"status": "completed", "final_response": "A"}
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "A"})
        # The first member must not close an all-settled group or prevent the
        # second completion from being persisted after a restart.
        self.assertEqual(self.inbox._row(info["wait_handle"])["state"], "waiting")
        self.assertEqual((await self.inbox.wait(info["wait_handle"], 0.1))["state"], "waiting")
        self.states["b"] = {"status": "completed", "final_response": "B"}
        await self.inbox.publish_event({"session_id": "b", "type": "turn_completed", "turn_id": "tb", "content": "B"})
        done = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["result_id"], "group-result")
        self.assertEqual(len(done["results"]), 2)
        again = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual(again["result_id"], done["result_id"])

    async def test_intermediate_group_completion_does_not_hide_a_later_approval(self):
        info = await self.inbox.create(["a", "b"])
        self.states["a"] = {"status": "completed", "final_response": "A"}
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "A"})
        await self.inbox.publish_event({"session_id": "b", "type": "approval_request", "request_id": "r1", "content": "allow?"})

        result = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual((result["state"], result["requests"][0]["request_id"]), ("ask", "r1"))

    async def test_open_approval_precedes_group_done_snapshot(self):
        info = await self.inbox.create(["a", "b"])
        self.states["a"] = {"status": "completed", "final_response": "A"}
        self.states["b"] = {"status": "completed", "final_response": "B"}
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "A"})
        await self.inbox.publish_event({"session_id": "b", "type": "approval_request", "request_id": "r1", "content": "allow?"})

        result = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual((result["state"], result["requests"][0]["request_id"]), ("ask", "r1"))

    async def test_multiple_intermediate_completions_are_drained_before_undelivered(self):
        self.states["c"] = {"status": "running", "final_response": None}
        info = await self.inbox.create(["a", "b", "c"])
        self.states["a"] = {"status": "completed", "final_response": "A"}
        self.states["b"] = {"status": "completed", "final_response": "B"}
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "A"})
        await self.inbox.publish_event({"session_id": "b", "type": "turn_completed", "turn_id": "tb", "content": "B"})
        await self.inbox.publish_event({"session_id": "c", "type": "delivery_failed", "seq": "inj-1",
                                        "result_id": "undelivered:c:inj-1",
                                        "data": {"message_id": "inj-1", "reason": "write_failed"}})

        result = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual(result["state"], "undelivered")
        self.assertEqual(result["messages"][0]["message_id"], "inj-1")

    async def test_open_ask_repeats_until_answered_by_any_path(self):
        open_requests = {"r1"}
        self.inbox.set_request_check(lambda sid, rid: sid == "a" and rid in open_requests)
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "approval_request", "request_id": "r1", "content": "allow?"})
        for _ in range(2):
            result = await self.inbox.wait(info["wait_handle"], 0.1)
            self.assertEqual((result["state"], result["requests"][0]["request_id"]), ("ask", "r1"))
        open_requests.clear()  # answered outside respond_to_subagent, e.g. send_to_subagent or the console
        self.assertEqual((await self.inbox.wait(info["wait_handle"], 0.1))["state"], "waiting")
        self.assertEqual(self.inbox._row(info["wait_handle"])["state"], "waiting")

    async def test_answered_ask_never_hides_the_next_ask_or_the_result(self):
        # 2026-11-12 regression: an approval answered through send_to_subagent stayed first in the
        # inbox, so the URL repeated it and never showed the second approval or the turn's end.
        open_requests = {"r1"}
        self.inbox.set_request_check(lambda sid, rid: rid in open_requests)
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "approval_request", "request_id": "r1", "content": "rm?"})
        self.assertEqual((await self.inbox.wait(info["wait_handle"], 0.1))["requests"][0]["request_id"], "r1")
        open_requests.discard("r1")
        open_requests.add("r2")
        await self.inbox.publish_event({"session_id": "a", "type": "approval_request", "request_id": "r2", "content": "status?"})
        result = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual((result["state"], result["requests"][0]["request_id"]), ("ask", "r2"))
        open_requests.discard("r2")
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "A"})
        done = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual((done["state"], done["final_response"]), ("done", "A"))

    async def test_any_settled_group_can_close_on_first_completion(self):
        info = await self.inbox.create(["a", "b"], mode="any_settled")
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "A"})
        result = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["final_response"], "A")

    async def test_undelivered_messages_wake_together_and_leave_the_handle_waiting(self):
        handle = (await self.inbox.create(["a"]))["wait_handle"]
        for mid in ("inj-1", "inj-2"):
            await self.inbox.publish_event({"session_id": "a", "type": "delivery_failed", "seq": mid,
                                            "result_id": f"undelivered:a:{mid}",
                                            "data": {"message_id": mid, "reason": "write_failed",
                                                     "confirmed_failure": True, "recovery_hint": "send it again."}})
        woke = await self.inbox.wait(handle, 0.1)
        self.assertEqual(woke["state"], "undelivered")
        self.assertEqual([m["message_id"] for m in woke["messages"]], ["inj-1", "inj-2"])
        self.assertEqual(woke["messages"][0]["recovery_hint"], "send it again.")
        self.assertEqual(self.inbox._row(handle)["state"], "waiting")
        self.assertEqual((await self.inbox.wait(handle, 0.1))["state"], "waiting")
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "A"})
        self.assertEqual((await self.inbox.wait(handle, 0.1))["state"], "done")

    async def test_rearming_handle_hides_previous_turn_result(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "old"})
        self.assertEqual((await self.inbox.wait(info["wait_handle"], 0.1))["state"], "done")
        await self.inbox.update_turn(info["wait_handle"], "a", "tb")
        self.assertEqual((await self.inbox.wait(info["wait_handle"], 0.1))["state"], "waiting")

    async def test_rearming_drops_only_this_sessions_uncollected_earlier_result(self):
        # A parent that denies a codex approval and sends new instructions at once never reads the
        # abort's turn end; the new message must not be answered with it.
        handle = (await self.inbox.create(["a", "b"], mode="any_settled"))["wait_handle"]
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "aborted"})
        await self.inbox.publish_event({"session_id": "b", "type": "turn_completed", "turn_id": "tb", "content": "B"})
        await self.inbox.update_turn(handle, "a", "ta2")
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta2", "content": "new"})
        await self.inbox.update_turn(handle, "a", "ta2")  # a steer within the same turn keeps its result
        self.assertEqual((await self.inbox.wait(handle, 0.1))["final_response"], "B")  # another member's: kept
        self.assertEqual((await self.inbox.wait(handle, 0.1))["final_response"], "new")

    async def test_rearming_a_session_reaches_every_open_handle_that_waits_on_it(self):
        # A turn can begin with no server call (omp retrying after the hub ended the turn), so the
        # handle re-arm must find the handles itself instead of being handed one per request.
        single = (await self.inbox.create(["a"]))["wait_handle"]
        group = (await self.inbox.create(["a", "b"]))["wait_handle"]
        other = (await self.inbox.create(["b"]))["wait_handle"]
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta",
                                        "content": "false engine error"})
        self.inbox.rearm_session_turn("a", "ta2")
        self.assertEqual(self.inbox._row(single)["turn_ids"], {"a": "ta2"})
        self.assertEqual(self.inbox._row(group)["turn_ids"], {"a": "ta2"})
        self.assertEqual(self.inbox._row(other)["turn_ids"], {})
        self.assertEqual((await self.inbox.wait(single, 0.1))["state"], "waiting")
        self.states["a"] = {"status": "completed", "final_response": "real answer"}
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta2",
                                        "content": "real answer"})
        self.assertEqual((await self.inbox.wait(single, 0.1))["final_response"], "real answer")
        self.states["b"] = {"status": "completed", "final_response": "B"}
        await self.inbox.publish_event({"session_id": "b", "type": "turn_completed", "turn_id": "tb",
                                        "content": "B"})
        done = await self.inbox.wait(group, 0.1)
        self.assertEqual(done["state"], "done")
        self.assertEqual([r["final_response"] for r in done["results"]], ["real answer", "B"])
        self.assertEqual((await self.inbox.wait(other, 0.1))["final_response"], "B")

    async def test_a_rearm_during_the_provider_await_is_not_overwritten_by_a_stale_done(self):
        # omp's retry re-arms the handle while a poll's state provider is still awaiting; the
        # provider then answers with the replaced turn's "done", which must neither be written
        # nor returned: the parent would otherwise be answered with the false engine error.
        info = await self.inbox.create(["a", "b"])
        self.states["a"] = {"status": "completed", "final_response": "false engine error"}
        self.states["b"] = {"status": "completed", "final_response": "B"}
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_provider(ids):
            entered.set()
            await release.wait()
            values = [self.states[sid] for sid in ids]
            return {"state": "done", "result_id": "stale-result",
                    "final_response": values[0]["final_response"], "results": values}

        self.inbox.set_state_provider(slow_provider)
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed",
                                        "turn_id": "ta", "content": "false engine error"})
        snapshot = asyncio.create_task(self.inbox._snapshot(info["wait_handle"]))
        await asyncio.wait_for(entered.wait(), 2)
        self.inbox.rearm_session_turn("a", "ta2")
        release.set()
        result = await asyncio.wait_for(snapshot, 2)
        self.assertEqual(result["state"], "waiting")
        row = self.inbox._row(info["wait_handle"])
        self.assertEqual((row["state"], row["result"]), ("waiting", None))
        self.assertEqual(row["turn_ids"], {"a": "ta2"})
        # The new turn still settles with its own completion, never with the stale one.
        self.states["a"] = {"status": "completed", "final_response": "real answer"}
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed",
                                        "turn_id": "ta2", "content": "real answer"})
        done = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual((done["state"], done["final_response"]), ("done", "real answer"))

    async def test_new_waiter_supersedes_old_connection(self):
        info = await self.inbox.create(["a"])
        old = asyncio.create_task(self.inbox.wait(info["wait_handle"], 2))
        await asyncio.sleep(0.03)
        new = asyncio.create_task(self.inbox.wait(info["wait_handle"], 0.1))
        self.assertEqual((await old)["state"], "superseded")
        self.assertEqual((await new)["state"], "waiting")

    async def test_ask_coalescing_collects_only_requests_in_window(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q1", "content": "first"})
        waiter = asyncio.create_task(self.inbox.wait(info["wait_handle"], 0.4, ask_coalesce_ms=60))
        await asyncio.sleep(0.02)
        await self.inbox.publish_event({"session_id": "a", "type": "approval_request", "request_id": "q2", "content": "second"})
        result = await waiter
        self.assertEqual([r["request_id"] for r in result["requests"]], ["q1", "q2"])
        self.assertEqual(result["result_id"], "q1")
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q3", "content": "later"})
        again = await self.inbox.wait(info["wait_handle"], 0.1, ask_coalesce_ms=0)
        self.assertEqual(again["requests"][0]["request_id"], "q1")

    async def test_ask_answered_during_coalescing_is_refreshed(self):
        info = await self.inbox.create(["a"])
        open_requests = {"q1"}
        self.inbox.set_request_check(lambda _sid, request_id: request_id in open_requests)
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q1", "content": "first"})
        waiter = asyncio.create_task(self.inbox.wait(info["wait_handle"], 0.2, ask_coalesce_ms=80))
        await asyncio.sleep(0.02)
        open_requests.clear()
        await self.inbox._notify(info["wait_handle"])
        result = await waiter
        self.assertEqual(result["state"], "waiting")

    async def test_open_ask_is_returned_when_the_window_outlasts_the_timeout(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q1", "content": "first"})
        result = await self.inbox.wait(info["wait_handle"], 0.1, ask_coalesce_ms=800)
        self.assertEqual(result["state"], "ask")
        self.assertEqual([r["request_id"] for r in result["requests"]], ["q1"])

    async def test_multiple_notifies_during_coalescing_are_safe(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q1", "content": "first"})
        waiter = asyncio.create_task(self.inbox.wait(info["wait_handle"], 0.3, ask_coalesce_ms=80))
        await asyncio.sleep(0.02)
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q2", "content": "second"})
        result = await waiter
        self.assertEqual(result["state"], "ask")
        self.assertEqual([x["result_id"] for x in result["requests"]], ["q1", "q2"])

    async def test_new_waiter_during_coalescing_supersedes_old(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q1", "content": "first"})
        old = asyncio.create_task(self.inbox.wait(info["wait_handle"], 0.3, ask_coalesce_ms=100))
        await asyncio.sleep(0.02)
        new = asyncio.create_task(self.inbox.wait(info["wait_handle"], 0.2, ask_coalesce_ms=0))
        self.assertEqual((await old)["state"], "superseded")
        self.assertEqual((await new)["state"], "ask")

    async def test_close_during_coalescing_supersedes_waiter(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q1", "content": "first"})
        waiter = asyncio.create_task(self.inbox.wait(info["wait_handle"], 0.3, ask_coalesce_ms=100))
        await asyncio.sleep(0.02)
        await self.inbox.close()
        self.assertEqual((await waiter)["state"], "superseded")

    async def test_each_open_ask_has_its_request_result_id(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "question", "request_id": "q1", "content": "first"})
        await self.inbox.publish_event({"session_id": "a", "type": "approval_request", "request_id": "q2", "content": "second"})
        result = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual([(x["request_id"], x["result_id"]) for x in result["requests"]], [("q1", "q1"), ("q2", "q2")])


if __name__ == "__main__":
    unittest.main()
