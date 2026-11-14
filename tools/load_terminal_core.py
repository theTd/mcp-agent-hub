"""Load test for the hub's terminal path (design §11): N printing sessions, one console socket each.

It starts a hub from the tree you name, gives every session the same fake engine
(``tools/fake_tui_engine.py`` replaying a recorded ``live-*`` corpus), attaches one WebSocket consumer
per session the way the console does on that tree, samples, and writes one JSON result.

Usage (run it with the interpreter the hub runs on; it starts the hub with ``py -3.15 -B``):

    py -3.15 -B tools/load_terminal_core.py --tree wt --sessions 15 --seconds 60
    py -3.15 -B tools/load_terminal_core.py --tree master-wt --sessions 15 --seconds 60
    py -3.15 -B tools/load_terminal_core.py --tree wt --sessions 50 --seconds 300 \
        --out scratch/full-wt-50.json

``--tree`` is any checkout of the hub (``wt`` and ``master-wt`` are the two the comparison uses); it
decides the socket path from the tree, not from a flag: a tree with the compiled core
(``hub_terminal_core*.pyd``) is attached through ``GET /api/sessions/{id}/terminal-ticket`` and the
core's own listener, any other tree through the hub's ``/ws/pty/{id}``.

Isolation (RULES.md, brief): the hub gets a free port it picked itself (never 4444/4445) and a data
folder under ``<tree>/../scratch/load/``; ``HUB_PROFILES_PATH`` points into that folder, because the
hub's default profile file is the live hub's own. Only the PIDs this run started are killed, by PID,
with ``taskkill /T`` (the hub's process tree), in a ``finally`` that also runs on Ctrl-C. The hub is
additionally assigned to a kill-on-close job: a console event that ends this interpreter outright
runs no Python at all, and closing the last job handle is then what takes the hub and its engines
with it. Nothing outside those processes is touched: no request ever reaches the live hub.

What one sample holds, and how each number is obtained (a proxy is labelled in ``measurement``):

- ``cpu_cores``: the hub process's ``GetProcessTimes`` kernel+user delta over the sample's wall time.
  ``children_cpu_cores`` sums the same for the hub's direct children (each engine and the conhost
  that hosts its pseudoconsole).
- ``threads``: threads whose owner is the hub PID, from a ``CreateToolhelp32Snapshot`` walk.
- ``lag_p50/p99/max_ms``: a coroutine injected into the hub with ``sys.remote_exec`` that measures
  ``asyncio.sleep(0.05)`` overshoot; ``lag_method`` says whether that worked or the tool fell back to
  a ``GET /health`` round trip (labelled ``http-roundtrip``).
- ``wakeups_per_s``: the injected sampler counts ``loop.call_soon_threadsafe`` calls — what wakes the
  hub's loop from a reader thread or from the core's ticker on both trees.
- ``core``: ``core.stats()`` read in the hub process once a second (only a tree with a core has it):
  sessions, alive, crashed, bytes_total, chunks_total, dropped_bytes, notify_calls, panics. On a core
  tree ``notify_calls`` is also ``drains_per_s``: one notify is one ``_on_core_tick`` → one ``drain()``.
- ``socket_drops``: ``sum(hub._socket_drops.values())`` (core trees only) — per-socket slow-client loss.

Bytes: every consumer counts UTF-8 bytes of the frames it got, split into ``bytes_replay`` (up to the
``replay_end`` control frame) and ``bytes_live``. ``core.bytes_total`` counts what the PTY delivered,
so the cumulative total at the last sample is compared with the consumer total (replay + live) —
both count from the session's spawn, and the core figure is a snapshot up to one second older. The
precise no-loss checks are ``core.dropped_bytes``, the per-socket drops the hub records, and the
marker sequence. Markers: the engine prints ``LOADMARK <n>`` every ``--marker-every`` chunks; every
number the engine had written when the consumers stopped must have arrived, and each number's first
appearance must come after the previous one (a repeat is a ConPTY repaint of an older line, not
loss). On a tree without a core there is no hub-side byte counter, and the engine's journal is the
producer side.

Each consumer also sends the resize frame the console sends on attach (120x36), so the resize path
runs during the measurement.

Result JSON (one file per run):

    {"schema": "load_terminal_core/1", "tree", "mode", "hub": {pid, port, ...}, "config",
     "measurement": {per metric: method or why not}, "samples": [...], "summary": {...},
     "consumers": [...], "accounting": {...}, "errors": [...], "leftover_pids": []}
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CASES = ("live-claude", "live-codex", "live-kimi", "live-omp", "live-agy")
LIVE_PORTS = (4444, 4445)
MARKER_RE = re.compile(r"LOADMARK[ \t]+(\d+)(?=[^0-9])")
MARKER_FINAL_RE = re.compile(r"LOADMARK[ \t]+(\d+)\Z")
REPLAY_END_TYPE = "replay_end"
# The size the hub spawns terminals at (the corpus was recorded at the same one); the console sends
# its pane's size on every attach, so one resize per consumer is part of the path being measured.
HUB_COLS, HUB_ROWS = 120, 36
ENGINE_TOOL = Path(__file__).resolve().parent / "fake_tui_engine.py"
HUB_PYTHON = ("py", "-3.15", "-B")
CREATE_NO_WINDOW = 0x08000000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD),
    ]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.CHAR * 260),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


TH32CS_SNAPTHREAD = 0x00000004
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9

# HANDLE results are 64-bit; without a restype ctypes truncates them to int.
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME),
                                     ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                     ctypes.POINTER(FILETIME)]
kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL


def _filetime_seconds(value: FILETIME) -> float:
    return ((int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)) / 10_000_000.0


def process_cpu_seconds(pid: int) -> Optional[float]:
    """Kernel+user CPU seconds of one process, or None if it is gone."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exited, kern, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited),
                                        ctypes.byref(kern), ctypes.byref(user)):
            return None
        return _filetime_seconds(kern) + _filetime_seconds(user)
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def thread_count(pid: int) -> Optional[int]:
    """Threads owned by ``pid`` (a toolhelp walk: the cheap per-PID answer Windows offers)."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        total = 0
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            return 0
        while True:
            if entry.th32OwnerProcessID == pid:
                total += 1
            if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
        return total
    finally:
        kernel32.CloseHandle(snapshot)


def child_pids(pid: int) -> List[int]:
    """Direct children of ``pid``; the core (and pywinpty) spawn each engine themselves."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        children: List[int] = []
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return []
        while True:
            if entry.th32ParentProcessID == pid:
                children.append(int(entry.th32ProcessID))
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
        return children
    finally:
        kernel32.CloseHandle(snapshot)


