#!/usr/bin/env python3
"""Smoke-test the exact Docker Compose portfolio-demo path."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8110"
TIMEOUT_SECONDS = 60


def request(path: str, *, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed loopback demo endpoint
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode()
        raise RuntimeError(f"{method} {path} failed: {error.code} {detail}") from error


def wait_for(path: str, predicate, description: str):
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last = None
    while time.monotonic() < deadline:
        try:
            last = request(path)
            if predicate(last):
                return last
        except (OSError, RuntimeError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {description}; last response: {last!r}")


def seed(scenario: str):
    return request(
        "/api/demo/seed",
        method="POST",
        body={"scenario": scenario, "reset": True},
    )


def main() -> None:
    ready = wait_for(
        "/api/ready",
        lambda value: value.get("status") == "ready",
        "Compose service readiness",
    )
    assert ready["database"] == "ok"
    assert ready["process"] == "all"

    tour = seed("tour")
    assert tour["next_action"] == "approve"
    assert tour.get("approval_id"), tour
    request(
        f"/api/approvals/{tour['approval_id']}/decision",
        method="POST",
        body={"decision": "approved", "note": "automated demo smoke"},
    )
    completed = wait_for(
        f"/api/runs/{tour['focus_run_id']}",
        lambda value: value.get("status") == "completed",
        "support tour completion after approval",
    )
    assert completed["status"] == "completed"

    quality = seed("quality")
    assert quality["pass_rates"] == [4 / 6, 1.0], quality
    left, right = quality["focus_eval_ids"]
    comparison = request(f"/api/evaluations/{left}/compare/{right}")
    assert comparison, "evaluation comparison returned no data"

    incident = seed("incident")
    failed = wait_for(
        f"/api/runs/{incident['focus_run_id']}",
        lambda value: value.get("status") == "failed",
        "incident failure after retries",
    )
    assert failed["status"] == "failed"
    alerts = request("/api/alerts")
    assert any(alert.get("triggered") for alert in alerts), alerts

    print("Demo smoke passed: ready, tour, approval, quality diff, incident, alert")


if __name__ == "__main__":
    main()
