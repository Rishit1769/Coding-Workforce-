"""Multi-project supervisor: the central engine/daemon process.

Runs one Orchestrator per registered project inside a single asyncio loop.
Every project keeps its own database, worktrees and lock. Provider
semaphores, hourly budgets, role availability, circuit breakers and the
global developer-concurrency cap are shared through a SharedRuntime. Projects
registered while the daemon runs are picked up on the next rescan."""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Callable

from workforce.engine import control
from workforce.engine.orchestrator import EngineLocked, Orchestrator, RunSummary
from workforce.engine.shared import SharedRuntime
from workforce.logging import get_logger
from workforce.projects import ProjectRegistry

log = get_logger("supervisor")


class SupervisorLocked(Exception):
    pass


@dataclass
class SupervisorResult:
    reason: str
    projects: dict[str, str] = field(default_factory=dict)  # name -> outcome


class Supervisor:
    def __init__(self, registry: ProjectRegistry, adapters_factory: Callable, rescan_s: float = 30.0,
                 only: list[str] | None = None, unattended: bool = False):
        self.registry = registry
        self.central = registry.central
        self.adapters_factory = adapters_factory
        self.rescan_s = rescan_s
        self.only = set(only) if only else None
        self.unattended = unattended
        self.tasks: dict[str, asyncio.Task] = {}
        self.outcomes: dict[str, str] = {}
        self._lock_fd = None

    def _lock(self) -> None:
        self._lock_fd = open(self.central.cfg.home / "supervisor.lock", "a+")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SupervisorLocked("another workforce supervisor is already running")
        # Opened without truncation: a failed attempt must not wipe the running engine's PID.
        self._lock_fd.seek(0)
        self._lock_fd.truncate()
        self._lock_fd.write(str(os.getpid()))
        self._lock_fd.flush()

    def _unlock(self) -> None:
        if self._lock_fd:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()
            self._lock_fd = None

    def _flag(self) -> str:
        with self.central.db.session() as s:
            return control.get_flag(s, "run_state", "running") or "running"

    def _set(self, value: str) -> None:
        with self.central.db.session() as s:
            control.set_flag(s, "run_state", value)

    def _start_new(self, shared: SharedRuntime, until_idle: bool, max_seconds: float | None) -> None:
        for ctx in self.registry.contexts():
            if self.only and ctx.name not in self.only:
                continue
            if ctx.name in self.tasks:
                continue  # each project gets exactly one orchestrator per supervisor run
            shared.add_project(ctx.app.db, ctx.repo)
            if self.unattended:
                ctx.app.cfg.mode = "unattended"
                ctx.app.cfg.require_integration_approval = False
            orch = Orchestrator(ctx.app, adapters=self.adapters_factory(ctx.app.cfg), shared=shared,
                                project_name=ctx.name, install_signals=False, follow_global_state=True)
            self.tasks[ctx.name] = asyncio.create_task(orch.run(until_idle=until_idle, max_seconds=max_seconds),
                                                       name=f"project-{ctx.name}")
            log.info("project orchestrator started", extra={"project_name": ctx.name})

    def _collect(self) -> None:
        for name, t in self.tasks.items():
            if not t.done() or name in self.outcomes:
                continue
            if t.cancelled():
                self.outcomes[name] = "cancelled"
            elif isinstance(t.exception(), EngineLocked):
                self.outcomes[name] = "skipped: already being run by another engine process"
            elif t.exception() is not None:
                self.outcomes[name] = f"error: {t.exception()!r}"
                log.error("project orchestrator crashed", extra={"project_name": name, "error": repr(t.exception())})
            else:
                r: RunSummary = t.result()
                self.outcomes[name] = r.reason

    async def run(self, until_idle: bool = True, max_seconds: float | None = None) -> SupervisorResult:
        self._lock()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: self._set("stopping"))
            except (NotImplementedError, RuntimeError):
                pass
        self._set("running")
        shared = SharedRuntime.from_registry(self.registry)
        started = time.monotonic()
        last_scan = -1e9
        reason = "all projects idle"
        try:
            while True:
                with self.central.db.session() as s:
                    control.heartbeat(s)
                if self._flag() in ("stopping", "stopped"):
                    reason = "stop requested"
                    break
                if max_seconds and time.monotonic() - started > max_seconds:
                    reason = "time limit"
                    self._set("stopping")
                    break
                if time.monotonic() - last_scan >= self.rescan_s or not self.tasks:
                    self._start_new(shared, until_idle, max_seconds)
                    last_scan = time.monotonic()
                self._collect()
                if until_idle and self.tasks and all(t.done() for t in self.tasks.values()):
                    break
                if until_idle and not self.tasks:
                    reason = "no registered projects"
                    break
                await asyncio.sleep(min(1.0, self.central.cfg.limits.poll_interval_s))
            if self.tasks:
                await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            self._collect()
            return SupervisorResult(reason, dict(self.outcomes))
        finally:
            for t in self.tasks.values():
                t.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            self._set("stopped")
            self._unlock()
