"""Multi-project support: registry, discovery and per-project state isolation.

The central database's existing `projects` table is the registry. Each project
registered with `workforce project register` gets its own state home
(`<central home>/homes/<name>/`: database, logs, worktrees, artifacts and
preserved documents), so documentation, worktrees, branches (`wf/<name>/...`),
task queues and execution history of different projects never mix.

Registry rows created before multi-project support have `home_path = NULL`;
their state stays in the central database exactly as before.

Active project resolution order:
    explicit selector (command argument) > `workforce --project PATH` / $WORKFORCE_PROJECT
    > nearest `.workforce.yaml` above the current directory > a registered repository containing
    the current directory > `workforce project use` > the only registered project.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select

from workforce.app import App, build_app
from workforce.config import WorkforceConfig
from workforce.db.models import Project
from workforce.engine import control
from workforce.logging import register_project_logs

MARKER = ".workforce.yaml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
CURRENT_FLAG = "current_project"


class ProjectError(Exception):
    pass


class ProjectOverrides(BaseModel):
    """Optional per-project settings in .workforce.yaml (applied over the central configuration)."""

    target_branch: str | None = None
    require_architecture_approval: bool | None = None
    require_integration_approval: bool | None = None
    max_dev_concurrency: int | None = None
    max_correction_attempts: int | None = None
    command_timeout_s: int | None = None
    extra_allowed_commands: list[str] = Field(default_factory=list)
    extra_protected_paths: list[str] = Field(default_factory=list)


class MarkerFile(BaseModel):
    project: str
    repo: str
    overrides: ProjectOverrides = ProjectOverrides()


@dataclass
class ProjectContext:
    """A resolved project: its registry entry plus the App holding its isolated state."""

    central: App
    entry_id: int
    name: str
    repo: Path
    home: Path
    app: App
    project_id: int  # id of the project row inside app.db
    legacy: bool

    @property
    def isolated(self) -> bool:
        return not self.legacy


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    return s[:63]


def find_marker(start: Path) -> Path | None:
    """Nearest .workforce.yaml in `start` or any parent directory."""
    p = start.resolve()
    for d in (p, *p.parents):
        if (d / MARKER).is_file():
            return d / MARKER
    return None


def read_marker(path: Path) -> MarkerFile:
    try:
        return MarkerFile.model_validate(yaml.safe_load(path.read_text()) or {})
    except Exception as e:
        raise ProjectError(f"invalid {path}: {e}") from e


def _git_toplevel(path: Path) -> Path | None:
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True)
    return Path(p.stdout.strip()).resolve() if p.returncode == 0 else None


def _exclude_marker(repo: Path) -> None:
    """Keep the marker out of `git status` without touching tracked files (.git/info/exclude is local)."""
    p = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=repo, capture_output=True, text=True)
    if p.returncode != 0:
        return
    exclude = (repo / p.stdout.strip()).resolve()
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if f"/{MARKER}" not in lines:
        exclude.write_text("\n".join([*lines, f"/{MARKER}"]) + "\n")


def project_config(central: WorkforceConfig, home: Path, overrides: ProjectOverrides | None) -> WorkforceConfig:
    cfg = central.model_copy(deep=True)
    cfg.home = home
    o = overrides or ProjectOverrides()
    if o.target_branch:
        cfg.git.target_branch = o.target_branch
    if o.require_architecture_approval is not None:
        cfg.require_architecture_approval = o.require_architecture_approval
    if o.require_integration_approval is not None:
        cfg.require_integration_approval = o.require_integration_approval
    if o.max_dev_concurrency is not None:
        cfg.limits.max_dev_concurrency = min(o.max_dev_concurrency, central.limits.max_total_dev_concurrency)
    if o.max_correction_attempts is not None:
        cfg.limits.max_correction_attempts = o.max_correction_attempts
    if o.command_timeout_s is not None:
        cfg.limits.command_timeout_s = o.command_timeout_s
    cfg.policy.allowed_commands = [*cfg.policy.allowed_commands, *o.extra_allowed_commands]
    cfg.policy.protected_paths = [*cfg.policy.protected_paths, *o.extra_protected_paths]
    return cfg


class ProjectRegistry:
    def __init__(self, central: App, global_selector: str | None = None):
        self.central = central
        self.global_selector = global_selector
        self._apps: dict[str, App] = {}

    # ---- registry -------------------------------------------------------
    def entries(self) -> list[Project]:
        with self.central.db.session() as s:
            rows = list(s.scalars(select(Project).order_by(Project.id)))
            s.expunge_all()
            return rows

    def _by_name(self, name: str) -> Project | None:
        return next((e for e in self.entries() if e.name == name), None)

    def entry(self, selector: str | Path) -> Project:
        sel = str(selector)
        looks_like_path = "/" in sel or sel.startswith("~") or isinstance(selector, Path)
        if not looks_like_path:
            e = self._by_name(sel)
            if e is None:
                raise ProjectError(f"no registered project named {sel!r} (see `workforce project list`)")
            return e
        p = Path(sel).expanduser()
        if not p.is_absolute():
            raise ProjectError(f"project paths must be absolute: {sel!r}")
        p = p.resolve()
        marker = find_marker(p) if p.exists() else None
        if marker:
            name = read_marker(marker).project
            e = self._by_name(name)
            if e is None:
                raise ProjectError(f"{marker} names project {name!r}, which is not registered here; "
                                   f"run `workforce project register {marker.parent}`")
            return e
        matches = [e for e in self.entries() if p == Path(e.repo_path) or p.is_relative_to(Path(e.repo_path))]
        if not matches:
            raise ProjectError(f"{p} is not inside a registered project; run `workforce project register {p}`")
        return max(matches, key=lambda e: len(e.repo_path))

    def home_of(self, entry: Project) -> Path:
        return Path(entry.home_path) if entry.home_path else self.central.cfg.home

    def app_for(self, entry: Project) -> App:
        if not entry.home_path:
            return self.central
        if entry.name not in self._apps:
            marker = Path(entry.repo_path) / MARKER
            overrides = read_marker(marker).overrides if marker.is_file() else None
            cfg = project_config(self.central.cfg, Path(entry.home_path), overrides)
            self._apps[entry.name] = build_app(cfg=cfg, configure_logging=False)
            register_project_logs(entry.name, cfg.log_dir)
        return self._apps[entry.name]

    def context(self, entry: Project) -> ProjectContext:
        app = self.app_for(entry)
        if entry.home_path:
            with app.db.session() as s:
                local = s.scalars(select(Project).where(Project.name == entry.name)).first()
                if local is None:
                    raise ProjectError(f"project state for {entry.name!r} is missing in {entry.home_path}")
                pid = local.id
        else:
            pid = entry.id
        return ProjectContext(self.central, entry.id, entry.name, Path(entry.repo_path), self.home_of(entry), app,
                              pid, legacy=not entry.home_path)

    def contexts(self) -> list[ProjectContext]:
        return [self.context(e) for e in self.entries()]

    # ---- registration ---------------------------------------------------
    def register(self, repo: Path | str, name: str | None = None, test_command: str = "python -m pytest -q",
                 write_marker: bool = True) -> ProjectContext:
        from workforce.security.policy import check_command

        repo_p = Path(repo).expanduser()
        if not repo_p.is_absolute():
            raise ProjectError(f"register a repository by absolute path (got {str(repo)!r})")
        repo_p = repo_p.resolve()
        if not repo_p.is_dir():
            raise ProjectError(f"{repo_p} does not exist")
        top = _git_toplevel(repo_p)
        if top is None:
            raise ProjectError(f"{repo_p} is not a git repository")
        if top != repo_p:
            raise ProjectError(f"{repo_p} is inside the repository {top}; register the repository root")
        check_command(test_command, self.central.cfg.policy)
        name = name or slugify(repo_p.name)
        if not NAME_RE.match(name):
            raise ProjectError(f"invalid project name {name!r}: use lowercase letters, digits, '-' or '_'")
        engine_home = self.central.cfg.home.resolve()
        if repo_p == engine_home or repo_p.is_relative_to(engine_home):
            raise ProjectError("a project repository cannot live inside the workforce state directory")
        marker = repo_p / MARKER
        if marker.is_file() and read_marker(marker).project != name:
            raise ProjectError(f"{marker} already belongs to project {read_marker(marker).project!r}")
        for e in self.entries():
            if e.name == name and Path(e.repo_path) != repo_p:
                raise ProjectError(f"project name {name!r} is already used for {e.repo_path}")
            if Path(e.repo_path) == repo_p and e.name != name:
                raise ProjectError(f"{repo_p} is already registered as {e.name!r}")
        existing = self._by_name(name)
        home = (self.central.cfg.home / "homes" / name).resolve()
        with self.central.db.session() as s:
            if existing is None:
                s.add(Project(name=name, repo_path=str(repo_p), test_command=test_command, status="registered",
                              home_path=str(home)))
            else:
                row = s.get(Project, existing.id)
                row.test_command = test_command
                if row.home_path:
                    home = Path(row.home_path)
            control.audit(s, "human", "project.registered", {"name": name, "repo": str(repo_p), "home": str(home)})
        entry = self._by_name(name)
        if entry.home_path:
            app = self.app_for(entry)
            with app.db.session() as s:
                local = s.scalars(select(Project).where(Project.name == name)).first()
                if local is None:
                    s.add(Project(name=name, repo_path=str(repo_p), test_command=test_command, status="registered"))
                else:
                    local.test_command = test_command
        if write_marker and not marker.is_file():
            marker.write_text(
                "# Coding Workforce project marker (discovered from any subdirectory).\n"
                "# Optional per-project overrides go under `overrides:` - see docs/operations.md.\n"
                + yaml.safe_dump({"project": name, "repo": str(repo_p), "overrides": {}}, sort_keys=False))
            _exclude_marker(repo_p)
        return self.context(entry)

    # ---- selection ------------------------------------------------------
    def current(self) -> str | None:
        with self.central.db.session() as s:
            return control.get_flag(s, CURRENT_FLAG) or None

    def use(self, selector: str | None) -> ProjectContext | None:
        with self.central.db.session() as s:
            if selector is None:
                control.set_flag(s, CURRENT_FLAG, "")
                return None
        e = self.entry(selector)
        with self.central.db.session() as s:
            control.set_flag(s, CURRENT_FLAG, e.name)
            control.audit(s, "human", "project.use", {"name": e.name})
        return self.context(e)

    def resolve(self, selector: str | Path | None = None, cwd: Path | None = None) -> ProjectContext:
        sel = selector or self.global_selector
        if sel:
            return self.context(self.entry(sel))
        here = (cwd or Path.cwd()).resolve()
        marker = find_marker(here)
        if marker:
            return self.context(self.entry(str(marker.parent)))
        inside = [e for e in self.entries() if here == Path(e.repo_path) or here.is_relative_to(Path(e.repo_path))]
        if inside:  # registered repository without a marker (e.g. projects created before markers existed)
            return self.context(max(inside, key=lambda e: len(e.repo_path)))
        cur = self.current()
        if cur:
            e = self._by_name(cur)
            if e is not None:
                return self.context(e)
        entries = self.entries()
        if len(entries) == 1:
            return self.context(entries[0])
        if not entries:
            raise ProjectError("no projects registered; run `workforce project register /absolute/path/to/repo`")
        raise ProjectError("no active project: run inside a registered repository, pass `--project /abs/path`, or "
                           "select one with `workforce project use NAME` (registered: "
                           + ", ".join(e.name for e in entries) + ")")
