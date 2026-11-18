"""`workforce` command-line interface.

Engine-wide commands (models, safety-check, daemon, serve, project registry) use the
central configuration in the engine installation directory. Project commands act on
the *active project*, resolved in this order:

    command argument (name or absolute path) > `workforce --project /abs/path ...` /
    $WORKFORCE_PROJECT > nearest .workforce.yaml above the current directory >
    `workforce project use NAME` > the only registered project.
"""

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
from workforce.projects import ProjectContext, ProjectError, ProjectRegistry

app = typer.Typer(help="Coding Workforce: documentation-driven multi-agent engineering.", no_args_is_help=True)
console = Console()

ConfigOpt = typer.Option(None, "--config", "-c", help="Central workforce.yaml (default: <engine root>/workforce.yaml)",
                         envvar="WORKFORCE_CONFIG")
_GLOBAL: dict = {"project": None}


@app.callback()
def _root(project: Optional[str] = typer.Option(
        None, "--project", "-P", envvar="WORKFORCE_PROJECT",
        help="Select the project by absolute path (or registered name) instead of discovering it from the "
             "current directory.")) -> None:
    _GLOBAL["project"] = project


def _app(config: Optional[Path]) -> App:
    """The central (engine-wide) application."""
    return build_app(config)


def _registry(config: Optional[Path]) -> ProjectRegistry:
    return ProjectRegistry(_app(config), global_selector=_GLOBAL["project"])


def _fail(msg: str, code: int = 2):
    console.print(f"[red]{msg}[/]")
    raise typer.Exit(code)


def _ctx(config: Optional[Path], selector: str | None = None) -> ProjectContext:
    try:
        return _registry(config).resolve(selector)
    except ProjectError as e:
        _fail(str(e))


def _split_project(reg_args: list[str], config: Optional[Path], min_rest: int) -> tuple[ProjectContext, list[str]]:
    """Commands like `req approve [PROJECT] [KEYS...]`: the first argument is a project only if it names one."""
    reg = _registry(config)
    args = list(reg_args or [])
    if len(args) > min_rest and args:
        first = args[0]
        try:
            if first.startswith("/") or first in {e.name for e in reg.entries()}:
                return reg.context(reg.entry(first)), args[1:]
        except ProjectError as e:
            _fail(str(e))
    try:
        return reg.resolve(None), args
    except ProjectError as e:
        _fail(str(e))


@app.command()
def init(path: Optional[Path] = typer.Argument(None, help="Where to write the config (default: <engine root>/workforce.yaml)")) -> None:
    """Create the central workforce.yaml with the official role assignments and initialise the database."""
    from workforce.config import WorkforceConfig, default_config_path

    path = path or default_config_path()
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
    """Print the effective central configuration (or the active project's, with --project)."""
    if _GLOBAL["project"]:
        console.print_json(_ctx(config).app.cfg.model_dump_json())
        return
    console.print_json(_app(config).cfg.model_dump_json())


def _task_counts(c: ProjectContext) -> dict[str, int]:
    with c.app.db.session() as s:
        return dict(s.execute(select(m.Task.state, func.count()).where(m.Task.project_id == c.project_id)
                              .group_by(m.Task.state)).all())


