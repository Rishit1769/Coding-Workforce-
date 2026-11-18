"""Git operations used by the engine. Agents never run git themselves: the
engine creates worktrees, commits with role attribution, merges and rolls back.

Destructive operations are restricted to engine-owned worktrees/branches
(prefixes `wf/`), and never touch protected branches without approval."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from workforce.config import PolicyConfig
from workforce.security.policy import PolicyViolation, check_branch_writable, is_destructive_git

ENGINE_BRANCH_PREFIX = "wf/"


class GitError(Exception):
    pass


@dataclass
class MergeResult:
    ok: bool
    conflicts: list[str]
    output: str


class GitRepo:
    def __init__(self, path: Path | str, policy: PolicyConfig):
        self.path = Path(path).resolve()
        self.policy = policy

    # ------------------------------------------------------------------
    def git(self, *args: str, cwd: Path | None = None, check: bool = True, allow_destructive: bool = False,
            env: dict | None = None) -> subprocess.CompletedProcess:
        if is_destructive_git(list(args)) and not allow_destructive:
            raise PolicyViolation("destructive-git", f"git {' '.join(args)} requires approval")
        full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
        p = subprocess.run(["git", *args], cwd=cwd or self.path, capture_output=True, text=True, env=full_env)
        if check and p.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
        return p

    def out(self, *args: str, cwd: Path | None = None) -> str:
        return self.git(*args, cwd=cwd).stdout.strip()

    def is_repo(self) -> bool:
        return self.git("rev-parse", "--git-dir", check=False).returncode == 0

    def rev(self, ref: str, cwd: Path | None = None) -> str:
        return self.out("rev-parse", ref, cwd=cwd)

    def branch_exists(self, name: str) -> bool:
        return self.git("show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0

    def current_branch(self, cwd: Path | None = None) -> str:
        return self.out("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)

    def is_clean(self, cwd: Path | None = None) -> bool:
        return self.out("status", "--porcelain", cwd=cwd) == ""

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0

    # ---- branches / worktrees -----------------------------------------
    def ensure_branch(self, name: str, base: str) -> None:
        if not name.startswith(ENGINE_BRANCH_PREFIX):
            check_branch_writable(name, self.policy)
        if not self.branch_exists(name):
            self.git("branch", name, base)

    def worktrees(self) -> dict[str, str]:
        res: dict[str, str] = {}
        path = None
        for line in self.out("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = line.split(" ", 1)[1]
            elif line.startswith("branch ") and path:
                res[str(Path(path).resolve())] = line.split(" ", 1)[1].removeprefix("refs/heads/")
        return res

    def add_worktree(self, path: Path, branch: str, base: str | None = None) -> Path:
        path = Path(path).resolve()
        if str(path) in self.worktrees():
            return path
        if path.exists() and any(path.iterdir()):
            raise GitError(f"worktree path {path} exists and is not a registered worktree")
        self.git("worktree", "prune")
        if not self.branch_exists(branch):
            self.ensure_branch(branch, base or "HEAD")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", str(path), branch)
        return path

    def add_detached_worktree(self, path: Path, ref: str) -> Path:
        path = Path(path).resolve()
        if path.exists():
            self.remove_worktree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "--detach", str(path), ref)
        return path

    def remove_worktree(self, path: Path) -> None:
        path = Path(path).resolve()
        if str(path) in self.worktrees() or (path / ".git").exists():
            self.git("worktree", "remove", "--force", str(path), check=False)
        self.git("worktree", "prune", check=False)

    def reset_worktree(self, wt: Path, ref: str) -> None:
        """Discard uncommitted agent edits in an engine-owned worktree."""
        branch = self.current_branch(cwd=wt)
        if branch != "HEAD" and not branch.startswith(ENGINE_BRANCH_PREFIX):
            raise PolicyViolation("reset-non-engine-branch", f"refusing to reset {branch}")
        self.git("merge", "--abort", cwd=wt, check=False)
        self.git("reset", "--hard", ref, cwd=wt, allow_destructive=True)
        self.git("clean", "-fd", cwd=wt, allow_destructive=True)

    # ---- changes ------------------------------------------------------
    def changed_files(self, base: str, head: str = "HEAD", cwd: Path | None = None) -> list[str]:
        o = self.out("diff", "--name-only", f"{base}...{head}", cwd=cwd)
        return [f for f in o.splitlines() if f]

    def working_changes(self, cwd: Path) -> list[str]:
        files = []
        for line in self.out("status", "--porcelain", "--untracked-files=all", cwd=cwd).splitlines():
            entry = line[3:]
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            files.append(entry.strip('"'))
        return files

    def diff(self, base: str, head: str = "HEAD", cwd: Path | None = None, max_chars: int = 60000) -> str:
        d = self.out("diff", "--stat", "--patch", f"{base}...{head}", cwd=cwd)
        if len(d) > max_chars:
            d = d[:max_chars] + f"\n... [diff truncated, {len(d)} chars total]"
        return d

    def commit_all(self, wt: Path, message: str, role: str, model: str, trailers: dict[str, str]) -> str | None:
        branch = self.current_branch(cwd=wt)
        if not branch.startswith(ENGINE_BRANCH_PREFIX):
            check_branch_writable(branch, self.policy)
        self.git("add", "-A", cwd=wt)
        if self.git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode == 0:
            return None
        body = message + "\n\n" + "\n".join(f"{k}: {v}" for k, v in trailers.items())
        env = {"GIT_AUTHOR_NAME": f"workforce/{role} ({model})", "GIT_AUTHOR_EMAIL": f"{role}@workforce.local",
               "GIT_COMMITTER_NAME": "Coding Workforce Engine", "GIT_COMMITTER_EMAIL": "engine@workforce.local"}
        self.git("commit", "-q", "--no-verify", "-m", body, cwd=wt, env=env)
        return self.rev("HEAD", cwd=wt)

    def merge(self, wt: Path, ref: str, message: str) -> MergeResult:
        env = {"GIT_AUTHOR_NAME": "workforce/devops", "GIT_AUTHOR_EMAIL": "devops@workforce.local",
               "GIT_COMMITTER_NAME": "Coding Workforce Engine", "GIT_COMMITTER_EMAIL": "engine@workforce.local"}
        p = self.git("merge", "--no-ff", "--no-edit", "-m", message, ref, cwd=wt, check=False, env=env)
        if p.returncode == 0:
            return MergeResult(True, [], p.stdout)
        conflicts = self.out("diff", "--name-only", "--diff-filter=U", cwd=wt).splitlines()
        return MergeResult(False, conflicts, p.stdout + p.stderr)

    def conclude_merge(self, wt: Path) -> str:
        env = {"GIT_COMMITTER_NAME": "Coding Workforce Engine", "GIT_COMMITTER_EMAIL": "engine@workforce.local"}
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "--no-verify", "--no-edit", cwd=wt, env=env)
        return self.rev("HEAD", cwd=wt)

    def has_conflict_markers(self, wt: Path, files: list[str]) -> list[str]:
        bad = []
        for f in files:
            p = wt / f
            if p.exists() and p.is_file():
                txt = p.read_text(errors="replace")
                if "<<<<<<< " in txt or "\n>>>>>>> " in txt:
                    bad.append(f)
        return bad

    def revert(self, wt: Path, sha: str) -> str:
        """Non-destructive rollback of an integrated change (creates a revert commit)."""
        args = ["revert", "--no-edit"]
        if len(self.out("rev-list", "--parents", "-n", "1", sha, cwd=wt).split()) > 2:
            args += ["-m", "1"]
        env = {"GIT_COMMITTER_NAME": "Coding Workforce Engine", "GIT_COMMITTER_EMAIL": "engine@workforce.local"}
        self.git(*args, sha, cwd=wt, env=env)
        return self.rev("HEAD", cwd=wt)

    def log(self, ref: str, n: int = 50) -> list[dict]:
        fmt = "%H%x1f%an%x1f%ad%x1f%s"
        rows = []
        for line in self.out("log", f"-n{n}", f"--format={fmt}", "--date=iso-strict", ref).splitlines():
            sha, author, date, subject = line.split("\x1f")
            rows.append({"sha": sha, "author": author, "date": date, "subject": subject})
        return rows
