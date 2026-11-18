from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workforce.app import build_app
from workforce.config import WorkforceConfig


@pytest.fixture
def cfg(tmp_path: Path) -> WorkforceConfig:
    c = WorkforceConfig(home=tmp_path / ".workforce")
    c.limits.poll_interval_s = 0.05
    return c


@pytest.fixture
def wf(cfg):
    return build_app(cfg=cfg, stderr_logs=False)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "test@example.invalid")
    git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("# sample\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r
