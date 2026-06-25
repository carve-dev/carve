# Plan / Build: the change lifecycle and the Plan & Build entities

> **The durable design of record for Carve's central lifecycle** — `plan → build → (run / deploy)`, modeled on `terraform plan`/`apply`. A **Plan** is a reviewable, durable artifact (task graph, file diffs, **cost + runtime estimate**, impact analysis, config hash); a **Build** materializes an approved Plan into files (a `manifest`, `current_build_id`). This capability owns the **Plan/Build entities, their persistence, the `plan`/`build`/`plan-and-build` verbs, `--refine` chaining, and the config-hash drift check.** The *mechanics* of producing a plan (the orchestrator delegating + synthesizing) are the [harness](./harness.md)'s; this spec owns the **entity, the synthesis-into-one-reviewable-Plan, the lifecycle, and the safety net.** *Phasing annotation:* the core (plan/build/refine/run separation) **shipped in M1.1**; this spec is its durable design of record and the home for its forward evolution (the dbt-build path, richer impact analysis).

## Status

- **Status:** Drafting (durable design for shipped + evolving behavior)
- **Depends on:** [harness](./harness.md) (the orchestrator that delegates + returns usage/cost per subagent), [state-store](./state-store.md) (the `plans`/`builds` tables + the `.carve/plans/<id>.json` artifacts), [layout](./layout.md) (the `config_hash` over `carve.toml` + component refs), [pipelines](./pipelines.md) / [dlt-engineer](./dlt-engineer.md) / [dbt-engineer](./dbt-engineer.md) (the subagents whose authored diffs a Plan composes; the Build manifest they fill).
- **Used by:** [deploy](./deploy.md) (consumes the Build manifest + `config_hash`), [rest-api](./rest-api.md) / [mcp-server](./mcp-server.md) (expose `/plans` + `/builds`; this spec owns the services behind those routers), [runtime](./runtime.md) (a run executes a pipeline materialized by a Build), [recovery](./recovery.md) (a fix surfaces as a proposed Plan).
- **Lineage:** the plan/build/run separation shipped in **M1.1** ([`../milestone-1.1-followups/06-plan-build-run-separation.md`](../milestone-1.1-followups/06-plan-build-run-separation.md)). This capability spec is the durable design that was missing under the three-tier model.

## Goal

Own the change lifecycle as a first-class, reviewable thing: **`carve plan` produces a durable, inspectable Plan** (no files written); **`carve build` materializes it** (files written, not deployed); the two are decoupled so a Plan can be reviewed, refined, diffed, and built later — with a **config-hash drift check** that refuses to build a Plan against config that moved underneath it.

## Out of scope

- **Authoring** the contents of a plan — the [dlt-engineer](./dlt-engineer.md) / [dbt-engineer](./dbt-engineer.md) / [pipeline engineer](./pipelines.md) write the code; this spec composes their results into a Plan and records the Build.
- **The orchestrator loop / delegation mechanics** — [harness](./harness.md). This spec consumes the harness's per-subagent `DelegationResult` (usage/cost) to roll up the Plan's cost.
- **Running** a built pipeline ([runtime](./runtime.md)) and **promoting** it ([deploy](./deploy.md)).
- **The raw table DDL** — [state-store](./state-store.md) owns the `plans`/`builds` table creation; this spec owns their *semantics*.

## Behavior

### The Plan entity

A Plan is a durable artifact persisted to `.carve/plans/<plan_id>.json` **and** indexed in the `plans` table:

