import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def mock_script_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point pinned mock scripts at a temp dir so tests never write into the repo."""
    monkeypatch.setenv("AGENTOPS_MOCK_SCRIPT_DIR", str(tmp_path / "pins"))
    monkeypatch.setenv("AGENTOPS_MOCK_CHUNK_MS", "0")


def test_demo_tour_pauses_for_approval_and_resumes(client: TestClient):
    result = client.post("/api/demo/seed", json={"scenario": "tour"}).json()
    assert result["scenario"] == "tour"
    assert result["focus"] == "trace"
    assert result["next_action"] == "approve"
    assert result["approval_id"] is not None

    run = client.get(f"/api/runs/{result['focus_run_id']}").json()
    assert run["status"] == "waiting_approval"
    assert [span["tool"] for span in run["spans"]] == ["llm", "approval"]
    assert run["spans"][0]["status"] == "completed"
    assert "[mock:" in run["spans"][0]["output"]

    decided = client.post(
        f"/api/approvals/{result['approval_id']}/decision",
        json={"decision": "approved", "note": "looks good"},
    ).json()
    assert decided["status"] == "completed"
    assert decided["output"] is not None

    project_id = result["project_id"]
    memories = client.get(f"/api/memories?project_id={project_id}").json()
    assert any(item["key"] == "last_reply" for item in memories)


def test_demo_tour_handoff_creates_child_run(client: TestClient):
    seeded = client.post("/api/demo/seed", json={"scenario": "tour"}).json()
    approval_id = seeded["approval_id"]
    client.post(f"/api/approvals/{approval_id}/decision", json={"decision": "approved"})
    parent = client.get(f"/api/runs/{seeded['focus_run_id']}").json()
    assert parent["status"] == "completed"
    workflows = client.get(f"/api/workflows?project_id={seeded['project_id']}").json()
    child = next(item for item in workflows if item["name"] == "QA policy check")
    child_runs = client.get(f"/api/runs?workflow_id={child['id']}").json()
    assert len(child_runs) == 1
    assert child_runs[0]["parent_run_id"] == parent["id"]
    assert child_runs[0]["status"] == "completed"


def test_demo_quality_shows_regression_fixed(client: TestClient):
    result = client.post("/api/demo/seed", json={"scenario": "quality"}).json()
    assert result["focus"] == "quality"
    assert result["pass_rates"] == [pytest.approx(4 / 6), pytest.approx(1.0)]
    assert len(result["focus_eval_ids"]) == 2

    v1 = client.get(f"/api/evaluations/{result['focus_eval_ids'][0]}").json()
    v2 = client.get(f"/api/evaluations/{result['focus_eval_ids'][1]}").json()
    assert v1["passed"] == 4 and v1["total"] == 6
    assert v2["passed"] == 6 and v2["total"] == 6
    failed_cases = [case for case in v1["results"] if not case["passed"]]
    assert len(failed_cases) == 2
    assert all(case["expected"] == "BILLING" for case in failed_cases)
    assert all(
        case["passed"] for case in v2["results"] if case["expected"] == "BILLING"
    )


def test_demo_incident_fails_after_retries_and_triggers_alert(client: TestClient):
    result = client.post("/api/demo/seed", json={"scenario": "incident"}).json()
    assert result["focus"] == "trace"
    run = client.get(f"/api/runs/{result['focus_run_id']}").json()
    assert run["status"] == "failed"
    assert "connection refused" in run["error"]
    assert run["spans"][-1]["status"] == "failed"
    alerts = client.get("/api/alerts").json()
    billing_alerts = [alert for alert in alerts if alert["name"] == "Billing sync failing"]
    assert billing_alerts and billing_alerts[0]["triggered"] is True


def test_demo_seed_is_idempotent(client: TestClient):
    first = client.post("/api/demo/seed", json={"scenario": "quality"}).json()
    second = client.post("/api/demo/seed", json={"scenario": "quality"}).json()
    assert first["project_id"] == second["project_id"]
    assert first["focus_eval_ids"] == second["focus_eval_ids"]
    # No duplicate datasets or workflows from re-seeding.
    workflows = client.get(f"/api/workflows?project_id={first['project_id']}").json()
    classifiers = [item for item in workflows if item["name"] == "Ticket classifier"]
    assert len(classifiers) == 2


def test_demo_reset_recreates_the_project(client: TestClient):
    first = client.post("/api/demo/seed", json={"scenario": "tour"}).json()
    second = client.post(
        "/api/demo/seed", json={"scenario": "tour", "reset": True}
    ).json()
    assert second["project_id"] != first["project_id"]
    assert client.get(f"/api/projects/{first['project_id']}").status_code == 404
    run = client.get(f"/api/runs/{second['focus_run_id']}").json()
    assert run["status"] == "waiting_approval"


def test_demo_rejects_unknown_scenario(client: TestClient):
    response = client.post("/api/demo/seed", json={"scenario": "nope"})
    assert response.status_code == 422


def test_demo_can_be_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOPS_DEMO_ENABLED", "0")
    response = client.post("/api/demo/seed", json={"scenario": "tour"})
    assert response.status_code == 404


def test_delete_project_endpoint(client: TestClient, project: dict):
    response = client.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": project["id"]}
    assert client.get(f"/api/projects/{project['id']}").status_code == 404
    assert client.delete("/api/projects/9999").status_code == 404