def free_port() -> int:
    """A port nothing listens on right now, never one of the live hub's."""
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in LIVE_PORTS:
            return port


class JobTree:
    """A kill-on-close job around the hub: however this tool dies, Windows ends the hub's tree.

    The in-process ``finally`` covers exceptions and a Ctrl-C that reaches Python; a console event
    that terminates the interpreter outright (``STATUS_CONTROL_C_EXIT``) runs no Python code at all.
    Closing the last handle to this job then is what kills the hub, its sessions and their engines —
    the launcher and, by inheritance, everything the hub spawns are members of it.
    """

    def __init__(self) -> None:
        self.handle = kernel32.CreateJobObjectW(None, None)
        self.error = ""
        if not self.handle:
            self.error = f"CreateJobObjectW failed (error {ctypes.get_last_error()})"
            return
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                                ctypes.byref(limits), ctypes.sizeof(limits)):
            self.error = f"SetInformationJobObject failed (error {ctypes.get_last_error()})"

    def assign(self, process: subprocess.Popen) -> bool:
        if not self.handle:
            return False
        if kernel32.AssignProcessToJobObject(self.handle, int(process._handle)):
            return True
        self.error = f"AssignProcessToJobObject failed (error {ctypes.get_last_error()})"
        return False


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[index], 2)


SAMPLER_TEMPLATE = '''\
# Injected into the hub process by tools/load_terminal_core.py through sys.remote_exec (PEP 768).
# It runs in the hub's main thread, installs one counter and one coroutine on the hub's own loop, and
# reports from inside the process: no request reaches the hub, so the samples add no hub traffic.
import asyncio
import json
import sys
import time
import traceback

OUT = {out!r}
LAG_INTERVAL = {lag_interval!r}
STATS_EVERY = {stats_every!r}


def _write(handle, record):
    handle.write(json.dumps(record, separators=(",", ":")) + "\\n")
    handle.flush()


def _install():
    loop = asyncio.get_running_loop()
    hub = getattr(sys.modules.get("__main__"), "hub", None)
    handle = open(OUT, "a", encoding="utf-8", buffering=1)
    state = {{"wakeups": 0}}
    original = loop.call_soon_threadsafe

    def counting(*args, **kwargs):
        # Exactly the calls that cross from a reader/core thread into the loop on both trees.
        state["wakeups"] += 1
        return original(*args, **kwargs)

    loop.call_soon_threadsafe = counting
    _write(handle, {{"event": "installed", "at": time.time(), "loop": type(loop).__name__,
                    "has_hub": hub is not None, "has_core": getattr(hub, "core", None) is not None}})

    async def sampler():
        next_stats = 0.0
        last = time.perf_counter()
        while True:
            await asyncio.sleep(LAG_INTERVAL)
            now = time.perf_counter()
            overshoot = (now - last - LAG_INTERVAL) * 1000.0
            last = now
            record = {{"event": "sample", "at": time.time(), "lag_ms": max(0.0, overshoot),
                      "wakeups": state["wakeups"]}}
            if now >= next_stats:
                next_stats = now + STATS_EVERY
                try:
                    core = getattr(hub, "core", None)
                    if core is not None:
                        record["core"] = core.stats()
                except Exception as exc:
                    record["core_error"] = repr(exc)
                try:
                    record["socket_drops"] = sum(int(v) for v in hub._socket_drops.values())
                except Exception:
                    pass
                try:
                    record["sessions"] = len(hub.sessions)
                except Exception:
                    pass
                # The §11 thread budget is about Python's own threads; the core's per-session threads
                # are native and only the OS-level count (the tool's) sees them.
                try:
                    import threading
                    record["python_threads"] = threading.active_count()
                except Exception:
                    pass
            _write(handle, record)

    loop.create_task(sampler())


try:
    _install()
except Exception:
    try:
        with open(OUT, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({{"event": "install_failed", "at": time.time(),
                                     "error": traceback.format_exc()}}) + "\\n")
    except Exception:
        pass
'''


