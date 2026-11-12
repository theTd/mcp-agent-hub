"""Guards against the event-loop stalls measured in the live hub on 2026-11-12.

Run: python -m unittest -v test_event_loop_stalls
"""
import ast
import importlib
import logging
import sqlite3
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dsh_endpoint
from hub_database import HubDatabase

HUB_DIR = Path(__file__).resolve().parent


def _httpx_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "httpx"
        and node.func.attr in {"request", "get", "post", "put", "delete", "stream", "Client", "AsyncClient"}
    ]


class DshTlsContextTests(unittest.TestCase):
    def test_one_context_is_built_and_shared(self):
        with patch.object(dsh_endpoint, "_tls_context", None), \
                patch.object(dsh_endpoint.httpx, "create_ssl_context",
                             wraps=dsh_endpoint.httpx.create_ssl_context) as build:
            first = dsh_endpoint.dsh_tls_context()
            second = dsh_endpoint.dsh_tls_context()
        self.assertIsInstance(first, ssl.SSLContext)
        self.assertIs(first, second)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(first.verify_mode, ssl.CERT_REQUIRED)

    def test_every_hub_call_to_dsh_reuses_the_shared_context(self):
        # A client built without it loads the whole CA bundle (~60 ms, mostly holding the GIL).
        calls = _httpx_calls(HUB_DIR / "session_manager.py")
        self.assertTrue(calls)
        missing = [
            call.lineno for call in calls
            if not any(
                keyword.arg == "verify" and isinstance(keyword.value, ast.Call)
                and getattr(keyword.value.func, "id", "") == "dsh_tls_context"
                for keyword in call.keywords
            )
        ]
        self.assertEqual(missing, [], "session_manager.py: httpx calls without verify=dsh_tls_context()")


class WorkerEventsIndexTests(unittest.TestCase):
    def test_session_reads_use_the_index(self):
        with tempfile.TemporaryDirectory(prefix="hub-index-", ignore_cleanup_errors=True) as root:
            path = Path(root) / "hub_state.db"
            HubDatabase(path)
            with sqlite3.connect(path) as conn:
                queries = (
                    "SELECT seq, content FROM worker_events WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                    "SELECT DISTINCT request_id FROM worker_events WHERE session_id=? "
                    "AND type IN ('approval_request', 'question') AND request_id IS NOT NULL AND request_id != ''",
                    "SELECT MAX(timestamp) FROM worker_events WHERE session_id=? AND type='turn_completed'",
                )
                for query in queries:
                    params = ("s", 80) if query.count("?") == 2 else ("s",)
                    plan = " ".join(row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + query, params))
                    with self.subTest(query=query):
                        self.assertIn("idx_worker_events_session", plan)
            conn.close()


class FastmcpLoggingTests(unittest.TestCase):
    def test_tool_errors_are_logged_without_rich_rendering(self):
        # A rich traceback (syntax-highlighted source) held the loop for 1.75 s per failed tool call.
        with patch("session_manager.HubSessionManager"):
            importlib.import_module("server")
        handlers = logging.getLogger("fastmcp").handlers
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertNotIn("rich", type(handler).__module__)
        try:
            raise RuntimeError("boom")
        except RuntimeError as error:
            record = logging.LogRecord("fastmcp", logging.ERROR, __file__, 1, "Error calling tool 'x'", None,
                                       (type(error), error, error.__traceback__))
        text = handlers[0].format(record)
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("RuntimeError: boom", text)


if __name__ == "__main__":
    unittest.main()
