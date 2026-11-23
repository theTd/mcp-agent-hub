"""rtc-purge: the dropped engine's data goes at hub start (decision `dsh-purge-at-start`).

DSH rows in every per-session table and a saved `dsh` profile are removed once, after a backup
that must check out; nothing is deleted when the backup fails, and a second start does nothing.
`StartHookTests` proves the manager runs the purge before restart-resume reads a row.

Run: python -m unittest -v test_retired_engine_purge
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import session_manager
from hub_database import HubDatabase
from restart_resume_request import REQUEST_NAME
from retired_engine_purge import (
    _copy_database,
    purge_retired_engine_data,
    purge_retired_engine_profiles,
    purge_retired_engine_rows,
)
from session_manager import HubSessionManager

STAMP = "20260101-000000"


def _seed_database(path: Path, sessions, *, per_table=()):
    """Schema through the hub's own class, rows through SQL so each column stays visible."""
    HubDatabase(path)
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            for session_id, engine, snapshot, status in sessions:
                conn.execute(
                    "INSERT INTO sessions (session_id, engine, profile_snapshot, status, end_reason, "
                    "archived_at, created_at, updated_at, parent_messages) VALUES (?,?,?,?,?,?,?,?,?)",
                    (session_id, engine, snapshot, status, "exited", 1.0, 1.0, 1.0,
                     json.dumps([{"from": "dsh-a", "text": "hello"}])),
                )
            for session_id in per_table:
                conn.execute(
                    "INSERT INTO tool_calls (session_id, step_num, timestamp, tool_name, status) "
                    "VALUES (?,?,?,?,?)", (session_id, 1, 1.0, "bash", "ok"))
                conn.execute(
                    "INSERT INTO intermediate_messages (session_id, step_num, timestamp, content, source) "
                    "VALUES (?,?,?,?,?)", (session_id, 1, 1.0, "step", "agent"))
                conn.execute(
                    "INSERT INTO worker_events (session_id, timestamp, type, content) VALUES (?,?,?,?)",
                    (session_id, 1.0, "note", f"{session_id} did something"))
            # A row of another session that only mentions a retired one must stay.
            conn.execute(
                "INSERT INTO worker_events (session_id, timestamp, type, content) VALUES (?,?,?,?)",
                ("omp-x", 2.0, "child_finished", "dsh-a finished"))
    finally:
        conn.close()


def _ids(path: Path, table: str):
    conn = sqlite3.connect(str(path))
    try:
        return sorted(str(r[0]) for r in conn.execute(f"SELECT session_id FROM {table}").fetchall())
    finally:
        conn.close()


def _write_json(path: Path, document) -> bytes:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path.read_bytes()


def _backups(folder: Path):
    return sorted(p.name for p in folder.glob("*.pre-dsh-purge-*"))


class DatabasePurgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="purge-db-")
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "hub_state.db"

    def tearDown(self):
        self.temporary.cleanup()

    def test_the_retired_rows_go_from_every_table_and_other_rows_stay(self):
        _seed_database(
            self.db_path,
            [
                ("dsh-a", "dsh", "", "exited"),
                ("dsh-b", " DSH ", "", "interrupted"),
                ("dsh-snap", "", json.dumps({"engine": "dsh", "profile_id": "dsh"}), "exited"),
                ("omp-x", "omp", "", "running"),
                ("old-row", "", json.dumps({"engine": "codex"}), "exited"),
            ],
            per_table=("dsh-a", "omp-x"),
        )
        with self.assertLogs("mcp-agent-hub.retired_engine_purge", level="INFO") as logs:
            result = purge_retired_engine_rows(self.db_path, stamp=STAMP)

        self.assertEqual(result["sessions"], 3)
        self.assertEqual(result["rows"], 6)
        # Every per-session table lost the retired ids and kept the others.
        for table in ("sessions", "tool_calls", "intermediate_messages", "worker_events"):
            ids = _ids(self.db_path, table)
            for retired in ("dsh-a", "dsh-b", "dsh-snap"):
                self.assertNotIn(retired, ids, f"{retired} survived in {table}")
            self.assertIn("omp-x", ids, f"{table} lost an ordinary session's row")
        self.assertIn("old-row", _ids(self.db_path, "sessions"), "an empty engine column is not DSH")
        # A row that only mentions a retired session stays.
        conn = sqlite3.connect(str(self.db_path))
        try:
            mentions = conn.execute(
                "SELECT content FROM worker_events WHERE session_id='omp-x' AND content='dsh-a finished'"
            ).fetchall()
            parent_messages = conn.execute(
                "SELECT parent_messages FROM sessions WHERE session_id='omp-x'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(len(mentions), 1)
        self.assertIn("dsh-a", parent_messages)

        # The backup exists under the decided name, opens, and holds the retired rows.
        backup = self.root / f"hub_state.pre-dsh-purge-{STAMP}.db"
        self.assertTrue(backup.is_file(), _backups(self.root))
        self.assertEqual(_ids(backup, "sessions"),
                         ["dsh-a", "dsh-b", "dsh-snap", "old-row", "omp-x"])
        self.assertIn("dsh-a", _ids(backup, "tool_calls"))
        # No VACUUM: the log line says the file keeps its size.
        self.assertTrue(any("keeps its size" in line for line in logs.output), logs.output)

    def test_nothing_to_purge_writes_nothing(self):
        _seed_database(self.db_path, [("omp-x", "omp", "", "running")], per_table=("omp-x",))
        observer = sqlite3.connect(str(self.db_path))
        observer.execute("SELECT COUNT(*) FROM sessions").fetchone()
        version = observer.execute("PRAGMA data_version").fetchone()[0]
        try:
            profiles = self.root / "agent_profiles.json"
            before_profiles = _write_json(profiles, {"profiles": {"omp": {"engine": "omp"}}})
            result = purge_retired_engine_data(self.db_path, profiles, lambda doc: None, stamp=STAMP)
            after_version = observer.execute("PRAGMA data_version").fetchone()[0]
        finally:
            observer.close()
        self.assertEqual(result, {"database": {"sessions": 0, "rows": 0, "backup": None},
                                  "profiles": {"removed": 0, "backup": None}})
        self.assertEqual(after_version, version, "a start with nothing to purge wrote to the database")
        self.assertEqual(_backups(self.root), [], "a start with nothing to purge wrote a backup")
        self.assertEqual(profiles.read_bytes(), before_profiles)

    def test_a_failing_backup_deletes_nothing_and_logs_a_warning(self):
        _seed_database(self.db_path, [("dsh-a", "dsh", "", "exited")], per_table=("dsh-a",))
        with patch("retired_engine_purge._copy_database", side_effect=OSError("no space left")):
            with self.assertLogs("mcp-agent-hub.retired_engine_purge", level="WARNING") as logs:
                result = purge_retired_engine_rows(self.db_path, stamp=STAMP)
        self.assertEqual(result, {"sessions": 0, "rows": 0, "backup": None})
        self.assertTrue(any("backup" in line and "nothing deleted" in line for line in logs.output),
                        logs.output)
        for table in ("sessions", "tool_calls", "intermediate_messages", "worker_events"):
            self.assertIn("dsh-a", _ids(self.db_path, table), f"{table} lost rows without a backup")
        self.assertEqual(_backups(self.root), [])

    def test_legacy_profile_id_rows_and_orphan_children_are_purged(self):
        _seed_database(self.db_path, [("dsh-legacy", "", "", "exited")])
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute("UPDATE sessions SET profile_id='dsh' WHERE session_id='dsh-legacy'")
                conn.execute("INSERT INTO tool_calls (session_id, step_num) VALUES ('dsh-orphan', 1)")
                conn.execute("INSERT INTO intermediate_messages (session_id, step_num, content) VALUES ('dsh-orphan', 1, 'old')")
                conn.execute("INSERT INTO worker_events (session_id, timestamp, type, content) VALUES ('dsh-orphan', 1, 'old', 'old')")
        finally:
            conn.close()
        result = purge_retired_engine_rows(self.db_path, stamp=STAMP)
        self.assertEqual(result["sessions"], 1)
        for table in ("tool_calls", "intermediate_messages", "worker_events"):
            self.assertNotIn("dsh-orphan", _ids(self.db_path, table))

    def test_orphan_children_are_purged_without_a_session_row(self):
        _seed_database(self.db_path, [])
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute("INSERT INTO worker_events (session_id, timestamp, type, content) VALUES ('dsh-orphan', 1, 'old', 'old')")
        finally:
            conn.close()
        result = purge_retired_engine_rows(self.db_path, stamp=STAMP)
        self.assertEqual(result["sessions"], 0)
        self.assertNotIn("dsh-orphan", _ids(self.db_path, "worker_events"))

    def test_database_backup_collision_preserves_existing_file(self):
        _seed_database(self.db_path, [("dsh-a", "dsh", "", "exited")])
        backup = self.root / f"hub_state.pre-dsh-purge-{STAMP}.db"
        backup.write_bytes(b"previous backup")
        result = purge_retired_engine_rows(self.db_path, stamp=STAMP)
        self.assertEqual(result["sessions"], 0)
        self.assertEqual(backup.read_bytes(), b"previous backup")

    def test_mismatched_copy_is_removed(self):
        _seed_database(self.db_path, [("dsh-a", "dsh", "", "exited")])
        backup = self.root / f"hub_state.pre-dsh-purge-{STAMP}.db"
        with self.assertRaisesRegex(RuntimeError, "id set"):
            _copy_database(self.db_path, backup, {"sessions": {"other"}, "tool_calls": set(),
                                                  "intermediate_messages": set(), "worker_events": set()})
        self.assertFalse(backup.exists())
        self.assertEqual(_ids(self.db_path, "sessions"), ["dsh-a"])

    def test_a_second_run_does_nothing(self):
        _seed_database(self.db_path, [("dsh-a", "dsh", "", "exited")], per_table=("dsh-a",))
        first = purge_retired_engine_rows(self.db_path, stamp=STAMP)
        self.assertEqual(first["sessions"], 1)
        backup_bytes = (self.root / f"hub_state.pre-dsh-purge-{STAMP}.db").read_bytes()
        observer = sqlite3.connect(str(self.db_path))
        version = observer.execute("PRAGMA data_version").fetchone()[0]
        try:
            second = purge_retired_engine_rows(self.db_path, stamp="20260102-000000")
            self.assertEqual(observer.execute("PRAGMA data_version").fetchone()[0], version)
        finally:
            observer.close()
        self.assertEqual(second, {"sessions": 0, "rows": 0, "backup": None})
        self.assertEqual(_backups(self.root), [f"hub_state.pre-dsh-purge-{STAMP}.db"])
        self.assertEqual((self.root / f"hub_state.pre-dsh-purge-{STAMP}.db").read_bytes(),
                         backup_bytes)


class ProfilePurgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="purge-profiles-")
        self.root = Path(self.temporary.name)
        self.profiles = self.root / "agent_profiles.json"

    def tearDown(self):
        self.temporary.cleanup()

    def writer(self):
        def write(document):
            self.profiles.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        return write

    def test_the_retired_profile_goes_and_everything_else_is_equal(self):
        document = {
            "profiles": {
                "omp": {"id": "omp", "name": "OMP", "engine": "omp", "args": ["--x"]},
                "dsh": {"id": "dsh", "name": "DSH", "engine": "dsh", "kind": "tui"},
                "legacy": {"id": "legacy", "name": "Old", "engine": "DSH "},
                "pwsh": {"id": "pwsh", "name": "Pwsh", "engine": "pwsh"},
            },
            "global_routing_instructions": "route by task",
            "subagent_idle_timeout_minutes": 90,
            "engine_policies": {"omp": {"model_notes": {"a": "keep me"}}},
        }
        original = _write_json(self.profiles, document)
        result = purge_retired_engine_profiles(self.profiles, self.writer(), stamp=STAMP)

        self.assertEqual(result["removed"], 2)
        after = json.loads(self.profiles.read_text(encoding="utf-8"))
        expected = json.loads(json.dumps(document))
        expected["profiles"].pop("dsh")
        expected["profiles"].pop("legacy")
        self.assertEqual(after, expected)
        self.assertEqual(list(after), list(document), "the top-level key order changed")
        self.assertEqual(list(after["profiles"]), list(expected["profiles"]),
                         "the remaining profiles' order changed")
        self.assertEqual(after["global_routing_instructions"], "route by task")
        # The backup is byte-for-byte the file that was there before.
        backup = self.root / f"agent_profiles.pre-dsh-purge-{STAMP}.json"
        self.assertTrue(backup.is_file(), _backups(self.root))
        self.assertEqual(backup.read_bytes(), original)

    def test_an_unparsable_file_is_left_untouched(self):
        raw = "{not json".encode("utf-8")
        self.profiles.write_bytes(raw)
        with self.assertLogs("mcp-agent-hub.retired_engine_purge", level="WARNING") as logs:
            result = purge_retired_engine_profiles(self.profiles, self.writer(), stamp=STAMP)
        self.assertEqual(result, {"removed": 0, "backup": None})
        self.assertEqual(self.profiles.read_bytes(), raw)
        self.assertEqual(_backups(self.root), [])
        self.assertTrue(any("did not parse" in line for line in logs.output), logs.output)

    def test_a_missing_file_changes_nothing(self):
        with self.assertLogs("mcp-agent-hub.retired_engine_purge", level="INFO"):
            result = purge_retired_engine_profiles(self.profiles, self.writer(), stamp=STAMP)
        self.assertEqual(result, {"removed": 0, "backup": None})
        self.assertFalse(self.profiles.exists(), "the purge created a profiles file")

    def test_a_failing_backup_changes_nothing(self):
        original = _write_json(self.profiles, {"profiles": {"dsh": {"engine": "dsh"}}})
        # The copy's own name is taken, so the backup cannot be written.
        (self.root / f"agent_profiles.pre-dsh-purge-{STAMP}.json").mkdir()
        with self.assertLogs("mcp-agent-hub.retired_engine_purge", level="WARNING") as logs:
            result = purge_retired_engine_profiles(self.profiles, self.writer(), stamp=STAMP)
        self.assertEqual(result, {"removed": 0, "backup": None})
        self.assertEqual(self.profiles.read_bytes(), original)
        self.assertTrue(any("backup" in line and "nothing changed" in line for line in logs.output),
                        logs.output)

    def test_existing_backup_file_is_left_untouched(self):
        original = _write_json(self.profiles, {"profiles": {"dsh": {"engine": "dsh"}}})
        backup = self.root / f"agent_profiles.pre-dsh-purge-{STAMP}.json"
        backup.write_bytes(b"previous backup")
        result = purge_retired_engine_profiles(self.profiles, self.writer(), stamp=STAMP)
        self.assertEqual(result, {"removed": 0, "backup": None})
        self.assertEqual(self.profiles.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), b"previous backup")

    def test_profile_writer_failure_keeps_source_and_returns_backup(self):
        original = _write_json(self.profiles, {"profiles": {"dsh": {"engine": "dsh"}}})
        def fail(_document):
            raise OSError("writer failed")
        result = purge_retired_engine_profiles(self.profiles, fail, stamp=STAMP)
        self.assertEqual(result["removed"], 0)
        self.assertTrue((self.root / f"agent_profiles.pre-dsh-purge-{STAMP}.json").exists())
        self.assertEqual(self.profiles.read_bytes(), original)


