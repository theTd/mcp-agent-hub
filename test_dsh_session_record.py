"""DSH native session record: frame walking, incremental reads, telemetry mapping, Hub drain."""
import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dsh_session_record import (
    DshSessionRecordReader,
    RecordDecodeError,
    _node_decoder,
    _stdlib_decoder,
    locate_dsh_session_record,
    native_events_to_telemetry,
    zstd_frame_end,
)

MAGIC = struct.pack("<I", 0xFD2FB528)


def raw_frame(payload: bytes) -> bytes:
    """A valid zstd frame holding `payload` in one uncompressed (raw) block."""
    assert len(payload) < 256
    descriptor = bytes([0x20])  # single segment, 1-byte content size, no checksum, no dictionary
    block = (1 | (len(payload) << 3)).to_bytes(3, "little")  # last block, raw
    return MAGIC + descriptor + bytes([len(payload)]) + block + payload


def raw_decoder(frames):
    return b"".join(frame[9:] for frame in frames)


def skippable(payload: bytes) -> bytes:
    return struct.pack("<II", 0x184D2A5E, len(payload)) + payload


def line(seq, type_, data, time_ms=None):
    return json.dumps({"seq": seq, "time": time_ms or 1_000_000 + seq, "type": type_, "data": data}).encode() + b"\n"


CALL = line(2, "tool/call", {"callId": "c1", "name": "run_code",
                             "arguments": json.dumps({"description": "Read x", "code": "print(1)"})})
RESULT = line(3, "tool/result", {"message": {"toolCallId": "c1", "isError": True,
                                             "content": [{"type": "text", "text": "Traceback: boom"}]}})
MESSAGE = line(1, "assistant/message", {"message": {"content": [
    {"type": "reasoning", "text": "think first"}, {"type": "text", "text": "Reading x."}]}})


