    # Carve — design documents

This folder contains the full product and engineering specification for **Carve**, an AI-first open-source framework for data engineering and analytics engineering. It captures the architectural decisions, design choices, and build plan for the project, organized so an engineering team can pick it up and execute.

## Document map

### Top-level

- [`PRD.md`](./PRD.md) — the master product requirements document. The single source of truth for what Carve is, who it's for, and what it does.
- [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) — the build plan, organized around four product pillars released as v0.1 → v0.4.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — the technical architecture deep-dive: components, data flow, extension points, the dev/prod target model, and the boundary between OSS and SaaS.

### Carve's four product pillars

> ⚠️ **This section is stale.** The pillar ordering/bundling below is the pre-2026-05 sequential model. The authoritative pillar framing is in [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) (P1 Extract&Load, P2 Runtime, P3 Transform, P4 Multi-step; P1+P2+P4 ship in v0.1, P3 in v0.2). The structural model is being revised again per [`_strategy/2026-06-control-plane.md`](./_strategy/2026-06-control-plane.md) (Carve is a control plane that references components, not a project). This section will be rewritten once that lands.

Carve is structured as four independent-but-composable pillars. Each ships as its own version; later pillars build on earlier ones, but each pillar produces value standalone.

| Pillar | Version | Status | Goal |
|---|---|---|---|
| 1. **Extract & Load** | v0.1 | **shipped** | AI authors Python EL scripts that move data from sources into Snowflake. Standalone — works for users who already have a dbt project + their own scheduler. |
| 1.1. **Flat layout + git promotion** | v0.1.1 | **specs drafted** | Simplification pass: one code tree per artifact under `el/<name>/`, single-target deploy command, Jinja-templated DDL. Promotion via git, not file copy. |
| 2. **Transform** | v0.2 | planned | AI maintains dbt models in a new or existing repo. |
| 3. **Pipeline** | v0.3 | planned | Define multi-step pipelines composed of EL artifacts (Pillar 1) + dbt models (Pillar 2) + ad-hoc steps (SQL, shell, HTTP). |
| 4. **Schedule & Execution** | v0.4 | planned | Schedule, run, monitor, and maintain pipeline executions. |

**Adoption is incremental.** A team that just wants AI-authored EL scripts adopts Pillar 1 and keeps using their existing scheduler. A team that wants the whole lifecycle adopts all four. Pillars 1 and 2 work standalone; Pillars 3 and 4 build on them.

### Pillar 1 — Extract & Load (v0.1, shipped)

The smallest end-to-end loop: AI authors EL scripts, generates DDL, runs them in dev, and deploys them to other targets. CLI only; no UI. Tagged as `v0.1.0`.

- [`pillar-1-extract-load/README.md`](./pillar-1-extract-load/README.md) — overview, acceptance criteria, lineage notes
- [`01-target-system.md`](./pillar-1-extract-load/01-target-system.md) — `targets/<name>/` layout, `carve target` subcommand, `--target` flag
- [`02-plan-build-lifecycle.md`](./pillar-1-extract-load/02-plan-build-lifecycle.md) — Plan / Build entity / lifecycle, per-target
- [`03-init-per-target-layout.md`](./pillar-1-extract-load/03-init-per-target-layout.md) — `carve init` scaffolds the centralized layout
- [`04-extract-load-agent.md`](./pillar-1-extract-load/04-extract-load-agent.md) — AI specialist authoring EL scripts
- [`05-schema-retrieval.md`](./pillar-1-extract-load/05-schema-retrieval.md) — catalog skills + the skill registry infrastructure
- [`06-snowflake-ddl-for-el.md`](./pillar-1-extract-load/06-snowflake-ddl-for-el.md) — per-EL DDL emission contract
- [`07-el-run.md`](./pillar-1-extract-load/07-el-run.md) — `carve el run` command + `carve el list`
- [`08-el-deploy.md`](./pillar-1-extract-load/08-el-deploy.md) — `carve el deploy --from X --to Y` (single deterministic command) + `carve el verify`
- [`09-recovery-agent.md`](./pillar-1-extract-load/09-recovery-agent.md) — auto-fix loop for run + deploy failures

### Pillar 1.1 — Flat layout + git-based promotion (v0.1.1, specs drafted)

Simplification pass after dogfooding v0.1.0 surfaced friction with per-target folders + `--from X --to Y` deploy. One code tree per artifact, single-target deploy command, Jinja-templated DDL, git for promotion.

- [`pillar-1.1-flat-layout/README.md`](./pillar-1.1-flat-layout/README.md) — rationale, migration story, supersession table
- [`01-flat-layout.md`](./pillar-1.1-flat-layout/01-flat-layout.md) — `targets/<X>/el/<name>/` → `el/<name>/`
- [`02-destination-with-sections.md`](./pillar-1.1-flat-layout/02-destination-with-sections.md) — `destination.toml` with `[default]` + per-target sections
- [`03-templated-ddl-and-deploy.md`](./pillar-1.1-flat-layout/03-templated-ddl-and-deploy.md) — Jinja-templated DDL + `carve el deploy --target X`
- [`04-recovery-and-cicd-docs.md`](./pillar-1.1-flat-layout/04-recovery-and-cicd-docs.md) — recovery-agent path updates + CI/CD docs rewrite

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
4. Open [`pillar-1-extract-load/`](./pillar-1-extract-load/) and start building (M1 + M1.1 are already shipped).

If you're contributing to a specific area:

1. Find the relevant spec in the active pillar directory.
2. Each spec is self-contained — it lists its dependencies on other specs at the top, plus a `Lineage` field naming any M1 / M1.1 / archived M2 / archived M3 ancestors.
3. Specs include scope, interfaces, file paths, acceptance criteria, tests, and estimated effort.

## Status

- **M1 and M1.1 are shipped.** Code is in `src/`. ~300 tests passing.
- **Pillar 1 specs are in flight.** Drafting and review complete; implementation hasn't started.
- **Pillars 2-4 are planned.** Spec directories haven't been created yet; lineage from M2/M3 archive will inform them when work starts.
