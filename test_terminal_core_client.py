"""The hub's client contract with the Rust terminal core, through ``terminal_core.py``.

Covers docs/rust-terminal-core.md §5.2 (calls, deadlines and `retire`), §5.3 (drain clears its flags, the 50 ms notify window, natural
exits) and §7.3 (the missing-module message).

The live cases need the built extension beside this file (``hub_terminal_core.pyd``). They **fail**, not
skip, when it is missing or broken: a green suite on an unbuilt tree is a gate that cannot see a broken
build (review nit).
"""
import base64
import importlib.util
import os
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
EXTENSION = HERE / "hub_terminal_core.pyd"

try:
    import terminal_core
    from terminal_core import Core
except ImportError as _import_error:  # exercised only on an unbuilt tree, and then the live tests fail
    terminal_core = None
    Core = None
    IMPORT_ERROR = _import_error
else:
    IMPORT_ERROR = None


def live_core(test):
    """Marks a test as needing the extension: a missing or broken build fails it instead of skipping."""

    def wrapper(self, *args, **kwargs):
        if Core is None:
            self.fail(f"hub_terminal_core is not built or does not load: {IMPORT_ERROR}")
        return test(self, *args, **kwargs)

    wrapper.__name__ = test.__name__
    wrapper.__doc__ = test.__doc__
    return wrapper


class MissingModuleMessageTests(unittest.TestCase):
    def test_message_carries_the_expected_path_and_both_build_commands(self):
        path = HERE / "terminal_core.py"
        spec = importlib.util.spec_from_file_location("terminal_core_missing_probe", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"hub_terminal_core": None}):
            with self.assertRaises(ImportError) as caught:
                spec.loader.exec_module(module)
        self.assertEqual(
            str(caught.exception).splitlines(),
            [
                f"终端核心未构建：缺少 hub_terminal_core（预期 {EXTENSION}）。",
                "构建：cargo build --release --manifest-path terminal-core\\Cargo.toml",
                "      Copy-Item terminal-core\\target\\release\\hub_terminal_core.dll hub_terminal_core.pyd",
            ],
        )


class WrapperSurfaceTests(unittest.TestCase):
    @live_core
    def test_wrapper_reexports_the_extension_identity(self):
        self.assertIs(terminal_core.Core, Core)
        self.assertIs(terminal_core.strip_osc_terminal_replies,
                      sys.modules["hub_terminal_core"].strip_osc_terminal_replies)
        self.assertIn("strip_osc_terminal_replies", terminal_core.__all__)
        self.assertEqual(terminal_core.ABI, "abi3-py311")
        self.assertTrue(terminal_core.__version__)


