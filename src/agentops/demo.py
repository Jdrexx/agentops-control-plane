"""Server-side demo scenarios.

One ``POST /api/demo/seed`` builds a complete, idempotent, offline scenario that
exercises the product's real paths — no frontend ``seed()``, no API keys, no
network. Every scenario uses the deterministic ``mock`` provider, and the
``quality`` scenario pins response scripts so the regression story is exact:
workflow v1 fails two evaluation cases, workflow v2 fixes them.

Scenarios:

- ``tour`` — streamed draft -> human approval -> handoff to a QA child run ->
  memory write. The run pauses at the approval so a reviewer can click
  "Approve & resume" and watch the rest execute.
- ``quality`` — a 6-case dataset; workflow v1 passes 4/6, immutable v2 passes
  6/6. Returns both evaluation ids for a case-level comparison.
- ``incident`` — an LLM step that fails after retries (deterministic provider
  failure injection), a triggered failure-rate alert, and a failed run in the
  feed.

All scenarios are idempotent by fixed project name: seeding again returns the
existing focus without duplicating data. ``reset=True`` deletes and recreates
the demo project.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .providers import _mock_fingerprint

MODEL = "mock-small"

SCENARIOS: dict[str, dict[str, str]] = {
    "tour": {
        "title": "Support triage",
        "blurb": "Draft -> approval -> QA handoff. Approve the pending action to continue.",
    },
    "quality": {
        "title": "Regression check",
        "blurb": "v1 fails 2 of 6 evaluation cases; v2 fixes them. Compare the two runs.",
    },
    "incident": {
        "title": "Incident response",
        "blurb": "A billing sync that fails after retries and trips the failure-rate alert.",
    },
}

BUILDERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {}


def _script_dir() -> Path:
    return Path(os.getenv("AGENTOPS_MOCK_SCRIPT_DIR", "examples/mock_responses"))


def _pin(model: str, system: str, prompt: str, text: str) -> None:
    """Write a pinned response script for a prompt, unless one already exists."""
    fingerprint = _mock_fingerprint(model, system, prompt)
    target = _script_dir() / f"{fingerprint[:16]}.txt"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _pin_failure(model: str, system: str, prompt: str, message: str) -> None:
    """Write a deterministic failure script for a prompt, unless one exists."""
    fingerprint = _mock_fingerprint(model, system, prompt)
    target = _script_dir() / f"{fingerprint[:16]}.fail"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(message, encoding="utf-8")


def _find_project(service: Any, name: str) -> dict[str, Any] | None:
    for project in service.list_projects():
        if project["name"] == name:
            return project
    return None


def _workflow_named(service: Any, project_id: int, name: str) -> list[dict[str, Any]]:
    return [
        workflow
        for workflow in service.list_workflows(project_id)
        if workflow["name"] == name
    ]


def _latest_run(service: Any, workflow_id: int) -> dict[str, Any] | None:
    runs = service.list_runs(workflow_id, limit=1)
    return runs[0] if runs else None


def _existing_focus(service: Any, scenario: str, project: dict[str, Any]) -> dict[str, Any] | None:
    """Return focus pointers into an already-seeded project, or None."""
    if scenario == "tour":
        workflows = _workflow_named(service, project["id"], "Customer response")
        if not workflows:
            return None
        parent = workflows[-1]
        run = _latest_run(service, parent["id"])
        if run is None:
            return None
        return {
            "focus": "trace",
            "focus_run_id": run["id"],
            "next_action": "approve" if run["status"] == "waiting_approval" else None,
        }
    if scenario == "quality":
        workflows = sorted(
            _workflow_named(service, project["id"], "Ticket classifier"),
            key=lambda workflow: workflow["version"],
        )
        if len(workflows) < 2:
            return None
        v1, v2 = workflows[0], workflows[-1]
        evals_v1 = service.list_evaluations(v1["id"], limit=1)
        evals_v2 = service.list_evaluations(v2["id"], limit=1)
        if not evals_v1 or not evals_v2:
            return None
        return {"focus": "quality", "focus_eval_ids": [evals_v1[0]["id"], evals_v2[0]["id"]]}
    workflows = _workflow_named(service, project["id"], "Nightly billing sync")
    if not workflows:
        return None
    run = _latest_run(service, workflows[-1]["id"])
    if run is None:
        return None
    return {"focus": "trace", "focus_run_id": run["id"]}


def seed(service: Any, scenario: str, reset: bool = False) -> dict[str, Any]:
    """Build or reuse the requested demo scenario.

    Idempotent: a project with the scenario's fixed name is reused (and its
    current focus returned) unless ``reset`` deletes it first.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown demo scenario: {scenario}")
    name = f"Demo — {SCENARIOS[scenario]['title']}"
    project = _find_project(service, name)
    if project is not None and not reset:
        existing = _existing_focus(service, scenario, project)
        if existing is not None:
            return {"scenario": scenario, "project_id": project["id"], **existing}
    if project is not None:
        service.delete_project(project["id"])
        project = None
    if project is None:
        project = service.create_project(name, "Automated demo scenario")
    result = BUILDERS[scenario](service, project)
    result["scenario"] = scenario
    result["project_id"] = project["id"]
    return result


