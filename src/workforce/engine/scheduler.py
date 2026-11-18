"""Dependency graph and deterministic scheduling policy.

The Manager agent may *suggest* an ordering/assignment; this module validates
it and enforces dependencies, file ownership and concurrency limits."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from workforce.config import DEVELOPER_ROLES, Role
from workforce.db.models import Task
from workforce.engine.state import ACTIVE, TaskState, transition
from workforce.security.policy import paths_overlap


class GraphError(Exception):
    pass


def validate_graph(tasks: list[dict] | list[Task]) -> list[str]:
    """Return a topological order of task keys; raise GraphError on missing deps or cycles."""
    deps = {}
    for t in tasks:
        key = t["key"] if isinstance(t, dict) else t.key
        d = t.get("depends_on", []) if isinstance(t, dict) else t.depends_on
        deps[key] = list(d or [])
    for k, ds in deps.items():
        for d in ds:
            if d not in deps:
                raise GraphError(f"task {k} depends on unknown task {d}")
            if d == k:
                raise GraphError(f"task {k} depends on itself")
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(k: str, stack: list[str]) -> None:
        if state.get(k) == 2:
            return
        if state.get(k) == 1:
            raise GraphError("dependency cycle: " + " -> ".join(stack + [k]))
        state[k] = 1
        for d in deps[k]:
            visit(d, stack + [k])
        state[k] = 2
        order.append(k)

    for k in deps:
        visit(k, [])
    return order


def project_tasks(s: Session, project_id: int) -> list[Task]:
    return list(s.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.id)))


def promote_ready(s: Session, project_id: int) -> list[str]:
    tasks = project_tasks(s, project_id)
    by_key = {t.key: t for t in tasks}
    promoted = []
    for t in tasks:
        if t.state != TaskState.BACKLOG:
            continue
        if all(by_key[d].state == TaskState.COMPLETED for d in t.depends_on if d in by_key):
            transition(s, t, TaskState.READY, "all dependencies completed")
            promoted.append(t.key)
    return promoted


@dataclass
class Assignment:
    task_key: str
    role: Role
    reason: str


def plan_assignments(s: Session, project_id: int, available_devs: list[Role], max_concurrency: int,
                     suggestion: list[dict] | None = None) -> list[Assignment]:
    """Pick READY tasks for free developer slots.

    `suggestion` is the Manager's proposed ordering ([{task_key, role}]); invalid
    entries are ignored and the deterministic order fills the remaining slots."""
    tasks = project_tasks(s, project_id)
    active = [t for t in tasks if t.state in ACTIVE or t.state == TaskState.NEEDS_FIX]
    # A developer is free once their task has passed review (INTEGRATING is DevOps work).
    busy_roles = {Role(t.assigned_role) for t in active if t.assigned_role and t.state != TaskState.INTEGRATING}
    free_roles = [r for r in available_devs if r not in busy_roles]
    slots = max(0, max_concurrency - len([t for t in active if t.state != TaskState.INTEGRATING]))
    ready = {t.key: t for t in tasks if t.state == TaskState.READY}
    owned_active = [t.owned_paths for t in active]
    out: list[Assignment] = []

    def try_assign(t: Task, role: Role | None, reason: str) -> bool:
        nonlocal slots
        if slots <= 0 or not free_roles:
            return False
        if any(paths_overlap(t.owned_paths, o) for o in owned_active):
            return False
        if role is None or role not in free_roles:
            pref = Role(t.preferred_role) if t.preferred_role in {r.value for r in DEVELOPER_ROLES} else None
            role = pref if pref in free_roles else free_roles[0]
        free_roles.remove(role)
        owned_active.append(t.owned_paths)
        slots -= 1
        out.append(Assignment(t.key, role, reason))
        ready.pop(t.key, None)
        return True

    for sug in suggestion or []:
        t = ready.get(str(sug.get("task_key")))
        role = sug.get("role")
        if t is None:
            continue
        r = Role(role) if role in {x.value for x in DEVELOPER_ROLES} else None
        try_assign(t, r, "manager suggestion")
    for t in list(ready.values()):
        try_assign(t, None, "deterministic order")
    return out
