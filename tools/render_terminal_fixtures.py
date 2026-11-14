"""Render the frozen terminal corpus into golden outputs with today's Python code (§9 step 0).

This is the oracle side of the migration: the Rust parser and history ring (step 2) must reproduce
these files byte for byte.  Nothing here reimplements the hub's behaviour — it imports it.  After
step 3b that behaviour lives in the offline oracle (decision `oracle-to-tools`,
``tools/pyte_oracle.py``), because the hub's terminal is the Rust core and it no longer imports
``pyte``:

- ``PyteOracleSession.feed_output``: W8 strip on resumed sessions, OSC colour-query strip, the
  raw-history append and the W9 trim with its slack, all in the order the PTY reader used to run
  them;
- ``HubTerminalScreen``/``HubTerminalStream`` through the session's ``stream.feed``;
- ``read_screen``/``read_scrollback``/``read_raw``/``read_raw_tail``/``seed_history`` (the seam's
  reply shapes, design §5.2), ``trim_history_text`` and ``resume_banner``;
- the socket's replay frame (the joined ring with one more ``OSC_COLOR_QUERY_RE`` pass, §6.4), so
  the golden is what the core's socket sends a newly attached console;
- ``OSC_TERMINAL_REPLY_RE``/``strip_osc_terminal_replies`` (the rule the core's socket applies to
  browser input, mirrored here from ``session_manager``) for the browser→PTY input cases, and the
  oracle's ``OSC_COLOR_QUERY_RE`` for the ring.

The submit-diagnostic tail and the resumed seed are no longer mirrored expressions: both are seam
calls (``read_raw_tail``, ``seed_history``) and the tail's bound is the imported
``SUBMIT_DIAGNOSTIC_TAIL_CHARS``, so a change in the hub moves the goldens with it.

Goldens are UTF-8 written with newline translation off, so a golden's own ``\\r\\n`` stays byte-exact;
a golden ends in whatever the hub's rendered text ends in (usually no newline, but ``raw`` and
``replay`` keep the stream's own trailing ``\\r\\n``).

``session``  screen_final.txt, screen_at_resize.txt (when the corpus resizes), scrollback_250.txt,
             raw.txt (W9-trimmed raw history, as ``_scrollback_blob`` writes it), raw_tail.txt
             (``_capture_submit_diagnostic``'s last 65536 chars), replay.txt (what the core's
             socket sends a new console: the whole ``raw_history`` join with one more OSC
             colour-query strip), and the same files under ``resumed/`` for a session that starts
             from the archived history plus the resume banner (``_make_resumed_session``, :4864),
             with ``resume_count`` read from meta.json.
``trigger``  a session fed enough output to cross ``max_history_bytes + HISTORY_TRIM_SLACK_BYTES``
             in-loop: raw.txt (the collapsed history — the replay frame would be these same bytes),
             raw_tail.txt and trigger.json (whether and where the W9 trigger fired, and the entry and
             byte counts after it).  Its chunks are regenerated from meta's ``generator`` block by
             step 2's Rust test.
``trim``     trimmed.txt: ``trim_history_text`` of the joined corpus with meta's ``max_bytes``.
``input``    stripped.txt: what ``strip_osc_terminal_replies`` lets through, message by message.

``chunk_encoding`` in meta.json is ``text`` for the step-0 pywinpty recordings (winpty hands the hub
``str``) and ``utf8_bytes`` for a system-ConPTY recording and for the byte-level edge case: ``chunk``
is base64 of exactly what one ``ReadFile`` returned (one pipe read, 8 KiB buffer), so a multi-byte
character split across two reads stays split.  The decoder below carries the partial bytes into the
next chunk, which is the 3-byte carry the core implements (design §5.4); the text the hub is fed is
the same either way, so the goldens do not depend on which of the two is modelled.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from session_manager import (  # noqa: E402  (the hub's own constants, never copied)
    HISTORY_TRIM_SLACK_BYTES,
    OSC_TERMINAL_REPLY_RE,
    RESUME_SCROLLBACK_MAX_BYTES,
    SUBMIT_DIAGNOSTIC_TAIL_CHARS,
    resume_banner,
    strip_osc_terminal_replies,
)
from tools.pyte_oracle import (  # noqa: E402  (the pyte oracle, `oracle-to-tools`)
    OSC_COLOR_QUERY_RE,
    PyteOracleSession,
    trim_history_text,
)

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "terminal"
# The two limits the hub reads: screen 80 lines, scrollback 250 (design doc §4 consumer map).
SCREEN_LINES = 80
SCROLLBACK_LINES = 250
# ``_capture_submit_diagnostic``'s bound, imported from the hub so the two cannot drift.
RAW_TAIL_CHARS = SUBMIT_DIAGNOSTIC_TAIL_CHARS
RESUMED_DIR = "resumed"


class Case:
    def __init__(self, path: Path):
        self.path = path
        self.meta: Dict[str, object] = json.loads(read_text_exact(path / "meta.json"))
        self.chunks: List[str] = decode_chunks(self.meta, read_chunk_records(path / "chunks.jsonl"))

    @property
    def kind(self) -> str:
        return str(self.meta.get("kind") or "session")

    @property
    def engine(self) -> str:
        return str(self.meta.get("engine") or "generic")

    @property
    def cols(self) -> int:
        return int(self.meta.get("cols") or 120)

    @property
    def rows(self) -> int:
        return int(self.meta.get("rows") or 36)

    @property
    def resume_count(self) -> int:
        """Which resume the ``resumed/`` goldens model; meta.json records it, nothing is hardcoded."""
        return int(self.meta.get("resume_count") or 1)

    @property
    def resize(self) -> Optional[Dict[str, object]]:
        value = self.meta.get("resize")
        return value if isinstance(value, dict) else None


def read_text_exact(path: Path) -> str:
    """Golden bytes as they are: universal-newline translation would hide a CRLF the hub rendered."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def read_chunk_records(path: Path) -> List[Dict[str, object]]:
    return [json.loads(line) for line in read_text_exact(path).splitlines() if line.strip()]


