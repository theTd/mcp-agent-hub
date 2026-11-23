"""Classify an engine's own failure text into one parent-facing error kind.

Endpoint-agnostic on purpose: the callers are the transcript watchers and the screen poll, and
none of an engine's API objects belongs here.  The text is whatever the engine wrote (a Codex
``error.message``, a Claude ``API Error: 429 ...`` line, a Kimi ``error.message``, an omp
``errorMessage``), plus its optional status hints.

The order is the decision table, first match wins.  It is what makes a real record classify as
written in the spec: ``usage limit`` with status 429 is quota, while ``exceeded retry limit, last
status: 429 Too Many Requests`` is rate_limit (the word "exceeded" alone is not quota).
"""

from __future__ import annotations

import re
from typing import Any, Dict

ENGINE_ERROR_KINDS = ("auth", "quota", "rate_limit", "server", "network", "request", "other")
RETRYABLE_KINDS = frozenset({"rate_limit", "server", "network"})
MAX_ENGINE_ERROR_MESSAGE_CHARS = 500

_AUTH_WORDS = (
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid x-api-key",
    "not logged in",
    "/login",
)
_QUOTA_WORDS = (
    "insufficient_quota",
    "quota",
    "usage limit",
    "usage_limit",
    "billing",
    "credit balance",
    "out of credits",
    "payment required",
    "额度",
    "余额",
)
_RATE_LIMIT_WORDS = ("rate limit", "rate_limit", "too many requests")
_SERVER_WORDS = (
    "overloaded",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)
_NETWORK_WORDS = (
    "connection failed",
    "connection refused",
    "connection reset",
    "connection closed",
    "error sending request",
    "stream disconnected",
    "timed out",
    "timeout",
    "econnreset",
    "econnrefused",
    "enotfound",
    "eai_again",
    "socket hang up",
    "network",
    "dns",
    "apiconnectionerror",
)
_REQUEST_WORDS = (
    "payload too large",
    "request entity too large",
    "context length",
    "context window",
    "prompt is too long",
    "maximum context",
)
# An end-of-stream is a broken connection, not a rejected request (agy's real ERROR_MESSAGE ends
# "...: EOF"). As a whole word, so "geoff" is not a network failure.
_NETWORK_PATTERNS = (re.compile(r"(?<![a-z])eof(?![a-z])"),)
_HTML_START_RE = re.compile(r"<[a-zA-Z!/]")
_WHITESPACE_RE = re.compile(r"\s+")


def _has_number(text: str, number: str) -> bool:
    """A status number matches as a whole number: "1500 tokens" is not a 5xx status."""
    return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", text) is not None


def _has_server_status(text: str) -> bool:
    """Any 5xx, including 529; not the "500" inside "1500"."""
    return re.search(r"(?<!\d)5\d\d(?!\d)", text) is not None


def _clean_message(message: Any) -> str:
    """One line, no HTML body, at most MAX_ENGINE_ERROR_MESSAGE_CHARS characters.

    A gateway's 413 page arrives as ``unexpected status 413 Payload Too Large: <html>...`` — the
    parent needs the status line, not a page of markup, and the message must stay one line.
    """
    text = str(message or "")
    match = _HTML_START_RE.search(text)
    if match:
        text = text[: match.start()].rstrip(" :;,-")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:MAX_ENGINE_ERROR_MESSAGE_CHARS]


def classify_engine_error(
    message: Any, *, code: Any = None, http_status: Any = None
) -> Dict[str, Any]:
    """{"kind", "retryable", "message"} for one engine failure.

    `code` and `http_status` are hints the engine put beside the text (a codex_error_info string,
    a Kimi ``error.code``, a status code); they only take part in the same word/number matching.
    """
    cleaned = _clean_message(message)
    haystack = " ".join((cleaned, str(code or ""), str(http_status or ""))).lower()

    kind = "other"
    if (
        _has_number(haystack, "401")
        or _has_number(haystack, "403")
        or any(word in haystack for word in _AUTH_WORDS)
    ):
        kind = "auth"
    elif (
        _has_number(haystack, "402")
        or any(word in haystack for word in _QUOTA_WORDS)
    ):
        kind = "quota"
    elif (
        _has_number(haystack, "429")
        or any(word in haystack for word in _RATE_LIMIT_WORDS)
    ):
        kind = "rate_limit"
    elif _has_server_status(haystack) or any(word in haystack for word in _SERVER_WORDS):
        kind = "server"
    elif (
        haystack.strip() == "terminated"
        or any(word in haystack for word in _NETWORK_WORDS)
        or any(pattern.search(haystack) for pattern in _NETWORK_PATTERNS)
    ):
        kind = "network"
    elif (
        any(_has_number(haystack, number) for number in ("400", "404", "413", "422"))
        or any(word in haystack for word in _REQUEST_WORDS)
    ):
        kind = "request"

    return {"kind": kind, "retryable": kind in RETRYABLE_KINDS, "message": cleaned}


_ERROR_ADVICE = {
    "quota": "Retrying will not help until the account, key or quota is fixed; tell the user.",
    "auth": "Retrying will not help until the account, key or quota is fixed; tell the user.",
    "rate_limit": "Send a message to retry after a pause.",
    "server": "Send a message to retry after a pause.",
    "network": "Send a message to retry after a pause.",
    "request": "The request was rejected (too large or invalid); change or shrink it first.",
    "other": "Send a message to retry, or inspect the session.",
}


def engine_error_summary(engine_label: str, info: Dict[str, Any]) -> str:
    """The turn's final response: a marked engine error, never passed off as an answer."""
    kind = str(info.get("kind") or "other")
    if kind not in ENGINE_ERROR_KINDS:
        kind = "other"
    message = str(info.get("message") or "the engine reported an error without a message").strip()
    if not message.endswith((".", "!", "?")):
        message += "."
    advice = _ERROR_ADVICE.get(kind, _ERROR_ADVICE["other"])
    label = str(engine_label or "engine")
    return (
        f"{label} turn ended on an engine error ({kind}): {message} This is not an answer; "
        f"{label} is idle at its prompt. {advice}"
    )
