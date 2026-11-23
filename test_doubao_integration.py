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
    doubao_card_submitting,
    doubao_has_idle_composer,
    export_doubao_session,
    extract_doubao_screen_reply,
    find_doubao_session_by_marker,
    parse_doubao_exit_resume_id,
    classify_doubao_option,
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


# Real `doubao` 2.37.2 captures: unnumbered confirmation rows (snapshot A/B), an `ask` question
# card with radio rows and trailing actions (C), and the numbered `/permissions` menu (D, no card).
CONFIRM_CARD_WIDE = (
    "◇ Processing\n"
    "◇ 执行删除操作指令\n"
    "◇ 正在执行代码 Remove-Item -Recurse -Force E:\\misc\\mcp-agent-hub\\.scratch\\doubao_probe\\risky_target_3 · Running\n"
    "◇ 是否允许删除 E:\\misc\\mcp-agent-hub\\.scratch\\doubao_probe\\risky_target_3？\n"
    "\n"
    "─────────────────────────────────────────────────────────────────────────────────────────────────────────────\n"
    "\n"
    "  是否允许删除 E:\\misc\\mcp-agent-hub\\.scratch\\doubao_probe\\risky_target_3？\n"
    "❯ Allow once\n"
    "  Reject\n"
    "  Reject and stop\n"
    "\n"
    "\n"
    "  ↑↓ navigate | Enter / shortcut confirm | Esc stop\n"
)
CONFIRM_CARD_NARROW = (
    "◇ 正在执行代码 Remove-Item -Recurse -Force E:\\misc\\mcp-agent-hub\\.scratch\\douba\n"
    "  o_probe\\risky_target_3 · Running\n"
    "◇ 是否允许递归强制删除 E:\\misc\\mcp-agent-hub\\.scratch\\doubao_probe\\risky_target\n"
    "◇ _3 目录？\n"
    "\n"
    "───────────────────────────────────────────────────────────────────────────────\n"
    "\n"
    "  是否允许递归强制删除 E:\\misc\\mcp-agent-hub\\.scratch\\doubao_probe\\risky_targ\n"
    "  et_3 目录？\n"
    "❯ Allow once\n"
    "  Reject\n"
    "  Reject and stop\n"
    "\n"
    "\n"
    "  ↑↓ navigate | Enter / shortcut confirm | Esc stop\n"
)
QUESTION_CARD = (
    "  (required) Which environment should we deploy to?\n"
    "❯ ( ) staging · Deploy to the staging environment for pre-release verification.\n"
    "  ( ) production · Deploy to the production environment, live for end users.\n"
    "  ( ) Other:  [自定义描述...]\n"
    "  Submit\n"
    "  Skip\n"
    "  Reject and stop\n"
    "\n"
    "  Ctrl+S submit | Space/Enter select | ↑↓ move | Esc skip\n"
)
PERMISSIONS_MENU = (
    "  Permissions · Next turn\n"
    "\n"
    "  1. Always ask\n"
    "❯ 2. Ask when risky (current)\n"
    "  3. Allow all\n"
    "\n"
    "\n"
    "  ↑↓ navigate | Enter confirm | Esc back | number to select\n"
)
QUESTION_CARD_FREE_TEXT = (
    "  Anything else to keep in mind?\n"
    "❯ Answer:  [Optional]\n"
    "\n"
    "  Type to answer | Ctrl+S submit | Esc skip\n"
)
# The renderer flattens every question of an `ask` card into one list: options, then that
# question's text row, for each question, then the actions.
QUESTION_CARD_MULTI = (
    "  (required) Which environment should we deploy to?\n"
    "❯ ( ) staging\n"
    "  ( ) production\n"
    "  (required) Which region?\n"
    "  ( ) eu\n"
    "  ( ) us\n"
    "  Submit\n"
    "  Skip\n"
    "\n"
    "  Ctrl+S submit | Space/Enter select | ↑↓ move | Esc skip\n"
)
# A standalone `Answer:` row beside radio rows (two questions' rows in one list).
QUESTION_CARD_MIXED = (
    "  Which environment should we deploy to?\n"
    "❯ Answer:  [Optional]\n"
    "  ( ) staging\n"
    "  ( ) production\n"
    "  ( ) Other:  [type your answer]\n"
    "  Submit\n"
    "  Skip\n"
    "\n"
    "  Ctrl+S submit | Space/Enter select | ↑↓ move | Esc skip\n"
)
# 50-column PTY: Ink wraps the footer over two rows.
QUESTION_CARD_WRAPPED_50 = (
    "  (required) Which environment?\n"
    "❯ ( ) staging\n"
    "  ( ) production\n"
    "  Submit\n"
    "  Skip\n"
    "\n"
    "  Ctrl+S submit | Space/Enter select | ↑↓\n"
    "  move | Esc skip\n"
)
CONFIRM_CARD_WRAPPED_50 = (
    "◇ 是否允许删除 X？\n"
    "\n"
    "─────────────────────────────\n"
    "\n"
    "  是否允许删除 X？\n"
    "❯ Allow once\n"
    "  Reject\n"
    "\n"
    "  ↑↓ navigate | Enter /\n"
    "  shortcut confirm | Esc stop\n"
)
# A one-option card: the rule anchor, the visible Ink cursor and the real footer carry it.
CONFIRM_CARD_ONE_OPTION = (
    "◇ 是否停止当前任务？\n"
    "\n"
    "─────────────────────────────\n"
    "\n"
    "  是否停止当前任务？\n"
    "❯ Reject and stop\n"
    "\n"
    "  ↑↓ navigate | Enter / shortcut confirm | Esc stop\n"
)


