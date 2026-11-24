"""Restart the Agent Hub without losing the sessions it was running.

Three subcommands, one JSON object on stdout, and the exit code says what happened:

  status       read-only: the portable root, the running hub, the restart phase and the last
               resume/update result. It never prints the shutdown token.
  restart      ask the hub to restart itself (`POST /api/hub/graceful-restart`) after writing
               `restart_plan.json`, so the desktop shell knows this stop is planned and starts
               the hub again; then wait until the sessions are back.
  replace-exe  do every check before stopping anything, then stage the new EXE next to the old
               one, ask for a `replace-exe` restart and start the detached helper that waits for
               the old hub, swaps the files (`ReplaceFileW`, rollback on failure) and relaunches
               the app from explorer.

Exit codes: 0 all good (a skipped session is not a failure), 2 restarted but at least one
session could not be resumed, 1 refused or failed (and, for a refusal, nothing was touched).

Standard library only (Python 3.8+); loopback requests bypass the system proxy. Windows-only
steps (`QueryFullProcessImageNameW`, `ShellExecuteW`) are isolated behind small functions so a
test can inject a stub.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PLAN_SEC = 600.0
RESTART_TIMEOUT_SEC = 300.0
REPLACE_TIMEOUT_SEC = 600.0
POLL_SEC = 1.0
# How long a failed helper launch waits for the old hub to leave before it brings back the old EXE.
RECOVERY_WAIT_SEC = 30.0
LIVE_STATUSES = ("starting", "running", "waiting_parent", "completed")
TERMINAL_PHASES = ("ready", "failed")
# The plan results that mean the swap did not happen: waiting for the hub to come back is pointless.
PLAN_FAILURE_PHASES = ("validate_failed", "wait_exit_timeout", "swap_failed", "rolled_back")
EXIT_OK, EXIT_PARTIAL, EXIT_ERROR = 0, 2, 1


class CliError(Exception):
    """A refusal or failure the user must see; the message is the whole story."""


# ---------------------------------------------------------------------------------------------
# Files and identity
# ---------------------------------------------------------------------------------------------
def launch_path(portable_root: Path) -> Path:
    return Path(portable_root) / "launch.json"


def plan_path(portable_root: Path) -> Path:
    return Path(portable_root) / "restart_plan.json"


def plan_result_path(portable_root: Path) -> Path:
    return Path(portable_root) / "restart_plan.result.json"


def load_launch(portable_root: Path) -> Dict[str, Any]:
    path = launch_path(portable_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise CliError(f"没有 {path}：{portable_root} 不是一个便携版的运行目录")
    except (OSError, ValueError) as exc:
        raise CliError(f"{path} 无法读取：{exc}")
    if not isinstance(raw, dict):
        raise CliError(f"{path} 不是 JSON 对象")
    try:
        port = int(raw.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise CliError(f"{path} 缺少有效 port")
    return raw


def redacted_launch(raw: Dict[str, Any]) -> Dict[str, Any]:
    """launch.json without its secret: whether a token exists, never the token itself."""
    redacted = {key: value for key, value in raw.items() if key != "shutdown_token"}
    redacted["shutdown_token_set"] = bool(raw.get("shutdown_token"))
    return redacted


def data_dir_of(launch: Dict[str, Any], portable_root: Path) -> Path:
    value = launch.get("data_dir")
    return Path(value) if value else Path(portable_root) / "data"


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def write_json_atomic(path: Path, body: Dict[str, Any]) -> None:
    path = Path(path)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    temp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def pending_plan(portable_root: Path, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """A plan that is still valid; an expired or unreadable one is a leftover, not a plan."""
    raw = read_json_file(plan_path(portable_root))
    if raw is None:
        return None
    try:
        expires = float(raw.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        expires = 0.0
    if not expires or expires <= (time.time() if now is None else now):
        return None
    return raw


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def first_hex_token(text: str) -> str:
    for token in text.replace("\t", " ").split():
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
    return ""


def sidecar_sha256(new_exe: Path) -> Optional[str]:
    for suffix in (".sha256", ".sha256.txt"):
        path = Path(str(new_exe) + suffix)
        if path.is_file():
            try:
                return first_hex_token(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return None
    return None


def same_path(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def query_process_image(pid: int) -> Optional[str]:
    """The real executable path of a live process (`QueryFullProcessImageNameW`)."""
    if os.name != "nt" or not pid:
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value or None
    finally:
        kernel32.CloseHandle(handle)


def shell_execute_helper(helper_exe: Path, plan: Path) -> None:
    """Start the helper outside this process' job object, so it survives its parent."""
    if os.name != "nt":
        raise CliError("replace-exe 只能在 Windows 上运行")
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    result = shell32.ShellExecuteW(None, "open", str(helper_exe),
                                   f'--apply-update "{plan}"', None, 0)
    if not result or int(result) <= 32:
        raise CliError(f"无法启动升级助手 {helper_exe}（ShellExecuteW 返回 {result}）")


