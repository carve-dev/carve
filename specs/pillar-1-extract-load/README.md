# Pillar 1 — Extract & Load (v0.1)

**Duration:** ~1 week (estimate)
**Goal:** AI authors Python extract-and-load scripts for Snowflake. Standalone — works for users who want only the EL piece (already have a dbt project, already have an orchestrator, just want help with the ingestion code).

## Acceptance criteria

A data engineer with a Snowflake account (no dbt project, no orchestrator, nothing else) can:

1. Run `carve init` — gets `carve.toml`, `targets/dev/{el,connections.toml,.env.example}`, `.gitignore`
2. Fill in `targets/dev/.env` and `connections.toml` for their dev Snowflake
3. Run `carve plan "ingest the Iowa liquor sales feed"` — AI produces a design for an EL artifact
4. Run `carve build <plan_id>` — AI authors `targets/dev/el/iowa_liquor/{main.py, requirements.txt}`
5. Run `carve el run iowa_liquor` — script runs against dev, lands rows in dev's Snowflake
6. (When ready for prod) `carve target create prod` — scaffolds `targets/prod/`
7. Fill in `targets/prod/.env` and `connections.toml` for prod
8. `carve el deploy iowa_liquor --from dev --to prod` — copies the artifact to `targets/prod/el/iowa_liquor/`, generates DDL, opens a PR; user wires the post-merge automation

This is `v0.1.0` on GitHub — proves Pillar 1 in isolation. A user adopting only Pillar 1 has a complete, useful product without ever touching dbt, pipelines, or scheduling.

## What ships

- **Target system.** Per-target folder layout (`targets/<name>/`), `carve target` subcommand family, `default_target` config, `--target` flag.
- **Plan / build / run / deploy lifecycle** adapted to per-target folders. Plans produce designs; builds write to `targets/<active>/el/<name>/`; the `Build` entity persists.
- **Extract-load agent** as the AI specialist that authors EL scripts. Universal data-engineering skill + Snowflake destination skill, loaded on demand.
- **Snowflake DDL generation** for EL destinations — per-EL `<target>/snowflake/<el-name>.sql` with the CREATE TABLE / GRANT statements the script needs.
- **Catalog skills** for the EL agent to inspect target schemas at plan time.
- **EL deploy** — `carve el deploy <name> --from X --to Y` runs local pre-flight + opens a PR carrying the artifact + DDL + a deployment checklist. Composable post-merge primitives: `carve el provision`, `carve el migrate`, `carve el verify`. One example GitHub Actions workflow ships in docs; users adopt as they wish.
- **Recovery agent** for `carve el run` failures and Phase-1 deploy failures (auto-fix loop bounded by attempts + cost).
- **CLI subcommand structure** (`carve el ...`, `carve target ...`).

## What is deferred to later pillars

- **dbt integration / agent / brownfield onboarding / convention inference** → Pillar 2
- **Multi-step pipelines and ad-hoc step types (SQL, shell, http)** → Pillar 3
- **Multi-task task graphs and the build-coordinator pattern** → Pillar 2 onward (only meaningful with multiple specialists)
- **Scheduling, monitoring, run history UI** → Pillar 4
- **FastAPI server, WebSocket streaming, web UI** → after Pillar 4 (or scrap; CLI/Claude/chat are the primary interfaces)
- **Full Snowflake agent** (warehouses, role hierarchies, RBAC management) → Pillar 2 or later; Pillar 1 ships only the per-EL DDL subset
- **Embedding-based schema search** → far future
- **Multi-target deploy in a single command** (`--targets staging,prod`) → later
- **Pipeline / artifact lifecycle** (disable / archive / restore) → Pillar 4 alongside scheduling

## What survives from M1 / M1.1 unchanged

Every shipped primitive is preserved as-is. Pillar 1 is **incremental**, not a rewrite:

- All 300+ existing tests stay green
- State store (`plans`, `runs` tables) — schema gains the `builds` table from accepted M2-01 via migration `0004_build_entity.py`; existing columns are unchanged (with the `0003` apply→deploy rename in place)
- `AgentLoop` and `terminator_tool` mechanism (M1-04)
- Snowflake connector (M1-06) — gains per-target connection lookup; the connector itself is unchanged
- `LocalVenvRunner` and the step + runner protocols (M1-05)
- Apply→deploy rename + migration `0003_rename_apply_to_deploy.py` (already shipped)
- Anthropic SDK integration (M1-04, M1.1-02 OAuth path)
- Live progress observer (M1.1-04)
- Init config templates (M1.1-01) — content preserved; only directory destinations change
- Dotenv autoload (M1.1-03) — loader unchanged; resolution becomes target-aware
- Pipeline-centric lifecycle verbs (M1.1-06) — plan / build / run / deploy stay; CLI restructures around subcommands