def decode_chunks(meta: Dict[str, object], records: List[Dict[str, object]]) -> List[str]:
    """The chunk texts the hub's reader would have produced.

    ``text``: already exactly what ``pty.read`` returned.  ``utf8_bytes``: base64 pipe bytes; one
    incremental decoder reproduces the core's carry, so every text chunk is the text up to the end
    of that read and the number of chunks stays what was recorded.
    """
    encoding = str(meta.get("chunk_encoding") or "text")
    if encoding == "text":
        return [str(record["chunk"]) for record in records]
    if encoding != "utf8_bytes":
        raise SystemExit(f"unknown chunk_encoding {encoding!r}")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    texts: List[str] = []
    for record in records:
        payload = base64.b64decode(str(record["chunk"]))
        texts.append(decoder.decode(payload, final=False))
    tail = decoder.decode(b"", final=True)
    if tail:
        texts.append(tail)
    return texts


def resume_seed(archived: str, resume_count: int) -> str:
    """Mirror of ``_make_resumed_session``'s seed (:4862-4866); the test pins that source text."""
    return f"{archived}{resume_banner(resume_count)}" if archived else resume_banner(resume_count)


def new_session(engine: str, cols: int, rows: int, resume_count: int = 0,
                seed: str = "") -> PyteOracleSession:
    """The oracle terminal of one session: a pyte screen and a raw ring, fed chunk by chunk."""
    session = PyteOracleSession(engine, cols, rows, resume_count=resume_count)
    if seed:
        # the seam's spawn.seed_history: archived output plus separator, one entry, rev 1.
        session.seed_history(seed)
    return session


def screen_text(session: PyteOracleSession) -> str:
    return session.read_screen(SCREEN_LINES).text


def joined_raw(session: PyteOracleSession) -> str:
    return session.read_raw().text


def trimmed_raw(session: PyteOracleSession) -> str:
    return trim_history_text(joined_raw(session), RESUME_SCROLLBACK_MAX_BYTES)


def replay_frame(session: PyteOracleSession) -> str:
    """The replay the core's socket sends a newly attached console (§6.4).

    It is the join of ``raw_history`` and *then* one more ``OSC_COLOR_QUERY_RE`` pass, which is not
    the same string as the per-chunk-stripped history when a query is split across two chunks. The
    oracle computes it; nothing of the hub's socket path is involved.
    """
    return session.replay_frame()