def probe_build_info(exe: Path, timeout: float = 30.0) -> Dict[str, Any]:
    """`<exe> --print-build-info`: proves the new EXE is a portable build of this payload."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run([str(exe), "--print-build-info"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError(f"无法运行 {exe} --print-build-info：{exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise CliError(f"{exe} --print-build-info 退出码 {proc.returncode}：{detail}")
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            info = json.loads(line)
        except ValueError:
            continue
        if isinstance(info, dict):
            return info
    raise CliError(f"{exe} --print-build-info 没有输出 JSON")


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
class HubUnreachable(Exception):
    """The hub did not answer at all (down, restarting, or not listening yet).

    `reason` keeps the transport exception: a refused connection proves the request never landed,
    while a read timeout or a dropped response does not, and the two must be handled differently.
    """

    def __init__(self, message: str, *, reason: Optional[BaseException] = None):
        super().__init__(message)
        self.reason = reason


def _exception_chain(exc: Optional[BaseException]) -> List[BaseException]:
    """The exception and everything it wraps (`URLError.reason`, `__cause__`, `__context__`)."""
    seen: List[BaseException] = []
    while exc is not None and len(seen) < 8 and exc not in seen:
        seen.append(exc)
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
            exc = exc.reason
        else:
            exc = exc.__cause__ or exc.__context__
    return seen


def request_may_have_arrived(exc: BaseException) -> bool:
    """True when the POST may have reached the hub (a timeout, or a connection cut mid-answer).

    A refused connection, a DNS failure or a socket that never opened means the request was never
    sent: the hub did not accept a stop for this restart, so its plan may go.
    """
    kinds = (TimeoutError, socket.timeout, http.client.IncompleteRead,
             http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError,
             BrokenPipeError)
    return any(isinstance(inner, kinds) for inner in _exception_chain(exc))


def hub_looks_idle(opener, base: str) -> bool:
    """True only when the hub answers and is still `idle`: proof it never took the restart.

    No answer (the hub is gone, or its port is closing) and any other phase (`stopping`) both mean
    the request may well have been accepted, so the plan must survive.
    """
    status = get_json(opener, base + "/api/hub/restart-status")
    if not status or status.get("http_status"):
        return False
    return str(status.get("phase") or "") == "idle"


def post_verdict(opener, base: str, exc: BaseException) -> str:
    """`"dropped"` when the request never reached the hub, else `"maybe_accepted"`."""
    if not request_may_have_arrived(exc):
        return "dropped"
    if hub_looks_idle(opener, base):
        return "dropped"
    return "maybe_accepted"


def make_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _decode_body(raw: bytes) -> Dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
    except ValueError:
        return {"_body": text[:2000]}
    return body if isinstance(body, dict) else {"_body": body}


def request_json(opener, url: str, *, method: str = "GET", body: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None,
                 timeout: float = 5.0) -> Tuple[int, Dict[str, Any]]:
    """(status code, JSON object); an HTTP error is an answer, an unreachable hub raises."""
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    request = urllib.request.Request(url, data=payload, headers=merged, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200) or 200), _decode_body(response.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), _decode_body(exc.read())
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise HubUnreachable(str(exc), reason=exc)


def hub_base(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def token_headers(launch: Dict[str, Any]) -> Dict[str, str]:
    token = str(launch.get("shutdown_token") or "").strip()
    if not token:
        raise CliError("launch.json 没有 shutdown_token：这个 Hub 未启用远程停止/重启，"
                       "请从托盘退出后重新启动桌面版")
    return {"x-hub-shutdown-token": token}


def get_json(opener, url: str) -> Optional[Dict[str, Any]]:
    try:
        status, payload = request_json(opener, url, timeout=3.0)
    except HubUnreachable:
        return None
    return payload if status == 200 else {"http_status": status, "detail": payload}


def plan_result_for(portable_root: Path, restart_id: str) -> Optional[Dict[str, Any]]:
    """The `restart_plan.result.json` that answers *this* run, if the helper already wrote one.

    It is `None` while the helper has nothing to say, and never another restart's result: a
    leftover from an earlier run must not end this one.
    """
    raw = read_json_file(plan_result_path(portable_root))
    if not isinstance(raw, dict):
        return None
    rid = str(raw.get("restart_id") or "")
    if restart_id and rid and rid != restart_id:
        return None
    return raw


def plan_result_failure(portable_root: Path, restart_id: str) -> Optional[Dict[str, Any]]:
    """The result, when it already says the swap failed: no hub is coming back from it.

    The helper writes `ok: false` for `validate_failed`, `wait_exit_timeout`, `swap_failed` and a
    rollback; the CLI must stop waiting there instead of spending the whole timeout on a restart
    that will never happen.
    """
    raw = plan_result_for(portable_root, restart_id)
    if not isinstance(raw, dict) or raw.get("ok") is not False:
        return None
    return raw


def wait_for_restart(opener, base: str, before: Dict[str, Any], timeout: float, *,
                     portable_root: Optional[Path] = None, restart_id: str = "",
                     poll: float = POLL_SEC, clock=time.monotonic, sleep=time.sleep,
                     ) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]:
    """(restart-status, hub info, plan result that failed) for a *new* hub that finished resuming.

    `before` is the old hub's /api/hub/info: the new one must differ in `started_at` (or the old
    one had none to compare), so a stop that never happened is not mistaken for a restart.

    The plan result is read first on every poll: a helper that already reported a failure (a
    rejected build, the old hub that would not exit, a failed or rolled-back swap) stops the loop
    at once, and the caller reports that instead of a generic timeout.
    """
    deadline = clock() + max(1.0, timeout)
    last: Dict[str, Any] = {}
    root = Path(portable_root) if portable_root else None
    while clock() < deadline:
        if root is not None:
            failure = plan_result_failure(root, restart_id)
            if failure is not None:
                return last, {}, failure
        status = get_json(opener, base + "/api/hub/restart-status")
        if status and str(status.get("phase") or "") in TERMINAL_PHASES:
            info = get_json(opener, base + "/api/hub/info")
            old_started = str((before or {}).get("started_at") or "")
            old_pid = (before or {}).get("pid")
            # `started_at` is second-resolution, so a quick restart can keep it: the pid is the
            # second witness that this is the new hub, not the old one answering a stale poll.
            newer = bool(info) and (
                (old_started and str(info.get("started_at") or "") != old_started)
                or (old_pid and info.get("pid") and info.get("pid") != old_pid)
                or (not old_started and not old_pid))
            if newer:
                return status, info, None
        last = status or last
        sleep(poll)
    raise CliError("等待 Hub 重启完成超时（最后一次看到的阶段："
                   f"{last.get('phase') if last else 'Hub 无应答'}）")


def wait_for_terminal(opener, base: str, before: Dict[str, Any], timeout: float,
                      *, poll: float = POLL_SEC, clock=time.monotonic,
                      sleep=time.sleep) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """`wait_for_restart` without a plan to watch: (restart-status, hub info)."""
    status, info, _failure = wait_for_restart(opener, base, before, timeout, poll=poll,
                                              clock=clock, sleep=sleep)
    return status, info


def exit_code_for(status: Optional[Dict[str, Any]]) -> int:
    if not status:
        return EXIT_ERROR
    if str(status.get("phase") or "") != "ready":
        return EXIT_PARTIAL if status.get("phase") == "failed" else EXIT_ERROR
    return 0


def continue_fields(args) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"continue_unfinished_turns": not bool(getattr(args, "no_continue", False)),
                              "resume_crashed": bool(getattr(args, "resume_crashed", False))}
    session_ids = getattr(args, "session_id", None)
    if session_ids:
        fields["session_ids"] = list(session_ids)
    return fields


# ---------------------------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------------------------
def session_summary(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("sessions"), list):
        rows = [row for row in payload["sessions"] if isinstance(row, dict)]
    live = [row for row in rows if str(row.get("status") or "") in LIVE_STATUSES]
    return {
        "count": len(rows),
        "live_count": len(live),
        "live": [{"session_id": row.get("session_id"), "engine": row.get("engine"),
                  "status": row.get("status"), "title": row.get("title")} for row in live],
    }


def cmd_status(args, *, opener=None) -> Tuple[int, Dict[str, Any]]:
    opener = opener or make_opener()
    root = Path(args.portable_root)
    out: Dict[str, Any] = {"ok": True, "command": "status", "portable_root": str(root)}
    try:
        launch = load_launch(root)
    except CliError as exc:
        launch = None
        out["ok"] = False
        out["launch_error"] = str(exc)
    out["launch"] = redacted_launch(launch) if launch else None
    port = int(args.port or (launch or {}).get("port") or 4444)
    out["port"] = port
    base = hub_base(port)
    for key, path in (("hub", "/api/hub/info"), ("health", "/health"),
                      ("restart_status", "/api/hub/restart-status"), ("sessions", "/api/sessions")):
        value = get_json(opener, base + path)
        out[key] = value if value is not None else {"error": "Hub 没有应答"}
    out["session_summary"] = session_summary(out.get("sessions"))
    out["restart_plan"] = pending_plan(root)
    data_dir = data_dir_of(launch, root) if launch else root / "data"
    out["resume_on_start"] = {
        "pending_request": (data_dir / "resume_on_start.json").is_file(),
        "result": read_json_file(data_dir / "resume_on_start.result.json"),
    }
    out["restart_plan_result"] = read_json_file(plan_result_path(root))
    return (0 if launch else EXIT_ERROR), out


# ---------------------------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------------------------
def hub_plan(root: Path, launch: Dict[str, Any], restart_id: str, mode: str,
             *, wall=time.time) -> Dict[str, Any]:
    app_pid = int(launch.get("app_pid") or 0)
    requested_at = wall()
    return {
        "version": 1,
        "restart_id": restart_id,
        "mode": mode,
        "requested_at": requested_at,
        "expires_at": requested_at + PLAN_SEC,
        "port": int(launch["port"]),
        "app_pid": app_pid,
        "app_creation_time": int(launch.get("app_creation_time") or 0),
        "hub_pid": int(launch.get("pid") or 0),
        "hub_creation_time": int(launch.get("creation_time") or 0),
        "data_dir": str(data_dir_of(launch, root)),
        # Null for a hub restart: the shell reads these as optional, and only `replace-exe` fills
        # them. A `null` is never mistaken for a real path.
        "target_exe": None,
        "old_exe_sha256": None,
        "new_exe": None,
        "new_exe_sha256": None,
        "staged_exe": None,
        "backup_exe": None,
    }


def cmd_restart(args, *, opener=None, clock=time.monotonic, sleep=time.sleep,
                wall=time.time, query_exe: Callable[[int], Optional[str]] = query_process_image,
                ) -> Tuple[int, Dict[str, Any]]:
    opener = opener or make_opener()
    root = Path(args.portable_root)
    out: Dict[str, Any] = {"ok": False, "command": "restart", "portable_root": str(root)}
    launch = load_launch(root)
    stale = pending_plan(root)
    if stale is not None:
        raise CliError(f"已有未过期的 restart_plan.json（restart_id={stale.get('restart_id')}），"
                       "先等它过期或确认上一次重启的进程已结束")
    port = int(launch["port"])
    base = hub_base(port)
    headers = token_headers(launch)
    before = get_json(opener, base + "/api/hub/info")
    if before is None or before.get("http_status"):
        raise CliError("Hub 没有应答 /api/hub/info：先确认它正在运行")

    restart_id = uuid.uuid4().hex
    plan = hub_plan(root, launch, restart_id, "hub", wall=wall)
    app_pid = int(launch.get("app_pid") or 0)
    try:
        image = query_exe(app_pid) if app_pid else None
    except Exception:
        image = None
    if image:
        plan["target_exe"] = image
    out["plan"] = plan
    write_json_atomic(plan_path(root), plan)

    body = {"mode": "hub", "restart_id": restart_id, **continue_fields(args)}
    unconfirmed = ""
    try:
        status, answer = request_json(opener, base + "/api/hub/graceful-restart", method="POST",
                                      body=body, headers=headers, timeout=10.0)
    except HubUnreachable as exc:
        verdict = post_verdict(opener, base, exc)
        if verdict == "dropped":
            clear_plan(root, restart_id)
            raise CliError(f"重启请求发不出去：{exc}")
        # A read timeout or a cut response: the hub may already be stopping for this restart, so
        # dropping the plan would leave the shell with an unexplained stop. Wait for it instead.
        unconfirmed = str(exc)
        status, answer = 0, {"unconfirmed": True, "reason": unconfirmed}
    if status not in (200, 202, 0):
        clear_plan(root, restart_id)
        raise CliError(f"Hub 拒绝了重启请求（HTTP {status}）：{answer.get('detail') or answer}")
    out["accepted"] = {"http_status": status, **answer}
    out["restart_id"] = restart_id
    if unconfirmed:
        out["accepted"]["unconfirmed"] = True
        out["note"] = ("重启请求没有得到确认响应（可能已被受理，Hub 正在关停）；"
                       f"保留 restart_plan.json 并继续等待：{unconfirmed}")

    if args.no_wait:
        out["ok"] = True
        out["phase"] = "stopping"
        note = "已请求重启；桌面壳会自动拉起新 Hub。没有等待恢复结果（--no-wait）"
        out["note"] = f"{out['note']} {note}" if unconfirmed else note
        return EXIT_OK, out

    started = clock()
    final_status, final_info = wait_for_terminal(
        opener, base, before, float(args.timeout_seconds), poll=args.poll_seconds,
        clock=clock, sleep=sleep)
    out["hub"] = final_info
    out["status"] = final_status
    out["phase"] = final_status.get("phase")
    out["sessions"] = final_status.get("result") or {}
    code = exit_code_for(final_status)
    out["ok"] = code == EXIT_OK
    out["waited_seconds"] = round(clock() - started, 1)
    # The shell has started the new hub by now; this plan has done its job.
    clear_plan(root, restart_id)
    return code, out


def clear_plan(root: Path, restart_id: str) -> None:
    """Remove the plan this run wrote, never someone else's."""
    try:
        raw = read_json_file(plan_path(root))
    except Exception:
        return
    if raw is None or str(raw.get("restart_id") or "") != restart_id:
        return
    try:
        plan_path(root).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# replace-exe
