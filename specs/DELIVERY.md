# Carve — Delivery plan

**What to build, in what order, given what's already built.** This is the temporal layer ([`_strategy/2026-06-spec-structure.md`](./_strategy/2026-06-spec-structure.md)): it sequences work into dependency-ordered, foundation-first increments and carries the phase/increment identity (and the release tag at the end). The durable design lives elsewhere and is **not** organized by phase — [`PRD.md`](./PRD.md) (what/why/who), [`ARCHITECTURE.md`](./ARCHITECTURE.md) (the technical model), and the capability specs (under [`capabilities/`](./capabilities/); they describe *how a capability works*, version-independently).

This is a **living, delta-aware** document. It plans *changes and additions* to the current codebase, not greenfield builds. As increments land, update the *Current state* section and check off exit criteria; as priorities shift, re-sequence increments here — without touching the capability specs. It covers the **whole lifecycle** — the initial foundation build *and* ongoing change (bugs, enhancements, new capabilities) after it — under one structure: **Current state** (the perpetual delta baseline) → **Increments** (the initial build; becomes the build log) → **Backlog** (ongoing work that needs sequencing). How any individual change flows (the bug-vs-change rule, spec-first) is [`_strategy/2026-06-change-lifecycle.md`](./_strategy/2026-06-change-lifecycle.md).

> **Note.** Specs live in [`capabilities/<area>`](./capabilities/) (durable design); increments below reference them by capability name. `DELIVERY.md` — not `capabilities/README.md` — is the source of truth for sequencing and scope. The concrete **file manifest for each slice is not stored** — it is generated at build time (see *How a slice is built*, below).

---

## How to read an increment

Each increment is a shippable, dependency-respecting slice:

- **Goal** — the user-visible capability the increment delivers.
- **In scope** — the capability slices, each pointing at its design spec.
- **Depends on** — increments/code that must exist first.
- **Delta** — what's *new* vs. what *modifies* already-shipped code (the delta-aware part).
- **Exit criteria** — how we know it's done.

