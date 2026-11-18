"""Per-task verification pipeline:

    ASSIGNED -> (Tester writes acceptance tests first) -> IMPLEMENTING (Developer)
    -> TESTING (engine runs independent acceptance + regression tests)
    -> REVIEWING (independent Reviewer verdict)
    -> INTEGRATING (DevOps: merge, conflict handling, regression) -> COMPLETED
    failures -> NEEDS_FIX -> bounded correction loop -> BLOCKED

Every step is idempotent and resumable from the persisted task state."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from workforce.agents.roles import Agents
from workforce.config import Role, WorkforceConfig
from workforce.db.models import Commit, Decision, Project, Requirement, Review, Task, TestRun
from workforce.db.session import Database
from workforce.engine import control
from workforce.engine.state import TaskState as S
from workforce.engine.state import transition
from workforce.engine.testing import CommandResult, run_checked
from workforce.gitops.repo import GitRepo
from workforce.logging import get_logger
from workforce.providers.base import ErrorKind
from workforce.providers.registry import ProviderRegistry
from workforce.security.policy import PolicyViolation, classify_diff

log = get_logger("pipeline")

WAIT_KINDS = {ErrorKind.CIRCUIT_OPEN, ErrorKind.USAGE_LIMIT, ErrorKind.BUDGET, ErrorKind.NETWORK}
UNAVAILABLE_KINDS = {ErrorKind.MODEL_UNAVAILABLE, ErrorKind.AUTH, ErrorKind.NOT_INSTALLED}


@dataclass
class EngineContext:
    cfg: WorkforceConfig
    db: Database
    registry: ProviderRegistry
    agents: Agents
    integration_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # task_id -> monotonic time before which the task should not be retried
    backoff: dict[int, float] = field(default_factory=dict)
    # engine-wide control database in multi-project runs (global pause/stop)
    global_db: Database | None = None

    def git(self, project: Project) -> GitRepo:
        return GitRepo(project.repo_path, self.cfg.policy)

    # naming -------------------------------------------------------------
    def integration_branch(self, project: Project) -> str:
        return f"wf/{project.name}/integration"

    def task_branch(self, project: Project, key: str) -> str:
        return f"wf/{project.name}/task/{key}"

    def accept_branch(self, project: Project, key: str) -> str:
        return f"wf/{project.name}/accept/{key}"

    def wt(self, project: Project, name: str) -> Path:
        return self.cfg.worktree_dir / project.name / name

    @staticmethod
    def accept_dir(key: str) -> str:
        return f"acceptance_tests/{key.lower().replace('-', '_')}"


class StepOutcome:
    PROGRESS = "progress"
    WAIT = "wait"      # cannot progress now (approval, provider wait); retry later
    STOP = "stop"      # terminal / paused / blocked


class TaskPipeline:
    def __init__(self, ctx: EngineContext, task_id: int):
        self.ctx = ctx
        self.task_id = task_id

    # ------------------------------------------------------------------
    def _load(self) -> tuple[Task, Project]:
        with self.ctx.db.session() as s:
            t = s.get(Task, self.task_id)
            p = s.get(Project, t.project_id)
            s.expunge_all()
            return t, p

    def _hold(self) -> str | None:
        with self.ctx.db.session() as s:
            return control.get_flag(s, f"task_hold:{self.task_id}")

    def _run_state(self) -> str:
        with self.ctx.db.session() as s:
            state = control.get_flag(s, "run_state", "running") or "running"
        if self.ctx.global_db is not None and state == "running":
            with self.ctx.global_db.session() as s:
                g = control.get_flag(s, "run_state", "running") or "running"
            if g in ("paused", "stopping"):
                return g
        return state

    def _move(self, to: S, reason: str, **fields) -> None:
        with self.ctx.db.session() as s:
            t = s.get(Task, self.task_id)
            for k, v in fields.items():
                setattr(t, k, v)
            transition(s, t, to, reason)

    def _update(self, **fields) -> None:
        with self.ctx.db.session() as s:
            t = s.get(Task, self.task_id)
            for k, v in fields.items():
                setattr(t, k, v)

    def _context(self, t: Task) -> tuple[dict, list[dict], list[dict]]:
        with self.ctx.db.session() as s:
            reqs = [{"key": r.key, "text": r.text, "acceptance_criteria": r.acceptance_criteria, "category": r.category}
                    for r in s.scalars(select(Requirement).where(Requirement.project_id == t.project_id,
                                                                 Requirement.key.in_(t.requirement_keys)))]
            decs = [{"key": d.key, "title": d.title, "decision": d.decision}
                    for d in s.scalars(select(Decision).where(Decision.project_id == t.project_id))
                    if not t.decision_keys or d.key in t.decision_keys]
        task = {"key": t.key, "title": t.title, "description": t.description, "owned_paths": t.owned_paths,
                "acceptance_criteria": t.acceptance_criteria, "requirement_keys": t.requirement_keys}
        return task, reqs, decs

    # ------------------------------------------------------------------
    async def drive(self) -> str:
        handlers = {S.ASSIGNED: self.prepare, S.IMPLEMENTING: self.implement, S.TESTING: self.test,
                    S.REVIEWING: self.review, S.INTEGRATING: self.integrate, S.NEEDS_FIX: self.correct}
        while True:
            t, _ = self._load()
            hold = self._hold()
            if hold == "pause" and t.state not in (S.PAUSED, S.COMPLETED, S.CANCELLED):
                self._move(S.PAUSED, "paused by human")
                with self.ctx.db.session() as s:
                    control.set_flag(s, f"task_hold:{self.task_id}", "")
                return StepOutcome.STOP
            if self._run_state() in ("paused", "stopping", "stopped"):
                return StepOutcome.WAIT
            if t.started_at and t.state in handlers:
                started = t.started_at if t.started_at.tzinfo else t.started_at.replace(tzinfo=UTC)
                if (datetime.now(UTC) - started).total_seconds() > self.ctx.cfg.limits.task_wall_clock_s:
                    self._move(S.BLOCKED, f"task wall-clock budget ({self.ctx.cfg.limits.task_wall_clock_s}s) exceeded")
                    return StepOutcome.STOP
            handler = handlers.get(S(t.state))
            if handler is None:
                return StepOutcome.STOP
            outcome = await handler()
            if outcome != StepOutcome.PROGRESS:
                return outcome

    def _provider_problem(self, out_error_kind: ErrorKind | None, what: str, err: str) -> str:
        """Common handling of provider failures that are not the developer's fault."""
        if out_error_kind in WAIT_KINDS:
            self.ctx.backoff[self.task_id] = time.monotonic() + self.ctx.cfg.limits.provider_wait_s
            with self.ctx.db.session() as s:
                control.audit(s, "engine", "task.waiting_provider", {"what": what, "error": err[:500]}, task_id=self.task_id)
            return StepOutcome.WAIT
        self._move(S.BLOCKED, f"{what} unavailable: {err[:500]}")
        return StepOutcome.STOP

    # ---- ASSIGNED: worktree + independent acceptance tests ---------------
    async def prepare(self) -> str:
        t, p = self._load()
        git = self.ctx.git(p)
        integ = self.ctx.integration_branch(p)
        base = git.rev(integ)
        branch = self.ctx.task_branch(p, t.key)
        wt = git.add_worktree(self.ctx.wt(p, t.key), branch, base)
        if not t.base_commit:
            self._update(branch=branch, worktree_path=str(wt), base_commit=base, head_commit=git.rev("HEAD", cwd=wt))
            t, p = self._load()
        if not t.acceptance_commit:
            ok, why = self.ctx.registry.role_available(Role.TESTER)
            if not ok:
                return self._provider_problem(ErrorKind.CIRCUIT_OPEN if why.startswith("circuit") else
                                              ErrorKind.MODEL_UNAVAILABLE, "tester", why)
            abranch = self.ctx.accept_branch(p, t.key)
            awt = git.add_worktree(self.ctx.wt(p, f"{t.key}-accept"), abranch, t.base_commit)
            git.reset_worktree(awt, "HEAD")
            task, reqs, decs = self._context(t)
            adir = self.ctx.accept_dir(t.key)
            out = await self.ctx.agents.tester(task, reqs, decs, awt, adir, t.id, t.project_id)
            if not out.ok:
                git.reset_worktree(awt, "HEAD")
                if out.result.error_kind in WAIT_KINDS | UNAVAILABLE_KINDS:
                    return self._provider_problem(out.result.error_kind, "tester", out.error)
                self._move(S.BLOCKED, f"tester could not produce acceptance tests: {out.error}")
                return StepOutcome.STOP
            changed = git.working_changes(awt)
            outside = [f for f in changed if not f.startswith(adir + "/")]
            tests = [f for f in changed if f.startswith(adir + "/") and Path(f).name.startswith("test_")]
            if outside or not tests:
                git.reset_worktree(awt, "HEAD")
                self._move(S.BLOCKED, f"tester output rejected by policy: files outside {adir}: {outside}; "
                                      f"test files: {tests}")
                return StepOutcome.STOP
            rc = self.ctx.cfg.role(Role.TESTER)
            sha = git.commit_all(awt, f"test({t.key}): independent acceptance tests for {', '.join(t.requirement_keys)}",
                                 "tester", rc.model, {"Workforce-Task": t.key,
                                                      "Workforce-Requirements": ",".join(t.requirement_keys)})
            self._record_commit(t, sha, abranch, "tester", f"acceptance tests for {t.key}", changed)
            git.remove_worktree(awt)
            self._update(acceptance_commit=sha)
        self._move(S.IMPLEMENTING, "acceptance tests ready; starting implementation")
        return StepOutcome.PROGRESS

    def _record_commit(self, t: Task, sha: str | None, branch: str, role: str, msg: str, files: list[str]) -> None:
        if not sha:
            return
        with self.ctx.db.session() as s:
            s.add(Commit(project_id=t.project_id, task_id=t.id, sha=sha, branch=branch, role=role, message=msg,
                         files=files))

    # ---- IMPLEMENTING -----------------------------------------------------
    async def implement(self) -> str:
        t, p = self._load()
        git = self.ctx.git(p)
        wt = Path(t.worktree_path)
        role = Role(t.assigned_role)
        ok, why = self.ctx.registry.role_available(role)
        if not ok:
            git.reset_worktree(wt, t.head_commit)
            self._move(S.READY, f"{role.value} unavailable ({why[:200]}); returning task to queue", assigned_role=None)
            return StepOutcome.STOP
        git.reset_worktree(wt, t.head_commit)
        integ_head = git.rev(self.ctx.integration_branch(p))
        if any(fb.get("type") == "conflict" for fb in t.feedback) and not git.is_ancestor(integ_head, t.head_commit):
            # Bring the task branch up to date with integration; leave markers for the developer.
            m = git.merge(wt, self.ctx.integration_branch(p), f"merge integration into {t.key}")
            if m.ok:
                self._update(head_commit=git.rev("HEAD", cwd=wt))
            self._update(base_commit=integ_head)
            t, p = self._load()
        # Only edits made by the agent from this point on are attributed to the developer.
        snap = git.snapshot(wt)
        task, reqs, decs = self._context(t)
        out = await self.ctx.agents.developer(role, task, reqs, decs, wt, t.feedback, p.test_command, t.id, t.project_id)
        if not out.ok:
            kind = out.result.error_kind
            if kind in WAIT_KINDS:
                git.reset_worktree(wt, t.head_commit)
                return self._provider_problem(kind, role.value, out.error)
            if kind in UNAVAILABLE_KINDS:
                git.reset_worktree(wt, t.head_commit)
                self._move(S.READY, f"{role.value} unavailable: {out.error[:300]}", assigned_role=None)
                return StepOutcome.STOP
            git.reset_worktree(wt, t.head_commit)
            return self._fail("developer", "agent_failure", f"developer run failed: {out.error}",
                              {"error_kind": str(kind)})
        changed = git.changed_since(wt, snap)
        cls = classify_diff(changed, list(t.owned_paths) + ["tests/**"], self.ctx.cfg.policy)
        if cls["protected"] or cls["secret"]:
            git.reset_worktree(wt, t.head_commit)
            return self._fail("policy", "protected_paths",
                              f"modified protected/secret files {cls['protected'] + cls['secret']}; changes discarded",
                              {"files": cls["protected"] + cls["secret"]})
        unmerged = git.out("diff", "--name-only", "--diff-filter=U", cwd=wt).splitlines()
        markers = git.has_conflict_markers(wt, sorted(set(unmerged) | set(changed)))
        if markers:
            git.reset_worktree(wt, t.head_commit)
            return self._fail("policy", "unresolved_conflict", f"conflict markers remain in {markers}",
                              {"files": markers})
        if cls["unowned"]:
            git.reset_worktree(wt, t.head_commit)
            return self._fail("policy", "architecture_violation",
                              f"modified files outside the task's owned paths {t.owned_paths}: {cls['unowned']}",
                              {"files": cls["unowned"]})
        rc = self.ctx.cfg.role(role)
        try:
            sha = git.commit_all(wt, f"feat({t.key}): {t.title}", role.value, rc.model,
                                 {"Workforce-Task": t.key, "Workforce-Requirements": ",".join(t.requirement_keys),
                                  "Workforce-Attempt": str(t.attempts)})
        except PolicyViolation as e:
            return self._fail("policy", "commit_rejected", str(e), {})
        if sha is None and git.rev("HEAD", cwd=wt) == t.base_commit:
            return self._fail("developer", "no_changes", "developer produced no changes", {})
        self._record_commit(t, sha, t.branch, role.value, f"{t.key} attempt {t.attempts}", changed)
        self._move(S.TESTING, f"implementation committed by {role.value} ({(sha or 'no new commit')[:10]})",
                   head_commit=git.rev("HEAD", cwd=wt))
        return StepOutcome.PROGRESS

    # ---- TESTING ----------------------------------------------------------
    async def test(self) -> str:
        t, p = self._load()
        git = self.ctx.git(p)
        vwt = git.add_detached_worktree(self.ctx.wt(p, f"{t.key}-verify"), t.head_commit)
        adir = self.ctx.accept_dir(t.key)
        git.git("checkout", t.acceptance_commit, "--", adir, cwd=vwt)
        limits = self.ctx.cfg.limits
        results: list[tuple[str, CommandResult]] = []
        acc = await asyncio.to_thread(run_checked, p.test_command, vwt, self.ctx.cfg.policy,
                                      limits.command_timeout_s, [adir])
        results.append(("acceptance", acc))
        if acc.passed:
            reg = await asyncio.to_thread(run_checked, p.test_command, vwt, self.ctx.cfg.policy,
                                          limits.command_timeout_s)
            results.append(("regression", reg))
            if reg.passed and (vwt / "alembic.ini").exists():
                mig = await asyncio.to_thread(run_checked, "alembic upgrade head", vwt, self.ctx.cfg.policy,
                                              limits.command_timeout_s)
                results.append(("migration", mig))
        with self.ctx.db.session() as s:
            for kind, r in results:
                s.add(TestRun(task_id=t.id, project_id=t.project_id, kind=kind, attempt=t.attempts, command=r.command,
                              exit_code=r.exit_code, passed=r.passed, summary=r.summary(), output=r.output,
                              commit=t.head_commit))
        failed = [(k, r) for k, r in results if not r.passed]
        if failed:
            kind, r = failed[0]
            detail = {"suite": kind, "summary": r.summary(), "failures": r.failures(), "output_tail": r.output[-3000:]}
            if self.ctx.registry.role_available(Role.WORKER)[0]:
                c = await self.ctx.agents.worker_classify(vwt, r.output, t.id, t.project_id)
                if c.ok:
                    detail["classification"] = c.data
            return self._fail("tester", "test_failure", f"{kind} tests failed: {r.summary()}", detail)
        self._move(S.REVIEWING, "; ".join(f"{k}: {r.summary()}" for k, r in results))
        return StepOutcome.PROGRESS

    # ---- REVIEWING --------------------------------------------------------
    async def review(self) -> str:
        t, p = self._load()
        git = self.ctx.git(p)
        vwt = self.ctx.wt(p, f"{t.key}-verify")
        if not vwt.exists():
            vwt = git.add_detached_worktree(vwt, t.head_commit)
            git.git("checkout", t.acceptance_commit, "--", self.ctx.accept_dir(t.key), cwd=vwt)
        diff = git.diff(t.base_commit, t.head_commit)
        changed = git.changed_files(t.base_commit, t.head_commit)
        policy = classify_diff(changed, list(t.owned_paths) + ["tests/**"], self.ctx.cfg.policy)
        with self.ctx.db.session() as s:
            runs = s.scalars(select(TestRun).where(TestRun.task_id == t.id, TestRun.commit == t.head_commit)).all()
            evidence = {r.kind: {"passed": r.passed, "summary": r.summary, "command": r.command} for r in runs}
        if not evidence or not all(v["passed"] for v in evidence.values()):
            # Engine rule: never review (or approve) work without passing verification evidence.
            self._move(S.TESTING, "no passing test evidence for current head; re-testing")
            return StepOutcome.PROGRESS
        task, reqs, decs = self._context(t)
        verdict = None
        for _ in range(2):
            out = await self.ctx.agents.reviewer(task, reqs, decs, vwt, diff, evidence, policy, t.id, t.project_id)
            if not out.ok:
                if out.result.error_kind in WAIT_KINDS | UNAVAILABLE_KINDS:
                    return self._provider_problem(out.result.error_kind, "reviewer", out.error)
                continue
            verdict = self._normalize_verdict(out.data, t)
            if verdict is not None:
                break
        if verdict is None:
            self._move(S.BLOCKED, "reviewer did not produce a valid, evidence-backed verdict")
            return StepOutcome.STOP
        with self.ctx.db.session() as s:
            s.add(Review(task_id=t.id, attempt=t.attempts, verdict=verdict["verdict"], summary=verdict["summary"],
                         findings=verdict["findings"], execution_id=None, commit=t.head_commit))
        if verdict["verdict"] == "APPROVED":
            self._move(S.INTEGRATING, f"reviewer approved: {verdict['summary'][:300]}")
            return StepOutcome.PROGRESS
        if verdict["verdict"] == "BLOCKED":
            with self.ctx.db.session() as s:
                control.request_approval(s, "review_blocked", f"{t.key} blocked by reviewer: {verdict['summary'][:300]}",
                                         {"findings": verdict["findings"]}, project_id=t.project_id, task_id=t.id)
            self._move(S.BLOCKED, f"reviewer BLOCKED: {verdict['summary'][:500]}")
            return StepOutcome.STOP
        return self._fail("reviewer", "changes_requested", verdict["summary"], {"findings": verdict["findings"]})

    @staticmethod
    def _normalize_verdict(data: dict, t: Task) -> dict | None:
        v = dict(data)
        findings = v.get("findings") or []
        if v["verdict"] in ("CHANGES_REQUESTED", "BLOCKED"):
            actionable = [f for f in findings if f.get("evidence") and f.get("requirement_keys")]
            if not actionable:
                return None  # rejections must carry evidence and requirement IDs
        if v["verdict"] == "APPROVED":
            unsatisfied = [c for c in v.get("requirement_coverage") or [] if not c.get("satisfied")]
            blocking = [f for f in findings if f.get("severity") in ("critical", "major")]
            if unsatisfied or blocking:
                # Inconsistent approval: the engine treats it as a change request.
                v["verdict"] = "CHANGES_REQUESTED"
                v["summary"] = "[engine] approval overridden: unsatisfied requirements or major findings. " + v["summary"]
                for c in unsatisfied:
                    findings.append({"severity": "major", "category": "acceptance_criteria",
                                     "requirement_keys": [c["requirement_key"]], "file": "", "line": 0,
                                     "evidence": c.get("evidence", ""), "recommendation": "satisfy requirement"})
                v["findings"] = findings
        return v

    # ---- NEEDS_FIX: bounded correction loop -------------------------------
    def _fail(self, source: str, ftype: str, message: str, detail: dict) -> str:
        t, _ = self._load()
        item = {"source": source, "type": ftype, "message": message[:2000], "attempt": t.attempts, **detail}
        self._move(S.NEEDS_FIX, f"[{source}] {message[:500]}", feedback=[*t.feedback, item])
        return StepOutcome.PROGRESS

    @staticmethod
    def signature(item: dict) -> str:
        core = {"source": item.get("source"), "type": item.get("type"),
                "failures": sorted(item.get("failures") or []),
                "files": sorted(item.get("files") or []),
                "findings": sorted(f"{f.get('category')}|{f.get('file')}|{','.join(f.get('requirement_keys') or [])}"
                                   for f in item.get("findings") or [])}
        if not core["failures"] and not core["files"] and not core["findings"]:
            core["message"] = item.get("message", "")[:300]
        return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]

    async def correct(self) -> str:
        t, _ = self._load()
        last = t.feedback[-1] if t.feedback else {}
        sig = self.signature(last)
        if sig in (t.failure_signatures or []):
            self._move(S.BLOCKED, f"repeated identical failure detected ({last.get('type')}); stopping correction loop "
                                  "early", failure_signatures=[*t.failure_signatures, sig])
            return StepOutcome.STOP
        if t.attempts >= self.ctx.cfg.limits.max_correction_attempts:
            self._move(S.BLOCKED, f"maximum automated correction attempts ({t.attempts}) exhausted; last failure: "
                                  f"{last.get('message', '')[:300]}", failure_signatures=[*t.failure_signatures, sig])
            return StepOutcome.STOP
        self._move(S.IMPLEMENTING, f"correction attempt {t.attempts + 1} with structured feedback from {last.get('source')}",
                   attempts=t.attempts + 1, failure_signatures=[*t.failure_signatures, sig])
        return StepOutcome.PROGRESS

    # ---- INTEGRATING ------------------------------------------------------
    async def integrate(self) -> str:
        t, p = self._load()
        cfg = self.ctx.cfg
        if cfg.require_integration_approval:
            with self.ctx.db.session() as s:
                st = control.approval_status(s, "integration", t.project_id, t.id)
                if st is None or st == "pending":
                    control.request_approval(s, "integration", f"Integrate {t.key}: {t.title}",
                                             {"head": t.head_commit, "branch": t.branch}, t.project_id, t.id)
                    return StepOutcome.WAIT
            if st == "rejected":
                self._move(S.BLOCKED, "integration rejected by human")
                return StepOutcome.STOP
        async with self.ctx.integration_lock:
            return await self._integrate_locked()

    async def _integrate_locked(self) -> str:
        t, p = self._load()
        git = self.ctx.git(p)
        integ_branch = self.ctx.integration_branch(p)
        if git.is_ancestor(t.head_commit, integ_branch) and git.is_ancestor(t.acceptance_commit, integ_branch):
            self._move(S.COMPLETED, "already integrated", integrated_commit=git.rev(integ_branch))
            return StepOutcome.STOP
        iwt = git.add_worktree(self.ctx.wt(p, "_integration"), integ_branch)
        pre = git.rev("HEAD", cwd=iwt)
        git.reset_worktree(iwt, pre)
        for ref, label in ((t.branch, "implementation"), (self.ctx.accept_branch(p, t.key), "acceptance tests")):
            m = git.merge(iwt, ref, f"merge({t.key}): {label} - {t.title}")
            if not m.ok:
                resolved = await self._resolve_conflict(git, iwt, m.conflicts, t)
                if not resolved:
                    git.reset_worktree(iwt, pre)
                    return self._fail("integration", "conflict",
                                      f"merge conflict with integration branch in {m.conflicts}; resolve conflict "
                                      "markers left in your worktree", {"files": m.conflicts})
        reg = await asyncio.to_thread(run_checked, p.test_command, iwt, self.ctx.cfg.policy,
                                      self.ctx.cfg.limits.command_timeout_s)
        head = git.rev("HEAD", cwd=iwt)
        with self.ctx.db.session() as s:
            s.add(TestRun(task_id=t.id, project_id=t.project_id, kind="integration_regression", attempt=t.attempts,
                          command=reg.command, exit_code=reg.exit_code, passed=reg.passed, summary=reg.summary(),
                          output=reg.output, commit=head))
        if not reg.passed:
            git.reset_worktree(iwt, pre)
            return self._fail("devops", "integration_regression", f"regression suite failed after merge: {reg.summary()}",
                              {"failures": reg.failures(), "output_tail": reg.output[-3000:]})
        self._record_commit(t, head, integ_branch, "devops", f"integrate {t.key}", git.changed_files(pre, head, cwd=iwt))
        for name in (t.key, f"{t.key}-verify"):
            git.remove_worktree(self.ctx.wt(p, name))
        self._move(S.COMPLETED, f"integrated into {integ_branch} at {head[:10]}; regression: {reg.summary()}",
                   integrated_commit=head, worktree_path=None)
        if self.ctx.cfg.git.push_authorized:
            self._push(git, integ_branch, t)
        return StepOutcome.STOP

    async def _resolve_conflict(self, git: GitRepo, iwt: Path, conflicts: list[str], t: Task) -> bool:
        if not self.ctx.registry.role_available(Role.DEVOPS)[0]:
            return False
        before = {f: (iwt / f).read_bytes() for f in git.out("ls-files", cwd=iwt).splitlines()
                  if f not in conflicts and (iwt / f).is_file()}
        out = await self.ctx.agents.devops_merge(iwt, conflicts, "integration branch",
                                                 f"{t.key}: {t.title}", t.id, t.project_id)
        if not out.ok or not out.data.get("safe"):
            return False
        if git.has_conflict_markers(iwt, conflicts):
            return False
        for f, content in before.items():
            if not (iwt / f).exists() or (iwt / f).read_bytes() != content:
                return False  # DevOps touched files outside the conflict set
        reg = await asyncio.to_thread(run_checked, self._project().test_command, iwt, self.ctx.cfg.policy,
                                      self.ctx.cfg.limits.command_timeout_s)
        if not reg.passed:
            return False
        git.conclude_merge(iwt)
        with self.ctx.db.session() as s:
            control.audit(s, "devops", "merge.conflict_resolved", {"files": conflicts, "summary": out.data.get("summary")},
                          project_id=t.project_id, task_id=t.id)
        return True

    def _project(self) -> Project:
        return self._load()[1]

    def _push(self, git: GitRepo, branch: str, t: Task) -> None:
        r = git.git("push", self.ctx.cfg.git.remote, branch, check=False)
        with self.ctx.db.session() as s:
            control.audit(s, "devops", "git.push", {"branch": branch, "ok": r.returncode == 0,
                                                    "output": (r.stderr or r.stdout)[-500:]},
                          project_id=t.project_id, task_id=t.id)
