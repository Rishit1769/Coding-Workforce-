"""Crash recovery: reconcile persisted state with git reality without
duplicating completed work."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from workforce.db.models import Project, Task
from workforce.engine.control import audit
from workforce.engine.state import TaskState, transition
from workforce.gitops.repo import GitRepo


def recover_project(s: Session, project: Project, git: GitRepo, integration_branch: str) -> list[dict]:
    """Called on engine start. Returns a list of recovery actions taken."""
    actions = []
    tasks = s.scalars(select(Task).where(Task.project_id == project.id)).all()
    for t in tasks:
        st = TaskState(t.state)
        if st == TaskState.INTEGRATING and t.head_commit and git.branch_exists(integration_branch) \
                and git.is_ancestor(t.head_commit, integration_branch):
            # Merge landed before the crash: do not integrate twice.
            t.integrated_commit = t.integrated_commit or git.rev(integration_branch)
            transition(s, t, TaskState.COMPLETED, "recovery: integration already present on integration branch")
            actions.append({"task": t.key, "action": "completed (already integrated)"})
        elif st == TaskState.IMPLEMENTING:
            # Uncommitted agent edits from an interrupted run are untrusted: discard them.
            if t.worktree_path and Path(t.worktree_path).exists():
                git.reset_worktree(Path(t.worktree_path), t.head_commit or t.base_commit or "HEAD")
            actions.append({"task": t.key, "action": "re-run implementation (partial edits discarded)"})
        elif st in (TaskState.TESTING, TaskState.REVIEWING, TaskState.INTEGRATING, TaskState.ASSIGNED):
            actions.append({"task": t.key, "action": f"resume {st.value}"})
    if actions:
        audit(s, "engine", "recovery", {"actions": actions}, project_id=project.id)
    return actions