class CoreClientTests(unittest.TestCase):
    def setUp(self):
        if Core is None:
            self.fail(f"hub_terminal_core is not built or does not load: {IMPORT_ERROR}")
        self.notify_calls = []
        self.logs = []
        self.core = Core(notify=lambda: self.notify_calls.append(time.monotonic()), log=self.logs.append)
        self.session = "client-test"

    def tearDown(self):
        try:
            self.core.kill_all(2000)
        finally:
            self.core.shutdown()

    def spawn(self, argv=None, cols=100, rows=30, session=None):
        return self.core.spawn(session or self.session, argv or ["cmd.exe"], cols=cols, rows=rows)

    def wait_for(self, needle, timeout=10.0, session=None):
        session = session or self.session
        end = time.time() + timeout
        tail = ""
        while time.time() < end:
            tail = self.core.raw_tail(session, 64 * 1024)
            if needle in tail:
                return tail
            time.sleep(0.02)
        self.fail(f"{needle!r} never reached raw_tail; tail was {tail[-400:]!r}")

    def drain_until(self, predicate, timeout=5.0):
        end = time.time() + timeout
        seen = []
        while time.time() < end:
            for record in self.core.drain():
                seen.append(record)
                if predicate(record):
                    return record
            time.sleep(0.02)
        self.fail(f"no drain record matched; records seen: {seen}")

    def test_spawn_echo_resize_kill_and_drain(self):
        reply = self.spawn()
        self.assertGreater(reply["pid"], 0)
        self.assertEqual((reply["cols"], reply["rows"]), (100, 30))
        self.assertGreater(reply["elapsed_ms"], 0)
        self.wait_for(">", timeout=15.0)

        payload = "echo hub-core-ok\r\n"
        self.assertEqual(self.core.write(self.session, payload), len(payload))
        self.wait_for("hub-core-ok")

        self.assertEqual(self.core.resize(self.session, 120, 40), (120, 40))
        with self.assertRaises(ValueError):
            self.core.resize(self.session, 19, 40)
        with self.assertRaises(ValueError):
            self.core.resize(self.session, 120, 4)

        alive = self.drain_until(lambda record: record["alive"] and record["bytes_total"] > 0)
        self.assertGreater(alive["last_output_at"], 0)

        exit_code = self.core.kill(self.session, 2000)
        self.assertIsNotNone(exit_code)
        dead = self.drain_until(lambda record: not record["alive"])
        self.assertEqual(dead["session_id"], self.session)
        self.assertEqual(dead["exit_code"], exit_code)
        self.assertFalse(dead["crashed"])
        # ConPTY can flush the console's remaining bytes after the process is gone, which dirties the
        # session once more; once that has settled, the flags stay clear and a drain returns nothing.
        settle_until = time.time() + 2.0
        while True:
            records = self.core.drain()
            if not records:
                break
            self.assertTrue(
                all(record["session_id"] == self.session and not record["alive"] for record in records),
                records,
            )
            self.assertLess(time.time(), settle_until, "the session never went quiet after kill")
            time.sleep(0.02)
        self.assertEqual(self.core.drain(), [], "a second drain must return nothing new")

    def test_natural_exit_is_reported_without_kill(self):
        """B1: a child that exits by itself is reported, with its code, without `kill`."""
        self.spawn(["cmd.exe", "/c", "echo natural-exit & exit 7"])
        # The 1 s bound belongs to the death report, not to the spawn: on a loaded machine `cmd.exe`
        # can take a while to start and to print, and that delay is not the reaper's. Wait for the
        # first sign of life generously, then hold the report itself to a second.
        self.drain_until(
            lambda record: record["session_id"] == self.session
            and (record["bytes_total"] > 0 or not record["alive"]),
            timeout=10.0,
        )
        started = time.monotonic()
        dead = self.drain_until(
            lambda record: record["session_id"] == self.session and not record["alive"],
            timeout=1.0,
        )
        self.assertLess(time.monotonic() - started, 1.0, "the death record took more than 1 s after output")
        self.assertEqual(dead["exit_code"], 7, dead)
        self.assertFalse(dead["crashed"], dead)
        self.assertGreater(dead["bytes_total"], 0, dead)
        # The reader published after its last bytes, so the output is already in the buffer.
        self.assertIn("natural-exit", self.core.raw_tail(self.session, 4096))
        # A naturally exited id is spawnable again, and the old session is released.
        again = self.spawn(["cmd.exe", "/c", "exit", "0"])
        self.assertGreater(again["pid"], 0)
        dead = self.drain_until(lambda record: not record["alive"], timeout=1.0)
        self.assertEqual(dead["exit_code"], 0, dead)

    def test_kill_during_a_burst_keeps_every_byte(self):
        """B1: a `kill` that lands while the reader is still filing output must not publish a death record
        that is missing bytes — the same ordering a natural-exit teardown needs (the Rust cases pin that
        interleaving exactly)."""
        burst = "for /L %i in (1,1,4000) do @echo 0123456789012345678901234567890123456789"
        self.spawn(["cmd.exe", "/c", burst])
        # As soon as bytes are being filed, kill: the record must wait for the reader's last byte.
        first = self.drain_until(lambda record: record["bytes_total"] > 0, timeout=10.0)
        self.assertGreater(first["bytes_total"], 0, first)
        self.core.kill(self.session, 2000)
        dead = self.drain_until(lambda record: not record["alive"], timeout=3.0)
        # Nothing can arrive after the reader's last byte, so the record equals the settled total and no
        # later record reports more.
        time.sleep(0.3)
        settled = self.core.stats()["bytes_total"]
        self.assertEqual(dead["bytes_total"], settled, dead)
        for record in self.core.drain():
            self.assertLessEqual(record["bytes_total"], dead["bytes_total"], record)

    def test_an_oversize_write_is_accepted_whole_then_times_out(self):
        """SF2: a write larger than the queue cap (1 MiB) is accepted whole when nothing is queued — a
        paste behaves as it did under pywinpty — and a second one while the first waits times out."""
        self.spawn(["cmd.exe", "/c", "ping -n 60 127.0.0.1"])
        time.sleep(0.5)
        payload = "x" * (2 * 1024 * 1024)
        self.assertEqual(self.core.write(self.session, payload, 200), len(payload))
        with self.assertRaises(TimeoutError) as caught:
            self.core.write(self.session, payload, 200)
        self.assertIn("nothing queued", str(caught.exception))

    def test_unknown_session_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.core.raw_tail("no-such-session", 16)
        with self.assertRaises(KeyError):
            self.core.write("no-such-session", "x")
        with self.assertRaises(KeyError):
            self.core.resize("no-such-session", 80, 24)
        with self.assertRaises(KeyError):
            self.core.kill("no-such-session", 100)

    def test_gil_is_released_while_write_blocks(self):
        # `ping` never reads stdin, so the console input pipe backs up and the writer queue fills.
        self.spawn(["cmd.exe", "/c", "ping -n 60 127.0.0.1"])
        time.sleep(0.5)
        payload = "x" * 64 * 1024
        blocked = False
        for _ in range(96):
            try:
                self.core.write(self.session, payload, 100)
            except TimeoutError:
                blocked = True
                break
        self.assertTrue(blocked, "the writer queue never filled")

        ticks = [0]
        stop = threading.Event()

        def spin():
            while not stop.is_set():
                ticks[0] += 1

        thread = threading.Thread(target=spin, daemon=True)
        thread.start()
        try:
            time.sleep(0.05)
            before = ticks[0]
            # The console drains at most one chunk between attempts, so a retry loop is what makes this
            # deterministic: the first call that has to wait for the deadline is the one measured.
            caught = None
            window = time.perf_counter() + 5.0
            while time.perf_counter() < window:
                started = time.perf_counter()
                try:
                    self.core.write(self.session, payload, 150)
                except TimeoutError as error:
                    caught = (error, time.perf_counter() - started)
                    break
            after = ticks[0]
        finally:
            stop.set()
            thread.join(2)
        self.assertIsNotNone(caught, "the writer queue never stayed full for a whole deadline")
        error, elapsed = caught
        # §5.2 (SF4): a full queue at the deadline is a TimeoutError with nothing queued, not a silent 0.
        self.assertIn("nothing queued", str(error))
        self.assertGreaterEqual(elapsed, 0.10, "the call was expected to wait for its deadline")
        self.assertGreater(after, before, "another Python thread made no progress: the GIL was held")
        # An empty write is not a timeout.
        self.assertEqual(self.core.write(self.session, ""), 0)

    def test_notify_is_rate_limited_to_the_drain_interval(self):
        self.spawn()
        self.wait_for(">", timeout=15.0)
        self.core.drain()

        # A long print loop keeps bytes flowing for the whole measurement window.
        self.core.write(
            self.session,
            "for /L %i in (1,1,2000000) do @echo 0123456789012345678901234567890123456789\r\n",
        )
        time.sleep(0.05)
        bytes_before = self.core.stats()["bytes_total"]
        start_index = len(self.notify_calls)
        time.sleep(1.0)
        calls = len(self.notify_calls) - start_index
        bytes_after = self.core.stats()["bytes_total"]

        self.assertGreater(bytes_after, bytes_before, "the session stopped printing during the window")
        self.assertGreaterEqual(calls, 1, "the core never woke the hub while output was flowing")
        self.assertLessEqual(calls, 22, f"{calls} notify calls in one second (expected about 20)")

    def test_spawn_failure_is_a_runtime_error_with_the_win32_message(self):
        with self.assertRaises(RuntimeError) as caught:
            self.core.spawn("missing-command", ["hub-terminal-core-no-such-command"], cols=80, rows=24)
        self.assertIn("hub-terminal-core-no-such-command", str(caught.exception))

        with self.assertRaises(RuntimeError) as caught:
            self.core.spawn("bad-cwd", ["cmd.exe"], cwd="Z:\\hub-terminal-core-no-such-dir", cols=80, rows=24)
        self.assertIn("CreateProcessW", str(caught.exception))

    def test_respawn_of_a_dead_session_replaces_it(self):
        first = self.spawn()
        self.core.kill(self.session, 2000)
        self.drain_until(lambda record: not record["alive"])
        second = self.spawn()
        self.assertNotEqual(first["pid"], second["pid"])
        with self.assertRaises(ValueError):
            self.spawn()

    def test_retire_keeps_the_exit_record_and_starts_fresh(self):
        self.assertFalse(self.core.retire("no-such-session"))

        self.spawn()
        self.wait_for(">", timeout=15.0)
        self.core.write(self.session, "echo RETIRE-MARKER\r\n")
        self.wait_for("RETIRE-MARKER")
        with self.assertRaises(ValueError) as caught:
            self.core.retire(self.session)
        self.assertIn("still running", str(caught.exception))

        exit_code = self.core.kill(self.session, 2000)
        self.assertIsNotNone(exit_code)
        with self.assertRaises(ValueError) as caught:
            self.core.retire(self.session)
        self.assertIn("exit record", str(caught.exception))

        dead = self.drain_until(lambda record: record["session_id"] == self.session and not record["alive"])
        self.assertEqual(dead["exit_code"], exit_code)
        self.assertTrue(self.core.retire(self.session))
        with self.assertRaises(KeyError):
            self.core.screen(self.session, 20)
        self.assertEqual(self.core.stats()["sessions"], 0)

        self.spawn()
        self.assertNotIn("RETIRE-MARKER", self.core.screen(self.session, 20)["text"])
        self.assertNotIn("RETIRE-MARKER", self.core.replay(self.session))

    def test_screen_scrollback_raw_and_replay_of_a_live_session(self):
        self.spawn(cols=100, rows=30)
        self.wait_for(">", timeout=15.0)
        self.core.write(self.session, "echo hub-screen-ok\r\n")
        self.wait_for("hub-screen-ok")

        deadline = time.time() + 10.0
        screen = None
        while time.time() < deadline:
            screen = self.core.screen(self.session, 80)
            if "hub-screen-ok" in screen["text"]:
                break
            time.sleep(0.02)
        self.assertIn("hub-screen-ok", screen["text"])
        self.assertEqual((screen["cols"], screen["rows"]), (100, 30))
        self.assertGreaterEqual(screen["rev"], 1)
        self.assertGreaterEqual(screen["cursor_x"], 0)
        self.assertGreaterEqual(screen["cursor_y"], 0)

        scrollback = self.core.scrollback(self.session, 250)
        self.assertIsInstance(scrollback["text"], str)
        self.assertGreater(scrollback["rev"], 0)

        raw = self.core.raw(self.session)
        self.assertIn("hub-screen-ok", raw["text"])
        self.assertGreaterEqual(raw["bytes"], len(raw["text"].encode("utf-8")))
        self.assertGreater(raw["rev"], 0)
        replay = self.core.replay(self.session)
        self.assertIn("hub-screen-ok", replay)
        self.assertNotIn("\x1b]11;?", replay)

    def test_snapshot_all_reports_the_live_session(self):
        self.spawn()
        self.wait_for(">", timeout=15.0)
        self.core.write(self.session, "echo snapshot-ok\r\n")
        self.wait_for("snapshot-ok")
        snapshots = self.core.snapshot_all(250, 512 * 1024)
        mine = [snapshot for snapshot in snapshots if snapshot["session_id"] == self.session]
        self.assertEqual(len(mine), 1, snapshots)
        self.assertIn("snapshot-ok", mine[0]["screen_text"])
        self.assertTrue(mine[0]["alive"])
        self.assertGreater(mine[0]["raw_rev"], 0)

    def test_the_drain_record_carries_the_raw_revision(self):
        self.core.attach_text(self.session, cols=40, rows=8)
        self.core.feed(self.session, "drain-raw-revision\r\n")
        record = self.drain_until(
            lambda entry: entry["session_id"] == self.session and entry["raw_rev"] > 0
        )
        self.assertGreater(record["raw_rev"], 0)
        self.assertEqual(record["raw_rev"], self.core.raw(self.session)["rev"])


    def test_seed_history_precedes_the_first_output(self):
        self.core.spawn(
            self.session,
            ["cmd.exe"],
            cols=80,
            rows=24,
            seed_history="RESUMED-SEED\r\n",
        )
        raw = self.core.raw(self.session)
        self.assertTrue(raw["text"].startswith("RESUMED-SEED"), raw["text"][:120])
        self.core.kill(self.session, 2000)

    def test_spawn_flags_bound_the_ring_and_the_history(self):
        self.core.spawn(
            self.session,
            ["cmd.exe"],
            cols=80,
            rows=24,
            flags={"raw_cap_bytes": 64, "raw_slack_bytes": 0, "history_rows": 10},
        )
        self.wait_for(">", timeout=15.0)
        line = "echo " + "0123456789" * 20 + "\r\n"
        self.core.write(self.session, line)
        deadline = time.time() + 10.0
        raw = self.core.raw(self.session)
        while time.time() < deadline and "较早的输出已截断" not in raw["text"]:
            time.sleep(0.02)
            raw = self.core.raw(self.session)
        self.assertIn("较早的输出已截断", raw["text"])
        # A trimmed ring may exceed its cap by less than the banner line (`HISTORY_BANNER_LINE_MAX_BYTES`).
        self.assertLessEqual(raw["bytes"], 64 + 128, raw["bytes"])
        self.assertLessEqual(len(self.core.scrollback(self.session, 250)["text"].splitlines()), 250)