def _seed_tour(service: Any, project: dict[str, Any]) -> dict[str, Any]:
    child = service.create_workflow(
        project["id"],
        "QA policy check",
        [
            {
                "name": "Review",
                "tool": "llm",
                "config": {
                    "provider": "mock",
                    "model": MODEL,
                    "system": "You are a QA reviewer. Reply PASS or FAIL with one reason.",
                    "prompt": "Review this response for tone and accuracy:\n{value}",
                    "input_cost_per_1k": 0.003,
                    "output_cost_per_1k": 0.015,
                },
            }
        ],
    )
    parent = service.create_workflow(
        project["id"],
        "Customer response",
        [
            {
                "name": "Draft reply",
                "tool": "llm",
                "config": {
                    "provider": "mock",
                    "model": MODEL,
                    "system": "You are a support agent. Be concise and warm.",
                    "prompt": "Draft a reply to: {value}",
                    "input_cost_per_1k": 0.003,
                    "output_cost_per_1k": 0.015,
                },
                "retries": 2,
                "retry_delay_seconds": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "Human approval",
                "tool": "approval",
                "config": {
                    "prompt": "Send this reply to the customer?",
                    "expires_in_seconds": 3600,
                },
            },
            {
                "name": "QA handoff",
                "tool": "handoff",
                "config": {"workflow_id": child["id"], "max_steps": 4},
            },
            {"name": "Remember reply", "tool": "memory_write", "config": {"key": "last_reply"}},
        ],
    )
    run = service.start_run(parent["id"], "My invoice is wrong again.")
    return {
        "focus": "trace",
        "focus_run_id": run["id"],
        "next_action": "approve" if run["status"] == "waiting_approval" else None,
        "blurb": SCENARIOS["tour"]["blurb"],
    }


def _seed_quality(service: Any, project: dict[str, Any]) -> dict[str, Any]:
    system = "You are a support ticket classifier. Reply with exactly one word."
    prompt_v1 = "Classify this support ticket: {value}"
    prompt_v2 = "Classify this support ticket: {value} Focus on the root cause."
    cases = [
        ("My invoice shows a charge I never made", "BILLING"),
        ("I was double charged for my subscription", "BILLING"),
        ("The login page keeps rejecting my password", "TECHNICAL"),
        ("My dashboard will not load after the update", "TECHNICAL"),
        ("Can I get a refund for last month?", "BILLING"),
        ("How do I export my data?", "OTHER"),
    ]
    # Pin responses so the regression is exact and offline: on v1 the first two
    # billing tickets are misclassified as OTHER (2 failures); on v2 every case
    # is classified correctly (6/6). Pins are written once and reused.
    for index, (case_input, label) in enumerate(cases):
        v1_answer = "OTHER" if index < 2 else label
        _pin(MODEL, system, prompt_v1.replace("{value}", case_input), v1_answer)
        _pin(MODEL, system, prompt_v2.replace("{value}", case_input), label)
    v1 = service.create_workflow(
        project["id"],
        "Ticket classifier",
        [
            {
                "name": "Classify",
                "tool": "llm",
                "config": {
                    "provider": "mock",
                    "model": MODEL,
                    "system": system,
                    "prompt": prompt_v1,
                },
            }
        ],
    )
    v2 = service.create_workflow(
        project["id"],
        "Ticket classifier",
        [
            {
                "name": "Classify",
                "tool": "llm",
                "config": {
                    "provider": "mock",
                    "model": MODEL,
                    "system": system,
                    "prompt": prompt_v2,
                },
            }
        ],
    )
    dataset = service.create_dataset(
        project["id"],
        "Ticket classification",
        [
            {"input": case_input, "expected": label, "matcher": "contains"}
            for case_input, label in cases
        ],
    )
    eval_v1 = service.evaluate(v1["id"], dataset["id"])
    eval_v2 = service.evaluate(v2["id"], dataset["id"])
    return {
        "focus": "quality",
        "focus_eval_ids": [eval_v1["id"], eval_v2["id"]],
        "pass_rates": [eval_v1["pass_rate"], eval_v2["pass_rate"]],
        "blurb": SCENARIOS["quality"]["blurb"],
    }


def _seed_incident(service: Any, project: dict[str, Any]) -> dict[str, Any]:
    system = "You are the billing sync agent."
    prompt = "Process billing file: {value}"
    run_input = "invoice-20260818.json"
    _pin_failure(
        MODEL,
        system,
        prompt.replace("{value}", run_input),
        "connection refused to billing-api.internal:443 after 30s",
    )
    workflow = service.create_workflow(
        project["id"],
        "Nightly billing sync",
        [
            {
                "name": "Sync invoices",
                "tool": "llm",
                "config": {
                    "provider": "mock",
                    "model": MODEL,
                    "system": system,
                    "prompt": prompt,
                },
                "retries": 2,
                "retry_delay_seconds": 1,
            }
        ],
    )
    # Alert metrics are computed from global stats, so a 0.5 threshold would
    # never fire once the other demo scenarios have filled the database with
    # successful runs. A >1% threshold trips on any failure in any order.
    service.create_alert("Billing sync failing", "failure_rate", 0.01)
    run = service.start_run(workflow["id"], run_input)
    return {
        "focus": "trace",
        "focus_run_id": run["id"],
        "blurb": SCENARIOS["incident"]["blurb"],
    }


BUILDERS["tour"] = _seed_tour
BUILDERS["quality"] = _seed_quality
BUILDERS["incident"] = _seed_incident
