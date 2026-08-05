# Deployment

## Railway

`railway.json` selects the Dockerfile, configures `/api/health`, and restarts failed containers. Provision a persistent volume for SQLite or a PostgreSQL service that exposes `DATABASE_URL`.

Required production variables:

- `AGENTOPS_API_KEY`: long random bootstrap administrator token
- `AGENTOPS_ENCRYPTION_KEY`: independent long random encryption secret

Optional variables:

- `DATABASE_URL`: automatically provided by Railway PostgreSQL
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OLLAMA_HOST`
- `AGENTOPS_OTLP_ENDPOINT`: HTTP collector endpoint for trace export
- `AGENTOPS_SLACK_WEBHOOK_URL`: Slack incoming webhook for run and approval notifications
- `AGENTOPS_SMTP_*` and `AGENTOPS_EMAIL_*`: TLS SMTP notification settings

The app listens on Railway's injected `PORT` value. After deployment, verify `/api/health`, then authenticate API requests with `Authorization: Bearer <AGENTOPS_API_KEY>`.

## Docker

The image runs as an unprivileged user and uses a multi-stage-like dependency install with the locked production environment. Mount `/data` when using SQLite. The Compose PostgreSQL profile is intended for local parity; replace its demonstration password in any shared environment.

## Scaling

Run one application replica with the built-in scheduler. Horizontal scaling requires moving queued execution and schedule leasing to a distributed worker. PostgreSQL is recommended before introducing external workers.
