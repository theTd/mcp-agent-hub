"""The hub's client contract with the Rust terminal core, through ``terminal_core.py``.

Covers docs/rust-terminal-core.md §5.2 (calls and deadlines), §5.3 (drain clears its flags, the 50 ms
notify window) and §7.3 (the missing-module message). The live cases need the built extension beside
this file (``hub_terminal_core.pyd``); without it they are skipped rather than failed.
"""
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
EXTENSION = HERE / "hub_terminal_core.pyd"

try:
    import terminal_core
    from terminal_core import Core
except ImportError as _import_error:  # pragma: no cover - exercised only on an unbuilt tree
    terminal_core = None
    Core = None
    IMPORT_ERROR = _import_error
else:
    IMPORT_ERROR = None


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
    @unittest.skipIf(Core is None, f"hub_terminal_core is not built: {IMPORT_ERROR}")
    def test_wrapper_reexports_the_extension_identity(self):
        self.assertIs(terminal_core.Core, Core)
        self.assertEqual(terminal_core.ABI, "abi3-py311")
        self.assertTrue(terminal_core.__version__)


@unittest.skipIf(Core is None, f"hub_terminal_core is not built: {IMPORT_ERROR}")
class CoreClientTests(unittest.TestCase):
    def setUp(self):
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
            if self.core.write(self.session, payload, 100) == 0:
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
            started = time.perf_counter()
            accepted = self.core.write(self.session, payload, 150)
            elapsed = time.perf_counter() - started
            after = ticks[0]
        finally:
            stop.set()
            thread.join(2)
        self.assertEqual(accepted, 0)
        self.assertGreaterEqual(elapsed, 0.10, "the call was expected to wait for its deadline")
        self.assertGreater(after, before, "another Python thread made no progress: the GIL was held")

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


if __name__ == "__main__":
    unittest.main()
