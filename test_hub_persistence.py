"""Serial, isolated regression checks. Never contacts a live hub or model service.

Run: python -m unittest -v test_hub_persistence
"""
import asyncio
import importlib.util
import json
import logging
import os
import socket
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from hub_persistence import HubPersistence, PersistenceUnavailable
from transcript_tail import TranscriptTail


def event(index):
    return {"session_id": "test", "timestamp": time.time(), "type": "message",
            "category": "message", "content": str(index), "status": "running"}


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hub-persistence-")
        self.path = Path(self.temporary.name) / "state.db"
        self.store = HubPersistence(self.path)
        self.locks = []
        await self.store.start()

    async def asyncTearDown(self):
        for conn in self.locks:
            conn.close()
        try:
            await self.store.close(timeout=0.5)
        except PersistenceUnavailable:
            pass
        self.temporary.cleanup()

    def lock(self):
        conn = sqlite3.connect(self.path, timeout=0.1)
        conn.execute("BEGIN IMMEDIATE")
        self.locks.append(conn)
        return conn

    async def test_wal_and_full_durability(self):
        def inspect():
            with self.store._backend._conn() as conn:
                return tuple(conn.execute("PRAGMA journal_mode").fetchone()), tuple(conn.execute("PRAGMA synchronous").fetchone())
        self.assertEqual(await asyncio.to_thread(inspect), (("wal",), (2,)))

    async def test_snapshots_and_aborted_units(self):
        lock = self.lock()
        original = event("original")
        async with self.store.unit(4096):
            seq = await self.store.insert_worker_event(original)
        original["content"] = "changed after acceptance"
        with self.assertRaisesRegex(ValueError, "abort"):
            async with self.store.unit(4096):
                await self.store.add_tool_call("test", 1, "aborted", "")
                raise ValueError("abort")
        lock.rollback()
        await self.store.flush()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT seq, content FROM worker_events").fetchall(), [(seq, "original")])
            self.assertEqual(conn.execute("SELECT count(*) FROM tool_calls").fetchone()[0], 0)

    async def test_five_second_lock_does_not_block_reads_or_loop(self):
        lock = self.lock()
        published = []
        started = time.monotonic()
        for index in range(40):
            async with self.store.unit(4096):
                seq = await self.store.insert_worker_event(event(index))
                self.store.on_accept(lambda seq=seq: published.append(seq))
        self.assertEqual(published, list(range(1, 41)))
        worst = 0
        while time.monotonic() - started < 5:
            before = time.monotonic()
            self.assertEqual(await self.store.get_recent_tool_calls("test", consistent=False), [])
            worst = max(worst, time.monotonic() - before)
            await asyncio.sleep(0.05)
        self.assertLess(worst, 0.5)
        self.assertEqual(self.store.status()["last_committed_seq"], 0)
        lock.rollback()
        await self.store.flush(timeout=3)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT seq FROM worker_events ORDER BY seq").fetchall(), [(n,) for n in range(1, 41)])
        self.assertEqual(len(published), 40, "committing must not publish events again")
        print(f"\n5s SQLite lock: worst read={worst*1000:.1f} ms, 40 events retained", flush=True)

    async def test_unit_capacity_backpressure_and_recovery(self):
        lock = self.lock()
        self.store.max_units = 1
        self.store.admission_timeout = 0.05
        await self.store.add_tool_call("test", 1, "first", "")
        with self.assertRaises(PersistenceUnavailable):
            await self.store.add_tool_call("test", 2, "rejected", "")
        self.assertEqual(self.store.status()["pending_writes"], 1)
        waiting = asyncio.create_task(self.store.run_internal(self.store.add_tool_call("test", 2, "second", "")))
        await asyncio.sleep(0.08)
        self.assertFalse(waiting.done(), "internal producers wait without dropping their record")
        lock.rollback()
        await asyncio.wait_for(waiting, timeout=2)
        await self.store.flush()
        self.assertEqual([x["tool_name"] for x in await self.store.get_recent_tool_calls("test")], ["first", "second"])

    async def test_byte_capacity(self):
        lock = self.lock()
        self.store.max_bytes = 1024
        self.store.admission_timeout = 0.05
        await self.store.add_tool_call("test", 1, "one", "x" * 700)
        with self.assertRaises(PersistenceUnavailable):
            await self.store.add_tool_call("test", 2, "two", "x" * 700)
        self.assertLessEqual(self.store.status()["pending_bytes"], 1024)
        lock.rollback()
        await self.store.flush()

    async def test_transaction_retry_rolls_back_partial_writes(self):
        original = self.store._backend.add_tool_call
        failed = False
        def flaky(*args, **kwargs):
            nonlocal failed
            original(*args, **kwargs)
            if args[2] == "second" and not failed:
                failed = True
                exc = sqlite3.OperationalError("synthetic lock contention")
                exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise exc
        self.store._backend.add_tool_call = flaky
        async with self.store.unit(4096):
            await self.store.add_tool_call("test", 1, "first", "")
            await self.store.add_tool_call("test", 2, "second", "")
        await self.store.flush()
        self.assertTrue(failed)
        self.assertEqual([x["tool_name"] for x in await self.store.get_recent_tool_calls("test")], ["first", "second"])

    async def test_fatal_write_is_visible_and_retained(self):
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic I/O failure")
        self.store._backend.apply_batch = fail
        with self.assertLogs("hub_persistence", level=logging.ERROR):
            await self.store.add_tool_call("test", 1, "failure", "")
            with self.assertRaises(PersistenceUnavailable):
                await self.store.flush()
        status = self.store.status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["pending_writes"], 1)
        self.assertIsNotNone(status["last_error"])
        with self.assertRaises(PersistenceUnavailable):
            await self.store.add_tool_call("test", 2, "not accepted", "")

    async def test_barrier_timeout_is_not_missing_data(self):
        lock = self.lock()
        await self.store.add_tool_call("test", 1, "pending", "")
        with self.assertRaises(PersistenceUnavailable):
            await self.store.flush(timeout=0.05)
        self.assertEqual(await self.store.get_recent_tool_calls("test", consistent=False), [])
        lock.rollback()
        await self.store.flush()
        self.assertEqual(len(await self.store.get_recent_tool_calls("test")), 1)

    async def test_close_flushes_and_sequence_continues_after_restart(self):
        async with self.store.unit(4096):
            first = await self.store.insert_worker_event(event("first"))
        await self.store.close()
        self.store = HubPersistence(self.path)
        await self.store.start()
        async with self.store.unit(4096):
            second = await self.store.insert_worker_event(event("second"))
        await self.store.close()
        self.assertEqual(second, first + 1)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM worker_events").fetchone()[0], 2)

    async def test_close_is_bounded_under_permanent_lock(self):
        lock = self.lock()
        await self.store.add_tool_call("test", 1, "pending", "")
        started = time.monotonic()
        with self.assertLogs("hub_persistence", level=logging.ERROR):
            with self.assertRaises(PersistenceUnavailable):
                await self.store.close(timeout=0.4)
        self.assertLess(time.monotonic() - started, 0.55)
        self.assertEqual(self.store.status()["pending_writes"], 1)
        lock.rollback()

    async def test_tail_acknowledges_only_complete_accepted_lines(self):
        path = Path(self.temporary.name) / "tail.jsonl"
        path.write_bytes(b'{"kind":"step"}\n' * 70 + b'{"kind":')
        tail = TranscriptTail(path)
        first = await tail.read_batch()
        self.assertEqual(len(first), 64)
        self.assertEqual(await tail.read_batch(), first, "rejected lines must remain available")
        tail.acknowledge(first[-1][0])
        rest = await tail.read_batch()
        self.assertEqual(len(rest), 6)
        tail.acknowledge(rest[-1][0])
        self.assertEqual(await tail.read_batch(), [])
        with path.open("ab") as stream:
            stream.write(b'"text"}\n')
        self.assertEqual(json.loads((await tail.read_batch())[0][1]), {"kind": "text"})


