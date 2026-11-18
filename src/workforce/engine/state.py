"""Task state machine. All transitions go through `transition()` which
validates the edge, persists a TaskEvent and writes the audit log."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.orm import Session

from workforce.db.models import AuditLog, Task, TaskEvent, utcnow


class TaskState(StrEnum):
    BACKLOG = "BACKLOG"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    INTEGRATING = "INTEGRATING"
    COMPLETED = "COMPLETED"
    NEEDS_FIX = "NEEDS_FIX"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


S = TaskState
TERMINAL = {S.COMPLETED, S.CANCELLED, S.FAILED}
ACTIVE = {S.ASSIGNED, S.IMPLEMENTING, S.TESTING, S.REVIEWING, S.INTEGRATING}

_ALWAYS = {S.BLOCKED, S.PAUSED, S.CANCELLED, S.FAILED}

TRANSITIONS: dict[TaskState, set[TaskState]] = {
    S.BACKLOG: {S.READY} | _ALWAYS,
    S.READY: {S.ASSIGNED, S.BACKLOG} | _ALWAYS,
    S.ASSIGNED: {S.IMPLEMENTING, S.READY} | _ALWAYS,
    S.IMPLEMENTING: {S.TESTING, S.NEEDS_FIX, S.READY} | _ALWAYS,
    S.TESTING: {S.REVIEWING, S.NEEDS_FIX, S.TESTING} | _ALWAYS,
    S.REVIEWING: {S.INTEGRATING, S.NEEDS_FIX, S.REVIEWING} | _ALWAYS,
    S.INTEGRATING: {S.COMPLETED, S.NEEDS_FIX, S.INTEGRATING} | _ALWAYS,
    S.NEEDS_FIX: {S.IMPLEMENTING, S.ASSIGNED} | _ALWAYS,
    S.BLOCKED: {S.READY, S.BACKLOG, S.CANCELLED, S.NEEDS_FIX},
    S.PAUSED: {S.READY, S.BACKLOG, S.ASSIGNED, S.IMPLEMENTING, S.TESTING, S.REVIEWING, S.INTEGRATING, S.NEEDS_FIX,
               S.CANCELLED, S.BLOCKED},
    S.FAILED: {S.READY},
    S.COMPLETED: set(),
    S.CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


def transition(session: Session, task: Task, to: TaskState | str, reason: str = "", actor: str = "engine") -> None:
    to = TaskState(to)
    frm = TaskState(task.state)
    if to not in TRANSITIONS[frm]:
        raise InvalidTransition(f"{task.key}: {frm} -> {to} is not allowed")
    if to == S.PAUSED:
        task.paused_from = frm.value
    task.state = to.value
    task.updated_at = utcnow()
    if to == S.IMPLEMENTING and task.started_at is None:
        task.started_at = utcnow()
    if to == S.COMPLETED:
        task.completed_at = utcnow()
    if to == S.BLOCKED:
        task.blocked_reason = reason
    elif frm == S.BLOCKED:
        task.blocked_reason = None
    session.add(TaskEvent(task_id=task.id, from_state=frm.value, to_state=to.value, reason=reason[:4000]))
    session.add(AuditLog(actor=actor, action="task.transition", project_id=task.project_id, task_id=task.id,
                         details={"key": task.key, "from": frm.value, "to": to.value, "reason": reason[:1000]}))
    session.flush()
