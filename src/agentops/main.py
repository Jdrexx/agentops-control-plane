from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .database import Database
from .demo import seed as seed_demo
from .schemas import (
    AlertCreate,
    ApprovalDecision,
    DatasetCreate,
    DemoSeedRequest,
    EvaluationCreate,
    MemoryCreate,
    ProjectCreate,
    ProjectImport,
    ProjectMemberCreate,
    RunCreate,
    ScheduleCreate,
    SecretCreate,
    UserCreate,
    WebhookCreate,
    WorkflowCreate,
)
from .security import Actor, Authenticator
from .service import AgentOpsService, ConflictError, NotFoundError

STATIC_DIR = Path(__file__).parent / "static"


def create_app(database_path: str | None = None) -> FastAPI:
    path = (
        database_path
        or os.getenv("AGENTOPS_DATABASE")
        or os.getenv("DATABASE_URL")
        or "data/agentops.db"
    )
    database = Database(path)
    authenticator = Authenticator(database)
    rate_buckets: dict[str, deque[float]] = defaultdict(deque)
    rate_lock = Lock()
    process_mode = os.getenv("AGENTOPS_PROCESS_MODE", "all").lower()
    if process_mode not in {"all", "web", "worker", "scheduler"}:
        raise RuntimeError("AGENTOPS_PROCESS_MODE must be all, web, worker, or scheduler")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        app.state.service = AgentOpsService(
            database, consume_runs=process_mode in {"all", "worker"}
        )
        if process_mode in {"all", "worker"}:
            app.state.service.recover_incomplete_runs()
        if process_mode in {"all", "scheduler"}:
            app.state.service.start_scheduler()
        yield
        app.state.service.close()

    app = FastAPI(title="AgentOps Control Plane", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        public = request.url.path in {
            "/",
            "/api/health",
            "/api/ready",
            "/api/auth/status",
            "/robots.txt",
            "/.well-known/security.txt",
        } or request.url.path.startswith("/static/")
        actor = Actor("local-user", "admin")
        authenticated_request = False
        response = None
        if not public and authenticator.enabled():
            authorization = request.headers.get("Authorization", "")
            token = (
                authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
            )
            authenticated = authenticator.authenticate(token) if token else None
            if authenticated is None:
                response = JSONResponse({"detail": "valid bearer token required"}, status_code=401)
            else:
                actor = authenticated
                authenticated_request = True
        elif not public:
            authenticated_request = True
        if not public and response is None:
            request.state.actor = actor
            if request.method not in {"GET", "HEAD", "OPTIONS"} and actor.role == "viewer":
                response = JSONResponse({"detail": "viewer role is read-only"}, status_code=403)
            admin_path = request.url.path.startswith(
                (
                    "/api/users",
                    "/api/secrets",
                    "/api/audit",
                    "/api/project-members",
                    "/api/webhooks",
                    "/api/alerts",
                    "/api/approvals/expire",
                    "/api/schedules/run-due",
                )
            )
            if response is None and admin_path and actor.role != "admin":
                response = JSONResponse({"detail": "admin role required"}, status_code=403)
            if response is None:
                timestamp = time.monotonic()
                with rate_lock:
                    bucket = rate_buckets[actor.name]
                    while bucket and bucket[0] < timestamp - 60:
                        bucket.popleft()
                    if len(bucket) >= 240:
                        response = JSONResponse(
                            {"detail": "request rate limit exceeded"}, status_code=429
                        )
                    else:
                        bucket.append(timestamp)
        if response is None:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not public:
            response.headers["Cache-Control"] = "no-store"
        if (
            not public
            and authenticated_request
            and request.method not in {"GET", "HEAD", "OPTIONS"}
        ):
            service(request).audit(
                actor.name, request.method, request.url.path, response.status_code
            )
        return response

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, error: NotFoundError):
        return _error(404, str(error))

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, error: ConflictError):
        return _error(409, str(error))

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/.well-known/security.txt", include_in_schema=False)
    def security_txt():
        return PlainTextResponse(
            "Contact: https://github.com/Jdrexx/agentops-control-plane/security\n"
            "Expires: 2027-08-05T00:00:00Z\n"
        )

    @app.get("/api/ready")
    def readiness():
        if not database.ready():
            return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)
        queue = app.state.service.run_queue
        if not queue.ready():
            return JSONResponse(
                {"status": "unavailable", "database": "ok", "queue": "error"},
                status_code=503,
            )
        return {
            "status": "ready",
            "database": "ok",
            "queue": queue.backend,
            "process": process_mode,
        }

    @app.get("/api/auth/status")
    def auth_status() -> dict[str, bool]:
        return {"enabled": authenticator.enabled()}

    @app.get("/api/session")
    def session(request: Request) -> dict[str, str]:
        actor: Actor = request.state.actor
        return {"name": actor.name, "role": actor.role}

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate, request: Request):
        project = service(request).create_project(body.name, body.description)
        grant_created_project(request, project["id"])
        return project

    @app.get("/api/projects")
    def projects(request: Request):
        return service(request).list_projects(actor_project_ids(request))

    @app.get("/api/projects/{project_id}")
    def project(project_id: int, request: Request):
        require_project_access(request, project_id)
        return service(request).get_project(project_id)

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: int, request: Request):
        require_project_access(request, project_id, write=True)
        service(request).delete_project(project_id)
        return {"deleted": project_id}

    @app.post("/api/demo/seed", status_code=201)
    def demo_seed(body: DemoSeedRequest, request: Request):
        if os.getenv("AGENTOPS_DEMO_ENABLED", "1") != "1":
            raise HTTPException(404, "demo scenarios are disabled")
        try:
            result = seed_demo(service(request), body.scenario, reset=body.reset)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if body.scenario == "tour" and result.get("next_action") == "approve":
            pending = service(request).list_approvals("pending")
            result["approval_id"] = next(
                (item["id"] for item in pending if item["run_id"] == result["focus_run_id"]),
                None,
            )
        return result

    @app.get("/api/projects/{project_id}/export")
    def export_project(project_id: int, request: Request):
        require_project_access(request, project_id)
        return service(request).export_project(project_id)

    @app.post("/api/projects/import", status_code=201)
    def import_project(body: ProjectImport, request: Request):
        imported = service(request).import_project(body.package.model_dump(), body.name)
        grant_created_project(request, imported["project"]["id"])
        return imported

    @app.post("/api/workflows", status_code=201)
    def create_workflow(body: WorkflowCreate, request: Request):
        require_project_access(request, body.project_id, write=True)
        return service(request).create_workflow(
            body.project_id, body.name, [step.model_dump() for step in body.steps]
        )

    @app.get("/api/workflows")
    def workflows(request: Request, project_id: int | None = None):
        if project_id is not None:
            require_project_access(request, project_id)
        return service(request).list_workflows(project_id, actor_project_ids(request))

    @app.get("/api/workflows/{workflow_id}")
    def workflow(workflow_id: int, request: Request):
        require_workflow_access(request, workflow_id)
        return service(request).get_workflow(workflow_id)

    @app.get("/api/tools")
    def tools_catalog() -> list[dict[str, object]]:
        return [
            {"name": "input", "description": "Pass the current value through", "config": {}},
            {
                "name": "template",
                "description": "Render the current value into a template",
                "config": {"template": "Result: {value}"},
            },
            {"name": "uppercase", "description": "Convert text to uppercase", "config": {}},
            {"name": "lowercase", "description": "Convert text to lowercase", "config": {}},
            {
                "name": "json_extract",
                "description": "Extract a top-level object key",
                "config": {"key": "field"},
            },
            {
                "name": "approval",
                "description": "Pause for a human decision",
                "config": {"prompt": "Approve this action?"},
            },
            {
                "name": "llm",
                "description": "Generate text with Ollama, OpenAI, or Anthropic",
                "config": {
                    "provider": "ollama",
                    "model": "llama3.2",
                    "system": "",
                    "prompt": "{value}",
                },
            },
            {
                "name": "memory_write",
                "description": "Persist the current value in project memory",
                "config": {"namespace": "default", "key": "value"},
            },
            {
                "name": "memory_read",
                "description": "Load a value from project memory",
                "config": {"namespace": "default", "key": "value"},
            },
            {
                "name": "handoff",
                "description": "Delegate to another workflow and trace the child run",
                "config": {"workflow_id": 1, "max_steps": 100},
            },
            {
                "name": "fail",
                "description": "Raise a controlled failure for testing",
                "config": {"message": "Intentional failure"},
            },
        ]

    @app.get("/api/templates")
    def templates(request: Request):
        return service(request).list_templates()

    @app.post("/api/templates/{template_id}", status_code=201)
    def create_from_template(template_id: str, project_id: int, request: Request):
        require_project_access(request, project_id, write=True)
        return service(request).create_from_template(template_id, project_id)

    @app.get("/api/providers")
    def providers(request: Request):
        return service(request).providers.status()

    @app.post("/api/workflows/{workflow_id}/runs", status_code=201)
    def start_run(workflow_id: int, body: RunCreate, request: Request):
        require_workflow_access(request, workflow_id, write=True)
        return service(request).start_run(
            workflow_id,
            body.input,
            parent_run_id=body.parent_run_id,
            queued=body.execution == "queued",
            max_steps=body.max_steps,
            actor_role=request.state.actor.role,
        )

    @app.get("/api/runs")
    def runs(
        request: Request,
        workflow_id: int | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ):
        if workflow_id is not None:
            require_workflow_access(request, workflow_id)
        return service(request).list_runs(
            workflow_id, limit, project_ids=actor_project_ids(request)
        )

    @app.get("/api/runs/{run_id}")
    def run(run_id: int, request: Request):
        require_run_access(request, run_id)
        return service(request).get_run(run_id)

    @app.get("/api/runs/{left_id}/compare/{right_id}")
    def compare_runs(left_id: int, right_id: int, request: Request):
        require_run_access(request, left_id)
        require_run_access(request, right_id)
        return service(request).compare_runs(left_id, right_id)

    @app.post("/api/runs/{run_id}/replay", status_code=201)
    def replay(run_id: int, request: Request):
        require_run_access(request, run_id, write=True)
        return service(request).replay(run_id)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: int, request: Request):
        require_run_access(request, run_id, write=True)
        return service(request).cancel_run(run_id)

    @app.post("/api/schedules", status_code=201)
    def create_schedule(body: ScheduleCreate, request: Request):
        require_workflow_access(request, body.workflow_id, write=True)
        return service(request).create_schedule(
            body.workflow_id, body.name, body.input, body.interval_seconds
        )

    @app.get("/api/schedules")
    def schedules(request: Request):
        return service(request).list_schedules(actor_project_ids(request))

    @app.post("/api/schedules/{schedule_id}/enabled")
    def set_schedule_enabled(schedule_id: int, enabled: bool, request: Request):
        require_schedule_access(request, schedule_id, write=True)
        return service(request).set_schedule_enabled(schedule_id, enabled)

    @app.post("/api/schedules/run-due")
    def run_due_schedules(request: Request):
        return service(request).run_due_schedules()

    @app.post("/api/webhooks", status_code=201)
    def create_webhook(body: WebhookCreate, request: Request):
        require_project_access(request, body.project_id, write=True)
        return service(request).create_webhook(body.project_id, body.url, body.events)

    @app.get("/api/webhooks")
    def webhooks(request: Request, project_id: int | None = None):
        if project_id is not None:
            require_project_access(request, project_id)
        return service(request).list_webhooks(project_id, actor_project_ids(request))

    @app.get("/api/approvals")
    def approvals(request: Request, status: str | None = None):
        if status not in {None, "pending", "approved", "rejected", "expired"}:
            raise HTTPException(422, "invalid approval status")
        return service(request).list_approvals(status, actor_project_ids(request))

    @app.post("/api/approvals/{approval_id}/decision")
    def decide(approval_id: int, body: ApprovalDecision, request: Request):
        require_approval_access(request, approval_id, write=True)
        return service(request).decide_approval(
            approval_id,
            body.decision,
            body.note,
            body.output,
            "output" in body.model_fields_set,
        )

    @app.post("/api/approvals/{approval_id}/escalate")
    def escalate(approval_id: int, request: Request, note: str = ""):
        require_approval_access(request, approval_id, write=True)
        return service(request).escalate_approval(approval_id, note)

    @app.post("/api/approvals/expire")
    def expire_approvals(request: Request):
        return {"expired": service(request).expire_approvals()}

    @app.post("/api/datasets", status_code=201)
    def create_dataset(body: DatasetCreate, request: Request):
        require_project_access(request, body.project_id, write=True)
        return service(request).create_dataset(
            body.project_id, body.name, [case.model_dump() for case in body.cases]
        )

    @app.get("/api/datasets")
    def datasets(request: Request, project_id: int | None = None):
        if project_id is not None:
            require_project_access(request, project_id)
        return service(request).list_datasets(project_id, actor_project_ids(request))

    @app.get("/api/datasets/{dataset_id}")
    def dataset(dataset_id: int, request: Request):
        require_dataset_access(request, dataset_id)
        return service(request).get_dataset(dataset_id)

    @app.post("/api/evaluations", status_code=201)
    def evaluate(body: EvaluationCreate, request: Request):
        require_workflow_access(request, body.workflow_id, write=True)
        require_dataset_access(request, body.dataset_id, write=True)
        return service(request).evaluate(
            body.workflow_id, body.dataset_id, actor_role=request.state.actor.role
        )

    @app.get("/api/evaluations")
    def evaluations(
        request: Request,
        workflow_id: int | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ):
        if workflow_id is not None:
            require_workflow_access(request, workflow_id)
        return service(request).list_evaluations(
            workflow_id, limit, project_ids=actor_project_ids(request)
        )

    @app.get("/api/evaluations/{evaluation_id}")
    def evaluation(evaluation_id: int, request: Request):
        require_evaluation_access(request, evaluation_id)
        return service(request).get_evaluation(evaluation_id)

    @app.get("/api/evaluations/{left_id}/compare/{right_id}")
    def compare_evaluations(left_id: int, right_id: int, request: Request):
        require_evaluation_access(request, left_id)
        require_evaluation_access(request, right_id)
        return service(request).compare_evaluations(left_id, right_id)

    @app.get("/api/stats")
    def stats(request: Request):
        return service(request).stats(actor_project_ids(request))

    @app.get("/api/stats/trends")
    def trends(request: Request, limit: int = Query(default=30, ge=1, le=200)):
        return service(request).trends(limit, actor_project_ids(request))

    @app.websocket("/api/live")
    async def live(websocket: WebSocket):
        actor = Actor("local-user", "admin")
        await websocket.accept()
        if authenticator.enabled():
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            except (TimeoutError, ValueError, WebSocketDisconnect):
                await websocket.close(code=4401)
                return
            token = message.get("token", "") if isinstance(message, dict) else ""
            authenticated = authenticator.authenticate(token)
            if authenticated is None:
                await websocket.close(code=4401)
                return
            actor = authenticated
        project_ids = (
            None
            if actor.role == "admin"
            else app.state.service.project_ids_for_user(actor.name)
        )
        try:
            while True:
                current_service = websocket.app.state.service
                await websocket.send_json(
                    {
                        "stats": current_service.stats(project_ids),
                        "runs": current_service.list_runs(limit=20, project_ids=project_ids),
                        "alerts": current_service.list_alerts(project_ids),
                        "events": current_service.list_agent_events(
                            limit=100, project_ids=project_ids
                        ),
                    }
                )
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            return

    @app.get("/api/runs/{run_id}/otel")
    def otel_trace(run_id: int, request: Request):
        require_run_access(request, run_id)
        return service(request).otel_trace(run_id)

    @app.post("/api/alerts", status_code=201)
    def create_alert(body: AlertCreate, request: Request):
        return service(request).create_alert(body.name, body.metric, body.threshold)

    @app.get("/api/alerts")
    def alerts(request: Request):
        return service(request).list_alerts()

    @app.get("/api/runs/{run_id}/agent-tree")
    def agent_tree(run_id: int, request: Request):
        require_run_access(request, run_id)
        return service(request).agent_tree(run_id)

    @app.put("/api/memories")
    def put_memory(body: MemoryCreate, request: Request):
        require_project_access(request, body.project_id, write=True)
        return service(request).put_memory(body.project_id, body.namespace, body.key, body.value)

    @app.get("/api/memories")
    def memories(project_id: int, request: Request):
        require_project_access(request, project_id)
        return service(request).list_memories(project_id)

    @app.post("/api/users", status_code=201)
    def create_user(body: UserCreate, request: Request):
        return service(request).create_user(body.name, body.token, body.role)

    @app.get("/api/users")
    def users(request: Request):
        return service(request).list_users()

    @app.put("/api/project-members", status_code=201)
    def put_project_member(body: ProjectMemberCreate, request: Request):
        return service(request).put_project_member(body.project_id, body.user_name, body.role)

    @app.get("/api/project-members")
    def project_members(project_id: int, request: Request):
        return service(request).list_project_members(project_id)

    @app.put("/api/secrets", status_code=201)
    def put_secret(body: SecretCreate, request: Request):
        require_project_access(request, body.project_id, write=True)
        return service(request).put_secret(body.project_id, body.name, body.value)

    @app.get("/api/secrets")
    def secrets(request: Request, project_id: int):
        require_project_access(request, project_id)
        return service(request).list_secrets(project_id)

    @app.get("/api/secrets/{secret_id}/reveal")
    def reveal_secret(secret_id: int, request: Request):
        return {"value": service(request).reveal_secret(secret_id)}

    @app.get("/api/audit")
    def audit_events(request: Request, limit: int = Query(default=200, ge=1, le=1000)):
        return service(request).list_audit_events(limit)

    return app


