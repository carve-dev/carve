# Carve — Delivery plan

**What to build, in what order, given what's already built.** This is the temporal layer ([`_strategy/2026-06-spec-structure.md`](./_strategy/2026-06-spec-structure.md)): it sequences work into dependency-ordered, foundation-first increments and carries the version/phase identity. The durable design lives elsewhere and is **not** organized by phase — [`PRD.md`](./PRD.md) (what/why/who), [`ARCHITECTURE.md`](./ARCHITECTURE.md) (the technical model), and the capability specs (today under [`capabilities/`](./capabilities/); they describe *how a capability works*, version-independently).

This is a **living, delta-aware** document. It plans *changes and additions* to the current codebase, not greenfield builds. As increments land, update the *Current state* section and check off exit criteria; as priorities shift, re-sequence increments here — without touching the capability specs.

> **Note.** Specs live in [`capabilities/<area>`](./capabilities/) (durable design); increments below reference them by capability name. `DELIVERY.md` — not `capabilities/README.md` or `PROJECT_PLAN.md` — is the source of truth for sequencing and scope. The concrete **file manifest for each slice is not stored** — it is generated at build time (see *How a slice is built*, below).

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
- **M1.1 — lifecycle + UX.** `carve init` templates, Claude-subscription OAuth, dotenv autoload, plan progress, agent-prompt tightening, the **plan / build / run** separation, run-retry-permits-redo.
- **Spec 01 — state store → Postgres.** Landed (SQLite retired; Postgres baseline + the six audited migrations). Followups landed: the M1 test sweep and `DATABASE_URL` precedence. ~300 tests passing.

Everything else (specs 02–19) is **designed but unbuilt**. The foundational AI specs **15 (harness)** and **16 (extensibility)** have been adversarially reviewed + hardened. The full corpus is internally consistent under the control-plane + AI-harness model.

---

## Increment 1 — Foundation: Postgres, control-plane layout, the AI harness

**Goal.** The structural + AI substrate everything runs on: a control-plane `carve.toml` that references components by name, and the Claude-Code-style harness (subagents, terminal tools, permission gate, verify-by-execution) with declarative extensibility.

**In scope**
- Finish/confirm Postgres state store — [state-store](./capabilities/state-store.md) *(largely landed)*
- OSS packaging: bundled docker-compose Postgres + external-Postgres option — [packaging](./capabilities/packaging.md)
- Control-plane flat layout: `carve.toml` `[components.<name>]`, the component locator, repo topology, simple-mode convention discovery, the workspace cache — [layout](./capabilities/layout.md)
- **The agent harness** — subagent `delegate`, terminal tools (edit/bash/grep/web), the permission gate (modes + `allowed_paths` + bash sandbox + secret-deny), verify-by-execution, interrupt/TODO/compaction — [harness](./capabilities/harness.md)
- **Extensibility** — declarative agents (`carve/agents/*.md`), skill packs (`SKILL.md`), hooks (`hooks.toml`), MCP both directions, runtime grant attenuation — [extensibility](./capabilities/extensibility.md)

**Depends on.** M1/M1.1 (the agent loop the harness wraps; the CLI/config it extends).

**Delta.** 01 is mostly done — verify + close gaps. 15 *wraps* the M1 agent loop (adds delegation, the gate, context management); it does not replace it. 16 is net-new. 03 introduces `carve.toml` as control-plane config (supersedes the M1 project-shaped config) + the locator (net-new).

**Exit criteria.** A `carve.toml` with `[components.<name>]` resolves names to code (simple + multi mode); an agent runs under the permission gate with terminal tools and can `delegate` to a subagent that verifies by execution; a user-authored `carve/agents/*.md` overrides a built-in, attenuated to its mode.

---

## Increment 2 — Components & composition: SQL, the DLT engineer, pipelines

**Goal.** AI authors and runs a dlt component, and composes components by name into a runnable pipeline DAG.

**In scope**
- The dialect-aware **SQL tool layer** (sqlglot validate/transpile, per-dialect introspection, role-gated exec; Snowflake + DuckDB first-class) + thin SQL specialist — [sql](./capabilities/sql.md)
- The **DLT engineer** subagent (authors/runs dlt; native/REST/curated-library/MCP paths) + the dlt-qa / dlt-security review subagents — [dlt-engineer](./capabilities/dlt-engineer.md)
- **Multi-step pipeline** composition: `pipelines/<name>.toml`, the step DAG executor (dlt/dbt/sql), `[seed_schedule]`, component-by-name, the definition reconciler, the **pipeline engineer** subagent, `carve component(s)` graduation — [pipelines](./capabilities/pipelines.md)

