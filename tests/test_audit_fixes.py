"""Audit-fix regression tests: secrets-unconfigured 503s and auth-required startup guard."""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.agentops.main import create_app


def test_put_secret_without_encryption_key_returns_503(tmp_path):
    """AGENTOPS_ENCRYPTION_KEY unset → 503 'secrets unavailable', not a 500 crash."""
    db_path = str(tmp_path / "agentops.db")
    with patch.dict(
        os.environ,
        {"AGENTOPS_DATABASE": db_path, "AGENTOPS_ENCRYPTION_KEY": "", "AGENTOPS_DEMO_ENABLED": "0"},
        clear=False,
    ):
        app = create_app(db_path)
        client = TestClient(app)
        with client:
            project = client.post("/api/projects", json={"name": "P", "description": ""})
            assert project.status_code == 201
            project_id = project.json()["id"]
            response = client.put(
                "/api/secrets",
                json={"project_id": project_id, "name": "k", "value": "v"},
            )
            assert response.status_code == 503
            assert "secrets unavailable" in response.json()["detail"]


def test_require_auth_startup_guard_raises_when_auth_unconfigured(tmp_path):
    """AGENTOPS_REQUIRE_AUTH=1 with no API key and no users → startup RuntimeError."""
    db_path = str(tmp_path / "empty.db")
    with patch.dict(
        os.environ,
        {
            "AGENTOPS_DATABASE": db_path,
            "AGENTOPS_API_KEY": "",
            "AGENTOPS_REQUIRE_AUTH": "1",
            "AGENTOPS_ENCRYPTION_KEY": "",
            "AGENTOPS_DEMO_ENABLED": "0",
        },
        clear=False,
    ):
        app = create_app(db_path)
        # the guard lives in lifespan (after DB init), so entering the
        # TestClient context must raise
        with (
            pytest.raises(RuntimeError, match="AGENTOPS_REQUIRE_AUTH=1"),
            TestClient(app),
        ):
            pass


def test_require_auth_startup_guard_allows_when_api_key_set(tmp_path):
    """AGENTOPS_REQUIRE_AUTH=1 with AGENTOPS_API_KEY set → app starts fine."""
    db_path = str(tmp_path / "keyed.db")
    with patch.dict(
        os.environ,
        {
            "AGENTOPS_DATABASE": db_path,
            "AGENTOPS_API_KEY": "test-api-key",
            "AGENTOPS_REQUIRE_AUTH": "1",
            "AGENTOPS_DEMO_ENABLED": "0",
        },
        clear=False,
    ):
        app = create_app(db_path)
        client = TestClient(app)
        with client:
            # auth is enabled: unauthenticated request is rejected
            response = client.get("/api/projects")
            assert response.status_code == 401