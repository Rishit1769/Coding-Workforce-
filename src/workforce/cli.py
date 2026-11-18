"""`workforce` command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from workforce.app import App, build_app
from workforce.db import models as m
from workforce.engine import control

app = typer.Typer(help="Coding Workforce: documentation-driven multi-agent engineering.", no_args_is_help=True)
console = Console()

ConfigOpt = typer.Option(None, "--config", "-c", help="Path to workforce.yaml", envvar="WORKFORCE_CONFIG")


def _app(config: Optional[Path]) -> App:
    return build_app(config)


@app.command()
def init(path: Path = typer.Argument(Path("workforce.yaml"), help="Where to write the config")) -> None:
    """Create a workforce.yaml with the official role assignments and initialise the database."""
    from workforce.config import WorkforceConfig

    if not path.exists():
        cfg = WorkforceConfig()
        data = json.loads(cfg.model_dump_json(exclude={"home"}))
        data["home"] = ".workforce"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        console.print(f"wrote {path}")
    a = build_app(path)
    console.print(f"database ready at {a.cfg.db_path}")


@app.command("config")
def show_config(config: Optional[Path] = ConfigOpt) -> None:
    """Print the effective configuration."""
    a = _app(config)
    console.print_json(a.cfg.model_dump_json())


@app.command()
def status(config: Optional[Path] = ConfigOpt, as_json: bool = typer.Option(False, "--json")) -> None:
    """Show run state, task counts per state, roles and pending approvals."""
    a = _app(config)
    with a.db.session() as s:
        run_state = control.get_flag(s, "run_state", "stopped")
        hb = control.get_flag(s, "heartbeat")
        counts = dict(s.execute(select(m.Task.state, func.count()).group_by(m.Task.state)).all())
        roles = s.scalars(select(m.RoleStatus)).all()
        pending = s.scalars(select(m.Approval).where(m.Approval.status == "pending")).all()
        data = {
            "run_state": run_state, "heartbeat": hb, "tasks": counts,
            "roles": {r.role: {"model": r.model, "available": r.available, "detail": r.detail} for r in roles},
            "pending_approvals": [{"id": p.id, "kind": p.kind, "subject": p.subject} for p in pending],
        }
    if as_json:
        console.print_json(json.dumps(data, default=str))
        return
    console.print(f"[bold]run state:[/] {run_state}   [bold]heartbeat:[/] {hb or '-'}")
    t = Table("state", "count")
    for k, v in sorted(counts.items()):
        t.add_row(k, str(v))
    console.print(t)
    if roles:
        rt = Table("role", "model", "available", "detail")
        for r in roles:
            rt.add_row(r.role, r.model, "yes" if r.available else "[red]no[/]", r.detail[:80])
        console.print(rt)
    for p in pending:
        console.print(f"[yellow]approval #{p.id}[/] {p.kind}: {p.subject}")


models_app = typer.Typer(help="Model assignment verification.")
app.add_typer(models_app, name="models")


@models_app.command("verify")
def models_verify(config: Optional[Path] = ConfigOpt,
                  role: list[str] = typer.Option(None, "--role", help="Only verify these roles")) -> None:
    """Probe every role's CLI/model with a minimal prompt (consumes a small amount of subscription usage)."""
    import asyncio

    from workforce.config import Role
    from workforce.providers.probe import verify_roles
    from workforce.providers.registry import ProviderRegistry

    a = _app(config)
    reg = ProviderRegistry(a.cfg, a.db)
    report = asyncio.run(verify_roles(reg, [Role(r) for r in role] if role else None))
    t = Table("role", "desired", "provider", "model id", "cli", "available", "detail")
    for r in report:
        t.add_row(r["role"], r["desired_model"], r["provider"], r["model_id"], r["cli_version"] or "-",
                  "[green]yes[/]" if r["available"] else "[red]NO[/]", r["detail"][:90])
    console.print(t)
    console.print(f"report written to {a.cfg.artifact_dir / 'model_compatibility.json'}")


@models_app.command("list")
def models_list(config: Optional[Path] = ConfigOpt) -> None:
    """Show configured role assignments and last verification result."""
    a = _app(config)
    with a.db.session() as s:
        st = {r.role: r for r in s.scalars(select(m.RoleStatus))}
    t = Table("role", "desired", "provider", "model id", "verified", "detail")
    for role, rc in a.cfg.roles.items():
        r = st.get(role.value)
        t.add_row(role.value, rc.desired_model, rc.provider, rc.model,
                  "-" if r is None else ("[green]yes[/]" if r.available else "[red]no[/]"),
                  (r.detail[:80] if r else "never verified") + (f" [override: {rc.override_reason}]" if rc.override_reason else ""))
    console.print(t)


