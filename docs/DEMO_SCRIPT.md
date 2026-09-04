# 3-minute demo script

A recording script for the portfolio walkthrough. Three acts on the three
built-in offline scenarios: human-in-the-loop control, an incident, and a
release gate. Everything runs on the deterministic `mock` provider — no API
key, no network.

This is the short cut. [`VIDEO_DEMO.md`](VIDEO_DEMO.md) is the five-minute
runbook covering the same scenarios in more depth plus recovery cues; the two
should agree, so change both if you change the flow.

Narration is ~325 words — about 2:10 of actual speech at a normal 150 wpm. The
remaining 50 seconds are deliberate pauses while the UI does something worth
watching. Only the incident makes you wait (~2 s of retries); everything else
responds in well under a second, so the voice sets the pace, not the software.
If you talk through the gaps you will finish early and the demo will feel rushed.

## Before you record

```bash
cd agentops-control-plane
docker compose down -v          # wipes the volume so the stat tiles start at zero
AGENTOPS_MOCK_CHUNK_MS=60 docker compose up -d --build agentops
python3 scripts/demo_smoke.py   # must end with "Demo smoke passed"
```

Open <http://127.0.0.1:8110> and confirm the stat tiles read **0**.

- Do not record until `demo_smoke.py` passes — fix the environment instead.
- `down -v` is what resets the counters. Without it you open on a dirty board.
- `AGENTOPS_MOCK_CHUNK_MS=60` slows the streamed draft from the 35 ms default so
  the typing effect is visible on camera. It does not change the text.
- Make sure `AGENTOPS_DEMO_ENABLED` is unset (or `1`), or the buttons are gone.
- Browser at ~1280×800, zoom 110%. The trace inspector is the smallest text you
  will ask a viewer to read.

## The script

| Time     | On screen                                                         | Say                                                                                                                                                                                                                 |
| -------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **0:00** | Dashboard, all tiles at zero.                                     | "This is AgentOps Control Plane — a local-first flight recorder and release gate for AI agent workflows. It's running on my machine: no cloud account, no API key, no model download. Three things it does."        |
| **0:15** | Click **Support tour**. Live provider stream panel starts typing. | "First, control. This is a support workflow drafting a customer reply. That's the model's output streaming in live."                                                                                                |
| **0:30** | Run stops. Approval inbox fills in on the right.                  | "And it stops. The workflow hit an approval step, so the run is parked in `waiting_approval` — nothing was sent. A human decides."                                                                                  |
| **0:45** | Click **Approve & resume**.                                       | "I approve it, and the run picks up where it left off."                                                                                                                                                             |
| **0:55** | Click the run in the feed. Trace inspector opens.                 | "Here's the whole trace. Four steps: the drafted reply, the approval gate, a handoff that spawned a child QA workflow as its own run, and a write to project memory. Every step has its input, output, and timing." |
| **1:15** | Click **Incident**. Failed run appears at the top of the feed.    | "Second, observability when things break. This is a nightly billing sync."                                                                                                                                          |
| **1:25** | Click the failed run. Trace shows the error.                      | "It failed — connection refused to the billing API — and the trace holds the actual error, not a status code. The stat tiles moved, and the failure-rate alert I'd configured is now triggered."                    |
| **1:45** | Click **Regression check**, then the **Quality** tab.             | "Third, and the one I actually care about: shipping. Workflow versions here are immutable, so you can evaluate them against a dataset like test cases."                                                             |
| **2:00** | Two evaluations listed: 67% and 100%.                             | "Same six-case dataset, two versions of a ticket classifier. Version one passes four of six. Version two passes all six."                                                                                           |
| **2:10** | Tick both evaluations. Click **Compare selected**.                | "But a pass rate is not enough to ship on — I need to know _which_ cases moved."                                                                                                                                    |
| **2:20** | Diff panel opens, headed "Workflow N → Workflow N+1".             | "Two fixed, zero regressed, four stable. Both billing tickets went from FAIL to PASS, and nothing that worked before broke. That's the difference between a number going up and a release you can defend."          |
| **2:36** | Back to **Operations**. Stat tiles.                               | "Fifteen runs, one deliberate failure, cost estimated per run — all of it offline on a deterministic provider, so this demo is byte-identical every time."                                                          |
| **2:48** | Hold on the dashboard.                                            | "Point a step at OpenAI, Anthropic, or a local Ollama model and the traces, approvals, and gates work exactly the same. Repo's in the description."                                                                 |

## Gotchas that will bite you on camera

**Checkbox order no longer matters.** It used to: the comparison took the two
selections in the order you ticked them, and since the list renders newest-first,
ticking top-to-bottom diffed v2 against v1 and reported the two fixes as
**"Regressed 2, −33.3%"**. That is fixed — the older evaluation is always the
baseline, and the diff panel now states the direction in its header. Tick them in
whatever order you like.

**Run the scenarios in the order above: tour → incident → regression.** The
regression scenario executes 12 evaluation runs, which flood the execution feed
and bury the tour run you want to point at. By the time it fires you've moved to
the Quality tab, so the mess never shows.

**Tick "reset demo data" before every retake.** Seeding is idempotent by project
name — clicking a scenario a second time silently returns the existing demo and
nothing appears to happen. The checkbox is what forces a rebuild.

**The incident takes ~2 seconds, and that pause is the point.** The step exhausts
two retries (one second apart) before failing, so don't click through the delay
expecting an instant result. Don't promise per-attempt rows in the trace either —
a step records one span for its final outcome, so the visible evidence is the
delay and the error, not an attempt counter.

**Approve within the hour.** The tour's approval carries `expires_in_seconds:
3600`. If you seed it, break for lunch, and come back, the approval is expired
and rejects on decision. Re-seed with reset instead.

## If you have 30 more seconds

Two cheap additions, both on a selected run in the trace inspector:

- **Replay** re-runs the exact recorded input against the current workflow.
- **Compare with…** diffs two runs step by step.

Skip both if you're tight. The regression diff is the stronger version of the
same idea.
