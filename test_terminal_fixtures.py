"""Terminal corpus fixtures: layout, bytes, privacy, and the relations between the committed goldens.

The goldens under ``tools/fixtures/terminal/`` are the spec. They were rendered by the pyte oracle
(``tools/pyte_oracle.py``, deleted at step 6) and are now compared
against the Rust core by the corpus gate, ``terminal-core/terminal_stream/tests/fixture_corpus``:
that harness feeds every case through ``TerminalStream`` and requires the screen, scrollback, raw,
replay and trim files to match byte for byte (screen rows through ``screen_exceptions.jsonl``, which
is empty). This file checks what does not need a screen model: the case layout and metadata, the
recorded sizes, the scrubbed bytes, the privacy scan, and the relations between committed files.
"""
import base64
import codecs
import json
import os
import re
from pathlib import Path
import unittest

import session_manager

FIXTURES_ROOT = Path(__file__).resolve().parent / "tools" / "fixtures" / "terminal"
LIVE_CASES = ("live-agy", "live-claude", "live-codex", "live-codex-scrolled-paste", "live-kimi",
              "live-omp", "live-omp-approval-box")
EDGE_CASES = (
    "edge-screen-combining",
    "edge-screen-csi-cancel",
    "edge-screen-csi-controls",
    "edge-screen-csi-marker",
    "edge-screen-decom",
    "edge-screen-decom-region",
    "edge-screen-decstbm",
    "edge-screen-decstbm-oneparam",
    "edge-screen-decstbm-partial",
    "edge-screen-save-restore",
    "edge-screen-save-restore-origin",
    "edge-screen-zero-width",
    "edge-screen-zero-width-split",
    "edge-osc-color-query",
    "edge-osc-color-query-split",
    "edge-osc-reply",
    "edge-osc-reply-52-11x",
    "edge-osc-reply-split",
    "edge-utf8-split",
    "edge-w8-esc3j-carry-splits",
    "edge-w8-esc3j-split",
    "edge-w9-banner-after-cut",
    "edge-w9-banner-lead-crlf",
    "edge-w9-banner-too-old",
    "edge-w9-inloop-trigger",
    "edge-w9-trim-banner-inside",
    "edge-w9-trim-budget-zero",
    "edge-w9-trim-mid-character",
)
MAX_CORPUS_BYTES = 2 * 1024 * 1024
# The design's 80-line screen read (§4) and `_capture_submit_diagnostic`'s character tail.
SCREEN_LINES = 80
RAW_TAIL_CHARS = session_manager.SUBMIT_DIAGNOSTIC_TAIL_CHARS
# The core's W9 constants (`terminal_stream/src/history.rs`, `TRUNCATED_PREFIX`/`BANNER_LINE_MAX_BYTES`).
# Only the assertions about a golden's over-cap bound read them; no golden is computed from them.
TRUNCATED_PREFIX = "\x1b[90m[较早的输出已截断]\x1b[0m\r\n"
BANNER_LINE_MAX_BYTES = 128
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
UUID_RE = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SESSION_ID_RE = re.compile(r"(?i)session_[0-9a-f]{6,}")
# An absolute Windows path: its drive letter survives only before ``<HOME>``-style placeholders.
DRIVE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9_<])[A-Za-z]:[\\/]")
# Placeholder-shaped tokens only: ``ESC[<u`` and friends are kitty-keyboard sequences, not labels.
PLACEHOLDER_RE = re.compile(r"<[A-Za-z][A-Za-z0-9_]{0,30}>")
PLACEHOLDER_NAME_RE = re.compile(r"<[A-Z][A-Z0-9_]{1,20}>")


