from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_e2e_pipeline import setup, tasks
from workforce.api.server import create_api

H = {"X-Workforce-Client": "dashboard", "Content-Type": "application/json"}


async def test_api_reflects_real_state_and_controls(tmp_path):
    app, orch, repo, adapter, pid = await setup(tmp_path, {"always_buggy": True})
    await orch.run(until_idle=True, max_seconds=60)
    c = TestClient(create_api(app, static_dir=None))
    ov = c.get("/api/overview").json()
    assert ov["task_counts"] == {"BLOCKED": 1, "COMPLETED": 1} and ov["run_state"] == "stopped"
    agents = c.get("/api/agents").json()
    assert len(agents) == 8 and {a["role"] for a in agents} >= {"manager", "architect", "developer_1", "worker"}
    assert next(a for a in agents if a["role"] == "reviewer")["calls"] >= 1
    ts = c.get("/api/tasks", params={"project": pid}).json()
    blocked = next(t for t in ts if t["state"] == "BLOCKED")
    d = c.get(f"/api/tasks/{blocked['id']}").json()
    assert d["tests"] and not d["tests"][0]["passed"] and d["events"]
    tr = c.get("/api/traceability", params={"project": pid}).json()
    assert {r["requirement"]: r["final_status"] for r in tr}["FR-3"] == "verified"
    g = c.get("/api/git", params={"project": pid}).json()
    assert any(b["name"] == "wf/textstats/integration" for b in g["branches"]) and g["commits"]
    assert c.get("/api/executions").json()
    assert c.get("/api/logs").json()["audit"]
    assert c.get("/api/usage").json()["rows"]
    # controls
    assert c.post("/api/control/pause", json={}, headers=H).json()["run_state"] == "paused"
    r = c.post(f"/api/tasks/{blocked['id']}/retry", json={}, headers=H)
    assert r.status_code == 200 and tasks(app)["T-001"].state == "READY"
    appr = c.get("/api/approvals").json()
    promote = next(a for a in appr if a["kind"] == "promote") if any(a["kind"] == "promote" for a in appr) else None
    if promote:
        assert c.post(f"/api/approvals/{promote['id']}/decide", json={"approve": False}, headers=H).status_code == 200


async def test_api_csrf_and_host_guards(tmp_path, wf):
    c = TestClient(create_api(wf, static_dir=None))
    assert c.post("/api/control/pause", json={}).status_code == 403  # missing custom header
    assert c.post("/api/control/pause", json={}, headers={**H, "Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/control/pause", content="x", headers={"X-Workforce-Client": "dashboard",
                                                              "Content-Type": "text/plain"}).status_code == 415
    assert c.get("/api/overview", headers={"Host": "attacker.example"}).status_code == 403
    assert c.post("/api/control/explode", json={}, headers=H).status_code == 400
    assert c.post("/api/control/pause", json={}, headers={**H, "Origin": "http://127.0.0.1:8765"}).status_code == 200
