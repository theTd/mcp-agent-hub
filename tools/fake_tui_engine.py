"""Replay a recorded terminal corpus to stdout, as a stand-in for a printing engine (design §11).

This is the child of a real hub session: the hub spawns it through the terminal core (or, before the
cut-over, pywinpty) into a ConPTY, so the load test exercises the whole path — ConPTY, core reader,
parser, frame delivery, WebSocket consumer — with bytes whose timing is known.

Usage (from the repository root, as the profile command the load tool writes):

    py -3.15 -B tools/fake_tui_engine.py --case live-codex --speed 1 --loop \
        --marker-every 20 --journal scratch/session-1.jsonl

Options:

- ``--case NAME|PATH``: a directory under ``--corpus`` holding ``chunks.jsonl`` + ``meta.json`` (the
  step-0 recorder's format), or an explicit directory. The ``live-*`` cases are the real engines.
- ``--corpus DIR``: default ``<this file>/fixtures/terminal``.
- ``--speed F``: 1 replays at the recorded rate, 2 twice as fast, 0.5 half speed.
- ``--loop``: replay the case again and again (marker numbers keep counting across loops).
- ``--seconds N``: stop after N seconds of wall time (checked before each chunk; also the only way
  ``--loop`` ends by itself).
- ``--marker-every N``: after every N chunks write ``LOADMARK <n>`` on its own line; 0 disables.
- ``--marker-hold-ms N``: after a marker line, wait N ms before the next chunk. ConPTY repaints the
  screen and may merge a marker into the following full-screen draw, so a marker that is on screen for
  one paint interval is what a consumer can be expected to see; the wait is added to that repetition's
  schedule, so the corpus spacing after it is unchanged.
- ``--journal PATH``: append one JSON line per marker (and one ``done`` line at the end) so the load
  tool can still account for what was written when the hub kills this process. Each line carries
  ``at`` (unix seconds), which lets the load tool exclude markers the hub was still delivering when
  the sockets closed. Stdout only carries the final summary line, because a kill leaves no chance to
  print it.

Output is written with ``os.write`` to fd 1, so every chunk reaches the ConPTY as it is produced
(no stdio buffering). The final line is::

    LOADENGINE done case=<case> loops=<n> chunks=<n> corpus_bytes=<n> markers=<n> total_bytes=<n> elapsed_ms=<n> speed=<f>

``corpus_bytes`` counts replayed corpus bytes, ``total_bytes`` adds the marker lines.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_CORPUS = Path(__file__).resolve().parent / "fixtures" / "terminal"
MARKER_PREFIX = "LOADMARK "
DONE_PREFIX = "LOADENGINE done "


def resolve_case(case: str, corpus: Path) -> Path:
    """The case directory: an absolute path wins, otherwise it is a name under the corpus root."""
    direct = Path(case)
    if direct.is_dir():
        return direct.resolve()
    nested = (corpus / case).resolve()
    if nested.is_dir():
        return nested
    raise SystemExit(f"fake_tui_engine: no corpus case {case!r} under {str(corpus)!r}")


def read_case(case_dir: Path) -> Tuple[str, str, List[bytes], List[float]]:
    """``(case name, chunk_encoding, chunks, delay_seconds)`` for one recorded case.

    ``meta.json`` names the encoding the recorder used: ``utf8_bytes`` chunks are base64 of the exact
    bytes one ``ReadFile`` returned, ``text`` chunks are the text ``pty.read`` returned and are
    re-encoded here. A chunk split across two reads stays split, which is the point of the corpus.
    """
    meta_path = case_dir / "meta.json"
    chunks_path = case_dir / "chunks.jsonl"
    if not meta_path.is_file() or not chunks_path.is_file():
        raise SystemExit(f"fake_tui_engine: {str(case_dir)} is not a corpus case (needs meta.json and chunks.jsonl)")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    encoding = str(meta.get("chunk_encoding") or "text")
    chunks: List[bytes] = []
    delays: List[float] = []
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        raw = str(record.get("chunk") or "")
        if encoding == "utf8_bytes":
            chunks.append(base64.b64decode(raw))
        elif encoding == "text":
            chunks.append(raw.encode("utf-8"))
        else:
            raise SystemExit(f"fake_tui_engine: unknown chunk_encoding {encoding!r} in {str(meta_path)}")
        delays.append(max(0, int(record.get("delay_ms") or 0)) / 1000.0)
    if not chunks:
        raise SystemExit(f"fake_tui_engine: {str(chunks_path)} has no chunks")
    return case_dir.name, encoding, chunks, delays


def write_all(data: bytes) -> None:
    """Write every byte to fd 1; ``os.write`` may take less than asked in one call."""
    view = memoryview(data)
    while view:
        written = os.write(1, view)
        if written <= 0:
            raise OSError("stdout closed")
        view = view[written:]


class Journal:
    """Append-only accounting for a process the hub may kill before it can print its summary."""

    def __init__(self, path: Optional[Path]) -> None:
        self._path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def record(self, **fields) -> None:
        if self._path is None:
            return
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(fields, ensure_ascii=False, separators=(",", ":")) + "\n")


def replay(
    case: str,
    chunks: List[bytes],
    delays: List[float],
    speed: float,
    loop: bool,
    seconds: Optional[float],
    marker_every: int,
    marker_hold: float,
    journal: Journal,
) -> None:
    """Write the case at the recorded rate until it ends, ``--seconds`` elapses or the process dies."""
    began = time.perf_counter()
    marker = 0
    loops = 0
    chunks_written = 0
    corpus_bytes = 0
    total_bytes = 0
    stopping = False
    # Each repetition's schedule is absolute (its start + the case's cumulative delay / speed), so a
    # slow write or a sleeping thread cannot make later chunks drift later, and the next repetition
    # keeps the recorded spacing instead of sprinting to catch up with the previous one.
    while True:
        schedule_start = time.perf_counter()
        offset = 0.0
        for index, chunk in enumerate(chunks):
            offset += delays[index]
            remaining = schedule_start + offset / speed - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            if seconds is not None and time.perf_counter() - began >= seconds:
                stopping = True
                break
            write_all(chunk)
            corpus_bytes += len(chunk)
            total_bytes += len(chunk)
            chunks_written += 1
            if marker_every > 0 and chunks_written % marker_every == 0:
                marker += 1
                line = f"\r\n{MARKER_PREFIX}{marker}\r\n".encode("ascii")
                write_all(line)
                total_bytes += len(line)
                journal.record(event="marker", marker=marker, corpus_bytes=corpus_bytes,
                               total_bytes=total_bytes, at=time.time(),
                               at_ms=int((time.perf_counter() - began) * 1000))
                if marker_hold > 0:
                    time.sleep(marker_hold)
                    # The hold belongs to this repetition's schedule: the recorded spacing after the
                    # marker is kept instead of the next chunk firing immediately to catch up.
                    schedule_start += marker_hold
        if stopping:
            break
        loops += 1
        if not loop:
            break
        if seconds is not None and time.perf_counter() - began >= seconds:
            break
    elapsed_ms = int((time.perf_counter() - began) * 1000)
    summary = (f"{DONE_PREFIX}case={case} loops={loops} chunks={chunks_written} "
               f"corpus_bytes={corpus_bytes} markers={marker} total_bytes={total_bytes} "
               f"elapsed_ms={elapsed_ms} speed={speed}")
    write_final(summary)
    journal.record(event="done", case=case, loops=loops, chunks=chunks_written,
                   corpus_bytes=corpus_bytes, markers=marker, total_bytes=total_bytes,
                   elapsed_ms=elapsed_ms, speed=speed, at=time.time())


def write_final(summary: str) -> None:
    try:
        write_all(f"\r\n{summary}\r\n".encode("ascii", "replace"))
    except OSError:
        # The console is gone (the hub killed the session tree): the journal is all there is.
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a recorded terminal corpus to stdout (design §11).")
    parser.add_argument("--case", required=True, help="corpus case name under --corpus, or a directory")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="corpus root (default: tools/fixtures/terminal)")
    parser.add_argument("--speed", type=float, default=1.0, help="1 = the recorded rate")
    parser.add_argument("--loop", action="store_true", help="repeat the case until killed or --seconds")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    parser.add_argument("--marker-every", type=int, default=20, help="LOADMARK line every N chunks (0 = off)")
    parser.add_argument("--marker-hold-ms", type=float, default=40.0,
                        help="keep each marker line on screen this long (default 40 ms)")
    parser.add_argument("--journal", default=None, help="append JSON accounting lines to this file")
    args = parser.parse_args(argv)
    if args.speed <= 0:
        raise SystemExit("fake_tui_engine: --speed must be > 0")
    case_dir = resolve_case(args.case, Path(args.corpus))
    case, _encoding, chunks, delays = read_case(case_dir)
    journal = Journal(Path(args.journal) if args.journal else None)
    try:
        replay(case, chunks, delays, args.speed, args.loop, args.seconds, args.marker_every,
               args.marker_hold_ms / 1000.0, journal)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
