import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import cwd_git_status


class CwdGitStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_repo_and_non_repo(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            result = await cwd_git_status.collect_git_status([str(repo), str(Path(root) / "missing")])
        self.assertTrue(result[str(repo)]["ok"])
        self.assertFalse(result[str(Path(root) / "missing")]["ok"])

    async def test_repository_after_more_than_five_non_repo_folders(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(7):
                (Path(root) / f"empty{i}").mkdir()
            repo = Path(root) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            result = await cwd_git_status.collect_git_status([root])
        self.assertEqual(result[root]["repositories"][0]["cwd"], str(repo))

    async def test_timeout_is_reported(self):
        with patch.object(cwd_git_status, "_run_git", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = await cwd_git_status.collect_git_status(["C:/slow"])
        self.assertIn("timed out", result["C:/slow"]["error"])

    async def test_windows_child_has_no_window_and_prompt_disabled(self):
        class Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        with patch.object(cwd_git_status.os, "name", "nt"), patch.object(cwd_git_status.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), patch.object(cwd_git_status.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=Proc())) as launch:
            await cwd_git_status._run_git("C:/repo", "status")
        kwargs = launch.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    async def test_child_scan_is_threaded_and_bounded(self):
        with patch.object(cwd_git_status, "_run_git", new=AsyncMock(return_value=(128, "fatal: not a git repository"))):
            with patch.object(cwd_git_status.asyncio, "to_thread", new=AsyncMock(return_value=[])) as threaded:
                await cwd_git_status._status("C:/cwd")
        threaded.assert_awaited_once()

