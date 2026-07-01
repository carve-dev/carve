# Observability: agent/run telemetry, metrics rollups, OpenTelemetry export

> **The recording + reporting + export surface for everything Carve does.** Every run, step, **agent invocation, and skill call** is recorded; `carve metrics` rolls those up (token→$, run success/failure, per-agent usage); and an optional **OpenTelemetry/OTLP** exporter emits a trace-per-run. This capability owns the **agent-telemetry tables** (`agents`, `agent_invocations`, `skill_calls`) — whose migration was previously orphaned (every agent writes them; no spec created them) — the **metrics aggregation service** behind `carve metrics` / `GET /metrics/*`, and the **OTel export** (which had *zero* prior home).

## Status

- **Status:** Drafting
- > **Recording + `carve metrics` core landed (2026-06-30).** Observability is one of Increment 4's two capabilities; it ships in slices. **This slice shipped the recording substrate + the `carve metrics` rollups:** migration `0012_observability_telemetry` (the three tables — `agents` / `agent_invocations` / `skill_calls`); the **recording contract + live wiring** (a `RecordingObserver` implementing the harness `AgentObserver` protocol, writing through a `TelemetryRepo`, wired at the `delegation_run.py` call-site and threaded from the two production `run_engines` callers — `builder.py` BUILD and `planner.py` PLAN — as **best-effort** telemetry that never blocks a delegated run); and **`carve metrics costs|runs|agents`** backed by the `MetricsRollups` service. **The design refinement this slice made** (from the reviewers): all four *external* correlation ids (`run_id`/`plan_id`/`build_id`/`ask_id`) shipped as **nullable no-FK recording pointers**, not FKs — see §"The recording contract + tables". All four reviewers (security / python / agent-loop / qa) PASS after 2 fix iterations; ruff / mypy --strict / 2293 pytest green. This satisfies Increment 4's **`carve metrics`** exit criterion. **DEFERRED, each with a home:** the **OpenTelemetry/OTLP export** → a follow-up **otel slice** (resolving the Phasing open question below); the **`GET /metrics/*` REST routers** → **Increment 5 / [rest-api](./rest-api.md)** (the `MetricsRollups` service is built as their seam); and **telemetry-table archival/retention** (`agent_invocations`/`skill_calls` grow unbounded) → a future **archiver-extension slice**. Status stays **Drafting** — those slices remain.
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

> **Updated during implementation (2026-06-30):** the four *external* correlation ids on `agent_invocations` — `run_id`/`plan_id`/`build_id`/`ask_id` — shipped as **nullable columns with NO FK constraint** (recording pointers), not FKs; only the two *telemetry-internal* parents are enforced FKs. This is the literal expression of the "orphaned telemetry recorded by value" situation this capability owns. Rationale in "Why the external ids carry no FK", below.

- `agent_invocations(id PK, agent_name FK→agents.name, run_id NULL, plan_id NULL, build_id NULL, ask_id NULL, tokens_input, tokens_output, cost_usd, duration_ms, status, started_at, finished_at)` — one row per subagent invocation, written as the [harness](./harness.md) delegates. `id` is an **app-generated `String`** minted at invocation-open (mirrors `runs`/`step_runs`) so `skill_calls` reference it without a mid-recording flush. The four external ids are **nullable no-FK recording pointers**; the only enforced FK on the table is the telemetry-internal `agent_name → agents.name`.
- `skill_calls(id PK, agent_invocation_id FK→agent_invocations.id, skill_name, input_hash, output_size, result_too_large BOOL, pages_walked INT NULL, duration_ms, started_at, finished_at)` — one row per skill/tool call, with the §6.4 bounded-result signals. `id` is `BIGSERIAL` (append-only child; mirrors `events`/`logs`/`schedule_changes`).

The **recording contract** (what the harness/agents must emit per invocation + per skill call) is defined here so it isn't reinvented per agent spec; agents just emit, this capability persists + aggregates.

**Why the external ids carry no FK.** Correlation still resolves **by value** (including into `runs_archive` — same id) — the enforcement is dropped, not the pointer — and the reasons are uniform:

- **`run_id` — archiver-safety.** An FK child would block the runtime archiver's aged-out `runs` DELETE (`IntegrityError` → batch rollback), silently halting `runs` archival forever. The id value is kept (not `ON DELETE SET NULL`), so post-archival correlation to `runs_archive` still resolves.
- **`plan_id` / `build_id` — record-before-parent ordering.** The engineers record with recording live *before* the `plans` row is persisted (and the `builds` row lands only after the review fan-out passes), so an FK would `IntegrityError` at `open_invocation` commit time and silently drop the whole plan-/build-path invocation + its skill calls. The id is stamped now; the parent row lands later carrying the same id.
- **`ask_id` — no parent table yet.** The `asks` table ships with [ask](./ask.md) (Increment 5); until then the correlation is recorded, uncontrolled.

The only enforced FKs are the two internal parents (`agent_invocations.agent_name → agents.name`, `skill_calls.agent_invocation_id → agent_invocations.id`), which always exist first.

### The recording seam — how invocations get recorded