def read_text_exact(path):
    """Golden bytes as they are: universal-newline translation would hide a CRLF the hub rendered."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def read_chunk_records(path):
    return [json.loads(line) for line in read_text_exact(path).splitlines() if line.strip()]


def decode_chunks(meta, records):
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
    texts = []
    for record in records:
        payload = base64.b64decode(str(record["chunk"]))
        texts.append(decoder.decode(payload, final=False))
    tail = decoder.decode(b"", final=True)
    if tail:
        texts.append(tail)
    return texts


class Case:
    """One fixture folder: its meta, and its chunk stream as the hub's reader saw it."""

    def __init__(self, path: Path):
        self.path = path
        self.meta = json.loads(read_text_exact(path / "meta.json"))
        self.chunks = decode_chunks(self.meta, read_chunk_records(path / "chunks.jsonl"))

    @property
    def kind(self):
        return str(self.meta.get("kind") or "session")

    @property
    def engine(self):
        return str(self.meta.get("engine") or "generic")

    @property
    def cols(self):
        return int(self.meta.get("cols") or 120)

    @property
    def rows(self):
        return int(self.meta.get("rows") or 36)

    @property
    def resume_count(self):
        """Which resume the ``resumed/`` goldens model; meta.json records it, nothing is hardcoded."""
        return int(self.meta.get("resume_count") or 1)

    @property
    def resize(self):
        value = self.meta.get("resize")
        return value if isinstance(value, dict) else None


def case_dirs():
    return sorted(path for path in FIXTURES_ROOT.iterdir()
                  if path.is_dir() and (path / "meta.json").is_file())


def case_meta(name):
    return json.loads(read_text_exact(FIXTURES_ROOT / name / "meta.json"))


def case(name):
    return Case(FIXTURES_ROOT / name)


def golden(name, path):
    return read_text_exact(FIXTURES_ROOT / name / path)


def corpus_bytes(meta, records):
    """The corpus size on the wire: base64-decoded pipes bytes, or the text form's UTF-8 length."""
    if meta.get("chunk_encoding") == "utf8_bytes":
        return sum(len(base64.b64decode(str(record["chunk"]))) for record in records)
    return len("".join(str(record["chunk"]) for record in records).encode("utf-8"))


