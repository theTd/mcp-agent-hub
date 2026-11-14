"""A fake of the Rust terminal core's `Core` calls, for the hub's tests (design §10).

`AgentSession` and `HubSessionManager` reach the terminal only through the calls
`hub_terminal_core`'s `lib.rs` exposes (design §5.2, `seam-coverage.md`), so a test that needs
ordered keystrokes, a scripted screen or a controlled drain swaps the core for this object instead
of a PTY.  It is deliberately not a second terminal model: the screen is a string the test sets, the
ring is the text the test feeds, and the only ordering rule is arrival order.

What it keeps faithful, because the hub's behaviour depends on it:

- `spawn`/`attach_text` replace a dead incarnation of the same id and hand its sockets the prefix
  or seed frame first (`rtc-sinks-per-id`), so a resume test can check old bytes, banner, new bytes;
- `kill` records the death and queues the one death record; `drain` returns queued records and
  clears them, so `_on_core_tick` sees exactly what the core would publish;
- `retire` refuses a live id and one whose death record has not been drained (`rtc-retire`);
- `take_frames` is non-blocking and reports a closed queue after `retire`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


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
        self.alive = True
        self.exit_code: Optional[int] = None
        self.crashed = False
        self.degraded = False
        self.dropped_bytes = 0
        self.death_drained = False
        self.watched = False
        self.flags: Dict[str, Any] = {}
        self.seed_history: Optional[str] = None
        self.ws_prefix: Optional[str] = None
        self.queues: List[FakeTerminalQueue] = []


class FakeTerminal:
    """The `Core` surface the hub calls, with per-session lists a test can assert on."""

    def __init__(self) -> None:
        self.sessions: Dict[str, FakeTerminalSession] = {}
        self.records: List[Dict[str, Any]] = []
        self.handles: Dict[int, Tuple[str, FakeTerminalQueue]] = {}
        self._next_handle = 1
        self.retired: List[str] = []
        self.retire_calls: List[str] = []
        self.kill_calls: List[Tuple[str, int]] = []
        self.shut_down = False

    # ---- helpers a test uses to script the session ----------------------------------------

    def session(self, session_id: str) -> FakeTerminalSession:
        return self.sessions[session_id]

    def set_screen(self, session_id: str, text: str, cursor_x: int = 0, cursor_y: int = 0) -> None:
        state = self.sessions[session_id]
        state.screen_text = text
        state.cursor_x = cursor_x
        state.cursor_y = cursor_y
        state.screen_rev += 1

    def output(self, session_id: str, text: str) -> None:
        """Output as the reader files it: the ring grows, the revision moves, a record is queued,
        and every attached socket gets the same bytes."""
        state = self.sessions[session_id]
        state.ring += text
        state.ring_bytes = len(state.ring.encode("utf-8", "replace"))
        state.rev += 1
        # The same chunk the ring took goes to every attached socket (`rtc-sinks-per-id`).
        self._deliver(state, text)
        self.records.append(self._record(state, screen_text=state.screen_text if state.watched else None))

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
        # The id's fan-out is handed over: a prefix/seed reaches the sockets that were already attached.
        if previous is not None:
            state.queues = previous.queues
        if ws_prefix:
            self._deliver(state, ws_prefix)
        self.sessions[session_id] = state
        self.records.append(self._record(state))
        return {"pid": state.pid, "cols": state.cols, "rows": state.rows, "elapsed_ms": 0.0}

    def attach_text(self, session_id: str, cols: int = 80, rows: int = 24,
                    flags: Optional[Dict[str, Any]] = None, seed_history: Optional[str] = None,
                    seed: Optional[str] = None) -> Dict[str, Any]:
        previous = self.sessions.get(session_id)
        reply = self.spawn(session_id, ["text"], cols=cols, rows=rows, flags=flags,
                           seed_history=seed_history, ws_prefix=seed)
        state = self.sessions[session_id]
        state.pid = 0
        if previous is not None:
            state.queues = previous.queues
        return {"pid": 0, "cols": state.cols, "rows": state.rows}

    def feed(self, session_id: str, text: str, deadline_ms: Optional[int] = None) -> int:
        state = self.sessions[session_id]
        state.ring += text
        state.ring_bytes = len(state.ring.encode("utf-8", "replace"))
        state.rev += 1
        state.screen_rev += 1
        self._deliver(state, text)
        self.records.append(self._record(state, screen_text=state.screen_text if state.watched else None))
        return state.rev

    def write(self, session_id: str, text: str, deadline_ms: Optional[int] = None) -> int:
        state = self.sessions[session_id]
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
        state.exit_code = 0
        state.death_drained = False
        self.records.append(self._record(state, urgent=True))
        return state.exit_code

    def kill_all(self, grace_ms: Optional[int] = None) -> None:
        for session_id in list(self.sessions):
            try:
                self.kill(session_id, grace_ms)
            except KeyError:
                pass

    def drain(self) -> List[Dict[str, Any]]:
        records, self.records = self.records, []
        for record in records:
            state = self.sessions.get(str(record.get("session_id") or ""))
            if state is not None and record.get("alive") is False:
                state.death_drained = True
        return records

    def raw_tail(self, session_id: str, max_chars: Optional[int] = None,
                 deadline_ms: Optional[int] = None) -> str:
        if not max_chars or max_chars <= 0:
            return ""
        return self.sessions[session_id].ring[-int(max_chars):]

    def screen(self, session_id: str, max_lines: Optional[int] = None,
               deadline_ms: Optional[int] = None) -> Dict[str, Any]:
        state = self.sessions[session_id]
        lines = state.screen_text.splitlines() if state.screen_text else []
        if max_lines and len(lines) > int(max_lines):
            lines = lines[-int(max_lines):]
        return {"text": "\n".join(lines), "cursor_x": state.cursor_x, "cursor_y": state.cursor_y,
                "cols": state.cols, "rows": state.rows, "rev": state.screen_rev}

    def scrollback(self, session_id: str, max_lines: Optional[int] = None,
                   deadline_ms: Optional[int] = None) -> Dict[str, Any]:
        state = self.sessions[session_id]
        lines = state.screen_text.splitlines() if state.screen_text else []
        if max_lines and len(lines) > int(max_lines):
            lines = lines[-int(max_lines):]
        return {"text": "\n".join(lines), "rev": state.rev}

    def raw(self, session_id: str, deadline_ms: Optional[int] = None) -> Dict[str, Any]:
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
        return self.handles.pop(handle, None) is not None

    def retire(self, session_id: str) -> bool:
        self.retire_calls.append(session_id)
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
        self.sessions[session_id].watched = True

    def unwatch(self, session_id: str) -> None:
        self.sessions[session_id].watched = False

    def snapshot_all(self, max_lines: Optional[int] = None, max_bytes: Optional[int] = None,
                     budget_ms: int = 8000) -> List[Dict[str, Any]]:
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
        self.shut_down = True

    # ---- internals -------------------------------------------------------------------------

    def _record(self, state: FakeTerminalSession, screen_text: Optional[str] = None,
                urgent: bool = False) -> Dict[str, Any]:
        return {
            "session_id": state.session_id,
            "alive": state.alive,
            "exit_code": state.exit_code,
            "pid": state.pid,
            "bytes_total": state.ring_bytes,
            "chunks_total": state.rev,
            "last_output_at": None,
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
