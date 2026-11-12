"""Per-session auto-compaction threshold: one caller value, translated into each engine's own setting.

The caller passes either a percentage of the context window ("70%") or a token count (200000,
"200000", "200k"). Each engine supports only the units it has a per-process setting for:

- omp: both, through a ``--config`` overlay file the hub writes for the session
  (``compaction.thresholdPercent`` / ``compaction.thresholdTokens``, per-model thresholds off).
- claude: percent, through ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``. Claude uses
  min(window * pct, its default threshold), so the value can only make compaction earlier.
- codex: tokens, through ``-c model_auto_compact_token_limit=<n>``.

Every other engine has no per-session setting (Kimi and DSH read a host-wide file, AGY's switch
is undocumented) and rejects the parameter instead of ignoring it.
"""
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

PERCENT = "percent"
TOKENS = "tokens"
MIN_PERCENT = 1
MAX_PERCENT = 99
MIN_TOKENS = 1000

ENGINE_UNITS: Dict[str, tuple] = {
    "omp": (PERCENT, TOKENS),
    "claude": (PERCENT,),
    "codex": (TOKENS,),
}

CLAUDE_PCT_ENV = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
CODEX_TOKEN_LIMIT_KEY = "model_auto_compact_token_limit"

_PERCENT_RE = re.compile(r"(\d+)\s*%")
_TOKENS_RE = re.compile(r"(\d+)\s*([kKmM]?)")
_TOKEN_SUFFIX = {"": 1, "k": 1000, "m": 1000000}

_FORMAT_HINT = "自动压缩阈值（compaction_threshold）写成百分比（如 \"70%\"）或 token 数（如 200000、\"200k\"）"


def normalize_compaction_threshold(value: Any) -> Optional[Dict[str, Any]]:
    """{"unit": "percent"|"tokens", "value": int}, or None when unset; raises a Chinese ValueError."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(_FORMAT_HINT)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(_FORMAT_HINT)
        value = int(value)
    if isinstance(value, int):
        unit, number = TOKENS, value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        percent = _PERCENT_RE.fullmatch(text)
        tokens = _TOKENS_RE.fullmatch(text)
        if percent:
            unit, number = PERCENT, int(percent.group(1))
        elif tokens:
            unit, number = TOKENS, int(tokens.group(1)) * _TOKEN_SUFFIX[tokens.group(2).lower()]
        else:
            raise ValueError(_FORMAT_HINT)
    else:
        raise ValueError(_FORMAT_HINT)
    if unit == PERCENT and not MIN_PERCENT <= number <= MAX_PERCENT:
        raise ValueError(f"自动压缩阈值百分比必须在 {MIN_PERCENT}% 到 {MAX_PERCENT}% 之间")
    if unit == TOKENS and number < MIN_TOKENS:
        raise ValueError(
            f"自动压缩阈值 token 数至少 {MIN_TOKENS}；百分比请写成 \"{number}%\"" if number <= MAX_PERCENT
            else f"自动压缩阈值 token 数至少 {MIN_TOKENS}"
        )
    return {"unit": unit, "value": number}


def format_compaction_threshold(spec: Mapping[str, Any]) -> str:
    return f"{spec['value']}%" if spec["unit"] == PERCENT else f"{spec['value']} tokens"


def check_compaction_support(engine: str, kind: str, name: str, spec: Mapping[str, Any]) -> None:
    """Raise when this engine cannot apply the threshold for one session."""
    units = ENGINE_UNITS.get(engine) if kind == "tui" else None
    if not units:
        supported = "、".join(f"{eng}（{'/'.join('百分比' if u == PERCENT else 'token 数' for u in us)}）"
                             for eng, us in ENGINE_UNITS.items())
        raise ValueError(f"{name} 不支持按会话设置自动压缩阈值（compaction_threshold）。支持的引擎：{supported}")
    if spec["unit"] not in units:
        want = "百分比（如 \"70%\"）" if units == (PERCENT,) else "token 数（如 200000）"
        raise ValueError(f"{name} 的自动压缩阈值只能写成{want}")


def check_compaction_conflicts(engine: str, args: Iterable[str], caller_env: Mapping[str, str]) -> None:
    """The same setting given another way (extra_args, env) would make the result order-dependent."""
    if engine == "codex" and any(CODEX_TOKEN_LIMIT_KEY in str(arg) for arg in args):
        raise ValueError(f"启动参数里已有 {CODEX_TOKEN_LIMIT_KEY}，与 compaction_threshold 冲突，只能用其中一个")
    if engine == "claude" and CLAUDE_PCT_ENV in caller_env:
        raise ValueError(f"环境变量里已有 {CLAUDE_PCT_ENV}，与 compaction_threshold 冲突，只能用其中一个")


def omp_compaction_overlay_text(spec: Mapping[str, Any], session_id: str) -> str:
    """A config.yml overlay (JSON is valid YAML). Per-model thresholds are switched off, as omp does
    itself when it hands a threshold to its own subagents, so they cannot override this value."""
    compaction = {
        "enabled": True,
        "thresholdPercent": spec["value"] if spec["unit"] == PERCENT else -1,
        "thresholdTokens": spec["value"] if spec["unit"] == TOKENS else -1,
        "modelThresholdsEnabled": False,
    }
    return (
        f"# Written by mcp-agent-hub for session {session_id}: compaction_threshold "
        f"{format_compaction_threshold(spec)}\n"
        f"{json.dumps({'compaction': compaction})}\n"
    )


def write_omp_compaction_overlay(runtime_dir: Path, session_id: str, spec: Mapping[str, Any]) -> Path:
    """Kept beside omp_sess_<id>/ for the session's lifetime: a resume reuses the same --config path."""
    path = Path(runtime_dir) / f"omp_compaction_{session_id}.yml"
    path.write_text(omp_compaction_overlay_text(spec, session_id), encoding="utf-8")
    return path
