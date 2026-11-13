"""Accurate per-engine context window and auto-compaction usage tracking.

Supported CLI engines: codex, kimi, omp, claude, agy.
Excluded / non-LLM engines (dsh, pwsh, desktop_gui, generic) return None.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from compaction_threshold import CLAUDE_PCT_ENV, CODEX_TOKEN_LIMIT_KEY, PERCENT, TOKENS, normalize_compaction_threshold
from model_discovery import parse_omp_models_yml, parse_token_count

SUPPORTED_CONTEXT_ENGINES = frozenset({"codex", "kimi", "omp", "claude", "agy"})

AGY_ESTIMATED_WINDOW_TOKENS = 262_144
AGY_ESTIMATED_NOTE = "AGY 未公开物理窗口上限，圆环按实测自动压缩线（256k / 262,144）估算"
_AGY_CHECKPOINT_PROMPT_CAP = 32_000
_AGY_CHECKPOINT_PROMPT_FALLBACK = 18_000

_CLAUDE_DEFAULT_WINDOW = 200_000
_CLAUDE_1M_WINDOW = 1_000_000
_CLAUDE_RESERVE_TOKENS = 20_000
_CLAUDE_COMPACT_BUFFER = 13_000

_KIMI_DEFAULT_MAX_RATIO = 0.85
_KIMI_DEFAULT_RESERVED = 50_000

_OMP_DEFAULT_THRESHOLD_RATIO = 0.85
_OMP_DEFAULT_RESERVE_TOKENS = 16_384

_CODEX_COMPACT_ARG_RE = re.compile(r"model_auto_compact_token_limit\s*=\s*([0-9_]+)", re.IGNORECASE)


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        iv = int(round(value))
        return iv if iv >= 0 else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("_", "")
        if text.isdigit():
            return int(text)
    return None


def _pos_int_or_none(value: Any) -> Optional[int]:
    val = _int_or_none(value)
    return val if val is not None and val > 0 else None


def _int0(value: Any) -> int:
    val = _int_or_none(value)
    return val if val is not None else 0


def _float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _extract_cli_flag_value(args: Iterable[str], flags: tuple[str, ...]) -> Optional[str]:
    arg_list = [str(a) for a in args]
    for i, arg in enumerate(arg_list):
        if arg in flags and i + 1 < len(arg_list):
            val = arg_list[i + 1].strip()
            if val:
                return val
        for flag in flags:
            prefix = f"{flag}="
            if arg.startswith(prefix):
                val = arg[len(prefix):].strip()
                if val:
                    return val
    return None


def _extract_codex_compact_override(args: Optional[Iterable[str]]) -> Optional[int]:
    if not args:
        return None
    for raw in args:
        m = _CODEX_COMPACT_ARG_RE.search(str(raw))
        if m:
            val = _pos_int_or_none(m.group(1))
            if val is not None:
                return val
    return None


def _parse_omp_config_yml(path: Path) -> Dict[str, Any]:
    """Extracts default model and global compaction block from ~/.omp/agent/config.yml."""
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}

    info: Dict[str, Any] = {"compaction": {}}
    in_compaction = False
    compaction_indent: Optional[int] = None
    for raw in lines:
        stripped_comment = raw.split("#", 1)[0].rstrip()
        if not stripped_comment.strip():
            continue
        indent = len(stripped_comment) - len(stripped_comment.lstrip(" "))
        stripped = stripped_comment.lstrip(" ")

        if indent == 0:
            m_def = re.match(r"^default:\s*([^\s:#]+)", stripped)
            if m_def and "default_model" not in info:
                val = m_def.group(1).strip().strip("'\"")
                if ":" in val and "/" in val:
                    # strip trailing :thinking_level if any (provider/model:level)
                    val = val.rsplit(":", 1)[0]
                info["default_model"] = val

        if not in_compaction:
            if stripped == "compaction:":
                in_compaction = True
                compaction_indent = indent
            continue

        if compaction_indent is not None and indent <= compaction_indent:
            in_compaction = False
            compaction_indent = None
            continue

        if ":" in stripped and (compaction_indent is None or indent <= compaction_indent + 4):
            k, v = stripped.split(":", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k == "enabled":
                info["compaction"]["enabled"] = v.lower() not in ("false", "0", "no", "off")
            elif k == "thresholdPercent":
                num = _int_or_none(v)
                if num is not None:
                    info["compaction"]["thresholdPercent"] = num
            elif k == "thresholdTokens":
                num = parse_token_count(v) if v != "-1" else -1
                if num is not None:
                    info["compaction"]["thresholdTokens"] = num
            elif k == "reserveTokens":
                num = parse_token_count(v)
                if num is not None:
                    info["compaction"]["reserveTokens"] = num
    return info


def _parse_overlay_compaction(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None or not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("{"):
                data = json.loads(s)
                if isinstance(data, dict) and isinstance(data.get("compaction"), dict):
                    return data["compaction"]
    except Exception:
        return None
    return None


def context_window_signature(cw: Optional[Mapping[str, Any]]) -> str:
    """Stable change-detection signature used by transcript_version()."""
    if not isinstance(cw, Mapping):
        return ""
    return "|".join(
        (
            str(cw.get("engine") or ""),
            str(cw.get("model") or ""),
            str(cw.get("used_tokens") if cw.get("used_tokens") is not None else ""),
            str(cw.get("window_tokens") if cw.get("window_tokens") is not None else ""),
            str(cw.get("compact_tokens") if cw.get("compact_tokens") is not None else ""),
            str(cw.get("used_pct") if cw.get("used_pct") is not None else ""),
            str(cw.get("compact_pct") if cw.get("compact_pct") is not None else ""),
            "1" if cw.get("window_estimated") else "0",
            "1" if cw.get("awaiting_post_compact") else "0",
            "1" if cw.get("compacted") else "0",
            str(cw.get("source") or ""),
        )
    )


class ContextWindowCatalog:
    """Fast, event-loop-safe resolver for per-engine window sizes & auto-compaction thresholds.

    Uses discovery.peek_cache() and tiny local config files with short TTL caching; never
    spawns CLI subprocesses.
    """

    def __init__(self, home: Optional[Path] = None, discovery: Any = None, cache_ttl: float = 10.0):
        self.home = Path(home) if home is not None else Path(os.path.expanduser("~"))
        self.discovery = discovery
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._file_cache: Dict[str, tuple[float, Any]] = {}

    def _cached(self, key: str, loader: Any) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._file_cache.get(key)
            if hit is not None and (now - hit[0]) < self._cache_ttl:
                return hit[1]
        value = loader()
        with self._lock:
            self._file_cache[key] = (time.monotonic(), value)
        return value

    def _peek_engine_discovery(self, engine: str) -> Dict[str, Any]:
        if self.discovery is None:
            return {}
        try:
            cache = self.discovery.peek_cache()
        except Exception:
            return {}
        if not isinstance(cache, Mapping):
            return {}
        engines = cache.get("engines")
        if not isinstance(engines, Mapping):
            return {}
        eng = engines.get(engine)
        return dict(eng) if isinstance(eng, Mapping) else {}

    def _load_codex_config(self) -> Dict[str, Any]:
        def load() -> Dict[str, Any]:
            cfg_path = self.home / ".codex" / "config.toml"
            if not cfg_path.exists():
                return {}
            try:
                return tomllib.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                return {}
        return self._cached("codex_toml", load)

    def _load_kimi_config(self) -> Dict[str, Any]:
        def load() -> Dict[str, Any]:
            cfg_path = self.home / ".kimi-code" / "config.toml"
            if not cfg_path.exists():
                return {}
            try:
                return tomllib.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                return {}
        return self._cached("kimi_toml", load)

    def _load_omp_files(self) -> Dict[str, Any]:
        def load() -> Dict[str, Any]:
            yml_models = parse_omp_models_yml(self.home / ".omp" / "agent" / "models.yml")
            cfg = _parse_omp_config_yml(self.home / ".omp" / "agent" / "config.yml")
            return {"models": yml_models, "config": cfg}
        return self._cached("omp_files", load)

    def _load_claude_settings(self) -> Dict[str, Any]:
        def load() -> Dict[str, Any]:
            p = self.home / ".claude" / "settings.json"
            if not p.exists():
                return {}
            try:
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return self._cached("claude_settings", load)

    def resolve(
        self,
        engine: str,
        model: Optional[str] = None,
        *,
        session_compaction_threshold: Optional[Mapping[str, Any]] = None,
        extra_env: Optional[Mapping[str, str]] = None,
        cli_args: Optional[Iterable[str]] = None,
        omp_overlay_path: Optional[Path] = None,
        runtime_window_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        eng = str(engine or "").strip().lower()
        if eng not in SUPPORTED_CONTEXT_ENGINES:
            return {
                "model": str(model or ""),
                "window_tokens": None,
                "compact_tokens": None,
                "window_estimated": False,
                "estimated_note": "",
            }

        if eng == "agy":
            return {
                "model": str(model or ""),
                "window_tokens": AGY_ESTIMATED_WINDOW_TOKENS,
                "compact_tokens": AGY_ESTIMATED_WINDOW_TOKENS,
                "window_estimated": True,
                "estimated_note": AGY_ESTIMATED_NOTE,
            }

        if eng == "codex":
            return self._resolve_codex(
                model=model,
                session_compaction_threshold=session_compaction_threshold,
                cli_args=cli_args,
                runtime_window_tokens=runtime_window_tokens,
            )

        if eng == "kimi":
            return self._resolve_kimi(model=model, cli_args=cli_args)

        if eng == "omp":
            return self._resolve_omp(
                model=model,
                session_compaction_threshold=session_compaction_threshold,
                cli_args=cli_args,
                omp_overlay_path=omp_overlay_path,
            )

        return self._resolve_claude(
            model=model,
            session_compaction_threshold=session_compaction_threshold,
            extra_env=extra_env,
            cli_args=cli_args,
            runtime_window_tokens=runtime_window_tokens,
        )

    def _resolve_codex(
        self,
        *,
        model: Optional[str],
        session_compaction_threshold: Optional[Mapping[str, Any]],
        cli_args: Optional[Iterable[str]],
        runtime_window_tokens: Optional[int],
    ) -> Dict[str, Any]:
        cfg = self._load_codex_config()
        disc = self._peek_engine_discovery("codex")
        resolved_model = (
            str(model or "").strip()
            or _extract_cli_flag_value(cli_args or (), ("-m", "--model"))
            or str(cfg.get("model") or disc.get("default_model") or "").strip()
        )

        entry: Dict[str, Any] = {}
        for item in disc.get("model_details") or []:
            if not isinstance(item, Mapping):
                continue
            mid = str(item.get("id") or "")
            if resolved_model and (mid == resolved_model or mid.endswith(f"/{resolved_model}")):
                entry = dict(item)
                break

        window_tokens = _pos_int_or_none(runtime_window_tokens) or _pos_int_or_none(cfg.get("model_context_window"))
        if window_tokens is None:
            cat_win = _pos_int_or_none(entry.get("context_window_tokens"))
            eff_pct = _pos_int_or_none(entry.get("effective_context_window_percent"))
            if cat_win is not None:
                if eff_pct is not None and 1 <= eff_pct <= 100:
                    window_tokens = (cat_win * eff_pct) // 100
                else:
                    window_tokens = cat_win

        compact_tokens: Optional[int] = None
        if isinstance(session_compaction_threshold, Mapping) and session_compaction_threshold.get("unit") == TOKENS:
            compact_tokens = _pos_int_or_none(session_compaction_threshold.get("value"))
        if compact_tokens is None:
            compact_tokens = _extract_codex_compact_override(cli_args)
        if compact_tokens is None:
            compact_tokens = _pos_int_or_none(cfg.get(CODEX_TOKEN_LIMIT_KEY))
        if compact_tokens is None:
            compact_tokens = _pos_int_or_none(entry.get("auto_compact_token_limit"))

        return {
            "model": resolved_model,
            "window_tokens": window_tokens,
            "compact_tokens": compact_tokens,
            "window_estimated": False,
            "estimated_note": "",
        }

    def _resolve_kimi(self, *, model: Optional[str], cli_args: Optional[Iterable[str]]) -> Dict[str, Any]:
        cfg = self._load_kimi_config()
        disc = self._peek_engine_discovery("kimi")
        resolved_model = (
            str(model or "").strip()
            or _extract_cli_flag_value(cli_args or (), ("-m", "--model"))
            or str(cfg.get("default_model") or disc.get("default_model") or "").strip()
        )

        window_tokens: Optional[int] = None
        cfg_models = cfg.get("models") if isinstance(cfg.get("models"), Mapping) else {}
        if resolved_model and resolved_model in cfg_models and isinstance(cfg_models[resolved_model], Mapping):
            window_tokens = _pos_int_or_none(cfg_models[resolved_model].get("max_context_size"))
        elif resolved_model:
            for k, v in cfg_models.items():
                if isinstance(v, Mapping) and (k.endswith(f"/{resolved_model}") or v.get("model") == resolved_model):
                    window_tokens = _pos_int_or_none(v.get("max_context_size"))
                    if window_tokens is not None:
                        break

        if window_tokens is None:
            for item in disc.get("model_details") or []:
                if not isinstance(item, Mapping):
                    continue
                mid = str(item.get("id") or "")
                if resolved_model and (mid == resolved_model or mid.endswith(f"/{resolved_model}")):
                    window_tokens = _pos_int_or_none(item.get("max_context_size")) or _pos_int_or_none(item.get("context_window_tokens"))
                    if window_tokens is not None:
                        break

        compact_tokens: Optional[int] = None
        if window_tokens is not None:
            lc = cfg.get("loop_control") if isinstance(cfg.get("loop_control"), Mapping) else {}
            ratio = _float_or_none(lc.get("max_context_ratio") or lc.get("compaction_trigger_ratio"))
            if ratio is None or not (0.0 < ratio <= 1.0):
                ratio = _KIMI_DEFAULT_MAX_RATIO
            reserved = _int_or_none(lc.get("reserved_context_size"))
            if reserved is None:
                reserved = _KIMI_DEFAULT_RESERVED
            by_ratio = int(window_tokens * ratio)
            if window_tokens > reserved:
                compact_tokens = min(by_ratio, window_tokens - reserved)
            else:
                compact_tokens = by_ratio
            if compact_tokens <= 0:
                compact_tokens = None

        return {
            "model": resolved_model,
            "window_tokens": window_tokens,
            "compact_tokens": compact_tokens,
            "window_estimated": False,
            "estimated_note": "",
        }

    def _resolve_omp(
        self,
        *,
        model: Optional[str],
        session_compaction_threshold: Optional[Mapping[str, Any]],
        cli_args: Optional[Iterable[str]],
        omp_overlay_path: Optional[Path],
    ) -> Dict[str, Any]:
        files = self._load_omp_files()
        yml_models = files.get("models") or {}
        cfg = files.get("config") or {}
        disc = self._peek_engine_discovery("omp")

        resolved_model = (
            str(model or "").strip()
            or _extract_cli_flag_value(cli_args or (), ("--model", "-m"))
            or str(cfg.get("default_model") or disc.get("default_model") or "").strip()
        )

        window_tokens: Optional[int] = None
        if resolved_model:
            exact = yml_models.get(resolved_model)
            if isinstance(exact, Mapping):
                window_tokens = _pos_int_or_none(exact.get("context_window_tokens"))
            if window_tokens is None:
                for full_id, info in yml_models.items():
                    if full_id.endswith(f"/{resolved_model}") and isinstance(info, Mapping):
                        window_tokens = _pos_int_or_none(info.get("context_window_tokens"))
                        if window_tokens is not None:
                            break
        if window_tokens is None:
            for item in disc.get("model_details") or []:
                if not isinstance(item, Mapping):
                    continue
                mid = str(item.get("id") or "")
                m_short = str(item.get("model") or "")
                if resolved_model and (mid == resolved_model or m_short == resolved_model or mid.endswith(f"/{resolved_model}")):
                    window_tokens = _pos_int_or_none(item.get("context_window_tokens")) or parse_token_count(item.get("context_window"))
                    if window_tokens is not None:
                        break

        compact_tokens: Optional[int] = None
        overlay = _parse_overlay_compaction(omp_overlay_path)
        if overlay is not None:
            if overlay.get("enabled") is False:
                compact_tokens = None
            else:
                ttok = _pos_int_or_none(overlay.get("thresholdTokens"))
                tpct = _pos_int_or_none(overlay.get("thresholdPercent"))
                if ttok is not None:
                    compact_tokens = ttok
                elif tpct is not None and window_tokens is not None:
                    compact_tokens = (window_tokens * tpct) // 100

        if compact_tokens is None and isinstance(session_compaction_threshold, Mapping):
            unit = session_compaction_threshold.get("unit")
            val = _pos_int_or_none(session_compaction_threshold.get("value"))
            if unit == TOKENS and val is not None:
                compact_tokens = val
            elif unit == PERCENT and val is not None and window_tokens is not None:
                compact_tokens = (window_tokens * val) // 100

        if compact_tokens is None and window_tokens is not None:
            gcomp = cfg.get("compaction") if isinstance(cfg.get("compaction"), Mapping) else {}
            if gcomp.get("enabled") is not False:
                g_ttok = _pos_int_or_none(gcomp.get("thresholdTokens")) if (gcomp.get("thresholdTokens") or -1) != -1 else None
                g_tpct = _pos_int_or_none(gcomp.get("thresholdPercent")) if (gcomp.get("thresholdPercent") or -1) != -1 else None
                g_res = _pos_int_or_none(gcomp.get("reserveTokens"))
                if g_ttok is not None:
                    compact_tokens = g_ttok
                elif g_tpct is not None:
                    compact_tokens = (window_tokens * g_tpct) // 100
                elif g_res is not None:
                    reserve = max(int(window_tokens * (1.0 - _OMP_DEFAULT_THRESHOLD_RATIO)), g_res)
                    compact_tokens = window_tokens - reserve if window_tokens > reserve else int(window_tokens * _OMP_DEFAULT_THRESHOLD_RATIO)
                else:
                    reserve = max(int(window_tokens * (1.0 - _OMP_DEFAULT_THRESHOLD_RATIO)), _OMP_DEFAULT_RESERVE_TOKENS)
                    compact_tokens = window_tokens - reserve if window_tokens > reserve else int(window_tokens * _OMP_DEFAULT_THRESHOLD_RATIO)

        return {
            "model": resolved_model,
            "window_tokens": window_tokens,
            "compact_tokens": compact_tokens,
            "window_estimated": False,
            "estimated_note": "",
        }

    def _resolve_claude(
        self,
        *,
        model: Optional[str],
        session_compaction_threshold: Optional[Mapping[str, Any]],
        extra_env: Optional[Mapping[str, str]],
        cli_args: Optional[Iterable[str]],
        runtime_window_tokens: Optional[int],
    ) -> Dict[str, Any]:
        settings = self._load_claude_settings()
        settings_env = settings.get("env") if isinstance(settings.get("env"), Mapping) else {}
        disc = self._peek_engine_discovery("claude")
        resolved_model = (
            str(model or "").strip()
            or _extract_cli_flag_value(cli_args or (), ("--model", "-m"))
            or str(settings.get("model") or disc.get("default_model") or "").strip()
        )

        env_max = parse_token_count(
            (extra_env or {}).get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
            or settings_env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
        )
        window_tokens = _pos_int_or_none(runtime_window_tokens) or env_max
        if window_tokens is None:
            if "[1m]" in resolved_model.lower():
                window_tokens = _CLAUDE_1M_WINDOW
            else:
                window_tokens = _CLAUDE_DEFAULT_WINDOW

        effective = window_tokens - min(_CLAUDE_RESERVE_TOKENS, window_tokens // 4)
        default_compact = max(1, effective - min(_CLAUDE_COMPACT_BUFFER, effective // 4))

        pct_override: Optional[int] = None
        if isinstance(session_compaction_threshold, Mapping) and session_compaction_threshold.get("unit") == PERCENT:
            pct_override = _pos_int_or_none(session_compaction_threshold.get("value"))
        if pct_override is None:
            raw_pct = (extra_env or {}).get(CLAUDE_PCT_ENV) or settings_env.get(CLAUDE_PCT_ENV)
            pct_override = _pos_int_or_none(raw_pct)

        if pct_override is not None and 1 <= pct_override <= 99:
            compact_tokens = min((effective * pct_override) // 100, default_compact)
        else:
            compact_tokens = default_compact

        return {
            "model": resolved_model,
            "window_tokens": window_tokens,
            "compact_tokens": compact_tokens,
            "window_estimated": False,
            "estimated_note": "",
        }


class ContextWindowTracker:
    """Tracks incremental per-session context window occupancy from native transcript lines."""

    def __init__(
        self,
        engine: str,
        *,
        model: Optional[str] = None,
        catalog: Optional[ContextWindowCatalog] = None,
        session_compaction_threshold: Optional[Any] = None,
        extra_env: Optional[Mapping[str, str]] = None,
        cli_args: Optional[Iterable[str]] = None,
        omp_overlay_path: Optional[Path] = None,
        initial_snapshot: Optional[Mapping[str, Any]] = None,
    ):
        self.engine = str(engine or "").strip().lower()
        self.supported = self.engine in SUPPORTED_CONTEXT_ENGINES
        self.catalog = catalog or ContextWindowCatalog()
        self.extra_env = dict(extra_env or {})
        self.cli_args = [str(a) for a in (cli_args or ())]
        self.omp_overlay_path = Path(omp_overlay_path) if omp_overlay_path else None

        if isinstance(session_compaction_threshold, Mapping) and "unit" in session_compaction_threshold and "value" in session_compaction_threshold:
            self.session_compaction_threshold: Optional[Dict[str, Any]] = {
                "unit": session_compaction_threshold["unit"],
                "value": session_compaction_threshold["value"],
            }
        else:
            try:
                self.session_compaction_threshold = normalize_compaction_threshold(session_compaction_threshold)
            except ValueError:
                self.session_compaction_threshold = None

        self._lock = threading.RLock()
        self.model: str = str(model or "").strip()
        self.used_tokens: int = 0
        self.runtime_window_tokens: Optional[int] = None
        self.compacted: bool = False
        self.awaiting_post_compact: bool = False
        self.source: str = "initial"
        self.updated_at: float = 0.0
        self._first_prompt_tokens: Optional[int] = None
        self._has_observed: bool = False

        if isinstance(initial_snapshot, Mapping) and self.supported:
            if initial_snapshot.get("model"):
                self.model = str(initial_snapshot["model"]).strip()
            used = _int_or_none(initial_snapshot.get("used_tokens"))
            if used is not None:
                self.used_tokens = used
                self._has_observed = True
            if not initial_snapshot.get("window_estimated"):
                rw = _pos_int_or_none(initial_snapshot.get("window_tokens"))
                if rw is not None:
                    self.runtime_window_tokens = rw
            self.compacted = bool(initial_snapshot.get("compacted"))
            self.awaiting_post_compact = bool(initial_snapshot.get("awaiting_post_compact"))
            if initial_snapshot.get("source"):
                self.source = str(initial_snapshot["source"])
            if initial_snapshot.get("updated_at") is not None:
                self.updated_at = float(initial_snapshot["updated_at"] or 0.0)

    def _record_first_prompt(self, text: Any) -> None:
        if self._first_prompt_tokens is not None or not text:
            return
        s = str(text)
        if s:
            self._first_prompt_tokens = max(1, len(s) // 4)

    def observe_record(self, rec: Mapping[str, Any]) -> bool:
        """Processes one parsed JSON record from the engine's native transcript.

        Returns True iff the externally visible snapshot signature changed.
        """
        if not self.supported or not isinstance(rec, Mapping):
            return False

        with self._lock:
            before = context_window_signature(self._build_snapshot_locked())
            changed = False
            if self.engine == "codex":
                changed = self._observe_codex(rec)
            elif self.engine == "kimi":
                changed = self._observe_kimi(rec)
            elif self.engine == "omp":
                changed = self._observe_omp(rec)
            elif self.engine == "claude":
                changed = self._observe_claude(rec)
            elif self.engine == "agy":
                changed = self._observe_agy(rec)
            if not changed:
                return False
            self.updated_at = time.time()
            after = context_window_signature(self._build_snapshot_locked())
            return after != before

    def _observe_codex(self, rec: Mapping[str, Any]) -> bool:
        rtype = rec.get("type")
        payload = rec.get("payload") if isinstance(rec.get("payload"), Mapping) else {}
        changed = False

        if rtype == "session_meta":
            # Note: session_meta.payload.context_window is {"window_id": ...}, NOT a token integer.
            m = payload.get("model") or rec.get("model")
            if isinstance(m, str) and m.strip():
                self.model = m.strip()
                changed = True
            return changed

        if rtype == "turn_context":
            m = payload.get("model")
            if isinstance(m, str) and m.strip():
                self.model = m.strip()
                changed = True
            return changed

        if rtype == "compacted" or (rtype == "event_msg" and payload.get("type") == "context_compacted"):
            self.compacted = True
            self.awaiting_post_compact = True
            self.source = "compacted"
            return True

        if rtype == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if not isinstance(info, Mapping):
                return False
            mcw = parse_token_count(info.get("model_context_window"))
            if mcw is not None:
                self.runtime_window_tokens = mcw
                changed = True
            last = info.get("last_token_usage")
            if isinstance(last, Mapping):
                # In Codex, input_tokens and total_tokens already include cached_input_tokens.
                # Never add cached_input_tokens again and never use cumulative total_token_usage.
                total = _int_or_none(last.get("total_tokens"))
                if total is None and ("input_tokens" in last or "output_tokens" in last):
                    total = _int0(last.get("input_tokens")) + _int0(last.get("output_tokens"))
                if total is not None:
                    self.used_tokens = total
                    self.awaiting_post_compact = False
                    self.source = "token_count"
                    self._has_observed = True
                    changed = True
            return changed

        return False

    def _observe_kimi(self, rec: Mapping[str, Any]) -> bool:
        msg = rec.get("message") if isinstance(rec.get("message"), Mapping) else rec
        mtype = str(msg.get("type") or "")
        payload = msg.get("payload") if isinstance(msg.get("payload"), Mapping) else msg
        agent_id = payload.get("agentId") or payload.get("agent_id")
        if agent_id is not None and str(agent_id) != "main":
            return False

        if mtype == "profile.bind":
            m = payload.get("modelAlias") or payload.get("model")
            if isinstance(m, str) and m.strip():
                self.model = m.strip()
                return True
            return False

        if mtype == "token_counting.measured":
            tok = _int_or_none(payload.get("tokens"))
            if tok is None:
                tok = _int_or_none(payload.get("tokenCount") or payload.get("contextTokens"))
            if tok is not None:
                self.used_tokens = tok
                self.awaiting_post_compact = False
                self.source = "measured"
                self._has_observed = True
                return True
            return False

        if mtype == "context.apply_compaction":
            self.compacted = True
            after = _int_or_none(payload.get("tokensAfter") or payload.get("afterTokens") or payload.get("tokens"))
            if after is not None:
                self.used_tokens = after
                self.awaiting_post_compact = False
            else:
                self.awaiting_post_compact = True
            self.source = "compaction"
            self._has_observed = True
            return True

        return False

    def _observe_omp(self, rec: Mapping[str, Any]) -> bool:
        rtype = str(rec.get("type") or "")

        if rtype == "model_change":
            mid = rec.get("modelId") or rec.get("model")
            prov = rec.get("provider")
            if isinstance(mid, str) and mid.strip():
                mid_s = mid.strip()
                self.model = f"{prov}/{mid_s}" if (isinstance(prov, str) and prov.strip() and "/" not in mid_s) else mid_s
                return True
            return False

        if rtype == "compaction":
            self.compacted = True
            after = _int_or_none(rec.get("tokensAfter"))
            if after is not None:
                self.used_tokens = after
                self.awaiting_post_compact = False
            else:
                self.awaiting_post_compact = True
            self.source = "compaction"
            self._has_observed = True
            return True

        if rtype == "message":
            msg = rec.get("message") if isinstance(rec.get("message"), Mapping) else rec
            if msg.get("role") != "assistant":
                return False
            if msg.get("stopReason") in ("aborted", "error"):
                return False
            usage = msg.get("usage")
            if not isinstance(usage, Mapping):
                return False
            m = msg.get("model")
            prov = msg.get("provider")
            if isinstance(m, str) and m.strip():
                m_s = m.strip()
                if isinstance(prov, str) and prov.strip() and "/" not in m_s:
                    self.model = f"{prov.strip()}/{m_s}"
                else:
                    self.model = m_s

            used = _int_or_none(usage.get("contextTokens"))
            if used is None:
                total = _int_or_none(usage.get("totalTokens"))
                if total is not None:
                    used = max(0, total - _int0(usage.get("orchestration")))
                else:
                    used = (
                        _int0(usage.get("input"))
                        + _int0(usage.get("cacheRead"))
                        + _int0(usage.get("cacheWrite"))
                        + _int0(usage.get("output"))
                    )
            self.used_tokens = used
            self.awaiting_post_compact = False
            self.source = "assistant_usage"
            self._has_observed = True
            return True

        return False

    def _observe_claude(self, rec: Mapping[str, Any]) -> bool:
        if rec.get("isSidechain") or rec.get("isApiErrorMessage"):
            return False

        rtype = str(rec.get("type") or "")
        if rtype == "system" and rec.get("subtype") == "compact_boundary":
            self.compacted = True
            self.awaiting_post_compact = True
            self.source = "compact_boundary"
            return True

        if rtype == "assistant":
            msg = rec.get("message") if isinstance(rec.get("message"), Mapping) else {}
            m = str(msg.get("model") or "").strip()
            if m.startswith("<"):
                return False
            if m:
                self.model = m
            usage = msg.get("usage")
            if not isinstance(usage, Mapping) or not usage:
                return False
            used = (
                _int0(usage.get("input_tokens"))
                + _int0(usage.get("cache_creation_input_tokens"))
                + _int0(usage.get("cache_read_input_tokens"))
                + _int0(usage.get("output_tokens"))
            )
            if used > 0 or "input_tokens" in usage:
                self.used_tokens = used
                self.awaiting_post_compact = False
                self.source = "assistant_usage"
                self._has_observed = True
                return True
            return False

        if rtype == "result":
            mu = rec.get("modelUsage")
            if isinstance(mu, Mapping) and mu:
                chosen: Optional[Mapping[str, Any]] = None
                if self.model and isinstance(mu.get(self.model), Mapping):
                    chosen = mu[self.model]
                else:
                    for k, v in mu.items():
                        if isinstance(v, Mapping) and _pos_int_or_none(v.get("contextWindow")) is not None:
                            chosen = v
                            if not self.model and not str(k).startswith("<"):
                                self.model = str(k)
                            break
                if chosen is not None:
                    cw = _pos_int_or_none(chosen.get("contextWindow"))
                    if cw is not None:
                        self.runtime_window_tokens = cw
                        return True
            return False

        return False

    def _observe_agy(self, rec: Mapping[str, Any]) -> bool:
        rtype = str(rec.get("type") or "").upper()
        if isinstance(rec.get("model"), str) and rec["model"].strip():
            self.model = rec["model"].strip()

        if rtype == "USER_INPUT":
            self._record_first_prompt(rec.get("content"))
            return False

        if rtype == "CHECKPOINT":
            self.compacted = True
            self.awaiting_post_compact = True
            out = _int0(rec.get("output_tokens"))
            base = (
                min(self._first_prompt_tokens, _AGY_CHECKPOINT_PROMPT_CAP)
                if self._first_prompt_tokens is not None
                else _AGY_CHECKPOINT_PROMPT_FALLBACK
            )
            self.used_tokens = base + out
            self.source = "checkpoint_est"
            self._has_observed = True
            return True

        if rtype == "PLANNER_RESPONSE":
            inp = _int0(rec.get("input_tokens"))
            cr = _int0(rec.get("cache_read_tokens"))
            out = _int0(rec.get("output_tokens"))
            used = inp + cr + out
            if used <= 0 and isinstance(rec.get("usage"), Mapping):
                u = rec["usage"]
                used = _int0(u.get("input_tokens")) + _int0(u.get("cache_read_tokens")) + _int0(u.get("output_tokens"))
            if used > 0 or rec.get("status") == "DONE":
                if used > 0:
                    self.used_tokens = used
                self.awaiting_post_compact = False
                self.source = "planner_response"
                self._has_observed = True
                return True
            return False

        return False

    def _build_snapshot_locked(self) -> Optional[Dict[str, Any]]:
        if not self.supported:
            return None
        resolved = self.catalog.resolve(
            self.engine,
            model=self.model or None,
            session_compaction_threshold=self.session_compaction_threshold,
            extra_env=self.extra_env,
            cli_args=self.cli_args,
            omp_overlay_path=self.omp_overlay_path,
            runtime_window_tokens=self.runtime_window_tokens,
        )
        window_tokens = resolved.get("window_tokens")
        compact_tokens = resolved.get("compact_tokens")

        if window_tokens is None and not self._has_observed:
            return None

        used_pct: Optional[float] = None
        if window_tokens and window_tokens > 0:
            used_pct = round(max(0.0, (self.used_tokens / window_tokens) * 100.0), 1)

        compact_pct: Optional[float] = None
        if window_tokens and window_tokens > 0 and compact_tokens is not None and compact_tokens > 0:
            compact_pct = round(min(100.0, max(0.0, (compact_tokens / window_tokens) * 100.0)), 1)

        return {
            "engine": self.engine,
            "model": resolved.get("model") or self.model or "",
            "used_tokens": int(self.used_tokens),
            "window_tokens": int(window_tokens) if window_tokens is not None else None,
            "compact_tokens": int(compact_tokens) if compact_tokens is not None else None,
            "used_pct": used_pct,
            "compact_pct": compact_pct,
            "window_estimated": bool(resolved.get("window_estimated")),
            "awaiting_post_compact": bool(self.awaiting_post_compact),
            "compacted": bool(self.compacted),
            "source": self.source,
            "estimated_note": str(resolved.get("estimated_note") or ""),
            "updated_at": round(self.updated_at, 3) if self.updated_at else None,
        }

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._build_snapshot_locked()


def _existing_path(candidate: Any) -> Optional[Path]:
    if not candidate:
        return None
    try:
        p = Path(str(candidate)).expanduser()
        if p.is_file():
            return p
    except Exception:
        return None
    return None


def resolve_native_transcript_path(
    engine: str,
    session_record: Mapping[str, Any],
    home: Optional[Path] = None,
) -> Optional[Path]:
    """Resolves the raw CLI transcript file for historical backfill of archived sessions."""
    eng = str(engine or "").strip().lower()
    if eng not in SUPPORTED_CONTEXT_ENGINES:
        return None
    base_home = Path(home) if home is not None else Path(os.path.expanduser("~"))

    direct = _existing_path(session_record.get("transcript_path"))
    if direct is not None:
        return direct

    conv_ref = session_record.get("conversation_ref")
    if isinstance(conv_ref, str):
        try:
            conv_ref = json.loads(conv_ref)
        except Exception:
            conv_ref = None
    ref = conv_ref if isinstance(conv_ref, Mapping) else {}

    if eng == "codex":
        for key in ("rollout_path", "native_path", "path"):
            p = _existing_path(ref.get(key))
            if p is not None:
                return p
        tid = str(ref.get("thread_id") or "").strip()
        if tid:
            matches = sorted((base_home / ".codex" / "sessions").rglob(f"*-{tid}.jsonl"))
            if matches:
                return matches[-1]
        return None

    if eng == "omp":
        for key in ("session_file", "native_path", "path"):
            p = _existing_path(ref.get(key))
            if p is not None:
                return p
        return None

    if eng == "kimi":
        for key in ("wire_path", "native_path", "path"):
            p = _existing_path(ref.get(key))
            if p is not None:
                return p
        sdir = ref.get("session_dir")
        if sdir:
            for rel in (Path("agents") / "main" / "wire.jsonl", Path("wire.jsonl")):
                p = _existing_path(Path(str(sdir)) / rel)
                if p is not None:
                    return p
        return None

    if eng == "claude":
        for key in ("transcript_path", "session_file", "jsonl_path", "native_path", "path"):
            p = _existing_path(ref.get(key))
            if p is not None:
                return p
        sid = str(ref.get("session_id") or "").strip()
        if sid:
            matches = sorted((base_home / ".claude" / "projects").rglob(f"{sid}.jsonl"))
            if matches:
                return matches[-1]
        return None

    if eng == "agy":
        for key in ("transcript_path", "native_path", "path"):
            p = _existing_path(ref.get(key))
            if p is not None:
                return p
        cid = str(ref.get("conversation_id") or "").strip()
        if cid:
            p = _existing_path(base_home / ".gemini" / "antigravity" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl")
            if p is not None:
                return p
        return None

    return None


def scan_file_into_tracker(path: Path, tracker: ContextWindowTracker) -> Optional[Dict[str, Any]]:
    """Synchronously streams a native JSONL transcript file into tracker (run via asyncio.to_thread)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, Mapping):
                    tracker.observe_record(rec)
    except Exception:
        return tracker.snapshot()
    return tracker.snapshot()
