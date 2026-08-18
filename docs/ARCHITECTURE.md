# Architecture

## Boundaries

- **FastAPI application:** validation, authentication and role enforcement, secure headers, rate limiting, REST APIs, WebSocket updates, and the static dashboard.
- **AgentOps service:** immutable workflow versions, execution state transitions, evaluations, approvals, schedules, webhooks, memory, handoffs, telemetry, secrets, and audit records.
- **Provider registry:** normalized Ollama, OpenAI, and Anthropic text generation through a single boundary.
- **Database compatibility layer:** SQLite for personal operation and PostgreSQL for larger or multi-service deployments. Bound parameters are retained across both drivers.
- **Worker and scheduler:** a bounded local thread pool executes queued runs; a scheduler dispatches due schedules and expires approvals. Queued and interrupted running work is recovered on startup.

## Run state model

Runs enter `running` or `queued`. A worker moves queued work to `running`, then to `completed`, `failed`, or `waiting_approval`. Operators can move non-terminal work to `cancelled`. Approval decisions resume a run or finish it as `rejected`; expiry also rejects the run. Terminal history is never rewritten.

Each completed attempt creates one ordered span containing status, input/output, error, latency, estimated tokens, and cost. Step retries happen before the final span is recorded. Replay creates a linked run. Handoffs create linked child runs and structured agent events.

## Data and security

Workflow definitions, inputs, outputs, memory, evaluation cases, and event payloads are JSON. API presentation recursively redacts common credential fields while internal replay and recovery retain the original persisted payload. Project secrets are separately encrypted with Fernet using a key derived from `AGENTOPS_ENCRYPTION_KEY`.

Authentication is optional in trusted local mode. Once enabled, bearer tokens are SHA-256 hashed at rest and map to admin, operator, or viewer roles. Mutating requests are audited.

## Extension points

Provider calls are isolated in `providers.py`. A distributed queue can replace the local executor while keeping the run state contract. The OTLP-shaped export can be routed to an OpenTelemetry collector. The database compatibility layer allows a hosted PostgreSQL service without changing domain queries.
