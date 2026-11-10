"""Bounded transcript reads with explicit acknowledgement of complete lines."""
import asyncio
from pathlib import Path


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
