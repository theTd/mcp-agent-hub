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
TERMINAL_FAILURE_TYPES = frozenset({"delivery_failed", "crashed", "session_exited", "exited"})


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
        self._init_db()

    def set_state_provider(self, provider: Callable[[List[str]], Awaitable[Dict[str, Any]]]) -> None:
        self._state_provider = provider

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
            if waiter:
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

    async def resolve_request(self, handle: str, request_id: str) -> bool:
        if not request_id:
            return False
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM wait_handles WHERE handle=?", (handle,)).fetchone()
            if not row:
                raise WaitHandleError("unknown wait handle")
            cur = conn.execute(
                "UPDATE wait_inbox_events SET consumed=1 WHERE handle=? AND request_id=? AND consumed=0",
                (handle, request_id),
            )
            conn.execute("UPDATE wait_handles SET state='waiting',updated_at=? WHERE handle=? AND state='ask'", (time.time(), handle))
            return cur.rowcount > 0

    async def update_turn(self, handle: str, session_id: str, turn_id: str) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT turn_ids_json FROM wait_handles WHERE handle=?", (handle,)).fetchone()
            if not row:
                raise WaitHandleError("unknown wait handle")
            turns = self._load(row["turn_ids_json"], {})
            turns[str(session_id)] = str(turn_id)
            conn.execute("UPDATE wait_handles SET turn_ids_json=?,state='waiting',updated_at=? WHERE handle=?", (self._json(turns), time.time(), handle))

    async def _next_event(self, handle: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM wait_inbox_events WHERE handle=? AND consumed=0 ORDER BY id LIMIT 1", (handle,)
            ).fetchone()
            if not row:
                return None
            item = self._load(row["payload_json"], {})
            item["result_id"] = row["result_id"] or item.get("result_id")
            # At-least-once delivery uses the stable result/request id.  Marking here is safe for
            # retries because a disconnect can replay the persisted result through the handle row.
            if row["event_type"] not in ("approval_request", "question"):
                conn.execute("UPDATE wait_inbox_events SET consumed=1 WHERE id=?", (row["id"],))
            return item

    async def _snapshot(self, handle: str) -> Dict[str, Any]:
        row = self._row(handle)
        if not row:
            raise WaitHandleError("unknown wait handle")
        event = await self._next_event(handle)
        if event:
            kind = event.get("type")
            if kind in ("approval_request", "question"):
                return {"state": "ask", "requests": [event], "result_id": event.get("result_id") or event.get("request_id")}
            if kind == "turn_completed":
                # A group handle is all-settled by default.  A member completion is cached in the
                # inbox, then the state provider decides whether every member has completed.
                if len(row["session_ids"]) == 1 or row.get("mode") != "all_settled":
                    result = dict(event)
                    result.setdefault("final_response", event.get("content", ""))
                    result_id = event.get("result_id") or f"result:{event.get('session_id')}:{event.get('turn_id') or event.get('seq')}"
                    with self._connect() as conn:
                        conn.execute("UPDATE wait_handles SET result_json=?,result_id=?,state='done',updated_at=? WHERE handle=?", (self._json(result), result_id, time.time(), handle))
                    return {"state": "done", "result_id": result_id, "final_response": result.get("final_response", ""), "results": [result]}
                if self._state_provider:
                    state = await self._state_provider(list(row["session_ids"]))
                    if state.get("state") == "done":
                        result = dict(event)
                        result.setdefault("final_response", event.get("content", ""))
                        result_id = state.get("result_id") or event.get("result_id") or f"result:{event.get('session_id')}:{event.get('turn_id') or event.get('seq')}"
                        with self._connect() as conn:
                            conn.execute("UPDATE wait_handles SET result_json=?,result_id=?,state='done',updated_at=? WHERE handle=?", (self._json(state), result_id, time.time(), handle))
                        return {"state": "done", "result_id": result_id, "final_response": state.get("final_response") or result.get("final_response", ""), "results": state.get("results") or [result]}
                return {"state": "waiting", "wait_handle": handle}
            if kind in TERMINAL_FAILURE_TYPES or kind == "hub_shutdown":
                message = event.get("content") or kind
                return {
                    "state": "failed",
                    "result_id": event.get("result_id") or event.get("request_id"),
                    "errors": [{"type": kind, "message": message, "event": event}],
                }
        if row["state"] == "done" and row.get("result") is not None:
            saved = row["result"] or {}
            return {"state": "done", "result_id": row.get("result_id"), "final_response": saved.get("final_response") or saved.get("content", ""), "results": saved.get("results") or [saved]}
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
                return state
        return {"state": "waiting", "wait_handle": handle}

    async def wait(self, handle: str, timeout: float = 25.0) -> Dict[str, Any]:
        timeout = max(0.1, min(float(timeout), 120.0))
        async with self._lock:
            if not self._row(handle):
                raise WaitHandleError("unknown wait handle")
            prior = self._waiters.get(handle)
            if prior:
                prior[1].set_result({"state": "superseded", "wait_handle": handle})
            token = secrets.token_urlsafe(12)
            future = asyncio.get_running_loop().create_future()
            self._waiters[handle] = (token, future)
        deadline = time.monotonic() + timeout
        try:
            while True:
                current = await self._snapshot(handle)
                if current.get("state") != "waiting":
                    return {"wait_handle": handle, **current}
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return {"wait_handle": handle, "state": "waiting"}
                try:
                    await asyncio.wait_for(asyncio.shield(future), remain)
                except asyncio.TimeoutError:
                    return {"wait_handle": handle, "state": "waiting"}
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
            waiter[1].set_result({"state": "superseded", "wait_handle": handle})
        self._waiters.clear()
