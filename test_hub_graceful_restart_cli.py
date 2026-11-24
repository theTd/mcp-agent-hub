"""tools/hub_graceful_restart.py against a stub hub: status, restart and replace-exe.

The stub is a real local HTTP server, so the CLI's own urllib path (proxy bypass included) is
exercised; most Windows-only bits (ShellExecuteW, --print-build-info) are injected stubs, while
`query_process_image` gets a real process of its own.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import hub_graceful_restart as cli  # noqa: E402

TOKEN = "stub-shutdown-token"


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FakeHub:
    """The hub's answers, plus everything the CLI sent it."""

    def __init__(self, port: int):
        self.port = port
        self.info = {"service": "mcp-agent-hub", "pid": 4242, "port": port,
                     "started_at": "2026-11-24T10:00:00+08:00", "shutdown_enabled": True}
        self.status = {"ok": True, "phase": "ready", "restart_id": None, "mode": None,
                       "requested_at": None, "started_at": self.info["started_at"],
                       "port": port, "pid": 4242, "target_count": 0, "resumed_count": 0,
                       "skipped_count": 0, "failed_count": 0, "result": {"sessions": {}}}
        self.sessions = [{"session_id": "codex-run", "engine": "codex", "status": "running",
                          "title": "restart me"},
                         {"session_id": "claude-idle", "engine": "claude", "status": "completed",
                          "title": "idle"}]
        self.requests = []
        self.plan_at_post = None
        self.posts = []
        self.on_post = None  # (path, body, headers) -> (status, answer)
        self.dead = False    # the hub accepted a stop and is gone: no route answers any more

    def default_post(self, path, body, headers):
        self.posts.append(body)
        self.info["started_at"] = "2026-11-24T10:01:00+08:00"
        self.status.update({"phase": "ready", "restart_id": body.get("restart_id"),
                            "mode": body.get("mode"), "target_count": 2, "resumed_count": 2,
                            "failed_count": 0, "skipped_count": 0,
                            "result": {"sessions": {"codex-run": {"ok": True},
                                                    "claude-idle": {"ok": True}}}})
        return 202, {"ok": True, "restart_id": body.get("restart_id"),
                     "mode": body.get("mode"), "session_count": 2}

    def get(self, path: str):
        if self.dead:
            return 503, {"detail": "hub is stopping"}
        if path == "/api/hub/info":
            return 200, self.info
        if path == "/health":
            return 200, {"ok": True, "status": "healthy"}
        if path == "/api/hub/restart-status":
            return 200, self.status
        if path == "/api/sessions":
            return 200, {"sessions": self.sessions}
        return 404, {"detail": "no such route"}


class HubServer:
    def __init__(self, fake: FakeHub):
        fake_holder = fake

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                fake_holder.requests.append(("GET", path, None))
                code, body = fake_holder.get(path)
                self._reply(code, body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw or b"{}")
                path = self.path.split("?", 1)[0]
                headers = {k.lower(): v for k, v in dict(self.headers).items()}
                fake_holder.requests.append(("POST", path, body))
                if fake_holder.on_post is not None:
                    code, answer = fake_holder.on_post(path, body, headers)
                else:
                    code, answer = fake_holder.default_post(path, body, headers)
                self._reply(code, answer)

            def _reply(self, code, body):
                payload = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _PostFailsOpener:
    """The real opener, except that a POST never gets an answer (a read timeout, a cut socket)."""

    def __init__(self, inner, error: BaseException):
        self.inner = inner
        self.error = error

    def open(self, request, timeout=None):
        if getattr(request, "method", "GET") == "POST":
            raise self.error
        return self.inner.open(request, timeout=timeout)


class _RootCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="restart-cli-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.fake = FakeHub(0)
        self.server = HubServer(self.fake)
        self.fake.info["port"] = self.server.port
        self.fake.status["port"] = self.server.port
        self.write_launch()

    def tearDown(self):
        self.server.close()
        self.temp.cleanup()

    def write_launch(self, **overrides):
        record = {
            "version": 1, "python": "C:\\hub\\python.exe", "repo_dir": "C:\\hub\\resources",
            "data_dir": str(self.data), "port": self.server.port, "pid": 73584,
            "creation_time": 134399000000000001, "app_pid": 11648,
            "app_creation_time": 134399000000000000, "state": "running",
            "shutdown_token": TOKEN,
        }
        record.update(overrides)
        (self.root / "launch.json").write_text(json.dumps(record), encoding="utf-8")

    def args(self, *argv):
        return cli.build_parser().parse_args([argv[0], "--portable-root", str(self.root), *argv[1:]])

    def plan(self):
        return json.loads((self.root / "restart_plan.json").read_text(encoding="utf-8"))


