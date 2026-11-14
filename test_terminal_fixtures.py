"""Terminal corpus fixtures: the goldens re-render, pin today's behaviour and carry no personal data.

Run: ``py -3.15 -B test_terminal_fixtures.py`` from the repository root.

The goldens were rendered from exactly the committed bytes by ``tools/render_terminal_fixtures.py``
with today's Python code; step 2's Rust parser and history ring must reproduce them byte for byte, so
this test is the parity gate.  It also guards the *oracle* itself: the renderer must use the hub's own
objects (``AgentSession``, ``trim_history_text``, ``resume_banner``, the screen and stream classes,
``attach_ws_queue``), because a copy of one of them would render goldens nobody compares against the
hub anymore.

The privacy check reads the host markers from the environment at test time and never stores them: a
fixture that carries the current user name, machine name, profile path, an absolute path, an e-mail
address, a UUID or a ``session_<hex>`` handle fails instead of being committed.

Line endings: goldens are compared with ``newline=""``, so a golden's own ``\\r\\n`` is preserved and
the check is byte-exact rather than whatever the platform's universal-newline reader would give.
"""

import base64
import inspect
import json
import os
import re
import unittest
from pathlib import Path

import session_manager
from tools import pyte_oracle
from tools import render_terminal_fixtures as renderer

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
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
UUID_RE = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SESSION_ID_RE = re.compile(r"(?i)session_[0-9a-f]{6,}")
# An absolute Windows path: its drive letter survives only before ``<HOME>``-style placeholders.
DRIVE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9_<])[A-Za-z]:[\\/]")
# Placeholder-shaped tokens only: ``ESC[<u`` and friends are kitty-keyboard sequences, not labels.
PLACEHOLDER_RE = re.compile(r"<[A-Za-z][A-Za-z0-9_]{0,30}>")
PLACEHOLDER_NAME_RE = re.compile(r"<[A-Z][A-Z0-9_]{1,20}>")


def case_dirs():
    return renderer.case_dirs(FIXTURES_ROOT)


def case_meta(name):
    return json.loads(renderer.read_text_exact(FIXTURES_ROOT / name / "meta.json"))


def case(name):
    return renderer.Case(FIXTURES_ROOT / name)


def golden(name, path):
    return renderer.read_text_exact(FIXTURES_ROOT / name / path)


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
            yield path, "".join(renderer.decode_chunks(case_meta(path.parent.name), records))
        elif path.suffix == ".json":
            # The JSON file's own text escapes the separators; the decoded values carry them plainly.
            yield path, json.dumps(json.loads(raw), ensure_ascii=False)


class TerminalFixtureLayoutTests(unittest.TestCase):
    def test_every_recorded_and_edge_case_is_present(self):
        names = {path.name for path in case_dirs()}
        self.assertEqual(set(LIVE_CASES) - names, set(), "missing live recordings")
        self.assertEqual(set(EDGE_CASES) - names, set(), "missing edge cases")

    def test_meta_describes_the_corpus(self):
        for path in case_dirs():
            meta = case_meta(path.name)
            records = renderer.read_chunk_records(path / "chunks.jsonl")
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
            records = renderer.read_chunk_records(FIXTURES_ROOT / name / "chunks.jsonl")
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
            records = renderer.read_chunk_records(FIXTURES_ROOT / name / "chunks.jsonl")
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
                             len(renderer.read_chunk_records(FIXTURES_ROOT / name / "chunks.jsonl")), name)
            self.assertNotIn("chars_before", scrub, f"{name}: cumulative block keeps a stale char count")
            last_run = scrub.get("last_run") or {}
            self.assertIsInstance(last_run.get("replacements"), int, name)
            self.assertIsInstance(last_run.get("chars_before"), int, name)
            self.assertIsInstance(last_run.get("chars_after"), int, name)
            for rule in list(scrub.get("rules") or []) + list(last_run.get("rules") or []):
                self.assertRegex(str(rule.get("label")), allowed_labels, name)
                self.assertIsInstance(rule.get("count"), int, name)


