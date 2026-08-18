# Next steps

This backlog keeps AgentOps Control Plane focused as a portfolio proof of concept.
Cloud operations are intentionally deferred until real client demand justifies their
ongoing cost.

## Current baseline

- A reviewer can launch the complete application with one Docker Compose command.
- SQLite, the in-process queue, and the built-in scheduler require no external services.
- The dashboard offers three one-click offline demo scenarios (support tour,
  regression check, incident) on a deterministic mock provider — no keys, no
  network, no Ollama download.
- The Quality Lab is fully operable from the dashboard: dataset creation,
  queued evaluations with progress and cancellation, release gates, and
  case-level regression diffs.
- PostgreSQL, Redis, separated process modes, telemetry, notifications, and S3-compatible
  backups remain visible as portable extension points.
- Schema changes are versioned migrations applied under a cross-process lock on
  both SQLite and PostgreSQL.
- CI runs linting, tests with an 85% coverage floor, JavaScript validation, and a
  production-container build.

## P0: improve the portfolio experience

### 1. Add visual proof to the README

- [x] Capability matrix and Mermaid architecture diagram in the README.
- [x] Three offline demo scenarios replace the old single seed workflow.
- [ ] Record a short GIF showing a run, its step trace, replay, and evaluation results.
- [ ] Keep all example data synthetic and remove API keys, tokens, and local paths.

Done when a reviewer can understand the product in under a minute without installing it.

### 2. Create a five-minute walkthrough

- [ ] Explain the user problem and target operator.
- [x] Run the built-in demo workflow (three scenarios).
- [ ] Show approval, replay, evaluation, and cost/latency observability.
- [ ] Close with the architecture boundary and production scaling path.

Done when the walkthrough is repeatable without a cloud deployment or live paid model
call.

### 3. Make reviewer setup nearly automatic

- [x] Keep `docker compose up --build agentops` as the canonical command.
- [ ] Add a smoke check for the Compose-based SQLite configuration.
- [ ] Verify setup on a clean machine and record the expected startup time.
- [x] Keep the seed action idempotent so repeat demonstrations stay tidy.

Done when a new reviewer can reach the dashboard and complete the demo using only the
README.

## P1: strengthen the engineering story

### 4. Document key tradeoffs

- Explain why SQLite and an in-process queue are appropriate for the proof of concept.
- Document the boundary for moving to PostgreSQL, Redis, and separated workers.
- Call out single-process scheduling, per-process rate limits, and trusted in-process
  adapters as deliberate constraints rather than hidden production claims.

### 5. Exercise portable extension points

- Keep PostgreSQL compatibility covered by tests.
- Add integration tests for Redis-backed queued execution when a disposable Redis is
  available.
- Test one OpenTelemetry export and one notification path with local fixtures.
- Keep optional integrations outside application readiness.

### 6. Collect product feedback

- Ask prospective users which run failures are hardest to diagnose.
- Record whether approvals, replay, evaluation, or cost controls are the strongest hook.
- Turn repeated feedback into small, demonstrable issues rather than broad platform work.

## Hosting decision gate

Do not add an always-on deployment merely to make the repository look complete. Revisit
hosting when at least one of these is true:

- a client needs a persistent shared evaluation environment;
- multiple reviewers cannot reasonably run Docker locally;
- a live pilot has an owner, access policy, and usage window; or
- the expected business value clearly exceeds hosting and maintenance cost.

Before any network-exposed pilot, require authentication, encryption, TLS, backups,
dependency monitoring, and a tested shutdown path. Until then, a local run, screen share,
or short-lived authenticated tunnel is the intended demonstration model.
