"""Exercise the delivered EXE and extracted backend without stopping an installed desktop."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time

import httpx
from winpty import PtyProcess


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("PASS:", message, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--seed-state", required=True, type=Path)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="hub-exe-"))
    print("Artifacts:", root, flush=True)
    location = root / "中文 空格"
    location.mkdir()
    exe = location / "Agent Hub.exe"
    shutil.copy2(args.exe, exe)
    env = dict(os.environ)
    for key in ("AGENT_HUB_REPO", "AGENT_HUB_PYTHON", "AGENT_HUB_PORT", "PYTHONHOME", "PYTHONPATH", "HUB_DATA_DIR", "HUB_PROFILES_PATH"):
        env.pop(key, None)
    env.update(APPDATA=str(root / "Roaming"), LOCALAPPDATA=str(root / "Local"), PIP_INDEX_URL="https://127.0.0.1:1/simple", HTTP_PROXY="http://127.0.0.1:1", HTTPS_PROXY="http://127.0.0.1:1", NO_PROXY="127.0.0.1,localhost", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PATH=str(Path(os.environ["SystemRoot"]) / "System32"))
    runtime = root / "Local" / "mcp-agent-hub-portable" / "portable"
    runtime.mkdir(parents=True)
    old_config = root / "Roaming/mcp-agent-hub-desktop/config.json"
    old_config.parent.mkdir(parents=True)
    old_config.write_text(json.dumps({"repo_dir": "E:\\old-repository", "python": "E:\\missing.exe", "port": 4444}), encoding="utf-8")
    old_bytes = old_config.read_bytes()
    env.update(AGENT_HUB_REPO="E:\\old-environment", AGENT_HUB_PYTHON="E:\\missing.exe", AGENT_HUB_PORT="invalid")
    config = root / "Roaming/mcp-agent-hub-portable/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"python": str(root / "missing.exe")}), encoding="utf-8")
    failed = subprocess.run([str(exe), "--portable-prepare-only"], env=env, cwd=root, timeout=60)
    check(failed.returncode != 0 and not (runtime / "prepared.json").exists(), "invalid explicit interpreter fails without publishing readiness")
    check(not (runtime / "downloads").exists(), "explicit interpreter failure does not download Python")
    config.write_text(json.dumps({"repo_dir": "E:\\ignored-portable-override"}), encoding="utf-8")
    shutil.copy2(args.seed_state, runtime / "environment.json")
    busy = socket.socket()
    try:
        busy.bind(("127.0.0.1", 4444))
        busy.listen()
    except OSError:
        busy.close()  # An existing desktop already occupies the default port.
    try:
        ready = subprocess.run([str(exe), "--portable-prepare-only"], env=env, cwd=root, timeout=60)
        launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))
        check(4445 <= launch["port"] <= 4544, "occupied default port selects an independent Hub port")
        config.write_text(json.dumps({"port": 4444}), encoding="utf-8")
        conflict = subprocess.run([str(exe), "--portable-prepare-only"], env=env, cwd=root, timeout=60)
        check(conflict.returncode != 0, "explicit occupied port fails instead of attaching to another Hub")
        config.write_text("{}", encoding="utf-8")
    finally:
        busy.close()
    check(ready.returncode == 0, "single EXE unpacks and prepares with network unavailable")
    check(old_config.read_bytes() == old_bytes, "installed configuration and legacy environment are isolated")
    python = Path(json.loads((runtime / "prepared.json").read_text(encoding="utf-8"))["python"])
    moved = root / "moved.exe"
    shutil.move(exe, moved)
    check(subprocess.run([str(moved), "--portable-prepare-only"], env=env, cwd=root, timeout=60).returncode == 0, "moving and renaming the EXE preserves cached startup")
    source = next((runtime / "resources").iterdir())
    check(Path(launch["repo_dir"]).resolve() == source.resolve(), "repository overrides cannot redirect the portable payload")
    check(not any(p.suffix in (".exe", ".db") or p.name == "agent_profiles.json" for p in source.rglob("*")), "embedded payload has no Python, installers, database or personal profiles")
    check(not (runtime / "downloads").exists(), "cached EXE startup makes zero Python downloads")
    check(not (source / "hub_worker.py").exists() and not (source / "worker_bridge_mcp.py").exists(), "retired worker bridge is absent")
    check((source / "native_session_launch.py").is_file(), "native launch configuration helper is packaged")
    check((source / "agent_guidance.py").is_file(), "English agent guidance is packaged")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_hex(16)
    data = runtime / "data"
    api_env = dict(env, HUB_PORT=str(port), HUB_SHUTDOWN_TOKEN=token, HUB_DATA_DIR=str(data), HUB_PROFILES_PATH=str(data / "profiles.json"), HUB_API_URL=f"http://127.0.0.1:{port}")
    base = f"http://127.0.0.1:{port}"
    with (root / "hub.log").open("wb") as log, httpx.Client(trust_env=False, timeout=3) as client:
        process = subprocess.Popen([str(python), str(source / "server.py")], cwd=root, env=api_env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError((root / "hub.log").read_text(encoding="utf-8", errors="replace"))
                try:
                    info = client.get(base + "/api/hub/info").json()
                    if info.get("service") == "mcp-agent-hub":
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("extracted Hub did not become ready")
                time.sleep(0.2)
            check(Path(info["repo"]).resolve() == source.resolve(), "Hub serves directly from embedded resources, without a checkout")
            index = client.get(base + "/").text
            check("cdn.jsdelivr.net" not in index, "console does not depend on CDN resources")
            for asset in ("xterm.js", "xterm.css", "addon-fit.js"):
                check(client.get(base + "/static/vendor/" + asset).status_code == 200, f"offline terminal resource {asset} is served")
            check((data / "hub_state.db").is_file() and not (source / "hub_state.db").exists(), "database is stored separately from extracted code")
            check(client.get(base + "/api/sessions").json()["sessions"] == [], "isolated test Hub has no personal live sessions")

            pty = PtyProcess.spawn([str(python), "-I", "-c", "print('portable-pty-ok')"], cwd=str(root), env=api_env)
            try:
                output = ""
                deadline = time.monotonic() + 15
                while "portable-pty-ok" not in output and time.monotonic() < deadline:
                    try:
                        output += pty.read()
                    except EOFError:
                        break
                check("portable-pty-ok" in output, "prepared Python can spawn and read a ConPTY session")
            finally:
                pty.close(force=True)
            subprocess.run([str(python), str(source / "hub_poll.py"), "--help"], env=api_env, cwd=root, check=True, capture_output=True, timeout=15)
            hook = subprocess.run([str(python), str(source / "engine_policies/claude_pre_tool_hook.py")], input=json.dumps({"hook_event_name": "PermissionRequest", "session_id": "portable-smoke", "tool_name": "Bash", "tool_input": {"command": "echo test"}}), text=True, env=api_env, cwd=root, capture_output=True, timeout=15)
            check(hook.returncode == 0 and (data / ".runtime_mcp_configs/claude_hooks_portable-smoke.jsonl").is_file(), "extracted Claude hook writes to the same data directory")
        finally:
            if process.poll() is None:
                try:
                    client.post(base + "/api/hub/shutdown", headers={"x-hub-shutdown-token": token})
                    process.wait(timeout=20)
                except (httpx.HTTPError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=10)
    check(process.returncode == 0, "isolated Hub shuts down cleanly")


if __name__ == "__main__":
    main()
