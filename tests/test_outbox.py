import hashlib
import hmac

from fastapi.testclient import TestClient


def _create_webhook(
    client: TestClient, project_id: int, url: str, events: list[str] | None = None
) -> dict:
    response = client.post(
        "/api/webhooks",
        json={"project_id": project_id, "url": url, "events": events or ["run.completed"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pending_outbox(client: TestClient) -> list[dict]:
    return client.get("/api/outbox?status=pending").json()


def test_webhook_creation_rejects_private_targets(client: TestClient, project: dict):
    for url in ("http://localhost/hook", "http://127.0.0.1/hook", "http://10.0.0.5/hook", "http://169.254.169.254/hook"):
        response = client.post(
            "/api/webhooks",
            json={"project_id": project["id"], "url": url, "events": ["run.completed"]},
        )
        assert response.status_code == 422, (url, response.text)


def test_run_completion_enqueues_and_delivers_webhook(client: TestClient, project: dict):
    captured = []
    client.app.state.service._send_webhook = (
        lambda url, payload, delivery_id=None: captured.append((url, payload, delivery_id))
    )
    _create_webhook(client, project["id"], "https://8.8.8.8/hook")
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Notify",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"}).json()

    pending = _pending_outbox(client)
    assert len(pending) == 1
    row = pending[0]
    assert row["destination"].startswith("webhook:")
    assert row["event"] == "run.completed"
    assert row["payload"]["run"]["id"] == run["id"]

    client.app.state.service._deliver_outbox(row["id"])
    assert captured and captured[0][0] == "https://8.8.8.8/hook"
    assert captured[0][2] == row["id"]
    delivered = client.get("/api/outbox?status=delivered").json()
    assert any(item["id"] == row["id"] for item in delivered)


def test_webhook_delivery_is_hmac_signed(client: TestClient, monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    def fake_urlopen(request, timeout=10):
        captured["url"] = request.full_url
        captured["headers"] = {key.lower(): value for key, value in request.headers.items()}
        captured["body"] = request.data
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTOPS_WEBHOOK_SECRET", "s3cret-value")
    service = client.app.state.service
    service._send_webhook("https://8.8.8.8/hook", {"event": "run.completed"}, delivery_id=9)

    assert captured["url"] == "https://8.8.8.8/hook"
    assert captured["headers"]["x-agentops-delivery"] == "9"
    signature = captured["headers"]["x-agentops-signature"]
    assert signature.startswith("t=")
    timestamp = signature.split(",")[0][2:]
    expected = hmac.new(
        b"s3cret-value", f"{timestamp}.".encode() + captured["body"], hashlib.sha256
    ).hexdigest()
    assert signature == f"t={timestamp},v1={expected}"


def test_send_webhook_rejects_private_addresses(client: TestClient):
    service = client.app.state.service
    for url in ("http://127.0.0.1/hook", "http://10.1.2.3/hook", "http://169.254.169.254/latest/meta-data"):
        try:
            service._send_webhook(url, {"event": "x"})
        except ValueError as error:
            assert "must not" in str(error), (url, error)
        else:
            raise AssertionError(f"private URL was not rejected: {url}")


def test_webhook_retries_then_dead_letters(client: TestClient, project: dict):
    def always_fail(url, payload, delivery_id=None):
        raise RuntimeError("connection refused")

    client.app.state.service._send_webhook = always_fail
    _create_webhook(client, project["id"], "https://8.8.8.8/hook")
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Notify fail",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
    row = _pending_outbox(client)[0]

    service = client.app.state.service
    for _ in range(6):
        service._deliver_outbox(row["id"])
    dead = client.get("/api/outbox?status=dead").json()
    assert any(item["id"] == row["id"] for item in dead)
    assert dead[0]["attempts"] == 6
    assert "connection refused" in dead[0]["last_error"]
    deliveries = client.get(f"/api/webhooks?project_id={project['id']}").json()
    assert deliveries[0]["id"] >= 0  # webhook still listed


def test_outbox_deduplicates_repeated_dispatches(client: TestClient, project: dict):
    _create_webhook(client, project["id"], "https://8.8.8.8/hook")
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Dedupe",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"}).json()
    service = client.app.state.service
    service._dispatch_webhooks(run["id"], "run.completed")
    service._dispatch_webhooks(run["id"], "run.completed")
    assert len(_pending_outbox(client)) == 1


