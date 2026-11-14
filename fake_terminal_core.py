"""A fake of the Rust terminal core's `Core` calls, for the hub's tests (design §10).

`AgentSession` and `HubSessionManager` reach the terminal only through the calls
`hub_terminal_core`'s `lib.rs` exposes (design §5.2, `seam-coverage.md`), so a test that needs
ordered keystrokes, a scripted screen or a controlled drain swaps the core for this object instead
of a PTY.  It is deliberately not a second terminal model: the screen is a string the test sets, the
ring is the text the test feeds, and the only ordering rule is arrival order.

What it keeps faithful, because the hub's behaviour depends on it:

- `spawn`/`attach_text` replace a dead incarnation of the same id and hand its sockets the prefix
  or seed frame first (`rtc-sinks-per-id`), so a resume test can check old bytes, banner, new bytes;
- an id's incarnation counter drops the predecessor's queued records when the id is re-created, as
  the core's table insert does, so a death that was not drained before a same-id reattach is lost;
- `attach_text` gives a text session pid 0 (no process), like the real core;
- `kill` never invents a clean code 0 for a process: it answers the code the session was scripted
  with, otherwise the job's nonzero termination code; a text session's kill answers `None`, as the
  real core's does;
- `drain` returns queued records and clears them, so `_on_core_tick` sees exactly what the core would
  publish, and only the current incarnation's records can mark its death drained; it can withhold a
  death record for N drains (`withhold_death_records`), which the fixed core may do for one;
- `retire` refuses a live id, one whose death record has not been drained (`rtc-retire`) and one
  whose resume is in flight (the coming core contract), and it can be made busy (`TimeoutError`);
- `take_frames` is non-blocking; a detached or retired handle reports `closed` at its first take,
  and `detach_queue` can miss its deadline with `TimeoutError` (the coming registry contract).

Test-only knobs: `set_exit`, `die`, `late_code`, `set_last_output_at`, `set_write_timeouts`,
`block_detach`, `block_retire`, `resume_in_flight`, `read_timeouts`, `snapshot_delay`,
`shutdown_delay`, `withhold_death_records`, `calls` (with `snapshot_budgets`, `kill_graces`,
`detach_calls`, `retire_calls`).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set, Tuple


class FakeTerminalQueue:
    """One attached socket's bounded frame queue (`FrameQueue`), in arrival order."""

    def __init__(self) -> None:
        self.frames: List[bytes] = []
        self.closed = False
        self.dropped = 0


class FakeTerminalSession:
    """One session's state: the raw ring, the screen text, the keystrokes and its sockets."""

    def __init__(self, session_id: str, pid: int = 4242, cols: int = 120, rows: int = 36) -> None:
        self.session_id = session_id
        self.pid = pid
        self.cols = int(cols)
        self.rows = int(rows)
        self.ring = ""
        self.ring_bytes = 0
        self.rev = 0
        self.screen_text = ""
        self.screen_rev = 0
        self.cursor_x = 0
        self.cursor_y = 0
        self.inputs: List[str] = []
        # The incarnation: bumped by every `spawn`/`attach_text`, so a predecessor's records are
        # recognisably stale (`rtc-retire`'s table insert drops them).
        self.generation = 0
        self.alive = True
        self.exit_code: Optional[int] = None
        self.crashed = False
        self.degraded = False
        self.dropped_bytes = 0
        self.death_drained = False
        self.last_output_at: Optional[float] = None
        # A full write queue: the next N `write` calls raise TimeoutError and queue nothing.
        self.write_timeouts = 0
        self.watched = False
        self.watched_max_lines: Optional[int] = None
        self.watch_calls: List[int] = []
        self.flags: Dict[str, Any] = {}
        self.seed_history: Optional[str] = None
        self.ws_prefix: Optional[str] = None
        self.queues: List[FakeTerminalQueue] = []