project_app = typer.Typer(help="Projects and documentation.")
app.add_typer(project_app, name="project")
req_app = typer.Typer(help="Requirements, issues and traceability.")
app.add_typer(req_app, name="req")


def _project(s, name: str) -> m.Project:
    p = s.scalars(select(m.Project).where(m.Project.name == name)).first()
    if p is None:
        raise typer.BadParameter(f"unknown project {name!r}")
    return p


@project_app.command("create")
def project_create(name: str, repo: Path = typer.Option(..., help="Target git repository the workforce builds in"),
                   docs: Path = typer.Option(..., help="Documentation file or directory"),
                   test_command: str = typer.Option("python -m pytest -q", help="Regression test command (allowlisted)"),
                   config: Optional[Path] = ConfigOpt) -> None:
    """Register a project and ingest its documentation."""
    from workforce.docs.ingest import ingest
    from workforce.gitops.repo import GitRepo
    from workforce.security.policy import check_command

    a = _app(config)
    check_command(test_command, a.cfg.policy)
    repo = repo.resolve()
    if not GitRepo(repo, a.cfg.policy).is_repo():
        raise typer.BadParameter(f"{repo} is not a git repository")
    with a.db.session() as s:
        p = m.Project(name=name, repo_path=str(repo), test_command=test_command, status="requirements_review")
        s.add(p)
        s.flush()
        stats = ingest(s, a.cfg, p, docs)
    console.print(f"project {name} created: {stats}")
    console.print("next: review `workforce req list`, resolve issues, then `workforce req approve`")


@project_app.command("ingest")
def project_ingest(name: str, docs: Path, config: Optional[Path] = ConfigOpt) -> None:
    """Ingest additional or updated documentation (changed requirements need re-approval)."""
    from workforce.docs.ingest import ingest

    a = _app(config)
    with a.db.session() as s:
        console.print(ingest(s, a.cfg, _project(s, name), docs))


@project_app.command("list")
def project_list(config: Optional[Path] = ConfigOpt) -> None:
    a = _app(config)
    with a.db.session() as s:
        t = Table("name", "status", "repo", "tasks")
        for p in s.scalars(select(m.Project)):
            n = s.scalar(select(func.count()).select_from(m.Task).where(m.Task.project_id == p.id))
            t.add_row(p.name, p.status, p.repo_path, str(n))
        console.print(t)


@req_app.command("list")
def req_list(project: str, config: Optional[Path] = ConfigOpt) -> None:
    a = _app(config)
    with a.db.session() as s:
        p = _project(s, project)
        t = Table("key", "status", "category", "prio", "text", "AC")
        for r in s.scalars(select(m.Requirement).where(m.Requirement.project_id == p.id).order_by(m.Requirement.id)):
            t.add_row(r.key, r.status, r.category, r.priority, r.text[:90], str(len(r.acceptance_criteria)))
        console.print(t)
        it = Table("issue", "kind", "severity", "status", "requirements", "description")
        for i in s.scalars(select(m.Issue).where(m.Issue.project_id == p.id)):
            it.add_row(str(i.id), i.kind, i.severity, i.status, ",".join(i.requirement_keys), i.description[:100])
        console.print(it)


@req_app.command("approve")
def req_approve(project: str, keys: list[str] = typer.Argument(None), config: Optional[Path] = ConfigOpt) -> None:
    """Approve proposed requirements (all, or the given keys). Blocked by open major issues."""
    from workforce.docs.ingest import approve_requirements

    a = _app(config)
    with a.db.session() as s:
        got = approve_requirements(s, _project(s, project).id, "human", keys or None)
    console.print(f"approved {len(got)} requirements")


@req_app.command("reject")
def req_reject(project: str, key: str, reason: str = typer.Option(...), config: Optional[Path] = ConfigOpt) -> None:
    from workforce.docs.ingest import reject_requirement

    a = _app(config)
    with a.db.session() as s:
        reject_requirement(s, _project(s, project).id, key, "human", reason)


