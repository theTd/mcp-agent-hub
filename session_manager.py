import asyncio
import base64
import ctypes
from ctypes import wintypes
import datetime
import io
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import httpx
import pyte
from PIL import Image
from winpty import PtyProcess

from model_discovery import AgentModelDiscovery
from transcript_summarizer import TranscriptSummarizerRegistry, sanitize_tui_screen_text

# Win32 API constants for Desktop GUI window capture & off-screen tucking
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

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
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN transcript_path TEXT DEFAULT ''")
            except Exception:
                pass

    def get_max_seq(self) -> int:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM worker_events").fetchone()
            return int(row["m"]) if row else 0

    def upsert_session(self, s_dict: Dict[str, Any], scrollback_text: str = ""):
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, profile_id, profile_name, kind, title, cwd, status,
                    created_at, updated_at, ended_at, exit_code, step_count,
                    current_activity, last_task, final_response, final_response_source,
                    files_changed_json, scrollback_text, transcript_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    ended_at=COALESCE(excluded.ended_at, sessions.ended_at),
                    exit_code=COALESCE(excluded.exit_code, sessions.exit_code),
                    step_count=excluded.step_count,
                    current_activity=excluded.current_activity,
                    last_task=excluded.last_task,
                    final_response=COALESCE(excluded.final_response, sessions.final_response),
                    final_response_source=COALESCE(excluded.final_response_source, sessions.final_response_source),
                    files_changed_json=excluded.files_changed_json,
                    scrollback_text=CASE WHEN length(excluded.scrollback_text) > 0 THEN excluded.scrollback_text ELSE sessions.scrollback_text END,
                    transcript_path=CASE WHEN length(excluded.transcript_path) > 0 THEN excluded.transcript_path ELSE sessions.transcript_path END
                """,
                (
                    s_dict["session_id"],
                    s_dict["profile_id"],
                    s_dict["profile_name"],
                    s_dict["kind"],
                    s_dict["title"],
                    s_dict["cwd"],
                    s_dict["status"],
                    s_dict["created_at"],
                    time.time(),
                    s_dict.get("ended_at"),
                    s_dict.get("exit_code"),
                    s_dict.get("step_count", 0),
                    s_dict.get("current_activity", ""),
                    s_dict.get("last_task", ""),
                    s_dict.get("final_response"),
                    s_dict.get("final_response_source"),
                    json.dumps(s_dict.get("files_changed", []), ensure_ascii=False),
                    scrollback_text,
                    s_dict.get("transcript_path") or "",
                ),
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

    def get_session_record(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["files_changed"] = json.loads(d.pop("files_changed_json", "[]"))
            except Exception:
                d["files_changed"] = []
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

    def list_persisted_sessions(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT session_id, profile_id, profile_name, kind, title, cwd, status,
                       created_at, updated_at, ended_at, exit_code, step_count,
                       current_activity, last_task,
                       CASE WHEN final_response IS NOT NULL AND length(final_response) > 0 THEN 1 ELSE 0 END AS has_final_response,
                       final_response_source
                FROM sessions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]


# Priority levels for final_response sources so explicit MCP or native transcript never gets overwritten by raw screen text
FINAL_SOURCE_PRIORITY = {
    None: 0,
    "terminal_turn_snapshot": 1,
    "transcript_summarizer": 2,
    "native_transcript": 3,
    "codex_task_complete": 4,
    "dsh_hub_completion": 4,
    "report_complete_mcp": 5,
}

# Heuristic regex for detecting TUI tool calls on terminal lines when native hooks aren't present
TUI_TOOL_PATTERN = re.compile(
    r"^\s*[●•✦⚡▶]\s*(Bash|Read|Edit|Write|Grep|Glob|LS|WebSearch|Fetch|Task|MultiEdit|NotebookEdit|cmd|powershell)\s*(?:\((.+?)\)|:\s*(.+))?$",
    re.IGNORECASE,
)


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
        on_exit_cb: Optional[Callable[[str, int], None]] = None,
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
        self.last_output_at = time.time()
        self.ended_at: Optional[float] = None
        self.status = "starting"  # starting | running | waiting_parent | completed | exited | terminated
        self.exit_code: Optional[int] = None
        self.last_task: str = ""

        # Rich Telemetry & Lifecycle State
        self.step_count: int = 0
        self.current_activity: str = "Initializing session..."
        self.final_response: Optional[str] = None
        self.final_response_source: Optional[str] = None
        self.files_changed: List[str] = []
        self.pending_question_id: Optional[str] = None
        self.pending_interaction: Optional[Dict[str, Any]] = None
        self.engine: str = "generic"
        self.approval_mode: str = "auto"
        self.transcript_path: Optional[Path] = None
        self.effective_model: Optional[str] = None
        self.effective_thinking_level: Optional[str] = None
        self._seen_tui_tool_lines: Set[str] = set()
        self._seen_tui_prompt_signatures: Set[str] = set()

        # Headless VT100 terminal screen buffer WITH 3,000-line scrollback history!
        self.screen = pyte.HistoryScreen(cols, rows, history=3000)
        self.stream = pyte.Stream(self.screen)

        # Raw ANSI history buffer for Web Console xterm.js replay
        self.raw_history: List[str] = []
        self.raw_history_bytes = 0
        self.max_history_bytes = 512 * 1024

        # Connected WebSocket queues for live browser xterm.js streaming
        self.ws_queues: Set[asyncio.Queue] = set()
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
        cur_prio = FINAL_SOURCE_PRIORITY.get(self.final_response_source, 0)
        new_prio = FINAL_SOURCE_PRIORITY.get(source, 1)
        if new_prio >= cur_prio:
            self.final_response = text
            self.final_response_source = source

    def start_pty(self, loop: asyncio.AbstractEventLoop, env_overrides: Optional[Dict[str, str]] = None):
        self._loop = loop
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["HUB_SESSION_ID"] = self.session_id
        env["HUB_API_URL"] = os.environ.get("HUB_API_URL", "http://127.0.0.1:4444")
        if env_overrides:
            env.update(env_overrides)

        cmd_list = [self.command] + list(self.args)
        self.pty = PtyProcess.spawn(
            cmd_list,
            cwd=self.cwd,
            env=env,
            dimensions=(self.rows, self.cols),
        )
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
        if self.status not in ("completed", "terminated"):
            self.status = "exited"
        try:
            self.exit_code = self.pty.exitstatus if self.pty else 0
        except Exception:
            self.exit_code = 0

        # Snapshot sanitized terminal output into final_response only if no higher-priority response was recorded
        scrollback = self.get_full_scrollback_text(max_lines=80)
        clean_lines = sanitize_tui_screen_text(scrollback, max_lines=10)
        if clean_lines:
            self.set_final_response("\n".join(clean_lines), "terminal_turn_snapshot")

        self.current_activity = f"Session {self.status} (exit_code={self.exit_code})"

        if self.on_exit_cb and self._loop:
            self._loop.call_soon_threadsafe(self.on_exit_cb, self.session_id, self.exit_code or 0)

    def _on_pty_output(self, data: str):
        detected_tools = []
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
            self.raw_history_bytes += len(data)
            while self.raw_history_bytes > self.max_history_bytes and len(self.raw_history) > 1:
                removed = self.raw_history.pop(0)
                self.raw_history_bytes -= len(removed)

        if detected_tools and self.on_tui_tool_cb and self._loop:
            for t_name, t_arg in detected_tools:
                self._loop.call_soon_threadsafe(self.on_tui_tool_cb, self.session_id, t_name, t_arg)

        if self._loop and self.ws_queues:
            for q in list(self.ws_queues):
                self._loop.call_soon_threadsafe(self._push_ws_safe, q, data)

    @staticmethod
    def _push_ws_safe(q: asyncio.Queue, data: str):
        try:
            q.put_nowait(data)
        except Exception:
            pass

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

    def terminate(self):
        self.ended_at = time.time()
        scrollback = self.get_full_scrollback_text(max_lines=80)
        clean_lines = sanitize_tui_screen_text(scrollback, max_lines=10)
        if clean_lines:
            self.set_final_response("\n".join(clean_lines), "terminal_turn_snapshot")
        if self.pty and self.pty.isalive():
            try:
                self.pty.terminate(force=True)
            except Exception:
                pass
        if self.dsh_job_id and self.status in ("starting", "running", "waiting_parent"):
            job_id = self.dsh_job_id
            threading.Thread(
                target=lambda: httpx.post(f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}/cancel", timeout=3.0),
                daemon=True,
            ).start()
        if self.status != "completed":
            self.status = "terminated"
        self.current_activity = "Terminated by parent/user"

    def to_dict(self) -> Dict[str, Any]:
        alive = bool(self.pty and self.pty.isalive()) if self.pty else (self.status in ("running", "waiting_parent"))
        if not alive and self.pty is not None and self.status not in ("completed", "exited", "terminated"):
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
            "step_count": self.step_count,
            "current_activity": self.current_activity,
            "pending_question_id": self.pending_question_id,
            "pending_interaction": self.pending_interaction,
            "final_response": self.final_response,
            "final_response_source": self.final_response_source,
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
        }


class Win32WindowHelper:
    """Manages native Windows GUI Agent windows so they can be viewed & controlled inside the Web Console without cluttering the desktop."""

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

        # Bidirectional Mailbox & Sequenced Event Bus
        self.global_seq: int = self.db.get_max_seq()
        self.worker_events: List[Dict[str, Any]] = []
        self.parent_messages: Dict[str, List[Dict[str, Any]]] = {}
        self.pending_questions: Dict[str, Dict[str, Any]] = {}
        self._event_waiters: Set[asyncio.Event] = set()

        self.load_profiles()

    def _infer_engine(self, profile_id: str, prof: Dict[str, Any]) -> str:
        if prof.get("engine"):
            return str(prof["engine"]).lower()
        pid = profile_id.lower()
        for prefix in ("omp", "kimi", "agy", "codex", "dsh", "pwsh"):
            if pid.startswith(prefix):
                return prefix
        return "generic"

    def load_profiles(self, refresh_discovery: bool = False):
        discovery = self.model_discovery.discover_all(force_refresh=refresh_discovery)
        raw_engines = discovery.get("engines", {})
        data: Dict[str, Any] = {}
        if self.profiles_path.exists():
            try:
                data = json.loads(self.profiles_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}

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
            if not prof.get("default_model") and eng_info.get("default_model"):
                prof["default_model"] = eng_info["default_model"]
            if not prof.get("default_thinking_level") and eng_info.get("default_thinking_level"):
                prof["default_thinking_level"] = eng_info["default_thinking_level"]

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

        # Auto-discover and register Codex profiles from ~/.codex/config.toml & .codex-global-state.json
        codex_info = self.engines_catalog.get("codex") or {}
        codex_bin = codex_info.get("binary") or r"C:\Users\cui\AppData\Roaming\npm\codex.cmd"
        codex_all_models = codex_info.get("all_discovered_models", [])
        codex_disabled_models = codex_info.get("disabled_models", [])
        codex_avail_models = codex_info.get("available_models", [])
        codex_avail_levels = codex_info.get("available_thinking_levels", [])
        codex_model_details = codex_info.get("model_details", [])

        # 1) [profiles.<name>] in ~/.codex/config.toml
        for c_prof_name, c_prof_cfg in (codex_info.get("config_profiles") or {}).items():
            if not isinstance(c_prof_cfg, dict):
                continue
            synth_id = f"codex-{c_prof_name}"
            if synth_id not in enriched:
                c_mod = c_prof_cfg.get("model") or codex_info.get("default_model", "gpt-6.1-sol")
                c_eff = c_prof_cfg.get("model_reasoning_effort") or codex_info.get("default_thinking_level", "medium")
                enriched[synth_id] = {
                    "id": synth_id,
                    "name": f"Codex Profile: {c_prof_name} ({c_mod} [{c_eff}])",
                    "engine": "codex",
                    "enabled": c_mod not in codex_disabled_models,
                    "kind": "tui",
                    "command": codex_bin,
                    "args": ["-p", c_prof_name, "--dangerously-bypass-approvals-and-sandbox"],
                    "default_model": c_mod,
                    "default_thinking_level": c_eff,
                    "task_mode": "codex_positional",
                    "when_to_use": f"当需要使用 ~/.codex/config.toml 中预设的 [{c_prof_name}] 配置 ({c_mod}, 思考等级 {c_eff}) 执行代码工程任务时调用。",
                    "description": f"自动扫描自 ~/.codex/config.toml [profiles.{c_prof_name}] ({c_mod}, effort={c_eff})",
                    "startup_delay_sec": 2.5,
                    "submit_key": "\r",
                    "all_discovered_models": codex_all_models,
                    "disabled_models": codex_disabled_models,
                    "available_models": codex_avail_models,
                    "available_thinking_levels": codex_avail_levels,
                    "model_details": codex_model_details,
                }

        # 2) Recent model configurations from ~/.codex/.codex-global-state.json
        for rc in (codex_info.get("recent_configurations") or []):
            rc_mod = rc.get("model")
            rc_eff = rc.get("thinking_level") or "medium"
            if not rc_mod:
                continue
            synth_id = f"codex-{rc_mod}-{rc_eff}".lower().replace(".", "_")
            clean_id = f"codex-{rc_mod}-{rc_eff}"
            if clean_id not in enriched and synth_id not in enriched:
                enriched[clean_id] = {
                    "id": clean_id,
                    "name": f"Codex {rc_mod} ({rc_eff.capitalize()})",
                    "engine": "codex",
                    "enabled": rc_mod not in codex_disabled_models,
                    "kind": "tui",
                    "command": codex_bin,
                    "args": [
                        "-m",
                        rc_mod,
                        "-c",
                        f'model_reasoning_effort="{rc_eff}"',
                        "--dangerously-bypass-approvals-and-sandbox",
                    ],
                    "default_model": rc_mod,
                    "default_thinking_level": rc_eff,
                    "task_mode": "codex_positional",
                    "when_to_use": f"当需要使用 OpenAI Codex CLI 的 {rc_mod} 模型（思考等级 {rc_eff}）进行代码实现或调试时调用。",
                    "description": f"自动扫描自本机 Codex 最近使用配置 ({rc_mod}, reasoning_effort={rc_eff})",
                    "startup_delay_sec": 2.5,
                    "submit_key": "\r",
                    "all_discovered_models": codex_all_models,
                    "disabled_models": codex_disabled_models,
                    "available_models": codex_avail_models,
                    "available_thinking_levels": codex_avail_levels,
                    "model_details": codex_model_details,
                }

        self.profiles = enriched

    def get_enabled_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Returns only enabled profiles where the default model is not disabled (or dynamic base profiles)."""
        result: Dict[str, Dict[str, Any]] = {}
        for pid, p in self.profiles.items():
            if not p.get("enabled", True):
                continue
            result[pid] = p
        return result

    def build_routing_guide_markdown(self) -> str:
        """Compiles user-authored `when_to_use` annotations and model enable/disable policies into a structured guide for the Parent Agent."""
        lines: List[str] = [
            "# 🧭 Sub-Agent 调用与路由指南 (User-Configured Routing Guide)",
            self.global_routing_instructions.strip(),
            "",
            "## 1. 各 Sub-Agent Profile 调用场景注释 (`when_to_use`)",
        ]
        for pid, p in self.profiles.items():
            if not p.get("enabled", True):
                continue
            name = p.get("name", pid)
            eng = p.get("engine", "generic")
            def_mod = p.get("default_model") or "N/A"
            def_thk = p.get("default_thinking_level") or "N/A"
            when = (p.get("when_to_use") or p.get("description") or "").strip()
            lines.append(
                f"- **`{pid}`** ({name} | engine=`{eng}`, default_model=`{def_mod}`, thinking=`{def_thk}`):\n"
                f"  - 📌 **何时调用**: {when}"
            )
        lines.append("\n## 2. 各 Agent 引擎当前已启用模型 (`available_models`) 与已禁用模型 (`disabled_models`)")
        for eng, info in self.engines_catalog.items():
            avail = info.get("available_models", [])
            dis = info.get("disabled_models", [])
            thk = info.get("available_thinking_levels", [])
            dis_str = f" | 🚫 已禁用(禁止调用): `{', '.join(dis)}`" if dis else ""
            lines.append(
                f"- **`{eng}`**: ✅ 可用模型: `{', '.join(avail) or '无'}` | 🧠 思考等级: `{', '.join(thk) or '无'}`{dis_str}"
            )
        return "\n".join(lines)

    def toggle_engine_model(
        self,
        engine: str,
        model_id: str,
        enabled: bool,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enables or disables a specific model for an Agent engine and persists to agent_profiles.json."""
        eng = engine.strip().lower()
        mid = model_id.strip()
        if not eng or not mid:
            raise ValueError("Both 'engine' and 'model_id' are required.")

        raw_data: Dict[str, Any] = {"profiles": {}}
        if self.profiles_path.exists():
            try:
                raw_data = json.loads(self.profiles_path.read_text(encoding="utf-8"))
            except Exception:
                raw_data = {"profiles": {}}

        policies = raw_data.setdefault("engine_policies", {})
        eng_pol = policies.setdefault(eng, {"disabled_models": [], "model_notes": {}})
        disabled_list: List[str] = list(eng_pol.get("disabled_models") or [])

        if enabled:
            disabled_list = [m for m in disabled_list if m != mid]
        else:
            if mid not in disabled_list:
                disabled_list.append(mid)
        eng_pol["disabled_models"] = disabled_list

        if note is not None:
            notes_map = eng_pol.setdefault("model_notes", {})
            if note.strip():
                notes_map[mid] = note.strip()
            else:
                notes_map.pop(mid, None)

        self.profiles_path.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.load_profiles(refresh_discovery=False)
        return self.engines_catalog.get(eng, {})

    def save_global_routing_instructions(self, instructions: str) -> str:
        """Persists global routing instructions for Parent Agents into agent_profiles.json."""
        raw_data: Dict[str, Any] = {"profiles": {}}
        if self.profiles_path.exists():
            try:
                raw_data = json.loads(self.profiles_path.read_text(encoding="utf-8"))
            except Exception:
                raw_data = {"profiles": {}}
        raw_data["global_routing_instructions"] = instructions.strip()
        self.profiles_path.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.load_profiles(refresh_discovery=False)
        return self.global_routing_instructions

    def save_or_update_profile(self, profile_data: Dict[str, Any]) -> Dict[str, Any]:
        """Persists a new or modified Agent Profile to agent_profiles.json and reloads enriched profiles."""
        pid = (profile_data.get("id") or "").strip()
        if not pid:
            raise ValueError("Profile 'id' is required.")
        raw_data = {"profiles": {}}
        if self.profiles_path.exists():
            try:
                raw_data = json.loads(self.profiles_path.read_text(encoding="utf-8"))
            except Exception:
                raw_data = {"profiles": {}}
        profiles_map = raw_data.setdefault("profiles", {})
        existing = dict(profiles_map.get(pid) or {})
        # Strip transient auto-discovered arrays before writing to disk
        clean_entry = dict(existing)
        for k, v in profile_data.items():
            if k in ("available_models", "available_thinking_levels", "model_details", "all_discovered_models"):
                continue
            clean_entry[k] = v
        clean_entry["id"] = pid
        clean_entry.setdefault("enabled", True)
        clean_entry.setdefault("when_to_use", "")
        clean_entry.setdefault("kind", "tui")
        clean_entry.setdefault("submit_key", "\r")
        clean_entry.setdefault("startup_delay_sec", 2.5)
        profiles_map[pid] = clean_entry
        self.profiles_path.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.load_profiles()
        return self.profiles[pid]

    def delete_profile(self, profile_id: str) -> bool:
        """Deletes a profile from agent_profiles.json."""
        if not self.profiles_path.exists():
            return False
        raw_data = json.loads(self.profiles_path.read_text(encoding="utf-8"))
        profiles_map = raw_data.get("profiles", {})
        if profile_id not in profiles_map:
            return False
        del profiles_map[profile_id]
        self.profiles_path.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.load_profiles()
        return True

    def _notify_waiters(self):
        for ev in list(self._event_waiters):
            ev.set()

    def persist_session(self, session: AgentSession):
        scrollback = session.get_full_scrollback_text(max_lines=250)
        self.db.upsert_session(session.to_dict(), scrollback_text=scrollback)

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

        # Upgrade session's final_response if transcript summarizer found a clean conclusion
        if sess and summary.get("latest_conclusion"):
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
            "files_changed": meta.get("files_changed", []),
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

    def deliver_parent_message(
        self,
        session_id: str,
        text: str,
        request_id: Optional[str] = None,
        also_write_pty: bool = True,
        press_enter: bool = True,
    ) -> Dict[str, Any]:
        sess = self.sessions.get(session_id)
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
        if sess and sess.dsh_spec and not resolved_question_id and not dsh_replied:
            clean_txt = text.strip()
            if clean_txt:
                try:
                    loop = asyncio.get_running_loop()
                    if sess.dsh_job_id and sess.status == "running":
                        httpx.post(
                            f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{sess.dsh_job_id}/messages",
                            headers={"x-dsh-crew-job-token": sess.dsh_token or ""},
                            json={"message": clean_txt},
                            timeout=5.0,
                        )
                        sess.emit_virtual_ansi(f"\r\n\x1b[1;36m➤ [Parent Steer Injected into DSH]: {clean_txt}\x1b[0m\r\n")
                        pty_written = True
                    elif sess.dsh_job_id and sess.status in ("completed", "exited"):
                        new_token = str(uuid.uuid4())
                        r = httpx.post(
                            f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{sess.dsh_job_id}/continue",
                            json={"text": clean_txt, "messaging_token": new_token},
                            timeout=10.0,
                        )
                        cjob = r.json().get("job", {})
                        if cjob.get("id"):
                            sess.dsh_job_id = cjob["id"]
                            sess.dsh_token = new_token
                            sess.status = "running"
                            sess.last_task = clean_txt
                            sess.current_activity = f"Continuing DSH session ({cjob['id']}): {clean_txt[:60]}"
                            sess.emit_virtual_ansi(
                                f"\r\n\x1b[1;36m↻ [Continuing DSH Session {sess.dsh_session_id} -> {cjob['id']}]: {clean_txt}\x1b[0m\r\n"
                            )
                            loop.create_task(self._watch_dsh_hub_job(sess, cjob["id"], new_token))
                            pty_written = True
                    elif not sess.dsh_job_id:
                        loop.create_task(self._spawn_dsh_hub_job(sess, clean_txt))
                        pty_written = True
                except Exception as e:
                    sess.emit_virtual_ansi(f"\r\n\x1b[31m✖ DSH message delivery error: {e}\x1b[0m\r\n")
        elif sess and also_write_pty and not resolved_question_id:
            if sess.kind == "desktop_gui" and sess.hwnd:
                payload = text + ("\r" if press_enter and not text.endswith("\r") else "")
                pty_written = Win32WindowHelper.send_text(sess.hwnd, payload)
            else:
                clean_txt = text.rstrip("\r\n") if press_enter else text
                pty_written = sess.write_input(clean_txt)
                if press_enter:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.call_later(0.25, sess.write_input, "\r")
                    except Exception:
                        sess.write_input("\r")

        if sess:
            self.persist_session(sess)

        return {
            "delivered": True,
            "session_id": session_id,
            "resolved_question_id": resolved_question_id,
            "pty_written": pty_written,
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
        python_exe = self.base_dir / ".venv" / "Scripts" / "python.exe"
        poll_script = self.base_dir / "hub_poll.py"
        return f'& "{python_exe}" "{poll_script}" --session {session_id} --after-seq {after_seq} --timeout {timeout}'

    def _build_session_mcp_config(self, session_id: str) -> Path:
        python_exe = str((self.base_dir / ".venv" / "Scripts" / "python.exe").resolve())
        bridge_script = str((self.base_dir / "worker_bridge_mcp.py").resolve())
        cfg = {
            "mcpServers": {
                "parent_bridge": {
                    "command": python_exe,
                    "args": [bridge_script],
                    "env": {
                        "HUB_SESSION_ID": session_id,
                        "HUB_API_URL": "http://127.0.0.1:4444",
                    },
                }
            }
        }
        cfg_path = self.runtime_configs_dir / f"worker-mcp-{session_id}.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg_path

    async def _watch_codex_rollout(self, session: AgentSession):
        """Live tails C:\\Users\\cui\\.codex\\sessions\\YYYY\\MM\\DD\\rollout-*.jsonl for a spawned Codex session."""
        codex_root = Path(os.path.expanduser("~")) / ".codex" / "sessions"
        if not codex_root.exists():
            return

        target_file: Optional[Path] = None
        seen_before = set(codex_root.rglob("rollout-*.jsonl"))
        start_wait = time.time()

        while session.status in ("starting", "running", "waiting_parent") and (time.time() - start_wait) < 30.0:
            await asyncio.sleep(1.0)
            now_files = set(codex_root.rglob("rollout-*.jsonl"))
            new_files = sorted(now_files - seen_before, key=lambda p: p.stat().st_mtime, reverse=True)
            if new_files:
                target_file = new_files[0]
                break

        if not target_file:
            return
        session.transcript_path = target_file
        self.persist_session(session)

        offset = 0
        while session.status in ("starting", "running", "waiting_parent"):
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

    async def _watch_omp_session(self, session: AgentSession, omp_session_dir: Path):
        """Live tails OMP's dedicated session JSONL file inside omp_session_dir for tool calls, thoughts, and final responses."""
        target_file: Optional[Path] = None
        start_wait = time.time()
        while session.status in ("starting", "running", "waiting_parent") and (time.time() - start_wait) < 40.0:
            await asyncio.sleep(0.8)
            files = sorted(omp_session_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                target_file = files[0]
                break
        if not target_file:
            return
        session.transcript_path = target_file
        self.persist_session(session)

        offset = 0
        while session.status in ("starting", "running", "waiting_parent"):
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

    async def _watch_kimi_session(self, session: AgentSession):
        """Live tails Kimi Code CLI's wire.jsonl for toolCalls, assistant thoughts/messages, and agent.turn.ended."""
        kimi_root = Path(os.path.expanduser("~")) / ".kimi-code" / "sessions"
        if not kimi_root.exists():
            return

        target_file: Optional[Path] = None
        seen_before = {p: p.stat().st_mtime for p in kimi_root.rglob("wire.jsonl") if p.exists()}
        start_wait = time.time()

        while session.status in ("starting", "running", "waiting_parent") and (time.time() - start_wait) < 90.0:
            await asyncio.sleep(1.0)
            # First try exact match via Kimi TUI banner: Session: session_<uuid>
            scr_txt = session.get_clean_screen_text(max_lines=35)
            m = re.search(r"(session_[0-9a-f\-]{20,})", scr_txt)
            if m:
                kimi_sid = m.group(1)
                exact = list(kimi_root.rglob(f"{kimi_sid}/agents/main/wire.jsonl"))
                if exact and exact[0].exists():
                    target_file = exact[0]
                    break

            candidates = []
            for p in kimi_root.rglob("wire.jsonl"):
                try:
                    mtime = p.stat().st_mtime
                    if p not in seen_before or mtime > seen_before[p]:
                        candidates.append((mtime, p))
                except Exception:
                    pass
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                target_file = candidates[0][1]
                break

        if not target_file:
            return
        session.transcript_path = target_file
        self.persist_session(session)

        offset = 0
        latest_assistant_text: Optional[str] = None
        while session.status in ("starting", "running", "waiting_parent"):
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

    async def _watch_agy_transcript(self, session: AgentSession):
        """Live tails AGY CLI's transcript.jsonl under ~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl."""
        brain_root = Path(os.path.expanduser("~")) / ".gemini" / "antigravity" / "brain"
        if not brain_root.exists():
            return

        seen_before = {p: p.stat().st_mtime for p in brain_root.rglob("transcript.jsonl") if p.exists()}
        target_file: Optional[Path] = None
        start_wait = time.time()

        while session.status in ("starting", "running", "waiting_parent") and (time.time() - start_wait) < 40.0:
            await asyncio.sleep(1.0)
            candidates = []
            for p in brain_root.rglob("transcript.jsonl"):
                try:
                    # Ignore our own current conversation transcript if any
                    if "ed53e67b-76e2-42ec-a195-24c89a9d4e5d" in str(p):
                        continue
                    mtime = p.stat().st_mtime
                    if p not in seen_before or mtime > seen_before[p]:
                        candidates.append((mtime, p))
                except Exception:
                    pass
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                target_file = candidates[0][1]
                break

        if not target_file:
            return
        session.transcript_path = target_file
        self.persist_session(session)

        offset = 0
        while session.status in ("starting", "running", "waiting_parent"):
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
            except Exception:
                pass
            await asyncio.sleep(0.8)

    async def _auto_handle_tui_startup_and_trust(
        self,
        session: AgentSession,
        task_to_inject: Optional[str],
        startup_delay: float = 2.5,
        submit_key: str = "\r",
    ):
        """
        Watches a newly spawned TUI session for interactive 'Trust this folder?' dialogs (Kimi, AGY, Claude)
        and automatically presses Enter to confirm trust, then cleanly injects any pty_input task!
        """
        trust_phrases = (
            "Trust this folder?",
            "Trust and continue",
            "Do you trust the contents of this project?",
            "Yes, I trust this folder",
            "Do you trust the files in this folder?",
        )
        start = time.time()
        trust_dismissed = False
        task_sent = False if task_to_inject else True
        is_kimi = session.profile_id.startswith("kimi")

        while session.status in ("starting", "running") and (time.time() - start) < 30.0:
            await asyncio.sleep(0.5)
            screen_txt = session.get_clean_screen_text(max_lines=35)

            if not trust_dismissed and any(p in screen_txt for p in trust_phrases):
                trust_dismissed = True
                session.write_input("\r")
                await asyncio.sleep(2.2)
                continue

            if not task_sent and (time.time() - start) >= startup_delay:
                if any(p in screen_txt for p in trust_phrases):
                    session.write_input("\r")
                    await asyncio.sleep(2.0)
                    continue
                # For Kimi TUI, wait until the 'Welcome to Kimi Code!' composer banner is actually rendered
                if is_kimi and "Welcome to Kimi Code!" not in screen_txt and (time.time() - start) < 16.0:
                    continue
                if is_kimi:
                    await asyncio.sleep(0.6)
                task_sent = True
                clean_task = task_to_inject.replace("\r", " ").replace("\n", " ").strip()
                session.write_input(clean_task)
                await asyncio.sleep(0.8)
                session.write_input(submit_key)

            if trust_dismissed and task_sent:
                break

    def _on_session_exit(self, session_id: str, exit_code: int):
        sess = self.sessions.get(session_id)
        if sess:
            self.persist_session(sess)
        self.record_worker_event(
            session_id,
            {
                "type": "exited",
                "category": "lifecycle",
                "status": "exited",
                "content": (
                    f"Sub-Agent 进程已退出 (exit_code={exit_code})。"
                    f"可调用 get_subagent_final_response('{session_id}') 获取持久化的最终输出与执行轨迹。"
                ),
            },
        )

    async def _spawn_dsh_hub_job(self, session: AgentSession, task: Optional[str]):
        """Spawns a first-class in-host DSH session via http://127.0.0.1:3080/_dsh/dsh-crew/jobs."""
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
                    session.emit_virtual_ansi(f"\x1b[1;31m✖ DSH Spawn Error: {err_msg}\x1b[0m\r\n")
                    session.status = "exited"
                    self.persist_session(session)
                    return
                job = data["job"]
                job_id = job["id"]
                dsh_sid = job.get("sessionId", "")
                session.dsh_job_id = job_id
                session.dsh_session_id = dsh_sid
                session.dsh_token = token
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
            session.emit_virtual_ansi(f"\x1b[1;31m✖ Failed to reach DSH Host on 127.0.0.1:3080: {e}\x1b[0m\r\n")
            session.status = "exited"
            self.persist_session(session)

    async def _watch_dsh_hub_job(self, session: AgentSession, job_id: str, token: str):
        """
        Tails ~/.config/dsh-crew/logs/<job_id>.log for real-time steps/tools/thoughts AND
        polls /_dsh/dsh-crew/jobs/<job_id>/messages for bidirectional worker->parent messages & questions!
        """
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
            while session.dsh_job_id == job_id and session.status in ("starting", "running", "waiting_parent"):
                drain_log_lines()

                # Poll DSH WorkerMailbox for worker->parent messages or blocking questions
                try:
                    m_resp = await client.get(
                        f"http://127.0.0.1:3080/_dsh/dsh-crew/jobs/{job_id}/messages",
                        params={"after": msg_cursor, "wait": 1},
                        headers={"x-dsh-crew-job-token": token},
                    )
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
                            session.status = "completed" if jstatus == "done" else "exited"
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
                            self.record_worker_event(
                                session.session_id,
                                {
                                    "type": "completed",
                                    "category": "completed",
                                    "status": "completed",
                                    "content": full_res or f"DSH job {jstatus}",
                                },
                            )
                            break
                except Exception:
                    pass

                await asyncio.sleep(0.4)

    def _build_worker_bridge_system_hint(self, session_id: str) -> str:
        python_exe = str((self.base_dir / ".venv" / "Scripts" / "python.exe").resolve()).replace("\\", "/")
        worker_cli = str((self.base_dir / "hub_worker.py").resolve()).replace("\\", "/")
        return (
            f"\n[Sub-Agent Bridge Info: session_id={session_id}]\n"
            f"You are running inside MCP Agent Hub. To communicate with the Parent Agent at any time, you can run:\n"
            f'- Report step: `{python_exe} "{worker_cli}" step "step description" --tool "tool_name"`\n'
            f'- Send message to parent: `{python_exe} "{worker_cli}" message "your progress message"`\n'
            f'- Ask parent & wait for answer: `{python_exe} "{worker_cli}" ask "your question"`\n'
            f'- Report task complete: `{python_exe} "{worker_cli}" complete "your final summary"`\n'
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
    def _apply_engine_model_and_thinking_args(
        engine: str,
        base_args: List[str],
        effective_model: Optional[str],
        effective_thinking: Optional[str],
        explicit_model: Optional[str],
        explicit_thinking: Optional[str],
    ) -> tuple[List[str], Optional[str], Optional[str]]:
        """Dynamically injects/overrides CLI flags for model and thinking_level across omp, kimi, agy, codex, claude, reclaude."""
        args = list(base_args)
        eng = (engine or "").lower()

        if eng == "omp":
            args = [a for a in args if not a.startswith("--model=") and not a.startswith("--thinking=")]
            if effective_model:
                args.insert(0, f"--model={effective_model}")
            if effective_thinking:
                args.insert(1 if effective_model else 0, f"--thinking={effective_thinking}")
        elif eng == "kimi":
            cleaned: List[str] = []
            skip_next = False
            for a in args:
                if skip_next:
                    skip_next = False
                    continue
                if a in ("-m", "--model"):
                    skip_next = True
                    continue
                cleaned.append(a)
            args = cleaned
            if effective_model:
                args = ["-m", effective_model] + args
        elif eng == "agy":
            cleaned = []
            skip_next = False
            for a in args:
                if skip_next:
                    skip_next = False
                    continue
                if a in ("--model", "--effort"):
                    skip_next = True
                    continue
                cleaned.append(a)
            args = cleaned
            resolved_model = effective_model
            if resolved_model and effective_thinking in ("low", "medium", "high"):
                if explicit_thinking and re.search(r"-(low|medium|high)$", resolved_model):
                    resolved_model = re.sub(r"-(low|medium|high)$", f"-{effective_thinking}", resolved_model)
                elif resolved_model in ("gemini-3.8-flash", "gemini-4-argon", "claude-sonnet-5-5", "claude-opus-5-5"):
                    resolved_model = f"{resolved_model}-{effective_thinking}"
            effective_model = resolved_model
            if effective_model:
                args = ["--model", effective_model] + args
        elif eng == "codex":
            if explicit_model and effective_model:
                args = ["-m", effective_model] + args
            if explicit_thinking and effective_thinking:
                args = ["-c", f'model_reasoning_effort="{effective_thinking}"'] + args

        return args, effective_model, effective_thinking

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
            self.sessions[session_id] = sess
            self.persist_session(sess)
            return sess

        if profile_id not in self.profiles:
            enabled_keys = [k for k, v in self.profiles.items() if v.get("enabled", True)]
            raise ValueError(f"Unknown agent profile '{profile_id}'. Enabled profiles: {enabled_keys}")

        prof = self.profiles[profile_id]
        if not prof.get("enabled", True):
            enabled_keys = [k for k, v in self.profiles.items() if v.get("enabled", True)]
            raise ValueError(
                f"Agent profile '{profile_id}' has been disabled by user policy. "
                f"Please choose an enabled profile from: {enabled_keys}"
            )

        engine = self._infer_engine(profile_id, prof)
        disabled_mods = set(prof.get("disabled_models") or [])
        avail_mods = list(prof.get("available_models") or [])

        if model and model.strip() in disabled_mods:
            raise ValueError(
                f"Model '{model.strip()}' for engine '{engine}' has been disabled by user policy. "
                f"Currently enabled models for '{engine}': {avail_mods}"
            )

        effective_model = (model or prof.get("default_model") or "").strip() or None
        if effective_model and effective_model in disabled_mods:
            if avail_mods:
                effective_model = avail_mods[0]
            else:
                raise ValueError(f"All models for engine '{engine}' are currently disabled by user policy.")

        session_id = f"{profile_id}-{uuid.uuid4().hex[:6]}"
        work_dir = cwd or os.getcwd()
        if not os.path.isdir(work_dir):
            work_dir = os.getcwd()

        effective_thinking = (thinking_level or prof.get("default_thinking_level") or "").strip() or None

        args, effective_model, effective_thinking = self._apply_engine_model_and_thinking_args(
            engine=engine,
            base_args=list(prof.get("args", [])),
            effective_model=effective_model,
            effective_thinking=effective_thinking,
            explicit_model=model,
            explicit_thinking=thinking_level,
        )
        effective_approval_mode = (approval_mode or prof.get("approval_mode") or "auto").strip().lower()
        args = self._apply_approval_mode_args(engine=engine, base_args=args, approval_mode=effective_approval_mode)
        task_mode = prof.get("task_mode", "pty_input")
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
            kind=prof.get("kind", "tui"),
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

        if prof.get("kind") == "web_gui":
            if task:
                sess.args = ["-NoLogo", "-NoProfile", "-Command", f"dsh headless {json.dumps(task)}; pwsh -NoLogo"]
            else:
                sess.args = ["-NoLogo", "-NoProfile"]

        if task:
            sess.last_task = task
            sess.current_activity = f"Running initial task: {task[:80]}"

        if task_mode == "dsh_hub_api":
            dsh_prov = prof.get("dsh_provider", "clay")
            dsh_mod = prof.get("dsh_model", "deepseek-flash")
            if effective_model:
                if "/" in effective_model:
                    dsh_prov, dsh_mod = effective_model.split("/", 1)
                else:
                    dsh_mod = effective_model
            sess.dsh_spec = {
                "tier": prof.get("dsh_tier", "flash"),
                "effort": effective_thinking or prof.get("dsh_effort", "high"),
                "provider": dsh_prov,
                "model": dsh_mod,
            }
            sess.start_virtual(
                loop,
                on_virtual_line_cb=lambda sid, line: self.deliver_parent_message(sid, line, also_write_pty=False),
            )
            self.sessions[session_id] = sess
            self.persist_session(sess)
            asyncio.create_task(self._spawn_dsh_hub_job(sess, task))
            return sess

        sess.start_pty(loop)
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

        if prof.get("kind") == "tui":
            asyncio.create_task(self._watch_tui_interactive_prompts(sess))
            startup_delay = float(prof.get("startup_delay_sec", 2.5))
            submit_key = prof.get("submit_key", "\r")
            prompt_to_send: Optional[str] = None
            if task and task_mode == "pty_input":
                prompt_to_send = task
                if profile_id.startswith("kimi") and "hub_worker.py" not in task:
                    python_exe = str((self.base_dir / ".venv" / "Scripts" / "python.exe").resolve()).replace("\\", "/")
                    worker_cli = str((self.base_dir / "hub_worker.py").resolve()).replace("\\", "/")
                    prompt_to_send = f"{task} (When done run: {python_exe} {worker_cli} complete SUMMARY)"
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