**How a slice is built.** The build (`/build-spec`'s planning stage) takes a slice = (capability spec × this increment) and **generates a *delivery spec*** at build time: it reads the spec as the design reference, inspects the **current codebase**, and emits the concrete, delta-aware file manifest (*create / modify*) plus the increment's slice of the spec's Acceptance + Tests as the bar. The manifest is computed, never stored — so it is always correct against what's already built (see [`_strategy/2026-06-spec-structure.md`](./_strategy/2026-06-spec-structure.md) → *The delivery spec*).

---

## Current state (the delta baseline)

Shipped and in `src/` — every increment plans *against* this:

- **M1 — walking skeleton.** CLI foundation (Typer), config loader, state store, the Anthropic agent loop, the Python step + `LocalVenvRunner` subprocess primitive, the Snowflake connector. The smallest end-to-end loop.
- **M1.1 — lifecycle + UX.** `carve init` templates, Claude-subscription OAuth, dotenv autoload, plan progress, agent-prompt tightening, the **plan / build / run** separation, run-retry-permits-redo. (The OAuth and the plan/build/run separation are the shipped cores of [model-auth](./capabilities/model-auth.md) + [plan-build](./capabilities/plan-build.md) — Increment 0 formalizes them.)
- **Spec 01 — state store → Postgres.** Landed (SQLite retired; Postgres baseline + the six audited migrations). Followups landed: the M1 test sweep and `DATABASE_URL` precedence. ~300 tests passing. **Increment 0 (state-store formalization) complete 2026-06-18** — spec reconciled to the shipped code.

- **Layout (Increment 1) — control-plane `carve.toml`.** Landed 2026-06-18: the `[components.<name>]` schema (transport-validated `url`/`ref`/`branch`), `ProjectPaths`, the component locator (`resolve_component` / `discover_components` / `workspace_dirname`), the git workspace cache (ref-pin, credential redaction, hardened env, bounded `timeout`), the provenance reader, and the `Workspace` model + migration `0007`. 879 tests passing.

- **Harness (Increment 1) — the Claude-Code-style agentic engine.** Landed 2026-06-19: sync sequential subagent `delegate` (`delegation.py`, `DelegationResult`, mode-clamp to `min(parent, capability)`, context isolation, harness-tracked `files_changed`); terminal-grade tools (`tools/fs_tools.py` edit/create_file with re-read-at-apply TOCTOU, `bash_tool.py`, `search_tools.py`, `web_tools.py`, `todo_tool.py`); the single pre-execution permission gate (`permissions/gate.py`, gate-first in `loop.py:_execute_tool_calls`, `grant ∩ mode` attenuation, fail-closed prompt) over the per-mode policy floor (`permissions/policy.py` with `_ALWAYS_DENY` + `DANGEROUS_BASH_FLAGS`) and the shared secret-path deny-list (`tools/secrets_denylist.py`); the bounded format-agnostic verification loop (`verification.py`); interrupt/cancel (`cancel.py`), steering (`steering.py`), and compaction (`compaction.py`). `loop.py` is a purely-additive MODIFY (sync preserved). Spec reconciled to the shipped code (minor inline drift only). Deferred follow-ups (non-blocking, in the harness spec's Open questions): grow the secret deny-list with the warehouse-cred surface, unicode-normalize the secret-name compare, and add dedicated `web_fetch`/`web_search`/`todo` unit tests.

The rest of Increment 1 (packaging, extensibility, model-auth) and everything beyond is **designed but unbuilt**. The foundational AI spec **extensibility** has been adversarially reviewed + hardened. The full corpus is internally consistent under the control-plane + AI-harness model.

---

## Increment 0 — Baseline: formalize the shipped state store ✅ *(done 2026-06-18)*

**Goal.** Reconcile the shipped Postgres state store with its spec — the foundation every increment plans against.

**In scope**
- Postgres state store — [state-store](./capabilities/state-store.md) *(landed; spec reconciled — verified green against a live Postgres testcontainer)*

> **Re-slotted.** The other two M1.1-shipped capabilities first bucketed here — **model-auth** and **plan-build** — moved to where their declared deps are actually *rebuilt*: **model-auth → Increment 1** (it integrates with the rebuilt layout + harness), **plan-build → Increment 3** (its plan-synthesis rolls up the engineers' diffs/costs). Formalizing shipped code against the soon-to-be-replaced M1 shape is busywork; against the rebuilt foundation it's real.

**Depends on.** Current state only.

**Delta.** Verify/MODIFY against shipped code (the M1 fixture sweep + the three unit tests already landed). No new code — the spec was made honest (Status → Landed).

**Exit criteria.** ✅ The state-store spec matches the code; the full state/migration test surface is green against Postgres.

---

## Increment 1 — Foundation: control-plane layout + the AI harness

**Goal.** The structural + AI substrate everything runs on: a control-plane `carve.toml` that references components by name, and the Claude-Code-style harness (subagents, terminal tools, permission gate, verify-by-execution) with declarative extensibility.

**In scope**
- OSS packaging: bundled docker-compose Postgres + external-Postgres option — [packaging](./capabilities/packaging.md)
- Control-plane flat layout: `carve.toml` `[components.<name>]`, the component locator, repo topology, simple-mode convention discovery, the workspace cache — [layout](./capabilities/layout.md)
- **The agent harness** — subagent `delegate`, terminal tools (edit/bash/grep/web), the permission gate (modes + `allowed_paths` + bash sandbox + secret-deny), verify-by-execution, interrupt/TODO/compaction — [harness](./capabilities/harness.md)
- **Extensibility** — declarative agents (`carve/agents/*.md`), skill packs (`SKILL.md`), hooks (`hooks.toml`), MCP both directions, runtime grant attenuation — [extensibility](./capabilities/extensibility.md)
- **Model auth** *(formalize M1.1-shipped)* — `ANTHROPIC_API_KEY` + Claude-subscription OAuth, credential precedence, `carve auth login`, against the rebuilt harness (the consumer) + layout (`models.toml`'s config-bundle home) — [model-auth](./capabilities/model-auth.md)

**Depends on.** Increment 0 (the state store) + M1/M1.1 (the agent loop the harness wraps + the OAuth/API-key model-auth shipped in M1.1, which this increment formalizes).

**Delta.** 15 *wraps* the M1 agent loop (adds delegation, the gate, context management); it does not replace it. 16 is net-new. 03 introduces `carve.toml` as control-plane config (supersedes the M1 project-shaped config) + the locator (net-new). model-auth formalizes the M1.1 OAuth/API-key code against the rebuilt harness + layout (re-homing `models.toml` into the control-plane config) — verify + close the `auth login` CLI gap.

**Exit criteria.** A `carve.toml` with `[components.<name>]` resolves names to code (simple + multi mode); an agent runs under the permission gate with terminal tools and can `delegate` to a subagent that verifies by execution; a user-authored `carve/agents/*.md` overrides a built-in, attenuated to its mode; `carve auth login` + API-key precedence work against the rebuilt harness, with `models.toml` in the layout's config bundle.

---

## Increment 2 — Bootstrap & SQL: a scaffolded project + the dialect-aware tool

**Goal.** Scaffold a real project (greenfield or brownfield) with project memory, and stand up the dialect-aware SQL tool every engineer rides on.

**In scope**
- **`carve init`** — greenfield/brownfield across the Postgres × dbt × dlt × memory axes; renders the control-plane `carve.toml`; convention inference — [init](./capabilities/init.md)
- **Project memory** — `conventions.md` / `standards.md` / `decisions.md`, sidecars, `carve memory` surface — [memory](./capabilities/memory.md)
- The dialect-aware **SQL tool layer** — sqlglot validate/transpile, per-dialect introspection, role-gated exec (Snowflake + DuckDB first-class) + a thin SQL specialist — [sql](./capabilities/sql.md)

**Depends on.** Increment 1 (layout, harness, extensibility) + Increment 0 (the state store).

**Delta.** 05 *rewrites* the M1.1 init around the control-plane `carve.toml` + the four axes. 06 builds on 05's scaffolded memory files. 18 *generalizes* the M1 Snowflake-only `run_snowflake_query` + catalog skills into a dialect-aware layer (preserves the connector).

**Exit criteria.** `carve init` produces a working project (bundled or external Postgres) with memory scaffolding; brownfield init infers conventions and writes no `[components.*]` blocks in simple mode; the `sql` tool introspects a live warehouse on the read role.

---

## Increment 3 — Components: the AI authors, runs & composes dlt **and** dbt

**Goal.** The AI authors **both** dlt and dbt components (co-equal), runs them, provisions backends on demand, and composes components by name into a runnable pipeline DAG.

**In scope**
- The **DLT engineer** subagent (authors/runs dlt; native/REST/curated-library/MCP paths) + dlt-qa / dlt-security reviewers — [dlt-engineer](./capabilities/dlt-engineer.md)
- The **dbt engineer** subagent — authors/modifies dbt models, tests, sources; verifies via `dbt build`/`test`; + a dbt-qa reviewer — [dbt-engineer](./capabilities/dbt-engineer.md)
- **dbt execution backends** — local (bundled Fusion/dbt-core, or the team's own dbt) + managed (snowflake-native, dbt Cloud, remote), behind one step interface — [dbt-execution](./capabilities/dbt-execution.md)
- **connect** — AI-driven on-demand provisioning: engine install + pin, warehouse/source connect — [connect](./capabilities/connect.md)
- **Multi-step pipeline** composition: `pipelines/<name>.toml`, the step DAG executor (dlt/dbt/sql), `[seed_schedule]`, component-by-name, the definition reconciler, the **pipeline engineer**, `carve component(s)` graduation — [pipelines](./capabilities/pipelines.md)
- **Plan / build** *(formalize M1.1-shipped + complete)* — the Plan/Build entities + `plan`/`build`/`plan-and-build` verbs + `--refine` (shipped), the config-hash drift gate, and the plan synthesis that now rolls up the engineers' verified diffs/costs — [plan-build](./capabilities/plan-build.md)

**Depends on.** Increment 2 (init/memory/sql) + Increment 1 (harness, layout, extensibility).

**Delta.** 04 *replaces/generalizes* the M1 EL agent as a declarative subagent. **dbt-engineer is net-new — the exact parallel to the DLT engineer, co-equal from the start.** dbt-execution is net-new: it implements the `dbt` step against the StepExecutor protocol (the runtime's scheduler + worker-placement *dispatch* it in Increment 4). connect + dbt-execution are co-designed (the bundled-engine provisioning seam). 08 is net-new (the reconciler creates the `pipelines`/`schedules` tables). plan-build formalizes its M1.1-shipped lifecycle core + adds the config-hash drift check + the synthesis rollup, now that the dlt/dbt engineers produce the diffs/costs it composes.

**Exit criteria.** `carve plan "ingest Stripe, then stage it with dbt"` → the **DLT and dbt engineers** author + verify their components (`dlt pipeline run`, `dbt build`/`test`); `carve build` materializes them + a `pipelines/<name>.toml` referencing them by name; `carve pipelines validate` passes; first dbt use provisions + pins the engine via `connect`; `carve plan` rolls up exact LLM cost + a runtime estimate from the engineers' diffs, and `carve build` refuses a drifted plan (exit 3).

---

## Increment 4 — Runtime & telemetry: schedule, run, record

**Goal.** Schedule and run composed pipelines end-to-end on cron, with telemetry.

**In scope**
- The **runtime** — scheduler (reads the `schedules` table), Postgres job queue (optimistic claim), worker pool, heartbeats, reaper, archiver; the live `schedules` table + `carve schedule` mutation surface + `schedule_changes` audit; dispatches dlt/dbt/sql steps (incl. worker placement for dbt-execution's local backend) — [runtime](./capabilities/runtime.md)
- **Observability** — agent/run/step/skill telemetry tables, `carve metrics` rollups (token→$, run success/failure, per-agent usage), OpenTelemetry/OTLP export — [observability](./capabilities/observability.md)

**Depends on.** Increment 3 (08's pipeline definitions + reconciler the scheduler reads; dbt-execution's steps the runtime dispatches; the engineers whose runs observability records).

**Delta.** 07 *wraps* the M1 `LocalVenvRunner` in a scheduler + queue + worker layer; creates the runtime tables (jobs, workers, archives, events, **schedules**, schedule_changes); completes the worker-placement dispatch for dbt-execution's local backend. observability records over runtime's events + the harness's per-agent-invocation telemetry (the instrumentation hook is wired here); the `/metrics` REST surface lands in Increment 5.

**Exit criteria.** `carve serve` schedules + runs a composed dlt→dbt→sql pipeline on cron; `carve schedule pause/resume/set-cron` changes firing instantly, audited; `carve metrics` rolls up cost / runs / per-agent usage.

---

## Increment 5 — Interfaces & investigation: REST, MCP, UI, ask, lineage, search

**Goal.** Drive Carve programmatically, and investigate the project read-only.

**In scope**
- **REST API** — full CLI-surface coverage, auth, errors, pagination, streaming, webhooks (incl. `/metrics/*` onto observability) — [rest-api](./capabilities/rest-api.md)
- **MCP server** — auto-generated from REST; stdio + WebSocket — [mcp-server](./capabilities/mcp-server.md)
- **Static HTML UI** — regenerated per run; `carve docs serve` — [ui](./capabilities/ui.md)
- **The explorer (`ask`)** — read-only investigative subagent; citations — [ask](./capabilities/ask.md)
- **Lineage by investigation** — the `dlt_schema` reader skill; the explorer answers lineage via dbt manifest + dlt schema + code (no Carve store) — [lineage](./capabilities/lineage.md)
- **Semantic search** — the embedding index + `semantic_search` skill + `carve embeddings rebuild` — the fuzzy retrieval layer atop the deterministic ones — [semantic-search](./capabilities/semantic-search.md)

**Depends on.** Increment 4 (surfaces to expose) + Increment 3 (the dbt manifest lineage reads). 12/19/semantic-search need the harness (1) + SQL tool (2); 10/11 need 09; semantic-search needs ask + lineage.

**Delta.** Largely net-new surfaces over the increments 1–4 substrate. 12 subsumes the old ask-only guardrail into the `read_only` mode. 19 adds one skill (`dlt_schema`) + explorer guidance — no graph. semantic-search adds the embedding index + skill + rebuild command.

**Exit criteria.** Every CLI action has a REST + MCP equivalent (parity); `carve ask "where does X come from?"` returns a cited answer via investigation; `carve ask` resolves a fuzzy concept ("churn metrics") via semantic search; the static UI shows run history + per-run logs.

---

## Increment 6 — Deploy & recovery

**Goal.** Promote built code to prod, and auto-diagnose failures.

**In scope**
- **Deploy** — `carve deploy <pipeline>` configurable handoff (files/commit/push/pr, default pr); cross-repo linked PRs; pre-flight drift — [deploy](./capabilities/deploy.md)
- **Recovery engineer** — diagnose-then-delegate on retries-exhausted `run.failed`; the `Investigation` entity; auto-pause/resume gated by pause origin — [recovery](./capabilities/recovery.md)

**Depends on.** Increment 3 (deploy targets) + Increment 4 (07 `run.failed`, schedules auto-pause). 17 needs the harness/delegation (1) + the engineers it delegates to (3) + deploy (this increment, for the resolving-deploy → auto-resume link).

**Delta.** 14 *retires* the `carve el deploy` DDL-apply path; net-new handoff/linked-PR machinery + the `deploys` table. 17 reuses the M1 recovery POC's reconcilable parts; net-new `investigations` table + the diagnose-then-delegate flow (delegating dlt/dbt/sql fixes to the engineers).

**Exit criteria.** `carve deploy` opens a (linked) PR by default, each handoff depth working; a retries-exhausted failure produces a grounded `Investigation` + a reviewable fix Plan, auto-pauses the schedule, and the resolving deploy auto-resumes it (unless a human paused it).

---

## Increment 7 — Reference & initial release

**Goal.** Correct reference docs and tag the initial release — shipping **all 26 capabilities**: the full intent → plan → build → run → deploy → schedule loop for dlt **and** dbt.

**In scope**
- **Reference docs** — cli-reference / config-schema / glossary / governance kept in lock-step via completeness tests — [reference-docs](./capabilities/reference-docs.md) *(content rewritten 2026-06; this increment adds the completeness tests against built code)*
- **Release** — tag the initial release (the semver version is chosen at release time).

**Depends on.** Everything (reference derives from the built surface).

**Delta.** The reference content is already regenerated to the current model; this increment adds the build-time completeness tests (every Typer command in cli-reference; every init-scaffolded file in config-schema) and pins the few **planned** CLI commands by giving each an owning slice or cutting it.

**Exit criteria (initial release).** `carve init → plan → build (dlt + dbt) → run → deploy → scheduled-run-on-cron` works end-to-end against a real Snowflake account, and the same loop works via REST and MCP. Completeness tests green.

---

## Sequencing rationale

```
M1 / M1.1
   │
   ▼
Incr 0  state-store                                      (formalize the shipped state store -- done)
   │
   ▼
Incr 1  packaging · layout · harness · extensibility ·   (control-plane + AI foundation;
        model-auth                                        + formalize M1.1 model-auth)
   │
   ▼
Incr 2  init · memory · sql                              (scaffold a project + the SQL tool)
   │
   ▼
Incr 3  dlt-engineer · dbt-engineer · dbt-execution ·    (AI authors / runs / composes
        connect · pipelines · plan-build                  dlt AND dbt; + M1.1 plan-build)
   │
   ▼
Incr 4  runtime · observability                          (schedule / run / record)
   │
   ▼
Incr 5  rest-api · mcp-server · ui · ask · lineage ·     (interfaces + investigation)
        semantic-search
   │
   ▼
Incr 6  deploy · recovery
   │
   ▼
Incr 7  reference-docs + initial release tag             (all 26 capabilities)
```

- **Baseline first.** Incr 0 reconciles the shipped state store with its spec; the other M1.1-shipped capabilities (model-auth, plan-build) formalize alongside the rebuilt foundation they integrate with (model-auth in Incr 1, plan-build in Incr 3) rather than against the soon-to-be-replaced M1 shape.
- **Foundation before components.** The harness + control-plane layout (1) and the scaffold + SQL tool (2) gate everything AI- and component-shaped.
- **dlt and dbt are co-equal components (3).** Both are authored, verified-by-execution, and composed from the start — dbt is *not* deferred. Execution (dbt-execution) + on-demand provisioning (connect) land with them, before the scheduler, so a dbt step can run the moment it's composed.
- **Capability before interface.** Author / run / compose / schedule (3–4) before exposing over REST/MCP/UI + investigation (5).
- **Deploy + recovery after a pipeline can run** (they act on built/running pipelines).
- **Reference + release last** (derives from the built surface) — the initial release ships the whole loop, dlt and dbt alike.

## Backlog (post-release enhancements)

All 26 capabilities are placed in Increments 0–7 above. This is the living queue for work *after* the initial release — genuine enhancements deliberately scoped out of the first loop. They ride the change lifecycle ([`_strategy/2026-06-change-lifecycle.md`](./_strategy/2026-06-change-lifecycle.md)): a new spec or a spec edit, then `/build-spec`; small ones are GitHub issues, larger ones get an increment here.

- **Concurrent subagent fan-out** — the harness runs sequentially in the initial release; concurrency is a later enhancement ([harness](./capabilities/harness.md)).
- **Column-level lineage** — the explorer reads model SQL on demand today; column-level may arrive via the Fusion dbt engine ([lineage](./capabilities/lineage.md), [dbt-execution](./capabilities/dbt-execution.md)).
- **Custom step-type SDK + in-process custom-skill SDK** — built-in step types + MCP/`SKILL.md` skills ship first ([extensibility](./capabilities/extensibility.md)).
- **First-class BigQuery / Databricks / Redshift / SQL Server** — they work via sqlglot now; introspection hardening to first-class is later ([sql](./capabilities/sql.md)).
- **Multi-LLM providers** (OpenAI / Google) beyond Anthropic ([model-auth](./capabilities/model-auth.md)).
- **Multiple schedules per pipeline**, Salesforce/SaaS CDC sources, an OAuth side-channel browser flow, opt-in auto-deploy for trivial fixes, and further curated-connector-library waves.
