# Observability: agent/run telemetry, metrics rollups, OpenTelemetry export

> **The recording + reporting + export surface for everything Carve does.** Every run, step, **agent invocation, and skill call** is recorded; `carve metrics` rolls those up (token→$, run success/failure, per-agent usage); and an optional **OpenTelemetry/OTLP** exporter emits a trace-per-run. This capability owns the **agent-telemetry tables** (`agents`, `agent_invocations`, `skill_calls`) — whose migration was previously orphaned (every agent writes them; no spec created them) — the **metrics aggregation service** behind `carve metrics` / `GET /metrics/*`, and the **OTel export** (which had *zero* prior home).

## Status

- **Status:** Drafting
- **Depends on:** [state-store](./state-store.md) (these tables live in Postgres; this spec ships their migration), [runtime](./runtime.md) (the `events` stream + `runs`/`step_runs` it records over), [harness](./harness.md) (emits per-agent-invocation + per-skill-call telemetry as subagents run).
- **Used by:** [rest-api](./rest-api.md) (the `/metrics/*` routers wire onto this service), [reference-docs](./reference-docs.md) (`carve metrics` CLI), [ask](./ask.md)/[recovery](./recovery.md) (read telemetry for "agent took many tries" correlation).
- **Lineage:** net-new. Consolidates the **orphaned agent-telemetry tables** (ARCHITECTURE §9.5 — referenced by ask/dlt-engineer/etc., created by no spec) + the **circularly-unowned metrics router** + the **entirely unhomed OpenTelemetry export** (PRD §6.14).

## Goal

One home for "what happened, how much it cost, and how to export it." Record every run/step/**agent-invocation/skill-call**; aggregate into the `carve metrics` rollups (cost, runs, agents); and optionally emit OpenTelemetry traces — without scattering the recording contract across the agent specs or leaving the telemetry tables unowned.

## Out of scope

- **The run/step/job state machine + the `events` table** — [runtime](./runtime.md) owns those (this spec records *over* them and adds the agent/skill layer).
- **Webhook delivery** — [rest-api](./rest-api.md) owns `webhooks`/`webhook_deliveries` (event *delivery* to external URLs); this spec is *recording + rollups + OTel*, not webhook fan-out.
- **The hosted observability product** — the polished cloud dashboards, anomaly callouts, freshness monitoring are [hosted](./reference-docs.md) (commercial). This spec is the OSS recording + `carve metrics` + OTel substrate they build on.

## Behavior

### The recording contract + tables (owns the migration)

This spec creates and owns (one Alembic migration):

- `agents(name PK, model, system_prompt_path, allowed_skills JSONB, guardrails JSONB, specialization JSONB, source, created_at, updated_at)` — the registry projection of discovered agents ([extensibility](./extensibility.md) discovers them; this records them).
- `agent_invocations(id PK, agent_name FK, run_id FK NULL, plan_id FK NULL, ask_id FK NULL, build_id FK NULL, tokens_input, tokens_output, cost_usd, duration_ms, status, started_at, finished_at)` — one row per subagent invocation, written by the [harness](./harness.md) as it delegates.
- `skill_calls(id PK, agent_invocation_id FK, skill_name, input_hash, output_size, result_too_large BOOL, pages_walked INT NULL, duration_ms, started_at, finished_at)` — one row per skill/tool call, with the §6.4 bounded-result signals.

The **recording contract** (what the harness/agents must emit per invocation + per skill call) is defined here so it isn't reinvented per agent spec; agents just emit, this capability persists + aggregates.

### `carve metrics` — the rollup service

The aggregation behind `carve metrics costs|runs|agents` (and `GET /metrics/{costs,runs,agents}`):

- **costs** — token→USD rollup (per model price) + (where known) warehouse-credit accounting, over a window (`--since`).
- **runs** — success/failure counts, median/p95 duration, by pipeline/target.
- **agents** — per-agent invocation counts, token + cost totals, success rate, skill-call mix.

This spec owns the aggregation queries; [rest-api](./rest-api.md) wires the HTTP surface; the CLI is reference.

### OpenTelemetry / OTLP export (optional)

Configured in `carve/runtime.toml` (`[observability.otel]`). When enabled, **each run emits a trace with one span per step** (proper parent/child: run → steps → agent invocations → skill calls), exported via **OTLP/gRPC or OTLP/HTTP**. Off by default; OSS-complete (no hosted dependency). This is the integration point for a team's existing observability stack (Datadog/Honeycomb/Grafana).

## Tests

- **Unit (recording):** a delegated subagent run writes an `agent_invocations` row (tokens/cost/duration/status) + a `skill_calls` row per tool call, linked to the run/plan/ask; the migration creates all three tables.
- **Unit (rollups):** `carve metrics costs --since 7d` sums token→USD correctly; `metrics runs` computes success/failure + median duration; `metrics agents` aggregates per-agent usage.
- **Integration (OTel):** with `[observability.otel]` enabled, a run emits one trace, one span per step, correct parent/child nesting, exported to a stub OTLP collector; disabled → no spans, no overhead.

## Acceptance

- The `agents`/`agent_invocations`/`skill_calls` tables are **created and owned here** (no longer orphaned); every agent invocation + skill call is recorded against the run/plan/ask that triggered it.
- `carve metrics costs|runs|agents` returns correct rollups; the `/metrics/*` routers wire onto this service.
- OpenTelemetry export emits a trace-per-run (span-per-step) over OTLP when enabled, and is a no-op when off.

## Design notes

- **Why a dedicated capability?** The recording contract spans every agent, the rollups span every run, and OTel export had *no* home at all — three threads with one subject (telemetry in → metrics/traces out). Folding the tables into harness and the rollups into rest-api would re-scatter them and leave OTel homeless. One capability keeps the contract coherent.
- **Why it owns the orphaned tables.** `state-store` scopes itself to the M1 baseline tables and disclaims new ones; `runtime`/`ask` own *their* tables; the agent-telemetry tables fell through. Putting them with the rollups + export that read them is the natural home.

## Open questions

- **Warehouse-cost accounting in `metrics costs`.** Token→USD is exact; warehouse credits depend on the dialect/backend exposing cost (Snowflake `QUERY_HISTORY` does — ties to [dbt-execution](./dbt-execution.md)/[sql](./sql.md)). How far to go in v0.1 vs. report tokens-only.
- **Phasing.** OTel export is plausibly later than the core recording + `carve metrics`; a [DELIVERY](../DELIVERY.md) call.