@app.command()
def status(config: Optional[Path] = ConfigOpt, as_json: bool = typer.Option(False, "--json")) -> None:
    """Engine-wide status: daemon state, every registered project, roles and pending approvals."""
    reg = _registry(config)
    with reg.central.db.session() as s:
        run_state = control.get_flag(s, "run_state", "stopped")
        hb = control.get_flag(s, "heartbeat")
        roles = s.scalars(select(m.RoleStatus)).all()
        role_data = {r.role: {"model": r.model, "available": r.available, "detail": r.detail} for r in roles}
    projects = []
    for c in reg.contexts():
        with c.app.db.session() as s:
            p = s.get(m.Project, c.project_id)
            pending = s.scalar(select(func.count()).select_from(m.Approval).where(
                m.Approval.status == "pending",
                (m.Approval.project_id == c.project_id) | (m.Approval.project_id.is_(None)))) or 0
            prs = control.get_flag(s, "run_state", "stopped")
        projects.append({"name": c.name, "repo": str(c.repo), "status": p.status, "run_state": prs,
                         "tasks": _task_counts(c), "pending_approvals": pending})
    data = {"engine_run_state": run_state, "heartbeat": hb, "current_project": reg.current(),
            "projects": projects, "roles": role_data}
    if as_json:
        console.print_json(json.dumps(data, default=str))
        return
    console.print(f"[bold]engine:[/] {run_state}   [bold]heartbeat:[/] {hb or '-'}   "
                  f"[bold]current project:[/] {reg.current() or '-'}")
    t = Table("project", "status", "run state", "tasks", "pending approvals", "repository")
    for p in projects:
        t.add_row(p["name"], p["status"], p["run_state"], ", ".join(f"{k}={v}" for k, v in sorted(p["tasks"].items())) or "-",
                  str(p["pending_approvals"]), p["repo"])
    console.print(t)
    if roles:
        rt = Table("role", "model", "available", "detail")
        for r in roles:
            rt.add_row(r.role, r.model, "yes" if r.available else "[red]no[/]", r.detail[:80])
        console.print(rt)


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


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
project_app = typer.Typer(help="Register, select and inspect projects.")
app.add_typer(project_app, name="project")
req_app = typer.Typer(help="Requirements, issues and traceability (active project).")
app.add_typer(req_app, name="req")


def _ingest(c: ProjectContext, docs: Path) -> dict:
    from workforce.docs.ingest import ingest

    docs = docs.expanduser()
    if not docs.is_absolute():
        docs = (Path.cwd() / docs).resolve()
    if not docs.exists():
        _fail(f"{docs} does not exist")
    with c.app.db.session() as s:
        p = s.get(m.Project, c.project_id)
        stats = ingest(s, c.app.cfg, p, docs)
        if p.status in ("registered", "ingesting"):
            p.status = "requirements_review"
    return stats


@project_app.command("register")
def project_register(repo: Path = typer.Argument(..., help="Absolute path of an existing git repository"),
                     name: Optional[str] = typer.Option(None, help="Project name (default: repository folder name)"),
                     docs: Optional[Path] = typer.Option(None, help="Documentation file or directory to ingest"),
                     test_command: str = typer.Option("python -m pytest -q", help="Regression test command (allowlisted)"),
                     use: bool = typer.Option(False, "--use", help="Also make it the current project"),
                     config: Optional[Path] = ConfigOpt) -> None:
    """Register an existing git repository. Its state is stored separately under the engine's state directory,
    and a .workforce.yaml marker in the repository root enables discovery from any subdirectory."""
    reg = _registry(config)
    try:
        c = reg.register(repo, name=name, test_command=test_command)
        if use:
            reg.use(c.name)
    except ProjectError as e:
        _fail(str(e))
    console.print(f"registered [bold]{c.name}[/] -> {c.repo}\n  state: {c.home}\n  marker: {c.repo / '.workforce.yaml'}")
    if docs:
        console.print(f"ingested: {_ingest(c, docs)}")
        console.print("next: `workforce req list`, resolve issues, then `workforce req approve`")


@project_app.command("create")
def project_create(name: str, repo: Path = typer.Option(..., help="Absolute path of the target git repository"),
                   docs: Path = typer.Option(..., help="Documentation file or directory"),
                   test_command: str = typer.Option("python -m pytest -q", help="Regression test command (allowlisted)"),
                   config: Optional[Path] = ConfigOpt) -> None:
    """Register a project under NAME and ingest its documentation (register + ingest in one step)."""
    try:
        c = _registry(config).register(repo, name=name, test_command=test_command)
    except ProjectError as e:
        _fail(str(e))
    stats = _ingest(c, docs)
    console.print(f"project {name} created: {stats}")
    console.print("next: review `workforce req list`, resolve issues, then `workforce req approve`")