class Consumer:
    """One console socket: the same two steps the console performs, and what it read."""

    def __init__(self, session_id: str, url_factory, stop: asyncio.Event) -> None:
        self.session_id = session_id
        self.url_factory = url_factory
        self.stop = stop
        self.connects = 0
        self.frames = 0
        self.bytes_replay = 0
        self.bytes_live = 0
        self.replay_done = False
        self.markers: List[int] = []
        self.first_appearance: List[int] = []
        self.duplicates = 0
        self.last_error = ""
        self.first_byte_at: Optional[float] = None
        self.last_byte_at: Optional[float] = None
        self._pending = ""
        self._seen: set = set()

    def on_message(self, message: Any) -> None:
        text = message if isinstance(message, str) else message.decode("utf-8", "replace")
        size = len(message) if isinstance(message, (bytes, bytearray)) else len(text.encode("utf-8"))
        now = time.time()
        if self.first_byte_at is None:
            self.first_byte_at = now
        self.last_byte_at = now
        self.frames += 1
        stripped = text.strip()
        # Both trees mark the end of the replay with a JSON control frame; master writes it with a
        # space after the colon, the core without, so the type is what is compared.
        if stripped.startswith("{") and REPLAY_END_TYPE in stripped:
            try:
                if json.loads(stripped).get("type") == REPLAY_END_TYPE:
                    self.replay_done = True
                    return
            except ValueError:
                pass
        if self.replay_done:
            self.bytes_live += size
        else:
            self.bytes_replay += size
        self._scan_markers(text)

    def _scan_markers(self, text: str) -> None:
        self._pending += text
        last_end = 0
        for match in MARKER_RE.finditer(self._pending):
            self._record_marker(int(match.group(1)))
            last_end = match.end()
        # Keep only an unfinished marker at the tail: a frame boundary may split "LOADMARK 12".
        self._pending = self._pending[last_end:][-32:]

    def _record_marker(self, number: int) -> None:
        self.markers.append(number)
        if number in self._seen:
            self.duplicates += 1
        else:
            self.first_appearance.append(number)
        self._seen.add(number)

    def finalize(self) -> None:
        """Count a trailing marker whose terminator never arrived before the socket closed."""
        match = MARKER_FINAL_RE.search(self._pending)
        if match:
            self._record_marker(int(match.group(1)))
            self._pending = ""

    async def run(self) -> None:
        from websockets.asyncio.client import connect
        while not self.stop.is_set():
            try:
                url = await self.url_factory()
                async with connect(url, open_timeout=20, max_size=None, close_timeout=2) as socket_:
                    self.connects += 1
                    await socket_.send(json.dumps({"type": "resize", "cols": HUB_COLS, "rows": HUB_ROWS}))
                    async for message in socket_:
                        self.on_message(message)
                        if self.stop.is_set():
                            return
                if self.stop.is_set():
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self.stop.is_set():
                    return
                await asyncio.sleep(0.5)


def resolve_tree(value: str) -> Path:
    """``--tree wt`` names a checkout; relative to the cwd, this tool's tree, or its parent folder."""
    tree_dir = ENGINE_TOOL.parent.parent
    for candidate in (Path(value), tree_dir / value, tree_dir.parent / value):
        if (candidate / "server.py").is_file():
            return candidate.resolve()
    return Path(value).resolve()


