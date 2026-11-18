"""Workspace boundary enforcement for agent runs.

Prevention is layered: every agent runs with its task worktree (or, read-only, the
project repository) as cwd; Claude Code edits are limited to that directory and an
explicit tool allowlist; Codex uses its OS sandbox (`workspace-write` covers only
the cwd); OpenCode denies `external_directory` access. This module adds the
engine-side checks:

1. `check_cwd`: an agent may only be started inside the workspace roots of its own
   project (its worktree directory and repository).
2. `WorkspaceGuard`: snapshots the working trees of every registered project
   repository and the engine installation before an agent runs and reports any
   file that changed while it ran.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from workforce.security.policy import PolicyViolation

Snapshot = dict[str, dict[str, tuple[str, int, int]]]


def check_cwd(cwd: Path, roots: list[Path]) -> None:
    c = Path(cwd).resolve()
    for r in roots:
        r = Path(r).resolve()
        if c == r or c.is_relative_to(r):
            return
    raise PolicyViolation("workspace-escape",
                          f"agent working directory {c} is outside the project's workspaces {[str(r) for r in roots]}")


def _repo_state(repo: Path) -> dict[str, tuple[str, int, int]] | None:
    p = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=repo,
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    state: dict[str, tuple[str, int, int]] = {}
    entries = p.stdout.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        code, path = e[:2], e[3:]
        if code[0] in "RC":  # rename/copy: next entry is the source path
            i += 1
        f = repo / path
        try:
            st = f.stat()
            state[path] = (code, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            state[path] = (code, 0, -1)
    return state


class WorkspaceGuard:
    def __init__(self, repos: list[Path]):
        self.repos = [Path(r).resolve() for r in repos if Path(r).exists()]

    def snapshot(self) -> Snapshot:
        snap: Snapshot = {}
        for r in self.repos:
            st = _repo_state(r)
            if st is not None:
                snap[str(r)] = st
        return snap

    def changes(self, before: Snapshot) -> dict[str, list[str]]:
        after = self.snapshot()
        out: dict[str, list[str]] = {}
        for repo, b in before.items():
            a = after.get(repo, {})
            changed = sorted(f for f in a.keys() | b.keys() if a.get(f) != b.get(f))
            if changed:
                out[repo] = changed
        return out