def test_slack_and_email_notifications_use_outbox(client: TestClient, project: dict, monkeypatch):
    monkeypatch.setenv("AGENTOPS_SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/x")
    monkeypatch.setenv("AGENTOPS_SMTP_HOST", "smtp.test")
    monkeypatch.setenv("AGENTOPS_EMAIL_TO", "ops@test.example")
    slack_calls, email_calls = [], []
    client.app.state.service.notifier._slack = lambda url, event, payload: slack_calls.append(event)
    client.app.state.service.notifier._email = lambda event, payload: email_calls.append(event)

    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Notify channels",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})

    pending = _pending_outbox(client)
    destinations = {row["destination"] for row in pending}
    assert {"slack", "email"} <= destinations

    service = client.app.state.service
    for row in pending:
        service._deliver_outbox(row["id"])
    assert slack_calls == ["run.completed"]
    assert email_calls == ["run.completed"]


def test_outbox_claim_prevents_double_delivery(client: TestClient, project: dict):
    captured = []
    client.app.state.service._send_webhook = (
        lambda url, payload, delivery_id=None: captured.append(url)
    )
    _create_webhook(client, project["id"], "https://8.8.8.8/hook")
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Claim",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
    row = _pending_outbox(client)[0]
    service = client.app.state.service

    # Simulate a second worker that claimed the row a moment ago.
    with service.db.connect() as connection:
        connection.execute(
            "UPDATE outbox_events SET status='claimed', claimed_at=? WHERE id=?",
            ("2099-01-01T00:00:00+00:00", row["id"]),
        )
    service._deliver_outbox(row["id"])
    assert captured == [], "a row claimed by another worker was delivered"

    # The claiming worker finishes and marks it delivered.
    with service.db.connect() as connection:
        connection.execute(
            "UPDATE outbox_events SET status='delivered', claimed_at=NULL WHERE id=?",
            (row["id"],),
        )
    assert client.get("/api/outbox?status=delivered").json()[0]["id"] == row["id"]


def test_outbox_reclaims_expired_leases(client: TestClient, project: dict):
    captured = []
    client.app.state.service._send_webhook = (
        lambda url, payload, delivery_id=None: captured.append(url)
    )
    _create_webhook(client, project["id"], "https://8.8.8.8/hook")
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Lease",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
    row = _pending_outbox(client)[0]
    service = client.app.state.service

    # A crashed worker left the row claimed past its lease; the next worker reclaims.
    with service.db.connect() as connection:
        connection.execute(
            "UPDATE outbox_events SET status='claimed', claimed_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", row["id"]),
        )
    service._deliver_outbox(row["id"])
    assert captured == ["https://8.8.8.8/hook"]
    assert client.get("/api/outbox?status=delivered").json()[0]["id"] == row["id"]


def test_deleted_webhook_dead_letters_immediately(client: TestClient, project: dict):
    webhook = _create_webhook(client, project["id"], "https://8.8.8.8/hook")
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Orphan",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
    row = _pending_outbox(client)[0]

    # Deleting the project cascades the webhook away while the row is queued.
    assert client.delete(f"/api/projects/{project['id']}").status_code == 200
    assert client.get(f"/api/webhooks/{webhook['id']}").status_code == 404

    client.app.state.service._deliver_outbox(row["id"])
    dead = client.get("/api/outbox?status=dead").json()
    assert any(item["id"] == row["id"] for item in dead)
    assert dead[0]["attempts"] == 1
    assert "no longer exists" in dead[0]["last_error"]


def test_outbox_endpoint_lists_with_status_filter(client: TestClient, project: dict):
    _create_webhook(client, project["id"], "https://8.8.8.8/hook")
    workflow = client.post(
        "/api/workflows",
        json={
            "project_id": project["id"],
            "name": "Outbox list",
            "steps": [{"name": "Pass", "tool": "input"}],
        },
    ).json()
    client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
    rows = client.get("/api/outbox").json()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert client.get("/api/outbox?status=dead").json() == []
