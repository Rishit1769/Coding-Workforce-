from __future__ import annotations

import json
import plistlib

from sqlalchemy import select
from typer.testing import CliRunner

from tests.test_e2e_pipeline import git, setup, tasks
from workforce import daemon
from workforce.cli import app as cli
from workforce.db.models import Approval
from workforce.engine import control
from workforce.engine.orchestrator import Orchestrator, approve
from workforce.reporting import progress_report
from workforce.selftest.scenario import build_adapter

runner = CliRunner()


def _cfg_file(tmp_path, app):
    data = json.loads(app.cfg.model_dump_json())
    data["home"] = str(app.cfg.home)
    p = tmp_path / "wf.yaml"
    import yaml
    p.write_text(yaml.safe_dump(data))
    return p


async def test_pause_resume_stop_flags(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path, {"slow_dev": 0.3})
    with app.db.session() as s:
        control.request_run_state(s, "paused", "human")
    summary = await orch.run(until_idle=True, max_seconds=1.5)
    assert summary.reason == "time limit"
    assert all(t.state == "BACKLOG" for t in tasks(app).values()), "nothing scheduled while paused"
    with app.db.session() as s:
        control.request_run_state(s, "running", "human")
    orch2 = Orchestrator(app, adapters={"scripted": build_adapter({})})
    await orch2.run(until_idle=True, max_seconds=60)
    assert all(t.state == "COMPLETED" for t in tasks(app).values())
    import asyncio
    run = asyncio.create_task(Orchestrator(app, adapters={"scripted": build_adapter({})}).run(until_idle=False,
                                                                                              max_seconds=10))
    await asyncio.sleep(0.3)
    with app.db.session() as s:
        control.request_run_state(s, "stopping", "human")
    s3 = await run
    assert s3.reason == "stop requested"
    with app.db.session() as s:
        assert control.get_flag(s, "run_state") == "stopped"


async def test_cli_retry_cancel_report_promote(tmp_path):
    app, orch, repo, adapter, pid = await setup(tmp_path, {"always_buggy": True})
    await orch.run(until_idle=True, max_seconds=60)
    cfgp = _cfg_file(tmp_path, app)
    assert tasks(app)["T-001"].state == "BLOCKED"
    r = runner.invoke(cli, ["task", "retry", "T-001", "--config", str(cfgp)])
    assert r.exit_code == 0, r.output
    assert tasks(app)["T-001"].state == "READY"
    r = runner.invoke(cli, ["task", "cancel", "T-001", "--config", str(cfgp)])
    assert r.exit_code == 0, r.output
    assert tasks(app)["T-001"].state == "CANCELLED"
    r = runner.invoke(cli, ["report", "textstats", "--config", str(cfgp)])
    assert r.exit_code == 0 and "T-002 | COMPLETED" in r.output and "Provider usage" in r.output
    # promotion is gated on an explicit approval
    r = runner.invoke(cli, ["integrate", "promote", "textstats", "--config", str(cfgp)])
    assert r.exit_code == 2
    await Orchestrator(app, adapters={"scripted": build_adapter({})}).run(until_idle=True, max_seconds=30)
    with app.db.session() as s:
        aid = s.scalars(select(Approval).where(Approval.kind == "promote")).one().id
    approve(app, aid, True, "human")
    r = runner.invoke(cli, ["integrate", "promote", "textstats", "--config", str(cfgp)])
    assert r.exit_code == 0, r.output
    assert "ranking.py" in git(repo, "ls-tree", "-r", "--name-only", "main")
    assert "not pushed" in r.output


async def test_rollback_reverts_integration(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path)
    await orch.run(until_idle=True, max_seconds=60)
    cfgp = _cfg_file(tmp_path, app)
    r = runner.invoke(cli, ["integrate", "rollback", "textstats", "T-002", "--config", str(cfgp)])
    assert r.exit_code == 0, r.output
    files = git(repo, "ls-tree", "-r", "--name-only", "wf/textstats/integration")
    assert "textstats/ranking.py" not in files and "textstats/counting.py" in files
    assert tasks(app)["T-002"].state == "BLOCKED"


def test_launchd_plist_and_unattended_gate(cfg, tmp_path):
    cfgp = tmp_path / "workforce.yaml"
    cfgp.write_text("")
    pl = daemon.build_plist(cfg, cfgp)
    assert "--unattended" in pl["ProgramArguments"] and "--all-projects" in pl["ProgramArguments"] and pl["KeepAlive"] is False
    plistlib.dumps(pl)
    ok, why = daemon.unattended_allowed(None)
    assert not ok and "never been run" in why
    rec = daemon.safety_record([{"check": "x", "ok": True, "detail": ""}], {"ok": True, "detail": "ok"})
    assert daemon.unattended_allowed(json.dumps(rec))[0]
    rec["code_fingerprint"] = "stale"
    assert not daemon.unattended_allowed(json.dumps(rec))[0]


def test_unattended_run_refused_without_safety_check(tmp_path):
    p = tmp_path / "wf.yaml"
    p.write_text(f"home: {tmp_path / '.wf'}\n")
    r = runner.invoke(cli, ["run", "--unattended", "--config", str(p)])
    assert r.exit_code == 2 and "refused" in r.output


def test_config_checks_flag_unverified_roles(cfg):
    checks = {c["check"]: c["ok"] for c in daemon.config_checks(cfg, {"developer_1": True})}
    assert checks["dashboard bound to loopback"] and not checks["tester verified available"]


async def test_progress_report_uses_real_state(tmp_path):
    app, orch, repo, adapter, pid = await setup(tmp_path)
    await orch.run(until_idle=True, max_seconds=60)
    with app.db.session() as s:
        md = progress_report(s, app.cfg, pid)
    assert "T-001 | COMPLETED" in md and "verified=" in md
