"""Record one live engine session as a terminal corpus chunk stream (design doc §9 step 0).

The corpus is the input the pyte oracle renders in ``tools/render_terminal_fixtures.py``; step 1
re-records the same cases against the system ConPTY before the Rust core is compared to them.

Spawn mirrors the hub exactly: ``winpty.PtyProcess.spawn`` with the hub's default 120x36 size and
the env policy of ``AgentSession.start_pty`` (session_manager.py:2245), argv passed through the
hub's own ``unwrap_windows_cmd_shim`` (:1864), chunks read with ``pty.read(4096)`` from one reader
thread the way ``_read_loop`` (:2280) reads them.  Only the bookkeeping after the read differs:
this tool timestamps every chunk so the stream can be replayed instead of recorded again.

Isolation rules (RULES.md, brief §1):
- the task text carries ``[Hub Session: session_id=rtc-corpus-<engine>-<random>]`` so no hub
  session can ever claim the transcript (``_claim_conversation``, session_manager.py:3371-3376);
- ``HUB_PORT``/``HUB_API_URL`` point at a port nothing listens on and every other ``HUB_*`` variable
  is removed;
- the engine runs in a scratch task folder and is killed with its children at the end.

What is isolated per engine, and by what:

- ``claude``: ``--strict-mcp-config --mcp-config '{"mcpServers":{}}'`` (both after the prompt, since
  ``--mcp-config`` is variadic).  No other user MCP source is read under strict mode.
- ``codex``: the recorder reads ``~/.codex/config.toml`` (read-only) and passes
  ``-c mcp_servers.<name>.enabled=false`` for **every** configured server whose url or command names
  port 4444 or 4445; the name is never the reason, the target is.  Servers that point elsewhere stay
  enabled, as today, and the file is never written.
- ``omp``: no MCP flag exists; run it with ``--env PI_CODING_AGENT_DIR=<copy of ~/.omp/agent with an
  empty mcp.json>``.  The recorder refuses when that override is missing and the user's own
  ``mcp.json`` (the default agent dir) lists a server on 4444/4445.
- ``kimi``: reads ``<KIMI_CODE_HOME>/mcp.json`` (user), ``<project root>/.mcp.json`` and
  ``<cwd>/.kimi-code/mcp.json``; it has no MCP flag and no MCP-disable switch.  The recorder therefore
  refuses to record kimi when any of those three files (read-only) lists a server whose url or command
  targets 4444/4445, and says which file and server.  Otherwise the user's servers are dialled exactly
  as a normal kimi run would, and the recording would not be reproducible on another machine.
- ``agy``: ``--settings '{"agentMode":"auto"}'`` per D5.  It loads ``~/.gemini/config/mcp_config.json``
  (``agy mcp list``) and has no session-scoped switch for it, so the recorder refuses when that file
  enables a server on 4444/4445; ``agy mcp disable`` is the operator's call, not the tool's.

Chunks are recorded as ``(delay_ms since the previous chunk, text)`` in ``chunks.jsonl``.  Personal
data is removed afterwards by ``tools/terminal_corpus_scrub.py``; never commit a raw recording.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from session_manager import (  # noqa: E402
    HubTerminalScreen,
    HubTerminalStream,
    HubSessionManager,
    unwrap_windows_cmd_shim,
)
from winpty import PtyProcess  # noqa: E402

RECORDER_VERSION = 1
# The hub's default PTY size (SessionManager.default_cols/default_rows, session_manager.py:2922).
DEFAULT_COLS = 120
DEFAULT_ROWS = 36
# The resize a pane performs mid-recording: same 20/5 clamp floor, a plausible single-window growth.
RESIZE_COLS = 140
RESIZE_ROWS = 44
# A port that is checked to be closed before the engine starts; HUB_PORT/HUB_API_URL point at it so
# an engine that reads either can never reach the live hub on 4444.
ISOLATED_PORT = 4599
# Brief §1: about 3 minutes and about 2 MB per recording.
MAX_DURATION_SEC = 180.0
MAX_CORPUS_BYTES = 2 * 1024 * 1024
# Read size of one chunk: AgentSession._read_loop uses 4096 (session_manager.py:2282).
READ_SIZE = 4096
IDLE_SLEEP_SEC = 0.02


class Answer:
    """One screen prompt this recorder answers, and the keys it types (the hub types keys too)."""

    def __init__(self, name: str, pattern: str, keys: str, guard: Optional[str] = None,
                 delay_after_match_sec: float = 1.0, repeat_after_sec: float = 0.0,
                 then_keys: str = "", then_delay_sec: float = 0.0):
        self.name = name
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.guard = re.compile(guard, re.IGNORECASE) if guard else None
        self.keys = keys
        self.delay_after_match_sec = delay_after_match_sec
        # A tool prompt can appear several times in one session; answering it once is not enough.
        self.repeat_after_sec = repeat_after_sec
        # A second keystroke after the text settled: kimi's composer needs its Enter as its own
        # write once the pasted task has been rendered (the hub's checked submit does the same).
        self.then_keys = then_keys
        self.then_delay_sec = then_delay_sec


# A permission prompt is only ever approved for a read-only command: the task is "list the files",
# and RULES.md forbids approving anything that could write outside the scratch task folder.
READ_ONLY_COMMAND = r"\b(?:ls|dir|find|findstr|type|cat|Get-ChildItem|Select-Object|Measure-Object|wc)\b"

ANSWERS: Dict[str, List[Answer]] = {
    "claude": [
        Answer("trust_folder", r"Do you trust the files in this folder", "\r", delay_after_match_sec=0.5),
        Answer("read_only_permission", r"Do you want to proceed\?", "1\r", guard=READ_ONLY_COMMAND),
    ],
    "codex": [
        Answer("trust_folder", r"(?:trust the files in this folder|Do you trust this folder)", "\r",
               delay_after_match_sec=0.5),
        Answer("read_only_approval", r"(?:Allow .* to run|Would you like to run the following command)",
               "y\r", guard=READ_ONLY_COMMAND),
    ],
    "omp": [
        Answer("trust_folder", r"trust (?:this folder|the files in this folder)", "\r",
               delay_after_match_sec=0.5),
        Answer("approve_read_only", r"Allow tool:", "\r", guard=READ_ONLY_COMMAND,
               repeat_after_sec=3.0),
    ],
    "kimi": [
        # The hub starts kimi with no task on the command line and types it once the TUI is up
        # (task_mode pty_input, startup_delay 3.5): typing before the composer exists loses the text.
        Answer("type_task", r"Kimi .{0,60}is ready", "{task}", delay_after_match_sec=0.5,
               then_keys="\r", then_delay_sec=0.8),
        Answer("trust_folder", r"(?:trust this (?:folder|workspace)|Do you trust)", "\r",
               delay_after_match_sec=0.5),
        Answer("read_only_approval", r"(?:Approve|Allow|Run).*(?:command|tool)", "1\r", guard=READ_ONLY_COMMAND),
    ],
    "agy": [
        Answer("trust_folder", r"(?:trust this (?:folder|workspace)|Do you trust)", "\r",
               delay_after_match_sec=0.5),
        Answer("read_only_approval", r"(?:Approve|Allow|Yes).*(?:command|tool)", "1\r", guard=READ_ONLY_COMMAND),
    ],
}

# Keys that leave the TUI; a still-running engine is killed with its whole tree afterwards.
EXIT_KEYS = "/exit\r"

ENGINES: Dict[str, Dict[str, object]] = {
    "claude": {
        # Cheapest model and lowest effort; no session file (nothing for a hub watcher to claim) and
        # user/plugin MCP servers replaced by an empty set so nothing dials the live hub. The
        # variadic --mcp-config stays behind the prompt, or it would swallow the prompt as a config.
        "args_before_task": ["--model", "haiku", "--effort", "low"],
        "args_after_task": ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
        "task_mode": "positional",
        "startup_delay": 2.0,
    },
    "codex": {
        # D4 policy, update check off, cheapest reasoning. Every configured MCP server that points at
        # a hub port is disabled by `codex_mcp_disables` from the real config, not by name here.
        "args_before_task": ["-a", "on-request", "-s", "workspace-write",
                 "-c", "check_for_update_on_startup=false",
                 "-c", "model_reasoning_effort=minimal"],
        "task_mode": "positional",
        "startup_delay": 2.5,
    },
    "omp": {
        # D2 approval mode, explicit cheapest model, lowest thinking, and an ephemeral session.
        # The user's ~/.omp/agent/mcp.json lists the live hub's MCP endpoint, and omp has no MCP flag:
        # run it with --env PI_CODING_AGENT_DIR=<copy of that agent dir with an empty mcp.json>.
        "args_before_task": ["--model=clay/deepseek-flash", "--thinking=off",
                             "--approval-mode=write", "--no-session", "--no-title"],
        "task_mode": "positional",
        "startup_delay": 2.0,
    },
    "kimi": {
        # D3 `-y`; the thinking level travels in the child-only env var the hub uses.
        "args_before_task": ["-y"],
        "task_mode": "pty_input",
        "startup_delay": 3.5,
        "env": {"KIMI_MODEL_THINKING_EFFORT": "low"},
    },
    "agy": {
        # D5 agent mode; lowest effort; the initial prompt goes in with -i as the hub does.
        "args_before_task": ["--effort", "low", "--mode", "auto"],
        "task_mode": "interactive_flag",
        "startup_delay": 2.5,
    },
}


def marker_session_id(engine: str) -> str:
    """An id no hub session has: the transcripts this recording creates can never be claimed."""
    return f"rtc-corpus-{engine}-{secrets.token_hex(4)}"


# The two ports a hub listens on; nothing this recorder starts may dial either of them.
HUB_PORTS = (4444, 4445)


def _mentions_hub_port(text: str) -> bool:
    return any(re.search(rf"(?<!\d){port}(?!\d)", text) for port in HUB_PORTS)


def mcp_entry_targets_hub(entry: Dict[str, object]) -> bool:
    """An MCP server entry (url or stdio command) that would reach a hub on 4444/4445."""
    parts = [str(entry.get("url") or ""), str(entry.get("command") or "")]
    args = entry.get("args")
    if isinstance(args, list):
        parts.extend(str(item) for item in args)
    elif args:
        parts.append(str(args))
    return any(_mentions_hub_port(part) for part in parts)


def codex_config_path() -> Path:
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home) / "config.toml"


def codex_mcp_disables(config_path: Optional[Path] = None) -> List[str]:
    """``-c mcp_servers.<name>.enabled=false`` for every server that targets 4444/4445.

    The name is never the criterion: a differently named server pointing at the live hub is disabled
    too, and ``agent-hub`` is only disabled when its url still is the hub's.  The file is read, never
    written.
    """
    path = config_path or codex_config_path()
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"cannot read {path} to isolate codex MCP servers: {exc}") from exc
    args: List[str] = []
    for name, entry in (data.get("mcp_servers") or {}).items():
        if isinstance(entry, dict) and mcp_entry_targets_hub(entry):
            args.extend(["-c", f"mcp_servers.{name}.enabled=false"])
    return args


def kimi_project_root(start: Path) -> Path:
    """kimi's project-root scope: the nearest ancestor holding ``.git`` (a dir or a worktree file)."""
    for path in (start, *start.parents):
        if (path / ".git").exists():
            return path
    return start


