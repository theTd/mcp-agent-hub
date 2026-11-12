"""One-shot request to resume the sessions a restart interrupts, read when the hub starts.

Whoever restarts the hub on purpose writes `resume_on_start.json` into the hub's data folder
before stopping it. The next start takes the file (renames it, so it is used once), notes which
sessions were live when the hub stopped, and resumes them. Until that is done every request
except `/api/hub/info` waits (see `RestartResumeGate`), so a parent that watches for the hub to
come back finds its sessions already running instead of interrupted, and cannot race the resume.

File contents (all optional):
    {"session_ids": ["codex-…", …],          # default: every session live when the hub stopped
     "continue_unfinished_turns": true,       # default true
     "continue_session_ids": ["codex-…", …],  # default: decided from each session's status at the stop
     "continue_text": "…"}                    # default CONTINUE_TEXT

A listed session that an earlier start already archived as interrupted (and nobody resumed since)
is resumed too. Its status at the stop is no longer known, so it gets the message only when it is
in `continue_session_ids`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger("mcp-agent-hub.restart_resume")

REQUEST_NAME = "resume_on_start.json"
TAKEN_NAME = "resume_on_start.taken.json"
RESULT_NAME = "resume_on_start.result.json"
# A turn the stop cut off. "completed" is live but idle, so it comes back without a message.
UNFINISHED_TURN_STATUSES = frozenset({"starting", "running", "waiting_parent"})
CONTINUE_TEXT = (
    "[Hub] The agent hub was restarted while you were working, so your previous turn was cut off: "
    "any command, script or approval prompt that was running has stopped. Check where you left off "
    "and continue the task. Ask again for any approval you were waiting on."
)
# Requests wait at most this long for the resume; the desktop app gives the hub 60 s to answer.
GATE_MAX_WAIT_SEC = 30.0
PORT_OPEN_WAIT_SEC = 60.0
PORT_POLL_SEC = 0.05


@dataclass(frozen=True)
class RestartResumeRequest:
    session_ids: Optional[FrozenSet[str]]
    continue_unfinished_turns: bool
    continue_session_ids: Optional[FrozenSet[str]]
    continue_text: str
    data_dir: Path

    @classmethod
    def take(cls, data_dir: Path) -> Optional["RestartResumeRequest"]:
        """Read and consume the request; None when there is none or it cannot be read."""
        source = Path(data_dir) / REQUEST_NAME
        if not source.is_file():
            return None
        taken = Path(data_dir) / TAKEN_NAME
        try:
            os.replace(source, taken)
            raw = json.loads(taken.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring the resume-on-start request %s: %s", source, exc)
            return None
        if not isinstance(raw, dict):
            logger.warning("Ignoring the resume-on-start request %s: not a JSON object", source)
            return None
        ids = raw.get("session_ids")
        cont = raw.get("continue_session_ids")
        text = str(raw.get("continue_text") or "").strip() or CONTINUE_TEXT
        return cls(
            session_ids=frozenset(str(i) for i in ids) if isinstance(ids, list) else None,
            continue_unfinished_turns=raw.get("continue_unfinished_turns", True) is not False,
            continue_session_ids=frozenset(str(i) for i in cont) if isinstance(cont, list) else None,
            continue_text=text,
            data_dir=Path(data_dir),
        )

    def wants(self, session_id: str) -> bool:
        return self.session_ids is None or session_id in self.session_ids

    def text_for(self, session_id: str, engine: str, status_at_stop: str) -> Optional[str]:
        """The message a resumed session gets: only a terminal agent whose turn the stop cut off.

        A DSH job keeps running in DSH Host while the hub is down, so its session is only
        re-attached.
        """
        if not self.continue_unfinished_turns or engine == "dsh":
            return None
        if self.continue_session_ids is not None:
            return self.continue_text if session_id in self.continue_session_ids else None
        return self.continue_text if status_at_stop in UNFINISHED_TURN_STATUSES else None

    def write_result(self, results: Dict[str, Dict[str, Any]], started_at: float) -> None:
        body = {"started_at": started_at, "finished_at": time.time(), "sessions": results}
        try:
            (self.data_dir / RESULT_NAME).write_text(
                json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write %s: %s", RESULT_NAME, exc)


class RestartResumeGate:
    """Holds requests until the resume-on-start pass is done (or GATE_MAX_WAIT_SEC passed)."""

    def __init__(self) -> None:
        self._open = asyncio.Event()
        self._closed_at: Optional[float] = None

    def close(self) -> None:
        self._open.clear()
        self._closed_at = time.monotonic()

    def open(self) -> None:
        self._open.set()

    @property
    def is_open(self) -> bool:
        return self._closed_at is None or self._open.is_set()

    async def wait(self) -> None:
        if self.is_open:
            return
        left = GATE_MAX_WAIT_SEC - (time.monotonic() - float(self._closed_at or 0.0))
        if left <= 0:
            return
        try:
            await asyncio.wait_for(self._open.wait(), timeout=left)
        except asyncio.TimeoutError:
            pass


async def wait_for_port(port: int, timeout: float = PORT_OPEN_WAIT_SEC) -> bool:
    """True once 127.0.0.1:port accepts a connection (the hub's own server is listening)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1.0)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return True
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(PORT_POLL_SEC)
    return False