# ---------------------------------------------------------------------------------------------
def resolve_target_exe(args, launch: Dict[str, Any],
                       query_exe: Callable[[int], Optional[str]]) -> Path:
    if args.target_exe:
        return Path(args.target_exe).expanduser().resolve()
    app_pid = int(launch.get("app_pid") or 0)
    image = query_exe(app_pid) if app_pid else None
    if not image:
        raise CliError("无法确定桌面 App 的 EXE 路径（launch.json 的 app_pid 已退出？）；"
                       "用 --target-exe 指定要替换的 EXE")
    return Path(image).resolve()


def is_dist_build(path: Path) -> bool:
    parts = [part.lower() for part in Path(path).parts]
    return "desktop" in parts and "dist" in parts and parts.index("dist") > parts.index("desktop")


def precheck_replace(args, *, probe: Optional[Callable[[Path], Dict[str, Any]]],
                     query_exe: Callable[[int], Optional[str]],
                     sha: Callable[[Path], str]) -> Dict[str, Any]:
    """Everything that can refuse the update; it never writes, copies or stops anything."""
    new_exe = Path(args.new_exe).expanduser().resolve()
    if not new_exe.is_file():
        raise CliError(f"找不到新 EXE：{new_exe}")
    new_sha = sha(new_exe).lower()
    expected = str(args.sha256 or "").strip().lower() or sidecar_sha256(new_exe)
    if expected and expected != new_sha:
        raise CliError(f"新 EXE 的 SHA-256 与期望不符：实际 {new_sha}，期望 {expected}")
    build: Dict[str, Any] = {}
    if not args.skip_build_probe:
        build = (probe or probe_build_info)(new_exe)
        if build.get("portable") is not True:
            raise CliError("新 EXE 不是便携构建（--print-build-info 没有 portable:true）")
        reported = str(build.get("exe_sha256") or "").strip().lower()
        if reported != new_sha:
            raise CliError(f"新 EXE 自报的 exe_sha256 与文件不符：{reported or '（空）'} != {new_sha}")
    root = Path(args.portable_root)
    launch = load_launch(root)
    target = resolve_target_exe(args, launch, query_exe)
    if not target.is_file():
        raise CliError(f"要替换的 EXE 不存在：{target}")
    if same_path(target, new_exe):
        raise CliError("--target-exe 与新 EXE 是同一个文件：这不是升级")
    if is_dist_build(target):
        raise CliError(f"拒绝升级运行在 {target.parent} 的构建产物；请先部署到正式位置")
    stale = pending_plan(root)
    if stale is not None:
        raise CliError(f"已有未过期的 restart_plan.json（restart_id={stale.get('restart_id')}）")
    return {"new_exe": new_exe, "new_sha256": new_sha, "target_exe": target,
            "launch": launch, "build": build}


