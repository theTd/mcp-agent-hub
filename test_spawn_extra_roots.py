"""extra_roots: folders beyond cwd a worker may read and write, checked, and kept across a resume."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from fastmcp import Client

from spawn_extra_roots import EXTRA_ROOTS_MAX, extra_root_args, normalize_extra_roots
from test_spawn_wiring import SpawnWiringCase, _data


class NormalizeExtraRootsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="extra-roots-")
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        (self.home / ".ssh" / "keys").mkdir(parents=True)
        (self.home / "work").mkdir()
        self.data = self.base / "hubdata"
        (self.data / "briefs").mkdir(parents=True)
        self.shared = self.base / "shared"
        self.shared.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, *paths):
        return normalize_extra_roots([str(p) for p in paths], forbidden=[str(self.data)], home=str(self.home))

    def refused(self, path, words):
        with self.assertRaises(ValueError) as caught:
            self.check(path)
        self.assertIn(words, str(caught.exception))

    def test_real_folders_pass_in_order_without_duplicates(self):
        self.assertEqual(self.check(self.shared, self.home / "work", self.shared),
                         [str(self.shared), str(self.home / "work")])
        self.assertEqual(normalize_extra_roots(None), [])

    def test_shape_errors(self):
        for bad in ("E:/x", [1], ["relative/folder"], [""], ["E:\\x\x00"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_extra_roots(bad, home=str(self.home))
        with self.assertRaises(ValueError):
            self.check(*([self.shared] * (EXTRA_ROOTS_MAX + 1)))

    def test_missing_folders_and_files_are_refused(self):
        self.refused(self.base / "nope", "not an existing folder")
        (self.base / "file.txt").write_text("x", encoding="utf-8")
        self.refused(self.base / "file.txt", "not an existing folder")

    def test_roots_home_credentials_system_and_hub_data_are_refused(self):
        self.refused(Path(self.base.anchor), "drive or share root")
        self.refused(self.home, "home folder")
        self.refused(self.base, "home folder")  # it holds the home folder
        self.refused(self.home / ".ssh" / "keys", ".ssh")
        self.refused(self.data, "data folder")
        self.refused(self.data / "briefs", "data folder")
        system = os.environ.get("SystemRoot") or os.environ.get("ProgramData")
        if system and os.path.isdir(system):
            self.refused(system, "system or application folder")

    @unittest.skipUnless(sys.platform == "win32", "junctions are Windows folders")
    def test_a_junction_is_refused(self):
        link = self.base / "link"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.shared)], capture_output=True)
        if made.returncode != 0:
            self.skipTest("mklink /J is not available here")
        try:
            self.refused(link, "link or junction")
        finally:
            os.rmdir(link)  # removes the junction only, never its target

    def test_engine_flags(self):
        for engine in ("claude", "codex", "omp", "kimi", "agy"):
            self.assertEqual(extra_root_args(engine, "tui", ["E:\\a", "E:\\b"]), ["--add-dir=E:\\a", "--add-dir=E:\\b"])
        self.assertEqual(extra_root_args("dsh", "web_gui", []), [])
        for engine, kind in (("dsh", "tui"), ("dsh", "web_gui"), ("pwsh", "tui")):
            with self.subTest(engine=engine), self.assertRaises(ValueError) as caught:
                extra_root_args(engine, kind, ["E:\\a"])
            self.assertIn("put the files under cwd", str(caught.exception))


class ExtraRootsSpawnTests(SpawnWiringCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.shared_tmp = tempfile.TemporaryDirectory(prefix="extra-roots-shared-")
        self.shared = str(Path(self.shared_tmp.name).resolve())

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.shared_tmp.cleanup()

    def assert_root_launched(self, sess):
        flag = f"--add-dir={self.shared}"
        self.assertIn(flag, sess.args)
        self.assertEqual(sess.profile_snapshot["extra_roots"], [self.shared])
        self.assertIn(flag, sess.profile_snapshot["args"])
        record = {"engine": "omp", "profile_snapshot": sess.profile_snapshot,
                  "conversation_ref": {"session_dir": "d", "session_file": "f"}}
        self.assertIn(flag, self.hub.build_resume_launch(record)["args"])

    async def test_spawn_subagent_launches_and_resumes_with_the_root(self):
        data = await self.spawn_via_mcp(extra_roots=[self.shared])
        self.assert_root_launched(self.hub.sessions[data["session_id"]])

    async def test_spawn_subagents_and_rest_pass_it_through(self):
        async with Client(self.server.mcp) as client:
            data = _data(await client.call_tool("spawn_subagents", {"specs": [
                {"agent_type": "omp", "leaf_worker": True, "task": "Grouped", "extra_roots": [self.shared]},
            ]}))
        self.assert_root_launched(self.hub.sessions[data["sessions"][0]["session_id"]])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://127.0.0.1:4444") as client:
            ok = await self.spawn_via_rest(client, extra_roots=[self.shared])
            bad = await self.spawn_via_rest(client, extra_roots=[str(self.root)])  # the hub's data folder
            preview = await client.post("/api/launch-preview",
                                        json={"profile_id": "omp", "extra_roots": [self.shared]})
        self.assertEqual(ok.status_code, 200)
        self.assert_root_launched(self.hub.sessions[ok.json()["session"]["session_id"]])
        self.assertEqual(bad.status_code, 400)
        self.assertIn("data folder", bad.text)
        self.assertEqual(preview.json().get("extra_roots"), [self.shared], preview.text)

    async def test_a_refused_root_starts_nothing(self):
        before = set(self.hub.sessions)
        with self.assertRaises(Exception) as caught:
            await self.spawn_via_mcp(extra_roots=["relative"])
        self.assertIn("absolute path", str(caught.exception))
        self.assertEqual(set(self.hub.sessions), before)


if __name__ == "__main__":
    unittest.main()
