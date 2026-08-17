from typing import Any

import pytest

from scripts.staging_smoke import SmokeError, run_smoke, validate_target


class FakeSmokeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.run_reads = 0

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        self.calls.append((method, path, payload))
        if path == "/api/ready":
            return {"status": "ready", "database": "ok", "queue": "redis", "process": "web"}
        if path == "/api/session":
            return {"name": "staging-admin", "role": "admin"}
        if path == "/api/projects" and method == "GET":
            return []
        if path == "/api/projects" and method == "POST":
            return {"id": 7, "name": payload["name"]}
        if path == "/api/workflows?project_id=7":
            return []
        if path == "/api/workflows" and method == "POST":
            return {"id": 11, "name": payload["name"], "steps": payload["steps"]}
        if path == "/api/workflows/11/runs":
            return {"id": 19, "status": "queued"}
        if path == "/api/runs/19":
            self.run_reads += 1
            if self.run_reads == 1:
                return {"id": 19, "status": "running"}
            return {
                "id": 19,
                "status": "completed",
                "output": "STAGING SMOKE",
                "spans": [{"tool": "uppercase", "status": "completed"}],
            }
        raise AssertionError(f"unexpected request: {method} {path}")


def test_staging_smoke_creates_reusable_fixture_and_waits_for_worker():
    client = FakeSmokeClient()
    result = run_smoke(client, sleep=lambda _: None)

    assert result == {
        "status": "passed",
        "process": "web",
        "queue": "redis",
        "actor": "staging-admin",
        "project_id": 7,
        "project_created": True,
        "workflow_id": 11,
        "workflow_created": True,
        "run_id": 19,
        "run_status": "completed",
    }
    assert client.run_reads == 2


def test_staging_smoke_refuses_production_without_explicit_override():
    with pytest.raises(SmokeError, match="refusing a non-staging target"):
        validate_target("https://agentops.example.com")

    assert (
        validate_target("https://agentops.example.com", allow_non_staging=True)
        == "https://agentops.example.com"
    )


def test_staging_smoke_requires_https():
    with pytest.raises(SmokeError, match="absolute HTTPS URL"):
        validate_target("http://agentops-staging.example.com")
