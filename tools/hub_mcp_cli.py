"""Call a Hub session's MCP pool from one stdlib-only script (no third-party dependency).

Usage, with the bridge file the Hub writes for this session (``--bridge`` also comes from HUB_MCP_BRIDGE):

  python hub_mcp_cli.py --bridge <bridge.json> list
  python hub_mcp_cli.py --bridge <bridge.json> ping <server> [--timeout-ms N]
  python hub_mcp_cli.py --bridge <bridge.json> tools <server> [--timeout-ms N]
  python hub_mcp_cli.py --bridge <bridge.json> call <server> <tool> [--arg key=value ...]
                        [--arg-json key=<json> ...] [--file args.json] [--timeout-ms N]
                        [--wait SECONDS] [--out out.json]
  python hub_mcp_cli.py --bridge <bridge.json> batch --file <batch.json> [--timeout-ms N]
                        [--wait SECONDS] [--out out.json]
  python hub_mcp_cli.py --bridge <bridge.json> job <job_id> [--wait SECONDS] [--cancel] [--out out.json]

``--arg`` always stays a string ("00123" is "00123", "true" is "true"); ``--arg-json`` parses one JSON
value, so numbers, booleans, arrays, objects and null go there. ``--file`` reads one UTF-8 (BOM allowed)
JSON object whose keys ``--arg``/``--arg-json`` may override. ``--wait`` is the seconds the Hub may hold
the request before answering status="running"; the HTTP socket timeout stays above it. ``job --wait``
polls in chunks of at most 60 s, so a long wait never exceeds the Hub's own long-poll bound.

Exit codes, so a shell can branch without parsing JSON:
  0  the bridge call itself succeeded: list, ping, tools, a finished call, batch, or job. A tool result
     whose own ``isError`` is true also exits 0: the Hub did hand back the tool's answer, and the model
     reads isError in the JSON. Some shells drop stdout from a failing command, so a tool-level error
     must not masquerade as a bridge failure.
  2  usage or connection-config error, or the call is still running as an async job (status="running"),
     which is fetched with ``job <job_id>``.
  1  the bridge call failed: the Hub answered ok=false, rejected the token, was unreachable, or sent
     something that is not a JSON object.

Every run prints exactly one JSON object on stdout (``ensure_ascii=False``); failures print
``{"ok": false, "error": {"kind", "message", "hint"}}`` instead of a traceback.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
DEFAULT_HUB = "http://127.0.0.1:4444"
DEFAULT_WAIT_SECONDS = 30.0
DEFAULT_JOB_WAIT_SECONDS = 25.0
# The Hub's own long-poll for a job is bounded; one request must never ask it to hold longer than
# that, or the client would sit in a dead socket waiting for an answer the Hub already gave up on.
MAX_JOB_POLL_WAIT_MS = 60_000
JOB_POLL_PAUSE_SECONDS = 0.2
# A local tool call needs room beyond the Hub's own wait, or the socket dies before the Hub answers.
TIMEOUT_MARGIN_SECONDS = 20.0
MIN_HTTP_TIMEOUT_SECONDS = 30.0
UNREACHABLE_HINT = ("Hub 未运行或地址不对：确认 Hub 已启动，bridge.json 里的 hub_url 正确"
                    f"（本机默认 {DEFAULT_HUB}）。")

_CONNECTION_OPTIONS = (
    ("--bridge", "bridge", "HUB_MCP_BRIDGE", "bridge.json 路径：Hub 为该会话写好的连接文件"),
    ("--hub", "hub", "HUB_MCP_URL", "Hub 基地址，例如 http://127.0.0.1:4444"),
    ("--session", "session", "HUB_MCP_SESSION", "Hub 会话 id"),
    ("--token", "token", "HUB_MCP_TOKEN", "会话 MCP token"),
)


class UsageError(Exception):
    """A bad command line or connection config: exit 2 with a JSON error, never a traceback."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message, self.hint = message, hint