def relaunch_via_explorer(exe: Path) -> bool:
    """Start `exe` through the shell, outside this process, as a last resort.

    Windows-only; the call is isolated here so a test can inject its own hook. `explorer.exe`
    starts the app the way a user double-clicking it would, and the new process is not in this
    CLI's job object, so it survives the CLI's exit.
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    result = shell32.ShellExecuteW(None, "open", str(exe), None, None, 1)
    return bool(result) and int(result) > 32


def wait_for_old_hub_gone(opener, base: str, before: Dict[str, Any], timeout: float, *,
                          poll: float = POLL_SEC, clock=time.monotonic,
                          sleep=time.sleep) -> bool:
    """True once the hub that was running before is no longer answering on its port."""
    old_pid = str((before or {}).get("pid") or "")
    deadline = clock() + max(0.5, float(timeout))
    while True:
        info = get_json(opener, base + "/api/hub/info")
        if not info or info.get("http_status"):
            return True
        pid = str(info.get("pid") or "")
        if not old_pid or not pid or pid != old_pid:
            # Another hub answers where the old one was: it is already gone.
            return True
        if clock() >= deadline:
            return False
        sleep(poll)


def hub_pid_now(opener, base: str) -> Optional[int]:
    """The pid of the hub that answers on this port right now, or None when none answers."""
    info = get_json(opener, base + "/api/hub/info")
    if not info or info.get("http_status"):
        return None
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid or None


def recover_after_helper_failure(opener, base: str, before: Dict[str, Any], target: Path, *,
                                 helper_error: str, launch_exe: Callable[[Path], bool],
                                 timeout: float, poll: float = POLL_SEC,
                                 clock=time.monotonic, sleep=time.sleep) -> Dict[str, Any]:
    """What the CLI reports and does after the helper failed to start, with the hub already told to stop.

    The new EXE was never swapped in, so the honest outcome is "no upgrade": the old EXE is
    started again once the old hub has left, unless the shell has already brought a hub back.
    """
    out: Dict[str, Any] = {"ok": False, "phase": "helper_launch_failed",
                           "helper_launch_failed": True, "helper_error": helper_error,
                           "exe_replaced": False}
    if not wait_for_old_hub_gone(opener, base, before, timeout, poll=poll, clock=clock, sleep=sleep):
        out.update({"recovered": False,
                    "recovery": "旧 Hub 仍未退出，未尝试兜底拉起；请稍后手动启动，或检查 restart_plan.json"})
        return out
    old_pid = (before or {}).get("pid")
    pid_now = hub_pid_now(opener, base)
    if pid_now and (not old_pid or str(pid_now) != str(old_pid)):
        out.update({"recovered": True, "recovery": "桌面壳已拉起新的 Hub（pid 已变化），无需兜底"})
        return out
    try:
        started = bool(launch_exe(target))
    except Exception as exc:  # a fallback that fails must still answer, not traceback
        out.update({"recovered": False, "recovery": f"兜底拉起原 EXE 失败：{exc}"})
        return out
    out.update({"recovered": started,
                "recovery": ("已用 explorer 兜底拉起原 EXE（旧版本重新运行）" if started
                             else "兜底拉起原 EXE 未成功：请手动启动")})
    return out


def cmd_replace_exe(args, *, opener=None, probe=None,
                    query_exe: Callable[[int], Optional[str]] = query_process_image,
                    sha: Callable[[Path], str] = sha256_file,
                    spawn_helper: Callable[[Path, Path], None] = shell_execute_helper,
                    launch_exe: Callable[[Path], bool] = relaunch_via_explorer,
                    clock=time.monotonic, sleep=time.sleep, wall=time.time,
                    ) -> Tuple[int, Dict[str, Any]]:
    opener = opener or make_opener()
    root = Path(args.portable_root)
    out: Dict[str, Any] = {"ok": False, "command": "replace-exe", "portable_root": str(root)}
    checked = precheck_replace(args, probe=probe, query_exe=query_exe, sha=sha)
    new_exe, new_sha = checked["new_exe"], checked["new_sha256"]
    target, launch = checked["target_exe"], checked["launch"]
    port = int(launch["port"])
    base = hub_base(port)
    restart_id = uuid.uuid4().hex
    nonce = restart_id[:12]
    staged = target.parent / f".hub-update-{nonce}.exe"
    helper = Path(tempfile.gettempdir()) / f"hub-update-helper-{nonce}.exe"
    backup = Path(str(target) + ".bak")

    out["new_exe"] = str(new_exe)
    out["new_exe_sha256"] = new_sha
    out["target_exe"] = str(target)
    out["build"] = checked["build"]
    out["restart_id"] = restart_id

    if args.dry_run:
        plan = hub_plan(root, launch, restart_id, "replace-exe", wall=wall)
        plan.update({"target_exe": str(target), "old_exe_sha256": sha(target).lower(),
                     "new_exe": str(new_exe), "new_exe_sha256": new_sha,
                     "staged_exe": str(staged), "backup_exe": str(backup)})
        out.update({"ok": True, "phase": "dry-run", "plan": plan,
                    "note": "所有预检通过；--dry-run 没有写文件、没有停止任何进程"})
        return EXIT_OK, out

    headers = token_headers(launch)
    before = get_json(opener, base + "/api/hub/info")
    if before is None or before.get("http_status"):
        raise CliError("Hub 没有应答 /api/hub/info：先确认它正在运行")

    old_sha = sha(target).lower()
    plan = hub_plan(root, launch, restart_id, "replace-exe", wall=wall)
    plan.update({"target_exe": str(target), "old_exe_sha256": old_sha,
                 "new_exe": str(new_exe), "new_exe_sha256": new_sha,
                 "staged_exe": str(staged), "backup_exe": str(backup)})
    out["plan"] = plan

    def drop_files() -> None:
        """Remove the staged EXE and the helper copy; the plan is the caller's decision."""
        for path in (staged, helper):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def drop_staged() -> None:
        """Nothing has been asked of the hub, so the plan goes with the staged files."""
        drop_files()
        clear_plan(root, restart_id)

    # Stage on the target's own volume first, so the helper's ReplaceFileW is a same-volume move.
    try:
        shutil.copy2(new_exe, staged)
    except OSError as exc:
        drop_staged()
        raise CliError(f"无法暂存新 EXE 到 {staged}：{exc}")
    if sha(staged).lower() != new_sha:
        drop_staged()
        raise CliError("暂存文件复制后 SHA-256 不一致，已清理")
    try:
        shutil.copy2(new_exe, helper)
    except OSError as exc:
        drop_staged()
        raise CliError(f"无法把升级助手复制到 {helper}：{exc}")
    try:
        write_json_atomic(plan_path(root), plan)
    except OSError as exc:
        drop_staged()
        raise CliError(f"无法写入 {plan_path(root)}：{exc}")

    body = {"mode": "replace-exe", "restart_id": restart_id, **continue_fields(args)}
    try:
        status, answer = request_json(opener, base + "/api/hub/graceful-restart", method="POST",
                                      body=body, headers=headers, timeout=10.0)
    except HubUnreachable as exc:
        verdict = post_verdict(opener, base, exc)
        if verdict == "dropped":
            drop_staged()
            raise CliError(f"重启请求发不出去：{exc}")
        # The hub may already be stopping for this restart: keep the plan the helper needs.
        out["accepted"] = {"http_status": None, "unconfirmed": True, "reason": str(exc)}
        out["note"] = ("重启请求没有得到确认响应（可能已被受理，Hub 正在关停）；"
                       f"保留 restart_plan.json 并继续：{exc}")
    else:
        if status not in (200, 202):
            drop_staged()
            raise CliError(f"Hub 拒绝了重启请求（HTTP {status}）：{answer.get('detail') or answer}")
        out["accepted"] = {"http_status": status, **answer}

    try:
        spawn_helper(helper, plan_path(root))
    except Exception as exc:
        # The hub was already told to stop. Dropping the plan and walking away would leave the
        # user with no hub at all, so the staged files go, the plan stays (the shell sees a
        # planned stop), and the old EXE is started again once the old hub has left.
        drop_files()
        out.update(recover_after_helper_failure(
            opener, base, before, target, helper_error=str(exc), launch_exe=launch_exe,
            timeout=min(RECOVERY_WAIT_SEC, max(1.0, float(args.timeout_seconds))),
            poll=args.poll_seconds, clock=clock, sleep=sleep))
        return EXIT_ERROR, out

    started = clock()
    final_status, final_info, plan_failure = wait_for_restart(
        opener, base, before, float(args.timeout_seconds), portable_root=root,
        restart_id=restart_id, poll=args.poll_seconds, clock=clock, sleep=sleep)
    out["hub"] = final_info
    out["status"] = final_status
    out["phase"] = final_status.get("phase")
    out["sessions"] = final_status.get("result") or {}
    out["waited_seconds"] = round(clock() - started, 1)
    try:
        out["exe_sha256"] = sha(target).lower()
        out["exe_replaced"] = out["exe_sha256"] == new_sha
    except OSError as exc:
        out["exe_sha256"] = None
        out["exe_replaced"] = False
        out["exe_sha256_error"] = str(exc)
    plan_result = plan_result_for(root, restart_id) or plan_failure
    out["plan_result"] = plan_result
    if plan_failure is not None:
        # The helper answered before the timeout: the swap never happened, and waiting on for
        # the hub to come back would hide that behind "timed out".
        out["phase"] = "plan_failed"
        out["ok"] = False
        out["plan_failed_phase"] = str(plan_failure.get("phase") or "") or None
        out["error"] = (f"升级助手报告失败（{plan_failure.get('phase') or 'unknown'}）："
                        f"{plan_failure.get('detail') or plan_failure.get('error') or plan_failure}")
        if str(plan_failure.get("phase") or "") in ("rolled_back",) or plan_failure.get("rolled_back"):
            out["rollback"] = "升级失败，已自动回滚至旧版本"
        elif out["exe_replaced"]:
            out["rollback"] = None
        return EXIT_ERROR, out
    if not out["exe_replaced"]:
        # Only claim a usable `.bak` when nothing was rolled back: after a rollback the old file is
        # already in place, and pointing at a backup that does not exist sends the user nowhere.
        rolled_back = bool((plan_result or {}).get("rolled_back"))
        if rolled_back:
            out["rollback"] = "升级失败，已自动回滚至旧版本"
        elif backup.exists():
            out["rollback"] = f"{backup} 仍在；确认后可用它换回旧 EXE"
        else:
            out["rollback"] = "未找到 .bak 备份；新 EXE 未生效，旧版本可能已被替换，请重新部署"
        return EXIT_ERROR, out
    code = exit_code_for(final_status)
    out["ok"] = code == EXIT_OK
    clear_plan(root, restart_id)
    return code, out


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    default_root = str(Path(os.environ.get("LOCALAPPDATA") or Path.home())
                       / "mcp-agent-hub-portable" / "portable")
    root_help = ("含 launch.json 的便携版运行目录"
                 "（默认 %%LOCALAPPDATA%%\\mcp-agent-hub-portable\\portable）")
    # The root is accepted before the verb and after it; a subparser default would otherwise
    # overwrite a value parsed on the top-level parser, so the subparser's is SUPPRESS.
    sub_root = argparse.ArgumentParser(add_help=False)
    sub_root.add_argument("--portable-root", default=argparse.SUPPRESS, help=root_help)
    parser = argparse.ArgumentParser(
        description="优雅重启 Agent Hub（便携版）并恢复它的会话。")
    parser.add_argument("--portable-root", default=default_root, help=root_help)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", parents=[sub_root],
                            help="只读：Hub、会话和最近一次重启的状态")
    status.add_argument("--port", type=int, default=0, help="覆盖 launch.json 的端口")

    for name, help_text in (("restart", "请求 Hub 优雅重启并等待会话恢复"),
                            ("replace-exe", "换掉便携版 EXE 并重启")):
        cmd = sub.add_parser(name, parents=[sub_root], help=help_text)
        if name == "replace-exe":
            cmd.add_argument("new_exe", help="新构建的便携版 EXE")
            cmd.add_argument("--target-exe", default="", help="要替换的 EXE（默认查 launch.json 的 app_pid）")
            cmd.add_argument("--sha256", default="", help="期望的新 EXE SHA-256（默认读 <new_exe>.sha256）")
            cmd.add_argument("--skip-build-probe", action="store_true",
                             help="跳过新 EXE 的 --print-build-info 校验（仅测试用）")
            cmd.add_argument("--dry-run", action="store_true", help="只做预检并打印计划")
            cmd.add_argument("--timeout-seconds", type=float, default=REPLACE_TIMEOUT_SEC)
        else:
            cmd.add_argument("--timeout-seconds", type=float, default=RESTART_TIMEOUT_SEC)
            cmd.add_argument("--no-wait", action="store_true", help="只发出请求，不等待恢复结果")
        cmd.add_argument("--no-continue", action="store_true",
                         help="不向被打断回合的会话送继续消息")
        cmd.add_argument("--resume-crashed", action="store_true",
                         help="连历史 crashed 会话也恢复")
        cmd.add_argument("--session-id", action="append", default=[],
                         help="只恢复这些会话（可重复）")
        cmd.add_argument("--poll-seconds", type=float, default=POLL_SEC)
    return parser


def main(argv=None, *, opener=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            code, payload = cmd_status(args, opener=opener)
        elif args.command == "restart":
            code, payload = cmd_restart(args, opener=opener)
        else:
            code, payload = cmd_replace_exe(args, opener=opener)
    except CliError as exc:
        code, payload = EXIT_ERROR, {"ok": False, "command": args.command, "error": str(exc)}
    except Exception as exc:  # a bug must still answer in JSON, with a non-zero code
        code, payload = EXIT_ERROR, {"ok": False, "command": args.command,
                                     "error": f"{type(exc).__name__}: {exc}"}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
