from fastapi.testclient import TestClient

from .conftest import create_workflow


def test_health_and_dashboard(client: TestClient):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "version": "0.1.0"}
    assert client.get("/api/ready").json() == {
        "status": "ready",
        "database": "ok",
        "queue": "local",
        "process": "all",
    }
    assert client.get("/api/auth/status").json() == {"enabled": False}
    assert client.get("/api/session").json() == {"name": "local-user", "role": "admin"}
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "AgentOps Control Plane" in dashboard.text
    assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"


def test_project_crud_and_duplicate_conflict(client: TestClient, project: dict):
    assert client.get(f"/api/projects/{project['id']}").json()["name"] == "Operations"
    assert len(client.get("/api/projects").json()) == 1
    duplicate = client.post("/api/projects", json={"name": "Operations"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "project name already exists"


def test_project_validation_and_not_found(client: TestClient):
    assert client.post("/api/projects", json={"name": "   "}).status_code == 422
    assert client.post("/api/projects", json={"name": "x" * 101}).status_code == 422
    assert client.get("/api/projects/9999").status_code == 404


def test_workflow_templates_create_versioned_workflow(client: TestClient, project: dict):
    templates = client.get("/api/templates").json()
    assert {item["id"] for item in templates} >= {"research-brief", "code-review"}
    created = client.post(f"/api/templates/research-brief?project_id={project['id']}")
    assert created.status_code == 201
    assert created.json()["name"] == "Research brief"
    assert created.json()["steps"][-1]["tool"] == "approval"
    assert client.post(f"/api/templates/missing?project_id={project['id']}").status_code == 404


def test_workflow_versions_are_immutable(client: TestClient, project: dict):
    steps = [{"name": "Case", "tool": "uppercase", "config": {}}]
    first = create_workflow(client, project["id"], steps, "Normalize")
    second = create_workflow(client, project["id"], steps, "Normalize")
    assert first["version"] == 1
    assert second["version"] == 2
    assert first["id"] != second["id"]
    assert len(client.get(f"/api/workflows?project_id={project['id']}").json()) == 2


def test_workflow_rejects_invalid_tool_empty_steps_and_missing_project(
    client: TestClient, project: dict
):
    invalid_tool = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Bad",
            "steps": [{"name": "x", "tool": "shell"}],
        },
    )
    assert invalid_tool.status_code == 422
    assert (
        client.post(
            "/api/workflows", json={"project_id": project["id"], "name": "Empty", "steps": []}
        ).status_code
        == 422
    )
    missing = client.post(
        "/api/workflows",
        json={"project_id": 999, "name": "Missing", "steps": [{"name": "x", "tool": "input"}]},
    )
    assert missing.status_code == 404

    blank_workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "   ",
            "steps": [{"name": "x", "tool": "input"}],
        },
    )
    assert blank_workflow.status_code == 422
    blank_step = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Blank step",
            "steps": [{"name": "   ", "tool": "input"}],
        },
    )
    assert blank_step.status_code == 422
    invalid_runtime_config = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Bad retries",
            "steps": [{"name": "Run", "tool": "input", "config": {"retries": None}}],
        },
    )
    assert invalid_runtime_config.status_code == 422


def test_successful_run_records_ordered_trace(client: TestClient, project: dict):
    workflow = create_workflow(
        client,
        project["id"],
        [
            {"name": "Extract", "tool": "json_extract", "config": {"key": "message"}},
            {"name": "Normalize", "tool": "uppercase", "config": {}},
            {"name": "Wrap", "tool": "template", "config": {"template": "RESULT={value}"}},
        ],
    )
    response = client.post(
        f"/api/workflows/{workflow['id']}/runs", json={"input": {"message": "hello"}}
    )
    assert response.status_code == 201
    run = response.json()
    assert run["status"] == "completed"
    assert run["output"] == "RESULT=HELLO"
    assert run["current_step"] == 3
    assert [span["tool"] for span in run["spans"]] == ["json_extract", "uppercase", "template"]
    assert all(span["status"] == "completed" for span in run["spans"])
    assert all(span["duration_ms"] >= 0 for span in run["spans"])


def test_llm_step_runs_offline_with_mock_provider(client: TestClient, project: dict):
    workflow = create_workflow(
        client,
        project["id"],
        [
            {
                "name": "Draft",
                "tool": "llm",
                "config": {
                    "provider": "mock",
                    "model": "mock-small",
                    "prompt": "Draft a reply to: {value}",
                },
            },
        ],
    )
    first = client.post(
        f"/api/workflows/{workflow['id']}/runs", json={"input": "customer"}
    ).json()
    second = client.post(
        f"/api/workflows/{workflow['id']}/runs", json={"input": "customer"}
    ).json()
    assert first["status"] == "completed"
    assert first["output"] == second["output"]
    assert "[mock:" in first["output"]
    assert first["spans"][0]["tool"] == "llm"
    assert first["spans"][0]["input_tokens"] >= 1
    providers = {item["name"]: item for item in client.get("/api/providers").json()}
    assert providers["mock"]["configured"] is True