class HubFailure(Exception):
    """The bridge call failed: exit 1, carrying the Hub's own words when it answered."""

    def __init__(self, message: str, *, hint: str = "", kind: str = "hub_error",
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.message, self.hint, self.kind, self.status = message, hint, kind, status


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would print usage text and exit with a bare status
        raise UsageError(f"命令行参数错误：{message}",
                         "用法：hub_mcp_cli.py --bridge <bridge.json> [list|ping|tools|call|batch|job] ...")


def _add_connection_options(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    for flag, dest, env_name, help_text in _CONNECTION_OPTIONS:
        default = argparse.SUPPRESS if suppress else (os.environ.get(env_name) or None)
        parser.add_argument(flag, dest=dest, default=default,
                            help=f"{help_text}（环境变量 {env_name}）")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="hub_mcp_cli.py", description="Call the Hub's session MCP pool.")
    _add_connection_options(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="{list,ping,tools,call,batch,job}")
    # The same options after the subcommand must not reset values given before it.
    common = argparse.ArgumentParser(add_help=False)
    _add_connection_options(common, suppress=True)

    subparsers.add_parser("list", parents=[common], help="列出本会话挂载的服务器")

    ping = subparsers.add_parser("ping", parents=[common], help="单台服务器的连通性")
    ping.add_argument("server")
    ping.add_argument("--timeout-ms", type=int, default=None, dest="timeout_ms")

    tools = subparsers.add_parser("tools", parents=[common], help="列出某台服务器的工具与参数 schema")
    tools.add_argument("server")
    tools.add_argument("--timeout-ms", type=int, default=None, dest="timeout_ms")

    call = subparsers.add_parser("call", parents=[common], help="调用一个工具")
    call.add_argument("server")
    call.add_argument("tool")
    call.add_argument("--arg", action="append", metavar="KEY=VALUE", help="字符串参数，原样保留类型")
    call.add_argument("--arg-json", action="append", metavar="KEY=JSON", dest="arg_json",
                      help="JSON 参数：数字、布尔、数组、对象、null")
    call.add_argument("--file", default=None, metavar="ARGS.JSON",
                      help="参数对象文件（UTF-8，可带 BOM）；--arg/--arg-json 覆盖同名键")
    call.add_argument("--timeout-ms", type=int, default=None, dest="timeout_ms")
    call.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS,
                      help=f"Hub 最多等待的秒数（默认 {DEFAULT_WAIT_SECONDS:g}），超时返回 status=running")
    call.add_argument("--out", default=None, metavar="OUT.JSON",
                      help="完整响应写入该文件，stdout 只打印摘要")

    batch = subparsers.add_parser("batch", parents=[common], help="一次投递多个调用")
    batch.add_argument("--file", required=True, metavar="BATCH.JSON",
                       help='调用数组，或 {"calls": [...]}')
    batch.add_argument("--timeout-ms", type=int, default=None, dest="timeout_ms",
                       help="为没有自带 timeoutMs 的每一项补默认超时")
    batch.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS)
    batch.add_argument("--out", default=None, metavar="OUT.JSON")

    job = subparsers.add_parser("job", parents=[common], help="取异步结果，或 --cancel 取消")
    job.add_argument("job_id")
    job.add_argument("--wait", type=float, default=DEFAULT_JOB_WAIT_SECONDS,
                     help=f"最多等待的秒数（默认 {DEFAULT_JOB_WAIT_SECONDS:g}）")
    job.add_argument("--cancel", action="store_true", help="取消这个 job 而不是等结果")
    job.add_argument("--out", default=None, metavar="OUT.JSON")
    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        raise UsageError("缺少子命令", "用法：hub_mcp_cli.py --bridge <bridge.json> list|ping|tools|call|batch|job")
    timeout_ms = getattr(args, "timeout_ms", None)
    if timeout_ms is not None and timeout_ms <= 0:
        raise UsageError("--timeout-ms 必须是正整数", "例如 --timeout-ms 60000。")
    wait = getattr(args, "wait", None)
    if wait is not None and wait < 0:
        raise UsageError("--wait 不能为负数", "它表示 Hub 最多等待的秒数，例如 --wait 30。")
    return args


