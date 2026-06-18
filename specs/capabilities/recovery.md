# Recovery engineer: diagnose-then-delegate failure recovery

> The control-plane-era recovery agent. Per [`../_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md): recovery is a **meta-agent that diagnoses a failure (grounded), then delegates the fix** to the DLT / DBT / SQL engineer. Resolves the **orphaned recovery POC** (the AI-map finding): the shipped `recovery/agent.py` targets the now-retired `carve el deploy` DDL flow — this spec retargets it to `run.failed` + delegation and drops the dead deploy invocation contexts.

## Status

- **Status:** Drafting
- **Depends on:** [harness](./harness.md) (subagent + delegation + grounded tools), [extensibility](./extensibility.md) (declarative agent), [runtime](./runtime.md) (the `run.failed` event, retries, auto-pause), [dlt-engineer](./dlt-engineer.md) / [pipelines](./pipelines.md) (the engineers it delegates fixes to), [deploy](./deploy.md) (`resolved_by_deploy_id`).
- **Blocks:** nothing (a leaf capability). Reconciles the shipped `src/carve/core/agents/recovery/`.

## Goal

When a scheduled run fails (retries exhausted), Carve **diagnoses the failure with grounded evidence and proposes a reviewable fix** — without ever writing to prod autonomously. Concretely:

1. **Trigger:** the `run.failed` event (retries exhausted, per spec 07). Per-pipeline opt-out (`[recovery] enabled = false`); a daily LLM cost cap.
2. **Diagnose:** the recovery engineer (a subagent) classifies the failure on **dlt's real exception hierarchy** and computes a per-resource **schema diff** (dlt's destination-side cached schema vs. the current source schema), grounded in real tool output — never a guessed cause.
3. **Delegate the fix:** for a code-fixable failure, recovery **delegates to the relevant engineer** (DLT/DBT/SQL) via the harness `delegate` tool; that engineer produces a normal, reviewable **Plan** (file diffs) — which flows through `plan → build → deploy/PR` like any other change. **Human-in-the-loop is a hard invariant** (PRD §6.3); Carve never auto-deploys a fix.
4. **Record + pause:** an **`Investigation`** row captures the diagnosis + proposed plan; the schedule auto-pauses (`paused_by = recovery`, unless a user already paused it) and **auto-resumes** when the resolving deploy lands (only if still recovery-paused).

## Out of scope

- **Auto-deploying fixes** without human review (crosses the no-autonomous-writes line; may revisit post-v0.1 for trivial type-widening with explicit allowlisting).
- **Cross-pipeline failure correlation** / an "incident commander" (deferred).
- **The dlt exception adapter's exhaustive coverage** — v0.1 pins the common categories (below); novel classes are surfaced, not auto-fixed.

## Reconciliation with the shipped POC

`src/carve/core/agents/recovery/` exists (P1-09) but is built around the **retired** `carve el deploy` DDL flow. This spec:

- **Drops** `DeployPreflightInvocation`, `DeployVerifyInvocation`, `DeployDdlApplyInvocation` (the el-deploy DDL contexts — gone with Wave 2's deploy retirement; dlt owns destination schema).
- **Retargets** the `ElRun`/run-failure path to the `run.failed` event from the runtime (spec 07).
- **Adds** the diagnose-then-**delegate** model (the POC diagnosed in isolation; now it delegates the fix to a domain engineer subagent).
- **Replaces** the message-string failure classification with the **dlt exception-class** classification (the real classes + adapter — see below).

## Behavior

### The Investigation entity

```
investigations(
  id PK UUID, triggering_run_id FK, pipeline FK, target,
  diagnosis_md, category,                 -- the classified failure category
  proposed_plan_id NULL,                  -- forward-declared FK to plans
  status,                                 -- proposed | acknowledged | resolved | dismissed
  resolved_by_plan_id NULL,               -- final merged plan in the refinement chain (UC4); FK to plans
  resolved_by_deploy_id NULL,             -- forward-declared FK to deploys (spec 14)
  recurring_run_ids JSONB,                -- dedup: same error+pipeline within a window appends here
  tenant_id BIGINT NOT NULL DEFAULT 1, created_at, resolved_at NULL
)
```

Forward-declared nullable FKs (plans exist; deploys per spec 14) follow the same pattern as the deploys row.

### Trigger → diagnose → delegate

1. **Trigger.** On `run.failed` with `retries_exhausted=true` (spec 07), the runtime invokes the recovery engineer — unless `[recovery] enabled = false` for the pipeline or the **daily cost cap** (`[recovery] daily_token_budget_usd`, default $5) is spent (then: log only, no diagnosis until reset).
2. **Auto-pause.** The runtime's `auto_pause_recovery` mutator (spec 07) sets the schedule `paused, paused_by = 'recovery'` immediately — **unless a user has already paused it** (`paused_by = 'user'`), in which case the human's pause is left untouched and only the diagnosis proceeds. A `schedule.paused` event fires with `source = 'recovery'`. (There is no `paused-by-code`: `[seed_schedule]` cannot seed a pause — spec 08 — so the only origins are `user` and `recovery`.)
3. **Diagnose (read-only, grounded).** The recovery engineer runs in a **read-only permission mode** (spec 15): it reads the failed run's logs, classifies the exception (`classify.py`), and computes a per-resource **schema diff** (`schema_diff.py` — dlt's destination cached schema vs. the current source schema). It **degrades gracefully**: no cached schema → "manual review required," surface logs, no proposed plan.
4. **Record.** An `Investigation` row is written (`status=proposed`) with the markdown diagnosis + category; an `incident.diagnosed` event fires (Slack/webhook).
5. **Delegate the fix (when code-fixable).** Recovery calls `delegate(<engineer>, fix_task, context)` — DLT engineer for a dlt-source fix, DBT engineer (v0.2) for a model fix, SQL specialist for a query fix. The engineer returns a **proposed Plan** (file diffs) linked to the Investigation (`proposed_plan_id`). **No write to prod** — the Plan is reviewable and flows through the normal `build → deploy/PR` path.
6. **Resolve.** When the resolving deploy lands (the deploy carries the `investigation_id`, spec 14), the Investigation → `resolved` (`resolved_by_deploy_id`) and the schedule **auto-resumes only if it is still `paused_by = 'recovery'`** (`auto_resume_recovery`, spec 07). If a user paused it in the interim (`paused_by = 'user'`), auto-resume is **suppressed** — the Investigation still resolves, but the schedule stays paused until a human resumes it; `schedule.resumed` fires only when an auto-resume actually happens. If the analyst dismisses ("won't fix"), → `dismissed`.

### Classification (grounded in dlt's real hierarchy)

`classify.py` unwraps the top-level `PipelineStepFailed` to its `.exception` and matches on dlt's real classes (the corrected names this project pinned), via `isinstance` on the terminal/transient base classes so dlt leaf renames don't break it:

| Category | dlt evidence | Propose fix? → delegate to |
|---|---|---|
| Schema-contract drift (column add/remove, type change) | `DataValidationError` (`.schema_entity`, `.contract_mode`) | yes → **DLT engineer** (add/remove column, relax contract, type hint) |
| Terminal load failure (NOT NULL, PK conflict, bad type) | `LoadClientJobFailed` | no — data-quality; surface logs |
| Missing relation / destination scaffolding | `DatabaseUndefinedRelation` | partial → **SQL specialist** (provision the missing container) |
| Transient (network/rate-limit/timeout) | `DestinationTransientException` + retry exhaustion | no fix — recommend retry tuning |
| Destination outage (≥2 pipelines failing) | connection errors across pipelines | no — infra; no pause |
| Credentials expired/revoked | source-side HTTP 401/403 (no native dlt class) | no — instruct credential refresh; never touches secrets |
| Novel / unclassified | anything outside the set | no — record logs; analyst owns it |

### CLI

```
carve investigations list [--status proposed|resolved|dismissed] [--since 7d]
carve investigations show <id> [--all-runs]      # diagnosis + proposed plan diff + recurring runs
carve investigations dismiss <id> --reason "..."
```

REST/MCP parity per specs 09/10. Recurring-run display capped (10 + "… and N more").

## Tests

- **Unit (classify):** real dlt exceptions (`DataValidationError`, `LoadClientJobFailed`, `DatabaseUndefinedRelation`, transient/terminal) map to the right categories; `PipelineStepFailed` is unwrapped; an auth 401/403 with no dlt class is classified `credentials`.
- **Unit (schema diff):** per-resource add/remove/type-change detected against a fixture cached schema; missing cached schema → graceful "manual review."
- **Integration (delegate):** a `run.failed` with a schema-contract drift → diagnosis → `delegate(dlt-engineer, …)` → a reviewable Plan linked to the Investigation; **no autonomous write to any target** (asserted).
- **Integration (auto-pause/resume):** failure → `paused_by = recovery`; a deploy carrying the `investigation_id` → Investigation `resolved` + schedule auto-resumes. Plus the **origin gate**: a user pause after the auto-pause (`paused_by = user`) suppresses the auto-resume on deploy (Investigation still `resolved`, schedule stays paused); and an auto-pause attempt on an already-user-paused schedule leaves it `paused_by = user`.
- **Unit (cost cap / opt-out):** budget exhausted → no diagnosis (log only); `[recovery] enabled = false` → no invocation.

## Acceptance

- A failed scheduled run produces a grounded `Investigation` (real dlt category + schema diff or graceful degradation), an `incident.diagnosed` event, and a `paused_by = recovery` schedule.
- For a code-fixable failure, recovery **delegates** to the right engineer and surfaces a **reviewable Plan** — never deploying autonomously.
- The resolving deploy auto-resolves the Investigation and auto-resumes the schedule — **unless a human paused it in the interim**, in which case the Investigation still resolves but the human's pause is preserved.
- The shipped POC's retired deploy invocation contexts are gone; classification uses dlt's real exception classes.
- `carve investigations list/show/dismiss` (+ REST/MCP) work; the daily cost cap + per-pipeline opt-out hold.

## Design notes

- **Why diagnose-then-delegate (vs. one recovery agent that also fixes)?** The fix belongs to the domain expert. Recovery's job is accurate diagnosis + routing; the DLT/DBT/SQL engineer (with its tools, conventions, and verification loop) writes the actual fix. This is the orchestrator pattern applied to failures, and it reuses every engineer's quality machinery.
- **Why human-in-the-loop is hard?** The PRD's no-autonomous-writes-to-prod invariant. Recovery proposes; a human reviews; the fix ships through the audited `plan → build → PR → merge → deploy` path. The hosted product's approval workflows extend this, never replace it.
- **Why classify on exception *classes* (not messages)?** The POC's message-regex was brittle. dlt's real hierarchy + `isinstance` on terminal/transient base classes is robust to leaf renames; the adapter is the one place that knows dlt's exception shapes.
- **Why an Investigation entity (vs. child Run rows)?** The POC reused `runs.parent_run_id`; the control-plane-era model needs a first-class record carrying the diagnosis, the proposed plan, the resolving deploy, and dedup — and `resolved_by_deploy_id` ties into the linked-PR deploy (spec 14).

## Open questions

- **Investigation migration number.** *Implementation default.* Next sequential; `down_revision` = current head at build time.
- **`target` source on the Investigation.** *Implementation default.* From the failed run's target.
- **Delegation in the runtime context → RESOLVED.** Recovery runs inside `carve serve`; it `delegate`s **synchronously** and persists the proposed Plan (no live worker contention) — consistent with spec 15's v0.1 sequential + sync execution model.
