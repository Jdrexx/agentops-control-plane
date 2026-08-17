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


def test_project_membership_scopes_resources_and_live_updates(tmp_path: Path, monkeypatch):
    admin_token = "admin-token-that-is-at-least-24-characters"  # noqa: S105
    operator_token = "operator-token-that-is-at-least-24-characters"  # noqa: S105
    monkeypatch.setenv("AGENTOPS_API_KEY", admin_token)
    admin = authorized(admin_token)
    operator = authorized(operator_token)

    with TestClient(
        create_app(str(tmp_path / "scoped.db")), backend_options={"use_uvloop": True}
    ) as client:
        client.post(
            "/api/users",
            json={"name": "operator", "token": operator_token, "role": "operator"},
            headers=admin,
        )
        allowed_project = client.post(
            "/api/projects", json={"name": "Allowed"}, headers=admin
        ).json()
        hidden_project = client.post(
            "/api/projects", json={"name": "Hidden"}, headers=admin
        ).json()
        client.put(
            "/api/project-members",
            json={
                "project_id": allowed_project["id"],
                "user_name": "operator",
                "role": "operator",
            },
            headers=admin,
        )

        def workflow(project_id: int, name: str, tool: str = "input") -> dict:
            return client.post(
                "/api/workflows",
                json={
                    "project_id": project_id,
                    "name": name,
                    "steps": [{"name": name, "tool": tool}],
                },
                headers=admin,
            ).json()

        allowed_workflow = workflow(allowed_project["id"], "Allowed workflow")
        hidden_workflow = workflow(hidden_project["id"], "Hidden workflow")
        allowed_run = client.post(
            f"/api/workflows/{allowed_workflow['id']}/runs",
            json={"input": "allowed"},
            headers=admin,
        ).json()
        hidden_run = client.post(
            f"/api/workflows/{hidden_workflow['id']}/runs",
            json={"input": "hidden"},
            headers=admin,
        ).json()

        def dataset(project_id: int, name: str) -> dict:
            return client.post(
                "/api/datasets",
                json={
                    "project_id": project_id,
                    "name": name,
                    "cases": [{"input": name, "expected": name}],
                },
                headers=admin,
            ).json()

        allowed_dataset = dataset(allowed_project["id"], "Allowed cases")
        hidden_dataset = dataset(hidden_project["id"], "Hidden cases")
        allowed_evaluation = client.post(
            "/api/evaluations",
            json={
                "workflow_id": allowed_workflow["id"],
                "dataset_id": allowed_dataset["id"],
            },
            headers=admin,
        ).json()
        hidden_evaluation = client.post(
            "/api/evaluations",
            json={
                "workflow_id": hidden_workflow["id"],
                "dataset_id": hidden_dataset["id"],
            },
            headers=admin,
        ).json()
        allowed_schedule = client.post(
            "/api/schedules",
            json={
                "workflow_id": allowed_workflow["id"],
                "name": "Allowed schedule",
                "input": "allowed",
                "interval_seconds": 60,
            },
            headers=admin,
        ).json()
        hidden_schedule = client.post(
            "/api/schedules",
            json={
                "workflow_id": hidden_workflow["id"],
                "name": "Hidden schedule",
                "input": "hidden",
                "interval_seconds": 60,
            },
            headers=admin,
        ).json()
        allowed_approval_workflow = workflow(allowed_project["id"], "Allowed approval", "approval")
        hidden_approval_workflow = workflow(hidden_project["id"], "Hidden approval", "approval")
        client.post(
            f"/api/workflows/{allowed_approval_workflow['id']}/runs",
            json={"input": "allowed"},
            headers=admin,
        )
        client.post(
            f"/api/workflows/{hidden_approval_workflow['id']}/runs",
            json={"input": "hidden"},
            headers=admin,
        )
        allowed_approval, hidden_approval = client.get(
            "/api/approvals?status=pending", headers=admin
        ).json()
        if allowed_approval["run_id"] > hidden_approval["run_id"]:
            allowed_approval, hidden_approval = hidden_approval, allowed_approval

        assert [item["id"] for item in client.get("/api/projects", headers=operator).json()] == [
            allowed_project["id"]
        ]
        assert {
            item["id"] for item in client.get("/api/workflows", headers=operator).json()
        } == {allowed_workflow["id"], allowed_approval_workflow["id"]}
        visible_runs = client.get("/api/runs", headers=operator).json()
        assert {item["workflow_id"] for item in visible_runs} == {
            allowed_workflow["id"],
            allowed_approval_workflow["id"],
        }
        assert [item["id"] for item in client.get("/api/datasets", headers=operator).json()] == [
            allowed_dataset["id"]
        ]
        assert [
            item["id"] for item in client.get("/api/evaluations", headers=operator).json()
        ] == [allowed_evaluation["id"]]
        assert [item["id"] for item in client.get("/api/schedules", headers=operator).json()] == [
            allowed_schedule["id"]
        ]
        assert [
            item["id"]
            for item in client.get("/api/approvals?status=pending", headers=operator).json()
        ] == [allowed_approval["id"]]
        assert client.get("/api/stats", headers=operator).json()["total_runs"] == len(
            visible_runs
        )

        assert (
            client.get(f"/api/workflows/{hidden_workflow['id']}", headers=operator).status_code
            == 403
        )
        assert client.get(f"/api/runs/{hidden_run['id']}", headers=operator).status_code == 403
        assert (
            client.get(f"/api/datasets/{hidden_dataset['id']}", headers=operator).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/evaluations/{hidden_evaluation['id']}", headers=operator
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/schedules/{hidden_schedule['id']}/enabled?enabled=false",
                headers=operator,
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/approvals/{hidden_approval['id']}/decision",
                json={"decision": "approved"},
                headers=operator,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/webhooks",
                json={
                    "project_id": allowed_project["id"],
                    "url": "https://hooks.example.test/runs",
                    "events": ["run.completed"],
                },
                headers=operator,
            ).status_code
            == 403
        )

        with client.websocket_connect("/api/live") as websocket:
            websocket.send_json({"type": "authenticate", "token": operator_token})
            message = websocket.receive_json()
        assert {item["workflow_id"] for item in message["runs"]} == {
            allowed_workflow["id"],
            allowed_approval_workflow["id"],
        }
        assert message["stats"]["total_runs"] == len(visible_runs)

        created = client.post(
            "/api/projects", json={"name": "Operator created"}, headers=operator
        )
        assert created.status_code == 201
        assert (
            client.get(f"/api/projects/{created.json()['id']}", headers=operator).status_code
            == 200
        )

        unauthorized = client.get("/api/session")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["x-content-type-options"] == "nosniff"
        assert unauthorized.headers["cache-control"] == "no-store"
        assert client.get(f"/api/runs/{allowed_run['id']}", headers=operator).status_code == 200


