# Running the proof of concept

The portfolio version is designed to run on a personal machine without a cloud
service. Docker Compose provides the most reproducible review path, while a native
Python process is convenient during development.

## Docker Compose

Start the complete single-process application with persistent local data:

```bash
docker compose up --build agentops
```

Open <http://127.0.0.1:8110>. The `agentops-data` volume preserves the SQLite database
between container restarts. The application uses its in-process worker and scheduler,
so no database or queue service needs to be purchased or maintained.

Use the optional PostgreSQL profile only when demonstrating database portability:

```bash
docker compose --profile postgres up --build agentops-postgres
```

That profile is a local demonstration configuration. Replace its example password
before using it on any shared network.

## Native Python

```bash
uv sync
uv run uvicorn src.agentops.main:app --reload --host 127.0.0.1 --port 8110
```

The default database is `data/agentops.db`. Set `AGENTOPS_DATABASE` to choose another
SQLite path. Open `/api/health` for liveness, `/api/ready` for database and queue
readiness, and `/docs` for the interactive API reference.

## Portfolio review flow

1. Open the dashboard and select **Load demo workflow**.
2. Run the generated workflow and inspect its step trace and metrics.
3. Demonstrate replay, approval, evaluation, project export, and role-aware APIs as
   relevant to the conversation.
4. Show `docs/ARCHITECTURE.md` and the passing CI checks to explain the design and
   engineering tradeoffs.

The built-in workflow tools do not require model-provider credentials. Add an
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or local `OLLAMA_HOST` only when an LLM step is
part of the demonstration.

## Network exposure

The default configuration assumes loopback-only access. For an occasional remote
demonstration, prefer a screen share or a short-lived authenticated tunnel rather than
an always-on paid deployment. Before accepting network traffic:

- set independent, long random values for `AGENTOPS_API_KEY` and
  `AGENTOPS_ENCRYPTION_KEY`;
- terminate TLS at a trusted reverse proxy or tunnel;
- keep the SQLite data directory persistent and backed up;
- restrict access to the intended reviewers; and
- remove the public route when the demonstration ends.

Authenticate API requests with `Authorization: Bearer <AGENTOPS_API_KEY>`. Review the
[threat model](THREAT_MODEL.md) before exposing the application.

## Optional integrations

Provider, telemetry, and notification integrations remain disabled when their variables
are absent. They are portable environment-variable integrations rather than hosting
requirements:

- `DATABASE_URL`: optional PostgreSQL database
- `REDIS_URL`: optional durable run queue for separated workers
- `AGENTOPS_OTLP_ENDPOINT`: optional HTTP trace collector
- `AGENTOPS_SLACK_WEBHOOK_URL`: optional Slack notifications
- `AGENTOPS_SMTP_*` and `AGENTOPS_EMAIL_*`: optional TLS SMTP notifications

The default `AGENTOPS_PROCESS_MODE=all` is appropriate for the proof of concept. A
future multi-service deployment can use `web`, `worker`, and `scheduler` modes with
PostgreSQL and Redis. That expansion is intentionally not required for personal use.
