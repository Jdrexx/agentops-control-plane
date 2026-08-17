# Deployment

## Railway

`railway.json` selects the Dockerfile, gates deployments on dependency-aware
`/api/ready`, and restarts failed containers. Provision a persistent volume for
SQLite or a PostgreSQL service that exposes `DATABASE_URL`.

Required production variables:

- `AGENTOPS_API_KEY`: long random bootstrap administrator token
- `AGENTOPS_ENCRYPTION_KEY`: independent long random encryption secret

Optional variables:

- `DATABASE_URL`: automatically provided by Railway PostgreSQL
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OLLAMA_HOST`
- `AGENTOPS_OTLP_ENDPOINT`: HTTP collector endpoint for trace export
- `AGENTOPS_SLACK_WEBHOOK_URL`: Slack incoming webhook for run and approval notifications
- `AGENTOPS_SMTP_*` and `AGENTOPS_EMAIL_*`: TLS SMTP notification settings

The app listens on Railway's injected `PORT` value. After deployment, verify
`/api/health` for process liveness and `/api/ready` for database and queue
readiness, then authenticate API requests with
`Authorization: Bearer <AGENTOPS_API_KEY>`.

## Staging smoke test

Keep staging on a database and Redis instance that are distinct from production.
The reusable smoke command authenticates as the staging administrator, reuses a
dedicated fixture project and workflow, submits a queued run, waits for the
worker, and verifies its recorded trace. It refuses non-staging hostnames unless
explicitly overridden.

```bash
railway run --environment staging --service agentops-control-plane \
  .venv/bin/python scripts/staging_smoke.py \
  --base-url https://agentops-control-plane-staging.up.railway.app
```

Run it after staging deployments and before promoting higher-risk changes. It
creates the fixture once and adds one run record per invocation.

## Optional integration activation

Provider, telemetry, and notification integrations are optional and remain
disabled when their variables are absent. Activate them one at a time in
staging before copying configuration to production:

1. **Model provider:** configure one of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
   or `OLLAMA_HOST`; verify `/api/providers`; then run one budget-limited `llm`
   workflow.
2. **Trace export:** configure `AGENTOPS_OTLP_ENDPOINT` for a staging collector,
   complete one workflow, and verify that the collector received the trace.
3. **Notifications:** configure either `AGENTOPS_SLACK_WEBHOOK_URL` or the
   `AGENTOPS_SMTP_*` and `AGENTOPS_EMAIL_*` variables, trigger one controlled
   failed run and approval event, and verify delivery.
4. Use independent staging credentials and destinations. Promote only the
   variable names and tested configuration pattern, never staging secrets.

Do not make deployment readiness depend on these optional destinations; a
provider or notification outage must not make the control plane unavailable.

## Docker

The image runs as an unprivileged user and uses a multi-stage-like dependency install with the locked production environment. Mount `/data` when using SQLite. The Compose PostgreSQL profile is intended for local parity; replace its demonstration password in any shared environment.

## Scaling

Run one application replica with the built-in scheduler. Horizontal scaling requires moving queued execution and schedule leasing to a distributed worker. PostgreSQL is recommended before introducing external workers.
