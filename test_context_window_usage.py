"""Context window usage tracking, AGY xhigh splitting, and OMP tool intent summaries."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_window_usage import (
    AGY_ESTIMATED_WINDOW_TOKENS,
    ContextWindowCatalog,
    ContextWindowTracker,
    resolve_native_transcript_path,
    scan_file_into_tracker,
)
from model_discovery import AgentModelDiscovery, parse_omp_models_yml, parse_token_count
from session_manager import AgentSession, HubSessionManager, split_agy_model_id
from transcript_feed import tool_title


class FakeDiscovery:
    def __init__(self, engines=None):
        self._cache = {"engines": engines or {}}

    def peek_cache(self):
        return self._cache


class TokenParsingAndModelsYmlTests(unittest.TestCase):
    def test_parse_token_count_supports_units_and_rejects_invalid(self):
        self.assertEqual(parse_token_count(272000), 272000)
        self.assertEqual(parse_token_count("256k"), 256000)
        self.assertEqual(parse_token_count("256Ki"), 262144)
        self.assertEqual(parse_token_count("1M"), 1000000)
        self.assertEqual(parse_token_count("1,048,576"), 1048576)
        self.assertIsNone(parse_token_count(None))
        self.assertIsNone(parse_token_count(True))
        self.assertIsNone(parse_token_count(0))

    def test_parse_omp_models_yml_reads_exact_context_windows(self):
        with tempfile.TemporaryDirectory(prefix="hub-models-yml-") as tmp:
            p = Path(tmp) / "models.yml"
            p.write_text(
                "providers:\n"
                "  clay:\n"
                "    baseUrl: https://example.test/v1\n"
                "    models:\n"
                "      - id: deepseek-flash\n"
                "        contextWindow: 1048576\n"
                "        maxTokens: 393216\n"
                "      - id: minimax-m3.1-flash\n"
                "        contextWindow: 1000000 # comment\n"
                "        maxTokens: 524288\n",
                encoding="utf-8",
            )
            parsed = parse_omp_models_yml(p)
            self.assertEqual(parsed["clay/deepseek-flash"]["context_window_tokens"], 1048576)
            self.assertEqual(parsed["clay/minimax-m3.1-flash"]["context_window_tokens"], 1000000)


class EngineFormulaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hub-cw-formulas-")
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unsupported_engines_return_none(self):
        for eng in ("dsh", "pwsh", "desktop_gui", "generic"):
            tracker = ContextWindowTracker(eng, catalog=ContextWindowCatalog(home=self.home))
            self.assertFalse(tracker.supported)
            self.assertFalse(tracker.observe_record({"type": "token_count", "tokens": 1234}))
            self.assertIsNone(tracker.snapshot())

    def test_codex_uses_last_token_usage_and_ignores_cached_double_count_and_cumulative_total(self):
        disc = FakeDiscovery(
            {
                "codex": {
                    "default_model": "gpt-6.1-sol",
                    "model_details": [
                        {
                            "id": "gpt-6.1-sol",
                            "context_window_tokens": 272000,
                            "effective_context_window_percent": 95,
                            "auto_compact_token_limit": 244800,
                        }
                    ],
                }
            }
        )
        cat = ContextWindowCatalog(home=self.home, discovery=disc)
        tracker = ContextWindowTracker("codex", model="gpt-6.1-sol", catalog=cat, cli_args=["-c", "model_auto_compact_token_limit=220000"])
        snap0 = tracker.snapshot()
        self.assertEqual(snap0["window_tokens"], 258400)
        self.assertEqual(snap0["compact_tokens"], 220000)

        # session_meta.payload.context_window is a dict {"window_id": ...}, never a token int
        tracker.observe_record({"type": "session_meta", "payload": {"model": "gpt-6.1-sol", "context_window": {"window_id": "abc"}}})
        tracker.observe_record(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "model_context_window": 258400,
                        "total_token_usage": {"input_tokens": 999999, "total_tokens": 1000500},
                        "last_token_usage": {
                            "input_tokens": 40000,
                            "cached_input_tokens": 35000,
                            "output_tokens": 500,
                            "total_tokens": 40500,
                        },
                    },
                },
            }
        )
        snap = tracker.snapshot()
        self.assertEqual(snap["used_tokens"], 40500)
        self.assertEqual(snap["window_tokens"], 258400)
        self.assertEqual(snap["compact_tokens"], 220000)
        self.assertFalse(snap["window_estimated"])

        tracker.observe_record({"type": "compacted", "payload": {}})
        self.assertTrue(tracker.snapshot()["awaiting_post_compact"])
        self.assertTrue(tracker.snapshot()["compacted"])

    def test_kimi_counts_only_main_agent_and_reads_config_toml(self):
        kdir = self.home / ".kimi-code"
        kdir.mkdir()
        (kdir / "config.toml").write_text(
            'default_model = "kimi-code/k3.1-pro"\n'
            '[models."kimi-code/k3.1-pro"]\n'
            'max_context_size = 1048576\n'
            '[models."kimi-code/k3-256k"]\n'
            'max_context_size = 262144\n',
            encoding="utf-8",
        )
        cat = ContextWindowCatalog(home=self.home)
        tracker = ContextWindowTracker("kimi", catalog=cat)
        self.assertEqual(tracker.snapshot()["window_tokens"], 1048576)
        self.assertEqual(tracker.snapshot()["compact_tokens"], int(1048576 * 0.85))

        tracker.observe_record({"message": {"type": "profile.bind", "agentId": "main", "modelAlias": "kimi-code/k3-256k"}})
        # Subagent token counting must be ignored
        self.assertFalse(
            tracker.observe_record({"message": {"type": "token_counting.measured", "agentId": "sub_1", "tokens": 99000}})
        )
        # Main agent updates occupancy
        self.assertTrue(
            tracker.observe_record({"message": {"type": "token_counting.measured", "agentId": "main", "tokens": 64000}})
        )
        snap = tracker.snapshot()
        self.assertEqual(snap["model"], "kimi-code/k3-256k")
        self.assertEqual(snap["used_tokens"], 64000)
        self.assertEqual(snap["window_tokens"], 262144)
        self.assertEqual(snap["compact_tokens"], min(int(262144 * 0.85), 262144 - 50000))

        tracker.observe_record({"message": {"type": "context.apply_compaction", "agentId": "main", "tokensAfter": 14000}})
        snap2 = tracker.snapshot()
        self.assertEqual(snap2["used_tokens"], 14000)
        self.assertTrue(snap2["compacted"])
        self.assertFalse(snap2["awaiting_post_compact"])

    def test_omp_reads_models_yml_overlay_threshold_and_ignores_side_model_usage(self):
        odir = self.home / ".omp" / "agent"
        odir.mkdir(parents=True)
        (odir / "models.yml").write_text(
            "providers:\n"
            "  clay:\n"
            "    models:\n"
            "      - id: deepseek-flash\n"
            "        contextWindow: 1048576\n",
            encoding="utf-8",
        )
        overlay = self.home / "omp_compaction_s1.yml"
        overlay.write_text('# header\n{"compaction": {"enabled": true, "thresholdPercent": 70, "thresholdTokens": -1}}\n', encoding="utf-8")
        cat = ContextWindowCatalog(home=self.home)
        tracker = ContextWindowTracker("omp", model="clay/deepseek-flash", catalog=cat, omp_overlay_path=overlay)
        self.assertEqual(tracker.snapshot()["window_tokens"], 1048576)
        self.assertEqual(tracker.snapshot()["compact_tokens"], (1048576 * 70) // 100)

        # Side model_usage calls (title generation) do not pollute occupancy
        self.assertFalse(tracker.observe_record({"type": "model_usage", "purpose": "title", "usage": {"totalTokens": 500}}))
        # Assistant aborted message ignored
        self.assertFalse(
            tracker.observe_record({"type": "message", "message": {"role": "assistant", "stopReason": "aborted", "usage": {"totalTokens": 999}}})
        )
        self.assertTrue(
            tracker.observe_record(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "model": "deepseek-flash",
                        "provider": "clay",
                        "stopReason": "stop",
                        "usage": {"input": 10000, "cacheRead": 5000, "output": 300, "totalTokens": 15300},
                    },
                }
            )
        )
        self.assertEqual(tracker.snapshot()["used_tokens"], 15300)

        tracker.observe_record({"type": "compaction", "tokensAfter": 12000})
        self.assertEqual(tracker.snapshot()["used_tokens"], 12000)
        self.assertTrue(tracker.snapshot()["compacted"])

    def test_claude_cache_sum_sidechain_skip_and_override(self):
        cat = ContextWindowCatalog(home=self.home)
        tracker = ContextWindowTracker(
            "claude",
            model="claude-sonnet-5-5[1m]",
            catalog=cat,
            extra_env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"},
        )
        snap0 = tracker.snapshot()
        self.assertEqual(snap0["window_tokens"], 1_000_000)
        # effective = 1_000_000 - 20_000 = 980_000; 70% = 686_000 (< default 967_000)
        self.assertEqual(snap0["compact_tokens"], 686_000)

        self.assertFalse(tracker.observe_record({"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 99999}}}))
        self.assertFalse(tracker.observe_record({"type": "assistant", "message": {"model": "<synthetic>", "usage": {"input_tokens": 99999}}}))
        self.assertTrue(
            tracker.observe_record(
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-5-5[1m]",
                        "usage": {
                            "input_tokens": 1200,
                            "cache_creation_input_tokens": 3000,
                            "cache_read_input_tokens": 45000,
                            "output_tokens": 400,
                        },
                    },
                }
            )
        )
        self.assertEqual(tracker.snapshot()["used_tokens"], 49600)

        tracker.observe_record({"type": "system", "subtype": "compact_boundary", "compactMetadata": {"preTokens": 49600}})
        self.assertTrue(tracker.snapshot()["awaiting_post_compact"])

        tracker.observe_record({"type": "result", "modelUsage": {"claude-sonnet-5-5[1m]": {"contextWindow": 1000000}}})
        self.assertEqual(tracker.snapshot()["window_tokens"], 1000000)

    def test_agy_estimated_window_and_checkpoint_recovery(self):
        cat = ContextWindowCatalog(home=self.home)
        tracker = ContextWindowTracker("agy", model="gemini-4-hydrogen-xhigh", catalog=cat)
        snap0 = tracker.snapshot()
        self.assertEqual(snap0["window_tokens"], AGY_ESTIMATED_WINDOW_TOKENS)
        self.assertEqual(snap0["compact_tokens"], AGY_ESTIMATED_WINDOW_TOKENS)
        self.assertTrue(snap0["window_estimated"])
        self.assertIn("256k", snap0["estimated_note"])

        tracker.observe_record({"type": "USER_INPUT", "content": "x" * 4000})  # 1000 est prompt tokens
        tracker.observe_record(
            {"type": "PLANNER_RESPONSE", "status": "DONE", "input_tokens": 3000, "cache_read_tokens": 40000, "output_tokens": 500}
        )
        self.assertEqual(tracker.snapshot()["used_tokens"], 43500)

        tracker.observe_record({"type": "CHECKPOINT", "status": "DONE", "input_tokens": 250000, "output_tokens": 6000})
        snap_c = tracker.snapshot()
        self.assertTrue(snap_c["awaiting_post_compact"])
        self.assertEqual(snap_c["used_tokens"], 7000)
        self.assertEqual(snap_c["source"], "checkpoint_est")


class HubIntegrationAndFixesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temporary = tempfile.TemporaryDirectory(prefix="hub-cw-int-")
        self.root = Path(self.temporary.name)
        profiles = self.root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=self.root)
        self.hub.context_window_catalog = ContextWindowCatalog(home=self.root, discovery=self.hub.model_discovery)
        await self.hub.start()

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temporary.cleanup()

    async def until(self, condition):
        async with asyncio.timeout(5):
            while not condition():
                await asyncio.sleep(0.02)

    async def test_live_watcher_updates_transcript_version_and_persists_to_archive_and_resume(self):
        sess = AgentSession("codex-cw1", "codex", "codex", "tui", "CW", str(self.root), "unused", [])
        sess.engine = "codex"
        sess.effective_model = "gpt-6.1-sol"
        sess.profile_snapshot = {"engine": "codex", "model": "gpt-6.1-sol"}
        sess.status = "running"
        sess._loop = asyncio.get_running_loop()
        self.hub.sessions[sess.session_id] = sess

        work = self.root / "codex-work"
        work.mkdir()
        rollout = work / "rollout-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        sess.resume_attach = True
        sess.conversation_ref = {"session_id": "s1", "rollout_path": str(rollout)}

        feed1 = await self.hub.get_transcript_view(sess.session_id)
        v1 = feed1["version"]
        poll_same = await self.hub.get_transcript_view(sess.session_id, since=v1)
        self.assertTrue(poll_same["unchanged"])

        task = self.hub._schedule(self.hub._watch_codex_rollout(sess, work))
        try:
            with rollout.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "model_context_window": 258400,
                                    "last_token_usage": {"input_tokens": 50000, "cached_input_tokens": 10000, "output_tokens": 1200, "total_tokens": 51200},
                                },
                            },
                        }
                    )
                    + "\n"
                )
            await self.until(lambda: (sess.context_window_snapshot() or {}).get("used_tokens") == 51200)
            v2 = self.hub.transcript_version(sess.session_id, 80)
            self.assertNotEqual(v1, v2)
            feed2 = await self.hub.get_transcript_view(sess.session_id, since=v1)
            self.assertFalse(feed2.get("unchanged"))
            self.assertEqual(feed2["context_window"]["used_tokens"], 51200)
            self.assertEqual(feed2["context_window"]["window_tokens"], 258400)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        await self.hub._archive_session(sess, end_reason="exited")
        saved = await self.hub.db.get_session_record(sess.session_id)
        self.assertEqual(saved["context_window"]["used_tokens"], 51200)

        resumed = await self.hub._make_resumed_session(
            sess.session_id, saved, saved["profile_snapshot"], saved["conversation_ref"], {"args": []}
        )
        self.assertEqual(resumed.context_window_snapshot()["used_tokens"], 51200)

    async def test_archived_session_without_context_window_json_backfills_from_native_file(self):
        brain = self.root / "agy-arch" / ".system_generated" / "logs"
        brain.mkdir(parents=True)
        tp = brain / "transcript.jsonl"
        tp.write_text(
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "input_tokens": 2000, "cache_read_tokens": 18000, "output_tokens": 300}) + "\n",
            encoding="utf-8",
        )
        sess = AgentSession("agy-old", "agy", "agy", "tui", "Old AGY", str(self.root), "unused", [])
        sess.engine = "agy"
        sess.status = "exited"
        sess.conversation_ref = {"conversation_id": "agy-arch", "transcript_path": str(tp)}
        await self.hub.db.upsert_session(
            sess.to_dict(),
            archive={
                "end_reason": "exited",
                "archived_at": 100.0,
                "spawned_at": 90.0,
                "profile_snapshot": json.dumps({"engine": "agy"}),
                "conversation_ref": json.dumps(sess.conversation_ref),
                "context_window_json": "",
            },
        )
        await self.hub.db.flush()
        resolved = resolve_native_transcript_path("agy", {"conversation_ref": sess.conversation_ref})
        self.assertEqual(resolved, tp)

        feed = await self.hub.get_transcript_view("agy-old")
        self.assertIsNotNone(feed["context_window"])
        self.assertEqual(feed["context_window"]["used_tokens"], 20300)
        self.assertEqual(feed["context_window"]["window_tokens"], AGY_ESTIMATED_WINDOW_TOKENS)
        self.assertTrue(feed["context_window"]["window_estimated"])

    async def test_agy_xhigh_thinking_level_splits_and_resolves_launch(self):
        self.assertEqual(split_agy_model_id("gemini-4-hydrogen-xhigh"), ("gemini-4-hydrogen", "xhigh"))
        fake_agy = {
            "engine": "agy",
            "name": "Antigravity CLI (AGY)",
            "binary": "agy.EXE",
            "installed": True,
            "default_model": "gemini-4-hydrogen",
            "default_thinking_level": "medium",
            "available_models": [
                "gemini-4-hydrogen-low",
                "gemini-4-hydrogen-medium",
                "gemini-4-hydrogen-high",
                "gemini-4-hydrogen-xhigh",
            ],
            "model_details": [
                {"id": "gemini-4-hydrogen-low", "display_name": "Gemini 4 Hydrogen (Low)", "built_in_thinking_level": "low", "supported_thinking_levels": ["low"]},
                {"id": "gemini-4-hydrogen-medium", "display_name": "Gemini 4 Hydrogen (Medium)", "built_in_thinking_level": "medium", "supported_thinking_levels": ["medium"]},
                {"id": "gemini-4-hydrogen-high", "display_name": "Gemini 4 Hydrogen (High)", "built_in_thinking_level": "high", "supported_thinking_levels": ["high"]},
                {"id": "gemini-4-hydrogen-xhigh", "display_name": "Gemini 4 Hydrogen (XHigh)", "built_in_thinking_level": "xhigh", "supported_thinking_levels": ["xhigh"]},
            ],
            "available_thinking_levels": ["low", "medium", "high", "xhigh"],
        }
        self.hub.load_profiles(discovery={"discovered_at": 1.0, "engines": {"agy": fake_agy}})
        opts = self.hub.engine_model_options("agy")
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0]["id"], "gemini-4-hydrogen")
        self.assertEqual(opts[0]["label"], "Gemini 4 Hydrogen")
        self.assertEqual(opts[0]["thinking_levels"], ["low", "medium", "high", "xhigh"])

        self.hub.save_or_update_profile({
            "id": "agy-xhigh-prof",
            "name": "AGY Test",
            "engine": "agy",
            "command": "agy.EXE",
            "default_model": "gemini-4-hydrogen-xhigh",
        })
        launch = self.hub.resolve_launch("agy-xhigh-prof")
        self.assertEqual(launch["model"], "gemini-4-hydrogen")
        self.assertEqual(launch["thinking_level"], "xhigh")
        self.assertEqual(launch["cli_model"], "gemini-4-hydrogen-xhigh")
        self.assertIn("gemini-4-hydrogen-xhigh", launch["args"])

    async def test_omp_tool_call_summary_uses_intent_and_full_arguments(self):
        self.assertEqual(tool_title("bash", json.dumps({"command": "ls", "i": "Listing files"})), "Listing files")
        self.assertEqual(tool_title("web_search", json.dumps({"intent": "Searching specs", "query": "abc"})), "Searching specs")

        sess = AgentSession("omp-intent", "omp", "omp", "tui", "OMP", str(self.root), "unused", [])
        sess.engine = "omp"
        sess.status = "running"
        sess._loop = asyncio.get_running_loop()
        self.hub.sessions[sess.session_id] = sess

        work = self.root / "omp-work"
        work.mkdir()
        log = work / "native.jsonl"
        log.touch()
        sess.resume_attach = True
        sess.conversation_ref = {"session_file": str(log)}

        task = self.hub._schedule(self.hub._watch_omp_session(sess, work))
        try:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": "call_ws_1",
                                        "name": "web_search",
                                        "arguments": {"query": "MiniMax M3.1 Flash contextWindow", "i": "Searching MiniMax specs"},
                                        "intent": "Searching MiniMax specs",
                                    }
                                ],
                                "stopReason": "toolUse",
                            },
                        }
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps(
                        {
                            "type": "custom",
                            "customType": "tool_execution_start",
                            "data": {
                                "toolCallId": "call_ws_1",
                                "toolName": "web_search",
                                "intent": "Searching MiniMax specs",
                            },
                        }
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "call_ws_1",
                                "toolName": "web_search",
                                "content": [{"type": "text", "text": "timeout"}],
                                "isError": True,
                            },
                        }
                    )
                    + "\n"
                )
            await self.until(lambda: any(e["type"] == "tool_result" for e in self.hub.get_worker_events(sess.session_id)))
            feed = await self.hub.get_transcript_view(sess.session_id)
            tool = next(entry["tool"] for entry in feed["entries"] if entry["kind"] == "tool")
            self.assertEqual(tool["name"], "web_search")
            self.assertEqual(tool["title"], "Searching MiniMax specs")
            self.assertEqual(tool["status"], "error")
            self.assertIn("MiniMax M3.1 Flash contextWindow", tool["input"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