def test_failure_is_terminal_and_trace_contains_error(client: TestClient, project: dict):
    workflow = create_workflow(
        client,
        project["id"],
        [
            {"name": "First", "tool": "lowercase", "config": {}},
            {"name": "Break", "tool": "fail", "config": {"message": "provider unavailable"}},
            {"name": "Never", "tool": "uppercase", "config": {}},
        ],
    )
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "HELLO"}).json()
    assert run["status"] == "failed"
    assert run["error"] == "provider unavailable"
    assert run["output"] == "hello"
    assert len(run["spans"]) == 2
    assert run["spans"][-1]["status"] == "failed"
    assert run["spans"][-1]["error"] == "provider unavailable"


def test_json_extract_failure_is_safe(client: TestClient, project: dict):
    workflow = create_workflow(
        client,
        project["id"],
        [{"name": "Extract", "tool": "json_extract", "config": {"key": "missing"}}],
    )
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": {"other": 1}}).json()
    assert run["status"] == "failed"
    assert run["error"] == "key not found: missing"


def test_approval_resumes_exactly_once(client: TestClient, project: dict):
    workflow = create_workflow(
        client,
        project["id"],
        [
            {"name": "Before", "tool": "uppercase", "config": {}},
            {"name": "Review", "tool": "approval", "config": {"prompt": "Release result?"}},
            {"name": "After", "tool": "template", "config": {"template": "OK:{value}"}},
        ],
    )
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "hello"}).json()
    assert run["status"] == "waiting_approval"
    assert run["output"] == "HELLO"
    assert [span["status"] for span in run["spans"]] == ["completed", "waiting"]
    pending = client.get("/api/approvals?status=pending").json()
    assert len(pending) == 1
    assert pending[0]["prompt"] == "Release result?"
    decision = client.post(
        f"/api/approvals/{pending[0]['id']}/decision",
        json={"decision": "approved", "note": "Reviewed"},
    )
    assert decision.status_code == 200
    completed = decision.json()
    assert completed["status"] == "completed"
    assert completed["output"] == "OK:HELLO"
    assert [span["status"] for span in completed["spans"]] == ["completed"] * 3
    repeated = client.post(
        f"/api/approvals/{pending[0]['id']}/decision", json={"decision": "approved"}
    )
    assert repeated.status_code == 409
    assert len(client.get(f"/api/runs/{run['id']}").json()["spans"]) == 3


def test_rejected_approval_terminates_run(client: TestClient, project: dict):
    workflow = create_workflow(
        client, project["id"], [{"name": "Review", "tool": "approval", "config": {}}]
    )
    client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "sensitive"})
    approval = client.get("/api/approvals?status=pending").json()[0]
    response = client.post(
        f"/api/approvals/{approval['id']}/decision",
        json={"decision": "rejected", "note": "Unsafe output"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["error"] == "Unsafe output"
    assert client.get("/api/approvals?status=rejected").json()[0]["note"] == "Unsafe output"


def test_approval_can_edit_escalate_and_expire(client: TestClient, project: dict):
    workflow = create_workflow(
        client,
        project["id"],
        [
            {
                "name": "Review",
                "tool": "approval",
                "config": {"prompt": "Edit this?", "expires_in_seconds": 60},
            },
            {"name": "Finish", "tool": "template", "config": {"template": "FINAL:{value}"}},
        ],
        "Editable",
    )
    client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "draft"})
    approval = client.get("/api/approvals?status=pending").json()[0]
    escalated = client.post(f"/api/approvals/{approval['id']}/escalate?note=Needs+owner").json()
    assert escalated["escalation_level"] == 1
    approved = client.post(
        f"/api/approvals/{approval['id']}/decision",
        json={"decision": "approved", "output": "edited"},
    ).json()
    assert approved["output"] == "FINAL:edited"

    expiring = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "stale"}).json()
    assert expiring["status"] == "waiting_approval"
    assert client.app.state.service.expire_approvals("2999-01-01T00:00:00+00:00") == 1
    expired = client.get(f"/api/runs/{expiring['id']}").json()
    assert expired["status"] == "rejected"
    assert client.get("/api/approvals?status=expired").json()[0]["status"] == "expired"


