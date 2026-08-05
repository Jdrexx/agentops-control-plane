from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class Step(BaseModel):
    name: str = Field(min_length=1, max_length=100)
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


class WorkflowCreate(BaseModel):
    project_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    steps: list[Step] = Field(min_length=1, max_length=100)


class RunCreate(BaseModel):
    input: Any
    execution: Literal["sync", "queued"] = "sync"
    parent_run_id: int | None = Field(default=None, gt=0)
    max_steps: int = Field(default=100, ge=1, le=1000)


class ScheduleCreate(BaseModel):
    workflow_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
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
    input: Any
    expected: Any
    matcher: Literal["exact", "contains", "regex", "json_schema", "llm_judge"] = "exact"


class DatasetCreate(BaseModel):
    project_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    cases: list[DatasetCase] = Field(min_length=1, max_length=500)


class EvaluationCreate(BaseModel):
    workflow_id: int = Field(gt=0)
    dataset_id: int = Field(gt=0)


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    token: str = Field(min_length=24, max_length=500)
    role: Literal["admin", "operator", "viewer"]


class ProjectMemberCreate(BaseModel):
    project_id: int = Field(gt=0)
    user_name: str = Field(min_length=1, max_length=100)
    role: Literal["operator", "viewer"]


class SecretCreate(BaseModel):
    project_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=100_000)


class AlertCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    metric: Literal["failure_rate", "pending_approvals", "total_cost_usd"]
    threshold: float = Field(ge=0)


class MemoryCreate(BaseModel):
    project_id: int = Field(gt=0)
    namespace: str = Field(default="default", min_length=1, max_length=100)
    key: str = Field(min_length=1, max_length=200)
    value: Any


class ProjectImport(BaseModel):
    package: dict[str, Any]
    name: str | None = Field(default=None, max_length=100)