@project_app.command("ingest")
def project_ingest(args: list[str] = typer.Argument(..., help="[PROJECT] DOCS"),
                   config: Optional[Path] = ConfigOpt) -> None:
    """Ingest additional or updated documentation (changed requirements need re-approval)."""
    c, rest = _split_project(args, config, 1)
    if len(rest) != 1:
        _fail("usage: workforce project ingest [PROJECT] DOCS")
    console.print(_ingest(c, Path(rest[0])))


@project_app.command("list")
def project_list(config: Optional[Path] = ConfigOpt) -> None:
    """List registered projects (* = current project)."""
    reg = _registry(config)
    cur = reg.current()
    t = Table("", "name", "status", "tasks", "repository", "state directory")
    for c in reg.contexts():
        with c.app.db.session() as s:
            st = s.get(m.Project, c.project_id).status
        counts = _task_counts(c)
        t.add_row("*" if c.name == cur else "", c.name, st,
                  f"{counts.get('COMPLETED', 0)}/{sum(counts.values())} done" if counts else "-",
                  str(c.repo), str(c.home) + (" (shared legacy DB)" if c.legacy else ""))
    console.print(t)


@project_app.command("use")
def project_use(selector: Optional[str] = typer.Argument(None, help="Project name or absolute path"),
                clear: bool = typer.Option(False, "--clear", help="Forget the current project"),
                config: Optional[Path] = ConfigOpt) -> None:
    """Select the current project for commands run outside any registered repository."""
    reg = _registry(config)
    if clear:
        reg.use(None)
        console.print("current project cleared")
        return
    if not selector:
        console.print(f"current project: {reg.current() or '-'}")
        return
    try:
        c = reg.use(selector)
    except ProjectError as e:
        _fail(str(e))
    console.print(f"current project -> {c.name} ({c.repo})")


@project_app.command("status")
def project_status(selector: Optional[str] = typer.Argument(None, help="Project name or absolute path"),
                   as_json: bool = typer.Option(False, "--json"), config: Optional[Path] = ConfigOpt) -> None:
    """Detailed status of the active (or given) project."""
    c = _ctx(config, selector)
    with c.app.db.session() as s:
        p = s.get(m.Project, c.project_id)
        reqs = dict(s.execute(select(m.Requirement.status, func.count()).where(m.Requirement.project_id == p.id)
                              .group_by(m.Requirement.status)).all())
        open_issues = s.scalar(select(func.count()).select_from(m.Issue).where(
            m.Issue.project_id == p.id, m.Issue.status == "open")) or 0
        pending = [{"id": a.id, "kind": a.kind, "subject": a.subject} for a in s.scalars(
            select(m.Approval).where(m.Approval.status == "pending",
                                     (m.Approval.project_id == p.id) | (m.Approval.project_id.is_(None))))]
        tasks = [{"key": t.key, "state": t.state, "role": t.assigned_role, "attempts": t.attempts,
                  "blocked_reason": t.blocked_reason}
                 for t in s.scalars(select(m.Task).where(m.Task.project_id == p.id).order_by(m.Task.id))]
        data = {"name": c.name, "repo": str(c.repo), "state_dir": str(c.home), "isolated_state": c.isolated,
                "status": p.status, "test_command": p.test_command, "run_state": control.get_flag(s, "run_state", "stopped"),
                "heartbeat": control.get_flag(s, "heartbeat"), "requirements": reqs, "open_issues": open_issues,
                "tasks": tasks, "pending_approvals": pending,
                "integration_branch": f"wf/{c.name}/integration", "target_branch": c.app.cfg.git.target_branch}
    if as_json:
        console.print_json(json.dumps(data, default=str))
        return
    console.print(f"[bold]{c.name}[/] — {data['status']} (run state {data['run_state']})")
    console.print(f"  repository:  {c.repo}\n  state dir:   {c.home}\n  test cmd:    {data['test_command']}\n"
                  f"  branches:    {data['integration_branch']} -> {data['target_branch']}")
    console.print(f"  requirements: {reqs or '-'}; open issues: {open_issues}")
    t = Table("task", "state", "role", "attempts", "blocked reason")
    for x in tasks:
        t.add_row(x["key"], x["state"], x["role"] or "-", str(x["attempts"]), (x["blocked_reason"] or "")[:70])
    console.print(t)
    for a in pending:
        console.print(f"  [yellow]approval #{a['id']}[/] {a['kind']}: {a['subject'][:120]}")


