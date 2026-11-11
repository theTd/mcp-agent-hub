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

    async def test_request_id_resolution_is_idempotent(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "approval_request", "request_id": "r1", "content": "allow?"})
        self.assertEqual((await self.inbox.wait(info["wait_handle"], 0.1))["state"], "ask")
        self.assertTrue(await self.inbox.resolve_request(info["wait_handle"], "r1"))
        self.assertFalse(await self.inbox.resolve_request(info["wait_handle"], "r1"))

    async def test_any_settled_group_can_close_on_first_completion(self):
        info = await self.inbox.create(["a", "b"], mode="any_settled")
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "A"})
        result = await self.inbox.wait(info["wait_handle"], 0.1)
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["final_response"], "A")

    async def test_rearming_handle_hides_previous_turn_result(self):
        info = await self.inbox.create(["a"])
        await self.inbox.publish_event({"session_id": "a", "type": "turn_completed", "turn_id": "ta", "content": "old"})
        self.assertEqual((await self.inbox.wait(info["wait_handle"], 0.1))["state"], "done")
        await self.inbox.update_turn(info["wait_handle"], "a", "tb")
        self.assertEqual((await self.inbox.wait(info["wait_handle"], 0.1))["state"], "waiting")

    async def test_new_waiter_supersedes_old_connection(self):
        info = await self.inbox.create(["a"])
        old = asyncio.create_task(self.inbox.wait(info["wait_handle"], 2))
        await asyncio.sleep(0.03)
        new = asyncio.create_task(self.inbox.wait(info["wait_handle"], 0.1))
        self.assertEqual((await old)["state"], "superseded")
        self.assertEqual((await new)["state"], "waiting")


if __name__ == "__main__":
    unittest.main()
