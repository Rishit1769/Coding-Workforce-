"""Architecture & planning phase: run the Architect, validate its plan against
approved requirements and policy, persist ADRs/tasks, and gate on approval."""

from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import delete, select

from workforce.agents.roles import Agents
from workforce.config import Role, WorkforceConfig
from workforce.db.models import Decision, Issue, Project, Requirement, Task
from workforce.db.session import Database
from workforce.engine.control import audit, request_approval
from workforce.engine.scheduler import GraphError, validate_graph
from workforce.gitops.repo import GitRepo
from workforce.security.policy import _match

KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z0-9.\-]+$")


class PlanningError(Exception):
    pass


def approved_requirements(db: Database, pid: int) -> list[dict]:
    with db.session() as s:
        return [{"key": r.key, "text": r.text, "category": r.category, "priority": r.priority,
                 "acceptance_criteria": r.acceptance_criteria, "verification_method": r.verification_method,
                 "section": r.section}
                for r in s.scalars(select(Requirement).where(Requirement.project_id == pid,
                                                             Requirement.status.in_(["approved", "amended"])))]


def repo_summary(repo: Path, git: GitRepo, limit: int = 300) -> str:
    files = git.out("ls-files").splitlines()
    lines = [f"{len(files)} tracked files:"] + files[:limit]
    readme = repo / "README.md"
    if readme.exists():
        lines += ["", "README.md (head):", readme.read_text(errors="replace")[:3000]]
    return "\n".join(lines)


def validate_plan(plan: dict, approved_keys: set[str], cfg: WorkforceConfig) -> list[str]:
    errs: list[str] = []
    tasks = plan.get("tasks") or []
    if not tasks:
        errs.append("plan contains no tasks")
    keys = [t["key"] for t in tasks]
    if len(set(keys)) != len(keys):
        errs.append("duplicate task keys")
    dkeys = {d["key"] for d in plan.get("decisions") or []}
    covered: set[str] = set()
    for t in tasks:
        if not KEY_RE.match(t["key"]):
            errs.append(f"task key {t['key']!r} is not of the form T-001")
        unknown = set(t["requirement_keys"]) - approved_keys
        if not t["requirement_keys"]:
            errs.append(f"{t['key']} cites no requirements (tasks must not invent work)")
        if unknown:
            errs.append(f"{t['key']} cites unknown/unapproved requirements {sorted(unknown)}")
        covered |= set(t["requirement_keys"])
        if not t["owned_paths"]:
            errs.append(f"{t['key']} declares no owned_paths")
        for p in t["owned_paths"]:
            if _match(p.rstrip("*").rstrip("/") or p, cfg.policy.protected_paths) or _match(p, cfg.policy.protected_paths):
                errs.append(f"{t['key']} claims protected path {p}")
            if p.startswith("/") or ".." in p:
                errs.append(f"{t['key']} owned path {p} escapes the repository")
        bad_dec = set(t["decision_keys"]) - dkeys
        if bad_dec:
            errs.append(f"{t['key']} cites unknown decisions {sorted(bad_dec)}")
    missing = approved_keys - covered
    if missing:
        errs.append(f"approved requirements not covered by any task: {sorted(missing)}")
    try:
        validate_graph(tasks)
    except GraphError as e:
        errs.append(str(e))
    return errs