@req_app.command("resolve")
def req_resolve(issue_id: int, resolution: str = typer.Option(...),
                amend: list[str] = typer.Option(None, help="KEY=new requirement text"),
                config: Optional[Path] = ConfigOpt) -> None:
    """Resolve an ambiguity/contradiction, optionally amending requirement text."""
    from workforce.docs.ingest import resolve_issue

    a = _app(config)
    amendments = dict(x.split("=", 1) for x in (amend or []))
    with a.db.session() as s:
        resolve_issue(s, issue_id, resolution, "human", amendments)
    console.print(f"issue {issue_id} resolved")


@req_app.command("trace")
def req_trace(project: str, fmt: str = typer.Option("markdown", "--format", help="markdown|csv|json"),
              config: Optional[Path] = ConfigOpt) -> None:
    """Print the requirement traceability matrix."""
    from workforce.docs.traceability import build_matrix, matrix_csv, matrix_markdown

    a = _app(config)
    with a.db.session() as s:
        rows = build_matrix(s, _project(s, project).id)
    if fmt == "json":
        console.print_json(json.dumps(rows))
    elif fmt == "csv":
        print(matrix_csv(rows))
    else:
        print(matrix_markdown(rows))


# ---------------------------------------------------------------------------
# Orchestration lifecycle
# ---------------------------------------------------------------------------

def build_adapters(cfg) -> dict:
    """Real CLI adapters, plus the scripted self-test adapter only if a role is explicitly configured for it."""
    import importlib
    import os

    from workforce.providers.registry import default_adapters

    adapters = default_adapters(cfg)
    if any(rc.provider == "scripted" for rc in cfg.roles.values()):
        spec = os.environ.get("WORKFORCE_SCRIPTED_FACTORY", "workforce.selftest.scenario:factory_from_env")
        mod, fn = spec.split(":")
        adapters["scripted"] = getattr(importlib.import_module(mod), fn)()
    return adapters


def _orchestrator(a: App):
    from workforce.engine.orchestrator import Orchestrator

    return Orchestrator(a, adapters=build_adapters(a.cfg))


@app.command()
def plan(project: str, feedback: str = typer.Option("", help="Guidance for re-planning"),
         config: Optional[Path] = ConfigOpt) -> None:
    """Run the Architect on approved requirements and request architecture approval."""
    import asyncio

    from workforce.engine.planning import PlanningError, run_architecture

    a = _app(config)
    orch = _orchestrator(a)
    with a.db.session() as s:
        pid = _project(s, project).id
    try:
        result = asyncio.run(run_architecture(a.db, a.cfg, orch.agents, pid, feedback))
    except PlanningError as e:
        console.print(f"[red]planning failed:[/] {e}")
        raise typer.Exit(1)
    console.print(f"architecture proposed: {len(result['decisions'])} decisions, {len(result['tasks'])} tasks")
    console.print(f"see {a.cfg.home / 'projects' / project / 'architecture.md'}; approve with `workforce approvals`")


@app.command("approvals")
def approvals_list(config: Optional[Path] = ConfigOpt, all_: bool = typer.Option(False, "--all")) -> None:
    """List approval requests (pending by default)."""
    a = _app(config)
    with a.db.session() as s:
        q = select(m.Approval).order_by(m.Approval.id)
        if not all_:
            q = q.where(m.Approval.status == "pending")
        t = Table("id", "kind", "status", "subject")
        for ap in s.scalars(q):
            t.add_row(str(ap.id), ap.kind, ap.status, ap.subject[:110])
        console.print(t)


@app.command("approve")
def approve_cmd(approval_id: int, comment: str = typer.Option(None), config: Optional[Path] = ConfigOpt) -> None:
    """Approve a request (architecture, integration, clarification, promote, ...)."""
    from workforce.engine.orchestrator import approve

    a = _app(config)
    ap = approve(a, approval_id, True, "human", comment)
    console.print(f"approved #{ap.id} ({ap.kind})")


@app.command("reject")
def reject_cmd(approval_id: int, comment: str = typer.Option(None), config: Optional[Path] = ConfigOpt) -> None:
    from workforce.engine.orchestrator import approve

    a = _app(config)
    ap = approve(a, approval_id, False, "human", comment)
    console.print(f"rejected #{ap.id} ({ap.kind})")


