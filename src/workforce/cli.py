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


if __name__ == "__main__":  # pragma: no cover
    app()
