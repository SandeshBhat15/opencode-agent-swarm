"""Test helpers. Zero third-party dependencies, zero network, zero API spend."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_cli(*args: str, workspace: Path | str = ".", expect: int | None = 0) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI in-process-friendly fashion: real subprocess, real exit code."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "swarmkit", "--workspace", str(workspace), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            f"`{' '.join(args)}` exited {proc.returncode}, expected {expect}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


class TempWorkspace(unittest.TestCase):
    """Gives each test an isolated workspace and run dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_run(self, slug: str = "test-run") -> Path:
        from swarmkit import store

        return store.init_run(self.ws, slug)


class GitRepo(TempWorkspace):
    """A real git repo with one commit, so worktree code paths actually run."""

    def setUp(self) -> None:
        super().setUp()
        git("init", "-q", "-b", "main", cwd=self.ws)
        git("config", "user.email", "test@example.com", cwd=self.ws)
        git("config", "user.name", "Test", cwd=self.ws)
        (self.ws / "README.md").write_text("seed\n", encoding="utf-8")
        git("add", "-A", cwd=self.ws)
        git("commit", "-q", "-m", "seed", cwd=self.ws)