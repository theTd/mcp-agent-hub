"""The load test's fake engine replays a corpus in order, at the recorded rate, and accounts for it.

Run: py -3.15 -B -m unittest test_fake_tui_engine
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parent
ENGINE = HUB_DIR / "tools" / "fake_tui_engine.py"
DONE_RE = re.compile(r"LOADENGINE done case=(\S+) loops=(\d+) chunks=(\d+) corpus_bytes=(\d+) "
                     r"markers=(\d+) total_bytes=(\d+) elapsed_ms=(\d+) speed=(\S+)")


def write_case(root: Path, name: str, chunks, delays_ms, encoding: str = "utf8_bytes") -> Path:
    case = root / name
    case.mkdir(parents=True)
    meta = {"kind": "session", "chunk_encoding": encoding, "cols": 120, "rows": 36}
    (case / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    lines = []
    for chunk, delay in zip(chunks, delays_ms):
        stored = base64.b64encode(chunk).decode("ascii") if encoding == "utf8_bytes" else chunk.decode("utf-8")
        lines.append(json.dumps({"delay_ms": delay, "chunk": stored}))
    (case / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return case


def run_engine(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(ENGINE), *args],
        capture_output=True, timeout=timeout, check=False,
    )


def parse_done(stdout: bytes) -> dict:
    text = stdout.decode("utf-8", "replace")
    match = DONE_RE.search(text)
    if not match:
        raise AssertionError(f"no summary line in {text!r}")
    case, loops, chunks, corpus_bytes, markers, total_bytes, elapsed_ms, speed = match.groups()
    return {
        "case": case, "loops": int(loops), "chunks": int(chunks),
        "corpus_bytes": int(corpus_bytes), "markers": int(markers),
        "total_bytes": int(total_bytes), "elapsed_ms": int(elapsed_ms), "speed": float(speed),
    }


class ReplayTests(unittest.TestCase):
    def test_bytes_are_replayed_in_order_and_accounted(self):
        chunks = [b"\x1b[2Jalpha\n", "charlie \u4e2d\u6587\n".encode("utf-8"), b"bravo\r\n", b"delta"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case(root, "case-a", chunks, [0, 0, 0, 0])
            journal = root / "j.jsonl"
            proc = run_engine("--case", "case-a", "--corpus", str(root), "--marker-every", "2",
                              "--journal", str(journal))
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        # The chunks appear once, in corpus order, with only marker lines between them.
        body = proc.stdout
        positions = [body.index(chunk) for chunk in chunks]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(body.count(chunks[0]), 1)
        done = parse_done(body)
        self.assertEqual(done["case"], "case-a")
        self.assertEqual(done["loops"], 1)
        self.assertEqual(done["chunks"], 4)
        self.assertEqual(done["corpus_bytes"], sum(len(c) for c in chunks))
        self.assertEqual(done["markers"], 2)
        marker_line = b"\r\nLOADMARK 1\r\n"
        self.assertEqual(done["total_bytes"], done["corpus_bytes"] + 2 * len(marker_line))
        self.assertIn(marker_line, body)
        self.assertIn(b"\r\nLOADMARK 2\r\n", body)
        self.assertNotIn(b"LOADMARK 3", body)
        self.assertEqual([r["event"] for r in records], ["marker", "marker", "done"])
        self.assertEqual([r["marker"] for r in records[:2]], [1, 2])
        self.assertEqual(records[0]["corpus_bytes"], len(chunks[0]) + len(chunks[1]))
        self.assertEqual(records[-1]["total_bytes"], done["total_bytes"])

    def test_speed_scales_the_recorded_delays(self):
        chunks = [b"one\n", b"two\n", b"three\n"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case(root, "case-slow", chunks, [120, 120, 120], encoding="text")
            started = time.perf_counter()
            plain = run_engine("--case", "case-slow", "--corpus", str(root), "--marker-every", "0")
            plain_wall = time.perf_counter() - started
            fast = run_engine("--case", "case-slow", "--corpus", str(root), "--speed", "8",
                              "--marker-every", "0")
        self.assertEqual(plain.returncode, 0)
        self.assertEqual(fast.returncode, 0)
        # Encoding `text` is replayed as UTF-8, and a case with no markers writes only its chunks.
        self.assertTrue(plain.stdout.startswith(b"one\ntwo\nthree\n\r\nLOADENGINE done "))
        plain_done = parse_done(plain.stdout)
        fast_done = parse_done(fast.stdout)
        self.assertEqual((plain_done["chunks"], plain_done["corpus_bytes"], plain_done["markers"]), (3, 14, 0))
        self.assertGreaterEqual(plain_wall, 0.3, "the recorded 360 ms was not waited out")
        self.assertGreaterEqual(plain_done["elapsed_ms"], 350)
        # 360 ms of recorded delay at 8x is ~45 ms; the summary carries the measured time.
        self.assertLess(fast_done["elapsed_ms"], 250)
        self.assertLess(fast_done["elapsed_ms"], plain_done["elapsed_ms"] // 2)

    def test_loop_stops_after_seconds(self):
        chunks = [b"x" * 5, b"y" * 5]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case(root, "case-loop", chunks, [50, 50])
            proc = run_engine("--case", "case-loop", "--corpus", str(root), "--loop", "--seconds", "0.35",
                              "--marker-every", "1", "--marker-hold-ms", "0")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        done = parse_done(proc.stdout)
        self.assertGreaterEqual(done["loops"], 2, "the loop did not repeat")
        self.assertGreaterEqual(done["chunks"], 4)
        self.assertEqual(done["corpus_bytes"], done["chunks"] * 5)
        self.assertEqual(done["markers"], done["chunks"])
        self.assertGreaterEqual(done["elapsed_ms"], 350)
        self.assertEqual(proc.stdout.count(b"LOADMARK "), done["markers"])

    def test_marker_hold_keeps_the_marker_on_screen(self):
        chunks = [b"a\n", b"b\n", b"c\n"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case(root, "case-hold", chunks, [0, 0, 0])
            proc = run_engine("--case", "case-hold", "--corpus", str(root), "--marker-every", "1",
                              "--marker-hold-ms", "100")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        done = parse_done(proc.stdout)
        self.assertEqual(done["markers"], 3)
        self.assertGreaterEqual(done["elapsed_ms"], 250, "the 3 x 100 ms hold was not waited out")
        self.assertEqual(proc.stdout.count(b"LOADMARK "), 3)

    def test_unknown_case_fails_loudly(self):
        proc = run_engine("--case", "no-such-case")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"no corpus case", proc.stderr)


if __name__ == "__main__":
    unittest.main()
