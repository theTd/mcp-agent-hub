"""Validate session MCP definitions and translate them to native CLI arguments."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re


def _string(value, label, *, nonempty=False):
    if not isinstance(value, str) or "\x00" in value or (nonempty and not value.strip()):
        raise ValueError(f"{label} 必须是不含 NUL 的{'非空' if nonempty else ''}字符串")
    return value


def _string_map(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是字符串键值对象")
    return {_string(k, label, nonempty=True): _string(v, label) for k, v in value.items()}


def normalize_mcp_config(servers=None, disabled=None, strict=False):
    if servers is None:
        servers = {}
    if disabled is None:
        disabled = []
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers 必须是服务器名称到配置的对象")
    if not isinstance(disabled, list):
        raise ValueError("disabled_mcp_servers 必须是字符串数组")
    if type(strict) is not bool:
        raise ValueError("strict_mcp_config 必须是布尔值")
    result = {}
    for name, spec in servers.items():
        _string(name, "MCP 服务器名称", nonempty=True)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("新增 MCP 服务器名称只能包含英文字母、数字、下划线和连字符")
        if not isinstance(spec, dict):
            raise ValueError(f"mcp_servers.{name} 必须是对象")
        if any(not isinstance(key, str) for key in spec):
            raise ValueError(f"mcp_servers.{name} 的字段名称必须是字符串")
        http = "url" in spec
        if http == ("command" in spec):
            raise ValueError(f"mcp_servers.{name} 必须且只能提供 url 或 command")
        allowed = {"url", "headers"} if http else {"command", "args", "env"}
        if set(spec) - allowed:
            raise ValueError(f"mcp_servers.{name} 不支持字段：{', '.join(sorted(set(spec) - allowed))}")
        entry = {}
        for key, value in spec.items():
            label = f"mcp_servers.{name}.{key}"
            if key in ("url", "command"):
                entry[key] = _string(value, label, nonempty=True)
                if key == "url" and not re.match(r"^https?://[^/\s]+", value, re.I):
                    raise ValueError(f"{label} 必须是 HTTP(S) URL")
            elif key == "args":
                if not isinstance(value, list):
                    raise ValueError(f"{label} 必须是字符串数组")
                entry[key] = [_string(v, label) for v in value]
            else:
                entry[key] = _string_map(value, label)
        result[name] = entry
    names = []
    for name in disabled:
        _string(name, "disabled_mcp_servers 项", nonempty=True)
        if name != name.strip():
            raise ValueError("禁用 MCP 名称不能含首尾空白")
        if name in result:
            raise ValueError(f"MCP「{name}」不能同时新增和禁用")
        if name not in names:
            names.append(name)
    return {"mcp_servers": result, "disabled_mcp_servers": names, "strict_mcp_config": strict}


def mcp_config_active(config):
    return bool(config["mcp_servers"] or config["disabled_mcp_servers"] or config["strict_mcp_config"])


def validate_mcp_engine(engine, kind, config):
    if mcp_config_active(config) and (engine not in ("codex", "claude") or kind != "tui"):
        raise ValueError("会话级 MCP 配置仅支持 Codex / Claude TUI")
    if config["strict_mcp_config"] and engine != "claude":
        raise ValueError("strict_mcp_config 仅支持 Claude；Codex 不提供完整 MCP 白名单")


def _settings_tokens(args):
    i = 0
    while i < len(args):
        key, equal, value = args[i].partition("=")
        if key == "--settings":
            if not equal:
                if i + 1 >= len(args):
                    raise ValueError("--settings 缺少配置值")
                value = args[i + 1]
            yield i, 1 if equal else 2, value
            i += 1 if equal else 2
        else:
            i += 1


def _read_settings(value, cwd):
    try:
        if value.lstrip().startswith("{"):
            data = json.loads(value)
        else:
            path = Path(os.path.expandvars(os.path.expanduser(value)))
            if not path.is_absolute():
                path = Path(cwd) / path
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        return data
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法合并 Claude --settings：{exc}") from exc


def check_mcp_arg_conflicts(engine, args, cwd):
    """Do not silently combine native MCP controls with structured controls."""
    if engine == "claude":
        flags = {"--mcp-config", "--strict-mcp-config", "--plugin-dir", "--plugin-url",
                 "--chrome", "--no-chrome", "--safe-mode", "--bare"}
        if any(a.split("=", 1)[0] in flags for a in args):
            raise ValueError("结构化 MCP 配置与 Profile / extra_args 中的原生 MCP 参数冲突")
        mcp_keys = {"mcpServers", "allowedMcpServers", "deniedMcpServers", "managedMcpServers",
                    "disabledMcpServers", "enabledMcpServers", "enabledMcpjsonServers",
                    "disabledMcpjsonServers", "enableAllProjectMcpServers",
                    "disableClaudeAiConnectors", "enabledPlugins", "allowAllClaudeAiMcps",
                    "allowManagedMcpServersOnly", "allowClaudeInChromeWithManagedMcp"}
        for _, _, value in _settings_tokens(args):
            if mcp_keys.intersection(_read_settings(value, cwd)):
                raise ValueError("结构化 MCP 配置与 --settings 中的 MCP 配置冲突")
    elif engine == "codex":
        for i, arg in enumerate(args):
            value = None
            if arg in ("-c", "--config") and i + 1 < len(args):
                value = args[i + 1]
            elif arg.startswith("--config="):
                value = arg[len("--config="):]
            elif arg.startswith("-c") and len(arg) > 2:
                value = arg[2:].lstrip("=")
            if value is not None:
                key = value.split("=", 1)[0].strip().replace('"', '').replace("'", "")
                if key == "mcp_servers" or key.startswith("mcp_servers.") or key == "plugins" or key.startswith("plugins."):
                    raise ValueError("结构化 MCP 配置与 Profile / extra_args 中的 Codex MCP 或插件覆盖冲突")


def _merge_settings(left, right):
    merged = deepcopy(left)
    for key, value in right.items():
        old = merged.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = _merge_settings(old, value)
        elif isinstance(old, list) and isinstance(value, list):
            merged[key] = old + [deepcopy(item) for item in value if item not in old]
        else:
            merged[key] = deepcopy(value)
    return merged


def replace_claude_settings(args, settings):
    drop = {j for i, width, _ in _settings_tokens(args) for j in range(i, i + width)}
    return [a for i, a in enumerate(args) if i not in drop] + [
        "--settings=" + json.dumps(settings, ensure_ascii=False, separators=(",", ":"))]


def merge_claude_settings(args, cwd, *, approval_path=None, disabled=()):
    """Keep user settings separately so resume can reapply the current Hub approval policy."""
    user_settings, approval = {}, {}
    for _, _, value in _settings_tokens(args):
        data = _read_settings(value, cwd)
        if approval_path and Path(value) == Path(approval_path):
            approval = _merge_settings(approval, data)
        else:
            user_settings = _merge_settings(user_settings, data)
    if disabled:
        user_settings = _merge_settings(user_settings, {
            "deniedMcpServers": [{"serverName": name} for name in disabled]})
    # The Hub hook and ask rules survive user settings; native CLI permission mode stays authoritative.
    combined = _merge_settings(user_settings, approval)
    return replace_claude_settings(args, combined), user_settings


def _toml(value):
    # Validated server definitions contain strings, lists and maps, plus generated enabled booleans.
    # JSON strings are TOML basic strings, except JSON's unsupported \\/ escape (never emitted here).
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ",".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{_toml(k)}={_toml(v)}" for k, v in value.items()) + "}"
    raise ValueError("不支持的 MCP TOML 值")


def build_mcp_args(engine, config):
    args = []
    if engine == "codex":
        servers = {}
        for name, spec in config["mcp_servers"].items():
            native = {("http_headers" if k == "headers" else k): v for k, v in spec.items()}
            # Re-enable an existing disabled name, while otherwise retaining native merge behavior.
            servers[name] = dict(native, enabled=True)
        for name in config["disabled_mcp_servers"]:
            servers[name] = {"enabled": False}
        if servers:
            # Codex splits -c paths on dots without decoding quoted keys. Put literal
            # server names inside one TOML value, where the real TOML parser handles them.
            args.extend(["-c", "mcp_servers=" + _toml(servers)])
    elif engine == "claude":
        if config["mcp_servers"] or config["strict_mcp_config"]:
            servers = {name: dict(spec, type="http" if "url" in spec else "stdio")
                       for name, spec in config["mcp_servers"].items()}
            args.append("--mcp-config=" + json.dumps({"mcpServers": servers}, ensure_ascii=False, separators=(",", ":")))
        if config["strict_mcp_config"]:
            args.append("--strict-mcp-config")
    return args