class TextSessionTests(unittest.TestCase):
    """`attach_text` + `feed`: a screen without a process (§5.2), and the character-count tail."""

    def setUp(self):
        if Core is None:
            self.fail(f"hub_terminal_core is not built or does not load: {IMPORT_ERROR}")
        self.notify_calls = []
        self.core = Core(notify=lambda: self.notify_calls.append(time.monotonic()), log=lambda line: None)
        self.session = "virtual"

    def tearDown(self):
        try:
            self.core.kill_all(2000)
        finally:
            self.core.shutdown()

    def test_feed_renders_and_the_tail_never_splits_a_character(self):
        reply = self.core.attach_text(
            self.session, cols=40, rows=6, seed_history="ARCHIVED\r\n"
        )
        self.assertEqual((reply["cols"], reply["rows"]), (40, 6))
        first = self.core.feed(self.session, "hello 世界\r\nsecond line")
        self.assertGreaterEqual(first, 1)
        screen = self.core.screen(self.session, 10)
        self.assertIn("hello 世界", screen["text"])
        self.assertIn("second line", screen["text"])

        self.assertTrue(
            self.core.raw(self.session)["text"].startswith("ARCHIVED"),
            "attach_text's seed_history is the ring's first entry",
        )
        second = self.core.feed(self.session, "\r\n第三行")
        self.assertGreater(second, first)
        self.assertIn("第三行", self.core.screen(self.session, 10)["text"])
        # raw_tail is a character count, as the hub's "".join(raw_history)[-65536:] always was: the tail
        # of a CJK string comes back whole instead of being cut inside a three-byte character.
        self.assertEqual(self.core.raw_tail(self.session, 3), "第三行")
        self.assertEqual(self.core.raw_tail(self.session, 1), "行")
        self.assertIn("hello 世界", self.core.raw(self.session)["text"])
        self.assertIn("hello 世界", self.core.replay(self.session))

        record = self.drain_until(lambda entry: entry["session_id"] == self.session)
        self.assertGreaterEqual(record["screen_rev"], second)
        self.assertIsNone(record["screen_text"], "a session that is not watched carries no screen")
        self.assertTrue(record["alive"])

        self.core.watch_screen(self.session, 10, 0)
        self.core.feed(self.session, "\r\nwatched")
        watched = self.drain_until(
            lambda entry: entry["session_id"] == self.session and entry["screen_text"]
        )
        self.assertIn("watched", watched["screen_text"])
        self.core.unwatch(self.session)

        # A text session ends without an exit code, and its state stays readable.
        self.assertIsNone(self.core.kill(self.session, 100))
        dead = self.drain_until(lambda entry: entry["session_id"] == self.session and not entry["alive"])
        self.assertIsNone(dead["exit_code"])
        self.assertIn("watched", self.core.screen(self.session, 10)["text"])

    def test_unknown_text_session_raises_key_error(self):
        for call in (
            lambda: self.core.feed("no-such-session", "x"),
            lambda: self.core.screen("no-such-session", 10),
            lambda: self.core.scrollback("no-such-session", 10),
            lambda: self.core.raw("no-such-session"),
            lambda: self.core.replay("no-such-session"),
            lambda: self.core.watch_screen("no-such-session", 10, 1),
            lambda: self.core.unwatch("no-such-session"),
        ):
            with self.assertRaises(KeyError):
                call()

    def drain_until(self, predicate, timeout=5.0):
        end = time.time() + timeout
        seen = []
        while time.time() < end:
            for record in self.core.drain():
                seen.append(record)
                if predicate(record):
                    return record
            time.sleep(0.02)
        self.fail(f"no drain record matched; records seen: {seen}")


