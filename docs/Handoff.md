# AgentOps Control Plane — Handoff

Last updated: 2026-08-19 · Branch: `main` @ `2a0799d` · 118 tests · 90% coverage · ruff clean

## What this project is

A local-first control plane for building, running, approving, evaluating, and
observing AI-agent workflows. One Docker Compose command (or `uv run`) brings up a
dashboard, REST API, scheduler, queue, and deterministic mock LLM provider — the
entire product is demonstrable offline with zero API keys.

Public repo: https://github.com/Jdrexx/agentops-control-plane
Local clone: `/home/j-alien/Documents/GitRepositories/agentops-control-plane`

## Where we are (2026-08-18/19)

A 20-item robustness plan (Triad+ multi-voice analysis, report in
`BrainVault/AI work/Triad/Triad_20260818_131156__ AgentOps Control Plane _ project conte.md`)
was executed against the pre-existing base. **12 of 20 items are shipped**; the
remaining 4 are scoped below.

### Shipped since the Triad analysis (all merged to `main`, CI green)

| # | Item | PR(s) | What landed |
|---|------|-------|-------------|
| 1 | Deterministic mock provider | #11 | Paced streaming, pinned response scripts (`AGENTOPS_MOCK_*`), failure injection, exact token counts — demo runs fully offline |
| 2 | Server-side demo scenarios | #12 | `POST /api/demo/seed` with tour / quality / incident scenes, idempotent by project, deep-links |
| 3 | Versioned schema migrations | #11 | Hand-rolled ledger (0001–0009), cross-process lock (`BEGIN IMMEDIATE` / `pg_advisory_lock`), Postgres previously never got later columns |
| 4 | Webhook outbox + SSRF + HMAC | #15 | Delivery off the request path, atomic claims + lease expiry, 6 attempts → dead-letter, private-IP rejection at create and at delivery, `X-AgentOps-Signature` HMAC, orphan dead-lettering |
| 5 | Quality Lab product surface | #13 | Stable case IDs, queued evals with progress/cancel, release gates (`pass_rate_min`), case-level diff (regressed/fixed/removed/added/stable_*), jsonschema matcher, dataset delete, full dashboard UI |
| 6 | Provider usage + retries | #16 | `generate_detailed()` → real token usage into spans (Ollama eval counts, OpenAI/Anthropic usage, exact mock), retry classification (408/429/5xx + transport, Retry-After/backoff, `AGENTOPS_PROVIDER_RETRIES`), malformed streams fail, no retry after published chunks |
| 7 | Secrets → LLM steps | #17 | `credential_ref` in LLM step config, decrypted project secret passed as provider API key, reveals moved to POST and audited (incl. workflow-time reveals), plaintext never in runs/spans/exports |
| 9 | Pagination + idempotency | #18 | `Idempotency-Key` on run creation (unique partial index per workflow), cursor pagination on `/api/audit` + new `/api/events`, run filters (status/project_id/has_parent), run-selector compare dialog |
| 13 | Approval governance | #19 | `approver_roles` enforced at decision time, `decided_by` + `policy` on approval rows, read-time expiry derivation (no scheduler dependency), expired decisions materialize + reject, viewer-only policies rejected |
| 14 | README proof | #14 | Capability matrix, Mermaid architecture diagram, `docs/demo.gif` (605 KB, offline), honest Limitations section |

Also in the baseline (pre-Triad, already on `main`): auth (API key / user roles /
project membership), approval workflow + escalation, replay, run comparison,
templates, schedules, notifications, OTLP-shaped telemetry hooks, export/import,
audit trail, production smoke guards.

### The Codex review bot earned its keep

`chatgpt-codex-connector` auto-reviews every PR and its threads block merges
(`required_conversation_resolution`). Every review this cycle found real issues:
README overclaims (backups not client-side encrypted, token counts are estimates,
escalation API-only), outbox double-delivery, Postgres `ON CONFLICT` crash,
non-unique idempotency index, cross-project idempotency leak, WS event ordering,
audit page ordering, CASE-alias filtering on Postgres, rollback-swallowed expiry
materialization, viewer-only policies, un-audited workflow-time secret reveals.
**None of its findings were noise.** Treat its threads as a free P1/P2 audit.

## Repo conventions (the gate every PR must pass)

- Lint: **ruff only** (`ruff check .`). Pyright-only type hints are acceptable.
- Tests: `pytest -q -W error::ResourceWarning` — currently **118 passing**.
- Coverage: `pytest --cov=src.agentops` — currently **90%** (floor is 85% in CI).
- JS: `node --check src/agentops/static/app.js`.
- CI: `uv sync --frozen` — **regenerate `uv.lock` whenever `pyproject.toml` changes**
  (jsonschema was added this way; a stale lock fails CI).
- Demo/GIF capture: mock provider env vars — see README "Demo scenarios".

## Git workflow (protected `main`)