def resolve_connection(args: argparse.Namespace) -> Tuple[str, str, str]:
    """Bridge file first, explicit flags over it; plain --hub/--session/--token for other engines."""
    hub = getattr(args, "hub", None)
    session = getattr(args, "session", None)
    token = getattr(args, "token", None)
    bridge = getattr(args, "bridge", None)
    if bridge:
        path = _path(bridge)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise UsageError(f"读不到 bridge 文件：{path}（{exc}）",
                             "这个文件由 Hub 在会话启动时写入；会话重开后再试，或改用 --hub/--session/--token。")
        except ValueError as exc:
            raise UsageError(f"bridge 文件不是合法 JSON：{path}（{exc}）",
                             "文件被截断或改坏了；重开会话让 Hub 重写，或改用 --hub/--session/--token。")
        if not isinstance(data, dict):
            raise UsageError(f"bridge 文件顶层必须是 JSON 对象：{path}",
                             "期望 hub_url、session_id、token 三个字段。")
        hub = hub or _as_text(data.get("hub_url"))
        session = session or _as_text(data.get("session_id"))
        token = token or _as_text(data.get("token"))
    missing = [label for label, value in (("hub_url（--hub）", hub),
                                          ("session_id（--session）", session),
                                          ("token（--token）", token)) if not value]
    if missing:
        raise UsageError("缺少连接信息：" + "、".join(missing),
                         "用 --bridge 指向 Hub 写的 bridge.json，或同时给出 --hub、--session、--token。")
    hub_url = str(hub).strip().rstrip("/")
    if not hub_url.lower().startswith(("http://", "https://")):
        raise UsageError(f"hub 地址必须是 http:// 或 https:// 开头：{hub_url}", "例如 http://127.0.0.1:4444。")
    return hub_url, str(session).strip(), str(token).strip()


def _as_text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _path(path_text: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path_text))))


def parse_arguments(pairs: Optional[List[str]], json_pairs: Optional[List[str]]) -> Dict[str, Any]:
    """--arg keeps the literal string; --arg-json parses one JSON value. JSON wins on a shared key."""
    values: Dict[str, Any] = {}
    for item in pairs or ():
        key, separator, value = str(item).partition("=")
        if not separator or not key:
            raise UsageError(f"--arg 需要 key=value 形式：{item!r}",
                             "字符串参数写成 --arg key=文本；空值写成 --arg key=。")
        values[key] = value
    for item in json_pairs or ():
        key, separator, text = str(item).partition("=")
        if not separator or not key:
            raise UsageError(f"--arg-json 需要 key=<json> 形式：{item!r}",
                             "例如 --arg-json n=3 或 --arg-json items='[1,2]'。")
        try:
            values[key] = json.loads(text)
        except ValueError as exc:
            raise UsageError(f"--arg-json {key} 不是合法 JSON：{text!r}（{exc}）",
                             "字符串请用 --arg；JSON 值里的字符串要带双引号。")
    return values


def read_json_file(path_text: str, label: str) -> Any:
    path = _path(path_text)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise UsageError(f"读不到{label}文件：{path}（{exc}）",
                         "确认路径存在且有读权限；路径含空格时用引号包住。")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise UsageError(f"{label}文件不是合法 JSON：{path}（{exc}）", "文件需为 UTF-8 编码的 JSON。")


def build_arguments(args: argparse.Namespace) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    if getattr(args, "file", None):
        data = read_json_file(args.file, "参数")
        if not isinstance(data, dict):
            raise UsageError(f"参数文件顶层必须是 JSON 对象：{args.file}",
                             '写成 {"path": "..."} 这样的键值表，参数名作键。')
        values.update(data)
    values.update(parse_arguments(getattr(args, "arg", None), getattr(args, "arg_json", None)))
    return values