class LoadRun:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tree = resolve_tree(args.tree)
        self.run_dir = Path(args.work_dir or (self.tree.parent / "scratch" / "load" / self.run_name())).resolve()
        self.data_dir = self.run_dir / "data"
        self.sampler_path = self.run_dir / "sampler.jsonl"
        self._sampler_offset = 0
        self._sampler_buffer = b""
        self._sampler_events: List[Dict[str, Any]] = []
        self._install_at: Optional[float] = None
        self._prev_consumer_live = 0
        self.consumers_stopped_at: Optional[float] = None
        self.sampler_script = self.run_dir / "loop_sampler.py"
        self.hub_log_path = self.run_dir / "hub.log"
        self.hub_log = None
        self.job = JobTree()
        self.proc: Optional[subprocess.Popen] = None
        self.hub_pid: Optional[int] = None
        self.port = int(args.port or free_port())
        self.mode = "core" if any(self.tree.glob("hub_terminal_core*.pyd")) else "pty"
        self.profile_id = "load-fake-engine"
        self.sessions: List[Dict[str, Any]] = []
        self.consumers: List[Consumer] = []
        self.consumer_tasks: List[asyncio.Task] = []
        self.client = None
        self.stop = asyncio.Event()
        self.samples: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.result: Dict[str, Any] = {}

    # -- naming, paths ---------------------------------------------------------------------
    def run_name(self) -> str:
        return f"{self.tree.name}-{self.args.sessions}-{int(time.time())}"

    def out_path(self) -> Path:
        if self.args.out:
            return Path(self.args.out).resolve()
        return self.run_dir / "result.json"

    def prepare(self) -> None:
        if not (self.tree / "server.py").is_file():
            raise SystemExit(f"load_terminal_core: {str(self.tree)} has no server.py")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # The hub's default profile file is the live hub's (`%APPDATA%\\mcp-agent-hub`) — never that.
        (self.data_dir / "profiles.json").write_text('{"profiles":{}}', encoding="utf-8")
        self.sampler_script.write_text(
            SAMPLER_TEMPLATE.format(out=str(self.sampler_path), lag_interval=0.05, stats_every=1.0),
            encoding="utf-8")
        self.sampler_path.write_text("", encoding="utf-8")

    # -- hub ---------------------------------------------------------------------------------
    def start_hub(self) -> None:
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("HUB_"):
                env.pop(key)
        env.update({
            "HUB_PORT": str(self.port),
            "HUB_DATA_DIR": str(self.data_dir),
            "HUB_PROFILES_PATH": str(self.data_dir / "profiles.json"),
            "HUB_API_URL": f"http://127.0.0.1:{self.port}",
        })
        self.hub_log = open(self.hub_log_path, "wb")
        self.proc = subprocess.Popen(
            [*HUB_PYTHON, "server.py"], cwd=str(self.tree), env=env,
            stdout=self.hub_log, stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW,
        )
        if not self.job.assign(self.proc):
            self.errors.append(f"kill-on-close job not set: {self.job.error}")
        started = time.time()
        while time.time() - started < 180.0:
            if self.proc.poll() is not None:
                raise SystemExit(f"load_terminal_core: the hub exited during startup "
                                 f"(code {self.proc.returncode}); see {self.hub_log_path}")
            try:
                info = self._get_json("/api/hub/info", timeout=2.0)
                self.hub_pid = int(info["pid"])
                self.result["hub"] = {
                    "pid": self.hub_pid, "launcher_pid": self.proc.pid, "port": self.port,
                    "data_dir": str(self.data_dir), "repo": info.get("repo"),
                    "version": info.get("version"), "ready_seconds": round(time.time() - started, 2),
                    "python": " ".join(HUB_PYTHON),
                }
                return
            except Exception:
                time.sleep(0.3)
        raise SystemExit(f"load_terminal_core: the hub on {self.port} never answered; see {self.hub_log_path}")

    def _get_json(self, path: str, timeout: float = 30.0) -> Any:
        import httpx
        response = httpx.get(f"http://127.0.0.1:{self.port}{path}", timeout=timeout)
        response.raise_for_status()
        return response.json()

    def install_sampler(self) -> str:
        """``sys.remote_exec`` the lag/wakeup sampler into the hub; returns the method it settled on."""
        try:
            sys.remote_exec(self.hub_pid, str(self.sampler_script))
        except Exception as exc:
            self.errors.append(f"sys.remote_exec failed: {type(exc).__name__}: {exc}")
            return "http-roundtrip"
        deadline = time.time() + 5.0
        while time.time() < deadline:
            events = self.read_sampler_events()
            installed = next((e for e in events if e.get("event") == "installed"), None)
            if installed is not None:
                self._install_at = float(installed.get("at") or time.time())
                return "in-process-sampler"
            failure = next((e for e in events if e.get("event") == "install_failed"), None)
            if failure is not None:
                self.errors.append(f"sampler install failed: {str(failure.get('error'))[-500:]}")
                return "http-roundtrip"
            time.sleep(0.2)
        self.errors.append("sampler installed nothing within 5 s")
        return "http-roundtrip"

    def read_sampler_events(self) -> List[Dict[str, Any]]:
        """New sampler lines since the last call, appended to ``_sampler_events``.

        The sampler writes ~20 lines a second for the whole run; re-reading the file per sample would
        parse megabytes by the end, so only the bytes since the last offset are read.
        """
        try:
            with open(self.sampler_path, "rb") as handle:
                handle.seek(self._sampler_offset)
                data = handle.read()
                self._sampler_offset = handle.tell()
        except OSError:
            return self._sampler_events
        self._sampler_buffer += data
        *lines, self._sampler_buffer = self._sampler_buffer.split(b"\n")
        for raw in lines:
            if not raw.strip():
                continue
            try:
                self._sampler_events.append(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                continue
        return self._sampler_events

    async def save_profile(self) -> None:
        profile = {
            "id": self.profile_id,
            "name": "Load fake engine",
            "engine": "generic",
            "kind": "tui",
            "command": sys.executable,
            "args": [str(ENGINE_TOOL)],
            "task_mode": "pty_input",
            "startup_delay_sec": 0,
            "submit_key": "\r",
            "enabled": True,
            "description": "load_terminal_core.py: replays a recorded corpus (design §11)",
        }
        response = await self.client.post(f"http://127.0.0.1:{self.port}/api/profiles", json=profile)
        if response.status_code != 200:
            raise SystemExit(f"load_terminal_core: saving the profile failed ({response.status_code}): "
                             f"{response.text[:300]}")

    async def create_sessions(self) -> None:
        cases = [case.strip() for case in self.args.cases.split(",") if case.strip()]
        started = time.time()
        for index in range(self.args.sessions):
            case = cases[index % len(cases)]
            journal = self.run_dir / f"engine-{index:02d}.jsonl"
            body = {
                "profile_id": self.profile_id,
                "cwd": str(self.run_dir),
                "title": f"load {index:02d} {case}",
                "extra_args": ["--case", case, "--speed", str(self.args.speed), "--loop",
                               "--marker-every", str(self.args.marker_every),
                               "--marker-hold-ms", str(self.args.marker_hold_ms),
                               "--journal", str(journal)],
            }
            response = await self.client.post(f"http://127.0.0.1:{self.port}/api/sessions", json=body)
            if response.status_code != 200:
                self.errors.append(f"session {index}: HTTP {response.status_code} {response.text[:200]}")
                continue
            session_id = response.json()["session"]["session_id"]
            consumer = Consumer(session_id, self.url_factory(session_id), self.stop)
            self.consumers.append(consumer)
            self.sessions.append({"index": index, "session_id": session_id, "case": case,
                                  "journal": str(journal)})
            self.consumer_tasks.append(asyncio.create_task(consumer.run()))
            await asyncio.sleep(self.args.stagger_ms / 1000.0)
        self.result["hub"]["spawn_seconds"] = round(time.time() - started, 2)
        # Sample only once every pane is attached, so the numbers are the steady state.
        deadline = time.time() + 60.0
        while time.time() < deadline and not all(c.connects for c in self.consumers):
            await asyncio.sleep(0.2)
        if not all(c.connects for c in self.consumers):
            self.errors.append(f"{sum(1 for c in self.consumers if not c.connects)} consumer(s) never attached")

    def url_factory(self, session_id: str):
        if self.mode == "pty":
            async def direct() -> str:
                return f"ws://127.0.0.1:{self.port}/ws/pty/{session_id}"
            return direct

        async def ticketed() -> str:
            response = await self.client.get(
                f"http://127.0.0.1:{self.port}/api/sessions/{session_id}/terminal-ticket")
            response.raise_for_status()
            return str(response.json()["url"])
        return ticketed

    # -- sampling ----------------------------------------------------------------------------
    def _sample_process(self, previous: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        sample: Dict[str, Any] = {"at": now}
        cpu = process_cpu_seconds(self.hub_pid)
        wall = now - previous.get("at", now)
        if cpu is not None and wall > 0:
            sample["cpu_cores"] = round((cpu - previous.get("_cpu_seconds", cpu)) / wall, 3)
            sample["hub_cpu_seconds"] = round(cpu, 3)
        sample["_cpu_seconds"] = cpu
        children = child_pids(self.hub_pid)
        child_cpu = 0.0
        counted = 0
        for pid in children:
            value = process_cpu_seconds(pid)
            if value is not None:
                child_cpu += value
                counted += 1
        if wall > 0:
            sample["children_cpu_cores"] = round(
                (child_cpu - previous.get("_child_cpu", child_cpu)) / wall, 3)
        sample["_child_cpu"] = child_cpu
        sample["engine_processes"] = counted
        sample["threads"] = thread_count(self.hub_pid)
        return sample

    def _sampler_window(self, start: float, end: float) -> Dict[str, Any]:
        events = [e for e in self.read_sampler_events() if e.get("event") == "sample"]
        in_window = [e for e in events if start <= e.get("at", 0) <= end]
        lags = [e["lag_ms"] for e in in_window if "lag_ms" in e]
        window: Dict[str, Any] = {
            "lag_p50_ms": percentile(lags, 50),
            "lag_p99_ms": percentile(lags, 99),
            "lag_max_ms": round(max(lags), 2) if lags else None,
            "lag_samples": len(lags),
        }
        if len(in_window) >= 2:
            first, last = in_window[0], in_window[-1]
            span = max(1e-6, last["at"] - first["at"])
            window["wakeups_per_s"] = round((last["wakeups"] - first["wakeups"]) / span, 1)
        for event in reversed(in_window):
            for key in ("core", "socket_drops", "sessions", "python_threads"):
                if event.get(key) is not None:
                    window["hub_sessions" if key == "sessions" else key] = event[key]
            if "core" in window:
                window["core_at"] = event.get("at")
                break
        return window

    async def sample_loop(self) -> None:
        import httpx
        started = time.time()
        self.result["hub"]["sample_started_at"] = started
        end_at = started + self.args.seconds
        self._prev_consumer_live = sum(c.bytes_live for c in self.consumers)
        previous = self._sample_process({"at": started - self.args.sample_interval})
        while True:
            remaining = end_at - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.args.sample_interval, remaining))
            sample = self._sample_process(previous)
            previous = sample
            window = self._sampler_window(sample["at"] - self.args.sample_interval, sample["at"])
            sample.update(window)
            consumer_live = sum(c.bytes_live for c in self.consumers)
            sample["consumer_bytes_live"] = consumer_live
            sample["consumer_bytes_replay"] = sum(c.bytes_replay for c in self.consumers)
            sample["consumer_frames"] = sum(c.frames for c in self.consumers)
            sample["markers_seen"] = sum(len(c.markers) for c in self.consumers)
            sample["consumer_bytes_live_window"] = consumer_live - self._prev_consumer_live
            self._prev_consumer_live = consumer_live

            if self.result["measurement"]["lag_method"] != "in-process-sampler":
                probe_started = time.perf_counter()
                try:
                    async with httpx.AsyncClient(timeout=5.0) as probe:
                        await probe.get(f"http://127.0.0.1:{self.port}/health")
                    sample["lag_p50_ms"] = sample["lag_p99_ms"] = sample["lag_max_ms"] = round(
                        (time.perf_counter() - probe_started) * 1000.0, 2)
                    sample["lag_samples"] = 1
                except Exception as exc:
                    sample["lag_error"] = f"{type(exc).__name__}: {exc}"
            self.samples.append({key: value for key, value in sample.items() if not key.startswith("_")})
            if not pid_alive(self.hub_pid):
                self.errors.append(f"the hub process {self.hub_pid} died during the sample")
                break
        self.result["hub"]["sample_ended_at"] = time.time()
        self.result["hub"]["measured_seconds"] = round(
            self.result["hub"]["sample_ended_at"] - started, 1)
        # A session that died mid-run would make every average above a measurement of fewer sessions
        # than asked for, so say it here rather than leave it to the per-consumer error strings.
        alive = await self.count_alive_sessions()
        self.result["hub"]["sessions_alive_at_end"] = alive
        if alive is not None and alive < len(self.sessions):
            self.errors.append(f"only {alive} of {len(self.sessions)} sessions were alive at the "
                               f"last sample")

    async def count_alive_sessions(self) -> Optional[int]:
        if self.client is None:
            return None
        try:
            response = await self.client.get(f"http://127.0.0.1:{self.port}/api/sessions", timeout=15.0)
            rows = response.json().get("sessions") or []
        except Exception:
            return None
        running = {"running", "waiting_parent", "waiting_agent"}
        return sum(1 for row in rows
                   if row.get("process_alive") is True or str(row.get("status")) in running)

    # -- accounting --------------------------------------------------------------------------
    def summarize(self) -> None:
        measured = [s for s in self.samples if "cpu_cores" in s]
        summary: Dict[str, Any] = {
            "samples": len(self.samples),
            "cpu_cores_p50": percentile([s["cpu_cores"] for s in measured], 50),
            "cpu_cores_p99": percentile([s["cpu_cores"] for s in measured], 99),
            "cpu_cores_max": max((s["cpu_cores"] for s in measured), default=None),
            "children_cpu_cores_p50": percentile(
                [s.get("children_cpu_cores", 0.0) for s in measured], 50),
            "threads_max": max((s.get("threads") or 0 for s in measured), default=None),
            "python_threads_last": next((s.get("python_threads") for s in reversed(self.samples)
                                         if s.get("python_threads") is not None), None),
            "python_threads_max": max((s.get("python_threads") or 0 for s in self.samples), default=None),
            "engine_processes_max": max((s.get("engine_processes") or 0 for s in measured), default=None),
        }
        lags = [s["lag_p99_ms"] for s in self.samples if s.get("lag_p99_ms") is not None]
        summary["lag_p99_ms_max_of_windows"] = max(lags, default=None)
        core_samples = [s["core"] for s in self.samples if s.get("core")]
        if core_samples:
            first, last = core_samples[0], core_samples[-1]
            summary["core_bytes_delta"] = last["bytes_total"] - first["bytes_total"]
            summary["core_bytes_total_last"] = last["bytes_total"]
            summary["core_chunks_delta"] = last["chunks_total"] - first["chunks_total"]
            summary["core_dropped_bytes_delta"] = last["dropped_bytes"] - first["dropped_bytes"]
            summary["core_notify_calls_delta"] = last["notify_calls"] - first["notify_calls"]
            summary["core_panics"] = last["panics"]
            summary["core_sessions_last"] = last["sessions"]
            summary["core_alive_last"] = last["alive"]
        wakeups = [s["wakeups_per_s"] for s in self.samples if s.get("wakeups_per_s") is not None]
        summary["wakeups_per_s_p50"] = percentile(wakeups, 50)
        summary["wakeups_per_s_max"] = max(wakeups, default=None)
        # The same 50 ms samples over the whole measurement, and over the quiet window before the
        # sessions spawned: the second is this machine's floor for the probe, without which a p99
        # reads higher than the hub's own stalls.
        started = self.result["hub"].get("sample_started_at")
        ended = self.result["hub"].get("sample_ended_at")
        if started and ended:
            overall = self._sampler_window(started, ended)
            summary["lag_overall_p50_ms"] = overall.get("lag_p50_ms")
            summary["lag_overall_p99_ms"] = overall.get("lag_p99_ms")
            summary["lag_overall_max_ms"] = overall.get("lag_max_ms")
            summary["lag_overall_samples"] = overall.get("lag_samples")
        baseline_end = self.result["hub"].get("baseline_ended_at")
        if self._install_at is not None and baseline_end:
            baseline = self._sampler_window(self._install_at + 0.5, baseline_end)
            summary["lag_baseline_p50_ms"] = baseline.get("lag_p50_ms")
            summary["lag_baseline_p99_ms"] = baseline.get("lag_p99_ms")
            summary["lag_baseline_samples"] = baseline.get("lag_samples")
        self.result["summary"] = summary
        self.result["samples"] = self.samples

        consumers = []
        markers_expected = markers_missing = out_of_order = duplicates = engine_total = 0
        # A marker the engine wrote in the last moment may still have been in the core's socket queue
        # when the consumer was cancelled; those are excluded rather than counted as loss.
        stopped_at = self.consumers_stopped_at or self.result["hub"].get("sample_ended_at")
        cutoff = (stopped_at - 0.5) if stopped_at else None
        in_flight = 0
        for consumer in self.consumers:
            consumer.finalize()
            entry = next(s for s in self.sessions if s["session_id"] == consumer.session_id)
            journal = self.read_journal(Path(entry["journal"]), cutoff=cutoff)
            produced = int(journal.get("markers") or 0)
            in_flight += int(journal.get("last_marker") or 0) - produced
            markers_expected += produced
            missing = len([n for n in range(1, produced + 1) if n not in consumer._seen])
            markers_missing += missing
            # The socket is a rendering stream: ConPTY repaints an older line now and then, so a repeat
            # of an earlier number is a repaint, not loss. What must hold is that each number's *first*
            # appearance comes after the previous one.
            appearance = consumer.first_appearance
            ordered = all(appearance[i] < appearance[i + 1] for i in range(len(appearance) - 1))
            out_of_order += 0 if ordered else 1
            duplicates += consumer.duplicates
            engine_total += int(journal.get("total_bytes") or 0)
            consumers.append({
                "session_id": consumer.session_id,
                "case": entry["case"],
                "connects": consumer.connects,
                "frames": consumer.frames,
                "bytes_replay": consumer.bytes_replay,
                "bytes_live": consumer.bytes_live,
                "replay_done": consumer.replay_done,
                "markers_seen": len(consumer.markers),
                "markers_engine": produced,
                "markers_engine_total": int(journal.get("last_marker") or 0),
                "markers_missing": missing,
                "markers_duplicated": consumer.duplicates,
                "markers_in_order": ordered,
                "markers_first_appearances": len(consumer.first_appearance),
                "markers_last": consumer.markers[-1] if consumer.markers else None,
                "engine_total_bytes": int(journal.get("total_bytes") or 0),
                "engine_corpus_bytes": int(journal.get("corpus_bytes") or 0),
                "engine_done": bool(journal.get("done")),
                "last_error": consumer.last_error,
            })
        self.result["consumers"] = consumers
        self.result["accounting"] = {
            "markers_expected": markers_expected,
            "markers_in_flight_excluded": in_flight,
            "markers_missing": markers_missing,
            "markers_duplicated": duplicates,
            "consumers_first_appearance_out_of_order": out_of_order,
            "consumer_bytes_live": sum(c.bytes_live for c in self.consumers),
            "consumer_bytes_replay": sum(c.bytes_replay for c in self.consumers),
            "engine_total_bytes": engine_total,
            "core_bytes_delta": summary.get("core_bytes_delta"),
            "core_dropped_bytes_delta": summary.get("core_dropped_bytes_delta"),
            "socket_drops_last": next((s.get("socket_drops") for s in reversed(self.samples)
                                       if s.get("socket_drops") is not None), None),
        }
        last_core = summary.get("core_bytes_total_last")
        consumer_total = sum(c.bytes_live + c.bytes_replay for c in self.consumers)
        self.result["accounting"].update({
            "bytes_core_total_last": last_core,
            "bytes_consumer_total": consumer_total,
            "bytes_difference": (last_core - consumer_total) if last_core is not None else None,
        })

    @staticmethod
    def read_journal(path: Path, cutoff: Optional[float] = None) -> Dict[str, Any]:
        """The engine's last word on what it wrote: a killed process prints no summary line.

        ``cutoff`` (unix seconds) counts only markers the engine had written before it, so a marker
        still travelling through ConPTY and the core when the sockets closed is not called lost.
        """
        result: Dict[str, Any] = {"markers": 0, "last_marker": 0, "total_bytes": 0,
                                  "corpus_bytes": 0, "done": False}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return result
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") == "marker":
                number = int(record.get("marker") or 0)
                result.update(corpus_bytes=record.get("corpus_bytes"),
                              total_bytes=record.get("total_bytes"),
                              last_marker=max(int(result["last_marker"]), number))
                if cutoff is None or float(record.get("at") or 0.0) <= cutoff:
                    result["markers"] = max(int(result["markers"]), number)
            elif record.get("event") == "done":
                result.update(corpus_bytes=record.get("corpus_bytes"),
                              total_bytes=record.get("total_bytes"),
                              markers=int(record.get("markers") or 0),
                              last_marker=max(int(result["last_marker"]),
                                              int(record.get("markers") or 0)),
                              done=True, loops=record.get("loops"))
        return result

    # -- teardown ---------------------------------------------------------------------------
    async def stop_consumers(self) -> None:
        self.stop.set()
        for task in self.consumer_tasks:
            task.cancel()
        if self.consumer_tasks:
            _done, pending = await asyncio.wait(self.consumer_tasks, timeout=10.0)
            if pending:
                self.errors.append(f"{len(pending)} consumer task(s) did not stop within 10 s")
        self.consumers_stopped_at = time.time()

    async def close_sessions(self) -> None:
        client = self.client
        if client is None or self.hub_pid is None or not pid_alive(self.hub_pid):
            return
        for entry in self.sessions:
            try:
                await client.delete(f"http://127.0.0.1:{self.port}/api/sessions/"
                                    f"{entry['session_id']}", timeout=30.0)
            except Exception as exc:
                self.errors.append(f"closing {entry['session_id']}: {type(exc).__name__}: {exc}")
        deadline = time.time() + 20.0
        while time.time() < deadline:
            try:
                rows = (await client.get(f"http://127.0.0.1:{self.port}/api/sessions",
                                         timeout=10.0)).json().get("sessions") or []
            except Exception:
                break
            if not rows:
                return
            await asyncio.sleep(0.3)

    def stop_hub(self) -> None:
        """Kill only the processes this run started: the hub's tree, by PID."""
        owned = self.owned_pids()
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True, check=False)
        for pid in owned:
            if pid_alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, check=False)
        if self.hub_log is not None:
            try:
                self.hub_log.close()
            except OSError:
                pass
            self.hub_log = None

    def owned_pids(self) -> List[int]:
        pids = [pid for pid in ((self.proc.pid if self.proc else None), self.hub_pid) if pid]
        if self.hub_pid:
            pids.extend(child_pids(self.hub_pid))
        return pids

    def leftovers(self) -> List[int]:
        return [pid for pid in self.owned_pids() if pid_alive(pid)]

    # -- run --------------------------------------------------------------------------------
    async def execute(self) -> Dict[str, Any]:
        import httpx
        self.prepare()
        self.result = {
            "schema": "load_terminal_core/1",
            "tree": str(self.tree),
            "mode": self.mode,
            "config": {
                "sessions": self.args.sessions, "seconds": self.args.seconds, "speed": self.args.speed,
                "cases": self.args.cases, "marker_every": self.args.marker_every,
                "marker_hold_ms": self.args.marker_hold_ms,
                "stagger_ms": self.args.stagger_ms, "sample_interval_s": self.args.sample_interval,
                "baseline_seconds": self.args.baseline_seconds,
                "runner_python": sys.executable,
            },
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "measurement": {
                "cpu": "GetProcessTimes kernel+user of the hub PID / wall time (cores); the hub's direct "
                       "children (each engine and its conhost) summed the same way",
                "threads": "toolhelp thread snapshot filtered by owner PID",
                "lag_method": "in-process-sampler",
                "lag": "injected coroutine: asyncio.sleep(0.05) overshoot, p50/p99/max over 50 ms samples",
                "wakeups": "loop.call_soon_threadsafe calls counted in the hub process",
                "drains": "core notify_calls delta (one notify = one hub drain) on a core tree",
                "context_switches": "not measured: Windows has no per-process context-switch counter "
                                    "without a perf provider; typeperf/Get-Counter samples system-wide "
                                    "and costs a PowerShell process per sample",
                "bytes": "consumer UTF-8 bytes per frame (replay + live) vs core.bytes_total, both "
                         "cumulative from spawn; the core value is a snapshot up to 1 s older, so the "
                         "difference carries up to one second of output. dropped_bytes, socket_drops "
                         "and the marker sequence are the precise no-loss checks; the engine journal is "
                         "the producer side",
            },
            "errors": self.errors,
        }
        self.client = httpx.AsyncClient(timeout=60.0)
        try:
            self.start_hub()
            method = self.install_sampler()
            self.result["measurement"]["lag_method"] = method
            if method != "in-process-sampler":
                self.result["measurement"]["lag"] = (
                    "PROXY: the in-process sampler could not be injected; the tool measured the round "
                    "trip of GET /health once per sample instead")
            # A quiet stretch before the sessions spawn, so the report can say what this machine's
            # loop floor is: a p99 without it reads as the hub's own stalls.
            await asyncio.sleep(self.args.baseline_seconds)
            self.result["hub"]["baseline_ended_at"] = time.time()
            await self.save_profile()
            await self.create_sessions()
            await self.sample_loop()
        finally:
            try:
                await self.stop_consumers()
            except Exception as exc:
                self.errors.append(f"stopping consumers: {type(exc).__name__}: {exc}")
            try:
                await self.close_sessions()
            except Exception as exc:
                self.errors.append(f"closing sessions: {type(exc).__name__}: {exc}")
            if self.client is not None:
                try:
                    await self.client.aclose()
                except Exception:
                    pass
                self.client = None
            self.stop_hub()
            self.summarize()
            self.result["leftover_pids"] = self.leftovers()
            self.result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self.result["run_dir"] = str(self.run_dir)
            out = self.out_path()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(self.result, indent=2, ensure_ascii=False), encoding="utf-8")
            self.result["result_path"] = str(out)
        return self.result


