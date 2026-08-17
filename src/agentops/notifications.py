from __future__ import annotations

import json
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any


class Notifier:
    def notify(self, event: str, payload: dict[str, Any]) -> None:
        slack_url = os.getenv("AGENTOPS_SLACK_WEBHOOK_URL")
        if slack_url:
            self._slack(slack_url, event, payload)
        if os.getenv("AGENTOPS_SMTP_HOST") and os.getenv("AGENTOPS_EMAIL_TO"):
            self._email(event, payload)

    @staticmethod
    def _slack(url: str, event: str, payload: dict[str, Any]) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("Slack webhook URL must use HTTPS")
        body = {"text": f"AgentOps {event}: run #{payload['run']['id']}"}
        request = urllib.request.Request(  # noqa: S310 -- HTTPS validated above.
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10):  # noqa: S310  # nosec B310
                pass
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(f"Slack notification failed: {error}") from error

    @staticmethod
    def _email(event: str, payload: dict[str, Any]) -> None:
        message = EmailMessage()
        message["Subject"] = f"AgentOps notification: {event}"
        message["From"] = os.getenv("AGENTOPS_EMAIL_FROM", "agentops@localhost")
        message["To"] = os.environ["AGENTOPS_EMAIL_TO"]
        message.set_content(json.dumps(payload, indent=2, default=str))
        host = os.environ["AGENTOPS_SMTP_HOST"]
        port = int(os.getenv("AGENTOPS_SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.starttls()
            username = os.getenv("AGENTOPS_SMTP_USERNAME")
            password = os.getenv("AGENTOPS_SMTP_PASSWORD")
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