def call_body(args: argparse.Namespace) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "action": "call",
        "server": args.server,
        "tool": args.tool,
        "arguments": build_arguments(args),
        "waitMs": wait_ms(args.wait),
    }
    if args.timeout_ms is not None:
        body["timeoutMs"] = args.timeout_ms
    return body


def batch_body(args: argparse.Namespace) -> Dict[str, Any]:
    data = read_json_file(args.file, " batch ")
    calls = data.get("calls") if isinstance(data, dict) else data
    if not isinstance(calls, list) or not calls:
        raise UsageError(f"batch 文件里没有调用数组：{args.file}",
                         '顶层写成 [...]，或 {"calls": [...]}；每项含 server 与 tool。')
    normalised = []
    for item in calls:
        if not isinstance(item, dict):
            raise UsageError(f"batch 的每一项都必须是对象：{item!r}",
                             '每项形如 {"server": "fs", "tool": "...", "arguments": {}}。')
        entry = dict(item)
        if args.timeout_ms is not None:
            entry.setdefault("timeoutMs", args.timeout_ms)
        normalised.append(entry)
    return {"action": "batch", "calls": normalised, "waitMs": wait_ms(args.wait)}


def wait_ms(seconds: Optional[float]) -> int:
    return int(round(max(0.0, float(seconds or 0.0)) * 1000))


def http_timeout(wait_seconds: Optional[float], timeout_ms: Optional[int] = None) -> float:
    wanted = max(float(wait_seconds or 0.0), float(timeout_ms or 0) / 1000.0)
    return max(MIN_HTTP_TIMEOUT_SECONDS, wanted + TIMEOUT_MARGIN_SECONDS)


def action_url(hub_url: str, session_id: str) -> str:
    return f"{hub_url}/api/sessions/{urllib.parse.quote(session_id, safe='')}/mcp"


def job_url(hub_url: str, session_id: str, job_id: str, wait_milliseconds: Optional[int] = None) -> str:
    base = f"{hub_url}/api/sessions/{urllib.parse.quote(session_id, safe='')}/mcp/jobs/" \
           f"{urllib.parse.quote(str(job_id), safe='')}"
    if wait_milliseconds is None:
        return base
    return f"{base}?{urllib.parse.urlencode({'wait_ms': wait_milliseconds})}"


def build_opener() -> urllib.request.OpenerDirector:
    # The Hub listens on loopback; a system proxy would only break it.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace") if raw else ""


