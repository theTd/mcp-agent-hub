"""Read DeepSeek Harness (DSH) native session records for the Hub transcript.

The dsh-crew job log (`~/.config/dsh-crew/logs/<job>.log`) only names each tool (`run_code`),
clips assistant text to 300 characters and has no reasoning.  The DSH host writes the complete
session to `<DSH home>/sessions/<workspace>/<session id>/session.v<N>.jsonl.zstd`: every tool
call with its arguments, every tool result, reasoning and assistant text.  This module finds that
record, walks its complete zstd frames, decodes them and maps the native events onto the Hub's
engine-neutral telemetry payloads (`HubSessionManager.record_telemetry`).

DSH appends one zstd frame per write, so the reader keeps a byte offset and only decodes frames
that are complete on disk.  Python gets a zstd decoder in the standard library from 3.14
(`compression.zstd`); older runtimes decode through Node.  DSH itself requires Node >= 24.9, whose
`node:zlib` has zstd built in, so Node is present wherever a DSH record exists.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

ZSTD_MAGIC = 0xFD2FB528
SKIPPABLE_MAGIC_MASK = 0xFFFFFFF0
SKIPPABLE_MAGIC = 0x184D2A50
# A memory bound per tool body, output or reasoning block while payloads are handed over; the Hub
# clips again (with a note) for the console feed.  The native record keeps the full text.
DETAIL_LIMIT = 200_000
NODE_DECODE_TIMEOUT_SEC = 20.0

_NODE_DECODER = r"""
const { zstdDecompressSync } = require('node:zlib');
const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', () => {
  const buf = Buffer.concat(chunks);
  const out = [];
  let p = 0;
  while (p + 4 <= buf.length) {
    const n = buf.readUInt32LE(p);
    p += 4;
    out.push(zstdDecompressSync(buf.subarray(p, p + n)));
    p += n;
  }
  process.stdout.write(Buffer.concat(out));
});
"""


class RecordDecodeError(RuntimeError):
    """The record exists but its frames could not be decoded (corrupt data or no decoder)."""


def dsh_home() -> Path:
    """DSH's data root: `DSH_HOME` when set (DSH honours it too), else `~/.dsh`."""
    raw = os.environ.get("DSH_HOME", "").strip()
    return Path(os.path.expanduser(raw)) if raw else Path(os.path.expanduser("~")) / ".dsh"


def locate_dsh_session_record(dsh_session_id: str, home: Optional[Path] = None) -> Optional[Path]:
    """The newest-format record file of one DSH session, or None while it does not exist yet."""
    sid = str(dsh_session_id or "").strip()
    # The id becomes part of a glob; refuse anything that could widen or escape it.
    if not sid or any(ch in sid for ch in "*?[]/\\") or ".." in sid:
        return None
    sessions = (home or dsh_home()) / "sessions"

    def version(path: Path) -> int:
        try:
            return int(path.name.split(".")[1].lstrip("v"))
        except (IndexError, ValueError):
            return -1

    try:
        candidates = [(version(p), p.stat().st_mtime, p)
                      for p in sessions.glob(f"*/{sid}/session.v*.jsonl*") if p.is_file()]
    except OSError:  # a file vanished between the glob and stat, or the folder is unreadable
        return None
    return max(candidates, key=lambda item: item[:2])[2] if candidates else None


def zstd_frame_end(buf: bytes, pos: int) -> Optional[int]:
    """End offset of the zstd (or skippable) frame at `pos`; None when it is not complete yet.

    Walks the frame header and block headers (RFC 8878 section 3.1) without decompressing, so a
    frame that DSH is still appending is never handed to the decoder.
    """
    size = len(buf)
    if size - pos < 4:
        return None
    (magic,) = struct.unpack_from("<I", buf, pos)
    if magic & SKIPPABLE_MAGIC_MASK == SKIPPABLE_MAGIC:
        if size - pos < 8:
            return None
        (length,) = struct.unpack_from("<I", buf, pos + 4)
        end = pos + 8 + length
        return end if end <= size else None
    if magic != ZSTD_MAGIC:
        raise RecordDecodeError(f"no zstd frame at byte {pos}")
    p = pos + 4
    if p >= size:
        return None
    descriptor = buf[p]
    p += 1
    if descriptor & 0x08:
        raise RecordDecodeError(f"reserved bit set in the frame header at byte {pos}")
    single_segment = (descriptor >> 5) & 1
    p += 0 if single_segment else 1  # window descriptor
    p += (0, 1, 2, 4)[descriptor & 0x03]  # dictionary id
    p += (1 if single_segment else 0, 2, 4, 8)[descriptor >> 6]  # frame content size
    while True:
        if p + 3 > size:
            return None
        header = buf[p] | (buf[p + 1] << 8) | (buf[p + 2] << 16)
        p += 3
        block_type = (header >> 1) & 0x03
        if block_type == 3:
            raise RecordDecodeError(f"reserved block type in the frame at byte {pos}")
        p += 1 if block_type == 1 else header >> 3  # RLE blocks store one byte
        if p > size:
            return None
        if header & 1:
            break
    if (descriptor >> 2) & 1:
        p += 4  # content checksum
    return p if p <= size else None


