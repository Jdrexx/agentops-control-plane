from pathlib import Path

from fastapi.testclient import TestClient

from src.agentops.main import create_app


def authorized(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_optional_auth_roles_and_audit(tmp_path: Path, monkeypatch):
    admin_token = "admin-token-that-is-at-least-24-characters"  # noqa: S105
    viewer_token = "viewer-token-that-is-at-least-24-characters"  # noqa: S105
    monkeypatch.setenv("AGENTOPS_API_KEY", admin_token)
    with TestClient(
        create_app(str(tmp_path / "auth.db")), backend_options={"use_uvloop": True}
    ) as client:
        assert client.get("/api/auth/status").json() == {"enabled": True}
        assert client.get("/api/session").status_code == 401
        assert client.get("/api/session", headers=authorized(admin_token)).json() == {
            "name": "bootstrap-admin",
            "role": "admin",
        }
        assert client.get("/api/projects").status_code == 401
        project = client.post(
            "/api/projects", json={"name": "Secured"}, headers=authorized(admin_token)
        )
        assert project.status_code == 201
        user = client.post(
            "/api/users",
            json={"name": "reader", "token": viewer_token, "role": "viewer"},
            headers=authorized(admin_token),
        )
        assert user.status_code == 201
        assert client.get("/api/projects", headers=authorized(viewer_token)).status_code == 200
        member = client.put(
            "/api/project-members",
            json={"project_id": project.json()["id"], "user_name": "reader", "role": "viewer"},
            headers=authorized(admin_token),
        )
        assert member.status_code == 201
        assert (
            client.get(
                f"/api/projects/{project.json()['id']}", headers=authorized(viewer_token)
            ).status_code
            == 200
        )
        forbidden = client.post(
            "/api/projects", json={"name": "No write"}, headers=authorized(viewer_token)
        )
        assert forbidden.status_code == 403
        audit = client.get("/api/audit", headers=authorized(admin_token)).json()
        assert {event["resource"] for event in audit} >= {"/api/projects", "/api/users"}
        assert "token_hash" not in client.get("/api/users", headers=authorized(admin_token)).text


def test_encrypted_secrets_and_trace_redaction(tmp_path: Path, monkeypatch):
    admin_token = "admin-token-that-is-at-least-24-characters"  # noqa: S105
    monkeypatch.setenv("AGENTOPS_API_KEY", admin_token)
    monkeypatch.setenv("AGENTOPS_ENCRYPTION_KEY", "test encryption phrase with sufficient entropy")
    headers = authorized(admin_token)
    with TestClient(
        create_app(str(tmp_path / "vault.db")), backend_options={"use_uvloop": True}
    ) as client:
        project = client.post("/api/projects", json={"name": "Vault"}, headers=headers).json()
        secret = client.put(
            "/api/secrets",
            json={"project_id": project["id"], "name": "provider", "value": "top-secret"},
            headers=headers,
        ).json()
        assert "top-secret" not in str(secret)
        assert client.get(f"/api/secrets/{secret['id']}/reveal", headers=headers).json() == {
            "value": "top-secret"
        }
        with client.app.state.service.db.connect() as connection:
            ciphertext = connection.execute(
                "SELECT ciphertext FROM secrets WHERE id=?", (secret["id"],)
            ).fetchone()[0]
        assert "top-secret" not in ciphertext

        workflow = client.post(
            "/api/workflows",
            json={
                "project_id": project["id"],
                "name": "Redaction",
                "steps": [{"name": "Pass", "tool": "input"}],
            },
            headers=headers,
        ).json()
        run = client.post(
            f"/api/workflows/{workflow['id']}/runs",
            json={"input": {"password": "visible-to-execution", "safe": "shown"}},
            headers=headers,
        ).json()
        assert run["output"] == {"password": "[REDACTED]", "safe": "shown"}
        replay = client.post(f"/api/runs/{run['id']}/replay", headers=headers).json()
        assert replay["output"]["safe"] == "shown"
