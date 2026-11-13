import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from dsh_endpoint import dsh_url


_TOKEN_COUNT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(KI|K|MI|M|GI|G)?\s*$", re.IGNORECASE)
_TOKEN_MULTIPLIERS = {
    None: 1,
    "": 1,
    "K": 1_000,
    "M": 1_000_000,
    "G": 1_000_000_000,
    "KI": 1024,
    "MI": 1024 * 1024,
    "GI": 1024 * 1024 * 1024,
}


def parse_token_count(raw: Any) -> Optional[int]:
    """Parses an integer token count or compact suffix string (e.g. '1M', '256k', '256Ki')."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        val = int(round(raw))
        return val if val > 0 else None
    text = str(raw).strip().replace(",", "").replace("_", "")
    if not text:
        return None
    m = _TOKEN_COUNT_RE.match(text)
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "").upper()
    mult = _TOKEN_MULTIPLIERS.get(unit)
    if mult is None:
        return None
    val = int(round(num * mult))
    return val if val > 0 else None


def _strip_yaml_inline_comment(text: str) -> str:
    in_quote: Optional[str] = None
    for idx, ch in enumerate(text):
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
        elif ch == "#" and (idx == 0 or text[idx - 1].isspace()):
            return text[:idx].rstrip()
    return text.rstrip()


def parse_omp_models_yml(path: Path) -> Dict[str, Dict[str, int]]:
    """Extracts exact {provider/model: {context_window_tokens, max_output_tokens}} from ~/.omp/agent/models.yml."""
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}

    result: Dict[str, Dict[str, int]] = {}
    providers_indent: Optional[int] = None
    current_provider: Optional[str] = None
    provider_indent: Optional[int] = None
    models_indent: Optional[int] = None
    item_indent: Optional[int] = None
    current_item: Optional[Dict[str, Any]] = None

    def flush_item() -> None:
        nonlocal current_item
        if not current_item or not current_provider:
            current_item = None
            return
        mid = str(current_item.get("id") or "").strip().strip("'\"")
        if mid:
            entry: Dict[str, int] = {}
            if current_item.get("context_window_tokens"):
                entry["context_window_tokens"] = current_item["context_window_tokens"]
            if current_item.get("max_output_tokens"):
                entry["max_output_tokens"] = current_item["max_output_tokens"]
            if entry:
                result[f"{current_provider}/{mid}"] = entry
        current_item = None

    for raw in lines:
        cleaned = _strip_yaml_inline_comment(raw)
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        stripped = cleaned.lstrip(" ")

        if providers_indent is None:
            if stripped == "providers:":
                providers_indent = indent
            continue

        if indent <= providers_indent and not stripped.startswith("-"):
            flush_item()
            if stripped == "providers:":
                providers_indent = indent
                current_provider = None
                provider_indent = None
                models_indent = None
                item_indent = None
            else:
                providers_indent = None
                current_provider = None
                provider_indent = None
                models_indent = None
                item_indent = None
            continue

        if provider_indent is None or (indent <= provider_indent and not stripped.startswith("-")):
            flush_item()
            models_indent = None
            item_indent = None
            if stripped.endswith(":"):
                current_provider = stripped[:-1].strip().strip("'\"")
                provider_indent = indent
            else:
                current_provider = None
                provider_indent = None
            continue

        if models_indent is not None and indent <= models_indent and not stripped.startswith("-"):
            flush_item()
            models_indent = None
            item_indent = None

        if models_indent is None:
            if stripped == "models:":
                models_indent = indent
            continue

        if stripped.startswith("- ") or stripped == "-":
            if item_indent is None or indent <= item_indent:
                flush_item()
                item_indent = indent
                current_item = {}
                rest = stripped[1:].strip()
                if ":" in rest:
                    k, v = rest.split(":", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k == "id":
                        current_item["id"] = v
                    elif k == "contextWindow":
                        tok = parse_token_count(v)
                        if tok:
                            current_item["context_window_tokens"] = tok
                    elif k == "maxTokens":
                        tok = parse_token_count(v)
                        if tok:
                            current_item["max_output_tokens"] = tok
                continue

        if current_item is not None and (item_indent is None or indent > item_indent) and ":" in stripped:
            k, v = stripped.split(":", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k == "id":
                current_item["id"] = v
            elif k == "contextWindow":
                tok = parse_token_count(v)
                if tok:
                    current_item["context_window_tokens"] = tok
            elif k == "maxTokens":
                tok = parse_token_count(v)
                if tok:
                    current_item["max_output_tokens"] = tok

    flush_item()
    return result


class AgentModelDiscovery:
    """
    Automatically discovers available LLM models and supported thinking/effort levels
    across all locally installed Agent CLIs (omp, kimi, agy, codex, dsh).
    """

    # A forced refresh younger than this is answered from cache, so parallel
    # callers share one in-flight discovery run instead of spawning it N times.
    REFRESH_FLOOR_SEC = 5.0

    def __init__(self):
        self.home = Path(os.path.expanduser("~"))
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_at: float = 0.0
        self._cache_ttl: float = 120.0
        self._lock = threading.Lock()
        self._claude_binary_cache: Dict[tuple, Dict[str, Any]] = {}
        self._claude_cache_lock = threading.Lock()

    def peek_cache(self) -> Optional[Dict[str, Any]]:
        """Returns whatever has already been discovered, without ever running a CLI (never blocks)."""
        return self._cache

    def discover_all(self, force_refresh: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force_refresh and self._cache and (now - self._cache_at) < self._cache_ttl:
            return self._cache

        with self._lock:
            now = time.time()
            age = now - self._cache_at
            if self._cache is not None and age < self._cache_ttl and (not force_refresh or age < self.REFRESH_FLOOR_SEC):
                # A concurrent caller already refreshed while we waited on the lock.
                return self._cache

            engines = {
                "omp": self._discover_omp(),
                "kimi": self._discover_kimi(),
                "agy": self._discover_agy(),
                "codex": self._discover_codex(),
                "claude": self._discover_claude(),
                "dsh": self._discover_dsh(),
            }
            self._cache = {
                "discovered_at": round(now, 3),
                "engines": engines,
            }
            self._cache_at = now
            return self._cache

    def _discover_omp(self) -> Dict[str, Any]:
        omp_bin = Path(shutil.which("omp") or (self.home / "AppData" / "Local" / "omp" / "omp.exe"))
        thinking_levels = ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"]
        models: List[Dict[str, Any]] = []
        seen_ids = set()
        yml_models = parse_omp_models_yml(self.home / ".omp" / "agent" / "models.yml")

        # 1. Run `omp.exe models` to parse live provider tables
        if omp_bin.exists():
            try:
                r = subprocess.run(
                    [str(omp_bin), "models"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=6.0,
                )
                current_provider = None
                for raw_line in r.stdout.splitlines():
                    line = raw_line.strip()
                    m_prov = re.match(r"^([a-zA-Z0-9_\-]+)\s+\(\d+\)$", line)
                    if m_prov:
                        current_provider = m_prov.group(1)
                        continue
                    if line.startswith("│") and current_provider:
                        cols = [c.strip() for c in line.strip("│").split("│")]
                        if len(cols) >= 4 and cols[0] != "model":
                            m_name, ctx, max_out, think_col = cols[0], cols[1], cols[2], cols[3]
                            full_id = f"{current_provider}/{m_name}"
                            if full_id not in seen_ids:
                                seen_ids.add(full_id)
                                m_thinks = (
                                    [x.strip() for x in think_col.split(",") if x.strip()]
                                    if "," in think_col
                                    else (["low", "medium", "high", "max"] if think_col == "yes" else ["off"])
                                )
                                yml_info = yml_models.get(full_id) or {}
                                ctx_tokens = yml_info.get("context_window_tokens") or parse_token_count(ctx)
                                models.append(
                                    {
                                        "id": full_id,
                                        "provider": current_provider,
                                        "model": m_name,
                                        "context_window": ctx,
                                        "context_window_tokens": ctx_tokens,
                                        "max_output": max_out,
                                        "supported_thinking_levels": m_thinks,
                                    }
                                )
            except Exception:
                pass

        # 2. Fallback/supplement from ~/.omp/agent/models.yml
        for full_id, yml_info in yml_models.items():
            if full_id in seen_ids:
                continue
            seen_ids.add(full_id)
            provider, m_name = full_id.split("/", 1) if "/" in full_id else ("", full_id)
            ctx_tokens = yml_info.get("context_window_tokens")
            max_out_tokens = yml_info.get("max_output_tokens")
            models.append(
                {
                    "id": full_id,
                    "provider": provider,
                    "model": m_name,
                    "context_window": str(ctx_tokens) if ctx_tokens else "",
                    "context_window_tokens": ctx_tokens,
                    "max_output": str(max_out_tokens) if max_out_tokens else "",
                    "supported_thinking_levels": ["low", "medium", "high", "max"],
                }
            )

        if not models:
            models.append(
                {
                    "id": "clay/deepseek-flash",
                    "provider": "clay",
                    "model": "deepseek-flash",
                    "context_window": "1M",
                    "context_window_tokens": 1_048_576,
                    "max_output": "393K",
                    "supported_thinking_levels": ["low", "high", "max"],
                }
            )

        seen_levels = {
            lvl
            for m in models
            for lvl in (m.get("supported_thinking_levels") or [])
        }
        available_levels = [lvl for lvl in thinking_levels if lvl in seen_levels] or thinking_levels

        default_model = "clay/deepseek-flash"
        default_thinking = "high"
        cfg_yml = self.home / ".omp" / "agent" / "config.yml"
        if cfg_yml.exists():
            try:
                txt = cfg_yml.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"default:\s*([^\s:#]+)(?::([a-zA-Z]+))?", txt)
                if m:
                    default_model = m.group(1)
                    if m.group(2):
                        default_thinking = m.group(2)
            except Exception:
                pass

        return {
            "engine": "omp",
            "name": "Oh-My-Pi (OMP CLI)",
            "binary": str(omp_bin),
            "installed": omp_bin.exists(),
            "default_model": default_model,
            "default_thinking_level": default_thinking,
            "available_models": [m["id"] for m in models],
            "model_details": models,
            "available_thinking_levels": available_levels,
            "cli_flags_template": {
                "model": "--model={model}",
                "thinking_level": "--thinking={thinking_level}",
            },
        }

    def _discover_kimi(self) -> Dict[str, Any]:
        kimi_bin = Path(shutil.which("kimi") or (self.home / ".kimi-code" / "bin" / "kimi.exe"))
        cfg_path = self.home / ".kimi-code" / "config.toml"
        models: List[Dict[str, Any]] = []
        default_model = "kimi-code/k3.1-pro"
        all_efforts = ["low", "high", "max"]

        if cfg_path.exists():
            try:
                cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
                default_model = cfg.get("default_model") or default_model
                for m_id, m_info in (cfg.get("models") or {}).items():
                    if not isinstance(m_info, dict):
                        continue
                    effs = m_info.get("support_efforts") or ["low", "high", "max"]
                    models.append(
                        {
                            "id": m_id,
                            "display_name": m_info.get("display_name") or m_id,
                            "provider": m_info.get("provider"),
                            "max_context_size": m_info.get("max_context_size"),
                            "supported_thinking_levels": effs,
                            "default_thinking_level": m_info.get("default_effort") or "high",
                            "capabilities": m_info.get("capabilities") or [],
                        }
                    )
            except Exception:
                pass

        if not models:
            models = [
                {
                    "id": "kimi-code/k3.1-pro",
                    "display_name": "K3.1 Pro",
                    "supported_thinking_levels": ["low", "high", "max"],
                    "default_thinking_level": "high",
                }
            ]

        default_thinking = next(
            (m.get("default_thinking_level") for m in models if m.get("id") == default_model and m.get("default_thinking_level")),
            "high",
        )

        return {
            "engine": "kimi",
            "name": "Kimi Code CLI",
            "binary": str(kimi_bin),
            "installed": kimi_bin.exists(),
            "default_model": default_model,
            "default_thinking_level": default_thinking,
            "available_models": [m["id"] for m in models],
            "model_details": models,
            "available_thinking_levels": all_efforts,
            "cli_flags_template": {
                "model": "-m {model}",
                # V-4: the level really reaches Kimi through the child process environment only.
                "thinking_level": "环境变量 KIMI_MODEL_THINKING_EFFORT={thinking_level}（只作用于本会话）",
            },
        }

    def _discover_agy(self) -> Dict[str, Any]:
        agy_bin = Path(shutil.which("agy") or (self.home / "AppData" / "Local" / "agy" / "bin" / "agy.EXE"))
        models: List[Dict[str, Any]] = []
        thinking_levels = ["low", "medium", "high", "xhigh"]

        def _infer_agy_effort(model_id: str) -> Optional[str]:
            for lvl in ("xhigh", "high", "medium", "low"):
                if model_id.endswith(f"-{lvl}"):
                    return lvl
            return None

        if agy_bin.exists():
            try:
                r = subprocess.run(
                    [str(agy_bin), "models"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=7.0,
                )
                for raw_line in r.stdout.splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("Fetching"):
                        continue
                    parts = line.split("\t", 1)
                    m_id = parts[0].strip()
                    label = parts[1].strip() if len(parts) > 1 else m_id
                    inferred_effort = _infer_agy_effort(m_id)
                    models.append(
                        {
                            "id": m_id,
                            "display_name": label,
                            "built_in_thinking_level": inferred_effort,
                            "supported_thinking_levels": [inferred_effort] if inferred_effort else thinking_levels,
                        }
                    )
            except Exception:
                pass

        if not models:
            for m_id, label in [
                ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
                ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
                ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
                ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
                ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"),
                ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
                ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)"),
                ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium)"),
                ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)"),
                ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)"),
                ("claude-sonnet-5-5-low", "Claude Sonnet 5.5 (Low)"),
                ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)"),
                ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
                ("claude-opus-5-5-low", "Claude Opus 5.5 (Low)"),
                ("claude-opus-5-5-medium", "Claude Opus 5.5 (Medium)"),
                ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"),
                ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
            ]:
                inferred_effort = _infer_agy_effort(m_id)
                models.append(
                    {
                        "id": m_id,
                        "display_name": label,
                        "built_in_thinking_level": inferred_effort,
                        "supported_thinking_levels": [inferred_effort] if inferred_effort else thinking_levels,
                    }
                )

        return {
            "engine": "agy",
            "name": "Antigravity CLI (AGY)",
            "binary": str(agy_bin),
            "installed": agy_bin.exists(),
            "default_model": "gemini-3.8-flash",
            "default_thinking_level": "low",
            "available_models": [m["id"] for m in models],
            "model_details": models,
            "available_thinking_levels": thinking_levels,
            "cli_flags_template": {
                # V-4: agy has no thinking flag; the level is appended to the model name.
                "model": "--model {model}-{thinking_level}",
                "thinking_level": "模型名后缀 -{thinking_level}",
            },
        }

    def _discover_codex(self) -> Dict[str, Any]:
        codex_cmd = Path(
            shutil.which("codex.cmd")
            or shutil.which("codex")
            or (self.home / "AppData" / "Roaming" / "npm" / "codex.cmd")
        )
        cfg_path = self.home / ".codex" / "config.toml"
        global_state_path = self.home / ".codex" / ".codex-global-state.json"
        default_model = "gpt-6.1-sol"
        default_provider = "clay"
        default_effort = "medium"
        thinking_levels: List[str] = ["low", "medium", "high", "xhigh", "max", "ultra"]
        discovered_models: List[str] = []
        models: List[Dict[str, Any]] = []
        codex_toml_profiles: Dict[str, Any] = {}
        recent_configs: List[Dict[str, Any]] = []
        exe_candidates: List[Path] = []

        if cfg_path.exists():
            try:
                cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
                default_model = cfg.get("model") or default_model
                default_provider = cfg.get("model_provider") or default_provider
                default_effort = cfg.get("model_reasoning_effort") or default_effort
                if isinstance(cfg.get("profiles"), dict):
                    codex_toml_profiles = cfg["profiles"]
                # Check if CODEX_CLI_PATH is specified in mcp_servers.node_repl.env
                cli_env_path = (
                    cfg.get("mcp_servers", {})
                    .get("node_repl", {})
                    .get("env", {})
                    .get("CODEX_CLI_PATH")
                )
                if cli_env_path:
                    exe_candidates.append(Path(cli_env_path))
            except Exception:
                pass

        # Locate codex.exe binary to extract embedded bundled models.json
        local_codex_bin_dir = self.home / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
        if local_codex_bin_dir.exists():
            exe_candidates.extend(sorted(local_codex_bin_dir.rglob("codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True))
        npm_codex_dir = self.home / "AppData" / "Roaming" / "npm" / "node_modules" / "@openai" / "codex"
        if npm_codex_dir.exists():
            exe_candidates.extend(npm_codex_dir.rglob("codex.exe"))

        embedded_catalog_loaded = False
        for exe_path in exe_candidates:
            if not exe_path.exists():
                continue
            try:
                raw_bytes = exe_path.read_bytes()
                marker = raw_bytes.find(b'"slug": "')
                if marker == -1:
                    marker = raw_bytes.find(b'"slug":"')
                if marker != -1:
                    models_key_pos = raw_bytes.rfind(b'"models": [', 0, marker)
                    if models_key_pos != -1:
                        brace_pos = raw_bytes.rfind(b"{", 0, models_key_pos)
                        if brace_pos != -1:
                            chunk_str = raw_bytes[brace_pos : brace_pos + 850_000].decode("utf-8", errors="ignore")
                            catalog_obj, _ = json.JSONDecoder().raw_decode(chunk_str)
                            all_efforts_seen: List[str] = []
                            for m_entry in catalog_obj.get("models", []):
                                slug = m_entry.get("slug")
                                if not slug:
                                    continue
                                eff_list = [
                                    r.get("effort")
                                    for r in (m_entry.get("supported_reasoning_levels") or [])
                                    if isinstance(r, dict) and r.get("effort")
                                ]
                                for ef in eff_list:
                                    if ef not in all_efforts_seen:
                                        all_efforts_seen.append(ef)
                                vis = m_entry.get("visibility", "list")
                                if slug not in discovered_models:
                                    discovered_models.append(slug)
                                ctx_win = parse_token_count(m_entry.get("context_window"))
                                auto_compact_lim = parse_token_count(m_entry.get("auto_compact_token_limit"))
                                eff_pct_raw = m_entry.get("effective_context_window_percent")
                                eff_pct = (
                                    int(round(eff_pct_raw))
                                    if isinstance(eff_pct_raw, (int, float)) and not isinstance(eff_pct_raw, bool) and eff_pct_raw > 0
                                    else None
                                )
                                models.append({
                                    "id": slug,
                                    "display_name": m_entry.get("display_name") or slug,
                                    "visibility": vis,
                                    "default_thinking_level": m_entry.get("default_reasoning_level") or default_effort,
                                    "supported_thinking_levels": eff_list or thinking_levels,
                                    "provider": default_provider,
                                    "context_window_tokens": ctx_win,
                                    "auto_compact_token_limit": auto_compact_lim,
                                    "effective_context_window_percent": eff_pct,
                                })
                            if all_efforts_seen:
                                thinking_levels = all_efforts_seen
                            embedded_catalog_loaded = True
                            break
            except Exception:
                pass

        # Also read ~/.codex/.codex-global-state.json for recent model configurations
        if global_state_path.exists():
            try:
                gstate = json.loads(global_state_path.read_text(encoding="utf-8", errors="replace"))
                atoms = gstate.get("electron-persisted-atom-state", {})
                recent_list = atoms.get("composer-recent-model-configurations-v1") or []
                for rc in recent_list:
                    if isinstance(rc, dict) and rc.get("model"):
                        m_slug = rc["model"]
                        rc_eff = rc.get("reasoningEffort") or default_effort
                        recent_configs.append({"model": m_slug, "thinking_level": rc_eff})
                        if m_slug not in discovered_models:
                            discovered_models.insert(0, m_slug)
                            models.insert(0, {
                                "id": m_slug,
                                "display_name": m_slug,
                                "visibility": "list",
                                "default_thinking_level": rc_eff,
                                "supported_thinking_levels": thinking_levels,
                                "provider": default_provider,
                            })
            except Exception:
                pass

        if default_model and default_model not in discovered_models:
            discovered_models.insert(0, default_model)
            models.insert(0, {
                "id": default_model,
                "display_name": default_model,
                "visibility": "list",
                "default_thinking_level": default_effort,
                "supported_thinking_levels": thinking_levels,
                "provider": default_provider,
            })
        elif default_model in discovered_models:
            # Put user's default_model first in available_models
            discovered_models.remove(default_model)
            discovered_models.insert(0, default_model)

        return {
            "engine": "codex",
            "name": "OpenAI Codex CLI",
            "binary": str(codex_cmd if codex_cmd.exists() else (exe_candidates[0] if exe_candidates else codex_cmd)),
            "installed": codex_cmd.exists() or any(p.exists() for p in exe_candidates),
            "default_model": default_model,
            "default_provider": default_provider,
            "default_thinking_level": default_effort,
            "available_models": discovered_models,
            "model_details": models,
            "available_thinking_levels": thinking_levels,
            "recent_configurations": recent_configs,
            "config_profiles": codex_toml_profiles,
            "embedded_catalog_loaded": embedded_catalog_loaded,
            "cli_flags_template": {
                "model": "-m {model}",
                "thinking_level": '-c model_reasoning_effort="{thinking_level}"',
            },
        }

    @staticmethod
    def _canonicalize_claude_catalog_id(model_id: str) -> str:
        return re.sub(r"^(claude-[a-z0-9.\-]+)-\d{8}$", r"\1", (model_id or "").strip())

    @staticmethod
    def _format_claude_display_name(raw_name: Optional[str], model_id: str) -> str:
        name = (raw_name or "").strip()
        if name:
            if name.startswith(("Fable ", "Opus ", "Sonnet ", "Haiku ")):
                return f"Claude {name}"
            return name
        return model_id

    def _resolve_reclaude_config_dir(self) -> Optional[Path]:
        current_file = self.home / ".rec" / "claude" / "current"
        try:
            if not current_file.is_file():
                return None
            acct = current_file.read_text(encoding="utf-8", errors="replace").strip()
            if not acct or "/" in acct or "\\" in acct or acct in (".", ".."):
                return None
            cand = self.home / ".rec" / "claude" / "accounts" / acct / "config"
            if cand.is_dir():
                return cand
        except OSError:
            pass
        return None

    def _read_claude_cached_catalog(self, claude_cfg_dir: Path) -> Optional[Dict[str, Any]]:
        catalog_dir = claude_cfg_dir / "cache" / "model-catalog"
        if not catalog_dir.is_dir():
            return None
        best_score = None
        best_doc: Optional[Dict[str, Any]] = None
        for json_path in catalog_dir.glob("*.json"):
            try:
                st = json_path.stat()
                doc = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(doc, dict):
                continue
            catalog = doc.get("catalog")
            if not isinstance(catalog, dict):
                continue
            config = catalog.get("config")
            if not isinstance(config, dict):
                continue
            if catalog.get("surface") != "cc" and config.get("id") != "cc":
                continue
            raw_models = config.get("models")
            if not isinstance(raw_models, list) or not raw_models:
                continue
            try:
                fetched_at = int(doc.get("fetchedAt") or 0)
            except (TypeError, ValueError):
                fetched_at = 0
            score = (fetched_at, st.st_mtime_ns)
            if best_score is None or score > best_score:
                best_score = score
                best_doc = doc
        if best_doc is None:
            return None

        catalog = best_doc["catalog"]
        config = catalog["config"]
        thinking_by_model: Dict[str, str] = {}
        state = catalog.get("state")
        if isinstance(state, dict) and isinstance(state.get("thinking_by_model"), list):
            for item in state["thinking_by_model"]:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or "").strip()
                effort = str(((item.get("thinking") or {}).get("effort")) or "").strip().lower()
                if item_id and effort:
                    thinking_by_model[item_id] = effort
                    thinking_by_model[self._canonicalize_claude_catalog_id(item_id)] = effort

        parsed_models: List[Dict[str, Any]] = []
        seen_ids = set()
        for entry in config["models"]:
            if not isinstance(entry, dict):
                continue
            if entry.get("disabled") is True or entry.get("section") == "deprecated":
                continue
            raw_id = str(entry.get("id") or "").strip()
            if not raw_id:
                continue
            cid = self._canonicalize_claude_catalog_id(raw_id)
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            display_name = self._format_claude_display_name(entry.get("name"), cid)
            section = "overflow" if entry.get("section") == "overflow" else "main"
            runtime = entry.get("runtime") if isinstance(entry.get("runtime"), dict) else {}
            thinking = entry.get("thinking") if isinstance(entry.get("thinking"), dict) else {}

            if isinstance(runtime.get("effort_levels"), list):
                levels: Optional[List[str]] = [
                    str(x).strip().lower() for x in runtime["effort_levels"] if str(x).strip()
                ]
            elif thinking.get("type") == "none":
                levels = []
            elif isinstance(thinking.get("effort_options"), list):
                levels = [
                    str(o.get("id")).strip().lower()
                    for o in thinking["effort_options"]
                    if isinstance(o, dict) and str(o.get("id") or "").strip()
                ]
            else:
                levels = None

            if levels == []:
                default_effort = None
            else:
                default_effort = str(runtime.get("default_effort") or "").strip().lower() or None
                if default_effort is None and levels:
                    cand_effort = thinking_by_model.get(raw_id) or thinking_by_model.get(cid)
                    if cand_effort and cand_effort in levels:
                        default_effort = cand_effort

            parsed_models.append(
                {
                    "id": cid,
                    "display_name": display_name,
                    "section": section,
                    "levels": levels,
                    "default_effort": default_effort,
                }
            )

        alias_targets: Dict[str, str] = {}
        pat = config.get("provider_alias_targets")
        if isinstance(pat, dict):
            for alias, target_info in pat.items():
                if isinstance(target_info, dict) and isinstance(target_info.get("default"), str):
                    target = target_info["default"].strip()
                    if target:
                        alias_targets[str(alias).strip().lower()] = self._canonicalize_claude_catalog_id(target)

        return {"models": parsed_models, "alias_targets": alias_targets} if parsed_models else None

    def _extract_claude_binary_catalog(self, claude_bin: Path) -> Optional[Dict[str, Any]]:
        script_suffixes = {".cmd", ".bat", ".ps1", ".sh"}
        seen_paths = set()

        def _parse_candidate(cand: Path) -> Optional[Dict[str, Any]]:
            if not cand or cand.suffix.lower() in script_suffixes:
                return None
            try:
                if not cand.is_file():
                    return None
                resolved = cand.resolve()
                norm = os.path.normcase(str(resolved))
                if norm in seen_paths:
                    return None
                seen_paths.add(norm)
                st = resolved.stat()
                cache_key = (norm, st.st_mtime_ns, st.st_size)
            except OSError:
                return None

            with self._claude_cache_lock:
                cached = self._claude_binary_cache.get(cache_key)
            if cached is None:
                parsed_models: List[Dict[str, Any]] = []
                seen_ids = set()
                alias_targets: Dict[str, str] = {}
                try:
                    raw_bytes = resolved.read_bytes()

                    # 1. Embedded surfaces.cc model-catalog seed (UE)
                    m_cc = raw_bytes.find(b'model_selector_config:[{id:"cc",models:[')
                    if m_cc != -1:
                        m_pat = raw_bytes.find(b"provider_alias_targets:", m_cc, m_cc + 40_000)
                        end_idx = (m_pat + 1500) if m_pat != -1 else (m_cc + 40_000)
                        cc_snip = raw_bytes[m_cc:end_idx].decode("utf-8", errors="replace")
                        matches = list(re.finditer(r'\{id:"(claude-[a-z0-9.\-]+)",name:"([^"]+)"', cc_snip))
                        pat_pos = cc_snip.find("provider_alias_targets:")
                        for idx, m in enumerate(matches):
                            start = m.start()
                            nxt = (
                                matches[idx + 1].start()
                                if idx + 1 < len(matches)
                                else (pat_pos if pat_pos > start else len(cc_snip))
                            )
                            entry = cc_snip[start:nxt]
                            if "disabled:!0" in entry or 'section:"deprecated"' in entry:
                                continue
                            cid = self._canonicalize_claude_catalog_id(m.group(1))
                            if cid in seen_ids:
                                continue
                            seen_ids.add(cid)
                            display_name = self._format_claude_display_name(m.group(2), cid)
                            section = "overflow" if 'section:"overflow"' in entry else "main"
                            eff_m = re.search(r"effort_levels:\[([^\]]*)\]", entry)
                            if eff_m:
                                levels: Optional[List[str]] = [
                                    x.strip().strip('"').strip("'").lower()
                                    for x in eff_m.group(1).split(",")
                                    if x.strip().strip('"').strip("'")
                                ]
                            elif 'thinking:{type:"none"}' in entry:
                                levels = []
                            else:
                                levels = None
                            def_m = re.search(r'default_effort:"([^"]+)"', entry)
                            default_effort = def_m.group(1).strip().lower() if def_m else None
                            if levels == []:
                                default_effort = None
                            parsed_models.append(
                                {
                                    "id": cid,
                                    "display_name": display_name,
                                    "section": section,
                                    "levels": levels,
                                    "default_effort": default_effort,
                                }
                            )
                        if pat_pos != -1:
                            pat_snip = cc_snip[pat_pos:]
                            for a_m in re.finditer(
                                r'\b(fable|opus|sonnet|haiku):\{default:"(claude-[a-z0-9.\-]+)"',
                                pat_snip,
                            ):
                                alias_targets[a_m.group(1).lower()] = self._canonicalize_claude_catalog_id(a_m.group(2))

                    # 2. Embedded canonical model constants (Em)
                    em_marker = raw_bytes.find(b'FABLE_ID:"')
                    if em_marker == -1:
                        em_marker = raw_bytes.find(b'OPUS_ID:"')
                    if em_marker != -1:
                        brace_start = raw_bytes.rfind(b"{", max(0, em_marker - 2048), em_marker + 10)
                        brace_end = raw_bytes.find(b"}", em_marker, em_marker + 2048)
                        if brace_start != -1 and brace_end != -1:
                            em_snip = raw_bytes[brace_start : brace_end + 1].decode("ascii", errors="ignore")
                            pairs = re.findall(
                                r'([A-Z0-9_]+)_ID:"(claude-(?:fable|opus|sonnet|haiku)-[a-z0-9.\-]+)",\1_NAME:"([^"]+)"',
                                em_snip,
                            )
                            for prefix, raw_id, raw_label in pairs:
                                cid = self._canonicalize_claude_catalog_id(raw_id)
                                if cid in seen_ids:
                                    continue
                                seen_ids.add(cid)
                                parsed_models.append(
                                    {
                                        "id": cid,
                                        "display_name": self._format_claude_display_name(raw_label, cid),
                                        "section": "overflow" if prefix.startswith("PREV_") else "main",
                                        "levels": None,
                                        "default_effort": None,
                                    }
                                )
                except Exception:
                    pass

                cached = {"models": parsed_models, "alias_targets": alias_targets}
                with self._claude_cache_lock:
                    self._claude_binary_cache[cache_key] = cached

            return cached if cached.get("models") else None

        hit = _parse_candidate(claude_bin)
        if hit:
            return hit

        fallback_candidates: List[Path] = [self.home / ".local" / "bin" / "claude.exe"]
        versions_dir = self.home / ".local" / "share" / "claude" / "versions"
        if versions_dir.is_dir():
            try:
                fallback_candidates.extend(
                    sorted(
                        (p for p in versions_dir.iterdir() if p.is_file()),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                )
            except OSError:
                pass
        for cand in fallback_candidates:
            hit = _parse_candidate(cand)
            if hit:
                return hit

        for which_name in ("claude.exe", "claude"):
            found = shutil.which(which_name)
            if found:
                hit = _parse_candidate(Path(found))
                if hit:
                    return hit
        return None

    def _discover_claude(
        self,
        binary_override: Optional[str] = None,
        config_dir_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        raw_bin = (binary_override or os.environ.get("CLAUDE_BIN") or "").strip()
        fallback_exe = self.home / ".local" / "bin" / "claude.exe"
        fallback_cmd = self.home / "AppData" / "Roaming" / "npm" / "claude.cmd"
        fallback_reclaude = self.home / "AppData" / "Local" / "Programs" / "reclaude" / "bin" / "reclaude.exe"
        if raw_bin:
            expanded_bin = os.path.expandvars(os.path.expanduser(raw_bin))
            candidate_bin = Path(expanded_bin)
            if candidate_bin.exists():
                claude_bin = candidate_bin
            else:
                which_bin = shutil.which(expanded_bin)
                claude_bin = Path(which_bin) if which_bin else candidate_bin
        else:
            claude_bin_str = (
                shutil.which("claude.exe")
                or shutil.which("claude")
                or shutil.which("claude.cmd")
            )
            if claude_bin_str:
                claude_bin = Path(claude_bin_str)
            elif fallback_exe.exists():
                claude_bin = fallback_exe
            elif fallback_cmd.exists():
                claude_bin = fallback_cmd
            else:
                reclaude_str = shutil.which("reclaude.exe") or shutil.which("reclaude")
                if reclaude_str:
                    claude_bin = Path(reclaude_str)
                elif fallback_reclaude.exists():
                    claude_bin = fallback_reclaude
                else:
                    claude_bin = fallback_exe

        raw_cfg_dir = (config_dir_override or os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
        if raw_cfg_dir:
            claude_cfg_dir = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(raw_cfg_dir))))
        elif claude_bin.stem.lower() == "reclaude":
            claude_cfg_dir = self._resolve_reclaude_config_dir() or (self.home / ".claude")
        else:
            claude_cfg_dir = self.home / ".claude"

        thinking_levels: List[str] = ["low", "medium", "high", "xhigh", "max"]
        default_model = "sonnet"
        default_effort = "high"
        global_effort_override: Optional[str] = None
        model_effort_overrides: Dict[str, str] = {}
        explicit_env_models: List[tuple] = []
        avail_cfg_models: List[str] = []

        settings_path = claude_cfg_dir / "settings.json"
        if settings_path.exists():
            try:
                cfg = json.loads(settings_path.read_text(encoding="utf-8", errors="replace"))
                env_cfg = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
                cfg_model = (
                    cfg.get("model")
                    or env_cfg.get("ANTHROPIC_MODEL")
                    or env_cfg.get("ANTHROPIC_DEFAULT_MODEL")
                    or env_cfg.get("CLAUDE_MODEL")
                )
                if isinstance(cfg_model, str) and cfg_model.strip():
                    default_model = cfg_model.strip()
                cfg_effort = (
                    cfg.get("effortLevel")
                    or env_cfg.get("CLAUDE_CODE_EFFORT_LEVEL")
                )
                if isinstance(cfg_effort, str) and cfg_effort.strip().lower() in thinking_levels:
                    global_effort_override = cfg_effort.strip().lower()
                    default_effort = global_effort_override

                raw_model_settings = cfg.get("modelSettings")
                if isinstance(raw_model_settings, dict):
                    for m_key, m_cfg in raw_model_settings.items():
                        if not isinstance(m_key, str) or not m_key.strip() or not isinstance(m_cfg, dict):
                            continue
                        raw_lvl = m_cfg.get("effortLevel")
                        if isinstance(raw_lvl, str) and raw_lvl.strip().lower() in thinking_levels:
                            lvl_clean = raw_lvl.strip().lower()
                            model_effort_overrides[m_key.strip()] = lvl_clean
                            model_effort_overrides[self._canonicalize_claude_catalog_id(m_key)] = lvl_clean

                for env_key, name_key in (
                    ("ANTHROPIC_MODEL", None),
                    ("ANTHROPIC_DEFAULT_MODEL", None),
                    ("ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME"),
                    ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"),
                    ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"),
                    ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME"),
                    ("ANTHROPIC_CUSTOM_MODEL_OPTION", "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"),
                ):
                    val = env_cfg.get(env_key)
                    if isinstance(val, str) and val.strip():
                        name_val = env_cfg.get(name_key) if name_key else None
                        label = name_val.strip() if isinstance(name_val, str) and name_val.strip() else None
                        explicit_env_models.append((val.strip(), label, env_key))

                avail = cfg.get("availableModels")
                if isinstance(avail, list):
                    for item in avail:
                        if isinstance(item, str) and item.strip():
                            avail_cfg_models.append(item.strip())
            except Exception:
                pass

        cached_cat = self._read_claude_cached_catalog(claude_cfg_dir)
        bin_cat = self._extract_claude_binary_catalog(claude_bin)

        catalog_entries: List[Dict[str, Any]] = []
        catalog_by_id: Dict[str, Dict[str, Any]] = {}
        if cached_cat:
            for entry in cached_cat["models"]:
                catalog_entries.append(entry)
                catalog_by_id[entry["id"]] = entry
            if bin_cat:
                for entry in bin_cat["models"]:
                    if entry.get("section") == "main" and entry["id"] not in catalog_by_id:
                        catalog_entries.append(entry)
                        catalog_by_id[entry["id"]] = entry
        elif bin_cat:
            for entry in bin_cat["models"]:
                catalog_entries.append(entry)
                catalog_by_id[entry["id"]] = entry

        alias_targets: Dict[str, str] = {
            "fable": "claude-fable-5-2",
            "opus": "claude-opus-5-5",
            "sonnet": "claude-sonnet-5-5",
            "haiku": "claude-haiku-5-5",
        }
        if bin_cat and bin_cat.get("alias_targets"):
            alias_targets.update(bin_cat["alias_targets"])
        if cached_cat and cached_cat.get("alias_targets"):
            alias_targets.update(cached_cat["alias_targets"])
        for fam, env_key in (
            ("fable", "ANTHROPIC_DEFAULT_FABLE_MODEL"),
            ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
            ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
            ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
        ):
            for _val, _lbl, ek in explicit_env_models:
                if ek == env_key:
                    alias_targets[fam] = _val

        alias_family_map = {
            "fable": "fable",
            "fable[1m]": "fable",
            "opus": "opus",
            "opus[1m]": "opus",
            "opusplan": "opus",
            "sonnet": "sonnet",
            "sonnet[1m]": "sonnet",
            "haiku": "haiku",
        }

        fallback_levels_by_id: Dict[str, List[str]] = {
            "claude-opus-4-6": ["low", "medium", "high", "max"],
            "claude-sonnet-4-6": ["low", "medium", "high", "max"],
            "claude-haiku-4-5": [],
        }

        discovered_models: List[str] = []
        models: List[Dict[str, Any]] = []

        def _resolve_model_thinking(m_id: str) -> tuple:
            target_id = alias_targets.get(alias_family_map.get(m_id.lower(), ""))
            cat = catalog_by_id.get(m_id) or (catalog_by_id.get(target_id) if target_id else None)
            if cat and isinstance(cat.get("levels"), list):
                supported = list(cat["levels"])
            elif m_id in fallback_levels_by_id:
                supported = list(fallback_levels_by_id[m_id])
            else:
                supported = list(thinking_levels)

            if not supported:
                return [], None

            override_lvl = model_effort_overrides.get(m_id) or (
                model_effort_overrides.get(target_id) if target_id else None
            )
            cat_def = cat.get("default_effort") if cat else None
            chosen_default = override_lvl or global_effort_override or cat_def or default_effort
            if chosen_default not in supported:
                chosen_default = supported[len(supported) // 2]
            return supported, chosen_default

        def _add_model(m_id: str, display_name: Optional[str] = None) -> None:
            m_id = (m_id or "").strip()
            if not m_id or m_id in discovered_models:
                return
            supported, model_def = _resolve_model_thinking(m_id)
            discovered_models.append(m_id)
            models.append(
                {
                    "id": m_id,
                    "display_name": display_name or m_id,
                    "default_thinking_level": model_def,
                    "supported_thinking_levels": supported,
                }
            )

        # 1. Explicit env models and availableModels from settings.json
        for m_val, m_lbl, _env_key in explicit_env_models:
            _add_model(m_val, m_lbl)
        for m_val in avail_cfg_models:
            _add_model(m_val)

        # 2. Discovered main-section catalog models
        for entry in catalog_entries:
            if entry.get("section") == "main":
                _add_model(entry["id"], entry.get("display_name"))

        # 3. Standard aliases
        for m_id, label in [
            ("fable", "Claude Fable (Latest Alias)"),
            ("opus", "Claude Opus (Latest Alias)"),
            ("sonnet", "Claude Sonnet (Latest Alias)"),
            ("haiku", "Claude Haiku (Latest Alias)"),
            ("opusplan", "Claude Opus Plan + Sonnet Execution"),
            ("fable[1m]", "Claude Fable (1M Context)"),
            ("opus[1m]", "Claude Opus (1M Context)"),
            ("sonnet[1m]", "Claude Sonnet (1M Context)"),
        ]:
            _add_model(m_id, label)

        # 4. Discovered overflow-section catalog models
        for entry in catalog_entries:
            if entry.get("section") == "overflow":
                _add_model(entry["id"], entry.get("display_name"))

        # 5. Static baseline fallback models
        for m_id, label in [
            ("claude-fable-5-2", "Claude Fable 5.2"),
            ("claude-opus-5-5", "Claude Opus 5.5"),
            ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
            ("claude-haiku-5-5", "Claude Haiku 5.5"),
            ("claude-fable-5-1", "Claude Fable 5.1"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            ("claude-opus-4-6", "Claude Opus 4.6"),
            ("claude-haiku-4-5", "Claude Haiku 4.5"),
        ]:
            _add_model(m_id, label)

        # 6. Any modelSettings keys not yet in discovered_models
        for m_key in model_effort_overrides:
            if self._canonicalize_claude_catalog_id(m_key) not in discovered_models:
                _add_model(m_key)

        # 7. Ensure default_model is first in both discovered_models and models
        if default_model and default_model not in discovered_models:
            supported, model_def = _resolve_model_thinking(default_model)
            discovered_models.insert(0, default_model)
            models.insert(
                0,
                {
                    "id": default_model,
                    "display_name": default_model,
                    "default_thinking_level": model_def,
                    "supported_thinking_levels": supported,
                },
            )
        elif default_model in discovered_models:
            idx = discovered_models.index(default_model)
            if idx > 0:
                discovered_models.pop(idx)
                discovered_models.insert(0, default_model)
                m_entry = models.pop(idx)
                models.insert(0, m_entry)

        engine_default_thinking = (
            (models[0].get("default_thinking_level") if models else None)
            or global_effort_override
            or default_effort
        )

        return {
            "engine": "claude",
            "name": "Claude Code CLI",
            "binary": str(claude_bin),
            "config_dir": str(claude_cfg_dir),
            "installed": claude_bin.exists(),
            "default_model": default_model,
            "default_thinking_level": engine_default_thinking,
            "available_models": discovered_models,
            "model_details": models,
            "available_thinking_levels": thinking_levels,
            "cli_flags_template": {
                "model": "--model {model}",
                "thinking_level": "--effort {thinking_level}",
            },
        }

    def _discover_dsh(self) -> Dict[str, Any]:
        dsh_cfg_path = self.home / ".config" / "dsh-crew" / "config.json"
        thinking_levels = ["high", "max"]
        default_model = "clay/deepseek-flash"
        default_effort = "high"
        providers_list: List[Dict[str, Any]] = []
        bindings: Dict[str, Any] = {}

        if dsh_cfg_path.exists():
            try:
                cfg = json.loads(dsh_cfg_path.read_text(encoding="utf-8", errors="replace"))
                fp = cfg.get("flash_provider", "clay")
                fm = cfg.get("flash_model", "deepseek-flash")
                default_model = f"{fp}/{fm}"
                default_effort = cfg.get("default_effort", "high")
            except Exception:
                pass

        try:
            r = httpx.get(dsh_url("/_dsh/dsh-crew/providers", dsh_cfg_path), timeout=2.0)
            if r.is_success:
                data = r.json()
                providers_list = data.get("providers") or []
                bindings = data.get("bindings") or {}
        except Exception:
            pass

        available_models = [
            "clay/deepseek-flash",
            "deepseek-official/deepseek-v4-flash",
            "deepseek-official/deepseek-v4-pro",
            "clay/minimax-m3.1-flash",
        ]
        for _tier_name, b in bindings.items():
            if isinstance(b, dict) and b.get("provider") and b.get("model"):
                cand = f"{b['provider']}/{b['model']}"
                if cand not in available_models:
                    available_models.insert(0, cand)
        if default_model and default_model not in available_models:
            available_models.insert(0, default_model)
        elif default_model in available_models:
            available_models.remove(default_model)
            available_models.insert(0, default_model)

        models = [
            {
                "id": m_id,
                "provider": m_id.split("/")[0] if "/" in m_id else "clay",
                "model": m_id.split("/", 1)[1] if "/" in m_id else m_id,
                "supported_thinking_levels": thinking_levels,
            }
            for m_id in available_models
        ]

        return {
            "engine": "dsh",
            "name": "DeepSeek Harness (In-Host Web / Crew)",
            "binary": dsh_url("/_dsh/dsh-crew/jobs", dsh_cfg_path),
            "installed": True,
            "default_model": default_model,
            "default_thinking_level": default_effort,
            "available_models": available_models,
            "model_details": models,
            "available_thinking_levels": thinking_levels,
            "providers": providers_list,
            "cli_flags_template": {
                "model": "JSON payload {provider, model}",
                "thinking_level": "JSON payload {effort: thinking_level}",
            },
        }

