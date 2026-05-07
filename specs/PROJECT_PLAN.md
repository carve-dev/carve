# Carve — Project Plan

Carve is structured as **four product pillars**, each shipped as its own version. Pillars 1 and 2 work standalone; Pillars 3 and 4 build on them. A team that just wants AI-authored EL scripts adopts Pillar 1; a team that wants the full lifecycle adopts all four.

## The four pillars

| Version | Pillar | Status | Goal |
|---|---|---|---|
| v0.1 | **Extract & Load** | specs in flight | AI authors Python EL scripts that move data from sources into Snowflake. Standalone — works for users who already have a dbt project + their own scheduler. |
| v0.2 | **Transform** | planned | AI maintains dbt models in a new or existing repo. |
| v0.3 | **Pipeline** | planned | Multi-step pipelines composed of EL artifacts (Pillar 1) + dbt models (Pillar 2) + ad-hoc steps (SQL, shell, HTTP). |
| v0.4 | **Schedule & Execution** | planned | Schedule, run, monitor, and maintain pipeline executions. |

Each pillar is a usable product. Demo it, get feedback, then start the next.

## Foundation (already shipped)

Two pre-pillar milestones laid the groundwork:

- **M1 — Walking skeleton.** Smallest end-to-end loop: CLI foundation, config loader, state store, Anthropic agent loop with tool-use, Python step + `LocalVenvRunner`, Snowflake connector. Specs in [`milestone-1-walking-skeleton/`](./milestone-1-walking-skeleton/). **Shipped.**
- **M1.1 — Follow-ups.** UX polish and the pipeline-centric lifecycle: init config templates, Claude Code OAuth path, dotenv autoload, live progress output, plan-prompt tightening, plan/build/run separation, run-retry-permits-redo. Specs in [`milestone-1.1-followups/`](./milestone-1.1-followups/). **Shipped.**

Combined, M1 + M1.1 give Carve the agent loop, state store, runner, connector, and the `plan → build → run → deploy` lifecycle that all four pillars build on. ~300 tests passing.

## Guiding principles

- **Ship before perfect.** The version that gets feedback in week 2 is more valuable than the one that ships in month 6 with three more features.
- **Pick boring technology.** `typer`, `pydantic`, `SQLAlchemy`, `SQLite`, `Snowflake-connector-python`, `Anthropic SDK`, `tomlkit`. Save the novelty budget for the agent layer.
- **Skip the SaaS scaffolding.** The runner and connector abstractions matter because the SaaS pivot exists later. The implementations don't.
- **Ship pillars standalone.** A user adopting only Pillar 1 should never feel they're using a half-finished product. Each pillar is complete on its own.
- **Defer extension points.** Hard-code built-in skills and step types until they've stabilized. The skills SDK lands later.

## Pillar 1 — Extract & Load (v0.1, in flight)

**Goal:** AI authors Python EL scripts for Snowflake. Standalone.

**Acceptance criteria.** A data engineer with a Snowflake account (no dbt project, no orchestrator, nothing else) can:

