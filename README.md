# AgentOps Control Plane

A local-first control plane for building, running, approving, evaluating, and observing AI-agent workflows.

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
- SQLite or PostgreSQL persistence, Docker, Compose, Railway, and project import/export

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

SQLite with a persistent volume:

```bash
docker compose up --build agentops
```

PostgreSQL profile:

```bash
docker compose --profile postgres up --build agentops-postgres
```

The SQLite service listens on port 8110; the PostgreSQL-backed service listens on 8111.

## Workflow tools

`input`, `template`, `uppercase`, `lowercase`, `json_extract`, `llm`, `memory_read`, `memory_write`, `handoff`, `approval`, and `fail`.

Every step configuration may include `retries`, `retry_delay_seconds`, `timeout_seconds`, and `allowed_roles`. LLM steps also accept provider, model, system, prompt, and optional per-1K-token cost rates. Approval steps accept a prompt and `expires_in_seconds`.

## Quality checks

```bash
uv run ruff check .
uv run pytest --cov=src.agentops --cov-report=term-missing
node --check src/agentops/static/app.js
```

See [Architecture](docs/ARCHITECTURE.md), [Deployment](docs/DEPLOYMENT.md), and [Threat Model](docs/THREAT_MODEL.md).

Production operators should also follow [Backup and recovery](docs/BACKUP_AND_RECOVERY.md).

## License

MIT
