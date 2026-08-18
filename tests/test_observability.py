from fastapi.testclient import TestClient

from .conftest import create_workflow


def test_usage_alerts_otel_and_live_stream(client: TestClient, project: dict):
    from src.agentops.providers import ProviderResult

    client.app.state.service.providers.generate_detailed = (
        lambda *_args, **_kwargs: ProviderResult(
            "generated response", "ollama", "test", input_tokens=5, output_tokens=7
        )
    )
    workflow = create_workflow(
        client,
        project["id"],
        [
            {
                "name": "Model",
                "tool": "llm",
                "config": {
                    "provider": "ollama",
                    "input_cost_per_1k": 1,
                    "output_cost_per_1k": 2,
                },
            }
        ],
    )
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "hello"}).json()
    stats = client.get("/api/stats").json()
    assert stats["input_tokens"] > 0
    assert stats["output_tokens"] > 0
    assert stats["total_cost_usd"] > 0
    assert run["spans"][0]["cost_usd"] > 0

    alert = client.post(
        "/api/alerts",
        json={"name": "Any model spend", "metric": "total_cost_usd", "threshold": 0.000001},
    )
    assert alert.status_code == 201
    assert client.get("/api/alerts").json()[0]["triggered"] is True
    trends = client.get("/api/stats/trends").json()
    assert trends[0]["id"] == run["id"]
    assert trends[0]["cost_usd"] > 0

    trace = client.get(f"/api/runs/{run['id']}/otel").json()
    exported = trace["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert exported[0]["name"] == "Model"
    assert exported[0]["traceId"] == f"{run['id']:032x}"

    with client.websocket_connect("/api/live") as socket:
        message = socket.receive_json()
        assert message["stats"]["total_runs"] == 1
        assert message["runs"][0]["id"] == run["id"]
