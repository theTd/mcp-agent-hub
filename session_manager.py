import asyncio
import base64
import ctypes
from ctypes import wintypes
import datetime
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import httpx
import pyte
from PIL import Image
from winpty import PtyProcess

# R6: the real class is kept apart from the module-level name so a test can replace
# session_manager.PtyProcess with a fake without making the shutdown tree-kill touch a fake PID.
REAL_PTY_PROCESS = PtyProcess

from model_discovery import AgentModelDiscovery
from transcript_summarizer import (
    ENGINE_BUSY_LINE_RES,
    ENGINE_BUSY_PHRASES,
    TranscriptSummarizerRegistry,
    sanitize_tui_screen_text,
)

logger = logging.getLogger("mcp-agent-hub.session_manager")

# Win32 API constants for Desktop GUI window capture & off-screen tucking
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# S4: a window's process name is read through a limited-information handle; a failure is not an error.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
try:
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
except Exception:  # pragma: no cover - only on a non-Windows host
    pass

PW_RENDERFULLCONTENT = 0x00000002
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class HubDatabase:
    """SQLite persistence layer ensuring Sub-Agent trace, intermediate messages, and final_response survive process exit & hub restarts."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock, self._conn() as conn:
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
            ):
                try:
                    conn.execute(ddl)
                except Exception:
                    pass

    def get_max_seq(self) -> int:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM worker_events").fetchone()
            return int(row["m"]) if row else 0

    def upsert_session(
        self,
        s_dict: Dict[str, Any],
        scrollback_text: str = "",
        archive: Optional[Dict[str, Any]] = None,
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
            "updated_at": time.time(),
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
        with self._lock, self._conn() as conn:
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
    ):
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tool_calls (session_id, step_num, timestamp, tool_name, input_summary, output_summary, status, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, step_num, round(time.time(), 3), tool_name, input_summary[:1000], output_summary[:1000], status, source),
            )

    def complete_latest_tool_call(self, session_id: str, tool_name: Optional[str], output_summary: str):
        with self._lock, self._conn() as conn:
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

    def add_intermediate_message(self, session_id: str, step_num: int, content: str, source: str = "agent"):
        content = (content or "").strip()
        if not content:
            return
        with self._lock, self._conn() as conn:
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
                (session_id, step_num, round(time.time(), 3), content[:4000], source),
            )

    def insert_worker_event(self, rec: Dict[str, Any]) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO worker_events (session_id, timestamp, type, category, content, status, files_changed_json, request_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["session_id"],
                    rec["timestamp"],
                    rec["type"],
                    rec["category"],
                    rec["content"],
                    rec["status"],
                    json.dumps(rec.get("files_changed", []), ensure_ascii=False),
                    rec.get("request_id"),
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
        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT step_num, timestamp, tool_name, input_summary, output_summary, status, source FROM tool_calls WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_recent_intermediate_messages(self, session_id: str, limit: int = 15) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT step_num, timestamp, content, source FROM intermediate_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def list_persisted_sessions(self, limit: int = 30, include_archived: bool = True) -> List[Dict[str, Any]]:
        """Session summary rows; `include_archived=False` is "live + ended-in-memory" (spec §6)."""
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM sessions WHERE {' AND '.join(clauses)}", params
            ).fetchone()
            return int(row["n"]) if row else 0

    def get_scrollback_blob(self, session_id: str) -> Optional[bytes]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT scrollback FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if not row or row["scrollback"] is None:
                return None
            raw = row["scrollback"]
            return bytes(raw) if not isinstance(raw, bytes) else raw

    def delete_session_record(self, session_id: str) -> bool:
        """Deletes the hub's row only; the CLI's own conversation files are never touched."""
        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE session_id=?", params)

    def update_archive_fields_bulk(self, updates: List[tuple]) -> None:
        """R8: writes many rows' archive fields in ONE transaction (one commit per archive page)."""
        if not updates:
            return
        allowed = set(ARCHIVE_COLUMNS) | {"status", "ended_at", "current_activity"}
        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
            return [str(r["session_id"]) for r in conn.execute("SELECT session_id FROM sessions").fetchall()]


# Priority levels for final_response sources so explicit MCP or native transcript never gets overwritten by raw screen text
FINAL_SOURCE_PRIORITY = {
    None: 0,
    "terminal_turn_snapshot": 1,
    "transcript_summarizer": 2,
    # K4: the content of a turn_completed worker event is the engine's own end-of-turn answer. It has
    # to beat a screen snapshot and the summariser, and it has to lose to the sources that are
    # strictly better evidence for the same turn: the engine's own transcript, a codex/dsh completion
    # and the worker's report_complete. So it sits between them, and a transcript that already named
    # the answer keeps its own source label instead of being relabelled by the event.
    "turn_completed": 3,
    "native_transcript": 4,
    "codex_task_complete": 5,
    "dsh_hub_completion": 5,
    "report_complete_mcp": 6,
}

# F1: the completion hint tells an engine to run `complete "<用一句话写出你的结果>"`. A literal copy of
# that placeholder (or of the old "SUMMARY") is not a result: the round-4 kimi run archived
# final_response="SUMMARY". One constant is used by the hint and by the completion handler, so the
# two cannot drift apart again.
COMPLETION_SUMMARY_PLACEHOLDER = "<用一句话写出你的结果>"
PLACEHOLDER_SUMMARY_TEXTS = frozenset({
    "summary",
    "your final summary",
    "your one-sentence result",
    "用一句话写出你的结果",
})
PLACEHOLDER_SUMMARY_RE = re.compile(r"^<[^<>]{0,80}>$")
SUMMARY_QUOTE_CHARS = "\"'“”‘’「」"


def is_placeholder_summary(text: Optional[str]) -> bool:
    """F1: True when a completion summary is only the hint's placeholder, never a result."""
    s = str(text or "").strip()
    if not s:
        return True
    s = s.strip(SUMMARY_QUOTE_CHARS).strip()
    if s.lower() in PLACEHOLDER_SUMMARY_TEXTS:
        return True
    return bool(PLACEHOLDER_SUMMARY_RE.match(s))

# Heuristic regex for detecting TUI tool calls on terminal lines when native hooks aren't present
TUI_TOOL_PATTERN = re.compile(
    r"^\s*[●•✦⚡▶]\s*(Bash|Read|Edit|Write|Grep|Glob|LS|WebSearch|Fetch|Task|MultiEdit|NotebookEdit|cmd|powershell)\s*(?:\((.+?)\)|:\s*(.+))?$",
    re.IGNORECASE,
)

DEFAULT_HUB_PORT = 4444
KNOWN_ENGINES = ("omp", "kimi", "agy", "codex", "dsh", "pwsh")
AGY_THINKING_LEVELS = ("low", "medium", "high")
AGY_SUFFIX_RE = re.compile(r"-(low|medium|high)$")
# A profile with an absurdly long argument list is a mistake, not a launch recipe.
MAX_LAUNCH_ARGS = 4096

# Q6: Windows refuses `os.replace` / an open-for-write while another process holds the file. A short
# retry covers the millisecond window of a concurrent reader; a lock that survives it is reported as
# a 400 (「配置文件被其他程序占用，请稍后重试」), never as a 500 and never as a parse error.
PROFILE_FILE_RETRY_ATTEMPTS = 10
PROFILE_FILE_RETRY_DELAY_SEC = 0.03
PROFILE_FILE_LOCKED_MESSAGE = "配置文件被其他程序占用，请稍后重试"

# Q8: keep looking for a session's own transcript for as long as its process lives, backing off to
# one scan every 5 s after the first minute instead of giving up after 30/40/90 s.
TRANSCRIPT_SCAN_FAST_WINDOW_SEC = 60.0
TRANSCRIPT_SCAN_FAST_INTERVAL_SEC = 1.0
TRANSCRIPT_SCAN_SLOW_INTERVAL_SEC = 5.0
# Q2: the hub session marker travels in the task text, i.e. near the beginning of the transcript.
TRANSCRIPT_MARKER_SCAN_BYTES = 128 * 1024
TRANSCRIPT_MARKER_RE = re.compile(r"session_id\s*=\s*([A-Za-z0-9_.\-]+)")

# ---- Archive & resume (spec archive-spec.md §1-§5) ------------------------------------------
# Live statuses keep their meaning; an ended session carries one of these end reasons.
LIVE_STATUSES = ("starting", "running", "waiting_parent", "completed")
END_REASONS = ("exited", "crashed", "terminated", "interrupted")
END_REASON_LABELS = {
    "exited": "已退出",
    "crashed": "异常退出",
    "terminated": "已终止",
    "interrupted": "已中断",
}
# K5: the `terminated` event says who ended the session. The console pane and an MCP caller are two
# different actors and must not share one sentence.
TERMINATED_MESSAGES = {
    "console": "用户在总控台终止了会话",
    "mcp": "主 Agent 终止了会话",
}
# Engines with a revive recipe (spec §4). Anything else cannot be resumed.
RESUME_ENGINES = ("omp", "kimi", "agy", "codex", "dsh")
ENGINE_RESUME_BLOCK_REASONS = {
    "pwsh": "PowerShell 会话结束后无法找回上下文",
    "desktop_gui": "桌面窗口会话结束后无法找回上下文",
}
DEFAULT_RESUME_BLOCK_REASON = "该引擎的会话结束后无法恢复上下文"
RESUME_SCROLLBACK_MAX_BYTES = 512 * 1024
RESUME_SCROLLBACK_INTERVAL_SEC = 60.0
# D1: archive records are kept forever. There is no age/count limit and no pruning loop; the only
# way a record leaves the hub is the manual delete (console 删除记录 / DELETE /api/archive/{id}).
# R5/R6: a graceful stop must not hang on a CLI that ignores the first signal or a DSH host that
# takes its time; each session is bounded and so is the whole loop.
SHUTDOWN_MAX_SEC = 15.0
SHUTDOWN_JOB_CANCEL_TIMEOUT_SEC = 2.0
AGY_RESUME_FAILURE_MESSAGE = "恢复失败：agy 没有接上原会话，开了一个新会话"
CODEX_ARCHIVED_MESSAGE = "codex 已把该会话归档，请先运行 codex unarchive {sid}"
KIMI_LOCKED_MESSAGE = "该 Kimi 会话正被另一个进程使用"
DSH_UNKNOWN_JOB_MESSAGE = "DSH 服务已不认识这个任务（可能重启过）"
DSH_STATE_UNAVAILABLE_MESSAGE = "DSH 任务状态不可用：{reason}"
# R3: these are ENDED jobs. Re-attaching one as "completed" lets the next message /continue it;
# refusing them made the archive say 可恢复 while 恢复 without text failed.
DSH_ENDED_JOB_STATUSES = ("done", "failed", "timeout", "cancelled")
RESUME_FAILED_PREFIX = "恢复失败："
# A4: a resume failure that no re-check can undo is sticky. agy opened a different conversation
# (only a real resume can fix it) and codex archived the conversation (only codex unarchive can);
# _finalize_session and compute_resumable must keep these instead of recomputing "resumable".
# The codex message carries the session id, so it is matched by its prefix.
STICKY_RESUME_BLOCK_REASONS = (AGY_RESUME_FAILURE_MESSAGE,)
STICKY_RESUME_BLOCK_PREFIXES = ("codex 已把该会话归档",)
# A3: after a resumed agy process starts, a brand-new conversation dir can only be that process
# opening an empty conversation for an id it does not know. The window keeps the scan bounded.
AGY_RESUME_VERIFY_WINDOW_SEC = 30.0
# The hub's anchor (time.time()) and the filesystem's birth time are different clock sources and can
# round against each other; a dir born in the same tick as the spawn is still the new conversation.
AGY_CONVERSATION_ANCHOR_SLACK_SEC = 0.05
# A11: the archive thread keeps every worker→parent message per session, outside the 2000-event ring.
MAILBOX_EVENT_TYPES = frozenset({
    "message", "message_to_parent", "question", "approval_request", "completed", "turn_completed",
})
WORKER_MESSAGE_LOG_CAP = 2000
# V2: the startup injection waits for a settled TUI with a visible prompt instead of a fixed delay,
# then proves the Enter really submitted the text.
TUI_READY_STABLE_SEC = 1.5
TUI_STARTUP_WINDOW_SEC = 90.0
# W2: the composer rows found in the real captures. kimi draws a boxed row; omp and codex (and
# agy, which uses the same Go TUI shape) draw a bare prompt glyph with status lines below it.
TUI_INPUT_BOX_RE = re.compile(r"^\s*[│┃|]\s*>\s?(.*?)\s*[│┃|]\s*$")
TUI_INPUT_BOX_OPEN_RE = re.compile(r"^\s*[│┃|]\s*>\s?(.*?)\s*$")
TUI_INPUT_GLYPH_RE = re.compile(r"^\s*(?:[│┃|]\s*)?(?:[>❯›»])\s?(.*)$")
# K2 (fix 9): how each engine draws its composer. "box" is a framed box whose first content row is
# "│ > …" and whose height grows with the text (kimi's box is 12 rows tall for a task carrying the
# worker-bridge hint at 120 columns); every other engine draws a bare prompt glyph row ("glyph").
TUI_COMPOSER_SHAPES = {
    "kimi": "box",
}
TUI_BOX_TOP_RE = re.compile(r"^\s*╭─.*╮\s*$")
TUI_BOX_BOTTOM_RE = re.compile(r"^\s*╰─.*╯\s*$")
TUI_BOX_SIDE_RE = re.compile(r"^\s*[│┃]")
TUI_SUBMIT_CONFIRM_SEC = 3.0
TUI_SUBMIT_CONFIRM_POLL_SEC = 0.25
TUI_SUBMIT_MAX_TRIES = 3
TUI_SUBMIT_RETRY_STEP_SEC = 1.0
TUI_TYPE_SETTLE_SEC = 0.8
# D3: when the evidence window ends with ok:null/"unconfirmed" the hub presses Enter once more and
# watches this short window. The press counts toward TUI_SUBMIT_MAX_TRIES.
TUI_SUBMIT_EXTRA_PRESS_SEC = 5.0
# W2: engine input boxes do not live on the last line (kimi draws two status lines under its
# composer), so the prompt is searched in the bottom rows of the screen, per engine.
TUI_PROMPT_SCAN_ROWS = 10
# R1/W4: the dialog watcher ticks at this interval for the whole startup window.
TUI_DIALOG_POLL_SEC = 0.5
TUI_TRUST_CONFIRM_SETTLE_SEC = 0.5
# The trust dialogs seen in the real captures (agy, codex, omp, kimi). Enter confirms the
# highlighted "Yes, I trust this folder"-style option; the same handler must be alive for the
# whole startup window, not only for the first half second (R1/W4).
TUI_TRUST_DIALOG_PHRASES = (
    "Trust this folder?",
    "Trust and continue",
    "Do you trust the contents of this project?",
    "Do you trust the authors of the files in this folder?",
    "Do you trust the files in this folder?",
    "Yes, I trust this folder",
)
# D3: a known dialog must never receive the extra Enter. The omp ready capture carries this banner
# right above the composer ("Update Available / New version 18.17.0 is available"), so it counts.
TUI_UPDATE_DIALOG_PHRASES = (
    "Update Available",
    "New version ",
)
# W3/K2: the screen proves the agent is working or answering when one of these is visible.
# K2 removed the words that are NOT evidence, and every one of them came from a real capture:
#   - a bare "thinking": kimi's status bar permanently reads "K3.1 Pro thinking: low";
#   - a bare braille glyph: omp's status bar opens with one (" ⠧ 0s · ◒ DeepSeek Flash …"), so a
#     spinner-shaped rule would call omp busy forever.
# The phrases below were checked against the round-3 replays; each is only drawn while working.
# kimi "⠋ Working…" / "⠋ thinking…", omp "⎋ Working…", agy "⣷  Generating...",
# codex "Worked for 35s" / "esc to interrupt".
TUI_AGENT_ACTIVE_RE = re.compile(
    r"(?:Working…|Working \(|Generating|Running a command|esc to interrupt|Worked for"
    r"|thinking…|thinking\.\.\.)",
    re.IGNORECASE,
)
# K1: a visible composer does not mean the engine can take input yet. Every engine draws its
# prompt while it is still starting up and swallows the first Enter until that startup ends, so the
# readiness gate also waits for these indicators to leave the screen. F3: the lists live in
# transcript_summarizer, beside the screen filter that must ignore the same rows, so readiness and
# final-response extraction can never disagree about what "busy" or "still starting" looks like.
TUI_BUSY_PHRASES = ENGINE_BUSY_PHRASES
TUI_BUSY_LINE_RES = ENGINE_BUSY_LINE_RES
# K1 (fix 9): the banner an engine draws once a FRESH spawn can really take input. kimi draws a bare
# composer ~10 s before that, then "Preparing Kimi Code…", then (22-48 s after spawn in the probes)
# the Welcome box. An Enter before the Welcome box is swallowed or answered with "Error: LLM not
# set" (runs hub-03, early-01/02); one Enter after it submitted every time (welcome-01/02/03).
# A resume is not gated on it: kimi -S draws no composer until its Welcome box, the box scrolls off
# within seconds, and an Enter typed right at Welcome was still swallowed (resume probes, fix 9),
# while the ordinary composer + busy + stable gate submitted with one Enter.
TUI_READY_BANNERS = {
    "kimi": "Welcome to Kimi Code!",
}
# K1 (fix 9): a banner that never shows (a kimi update that changes the text) must not hang the
# spawn: this long after the trust dialog was answered (or after spawn without one) the gate falls
# back to composer + busy + stable, with a warning in the log. The fallback always leaves the
# reserve below inside the startup window, so the old gate still gets its chance to type.
TUI_READY_BANNER_FALLBACK_SEC = 60.0
TUI_READY_BANNER_RESERVE_SEC = 10.0
# K1 (fix 9): the banner may scroll above the visible screen before a poll sees it, so the recent
# scrollback is searched too, this many rows deep.
TUI_READY_BANNER_HISTORY_ROWS = 200
# K3 (fix 9): what proves (or refutes) one Enter, per engine. The defaults are the W3/K2 proofs.
#   cleared_composer  a composer that held the text and is now empty proves the submit. Not for
#                     kimi: it clears its box when it REJECTS an Enter ("Error: LLM not set", and no
#                     session is ever created -- early-01/02).
#   transcript        "growth": the transcript file grew after the Enter. "user_message": the
#                     transcript watcher recorded a new user message holding the text -- kimi's
#                     resumed wire.jsonl grows by itself at startup (MCP tool lists), so growth alone
#                     proves nothing there.
#   history_glyph     a submitted message is redrawn as a history row starting with this glyph; a
#                     new such row holding the text counts as screen evidence.
#   reject_phrases    the engine's refusal of an Enter; a new one on screen means "not submitted, the
#                     box was cleared" -- the caller waits for readiness and types the text again.
TUI_DEFAULT_SUBMIT_PROOF = {
    "cleared_composer": True,
    "transcript": "growth",
    "history_glyph": "",
    "reject_phrases": (),
}
TUI_SUBMIT_PROOF_RULES = {
    "kimi": {
        "cleared_composer": False,
        "transcript": "user_message",
        "history_glyph": "❯",
        "reject_phrases": ("LLM not set",),
    },
}
# K2: how long a submit whose text was never seen on the composer keeps waiting for evidence. The
# kimi spawn in evidence round 3 typed into a box that was still booting, so the first look proved
# nothing; a redraw can also clear the bottom rows and put the text back a moment later.
TUI_SUBMIT_EVIDENCE_SEC = 15.0
# W9: the raw history is a bounded 512 KB window. Trimming rebuilds the joined text, so it runs
# with a little slack instead of on every 4 KB PTY chunk (the cap is still enforced on the tail).
HISTORY_TRIM_SLACK_BYTES = 64 * 1024
HISTORY_TRUNCATED_PREFIX = "\x1b[90m[较早的输出已截断]\x1b[0m\r\n"
# R7: an agy resume whose archived transcript is still locked by another watcher gets this long
# to become claimable, then the watcher stops instead of spinning for the session's lifetime.
AGY_RESUME_TRANSCRIPT_WAIT_SEC = 120.0
ARCHIVE_COLUMNS = (
    "end_reason", "archived_at", "spawned_at", "profile_snapshot", "conversation_ref",
    "scrollback", "last_screen", "parent_messages", "resume_count", "last_resumed_at",
    "resumable", "resume_block_reason",
)


def resume_banner(resume_count: int) -> str:
    """The separator a revived pane shows between the archived output and the new one (spec §5.3)."""
    return f"\r\n\x1b[90m── 已恢复（第 {int(resume_count)} 次）──\x1b[0m\r\n"


def trim_history_text(text: str, max_bytes: int) -> str:
    """W9: cap a raw terminal history at max_bytes without cutting a line or the newest banner.

    The old buffer dropped whole PTY chunks, so the archived scrollback and every resume banner
    left in one step. This cuts bytes from the front, moves to the next line break, prefixes a
    grey notice, and never eats the newest "── 已恢复（第 N 次）──" line (nor what follows it)
    unless that part alone is larger than the whole budget.
    """
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text
    prefix = HISTORY_TRUNCATED_PREFIX
    budget = max_bytes - len(prefix.encode("utf-8"))
    if budget <= 0:
        return prefix
    start = len(raw) - budget
    next_break = raw.find(b"\n", start)
    if next_break >= 0:
        start = next_break + 1
    # Keep the newest banner and everything after it when that tail alone fits.
    marker = "\x1b[90m── 已恢复（"
    idx = text.rfind(marker)
    if idx >= 0:
        banner_start = len(text[:idx].encode("utf-8", "replace"))
        if banner_start >= 2 and text[:idx].endswith("\r\n"):
            banner_start -= 2
        if len(raw) - banner_start <= budget and banner_start < start:
            start = banner_start
    body = raw[start:].decode("utf-8", "replace")
    return prefix + body


def _store_root(env_name: str, default: Path) -> Path:
    """A CLI store root; the HUB_* override lets tests point at a synthetic copy (never the real store)."""
    raw = os.environ.get(env_name)
    if raw and raw.strip():
        return Path(os.path.expanduser(raw.strip()))
    return default


def codex_sessions_root() -> Path:
    return _store_root("HUB_CODEX_SESSIONS_ROOT", Path(os.path.expanduser("~")) / ".codex" / "sessions")


def kimi_store_root() -> Path:
    return _store_root("HUB_KIMI_STORE_ROOT", Path(os.path.expanduser("~")) / ".kimi-code")


def agy_brain_root() -> Path:
    """V3: agy 1.5.2 writes conversations to ~/.gemini/antigravity-cli/brain.

    The old ~/.gemini/antigravity/brain is only the fallback for an install that still uses it;
    HUB_AGY_BRAIN_ROOT keeps overriding either for tests.
    """
    raw = os.environ.get("HUB_AGY_BRAIN_ROOT")
    if raw and raw.strip():
        return Path(os.path.expanduser(raw.strip()))
    cli_root = Path(os.path.expanduser("~")) / ".gemini" / "antigravity-cli"
    if (cli_root / "brain").is_dir():
        return cli_root / "brain"
    return Path(os.path.expanduser("~")) / ".gemini" / "antigravity" / "brain"


