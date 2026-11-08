"""Desktop-shell hub API: /api/hub/info and the token-guarded /api/hub/shutdown.

Starts two throwaway hubs from this checkout on test ports (never 4444):
  - without HUB_SHUTDOWN_TOKEN: info answers, shutdown is 404;
  - with a token: a missing or wrong token is 403, the right one answers {"ok": true} and the
    hub stops gracefully (uvicorn's Ctrl+C path, lifespan shutdown included) with exit code 0.

Run: <venv python> test_hub_desktop_api.py
"""

import io
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")

HUB_DIR = Path(__file__).parent.resolve()
# K3: the interpreter this suite runs on, not a guessed <repo>/.venv (a worktree has none).
PYTHON_EXE = sys.executable
PORT_NO_TOKEN = int(os.environ.get("HUB_TEST_PORT_A", "4468"))
PORT_TOKEN = int(os.environ.get("HUB_TEST_PORT_B", "4469"))

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def start_hub(port: int, token: str, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["HUB_PORT"] = str(port)
    env.pop("HUB_SHUTDOWN_TOKEN", None)
    if token:
        env["HUB_SHUTDOWN_TOKEN"] = token
    log = open(log_path, "wb")
    return subprocess.Popen(
        [PYTHON_EXE, str(HUB_DIR / "server.py")],
        cwd=str(HUB_DIR),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
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
        r = httpx.post(url, headers={"X-Hub-Shutdown-Token": token}, timeout=5.0)
        check("shutdown with right token -> 200 ok", r.status_code == 200 and r.json() == {"ok": True}, r.text)
        try:
            code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            code = None
        check("hub exited by itself within 20 s", code is not None, f"exit={code}")
        check("exit code 0", code == 0, f"exit={code}")
        log_text = log_path.read_bytes().decode("utf-8", errors="replace")
        check("lifespan shutdown ran", "Application shutdown complete" in log_text)
        check("token never logged", token not in log_text)
    finally:
        stop(proc)


def main() -> int:
    log_dir = Path(os.environ.get("HUB_TEST_LOG_DIR") or (Path(tempfile.gettempdir()) / "hub-desktop-api-test"))
    log_dir.mkdir(parents=True, exist_ok=True)
    scenario_no_token(log_dir)
    scenario_token(log_dir)
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
