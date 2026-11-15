import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from session_manager import HubSessionManager


class OmpNewTranscriptsTests(unittest.TestCase):
    """The OMP watcher claims omp's session file, never a `.jsonl` inside the session's artifact dir."""

    def test_artifact_changes_log_is_never_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = SimpleNamespace(spawned_at=time.time(), created_at=time.time())
            time.sleep(0.05)
            stem = "2026-11-15T04-43-25-327Z_01a1dd84-6b0f-779d-b34f-929ce42dba14"
            main = root / f"{stem}.jsonl"
            main.write_text('{"type":"session"}\n', encoding="utf-8")
            artifacts = root / stem
            artifacts.mkdir()
            (artifacts / "1.read.log").write_text("x", encoding="utf-8")
            changes = artifacts / "changes.jsonl"
            changes.write_text('{"type":"call"}\n', encoding="utf-8")
            # The live failure: the artifact log was written last, so it was the newest .jsonl.
            later = time.time() + 5
            os.utime(changes, (later, later))

            self.assertEqual(HubSessionManager._omp_new_transcripts(session, root), [main])

    def test_skips_files_older_than_the_spawn_and_orders_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old.jsonl").write_text("{}\n", encoding="utf-8")
            time.sleep(0.05)
            session = SimpleNamespace(spawned_at=time.time(), created_at=time.time())
            time.sleep(0.05)
            first = root / "a.jsonl"
            first.write_text("{}\n", encoding="utf-8")
            second = root / "b.jsonl"
            second.write_text("{}\n", encoding="utf-8")
            os.utime(first, (time.time() + 10, time.time() + 10))
            (root / "notes.txt").write_text("x", encoding="utf-8")

            self.assertEqual(HubSessionManager._omp_new_transcripts(session, root), [first, second])

    def test_missing_session_dir_finds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = SimpleNamespace(spawned_at=0.0, created_at=0.0)
            self.assertEqual(HubSessionManager._omp_new_transcripts(session, Path(tmp) / "absent"), [])


if __name__ == "__main__":
    unittest.main()