def _pid_alive(pid: int) -> bool:
    """True while a process with this pid exists (used for the kimi per-session lock)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _is_file_lock_error(exc: BaseException) -> bool:
    """True for the Windows errors a held file raises: sharing/lock violations (32/33) and EACCES."""
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in (32, 33)


def hub_port() -> int:
    """The port this hub really listens on (`HUB_PORT`, default 4444)."""
    try:
        return int(os.environ.get("HUB_PORT") or DEFAULT_HUB_PORT)
    except ValueError:
        return DEFAULT_HUB_PORT


def hub_api_base_url() -> str:
    return os.environ.get("HUB_API_URL") or f"http://127.0.0.1:{hub_port()}"


def split_agy_model_id(model_id: str) -> tuple:
    """`gpt-oss-120b-medium` -> ("gpt-oss-120b", "medium"); a bare base id keeps a None level."""
    mid = model_id or ""
    m = AGY_SUFFIX_RE.search(mid)
    if not m:
        return mid, None
    return mid[: m.start()], m.group(1)


def _injected_flag_tokens(engine: str, args: List[str]):
    """Yields (index, flag label) for every argument the hub injects itself, for one engine."""
    eng = (engine or "").strip().lower()
    i = 0
    total = len(args)
    while i < total:
        a = str(args[i])
        if eng == "omp":
            if a.startswith("--model=") or a.startswith("--thinking="):
                yield i, a.split("=", 1)[0] + "="
                i += 1
                continue
        elif eng == "kimi":
            if a in ("-m", "--model"):
                yield i, a
                if i + 1 < total:
                    yield i + 1, a
                i += 2
                continue
        elif eng == "agy":
            if a in ("--model", "--effort"):
                yield i, a
                if i + 1 < total:
                    yield i + 1, a
                i += 2
                continue
        elif eng == "codex":
            if a in ("-m", "--model"):
                yield i, a
                if i + 1 < total:
                    yield i + 1, a
                i += 2
                continue
            if a == "-c" and i + 1 < total:
                nxt = str(args[i + 1]).strip().lstrip('"').lstrip("'")
                if nxt.startswith("model_reasoning_effort") or nxt.startswith("model="):
                    yield i, "-c"
                    yield i + 1, "-c"
                    i += 2
                    continue
        i += 1


def injected_flags_in_args(engine: str, args: List[str]) -> List[str]:
    """Every distinct flag in `args` that the hub injects at launch time, in first-appearance order."""
    labels: List[str] = []
    for _idx, label in _injected_flag_tokens(engine, args):
        if label not in labels:
            labels.append(label)
    return labels


def strip_injected_flags(engine: str, args: List[str]) -> List[str]:
    """Removes every hub-injected model/thinking flag, so old profile files still launch cleanly."""
    drop = {idx for idx, _label in _injected_flag_tokens(engine, args)}
    return [str(a) for i, a in enumerate(args) if i not in drop]


class HubTerminalScreen(pyte.HistoryScreen):
    """K4 (fix 9): the hub's screen model of a PTY.

    kimi sends a private status query (CSI ? 996 n). pyte dispatches every private CSI with
    private=True, but its report_device_status takes no such flag, so the query raised a TypeError
    in the middle of the PTY chunk. A private DSR asks the terminal something and draws nothing, so
    the whole CSI ? … n family is ignored here.
    """

    def report_device_status(self, mode: int, **kwargs: Any) -> None:
        if kwargs.get("private"):
            return
        super().report_device_status(mode)


class HubTerminalStream(pyte.Stream):
    """K4 (fix 9): one escape sequence pyte cannot handle costs only itself, not the rest of the chunk.

    pyte resets its parser when a handler raises but re-raises, and Stream.feed then abandons every
    byte after it (the raw scrollback kept them, the screen model did not). Here the failed
    character is dropped and the next one is parsed by the fresh parser.
    """

    def _send_to_parser(self, data: str) -> Optional[bool]:
        try:
            return super()._send_to_parser(data)
        except Exception:
            # pyte's own handler already re-initialised the parser; report its plain-text state.
            return self._taking_plain_text


class AgentSession:
    """Represents a single managed Sub-Agent session (TUI via ConPTY, Web GUI, or Win32 Desktop GUI)."""

    def __init__(
        self,
        session_id: str,
        profile_id: str,
        profile_name: str,
        kind: str,
        title: str,
        cwd: str,
        command: str,
        args: List[str],
        gui_url: Optional[str] = None,
        hwnd: Optional[int] = None,
        cols: int = 120,
        rows: int = 36,
        # A7: the callback carries the session object, so an exit of an object a resume already
        # replaced can never be mistaken for the live session's own exit.
        on_exit_cb: Optional[Callable[["AgentSession", int], None]] = None,
        on_tui_tool_cb: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.session_id = session_id
        self.profile_id = profile_id
        self.profile_name = profile_name
        self.kind = kind  # "tui" | "web_gui" | "desktop_gui"
        self.title = title
        self.cwd = cwd
        self.command = command
        self.args = args
        self.gui_url = gui_url
        self.hwnd = hwnd
        self.tucked_offscreen = False
        self.original_rect: Optional[tuple] = None
        self.on_exit_cb = on_exit_cb
        self.on_tui_tool_cb = on_tui_tool_cb

        self.cols = cols
        self.rows = rows
        self.created_at = time.time()
        # Q2: the moment the child process really starts; transcript ownership is measured from here,
        # not from the hub's bookkeeping time of create_session.
        self.spawned_at: Optional[float] = None
        self.last_output_at = time.time()
        self.ended_at: Optional[float] = None
        self.status = "starting"  # starting | running | waiting_parent | completed | exited | terminated
        self.exit_code: Optional[int] = None
        # V-5: set by terminate(); the PTY reader must not report a second lifecycle event for it.
        self.terminated_by_user: bool = False
        # B9: the initial task, never overwritten by follow-up messages or by status text.
        self.last_task: str = ""

        # Rich Telemetry & Lifecycle State
        self.step_count: int = 0
        self.current_activity: str = "Initializing session..."
        self.final_response: Optional[str] = None
        self.final_response_source: Optional[str] = None
        # D2: True while the stored answer belongs to the turn that ended (set by begin_new_turn on
        # a resume and on a follow-up). The next turn's first source overwrites it whatever its rank.
        self.final_response_from_previous_turn: bool = False
        self.files_changed: List[str] = []
        self.pending_question_id: Optional[str] = None
        self.pending_interaction: Optional[Dict[str, Any]] = None
        self.engine: str = "generic"
        self.approval_mode: str = "auto"
        self.transcript_path: Optional[Path] = None
        # K3 (fix 9): user messages the transcript watcher has read from this session's transcript
        # (a counter and the newest text), so a typed submit can be proved by the transcript itself.
        self.transcript_user_seq: int = 0
        self.transcript_user_text: str = ""
        self.effective_model: Optional[str] = None
        self.effective_thinking_level: Optional[str] = None

        # Archive spec §3: everything the final record needs in order to view and revive this session.
        self.end_reason: Optional[str] = None
        self.archived_at: Optional[float] = None
        self.last_screen: str = ""
        self.profile_snapshot: Dict[str, Any] = {}
        self.conversation_ref: Dict[str, Any] = {}
        self.resume_count: int = 0
        self.last_resumed_at: Optional[float] = None
        self.resumable: Optional[bool] = None
        self.resume_block_reason: Optional[str] = None
        # Resume (§5.3): attach the watcher to the known conversation and tail only what is new.
        self.resume_attach: bool = False
        self.attach_offset: Optional[int] = None
        self.completion_reported: bool = False
        # A10: the real outcome of typing a startup/resume text into the TUI (never guessed).
        self.last_injection: Optional[Dict[str, Any]] = None
        self._scrollback_written_at: float = 0.0
        self._scrollback_rev_written: int = -1
        self._seen_tui_tool_lines: Set[str] = set()
        self._seen_tui_prompt_signatures: Set[str] = set()

        # Headless VT100 terminal screen buffer WITH 3,000-line scrollback history!
        # K4 (fix 9): the hub's subclasses keep one bad escape sequence from losing the rest of a chunk.
        self.screen = HubTerminalScreen(cols, rows, history=3000)
        self.stream = HubTerminalStream(self.screen)

        # Raw ANSI history buffer for Web Console xterm.js replay
        self.raw_history: List[str] = []
        self.raw_history_bytes = 0
        # Archive spec §3: bumped on every append, so the 60 s scrollback write can tell "changed"
        # even when the capped buffer's byte count happens to be the same.
        self.raw_history_rev = 0
        self.max_history_bytes = RESUME_SCROLLBACK_MAX_BYTES

        # Connected WebSocket queues for live browser xterm.js streaming
        self.ws_queues: Set[asyncio.Queue] = set()
        # W7: one closer per browser socket, so removing the session from the live list also ends
        # the sockets with a normal close code instead of leaving them open and silent forever.
        self.ws_close_hooks: Dict[asyncio.Queue, Callable[[], Any]] = {}
        # W8: a resumed omp session clears saved lines with ESC[3J; a split sequence must not slip
        # through between two PTY chunks.
        self._esc3j_carry: str = ""
        self.pty: Optional[PtyProcess] = None
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # DeepSeek Harness (dsh-crew in-host) native session bridge fields
        self.dsh_job_id: Optional[str] = None
        self.dsh_session_id: Optional[str] = None
        self.dsh_token: Optional[str] = None
        self.dsh_spec: Dict[str, Any] = {}
        self._virtual_line_buf: str = ""
        self.on_virtual_line_cb: Optional[Callable[[str, str], Any]] = None

    def start_virtual(
        self,
        loop: asyncio.AbstractEventLoop,
        on_virtual_line_cb: Optional[Callable[[str, str], Any]] = None,
    ):
        self._loop = loop
        self.on_virtual_line_cb = on_virtual_line_cb
        self.status = "running"
        self.current_activity = "Connected to DeepSeek Harness Host"
        self.last_output_at = time.time()

    def emit_virtual_ansi(self, data: str):
        self._on_pty_output(data)

    def set_final_response(self, text: Optional[str], source: str):
        text = (text or "").strip()
        if not text:
            return
        new_prio = FINAL_SOURCE_PRIORITY.get(source, 1)
        if self.final_response_from_previous_turn:
            # D2: the previous turn's answer is kept only until this turn writes one. The new turn's
            # first source wins whatever its rank, so an old report_complete_mcp can no longer
            # outrank the turn that is running now; the flag clears with that first write.
            self.final_response = text
            self.final_response_source = source
            self.final_response_from_previous_turn = False
        else:
            # Within one turn the source priority stays exactly as the K4 table defines it.
            cur_prio = FINAL_SOURCE_PRIORITY.get(self.final_response_source, 0)
            if new_prio >= cur_prio:
                self.final_response = text
                self.final_response_source = source
        # Spec §1.4: "reported completion" is what separates a clean exit from an abnormal one.
        if str(source) in ("report_complete_mcp", "codex_task_complete", "dsh_hub_completion"):
            self.completion_reported = True

    def start_pty(
        self,
        loop: asyncio.AbstractEventLoop,
        env_overrides: Optional[Dict[str, str]] = None,
        ws_prefix: Optional[str] = None,
    ):
        self._loop = loop
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["HUB_SESSION_ID"] = self.session_id
        env["HUB_API_URL"] = hub_api_base_url()
        if env_overrides:
            env.update(env_overrides)

        cmd_list = [self.command] + list(self.args)
        # Q2: immediately before the process is spawned, so a transcript created at (or after) this
        # instant can be this session's and nothing older can.
        self.spawned_at = time.time()
        self.pty = PtyProcess.spawn(
            cmd_list,
            cwd=self.cwd,
            env=env,
            dimensions=(self.rows, self.cols),
        )
        # A1: a resumed object may inherit sockets attached to the object it replaced. The separator
        # goes out between the spawn and the reader thread, so it can never land after the new
        # process's first output, and a failed spawn (which raised above) pushed nothing.
        if ws_prefix:
            self.push_ws_frame(ws_prefix)
        self.status = "running"
        self.current_activity = "Interactive session running"
        self.last_output_at = time.time()

        reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        reader_thread.start()

    def _read_loop(self):
        while self.pty and self.pty.isalive():
            try:
                data = self.pty.read(4096)
                if not data:
                    time.sleep(0.02)
                    continue
                self._on_pty_output(data)
            except EOFError:
                break
            except Exception:
                break

        self.ended_at = time.time()
        try:
            self.exit_code = self.pty.exitstatus if self.pty else 0
        except Exception:
            self.exit_code = 0

        # Snapshot sanitized terminal output into final_response only if no higher-priority response was recorded
        scrollback = self.get_full_scrollback_text(max_lines=80)
        clean_lines = sanitize_tui_screen_text(scrollback, max_lines=10, engine=self.engine)
        if clean_lines:
            self.set_final_response("\n".join(clean_lines), "terminal_turn_snapshot")

        # Archive spec §1.4/§2: the process's end gets a named reason. A user termination already
        # decided its status in terminate(); the reader thread must not overwrite it.
        if self.terminated_by_user:
            self.end_reason = "terminated"
        else:
            self.end_reason = self.classify_end_reason(self.exit_code)
            self.status = self.end_reason
        self.current_activity = f"Session {self.status} (exit_code={self.exit_code})"

        if self.on_exit_cb and self._loop and not self.terminated_by_user:
            # V-5: a user termination already has its one lifecycle event; do not queue a second one.
            # A7: carry this object, so the manager can tell it apart from a later resume's object.
            self._loop.call_soon_threadsafe(self.on_exit_cb, self, self.exit_code or 0)

    def _on_pty_output(self, data: str):
        detected_tools = []
        # W8: omp clears the screen AND its saved lines when it starts (ESC[2J + ESC[3J). On a
        # resumed session the saved lines are the archived output and the resume banner, so the
        # erase-saved-lines half never reaches the history or a client.
        if self.resume_count:
            data = self._strip_saved_lines_erase(data)
        with self._lock:
            self.last_output_at = time.time()
            try:
                self.stream.feed(data)
            except Exception:
                pass

            # Heuristic detection of TUI tool lines for agents without native hooks
            for line in self.screen.display:
                clean = line.strip()
                if not clean or clean in self._seen_tui_tool_lines:
                    continue
                m = TUI_TOOL_PATTERN.match(clean)
                if m:
                    self._seen_tui_tool_lines.add(clean)
                    t_name = m.group(1)
                    t_arg = (m.group(2) or m.group(3) or "").strip()
                    detected_tools.append((t_name, t_arg))

            self.raw_history.append(data)
            # W9: count UTF-8 bytes (the cap is 512 KB, not 512k characters) and trim the JOINED
            # history at a line break instead of dropping whole chunks; the slack keeps the
            # 512 KB rebuild off every PTY read once the buffer is full.
            self.raw_history_bytes += len(data.encode("utf-8", "replace"))
            self.raw_history_rev += 1
            if self.raw_history_bytes > self.max_history_bytes + HISTORY_TRIM_SLACK_BYTES:
                joined = "".join(self.raw_history)
                trimmed = trim_history_text(joined, self.max_history_bytes)
                if trimmed != joined:
                    self.raw_history = [trimmed]
                    self.raw_history_bytes = len(trimmed.encode("utf-8", "replace"))
            # Q11: the fan-out targets belong to the same critical section as the history append.
            # A WebSocket registered afterwards gets this chunk inside its replay snapshot, never
            # twice (append + live frame) and never zero times (the old snapshot/register gap).
            ws_targets = list(self.ws_queues)

        if detected_tools and self.on_tui_tool_cb and self._loop:
            for t_name, t_arg in detected_tools:
                self._loop.call_soon_threadsafe(self.on_tui_tool_cb, self.session_id, t_name, t_arg)

        if self._loop and ws_targets:
            for q in ws_targets:
                self._loop.call_soon_threadsafe(self._push_ws_safe, q, data)

    @staticmethod
    def _push_ws_safe(q: asyncio.Queue, data: str):
        try:
            q.put_nowait(data)
        except Exception:
            pass

    def _strip_saved_lines_erase(self, data: str) -> str:
        """W8: removes ESC[3J (erase saved lines) from a resumed session's output.

        The sequence can be split across two PTY reads, so up to three trailing bytes are carried
        into the next call. ESC[2J (erase display) is deliberately left alone: it only clears the
        viewport, and the bundled xterm.js 5.5.0 has no scrollOnEraseInDisplay option.
        """
        if not data:
            return data
        buf = self._esc3j_carry + data
        self._esc3j_carry = ""
        out = buf.replace("\x1b[3J", "")
        for size in (3, 2, 1):
            suffix = "\x1b[3J"[:size]
            if out.endswith(suffix):
                self._esc3j_carry = suffix
                out = out[:-size]
                break
        return out

    def register_ws_closer(self, q: asyncio.Queue, closer: Callable[[], Any]) -> None:
        """W7: the server registers how to close the browser socket that owns this queue."""
        with self._lock:
            self.ws_close_hooks[q] = closer

    def close_ws_frontends(self) -> None:
        """W7: normal-close every browser socket still attached to this session (normal code 1000)."""
        with self._lock:
            hooks = list(self.ws_close_hooks.values())
            self.ws_close_hooks.clear()
        for closer in hooks:
            try:
                closer()
            except Exception:
                pass

    def attach_ws_queue(self) -> tuple:
        """Q11: snapshot the replay and register the live queue in one atomic step.

        The PTY reader appends to `raw_history` and snapshots its fan-out targets under the same
        lock, so every output chunk is either inside the replay this socket receives or in its
        queue — never both, never neither.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            replay = "".join(self.raw_history)
            self.ws_queues.add(q)
        return replay, q

    def detach_ws_queue(self, q: asyncio.Queue) -> None:
        with self._lock:
            self.ws_queues.discard(q)
            self.ws_close_hooks.pop(q, None)

    def adopt_ws_frontends(self, previous: "AgentSession") -> None:
        """A1: an open browser socket keeps streaming after a resume replaces the session object.

        The queue set is handed over by identity, so the server's disconnect path — which still holds
        the old object — detaches from the very set the new session fans out to. The resume separator
        is pushed separately (push_ws_frame) because the seed in raw_history is replay-only.
        """
        self.ws_queues = previous.ws_queues
        # W7: the closers travel with the sockets, or a terminate after the resume could no longer
        # close them.
        self.ws_close_hooks = previous.ws_close_hooks

    def push_ws_frame(self, frame: str) -> None:
        """A1: writes one frame to every browser socket already attached to this session id."""
        for q in list(self.ws_queues):
            self._push_ws_safe(q, frame)

    def write_input(self, text: str):
        if self.pty and self.pty.isalive():
            self.pty.write(text)
            return True
        if self.on_virtual_line_cb is not None:
            for ch in text:
                if ch in ("\r", "\n"):
                    self.emit_virtual_ansi("\r\n")
                    line = self._virtual_line_buf.strip()
                    self._virtual_line_buf = ""
                    if line:
                        self.on_virtual_line_cb(self.session_id, line)
                elif ch in ("\x7f", "\b"):
                    if self._virtual_line_buf:
                        self._virtual_line_buf = self._virtual_line_buf[:-1]
                        self.emit_virtual_ansi("\b \b")
                else:
                    self._virtual_line_buf += ch
                    self.emit_virtual_ansi(ch)
            return True
        return False

    def resize(self, cols: int, rows: int):
        with self._lock:
            self.cols = max(20, int(cols))
            self.rows = max(5, int(rows))
            try:
                self.screen.resize(self.rows, self.cols)
            except Exception:
                pass
            if self.pty and self.pty.isalive():
                try:
                    self.pty.setwinsize(self.rows, self.cols)
                except Exception:
                    pass

    def get_clean_screen_text(self, max_lines: int = 80) -> str:
        with self._lock:
            lines = [line.rstrip() for line in self.screen.display]
            while lines and not lines[-1]:
                lines.pop()
            if max_lines and len(lines) > max_lines:
                lines = lines[-max_lines:]
            return "\n".join(lines)

    def note_transcript_user_message(self, text: str) -> None:
        """K3 (fix 9): the transcript watcher read a user message; a typed submit may cite it."""
        self.transcript_user_seq += 1
        self.transcript_user_text = str(text or "")

    def get_full_scrollback_text(self, max_lines: int = 300) -> str:
        """Returns combined scrollback history + active screen lines from pyte.HistoryScreen."""
        with self._lock:
            hist_lines: List[str] = []
            try:
                for row in self.screen.history.top:
                    chars = "".join( row[col].data if col in row else " " for col in range(self.cols) ).rstrip()
                    hist_lines.append(chars)
            except Exception:
                pass
            curr_lines = [line.rstrip() for line in self.screen.display]
            all_lines = hist_lines + curr_lines
            while all_lines and not all_lines[-1]:
                all_lines.pop()
            if max_lines and len(all_lines) > max_lines:
                all_lines = all_lines[-max_lines:]
            return "\n".join(all_lines)

    def classify_end_reason(self, exit_code: Optional[int]) -> str:
        """Spec §1.4/§2: name an abnormal end instead of calling everything "exited".

        A non-zero code is always a crash. Code 0 after the agent reported completion is a clean exit;
        a plain shell's 0 is a clean quit; an agent TUI that dies with 0 before ever reporting
        completion is the "exit before completion" case the spec calls 异常退出.
        """
        if self.terminated_by_user:
            return "terminated"
        if exit_code is not None and int(exit_code) != 0:
            return "crashed"
        if self.completion_reported:
            return "exited"
        if (self.engine or "").strip().lower() in ("pwsh", "generic") or self.kind == "desktop_gui":
            return "exited"
        return "crashed"

    def is_process_alive(self) -> bool:
        """B9: "live" means the process is alive (PTY alive; dsh virtual sessions by status), never the status alone."""
        if self.pty is not None:
            try:
                return bool(self.pty.isalive())
            except Exception:
                return False
        if self.dsh_job_id or self.dsh_spec:
            return self.status in ("starting", "running", "waiting_parent")
        return self.status in ("running", "waiting_parent")

    def terminate(self) -> bool:
        """Closes the session. An ended session keeps its own final status (B9); only a live one becomes terminated.

        Returns True when this call really terminated a live process.
        """
        was_alive = self.is_process_alive()
        self.ended_at = time.time()
        scrollback = self.get_full_scrollback_text(max_lines=80)
        clean_lines = sanitize_tui_screen_text(scrollback, max_lines=10, engine=self.engine)
        if clean_lines:
            self.set_final_response("\n".join(clean_lines), "terminal_turn_snapshot")
        if was_alive:
            # R14: a live process always ends as terminated, even when telemetry had already marked it
            # completed. V-5: remember the cause so the PTY reader does not also emit an `exited` event.
            self.terminated_by_user = True
            self.status = "terminated"
            self.end_reason = "terminated"
            self.current_activity = "Terminated by parent/user"
        if self.pty and was_alive:
            try:
                self.pty.terminate(force=True)
            except Exception:
                pass
        if self.dsh_job_id and was_alive:
            job_id = self.dsh_job_id
            threading.Thread(
                target=lambda: httpx.post(f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}/cancel", timeout=3.0),
                daemon=True,
            ).start()
        if was_alive:
            return True
        # The process had already ended: keep its real final status and activity untouched.
        return False

    def to_dict(self) -> Dict[str, Any]:
        alive = self.is_process_alive()
        if not alive and self.pty is not None and self.status not in (
            "completed", "exited", "crashed", "terminated", "interrupted"
        ):
            self.status = "exited"
        return {
            "session_id": self.session_id,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "engine": self.engine,
            "approval_mode": self.approval_mode,
            "kind": self.kind,
            "title": self.title,
            "cwd": self.cwd,
            "gui_url": self.gui_url,
            "hwnd": self.hwnd,
            "tucked_offscreen": self.tucked_offscreen,
            "status": self.status,
            "process_alive": alive,
            "step_count": self.step_count,
            "current_activity": self.current_activity,
            "pending_question_id": self.pending_question_id,
            "pending_interaction": self.pending_interaction,
            "final_response": self.final_response,
            "final_response_source": self.final_response_source,
            # D2: true while the answer above still belongs to the turn before the current one.
            "from_previous_turn": self.final_response_from_previous_turn,
            "files_changed": self.files_changed,
            "pid": getattr(self.pty, "pid", None) if self.pty else None,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "last_output_at": self.last_output_at,
            "idle_seconds": round(time.time() - self.last_output_at, 1),
            "last_task": self.last_task,
            "cols": self.cols,
            "rows": self.rows,
            "dsh_job_id": self.dsh_job_id,
            "dsh_session_id": self.dsh_session_id,
            "effective_model": self.effective_model,
            "effective_thinking_level": self.effective_thinking_level,
            "transcript_path": str(self.transcript_path) if self.transcript_path else "",
            "end_reason": self.end_reason,
            "archived_at": self.archived_at,
            "resume_count": self.resume_count,
            "last_resumed_at": self.last_resumed_at,
            "resumable": self.resumable,
            "resume_block_reason": self.resume_block_reason,
            # A10: "pending" until the TUI really took the text; the outcome is recorded here.
            "last_injection": self.last_injection,
        }


class Win32WindowHelper:
    """Manages native Windows GUI Agent windows so they can be viewed & controlled inside the Web Console without cluttering the desktop."""

    @staticmethod
    def _process_exe_name(pid: int) -> str:
        """S4: the window owner's executable file name, or "" when Windows refuses the query."""
        if not pid:
            return ""
        handle = None
        try:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return ""
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value)
        except Exception:
            return ""
        finally:
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass

    @staticmethod
    def list_top_windows() -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title or title in ("Program Manager", "Settings", "Windows Input Experience"):
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 100 or h < 80:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            results.append({
                "hwnd": int(hwnd),
                "title": title,
                "pid": int(pid.value),
                "process_name": Win32WindowHelper._process_exe_name(int(pid.value)),
                "x": rect.left,
                "y": rect.top,
                "width": w,
                "height": h,
            })
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return results

    @staticmethod
    def capture_window_jpeg_b64(hwnd: int) -> Optional[Dict[str, Any]]:
        if not user32.IsWindow(hwnd):
            return None
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 10 or height <= 10:
            return None

        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        old_bmp = gdi32.SelectObject(mem_dc, bmp)

        user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buf_len = width * height * 4
        buf = ctypes.create_string_buffer(buf_len)
        gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(bmi), 0)

        gdi32.SelectObject(mem_dc, old_bmp)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

        img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1).convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=78)
        b64 = base64.b64encode(out.getvalue()).decode("ascii")
        return {"width": width, "height": height, "jpeg_b64": b64}

    @staticmethod
    def tuck_window_offscreen(session: AgentSession, tuck: bool) -> bool:
        hwnd = session.hwnd
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if tuck and not session.tucked_offscreen:
            session.original_rect = (rect.left, rect.top, w, h)
            user32.MoveWindow(hwnd, -3200, 100, w, h, True)
            session.tucked_offscreen = True
        elif not tuck and session.tucked_offscreen:
            orig_x, orig_y = (session.original_rect[0], session.original_rect[1]) if session.original_rect else (120, 120)
            user32.MoveWindow(hwnd, orig_x, orig_y, w, h, True)
            session.tucked_offscreen = False
        return True

    @staticmethod
    def send_click(hwnd: int, x: int, y: int):
        if not user32.IsWindow(hwnd):
            return False
        lparam = (int(y) << 16) | (int(x) & 0xFFFF)
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
        return True

    @staticmethod
    def send_text(hwnd: int, text: str):
        if not user32.IsWindow(hwnd):
            return False
        for ch in text:
            if ch in ("\n", "\r"):
                user32.PostMessageW(hwnd, WM_KEYDOWN, 0x0D, 0)
                user32.PostMessageW(hwnd, WM_KEYUP, 0x0D, 0)
            else:
                user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 0)
        return True