def _stdlib_decoder() -> Optional[Callable[[Sequence[bytes]], bytes]]:
    try:
        from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+
    except ImportError:
        return None
    def decode(frames: Sequence[bytes]) -> bytes:
        try:
            return b"".join(zstd.decompress(frame) for frame in frames)
        except Exception as error:  # ZstdError is not an OSError
            raise RecordDecodeError(f"zstd frame could not be decoded: {error}") from error

    return decode


def _node_decoder() -> Optional[Callable[[Sequence[bytes]], bytes]]:
    node = shutil.which("node")
    if not node:
        return None

    def decode(frames: Sequence[bytes]) -> bytes:
        payload = b"".join(struct.pack("<I", len(frame)) + frame for frame in frames)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            done = subprocess.run(
                [node, "-e", _NODE_DECODER],
                input=payload,
                capture_output=True,
                timeout=NODE_DECODE_TIMEOUT_SEC,
                creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RecordDecodeError(f"node zstd decoder failed: {error}") from error
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown error"]
            raise RecordDecodeError(f"node zstd decoder failed: {detail[0][:200]}")
        return done.stdout

    return decode


def default_frame_decoder() -> Optional[Callable[[Sequence[bytes]], bytes]]:
    """The in-process decoder when the runtime has one, else Node; None when neither exists."""
    return _stdlib_decoder() or _node_decoder()


class DshSessionRecordReader:
    """Incrementally read the JSON events appended to one DSH session record."""

    def __init__(self, path: Path, *, decoder: Optional[Callable[[Sequence[bytes]], bytes]] = None,
                 start_offset: int = 0):
        """`start_offset` resumes at a `resume_offset` an earlier reader of the same file reported."""
        self.path = Path(path)
        self.compressed = self.path.name.endswith(".zstd")
        self._decoder = decoder
        self.offset = max(0, int(start_offset or 0))
        self._verify_start = self.offset > 0
        self._carry = b""  # a decoded line that a later frame completes
        self._clean_offset = self.offset

    @property
    def resume_offset(self) -> int:
        """A file position at a frame and line boundary: every event before it has been returned."""
        return self._clean_offset

    def _decode(self, frames: Sequence[bytes]) -> bytes:
        if self._decoder is None:
            self._decoder = default_frame_decoder()
            if self._decoder is None:
                raise RecordDecodeError("no zstd decoder: install Node >= 24 or run the Hub on Python 3.14")
        try:
            return self._decoder(frames)
        except RecordDecodeError:
            raise
        except Exception as error:
            raise RecordDecodeError(f"zstd frames could not be decoded: {error}") from error

    def _start_is_boundary(self, stream) -> bool:
        if self.compressed:
            stream.seek(self.offset)
            head = stream.read(4)
            if len(head) < 4:
                return False
            (magic,) = struct.unpack("<I", head)
            return magic == ZSTD_MAGIC or magic & SKIPPABLE_MAGIC_MASK == SKIPPABLE_MAGIC
        stream.seek(self.offset - 1)
        return stream.read(1) == b"\n"

    def read_new_events(self) -> List[Dict[str, Any]]:
        """Events written since the last call, in file order.  Raises RecordDecodeError."""
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                end = stream.tell()
                if self._verify_start and end > self.offset:
                    self._verify_start = False
                    if not self._start_is_boundary(stream):
                        # The stored position belongs to another file version: read it all again
                        # (the caller's sequence filter drops what it already recorded).
                        self.offset = self._clean_offset = 0
                if end < self.offset:
                    # Rewritten or truncated by the host: start over rather than read garbage.
                    self.offset = self._clean_offset = 0
                    self._carry = b""
                stream.seek(self.offset)
                chunk = stream.read(end - self.offset)
        except FileNotFoundError:
            return []
        if not chunk:
            return []
        if self.compressed:
            frames: List[bytes] = []
            pos = 0
            while True:
                frame_end = zstd_frame_end(chunk, pos)
                if frame_end is None:
                    break
                if chunk[pos:pos + 4] == struct.pack("<I", ZSTD_MAGIC):
                    frames.append(chunk[pos:frame_end])
                pos = frame_end
            if not frames:
                self.offset += pos
                if not self._carry:
                    self._clean_offset = self.offset
                return []
            data = self._decode(frames)
            self.offset += pos
        else:
            cut = chunk.rfind(b"\n") + 1
            data = chunk[:cut]
            self.offset += cut
        data = self._carry + data
        lines = data.split(b"\n")
        self._carry = lines.pop()  # empty when the data ended with a newline
        if not self._carry:
            self._clean_offset = self.offset
        events: List[Dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                logger.warning("Skipping a malformed DSH record line in %s", self.path)
                continue
            if isinstance(event, dict):
                events.append(event)
        return events


def _clip(text: str, limit: int = DETAIL_LIMIT) -> str:
    text = str(text or "").replace("\x00", "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（已截断，原文 {len(text)} 字符）"


def _text_parts(content: Any, *, kind: str = "text") -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == kind and part.get("text")
    ).strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tool_arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _result_call_id(data: Dict[str, Any]) -> str:
    message = _as_dict(data.get("message"))
    return str(message.get("toolCallId") or _as_dict(message.get("source")).get("callId") or "")


def native_events_to_telemetry(
    events: Sequence[Dict[str, Any]], *, after_seq: int = -1, since_ms: Optional[float] = None,
    tool_names: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Map DSH native events onto `record_telemetry` payloads, oldest first.

    `after_seq` skips events the Hub already recorded (a continued job reuses the session record);
    `since_ms` additionally skips events older than the job when no sequence was stored yet.
    `tool_names` (call id -> tool name) carries calls over to results read by a later call.
    Each payload carries `native_seq` so the caller can persist its progress.
    """
    payloads: List[Dict[str, Any]] = []
    tool_names = {} if tool_names is None else tool_names
    for event in events:
        try:
            seq = int(event.get("seq", ""))
        except (TypeError, ValueError):
            continue
        etype = str(event.get("type") or "")
        data = _as_dict(event.get("data"))
        if etype == "tool/call":
            # Remember names even for skipped calls: a later result still needs its tool name.
            tool_names[str(data.get("callId") or "")] = str(data.get("name") or "tool")
        if seq <= after_seq or (since_ms is not None and float(event.get("time") or 0) < since_ms):
            if etype == "tool/result":
                tool_names.pop(_result_call_id(data), None)
            continue
        timestamp = float(event.get("time") or 0) / 1000.0 or None
        if etype == "assistant/message":
            message = _as_dict(data.get("message"))
            content = message.get("content")
            reasoning = _text_parts(content, kind="reasoning")
            text = _text_parts(content, kind="text")
            if reasoning:
                payloads.append({
                    "source": "dsh_session_record", "hook_event": "AssistantReasoning",
                    "reasoning": _clip(reasoning), "native_seq": seq, "timestamp": timestamp,
                })
            if text:
                payloads.append({
                    "source": "dsh_session_record", "hook_event": "AssistantMessage",
                    "intermediate_messages": [text], "final_assistant_text": text,
                    "native_seq": seq, "timestamp": timestamp,
                })
        elif etype == "tool/call":
            name = str(data.get("name") or "tool")
            arguments = _tool_arguments(data.get("arguments"))
            title = ""
            if isinstance(arguments, dict):
                title = str(arguments.get("description") or arguments.get("title") or "").strip()
                body = arguments.get("code") if isinstance(arguments.get("code"), str) else None
                tool_input = body if body is not None else json.dumps(arguments, ensure_ascii=False, indent=2)
            else:
                tool_input = str(arguments or "")
            payloads.append({
                "source": "dsh_session_record", "hook_event": "ToolCall", "tool_name": name,
                "tool_input": _clip(tool_input), "tool_title": title[:300],
                "call_id": str(data.get("callId") or ""), "native_seq": seq, "timestamp": timestamp,
            })
        elif etype == "tool/result":
            message = _as_dict(data.get("message"))
            call_id = _result_call_id(data)
            output = _text_parts(message.get("content"))
            failed = bool(message.get("isError") or data.get("error"))
            if not output and data.get("error"):
                output = str(data.get("error"))
            payloads.append({
                "source": "dsh_session_record", "hook_event": "PostToolUse",
                "tool_name": tool_names.pop(call_id, "tool"), "tool_response": _clip(output),
                "call_id": call_id, "tool_ok": not failed, "native_seq": seq, "timestamp": timestamp,
            })
    return payloads
