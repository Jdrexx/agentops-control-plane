import time

from fastapi.testclient import TestClient


def _dataset(client: TestClient, project_id: int, name: str, cases: list[dict]) -> dict:
    response = client.post(
        "/api/datasets", json={"project_id": project_id, "name": name, "cases": cases}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wait_evaluation(client: TestClient, evaluation_id: int, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        evaluation = client.get(f"/api/evaluations/{evaluation_id}").json()
        if evaluation["status"] in {"completed", "cancelled"}:
            return evaluation
        time.sleep(0.05)
    raise AssertionError(f"evaluation {evaluation_id} did not finish in time")


def test_dataset_cases_get_stable_ids(client: TestClient, project: dict):
    dataset = _dataset(
        client,
        project["id"],
        "Stable ids",
        [
            {"id": "billing-check", "input": "x", "expected": "x"},
            {"input": "y", "expected": "y"},
            {"input": "z", "expected": "z"},
        ],
    )
    assert [case["id"] for case in dataset["cases"]] == ["billing-check", "case-2", "case-3"]


def test_duplicate_case_ids_are_rejected(client: TestClient, project: dict):
    response = client.post(
        "/api/datasets",
        json={
            "project_id": project["id"],
            "name": "Duplicates",
            "cases": [
                {"id": "same", "input": "x", "expected": "x"},
                {"id": "same", "input": "y", "expected": "y"},
            ],
        },
    )
    assert response.status_code == 409


def test_queued_evaluation_reports_progress_and_completes(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Upper",
            "steps": [{"name": "Upper", "tool": "uppercase"}],
        },
    ).json()
    dataset = _dataset(
        client,
        project["id"],
        "Queued cases",
        [{"input": "a", "expected": "A"}, {"input": "b", "expected": "B"}],
    )
    started = client.post(
        "/api/evaluations",
        json={
            "workflow_id": workflow["id"],
            "dataset_id": dataset["id"],
            "execution": "queued",
            "pass_rate_min": 1.0,
        },
    ).json()
    assert started["status"] in {"queued", "running"}
    assert started["completed_cases"] == 0
    finished = _wait_evaluation(client, started["id"])
    assert finished["status"] == "completed"
    assert finished["passed"] == 2
    assert finished["completed_cases"] == 2
    assert finished["gate"] == "passed"
    assert all("case_id" in case for case in finished["results"])
    assert all(case["duration_ms"] >= 0 for case in finished["results"])


def test_queued_evaluation_can_be_cancelled(client: TestClient, project: dict, monkeypatch):
    monkeypatch.setenv("AGENTOPS_MOCK_LATENCY_MS", "100")
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Slow classify",
            "steps": [
                {
                    "name": "Classify",
                    "tool": "llm",
                    "config": {
                        "provider": "mock",
                        "model": "mock-small",
                        "prompt": "Classify: {value}",
                    },
                }
            ],
        },
    ).json()
    dataset = _dataset(
        client,
        project["id"],
        "Slow cases",
        [{"input": f"ticket {index}", "expected": "x"} for index in range(10)],
    )
    started = client.post(
        "/api/evaluations",
        json={"workflow_id": workflow["id"], "dataset_id": dataset["id"], "execution": "queued"},
    ).json()
    cancelled = client.post(f"/api/evaluations/{started['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    finished = _wait_evaluation(client, started["id"])
    assert finished["status"] == "cancelled"
    assert finished["completed_cases"] < 10


def test_release_gate_fails_on_bad_pass_rate(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Identity",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    dataset = _dataset(
        client,
        project["id"],
        "Gate cases",
        [
            {"input": "ok", "expected": "ok"},
            {"input": "wrong", "expected": "RIGHT"},
        ],
    )
    evaluation = client.post(
        "/api/evaluations",
        json={
            "workflow_id": workflow["id"],
            "dataset_id": dataset["id"],
            "pass_rate_min": 1.0,
            "max_cost_usd": 0.05,
        },
    ).json()
    assert evaluation["gate"] == "failed"
    assert any("pass rate" in reason for reason in evaluation["gate_reasons"])
    assert evaluation["pass_rate"] == 0.5


def test_evaluation_compare_rejects_cross_dataset(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Identity 2",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    first = _dataset(client, project["id"], "One", [{"input": "a", "expected": "a"}])
    second = _dataset(client, project["id"], "Two", [{"input": "b", "expected": "b"}])
    left = client.post(
        "/api/evaluations", json={"workflow_id": workflow["id"], "dataset_id": first["id"]}
    ).json()
    right = client.post(
        "/api/evaluations", json={"workflow_id": workflow["id"], "dataset_id": second["id"]}
    ).json()
    response = client.get(f"/api/evaluations/{left['id']}/compare/{right['id']}")
    assert response.status_code == 409


def test_llm_judge_uses_case_rubric_and_provider(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Draft",
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
    dataset = _dataset(
        client,
        project["id"],
        "Judged",
        [
            {
                "input": "ticket",
                "expected": "no unsupported claims",
                "matcher": "llm_judge",
                "judge_provider": "mock",
                "judge_model": "mock-small",
                "rubric": "The reply is polite and accurate.",
            }
        ],
    )
    seen = {}

    def fake_generate(provider, model, prompt, system="", on_chunk=None):
        seen["prompt"] = prompt
        return "PASS"

    client.app.state.service.providers.generate = fake_generate
    evaluation = client.post(
        "/api/evaluations", json={"workflow_id": workflow["id"], "dataset_id": dataset["id"]}
    ).json()
    assert evaluation["results"][0]["passed"] is True
    assert "The reply is polite and accurate." in seen["prompt"]
    assert "no unsupported claims" not in seen["prompt"]  # rubric wins over expected


def test_regex_pattern_length_is_capped(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Identity 3",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    dataset = _dataset(
        client,
        project["id"],
        "ReDoS guard",
        [{"input": "abc", "expected": "a" * 3000, "matcher": "regex"}],
    )
    evaluation = client.post(
        "/api/evaluations", json={"workflow_id": workflow["id"], "dataset_id": dataset["id"]}
    ).json()
    assert evaluation["results"][0]["passed"] is False


def test_json_schema_matcher_enforces_full_spec(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Identity 4",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    schema = {"type": "string", "pattern": "^AB"}
    dataset = _dataset(
        client,
        project["id"],
        "Schema spec",
        [
            {"input": "ABC", "expected": schema, "matcher": "json_schema"},
            {"input": "ZZZ", "expected": schema, "matcher": "json_schema"},
        ],
    )
    evaluation = client.post(
        "/api/evaluations", json={"workflow_id": workflow["id"], "dataset_id": dataset["id"]}
    ).json()
    assert [case["passed"] for case in evaluation["results"]] == [True, False]


def test_delete_dataset_cascades_evaluations(client: TestClient, project: dict):
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Identity 5",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    dataset = _dataset(client, project["id"], "To delete", [{"input": "x", "expected": "x"}])
    evaluation = client.post(
        "/api/evaluations", json={"workflow_id": workflow["id"], "dataset_id": dataset["id"]}
    ).json()
    response = client.delete(f"/api/datasets/{dataset['id']}")
    assert response.status_code == 200
    assert client.get(f"/api/datasets/{dataset['id']}").status_code == 404
    assert client.get(f"/api/evaluations/{evaluation['id']}").status_code == 404
