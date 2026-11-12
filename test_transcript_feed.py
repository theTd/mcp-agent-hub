"""The console transcript feed (transcript_feed.py): pairing, de-duplication, titles, interactions."""
import time
import unittest

from transcript_feed import (
    build_transcript_feed,
    interaction_summary,
    parse_parent_notice,
    tool_title,
)


def event(seq, type_, content="", **extra):
    return {"seq": seq, "timestamp": 1000.0 + seq, "type": type_, "content": content, **extra}


def feed(events, **kwargs):
    kwargs.setdefault("meta", {"status": "completed", "last_task": "检查这个目录"})
    return build_transcript_feed(session_id="s", events=events, **kwargs)


def kinds(result):
    return [entry["kind"] for entry in result["entries"]]


HUB_NOTICE = (
    "Approval request\n"
    "- Engine: `codex` (`codex_tui_screen_detector`) | request_id=`int-1`\n"
    "- Title: Codex TUI Approval Modal\n"
    "- Details: Would you like to run the following command?\n"
    "Reason: list the shots\n"
    "$ py -3 C:\\Users\\me\\tools\\scripts\\deep\\game_client.py status host\n"
    "- Options:\n"
    "  [1] Yes, proceed (y)\n"
    "  [2] No, and tell Codex what to do differently (esc)\n"
    "Use respond_to_subagent(session_id='s', request_id='int-1') with decision='approve' / 'deny'."
)


class ToolEntryTests(unittest.TestCase):
    def test_call_and_result_pair_by_call_id_even_when_interleaved(self):
        result = feed([
            event(1, "tool_call", data={"tool": "read_file", "call_id": "a", "input": '{"path": "a.txt"}'}),
            event(2, "tool_call", data={"tool": "read_file", "call_id": "b", "input": '{"path": "b.txt"}'}),
            event(3, "tool_result", data={"tool": "read_file", "call_id": "b", "output": "B", "ok": True}),
            event(4, "tool_result", data={"tool": "read_file", "call_id": "a", "output": "boom", "ok": False}),
        ])
        tools = [entry["tool"] for entry in result["entries"] if entry["kind"] == "tool"]
        self.assertEqual([(t["title"], t["output"], t["status"]) for t in tools],
                         [("a.txt", "boom", "error"), ("b.txt", "B", "ok")])
        self.assertNotIn("_call_id", result["entries"][1])

    def test_step_marker_is_not_a_tool(self):
        result = feed([event(1, "tool_call", data={"tool": "step", "step_marker": True})])
        self.assertNotIn("tool", kinds(result))

    def test_legacy_content_without_data_still_pairs_and_detects_failure(self):
        result = feed([
            event(1, "tool_call", "shell: ls -la"),
            event(2, "tool_result", "shell: Traceback (most recent call last):\n  boom"),
        ])
        tool = next(entry["tool"] for entry in result["entries"] if entry["kind"] == "tool")
        self.assertEqual(tool["name"], "shell")
        self.assertEqual(tool["status"], "error")

    def test_result_without_a_call_gets_its_own_row(self):
        result = feed([event(1, "tool_result", data={"call_id": "x", "output": "late", "ok": True})])
        tool = next(entry["tool"] for entry in result["entries"] if entry["kind"] == "tool")
        self.assertEqual((tool["output"], tool["status"]), ("late", "ok"))


class ToolTitleTests(unittest.TestCase):
    def test_describing_argument_wins(self):
        self.assertEqual(tool_title("pwsh", '{"description": "Run tests", "command": "pytest"}'), "Run tests")

    def test_json_without_a_describing_argument_lists_scalars(self):
        self.assertEqual(tool_title("wait", '{"cell_id": "88", "yield_time_ms": 30000, "flag": true}'),
                         "cell_id=88 · yield_time_ms=30000")

    def test_code_cells(self):
        self.assertEqual(tool_title("exec", 'const cmds = [\n  "py drive.py shot a",\n  "py drive.py shot b"\n];'),
                         "py drive.py shot a 等")
        self.assertEqual(tool_title("exec", 'await tools.view_image({path:"E:\\\\a\\\\b\\\\c\\\\d\\\\shot.png"})'),
                         "view_image · …\\shot.png")
        self.assertEqual(tool_title("exec", 'await tools.write_stdin({session_id: 3})'), "调用 write_stdin")
        patch_cell = 'const patch = "*** Begin Patch\\n*** Add File: E:\\\\x\\\\y\\\\z\\\\w\\\\SHOTS.md\\n+# Pass';
        self.assertEqual(tool_title("exec", patch_cell), "apply_patch · Add …\\SHOTS.md")

    def test_long_absolute_paths_are_shortened_but_urls_are_not(self):
        title = tool_title("shell", "py -3 C:\\Users\\me\\skills\\remote\\scripts\\client.py status see https://a.io/b/c/d/e")
        self.assertEqual(title, "py -3 …\\client.py status see https://a.io/b/c/d/e")

    def test_backslash_heavy_input_does_not_backtrack(self):
        # Regression: a Windows path in an unterminated `command: "...` hung the transcript request.
        text = 'tools.exec({command: "' + "E:\\\\mcpe\\\\x" * 3000
        started = time.perf_counter()
        tool_title("exec", text)
        self.assertLess(time.perf_counter() - started, 1.0)


