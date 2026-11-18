"""Deterministic execution policy. Agents cannot override these checks: the
engine calls them before running commands, writing branches or integrating."""

from __future__ import annotations

import fnmatch
import shlex
from pathlib import Path

from workforce.config import PolicyConfig


class PolicyViolation(Exception):
    def __init__(self, rule: str, detail: str):
        super().__init__(f"[{rule}] {detail}")
        self.rule = rule
        self.detail = detail


_SHELL_META = set(";&|`$<>\n")


def parse_command(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        argv = command
    else:
        if any(c in command for c in _SHELL_META):
            raise PolicyViolation("shell-metacharacters", f"command contains shell metacharacters: {command!r}")
        argv = shlex.split(command)
    if not argv:
        raise PolicyViolation("empty-command", "empty command")
    return argv


def check_command(command: str | list[str], policy: PolicyConfig) -> list[str]:
    """Return argv if the command is allowlisted; raise PolicyViolation otherwise.
    Commands are executed without a shell, so the allowlist is a prefix match on argv."""
    argv = parse_command(command)
    normalized = [Path(argv[0]).name if argv[0].startswith(("/", ".")) else argv[0], *argv[1:]]
    if normalized[0] in {"python3", "python3.12", "python3.13"}:
        normalized[0] = "python"
    for allowed in policy.allowed_commands:
        prefix = shlex.split(allowed)
        if normalized[: len(prefix)] == prefix:
            return argv
    raise PolicyViolation("command-not-allowlisted", f"{' '.join(argv)!r} is not in policy.allowed_commands")


def ensure_within(path: str | Path, root: str | Path) -> Path:
    root_r = Path(root).resolve()
    p = (root_r / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if p != root_r and root_r not in p.parents:
        raise PolicyViolation("path-escape", f"{path} escapes repository root {root_r}")
    return p


def check_branch_writable(branch: str, policy: PolicyConfig, approved: bool = False) -> None:
    if branch in policy.protected_branches and not approved:
        raise PolicyViolation("protected-branch", f"writing to protected branch {branch!r} requires human approval")


def _match(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return True
        if pat.endswith("/**") and (path == pat[:-3] or path.startswith(pat[:-2])):
            return True
    return False


def classify_diff(changed: list[str], owned: list[str], policy: PolicyConfig) -> dict[str, list[str]]:
    """Split changed files into protected / secret / out-of-ownership buckets."""
    out = {"protected": [], "secret": [], "unowned": []}
    for f in changed:
        if _match(f, policy.protected_paths):
            out["protected"].append(f)
        if any(fnmatch.fnmatch(Path(f).name, g) for g in policy.secret_file_globs):
            out["secret"].append(f)
        if owned and not _match(f, owned):
            out["unowned"].append(f)
    return out


def paths_overlap(a: list[str], b: list[str]) -> bool:
    """Conservative check for whether two ownership glob sets can collide."""
    if not a or not b:
        return True  # unknown ownership is treated as conflicting
    for pa in a:
        for pb in b:
            if fnmatch.fnmatch(pa, pb) or fnmatch.fnmatch(pb, pa):
                return True
            sa, sb = pa.split("*")[0], pb.split("*")[0]
            if sa.startswith(sb) or sb.startswith(sa):
                return True
    return False


DESTRUCTIVE_GIT = {("push", "--force"), ("push", "-f"), ("reset", "--hard"), ("clean", "-fd"), ("clean", "-fdx"),
                   ("branch", "-D"), ("push", "--delete")}


def is_destructive_git(args: list[str]) -> bool:
    if not args:
        return False
    return any(args[0] == a and b in args[1:] for a, b in DESTRUCTIVE_GIT)
