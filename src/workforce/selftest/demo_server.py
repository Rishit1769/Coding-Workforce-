"""Seed a throw-away workforce home by running the real orchestration engine
(with scripted agents) on the sample project, then serve the control API and
dashboard. Used by the dashboard's Playwright tests.

    python -m workforce.selftest.demo_server --port 8799 [--home DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from sqlalchemy import select

from workforce.app import build_app
from workforce.config import Role, WorkforceConfig
from workforce.db.models import Approval, Project
from workforce.docs.ingest import approve_requirements, ingest
from workforce.engine.orchestrator import Orchestrator, approve
from workforce.engine.planning import run_architecture
from workforce.selftest.scenario import build_adapter, create_sample_project


async def seed(root: Path) -> object:
    cfg = WorkforceConfig(home=root / ".wf")
    for role in Role:
        cfg.roles[role].provider = "scripted"
        cfg.roles[role].model = f"scripted-{role.value}"
    cfg.limits.poll_interval_s = 0.02
    cfg.limits.provider_wait_s = 0.1
    cfg.require_integration_approval = True
    app = build_app(cfg=cfg, stderr_logs=False)
    repo, docs = create_sample_project(root)
    with app.db.session() as s:
        p = Project(name="textstats", repo_path=str(repo))
        s.add(p)
        s.flush()
        ingest(s, cfg, p, docs)
        approve_requirements(s, p.id, "human")
        pid = p.id
    orch = Orchestrator(app, adapters={"scripted": build_adapter({"buggy_first": True})})
    await run_architecture(app.db, cfg, orch.agents, pid)
    with app.db.session() as s:
        aid = s.scalars(select(Approval).where(Approval.kind == "architecture")).one().id
    approve(app, aid, True, "human")
    await orch.run(until_idle=True, max_seconds=60)  # stops with both tasks awaiting integration approval
    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--home", type=Path, default=None)
    args = ap.parse_args()
    root = args.home or Path(tempfile.mkdtemp(prefix="wf-demo-"))
    app = asyncio.run(seed(root))
    from workforce.api.server import serve

    serve(app, port=args.port)


if __name__ == "__main__":
    main()
