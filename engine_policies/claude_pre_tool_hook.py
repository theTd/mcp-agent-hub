#!/usr/bin/env python3
"""Claude Code PreToolUse & PermissionRequest hook for MCP Agent Hub high-risk approval mode.

Goals:
1. PreToolUse:
   - Block/ask on destructive commands (Remove-Item, rm, del, rmdir, git reset --hard,
     git push --force, git clean -f, disk/system commands, etc.).
   - Allow benign read-only PowerShell/Bash inspection commands even when they contain
     PowerShell subexpressions `$()`, expandable strings, `try { ... } catch { ... }`
     blocks, or read-only .NET type inspection (such as
     `[Security.Principal.WindowsPrincipal]::new(...)`), preventing false-positive
     approval prompts on read-only queries (Issue N2 / C4).
   - Defer write/mutating commands (`Set-Content`, `New-Item`, `Out-File`, redirections, etc.)
     that use `$()` / script blocks / `.NET` methods to Claude's `acceptEdits` validator.
2. PermissionRequest:
   - Whenever Claude Code is about to display an interactive permission prompt for ANY tool,
     append a structured JSONL event (`tool_name`, `tool_input`, `permission_suggestions`,
     `session_id`) to `.runtime_mcp_configs/claude_hooks_<session_id>.jsonl` (and
     `HUB_CLAUDE_HOOK_LOG` if set) so the Hub can register the approval request directly
     from hook/transcript data without fragile terminal screen scraping.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time

# High-risk destructive patterns that must ALWAYS require user/parent approval
HIGH_RISK_PATTERNS = (
    re.compile(
        r"(?:^|[;&|()\s{])(?<![-\w/\\.])(?:Remove-Item|rm|ri|del|erase|rmdir|rd)(?![-\w])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[;&|()\s{])(?<![-\w/\\.])(?:Clear-Disk|Format-Volume|format\.com|diskpart|shutdown|Restart-Computer|Stop-Computer)(?![-\w])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![-\w])\bgit\s+(?:reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+(?:--force|-f|\+)|branch\s+-D)(?![-\w])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![-\w])\b(?:npm|pnpm|yarn)\s+publish(?![-\w])",
        re.IGNORECASE,
    ),
    re.compile(
        r"\[(?:System\.)?IO\.(?:Directory|File|DirectoryInfo|FileInfo)\]::\s*Delete\s*\(",
        re.IGNORECASE,
    ),
)

# Commands or operators that mutate files, processes, registry, network, or state.
# If any of these appear in a command, the hook does NOT auto-allow it, leaving
# Claude Code's `acceptEdits` mode + syntax checks to decide.
WRITE_OR_MUTATING_PATTERNS = (
    re.compile(
        r"(?:^|[;&|()\s{])(?<![-\w/\\.])(?:"
        r"Set-Content|sc|Add-Content|ac|Out-File|New-Item|ni|Copy-Item|cp|cpi|copy|"
        r"Move-Item|mv|mi|move|Rename-Item|ren|rni|Clear-Content|clc|Clear-Item|cli|"
        r"Set-Item|si|Set-ItemProperty|sp|New-ItemProperty|Remove-ItemProperty|rp|"
        r"Expand-Archive|Compress-Archive|Export-Csv|Export-Clixml|Tee-Object|tee|"
        r"Invoke-Expression|iex|Invoke-Command|icm|Invoke-Item|ii|Start-Process|saps|start|"
        r"Stop-Process|kill|spps|taskkill|Invoke-WebRequest|iwr|curl|wget|Invoke-RestMethod|irm|"
        r"Start-BitsTransfer|New-Service|Set-Service|Stop-Service|Restart-Service|"
        r"Register-ScheduledTask|New-ScheduledTask|Set-ScheduledTask|Unregister-ScheduledTask|"
        r"schtasks|reg\.exe|reg\s+(?:add|delete|import|restore|save|load|unload)|"
        r"xcopy|robocopy|sed\s+-i|chmod|chown|dd|mkfs"
        r")(?![-\w])",
        re.IGNORECASE,
    ),
    # File redirection (`>`, `>>`, `2>`, `*>`) outside `2>&1` or `1>&2`
    re.compile(r"(?<![0-9>&-])>>?(?![>&])"),
    # .NET mutating methods
    re.compile(
        r"::\s*(?:WriteAllText|WriteAllLines|WriteAllBytes|AppendAllText|AppendAllLines|"
        r"Create|CreateDirectory|Move|Copy|Delete|OpenWrite|Start|Kill|SetEnvironmentVariable)\s*\(",
        re.IGNORECASE,
    ),
)


def _strip_safe_redirections(cmd: str) -> str:
    """Remove harmless stream merges like `2>&1` and `1>&2` before checking `>`."""
    return re.sub(r"\b[12\*]>&[12]\b", "", cmd)


def evaluate_command(command: str) -> dict:
    cmd = (command or "").strip()
    if not cmd:
        return {}

    # Always allow hub_worker.py bridge commands
    if "hub_worker.py" in cmd:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "Hub worker bridge command",
            }
        }

    # 1. High-risk destructive commands -> always ask
    for pat in HIGH_RISK_PATTERNS:
        if pat.search(cmd):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": "High-risk destructive command requires approval",
                }
            }

    # 2. Mutating / write / process / network commands -> passthrough to acceptEdits
    cleaned = _strip_safe_redirections(cmd)
    for pat in WRITE_OR_MUTATING_PATTERNS:
        if pat.search(cleaned):
            return {}

    # 3. Pure read-only command -> allow (prevents false-positive prompts on `$()`, `try/catch`, read-only `.NET`)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "Read-only command allowed by hub policy",
        }
    }


def _record_permission_request(payload: dict) -> None:
    session_id = str(payload.get("session_id") or "").strip()
    tool_name = str(payload.get("tool_name") or "").strip()
    tool_input = payload.get("tool_input")
    suggestions = payload.get("permission_suggestions")
    record = {
        "timestamp": round(time.time(), 3),
        "hook_event_name": "PermissionRequest",
        "session_id": session_id,
        "tool_use_id": str(payload.get("tool_use_id") or ""),
        "tool_name": tool_name,
        "tool_input": tool_input if isinstance(tool_input, dict) else {"raw": str(tool_input or "")},
        "permission_suggestions": suggestions if isinstance(suggestions, list) else [],
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    targets: list[Path] = []
    env_path = os.environ.get("HUB_CLAUDE_HOOK_LOG", "").strip()
    if env_path:
        targets.append(Path(env_path))
    if session_id and re.match(r"^[A-Za-z0-9_.\-]+$", session_id):
        runtime_dir = Path(__file__).resolve().parent.parent / ".runtime_mcp_configs"
        default_path = runtime_dir / f"claude_hooks_{session_id}.jsonl"
        if default_path not in targets:
            targets.append(default_path)
    for target in targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except Exception:
        return 0

    event_name = str(payload.get("hook_event_name") or "PreToolUse").strip()
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}

    if event_name == "PermissionRequest":
        cmd = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
        if tool_name in ("Bash", "PowerShell") and "hub_worker.py" in cmd:
            out = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }
            sys.stdout.write(json.dumps(out))
            sys.stdout.flush()
            return 0
        _record_permission_request(payload)
        return 0

    if tool_name not in ("Bash", "PowerShell"):
        return 0

    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    decision = evaluate_command(command)
    if decision:
        sys.stdout.write(json.dumps(decision))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