class FrameWalkerTests(unittest.TestCase):
    def test_frame_ends_and_incomplete_frames(self):
        frame = raw_frame(b"hello")
        self.assertEqual(zstd_frame_end(frame, 0), len(frame))
        self.assertIsNone(zstd_frame_end(frame[:-1], 0))
        self.assertIsNone(zstd_frame_end(frame[:6], 0))
        skip = skippable(b"meta")
        self.assertEqual(zstd_frame_end(skip + frame, 0), len(skip))
        with self.assertRaises(RecordDecodeError):
            zstd_frame_end(b"not a frame", 0)

    def test_rle_block_counts_one_byte(self):
        block = (1 | (1 << 1) | (50 << 3)).to_bytes(3, "little")  # last block, RLE of 50 bytes
        frame = MAGIC + bytes([0x20, 50]) + block + b"x"
        self.assertEqual(zstd_frame_end(frame, 0), len(frame))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-record-")
        self.path = Path(self.temp.name) / "session.v4.jsonl.zstd"

    def tearDown(self):
        self.temp.cleanup()

    def test_reads_whole_frames_only_and_joins_lines_split_across_frames(self):
        head, tail = CALL[:20], CALL[20:]
        partial = raw_frame(RESULT)
        self.path.write_bytes(raw_frame(MESSAGE + head) + skippable(b"x") + raw_frame(tail) + partial[:-3])
        reader = DshSessionRecordReader(self.path, decoder=raw_decoder)
        self.assertEqual([event["seq"] for event in reader.read_new_events()], [1, 2])
        self.assertEqual(reader.read_new_events(), [])
        with self.path.open("ab") as stream:
            stream.write(partial[-3:])
        self.assertEqual([event["seq"] for event in reader.read_new_events()], [3])

    def test_truncated_file_is_read_again_from_the_start(self):
        self.path.write_bytes(raw_frame(MESSAGE) + raw_frame(CALL))
        reader = DshSessionRecordReader(self.path, decoder=raw_decoder)
        self.assertEqual(len(reader.read_new_events()), 2)
        self.path.write_bytes(raw_frame(MESSAGE))
        self.assertEqual([event["seq"] for event in reader.read_new_events()], [1])

    def test_any_decoder_failure_is_a_record_error(self):
        self.path.write_bytes(raw_frame(MESSAGE))

        def broken(frames):
            raise ValueError("checksum mismatch")

        with self.assertRaises(RecordDecodeError):
            DshSessionRecordReader(self.path, decoder=broken).read_new_events()

    def test_resume_offset_skips_what_was_read_and_survives_a_rewritten_file(self):
        self.path.write_bytes(raw_frame(MESSAGE) + raw_frame(CALL[:20]) + raw_frame(CALL[20:]))
        first = DshSessionRecordReader(self.path, decoder=raw_decoder)
        self.assertEqual(len(first.read_new_events()), 2)
        with self.path.open("ab") as stream:
            stream.write(raw_frame(RESULT))
        resumed = DshSessionRecordReader(self.path, decoder=raw_decoder, start_offset=first.resume_offset)
        self.assertEqual([event["seq"] for event in resumed.read_new_events()], [3])
        # A read that ends inside a line keeps the last safe position: never resume mid-line.
        self.path.write_bytes(raw_frame(MESSAGE) + raw_frame(CALL[:20]))
        partial = DshSessionRecordReader(self.path, decoder=raw_decoder)
        partial.read_new_events()
        self.assertEqual(partial.resume_offset, 0)
        with self.path.open("ab") as stream:
            stream.write(raw_frame(CALL[20:]))
        partial.read_new_events()
        self.assertEqual(partial.resume_offset, self.path.stat().st_size)
        # An offset that is not a frame start (another file version) reads everything again.
        stale = DshSessionRecordReader(self.path, decoder=raw_decoder, start_offset=3)
        self.assertEqual([event["seq"] for event in stale.read_new_events()], [1, 2])

    def test_plain_jsonl_record(self):
        plain = Path(self.temp.name) / "session.v3.jsonl"
        plain.write_bytes(MESSAGE + CALL[:10])
        reader = DshSessionRecordReader(plain)
        self.assertEqual([event["seq"] for event in reader.read_new_events()], [1])
        with plain.open("ab") as stream:
            stream.write(CALL[10:])
        self.assertEqual([event["seq"] for event in reader.read_new_events()], [2])

    def test_real_decoder_reads_a_raw_block_frame(self):
        decoder = _stdlib_decoder() or _node_decoder()
        if decoder is None:
            self.skipTest("no zstd decoder (needs Python 3.14 or Node >= 24)")
        try:
            self.assertEqual(decoder([raw_frame(b"abc"), raw_frame(b"def")]), b"abcdef")
        except RecordDecodeError as error:
            self.skipTest(f"decoder unavailable here: {error}")

    def test_locate_prefers_the_newest_format_and_rejects_glob_ids(self):
        home = Path(self.temp.name) / "home"
        folder = home / "sessions" / "proj-slug" / "session-abc"
        folder.mkdir(parents=True)
        (folder / "session.v3.jsonl").write_text("", encoding="utf-8")
        (folder / "session.v4.jsonl.zstd").write_bytes(b"")
        self.assertEqual(locate_dsh_session_record("session-abc", home).name, "session.v4.jsonl.zstd")
        self.assertIsNone(locate_dsh_session_record("session-*", home))
        self.assertIsNone(locate_dsh_session_record("../session-abc", home))
        self.assertIsNone(locate_dsh_session_record("session-missing", home))