class ReplyStripTests(unittest.TestCase):
    def test_strip_osc_terminal_replies_is_a_function(self):
        if Core is None:
            self.fail(f"hub_terminal_core is not built or does not load: {IMPORT_ERROR}")
        strip = terminal_core.strip_osc_terminal_replies
        self.assertEqual(strip("\x1b]11;rgb:1/1/1\x07hi"), "hi")
        self.assertEqual(strip("\x1b]52;c;YWJj\x07"), "")
        self.assertEqual(strip("\x1b]110;rgb:0/0/0\x1b\\x"), "x")
        self.assertEqual(strip("plain"), "plain")
        self.assertEqual(strip("\x1b]10;rgb:1/1"), "\x1b]10;rgb:1/1")


# B2: `shutdown`/`Drop` must not wait for the GIL while they hold it. A regression hangs the process, so
# each case runs in a child interpreter that the parent kills at a hard timeout.
GIL_PROBE = textwrap.dedent(
    """
    import gc
    import sys
    import time

    sys.path.insert(0, sys.argv[1])
    import terminal_core

    mode = sys.argv[2]
    core = terminal_core.Core(notify=lambda: None, log=lambda line: None)
    core.spawn(
        "printing",
        ["cmd.exe", "/c", "for /L %i in (1,1,1000000) do @echo 0123456789012345678901234567890123456789"],
    )
    time.sleep(0.5)  # a session is printing, so the ticker is calling back
    sys.setswitchinterval(60)
    # Hold the GIL with a busy loop, then make the call that used to join the ticker while holding it.
    end = time.perf_counter() + 0.05
    while time.perf_counter() < end:
        pass
    started = time.perf_counter()
    if mode == "shutdown":
        core.shutdown()
    else:
        del core
        gc.collect()
    print("elapsed=%.3f" % (time.perf_counter() - started))
    """
)