@app.command("run")
def run_cmd(config: Optional[Path] = ConfigOpt,
            until_idle: bool = typer.Option(True, "--until-idle/--forever",
                                            help="Stop automatically when no approved actionable work remains"),
            unattended: bool = typer.Option(False, help="Unattended mode (requires a passing safety check)"),
            max_seconds: float = typer.Option(None, help="Hard wall-clock limit")) -> None:
    """Run the orchestration engine in the foreground."""
    import asyncio

    from workforce.daemon import unattended_allowed
    from workforce.engine.orchestrator import EngineLocked

    a = _app(config)
    if unattended:
        with a.db.session() as s:
            ok, why = unattended_allowed(control.get_flag(s, "safety_check"))
        if not ok:
            console.print(f"[red]unattended mode refused:[/] {why}")
            raise typer.Exit(2)
        a.cfg.mode = "unattended"
        a.cfg.require_integration_approval = False
    orch = _orchestrator(a)
    try:
        summary = asyncio.run(orch.run(until_idle=until_idle, max_seconds=max_seconds))
    except EngineLocked as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(3)
    console.print(f"engine stopped: {summary.reason}")
    console.print(f"completed={summary.completed} blocked={summary.blocked} waiting={summary.waiting}")


@app.command()
def start(config: Optional[Path] = ConfigOpt, unattended: bool = typer.Option(False)) -> None:
    """Start the engine as a detached background process (logs in <home>/logs)."""
    import subprocess
    import sys

    a = _app(config)
    args = [sys.executable, "-m", "workforce.cli", "run", "--until-idle"]
    if unattended:
        args.append("--unattended")
    if config:
        args += ["--config", str(config.resolve())]
    out = open(a.cfg.log_dir / "engine.out.log", "a")
    proc = subprocess.Popen(args, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    console.print(f"engine started in background (pid {proc.pid})")


def _set_run_state(config: Optional[Path], state: str) -> None:
    a = _app(config)
    with a.db.session() as s:
        control.request_run_state(s, state, "human")
    console.print(f"run state -> {state}")


@app.command()
def pause(config: Optional[Path] = ConfigOpt) -> None:
    """Pause scheduling; in-flight steps finish, no new steps start."""
    _set_run_state(config, "paused")


@app.command()
def resume(config: Optional[Path] = ConfigOpt) -> None:
    """Resume a paused engine."""
    _set_run_state(config, "running")


@app.command()
def stop(config: Optional[Path] = ConfigOpt) -> None:
    """Gracefully stop the engine (in-flight steps get a grace period, then are cancelled)."""
    _set_run_state(config, "stopping")


task_app = typer.Typer(help="Inspect and control individual tasks.")
app.add_typer(task_app, name="task")


def _task(s, key: str, project: str | None) -> m.Task:
    q = select(m.Task).where(m.Task.key == key)
    if project:
        q = q.where(m.Task.project_id == _project(s, project).id)
    rows = s.scalars(q).all()
    if len(rows) != 1:
        raise typer.BadParameter(f"task {key} not found or ambiguous (use --project)")
    return rows[0]


@task_app.command("list")
def task_list(project: Optional[str] = typer.Argument(None), config: Optional[Path] = ConfigOpt) -> None:
    a = _app(config)
    with a.db.session() as s:
        q = select(m.Task).order_by(m.Task.id)
        if project:
            q = q.where(m.Task.project_id == _project(s, project).id)
        t = Table("key", "state", "role", "attempts", "deps", "requirements", "title", "blocked reason")
        for x in s.scalars(q):
            t.add_row(x.key, x.state, x.assigned_role or "-", str(x.attempts), ",".join(x.depends_on),
                      ",".join(x.requirement_keys), x.title[:40], (x.blocked_reason or "")[:60])
        console.print(t)


@task_app.command("show")
def task_show(key: str, project: Optional[str] = typer.Option(None), config: Optional[Path] = ConfigOpt) -> None:
    a = _app(config)
    with a.db.session() as s:
        t = _task(s, key, project)
        console.print(f"[bold]{t.key}[/] {t.title} — {t.state} (attempts {t.attempts})")
        console.print(t.description)
        for e in t.events:
            console.print(f"  {e.ts:%H:%M:%S} {e.from_state} -> {e.to_state}: {e.reason[:150]}")
        for fb in t.feedback:
            console.print(f"  [yellow]feedback[/] {fb.get('source')}/{fb.get('type')}: {fb.get('message', '')[:200]}")


@task_app.command("retry")
def task_retry(key: str, project: Optional[str] = typer.Option(None), config: Optional[Path] = ConfigOpt) -> None:
    """Retry a BLOCKED/FAILED task with a fresh correction budget."""
    from workforce.engine.state import transition

    a = _app(config)
    with a.db.session() as s:
        t = _task(s, key, project)
        if t.state not in ("BLOCKED", "FAILED"):
            raise typer.BadParameter(f"{key} is {t.state}, not BLOCKED/FAILED")
        t.attempts, t.failure_signatures, t.assigned_role = 0, [], None
        transition(s, t, "READY", "human retry", actor="human")
    console.print(f"{key} -> READY")


@task_app.command("cancel")
def task_cancel(key: str, project: Optional[str] = typer.Option(None), config: Optional[Path] = ConfigOpt) -> None:
    """Cancel a task (a running step is interrupted)."""
    a = _app(config)
    with a.db.session() as s:
        t = _task(s, key, project)
        control.set_flag(s, f"task_hold:{t.id}", "cancel")
        control.audit(s, "human", "task.cancel_requested", {"key": key}, t.project_id, t.id)
    console.print(f"cancel requested for {key} (applied by the engine; immediate if the engine is not running)")
    _apply_holds_if_idle(a)


@task_app.command("pause")
def task_pause(key: str, project: Optional[str] = typer.Option(None), config: Optional[Path] = ConfigOpt) -> None:
    a = _app(config)
    with a.db.session() as s:
        t = _task(s, key, project)
        control.set_flag(s, f"task_hold:{t.id}", "pause")
    console.print(f"pause requested for {key}")
    _apply_holds_if_idle(a)


@task_app.command("resume")
def task_resume(key: str, project: Optional[str] = typer.Option(None), config: Optional[Path] = ConfigOpt) -> None:
    from workforce.engine.state import transition

    a = _app(config)
    with a.db.session() as s:
        t = _task(s, key, project)
        if t.state != "PAUSED":
            raise typer.BadParameter(f"{key} is not paused")
        transition(s, t, t.paused_from or "READY", "resumed by human", actor="human")
    console.print(f"{key} resumed")


def _apply_holds_if_idle(a: App) -> None:
    from workforce.engine.orchestrator import EngineLocked, Orchestrator

    orch = Orchestrator(a, adapters={})
    try:
        orch._acquire_lock()
    except EngineLocked:
        return  # the running engine applies the hold
    try:
        orch._apply_holds()
    finally:
        orch._release_lock()


@app.command()
def report(project: Optional[str] = typer.Argument(None), config: Optional[Path] = ConfigOpt) -> None:
    """Generate a progress report (markdown) from real orchestration state."""
    from workforce.reporting import progress_report

    a = _app(config)
    with a.db.session() as s:
        md = progress_report(s, a.cfg, _project(s, project).id if project else None)
    out = a.cfg.artifact_dir / "progress_report.md"
    out.write_text(md)
    print(md)
    console.print(f"[dim]written to {out}[/]")


integrate_app = typer.Typer(help="Integration branch operations.")
app.add_typer(integrate_app, name="integrate")


@integrate_app.command("promote")
def integrate_promote(project: str, config: Optional[Path] = ConfigOpt) -> None:
    """Fast-forward the protected target branch to the integration branch (requires an approved 'promote' request)."""
    from workforce.gitops.repo import GitRepo

    a = _app(config)
    with a.db.session() as s:
        p = _project(s, project)
        if control.approval_status(s, "promote", p.id) != "approved":
            console.print("[red]promotion requires an approved 'promote' request (workforce approvals)[/]")
            raise typer.Exit(2)
        repo_path, pid = p.repo_path, p.id
    git = GitRepo(repo_path, a.cfg.policy)
    integ, target = f"wf/{project}/integration", a.cfg.git.target_branch
    if not git.is_ancestor(target, integ):
        console.print(f"[red]{target} has diverged from {integ}; refusing non-fast-forward promotion[/]")
        raise typer.Exit(2)
    if git.current_branch() == target:
        if not git.is_clean():
            console.print(f"[red]{target} checkout has uncommitted changes; refusing[/]")
            raise typer.Exit(2)
        git.git("merge", "--ff-only", integ)
    else:
        git.git("fetch", ".", f"{integ}:{target}")
    pushed = "not pushed (push not authorized in config)"
    if a.cfg.git.push_authorized:
        r = git.git("push", a.cfg.git.remote, target, check=False)
        pushed = "pushed" if r.returncode == 0 else f"push failed: {r.stderr.strip()[:200]}"
    with a.db.session() as s:
        control.audit(s, "human", "integration.promoted", {"target": target, "head": git.rev(target), "push": pushed},
                      project_id=pid)
    console.print(f"{target} -> {git.rev(target)[:10]}; {pushed}")


@integrate_app.command("rollback")
def integrate_rollback(project: str, key: str, config: Optional[Path] = ConfigOpt) -> None:
    """Revert an integrated task on the integration branch (non-destructive revert commit)."""
    from workforce.engine.state import transition
    from workforce.gitops.repo import GitRepo

    a = _app(config)
    with a.db.session() as s:
        p = _project(s, project)
        t = _task(s, key, project)
        if not t.integrated_commit:
            raise typer.BadParameter(f"{key} has not been integrated")
        git = GitRepo(p.repo_path, a.cfg.policy)
        iwt = git.add_worktree(a.cfg.worktree_dir / p.name / "_integration", f"wf/{p.name}/integration")
        merges = [c for c in s.scalars(select(m.Commit).where(m.Commit.task_id == t.id, m.Commit.role == "devops"))]
        log_ = git.out("log", "--merges", "--format=%H %s", f"{t.base_commit}..{t.integrated_commit}", cwd=iwt)
        shas = [line.split()[0] for line in log_.splitlines() if f"({t.key})" in line]
        for sha in shas:
            git.revert(iwt, sha)
        control.audit(s, "human", "integration.rollback", {"task": key, "reverted": shas, "merges": len(merges)},
                      p.id, t.id)
        t.integrated_commit = None
        transition(s, t, "BLOCKED", "rolled back by human", actor="human")
    console.print(f"reverted {len(shas)} merge commit(s) for {key}")


@app.command("safety-check")
def safety_check(config: Optional[Path] = ConfigOpt,
                 skip_tests: bool = typer.Option(False, help="Only run configuration checks")) -> None:
    """Run safety checks + the workforce test-suite; a pass enables unattended mode for this code version."""
    from workforce.daemon import config_checks, run_test_suite, safety_record

    a = _app(config)
    with a.db.session() as s:
        roles = {r.role: r.available for r in s.scalars(select(m.RoleStatus))}
    checks = config_checks(a.cfg, roles)
    suite = {"ok": False, "detail": "skipped"} if skip_tests else run_test_suite(Path(__file__).resolve().parents[2])
    rec = safety_record(checks, suite)
    with a.db.session() as s:
        control.set_flag(s, "safety_check", json.dumps(rec))
        control.audit(s, "human", "safety.check", {"passed": rec["passed"]})
    t = Table("check", "ok", "detail")
    for c in checks + [{"check": "workforce test-suite", **suite}]:
        t.add_row(c["check"], "[green]yes[/]" if c["ok"] else "[red]NO[/]", str(c["detail"])[:80])
    console.print(t)
    console.print("[green]PASSED[/] unattended mode enabled" if rec["passed"] else "[red]FAILED[/] unattended mode stays disabled")
    if not rec["passed"]:
        raise typer.Exit(1)


@app.command()
def serve(config: Optional[Path] = ConfigOpt, port: int = typer.Option(None)) -> None:
    """Serve the control API and dashboard on 127.0.0.1 (default port 8765)."""
    from workforce.api.server import DASHBOARD_DIR
    from workforce.api.server import serve as _serve

    a = _app(config)
    if not DASHBOARD_DIR.exists():
        console.print("[yellow]dashboard not built; run `npm run build` in dashboard/ (API still available)[/]")
    console.print(f"dashboard: http://{a.cfg.dashboard.host}:{port or a.cfg.dashboard.port}/")
    _serve(a, port=port)


daemon_app = typer.Typer(help="macOS launchd background operation.")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("install")
def daemon_install(config: Path = typer.Option(Path("workforce.yaml"), "--config", "-c"),
                   load: bool = typer.Option(True)) -> None:
    """Install a LaunchAgent that runs the engine unattended (requires a passing safety check)."""
    from workforce import daemon
    from workforce.daemon import unattended_allowed

    a = _app(config)
    with a.db.session() as s:
        ok, why = unattended_allowed(control.get_flag(s, "safety_check"))
    if not ok:
        console.print(f"[red]refusing to install unattended daemon:[/] {why}")
        raise typer.Exit(2)
    p = daemon.install(a.cfg, config, load=load)
    console.print(f"installed {p}")


@daemon_app.command("uninstall")
def daemon_uninstall() -> None:
    from workforce import daemon

    daemon.uninstall()
    console.print("uninstalled")


@daemon_app.command("status")
def daemon_status() -> None:
    from workforce import daemon

    console.print(daemon.status())


if __name__ == "__main__":  # pragma: no cover
    app()
