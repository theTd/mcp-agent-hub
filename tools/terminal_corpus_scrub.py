"""Remove personal data from a recorded terminal corpus (design doc §9 step 0, brief §2).

A recording contains the Windows user name, profile paths, the machine name, the account an engine
prints in its banner and, for some engines, an organisation, an e-mail or a resume handle.  They are
replaced in the *joined* stream — a match can span two chunks — and the text is then re-split at the
original chunk boundaries, so escape sequences and the chunk count the fixture pins stay exactly as
recorded.

The golden files are rendered from the scrubbed bytes, so what is committed is what was rendered.
Values come from the environment (``USERNAME``, ``USERPROFILE``, ``COMPUTERNAME``) plus patterns for
e-mail addresses, API keys, account banner fields, UUID-shaped tokens and ``session_<hex>`` resume
handles; nothing real is written into the repository or into the tool.  ``--extra LABEL=TEXT`` adds
one case-specific literal (escaped, so a Windows path is safe to pass), ``--extra-regex LABEL=PATTERN``
adds one case-specific regular expression, and ``--extra-window LABEL=PATH`` masks a long path together
with every wrapped fragment of it: an engine that scrolls or wraps the task folder prints the path in
pieces, and each piece is a contiguous window of the whole.

``chunk_encoding`` selects how the chunk bytes are read.  ``text`` is the step-0 pywinpty corpus.
``utf8_bytes`` (base64 of the exact pipe bytes, what the system-ConPTY recorder writes) is decoded as
one UTF-8 stream — a chunk boundary may cut a character — scrubbed as text, and then re-split at the
same chunk boundaries in bytes, so a character split across two reads stays split and the byte count
only changes where a rule replaced something.

Every rule is reported by label and count only, never by value.  The report in ``meta.json`` keeps
cumulative totals (so a corpus scrubbed in two passes still says what both removed) plus this run's
own counts under ``last_run``; it never re-persists a previous pass's character counts.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

# One replacement: label used in the report, pattern, and the placeholder written instead.
Rule = Tuple[str, "re.Pattern[str]", str]

# The shortest wrapped fragment a window rule will mask; below this, a piece is too generic to match.
WINDOW_MIN_LEN = 8
# Every separator spelling a profile path appears with in a stream.
PERCENT_ENCODED = (("\\", "%5C"), ("/", "%2F"), (":", "%3A"))


def _escaped(value: str) -> str:
    return re.escape(value)


def build_rules(extra: Optional[List[Tuple[str, str]]] = None,
                extra_patterns: Optional[List[Tuple[str, str]]] = None) -> List[Rule]:
    """The scrub rules, most specific first.

    The path rules run before the bare-name rules so ``C:\\Users\\name`` never becomes
    ``C:\\Users\\<USER>\\...`` twice, the account rules come last so a banner field that repeats a
    path is already masked by the time they look, and a resume handle is masked whole before the
    bare UUID rule can split it into ``session_<UUID>``.
    """
    rules: List[Rule] = []
    user = (os.environ.get("USERNAME") or "").strip()
    profile = (os.environ.get("USERPROFILE") or "").strip()
    machine = (os.environ.get("COMPUTERNAME") or "").strip()

    def add(label: str, pattern: str, placeholder: str, flags: int = 0) -> None:
        rules.append((label, re.compile(pattern, flags), placeholder))

    if profile:
        # One rule per spelling; IGNORECASE covers the streams that upper-case a path segment.
        variants = {profile, profile.replace("\\", "/"), profile.replace("\\", "\\\\")}
        for old, new in PERCENT_ENCODED:
            variants.add(profile.replace(old, new))
        for index, value in enumerate(sorted(variants, key=len, reverse=True)):
            add(f"profile_path_{index}", _escaped(value), "<HOME>", re.IGNORECASE)
    if user:
        # The profile folder name is the user name; a path that carries it but not the exact
        # profile string (another drive, a short name) still gets masked.
        add("profile_folder", r"(?:[A-Za-z]:)?[\\/]+Users[\\/]+" + _escaped(user), "<HOME>",
            re.IGNORECASE)
        add("user_name", r"(?<![A-Za-z0-9_])" + _escaped(user) + r"(?![A-Za-z0-9_])", "<USER>",
            re.IGNORECASE)
    if machine:
        add("machine_name", r"(?<![A-Za-z0-9_\-])" + _escaped(machine) + r"(?![A-Za-z0-9_\-])",
            "<MACHINE>", re.IGNORECASE)
    # A drive-rooted path that is not the user's profile is still this machine's layout (conhost
    # writes the child's image path into the title: ``C:\Program Files\nodejs\node.exe``). The first
    # rule keeps the spaces inside a path segment ("Program Files"), the second covers the plain form;
    # both stop at characters a command line or a wrapped line puts after a path.
    add("absolute_path_spaces",
        r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\x00-\x1f]+[\\/])+[^\s\\/:*?\"<>|\x00-\x1f]*",
        "<PATH>", re.IGNORECASE)
    add("absolute_path", r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\\/:*?\"<>|\x00-\x1f]*", "<PATH>",
        re.IGNORECASE)
    add("email", r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+", "<EMAIL>")
    add("api_key", r"\b(?:sk|pk|api|ghp|glpat)[-_][A-Za-z0-9_\-]{16,}\b", "<KEY>")
    # A resume handle is account-scoped: ``kimi`` prints ``Session: session_<uuid>`` and some engines
    # print a conversation flag whose value is a bare UUID.  Mask the whole handle first.
    add("session_handle",
        r"session_[0-9A-Fa-f]{6,}(?:-[0-9A-Fa-f]{4,})*(?![0-9A-Fa-f])", "<UUID>")
    add("uuid",
        r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
        r"-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])", "<UUID>")
    # Account banner fields the engines print at startup: keep the label, drop the value.
    add("account_banner",
        r"(?im)^(?P<label>\s*(?:signed in as|logged in as|account|email|user|organisation|"
        r"organization|workspace owner|plan)\s*[:：]\s*)(?P<value>\S.*?)\s*$",
        r"\g<label><ACCOUNT>")
    add("signed_in_sentence",
        r"(?i)(signed in as|logged in as)\s+[^\r\n\x1b]+", r"\1 <ACCOUNT>")
    for label, text in extra or []:
        add(f"extra_{label}", _escaped(text), f"<{label.upper()}>")
    for label, pattern in extra_patterns or []:
        add(f"extra_{label}", pattern, f"<{label.upper()}>")
    return rules


def window_matches(text: str, needle: str, min_len: int = WINDOW_MIN_LEN) -> List[Tuple[int, int]]:
    """Spans of ``text`` that are contiguous windows (>= min_len chars) of ``needle``, greedily.

    A terminal that wraps or horizontally scrolls a long path prints it in pieces without any single
    escape or newline that a literal rule could key on (agy scrolls the task folder across its status
    line).  Every piece is a contiguous substring of the path, so masking the longest matching window
    at each position removes them all without needing to know where the renderer broke them.
    """
    spans: List[Tuple[int, int]] = []
    index = 0
    while index < len(text):
        limit = min(len(needle), len(text) - index)
        best = 0
        for length in range(limit, min_len - 1, -1):
            if text[index:index + length] in needle:
                best = length
                break
        if best:
            spans.append((index, index + best))
            index += best
        else:
            index += 1
    return spans


def find_spans(text: str, rules: List[Rule],
               windows: Optional[List[Tuple[str, str]]] = None
               ) -> Tuple[List[Tuple[int, int, str, str]], Dict[str, int]]:
    """Non-overlapping matches, earliest rule first (a later rule may not cut an earlier match)."""
    taken = bytearray(len(text))
    spans: List[Tuple[int, int, str, str]] = []
    counts: Dict[str, int] = {}
    # A window rule is a literal path whose wrapped fragments must go too; it runs before the regex
    # rules because it matches the longest piece at each position.
    for label, needle in windows or []:
        for start, end in window_matches(text, needle):
            if any(taken[start:end]):
                continue
            taken[start:end] = b"\x01" * (end - start)
            spans.append((start, end, f"<{label.upper()}>", f"window_{label}"))
            counts[f"window_{label}"] = counts.get(f"window_{label}", 0) + 1
    for label, pattern, placeholder in rules:
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            if end <= start or any(taken[start:end]):
                continue
            taken[start:end] = b"\x01" * (end - start)
            replacement = match.expand(placeholder) if "\\g<" in placeholder else placeholder
            spans.append((start, end, replacement, label))
            counts[label] = counts.get(label, 0) + 1
    spans.sort(key=lambda item: item[0])
    return spans, counts


def scrub_joined(text: str, rules: List[Rule],
                 windows: Optional[List[Tuple[str, str]]] = None) -> Tuple[str, Dict[str, int]]:
    spans, counts = find_spans(text, rules, windows)
    if not spans:
        return text, counts
    parts: List[str] = []
    cursor = 0
    for start, end, replacement, _label in spans:
        parts.append(text[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), counts


def map_boundaries(boundaries: List[int], spans: List[Tuple[int, int, str, str]]) -> List[int]:
    """Maps original chunk boundaries through the edits, keeping them ascending.

    A boundary that falls inside a replaced span moves to the span's start, so the placeholder is
    never cut in two; several boundaries inside one span collapse (an empty chunk is legal and keeps
    the chunk count).
    """
    mapped: List[int] = []
    previous = 0
    edit_index = 0
    delta = 0
    for boundary in boundaries:
        while edit_index < len(spans) and spans[edit_index][1] <= boundary:
            start, end, _repl, _label = spans[edit_index]
            delta += len(_repl) - (end - start)
            edit_index += 1
        value = boundary + delta
        if edit_index < len(spans):
            start, end, _repl, _label = spans[edit_index]
            if start < boundary < end:
                value = start + delta
        value = max(previous, value)
        mapped.append(value)
        previous = value
    return mapped


def scrub_values(value, rules: List[Rule], counts: Dict[str, int],
                 windows: Optional[List[Tuple[str, str]]] = None):
    """Applies the rules to every string inside a JSON document and counts what they replaced."""
    if isinstance(value, str):
        cleaned, found = scrub_joined(value, rules, windows)
        for label, count in found.items():
            counts[label] = counts.get(label, 0) + count
        return cleaned
    if isinstance(value, dict):
        return {key: scrub_values(item, rules, counts, windows) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_values(item, rules, counts, windows) for item in value]
    return value


def byte_offsets(text: str) -> List[int]:
    """UTF-8 byte offset of every character, plus the end: a chunk boundary can fall inside one."""
    offsets = [0]
    total = 0
    for character in text:
        total += len(character.encode("utf-8"))
        offsets.append(total)
    return offsets


def locate_offset(offsets: List[int], boundary: int) -> Tuple[int, int]:
    """``(character index, bytes of it before the boundary)`` for a UTF-8 byte offset."""
    index = bisect.bisect_left(offsets, boundary)
    if index < len(offsets) and offsets[index] == boundary:
        return index, 0
    index = max(0, index - 1)
    return index, boundary - offsets[index]


def inside_a_replacement(character: int, spans: List[Tuple[int, int, str, str]]) -> bool:
    """True when the character itself is replaced, so its byte prefix no longer exists."""
    return any(start <= character < end for start, end, _replacement, _label in spans)


def split_byte_chunks(parts: List[bytes], text: str, scrubbed: str,
                      spans: List[Tuple[int, int, str, str]]) -> List[bytes]:
    """Re-splits the scrubbed text at the original chunk boundaries, in bytes.

    A boundary that fell inside a multi-byte character stays inside the same character: the character
    survives an edit unless a rule replaced it, and then the boundary snaps to the character's start.
    The byte stream is the exact pipe bytes, so a split character must stay split (design §5.4).
    """
    original_offsets = byte_offsets(text)
    cleaned_offsets = byte_offsets(scrubbed)
    cleaned = scrubbed.encode("utf-8")
    new_offsets: List[int] = []
    boundary = 0
    for part in parts:
        boundary += len(part)
        character, prefix = locate_offset(original_offsets, boundary)
        mapped = map_boundaries([character], spans)[0]
        mapped = min(max(0, mapped), len(cleaned_offsets) - 1)
        offset = cleaned_offsets[mapped]
        if prefix and not inside_a_replacement(character, spans) and mapped + 1 < len(cleaned_offsets):
            offset = min(offset + prefix, cleaned_offsets[mapped + 1])
        if new_offsets:
            offset = max(offset, new_offsets[-1])
        new_offsets.append(min(offset, len(cleaned)))
    chunks: List[bytes] = []
    start = 0
    for offset in new_offsets:
        chunks.append(cleaned[start:offset])
        start = offset
    # Whatever follows the last original boundary belongs to the last chunk.
    chunks[-1] += cleaned[start:]
    return chunks


def scrub_case(case_dir: Path, extra: Optional[List[Tuple[str, str]]] = None,
               extra_patterns: Optional[List[Tuple[str, str]]] = None,
               windows: Optional[List[Tuple[str, str]]] = None) -> Dict[str, object]:
    rules = build_rules(extra, extra_patterns)
    chunks_path = case_dir / "chunks.jsonl"
    meta_path = case_dir / "meta.json"
    meta: Dict[str, object] = {}
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    encoding = str(meta.get("chunk_encoding") or "text")
    if encoding not in ("text", "utf8_bytes"):
        raise SystemExit(f"{case_dir}: unknown chunk_encoding {encoding!r}")

    records = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if encoding == "text":
        texts = [str(record["chunk"]) for record in records]
        joined = "".join(texts)
        spans, counts = find_spans(joined, rules, windows)
        scrubbed, _counts = scrub_joined(joined, rules, windows)

        bounds: List[int] = []
        offset = 0
        for text in texts:
            offset += len(text)
            bounds.append(offset)
        mapped = map_boundaries(bounds, spans)
        start = 0
        new_texts: List[str] = []
        for boundary in mapped:
            new_texts.append(scrubbed[start:boundary])
            start = boundary
        # Whatever follows the last original boundary belongs to the last chunk: the piece count must
        # stay exactly what was recorded.
        new_texts[-1] += scrubbed[start:]
        if len(new_texts) != len(texts):
            raise SystemExit(f"{case_dir}: chunk count changed during scrub")
        payloads: List[Union[str, bytes]] = list(new_texts)
    else:
        # Base64 of the exact pipe bytes. Decoded once as a stream — the chunks are one UTF-8 stream
        # whose boundaries may cut a character — scrubbed as text, then re-split at the same
        # boundaries, so a split character stays split and the byte count only changes where a rule
        # replaced something.
        parts = [base64.b64decode(str(record["chunk"])) for record in records]
        joined_bytes = b"".join(parts)
        try:
            joined = joined_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SystemExit(f"{case_dir}: chunk bytes are not valid UTF-8 ({exc}); the text rules "
                             "cannot run on them") from exc
        spans, counts = find_spans(joined, rules, windows)
        scrubbed, _counts = scrub_joined(joined, rules, windows)
        new_parts = split_byte_chunks(parts, joined, scrubbed, spans)
        if len(new_parts) != len(parts):
            raise SystemExit(f"{case_dir}: chunk count changed during scrub")
        payloads = list(new_parts)

    with open(chunks_path, "w", encoding="utf-8", newline="\n") as fh:
        for record, payload in zip(records, payloads):
            chunk = (payload if isinstance(payload, str)
                     else base64.b64encode(payload).decode("ascii"))
            fh.write(json.dumps({"delay_ms": record["delay_ms"], "chunk": chunk},
                                ensure_ascii=False) + "\n")

    # meta.json carries text too (the task, launch argv, notes): scrub its string values as well.
    # Cumulative totals say what this corpus has had removed over every pass; ``last_run`` says what
    # this pass found, so neither number is ever a mix of two runs.
    previous = meta.get("scrub") if isinstance(meta.get("scrub"), dict) else {}
    totals: Dict[str, int] = {str(rule.get("label")): int(rule.get("count") or 0)
                              for rule in previous.get("rules") or []}
    meta_counts: Dict[str, int] = {}
    meta = scrub_values(meta, rules, meta_counts, windows)
    this_run: Dict[str, int] = dict(counts)
    for label, count in meta_counts.items():
        this_run[label] = this_run.get(label, 0) + count
    for label, count in this_run.items():
        totals[label] = totals.get(label, 0) + count
    meta["scrub"] = {
        "rules": [{"label": label, "count": count} for label, count in sorted(totals.items())],
        "replacements": sum(totals.values()),
        "chunks": len(payloads),
        "chunk_encoding": encoding,
        "runs": int(previous.get("runs") or 0) + 1,
        "last_run": {
            "rules": [{"label": label, "count": count} for label, count in sorted(this_run.items())],
            "replacements": sum(this_run.values()),
            "chars_before": len(joined),
            "chars_after": len(scrubbed),
        },
    }
    if meta_path.is_file():
        with open(meta_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta["scrub"]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("case", type=Path, help="case directory holding chunks.jsonl and meta.json")
    parser.add_argument("--extra", action="append", default=[], metavar="LABEL=TEXT",
                        help="one case-specific literal (escaped, so a path needs no quoting)")
    parser.add_argument("--extra-regex", action="append", default=[], metavar="LABEL=PATTERN",
                        help="one case-specific regular expression")
    parser.add_argument("--extra-window", action="append", default=[], metavar="LABEL=PATH",
                        help="a long path whose wrapped fragments must go too (>= 8 contiguous chars)")
    args = parser.parse_args(argv)

    def parse(items: List[str], flag: str) -> List[Tuple[str, str]]:
        pairs: List[Tuple[str, str]] = []
        for item in items:
            label, _, value = item.partition("=")
            if not label or not value:
                raise SystemExit(f"{flag} needs LABEL=VALUE, got {item!r}")
            pairs.append((label, value))
        return pairs

    report = scrub_case(args.case.resolve(), parse(args.extra, "--extra"),
                        parse(args.extra_regex, "--extra-regex"),
                        parse(args.extra_window, "--extra-window"))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())