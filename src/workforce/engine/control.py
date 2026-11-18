"""Control plane: run-state flags shared by CLI, API and orchestrator, plus
audit and approval helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from workforce.db.models import Approval, AuditLog, Control, utcnow

RUN_STATES = {"stopped", "running", "paused", "stopping"}


def get_flag(s: Session, key: str, default: str | None = None) -> str | None:
    row = s.get(Control, key)
    return row.value if row else default


def set_flag(s: Session, key: str, value: str) -> None:
    row = s.get(Control, key)
    if row:
        row.value = value
        row.updated_at = utcnow()
    else:
        s.add(Control(key=key, value=value))
    s.flush()


def request_run_state(s: Session, state: str, actor: str) -> None:
    if state not in RUN_STATES:
        raise ValueError(state)
    set_flag(s, "run_state", state)
    audit(s, actor, f"control.{state}", {})


def heartbeat(s: Session) -> None:
    set_flag(s, "heartbeat", datetime.now(UTC).isoformat())


def audit(s: Session, actor: str, action: str, details: dict, project_id: int | None = None, task_id: int | None = None) -> None:
    s.add(AuditLog(actor=actor, action=action, details=details, project_id=project_id, task_id=task_id))


def request_approval(s: Session, kind: str, subject: str, payload: dict | None = None,
                     project_id: int | None = None, task_id: int | None = None) -> Approval:
    existing = s.scalars(select(Approval).where(Approval.kind == kind, Approval.status == "pending",
                                                Approval.project_id == project_id, Approval.task_id == task_id,
                                                Approval.subject == subject)).first()
    if existing:
        return existing
    a = Approval(kind=kind, subject=subject, payload=payload or {}, project_id=project_id, task_id=task_id)
    s.add(a)
    s.flush()
    audit(s, "engine", "approval.requested", {"id": a.id, "kind": kind, "subject": subject}, project_id, task_id)
    return a


def decide_approval(s: Session, approval_id: int, approve: bool, actor: str, comment: str | None = None) -> Approval:
    a = s.get(Approval, approval_id)
    if a is None:
        raise KeyError(f"approval {approval_id} not found")
    if a.status != "pending":
        raise ValueError(f"approval {approval_id} already {a.status}")
    a.status = "approved" if approve else "rejected"
    a.decided_by = actor
    a.decided_at = utcnow()
    a.comment = comment
    audit(s, actor, "approval.decided", {"id": a.id, "kind": a.kind, "status": a.status, "comment": comment},
          a.project_id, a.task_id)
    s.flush()
    return a


def approval_status(s: Session, kind: str, project_id: int | None, task_id: int | None = None) -> str | None:
    a = s.scalars(select(Approval).where(Approval.kind == kind, Approval.project_id == project_id,
                                         Approval.task_id == task_id).order_by(Approval.id.desc())).first()
    return a.status if a else None
