"""Engine-executed test commands (allowlisted, no shell, sanitised env)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from workforce.config import PolicyConfig
from workforce.security.policy import check_command
from workforce.security.redaction import redact

_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|SESSION)", re.I)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str
    duration_s: float
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        m = re.findall(r"=+ (.*(?:passed|failed|error|no tests ran).*?) =+", self.output)
        if m:
            return m[-1]
        if self.timed_out:
            return "timed out"
        return f"exit {self.exit_code}"

    def failures(self) -> list[str]:
        return sorted(set(re.findall(r"^(?:FAILED|ERROR) (\S+)", self.output, re.M)))


def sanitized_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def resolve_argv(argv: list[str], cwd: Path) -> list[str]:
    if argv[0] == "python":
        venv = cwd / ".venv" / "bin" / "python"
        return [str(venv) if venv.exists() else sys.executable, *argv[1:]]
    return argv


def run_checked(command: str, cwd: Path, policy: PolicyConfig, timeout_s: int, extra_args: list[str] | None = None) -> CommandResult:
    argv = check_command(command, policy) + list(extra_args or [])
    t0 = time.monotonic()
    try:
        p = subprocess.run(resolve_argv(argv, cwd), cwd=cwd, capture_output=True, text=True, timeout=timeout_s,
                           env=sanitized_env(), stdin=subprocess.DEVNULL)
        out = (p.stdout + "\n" + p.stderr)
        return CommandResult(" ".join(argv), p.returncode, redact(out[-30000:]), time.monotonic() - t0)
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        return CommandResult(" ".join(argv), -1, redact(out[-30000:]), time.monotonic() - t0, timed_out=True)
