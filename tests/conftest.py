from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agentops.main import create_app


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(
        create_app(str(tmp_path / "test.db")), backend_options={"use_uvloop": True}
    ) as test_client:
        yield test_client


@pytest.fixture
def project(client: TestClient):
    response = client.post("/api/projects", json={"name": "Operations", "description": "Tests"})
    assert response.status_code == 201
    return response.json()


def create_workflow(client: TestClient, project_id: int, steps: list[dict], name: str = "Flow"):
    response = client.post(
        "/api/workflows", json={"project_id": project_id, "name": name, "steps": steps}
    )
    assert response.status_code == 201, response.text
    return response.json()
