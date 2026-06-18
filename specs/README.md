    # Carve — design documents

This folder contains the full product and engineering specification for **Carve**, an AI-first open-source framework for data engineering and analytics engineering. It captures the architectural decisions, design choices, and build plan for the project, organized so an engineering team can pick it up and execute.

## Document map

The corpus splits into **durable design** (version-independent — what Carve is and how it works) and a **delivery plan** (temporal — what to build, when). See [`_strategy/2026-06-spec-structure.md`](./_strategy/2026-06-spec-structure.md).

### Top-level

**Durable design:**
- [`PRD.md`](./PRD.md) — the master product requirements document. The single source of truth for what Carve is, who it's for, and what it does.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — the technical architecture deep-dive: components, data flow, extension points, the dev/prod target model, and the boundary between OSS and SaaS.
- Capability specs in [`capabilities/`](./capabilities/) — the lowest level of *design* detail, one per capability area.

**Delivery:**
- [`DELIVERY.md`](./DELIVERY.md) — the live, dependency-aware, **delta-aware** delivery plan, organized into foundation-first increments. **The source of truth for sequencing and scope.**
- [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) — the prior pillar-based build plan; **superseded for sequencing** by `DELIVERY.md` (its durable "shape of Carve" framing folds into PRD/ARCHITECTURE).

### Carve's four product pillars

**Carve is a control plane plus an AI harness, over independently-versioned dlt/dbt/sql components — not a project that contains them** ([`_strategy/2026-06-control-plane.md`](./_strategy/2026-06-control-plane.md), [`_strategy/2026-06-ai-harness.md`](./_strategy/2026-06-ai-harness.md)). The value proposition: **build, schedule, and monitor pipelines — all with AI.** The work is organized as four pillars (sequenced into increments in [`DELIVERY.md`](./DELIVERY.md)); the capability specs are in [`capabilities/`](./capabilities/).

| Pillar | Theme | Ships |
|---|---|---|
| **P1** | Extract & Load — the **DLT component + engineer** (AI authors/runs dlt components) | v0.1 |
| **P2** | Runtime — the **control plane** (scheduler / executor / monitor referencing components by name) | v0.1 |
| **P3** | Transform — the **dbt component + engineer** | v0.2 |
| **P4** | Multi-step pipeline — **composition** (components by name → step DAG) | v0.1 |

Underpinning all four: the **AI harness** — a Claude-Code-style agentic engine (subagent orchestration, terminal-grade tools, a permission system, verify-by-execution, and declarative agents/skills/hooks extensibility), plus the recovery engineer and the dialect-aware SQL tool layer.

**Adoption is incremental.** A brownfield dbt shop can use Carve in **orchestration-only mode** (bring your own dlt/dbt; Carve composes, schedules, monitors). A team that wants AI to build ingestion adopts the DLT engineer + control plane. A team that wants the whole lifecycle adopts all four pillars.

### The capability specs

The durable design lives in [`capabilities/`](./capabilities/) — **19 capability specs**, drafted/revised to the control-plane + AI-harness model. See [`capabilities/README.md`](./capabilities/README.md) for the full list, per-spec status, and the foundational reading order (specs **15 agent-harness** and **16 extensibility** are the AI foundation everything runs on). The pre-2026-05 Pillar 1 / Pillar 1.1 specs were archived (their content carried forward) — see [`_archive/`](./_archive/).

### Foundation (M1, M1.1) — already shipped

These are kept as living spec directories for the M1 / M1.1 work that's already in code.

- [`milestone-1-walking-skeleton/`](./milestone-1-walking-skeleton/) — the smallest end-to-end loop (CLI foundation, config loader, state store, Anthropic agent loop, Python step + runner, Snowflake connector). Shipped.
- [`milestone-1.1-followups/`](./milestone-1.1-followups/) — UX polish and the pipeline-centric lifecycle (init templates, OAuth, dotenv autoload, plan progress, agent prompt tightening, plan/build/run separation, run-retry-permits-redo). Shipped.

### Reference

- [`reference/config-schema.md`](./reference/config-schema.md) — full TOML/YAML schema reference for `carve.toml` and the `carve/` config directory
- [`reference/cli-reference.md`](./reference/cli-reference.md) — every CLI command, flag, and exit code
- [`reference/governance.md`](./reference/governance.md) — open-source governance, contributor model, license choice
- [`reference/glossary.md`](./reference/glossary.md) — definitions of the terms used throughout these docs

### Archive

- [`_archive/`](./_archive/) — historical specs that have been superseded by the pillar restructure. Includes the original "milestone 2 — real product" and "milestone 3 — polish" milestones, kept as source material for later pillars and as lineage for the current pillar specs. See [`_archive/README.md`](./_archive/README.md) for the disposition map.

## How to use these docs

If you're picking this project up cold:

1. Read [`PRD.md`](./PRD.md) end to end. It's the most important document — everything else is implementation detail.
2. Read [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) to understand the four-pillar delivery shape.
3. Skim [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the technical model.
4. Open [`capabilities/`](./capabilities/) and start building — read [`capabilities/README.md`](./capabilities/README.md) first (specs 15/16 are the AI-harness foundation everything runs on). M1 + M1.1 are already shipped.

If you're contributing to a specific area:

1. Find the relevant spec in [`capabilities/`](./capabilities/).
2. Each spec is self-contained — it lists its dependencies on other specs at the top, plus a `Lineage` field naming any M1 / M1.1 / archived M2 / archived M3 ancestors.
3. Specs include scope, interfaces, file paths, acceptance criteria, tests, and estimated effort.

## Status

- **M1 and M1.1 are shipped.** Code is in `src/`. ~300 tests passing; spec 01 (state store → Postgres) landed.
- **The 19 capability specs are drafted/revised** to the control-plane + AI-harness model ([`capabilities/`](./capabilities/)); the foundation harness specs (15/16) have been adversarially reviewed and hardened. The build sequence is in [`DELIVERY.md`](./DELIVERY.md); implementing it is the active phase.
- **The two foundational decisions** are captured in [`_strategy/2026-06-control-plane.md`](./_strategy/2026-06-control-plane.md) and [`_strategy/2026-06-ai-harness.md`](./_strategy/2026-06-ai-harness.md).
