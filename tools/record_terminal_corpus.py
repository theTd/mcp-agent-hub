"""Record one live engine session as a terminal corpus chunk stream (design doc §9 steps 0 and 2a).

The corpus is what the fixture gates compare; a new case's goldens can only come from the core now
(step 6 deleted the pyte oracle and its renderer, ``tools/render_terminal_fixtures.py``).  The
recorder's own live screen is a text session in the core, so a prompt it detects is a prompt the
real hub would see.  The default backend runs the engine on the **system** ConPTY through
``conpty_record_tap`` — the same spawn,
command-line, job and ``ReadFile`` code the Rust core runs — so the fixtures come from the producer the
core will use, not from the ``conpty.dll`` pywinpty bundles.  ``--backend pywinpty`` keeps the step-0
transport reachable until pywinpty is deleted in step 5.

Spawn mirrors the hub exactly: the hub's default 120x36 size, the env policy of
``AgentSession.start_pty`` (session_manager.py:2245), and argv passed through the hub's own
``unwrap_windows_cmd_shim`` (:1864).  Chunks are the bytes one ``ReadFile`` returned, in the tap's
8 KiB buffer — the size the core's reader uses too — each with the tap's own timestamp, so the stream
can be replayed instead of recorded again.  Only the bookkeeping after the read differs from the hub's.

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

A recording normally ends the way a hub session would: a resize mid-recording, then ``/exit``.  Two
stop points end it at a screen a consumer reads instead, because a consumer's input is not the
engine's exit:

- ``--followup FILE`` types FILE's text into the composer once the first idle is reached, waits for
  the screen to stop changing, and ends the run there (the Codex settle loop reads the composer
  before its Enter, so the draft must stay unsent).
- ``--deny-box [TEXT]`` stops at the first approval box -- or the first whose command contains
  TEXT -- once that box has stood still, and denies it with Escape: never an approval (RULES.md: the
  recorder approves nothing that writes).  TEXT is matched against the screen with all whitespace
  removed, because a box wraps its command.

Both suppress the mid-recording resize and the exit keys (a stop point replaces that script), and
both record ``consumer`` in meta.json: ``reads_at_chunk`` is the number of chunks a replay feeds to
reproduce exactly the screen the consumer read, ``settled`` whether the screen stood still before
that, and -- for ``--followup`` -- ``expected_text``, the text the settle loop takes as the payload
it must see.  A ``--deny-box`` run records the box's screen before the denial, so a replay that stops
there is the consumer's input, not the box's aftermath.

Chunks are recorded as ``{"delay_ms": <since the previous chunk>, "chunk": …}`` in ``chunks.jsonl``.
With the system backend ``chunk`` is **base64 of the exact bytes** one read returned
(``chunk_encoding: "utf8_bytes"`` in meta.json), because the core decodes UTF-8 itself with a carry and
a character split across two reads must stay split; with ``--backend pywinpty`` it is the text
``pty.read`` returned (``chunk_encoding: "text"``), as step 0 recorded it.  Personal data is removed
afterwards by ``tools/terminal_corpus_scrub.py``; never commit a raw recording.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import json
import os
import platform
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from session_manager import (  # noqa: E402
    HubSessionManager,
    unwrap_windows_cmd_shim,
)
# The recorder's own live screen is a text session in the core: the same parser the hub runs, with a
# screen and no process, fed the text the pipe produced (step 6 deleted the pyte oracle).
from terminal_core import Core  # noqa: E402
from winpty import PtyProcess  # noqa: E402

# The default backend: the core's own spawn/read code as a headless tap (conpty_record_tap.rs).
TAP_EXE = REPO_ROOT / "terminal-core" / "target" / "release" / "conpty_record_tap.exe"
# The read size the tap uses (conpty_session/src/session.rs `IO_CHUNK`): one frame is one read.
TAP_READ_BYTES = 8192
# The tap's frame header on both pipes: kind, then a little-endian u32 payload length.
TAP_FRAME_HEADER = struct.Struct("<BI")
# CREATE_NO_WINDOW: the tap is a console binary, and nothing may appear on the desktop.
CREATE_NO_WINDOW = 0x08000000
# 3: meta.json carries ``consumer`` (the chunk index a stop point's screen is replayed to).
RECORDER_VERSION = 3
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
# The size asked of the backend's ``read``. The tap ignores it: one frame is one ReadFile on the
# core's 8 KiB buffer (TAP_READ_BYTES), which is the chunk boundary this corpus records. pywinpty
# reads up to 4096, as AgentSession._read_loop does (session_manager.py:2282).
READ_SIZE = 4096
IDLE_SLEEP_SEC = 0.02
# A stop point waits for the screen a consumer reads to stand still this long before it records where
# a replay ends on it (and, for a box, before the keys change it).
CONSUMER_SETTLE_SEC = 1.5
# The engine may still be drawing that screen (Codex takes a long paste in a few words per frame), so
# the wait has a bound; running out is recorded on the event rather than passed off as settled.
CONSUMER_SETTLE_MAX_SEC = 60.0
# The rows of an approval box: its header, and the frame rows that close it (omp draws the options
# and the key hints inside the same frame). What a snapshot waits on stands still between them.
BOX_HEADER = "Allow tool:"
BOX_CLOSING_ROWS = ("\u251c", "\u2570", "\u2514", "\u2523", "\u2517")


class Answer:
    """One screen prompt this recorder answers, and the keys it types (the hub types keys too).

    ``guard_flat`` is a guard matched against the screen with every whitespace character removed: an
    approval box wraps a long command, so the literal it must contain is split across rows on the
    screen but whole in that form.  ``snapshot`` marks the matched screen as the one a consumer reads:
    the screen (or ``snapshot_focus``: "box" = the approval box's own rows) is allowed to stand
    still, ``consumer.reads_at_chunk`` is recorded for it, and only then are the keys sent.
    ``end_after`` ends the run once the screen settles after those keys.
    """

    def __init__(self, name: str, pattern: str, keys: str, guard: Optional[str] = None,
                 delay_after_match_sec: float = 1.0, repeat_after_sec: float = 0.0,
                 then_keys: str = "", then_delay_sec: float = 0.0,
                 guard_flat: Optional[str] = None, snapshot: bool = False,
                 snapshot_focus: str = "", end_after: bool = False, consumer_why: str = ""):
        self.name = name
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.guard = re.compile(guard, re.IGNORECASE) if guard else None
        self.guard_flat = re.compile(guard_flat, re.IGNORECASE) if guard_flat else None
        self.keys = keys
        self.delay_after_match_sec = delay_after_match_sec
        # A tool prompt can appear several times in one session; answering it once is not enough.
        self.repeat_after_sec = repeat_after_sec
        # A second keystroke after the text settled: kimi's composer needs its Enter as its own
        # write once the pasted task has been rendered (the hub's checked submit does the same).
        self.then_keys = then_keys
        self.then_delay_sec = then_delay_sec
        self.snapshot = snapshot
        self.snapshot_focus = snapshot_focus
        self.end_after = end_after
        self.consumer_why = consumer_why


# A permission prompt is only ever approved for a read-only command: the task is "list the files",
# and RULES.md forbids approving anything that could write outside the scratch task folder.
READ_ONLY_COMMAND = r"\b(?:ls|dir|find|findstr|type|cat|Get-ChildItem|Select-Object|Measure-Object|wc)\b"

ANSWERS: Dict[str, List[Answer]] = {
    "claude": [
        Answer("trust_folder", r"Do you trust the files in this folder", "\r", delay_after_match_sec=0.5),
        # Newer builds draw a two-line choice whose cursor starts on "No, exit": the only way to
        # trust the folder is Down (to "Yes, I trust this folder"), then Enter.
        Answer("trust_folder_choice", r"Yes, I trust this folder", "\x1b[B\r", guard=r"No, exit",
               delay_after_match_sec=1.0, repeat_after_sec=5.0),
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
        # The binary must be the hub's own profile launcher (--command ...\reclaude.exe): a bare
        # ``claude`` answers "Not logged in" because the hub never launches it directly.
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


def windows_build() -> str:
    """The full OS build (``10.0.26220.9836``): platform.version() omits the UBR."""
    version = platform.version()
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            ubr = winreg.QueryValueEx(key, "UBR")[0]
    except OSError:
        return version
    return f"{version}.{int(ubr)}"


def engine_binary(engine: str, override: Optional[str] = None) -> str:
    """The engine's executable, as the hub's profile launches it.

    ``--command`` carries the one profile-specific binary the recorder must use instead of the
    default: the hub's claude profile launches the owner's account wrapper (``reclaude.exe``), not a
    bare ``claude``, so a bare one would answer "Not logged in". The path stays on the command line,
    never in meta.json.
    """
    if override:
        return override
    return HubSessionManager._default_engine_binary(engine)


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


def build_argv(engine: str, task: str, engine_session_id: Optional[str] = None,
               command: Optional[str] = None) -> Tuple[str, List[str]]:
    """The engine's own CLI with the task in the position the hub's task_mode puts it.

    ``engine_session_id`` is the hub's claude_positional behaviour (session_manager.py:12499): the
    hub names the conversation id up front, so the recorder knows which transcript it created.
    ``command`` overrides the engine's binary (``--command``): the hub's claude profile launches the
    account wrapper, and a bare ``claude`` would answer "Not logged in".
    """
    spec = ENGINES[engine]
    command = engine_binary(engine, command)
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


class TapFailed(RuntimeError):
    """The tap could not spawn the engine, or died before it reported one."""


class TapSession:
    """One engine on the system ConPTY, driven through ``conpty_record_tap``.

    The surface is the one ``winpty.PtyProcess`` gave the step-0 recorder (``read``, ``write``,
    ``setwinsize``, ``isalive``, ``terminate``, ``exitstatus``, ``pid``, ``close``), plus the tap's own
    clock: ``read`` returns ``(bytes, at_us)`` where ``at_us`` is when the read returned, so the
    recorded delays are the tap's rather than Python's.
    """

    def __init__(self, exe: Path, cmd_list: List[str], cwd: Path, env: Dict[str, str],
                 cols: int, rows: int):
        if not exe.is_file():
            raise SystemExit(f"tap not built: {exe}\nbuild: cargo build --release -p conpty_session "
                             "--bin conpty_record_tap (from terminal-core/)")
        argv = [str(exe), "--cols", str(cols), "--rows", str(rows), "--cwd", str(cwd), "--"]
        argv.extend(cmd_list)
        self._lock = threading.Lock()
        self._log: List[str] = []
        self._exit_code: Optional[int] = None
        self._eof = False
        # CREATE_NO_WINDOW plus pipes: the tap and its ConPTY child never show a window. Its stderr is
        # dropped because the tap reports its own failures as a frame.
        self._proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        self.pid = self._await_spawn()

    def _await_spawn(self) -> int:
        while True:
            frame = self._read_frame()
            if frame is None:
                code = self._proc.poll()
                raise TapFailed(f"tap exited ({code}) before reporting its spawn" +
                                (f": {self._log[-1]}" if self._log else ""))
            kind, payload = frame
            if kind == b"L":
                self._log.append(payload.decode("utf-8", "replace"))
            elif kind == b"F":
                raise TapFailed(payload.decode("utf-8", "replace"))
            elif kind == b"S":
                if len(payload) < 8:
                    raise TapFailed("spawn frame is short")
                return struct.unpack_from("<I", payload, 0)[0]

    def _read_frame(self) -> Optional[Tuple[bytes, bytes]]:
        assert self._proc.stdout is not None
        header = self._proc.stdout.read(TAP_FRAME_HEADER.size)
        if not header or len(header) != TAP_FRAME_HEADER.size:
            return None
        kind, length = TAP_FRAME_HEADER.unpack(header)
        payload = self._proc.stdout.read(length) if length else b""
        if payload is None or len(payload) != length:
            return None
        return bytes([kind]), payload

    def _send(self, kind: bytes, payload: bytes = b"") -> bool:
        with self._lock:
            if self._proc.stdin is None or self._proc.stdin.closed:
                return False
            try:
                self._proc.stdin.write(TAP_FRAME_HEADER.pack(kind[0], len(payload)) + payload)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return False
        return True

    def read(self, size: int) -> Optional[Tuple[bytes, int]]:
        """The next ReadFile result and its tap time; ``None`` once the tap reported its exit."""
        while True:
            frame = self._read_frame()
            if frame is None:
                self._eof = True
                return None
            kind, payload = frame
            if kind == b"C":
                if len(payload) < 8:
                    continue
                return payload[8:], struct.unpack_from("<Q", payload, 0)[0]
            if kind == b"L":
                self._log.append(payload.decode("utf-8", "replace"))
            elif kind == b"F":
                self._eof = True
                raise TapFailed(payload.decode("utf-8", "replace"))
            elif kind == b"E":
                self._exit_code = struct.unpack("<i", payload)[0]
                self._eof = True
                return None

    def write(self, keys: str) -> None:
        self._send(b"I", keys.encode("utf-8", "replace"))

    def setwinsize(self, rows: int, cols: int) -> None:
        self._send(b"R", struct.pack("<HH", cols, rows))

    def isalive(self) -> bool:
        return not self._eof and self._exit_code is None

    def terminate(self, force: bool = True) -> None:
        """Kill the engine's job; the tap's job handle also kills the tree if this process dies."""
        self._send(b"K")
        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    @property
    def exitstatus(self) -> Optional[int]:
        return self._exit_code if self._exit_code is not None else self._proc.returncode

    @property
    def log(self) -> List[str]:
        return list(self._log)

    def close(self) -> None:
        for stream in (self._proc.stdin, self._proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if self._proc.poll() is None:
            self._proc.kill()


class PywinptySession:
    """``winpty.PtyProcess`` behind ``TapSession``'s surface: the step-0 transport, text chunks."""

    def __init__(self, cmd_list: List[str], cwd: Path, env: Dict[str, str], cols: int, rows: int):
        self._pty = PtyProcess.spawn(cmd_list, cwd=str(cwd), env=env, dimensions=(rows, cols))
        self.pid = self._pty.pid

    def read(self, size: int) -> Optional[Tuple[str, None]]:
        try:
            data = self._pty.read(size)
        except EOFError:
            return None
        if not data:
            return None
        return data, None

    def write(self, keys: str) -> None:
        self._pty.write(keys)

    def setwinsize(self, rows: int, cols: int) -> None:
        self._pty.setwinsize(rows, cols)

    def isalive(self) -> bool:
        return bool(self._pty.isalive())

    def terminate(self, force: bool = True) -> None:
        self._pty.terminate(force=force)

    @property
    def exitstatus(self) -> Optional[int]:
        try:
            return self._pty.exitstatus
        except Exception:
            return None

    @property
    def log(self) -> List[str]:
        return []

    def close(self) -> None:
        try:
            self._pty.close()
        except Exception:
            pass


class Recorder:
    def __init__(self, engine: str, out_dir: Path, task_dir: Path, task: str,
                 cols: int, rows: int, resize_after_idle_sec: float, idle_sec: float,
                 min_duration_sec: float, max_duration_sec: float,
                 extra_env: Optional[Dict[str, str]] = None, backend: str = "system",
                 tap: Optional[Path] = None, command: Optional[str] = None,
                 followup_text: Optional[str] = None, deny_box: Optional[str] = None):
        if backend not in ("system", "pywinpty"):
            raise SystemExit(f"unknown backend {backend!r}")
        self.engine = engine
        self.backend = backend
        self.tap = Path(tap) if tap else TAP_EXE
        self.command = command
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
        # A stop point replaces the resize-and-exit script: the consumer reads the screen there, and
        # a repaint or a typed "/exit" would change it.
        self.followup_text = followup_text
        self.deny_box = deny_box
        self.scripted_stop = followup_text is not None or deny_box is not None
        self.answers = list(ANSWERS[engine])
        if deny_box is not None:
            # Never an approval: Escape denies the box once (D7), after the box's own screen has been
            # recorded, and the run ends on it. It goes first so no other rule can answer the box; an
            # empty guard denies whichever box comes first.
            self.answers.insert(0, Answer(
                "deny_box", r"Allow tool:", "\x1b",
                guard_flat=re.escape(deny_box) if deny_box else None,
                delay_after_match_sec=0.0, snapshot=True, snapshot_focus="box", end_after=True,
                consumer_why="the OMP approval box is on screen here, before its denial",
            ))
        # What a replay has to feed to arrive at the screen a consumer reads (meta.json "consumer").
        self.consumer: Optional[Dict[str, object]] = None
        # Latest moment for the resize when the answer never produces a quiet second.
        self.resize_deadline_sec = min_duration_sec + 20.0
        # The system ConPTY hands back pipe bytes; the core decodes them with a carry, so the corpus
        # keeps them as bytes. pywinpty already returns text and is kept as step 0 recorded it.
        self.chunk_encoding = "utf8_bytes" if backend == "system" else "text"

        self.chunks: List[Tuple[int, Union[str, bytes]]] = []
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
        # The screen the prompt rules read: a text session in the core, the hub's own parser. The
        # core's clamp floors are the hub's (20x5), so a smaller --cols/--rows is raised to them.
        self.core = Core(notify=lambda: None, log=lambda line: None)
        self._core_id = f"recorder-{uuid.uuid4().hex}"
        self.core.attach_text(self._core_id, cols=max(20, cols), rows=max(5, rows))
        # The screen sees the same text the hub will: one incremental decoder, so a character split
        # across two reads is fed once the second half arrives.
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._last_chunk_at = time.monotonic()
        self._last_prev_at: Optional[float] = None
        self._last_chunk_us: Optional[int] = None
        self._eof = threading.Event()
        self._read_error: Optional[str] = None
        self.pty: Optional[Union[TapSession, PywinptySession]] = None
        self.pid: Optional[int] = None

    # ---- bookkeeping ---------------------------------------------------------------------
    def _now_ms(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _screen_text(self) -> str:
        with self._lock:
            try:
                text = self.core.screen(self._core_id, self.rows)["text"]
            except Exception:
                text = ""
        return text

    def _settle_screen(self, focus: Optional[Callable[[str], str]] = None) -> bool:
        """Waits until the screen has not changed for ``CONSUMER_SETTLE_SEC``; False on the bound.

        The screen text, not the chunk arrival, is the signal: a blinking cursor or a spinner redraw
        keeps the pipe busy while what a consumer reads stays the same.  ``focus`` narrows the
        compared text to what the consumer parses: omp repaints its spinner and clock under the
        approval box, so the box rows are what has to stand still, not the whole screen.
        """
        def current() -> str:
            text = self._screen_text()
            return focus(text) if focus is not None else text

        deadline = time.monotonic() + CONSUMER_SETTLE_MAX_SEC
        last = current()
        changed_at = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.1)
            text = current()
            if text != last or (focus is not None and not text):
                last, changed_at = text, time.monotonic()
            elif time.monotonic() - changed_at >= CONSUMER_SETTLE_SEC:
                return True
        return False

    @staticmethod
    def _box_rows(screen_text: str) -> str:
        """The approval box's own rows: its ``Allow tool:`` header to the frame row that closes it.

        A box is drawn row by row and omp keeps repainting the status lines under it; the box's rows
        are what an approval consumer parses, so they are what a snapshot waits on.
        """
        lines = screen_text.splitlines()
        head = max((index for index, line in enumerate(lines) if BOX_HEADER in line), default=None)
        if head is None:
            return ""
        rows = [lines[head]]
        for line in lines[head + 1:]:
            rows.append(line)
            if line.lstrip()[:1] in BOX_CLOSING_ROWS:
                break
        return "\n".join(rows)

    def _mark_consumer(self, started: float, event: str, why: str, settled: bool,
                       **extra: object) -> None:
        """Records where a replay of the chunk stream ends on the screen a consumer reads.

        ``extra`` carries what the consumer's call needs besides the screen: a follow-up stop point
        records the text it typed, which is the settle loop's ``expected_text``.
        """
        with self._lock:
            chunk_index = len(self.chunks)
        self.consumer = {
            "reads_at_chunk": chunk_index,
            "reason": why,
            "settled": settled,
            **extra,
        }
        self.events.append({"event": "consumer_read", "reason": event,
                            "at_ms": self._now_ms(started), "chunk_index": chunk_index,
                            "settled": settled})

    @staticmethod
    def _chunk_bytes(chunk: Union[str, bytes]) -> bytes:
        return chunk.encode("utf-8", "replace") if isinstance(chunk, str) else chunk

    def _chunk_text(self, chunk: Union[str, bytes]) -> str:
        """The text the hub's stream would see; the carry keeps a split character out of the screen."""
        return chunk if isinstance(chunk, str) else self._decoder.decode(chunk, final=False)

    def _record_chunk(self, chunk: Union[str, bytes], at_us: Optional[int], started: float) -> None:
        now = time.monotonic()
        if at_us is not None:
            # The tap's own read clock: the delay is between two ReadFile returns, not two Python ones.
            previous_us = self._last_chunk_us if self._last_chunk_us is not None else 0
            delay_ms = int(round((at_us - previous_us) / 1000))
            self._last_chunk_us = at_us
        else:
            previous = self._last_prev_at if self._last_prev_at is not None else started
            delay_ms = int(round((now - previous) * 1000))
        self._last_prev_at = now
        data = self._chunk_bytes(chunk)
        text = self._chunk_text(chunk)
        with self._lock:
            self.chunks.append((max(0, delay_ms), chunk))
            self.bytes_total += len(data)
            try:
                if text:
                    self.core.feed(self._core_id, text)
            except Exception:
                # The core's parser cannot fail; a retired session would, and the corpus keeps the
                # bytes regardless.
                pass
        self._last_chunk_at = now

    def _read_loop(self, started: float) -> None:
        while True:
            pty = self.pty
            if pty is None or not pty.isalive():
                break
            try:
                result = pty.read(READ_SIZE)
                if result is None:
                    time.sleep(IDLE_SLEEP_SEC)
                    continue
                data, at_us = result
                self._record_chunk(data, at_us, started)
            except (EOFError, TapFailed) as exc:
                self._read_error = str(exc) or type(exc).__name__
                break
            except Exception as exc:
                self._read_error = f"{type(exc).__name__}: {exc}"
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

    def _answer_prompts(self, started: float, answered: Dict[str, float]) -> bool:
        """Answers the prompts on screen; True when a rule with ``end_after`` fired."""
        text = self._screen_text()
        if not text:
            return False
        flat = "".join(text.split())
        now = time.monotonic()
        for rule in self.answers:
            fired_at = answered.get(rule.name)
            if fired_at is not None and not (
                    rule.repeat_after_sec and (now - fired_at) >= rule.repeat_after_sec):
                continue
            if rule.guard is not None and not rule.guard.search(text):
                continue
            if rule.guard_flat is not None and not rule.guard_flat.search(flat):
                continue
            if not rule.pattern.search(text):
                continue
            answered[rule.name] = now
            if rule.snapshot:
                # The screen a consumer reads is this one, not what the keys make of it: let it stand
                # still and record where a replay ends on it before the keys change anything.
                focus = self._box_rows if rule.snapshot_focus == "box" else None
                self._mark_consumer(started, rule.name, rule.consumer_why,
                                    self._settle_screen(focus))
            if rule.delay_after_match_sec:
                time.sleep(rule.delay_after_match_sec)
            self._write(rule.keys.replace("{task}", self.task), started, rule.name)
            if rule.then_keys:
                time.sleep(rule.then_delay_sec)
                self._write(rule.then_keys, started, f"{rule.name}_submit")
            if rule.end_after:
                return True
        return False

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
                self.core.resize(self._core_id, RESIZE_COLS, RESIZE_ROWS)
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
                                   self.engine_session_id, self.command)
        self.mcp_disabled = [part[len("mcp_servers."):-len(".enabled=false")] for part in args
                             if part.startswith("mcp_servers.") and part.endswith(".enabled=false")]
        cmd_list = unwrap_windows_cmd_shim(command, args, env=env)
        started = time.monotonic()
        if self.backend == "system":
            self.pty = TapSession(self.tap, cmd_list, self.task_dir, env, self.cols, self.rows)
        else:
            self.pty = PywinptySession(cmd_list, self.task_dir, env, self.cols, self.rows)
        self.pid = self.pty.pid
        self.events.append({"event": "spawn", "at_ms": 0})

        reader = threading.Thread(target=self._read_loop, args=(started,), daemon=True)
        reader.start()

        startup_delay = float(ENGINES[self.engine]["startup_delay"])
        answered: Dict[str, float] = {}
        task_sent = False
        idle_since: Optional[float] = None
        resized = False
        followup_at: Optional[float] = None
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

            if self._answer_prompts(started, answered):
                # A box the run exists for was read and denied; nothing may answer it now.
                self._settle_screen()
                stop_reason = "box_denied"
                break

            if idle_since is None and quiet >= 1.0 and elapsed >= self.min_duration_sec:
                idle_since = last_output + 1.0
                self.events.append({"event": "answer_settled", "at_ms": self._now_ms(started)})

            if self.followup_text is not None and idle_since is not None and followup_at is None:
                # The first idle is the moment a follow-up is typed: the composer holds the draft, and
                # the consumer reads it once Codex has taken every character and the screen is still.
                followup_at = now
                self._write(self.followup_text, started, "followup")
                settled = self._settle_screen()
                self._mark_consumer(
                    started, "followup",
                    "the Codex settle loop reads the composer here: the follow-up was typed, Codex "
                    "took all of it, and the screen has stood still since",
                    settled, expected_text=self.followup_text,
                )
                stop_reason = "followup_settled" if settled else "followup_timeout"
                break

            # The resize goes in mid-recording, after the answer settled: the repaint it causes is
            # the chunk stream step 2 has to reproduce. An engine that never goes quiet (kimi paints
            # a spinner and a rotating tip) gets it at a deadline instead, still mid-recording. A
            # stop point replaces this whole script: no repaint, and no "/exit" typed into a screen
            # a consumer reads.
            if not self.scripted_stop:
                resize_due = (idle_since is not None
                              and (now - idle_since) >= self.resize_after_idle_sec)
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
            "chunk_encoding": self.chunk_encoding,
            # Which ConPTY produced the bytes (owner decision terminal-core-conpty) and the OS build
            # whose conhost/OpenConsole wrote them: a rebuild on another build must say so.
            "pty": "system-conpty" if self.backend == "system" else "pywinpty",
            "windows_build": windows_build(),
            "read_buffer_bytes": TAP_READ_BYTES if self.backend == "system" else None,
            # Which resume the ``resumed/`` goldens model (the corpus gate).
            "resume_count": 1,
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
            # A stop point's screen: how many chunks a replay feeds to reach exactly the screen the
            # consumer read (steps 3's live captures; test_terminal_live_consumers.py).
            "consumer": self.consumer,
            "recorder": {
                "version": RECORDER_VERSION,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "chunk_count": len(chunks),
                "bytes": self.bytes_total,
                "max_chunk_bytes": max((len(self._chunk_bytes(c)) for _, c in chunks), default=0),
                "events": self.events,
                "exit": self.exit_info,
                "read_error": self._read_error,
            },
        }
        self._write_case(chunks, meta)
        self._release_core()
        return meta

    def _release_core(self) -> None:
        """Ends the recorder's text session and its threads before the tool's process exits."""
        try:
            self.core.kill_all(2000)
        except Exception:
            pass
        try:
            self.core.shutdown()
        except Exception:
            pass

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

    def _write_case(self, chunks: List[Tuple[int, Union[str, bytes]]],
                    meta: Dict[str, object]) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        chunks_path = self.out_dir / "chunks.jsonl"
        with open(chunks_path, "w", encoding="utf-8", newline="\n") as fh:
            for delay_ms, chunk in chunks:
                # Byte chunks are base64 of the exact read: JSON cannot carry a split character, and the
                # core's carry must see the split the pipe produced.
                payload = (chunk if isinstance(chunk, str)
                           else base64.b64encode(chunk).decode("ascii"))
                fh.write(json.dumps({"delay_ms": delay_ms, "chunk": payload},
                                    ensure_ascii=False) + "\n")
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
    parser.add_argument("--backend", choices=("system", "pywinpty"), default="system",
                        help="PTY backend: the system ConPTY through the tap (default), or pywinpty")
    parser.add_argument("--tap", type=Path, default=TAP_EXE,
                        help="the conpty_record_tap binary (default: the crate's release build)")
    parser.add_argument("--command", default=None,
                        help="the engine binary to launch instead of the hub's default, e.g. the "
                             "reclaude.exe account wrapper the hub's claude profile uses")
    parser.add_argument("--followup", type=Path, default=None, metavar="FILE",
                        help="stop point: type FILE's text into the composer at the first idle, wait "
                             "for the screen to stand still, and end there (no resize, no exit keys)")
    parser.add_argument("--deny-box", nargs="?", const="", default=None, metavar="TEXT",
                        help="stop point: stop at the first approval box (or the first whose command "
                             "contains TEXT, with whitespace removed), deny it with Escape, and end "
                             "there (no resize, no exit keys)")
    args = parser.parse_args(argv)

    task_dir = args.task_dir.resolve()
    if not task_dir.is_dir():
        raise SystemExit(f"task folder does not exist: {task_dir}")
    if args.followup is not None and args.deny_box is not None:
        raise SystemExit("--followup and --deny-box are two different stop points; pass one")
    followup_text: Optional[str] = None
    if args.followup is not None:
        try:
            followup_text = args.followup.read_text(encoding="utf-8").strip("\r\n")
        except OSError as exc:
            raise SystemExit(f"cannot read the follow-up text {args.followup}: {exc}") from exc
        if not followup_text:
            raise SystemExit(f"the follow-up file {args.followup} is empty")
        # One write of one line: a newline would submit the draft instead of leaving it on screen.
        followup_text = " ".join(followup_text.split())
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
        backend=args.backend,
        tap=args.tap,
        command=args.command,
        followup_text=followup_text,
        deny_box=args.deny_box,
    )
    meta = recorder.run()
    recorded = meta["recorder"]  # type: ignore[index]
    if not recorded["chunk_count"]:
        raise SystemExit(f"no output recorded: {recorded.get('read_error') or 'the engine printed nothing'}")
    print(json.dumps({"case": str(args.out), "marker_id": marker_id, **recorded},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