class TelemetryMappingTests(unittest.TestCase):
    def events(self):
        return [json.loads(raw) for raw in (MESSAGE, CALL, RESULT)]

    def test_maps_reasoning_text_calls_and_results(self):
        payloads = native_events_to_telemetry(self.events())
        self.assertEqual([p["hook_event"] for p in payloads],
                         ["AssistantReasoning", "AssistantMessage", "ToolCall", "PostToolUse"])
        call, result = payloads[2], payloads[3]
        self.assertEqual((call["tool_name"], call["tool_title"], call["tool_input"], call["call_id"]),
                         ("run_code", "Read x", "print(1)", "c1"))
        self.assertEqual((result["tool_name"], result["tool_ok"], result["tool_response"]),
                         ("run_code", False, "Traceback: boom"))
        self.assertEqual([p["native_seq"] for p in payloads], [1, 1, 2, 3])

    def test_after_seq_and_since_skip_recorded_events_but_keep_tool_names(self):
        self.assertEqual([p["hook_event"] for p in native_events_to_telemetry(self.events(), after_seq=2)],
                         ["PostToolUse"])
        self.assertEqual(native_events_to_telemetry(self.events(), after_seq=2)[0]["tool_name"], "run_code")
        self.assertEqual([p["native_seq"] for p in native_events_to_telemetry(self.events(), since_ms=1_000_002.5)],
                         [3])

    def test_tool_names_carry_over_to_results_read_later(self):
        message, call, result = self.events()
        names = {}
        native_events_to_telemetry([message, call], tool_names=names)
        later = native_events_to_telemetry([result], tool_names=names)
        self.assertEqual(later[0]["tool_name"], "run_code")
        self.assertEqual(names, {})  # a finished call is forgotten


class DisplayPromptTests(unittest.TestCase):
    def test_reply_instructions_for_the_parent_are_left_out(self):
        from session_manager import _dsh_display_prompt

        text = ("[approval request from the worker — paused]\nAllow this call? `pwsh` asks for access.\n"
                "Options: Allow once | Reject\nReply with exactly one option label.")
        self.assertEqual(_dsh_display_prompt(text), "Allow this call? `pwsh` asks for access.")


class HubDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from session_manager import AgentSession, HubSessionManager

        self.temp = tempfile.TemporaryDirectory(prefix="hub-dsh-record-")
        root = Path(self.temp.name)
        profiles = root / "profiles.json"
        profiles.write_text('{"profiles":{}}', encoding="utf-8")
        with patch.object(HubSessionManager, "_initial_discovery", lambda h: h._discovery_ready.set()):
            self.hub = HubSessionManager(profiles, base_dir=Path(__file__).parent, data_dir=root)
        await self.hub.start()
        session = AgentSession("dsh-x", "dsh", "DSH", "tui", "dsh", str(root), "unused", [])
        session.engine = "dsh"
        session.profile_snapshot = {"engine": "dsh"}
        session.last_task = "read x"
        session.status = "running"
        self.session = session
        self.hub.sessions[session.session_id] = session
        self.record = root / "session.v4.jsonl"

    async def asyncTearDown(self):
        await self.hub.archive_all_sessions_on_shutdown()
        self.temp.cleanup()

    async def test_drain_records_details_once_and_remembers_the_position(self):
        from session_manager import DSH_RECORD_SEQ_KEY, _DshRecordCursor

        self.record.write_bytes(MESSAGE + CALL)
        cursor = _DshRecordCursor(DshSessionRecordReader(self.record), after_seq=-1, since_ms=None)
        await self.hub._drain_dsh_session_record(self.session, cursor)
        with self.record.open("ab") as stream:
            stream.write(RESULT)
        await self.hub._drain_dsh_session_record(self.session, cursor)
        await self.hub._drain_dsh_session_record(self.session, cursor)

        self.assertEqual(self.session.conversation_ref[DSH_RECORD_SEQ_KEY], 3)
        feed = await self.hub.get_transcript_view("dsh-x")
        tools = [entry["tool"] for entry in feed["entries"] if entry["kind"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual((tools[0]["title"], tools[0]["input"], tools[0]["output"], tools[0]["status"]),
                         ("Read x", "print(1)", "Traceback: boom", "error"))
        self.assertEqual([entry["kind"] for entry in feed["entries"] if entry["kind"] in ("thinking", "assistant")],
                         ["thinking", "assistant"])

    async def test_progress_is_kept_per_payload_with_a_resume_offset(self):
        from session_manager import DSH_RECORD_OFFSET_KEY, _DshRecordCursor

        self.record.write_bytes(MESSAGE + CALL)
        cursor = _DshRecordCursor(DshSessionRecordReader(self.record), after_seq=-1, since_ms=None)
        recorded = []

        async def stop_after_first(session_id, payload):
            recorded.append(payload["hook_event"])
            self.session.terminated_by_user = len(recorded) >= 1

        with patch.object(self.hub, "record_telemetry", side_effect=stop_after_first):
            await self.hub._drain_dsh_session_record(self.session, cursor)
        self.assertEqual(recorded, ["AssistantReasoning"])
        self.assertEqual(self.session.conversation_ref["dsh_record_seq"], 1)
        self.assertEqual(self.session.conversation_ref[DSH_RECORD_OFFSET_KEY], len(MESSAGE + CALL))
        self.session.terminated_by_user = False

    async def test_reattach_without_a_stored_position_skips_what_the_log_recorded(self):
        self.record.write_bytes(MESSAGE + CALL)
        self.session.dsh_session_id = "session-abc"
        with patch("session_manager.locate_dsh_session_record", return_value=self.record), \
                patch("session_manager.httpx.AsyncClient", side_effect=RuntimeError("offline")):
            cursor = await self.hub._open_dsh_session_record(self.session, "job-1", reattach=True)
            fresh = await self.hub._open_dsh_session_record(self.session, "job-1")
        self.assertEqual(cursor.after_seq, 2)
        self.assertEqual(fresh.after_seq, -1)
        with self.record.open("ab") as stream:
            stream.write(RESULT)
        await self.hub._drain_dsh_session_record(self.session, cursor)
        feed = await self.hub.get_transcript_view("dsh-x")
        tool = next(entry["tool"] for entry in feed["entries"] if entry["kind"] == "tool")
        self.assertEqual((tool["name"], tool["status"]), ("run_code", "error"))
        self.assertFalse(any(entry["kind"] == "thinking" for entry in feed["entries"]))

    async def test_settle_reads_frames_written_after_the_job_ended(self):
        from session_manager import _DshRecordCursor

        self.record.write_bytes(MESSAGE + CALL)
        cursor = _DshRecordCursor(DshSessionRecordReader(self.record), after_seq=-1, since_ms=None)

        async def late_write():
            await asyncio.sleep(0.2)
            with self.record.open("ab") as stream:
                stream.write(RESULT)

        with patch("session_manager.DSH_RECORD_SETTLE_INTERVAL_SEC", 0.3):
            writer = asyncio.ensure_future(late_write())
            await self.hub._settle_dsh_session_record(self.session, cursor)
            await writer
        self.assertEqual(cursor.after_seq, 3)

    def test_long_tool_details_say_they_were_cut(self):
        from session_manager import TOOL_DETAIL_LIMIT, _clip_detail

        clipped = _clip_detail("x" * (TOOL_DETAIL_LIMIT + 10))
        self.assertTrue(clipped.startswith("x" * TOOL_DETAIL_LIMIT))
        self.assertIn(str(TOOL_DETAIL_LIMIT + 10), clipped)
        self.assertEqual(_clip_detail("short"), "short")

    async def test_unreadable_record_falls_back_to_the_job_log(self):
        from session_manager import _DshRecordCursor

        self.record.write_bytes(b"x")
        broken = DshSessionRecordReader(self.record)
        with patch.object(broken, "read_new_events", side_effect=RecordDecodeError("corrupt")):
            cursor = _DshRecordCursor(broken, after_seq=-1, since_ms=None)
            await self.hub._drain_dsh_session_record(self.session, cursor)
        self.assertFalse(cursor.active)


if __name__ == "__main__":
    unittest.main()