def folder_card(path: str) -> str:
    """A folder-access confirmation card that differs from its twin only in the directory."""
    return (
        "◇ 允许访问以下目录？\n"
        "\n"
        "─────────────────────────────\n"
        "\n"
        "  Allow access to these directories?\n"
        f"  {path}\n"
        "❯ Allow once\n"
        "  Reject\n"
        "\n"
        "\n"
        "  ↑↓ navigate | Enter / shortcut confirm | Esc stop\n"
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

    def test_wide_confirmation_card_parses_actions_command_and_cursor(self):
        parsed = parse_doubao_prompt(CONFIRM_CARD_WIDE)
        self.assertEqual(parsed["kind"], "approval_request")
        self.assertEqual(parsed["options"], ["Allow once", "Reject", "Reject and stop"])
        self.assertEqual(parsed["option_actions"], ["approve_once", "deny", "deny_feedback"])
        self.assertEqual(parsed["selected_index"], 1)
        self.assertEqual(parsed["esc_action"], "stop")
        self.assertEqual(
            parsed["command"],
            "Remove-Item -Recurse -Force E:\\misc\\mcp-agent-hub\\.scratch\\doubao_probe\\risky_target_3",
        )
        self.assertEqual(
            parsed["title"], "是否允许删除 E:\\misc\\mcp-agent-hub\\.scratch\\doubao_probe\\risky_target_3？"
        )
        self.assertIn(parsed["command"], parsed["prompt"])
        self.assertIn("是否允许删除", parsed["prompt"])
        # The card identity does not change when only the cursor moves.
        moved = CONFIRM_CARD_WIDE.replace("❯ Allow once", "  Allow once").replace(
            "  Reject\n", "❯ Reject\n", 1
        )
        self.assertEqual(parse_doubao_prompt(moved)["selected_index"], 2)
        self.assertEqual(parse_doubao_prompt(moved)["signature"], parsed["signature"])

    def test_narrow_confirmation_card_keeps_wrapped_paths_uncorrupted(self):
        parsed = parse_doubao_prompt(CONFIRM_CARD_NARROW)
        self.assertIn("douba", parsed["command"])
        self.assertIn("o_probe", parsed["command"])
        self.assertNotIn("douba o_probe", parsed["command"])
        self.assertIn("risky_target", parsed["prompt"])
        self.assertIn("et_3 目录？", parsed["prompt"])
        self.assertNotIn("risky_target et_3", parsed["prompt"])
        self.assertEqual(parsed["option_actions"], ["approve_once", "deny", "deny_feedback"])

    def test_question_card_parses_radios_text_row_actions_and_free_text(self):
        parsed = parse_doubao_prompt(QUESTION_CARD)
        self.assertEqual(parsed["kind"], "question")
        self.assertEqual(parsed["prompt"], "Which environment should we deploy to?")
        self.assertEqual(parsed["options"], ["staging", "production", "Other"])
        self.assertEqual(parsed["actions"], ["Submit", "Skip", "Reject and stop"])
        self.assertEqual(parsed["action_decisions"], ["submit", "skip", "deny_feedback"])
        self.assertEqual((parsed["other_option_index"], parsed["selected_index"]), (3, 1))
        self.assertEqual(parsed["raw_options_count"], 2)
        self.assertFalse(parsed["multi_select"])
        self.assertTrue(parsed["esc_action"] == "skip")
        free_text = parse_doubao_prompt(QUESTION_CARD_FREE_TEXT)
        self.assertTrue(free_text["free_text_only"])
        self.assertEqual(free_text["options"], [])
        self.assertEqual(free_text["selected_index"], 1)

    def test_multi_select_question_records_checked_rows(self):
        parsed = parse_doubao_prompt(
            "  Which checks?\n"
            "❯ [✓] lint · Run the linter.\n"
            "  [ ] e2e\n"
            "  Submit\n"
            "  Skip\n"
            "\n"
            "  Ctrl+S submit | Space/Enter select | ↑↓ move | Esc skip\n"
        )
        self.assertTrue(parsed["multi_select"])
        self.assertEqual(parsed["checked_indices"], [1])
        self.assertEqual(parsed["options"], ["lint", "e2e"])

    def test_permissions_menu_idle_composer_prose_and_half_drawn_frames_are_not_cards(self):
        self.assertIsNone(parse_doubao_prompt(PERMISSIONS_MENU))
        self.assertIsNone(parse_doubao_prompt("◆ hi\n\n❯  Ask Doubao to do anything\n"))
        # A card that scrolled above the idle composer is scrollback, not the live prompt.
        self.assertIsNone(parse_doubao_prompt(CONFIRM_CARD_WIDE + "❯  Ask Doubao to do anything\n"))
        # A half-drawn Ink frame has no footer yet.
        self.assertIsNone(parse_doubao_prompt(CONFIRM_CARD_WIDE.rsplit("  ↑↓", 1)[0]))
        prose = (
            "◆ The CLI would draw a card like this:\n"
            "──────────────────────────────────────────\n"
            "  是否允许删除 X？\n"
            "❯ Allow once\n"
            "  Reject\n"
        )
        self.assertIsNone(parse_doubao_prompt(prose))
        # A card below an answer (its own footer) still parses.
        self.assertEqual(parse_doubao_prompt("◆ 需要你确认\n" + CONFIRM_CARD_WIDE)["kind"], "approval_request")

    def test_option_classification_treats_global_allow_as_persist(self):
        self.assertEqual(classify_doubao_option("Always allow"), "persist")
        self.assertEqual(classify_doubao_option("Allow always"), "persist")
        self.assertEqual(classify_doubao_option("[✓] 允许所有"), "persist")
        self.assertEqual(classify_doubao_option("Allow similar commands [2]"), "approve_session")
        self.assertEqual(classify_doubao_option("Allow once"), "approve_once")
        self.assertEqual(classify_doubao_option("Allow folder access once"), "approve_once")
        self.assertEqual(classify_doubao_option("Allow listed folders once"), "approve_once")
        self.assertEqual(classify_doubao_option("Reject and stop"), "deny_feedback")
        self.assertEqual(classify_doubao_option("( ) 拒绝并停止"), "deny_feedback")
        self.assertEqual(classify_doubao_option("Reject"), "deny")
        self.assertEqual(classify_doubao_option("Submit"), "submit")
        self.assertEqual(classify_doubao_option("Something new"), "deny")

    def test_chinese_negations_never_read_as_approvals(self):
        # `不允许` holds `允许`, `禁止删除` holds `删除`: a substring match would approve the call.
        self.assertEqual(classify_doubao_option("不允许"), "deny")
        self.assertEqual(classify_doubao_option("禁止删除"), "deny")
        self.assertEqual(classify_doubao_option("不同意"), "deny")
        self.assertEqual(classify_doubao_option("不批准"), "deny")
        self.assertEqual(classify_doubao_option("不继续"), "deny")
        self.assertEqual(classify_doubao_option("取消"), "deny")
        self.assertEqual(classify_doubao_option("跳过"), "deny")
        # The positive forms still approve.
        self.assertEqual(classify_doubao_option("允许"), "approve_once")
        self.assertEqual(classify_doubao_option("批准"), "approve_once")
        self.assertEqual(classify_doubao_option("继续"), "approve_once")
        self.assertEqual(classify_doubao_option("删除"), "approve_once")

    def test_folder_cards_with_different_directories_have_distinct_signatures(self):
        first = parse_doubao_prompt(folder_card("D:\\project\\a"))
        second = parse_doubao_prompt(folder_card("D:\\project\\b"))
        self.assertEqual(first["command"], "")
        self.assertEqual(first["options"], second["options"])
        self.assertEqual(first["title"], second["title"])
        self.assertIn("D:\\project\\a", first["prompt"])
        self.assertNotEqual(first["signature"], second["signature"])

    def test_body_less_confirmation_cards_differ_in_a_later_fallback_line(self):
        def card(second_line: str) -> str:
            # No heading inside the card body: the `◇` question above the rule is the fallback.
            return (
                "◇ 是否允许删除 A 中第二行提到\n"
                f"◇ {second_line}\n"
                "\n"
                "─────────────────────────────\n"
                "\n"
                "❯ Allow once\n"
                "  Reject\n"
                "\n"
                "  ↑↓ navigate | Enter / shortcut confirm | Esc stop\n"
            )

        first = parse_doubao_prompt(card("的文件？"))
        second = parse_doubao_prompt(card("的目录？"))
        self.assertEqual(first["kind"], "approval_request")
        self.assertEqual(first["title"], second["title"])
        self.assertEqual(first["title"], "是否允许删除 A 中第二行提到")
        self.assertIn("的文件？", first["prompt"])
        self.assertIn("的目录？", second["prompt"])
        self.assertNotEqual(first["signature"], second["signature"])

    def test_multi_question_cards_differ_in_a_later_question_heading(self):
        first = parse_doubao_prompt(QUESTION_CARD_MULTI)
        second = parse_doubao_prompt(QUESTION_CARD_MULTI.replace("Which region?", "Which zone?"))
        self.assertTrue(first["multi_question"])
        self.assertTrue(second["multi_question"])
        self.assertEqual(first["prompt"], second["prompt"])
        self.assertEqual(first["options"], second["options"])
        self.assertNotEqual(first["signature"], second["signature"])

    def test_question_signature_ignores_cursor_checks_typed_text_and_the_text_footer(self):
        typed = QUESTION_CARD.replace("❯ ( ) staging", "  ( ) staging").replace(
            "  ( ) Other:  [自定义描述...]", "❯ ( ) Other:  something typed"
        ).replace(
            "  Ctrl+S submit | Space/Enter select | ↑↓ move | Esc skip",
            "  Type to answer | Ctrl+S submit | Esc skip",
        )
        base = parse_doubao_prompt(QUESTION_CARD)
        moved = parse_doubao_prompt(typed)
        self.assertEqual(moved["selected_index"], 3)
        self.assertEqual(moved["signature"], base["signature"])
        checked = parse_doubao_prompt(QUESTION_CARD.replace("( ) staging", "(●) staging"))
        self.assertEqual(checked["signature"], base["signature"])
        self.assertEqual(checked["checked_indices"], [1])
        described = parse_doubao_prompt(
            QUESTION_CARD.replace("staging · Deploy", "staging · Ship it now")
        )
        self.assertNotEqual(described["signature"], base["signature"])

    def test_free_text_only_card_keeps_its_signature_when_the_cursor_moves_to_skip(self):
        card = (
            "  Anything else to keep in mind?\n"
            "❯ Answer:  [Optional]\n"
            "  Submit\n"
            "  Skip\n"
            "\n"
            "  Type to answer | Ctrl+S submit | Esc skip\n"
        )
        moved = (
            "  Anything else to keep in mind?\n"
            "  Answer:  [Optional]\n"
            "  Submit\n"
            "❯ Skip\n"
            "\n"
            "  Ctrl+S submit | Space/Enter select | ↑↓ move | Esc skip\n"
        )
        on_text = parse_doubao_prompt(card)
        on_skip = parse_doubao_prompt(moved)
        self.assertTrue(on_text["free_text_only"])
        self.assertTrue(on_skip["free_text_only"])
        self.assertEqual(on_skip["selected_index"], 3)
        self.assertEqual(on_skip["signature"], on_text["signature"])

    def test_multi_question_and_mixed_cards_are_flagged(self):
        multi = parse_doubao_prompt(QUESTION_CARD_MULTI)
        self.assertTrue(multi["multi_question"])
        self.assertEqual(multi["options"], ["staging", "production", "eu", "us"])
        self.assertEqual(
            [(row["kind"], row["label"]) for row in multi["nav_rows"]],
            [("option", "staging"), ("option", "production"), ("option", "eu"),
             ("option", "us"), ("action", "Submit"), ("action", "Skip")],
        )
        mixed = parse_doubao_prompt(QUESTION_CARD_MIXED)
        self.assertTrue(mixed["multi_question"])
        self.assertEqual(
            [(row["kind"], row["label"]) for row in mixed["nav_rows"]],
            [("text", "Answer"), ("option", "staging"), ("option", "production"),
             ("text", "Other"), ("action", "Submit"), ("action", "Skip")],
        )
        single = parse_doubao_prompt(QUESTION_CARD)
        self.assertFalse(single["multi_question"])
        free_text = parse_doubao_prompt(QUESTION_CARD_FREE_TEXT)
        self.assertFalse(free_text["multi_question"])

    def test_one_option_confirmation_card_parses(self):
        parsed = parse_doubao_prompt(CONFIRM_CARD_ONE_OPTION)
        self.assertEqual(parsed["kind"], "approval_request")
        self.assertEqual(parsed["options"], ["Reject and stop"])
        self.assertEqual(parsed["option_actions"], ["deny_feedback"])
        self.assertEqual(parsed["selected_index"], 1)

    def test_footer_wrapped_on_a_fifty_column_pty_is_joined(self):
        question = parse_doubao_prompt(QUESTION_CARD_WRAPPED_50)
        self.assertEqual(question["kind"], "question")
        self.assertEqual(question["options"], ["staging", "production"])
        self.assertEqual(question["esc_action"], "skip")
        confirm = parse_doubao_prompt(CONFIRM_CARD_WRAPPED_50)
        self.assertEqual(confirm["kind"], "approval_request")
        self.assertEqual(confirm["options"], ["Allow once", "Reject"])
        self.assertEqual(confirm["esc_action"], "stop")

    def test_a_card_quoted_in_a_code_fence_is_never_the_live_card(self):
        quoted = (
            "◆ The card looks like this:\n"
            "\n"
            "```text\n"
            "──────────\n"
            "\n"
            "  Allow access to these directories?\n"
            "❯ Allow once\n"
            "  Reject\n"
            "\n"
            "  ↑↓ navigate | Enter / shortcut confirm | Esc stop\n"
            "```\n"
        )
        self.assertIsNone(parse_doubao_prompt(quoted))
        # A real card drawn below an answer still parses (it sits outside every fence).
        self.assertEqual(
            parse_doubao_prompt("◆ 需要你确认\n" + CONFIRM_CARD_WIDE)["kind"], "approval_request"
        )
        self.assertEqual(
            parse_doubao_prompt("◆ 需要你确认\n\n" + CONFIRM_CARD_WIDE)["kind"], "approval_request"
        )

    def test_idle_composer_is_only_the_live_row_and_the_busy_footer_is_recognized(self):
        self.assertTrue(
            doubao_has_idle_composer("◆ hi\n\n❯  Ask Doubao to do anything\nAuto · high · E:\\work")
        )
        prose = (
            "◆ The hint row reads: Ask Doubao to do anything, then it waits.\n"
            "──────────\n"
            "  Allow access to these directories?\n"
            "❯ Allow once\n"
            "  Reject\n"
        )
        self.assertFalse(doubao_has_idle_composer(prose))
        self.assertTrue(
            doubao_card_submitting("◆ hi\n\n  Submitting, waiting for confirmation… | Esc skip\n")
        )
        self.assertFalse(doubao_card_submitting("◆ hi\n\n❯  Ask Doubao to do anything\n"))

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
        with self.assertRaisesRegex(ValueError, "MCP"):
            self.hub.resolve_launch(
                "doubao", cwd=str(self.root), mcp_servers={"x": {"url": "https://example.invalid"}}
            )
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


class _FakeDoubaoConfirmCard:
    """A real 2.37.2 confirmation card: unnumbered rows, arrows clamp at both ends.

    The renderer dispatches a row only on `e.shortcut === t`; the 2.37.2 server sends no digit
    shortcuts, so a digit does nothing and only the cursor plus Enter confirms a row.
    """

    def __init__(self, options=("Allow once", "Reject", "Reject and stop"), *, ignore_arrows=False,
                 close_after_reads=0, closes_on_enter=True,
                 busy_until_closed=False,
                 question="是否允许删除 E:\\misc\\risky_target_3？",
                 command="Remove-Item -Recurse -Force E:\\misc\\risky_target_3") -> None:
        self.options = list(options)
        self.selected = 0
        self.ignore_arrows = ignore_arrows
        self.close_after_reads = close_after_reads
        self.closes_on_enter = closes_on_enter
        self.busy_until_closed = busy_until_closed
        self.question = question
        self.command = command
        self.entered_on = None
        self.submitting = False
        self.closed = False
        self._linger = None
        self.keys = []

    def on_key(self, text: str) -> None:
        self.keys.append(text)
        if self.closed:
            return
        if text == "\x1b[B" and not self.ignore_arrows:
            self.selected = min(len(self.options) - 1, self.selected + 1)
        elif text == "\x1b[A" and not self.ignore_arrows:
            self.selected = max(0, self.selected - 1)
        elif text == "\r" and self.closes_on_enter:
            self.entered_on = self.selected
            self._close()
        elif text == "\x1b":
            self.entered_on = self.selected
            self._close()

    def _close(self) -> None:
        # The real card repaints its footer as `Submitting, waiting for confirmation…` while it
        # closes; `busy_until_closed` keeps it there until the test closes it.
        self.submitting = True
        if self.busy_until_closed:
            return
        self._linger = self.close_after_reads
        if self._linger <= 0:
            self.closed = True

    def render(self, *, decrement: bool = True) -> str:
        if decrement and self._linger is not None:
            if self._linger <= 0:
                self.closed = True
            else:
                self._linger -= 1
        if self.closed:
            return "◆ 完成\n\n❯  Ask Doubao to do anything\n"
        rows = [
            "◇ 正在执行代码 " + self.command + " · Running",
            "◇ " + self.question,
            "",
            "─" * 60,
            "",
            "  " + self.question,
        ]
        for index, label in enumerate(self.options):
            rows.append(("❯ " if index == self.selected else "  ") + label)
        footer = (
            "Submitting, waiting for confirmation… | Esc stop"
            if self.submitting
            else "↑↓ navigate | Enter / shortcut confirm | Esc stop"
        )
        rows += ["", "", "  " + footer]
        return "\n".join(rows) + "\n"


class _FakeDoubaoQuestionCard:
    """A real 2.37.2 `ask` card: radio/text rows, Submit/Skip/Reject actions, Ctrl+S submits.

    The renderer dispatches an option only on `e.shortcut === t` and the server sends none, so a
    digit is a no-op here too; the cursor (`❯`) is what selects, and `Ctrl+U` clears a text row.
    """

    def __init__(self, options=("staging", "production"), *, other=True, free_text=False,
                 actions=("Submit", "Skip", "Reject and stop"), ignore_arrows=False,
                 close_after_reads=0, drop_enter=0, multi_select=False, prechecked=(),
                 initial_text="", vanish_on_type=False, busy_until_closed=False,
                 prompt="Which environment should we deploy to?") -> None:
        self.options = list(options)
        self.other = other
        self.free_text = free_text
        self.actions = list(actions)
        self.ignore_arrows = ignore_arrows
        self.close_after_reads = close_after_reads
        self.drop_enter = drop_enter
        self.multi_select = multi_select
        self.vanish_on_type = vanish_on_type
        self.busy_until_closed = busy_until_closed
        self.prompt = prompt
        self.selected = 0
        self.typed = initial_text
        self.checked = list(prechecked)
        self.submitting = False
        self.closed = False
        self._linger = None
        self.keys = []

    def nav(self):
        rows = []
        if self.free_text:
            rows.append(("text", ""))
        else:
            rows.extend(("option", label) for label in self.options)
            if self.other:
                rows.append(("text", "Other"))
        rows.extend(("action", label) for label in self.actions)
        return rows

    def on_key(self, text: str) -> None:
        self.keys.append(text)
        if self.closed:
            return
        rows = self.nav()
        if text == "\x1b[B" and not self.ignore_arrows:
            self.selected = min(len(rows) - 1, self.selected + 1)
        elif text == "\x1b[A" and not self.ignore_arrows:
            self.selected = max(0, self.selected - 1)
        elif text == "\r":
            kind, label = rows[self.selected]
            if kind == "option":
                if self.drop_enter > 0:
                    self.drop_enter -= 1
                    return
                if self.multi_select:
                    self.checked = (
                        [c for c in self.checked if c != label]
                        if label in self.checked else [*self.checked, label]
                    )
                else:
                    self.checked = [label]
            elif kind == "action" and (label != "Submit" or self.checked or self.typed):
                self.entered_on = label
                self._close()
        elif text == "\x13":
            self.entered_on = "submit"
            self._close()
        elif text == "\x1b":
            self.entered_on = "esc"
            self._close()
        elif rows[self.selected][0] == "text":
            if text == "\x15":
                # Module 5501: `i.ctrl && "u" === t` replaces the whole buffer with "".
                self.typed = ""
            else:
                self.typed += text

    def _close(self) -> None:
        self.submitting = True
        if self.busy_until_closed:
            return
        self._linger = self.close_after_reads
        if self._linger <= 0:
            self.closed = True

    def render(self, *, decrement: bool = True) -> str:
        if decrement and self._linger is not None:
            if self._linger <= 0:
                self.closed = True
            else:
                self._linger -= 1
        if self.closed or (self.vanish_on_type and self.typed):
            return "◆ 完成\n\n❯  Ask Doubao to do anything\n"
        rows = ["  (required) " + self.prompt]
        rows.extend(self._render_nav())
        if self.submitting:
            footer = "Submitting, waiting for confirmation… | Esc skip"
        elif self.free_text or self.nav()[self.selected][0] == "text":
            footer = "Type to answer | Ctrl+S submit | Esc skip"
        else:
            footer = "Ctrl+S submit | Space/Enter select | ↑↓ move | Esc skip"
        rows += ["", "  " + footer]
        return "\n".join(rows) + "\n"

    def _render_nav(self):
        rows = []
        if self.free_text:
            text = ("Answer: " + self.typed) if self.typed else "Answer:  [Optional]"
            rows.append(("❯ " if self.selected == 0 else "  ") + text)
        else:
            for index, label in enumerate(self.options):
                if self.multi_select:
                    mark = "✓" if label in self.checked else " "
                    row = f"[{mark}] {label}"
                else:
                    mark = "●" if label in self.checked else " "
                    row = f"({mark}) {label}"
                rows.append(("❯ " if index == self.selected else "  ") + row)
            if self.other:
                index = len(self.options)
                text = "Other: " + self.typed if self.typed else "Other:  [自定义描述...]"
                rows.append(("❯ " if index == self.selected else "  ") + "( ) " + text)
        base = 1 if self.free_text else len(self.options) + (1 if self.other else 0)
        for offset, action in enumerate(self.actions):
            rows.append(("❯ " if base + offset == self.selected else "  ") + action)
        return rows


class _FakeStaticCard:
    """A fixed card screen (multi-question / mixed / quoted cases); a confirming key closes it."""

    def __init__(self, screen: str) -> None:
        self.screen = screen
        self.closed = False
        self.keys: list = []

    def on_key(self, text: str) -> None:
        self.keys.append(text)
        if text in ("\r", "\x13", "\x1b"):
            self.closed = True

    def render(self, *, decrement: bool = True) -> str:
        if self.closed:
            return "◆ 完成\n\n❯  Ask Doubao to do anything\n"
        return self.screen


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

    def attach_card(self, sess: AgentSession, card):
        def write(text):
            card.on_key(text)
            self.fake.set_screen(sess.session_id, card.render())
            return True
        sess.write_input = write
        self.fake.set_screen(sess.session_id, card.render(decrement=False))
        return card

    async def register(self, sess: AgentSession, *, backdate: bool = True):
        interaction = await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNotNone(interaction)
        if backdate:
            # The card has been on screen for a while; Ink's 400 ms guard is over.
            interaction["created_at"] = time.time() - 2.0
        return interaction

    async def test_confirmation_card_registers_and_approve_sends_enter_only(self):
        sess = self.session("-approval")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard())
        interaction = await self.register(sess)
        self.assertEqual(interaction["kind"], "approval_request")
        self.assertEqual(interaction["options"], ["Allow once", "Reject", "Reject and stop"])
        self.assertEqual(interaction["metadata"]["prompt_signature"], parse_doubao_prompt(card.render(decrement=False))["signature"])
        self.assertIn("Remove-Item", interaction["prompt"])
        self.assertEqual(sess.status, "waiting_parent")
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertTrue(result["delivered"])
        self.assertEqual(result["response_state"], "confirmed")
        self.assertEqual(card.keys, ["\r"])
        self.assertEqual(card.entered_on, 0)
        self.assertIsNone(sess.pending_interaction)

    async def test_deny_moves_the_cursor_to_reject_before_enter_and_never_sends_a_digit(self):
        sess = self.session("-deny")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard())
        await self.register(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="deny")
        self.assertEqual(result["approval_action"], "deny")
        self.assertEqual(card.keys, ["\x1b[B", "\r"])
        self.assertEqual(card.entered_on, 1)
        self.assertFalse(any(key in ("1", "2", "3", "4") for key in card.keys))
        self.assertEqual(result["response_state"], "confirmed")

    async def test_deny_feedback_navigates_to_reject_and_stop_without_inline_text(self):
        sess = self.session("-deny-feedback")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard())
        await self.register(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="deny", custom_input="do not")
        self.assertEqual(card.keys, ["\x1b[B", "\x1b[B", "\r"])
        self.assertEqual(card.entered_on, 2)
        self.assertTrue(any("拒绝反馈" in note for note in result["notes"]))

    async def test_approve_session_uses_allow_similar_and_never_always_allow(self):
        sess = self.session("-approve-session")
        card = self.attach_card(
            sess,
            _FakeDoubaoConfirmCard(
                options=("Allow once", "Allow similar commands", "Always allow", "Reject")
            ),
        )
        await self.register(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve_session")
        self.assertEqual(card.keys, ["\x1b[B", "\r"])
        self.assertEqual(card.entered_on, 1)
        self.assertEqual(result["approval_action"], "approve_session")

        # An explicit option_index on the persistent global allow is refused, never sent.
        second = self.session("-persist")
        persist = self.attach_card(
            second, _FakeDoubaoConfirmCard(options=("Allow once", "Always allow", "Reject"))
        )
        await self.register(second)
        result = await self.hub.respond_to_interaction(second.session_id, option_index=2)
        self.assertEqual(persist.keys, ["\r"])
        self.assertEqual(persist.entered_on, 0)
        self.assertEqual(result["approval_action"], "approve_once")
        self.assertTrue(any("全局" in note for note in result["notes"]))

    async def test_degraded_and_unsafe_cards_never_approve(self):
        # No `Allow similar`: a session approval degrades to Allow once.
        sess = self.session("-degrade")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard(options=("Allow once", "Reject")))
        await self.register(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve_session")
        self.assertEqual(card.keys, ["\r"])
        self.assertEqual(result["approval_action"], "approve_once")
        self.assertTrue(any("降级" in note for note in result["notes"]))

        # A persistent-only card has no approvable row: nothing is sent.
        only_persist = self.session("-persist-only")
        stubborn = self.attach_card(only_persist, _FakeDoubaoConfirmCard(options=("Always allow", "Reject")))
        await self.register(only_persist)
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(only_persist.session_id, decision="approve")
        self.assertEqual(stubborn.keys, [])

    async def test_cursor_that_cannot_move_aborts_without_enter(self):
        sess = self.session("-stuck")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard(ignore_arrows=True))
        await self.register(sess)
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(sess.session_id, decision="deny")
        self.assertNotIn("\r", card.keys)
        self.assertEqual(card.keys, ["\x1b[B", "\x1b[B"])

    async def test_stale_card_sends_nothing(self):
        sess = self.session("-stale")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard())
        await self.register(sess)
        card.closed = True
        self.fake.set_screen(sess.session_id, card.render())
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertEqual(card.keys, [])

    async def test_slow_closing_card_is_unconfirmed_then_confirmed_by_polling(self):
        sess = self.session("-slow")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard(close_after_reads=100))
        await self.register(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertEqual(result["response_state"], "unconfirmed")
        pending = sess.pending_interaction
        self.assertIsNotNone(pending)
        self.assertFalse(self.hub._request_awaits_parent(sess.session_id, pending["request_id"]))
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        # The card finally leaves the screen: the background poll confirms and clears it.
        card.closed = True
        self.fake.set_screen(sess.session_id, card.render())
        await self.hub.poll_tui_screen_interactions_once(sess)
        await asyncio.sleep(0.2)
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNone(sess.pending_interaction)
        self.assertEqual(pending["metadata"]["response_state"], "confirmed")
        # A following identical card is a new request, detected at once.
        again = _FakeDoubaoConfirmCard()
        self.attach_card(sess, again)
        interaction = await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNotNone(interaction)
        self.assertNotEqual(interaction["request_id"], pending["request_id"])

    async def test_externally_dismissed_card_clears_after_grace(self):
        sess = self.session("-external")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard(close_after_reads=1000))
        pending = await self.register(sess)
        card.closed = True
        self.fake.set_screen(sess.session_id, "◆ 完成\n")
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIs(sess.pending_interaction, pending)
        self.assertEqual(sess.status, "waiting_parent")
        await asyncio.sleep(1.05)
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNone(sess.pending_interaction)
        self.assertEqual(sess.status, "running")

    async def test_externally_dismissed_card_with_idle_composer_clears_at_once(self):
        sess = self.session("-external-idle")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard())
        await self.register(sess)
        card.closed = True
        self.fake.set_screen(sess.session_id, card.render())
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNone(sess.pending_interaction)
        self.assertEqual(sess.status, "running")

    async def test_question_option_selection_submits_with_ctrl_s(self):
        sess = self.session("-question-option")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard())
        interaction = await self.register(sess)
        self.assertEqual(interaction["kind"], "question")
        self.assertEqual(interaction["options"], ["staging", "production", "Other"])
        await self.hub.respond_to_interaction(sess.session_id, option_index=2)
        self.assertEqual(card.keys, ["\x1b[B", "\r", "\x13"])
        self.assertEqual(card.checked, ["production"])

    async def test_question_other_row_takes_text_and_ctrl_s(self):
        sess = self.session("-question-other")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard())
        await self.register(sess)
        await self.hub.respond_to_interaction(sess.session_id, custom_input="staging-2")
        # `\x15` is the renderer's own Ctrl+U clear before the text goes in.
        self.assertEqual(card.keys, ["\x1b[B", "\x1b[B", "\x15", "staging-2", "\x13"])
        self.assertEqual(card.typed, "staging-2")

    async def test_question_deny_skips_with_escape(self):
        sess = self.session("-question-deny")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard())
        await self.register(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="deny")
        self.assertEqual(card.keys, ["\x1b"])
        self.assertEqual(card.entered_on, "esc")
        self.assertEqual(result["response_state"], "confirmed")

    async def test_free_text_question_takes_text_and_ctrl_s(self):
        sess = self.session("-question-text")
        card = self.attach_card(
            sess,
            _FakeDoubaoQuestionCard(options=(), other=False, free_text=True, prompt="Anything else?"),
        )
        await self.register(sess)
        await self.hub.respond_to_interaction(sess.session_id, custom_input="no further notes")
        self.assertEqual(card.keys, ["\x15", "no further notes", "\x13"])
        self.assertEqual(card.typed, "no further notes")

    async def test_ink_input_guard_delays_the_first_key(self):
        sess = self.session("-guard")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard())
        await self.register(sess, backdate=False)
        started = time.monotonic()
        await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertEqual(card.keys, ["\r"])

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


    # ── Second review: conflicting parameters, card identity, races, timeouts ───────────────

    async def test_deny_with_an_allowing_option_index_is_a_conflict(self):
        sess = self.session("-conflict-deny")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard())
        await self.register(sess)
        with self.assertRaises(ValueError) as raised:
            await self.hub.respond_to_interaction(sess.session_id, decision="deny", option_index=1)
        self.assertIn("冲突", str(raised.exception))
        self.assertEqual(card.keys, [])
        self.assertNotIn("response_state", (sess.pending_interaction or {})["metadata"])

        # `Always allow` is an allow as well, so naming it next to a deny is the same conflict.
        second = self.session("-conflict-persist")
        persist = self.attach_card(
            second, _FakeDoubaoConfirmCard(options=("Allow once", "Always allow", "Reject"))
        )
        await self.register(second)
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(second.session_id, decision="deny", option_index=2)
        self.assertEqual(persist.keys, [])

    async def test_approve_with_a_rejecting_option_index_is_a_conflict_and_retry_still_works(self):
        sess = self.session("-conflict-approve")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard())
        await self.register(sess)
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(sess.session_id, decision="approve", option_index=2)
        self.assertEqual(card.keys, [])
        # Nothing was claimed or sent: the parent may retry the same card cleanly.
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertEqual(result["response_state"], "confirmed")
        self.assertEqual(card.keys, ["\r"])

    async def test_a_card_that_another_one_replaced_is_never_answered(self):
        sess = self.session("-folder-replaced")
        first = self.attach_card(sess, _FakeStaticCard(folder_card("D:\\project\\a")))
        pending = await self.register(sess)
        second = self.attach_card(sess, _FakeStaticCard(folder_card("D:\\project\\b")))
        self.assertNotEqual(
            parse_doubao_prompt(first.render(decrement=False))["signature"],
            parse_doubao_prompt(second.render(decrement=False))["signature"],
        )
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertEqual(first.keys, [])
        self.assertEqual(second.keys, [])
        self.assertIs(sess.pending_interaction, pending)
        self.assertNotIn("response_state", pending["metadata"])

    async def test_a_slow_card_replaced_during_confirmation_never_poisons_the_new_one(self):
        sess = self.session("-race-replaced")
        first = self.attach_card(sess, _FakeDoubaoConfirmCard(close_after_reads=100))
        old = await self.register(sess)
        task = asyncio.create_task(
            self.hub.respond_to_interaction(sess.session_id, decision="approve")
        )
        await self.until(lambda: "\r" in first.keys)
        second = self.attach_card(
            sess, _FakeDoubaoConfirmCard(question="是否允许删除另一个目标？", command="cmd-2")
        )
        result = await task
        self.assertEqual(result["response_state"], "confirmed")
        self.assertEqual(first.entered_on, 0)
        self.assertEqual(second.keys, [])
        # The replacement registers as its own request, still waiting for the parent.
        pending = await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNotNone(pending)
        self.assertNotEqual(pending["request_id"], old["request_id"])
        self.assertNotIn("response_state", pending["metadata"])
        self.assertTrue(self.hub._request_awaits_parent(sess.session_id, pending["request_id"]))
        self.assertEqual(second.keys, [])

    async def test_concurrent_responses_write_exactly_one_enter(self):
        sess = self.session("-concurrent-same")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard())
        await self.register(sess)
        results = await asyncio.gather(
            self.hub.respond_to_interaction(sess.session_id, decision="approve"),
            self.hub.respond_to_interaction(sess.session_id, decision="approve"),
            return_exceptions=True,
        )
        self.assertEqual(card.keys.count("\r"), 1)
        self.assertEqual(sum(1 for r in results if not isinstance(r, Exception)), 1)
        self.assertEqual(sum(1 for r in results if isinstance(r, ValueError)), 1)

        # Conflicting decisions: whichever claims first is the only one that writes.
        other = self.session("-concurrent-conflict")
        other_card = self.attach_card(other, _FakeDoubaoConfirmCard())
        await self.register(other)
        results = await asyncio.gather(
            self.hub.respond_to_interaction(other.session_id, decision="approve"),
            self.hub.respond_to_interaction(other.session_id, decision="deny"),
            return_exceptions=True,
        )
        self.assertEqual(other_card.keys.count("\r"), 1)
        self.assertEqual(sum(1 for r in results if isinstance(r, ValueError)), 1)
        self.assertEqual(sum(1 for r in results if not isinstance(r, Exception)), 1)

    async def test_multi_question_and_mixed_cards_refuse_answers_but_allow_a_deny(self):
        sess = self.session("-multi-answer")
        card = self.attach_card(sess, _FakeStaticCard(QUESTION_CARD_MULTI))
        await self.register(sess)
        with self.assertRaises(ValueError) as raised:
            await self.hub.respond_to_interaction(sess.session_id, option_index=1)
        self.assertIn("多问题", str(raised.exception))
        self.assertEqual(card.keys, [])
        result = await self.hub.respond_to_interaction(sess.session_id, decision="deny")
        self.assertEqual(card.keys, ["\x1b"])
        self.assertEqual(result["response_state"], "confirmed")

        mixed = self.session("-mixed-answer")
        mixed_card = self.attach_card(mixed, _FakeStaticCard(QUESTION_CARD_MIXED))
        await self.register(mixed)
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(mixed.session_id, custom_input="staging")
        self.assertEqual(mixed_card.keys, [])

    async def test_out_of_range_option_index_never_falls_back_to_the_first_option(self):
        for index in (99, 0):
            sess = self.session(f"-range-{index}")
            card = self.attach_card(sess, _FakeDoubaoQuestionCard())
            await self.register(sess)
            with self.assertRaises(ValueError) as raised:
                await self.hub.respond_to_interaction(sess.session_id, option_index=index)
            self.assertIn("超出范围", str(raised.exception))
            self.assertEqual(card.keys, [])

    async def test_other_row_takes_custom_input_and_clears_the_previous_text(self):
        sess = self.session("-other-clear")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard(initial_text="old"))
        await self.register(sess)
        await self.hub.respond_to_interaction(
            sess.session_id, option_index=3, custom_input="canary"
        )
        self.assertEqual(card.keys, ["\x1b[B", "\x1b[B", "\x15", "canary", "\x13"])
        self.assertEqual(card.typed, "canary")
        self.assertEqual(card.entered_on, "submit")

    async def test_other_row_without_custom_input_is_refused(self):
        sess = self.session("-other-empty")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard())
        await self.register(sess)
        with self.assertRaises(ValueError) as raised:
            await self.hub.respond_to_interaction(sess.session_id, option_index=3)
        self.assertIn("custom_input", str(raised.exception))
        self.assertEqual(card.keys, [])

    async def test_a_stuck_unconfirmed_card_is_reopened_under_a_new_request_id(self):
        sess = self.session("-unconfirmed-timeout")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard(close_after_reads=100))
        old = await self.register(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertEqual(result["response_state"], "unconfirmed")
        self.assertTrue(doubao_card_submitting(card.render(decrement=False)))
        # The busy footer is gone but the card is still drawn: the answer never landed natively.
        card.submitting = False
        self.fake.set_screen(sess.session_id, card.render(decrement=False))
        with patch("session_manager.DOUBAO_UNCONFIRMED_TIMEOUT_SEC", 0.0):
            await self.hub.poll_tui_screen_interactions_once(sess)
        pending = sess.pending_interaction
        self.assertIsNotNone(pending)
        self.assertNotEqual(pending["request_id"], old["request_id"])
        self.assertNotIn("response_state", pending["metadata"])
        self.assertTrue(self.hub._request_awaits_parent(sess.session_id, pending["request_id"]))
        messages = await self.hub.db.get_recent_intermediate_messages(sess.session_id, 20)
        self.assertTrue(
            any("unconfirmed timeout" in str(row.get("content") or "") for row in messages)
        )

    async def test_a_wedged_submitting_footer_times_out_instead_of_pinning_the_session(self):
        sess = self.session("-submitting-timeout")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard(busy_until_closed=True))
        old = await self.register(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertEqual(result["response_state"], "unconfirmed")
        # The busy footer never clears: the card no longer parses, but the clock must still run.
        self.assertTrue(doubao_card_submitting(card.render(decrement=False)))
        with patch("session_manager.DOUBAO_UNCONFIRMED_TIMEOUT_SEC", 0.0):
            await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNone(sess.pending_interaction)
        self.assertEqual(old["metadata"]["response_state"], "confirmed")
        self.assertNotIn(old["request_id"], sess._doubao_unconfirmed_since)
        messages = await self.hub.db.get_recent_intermediate_messages(sess.session_id, 20)
        self.assertTrue(
            any("unconfirmed timeout" in str(row.get("content") or "") for row in messages)
        )

    async def test_the_submitting_footer_keeps_a_card_in_flight_until_it_clears(self):
        sess = self.session("-submitting")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard(busy_until_closed=True))
        old = await self.register(sess)
        result = await self.hub.respond_to_interaction(sess.session_id, decision="approve")
        self.assertEqual(result["response_state"], "unconfirmed")
        self.assertIs(sess.pending_interaction, old)
        self.assertEqual(old["metadata"]["response_state"], "unconfirmed")
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIs(sess.pending_interaction, old)
        card.closed = True
        self.fake.set_screen(sess.session_id, card.render(decrement=False))
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIsNone(sess.pending_interaction)
        self.assertEqual(old["metadata"]["response_state"], "confirmed")

    async def test_custom_input_control_bytes_never_reach_the_pty(self):
        sess = self.session("-sanitize")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard())
        await self.register(sess)
        await self.hub.respond_to_interaction(
            sess.session_id, custom_input="line1\nline2\x13\x1b"
        )
        self.assertEqual(card.typed, "line1 line2")
        self.assertNotIn("\x1b", card.keys)
        self.assertEqual(card.keys[-1], "\x13")

    async def test_a_card_that_vanished_before_ctrl_s_is_not_submitted(self):
        sess = self.session("-vanish")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard(vanish_on_type=True))
        await self.register(sess)
        with self.assertRaises(ValueError) as raised:
            await self.hub.respond_to_interaction(sess.session_id, custom_input="hello")
        self.assertIn("Ctrl+S", str(raised.exception))
        self.assertNotIn("\x13", card.keys)

    async def test_deny_with_custom_input_on_a_question_card_skips_instead_of_typing(self):
        sess = self.session("-question-deny-text")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard())
        await self.register(sess)
        result = await self.hub.respond_to_interaction(
            sess.session_id, decision="deny", custom_input="reason"
        )
        self.assertEqual(card.keys, ["\x1b"])
        self.assertEqual(card.typed, "")
        self.assertEqual(result["response_state"], "confirmed")
        self.assertTrue(any("custom_input 未发送" in note for note in result["notes"]))

    async def test_a_dropped_enter_on_an_option_is_retried_once(self):
        sess = self.session("-drop-enter")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard(drop_enter=1))
        await self.register(sess)
        await self.hub.respond_to_interaction(sess.session_id, option_index=2)
        self.assertEqual(card.keys.count("\r"), 2)
        self.assertEqual(card.keys[-1], "\x13")
        self.assertEqual(card.checked, ["production"])

    async def test_an_option_that_never_checks_aborts_before_ctrl_s(self):
        sess = self.session("-never-checks")
        card = self.attach_card(sess, _FakeDoubaoQuestionCard(drop_enter=99))
        await self.register(sess)
        with self.assertRaises(ValueError):
            await self.hub.respond_to_interaction(sess.session_id, option_index=1)
        self.assertNotIn("\x13", card.keys)

    async def test_an_already_checked_multi_select_row_is_not_toggled_off(self):
        sess = self.session("-multi-checked")
        card = self.attach_card(
            sess, _FakeDoubaoQuestionCard(multi_select=True, prechecked=("staging",))
        )
        interaction = await self.register(sess)
        self.assertEqual(interaction["metadata"]["checked_indices"], [1])
        self.assertEqual(card.keys, [])
        await self.hub.respond_to_interaction(sess.session_id, option_index=1)
        self.assertEqual(card.keys, ["\x13"])
        self.assertEqual(card.checked, ["staging"])

    async def test_assistant_prose_quoting_the_composer_does_not_clear_a_live_card(self):
        sess = self.session("-prose-composer")
        card = self.attach_card(sess, _FakeDoubaoConfirmCard(close_after_reads=1000))
        pending = await self.register(sess)
        self.fake.set_screen(
            sess.session_id,
            "◆ The hint row reads: Ask Doubao to do anything, then it waits.\n"
            "──────────\n"
            "  Allow access to these directories?\n"
            "❯ Allow once\n"
            "  Reject\n",
        )
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIs(sess.pending_interaction, pending)
        self.assertEqual(card.keys, [])

    async def test_a_replacement_needs_two_observations_before_an_unanswered_card_is_dropped(self):
        sess = self.session("-replaced-twice")
        first = self.attach_card(sess, _FakeDoubaoConfirmCard(close_after_reads=1000))
        pending = await self.register(sess)
        second = self.attach_card(
            sess, _FakeDoubaoConfirmCard(question="另一个问题？", command="cmd-2")
        )
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertIs(sess.pending_interaction, pending)
        self.assertEqual(sess._doubao_replaced_count.get(pending["request_id"]), 1)
        self.assertNotIn("absent_since", pending["metadata"])
        await self.hub.poll_tui_screen_interactions_once(sess)
        self.assertNotIn(pending["request_id"], sess._doubao_replaced_count)
        new = sess.pending_interaction
        self.assertIsNotNone(new)
        self.assertNotEqual(new["request_id"], pending["request_id"])
        self.assertEqual(first.keys, [])
        self.assertEqual(second.keys, [])


if __name__ == "__main__":
    unittest.main()
