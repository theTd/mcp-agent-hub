"""spawned_by: the hub records which parent spawned a session; list_active_sessions shows it and filters mine."""
import importlib
import os
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mcp_caller_identity as identity
from fake_terminal_core import FakeTerminal
from session_manager import HubSessionManager

EXTERNAL = {"pid": 4242, "process": "claude.exe", "hub_session_id": None}
ORCHESTRATOR = {"pid": 77, "process": "codex.exe", "hub_session_id": "codex-orch", "hub_session_title": "Lead"}


def _v4_row(local_port, remote_port, pid):
    port = lambda p: socket.htons(p)  # noqa: E731
    return struct.pack("<IIIIII", 5, 0x0100007F, port(local_port), 0x0100007F, port(remote_port), pid)


class CallerIdentityTests(unittest.TestCase):
    def test_the_row_with_the_peer_as_local_end_names_the_caller(self):
        hub_side = _v4_row(4444, 50001, 1)  # the hub's own end of the same connection
        caller_side = _v4_row(50001, 4444, 999)
        raw = struct.pack("<I", 2) + hub_side + caller_side
        self.assertEqual(identity.find_connection_owner(raw, identity.AF_INET, 50001, 4444), 999)
        self.assertIsNone(identity.find_connection_owner(raw, identity.AF_INET, 50002, 4444))

    def test_ancestor_walk_finds_the_session_and_stops_on_a_cycle(self):
        processes = {30: (20, "node.exe"), 20: (10, "omp.exe"), 10: (1, "explorer.exe"), 5: (6, "a"), 6: (5, "b")}
        self.assertEqual(identity.owning_session(30, processes, {20: "omp-1"}), "omp-1")
        self.assertIsNone(identity.owning_session(30, processes, {}))
        self.assertIsNone(identity.owning_session(5, processes, {}))

    def test_same_caller(self):
        self.assertTrue(identity.same_caller(ORCHESTRATOR, dict(ORCHESTRATOR, pid=78)))
        self.assertFalse(identity.same_caller(ORCHESTRATOR, EXTERNAL))
        self.assertTrue(identity.same_caller(EXTERNAL, dict(EXTERNAL)))
        self.assertFalse(identity.same_caller(EXTERNAL, dict(EXTERNAL, pid=4243)))
        self.assertFalse(identity.same_caller(EXTERNAL, None))

    @unittest.skipUnless(sys.platform == "win32", "Windows tables")
    def test_a_real_loopback_connection_resolves_to_this_process(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname())
        conn, peer = server.accept()
        try:
            caller = identity.resolve_caller(peer[0], peer[1], server.getsockname()[1], {os.getppid(): "parent"})
        finally:
            for sock in (conn, client, server):
                sock.close()
        self.assertEqual(caller["pid"], os.getpid())
        self.assertEqual(caller["hub_session_id"], "parent")

    def test_a_non_loopback_peer_is_unknown(self):
        self.assertIsNone(identity.peer_pid("10.0.0.5", 50001, 4444))


class SpawnedByListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A hub of the test's own, patched into server (as test_spawn_wiring does), so the long-lived
        # module-level hub never points at this temp folder.
        self.tmp = tempfile.TemporaryDirectory(prefix="spawned-by-")
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

    def _fake(self, sid, spawned_by):
        raw = {"session_id": sid, "title": sid, "profile_id": "omp", "status": "running", "cwd": self.tmp.name}
        return SimpleNamespace(to_dict=lambda: dict(raw), effective_model="m",
                               profile_snapshot={"spawned_by": spawned_by})

    async def test_entries_name_the_spawner_and_mine_filters(self):
        self.server.hub.sessions = {
            "a": self._fake("a", ORCHESTRATOR), "b": self._fake("b", EXTERNAL), "c": self._fake("c", None),
        }
        listed = await self.server.list_active_sessions()
        by_id = {e["session_id"]: e["spawned_by"] for e in listed["active_sessions"]}
        self.assertEqual(by_id, {"a": {"session_id": "codex-orch", "title": "Lead"},
                                 "b": {"process": "claude.exe", "pid": 4242}, "c": None})
        with patch.object(self.server, "_mcp_caller", return_value=dict(EXTERNAL)):
            mine = await self.server.list_active_sessions(mine=True)
        self.assertEqual([e["session_id"] for e in mine["active_sessions"]], ["b"])
        with patch.object(self.server, "_mcp_caller", return_value=None):
            with self.assertRaises(ValueError):
                await self.server.list_active_sessions(mine=True)

    async def test_stored_rows_name_the_spawner_and_mine_filters_both_views(self):
        import json
        from unittest.mock import AsyncMock
        from hub_database import HubDatabase
        db = HubDatabase(Path(self.tmp.name) / "stored.db")
        for sid, caller in (("old-ext", EXTERNAL), ("old-orch", ORCHESTRATOR), ("old-none", None)):
            db.upsert_session(
                {"session_id": sid, "profile_id": "omp", "profile_name": "OMP", "kind": "worker",
                 "title": sid, "cwd": self.tmp.name, "status": "completed", "created_at": 1,
                 "step_count": 1, "current_activity": "", "engine": "omp", "effective_model": "m"},
                archive={"profile_snapshot": json.dumps({"spawned_by": caller}) if caller else ""})

        async def read_rows(**kwargs):
            kwargs.pop("consistent", None)
            return db.list_persisted_sessions(**kwargs)

        self.server.hub.sessions = {}
        with patch.object(self.server.hub.db, "list_persisted_sessions", new=AsyncMock(side_effect=read_rows)):
            listed = await self.server.list_active_sessions(include_historical=True)
            with patch.object(self.server, "_mcp_caller", return_value=dict(EXTERNAL)):
                mine = await self.server.list_active_sessions(include_historical=True, mine=True)
                full = await self.server.list_active_sessions(include_historical=True, mine=True, view="full")
        by_id = {e["session_id"]: e["spawned_by"] for e in listed["historical_sessions"]}
        self.assertEqual(by_id, {"old-ext": {"process": "claude.exe", "pid": 4242},
                                 "old-orch": {"session_id": "codex-orch", "title": "Lead"}, "old-none": None})
        self.assertEqual([e["session_id"] for e in mine["historical_sessions"]], ["old-ext"])
        self.assertEqual([e["session_id"] for e in full["persisted_sessions"]], ["old-ext"])

    async def test_caller_lookup_uses_the_request_peer_and_names_the_session_title(self):
        orchestrator = SimpleNamespace(pid=77, title="Lead")
        self.server.hub.sessions = {"codex-orch": orchestrator}
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1", port=50001),
                                  scope={"server": ("127.0.0.1", 4444)})
        seen = {}

        def fake_resolve(host, port, server_port, session_pids):
            seen.update(host=host, port=port, server_port=server_port, pids=session_pids)
            return {"pid": 90, "process": "codex.exe", "hub_session_id": "codex-orch"}

        with patch.object(self.server, "get_http_request", return_value=request), \
             patch.object(self.server, "resolve_caller", side_effect=fake_resolve):
            caller = await self.server._mcp_caller()
        self.assertEqual(seen, {"host": "127.0.0.1", "port": 50001, "server_port": 4444, "pids": {77: "codex-orch"}})
        self.assertEqual(caller["hub_session_title"], "Lead")

    async def test_no_http_request_means_unknown(self):
        self.assertIsNone(await self.server._mcp_caller())


if __name__ == "__main__":
    unittest.main()
