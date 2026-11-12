"""SQLite storage and transactions. Called only off the asyncio event loop."""
import json
import pickle
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional


class _ClosingCursor(sqlite3.Cursor):
    def fetchone(self):
        try:
            return super().fetchone()
        finally:
            self.close()

    def fetchall(self):
        try:
            return super().fetchall()
        finally:
            self.close()


class _ClosingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        cursor = self.cursor(factory=_ClosingCursor)
        try:
            cursor.execute(sql, parameters)
            if cursor.description is None:
                cursor.close()
            return cursor
        except BaseException:
            cursor.close()
            raise

    def executescript(self, sql):
        cursor = self.cursor(factory=_ClosingCursor)
        try:
            cursor.executescript(sql)
        finally:
            cursor.close()


END_REASON_LABELS = {
    "exited": "已退出",
    "crashed": "异常退出",
    "terminated": "已终止",
    "interrupted": "已中断",
}

ARCHIVE_COLUMNS = (
    "end_reason", "archived_at", "spawned_at", "profile_snapshot", "conversation_ref",
    "scrollback", "last_screen", "parent_messages", "resume_count", "last_resumed_at",
    "resumable", "resume_block_reason",
)


class HubDatabase:
    """SQLite persistence layer ensuring Sub-Agent trace, intermediate messages, and final_response survive process exit & hub restarts."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._writer = None
        self._writer_thread = None
        self._init_db()

    @staticmethod
    def _connect(path, *, timeout=0.1):
        conn = sqlite3.connect(str(path), timeout=timeout, factory=_ClosingConnection)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=FULL")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def _conn(self):
        if self._writer is not None and threading.get_ident() == self._writer_thread:
            yield self._writer
            return
        conn = self._connect(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def open_writer(self):
        self._writer_thread = threading.get_ident()
        self._writer = self._connect(self.db_path)

    def close_writer(self):
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def apply_batch(self, units):
        """One transaction; rollback retains every logical unit for an ordered retry."""
        conn = self._writer
        try:
            conn.execute("BEGIN IMMEDIATE")
            for operations in units:
                for operation in operations:
                    name, args, kwargs = pickle.loads(operation)
                    getattr(self, name)(*args, **kwargs)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _init_db(self):
        conn = self._connect(self.db_path, timeout=2.0)
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError("Hub database requires WAL mode")
        finally:
            conn.close()
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    profile_id TEXT,
                    profile_name TEXT,
                    kind TEXT,
                    title TEXT,
                    cwd TEXT,
                    status TEXT,
                    created_at REAL,
                    updated_at REAL,
                    ended_at REAL,
                    exit_code INTEGER,
                    step_count INTEGER DEFAULT 0,
                    current_activity TEXT DEFAULT '',
                    last_task TEXT DEFAULT '',
                    engine TEXT DEFAULT '',
                    effective_model TEXT,
                    effective_thinking_level TEXT,
                    final_response TEXT,
                    final_response_source TEXT,
                    files_changed_json TEXT DEFAULT '[]',
                    scrollback_text TEXT DEFAULT '',
                    transcript_path TEXT DEFAULT ''
                    ,turn_id TEXT DEFAULT ''
                    ,wait_handle TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    step_num INTEGER,
                    timestamp REAL,
                    tool_name TEXT,
                    input_summary TEXT,
                    output_summary TEXT,
                    status TEXT,
                    source TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id, id);

                CREATE TABLE IF NOT EXISTS intermediate_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    step_num INTEGER,
                    timestamp REAL,
                    content TEXT,
                    source TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_msgs_session ON intermediate_messages(session_id, id);

                CREATE TABLE IF NOT EXISTS worker_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    timestamp REAL,
                    type TEXT,
                    category TEXT,
                    content TEXT,
                    status TEXT,
                    files_changed_json TEXT,
                    request_id TEXT
                );
                """
            )
            # B9: persisted history must say which engine/model really ran, and which process status ended.
            # Archive spec §3: an ended session keeps everything needed to view and revive it. Old rows
            # get NULLs; every ALTER that already ran fails harmlessly, so a second start is a no-op.
            for ddl in (
                "ALTER TABLE sessions ADD COLUMN transcript_path TEXT DEFAULT ''",
                "ALTER TABLE sessions ADD COLUMN engine TEXT DEFAULT ''",
                "ALTER TABLE sessions ADD COLUMN effective_model TEXT",
                "ALTER TABLE sessions ADD COLUMN effective_thinking_level TEXT",
                "ALTER TABLE sessions ADD COLUMN end_reason TEXT",
                "ALTER TABLE sessions ADD COLUMN archived_at REAL",
                "ALTER TABLE sessions ADD COLUMN spawned_at REAL",
                "ALTER TABLE sessions ADD COLUMN profile_snapshot TEXT",
                "ALTER TABLE sessions ADD COLUMN conversation_ref TEXT",
                "ALTER TABLE sessions ADD COLUMN scrollback BLOB",
                "ALTER TABLE sessions ADD COLUMN last_screen TEXT DEFAULT ''",
                "ALTER TABLE sessions ADD COLUMN parent_messages TEXT",
                "ALTER TABLE sessions ADD COLUMN resume_count INTEGER DEFAULT 0",
                "ALTER TABLE sessions ADD COLUMN last_resumed_at REAL",
                "ALTER TABLE sessions ADD COLUMN resumable INTEGER",
                "ALTER TABLE sessions ADD COLUMN resume_block_reason TEXT",
                "ALTER TABLE sessions ADD COLUMN turn_id TEXT DEFAULT ''",
                "ALTER TABLE sessions ADD COLUMN wait_handle TEXT DEFAULT ''",
                # Transcript feed: structured tool fields (tool, call id, input, output, ok) of an event.
                "ALTER TABLE worker_events ADD COLUMN data_json TEXT",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as exc:
                    if 'duplicate column name' not in str(exc).lower():
                        raise

    def get_max_seq(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM worker_events").fetchone()
            return int(row["m"]) if row else 0

    def upsert_session(
        self,
        s_dict: Dict[str, Any],
        scrollback_text: str = "",
        archive: Optional[Dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ):
        """Inserts or refreshes one session row; `archive` carries the archive-spec columns (§3).

        Every archive column is COALESCEd, so a plain telemetry persist never erases what the final
        record already wrote. `resumable` is written whenever it is not None (0 must beat a stale 1),
        and its block reason is replaced in the same step so it can be cleared again.
        """
        arc = archive or {}
        values: Dict[str, Any] = {
            "session_id": s_dict["session_id"],
            "profile_id": s_dict["profile_id"],
            "profile_name": s_dict["profile_name"],
            "kind": s_dict["kind"],
            "title": s_dict["title"],
            "cwd": s_dict["cwd"],
            "status": s_dict["status"],
            "created_at": s_dict["created_at"],
            "updated_at": time.time() if timestamp is None else timestamp,
            "ended_at": s_dict.get("ended_at"),
            "exit_code": s_dict.get("exit_code"),
            "step_count": s_dict.get("step_count", 0),
            "current_activity": s_dict.get("current_activity", ""),
            "last_task": s_dict.get("last_task", ""),
            "engine": s_dict.get("engine") or "",
            "effective_model": s_dict.get("effective_model"),
            "effective_thinking_level": s_dict.get("effective_thinking_level"),
            "final_response": s_dict.get("final_response"),
            "final_response_source": s_dict.get("final_response_source"),
            "files_changed_json": json.dumps(s_dict.get("files_changed", []), ensure_ascii=False),
            "scrollback_text": scrollback_text,
            "transcript_path": s_dict.get("transcript_path") or "",
            "turn_id": s_dict.get("turn_id") or "",
            "wait_handle": s_dict.get("wait_handle") or "",
            "end_reason": arc.get("end_reason"),
            "archived_at": arc.get("archived_at"),
            "spawned_at": arc.get("spawned_at"),
            "profile_snapshot": arc.get("profile_snapshot") or "",
            "conversation_ref": arc.get("conversation_ref") or "",
            "scrollback": arc.get("scrollback"),
            "last_screen": arc.get("last_screen") or "",
            "parent_messages": arc.get("parent_messages") or "",
            "resume_count": int(arc.get("resume_count") or 0),
            "last_resumed_at": arc.get("last_resumed_at"),
            "resumable": arc.get("resumable"),
            "resume_block_reason": arc.get("resume_block_reason"),
        }
        columns = list(values.keys())
        placeholders = ", ".join(f":{c}" for c in columns)
        updates = []
        for col in columns:
            if col == "session_id":
                continue
            # A12: a plain telemetry persist (which knows none of these) must never erase what the
            # final record already wrote. D2 removed the one clear that used to exist: a resume now
            # keeps the previous answer too, and the new turn overwrites it through set_final_response.
            if col in ("ended_at", "exit_code", "archived_at", "spawned_at",
                       "profile_snapshot", "conversation_ref", "parent_messages",
                       "last_resumed_at", "end_reason", "final_response", "final_response_source"):
                updates.append(f"{col}=COALESCE(NULLIF(excluded.{col}, ''), sessions.{col})")
            elif col == "scrollback_text":
                updates.append(
                    "scrollback_text=CASE WHEN length(excluded.scrollback_text) > 0 "
                    "THEN excluded.scrollback_text ELSE sessions.scrollback_text END"
                )
            elif col == "scrollback":
                updates.append(
                    "scrollback=CASE WHEN excluded.scrollback IS NOT NULL "
                    "AND length(excluded.scrollback) > 0 THEN excluded.scrollback ELSE sessions.scrollback END"
                )
            elif col == "last_screen":
                updates.append(
                    "last_screen=CASE WHEN length(excluded.last_screen) > 0 "
                    "THEN excluded.last_screen ELSE sessions.last_screen END"
                )
            elif col == "transcript_path":
                updates.append(
                    "transcript_path=CASE WHEN length(excluded.transcript_path) > 0 "
                    "THEN excluded.transcript_path ELSE sessions.transcript_path END"
                )
            elif col == "engine":
                updates.append("engine=COALESCE(NULLIF(excluded.engine, ''), sessions.engine)")
            elif col == "resume_block_reason":
                updates.append(
                    "resume_block_reason=CASE WHEN excluded.resumable IS NOT NULL "
                    "THEN excluded.resume_block_reason "
                    "ELSE COALESCE(excluded.resume_block_reason, sessions.resume_block_reason) END"
                )
            elif col == "resumable":
                updates.append("resumable=COALESCE(excluded.resumable, sessions.resumable)")
            else:
                updates.append(f"{col}=excluded.{col}")
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(session_id) DO UPDATE SET {', '.join(updates)}",
                values,
            )

    def add_tool_call(
        self,
        session_id: str,
        step_num: int,
        tool_name: str,
        input_summary: str,
        output_summary: str = "",
        status: str = "running",
        source: str = "hook",
        timestamp: Optional[float] = None,
    ):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tool_calls (session_id, step_num, timestamp, tool_name, input_summary, output_summary, status, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, step_num, round(time.time() if timestamp is None else timestamp, 3), tool_name, input_summary[:1000], output_summary[:1000], status, source),
            )

    def complete_latest_tool_call(self, session_id: str, tool_name: Optional[str], output_summary: str):
        with self._conn() as conn:
            if tool_name:
                row = conn.execute(
                    "SELECT id FROM tool_calls WHERE session_id=? AND tool_name=? ORDER BY id DESC LIMIT 1",
                    (session_id, tool_name),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM tool_calls WHERE session_id=? ORDER BY id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            if row:
                conn.execute(
                    "UPDATE tool_calls SET status='completed', output_summary=? WHERE id=?",
                    (output_summary[:1000], row["id"]),
                )

    def add_intermediate_message(self, session_id: str, step_num: int, content: str, source: str = "agent", timestamp: Optional[float] = None):
        content = (content or "").strip()
        if not content:
            return
        with self._conn() as conn:
            last = conn.execute(
                "SELECT content FROM intermediate_messages WHERE session_id=? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if last and last["content"] == content:
                return
            conn.execute(
                """
                INSERT INTO intermediate_messages (session_id, step_num, timestamp, content, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, step_num, round(time.time() if timestamp is None else timestamp, 3), content[:4000], source),
            )

    def insert_worker_event(self, rec: Dict[str, Any]) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO worker_events (seq, session_id, timestamp, type, category, content, status, files_changed_json, request_id, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["seq"],
                    rec["session_id"],
                    rec["timestamp"],
                    rec["type"],
                    rec["category"],
                    rec["content"],
                    rec["status"],
                    json.dumps(rec.get("files_changed", []), ensure_ascii=False),
                    rec.get("request_id"),
                    json.dumps(rec["data"], ensure_ascii=False) if rec.get("data") else None,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _decode_json_column(raw: Any, default: Any) -> Any:
        if raw is None or raw == "":
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def get_session_record(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["files_changed"] = self._decode_json_column(d.pop("files_changed_json", None), [])
            # Archive spec §3: the JSON columns come back parsed; the compressed scrollback stays out
            # of the record (the scrollback endpoint reads it through get_scrollback_blob).
            d["profile_snapshot"] = self._decode_json_column(d.pop("profile_snapshot", None), None)
            d["conversation_ref"] = self._decode_json_column(d.pop("conversation_ref", None), None)
            d["parent_messages"] = self._decode_json_column(d.pop("parent_messages", None), [])
            d.pop("scrollback", None)
            d["archived"] = bool(d.get("archived_at"))
            return d

    def get_recent_tool_calls(self, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT step_num, timestamp, tool_name, input_summary, output_summary, status, source FROM tool_calls WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_recent_intermediate_messages(self, session_id: str, limit: int = 15) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT step_num, timestamp, content, source FROM intermediate_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_recent_worker_events(self, session_id: str, limit: int = 80) -> List[Dict[str, Any]]:
        """Return the newest persisted mailbox events in chronological order.

        The in-memory event ring is intentionally bounded and is not restored on Hub startup.
        Transcript consumers therefore need a small persisted tail as a fallback for archived
        sessions and for a page opened immediately after a restart.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT seq, session_id, timestamp, type, category, content, status,
                       files_changed_json, request_id, data_json
                FROM worker_events
                WHERE session_id=?
                ORDER BY seq DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
            result = []
            for row in reversed(rows):
                item = dict(row)
                item["files_changed"] = self._decode_json_column(item.pop("files_changed_json", None), [])
                item["data"] = self._decode_json_column(item.pop("data_json", None), None)
                item["interaction"] = None
                result.append(item)
            return result

    def list_persisted_sessions(self, limit: int = 30, include_archived: bool = True) -> List[Dict[str, Any]]:
        """Session summary rows; `include_archived=False` is "live + ended-in-memory" (spec §6)."""
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT session_id, profile_id, profile_name, kind, title, cwd, status,
                       created_at, updated_at, ended_at, exit_code, step_count,
                       current_activity, last_task, engine, effective_model, effective_thinking_level,
                       CASE WHEN final_response IS NOT NULL AND length(final_response) > 0 THEN 1 ELSE 0 END AS has_final_response,
                       final_response_source
                FROM sessions
                {where}
                ORDER BY COALESCE(ended_at, updated_at, created_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- Archive & resume storage (spec archive-spec.md §3, §6) ----------------------------

    ARCHIVE_SUMMARY_COLUMNS = """
        session_id, profile_id, profile_name, kind, title, cwd, status, engine,
        created_at, updated_at, ended_at, exit_code, step_count, last_task,
        effective_model, effective_thinking_level, end_reason, archived_at,
        resume_count, last_resumed_at, resumable, resume_block_reason,
        profile_snapshot, conversation_ref,
        CASE WHEN final_response IS NOT NULL AND length(final_response) > 0 THEN 1 ELSE 0 END
            AS has_final_response
    """

    def list_archived(self, limit: int = 50, offset: int = 0, query: str = "") -> List[Dict[str, Any]]:
        """Archive rows, newest first; `query` matches session id, task, cwd and profile (§6)."""
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        clauses = ["archived_at IS NOT NULL"]
        params: List[Any] = []
        q = (query or "").strip()
        if q:
            like = f"%{q}%"
            clauses.append(
                "(session_id LIKE ? OR last_task LIKE ? OR cwd LIKE ? "
                "OR profile_id LIKE ? OR profile_name LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        sql = (
            f"SELECT {self.ARCHIVE_SUMMARY_COLUMNS} FROM sessions WHERE {' AND '.join(clauses)} "
            "ORDER BY COALESCE(ended_at, archived_at, updated_at) DESC, archived_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["resumable"] = bool(d.get("resumable"))
                d["archived"] = True
                d["end_reason_label"] = END_REASON_LABELS.get(str(d.get("end_reason") or ""), "")
                # W1: the list row must carry the same inputs the single-record path has, or
                # compute_resumable sees no snapshot / a JSON string and condemns every record.
                d["profile_snapshot"] = self._decode_json_column(d.get("profile_snapshot"), None)
                d["conversation_ref"] = self._decode_json_column(d.get("conversation_ref"), {})
                out.append(d)
            return out

    def count_archived(self, query: str = "") -> int:
        clauses = ["archived_at IS NOT NULL"]
        params: List[Any] = []
        q = (query or "").strip()
        if q:
            like = f"%{q}%"
            clauses.append(
                "(session_id LIKE ? OR last_task LIKE ? OR cwd LIKE ? "
                "OR profile_id LIKE ? OR profile_name LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM sessions WHERE {' AND '.join(clauses)}", params
            ).fetchone()
            return int(row["n"]) if row else 0

    def get_scrollback_blob(self, session_id: str) -> Optional[bytes]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT scrollback FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if not row or row["scrollback"] is None:
                return None
            raw = row["scrollback"]
            return bytes(raw) if not isinstance(raw, bytes) else raw

    def delete_session_record(self, session_id: str) -> bool:
        """Deletes the hub's row only; the CLI's own conversation files are never touched."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            return cur.rowcount > 0

    def update_archive_fields(self, session_id: str, **fields: Any) -> None:
        """Writes a few session columns directly (startup normalisation, resume blocks)."""
        # The startup normalisation also rewrites the live status it found in the row, and the
        # resume-block updates rewrite nothing else; both sets must be allowed here.
        allowed = set(ARCHIVE_COLUMNS) | {"status", "ended_at", "current_activity"}
        sets = []
        params: List[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            params.append(value)
        if not sets:
            return
        params.append(session_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE session_id=?", params)

    def update_archive_fields_bulk(self, updates: List[tuple]) -> None:
        """R8: writes many rows' archive fields in ONE transaction (one commit per archive page)."""
        if not updates:
            return
        allowed = set(ARCHIVE_COLUMNS) | {"status", "ended_at", "current_activity"}
        with self._conn() as conn:
            for session_id, fields in updates:
                sets: List[str] = []
                params: List[Any] = []
                for key, value in (fields or {}).items():
                    if key not in allowed:
                        continue
                    sets.append(f"{key}=?")
                    params.append(value)
                if not sets:
                    continue
                params.append(session_id)
                conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE session_id=?", params)

    def reset_session_after_resume(self, session_id: str) -> None:
        """A revived session is live again: it leaves the archive and its end state (§5.3).

        D2: the stored final answer is deliberately left in place. The previous turn's reply stays
        readable until the revived turn writes its own; begin_new_turn marks it in memory as
        "from_previous_turn" and the next final-response source overwrites it.
        """
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET archived_at=NULL, end_reason=NULL, ended_at=NULL, exit_code=NULL,
                    resumable=NULL, resume_block_reason=NULL, status='starting'
                WHERE session_id=?
                """,
                (session_id,),
            )

    def list_session_ids(self) -> List[str]:
        with self._conn() as conn:
            return [str(r["session_id"]) for r in conn.execute("SELECT session_id FROM sessions").fetchall()]