@req_app.command("list")
def req_list(project: Optional[str] = typer.Argument(None), config: Optional[Path] = ConfigOpt) -> None:
    c = _ctx(config, project)
    with c.app.db.session() as s:
        t = Table("key", "status", "category", "prio", "text", "AC")
        for r in s.scalars(select(m.Requirement).where(m.Requirement.project_id == c.project_id).order_by(m.Requirement.id)):
            t.add_row(r.key, r.status, r.category, r.priority, r.text[:90], str(len(r.acceptance_criteria)))
        console.print(t)
        it = Table("issue", "kind", "severity", "status", "requirements", "description")
        for i in s.scalars(select(m.Issue).where(m.Issue.project_id == c.project_id)):
            it.add_row(str(i.id), i.kind, i.severity, i.status, ",".join(i.requirement_keys), i.description[:100])
        console.print(it)


@req_app.command("approve")
def req_approve(args: list[str] = typer.Argument(None, help="[PROJECT] [KEYS...]"),
                config: Optional[Path] = ConfigOpt) -> None:
    """Approve proposed requirements (all, or the given keys). Blocked by open major issues."""
    from workforce.docs.ingest import approve_requirements

    c, keys = _split_project(args, config, 0)
    with c.app.db.session() as s:
        got = approve_requirements(s, c.project_id, "human", keys or None)
    console.print(f"approved {len(got)} requirements in {c.name}")


@req_app.command("reject")
def req_reject(args: list[str] = typer.Argument(..., help="[PROJECT] KEY"), reason: str = typer.Option(...),
               config: Optional[Path] = ConfigOpt) -> None:
    from workforce.docs.ingest import reject_requirement

    c, rest = _split_project(args, config, 1)
    if len(rest) != 1:
        _fail("usage: workforce req reject [PROJECT] KEY --reason TEXT")
    with c.app.db.session() as s:
        reject_requirement(s, c.project_id, rest[0], "human", reason)


@req_app.command("resolve")
def req_resolve(issue_id: int, resolution: str = typer.Option(...),
                amend: list[str] = typer.Option(None, help="KEY=new requirement text"),
                project: Optional[str] = typer.Option(None, help="Project name or absolute path"),
                config: Optional[Path] = ConfigOpt) -> None:
    """Resolve an ambiguity/contradiction, optionally amending requirement text."""
    from workforce.docs.ingest import resolve_issue

    c = _ctx(config, project)
    amendments = dict(x.split("=", 1) for x in (amend or []))
    with c.app.db.session() as s:
        resolve_issue(s, issue_id, resolution, "human", amendments)
    console.print(f"issue {issue_id} resolved")


@req_app.command("trace")
def req_trace(project: Optional[str] = typer.Argument(None),
              fmt: str = typer.Option("markdown", "--format", help="markdown|csv|json"),
              config: Optional[Path] = ConfigOpt) -> None:
    """Print the requirement traceability matrix."""
    from workforce.docs.traceability import build_matrix, matrix_csv, matrix_markdown

    c = _ctx(config, project)
    with c.app.db.session() as s:
        rows = build_matrix(s, c.project_id)
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


def _orchestrator(c: ProjectContext):
    from workforce.engine.orchestrator import Orchestrator
    from workforce.engine.shared import SharedRuntime

    reg = ProjectRegistry(c.central)
    shared = SharedRuntime.from_registry(reg)
    return Orchestrator(c.app, adapters=build_adapters(c.app.cfg), shared=shared, project_name=c.name)


