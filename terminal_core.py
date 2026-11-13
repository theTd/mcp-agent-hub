"""Thin import wrapper for the Rust terminal core extension (``hub_terminal_core``).

The hub imports this module at start and refuses to run without the compiled core
(docs/rust-terminal-core.md §7.3): there is no Python PTY fallback, so a missing module must fail
here with the exact build command instead of failing later on a launch.

The extension is one abi3 artifact built for CPython 3.11+ and dropped beside ``server.py``:
    cargo build --release --manifest-path terminal-core\\Cargo.toml
    Copy-Item terminal-core\\target\\release\\hub_terminal_core.dll hub_terminal_core.pyd
"""
from pathlib import Path

_REPO_DIR = Path(__file__).resolve().parent
_EXPECTED = _REPO_DIR / "hub_terminal_core.pyd"

try:
    import hub_terminal_core as _core
except ImportError as exc:
    raise ImportError(
        "终端核心未构建：缺少 hub_terminal_core（预期 {expected}）。\n"
        "构建：cargo build --release --manifest-path terminal-core\\Cargo.toml\n"
        "      Copy-Item terminal-core\\target\\release\\hub_terminal_core.dll hub_terminal_core.pyd".format(
            expected=_EXPECTED
        )
    ) from exc

Core = _core.Core
# §6.4/§8: the browser-reply strip moves to the core in step 4; exported here so Python reaches it now.
strip_osc_terminal_replies = _core.strip_osc_terminal_replies
__version__ = _core.__version__
ABI = _core.ABI

__all__ = ["Core", "strip_osc_terminal_replies", "ABI", "__version__"]
