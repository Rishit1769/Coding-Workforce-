"""Background engine launcher with duplicate-instance prevention.

Used by `workforce start` and the bare `force` command. An engine is never
started for a project whose engine lock is held, nor while the multi-project
supervisor (which schedules every registered project) is running. The engine
process itself also takes an exclusive flock, so even two launchers racing each
other cannot produce two engines for one project."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from workforce.engine.orchestrator import lock_holder
from workforce.projects import ProjectContext, ProjectRegistry


@dataclass
class LaunchResult:
    started: bool
    pid: int | None
    message: str
    log: Path


def running_engine(ctx: ProjectContext) -> tuple[int | None, str]:
    pid = lock_holder(ctx.app.cfg.home / "engine.lock")
    if pid is not None:
        return pid, f"the workforce for {ctx.name} is already running (pid {pid or 'unknown'}); not starting another"
    sup = lock_holder(ctx.central.cfg.home / "supervisor.lock")
    if sup is not None:
        return sup, (f"the multi-project engine is already running (pid {sup or 'unknown'}) and schedules "
                     f"{ctx.name}; not starting another")
    return None, ""


def running_supervisor(reg: ProjectRegistry) -> int | None:
    sup = lock_holder(reg.central.cfg.home / "supervisor.lock")
    if sup is not None:
        return sup
    for c in reg.contexts():
        pid = lock_holder(c.app.cfg.home / "engine.lock")
        if pid is not None:
            return pid
    return None


def _spawn(args: list[str], log: Path) -> tuple[subprocess.Popen, int]:
    log.parent.mkdir(parents=True, exist_ok=True)
    offset = log.stat().st_size if log.exists() else 0
    out = open(log, "a")
    proc = subprocess.Popen(args, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    return proc, offset


def _await_start(proc: subprocess.Popen, lock: Path, log: Path, offset: int, label: str,
                 wait_s: float) -> LaunchResult:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if lock_holder(lock) == proc.pid:
            return LaunchResult(True, proc.pid, f"started the workforce for {label} in the background "
                                                f"(pid {proc.pid}); log: {log}", log)
        rc = proc.poll()
        if rc is not None:
            tail = log.read_text(errors="replace")[offset:].strip().splitlines()
            outcome = next((line for line in reversed(tail) if line.strip()), f"exit code {rc}")
            if rc == 3:
                return LaunchResult(False, None, f"another engine took the lock first; not started ({outcome})", log)
            return LaunchResult(rc == 0, proc.pid, f"the workforce for {label} ran and stopped: {outcome}", log)
        time.sleep(0.1)
    return LaunchResult(True, proc.pid, f"started the workforce for {label} (pid {proc.pid}); log: {log}", log)


def launch_project(ctx: ProjectContext, config: Path | None = None, unattended: bool = False,
                   wait_s: float = 10.0) -> LaunchResult:
    log = ctx.app.cfg.log_dir / "engine.out.log"
    pid, msg = running_engine(ctx)
    if pid is not None:
        return LaunchResult(False, pid, msg, log)
    args = [sys.executable, "-m", "workforce.cli", "--project", str(ctx.repo), "run", "--until-idle"]
    if unattended:
        args.append("--unattended")
    if config:
        args += ["--config", str(Path(config).resolve())]
    proc, offset = _spawn(args, log)
    return _await_start(proc, ctx.app.cfg.home / "engine.lock", log, offset, ctx.name, wait_s)


def launch_all(reg: ProjectRegistry, config: Path | None = None, unattended: bool = False,
               wait_s: float = 10.0) -> LaunchResult:
    log = reg.central.cfg.log_dir / "engine.out.log"
    pid = running_supervisor(reg)
    if pid is not None:
        return LaunchResult(False, pid, f"a workforce engine is already running (pid {pid or 'unknown'}); "
                                        "not starting the multi-project engine", log)
    args = [sys.executable, "-m", "workforce.cli", "run", "--all-projects", "--until-idle"]
    if unattended:
        args.append("--unattended")
    if config:
        args += ["--config", str(Path(config).resolve())]
    proc, offset = _spawn(args, log)
    return _await_start(proc, reg.central.cfg.home / "supervisor.lock", log, offset, "all projects", wait_s)