@app.command()
def plan(project: Optional[str] = typer.Argument(None), feedback: str = typer.Option("", help="Guidance for re-planning"),
         config: Optional[Path] = ConfigOpt) -> None:
    """Run the Architect on approved requirements and request architecture approval."""
    import asyncio

    from workforce.engine.planning import PlanningError, run_architecture
    from workforce.logging import set_log_project

    c = _ctx(config, project)
    orch = _orchestrator(c)
    set_log_project(c.name)
    try:
        result = asyncio.run(run_architecture(c.app.db, c.app.cfg, orch.agents, c.project_id, feedback))
    except PlanningError as e:
        _fail(f"planning failed: {e}", 1)
    console.print(f"architecture proposed for {c.name}: {len(result['decisions'])} decisions, {len(result['tasks'])} tasks")
    console.print(f"see {c.app.cfg.home / 'projects' / c.name / 'architecture.md'}; approve with `workforce approvals`")


@app.command("approvals")
def approvals_list(project: Optional[str] = typer.Option(None, help="Project name or absolute path"),
                   config: Optional[Path] = ConfigOpt, all_: bool = typer.Option(False, "--all")) -> None:
    """List approval requests of the active project (pending by default)."""
    c = _ctx(config, project)
    with c.app.db.session() as s:
        q = select(m.Approval).order_by(m.Approval.id)
        if not all_:
            q = q.where(m.Approval.status == "pending")
        t = Table("id", "kind", "status", "subject", title=f"approvals — {c.name}")
        for ap in s.scalars(q):
            t.add_row(str(ap.id), ap.kind, ap.status, ap.subject[:110])
        console.print(t)


@app.command("approve")
def approve_cmd(approval_id: int, comment: str = typer.Option(None),
                project: Optional[str] = typer.Option(None, help="Project name or absolute path"),
                config: Optional[Path] = ConfigOpt) -> None:
    """Approve a request (architecture, integration, clarification, promote, ...) of the active project."""
    from workforce.engine.orchestrator import approve

    c = _ctx(config, project)
    try:
        ap = approve(c.app, approval_id, True, "human", comment)
    except (KeyError, ValueError) as e:
        _fail(f"{c.name}: {e}")
    console.print(f"{c.name}: approved #{ap.id} ({ap.kind})")


@app.command("reject")
def reject_cmd(approval_id: int, comment: str = typer.Option(None),
               project: Optional[str] = typer.Option(None, help="Project name or absolute path"),
               config: Optional[Path] = ConfigOpt) -> None:
    from workforce.engine.orchestrator import approve

    c = _ctx(config, project)
    try:
        ap = approve(c.app, approval_id, False, "human", comment)
    except (KeyError, ValueError) as e:
        _fail(f"{c.name}: {e}")
    console.print(f"{c.name}: rejected #{ap.id} ({ap.kind})")


def _unattended_ok(central: App) -> None:
    from workforce.daemon import unattended_allowed

    with central.db.session() as s:
        ok, why = unattended_allowed(control.get_flag(s, "safety_check"))
    if not ok:
        _fail(f"unattended mode refused: {why}")


@app.command("run")
def run_cmd(config: Optional[Path] = ConfigOpt,
            all_projects: bool = typer.Option(False, "--all-projects",
                                              help="Schedule every registered project in this engine process"),
            until_idle: bool = typer.Option(True, "--until-idle/--forever",
                                            help="Stop automatically when no approved actionable work remains"),
            unattended: bool = typer.Option(False, help="Unattended mode (requires a passing safety check)"),
            max_seconds: float = typer.Option(None, help="Hard wall-clock limit")) -> None:
    """Run the engine in the foreground for the active project, or for all projects with --all-projects
    (also used when no project can be resolved)."""
    import asyncio

    from workforce.engine.orchestrator import EngineLocked
    from workforce.engine.supervisor import Supervisor, SupervisorLocked

    reg = _registry(config)
    if unattended:
        _unattended_ok(reg.central)
    ctx = None
    if not all_projects:
        try:
            ctx = reg.resolve(None)
        except ProjectError:
            ctx = None
    if ctx is None:
        sup = Supervisor(reg, build_adapters, unattended=unattended)
        try:
            res = asyncio.run(sup.run(until_idle=until_idle, max_seconds=max_seconds))
        except SupervisorLocked as e:
            _fail(str(e), 3)
        console.print(f"engine stopped: {res.reason}")
        for name, outcome in res.projects.items():
            console.print(f"  {name}: {outcome}")
        return
    if unattended:
        ctx.app.cfg.mode = "unattended"
        ctx.app.cfg.require_integration_approval = False
    orch = _orchestrator(ctx)
    try:
        summary = asyncio.run(orch.run(until_idle=until_idle, max_seconds=max_seconds))
    except EngineLocked as e:
        _fail(f"{ctx.name}: {e}", 3)
    console.print(f"engine stopped ({ctx.name}): {summary.reason}")
    console.print(f"completed={summary.completed} blocked={summary.blocked} waiting={summary.waiting}")