def service(request: Request) -> AgentOpsService:
    return request.app.state.service


def actor_project_ids(request: Request) -> set[int] | None:
    actor: Actor = request.state.actor
    if actor.role == "admin":
        return None
    return service(request).project_ids_for_user(actor.name)


def grant_created_project(request: Request, project_id: int) -> None:
    actor: Actor = request.state.actor
    if actor.role != "admin":
        service(request).put_project_member(project_id, actor.name, "operator")


def require_project_access(request: Request, project_id: int, write: bool = False) -> None:
    actor: Actor = request.state.actor
    if actor.role == "admin":
        return
    role = service(request).project_role(project_id, actor.name)
    if role is None:
        raise HTTPException(status_code=403, detail="project membership required")
    if write and role == "viewer":
        raise HTTPException(status_code=403, detail="project membership is read-only")


def require_workflow_access(request: Request, workflow_id: int, write: bool = False) -> None:
    workflow = service(request).get_workflow(workflow_id)
    require_project_access(request, workflow["project_id"], write)


def require_run_access(request: Request, run_id: int, write: bool = False) -> None:
    run = service(request).get_run(run_id)
    require_workflow_access(request, run["workflow_id"], write)


def require_approval_access(request: Request, approval_id: int, write: bool = False) -> None:
    approval = service(request).list_approvals_by_id(approval_id)
    require_run_access(request, approval["run_id"], write)


def require_dataset_access(request: Request, dataset_id: int, write: bool = False) -> None:
    dataset = service(request).get_dataset(dataset_id)
    require_project_access(request, dataset["project_id"], write)


def require_evaluation_access(request: Request, evaluation_id: int) -> None:
    evaluation = service(request).get_evaluation(evaluation_id)
    require_workflow_access(request, evaluation["workflow_id"])


def require_schedule_access(request: Request, schedule_id: int, write: bool = False) -> None:
    schedule = service(request).get_schedule(schedule_id)
    require_workflow_access(request, schedule["workflow_id"], write)


def _error(status_code: int, message: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": message})


app = create_app()