class FakeTerminal:
    """The `Core` surface the hub calls, with per-session lists a test can assert on."""

    def __init__(self) -> None:
        self.sessions: Dict[str, FakeTerminalSession] = {}
        # (incarnation, record): `drain` drops a record whose incarnation is no longer current.
        self.records: List[Tuple[int, Dict[str, Any]]] = []
        self.handles: Dict[int, Tuple[str, FakeTerminalQueue]] = {}
        self._next_handle = 1
        self.retired: List[str] = []
        self.retire_calls: List[str] = []
        self.kill_calls: List[Tuple[str, int]] = []
        self.detach_calls: List[int] = []
        self.snapshot_budgets: List[int] = []
        self.kill_graces: List[int] = []
        self.shut_down = False
        # The coming core contracts: a busy registry (`detach_queue`) and a resume in flight.
        self.block_detach = 0
        self.block_retire = 0
        self.resume_in_flight: Set[str] = set()
        # The core's table read deadline: the next N session reads raise TimeoutError, as a table
        # held by a spawn insert or a snapshot does.
        self.read_timeouts = 0
        # Shutdown scripting: how long each call takes, and the order they were made in.
        self.snapshot_delay = 0.0
        self.shutdown_delay = 0.0
        self.calls: List[str] = []
        # Core contract 5: a death record can be withheld for N drains (until the session's final
        # batch has flushed), so the hub's ordering around a death is testable as it really is.
        self.withhold_death_records = 0

    # ---- helpers a test uses to script the session ----------------------------------------

    def session(self, session_id: str) -> FakeTerminalSession:
        return self.sessions[session_id]

    def set_screen(self, session_id: str, text: str, cursor_x: int = 0, cursor_y: int = 0) -> None:
        state = self.sessions[session_id]
        state.screen_text = text
        state.cursor_x = cursor_x
        state.cursor_y = cursor_y
        state.screen_rev += 1

    def set_exit(self, session_id: str, code: Optional[int], crashed: bool = False) -> None:
        """Scripts the death: `kill` answers this code, and the death record carries it."""
        state = self.sessions[session_id]
        state.exit_code = code
        state.crashed = crashed

    def set_last_output_at(self, session_id: str, timestamp: Optional[float]) -> None:
        self.sessions[session_id].last_output_at = timestamp

    def die(self, session_id: str) -> None:
        """The process is gone without a `kill`: the reader saw it end, or the reader panicked.

        The death record carries whatever `set_exit` scripted, `None` included, so the hub's
        `None`/`crashed` handling is reachable exactly as the real core reports it.
        """
        state = self.sessions[session_id]
        state.alive = False
        state.death_drained = False
        self.records.append((state.generation, self._record(state, urgent=True)))

    def late_code(self, session_id: str, code: int) -> None:
        """SF3: a code the core learned after the death record, on the same dead session."""
        state = self.sessions[session_id]
        state.exit_code = int(code)
        self.records.append((state.generation, self._record(state)))

    def set_write_timeouts(self, session_id: str, count: int) -> None:
        """The next `count` `write` calls miss the queue deadline and queue nothing."""
        self.sessions[session_id].write_timeouts = int(count)

    def output(self, session_id: str, text: str) -> None:
        """Output as the reader files it: the ring grows, the revision moves, a record is queued,
        and every attached socket gets the same bytes."""
        state = self.sessions[session_id]
        state.ring += text
        state.ring_bytes = len(state.ring.encode("utf-8", "replace"))
        state.rev += 1
        # The same chunk the ring took goes to every attached socket (`rtc-sinks-per-id`).
        self._deliver(state, text)
        self.records.append((state.generation, self._record(
            state, screen_text=state.screen_text if state.watched else None)))

    def frames_of(self, handle: int) -> List[bytes]:
        return self.handles[handle][1].frames

    def inputs_of(self, session_id: str) -> List[str]:
        return self.sessions[session_id].inputs

    # ---- `Core`'s calls --------------------------------------------------------------------

    def spawn(self, session_id: str, argv: List[str], cwd: Optional[str] = None,
              env: Optional[Dict[str, str]] = None, cols: int = 80, rows: int = 24,
              flags: Optional[Dict[str, Any]] = None, seed_history: Optional[str] = None,
              ws_prefix: Optional[str] = None) -> Dict[str, Any]:
        previous = self.sessions.get(session_id)
        if previous is not None and previous.alive:
            raise ValueError(f"session {session_id} is already running in the core")
        state = FakeTerminalSession(session_id, cols=cols, rows=rows)
        state.flags = dict(flags or {})
        state.seed_history = seed_history
        state.ws_prefix = ws_prefix
        if seed_history:
            state.ring = seed_history
            state.ring_bytes = len(seed_history.encode("utf-8", "replace"))
            state.rev = 1
        # A new incarnation: the predecessor's fan-out is handed over, the rest of it is not.
        if previous is not None:
            state.queues = previous.queues
        self._next_generation(state)
        if ws_prefix:
            self._deliver(state, ws_prefix)
        self.sessions[session_id] = state
        self.records.append((state.generation, self._record(state)))
        return {"pid": state.pid, "cols": state.cols, "rows": state.rows, "elapsed_ms": 0.0}

    def attach_text(self, session_id: str, cols: int = 80, rows: int = 24,
                    flags: Optional[Dict[str, Any]] = None, seed_history: Optional[str] = None,
                    seed: Optional[str] = None) -> Dict[str, Any]:
        previous = self.sessions.get(session_id)
        if previous is not None and previous.alive:
            raise ValueError(f"session {session_id} is already running in the core")
        # A text session has no process: pid 0 is the reply and the record alike, as the real core
        # reports it, so the hub takes the no-process branch (`has_process` false).
        state = FakeTerminalSession(session_id, pid=0, cols=cols, rows=rows)
        state.flags = dict(flags or {})
        state.seed_history = seed_history
        state.ws_prefix = seed
        if seed_history:
            state.ring = seed_history
            state.ring_bytes = len(seed_history.encode("utf-8", "replace"))
            state.rev = 1
        if previous is not None:
            state.queues = previous.queues
        self._next_generation(state)
        if seed:
            self._deliver(state, seed)
        self.sessions[session_id] = state
        self.records.append((state.generation, self._record(state)))
        return {"pid": 0, "cols": state.cols, "rows": state.rows}

    def feed(self, session_id: str, text: str, deadline_ms: Optional[int] = None) -> int:
        state = self.sessions[session_id]
        state.ring += text
        state.ring_bytes = len(state.ring.encode("utf-8", "replace"))
        state.rev += 1
        state.screen_rev += 1
        self._deliver(state, text)
        self.records.append((state.generation, self._record(
            state, screen_text=state.screen_text if state.watched else None)))
        return state.rev

    def write(self, session_id: str, text: str, deadline_ms: Optional[int] = None) -> int:
        state = self.sessions[session_id]
        if state.write_timeouts > 0:
            state.write_timeouts -= 1
            raise TimeoutError(f"write to {session_id} missed its deadline (queue full)")
        state.inputs.append(text)
        return len(text.encode("utf-8"))

    def resize(self, session_id: str, cols: int, rows: int,
               deadline_ms: Optional[int] = None) -> Tuple[int, int]:
        state = self.sessions[session_id]
        if int(cols) < 20 or int(rows) < 5:
            raise ValueError("resize below 20x5")
        state.cols, state.rows = int(cols), int(rows)
        return state.cols, state.rows

    def kill(self, session_id: str, grace_ms: Optional[int] = None) -> Optional[int]:
        state = self.sessions[session_id]
        self.kill_calls.append((session_id, int(grace_ms or 2000)))
        if not state.alive:
            return state.exit_code
        state.alive = False
        if state.pid and state.exit_code is None:
            # A forced kill of a live process yields the job's termination code, never a clean 0.
            state.exit_code = 1
        # A text session has no process: the real core publishes `exit_code=None, crashed=false`
        # for its kill, and only a scripted code (`set_exit`) is ever reported for one.
        state.death_drained = False
        self.records.append((state.generation, self._record(state, urgent=True)))
        return state.exit_code

    def kill_all(self, grace_ms: Optional[int] = None) -> None:
        self.calls.append("kill_all")
        self.kill_graces.append(int(grace_ms or 0))
        for session_id in list(self.sessions):
            try:
                self.kill(session_id, grace_ms)
            except KeyError:
                pass

    def drain(self) -> List[Dict[str, Any]]:
        records, self.records = self.records, []
        out: List[Dict[str, Any]] = []
        held: List[Tuple[int, Dict[str, Any]]] = []
        for generation, record in records:
            state = self.sessions.get(str(record.get("session_id") or ""))
            if state is None or state.generation != generation:
                # The id was re-created (a resume): the core's table insert dropped this record.
                continue
            if record.get("alive") is False and self.withhold_death_records > 0:
                # Contract 5: the record waits until the session's last batch has flushed, one
                # drain later; its death is not drained yet, so `retire` still refuses the id.
                held.append((generation, record))
                continue
            if record.get("alive") is False:
                state.death_drained = True
            out.append(record)
        if held:
            self.withhold_death_records -= 1
            self.records = held + self.records
        return out

    def raw_tail(self, session_id: str, max_chars: Optional[int] = None,
                 deadline_ms: Optional[int] = None) -> str:
        self._check_read()
        if not max_chars or max_chars <= 0:
            return ""
        return self.sessions[session_id].ring[-int(max_chars):]

    def screen(self, session_id: str, max_lines: Optional[int] = None,
               deadline_ms: Optional[int] = None) -> Dict[str, Any]:
        self._check_read()
        state = self.sessions[session_id]
        lines = state.screen_text.splitlines() if state.screen_text else []
        if max_lines and len(lines) > int(max_lines):
            lines = lines[-int(max_lines):]
        return {"text": "\n".join(lines), "cursor_x": state.cursor_x, "cursor_y": state.cursor_y,
                "cols": state.cols, "rows": state.rows, "rev": state.screen_rev}

    def scrollback(self, session_id: str, max_lines: Optional[int] = None,
                   deadline_ms: Optional[int] = None) -> Dict[str, Any]:
        self._check_read()
        state = self.sessions[session_id]
        lines = state.screen_text.splitlines() if state.screen_text else []
        if max_lines and len(lines) > int(max_lines):
            lines = lines[-int(max_lines):]
        return {"text": "\n".join(lines), "rev": state.rev}

    def raw(self, session_id: str, deadline_ms: Optional[int] = None) -> Dict[str, Any]:
        self._check_read()
        state = self.sessions[session_id]
        return {"text": state.ring, "bytes": state.ring_bytes, "rev": state.rev}

    def replay(self, session_id: str, deadline_ms: Optional[int] = None) -> str:
        return self.sessions[session_id].ring

    def attach_queue(self, session_id: str, max_bytes: Optional[int] = None) -> Tuple[str, int]:
        state = self.sessions[session_id]
        handle = self._next_handle
        self._next_handle += 1
        queue = FakeTerminalQueue()
        state.queues.append(queue)
        self.handles[handle] = (session_id, queue)
        return state.ring, handle

    def take_frames(self, handle: int) -> Tuple[List[bytes], int, bool]:
        _session_id, queue = self.handles[handle]
        frames, queue.frames = queue.frames, []
        return frames, queue.dropped, queue.closed

    def detach_queue(self, handle: int) -> bool:
        self.detach_calls.append(handle)
        if self.block_detach > 0:
            self.block_detach -= 1
            raise TimeoutError(f"detach_queue({handle}) missed the registry deadline")
        return self.handles.pop(handle, None) is not None

    def retire(self, session_id: str) -> bool:
        self.retire_calls.append(session_id)
        if session_id in self.resume_in_flight:
            raise ValueError(f"session {session_id} is being resumed")
        if self.block_retire > 0:
            self.block_retire -= 1
            raise TimeoutError(f"retire({session_id}) missed the table deadline")
        state = self.sessions.get(session_id)
        if state is None:
            return False
        if state.alive:
            raise ValueError(f"session {session_id} is still running")
        if not state.death_drained:
            raise ValueError(f"session {session_id}'s exit record has not been drained")
        del self.sessions[session_id]
        for queue in state.queues:
            queue.closed = True
        self.retired.append(session_id)
        return True

    def watch_screen(self, session_id: str, max_lines: Optional[int] = None,
                     interval_ms: int = 250) -> None:
        state = self.sessions[session_id]
        state.watched = True
        state.watched_max_lines = None if max_lines is None else int(max_lines)
        state.watch_calls.append(int(max_lines or 0))

    def unwatch(self, session_id: str) -> None:
        self.sessions[session_id].watched = False

    def snapshot_all(self, max_lines: Optional[int] = None, max_bytes: Optional[int] = None,
                     budget_ms: int = 8000) -> List[Dict[str, Any]]:
        self.calls.append("snapshot_all")
        self.snapshot_budgets.append(int(budget_ms))
        if self.snapshot_delay:
            time.sleep(self.snapshot_delay)
        out = []
        for state in self.sessions.values():
            out.append({
                "session_id": state.session_id,
                "alive": state.alive,
                "exit_code": state.exit_code,
                "pid": state.pid,
                "crashed": state.crashed,
                "degraded": state.degraded,
                "screen_text": state.screen_text,
                "scrollback_text": state.screen_text,
                "raw_text": state.ring,
                "raw_rev": state.rev,
                "raw_bytes": state.ring_bytes,
            })
        return out

    def stats(self) -> Dict[str, Any]:
        return {"sessions": len(self.sessions), "alive": sum(1 for s in self.sessions.values() if s.alive),
                "crashed": 0, "bytes_total": 0, "chunks_total": 0, "dropped_bytes": 0,
                "notify_calls": 0, "panics": 0}

    def shutdown(self) -> None:
        self.calls.append("shutdown")
        if self.shutdown_delay:
            time.sleep(self.shutdown_delay)
        self.shut_down = True

    # ---- internals -------------------------------------------------------------------------

    def _check_read(self) -> None:
        """One scripted table-read deadline: the core's `session()` deadline raises `TimeoutError`."""
        if self.read_timeouts > 0:
            self.read_timeouts -= 1
            raise TimeoutError("the core's session table missed the read deadline")

    def _next_generation(self, state: FakeTerminalSession) -> None:
        previous = self.sessions.get(state.session_id)
        state.generation = 1 if previous is None else previous.generation + 1
        state.death_drained = False

    def _record(self, state: FakeTerminalSession, screen_text: Optional[str] = None,
                urgent: bool = False) -> Dict[str, Any]:
        return {
            "session_id": state.session_id,
            "alive": state.alive,
            "exit_code": state.exit_code,
            "pid": state.pid,
            "bytes_total": state.ring_bytes,
            "chunks_total": state.rev,
            "last_output_at": state.last_output_at,
            # `urgent` is the core's own flag for a death record; the hub reads no such key.
            "dropped_bytes": state.dropped_bytes,
            "crashed": state.crashed,
            "degraded": state.degraded,
            "screen_rev": state.screen_rev,
            "raw_rev": state.rev,
            "screen_text": screen_text,
        }

    def _deliver(self, state: FakeTerminalSession, text: str) -> None:
        for queue in state.queues:
            queue.frames.append(text.encode("utf-8"))