def kimi_mcp_files(task_dir: Path) -> List[Path]:
    """The three files kimi merges, in its own precedence order (later overrides earlier)."""
    home = os.environ.get("KIMI_CODE_HOME") or str(Path.home() / ".kimi-code")
    return [Path(home) / "mcp.json", kimi_project_root(task_dir) / ".mcp.json",
            task_dir / ".kimi-code" / "mcp.json"]


def assert_omp_mcp_isolated(extra_env: Dict[str, str]) -> Optional[str]:
    """omp has no MCP flag: it reads ``<PI_CODING_AGENT_DIR>/mcp.json``, and the user's copy lists the
    live hub.  The recorder requires the override, and only refuses when the user's own file would
    otherwise reach a hub port (an override pointing at an empty copy is the documented way)."""
    override = extra_env.get("PI_CODING_AGENT_DIR")
    if override:
        return None
    home = os.environ.get("PI_CODING_AGENT_DIR") or str(Path.home() / ".omp" / "agent")
    path = Path(home) / "mcp.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read {path} to isolate omp MCP servers: {exc}") from exc
    hits = [name for name, entry in (data.get("mcpServers") or {}).items()
            if isinstance(entry, dict) and mcp_entry_targets_hub(entry)]
    if hits:
        raise SystemExit(f"refusing to record omp: {path} lists MCP servers that point at a hub port "
                         f"({', '.join(sorted(hits))}) and omp has no MCP flag. Run with "
                         "--env PI_CODING_AGENT_DIR=<copy of the agent dir with an empty mcp.json>.")
    return str(path)