def test_replay_preserves_original_and_links_parent(client: TestClient, project: dict):
    workflow = create_workflow(
        client, project["id"], [{"name": "Case", "tool": "uppercase", "config": {}}]
    )
    original = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "again"}).json()
    replay = client.post(f"/api/runs/{original['id']}/replay")
    assert replay.status_code == 201
    assert replay.json()["id"] != original["id"]
    assert replay.json()["parent_run_id"] == original["id"]
    assert replay.json()["input"] == original["input"]
    assert replay.json()["output"] == "AGAIN"
    assert client.get(f"/api/runs/{original['id']}").json()["parent_run_id"] is None


def test_dataset_evaluation_exact_and_contains(client: TestClient, project: dict):
    workflow = create_workflow(
        client, project["id"], [{"name": "Upper", "tool": "uppercase", "config": {}}]
    )
    dataset = client.post(
        "/api/datasets",
        json={
            "project_id": project["id"],
            "name": "Regression",
            "cases": [
                {"input": "hello", "expected": "HELLO", "matcher": "exact"},
                {"input": "agent ops", "expected": "AGENT", "matcher": "contains"},
                {"input": "wrong", "expected": "RIGHT", "matcher": "exact"},
            ],
        },
    )
    assert dataset.status_code == 201
    evaluation = client.post(
        "/api/evaluations", json={"workflow_id": workflow["id"], "dataset_id": dataset.json()["id"]}
    )
    assert evaluation.status_code == 201
    result = evaluation.json()
    assert result["passed"] == 2
    assert result["total"] == 3
    assert result["pass_rate"] == 2 / 3
    assert [item["passed"] for item in result["results"]] == [True, True, False]


def test_evaluation_rejects_cross_project_resources(client: TestClient, project: dict):
    workflow = create_workflow(client, project["id"], [{"name": "Input", "tool": "input"}])
    other = client.post("/api/projects", json={"name": "Other"}).json()
    dataset = client.post(
        "/api/datasets",
        json={
            "project_id": other["id"],
            "name": "Cases",
            "cases": [{"input": "x", "expected": "x"}],
        },
    ).json()
    response = client.post(
        "/api/evaluations", json={"workflow_id": workflow["id"], "dataset_id": dataset["id"]}
    )
    assert response.status_code == 409


def test_stats_and_list_filters(client: TestClient, project: dict):
    good = create_workflow(client, project["id"], [{"name": "Input", "tool": "input"}], "Good")
    bad = create_workflow(client, project["id"], [{"name": "Fail", "tool": "fail"}], "Bad")
    client.post(f"/api/workflows/{good['id']}/runs", json={"input": 1})
    client.post(f"/api/workflows/{bad['id']}/runs", json={"input": 2})
    stats = client.get("/api/stats").json()
    assert stats["total_runs"] == 2
    assert stats["completed_runs"] == 1
    assert stats["failed_runs"] == 1
    assert stats["success_rate"] == 0.5
    assert len(client.get(f"/api/runs?workflow_id={good['id']}").json()) == 1
    assert client.get("/api/runs?limit=0").status_code == 422
    assert client.get("/api/runs?limit=501").status_code == 422


def test_missing_resources_and_invalid_approval_filter(client: TestClient):
    assert client.get("/api/workflows/999").status_code == 404
    assert client.get("/api/runs/999").status_code == 404
    assert client.post("/api/runs/999/replay").status_code == 404
    assert client.get("/api/datasets/999").status_code == 404
    assert (
        client.post("/api/approvals/999/decision", json={"decision": "approved"}).status_code == 404
    )
    assert client.get("/api/approvals?status=nonsense").status_code == 422


def test_arbitrary_json_values_round_trip(client: TestClient, project: dict):
    workflow = create_workflow(client, project["id"], [{"name": "Identity", "tool": "input"}])
    for value in [None, True, 42, 3.14, [1, "two"], {"nested": {"value": "✓"}}]:
        run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": value}).json()
        assert run["status"] == "completed"
        assert run["output"] == value


def test_tool_catalog_history_and_run_comparison(client: TestClient, project: dict):
    tools = client.get("/api/tools").json()
    assert {tool["name"] for tool in tools} >= {"input", "approval", "template"}
    workflow = create_workflow(client, project["id"], [{"name": "Upper", "tool": "uppercase"}])
    left = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "first"}).json()
    right = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "second"}).json()
    comparison = client.get(f"/api/runs/{left['id']}/compare/{right['id']}").json()
    assert comparison["same_workflow"] is True
    assert comparison["output_changed"] is True
    assert comparison["left"]["output"] == "FIRST"

    dataset = client.post(
        "/api/datasets",
        json={
            "project_id": project["id"],
            "name": "History",
            "cases": [{"input": "ok", "expected": "OK"}],
        },
    ).json()
    client.post(
        "/api/evaluations", json={"workflow_id": workflow["id"], "dataset_id": dataset["id"]}
    )
    assert client.get(f"/api/datasets?project_id={project['id']}").json()[0]["cases"]
    history = client.get(f"/api/evaluations?workflow_id={workflow['id']}").json()
    assert history[0]["pass_rate"] == 1
    assert client.get("/api/evaluations?limit=0").status_code == 422


