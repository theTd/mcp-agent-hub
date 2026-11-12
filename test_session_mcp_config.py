"""Structured MCP launch, approval settings and resume contracts; no model calls."""
import json
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

import httpx
from fastmcp import Client

from session_manager import AgentSession
from session_mcp_config import (normalize_mcp_config, build_mcp_args, check_mcp_arg_conflicts)
import test_session_launch_args as launch_fixture


SERVERS = {
    "docs": {"url": "https://example.invalid/mcp", "headers": {"X-Tenant": "demo"}},
    "local_tools": {"command": "node", "args": ["E:/中文 with spaces/server.mjs", "", 'a"\\\n😀'],
                    "env": {"MODE": "readonly", "odd.key": "$(whoami)"}},
}


def settings(args):
    values = [json.loads(arg.split("=", 1)[1]) for arg in args if arg.startswith("--settings=")]
    if len(values) != 1:
        raise AssertionError(f"expected exactly one merged settings argument, got {len(values)}")
    return values[0]


class McpConfigTests(unittest.TestCase):
    def test_validation_and_detached_copy(self):
        config = normalize_mcp_config(SERVERS, ["old", "old"])
        self.assertEqual(config["disabled_mcp_servers"], ["old"])
        config["mcp_servers"]["local_tools"]["args"].append("changed")
        self.assertNotIn("changed", SERVERS["local_tools"]["args"])
        self.assertEqual(normalize_mcp_config(), {"mcp_servers": {}, "disabled_mcp_servers": [], "strict_mcp_config": False})

    def test_invalid_input(self):
        invalid = [
            {"servers": []}, {"disabled": "old"}, {"disabled": [1]}, {"disabled": [" old"]},
            {"disabled": [""]}, {"strict": None}, {"strict": 1},
            {"servers": {"bad.name": {"command": "node"}}},
            {"servers": {"x": []}}, {"servers": {"x": {}}},
            {"servers": {"x": {"url": "https://a", "command": "node"}}},
            {"servers": {"x": {"command": ""}}}, {"servers": {"x": {"url": "file:///a"}}},
            {"servers": {"x": {"url": "https://a", "env": {}}}},
            {"servers": {"x": {"command": "node", "args": "x"}}},
            {"servers": {"x": {"command": "node", "args": [1]}}},
            {"servers": {"x": {"command": "node", "env": {"a": 1}}}},
            {"servers": {"x": {"url": "https://a", "headers": []}}},
            {"servers": {"x": {"command": "node\x00"}}},
            {"servers": {"x": {"command": "node", "args": ["\x00"]}}},
            {"servers": {"x": {"command": "node"}}, "disabled": ["x"]},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                normalize_mcp_config(**kwargs)

    def test_codex_toml_roundtrip_and_literal_names(self):
        config = normalize_mcp_config(SERVERS, ['old.with.dots', 'a"b\\c'])
        args = build_mcp_args("codex", config)
        parsed = {}
        for i in range(0, len(args), 2):
            self.assertEqual(args[i], "-c")
            parsed.update(tomllib.loads(args[i + 1])["mcp_servers"])
        self.assertEqual(parsed["docs"], {"url": SERVERS["docs"]["url"], "http_headers": {"X-Tenant": "demo"}, "enabled": True})
        self.assertEqual(parsed["local_tools"], dict(SERVERS["local_tools"], enabled=True))
        self.assertEqual(parsed['a"b\\c'], {"enabled": False})
        self.assertEqual(parsed['old.with.dots'], {"enabled": False})

    def test_claude_json_and_empty_strict(self):
        args = build_mcp_args("claude", normalize_mcp_config(SERVERS, strict=True))
        config = json.loads(args[0].split("=", 1)[1])["mcpServers"]
        self.assertEqual(config["docs"], dict(SERVERS["docs"], type="http"))
        self.assertEqual(config["local_tools"], dict(SERVERS["local_tools"], type="stdio"))
        self.assertEqual(args[-1], "--strict-mcp-config")
        self.assertEqual(build_mcp_args("claude", normalize_mcp_config(strict=True)),
                         ['--mcp-config={"mcpServers":{}}', '--strict-mcp-config'])

    def test_native_conflict_variants(self):
        cases = {
            "codex": [["-c", "mcp_servers.x.enabled=false"], ["--config=mcp_servers={}"],
                      ['-cmcp_servers."x".url="https://a"'], ['-c=mcp_servers.x.enabled=true'],
                      ["--config", '"mcp_servers".x.enabled=false'], ["-c", 'plugins."p".enabled=false']],
            "claude": [["--mcp-config=x.json"], ["--mcp-config", "{}"], ["--strict-mcp-config"],
                       ["--plugin-dir", "x"], ["--settings", '{"deniedMcpServers":[]}'],
                       ['--settings={"disableClaudeAiConnectors":true}']],
        }
        for engine, variants in cases.items():
            for args in variants:
                with self.subTest(engine=engine, args=args), self.assertRaises(ValueError):
                    check_mcp_arg_conflicts(engine, args, ".")
        check_mcp_arg_conflicts("codex", ["-c", 'model_reasoning_effort="high"'], ".")
        check_mcp_arg_conflicts("claude", ['--settings={"env":{"M":"x"}}'], ".")


class SessionMcpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = launch_fixture.SessionLaunchArgsTests.asyncSetUp
    asyncTearDown = launch_fixture.SessionLaunchArgsTests.asyncTearDown

    async def test_preview_creation_and_durable_resume(self):
        for engine in ("codex", "claude"):
            with self.subTest(engine=engine):
                config = dict(mcp_servers=SERVERS, disabled_mcp_servers=["old"], strict_mcp_config=engine == "claude")
                preview = self.hub.resolve_launch(engine, cwd=str(self.root), **config)
                with patch.object(AgentSession, "start_pty"), patch.object(self.hub, "_schedule", lambda coro: coro.close()):
                    sess = await self.hub.create_session(engine, cwd=str(self.root), task="check MCP", **config)
                self.assertEqual(sess.profile_snapshot["args"], preview["args"])
                for key, value in config.items():
                    self.assertEqual(sess.profile_snapshot[key], value)
                await self.hub.db.flush()
                record = await self.hub.db.get_session_record(sess.session_id)
                record["conversation_ref"] = {"session_id": "fixture"}
                self.hub.profiles[engine]["args"] = ["--changed"]
                resumed = self.hub.build_resume_launch(record)
                self.assertNotIn("--changed", resumed["args"])
                if engine == "claude":
                    self.assertEqual(resumed["args"].count("--strict-mcp-config"), 1)
                    self.assertEqual(len([a for a in resumed["args"] if a.startswith("--mcp-config=")]), 1)
                    self.assertEqual(settings(resumed["args"]), settings(preview["args"]))
                else:
                    self.assertEqual(resumed["args"].count('-c'), 1)
                self.assertEqual(self.hub.profiles[engine]["args"], ["--changed"])

    async def test_claude_settings_merge_and_approval_changes_on_resume(self):
        user = {"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "custom-hook"}]}]},
                "permissions": {"ask": ["Read(private)"], "deny": ["Read(secret)"]}, "env": {"A": "1"}}
        path = self.root / "settings with spaces.json"
        path.write_text(json.dumps(user), encoding="utf-8")
        self.hub.profiles["claude"]["args"] = ["--settings", path.name]
        with patch.object(AgentSession, "start_pty"), patch.object(self.hub, "_schedule", lambda coro: coro.close()):
            sess = await self.hub.create_session("claude", cwd=str(self.root), disabled_mcp_servers=["old"],
                                                 extra_args=['--settings={"env":{"B":"2"}}'])
        merged = settings(sess.args)
        self.assertEqual(merged["env"], {"A": "1", "B": "2"})
        self.assertIn("Read(secret)", merged["permissions"]["deny"])
        # D13: auto mode (the default) adds only the PermissionRequest relay; ask rules stay the user's.
        self.assertEqual(sess.args[sess.args.index("--permission-mode") + 1], "auto")
        self.assertEqual(merged["hooks"]["PreToolUse"], user["hooks"]["PreToolUse"])
        self.assertEqual(len(merged["hooks"]["PermissionRequest"]), 1)
        self.assertEqual(merged["permissions"]["ask"], user["permissions"]["ask"])
        self.assertEqual(merged["deniedMcpServers"], [{"serverName": "old"}])
        with patch.object(AgentSession, "start_pty"), patch.object(self.hub, "_schedule", lambda coro: coro.close()):
            ask_sess = await self.hub.create_session("claude", cwd=str(self.root), disabled_mcp_servers=["old"],
                                                     approval_mode="ask")
        asked = settings(ask_sess.args)
        self.assertEqual(ask_sess.args[ask_sess.args.index("--permission-mode") + 1], "acceptEdits")
        self.assertGreater(len(asked["hooks"]["PreToolUse"]), 1)
        self.assertGreater(len(asked["permissions"]["ask"]), 1)
        record = {"engine": "claude", "profile_id": "claude", "profile_snapshot": sess.profile_snapshot,
                  "conversation_ref": {"session_id": "fixture"}}
        path.unlink()  # Settings content is snapshotted; no live external file needed on resume.
        self.hub.profiles["claude"]["bypass_approvals"] = True
        bypassed = settings(self.hub.build_resume_launch(record)["args"])
        self.assertEqual(bypassed["hooks"], user["hooks"])
        self.assertEqual(bypassed["permissions"], user["permissions"])
        self.assertEqual(bypassed["deniedMcpServers"], merged["deniedMcpServers"])
        self.hub.profiles["claude"]["bypass_approvals"] = False
        self.assertEqual(settings(self.hub.build_resume_launch(record)["args"]), merged)

    async def test_reject_before_spawn_and_preserve_legacy_behavior(self):
        for engine in ("pwsh", "dsh", "agy", "kimi", "omp", "desktop-gui"):
            with self.subTest(engine=engine), self.assertRaises(ValueError):
                await self.hub.create_session(engine, mcp_servers=SERVERS)
        with self.assertRaises(ValueError):
            self.hub.resolve_launch("codex", strict_mcp_config=True)
        self.hub.profiles["claude"]["kind"] = "web_gui"
        with self.assertRaises(ValueError):
            self.hub.resolve_launch("claude", disabled_mcp_servers=["old"])
        self.hub.profiles["codex"]["args"] = ["-c", "mcp_servers.old.enabled=false"]
        self.hub.resolve_launch("codex")  # No structured config: original argv channel still works.
        with self.assertRaises(ValueError):
            self.hub.resolve_launch("codex", mcp_servers=SERVERS)
        self.assertFalse(self.hub.sessions)

    async def test_unreadable_or_conflicting_settings_fail_closed(self):
        for value in ("missing.json", "[]", '{"deniedMcpServers":[]}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.hub.resolve_launch("claude", cwd=str(self.root), mcp_servers=SERVERS,
                                        extra_args=["--settings", value])
        bad = self.root / "bad.json"
        bad.write_text('{"disabledMcpjsonServers":["old"]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.hub.resolve_launch("claude", cwd=str(self.root), mcp_servers=SERVERS,
                                    extra_args=["--settings=" + str(bad)])

    async def test_http_and_mcp_share_fields_and_errors(self):
        config = dict(mcp_servers=SERVERS, disabled_mcp_servers=["old"], strict_mcp_config=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test") as client:
            preview = await client.post("/api/launch-preview", json=dict(profile_id="claude", **config))
            self.assertTrue(preview.json()["ok"])
            self.assertEqual(preview.json()["mcp_servers"], SERVERS)
            for invalid in (dict(mcp_servers=[]), dict(strict_mcp_config=None), dict(disabled_mcp_servers="x")):
                self.assertFalse((await client.post("/api/launch-preview", json=dict(profile_id="claude", **invalid))).json()["ok"])
                self.assertEqual((await client.post("/api/sessions", json=dict(profile_id="claude", **invalid))).status_code, 400)
            with patch.object(AgentSession, "start_pty"), patch.object(self.hub, "_schedule", lambda coro: coro.close()):
                response = await client.post("/api/sessions", json=dict(profile_id="claude", **config))
                self.assertEqual(response.status_code, 200)
                async with Client(self.server.mcp) as mcp_client:
                    schema = next(t for t in await mcp_client.list_tools() if t.name == "spawn_subagent").input_schema
                    for key in config:
                        self.assertIn(key, schema["properties"])
                    result = await mcp_client.call_tool("spawn_subagent", dict(agent_type="claude", wait_seconds=0, **config))
                    data = result.data if result.data is not None else json.loads(result.content[0].text)
                    self.assertIn(data["session_id"], self.hub.sessions)
        for sess in self.hub.sessions.values():
            self.assertEqual(sess.profile_snapshot["args"], preview.json()["args"])

    def test_portable_manifest_contains_helper(self):
        root = Path(__file__).parent
        self.assertIn("session_mcp_config.py", (root / "desktop/portable/payload.txt").read_text(encoding="utf-8").splitlines())


if __name__ == "__main__":
    unittest.main()