class GilTeardownTests(unittest.TestCase):
    def run_probe(self, mode):
        result = subprocess.run(
            [sys.executable, "-B", "-c", GIL_PROBE, str(HERE), mode],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        elapsed = float(result.stdout.strip().split("=")[1])
        self.assertLess(elapsed, 3.0, f"{mode} took {elapsed:.3f} s while the GIL was held")
        return elapsed

    @live_core
    def test_shutdown_returns_while_another_thread_holds_the_gil(self):
        self.run_probe("shutdown")

    @live_core
    def test_dropping_the_last_reference_returns_while_the_gil_is_held(self):
        self.run_probe("drop")


class _Refused(Exception):
    """The handshake answered with an HTTP error instead of `101 Switching Protocols`."""

    def __init__(self, status, head):
        super().__init__(f"{status}: {head.splitlines()[0] if head else ''}")
        self.status = status


class _WsClient:
    """Just enough WebSocket for a console pane's two directions (RFC 6455, no extensions).

    stdlib only: the suite may not install anything (RULES.md), and this is the shape of what the
    browser does anyway — a masked text frame out, unmasked frames and a close code in.
    """

    def __init__(self, url, timeout=5.0):
        parsed = urlsplit(url)
        if parsed.scheme != "ws":
            raise AssertionError(f"not a ws url: {url}")
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        self.opened = self._read_head()
        status = int(self.opened.split(" ", 2)[1])
        if status != 101:
            self.sock.close()
            raise _Refused(status, self.opened)

    def _read_head(self):
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError("the handshake ended without a response")
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        return head.decode("latin-1")

    def _read_exact(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("the socket closed mid-frame")
            self.buffer += chunk
        taken, self.buffer = self.buffer[:count], self.buffer[count:]
        return taken

    def read_message(self, timeout=5.0):
        """One `(opcode, payload)` frame; the server's frames are never masked."""
        self.sock.settimeout(timeout)
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if second & 0x80:
            raise AssertionError("a server frame must not be masked")
        return opcode, self._read_exact(length)

    def read_text_until(self, needle, timeout=5.0):
        end = time.time() + timeout
        seen = ""
        while needle not in seen:
            remaining = end - time.time()
            if remaining <= 0:
                raise AssertionError(f"{needle!r} never arrived; saw {seen[-400:]!r}")
            opcode, payload = self.read_message(remaining)
            if opcode == 1:
                seen += payload.decode("utf-8", "replace")
            elif opcode == 8:
                raise AssertionError(f"closed before {needle!r}: {payload!r}")
        return seen

    def send_text(self, text):
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def read_close(self, timeout=5.0):
        end = time.time() + timeout
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                raise AssertionError("the socket was never closed")
            opcode, payload = self.read_message(remaining)
            if opcode == 8:
                return struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TerminalSocketTests(unittest.TestCase):
    """§6.4 through the extension: the listener, the ticket, the wire order, input and the close.

    This is the surface step 4b moves the hub's console onto (`ws_port`/`ws_ticket`/`close_sockets`),
    so the wire contract is proven from Python over a real loopback socket, not only from Rust.
    """

    def setUp(self):
        if Core is None:
            self.fail(f"hub_terminal_core is not built or does not load: {IMPORT_ERROR}")
        self.logs = []
        self.core = Core(log=self.logs.append)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        try:
            self.core.kill_all(2000)
        finally:
            self.core.shutdown()

    def connect(self, session):
        client = _WsClient(self.core.ws_ticket(session)["url"])
        self.clients.append(client)
        return client

    def drain_requests(self, timeout=5.0):
        """Every `resize_requests` entry the drains carry within `timeout`."""
        resizes = []
        end = time.time() + timeout
        while time.time() < end and not resizes:
            for record in self.core.drain():
                resizes.extend(record["resize_requests"])
            time.sleep(0.02)
        return resizes

    @live_core
    def test_a_real_socket_serves_a_session_end_to_end(self):
        self.core.attach_text("live", cols=40, rows=8)
        self.assertGreater(self.core.ws_port, 0)
        self.assertTrue(self.core.ws_listening())

        client = self.connect("live")
        seen = client.read_text_until("replay_end")
        self.assertIn("replay_end", seen)
        self.core.feed("live", "hello socket\r\n")
        self.assertIn("hello socket", client.read_text_until("hello socket"))

        # The resize travels on the drain record; the input frame on a text session is consumed and
        # discarded (DSH, its only owner, was dropped).
        client.send_text('{"type":"resize","cols":100,"rows":40,"propagate":true}')
        client.send_text('{"type":"input","data":"typed"}')
        resizes = self.drain_requests()
        self.assertEqual(resizes, [{"cols": 100, "rows": 40, "propagate": True}])

        self.assertEqual(self.core.close_sockets("live"), 1)
        self.assertEqual(client.read_close(), 1000, "the hub's close is 1000")
        end = time.time() + 5.0
        while self.core.socket_count() and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(self.core.socket_count(), 0)

    @live_core
    def test_the_ticket_is_checked_at_the_handshake_and_never_logged(self):
        self.core.attach_text("ticketed", cols=40, rows=8)
        url = self.core.ws_ticket("ticketed")["url"]
        token = url.split("ticket=", 1)[1]
        tampered = url[:-1] + ("A" if url[-1] != "A" else "B")
        with self.assertRaises(_Refused) as caught:
            _WsClient(tampered)
        self.assertEqual(caught.exception.status, 401, "a bad ticket is an HTTP 401, not a socket")

        client = _WsClient(url)
        self.clients.append(client)
        client.read_text_until("replay_end")
        with self.assertRaises(_Refused) as caught:
            _WsClient(url)
        self.assertEqual(caught.exception.status, 401, "a ticket works once")
        self.assertEqual(self.core.socket_count(), 1, "only the honest socket exists")

        logged = "\n".join(self.logs)
        self.assertIn("401", logged, "the refused handshake was not logged")
        self.assertNotIn(token, logged, "the ticket reached a log line")


if __name__ == "__main__":
    unittest.main()
