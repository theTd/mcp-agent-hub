import json
import os
import re
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


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
                "dsh": self._discover_dsh(),
            }
            self._cache = {
                "discovered_at": round(now, 3),
                "engines": engines,
            }
            self._cache_at = now
            return self._cache

    def _discover_omp(self) -> Dict[str, Any]:
        omp_bin = Path(r"C:\Users\cui\AppData\Local\omp\omp.exe")
        thinking_levels = ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"]
        models: List[Dict[str, Any]] = []
        seen_ids = set()

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
                                models.append(
                                    {
                                        "id": full_id,
                                        "provider": current_provider,
                                        "model": m_name,
                                        "context_window": ctx,
                                        "max_output": max_out,
                                        "supported_thinking_levels": m_thinks,
                                        "omp_cli_thinking_levels": thinking_levels,
                                    }
                                )
            except Exception:
                pass

        # 2. Fallback/supplement from ~/.omp/agent/models.yml
        if not models:
            models.append(
                {
                    "id": "clay/deepseek-flash",
                    "provider": "clay",
                    "model": "deepseek-flash",
                    "context_window": "1M",
                    "max_output": "393K",
                    "supported_thinking_levels": ["low", "high", "max"],
                    "omp_cli_thinking_levels": thinking_levels,
                }
            )

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
            "available_thinking_levels": thinking_levels,
            "cli_flags_template": {
                "model": "--model={model}",
                "thinking_level": "--thinking={thinking_level}",
            },
        }

    def _discover_kimi(self) -> Dict[str, Any]:
        kimi_bin = self.home / ".kimi-code" / "bin" / "kimi.exe"
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
                    "default_thinking_level": "low",
                }
            ]

        return {
            "engine": "kimi",
            "name": "Kimi Code CLI",
            "binary": str(kimi_bin),
            "installed": kimi_bin.exists(),
            "default_model": default_model,
            "default_thinking_level": "low",
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
        agy_bin = Path(r"C:\Users\cui\AppData\Local\agy\bin\agy.EXE")
        models: List[Dict[str, Any]] = []
        thinking_levels = ["low", "medium", "high"]

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
                    inferred_effort = None
                    for lvl in ("low", "medium", "high"):
                        if m_id.endswith(f"-{lvl}"):
                            inferred_effort = lvl
                            break
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
                ("claude-sonnet-5-5-low", "Claude Sonnet 5.5 (Low)"),
                ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)"),
                ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
                ("claude-opus-5-5-low", "Claude Opus 5.5 (Low)"),
                ("claude-opus-5-5-medium", "Claude Opus 5.5 (Medium)"),
                ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"),
                ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
            ]:
                models.append({"id": m_id, "display_name": label, "supported_thinking_levels": thinking_levels})

        return {
            "engine": "agy",
            "name": "Antigravity CLI (AGY)",
            "binary": str(agy_bin),
            "installed": agy_bin.exists(),
            "default_model": "gemini-3.8-flash-low",
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
        codex_cmd = Path(r"C:\Users\cui\AppData\Roaming\npm\codex.cmd")
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
                                models.append({
                                    "id": slug,
                                    "display_name": m_entry.get("display_name") or slug,
                                    "visibility": vis,
                                    "default_thinking_level": m_entry.get("default_reasoning_level") or default_effort,
                                    "supported_thinking_levels": eff_list or thinking_levels,
                                    "provider": default_provider,
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

    def _discover_dsh(self) -> Dict[str, Any]:
        dsh_cfg_path = self.home / ".config" / "dsh-crew" / "config.json"
        thinking_levels = ["off", "low", "medium", "high", "max"]
        tiers = ["flash", "pro"]
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
            r = httpx.get("http://127.0.0.1:3080/_dsh/dsh-crew/providers", timeout=2.0)
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
        for tier_name, b in bindings.items():
            if isinstance(b, dict) and b.get("provider") and b.get("model"):
                cand = f"{b['provider']}/{b['model']}"
                if cand not in available_models:
                    available_models.insert(0, cand)

        models = [
            {
                "id": m_id,
                "provider": m_id.split("/")[0] if "/" in m_id else "clay",
                "model": m_id.split("/", 1)[1] if "/" in m_id else m_id,
                "supported_thinking_levels": thinking_levels,
                "supported_tiers": tiers,
            }
            for m_id in available_models
        ]

        return {
            "engine": "dsh",
            "name": "DeepSeek Harness (In-Host Web / Crew)",
            "binary": "http://127.0.0.1:3080/_dsh/dsh-crew/jobs",
            "installed": True,
            "default_model": default_model,
            "default_thinking_level": default_effort,
            "available_models": available_models,
            "model_details": models,
            "available_thinking_levels": thinking_levels,
            "available_tiers": tiers,
            "providers": providers_list,
            "tier_bindings": bindings,
            "cli_flags_template": {
                "model": "JSON payload {provider, model}",
                "thinking_level": "JSON payload {effort: thinking_level}",
            },
        }

