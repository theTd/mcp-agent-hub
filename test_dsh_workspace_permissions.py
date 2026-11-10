"""DSH ACL preparation and dispatch gates, without model or shell calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from dsh_workspace_permissions import (
    WRITE_OWNER, _aces, _acl_with_entries, _owner_ace, _save_record, prepare_dsh_workspace,
)

SID = b"\x01\x01\x00\x00\x00\x00\x00\x05" + struct.pack("<I", 21)
ACL = struct.pack("<BBHHH", 2, 0, 8, 0, 0)
READY = {"status": "ready", "allowed": True, "reason": "ready", "missing_permissions": []}
BLOCKED = {"status": "blocked", "allowed": False, "reason": "ACL blocked", "missing_permissions": ["WRITE_OWNER"]}


class FakeAcl:
    def __init__(self):
        self.identity = ("host-user", SID)
        self.owner = "host-user"
        self.dac = True
        self.owner_write = False
        self.denied = False
        self.acl = ACL
        self.writes = []
        self.fail_verify = False
        self.fail_rollback = False
        self.extra = None
        self.mutex = threading.Lock()

    def host_identity(self, endpoint):
        return self.identity, "identity unavailable"

    @contextmanager
    def lock(self, path):
        with self.mutex:
            yield

    @contextmanager
    def workspace(self, path):
        yield 123

    def inspect(self, handle, path):
        owner_right = self.owner_write or _owner_ace(SID) in _aces(self.acl)
        return {"owner_sid": self.owner, "acl": self.acl, "explicit_deny": self.denied,
                "rights": {"WRITE_DAC": self.dac, "WRITE_OWNER": owner_right and not self.fail_verify}}

    def write_acl(self, handle, path, acl):
        if self.fail_rollback and self.writes:
            raise OSError("rollback denied")
        self.writes.append(acl)
        self.acl = acl
        if self.extra and len(self.writes) == 1:
            self.acl = _acl_with_entries(acl, _aces(acl) + [self.extra])


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-acl-unit-")
        self.root = Path(self.temp.name)
        self.backend = FakeAcl()

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **kw):
        return prepare_dsh_workspace(str(self.root / "workspace"), "http://localhost:3080", self.root,
                                     backend=self.backend, **kw)

    def test_ready_and_preview_make_no_writes(self):
        preview = self.prepare(repair=False)
        self.assertEqual(preview["status"], "would_repair")
        self.assertEqual(preview["missing_permissions"], ["WRITE_OWNER"])
        self.assertFalse((self.root / "diagnostics").exists())
        self.backend.owner_write = True
        self.assertEqual(self.prepare()["status"], "ready")
        self.assertFalse(self.backend.writes)

    def test_root_only_grant_journal_and_idempotence(self):
        result = self.prepare()
        self.assertEqual(result["status"], "repaired")
        entries = _aces(self.backend.acl)
        self.assertEqual(entries, [_owner_ace(SID)])
        self.assertEqual(entries[0][1], 0)  # no inheritance flags
        self.assertEqual(struct.unpack_from("<I", entries[0], 4)[0], WRITE_OWNER)
        record = json.loads((self.root / "diagnostics" / (result["diagnostic_id"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "repaired")
        self.assertIn("before_dacl", record)
        self.assertEqual(self.prepare()["status"], "ready")
        self.assertEqual(len(self.backend.writes), 1)

    def test_unknown_host_and_different_host_account_skip(self):
        self.backend.identity = None
        for endpoint in ("http://remote:3080", "http://localhost:3080"):
            result = prepare_dsh_workspace(str(self.root), endpoint, self.root, backend=self.backend)
            self.assertEqual(result["status"], "skipped")
            self.assertTrue(result["allowed"])
        self.assertFalse(self.backend.writes)

    def test_owner_denies_missing_dac_and_invalid_acl_block(self):
        cases = (("owner", "other-user"), ("denied", True), ("dac", False), ("acl", b"bad"))
        for key, value in cases:
            with self.subTest(key=key):
                self.backend = FakeAcl()
                setattr(self.backend, key, value)
                result = self.prepare()
                self.assertFalse(result["allowed"])
                self.assertFalse(self.backend.writes)

    def test_disk_home_system_and_reparse_paths_block(self):
        for path in (Path(self.root.anchor), Path.home()):
            result = prepare_dsh_workspace(str(path), "http://localhost:3080", self.root, backend=self.backend)
            self.assertFalse(result["allowed"])
        with patch.dict("os.environ", {"SystemRoot": str(self.root / "windows")}):
            result = prepare_dsh_workspace(str(self.root / "windows" / "child"), "http://localhost:3080", self.root, backend=self.backend)
            self.assertFalse(result["allowed"])
        @contextmanager
        def reparse(path):
            raise ValueError("reparse point")
            yield
        self.backend.workspace = reparse
        self.assertFalse(self.prepare()["allowed"])
        self.assertFalse(self.backend.writes)

    def test_verification_failure_removes_only_added_ace(self):
        extra = struct.pack("<BBHI", 0, 0, 8 + len(SID), 2) + SID
        self.backend.extra = extra
        self.backend.fail_verify = True
        result = self.prepare()
        self.assertFalse(result["allowed"])
        self.assertIn("confirmed", result["reason"])
        self.assertEqual(_aces(self.backend.acl), [extra])

    def test_rollback_failure_is_visible(self):
        self.backend.fail_verify = self.backend.fail_rollback = True
        result = self.prepare()
        self.assertFalse(result["allowed"])
        self.assertIn("rollback denied", result["reason"])
        record = json.loads(next((self.root / "diagnostics").glob("*.json")).read_text(encoding="utf-8"))
        self.assertTrue(record["rollback"].startswith("failed"))

    def test_backup_failure_does_not_change_permissions(self):
        with patch("dsh_workspace_permissions._save_record", side_effect=OSError("disk full")):
            result = self.prepare()
        self.assertFalse(result["allowed"])
        self.assertFalse(self.backend.writes)

    def test_failed_journal_update_keeps_durable_recovery_record(self):
        journal = self.root / "recovery.json"
        _save_record(journal, {"status": "prepared", "before_dacl": "backup"}, create=True)
        with patch("dsh_workspace_permissions.os.replace", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            _save_record(journal, {"status": "repaired"})
        self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["before_dacl"], "backup")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_concurrent_preparations_add_one_ace(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.prepare(), range(8)))
        self.assertEqual(sum(r["status"] == "repaired" for r in results), 1)
        self.assertTrue(all(r["allowed"] for r in results))
        self.assertEqual(len(self.backend.writes), 1)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from session_manager import AgentSession, HubSessionManager
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-gate-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()
        self.sess = AgentSession("dsh-gate", "dsh", "DSH", "tui", "Test", str(self.root), "", [])
        self.sess.engine = "dsh"
        self.sess.dsh_spec = {"tier": "flash"}
        self.sess.profile_snapshot = {"engine": "dsh", "cwd": str(self.root), "dsh_spec": self.sess.dsh_spec}
        self.sess._loop = asyncio.get_running_loop()
        self.hub.sessions[self.sess.session_id] = self.sess
        self.prep = patch("session_manager.prepare_dsh_workspace", return_value=copy.deepcopy(BLOCKED))
        self.prepare = self.prep.start()

    async def asyncTearDown(self):
        self.prep.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def test_spawn_blocks_http_and_wakes_parent(self):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            await self.hub._spawn_dsh_hub_job(self.sess, "task")
        post.assert_not_awaited()
        self.assertEqual(self.sess.status, "exited")
        self.assertEqual(self.sess.sandbox_preparation["status"], "blocked")
        self.assertTrue(any(event["type"] == "error" for event in self.hub.get_worker_events(self.sess.session_id)))
        await self.hub.db.flush()
        record = await self.hub.db.get_session_record(self.sess.session_id)
        self.assertEqual(record["profile_snapshot"]["sandbox_preparation"]["status"], "blocked")
        trace = await self.hub.get_subagent_status_and_trace(self.sess.session_id)
        self.assertEqual(trace["sandbox_preparation"]["status"], "blocked")

    async def test_empty_start_is_check_only(self):
        await self.hub._spawn_dsh_hub_job(self.sess, None)
        self.assertFalse(self.prepare.call_args.kwargs["repair"])

    async def test_ready_spawn_dispatches_once_and_persists_preparation(self):
        import httpx
        self.prepare.return_value = copy.deepcopy(READY)
        response = httpx.Response(200, json={"ok": True, "job": {"id": "new-job", "sessionId": "native-id"}})
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response) as post, \
                patch.object(self.hub, "_watch_dsh_hub_job", new_callable=AsyncMock):
            await self.hub._spawn_dsh_hub_job(self.sess, "task")
        self.assertEqual(post.await_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["cwd"], str(self.root))
        self.assertEqual(self.sess.dsh_job_id, "new-job")
        self.assertEqual(self.sess.to_dict()["sandbox_preparation"]["status"], "ready")

    async def test_completed_send_blocks_and_preserves_answer(self):
        self.sess.status = "completed"
        self.sess.dsh_job_id = "old-job"
        self.sess.final_response = "old answer"
        with patch("httpx.post") as post:
            result = await self.hub.deliver_parent_message(self.sess.session_id, "continue")
        post.assert_not_called()
        self.assertFalse(result["delivered"])
        self.assertEqual(self.sess.final_response, "old answer")
        self.assertEqual(self.sess.status, "completed")

    async def test_interaction_continue_blocks_and_keeps_request(self):
        self.sess.status = "waiting_parent"
        self.sess.dsh_job_id = "old-job"
        self.sess.pending_interaction = {"kind": "question", "request_id": "q1", "prompt": "Which?",
                                         "options": ["Go"], "metadata": {"dsh_needs_continue": True}}
        with patch("httpx.post") as post, self.assertRaisesRegex(ValueError, "ACL blocked"):
            await self.hub.respond_to_interaction(self.sess.session_id, option_index=1, request_id="q1")
        post.assert_not_called()
        self.assertEqual(self.sess.pending_interaction["request_id"], "q1")

    async def test_resume_failure_does_not_replace_record(self):
        record = {"cwd": str(self.root), "profile_snapshot": self.sess.profile_snapshot,
                  "conversation_ref": {"dsh_job_id": "old-job"}}
        original = copy.deepcopy(record)
        with patch.object(self.hub, "build_resume_launch", return_value={"cwd": str(self.root)}), \
                patch.object(self.hub, "_make_resumed_session", new_callable=AsyncMock) as make, \
                patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            with self.assertRaisesRegex(ValueError, "ACL blocked"):
                await self.hub._resume_dsh_session(self.sess.session_id, record, "continue")
        make.assert_not_awaited()
        post.assert_not_awaited()
        self.assertEqual(record, original)

    async def test_attach_only_checks_even_if_repair_would_be_blocked(self):
        import httpx
        record = {"cwd": str(self.root), "profile_snapshot": self.sess.profile_snapshot,
                  "conversation_ref": {"dsh_job_id": "old-job"}, "final_response": "previous"}
        response = httpx.Response(200, json={"ok": True, "job": {"id": "old-job", "status": "done"}})
        with patch.object(self.hub, "build_resume_launch", return_value={"cwd": str(self.root)}), \
                patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response), \
                patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            result = await self.hub._resume_dsh_session(self.sess.session_id, record, None)
        self.assertFalse(result["delivered"])
        self.assertEqual(result["session"].status, "completed")
        self.assertFalse(self.prepare.call_args.kwargs["repair"])
        post.assert_not_awaited()

    async def test_preview_returns_check_without_mutation(self):
        import importlib
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            server = importlib.import_module("server")
        import httpx
        with patch.object(server, "hub", self.hub), \
                patch.object(server, "ensure_profiles_loaded", new_callable=AsyncMock), \
                patch.object(self.hub, "resolve_launch", return_value={"engine": "dsh", "cwd": str(self.root)}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
                response = await client.post("/api/launch-preview", json={"profile_id": "dsh"})
        self.assertEqual(response.json()["sandbox_preparation"]["status"], "blocked")
        self.assertFalse(self.prepare.call_args.kwargs["repair"])

    async def test_running_message_does_not_reprepare(self):
        self.sess.status = "running"
        self.sess.dsh_job_id = "running-job"
        with patch("httpx.post") as post:
            post.return_value.is_success = True
            result = await self.hub.deliver_parent_message(self.sess.session_id, "steer")
        self.assertTrue(result["delivered"])
        self.prepare.assert_not_called()
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
