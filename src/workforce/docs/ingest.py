"""Ingestion pipeline: preserve originals, parse, extract requirements, detect
issues and injection attempts, and manage the human approval of requirements."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from workforce.config import WorkforceConfig
from workforce.db.models import Document, Issue, Project, Requirement
from workforce.docs.analysis import detect_issues
from workforce.docs.extractor import Candidate, content_hash, extract
from workforce.docs.parsers import SUPPORTED, parse_document
from workforce.engine.control import audit, request_approval
from workforce.security.injection import scan_for_injection


def iter_docs(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
                  and not any(part.startswith(".") for part in p.relative_to(path).parts))


def ingest(s: Session, cfg: WorkforceConfig, project: Project, source: Path) -> dict:
    store = cfg.home / "projects" / project.name / "documents"
    store.mkdir(parents=True, exist_ok=True)
    stats = {"documents": 0, "requirements_new": 0, "requirements_changed": 0, "issues": 0, "injection_findings": 0}
    all_cands: list[Candidate] = []
    for path in iter_docs(source):
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        doc = s.scalars(select(Document).where(Document.project_id == project.id, Document.sha256 == sha)).first()
        if doc is None:
            stored = store / f"{sha[:12]}-{path.name}"
            shutil.copy2(path, stored)
            stored.chmod(0o444)  # originals are preserved read-only
            doc = Document(project_id=project.id, original_path=str(path.resolve()), stored_path=str(stored),
                           sha256=sha, kind="pending")
            s.add(doc)
            s.flush()
        parsed = parse_document(path)
        doc.kind = parsed.kind
        stats["documents"] += 1
        for f in scan_for_injection(parsed.raw_text):
            stats["injection_findings"] += 1
            _add_issue(s, project.id, "injection", "major", [],
                       f"Possible prompt-injection content in {path.name} ({f['tag']}): \"{f['excerpt']}\". "
                       "It is treated as data and will not be executed.")
        cands = extract(parsed, path.stem)
        for c in cands:
            existing = s.scalars(select(Requirement).where(Requirement.project_id == project.id,
                                                           Requirement.key == c.key)).first()
            if existing is None:
                s.add(Requirement(project_id=project.id, key=c.key, document_id=doc.id, section=c.section, text=c.text,
                                  category=c.category, priority=c.priority, acceptance_criteria=c.acceptance_criteria,
                                  dependencies=c.dependencies, verification_method=c.verification_method,
                                  content_hash=c.content_hash))
                stats["requirements_new"] += 1
            elif existing.content_hash != c.content_hash:
                # Changed text for an explicit ID: needs re-approval, never silently accepted.
                existing.text, existing.content_hash = c.text, c.content_hash
                existing.acceptance_criteria = c.acceptance_criteria
                existing.status = "proposed"
                stats["requirements_changed"] += 1
        all_cands += cands
        s.flush()
    for iss in detect_issues(all_cands):
        if _add_issue(s, project.id, iss["kind"], iss["severity"], iss["requirement_keys"], iss["description"]):
            stats["issues"] += 1
    _mark_clarifications(s, project.id)
    audit(s, "engine", "docs.ingested", stats, project_id=project.id)
    return stats


def _add_issue(s: Session, pid: int, kind: str, severity: str, keys: list[str], desc: str) -> bool:
    dup = s.scalars(select(Issue).where(Issue.project_id == pid, Issue.description == desc)).first()
    if dup:
        return False
    iss = Issue(project_id=pid, kind=kind, severity=severity, requirement_keys=keys, description=desc)
    s.add(iss)
    s.flush()
    if severity == "major":
        request_approval(s, "clarification", f"Issue #{iss.id} ({kind}): {desc[:300]}", {"issue_id": iss.id, "requirement_keys": keys},
                         project_id=pid)
    return True


def _mark_clarifications(s: Session, pid: int) -> None:
    open_keys = set()
    for iss in s.scalars(select(Issue).where(Issue.project_id == pid, Issue.status == "open", Issue.severity == "major")):
        open_keys.update(iss.requirement_keys)
    for r in s.scalars(select(Requirement).where(Requirement.project_id == pid)):
        if r.key in open_keys and r.status == "proposed":
            r.status = "needs_clarification"


def resolve_issue(s: Session, issue_id: int, resolution: str, actor: str, amend: dict[str, str] | None = None) -> Issue:
    """Human resolution of an issue; optional amended requirement texts {key: new_text}."""
    from workforce.db.models import Approval

    iss = s.get(Issue, issue_id)
    if iss is None:
        raise KeyError(issue_id)
    iss.status, iss.resolution = "resolved", resolution
    for key, text in (amend or {}).items():
        r = s.scalars(select(Requirement).where(Requirement.project_id == iss.project_id, Requirement.key == key)).one()
        r.text, r.content_hash, r.status = text, content_hash(text), "amended"
    for a in s.scalars(select(Approval).where(Approval.kind == "clarification", Approval.status == "pending",
                                              Approval.project_id == iss.project_id)):
        if a.payload.get("issue_id") == issue_id:
            a.status, a.decided_by, a.comment = "approved", actor, resolution
    still_open = set()
    for other in s.scalars(select(Issue).where(Issue.project_id == iss.project_id, Issue.status == "open",
                                               Issue.severity == "major")):
        still_open.update(other.requirement_keys)
    for key in iss.requirement_keys:
        r = s.scalars(select(Requirement).where(Requirement.project_id == iss.project_id, Requirement.key == key)).first()
        if r and r.status == "needs_clarification" and key not in still_open:
            r.status = "proposed"
    audit(s, actor, "issue.resolved", {"issue": issue_id, "resolution": resolution, "amend": amend or {}},
          project_id=iss.project_id)
    return iss


def approve_requirements(s: Session, pid: int, actor: str, keys: list[str] | None = None) -> list[str]:
    """Approve proposed/amended requirements. Requirements with open major issues are never approved."""
    approved = []
    for r in s.scalars(select(Requirement).where(Requirement.project_id == pid)):
        if keys and r.key not in keys:
            continue
        if r.status in ("proposed", "amended"):
            r.status = "approved"
            approved.append(r.key)
    audit(s, actor, "requirements.approved", {"keys": approved}, project_id=pid)
    return approved


def reject_requirement(s: Session, pid: int, key: str, actor: str, reason: str) -> None:
    r = s.scalars(select(Requirement).where(Requirement.project_id == pid, Requirement.key == key)).one()
    r.status = "rejected"
    audit(s, actor, "requirement.rejected", {"key": key, "reason": reason}, project_id=pid)
