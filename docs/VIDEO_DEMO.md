# Video demo runbook

This is the repeatable five-minute portfolio walkthrough. It uses only the local
Docker Compose service and deterministic mock provider; no API keys, paid model,
Ollama download, or network call is part of the product demonstration.

## Preflight

Use a fresh clone or pull the intended commit. Close unrelated applications and
notifications, then run:

```bash
docker compose up -d --build agentops
python scripts/demo_smoke.py
```

The smoke command must end with `Demo smoke passed`. If it does not, stop and
fix the environment rather than recording around the failure.

Open <http://127.0.0.1:8110>, use a 1440×900 or larger browser window, set zoom
to 100%, and tick **reset demo data**. Keep DevTools, terminal secrets, local
paths, deployment dashboards, and repository status checks off screen.

## Five-minute walkthrough

### 0:00–0:35 — Problem and promise

Start on Operations:

> AgentOps is a local-first flight recorder and release gate for inspectable
> AI-agent workflows. It lets an operator trace runs, approve sensitive actions,
> compare quality, and diagnose failures without exporting operational data.

Call the project a portfolio proof of concept, not a production SaaS.

### 0:35–2:15 — Human-controlled support workflow

1. Click **Support tour**.
2. Point out the paced provider stream and the run entering
   `waiting_approval`.
3. Open the trace and briefly identify the model step and approval boundary.
4. Click **Approve & resume**.
5. Show the QA child handoff and completed parent trace.

Narrative: model output is observable, a sensitive action is gated, and the
handoff remains part of one inspectable run tree.

### 2:15–3:30 — Regression gate

1. Click **Regression check**.
2. Open **Quality**.
3. Select the two seeded evaluations and click **Compare selected**.
4. Highlight v1 at 4/6, v2 at 6/6, two fixed cases, and zero regressions.

Narrative: immutable workflow versions can be evaluated before release, with
case-level evidence rather than a single aggregate score.

### 3:30–4:20 — Incident diagnosis

1. Return to Operations and click **Incident**.
2. Show the failed billing-sync run, retry evidence, error detail, and triggered
   failure-rate alert.

Narrative: the failure is deliberately injected by the offline mock provider,
so the demonstration is deterministic and repeatable.

### 4:20–5:00 — Architecture and honest boundary

Explain that the demo uses FastAPI, SQLite, and in-process worker/scheduler
modes. PostgreSQL, Redis, separated workers, real providers, notifications, and
webhooks are extension boundaries.

Do not claim multi-replica production readiness, shared rate limiting, cron
scheduling, real OTLP/protobuf export, or client-side encrypted backups.

## Recovery cues

- If data from an earlier rehearsal appears, tick **reset demo data** and repeat
  the scenario.
- If the approval is already resolved, reset and reseed Support tour.
- If the browser loses the live stream, refresh and reopen the selected run;
  the persisted trace remains available.
- If startup or a scenario fails, run `docker compose logs --tail=200 agentops`
  and do not record until `python scripts/demo_smoke.py` passes.
- The unrelated optional Railway backup deployment is not part of this local
  demo gate and should not be shown as application health.

## Cleanup

```bash
docker compose down
```

Keep the named volume if another take is likely. To remove demo state completely,
run `docker compose down -v`.
