"""Local control API (FastAPI). Every response is derived from the live state
database or git; nothing is simulated. Bound to loopback only.

Multi-project mode (`registry` given, used by `workforce serve`): the `project`
query parameter is the central registry id and every project-scoped request is
answered from that project's own state database. Without a registry the API
serves a single state database and `project` is the project id inside it.

CSRF/DNS-rebinding defences: Host header must be loopback; state-changing
requests require JSON content type, the X-Workforce-Client header (forces a
CORS preflight that this server never approves) and, if present, a loopback
Origin."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from workforce.app import App
from workforce.config import DEVELOPER_ROLES
from workforce.db import models as m
from workforce.docs.traceability import build_matrix
from workforce.engine import control
from workforce.engine.state import TaskState, transition
from workforce.reporting import usage_summary
from workforce.security.redaction import redact, redact_obj

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}
DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "dashboard" / "out"


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


class Decision(BaseModel):
    approve: bool
    comment: str | None = None


@dataclass
class Scope:
    app: App
    s: Session
    pid: int | None  # project id inside app.db (None: no project selected)
    repo: Path | None
    name: str | None


def create_api(wf: App, static_dir: Path | None = DASHBOARD_DIR, registry=None) -> FastAPI:
    api = FastAPI(title="Coding Workforce control API", version="0.2.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @api.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in LOOPBACK:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-workforce-client") != "dashboard":
                return JSONResponse({"detail": "missing X-Workforce-Client header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and (urlparse(origin).hostname or "") not in LOOPBACK:
                return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
            if "application/json" not in (request.headers.get("content-type") or ""):
                return JSONResponse({"detail": "JSON body required"}, status_code=415)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _ctx(project: int):
        entry = next((e for e in registry.entries() if e.id == project), None)
        if entry is None:
            raise HTTPException(404, f"unknown project {project}")
        return registry.context(entry)

    def scoped(project: int | None = None):
        """Resolve the state database a request is about."""
        if registry is not None and project is not None:
            c = _ctx(project)
            with c.app.db.session() as s:
                yield Scope(c.app, s, c.project_id, c.repo, c.name)
            return
        with wf.db.session() as s:
            p = s.get(m.Project, project) if project is not None else None
            if project is not None and p is None:
                raise HTTPException(404, f"unknown project {project}")
            yield Scope(wf, s, project, Path(p.repo_path) if p else None, p.name if p else None)

    def central():
        with wf.db.session() as s:
            yield s

    def _alive(s: Session) -> tuple[str, str | None, bool]:
        hb = control.get_flag(s, "heartbeat")
        state = control.get_flag(s, "run_state", "stopped")
        alive = bool(hb) and state != "stopped" and (
            datetime.now(UTC) - datetime.fromisoformat(hb)).total_seconds() < max(30, wf.cfg.limits.poll_interval_s * 10)
        return state, hb, alive

    def _projects() -> list[dict]:
        if registry is None:
            with wf.db.session() as s:
                return [{"id": p.id, "name": p.name, "status": p.status, "repo": p.repo_path}
                        for p in s.scalars(select(m.Project))]
        out = []
        for c in registry.contexts():
            with c.app.db.session() as s:
                p = s.get(m.Project, c.project_id)
                out.append({"id": c.entry_id, "name": c.name, "status": p.status, "repo": str(c.repo),
                            "state_dir": str(c.home)})
        return out

    # ---- read endpoints ------------------------------------------------
    @api.get("/api/overview")
    def overview(project: int | None = None, sc: Scope = Depends(scoped), cs: Session = Depends(central)):
        state, hb, alive = _alive(sc.s if sc.pid is not None else cs)
        if registry is not None and sc.pid is not None:
            eng_state, eng_hb, eng_alive = _alive(cs)
            if eng_alive and not alive:  # project driven by the multi-project daemon
                state, hb, alive = eng_state, eng_hb, eng_alive
        q = select(m.Task.state, func.count()).group_by(m.Task.state)
        if sc.pid is not None:
            q = q.where(m.Task.project_id == sc.pid)
        counts = dict(sc.s.execute(q).all())
        safety = control.get_flag(cs, "safety_check")
        pending_q = select(func.count()).select_from(m.Approval).where(m.Approval.status == "pending")
        return {
            "run_state": state, "heartbeat": hb, "engine_alive": alive,
            "mode": sc.app.cfg.mode, "task_counts": counts, "total_tasks": sum(counts.values()),
            "pending_approvals": sc.s.scalar(pending_q),
            "safety_check": json.loads(safety) if safety else None,
            "projects": _projects(), "multi_project": registry is not None,
        }

    @api.get("/api/agents")
    def agents(project: int | None = None, sc: Scope = Depends(scoped), cs: Session = Depends(central)):
        status = {r.role: r for r in cs.scalars(select(m.RoleStatus))}
        breakers = {b.provider: b for b in cs.scalars(select(m.ProviderState))}
        s = sc.s
        out = []
        for role, rc in wf.cfg.roles.items():
            st = status.get(role.value)
            br = breakers.get(rc.provider)
            ex_q = select(m.Execution).where(m.Execution.role == role.value)
            last = s.scalars(ex_q.order_by(m.Execution.id.desc())).first()
            current = None
            if role in DEVELOPER_ROLES:
                t = s.scalars(select(m.Task).where(m.Task.assigned_role == role.value,
                                                   m.Task.state.in_(["ASSIGNED", "IMPLEMENTING", "NEEDS_FIX"]))).first()
                current = t.key if t else None
            running = s.scalars(select(m.Execution).where(m.Execution.role == role.value,
                                                          m.Execution.status == "running")).first()
            calls = s.scalar(select(func.count()).select_from(m.Execution).where(m.Execution.role == role.value))
            out.append({
                "role": role.value, "desired_model": rc.desired_model, "plan": rc.plan, "provider": rc.provider,
                "model": rc.model, "reasoning_effort": rc.reasoning_effort, "enabled": rc.enabled,
                "override_reason": rc.override_reason,
                "verified": None if st is None else st.available, "verified_at": _iso(st.verified_at) if st else None,
                "detail": st.detail if st else "never verified",
                "breaker": br.breaker if br else "closed", "breaker_until": _iso(br.open_until) if br else None,
                "busy": running is not None, "running_purpose": running.purpose if running else None,
                "current_task": current, "calls": calls,
                "last_execution": None if last is None else {"id": last.id, "status": last.status, "purpose": last.purpose,
                                                             "at": _iso(last.started_at), "error_kind": last.error_kind},
            })
        return out

    @api.get("/api/tasks")
    def tasks(project: int | None = None, sc: Scope = Depends(scoped)):
        q = select(m.Task).order_by(m.Task.id)
        if sc.pid is not None:
            q = q.where(m.Task.project_id == sc.pid)
        return [{"id": t.id, "project_id": t.project_id, "key": t.key, "title": t.title, "state": t.state,
                 "assigned_role": t.assigned_role, "preferred_role": t.preferred_role, "attempts": t.attempts,
                 "depends_on": t.depends_on, "requirement_keys": t.requirement_keys, "owned_paths": t.owned_paths,
                 "branch": t.branch, "head_commit": t.head_commit, "integrated_commit": t.integrated_commit,
                 "blocked_reason": t.blocked_reason, "updated_at": _iso(t.updated_at)} for t in sc.s.scalars(q)]

    @api.get("/api/tasks/{task_id}")
    def task_detail(task_id: int, project: int | None = None, sc: Scope = Depends(scoped)):
        s = sc.s
        t = s.get(m.Task, task_id)
        if t is None or (sc.pid is not None and t.project_id != sc.pid):
            raise HTTPException(404)
        return redact_obj({
            "id": t.id, "key": t.key, "title": t.title, "description": t.description, "state": t.state,
            "acceptance_criteria": t.acceptance_criteria, "feedback": t.feedback, "attempts": t.attempts,
            "events": [{"from": e.from_state, "to": e.to_state, "reason": e.reason, "ts": _iso(e.ts)} for e in t.events],
            "tests": [{"id": r.id, "kind": r.kind, "attempt": r.attempt, "passed": r.passed, "summary": r.summary,
                       "command": r.command, "commit": r.commit, "ts": _iso(r.ts), "output_tail": r.output[-4000:]}
                      for r in s.scalars(select(m.TestRun).where(m.TestRun.task_id == t.id).order_by(m.TestRun.id))],
            "reviews": [{"verdict": r.verdict, "summary": r.summary, "findings": r.findings, "attempt": r.attempt,
                         "ts": _iso(r.ts)} for r in s.scalars(select(m.Review).where(m.Review.task_id == t.id).order_by(m.Review.id))],
            "commits": [{"sha": c.sha, "branch": c.branch, "role": c.role, "message": c.message, "files": c.files,
                         "ts": _iso(c.ts)} for c in s.scalars(select(m.Commit).where(m.Commit.task_id == t.id).order_by(m.Commit.id))],
            "executions": [{"id": e.id, "role": e.role, "purpose": e.purpose, "status": e.status, "model": e.model,
                            "duration_s": e.duration_s, "error_kind": e.error_kind, "ts": _iso(e.started_at)}
                           for e in s.scalars(select(m.Execution).where(m.Execution.task_id == t.id).order_by(m.Execution.id))],
        })

    @api.get("/api/executions")
    def executions(limit: int = 100, project: int | None = None, sc: Scope = Depends(scoped)):
        rows = sc.s.scalars(select(m.Execution).order_by(m.Execution.id.desc()).limit(min(limit, 500)))
        return [{"id": e.id, "task_id": e.task_id, "role": e.role, "purpose": e.purpose, "provider": e.provider,
                 "model": e.model, "status": e.status, "error_kind": e.error_kind,
                 "error": redact(e.error or "")[:500], "duration_s": e.duration_s, "usage": e.usage,
                 "started_at": _iso(e.started_at)} for e in rows]

    @api.get("/api/errors")
    def errors(project: int | None = None, sc: Scope = Depends(scoped), cs: Session = Depends(central)):
        failed = sc.s.scalars(select(m.Execution).where(m.Execution.status.in_(["failed", "timeout", "interrupted"]))
                              .order_by(m.Execution.id.desc()).limit(100))
        return {
            "providers": [{"provider": p.provider, "breaker": p.breaker, "consecutive_failures": p.consecutive_failures,
                           "open_until": _iso(p.open_until), "last_error": redact(p.last_error or "")}
                          for p in cs.scalars(select(m.ProviderState))],
            "failures": [{"id": e.id, "role": e.role, "provider": e.provider, "status": e.status,
                          "error_kind": e.error_kind, "error": redact(e.error or "")[:400],
                          "at": _iso(e.started_at)} for e in failed],
        }

    @api.get("/api/usage")
    def usage(project: int | None = None, sc: Scope = Depends(scoped)):
        return {"note": "Token counts are as reported by each CLI. Costs are list-price estimates reported by the CLI, "
                        "not subscription charges; subscription quotas are not visible to the workforce.",
                "rows": usage_summary(sc.s, sc.pid)}

    @api.get("/api/approvals")
    def approvals(status: str | None = "pending", project: int | None = None, sc: Scope = Depends(scoped)):
        q = select(m.Approval).order_by(m.Approval.id.desc())
        if status and status != "all":
            q = q.where(m.Approval.status == status)
        return [{"id": a.id, "kind": a.kind, "subject": a.subject, "status": a.status, "project_id": a.project_id,
                 "task_id": a.task_id, "payload": redact_obj(a.payload), "created_at": _iso(a.created_at),
                 "decided_by": a.decided_by, "comment": a.comment} for a in sc.s.scalars(q.limit(200))]

    def _need_project(sc: Scope) -> int:
        if sc.pid is None:
            raise HTTPException(400, "project query parameter required")
        return sc.pid

    @api.get("/api/traceability")
    def traceability(project: int, sc: Scope = Depends(scoped)):
        return build_matrix(sc.s, _need_project(sc))

    @api.get("/api/requirements")
    def requirements(project: int, sc: Scope = Depends(scoped)):
        pid = _need_project(sc)
        return {
            "requirements": [{"key": r.key, "text": r.text, "status": r.status, "category": r.category,
                              "priority": r.priority, "acceptance_criteria": r.acceptance_criteria}
                             for r in sc.s.scalars(select(m.Requirement).where(m.Requirement.project_id == pid))],
            "issues": [{"id": i.id, "kind": i.kind, "severity": i.severity, "status": i.status,
                        "requirement_keys": i.requirement_keys, "description": i.description}
                       for i in sc.s.scalars(select(m.Issue).where(m.Issue.project_id == pid))],
        }

    @api.get("/api/git")
    def git_state(project: int, sc: Scope = Depends(scoped)):
        from workforce.gitops.repo import GitRepo

        _need_project(sc)
        g = GitRepo(sc.repo, sc.app.cfg.policy)
        prefix = f"refs/heads/wf/{sc.name}/"
        branches = []
        for line in g.out("for-each-ref", "--format=%(refname:short)\x1f%(objectname)\x1f%(committerdate:iso-strict)",
                          prefix).splitlines():
            name, sha, date = line.split("\x1f")
            branches.append({"name": name, "sha": sha, "date": date})
        integ = f"wf/{sc.name}/integration"
        return {"branches": branches, "integration_branch": integ, "target_branch": sc.app.cfg.git.target_branch,
                "commits": g.log(integ, 40) if g.branch_exists(integ) else [],
                "push_authorized": sc.app.cfg.git.push_authorized}

    @api.get("/api/logs")
    def logs(limit: int = 200, project: int | None = None, sc: Scope = Depends(scoped)):
        audit = [{"ts": _iso(a.ts), "actor": a.actor, "action": a.action, "task_id": a.task_id,
                  "details": redact_obj(a.details)}
                 for a in sc.s.scalars(select(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(min(limit, 1000)))]
        system = []
        f = sc.app.cfg.log_dir / "workforce.jsonl"
        if f.exists():
            with f.open() as fh:
                for line in deque(fh, maxlen=min(limit, 1000)):
                    try:
                        system.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return {"audit": audit, "system": list(reversed(system))}

    # ---- control endpoints -------------------------------------------
    @api.post("/api/control/{action}")
    def control_action(action: str, project: int | None = None, sc: Scope = Depends(scoped)):
        mapping = {"pause": "paused", "resume": "running", "stop": "stopping"}
        if action not in mapping:
            raise HTTPException(400, "action must be pause, resume or stop")
        control.request_run_state(sc.s, mapping[action], "human(dashboard)")
        return {"run_state": mapping[action], "scope": sc.name or "engine"}

    @api.post("/api/approvals/{approval_id}/decide")
    def decide(approval_id: int, d: Decision, project: int | None = None):
        from workforce.engine.orchestrator import approve

        target = _ctx(project).app if registry is not None and project is not None else wf
        try:
            a = approve(target, approval_id, d.approve, "human(dashboard)", d.comment)
        except (KeyError, ValueError) as e:
            raise HTTPException(409, str(e))
        return {"id": a.id, "status": a.status}

    @api.post("/api/tasks/{task_id}/{action}")
    def task_action(task_id: int, action: str, project: int | None = None, sc: Scope = Depends(scoped)):
        s = sc.s
        t = s.get(m.Task, task_id)
        if t is None or (sc.pid is not None and t.project_id != sc.pid):
            raise HTTPException(404)
        if action == "retry":
            if t.state not in ("BLOCKED", "FAILED"):
                raise HTTPException(409, f"task is {t.state}")
            t.attempts, t.failure_signatures, t.assigned_role = 0, [], None
            transition(s, t, TaskState.READY, "human retry (dashboard)", actor="human(dashboard)")
        elif action in ("cancel", "pause"):
            control.set_flag(s, f"task_hold:{t.id}", action)
            control.audit(s, "human(dashboard)", f"task.{action}_requested", {"key": t.key}, t.project_id, t.id)
        elif action == "resume":
            if t.state != "PAUSED":
                raise HTTPException(409, "task is not paused")
            transition(s, t, t.paused_from or "READY", "resumed (dashboard)", actor="human(dashboard)")
        else:
            raise HTTPException(400, "unknown action")
        return {"ok": True, "state": t.state}

    if static_dir and static_dir.exists():
        api.mount("/_next", StaticFiles(directory=static_dir / "_next"), name="next")

        @api.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            f = (static_dir / path).resolve()
            if path and f.is_file() and static_dir.resolve() in f.parents:
                return FileResponse(f)
            html = static_dir / f"{path}.html"
            if path and html.is_file():
                return FileResponse(html)
            return FileResponse(static_dir / "index.html")

    return api


def serve(wf: App, host: str | None = None, port: int | None = None, registry=None) -> None:
    import uvicorn

    host = host or wf.cfg.dashboard.host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the control API may only bind to loopback")
    uvicorn.run(create_api(wf, registry=registry), host=host, port=port or wf.cfg.dashboard.port, log_level="warning")
