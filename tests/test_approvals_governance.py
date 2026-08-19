from pathlib import Path

from fastapi.testclient import TestClient

from src.agentops.main import create_app


def _approval_workflow(
    client: TestClient, project_id: int, approver_roles=None, headers=None
):
    config = {"prompt": "Approve this?"}
    if approver_roles is not None:
        config["approver_roles"] = approver_roles
    response = client.post(
        "/api/workflows",
        json={
            "project_id": project_id,
            "name": "Governed",
            "steps": [{"name": "Review", "tool": "approval", "config": config}],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pending_approval(client: TestClient, run_id: int, headers=None) -> dict:
    approvals = client.get("/api/approvals?status=pending", headers=headers).json()
    return next(item for item in approvals if item["run_id"] == run_id)


def test_approver_roles_restrict_decisions(tmp_path: Path, monkeypatch):
    admin_token = "admin-token-that-is-at-least-24-characters"  # noqa: S105
    operator_token = "operator-token-that-is-at-least-24-characters"  # noqa: S105
    monkeypatch.setenv("AGENTOPS_API_KEY", admin_token)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    operator_headers = {"Authorization": f"Bearer {operator_token}"}
    with TestClient(create_app(str(tmp_path / "governance.db"))) as client:
        project = client.post(
            "/api/projects", json={"name": "Governed"}, headers=admin_headers
        ).json()
        client.post(
            "/api/users",
            json={"name": "op-user", "token": operator_token, "role": "operator"},
            headers=admin_headers,
        )
        client.put(
            "/api/project-members",
            json={"project_id": project["id"], "user_name": "op-user", "role": "operator"},
            headers=admin_headers,
        )
        workflow = _approval_workflow(
            client, project["id"], approver_roles=["admin"], headers=admin_headers
        )
        run = client.post(
            f"/api/workflows/{workflow['id']}/runs",
            json={"input": "x"},
            headers=operator_headers,
        ).json()
        assert run["status"] == "waiting_approval"
        approval = _pending_approval(client, run["id"], headers=admin_headers)

        denied = client.post(
            f"/api/approvals/{approval['id']}/decision",
            json={"decision": "approved"},
            headers=operator_headers,
        )
        assert denied.status_code == 409
        assert "not allowed" in denied.json()["detail"]

        approved = client.post(
            f"/api/approvals/{approval['id']}/decision",
            json={"decision": "approved"},
            headers=admin_headers,
        ).json()
        assert approved["status"] == "completed"


def test_decided_by_and_policy_are_recorded(client: TestClient, project: dict):
    workflow = _approval_workflow(client, project["id"], approver_roles=["admin", "operator"])
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"}).json()
    approval = _pending_approval(client, run["id"])
    assert approval["policy"]["approver_roles"] == ["admin", "operator"]

    client.post(
        f"/api/approvals/{approval['id']}/decision",
        json={"decision": "approved", "note": "fine"},
    )
    decided = client.get("/api/approvals?status=approved").json()
    row = next(item for item in decided if item["id"] == approval["id"])
    assert row["decided_by"] == "local-user"
    assert row["note"] == "fine"


def test_expiry_is_derived_at_read_time(client: TestClient, project: dict):
    workflow = _approval_workflow(client, project["id"])
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"}).json()
    approval = _pending_approval(client, run["id"])
    with client.app.state.service.db.connect() as connection:
        connection.execute(
            "UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (approval["id"],),
        )

    pending = client.get("/api/approvals?status=pending").json()
    assert all(item["id"] != approval["id"] for item in pending)
    expired = client.get("/api/approvals?status=expired").json()
    assert any(item["id"] == approval["id"] for item in expired)

    decision = client.post(
        f"/api/approvals/{approval['id']}/decision", json={"decision": "approved"}
    )
    assert decision.status_code == 409
    assert "expired" in decision.json()["detail"]
    # The derived state was materialized for the record.
    materialized = client.get("/api/approvals?status=expired").json()
    assert any(item["id"] == approval["id"] for item in materialized)


def test_approver_roles_schema_validation(client: TestClient, project: dict):
    for bad_roles in (["admin", "superuser"], ["viewer"], ["viewer", "operator"]):
        response = client.post(
            "/api/workflows",
            json={
                "project_id": project["id"],
                "name": "Bad policy",
                "steps": [
                    {
                        "name": "Review",
                        "tool": "approval",
                        "config": {"approver_roles": bad_roles},
                    }
                ],
            },
        )
        assert response.status_code == 422, bad_roles
