"""One-shot request to resume the sessions a restart interrupts, read when the hub starts.

Whoever restarts the hub on purpose writes `resume_on_start.json` into the hub's data folder
before stopping it. The next start takes the file (renames it, so it is used once), notes which
sessions were live when the hub stopped, and resumes them. Until that is done every request
except `/api/hub/info` waits (see `RestartResumeGate`), so a parent that watches for the hub to
come back finds its sessions already running instead of interrupted, and cannot race the resume.

The same file also tells a hub that is *stopping* that the stop is a planned restart: `peek` reads
it without consuming it, so the shutdown can keep its sessions resumable instead of settling every
parent wait handle as a final stop.

File contents (all optional):
    {"session_ids": ["codex-…", …],          # default: every session live when the hub stopped
     "continue_unfinished_turns": true,       # default true
     "continue_session_ids": ["codex-…", …],  # default: decided from each session's status at the stop
     "continue_text": "…",                    # default CONTINUE_TEXT
     "restart_id": "…",                       # optional stamp for logs/result correlation
     "requested_at": 1690000000.0,             # optional wall-clock time of the request
     "sessions": {"codex-…": {"engine": …, "status": …}},  # live-at-request snapshot
     "resume_crashed": false,                 # default false: only fetch crashed sessions on purpose
     "mode": "hub"}                           # "hub" or "replace-exe", for the status endpoint

A listed session that an earlier start already archived as interrupted (and nobody resumed since)
is resumed too. Its status at the stop is no longer known, so it gets the message only when it is
in `continue_session_ids`.

A take is not a promise the resume happened. The file it consumed (`resume_on_start.taken.json`)
and the answer it writes (`resume_on_start.result.json`) are stamped with the same `restart_id`,
so a start can tell an answered pass from one that a hard kill cut off: a `.taken.json` whose
`restart_id` has no matching result is taken again, and the sessions the dead pass never reached
are resumed. `mark_completed` stamps the `.taken.json` as answered, which keeps that file for a
diagnosis without arming a resume a third time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
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
# The default owner of a take: this process. A `.taken.json` this token stamped is one this
# process already consumed, so a second `take` in it never re-arms the resume; a process that did
# not write it (a hard kill left it behind) is free to adopt it.
PROCESS_TOKEN = f"{os.getpid()}:{uuid.uuid4().hex}"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """A JSON object at `path`, or None: a missing or unreadable file is no object."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("%s is unreadable: %s", path, exc)
        return None
    return raw if isinstance(raw, dict) else None


def _write_json(path: Path, body: Dict[str, Any]) -> bool:
    """Write `body` whole through a temp file, so a reader never sees a half stamp."""
    path = Path(path)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        return True
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)
        return False


def _result_answers(result: Optional[Dict[str, Any]], taken: Dict[str, Any],
                    taken_path: Path) -> bool:
    """True when `result` is the answer to the take `taken` (so nothing is left to resume).

    The stamp is the whole proof: both files carry the `restart_id` the take minted, and a pass
    that stopped mid-way writes `completed: false`, which is not an answer. Only a file written
    by an older writer (no `restart_id` on either side) falls back to the mtime comparison.
    """
    if not isinstance(result, dict):
        return False
    rid = str(taken.get("restart_id") or "")
    result_rid = str(result.get("restart_id") or "")
    if rid and result_rid:
        return result_rid == rid and result.get("completed", True) is not False
    try:
        finished = float(result.get("finished_at") or 0.0)
        taken_at = Path(taken_path).stat().st_mtime
    except (TypeError, ValueError, OSError):
        return False
    return bool(finished) and finished >= taken_at