async def run_architecture(db: Database, cfg: WorkforceConfig, agents: Agents, project_id: int,
                           feedback: str = "") -> dict:
    with db.session() as s:
        p = s.get(Project, project_id)
        repo = Path(p.repo_path)
        open_major = s.scalars(select(Issue).where(Issue.project_id == project_id, Issue.status == "open",
                                                   Issue.severity == "major")).all()
        started = s.scalars(select(Task).where(Task.project_id == project_id,
                                               Task.state.notin_(["BACKLOG", "READY", "CANCELLED"]))).first()
        if started:
            raise PlanningError("tasks have already started; re-planning requires cancelling them first")
        p.status = "architecting"
    reqs = approved_requirements(db, project_id)
    if not reqs:
        raise PlanningError("no approved requirements; approve requirements before planning")
    git = GitRepo(repo, cfg.policy)
    summary = repo_summary(repo, git)
    if agents.reg.role_available(Role.WORKER)[0]:
        w = await agents.worker_explore(repo, project_id)
        if w.ok:
            summary += "\n\nLightweight worker exploration:\n" + json.dumps(w.data, indent=2)
    approved_keys = {r["key"] for r in reqs}
    errs: list[str] = []
    plan = None
    for _ in range(2):
        out = await agents.architect(project_id, repo, reqs, summary, feedback + "\n".join(errs))
        if not out.ok:
            with db.session() as s:
                s.get(Project, project_id).status = "architecture_failed"
                audit(s, "engine", "architecture.failed", {"error": out.error}, project_id=project_id)
            raise PlanningError(f"architect failed: {out.error}")
        plan = out.data
        errs = validate_plan(plan, approved_keys, cfg)
        if not errs:
            break
    if errs:
        with db.session() as s:
            s.get(Project, project_id).status = "architecture_failed"
            audit(s, "engine", "architecture.invalid", {"errors": errs}, project_id=project_id)
        raise PlanningError("architect plan failed validation: " + "; ".join(errs))
    assert plan is not None
    with db.session() as s:
        s.execute(delete(Task).where(Task.project_id == project_id))
        s.execute(delete(Decision).where(Decision.project_id == project_id))
        for d in plan["decisions"]:
            s.add(Decision(project_id=project_id, key=d["key"], title=d["title"], context=d["context"],
                           decision=d["decision"], consequences=d["consequences"],
                           requirement_keys=d["requirement_keys"]))
        for t in plan["tasks"]:
            s.add(Task(project_id=project_id, key=t["key"], title=t["title"], description=t["description"],
                       requirement_keys=t["requirement_keys"], decision_keys=t["decision_keys"],
                       depends_on=t["depends_on"], owned_paths=t["owned_paths"],
                       acceptance_criteria=t["acceptance_criteria"], preferred_role=t["preferred_role"]))
        for i in plan["issues"]:
            iss = Issue(project_id=project_id, kind=i["kind"], severity=i["severity"],
                        requirement_keys=i["requirement_keys"], description=f"[architect] {i['description']}")
            s.add(iss)
            s.flush()
            if i["severity"] == "major":
                request_approval(s, "clarification", f"Issue #{iss.id} ({i['kind']}): {i['description'][:300]}",
                                 {"issue_id": iss.id}, project_id=project_id)
        p = s.get(Project, project_id)
        art = cfg.home / "projects" / p.name
        art.mkdir(parents=True, exist_ok=True)
        (art / "architecture.json").write_text(json.dumps(plan, indent=2))
        (art / "architecture.md").write_text(render_architecture(plan))
        if cfg.require_architecture_approval:
            p.status = "awaiting_architecture_approval"
            request_approval(s, "architecture", f"Approve architecture for {p.name}: {len(plan['decisions'])} decisions, "
                             f"{len(plan['tasks'])} tasks", {"summary": plan["summary"], "risks": plan["risks"],
                                                             "open_issues_before_planning": len(open_major)},
                             project_id=project_id)
        else:
            approve_architecture(s, p, "engine(auto)")
        audit(s, "architect", "architecture.proposed", {"tasks": len(plan["tasks"]),
                                                         "decisions": len(plan["decisions"])}, project_id=project_id)
    return plan


def approve_architecture(s, project: Project, actor: str) -> None:
    for d in s.scalars(select(Decision).where(Decision.project_id == project.id)):
        d.status = "approved"
    project.status = "executing"
    audit(s, actor, "architecture.approved", {}, project_id=project.id)


def render_architecture(plan: dict) -> str:
    out = ["# Architecture", "", plan["summary"], "", "## Decisions"]
    for d in plan["decisions"]:
        out += [f"### {d['key']}: {d['title']}", f"Requirements: {', '.join(d['requirement_keys'])}", "",
                f"**Context.** {d['context']}", "", f"**Decision.** {d['decision']}", "",
                f"**Consequences.** {d['consequences']}", ""]
    out += ["## Tasks"]
    for t in plan["tasks"]:
        out += [f"### {t['key']}: {t['title']}", f"- requirements: {', '.join(t['requirement_keys'])}",
                f"- depends on: {', '.join(t['depends_on']) or '-'}", f"- owned paths: {', '.join(t['owned_paths'])}",
                f"- preferred role: {t['preferred_role']}", "", t["description"], ""]
    out += ["## Risks"] + [f"- {r}" for r in plan["risks"]]
    return "\n".join(out)