class HubSessionManager:
    """Central manager for Sub-Agent profiles, ConPTY sessions, 4-Layer Telemetry, and SQLite Post-Lifecycle Persistence."""

    def __init__(self, profiles_path: Path):
        self.profiles_path = profiles_path
        self.base_dir = profiles_path.parent
        self.runtime_configs_dir = self.base_dir / ".runtime_mcp_configs"
        self.runtime_configs_dir.mkdir(exist_ok=True)

        self.db = HubDatabase(self.base_dir / "hub_state.db")
        self.summarizer_registry = TranscriptSummarizerRegistry()
        self.model_discovery = AgentModelDiscovery()
        self.profiles: Dict[str, Dict[str, Any]] = {}
        self.engines_catalog: Dict[str, Any] = {}
        self.engine_policies: Dict[str, Dict[str, Any]] = {}
        self.global_routing_instructions: str = ""
        self.sessions: Dict[str, AgentSession] = {}
        # V2: one transcript file belongs to exactly one hub session. Two watchers must never tail
        # the same conversation, and a file that existed before a session started is not its own.
        self._claimed_transcripts: Dict[str, str] = {}
        self._transcript_lock = threading.RLock()
        # Q3: marker-less conversations are assigned in launch order per engine, so "pick candidate
        # + claim" has to be one step per engine, not one step per watcher.
        self._engine_transcript_locks: Dict[str, threading.RLock] = {}
        # Q2: a marker scan of the first TRANSCRIPT_MARKER_SCAN_BYTES, cached until the file grows.
        self._marker_scan_cache: Dict[str, tuple] = {}
        # Q5: profile mutations rebuild the in-memory state from the discovery already in hand. This
        # snapshot is the only discovery they are allowed to use; they never run discovery themselves.
        self._cached_discovery: Dict[str, Any] = {"engines": {}}
        # V-5: ids whose one lifecycle event is the terminated event, even if the PTY reader
        # reported its own exit just before close_session removed the session from the live list.
        self._user_terminated: Set[str] = set()
        # Archive spec §5: only one resume per session at a time; the second call waits for the first
        # and then behaves as "already live".
        self._resume_locks: Dict[str, asyncio.Lock] = {}

        # Bidirectional Mailbox & Sequenced Event Bus
        self.global_seq: int = self.db.get_max_seq()
        self.worker_events: List[Dict[str, Any]] = []
        self.parent_messages: Dict[str, List[Dict[str, Any]]] = {}
        # A11: worker→parent messages kept per session, outside the global 2000-event ring, so the
        # archive thread is complete even for a long-running session.
        self._worker_message_logs: Dict[str, List[Dict[str, Any]]] = {}
        self.pending_questions: Dict[str, Dict[str, Any]] = {}
        self._event_waiters: Set[asyncio.Event] = set()

        # B11: the constructor must never shell out to five CLIs. It loads the profile file at once
        # (using whatever discovery already has) and finishes discovery on a background thread, so
        # the very first /api/sessions poll answers immediately.
        self._discovery_ready = threading.Event()
        self.load_profiles(discovery=self.model_discovery.peek_cache() or {"engines": {}})
        # Archive spec §1.5/§5: rows a stopped hub left live become interrupted + archived, and ended
        # rows get their final record. D1: nothing is pruned here or anywhere else — records are kept
        # forever. Profiles are loaded first, because "still resumable" depends on the profile still
        # existing and being enabled.
        try:
            self._normalize_archive_on_startup()
        except Exception:
            pass
        # Spec §3: while a session is live its scrollback is written at most once every 60 s, only
        # when it changed — a timer, not a side effect of some other persist that may never come.
        threading.Thread(target=self._scrollback_flush_loop, name="hub-scrollback-flush", daemon=True).start()
        threading.Thread(target=self._initial_discovery, name="hub-initial-discovery", daemon=True).start()

    def _initial_discovery(self) -> None:
        try:
            self.load_profiles()
        except Exception:
            pass
        finally:
            self._discovery_ready.set()

    def wait_for_discovery(self, timeout: float = 60.0) -> bool:
        """Blocks until the startup discovery thread finished (used by scripts and tests)."""
        return self._discovery_ready.wait(timeout)

    # ---- V2: transcript ownership ---------------------------------------------------------

    @staticmethod
    def _session_spawn_anchor(session: AgentSession) -> float:
        """Q2: when the child process really started (falls back to the session's bookkeeping time)."""
        return float(getattr(session, "spawned_at", None) or session.created_at)

    @staticmethod
    def _conversation_creation_time(path: Path, levels_up: int = 0) -> tuple:
        """(creation timestamp, is_a_real_birth_time) of the conversation a transcript belongs to.

        Windows has a real creation time: `st_birthtime` on Python 3.12+. `levels_up` walks from the
        transcript file to its conversation directory.
        """
        target = Path(path)
        for _ in range(max(0, int(levels_up))):
            target = target.parent
        try:
            st = target.stat()
        except OSError:
            return None, False
        birth = getattr(st, "st_birthtime", None)
        if birth is not None:
            return float(birth), True
        return float(st.st_ctime), False

    @classmethod
    def _conversation_created_after(cls, path: Path, session: AgentSession, levels_up: int = 0) -> bool:
        """True when the conversation this file belongs to was created after the process started.

        A transcript that already existed before the launch belongs to another conversation; attaching
        to it is the V2 bug (a second session reporting the first session's thoughts). Q2: with a real
        creation time there is no negative slack; the 1 s slack only covers the fallback where
        `st_ctime` is metadata-change time rather than creation time.
        """
        created, exact = cls._conversation_creation_time(path, levels_up)
        if created is None:
            return False
        anchor = cls._session_spawn_anchor(session)
        if exact:
            return created >= anchor
        return created >= (anchor - 1.0)

    @staticmethod
    def _transcript_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def _transcript_claimed_by_other(self, path: Path, session: AgentSession) -> bool:
        key = self._transcript_key(path)
        with self._transcript_lock:
            owner = self._claimed_transcripts.get(key)
        return bool(owner) and owner != session.session_id

    def _transcript_marker_ids(self, path: Path) -> Set[str]:
        """Q2: hub session ids named in the transcript's first chunk (the task carries session_id=<id>).

        Only the head of the file is read, because the bridge hint travels with the initial task; the
        answer is cached until the file grows, so a steady-state scan costs one stat.
        """
        try:
            size = Path(path).stat().st_size
        except OSError:
            return set()
        key = self._transcript_key(path)
        cached = self._marker_scan_cache.get(key)
        if cached and cached[0] == size:
            return set(cached[1])
        try:
            with open(path, "rb") as fh:
                chunk = fh.read(TRANSCRIPT_MARKER_SCAN_BYTES)
        except OSError:
            return set()
        ids = set(TRANSCRIPT_MARKER_RE.findall(chunk.decode("utf-8", "replace")))
        self._marker_scan_cache[key] = (size, frozenset(ids))
        return ids

    def _engine_transcript_lock(self, engine: str) -> threading.RLock:
        eng = (engine or "").strip().lower()
        with self._transcript_lock:
            lock = self._engine_transcript_locks.get(eng)
            if lock is None:
                lock = threading.RLock()
                self._engine_transcript_locks[eng] = lock
            return lock

    def _session_has_transcript(self, session: AgentSession) -> bool:
        if session.transcript_path:
            return True
        with self._transcript_lock:
            return session.session_id in self._claimed_transcripts.values()

    _CODEX_ROLLOUT_UUID_RE = re.compile(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.IGNORECASE
    )

    @classmethod
    def _codex_session_id_from_rollout(cls, path: Path) -> Optional[str]:
        """The uuid of a codex rollout: filename tail, else session_meta.payload.id (research report)."""
        match = cls._CODEX_ROLLOUT_UUID_RE.search(Path(path).name)
        if match:
            return match.group(1)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for _ in range(20):
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "session_meta":
                        value = (obj.get("payload") or {}).get("id")
                        return str(value) if value else None
        except OSError:
            return None
        return None

    @classmethod
    def _conversation_ref_for(cls, engine: str, path: Path) -> Dict[str, Any]:
        """The engine conversation reference the watcher just claimed (spec §4), or {} when unknown."""
        eng = (engine or "").strip().lower()
        p = Path(path)
        try:
            if eng == "omp":
                # The hub-private omp_sess_<session> dir and its <ts>_<uuid>.jsonl.
                return {"session_dir": str(p.parent), "session_file": str(p)}
            if eng == "kimi":
                # .../sessions/wd_*/session_<uuid>/agents/main/wire.jsonl
                session_dir = p.parent.parent.parent
                return {"session_id": session_dir.name, "session_dir": str(session_dir)}
            if eng == "agy":
                # .../brain/<uuid>/.system_generated/logs/transcript.jsonl
                brain_dir = p.parent.parent.parent
                return {"conversation_id": brain_dir.name, "brain_dir": str(brain_dir)}
            if eng == "codex":
                sid = cls._codex_session_id_from_rollout(p)
                return {"session_id": sid or "", "rollout_path": str(p)}
        except Exception:
            return {}
        return {}

    def _claim_transcript(self, session: AgentSession, path: Path) -> bool:
        """Claims one transcript file for this session; False when another live session owns it."""
        key = self._transcript_key(path)
        with self._transcript_lock:
            owner = self._claimed_transcripts.get(key)
            if owner and owner != session.session_id:
                return False
            self._claimed_transcripts[key] = session.session_id
        session.transcript_path = Path(path)
        ref = self._conversation_ref_for(session.engine, Path(path))
        if ref:
            # Spec §3: filled as soon as the watcher claims a conversation.
            session.conversation_ref = ref
        self.persist_session(session)
        return True

    def _release_transcripts(self, session_id: str) -> None:
        with self._transcript_lock:
            for key, owner in list(self._claimed_transcripts.items()):
                if owner == session_id:
                    del self._claimed_transcripts[key]

    def _claim_conversation(self, session: AgentSession, candidates: List[Path], levels_up: int = 0) -> Optional[Path]:
        """Q2/Q3: marker first, then FIFO in launch order, all under one lock per engine.

        A transcript that names a hub session id belongs to that session, and no other session may
        claim it. Without a marker the earliest-spawned session of the engine that has no transcript
        yet takes the earliest-created unclaimed conversation; a later session waits its turn instead
        of grabbing the newest file.
        """
        paths = [Path(p) for p in candidates]
        if not paths:
            return None
        marked_for_me: List[Path] = []
        unmarked: List[Path] = []
        for path in paths:
            ids = self._transcript_marker_ids(path)
            if session.session_id in ids:
                marked_for_me.append(path)
            elif ids:
                # It names another hub session: never claim it, whatever its timestamps say.
                continue
            else:
                unmarked.append(path)

        if marked_for_me:
            with self._transcript_lock:
                for path in sorted(
                    marked_for_me,
                    key=lambda p: ((self._conversation_creation_time(p, levels_up)[0] or 0.0), str(p)),
                ):
                    if self._transcript_claimed_by_other(path, session):
                        continue
                    if self._claim_transcript(session, path):
                        return path

        engine = (session.engine or "").strip().lower()
        with self._engine_transcript_lock(engine):
            live = [
                s for s in list(self.sessions.values())
                if (s.engine or "").strip().lower() == engine and s.is_process_alive()
            ]
            live.sort(key=lambda s: (self._session_spawn_anchor(s), s.session_id))
            waiting = [s for s in live if not self._session_has_transcript(s)]
            if not waiting or waiting[0].session_id != session.session_id:
                # A session launched earlier has no conversation yet: this one waits its turn.
                return None
            ordered = [
                p for p in unmarked
                if self._conversation_created_after(p, session, levels_up)
            ]
            ordered.sort(
                key=lambda p: ((self._conversation_creation_time(p, levels_up)[0] or 0.0), str(p))
            )
            for path in ordered:
                if self._transcript_claimed_by_other(path, session):
                    continue
                if self._claim_transcript(session, path):
                    return path
        return None

    @staticmethod
    def _transcript_scan_interval(elapsed: float) -> float:
        """Q8: look every second for the first minute, then every 5 s, for as long as the session lives."""
        if elapsed < TRANSCRIPT_SCAN_FAST_WINDOW_SEC:
            return TRANSCRIPT_SCAN_FAST_INTERVAL_SEC
        return TRANSCRIPT_SCAN_SLOW_INTERVAL_SEC

    def _infer_engine(self, profile_id: str, prof: Dict[str, Any]) -> str:
        if prof.get("engine"):
            return str(prof["engine"]).lower()
        pid = profile_id.lower()
        for prefix in ("omp", "kimi", "agy", "codex", "dsh", "pwsh"):
            if pid.startswith(prefix):
                return prefix
        return "generic"

    def _build_engine_model_options(
        self,
        eng: str,
        engine_info: Dict[str, Any],
        all_models: List[str],
        disabled_models: List[str],
        model_notes: Dict[str, str],
        details: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """B3: the normalised `models` list the console and the routing guide use instead of raw CLI ids."""
        engine_levels = [str(x) for x in (engine_info.get("available_thinking_levels") or [])]
        detail_by_id: Dict[str, Dict[str, Any]] = {}
        for d in details:
            mid = str(d.get("id") or "")
            if mid and mid not in detail_by_id:
                detail_by_id[mid] = d
        disabled = set(disabled_models or [])
        notes = model_notes or {}

        def _levels_for(mid: str) -> List[str]:
            d = detail_by_id.get(mid) or {}
            if eng == "omp":
                # The omp CLI accepts its own level names for every model it can reach.
                omp_levels = d.get("omp_cli_thinking_levels")
                if omp_levels:
                    return [str(x) for x in omp_levels]
            supported = d.get("supported_thinking_levels")
            if supported:
                return [str(x) for x in supported]
            return list(engine_levels)

        result: List[Dict[str, Any]] = []
        if eng == "agy":
            # agy carries its thinking level in the model name suffix: group the raw ids by base.
            order: List[str] = []
            groups: Dict[str, Dict[str, Any]] = {}
            for mid in all_models:
                base, suffix = split_agy_model_id(mid)
                if base not in groups:
                    groups[base] = {"ids": [], "levels": [], "first": mid, "enabled": False}
                    order.append(base)
                grp = groups[base]
                grp["ids"].append(mid)
                if suffix and suffix not in grp["levels"]:
                    grp["levels"].append(suffix)
                if mid not in disabled:
                    grp["enabled"] = True
            for base in order:
                grp = groups[base]
                # Keep the engine's own order (low, medium, high), then anything unexpected.
                levels = [l for l in AGY_THINKING_LEVELS if l in grp["levels"]]
                levels += [l for l in grp["levels"] if l not in AGY_THINKING_LEVELS]
                first_detail = detail_by_id.get(grp["first"]) or {}
                label = str(first_detail.get("display_name") or base)
                label = re.sub(r"\s*\((low|medium|high)\)\s*$", "", label, flags=re.IGNORECASE).strip() or base
                _first_base, first_suffix = split_agy_model_id(grp["first"])
                result.append(
                    {
                        "id": base,
                        "label": label,
                        "thinking_levels": levels,
                        "default_thinking_level": first_suffix if first_suffix in levels else None,
                        "enabled": grp["enabled"],
                        "note": str(notes.get(base) or ""),
                    }
                )
        else:
            for mid in all_models:
                d = detail_by_id.get(mid) or {}
                levels = _levels_for(mid)
                default_level = d.get("default_thinking_level")
                if default_level not in levels:
                    default_level = None
                label = str(d.get("display_name") or mid)
                result.append(
                    {
                        "id": mid,
                        "label": label or mid,
                        "thinking_levels": levels,
                        "default_thinking_level": default_level,
                        "enabled": mid not in disabled,
                        "note": str(notes.get(mid) or ""),
                    }
                )

        # Enabled first, discovery order preserved inside each group.
        return [m for m in result if m["enabled"]] + [m for m in result if not m["enabled"]]

    def load_profiles(self, refresh_discovery: bool = False, discovery: Optional[Dict[str, Any]] = None):
        """Rebuilds profiles from disk + discovery. Only the explicit refresh paths may discover here.

        Q5: profile-mutating methods never call this without a discovery snapshot, so they never run
        a CLI on the event loop; `_reload_after_write` is their entry point.
        """
        if discovery is None:
            discovery = self.model_discovery.discover_all(force_refresh=refresh_discovery)
        self._cached_discovery = discovery or {"engines": {}}
        data = self._read_profiles_for_load()
        if data is None:
            # Q6: the file is held by another program; keep the profiles already in memory instead of
            # replacing them with {} (the console must never show an empty list because of a lock).
            return
        self._apply_profile_data(data, self._cached_discovery)

    def _read_profiles_for_load(self) -> Optional[Dict[str, Any]]:
        """Reads the profile file for the console; None means "locked, keep what is in memory"."""
        if not self.profiles_path.exists():
            return {"profiles": {}}
        try:
            text = self._read_text_with_retries(self.profiles_path)
        except ValueError:
            return None
        except OSError:
            return {}
        try:
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _apply_profile_data(self, data: Dict[str, Any], discovery: Dict[str, Any]):
        """Rebuilds the in-memory catalog and profiles from one file document and one discovery snapshot."""
        raw_engines = (discovery or {}).get("engines", {}) or {}

        self.engine_policies = data.get("engine_policies") or {}
        self.global_routing_instructions = data.get("global_routing_instructions") or (
            "【主 Agent 调度总则】\n"
            "1. 调用 Sub-Agent 前请参考各 Profile 的 `when_to_use` 注释，根据任务复杂度、延迟要求和上下文体量选择最合适的 Agent 与思考等级；\n"
            "2. 已被用户禁用 (`disabled_models` 或 `enabled=false`) 的模型与 Profile 严禁调用。"
        )

        # Apply per-engine model enable/disable policies
        processed_engines: Dict[str, Any] = {}
        for eng, eng_info in raw_engines.items():
            e_copy = dict(eng_info)
            all_mods = list(e_copy.get("all_discovered_models") or e_copy.get("available_models", []))
            pol = self.engine_policies.get(eng) or {}
            disabled_mods = list(pol.get("disabled_models") or [])
            mod_notes = dict(pol.get("model_notes") or {})
            enabled_mods = [m for m in all_mods if m not in disabled_mods]

            e_copy["all_discovered_models"] = all_mods
            e_copy["disabled_models"] = disabled_mods
            e_copy["model_notes"] = mod_notes
            e_copy["available_models"] = enabled_mods

            details = []
            for md in (e_copy.get("model_details") or []):
                md_copy = dict(md)
                mid = md_copy.get("id", "")
                md_copy["enabled"] = mid not in disabled_mods
                if mid in mod_notes:
                    md_copy["note"] = mod_notes[mid]
                details.append(md_copy)
            e_copy["model_details"] = details
            e_copy["models"] = self._build_engine_model_options(
                eng=eng,
                engine_info=e_copy,
                all_models=all_mods,
                disabled_models=disabled_mods,
                model_notes=mod_notes,
                details=details,
            )
            processed_engines[eng] = e_copy

        self.engines_catalog = processed_engines

        raw_profiles = data.get("profiles", {})
        enriched: Dict[str, Dict[str, Any]] = {}
        for pid, p in raw_profiles.items():
            prof = dict(p)
            eng = self._infer_engine(pid, prof)
            prof["engine"] = eng
            prof["enabled"] = bool(prof.get("enabled", True))
            prof["when_to_use"] = prof.get("when_to_use") or ""
            eng_info = self.engines_catalog.get(eng) or {}
            # B4: remember whether the default came from the profile file or from the engine.
            explicit_model = str(prof.get("default_model") or "").strip()
            explicit_thinking = str(prof.get("default_thinking_level") or "").strip()
            if eng == "agy" and explicit_model:
                # agy ids carry a thinking suffix: store the base, keep the suffix as the thinking level.
                base_model, suffix_level = split_agy_model_id(explicit_model)
                explicit_model = base_model
                if not explicit_thinking and suffix_level:
                    explicit_thinking = suffix_level
            if explicit_model:
                prof["default_model"] = explicit_model
            elif eng_info.get("default_model"):
                prof["default_model"] = eng_info["default_model"]
            if explicit_thinking:
                prof["default_thinking_level"] = explicit_thinking
            elif eng_info.get("default_thinking_level"):
                prof["default_thinking_level"] = eng_info["default_thinking_level"]
            prof["default_model_source"] = "profile" if explicit_model else "engine"
            prof["default_thinking_source"] = "profile" if explicit_thinking else "engine"

            all_mods = eng_info.get("all_discovered_models", [])
            prof_disabled = list(
                dict.fromkeys(list(eng_info.get("disabled_models", [])) + list(prof.get("disabled_models") or []))
            )
            prof["all_discovered_models"] = all_mods
            prof["disabled_models"] = prof_disabled
            prof["available_models"] = [m for m in all_mods if m not in prof_disabled]
            prof["available_thinking_levels"] = eng_info.get("available_thinking_levels") or prof.get(
                "available_thinking_levels", []
            )
            prof["model_details"] = eng_info.get("model_details", [])
            enriched[pid] = prof

        # B2: discovery may still report ~/.codex [profiles.*] and recent configurations in the
        # catalog, but they no longer synthesise profiles. A Profile is a launch recipe, not a combo.

        self.profiles = enriched

    def _reload_after_write(self, data: Dict[str, Any]) -> None:
        """Q5: the mutation already holds the document it just wrote — apply it with the cached discovery.

        No discovery run, no disk re-read: the console stays responsive while a profile is saved, and
        the catalog-dependent validation reports 「模型列表尚未就绪，请稍后再试」 when nothing is cached.
        """
        self._apply_profile_data(data, self._cached_discovery)

    def get_enabled_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Returns only enabled profiles where the default model is not disabled (or dynamic base profiles)."""
        result: Dict[str, Dict[str, Any]] = {}
        for pid, p in self.profiles.items():
            if not p.get("enabled", True):
                continue
            result[pid] = p
        return result

    def build_routing_guide_markdown(self) -> str:
        """B12: the guide parent agents read. Only enabled profiles and enabled models appear."""
        lines: List[str] = ["# Sub-Agent 调用与路由指南"]
        instructions = (self.global_routing_instructions or "").strip()
        if instructions:
            lines.append(instructions)
        lines.append("")
        lines.append("## Profile（agent_type）")
        for pid, p in self.profiles.items():
            if not p.get("enabled", True):
                continue
            name = p.get("name") or pid
            def_mod = p.get("default_model") or "无"
            def_thk = p.get("default_thinking_level") or "无"
            lines.append(f"- `{pid}` — {name}｜默认：{def_mod} · {def_thk}")
            when = (p.get("when_to_use") or p.get("description") or "").strip()
            if when:
                lines.append(f"  何时用：{when}")
        lines.append("")
        lines.append("## 可用模型与思考等级（model / thinking_level）")
        for eng, info in self.engines_catalog.items():
            models = info.get("models") or []
            if not models:
                continue
            header = f"### {eng}"
            if eng == "agy":
                header += "（思考等级会拼成模型名后缀）"
            lines.append(header)
            for m in models:
                if not m.get("enabled"):
                    continue
                levels = ", ".join(m.get("thinking_levels") or []) or "无"
                note = (m.get("note") or "").strip()
                model_line = f"- `{(m.get('id') or '')}`"
                if note:
                    model_line += f" — {note}"
                model_line += f"｜思考等级：{levels}"
                lines.append(model_line)
        disabled_groups: List[str] = []
        for eng, info in self.engines_catalog.items():
            disabled_entries = [m.get("id") for m in (info.get("models") or []) if not m.get("enabled")]
            if disabled_entries:
                disabled_groups.append(f"{eng}: {', '.join(str(x) for x in disabled_entries)}")
        if disabled_groups:
            lines.append("")
            lines.append("已停用（禁止调用）：" + "，".join(disabled_groups))
        return "\n".join(lines)

    # ---- B13: model enable/disable + per-model notes -------------------------------------

    PROFILE_PERSIST_KEYS = (
        "id",
        "name",
        "engine",
        "kind",
        "command",
        "args",
        "default_model",
        "default_thinking_level",
        "task_mode",
        "startup_delay_sec",
        "submit_key",
        "enabled",
        "when_to_use",
        "description",
        "gui_url",
        "dsh_tier",
        "disabled_models",
    )

    # B5 / R11: every persistable key except these three can be cleared with null or "". The stored
    # key is deleted, so `startup_delay_sec` falls back to 2.5 and `task_mode` to pty_input.
    PROFILE_REQUIRED_KEYS = ("name", "engine", "command")

    def _read_text_with_retries(self, path: Path) -> str:
        """Q6: a read retries a Windows file lock the same way a write does (30 ms, 10 attempts)."""
        last: Optional[BaseException] = None
        for _attempt in range(PROFILE_FILE_RETRY_ATTEMPTS):
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                if not _is_file_lock_error(exc):
                    raise
                last = exc
                time.sleep(PROFILE_FILE_RETRY_DELAY_SEC)
        raise ValueError(PROFILE_FILE_LOCKED_MESSAGE) from last

    def _read_profiles_file(self) -> Dict[str, Any]:
        """Reads the profile file for a caller that is about to write it back.

        R16: a parse error raises instead of returning an empty document. Q6: a file another program
        holds open raises 「配置文件被其他程序占用，请稍后重试」 after the retry loop, never the
        parse-error message and never a bare 500.
        """
        if not self.profiles_path.exists():
            return {"profiles": {}}
        text = self._read_text_with_retries(self.profiles_path)
        try:
            data = json.loads(text)
        except Exception as exc:
            raise ValueError(f"agent_profiles.json 解析失败，本次修改未写入：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("agent_profiles.json 顶层不是 JSON 对象，本次修改未写入。")
        return data

    def _write_profiles_file(self, raw_data: Dict[str, Any]) -> None:
        """R16: write a temp file in the same folder and replace, so a crash never leaves a half file.

        LF line endings, UTF-8, Chinese kept readable (`ensure_ascii=False`), indent 2. Q6: the temp
        open and the replace both retry a Windows lock (10 × 30 ms) before the save reports the
        「配置文件被其他程序占用」 ValueError.
        """
        text = json.dumps(raw_data, ensure_ascii=False, indent=2) + "\n"
        target = self.profiles_path
        last: Optional[BaseException] = None
        for _attempt in range(PROFILE_FILE_RETRY_ATTEMPTS):
            fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, target)
                return
            except OSError as exc:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                if not _is_file_lock_error(exc):
                    raise
                last = exc
                time.sleep(PROFILE_FILE_RETRY_DELAY_SEC)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        raise ValueError(PROFILE_FILE_LOCKED_MESSAGE) from last

    def engine_model_options(self, engine: str) -> List[Dict[str, Any]]:
        info = self.engines_catalog.get((engine or "").strip().lower()) or {}
        return list(info.get("models") or [])

    def engine_model_option(self, engine: str, model_id: str) -> Optional[Dict[str, Any]]:
        mid = (model_id or "").strip()
        for m in self.engine_model_options(engine):
            if m.get("id") == mid:
                return m
        return None

    def _sorted_levels_by_engine_order(self, engine: str, levels: List[str]) -> List[str]:
        info = self.engines_catalog.get(engine) or {}
        order = [str(x) for x in (info.get("available_thinking_levels") or [])]
        seen = [l for l in order if l in levels]
        return seen + [l for l in levels if l not in seen]

    def toggle_engine_model(
        self,
        engine: str,
        model_id: str,
        enabled: Optional[bool] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """B13: enable/disable one model (agy: a base id toggles every suffixed id of that base) and edit its note.

        R6: `enabled=None` means a note-only update; the model's enable/disable state is left exactly as
        it is. An empty note deletes it.
        """
        if enabled is None and note is None:
            raise ValueError("缺少 enabled 或 note，无法更新模型。")
        eng = (engine or "").strip().lower()
        mid = (model_id or "").strip()
        if not eng or not mid:
            raise ValueError("缺少引擎或模型标识。")
        if eng not in KNOWN_ENGINES:
            raise ValueError(f"未知引擎「{eng}」。可用：{', '.join(KNOWN_ENGINES)}")

        # Accept either a raw discovered id or a B3 base id (agy groups its suffix variants).
        catalog_ids = set(str(x) for x in (self.engines_catalog.get(eng, {}).get("all_discovered_models") or []))
        base_ids = set(str(m.get("id")) for m in self.engine_model_options(eng) if m.get("id"))
        base, _suffix = split_agy_model_id(mid) if eng == "agy" else (mid, None)
        if catalog_ids and mid not in catalog_ids and mid not in base_ids and base not in base_ids:
            raise ValueError(f"引擎「{eng}」里没有模型「{mid}」。")

        # Every raw id this call must write policy for (agy: all suffixes of the base).
        if eng == "agy":
            targets = sorted(m for m in catalog_ids if split_agy_model_id(m)[0] == base)
            if not targets:
                # Policy may key a base id directly when discovery reported no suffix variants.
                targets = [base]
        else:
            targets = [mid]

        raw_data = self._read_profiles_file()
        policies = raw_data.setdefault("engine_policies", {})
        eng_pol = policies.setdefault(eng, {"disabled_models": [], "model_notes": {}})
        disabled_list: List[str] = [str(m) for m in (eng_pol.get("disabled_models") or [])]

        if enabled is not None:
            for target in targets:
                if enabled:
                    disabled_list = [m for m in disabled_list if m != target]
                elif target not in disabled_list:
                    disabled_list.append(target)
            eng_pol["disabled_models"] = disabled_list

        note_engine_key = base if eng == "agy" else mid
        if note is not None:
            notes_map = eng_pol.setdefault("model_notes", {})
            cleaned = str(note).strip()
            if cleaned:
                notes_map[note_engine_key] = cleaned
            else:
                notes_map.pop(note_engine_key, None)

        self._write_profiles_file(raw_data)
        self._reload_after_write(raw_data)
        return self.engines_catalog.get(eng, {})

    def save_global_routing_instructions(self, instructions: str) -> str:
        """Persists global routing instructions for Parent Agents into agent_profiles.json."""
        raw_data = self._read_profiles_file()
        raw_data["global_routing_instructions"] = instructions.strip()
        self._write_profiles_file(raw_data)
        self._reload_after_write(raw_data)
        return self.global_routing_instructions

    # ---- B5: saving and deleting profiles ------------------------------------------------

    def _validate_profile_input(
        self,
        eng: str,
        args: List[str],
        kind: str,
        default_model: Optional[str],
        default_thinking: Optional[str],
    ) -> None:
        bad = injected_flags_in_args(eng, args)
        if bad:
            raise ValueError(
                f"参数里不要写模型或思考等级（{bad[0]}）：Hub 会按启动时的选择自动注入。"
            )
        if kind not in ("tui", "web_gui"):
            raise ValueError(f"会话形式「{kind}」无效，只能是 tui 或 web_gui。")
        if kind == "web_gui" and eng != "dsh":
            raise ValueError("只有 dsh 引擎可以使用 web_gui 会话形式。")

        options = self.engine_model_options(eng)
        if not options:
            if eng in ("pwsh", "generic") or self.engines_catalog.get(eng) is not None:
                # pwsh really has no model choice; a discovered engine with zero models is a discovery
                # gap (R4), and its profile defaults are accepted unvalidated.
                return
            if default_model or default_thinking:
                # Q5: the catalog is not cached yet (startup discovery is still running). Say so
                # instead of blocking the event loop on a discovery run just to validate this save.
                raise ValueError("模型列表尚未就绪，请稍后再试")
            return
        ids = [str(m.get("id")) for m in options]
        enabled_ids = [str(m.get("id")) for m in options if m.get("enabled")]
        chosen = self.engine_model_option(eng, default_model) if default_model else None
        if default_model and not chosen:
            if eng == "agy":
                base, _suffix = split_agy_model_id(str(default_model))
                chosen = self.engine_model_option(eng, base)
            if chosen is None:
                raise ValueError(f"模型「{default_model}」不在引擎「{eng}」的可用模型里。可用：{', '.join(enabled_ids) or '无'}")
        if chosen is not None and not chosen.get("enabled"):
            raise ValueError(f"模型「{chosen.get('id')}」已被停用，不能设为默认模型。")
        if default_thinking and chosen is not None:
            levels = [str(x) for x in (chosen.get("thinking_levels") or [])]
            if str(default_thinking) not in levels:
                raise ValueError(
                    f"{chosen.get('id')} 支持的思考等级：{', '.join(levels) or '无'}"
                )
        if default_model and str(default_model) not in ids and not chosen:
            raise ValueError(f"模型「{default_model}」不在引擎「{eng}」的可用模型里。")

    def save_or_update_profile(self, profile_data: Dict[str, Any]) -> Dict[str, Any]:
        """B5: persists one Profile as a launch recipe, dropping every enrichment key."""
        pid = str(profile_data.get("id") or "").strip()
        if not pid:
            raise ValueError("Profile 的 id 不能为空。")

        raw_data = self._read_profiles_file()
        profiles_map = raw_data.setdefault("profiles", {})
        existing = profiles_map.get(pid) or {}
        if not isinstance(existing, dict):
            existing = {}

        incoming = dict(profile_data)
        merged = dict(existing)
        for key in self.PROFILE_PERSIST_KEYS:
            if key not in incoming:
                continue
            value = incoming[key]
            if key not in self.PROFILE_REQUIRED_KEYS and (
                value is None or (isinstance(value, str) and not value.strip())
            ):
                merged.pop(key, None)
                continue
            merged[key] = value

        for required in ("name", "engine", "command"):
            value = merged.get(required)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"Profile 的 {required} 不能清空。")

        eng = self._infer_engine(pid, merged)
        merged["engine"] = eng
        merged["id"] = pid

        raw_args = merged.get("args")
        if raw_args is None:
            # R11: a cleared args key means "no arguments", not a validation failure.
            raw_args = []
        if not isinstance(raw_args, list) or any(not isinstance(a, str) for a in raw_args):
            raise ValueError("参数（args）必须是字符串列表，每行一个参数。")
        args = [a for a in raw_args]
        if len(args) > MAX_LAUNCH_ARGS:
            raise ValueError("参数（args）过多。")

        kind = str(merged.get("kind") or "tui")
        default_model = merged.get("default_model")
        default_model = str(default_model).strip() if default_model else None
        if not default_model:
            default_model = None
        default_thinking = merged.get("default_thinking_level")
        default_thinking = str(default_thinking).strip() if default_thinking else None
        if not default_thinking:
            default_thinking = None

        self._validate_profile_input(eng, args, kind, default_model, default_thinking)

        if eng == "agy" and default_model:
            default_model, suffix_level = split_agy_model_id(default_model)
            merged["default_model"] = default_model
            # R8: a suffixed model id only supplies the level when the caller did not supply one.
            if suffix_level and not default_thinking:
                merged["default_thinking_level"] = suffix_level

        clean_entry: Dict[str, Any] = {}
        for key in self.PROFILE_PERSIST_KEYS:
            if key in merged:
                clean_entry[key] = merged[key]
        clean_entry["id"] = pid
        clean_entry["engine"] = eng
        clean_entry["args"] = args
        # R11: a key the caller cleared is not written back. The readers below default what is missing:
        # load_profiles fills enabled / when_to_use, resolve_launch defaults kind, create_session `\r`
        # for submit_key and 2.5 for a missing startup_delay_sec.
        if not clean_entry.get("name"):
            clean_entry["name"] = pid
        if clean_entry.get("default_model") in (None, ""):
            clean_entry.pop("default_model", None)
        if clean_entry.get("default_thinking_level") in (None, ""):
            clean_entry.pop("default_thinking_level", None)

        profiles_map[pid] = clean_entry
        self._write_profiles_file(raw_data)
        self._reload_after_write(raw_data)
        return self.profiles.get(pid, {})

    def delete_profile(self, profile_id: str) -> bool:
        """B5: deletes one Profile; an unknown id raises a Chinese ValueError."""
        pid = (profile_id or "").strip()
        raw_data = self._read_profiles_file()
        profiles_map = raw_data.get("profiles") or {}
        if pid not in profiles_map:
            raise ValueError(f"找不到 Profile「{pid}」。")
        del profiles_map[pid]
        self._write_profiles_file(raw_data)
        self._reload_after_write(raw_data)
        return True


    def _notify_waiters(self):
        for ev in list(self._event_waiters):
            ev.set()

    def persist_session(
        self, session: AgentSession, force_scrollback: bool = False, final: bool = False
    ):
        """Writes the row. `final` marks the exit/archive write, the only one that rebuilds the
        mailbox thread (A11) instead of leaving the stored one alone."""
        scrollback = session.get_full_scrollback_text(max_lines=250)
        self.db.upsert_session(
            session.to_dict(),
            scrollback_text=scrollback,
            archive=self._archive_payload(session, force_scrollback=force_scrollback, final=final),
        )

    # ---- Archive & resume (spec archive-spec.md §2-§5) --------------------------------------

    @staticmethod
    def _last_screen_text(session: AgentSession) -> str:
        """The last 20 non-empty screen lines, as the final record keeps them (spec §3)."""
        try:
            lines = [ln for ln in session.get_clean_screen_text(max_lines=80).splitlines() if ln.strip()]
        except Exception:
            lines = []
        return "\n".join(lines[-20:])

    @staticmethod
    def _first_line_text(text: Any) -> str:
        for line in str(text or "").splitlines():
            if line.strip():
                return line.strip()
        return ""

    def _mailbox_thread(self, session_id: str) -> List[Dict[str, Any]]:
        """The mailbox thread in both directions, as the archive record stores it (spec §3, A11)."""
        thread: List[Dict[str, Any]] = []
        for msg in self.parent_messages.get(session_id, []):
            thread.append({
                "direction": "parent",
                "timestamp": msg.get("timestamp"),
                "content": msg.get("content", ""),
                "message_id": msg.get("id"),
                "resolved_question_id": msg.get("resolved_question_id"),
            })
        # A11: the worker side comes from the per-session log, not from the bounded event ring.
        for ev in getattr(self, "_worker_message_logs", {}).get(session_id, []):
            thread.append({
                "direction": "worker",
                "timestamp": ev.get("timestamp"),
                "content": ev.get("content", ""),
                "type": ev.get("type"),
                "seq": ev.get("seq"),
            })
        thread.sort(key=lambda item: float(item.get("timestamp") or 0))
        return thread

    def _remember_worker_message(self, session_id: str, record: Dict[str, Any]) -> None:
        """A11: keeps the worker→parent side of the archive thread outside the 2000-event ring."""
        if record.get("type") not in MAILBOX_EVENT_TYPES:
            return
        logs = getattr(self, "_worker_message_logs", None)
        if logs is None:
            logs = {}
            self._worker_message_logs = logs
        entries = logs.setdefault(session_id, [])
        entries.append({
            "direction": "worker",
            "timestamp": record.get("timestamp"),
            "content": record.get("content", ""),
            "type": record.get("type"),
            "seq": record.get("seq"),
        })
        if len(entries) > WORKER_MESSAGE_LOG_CAP:
            del entries[: len(entries) - WORKER_MESSAGE_LOG_CAP]

    def _scrollback_blob(self, session: AgentSession, force: bool = False) -> Optional[bytes]:
        """zlib-compressed newest ≤512 KB of the raw terminal stream (spec §1.6/§3).

        While the session is live it is written at most once every 60 s and only when the stream
        changed; the final record always writes it.
        """
        now = time.time()
        if not force:
            if now - session._scrollback_written_at < RESUME_SCROLLBACK_INTERVAL_SEC:
                return None
            if session.raw_history_rev == session._scrollback_rev_written:
                return None
        # W9: cut at a line break and keep the newest resume banner (a raw byte slice can split an
        # escape sequence or drop the banner the pane is supposed to show above the new output).
        text = trim_history_text("".join(session.raw_history), RESUME_SCROLLBACK_MAX_BYTES)
        raw = text.encode("utf-8", "replace")
        session._scrollback_written_at = now
        session._scrollback_rev_written = session.raw_history_rev
        return zlib.compress(raw, 6)

    def _archive_payload(
        self, session: AgentSession, force_scrollback: bool = False, final: bool = False
    ) -> Dict[str, Any]:
        # A11: the thread is built on the exit/archive write only; every other persist passes "" and
        # the COALESCE upsert keeps what the final record already stored.
        thread = self._mailbox_thread(session.session_id) if final else []
        return {
            "end_reason": session.end_reason,
            "archived_at": session.archived_at,
            "spawned_at": session.spawned_at,
            "profile_snapshot": (
                json.dumps(session.profile_snapshot, ensure_ascii=False) if session.profile_snapshot else ""
            ),
            "conversation_ref": (
                json.dumps(session.conversation_ref, ensure_ascii=False) if session.conversation_ref else ""
            ),
            "scrollback": self._scrollback_blob(session, force=force_scrollback),
            "last_screen": session.last_screen or "",
            "parent_messages": json.dumps(thread, ensure_ascii=False) if thread else "",
            "resume_count": session.resume_count,
            "last_resumed_at": session.last_resumed_at,
            "resumable": None if session.resumable is None else (1 if session.resumable else 0),
            "resume_block_reason": session.resume_block_reason,
        }

    @staticmethod
    def _record_from_session(session: AgentSession) -> Dict[str, Any]:
        rec = session.to_dict()
        rec["profile_snapshot"] = session.profile_snapshot
        rec["conversation_ref"] = session.conversation_ref
        return rec

    @staticmethod
    def _sticky_resume_block(record: Dict[str, Any]) -> str:
        """A4: a stored failure that re-checking must never turn back into "resumable".

        agy opened a different conversation (the archived one is not attached) and codex archived
        the conversation (only `codex unarchive` fixes that). Both are facts about the engine's own
        store, not about a file that may reappear.
        """
        reason = str(record.get("resume_block_reason") or "")
        if not reason:
            return ""
        if reason in STICKY_RESUME_BLOCK_REASONS:
            return reason
        for prefix in STICKY_RESUME_BLOCK_PREFIXES:
            if reason.startswith(prefix):
                return reason
        return ""

    def _finalize_session(self, session: AgentSession, end_reason: Optional[str] = None) -> AgentSession:
        """Writes the parts of the final record that only exist once the session has stopped."""
        if end_reason:
            session.end_reason = end_reason
        if session.ended_at is None:
            session.ended_at = time.time()
        if not session.end_reason:
            session.end_reason = session.classify_end_reason(session.exit_code)
        session.last_screen = self._last_screen_text(session)
        sticky = self._sticky_resume_block(self._record_from_session(session))
        if sticky:
            # A4: never recompute a sticky failure into "resumable".
            session.resumable = False
            session.resume_block_reason = sticky
            return session
        ok, reason = self.compute_resumable(self._record_from_session(session))
        session.resumable = ok
        session.resume_block_reason = None if ok else reason
        return session

    def _archive_session(self, session: AgentSession, end_reason: Optional[str] = None) -> AgentSession:
        """The final record, written when a session leaves the live list or at hub shutdown/start."""
        session.archived_at = time.time()
        self._finalize_session(session, end_reason=end_reason)
        # V5: an archived record must never report a live status; its end state is the truth.
        if session.end_reason:
            session.status = session.end_reason
        self.persist_session(session, force_scrollback=True, final=True)
        return session

    def compute_resumable(self, record: Dict[str, Any]) -> tuple:
        """(can_resume, Chinese reason) for one hub record — the §4 pre-checks, re-checked on demand."""
        sticky = self._sticky_resume_block(record)
        if sticky:
            # A4/A5: a sticky failure stays a failure however often this is re-checked.
            return False, sticky
        engine = str(record.get("engine") or "").strip().lower()
        snapshot = record.get("profile_snapshot") or {}
        ref = record.get("conversation_ref") or {}
        session_id = str(record.get("session_id") or "")
        if not isinstance(snapshot, dict) or not snapshot:
            return False, "未记录启动参数，无法恢复"
        if engine not in RESUME_ENGINES:
            return False, ENGINE_RESUME_BLOCK_REASONS.get(engine, DEFAULT_RESUME_BLOCK_REASON)
        cwd = str(snapshot.get("cwd") or record.get("cwd") or "")
        if not cwd or not os.path.isdir(cwd):
            return False, f"工作目录已不存在：{cwd}"
        profile_id = str(snapshot.get("profile_id") or record.get("profile_id") or "")
        prof = self.profiles.get(profile_id)
        if not prof or not prof.get("enabled", True):
            return False, f"Profile「{profile_id}」不存在或已停用"
        if not isinstance(ref, dict) or not ref:
            return False, "未记录到该会话的对话位置，无法恢复"
        if engine == "omp":
            path = str(ref.get("session_file") or "")
            if not path or not os.path.isfile(path):
                return False, f"omp 会话文件已不存在：{path}"
            with self._transcript_lock:
                owner = self._claimed_transcripts.get(self._transcript_key(Path(path)))
            if owner and owner != session_id:
                live = self.sessions.get(owner)
                if live is not None and live.is_process_alive():
                    return False, "该 omp 会话正被另一个运行中的会话使用"
        elif engine == "kimi":
            sid = str(ref.get("session_id") or "")
            sdir = str(ref.get("session_dir") or "")
            if not sid or not sdir or not os.path.isdir(sdir):
                return False, f"Kimi 会话目录已不存在：{sdir or sid}"
            lock_path = kimi_store_root() / "locks" / "sessions" / f"{sid}.lock"
            if lock_path.exists():
                holder = 0
                try:
                    holder = int((lock_path.read_text(encoding="utf-8", errors="replace").split(":", 1)[0] or "0").strip())
                except Exception:
                    holder = 0
                if holder and holder != os.getpid() and _pid_alive(holder):
                    return False, KIMI_LOCKED_MESSAGE
        elif engine == "agy":
            # V3/A3: the durable store is conversations/<uuid>.db or the brain dir itself. Keying the
            # check on transcript.jsonl was wrong: the real store has brain dirs without one.
            # The conversations store is a SIBLING of the brain dir: <root>/conversations/<uuid>.db
            # beside <root>/brain/<uuid> (confirmed against the real antigravity-cli layout).
            brain = str(ref.get("brain_dir") or "")
            conv_id = str(ref.get("conversation_id") or (Path(brain).name if brain else ""))
            durable_db = Path(brain).parent.parent / "conversations" / f"{conv_id}.db" if brain else None
            brain_ok = bool(brain) and os.path.isdir(brain)
            if not brain_ok and not (durable_db is not None and durable_db.is_file()):
                return False, f"AGY 会话目录已不存在：{brain}"
        elif engine == "codex":
            sid = str(ref.get("session_id") or "")
            path = str(ref.get("rollout_path") or "")
            if not sid:
                return False, "未记录到 codex 会话 ID，无法恢复"
            if not path or not os.path.isfile(path):
                return False, f"codex 会话记录已不存在：{path}"
        elif engine == "dsh":
            if not ref.get("dsh_job_id"):
                return False, "DSH 任务 ID 缺失，无法恢复"
        return True, ""

    def build_resume_launch(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """The revive command for one record (spec §4), built from profile_snapshot + conversation_ref.

        Pure: it never spawns anything and never touches the engine's store beyond what the caller
        already checked. The message is appended later by resume_subagent, per the engine's task mode.
        """
        engine = str(record.get("engine") or "").strip().lower()
        snapshot = record.get("profile_snapshot") or {}
        ref = record.get("conversation_ref") or {}
        cwd = str(snapshot.get("cwd") or record.get("cwd") or "")
        command = str(snapshot.get("command") or "")
        base_args = [str(a) for a in (snapshot.get("args") or [])]
        env = {str(k): str(v) for k, v in (snapshot.get("env") or {}).items()}
        built: Dict[str, Any] = {
            "command": command, "args": [], "env": env, "cwd": cwd,
            "message_mode": "none", "attach_offset": 0,
        }
        if engine == "omp":
            # V4: the spawn carried --append-system-prompt=<worker-bridge hint>. A resume that drops
            # it leaves the same session without the hint it was started with.
            hint_args: List[str] = []
            if str(snapshot.get("task_mode") or "") == "omp_positional" and not any(
                str(a).startswith("--append-system-prompt") for a in base_args
            ):
                hint_args = [
                    f"--append-system-prompt="
                    f"{self._build_worker_bridge_system_hint(str(record.get('session_id') or ''))}"
                ]
            built["args"] = [
                f"--session-dir={ref.get('session_dir')}",
                f"--resume={ref.get('session_file')}",
            ] + hint_args + base_args
            built["message_mode"] = "positional"
        elif engine == "kimi":
            # Research: --agent / --agent-file / --environment are rejected together with -S.
            cleaned = [a for a in base_args if a not in ("--agent", "--agent-file", "--environment")]
            built["args"] = ["-S", str(ref.get("session_id"))] + cleaned
            built["message_mode"] = "pty_input"
        elif engine == "agy":
            built["args"] = [f"--conversation={ref.get('conversation_id')}"] + base_args
            built["message_mode"] = "agy_interactive_flag"
        elif engine == "codex":
            built["args"] = ["resume", str(ref.get("session_id")), "-C", cwd] + base_args
            built["message_mode"] = "positional"
        elif engine == "dsh":
            built["args"] = []
            built["message_mode"] = "dsh_continue"
        else:
            raise ValueError(ENGINE_RESUME_BLOCK_REASONS.get(engine, DEFAULT_RESUME_BLOCK_REASON))
        return built

    @staticmethod
    def _resume_attach_offset(engine: str, ref: Dict[str, Any]) -> int:
        """Byte offset the resumed watcher starts at, so the old turns are not re-read (spec §4)."""
        path = ""
        if engine == "omp":
            path = str(ref.get("session_file") or "")
        elif engine == "kimi":
            sdir = str(ref.get("session_dir") or "")
            path = os.path.join(sdir, "agents", "main", "wire.jsonl") if sdir else ""
        elif engine == "agy":
            brain = str(ref.get("brain_dir") or "")
            path = os.path.join(brain, ".system_generated", "logs", "transcript.jsonl") if brain else ""
        elif engine == "codex":
            path = str(ref.get("rollout_path") or "")
        try:
            return os.path.getsize(path) if path else 0
        except OSError:
            return 0

    def _load_scrollback_text(self, session_id: str) -> str:
        blob = self.db.get_scrollback_blob(session_id)
        if blob:
            try:
                return zlib.decompress(blob).decode("utf-8", "replace")
            except Exception:
                pass
        # A row written before this feature has only the plain text tail; better than an empty pane.
        rec = self.db.get_session_record(session_id) or {}
        return str(rec.get("scrollback_text") or "")

    def _load_record_for_resume(self, session_id: str) -> Optional[Dict[str, Any]]:
        rec = self.db.get_session_record(session_id)
        sess = self.sessions.get(session_id)
        if sess is None:
            return rec
        live_rec = self._record_from_session(sess)
        if not rec:
            return live_rec
        for key in ("profile_snapshot", "conversation_ref", "last_task", "title", "kind", "cwd", "engine"):
            if not rec.get(key) and live_rec.get(key):
                rec[key] = live_rec[key]
        return rec

    def _mark_resumable(self, session_id: str, ok: bool, reason: str = "") -> None:
        sess = self.sessions.get(session_id)
        if sess is not None:
            sess.resumable = ok
            sess.resume_block_reason = None if ok else reason
        self.db.update_archive_fields(
            session_id,
            resumable=1 if ok else 0,
            resume_block_reason=None if ok else reason,
        )

    def _resume_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._resume_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._resume_locks[session_id] = lock
        return lock

    def _make_resumed_session(
        self,
        session_id: str,
        record: Dict[str, Any],
        snapshot: Dict[str, Any],
        ref: Dict[str, Any],
        built: Dict[str, Any],
    ) -> AgentSession:
        engine = str(snapshot.get("engine") or record.get("engine") or "").strip().lower()
        profile_id = str(snapshot.get("profile_id") or record.get("profile_id") or "")
        prof = self.profiles.get(profile_id) or {}
        count = int(record.get("resume_count") or 0) + 1
        sess = AgentSession(
            session_id=session_id,
            profile_id=profile_id,
            profile_name=str(record.get("profile_name") or prof.get("name") or profile_id),
            kind=str(snapshot.get("kind") or record.get("kind") or "tui"),
            title=str(record.get("title") or f"{prof.get('name', profile_id)} ({session_id})"),
            cwd=str(built.get("cwd") or snapshot.get("cwd") or record.get("cwd") or self.hub_default_cwd()),
            command=str(built.get("command") or snapshot.get("command") or ""),
            args=list(built.get("args") or []),
            gui_url=prof.get("gui_url"),
            on_exit_cb=self._on_session_exit,
            on_tui_tool_cb=self._on_tui_tool_detected,
        )
        sess.engine = engine
        sess.effective_model = snapshot.get("model") or record.get("effective_model")
        sess.effective_thinking_level = snapshot.get("thinking_level") or record.get("effective_thinking_level")
        sess.approval_mode = str(prof.get("approval_mode") or "auto")
        sess.last_task = str(record.get("last_task") or "")
        sess.created_at = float(record.get("created_at") or time.time())
        sess.profile_snapshot = dict(snapshot)
        sess.conversation_ref = dict(ref)
        sess.resume_attach = True
        sess.attach_offset = int(built.get("attach_offset") or 0)
        sess.resume_count = count
        sess.last_resumed_at = time.time()
        # D2: the ended turn's answer comes back with the revived object, and begin_new_turn marks it
        # as from_previous_turn at the resume call site. It is kept only until the revived turn
        # writes its own; set_final_response lets that first source win whatever its rank. The
        # archived pane still replays the scrollback, so the old answer is visible there too.
        sess.final_response = record.get("final_response") or None
        sess.final_response_source = record.get("final_response_source") or None
        # Spec §5.3: the pane starts with the archived output, then the resume separator.
        archived = self._load_scrollback_text(session_id)
        seed = f"{archived}{resume_banner(count)}" if archived else resume_banner(count)
        sess.raw_history = [seed]
        sess.raw_history_bytes = len(seed.encode("utf-8", "replace"))
        sess.raw_history_rev = 1
        if engine == "dsh":
            sess.dsh_spec = dict(snapshot.get("dsh_spec") or {})
            sess.dsh_job_id = ref.get("dsh_job_id")
            sess.dsh_session_id = ref.get("dsh_session_id")
        self._restore_parent_mailbox(session_id, record)
        return sess

    def _restore_parent_mailbox(self, session_id: str, record: Dict[str, Any]) -> None:
        """A11: both directions come back, or a second archive of the same record loses one side."""
        if session_id in self.parent_messages:
            return
        thread = record.get("parent_messages") or []
        restored = []
        worker_entries: List[Dict[str, Any]] = []
        for item in thread:
            if not isinstance(item, dict):
                continue
            if item.get("direction") == "parent":
                restored.append({
                    "id": item.get("message_id") or f"pmsg-{uuid.uuid4().hex[:8]}",
                    "session_id": session_id,
                    "timestamp": item.get("timestamp"),
                    "content": item.get("content", ""),
                    "resolved_question_id": item.get("resolved_question_id"),
                    "read_by_worker": True,
                })
            elif item.get("direction") == "worker":
                worker_entries.append({
                    "direction": "worker",
                    "timestamp": item.get("timestamp"),
                    "content": item.get("content", ""),
                    "type": item.get("type"),
                    "seq": item.get("seq"),
                })
        if restored:
            self.parent_messages[session_id] = restored
        if worker_entries:
            logs = getattr(self, "_worker_message_logs", None)
            if logs is None:
                logs = {}
                self._worker_message_logs = logs
            logs.setdefault(session_id, []).extend(worker_entries)

    async def resume_subagent(self, session_id: str, text: Optional[str] = None) -> Dict[str, Any]:
        """Revive an ended session under the same id (spec §5).

        Returns {"session", "resumed", "already_live", "delivered", "delivery", "injection"}:
        A2 needs the honest answer to "did this call really resume it, and did the text arrive",
        because a caller that lost the resume race must have its text delivered to the now-live
        session instead of being handed a canned `delivered: true`.
        Raises ValueError with the Chinese `resume_block_reason` when the record cannot be revived.
        """
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("找不到会话 ")
        lock = self._resume_lock(sid)
        async with lock:
            try:
                return await self._resume_locked(sid, text)
            except ValueError:
                raise
            except Exception as e:
                # A9: a spawn OSError / a DB error / a non-JSON DSH reply is a resume failure with
                # a Chinese 400 detail, never a 500.
                raise ValueError(f"{RESUME_FAILED_PREFIX}{e}") from e

    async def _resume_locked(self, sid: str, text: Optional[str]) -> Dict[str, Any]:
        existing = self.sessions.get(sid)
        if existing is not None and existing.is_process_alive():
            # A2: another resume (or the session itself) won the race. The text still has to be
            # delivered — through the ordinary parent→child path, not the resume injection.
            delivery = None
            if text and str(text).strip():
                delivery = self.deliver_parent_message(sid, str(text), also_write_pty=True)
            return {
                "session": existing,
                "resumed": False,
                "already_live": True,
                "delivered": bool(delivery and delivery.get("delivered")),
                "delivery": delivery,
                "injection": (delivery or {}).get("injection") or "none",
            }
        record = self._load_record_for_resume(sid)
        if record is None:
            raise ValueError(f"找不到会话 {sid}")
        engine = str(record.get("engine") or "").strip().lower()
        ok, reason = self.compute_resumable(record)
        if ok and engine == "dsh":
            job_id = str((record.get("conversation_ref") or {}).get("dsh_job_id") or "")
            if not await self._dsh_job_exists(job_id):
                ok, reason = False, DSH_UNKNOWN_JOB_MESSAGE
        if not ok:
            self._mark_resumable(sid, False, reason)
            raise ValueError(reason)
        if engine == "dsh":
            return await self._resume_dsh_session(sid, record, text)
        return await self._resume_process_session(sid, record, text)

    def _resume_task_text(self, session_id: str, text: str, engine: str) -> str:
        clean = str(text).replace("\r", " ").strip()
        if engine == "kimi":
            # Spec §4: the kimi task is typed into the TUI with the session-id marker.
            return f"{clean}\n\n[Sub-Agent Bridge Info: session_id={session_id}]"
        return clean

    async def _resume_process_session(
        self, session_id: str, record: Dict[str, Any], text: Optional[str]
    ) -> AgentSession:
        loop = asyncio.get_running_loop()
        snapshot = record.get("profile_snapshot") or {}
        ref = record.get("conversation_ref") or {}
        engine = str(snapshot.get("engine") or record.get("engine") or "").strip().lower()
        built = self.build_resume_launch(record)
        built["attach_offset"] = self._resume_attach_offset(engine, ref)
        message = str(text) if text is not None and str(text).strip() else None
        prompt_for_tui: Optional[str] = None
        # A10: how the text really travels. "argv" is handed over at spawn, "pending" is typed after
        # the TUI is ready and only then has an outcome (last_injection); pty_written is never faked.
        injection = "none"
        if message:
            if built["message_mode"] == "positional":
                built["args"].append(self._resume_task_text(session_id, message, engine))
                injection = "argv"
            elif built["message_mode"] == "agy_interactive_flag":
                built["args"].extend(["-i", self._resume_task_text(session_id, message, engine)])
                injection = "argv"
            elif built["message_mode"] == "pty_input":
                prompt_for_tui = self._resume_task_text(session_id, message, engine)
                injection = "pending"

        sess = self._make_resumed_session(session_id, record, snapshot, ref, built)
        startup_delay = 2.5
        try:
            startup_delay = float(snapshot.get("startup_delay_sec") or 2.5)
        except (TypeError, ValueError):
            startup_delay = 2.5
        submit_key = str(snapshot.get("submit_key") or "\r")

        old = self.sessions.get(session_id)
        if old is not None and old is not sess:
            # A1: browser sockets opened on the old object keep streaming — the queue set is handed
            # over by identity, and start_pty pushes the separator to them before any new output.
            sess.adopt_ws_frontends(old)
        # A7: install the new object before the spawn. A resume that dies immediately then reports
        # its own exit (the callback carries the object), never the old dead object's.
        self.sessions[session_id] = sess
        try:
            # A9: a failed spawn (bad command, missing binary) must leave the archived record exactly
            # as it was — no zombie row, and the old ended object back in the live list.
            sess.start_pty(
                loop,
                env_overrides=dict(built.get("env") or {}) or None,
                ws_prefix=resume_banner(sess.resume_count) if old is not None else None,
            )
        except Exception as e:
            # R9: a spawn that raised after the PTY had already started must not leave that process
            # behind while the old (archived) object is put back in the live list.
            if sess.pty is not None:
                try:
                    sess.pty.terminate(force=True)
                except Exception:
                    pass
            if old is not None:
                self.sessions[session_id] = old
            else:
                self.sessions.pop(session_id, None)
            raise ValueError(f"{RESUME_FAILED_PREFIX}{e}") from e
        if old is not None:
            self._release_transcripts(session_id)
        self.db.reset_session_after_resume(session_id)
        # D2: the same shared helper the live follow-up uses marks the archived answer as the
        # previous turn's; it stays readable until the resumed turn writes its own.
        self.begin_new_turn(sess)
        self.persist_session(sess)

        if engine == "codex":
            asyncio.create_task(self._watch_codex_rollout(sess))
        elif engine == "omp":
            session_dir = Path(str(ref.get("session_dir") or self.runtime_configs_dir / f"omp_sess_{session_id}"))
            asyncio.create_task(self._watch_omp_session(sess, session_dir))
        elif engine == "kimi":
            asyncio.create_task(self._watch_kimi_session(sess))
        elif engine == "agy":
            asyncio.create_task(self._watch_agy_transcript(sess))

        if sess.kind == "tui":
            asyncio.create_task(self._watch_tui_interactive_prompts(sess))
            asyncio.create_task(
                self._auto_handle_tui_startup_and_trust(
                    sess,
                    task_to_inject=prompt_for_tui,
                    startup_delay=startup_delay,
                    submit_key=submit_key,
                )
            )

        self.record_worker_event(
            session_id,
            {
                "type": "resumed",
                "category": "lifecycle",
                "status": "resumed",
                "content": f"会话已恢复（第 {sess.resume_count} 次）",
            },
        )
        delivery = {
            "delivered": bool(message),
            "session_id": session_id,
            "resumed": True,
            "resume_count": sess.resume_count,
            "pty_written": False,
            "injection": injection,
            "note": "文本将在 TUI 就绪后输入，结果见 last_injection" if injection == "pending" else "",
        }
        return {
            "session": sess,
            "resumed": True,
            "already_live": False,
            "delivered": bool(message),
            "delivery": delivery,
            "injection": injection,
        }

    async def _dsh_job_exists(self, job_id: str) -> bool:
        """A8: only a real job object counts as "DSH still knows this job" — never a bare ok."""
        if not job_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}", params={"wait": 0})
                if resp.status_code == 404:
                    return False
                data = resp.json()
        except Exception:
            return False
        if not resp.is_success or not isinstance(data, dict):
            return False
        job = data.get("job")
        return isinstance(job, dict) and bool(job.get("id"))

    @staticmethod
    def _dsh_continue_says_running(reason: str) -> bool:
        """R5: the /continue refusal that means "this job is running, steer it instead"."""
        text = str(reason or "").lower()
        return any(marker in text for marker in (
            "worker is running", "already running", "is running", "正在运行", "运行中",
        ))

    async def _dsh_post_message(
        self, job_id: str, token: str, text: str, timeout: float = 10.0
    ):
        """R5: posts text to a RUNNING dsh job through /messages (returns the response or None)."""
        clean = str(text or "").strip()
        if not job_id or not token or not clean:
            return None
        try:
            async with httpx.AsyncClient(timeout=max(1.0, float(timeout))) as client:
                return await client.post(
                    f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}/messages",
                    headers={"x-dsh-crew-job-token": token},
                    json={"message": clean},
                )
        except Exception:
            return None

    async def _resume_dsh_session(
        self, session_id: str, record: Dict[str, Any], text: Optional[str]
    ) -> Dict[str, Any]:
        """DSH has no process: continue the recorded job, or only re-attach the virtual terminal (§4)."""
        loop = asyncio.get_running_loop()
        snapshot = record.get("profile_snapshot") or {}
        ref = dict(record.get("conversation_ref") or {})
        job_id = str(ref.get("dsh_job_id") or "")
        built = self.build_resume_launch(record)
        # R5: the job's own messaging token is kept in the conversation reference so the
        # /messages fallback (and a later steering message) still works after a hub restart.
        old_token = str(ref.get("dsh_token") or "")
        token = str(uuid.uuid4())
        clean = str(text or "").strip()
        new_job = ""
        cjob: Dict[str, Any] = {}
        jstatus: Optional[str] = None
        dsh_send_mode = "attach"

        # Talk to DSH first: a failed continue/re-attach must not replace the archived record.
        if clean:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}/continue",
                        json={"text": clean, "messaging_token": token},
                    )
            except Exception as e:
                raise ValueError(f"DSH 恢复失败：{e}") from e
            try:
                data = resp.json()
            except Exception:
                raise ValueError("DSH 恢复失败：DSH 返回了非 JSON 内容") from None
            if not resp.is_success or not isinstance(data, dict) or not data.get("ok"):
                err = data.get("error") if isinstance(data, dict) else None
                err_text = str(err or f"HTTP {resp.status_code}")
                # R5: a running job refuses /continue ("worker is running"); a message reaches it.
                if old_token and self._dsh_continue_says_running(err_text):
                    fallback = await self._dsh_post_message(job_id, old_token, clean)
                    if fallback is None or not fallback.is_success:
                        raise ValueError(
                            f"DSH 恢复失败：{err_text}（改用 /messages 发送也失败）"
                        )
                    jstatus = "running"
                    new_job = job_id
                    dsh_send_mode = "message"
                else:
                    raise ValueError(f"DSH 恢复失败：{err_text}")
            else:
                cjob = data.get("job") or {}
                new_job = str(cjob.get("id") or "")
                if not new_job:
                    raise ValueError("DSH 恢复失败：continue 没有返回新的任务 ID")
                dsh_send_mode = "continue"
        else:
            # No text: nothing is sent, only the virtual terminal is re-attached (§4). A8: an
            # unreadable / failed / cancelled / unknown job fails the resume with a Chinese reason
            # instead of leaving a pseudo-live "starting" session behind.
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(
                        f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}", params={"wait": 0}
                    )
            except Exception as e:
                raise ValueError(DSH_STATE_UNAVAILABLE_MESSAGE.format(reason=e)) from e
            if not resp.is_success:
                raise ValueError(DSH_STATE_UNAVAILABLE_MESSAGE.format(reason=f"HTTP {resp.status_code}"))
            try:
                data = resp.json()
            except Exception:
                raise ValueError(DSH_STATE_UNAVAILABLE_MESSAGE.format(reason="DSH 返回了非 JSON 内容")) from None
            jview = (data.get("job") if isinstance(data, dict) else None) or {}
            if not isinstance(jview, dict) or not jview.get("id"):
                raise ValueError(DSH_STATE_UNAVAILABLE_MESSAGE.format(reason="DSH 返回的任务信息无法识别"))
            jstatus = str(jview.get("status") or "")
            # R3: running / needs_input / every ended status re-attach; only a missing or
            # unreadable status refuses (A8's message).
            if jstatus not in ("running", "needs_input") + DSH_ENDED_JOB_STATUSES:
                raise ValueError(
                    DSH_STATE_UNAVAILABLE_MESSAGE.format(reason=f"任务状态为 {jstatus or '未知'}")
                )

        sess = self._make_resumed_session(session_id, record, snapshot, ref, built)
        old = self.sessions.get(session_id)
        # A1: a dsh pane's open socket survives the object replacement too, and typed input has to
        # reach the virtual terminal — which needs the loop and the parent-delivery callback.
        sess._loop = loop
        if sess.on_virtual_line_cb is None:
            sess.on_virtual_line_cb = lambda sid, line: self.deliver_parent_message(
                sid, line, also_write_pty=False
            )
        if old is not None and old is not sess:
            sess.adopt_ws_frontends(old)
            sess.push_ws_frame(resume_banner(sess.resume_count))
        self.sessions[session_id] = sess
        if old is not None:
            self._release_transcripts(session_id)
        self.db.reset_session_after_resume(session_id)
        # D2: same shared helper as every other new turn (resume keeps the archived answer).
        self.begin_new_turn(sess)
        if clean:
            # R5: a /messages delivery keeps the job and its original token; a /continue delivery
            # gets the new job and the new token. Both are persisted with the conversation ref.
            active_job = new_job
            active_token = token if dsh_send_mode == "continue" else old_token
            sess.dsh_job_id = active_job
            sess.dsh_token = active_token
            sess.conversation_ref = {
                "dsh_job_id": active_job,
                "dsh_session_id": sess.dsh_session_id or cjob.get("sessionId") or "",
                "dsh_token": active_token,
            }
            sess.status = "running"
            if dsh_send_mode == "message":
                sess.current_activity = f"Messaged running DSH job ({active_job}): {clean[:60]}"
                sess.emit_virtual_ansi(
                    "\r\n\x1b[1;36m➤ [Parent Steer Injected into running DSH job " + str(active_job) + "]: " + str(clean[:120]) + "\x1b[0m\r\n"
                )
            else:
                sess.current_activity = f"Continuing DSH session ({active_job}): {clean[:60]}"
        elif jstatus in ("running", "needs_input"):
            sess.dsh_token = old_token or token
            sess.status = "running"
            sess.current_activity = f"Re-attached DSH job {job_id}"
            if old_token:
                sess.conversation_ref = dict(sess.conversation_ref or {}, dsh_token=old_token)
        elif jstatus in DSH_ENDED_JOB_STATUSES:
            # R3: ended-but-live. The next message goes through /continue, exactly like the
            # send_to_subagent path that always accepted a done/failed/cancelled job.
            sess.dsh_token = old_token or ""
            sess.status = "completed"
            sess.current_activity = f"Re-attached ended DSH job {job_id} ({jstatus})"
            if old_token:
                sess.conversation_ref = dict(sess.conversation_ref or {}, dsh_token=old_token)
        self.persist_session(sess)
        self.record_worker_event(
            session_id,
            {
                "type": "resumed",
                "category": "lifecycle",
                "status": "resumed",
                "content": f"会话已恢复（第 {sess.resume_count} 次）",
            },
        )
        if clean:
            loop.create_task(self._watch_dsh_hub_job(
                sess, new_job, sess.dsh_token or token
            ))
        elif jstatus in ("running", "needs_input"):
            loop.create_task(self._watch_dsh_hub_job(sess, job_id, sess.dsh_token or token))
        injection = "dsh_continue" if clean else "dsh_attach"
        delivery = {
            "delivered": bool(clean),
            "session_id": session_id,
            "resumed": True,
            "resume_count": sess.resume_count,
            "pty_written": False,
            "injection": injection,
            "note": "" if clean else "仅重新挂接虚拟终端，没有发送文本",
        }
        return {
            "session": sess,
            "resumed": True,
            "already_live": False,
            "delivered": bool(clean),
            "delivery": delivery,
            "injection": injection,
        }

    def list_archive(self, limit: int = 50, offset: int = 0, query: str = "") -> Dict[str, Any]:
        """The console's and the parent agent's archive view (spec §6).

        W1/R8: the page is refreshed from the same complete rows the single-record path uses, and
        all changed rows are written in one transaction instead of one commit per row.
        """
        rows = self.db.list_archived(limit=limit, offset=offset, query=query)
        changed: List[tuple] = []
        for rec in rows:
            if not self._recheck_resumable(rec):
                continue
            changed.append((
                str(rec.get("session_id") or ""),
                {
                    "resumable": 1 if rec.get("resumable") else 0,
                    "resume_block_reason": rec.get("resume_block_reason") or None,
                },
            ))
        try:
            self.db.update_archive_fields_bulk(changed)
        except Exception:
            # A failed refresh write must not take the archive page down; the next read retries.
            pass
        return {
            "sessions": [self._archive_summary(rec) for rec in rows],
            "total": self.db.count_archived(query),
            "limit": max(1, min(int(limit or 50), 200)),
            "offset": max(0, int(offset or 0)),
        }

    def _recheck_resumable(self, rec: Dict[str, Any]) -> bool:
        """A5/W1: re-checks the cheap filesystem facts and updates the record in place.

        Returns True when the stored verdict must be written. A record that never selected the
        snapshot and the conversation reference (a partial list row, or an older caller) is left
        exactly as stored: a "not resumable" verdict computed from missing columns is a lie that
        W1 showed will be persisted for every row.
        """
        if "profile_snapshot" not in rec or "conversation_ref" not in rec:
            return False
        end_reason = str(rec.get("end_reason") or "")
        if not rec.get("archived_at") and not end_reason:
            return False
        ok, reason = self.compute_resumable(rec)
        stored_ok = bool(rec.get("resumable"))
        stored_reason = str(rec.get("resume_block_reason") or "")
        rec["resumable"] = ok
        rec["resume_block_reason"] = None if ok else reason
        return stored_ok != ok or stored_reason != ("" if ok else reason)

    def _refresh_resumable(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """A5: the single-record archive endpoint re-checks and persists a change."""
        session_id = str(rec.get("session_id") or "")
        if self._recheck_resumable(rec):
            try:
                self.db.update_archive_fields(
                    session_id,
                    resumable=1 if rec.get("resumable") else 0,
                    resume_block_reason=rec.get("resume_block_reason") or None,
                )
            except Exception:
                pass
        return rec

    @staticmethod
    def _decoded_conversation_ref(rec: Dict[str, Any]) -> Dict[str, Any]:
        """V5: the archive API returns the engine conversation reference the record really holds."""
        ref = rec.get("conversation_ref")
        if isinstance(ref, str):
            try:
                ref = json.loads(ref) if ref else None
            except Exception:
                ref = None
        return ref if isinstance(ref, dict) else {}

    def _archive_summary(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        model = " · ".join([x for x in (rec.get("effective_model"), rec.get("effective_thinking_level")) if x])
        status = rec.get("status")
        end_reason = rec.get("end_reason")
        # V5: an archived record must never report a live status; its end state is what happened.
        if (rec.get("archived_at") or end_reason) and str(status or "") in LIVE_STATUSES:
            status = end_reason or status
        return {
            "session_id": rec.get("session_id"),
            "profile_id": rec.get("profile_id"),
            "profile_name": rec.get("profile_name"),
            "engine": rec.get("engine"),
            "kind": rec.get("kind"),
            "title": rec.get("title"),
            "cwd": rec.get("cwd"),
            "last_task": rec.get("last_task") or "",
            "task_first_line": self._first_line_text(rec.get("last_task")),
            "effective_model": rec.get("effective_model"),
            "effective_thinking_level": rec.get("effective_thinking_level"),
            "model_line": model,
            "status": status,
            "end_reason": end_reason,
            "end_reason_label": END_REASON_LABELS.get(str(end_reason or ""), ""),
            "ended_at": rec.get("ended_at"),
            "archived_at": rec.get("archived_at"),
            "created_at": rec.get("created_at"),
            "exit_code": rec.get("exit_code"),
            "step_count": rec.get("step_count") or 0,
            "resume_count": rec.get("resume_count") or 0,
            "last_resumed_at": rec.get("last_resumed_at"),
            "conversation_ref": self._decoded_conversation_ref(rec),
            "resumable": bool(rec.get("resumable")),
            "resume_block_reason": rec.get("resume_block_reason") or "",
            "archived": bool(rec.get("archived_at")),
            "has_final_response": bool(rec.get("has_final_response") or rec.get("final_response")),
        }

    def get_archive_record(self, session_id: str) -> Optional[Dict[str, Any]]:
        rec = self.db.get_session_record(session_id)
        if not rec:
            return None
        return self._archive_summary(self._refresh_resumable(rec))

    def get_archive_scrollback(self, session_id: str) -> Optional[str]:
        rec = self.db.get_session_record(session_id)
        if not rec:
            return None
        return self._load_scrollback_text(session_id)

    def delete_archive_record(self, session_id: str) -> bool:
        """Deletes the hub's archived row only; the engine's own conversation files stay (§6)."""
        rec = self.db.get_session_record(session_id)
        if not rec:
            raise ValueError(f"找不到会话 {session_id}")
        if session_id in self.sessions:
            raise ValueError("会话仍在列表中，请先移除再删除归档记录")
        if not rec.get("archived_at"):
            raise ValueError("会话仍在列表中，请先移除再删除归档记录")
        return self.db.delete_session_record(session_id)

    def _scrollback_flush_loop(self) -> None:
        """Write each live session's scrollback once it is 60 s old (spec §1.6/§3).

        The tick is short and the 60 s throttle lives in _scrollback_blob, so the first write lands
        a few seconds after the session's first minute instead of drifting one further minute.
        """
        while True:
            time.sleep(5.0)
            try:
                now = time.time()
                for sess in list(self.sessions.values()):
                    if not sess.is_process_alive():
                        continue
                    if now - sess._scrollback_written_at < RESUME_SCROLLBACK_INTERVAL_SEC:
                        continue
                    self.persist_session(sess)
            except Exception:
                pass

    def _normalize_archive_on_startup(self) -> None:
        """Spec §1.5/§5: a stopped hub's live rows become interrupted + archived at the next start."""
        now = time.time()
        status_to_reason = {
            "exited": "exited", "crashed": "crashed", "terminated": "terminated",
            "completed": "exited", "interrupted": "interrupted",
        }
        for sid in self.db.list_session_ids():
            rec = self.db.get_session_record(sid)
            if not rec:
                continue
            if rec.get("archived_at") and rec.get("resumable") is not None:
                continue
            fields: Dict[str, Any] = {}
            status = str(rec.get("status") or "")
            if not rec.get("archived_at"):
                fields["archived_at"] = now
                fields["ended_at"] = rec.get("ended_at") or now
                if status in LIVE_STATUSES:
                    fields["status"] = "interrupted"
                    fields["end_reason"] = "interrupted"
                    fields["current_activity"] = "Hub 停止时会话仍在运行（已中断）"
                else:
                    fields["end_reason"] = rec.get("end_reason") or status_to_reason.get(status, "crashed")
                if not rec.get("last_screen"):
                    lines = [ln for ln in str(rec.get("scrollback_text") or "").splitlines() if ln.strip()]
                    fields["last_screen"] = "\n".join(lines[-20:])
            merged = dict(rec)
            merged.update(fields)
            ok, reason = self.compute_resumable(merged)
            fields["resumable"] = 1 if ok else 0
            fields["resume_block_reason"] = None if ok else reason
            self.db.update_archive_fields(sid, **fields)

    def close_session(self, session_id: str, source: str = "console") -> Dict[str, Any]:
        """B9: removes a session from the live list without rewriting the status an ended process already earned.

        A session whose process was alive is terminated and emits a `terminated` worker event; one
        that had already ended keeps its own final status and activity. Either way the session leaves
        the live list as an archived record (spec §5).
        K5: `source` says who did it -- "console" (the 总控台 pane) or "mcp" (the parent agent through
        terminate_subagent), because the event text used to claim the user did it even when the parent
        had. Any other value is treated as the console, the only other caller there is.
        Returns {"found": bool, "was_alive": bool, "status": str}.
        """
        sess = self.sessions.get(session_id)
        if not sess:
            return {"found": False, "was_alive": False, "status": None}
        was_alive = sess.is_process_alive()
        terminated = sess.terminate()
        if terminated:
            sess.end_reason = "terminated"
        self._archive_session(sess)
        del self.sessions[session_id]
        # W7: the pane the console is about to remove still owns a socket; end it normally instead
        # of leaving the server side open and silent for the rest of the hub's life.
        sess.close_ws_frontends()
        if terminated:
            self._user_terminated.add(session_id)
        self._release_transcripts(session_id)
        if terminated or (was_alive and sess.status == "terminated"):
            self.record_worker_event(
                session_id,
                {
                    "type": "terminated",
                    "category": "lifecycle",
                    "status": "terminated",
                    "content": TERMINATED_MESSAGES.get(
                        "mcp" if str(source or "").lower() == "mcp" else "console",
                        TERMINATED_MESSAGES["console"],
                    ),
                },
            )
        return {"found": True, "was_alive": bool(was_alive), "status": sess.status}

    @staticmethod
    def _cancel_dsh_job(job_id: str, timeout: float = SHUTDOWN_JOB_CANCEL_TIMEOUT_SEC) -> bool:
        """R5: stops a live DSH job at hub shutdown so a restart can /continue it, not meet /cancel."""
        if not job_id:
            return False
        try:
            resp = httpx.post(
                f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}/cancel",
                timeout=max(0.2, float(timeout)),
            )
            return bool(resp.is_success)
        except Exception:
            return False

    @staticmethod
    def _terminate_pty_tree(sess: AgentSession, budget: float = 2.0) -> None:
        """R6: kills a live PTY's process tree (not just the root) within a bounded time.

        A Windows PTY's children (MCP servers, shells) survive a plain terminate of the root; a
        surviving kimi keeps its per-session lock, which makes the archived row refuse to resume.
        The taskkill is only used for a real winpty process, never in tests with a fake PID.
        """
        pty = sess.pty
        if pty is None:
            return
        pid = getattr(pty, "pid", None)
        if pid and isinstance(pty, REAL_PTY_PROCESS):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=max(0.2, min(2.0, float(budget))),
                )
            except Exception:
                pass
        try:
            pty.terminate(force=True)
        except Exception:
            pass

    def archive_all_sessions_on_shutdown(self) -> int:
        """A6/R5/R6/W7: a graceful hub stop archives every in-memory session, flushing scrollback + screen.

        A live one becomes interrupted (spec §1.5) and is resumable afterwards; an already-ended one
        keeps its own reason. R6 terminates a live PTY's process tree, R5 cancels a live DSH job, and
        W7 closes the browser sockets. Each session is best effort and the whole loop is bounded.
        """
        archived = 0
        deadline = time.time() + SHUTDOWN_MAX_SEC
        for session_id, sess in list(self.sessions.items()):
            try:
                if sess.is_process_alive():
                    end_reason = "interrupted"
                    sess.status = "interrupted"
                    sess.current_activity = "Hub 停止时会话仍在运行（已中断）"
                    remaining = min(2.0, deadline - time.time())
                    if remaining > 0:
                        if sess.dsh_job_id:
                            self._cancel_dsh_job(str(sess.dsh_job_id), timeout=remaining)
                        elif sess.pty is not None:
                            self._terminate_pty_tree(sess, budget=remaining)
                else:
                    end_reason = sess.end_reason or sess.classify_end_reason(sess.exit_code)
                self._archive_session(sess, end_reason=end_reason)
                sess.close_ws_frontends()
                archived += 1
            except Exception:
                continue
        self.sessions.clear()
        with self._transcript_lock:
            self._claimed_transcripts.clear()
        return archived

    def record_telemetry(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Processes telemetry from Claude Hooks, Codex JSONL Tailer, Worker MCP `report_step`, or TUI Heuristic Parser."""
        sess = self.sessions.get(session_id)
        if sess and data.get("transcript_path"):
            sess.transcript_path = Path(str(data["transcript_path"]))
        hook_event = data.get("hook_event", "")
        source = data.get("source", "telemetry")
        tool_name = data.get("tool_name")
        tool_input = data.get("tool_input")
        tool_response = data.get("tool_response")
        step_title = data.get("step_title")
        intermediate_list = data.get("intermediate_messages") or []
        final_text = data.get("final_assistant_text")

        input_str = ""
        if isinstance(tool_input, (dict, list)):
            input_str = json.dumps(tool_input, ensure_ascii=False)[:600]
        elif tool_input is not None:
            input_str = str(tool_input)[:600]

        resp_str = ""
        if isinstance(tool_response, (dict, list)):
            resp_str = json.dumps(tool_response, ensure_ascii=False)[:600]
        elif tool_response is not None:
            resp_str = str(tool_response)[:600]

        step_num = sess.step_count if sess else 0

        if hook_event in ("PreToolUse", "ToolCall", "ReportStep", "TuiToolDetected"):
            if sess:
                sess.step_count += 1
                step_num = sess.step_count
                if step_title:
                    sess.current_activity = f"Step {step_num}: {step_title}"
                elif tool_name:
                    sess.current_activity = f"Step {step_num}: Executing tool [{tool_name}] {input_str[:80]}"
            if tool_name:
                self.db.add_tool_call(
                    session_id=session_id,
                    step_num=step_num,
                    tool_name=tool_name,
                    input_summary=input_str or (step_title or ""),
                    status="completed" if hook_event != "PreToolUse" else "running",
                    source=source,
                )

        elif hook_event == "PostToolUse":
            self.db.complete_latest_tool_call(session_id, tool_name, resp_str)
            if sess and tool_name:
                sess.current_activity = f"Step {sess.step_count}: Completed tool [{tool_name}]"

        for msg in intermediate_list:
            if msg and str(msg).strip():
                self.db.add_intermediate_message(session_id, step_num, str(msg).strip(), source=source)

        if final_text and str(final_text).strip():
            f_source = "native_transcript"
            if sess:
                sess.set_final_response(str(final_text).strip(), f_source)
                if hook_event in ("Stop", "SubagentStop", "TurnComplete"):
                    sess.current_activity = f"Step {sess.step_count}: Turn completed (final response captured)"

        if hook_event in ("Stop", "SubagentStop", "TurnComplete") and final_text:
            self.record_worker_event(
                session_id,
                {
                    "type": "turn_completed",
                    "category": "turn_completed",
                    "status": "turn_idle",
                    "content": str(final_text).strip(),
                },
            )

        if sess:
            self.persist_session(sess)

        return {"ok": True, "session_id": session_id, "step_count": step_num}

    def _on_tui_tool_detected(self, session_id: str, tool_name: str, tool_arg: str):
        self.record_telemetry(
            session_id,
            {
                "source": "tui_screen_parser",
                "hook_event": "TuiToolDetected",
                "tool_name": tool_name,
                "tool_input": tool_arg,
            },
        )

    def record_worker_event(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Records an event emitted by a Sub-Agent (message, question, completed, turn_completed, exited) and wakes Parent pollers."""
        sess = self.sessions.get(session_id)
        ev_type = payload.get("type", "message")
        content = (payload.get("content") or "").strip()
        if ev_type == "completed" and is_placeholder_summary(content):
            # F1: the hint's own placeholder (or the old literal "SUMMARY") is not a result. The
            # completion itself still counts -- status and event below -- but an empty summary must
            # never become final_response, an intermediate message, or a stored event body.
            content = ""

        if sess:
            if ev_type == "completed":
                if sess.step_count == 0:
                    sess.step_count = 1
                    self.db.add_tool_call(
                        session_id=session_id,
                        step_num=1,
                        tool_name="report_complete",
                        input_summary=content[:200],
                        status="completed",
                        source="worker_bridge",
                    )
                if content:
                    self.db.add_intermediate_message(session_id, sess.step_count, content, source="report_complete_mcp")
                sess.status = "completed"
                sess.set_final_response(content, "report_complete_mcp")
                sess.files_changed = payload.get("files_changed") or sess.files_changed
                sess.current_activity = f"Task completed ({payload.get('status', 'success')})"
            elif ev_type in ("question", "approval_request"):
                sess.status = "waiting_parent"
                prefix = "Waiting for parent approval" if ev_type == "approval_request" else "Waiting for parent answer"
                sess.current_activity = f"{prefix}: {content[:80]}"
            elif ev_type == "message" and content:
                self.db.add_intermediate_message(session_id, sess.step_count, content, source="message_parent_mcp")
            if ev_type == "turn_completed" and content:
                # K4: every engine's turn_completed carries the end-of-turn answer (agy "OK", kimi
                # "PELICAN-7", codex/omp task_complete, dsh's result). It is this turn's final
                # response unless a better source already wrote one: set_final_response keeps the
                # higher-priority source, so report_complete / codex_task_complete are never lost.
                sess.set_final_response(content, "turn_completed")
            if ev_type in ("completed", "turn_completed"):
                # Spec §1.4: the agent reported completion, so a later exit is a clean one.
                sess.completion_reported = True

        record = {
            "session_id": session_id,
            "timestamp": round(time.time(), 3),
            "type": ev_type,
            "category": payload.get("category", ev_type),
            "content": content,
            "status": payload.get("status", "info"),
            "files_changed": payload.get("files_changed", []),
            "request_id": payload.get("request_id"),
            "interaction": payload.get("interaction"),
        }
        seq = self.db.insert_worker_event(record)
        self.global_seq = max(self.global_seq, seq)
        record["seq"] = seq

        self.worker_events.append(record)
        if len(self.worker_events) > 2000:
            self.worker_events = self.worker_events[-2000:]
        # A11: the archive thread's worker side must outlive the ring.
        self._remember_worker_message(session_id, record)

        if sess:
            self.persist_session(sess)

        self._notify_waiters()
        return record

    def summarize_transcript(self, session_id: str) -> Dict[str, Any]:
        """
        Produces a structured, CLI-specific transcript summary (for omp, kimi, codex, agy, dsh, claude)
        so the Parent Agent never needs to read noisy raw TUI screen dumps.
        """
        sess = self.sessions.get(session_id)
        raw_fallback = ""
        if sess:
            meta = sess.to_dict()
            t_path = sess.transcript_path
            raw_fallback = sess.get_clean_screen_text(max_lines=30)
        else:
            persisted = self.db.get_session_record(session_id)
            if not persisted:
                raise ValueError(f"Session '{session_id}' not found in live memory or SQLite history.")
            raw_fallback = persisted.pop("scrollback_text", "")
            meta = persisted
            tp_str = meta.get("transcript_path") or ""
            t_path = Path(tp_str) if tp_str else None

        tools = self.db.get_recent_tool_calls(session_id, limit=20)
        inter_msgs = self.db.get_recent_intermediate_messages(session_id, limit=15)

        summary = self.summarizer_registry.summarize_session(
            profile_id=meta.get("profile_id", "generic"),
            transcript_path=t_path,
            session_meta=meta,
            db_tools=tools,
            db_messages=inter_msgs,
            raw_screen_fallback=raw_fallback,
        )

        # Upgrade session's final_response if transcript summarizer found a clean conclusion.
        # D2: never while the stored answer is still the previous turn's. The summariser reads
        # whatever transcript exists, which right after a follow-up or a resume is still the ended
        # turn's; letting it "overwrite" would both lose the kept answer and clear from_previous_turn
        # before the new turn produced anything. The new turn's own sources (turn_completed, the
        # native transcript, report_complete) still replace the kept answer through set_final_response.
        if sess and summary.get("latest_conclusion") and not sess.final_response_from_previous_turn:
            sess.set_final_response(summary["latest_conclusion"], "transcript_summarizer")
            self.persist_session(sess)

        return summary

    def get_subagent_status_and_trace(
        self,
        session_id: str,
        recent_tools_limit: int = 15,
        recent_messages_limit: int = 10,
    ) -> Dict[str, Any]:
        """Queries full live OR post-lifecycle telemetry, tool execution trace, intermediate messages, and CLI-specific transcript summary."""
        sess = self.sessions.get(session_id)
        if sess:
            self.persist_session(sess)
            meta = sess.to_dict()
        else:
            persisted = self.db.get_session_record(session_id)
            if not persisted:
                raise ValueError(f"Session '{session_id}' not found in live memory or SQLite history.")
            persisted.pop("scrollback_text", "")
            meta = persisted

        tools = self.db.get_recent_tool_calls(session_id, limit=recent_tools_limit)
        inter_msgs = self.db.get_recent_intermediate_messages(session_id, limit=recent_messages_limit)
        transcript_summary = self.summarize_transcript(session_id)

        final_resp = meta.get("final_response")
        final_src = meta.get("final_response_source")
        if (not final_resp or final_src == "terminal_turn_snapshot") and transcript_summary.get("latest_conclusion"):
            final_resp = transcript_summary["latest_conclusion"]
            final_src = "transcript_summarizer"

        return {
            "session_id": session_id,
            "is_alive_in_memory": sess is not None and sess.status in ("starting", "running", "waiting_parent"),
            "lifecycle_status": meta.get("status"),
            "profile_id": meta.get("profile_id"),
            "engine": meta.get("engine"),
            "approval_mode": meta.get("approval_mode"),
            "title": meta.get("title"),
            "cwd": meta.get("cwd"),
            "step_count": max(meta.get("step_count", 0), transcript_summary.get("step_count", 0), len(tools)),
            "current_activity": meta.get("current_activity", ""),
            "last_task": meta.get("last_task", ""),
            "pending_question_id": meta.get("pending_question_id"),
            "pending_interaction": sess.pending_interaction if sess else meta.get("pending_interaction"),
            "recent_tool_calls": tools,
            "intermediate_messages": inter_msgs,
            "final_response": final_resp,
            "final_response_source": final_src,
            # D2: the answer is still the previous turn's while the flag says so.
            "from_previous_turn": bool(meta.get("from_previous_turn", False)),
            "files_changed": meta.get("files_changed", []),
            # A10: the real outcome of the startup/resume text, when there was one.
            "last_injection": meta.get("last_injection"),
            "transcript_summary": transcript_summary,
        }

    def get_subagent_final_response(self, session_id: str) -> Dict[str, Any]:
        """Retrieves the Sub-Agent's clean final response and transcript summary both during and after its lifecycle."""
        transcript_summary = self.summarize_transcript(session_id)
        sess = self.sessions.get(session_id)
        if sess:
            if not sess.final_response and transcript_summary.get("latest_conclusion"):
                sess.set_final_response(transcript_summary["latest_conclusion"], "transcript_summarizer")
            self.persist_session(sess)

        rec = self.db.get_session_record(session_id)
        if not rec:
            raise ValueError(f"Session '{session_id}' not found in live memory or SQLite history.")

        inter_msgs = self.db.get_recent_intermediate_messages(session_id, limit=5)
        latest_inter = inter_msgs[-1]["content"] if inter_msgs else None

        final_resp = rec.get("final_response")
        final_src = rec.get("final_response_source")
        if (not final_resp or final_src == "terminal_turn_snapshot") and transcript_summary.get("latest_conclusion"):
            final_resp = transcript_summary["latest_conclusion"]
            final_src = "transcript_summarizer"
        elif not final_resp:
            final_resp = latest_inter or transcript_summary.get("digest_markdown", "")
            final_src = "intermediate_message" if latest_inter else "transcript_summary"

        return {
            "session_id": session_id,
            "profile_id": rec.get("profile_id"),
            "lifecycle_status": rec.get("status"),
            "step_count": max(rec.get("step_count", 0), transcript_summary.get("step_count", 0)),
            "created_at": rec.get("created_at"),
            "ended_at": rec.get("ended_at"),
            "exit_code": rec.get("exit_code"),
            "final_response": final_resp,
            "final_response_source": final_src,
            # D2: a live session knows the answer above is the previous turn's; an archived record
            # has no live flag and shows the stored value exactly as before.
            "from_previous_turn": bool(
                sess is not None and getattr(sess, "final_response_from_previous_turn", False)
            ),
            "files_changed": rec.get("files_changed", []),
            "transcript_summary": transcript_summary,
        }

    async def ask_parent_and_wait(self, session_id: str, question: str, timeout_seconds: float = 300.0) -> Dict[str, Any]:
        req_id = f"ask-{uuid.uuid4().hex[:8]}"
        wait_ev = asyncio.Event()
        q_entry = {
            "request_id": req_id,
            "session_id": session_id,
            "question": question,
            "asked_at": time.time(),
            "answered": False,
            "answer": None,
            "event": wait_ev,
        }
        self.pending_questions[req_id] = q_entry

        sess = self.sessions.get(session_id)
        if sess:
            sess.pending_question_id = req_id

        event_rec = self.record_worker_event(
            session_id,
            {
                "type": "question",
                "category": "question_waiting_reply",
                "content": question,
                "request_id": req_id,
            },
        )

        try:
            await asyncio.wait_for(wait_ev.wait(), timeout=timeout_seconds)
            return {
                "answered": True,
                "request_id": req_id,
                "seq": event_rec["seq"],
                "answer": q_entry["answer"],
            }
        except asyncio.TimeoutError:
            return {
                "answered": False,
                "request_id": req_id,
                "seq": event_rec["seq"],
                "error": f"等待主 Agent 回复超时 ({timeout_seconds}s)。可调用 poll_parent_instructions 查看后续回复。",
            }
        finally:
            if sess and sess.pending_question_id == req_id:
                sess.pending_question_id = None
                if sess.status == "waiting_parent":
                    sess.status = "running"

    def register_pending_interaction(
        self,
        session: AgentSession,
        kind: str,
        title: str,
        prompt: str,
        options: List[str],
        source: str,
        request_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Registers a structured high-risk approval request (`kind="approval_request"`) or interactive
        multi-choice question (`kind="question"`) detected from Transcript JSONL or TUI screen,
        transitions the session to `waiting_parent`, and emits a sequenced event to wake the Parent Agent.
        """
        req_id = request_id or f"int-{uuid.uuid4().hex[:8]}"
        if session.pending_interaction and session.pending_interaction.get("request_id") == req_id:
            return session.pending_interaction

        interaction: Dict[str, Any] = {
            "request_id": req_id,
            "session_id": session.session_id,
            "engine": session.engine or self._infer_engine(session.profile_id, {}),
            "kind": kind,  # "approval_request" or "question"
            "title": title,
            "prompt": prompt,
            "options": list(options),
            "source": source,
            "created_at": round(time.time(), 3),
            "metadata": dict(metadata or {}),
        }
        session.pending_interaction = interaction
        session.pending_question_id = req_id
        session.status = "waiting_parent"

        if req_id not in self.pending_questions:
            self.pending_questions[req_id] = {
                "request_id": req_id,
                "session_id": session.session_id,
                "question": prompt,
                "asked_at": time.time(),
                "answered": False,
                "answer": None,
                "event": asyncio.Event(),
                "interaction": interaction,
                **(metadata or {}),
            }
        else:
            self.pending_questions[req_id]["interaction"] = interaction

        opts_lines = "\n".join(f"  [{i + 1}] {opt}" for i, opt in enumerate(options)) if options else "  (自由文本回复)"
        ev_type = "approval_request" if kind == "approval_request" else "question"
        formatted_content = (
            f"【{'⚠️ 高风险操作审批请求 (Approval Request)' if kind == 'approval_request' else '❓ Sub-Agent 交互提问 / 选项征询 (Interactive Question)'}】\n"
            f"- 来源引擎: `{interaction['engine']}` (`{source}`) | request_id=`{req_id}`\n"
            f"- 标题: {title}\n"
            f"- 详情: {prompt}\n"
            f"- 可选操作/选项:\n{opts_lines}\n"
            f"👉 主 Agent 请调用 `respond_to_subagent_approval_or_choice(session_id='{session.session_id}', decision='approve'|'approve_session'|'deny', option_index=1..N, custom_input='...')` 代批或代选。"
        )
        self.record_worker_event(
            session.session_id,
            {
                "type": ev_type,
                "category": "approval_waiting_reply" if kind == "approval_request" else "question_waiting_reply",
                "status": "waiting_parent",
                "request_id": req_id,
                "content": formatted_content,
                "interaction": interaction,
            },
        )
        return interaction

    def clear_pending_interaction(self, session: AgentSession, request_id: Optional[str] = None):
        """Clears an active pending interaction when resolved via Parent response or native Transcript event."""
        if not session.pending_interaction:
            return
        if request_id and session.pending_interaction.get("request_id") != request_id:
            return
        old_id = session.pending_interaction.get("request_id")
        session.pending_interaction = None
        if session.pending_question_id == old_id:
            session.pending_question_id = None
        if old_id and old_id in self.pending_questions:
            self.pending_questions[old_id]["answered"] = True
            self.pending_questions[old_id]["event"].set()
        if session.status == "waiting_parent":
            session.status = "running"
        self.persist_session(session)

    def respond_to_interaction(
        self,
        session_id: str,
        decision: Optional[str] = None,
        option_index: Optional[int] = None,
        custom_input: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes the Parent Agent's approval decision (`approve`, `approve_session`, `deny`) or
        multi-choice selection (`option_index` 1-based, or `custom_input`) using the exact protocol
        verified for each CLI engine (`dsh`, `kimi`, `omp`, `codex`, `agy`).
        """
        sess = self.sessions.get(session_id)
        if not sess:
            raise ValueError(f"Active session '{session_id}' not found.")

        eng = (sess.engine or self._infer_engine(sess.profile_id, {})).lower()
        inter = sess.pending_interaction or {}
        target_qid = request_id or inter.get("request_id") or sess.pending_question_id
        q_entry = self.pending_questions.get(target_qid) if target_qid else None
        if not inter and q_entry and q_entry.get("interaction"):
            inter = q_entry["interaction"]

        kind = inter.get("kind") or "approval_request"
        options: List[str] = list(inter.get("options") or [])
        meta: Dict[str, Any] = dict(inter.get("metadata") or (q_entry or {}))

        dec_norm = (decision or "").strip().lower()
        chosen_idx_1based: Optional[int] = option_index
        chosen_label: str = ""

        # Map semantic decision or custom_input matching onto 1-based option_index
        if chosen_idx_1based is None:
            if dec_norm in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
                chosen_idx_1based = int(dec_norm)
            elif custom_input and options:
                ci_clean = custom_input.strip().lower()
                for idx, opt_txt in enumerate(options):
                    if ci_clean == opt_txt.strip().lower() or ci_clean in opt_txt.strip().lower():
                        chosen_idx_1based = idx + 1
                        break

        if chosen_idx_1based is None and dec_norm:
            if dec_norm in ("approve", "allow", "allow_once", "yes", "y", "ok", "accept"):
                chosen_idx_1based = 1
            elif dec_norm in ("approve_session", "allow_session", "always", "a", "session"):
                chosen_idx_1based = 2 if len(options) >= 2 else 1
            elif dec_norm in ("deny", "reject", "no", "n", "cancel"):
                # Find option containing Reject / Deny / No, otherwise 3 or last
                deny_idx = None
                for idx, opt_txt in enumerate(options):
                    if any(w in opt_txt.lower() for w in ("reject", "deny", "no,", "no ")):
                        deny_idx = idx + 1
                        break
                chosen_idx_1based = deny_idx or (3 if len(options) >= 3 else max(1, len(options)))

        if chosen_idx_1based is not None and 1 <= chosen_idx_1based <= len(options):
            chosen_label = options[chosen_idx_1based - 1]
        elif custom_input:
            chosen_label = custom_input.strip()
        elif dec_norm:
            chosen_label = dec_norm
        else:
            chosen_idx_1based = 1
            chosen_label = options[0] if options else "Approve once"

        action_trace = ""
        delivered = False

        try:
            loop = asyncio.get_running_loop()
        except Exception:
            loop = None

        # ── Engine 1: DSH (`@zseven-w/dsh-crew` + `dsh-security-mode`) ──────────────────────────
        if eng == "dsh" or sess.dsh_spec:
            dsh_job_id = meta.get("dsh_job_id") or sess.dsh_job_id
            dsh_token = meta.get("dsh_token") or sess.dsh_token or ""
            dsh_msg_id = meta.get("dsh_message_id")
            dsh_needs_continue = bool(meta.get("dsh_needs_continue"))
            reply_str = custom_input.strip() if (custom_input and not option_index and not decision) else chosen_label
            if not reply_str:
                if dec_norm in ("deny", "reject", "no"):
                    reply_str = "Reject"
                elif dec_norm in ("approve_session", "allow_session", "always"):
                    reply_str = "Allow for this session"
                else:
                    reply_str = "Allow once"

            if dsh_job_id and dsh_msg_id and not dsh_needs_continue:
                r = httpx.post(
                    f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{dsh_job_id}/messages",
                    headers={"x-dsh-crew-job-token": dsh_token},
                    json={"message": reply_str, "reply_to": dsh_msg_id},
                    timeout=6.0,
                )
                delivered = r.is_success
                action_trace = f"DSH mailbox reply_to={dsh_msg_id} message={reply_str!r} (HTTP {r.status_code})"
                sess.emit_virtual_ansi(
                    f"\r\n\x1b[1;32m↩ [Parent Approved/Replied to DSH ({dsh_msg_id})]: {reply_str}\x1b[0m\r\n"
                )
            elif dsh_job_id:
                new_token = str(uuid.uuid4())
                r = httpx.post(
                    f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{dsh_job_id}/continue",
                    json={"text": reply_str, "messaging_token": new_token},
                    timeout=10.0,
                )
                cjob = r.json().get("job", {}) if r.is_success else {}
                if cjob.get("id"):
                    sess.dsh_job_id = cjob["id"]
                    sess.dsh_token = new_token
                    sess.status = "running"
                    if loop:
                        loop.create_task(self._watch_dsh_hub_job(sess, cjob["id"], new_token))
                    delivered = True
                action_trace = f"DSH /continue job={dsh_job_id}->{cjob.get('id')} text={reply_str!r}"
                sess.emit_virtual_ansi(
                    f"\r\n\x1b[1;32m↻ [Parent Continued DSH needs_input with]: {reply_str}\x1b[0m\r\n"
                )

        # ── Engine 2: Kimi CLI (`ApprovalPanelComponent` & `QuestionDialogComponent`) ──────────
        elif eng == "kimi":
            if kind == "approval_request":
                # In Kimi ApprovalPanelComponent:
                # 1 = Approve once, 2 = Approve for this session, 3 = Reject, 4 = Reject with feedback
                if dec_norm in ("deny", "reject", "no") and custom_input:
                    sess.write_input("4")
                    if loop:
                        loop.call_later(0.25, sess.write_input, custom_input.strip() + "\r")
                    else:
                        time.sleep(0.25)
                        sess.write_input(custom_input.strip() + "\r")
                    action_trace = f"Kimi ApprovalPanel key '4' + feedback {custom_input.strip()!r}"
                else:
                    key_num = str(chosen_idx_1based or 1)
                    if key_num not in ("1", "2", "3", "4"):
                        key_num = "1"
                    sess.write_input(key_num)
                    action_trace = f"Kimi ApprovalPanel key {key_num!r} ({chosen_label})"
                delivered = True
            else:
                # In Kimi QuestionDialogComponent:
                # Single-select: pressing number key 1..N selects option AND advances to Submit tab!
                # Then sending "\r" after 250ms triggers executeSubmitAction(0) ("Submit").
                raw_opts_count = int(meta.get("raw_options_count") or len(options) or 2)
                is_multi = bool(meta.get("multi_select", False))
                if custom_input and (chosen_idx_1based is None or chosen_idx_1based > raw_opts_count):
                    other_key = str(raw_opts_count + 1)
                    sess.write_input(other_key)
                    if loop:
                        loop.call_later(0.25, sess.write_input, custom_input.strip() + "\r")
                        loop.call_later(0.55, sess.write_input, "\r")
                    else:
                        time.sleep(0.25)
                        sess.write_input(custom_input.strip() + "\r")
                        time.sleep(0.25)
                        sess.write_input("\r")
                    action_trace = f"Kimi QuestionDialog Other key {other_key!r} + text {custom_input.strip()!r} + Submit('\\r')"
                else:
                    key_num = str(chosen_idx_1based or 1)
                    sess.write_input(key_num)
                    if is_multi:
                        # In multi-select, Tab advances to Submit tab, then Enter submits
                        if loop:
                            loop.call_later(0.2, sess.write_input, "\t")
                            loop.call_later(0.45, sess.write_input, "\r")
                        else:
                            time.sleep(0.2)
                            sess.write_input("\t")
                            time.sleep(0.25)
                            sess.write_input("\r")
                    else:
                        if loop:
                            loop.call_later(0.25, sess.write_input, "\r")
                        else:
                            time.sleep(0.25)
                            sess.write_input("\r")
                    action_trace = f"Kimi QuestionDialog option key {key_num!r} ({chosen_label}) + Submit('\\r')"
                delivered = True

        # ── Engine 3: OMP (`ask` tool selector `class qR` & `showHookConfirm`) ────────────────
        elif eng == "omp":
            raw_opts_count = int(meta.get("raw_options_count") or max(1, len(options) - 1))
            clamp_ups = "\x1b[A" * (len(options) + 4)
            if custom_input and (chosen_idx_1based is None or "other" in chosen_label.lower()):
                # "Other (type your own)" is at 0-based index `raw_opts_count`
                downs = "\x1b[B" * raw_opts_count
                sess.write_input(clamp_ups)
                if loop:
                    loop.call_later(0.15, sess.write_input, downs + "\r")
                    loop.call_later(0.45, sess.write_input, custom_input.strip() + "\r")
                else:
                    time.sleep(0.15)
                    sess.write_input(downs + "\r")
                    time.sleep(0.25)
                    sess.write_input(custom_input.strip() + "\r")
                action_trace = f"OMP selector clamp_top + {raw_opts_count}xDown (Other) + text {custom_input.strip()!r}"
            else:
                target_0_idx = max(0, (chosen_idx_1based or 1) - 1)
                downs = "\x1b[B" * target_0_idx
                sess.write_input(clamp_ups)
                if loop:
                    loop.call_later(0.15, sess.write_input, downs + "\r")
                else:
                    time.sleep(0.15)
                    sess.write_input(downs + "\r")
                action_trace = f"OMP selector clamp_top + {target_0_idx}xDown + '\\r' ({chosen_label})"
            delivered = True

        # ── Engine 4: Codex CLI (Approval modal & `request_user_input_async`) ─────────────────
        elif eng == "codex":
            if kind == "approval_request":
                if dec_norm in ("deny", "reject", "no") or chosen_idx_1based == 3:
                    sess.write_input("n")
                    if custom_input:
                        if loop:
                            loop.call_later(0.25, sess.write_input, custom_input.strip() + "\r")
                        else:
                            time.sleep(0.25)
                            sess.write_input(custom_input.strip() + "\r")
                    action_trace = "Codex approval key 'n' (Reject)"
                elif dec_norm in ("approve_session", "allow_session", "always") or chosen_idx_1based == 2:
                    sess.write_input("a")
                    action_trace = "Codex approval key 'a' (Approve for session)"
                else:
                    sess.write_input("y")
                    action_trace = "Codex approval key 'y' (Approve once)"
                delivered = True
            else:
                # For Codex `request_user_input_async` / question prompt:
                reply_txt = custom_input.strip() if custom_input else chosen_label
                sess.write_input(reply_txt)
                if loop:
                    loop.call_later(0.25, sess.write_input, "\r")
                else:
                    time.sleep(0.25)
                    sess.write_input("\r")
                action_trace = f"Codex question reply {reply_txt!r} + '\\r'"
                delivered = True

        # ── Engine 5: AGY CLI (Permission prompt & `ask_question` selector) ───────────────────
        elif eng == "agy":
            if kind == "approval_request":
                if dec_norm in ("deny", "reject", "no") or (chosen_idx_1based and chosen_idx_1based >= 2):
                    sess.write_input("\x1b[B\r")
                    action_trace = "AGY permission prompt Down + '\\r' (Deny)"
                else:
                    sess.write_input("\r")
                    action_trace = "AGY permission prompt '\\r' (Allow)"
                delivered = True
            else:
                clamp_ups = "\x1b[A" * (len(options) + 3)
                if custom_input and not option_index:
                    # Select 'Other' at bottom of options list
                    downs = "\x1b[B" * len(options)
                    sess.write_input(clamp_ups)
                    if loop:
                        loop.call_later(0.15, sess.write_input, downs + "\r")
                        loop.call_later(0.4, sess.write_input, custom_input.strip() + "\r")
                    else:
                        time.sleep(0.15)
                        sess.write_input(downs + "\r")
                        time.sleep(0.25)
                        sess.write_input(custom_input.strip() + "\r")
                    action_trace = f"AGY ask_question Other + {custom_input.strip()!r}"
                else:
                    target_0_idx = max(0, (chosen_idx_1based or 1) - 1)
                    downs = "\x1b[B" * target_0_idx
                    sess.write_input(clamp_ups)
                    if loop:
                        loop.call_later(0.15, sess.write_input, downs + "\r")
                    else:
                        time.sleep(0.15)
                        sess.write_input(downs + "\r")
                    action_trace = f"AGY ask_question clamp_top + {target_0_idx}xDown + '\\r' ({chosen_label})"
                delivered = True

        # ── Generic / Fallback TUI or Worker MCP `ask_parent` ─────────────────────────────────
        else:
            if q_entry and not q_entry.get("answered"):
                q_entry["answered"] = True
                q_entry["answer"] = chosen_label
                q_entry["event"].set()
            sess.write_input(chosen_label)
            if loop:
                loop.call_later(0.25, sess.write_input, "\r")
            else:
                sess.write_input("\r")
            action_trace = f"Generic PTY write {chosen_label!r} + '\\r'"
            delivered = True

        # Mark pending question/interaction as resolved
        self.clear_pending_interaction(sess, target_qid)
        sess.current_activity = f"Parent responded ({chosen_label[:50]})"
        self.db.add_intermediate_message(
            session_id,
            sess.step_count,
            f"[Parent Delegated Response -> {chosen_label}] ({action_trace})",
            source="parent_approval_proxy",
        )
        self.persist_session(sess)

        return {
            "ok": True,
            "delivered": delivered,
            "session_id": session_id,
            "engine": eng,
            "resolved_request_id": target_qid,
            "interaction_kind": kind,
            "chosen_option_index": chosen_idx_1based,
            "chosen_label": chosen_label,
            "action_trace": action_trace,
        }

    def begin_new_turn(self, session: AgentSession, status: Optional[str] = None) -> None:
        """T1/D2: a message to a completed session (or a resume) starts a new turn.

        This is the one definition of the decision "the previous final reply is kept until the new
        turn overwrites it". Both paths call it: a live follow-up from deliver_parent_message and
        both resume paths (§5.3), right after db.reset_session_after_resume. The answer and its
        source stay in place, and the flag marks them as belonging to the ended turn:
        set_final_response lets the new turn's first source replace them whatever its rank. The
        priority table still governs the rest of that turn. completion_reported restarts with the
        turn, so a later exit is judged on this turn's evidence.
        """
        session.final_response_from_previous_turn = bool(session.final_response)
        session.completion_reported = False
        if status is not None:
            session.status = status

    def deliver_parent_message(
        self,
        session_id: str,
        text: str,
        request_id: Optional[str] = None,
        also_write_pty: bool = True,
        press_enter: bool = True,
    ) -> Dict[str, Any]:
        sess = self.sessions.get(session_id)
        # T1: this message may start a new turn on a session that already reported completion. The
        # status is captured before the delivery branches change it, and the reset itself runs at
        # the end, only when the text really reached the session — a failed delivery must not leave
        # the session "running" with no turn behind it.
        starts_new_turn = bool(sess is not None and sess.status == "completed")
        # If there is an active structured approval/question interaction on this session,
        # route through our engine-aware responder automatically!
        if sess and sess.pending_interaction:
            t_clean = text.strip()
            opt_idx: Optional[int] = None
            dec: Optional[str] = None
            if t_clean.isdigit():
                opt_idx = int(t_clean)
            elif t_clean.lower() in (
                "approve",
                "allow",
                "allow once",
                "yes",
                "y",
                "approve_session",
                "allow for this session",
                "always",
                "deny",
                "reject",
                "no",
                "n",
            ):
                dec = t_clean.lower().replace(" ", "_")
            res = self.respond_to_interaction(
                session_id=session_id,
                decision=dec,
                option_index=opt_idx,
                custom_input=None if (opt_idx or dec) else t_clean,
                request_id=request_id,
            )
            return {
                "delivered": res.get("delivered", True),
                "session_id": session_id,
                "resolved_question_id": res.get("resolved_request_id"),
                "pty_written": True,
                "injection": "interaction",
                "interaction_response": res,
            }

        resolved_question_id = None

        target_qid = request_id or (sess.pending_question_id if sess else None)
        dsh_replied = False
        if target_qid and target_qid in self.pending_questions:
            q_entry = self.pending_questions[target_qid]
            if not q_entry["answered"]:
                q_entry["answered"] = True
                q_entry["answer"] = text
                q_entry["event"].set()
                resolved_question_id = target_qid
                if q_entry.get("dsh_message_id") and q_entry.get("dsh_job_id"):
                    try:
                        httpx.post(
                            f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{q_entry['dsh_job_id']}/messages",
                            headers={"x-dsh-crew-job-token": q_entry["dsh_token"]},
                            json={"message": text, "reply_to": q_entry["dsh_message_id"]},
                            timeout=5.0,
                        )
                        dsh_replied = True
                        if sess:
                            sess.pending_question_id = None
                            sess.status = "running"
                            sess.emit_virtual_ansi(
                                f"\r\n\x1b[1;32m↩ [Parent Replied to DSH Question {resolved_question_id}]: {text}\x1b[0m\r\n"
                            )
                    except Exception as e:
                        if sess:
                            sess.emit_virtual_ansi(f"\r\n\x1b[31m✖ Failed to deliver reply to DSH: {e}\x1b[0m\r\n")

        box = self.parent_messages.setdefault(session_id, [])
        msg_rec = {
            "id": f"pmsg-{uuid.uuid4().hex[:8]}",
            "session_id": session_id,
            "timestamp": round(time.time(), 3),
            "content": text,
            "resolved_question_id": resolved_question_id,
            "read_by_worker": bool(resolved_question_id),
        }
        box.append(msg_rec)

        pty_written = False
        dsh_error: Optional[str] = None
        if sess and sess.dsh_spec and not resolved_question_id and not dsh_replied:
            clean_txt = text.strip()
            if not clean_txt:
                dsh_error = "消息为空，未发送给 DSH"
            else:
                try:
                    loop = asyncio.get_running_loop()
                    if sess.dsh_job_id and sess.status == "running":
                        r = httpx.post(
                            f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{sess.dsh_job_id}/messages",
                            headers={"x-dsh-crew-job-token": sess.dsh_token or ""},
                            json={"message": clean_txt},
                            timeout=5.0,
                        )
                        if not r.is_success:
                            dsh_error = f"DSH 拒绝了这条消息（HTTP {r.status_code}）"
                        else:
                            sess.emit_virtual_ansi(f"\r\n\x1b[1;36m➤ [Parent Steer Injected into DSH]: {clean_txt}\x1b[0m\r\n")
                            pty_written = True
                    elif sess.dsh_job_id and sess.status in ("completed", "exited"):
                        new_token = str(uuid.uuid4())
                        r = httpx.post(
                            f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{sess.dsh_job_id}/continue",
                            json={"text": clean_txt, "messaging_token": new_token},
                            timeout=10.0,
                        )
                        cjob = (r.json() or {}).get("job", {}) if r.is_success else {}
                        if cjob.get("id"):
                            sess.dsh_job_id = cjob["id"]
                            sess.dsh_token = new_token
                            sess.conversation_ref = {
                                "dsh_job_id": cjob["id"],
                                "dsh_session_id": sess.dsh_session_id or cjob.get("sessionId") or "",
                                "dsh_token": new_token,
                            }
                            sess.status = "running"
                            # B9: last_task stays the initial task; follow-ups live in current_activity.
                            if not sess.last_task:
                                sess.last_task = clean_txt
                            sess.current_activity = f"Continuing DSH session ({cjob['id']}): {clean_txt[:60]}"
                            sess.emit_virtual_ansi(
                                f"\r\n\x1b[1;36m↻ [Continuing DSH Session {sess.dsh_session_id} -> {cjob['id']}]: {clean_txt}\x1b[0m\r\n"
                            )
                            loop.create_task(self._watch_dsh_hub_job(sess, cjob["id"], new_token))
                            pty_written = True
                        else:
                            dsh_error = f"DSH 无法继续该任务（HTTP {r.status_code}）"
                    elif not sess.dsh_job_id:
                        loop.create_task(self._spawn_dsh_hub_job(sess, clean_txt))
                        pty_written = True
                    else:
                        # A8: a DSH session in any other status cannot take the text; say so.
                        dsh_error = f"DSH 会话当前状态（{sess.status}）无法接收消息"
                except Exception as e:
                    dsh_error = f"DSH 消息发送失败：{e}"
                    sess.emit_virtual_ansi(f"\r\n\x1b[31m✖ DSH message delivery error: {e}\x1b[0m\r\n")
        elif sess and also_write_pty and not resolved_question_id:
            if sess.kind == "desktop_gui" and sess.hwnd:
                payload = text + ("\r" if press_enter and not text.endswith("\r") else "")
                pty_written = Win32WindowHelper.send_text(sess.hwnd, payload)
            else:
                clean_txt = text.rstrip("\r\n") if press_enter else text
                pty_written = sess.write_input(clean_txt)
                if pty_written and clean_txt:
                    # W11: a live steering write is the last injection too, so the tool result no
                    # longer shows last_injection:null after a send_to_subagent.
                    sess.last_injection = {
                        "at": round(time.time(), 3),
                        "ok": True,
                        "reason": "live_write",
                        "attempts": 1,
                        "mode": "parent_message",
                        "label": "已写入",
                    }
                if press_enter:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.call_later(0.25, sess.write_input, "\r")
                    except Exception:
                        sess.write_input("\r")

        if dsh_error is not None:
            # A8: a message DSH could not take is reported with its reason, never as a canned true.
            if sess:
                self.persist_session(sess)
            return {
                "delivered": False,
                "reason": dsh_error,
                "session_id": session_id,
                "resolved_question_id": resolved_question_id,
                "pty_written": False,
                "injection": "dsh_failed",
                "mailbox_message_id": msg_rec["id"],
            }

        if sess and starts_new_turn and pty_written and not resolved_question_id:
            # T1: send_to_subagent's live write, the console's send and a parent reply all land
            # here. The completed turn's answer is cleared and the status goes back to running, so
            # the wait in send_to_subagent sees a turn in progress instead of status_completed.
            self.begin_new_turn(sess, status="running")

        if sess:
            self.persist_session(sess)

        return {
            "delivered": True,
            "session_id": session_id,
            "resolved_question_id": resolved_question_id,
            "pty_written": pty_written,
            "injection": "live_write" if pty_written else "none",
            "mailbox_message_id": msg_rec["id"],
        }

    def get_worker_events(self, session_id: str = "all", after_seq: int = 0) -> List[Dict[str, Any]]:
        target_ids = None
        if session_id and session_id != "all":
            target_ids = {s.strip() for s in session_id.split(",") if s.strip()}

        results = []
        for ev in self.worker_events:
            if ev["seq"] <= after_seq:
                continue
            if target_ids and ev["session_id"] not in target_ids:
                continue
            results.append(ev)
        return results

    async def poll_worker_events(
        self,
        session_id: str = "all",
        after_seq: int = 0,
        timeout_sec: float = 25.0,
    ) -> Dict[str, Any]:
        immediate = self.get_worker_events(session_id=session_id, after_seq=after_seq)
        if immediate:
            next_seq = immediate[-1]["seq"]
            return {
                "woke_reason": "events_available",
                "session_id": session_id,
                "after_seq": after_seq,
                "next_seq": next_seq,
                "events": immediate,
                "next_poll_command": self.build_poll_command(session_id, next_seq),
            }

        waiter = asyncio.Event()
        self._event_waiters.add(waiter)
        start = time.time()
        try:
            while True:
                remaining = timeout_sec - (time.time() - start)
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                waiter.clear()
                found = self.get_worker_events(session_id=session_id, after_seq=after_seq)
                if found:
                    next_seq = found[-1]["seq"]
                    return {
                        "woke_reason": "worker_event_triggered",
                        "session_id": session_id,
                        "after_seq": after_seq,
                        "next_seq": next_seq,
                        "events": found,
                        "next_poll_command": self.build_poll_command(session_id, next_seq),
                    }
        finally:
            self._event_waiters.discard(waiter)

        return {
            "woke_reason": "timeout_no_new_events",
            "session_id": session_id,
            "after_seq": after_seq,
            "next_seq": max(after_seq, self.global_seq),
            "events": [],
            "next_poll_command": self.build_poll_command(session_id, after_seq),
        }

    def build_poll_command(self, session_id: str, after_seq: int = 0, timeout: int = 1800) -> str:
        # K3: the interpreter the hub itself runs on, exactly as the bridge hint already does. A
        # worktree (or any checkout without a local .venv) has no <repo>/.venv, and the parent was
        # handed a path that did not exist.
        python_exe = Path(sys.executable)
        poll_script = self.base_dir / "hub_poll.py"
        # R9: the poll command must reach the hub that issued it, not a hard-coded default port.
        return (
            f'& "{python_exe}" "{poll_script}" --session {session_id} '
            f'--after-seq {after_seq} --timeout {timeout} --hub-url "{hub_api_base_url()}"'
        )

    def _transcript_watch_alive(self, session: AgentSession) -> bool:
        """F2: a transcript watcher follows the process, not the status label.

        `completed` is a live status (archive-spec §2): the process is still running, and kimi
        writes its real reply and `agent.turn.ended` to the transcript only after it reported
        completion. The watcher therefore stops when the process ends, when the session leaves the
        live list, or at its own deadline -- never merely because the report said `completed`.
        """
        if self.sessions.get(session.session_id) is not session:
            return False
        try:
            return bool(session.is_process_alive())
        except Exception:
            return False

    async def _watch_codex_rollout(self, session: AgentSession, root: Optional[Path] = None):
        """Live tails the Codex rollout jsonl files for a spawned Codex session.

        `root` points the watcher at a synthetic copy of the layout for tests; production uses
        ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.
        """
        codex_root = Path(root) if root is not None else codex_sessions_root()
        if not codex_root.exists():
            return

        target_file: Optional[Path] = None
        offset = 0
        scan_start = time.time()

        if session.resume_attach and session.conversation_ref:
            # Spec §4: attach directly to the known rollout (no discovery heuristic), tail from its end.
            ref_path = str(session.conversation_ref.get("rollout_path") or "")
            if ref_path and os.path.isfile(ref_path):
                target_file = Path(ref_path)
                offset = int(session.attach_offset or 0)
                self._claim_transcript(session, target_file)
        else:
            while self._transcript_watch_alive(session):
                await asyncio.sleep(self._transcript_scan_interval(time.time() - scan_start))
                if not self._transcript_watch_alive(session):
                    break
                candidates = []
                for candidate in codex_root.rglob("rollout-*.jsonl"):
                    try:
                        # V2/Q2: only a rollout created after this session's process started. The marker
                        # check and the launch-order fallback live in _claim_conversation.
                        if not self._conversation_created_after(candidate, session):
                            continue
                        candidates.append(candidate)
                    except OSError:
                        continue
                target_file = self._claim_conversation(session, candidates, levels_up=0)
                if target_file:
                    break

        if not target_file:
            return

        while self._transcript_watch_alive(session):
            if session.resume_attach:
                # A resumed codex may start a new rollout for the same session id; accept it when it
                # appears and tail it from the beginning (everything in it is new).
                switched = self._codex_resume_new_rollout(session, codex_root, target_file, scan_start)
                if switched is not None:
                    target_file = switched
                    offset = 0
            try:
                with open(target_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    lines = f.readlines()
                    offset = f.tell()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    otype = obj.get("type")
                    payload = obj.get("payload") or {}
                    ptype = payload.get("type")

                    if otype == "response_item":
                        if ptype in ("custom_tool_call", "function_call"):
                            tname = payload.get("name") or payload.get("tool_name") or ptype
                            targs = payload.get("arguments") or payload.get("input") or ""
                            self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "codex_jsonl",
                                    "hook_event": "ToolCall",
                                    "tool_name": tname,
                                    "tool_input": targs,
                                },
                            )
                            if tname in ("request_user_input", "request_user_input_async"):
                                try:
                                    parsed_args = json.loads(targs) if isinstance(targs, str) else (targs or {})
                                except Exception:
                                    parsed_args = {}
                                qs = parsed_args.get("questions") or []
                                q0 = qs[0] if qs and isinstance(qs[0], dict) else {}
                                q_title = str(q0.get("header") or q0.get("id") or "Codex Question")
                                q_prompt = str(q0.get("question") or q0.get("title") or targs)
                                raw_opts = q0.get("options") or []
                                opt_labels = [
                                    str(o.get("label") or o.get("value") or o) if isinstance(o, dict) else str(o)
                                    for o in raw_opts
                                ]
                                self.register_pending_interaction(
                                    session=session,
                                    kind="question",
                                    title=q_title,
                                    prompt=q_prompt,
                                    options=opt_labels,
                                    source="codex_jsonl_request_user_input",
                                    request_id=payload.get("call_id") or f"codex-q-{uuid.uuid4().hex[:6]}",
                                    metadata={"raw_options_count": len(opt_labels)},
                                )
                        elif ptype == "message" and payload.get("role") == "assistant":
                            parts = payload.get("content") or []
                            texts = [
                                p.get("text", "")
                                for p in parts
                                if isinstance(p, dict) and p.get("type") in ("output_text", "text") and p.get("text")
                            ]
                            if texts:
                                msg_txt = "\n".join(texts).strip()
                                self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "codex_jsonl",
                                        "hook_event": "AssistantMessage",
                                        "intermediate_messages": [msg_txt],
                                        "final_assistant_text": msg_txt,
                                    },
                                )
                    elif otype == "event_msg" and ptype in ("exec_approval_request", "apply_patch_approval_request"):
                        cmd_desc = payload.get("command") or payload.get("reason") or json.dumps(payload, ensure_ascii=False)[:300]
                        if isinstance(cmd_desc, list):
                            cmd_desc = " ".join(str(x) for x in cmd_desc)
                        self.register_pending_interaction(
                            session=session,
                            kind="approval_request",
                            title=f"Codex {ptype}",
                            prompt=f"Would you like to run/apply: {cmd_desc}",
                            options=[
                                "Yes, proceed (y)",
                                "Yes, and don't ask again for this command (a)",
                                "No, and tell Codex what to do differently (n)",
                            ],
                            source="codex_jsonl_approval",
                            request_id=payload.get("call_id") or f"codex-appr-{uuid.uuid4().hex[:6]}",
                        )
                    elif otype == "event_msg" and ptype == "task_complete":
                        last_msg = payload.get("last_agent_message")
                        if last_msg:
                            session.set_final_response(str(last_msg), "codex_task_complete")
                            self.record_worker_event(
                                session.session_id,
                                {
                                    "type": "turn_completed",
                                    "category": "turn_completed",
                                    "status": "turn_idle",
                                    "content": str(last_msg),
                                },
                            )
            except Exception:
                pass
            await asyncio.sleep(1.0)

        if session.resume_attach and session.status in ("exited", "crashed"):
            # Research: an archived codex session refuses to resume; surface its own instruction.
            screen = session.get_clean_screen_text(max_lines=40).lower()
            if "is archived" in screen or "unarchive" in screen:
                self._mark_codex_archived(session)

    @staticmethod
    def _codex_date_dirs(root: Path, anchor: float, now: float) -> List[Path]:
        """A13: the sessions/<Y>/<M>/<D> folders a resume can possibly write into.

        The spawn date, plus every day up to today when the resume crosses midnight — never a
        recursive walk over the whole store, which is what made the check slow on a year of rollouts.
        """
        try:
            start = datetime.datetime.fromtimestamp(float(anchor))
            end = datetime.datetime.fromtimestamp(float(now))
        except (OverflowError, OSError, ValueError):
            return []
        days: List[Path] = []
        day = start.date()
        last = end.date()
        for _ in range(4):
            days.append(Path(root) / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}")
            if day >= last:
                break
            day = day + datetime.timedelta(days=1)
        return days

    def _codex_resume_new_rollout(
        self, session: AgentSession, root: Path, current: Path, scan_start: float
    ) -> Optional[Path]:
        ref = session.conversation_ref or {}
        sid = str(ref.get("session_id") or "")
        if not sid or (time.time() - scan_start) > 60.0:
            return None
        candidates: List[Path] = []
        for folder in self._codex_date_dirs(
            Path(root), self._session_spawn_anchor(session), time.time()
        ):
            if not folder.is_dir():
                continue
            for candidate in folder.glob("rollout-*.jsonl"):
                try:
                    if candidate == Path(current) or not self._conversation_created_after(candidate, session):
                        continue
                    if self._codex_session_id_from_rollout(candidate) != sid:
                        continue
                    candidates.append(candidate)
                except OSError:
                    continue
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0)
        chosen = candidates[-1]
        if not self._claim_transcript(session, chosen):
            return None
        session.conversation_ref = {"session_id": sid, "rollout_path": str(chosen)}
        self.persist_session(session)
        return chosen

    def _mark_codex_archived(self, session: AgentSession) -> None:
        reason = CODEX_ARCHIVED_MESSAGE.format(sid=(session.conversation_ref or {}).get("session_id") or "")
        session.resumable = False
        session.resume_block_reason = reason
        self.db.update_archive_fields(session.session_id, resumable=0, resume_block_reason=reason)
        session.emit_virtual_ansi(f"\r\n\x1b[1;31m✖ {reason}\x1b[0m\r\n")
        self.record_worker_event(
            session.session_id,
            {"type": "error", "category": "lifecycle", "status": "failed", "content": reason},
        )

    async def _watch_omp_session(self, session: AgentSession, omp_session_dir: Path):
        """Live tails OMP's dedicated session JSONL file inside omp_session_dir for tool calls, thoughts, and final responses."""
        target_file: Optional[Path] = None
        offset = 0
        scan_start = time.time()
        if session.resume_attach and session.conversation_ref:
            # Spec §4: omp appends to the same session file, so attach to it and tail from the end.
            ref_path = str(session.conversation_ref.get("session_file") or "")
            if ref_path and os.path.isfile(ref_path):
                target_file = Path(ref_path)
                offset = int(session.attach_offset or 0)
                self._claim_transcript(session, target_file)
        else:
            while self._transcript_watch_alive(session):
                await asyncio.sleep(self._transcript_scan_interval(time.time() - scan_start))
                if not self._transcript_watch_alive(session):
                    break
                # This directory is created by the hub for this one session (`--session-dir`), so a
                # cross-session attach is already impossible; claim it anyway so the rule is uniform.
                files = sorted(omp_session_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                for candidate in files:
                    if not self._conversation_created_after(candidate, session):
                        continue
                    if self._claim_transcript(session, candidate):
                        target_file = candidate
                        break
                if target_file:
                    break
        if not target_file:
            return

        while self._transcript_watch_alive(session):
            try:
                with open(target_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    lines = f.readlines()
                    offset = f.tell()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    otype = obj.get("type")
                    ctype = obj.get("customType")
                    if otype == "custom" and ctype == "tool_execution_start":
                        data = obj.get("data") or {}
                        tname = data.get("toolName", "tool")
                        targs = data.get("args") or {}
                        self.record_telemetry(
                            session.session_id,
                            {
                                "source": "omp_jsonl",
                                "hook_event": "PreToolUse",
                                "tool_name": tname,
                                "tool_input": targs,
                            },
                        )
                        if tname == "ask" and isinstance(targs, dict):
                            qs = targs.get("questions") or []
                            q0 = qs[0] if qs and isinstance(qs[0], dict) else targs
                            q_header = str(q0.get("header") or q0.get("id") or "OMP Ask")
                            q_prompt = str(q0.get("question") or "Sub-Agent is asking a question")
                            raw_opts = q0.get("options") or []
                            opt_labels = [
                                str(o.get("label") or o) if isinstance(o, dict) else str(o)
                                for o in raw_opts
                            ]
                            display_opts = list(opt_labels) + ["Other (type your own)"]
                            self.register_pending_interaction(
                                session=session,
                                kind="question",
                                title=q_header,
                                prompt=q_prompt,
                                options=display_opts,
                                source="omp_jsonl_ask_tool",
                                request_id=str(data.get("toolCallId") or f"omp-ask-{uuid.uuid4().hex[:6]}"),
                                metadata={
                                    "raw_options_count": len(opt_labels),
                                    "recommended": q0.get("recommended"),
                                    "multi": bool(q0.get("multi", False)),
                                },
                            )
                    elif otype == "message":
                        msg = obj.get("message") or {}
                        role = msg.get("role")
                        content = msg.get("content") or []
                        if role == "toolResult":
                            if msg.get("toolName") == "ask":
                                self.clear_pending_interaction(session)
                            self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "omp_jsonl",
                                    "hook_event": "PostToolUse",
                                    "tool_name": msg.get("toolName"),
                                    "tool_response": content,
                                },
                            )
                        elif role == "assistant" and isinstance(content, list):
                            thoughts = []
                            texts = []
                            has_tool_use = False
                            for part in content:
                                if not isinstance(part, dict):
                                    continue
                                ptype = part.get("type")
                                if ptype == "thinking" and part.get("thinking"):
                                    thoughts.append(str(part["thinking"]).strip())
                                elif ptype == "text" and part.get("text"):
                                    texts.append(str(part["text"]).strip())
                                elif ptype in ("toolCall", "tool_use"):
                                    has_tool_use = True
                                    if part.get("name") == "ask" and isinstance(part.get("arguments"), dict):
                                        targs = part["arguments"]
                                        qs = targs.get("questions") or []
                                        q0 = qs[0] if qs and isinstance(qs[0], dict) else targs
                                        q_header = str(q0.get("header") or q0.get("id") or "OMP Ask")
                                        q_prompt = str(q0.get("question") or "Sub-Agent is asking a question")
                                        raw_opts = q0.get("options") or []
                                        opt_labels = [
                                            str(o.get("label") or o) if isinstance(o, dict) else str(o)
                                            for o in raw_opts
                                        ]
                                        display_opts = list(opt_labels) + ["Other (type your own)"]
                                        self.register_pending_interaction(
                                            session=session,
                                            kind="question",
                                            title=q_header,
                                            prompt=q_prompt,
                                            options=display_opts,
                                            source="omp_jsonl_ask_tool",
                                            request_id=str(part.get("id") or f"omp-ask-{uuid.uuid4().hex[:6]}"),
                                            metadata={
                                                "raw_options_count": len(opt_labels),
                                                "recommended": q0.get("recommended"),
                                                "multi": bool(q0.get("multi", False)),
                                            },
                                        )
                            inter = thoughts + texts
                            final_txt = "\n".join(texts).strip() if (texts and not has_tool_use) else None
                            self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "omp_jsonl",
                                    "hook_event": "TurnComplete" if final_txt else "AssistantMessage",
                                    "intermediate_messages": inter,
                                    "final_assistant_text": final_txt,
                                },
                            )
            except Exception:
                pass
            await asyncio.sleep(0.8)

    async def _watch_kimi_session(self, session: AgentSession, root: Optional[Path] = None):
        """Live tails Kimi Code CLI's wire.jsonl for toolCalls, assistant thoughts/messages, and agent.turn.ended.

        `root` points the watcher at a synthetic copy of the layout for tests; production uses
        ~/.kimi-code/sessions/<session>/agents/main/wire.jsonl.
        """
        kimi_root = (
            Path(root) if root is not None else kimi_store_root() / "sessions"
        )
        if not kimi_root.exists():
            return

        target_file: Optional[Path] = None
        offset = 0
        scan_start = time.time()

        if session.resume_attach and session.conversation_ref:
            # Spec §4: kimi appends to the same wire.jsonl; attach and tail from the end.
            session_dir = str(session.conversation_ref.get("session_dir") or "")
            ref_path = os.path.join(session_dir, "agents", "main", "wire.jsonl") if session_dir else ""
            if ref_path and os.path.isfile(ref_path):
                target_file = Path(ref_path)
                offset = int(session.attach_offset or 0)
                self._claim_transcript(session, target_file)

        while target_file is None and self._transcript_watch_alive(session):
            await asyncio.sleep(self._transcript_scan_interval(time.time() - scan_start))
            if not self._transcript_watch_alive(session):
                break
            # First try exact match via Kimi TUI banner: Session: session_<uuid>
            scr_txt = session.get_clean_screen_text(max_lines=35)
            m = re.search(r"(session_[0-9a-f\-]{20,})", scr_txt)
            if m:
                # The TUI banner names the exact conversation, so this path needs no time heuristic;
                # it still must not be a transcript another hub session is already tailing (V2).
                kimi_sid = m.group(1)
                exact = list(kimi_root.rglob(f"{kimi_sid}/agents/main/wire.jsonl"))
                if exact and exact[0].exists() and self._claim_transcript(session, exact[0]):
                    target_file = exact[0]
                    break

            candidates = []
            for p in kimi_root.rglob("wire.jsonl"):
                try:
                    # V2/Q2: a wire.jsonl that already existed is not this session's; only a session
                    # directory created after the launch. The marker check lives in _claim_conversation.
                    if not self._conversation_created_after(p, session, levels_up=2):
                        continue
                    candidates.append(p)
                except OSError:
                    pass
            target_file = self._claim_conversation(session, candidates, levels_up=2)
            if target_file:
                break

        if not target_file:
            return
        session.transcript_path = target_file
        self.persist_session(session)

        latest_assistant_text: Optional[str] = None
        while self._transcript_watch_alive(session):
            try:
                with open(target_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    lines = f.readlines()
                    offset = f.tell()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    if obj.get("type") == "interaction.request":
                        req_id = str(obj.get("id") or f"kimi-int-{uuid.uuid4().hex[:6]}")
                        ikind = obj.get("kind") or "question"
                        req_obj = obj.get("request") or {}
                        if ikind == "approval":
                            disp = req_obj.get("display") or {}
                            cmd_str = disp.get("command") or req_obj.get("description") or req_obj.get("action") or "action"
                            self.register_pending_interaction(
                                session=session,
                                kind="approval_request",
                                title=f"Kimi Approval: {req_obj.get('toolName') or 'Shell'} ({req_obj.get('action') or 'run'})",
                                prompt=f"Command/Action requiring approval: {cmd_str}",
                                options=[
                                    "Approve once",
                                    "Approve for this session",
                                    "Reject",
                                    "Reject with feedback",
                                ],
                                source="kimi_wire_jsonl_interaction_request",
                                request_id=req_id,
                            )
                        else:
                            qs = req_obj.get("questions") or []
                            q0 = qs[0] if qs and isinstance(qs[0], dict) else req_obj
                            q_header = str(q0.get("header") or "Kimi Question")
                            q_prompt = str(q0.get("question") or "Sub-Agent is asking a question")
                            raw_opts = q0.get("options") or []
                            opt_labels = [
                                f"{o.get('label', '')}" + (f" — {o.get('description')}" if o.get("description") else "")
                                if isinstance(o, dict)
                                else str(o)
                                for o in raw_opts
                            ]
                            display_opts = list(opt_labels) + ["Other (custom text input)"]
                            self.register_pending_interaction(
                                session=session,
                                kind="question",
                                title=q_header,
                                prompt=q_prompt,
                                options=display_opts,
                                source="kimi_wire_jsonl_interaction_request",
                                request_id=req_id,
                                metadata={
                                    "raw_options_count": len(opt_labels),
                                    "multi_select": bool(q0.get("multi_select", False)),
                                },
                            )
                        continue

                    if obj.get("type") == "interaction.resolved":
                        self.clear_pending_interaction(session, str(obj.get("id") or ""))
                        continue

                    if obj.get("type") == "turn.prompt" and (obj.get("origin") or {}).get("kind") == "user":
                        # K3 (fix 9): kimi's record of a user message the TUI really accepted. The
                        # typed-submit check reads it from the session; no second reader of the file.
                        parts = obj.get("input") or []
                        session.note_transcript_user_message(" ".join(
                            str(p.get("text") or "") for p in parts if isinstance(p, dict)
                        ))
                        continue

                    if obj.get("kind") == "event" and obj.get("type") == "agent.turn.ended":
                        if latest_assistant_text:
                            session.set_final_response(latest_assistant_text, "native_transcript")
                            self.record_worker_event(
                                session.session_id,
                                {
                                    "type": "turn_completed",
                                    "category": "turn_completed",
                                    "status": "turn_idle",
                                    "content": latest_assistant_text,
                                },
                            )
                        continue

                    if obj.get("type") == "context.append_loop_event":
                        ev = obj.get("event") or {}
                        ev_type = ev.get("type")
                        if ev_type == "content.part":
                            part = ev.get("part") or {}
                            if part.get("type") == "think" and part.get("think"):
                                self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "kimi_wire_jsonl",
                                        "hook_event": "AssistantMessage",
                                        "intermediate_messages": [str(part["think"]).strip()],
                                    },
                                )
                            elif part.get("type") == "text" and part.get("text"):
                                txt = str(part["text"]).strip()
                                latest_assistant_text = txt
                                self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "kimi_wire_jsonl",
                                        "hook_event": "AssistantMessage",
                                        "intermediate_messages": [txt],
                                        "final_assistant_text": txt,
                                    },
                                )
                        elif ev_type == "tool.call":
                            self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "kimi_wire_jsonl",
                                    "hook_event": "PreToolUse",
                                    "tool_name": ev.get("name") or "kimi_tool",
                                    "tool_input": ev.get("args") or "",
                                },
                            )
                        elif ev_type == "tool.result":
                            self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "kimi_wire_jsonl",
                                    "hook_event": "PostToolUse",
                                    "tool_name": ev.get("name"),
                                    "tool_response": ev.get("output") or ev.get("result") or "",
                                },
                            )
                        continue

                    outer_msg = obj.get("message")
                    if not isinstance(outer_msg, dict):
                        continue
                    inner = outer_msg.get("message") if isinstance(outer_msg.get("message"), dict) else outer_msg
                    role = inner.get("role")
                    content = inner.get("content") or []
                    tool_calls = inner.get("toolCalls") or []

                    if role == "assistant":
                        inter = []
                        texts = []
                        if isinstance(content, list):
                            for part in content:
                                if not isinstance(part, dict):
                                    continue
                                if part.get("type") == "think" and part.get("think"):
                                    inter.append(str(part["think"]).strip())
                                elif part.get("type") == "text" and part.get("text"):
                                    txt = str(part["text"]).strip()
                                    inter.append(txt)
                                    texts.append(txt)
                        if texts:
                            latest_assistant_text = "\n".join(texts)
                        if inter:
                            self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "kimi_wire_jsonl",
                                    "hook_event": "AssistantMessage",
                                    "intermediate_messages": inter,
                                    "final_assistant_text": latest_assistant_text,
                                },
                            )
                        for tc in tool_calls:
                            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                            tname = fn.get("name") or tc.get("name") or "kimi_tool"
                            targs = fn.get("arguments") or tc.get("arguments") or ""
                            self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "kimi_wire_jsonl",
                                    "hook_event": "PreToolUse",
                                    "tool_name": tname,
                                    "tool_input": targs,
                                },
                            )
                    elif role == "tool":
                        self.record_telemetry(
                            session.session_id,
                            {
                                "source": "kimi_wire_jsonl",
                                "hook_event": "PostToolUse",
                                "tool_response": content,
                            },
                        )
            except Exception:
                pass
            await asyncio.sleep(0.8)

    async def _watch_agy_transcript(self, session: AgentSession, root: Optional[Path] = None):
        """Live tails AGY CLI's transcript.jsonl.

        `root` points the watcher at a synthetic copy of the layout for tests; production is
        ~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript.jsonl (V3).
        """
        brain_root = Path(root) if root is not None else agy_brain_root()
        if not brain_root.exists():
            return

        target_file: Optional[Path] = None
        offset = 0
        scan_start = time.time()
        ref_path = ""

        if session.resume_attach and session.conversation_ref:
            # Spec §4: attach directly to the archived brain dir. agy silently starts an EMPTY
            # conversation for an unknown id, so a new conversation dir here means the resume failed.
            brain_dir = str(session.conversation_ref.get("brain_dir") or "")
            ref_path = os.path.join(brain_dir, ".system_generated", "logs", "transcript.jsonl") if brain_dir else ""
            if ref_path and os.path.isfile(ref_path):
                target_file = Path(ref_path)
                offset = int(session.attach_offset or 0)
                self._claim_transcript(session, target_file)

        if target_file is None and session.resume_attach and ref_path:
            # A3/V3: the archived brain dir is the reference. Some brain dirs hold no
            # transcript.jsonl until the resumed turn writes one, so wait for that file INSIDE the
            # same dir — and keep looking for a different new conversation, which is the failure.
            resume_attach_offset = int(session.attach_offset or 0)
            verify_deadline = self._session_spawn_anchor(session) + AGY_RESUME_VERIFY_WINDOW_SEC
            # R7: the wait for the transcript file gets its own 120 s deadline. The 30 s verify
            # window is only about stray conversations; the old loop spun for the session's whole
            # life when the file never appeared, and gave up at the first failed claim.
            transcript_deadline = time.time() + AGY_RESUME_TRANSCRIPT_WAIT_SEC
            while target_file is None and self._transcript_watch_alive(session):
                if time.time() > transcript_deadline:
                    logger.warning(
                        "agy 恢复：%.0f 秒内未能接管 transcript %s，停止监听会话 %s",
                        AGY_RESUME_TRANSCRIPT_WAIT_SEC, ref_path, session.session_id,
                    )
                    return
                if os.path.isfile(ref_path):
                    candidate = Path(ref_path)
                    if self._claim_transcript(session, candidate):
                        target_file = candidate
                        offset = resume_attach_offset
                        break
                    # R7: a failed claim means another watcher still owns the file; keep waiting
                    # (until the deadline above) instead of abandoning the resume.
                if time.time() <= verify_deadline:
                    stray = self._agy_new_conversation_dir(session, brain_root)
                    if stray is not None:
                        self._mark_agy_resume_failed(session)
                        return
                await asyncio.sleep(0.5)
            if target_file is None:
                return

        while target_file is None and not session.resume_attach and self._transcript_watch_alive(session):
            await asyncio.sleep(self._transcript_scan_interval(time.time() - scan_start))
            if not self._transcript_watch_alive(session):
                break
            candidates = []
            for p in brain_root.rglob("transcript.jsonl"):
                try:
                    # V2/Q2: the conversation directory (brain/<uuid>) must be younger than this
                    # session's process. A transcript that already existed belongs to somebody else.
                    if not self._conversation_created_after(p, session, levels_up=2):
                        continue
                    candidates.append(p)
                except OSError:
                    continue
            target_file = self._claim_conversation(session, candidates, levels_up=2)
            if target_file:
                break

        if not target_file:
            return

        attach_size = int(session.attach_offset or 0)
        resume_confirmed = not session.resume_attach
        # A3: the stray scan is bounded to the first 30 s after the resume spawn; past that it stops.
        verify_deadline = self._session_spawn_anchor(session) + AGY_RESUME_VERIFY_WINDOW_SEC
        while self._transcript_watch_alive(session):
            if session.resume_attach and not resume_confirmed:
                try:
                    if os.path.getsize(target_file) > attach_size:
                        resume_confirmed = True
                except OSError:
                    pass
                if not resume_confirmed and time.time() <= verify_deadline:
                    stray = self._agy_new_conversation_dir(session, brain_root)
                    if stray is not None:
                        self._mark_agy_resume_failed(session)
                        return
            try:
                with open(target_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    lines = f.readlines()
                    offset = f.tell()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "PLANNER_RESPONSE":
                        thinking = (obj.get("thinking") or "").strip()
                        content = (obj.get("content") or "").strip()
                        tcalls = obj.get("tool_calls") or []
                        # The real store writes tool_calls as a JSON string; an empty list (or an
                        # absent key) is the end-of-turn marker W6 needs.
                        if isinstance(tcalls, str):
                            try:
                                tcalls = json.loads(tcalls) if tcalls.strip() else []
                            except Exception:
                                tcalls = []
                        if not isinstance(tcalls, list):
                            tcalls = []
                        inter = [x for x in (thinking, content) if x]
                        if inter:
                            self.record_telemetry(
                                session.session_id,
                                {
                                    "source": "agy_transcript",
                                    "hook_event": "AssistantMessage",
                                    "intermediate_messages": inter,
                                    "final_assistant_text": content or None,
                                },
                            )
                        for tc in tcalls:
                            if isinstance(tc, dict):
                                tname = tc.get("name") or tc.get("tool_name") or "agy_tool"
                                targs = tc.get("args") or tc.get("arguments") or ""
                                self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "agy_transcript",
                                        "hook_event": "PreToolUse",
                                        "tool_name": tname,
                                        "tool_input": targs,
                                    },
                                )
                                if tname in ("ask_question", "default_api:ask_question"):
                                    parsed_args = targs if isinstance(targs, dict) else {}
                                    if isinstance(targs, str):
                                        try:
                                            parsed_args = json.loads(targs)
                                        except Exception:
                                            parsed_args = {}
                                    qs = parsed_args.get("questions") or []
                                    q0 = qs[0] if qs and isinstance(qs[0], dict) else parsed_args
                                    q_prompt = str(q0.get("question") or "AGY Sub-Agent is asking a question")
                                    raw_opts = [str(o) for o in (q0.get("options") or [])]
                                    self.register_pending_interaction(
                                        session=session,
                                        kind="question",
                                        title="AGY ask_question",
                                        prompt=q_prompt,
                                        options=raw_opts + ["Other (custom text input)"],
                                        source="agy_transcript_ask_question",
                                        request_id=f"agy-ask-{obj.get('step_index', uuid.uuid4().hex[:6])}",
                                        metadata={
                                            "raw_options_count": len(raw_opts),
                                            "multi_select": bool(q0.get("is_multi_select", False)),
                                        },
                                    )
                        if not tcalls:
                            # W6: a planner response with no pending tool calls is AGY's end of
                            # turn; the next transcript row is a new USER_INPUT (or the file ends).
                            # On a resume the watcher attaches at the old file size, so every event
                            # here belongs to a new turn and only one is emitted per turn.
                            final_text = content or thinking
                            if final_text:
                                session.set_final_response(final_text, "native_transcript")
                                self.record_worker_event(
                                    session.session_id,
                                    {
                                        "type": "turn_completed",
                                        "category": "turn_completed",
                                        "status": "turn_idle",
                                        "content": final_text,
                                    },
                                )
            except Exception:
                pass
            await asyncio.sleep(0.8)

    @staticmethod
    def _agy_conversation_is_durable(brain_dir: Path) -> bool:
        """A3/V3: a real agy conversation dir — or its conversations/<uuid>.db — not a bare dir.

        The old check keyed on transcript.jsonl existing, which the real store disproves: 2 of the
        60 brain dirs there hold no transcript at all. The brain dir (with content) and the SQLite
        conversation store are the durable signals.
        """
        p = Path(brain_dir)
        try:
            if not p.is_dir():
                return False
            # The store is a sibling of the brain dir: <root>/conversations/<uuid>.db beside
            # <root>/brain/<uuid> (confirmed against the real antigravity-cli layout), never a child
            # of the brain dir — a "conversations" folder inside brain/ is not a conversation.
            db = p.parent.parent / "conversations" / f"{p.name}.db"
            if db.is_file():
                return True
            return any(p.iterdir())
        except OSError:
            return False

    def _agy_new_conversation_dir(self, session: AgentSession, brain_root: Path) -> Optional[Path]:
        """A3: a conversation dir that proves the resumed agy opened a NEW conversation.

        Only within AGY_RESUME_VERIFY_WINDOW_SEC after the resume spawn, only a dir that is not the
        archived one, and only when it cannot belong to another agy session that is still starting
        (such a dir is "unknown", never a failure — one resume must not fail another's).
        """
        anchor = self._session_spawn_anchor(session)
        now = time.time()
        if now - anchor > AGY_RESUME_VERIFY_WINDOW_SEC:
            return None
        ref = session.conversation_ref or {}
        ref_id = str(ref.get("conversation_id") or "")
        claimed_dirs = {
            str((s.conversation_ref or {}).get("brain_dir") or "")
            for s in list(self.sessions.values())
            if s is not session
        }
        claimed_dirs.discard("")
        pending_elsewhere = False
        for other in list(self.sessions.values()):
            if other is session or (other.engine or "").strip().lower() != "agy":
                continue
            if other.conversation_ref or not other.is_process_alive():
                continue
            other_anchor = self._session_spawn_anchor(other)
            if (anchor - 1.0) <= other_anchor <= now:
                pending_elsewhere = True
                break
        try:
            children = list(Path(brain_root).iterdir())
        except OSError:
            return None
        for child in children:
            try:
                if not child.is_dir() or child.name == ref_id or str(child) in claimed_dirs:
                    continue
                created, exact = self._conversation_creation_time(child, levels_up=0)
                if created is None:
                    continue
                slack = AGY_CONVERSATION_ANCHOR_SLACK_SEC if exact else 1.0
                if created < (anchor - slack):
                    continue
                if not self._agy_conversation_is_durable(child):
                    continue
                if pending_elsewhere:
                    # Another agy session spawned in the same window has not claimed a conversation
                    # yet, so this new dir may be its own: answer "unknown", do not fail this resume.
                    continue
                return child
            except OSError:
                continue
        return None

    def _mark_agy_resume_failed(self, session: AgentSession) -> None:
        session.resumable = False
        session.resume_block_reason = AGY_RESUME_FAILURE_MESSAGE
        self.db.update_archive_fields(
            session.session_id, resumable=0, resume_block_reason=AGY_RESUME_FAILURE_MESSAGE
        )
        session.emit_virtual_ansi(f"\r\n\x1b[1;31m✖ {AGY_RESUME_FAILURE_MESSAGE}\x1b[0m\r\n")
        self.record_worker_event(
            session.session_id,
            {
                "type": "error",
                "category": "lifecycle",
                "status": "failed",
                "content": AGY_RESUME_FAILURE_MESSAGE,
            },
        )

    @classmethod
    def _tui_input_line(cls, screen_text: str, engine: str = "") -> Optional[str]:
        """W2/K2: the text on the engine's composer ("" when it is empty), or None when none is drawn.

        The real captures show three different composers:
          kimi   a framed box: "│ > …" on its first row, the wrapped rest of the text on the rows
                 below it, then "╰───╯" (12 rows tall for a task carrying the worker-bridge hint)
          omp    a bare ❯ row under a rule, with status lines below it
          codex  a bare › row (placeholder "Ask Codex to do anything") above its status line
        Looking only at the last non-empty line (the old code) always found a status line instead.
        K2 (fix 9): a box is located by its frame, so the whole text of a composer of any height is
        returned; the old bottom-rows search missed the "│ > " row of a tall box altogether.
        """
        lines = [ln.rstrip() for ln in str(screen_text or "").splitlines()]
        span = cls._tui_composer_span(lines, engine)
        return span[0] if span is not None else None

    @classmethod
    def _tui_composer_span(cls, lines: List[str], engine: str = "") -> Optional[tuple]:
        """K2 (fix 9): (text, first_row, last_row, wrapped_rows) of the composer, or None.

        first_row/last_row index `lines` and cover the composer's whole drawing (a box's borders
        included); wrapped_rows counts the text rows below its first one, which is how much taller
        it is than a one-line composer. The composer is searched from the bottom: its lowest row
        (a box's bottom border) must lie in the bottom TUI_PROMPT_SCAN_ROWS rows, as before, while
        the composer itself may reach any height above that.
        """
        eng = (engine or "").strip().lower()
        window = max(0, len(lines) - TUI_PROMPT_SCAN_ROWS)
        if TUI_COMPOSER_SHAPES.get(eng) == "box":
            framed = cls._tui_framed_box_composer(lines, window)
            if framed is not None:
                return framed
            # An early or synthetic screen whose box is not closed yet: the "│ > " row in the
            # bottom rows, plus the box rows that follow it.
            for i in range(len(lines) - 1, window - 1, -1):
                match = TUI_INPUT_BOX_RE.match(lines[i]) or TUI_INPUT_BOX_OPEN_RE.match(lines[i])
                if match:
                    last = i
                    while last + 1 < len(lines) and TUI_BOX_SIDE_RE.match(lines[last + 1]):
                        last += 1
                    parts = [match.group(1).rstrip()] + [
                        cls._tui_box_row_text(row) for row in lines[i + 1:last + 1]
                    ]
                    return " ".join(p for p in parts if p), i, last, last - i
            # Only the boxed composer is kimi's input row; a ❯ row above it is submitted history.
            # A synthetic/early screen may show only a bare ❯ row. Accept the LAST ❯ row when
            # every non-empty row below it is wrapped text (a long task wraps onto the following
            # screen rows); never accept it when chrome follows, because then the ❯ is restored
            # history in the middle of the transcript.
            for i in range(len(lines) - 1, window - 1, -1):
                if not lines[i].strip():
                    continue
                match = TUI_INPUT_GLYPH_RE.match(lines[i])
                if match is None:
                    # Possibly a wrapped continuation row of a ❯ above it: keep scanning upward.
                    continue
                below = [ln.strip() for ln in lines[i + 1:] if ln.strip()]
                if any(not cls._tui_plain_continuation(ln) for ln in below):
                    return None
                return match.group(1).rstrip(), i, i, 0
            return None
        for i in range(len(lines) - 1, window - 1, -1):
            match = TUI_INPUT_GLYPH_RE.match(lines[i])
            if match:
                return match.group(1).rstrip(), i, i, 0
        return None

    @classmethod
    def _tui_framed_box_composer(cls, lines: List[str], window: int) -> Optional[tuple]:
        """K2 (fix 9): the lowest closed box whose first row is "│ > …", read from its frame.

        Every bottom border inside the bottom rows is tried from the bottom up; a box whose first
        row is not a "│ > " row (kimi's Welcome box) is skipped. Its text rows are joined with one
        space: kimi wraps at spaces and drops the space it wrapped at.
        """
        for bottom in range(len(lines) - 1, window - 1, -1):
            if not TUI_BOX_BOTTOM_RE.match(lines[bottom]):
                continue
            top = bottom - 1
            while top >= 0 and TUI_BOX_SIDE_RE.match(lines[top]):
                top -= 1
            if top < 0 or top + 1 >= bottom or not TUI_BOX_TOP_RE.match(lines[top]):
                continue
            body = lines[top + 1:bottom]
            first = TUI_INPUT_BOX_RE.match(body[0]) or TUI_INPUT_BOX_OPEN_RE.match(body[0])
            if first is None:
                continue
            parts = [first.group(1).rstrip()] + [cls._tui_box_row_text(row) for row in body[1:]]
            return " ".join(p for p in parts if p), top, bottom, len(body) - 1
        return None

    @staticmethod
    def _tui_box_row_text(row: str) -> str:
        """K2 (fix 9): the text inside one box row, without its side bars and padding."""
        text = row.strip()
        if text[:1] in ("│", "┃", "|"):
            text = text[1:]
        if text[-1:] in ("│", "┃", "|"):
            text = text[:-1]
        return text.strip()

    @staticmethod
    def _tui_plain_continuation(line: str) -> bool:
        """True for a wrapped continuation row, False for chrome (response bullet, box, status)."""
        return not line.startswith((
            "●", "✗", "•", "╭", "╰", "│", "┃", "|", "Never Ask", "context:",
            "MCP server", "Working", "thinking", "? for shortcuts",
        ))

    @classmethod
    def _tui_prompt_visible(cls, screen_text: str, engine: str = "") -> bool:
        """V2(a)/W2: the engine's input row is on screen, so typing lands in its composer."""
        return cls._tui_input_line(screen_text, engine) is not None

    @classmethod
    def _tui_busy_indicator(cls, screen_text: str, engine: str = "") -> str:
        """K1: the startup/busy indicator currently on screen, or "" when the engine is settled.

        Only the bottom rows are read: a startup banner still visible far above the composer (kimi's
        welcome box, omp's MCP list) does not mean the engine is still starting, and a transcript row
        that merely quotes "Working…" must not hold readiness back forever.
        K2 (fix 9): the bottom rows are counted as if the composer were one line tall, so the rows
        right above a tall composer ("⠋ Preparing Kimi Code…" over kimi's 12-row box) are still
        read; the composer's own rows are not (they hold the typed text, not an indicator).
        """
        eng = (engine or "").strip().lower()
        lines = [ln.rstrip() for ln in str(screen_text or "").splitlines()]
        span = cls._tui_composer_span(lines, eng)
        wrapped = span[3] if span is not None else 0
        first = max(0, len(lines) - TUI_PROMPT_SCAN_ROWS - wrapped)
        rows = [
            ln for idx, ln in enumerate(lines)
            if idx >= first and (span is None or not span[1] <= idx <= span[2])
        ]
        for phrase in TUI_BUSY_PHRASES.get(eng, ()):
            if any(phrase in ln for ln in rows):
                return phrase
        line_re = TUI_BUSY_LINE_RES.get(eng)
        if line_re is not None:
            for ln in rows:
                if line_re.match(ln):
                    return "spinner"
        return ""

    @classmethod
    def _tui_ready(cls, screen_text: str, engine: str = "") -> tuple:
        """V2(a)/W2/K1: (ready, reason) for the composer AND against the startup indicators.

        K1 is the second half of readiness: every engine draws its composer before it accepts input,
        so a visible composer is necessary but never sufficient. The caller still owns the "screen
        did not change for TUI_READY_STABLE_SEC" half.
        """
        if cls._tui_input_line(screen_text, engine) is None:
            return False, "no_prompt"
        busy = cls._tui_busy_indicator(screen_text, engine)
        if busy:
            return False, f"busy:{busy}"
        return True, "stable_prompt"

    @staticmethod
    def _tui_agent_active(screen_text: str) -> bool:
        """W3/K2: the screen shows the agent working or answering, which confirms a submit."""
        return bool(TUI_AGENT_ACTIVE_RE.search(str(screen_text or "")))

    @classmethod
    def _tui_blocking_dialog(cls, screen_text: str, session: Any = None) -> str:
        """D3: the known trust/update dialog on screen ("" when there is none).

        A dialog the hub already registered as a structured interaction counts as known too: an
        Enter there would answer that dialog for the agent instead of submitting the task.
        """
        if session is not None and getattr(session, "pending_interaction", None):
            return "pending_interaction"
        text = str(screen_text or "")
        for phrase in TUI_TRUST_DIALOG_PHRASES + TUI_UPDATE_DIALOG_PHRASES:
            if phrase in text:
                return phrase
        return ""

    @staticmethod
    def _composer_holds(line: Optional[str], probe: str, tail: str) -> bool:
        """True when the located composer row still shows part of the typed text.

        A long task wraps: its START sits on the composer's first row, while a horizontally
        scrolled composer (codex) may instead show its END. Either proves the text is still there.
        K2 (fix 9): whitespace is ignored on both sides, because a wrapped composer's rows are joined
        with one space wherever the engine broke the line, which may not be where the text had one.
        """
        if line is None:
            return False
        flat = re.sub(r"\s+", "", line)
        for part in (probe, tail):
            key = re.sub(r"\s+", "", part or "")
            if key and key in flat:
                return True
        return False

    @staticmethod
    def _tui_submit_proof_rules(engine: str = "") -> Dict[str, Any]:
        """K3 (fix 9): the engine's submit-proof rules (TUI_SUBMIT_PROOF_RULES over the defaults)."""
        eng = (engine or "").strip().lower()
        return {**TUI_DEFAULT_SUBMIT_PROOF, **TUI_SUBMIT_PROOF_RULES.get(eng, {})}

    @staticmethod
    def _tui_reject_count(screen_text: str, rules: Dict[str, Any]) -> int:
        """K3 (fix 9): how many of the engine's refusals of an Enter are on screen."""
        text = str(screen_text or "")
        return sum(text.count(phrase) for phrase in rules.get("reject_phrases") or ())

    @classmethod
    def _tui_history_rows_holding(cls, screen_text: str, glyph: str, probe: str) -> int:
        """K3 (fix 9): submitted-message rows ("❯ …" for kimi) on screen holding the text's start."""
        if not glyph or not probe:
            return 0
        count = 0
        for row in str(screen_text or "").splitlines():
            row = row.strip()
            if row.startswith(glyph) and cls._composer_holds(row[len(glyph):], probe, ""):
                count += 1
        return count

    @classmethod
    def _tui_submit_marks(
        cls, session: Any, screen_text: str, engine: str, probe: str
    ) -> Dict[str, int]:
        """K3 (fix 9): the evidence already present before an Enter; only what the Enter adds counts.

        An "Error: LLM not set" line from an earlier refused Enter stays on kimi's screen, a resumed
        kimi redraws the old "❯ …" rows, and the transcript may already hold user messages.
        """
        rules = cls._tui_submit_proof_rules(engine)
        return {
            "rejects": cls._tui_reject_count(screen_text, rules),
            "history": cls._tui_history_rows_holding(screen_text, rules.get("history_glyph") or "", probe),
            "user_seq": int(getattr(session, "transcript_user_seq", 0) or 0),
        }

    def _tui_transcript_proof(
        self,
        session: Any,
        rules: Dict[str, Any],
        size_before: int,
        marks: Dict[str, Any],
        probe: str,
        tail: str,
    ) -> str:
        """K3 (fix 9): the transcript's proof of this submit ("" when it shows none yet)."""
        if rules.get("transcript") == "user_message":
            # The engine's transcript watcher (the only reader of the file) counts user messages;
            # a new one holding the typed text is the proof.
            seq = int(getattr(session, "transcript_user_seq", 0) or 0)
            if seq > int(marks.get("user_seq") or 0) and self._composer_holds(
                str(getattr(session, "transcript_user_text", "") or ""), probe, tail
            ):
                return "transcript_user_message"
            return ""
        if session.transcript_path:
            try:
                # transcript_grew also covers "the claimed conversation gained the user turn":
                # on a resume the conversation was claimed before the text was typed, so only
                # bytes appended after this Enter prove this turn arrived.
                if Path(session.transcript_path).stat().st_size > size_before:
                    return "transcript_grew"
            except OSError:
                pass
        return ""

    async def _confirm_tui_submit(
        self,
        session: AgentSession,
        probe: str,
        tail: str,
        size_before: int,
        engine: str = "",
        echoed: bool = True,
        deadline: Optional[float] = None,
        marks: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """V2(b)/W3/K2/K3: confirmation of one Enter, on positive evidence only.

        ("confirmed", reason) needs one of these proofs (per engine, TUI_SUBMIT_PROOF_RULES):
          (a) the text was SEEN on the composer after typing (echoed) and that composer is now
              located and no longer holds it -- an empty composer proves nothing on its own when
              the text was never seen there, which is exactly how round 3 reported success while
              the whole task still sat in kimi's box. K3 (fix 9): never for kimi, which clears
              its box when it refuses an Enter;
          (b) the transcript: it grew, or (kimi) its watcher read a new user message with the text;
          (c) K3 (fix 9): a new submitted-message row ("❯ …") holding the text; or
          (d) the engine's working indicator is on screen.
        ("rejected", phrase) K3 (fix 9): a new refusal (kimi "Error: LLM not set") is on screen.
        ("unsubmitted", reason) only when the text is POSITIVELY still on the located input row.
        ("unknown", reason) when nothing decides it yet. D3 then lets the caller press Enter once
        more, but only with no known trust/update dialog on screen (see _submit_text_to_tui).
        `marks` holds what was there before the Enter (_tui_submit_marks); the last screen read is
        written back into it, as the next Enter's starting point.
        """
        rules = self._tui_submit_proof_rules(engine)
        marks = marks if marks is not None else {}
        # K2: when the text was never seen on the composer, an empty composer proves nothing, so the
        # look keeps watching for ~15 s instead of deciding. A seen echo keeps the shorter W3 window,
        # except where a cleared composer is no proof at all (K3): then only the transcript, a history
        # row or activity can decide, and those take seconds to show (1.7-7 s for kimi's wire file).
        # The caller may pass a deadline, so retrying cannot stack one full window on top of another.
        long_window = (not echoed) or (not rules.get("cleared_composer"))
        window = TUI_SUBMIT_EVIDENCE_SEC if long_window else TUI_SUBMIT_CONFIRM_SEC
        started = time.time()
        limit = started + window
        if deadline is not None:
            limit = min(limit, deadline)
        last_line: Optional[str] = None
        while time.time() < limit:
            await asyncio.sleep(TUI_SUBMIT_CONFIRM_POLL_SEC)
            screen = session.get_clean_screen_text(max_lines=40)
            marks["screen"] = screen
            last_line = self._tui_input_line(screen, engine)
            if self._tui_reject_count(screen, rules) > int(marks.get("rejects") or 0):
                phrase = next((p for p in rules.get("reject_phrases") or () if p in screen), "rejected")
                return "rejected", phrase
            holds = self._composer_holds(last_line, probe, tail)
            if echoed and rules.get("cleared_composer") and last_line is not None and not holds:
                return "confirmed", "input_line_cleared"
            proof = self._tui_transcript_proof(session, rules, size_before, marks, probe, tail)
            if proof:
                return "confirmed", proof
            glyph = rules.get("history_glyph") or ""
            if glyph and self._tui_history_rows_holding(screen, glyph, probe) > int(marks.get("history") or 0):
                return "confirmed", "history_row"
            if self._tui_agent_active(screen):
                return "confirmed", "agent_active"
            if holds and (not echoed or (long_window and time.time() - started >= TUI_SUBMIT_CONFIRM_SEC)):
                # K2: the text the hub typed is visibly sitting on the composer while nothing
                # indicates work -- "seen still unsent", so the caller may press Enter again (the
                # first Enter was swallowed because the engine was still starting up). A text that
                # was seen before the Enter gets the W3 window first, as it always had.
                return "unsubmitted", "still_in_input_line"
        if last_line is None:
            screen = session.get_clean_screen_text(max_lines=40)
            marks["screen"] = screen
            last_line = self._tui_input_line(screen, engine)
        if self._composer_holds(last_line, probe, tail):
            return "unsubmitted", "still_in_input_line"
        return "unknown", "unconfirmed"

    async def _submit_text_to_tui(
        self,
        session: AgentSession,
        text: str,
        submit_key: str,
        prior: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """V2(b)(c)/W3/K2: type the text, press Enter, and accept the submit only on evidence.

        K2: the first look at the composer after typing is not allowed to decide anything on its own.
        `echoed` records whether the text was really SEEN on the located composer; an empty composer
        with echoed=False (kimi's box was still booting, then the screen redrew) proves nothing, so
        the check keeps watching for TUI_SUBMIT_EVIDENCE_SEC. If the text then appears on the
        composer with nothing indicating work, one more Enter is pressed (within the same retry
        budget).

        D3: when the evidence window ends with nothing proved (unknown), the hub presses Enter once
        more and watches TUI_SUBMIT_EXTRA_PRESS_SEC. That press counts toward
        TUI_SUBMIT_MAX_TRIES, is skipped entirely while a known trust/update dialog is on screen,
        and if the outcome is still unknown the result stays ok:null / "unconfirmed" with
        extra_press=true so last_injection shows that the extra press really happened.

        K3 (fix 9): when the engine refuses an Enter (kimi "Error: LLM not set") the text is gone
        from its composer. The result then carries retype=true while Enters remain: the caller waits
        for readiness again and calls this once more with `prior` (the earlier result), so the
        attempts, the one D3 extra press and the retype count all continue across the retype.
        """
        clean = str(text).replace("\r", " ").replace("\n", " ").strip()
        if not clean:
            return {"ok": False, "reason": "empty_text", "attempts": 0, "label": "失败"}
        size_before = 0
        if session.transcript_path:
            try:
                size_before = Path(session.transcript_path).stat().st_size
            except OSError:
                size_before = 0
        session.write_input(clean)
        await asyncio.sleep(TUI_TYPE_SETTLE_SEC)
        probe = clean[:30].strip()
        tail = clean[-30:].strip()
        engine = session.engine
        screen = session.get_clean_screen_text(max_lines=40)
        line = self._tui_input_line(screen, engine)
        echoed = self._composer_holds(line, probe, tail)
        prior = prior or {}
        attempts = int(prior.get("attempts") or 0)
        extra_press = bool(prior.get("extra_press"))
        retyped = int(prior.get("retyped") or 0)
        extra: Dict[str, Any] = {"retyped": retyped} if retyped else {}
        # K2: echoed is the FIRST look, right after typing, and it is what the result reports. The
        # later presses may additionally be judged by (a) once the text has really been seen sitting
        # on the composer -- that is what turns "the box looked empty while kimi was still booting"
        # into one more Enter and a real success instead of a success that was never earned.
        seen = echoed
        # K2: one evidence budget for the whole submit, not one per Enter.
        evidence_deadline = time.time() + TUI_SUBMIT_EVIDENCE_SEC
        # K3 (fix 9): the screen last read before each Enter is that Enter's starting point.
        marks: Dict[str, Any] = {"screen": screen}
        while attempts < TUI_SUBMIT_MAX_TRIES:
            attempts += 1
            marks.update(self._tui_submit_marks(session, marks.get("screen") or "", engine, probe))
            session.write_input(submit_key)
            state, reason = await self._confirm_tui_submit(
                session, probe, tail, size_before, engine, echoed=seen,
                deadline=evidence_deadline, marks=marks,
            )
            if state == "confirmed":
                return {"ok": True, "reason": reason, "attempts": attempts, "echoed": echoed,
                        "extra_press": extra_press, "label": "成功", **extra}
            if state == "rejected":
                # K3 (fix 9): the engine refused this Enter and cleared its composer; the text is
                # not on screen any more, so pressing Enter again would submit nothing.
                result = {"ok": False, "reason": "rejected_by_engine", "rejected": reason,
                          "attempts": attempts, "echoed": echoed, "extra_press": extra_press,
                          "label": "失败", **extra}
                if attempts < TUI_SUBMIT_MAX_TRIES:
                    result["retype"] = True
                return result
            if state == "unknown":
                # D3: nothing proved either way. Press Enter once more and watch a short window --
                # but never while a known trust/update dialog is on screen, because that Enter
                # would answer the dialog instead of submitting the task.
                marks["screen"] = session.get_clean_screen_text(max_lines=40)
                dialog = self._tui_blocking_dialog(marks["screen"], session)
                if extra_press or attempts >= TUI_SUBMIT_MAX_TRIES or dialog:
                    result = {"ok": None, "reason": "unconfirmed", "attempts": attempts,
                              "echoed": echoed, "extra_press": extra_press,
                              "dialog": dialog or None, "label": "未确认", **extra}
                    if extra_press:
                        # The extra press was made and the short D3 window still proved nothing.
                        result["detail"] = "checked_after_extra_press"
                    return result
                extra_press = True
                # The next loop iteration is that one extra Enter; it counts toward the attempt
                # limit and gets the short D3 window instead of the long evidence one.
                evidence_deadline = time.time() + TUI_SUBMIT_EXTRA_PRESS_SEC
                continue
            # "unsubmitted": the text is visibly still on the input row, so one more Enter is safe.
            # K2's one evidence budget for the whole submit stays in force here; only the D3 extra
            # press gets a window of its own.
            seen = True
            await asyncio.sleep(TUI_SUBMIT_RETRY_STEP_SEC * attempts)
        return {"ok": False, "reason": "text_still_in_input_line", "attempts": attempts,
                "echoed": echoed, "extra_press": extra_press, "label": "失败", **extra}

    @staticmethod
    def _tui_ready_banner(session: Any) -> str:
        """K1 (fix 9): the banner this startup waits for ("" for none): fresh spawns only.

        A resume is never gated on it (see TUI_READY_BANNERS): kimi -S draws no composer before its
        Welcome box, which then scrolls away within seconds.
        """
        if int(getattr(session, "resume_count", 0) or 0) > 0:
            return ""
        return TUI_READY_BANNERS.get(str(getattr(session, "engine", "") or "").strip().lower(), "")

    @staticmethod
    def _tui_banner_visible(session: Any, screen_text: str, banner: str) -> bool:
        """K1 (fix 9): the banner is on screen or in the recent scrollback (it may scroll off fast)."""
        if banner in str(screen_text or ""):
            return True
        scrollback = getattr(session, "get_full_scrollback_text", None)
        if scrollback is None:
            return False
        try:
            return banner in scrollback(max_lines=TUI_READY_BANNER_HISTORY_ROWS)
        except Exception:
            return False

    def _record_startup_injection(self, session: AgentSession, result: Dict[str, Any]) -> None:
        """W3: last_injection, the pane's red/grey line and the persisted row for one startup text."""
        session.last_injection = result
        if result.get("ok") is False:
            # W3: the red line is for a CONFIRMED failure only.
            session.emit_virtual_ansi(
                f"\r\n\x1b[1;31m✖ 启动文本注入失败（{result.get('reason')}，尝试 {result.get('attempts')} 次）\x1b[0m\r\n"
            )
        elif result.get("ok") is None:
            session.emit_virtual_ansi(
                "\r\n\x1b[90m… 启动文本注入未确认（unconfirmed）\x1b[0m\r\n"
            )
        try:
            self.persist_session(session)
        except Exception:
            pass

    async def _auto_handle_tui_startup_and_trust(
        self,
        session: AgentSession,
        task_to_inject: Optional[str],
        startup_delay: float = 2.5,
        submit_key: str = "\r",
    ):
        """R1/W4: one dialog watcher for every TUI engine for the whole startup window.

        R1 restored the pre-04dee6f behaviour: an engine with nothing to type (agy/codex/omp argv
        tasks) still watches the trust dialog instead of returning after 500 ms (agy then processed
        its -i prompt behind the dialog forever). W4: the same watcher runs during the readiness
        wait, so a dialog that appears later is answered, and the task text is never typed while a
        known dialog is on screen. W3: the injection outcome is recorded honestly in last_injection.

        K1 (fix 9): a fresh spawn of an engine with a ready banner (kimi's Welcome box) also waits
        for that banner; if it has not shown TUI_READY_BANNER_FALLBACK_SEC after the trust dialog
        was answered (or after spawn), the gate warns and falls back to composer + busy + stable.
        K3 (fix 9): an Enter the engine refused is followed by a new wait for readiness and the
        whole text typed again, within the same attempt limit.
        """
        start = time.time()
        deadline = start + TUI_STARTUP_WINDOW_SEC
        typed = not task_to_inject
        trust_dismissed = 0
        # The same dialog is often redrawn (resize, spinner); press Enter once per distinct
        # dialog text, not once per poll, so a slow redraw can never receive a second Enter.
        last_dialog: Optional[str] = None
        last_text: Optional[str] = None
        stable_since = time.time()
        ready: Dict[str, Any] = {"ready": False, "reason": "no_screen"}
        banner = "" if typed else self._tui_ready_banner(session)
        banner_anchor = start
        banner_seen_at: Optional[float] = None
        banner_fallback = False
        # K3 (fix 9): the refused submit that is waiting to be typed again, if any.
        rejected: Optional[Dict[str, Any]] = None
        retypes = 0

        while session.status in ("starting", "running"):
            await asyncio.sleep(TUI_DIALOG_POLL_SEC)
            if session.status not in ("starting", "running"):
                return
            now = time.time()
            screen_txt = session.get_clean_screen_text(max_lines=40)
            if any(phrase in screen_txt for phrase in TUI_TRUST_DIALOG_PHRASES):
                # R1/W4: answer it wherever it appears in the window; never type behind it.
                if screen_txt != last_dialog:
                    session.write_input("\r")
                    last_dialog = screen_txt
                    trust_dismissed += 1
                last_text = None
                stable_since = time.time()
                # K1 (fix 9): the banner's fallback clock starts when the last dialog is answered.
                banner_anchor = time.time()
                await asyncio.sleep(TUI_TRUST_CONFIRM_SETTLE_SEC)
                continue
            last_dialog = None
            if screen_txt != last_text:
                last_text = screen_txt
                stable_since = now
            input_line = self._tui_input_line(screen_txt, session.engine)
            stable = (now - stable_since) >= TUI_READY_STABLE_SEC
            prompted, prompt_reason = self._tui_ready(screen_txt, session.engine)
            if banner and banner_seen_at is None and not banner_fallback:
                if self._tui_banner_visible(session, screen_txt, banner):
                    banner_seen_at = now
                elif now >= min(banner_anchor + TUI_READY_BANNER_FALLBACK_SEC,
                                deadline - TUI_READY_BANNER_RESERVE_SEC):
                    # K1 (fix 9): an engine update may have changed the banner; do not hang the spawn.
                    banner_fallback = True
                    logger.warning(
                        "[%s] %r did not appear within %.0f s of startup; typing on the composer/busy/"
                        "stable gate instead", session.session_id, banner, now - banner_anchor,
                    )
            if not screen_txt.strip():
                ready = {"ready": False, "reason": "no_screen"}
            elif not prompted:
                # K1: a composer alone is not readiness. The engine draws it while still booting
                # ("⠋ Preparing Kimi Code…", "Connecting to MCP servers:", "Starting MCP servers")
                # and swallows the Enter typed then, so the reason names the indicator that is up.
                ready = {"ready": False, "reason": prompt_reason}
            elif banner and banner_seen_at is None and not banner_fallback:
                # K1 (fix 9): kimi's bare composer before its Welcome box is not ready either.
                ready = {"ready": False, "reason": "waiting_banner"}
            elif not stable:
                ready = {"ready": False, "reason": "waiting_stable"}
            else:
                ready = {"ready": True, "reason": "stable_prompt", "elapsed_sec": round(now - start, 1)}
            if banner:
                ready["banner"] = banner
                ready["banner_seen_sec"] = (
                    round(banner_seen_at - start, 1) if banner_seen_at is not None else None
                )
                if banner_fallback:
                    ready["banner_fallback"] = True

            if not typed and ready["ready"] and (now - start) >= startup_delay:
                prior = None if rejected is None else dict(rejected, retyped=retypes)
                result = await self._submit_text_to_tui(session, task_to_inject, submit_key, prior=prior)
                result["ready"] = ready
                result["trust_dismissed"] = trust_dismissed
                result["at"] = round(time.time(), 3)
                result["mode"] = "startup_task"
                if result.pop("retype", False):
                    # K3 (fix 9): the engine refused the Enter and cleared its box. Wait for the
                    # readiness gate again (a fresh stable period at least), then type it all anew.
                    rejected = result
                    retypes += 1
                    logger.warning(
                        "[%s] the engine refused the startup text (%s) after %s Enter(s); typing it "
                        "again once ready", session.session_id, result.get("rejected"),
                        result.get("attempts"),
                    )
                    last_text = None
                    stable_since = time.time()
                    continue
                self._record_startup_injection(session, result)
                # W4: the text is typed; the readiness wait it could have been lost in is over.
                return

            # R1: stop early only once the agent is clearly past the dialogs (prompt visible or
            # conversation claimed). With text still to type the ready branch above owns the exit.
            if typed and (input_line is not None or session.conversation_ref):
                return
            if now >= deadline:
                if not typed:
                    if rejected is not None:
                        # K3 (fix 9): refused, and never ready again for the retype: a real failure.
                        result = dict(rejected)
                        result.update({
                            "label": "失败", "at": round(time.time(), 3), "ready": ready,
                            "detail": "not_ready_again_before_deadline",
                        })
                        self._record_startup_injection(session, result)
                        return
                    self._record_startup_injection(session, {
                        "ok": None, "reason": "unconfirmed", "attempts": 0,
                        "mode": "startup_task", "at": round(time.time(), 3),
                        "label": "未确认", "ready": ready, "trust_dismissed": trust_dismissed,
                    })
                return

    def _crash_tail(self, sess: AgentSession) -> str:
        """The last ≤15 lines / ≤1500 chars of output, as the crash event carries them (spec §5)."""
        text = sess.last_screen or self._last_screen_text(sess)
        lines = [ln for ln in text.splitlines() if ln.strip()][-15:]
        tail = "\n".join(lines)
        if not tail:
            return "（无输出）"
        return tail[-1500:]

    def _exit_event_content(self, session_id: str, exit_code: int, sess: Optional[AgentSession]) -> str:
        resumable = bool(sess.resumable) if sess is not None else False
        hint = f"可调用 get_subagent_final_response('{session_id}') 获取持久化的最终输出与执行轨迹"
        if resumable:
            hint += f"；可调用 resume_subagent('{session_id}') 恢复该会话"
        if sess is not None and sess.end_reason == "crashed":
            return f"异常退出（退出码 {exit_code}）。最后输出：\n{self._crash_tail(sess)}\n{hint}。"
        return f"进程已退出（退出码 {exit_code}）。{hint}。"

    def _on_session_exit(self, session: Any, exit_code: int):
        """One exited process → at most one lifecycle event for its session id (A7).

        The callback carries the session object: after a resume replaced it, the old object's exit
        must not touch the new session's row, transcript claim or status. A string id is still
        accepted for an older caller.
        """
        obj = session if isinstance(session, AgentSession) else None
        session_id = obj.session_id if obj is not None else str(session)
        sess = self.sessions.get(session_id)
        if obj is not None and sess is not obj:
            if sess is None:
                rec = self.db.get_session_record(session_id)
                if rec and rec.get("archived_at"):
                    # close_session already wrote the final record and emitted its one event.
                    self._release_transcripts(session_id)
                    self._user_terminated.discard(session_id)
            # Either a resume already owns this id or the session is gone: this exit is history.
            return
        if session_id in self._user_terminated or (sess and sess.terminated_by_user):
            # V-5: the user termination already emitted its one lifecycle event.
            # Q12: the process is gone; let go of its transcript and of the one-event marker.
            self._release_transcripts(session_id)
            self._user_terminated.discard(session_id)
            return
        if sess is None:
            rec = self.db.get_session_record(session_id)
            if rec and rec.get("archived_at"):
                # close_session already wrote the final record and emitted its one event.
                self._release_transcripts(session_id)
                self._user_terminated.discard(session_id)
                return
        end_reason = "exited"
        if sess is not None:
            sess.exit_code = exit_code
            self._finalize_session(sess)
            # A7: the object is the live one, so its status follows its own end state (the PTY reader
            # normally did this already; doing it here keeps the callback self-contained).
            if sess.end_reason:
                sess.status = sess.end_reason
            # The final record writes the scrollback and the mailbox thread once, here (spec §3, A11).
            self.persist_session(sess, force_scrollback=True, final=True)
            end_reason = sess.end_reason or "exited"
        ev_type = "crashed" if end_reason == "crashed" else "exited"
        self.record_worker_event(
            session_id,
            {
                "type": ev_type,
                "category": "lifecycle",
                "status": end_reason,
                "content": self._exit_event_content(session_id, exit_code, sess),
            },
        )
        # Q12: the exit event is out. The process ended on its own, so its conversation is nobody's
        # live transcript any more and the one-lifecycle-event marker must not outlive it.
        self._release_transcripts(session_id)
        self._user_terminated.discard(session_id)

    async def _spawn_dsh_hub_job(self, session: AgentSession, task: Optional[str]):
        """Spawns a first-class in-host DSH session via http://127.0.0.1:3080/_dsh/dsh-crew/jobs."""
        if session.terminated_by_user:
            # Q4: the user terminated the session before this task got its first turn.
            return
        spec = session.dsh_spec
        provider = spec.get("provider", "clay")
        model = spec.get("model", "deepseek-flash")
        tier = spec.get("tier", "flash")
        effort = spec.get("effort", "high")

        banner = (
            f"\x1b[1;36m╭─── DeepSeek Harness (In-Host Session @ 127.0.0.1:3080) ──────────────────────╮\x1b[0m\r\n"
            f"\x1b[1;36m│\x1b[0m Hub Session : \x1b[1;33m{session.session_id:<62}\x1b[1;36m│\x1b[0m\r\n"
            f"\x1b[1;36m│\x1b[0m Model Route : \x1b[1;32m{provider} / {model} (tier={tier}, effort={effort}){'':<23}\x1b[1;36m│\x1b[0m\r\n"
            f"\x1b[1;36m│\x1b[0m Workspace   : {session.cwd[:62]:<62}\x1b[1;36m│\x1b[0m\r\n"
            f"\x1b[1;36m╰──────────────────────────────────────────────────────────────────────────────╯\x1b[0m\r\n"
        )
        session.emit_virtual_ansi(banner)

        if not task or not task.strip():
            session.emit_virtual_ansi(
                "\x1b[90m✦ Ready. Type a task below or call send_to_subagent to dispatch into DSH Web Host.\x1b[0m\r\n> "
            )
            self.persist_session(session)
            return

        token = str(uuid.uuid4())
        payload = {
            "task": task.strip(),
            "tier": tier,
            "effort": effort,
            "provider": provider,
            "model": model,
            "cwd": session.cwd,
            "messaging_token": token,
            "source": "mcp-agent-hub",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post("http://127.0.0.1:3080/_dsh/dsh-crew/jobs", json=payload)
                data = r.json()
                if not r.is_success or not data.get("ok"):
                    err_msg = data.get("error") or f"HTTP {r.status_code}"
                    if session.terminated_by_user:
                        # Q4: a failed spawn must not rewrite a session the user already terminated.
                        return
                    session.emit_virtual_ansi(f"\x1b[1;31m✖ DSH Spawn Error: {err_msg}\x1b[0m\r\n")
                    session.status = "exited"
                    self.persist_session(session)
                    return
                job = data["job"]
                job_id = job["id"]
                dsh_sid = job.get("sessionId", "")
                if session.terminated_by_user:
                    # Q4: the termination raced the spawn request. Clean up the job that was created,
                    # but leave the session's status, persistence and events exactly as terminate()
                    # left them.
                    threading.Thread(
                        target=lambda: httpx.post(
                            f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}/cancel", timeout=3.0
                        ),
                        daemon=True,
                    ).start()
                    return
                session.dsh_job_id = job_id
                session.dsh_session_id = dsh_sid
                session.dsh_token = token
                # Spec §3/§4: the dsh conversation reference is the job handle the continue API
                # takes; R5 also keeps the job's messaging token so /messages still works after a
                # hub restart.
                session.conversation_ref = {
                    "dsh_job_id": job_id,
                    "dsh_session_id": dsh_sid,
                    "dsh_token": token,
                }
                session.status = "running"
                session.current_activity = f"DSH Job {job_id} running in {dsh_sid}"
                session.emit_virtual_ansi(
                    f"\x1b[1;35m🚀 Spawned DSH Job:\x1b[0m \x1b[1m{job_id}\x1b[0m  "
                    f"\x1b[90m(DSH Web Session: {dsh_sid})\x1b[0m\r\n"
                    f"\x1b[90m❯ {task.strip()}\x1b[0m\r\n"
                )
                self.persist_session(session)
                await self._watch_dsh_hub_job(session, job_id, token)
        except Exception as e:
            if session.terminated_by_user:
                # Q4: a spawn that fails after the user terminated must not resurrect the session.
                return
            session.emit_virtual_ansi(f"\x1b[1;31m✖ Failed to reach DSH Host on 127.0.0.1:3080: {e}\x1b[0m\r\n")
            session.status = "exited"
            self.persist_session(session)

    async def _watch_dsh_hub_job(self, session: AgentSession, job_id: str, token: str):
        """
        Tails ~/.config/dsh-crew/logs/<job_id>.log for real-time steps/tools/thoughts AND
        polls /_dsh/dsh-crew/jobs/<job_id>/messages for bidirectional worker->parent messages & questions!
        """
        if session.terminated_by_user:
            # Q4: never attach, persist or emit for a session the user already terminated.
            return
        log_file = Path(os.path.expanduser("~")) / ".config" / "dsh-crew" / "logs" / f"{job_id}.log"
        session.transcript_path = log_file
        self.persist_session(session)
        log_offset = 0
        msg_cursor = 0

        def drain_log_lines():
            nonlocal log_offset
            if not log_file.exists():
                return
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(log_offset)
                    lines = f.readlines()
                    log_offset = f.tell()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    kind = ev.get("kind")
                    summary = ev.get("summary", "")
                    if kind == "step":
                        s_num = int(ev.get("step") or (session.step_count + 1))
                        t_num = ev.get("turn", 1)
                        session.step_count = max(session.step_count, s_num)
                        session.current_activity = f"Step {s_num} (Turn {t_num})"
                        session.emit_virtual_ansi(f"\r\n\x1b[1;36m▶ [Step {s_num} · Turn {t_num}]\x1b[0m\r\n")
                        self.persist_session(session)
                    elif kind == "tool_call":
                        tname = ev.get("tool") or summary or "dsh_tool"
                        self.record_telemetry(
                            session.session_id,
                            {
                                "source": "dsh_job_log",
                                "hook_event": "PreToolUse",
                                "tool_name": tname,
                                "tool_input": summary,
                            },
                        )
                        session.emit_virtual_ansi(f"\x1b[33m  ⚙ Tool Call: {tname}\x1b[0m\r\n")
                    elif kind == "tool_result":
                        self.record_telemetry(
                            session.session_id,
                            {
                                "source": "dsh_job_log",
                                "hook_event": "PostToolUse",
                                "tool_response": summary,
                            },
                        )
                        session.emit_virtual_ansi(f"\x1b[32m  ✓ {summary}\x1b[0m\r\n")
                    elif kind == "text" and summary:
                        self.record_telemetry(
                            session.session_id,
                            {
                                "source": "dsh_job_log",
                                "hook_event": "AssistantMessage",
                                "intermediate_messages": [summary],
                                "final_assistant_text": summary,
                            },
                        )
                        session.emit_virtual_ansi(f"\x1b[37m  💬 {summary}\x1b[0m\r\n")
                    elif kind == "error" and summary:
                        session.emit_virtual_ansi(f"\x1b[1;31m  ✖ {summary}\x1b[0m\r\n")
            except Exception:
                pass

        async with httpx.AsyncClient(timeout=10.0) as client:
            while (
                session.dsh_job_id == job_id
                and not session.terminated_by_user
                and session.status in ("starting", "running", "waiting_parent")
            ):
                drain_log_lines()

                # Poll DSH WorkerMailbox for worker->parent messages or blocking questions
                try:
                    m_resp = await client.get(
                        f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}/messages",
                        params={"after": msg_cursor, "wait": 1},
                        headers={"x-dsh-crew-job-token": token},
                    )
                    if session.terminated_by_user:
                        # Q4: the user terminated while this poll was in flight; stop before any
                        # status / persistence / event change.
                        return
                    if m_resp.is_success:
                        m_res = m_resp.json().get("result") or {}
                        for m in m_res.get("messages") or []:
                            seq = int(m.get("sequence", 0))
                            if seq > msg_cursor:
                                msg_cursor = seq
                            mkind = m.get("kind")
                            mtext = m.get("text", "")
                            mid = m.get("message_id") or f"dsh-msg-{seq}"
                            if mkind == "message":
                                if m.get("expects_reply"):
                                    # Parse options from DSH worker-question handoff format: `Options: A | B | C`
                                    parsed_opts: List[str] = []
                                    for mline in mtext.splitlines():
                                        if mline.strip().startswith("Options:"):
                                            raw_part = mline.split("Options:", 1)[1]
                                            parsed_opts = [x.strip() for x in raw_part.split("|") if x.strip()]
                                            break
                                    is_sec_approval = (
                                        "[Security mode]" in mtext
                                        or "Allow this call?" in mtext
                                        or ("Allow once" in parsed_opts and "Reject" in parsed_opts)
                                    )
                                    int_kind = "approval_request" if is_sec_approval else "question"
                                    int_title = "DSH Security Mode High-Risk Approval" if is_sec_approval else "DSH Worker Question"
                                    if not parsed_opts and is_sec_approval:
                                        parsed_opts = ["Allow once", "Allow for this session", "Reject"]
                                    session.emit_virtual_ansi(
                                        f"\r\n\x1b[1;35m{'🛡️' if is_sec_approval else '❓'} [DSH Worker {'Approval Request' if is_sec_approval else 'Asking Parent'} (request_id={mid})]:\x1b[0m\r\n"
                                        f"\x1b[35m   {mtext}\x1b[0m\r\n"
                                    )
                                    self.register_pending_interaction(
                                        session=session,
                                        kind=int_kind,
                                        title=int_title,
                                        prompt=mtext,
                                        options=parsed_opts,
                                        source="dsh_worker_question_handoff",
                                        request_id=mid,
                                        metadata={
                                            "dsh_job_id": job_id,
                                            "dsh_token": token,
                                            "dsh_message_id": mid,
                                        },
                                    )
                                else:
                                    session.emit_virtual_ansi(
                                        f"\r\n\x1b[1;34m📨 [DSH Worker Message to Parent]: {mtext}\x1b[0m\r\n"
                                    )
                                    self.record_worker_event(
                                        session.session_id,
                                        {
                                            "type": "message_to_parent",
                                            "category": "message",
                                            "status": "running",
                                            "content": mtext,
                                        },
                                    )
                except Exception:
                    pass

                # Check job status & final result
                try:
                    j_resp = await client.get(f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}", params={"wait": 0})
                    if session.terminated_by_user:
                        # Q4: same rule for the job-status poll.
                        return
                    if j_resp.is_success:
                        jview = j_resp.json().get("job") or {}
                        jstatus = jview.get("status")
                        if jview.get("step"):
                            session.step_count = max(session.step_count, int(jview["step"]))
                        if jstatus == "needs_input":
                            qs = jview.get("questions") or []
                            if qs and not session.pending_interaction:
                                q0 = qs[0] if isinstance(qs[0], dict) else {}
                                q_header = str(q0.get("header") or "DSH Input Required")
                                q_prompt = str(q0.get("question") or "DSH worker paused waiting for parent input")
                                if q0.get("detail"):
                                    q_prompt += f"\n{q0.get('detail')}"
                                raw_opts = q0.get("options") or []
                                opt_labels = [
                                    str(o.get("label") or o) if isinstance(o, dict) else str(o)
                                    for o in raw_opts
                                ]
                                is_sec = q_header == "Security mode" or "Allow this call?" in q_prompt
                                self.register_pending_interaction(
                                    session=session,
                                    kind="approval_request" if is_sec else "question",
                                    title=q_header,
                                    prompt=q_prompt,
                                    options=opt_labels,
                                    source="dsh_job_needs_input",
                                    request_id=f"dsh-ni-{job_id}",
                                    metadata={
                                        "dsh_job_id": job_id,
                                        "dsh_token": token,
                                        "dsh_needs_continue": True,
                                    },
                                )
                        elif jstatus in ("done", "failed", "cancelled", "timeout"):
                            if session.terminated_by_user:
                                # Q4: the job ended, but the user's termination is the final word on
                                # this session's status, persistence and events.
                                return
                            drain_log_lines()
                            full_res = (jview.get("result") or jview.get("error") or "").strip()
                            if full_res:
                                session.set_final_response(full_res, "dsh_hub_completion")
                                self.record_telemetry(
                                    session.session_id,
                                    {
                                        "source": "dsh_hub_api",
                                        "hook_event": "TurnComplete",
                                        "intermediate_messages": [full_res],
                                        "final_assistant_text": full_res,
                                    },
                                )
                            if jstatus == "done":
                                session.status = "completed"
                                session.end_reason = "exited"
                                session.exit_code = 0
                                session.completion_reported = True
                            else:
                                # Spec §2: a failed/cancelled/timeout DSH job is an abnormal end.
                                session.status = "crashed"
                                session.end_reason = "crashed"
                                session.exit_code = 1
                            session.conversation_ref = {
                                "dsh_job_id": job_id,
                                "dsh_session_id": session.dsh_session_id or "",
                            }
                            tokens_info = jview.get("tokens") or {}
                            session.current_activity = (
                                f"DSH {jstatus} (steps={session.step_count}, tools={jview.get('toolCalls', 0)}, "
                                f"tokens={tokens_info.get('input', 0)}/{tokens_info.get('output', 0)})"
                            )
                            formatted_res = full_res.replace("\r\n", "\n").replace("\n", "\r\n")
                            session.emit_virtual_ansi(
                                f"\r\n\x1b[1;32m╭─── DSH Turn Completed ({jstatus.upper()}) ────────────────────────────────────╮\x1b[0m\r\n"
                                f"{formatted_res}\r\n"
                                f"\x1b[1;32m╰──────────────────────────────────────────────────────────────────────────────╯\x1b[0m\r\n"
                                f"\x1b[90m(Session {session.dsh_session_id} stays warm in DSH Host — send input to continue)\x1b[0m\r\n> "
                            )
                            self.persist_session(session)
                            # V6: one feed entry per dsh turn. The record_telemetry(TurnComplete)
                            # above already emitted turn_completed with this result; the extra
                            # "completed" event duplicated it. "completed" is the worker's
                            # report_complete signal for a process that is done — a dsh job stays
                            # warm and can be continued, so it would also misstate the session.
                            # Only a result-less turn needs its own event.
                            if not full_res:
                                self.record_worker_event(
                                    session.session_id,
                                    {
                                        "type": "turn_completed",
                                        "category": "turn_completed",
                                        "status": "turn_idle",
                                        "content": f"DSH 任务 {jstatus} 结束",
                                    },
                                )
                            break
                except Exception:
                    pass

                await asyncio.sleep(0.4)

    def _build_worker_bridge_system_hint(self, session_id: str) -> str:
        # W10: the hub may run on an interpreter that has no <repo>/.venv (this worktree does not);
        # the hint must name the interpreter the hub itself runs on, or every bridge command fails.
        python_exe = str(Path(sys.executable).resolve()).replace("\\", "/")
        worker_cli = str((self.base_dir / "hub_worker.py").resolve()).replace("\\", "/")
        return (
            f"\n[Sub-Agent Bridge Info: session_id={session_id}]\n"
            f"You are running inside MCP Agent Hub. To communicate with the Parent Agent at any time, you can run:\n"
            f'- Report step: `{python_exe} "{worker_cli}" step "step description" --tool "tool_name"`\n'
            f'- Send message to parent: `{python_exe} "{worker_cli}" message "your progress message"`\n'
            f'- Ask parent & wait for answer: `{python_exe} "{worker_cli}" ask "your question"`\n'
            f'- Report task complete: `{python_exe} "{worker_cli}" complete "{COMPLETION_SUMMARY_PLACEHOLDER}"`'
            f' (replace the placeholder with your own one-sentence result)\n'
        )

    async def _watch_tui_interactive_prompts(self, session: AgentSession):
        """
        Secondary TUI screen fallback detector for high-risk approval modals and multi-choice questions
        across Codex, Kimi, OMP, and AGY (catches prompts that only render in the TUI without a separate JSONL event).
        """
        while session.status in ("starting", "running", "waiting_parent"):
            await asyncio.sleep(0.9)
            if session.pending_interaction is not None:
                continue
            scr = session.get_clean_screen_text(max_lines=38)
            if not scr.strip():
                continue

            eng = (session.engine or self._infer_engine(session.profile_id, {})).lower()

            # 1) Codex TUI approval modal ("Would you like to run the following command?" / "Would you like to make the following edits?")
            if eng == "codex" and (
                "Would you like to run the following command?" in scr
                or "Would you like to make the following edits?" in scr
            ):
                sig = f"codex_appr:{hash(scr.strip()[-400:])}"
                if sig not in session._seen_tui_prompt_signatures:
                    session._seen_tui_prompt_signatures.add(sig)
                    self.register_pending_interaction(
                        session=session,
                        kind="approval_request",
                        title="Codex TUI Approval Modal",
                        prompt=scr.strip()[-600:],
                        options=[
                            "Yes, proceed (y)",
                            "Yes, and don't ask again (a)",
                            "No, and tell Codex what to do differently (n)",
                        ],
                        source="codex_tui_screen_detector",
                    )
                continue

            # 2) AGY TUI permission prompt ("Run this command?" / "Allow access to" / "Yes, allow access")
            if eng == "agy" and (
                "Run this command?" in scr
                or ("Yes, allow" in scr and "No, deny" in scr)
            ):
                sig = f"agy_appr:{hash(scr.strip()[-400:])}"
                if sig not in session._seen_tui_prompt_signatures:
                    session._seen_tui_prompt_signatures.add(sig)
                    self.register_pending_interaction(
                        session=session,
                        kind="approval_request",
                        title="AGY TUI Permission Prompt",
                        prompt=scr.strip()[-600:],
                        options=["Yes, allow", "No, deny"],
                        source="agy_tui_screen_detector",
                    )
                continue

            # 2b) AGY TUI `ask_question` multi-choice selector ("Question 1/" or "Navigate · enter Select")
            if eng == "agy" and (
                "Question 1/" in scr
                or "Navigate · enter Select" in scr
                or "Write-in..." in scr
            ):
                sig = f"agy_q:{hash(scr.strip()[-350:])}"
                if sig not in session._seen_tui_prompt_signatures:
                    session._seen_tui_prompt_signatures.add(sig)
                    q_text = "AGY Sub-Agent Question"
                    opts: List[str] = []
                    for sline in scr.splitlines():
                        sclean = sline.strip()
                        if sclean.startswith("Question 1/") and ":" in sclean:
                            q_text = sclean.split(":", 1)[1].strip()
                        elif sclean.startswith("? ") and len(sclean) > 4:
                            q_text = sclean[2:].strip()
                        m_opt = re.match(r"^[>\s]*(\d+)\.\s+(.+)$", sclean)
                        if m_opt:
                            opt_lbl = m_opt.group(2).strip()
                            # Clean any pyte line-overlap artifacts from long prompt lines
                            for noise_tok in ("mini/antigravity", "complete:", "hub_worker.py"):
                                if noise_tok in opt_lbl:
                                    opt_lbl = opt_lbl.split(noise_tok)[0].strip()
                            opts.append(opt_lbl)
                    if not opts:
                        opts = ["Option 1", "Option 2", "Write-in..."]
                    self.register_pending_interaction(
                        session=session,
                        kind="question",
                        title="AGY TUI ask_question",
                        prompt=q_text,
                        options=opts,
                        source="agy_tui_screen_detector",
                        metadata={"raw_options_count": max(1, len(opts) - 1)},
                    )
                continue

            # 3) Kimi TUI ApprovalPanel fallback ("Approve once" + "Approve for this session")
            if eng == "kimi" and ("Approve once" in scr and "Approve for this session" in scr):
                sig = f"kimi_appr:{hash(scr.strip()[-400:])}"
                if sig not in session._seen_tui_prompt_signatures:
                    session._seen_tui_prompt_signatures.add(sig)
                    self.register_pending_interaction(
                        session=session,
                        kind="approval_request",
                        title="Kimi TUI Approval Panel",
                        prompt=scr.strip()[-600:],
                        options=[
                            "Approve once",
                            "Approve for this session",
                            "Reject",
                            "Reject with feedback",
                        ],
                        source="kimi_tui_screen_detector",
                    )
                continue

            # 4) OMP TUI CollabApproval / HookConfirm fallback ("Allow once" + "Always for this session" + "Deny")
            if eng == "omp" and ("Allow once" in scr and "Always for this session" in scr and "Deny" in scr):
                sig = f"omp_appr:{hash(scr.strip()[-400:])}"
                if sig not in session._seen_tui_prompt_signatures:
                    session._seen_tui_prompt_signatures.add(sig)
                    self.register_pending_interaction(
                        session=session,
                        kind="approval_request",
                        title="OMP TUI Tool Approval",
                        prompt=scr.strip()[-600:],
                        options=["Allow once", "Always for this session", "Deny"],
                        source="omp_tui_screen_detector",
                        metadata={"raw_options_count": 3},
                    )
                continue

    @staticmethod
    def _apply_approval_mode_args(engine: str, base_args: List[str], approval_mode: str) -> List[str]:
        """
        Adjusts CLI launch flags according to `approval_mode`:
        - `auto` (default): autonomous execution; interactive question tools (`ask`, `ask_user_question`, `request_user_input_async`) still delegate to Parent.
        - `delegate_to_parent` (or `ask`): high-risk commands/edits AND interactive questions pause and delegate approval to Parent Agent!
          * Kimi: replaces `--auto` (Never Ask) with `-y` (`--yolo` = Ask When Needed mode: routine commands run automatically, risky actions & `AskUserQuestion` ask!)
          * Codex: replaces `--dangerously-bypass-approvals-and-sandbox` with `-a on-request -s workspace-write`
          * AGY: replaces `--dangerously-skip-permissions` with `--mode accept-edits` (prompts before shell commands)
        - `strict_ask`: prompts for every command/edit (`codex -a untrusted`, `kimi` without `--auto`/`-y`, `agy` without `--dangerously-skip-permissions`).
        """
        mode = (approval_mode or "auto").strip().lower()
        args = list(base_args)
        eng = (engine or "").lower()
        if mode in ("auto", "default", ""):
            return args

        if eng == "kimi":
            args = [a for a in args if a not in ("--auto", "--yolo", "-y")]
            if mode in ("delegate_to_parent", "ask", "ask_high_risk"):
                args.insert(0, "-y")
        elif eng == "codex":
            args = [a for a in args if a != "--dangerously-bypass-approvals-and-sandbox"]
            if mode in ("delegate_to_parent", "ask", "ask_high_risk"):
                args = ["-a", "on-request", "-s", "workspace-write"] + args
            elif mode == "strict_ask":
                args = ["-a", "on-request", "-s", "read-only"] + args
        elif eng == "agy":
            args = [a for a in args if a != "--dangerously-skip-permissions"]
            if mode in ("delegate_to_parent", "ask", "ask_high_risk"):
                args = ["--mode", "accept-edits"] + args
        return args

    @staticmethod
    def _middle_level(levels) -> Optional[str]:
        """The middle of a level list, used when neither the profile nor the model names a default."""
        if not levels:
            return None
        return levels[len(levels) // 2]

    def hub_default_cwd(self) -> str:
        """B10: the hub's own working directory, absolute."""
        return os.path.abspath(os.getcwd())

    def resolve_launch(
        self,
        profile_id: str,
        model: Optional[str] = None,
        thinking_level: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> Dict[str, Any]:
        """B6: the one place that turns (profile, model, thinking, cwd) into a real command line.

        Returns exactly what will run; raises a Chinese ValueError for invalid input. Spawns nothing.
        """
        pid = (profile_id or '').strip()
        prof = self.profiles.get(pid)
        enabled_ids = [k for k, v in self.profiles.items() if v.get('enabled', True)]
        if not prof or not prof.get('enabled', True):
            raise ValueError(
                f"Profile「{pid}」不存在或已停用。可用：{', '.join(enabled_ids) or '无'}"
            )

        eng = str(prof.get('engine') or 'generic').strip().lower()
        name = str(prof.get('name') or pid)
        kind = str(prof.get('kind') or 'tui')
        command = str(prof.get('command') or '')
        base_args = [str(a) for a in (prof.get('args') or [])]
        notes = []  # Chinese notes, e.g. substitutions
        env: Dict[str, str] = {}

        options = self.engine_model_options(eng)
        explicit_model = str(model).strip() if model and str(model).strip() else None
        explicit_level = str(thinking_level).strip() if thinking_level and str(thinking_level).strip() else None

        resolved_model: Optional[str] = None
        model_source: Optional[str] = None
        resolved_level: Optional[str] = None
        thinking_source: Optional[str] = None
        cli_model: Optional[str] = None

        # R13: the profile's own disabled_models apply on top of the engine policy. For agy a disabled
        # suffixed id removes just that level from the base id; a disabled base removes the whole model.
        profile_disabled = {str(x) for x in (prof.get('disabled_models') or [])}
        enabled_options: List[Dict[str, Any]] = []
        for catalog_entry in options:
            if not catalog_entry.get('enabled'):
                continue
            entry_id = str(catalog_entry.get('id'))
            entry = dict(catalog_entry)
            if eng == 'agy':
                blocked_levels = set()
                blocked_entirely = False
                for blocked in profile_disabled:
                    blocked_base, blocked_level = split_agy_model_id(blocked)
                    if blocked_base != entry_id:
                        continue
                    if blocked_level:
                        blocked_levels.add(blocked_level)
                    else:
                        blocked_entirely = True
                if blocked_entirely:
                    continue
                remaining = [str(x) for x in (entry.get('thinking_levels') or []) if str(x) not in blocked_levels]
                if not remaining:
                    continue
                entry['thinking_levels'] = remaining
                if entry.get('default_thinking_level') not in remaining:
                    entry['default_thinking_level'] = None
            elif entry_id in profile_disabled:
                continue
            enabled_options.append(entry)

        option: Optional[Dict[str, Any]] = None
        if not options:
            if self.engines_catalog.get(eng) is not None and (
                prof.get('default_model') or prof.get('default_thinking_level') or explicit_model or explicit_level
            ):
                # R4: discovery found the engine but zero models (a discovery gap). Launching with the
                # profile's own defaults, unvalidated, beats silently injecting nothing.
                resolved_model = explicit_model or (str(prof.get('default_model') or '').strip() or None)
                resolved_level = explicit_level or (str(prof.get('default_thinking_level') or '').strip() or None)
                if eng == 'agy' and resolved_model:
                    base_model, suffix_level = split_agy_model_id(resolved_model)
                    resolved_model = base_model
                    if not resolved_level and suffix_level:
                        resolved_level = suffix_level
                    cli_model = f'{resolved_model}-{resolved_level}' if resolved_level else resolved_model
                model_source = 'explicit' if explicit_model else ('profile' if resolved_model else None)
                thinking_source = 'explicit' if explicit_level else ('profile' if resolved_level else None)
                notes.append(
                    '未能探测到模型列表，按启动时指定的模型与思考等级启动'
                    if (explicit_model or explicit_level)
                    else '未能探测到模型列表，按 Profile 默认值启动'
                )
            else:
                # pwsh and engines without a catalog skip model and thinking entirely.
                explicit_model = None
                explicit_level = None
        elif not enabled_options:
            # R4: the engine has models, but every one is disabled by policy or by this profile.
            raise ValueError(f'{eng} 的模型都已停用，请先在「引擎与模型」里启用一个。')
        else:
            enabled_option_ids = [str(m.get('id')) for m in enabled_options]
            by_id = {str(m.get('id')): m for m in enabled_options}
            if explicit_model:
                option = by_id.get(explicit_model)
                if option is None and eng == 'agy':
                    # agy also accepts a suffixed id: the suffix counts as explicit thinking,
                    # unless an explicit thinking_level was given too, in which case that wins.
                    agy_base, agy_suffix = split_agy_model_id(explicit_model)
                    option = by_id.get(agy_base)
                    if option is not None and agy_suffix and explicit_level is None:
                        explicit_level = agy_suffix
                if option is None:
                    raise ValueError(
                        f"模型「{explicit_model}」不可用（可能已停用或不存在）。可用："
                        f"{', '.join(enabled_option_ids) or '无'}"
                    )
                resolved_model = str(option.get('id'))
                model_source = 'explicit'
            else:
                default_model = str(prof.get('default_model') or '').strip() or None
                if default_model:
                    default_source = 'profile' if prof.get('default_model_source') != 'engine' else 'engine'
                    candidate = by_id.get(default_model)
                    if eng == 'agy' and candidate is None:
                        candidate = by_id.get(split_agy_model_id(default_model)[0])
                    if candidate is not None:
                        resolved_model = str(candidate.get('id'))
                        model_source = default_source
                        option = candidate
                if option is None and enabled_options:
                    option = enabled_options[0]
                    resolved_model = str(option.get('id'))
                    model_source = 'substituted'
                    notes.append(
                        f"默认模型 {default_model or '（未设置）'} 已停用，改用 {resolved_model}"
                    )

            if option is not None:
                levels = [str(x) for x in (option.get('thinking_levels') or [])]
                levels = self._sorted_levels_by_engine_order(eng, levels)
                if True:
                    if explicit_level:
                        if explicit_level not in levels:
                            raise ValueError(
                                f"{resolved_model} 支持的思考等级：{', '.join(levels) or '无'}"
                            )
                        resolved_level = explicit_level
                        thinking_source = 'explicit'
                    else:
                        profile_level = str(prof.get('default_thinking_level') or '').strip() or None
                        if profile_level and profile_level in levels:
                            resolved_level = profile_level
                            thinking_source = 'profile'
                        elif profile_level:
                            model_default = option.get('default_thinking_level')
                            if model_default in levels:
                                resolved_level = str(model_default)
                                thinking_source = 'model'
                            else:
                                resolved_level = self._middle_level(levels)
                                thinking_source = 'model' if resolved_level else None
                            notes.append(
                                f"Profile 默认思考等级 {profile_level} 不被 {resolved_model} 支持，改用 {resolved_level}"
                            )
                        else:
                            model_default = option.get('default_thinking_level')
                            if model_default and str(model_default) in levels:
                                resolved_level = str(model_default)
                                thinking_source = 'model'
                            elif levels:
                                resolved_level = self._middle_level(levels)
                                thinking_source = 'model'
                            else:
                                engine_levels = self._sorted_levels_by_engine_order(
                                    eng, [str(x) for x in ((self.engines_catalog.get(eng) or {}).get('available_thinking_levels') or [])]
                                )
                                resolved_level = engine_levels[0] if engine_levels else None
                                thinking_source = 'engine' if resolved_level else None

        # Rule 4: start from the stored args, strip injected flags (old files still work), inject at the front.
        args = strip_injected_flags(eng, base_args)

        if eng == 'omp':
            injected = []
            if resolved_model:
                injected.append(f'--model={resolved_model}')
            if resolved_level:
                injected.append(f'--thinking={resolved_level}')
            args = injected + args
        elif eng == 'kimi':
            if resolved_model:
                args = ['-m', resolved_model] + args
            if resolved_level:
                # Only this child process; never the hub's own environment.
                env['KIMI_MODEL_THINKING_EFFORT'] = resolved_level
        elif eng == 'agy':
            if resolved_model:
                if resolved_level:
                    cli_model = f'{resolved_model}-{resolved_level}'
                else:
                    cli_model = resolved_model
                args = ['--model', cli_model] + args
        elif eng == 'codex':
            injected = []
            if resolved_model:
                injected.extend(['-m', resolved_model])
            if resolved_level:
                injected.extend(['-c', f'model_reasoning_effort="{resolved_level}"'])
            args = injected + args
        # dsh injects no args at all: provider/model split and effort travel in the hub API payload.

        # Rule 5: cwd.
        if cwd is None or str(cwd).strip() == '':
            work_dir = self.hub_default_cwd()
        else:
            candidate_cwd = str(cwd).strip()
            expanded = os.path.expandvars(os.path.expanduser(candidate_cwd))
            resolved_cwd = os.path.abspath(expanded)
            if not os.path.isdir(resolved_cwd):
                raise ValueError(f'工作目录不存在：{candidate_cwd}')
            work_dir = resolved_cwd

        if eng == 'agy' and cli_model is None and resolved_model:
            cli_model = f'{resolved_model}-{resolved_level}' if resolved_level else resolved_model

        # V-3: dsh travels as an API payload, not as CLI args, so the preview must carry the same spec
        # the session will really use.
        dsh_spec: Optional[Dict[str, Any]] = None
        if eng == 'dsh':
            dsh_provider = 'clay'
            dsh_model_name = 'deepseek-flash'
            if resolved_model:
                if '/' in resolved_model:
                    dsh_provider, dsh_model_name = resolved_model.split('/', 1)
                else:
                    dsh_model_name = resolved_model
            dsh_spec = {
                'tier': str(prof.get('dsh_tier') or 'flash'),
                'provider': dsh_provider,
                'model': dsh_model_name,
                'effort': resolved_level or 'high',
            }

        return {
            'profile_id': pid,
            'profile_name': name,
            'engine': eng,
            'kind': kind,
            'command': command,
            'args': args,
            'env': env,
            'cwd': work_dir,
            'model': resolved_model,
            'model_source': model_source,
            'thinking_level': resolved_level,
            'thinking_source': thinking_source,
            'cli_model': cli_model,
            'dsh_spec': dsh_spec,
            'notes': notes,
        }

    async def create_session(
        self,
        profile_id: str,
        task: Optional[str] = None,
        cwd: Optional[str] = None,
        title: Optional[str] = None,
        hwnd: Optional[int] = None,
        model: Optional[str] = None,
        thinking_level: Optional[str] = None,
        approval_mode: str = "auto",
    ) -> AgentSession:
        loop = asyncio.get_running_loop()

        if profile_id == "desktop-gui" and hwnd:
            session_id = f"gui-{uuid.uuid4().hex[:8]}"
            sess = AgentSession(
                session_id=session_id,
                profile_id="desktop-gui",
                profile_name="Desktop GUI Window",
                kind="desktop_gui",
                title=title or f"HWND-{hwnd}",
                cwd=cwd or os.getcwd(),
                command="",
                args=[],
                hwnd=int(hwnd),
                on_exit_cb=self._on_session_exit,
                on_tui_tool_cb=self._on_tui_tool_detected,
            )
            sess.status = "running"
            sess.engine = "desktop_gui"
            # Spec §3: even a non-resumable session keeps the launch it ran with, so the archive can
            # say why it cannot be revived instead of "no launch recorded".
            sess.profile_snapshot = {
                "profile_id": "desktop-gui", "engine": "desktop_gui", "kind": "desktop_gui",
                "command": "", "args": [], "env": {}, "model": None, "thinking_level": None,
                "cli_model": None, "cwd": sess.cwd, "task_mode": "desktop_gui",
                "startup_delay_sec": 0, "submit_key": "\r", "dsh_spec": None,
            }
            self.sessions[session_id] = sess
            self.persist_session(sess)
            return sess

        launch = self.resolve_launch(profile_id, model=model, thinking_level=thinking_level, cwd=cwd)

        prof = self.profiles[profile_id]
        engine = launch['engine']
        # Q7: one reading of `kind` for the whole launch path; a stored null means tui, never None.
        kind = str(prof.get("kind") or "tui")
        work_dir = launch['cwd']
        effective_model = launch['model']
        effective_thinking = launch['thinking_level']
        args = list(launch['args'])
        env_overrides: Dict[str, str] = dict(launch.get('env') or {})
        if engine == 'agy' and launch.get('cli_model'):
            effective_model = launch['cli_model']

        # Q12: 8 hex characters, wide enough that a reused profile prefix never collides.
        session_id = f'{profile_id}-{uuid.uuid4().hex[:8]}'
        effective_approval_mode = (approval_mode or prof.get('approval_mode') or 'auto').strip().lower()
        args = self._apply_approval_mode_args(engine=engine, base_args=args, approval_mode=effective_approval_mode)
        # Archive spec §3: the resolved launch, as it will really run. The task itself is added below
        # by the task mode and is deliberately NOT part of the snapshot (a resume adds its own message).
        launch_args = list(args)
        task_mode = str(prof.get("task_mode") or "pty_input")
        # R1: everything that can raise runs before the PTY starts and before the session is stored,
        # so a bad profile value leaves no orphan session behind.
        try:
            startup_delay = float(prof.get("startup_delay_sec") or 2.5)
        except (TypeError, ValueError):
            raise ValueError(
                f"Profile「{profile_id}」的启动等待（startup_delay_sec）不是数字：{prof.get('startup_delay_sec')!r}"
            )
        if startup_delay < 0:
            startup_delay = 0.0
        submit_key = str(prof.get("submit_key") or "\r")
        omp_sess_dir: Optional[Path] = None

        if task_mode == "omp_positional":
            omp_sess_dir = self.runtime_configs_dir / f"omp_sess_{session_id}"
            omp_sess_dir.mkdir(exist_ok=True)
            args.extend([
                f"--session-dir={omp_sess_dir}",
                f"--append-system-prompt={self._build_worker_bridge_system_hint(session_id)}",
            ])
            if task:
                args.append(task)
        elif task_mode == "agy_interactive_flag":
            if task:
                args.extend(["-i", task + "\n" + self._build_worker_bridge_system_hint(session_id)])
        elif task_mode == "codex_positional":
            if task:
                args.append(task + "\n" + self._build_worker_bridge_system_hint(session_id))

        sess = AgentSession(
            session_id=session_id,
            profile_id=profile_id,
            profile_name=prof.get("name", profile_id),
            kind=kind,
            title=title or f"{prof.get('name', profile_id)} ({session_id})",
            cwd=work_dir,
            command=prof["command"],
            args=args,
            gui_url=prof.get("gui_url"),
            on_exit_cb=self._on_session_exit,
            on_tui_tool_cb=self._on_tui_tool_detected,
        )
        sess.effective_model = effective_model
        sess.effective_thinking_level = effective_thinking
        sess.engine = engine
        sess.approval_mode = effective_approval_mode
        sess.profile_snapshot = {
            "profile_id": profile_id,
            "engine": engine,
            "kind": kind,
            "command": prof["command"],
            "args": launch_args,
            "env": dict(launch.get("env") or {}),
            "model": launch.get("model"),
            "thinking_level": launch.get("thinking_level"),
            "cli_model": launch.get("cli_model"),
            "cwd": work_dir,
            "task_mode": task_mode,
            "startup_delay_sec": startup_delay,
            "submit_key": submit_key,
            "dsh_spec": dict(launch.get("dsh_spec") or {}) or None,
        }

        if kind == "web_gui":
            if task:
                sess.args = ["-NoLogo", "-NoProfile", "-Command", f"dsh headless {json.dumps(task)}; pwsh -NoLogo"]
            else:
                sess.args = ["-NoLogo", "-NoProfile"]
            # "args (as launched)": web_gui really runs the dsh headless command, not the profile args.
            sess.profile_snapshot["args"] = list(sess.args)

        if task:
            # B9: last_task is the initial task and is never overwritten afterwards.
            sess.last_task = task
            sess.current_activity = f"Running initial task: {task[:80]}"

        if task_mode == "dsh_hub_api":
            # B6 rule 4 / V-3: dsh injects no args. resolve_launch already built the dsh spec
            # (tier, provider/model split, effort), so the session and the preview use one value.
            sess.dsh_spec = dict(launch.get("dsh_spec") or {})
            sess.start_virtual(
                loop,
                on_virtual_line_cb=lambda sid, line: self.deliver_parent_message(sid, line, also_write_pty=False),
            )
            self.sessions[session_id] = sess
            self.persist_session(sess)
            asyncio.create_task(self._spawn_dsh_hub_job(sess, task))
            return sess

        sess.start_pty(loop, env_overrides=env_overrides or None)
        self.sessions[session_id] = sess
        self.persist_session(sess)

        if engine == "codex":
            asyncio.create_task(self._watch_codex_rollout(sess))
        elif omp_sess_dir is not None:
            asyncio.create_task(self._watch_omp_session(sess, omp_sess_dir))
        elif engine == "kimi":
            asyncio.create_task(self._watch_kimi_session(sess))
        elif engine == "agy":
            asyncio.create_task(self._watch_agy_transcript(sess))

        if kind == "tui":
            asyncio.create_task(self._watch_tui_interactive_prompts(sess))
            prompt_to_send: Optional[str] = None
            if task and task_mode == "pty_input":
                prompt_to_send = task
                if profile_id.startswith("kimi"):
                    if "hub_worker.py" not in task:
                        # Kimi has no system-prompt flag, so the full worker-bridge hint (session
                        # marker plus step / message / ask / complete) travels with the typed task,
                        # as it does in the task text for codex and agy. Newlines become spaces
                        # when typed. The marker inside it keeps transcript ownership unambiguous (Q2).
                        prompt_to_send = task + "\n" + self._build_worker_bridge_system_hint(sess.session_id)
                    else:
                        # The caller wrote its own bridge instructions; only add the session marker.
                        prompt_to_send = f"{task}\n\n[Sub-Agent Bridge Info: session_id={sess.session_id}]"
            asyncio.create_task(
                self._auto_handle_tui_startup_and_trust(
                    sess,
                    task_to_inject=prompt_to_send,
                    startup_delay=startup_delay,
                    submit_key=submit_key,
                )
            )

        return sess

    async def wait_for_output_settle(
        self,
        session: AgentSession,
        timeout_sec: float = 25.0,
        quiet_window_sec: float = 2.5,
        progress_cb: Optional[Callable[[float, str], Any]] = None,
    ) -> Dict[str, Any]:
        start = time.time()
        initial_output_at = session.last_output_at
        has_seen_new_output = False

        while (time.time() - start) < timeout_sec:
            await asyncio.sleep(0.5)
            now = time.time()
            elapsed = now - start

            if session.last_output_at > initial_output_at:
                has_seen_new_output = True

            if progress_cb and int(elapsed * 2) % 6 == 0:
                try:
                    res = progress_cb(elapsed, session.get_clean_screen_text(max_lines=15))
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

            if session.status in ("completed", "waiting_parent", "exited", "terminated"):
                self.persist_session(session)
                return {"settled": True, "reason": f"status_{session.status}", "elapsed_sec": round(elapsed, 1)}

            if has_seen_new_output and (now - session.last_output_at) >= quiet_window_sec and elapsed >= 3.0:
                try:
                    self.summarize_transcript(session.session_id)
                except Exception:
                    pass
                self.persist_session(session)
                return {"settled": True, "reason": "output_settled", "elapsed_sec": round(elapsed, 1)}

        self.persist_session(session)
        return {
            "settled": False,
            "reason": "timeout_still_running_in_console",
            "elapsed_sec": round(time.time() - start, 1),
        }