def case_texts():
    """Every string a fixture holds: file text, the decoded chunk stream, and decoded meta values."""
    for path in sorted(FIXTURES_ROOT.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes().decode("utf-8", "replace")
        yield path, raw
        if path.suffix == ".jsonl":
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
            # The stream as the hub will see it: base64 chunks are the pipe's bytes, decoded with
            # the renderer's UTF-8 carry, so a byte corpus is scanned like a text one.
            yield path, "".join(decode_chunks(case_meta(path.parent.name), records))
        elif path.suffix == ".json":
            # The JSON file's own text escapes the separators; the decoded values carry them plainly.
            yield path, json.dumps(json.loads(raw), ensure_ascii=False)


def generated_chunks(generator):
    """The corpus a ``trigger`` case's generator block describes: numbered lines, whole-line chunks.

    Deterministic and dependency-free, so the Rust corpus gate regenerates the same bytes from the
    same block instead of trusting a 600 KB file it cannot read.
    """
    template = str(generator["line_format"])
    terminator = str(generator.get("line_terminator") or "\r\n")
    first = int(generator.get("first_index") or 0)
    count = int(generator["count"])
    chunk_bytes = int(generator.get("chunk_bytes") or 4096)
    chunks = []
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


def trigger_seed(case_):
    """The resume seed a ``trigger`` case starts from: archived lines, then the separator."""
    spec = case_.meta.get("seed") if isinstance(case_.meta.get("seed"), dict) else {}
    archived = str(spec.get("archived_repeat") or "") * int(spec.get("archived_count") or 0)
    return f"{archived}{session_manager.resume_banner(case_.resume_count)}"


class TerminalFixtureLayoutTests(unittest.TestCase):
    def test_every_recorded_and_edge_case_is_present(self):
        names = {path.name for path in case_dirs()}
        self.assertEqual(set(LIVE_CASES) - names, set(), "missing live recordings")
        self.assertEqual(set(EDGE_CASES) - names, set(), "missing edge cases")

    def test_meta_describes_the_corpus(self):
        for path in case_dirs():
            meta = case_meta(path.name)
            records = read_chunk_records(path / "chunks.jsonl")
            self.assertIn(meta["kind"], ("session", "trim", "input", "trigger"), path.name)
            self.assertTrue(records, f"{path.name}: empty corpus")
            self.assertIn("chunk_encoding", meta, path.name)
            if meta["kind"] == "session":
                self.assertTrue(meta["cols"] >= 20 and meta["rows"] >= 5, path.name)
            if meta.get("resize"):
                resize = meta["resize"]
                self.assertEqual(len(resize["to"]), 2, path.name)
                self.assertLessEqual(resize["chunk_index"], len(records), path.name)
            if meta["kind"] in ("session", "trigger"):
                # Which resume the ``resumed/`` goldens model: nothing may hardcode it.
                self.assertIsInstance(meta.get("resume_count"), int, path.name)
            if meta["kind"] == "trigger":
                self.assertIn("generator", meta, path.name)
                self.assertIn("seed", meta, path.name)

    def test_live_corpora_keep_the_size_and_chunk_count_they_were_recorded_with(self):
        # The scrub re-splits at the original boundaries, so both numbers must survive it unchanged.
        for name in LIVE_CASES:
            meta = case_meta(name)
            recorded = meta["recorder"]
            records = read_chunk_records(FIXTURES_ROOT / name / "chunks.jsonl")
            self.assertEqual(recorded["chunk_count"], len(records), name)
            self.assertLessEqual(corpus_bytes(meta, records), MAX_CORPUS_BYTES, name)
            self.assertTrue(recorded["exit"]["stop_reason"], name)
            self.assertGreaterEqual(recorded["max_chunk_bytes"], 1, name)

    def test_system_conpty_corpora_carry_exact_bytes(self):
        # Step 2a: a system-ConPTY case is the pipe's bytes, base64 in chunks.jsonl, and the stream is
        # one valid UTF-8 sequence whose chunk boundaries may cut a character (the core's 3-byte carry).
        system = set()
        for name in LIVE_CASES:
            meta = case_meta(name)
            self.assertIn(meta["chunk_encoding"], ("text", "utf8_bytes"), name)
            if meta.get("pty") != "system-conpty":
                continue
            system.add(name)
            self.assertEqual(meta["chunk_encoding"], "utf8_bytes", name)
            self.assertEqual(meta["read_buffer_bytes"], 8192, name)
            self.assertRegex(str(meta["windows_build"]), r"^\d+\.\d+\.\d+(\.\d+)?$", name)
            records = read_chunk_records(FIXTURES_ROOT / name / "chunks.jsonl")
            joined = b"".join(base64.b64decode(str(record["chunk"])) for record in records)
            joined.decode("utf-8")  # a byte corpus that is not one UTF-8 stream would render as U+FFFD
        # live-agy is still the step-0 pywinpty capture: agy's own MCP config points at the live hub
        # and it has no session-scoped MCP flag, so the recorder refused to run it (step 2a report).
        self.assertEqual(system, {"live-claude", "live-codex", "live-kimi", "live-omp",
                                  "live-codex-scrolled-paste", "live-omp-approval-box"})

    def test_scrub_report_is_per_run(self):
        # Labels and counts only: the record names categories, never what was replaced.
        allowed_labels = re.compile(r"^(?:[a-z0-9_]+|window_[a-z0-9_]+)$")
        for name in LIVE_CASES:
            meta = case_meta(name)
            scrub = meta.get("scrub") or {}
            self.assertGreaterEqual(scrub.get("runs") or 0, 2, name)
            self.assertEqual(scrub.get("chunks"),
                             len(read_chunk_records(FIXTURES_ROOT / name / "chunks.jsonl")), name)
            self.assertNotIn("chars_before", scrub, f"{name}: cumulative block keeps a stale char count")
            last_run = scrub.get("last_run") or {}
            self.assertIsInstance(last_run.get("replacements"), int, name)
            self.assertIsInstance(last_run.get("chars_before"), int, name)
            self.assertIsInstance(last_run.get("chars_after"), int, name)
            for rule in list(scrub.get("rules") or []) + list(last_run.get("rules") or []):
                self.assertRegex(str(rule.get("label")), allowed_labels, name)
                self.assertIsInstance(rule.get("count"), int, name)


class TerminalFixtureGoldenTests(unittest.TestCase):
    def test_every_session_case_has_its_replay_frame(self):
        for path in case_dirs():
            meta = case_meta(path.name)
            if meta["kind"] == "session":
                self.assertTrue((path / "replay.txt").is_file(), path.name)
                self.assertTrue((path / "resumed" / "replay.txt").is_file(), path.name)

    def test_replay_is_the_join_until_a_query_is_split(self):
        # No colour query in this corpus: the replay is the untrimmed join, identical to raw.
        self.assertEqual(golden("edge-w8-esc3j-split", "replay.txt"),
                         golden("edge-w8-esc3j-split", "raw.txt"))
        # The query is split over three chunks, so the per-chunk strip keeps the fragments in the
        # history while the join-then-strip replay removes them: the two must differ.
        raw = golden("edge-osc-color-query-split", "raw.txt")
        replay = golden("edge-osc-color-query-split", "replay.txt")
        self.assertNotEqual(raw, replay)
        self.assertIn("\x1b]1", raw)
        self.assertNotIn("\x1b]1", replay)
        self.assertLess(len(replay), len(raw))
        # The corpus gate compares both files against the core's own ring and replay.

    def test_the_in_loop_trigger_collapses_the_ring_and_keeps_the_tail(self):
        name = "edge-w9-inloop-trigger"
        text = case(name)
        chunks = text.chunks
        record = json.loads(golden(name, "trigger.json"))
        index = record["fired_at_chunk"]
        self.assertIsInstance(index, int, record)
        self.assertEqual(record["entries_after"], 1, record)
        self.assertGreater(record["entries_before"], 1, record)
        max_bytes = record["max_bytes"]
        slack = record["slack_bytes"]
        # The chunk that collapsed it is exactly the one whose append crossed max + slack.
        self.assertLessEqual(record["bytes_before"], max_bytes + slack, record)
        self.assertGreater(record["bytes_before"] + len(chunks[index].encode("utf-8")),
                           max_bytes + slack, record)
        self.assertLessEqual(record["bytes_after"], max_bytes, record)
        # The committed history is the collapsed ring: its bytes are what the record counted, and the
        # collapse cuts the front only, so the submit-diagnostic tail is unchanged.
        raw = golden(name, "raw.txt")
        self.assertEqual(record["final_bytes"], len(raw.encode("utf-8")), record)
        joined = trigger_seed(text) + "".join(chunks)
        self.assertEqual(golden(name, "raw_tail.txt"), joined[-RAW_TAIL_CHARS:])
        # Every chunk after the collapse appends one entry again; nothing else merges them.
        self.assertEqual(record["final_entries"], 1 + len(chunks) - index - 1, record)
        # The corpus gate re-runs the collapse through the core's ring and compares raw.txt exactly.

    def test_the_trigger_corpus_matches_the_generator_its_meta_describes(self):
        # The corpus gate regenerates chunks.jsonl from the same block; it must be exact.
        meta = case_meta("edge-w9-inloop-trigger")
        generated = generated_chunks(meta["generator"])
        self.assertEqual(generated, case("edge-w9-inloop-trigger").chunks)
        total = len("".join(generated).encode("utf-8"))
        self.assertGreaterEqual(total, 600 * 1024)
        self.assertLessEqual(total, 640 * 1024)

    def test_the_w9_banner_cases_pin_the_hub_rule(self):
        # The prefix is not recomputed here: the budget-zero golden is exactly the prefix the trim
        # writes, so it is the committed copy of it.
        prefix = golden("edge-w9-trim-budget-zero", "trimmed.txt")
        self.assertEqual(prefix, TRUNCATED_PREFIX)
        # The banner's own opening, including the two leading bytes the block may restore; built from
        # the hub's separator so the corpus' resume count (1 or 2) does not matter.
        banner = session_manager.resume_banner(1).split("第")[0]
        for name, banner_kept in (("edge-w9-banner-after-cut", True),
                                  ("edge-w9-trim-banner-inside", True),
                                  ("edge-w9-banner-too-old", False),
                                  ("edge-w9-banner-lead-crlf", True)):
            meta = case_meta(name)
            text = "".join(case(name).chunks)
            raw = text.encode("utf-8")
            budget = meta["max_bytes"] - len(prefix.encode("utf-8"))
            found = text.rfind(banner)
            self.assertGreaterEqual(found, 0, f"{name}: no resume banner in the corpus")
            banner_start = len(text[:found].encode("utf-8"))
            trimmed = golden(name, "trimmed.txt")
            body = trimmed[len(prefix):].encode("utf-8")
            self.assertTrue(raw.endswith(body), f"{name}: the trim only cuts the front")
            start = len(raw) - len(body)
            if banner_kept:
                self.assertGreaterEqual(banner_start, start, name)
            else:
                self.assertLess(banner_start, start, name)
                self.assertNotIn("已恢复", trimmed, name)
            # The rule: the banner is kept whole when the budget's own cut (before the hub moves it
            # to a line break) falls before the end of the newest banner line, and dropped when the
            # cut is at or past that end.  Only the cut is computed here; the trim is the core's.
            raw_cut = len(raw) - budget
            marker_start = len(text[:found + 2].encode("utf-8"))
            banner_end = raw.index(b"\n", marker_start) + 1
            self.assertEqual(banner_kept, raw_cut < banner_end, name)
            over = len(trimmed.encode("utf-8")) - meta["max_bytes"]
            if name == "edge-w9-trim-banner-inside":
                # The cut is inside the banner line, so keeping it goes over the cap by less than it.
                self.assertGreater(raw_cut, banner_start, name)
                self.assertGreater(over, 0, name)
                self.assertLess(over, BANNER_LINE_MAX_BYTES, name)
            else:
                self.assertLessEqual(over, 0, name)
            if name == "edge-w9-banner-lead-crlf":
                self.assertTrue(trimmed.startswith(prefix + "\r\n"), name)


class TerminalFixtureHubStateTests(unittest.TestCase):
    def test_the_hub_carries_no_pyte_state_of_its_own(self):
        # `oracle-to-tools` then step 6: the terminal is the core's, and the oracle is gone.
        for name in ("HubTerminalScreen", "HubTerminalStream", "trim_history_text",
                     "OSC_COLOR_QUERY_RE"):
            self.assertFalse(hasattr(session_manager, name), name)


class TerminalFixturePrivacyTests(unittest.TestCase):
    def test_no_host_marker_survives_in_any_fixture(self):
        user = os.environ.get("USERNAME") or ""
        machine = os.environ.get("COMPUTERNAME") or ""
        profile = os.environ.get("USERPROFILE") or ""
        self.assertTrue(user and machine and profile,
                        "environment lacks USERNAME/COMPUTERNAME/USERPROFILE; the scan would be blind")
        patterns = []
        patterns.append(("user name", re.compile(r"(?<![A-Za-z0-9_<])" + re.escape(user)
                                                 + r"(?![A-Za-z0-9_])", re.IGNORECASE)))
        patterns.append(("machine name", re.compile(r"(?<![A-Za-z0-9_\-<])" + re.escape(machine)
                                                    + r"(?![A-Za-z0-9_\-])", re.IGNORECASE)))
        # Either separator spelling, the JSON-escaped double backslash, and the percent-encoded form.
        flexible = re.escape(profile).replace(r"\\", r"[\\/]{1,2}")
        patterns.append(("profile path", re.compile(flexible, re.IGNORECASE)))
        for old, new in (("\\", "%5C"), ("/", "%2F"), (":", "%3A")):
            patterns.append(("percent-encoded profile path",
                             re.compile(re.escape(profile.replace(old, new)), re.IGNORECASE)))
        patterns.append(("e-mail address", EMAIL_RE))
        patterns.append(("UUID or resume handle", UUID_RE))
        patterns.append(("session_<hex> handle", SESSION_ID_RE))
        patterns.append(("absolute path outside a placeholder", DRIVE_PATH_RE))

        offenders = []
        for path, text in case_texts():
            relative = path.relative_to(FIXTURES_ROOT).as_posix()
            for label, pattern in patterns:
                if pattern.search(text):
                    offenders.append(f"{relative}: {label}")
            for token in PLACEHOLDER_RE.findall(text):
                if not PLACEHOLDER_NAME_RE.fullmatch(token):
                    offenders.append(f"{relative}: not a placeholder: {token[:40]!r}")
        self.assertEqual(offenders, [], "fixtures still carry personal data:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main(verbosity=2)