- `main` is protected: PR + passing CI + linear history, `gh pr merge --rebase`.
- Merge sequence that works:
  1. Branch, commit, push, open PR.
  2. Wait for CI (`gh pr checks <n>`).
  3. Query review threads: GraphQL `pullRequest(number: n) { reviewThreads { nodes { id isResolved comments { nodes { body } } } } }`.
  4. If the bot found real issues → fix on the branch, push, then resolve each
     thread via `resolveReviewThread` mutation.
  5. If the bot posted a **COMMENTED review with no threads** (boilerplate only),
     it still blocks. You cannot dismiss COMMENTED reviews via the REST API;
     `--admin` merge also refuses with "All comments must be resolved". Wait for
     the bot's threads to appear (they arrive shortly after the review lands),
     resolve them, then merge normally.
- If a branch accidentally contains another branch's commits: `git checkout -B <branch> <base>` + `git apply` the split patches (`git diff <base> <bad> -- <paths>`), or `git rebase --onto origin/main <base> <branch>`.
- Local branch cleanup after each merge: `git checkout main && git reset --hard origin/main`.

## Environment variables (documented in `.env.example`)

`AGENTOPS_API_KEY` · `AGENTOPS_ENCRYPTION_KEY` (Fernet vault) ·
`AGENTOPS_DATABASE` (sqlite path or postgres URL) · `AGENTOPS_PROCESS_MODE`
(web/worker/scheduler) · `AGENTOPS_MOCK_CHUNK_MS` · `AGENTOPS_MOCK_LATENCY_MS` ·
`AGENTOPS_MOCK_SCRIPT_DIR` · `AGENTOPS_OUTBOX_POLL_MS` · `AGENTOPS_WEBHOOK_SECRET`
(opt-in HMAC) · `AGENTOPS_PROVIDER_RETRIES` · provider keys
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OLLAMA_HOST`).

## Next steps

### Immediate (recommended order)

1. **#11 — WebSocket + telemetry hardening.** Validate the `Origin` header on the
   WS handshake (CSWSH guard); move the per-frame sync DB queries off the event
   loop (cache the recent-events snapshot or run in a worker); rename the
   "OTLP exporter" honestly (it emits OTLP-shaped JSON, not real OTLP) or
   implement a true exporter.
2. **#10 — Structured logging + `/metrics`.** JSON-lines logging (request/run/
   queue events), and a `GET /metrics` endpoint in Prometheus text format
   (runs total, statuses, latency histogram, outbox depth, queue depth). Keep
   the endpoint on the admin path list.
3. **#8 — TransitionService extraction.** `_execute` in `service.py` is the
   largest method (~200 lines) and the core of the product. Extract step
   transitions into a dedicated module with its own unit tests (no FastAPI
   fixtures), and record `step_attempts` per span so retries are visible in the
   trace UI.
4. **#12 — Run manifest for reproducible replay/eval.** Snapshot workflow
   definition + provider versions + mock script pins into a manifest at run
   start; replay/eval reuse the manifest so "v1 fails 2 cases, v2 passes" is
   byte-reproducible and the claim is defensible in interviews.

### Deliberately not doing (per Triad verdict — do not resurrect without a reason)

React/Vue rewrite (ES modules are fine), SQLAlchemy (hand-rolled adapter +
migration runner is on-thesis), LLM tool-calling, Redis rate limiting, coverage
past 85–90%, multi-signer approvals, cloud deployment before real client demand.

### Stretch / backlog (from `docs/NEXT_STEPS.md`)

Five-minute walkthrough video, Compose smoke check, clean-machine setup
verification, ADR-style tradeoff docs (migration runner, SQLite-vs-Postgres
boundary, outbox-without-Redis), example repository of demo workflows.

## Gotchas learned this cycle

- `sqlite3.Row` has no `.get()` — use `row["col"]` with truthiness checks.
- SQLite **and** PostgreSQL both reject `WHERE` on a SELECT alias — repeat the
  CASE expression or wrap in a subquery.
- A `with db.connect()` context rolls back on exception — commit side effects
  (e.g. expiry materialization) in their own transaction before raising.
- Partial unique indexes (`WHERE col IS NOT NULL`) work on both dialects and are
  the right shape for idempotency keys.
- SCHEMA `CREATE INDEX` statements must not reference columns added by later
  migrations — legacy DBs run migrations in order and 0001 would fail. Keep
  late-column indexes inside their own migration.
- The WS live feed needs chronological (ascending) events; REST lists are
  newest-first. `list_agent_events(ascending=...)` covers both.
- Reverts of "nearly identical" validation blocks: fuzzy patching can match the
  wrong block (`allowed_roles` vs `approver_roles`) — always re-read the file
  after a failed patch instead of assuming the tree is what you expect.
- `git checkout main && git reset --hard origin/main` is how the tree moved under
  in-flight patches mid-session — check `git branch --show-current` before
  patching when multiple branches are open.

## Verification evidence (2026-08-19)

- `ruff check .` → All checks passed
- `pytest -q -W error::ResourceWarning` → 118 passed
- `pytest --cov=src.agentops` → 90% (2463 statements)
- `node --check src/agentops/static/app.js` → ok
- Live smoke: outbox enqueued on run completion and delivered; private-URL
  webhook rejected 422; demo GIF captured on a fully offline server (port 8122).
