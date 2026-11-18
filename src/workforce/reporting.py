"""Progress reports generated strictly from persisted orchestration state."""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from workforce.config import WorkforceConfig
from workforce.db.models import Approval, Execution, Project, Review, RoleStatus, Task, TestRun, utcnow
from workforce.docs.traceability import build_matrix


def usage_summary(s: Session, project_id: int | None = None) -> list[dict]:
    q = select(Execution)
    if project_id:
        q = q.where(Execution.project_id == project_id)
    agg: dict[tuple, dict] = defaultdict(lambda: {"calls": 0, "ok": 0, "failed": 0, "input_tokens": 0,
                                                  "output_tokens": 0, "seconds": 0.0, "est_cost_usd": 0.0})
    for ex in s.scalars(q):
        a = agg[(ex.role, ex.provider, ex.model)]
        a["calls"] += 1
        a["ok" if ex.status == "succeeded" else "failed"] += 1
        u = ex.usage or {}
        a["input_tokens"] += int(u.get("input_tokens") or 0)
        a["output_tokens"] += int(u.get("output_tokens") or 0)
        a["seconds"] += ex.duration_s or 0
        a["est_cost_usd"] += float(u.get("estimated_list_cost_usd") or u.get("cost_usd") or 0)
    return [{"role": r, "provider": p, "model": m, **v} for (r, p, m), v in sorted(agg.items())]


def progress_report(s: Session, cfg: WorkforceConfig, project_id: int | None = None) -> str:
    projects = [s.get(Project, project_id)] if project_id else list(s.scalars(select(Project)))
    out = [f"# Coding Workforce progress report", f"Generated {utcnow().isoformat()} from the live state database.", ""]
    out += ["## Agents", "| role | desired model | provider | model id | available | detail |", "|---|---|---|---|---|---|"]
    st = {r.role: r for r in s.scalars(select(RoleStatus))}
    for role, rc in cfg.roles.items():
        r = st.get(role.value)
        out.append(f"| {role.value} | {rc.desired_model} | {rc.provider} | {rc.model} | "
                   f"{'-' if r is None else ('yes' if r.available else 'NO')} | {(r.detail if r else 'not verified')[:80]} |")
    for p in projects:
        tasks = list(s.scalars(select(Task).where(Task.project_id == p.id).order_by(Task.id)))
        c = Counter(t.state for t in tasks)
        out += ["", f"## Project `{p.name}` — {p.status}", "",
                "Task states: " + (", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "no tasks"), "",
                "| task | state | role | attempts | requirements | last event |", "|---|---|---|---|---|---|"]
        for t in tasks:
            last = t.events[-1].reason[:90] if t.events else ""
            out.append(f"| {t.key} | {t.state} | {t.assigned_role or '-'} | {t.attempts} | "
                       f"{', '.join(t.requirement_keys)} | {last} |")
        blocked = [t for t in tasks if t.state == "BLOCKED"]
        if blocked:
            out += ["", "### Blockers"] + [f"- **{t.key}**: {t.blocked_reason}" for t in blocked]
        runs = list(s.scalars(select(TestRun).where(TestRun.project_id == p.id).order_by(TestRun.id)))
        out += ["", f"### Verification: {sum(r.passed for r in runs)}/{len(runs)} test runs passed"]
        reviews = list(s.scalars(select(Review).join(Task).where(Task.project_id == p.id)))
        out.append("Review verdicts: " + (", ".join(f"{k}={v}" for k, v in Counter(r.verdict for r in reviews).items()) or "none"))
        rows = build_matrix(s, p.id)
        out.append("Requirement status: " + ", ".join(f"{k}={v}" for k, v in Counter(r["final_status"] for r in rows).items()))
        pend = list(s.scalars(select(Approval).where(Approval.project_id == p.id, Approval.status == "pending")))
        if pend:
            out += ["", "### Pending approvals"] + [f"- #{a.id} {a.kind}: {a.subject}" for a in pend]
    out += ["", "## Provider usage (as reported by each CLI; costs are list-price estimates, not subscription charges)",
            "| role | provider | model | calls | ok | failed | in tok | out tok | seconds | est. cost |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for u in usage_summary(s, project_id):
        out.append(f"| {u['role']} | {u['provider']} | {u['model']} | {u['calls']} | {u['ok']} | {u['failed']} | "
                   f"{u['input_tokens']} | {u['output_tokens']} | {u['seconds']:.0f} | ${u['est_cost_usd']:.2f} |")
    return "\n".join(out)