class StatusTests(_RootCase):
    def test_status_reports_the_hub_and_never_the_token(self):
        result_file = self.data / "resume_on_start.result.json"
        result_file.write_text(json.dumps({"sessions": {"codex-run": {"ok": True}}}), encoding="utf-8")
        (self.root / "restart_plan.result.json").write_text(
            json.dumps({"ok": True, "restart_id": "r-old"}), encoding="utf-8")
        code, out = cli.cmd_status(self.args("status"))
        self.assertEqual(code, 0)
        text = json.dumps(out, ensure_ascii=False)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("shutdown_token\"", text)
        self.assertTrue(out["launch"]["shutdown_token_set"])
        self.assertNotIn("shutdown_token", out["launch"])
        self.assertEqual(out["port"], self.server.port)
        self.assertEqual(out["hub"]["pid"], 4242)
        self.assertEqual(out["health"]["status"], "healthy")
        self.assertEqual(out["restart_status"]["phase"], "ready")
        self.assertEqual(out["session_summary"]["count"], 2)
        self.assertEqual(out["session_summary"]["live_count"], 2)
        self.assertIsNone(out["restart_plan"])
        self.assertEqual(out["resume_on_start"]["result"]["sessions"]["codex-run"]["ok"], True)
        self.assertEqual(out["restart_plan_result"]["restart_id"], "r-old")

    def test_status_survives_a_dead_hub_and_a_missing_launch(self):
        self.server.close()
        code, out = cli.cmd_status(self.args("status"))
        self.assertEqual(code, 0)
        self.assertEqual(out["hub"]["error"], "Hub 没有应答")
        shutil.rmtree(self.root)
        code, out = cli.cmd_status(self.args("status", "--port", "4444"))
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("launch.json", out["launch_error"])

    def test_status_shows_a_pending_plan(self):
        (self.root / "restart_plan.json").write_text(json.dumps(
            {"restart_id": "r-pending", "mode": "hub", "expires_at": 9e9}), encoding="utf-8")
        code, out = cli.cmd_status(self.args("status"))
        self.assertEqual(out["restart_plan"]["restart_id"], "r-pending")