def feed_session(session: PyteOracleSession, case: Case) -> Optional[str]:
    """Feeds every chunk; returns the screen right after the resize, when the corpus has one."""
    resize = case.resize
    at_resize: Optional[str] = None
    index_at_resize = int(resize.get("chunk_index") or 0) if resize else -1
    for index, chunk in enumerate(case.chunks):
        if resize and index == index_at_resize:
            session.resize(int(resize["to"][0]), int(resize["to"][1]))  # type: ignore[index]
            at_resize = screen_text(session)
        session.feed_output(chunk)
    if resize and at_resize is None:
        session.resize(int(resize["to"][0]), int(resize["to"][1]))  # type: ignore[index]
        at_resize = screen_text(session)
    return at_resize


def render_session(case: Case, resume_count: int = 0, seed: str = "") -> Dict[str, str]:
    session = new_session(case.engine, case.cols, case.rows, resume_count=resume_count, seed=seed)
    at_resize = feed_session(session, case)
    goldens = {
        "screen_final.txt": screen_text(session),
        "scrollback_250.txt": session.read_scrollback(SCROLLBACK_LINES).text,
        "raw.txt": trimmed_raw(session),
        "raw_tail.txt": session.read_raw_tail(RAW_TAIL_CHARS),
        "replay.txt": replay_frame(session),
    }
    if at_resize is not None:
        goldens["screen_at_resize.txt"] = at_resize
    return goldens


def generated_chunks(generator: Dict[str, object]) -> List[str]:
    """The corpus a ``trigger`` case's generator block describes: numbered lines, whole-line chunks.

    Deterministic and dependency-free, so step 2's Rust test regenerates the same bytes from the
    same block instead of trusting a 600 KB file it cannot read.
    """
    template = str(generator["line_format"])
    terminator = str(generator.get("line_terminator") or "\r\n")
    first = int(generator.get("first_index") or 0)
    count = int(generator["count"])
    chunk_bytes = int(generator.get("chunk_bytes") or 4096)
    chunks: List[str] = []
    pending = ""
    pending_bytes = 0
    for index in range(first, first + count):
        line = template.format(i=index) + terminator
        size = len(line.encode("utf-8"))
        if pending and pending_bytes + size > chunk_bytes:
            chunks.append(pending)
            pending, pending_bytes = "", 0
        pending += line
        pending_bytes += size
    if pending:
        chunks.append(pending)
    return chunks


def trigger_seed(case: Case) -> str:
    """The resume seed a ``trigger`` case starts from: archived lines, then the separator."""
    spec = case.meta.get("seed") if isinstance(case.meta.get("seed"), dict) else {}
    archived = str(spec.get("archived_repeat") or "") * int(spec.get("archived_count") or 0)
    return resume_seed(archived, case.resume_count)


def render_trigger(case: Case) -> Dict[str, str]:
    """A session that crosses the W9 in-loop trigger: the history collapses to one trimmed entry."""
    session = new_session(case.engine, case.cols, case.rows,
                          resume_count=case.resume_count, seed=trigger_seed(case))
    fired_at: Optional[int] = None
    record: Dict[str, object] = {"fired_at_chunk": None}
    for index, chunk in enumerate(case.chunks):
        entries_before = len(session.raw_history)
        bytes_before = session.raw_history_bytes
        session.feed_output(chunk)
        collapsed = fired_at is None and len(session.raw_history) < entries_before
        if collapsed:
            fired_at = index
            record = {
                "fired_at_chunk": index,
                "entries_before": entries_before,
                "entries_after": len(session.raw_history),
                "bytes_before": bytes_before,
                "bytes_after": session.raw_history_bytes,
                "max_bytes": session.max_history_bytes,
                "slack_bytes": HISTORY_TRIM_SLACK_BYTES,
            }
    record["final_entries"] = len(session.raw_history)
    record["final_bytes"] = session.raw_history_bytes
    # No replay.txt: the ring already holds the trimmed text, so the replay frame and raw.txt would
    # be the same 512 KB file.  What is worth pinning here is the collapse, which trigger.json says.
    return {
        "raw.txt": joined_raw(session),
        "raw_tail.txt": session.read_raw_tail(RAW_TAIL_CHARS),
        "trigger.json": json.dumps(record, indent=2) + "\n",
    }


