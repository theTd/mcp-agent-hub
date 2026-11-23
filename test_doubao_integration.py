"""Doubao CLI engine: session records, discovery, launch argv, approvals, watchers and resume.

No engine process and no account: subprocess calls and the PTY are scripted, and the watcher runs
against a synthetic `doubao_cli` layout plus the fake terminal core.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from context_window_usage import SUPPORTED_CONTEXT_ENGINES, resolve_native_transcript_path
from doubao_session_record import (
    detect_doubao_error,
    export_doubao_session,
    extract_doubao_screen_reply,
    find_doubao_session_by_marker,
    parse_doubao_exit_resume_id,
    parse_doubao_prompt,
    parse_doubao_status_session_id,
    parse_doubao_trajectory_record,
    resolve_doubao_binary,
)
from fake_terminal_core import FakeTerminal
from model_discovery import AgentModelDiscovery
from session_manager import (
    APPROVAL_ENGINES,
    KNOWN_ENGINES,
    RESUME_ENGINES,
    TUI_READY_BANNERS,
    AgentSession,
    HubSessionManager,
)

DISCOVERY = {
    "engines": {
        "doubao": {
            "engine": "doubao",
            "name": "豆包 CLI",
            "installed": True,
            "binary": "doubao",
            "default_model": "9",
            "default_thinking_level": "high",
            "available_models": ["9", "seed-lite-7b"],
            "available_thinking_levels": ["low", "medium", "high", "xhigh", "max"],
            "model_details": [
                {"id": "9", "display_name": "Auto", "supported_thinking_levels": ["low", "medium", "high"],
                 "default_thinking_level": "high"},
                {"id": "seed-lite-7b", "display_name": "豆包 2.1 Lite",
                 "supported_thinking_levels": ["low", "medium", "high"], "default_thinking_level": "medium"},
            ],
        }
    }
}

BUSY_SCREEN = (
    "DOUBAO CLI\n"
    "✦ Responding… (8s • esc to interrupt)\n"
    "❯  Ask Doubao to do anything\n"
    "Auto · high · E:\\work"
)


def idle_screen(answer: str) -> str:
    return (
        "DOUBAO CLI\n"
        f"◆ {answer}\n"
        "❯  Ask Doubao to do anything\n"
        "Auto · high · E:\\work"
    )


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-doubao-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()


class DoubaoSessionRecordTests(TempCase):
    def assignment(self, conversation: str, marker: str, *, agent: str = "a1") -> Path:
        path = (
            self.root / "agent_mode" / "u1" / "workspace" / ".sessions" / conversation
            / "agents" / agent / "system" / "assignment.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Assignment\n\n[Hub Session: session_id={marker}]\n", encoding="utf-8")
        trajectory = path.parent / "trajectory.jsonl"
        trajectory.write_text('{"role":"assistant","content":"hi"}\n', encoding="utf-8")
        return path

    def test_marker_binds_exactly_one_conversation_and_its_trajectory(self):
        self.assignment("conv-1", "hub-a")
        second = self.assignment("conv-2", "hub-b")
        os.utime(second, (time.time() - 3600, time.time() - 3600))
        found = find_doubao_session_by_marker("hub-a", created_at=time.time() - 30, data_root=self.root)
        self.assertIsNotNone(found)
        conversation, trajectories = found
        self.assertEqual(conversation, "conv-1")
        self.assertEqual([path.name for path in trajectories], ["trajectory.jsonl"])
        self.assertEqual(trajectories[0].parent.parent.parent.parent.name, "conv-1")

    def test_marker_prefix_and_stale_assignment_never_match(self):
        path = self.assignment("conv-1", "hub-a")
        os.utime(path, (time.time() - 3600, time.time() - 3600))
        self.assertIsNone(
            find_doubao_session_by_marker("hub-a", created_at=time.time() - 30, data_root=self.root)
        )
        self.assertIsNone(
            find_doubao_session_by_marker("hub", created_at=0, data_root=self.root)
        )

    def test_missing_root_is_quiet(self):
        self.assertIsNone(
            find_doubao_session_by_marker("hub-a", created_at=0, data_root=self.root / "nope")
        )

    def test_trajectory_rows_become_normalized_events(self):
        events = parse_doubao_trajectory_record(
            {
                "role": "assistant",
                "content": "先看目录",
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}},
                    {"id": "c2", "type": "function", "function": {"name": "read", "arguments": ""}},
                ],
            }
        )
        self.assertEqual(events[0], {"kind": "assistant_text", "text": "先看目录"})
        self.assertEqual(events[1]["kind"], "tool_call")
        self.assertEqual(events[1]["name"], "bash")
        self.assertEqual(events[1]["arguments"], {"command": "ls"})
        self.assertEqual(events[2]["arguments"], "")
        tool = parse_doubao_trajectory_record({"role": "tool", "tool_call_id": "c1", "content": "a.txt"})
        self.assertEqual(tool, [{"kind": "tool_result", "call_id": "c1", "name": "", "content": "a.txt"}])
        user = parse_doubao_trajectory_record({"role": "user", "content": [{"type": "text", "text": "继续"}]})
        self.assertEqual(user, [{"kind": "user_text", "text": "继续"}])
        self.assertEqual(parse_doubao_trajectory_record("not a record"), [])
        self.assertEqual(parse_doubao_trajectory_record({"role": "system", "content": "x"}), [])

    def test_export_parses_json_array_and_never_raises(self):
        args = []
        def run(argv, **kwargs):
            args.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout='log line\n[{"query":"q","response":"r"}]\n', stderr="")
        with patch("doubao_session_record.subprocess.run", side_effect=run):
            rows = export_doubao_session("doubao.exe", "384", timeout=3.0)
        self.assertEqual(rows, [{"query": "q", "response": "r"}])
        self.assertEqual(args[0][0][:3], ["doubao.exe", "--export", "384"])
        self.assertEqual(args[0][1]["env"]["DOUBAO_CLI_NO_UPDATE_CHECK"], "1")
        with patch("doubao_session_record.subprocess.run",
                   return_value=SimpleNamespace(returncode=1, stdout="[]", stderr="x")):
            self.assertIsNone(export_doubao_session("doubao.exe", "384"))
        with patch("doubao_session_record.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout="not json", stderr="")):
            self.assertIsNone(export_doubao_session("doubao.exe", "384"))
        with patch("doubao_session_record.subprocess.run", side_effect=TimeoutError("slow")):
            self.assertIsNone(export_doubao_session("doubao.exe", "384"))
        self.assertIsNone(export_doubao_session("doubao.exe", ""))

    def test_exit_banner_and_status_ids(self):
        self.assertEqual(
            parse_doubao_exit_resume_id("Resume this session with: doubao --resume '38455800358'"),
            "38455800358",
        )
        self.assertEqual(
            parse_doubao_exit_resume_id('doubao --resume "384-abc"'), "384-abc"
        )
        self.assertIsNone(parse_doubao_exit_resume_id("no banner"))
        self.assertEqual(parse_doubao_status_session_id("Session: 38455800358\nother"), "38455800358")
        self.assertIsNone(parse_doubao_status_session_id("Session:"))
        self.assertIsNone(parse_doubao_status_session_id("no status"))

    def test_screen_reply_is_the_latest_assistant_block_only(self):
        lines = [
            "DOUBAO CLI",
            "▍ do it",
            "◆ 旧答案",
            "❯  Ask Doubao to do anything",
            "◆ 第一段",
            "第二段",
            "",
            "❯  Ask Doubao to do anything",
            "Auto · high · E:\\work",
        ]
        self.assertEqual(extract_doubao_screen_reply(lines), "第一段\n第二段")
        self.assertEqual(extract_doubao_screen_reply("\n".join(lines)), "第一段\n第二段")
        self.assertEqual(extract_doubao_screen_reply(["DOUBAO CLI", "❯  Ask Doubao"]), "")
        self.assertEqual(extract_doubao_screen_reply(["◆", "below"]), "below")

    def test_error_codes_classify_to_the_engine_error_kinds(self):
        cases = {
            "Error: auth_failed: login required": "auth",
            "SESSION_EXPIRED": "auth",
            "Too many requests, retry later": "rate_limit",
            "network_offline: 网络不可用": "network",
            "server_error: 503": "server",
            "invalid_arguments: 无法识别参数 -C": "request",
            "permission_denied: 无权限写入": "auth",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                info = detect_doubao_error(text)
                self.assertIsNotNone(info)
                self.assertEqual(info["kind"], kind)
                self.assertEqual(info["message"], text.strip().splitlines()[0])
        self.assertIsNone(detect_doubao_error("完成：见 a.txt"))
        envelope = detect_doubao_error('{"is_error": true, "errors": [{"code": "server_error"}]}')
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope["kind"], "server")

    def test_assistant_prose_that_names_error_codes_is_not_an_engine_error(self):
        prose = (
            "◆ `internal_error` and `auth_failed` are the CLI's own error codes.\n"
            "A task whose title mentions internal_error still runs normally."
        )
        self.assertIsNone(detect_doubao_error(prose))
        self.assertIsNone(detect_doubao_error("auth_failed and internal_error are documented here"))
        self.assertEqual(
            detect_doubao_error("◆ 说明\n\nauth_failed: 登录已过期")["code"], "auth_failed"
        )

    def test_approval_card_parse_marks_choices_and_fails_safe(self):
        screen = (
            "Allow this command?\n"
            "  pnpm build\n"
            "❯ 1. Allow once\n"
            "  2. Allow similar commands\n"
            "  3. Reject\n"
            "  4. Reject and stop\n"
            "  Submit\n"
        )
        parsed = parse_doubao_prompt(screen)
        self.assertEqual(parsed["kind"], "approval_request")
        self.assertEqual(parsed["title"], "Allow this command?")
        self.assertEqual(parsed["options"],
                         ["Allow once", "Allow similar commands", "Reject", "Reject and stop"])
        self.assertEqual(parsed["option_actions"], ["approve_once", "approve_session", "deny", "deny_feedback"])
        unknown = parse_doubao_prompt(
            "Files will be deleted?\n❯ 1. Continue anyway\n  2. Abort\n  Submit\n"
        )
        self.assertEqual(unknown["option_actions"], ["approve_once", "deny"])
        self.assertIsNone(parse_doubao_prompt("Answer:\n1. a\n2. b\n"))

    def test_question_card_needs_a_cursor_led_numbered_list(self):
        screen = "Which branch?\n❯ 1. main\n  2. dev\n❯  Ask Doubao to do anything\n"
        parsed = parse_doubao_prompt(screen)
        self.assertEqual(parsed["kind"], "question")
        self.assertEqual(parsed["prompt"], "Which branch?")
        self.assertEqual(parsed["options"], ["main", "dev"])
        self.assertIsNone(parse_doubao_prompt("Which branch?\n1. main\n2. dev\n❯  Ask Doubao to do anything\n"))

    def test_prose_and_idle_composer_never_become_cards(self):
        prose = (
            "◆ The CLI would draw this dialog:\n"
            "Allow this command?\n"
            "❯ 1. Allow once\n"
            "  2. Reject\n"
        )
        self.assertIsNone(parse_doubao_prompt(prose))
        draft = "◆ Earlier question?\n❯ 1. draft text\n"
        self.assertIsNone(parse_doubao_prompt(draft))
        # The same card below an answer (its own UI region) still parses.
        real = (
            "◆ 需要你确认\n"
            "\n"
            "Allow this command?\n"
            "  pnpm build\n"
            "❯ 1. Allow once\n"
            "  2. Reject\n"
            "  Submit\n"
        )
        self.assertEqual(parse_doubao_prompt(real)["kind"], "approval_request")

    def test_question_options_stop_at_status_and_key_hint_rows(self):
        for tail in ("Auto · high · E:\\work", "↑↓ navigate · Enter to select"):
            with self.subTest(tail=tail):
                parsed = parse_doubao_prompt(f"Which branch?\n❯ 1. main\n  2. dev\n{tail}\n")
                self.assertEqual(parsed["options"], ["main", "dev"])

    def test_binary_resolution_prefers_the_native_exe(self):
        native = self.root / "doubao_cli" / "app" / "current" / "doubao.exe"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            self.assertEqual(resolve_doubao_binary(), str(native))
            self.assertEqual(resolve_doubao_binary("custom.exe"), str(native))
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "empty")}), \
                patch("doubao_session_record.shutil.which", return_value=None):
            self.assertEqual(resolve_doubao_binary("my-doubao.exe"), "my-doubao.exe")
            self.assertEqual(resolve_doubao_binary(), "doubao")


class DoubaoDiscoveryTests(TempCase):
    def test_model_list_is_parsed_with_per_model_efforts(self):
        binary = self.root / "doubao.exe"
        binary.write_bytes(b"")
        stdout = json.dumps(
            {
                "type": "result", "subtype": "success", "is_error": False,
                "result": {"type": "model_list", "models": [
                    {"id": "9", "name": "Auto", "is_default": True,
                     "efforts": ["low", "medium", "high"], "default_effort": "high"},
                    {"id": "glm", "name": "GLM 5.4", "is_default": False,
                     "efforts": ["high", "max"], "default_effort": "max"},
                ]},
            }
        )
        with patch("model_discovery.resolve_doubao_binary", return_value=str(binary)), \
                patch("model_discovery.subprocess.run",
                      return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr="[cli-chat] log")) as run:
            entry = AgentModelDiscovery()._discover_doubao()
        self.assertTrue(entry["installed"])
        self.assertEqual(entry["available_models"], ["9", "glm"])
        self.assertEqual(entry["default_model"], "9")
        self.assertEqual(entry["default_thinking_level"], "high")
        self.assertEqual(entry["available_thinking_levels"], ["low", "medium", "high", "max"])
        self.assertEqual(entry["model_details"][1]["supported_thinking_levels"], ["high", "max"])
        self.assertEqual(entry["model_details"][1]["default_thinking_level"], "max")
        self.assertEqual(entry["cli_flags_template"], {"model": "--model", "thinking": "--effort"})
        self.assertEqual(run.call_args.args[0][:4], [str(binary), "model", "list", "--json"])
        self.assertEqual(run.call_args.kwargs["timeout"], 12.0)
        self.assertEqual(run.call_args.kwargs["env"]["DOUBAO_CLI_NO_UPDATE_CHECK"], "1")

    def test_no_default_flag_takes_the_first_model(self):
        binary = self.root / "doubao.exe"
        binary.write_bytes(b"")
        stdout = json.dumps({"result": {"models": [
            {"id": "a", "name": "A", "efforts": ["low"]},
            {"id": "b", "name": "B", "efforts": []},
        ]}})
        with patch("model_discovery.resolve_doubao_binary", return_value=str(binary)), \
                patch("model_discovery.subprocess.run",
                      return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr="")):
            entry = AgentModelDiscovery()._discover_doubao()
        self.assertEqual((entry["default_model"], entry["default_thinking_level"]), ("a", "low"))

    def test_missing_binary_malformed_json_and_login_errors_are_reported(self):
        with patch("model_discovery.resolve_doubao_binary", return_value=str(self.root / "missing.exe")), \
                patch("model_discovery.subprocess.run") as run:
            entry = AgentModelDiscovery()._discover_doubao()
        self.assertFalse(entry["installed"])
        self.assertEqual(entry["available_models"], [])
        self.assertEqual(entry["discovery_error"], "doubao binary not found")
        run.assert_not_called()

        binary = self.root / "doubao.exe"
        binary.write_bytes(b"")
        with patch("model_discovery.resolve_doubao_binary", return_value=str(binary)), \
                patch("model_discovery.subprocess.run",
                      return_value=SimpleNamespace(returncode=0, stdout="not json", stderr="")):
            broken = AgentModelDiscovery()._discover_doubao()
        self.assertTrue(broken["installed"])
        self.assertEqual(broken["discovery_error"], "doubao model list returned no JSON")

        with patch("model_discovery.resolve_doubao_binary", return_value=str(binary)), \
                patch("model_discovery.subprocess.run",
                      return_value=SimpleNamespace(
                          returncode=0,
                          stdout=json.dumps({"type": "result", "is_error": True, "result": "not logged in"}),
                          stderr="")):
            denied = AgentModelDiscovery()._discover_doubao()
        self.assertTrue(denied["installed"])
        self.assertEqual(denied["available_models"], [])
        self.assertEqual(denied["discovery_error"], "not logged in")


class DoubaoLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-doubao-launch-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.hub.load_profiles(discovery=DISCOVERY)
        self.fake = FakeTerminal()
        self.hub.core = self.fake
        await self.hub.start()

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    def test_engine_is_registered_with_a_tui_profile(self):
        for engine in (KNOWN_ENGINES, RESUME_ENGINES, APPROVAL_ENGINES):
            self.assertIn("doubao", engine)
        profile = self.hub.profiles["doubao"]
        self.assertEqual(profile["engine"], "doubao")
        self.assertEqual(profile["kind"], "tui")
        self.assertEqual(profile["task_mode"], "pty_input")
        self.assertEqual(profile["startup_delay_sec"], 3.0)
        self.assertEqual(profile["submit_key"], "\r")
        self.assertTrue(profile["enabled"])

    def test_ready_banner_is_the_composer_phrase_on_the_block_art_screen(self):
        # The live 120x36 ConPTY draws the title as Unicode block art, never as "DOUBAO CLI".
        startup_screen = (
            "█▀▄ █▀█ █ █ █▀▄ █▀█ █▀█\n"
            "█▄▀ █▄█ █▄█ █▄█ █▀█ █▄█\n"
            "\n"
            "Ask Doubao to do anything\n"
            "❯  Ask Doubao to do anything\n"
            "Auto · high · E:\\work\n"
        )
        self.assertEqual(TUI_READY_BANNERS["doubao"], "Ask Doubao to do anything")
        self.assertNotIn("DOUBAO CLI", startup_screen)
        self.assertTrue(
            self.hub._tui_banner_visible(
                SimpleNamespace(), startup_screen, TUI_READY_BANNERS["doubao"]
            )
        )

    def test_cwd_leads_every_business_flag(self):
        launch = self.hub.resolve_launch("doubao", cwd=str(self.root))
        args = launch["args"]
        self.assertEqual(args[:2], ["-C", str(self.root)])
        self.assertEqual(args.index("--permission-mode"), 2)
        self.assertEqual(args[3], "ask_risky")
        self.assertIn("--model", args)
        self.assertIn("--effort", args)
        self.assertEqual(launch["approval_mode"], "ask")
        self.assertEqual(launch["env"]["DOUBAO_CLI_NO_UPDATE_CHECK"], "1")

    def test_profile_duplicates_the_hub_owns_are_replaced(self):
        self.hub.profiles["doubao"]["args"] = [
            "-C", "D:\\elsewhere",
            "--model", "stale",
            "--permission-mode", "always_allow",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        args = self.hub.resolve_launch("doubao", cwd=str(self.root), approval_mode="ask")["args"]
        self.assertEqual(args[0:2], ["-C", str(self.root)])
        self.assertEqual(args.count("-C"), 1)
        self.assertEqual(args.count("--model"), 1)
        self.assertEqual(args[args.index("--model") + 1], "9")
        self.assertEqual(args.count("--permission-mode"), 1)
        self.assertEqual(args[args.index("--permission-mode") + 1], "ask_risky")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertNotIn("always_allow", args)

    def test_bypass_and_auto_modes_never_send_both_flags(self):
        auto = self.hub.resolve_launch("doubao", cwd=str(self.root), approval_mode="auto")
        self.assertEqual(auto["approval_mode"], "ask")
        self.assertIn("--permission-mode", auto["args"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", auto["args"])
        bypass = self.hub.resolve_launch("doubao", cwd=str(self.root), approval_mode="bypass")
        self.assertTrue(bypass["bypass_approvals"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", bypass["args"])
        self.assertNotIn("--permission-mode", bypass["args"])

    def test_extra_args_are_accepted_and_env_override_wins(self):
        launch = self.hub.resolve_launch(
            "doubao", cwd=str(self.root), extra_args=["--custom", "value"],
            env={"DOUBAO_CLI_NO_UPDATE_CHECK": "0"},
        )
        self.assertEqual(launch["args"][-2:], ["--custom", "value"])
        self.assertEqual(launch["env"]["DOUBAO_CLI_NO_UPDATE_CHECK"], "0")

    def test_resume_leads_with_cwd_and_carries_the_conversation_id(self):
        snapshot = dict(self.hub.resolve_launch("doubao", cwd=str(self.root)))
        snapshot["args"] = list(snapshot["args"])
        record = {
            "engine": "doubao", "profile_id": "doubao", "cwd": str(self.root),
            "profile_snapshot": snapshot,
            "conversation_ref": {"session_id": "38455800358", "cwd": str(self.root)},
        }
        built = self.hub.build_resume_launch(record)
        args = built["args"]
        self.assertEqual(args[:2], ["-C", str(self.root)])
        self.assertEqual(args.count("-C"), 1)
        self.assertEqual(args.index("--resume"), 2)
        self.assertEqual(args[3], "38455800358")
        self.assertEqual(built["message_mode"], "pty_input")
        self.assertEqual(built["env"]["DOUBAO_CLI_NO_UPDATE_CHECK"], "1")
        self.assertEqual(self.hub.compute_resumable(record), (True, ""))
        missing = dict(record, conversation_ref={"session_id": "  "})
        self.assertFalse(self.hub.compute_resumable(missing)[0])

    async def test_resumed_session_keeps_the_conversation_ref(self):
        snapshot = dict(self.hub.resolve_launch("doubao", cwd=str(self.root)))
        ref = {"session_id": "38455800358", "cwd": str(self.root)}
        record = {
            "engine": "doubao", "profile_id": "doubao", "profile_name": "豆包 CLI",
            "cwd": str(self.root), "title": "t", "created_at": time.time(),
            "profile_snapshot": snapshot, "conversation_ref": ref,
        }
        built = self.hub.build_resume_launch(record)
        sess = await self.hub._make_resumed_session(
            "doubao-resumed", record, record["profile_snapshot"], ref, built
        )
        self.assertEqual(sess.engine, "doubao")
        self.assertEqual(sess.conversation_ref, ref)
        self.assertEqual(sess.resume_count, 1)

    def test_unsupported_boundaries_are_rejected_not_ignored(self):
        # Session MCP itself is supported now (Hub session pool); the inherited-source list and a
        # bare shell interpreter are the two shapes it still refuses.
        with self.assertRaisesRegex(ValueError, "disabled_mcp_servers"):
            self.hub.resolve_launch(
                "doubao", cwd=str(self.root), disabled_mcp_servers=["x"])
        with self.assertRaisesRegex(ValueError, "shell"):
            self.hub.resolve_launch(
                "doubao", cwd=str(self.root), mcp_servers={"x": {"command": "cmd"}})
        shared = Path(__file__).parent / "docs"
        with self.assertRaisesRegex(ValueError, "extra_roots"):
            self.hub.resolve_launch("doubao", cwd=str(self.root), extra_roots=[str(shared)])
        with self.assertRaisesRegex(ValueError, "自动压缩阈值"):
            self.hub.resolve_launch("doubao", cwd=str(self.root), compaction_threshold="70%")
        self.assertNotIn("doubao", SUPPORTED_CONTEXT_ENGINES)
        self.assertIsNone(
            resolve_native_transcript_path("doubao", {"conversation_ref": {"session_id": "x"}})
        )

    def test_localappdata_doubao_state_is_never_an_extra_root(self):
        from spawn_extra_roots import normalize_extra_roots
        candidate = Path(os.environ.get("LOCALAPPDATA") or "") / "doubao_cli"
        if not candidate.is_dir():
            self.skipTest("no doubao_cli folder on this machine")
        with self.assertRaises(ValueError):
            normalize_extra_roots([str(candidate)])


class DoubaoWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="hub-doubao-watch-")
        self.root = Path(self.temp.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.fake = FakeTerminal()
        self.hub.core = self.fake
        await self.hub.start()
        self.data_root = self.root / "doubao_cli"
        self.data_root.mkdir()
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    def session(self, suffix: str = "") -> AgentSession:
        sid = f"doubao-native{suffix}"
        sess = AgentSession(sid, "doubao", "doubao", "tui", "Doubao fixture", str(self.root),
                            "unused", [], core=self.fake)
        sess.engine = "doubao"
        sess.status = "running"
        self.fake.spawn(sid, ["fixture"])
        sess._loop = asyncio.get_running_loop()
        self.hub.sessions[sid] = sess
        self.hub._on_core_tick()
        return sess

    def conversation(self, sess: AgentSession, conversation_id: str = "777") -> Path:
        folder = (
            self.data_root / "agent_mode" / "u1" / "workspace" / ".sessions" / conversation_id
            / "agents" / "a1" / "system"
        )
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "assignment.md").write_text(
            f"# Assignment\n[Hub Session: session_id={sess.session_id}]\n", encoding="utf-8"
        )
        trajectory = folder / "trajectory.jsonl"
        trajectory.touch()
        return trajectory

    def append(self, path: Path, *rows):
        with path.open("a", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def completed_events(self, sess: AgentSession):
        return [
            event for event in self.hub.worker_events
            if event.get("session_id") == sess.session_id and event.get("type") == "turn_completed"
        ]

    async def until(self, condition, timeout: float = 8.0):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.05)

    async def run_turn(self, sess: AgentSession, *, trajectory: Path):
        self.fake.set_screen(sess.session_id, BUSY_SCREEN)
        await asyncio.sleep(0.7)
        self.append(
            trajectory,
            {"role": "assistant", "content": "先检查目录",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
            {"role": "assistant", "content": "完成：见 a.txt"},
        )
        self.fake.set_screen(sess.session_id, idle_screen("完成：见 a.txt"))

    async def test_tool_turn_completes_from_the_trajectory(self):
        sess = self.session("-tool")
        trajectory = self.conversation(sess)
        sess.resume_attach = True
        sess.conversation_ref = {"session_id": "777", "cwd": str(self.root), "transcript_path": str(trajectory)}
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
        await self.run_turn(sess, trajectory=trajectory)
        await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, "完成：见 a.txt")
        self.assertEqual(sess.final_response_source, "doubao_native")
        self.assertTrue(sess.completion_reported)
        self.assertGreaterEqual(sess.step_count, 1)
        self.assertEqual([e["content"] for e in self.hub.worker_events
                          if e["session_id"] == sess.session_id and e["type"] == "turn_completed"],
                         ["完成：见 a.txt"])

    async def test_pure_text_turn_completes_from_export(self):
        sess = self.session("-text")
        trajectory = self.conversation(sess, "888")
        sess.resume_attach = True
        sess.conversation_ref = {"session_id": "888", "cwd": str(self.root), "transcript_path": str(trajectory)}
        # The resume seeding sees the pre-turn export (empty here); the turn then adds a row.
        exported = []
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
        with patch("session_manager.export_doubao_session",
                   side_effect=lambda *a, **k: list(exported)) as export:
            self.fake.set_screen(sess.session_id, BUSY_SCREEN)
            await asyncio.sleep(0.7)
            exported.append({"query": "q", "response": "导出的纯文本答案"})
            self.fake.set_screen(sess.session_id, idle_screen("导出的纯文本答案"))
            await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, "导出的纯文本答案")
        self.assertEqual(sess.final_response_source, "doubao_export")
        export.assert_called()

    async def test_resumed_replay_never_completes_the_new_turn(self):
        sess = self.session("-resume-replay")
        trajectory = self.conversation(sess, "901")
        # The archive predates the export counter: it says nothing, while the cloud view already
        # holds both earlier turns. The resumed TUI replays them onto the screen as well.
        sess.resume_attach = True
        sess.conversation_ref = {"session_id": "901", "cwd": str(self.root), "transcript_path": str(trajectory)}
        replayed = (
            "DOUBAO CLI\n"
            "▍ 第一轮的问题\n◆ DOUBAO_LIVE_OK_1 41\n"
            "▍ 第二轮的问题\n◆ DOUBAO_LIVE_OK_2 42\n"
            "❯  Ask Doubao to do anything\nAuto · high · E:\\work"
        )
        exported = [
            {"query": "第一轮的问题", "response": "DOUBAO_LIVE_OK_1 41"},
            {"query": "第二轮的问题", "response": "DOUBAO_LIVE_OK_2 42"},
        ]
        self.fake.set_screen(sess.session_id, replayed)
        with patch("session_manager.export_doubao_session", return_value=exported):
            self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
            await self.until(lambda: sess.conversation_ref.get("export_rows") == 2)
            # Two idle settle windows plus several export probes: Turn 3 has not run yet.
            await asyncio.sleep(2.5)
            self.assertEqual(sess.status, "running")
            self.assertEqual(self.completed_events(sess), [])
            self.assertIsNone(sess.final_response)
            # Turn 3 lands: only its own new answer may complete the turn.
            self.append(trajectory, {"role": "assistant", "content": "第三轮答案"})
            self.fake.set_screen(sess.session_id, idle_screen("第三轮答案"))
            await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, "第三轮答案")
        self.assertEqual(sess.final_response_source, "doubao_native")
        # The native turn counted its export row for the next resume.
        self.assertEqual(sess.conversation_ref.get("export_rows"), 3)

    async def test_startup_submission_pending_holds_turn_completion(self):
        sess = self.session("-startup-pending")
        trajectory = self.conversation(sess, "902")
        sess.resume_attach = True
        # A resume whose archive already counts one turn: the seeding is skipped, so only the
        # startup gate keeps the replayed row from ending the new turn.
        sess.conversation_ref = {
            "session_id": "902", "cwd": str(self.root), "transcript_path": str(trajectory),
            "export_rows": 1,
        }
        sess._doubao_export_rows = 1
        sess._startup_inject_pending = True
        sess.last_task = "第三轮的问题"
        self.fake.set_screen(sess.session_id, idle_screen("上一轮答案"))
        exported = [
            {"query": "上一轮的问题", "response": "上一轮答案"},
            {"query": "第三轮的问题", "response": "新一轮答案"},
        ]
        with patch("session_manager.export_doubao_session", return_value=exported):
            self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
            await asyncio.sleep(2.0)
            self.assertEqual(sess.status, "running")
            self.assertEqual(self.completed_events(sess), [])
            # The startup text is submitted: the grown export row may now end the turn.
            sess._startup_inject_pending = False
            await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, "新一轮答案")
        self.assertEqual(sess.final_response_source, "doubao_export")

    async def test_startup_task_clears_the_injection_gate(self):
        sess = self.session("-startup-clear")
        self.fake.set_screen(sess.session_id, idle_screen("上一轮答案"))
        # The spawn sites set this before scheduling the startup task (see create_session and
        # _resume_process_session); its finally must clear it for every engine, doubao included.
        sess._startup_inject_pending = True
        await self.hub._auto_handle_tui_startup_and_trust(sess, None, 0.0, "\r")
        self.assertFalse(sess._startup_inject_pending)

    async def test_screen_reply_is_the_last_resort(self):
        sess = self.session("-screen")
        sess.resume_attach = True
        sess.conversation_ref = {"session_id": "", "cwd": str(self.root)}
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root / "empty")))
        self.fake.set_screen(sess.session_id, BUSY_SCREEN)
        await asyncio.sleep(0.7)
        self.fake.set_screen(sess.session_id, idle_screen("屏幕答案"))
        await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, "屏幕答案")
        self.assertEqual(sess.final_response_source, "doubao_screen")

    async def test_engine_error_ends_the_turn_as_an_error(self):
        sess = self.session("-error")
        trajectory = self.conversation(sess, "999")
        sess.resume_attach = True
        sess.conversation_ref = {"session_id": "999", "cwd": str(self.root), "transcript_path": str(trajectory)}
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
        self.fake.set_screen(sess.session_id, BUSY_SCREEN)
        await asyncio.sleep(0.7)
        self.fake.set_screen(
            sess.session_id,
            "DOUBAO CLI\n◆ 正在运行构建\n\nError: auth_failed: 登录已过期\n❯  Ask Doubao to do anything\nAuto · high · E:\\work",
        )
        await self.until(lambda: sess.last_turn_outcome == "error")
        self.assertEqual(sess.status, "completed")
        self.assertEqual(sess.final_response_source, "engine_error")
        self.assertEqual((sess.last_turn_error or {}).get("kind"), "auth")
        self.assertIn("auth", sess.final_response)

    async def test_fresh_spawn_binds_the_marked_conversation(self):
        sess = self.session("-marker")
        trajectory = self.conversation(sess, "555")
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
        self.fake.set_screen(sess.session_id, BUSY_SCREEN)
        await asyncio.sleep(0.7)
        self.append(trajectory, {"role": "assistant", "content": "标记答案"})
        self.fake.set_screen(sess.session_id, idle_screen("标记答案"))
        await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.conversation_ref.get("session_id"), "555")
        self.assertEqual(sess.transcript_path, trajectory)
        self.assertEqual(sess.final_response, "标记答案")
        self.assertEqual(sess.final_response_source, "doubao_native")

    async def test_approval_card_registers_and_answers_with_the_numbered_key(self):
        sess = self.session("-approval")
        self.fake.set_screen(
            sess.session_id,
            "Allow this command?\n  pnpm build\n"
            "❯ 1. Allow once\n  2. Allow similar commands\n  3. Reject\n  4. Reject and stop\n  Submit\n",
        )
        interaction = await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNotNone(interaction)
        self.assertEqual(interaction["kind"], "approval_request")
        self.assertEqual(interaction["options"][:2], ["Allow once", "Allow similar commands"])
        self.assertEqual(sess.status, "waiting_parent")
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertTrue(result["delivered"])
        await asyncio.sleep(0.35)
        self.assertEqual(self.fake.inputs_of(sess.session_id)[:2], ["1", "\r"])

    async def test_approve_session_sends_key_two_and_question_uses_option_index(self):
        sess = self.session("-approve-session")
        self.fake.set_screen(
            sess.session_id,
            "Allow all operations?\n❯ 1. Allow once\n  2. Allow similar actions\n  3. Reject\n",
        )
        await self.hub.poll_tui_screen_interactions_once(sess)
        await self.hub.respond_to_interaction(sess.session_id, decision="approve_session")
        await asyncio.sleep(0.35)
        self.assertEqual(self.fake.inputs_of(sess.session_id)[:2], ["2", "\r"])

        other = self.session("-question")
        self.fake.set_screen(
            other.session_id,
            "Which branch?\n❯ 1. main\n  2. dev\n❯  Ask Doubao to do anything\n",
        )
        interaction = await self.hub.poll_tui_screen_interactions_once(other)
        self.assertEqual(interaction["kind"], "question")
        await self.hub.respond_to_interaction(other.session_id, option_index=2)
        await asyncio.sleep(0.35)
        self.assertEqual(self.fake.inputs_of(other.session_id)[:2], ["2", "\r"])

    # ── Review remediations ─────────────────────────────────────────────────────────────────

    async def test_assistant_prose_naming_error_codes_completes_as_an_answer(self):
        sess = self.session("-prose")
        trajectory = self.conversation(sess, "801")
        sess.resume_attach = True
        sess.conversation_ref = {
            "session_id": "801", "cwd": str(self.root), "transcript_path": str(trajectory),
        }
        reply = "`internal_error` and `auth_failed` are the CLI's own error codes."
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
        self.fake.set_screen(sess.session_id, BUSY_SCREEN)
        await asyncio.sleep(0.7)
        self.append(trajectory, {"role": "assistant", "content": reply})
        self.fake.set_screen(sess.session_id, idle_screen(reply))
        await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, reply)
        self.assertEqual(sess.final_response_source, "doubao_native")
        self.assertNotEqual(sess.last_turn_outcome, "error")
        self.assertIsNone(sess.last_turn_error)

    async def test_worked_for_mid_turn_stays_running_without_output(self):
        sess = self.session("-worked")
        sess.resume_attach = True
        sess.conversation_ref = {"session_id": "", "cwd": str(self.root)}
        self.fake.set_screen(sess.session_id, idle_screen("第一轮答案"))
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root / "empty")))
        await asyncio.sleep(0.8)
        self.fake.set_screen(
            sess.session_id,
            "DOUBAO CLI\n◆ 第一轮答案\n✦ Worked for 12s\n❯  Ask Doubao to do anything\nAuto · high · E:\\work",
        )
        await asyncio.sleep(2.5)
        self.assertEqual(sess.status, "running")
        self.assertEqual(self.completed_events(sess), [])

    async def test_new_turn_never_reuses_the_previous_screen_answer(self):
        sess = self.session("-two-turns")
        sess.resume_attach = True
        sess.conversation_ref = {"session_id": "", "cwd": str(self.root)}
        self.fake.set_screen(
            sess.session_id,
            "DOUBAO CLI\n❯  Ask Doubao to do anything\nAuto · high · E:\\work",
        )
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root / "empty")))
        await asyncio.sleep(0.8)
        self.fake.set_screen(sess.session_id, idle_screen("完成：见 a.txt"))
        await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, "完成：见 a.txt")
        self.assertEqual(len(self.completed_events(sess)), 1)

        # Turn 2: no trajectory row and no export; the screen still shows turn 1's answer.
        self.hub.begin_new_turn(sess, status="running")
        self.fake.set_screen(sess.session_id, idle_screen("完成：见 a.txt"))
        await asyncio.sleep(2.2)
        self.assertEqual(sess.status, "running")
        self.assertEqual(len(self.completed_events(sess)), 1)
        self.assertEqual(sess.final_response, "完成：见 a.txt")

        # Turn 2 renders its own `◆` block: now it completes with THAT answer, not turn 1's.
        self.fake.set_screen(sess.session_id, idle_screen("完成：见 b.txt"))
        await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, "完成：见 b.txt")
        self.assertEqual(sess.final_response_source, "doubao_screen")
        self.assertEqual(len(self.completed_events(sess)), 2)

    async def test_fast_turn_completes_without_any_busy_frame(self):
        sess = self.session("-fast")
        trajectory = self.conversation(sess, "778")
        sess.resume_attach = True
        sess.conversation_ref = {
            "session_id": "778", "cwd": str(self.root), "transcript_path": str(trajectory),
        }
        self.fake.set_screen(sess.session_id, idle_screen("旧答案"))
        with patch("session_manager.export_doubao_session", return_value=None):
            self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
            await asyncio.sleep(1.0)
            # The whole turn starts and finishes between two polls: no busy frame is ever drawn.
            self.append(trajectory, {"role": "assistant", "content": "瞬时答案"})
            self.fake.set_screen(sess.session_id, idle_screen("瞬时答案"))
            await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, "瞬时答案")
        self.assertEqual(sess.final_response_source, "doubao_native")

    async def test_processing_answer_above_idle_prompt_completes(self):
        sess = self.session("-processing")
        trajectory = self.conversation(sess, "802")
        sess.resume_attach = True
        sess.conversation_ref = {
            "session_id": "802", "cwd": str(self.root), "transcript_path": str(trajectory),
        }
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
        self.fake.set_screen(sess.session_id, BUSY_SCREEN)
        await asyncio.sleep(0.7)
        self.append(trajectory, {"role": "assistant", "content": "Processing is complete"})
        self.fake.set_screen(sess.session_id, idle_screen("Processing is complete"))
        await self.until(lambda: sess.status == "completed")
        self.assertEqual(sess.final_response, "Processing is complete")
        self.assertEqual(sess.final_response_source, "doubao_native")

    async def test_claiming_a_transcript_keeps_export_rows_and_cwd(self):
        sess = self.session("-claim-merge")
        trajectory = self.conversation(sess, "779")
        sess.conversation_ref = {"export_rows": 7, "cwd": str(self.root)}
        self.assertTrue(await self.hub._claim_transcript(sess, trajectory))
        self.assertEqual(sess.conversation_ref.get("session_id"), "779")
        self.assertEqual(sess.conversation_ref.get("export_rows"), 7)
        self.assertEqual(sess.conversation_ref.get("cwd"), str(self.root))

    async def test_rediscovering_a_trajectory_keeps_export_rows_and_cwd(self):
        sess = self.session("-rediscover")
        sess.conversation_ref = {"export_rows": 7, "cwd": str(self.root)}
        self.fake.set_screen(
            sess.session_id,
            "DOUBAO CLI\n❯  Ask Doubao to do anything\nAuto · high · E:\\work",
        )
        self.tasks.append(self.hub._schedule(self.hub._watch_doubao_session(sess, self.data_root)))
        self.conversation(sess, "556")
        await self.until(lambda: sess.conversation_ref.get("session_id") == "556")
        self.assertEqual(sess.conversation_ref.get("export_rows"), 7)
        self.assertEqual(sess.conversation_ref.get("cwd"), str(self.root))
        self.assertEqual(sess.conversation_ref.get("transcript_path"), str(sess.transcript_path))

    async def test_two_option_card_deny_presses_its_own_abort_row(self):
        sess = self.session("-two-option")
        self.fake.set_screen(
            sess.session_id,
            "Files will be deleted?\n❯ 1. Continue anyway\n  2. Abort\n  Submit\n",
        )
        interaction = await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertEqual(interaction["options"], ["Continue anyway", "Abort"])
        result = await self.hub.respond_to_interaction(sess.session_id, decision="deny")
        await asyncio.sleep(0.35)
        self.assertEqual(self.fake.inputs_of(sess.session_id)[:2], ["2", "\r"])
        self.assertEqual(result["chosen_option_index"], 2)
        self.assertEqual(result["chosen_label"], "Abort")
        self.assertEqual(result["approval_action"], "deny")

    async def test_session_approval_downgrades_instead_of_pressing_abort(self):
        sess = self.session("-downgrade")
        self.fake.set_screen(
            sess.session_id,
            "Files will be deleted?\n❯ 1. Continue anyway\n  2. Abort\n  Submit\n",
        )
        await self.hub.poll_tui_screen_interactions_once(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve_session")
        await asyncio.sleep(0.35)
        self.assertEqual(self.fake.inputs_of(sess.session_id)[:2], ["1", "\r"])
        self.assertEqual(result["approval_action"], "approve_once")
        self.assertEqual(result["requested_approval_action"], "approve_session")
        self.assertTrue(any("降级" in note for note in result["notes"]))

    async def test_four_option_card_denies_on_the_reject_rows(self):
        deny = self.session("-four-deny")
        self.fake.set_screen(
            deny.session_id,
            "Allow this command?\n  pnpm build\n"
            "❯ 1. Allow once\n  2. Allow similar commands\n  3. Reject\n  4. Reject and stop\n  Submit\n",
        )
        await self.hub.poll_tui_screen_interactions_once(deny)
        await self.hub.respond_to_interaction(deny.session_id, decision="deny")
        await asyncio.sleep(0.35)
        self.assertEqual(self.fake.inputs_of(deny.session_id)[:2], ["3", "\r"])

        feedback = self.session("-four-feedback")
        self.fake.set_screen(
            feedback.session_id,
            "Allow this command?\n  pnpm build\n"
            "❯ 1. Allow once\n  2. Allow similar commands\n  3. Reject\n  4. Reject and stop\n  Submit\n",
        )
        await self.hub.poll_tui_screen_interactions_once(feedback)
        await self.hub.respond_to_interaction(
            feedback.session_id, decision="deny", custom_input="改用 npm"
        )
        await asyncio.sleep(0.35)
        self.assertEqual(self.fake.inputs_of(feedback.session_id)[:2], ["4", "\r"])

    async def test_reordered_card_presses_the_rows_it_actually_drew(self):
        approve = self.session("-reordered-approve")
        self.fake.set_screen(
            approve.session_id,
            "Allow all operations?\n❯ 1. Reject\n  2. Allow once\n  Submit\n",
        )
        await self.hub.poll_tui_screen_interactions_once(approve)
        result = await self.hub.respond_to_interaction(approve.session_id, decision="approve")
        await asyncio.sleep(0.35)
        self.assertEqual(self.fake.inputs_of(approve.session_id)[:2], ["2", "\r"])
        self.assertEqual(result["chosen_label"], "Allow once")

        deny = self.session("-reordered-deny")
        self.fake.set_screen(
            deny.session_id,
            "Allow all operations?\n❯ 1. Reject\n  2. Allow once\n  Submit\n",
        )
        await self.hub.poll_tui_screen_interactions_once(deny)
        await self.hub.respond_to_interaction(deny.session_id, decision="deny")
        await asyncio.sleep(0.35)
        self.assertEqual(self.fake.inputs_of(deny.session_id)[:2], ["1", "\r"])


if __name__ == "__main__":
    unittest.main()
