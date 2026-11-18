"""Runtime state shared by every project scheduled in one engine process.

- Provider concurrency semaphores and hourly call budgets are global, so two
  projects cannot together exceed a provider's limits.
- Role availability and provider circuit breakers live in the central DB.
- A developer role works on one task at a time across all projects, and the
  total number of concurrent development tasks is capped by
  `limits.max_total_dev_concurrency`.
- `guarded_repos` are checked around every agent run to detect writes outside
  the agent's assigned workspace.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from workforce.config import WorkforceConfig, engine_root
from workforce.db.session import Database


@dataclass
class SharedRuntime:
    cfg: WorkforceConfig
    state_db: Database
    semaphores: dict[str, asyncio.Semaphore]
    budget_dbs: list[Database]
    guarded_repos: list[Path]
    busy_roles: dict[str, set[str]] = field(default_factory=dict)
    active_dev: dict[str, int] = field(default_factory=dict)

    @classmethod
    def build(cls, cfg: WorkforceConfig, state_db: Database, project_dbs: list[Database] | None = None,
              repos: list[Path] | None = None) -> "SharedRuntime":
        sems = {name: asyncio.Semaphore(pc.max_concurrency) for name, pc in cfg.providers.items()}
        dbs: list[Database] = []
        for d in [state_db, *(project_dbs or [])]:
            if all(d.path != x.path for x in dbs):
                dbs.append(d)
        guarded = sorted({Path(r).resolve() for r in (repos or [])} | {engine_root()})
        return cls(cfg, state_db, sems, dbs, guarded)

    @classmethod
    def from_registry(cls, registry) -> "SharedRuntime":
        contexts = registry.contexts()
        return cls.build(registry.central.cfg, registry.central.db, [c.app.db for c in contexts],
                         [c.repo for c in contexts])

    def add_project(self, db: Database, repo: Path) -> None:
        if all(db.path != x.path for x in self.budget_dbs):
            self.budget_dbs.append(db)
        r = Path(repo).resolve()
        if r not in self.guarded_repos:
            self.guarded_repos.append(r)

    # ---- cross-project developer accounting ----------------------------
    def publish(self, project: str, busy: set[str], active: int) -> None:
        self.busy_roles[project] = set(busy)
        self.active_dev[project] = active

    def withdraw(self, project: str) -> None:
        self.busy_roles.pop(project, None)
        self.active_dev.pop(project, None)

    def busy_elsewhere(self, project: str) -> set[str]:
        return set().union(*(v for k, v in self.busy_roles.items() if k != project))

    def capacity_for(self, project: str, project_limit: int) -> int:
        others = sum(v for k, v in self.active_dev.items() if k != project)
        return max(0, min(project_limit, self.cfg.limits.max_total_dev_concurrency - others))