class RestartTests(_RootCase):
    def test_restart_writes_the_plan_posts_and_waits_for_ready(self):
        original_post = self.fake.default_post

        def post(path, body, headers):
            self.fake.plan_at_post = self.plan()
            return original_post(path, body, headers)

        self.fake.on_post = post
        code, out = cli.cmd_restart(self.args("restart", "--poll-seconds", "0.01",
                                             "--timeout-seconds", "20"))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["phase"], "ready")
        self.assertEqual(out["accepted"]["http_status"], 202)
        self.assertEqual(out["sessions"]["sessions"]["codex-run"]["ok"], True)

        plan = self.fake.plan_at_post
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["mode"], "hub")
        self.assertEqual(plan["restart_id"], out["restart_id"])
        self.assertEqual(plan["port"], self.server.port)
        self.assertEqual(plan["app_pid"], 11648)
        self.assertEqual(plan["app_creation_time"], 134399000000000000)
        self.assertEqual(plan["hub_pid"], 73584)
        self.assertEqual(plan["hub_creation_time"], 134399000000000001)
        self.assertEqual(Path(plan["data_dir"]), self.data)
        self.assertGreater(plan["expires_at"] - plan["requested_at"], 590)
        self.assertFalse((self.root / "restart_plan.json").exists(),
                         "the plan is removed once the new hub is ready")

        posts = [row for row in self.fake.requests if row[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/api/hub/graceful-restart")
        self.assertEqual(self.fake.posts[-1]["mode"], "hub")
        self.assertEqual(self.fake.posts[-1]["continue_unfinished_turns"], True)

    def test_the_token_and_the_flags_travel_with_the_request(self):
        seen = {}

        def post(path, body, headers):
            seen["headers"] = headers
            seen["body"] = body
            return self.fake.default_post(path, body, headers)

        self.fake.on_post = post
        code, out = cli.cmd_restart(self.args("restart", "--no-continue", "--resume-crashed",
                                              "--session-id", "codex-run",
                                              "--poll-seconds", "0.01", "--timeout-seconds", "20"))
        self.assertEqual(code, 0, out)
        self.assertEqual(seen["headers"]["x-hub-shutdown-token"], TOKEN)
        self.assertEqual(seen["body"]["continue_unfinished_turns"], False)
        self.assertEqual(seen["body"]["resume_crashed"], True)
        self.assertEqual(seen["body"]["session_ids"], ["codex-run"])

    def test_a_refused_request_removes_the_plan_and_touches_nothing(self):
        def refuse(path, body, headers):
            return 409, {"detail": "Hub 已在关闭或重启中"}

        self.fake.on_post = refuse
        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_restart(self.args("restart"))
        self.assertIn("409", str(caught.exception))
        self.assertIn("Hub 已在关闭或重启中", str(caught.exception))
        self.assertFalse((self.root / "restart_plan.json").exists())

    def test_an_unexpired_plan_blocks_the_restart(self):
        (self.root / "restart_plan.json").write_text(json.dumps(
            {"restart_id": "r-pending", "mode": "hub", "expires_at": 9e9}), encoding="utf-8")
        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_restart(self.args("restart"))
        self.assertIn("restart_plan.json", str(caught.exception))
        self.assertEqual([r for r in self.fake.requests if r[0] == "POST"], [])

    def test_no_wait_answers_at_once(self):
        code, out = cli.cmd_restart(self.args("restart", "--no-wait"))
        self.assertEqual(code, 0)
        self.assertEqual(out["phase"], "stopping")
        self.assertIn("--no-wait", out["note"])
        self.assertTrue((self.root / "restart_plan.json").exists(),
                        "--no-wait leaves the plan for the shell")

    def test_a_timed_out_post_keeps_the_plan_when_the_hub_is_stopping(self):
        """A read timeout means the hub may already be stopping for this restart: keep the plan."""
        self.fake.status.update({"phase": "stopping", "restart_id": "r-timeout"})
        opener = _PostFailsOpener(cli.make_opener(), TimeoutError("timed out reading the answer"))
        code, out = cli.cmd_restart(self.args("restart", "--no-wait"), opener=opener)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["accepted"]["unconfirmed"])
        self.assertIn("restart_plan.json", out["note"])
        plan = self.plan()
        self.assertEqual(plan["mode"], "hub")
        self.assertTrue((self.root / "restart_plan.json").exists(),
                        "the hub may have accepted: the plan must not be deleted")

    def test_a_refused_connection_drops_the_plan(self):
        """The request never reached the hub, so nothing is stopping: the plan is a leftover."""
        opener = _PostFailsOpener(cli.make_opener(), ConnectionRefusedError(10061, "refused"))
        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_restart(self.args("restart"), opener=opener)
        self.assertIn("发不出去", str(caught.exception))
        self.assertFalse((self.root / "restart_plan.json").exists())

    def test_a_session_that_failed_to_resume_exits_2(self):
        def partial(path, body, headers):
            self.fake.posts.append(body)
            self.fake.info["started_at"] = "2026-11-24T10:02:00+08:00"
            self.fake.status.update({"phase": "failed", "failed_count": 1, "resumed_count": 1,
                                    "target_count": 2,
                                    "result": {"sessions": {"codex-run": {
                                        "ok": False, "error": "cannot"}}}})
            return 202, {"ok": True, "restart_id": body.get("restart_id"), "mode": "hub"}

        self.fake.on_post = partial
        code, out = cli.cmd_restart(self.args("restart", "--poll-seconds", "0.01",
                                             "--timeout-seconds", "20"))
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["phase"], "failed")
        self.assertEqual(out["status"]["failed_count"], 1)

    def test_a_hub_that_never_comes_back_times_out(self):
        def swallow(path, body, headers):
            return 202, {"ok": True, "restart_id": body.get("restart_id"), "mode": "hub"}

        self.fake.on_post = swallow
        with patch.object(self.fake, "get", lambda path: (503, {}) if path != "/api/hub/info"
                         else (200, self.fake.info)):
            with self.assertRaises(cli.CliError) as caught:
                cli.cmd_restart(self.args("restart", "--poll-seconds", "0.01",
                                          "--timeout-seconds", "1"))
        self.assertIn("超时", str(caught.exception))


