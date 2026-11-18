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


if __name__ == "__main__":  # pragma: no cover
    app()
