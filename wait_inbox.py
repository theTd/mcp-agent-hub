"""Opaque, durable wait handles for the parent-agent control plane.

The event table remains the audit/console history.  This module is the parent-facing
delivery inbox: it owns the session set, request/result deduplication, leases and the
small state machine returned by ``GET /api/wait/<handle>``.  A caller never sees a
global event sequence or a session id in the wait URL.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional


WAKE_EVENT_TYPES = frozenset({
    "approval_request", "question", "delivery_failed", "turn_completed",
    "session_exited", "exited", "crashed", "hub_shutdown",
})
ASK_EVENT_TYPES = frozenset({"approval_request", "question"})
TERMINAL_FAILURE_TYPES = frozenset({"crashed", "session_exited", "exited"})
# A parent's live message the hub could not submit, or could not prove submitted: the worker has not
# read it. Owner decision `hub-friction-1116` (undelivered-wake, 2026-11-16): it wakes the handle with
# state=undelivered and leaves it waiting (the worker is still alive), never failed.
UNDELIVERED_EVENT_TYPE = "delivery_failed"


def undelivered_entry(event: Dict[str, Any]) -> Dict[str, Any]:
    """The parent-facing view of one undelivered message (a delivery_failed event)."""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return {
        "session_id": event.get("session_id"),
        "message_id": data.get("message_id") or event.get("request_id"),
        "reason": data.get("reason") or event.get("content") or "undelivered",
        "confirmed_failure": bool(data.get("confirmed_failure")),
        "session_status": data.get("session_status"),
        "recovery_hint": data.get("recovery_hint") or "",
        "at": data.get("at") or event.get("timestamp"),
    }


def with_wait_outcome(entry: Dict[str, Any]) -> Dict[str, Any]:
    """One result entry carrying the ended turn's outcome: answered, error or aborted.

    The turn_completed event's ``data`` (record_worker_event copies it) names the outcome; an
    entry without one is an ordinary answer.  An error entry also carries its classified error.
    """
    entry = dict(entry)
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    outcome = str(data.get("outcome") or entry.get("outcome") or "answered")
    if outcome not in ("error", "aborted"):
        outcome = "answered"
    entry["outcome"] = outcome
    if outcome == "error":
        error = entry.get("error") if isinstance(entry.get("error"), dict) else {}
        entry["error"] = {
            "kind": str(error.get("kind") or data.get("error_kind") or "other"),
            "retryable": bool(error.get("retryable", data.get("retryable"))),
            "message": str(error.get("message") or data.get("error") or ""),
        }
    else:
        entry.pop("error", None)
    return entry


def engine_errors_of(results: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The done result's engine errors, one flat entry per session (top-level ``engine_errors``)."""
    errors = []
    for result in results:
        if not isinstance(result, dict) or result.get("outcome") != "error":
            continue
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        errors.append({
            "session_id": result.get("session_id"),
            "kind": str(error.get("kind") or "other"),
            "retryable": bool(error.get("retryable")),
            "message": str(error.get("message") or ""),
        })
    return errors


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class WaitHandleError(ValueError):
    """The opaque handle does not exist or is no longer usable."""


