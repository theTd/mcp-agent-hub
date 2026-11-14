import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from session_manager import HubSessionManager


def make_transcript(conversation_dir: Path) -> Path:
    path = conversation_dir / ".system_generated" / "logs" / "transcript.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()
    return path


class AgyNewTranscriptsTests(unittest.TestCase):
    """The new-conversation scan checks one known path per top-level brain dir, nothing deeper."""

    def test_finds_only_top_level_conversations_started_after_the_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_transcript(root / "old-conversation")
            time.sleep(0.05)
            session = SimpleNamespace(spawned_at=time.time(), created_at=time.time())
            time.sleep(0.05)
            new = make_transcript(root / "new-conversation")
            make_transcript(root / "new-parent" / "nested-conversation")
            (root / "stray-file.jsonl").touch()

            found = HubSessionManager._agy_new_transcripts(session, root)

            self.assertEqual(found, [new])

    def test_missing_brain_root_finds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = SimpleNamespace(spawned_at=0.0, created_at=0.0)
            self.assertEqual(HubSessionManager._agy_new_transcripts(session, Path(tmp) / "absent"), [])


if __name__ == "__main__":
    unittest.main()