def _settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Every optional field of the file, normalized, backward compatible with the old shape."""
    ids = raw.get("session_ids")
    cont = raw.get("continue_session_ids")
    sessions: Dict[str, Dict[str, str]] = {}
    snapshot = raw.get("sessions")
    if isinstance(snapshot, dict):
        for sid, info in snapshot.items():
            name = str(sid or "").strip()
            if not name:
                continue
            meta = info if isinstance(info, dict) else {}
            sessions[name] = {
                "engine": str(meta.get("engine") or "").strip().lower(),
                "status": str(meta.get("status") or "").strip().lower(),
            }
    restart_id = raw.get("restart_id")
    requested_at = raw.get("requested_at")
    try:
        requested_at = float(requested_at) if requested_at is not None else None
    except (TypeError, ValueError):
        requested_at = None
    mode = str(raw.get("mode") or "").strip().lower() or None
    return {
        "session_ids": frozenset(str(i) for i in ids) if isinstance(ids, list) else None,
        "continue_unfinished_turns": raw.get("continue_unfinished_turns", True) is not False,
        "continue_session_ids": frozenset(str(i) for i in cont) if isinstance(cont, list) else None,
        "continue_text": str(raw.get("continue_text") or "").strip() or CONTINUE_TEXT,
        "restart_id": str(restart_id) if restart_id else None,
        "requested_at": requested_at,
        "mode": mode,
        "sessions": sessions,
        "resume_crashed": raw.get("resume_crashed") is True,
    }


@dataclass(frozen=True)
class RestartResumeRequest:
    session_ids: Optional[FrozenSet[str]]
    continue_unfinished_turns: bool
    continue_session_ids: Optional[FrozenSet[str]]
    continue_text: str
    data_dir: Path
    restart_id: Optional[str] = None
    requested_at: Optional[float] = None
    mode: Optional[str] = None
    sessions: Dict[str, Dict[str, str]] = field(default_factory=dict)
    resume_crashed: bool = False

    @classmethod
    def peek(cls, data_dir: Path) -> Optional["RestartResumeRequest"]:
        """Read the request without consuming it; None when there is none or it cannot be read.

        A stopping hub asks this to learn whether *this* stop is a planned restart, so it must
        leave the file for the next start to take.
        """
        source = Path(data_dir) / REQUEST_NAME
        try:
            raw = json.loads(source.read_text(encoding="utf-8") or "{}")
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("Resume-on-start request %s is unreadable: %s", source, exc)
            return None
        if not isinstance(raw, dict):
            logger.warning("Resume-on-start request %s is not a JSON object", source)
            return None
        return cls(**{**_settings(raw), "data_dir": Path(data_dir)})

    @classmethod
    def take(cls, data_dir: Path, *, owner: Optional[str] = None) -> Optional["RestartResumeRequest"]:
        """Read and consume the request; None when there is none or it is already answered.

        A pending `resume_on_start.json` is the new request: it is renamed to `.taken.json` (which
        is where its `restart_id` and its owner are stamped) and returned. Without one, a
        `.taken.json` an *earlier* start left behind is returned again when its result is missing
        or belongs to another restart -- that start was hard-killed mid-resume, and `finally`
        never ran, so nothing put the request back. `owner` names this start (default: this
        process): a `.taken.json` the same owner already consumed is never consumed twice.
        """
        data_dir = Path(data_dir)
        source = data_dir / REQUEST_NAME
        taken_path = data_dir / TAKEN_NAME
        token = str(owner or PROCESS_TOKEN)
        if source.is_file():
            try:
                os.replace(source, taken_path)
                raw = json.loads(taken_path.read_text(encoding="utf-8") or "{}")
            except (OSError, ValueError) as exc:
                logger.warning("Ignoring the resume-on-start request %s: %s", source, exc)
                return None
            if not isinstance(raw, dict):
                logger.warning("Ignoring the resume-on-start request %s: not a JSON object", source)
                return None
            return cls._consume(data_dir, raw, taken_path, token, log_source=source)
        return cls._adopt_unfinished(data_dir, taken_path, token)

    @classmethod
    def _consume(cls, data_dir: Path, raw: Dict[str, Any], taken_path: Path, owner: str,
                 *, log_source: Optional[Path] = None) -> "RestartResumeRequest":
        """Stamp the take (a `restart_id` when the writer left none, this owner, not answered)."""
        settings = _settings(raw)
        restart_id = settings["restart_id"]
        minted = False
        if not restart_id:
            # From here on the take has a deterministic identity: `write_result` writes the same
            # id, so the next start can prove this pass answered this request.
            restart_id = uuid.uuid4().hex
            minted = True
        stamp: Dict[str, Any] = {}
        if minted or raw.get("restart_id") != restart_id:
            stamp["restart_id"] = restart_id
        if raw.get("owner") != owner:
            stamp["owner"] = owner
        if raw.get("completed") is not None:
            # A fresh take is a fresh pass: the previous answer, if any, belongs to an earlier one.
            stamp["completed"] = False
        if stamp:
            stamp.setdefault("taken_at", time.time())
            if not _write_json(taken_path, {**raw, **stamp}):
                logger.warning("Resume-on-start: could not stamp %s; the restart_id stays in memory",
                               taken_path)
        request = cls(**{**_settings({**raw, **stamp}), "data_dir": Path(data_dir)})
        logger.info("Resume-on-start: took %s (restart_id=%s)",
                    log_source or taken_path, request.restart_id)
        return request

    @classmethod
    def _adopt_unfinished(cls, data_dir: Path, taken_path: Path, owner: str
                          ) -> Optional["RestartResumeRequest"]:
        """A `.taken.json` whose pass never answered is this start's request again."""
        raw = _read_json(taken_path)
        if raw is None:
            return None
        if str(raw.get("owner") or "") == owner:
            # This start already took it: a second take is not a continuation, and the file is
            # either being handled right now or its answer is about to be written.
            return None
        if raw.get("completed") is True:
            return None
        if _result_answers(_read_json(Path(data_dir) / RESULT_NAME), raw, taken_path):
            return None
        logger.warning("Resume-on-start: %s was never answered (restart_id=%s); taking it again",
                       taken_path, raw.get("restart_id"))
        return cls._consume(data_dir, raw, taken_path, owner)

    def to_dict(self) -> Dict[str, Any]:
        """The request as the file holds it, for `restore`."""
        return {
            "session_ids": sorted(self.session_ids) if self.session_ids is not None else None,
            "continue_unfinished_turns": self.continue_unfinished_turns,
            "continue_session_ids": (sorted(self.continue_session_ids)
                                     if self.continue_session_ids is not None else None),
            "continue_text": self.continue_text,
            "restart_id": self.restart_id,
            "requested_at": self.requested_at,
            "mode": self.mode,
            "sessions": dict(self.sessions or {}),
            "resume_crashed": self.resume_crashed,
        }

    def restore(self) -> bool:
        """Write the request back after a resume pass that did not finish.

        The next start then takes it again and continues the sessions this pass never reached; the
        ones already back are skipped (they are live), so nothing is resumed or messaged twice.
        """
        path = self.data_dir / REQUEST_NAME
        try:
            path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except OSError as exc:
            logger.warning("Could not restore %s after an unfinished resume: %s", path, exc)
            return False

    def wants(self, session_id: str) -> bool:
        return self.session_ids is None or session_id in self.session_ids

    def allows_crashed(self) -> bool:
        """A crashed row is revived only when the caller asked for sessions by name or said so."""
        return self.session_ids is not None or self.resume_crashed

    def text_for(self, session_id: str, engine: str, status_at_stop: str) -> Optional[str]:
        """The message a resumed session gets: only a terminal agent whose turn the stop cut off."""
        if not self.continue_unfinished_turns:
            return None
        if self.continue_session_ids is not None:
            return self.continue_text if session_id in self.continue_session_ids else None
        return self.continue_text if status_at_stop in UNFINISHED_TURN_STATUSES else None

    def write_result(self, results: Dict[str, Dict[str, Any]], started_at: float, *,
                     restart_id: Optional[str] = None,
                     skipped: Optional[Dict[str, str]] = None,
                     completed: bool = True) -> None:
        """Write this pass' answer, always under the take's `restart_id`.

        `completed=False` is a pass that stopped mid-way: the answer is useful (it names what it
        reached) but it is not the proof a take needs, so the request is taken again next start.
        A completed pass stamps its `.taken.json`, which is what makes `take` a no-op afterwards.
        """
        rid = restart_id or self.restart_id
        body: Dict[str, Any] = {
            "started_at": started_at, "finished_at": time.time(), "sessions": results,
            "completed": bool(completed),
        }
        if rid:
            body["restart_id"] = rid
        if skipped:
            # A session this start left alone on purpose (an engine without a revive recipe).
            body["skipped"] = dict(skipped)
        if _write_json(self.data_dir / RESULT_NAME, body) and completed:
            self.mark_completed(restart_id=rid)

    def mark_completed(self, *, restart_id: Optional[str] = None) -> bool:
        """Stamp the `.taken.json` of this request as answered (its result is on disk).

        The file stays for a diagnosis; `take` reads the stamp first, so a second cold start
        neither resumes these sessions again nor rewrites their result. A `.taken.json` belonging
        to another restart is never stamped with this one's id.
        """
        rid = str(restart_id or self.restart_id or "")
        path = self.data_dir / TAKEN_NAME
        raw = _read_json(path)
        if raw is None:
            return False
        own = str(raw.get("restart_id") or "")
        if own and rid and own != rid:
            logger.debug("Not stamping %s: it belongs to restart %s, not %s", path, own, rid)
            return False
        if rid:
            raw["restart_id"] = rid
        raw["completed"] = True
        raw["completed_at"] = time.time()
        return _write_json(path, raw)


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

    async def wait_bounded(self, max_wait_sec: float) -> bool:
        """True once the gate opened within `max_wait_sec`; False when it is still closed.

        A long-poll caller uses it to answer 503 + Retry-After instead of hanging for the whole
        resume: the waiter retries, and the resume finishes behind a short, honest refusal.
        """
        if self.is_open:
            return True
        left = min(max(0.0, float(max_wait_sec)),
                   max(0.0, GATE_MAX_WAIT_SEC - (time.monotonic() - float(self._closed_at or 0.0))))
        if left <= 0:
            return self.is_open
        try:
            await asyncio.wait_for(self._open.wait(), timeout=left)
        except asyncio.TimeoutError:
            return self.is_open
        return True


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
