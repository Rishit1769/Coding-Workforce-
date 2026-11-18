"""Main orchestration loop."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from workforce.agents.roles import Agents
from workforce.app import App
from workforce.config import DEVELOPER_ROLES, Role
from workforce.db.models import Approval, Decision, Project, Task, TestRun
from workforce.engine import control
from workforce.engine.planning import approve_architecture
from workforce.engine.pipeline import EngineContext, StepOutcome, TaskPipeline
from workforce.engine.recovery import recover_project
from workforce.engine.scheduler import plan_assignments, promote_ready
from workforce.engine.state import ACTIVE, TaskState, transition
from workforce.engine.testing import run_checked
from workforce.logging import get_logger
from workforce.providers.base import ProviderAdapter
from workforce.providers.registry import ProviderRegistry, mark_interrupted_executions

log = get_logger("orchestrator")


class EngineLocked(Exception):
    pass


@dataclass
class RunSummary:
    reason: str
    completed: list[str]
    blocked: list[str]
    waiting: list[str]


class Orchestrator:
    def __init__(self, app: App, adapters: dict[str, ProviderAdapter] | None = None, actor: str = "engine"):
        self.app = app
        self.cfg = app.cfg
        self.db = app.db
        self.registry = ProviderRegistry(self.cfg, self.db, adapters)
        self.agents = Agents(self.cfg, self.registry)
        self.ctx = EngineContext(self.cfg, self.db, self.registry, self.agents)
        self.running: dict[int, asyncio.Task] = {}
        self._lock_fd = None
        self._manager_cooldown = 0.0

    # ---- lifecycle -------------------------------------------------------
    def _acquire_lock(self) -> None:
        path = self.cfg.home / "engine.lock"
        self._lock_fd = open(path, "w")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EngineLocked("another orchestrator instance is running")
        self._lock_fd.write(str(os.getpid()))
        self._lock_fd.flush()

    def _release_lock(self) -> None:
        if self._lock_fd:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()
            self._lock_fd = None

    def _flag(self, key: str, default=None):
        with self.db.session() as s:
            return control.get_flag(s, key, default)

    def _set(self, key: str, value: str) -> None:
        with self.db.session() as s:
            control.set_flag(s, key, value)

    def recover(self) -> list[dict]:
        actions = []
        n = mark_interrupted_executions(self.db)
        with self.db.session() as s:
            for p in s.scalars(select(Project).where(Project.status.in_(["executing", "completed"]))):
                actions += recover_project(s, p, self.ctx.git(p), self.ctx.integration_branch(p))
            if n:
                control.audit(s, "engine", "recovery.executions_interrupted", {"count": n})
        return actions

    def _ensure_integration(self, p: Project) -> None:
        git = self.ctx.git(p)
        branch = self.ctx.integration_branch(p)
        if git.branch_exists(branch):
            return
        git.ensure_branch(branch, self.cfg.git.target_branch if git.branch_exists(self.cfg.git.target_branch) else "HEAD")
        iwt = git.add_worktree(self.ctx.wt(p, "_integration"), branch)
        # Record approved ADRs in the repository for traceability.
        with self.db.session() as s:
            decs = s.scalars(select(Decision).where(Decision.project_id == p.id, Decision.status == "approved")).all()
            adr_dir = iwt / "docs" / "adr"
            for d in decs:
                adr_dir.mkdir(parents=True, exist_ok=True)
                (adr_dir / f"{d.key}.md").write_text(
                    f"# {d.key}: {d.title}\n\nStatus: approved\nRequirements: {', '.join(d.requirement_keys)}\n\n"
                    f"## Context\n{d.context}\n\n## Decision\n{d.decision}\n\n## Consequences\n{d.consequences}\n")
        git.commit_all(iwt, f"docs({p.name}): approved architecture decision records", "architect",
                       self.cfg.role(Role.ARCHITECT).model, {"Workforce-Project": p.name})

    # ---- main loop -------------------------------------------------------
    async def run(self, until_idle: bool = True, max_seconds: float | None = None) -> RunSummary:
        self._acquire_lock()
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: (self._set("run_state", "stopping"), stop_event.set()))
            except (NotImplementedError, RuntimeError):
                pass
        try:
            actions = self.recover()
            if actions:
                log.warning("recovery actions", extra={"actions": actions})
            if self._flag("run_state", "stopped") in ("stopped", "stopping", None):
                self._set("run_state", "running")
            self._set("engine_pid", str(os.getpid()))
            started = time.monotonic()
            reason = "stopped"
            while True:
                with self.db.session() as s:
                    control.heartbeat(s)
                state = self._flag("run_state", "running")
                if state in ("stopping", "stopped"):
                    reason = "stop requested"
                    break
                if max_seconds and time.monotonic() - started > max_seconds:
                    reason = "time limit"
                    break
                self._reap()
                self._apply_holds()
                if state == "paused":
                    await asyncio.sleep(self.cfg.limits.poll_interval_s)
                    continue
                launched = await self._schedule()
                if until_idle and not self.running and not launched and not self._has_actionable():
                    reason = self._finalize_idle()
                    break
                await asyncio.sleep(self.cfg.limits.poll_interval_s)
            await self._shutdown()
            return self._summary(reason)
        finally:
            for fut in self.running.values():
                fut.cancel()
            if self.running:
                await asyncio.gather(*self.running.values(), return_exceptions=True)
                self.running.clear()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            self._set("run_state", "stopped")
            self._release_lock()

    async def _shutdown(self) -> None:
        """Graceful stop: pipelines stop at the next step boundary; cancel after the grace period."""
        grace_s = self.cfg.limits.shutdown_grace_s
        if not self.running:
            return
        done, pending = await asyncio.wait(list(self.running.values()), timeout=grace_s)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._reap()

    def _reap(self) -> None:
        for tid, fut in list(self.running.items()):
            if fut.done():
                del self.running[tid]
                exc = fut.exception() if not fut.cancelled() else None
                if exc:
                    log.error("pipeline crashed", extra={"task_id": tid, "error": repr(exc)})
                    with self.db.session() as s:
                        t = s.get(Task, tid)
                        if t.state not in ("COMPLETED", "CANCELLED", "BLOCKED"):
                            transition(s, t, TaskState.BLOCKED, f"engine error: {exc!r}"[:1000])

    def _apply_holds(self) -> None:
        with self.db.session() as s:
            for tid, fut in list(self.running.items()):
                if control.get_flag(s, f"task_hold:{tid}") == "cancel":
                    fut.cancel()
            for c in s.scalars(select(Task).where(Task.state.notin_(["COMPLETED", "CANCELLED"]))):
                hold = control.get_flag(s, f"task_hold:{c.id}")
                if hold == "cancel" and c.id not in self.running:
                    transition(s, c, TaskState.CANCELLED, "cancelled by human")
                    control.set_flag(s, f"task_hold:{c.id}", "")
                elif hold == "pause" and c.id not in self.running and c.state != "PAUSED":
                    transition(s, c, TaskState.PAUSED, "paused by human")
                    control.set_flag(s, f"task_hold:{c.id}", "")

    def _launch(self, task_id: int) -> None:
        async def go():
            return await TaskPipeline(self.ctx, task_id).drive()
        self.running[task_id] = asyncio.create_task(go(), name=f"task-{task_id}")

    async def _schedule(self) -> int:
        launched = 0
        now = time.monotonic()
        with self.db.session() as s:
            projects = s.scalars(select(Project).where(Project.status == "executing")).all()
            s.expunge_all()
        for p in projects:
            self._ensure_integration(p)
            with self.db.session() as s:
                promote_ready(s, p.id)
                # resume active tasks that have no running pipeline (recovery, waits, approvals)
                for t in s.scalars(select(Task).where(Task.project_id == p.id,
                                                      Task.state.in_([x.value for x in ACTIVE] + ["NEEDS_FIX"]))):
                    if t.id in self.running or self.ctx.backoff.get(t.id, 0) > now:
                        continue
                    if t.state == "INTEGRATING" and self._awaiting_integration_approval(s, t):
                        continue
                    self._launch(t.id)
                    launched += 1
            devs = [r for r in DEVELOPER_ROLES if self.registry.role_available(r)[0]]
            if not devs:
                continue
            suggestion = await self._manager_suggestion(p, devs)
            with self.db.session() as s:
                for a in plan_assignments(s, p.id, devs, self.cfg.limits.max_dev_concurrency, suggestion):
                    t = s.scalars(select(Task).where(Task.project_id == p.id, Task.key == a.task_key)).one()
                    t.assigned_role = a.role.value
                    transition(s, t, TaskState.ASSIGNED, f"assigned to {a.role.value} ({a.reason})")
                    pending_launch = t.id
                    s.flush()
                    self._launch(pending_launch)
                    launched += 1
        return launched

    def _awaiting_integration_approval(self, s, t: Task) -> bool:
        if not self.cfg.require_integration_approval:
            return False
        st = control.approval_status(s, "integration", t.project_id, t.id)
        return st == "pending"

    async def _manager_suggestion(self, p: Project, devs: list[Role]) -> list[dict] | None:
        if time.monotonic() < self._manager_cooldown or not self.registry.role_available(Role.MANAGER)[0]:
            return None
        with self.db.session() as s:
            ready = [{"key": t.key, "title": t.title, "owned_paths": t.owned_paths, "preferred_role": t.preferred_role}
                     for t in s.scalars(select(Task).where(Task.project_id == p.id, Task.state == "READY"))]
            active = [{"key": t.key, "role": t.assigned_role, "owned_paths": t.owned_paths}
                      for t in s.scalars(select(Task).where(Task.project_id == p.id,
                                                            Task.state.in_([x.value for x in ACTIVE])))]
        free = [r.value for r in devs if r.value not in {a["role"] for a in active}]
        if len(ready) < 2 and len(free) < 2 or not ready or not free:
            return None  # no real choice to make: deterministic policy suffices
        out = await self.agents.manager(p.id, Path(p.repo_path), ready, free, active)
        if not out.ok:
            self._manager_cooldown = time.monotonic() + 300
            return None
        return out.data["assignments"]

    def _has_actionable(self) -> bool:
        now = time.monotonic()
        with self.db.session() as s:
            projects = s.scalars(select(Project).where(Project.status == "executing")).all()
            for p in projects:
                tasks = s.scalars(select(Task).where(Task.project_id == p.id)).all()
                devs = [r for r in DEVELOPER_ROLES if self.registry.role_available(r)[0]]
                if devs and plan_assignments(s, p.id, devs, self.cfg.limits.max_dev_concurrency):
                    return True
                for t in tasks:
                    if t.state in [x.value for x in ACTIVE] + ["NEEDS_FIX"]:
                        if t.state == "INTEGRATING" and self._awaiting_integration_approval(s, t):
                            continue
                        if self.ctx.backoff.get(t.id, 0) > now:
                            return True  # waiting on provider recovery, still actionable
                        return True
        return False

    def _finalize_idle(self) -> str:
        with self.db.session() as s:
            projects = s.scalars(select(Project).where(Project.status == "executing")).all()
            reasons = []
            for p in projects:
                tasks = s.scalars(select(Task).where(Task.project_id == p.id)).all()
                if tasks and all(t.state in ("COMPLETED", "CANCELLED") for t in tasks):
                    self._finalize_project(s, p)
                    reasons.append(f"{p.name}: all tasks completed")
                else:
                    blocked = [t.key for t in tasks if t.state in ("BLOCKED", "FAILED", "PAUSED")]
                    waiting = [a.subject for a in s.scalars(select(Approval).where(Approval.project_id == p.id,
                                                                                   Approval.status == "pending"))]
                    reasons.append(f"{p.name}: no actionable work (blocked={blocked}, pending approvals={len(waiting)})")
            control.audit(s, "engine", "engine.idle_stop", {"reasons": reasons})
        return "; ".join(reasons) or "no executing projects"

    def _finalize_project(self, s, p: Project) -> None:
        """All tasks done: final regression on integration branch + readiness report."""
        git = self.ctx.git(p)
        iwt = self.ctx.wt(p, "_integration")
        reg = run_checked(p.test_command, iwt, self.cfg.policy, self.cfg.limits.command_timeout_s)
        s.add(TestRun(project_id=p.id, kind="final_regression", command=reg.command, exit_code=reg.exit_code,
                      passed=reg.passed, summary=reg.summary(), output=reg.output, commit=git.rev("HEAD", cwd=iwt)))
        report = readiness_report(s, p, git, self.ctx.integration_branch(p), reg)
        out = self.cfg.home / "projects" / p.name / "readiness.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str))
        p.status = "completed" if reg.passed else "executing"
        control.request_approval(s, "promote", f"Promote {self.ctx.integration_branch(p)} into "
                                 f"{self.cfg.git.target_branch} (protected branch)", {"readiness": report},
                                 project_id=p.id)

    def _summary(self, reason: str) -> RunSummary:
        with self.db.session() as s:
            tasks = s.scalars(select(Task)).all()
            return RunSummary(reason, [t.key for t in tasks if t.state == "COMPLETED"],
                              [t.key for t in tasks if t.state == "BLOCKED"],
                              [t.key for t in tasks if t.state not in ("COMPLETED", "BLOCKED", "CANCELLED")])


def readiness_report(s, p: Project, git, integ_branch: str, reg) -> dict:
    tasks = s.scalars(select(Task).where(Task.project_id == p.id)).all()
    return {
        "project": p.name, "integration_branch": integ_branch, "head": git.rev(integ_branch),
        "final_regression": {"passed": reg.passed, "summary": reg.summary(), "command": reg.command},
        "tasks": {t.key: t.state for t in tasks},
        "migrations": "alembic.ini present" if (Path(p.repo_path) / "alembic.ini").exists() else "no migrations",
        "deployment": "local staging only; production deployment is never automatic",
        "ready": reg.passed and all(t.state == "COMPLETED" for t in tasks),
    }


def approve(app: App, approval_id: int, approve_: bool, actor: str, comment: str | None = None) -> Approval:
    """Human decision on an approval request, with kind-specific side effects."""
    with app.db.session() as s:
        a = control.decide_approval(s, approval_id, approve_, actor, comment)
        if a.kind == "architecture":
            p = s.get(Project, a.project_id)
            if approve_:
                approve_architecture(s, p, actor)
            else:
                p.status = "architecture_rejected"
        elif a.kind == "unattended" and approve_:
            control.set_flag(s, "unattended_approved", "1")
        return a
