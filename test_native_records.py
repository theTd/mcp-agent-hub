"""Exercise real transcript watchers using isolated native records, without agent processes."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from native_session_launch import strip_legacy_worker_bridge_args
from session_manager import AgentSession, HubSessionManager, approval_launch_flags
from transcript_summarizer import KimiTranscriptSummarizer
from transcript_tail import read_complete_lines


class FakePty:
    pid = 999999
    def __init__(self):
        self.inputs = []
        self.alive = True
    def isalive(self):
        return self.alive
    def write(self, text):
        self.inputs.append(text)
    def terminate(self, force=True):
        pass


class LaunchTests(unittest.TestCase):
    def test_strip_only_hub_additions_from_old_snapshots(self):
        runtime = Path("C:/old/.runtime_mcp_configs")
        bridge = str(runtime / "agy_sess_test")
        settings = {"agentMode": "auto", "trustedWorkspaces": ["C:/project", bridge],
                    "permissions": {"allow": ["read(*)", "mcp(hub-bridge/*)"]}, "userValue": 42}
        args = ["--append-system-prompt", "User instructions\n[Sub-Agent Bridge Info: session_id=test]\nReport task complete",
                "--allowedTools=Read,Bash(*hub_worker.py*),PowerShell(*hub_worker.py*),Edit",
                "--add-dir", bridge, "--add-dir=C:/user-workspace", "--settings", json.dumps(settings),
                "--append-system-prompt=Keep this", "--settings=user.json"]
        clean = strip_legacy_worker_bridge_args(args, runtime)
        self.assertEqual(clean[:3], ["--append-system-prompt", "User instructions", "--allowedTools=Read,Edit"])
        self.assertIn("--add-dir=C:/user-workspace", clean)
        saved = json.loads(clean[clean.index("--settings") + 1])
        self.assertEqual(saved["trustedWorkspaces"], ["C:/project"])
        self.assertEqual(saved["permissions"]["allow"], ["read(*)"])
        self.assertEqual(saved["userValue"], 42)
        self.assertIn("--append-system-prompt=Keep this", clean)
        self.assertIn("--settings=user.json", clean)
        self.assertEqual(clean, strip_legacy_worker_bridge_args(clean, runtime))
        self.assertNotIn("hub_worker", " ".join(approval_launch_flags("claude")))

    def test_tail_waits_for_complete_utf8_row_and_handles_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native.jsonl"
            row = (json.dumps({"text": "结果"}, ensure_ascii=False) + "\n").encode("utf-8")
            path.write_bytes(row[:-2])
            self.assertEqual(read_complete_lines(path, 0), ([], 0))
            with path.open("ab") as f:
                f.write(row[-2:])
            lines, offset = read_complete_lines(path, 0)
            self.assertEqual(json.loads(lines[0])["text"], "结果")
            self.assertEqual(offset, len(row))
            path.write_bytes(b'{}\n')
            self.assertEqual(read_complete_lines(path, offset), (['{}\n'], 3))

    def test_portable_payload_excludes_bridge(self):
        root = Path(__file__).parent
        payload = (root / "desktop/portable/payload.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("native_session_launch.py", payload)
        for name in ("hub_worker.py", "worker_bridge_mcp.py"):
            self.assertNotIn(name, payload)
            self.assertFalse((root / name).exists())


class NativeRecordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temporary = tempfile.TemporaryDirectory(prefix="hub-native-")
        self.root = Path(self.temporary.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        await self.hub.start()

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temporary.cleanup()

    async def test_launch_source_survives_persistence_resume_and_mailbox_delivery(self):
        for source in ("agent", "user"):
            sess = await self.hub.create_session(
                "desktop-gui", hwnd=123, launch_source=source,
            )
            self.assertEqual(sess.to_dict()["launch_source"], source)
            await self.hub.db.flush()
            record = await self.hub.db.get_session_record(sess.session_id)
            resumed = await self.hub._make_resumed_session(
                sess.session_id, record, record["profile_snapshot"], {}, {},
            )
            self.hub.sessions[sess.session_id] = resumed
            self.assertEqual(resumed.launch_source, source)
            for kind, category in (("question", "question_waiting_reply"),
                                   ("approval_request", "approval_waiting_reply")):
                event = await self.hub.record_worker_event(sess.session_id, {
                    "type": kind, "category": category, "content": "Reply?",
                    # Callers cannot override the session's notification ownership.
                    "launch_source": "user" if source == "agent" else "agent",
                })
                self.assertEqual(event["launch_source"], source)
                self.assertEqual(self.hub.get_worker_events(sess.session_id)[-1], event)
                self.assertEqual(resumed.status, "waiting_parent")

    async def test_legacy_and_default_launches_keep_user_notifications(self):
        legacy = self.session("codex", "-legacy")
        self.assertEqual(legacy.launch_source, "user")
        sess = await self.hub.create_session("desktop-gui", hwnd=123)
        self.assertEqual(sess.launch_source, "user")

    def session(self, engine, suffix=""):
        sid = f"{engine}-native{suffix}"
        sess = AgentSession(sid, engine, engine, "tui", "Native fixture", str(self.root), "unused", [])
        sess.engine = engine
        sess.status = "running"
        sess.pty = FakePty()
        sess._loop = asyncio.get_running_loop()
        self.hub.sessions[sid] = sess
        return sess

    async def until(self, condition):
        async with asyncio.timeout(5):
            while not condition():
                await asyncio.sleep(0.02)

    def append(self, path, *rows):
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def exercise(self, engine):
        sess = self.session(engine)
        work = self.root / engine
        work.mkdir()
        path = work / "native.jsonl"
        ref = {"session_id": "fixture", "rollout_path": str(path), "session_file": str(path),
               "transcript_path": str(path)}
        if engine == "agy":
            path = work / ".system_generated/logs/transcript.jsonl"
            path.parent.mkdir(parents=True)
            ref["brain_dir"] = str(work)
        elif engine == "kimi":
            path = work / "agents/main/wire.jsonl"
            path.parent.mkdir(parents=True)
            ref["session_dir"] = str(work)
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = ref
        question = {"questions": [{"header": "Branch", "question": "选择哪个分支？", "options": [{"label": "main"}, {"label": "dev"}]}]}
        if engine == "codex":
            ask = {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input", "call_id": "q1", "arguments": json.dumps(question)}}
            final = {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "原生最终结果"}}
            new_turn = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]}}
            watch = self.hub._watch_codex_rollout(sess, work)
        elif engine == "omp":
            ask = {"type": "custom", "customType": "tool_execution_start", "data": {"toolName": "ask", "toolCallId": "q1", "args": question}}
            final = {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "原生最终结果"}], "stopReason": "stop"}}
            new_turn = {"type": "message", "message": {"role": "user", "content": "continue"}}
            watch = self.hub._watch_omp_session(sess, work)
        elif engine == "agy":
            ask = {"type": "PLANNER_RESPONSE", "step_index": 1, "content": "", "tool_calls": json.dumps([{"name": "ask_question", "args": question}])}
            final = {"type": "PLANNER_RESPONSE", "content": "原生最终结果", "tool_calls": "[]"}
            new_turn = {"type": "USER_INPUT", "content": "continue"}
            watch = self.hub._watch_agy_transcript(sess, self.root)
        elif engine == "claude":
            ask = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": question}]}}
            final = {"type": "assistant", "message": {"content": [{"type": "text", "text": "原生最终结果"}], "stop_reason": "end_turn"}}
            new_turn = {"type": "user", "message": {"content": "continue"}}
            watch = self.hub._watch_claude_session(sess, work)
        else:
            ask = {"type": "interaction.request", "id": "q1", "kind": "question", "request": question}
            final = {"kind": "event", "type": "agent.turn.ended"}
            new_turn = {"type": "turn.prompt", "origin": {"kind": "user"}, "input": [{"type": "text", "text": "continue"}]}
            watch = self.hub._watch_kimi_session(sess, self.root)
        task = self.hub._schedule(watch)
        try:
            self.append(path, ask)
            await self.until(lambda: sess.pending_interaction is not None)
            received = self.hub.get_worker_events(session_id=sess.session_id, after_seq=0)
            self.assertTrue(received)
            self.assertEqual(sess.status, "waiting_parent")
            self.assertEqual(sess.pending_interaction["prompt"], "选择哪个分支？")
            await self.hub.respond_to_interaction(sess.session_id, option_index=1)
            await self.until(lambda: bool(sess.pty.inputs))
            self.assertIsNone(sess.pending_interaction)
            # Re-reading a native question after it was answered must not register it again.
            self.append(path, ask)
            if engine == "kimi":
                self.append(path, {"type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "原生最终结果"}}})
            self.append(path, final, final)
            await self.until(lambda: sess.final_response == "原生最终结果")
            self.assertEqual(sess.status, "completed")
            events = [e for e in self.hub.worker_events if e["session_id"] == sess.session_id]
            self.assertEqual(sum(e["type"] == "question" for e in events), 1)
            self.assertEqual(sum(e["type"] == "turn_completed" for e in events), 1)
            self.assertTrue(sess.completion_reported)
            self.append(path, new_turn)
            await self.until(lambda: sess.final_response_from_previous_turn)
            # A prompt typed straight into the TUI shows in the transcript as the user's message.
            typed = [e["content"] for e in self.hub.worker_events
                     if e["session_id"] == sess.session_id and e["type"] == "user_message"]
            self.assertEqual(typed, ["continue"])
            feed = await self.hub.get_transcript_view(sess.session_id)
            self.assertIn({"origin": "user", "text": "continue"},
                          [{"origin": e.get("origin"), "text": e.get("text")} for e in feed["entries"] if e["kind"] == "parent"])
            await self.hub.record_telemetry(sess.session_id, {"hook_event": "AssistantMessage", "intermediate_messages": ["还在工作？"], "final_assistant_text": "还在工作？"})
            result = await self.hub.get_subagent_final_response(sess.session_id)
            self.assertEqual(result["final_response"], "原生最终结果")
            self.assertTrue(result["from_previous_turn"])
            self.assertEqual(sess.status, "running")
            if engine == "kimi":
                self.append(path, {"type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "原生最终结果"}}})
            self.append(path, final)
            await self.until(lambda: sess.status == "completed")
            self.assertFalse(sess.final_response_from_previous_turn)
            await self.hub._archive_session(sess, end_reason="exited")
            saved = await self.hub.db.get_session_record(sess.session_id)
            self.assertEqual(saved["final_response"], "原生最终结果")
            self.assertTrue(saved["archived_at"])
            self.assertTrue(saved["parent_messages"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_codex_native_roundtrip(self):
        await self.exercise("codex")
    async def test_omp_native_roundtrip(self):
        await self.exercise("omp")
    async def test_agy_native_roundtrip(self):
        await self.exercise("agy")
    async def test_claude_native_roundtrip(self):
        await self.exercise("claude")
    async def test_kimi_native_roundtrip(self):
        await self.exercise("kimi")

    async def keep_thinking_out_of_messages(self, engine, streamed=False):
        """Thinking parts become console-only reasoning; parents and the final reply only get the text."""
        sess = self.session(engine, "-think")
        work = self.root / f"{engine}-think"
        work.mkdir()
        path = work / "native.jsonl"
        ref = {"session_id": "fixture", "session_file": str(path), "transcript_path": str(path)}
        thought, reply = "先想一想：要不要再读一遍文件", "答复正文：两处都改好了"
        if engine == "omp":
            rows = [{"type": "message", "message": {"role": "assistant", "stopReason": "stop", "content": [
                {"type": "thinking", "thinking": thought}, {"type": "text", "text": reply}]}}]
            watch = self.hub._watch_omp_session(sess, work)
        elif engine == "claude":
            rows = [{"type": "assistant", "message": {"stop_reason": "end_turn", "content": [
                {"type": "thinking", "thinking": thought}, {"type": "thinking", "thinking": ""},
                {"type": "text", "text": reply}]}}]
            watch = self.hub._watch_claude_session(sess, work)
        elif engine == "agy":
            path = work / ".system_generated/logs/transcript.jsonl"
            path.parent.mkdir(parents=True)
            ref["brain_dir"] = str(work)
            rows = [{"type": "PLANNER_RESPONSE", "thinking": thought, "content": reply, "tool_calls": "[]"}]
            watch = self.hub._watch_agy_transcript(sess, self.root)
        else:
            path = work / "agents/main/wire.jsonl"
            path.parent.mkdir(parents=True)
            ref["session_dir"] = str(work)
            whole = {"type": "agent.message.appended", "message": {"message": {"role": "assistant", "content": [
                {"type": "think", "think": thought}, {"type": "text", "text": reply}]}}}
            rows = [whole, {"kind": "event", "type": "agent.turn.ended"}]
            if streamed:
                # Current Kimi: the streamed parts first, then the same step again as one message.
                stream = [{"type": "context.append_loop_event", "event": {"type": "content.part", "part": part}}
                          for part in ({"type": "think", "think": thought}, {"type": "text", "text": reply})]
                rows = stream + rows
            watch = self.hub._watch_kimi_session(sess, self.root)
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = ref
        task = self.hub._schedule(watch)
        try:
            self.append(path, *rows)
            await self.until(lambda: sess.final_response == reply)
            events = [e for e in self.hub.worker_events if e["session_id"] == sess.session_id]
            self.assertEqual([e["content"] for e in events if e["type"] == "reasoning"], [thought])
            self.assertLessEqual(sum(e["type"] == "progress" and e["content"] == reply for e in events), 1)
            self.assertFalse([e for e in events if e["type"] != "reasoning" and "想" in str(e.get("content") or "")])
            self.assertFalse([e for e in events if "Thinking completed" in str(e.get("content") or "")])
            stored = await self.hub.db.get_recent_intermediate_messages(sess.session_id, 40)
            self.assertFalse([m for m in stored if "想" in str(m.get("content") or "")])
            feed = await self.hub.get_transcript_view(sess.session_id)
            kinds = [entry["kind"] for entry in feed["entries"]]
            self.assertIn("thinking", kinds)
            self.assertEqual(kinds.count("final"), 1)
            self.assertNotIn(reply, [entry.get("text") for entry in feed["entries"] if entry["kind"] == "assistant"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_omp_thinking_is_reasoning(self):
        await self.keep_thinking_out_of_messages("omp")
    async def test_claude_thinking_is_reasoning(self):
        await self.keep_thinking_out_of_messages("claude")
    async def test_agy_thinking_is_reasoning(self):
        await self.keep_thinking_out_of_messages("agy")
    async def test_kimi_thinking_is_reasoning(self):
        await self.keep_thinking_out_of_messages("kimi")

    async def test_agy_tool_arguments_are_decoded_and_titled_by_tool_summary(self):
        """agy writes each argument value as JSON text; the feed shows its toolSummary as the title."""
        sess = self.session("agy", "-tools")
        work = self.root / "agy-tools"
        path = work / ".system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"brain_dir": str(work)}
        call = {"name": "run_command", "args": {
            "CommandLine": json.dumps("Get-ChildItem -Name"), "WaitMsBeforeAsync": "5000",
            "toolSummary": json.dumps("List the files here")}}
        task = self.hub._schedule(self.hub._watch_agy_transcript(sess, self.root))
        try:
            self.append(path, {"type": "PLANNER_RESPONSE", "content": "", "tool_calls": json.dumps([call])})
            await self.until(lambda: sess._inflight_transcript_tool is not None)
            self.assertEqual(sess._inflight_transcript_tool["command"], "Get-ChildItem -Name")
            feed = await self.hub.get_transcript_view(sess.session_id)
            tool = next(entry["tool"] for entry in feed["entries"] if entry["kind"] == "tool")
            self.assertEqual(tool["title"], "List the files here")
            self.assertEqual(json.loads(tool["input"])["WaitMsBeforeAsync"], 5000)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_clipped_tool_arguments_keep_a_title_from_the_whole_text(self):
        sess = self.session("claude", "-long")
        long_command = "echo " + "x" * 5000
        await self.hub.record_telemetry(sess.session_id, {
            "source": "claude_transcript", "hook_event": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": long_command, "description": "Print a long line"}})
        feed = await self.hub.get_transcript_view(sess.session_id)
        tool = next(entry["tool"] for entry in feed["entries"] if entry["kind"] == "tool")
        self.assertEqual(tool["title"], "Print a long line")
        self.assertIn("已截断", tool["input"])
    async def test_kimi_streamed_step_is_recorded_once(self):
        await self.keep_thinking_out_of_messages("kimi", streamed=True)

    async def test_kimi_final_joins_the_last_steps_streamed_text_parts(self):
        sess = self.session("kimi", "-parts")
        work = self.root / "kimi-parts"
        path = work / "agents/main/wire.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"session_dir": str(work)}
        loop = lambda event: {"type": "context.append_loop_event", "event": event}
        text = lambda value: loop({"type": "content.part", "part": {"type": "text", "text": value}})
        task = self.hub._schedule(self.hub._watch_kimi_session(sess, self.root))
        try:
            self.append(path, loop({"type": "step.begin"}), text("先看文件。"),
                        loop({"type": "step.begin"}), text("第一段结论。"), text("第二段结论。"),
                        {"kind": "event", "type": "agent.turn.ended"})
            await self.until(lambda: sess.status == "completed")
            self.assertEqual(sess.final_response, "第一段结论。\n第二段结论。")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def kimi_wire(self, suffix):
        sess = self.session("kimi", suffix)
        work = self.root / f"kimi{suffix}"
        path = work / "agents/main/wire.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"session_dir": str(work)}
        return sess, path

    @staticmethod
    def kimi_step(text):
        part = {"type": "content.part", "part": {"type": "text", "text": text}}
        return [{"type": "context.append_loop_event", "event": {"type": "step.begin"}},
                {"type": "context.append_loop_event", "event": part},
                {"type": "context.append_loop_event", "event": {"type": "step.end", "finishReason": "end_turn"}}]

    @staticmethod
    def kimi_turn_ended(turn_id):
        return {"type": "turn.ended", "agentId": "main", "turnId": turn_id, "reason": "completed"}

    async def test_kimi_turn_ended_alone_completes_the_turn(self):
        # kimi-ed1ee650 (2026-11-13): current Kimi writes only turn.ended, and the session stayed
        # "running" after its answer because the watcher waited for agent.turn.ended.
        sess, path = self.kimi_wire("-turn-ended")
        task = self.hub._schedule(self.hub._watch_kimi_session(sess, self.root))
        try:
            self.append(path, *self.kimi_step("审计已写好。"), self.kimi_turn_ended(0),
                        {"type": "prompt.completed", "agentId": "main", "reason": "completed"})
            await self.until(lambda: sess.status == "completed")
            self.assertEqual(sess.final_response, "审计已写好。")
            self.assertTrue(sess.completion_reported)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_kimi_late_legacy_turn_end_does_not_end_the_next_turn(self):
        # Older builds wrote agent.turn.ended in the end-of-turn batch, often after the next prompt.
        sess, path = self.kimi_wire("-legacy-late")
        prompt = {"type": "turn.prompt", "origin": {"kind": "user"}, "input": [{"type": "text", "text": "再查一遍"}]}
        legacy_batch = [{"kind": "event", "type": "agent.message.appended", "message": {"message": {
                            "role": "assistant", "content": [{"type": "text", "text": "第一轮结论。"}]}}},
                        {"kind": "event", "type": "agent.turn.ended", "turnId": 0, "outcome": "done"}]
        task = self.hub._schedule(self.hub._watch_kimi_session(sess, self.root))
        try:
            self.append(path, *self.kimi_step("第一轮结论。"), self.kimi_turn_ended(0))
            await self.until(lambda: sess.status == "completed")
            self.append(path, prompt, *legacy_batch)
            await self.until(lambda: sess.final_response_from_previous_turn)
            await asyncio.sleep(0.6)  # more than two watcher ticks, so the late legacy row is read
            self.assertEqual(sess.status, "running")
            self.append(path, *self.kimi_step("第二轮结论。"), self.kimi_turn_ended(1))
            await self.until(lambda: sess.status == "completed")
            self.assertEqual(sess.final_response, "第二轮结论。")
            ends = [e for e in self.hub.worker_events
                    if e["session_id"] == sess.session_id and e["type"] == "turn_completed"]
            self.assertEqual([e["content"] for e in ends], ["第一轮结论。", "第二轮结论。"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_kimi_summary_counts_turns_once_in_either_format(self):
        current = self.root / "kimi-summary-current.jsonl"
        both = self.root / "kimi-summary-both.jsonl"
        legacy = self.root / "kimi-summary-legacy.jsonl"
        self.append(current, *self.kimi_step("a"), self.kimi_turn_ended(0), *self.kimi_step("b"), self.kimi_turn_ended(1))
        self.append(both, self.kimi_turn_ended(0), {"kind": "event", "type": "agent.turn.ended", "turnId": 0})
        self.append(legacy, {"kind": "event", "type": "agent.turn.ended"})
        summarize = lambda path: KimiTranscriptSummarizer().summarize(path, {}, [], [])["turns_completed"]
        self.assertEqual([summarize(current), summarize(both), summarize(legacy)], [2, 1, 1])

    async def test_text_the_engine_writes_as_the_user_is_not_a_typed_prompt(self):
        for engine in ("codex", "claude"):
            with self.subTest(engine=engine):
                sess = self.session(engine, "-injected")
                work = self.root / f"{engine}-injected"
                work.mkdir()
                path = work / "native.jsonl"
                path.touch()
                sess.resume_attach = True
                sess.conversation_ref = {"session_id": "fixture", "rollout_path": str(path),
                                         "session_file": str(path), "transcript_path": str(path)}
                if engine == "codex":
                    user = lambda text: {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                                              "content": [{"type": "input_text", "text": text}]}}
                    rows = [user("# AGENTS.md instructions for E:\\work\n\n<INSTRUCTIONS>be brief</INSTRUCTIONS>"),
                            user("<environment_context>\n  <cwd>E:\\work</cwd>\n</environment_context>"),
                            user("真正输入的问题")]
                    watch = self.hub._watch_codex_rollout(sess, work)
                else:
                    rows = [{"type": "user", "isMeta": True, "message": {"content": "Caveat: generated by commands"}},
                            {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
                            {"type": "user", "message": {"content": [{"type": "text", "text": "真正输入的问题"}]}}]
                    watch = self.hub._watch_claude_session(sess, work)
                task = self.hub._schedule(watch)
                try:
                    self.append(path, *rows)
                    events = lambda: [e["content"] for e in self.hub.worker_events
                                      if e["session_id"] == sess.session_id and e["type"] == "user_message"]
                    await self.until(lambda: bool(events()))
                    await asyncio.sleep(0.1)
                    self.assertEqual(events(), ["真正输入的问题"])
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_marker_head_is_read_once_it_is_complete(self):
        transcript = self.root / "growing.jsonl"
        head = self.hub._task_with_session_marker("codex-grow", "task") + "\n"
        transcript.write_text(head + "x" * 64 + "\n", encoding="utf-8")
        with patch("session_manager.TRANSCRIPT_MARKER_SCAN_BYTES", len(head)):
            self.assertEqual(self.hub._transcript_marker_ids(transcript), {"codex-grow"})
            with transcript.open("a", encoding="utf-8") as fh:
                fh.write('{"type":"event_msg"}\n')
            # The record grew at its end only: the cached head answers without reading the file.
            with patch("builtins.open", side_effect=AssertionError("head read again")):
                self.assertEqual(self.hub._transcript_marker_ids(transcript), {"codex-grow"})

    async def test_marker_prevents_cross_session_attach(self):
        first, second = self.session("codex", "-a"), self.session("codex", "-b")
        transcript = self.root / "marked.jsonl"
        transcript.write_text(self.hub._task_with_session_marker(first.session_id, "task"), encoding="utf-8")
        self.assertIsNone(await self.hub._claim_conversation(second, [transcript]))
        self.assertEqual(await self.hub._claim_conversation(first, [transcript]), transcript)

    async def test_start_and_resume_do_not_inject_bridge(self):
        modes = {"codex": "codex_positional", "agy": "agy_interactive_flag", "claude": "claude_positional", "omp": "omp_positional", "kimi": "pty_input"}
        for engine, mode in modes.items():
            with self.subTest(engine=engine):
                self.hub.profiles[engine] = {"name": engine, "kind": "tui", "task_mode": mode}
                launch = {"engine": engine, "cwd": str(self.root), "command": "unused", "args": [],
                          "model": "fixture", "thinking_level": "low", "env": {},
                          "mcp_servers": {}, "disabled_mcp_servers": [], "strict_mcp_config": False}
                pending = []
                def schedule(coro):
                    pending.append(coro.cr_frame.f_locals.copy())
                    coro.close()
                with patch.object(self.hub, "resolve_launch", return_value=launch), patch.object(self.hub, "_schedule", side_effect=schedule), patch.object(AgentSession, "start_pty", lambda s, *a, **k: setattr(s, "pty", FakePty())):
                    sess = await self.hub.create_session(engine, task="test task")
                text = " ".join(sess.args) + " ".join(str(p.get("task_to_inject") or "") for p in pending)
                self.assertIn("[Hub Session: session_id=", text)
                for forbidden in ("hub-bridge", "hub_worker.py", "report_complete", "ask_parent", "Sub-Agent Bridge Info"):
                    self.assertNotIn(forbidden, text)
                self.assertFalse(list(self.hub.runtime_configs_dir.glob("agy_sess_*")))
                record = {"session_id": sess.session_id, "engine": engine, "profile_id": engine,
                          "profile_snapshot": {"command": "unused", "cwd": str(self.root), "task_mode": mode,
                              "args": ["--append-system-prompt=User text\n[Sub-Agent Bridge Info: session_id=old]\ncall report_complete"]},
                          "conversation_ref": {"session_id": "old", "conversation_id": "old", "session_file": "old.jsonl", "session_dir": str(self.root)}}
                built = self.hub.build_resume_launch(record)
                self.assertIn("--append-system-prompt=User text", built["args"])
                self.assertNotIn("Bridge Info", " ".join(built["args"]))
                restored = await self.hub._make_resumed_session(sess.session_id, record, record["profile_snapshot"], record["conversation_ref"], built)
                self.assertNotIn("Bridge Info", " ".join(restored.profile_snapshot["args"]))
                self.assertIn("--append-system-prompt=User text", restored.profile_snapshot["args"])

    async def test_screen_question_is_adopted_by_native_record(self):
        sess = self.session("kimi")
        screen = await self.hub.register_pending_interaction(sess, "question", "Question", "which?", ["main", "dev"], "kimi_tui_screen_detector")
        native = await self.hub.register_pending_interaction(session=sess, kind="question", title="Question", prompt="which?", options=["main", "dev"], source="kimi_wire_jsonl_interaction_request", request_id="native-1")
        self.assertEqual(screen["created_at"], native["created_at"])
        self.assertEqual(sum(e["type"] == "question" for e in self.hub.worker_events), 1)
        await self.hub.respond_to_interaction(sess.session_id, option_index=1)
        await self.hub.register_pending_interaction(sess, "question", "Question", "which?", ["main", "dev"], "kimi_wire_jsonl_interaction_request", request_id="native-1")
        self.assertIsNone(sess.pending_interaction)

    async def test_answered_screen_question_ignores_delayed_native_replays(self):
        sess = self.session("kimi")
        await self.hub.register_pending_interaction(sess, "question", "Question", "which?", ["main", "dev"], "kimi_tui_screen_detector")
        await self.hub.respond_to_interaction(sess.session_id, option_index=1)
        for _ in range(2):
            await self.hub.register_pending_interaction(sess, "question", "Question", "which?", ["main", "dev"], "kimi_wire_jsonl_interaction_request", request_id="delayed-1")
        self.assertIsNone(sess.pending_interaction)
        self.assertEqual(sum(e["type"] == "question" for e in self.hub.worker_events), 1)

    async def test_native_progress_persists_and_wakes_listener(self):
        sess = self.session("codex")
        await self.hub.record_telemetry(sess.session_id, {"hook_event": "PreToolUse", "tool_name": "read", "tool_input": "file"})
        result = self.hub.get_worker_events(session_id=sess.session_id, after_seq=0)
        self.assertEqual(result[0]["type"], "tool_call")
        await self.hub.record_telemetry(sess.session_id, {"hook_event": "PostToolUse", "tool_name": "read", "tool_response": "contents"})
        await self.hub.record_telemetry(sess.session_id, {"hook_event": "AssistantMessage", "intermediate_messages": ["working"]})
        self.assertEqual([e["type"] for e in self.hub.worker_events], ["tool_call", "tool_result", "progress"])
        await self.hub.db.flush()
        import sqlite3
        with closing(sqlite3.connect(self.root / "hub_state.db")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM worker_events").fetchone()[0], 3)

    async def test_dsh_native_message_question_and_result(self):
        import httpx
        sess = self.session("dsh")
        sess.pty = None
        sess.dsh_spec = {"tier": "flash"}
        sess.dsh_job_id = "native-job"
        log = self.root / "dsh.jsonl"
        log.touch()
        answered = False
        delivered = []
        class NativeClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def get(self, url, **kwargs):
                await asyncio.sleep(0.01)
                if url.endswith("/messages"):
                    messages = [] if answered else [{"kind": "message", "sequence": 1, "message_id": "dsh-q", "text": "Which branch?", "expects_reply": True}]
                    return httpx.Response(200, json={"result": {"messages": messages}})
                return httpx.Response(200, json={"job": {"status": "done" if answered else "running", "result": "DSH native result" if answered else ""}})
        def reply(url, **kwargs):
            nonlocal answered
            answered = True
            delivered.append(kwargs["json"])
            return httpx.Response(200, json={"ok": True})
        with patch("session_manager.httpx.AsyncClient", return_value=NativeClient()), patch("session_manager.httpx.post", side_effect=reply):
            task = self.hub._schedule(self.hub._watch_dsh_hub_job(sess, sess.dsh_job_id, "fixture-token", log_path=log))
            await self.until(lambda: sess.pending_interaction is not None)
            await self.hub.deliver_parent_message(sess.session_id, "main")
            await asyncio.wait_for(task, timeout=5)
        self.assertEqual(delivered, [{"message": "main", "reply_to": "dsh-q"}])
        self.assertEqual(sess.final_response, "DSH native result")
        self.assertEqual(sum(e["type"] == "turn_completed" for e in self.hub.worker_events), 1)

    async def test_resume_skips_existing_transcript_rows(self):
        sess = self.session("claude")
        path = self.root / "resume.jsonl"
        row = {"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}], "stop_reason": "end_turn"}}
        self.append(path, row)
        sess.resume_attach = True
        sess.attach_offset = path.stat().st_size
        sess.conversation_ref = {"session_id": "resume", "transcript_path": str(path)}
        sess.set_final_response("old", "report_complete_mcp")  # Historic records remain readable.
        self.hub.begin_new_turn(sess)
        task = self.hub._schedule(self.hub._watch_claude_session(sess, self.root))
        try:
            await self.until(lambda: sess.transcript_path is not None)
            self.assertFalse(self.hub.worker_events)
            row["message"]["content"][0]["text"] = "new"
            encoded = (json.dumps(row) + "\n").encode("utf-8")
            with path.open("ab") as f:
                f.write(encoded[:-2])
            await asyncio.sleep(0.9)
            self.assertEqual(sess.final_response, "old")
            with path.open("ab") as f:
                f.write(encoded[-2:])
            await self.until(lambda: sess.final_response == "new")
            self.assertEqual(sum(e["type"] == "turn_completed" for e in self.hub.worker_events), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_no_final_answer_from_intermediate_prose(self):
        sess = self.session("codex")
        await self.hub.record_telemetry(sess.session_id, {"hook_event": "AssistantMessage", "final_assistant_text": "需要选择吗？", "intermediate_messages": ["需要选择吗？"]})
        result = await self.hub.get_subagent_final_response(sess.session_id)
        self.assertIsNone(result["final_response"])
        self.assertIsNone(sess.pending_interaction)
        self.assertEqual([e["type"] for e in self.hub.worker_events], ["progress"])

    async def test_native_empty_completion_keeps_previous_answer(self):
        sess = self.session("claude")
        sess.set_final_response("previous answer", "native_transcript")
        self.hub.begin_new_turn(sess)
        for _ in range(2):
            await self.hub.record_telemetry(sess.session_id, {"hook_event": "TurnComplete", "final_assistant_text": ""})
        self.assertEqual(sess.status, "completed")
        self.assertTrue(sess.final_response_from_previous_turn)
        self.assertEqual(sess.final_response, "previous answer")
        self.assertEqual(sum(e["type"] == "turn_completed" for e in self.hub.worker_events), 1)
        before = {"status": sess.status, "completion_reported": sess.completion_reported,
                  "from_previous_turn": sess.final_response_from_previous_turn,
                  "turn_completion_emitted": sess._turn_completion_emitted}
        self.hub.begin_new_turn(sess, status="running")
        self.hub._restore_turn_after_failed_message(sess, before)
        await self.hub.record_telemetry(sess.session_id, {"hook_event": "TurnComplete"})
        self.assertEqual(sum(e["type"] == "turn_completed" for e in self.hub.worker_events), 1)

    async def test_codex_tool_result_resolves_only_synchronous_question(self):
        sess = self.session("codex")
        path = self.root / "codex-questions.jsonl"
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"rollout_path": str(path)}
        task = self.hub._schedule(self.hub._watch_codex_rollout(sess, self.root))
        def row(ptype, **kwargs):
            return {"type": "response_item", "payload": {"type": ptype, **kwargs}}
        try:
            args = json.dumps({"questions": [{"question": "Which branch?"}]})
            self.append(path, row("function_call", name="request_user_input", call_id="sync", arguments=args))
            await self.until(lambda: sess.pending_question_id == "sync")
            self.append(path, row("function_call_output", call_id="sync", output='{"answers":{"branch":"main"}}'))
            await self.until(lambda: sess.pending_interaction is None)
            self.append(path, row("function_call", name="request_user_input_async", call_id="async", arguments=args))
            await self.until(lambda: sess.pending_question_id == "async")
            self.append(path, row("function_call_output", call_id="async", output="Question presented"),
                        row("function_call", name="read_file", call_id="progress", arguments="{}"))
            await self.until(lambda: sess.step_count == 3)
            self.assertEqual(sess.pending_question_id, "async")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_omp_prose_requires_native_stop_reason(self):
        sess = self.session("omp")
        path = self.root / "omp-stop.jsonl"
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"session_file": str(path)}
        task = self.hub._schedule(self.hub._watch_omp_session(sess, self.root))
        try:
            msg = {"role": "assistant", "content": [{"type": "text", "text": "still working?"}]}
            self.append(path, {"type": "message", "message": msg})
            await self.until(lambda: sess.transcript_path is not None)
            await asyncio.sleep(0.9)
            self.assertEqual(sess.status, "running")
            self.assertIsNone(sess.final_response)
            self.assertIsNone(sess.pending_interaction)
            self.assertEqual([e["type"] for e in self.hub.worker_events], ["progress"])
            msg["stopReason"] = "stop"
            self.append(path, {"type": "message", "message": msg})
            await self.until(lambda: sess.status == "completed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_exit_drains_delayed_partial_native_completion(self):
        sess = self.session("codex")
        path = self.root / "codex-exit.jsonl"
        path.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"rollout_path": str(path)}
        task = self.hub._schedule(self.hub._watch_codex_rollout(sess, self.root))
        try:
            await self.until(lambda: sess.transcript_path is not None)
            sess.pty.alive = False
            sess._finish_read(0)
            exiting = asyncio.create_task(self.hub._on_session_exit(sess, 0))
            row = (json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "delayed answer"}}) + "\n").encode()
            path.write_bytes(row[:-2])
            await asyncio.sleep(1.1)
            self.assertIsNone(sess.final_response)
            with path.open("ab") as stream:
                stream.write(row[-2:])
            await asyncio.wait_for(exiting, timeout=5)
            await asyncio.wait_for(task, timeout=3)
            self.assertEqual(sess.final_response, "delayed answer")
            self.assertEqual(sess.status, "exited")
            self.assertTrue(sess.completion_reported)
            saved = await self.hub.db.get_session_record(sess.session_id)
            self.assertEqual(saved["final_response"], "delayed answer")
            self.assertEqual(sum(e["type"] == "turn_completed" for e in self.hub.worker_events), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