class TerminalFixtureOracleTests(unittest.TestCase):
    def test_the_renderer_uses_the_oracles_objects(self):
        # `oracle-to-tools`: the pyte model left the hub in step 3b, so the renderer's objects are the
        # oracle module's own, imported (never copied) by the renderer and the recorder alike.
        self.assertIs(renderer.PyteOracleSession, pyte_oracle.PyteOracleSession)
        self.assertIs(renderer.trim_history_text, pyte_oracle.trim_history_text)
        self.assertIs(renderer.OSC_COLOR_QUERY_RE, pyte_oracle.OSC_COLOR_QUERY_RE)
        self.assertIs(renderer.resume_banner, session_manager.resume_banner)
        self.assertIs(renderer.strip_osc_terminal_replies, session_manager.strip_osc_terminal_replies)
        self.assertIs(renderer.OSC_TERMINAL_REPLY_RE, session_manager.OSC_TERMINAL_REPLY_RE)
        self.assertIs(renderer.HISTORY_TRIM_SLACK_BYTES, session_manager.HISTORY_TRIM_SLACK_BYTES)
        self.assertIs(renderer.RESUME_SCROLLBACK_MAX_BYTES,
                      session_manager.RESUME_SCROLLBACK_MAX_BYTES)
        self.assertEqual(pyte_oracle.PyteOracleSession.feed_output.__module__, "tools.pyte_oracle")
        # The screen and stream the goldens are rendered on are the oracle's, not look-alikes.
        session = renderer.new_session("generic", 60, 12)
        self.assertIsInstance(session.screen, pyte_oracle.HubTerminalScreen)
        self.assertIsInstance(session.stream, pyte_oracle.HubTerminalStream)
        # ... and the hub itself carries no pyte state any more: the terminal is the core's.
        for name in ("HubTerminalScreen", "HubTerminalStream", "trim_history_text", "OSC_COLOR_QUERY_RE"):
            self.assertFalse(hasattr(session_manager, name), name)

    def test_the_replay_golden_is_the_hubs_own_attach_frame(self):
        session = renderer.new_session("generic", 60, 12)
        session.raw_history = ["a\x1b]11;?\x07c"]
        direct, queue = session.attach_ws_queue()
        session.detach_ws_queue(queue)
        self.assertEqual(renderer.replay_frame(session), direct)
        self.assertEqual(direct, "ac")

    def test_the_seam_names_the_hub_uses_are_the_ones_the_renderer_calls(self):
        # Both were literals before step 3a; now each is a seam call, so they are pinned by name.
        diagnostic = " ".join(inspect.getsource(
            session_manager.HubSessionManager._capture_submit_diagnostic).split())
        self.assertIn("session.read_raw_tail(SUBMIT_DIAGNOSTIC_TAIL_CHARS)", diagnostic)
        self.assertEqual(renderer.RAW_TAIL_CHARS, session_manager.SUBMIT_DIAGNOSTIC_TAIL_CHARS)
        resumed = " ".join(inspect.getsource(
            session_manager.HubSessionManager._make_resumed_session).split())
        self.assertIn('seed = f"{archived}{resume_banner(count)}" if archived else resume_banner(count)',
                      resumed)
        self.assertIn("sess.seed_history(seed)", resumed)
        self.assertEqual(renderer.resume_seed("old", 2), "old" + session_manager.resume_banner(2))
        self.assertEqual(renderer.resume_seed("", 2), session_manager.resume_banner(2))


class TerminalFixtureGoldenTests(unittest.TestCase):
    def test_goldens_rerender_identically_with_todays_code(self):
        failures = []
        for path in case_dirs():
            failures.extend(renderer.check_case(path))
        self.assertEqual(failures, [], "goldens differ from a fresh render:\n" + "\n".join(failures))

    def test_every_session_case_has_its_replay_frame(self):
        for path in case_dirs():
            meta = case_meta(path.name)
            if meta["kind"] == "session":
                self.assertTrue((path / "replay.txt").is_file(), path.name)
                self.assertTrue((path / renderer.RESUMED_DIR / "replay.txt").is_file(), path.name)

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
        self.assertEqual(replay, pyte_oracle.OSC_COLOR_QUERY_RE.sub("", raw))

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
        # The in-loop result: the ring is trim(seed + chunks up to the trigger) plus what followed.
        seed = renderer.trigger_seed(text)
        joined = seed + "".join(chunks)
        expected = (pyte_oracle.trim_history_text(seed + "".join(chunks[:index + 1]), max_bytes)
                    + "".join(chunks[index + 1:]))
        self.assertEqual(golden(name, "raw.txt"), expected)
        # The collapse cuts the front only, so the submit-diagnostic tail is unchanged.
        self.assertEqual(golden(name, "raw_tail.txt"), joined[-renderer.RAW_TAIL_CHARS:])
        # Every chunk after the collapse appends one entry again; nothing else merges them.
        self.assertEqual(record["final_entries"], 1 + len(chunks) - index - 1, record)
        self.assertEqual(record["final_bytes"], len(expected.encode("utf-8")), record)

    def test_the_trigger_corpus_matches_the_generator_its_meta_describes(self):
        # Step 2's Rust test regenerates chunks.jsonl from the same block; it must be exact.
        meta = case_meta("edge-w9-inloop-trigger")
        generated = renderer.generated_chunks(meta["generator"])
        self.assertEqual(generated, case("edge-w9-inloop-trigger").chunks)
        total = len("".join(generated).encode("utf-8"))
        self.assertGreaterEqual(total, 600 * 1024)
        self.assertLessEqual(total, 640 * 1024)

    def test_the_w9_banner_cases_pin_the_hub_rule(self):
        prefix = pyte_oracle.HISTORY_TRUNCATED_PREFIX
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
            # cut is at or past that end.  Only the cut is computed here; the trim is the hub's.
            raw_cut = len(raw) - budget
            marker_start = len(text[:found + 2].encode("utf-8"))
            banner_end = raw.index(b"\n", marker_start) + 1
            self.assertEqual(banner_kept, raw_cut < banner_end, name)
            over = len(trimmed.encode("utf-8")) - meta["max_bytes"]
            if name == "edge-w9-trim-banner-inside":
                # The cut is inside the banner line, so keeping it goes over the cap by less than it.
                self.assertGreater(raw_cut, banner_start, name)
                self.assertGreater(over, 0, name)
                self.assertLess(over, pyte_oracle.HISTORY_BANNER_LINE_MAX_BYTES, name)
            else:
                self.assertLessEqual(over, 0, name)
            if name == "edge-w9-banner-lead-crlf":
                self.assertTrue(trimmed.startswith(prefix + "\r\n"), name)

    def test_a_budget_below_the_prefix_returns_the_prefix(self):
        name = "edge-w9-trim-budget-zero"
        self.assertEqual(golden(name, "trimmed.txt"), pyte_oracle.HISTORY_TRUNCATED_PREFIX)


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