**Depends on.** Increment 1 (harness, extensibility, control-plane layout). 04 needs the harness + SQL tool; 08 needs 04 + the locator.

**Delta.** 18 *generalizes* the M1 Snowflake-only `run_snowflake_query` + catalog skills into a dialect-aware layer (preserves the connector). 04 *replaces/generalizes* the M1 EL agent as a declarative subagent on the harness. 08 is net-new (the reconciler creates the `pipelines`/`schedules` tables it owns the seeding for).

**Exit criteria.** `carve plan "ingest <X>"` → the DLT engineer authors + verifies a dlt component; `carve build` materializes it + a `pipelines/<name>.toml` referencing it by name; `carve pipelines validate` passes; the `sql` tool introspects a live warehouse on the read role.

---

## Increment 3 — Runtime & bootstrap: scheduler, init, memory

**Goal.** A real project you can scaffold and run on a schedule end-to-end.

**In scope**
- The **runtime** — scheduler (reads the `schedules` table), Postgres job queue (optimistic claim), worker pool, heartbeats, reaper, archiver; the live `schedules` table + `carve schedule` mutation surface + `schedule_changes` audit — [runtime](./capabilities/runtime.md)
- **`carve init`** — greenfield/brownfield across the Postgres × dbt × dlt × memory axes; renders the control-plane `carve.toml`; convention inference — [init](./capabilities/init.md)
- **Project memory** — `conventions.md` / `standards.md` / `decisions.md`, sidecars, `carve memory` surface — [memory](./capabilities/memory.md)

**Depends on.** Increment 2 (08's pipeline definitions + reconciler that 07's scheduler reads; 05 renders configs the runtime serves). 05 also needs 01/02/03.

**Delta.** 07 *wraps* the M1 `LocalVenvRunner` (preserved) in a scheduler + queue + worker layer; creates the runtime tables (jobs, workers, archives, events, **schedules**, schedule_changes). 05 *rewrites* the M1.1 init around the control-plane carve.toml + the four axes. 06 builds on 05's scaffolded memory files.

**Exit criteria.** `carve init` produces a working project (bundled or external Postgres); `carve serve` schedules + runs a pipeline on cron; `carve schedule pause/resume/set-cron` changes firing instantly, audited; brownfield init infers conventions and writes no `[components.*]` blocks in simple mode.

---

## Increment 4 — Interfaces & investigation: REST, MCP, UI, ask, lineage

**Goal.** Drive Carve programmatically and investigate the project read-only.

**In scope**
- **REST API** — full CLI-surface coverage, auth, errors, pagination, streaming, webhooks — [rest-api](./capabilities/rest-api.md)
- **MCP server** — auto-generated from REST; stdio + WebSocket — [mcp-server](./capabilities/mcp-server.md)
- **Static HTML UI** — regenerated per run; `carve docs serve` — [ui](./capabilities/ui.md)
- **The explorer (`ask`)** — read-only investigative subagent; citations — [ask](./capabilities/ask.md)
- **Lineage by investigation** — the `dlt_schema` reader skill; the explorer answers lineage via dbt manifest + dlt schema + code (no Carve store) — [lineage](./capabilities/lineage.md)

**Depends on.** Increment 3 (07/08 surfaces to expose). 12/19 need the harness (incr 1) + SQL tool (incr 2); 10/11 need 09.

**Delta.** All largely net-new surfaces over the increments 1–3 substrate. 12 subsumes the old ask-only guardrail into the `read_only` mode. 19 adds one skill (`dlt_schema`) + explorer guidance — no graph.

**Exit criteria.** Every CLI action has a REST + MCP equivalent (parity); `carve ask "where does X come from?"` returns a cited answer via investigation; the static UI shows run history + per-run logs; the MCP server exposes the toolset over stdio + ws.

---

## Increment 5 — Deploy & recovery

**Goal.** Promote built code to prod, and auto-diagnose failures.

