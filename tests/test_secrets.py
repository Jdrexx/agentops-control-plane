from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agentops.main import create_app
from src.agentops.providers import ProviderResult


@pytest.fixture
def vaulted_client(tmp_path: Path, monkeypatch):
    """Client with the encryption key configured so the vault actually encrypts."""
    monkeypatch.setenv("AGENTOPS_ENCRYPTION_KEY", "test encryption phrase with sufficient entropy")
    with TestClient(
        create_app(str(tmp_path / "vault.db")), backend_options={"use_uvloop": True}
    ) as test_client:
        yield test_client


def test_llm_step_uses_project_secret_as_api_key(vaulted_client: TestClient):
    client = vaulted_client
    project = client.post("/api/projects", json={"name": "Secret user"}).json()
    client.put(
        "/api/secrets",
        json={"project_id": project["id"], "name": "openai-key", "value": "sk-secret-value"},
    )
    captured = {}

    def fake_generate_detailed(provider, model, prompt, system="", on_chunk=None, api_key=None):
        captured["api_key"] = api_key
        captured["provider"] = provider
        return ProviderResult(
            "secret-powered reply", provider, model, input_tokens=3, output_tokens=2
        )

    client.app.state.service.providers.generate_detailed = fake_generate_detailed
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Secret call",
            "steps": [
                {
                    "name": "Generate",
                    "tool": "llm",
                    "config": {
                        "provider": "openai",
                        "credential_ref": "openai-key",
                        "prompt": "Write a reply to: {value}",
                    },
                }
            ],
        },
    ).json()
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "hello"}).json()
    assert run["status"] == "completed"
    assert run["output"] == "secret-powered reply"
    assert captured["api_key"] == "sk-secret-value"
    assert captured["provider"] == "openai"
    # The secret value must never leak into the run or its spans.
    assert "sk-secret-value" not in str(run)
    assert "sk-secret-value" not in client.get(f"/api/runs/{run['id']}").text


def test_credential_ref_missing_fails_clearly(vaulted_client: TestClient):
    client = vaulted_client
    project = client.post("/api/projects", json={"name": "No secret"}).json()
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Missing ref",
            "steps": [
                {
                    "name": "Generate",
                    "tool": "llm",
                    "config": {"provider": "openai", "credential_ref": "does-not-exist"},
                }
            ],
        },
    ).json()
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"}).json()
    assert run["status"] == "failed"
    assert "does-not-exist" in run["error"]
    assert "not found" in run["error"]


def test_secret_reveal_is_posted_and_audited(vaulted_client: TestClient, monkeypatch):
    monkeypatch.setenv("AGENTOPS_API_KEY", "admin-token-that-is-at-least-24-characters")
    client = vaulted_client
    headers = {"Authorization": "Bearer admin-token-that-is-at-least-24-characters"}
    project = client.post("/api/projects", json={"name": "Audited"}, headers=headers).json()
    secret = client.put(
        "/api/secrets",
        json={"project_id": project["id"], "name": "audited-key", "value": "v"},
        headers=headers,
    ).json()
    before = len(client.get("/api/audit", headers=headers).json())
    client.post(f"/api/secrets/{secret['id']}/reveal", headers=headers)
    audit = client.get("/api/audit", headers=headers).json()
    assert len(audit) == before + 1
    assert audit[0]["resource"] == f"/api/secrets/{secret['id']}/reveal"
    assert audit[0]["action"] == "POST"


def test_credential_ref_rejects_invalid_values(vaulted_client: TestClient):
    client = vaulted_client
    project = client.post("/api/projects", json={"name": "Bad ref"}).json()
    response = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Bad ref workflow",
            "steps": [
                {
                    "name": "Generate",
                    "tool": "llm",
                    "config": {"provider": "openai", "credential_ref": "x" * 300},
                }
            ],
        },
    )
    assert response.status_code == 422