def _shorten(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _http_hint(status: int) -> str:
    if status in (401, 403):
        return "Hub 拒绝了这个 token：它由 Hub 按会话派生，改用手填通常不对；确认 bridge.json 属于当前会话。"
    if 400 <= status < 500:
        return "看 error.message；服务器名、工具名或参数可能写错，先 tools <server> 查 schema。"
    return "Hub 内部出错：稍后重试；持续失败时看 Hub 日志。"


def request_json(opener: urllib.request.OpenerDirector, method: str, url: str, token: str,
                 payload: Optional[Dict[str, Any]] = None,
                 timeout: float = MIN_HTTP_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """One HTTP exchange with the Hub; anything but a JSON object is a HubFailure, never a traceback."""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-Hub-Mcp-Token", token)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = _decode(exc.read())
        except Exception:  # a broken error body still must not surface as a traceback
            body = ""
        raise HubFailure(f"Hub 返回 HTTP {exc.code}：{_shorten(body) or exc.reason}",
                         hint=_http_hint(exc.code),
                         kind="auth" if exc.code in (401, 403) else
                         ("bad_request" if 400 <= exc.code < 500 else "hub_error"),
                         status=exc.code) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise HubFailure(f"等 Hub 响应超时：{reason}",
                             "服务端可能还在处理；稍后重试，或减小 --wait / --timeout-ms。",
                             kind="timeout") from None
        raise HubFailure(f"连不上 Hub：{reason}", hint=UNREACHABLE_HINT, kind="unreachable") from None
    except (socket.timeout, TimeoutError) as exc:
        raise HubFailure(f"等 Hub 响应超时：{exc}",
                         "服务端可能还在处理；稍后重试，或减小 --wait / --timeout-ms。",
                         kind="timeout") from None
    except (http.client.HTTPException, OSError) as exc:
        raise HubFailure(f"连接 Hub 失败：{exc}", hint=UNREACHABLE_HINT, kind="unreachable") from None
    if not raw:
        raise HubFailure("Hub 返回了空响应", hint="确认 hub_url 指向 Hub 的 HTTP 端口。", kind="bad_response")
    try:
        answer = json.loads(_decode(raw))
    except ValueError as exc:
        raise HubFailure(f"Hub 响应不是 JSON：{_shorten(_decode(raw))}（{exc}）",
                         hint=UNREACHABLE_HINT, kind="bad_response") from None
    if not isinstance(answer, dict):
        raise HubFailure(f"Hub 响应不是 JSON 对象，而是 {type(answer).__name__}",
                         hint=UNREACHABLE_HINT, kind="bad_response")
    return answer


def job_wait_chunks(wait_seconds: Optional[float], elapsed_seconds: float = 0.0) -> int:
    """The ``wait_ms`` to ask for now: never more than the Hub's bound, never more than is left."""
    total_ms = max(0, int(round(float(wait_seconds or 0.0) * 1000)))
    remaining_ms = max(0, total_ms - int(round(max(0.0, elapsed_seconds) * 1000)))
    return min(MAX_JOB_POLL_WAIT_MS, remaining_ms)


def poll_job(opener: urllib.request.OpenerDirector, hub_url: str, session_id: str, job_id: str,
             wait_seconds: Optional[float], token: str,
             clock=time.monotonic, sleep=time.sleep) -> Dict[str, Any]:
    """Read one job over chunks of at most ``MAX_JOB_POLL_WAIT_MS`` while it is still running.

    ``job <id> --wait 90`` used to hand the whole 90 s to the Hub in a single long poll: past its
    own bound the Hub answers "running" anyway, and the client can be left waiting on a socket the
    server already closed. Each chunk therefore asks for at most a minute, with its own socket
    timeout of that chunk plus the usual margin, until the job finishes or --wait is spent.
    """
    total = max(0.0, float(wait_seconds or 0.0))
    deadline = clock() + total
    answer: Dict[str, Any] = {}
    while True:
        elapsed = max(0.0, total - max(0.0, deadline - clock()))
        chunk_ms = job_wait_chunks(total, elapsed)
        answer = request_json(
            opener, "GET", job_url(hub_url, session_id, job_id, chunk_ms), token, None,
            timeout=max(MIN_HTTP_TIMEOUT_SECONDS, chunk_ms / 1000.0 + TIMEOUT_MARGIN_SECONDS),
        )
        if answer.get("status") != "running" or answer.get("ok") is False:
            return answer
        remaining = deadline - clock()
        if remaining <= 0:
            return answer
        # The Hub capped its long poll and the job is still running: pause briefly, never spin.
        sleep(min(JOB_POLL_PAUSE_SECONDS, remaining))


def run_command(args: argparse.Namespace, hub_url: str, session_id: str, token: str,
                opener: urllib.request.OpenerDirector) -> Dict[str, Any]:
    command = args.command
    timeout_ms = getattr(args, "timeout_ms", None)
    if command == "list":
        return request_json(opener, "POST", action_url(hub_url, session_id), token,
                            {"action": "list"}, timeout=http_timeout(0, timeout_ms))
    if command in ("ping", "tools"):
        body: Dict[str, Any] = {"action": command, "server": args.server}
        if timeout_ms is not None:
            body["timeoutMs"] = timeout_ms
        return request_json(opener, "POST", action_url(hub_url, session_id), token, body,
                            timeout=http_timeout(0, timeout_ms))
    if command == "call":
        return request_json(opener, "POST", action_url(hub_url, session_id), token, call_body(args),
                            timeout=http_timeout(args.wait, timeout_ms))
    if command == "batch":
        return request_json(opener, "POST", action_url(hub_url, session_id), token, batch_body(args),
                            timeout=http_timeout(args.wait, timeout_ms))
    if command == "job":
        if args.cancel:
            return request_json(opener, "POST", action_url(hub_url, session_id), token,
                                {"action": "cancel", "job": args.job_id},
                                timeout=MIN_HTTP_TIMEOUT_SECONDS)
        return poll_job(opener, hub_url, session_id, args.job_id, args.wait, token)
    raise UsageError(f"未知子命令：{command}", "可用：list、ping、tools、call、batch、job。")


def exit_code_for(answer: Dict[str, Any]) -> int:
    """ok=false is a bridge failure (1); a still-running call is 2; anything else, including a
    tool result with isError=true, is 0 because the bridge itself delivered the answer."""
    if answer.get("ok") is False:
        return EXIT_FAILED
    if answer.get("status") == "running":
        return EXIT_USAGE
    return EXIT_OK


def annotate_running(answer: Dict[str, Any]) -> Dict[str, Any]:
    if answer.get("status") != "running" or answer.get("hint"):
        return answer
    copy = dict(answer)
    job = answer.get("job")
    copy["hint"] = (f"调用仍在运行，用 job {job} 取结果。" if job
                    else "调用仍在运行：稍后用 job <job_id> 取结果。")
    return copy


def write_out(path_text: str, answer: Dict[str, Any]) -> Tuple[Path, int]:
    path = _path(path_text)
    try:
        text = json.dumps(answer, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise UsageError(f"响应无法序列化为 JSON：{exc}", "去掉 --out 直接看 stdout。")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bytes, not text: text mode would turn every LF into CRLF and break the reported size.
        path.write_bytes(text.encode("utf-8"))
    except OSError as exc:
        raise UsageError(f"写不到输出文件：{path}（{exc}）", "确认目录存在且有写权限。")
    return path, len(text.encode("utf-8"))


def out_summary(answer: Dict[str, Any], path: Path, size: int) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "ok": answer.get("ok"),
        "status": answer.get("status"),
        "writtenTo": str(path.resolve()),
        "bytes": size,
        "isError": answer.get("isError"),
    }
    if answer.get("status") == "running" and answer.get("job"):
        summary["job"] = answer["job"]
    return summary


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_error(message: str, hint: str, *, kind: str = "bad_request", code: int = EXIT_USAGE,
               status: Optional[int] = None) -> int:
    error: Dict[str, Any] = {"kind": kind, "message": message}
    if hint:
        error["hint"] = hint
    if status is not None:
        error["http_status"] = status
    _emit({"ok": False, "error": error})
    return code


def main(argv: Optional[List[str]] = None) -> int:
    # A Windows console code page cannot carry every character a tool result may hold.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        args = parse_args(argv)
    except UsageError as exc:
        return emit_error(exc.message, exc.hint, code=EXIT_USAGE)
    try:
        connection = resolve_connection(args)
        opener = build_opener()
        answer = run_command(args, *connection, opener)
    except UsageError as exc:
        return emit_error(exc.message, exc.hint, code=EXIT_USAGE)
    except HubFailure as exc:
        return emit_error(exc.message, exc.hint, kind=exc.kind, code=EXIT_FAILED, status=exc.status)
    answer = annotate_running(answer)
    code = exit_code_for(answer)
    if getattr(args, "out", None):
        try:
            path, size = write_out(args.out, answer)
        except UsageError as exc:
            return emit_error(exc.message, exc.hint, kind="io_error", code=EXIT_FAILED)
        _emit(out_summary(answer, path, size))
        return code
    _emit(answer)
    return code


if __name__ == "__main__":
    sys.exit(main())