@app.command()
def start(config: Optional[Path] = ConfigOpt, unattended: bool = typer.Option(False),
          all_projects: bool = typer.Option(False, "--all-projects")) -> None:
    """Start the engine as a detached background process (logs in the state directory)."""
    import subprocess
    import sys

    reg = _registry(config)
    args = [sys.executable, "-m", "workforce.cli"]
    log_dir = reg.central.cfg.log_dir
    if not all_projects:
        try:
            c = reg.resolve(None)
            args += ["--project", str(c.repo)]
            log_dir = c.app.cfg.log_dir
        except ProjectError:
            all_projects = True
    args += ["run", "--until-idle"] + (["--all-projects"] if all_projects else [])
    if unattended:
        args.append("--unattended")
    if config:
        args += ["--config", str(config.resolve())]
    out = open(log_dir / "engine.out.log", "a")
    proc = subprocess.Popen(args, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    console.print(f"engine started in background (pid {proc.pid}); log: {log_dir / 'engine.out.log'}")


def _set_run_state(config: Optional[Path], state: str, all_: bool) -> None:
    if all_:
        a = _app(config)
        with a.db.session() as s:
            control.request_run_state(s, state, "human")
        console.print(f"engine-wide run state -> {state}")
        return
    c = _ctx(config)
    with c.app.db.session() as s:
        control.request_run_state(s, state, "human")
    console.print(f"{c.name}: run state -> {state}")


AllOpt = typer.Option(False, "--all", help="Apply to the multi-project engine (every project) instead of the active project")


@app.command()
def pause(config: Optional[Path] = ConfigOpt, all_: bool = AllOpt) -> None:
    """Pause scheduling; in-flight steps finish, no new steps start."""
    _set_run_state(config, "paused", all_)


@app.command()
def resume(config: Optional[Path] = ConfigOpt, all_: bool = AllOpt) -> None:
    """Resume a paused engine."""
    _set_run_state(config, "running", all_)


@app.command()
def stop(config: Optional[Path] = ConfigOpt, all_: bool = AllOpt) -> None:
    """Gracefully stop the engine (in-flight steps get a grace period, then are cancelled)."""
    _set_run_state(config, "stopping", all_)


task_app = typer.Typer(help="Inspect and control individual tasks of the active project.")
app.add_typer(task_app, name="task")
TaskProjectOpt = typer.Option(None, "--project", help="Project name or absolute path")


def _task(s, key: str, project_id: int) -> m.Task:
    t = s.scalars(select(m.Task).where(m.Task.key == key, m.Task.project_id == project_id)).first()
    if t is None:
        raise typer.BadParameter(f"task {key} not found in this project")
    return t


@task_app.command("list")
def task_list(project: Optional[str] = typer.Argument(None), config: Optional[Path] = ConfigOpt) -> None:
    c = _ctx(config, project)
    with c.app.db.session() as s:
        t = Table("key", "state", "role", "attempts", "deps", "requirements", "title", "blocked reason", title=c.name)
        for x in s.scalars(select(m.Task).where(m.Task.project_id == c.project_id).order_by(m.Task.id)):
            t.add_row(x.key, x.state, x.assigned_role or "-", str(x.attempts), ",".join(x.depends_on),
                      ",".join(x.requirement_keys), x.title[:40], (x.blocked_reason or "")[:60])
        console.print(t)


@task_app.command("show")
def task_show(key: str, project: Optional[str] = TaskProjectOpt, config: Optional[Path] = ConfigOpt) -> None:
    c = _ctx(config, project)
    with c.app.db.session() as s:
        t = _task(s, key, c.project_id)
        console.print(f"[bold]{c.name} {t.key}[/] {t.title} — {t.state} (attempts {t.attempts})")
        console.print(t.description)
        for e in t.events:
            console.print(f"  {e.ts:%H:%M:%S} {e.from_state} -> {e.to_state}: {e.reason[:150]}")
        for fb in t.feedback:
            console.print(f"  [yellow]feedback[/] {fb.get('source')}/{fb.get('type')}: {fb.get('message', '')[:200]}")


@task_app.command("retry")
def task_retry(key: str, project: Optional[str] = TaskProjectOpt, config: Optional[Path] = ConfigOpt) -> None:
    """Retry a BLOCKED/FAILED task with a fresh correction budget."""
    from workforce.engine.state import transition

    c = _ctx(config, project)
    with c.app.db.session() as s:
        t = _task(s, key, c.project_id)
        if t.state not in ("BLOCKED", "FAILED"):
            raise typer.BadParameter(f"{key} is {t.state}, not BLOCKED/FAILED")
        t.attempts, t.failure_signatures, t.assigned_role = 0, [], None
        transition(s, t, "READY", "human retry", actor="human")
    console.print(f"{c.name}: {key} -> READY")


@task_app.command("cancel")
def task_cancel(key: str, project: Optional[str] = TaskProjectOpt, config: Optional[Path] = ConfigOpt) -> None:
    """Cancel a task (a running step is interrupted)."""
    c = _ctx(config, project)
    with c.app.db.session() as s:
        t = _task(s, key, c.project_id)
        control.set_flag(s, f"task_hold:{t.id}", "cancel")
        control.audit(s, "human", "task.cancel_requested", {"key": key}, t.project_id, t.id)
    console.print(f"{c.name}: cancel requested for {key} (applied by the engine; immediate if it is not running)")
    _apply_holds_if_idle(c.app)


@task_app.command("pause")
def task_pause(key: str, project: Optional[str] = TaskProjectOpt, config: Optional[Path] = ConfigOpt) -> None:
    c = _ctx(config, project)
    with c.app.db.session() as s:
        t = _task(s, key, c.project_id)
        control.set_flag(s, f"task_hold:{t.id}", "pause")
    console.print(f"{c.name}: pause requested for {key}")
    _apply_holds_if_idle(c.app)


@task_app.command("resume")
def task_resume(key: str, project: Optional[str] = TaskProjectOpt, config: Optional[Path] = ConfigOpt) -> None:
    from workforce.engine.state import transition

    c = _ctx(config, project)
    with c.app.db.session() as s:
        t = _task(s, key, c.project_id)
        if t.state != "PAUSED":
            raise typer.BadParameter(f"{key} is not paused")
        transition(s, t, t.paused_from or "READY", "resumed by human", actor="human")
    console.print(f"{c.name}: {key} resumed")


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
    """Generate a progress report (markdown) for the active project from real orchestration state."""
    from workforce.reporting import progress_report

    c = _ctx(config, project)
    with c.app.db.session() as s:
        md = progress_report(s, c.app.cfg, c.project_id)
    out = c.app.cfg.artifact_dir / "progress_report.md"
    out.write_text(md)
    print(md)
    console.print(f"[dim]written to {out}[/]")


integrate_app = typer.Typer(help="Integration branch operations (active project).")
app.add_typer(integrate_app, name="integrate")


@integrate_app.command("promote")
def integrate_promote(project: Optional[str] = typer.Argument(None), config: Optional[Path] = ConfigOpt) -> None:
    """Fast-forward the protected target branch to the integration branch (requires an approved 'promote' request)."""
    from workforce.gitops.repo import GitRepo

    c = _ctx(config, project)
    a = c.app
    with a.db.session() as s:
        if control.approval_status(s, "promote", c.project_id) != "approved":
            _fail("promotion requires an approved 'promote' request (workforce approvals)")
    git = GitRepo(c.repo, a.cfg.policy)
    integ, target = f"wf/{c.name}/integration", a.cfg.git.target_branch
    if not git.is_ancestor(target, integ):
        _fail(f"{target} has diverged from {integ}; refusing non-fast-forward promotion")
    if git.current_branch() == target:
        if not git.is_clean():
            _fail(f"{target} checkout has uncommitted changes; refusing")
        git.git("merge", "--ff-only", integ)
    else:
        git.git("fetch", ".", f"{integ}:{target}")
    pushed = "not pushed (push not authorized in config)"
    if a.cfg.git.push_authorized:
        r = git.git("push", a.cfg.git.remote, target, check=False)
        pushed = "pushed" if r.returncode == 0 else f"push failed: {r.stderr.strip()[:200]}"
    with a.db.session() as s:
        control.audit(s, "human", "integration.promoted", {"target": target, "head": git.rev(target), "push": pushed},
                      project_id=c.project_id)
    console.print(f"{c.name}: {target} -> {git.rev(target)[:10]}; {pushed}")


@integrate_app.command("rollback")
def integrate_rollback(args: list[str] = typer.Argument(..., help="[PROJECT] TASK_KEY"),
                       config: Optional[Path] = ConfigOpt) -> None:
    """Revert an integrated task on the integration branch (non-destructive revert commit)."""
    from workforce.engine.state import transition
    from workforce.gitops.repo import GitRepo

    c, rest = _split_project(args, config, 1)
    if len(rest) != 1:
        _fail("usage: workforce integrate rollback [PROJECT] TASK_KEY")
    key = rest[0]
    a = c.app
    with a.db.session() as s:
        t = _task(s, key, c.project_id)
        if not t.integrated_commit:
            raise typer.BadParameter(f"{key} has not been integrated")
        git = GitRepo(c.repo, a.cfg.policy)
        iwt = git.add_worktree(a.cfg.worktree_dir / c.name / "_integration", f"wf/{c.name}/integration")
        log_ = git.out("log", "--merges", "--format=%H %s", f"{t.base_commit}..{t.integrated_commit}", cwd=iwt)
        shas = [line.split()[0] for line in log_.splitlines() if f"({t.key})" in line]
        for sha in shas:
            git.revert(iwt, sha)
        control.audit(s, "human", "integration.rollback", {"task": key, "reverted": shas}, c.project_id, t.id)
        t.integrated_commit = None
        transition(s, t, "BLOCKED", "rolled back by human", actor="human")
    console.print(f"{c.name}: reverted {len(shas)} merge commit(s) for {key}")


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
    """Serve the control API and dashboard (all registered projects) on 127.0.0.1 (default port 8765)."""
    from workforce.api.server import DASHBOARD_DIR
    from workforce.api.server import serve as _serve

    reg = _registry(config)
    if not DASHBOARD_DIR.exists():
        console.print("[yellow]dashboard not built; run `npm run build` in dashboard/ (API still available)[/]")
    console.print(f"dashboard: http://{reg.central.cfg.dashboard.host}:{port or reg.central.cfg.dashboard.port}/")
    _serve(reg.central, port=port, registry=reg)


daemon_app = typer.Typer(help="macOS launchd background operation (schedules every registered project).")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("install")
def daemon_install(config: Optional[Path] = ConfigOpt, load: bool = typer.Option(True)) -> None:
    """Install a LaunchAgent that runs the multi-project engine unattended (requires a passing safety check)."""
    from workforce import daemon
    from workforce.config import default_config_path

    a = _app(config)
    _unattended_ok(a)
    p = daemon.install(a.cfg, config or default_config_path(), load=load)
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


def main() -> None:
    app(prog_name="workforce")


if __name__ == "__main__":  # pragma: no cover
    main()
