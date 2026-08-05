from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .database import Database
from .notifications import Notifier
from .providers import ProviderRegistry
from .queue import RunQueue
from .security import SecretVault, redact, token_hash
from .templates import WORKFLOW_TEMPLATES


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class AgentOpsService:
    def __init__(
        self,
        database: Database,
        providers: ProviderRegistry | None = None,
        consume_runs: bool = True,
    ):
        self.db = database
        self.providers = providers or ProviderRegistry()
        self.vault = SecretVault()
        self.notifier = Notifier()
        self.run_queue = RunQueue(self._execute_queued, consume=consume_runs)
        self.tool_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agentops-tool")
        self._scheduler_stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

    def close(self) -> None:
        self._scheduler_stop.set()
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=2)
        self.run_queue.close()
        self.tool_executor.shutdown(wait=False, cancel_futures=True)

    def start_scheduler(self) -> None:
        if self._scheduler_thread is not None:
            return
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="agentops-scheduler", daemon=True
        )
        self._scheduler_thread.start()

    def _scheduler_loop(self) -> None:
        while not self._scheduler_stop.wait(1):
            try:
                self.run_due_schedules()
                self.expire_approvals()
            except (sqlite3.Error, psycopg.Error, RuntimeError):
                continue

    def recover_incomplete_runs(self) -> int:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM runs WHERE status IN ('queued','running')"
            ).fetchall()
            connection.execute("UPDATE runs SET status='queued' WHERE status='running'")
        for row in rows:
            self.run_queue.submit(row["id"])
        return len(rows)

    def create_project(self, name: str, description: str) -> dict[str, Any]:
        try:
            with self.db.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO projects(name, description, created_at) VALUES (?, ?, ?)",
                    (name, description, now()),
                )
                project_id = cursor.lastrowid
        except (sqlite3.IntegrityError, psycopg.IntegrityError) as error:
            raise ConflictError("project name already exists") from error
        return self.get_project(int(project_id))

    def get_project(self, project_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("project not found")
        return dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def create_workflow(
        self, project_id: int, name: str, steps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.get_project(project_id)
        with self.db.connect() as connection:
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM workflows WHERE project_id=? AND name=?",
                (project_id, name),
            ).fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO workflows(project_id, name, version, definition, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, name.strip(), version, encode(steps), now()),
            )
            workflow_id = cursor.lastrowid
        return self.get_workflow(int(workflow_id))

    def get_workflow(self, workflow_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflows WHERE id=?", (workflow_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("workflow not found")
        result = dict(row)
        result["steps"] = decode(result.pop("definition"))
        return result

    def list_workflows(self, project_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workflows"
        params: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE project_id=?"
            params = (project_id,)
        query += " ORDER BY created_at DESC"
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["steps"] = decode(item.pop("definition"))
            results.append(item)
        return results

    def list_templates(self) -> list[dict[str, Any]]:
        return WORKFLOW_TEMPLATES

    def create_from_template(self, template_id: str, project_id: int) -> dict[str, Any]:
        template = next((item for item in WORKFLOW_TEMPLATES if item["id"] == template_id), None)
        if template is None:
            raise NotFoundError("workflow template not found")
        return self.create_workflow(project_id, template["name"], template["steps"])

    def start_run(
        self,
        workflow_id: int,
        payload: Any,
        parent_run_id: int | None = None,
        queued: bool = False,
        max_steps: int = 100,
        actor_role: str = "admin",
    ) -> dict[str, Any]:
        workflow = self.get_workflow(workflow_id)
        if parent_run_id is not None:
            parent = self.get_run(parent_run_id, redact_sensitive=False)
            parent_workflow = self.get_workflow(parent["workflow_id"])
            if parent_workflow["project_id"] != workflow["project_id"]:
                raise ConflictError("parent and child runs must belong to the same project")
        with self.db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO runs(
                       workflow_id, parent_run_id, status, input, max_steps, actor_role, started_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    workflow_id,
                    parent_run_id,
                    "queued" if queued else "running",
                    encode(payload),
                    max_steps,
                    actor_role,
                    now(),
                ),
            )
            run_id = int(cursor.lastrowid)
        if queued:
            self.run_queue.submit(run_id)
        else:
            self._execute(run_id)
        return self.get_run(run_id)

    def _execute(self, run_id: int) -> None:
        run = self.get_run(run_id, redact_sensitive=False)
        if run["status"] == "cancelled":
            return
        with self.db.connect() as connection:
            connection.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
        workflow = self.get_workflow(run["workflow_id"])
        value = run["input"] if run["current_step"] == 0 else run["output"]
        for index in range(run["current_step"], len(workflow["steps"])):
            if self.get_run(run_id, redact_sensitive=False)["status"] == "cancelled":
                return
            step = workflow["steps"][index]
            if index >= run["max_steps"]:
                with self.db.connect() as connection:
                    connection.execute(
                        """UPDATE runs
                           SET status='failed',error='step budget exceeded',finished_at=?
                           WHERE id=?""",
                        (now(), run_id),
                    )
                self._agent_event(run_id, "budget.exceeded", {"max_steps": run["max_steps"]})
                return
            started = time.perf_counter()
            config = step.get("config", {})
            allowed_roles = config.get("allowed_roles", ["admin", "operator"])
            if run["actor_role"] not in allowed_roles:
                with self.db.connect() as connection:
                    connection.execute(
                        """UPDATE runs
                           SET status='failed',error='tool permission denied',finished_at=?
                           WHERE id=?""",
                        (now(), run_id),
                    )
                self._agent_event(
                    run_id,
                    "permission.denied",
                    {"tool": step["tool"], "role": run["actor_role"]},
                )
                return
            retries = min(max(int(config.get("retries", 0)), 0), 5)
            retry_delay = min(max(float(config.get("retry_delay_seconds", 0)), 0), 5)
            error: Exception | None = None
            output: Any = None
            for attempt in range(retries + 1):
                try:
                    if step["tool"] == "approval":
                        self._pause_for_approval(run_id, index, step, value, started)
                        return
                    if step["tool"] in {"memory_read", "memory_write", "handoff"}:
                        output = self._apply_agent_tool(
                            run_id, workflow, step["tool"], config, value
                        )
                    else:
                        runtime_config = config
                        if step["tool"] == "llm":
                            self._agent_event(run_id, "llm.started", {"step_index": index})
                            runtime_config = {
                                **config,
                                "_on_chunk": lambda chunk, step_index=index: self._agent_event(
                                    run_id,
                                    "llm.chunk",
                                    {"step_index": step_index, "text": chunk},
                                ),
                            }
                        output = self._invoke_tool(step["tool"], runtime_config, value)
                        if step["tool"] == "llm":
                            self._agent_event(run_id, "llm.completed", {"step_index": index})
                    error = None
                    break
                except Exception as caught:
                    error = caught
                    if attempt < retries and retry_delay:
                        time.sleep(retry_delay)
            if self.get_run(run_id, redact_sensitive=False)["status"] == "cancelled":
                return
            if error is not None:
                duration = (time.perf_counter() - started) * 1000
                self._record_span(run_id, index, step, value, None, "failed", str(error), duration)
                with self.db.connect() as connection:
                    connection.execute(
                        """UPDATE runs
                           SET status='failed', error=?, current_step=?, finished_at=?
                           WHERE id=?""",
                        (str(error), index, now(), run_id),
                    )
                self._dispatch_webhooks(run_id, "run.failed")
                self._export_otel_if_configured(run_id)
                return
            duration = (time.perf_counter() - started) * 1000
            self._record_span(run_id, index, step, value, output, "completed", None, duration)
            value = output
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE runs SET output=?,current_step=? WHERE id=?",
                    (encode(value), index + 1, run_id),
                )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE runs SET status='completed',output=?,finished_at=? WHERE id=?",
                (encode(value), now(), run_id),
            )
        self._dispatch_webhooks(run_id, "run.completed")
        self._export_otel_if_configured(run_id)

    def _execute_queued(self, run_id: int) -> None:
        with self.db.connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status='running' WHERE id=? AND status='queued'", (run_id,)
            )
            if cursor.rowcount != 1:
                return
        self._execute(run_id)

    def cancel_run(self, run_id: int) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in {"completed", "failed", "rejected", "cancelled"}:
            raise ConflictError("terminal run cannot be cancelled")
        with self.db.connect() as connection:
            connection.execute(
                """UPDATE runs
                   SET status='cancelled',error='cancelled by operator',finished_at=?
                   WHERE id=?""",
                (now(), run_id),
            )
            connection.execute(
                """UPDATE approvals SET status='rejected', note='run cancelled', decided_at=?
                   WHERE run_id=? AND status='pending'""",
                (now(), run_id),
            )
        self._dispatch_webhooks(run_id, "run.cancelled")
        return self.get_run(run_id)

    def _pause_for_approval(
        self, run_id: int, index: int, step: dict[str, Any], value: Any, started: float
    ) -> None:
        prompt = str(step.get("config", {}).get("prompt", "Approve this workflow?"))
        expires_in = int(step.get("config", {}).get("expires_in_seconds", 0))
        expires_at = (
            (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
            if expires_in > 0
            else None
        )
        duration = (time.perf_counter() - started) * 1000
        self._record_span(run_id, index, step, value, value, "waiting", None, duration)
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO approvals(
                       run_id, step_index, prompt, status, created_at, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, index, prompt, "pending", now(), expires_at),
            )
            connection.execute(
                "UPDATE runs SET status='waiting_approval',output=?,current_step=? WHERE id=?",
                (encode(value), index, run_id),
            )
        self._dispatch_webhooks(run_id, "approval.pending")

    def _apply_tool(self, tool: str, config: dict[str, Any], value: Any) -> Any:
        if tool == "input":
            return value
        if tool == "template":
            template = str(config.get("template", "{value}"))
            rendered = value if isinstance(value, str) else encode(value)
            return template.replace("{value}", rendered)
        if tool == "uppercase":
            return str(value).upper()
        if tool == "lowercase":
            return str(value).lower()
        if tool == "json_extract":
            key = str(config.get("key", ""))
            if not isinstance(value, dict):
                raise ValueError("json_extract requires an object input")
            if key not in value:
                raise ValueError(f"key not found: {key}")
            return value[key]
        if tool == "llm":
            rendered = value if isinstance(value, str) else encode(value)
            prompt = str(config.get("prompt", "{value}")).replace("{value}", rendered)
            return self.providers.generate(
                str(config.get("provider", "ollama")),
                str(config.get("model", "")),
                prompt,
                str(config.get("system", "")),
                config.get("_on_chunk"),
            )
        if tool == "fail":
            raise RuntimeError(str(config.get("message", "intentional workflow failure")))
        raise ValueError(f"unknown tool: {tool}")

    def _invoke_tool(self, tool: str, config: dict[str, Any], value: Any) -> Any:
        timeout = min(max(float(config.get("timeout_seconds", 0)), 0), 300)
        if timeout == 0:
            return self._apply_tool(tool, config, value)
        future = self.tool_executor.submit(self._apply_tool, tool, config, value)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as error:
            future.cancel()
            raise TimeoutError(f"step timed out after {timeout:g} seconds") from error

    def _apply_agent_tool(
        self,
        run_id: int,
        workflow: dict[str, Any],
        tool: str,
        config: dict[str, Any],
        value: Any,
    ) -> Any:
        namespace = str(config.get("namespace", "default"))
        key = str(config.get("key", "value"))
        if tool == "memory_write":
            stored = config.get("value", value)
            self.put_memory(workflow["project_id"], namespace, key, stored)
            self._agent_event(run_id, "memory.written", {"namespace": namespace, "key": key})
            return value
        if tool == "memory_read":
            memory = self.get_memory(workflow["project_id"], namespace, key)
            self._agent_event(run_id, "memory.read", {"namespace": namespace, "key": key})
            return memory["value"]
        target_workflow_id = int(config.get("workflow_id", 0))
        target = self.get_workflow(target_workflow_id)
        if target["project_id"] != workflow["project_id"]:
            raise ConflictError("handoff target must belong to the same project")
        ancestry = self._workflow_ancestry(run_id)
        if target_workflow_id in ancestry:
            self._agent_event(run_id, "loop.detected", {"target_workflow_id": target_workflow_id})
            raise RuntimeError("agent handoff loop detected")
        self._agent_event(run_id, "handoff.started", {"target_workflow_id": target_workflow_id})
        child = self.start_run(
            target_workflow_id,
            value,
            parent_run_id=run_id,
            max_steps=int(config.get("max_steps", 100)),
            actor_role=self.get_run(run_id, redact_sensitive=False)["actor_role"],
        )
        self._agent_event(
            run_id,
            "handoff.completed",
            {"child_run_id": child["id"], "status": child["status"]},
        )
        if child["status"] != "completed":
            raise RuntimeError(f"handoff child run {child['id']} ended as {child['status']}")
        return child["output"]

    def _workflow_ancestry(self, run_id: int) -> set[int]:
        workflows: set[int] = set()
        current_id: int | None = run_id
        while current_id is not None:
            run = self.get_run(current_id, redact_sensitive=False)
            workflows.add(run["workflow_id"])
            current_id = run["parent_run_id"]
        return workflows

    def _agent_event(self, run_id: int, event_type: str, payload: Any) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO agent_events(run_id,event_type,payload,created_at) VALUES(?,?,?,?)",
                (run_id, event_type, encode(payload), now()),
            )

    def put_memory(self, project_id: int, namespace: str, key: str, value: Any) -> dict[str, Any]:
        self.get_project(project_id)
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO memories(project_id,namespace,key,value,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(project_id,namespace,key) DO UPDATE SET
                   value=excluded.value,updated_at=excluded.updated_at""",
                (project_id, namespace, key, encode(value), now()),
            )
        return self.get_memory(project_id, namespace, key)

    def get_memory(self, project_id: int, namespace: str, key: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                """SELECT * FROM memories
                   WHERE project_id=? AND namespace=? AND key=?""",
                (project_id, namespace, key),
            ).fetchone()
        if row is None:
            raise NotFoundError("memory not found")
        result = dict(row)
        result["value"] = redact(decode(result["value"]))
        return result

    def list_memories(self, project_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT namespace,key FROM memories
                   WHERE project_id=? ORDER BY namespace,key""",
                (project_id,),
            ).fetchall()
        return [self.get_memory(project_id, row["namespace"], row["key"]) for row in rows]

    def agent_tree(self, run_id: int) -> dict[str, Any]:
        run = self.get_run(run_id)
        with self.db.connect() as connection:
            events = connection.execute(
                "SELECT * FROM agent_events WHERE run_id=? ORDER BY created_at", (run_id,)
            ).fetchall()
            children = connection.execute(
                "SELECT id FROM runs WHERE parent_run_id=? ORDER BY started_at", (run_id,)
            ).fetchall()
        decoded_events = []
        for event in events:
            item = dict(event)
            item["payload"] = redact(decode(item["payload"]))
            decoded_events.append(item)
        return {
            "run": run,
            "events": decoded_events,
            "children": [self.agent_tree(child["id"]) for child in children],
        }

    def list_agent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        events = []
        for row in reversed(rows):
            event = dict(row)
            event["payload"] = redact(decode(event["payload"]))
            events.append(event)
        return events

    def export_project(self, project_id: int) -> dict[str, Any]:
        project = self.get_project(project_id)
        workflows = self.list_workflows(project_id)
        datasets = self.list_datasets(project_id)
        return {
            "format": "agentops-project",
            "version": 1,
            "project": {"name": project["name"], "description": project["description"]},
            "workflows": [
                {
                    "name": workflow["name"],
                    "version": workflow["version"],
                    "steps": workflow["steps"],
                }
                for workflow in reversed(workflows)
            ],
            "datasets": [
                {"name": dataset["name"], "cases": dataset["cases"]} for dataset in datasets
            ],
        }

    def import_project(self, package: dict[str, Any], name: str | None = None) -> dict[str, Any]:
        if package.get("format") != "agentops-project" or package.get("version") != 1:
            raise ConflictError("unsupported AgentOps project package")
        source_project = package.get("project", {})
        project = self.create_project(
            name or str(source_project.get("name", "Imported project")),
            str(source_project.get("description", "")),
        )
        workflow_ids = []
        for workflow in package.get("workflows", []):
            created = self.create_workflow(
                project["id"], str(workflow["name"]), list(workflow["steps"])
            )
            workflow_ids.append(created["id"])
        dataset_ids = []
        for dataset in package.get("datasets", []):
            created = self.create_dataset(
                project["id"], str(dataset["name"]), list(dataset["cases"])
            )
            dataset_ids.append(created["id"])
        return {
            "project": project,
            "workflow_ids": workflow_ids,
            "dataset_ids": dataset_ids,
        }

    def _record_span(
        self,
        run_id: int,
        index: int,
        step: dict[str, Any],
        input_value: Any,
        output: Any,
        status: str,
        error: str | None,
        duration: float,
    ) -> None:
        config = step.get("config", {})
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        if step["tool"] == "llm":
            input_tokens = max(1, len(encode(input_value)) // 4)
            output_tokens = max(1, len(encode(output)) // 4) if output is not None else 0
            cost_usd = (
                input_tokens * float(config.get("input_cost_per_1k", 0))
                + output_tokens * float(config.get("output_cost_per_1k", 0))
            ) / 1000
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO spans(
                       run_id, step_index, step_name, tool, status,
                       input, output, error, duration_ms, input_tokens,
                       output_tokens, cost_usd, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    index,
                    step["name"],
                    step["tool"],
                    status,
                    encode(input_value),
                    encode(output) if output is not None else None,
                    error,
                    duration,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    now(),
                ),
            )

    def get_run(self, run_id: int, redact_sensitive: bool = True) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            spans = connection.execute(
                "SELECT * FROM spans WHERE run_id=? ORDER BY step_index", (run_id,)
            ).fetchall()
        if row is None:
            raise NotFoundError("run not found")
        result = dict(row)
        result["input"] = decode(result["input"])
        result["output"] = decode(result["output"])
        result["spans"] = []
        for span in spans:
            item = dict(span)
            item["input"] = decode(item["input"])
            item["output"] = decode(item["output"])
            result["spans"].append(item)
        if redact_sensitive:
            result["input"] = redact(result["input"])
            result["output"] = redact(result["output"])
            for item in result["spans"]:
                item["input"] = redact(item["input"])
                item["output"] = redact(item["output"])
        return result

    def list_runs(self, workflow_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM runs"
        params: list[Any] = []
        if workflow_id is not None:
            query += " WHERE workflow_id=?"
            params.append(workflow_id)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["input"] = decode(item["input"])
            item["output"] = decode(item["output"])
            results.append(item)
        return results

    def decide_approval(
        self,
        approval_id: int,
        decision: str,
        note: str,
        output: Any = None,
        output_supplied: bool = False,
    ) -> dict[str, Any]:
        rejected_run_id: int | None = None
        with self.db.connect() as connection:
            approval = connection.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if approval is None:
                raise NotFoundError("approval not found")
            if approval["status"] != "pending":
                raise ConflictError("approval has already been decided")
            connection.execute(
                "UPDATE approvals SET status=?,note=?,decided_at=? WHERE id=?",
                (decision, note, now(), approval_id),
            )
            if decision == "rejected":
                connection.execute(
                    "UPDATE runs SET status='rejected',error=?,finished_at=? WHERE id=?",
                    (note or "approval rejected", now(), approval["run_id"]),
                )
                rejected_run_id = approval["run_id"]
            else:
                if output_supplied:
                    connection.execute(
                        "UPDATE runs SET output=? WHERE id=?",
                        (encode(output), approval["run_id"]),
                    )
                    connection.execute(
                        "UPDATE spans SET output=? WHERE run_id=? AND step_index=?",
                        (encode(output), approval["run_id"], approval["step_index"]),
                    )
                connection.execute(
                    "UPDATE spans SET status='completed' WHERE run_id=? AND step_index=?",
                    (approval["run_id"], approval["step_index"]),
                )
                connection.execute(
                    "UPDATE runs SET status='running',current_step=? WHERE id=?",
                    (approval["step_index"] + 1, approval["run_id"]),
                )
        if rejected_run_id is not None:
            self._dispatch_webhooks(rejected_run_id, "run.rejected")
            return self.get_run(rejected_run_id)
        self._execute(approval["run_id"])
        return self.get_run(approval["run_id"])

    def escalate_approval(self, approval_id: int, note: str = "") -> dict[str, Any]:
        with self.db.connect() as connection:
            approval = connection.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if approval is None:
                raise NotFoundError("approval not found")
            if approval["status"] != "pending":
                raise ConflictError("only pending approvals can be escalated")
            connection.execute(
                """UPDATE approvals
                   SET escalation_level=escalation_level+1,note=? WHERE id=?""",
                (note, approval_id),
            )
        self._dispatch_webhooks(approval["run_id"], "approval.escalated")
        return self.list_approvals_by_id(approval_id)

    def list_approvals_by_id(self, approval_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("approval not found")
        return dict(row)

    def expire_approvals(self, at: str | None = None) -> int:
        cutoff = at or now()
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM approvals
                   WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<=?""",
                (cutoff,),
            ).fetchall()
            for approval in rows:
                connection.execute(
                    """UPDATE approvals SET status='expired',note='approval expired',decided_at=?
                       WHERE id=?""",
                    (cutoff, approval["id"]),
                )
                connection.execute(
                    """UPDATE runs SET status='rejected',error='approval expired',finished_at=?
                       WHERE id=?""",
                    (cutoff, approval["run_id"]),
                )
        for approval in rows:
            self._dispatch_webhooks(approval["run_id"], "approval.expired")
        return len(rows)

    def list_approvals(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM approvals"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def replay(self, run_id: int) -> dict[str, Any]:
        run = self.get_run(run_id, redact_sensitive=False)
        return self.start_run(run["workflow_id"], run["input"], run_id)

    def create_dataset(
        self, project_id: int, name: str, cases: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.get_project(project_id)
        try:
            with self.db.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO datasets(project_id,name,cases,created_at) VALUES(?,?,?,?)",
                    (project_id, name, encode(cases), now()),
                )
                dataset_id = int(cursor.lastrowid)
        except (sqlite3.IntegrityError, psycopg.IntegrityError) as error:
            raise ConflictError("dataset name already exists in this project") from error
        return self.get_dataset(dataset_id)

    def get_dataset(self, dataset_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if row is None:
            raise NotFoundError("dataset not found")
        result = dict(row)
        result["cases"] = decode(result["cases"])
        return result

    def list_datasets(self, project_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM datasets"
        params: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE project_id=?"
            params = (project_id,)
        query += " ORDER BY created_at DESC"
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["cases"] = decode(item["cases"])
            results.append(item)
        return results

    def evaluate(self, workflow_id: int, dataset_id: int) -> dict[str, Any]:
        workflow = self.get_workflow(workflow_id)
        dataset = self.get_dataset(dataset_id)
        if workflow["project_id"] != dataset["project_id"]:
            raise ConflictError("workflow and dataset must belong to the same project")
        results = []
        for case in dataset["cases"]:
            created_run = self.start_run(workflow_id, case["input"])
            run = self.get_run(created_run["id"], redact_sensitive=False)
            actual = run["output"]
            expected = case["expected"]
            passed = run["status"] == "completed" and self._match_evaluation(
                case["matcher"], actual, expected
            )
            results.append(
                {
                    "run_id": run["id"],
                    "passed": passed,
                    "matcher": case["matcher"],
                    "actual": actual,
                    "expected": expected,
                }
            )
        passed_count = sum(result["passed"] for result in results)
        with self.db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO evaluations(
                       workflow_id, dataset_id, passed, total, results, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (workflow_id, dataset_id, passed_count, len(results), encode(results), now()),
            )
            evaluation_id = int(cursor.lastrowid)
        return {
            "id": evaluation_id,
            "workflow_id": workflow_id,
            "dataset_id": dataset_id,
            "passed": passed_count,
            "total": len(results),
            "pass_rate": passed_count / len(results),
            "results": results,
        }

    def _match_evaluation(self, matcher: str, actual: Any, expected: Any) -> bool:
        if matcher == "exact":
            return actual == expected
        if matcher == "contains":
            return str(expected) in str(actual)
        if matcher == "regex":
            try:
                return re.search(str(expected), str(actual)) is not None
            except re.error:
                return False
        if matcher == "json_schema":
            return self._matches_schema(actual, expected)
        if matcher == "llm_judge":
            verdict = self.providers.generate(
                "ollama",
                "",
                f"""Decide whether ACTUAL satisfies CRITERIA. Reply only PASS or FAIL.
CRITERIA: {encode(expected)}
ACTUAL: {encode(actual)}""",
            )
            return verdict.strip().upper().startswith("PASS")
        raise ValueError(f"unknown matcher: {matcher}")

    @classmethod
    def _matches_schema(cls, value: Any, schema: Any) -> bool:
        if not isinstance(schema, dict):
            return False
        expected_type = schema.get("type")
        types = {
            "object": dict,
            "array": list,
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "null": type(None),
        }
        if expected_type in types:
            if expected_type in {"number", "integer"} and isinstance(value, bool):
                return False
            if not isinstance(value, types[expected_type]):
                return False
        if isinstance(value, dict):
            if any(key not in value for key in schema.get("required", [])):
                return False
            for key, child_schema in schema.get("properties", {}).items():
                if key in value and not cls._matches_schema(value[key], child_schema):
                    return False
        if isinstance(value, list) and "items" in schema:
            return all(cls._matches_schema(item, schema["items"]) for item in value)
        return True

    def get_evaluation(self, evaluation_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("evaluation not found")
        result = dict(row)
        result["results"] = decode(result["results"])
        result["pass_rate"] = result["passed"] / result["total"] if result["total"] else 0
        return result

    def compare_evaluations(self, left_id: int, right_id: int) -> dict[str, Any]:
        left = self.get_evaluation(left_id)
        right = self.get_evaluation(right_id)
        left_cases = left["results"]
        right_cases = right["results"]
        regressions = []
        improvements = []
        for index in range(min(len(left_cases), len(right_cases))):
            if left_cases[index]["passed"] and not right_cases[index]["passed"]:
                regressions.append(index)
            if not left_cases[index]["passed"] and right_cases[index]["passed"]:
                improvements.append(index)
        return {
            "left": left,
            "right": right,
            "pass_rate_delta": right["pass_rate"] - left["pass_rate"],
            "regression_case_indexes": regressions,
            "improvement_case_indexes": improvements,
        }

    def list_evaluations(
        self, workflow_id: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM evaluations"
        params: list[Any] = []
        if workflow_id is not None:
            query += " WHERE workflow_id=?"
            params.append(workflow_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        evaluations = []
        for row in rows:
            item = dict(row)
            item["results"] = decode(item["results"])
            item["pass_rate"] = item["passed"] / item["total"] if item["total"] else 0
            evaluations.append(item)
        return evaluations

    def compare_runs(self, left_id: int, right_id: int) -> dict[str, Any]:
        left = self.get_run(left_id)
        right = self.get_run(right_id)
        return {
            "left": left,
            "right": right,
            "same_workflow": left["workflow_id"] == right["workflow_id"],
            "status_changed": left["status"] != right["status"],
            "output_changed": left["output"] != right["output"],
            "duration_delta_ms": round(
                sum(span["duration_ms"] for span in right["spans"])
                - sum(span["duration_ms"] for span in left["spans"]),
                3,
            ),
        }

    def create_schedule(
        self, workflow_id: int, name: str, payload: Any, interval_seconds: int
    ) -> dict[str, Any]:
        self.get_workflow(workflow_id)
        created_at = now()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO schedules(
                       workflow_id, name, input, interval_seconds, next_run_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    workflow_id,
                    name.strip(),
                    encode(payload),
                    interval_seconds,
                    created_at,
                    created_at,
                ),
            )
            schedule_id = int(cursor.lastrowid)
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE id=?", (schedule_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("schedule not found")
        result = dict(row)
        result["input"] = decode(result["input"])
        result["enabled"] = bool(result["enabled"])
        return result

    def list_schedules(self) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM schedules ORDER BY created_at DESC"
            ).fetchall()
        return [self.get_schedule(row["id"]) for row in rows]

    def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> dict[str, Any]:
        self.get_schedule(schedule_id)
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE schedules SET enabled=? WHERE id=?", (int(enabled), schedule_id)
            )
        return self.get_schedule(schedule_id)

    def run_due_schedules(self, at: str | None = None) -> list[dict[str, Any]]:
        due_at = at or now()
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM schedules
                   WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at""",
                (due_at,),
            ).fetchall()
        runs = []
        for row in rows:
            run = self.start_run(row["workflow_id"], decode(row["input"]), queued=True)
            next_at = datetime.fromisoformat(due_at).timestamp() + row["interval_seconds"]
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE schedules SET last_run_id=?,next_run_at=? WHERE id=?",
                    (run["id"], datetime.fromtimestamp(next_at, UTC).isoformat(), row["id"]),
                )
            runs.append(run)
        return runs

    def create_webhook(self, project_id: int, url: str, events: list[str]) -> dict[str, Any]:
        self.get_project(project_id)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConflictError("webhook URL must use HTTP or HTTPS")
        with self.db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO webhooks(project_id,url,events,created_at)
                   VALUES(?,?,?,?)""",
                (project_id, url, encode(events), now()),
            )
            webhook_id = int(cursor.lastrowid)
        return self.get_webhook(webhook_id)

    def get_webhook(self, webhook_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM webhooks WHERE id=?", (webhook_id,)).fetchone()
        if row is None:
            raise NotFoundError("webhook not found")
        result = dict(row)
        result["events"] = decode(result["events"])
        result["enabled"] = bool(result["enabled"])
        return result

    def list_webhooks(self, project_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT id FROM webhooks"
        params: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE project_id=?"
            params = (project_id,)
        query += " ORDER BY created_at DESC"
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self.get_webhook(row["id"]) for row in rows]

    def _dispatch_webhooks(self, run_id: int, event: str) -> None:
        run = self.get_run(run_id)
        workflow = self.get_workflow(run["workflow_id"])
        for webhook in self.list_webhooks(workflow["project_id"]):
            if not webhook["enabled"] or event not in webhook["events"]:
                continue
            error = None
            status = "delivered"
            try:
                self._send_webhook(webhook["url"], {"event": event, "run": run})
            except Exception as caught:
                status = "failed"
                error = str(caught)
            with self.db.connect() as connection:
                connection.execute(
                    """INSERT INTO webhook_deliveries(
                           webhook_id,run_id,event,status,error,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (webhook["id"], run_id, event, status, error, now()),
                )
        with suppress(RuntimeError):
            self.notifier.notify(event, {"run": run})

    @staticmethod
    def _send_webhook(url: str, payload: dict[str, Any]) -> None:
        request = urllib.request.Request(  # noqa: S310 -- validated when webhook is created.
            url,
            data=encode(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10):  # noqa: S310
                pass
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(f"webhook delivery failed: {error}") from error

    def create_user(self, name: str, token: str, role: str) -> dict[str, Any]:
        try:
            with self.db.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO users(name,token_hash,role,created_at) VALUES(?,?,?,?)",
                    (name.strip(), token_hash(token), role, now()),
                )
                user_id = int(cursor.lastrowid)
        except (sqlite3.IntegrityError, psycopg.IntegrityError) as error:
            raise ConflictError("user name or token already exists") from error
        return {"id": user_id, "name": name.strip(), "role": role}

    def list_users(self) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id,name,role,created_at FROM users ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def put_project_member(self, project_id: int, user_name: str, role: str) -> dict[str, Any]:
        self.get_project(project_id)
        with self.db.connect() as connection:
            if (
                connection.execute("SELECT 1 FROM users WHERE name=?", (user_name,)).fetchone()
                is None
            ):
                raise NotFoundError("user not found")
            existing = connection.execute(
                "SELECT id FROM project_members WHERE project_id=? AND user_name=?",
                (project_id, user_name),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE project_members SET role=? WHERE id=?", (role, existing["id"])
                )
                member_id = existing["id"]
            else:
                member_id = connection.execute(
                    "INSERT INTO project_members(project_id,user_name,role,created_at) "
                    "VALUES (?,?,?,?)",
                    (project_id, user_name, role, now()),
                ).lastrowid
            row = connection.execute(
                "SELECT * FROM project_members WHERE id=?", (member_id,)
            ).fetchone()
        return dict(row)

    def list_project_members(self, project_id: int) -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM project_members WHERE project_id=? ORDER BY user_name",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def project_role(self, project_id: int, user_name: str) -> str | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT role FROM project_members WHERE project_id=? AND user_name=?",
                (project_id, user_name),
            ).fetchone()
        return str(row["role"]) if row else None

    def put_secret(self, project_id: int, name: str, value: str) -> dict[str, Any]:
        self.get_project(project_id)
        ciphertext = self.vault.encrypt(value)
        timestamp = now()
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO secrets(project_id,name,ciphertext,created_at,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(project_id,name) DO UPDATE SET
                     ciphertext=excluded.ciphertext,updated_at=excluded.updated_at""",
                (project_id, name, ciphertext, timestamp, timestamp),
            )
            row = connection.execute(
                """SELECT id,project_id,name,created_at,updated_at FROM secrets
                   WHERE project_id=? AND name=?""",
                (project_id, name),
            ).fetchone()
        return dict(row)

    def list_secrets(self, project_id: int) -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT id,project_id,name,created_at,updated_at FROM secrets
                   WHERE project_id=? ORDER BY name""",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reveal_secret(self, secret_id: int) -> str:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT ciphertext FROM secrets WHERE id=?", (secret_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("secret not found")
        return self.vault.decrypt(row["ciphertext"])

    def audit(self, actor: str, action: str, resource: str, status_code: int) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO audit_events(actor,action,resource,status_code,created_at)
                   VALUES(?,?,?,?,?)""",
                (actor, action, resource, status_code, now()),
            )

    def list_audit_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def create_alert(self, name: str, metric: str, threshold: float) -> dict[str, Any]:
        with self.db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO alerts(name,metric,threshold,created_at) VALUES(?,?,?,?)",
                (name, metric, threshold, now()),
            )
            alert_id = int(cursor.lastrowid)
        return self.get_alert(alert_id)

    def get_alert(self, alert_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if row is None:
            raise NotFoundError("alert not found")
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def list_alerts(self) -> list[dict[str, Any]]:
        stats = self.stats()
        total = stats["total_runs"]
        values = {
            "failure_rate": stats["failed_runs"] / total if total else 0,
            "pending_approvals": stats["pending_approvals"],
            "total_cost_usd": stats["total_cost_usd"],
        }
        with self.db.connect() as connection:
            rows = connection.execute("SELECT id FROM alerts ORDER BY created_at DESC").fetchall()
        alerts = []
        for row in rows:
            alert = self.get_alert(row["id"])
            alert["current_value"] = values[alert["metric"]]
            alert["triggered"] = alert["enabled"] and alert["current_value"] >= alert["threshold"]
            alerts.append(alert)
        return alerts

    def otel_trace(self, run_id: int) -> dict[str, Any]:
        run = self.get_run(run_id)
        workflow = self.get_workflow(run["workflow_id"])
        spans = []
        for span in run["spans"]:
            spans.append(
                {
                    "name": span["step_name"],
                    "traceId": f"{run_id:032x}",
                    "spanId": f"{span['id']:016x}",
                    "attributes": [
                        {"key": "agentops.tool", "value": {"stringValue": span["tool"]}},
                        {"key": "agentops.status", "value": {"stringValue": span["status"]}},
                        {"key": "agentops.cost_usd", "value": {"doubleValue": span["cost_usd"]}},
                    ],
                    "status": {"code": "STATUS_CODE_ERROR" if span["error"] else "STATUS_CODE_OK"},
                }
            )
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "agentops-control-plane"},
                            },
                            {
                                "key": "agentops.workflow",
                                "value": {"stringValue": workflow["name"]},
                            },
                        ]
                    },
                    "scopeSpans": [{"scope": {"name": "agentops"}, "spans": spans}],
                }
            ]
        }

    def _export_otel_if_configured(self, run_id: int) -> None:
        endpoint = os.getenv("AGENTOPS_OTLP_ENDPOINT")
        if not endpoint:
            return
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return
        request = urllib.request.Request(  # noqa: S310 -- endpoint scheme validated above.
            endpoint,
            data=encode(self.otel_trace(run_id)).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10):  # noqa: S310
                pass
        except (urllib.error.URLError, TimeoutError):
            return

    def stats(self) -> dict[str, Any]:
        with self.db.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            completed = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE status='completed'"
            ).fetchone()[0]
            failed = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE status='failed'"
            ).fetchone()[0]
            pending = connection.execute(
                "SELECT COUNT(*) FROM approvals WHERE status='pending'"
            ).fetchone()[0]
            avg_ms = connection.execute(
                "SELECT COALESCE(AVG(duration_ms),0) FROM spans"
            ).fetchone()[0]
            usage = connection.execute(
                """SELECT COALESCE(SUM(input_tokens),0),
                          COALESCE(SUM(output_tokens),0),COALESCE(SUM(cost_usd),0)
                   FROM spans"""
            ).fetchone()
        return {
            "total_runs": total,
            "completed_runs": completed,
            "failed_runs": failed,
            "success_rate": completed / total if total else 0,
            "pending_approvals": pending,
            "average_step_duration_ms": round(avg_ms, 3),
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            "total_cost_usd": round(usage[2], 6),
        }

    def trends(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT r.id,r.status,r.started_at,
                          COALESCE(SUM(s.duration_ms),0) AS duration_ms,
                          COALESCE(SUM(s.cost_usd),0) AS cost_usd
                   FROM runs r LEFT JOIN spans s ON s.run_id=r.id
                   GROUP BY r.id,r.status,r.started_at
                   ORDER BY r.started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]
