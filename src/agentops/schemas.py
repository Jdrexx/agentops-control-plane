from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

ShortName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
MemoryKey = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class ProjectCreate(BaseModel):
    name: ShortName
    description: str = Field(default="", max_length=1000)


class Step(BaseModel):
    name: ShortName
    tool: Literal[
        "input",
        "template",
        "uppercase",
        "lowercase",
        "json_extract",
        "llm",
        "memory_read",
        "memory_write",
        "handoff",
        "approval",
        "fail",
    ]
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_runtime_config(self) -> Self:
        def integer(name: str, minimum: int, maximum: int) -> None:
            if name not in self.config:
                return
            value = self.config[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"config.{name} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"config.{name} must be between {minimum} and {maximum}")

        def number(name: str, minimum: float, maximum: float | None = None) -> None:
            if name not in self.config:
                return
            value = self.config[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"config.{name} must be a number")
            if not math.isfinite(value):
                raise ValueError(f"config.{name} must be finite")
            if value < minimum:
                raise ValueError(f"config.{name} must be at least {minimum:g}")
            if maximum is not None and value > maximum:
                raise ValueError(f"config.{name} must be at most {maximum:g}")

        integer("retries", 0, 5)
        number("retry_delay_seconds", 0, 5)
        number("timeout_seconds", 0, 300)
        roles = self.config.get("allowed_roles")
        if roles is not None and (
            not isinstance(roles, list)
            or not roles
            or any(
                not isinstance(role, str) or role not in {"admin", "operator", "viewer"}
                for role in roles
            )
        ):
            raise ValueError("config.allowed_roles must be a non-empty list of valid roles")
        if self.tool == "approval":
            integer("expires_in_seconds", 0, 31_536_000)
        if self.tool == "handoff":
            if "workflow_id" not in self.config:
                raise ValueError("config.workflow_id is required for handoff steps")
            integer("workflow_id", 1, 2_147_483_647)
            integer("max_steps", 1, 1000)
        if self.tool == "llm":
            number("input_cost_per_1k", 0)
            number("output_cost_per_1k", 0)
            credential_ref = self.config.get("credential_ref")
            if credential_ref is not None and (
                not isinstance(credential_ref, str)
                or not credential_ref.strip()
                or len(credential_ref) > 200
            ):
                raise ValueError(
                    "config.credential_ref must be a non-empty string up to 200 characters"
                )
        return self


class WorkflowCreate(BaseModel):
    project_id: int = Field(gt=0)
    name: ShortName
    steps: list[Step] = Field(min_length=1, max_length=100)


class RunCreate(BaseModel):
    input: Any
    execution: Literal["sync", "queued"] = "sync"
    parent_run_id: int | None = Field(default=None, gt=0)
    max_steps: int = Field(default=100, ge=1, le=1000)


class ScheduleCreate(BaseModel):
    workflow_id: int = Field(gt=0)
    name: ShortName
    input: Any
    interval_seconds: int = Field(ge=10, le=31_536_000)


class WebhookCreate(BaseModel):
    project_id: int = Field(gt=0)
    url: str = Field(min_length=8, max_length=2000)
    events: list[
        Literal[
            "run.completed",
            "run.failed",
            "run.rejected",
            "run.cancelled",
            "approval.pending",
            "approval.escalated",
            "approval.expired",
        ]
    ] = Field(min_length=1, max_length=7)


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=2000)
    output: Any = None


class DatasetCase(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=100)
    input: Any
    expected: Any
    matcher: Literal["exact", "contains", "regex", "json_schema", "llm_judge"] = "exact"
    judge_provider: Literal["mock", "ollama", "openai", "anthropic"] = "mock"
    judge_model: str = Field(default="", max_length=200)
    rubric: str = Field(default="", max_length=2000)


class DatasetCreate(BaseModel):
    project_id: int = Field(gt=0)
    name: ShortName
    cases: list[DatasetCase] = Field(min_length=1, max_length=500)


class EvaluationCreate(BaseModel):
    workflow_id: int = Field(gt=0)
    dataset_id: int = Field(gt=0)
    execution: Literal["sync", "queued"] = "sync"
    pass_rate_min: float | None = Field(default=None, ge=0, le=1)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_p95_latency_ms: float | None = Field(default=None, ge=0)


class UserCreate(BaseModel):
    name: ShortName
    token: str = Field(min_length=24, max_length=500)
    role: Literal["admin", "operator", "viewer"]


class ProjectMemberCreate(BaseModel):
    project_id: int = Field(gt=0)
    user_name: ShortName
    role: Literal["operator", "viewer"]


class SecretCreate(BaseModel):
    project_id: int = Field(gt=0)
    name: ShortName
    value: str = Field(min_length=1, max_length=100_000)


class AlertCreate(BaseModel):
    name: ShortName
    metric: Literal["failure_rate", "pending_approvals", "total_cost_usd"]
    threshold: float = Field(ge=0)


class MemoryCreate(BaseModel):
    project_id: int = Field(gt=0)
    namespace: ShortName = "default"
    key: MemoryKey
    value: Any


class DemoSeedRequest(BaseModel):
    scenario: Literal["tour", "quality", "incident"]
    reset: bool = False


class ProjectPackageProject(BaseModel):
    name: ShortName
    description: str = Field(default="", max_length=1000)


class ProjectPackageWorkflow(BaseModel):
    source_id: int | None = Field(default=None, gt=0)
    name: ShortName
    version: int = Field(ge=1)
    steps: list[Step] = Field(min_length=1, max_length=100)


class ProjectPackageDataset(BaseModel):
    name: ShortName
    cases: list[DatasetCase] = Field(min_length=1, max_length=500)


class ProjectPackage(BaseModel):
    format: Literal["agentops-project"]
    version: Literal[1]
    project: ProjectPackageProject
    workflows: list[ProjectPackageWorkflow] = Field(default_factory=list, max_length=1000)
    datasets: list[ProjectPackageDataset] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_references_and_names(self) -> Self:
        dataset_names = [dataset.name for dataset in self.datasets]
        if len(dataset_names) != len(set(dataset_names)):
            raise ValueError("dataset names must be unique within an imported project")
        source_ids = [
            workflow.source_id for workflow in self.workflows if workflow.source_id is not None
        ]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("workflow source IDs must be unique")
        handoff_targets = {
            step.config["workflow_id"]
            for workflow in self.workflows
            for step in workflow.steps
            if step.tool == "handoff"
        }
        if handoff_targets and len(source_ids) != len(self.workflows):
            raise ValueError("workflow source IDs are required to import handoff steps")
        missing_targets = handoff_targets - set(source_ids)
        if missing_targets:
            raise ValueError("handoff targets must refer to workflows in the imported project")
        return self


class ProjectImport(BaseModel):
    package: ProjectPackage
    name: ShortName | None = None
