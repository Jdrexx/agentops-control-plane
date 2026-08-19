from fastapi.testclient import TestClient


def _run_workflow(client: TestClient, workflow_id: int, input_value: str, key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        f"/api/workflows/{workflow_id}/runs", json={"input": input_value}, headers=headers
    )


def test_run_creation_is_idempotent_with_key(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Idempotent",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    first = _run_workflow(client, workflow["id"], "hello", "run-abc-123")
    second = _run_workflow(client, workflow["id"], "hello", "run-abc-123")
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    runs = client.get(f"/api/runs?workflow_id={workflow['id']}").json()
    assert len(runs) == 1


def test_idempotency_key_is_validated(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Bad key",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    response = _run_workflow(client, workflow["id"], "x", "not valid!?")
    assert response.status_code == 422


def test_idempotency_key_is_scoped_per_workflow(client: TestClient, project: dict):
    first = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Scoped A",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    second = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Scoped B",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    run_a = _run_workflow(client, first["id"], "x", "shared-key")
    run_b = _run_workflow(client, second["id"], "x", "shared-key")
    assert run_a.status_code == 201
    assert run_b.status_code == 201
    assert run_a.json()["id"] != run_b.json()["id"]
    # Replay within the same workflow still deduplicates.
    replay = _run_workflow(client, first["id"], "x", "shared-key")
    assert replay.status_code == 200
    assert replay.json()["id"] == run_a.json()["id"]


def test_run_filters_by_status_project_and_parent(client: TestClient, project: dict):
    good = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Good",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    bad = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Bad",
            "steps": [{"name": "Fail", "tool": "fail", "config": {"message": "boom"}}],
        },
    ).json()
    client.post(f"/api/workflows/{good['id']}/runs", json={"input": 1})
    client.post(f"/api/workflows/{bad['id']}/runs", json={"input": 2})
    other = client.post("/api/projects", json={"name": "Other project"}).json()
    other_workflow = client.post(
        "/api/workflows",
        json={
            "project_id": other["id"],
            "name": "Other",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    other_run = client.post(f"/api/workflows/{other_workflow['id']}/runs", json={"input": 3}).json()

    assert {item["id"] for item in client.get("/api/runs?status=completed").json()} == {
        other_run["id"],
        1,
    }
    assert {item["id"] for item in client.get("/api/runs?status=failed").json()} == {2}
    assert len(client.get(f"/api/runs?project_id={other['id']}").json()) == 1
    replay = client.post(f"/api/runs/{other_run['id']}/replay").json()
    children = client.get("/api/runs?has_parent=true").json()
    assert children and replay["id"] in {item["id"] for item in children}
    roots = client.get("/api/runs?has_parent=false").json()
    assert all(item["parent_run_id"] is None for item in roots)


def test_audit_cursor_pagination(client: TestClient, project: dict):
    for index in range(12):
        client.post("/api/projects", json={"name": f"Audit page {index}"})
    page_one = client.get("/api/audit?limit=5").json()
    assert len(page_one) == 5
    cursor = page_one[-1]["id"]
    page_two = client.get(f"/api/audit?limit=5&cursor={cursor}").json()
    assert len(page_two) == 5
    assert all(item["id"] < cursor for item in page_two)
    assert page_two[0]["id"] < page_one[-1]["id"]
    # Pages do not overlap.
    ids_one = {item["id"] for item in page_one}
    ids_two = {item["id"] for item in page_two}
    assert ids_one.isdisjoint(ids_two)


def test_agent_events_endpoint_supports_cursor(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Events",
            "steps": [
                {
                    "name": "Draft",
                    "tool": "llm",
                    "config": {
                        "provider": "mock",
                        "model": "mock-small",
                        "prompt": "Draft: {value}",
                    },
                }
            ],
        },
    ).json()
    client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
    events = client.get("/api/events?limit=10").json()
    assert events
    assert any(item["event_type"] == "llm.completed" for item in events)
    cursor = events[-1]["id"]
    older = client.get(f"/api/events?limit=10&cursor={cursor}").json()
    assert all(item["id"] < cursor for item in older)
    assert {item["id"] for item in events}.isdisjoint({item["id"] for item in older})