def agy_mcp_config_path() -> Optional[Path]:
    """The file ``agy mcp list`` reads: ``~/.gemini/config/mcp_config.json``."""
    override = os.environ.get("GEMINI_DIR")
    home = Path(override) if override else Path.home() / ".gemini"
    path = home / "config" / "mcp_config.json"
    return path if path.is_file() else None


def assert_agy_mcp_isolated() -> Optional[str]:
    """agy has no flag that empties the MCP set (``agy --help``; ``agy mcp disable`` writes the
    user's config, which the recorder must not do).  Refuse when the config it loads at start lists a
    server on a hub port -- ``agy mcp list`` shows exactly those entries."""
    path = agy_mcp_config_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read {path} to isolate agy MCP servers: {exc}") from exc
    hits = [name for name, entry in (data.get("mcpServers") or {}).items()
            if isinstance(entry, dict) and mcp_entry_targets_hub(entry)]
    if hits:
        raise SystemExit(f"refusing to record agy: {path} enables MCP servers that point at a hub "
                         f"port ({', '.join(sorted(hits))}) and agy has no session-scoped MCP flag. "
                         "Disable them (agy mcp disable <name>) or record with them removed.")
    return str(path)


def assert_kimi_mcp_isolated(task_dir: Path) -> List[str]:
    """kimi has no MCP flag and no MCP off switch (``kimi --help``; its bundle reads ``mcp.json``).

    ``KIMI_CODE_HOME`` would relocate the whole home (credentials included), so the only safe option
    is to refuse: any server in the three files that targets a hub port would be dialled, which
    RULES.md forbids.  Returns the files that were inspected.
    """
    inspected: List[str] = []
    hits: List[str] = []
    for path in kimi_mcp_files(task_dir):
        if not path.is_file():
            continue
        inspected.append(str(path))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"cannot read {path} to isolate kimi MCP servers: {exc}") from exc
        for name, entry in (data.get("mcpServers") or {}).items():
            if isinstance(entry, dict) and mcp_entry_targets_hub(entry):
                hits.append(f"{path} -> {name}")
    if hits:
        raise SystemExit("refusing to record kimi: its MCP configuration points at a hub port, and "
                         "kimi has no flag that empties the MCP set. Disable these servers first:\n  "
                         + "\n  ".join(hits))
    return inspected


