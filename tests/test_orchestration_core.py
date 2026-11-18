from __future__ import annotations

import pytest

from tests.conftest import git
from workforce.config import Role
from workforce.db.models import Project, Task
from workforce.engine.recovery import recover_project
from workforce.engine.scheduler import GraphError, plan_assignments, promote_ready, validate_graph
from workforce.engine.state import TaskState, transition
from workforce.gitops.repo import GitRepo
from workforce.security.policy import PolicyViolation


def test_graph_validation():
    order = validate_graph([{"key": "B", "depends_on": ["A"]}, {"key": "A", "depends_on": []}])
    assert order.index("A") < order.index("B")
    with pytest.raises(GraphError):
        validate_graph([{"key": "A", "depends_on": ["B"]}, {"key": "B", "depends_on": ["A"]}])
    with pytest.raises(GraphError):
        validate_graph([{"key": "A", "depends_on": ["Z"]}])


def _mk(s, pid, key, deps=(), owned=("src/**",), pref=None):
    t = Task(project_id=pid, key=key, title=key, depends_on=list(deps), owned_paths=list(owned), preferred_role=pref)
    s.add(t)
    s.flush()
    return t


def test_promotion_and_assignment(wf):
    with wf.db.session() as s:
        p = Project(name="p", repo_path="/x")
        s.add(p)
        s.flush()
        a = _mk(s, p.id, "T-1", owned=["src/a/**"], pref="developer_2")
        b = _mk(s, p.id, "T-2", owned=["src/b/**"])
        c = _mk(s, p.id, "T-3", deps=["T-1"], owned=["src/c/**"])
        d = _mk(s, p.id, "T-4", owned=["src/a/x.py"])  # overlaps T-1
        assert set(promote_ready(s, p.id)) == {"T-1", "T-2", "T-4"}
        asg = plan_assignments(s, p.id, [Role.DEVELOPER_1, Role.DEVELOPER_2], 2)
        got = {x.task_key: x.role for x in asg}
        assert got == {"T-1": Role.DEVELOPER_2, "T-2": Role.DEVELOPER_1}
        # mark T-1 active: T-4 overlaps it and must wait even with a free slot
        transition(s, a, TaskState.ASSIGNED)
        a.assigned_role = "developer_2"
        asg = plan_assignments(s, p.id, [Role.DEVELOPER_1, Role.DEVELOPER_2], 2)
        assert [x.task_key for x in asg] == ["T-2"]
        # manager suggestion with an invalid role/key is sanitised
        asg = plan_assignments(s, p.id, [Role.DEVELOPER_1, Role.DEVELOPER_2], 2,
                               suggestion=[{"task_key": "NOPE", "role": "developer_1"},
                                           {"task_key": "T-2", "role": "architect"}])
        assert [(x.task_key, x.role) for x in asg] == [("T-2", Role.DEVELOPER_1)]
        assert c.state == "BACKLOG" and d.state == "READY"


def test_git_worktree_commit_merge_conflict(repo, cfg, tmp_path):
    g = GitRepo(repo, cfg.policy)
    g.ensure_branch("wf/integration", "main")
    wt1 = g.add_worktree(tmp_path / "wt1", "wf/task/T-1", "wf/integration")
    wt2 = g.add_worktree(tmp_path / "wt2", "wf/task/T-2", "wf/integration")
    (wt1 / "README.md").write_text("one\n")
    (wt2 / "README.md").write_text("two\n")
    sha1 = g.commit_all(wt1, "feat: one", "developer_1", "m1", {"Workforce-Task": "T-1"})
    g.commit_all(wt2, "feat: two", "developer_2", "m2", {"Workforce-Task": "T-2"})
    assert "workforce/developer_1" in git(repo, "log", "-1", "--format=%an", sha1)
    integ = g.add_worktree(tmp_path / "integ", "wf/integration")
    assert g.merge(integ, "wf/task/T-1", "merge T-1").ok
    r = g.merge(integ, "wf/task/T-2", "merge T-2")
    assert not r.ok and r.conflicts == ["README.md"]
    g.reset_worktree(integ, "HEAD")
    assert g.is_clean(integ)
    # rollback via revert is non-destructive
    head = g.rev("HEAD", cwd=integ)
    g.revert(integ, head)
    assert (integ / "README.md").read_text() == "# sample\n"


def test_git_policy_guards(repo, cfg, tmp_path):
    g = GitRepo(repo, cfg.policy)
    with pytest.raises(PolicyViolation):
        g.git("push", "--force", "origin", "main")
    with pytest.raises(PolicyViolation):
        g.reset_worktree(repo, "HEAD")  # main is not an engine branch
    (repo / "x.txt").write_text("x")
    with pytest.raises(PolicyViolation):
        g.commit_all(repo, "x", "developer_1", "m", {})


def test_recovery_does_not_duplicate_integration(wf, repo, tmp_path):
    g = GitRepo(repo, wf.cfg.policy)
    g.ensure_branch("wf/integration", "main")
    wt = g.add_worktree(tmp_path / "wt", "wf/task/T-1", "wf/integration")
    (wt / "a.py").write_text("x=1\n")
    head = g.commit_all(wt, "feat", "developer_1", "m", {})
    integ = g.add_worktree(tmp_path / "integ", "wf/integration")
    assert g.merge(integ, "wf/task/T-1", "merge").ok
    with wf.db.session() as s:
        p = Project(name="p", repo_path=str(repo))
        s.add(p)
        s.flush()
        t = _mk(s, p.id, "T-1")
        t.head_commit = head
        for st in ("READY", "ASSIGNED", "IMPLEMENTING", "TESTING", "REVIEWING", "INTEGRATING"):
            transition(s, t, st)
        t2 = _mk(s, p.id, "T-2")
        t2.worktree_path = str(wt)
        t2.head_commit = head
        for st in ("READY", "ASSIGNED", "IMPLEMENTING"):
            transition(s, t2, st)
        (wt / "partial.py").write_text("half-written")
        actions = recover_project(s, p, g, "wf/integration")
        assert t.state == "COMPLETED"
        assert not (wt / "partial.py").exists()
        assert {a["task"] for a in actions} == {"T-1", "T-2"}
