import threading
import time

from fastapi.testclient import TestClient

from .conftest import create_workflow


def wait_for_status(client: TestClient, run_id: int, statuses: set[str]) -> dict:
    for _ in range(100):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def test_step_retries_before_succeeding(client: TestClient, project: dict):
    attempts = 0
    original = client.app.state.service._apply_tool

    def flaky(tool: str, config: dict, value):
        nonlocal attempts
        if tool == "uppercase":
            attempts += 1
            if attempts < 3:
                raise RuntimeError("temporary failure")
        return original(tool, config, value)

    client.app.state.service._apply_tool = flaky
    workflow = create_workflow(
        client,
        project["id"],
        [{"name": "Retry", "tool": "uppercase", "config": {"retries": 2}}],
    )
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "ok"}).json()
    assert run["status"] == "completed"
    assert run["output"] == "OK"
    assert attempts == 3


def test_queued_run_can_be_cancelled(client: TestClient, project: dict):
    entered = threading.Event()
    release = threading.Event()
    original = client.app.state.service._apply_tool

    def slow(tool: str, config: dict, value):
        entered.set()
        release.wait(timeout=2)
        return original(tool, config, value)

    client.app.state.service._apply_tool = slow
    workflow = create_workflow(client, project["id"], [{"name": "Slow", "tool": "input"}])
    queued = client.post(
        f"/api/workflows/{workflow['id']}/runs",
        json={"input": "work", "execution": "queued"},
    ).json()
    assert entered.wait(timeout=1)
    cancelled = client.post(f"/api/runs/{queued['id']}/cancel").json()
    release.set()
    assert cancelled["status"] == "cancelled"
    assert wait_for_status(client, queued["id"], {"cancelled"})["status"] == "cancelled"


def test_completed_queue_job_cannot_be_claimed_twice(client: TestClient, project: dict):
    workflow = create_workflow(client, project["id"], [{"name": "Once", "tool": "input"}])
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "once"}).json()
    client.app.state.service._execute_queued(run["id"])
    assert len(client.get(f"/api/runs/{run['id']}").json()["spans"]) == 1


def test_schedule_dispatch_and_webhook_delivery(client: TestClient, project: dict):
    workflow = create_workflow(client, project["id"], [{"name": "Pass", "tool": "input"}])
    schedule = client.post(
        "/api/schedules",
        json={
            "workflow_id": workflow["id"],
            "name": "Frequent check",
            "input": {"scheduled": True},
            "interval_seconds": 10,
        },
    )
    assert schedule.status_code == 201
    assert client.get("/api/schedules").json()[0]["enabled"] is True
    due = client.post("/api/schedules/run-due").json()
    assert len(due) == 1
    completed = wait_for_status(client, due[0]["id"], {"completed", "failed"})
    assert completed["output"] == {"scheduled": True}
    disabled = client.post(f"/api/schedules/{schedule.json()['id']}/enabled?enabled=false").json()
    assert disabled["enabled"] is False

    delivered = []
    client.app.state.service._send_webhook = lambda url, payload: delivered.append((url, payload))
    webhook = client.post(
        "/api/webhooks",
        json={
            "project_id": project["id"],
            "url": "https://hooks.example.test/runs",
            "events": ["run.completed"],
        },
    )
    assert webhook.status_code == 201
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "notify"}).json()
    assert delivered[0][0] == "https://hooks.example.test/runs"
    assert delivered[0][1]["run"]["id"] == run["id"]
    assert client.get(f"/api/webhooks?project_id={project['id']}").json()[0]["events"] == [
        "run.completed"
    ]


def test_step_timeout_and_role_permission(client: TestClient, project: dict):
    original = client.app.state.service._apply_tool

    def slow(tool: str, config: dict, value):
        time.sleep(0.05)
        return original(tool, config, value)

    client.app.state.service._apply_tool = slow
    timed = create_workflow(
        client,
        project["id"],
        [{"name": "Timed", "tool": "input", "config": {"timeout_seconds": 0.01}}],
        "Timed",
    )
    run = client.post(f"/api/workflows/{timed['id']}/runs", json={"input": "slow"}).json()
    assert run["status"] == "failed"
    assert run["error"] == "step timed out after 0.01 seconds"

    restricted = create_workflow(
        client,
        project["id"],
        [{"name": "Restricted", "tool": "input", "config": {"allowed_roles": ["operator"]}}],
        "Restricted",
    )
    denied = client.post(
        f"/api/workflows/{restricted['id']}/runs", json={"input": "blocked"}
    ).json()
    assert denied["status"] == "failed"
    assert denied["error"] == "tool permission denied"