def test_llm_tool_uses_provider_boundary(client: TestClient, project: dict):
    calls = []

    def generate(provider: str, model: str, prompt: str, system: str = "", on_chunk=None) -> str:
        calls.append((provider, model, prompt, system))
        on_chunk("model response")
        return "model response"

    client.app.state.service.providers.generate = generate
    workflow = create_workflow(
        client,
        project["id"],
        [
            {
                "name": "Generate",
                "tool": "llm",
                "config": {
                    "provider": "openai",
                    "model": "test-model",
                    "system": "Be concise",
                    "prompt": "Question: {value}",
                },
            }
        ],
    )
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "hello"}).json()
    assert run["status"] == "completed"
    assert run["output"] == "model response"
    assert calls == [("openai", "test-model", "Question: hello", "Be concise")]
    assert {item["name"] for item in client.get("/api/providers").json()} == {
        "mock",
        "ollama",
        "openai",
        "anthropic",
    }


def test_advanced_matchers_and_evaluation_comparison(client: TestClient, project: dict):
    identity = create_workflow(client, project["id"], [{"name": "Pass", "tool": "input"}])
    dataset = client.post(
        "/api/datasets",
        json={
            "project_id": project["id"],
            "name": "Structured quality",
            "cases": [
                {"input": "ABC-42", "expected": "^ABC-[0-9]+$", "matcher": "regex"},
                {
                    "input": {"name": "agent", "scores": [1, 2]},
                    "expected": {
                        "type": "object",
                        "required": ["name", "scores"],
                        "properties": {
                            "name": {"type": "string"},
                            "scores": {"type": "array", "items": {"type": "integer"}},
                        },
                    },
                    "matcher": "json_schema",
                },
            ],
        },
    ).json()
    first = client.post(
        "/api/evaluations", json={"workflow_id": identity["id"], "dataset_id": dataset["id"]}
    ).json()
    assert first["pass_rate"] == 1

    failing = create_workflow(
        client, project["id"], [{"name": "Stringify", "tool": "uppercase"}], "Changed"
    )
    second = client.post(
        "/api/evaluations", json={"workflow_id": failing["id"], "dataset_id": dataset["id"]}
    ).json()
    comparison = client.get(f"/api/evaluations/{first['id']}/compare/{second['id']}").json()
    assert comparison["pass_rate_delta"] == -0.5
    assert comparison["regression_case_indexes"] == [1]
    assert client.get(f"/api/evaluations/{first['id']}").json()["passed"] == 2


def test_project_export_and_import(client: TestClient, project: dict):
    create_workflow(client, project["id"], [{"name": "Pass", "tool": "input"}], "Portable")
    client.post(
        "/api/datasets",
        json={
            "project_id": project["id"],
            "name": "Portable cases",
            "cases": [{"input": "x", "expected": "x"}],
        },
    )
    package = client.get(f"/api/projects/{project['id']}/export").json()
    assert package["format"] == "agentops-project"
    imported = client.post(
        "/api/projects/import", json={"package": package, "name": "Imported copy"}
    )
    assert imported.status_code == 201
    assert imported.json()["project"]["name"] == "Imported copy"
    assert len(imported.json()["workflow_ids"]) == 1
    assert len(imported.json()["dataset_ids"]) == 1


def test_project_import_remaps_handoff_workflow_ids(client: TestClient, project: dict):
    child = create_workflow(
        client,
        project["id"],
        [{"name": "Normalize", "tool": "uppercase"}],
        "Portable child",
    )
    create_workflow(
        client,
        project["id"],
        [
            {
                "name": "Delegate",
                "tool": "handoff",
                "config": {"workflow_id": child["id"]},
            }
        ],
        "Portable parent",
    )
    package = client.get(f"/api/projects/{project['id']}/export").json()
    assert all(workflow["source_id"] for workflow in package["workflows"])

    imported = client.post(
        "/api/projects/import", json={"package": package, "name": "Imported handoff"}
    )
    assert imported.status_code == 201
    imported_project_id = imported.json()["project"]["id"]
    workflows = client.get(f"/api/workflows?project_id={imported_project_id}").json()
    by_name = {workflow["name"]: workflow for workflow in workflows}
    imported_child = by_name["Portable child"]
    imported_parent = by_name["Portable parent"]
    assert imported_parent["steps"][0]["config"]["workflow_id"] == imported_child["id"]
    run = client.post(
        f"/api/workflows/{imported_parent['id']}/runs", json={"input": "portable"}
    ).json()
    assert run["status"] == "completed"
    assert run["output"] == "PORTABLE"
