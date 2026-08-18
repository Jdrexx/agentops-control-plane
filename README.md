# AgentOps Control Plane

A local-first control plane for building, running, approving, evaluating, and observing
AI-agent workflows. This repository is a portfolio proof of concept: it runs entirely on
your machine and does not require a paid hosting account.

## Product capabilities

- Visual workflow studio with immutable workflow versions
- Detailed run and step traces, JSON inspection, replay, and comparisons
- Ollama, OpenAI, and Anthropic model adapters
- Synchronous or queued execution, retries, cancellation, recovery, schedules, and webhooks
- Exact, contains, regex, JSON-schema, and LLM-judge evaluations with regression comparisons
- Editable human approvals, expiry, escalation, and notification events
- Optional bearer authentication with admin, operator, and viewer roles
- Encrypted project secrets, trace redaction, rate limits, secure headers, and audit events
- Live WebSocket updates, token/cost metrics, alerts, and OTLP JSON export
- Agent handoffs, parent/child trees, project memory, step budgets, and loop detection
- Durable Redis-backed run queues with atomic PostgreSQL claims and local fallback
- Streamed Ollama, OpenAI, and Anthropic output over the live dashboard channel
- Starter workflow templates and project-level team membership roles
- SQLite or PostgreSQL persistence, Docker, Compose, and project import/export

## Quick portfolio demo

The fastest way to review the complete application is Docker Compose:

```bash
docker compose up --build agentops
```

Open <http://127.0.0.1:8110>. The dashboard offers three one-click offline
scenarios (all run on the deterministic `mock` provider — no API key, no
network, no Ollama download):

- **Support tour** — a streamed LLM draft pauses at a human approval, resumes
  on approval, hands off to a QA child workflow, and writes to project memory.
  Watch the live provider stream type out the draft.
- **Regression check** — a 6-case dataset evaluated against two immutable
  workflow versions: v1 fails 2 cases, v2 fixes them. Compare the evaluation
  history rows to see the regression closed.
- **Incident** — a billing-sync run that fails after retries with a triggered
  failure-rate alert and a failed run in the feed.

Each scenario is idempotent (seeding again returns the existing demo); tick
**reset demo data** to delete and rebuild it. The demo uses SQLite, keeps its
data in the local `agentops-data` Docker volume, and runs queued work in the
application process. No PostgreSQL, Redis, cloud account, or model-provider key
is required.

## Local development

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
uv run uvicorn src.agentops.main:app --reload --port 8110
```

Open <http://127.0.0.1:8110>. Interactive API docs are at <http://127.0.0.1:8110/docs>.

The default database is `data/agentops.db`. Set either `AGENTOPS_DATABASE` or `DATABASE_URL` to use another SQLite path or PostgreSQL URL.

## Security configuration

Local mode is intentionally frictionless. Authentication activates when `AGENTOPS_API_KEY` is set or a user has been created.

```bash
AGENTOPS_API_KEY='use-a-long-random-token' \
AGENTOPS_ENCRYPTION_KEY='use-an-independent-long-random-secret' \
uv run uvicorn src.agentops.main:app --port 8110
```

Provider keys are read from `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`. Ollama defaults to `http://127.0.0.1:11434` and can be changed with `OLLAMA_HOST`. Set `AGENTOPS_OTLP_ENDPOINT` to automatically export completed and failed traces.

## Containers

The default personal-use configuration is the same one used by the quick demo:

```bash
docker compose up --build agentops
```

An optional PostgreSQL profile is available for demonstrating the portable database
layer:

```bash
docker compose --profile postgres up --build agentops-postgres
```

The SQLite service listens on port 8110; the PostgreSQL-backed service listens on 8111.
Neither configuration depends on a particular hosting provider.

## Workflow tools

`input`, `template`, `uppercase`, `lowercase`, `json_extract`, `llm`, `memory_read`, `memory_write`, `handoff`, `approval`, and `fail`.

Every step configuration may include `retries`, `retry_delay_seconds`, `timeout_seconds`, and `allowed_roles`. LLM steps also accept provider, model, system, prompt, and optional per-1K-token cost rates. Approval steps accept a prompt and `expires_in_seconds`.

A built-in deterministic `mock` provider (model `mock-small`) generates byte-identical
offline output with no key and no network — useful for demos, tests, and CI. It
streams with paced chunks (`AGENTOPS_MOCK_CHUNK_MS`, default 35), honors a fixed
latency (`AGENTOPS_MOCK_LATENCY_MS`), and loads pinned responses from
`AGENTOPS_MOCK_SCRIPT_DIR` (default `examples/mock_responses/`) so a scenario can
guarantee exact output. See `examples/mock_responses/README.md` for the pinning
workflow.

## Quality checks

```bash
uv run ruff check .
uv run pytest --cov=src.agentops --cov-report=term-missing
node --check src/agentops/static/app.js
```

See [Architecture](docs/ARCHITECTURE.md), [Deployment](docs/DEPLOYMENT.md),
[Next steps](docs/NEXT_STEPS.md), and [Threat Model](docs/THREAT_MODEL.md).

If you later expose the application outside a trusted machine, follow the network-exposed
guidance in [Deployment](docs/DEPLOYMENT.md), [Threat Model](docs/THREAT_MODEL.md), and
[Backup and recovery](docs/BACKUP_AND_RECOVERY.md).

The default `AGENTOPS_PROCESS_MODE=all` keeps the web app, worker, and scheduler in one
process for personal use. The `web`, `worker`, and `scheduler` modes remain available for
future multi-service deployments.

## License

MIT