- **Goal** (the user's request), **task graph** (the steps the orchestrator decomposed it into), **file diffs** (what each subagent would write), **expected effects / impact analysis** (which pipelines/tables/downstream are affected — leans on [lineage](./lineage.md) investigation).
- **Cost + runtime estimate.** The Plan surfaces the **exact LLM cost** (known precisely — summed from each subagent's `DelegationResult.usage`/`cost_usd`, per [harness](./harness.md)) and an **estimated runtime** (first run vs. subsequent — e.g. "~25 min first load / <1 min incremental"), composed from the engineers' `expected_outputs`. **No warehouse-dollar estimate** (we can't know it precisely — UC1 resolved).
- **`config_hash`** over `carve.toml` + the resolved component refs at plan time — the drift anchor.
- **`parent_plan_id`** for refinement chains; **`carve_version`**; **`status`**; **`expires_at`** (Plans expire, default 24h).

### Plan synthesis (the behavior with no prior owner)

`carve plan "<goal>"` runs the [orchestrator](./harness.md): classify → delegate to the right subagents (each authoring its slice + returning a verified diff + usage) → **synthesize one reviewable Plan**: merge the file diffs, roll up the exact LLM cost, compose the runtime estimate, and render the impact analysis. The Plan is returned to the surface (CLI/chat/REST) for review; **no files are written.** `carve plan --refine <plan_id> "<feedback>"` produces a child Plan (`parent_plan_id` chain); `carve plan --pipeline <name> "<change>"` scopes to an existing pipeline.

### The Build entity + materialization

`carve build <plan_id>` materializes an approved Plan's task graph into files (dlt code, `pipelines/<name>.toml`, dbt models when the [dbt-engineer](./dbt-engineer.md) is in play), emits the `post_build` hook ([extensibility](./extensibility.md)), and records a **`Build`** row with a **`manifest_json`** listing every file written. A pipeline's **`current_build_id`** points at its most recent successful Build (what [deploy](./deploy.md) promotes, what [runtime](./runtime.md) runs). Build is **idempotent** (re-building the same Plan against unchanged config is a no-op). `carve plan-and-build "<goal>"` is the one-shot convenience (plan, then immediately build — for trusted/CI flows).

> **Updated during implementation (2026-06-25):** the `post_build` **emitter is owned by this capability's builder** and is **wired in plan-build Unit 2** (the live orchestrator/synthesis unit). [Extensibility](./extensibility.md) shipped the *subscription* seam (`HookRegistry`/`events.py`); `POST_BUILD` is in `DEFERRED_EMITTER_EVENTS` so `HookRegistry.emit` deliberately *raises* until an owner fires it (the extensibility delivery spec assigned the emitter to "build/pipelines = Incr 3"). Unit 1's builder records the `Build`/`current_build_id`/idempotency but does **not** yet emit `post_build`; Unit 2 lifts `POST_BUILD` out of the deferred set and fires it here. See [DELIVERY.md](../DELIVERY.md) → *Current state* for the recorded ownership decision.

### Config-hash drift check (the safety net)

Every Plan carries the `config_hash` it was generated against. **Build refuses to run a Plan whose `config_hash` no longer matches current config** (`carve.toml`/component refs moved since plan time) — exit `3`, with a clear "re-plan against current config" message. Deploy ([deploy](./deploy.md)) does the analogous pre-flight (exit `4` on target drift). This is the per-verb hash gate (ARCHITECTURE §7.6) that keeps "plan now, build later" safe.

## Tests

- **Unit (Plan entity):** a plan persists to `.carve/plans/<id>.json` + a `plans` row with task graph, file diffs, exact LLM cost, runtime estimate, `config_hash`, `expires_at`; an expired plan is rejected by build.
- **Unit (synthesis/cost):** the Plan's cost equals the sum of the subagents' `DelegationResult` usage; the runtime estimate composes from `expected_outputs`; **no warehouse-dollar figure** is emitted.
- **Unit (refine chain):** `plan --refine` sets `parent_plan_id`; the chain is walkable.
- **Integration (build materializes):** `carve build <plan_id>` writes exactly the Plan's file set, records a `Build` with `manifest_json`, updates `current_build_id`, emits `post_build`; re-building unchanged config is a no-op. *(The `post_build` emit lands in Unit 2 — see the §"The Build entity" callout; Unit 1's idempotency/manifest/`current_build_id` bullets are covered today.)*
- **Integration (drift):** mutating `carve.toml` after plan then `carve build <plan_id>` fails with exit 3 (`config_hash` mismatch) and a re-plan message.

## Acceptance

- A Plan is a **durable, reviewable artifact** (task graph + file diffs + exact LLM cost + runtime estimate + impact analysis + `config_hash`), persisted to disk + the `plans` table, refinable via `--refine`, expiring by default.
- **`build` materializes a Plan into files** (recording a `Build` manifest + `current_build_id`), emits `post_build`, and is idempotent. *(The `post_build` emit is this capability's responsibility against extensibility's shipped subscription seam, wired in Unit 2 — see the §"The Build entity" callout.)*
- **The config-hash drift check** refuses to build a Plan against drifted config (exit 3).
- The `plan` / `build` / `plan-and-build` verbs + `/plans` + `/builds` services are owned here (the [rest-api](./rest-api.md) routers wire onto them).

## Design notes

- **Why a dedicated capability?** plan/build is the spine of every change (the terraform-`plan`/`apply` model the whole product is built on), with its own entities, persistence, drift logic, refine chains, and two verbs + a REST/MCP surface — far too central and cross-cutting to live as an annotation, yet it had no home (only the shipped M1.1 work order). The audit found rest-api and state-store both explicitly pointing ownership elsewhere.
- **Why synthesis lives here, not in harness.** The harness owns *how* the orchestrator delegates (the loop, the gate, `DelegationResult`). Assembling those results into one reviewable Plan with cost/runtime/impact is a *lifecycle* concern — the deliverable, not the mechanism.
- **Why exact LLM cost but no warehouse estimate.** Carve knows token spend precisely; warehouse compute depends on data volume/warehouse size Carve can't predict. Honesty over a fake number (UC1).

## Open questions

- **Impact-analysis depth.** How rich the initial impact analysis is (file diffs + directly-affected pipelines, vs. a fuller downstream blast-radius via [lineage](./lineage.md) investigation) — a phasing call for [DELIVERY](../DELIVERY.md).
- **Runtime-estimate source.** The first-run/subsequent estimate needs the engineers' `expected_outputs` to carry duration hints; confirm that contract with [dlt-engineer](./dlt-engineer.md)/[dbt-engineer](./dbt-engineer.md).