def test_evaluation_and_replay_preserve_operator_role(tmp_path: Path, monkeypatch):
    admin_token = "admin-token-that-is-at-least-24-characters"  # noqa: S105
    operator_token = "operator-token-that-is-at-least-24-characters"  # noqa: S105
    monkeypatch.setenv("AGENTOPS_API_KEY", admin_token)
    admin = authorized(admin_token)
    operator = authorized(operator_token)

    with TestClient(
        create_app(str(tmp_path / "roles.db")), backend_options={"use_uvloop": True}
    ) as client:
        client.post(
            "/api/users",
            json={"name": "operator", "token": operator_token, "role": "operator"},
            headers=admin,
        )
        project = client.post("/api/projects", json={"name": "Roles"}, headers=admin).json()
        client.put(
            "/api/project-members",
            json={"project_id": project["id"], "user_name": "operator", "role": "operator"},
            headers=admin,
        )
        workflow = client.post(
            "/api/workflows",
            json={
                "project_id": project["id"],
                "name": "Admin-only step",
                "steps": [
                    {
                        "name": "Restricted",
                        "tool": "input",
                        "config": {"allowed_roles": ["admin"]},
                    }
                ],
            },
            headers=admin,
        ).json()
        dataset = client.post(
            "/api/datasets",
            json={
                "project_id": project["id"],
                "name": "Role cases",
                "cases": [{"input": "value", "expected": "value"}],
            },
            headers=admin,
        ).json()

        evaluation = client.post(
            "/api/evaluations",
            json={"workflow_id": workflow["id"], "dataset_id": dataset["id"]},
            headers=operator,
        )
        assert evaluation.status_code == 201
        assert evaluation.json()["passed"] == 0
        original = client.get(
            f"/api/runs/{evaluation.json()['results'][0]['run_id']}", headers=operator
        ).json()
        assert original["actor_role"] == "operator"
        assert original["status"] == "failed"

        replay = client.post(f"/api/runs/{original['id']}/replay", headers=operator)
        assert replay.status_code == 201
        assert replay.json()["actor_role"] == "operator"
        assert replay.json()["status"] == "failed"
