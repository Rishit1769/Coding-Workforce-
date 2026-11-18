"""End-to-end pipeline validation with deterministic scripted agents:
Documentation -> Planning -> Development -> Testing -> Review -> Integration ->
Completion, plus deliberate failure injection."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from workforce.app import build_app
from workforce.config import Role, WorkforceConfig
from workforce.db.models import Approval, Commit, Execution, Project, Review, Task, TaskEvent, TestRun
from workforce.docs.ingest import approve_requirements, ingest
from workforce.docs.traceability import build_matrix
from workforce.engine.orchestrator import Orchestrator, approve
from workforce.engine.planning import run_architecture
from workforce.selftest.scenario import build_adapter, create_sample_project


def make_cfg(home: Path) -> WorkforceConfig:
    cfg = WorkforceConfig(home=home)
    for role in Role:
        cfg.roles[role].provider = "scripted"
        cfg.roles[role].model = f"scripted-{role.value}"
    cfg.limits.poll_interval_s = 0.02
    cfg.limits.provider_wait_s = 0.1
    cfg.limits.shutdown_grace_s = 2
    cfg.require_integration_approval = False
    cfg.providers["scripted"].circuit_breaker.cooldown_s = 0
    cfg.providers["scripted"].max_retries = 0
    return cfg


async def setup(tmp_path, faults=None, cfg_mod=None):
    cfg = make_cfg(tmp_path / ".wf")
    if cfg_mod:
        cfg_mod(cfg)
    app = build_app(cfg=cfg, stderr_logs=False)
    repo, docs = create_sample_project(tmp_path)
    adapter = build_adapter(faults)
    with app.db.session() as s:
        p = Project(name="textstats", repo_path=str(repo), test_command="python -m pytest -q")
        s.add(p)
        s.flush()
        ingest(s, cfg, p, docs)
        approve_requirements(s, p.id, "human")
        pid = p.id
    orch = Orchestrator(app, adapters={"scripted": adapter})
    await run_architecture(app.db, cfg, orch.agents, pid)
    with app.db.session() as s:
        a = s.scalars(select(Approval).where(Approval.kind == "architecture")).one()
        aid = a.id
        assert s.get(Project, pid).status == "awaiting_architecture_approval"
    approve(app, aid, True, "human")
    return app, orch, repo, adapter, pid


def tasks(app):
    with app.db.session() as s:
        return {t.key: t for t in s.scalars(select(Task))}


def events(app, key):
    with app.db.session() as s:
        t = s.scalars(select(Task).where(Task.key == key)).one()
        return [(e.from_state, e.to_state, e.reason) for e in s.scalars(select(TaskEvent).where(TaskEvent.task_id == t.id))]


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout


async def test_happy_path_full_pipeline(tmp_path):
    app, orch, repo, adapter, pid = await setup(tmp_path)
    summary = await orch.run(until_idle=True, max_seconds=60)
    ts = tasks(app)
    assert {k: t.state for k, t in ts.items()} == {"T-001": "COMPLETED", "T-002": "COMPLETED"}
    assert "all tasks completed" in summary.reason
    # Developers worked in parallel in isolated worktrees on distinct branches
    assert ts["T-001"].assigned_role != ts["T-002"].assigned_role
    integ = "wf/textstats/integration"
    files = git(repo, "ls-tree", "-r", "--name-only", integ)
    for f in ("textstats/counting.py", "textstats/ranking.py", "docs/adr/ADR-001.md",
              "acceptance_tests/t_001/test_t_001_counting.py", "acceptance_tests/t_002/test_t_002_ranking.py"):
        assert f in files
    # main (protected) untouched; promotion requires approval
    assert "counting.py" not in git(repo, "ls-tree", "-r", "--name-only", "main")
    authors = git(repo, "log", "--format=%an", integ)
    assert "workforce/tester" in authors and "workforce/developer_1" in authors and "workforce/developer_2" in authors
    with app.db.session() as s:
        assert s.scalars(select(Approval).where(Approval.kind == "promote")).first() is not None
        assert s.get(Project, pid).status == "completed"
        kinds = {r.kind for r in s.scalars(select(TestRun))}
        assert {"acceptance", "regression", "integration_regression", "final_regression"} <= kinds
        rows = build_matrix(s, pid)
        assert {r["requirement"]: r["final_status"] for r in rows if r["requirement"].startswith("FR")} == \
               {"FR-1": "verified", "FR-2": "verified", "FR-3": "verified"}
        assert all(r["reviews"] and r["commits"] and r["tests"] for r in rows if r["requirement"].startswith("FR"))
    # Tester wrote acceptance tests before any developer call
    order = [c.role for c in adapter.calls]
    assert order.index("tester") < min(order.index("developer_1"), order.index("developer_2"))
    # Worktrees for completed tasks are cleaned up
    assert not (app.cfg.worktree_dir / "textstats" / "T-001").exists()


async def test_failing_test_triggers_correction(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path, {"buggy_first": True})
    await orch.run(until_idle=True, max_seconds=60)
    t = tasks(app)["T-001"]
    assert t.state == "COMPLETED" and t.attempts == 1
    assert t.feedback[0]["source"] == "tester" and t.feedback[0]["failures"]
    with app.db.session() as s:
        runs = s.scalars(select(TestRun).where(TestRun.task_id == t.id, TestRun.kind == "acceptance")).all()
        assert [r.passed for r in runs] == [False, True]
    dev_prompts = [c.prompt for c in adapter.calls if c.role == "developer_1" and "T-001" in c.prompt]
    assert "correction attempt" in dev_prompts[-1] and "word_count" in dev_prompts[-1]


async def test_requirement_violation_caught_by_reviewer(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path, {"violate_req": True})
    await orch.run(until_idle=True, max_seconds=60)
    t = tasks(app)["T-002"]
    assert t.state == "COMPLETED" and t.attempts == 1
    with app.db.session() as s:
        verdicts = [r.verdict for r in s.scalars(select(Review).where(Review.task_id == t.id).order_by(Review.id))]
        assert verdicts == ["CHANGES_REQUESTED", "APPROVED"]
        first = s.scalars(select(Review).where(Review.task_id == t.id)).first()
        assert first.findings[0]["requirement_keys"] == ["FR-3"] and first.findings[0]["evidence"]


async def test_architecture_violation_rejected_by_policy(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path, {"arch_violation": True})
    await orch.run(until_idle=True, max_seconds=60)
    t = tasks(app)["T-001"]
    assert t.state == "COMPLETED"
    assert t.feedback[0]["type"] == "architecture_violation" and t.feedback[0]["files"] == ["README.md"]
    assert git(repo, "show", "wf/textstats/integration:README.md").startswith("# textstats")


async def test_merge_conflict_resolved_by_devops(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path, {"conflict": True})
    await orch.run(until_idle=True, max_seconds=60)
    assert {k: t.state for k, t in tasks(app).items()} == {"T-001": "COMPLETED", "T-002": "COMPLETED"}
    assert any(c.role == "devops" and c.purpose == "merge_conflict" for c in adapter.calls)
    merged = git(repo, "show", "wf/textstats/integration:tests/test_unit.py")
    assert "test_wc" in merged and "test_tw" in merged and "<<<<<<<" not in merged


async def test_merge_conflict_unsafe_goes_back_to_developer(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path, {"conflict": True, "devops_unsafe": True})
    await orch.run(until_idle=True, max_seconds=60)
    ts = tasks(app)
    assert {k: t.state for k, t in ts.items()} == {"T-001": "COMPLETED", "T-002": "COMPLETED"}
    conflicted = [t for t in ts.values() if any(f.get("type") == "conflict" for f in t.feedback)]
    assert len(conflicted) == 1
    assert "<<<<<<<" not in git(repo, "show", "wf/textstats/integration:tests/test_unit.py")


async def test_agent_timeout_is_bounded_and_retried(tmp_path):
    def mod(cfg):
        cfg.roles[Role.DEVELOPER_1].timeout_s = 1
        cfg.roles[Role.DEVELOPER_2].timeout_s = 1
    app, orch, repo, adapter, _ = await setup(tmp_path, {"dev_timeout": True}, mod)
    t0 = time.monotonic()
    await orch.run(until_idle=True, max_seconds=60)
    assert time.monotonic() - t0 < 30
    ts = tasks(app)
    assert all(t.state == "COMPLETED" for t in ts.values())
    timed_out = [t for t in ts.values() if t.feedback and "timeout" in json.dumps(t.feedback[0])]
    assert len(timed_out) == 1
    with app.db.session() as s:
        assert s.scalars(select(Execution).where(Execution.status == "timeout")).first() is not None


async def test_provider_failure_circuit_breaker_and_recovery(tmp_path):
    def mod(cfg):
        cfg.providers["scripted"].circuit_breaker.failure_threshold = 2
    app, orch, repo, adapter, _ = await setup(tmp_path, {"reviewer_down": 3}, mod)
    await orch.run(until_idle=True, max_seconds=60)
    assert all(t.state == "COMPLETED" for t in tasks(app).values())
    with app.db.session() as s:
        failed = s.scalars(select(Execution).where(Execution.role == "reviewer", Execution.error_kind == "network")).all()
        assert len(failed) == 3
        from workforce.db.models import AuditLog
        assert s.scalars(select(AuditLog).where(AuditLog.action == "provider.circuit_open")).first() is not None


async def test_unavailable_tester_blocks_instead_of_substituting(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path, {"tester_unavailable": True})
    summary = await orch.run(until_idle=True, max_seconds=30)
    ts = tasks(app)
    assert all(t.state == "BLOCKED" for t in ts.values())
    assert all("tester" in (t.blocked_reason or "") for t in ts.values())
    assert not any(c.role.startswith("developer") for c in adapter.calls), "no unverified development"
    assert "no actionable work" in summary.reason
    ok, why = orch.registry.role_available(Role.TESTER)
    assert not ok and "model_unavailable" in why


async def test_bounded_correction_loop_blocks(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path, {"always_buggy": True})
    await orch.run(until_idle=True, max_seconds=60)
    t = tasks(app)["T-001"]
    assert t.state == "BLOCKED"
    # identical failure signature is detected early (before exhausting all attempts)
    assert "repeated identical failure" in t.blocked_reason and t.attempts == 1
    assert tasks(app)["T-002"].state == "COMPLETED"


async def test_correction_attempt_limit(tmp_path):
    app, orch, repo, adapter, _ = await setup(tmp_path, {"always_buggy": True})
    # make each failure look different so only the attempt limit can stop the loop
    from workforce.engine import pipeline as pl
    orig = pl.TaskPipeline.signature
    counter = {"n": 0}

    def unique_sig(item):
        counter["n"] += 1
        return f"{orig(item)}-{counter['n']}"
    pl.TaskPipeline.signature = staticmethod(unique_sig)
    try:
        await orch.run(until_idle=True, max_seconds=60)
    finally:
        pl.TaskPipeline.signature = staticmethod(orig)
    t = tasks(app)["T-001"]
    assert t.state == "BLOCKED" and t.attempts == 3 and "maximum automated correction attempts" in t.blocked_reason


async def test_integration_approval_gate_and_pause_resume(tmp_path):
    def mod(cfg):
        cfg.require_integration_approval = True
    app, orch, repo, adapter, _ = await setup(tmp_path, None, mod)
    summary = await orch.run(until_idle=True, max_seconds=60)
    ts = tasks(app)
    assert {t.state for t in ts.values()} == {"INTEGRATING"}
    assert "pending approvals=2" in summary.reason
    with app.db.session() as s:
        ids = [a.id for a in s.scalars(select(Approval).where(Approval.kind == "integration"))]
    for i in ids:
        approve(app, i, True, "human")
    await orch.run(until_idle=True, max_seconds=60)
    assert {t.state for t in tasks(app).values()} == {"COMPLETED"}


async def test_process_interruption_recovery(tmp_path):
    """Hard-cancel the engine mid-implementation, restart, and verify no duplicated work."""
    app, orch, repo, adapter, _ = await setup(tmp_path, {"slow_dev": 0.5})
    run = asyncio.create_task(orch.run(until_idle=True, max_seconds=60))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if any(t.state == "IMPLEMENTING" for t in tasks(app).values()):
            break
    # simulate a crash: the running execution row stays 'running', partial edits are left behind
    wt = app.cfg.worktree_dir / "textstats" / "T-001"
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    (wt / "textstats" / "partial_garbage.py").write_text("x = (")
    with app.db.session() as s:
        for ex in s.scalars(select(Execution).where(Execution.status == "cancelled")):
            ex.status = "running"
    orch2 = Orchestrator(app, adapters={"scripted": build_adapter({})})
    await orch2.run(until_idle=True, max_seconds=60)
    ts = tasks(app)
    assert all(t.state == "COMPLETED" for t in ts.values())
    with app.db.session() as s:
        assert s.scalars(select(Execution).where(Execution.status == "interrupted")).first() is not None
        merges = s.scalars(select(Commit).where(Commit.role == "devops")).all()
        assert len(merges) == 2, "each task integrated exactly once"
    assert "partial_garbage.py" not in git(repo, "ls-tree", "-r", "--name-only", "wf/textstats/integration")


def test_sigkill_engine_process_and_resume(tmp_path):
    """Real process interruption: SIGKILL a running `workforce run` and resume it."""
    cfg = make_cfg(tmp_path / ".wf")
    repo, docs = create_sample_project(tmp_path)
    data = json.loads(cfg.model_dump_json())
    data["home"] = str(cfg.home)
    (tmp_path / "workforce.yaml").write_text(yaml.safe_dump(data))
    env = {**os.environ, "WORKFORCE_CONFIG": str(tmp_path / "workforce.yaml"),
           "WORKFORCE_SELFTEST_FAULTS": json.dumps({"slow_dev": 3})}
    wf = [sys.executable, "-m", "workforce.cli"]
    run = lambda *a: subprocess.run([*wf, *a], env=env, capture_output=True, text=True, timeout=120)  # noqa: E731
    r = run("project", "create", "textstats", "--repo", str(repo), "--docs", str(docs))
    assert r.returncode == 0, r.stderr
    assert run("req", "approve", "textstats").returncode == 0
    r = run("plan", "textstats")
    assert r.returncode == 0, r.stdout + r.stderr
    app = build_app(cfg=cfg, stderr_logs=False)
    with app.db.session() as s:
        aid = s.scalars(select(Approval).where(Approval.kind == "architecture")).one().id
    assert run("approve", str(aid)).returncode == 0
    proc = subprocess.Popen([*wf, "run", "--until-idle"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 60
    while time.time() < deadline:
        if any(t.state == "IMPLEMENTING" for t in tasks(app).values()):
            break
        time.sleep(0.1)
    time.sleep(0.5)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    assert any(t.state == "IMPLEMENTING" for t in tasks(app).values())
    env["WORKFORCE_SELFTEST_FAULTS"] = "{}"
    r = subprocess.run([*wf, "run", "--until-idle"], env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(t.state == "COMPLETED" for t in tasks(app).values())
    with app.db.session() as s:
        assert s.scalars(select(Execution).where(Execution.status == "interrupted")).first() is not None