def cleanup_orphans(run: Optional[LoadRun]) -> None:
    """The Ctrl-C path: asyncio.run may never reach the coroutine's finally."""
    if run is None:
        return
    try:
        run.stop_hub()
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start N printing sessions on a hub from --tree, attach one console socket each, "
                    "sample, and write one JSON result (design §11).")
    parser.add_argument("--tree", required=True, help="hub checkout to run (wt, master-wt, ...)")
    parser.add_argument("--sessions", type=int, default=15, help="sessions to run at once")
    parser.add_argument("--seconds", type=float, default=300.0, help="how long to sample after all attach")
    parser.add_argument("--cases", default=",".join(DEFAULT_CASES),
                        help=f"corpus cases, round-robin (default: {','.join(DEFAULT_CASES)})")
    parser.add_argument("--speed", type=float, default=1.0, help="corpus replay speed (1 = as recorded)")
    parser.add_argument("--marker-every", type=int, default=20, help="engine marker cadence in chunks")
    parser.add_argument("--marker-hold-ms", type=float, default=40.0,
                        help="how long the engine keeps a marker on screen (default 40 ms)")
    parser.add_argument("--stagger-ms", type=int, default=150, help="delay between session launches")
    parser.add_argument("--sample-interval", type=float, default=5.0, help="seconds per result sample")
    parser.add_argument("--baseline-seconds", type=float, default=2.5,
                        help="quiet time after the sampler installs, before any session spawns")
    parser.add_argument("--port", type=int, default=0, help="hub port (default: a free one it picks)")
    parser.add_argument("--out", default=None, help="result JSON (default: <run dir>/result.json)")
    parser.add_argument("--work-dir", default=None, help="run dir (default: <tree>/../scratch/load/<run>)")
    args = parser.parse_args(argv)
    if args.port in LIVE_PORTS:
        raise SystemExit(f"load_terminal_core: refusing the live hub's port {args.port}")
    if args.sessions < 1:
        raise SystemExit("load_terminal_core: --sessions must be >= 1")
    run = LoadRun(args)
    try:
        result = asyncio.run(run.execute())
    except KeyboardInterrupt:
        cleanup_orphans(run)
        print("\nload_terminal_core: interrupted; the hub's process tree was killed", file=sys.stderr)
        return 130
    print(json.dumps({
        "tree": result["tree"], "mode": result["mode"], "sessions": result["config"]["sessions"],
        "measured_seconds": result.get("hub", {}).get("measured_seconds"),
        "summary": result.get("summary"), "accounting": result.get("accounting"),
        "errors": result.get("errors"), "leftover_pids": result.get("leftover_pids"),
        "result": result.get("result_path"),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
