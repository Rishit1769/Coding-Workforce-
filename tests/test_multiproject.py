"""Integration tests for running projects from any directory: discovery, path
resolution, per-project isolation, project switching, the shared multi-project
daemon, workspace boundaries and preservation of pre-existing state."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from workforce.api.server import create_api
from workforce.app import build_app
from workforce.cli import app as cli
from workforce.config import Role, WorkforceConfig
from workforce.db import models as m
from workforce.db.session import MIGRATIONS_DIR
from workforce.docs.ingest import approve_requirements, ingest
from workforce.engine.orchestrator import Orchestrator, approve
from workforce.engine.planning import run_architecture
from workforce.engine.shared import SharedRuntime
from workforce.engine.supervisor import Supervisor
from workforce.projects import MARKER, ProjectError, ProjectRegistry, find_marker
from workforce.providers.base import ExecutionResult
from workforce.selftest.scenario import build_adapter, create_sample_project

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout


def central_cfg(home: Path) -> WorkforceConfig:
    cfg = WorkforceConfig(home=home)
    for role in Role:
        cfg.roles[role].provider = "scripted"
        cfg.roles[role].model = f"scripted-{role.value}"
    cfg.limits.poll_interval_s = 0.02
    cfg.limits.provider_wait_s = 0.1
    cfg.limits.shutdown_grace_s = 2
    cfg.require_integration_approval = False
    cfg.providers["scripted"].max_retries = 0
    return cfg


def make_repo(tmp_path: Path, label: str) -> Path:
    repo, _ = create_sample_project(tmp_path / label)
    (repo / "src" / "deep" / "dir").mkdir(parents=True)
    return repo


@pytest.fixture
def central(tmp_path):
    return build_app(cfg=central_cfg(tmp_path / "engine-state"), stderr_logs=False)


def cfg_file(tmp_path: Path, central) -> Path:
    import yaml
    data = json.loads(central.cfg.model_dump_json())
    data["home"] = str(central.cfg.home)
    p = tmp_path / "central.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


# ---------------------------------------------------------------- registration
def test_register_validates_paths_and_writes_marker(central, tmp_path):
    reg = ProjectRegistry(central)
    repo = make_repo(tmp_path, "a")
    with pytest.raises(ProjectError, match="absolute"):
        reg.register("relative/path")
    with pytest.raises(ProjectError, match="not a git repository"):
        (tmp_path / "plain").mkdir()
        reg.register(tmp_path / "plain")
    with pytest.raises(ProjectError, match="register the repository root"):
        reg.register(repo / "src")
    with pytest.raises(ProjectError, match="invalid project name"):
        reg.register(repo, name="Bad Name!")
    c = reg.register(repo, name="anvikra")
    assert c.isolated and c.home == (central.cfg.home / "homes" / "anvikra").resolve()
    assert (repo / MARKER).is_file()
    # the marker never dirties the user's repository
    assert git(repo, "status", "--porcelain") == ""
    # idempotent re-registration; conflicting names/paths rejected
    assert reg.register(repo, name="anvikra").entry_id == c.entry_id
    with pytest.raises(ProjectError, match="already (registered|belongs)"):
        reg.register(repo, name="other")
    other = make_repo(tmp_path, "b")
    with pytest.raises(ProjectError, match="already used"):
        reg.register(other, name="anvikra")


# ---------------------------------------------------------------- discovery
def test_discovery_explicit_selection_and_switching(central, tmp_path):
    reg = ProjectRegistry(central)
    a, b = make_repo(tmp_path, "a"), make_repo(tmp_path, "b")
    reg.register(a, name="anvikra")
    reg.register(b, name="yantra")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    # upward search for .workforce.yaml from any subdirectory
    assert find_marker(a / "src" / "deep" / "dir") == a / MARKER
    assert reg.resolve(cwd=a / "src" / "deep" / "dir").name == "anvikra"
    assert reg.resolve(cwd=b).name == "yantra"
    # outside any project, with nothing selected, the YANTRA default project is used
    fallback = reg.resolve(cwd=outside)
    assert fallback.name == "yantra" and fallback.source == "default project"
    # project use (switching) is the fallback outside a repository ...
    reg.use("yantra")
    assert reg.resolve(cwd=outside).name == "yantra"
    reg.use(str(a))
    assert reg.resolve(cwd=outside).name == "anvikra"
    # ... but the marker of the directory you are in wins over `use`
    assert reg.resolve(cwd=b).name == "yantra"
    # --project /absolute/path beats both, including a path inside the repository
    explicit = ProjectRegistry(central, global_selector=str(b / "src"))
    assert explicit.resolve(cwd=a).name == "yantra"
    with pytest.raises(ProjectError, match="absolute"):
        ProjectRegistry(central, global_selector="./relative/b").resolve(cwd=a)
    with pytest.raises(ProjectError, match="not inside a registered project"):
        ProjectRegistry(central, global_selector=str(outside)).resolve(cwd=a)
    reg.use(None)
    assert reg.current() is None
    # without a YANTRA project and with several registered, explicit selection is required
    reg2 = ProjectRegistry(build_app(cfg=central_cfg(tmp_path / "engine2"), stderr_logs=False))
    reg2.register(make_repo(tmp_path, "c"), name="alpha")
    reg2.register(make_repo(tmp_path, "d"), name="beta")
    with pytest.raises(ProjectError, match="no active project"):
        reg2.resolve(cwd=outside)


def test_cli_from_any_directory(central, tmp_path, monkeypatch):
    cfgp = cfg_file(tmp_path, central)
    monkeypatch.setenv("WORKFORCE_CONFIG", str(cfgp))
    a, b = make_repo(tmp_path, "a"), make_repo(tmp_path, "b")
    r = runner.invoke(cli, ["project", "register", str(a), "--name", "anvikra", "--docs", str(a / "requirements")])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["project", "register", "relative/repo"])
    assert r.exit_code == 2 and "absolute" in r.output
    assert runner.invoke(cli, ["project", "register", str(b), "--name", "yantra"]).exit_code == 0
    # from a subdirectory of ANVIKRA: commands resolve to ANVIKRA
    monkeypatch.chdir(a / "src" / "deep" / "dir")
    r = runner.invoke(cli, ["req", "list"])
    assert r.exit_code == 0 and "FR-1" in r.output
    r = runner.invoke(cli, ["project", "status", "--json"])
    assert json.loads(r.output)["name"] == "anvikra"
    # YANTRA has no documents yet: its requirement list is empty even though ANVIKRA's is not
    r = runner.invoke(cli, ["--project", str(b), "req", "list"])
    assert r.exit_code == 0 and "FR-1" not in r.output
    # approve inside ANVIKRA only
    assert "approved 3 requirements in anvikra" in runner.invoke(cli, ["req", "approve"]).output
    monkeypatch.chdir(tmp_path)
    # outside every repository the YANTRA default applies (it has no requirements yet)
    r = runner.invoke(cli, ["req", "list"])
    assert r.exit_code == 0 and "FR-1" not in r.output
    assert runner.invoke(cli, ["project", "use", "anvikra"]).exit_code == 0
    r = runner.invoke(cli, ["project", "status", "--json"])
    assert json.loads(r.output)["name"] == "anvikra"
    assert runner.invoke(cli, ["project", "use", "yantra"]).exit_code == 0
    r = runner.invoke(cli, ["project", "status", "--json"])
    assert json.loads(r.output)["name"] == "yantra"
    r = runner.invoke(cli, ["project", "list"])
    assert "anvikra" in r.output and "yantra" in r.output and "*" in r.output
    r = runner.invoke(cli, ["status", "--json"])
    names = {p["name"] for p in json.loads(r.output)["projects"]}
    assert names == {"anvikra", "yantra"}
    # positional project name still works (backward compatible)
    r = runner.invoke(cli, ["req", "approve", "anvikra"])
    assert r.exit_code == 0 and "in anvikra" in r.output


def test_global_command_shim_and_engine_root(tmp_path):
    """`workforce` works from any directory without activating the venv, and resolves the central
    configuration from the engine installation directory - never from the cwd."""
    bindir = tmp_path / "bin"
    env = {**os.environ, "WORKFORCE_BIN_DIR": str(bindir)}
    r = subprocess.run([str(ROOT / "scripts" / "install_global.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    shim = bindir / "workforce"
    assert shim.is_symlink()
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "workforce.yaml").write_text("home: .wfstate\n")
    elsewhere = tmp_path / "somewhere" / "else"
    elsewhere.mkdir(parents=True)
    (elsewhere / "workforce.yaml").write_text("home: /should/not/be/used\n")
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "WORKFORCE_"))}
    clean_env["PATH"] = f"{bindir}:/usr/bin:/bin"
    clean_env["WORKFORCE_ENGINE_ROOT"] = str(engine)
    r = subprocess.run(["workforce", "config"], cwd=elsewhere, env=clean_env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["home"] == str(engine / ".wfstate")


# ---------------------------------------------------------------- isolation + daemon
async def _prepare(reg: ProjectRegistry, repo: Path, name: str) -> object:
    c = reg.register(repo, name=name)
    with c.app.db.session() as s:
        p = s.get(m.Project, c.project_id)
        ingest(s, c.app.cfg, p, repo / "requirements")
        approve_requirements(s, p.id, "human")
    orch = Orchestrator(c.app, adapters={"scripted": build_adapter({})}, shared=SharedRuntime.from_registry(reg),
                        project_name=name)
    await run_architecture(c.app.db, c.app.cfg, orch.agents, c.project_id)
    with c.app.db.session() as s:
        aid = s.scalars(select(m.Approval).where(m.Approval.kind == "architecture")).one().id
    approve(c.app, aid, True, "human")
    return reg.context(reg.entry(name))


def _tracking_factory(stats: dict):
    """Scripted agents that record how many developer runs overlap, overall and per role."""
    def factory(cfg):
        adapter = build_adapter({})
        dev = adapter.handlers["developer_1"]

        async def tracked(req):
            stats["active"] += 1
            stats["roles"].append(req.role)
            stats["max"] = max(stats["max"], stats["active"])
            stats["concurrent_roles"].append(tuple(sorted(stats["running_roles"] + [req.role])))
            stats["running_roles"].append(req.role)
            try:
                await asyncio.sleep(0.15)
                return await dev(req)
            finally:
                stats["active"] -= 1
                stats["running_roles"].remove(req.role)
        adapter.on("developer_1", tracked)
        adapter.on("developer_2", tracked)
        return {"scripted": adapter}
    return factory


async def test_two_projects_isolated_under_one_daemon(central, tmp_path):
    central.cfg.limits.max_total_dev_concurrency = 1
    reg = ProjectRegistry(central)
    a, b = make_repo(tmp_path, "a"), make_repo(tmp_path, "b")
    ca = await _prepare(reg, a, "anvikra")
    cb = await _prepare(reg, b, "yantra")
    stats = {"active": 0, "max": 0, "roles": [], "running_roles": [], "concurrent_roles": []}
    res = await Supervisor(reg, _tracking_factory(stats), rescan_s=0.1).run(until_idle=True, max_seconds=90)
    assert set(res.projects) == {"anvikra", "yantra"}
    assert all("all tasks completed" in v for v in res.projects.values()), res.projects
    # one shared engine honoured the global developer cap across projects
    assert stats["max"] == 1 and len(stats["roles"]) == 4
    for c, repo, other in ((ca, a, "yantra"), (cb, b, "anvikra")):
        with c.app.db.session() as s:
            assert {p.name for p in s.scalars(select(m.Project))} == {c.name}
            assert {t.state for t in s.scalars(select(m.Task))} == {"COMPLETED"}
            assert all(e.cwd.startswith(str(c.home)) or e.cwd == str(repo) for e in s.scalars(select(m.Execution)))
        # branches only in the project's own repository, namespaced by project
        branches = git(repo, "branch", "--list", "wf/*")
        assert f"wf/{c.name}/integration" in branches and f"wf/{other}/" not in branches
        assert (c.home / "worktrees" / c.name).is_dir() and not (c.home / "worktrees" / other).exists()
        log = (c.home / "logs" / "workforce.jsonl").read_text()
        assert '"project": "%s"' % c.name in log and '"project": "%s"' % other not in log
        assert "ranking.py" in git(repo, "ls-tree", "-r", "--name-only", f"wf/{c.name}/integration")
    with central.db.session() as s:
        assert s.scalars(select(m.Task)).first() is None, "central DB holds only the registry"
        assert {p.name for p in s.scalars(select(m.Project))} == {"anvikra", "yantra"}


async def test_global_pause_applies_to_all_projects(central, tmp_path):
    reg = ProjectRegistry(central)
    await _prepare(reg, make_repo(tmp_path, "a"), "anvikra")
    from workforce.engine import control
    with central.db.session() as s:
        control.request_run_state(s, "paused", "human")

    sup = Supervisor(reg, lambda cfg: {"scripted": build_adapter({})}, rescan_s=0.1)

    async def stop_later():
        await asyncio.sleep(1.0)
        with central.db.session() as s:
            control.request_run_state(s, "stopping", "human")
    # the supervisor resets the engine flag to running on start, so pause after it starts
    async def pause_soon():
        await asyncio.sleep(0.05)
        with central.db.session() as s:
            control.request_run_state(s, "paused", "human")
    res, *_ = await asyncio.gather(sup.run(until_idle=True, max_seconds=20), pause_soon(), stop_later())
    assert res.reason == "stop requested"
    c = reg.context(reg.entry("anvikra"))
    with c.app.db.session() as s:
        assert not any(t.state == "COMPLETED" for t in s.scalars(select(m.Task)))


# ---------------------------------------------------------------- workspace boundaries
async def test_agents_cannot_touch_other_workspaces(central, tmp_path):
    reg = ProjectRegistry(central)
    a, b = make_repo(tmp_path, "a"), make_repo(tmp_path, "b")
    ca = await _prepare(reg, a, "anvikra")
    reg.register(b, name="yantra")

    def factory(cfg):
        adapter = build_adapter({})
        dev = adapter.handlers["developer_1"]
        calls = {"n": 0}

        async def escaping(req):
            calls["n"] += 1
            if calls["n"] == 1:
                (b / "README.md").write_text("tampered by an agent of another project\n")
            return await dev(req)
        adapter.on("developer_1", escaping)
        adapter.on("developer_2", escaping)
        return {"scripted": adapter}

    shared = SharedRuntime.from_registry(reg)
    orch = Orchestrator(ca.app, adapters=factory(None), shared=shared, project_name="anvikra")
    # cwd outside the project's workspaces is refused before any agent starts
    res = await orch.registry.execute(Role.DEVELOPER_1, "x", b, purpose="implement")
    assert res.error_kind == "policy" and "outside the project's workspaces" in res.error
    await orch.run(until_idle=True, max_seconds=60)
    with ca.app.db.session() as s:
        tasks = list(s.scalars(select(m.Task)))
        assert any("outside its assigned workspace" in json.dumps(t.feedback) for t in tasks)
        assert s.scalars(select(m.Approval).where(m.Approval.kind == "workspace_violation")).first() is not None
        assert {t.state for t in tasks} == {"COMPLETED"}  # the rejected attempt was corrected
    assert "tampered" not in git(a, "show", "wf/anvikra/integration:README.md")


# ---------------------------------------------------------------- preservation
def test_existing_database_and_projects_preserved(tmp_path):
    """A database created before multi-project support upgrades in place: rows, tasks and
    configuration survive, and the old project keeps working from the central database."""
    home = tmp_path / "old-home"
    home.mkdir()
    db = home / "workforce.db"
    acfg = AlembicConfig()
    acfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    acfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(acfg, "0001")
    old_repo = make_repo(tmp_path, "old")
    con = sqlite3.connect(db)
    con.execute("insert into projects (name, repo_path, test_command, status, created_at) "
                "values ('legacy', ?, 'python -m pytest -q', 'executing', '2026-11-18')", (str(old_repo),))
    con.execute("insert into tasks (project_id, key, title, description, state, requirement_keys, decision_keys, "
                "depends_on, owned_paths, acceptance_criteria, attempts, failure_signatures, feedback, created_at, "
                "updated_at) values (1, 'T-001', 'old task', '', 'COMPLETED', '[]', '[]', '[]', '[]', '[]', 0, '[]', "
                "'[]', '2026-11-18', '2026-11-18')")
    con.execute("insert into role_status (role, provider, model, available, detail) "
                "values ('architect', 'claude_code', 'claude-opus-5-5', 1, 'verified')")
    con.commit()
    con.close()
    central = build_app(cfg=central_cfg(home), stderr_logs=False)  # runs migration 0002
    reg = ProjectRegistry(central)
    legacy = reg.context(reg.entry("legacy"))
    assert legacy.legacy and legacy.app is central and legacy.home == home
    with central.db.session() as s:
        assert s.scalars(select(m.Task)).one().title == "old task"
        assert s.get(m.RoleStatus, "architect").available
    new = reg.register(make_repo(tmp_path, "new"), name="anvikra")
    assert new.isolated and {e.name for e in reg.entries()} == {"legacy", "anvikra"}
    with new.app.db.session() as s:
        assert s.scalars(select(m.Task)).first() is None
    # pre-existing projects have no marker: discovered from their repository path instead
    assert not (old_repo / MARKER).exists()
    assert reg.resolve(cwd=old_repo / "src" / "deep").name == "legacy"
    assert reg.resolve("legacy").project_id == 1


# ---------------------------------------------------------------- API
async def test_api_serves_each_project_from_its_own_state(central, tmp_path):
    reg = ProjectRegistry(central)
    ca = await _prepare(reg, make_repo(tmp_path, "a"), "anvikra")
    cb = reg.register(make_repo(tmp_path, "b"), name="yantra")
    c = TestClient(create_api(central, static_dir=None, registry=reg))
    ov = c.get("/api/overview").json()
    assert ov["multi_project"] and {p["name"] for p in ov["projects"]} == {"anvikra", "yantra"}
    ids = {p["name"]: p["id"] for p in ov["projects"]}
    assert len(c.get("/api/tasks", params={"project": ids["anvikra"]}).json()) == 2
    assert c.get("/api/tasks", params={"project": ids["yantra"]}).json() == []
    tr = c.get("/api/traceability", params={"project": ids["anvikra"]}).json()
    assert {r["requirement"] for r in tr} >= {"FR-1", "FR-2", "FR-3"}
    assert c.get("/api/traceability", params={"project": ids["yantra"]}).json() == []
    assert c.get("/api/tasks", params={"project": 999}).status_code == 404
    H = {"X-Workforce-Client": "dashboard", "Content-Type": "application/json"}
    r = c.post("/api/control/pause", params={"project": ids["yantra"]}, json={}, headers=H).json()
    assert r == {"run_state": "paused", "scope": "yantra"}
    from workforce.engine import control
    with ca.app.db.session() as s:
        assert control.get_flag(s, "run_state") != "paused"
    with cb.app.db.session() as s:
        assert control.get_flag(s, "run_state") == "paused"
