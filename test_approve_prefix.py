"""Spawn-time approve-once prefixes, agy's full transcript command, and brief files.

No model calls and no real terminal: the fake core records keystrokes and the sessions' PTY start
is patched where a spawn test only needs the launch record.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fake_terminal_core import FakeTerminal
from session_manager import (
    AUTO_APPROVE_ONCE_MAX_USES,
    AgentSession,
    HubSessionManager,
    command_starts_with_prefix,
    normalize_brief_markdown,
    normalize_auto_approve_once_prefixes,
)

PREFIX = r"C:\tools\adb.exe shell"
COMMAND = r"C:\tools\adb.exe shell getprop ro.build.version"
AGY_FULL = r"py -3 C:\work\remote.py --task CASTAWAYL --limit 10"
AGY_SCREEN = r"py -3 C:\work\remote.py --task CASTAWAYL…"


def append_rows(path: Path, *rows) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


class PrefixValidationTests(unittest.TestCase):
    def test_absolute_program_paths_are_accepted(self):
        prefixes = [PREFIX, r"\\server\share\run.exe --flag", "/d/tools/run.sh"]
        self.assertEqual(normalize_auto_approve_once_prefixes(prefixes), prefixes)

    def test_bare_and_relative_program_names_are_rejected(self):
        for bad in ("adb.exe shell", "py -3 x.py", r"tools\adb.exe", "run.sh", "./run.sh"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_auto_approve_once_prefixes([bad])

    def test_shape_and_shell_syntax_are_rejected(self):
        for bad in ("not a list", [""], [None], [PREFIX + "\nnext"], [PREFIX + "\x7f"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_auto_approve_once_prefixes(bad)
        for syntax in (";", "|", "&", ">", "<", "`", "$(", "(", ")"):
            with self.subTest(syntax=syntax), self.assertRaises(ValueError):
                normalize_auto_approve_once_prefixes([PREFIX + " a " + syntax + " b"])
        with self.assertRaises(ValueError):
            normalize_auto_approve_once_prefixes([PREFIX] * 33)

    def test_prefix_matches_at_a_word_boundary_only(self):
        self.assertTrue(command_starts_with_prefix(COMMAND, PREFIX))
        self.assertTrue(command_starts_with_prefix(PREFIX, PREFIX))
        self.assertFalse(command_starts_with_prefix(PREFIX + "x --flag", PREFIX))
        self.assertFalse(command_starts_with_prefix(r"C:\tools\adb.exe.shell", PREFIX))
        self.assertFalse(command_starts_with_prefix(COMMAND.lower(), PREFIX))


class PrefixMatchTests(unittest.TestCase):
    def session(self, prefixes=(PREFIX,)):
        return SimpleNamespace(engine="omp", profile_snapshot={"auto_approve_once_prefixes": list(prefixes)})

    def interaction(self, command=COMMAND, source="transcript", truncated=False):
        return {"metadata": {"command": command, "command_source": source,
                             "command_truncated": truncated}}

    def test_complete_transcript_command_matches(self):
        self.assertEqual(
            HubSessionManager._auto_approve_once_prefix_matches(self.session(), self.interaction()), PREFIX
        )

    def test_every_shell_metacharacter_blocks_the_match(self):
        for token in (";", "|", "&", ">", "<", "`", "$(", "(", ")"):
            with self.subTest(token=token):
                self.assertIsNone(
                    HubSessionManager._auto_approve_once_prefix_matches(
                        self.session(), self.interaction(COMMAND + " " + token + " x")
                    )
                )
        self.assertIsNone(
            HubSessionManager._auto_approve_once_prefix_matches(
                self.session(), self.interaction(COMMAND + "\nrm -rf x")
            )
        )
        # PowerShell runs a subexpression in argument position; it is never auto-approved.
        self.assertIsNone(
            HubSessionManager._auto_approve_once_prefix_matches(
                self.session(), self.interaction(PREFIX + " (Remove-Item C:\\x -Recurse)")
            )
        )

    def test_any_control_character_blocks_the_match(self):
        for control in ("\x07", "\t", "\x1b", "\x7f"):
            with self.subTest(control=repr(control)):
                self.assertIsNone(
                    HubSessionManager._auto_approve_once_prefix_matches(
                        self.session(), self.interaction(COMMAND + " x" + control + "y")
                    )
                )

    def test_missing_complete_flag_is_not_trusted(self):
        interaction = {"metadata": {"command": COMMAND, "command_source": "transcript"}}
        self.assertIsNone(
            HubSessionManager._auto_approve_once_prefix_matches(self.session(), interaction)
        )

    def test_screen_and_truncated_commands_never_match(self):
        for source, truncated in (("screen", False), ("screen", True), ("none", False), ("transcript", True)):
            with self.subTest(source=source, truncated=truncated):
                self.assertIsNone(
                    HubSessionManager._auto_approve_once_prefix_matches(
                        self.session(), self.interaction(source=source, truncated=truncated)
                    )
                )

    def test_other_engines_and_unmatched_commands_never_match(self):
        session = self.session()
        command = self.interaction()
        session.engine = "codex"
        self.assertIsNone(HubSessionManager._auto_approve_once_prefix_matches(session, command))
        session.engine = "omp"
        self.assertIsNone(HubSessionManager._auto_approve_once_prefix_matches(
            session, self.interaction(r"D:\other\adb.exe shell getprop")
        ))


class HubCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-prefix-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.fake = FakeTerminal()
        self.hub.core = self.fake
        await self.hub.start()
        self.hub.profiles["omp-brief"] = {
            "name": "OMP Brief", "engine": "omp", "enabled": True, "kind": "tui",
            "command": "unused", "args": [], "task_mode": "omp_positional",
        }

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    def session(self, engine, prefixes=(), uses=0):
        sid = f"{engine}-prefix"
        sess = AgentSession(sid, engine, engine, "tui", "Prefix fixture", str(self.root), "unused", [],
                            core=self.fake)
        sess.engine = engine
        sess.status = "running"
        sess.profile_snapshot = {
            "engine": engine,
            "auto_approve_once_prefixes": list(prefixes),
            "auto_approve_once_uses": uses,
        }
        sess._loop = asyncio.get_running_loop()
        self.fake.spawn(sid, ["fixture"])
        self.hub.sessions[sid] = sess
        self.inputs = self.fake.inputs_of(sid)
        return sess

    async def until(self, predicate, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("condition not reached")
            await asyncio.sleep(0.02)

    def approval(self, command=COMMAND, source="transcript", truncated=False):
        return {
            "kind": "approval_request",
            "title": "OMP Tool Approval (bash)",
            "prompt": f"Allow tool: bash\nCommand: {command}",
            "options": ["Approve", "Deny"],
            "source": "omp_approval_event",
            "metadata": {
                "omp_prompt": "tool", "omp_options": ["Approve", "Deny"],
                "command": command, "command_source": source, "command_truncated": truncated,
            },
        }


class AutoApproveOnceTests(HubCase):
    async def test_prefix_answers_approve_once_and_records_the_event(self):
        sess = self.session("omp", [PREFIX])
        with patch.object(sess, "write_input", side_effect=self.inputs.append):
            interaction = await self.hub.register_pending_interaction(sess, **self.approval())
        self.assertEqual(interaction["auto_approval"]["prefix"], PREFIX)
        await asyncio.sleep(0.8)
        self.assertIn("\x11", self.inputs)          # Ctrl+Q: Approve once
        self.assertNotIn("\x1b[B", "".join(self.inputs))  # never the session row
        self.assertIsNone(sess.pending_interaction)
        self.assertEqual(sess.profile_snapshot["auto_approve_once_uses"], 1)
        events = [e for e in self.hub.get_worker_events(sess.session_id, 0)
                  if e["type"] == "approval_auto_resolved"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["command"], COMMAND)
        self.assertEqual(events[0]["data"]["prefix"], PREFIX)
        self.assertEqual(events[0]["data"]["count"], 1)
        self.assertEqual(events[0]["data"]["decision"], "approve_once")

    async def test_cap_sends_the_request_to_the_parent_and_says_so(self):
        sess = self.session("omp", [PREFIX], uses=AUTO_APPROVE_ONCE_MAX_USES)
        with patch.object(sess, "write_input", side_effect=self.inputs.append):
            interaction = await self.hub.register_pending_interaction(sess, **self.approval())
        self.assertTrue(interaction["metadata"]["auto_approve_once_cap_reached"])
        self.assertIs(sess.pending_interaction, interaction)
        self.assertEqual(self.inputs, [])
        event = [e for e in self.hub.get_worker_events(sess.session_id, 0) if e["type"] == "approval_request"][-1]
        self.assertIn(f"cap of {AUTO_APPROVE_ONCE_MAX_USES}", event["content"])

    async def test_screen_command_is_never_approved(self):
        sess = self.session("omp", [PREFIX])
        with patch.object(sess, "write_input", side_effect=self.inputs.append):
            interaction = await self.hub.register_pending_interaction(
                sess, **self.approval(source="screen", truncated=True)
            )
        self.assertNotIn("auto_approval", interaction)
        self.assertIsNotNone(sess.pending_interaction)
        self.assertEqual(self.inputs, [])

    async def test_missing_complete_flag_goes_to_the_parent(self):
        sess = self.session("omp", [PREFIX])
        approval = self.approval()
        approval["metadata"].pop("command_truncated")
        with patch.object(sess, "write_input", side_effect=self.inputs.append):
            interaction = await self.hub.register_pending_interaction(sess, **approval)
        self.assertNotIn("auto_approval", interaction)
        self.assertIsNotNone(sess.pending_interaction)
        self.assertEqual(self.inputs, [])

    async def test_agy_full_transcript_command_is_approved_once(self):
        sess = self.session("agy", [r"py -3 C:\work\remote.py"])
        work = self.root / "agy-run"
        path = work / ".system_generated" / "logs" / "transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"brain_dir": str(work)}
        watch = self.hub._schedule(self.hub._watch_agy_transcript(sess, self.root))
        try:
            append_rows(path, {"type": "PLANNER_RESPONSE", "content": "", "tool_calls": json.dumps(
                [{"name": "run_command", "args": {"CommandLine": json.dumps(AGY_FULL)}}])})
            await self.until(lambda: sess._inflight_transcript_tool is not None)
            parsed = {
                "header": "Requesting permission for py -3 C:\\work\\remote.py --task CASTAWAYL…",
                "reason": "", "command": AGY_SCREEN,
                "options": [{"key": "1", "label": "Yes, run command", "action": "approve_once"},
                            {"key": "4", "label": "No, cancel", "action": "deny"}],
            }
            with patch.object(sess, "write_input", side_effect=self.inputs.append):
                interaction = await self.hub._register_agy_screen_approval(sess, parsed, "screen")
            self.assertEqual(interaction["metadata"]["command_source"], "transcript")
            self.assertEqual(interaction["metadata"]["command"], AGY_FULL)
            self.assertIn("auto_approval", interaction)
            await asyncio.sleep(0.2)
            self.assertEqual(self.inputs, ["1"])  # approve once, never the session option
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

    async def test_prefixes_survive_a_resume(self):
        with patch.object(AgentSession, "start_pty"), \
                patch.object(self.hub, "_schedule", lambda coro: coro.close()):
            sess = await self.hub.create_session(
                "omp-brief", task="Task body", cwd=str(self.root),
                auto_approve_once_prefixes=[PREFIX],
            )
        self.assertEqual(sess.profile_snapshot["auto_approve_once_prefixes"], [PREFIX])
        await self.hub.db.flush()
        record = await self.hub.db.get_session_record(sess.session_id)
        resumed = await self.hub._make_resumed_session(
            sess.session_id, record, record["profile_snapshot"], {}, {},
        )
        self.assertEqual(resumed.engine, "omp")
        self.assertEqual(resumed.profile_snapshot["auto_approve_once_prefixes"], [PREFIX])
        self.assertEqual(resumed.profile_snapshot["auto_approve_once_uses"], 0)


class AgyTranscriptCommandTests(HubCase):
    def command_prompt(self, command, header=None):
        return {
            "header": header or f"Requesting permission for {command}",
            "reason": "", "command": command,
            "options": [{"key": "1", "label": "Yes, run command", "action": "approve_once"},
                        {"key": "4", "label": "No, cancel", "action": "deny"}],
        }

    async def test_unclipped_screen_command_equal_to_the_record_uses_it(self):
        sess = self.session("agy")
        sess._agy_response_tools = [{"tool_name": "run_command", "command": AGY_FULL}]
        interaction = await self.hub._register_agy_screen_approval(
            sess, self.command_prompt(AGY_FULL), "screen"
        )
        self.assertEqual(interaction["metadata"]["command"], AGY_FULL)
        self.assertEqual(interaction["metadata"]["command_source"], "transcript")
        self.assertFalse(interaction["metadata"]["command_truncated"])

    async def test_unclipped_screen_command_that_differs_keeps_the_screen_copy(self):
        sess = self.session("agy")
        sess._agy_response_tools = [{"tool_name": "run_command", "command": AGY_FULL}]
        other = r"py -3 C:\work\other.py --task X"
        interaction = await self.hub._register_agy_screen_approval(
            sess, self.command_prompt(other), "screen"
        )
        self.assertEqual(interaction["metadata"]["command"], other)
        self.assertEqual(interaction["metadata"]["command_source"], "screen")

    async def test_two_open_calls_sharing_a_visible_start_are_not_approved(self):
        clean = r"C:\tools\run.exe --task ALPHA"
        dirty = r"C:\tools\run.exe --task ALPHA; Remove-Item C:\x -Recurse"
        sess = self.session("agy", [r"C:\tools\run.exe"])
        work = self.root / "agy-two"
        path = work / ".system_generated" / "logs" / "transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"brain_dir": str(work)}
        watch = self.hub._schedule(self.hub._watch_agy_transcript(sess, self.root))
        try:
            append_rows(path, {"type": "PLANNER_RESPONSE", "content": "", "tool_calls": json.dumps([
                {"name": "run_command", "args": {"CommandLine": json.dumps(clean)}},
                {"name": "run_command", "args": {"CommandLine": json.dumps(dirty)}},
            ])})
            await self.until(lambda: len(sess._agy_response_tools) == 2)
            visible = r"C:\tools\run.exe --task ALPHA…"
            with patch("session_manager.AGY_BOX_CALL_WAIT_SEC", 0.05), \
                    patch.object(sess, "write_input", side_effect=self.inputs.append):
                interaction = await self.hub._register_agy_screen_approval(
                    sess, self.command_prompt(visible), "screen"
                )
            self.assertEqual(interaction["metadata"]["command"], visible)
            self.assertEqual(interaction["metadata"]["command_source"], "screen")
            self.assertTrue(interaction["metadata"]["command_truncated"])
            self.assertNotIn("auto_approval", interaction)
            self.assertIsNotNone(sess.pending_interaction)
            self.assertEqual(self.inputs, [])
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

    async def test_no_matching_record_keeps_the_screen_text_marked_truncated(self):
        sess = self.session("agy")
        with patch("session_manager.AGY_BOX_CALL_WAIT_SEC", 0.05):
            interaction = await self.hub._register_agy_screen_approval(
                sess, self.command_prompt(AGY_SCREEN), "screen"
            )
        self.assertEqual(interaction["metadata"]["command"], AGY_SCREEN)
        self.assertEqual(interaction["metadata"]["command_source"], "screen")
        self.assertTrue(interaction["metadata"]["command_truncated"])

    async def test_record_of_another_call_is_not_used(self):
        sess = self.session("agy")
        sess._inflight_transcript_tool = {"tool_name": "run_command", "command": "Get-ChildItem",
                                          "args": {}, "call_id": "c9"}
        with patch("session_manager.AGY_BOX_CALL_WAIT_SEC", 0.05):
            interaction = await self.hub._register_agy_screen_approval(
                sess, self.command_prompt(AGY_SCREEN), "screen"
            )
        self.assertEqual(interaction["metadata"]["command_source"], "screen")
        self.assertTrue(interaction["metadata"]["command_truncated"])


class ParentViewTests(HubCase):
    async def register_omp_box(self, sess):
        return await self.hub.register_pending_interaction(
            sess,
            kind="approval_request",
            title="OMP Tool Approval (bash)",
            prompt="Allow tool: bash",
            options=["Approve", "Approve for this session", "Deny"],
            source="omp_tui_screen_detector",
            metadata={
                "omp_prompt": "tool",
                "omp_options": ["Approve", "Approve for this session", "Deny"],
                "selected_index": 0,
                "session_label": "Approve for this session",
                "command": "adb devices",
                "command_source": "screen",
                "command_truncated": False,
            },
        )

    async def test_no_parent_view_exposes_the_omp_cursor(self):
        sess = self.session("omp")
        info = await self.hub.wait_inbox.ensure_for_sessions([sess.session_id], {sess.session_id: sess.turn_id})
        interaction = await self.register_omp_box(sess)
        self.assertEqual(sess.pending_interaction["metadata"]["selected_index"], 0)
        self.assertEqual(interaction["metadata"]["selected_index"], 0)  # internal state keeps it
        self.assertNotIn("selected_index", sess.to_dict()["pending_interaction"]["metadata"])
        ask = await self.hub.wait_inbox.wait(info["wait_handle"], 0.5)
        self.assertEqual(ask["state"], "ask")
        self.assertNotIn("selected_index", ask["requests"][0]["interaction"]["metadata"])
        pending = await self.hub.get_pending_interactions(sess.session_id)
        self.assertNotIn("selected_index", pending[0]["metadata"])
        with patch.object(self.hub, "summarize_transcript", return_value={}):
            trace = await self.hub.get_subagent_status_and_trace(sess.session_id, 0, 0)
        self.assertIsNotNone(trace["pending_interaction"])
        self.assertNotIn("selected_index", trace["pending_interaction"]["metadata"])

    async def test_persisted_record_keeps_the_cursor(self):
        sess = self.session("omp")
        await self.register_omp_box(sess)
        record = HubSessionManager._record_from_session(sess)
        self.assertEqual(record["pending_interaction"]["metadata"]["selected_index"], 0)
        captured = {}

        async def capture(record, **kwargs):
            captured.update(record)

        with patch.object(self.hub.db, "upsert_session", side_effect=capture):
            await self.hub.persist_session(sess)
        self.assertEqual(captured["pending_interaction"]["metadata"]["selected_index"], 0)
        self.assertNotIn("selected_index", sess.to_dict()["pending_interaction"]["metadata"])

    async def test_omp_cursor_move_still_works(self):
        sess = self.session("omp")
        await self.register_omp_box(sess)
        with patch.object(sess, "write_input", side_effect=self.inputs.append):
            await self.hub.respond_to_interaction(
                sess.session_id, decision="approve_session",
                request_id=sess.pending_interaction["request_id"],
            )
        await asyncio.sleep(0.8)
        self.assertEqual(self.inputs, ["\x1b[B", "\r"])


class BriefMarkdownTests(HubCase):
    async def spawn(self, task="Do the work", **kwargs):
        with patch.object(AgentSession, "start_pty"), \
                patch.object(self.hub, "_schedule", lambda coro: coro.close()):
            return await self.hub.create_session("omp-brief", task=task, cwd=str(self.root), **kwargs)

    def brief_path(self, sess):
        return self.root / "briefs" / f"{sess.session_id}.md"

    async def test_brief_written_and_the_pointer_comes_first(self):
        brief = "# Brief\n\nA long task description."
        sess = await self.spawn(brief_markdown=brief)
        path = self.brief_path(sess)
        self.assertEqual(path.read_text(encoding="utf-8"), brief)
        self.assertEqual(sess.profile_snapshot["brief_path"], str(path))
        arg = next(a for a in sess.args if "Read the full brief first:" in a)
        self.assertTrue(arg.startswith(f"Read the full brief first: {path.absolute()}"))
        self.assertNotIn(brief, arg)  # the brief itself is not typed into the task
        self.assertIn("\n\nDo the work", arg)
        self.assertEqual(sess.last_task, "Do the work")

    async def test_brief_deleted_on_terminate(self):
        sess = await self.spawn(brief_markdown="term brief")
        path = self.brief_path(sess)
        self.assertTrue(path.exists())
        await self.hub.close_session(sess.session_id, source="mcp")
        self.assertFalse(path.exists())

    async def test_brief_deleted_on_archive(self):
        sess = await self.spawn(brief_markdown="archive brief")
        path = self.brief_path(sess)
        self.assertTrue(path.exists())
        await self.hub._archive_session(sess)
        self.assertFalse(path.exists())

    async def test_oversized_brief_is_rejected_without_a_leftover(self):
        with self.assertRaises(ValueError):
            await self.spawn(brief_markdown="x" * 200_001)
        self.assertFalse((self.root / "briefs").exists() and any((self.root / "briefs").iterdir()))
        self.assertIsNone(normalize_brief_markdown(None))

    async def test_bad_prefix_is_rejected_before_any_session(self):
        with self.assertRaises(ValueError):
            await self.spawn(auto_approve_once_prefixes=["adb.exe shell"])
        self.assertEqual(self.hub.sessions, {})

    async def test_stale_briefs_are_pruned_and_a_live_one_is_kept(self):
        briefs = self.root / "briefs"
        briefs.mkdir(parents=True, exist_ok=True)
        ghost = briefs / "ghost-session.md"
        ghost.write_text("left by a crash", encoding="utf-8")
        live = briefs / "omp-live.md"
        live.write_text("live", encoding="utf-8")
        self.hub.sessions["omp-live"] = SimpleNamespace()
        try:
            self.assertEqual(self.hub._prune_stale_briefs(), 1)
        finally:
            self.hub.sessions.pop("omp-live", None)
        self.assertFalse(ghost.exists())
        self.assertTrue(live.exists())

    async def test_start_deletes_briefs_no_session_owns(self):
        briefs = self.root / "briefs"
        briefs.mkdir(parents=True, exist_ok=True)
        ghost = briefs / "interrupted-run.md"
        ghost.write_text("left by a crash", encoding="utf-8")
        await self.hub.archive_all_sessions_on_shutdown()
        profiles = self.root / "profiles.json"
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            second = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await second.start()
        try:
            self.assertFalse(ghost.exists())
        finally:
            await second.archive_all_sessions_on_shutdown()

    async def test_start_keeps_the_brief_of_a_session_the_stop_cut_off(self):
        briefs = self.root / "briefs"
        briefs.mkdir(parents=True, exist_ok=True)
        cut_off = briefs / "omp-cut-off.md"
        cut_off.write_text("resume reads me", encoding="utf-8")
        closed = briefs / "omp-closed.md"
        closed.write_text("no reader", encoding="utf-8")
        await self.hub.archive_all_sessions_on_shutdown()
        profiles = self.root / "profiles.json"
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            second = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        live_at_stop = {"omp-cut-off": {"engine": "omp", "status": "interrupted"}}
        with patch.object(second, "_normalize_archive_on_startup", AsyncMock(return_value=live_at_stop)):
            await second.start()
        try:
            self.assertTrue(cut_off.exists())
            self.assertFalse(closed.exists())
        finally:
            await second.archive_all_sessions_on_shutdown()


if __name__ == "__main__":
    unittest.main()