1. Run `carve init` — gets `carve.toml`, `carve/{connections.toml, runner.toml, models.toml}`, `targets/dev/el/`, `.env.example`, `.gitignore`
2. Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` + `DEV_SNOWFLAKE_*` vars
3. `carve plan "ingest the Iowa liquor sales feed"` — AI produces a design
4. `carve build <plan_id>` — AI authors `targets/dev/el/iowa_liquor/{main.py, requirements.txt}` plus `targets/dev/snowflake/iowa_liquor.sql`
5. `carve el run iowa_liquor` — script runs against dev, lands rows in dev's Snowflake
6. (When ready for prod) `carve target create prod` — appends `[snowflake.prod]` to `carve/connections.toml`
7. Add `PROD_SNOWFLAKE_*` values to `.env`
8. `carve el deploy iowa_liquor --from dev --to prod` — copies the artifact, applies DDL via the deploy role, smoke-verifies

**Specs.** Nine specs in [`pillar-1-extract-load/`](./pillar-1-extract-load/) — see the README for the full list.

**Estimated effort.** ~1 week for an experienced engineer using AI coding tools aggressively. The day-by-day depends on which specs the engineer picks up in parallel; spec dependencies are documented so the dependency graph is explicit.

**Internal milestone.** When all 9 specs are implemented and the acceptance flow above works end-to-end against a real Snowflake account, tag `v0.1.0`.

## Pillar 2 — Transform (v0.2, planned)

**Goal:** AI maintains dbt models in a new or existing repo.

**Probable scope** (specs not yet drafted; informed by the M2 archive):

- dbt agent — authors and modifies dbt models, tests, and documentation
- dbt integration — `dbt` step type, manifest reader, structured manifest queries (Layer 2 from P1-05's five-layer schema retrieval model)
- File grep + lineage traversal skills (Layers 3 + 4)
- Brownfield onboarding — detect existing dbt projects, integrate without overwriting
- Convention inference — analyze existing dbt repo, generate `carve/conventions.md`
- Build coordinator pattern — when there are multiple specialists (extract-load + dbt), the coordinator dispatches each plan task to the right one. Deferred from Pillar 1 because Pillar 1 has only one specialist.

Pillar 2 stays standalone — a user with no Pillar-1 EL artifacts can use Carve purely for dbt work.

## Pillar 3 — Pipeline (v0.3, planned)

**Goal:** Define multi-step pipelines that orchestrate EL + dbt + ad-hoc steps.

**Probable scope** (informed by the M3 archive):

- Pipeline definition format (`pipeline_defs/<name>.yml` per target)
- Multi-step execution: `depends_on`, parallel/sequential, failure modes
- Step types: `el://<artifact>`, `dbt://<model>`, `sql://<file>`, `shell://...`, `http://...`
- Custom step types via plugin
- Multi-step pipeline recovery (recovery agent gets "step that failed" awareness)

Pipelines reference Pillar 1 EL artifacts and Pillar 2 dbt models by name. Cross-pillar references resolve per-target via the centralized `targets/<name>/` folder structure (P1-01).

## Pillar 4 — Schedule & Execution (v0.4, planned)

**Goal:** Schedule, run, monitor, and maintain pipeline executions.

**Probable scope:**

- Schedule definition (`schedules/<name>.yml` per target — cron-style)
- Scheduler daemon (or generated CI/CD config snippets for users who use external schedulers)
- Run history UI (or expanded CLI views)
- Pipeline lifecycle (disable, archive, restore — currently sketched as M3-15 in the archive)
- Quality agent — split from the dbt agent for tests and freshness
- Embedding-based schema search (Layer 5 from P1-05)
- MCP server consumption

A future **UI milestone** (post-Pillar-4 or a parallel track) covers the FastAPI server, WebSocket streaming, workbench, and pipeline monitor screens. Carve's CLI / Claude / chat interfaces are the primary surface; the web UI is layered on top, optional, and not blocking the v0.4 release.

## Risk and slip

The risk pattern across all four pillars is the same:

- **Schema retrieval edge cases.** Real-world Snowflake accounts are messier than fixtures. Mitigation: dogfood early against three different test repos.
- **Brownfield detection edge cases (Pillar 2).** Same shape; same mitigation.
- **Recovery agent's failure-classification breadth.** New failure shapes appear in real use. Mitigation: the `failure_taxonomy.py` is intentionally extensible and ships with the patterns we know about; new categories arrive via PR.
- **Scheduler + monitoring complexity (Pillar 4).** This is the biggest unknown. Pillar 4's spec set will land closer to the work, not now.

If a pillar slips, the slip cuts scope rather than time — defer pieces to the next pillar or a follow-up release.

## What this plan deliberately defers

- **Multi-LLM-provider support.** Anthropic-only for v0.1. OpenAI / Google / others come when there's demand.
- **Docker runner.** `LocalVenvRunner` only.
- **Multi-user authentication.** Single user. SaaS comes later.
- **MCP server (Carve as MCP server).** Consumed in Pillar 4; not exposed in any v0.x.
- **Visual pipeline editor.** TOML / YAML authoring, AI-first. The web UI provides views, not authoring.
- **BigQuery, Databricks, Redshift, Postgres destinations.** Snowflake-only for v0.1; M4 or community contributions extend.
- **dbt Cloud as an executor backend.** Possibly later; stays out of v0.x.

## What's after v0.4

The first 30 days post-launch of each pillar are about listening, not building. Roadmap evolves with real user feedback. Early candidates for v0.5+:

- BigQuery / Databricks adapters
- Multi-provider LLM support
- Docker runner
- Multi-user authentication for SaaS-mode
- Visual pipeline monitor (if the CLI views aren't enough)

But these are guesses. The actual v0.5+ priorities come from issues and PRs that arrive after each pillar ships.