class ReplaceExeTests(_RootCase):
    def setUp(self):
        super().setUp()
        self.target = self.root / "app" / "mcp-agent-hub-windows-x64-portable.exe"
        self.target.parent.mkdir()
        self.target.write_bytes(b"old exe payload")
        self.new_exe = self.root / "build" / "mcp-agent-hub-windows-x64-portable.exe"
        self.new_exe.parent.mkdir()
        self.new_exe.write_bytes(b"new exe payload, a different build")
        self.new_sha = sha(self.new_exe)

    def replace_args(self, *extra):
        return self.args("replace-exe", str(self.new_exe), "--target-exe", str(self.target),
                         "--skip-build-probe", *extra)

    def test_a_sha_mismatch_refuses_without_touching_anything(self):
        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_replace_exe(self.replace_args("--sha256", "0" * 64))
        self.assertIn("SHA-256", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b"old exe payload")
        self.assertEqual([r for r in self.fake.requests if r[0] == "POST"], [])
        self.assertFalse((self.root / "restart_plan.json").exists())
        self.assertEqual(list(self.target.parent.glob(".hub-update-*")), [])

    def test_the_sidecar_sha_is_checked_too(self):
        (Path(str(self.new_exe) + ".sha256")).write_text(self.new_sha + "  file\n", encoding="utf-8")
        code, out = cli.cmd_replace_exe(self.replace_args("--dry-run"))
        self.assertEqual(code, 0)
        (Path(str(self.new_exe) + ".sha256")).write_text("0" * 64, encoding="utf-8")
        with self.assertRaises(cli.CliError):
            cli.cmd_replace_exe(self.replace_args("--dry-run"))

    def test_the_build_probe_must_agree_with_the_file(self):
        def probe(path):
            return {"version": "0.1.0", "payload_id": "abc", "exe_sha256": "0" * 64,
                    "portable": True}

        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_replace_exe(self.args("replace-exe", str(self.new_exe),
                                          "--target-exe", str(self.target)), probe=probe)
        self.assertIn("exe_sha256", str(caught.exception))

        def portable_probe(path):
            return {"version": "0.1.0", "payload_id": "abc", "exe_sha256": self.new_sha,
                    "portable": True}

        code, out = cli.cmd_replace_exe(self.args("replace-exe", str(self.new_exe),
                                                  "--target-exe", str(self.target),
                                                  "--dry-run"), probe=portable_probe)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["build"]["payload_id"], "abc")

    def test_the_same_file_and_a_dist_build_are_refused(self):
        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_replace_exe(self.args("replace-exe", str(self.new_exe),
                                          "--target-exe", str(self.new_exe),
                                          "--skip-build-probe", "--dry-run"))
        self.assertIn("同一个文件", str(caught.exception))
        dist = self.root / "desktop" / "dist" / "mcp-agent-hub-windows-x64-portable.exe"
        dist.parent.mkdir(parents=True)
        dist.write_bytes(b"dist build")
        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_replace_exe(self.args("replace-exe", str(self.new_exe),
                                          "--target-exe", str(dist),
                                          "--skip-build-probe", "--dry-run"))
        self.assertIn("desktop", str(caught.exception))

    def test_dry_run_touches_nothing(self):
        code, out = cli.cmd_replace_exe(self.replace_args("--dry-run"))
        self.assertEqual(code, 0)
        self.assertEqual(out["phase"], "dry-run")
        plan = out["plan"]
        self.assertEqual(plan["mode"], "replace-exe")
        self.assertEqual(plan["target_exe"], str(self.target.resolve()))
        self.assertEqual(plan["new_exe_sha256"], self.new_sha)
        self.assertEqual(plan["old_exe_sha256"], sha(self.target))
        self.assertFalse((self.root / "restart_plan.json").exists())
        self.assertEqual(list(self.target.parent.glob(".hub-update-*")), [])
        self.assertEqual([r for r in self.fake.requests if r[0] == "POST"], [])

    def test_the_full_path_stages_swaps_and_verifies(self):
        captured = {}

        def spawn(helper: Path, plan: Path):
            captured["helper"] = helper
            captured["plan"] = json.loads(plan.read_text(encoding="utf-8"))
            # What the Rust helper does: swap the staged file in, record the result, then the
            # shell starts the new hub.
            shutil.copy2(captured["plan"]["staged_exe"], captured["plan"]["target_exe"])
            (self.root / "restart_plan.result.json").write_text(
                json.dumps({"ok": True, "restart_id": captured["plan"]["restart_id"],
                            "action": "replaced"}), encoding="utf-8")
            self.fake.info["started_at"] = "2026-11-24T10:05:00+08:00"
            self.fake.status.update({"phase": "ready", "restart_id": captured["plan"]["restart_id"],
                                     "mode": "replace-exe", "target_count": 2,
                                     "resumed_count": 2, "failed_count": 0})

        code, out = cli.cmd_replace_exe(
            self.args("replace-exe", str(self.new_exe), "--target-exe", str(self.target),
                      "--skip-build-probe", "--sha256", self.new_sha,
                      "--poll-seconds", "0.01", "--timeout-seconds", "20"),
            spawn_helper=spawn)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["exe_replaced"])
        self.assertEqual(out["exe_sha256"], self.new_sha)
        self.assertEqual(self.target.read_bytes(), self.new_exe.read_bytes())
        self.assertEqual(out["plan_result"]["action"], "replaced")
        self.assertEqual(captured["plan"]["mode"], "replace-exe")
        self.assertEqual(captured["plan"]["staged_exe"],
                         str(self.target.parent / f".hub-update-{out['restart_id'][:12]}.exe"))
        self.assertEqual(Path(captured["helper"]).parent,
                         Path(tempfile.gettempdir()))
        self.assertTrue(str(captured["helper"]).endswith(".exe"))
        self.assertIn("POST", [r[0] for r in self.fake.requests])
        self.assertFalse((self.root / "restart_plan.json").exists())
        captured["helper"].unlink(missing_ok=True)

    def test_an_endpoint_failure_rolls_back_the_staging(self):
        def refuse(path, body, headers):
            return 403, {"detail": "停止令牌无效"}

        def never_used(helper, plan):
            raise AssertionError("the helper must not start after a refused request")

        self.fake.on_post = refuse
        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_replace_exe(self.args("replace-exe", str(self.new_exe),
                                          "--target-exe", str(self.target),
                                          "--skip-build-probe"), spawn_helper=never_used)
        self.assertIn("403", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b"old exe payload")
        self.assertFalse((self.root / "restart_plan.json").exists())
        self.assertEqual(list(self.target.parent.glob(".hub-update-*")), [])

    def test_a_plan_result_that_failed_ends_the_wait_at_once(self):
        """A helper that already reported a failure must not cost the caller its whole timeout."""
        def report_failure(helper, plan):
            body = json.loads(plan.read_text(encoding="utf-8"))
            (self.root / "restart_plan.result.json").write_text(json.dumps({
                "ok": False, "phase": "validate_failed", "restart_id": body["restart_id"],
                "detail": "新 EXE 不是便携构建"}), encoding="utf-8")

        started = time.monotonic()
        code, out = cli.cmd_replace_exe(
            self.args("replace-exe", str(self.new_exe), "--target-exe", str(self.target),
                      "--skip-build-probe", "--poll-seconds", "0.05",
                      "--timeout-seconds", "600"),
            spawn_helper=report_failure)
        elapsed = time.monotonic() - started
        self.assertEqual(code, 1, out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["phase"], "plan_failed")
        self.assertEqual(out["plan_failed_phase"], "validate_failed")
        self.assertIn("validate_failed", out["error"])
        self.assertIn("新 EXE 不是便携构建", out["error"])
        self.assertLess(elapsed, 2.0, "the wait must stop on the plan's own failure, not wait 600 s")
        self.assertNotIn("超时", str(out.get("error") or ""))

    def test_a_rolled_back_plan_says_it_was_rolled_back(self):
        def report_rollback(helper, plan):
            body = json.loads(plan.read_text(encoding="utf-8"))
            (self.root / "restart_plan.result.json").write_text(json.dumps({
                "ok": False, "phase": "swap_failed", "restart_id": body["restart_id"],
                "rolled_back": True, "detail": "ReplaceFileW 失败，已回滚"}), encoding="utf-8")

        code, out = cli.cmd_replace_exe(
            self.args("replace-exe", str(self.new_exe), "--target-exe", str(self.target),
                      "--skip-build-probe", "--poll-seconds", "0.02",
                      "--timeout-seconds", "30"),
            spawn_helper=report_rollback)
        self.assertEqual(code, 1, out)
        self.assertEqual(out["rollback"], "升级失败，已自动回滚至旧版本")
        self.assertEqual(self.target.read_bytes(), b"old exe payload")

    def test_a_helper_that_does_not_start_brings_the_old_exe_back(self):
        """A 202 is already a stop: a helper that never started must not leave the user hubless."""
        def boom(helper, plan):
            raise OSError("no shell")

        def accept_then_die(path, body, headers):
            answer = self.fake.default_post(path, body, headers)
            self.fake.dead = True  # accepted, and the hub is now going down for the restart
            return 202, answer

        self.fake.on_post = accept_then_die
        launched = []
        code, out = cli.cmd_replace_exe(
            self.args("replace-exe", str(self.new_exe), "--target-exe", str(self.target),
                      "--skip-build-probe", "--poll-seconds", "0.02",
                      "--timeout-seconds", "3"),
            spawn_helper=boom,
            launch_exe=lambda path: launched.append(Path(path)) or True)
        self.assertEqual(code, 1, out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["phase"], "helper_launch_failed")
        self.assertTrue(out["helper_launch_failed"])
        self.assertIn("no shell", out["helper_error"])
        self.assertTrue(out["recovered"])
        self.assertEqual(len(launched), 1)
        self.assertTrue(cli.same_path(launched[0], self.target),
                        "the fallback starts the original EXE, not the new one")
        self.assertEqual(self.target.read_bytes(), b"old exe payload", "nothing was swapped in")
        self.assertEqual(list(self.target.parent.glob(".hub-update-*")), [],
                         "the staged copy is cleaned up")
        self.assertTrue((self.root / "restart_plan.json").exists(),
                        "the plan stays: the shell must see this stop was planned")

    def test_a_helper_failure_does_not_start_a_second_hub_the_shell_already_started(self):
        def boom(helper, plan):
            raise OSError("no shell")

        def accept_then_revive(path, body, headers):
            answer = self.fake.default_post(path, body, headers)
            self.fake.info["pid"] = 9999          # the shell brought a new hub back
            self.fake.info["started_at"] = "2026-11-24T10:06:00+08:00"
            return 202, answer

        self.fake.on_post = accept_then_revive
        launched = []
        code, out = cli.cmd_replace_exe(
            self.args("replace-exe", str(self.new_exe), "--target-exe", str(self.target),
                      "--skip-build-probe", "--poll-seconds", "0.02",
                      "--timeout-seconds", "3"),
            spawn_helper=boom, launch_exe=lambda path: launched.append(Path(path)) or True)
        self.assertEqual(code, 1, out)
        self.assertTrue(out["helper_launch_failed"])
        self.assertTrue(out["recovered"])
        self.assertEqual(launched, [], "a hub is already answering: no fallback needed")


