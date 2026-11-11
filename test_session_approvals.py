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
                             bypass_launch_flags, normalize_auto_approval_rules)
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
        before = self.hub.global_seq
        pending = await self.pending()
        result = await self.server.wait_subagent_events(self.sess.session_id, before, timeout_seconds=1)
        self.assertEqual(self.sess.status, "waiting_parent")
        self.assertEqual(result["next_action"], "reply")
        self.assertEqual(result["pending_interactions"][0]["request_id"], pending["request_id"])
        await self.pending()
        self.assertEqual(len([e for e in self.hub.get_worker_events(self.sess.session_id, before)
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
                self.assertEqual(data["approval_source"], "session")


if __name__ == "__main__":
    unittest.main()
