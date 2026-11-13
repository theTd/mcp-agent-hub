"""Session policy and real Codex MCP modal regressions; no model calls."""
import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastmcp import Client
from session_manager import (AgentSession, HubSessionManager, APPROVAL_ENGINES,
                             parse_codex_approval_prompt, resolve_session_approval,
                             bypass_launch_flags, normalize_auto_approval_rules,
                             apply_approval_flags, resolve_session_approval_fields,
                             AUTO_ON_NOTE, AUTO_UNSUPPORTED_NOTE, CLAUDE_AUTO_SETTINGS_NAME,
                             CLIPPED_COMMAND_NOTE, screen_command_clipped, tool_call_for_approval_box)
from test_native_records import FakePty

MODAL = '''Field 1/1
Allow the approval_probe MCP server to run tool "emit_marker"?

token: codex-mcp-approval-phase-one

› 1. Allow   Run the tool and continue
  2. Cancel  Cancel this tool call
enter to submit | esc to cancel'''


class ApprovalParserTests(unittest.TestCase):
    def test_auto_approval_rules_are_bounded_and_structured(self):
        rule = {
            "id": "readonly",
            "effect": "approve_once",
            "match": {"tool": ["Bash"], "command_argv": [["git", "status"]]},
            "constraints": {"network": False, "outside_workspace": False, "shell_composition": False},
            "max_uses": 2,
            "expires_in_seconds": 60,
        }
        normalized = normalize_auto_approval_rules([rule])
        self.assertEqual(normalized[0]["match"]["command_argv"], [["git", "status"]])
        self.assertEqual(normalized[0]["max_uses"], 2)
        with self.assertRaises(ValueError):
            normalize_auto_approval_rules([dict(rule, effect="approve_always")])
        with self.assertRaises(ValueError):
            normalize_auto_approval_rules([dict(rule, expires_in_seconds=8 * 24 * 60 * 60)])

    def test_real_mcp_modal_and_wrapped_arguments(self):
        parsed = parse_codex_approval_prompt(MODAL.replace("phase-one", "phase-\none"))
        self.assertEqual(parsed["prompt_kind"], "mcp")
        self.assertEqual(parsed["server"], "approval_probe")
        self.assertEqual(parsed["tool"], "emit_marker")
        self.assertIn("phase-\none", parsed["arguments"])
        self.assertEqual([o["action"] for o in parsed["options"]], ["approve_once", "deny"])

    def test_incomplete_mcp_modal_is_not_an_approval(self):
        self.assertIsNone(parse_codex_approval_prompt(MODAL.split("›")[0]))

    def test_command_and_edit_shortcuts_survive(self):
        for kind, header, key in (("command", "run the following command", "p"),
                                  ("edit", "make the following edits", "a")):
            parsed = parse_codex_approval_prompt(
                f"Would you like to {header}?\n$ echo test\n1. Yes (y)\n2. Don't ask again ({key})\n3. No (esc)")
            self.assertEqual(parsed["prompt_kind"], kind)
            self.assertEqual(parsed["session_key"], key)

    def test_strict_policy_types_and_inapplicable_engine(self):
        for value in (0, 1, "false", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_session_approval("codex", {}, value)
        policy = resolve_session_approval("pwsh", {}, True)
        self.assertEqual(policy["approval_source"], "not_applicable")
        self.assertFalse(policy["bypass_approvals"])

    def test_d13_mode_defaults_overrides_and_conflicts(self):
        mode = lambda engine, profile=None, bypass=None, requested=None: resolve_session_approval(
            engine, profile or {}, bypass, requested)["approval_mode"]
        for engine in ("codex", "claude"):
            self.assertEqual(mode(engine), "auto")
            self.assertEqual(mode(engine, bypass=False), "auto")  # false = not bypassed, not ask
            self.assertEqual(mode(engine, requested="ask"), "ask")
        for engine in ("kimi", "omp", "agy"):
            self.assertEqual(mode(engine), "ask")
            self.assertEqual(mode(engine, requested="auto"), "ask")
        self.assertEqual(mode("codex", {"bypass_approvals": True}), "bypass")
        self.assertEqual(mode("codex", {"bypass_approvals": True}, False), "auto")
        self.assertEqual(mode("codex", {"bypass_approvals": True}, requested="ask"), "ask")
        self.assertEqual(mode("claude", bypass=True), "bypass")
        self.assertEqual(mode("claude", requested=" Bypass "), "bypass")
        self.assertIsNone(mode("dsh", requested="ask"))
        policy = resolve_session_approval("codex", {}, None, "ask")
        self.assertEqual((policy["approval_source"], policy["bypass_approvals"]), ("session", False))
        for bypass, requested in ((True, "ask"), (True, "auto"), (False, "bypass")):
            with self.subTest(bypass=bypass, requested=requested), self.assertRaises(ValueError):
                resolve_session_approval("codex", {}, bypass, requested)
        for bad in ("manual", "", True, 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_session_approval("codex", {}, None, bad)

    def test_d13_flags_replace_profile_modes(self):
        self.assertEqual(apply_approval_flags("codex", ["-a", "never", "--full-auto", "--x"], mode="auto"),
                         ["--approve-for-me", "--x"])
        claude = apply_approval_flags("claude", ["--permission-mode", "plan", "--x"], mode="auto")
        self.assertEqual(claude[:2], ["--permission-mode", "auto"])
        self.assertTrue(claude[2].startswith("--settings=") and claude[2].endswith(CLAUDE_AUTO_SETTINGS_NAME))
        self.assertEqual(claude[3:], ["--x"])
        # The hub's own overlays (either mode) are recognized and replaced on the next launch.
        self.assertEqual(apply_approval_flags("claude", claude, mode="ask")[:2], ["--permission-mode", "acceptEdits"])
        self.assertEqual(apply_approval_flags("claude", claude, mode="bypass"), ["--dangerously-skip-permissions", "--x"])
        self.assertEqual(apply_approval_flags("kimi", ["--yolo"], mode="auto"), ["-y"])

    def test_d13_legacy_fields_report_the_mode_they_ran(self):
        self.assertEqual(resolve_session_approval_fields({"engine": "codex"})["approval_mode"], "ask")
        self.assertEqual(resolve_session_approval_fields({"engine": "codex", "bypass_approvals": True})["approval_mode"], "bypass")
        self.assertIsNone(resolve_session_approval_fields({"engine": "dsh"})["approval_mode"])
        self.assertEqual(resolve_session_approval_fields({"engine": "claude", "approval_mode": "auto"})["approval_mode"], "auto")


class SessionApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-approval-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()
        with patch("session_manager.HubSessionManager", return_value=self.hub):
            self.server = importlib.import_module("server")
        self.hub_patch = patch.object(self.server, "hub", self.hub)
        self.hub_patch.start()
        self.screen = MODAL
        self.sess = AgentSession("codex-test", "codex", "Codex", "tui", "Test", str(self.root), "unused", [])
        self.sess.engine = "codex"
        self.sess.status = "running"
        self.sess.pty = FakePty()
        self.sess._loop = asyncio.get_running_loop()
        self.sess.get_clean_screen_text = lambda **kw: self.screen
        self.hub.sessions[self.sess.session_id] = self.sess

    async def asyncTearDown(self):
        self.hub_patch.stop()
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def pending(self):
        return await self.hub.poll_tui_screen_interactions_once(self.sess)

    async def test_session_auto_rule_resolves_structured_request_once(self):
        self.sess.engine = "pwsh"
        self.sess.profile_snapshot = {
            "auto_approval_rules": normalize_auto_approval_rules([{
                "id": "workspace-readonly",
                "effect": "approve_once",
                "match": {"tool": ["Bash"], "command_argv": [["git", "status"]]},
                "constraints": {"network": False, "outside_workspace": False, "shell_composition": False},
                "max_uses": 1,
                "expires_in_seconds": 900,
            }]),
        }
        with patch.object(self.sess, "write_input", return_value=True):
            interaction = await self.hub.register_pending_interaction(
                self.sess,
                kind="approval_request",
                title="read-only command",
                prompt="git status",
                options=["Approve", "Deny"],
                source="test",
                metadata={"tool": "Bash", "command": "git status", "argv": ["git", "status"], "cwd": str(self.root)},
            )
        self.assertEqual(interaction["auto_approval"]["rule_id"], "workspace-readonly")
        self.assertEqual(interaction["auto_approval"]["remaining_uses"], 0)
        self.assertIsNone(self.sess.pending_interaction)
        self.assertTrue(any(e["type"] == "approval_auto_resolved" for e in self.hub.get_worker_events(self.sess.session_id)))

    async def test_auto_rule_rejects_shell_and_outside_workspace(self):
        rule = normalize_auto_approval_rules([{
            "id": "readonly",
            "effect": "approve_once",
            "match": {"tool": ["Bash"], "command_argv": [["rg", "*"]]},
            "constraints": {"network": False, "outside_workspace": False, "shell_composition": False},
        }])[0]
        self.assertFalse(self.hub._auto_approval_rule_matches(
            self.sess,
            {"metadata": {"tool": "Bash", "command": "rg foo && rm -rf x", "argv": ["rg", "foo", "&&", "rm", "-rf", "x"]}},
            rule,
        ))
        self.assertFalse(self.hub._auto_approval_rule_matches(
            self.sess,
            {"metadata": {"tool": "Bash", "command": "rg foo", "argv": ["rg", "foo"], "path": str(self.root.parent / "outside")}},
            rule,
        ))

    async def test_detection_wakes_parent_and_deduplicates(self):
        pending = await self.pending()
        info = await self.hub.wait_inbox.ensure_for_sessions([self.sess.session_id], {self.sess.session_id: self.sess.turn_id})
        result = await self.hub.wait_inbox.wait(info["wait_handle"], 1)
        self.assertEqual(self.sess.status, "waiting_parent")
        self.assertEqual(result["state"], "ask")
        self.assertEqual(result["requests"][0]["request_id"], pending["request_id"])
        await self.pending()
        self.assertEqual(len([e for e in self.hub.get_worker_events(self.sess.session_id, 0)
                              if e["type"] == "approval_request"]), 1)

    async def answer(self, decision="approve"):
        pending = await self.pending()
        def write(text):
            self.sess.pty.inputs.append(text)
            self.screen = "Working..."
        with patch.object(self.sess, "write_input", write):
            return await self.hub.respond_to_interaction(self.sess.session_id, decision=decision,
                                                        request_id=pending["request_id"])

    async def test_allow_and_identical_following_call(self):
        result = await self.answer()
        self.assertEqual(result["response_state"], "unconfirmed")
        self.assertEqual(self.sess.pty.inputs, ["\x1b[A\x1b[A\r"])
        self.assertIsNotNone(self.sess.pending_interaction)
        await asyncio.sleep(.12)
        self.assertEqual(await self.hub.get_pending_interactions(self.sess.session_id), [])
        self.screen = MODAL
        again = await self.pending()
        self.assertIsNotNone(again)
        self.assertNotEqual(result["resolved_request_id"], again["request_id"])

    async def test_cancel_and_session_downgrade(self):
        result = await self.answer("deny")
        self.assertEqual(result["approval_action"], "deny")
        self.assertEqual(self.sess.pty.inputs[-1], "\x1b[A\x1b[A\x1b[B\r")
        await asyncio.sleep(.12)
        self.assertEqual(await self.hub.get_pending_interactions(self.sess.session_id), [])
        self.screen = MODAL
        result = await self.answer("approve_session")
        self.assertEqual(result["approval_action"], "approve_once")
        self.assertTrue(result["notes"])

    async def test_missing_stale_and_empty_requests_never_write(self):
        self.screen = "Idle"
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(self.sess.session_id, decision="approve")
        self.screen = MODAL
        await self.pending()
        for kwargs in ({"decision": "approve", "request_id": "stale"}, {}):
            with self.assertRaises(ValueError):
                await self.hub.respond_to_interaction(self.sess.session_id, **kwargs)
        self.assertEqual(self.sess.pty.inputs, [])

    async def test_changed_modal_never_uses_old_request(self):
        pending = await self.pending()
        self.screen = MODAL.replace("phase-one", "different")
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(self.sess.session_id, decision="approve", request_id=pending["request_id"])
        self.assertEqual(self.sess.pty.inputs, [])

    async def test_unconfirmed_keeps_pending_and_refuses_duplicate(self):
        pending = await self.pending()
        result = await self.hub.respond_to_interaction(self.sess.session_id, decision="approve")
        self.assertTrue(result["delivered"])
        self.assertEqual(result["response_state"], "unconfirmed")
        self.assertIs(self.sess.pending_interaction, pending)
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(self.sess.session_id, decision="approve")
        self.assertEqual(len(self.sess.pty.inputs), 1)
        self.screen = "Working..."
        await self.hub.get_pending_interactions(self.sess.session_id)
        await asyncio.sleep(.12)
        self.assertEqual(await self.hub.get_pending_interactions(self.sess.session_id), [])
        self.assertEqual(pending["metadata"]["response_state"], "confirmed")

    async def test_wait_url_follows_answers_from_send_and_console(self):
        # 2026-11-12 regression (codex-97e45358): an approval answered with send_to_subagent(text="y")
        # stayed on the wait URL, which then hid the second approval and the turn's end.
        def write(text):
            self.sess.pty.inputs.append(text)
            self.screen = "Working..."
        # The handle exists before the first approval, as after a spawn, so every ask enters its inbox.
        info = await self.hub.wait_inbox.ensure_for_sessions([self.sess.session_id], {self.sess.session_id: self.sess.turn_id})
        handle = self.sess.wait_handle = info["wait_handle"]
        first = await self.pending()
        self.assertEqual((await self.hub.wait_inbox.wait(handle, 0.1))["requests"][0]["request_id"], first["request_id"])
        with patch.object(self.sess, "write_input", write):
            async with Client(self.server.mcp) as client:
                sent = await client.call_tool("send_to_subagent", {"session_id": self.sess.session_id, "text": "y"})
        data = sent.data if sent.data is not None else json.loads(sent.content[0].text)
        self.assertEqual(data["wait_handle"], handle)
        self.assertEqual(data["delivery"]["resolved_question_id"], first["request_id"])
        # Sent but not natively confirmed yet: answered all the same, so the URL never repeats it.
        self.assertEqual((await self.hub.wait_inbox.wait(handle, 0.1))["state"], "waiting")
        await asyncio.sleep(.12)
        self.assertEqual(await self.hub.get_pending_interactions(self.sess.session_id), [])
        self.screen = MODAL
        second = await self.pending()
        self.assertNotEqual(second["request_id"], first["request_id"])
        result = await self.hub.wait_inbox.wait(handle, 0.1)
        self.assertEqual((result["state"], result["requests"][0]["request_id"]), ("ask", second["request_id"]))
        with patch.object(self.sess, "write_input", write):
            await self.hub.deliver_parent_message(self.sess.session_id, "approve", source="console")
        await asyncio.sleep(.12)
        self.assertEqual(await self.hub.get_pending_interactions(self.sess.session_id), [])
        await self.hub.record_worker_event(self.sess.session_id, {"type": "turn_completed", "content": "finished"})
        done = await self.hub.wait_inbox.wait(handle, 0.1)
        self.assertEqual((done["state"], done["final_response"]), ("done", "finished"))

    async def test_ask_of_an_ended_session_leaves_the_wait_url(self):
        info = await self.hub.wait_inbox.ensure_for_sessions([self.sess.session_id], {self.sess.session_id: self.sess.turn_id})
        await self.pending()
        self.assertEqual((await self.hub.wait_inbox.wait(info["wait_handle"], 0.1))["state"], "ask")
        self.hub.sessions.pop(self.sess.session_id)
        await self.hub.wait_inbox.publish_event({"session_id": self.sess.session_id, "type": "exited", "content": "gone"})
        self.assertEqual((await self.hub.wait_inbox.wait(info["wait_handle"], 0.1))["state"], "failed")
        self.hub.sessions[self.sess.session_id] = self.sess

    async def test_retired_tool_names_answer_with_their_replacement(self):
        async with Client(self.server.mcp) as client:
            names = {t.name for t in await client.list_tools()}
            for old in self.server.RETIRED_TOOL_HINTS:
                self.assertNotIn(old, names)
            result = await client.call_tool(
                "respond_to_subagent_approval_or_choice",
                {"session_id": self.sess.session_id, "request_id": "int-1", "decision": "approve"},
                raise_on_error=False,
            )
        self.assertTrue(result.is_error)
        text = result.content[0].text
        self.assertIn("respond_to_subagent", text)
        self.assertIn("send_to_subagent", text)
        self.assertIn("reconnect", text)

    def profile(self, engine, bypass):
        self.hub.profiles[engine] = {"name": engine, "engine": engine, "enabled": True, "kind": "tui",
                                     "command": "unused", "args": [], "bypass_approvals": bypass}

    async def test_all_engines_policy_preview_and_resume_matrix(self):
        for engine in APPROVAL_ENGINES:
            for default in (False, True):
                for override in (None, False, True):
                    with self.subTest(engine=engine, default=default, override=override):
                        self.profile(engine, default)
                        launch = self.hub.resolve_launch(engine, cwd=str(self.root), bypass_approvals=override)
                        expected = default if override is None else override
                        self.assertEqual(launch["high_risk_approval"], not expected)
                        self.assertEqual(launch["approval_source"], "profile" if override is None else "session")
                        for flag in bypass_launch_flags(engine):
                            if flag.startswith("--dangerously") or flag in ("--auto", "--auto-approve"):
                                self.assertEqual(flag in launch["args"], expected)
                        snapshot = dict(launch, bypass_approvals_override=override, task_mode="codex_positional")
                        record = {"engine": engine, "profile_id": engine, "profile_snapshot": snapshot,
                                  "conversation_ref": {"session_id": "fixture", "session_file": "fixture",
                                                       "session_dir": str(self.root), "brain_dir": str(self.root)}}
                        self.hub.profiles[engine]["bypass_approvals"] = not default
                        resumed = self.hub.build_resume_launch(record)
                        self.assertEqual(resumed["bypass_approvals"], (not default) if override is None else override)

    async def test_policy_persistence_and_legacy_resume(self):
        self.profile("codex", False)
        self.sess.profile_snapshot = dict(self.hub.resolve_launch("codex", cwd=str(self.root), bypass_approvals=True))
        await self.hub.persist_session(self.sess)
        await self.hub.db.flush()
        record = await self.hub.db.get_session_record(self.sess.session_id)
        self.assertIs(record["profile_snapshot"]["bypass_approvals_override"], True)
        self.assertEqual(self.sess.to_dict()["approval_source"], "session")
        record["conversation_ref"] = {"session_id": "fixture"}
        record["profile_snapshot"].pop("bypass_approvals_override")
        self.assertFalse(self.hub.build_resume_launch(record)["bypass_approvals"])

    async def test_http_preview_create_and_invalid_values(self):
        self.profile("codex", True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as client:
            preview = await client.post("/api/launch-preview", json={"profile_id": "codex", "cwd": str(self.root), "bypass_approvals": False})
            self.assertTrue(preview.json()["high_risk_approval"])
            self.assertEqual(preview.json()["approval_source"], "session")
            bad = await client.post("/api/sessions", json={"profile_id": "codex", "bypass_approvals": "false"})
            self.assertEqual(bad.status_code, 400)
            async def create(**kw):
                self.assertIs(kw["bypass_approvals"], False)
                return self.sess
            with patch.object(self.hub, "create_session", create):
                response = await client.post("/api/sessions", json={"profile_id": "codex", "bypass_approvals": False})
                self.assertEqual(response.status_code, 200)

    async def test_d13_launch_notes_and_resume_keep_an_explicit_mode(self):
        for engine in ("codex", "claude", "kimi"):
            self.profile(engine, False)
        codex = self.hub.resolve_launch("codex", cwd=str(self.root))
        self.assertEqual((codex["approval_mode"], codex["approval_source"]), ("auto", "profile"))
        self.assertEqual(codex["args"][0], "--approve-for-me")
        self.assertIn(AUTO_ON_NOTE, codex["notes"])
        kimi = self.hub.resolve_launch("kimi", cwd=str(self.root), approval_mode="auto")
        self.assertEqual(kimi["approval_mode"], "ask")
        self.assertIn(AUTO_UNSUPPORTED_NOTE.format(engine="kimi"), kimi["notes"])
        self.assertNotIn(AUTO_UNSUPPORTED_NOTE.format(engine="kimi"),
                         self.hub.resolve_launch("kimi", cwd=str(self.root))["notes"])
        for requested, resumed_mode in ((None, "auto"), ("ask", "ask")):
            with self.subTest(requested=requested):
                launch = self.hub.resolve_launch("claude", cwd=str(self.root), approval_mode=requested)
                snapshot = dict(launch, task_mode="pty_input")
                record = {"engine": "claude", "profile_id": "claude", "profile_snapshot": snapshot,
                          "conversation_ref": {"session_id": "fixture"}}
                resumed = self.hub.build_resume_launch(record)
                self.assertEqual(resumed["approval_mode"], resumed_mode)
                flag = resumed["args"][resumed["args"].index("--permission-mode") + 1]
                self.assertEqual(flag, "auto" if resumed_mode == "auto" else "acceptEdits")

    async def test_d13_http_and_mcp_forward_approval_mode(self):
        self.profile("codex", False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as client:
            preview = (await client.post("/api/launch-preview", json={
                "profile_id": "codex", "cwd": str(self.root), "approval_mode": "ask"})).json()
            self.assertEqual((preview["approval_mode"], preview["approval_source"]), ("ask", "session"))
            bad = await client.post("/api/launch-preview", json={"profile_id": "codex", "approval_mode": "manual"})
            self.assertFalse(bad.json()["ok"])  # the preview reports invalid input in-band
            self.assertIn("approval_mode", bad.json()["error"])
            clash = await client.post("/api/sessions", json={
                "profile_id": "codex", "approval_mode": "ask", "bypass_approvals": True})
            self.assertEqual(clash.status_code, 400)
        async def create(**kw):
            self.assertEqual(kw["approval_mode"], "ask")
            self.sess.profile_snapshot = {"engine": "codex", "approval_mode": "ask", "approval_mode_override": "ask",
                                          "bypass_approvals": False, "approval_source": "session"}
            return self.sess
        with patch.object(self.hub, "create_session", create):
            async with Client(self.server.mcp) as client:
                tools = {t.name: t for t in await client.list_tools()}
                self.assertIn("approval_mode", tools["spawn_subagent"].inputSchema["properties"])
                result = await client.call_tool("spawn_subagent", {"agent_type": "codex", "approval_mode": "ask"})
                data = result.data if result.data is not None else json.loads(result.content[0].text)
                self.assertEqual(data["approval"]["approval_mode"], "ask")

    async def test_mcp_spawn_schema_and_override(self):
        async def create(**kw):
            self.assertIs(kw["bypass_approvals"], False)
            self.sess.profile_snapshot = {"engine": "codex", "bypass_approvals": False,
                                          "bypass_approvals_override": False, "approval_source": "session"}
            return self.sess
        with patch.object(self.hub, "create_session", create):
            async with Client(self.server.mcp) as client:
                result = await client.call_tool("spawn_subagent", {"agent_type": "codex", "bypass_approvals": False, "wait_seconds": 0})
                data = result.data if result.data is not None else json.loads(result.content[0].text)
                self.assertEqual(data["approval"]["approval_source"], "session")


# A real cut-off delete of 2026-11-13 (docs/omp-approval-prompts-handoff.md): the box shows the start of
# the command, the transcript's tool call has all of it.
FULL_DELETE = ("cd wt && rm -rf ../scratch/bitetest && mkdir -p ../scratch/bitetest && cp *.py ../scratch/bitetest/ "
               "&& cp -r tools ../scratch/bitetest/ && cd ../scratch/bitetest && py -3.15 -B -m unittest "
               "test_terminal_fixtures 2>&1 | tail -5")


def omp_box(*command_rows, reason="Prompt required by bash pattern: * rm -r*"):
    rows = [f"│ Reason: {reason}"] + [f"│ {row}" for row in command_rows] + ["│ ❯ Approve", "│   Deny"]
    return "\n".join(["Working...", "╭─ Allow tool: bash ─────────────╮", *rows, "╰─────────────────────────────────╯"])


class OmpApprovalBoxTests(unittest.IsolatedAsyncioTestCase):
    """An omp approval box always reaches the parent with the full command, or says it is cut."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-omp-box-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()
        self.sess = AgentSession("omp-box", "omp", "OMP", "tui", "Test", str(self.root), "unused", [])
        self.sess.engine = "omp"
        self.sess.status = "running"
        self.sess.pty = FakePty()
        self.sess._loop = asyncio.get_running_loop()
        self.screen = omp_box("Command: cd wt && rm -rf ../scratch/bitetest && mkdir -p ../scratch/bitetest && cp *.p…")
        self.sess.get_clean_screen_text = lambda **kw: self.screen
        self.hub.sessions[self.sess.session_id] = self.sess

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    def open_call(self, call_id, command, tool="bash"):
        record = {"tool_name": tool, "command": command, "args": {"command": command}, "call_id": call_id}
        self.sess._open_transcript_tools[call_id] = record
        self.sess._inflight_transcript_tool = record
        return record

    async def test_the_full_command_comes_from_the_transcript(self):
        self.open_call("c1", FULL_DELETE)
        pending = await self.hub.poll_tui_screen_interactions_once(self.sess)
        meta = pending["metadata"]
        self.assertEqual(meta["command"], FULL_DELETE)
        self.assertEqual(meta["command_source"], "transcript")
        self.assertFalse(meta["command_truncated"])
        self.assertEqual(meta["call_id"], "c1")
        self.assertIn(f"Command: {FULL_DELETE}", pending["prompt"])
        self.assertIn("Reason: Prompt required by bash pattern: * rm -r*", pending["prompt"])
        self.assertNotIn("…", pending["prompt"])  # the box's cut copy is not repeated

    async def test_a_call_read_after_the_box_is_still_used(self):
        async def late():
            await asyncio.sleep(0.2)
            self.open_call("c1", FULL_DELETE)
        task = asyncio.ensure_future(late())
        pending = await self.hub.poll_tui_screen_interactions_once(self.sess)
        await task
        self.assertEqual(pending["metadata"]["command"], FULL_DELETE)
        self.assertEqual(pending["metadata"]["command_source"], "transcript")

    async def test_side_by_side_calls_pair_by_command(self):
        self.open_call("c1", FULL_DELETE)
        self.open_call("c2", "cd wt && git status --short")
        pending = await self.hub.poll_tui_screen_interactions_once(self.sess)
        self.assertEqual(pending["metadata"]["call_id"], "c1")
        self.assertEqual(pending["metadata"]["command"], FULL_DELETE)

    async def test_a_cut_command_without_its_call_says_so(self):
        self.open_call("c9", "cd elsewhere && rm -rf build")  # another command: never borrowed
        with patch("session_manager.OMP_BOX_CALL_WAIT_SEC", 0.1):
            pending = await self.hub.poll_tui_screen_interactions_once(self.sess)
        meta = pending["metadata"]
        self.assertEqual(meta["command_source"], "screen")
        self.assertTrue(meta["command_truncated"])
        self.assertTrue(meta["command"].endswith("…"))
        self.assertIsNone(meta["call_id"])
        self.assertIn(CLIPPED_COMMAND_NOTE, pending["prompt"])
        event = next(e for e in self.hub.get_worker_events(self.sess.session_id, 0) if e["type"] == "approval_request")
        self.assertIn(CLIPPED_COMMAND_NOTE, event["content"])
        info = await self.hub.wait_inbox.ensure_for_sessions([self.sess.session_id], {self.sess.session_id: self.sess.turn_id})
        result = await self.hub.wait_inbox.wait(info["wait_handle"], 1)
        self.assertEqual(result["state"], "ask")
        self.assertIn(CLIPPED_COMMAND_NOTE, json.dumps(result["requests"][0], ensure_ascii=False))

    async def test_a_whole_short_command_needs_no_transcript(self):
        self.screen = omp_box("Command: rm -rf scratch/x")
        pending = await self.hub.poll_tui_screen_interactions_once(self.sess)
        self.assertEqual(pending["metadata"]["command"], "rm -rf scratch/x")
        self.assertEqual(pending["metadata"]["command_source"], "screen")
        self.assertFalse(pending["metadata"]["command_truncated"])
        self.assertNotIn(CLIPPED_COMMAND_NOTE, pending["prompt"])


class ApprovalBoxMatchingTests(unittest.TestCase):
    def call(self, command, tool="bash", call_id="c"):
        return {"tool_name": tool, "command": command, "call_id": call_id}

    def test_box_prefix_matches_across_wraps_and_elisions(self):
        full = self.call("cat > a.py <<'PY'\nprint('x')\nPY\npy -3 a.py && rm -rf build")
        self.assertIs(tool_call_for_approval_box("bash", "cat > a.py <<'PY' print('x') PY py -3 a.…", [full]), full)
        self.assertIs(tool_call_for_approval_box("bash", "cat > a.py <<'PY' pr[…40ch elided…]", [full]), full)
        self.assertIsNone(tool_call_for_approval_box("bash", "cat > b.py…", [full]))
        self.assertIsNone(tool_call_for_approval_box("edit", "cat > a.py…", [full]))

    def test_unclear_boxes_match_nothing(self):
        a, b = self.call("cd wt && rm -rf x", call_id="a"), self.call("cd wt && rm -rf y", call_id="b")
        self.assertIsNone(tool_call_for_approval_box("bash", "cd wt && rm -rf …", [a, b]))
        self.assertIs(tool_call_for_approval_box("bash", "cd wt && rm -rf y", [a, b]), b)
        edit = self.call("src/x.py", tool="edit")
        self.assertIs(tool_call_for_approval_box("edit", "", [a, edit]), edit)

    def test_clipped_detection(self):
        self.assertTrue(screen_command_clipped("rm -rf scratch/bi…"))
        self.assertTrue(screen_command_clipped("cat x […120ch elided…]"))
        self.assertFalse(screen_command_clipped("rm -rf scratch/bitetest"))


if __name__ == "__main__":
    unittest.main()
