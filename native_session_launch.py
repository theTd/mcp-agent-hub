"""Remove retired Hub worker additions while preserving user launch configuration."""
import json
import os
from pathlib import Path
import re


def strip_legacy_worker_bridge_args(args, runtime_dir):
    def bridge_dir(value):
        # Also recognise snapshots from an earlier Hub data location.
        path = Path(str(value).replace("\\", "/"))
        return path.name.startswith("agy_sess_") and (
            path.parent.name == ".runtime_mcp_configs"
            or os.path.normcase(str(path.parent)) == os.path.normcase(str(Path(runtime_dir)))
        )

    out = []
    i = 0
    while i < len(args):
        arg = str(args[i])
        key, equal, inline = arg.partition("=")
        pair = not equal and i + 1 < len(args)
        value = inline if equal else str(args[i + 1]) if pair else ""
        width = 1 if equal else 2
        if key == "--append-system-prompt" and "[Sub-Agent Bridge Info:" in value:
            # Keep any user prompt preceding the Hub-generated suffix.
            value = value.split("[Sub-Agent Bridge Info:", 1)[0].rstrip()
        elif key in ("--allowedTools", "--allowed-tools") and "hub_worker.py" in value:
            value = re.sub(r"(?:Bash|PowerShell)\(\*hub_worker\.py\*\)", "", value)
            value = ",".join(part.strip() for part in value.split(",") if part.strip())
        elif key == "--add-dir" and bridge_dir(value):
            value = ""
        elif key == "--settings":
            try:
                settings = json.loads(value)
            except (ValueError, TypeError):
                settings = None
            if isinstance(settings, dict):
                changed = False
                trusted = settings.get("trustedWorkspaces")
                if isinstance(trusted, list) and any(bridge_dir(p) for p in trusted):
                    settings["trustedWorkspaces"] = [p for p in trusted if not bridge_dir(p)]
                    changed = True
                permissions = settings.get("permissions")
                if isinstance(permissions, dict) and isinstance(permissions.get("allow"), list):
                    rules = permissions["allow"]
                    if "mcp(hub-bridge/*)" in rules:
                        permissions["allow"] = [r for r in rules if r != "mcp(hub-bridge/*)"]
                        changed = True
                if changed:
                    value = json.dumps(settings, ensure_ascii=False, separators=(",", ":"))
                else:
                    out.append(arg)
                    i += 1
                    continue
            else:
                out.append(arg)
                i += 1
                continue
        else:
            out.append(arg)
            i += 1
            continue
        if value:
            out.extend([f"{key}={value}"] if equal else [key, value])
        i += width
    return out