> **Updated during implementation (2026-06-30):** the "instrumentation hook is wired here" ([DELIVERY](../DELIVERY.md) Increment-4 Delta) shipped as the seam described here.

A **`RecordingObserver`** implements the harness `AgentObserver` protocol *plus* an explicit `begin_invocation` / `end_invocation` lifecycle, writing through a **`TelemetryRepo`** (a sync `session_factory` repo, mirroring the scheduler's `Schedules`). It is wired at the single delegation call-site (`delegation_run.py`) and threaded from the two production `run_engines` callers via a public `Repository.session_factory` — **BUILD** (`builder.py`, correlating `run_id` + `plan_id`, `build_id=None` because the `Build` row lands only after review) and **PLAN** (`planner.py`, correlating `plan_id`). Per invocation: `begin_invocation` opens the `agent_invocations` row before `delegate()`; each `on_tool_result` writes one `skill_calls` row; `end_invocation` finalizes tokens/cost/status (from the `DelegationResult`) + a call-site-timed `duration_ms` (the result carries no duration field). Token→USD reuses `pricing.compute_cost_usd` — no new price table.

Two load-bearing properties:

- **Best-effort, never a blocker.** An absent `session_factory` ⇒ `NullObserver` (behaviour byte-identical to before this wiring); a telemetry write failure is logged and swallowed, never propagating into (or failing) the delegated run.
- **Correlation rides the sync/sequential delegation invariant.** A single "current open invocation" cursor attributes each skill call correctly because `delegation.py` runs one child loop at a time. The lifecycle is hardened to an **id-based** contract (`begin_invocation` returns the id; `end_invocation` finalizes *that* id; a double-open logs a fail-loud warning) so a future nested `delegate` can't silently corrupt it.

### `carve metrics` — the rollup service

The aggregation behind `carve metrics costs|runs|agents` (and `GET /metrics/{costs,runs,agents}`):

- **costs** — token→USD rollup (per model price) over a window (`--since`). *(Shipped: tokens→USD only, exact via `pricing.compute_cost_usd`; warehouse-credit accounting stays deferred — see Open questions.)*
- **runs** — success/failure counts, median/p95 duration, by pipeline/target.
- **agents** — per-agent invocation counts, token + cost totals, success rate, skill-call mix.

This spec owns the aggregation queries; [rest-api](./rest-api.md) wires the HTTP surface; the CLI is reference.

### OpenTelemetry / OTLP export (optional)

> **Deferred during implementation (2026-06-30):** OTel/OTLP export did **not** ship in the recording + `carve metrics` slice; it is deferred to a follow-up **otel slice** (the Phasing open question's [DELIVERY](../DELIVERY.md) call, now made — it adds an SDK/exporter dependency, a run→step→invocation→skill_call span tree that depends on these recording tables existing first, and a stub-collector integration test). The design below is the target for that slice; nothing here is live yet.

Configured in `carve/runtime.toml` (`[observability.otel]`). When enabled, **each run emits a trace with one span per step** (proper parent/child: run → steps → agent invocations → skill calls), exported via **OTLP/gRPC or OTLP/HTTP**. Off by default; OSS-complete (no hosted dependency). This is the integration point for a team's existing observability stack (Datadog/Honeycomb/Grafana).

## Tests

> **Updated during implementation (2026-06-30):** the recording + rollups tests shipped (`tests/core/observability/{test_recording,test_rollups}.py`, `tests/cli/commands/test_metrics.py`, the `0012` migration test, plus the `runs`-archiver FK-footgun and record-before-parent regressions). The recording test's ask-link is asserted as the **nullable no-FK column** (the `asks` table ships in Increment 5). The **OTel integration test is deferred with the otel slice**.

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

> **Resolved during implementation (2026-06-30):** the two questions below were answered by the shipped slice (annotated inline); a third — telemetry-table retention — was surfaced during implementation and added.

- **Warehouse-cost accounting in `metrics costs`.** Token→USD is exact; warehouse credits depend on the dialect/backend exposing cost (Snowflake `QUERY_HISTORY` does — ties to [dbt-execution](./dbt-execution.md)/[sql](./sql.md)). How far to go initially vs. report tokens-only. **Resolved:** this slice does **tokens→USD only** (exact, via `pricing.compute_cost_usd`); warehouse-credit accounting stays deferred, flagged in-code (following `cost_rollup.py`'s "no fake warehouse figure" honesty precedent).
- **Phasing.** OTel export is plausibly later than the core recording + `carve metrics`; a [DELIVERY](../DELIVERY.md) call. **Resolved:** OTel is deferred to a follow-up **otel slice**; the recording + `carve metrics` core shipped first (Increment 4's exit criterion names only `carve metrics`).
- **Telemetry-table retention (surfaced during implementation).** `agent_invocations` / `skill_calls` are themselves *unarchived* and grow unbounded — the [runtime](./runtime.md) archiver ages out `jobs`/`runs`/`logs`/`step_runs`, not these. A future **archiver-extension slice** should add their `*_archive` clones + retention windows, archiving these children *before* the `runs` pass (FK-order-safe).