def assert_port_closed(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(f"port {port} is in use; refusing to start an engine next to a listener")


def build_env(engine: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """AgentSession.start_pty's env policy plus this tool's isolation rules."""
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    # Nothing may point an engine at the live hub: drop every hub variable, including the ones this
    # session inherited, and point the two the CLI-side tooling reads at a closed port.
    for key in [k for k in env if k.upper().startswith("HUB_")]:
        env.pop(key, None)
    env["HUB_PORT"] = str(ISOLATED_PORT)
    env["HUB_API_URL"] = f"http://127.0.0.1:{ISOLATED_PORT}"
    # A nested Claude Code refuses to start, and these carry no rendering value for a recording.
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"):
        env.pop(key, None)
    env.update(ENGINES[engine].get("env") or {})
    env.update(extra or {})
    return env


def build_argv(engine: str, task: str, engine_session_id: Optional[str] = None
               ) -> Tuple[str, List[str]]:
    """The engine's own CLI with the task in the position the hub's task_mode puts it.

    ``engine_session_id`` is the hub's claude_positional behaviour (session_manager.py:12499): the
    hub names the conversation id up front, so the recorder knows which transcript it created.
    """
    spec = ENGINES[engine]
    command = HubSessionManager._default_engine_binary(engine)
    args = list(spec.get("args_before_task") or [])  # type: ignore[arg-type]
    if engine == "claude" and engine_session_id:
        args.extend(["--session-id", engine_session_id])
    if engine == "codex":
        # Read ~/.codex/config.toml (read-only) and disable every server that targets 4444/4445.
        args.extend(codex_mcp_disables())
    mode = spec["task_mode"]
    if mode == "interactive_flag":
        args.extend(["-i", task])
    elif mode == "positional":
        args.append(task)
    args.extend(spec.get("args_after_task") or [])  # type: ignore[arg-type]
    return command, args


class Recorder:
    def __init__(self, engine: str, out_dir: Path, task_dir: Path, task: str,
                 cols: int, rows: int, resize_after_idle_sec: float, idle_sec: float,
                 min_duration_sec: float, max_duration_sec: float,
                 extra_env: Optional[Dict[str, str]] = None):
        self.engine = engine
        self.out_dir = out_dir
        self.task_dir = task_dir
        self.task = task
        self.cols = cols
        self.rows = rows
        self.resize_after_idle_sec = resize_after_idle_sec
        self.idle_sec = idle_sec
        self.min_duration_sec = min_duration_sec
        self.max_duration_sec = max_duration_sec
        self.extra_env = dict(extra_env or {})
        # Latest moment for the resize when the answer never produces a quiet second.
        self.resize_deadline_sec = min_duration_sec + 20.0

        self.chunks: List[Tuple[int, str]] = []
        # The conversation id this engine will name its own session file with (claude: --session-id).
        self.engine_session_id: Optional[str] = None
        self.bytes_total = 0
        self.events: List[Dict[str, object]] = []
        self.resize: Optional[Dict[str, object]] = None
        self.exit_info: Dict[str, object] = {}
        # Provenance for the isolation claim in the module docstring, recorded per corpus.
        self.mcp_disabled: List[str] = []
        self.mcp_files: List[str] = []
        self._lock = threading.Lock()
        self._screen = HubTerminalScreen(cols, rows)
        self._stream = HubTerminalStream(self._screen)
        self._last_chunk_at = time.monotonic()
        self._last_prev_at: Optional[float] = None
        self._eof = threading.Event()
        self.pty: Optional[PtyProcess] = None
        self.pid: Optional[int] = None

    # ---- bookkeeping ---------------------------------------------------------------------
    def _now_ms(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _screen_text(self) -> str:
        with self._lock:
            try:
                lines = [line.rstrip() for line in self._screen.display]
            except Exception:
                lines = []
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines[-self.rows:])

    def _record_chunk(self, chunk: str, started: float) -> None:
        now = time.monotonic()
        prev = self._last_prev_at if self._last_prev_at is not None else started
        delay_ms = int(round((now - prev) * 1000))
        self._last_prev_at = now
        with self._lock:
            self.chunks.append((max(0, delay_ms), chunk))
            self.bytes_total += len(chunk.encode("utf-8", "replace"))
            try:
                self._stream.feed(chunk)
            except Exception:
                # K4: one bad sequence costs only itself; the corpus keeps the bytes regardless.
                pass
        self._last_chunk_at = now

    def _read_loop(self, started: float) -> None:
        while True:
            pty = self.pty
            if pty is None or not pty.isalive():
                break
            try:
                data = pty.read(READ_SIZE)
                if not data:
                    time.sleep(IDLE_SLEEP_SEC)
                    continue
                self._record_chunk(data, started)
            except EOFError:
                break
            except Exception:
                break
        self._eof.set()

    # ---- driving the TUI ------------------------------------------------------------------
    def _write(self, keys: str, started: float, reason: str) -> None:
        pty = self.pty
        if pty is None:
            return
        try:
            pty.write(keys)
        except Exception:
            return
        self.events.append({"event": "keys", "reason": reason, "at_ms": self._now_ms(started),
                            "bytes": len(keys.encode("utf-8", "replace"))})

    def _answer_prompts(self, started: float, answered: Dict[str, float]) -> None:
        text = self._screen_text()
        if not text:
            return
        now = time.monotonic()
        for rule in ANSWERS.get(self.engine, []):
            fired_at = answered.get(rule.name)
            if fired_at is not None and not (
                    rule.repeat_after_sec and (now - fired_at) >= rule.repeat_after_sec):
                continue
            if rule.guard is not None and not rule.guard.search(text):
                continue
            if not rule.pattern.search(text):
                continue
            answered[rule.name] = now
            time.sleep(rule.delay_after_match_sec)
            self._write(rule.keys.replace("{task}", self.task), started, rule.name)
            if rule.then_keys:
                time.sleep(rule.then_delay_sec)
                self._write(rule.then_keys, started, f"{rule.name}_submit")

    def _resize(self, started: float) -> None:
        pty = self.pty
        if pty is None:
            return
        before = (self.cols, self.rows)
        # The lock covers the winsize call and the chunk count together: a chunk the reader records
        # cannot slip between the two, so the fixture's boundary is exactly where the resize was.
        with self._lock:
            try:
                pty.setwinsize(RESIZE_ROWS, RESIZE_COLS)
            except Exception:
                return
            chunk_index = len(self.chunks)
            # The prompt detection reads this screen, so it must follow the PTY or it keeps guessing
            # at the old width (120x36) after the child has repainted for 140x44.
            try:
                self._screen.resize(RESIZE_ROWS, RESIZE_COLS)
            except Exception:
                pass
            self.cols, self.rows = RESIZE_COLS, RESIZE_ROWS
        self.resize = {"at_ms": self._now_ms(started), "chunk_index": chunk_index,
                       "from": list(before), "to": [RESIZE_COLS, RESIZE_ROWS]}

    # ---- lifecycle ------------------------------------------------------------------------
    def run(self) -> Dict[str, object]:
        env = build_env(self.engine, self.extra_env)
        task_mode = ENGINES[self.engine]["task_mode"]
        # kimi reads its MCP set from three files no flag can empty; refuse rather than dial a hub.
        if self.engine == "kimi":
            self.mcp_files = assert_kimi_mcp_isolated(self.task_dir)
        elif self.engine == "omp":
            inspected = assert_omp_mcp_isolated(self.extra_env)
            self.mcp_files = [inspected] if inspected else []
        elif self.engine == "agy":
            inspected = assert_agy_mcp_isolated()
            self.mcp_files = [inspected] if inspected else []
        self.engine_session_id = str(uuid.uuid4()) if self.engine == "claude" else None
        command, args = build_argv(self.engine, "" if task_mode == "pty_input" else self.task,
                                   self.engine_session_id)
        self.mcp_disabled = [part[len("mcp_servers."):-len(".enabled=false")] for part in args
                             if part.startswith("mcp_servers.") and part.endswith(".enabled=false")]
        cmd_list = unwrap_windows_cmd_shim(command, args, env=env)
        started = time.monotonic()
        self.pty = PtyProcess.spawn(cmd_list, cwd=str(self.task_dir), env=env,
                                    dimensions=(self.rows, self.cols))
        self.pid = self.pty.pid
        self.events.append({"event": "spawn", "at_ms": 0})

        reader = threading.Thread(target=self._read_loop, args=(started,), daemon=True)
        reader.start()

        startup_delay = float(ENGINES[self.engine]["startup_delay"])
        answered: Dict[str, float] = {}
        task_sent = False
        idle_since: Optional[float] = None
        resized = False
        exit_at: Optional[float] = None
        stop_reason = "max_duration"

        while True:
            now = time.monotonic()
            elapsed = now - started
            with self._lock:
                last_output = self._last_chunk_at
            quiet = now - last_output

            if not task_sent and elapsed >= startup_delay:
                # pty_input engines are typed by their readiness answer rule, never here: the hub
                # waits for the TUI before it types, and text written into a booting TUI is lost.
                task_sent = True

            self._answer_prompts(started, answered)

            if idle_since is None and quiet >= 1.0 and elapsed >= self.min_duration_sec:
                idle_since = last_output + 1.0
                self.events.append({"event": "answer_settled", "at_ms": self._now_ms(started)})

            # The resize goes in mid-recording, after the answer settled: the repaint it causes is
            # the chunk stream step 2 has to reproduce. An engine that never goes quiet (kimi paints
            # a spinner and a rotating tip) gets it at a deadline instead, still mid-recording.
            resize_due = idle_since is not None and (now - idle_since) >= self.resize_after_idle_sec
            if not resized and not resize_due and elapsed >= self.resize_deadline_sec:
                resize_due = True
            if not resized and resize_due:
                self._resize(started)
                resized = True
                idle_since = time.monotonic()
                self._last_chunk_at = idle_since

            if resized and exit_at is None and (now - self._last_chunk_at) >= self.idle_sec:
                self.events.append({"event": "idle_reached", "at_ms": self._now_ms(started)})
                self._write(EXIT_KEYS, started, "exit")
                exit_at = now

            if exit_at is not None and (self._eof.is_set() or (now - exit_at) >= 8.0):
                stop_reason = "exited" if self._eof.is_set() else "exit_timeout"
                break
            if elapsed >= self.max_duration_sec:
                stop_reason = "max_duration"
                break
            if self.bytes_total >= MAX_CORPUS_BYTES:
                stop_reason = "max_bytes"
                break
            if self._eof.is_set():
                stop_reason = "eof"
                break
            time.sleep(0.1)

        exit_code = self._shutdown(reader)
        duration_ms = self._now_ms(started)
        with self._lock:
            chunks = list(self.chunks)
        self.exit_info = {"stop_reason": stop_reason, "exit_code": exit_code,
                          "duration_ms": duration_ms}
        if stop_reason != "exited":
            self.events.append({"event": "killed", "at_ms": duration_ms})

        meta = {
            "kind": "session",
            "engine": self.engine,
            "cols": self.cols if self.resize is None else self.resize["from"][0],
            "rows": self.rows if self.resize is None else self.resize["from"][1],
            "resize": self.resize,
            "chunk_encoding": "text",
            "launch": {
                "command": Path(cmd_list[0]).name,
                "args": [Path(part).name if os.path.sep in part else part for part in cmd_list[1:]],
                "task_mode": task_mode,
            },
            "flags": {"ephemeral": task_mode != "pty_input",
                      "isolated_hub_port": ISOLATED_PORT,
                      "env_keys": sorted((ENGINES[self.engine].get("env") or {}).keys())
                                  + sorted(self.extra_env),
                      # What the isolation actually disabled or inspected: names, never values.
                      "mcp_disabled": self.mcp_disabled,
                      "mcp_files_inspected": len(self.mcp_files)},
            # Cleanup: the engine-named session file this run created, and the marker id that makes
            # any other transcript provably this recording's (deleted by hand after the run).
            "session_files": {"engine_session_id": self.engine_session_id},
            "task": self.task,
            "recorder": {
                "version": RECORDER_VERSION,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "chunk_count": len(chunks),
                "bytes": self.bytes_total,
                "max_chunk_bytes": max((len(c.encode("utf-8", "replace")) for _, c in chunks), default=0),
                "events": self.events,
                "exit": self.exit_info,
            },
        }
        self._write_case(chunks, meta)
        return meta

    def _shutdown(self, reader: threading.Thread) -> Optional[int]:
        pty = self.pty
        exit_code: Optional[int] = None
        if pty is not None:
            try:
                pty.terminate(force=True)
            except Exception:
                pass
        reader.join(timeout=5.0)
        if pty is not None:
            try:
                if self.pid and pty.isalive():
                    subprocess.run(["taskkill", "/PID", str(self.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            except Exception:
                pass
            try:
                exit_code = pty.exitstatus
            except Exception:
                exit_code = None
            try:
                pty.close()
            except Exception:
                pass
        return exit_code

    def _write_case(self, chunks: List[Tuple[int, str]], meta: Dict[str, object]) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        chunks_path = self.out_dir / "chunks.jsonl"
        with open(chunks_path, "w", encoding="utf-8", newline="\n") as fh:
            for delay_ms, chunk in chunks:
                fh.write(json.dumps({"delay_ms": delay_ms, "chunk": chunk}, ensure_ascii=False) + "\n")
        with open(self.out_dir / "meta.json", "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", required=True, choices=sorted(ENGINES))
    parser.add_argument("--out", required=True, type=Path,
                        help="case directory, e.g. tools/fixtures/terminal/live-claude")
    parser.add_argument("--task-dir", required=True, type=Path,
                        help="scratch folder holding the few dummy files the task lists")
    parser.add_argument("--task", default=None,
                        help="task text; the hub transcript marker is appended automatically")
    parser.add_argument("--marker-id", default=None,
                        help="rtc-corpus id (default: a fresh random one)")
    parser.add_argument("--cols", type=int, default=DEFAULT_COLS)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--resize-after-idle", type=float, default=3.0)
    parser.add_argument("--idle", type=float, default=20.0)
    parser.add_argument("--min-duration", type=float, default=20.0)
    parser.add_argument("--max-duration", type=float, default=MAX_DURATION_SEC)
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="one extra child variable, e.g. PI_CODING_AGENT_DIR=<isolated omp dir>")
    args = parser.parse_args(argv)

    task_dir = args.task_dir.resolve()
    if not task_dir.is_dir():
        raise SystemExit(f"task folder does not exist: {task_dir}")
    assert_port_closed(ISOLATED_PORT)
    marker_id = args.marker_id or marker_session_id(args.engine)
    base_task = args.task or ("list the files in this folder and say how many there are; change nothing")
    task = f"[Hub Session: session_id={marker_id}] {base_task}"

    extra_env: Dict[str, str] = {}
    for item in args.env:
        key, _, value = item.partition("=")
        if not key or not _:
            raise SystemExit(f"--env needs KEY=VALUE, got {item!r}")
        extra_env[key] = value

    recorder = Recorder(
        engine=args.engine,
        out_dir=args.out.resolve(),
        task_dir=task_dir,
        task=task,
        cols=args.cols,
        rows=args.rows,
        resize_after_idle_sec=args.resize_after_idle,
        idle_sec=args.idle,
        min_duration_sec=args.min_duration,
        max_duration_sec=min(args.max_duration, MAX_DURATION_SEC),
        extra_env=extra_env,
    )
    meta = recorder.run()
    recorded = meta["recorder"]  # type: ignore[index]
    print(json.dumps({"case": str(args.out), "marker_id": marker_id, **recorded},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
