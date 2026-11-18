"""Requirement traceability matrix:
Requirement -> Architecture Decision -> Task -> Commits -> Tests -> Review -> Final status."""

from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from workforce.db.models import Commit, Decision, Requirement, Review, Task, TestRun


def build_matrix(s: Session, pid: int) -> list[dict]:
    reqs = s.scalars(select(Requirement).where(Requirement.project_id == pid).order_by(Requirement.id)).all()
    decisions = s.scalars(select(Decision).where(Decision.project_id == pid)).all()
    tasks = s.scalars(select(Task).where(Task.project_id == pid)).all()
    rows = []
    for r in reqs:
        r_tasks = [t for t in tasks if r.key in (t.requirement_keys or [])]
        t_ids = [t.id for t in r_tasks]
        commits = s.scalars(select(Commit).where(Commit.task_id.in_(t_ids))).all() if t_ids else []
        tests = s.scalars(select(TestRun).where(TestRun.task_id.in_(t_ids)).order_by(TestRun.id)).all() if t_ids else []
        reviews = s.scalars(select(Review).where(Review.task_id.in_(t_ids)).order_by(Review.id)).all() if t_ids else []
        latest_tests = {}
        for tr in tests:
            latest_tests[(tr.task_id, tr.kind)] = tr
        latest_review = {}
        for rv in reviews:
            latest_review[rv.task_id] = rv
        if r.status == "rejected":
            final = "rejected"
        elif r.status != "approved" and r.status != "amended":
            final = f"not approved ({r.status})"
        elif not r_tasks:
            final = "uncovered"
        elif all(t.state == "COMPLETED" for t in r_tasks):
            final = "verified"
        elif any(t.state in ("BLOCKED", "FAILED") for t in r_tasks):
            final = "blocked"
        else:
            final = "in progress"
        rows.append({
            "requirement": r.key, "text": r.text, "status": r.status, "category": r.category,
            "decisions": [d.key for d in decisions if r.key in (d.requirement_keys or [])],
            "tasks": [{"key": t.key, "state": t.state} for t in r_tasks],
            "commits": [c.sha[:10] for c in commits],
            "tests": [{"task": next(t.key for t in r_tasks if t.id == tr.task_id), "kind": tr.kind, "passed": tr.passed}
                      for tr in latest_tests.values()],
            "reviews": [{"task": next(t.key for t in r_tasks if t.id == rv.task_id), "verdict": rv.verdict}
                        for rv in latest_review.values()],
            "final_status": final,
        })
        r.final_status = final
    return rows


def matrix_markdown(rows: list[dict]) -> str:
    out = ["| Requirement | Decisions | Tasks | Commits | Tests | Review | Final |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            r["requirement"], ", ".join(r["decisions"]) or "-",
            ", ".join(f"{t['key']} ({t['state']})" for t in r["tasks"]) or "-",
            ", ".join(r["commits"]) or "-",
            ", ".join(f"{t['task']}:{t['kind']}:{'pass' if t['passed'] else 'fail'}" for t in r["tests"]) or "-",
            ", ".join(f"{v['task']}:{v['verdict']}" for v in r["reviews"]) or "-",
            r["final_status"]))
    return "\n".join(out)


def matrix_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["requirement", "status", "decisions", "tasks", "commits", "tests", "reviews", "final_status", "text"])
    for r in rows:
        w.writerow([r["requirement"], r["status"], ";".join(r["decisions"]),
                    ";".join(f"{t['key']}:{t['state']}" for t in r["tasks"]), ";".join(r["commits"]),
                    ";".join(f"{t['task']}:{t['kind']}:{t['passed']}" for t in r["tests"]),
                    ";".join(f"{v['task']}:{v['verdict']}" for v in r["reviews"]), r["final_status"], r["text"]])
    return buf.getvalue()