class MainTests(_RootCase):
    def test_main_prints_one_json_object_and_sets_the_exit_code(self):
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["status", "--portable-root", str(self.root)])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["command"], "status")
        self.assertNotIn(TOKEN, buffer.getvalue())

    def test_a_refusal_is_still_json(self):
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["restart", "--portable-root", str(Path(self.root) / "nope")])
        self.assertEqual(code, 1)
        payload = json.loads(buffer.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("launch.json", payload["error"])


@unittest.skipUnless(os.name == "nt", "query_process_image 是 Win32 进程句柄查询")
class ProcessImageTests(unittest.TestCase):
    def test_a_running_process_reports_its_image(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            image = cli.query_process_image(proc.pid)
            self.assertIsNotNone(image)
            self.assertEqual(os.path.normcase(Path(image).resolve()),
                             os.path.normcase(Path(sys.executable).resolve()))
        finally:
            proc.kill()
            proc.wait()

    def test_an_exited_process_with_an_open_handle_is_not_running(self):
        # Popen keeps the process handle open after the child exits, so Windows still has the
        # process object: OpenProcess succeeds, but the image must not look like a live app.
        proc = subprocess.Popen([sys.executable, "-c", "pass"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        proc.wait()
        self.assertIsNone(cli.query_process_image(proc.pid))
        del proc  # closing the last handle is what makes the PID disappear


if __name__ == "__main__":
    unittest.main()