def render_trim(case: Case) -> Dict[str, str]:
    max_bytes = int(case.meta.get("max_bytes") or RESUME_SCROLLBACK_MAX_BYTES)
    return {"trimmed.txt": trim_history_text("".join(case.chunks), max_bytes)}


def render_input(case: Case) -> Dict[str, str]:
    # The core's socket strips each browser message before it reaches the PTY (§6.4); this is the
    # same rule, imported rather than copied so the goldens move with it.
    return {"stripped.txt": "".join(strip_osc_terminal_replies(chunk) for chunk in case.chunks)}


def render_case(path: Path) -> Dict[str, str]:
    """Every golden file this case should have, keyed by its path inside the case directory."""
    case = Case(path)
    if case.kind == "trigger":
        return render_trigger(case)
    if case.kind == "trim":
        return render_trim(case)
    if case.kind == "input":
        return render_input(case)
    if case.kind != "session":
        raise SystemExit(f"{path}: unknown case kind {case.kind!r}")
    goldens = render_session(case)
    # A resumed pane replays the archived (trimmed) output, then the separator; W8 is on from the
    # first byte, so the corpus' own repaint is rendered with the ESC[3J strip active.
    count = case.resume_count
    for name, text in render_session(case, resume_count=count,
                                     seed=resume_seed(goldens["raw.txt"], count)).items():
        goldens[f"{RESUMED_DIR}/{name}"] = text
    return goldens


def write_goldens(path: Path, goldens: Dict[str, str]) -> List[Path]:
    written: List[Path] = []
    for name, text in goldens.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        written.append(target)
    return written


def diff(expected: str, actual: str) -> str:
    """The first difference, quoted without surrounding context (goldens may hold personal data)."""
    limit = min(len(expected), len(actual))
    for index in range(limit):
        if expected[index] != actual[index]:
            return (f"first difference at char {index}: expected {expected[index]!r}, "
                    f"got {actual[index]!r}")
    if len(expected) != len(actual):
        longer = "expected" if len(expected) > len(actual) else "got"
        return f"length differs: expected {len(expected)} chars, got {len(actual)} ({longer} is longer)"
    return "no difference"


def check_case(path: Path) -> List[str]:
    failures: List[str] = []
    case = Case(path)
    goldens = render_case(path)
    for name, expected in goldens.items():
        target = path / name
        if not target.is_file():
            failures.append(f"{path.name}/{name}: golden missing")
            continue
        actual = read_text_exact(target)
        if actual != expected:
            failures.append(f"{path.name}/{name}: {diff(expected, actual)}")
    for pattern in ("*.txt", "*.json"):
        for target in sorted(path.rglob(pattern)):
            if target.name == "meta.json":
                continue
            name = target.relative_to(path).as_posix()
            if name not in goldens:
                failures.append(f"{path.name}/{name}: stale golden (not produced by this corpus)")
    if case.kind == "input":
        # Keep the pattern itself honest: the input cases must exercise the reply strip.
        if not any(OSC_TERMINAL_REPLY_RE.search(chunk) for chunk in case.chunks):
            failures.append(f"{path.name}: input case has no OSC reply to strip")
    if case.kind in ("session", "trigger") and "resume_count" not in case.meta:
        # The resumed goldens are rendered with this count; without it a reader cannot tell which.
        failures.append(f"{path.name}: meta.json has no resume_count for its resumed goldens")
    return failures


def case_dirs(root: Path) -> List[Path]:
    return sorted(path for path in root.iterdir() if path.is_dir() and (path / "meta.json").is_file())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=FIXTURES_ROOT)
    parser.add_argument("--case", type=Path, default=None, help="render one case directory")
    parser.add_argument("--write", action="store_true", help="write the goldens instead of checking")
    args = parser.parse_args(argv)
    targets = [args.case.resolve()] if args.case else case_dirs(args.root)
    failures: List[str] = []
    for path in targets:
        goldens = render_case(path)
        if args.write:
            written = write_goldens(path, goldens)
            print(f"{path.name}: wrote {len(written)} goldens")
        else:
            failures.extend(check_case(path))
            print(f"{path.name}: checked {len(goldens)} goldens")
    for failure in failures:
        print(f"FAIL {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())