class InteractionTests(unittest.TestCase):
    def test_hub_notice_is_split_into_prompt_and_options(self):
        notice = parse_parent_notice(HUB_NOTICE)
        self.assertEqual(notice["options"], ["Yes, proceed (y)", "No, and tell Codex what to do differently (esc)"])
        self.assertTrue(notice["prompt"].startswith("Would you like to run"))
        self.assertNotIn("respond_to_subagent", notice["prompt"])
        self.assertIsNone(parse_parent_notice("plain question?"))

    def test_clipped_notice_still_parses(self):
        notice = parse_parent_notice(HUB_NOTICE.split("- Options:")[0].rstrip("\n"))
        self.assertEqual(notice["options"], [])
        self.assertIn("$ py -3", notice["prompt"])

    def test_summary_prefers_the_command(self):
        self.assertEqual(interaction_summary(parse_parent_notice(HUB_NOTICE)["prompt"]),
                         "py -3 …\\game_client.py status host")
        self.assertEqual(interaction_summary("Allow this call? `pwsh` asks for **full access**."),
                         "Allow this call? pwsh asks for full access.")

    def test_legacy_approval_gets_resolution_and_worker_notices_are_dropped(self):
        result = feed(
            [event(1, "approval_request", HUB_NOTICE, request_id="int-1")],
            parent_messages=[
                {"direction": "worker", "type": "approval_request", "content": HUB_NOTICE, "timestamp": 1001.0},
                {"direction": "parent", "content": "Yes, proceed (y)", "message_id": "pmsg-1",
                 "resolved_question_id": "int-1", "timestamp": 1002.0},
                {"direction": "parent", "content": "也看一下日志", "message_id": "pmsg-2", "timestamp": 1003.0},
            ],
        )
        self.assertEqual(kinds(result), ["task", "interaction", "parent"])
        ask = result["entries"][1]
        self.assertEqual(ask["interaction"]["options"][0], "Yes, proceed (y)")
        self.assertEqual(ask["interaction"]["summary"], "py -3 …\\game_client.py status host")
        self.assertEqual(ask["resolution"]["text"], "Yes, proceed (y)")

    def test_auto_approved_request_gets_its_own_row(self):
        interaction = {"request_id": "a1", "kind": "approval_request", "title": "Run?", "prompt": "$ pytest -q"}
        result = feed([event(1, "approval_auto_resolved", "Automatic approval rule r1 matched",
                             request_id="a1", data={"interaction": interaction})])
        ask = next(entry for entry in result["entries"] if entry["kind"] == "interaction")
        self.assertEqual(ask["interaction"]["summary"], "pytest -q")
        self.assertEqual(ask["resolution"]["by"], "auto")

    def test_pending_interaction_is_marked_and_exposed(self):
        pending = {"request_id": "q1", "kind": "question", "title": "选择方案", "prompt": "继续吗？",
                   "options": ["继续", "停止"], "metadata": {"token": "secret"}}
        result = feed([event(1, "question", "继续吗？", request_id="q1", data={"interaction": pending})],
                      meta={"status": "waiting_parent", "last_task": "t"}, pending_interaction=pending)
        self.assertEqual(result["pending_interaction"]["options"], ["继续", "停止"])
        self.assertNotIn("metadata", result["pending_interaction"])
        ask = next(entry for entry in result["entries"] if entry["kind"] == "interaction")
        self.assertTrue(ask["pending"])


class MessageTests(unittest.TestCase):
    def test_final_answer_is_not_repeated_as_a_message(self):
        result = feed([event(1, "message", "完成了"), event(2, "turn_completed", "完成了")])
        self.assertEqual(kinds(result), ["task", "final"])

    def test_one_line_preview_of_the_final_answer_is_dropped(self):
        final = "Done. Nothing committed.\n\n## 1. Changes\n- the card's input row is now hidden when empty"
        preview = "Done. Nothing committed. ## 1. Changes - the card's input row is"
        result = feed([event(1, "message", preview), event(2, "turn_completed", final)])
        self.assertEqual(kinds(result), ["task", "final"])

    def test_stored_final_response_promotes_the_matching_message(self):
        result = feed([event(1, "progress", "答案")],
                      meta={"status": "completed", "last_task": "t", "final_response": "答案"})
        self.assertEqual(kinds(result), ["task", "final"])

    def test_long_message_and_its_shorter_stored_copy_are_one_entry(self):
        text = "长" * 4507
        result = feed([event(1, "progress", text)],
                      messages=[{"timestamp": 1001.0, "content": text[:4000], "source": "dsh_job_log"}])
        self.assertEqual(kinds(result), ["task", "assistant"])

    def test_bracket_tags_are_notices_but_leading_links_are_messages(self):
        result = feed([], messages=[
            {"timestamp": 1001.0, "content": "[Parent replied] 好的", "source": "hub"},
            {"timestamp": 1002.0, "content": "[Report](https://a.io/r) is ready", "source": "agent"},
        ])
        self.assertEqual(kinds(result), ["task", "notice", "assistant"])

    def test_reasoning_and_reports(self):
        result = feed([event(1, "reasoning", "想一想"), event(2, "message_to_parent", "进度：一半")])
        self.assertEqual(kinds(result), ["task", "thinking", "assistant"])
        self.assertEqual(result["entries"][2].get("channel"), "report")

    def test_limit_keeps_the_task_and_reports_hidden_entries(self):
        events = [event(i, "message", f"m{i}") for i in range(1, 31)]
        result = feed(events, limit=10)
        self.assertEqual(result["entries"][0]["kind"], "task")
        self.assertEqual(result["entries"][-1]["text"], "m30")
        self.assertTrue(result["has_more"])
        self.assertGreater(result["hidden_count"], 0)


if __name__ == "__main__":
    unittest.main()