## What gets restructured (additively where possible)

- **CLI shape** — subcommand pattern (`carve el run X`). Existing `carve run X` becomes a deprecated alias that warns + forwards for one minor version, then is removed.
- **File layout** — per-target folders (`targets/dev/el/X/main.py`). Existing root-level `pipelines/X/main.py` is supported during transition with a deprecation warning; no automatic migration in v0.1 (manual `git mv` is the upgrade path; keep an eye out for users hitting friction).
- **Plan / build verbs** stay general (the AI agent decides which pillar applies). Operational verbs go pillar-specific (`carve el deploy`, `carve el run`, `carve target create`).

## What's net-new (no M1/M1.1 ancestor)

- The target system itself (`targets/<name>/` layout + `carve target` subcommand family) — synthesized during this session's design discussion
- The `Build` entity as a separate row from Plan — first appears in accepted M2-01; lands here in Pillar 1
- The composable post-merge deploy primitives (`carve el provision`, `carve el verify`) — replacement for the parked M2-14 generated-workflow approach

## Spec list

In recommended build order. Each spec carries an explicit **Lineage** field naming its M1 / M1.1 / M2 ancestors so nothing reads as a fresh start.

| # | Spec | Purpose | Lineage |
|---|---|---|---|
| 01 | [target-system](./01-target-system.md) | `targets/<name>/` layout, `carve target` subcommand, `default_target`, `--target` flag | **Net-new** (synthesized this session) |
| 02 | [plan-build-lifecycle](./02-plan-build-lifecycle.md) | Plan + build + Build entity, per-target | Continues **M1.1-06** + reuses **accepted M2-01** |
| 03 | [init-per-target-layout](./03-init-per-target-layout.md) | `carve init` scaffolds `targets/dev/` | Continues **M1.1-01** (templates preserved) |
| 04 | [per-target-dotenv](./04-per-target-dotenv.md) | Load `targets/<active>/.env` based on resolved target | Continues **M1.1-03** (loader unchanged; path-resolution evolves) |
| 05 | [extract-load-agent](./05-extract-load-agent.md) | AI specialist authoring EL scripts | Carries **accepted M2-03** verbatim (delta: output path) |
| 06 | [schema-retrieval](./06-schema-retrieval.md) | Catalog skills only | Subset of **M2-09** (catalog layer only; manifest/lineage to Pillar 2) |
| 07 | [snowflake-ddl-for-el](./07-snowflake-ddl-for-el.md) | Per-EL DDL emission | Subset of **M2-05** (per-pipeline output portion only) |
| 08 | [el-run](./08-el-run.md) | `carve el run <name> [--target X]` | Continues **M1.1-06**'s `carve run`; CLI restructured under `el` subcommand |
| 09 | [el-deploy](./09-el-deploy.md) | `carve el deploy --from X --to Y` + composable primitives | Replaces **parked M2-14 proposal** (drops generated workflow files; reframes as composable primitives) |
| 10 | [recovery-agent](./10-recovery-agent.md) | Auto-fix loop for run + deploy-Phase-1 failures | Carries **M2-15** (scope narrows to Pillar 1 contexts) |

**Key lineage notes:**
- The accepted M2 specs we reviewed together (**M2-01** Plan/Build, **M2-02** Orchestration, **M2-03** Extract-load, **M2-07** Brownfield, **M2-10** FastAPI) all stay where they are at [`specs/milestone-2-real-product/`](../milestone-2-real-product/). M2-01 and M2-03 directly source pillar-1 specs; the others slot into later pillars (Pillar 2 for M2-07; UI milestone for M2-10) or get reframed (Pillar 2+ absorbs M2-02 when there are multiple specialists to orchestrate).
- The **M2-14 proposal** is parked in place ([`_spec_update_proposal_M2-14.md`](../milestone-2-real-product/_spec_update_proposal_M2-14.md)) as historical context for the deploy discussion. It is explicitly *not* accepted; P1-09 supersedes it with a leaner OSS-flexible reframe.
- The unreviewed M2 specs (M2-04, M2-06, M2-08, M2-09, M2-11, M2-12, M2-13, M2-15 inline edits) stay in place; their content is current state and gets re-homed into pillars as we draft each pillar.

## Definition of done

- All 10 specs implemented with tests
- Acceptance criteria above met end-to-end against a real Snowflake account
- A 3-minute screen recording of the demo flow (init → plan → build → run → target create → deploy)
- Internal tag `v0.1.0`
