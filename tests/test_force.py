"""CW-004: the global `force` command and the YANTRA default project.

All engines here use a temporary engine root and scripted agents; no real
provider CLI is invoked."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from tests.test_multiproject import central_cfg, make_repo
from workforce.app import build_app
from workforce.db import models as m
from workforce.engine.orchestrator import lock_holder
from workforce.force import split_globals
from workforce.projects import ProjectError, ProjectRegistry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A temporary engine installation (central config + state) with YANTRA and one other project."""
    cfg = central_cfg(tmp_path / "engine-state")
    data = json.loads(cfg.model_dump_json())
    data["home"] = str(cfg.home)
    cfgp = tmp_path / "central.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("WORKFORCE_CONFIG", str(cfgp))
    central = build_app(cfg=cfg, stderr_logs=False)
    reg = ProjectRegistry(central)
    yantra = make_repo(tmp_path, "Yantra")
    other = make_repo(tmp_path, "Other")
    reg.register(yantra, name="yantra")
    reg.register(other, name="anvikra")
    home = tmp_path / "fakehome"
    home.mkdir()
    return {"reg": reg, "central": central, "yantra": yantra, "other": other, "home": home, "cfg": cfgp,
            "tmp": tmp_path}


def force(*args: str, cwd: Path, extra_env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run([sys.executable, "-m", "workforce.force", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=timeout)


def project_state(reg: ProjectRegistry, name: str) -> tuple[str, str | None]:
    from workforce.engine import control
    c = reg.context(reg.entry(name))
    with c.app.db.session() as s:
        return s.get(m.Project, c.project_id).status, control.get_flag(s, "run_state")


def test_split_globals():
    assert split_globals([]) == ([], [])
    assert split_globals(["--project", "/a", "status"]) == (["--project", "/a"], ["status"])
    assert split_globals(["--project=/a"]) == (["--project=/a"], [])
    assert split_globals(["plan", "--feedback", "x"]) == ([], ["plan", "--feedback", "x"])


def test_yantra_is_the_fallback_project(engine):
    reg = engine["reg"]
    outside = engine["home"]
    c = reg.resolve(cwd=outside)
    assert c.name == "yantra" and c.source == "default project"
    # the directory you are in always wins
    assert reg.resolve(cwd=engine["other"] / "src").name == "anvikra"
    # `project use` beats the default; a configured default beats the built-in YANTRA fallback
    reg.use("anvikra")
    assert reg.resolve(cwd=outside).name == "anvikra"
    reg.use(None)
    reg.set_default("anvikra")
    assert reg.resolve(cwd=outside).name == "anvikra"
    reg.set_default(None)
    assert reg.resolve(cwd=outside).name == "yantra"
    with pytest.raises(ProjectError):
        reg.set_default("does-not-exist")


def test_help_and_passthrough_commands_from_every_location(engine):
    for cwd in (engine["home"], Path("/tmp"), engine["yantra"]):
        r = force("--help", cwd=cwd)
        assert r.returncode == 0, r.stderr
        assert "force <command>" in r.stdout and "status" in r.stdout and "plan" in r.stdout
        r = force("status", "--json", cwd=cwd)
        assert r.returncode == 0, r.stderr
        assert {p["name"] for p in json.loads(r.stdout)["projects"]} == {"yantra", "anvikra"}
        r = force("project", "status", "--json", cwd=cwd)
        assert json.loads(r.stdout)["name"] == "yantra"
    # inside another registered repository the active project is that one, not YANTRA
    r = force("project", "status", "--json", cwd=engine["other"])
    assert json.loads(r.stdout)["name"] == "anvikra"
    # the existing `workforce` command and its subcommands are unchanged
    r = subprocess.run([sys.executable, "-m", "workforce.cli", "project", "list"], cwd=engine["home"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "yantra" in r.stdout and "anvikra" in r.stdout


def test_pause_resume_stop_without_running_engine(engine):
    reg = engine["reg"]
    r = force("pause", cwd=engine["home"])
    assert r.returncode == 0 and "yantra: run state -> paused" in r.stdout and "no engine is running" in r.stdout
    assert project_state(reg, "yantra")[1] == "paused"
    r = force("resume", cwd=Path("/tmp"))
    assert "yantra: run state -> running" in r.stdout
    r = force("stop", cwd=engine["yantra"])
    assert "yantra: run state -> stopped" in r.stdout
    assert project_state(reg, "yantra")[1] == "stopped"
    assert project_state(reg, "anvikra")[1] is None, "other projects are untouched"


def test_plan_refuses_cleanly_without_approved_requirements(engine):
    r = force("plan", cwd=engine["home"])
    assert r.returncode == 1 and "no approved requirements" in r.stdout
    assert project_state(engine["reg"], "yantra")[0] == "registered", "refused plan leaves status untouched"


def test_plan_runs_for_yantra_with_scripted_agents(engine):
    yantra = engine["yantra"]
    assert force("project", "ingest", str(yantra / "requirements"), cwd=yantra).returncode == 0
    assert force("req", "approve", cwd=yantra).returncode == 0
    r = force("plan", cwd=engine["home"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "architecture proposed for yantra" in r.stdout
    c = engine["reg"].context(engine["reg"].entry("yantra"))
    with c.app.db.session() as s:
        assert len(s.scalars(select(m.Task)).all()) == 2
    other = engine["reg"].context(engine["reg"].entry("anvikra"))
    with other.app.db.session() as s:
        assert s.scalars(select(m.Task)).first() is None


def _hold(lock: Path):
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def test_bare_force_refuses_duplicates(engine):
    c = engine["reg"].context(engine["reg"].entry("yantra"))
    held = _hold(c.app.cfg.home / "engine.lock")
    try:
        assert lock_holder(c.app.cfg.home / "engine.lock") == os.getpid()
        for cwd in (engine["home"], Path("/tmp"), engine["yantra"]):
            r = force(cwd=cwd)
            assert r.returncode == 0, r.stderr
            assert f"already running (pid {os.getpid()})" in r.stdout and "not starting another" in r.stdout
    finally:
        held.close()
    sup = _hold(engine["central"].cfg.home / "supervisor.lock")
    try:
        r = force(cwd=engine["home"])
        assert "multi-project engine is already running" in r.stdout
    finally:
        sup.close()
    assert lock_holder(c.app.cfg.home / "engine.lock") is None


def test_bare_force_starts_one_background_engine(engine):
    """Executing project with slow scripted developers: two `force` calls give exactly one engine."""
    yantra = engine["yantra"]
    env = {"WORKFORCE_SELFTEST_FAULTS": json.dumps({"slow_dev": 2})}
    assert force("project", "ingest", str(yantra / "requirements"), cwd=yantra).returncode == 0
    assert force("req", "approve", cwd=yantra).returncode == 0
    assert force("plan", cwd=yantra).returncode == 0
    r = force("approvals", cwd=yantra)
    aid = next(line.split()[1] for line in r.stdout.splitlines() if "architecture" in line)
    assert force("approve", aid, cwd=yantra).returncode == 0
    c = engine["reg"].context(engine["reg"].entry("yantra"))
    lock = c.app.cfg.home / "engine.lock"
    first = force(cwd=engine["home"], extra_env=env)
    assert first.returncode == 0 and "started the workforce for yantra in the background" in first.stdout, first.stdout
    pid = lock_holder(lock)
    assert pid
    second = force(cwd=Path("/tmp"), extra_env=env)
    assert f"already running (pid {pid})" in second.stdout
    third = force("start", cwd=yantra, extra_env=env)
    assert "already running" in third.stdout
    r = force("stop", cwd=yantra)
    assert "run state -> stopping" in r.stdout
    deadline = time.time() + 60
    while lock_holder(lock) is not None and time.time() < deadline:
        time.sleep(0.2)
    assert lock_holder(lock) is None, "engine stopped gracefully"
    log = (c.app.cfg.log_dir / "engine.out.log").read_text()
    assert log.count("engine stopped (yantra)") == 1, "exactly one engine ran"


def test_install_script_links_force_and_keeps_workforce(tmp_path):
    bindir = tmp_path / "bin"
    r = subprocess.run([str(ROOT / "scripts" / "install_global.sh")], env={**os.environ, "WORKFORCE_BIN_DIR": str(bindir)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (bindir / "force").is_symlink() and (bindir / "workforce").is_symlink()
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIRTUAL_ENV")}
    env["PATH"] = f"{bindir}:/usr/bin:/bin"
    for cmd in ("force", "workforce"):
        r = subprocess.run([cmd, "--help"], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert r.returncode == 0 and "project" in r.stdout, r.stderr
    # never overwrite a foreign `force`
    (bindir / "force").unlink()
    (bindir / "force").write_text("#!/bin/sh\necho mine\n")
    r = subprocess.run([str(ROOT / "scripts" / "install_global.sh")], env={**os.environ, "WORKFORCE_BIN_DIR": str(bindir)},
                       capture_output=True, text=True)
    assert r.returncode == 1 and "refusing to overwrite" in r.stderr
    assert (bindir / "force").read_text().endswith("echo mine\n")
