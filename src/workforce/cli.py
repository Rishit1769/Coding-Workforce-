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


if __name__ == "__main__":  # pragma: no cover
    app()