**In scope**
- **Deploy** — `carve deploy <pipeline>` configurable handoff (files/commit/push/pr, default pr); cross-repo linked PRs; pre-flight drift — [deploy](./capabilities/deploy.md)
- **Recovery engineer** — diagnose-then-delegate on retries-exhausted `run.failed`; the `Investigation` entity; auto-pause/resume gated by pause origin — [recovery](./capabilities/recovery.md)

**Depends on.** Increment 2 (08 deploy targets) + increment 3 (07 `run.failed`, schedules auto-pause). 17 needs the harness/delegation (incr 1) + the engineers it delegates to (incr 2) + deploy (14, this increment, for the resolving-deploy → auto-resume link).

**Delta.** 14 *retires* the `carve el deploy` DDL-apply path; net-new handoff/linked-PR machinery + the `deploys` table. 17 reuses the M1 recovery POC's reconcilable parts; net-new `investigations` table + the diagnose-then-delegate flow.

**Exit criteria.** `carve deploy` opens a (linked) PR by default, with each handoff depth working; a retries-exhausted failure produces a grounded `Investigation` + a reviewable fix Plan, auto-pauses the schedule, and the resolving deploy auto-resumes it (unless a human paused it).

---

## Increment 6 — Reference & release

**Goal.** Correct reference docs and the `v0.1.0` tag.

**In scope**
- **Reference docs** — cli-reference / config-schema / glossary / governance kept in lock-step via completeness tests — [reference-docs](./capabilities/reference-docs.md) *(content rewritten 2026-06; this increment adds the completeness tests against built code)*
- **Release** — tag `v0.1.0`.

**Depends on.** Everything (reference derives from the built surface).

**Delta.** The reference content is already regenerated to the v0.1 model; this increment adds the build-time completeness tests (every Typer command in cli-reference; every init-scaffolded file in config-schema) and pins the few **planned** CLI commands (`run --watch/--resume`, `runs list/show/tail`, `auth login`, `metrics` CLI spelling) by giving each an owning slice or cutting it.

**Exit criteria (v0.1.0).** `carve init → plan → build → run → deploy → scheduled-run-on-cron` works end-to-end against a real Snowflake account, and the same loop works via REST and MCP. Completeness tests green.

---

## Sequencing rationale

```
M1 / M1.1 / 01  ──▶  Incr 1: 02 03 15 16  ──▶  Incr 2: 18 04 08  ──▶  Incr 3: 07 05 06
                                                                              │
                                          Incr 4: 09 10 11 12 19  ◀───────────┤
                                          Incr 5: 14 17           ◀───────────┘
                                          Incr 6: 13 + v0.1.0 tag (after all)
```

- **Foundation-first.** The harness (15/16) and control-plane layout (03) gate everything AI- and component-shaped; nothing real ships before them.
- **Capability before interface.** Author/run/compose (incr 2–3) before exposing over REST/MCP/UI (incr 4).
- **Deploy + recovery after a pipeline can run** (they act on built/running pipelines).
- **Reference + release last** (derives from the built surface) — the ADR's reasoning for why reference docs ship last.

## Post-v0.1 (not yet sequenced)

Tracked here so they aren't lost, deliberately unsequenced until v0.1 lands:

- **dbt capabilities** (now specced; awaiting increment placement — the next sequencing decision): [`dbt-execution`](./capabilities/dbt-execution.md) — run dbt as a step across backends (local bundled/external + managed snowflake-native / dbt-cloud / remote); needed *before* authoring, since orchestration-only shops run dbt without writing it. [`dbt-engineer`](./capabilities/dbt-engineer.md) — AI authoring of models/tests/sources (+ dbt-qa). [`connect`](./capabilities/connect.md) — on-demand provisioning (engine install + pin, warehouse/source connect).
- **Lineage depth:** `sql`-step producer tracking ([lineage](./capabilities/lineage.md) *Out of scope*). (Column-level lineage may arrive *via the Fusion dbt engine* rather than Carve — see [dbt-execution](./capabilities/dbt-execution.md).)
- **Retrieval:** embedding/semantic search (ARCHITECTURE §6.1 layer 5).
- **Concurrency:** concurrent subagent fan-out (v0.1 is sequential/sync).
- **The planned CLI commands** if not pinned in increment 6.