class StartHookTests(unittest.IsolatedAsyncioTestCase):
    """The manager's start-up path purges before restart-resume reads a row."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temporary = tempfile.TemporaryDirectory(prefix="purge-start-")
        self.root = Path(self.temporary.name)
        self.profiles = self.root / "agent_profiles.json"
        self.original_profiles = _write_json(self.profiles, {
            "profiles": {
                "dsh": {"id": "dsh", "name": "DSH", "engine": "dsh", "kind": "tui", "command": "dsh"},
                "plain": {"id": "plain", "name": "Plain", "engine": "pwsh", "kind": "tui",
                          "command": "pwsh.exe"},
            },
            "global_routing_instructions": "route",
        })
        _seed_database(self.root / "hub_state.db", [
            ("dsh-old", "dsh", "", "running"),
            ("codex-old", "codex", "", "running"),
        ], per_table=("dsh-old", "codex-old"))
        (self.root / REQUEST_NAME).write_text(
            json.dumps({"session_ids": ["codex-old"]}), encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery",
                          lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(self.profiles, base_dir=Path(session_manager.__file__).parent,
                                         data_dir=self.root)

    async def asyncTearDown(self):
        for task in list(self.hub._background_tasks):
            task.cancel()
        await asyncio.gather(*self.hub._background_tasks, return_exceptions=True)
        await self.hub.db.close()

    async def test_copy_failure_keeps_rows_and_start_continues(self):
        with patch("session_manager.purge_retired_engine_data", side_effect=OSError("copy failed")):
            with self.assertLogs("mcp-agent-hub.session_manager", level="WARNING"):
                await self.hub.start()
        self.assertIn("dsh-old", await self.hub.db.list_session_ids())
        self.assertEqual(list(self.root.glob("*.pre-dsh-purge-*")), [])

    async def test_profile_writer_failure_keeps_profiles_and_catalog(self):
        original = self.profiles.read_bytes()
        with patch.object(self.hub, "_write_profiles_file", side_effect=OSError("writer failed")):
            with self.assertLogs("mcp-agent-hub.retired_engine_purge", level="WARNING"):
                await self.hub.start()
        self.assertEqual(self.profiles.read_bytes(), original)
        self.assertNotIn("dsh", self.hub.profiles)

    async def test_second_start_does_nothing_for_both_halves(self):
        with patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            await self.hub.start()
            first_files = sorted(p.name for p in self.root.glob("*.pre-dsh-purge-*"))
            first_profile = self.profiles.read_bytes()
            await self.hub.start()
        self.assertEqual(sorted(p.name for p in self.root.glob("*.pre-dsh-purge-*")), first_files)
        self.assertEqual(self.profiles.read_bytes(), first_profile)

    async def test_the_purge_runs_before_resume_on_start_reads_rows(self):
        seen_by_normalizer = []

        async def spy():
            seen_by_normalizer.append(await self.hub.db.list_session_ids())
            return {}

        self.hub._normalize_archive_on_startup = spy
        with patch("session_manager.wait_for_port", new_callable=AsyncMock, return_value=True):
            with self.assertLogs("mcp-agent-hub.retired_engine_purge", level="INFO") as logs:
                await self.hub.start()

        # The rows were gone before the start-up normalisation read them.
        self.assertEqual(len(seen_by_normalizer), 1)
        self.assertNotIn("dsh-old", seen_by_normalizer[0])
        self.assertIn("codex-old", seen_by_normalizer[0])
        self.assertTrue(any("deleted 1 dsh session" in line for line in logs.output), logs.output)

        # The saved profile went through the hub's writer; the catalog no longer serves it.
        after = json.loads(self.profiles.read_text(encoding="utf-8"))
        self.assertEqual(sorted(after["profiles"]), ["plain"])
        self.assertEqual(after["global_routing_instructions"], "route")
        self.assertNotIn("dsh", self.hub.profiles)
        backup = self.root / "agent_profiles.pre-dsh-purge-"
        copies = [p for p in self.root.glob("agent_profiles.pre-dsh-purge-*.json")]
        self.assertEqual(len(copies), 1, copies)
        self.assertEqual(copies[0].read_bytes(), self.original_profiles)
        self.assertTrue(str(copies[0]).startswith(str(backup)))
        # The database backup holds the retired session.
        copies = [p for p in self.root.glob("hub_state.pre-dsh-purge-*.db")]
        self.assertEqual(len(copies), 1, copies)
        self.assertIn("dsh-old", _ids(copies[0], "sessions"))


if __name__ == "__main__":
    unittest.main()
