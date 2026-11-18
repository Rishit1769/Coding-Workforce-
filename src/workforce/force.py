"""`force`: the short global command for the Coding Workforce.

    force                      start the workforce (in the background) for the active project
    force --project /abs/path  start it for that project
    force <command> ...        exactly `workforce <command> ...` (status, plan, pause, resume, stop, ...)

The active project is the one the current directory belongs to (nearest
.workforce.yaml or registered repository), else `workforce project use`, else
the default project (`workforce project default`, otherwise YANTRA)."""

from __future__ import annotations

import os
import sys

from rich.console import Console

console = Console()

GLOBAL_WITH_VALUE = {"--project", "-P"}

HEADER = """[bold]force[/] — run the Coding Workforce from any directory.

  [bold]force[/]                       start the workforce in the background for the active project
  [bold]force --project /abs/path[/]   start it for a specific registered project
  [bold]force <command>[/]             any `workforce` command, e.g. force status · force plan ·
                              force pause · force resume · force stop · force project list

Active project: the repository you are in (nearest .workforce.yaml), else `force project use NAME`,
else the default project (`force project default NAME`; YANTRA unless changed).
Only one engine runs per project; `force` refuses to start a second one.
"""


def split_globals(args: list[str]) -> tuple[list[str], list[str]]:
    """Split leading global options (--project PATH) from the subcommand and its arguments."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in GLOBAL_WITH_VALUE:
            i += 2
        elif a.startswith("--project="):
            i += 1
        else:
            break
    return args[:i], args[i:]


def _project_option(globals_: list[str]) -> str | None:
    value = os.environ.get("WORKFORCE_PROJECT") or None
    for i, a in enumerate(globals_):
        if a in GLOBAL_WITH_VALUE and i + 1 < len(globals_):
            value = globals_[i + 1]
        elif a.startswith("--project="):
            value = a.split("=", 1)[1]
    return value


def start_active(project: str | None) -> int:
    from workforce.app import build_app
    from workforce.launcher import launch_project
    from workforce.projects import ProjectError, ProjectRegistry

    reg = ProjectRegistry(build_app(None), global_selector=project)
    try:
        ctx = reg.resolve(None)
    except ProjectError as e:
        console.print(f"[red]{e}[/]")
        console.print("Register a repository with: workforce project register /absolute/path/to/repo")
        return 2
    from workforce.db import models as m

    with ctx.app.db.session() as s:
        status = s.get(m.Project, ctx.project_id).status
    console.print(f"active project: [bold]{ctx.name}[/] ({ctx.repo}) — selected by: {ctx.source}; status: {status}")
    if status != "executing":
        console.print("  no approved plan is executing yet; the engine will find nothing to do. Next steps: "
                      "`force req list` → `force req approve` → `force plan` → `force approve <id>`")
    res = launch_project(ctx)
    console.print(res.message)
    if res.started:
        console.print("  watch: force project status · stop: force stop")
    return 0 if (res.started or res.pid is not None) else 1


def main(argv: list[str] | None = None) -> None:
    from workforce.cli import app

    args = list(sys.argv[1:] if argv is None else argv)
    globals_, rest = split_globals(args)
    if rest in (["--help"], ["-h"]) or (not globals_ and rest == ["help"]):
        console.print(HEADER)
        app(args=["--help"], prog_name="force")
        return
    if rest:
        app(args=args, prog_name="force")
        return
    sys.exit(start_active(_project_option(globals_)))


if __name__ == "__main__":  # pragma: no cover
    main()
