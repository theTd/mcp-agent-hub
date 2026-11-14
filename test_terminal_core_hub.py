"""Step 3b: the hub runs on the terminal core — real ConPTY through `HubSessionManager`.

`TerminalDrainTests` keeps the fake core (scripted records, a resume that hands its sockets over):
one failing record must not stop the others, and a socket attached before a resume keeps its queue.
`HubOnCoreTests` runs a real `cmd.exe` through the manager's session and drain tick: spawn, echo,
resize, kill, the exit event, `retire` on close and `shutdown` leaving the core's table empty.

Run: python -m unittest -v test_terminal_core_hub
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import session_manager
from fake_terminal_core import FakeTerminal
from session_manager import AgentSession, HubSessionManager


def _session(hub, session_id, sockets=None, **kwargs):
    """An `AgentSession` wired to the hub the way `create_session` wires it."""
    sess = AgentSession(
        session_id,
        "pwsh",
        "Pwsh",
        "tui",
        "probe",
        str(hub.data_dir),
        kwargs.pop("command", "cmd.exe"),
        kwargs.pop("args", []),
        core=hub.core,
        on_exit_cb=hub._exit_callback,
        sockets=sockets if sockets is not None else hub._terminal_sockets(session_id),
        **kwargs,
    )
    sess.engine = "pwsh"
    sess.status = "starting"
    hub.sessions[session_id] = sess
    return sess


class _ManagerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temporary = tempfile.TemporaryDirectory(prefix="hub-core-")
        self.root = Path(self.temporary.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(session_manager.__file__).parent,
                                         data_dir=self.root)
        self.hub.core = FakeTerminal()
        await self.hub.start()

    async def asyncTearDown(self):
        if self.hub.core is not None:
            self.hub.core.kill_all(500)
            self.hub._on_core_tick()
        await self.hub.archive_all_sessions_on_shutdown()
        self.hub.core.shutdown()
        self.temporary.cleanup()


class TerminalDrainTests(_ManagerCase):
    async def test_one_failing_record_does_not_stop_the_next_session(self):
        # §6.3: a record's failure is logged against its session and the next record still applies.
        broken = _session(self.hub, "drain-broken")
        healthy = _session(self.hub, "drain-healthy")
        self.hub.core.spawn("drain-broken", ["cmd.exe"])
        self.hub.core.spawn("drain-healthy", ["cmd.exe"])
        with patch.object(broken, "apply_core_record", side_effect=RuntimeError("record exploded")):
            self.hub.core.output("drain-broken", "第一行\r\n")
            self.hub.core.output("drain-healthy", "第二行\r\n")
            with self.assertLogs("mcp-agent-hub.session_manager", level="ERROR") as logs:
                self.hub._on_core_tick()
        self.assertTrue(any("drain-broken" in line for line in logs.output), logs.output)
        expected = self.hub.core.session("drain-healthy").rev
        self.assertEqual(healthy.raw_revision, expected)
        self.assertEqual(broken.raw_revision, 0, "the broken session's record was never applied")
        self.assertEqual(healthy.read_raw().text, "第二行\r\n")

    async def test_a_virtual_session_is_ended_in_the_core_and_retired(self):
        # `attach_text` has no process, but the core owns the id: closing the session must end it
        # there too, or `retire` could never accept the id and the ring would live for the hub's life.
        sess = _session(self.hub, "virtual-probe", command="", args=[])
        sess.start_virtual(asyncio.get_running_loop())
        sess.emit_virtual_ansi("第一行\r\n")
        self.hub._on_core_tick()
        self.assertEqual(sess.read_raw().text, "第一行\r\n")

        closed = await self.hub.close_session("virtual-probe", source="mcp")
        self.assertTrue(closed["found"])
        self.hub._on_core_tick()  # the death record schedules the retried retire
        for _ in range(100):
            if self.hub.core.stats()["sessions"] == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.hub.core.stats()["sessions"], 0)

    async def test_the_sockets_of_a_replaced_session_keep_receiving(self):
        # §6.4 A1 / rtc-sinks-per-id: the handle, the queue and the closer belong to the id, so the
        # manager's tick keeps serving a socket the console attached before the resume.
        old = _session(self.hub, "resume-socket")
        self.hub.core.spawn("resume-socket", ["cmd.exe"])
        replay, queue = old.attach_ws_queue()
        closer_called = []
        old.register_ws_closer(queue, lambda: closer_called.append(True))
        self.hub.core.output("resume-socket", "OLD-MARK\r\n")
        self.hub.core.kill("resume-socket", 1500)
        self.hub._on_core_tick()  # the predecessor's death record, as the hub would drain it

        resumed = _session(self.hub, "resume-socket")  # same id, same registry, new object
        self.assertIs(resumed.sockets, old.sockets)
        self.hub.core.spawn("resume-socket", ["cmd.exe"], ws_prefix="已恢复 (1)\r\n")
        self.hub.core.output("resume-socket", "NEW-MARK\r\n")

        self.hub._on_core_tick()
        self.assertEqual([], [f for f in [replay] if "OLD-MARK" in f],
                         "the replay was taken before the old bytes arrived")
        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait())
        self.assertEqual(frames, ["OLD-MARK\r\n", "已恢复 (1)\r\n", "NEW-MARK\r\n"])

        resumed.close_ws_frontends()
        self.assertEqual(closer_called, [True], "the closer registered on the old object still closed it")


class HubOnCoreTests(_ManagerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.hub.core.shutdown()  # the fake goes away; the hub's next `_ensure_core` builds the real one
        self.hub.core = None
        self.hub.core = self.hub._ensure_core()
        self.loop = asyncio.get_running_loop()
        self.session = _session(self.hub, "hub-core-probe")
        self.session.start_pty(self.loop)

    async def _screen_until(self, needle, timeout=20.0):
        """One screen read per 50 ms until `needle` shows or the deadline passes."""
        end = self.loop.time() + timeout
        text = ""
        while self.loop.time() < end:
            text = self.session.read_screen(max_lines=80).text
            if needle in text:
                return text
            await asyncio.sleep(0.05)
        return text

    async def test_spawn_echo_resize_kill_exit_and_retire(self):
        self.assertGreater(self.session.pid, 0, "spawn returned no pid")
        banner = await self._screen_until("Microsoft Windows")
        self.assertIn("Microsoft Windows", banner, banner)

        self.assertTrue(self.session.write_input("echo hub-core-echo\r\n"))
        self.assertIn("hub-core-echo", await self._screen_until("hub-core-echo"))

        self.assertEqual(self.session.resize(96, 24), (96, 24))
        self.session.drain_ws_frames()  # nothing attached: a no-op that still reads the seam
        self.assertEqual(self.session.read_screen().cols, 96)

        code = self.session.kill(1500)
        self.assertIsNotNone(code)
        for _ in range(200):
            self.hub._on_core_tick()
            if not self.session.is_process_alive():
                break
            await asyncio.sleep(0.05)
        self.assertFalse(self.session.is_process_alive(), "the death record never reached the session")
        self.assertEqual(self.session.exit_code, code)
        self.assertIn(self.session.status, ("exited", "crashed"), self.session.status)

        # A7's exit path: the scheduled task records the one lifecycle event for the live object.
        for _ in range(200):
            await asyncio.sleep(0)
            if self.session.ended_at is not None:
                break
        self.assertIsNotNone(self.session.ended_at, "the exit callback never ran")
        events = self.hub.get_worker_events("hub-core-probe")
        self.assertTrue([e for e in events if e["type"] in ("exited", "crashed")],
                        [(e["type"], e["status"]) for e in events])

        closed = await self.hub.close_session("hub-core-probe", source="console")
        self.assertTrue(closed["found"])
        self.assertEqual(self.hub.core.stats()["sessions"], 0, "retire left the id in the core's table")
        self.assertNotIn("hub-core-probe", self.hub.sessions)
        self.assertNotIn("hub-core-probe", self.hub.terminal_sockets)


if __name__ == "__main__":
    unittest.main()
