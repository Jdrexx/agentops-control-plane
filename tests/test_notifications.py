from src.agentops.notifications import Notifier


def test_notifier_routes_to_configured_channels(monkeypatch):
    calls = []
    notifier = Notifier()
    monkeypatch.setenv("AGENTOPS_SLACK_WEBHOOK_URL", "https://hooks.slack.test/agentops")
    monkeypatch.setenv("AGENTOPS_SMTP_HOST", "smtp.test")
    monkeypatch.setenv("AGENTOPS_EMAIL_TO", "operator@example.test")
    monkeypatch.setattr(notifier, "_slack", lambda url, event, payload: calls.append("slack"))
    monkeypatch.setattr(notifier, "_email", lambda event, payload: calls.append("email"))
    notifier.notify("approval.pending", {"run": {"id": 1}})
    assert calls == ["slack", "email"]
