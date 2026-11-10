"""Desktop-shell hub API: /api/hub/info and the token-guarded /api/hub/shutdown.

Starts two throwaway hubs from this checkout on test ports (never 4444):
  - without HUB_SHUTDOWN_TOKEN: info answers, shutdown is 404;
  - with a token: a missing or wrong token is 403, the right one answers {"ok": true} and the
    hub stops gracefully (uvicorn's Ctrl+C path, lifespan shutdown included) with exit code 0.

Run: <venv python> test_hub_desktop_api.py
"""

import io
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from contextlib import closing

import httpx

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")

HUB_DIR = Path(__file__).parent.resolve()
# K3: the interpreter this suite runs on, not a guessed <repo>/.venv (a worktree has none).
PYTHON_EXE = sys.executable
def unused_port():
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PORT_NO_TOKEN = int(os.environ.get("HUB_TEST_PORT_A") or unused_port())
PORT_TOKEN = int(os.environ.get("HUB_TEST_PORT_B") or unused_port())

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def start_hub(port: int, token: str, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["HUB_PORT"] = str(port)
    data_dir = log_path.parent / log_path.stem
    data_dir.mkdir(parents=True, exist_ok=True)
    profiles = data_dir / "profiles.json"
    profiles.write_text(json.dumps({"profiles": {}}), encoding="utf-8")
    env["HUB_DATA_DIR"] = str(data_dir)
    env["HUB_PROFILES_PATH"] = str(profiles)
    env.pop("HUB_SHUTDOWN_TOKEN", None)
    if token:
        env["HUB_SHUTDOWN_TOKEN"] = token
    # Exercise the real entry point and signal path without CLI/model discovery.
    entry = (
        "import runpy, session_manager; "
        "session_manager.HubSessionManager._initial_discovery = "
        "lambda manager: manager._discovery_ready.set(); "
        "runpy.run_path('server.py', run_name='__main__')"
    )
    with open(log_path, "wb") as log:
        return subprocess.Popen(
            [PYTHON_EXE, "-c", entry], cwd=str(HUB_DIR), env=env,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"hub on {port} exited early with {proc.returncode}")
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/hub/info", timeout=2.0)
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"hub on {port} not ready after {timeout}s")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        proc.wait(timeout=10)


def scenario_no_token(log_dir: Path) -> None:
    print(f"\n--- hub without HUB_SHUTDOWN_TOKEN on {PORT_NO_TOKEN} ---")
    proc = start_hub(PORT_NO_TOKEN, "", log_dir / "hub-no-token.log")
    try:
        info = wait_ready(PORT_NO_TOKEN, proc)
        check("info.service", info.get("service") == "mcp-agent-hub", repr(info.get("service")))
        check("info.port", info.get("port") == PORT_NO_TOKEN, repr(info.get("port")))
        check("info.pid is an int", isinstance(info.get("pid"), int), repr(info.get("pid")))
        check("info.repo", Path(str(info.get("repo"))).resolve() == HUB_DIR, repr(info.get("repo")))
        check("info.started_at", bool(info.get("started_at")), repr(info.get("started_at")))
        check("info.shutdown_enabled false", info.get("shutdown_enabled") is False)
        r = httpx.post(
            f"http://127.0.0.1:{PORT_NO_TOKEN}/api/hub/shutdown",
            headers={"X-Hub-Shutdown-Token": "anything"},
            timeout=5.0,
        )
        check("shutdown without token env -> 404", r.status_code == 404, str(r.status_code))
        time.sleep(1.0)
        check("hub still running after 404", proc.poll() is None)
    finally:
        stop(proc)


def scenario_token(log_dir: Path) -> None:
    print(f"\n--- hub with HUB_SHUTDOWN_TOKEN on {PORT_TOKEN} ---")
    token = secrets.token_hex(16)
    log_path = log_dir / "hub-token.log"
    proc = start_hub(PORT_TOKEN, token, log_path)
    try:
        info = wait_ready(PORT_TOKEN, proc)
        check("info.shutdown_enabled true", info.get("shutdown_enabled") is True)
        url = f"http://127.0.0.1:{PORT_TOKEN}/api/hub/shutdown"
        r = httpx.post(url, timeout=5.0)
        check("shutdown without header -> 403", r.status_code == 403, str(r.status_code))
        r = httpx.post(url, headers={"X-Hub-Shutdown-Token": token + "x"}, timeout=5.0)
        check("shutdown with wrong token -> 403", r.status_code == 403, str(r.status_code))
        check("hub still running after 403", proc.poll() is None)

        # Create a live pwsh session and hold an open long-poll connection so shutdown must
        # archive the session first and unblock/close open streams within the 10 s grace window.
        sr = httpx.post(
            f"http://127.0.0.1:{PORT_TOKEN}/api/sessions",
            json={"profile_id": "pwsh", "title": "shutdown-archive-check"},
            timeout=15.0,
        )
        session_id = (sr.json().get("session") or {}).get("session_id", "")
        check("created live pwsh session before shutdown", bool(session_id), session_id)

        poll_done = threading.Event()
        def _hold_long_poll() -> None:
            try:
                httpx.get(
                    f"http://127.0.0.1:{PORT_TOKEN}/api/mailbox/poll?session_id=all&after_seq=999999&timeout=60",
                    timeout=65.0,
                )
            except Exception:
                pass
            finally:
                poll_done.set()

        threading.Thread(target=_hold_long_poll, daemon=True).start()
        time.sleep(0.3)

        r = httpx.post(url, headers={"X-Hub-Shutdown-Token": token}, timeout=5.0)
        check("shutdown with right token -> 200 ok", r.status_code == 200 and r.json() == {"ok": True}, r.text)
        check("open long-poll unblocked on shutdown", poll_done.wait(timeout=5.0))
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            code = None
        check("hub exited by itself within 10 s", code is not None, f"exit={code}")
        check("exit code 0", code == 0, f"exit={code}")
        log_text = log_path.read_bytes().decode("utf-8", errors="replace")
        check("lifespan shutdown ran", "Application shutdown complete" in log_text)
        check("token never logged", token not in log_text)
        with closing(sqlite3.connect(log_path.parent / log_path.stem / "hub_state.db")) as conn:
            row = conn.execute(
                "SELECT status, end_reason, archived_at IS NOT NULL FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        check(
            "live session archived as interrupted on shutdown",
            row == ("interrupted", "interrupted", 1),
            repr(row),
        )
    finally:
        stop(proc)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hub-desktop-isolated-") as directory:
        log_dir = Path(os.environ.get("HUB_TEST_LOG_DIR") or directory)
        log_dir.mkdir(parents=True, exist_ok=True)
        scenario_no_token(log_dir)
        scenario_token(log_dir)
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