class WaitInbox:
    """Persistent wait inbox with one active long-poll lease per handle."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._condition = asyncio.Condition()
        self._lock = asyncio.Lock()
        self._waiters: Dict[str, tuple[str, asyncio.Future]] = {}
        self._state_provider: Optional[Callable[[List[str]], Awaitable[Dict[str, Any]]]] = None
        self._request_check: Optional[Callable[[str, str], bool]] = None
        self._init_db()

    def set_state_provider(self, provider: Callable[[List[str]], Awaitable[Dict[str, Any]]]) -> None:
        self._state_provider = provider

    def set_request_check(self, check: Callable[[str, str], bool]) -> None:
        """``check(session_id, request_id)`` is True while that request still awaits the parent.

        The inbox asks it before it hands out an ask, so a request answered by any path
        (respond_to_subagent, send_to_subagent, the console, an automatic rule, the native
        TUI) or gone with its session leaves the wait URL at once.
        """
        self._request_check = check

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=2.0, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wait_handles (
                    handle TEXT PRIMARY KEY,
                    session_ids_json TEXT NOT NULL,
                    turn_ids_json TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'all_settled',
                    state TEXT NOT NULL DEFAULT 'waiting',
                    result_json TEXT,
                    result_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    closed_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS wait_inbox_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    handle TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    request_id TEXT,
                    result_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(handle, event_key)
                );
                CREATE INDEX IF NOT EXISTS idx_wait_events_ready
                    ON wait_inbox_events(handle, consumed, id);
                """
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _load(raw: Optional[str], default: Any) -> Any:
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def _row(self, handle: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM wait_handles WHERE handle=?", (handle,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["session_ids"] = self._load(item.pop("session_ids_json"), [])
            item["turn_ids"] = self._load(item.pop("turn_ids_json"), {})
            item["result"] = self._load(item.pop("result_json"), None)
            return item

    def session_ids_for(self, handle: str) -> List[str]:
        """Return the sessions bound to a handle or wait URL."""
        key = str(handle or "").rstrip("/").rsplit("/", 1)[-1]
        row = self._row(key)
        if not row:
            raise WaitHandleError("unknown wait handle")
        return [str(item) for item in row.get("session_ids") or []]

    @staticmethod
    def _new_handle() -> str:
        return "wh_" + secrets.token_urlsafe(24)

    async def create(
        self,
        session_ids: Iterable[str],
        turn_ids: Optional[Dict[str, str]] = None,
        *,
        mode: str = "all_settled",
    ) -> Dict[str, Any]:
        ids = list(dict.fromkeys(str(x) for x in session_ids if str(x)))
        if not ids:
            raise ValueError("at least one session is required for a wait handle")
        handle = self._new_handle()
        now = time.time()
        turns = dict(turn_ids or {})
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO wait_handles(handle,session_ids_json,turn_ids_json,mode,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (handle, self._json(ids), self._json(turns), mode, "waiting", now, now),
            )
        return {"wait_handle": handle, "wait_url": f"/api/wait/{handle}", "session_ids": ids, "mode": mode}

    async def for_sessions(self, session_ids: Iterable[str]) -> Optional[Dict[str, Any]]:
        ids = set(str(x) for x in session_ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wait_handles WHERE state != 'closed' ORDER BY created_at DESC"
            ).fetchall()
        for raw in rows:
            current = set(self._load(raw["session_ids_json"], []))
            if current == ids:
                return self._row(raw["handle"])
        return None

    async def ensure_for_sessions(
        self,
        session_ids: Iterable[str],
        turn_ids: Optional[Dict[str, str]] = None,
        *,
        mode: str = "all_settled",
    ) -> Dict[str, Any]:
        existing = await self.for_sessions(session_ids)
        if existing and existing.get("mode") == mode:
            return {
                "wait_handle": existing["handle"],
                "wait_url": f"/api/wait/{existing['handle']}",
                "session_ids": existing["session_ids"],
                "mode": existing["mode"],
            }
        return await self.create(session_ids, turn_ids, mode=mode)

    async def _notify(self, handle: Optional[str] = None) -> None:
        async with self._condition:
            self._condition.notify_all()
        if handle:
            waiter = self._waiters.get(handle)
            if waiter and not waiter[1].done():
                waiter[1].set_result(True)

    async def publish_event(self, event: Dict[str, Any]) -> None:
        """Add one audit event to each matching parent inbox, with stable dedup keys."""
        event_type = str(event.get("type") or "")
        if event_type not in WAKE_EVENT_TYPES:
            return
        sid = str(event.get("session_id") or "")
        request_id = event.get("request_id")
        result_id = event.get("result_id")
        if event_type == "turn_completed":
            result_id = result_id or f"result:{sid}:{event.get('turn_id') or event.get('seq') or 'current'}"
        key = str(request_id or result_id or f"event:{sid}:{event_type}:{event.get('seq') or event.get('timestamp')}")
        payload = dict(event)
        payload["result_id"] = result_id
        handles: List[str] = []
        with self._connect() as conn:
            rows = conn.execute("SELECT handle,session_ids_json,state,mode FROM wait_handles WHERE state IN ('waiting','ask')").fetchall()
            for row in rows:
                session_ids = self._load(row["session_ids_json"], [])
                if sid and sid not in session_ids:
                    continue
                # A completion is terminal for a single/any-settled handle, but a group
                # handle must remain subscribed until every member has reported.  Marking
                # an all-settled handle done here would exclude later member completions
                # from the durable inbox and make restart recovery depend on the live
                # state provider.
                group_waiting = (
                    event_type == "turn_completed"
                    and row["state"] == "waiting"
                    and row["mode"] == "all_settled"
                    and len(session_ids) > 1
                )
                state = "ask" if event_type in ("approval_request", "question") else (
                    "failed" if event_type in TERMINAL_FAILURE_TYPES else "waiting"
                    if group_waiting else "done" if event_type == "turn_completed" else "waiting"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO wait_inbox_events(handle,event_key,event_type,request_id,result_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (row["handle"], key, event_type, request_id, result_id, self._json(payload), time.time()),
                )
                if state in ("ask", "failed"):
                    conn.execute("UPDATE wait_handles SET state=?,updated_at=? WHERE handle=? AND state IN ('waiting','ask')", (state, time.time(), row["handle"]))
                handles.append(row["handle"])
        for handle in handles:
            await self._notify(handle)

    async def publish_shutdown(self) -> None:
        await self.publish_event({"type": "hub_shutdown", "session_id": "", "content": "Hub shutdown"})

    def _request_open(self, event: Dict[str, Any], request_id: Optional[str]) -> bool:
        if self._request_check is None:
            return True
        return bool(self._request_check(str(event.get("session_id") or ""), str(request_id or event.get("request_id") or "")))

    async def update_turn(self, handle: str, session_id: str, turn_id: str) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT turn_ids_json FROM wait_handles WHERE handle=?", (handle,)).fetchone()
            if not row:
                raise WaitHandleError("unknown wait handle")
            turns = self._load(row["turn_ids_json"], {})
            turns[str(session_id)] = str(turn_id)
            # A turn end of this session's earlier turn that nobody collected is superseded: the new
            # message must not be answered with it (a codex deny ends the turn at once, and parents
            # often send new instructions right after the deny without reading the wait URL).
            for event in conn.execute(
                "SELECT id,payload_json FROM wait_inbox_events WHERE handle=? AND consumed=0 AND event_type='turn_completed'",
                (handle,),
            ).fetchall():
                payload = self._load(event["payload_json"], {})
                if str(payload.get("session_id") or "") == str(session_id) and str(payload.get("turn_id") or "") != str(turn_id):
                    conn.execute("UPDATE wait_inbox_events SET consumed=1 WHERE id=?", (event["id"],))
            conn.execute("UPDATE wait_handles SET turn_ids_json=?,state='waiting',updated_at=? WHERE handle=?", (self._json(turns), time.time(), handle))

    async def _next_event(self, handle: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wait_inbox_events WHERE handle=? AND consumed=0 ORDER BY id", (handle,)
            ).fetchall()
            for row in rows:
                item = self._load(row["payload_json"], {})
                item["result_id"] = row["result_id"] or item.get("result_id")
                if row["event_type"] in ASK_EVENT_TYPES:
                    # An ask is repeated until it is answered, then never again: the inbox
                    # checks the live request instead of trusting one answer path to clear it.
                    if self._request_open(item, row["request_id"]):
                        return item
                    conn.execute("UPDATE wait_inbox_events SET consumed=1 WHERE id=?", (row["id"],))
                    conn.execute(
                        "UPDATE wait_handles SET state='waiting',updated_at=? WHERE handle=? AND state='ask'",
                        (time.time(), handle),
                    )
                    continue
                # At-least-once delivery uses the stable result/request id.  Marking here is safe for
                # retries because a disconnect can replay the persisted result through the handle row.
                conn.execute("UPDATE wait_inbox_events SET consumed=1 WHERE id=?", (row["id"],))
                return item
            return None

    @staticmethod
    def _ask_entry(event: Dict[str, Any]) -> Dict[str, Any]:
        """Return the stable parent-facing shape while retaining the raw event fields."""
        entry = dict(event)
        entry.setdefault("session_id", event.get("session_id"))
        entry.setdefault("request_id", event.get("request_id"))
        entry.setdefault("kind", event.get("type"))
        entry.setdefault("prompt", event.get("prompt") or event.get("content") or event.get("question") or event.get("command"))
        entry.setdefault("command", event.get("command"))
        entry.setdefault("decisions", event.get("decisions") or event.get("options"))
        entry.setdefault("options", event.get("options") or event.get("decisions"))
        entry.setdefault("result_id", event.get("result_id") or event.get("request_id"))
        return entry

    async def _snapshot(self, handle: str) -> Dict[str, Any]:
        row = self._row(handle)
        if not row:
            raise WaitHandleError("unknown wait handle")
        event = await self._next_event(handle)
        if event:
            kind = event.get("type")
            if kind in ("approval_request", "question"):
                asks = await self._all_open_asks(handle)
                if not asks:
                    asks = [self._ask_entry(event)]
                first = asks[0]
                return {"state": "ask", "requests": asks,
                        "result_id": first.get("result_id") or first.get("request_id")}
            if kind == "turn_completed":
                # A group handle is all-settled by default.  A member completion is cached in the
                # inbox, then the state provider decides whether every member has completed.
                if len(row["session_ids"]) == 1 or row.get("mode") != "all_settled":
                    result = with_wait_outcome(dict(event))
                    result.setdefault("final_response", event.get("content", ""))
                    result_id = event.get("result_id") or f"result:{event.get('session_id')}:{event.get('turn_id') or event.get('seq')}"
                    with self._connect() as conn:
                        conn.execute("UPDATE wait_handles SET result_json=?,result_id=?,state='done',updated_at=? WHERE handle=?", (self._json(result), result_id, time.time(), handle))
                    return self._done_payload(result_id, result.get("final_response", ""), [result])
                if self._state_provider:
                    state = await self._state_provider(list(row["session_ids"]))
                    if state.get("state") == "done":
                        result = with_wait_outcome(dict(event))
                        result.setdefault("final_response", event.get("content", ""))
                        result_id = state.get("result_id") or event.get("result_id") or f"result:{event.get('session_id')}:{event.get('turn_id') or event.get('seq')}"
                        results = [with_wait_outcome(item) for item in (state.get("results") or [result])]
                        saved = {**state, "results": results}
                        with self._connect() as conn:
                            conn.execute("UPDATE wait_handles SET result_json=?,result_id=?,state='done',updated_at=? WHERE handle=?", (self._json(saved), result_id, time.time(), handle))
                        return self._done_payload(
                            result_id, state.get("final_response") or result.get("final_response", ""), results
                        )
                return {"state": "waiting", "wait_handle": handle}
            if kind == UNDELIVERED_EVENT_TYPE:
                # Every undelivered message waiting in the inbox comes back at once; the handle
                # stays waiting, so the next GET waits for the turn's result again.
                events = [event] + await self._take_events(handle, UNDELIVERED_EVENT_TYPE)
                return {"state": "undelivered", "result_id": event.get("result_id"),
                        "messages": [undelivered_entry(item) for item in events]}
            if kind in TERMINAL_FAILURE_TYPES or kind == "hub_shutdown":
                message = event.get("content") or kind
                return {
                    "state": "failed",
                    "result_id": event.get("result_id") or event.get("request_id"),
                    "errors": [{"type": kind, "message": message, "event": event}],
                }
        if row["state"] == "done" and row.get("result") is not None:
            saved = row["result"] or {}
            results = [with_wait_outcome(item) for item in (saved.get("results") or [saved])]
            return self._done_payload(
                row.get("result_id"), saved.get("final_response") or saved.get("content", ""), results
            )
        if row["state"] == "failed":
            return {
                "state": "failed",
                "result_id": row.get("result_id"),
                "errors": [{"message": row.get("closed_reason") or "wait failed"}],
            }
        if self._state_provider:
            state = await self._state_provider(list(row["session_ids"]))
            if state.get("state") in ("done", "failed", "ask"):
                if state.get("state") == "failed" and not state.get("errors"):
                    state = {**state, "errors": [{"message": state.get("error") or "wait failed"}]}
                if state.get("state") == "done":
                    results = [with_wait_outcome(item) for item in (state.get("results") or [])]
                    # A group has no single answer (the provider's value is None); keep it as the
                    # provider state had it instead of turning it into an empty string.
                    return {**state, **self._done_payload(
                        state.get("result_id"), state.get("final_response"), results
                    )}
                return state
        return {"state": "waiting", "wait_handle": handle}

    @staticmethod
    def _done_payload(result_id: Any, final_response: Any, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """A done result keeps its state; every entry names its outcome, errors are listed once."""
        payload = {"state": "done", "result_id": result_id, "final_response": final_response, "results": results}
        errors = engine_errors_of(results)
        if errors:
            payload["engine_errors"] = errors
        return payload

    async def _take_events(self, handle: str, event_type: str) -> List[Dict[str, Any]]:
        """Consume and return every unconsumed event of `event_type` in the handle's inbox, in order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wait_inbox_events WHERE handle=? AND consumed=0 AND event_type=? ORDER BY id",
                (handle, event_type),
            ).fetchall()
            items = []
            for row in rows:
                conn.execute("UPDATE wait_inbox_events SET consumed=1 WHERE id=?", (row["id"],))
                item = self._load(row["payload_json"], {})
                item["result_id"] = row["result_id"] or item.get("result_id")
                items.append(item)
        return items

    async def _all_open_asks(self, handle: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wait_inbox_events WHERE handle=? AND consumed=0 AND event_type IN ('approval_request','question') ORDER BY id",
                (handle,),
            ).fetchall()
        asks = []
        for row in rows:
            item = self._load(row["payload_json"], {})
            item["request_id"] = row["request_id"] or item.get("request_id")
            item["result_id"] = row["result_id"] or item.get("result_id") or item.get("request_id")
            if self._request_open(item, row["request_id"]):
                asks.append(self._ask_entry(item))
        return asks

    async def wait(self, handle: str, timeout: float = 25.0, ask_coalesce_ms: int = 0) -> Dict[str, Any]:
        timeout = max(0.1, min(float(timeout), 300.0))
        ask_coalesce_ms = max(0, min(int(ask_coalesce_ms), 5000))
        async with self._lock:
            if not self._row(handle):
                raise WaitHandleError("unknown wait handle")
            prior = self._waiters.get(handle)
            if prior:
                if not prior[1].done():
                    prior[1].set_result({"state": "superseded", "wait_handle": handle})
            token = secrets.token_urlsafe(12)
            future = asyncio.get_running_loop().create_future()
            self._waiters[handle] = (token, future)
        deadline = time.monotonic() + timeout
        coalesced = False
        try:
            while True:
                current = await self._snapshot(handle)
                if current.get("state") != "waiting":
                    if current.get("state") == "ask" and ask_coalesce_ms and not coalesced:
                        coalesced = True
                        remain = max(0.0, deadline - time.monotonic())
                        if remain:
                            await asyncio.sleep(min(ask_coalesce_ms / 1000.0, remain))
                        current_waiter = self._waiters.get(handle)
                        if not current_waiter or current_waiter[0] != token:
                            return {"state": "superseded", "wait_handle": handle}
                        if future.done() and future.result() is not True:
                            return dict(future.result())
                        # Read a fresh snapshot even when the window used up the timeout: an ask
                        # still open must be returned, never hidden behind state=waiting.
                        continue
                    return {"wait_handle": handle, **current}
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return {"wait_handle": handle, "state": "waiting"}
                try:
                    await asyncio.wait_for(asyncio.shield(future), remain)
                except asyncio.TimeoutError:
                    return {"wait_handle": handle, "state": "waiting"}
                current_waiter = self._waiters.get(handle)
                if not current_waiter or current_waiter[0] != token:
                    if future.done() and isinstance(future.result(), dict):
                        return dict(future.result())
                    return {"state": "superseded", "wait_handle": handle}
                if not future.done() or future.result() is True:
                    future = asyncio.get_running_loop().create_future()
                    self._waiters[handle] = (token, future)
                    continue
                return dict(future.result())
        finally:
            current = self._waiters.get(handle)
            if current and current[0] == token:
                self._waiters.pop(handle, None)

    async def close(self) -> None:
        for handle, waiter in list(self._waiters.items()):
            if not waiter[1].done():
                waiter[1].set_result({"state": "superseded", "wait_handle": handle})
        self._waiters.clear()
