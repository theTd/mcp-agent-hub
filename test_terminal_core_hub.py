"""Step 3b: the hub runs on the terminal core — real ConPTY through `HubSessionManager`.

`TerminalDrainTests` keeps the fake core (scripted records, a resume that hands its sockets over):
one failing record must not stop the others, and a socket attached before a resume keeps its queue.
`HubOnCoreTests` runs a real `cmd.exe` through the manager's session and drain tick: spawn, echo,
resize, kill, the exit event, `retire` on close and `shutdown` leaving the core's table empty.

Run: python -m unittest -v test_terminal_core_hub
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import session_manager
from fake_terminal_core import FakeTerminal
from session_manager import AgentSession, HubSessionManager


def _session(hub, session_id, **kwargs):
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
        # The stop ends the core itself (off the loop, design §6.6); calling it again here would
        # join a stopped ticker.
        await self.hub.archive_all_sessions_on_shutdown()
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

    async def test_a_text_session_is_ended_in_the_core_and_retired(self):
        # `attach_text` has no process, but the core owns the id: closing the session must end it
        # there too, or `retire` could never accept the id and the ring would live for the hub's life.
        sess = _session(self.hub, "text-probe", command="", args=[])
        self.hub.core.attach_text(sess.session_id, cols=120, rows=36)
        sess.emit_virtual_ansi("第一行\r\n")
        self.hub._on_core_tick()
        self.assertEqual(sess.read_raw().text, "第一行\r\n")

        closed = await self.hub.close_session("text-probe", source="mcp")
        self.assertTrue(closed["found"])
        self.hub._on_core_tick()  # the death record schedules the retried retire
        for _ in range(100):
            if self.hub.core.stats()["sessions"] == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.hub.core.stats()["sessions"], 0)

    async def test_a_resume_hands_the_banner_to_the_core_and_keeps_the_ids_entry(self):
        # §6.4 A1 / rtc-sinks-per-id: the sockets belong to the id inside the core, so the hub's
        # part of a resume is only the banner (`ws_prefix`) and never a re-registration. That an
        # attached socket really survives the swap is the core's own case
        # (`test_terminal_core_client.TerminalSocketTests`, `a_resume_banner_reaches_an_attached_socket`).
        old = _session(self.hub, "resume-socket")
        self.hub.core.spawn("resume-socket", ["cmd.exe"])
        self.hub.core.output("resume-socket", "OLD-MARK\r\n")
        self.hub.core.kill("resume-socket", 1500)
        self.hub._on_core_tick()  # the predecessor's death record, as the hub would drain it

        resumed = _session(self.hub, "resume-socket")  # same id, new object
        self.hub.core.spawn("resume-socket", ["cmd.exe"], ws_prefix="已恢复 (1)\r\n")
        self.hub.core.output("resume-socket", "NEW-MARK\r\n")

        self.hub._on_core_tick()
        state = self.hub.core.session("resume-socket")
        self.assertEqual(state.ws_prefix, "已恢复 (1)\r\n",
                         "the resume banner never reached the core's handover")
        self.assertEqual(state.ring, "NEW-MARK\r\n",
                         "the id's entry is the successor's, not the predecessor's")
        self.assertEqual(resumed.raw_revision, state.rev)
        self.assertNotIn("resume-socket", self.hub.core.retired)


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
        # The final write reads the core off the loop (r3-kimi 1/4), so the wait needs real time,
        # not just loop turns, and `ended_at` is set before that read: the event is the real signal.
        events = []
        for _ in range(400):
            await asyncio.sleep(0.01)
            events = self.hub.get_worker_events("hub-core-probe")
            if [e for e in events if e["type"] in ("exited", "crashed")]:
                break
        self.assertIsNotNone(self.session.ended_at, "the exit callback never ran")
        self.assertTrue([e for e in events if e["type"] in ("exited", "crashed")],
                        [(e["type"], e["status"]) for e in events])

        closed = await self.hub.close_session("hub-core-probe", source="console")
        self.assertTrue(closed["found"])
        self.assertEqual(self.hub.core.stats()["sessions"], 0, "retire left the id in the core's table")
        self.assertNotIn("hub-core-probe", self.hub.sessions)
        self.assertEqual(self.hub.core.socket_count(), 0, "a console socket outlived its session")


class RetiredEngineDataTests(unittest.IsolatedAsyncioTestCase):
    """DSH was dropped on 2026-11-15; its old data is purged at start, and these guards cover a skip.

    The owner decided on page `hub-dsh-removal-1115` that the rows and a saved profile go at the
    next start (after a backup, `test_retired_engine_purge`). The guards below still matter for a
    start whose purge was skipped because its backup failed: nothing revives the leftovers.
    """

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temporary = tempfile.TemporaryDirectory(prefix="hub-retired-")
        self.root = Path(self.temporary.name)
        self.profiles = self.root / "agent_profiles.json"
        self.profiles.write_text(json.dumps({"profiles": {
            "dsh": {"name": "DSH", "engine": "dsh", "kind": "tui", "command": "dsh"},
            "plain": {"name": "Plain", "engine": "pwsh", "kind": "tui", "command": "pwsh.exe"},
        }}), encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(self.profiles, base_dir=Path(session_manager.__file__).parent,
                                         data_dir=self.root)
        self.hub.core = FakeTerminal()
        await self.hub.start()

    async def asyncTearDown(self):
        if self.hub.core is not None:
            self.hub.core.kill_all(500)
            self.hub._on_core_tick()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temporary.cleanup()

    async def test_a_saved_dsh_profile_left_by_a_skipped_purge_is_skipped_by_the_loader(self):
        # The start-up purge removed the entry (`test_retired_engine_purge` proves that); put it
        # back to stand for a start whose backup failed and whose purge was therefore skipped.
        self.profiles.write_text(json.dumps({"profiles": {
            "dsh": {"name": "DSH", "engine": "dsh", "kind": "tui", "command": "dsh"},
            "plain": {"name": "Plain", "engine": "pwsh", "kind": "tui", "command": "pwsh.exe"},
        }}), encoding="utf-8")
        before = self.profiles.read_bytes()
        with self.assertLogs("mcp-agent-hub.session_manager", level="INFO") as logs:
            self.hub.load_profiles(discovery={"engines": {}})
        self.assertNotIn("dsh", self.hub.profiles, "the dropped engine's profile is still served")
        self.assertIn("plain", self.hub.profiles, "the ordinary profile was dropped with it")
        self.assertTrue(any("skipped 1 DSH profile(s)" in line for line in logs.output), logs.output)
        self.assertEqual(self.profiles.read_bytes(), before, "the loader rewrote the profile file")

    async def test_a_dsh_row_left_by_a_skipped_purge_refuses_with_a_reason(self):
        sess = _session(self.hub, "dsh-old", command="", args=[])
        sess.engine = "dsh"
        sess.profile_snapshot = {"profile_id": "dsh", "engine": "dsh", "kind": "tui",
                                 "cwd": str(self.root), "args": []}
        sess.conversation_ref = {"dsh_job_id": "job-1"}
        sess.status = "completed"
        sess.end_reason = "exited"
        await self.hub.persist_session(sess)
        await self.hub._archive_session(sess, end_reason="exited")
        page = await self.hub.list_archive()
        self.assertIn("dsh-old", [row["session_id"] for row in page["sessions"]],
                      "the DSH row disappeared from the archive")
        record = await self.hub.db.get_session_record("dsh-old")
        self.assertEqual(self.hub.compute_resumable(record),
                         (False, "DSH 已不再支持，无法恢复此会话。"))
        with self.assertRaises(ValueError) as caught:
            await self.hub.resume_subagent("dsh-old")
        self.assertEqual(str(caught.exception), "DSH 已不再支持，无法恢复此会话。")


class FakeCoreFidelityTests(unittest.TestCase):
    """T1-T3: the fake must be able to script what the real core reports."""

    def test_a_text_session_has_no_process(self):
        # T1: `attach_text` reports pid 0, so the hub takes the no-process branch.
        core = FakeTerminal()
        reply = core.attach_text("text-probe", cols=120, rows=36)
        self.assertEqual(reply["pid"], 0)
        record = core.drain()[0]
        self.assertEqual(record["pid"], 0)
        self.assertFalse(bool(record["pid"]))

    def test_a_predecessor_s_record_is_dropped_by_the_same_id_spawn(self):
        # T2: the core's table insert drops a death that was not drained before the reattach.
        core = FakeTerminal()
        core.spawn("dropped", ["cmd.exe"])
        core.kill("dropped", 200)
        core.spawn("dropped", ["cmd.exe"])  # the resume replaces the entry
        records = core.drain()
        self.assertEqual([r["alive"] for r in records], [True], records)
        self.assertFalse(core.session("dropped").death_drained)
        with self.assertRaises(ValueError):
            core.retire("dropped")

    def test_the_fake_scripts_a_cleanless_death_a_write_deadline_and_idle_time(self):
        # T3: the cases F1/F2/F9 need: no exit code, a full write queue, a real timestamp.
        core = FakeTerminal()
        core.spawn("scripted", ["cmd.exe"])
        core.drain()  # the spawn's own record; the death is what this test scripts
        core.set_exit("scripted", None, crashed=True)
        core.set_last_output_at("scripted", 1234.5)
        core.set_write_timeouts("scripted", 1)
        with self.assertRaises(TimeoutError):
            core.write("scripted", "x")
        core.die("scripted")
        record = core.drain()[0]
        self.assertEqual((record["alive"], record["exit_code"], record["crashed"]), (False, None, True))
        self.assertEqual(record["last_output_at"], 1234.5)


class CoreLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_builds_no_core_and_the_first_session_builds_one(self):
        # F14: `start()` must not load the extension, so a hub (or a test) that never opens a
        # terminal never starts the core's ticker.
        temporary = tempfile.TemporaryDirectory(prefix="hub-lazy-core-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profiles = root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            hub = HubSessionManager(profiles, base_dir=Path(session_manager.__file__).parent,
                                    data_dir=root)
        self.addAsyncCleanup(hub.archive_all_sessions_on_shutdown)
        await hub.start()
        self.assertIsNone(hub.core, "start() built the terminal core")
        core = hub._ensure_core()
        self.assertIs(hub._ensure_core(), core, "a second core was built")


if __name__ == "__main__":
    unittest.main()
