"""Regenerates the machine-independent part of tests/fixtures/cmdline_parity.tsv.

Run it with the dev interpreter that has pywinpty installed:

    py -3.15 -B terminal-core/conpty_session/tests/capture_cmdline_parity.py

The fixture used to encode this machine's `PATH` (`RESOLVE node ->
C:\\Program Files\\nodejs\\node.EXE`), so the parity test could only be green on a machine that had
node installed there. This capture builds its own layout instead:

* a stub directory holding `probe.exe`, `probe.cmd`, `probe.bat` and a no-extension `bare` file;
* `PATH` set to that directory and `PATHEXT` set to a fixed list;
* the expected rows come from `winpty.ptyprocess.which` — the function pywinpty's `PtyProcess.spawn`
  calls — with the same `path = env.get("PATH", os.defpath)` it passes;
* one `PATHEXT-DEFAULT` row carries `shutil._WIN_DEFAULT_PATHEXT`, what `which` uses when the process
  environment has no `PATHEXT` (pywinpty imports `shutil.which`, so that is its fallback exactly).

Only `RESOLVE` rows and the `PATHEXT`/`PATHEXT-DEFAULT` headers are rewritten; the `CASE` rows (what a
real pywinpty child received as `GetCommandLineW`) are carried over unchanged. The directory is written
as `<TOOLS>`, so no absolute path from this machine lands in the tree, and the Rust test builds the same
layout itself.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cmdline_parity.tsv"
PATHEXT = ".COM;.EXE;.BAT;.CMD"
TOOLS_TOKEN = "<TOOLS>"


def hexed(text: str) -> str:
    return text.encode("utf-8").hex()


def main() -> int:
    try:
        from winpty.ptyprocess import which  # the exact symbol pywinpty spawns through
    except ImportError:
        print("pywinpty is not importable; run this with the hub's dev interpreter", file=sys.stderr)
        return 1

    kept = [
        line
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.startswith("CASE\t")
    ]
    if not kept:
        print(f"{FIXTURE} has no CASE rows to keep", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="cmdline-parity-") as tmp:
        tools = Path(tmp) / "parity tools"
        tools.mkdir()
        for name in ("probe.exe", "probe.cmd", "probe.bat", "bare"):
            (tools / name).write_bytes(b"")

        env = {"PATH": str(tools)}
        # pywinpty's spawn: `which(command, path=env.get('PATH', os.defpath))`; `which` reads PATHEXT
        # from the process environment, so set it for the capture exactly as the test does.
        os.environ["PATHEXT"] = PATHEXT
        rows = [
            f"PATHEXT\t{hexed(PATHEXT)}",
            # What `which` falls back to when the process env has no PATHEXT: pywinpty imports
            # `shutil.which`, so this list is exactly `shutil._WIN_DEFAULT_PATHEXT`.
            f"PATHEXT-DEFAULT\t{hexed(shutil._WIN_DEFAULT_PATHEXT)}",
        ]
        for command in ("probe", "probe.cmd", "probe.bat", "bare"):
            resolved = which(command, path=env.get("PATH", os.defpath))
            expected = "" if resolved is None else hexed(resolved.replace(str(tools), TOOLS_TOKEN))
            rows.append(f"RESOLVE\t{command}\t{expected}")

    # LF, as git stores text in this repo (core.autocrlf=input); the parity test reads either.
    text = "\n".join(rows + kept) + "\n"
    FIXTURE.write_bytes(text.encode("utf-8"))
    print(f"wrote {FIXTURE} ({len(rows)} generated rows, {len(kept)} kept CASE rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())