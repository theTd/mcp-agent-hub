"""tools/hub_wait.py: loops the wait_url while waiting, exits 0 / 2 / 3."""
import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import hub_wait  # noqa: E402

from agent_guidance import HUB_WAIT_CLI, SERVER_INSTRUCTIONS  # noqa: E402

URL = "http://127.0.0.1:4444/api/wait/wh_abc"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScriptedOpener:
    """Answers each GET with the next scripted item: a dict, or an exception to raise."""

    def __init__(self, clock, script, seconds_per_poll=100.0):
        self.clock, self.script, self.step, self.urls = clock, list(script), seconds_per_poll, []

    def open(self, url, timeout):
        self.urls.append((url, timeout))
        self.clock.now += self.step
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, bytes):
            return io.BytesIO(item)
        return io.BytesIO(json.dumps(item).encode("utf-8"))


class HubWaitTests(unittest.TestCase):
    def run_script(self, script, max_seconds=1000.0, retry_seconds=120.0, step=100.0):
        clock = FakeClock()
        opener = ScriptedOpener(clock, script, step)
        code, payload = hub_wait.wait_until_settled(
            URL, max_seconds, retry_seconds, opener=opener, clock=clock, sleep=clock.sleep)
        return code, payload, opener

    def test_loops_while_waiting_and_returns_the_first_other_state(self):
        code, payload, opener = self.run_script(
            [{"state": "waiting"}, {"state": "waiting"}, {"state": "ask", "requests": [{"request_id": "r1"}]}])
        self.assertEqual(code, 0)
        self.assertEqual(payload["requests"][0]["request_id"], "r1")
        self.assertEqual(len(opener.urls), 3)
        self.assertTrue(opener.urls[0][0].startswith(URL + "?timeout="))

    def test_cap_exits_2_with_the_last_waiting_payload(self):
        code, payload, opener = self.run_script([{"state": "waiting", "n": i} for i in range(5)],
                                                max_seconds=250)
        self.assertEqual(code, 2)
        self.assertEqual(payload["state"], "waiting")
        self.assertLessEqual(opener.urls[-1][1], 300 + 30)
        # The last poll asks the hub to hold no longer than the time left.
        self.assertIn("timeout=50", opener.urls[-1][0])

    def test_http_error_exits_3_at_once(self):
        err = urllib.error.HTTPError(URL, 404, "Not Found", {}, io.BytesIO(b'{"detail":"unknown handle"}'))
        code, payload, _ = self.run_script([err])
        self.assertEqual(code, 3)
        self.assertEqual(payload["http_status"], 404)
        self.assertIn("unknown handle", payload["detail"])

    def test_refused_connection_is_retried_then_succeeds(self):
        refused = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        code, payload, _ = self.run_script([refused, refused, {"state": "done", "results": []}], step=1.0)
        self.assertEqual((code, payload["state"]), (0, "done"))

    def test_hub_unreachable_past_the_retry_window_exits_3(self):
        refused = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        code, payload, _ = self.run_script([refused] * 50, retry_seconds=20, step=1.0)
        self.assertEqual(code, 3)
        self.assertIn("unreachable", payload["detail"])

    def test_a_cut_or_garbled_answer_is_retried_not_a_crash(self):
        import http.client
        cut = http.client.IncompleteRead(b'{"state": "do')
        script = [cut, b"<html>proxy</html>", bytes([0xFF, 0xFE]), b"[1, 2]", {"state": "done", "results": []}]
        code, payload, _ = self.run_script(script, step=1.0)
        self.assertEqual((code, payload["state"]), (0, "done"))
        code, payload, _ = self.run_script([b"not json"] * 50, retry_seconds=20, step=1.0)
        self.assertEqual(code, 3)
        self.assertIn("unreachable", payload["detail"])

    def test_target_forms(self):
        self.assertEqual(hub_wait.resolve_wait_url(URL + "/?timeout=5", "x"), URL)
        self.assertEqual(hub_wait.resolve_wait_url("wh_abc", "http://127.0.0.1:4444/"), URL)
        with self.assertRaises(ValueError):
            hub_wait.resolve_wait_url("abc", "http://127.0.0.1:4444")

    def test_real_http_round_trip(self):
        answers = [{"state": "waiting"}, {"state": "failed", "results": [{"session_id": "s"}]}]
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = json.dumps(answers.pop(0)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/wait/wh_x"
            code, payload = hub_wait.wait_until_settled(url, 60, 5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((code, payload["state"]), (0, "failed"))
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0].startswith("/api/wait/wh_x?timeout="))

    def test_guide_names_the_shipped_cli(self):
        self.assertTrue(HUB_WAIT_CLI.is_file())
        self.assertIn(str(HUB_WAIT_CLI), SERVER_INSTRUCTIONS)
        payload = (Path(__file__).parent / "desktop/portable/payload.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("tools/hub_wait.py", payload)


if __name__ == "__main__":
    unittest.main()
