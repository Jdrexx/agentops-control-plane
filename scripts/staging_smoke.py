#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

DEFAULT_PROJECT_NAME = "AgentOps staging smoke"
DEFAULT_WORKFLOW_NAME = "Queued uppercase smoke"
EXPECTED_INPUT = "staging smoke"
EXPECTED_OUTPUT = "STAGING SMOKE"
TERMINAL_STATUSES = {"completed", "failed", "rejected", "cancelled"}


class SmokeError(RuntimeError):
    pass


class SmokeClient(Protocol):
    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any: ...


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        # The CLI validates the base URL as HTTPS before constructing this client.
        request = urllib.request.Request(  # noqa: S310  # nosec B310
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310  # nosec B310
                request, timeout=self.timeout
            ) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except json.JSONDecodeError:
                detail = body
            raise SmokeError(f"{method} {path} returned {error.code}: {detail}") from None
        except urllib.error.URLError as error:
            raise SmokeError(f"{method} {path} failed: {error.reason}") from None
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise SmokeError(f"{method} {path} returned invalid JSON") from error


def validate_target(base_url: str, allow_non_staging: bool = False) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SmokeError("the smoke target must be an absolute HTTPS URL")
    if not allow_non_staging and "staging" not in parsed.hostname.lower():
        raise SmokeError("refusing a non-staging target without --allow-non-staging")
    return base_url.rstrip("/")


def run_smoke(
    client: SmokeClient,
    *,
    project_name: str = DEFAULT_PROJECT_NAME,
    workflow_name: str = DEFAULT_WORKFLOW_NAME,
    wait_seconds: float = 30,
    poll_interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    ready = client.request("GET", "/api/ready")
    expected_ready = {"status": "ready", "database": "ok", "queue": "redis"}
    if any(ready.get(key) != value for key, value in expected_ready.items()):
        raise SmokeError(f"deployment is not dependency-ready: {ready}")

    session = client.request("GET", "/api/session")
    if session.get("role") != "admin":
        raise SmokeError("the staging smoke token must have the admin role")

    projects = client.request("GET", "/api/projects")
    project = next((item for item in projects if item["name"] == project_name), None)
    project_created = project is None
    if project is None:
        project = client.request(
            "POST",
            "/api/projects",
            {"name": project_name, "description": "Reusable staging smoke fixture"},
        )

    desired_steps = [{"name": "Uppercase", "tool": "uppercase", "config": {}}]
    project_id = int(project["id"])
    workflows = client.request(
        "GET", f"/api/workflows?project_id={urllib.parse.quote(str(project_id))}"
    )
    workflow = next(
        (
            item
            for item in workflows
            if item["name"] == workflow_name and item.get("steps") == desired_steps
        ),
        None,
    )
    workflow_created = workflow is None
    if workflow is None:
        workflow = client.request(
            "POST",
            "/api/workflows",
            {"project_id": project_id, "name": workflow_name, "steps": desired_steps},
        )

    run = client.request(
        "POST",
        f"/api/workflows/{int(workflow['id'])}/runs",
        {"input": EXPECTED_INPUT, "execution": "queued"},
    )
    deadline = time.monotonic() + wait_seconds
    while run.get("status") not in TERMINAL_STATUSES:
        if time.monotonic() >= deadline:
            raise SmokeError(f"queued run {run['id']} did not finish within {wait_seconds:g}s")
        sleep(poll_interval)
        run = client.request("GET", f"/api/runs/{int(run['id'])}")

    if run.get("status") != "completed":
        raise SmokeError(f"queued run {run['id']} ended as {run.get('status')}: {run.get('error')}")
    if run.get("output") != EXPECTED_OUTPUT:
        raise SmokeError(
            f"queued run {run['id']} returned {run.get('output')!r}, expected {EXPECTED_OUTPUT!r}"
        )
    spans = run.get("spans", [])
    if len(spans) != 1 or spans[0].get("tool") != "uppercase":
        raise SmokeError(f"queued run {run['id']} did not record the expected trace span")

    return {
        "status": "passed",
        "process": ready.get("process"),
        "queue": ready["queue"],
        "actor": session.get("name"),
        "project_id": project_id,
        "project_created": project_created,
        "workflow_id": int(workflow["id"]),
        "workflow_created": workflow_created,
        "run_id": int(run["id"]),
        "run_status": run["status"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a reusable authenticated smoke test against an isolated staging deployment."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("AGENTOPS_BASE_URL"),
        help="Staging base URL (or set AGENTOPS_BASE_URL).",
    )
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--workflow-name", default=DEFAULT_WORKFLOW_NAME)
    parser.add_argument("--wait-seconds", type=float, default=30)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--allow-non-staging",
        action="store_true",
        help="Explicitly allow a hostname that does not contain 'staging'.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not args.base_url:
            raise SmokeError("--base-url or AGENTOPS_BASE_URL is required")
        base_url = validate_target(args.base_url, args.allow_non_staging)
        token = os.getenv("AGENTOPS_API_KEY")
        if not token:
            raise SmokeError("AGENTOPS_API_KEY is required")
        result = run_smoke(
            ApiClient(base_url, token),
            project_name=args.project_name,
            workflow_name=args.workflow_name,
            wait_seconds=args.wait_seconds,
            poll_interval=args.poll_interval,
        )
    except SmokeError as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
