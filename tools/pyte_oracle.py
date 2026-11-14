"""The pyte oracle: the hub's terminal state model as step 3a left it, offline only.

Decision `oracle-to-tools`: after step 3b the hub's terminal is the Rust core
(``hub_terminal_core``), so the pyte screen, the W8/W9/OSC strips and the raw ring that used to live
in ``AgentSession`` moved here instead of disappearing.  ``tools/render_terminal_fixtures.py``,
``tools/record_terminal_corpus.py``'s comparison and ``test_terminal_live_consumers.py`` render the
frozen corpus through this module, so the goldens step 2's Rust tests compare against are still the
behaviour the hub had before the swap.  ``pyte`` leaves ``requirements`` at step 6 with this file.

Nothing in the hub imports this module: ``session_manager.py`` and ``server.py`` reach the terminal
only through ``Core`` (design §5.2), which a test in ``test_terminal_no_pyte.py`` checks in a fresh
interpreter.

What moved here, and from where:

- ``HubTerminalScreen``/``HubTerminalStream``/``_split_marker_csi`` (``session_manager.py``,
  step 3a) - the pyte subclasses that keep one bad sequence from costing the rest of a chunk;
- ``trim_history_text`` and its two constants - W9's read-side cut, now the core's rule;
- ``OSC_COLOR_QUERY_RE`` - the colour-query strip the core applies before append and before replay;
- the ring producer (W8 strip, per-chunk OSC strip, append, cap + slack trigger) and the seam reads,
  as ``PyteOracleSession``, the object the fixture renderer feeds.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import pyte

from session_manager import (
    HISTORY_TRIM_SLACK_BYTES,
    RESUME_SCROLLBACK_MAX_BYTES,
    SUBMIT_DIAGNOSTIC_TAIL_CHARS,
    RawRead,
    ScreenRead,
    ScrollbackRead,
)

# W9: the grey notice a trimmed history starts with, and the longest line the trim treats as the
# newest resume banner. Both are the core's constants too; only the renderer reads them here.
HISTORY_TRUNCATED_PREFIX = "\x1b[90m[较早的输出已截断]\x1b[0m\r\n"
HISTORY_BANNER_LINE_MAX_BYTES = 128
BANNER_MARKER = "\x1b[90m── 已恢复（"

# OSC 10/11/12/4 colour queries a TUI emits at startup. The core strips them before the append and
# before the replay; this is the pattern the goldens were rendered with.
OSC_COLOR_QUERY_RE = re.compile(r"\x1b\](?:4;\d+|1[0-9]);\?(?:\x07|\x1b\\)")


def trim_history_text(text: str, max_bytes: int) -> str:
    """W9: cap a raw terminal history at max_bytes without cutting a line or the newest banner.

    The old buffer dropped whole PTY chunks, so the archived scrollback and every resume banner
    left in one step. This cuts bytes from the front, moves to the next line break and prefixes a
    grey notice. The newest "── 已恢复（第 N 次）──" line is kept whole, with its leading line
    break, whenever the cut falls before its end: a cut inside the banner line keeps the banner and
    everything after it, so the result may exceed max_bytes by less than that line
    (HISTORY_BANNER_LINE_MAX_BYTES). A banner that ends before the cut is older than the budget and
    goes with the rest.
    """
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text
    prefix = HISTORY_TRUNCATED_PREFIX
    budget = max_bytes - len(prefix.encode("utf-8"))
    if budget <= 0:
        return prefix
    cut = len(raw) - budget
    start = cut
    next_break = raw.find(b"\n", cut)
    if next_break >= 0:
        start = next_break + 1
    idx = text.rfind(BANNER_MARKER)
    if idx >= 0:
        marker_start = len(text[:idx].encode("utf-8", "replace"))
        banner_start = marker_start
        if banner_start >= 2 and text[:idx].endswith("\r\n"):
            banner_start -= 2
        line_break = raw.find(b"\n", marker_start)
        banner_end = line_break + 1 if line_break >= 0 else len(raw)
        if (banner_start < start and cut < banner_end
                and banner_end - banner_start <= HISTORY_BANNER_LINE_MAX_BYTES):
            start = banner_start
    body = raw[start:].decode("utf-8", "replace")
    return prefix + body


class HubTerminalScreen(pyte.HistoryScreen):
    """K4 (fix 9): the hub's screen model of a PTY.

    kimi sends a private status query (CSI ? 996 n). pyte dispatches every private CSI with
    private=True, but its report_device_status takes no such flag, so the query raised a TypeError
    in the middle of the PTY chunk. A private DSR asks the terminal something and draws nothing, so
    the whole CSI ? … n family is ignored here.
    """

    def report_device_status(self, mode: int, **kwargs: Any) -> None:
        if kwargs.get("private"):
            return
        super().report_device_status(mode)

    def draw(self, data: str) -> None:
        """Skips a zero-width or unprintable character instead of dropping the rest of the text.

        pyte's draw stops at the first character that is zero-width and not combining (ZWSP, ZWJ, the
        emoji selector VS16 in "⚠️") or unprintable, so everything after it in the same run of plain
        text never reached the screen. Here pyte draws the runs around such a character; the character
        itself changes nothing, as in a real terminal (it does not even trigger a pending wrap).
        """
        if data.isascii() and data.isprintable():
            super().draw(data)
            return
        width_of = pyte.screens.wcwidth
        run_start = 0
        for index, char in enumerate(data):
            width = width_of(char)
            if width > 0 or (width == 0 and unicodedata.combining(char)):
                continue
            if index > run_start:
                super().draw(data[run_start:index])
            run_start = index + 1
        if run_start < len(data):
            super().draw(data[run_start:])

    @property
    def display(self) -> List[str]:
        """Safe display renderer that tolerates orphaned wide-char stubs (Char.data == '').

        When a TUI cursor-positions over the first cell of a 2-column CJK character without writing
        the second cell, pyte leaves Char(data='') in the second cell while is_wide_char is False.
        Stock pyte.Screen.display indexes char[0] unconditionally and raises IndexError.
        """
        wcwidth = pyte.screens.wcwidth

        def render(line):
            is_wide_char = False
            for x in range(self.columns):
                if is_wide_char:
                    is_wide_char = False
                    continue
                char = line[x].data
                if not char:
                    yield " "
                    continue
                is_wide_char = wcwidth(char[0]) == 2
                yield char

        return ["".join(render(self.buffer[y])) for y in range(self.lines)]


class HubTerminalStream(pyte.Stream):
    """K4 (fix 9): one escape sequence pyte cannot handle costs only itself, not the rest of the chunk.

    pyte resets its parser when a handler raises but re-raises, and Stream.feed then abandons every
    byte after it (the raw scrollback kept them, the screen model did not). Here the failed
    character is dropped and the next one is parsed by the fresh parser.

    Also drops the CSIs pyte ends early (``_split_marker_csi``): a kitty-keyboard marker such as
    kimi's ``ESC[<u`` used to reach the screen as the text ``u``.
    """

    def __init__(self, screen: Optional[pyte.Screen] = None, strict: bool = True) -> None:
        super().__init__(screen, strict)
        self._marker_csi_carry = ""

    def feed(self, data: str) -> None:
        if not self._marker_csi_carry and "\x1b" not in data and "\x9b" not in data:
            # No escape byte and nothing held over: the filter could not change this text, and the
            # per-call cost matters because virtual sessions feed one character at a time.
            super().feed(data)
            return
        # A PTY chunk can split any sequence; the kept tail goes in front of the next chunk, so the
        # filter always sees whole sequences.
        text = self._marker_csi_carry + data
        self._marker_csi_carry = ""
        filtered, self._marker_csi_carry = _split_marker_csi(text)
        if filtered:
            super().feed(filtered)

    def _send_to_parser(self, data: str) -> Optional[bool]:
        try:
            return super()._send_to_parser(data)
        except Exception:
            # pyte's own handler already re-initialised the parser; report its plain-text state.
            return self._taking_plain_text


# pyte 0.8.2 ends a CSI at the first byte that is not a digit, ';', '?', a space or '>', and
# dispatches that byte as the final character. ECMA-48, and the vte parser of the Rust core, read
# 0x30-0x3F as parameter bytes instead. The three bytes where the two readings differ are the ones
# the CLIs send: ':' (SGR subparameters) and the '<' '=' private markers of the kitty keyboard
# protocol (kimi and omp send ESC[<u, agy ESC[=5;1u and ESC[<1u).
_CSI_MARKER_CHARS = ":<="
# pyte's two CSI starts: ESC [ and the 8-bit CSI, which pyte accepts as one.
_CSI_START_RE = re.compile("\x1b\\[|\x9b")
# A real CSI is a few dozen bytes. A longer unterminated run can only be junk, and pyte holds such a
# run in its own parser anyway, so the filter stops carrying it rather than buffering without bound.
_CSI_CARRY_MAX = 512


def _split_marker_csi(text: str) -> Tuple[str, str]:
    """Splits PTY text into ``(what pyte must parse, a trailing partial CSI for the next chunk)``.

    A CSI whose parameter bytes include ':' '<' or '=' is removed whole: vte reads the same bytes as
    one sequence and draws nothing for it, while pyte would draw the rest of the sequence as text.
    Every sequence pyte parses today is handed over unchanged - '?' private modes, '>' secondary
    parameters, '$', and CAN/SUB cancelling a sequence (the CAN/SUB itself stays in the text, so
    pyte's own cancel path still runs).

    Three more bytes inside a CSI are read the way vte and a terminal read them. pyte took each of
    them for the final byte and drew the rest of the sequence as text:
    - an ESC abandons the sequence and starts the next one, so the partial CSI is dropped and the
      ESC is handed on as the start of a new sequence;
    - a C0 control runs where it arrives and the sequence goes on, so it is moved in front of the
      sequence (pyte runs only BEL, BS, HT, LF, VT, FF and CR in place);
    - DEL is ignored, so it is removed.
    """
    kept: List[str] = []
    pos = 0
    length = len(text)

    def without_controls(start: int, head_end: int, end: int) -> str:
        """``text[start:end]``, a CSI whose start ends at ``head_end``, without its C0 controls and DELs."""
        body = text[head_end:end]
        if all(" " <= char != "\x7f" for char in body):
            return text[start:end]
        return text[start:head_end] + "".join(char for char in body if " " <= char != "\x7f")

    while True:
        match = _CSI_START_RE.search(text, pos)
        if match is None:
            tail = text[pos:]
            if tail.endswith("\x1b"):
                # An ESC at the very end: the next chunk decides what it starts, and pyte holds it
                # in its own parser meanwhile, so nothing is drawn either way.
                kept.append(tail[:-1])
                return "".join(kept), "\x1b"
            kept.append(tail)
            return "".join(kept), ""
        start = match.start()
        kept.append(text[pos:start])
        head_end = i = match.end()
        marker = False
        controls: List[str] = []  # C0 controls inside the sequence, in arrival order
        final_end = -1  # just past the final byte: where pyte itself ends the sequence
        cancel_at = -1  # the CAN/SUB that cancels the sequence
        restart_at = -1  # an ESC inside the sequence: it abandons the sequence and starts the next
        while i < length:
            char = text[i]
            if "@" <= char <= "~":      # final byte: the CSI is complete
                final_end = i + 1
                break
            if " " <= char <= "?":      # parameter and intermediate bytes
                if char in _CSI_MARKER_CHARS:
                    marker = True
                i += 1
            elif char == "\x7f":
                i += 1                  # DEL is ignored inside a sequence
            elif char in "\x18\x1a":
                cancel_at = i
                break
            elif char == "\x1b":
                restart_at = i
                break
            elif char < " ":
                controls.append(char)   # runs now; the sequence goes on
                i += 1
            else:
                break                   # not a byte ECMA-48 allows inside a CSI
        # The controls run before the sequence is dispatched, so they go in front of it.
        kept.extend(controls)
        if restart_at >= 0:
            pos = restart_at            # the partial sequence draws nothing; the ESC starts the next
            continue
        if final_end < 0 and cancel_at < 0:
            if i == length:
                tail = without_controls(start, head_end, length)
                if len(text) - start <= _CSI_CARRY_MAX:
                    return "".join(kept), tail
                kept.append(tail)       # runaway sequence: pyte holds it in its own parser
                return "".join(kept), ""
            # A byte pyte reads in its own way; keep the bytes and look for the next start behind it.
            kept.append(without_controls(start, head_end, i))
            pos = i
            continue
        end = final_end if final_end >= 0 else cancel_at
        if marker:
            pos = end                   # consumed whole and drawing nothing, as vte does
        else:
            kept.append(without_controls(start, head_end, end))
            pos = end


class PyteOracleSession:
    """One terminal as the hub's pyte path rendered it: a screen, a raw ring and the seam's reads.

    The renderer feeds it chunk by chunk (``feed_output``), the way ``AgentSession._on_pty_output``
    was fed before step 3b, and reads the same seam shapes the hub's callers read.
    """

    def __init__(self, engine: str = "generic", cols: int = 120, rows: int = 36,
                 resume_count: int = 0) -> None:
        self.engine = engine
        self.cols = int(cols)
        self.rows = int(rows)
        self.resume_count = int(resume_count)
        self.screen = HubTerminalScreen(self.cols, self.rows, history=3000)
        self.stream = HubTerminalStream(self.screen)
        self.max_history_bytes = RESUME_SCROLLBACK_MAX_BYTES
        self.raw_history: List[str] = []
        self.raw_history_bytes = 0
        self.raw_history_rev = 0
        # W8: a resumed session's ESC[3J, kept across a sequence split over two chunks.
        self._esc3j_carry = ""

    # ---- the producer, as `AgentSession._on_pty_output` was -------------------------------

    def feed_output(self, data: str) -> None:
        if self.resume_count:
            data = self.strip_saved_lines_erase(data)
        if "\x1b]" in data:
            data = OSC_COLOR_QUERY_RE.sub("", data)
        try:
            self.stream.feed(data)
        except Exception:
            pass
        self.raw_history.append(data)
        self.raw_history_bytes += len(data.encode("utf-8", "replace"))
        self.raw_history_rev += 1
        if self.raw_history_bytes > self.max_history_bytes + HISTORY_TRIM_SLACK_BYTES:
            joined = "".join(self.raw_history)
            trimmed = trim_history_text(joined, self.max_history_bytes)
            if trimmed != joined:
                self.raw_history = [trimmed]
                self.raw_history_bytes = len(trimmed.encode("utf-8", "replace"))

    def strip_saved_lines_erase(self, data: str) -> str:
        """W8: removes ESC[3J (erase saved lines) from a resumed session's output.

        The sequence can be split across two PTY reads, so up to three trailing bytes are carried
        into the next call. ESC[2J (erase display) is deliberately left alone: it only clears the
        viewport, and the bundled xterm.js 5.5.0 has no scrollOnEraseInDisplay option.
        """
        if not data:
            return data
        buf = self._esc3j_carry + data
        self._esc3j_carry = ""
        out = buf.replace("\x1b[3J", "")
        for size in (3, 2, 1):
            suffix = "\x1b[3J"[:size]
            if out.endswith(suffix):
                self._esc3j_carry = suffix
                out = out[:-size]
                break
        return out

    def resize(self, cols: int, rows: int) -> Tuple[int, int]:
        """`resize`: the 20/5 clamp, then pyte's own top-left-anchored resize."""
        self.cols = max(20, int(cols))
        self.rows = max(5, int(rows))
        try:
            self.screen.resize(self.rows, self.cols)
        except Exception:
            pass
        return self.cols, self.rows

    # ---- the seam's reads, as `AgentSession` returned them --------------------------------

    def read_screen(self, max_lines: int = 80) -> ScreenRead:
        try:
            lines = [line.rstrip() for line in self.screen.display]
        except Exception:
            lines = []
        while lines and not lines[-1]:
            lines.pop()
        if max_lines and len(lines) > max_lines:
            lines = lines[-max_lines:]
        try:
            cursor_x, cursor_y = int(self.screen.cursor.x), int(self.screen.cursor.y)
        except Exception:
            cursor_x, cursor_y = 0, 0
        return ScreenRead("\n".join(lines), cursor_x, cursor_y,
                          int(self.screen.columns), int(self.screen.lines), self.raw_history_rev)

    def read_scrollback(self, max_lines: int = 300) -> ScrollbackRead:
        try:
            curr_lines = [line.rstrip() for line in self.screen.display]
        except Exception:
            curr_lines = []
        all_lines = curr_lines
        while all_lines and not all_lines[-1]:
            all_lines.pop()
        newest_history = []
        try:
            for row in reversed(self.screen.history.top):
                if max_lines and len(newest_history) + len(all_lines) >= max_lines:
                    break
                text = "".join(row[col].data if col in row else " " for col in range(self.cols)).rstrip()
                if text or all_lines or newest_history:
                    newest_history.append(text)
        except Exception:
            pass
        all_lines = list(reversed(newest_history)) + all_lines
        if max_lines:
            all_lines = all_lines[-max_lines:]
        return ScrollbackRead("\n".join(all_lines), self.raw_history_rev)

    def read_raw(self) -> RawRead:
        return RawRead("".join(self.raw_history), self.raw_history_bytes, self.raw_history_rev)

    def read_raw_tail(self, max_chars: int = SUBMIT_DIAGNOSTIC_TAIL_CHARS) -> str:
        text = "".join(self.raw_history)
        if max_chars <= 0:
            return ""
        return text[-max_chars:]

    @property
    def raw_revision(self) -> int:
        return self.raw_history_rev

    def seed_history(self, text: str) -> None:
        """`spawn.seed_history`: one ring entry, rev 1, the ESC[3J carry cleared."""
        self.raw_history = [text]
        self.raw_history_bytes = len(text.encode("utf-8", "replace"))
        self.raw_history_rev = 1
        self._esc3j_carry = ""

    def replay_frame(self) -> str:
        """The replay the console's socket sends on connect (§6.4).

        The joined ring with one more colour-query strip; the core renders it inside `attach_sink`,
        and this oracle is the offline golden's copy of that same computation.
        """
        replay = "".join(self.raw_history)
        if "\x1b]" in replay:
            replay = OSC_COLOR_QUERY_RE.sub("", replay)
        return replay