class _TestPty:
    pid = None
    exitstatus = 0
    def __init__(self):
        self.alive = True
        self.inputs = []
    def isalive(self):
        return self.alive
    def write(self, text):
        self.inputs.append(text)
    def setwinsize(self, rows, cols):
        pass
    def terminate(self, force=False):
        self.alive = False


class HubIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Match the production loop; debug traceback capture distorts socket latency.
        asyncio.get_running_loop().set_debug(False)
        import httpx
        import uvicorn
        import session_manager
        self.temporary = tempfile.TemporaryDirectory(prefix="hub-isolated-api-")
        self.directory = Path(self.temporary.name)
        profiles = self.directory / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        settings = {"HUB_DATA_DIR": str(self.directory), "HUB_PROFILES_PATH": str(profiles)}
        spec = importlib.util.spec_from_file_location("isolated_hub_server", Path(__file__).with_name("server.py"))
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, settings), patch.object(session_manager.HubSessionManager, "_initial_discovery", lambda manager: manager._discovery_ready.set()):
            spec.loader.exec_module(self.module)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.module.app, log_level="error", access_log=False, lifespan="on"))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        deadline = time.monotonic() + 5
        while not self.server.started:
            if self.task.done():
                await self.task
            if time.monotonic() >= deadline:
                raise RuntimeError("isolated hub startup timed out")
            await asyncio.sleep(0.01)
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=3)
        self.session = session_manager.AgentSession(
            session_id="test-tui", profile_id="pwsh", profile_name="Test terminal", kind="tui",
            title="Isolated terminal", cwd=str(self.directory), command="test", args=[],
            on_exit_cb=self.module.hub._exit_callback,
        )
        self.session.engine = "codex"
        self.session.pty = _TestPty()
        self.session._loop = asyncio.get_running_loop()
        self.session.status = "running"
        self.module.hub.sessions[self.session.session_id] = self.session
        await self.module.hub.persist_session(self.session)
        await self.module.hub.db.flush()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.should_exit = True
        await asyncio.wait_for(self.task, timeout=10)
        self.socket.close()
        self.temporary.cleanup()

    async def connect_terminal(self):
        from websockets.asyncio.client import connect
        websocket = await connect(f"ws://127.0.0.1:{self.port}/ws/pty/test-tui", proxy=None)
        while True:
            frame = await asyncio.wait_for(websocket.recv(), timeout=1)
            if frame == '{"type": "replay_end"}':
                return websocket

    async def test_worker_interfaces_are_removed(self):
        for path, method in [
            ("/worker-mcp", "post"),
            ("/api/telemetry/test-tui", "post"),
            ("/api/mailbox/test-tui/worker-event", "post"),
            ("/api/mailbox/test-tui/worker-ask", "post"),
            ("/api/mailbox/test-tui/parent-messages", "get"),
        ]:
            response = await getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 404, path)
        profiles = await self.module.list_agent_profiles()
        self.assertNotIn("worker_mcp_http_url", profiles)
        self.assertNotIn("worker_cli_script", profiles)

    async def test_http_and_terminal_during_five_second_write_lock(self):
        lock = sqlite3.connect(self.directory / "hub_state.db", timeout=0.1)
        lock.execute("BEGIN IMMEDIATE")
        websocket = await self.connect_terminal()
        try:
            start = time.monotonic()
            await self.module.hub.record_telemetry("test-tui", {"source": "codex_jsonl", "hook_event": "PreToolUse", "tool_name": "test_tool", "tool_input": "locked"})
            self.assertLess(time.monotonic() - start, 0.5, "telemetry acknowledges admission, not commit")
            worst = 0
            probes = 0
            while time.monotonic() - start < 5:
                before = time.monotonic()
                health = await self.client.get("/health")
                sessions = await self.client.get("/api/sessions")
                pong = await websocket.ping()
                await asyncio.wait_for(pong, timeout=0.5)
                self.assertEqual(health.status_code, 200)
                self.assertEqual(sessions.status_code, 200)
                self.assertEqual(len(sessions.json()["sessions"]), 1)
                worst = max(worst, time.monotonic() - before)
                probes += 1
                await asyncio.sleep(0.1)
            self.assertLess(worst, 0.5)
            await websocket.send(json.dumps({"type": "input", "data": "terminal-input-probe"}))
            deadline = time.monotonic() + 0.5
            while not self.session.pty.inputs and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            self.assertIn("terminal-input-probe", self.session.pty.inputs)
            print(f"\nHTTP + WebSocket under 5s SQLite lock: {probes} rounds, worst={worst*1000:.1f} ms", flush=True)
        finally:
            lock.rollback()
            lock.close()
            await websocket.close()
        await self.module.hub.db.flush(timeout=3)
        tools = await self.module.hub.db.get_recent_tool_calls("test-tui")
        self.assertEqual([row["tool_name"] for row in tools], ["test_tool"])

    async def test_dsh_thousand_line_backlog_remains_interactive(self):
        import session_manager
        job_id = "synthetic-job"
        @self.module.app.get(f"/_test_dsh/jobs/{job_id}/messages")
        async def messages():
            return {"result": {"messages": [], "next_cursor": 0}}
        @self.module.app.get(f"/_test_dsh/jobs/{job_id}")
        async def job():
            return {"job": {"status": "done", "result": "synthetic final", "toolCalls": 1000}}
        log = self.directory / "synthetic-job.jsonl"
        log.write_text("".join(json.dumps({"kind": "tool_call", "tool": f"tool_{i}", "summary": "test"}) + "\n" for i in range(1000)), encoding="utf-8")
        session = session_manager.AgentSession(
            session_id="test-dsh", profile_id="dsh", profile_name="Test DSH", kind="virtual",
            title="Synthetic backlog", cwd=str(self.directory), command="", args=[],
        )
        session.engine = "dsh"
        session.status = "running"
        session.dsh_job_id = job_id
        session._loop = asyncio.get_running_loop()
        self.module.hub.sessions[session.session_id] = session
        websocket = await self.connect_terminal()
        with patch.object(session_manager, "dsh_url", lambda path: f"http://127.0.0.1:{self.port}" + path.replace("/_dsh/dsh-crew", "/_test_dsh")):
            worker = self.module.hub._schedule(self.module.hub._watch_dsh_hub_job(session, job_id, "synthetic-token", log_path=log))
            start = time.monotonic()
            worst = 0
            rounds = 0
            try:
                while not worker.done():
                    self.assertLess(time.monotonic() - start, 60, "backlog failed to drain")
                    before = time.monotonic()
                    response = await self.client.get("/api/sessions")
                    pong = await websocket.ping()
                    await asyncio.wait_for(pong, timeout=0.5)
                    self.assertEqual(response.status_code, 200)
                    worst = max(worst, time.monotonic() - before)
                    rounds += 1
                    await asyncio.sleep(0.01)
                await worker
            finally:
                await websocket.close()
        await self.module.hub.db.flush(timeout=3)
        self.assertEqual(session.step_count, 1000)
        self.assertEqual(session.final_response, "synthetic final")
        self.assertLess(worst, 0.5)
        with closing(sqlite3.connect(self.directory / "hub_state.db")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tool_calls WHERE session_id='test-dsh'").fetchone()[0], 1000)
        events = [row for row in self.module.hub.worker_events if row["session_id"] == "test-dsh" and row["type"] == "turn_completed"]
        self.assertEqual(len(events), 1)
        print(f"\n1000 DSH log lines: {rounds} HTTP/WebSocket rounds, worst={worst*1000:.1f} ms", flush=True)

    async def test_scrollback_marks_commit_not_admission(self):
        lock = sqlite3.connect(self.directory / "hub_state.db", timeout=0.1)
        lock.execute("BEGIN IMMEDIATE")
        self.session._on_pty_output("persist-after-commit\r\n")
        old_rev = self.session._scrollback_rev_written
        await self.module.hub.persist_session(self.session, force_scrollback=True)
        self.assertEqual(self.session._scrollback_rev_written, old_rev)
        self.assertNotEqual(self.session.raw_history_rev, old_rev)
        lock.rollback()
        lock.close()
        await self.module.hub.db.flush()
        self.assertEqual(self.session._scrollback_rev_written, self.session.raw_history_rev)
        self.assertGreater(self.session._scrollback_written_at, 0)

    async def test_overload_returns_503_without_publishing_or_changing_state(self):
        store = self.module.hub.db
        lock = sqlite3.connect(self.directory / "hub_state.db", timeout=0.1)
        lock.execute("BEGIN IMMEDIATE")
        try:
            store.max_units = 1
            await store.add_tool_call("test-tui", 1, "capacity-filler", "")
            before = time.monotonic()
            response = await self.client.post("/api/mailbox/test-tui/reply", json={"text": "must not publish", "also_write_pty": False})
            elapsed = time.monotonic() - before
            self.assertEqual(response.status_code, 503, response.text)
            self.assertGreaterEqual(elapsed, 1.9)
            self.assertLess(elapsed, 2.5)
            self.assertEqual(self.module.hub.global_seq, 0)
            self.assertEqual(self.module.hub.worker_events, [])
            self.assertEqual(self.session.status, "running")
            self.assertIsNone(self.session.final_response)
        finally:
            lock.rollback()
            lock.close()
        await store.flush()
        response = await self.client.post("/api/mailbox/test-tui/reply", json={"text": "recovered", "also_write_pty": False})
        self.assertEqual(response.status_code, 200)
        await store.flush()
        self.assertEqual([row["content"] for row in self.module.hub.parent_messages["test-tui"]], ["recovered"])

    async def test_dsh_backpressure_keeps_unaccepted_offset(self):
        import session_manager
        job_id = "backpressure-job"
        @self.module.app.get(f"/_test_dsh/jobs/{job_id}/messages")
        async def messages():
            return {"result": {"messages": []}}
        @self.module.app.get(f"/_test_dsh/jobs/{job_id}")
        async def job():
            return {"job": {"status": "done", "result": "accepted after capacity"}}
        log = self.directory / "backpressure.jsonl"
        log.write_text('{"kind":"tool_call","tool":"retained"}\n', encoding="utf-8")
        observed = []
        read = asyncio.Event()
        class ObservedTail(TranscriptTail):
            async def read_batch(tail):
                observed.append(tail)
                result = await super().read_batch()
                read.set()
                return result
        store = self.module.hub.db
        store.max_units = 1
        lock = sqlite3.connect(self.directory / "hub_state.db", timeout=0.1)
        lock.execute("BEGIN IMMEDIATE")
        self.session.dsh_job_id = job_id
        with patch.object(session_manager, "TranscriptTail", ObservedTail), patch.object(session_manager, "dsh_url", lambda path: f"http://127.0.0.1:{self.port}" + path.replace("/_dsh/dsh-crew", "/_test_dsh")):
            worker = self.module.hub._schedule(self.module.hub._watch_dsh_hub_job(self.session, job_id, "fake", log_path=log))
            try:
                await asyncio.wait_for(read.wait(), timeout=2)
                await asyncio.sleep(0.05)
                self.assertFalse(worker.done())
                self.assertEqual(observed[0].offset, 0)
                self.assertEqual(self.session.step_count, 0)
                lock.rollback()
                await asyncio.wait_for(worker, timeout=5)
            finally:
                lock.close()
        await store.flush()
        self.assertEqual(observed[0].offset, log.stat().st_size)
        self.assertEqual(self.session.step_count, 1)

    async def test_failed_database_keeps_health_responsive(self):
        def fail(*args):
            raise sqlite3.OperationalError("synthetic disk failure")
        store = self.module.hub.db
        store._backend.apply_batch = fail
        with self.assertLogs("hub_persistence", level=logging.ERROR):
            await store.add_tool_call("test-tui", 1, "retained", "")
            with self.assertRaises(PersistenceUnavailable):
                await store.flush()
        before = time.monotonic()
        response = await self.client.get("/health")
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["persistence"]["state"], "failed")
        self.assertEqual(response.json()["persistence"]["pending_writes"], 1)
        with self.assertLogs(level=logging.ERROR) as logs:
            await self.module.hub.archive_all_sessions_on_shutdown()
        self.assertIn("uncommitted writes", "\n".join(logs.output))

    async def test_archive_preserves_tool_pair_reply_priority_and_resume_state(self):
        hub = self.module.hub
        await hub.record_telemetry("test-tui", {"hook_event": "PreToolUse", "tool_name": "paired", "tool_input": "input"})
        await hub.record_telemetry("test-tui", {"hook_event": "PostToolUse", "tool_name": "paired", "tool_response": "output"})
        await hub.record_telemetry("test-tui", {"source": "codex_jsonl", "hook_event": "TurnComplete", "final_assistant_text": "authoritative final"})
        await hub.record_worker_event("test-tui", {"type": "turn_completed", "content": "lower priority"})
        await hub._archive_session(self.session, end_reason="exited")
        await hub.persist_session(self.session)  # Empty archive fields must not erase the final row.
        record = await hub.db.get_session_record("test-tui")
        self.assertEqual(record["final_response"], "authoritative final")
        self.assertTrue(record["archived_at"])
        self.assertTrue(record["parent_messages"])
        page = await hub.list_archive()
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["sessions"][0]["session_id"], "test-tui")
        tools = await hub.db.get_recent_tool_calls("test-tui")
        self.assertEqual(tools[0]["output_summary"], "output")
        self.assertEqual(tools[0]["status"], "completed")
        await hub.db.reset_session_after_resume("test-tui")
        resumed = await hub.db.get_session_record("test-tui")
        self.assertEqual(resumed["final_response"], "authoritative final")
        self.assertIsNone(resumed["archived_at"])
        self.assertEqual(resumed["status"], "starting")

    async def test_shutdown_is_idempotent_and_archives_final_state(self):
        self.session.set_final_response("durable final", "native_transcript")
        await self.module.hub.record_worker_event("test-tui", {"type": "turn_completed", "content": "durable final"})
        counts = await asyncio.gather(self.module.hub.archive_all_sessions_on_shutdown(), self.module.hub.archive_all_sessions_on_shutdown())
        self.assertEqual(counts, [1, 1])
        with closing(sqlite3.connect(self.directory / "hub_state.db")) as conn:
            row = conn.execute("SELECT final_response, archived_at, status FROM sessions WHERE session_id='test-tui'").fetchone()
            self.assertEqual(row[0], "durable final")
            self.assertIsNotNone(row[1])
            self.assertEqual(row[2], "interrupted")
            self.assertEqual(conn.execute("SELECT count(*) FROM worker_events").fetchone()[0], 1)

    async def test_shutdown_respects_eight_second_budget_under_lock(self):
        hub = self.module.hub
        lock = sqlite3.connect(self.directory / "hub_state.db", timeout=0.1)
        lock.execute("BEGIN IMMEDIATE")
        await hub.record_worker_event("test-tui", {"type": "message", "content": "retained under lock"})
        started = time.monotonic()
        try:
            with self.assertLogs(level=logging.ERROR) as logs:
                await hub.archive_all_sessions_on_shutdown()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 8.3)
            self.assertIn("uncommitted writes", "\n".join(logs.output))
            self.assertGreater(hub.db.status()["pending_writes"], 0)
            print(f"\nShutdown under permanent write lock: {elapsed:.2f} s; pending count logged", flush=True)
        finally:
            lock.rollback()
            lock.close()

    async def test_parent_mailbox_admission_and_durable_reply(self):
        hub = self.module.hub
        await hub.register_pending_interaction(self.session, "question", "Native question", "question", [], "codex_jsonl_request_user_input", request_id="native-q")
        request_id = self.session.pending_question_id
        await hub.db.flush()
        hub.db.max_units = 1
        hub.db.admission_timeout = 0.05
        lock = sqlite3.connect(self.directory / "hub_state.db", timeout=0.1)
        lock.execute("BEGIN IMMEDIATE")
        try:
            await hub.db.add_tool_call("test-tui", 1, "filler", "")
            with self.assertRaises(PersistenceUnavailable):
                await hub.deliver_parent_message("test-tui", "rejected answer", request_id=request_id)
            self.assertIsNotNone(self.session.pending_interaction)
            self.assertFalse(hub.pending_questions[request_id]["answered"])
            self.assertEqual(hub.parent_messages.get("test-tui", []), [])
            lock.rollback()
            await hub.db.flush()
            await hub.deliver_parent_message("test-tui", "accepted answer", request_id=request_id)
            self.assertIsNone(self.session.pending_interaction)
            record = await hub.db.get_session_record("test-tui")
            self.assertEqual([m["content"] for m in record["parent_messages"] if m["direction"] == "parent"], ["accepted answer"])
        finally:
            lock.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
