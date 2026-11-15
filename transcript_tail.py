"""Bounded transcript reads with explicit acknowledgement of complete lines."""
import asyncio
from pathlib import Path


def read_complete_lines(path, offset, *, batch_lines=256):
    """Read UTF-8 JSONL without consuming an unfinished row or splitting a code point."""
    with Path(path).open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(offset if stream.tell() >= offset else 0)
        offset = stream.tell()
        lines = []
        for _ in range(batch_lines):
            line = stream.readline()
            if not line.endswith(b"\n"):
                break
            lines.append(line.decode("utf-8", "replace"))
            offset = stream.tell()
        return lines, offset


def read_complete_records(path, offset, *, batch_lines=256):
    """`read_complete_lines` with each row's (start, end) byte offsets.

    A reader that fails on one row can then resume at the row after it instead of losing the
    rest of the batch it had already read.
    """
    with Path(path).open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(offset if stream.tell() >= offset else 0)
        cursor = stream.tell()
        records = []
        for _ in range(batch_lines):
            start = cursor
            line = stream.readline()
            if not line.endswith(b"\n"):
                break
            cursor = stream.tell()
            records.append((start, cursor, line.decode("utf-8", "replace")))
        return records, cursor


class TranscriptTail:
    def __init__(self, path, *, batch_lines=64):
        self.path = Path(path)
        self.batch_lines = batch_lines
        self.offset = 0

    def _read_batch(self):
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(self.offset if stream.tell() >= self.offset else 0)
                lines = []
                for _ in range(self.batch_lines):
                    line = stream.readline()
                    if not line.endswith(b"\n"):
                        break
                    lines.append((stream.tell(), line.decode("utf-8", "replace")))
                return lines
        except FileNotFoundError:
            return []

    async def read_batch(self):
        return await asyncio.to_thread(self._read_batch)

    def acknowledge(self, offset):
        self.offset = offset
