> # ⚠ Archived
>
> **This milestone has been superseded by Carve's pillar-based structure.** The work
> originally planned here was restructured around four product pillars:
>
> 1. **Extract & Load** — see [`specs/pillar-1-extract-load/`](../../pillar-1-extract-load/) (v0.1)
> 2. **Transform** — `specs/pillar-2-transform/` (planned, v0.2)
> 3. **Pipeline** — `specs/pillar-3-pipeline/` (planned, v0.3)
> 4. **Schedule & Execution** — `specs/pillar-4-schedule/` (planned, v0.4)
>
> The specs in this directory are kept as **historical reference and source material**
> for the pillar specs. Several M3 specs map directly into Pillar 3 (multi-step
> pipelines, ad-hoc step types) and Pillar 4 (scheduling, monitoring); others
> (MCP, embedding search, doctor command, agent studio, dbt run view) sit at the
> boundary between Pillar 4 and a future UI milestone. Do **not** treat anything
> here as authoritative; consult the pillar specs (when written) for the current
> direction.
>
> **High-level disposition:**
> - Multi-step pipelines (M3-01, M3-02, M3-03) → **Pillar 3** (Pipeline)
> - Quality agent (M3-05) → splits from dbt agent in **Pillar 2** or later
> - MCP client (M3-04) → likely **Pillar 4** or later
> - Skills SDK (M3-06) → carried forward; SDK pattern lands when the in-tree skills stabilize
> - Custom step types (M3-07) → **Pillar 3**
> - Embedding search (M3-08) → far-future Layer 5 of P1-05's retrieval system
> - Web UI agent studio + dbt run view (M3-09, M3-10) → UI milestone after Pillar 4
> - Example projects (M3-11) → docs concern; lands when v0.1 ships
> - Documentation site (M3-12) → docs concern; ongoing
> - Doctor command (M3-13) → operational tooling; **Pillar 4** or later
> - Step disable/enable (M3-14) → **Pillar 3** (alongside multi-step pipelines)
> - Pipeline lifecycle (M3-15) → **Pillar 4** (disable/archive/restore for whole pipelines)
>
> See [`specs/_archive/README.md`](../README.md) for the full archive index.

---

# Milestone 3 — Polish for adoption (archived)

**Duration:** 3 weeks (days 22-42)
**Goal:** remove friction so the first hundred users you don't know personally can succeed on their own. v0.1.0 release at the end of week 6.

## Acceptance criteria

A stranger from the internet can:

1. Read the README, decide it's worth trying
2. Clone Carve, run `carve init`, configure their connections
3. Try the first example project successfully
4. Generate their first PR against their own dbt project
5. All within 20 minutes, without messaging anyone for help

## What ships in addition to M2

- Multi-step pipelines with `sql`, `shell`, `http` step types
- Per-step `enabled` flag and `carve step enable/disable/list` commands
- MCP client integration (consume external MCP servers as skills)
- Quality agent (split out from dbt agent)
- Skills SDK (drop a Python file, register a skill)
- Custom step types
- Embedding-based schema search
- Agent studio screen (UI)
- dbt run view screen (UI)
- Three example projects with realistic data
- mkdocs-material documentation site
- `carve doctor` health checks

## What is still deferred to v0.2+

- BigQuery, Databricks, Redshift adapters
- OpenAI / multi-LLM-provider support
- Docker runner
- Multi-user authentication, SSO, RBAC
- MCP server (Carve as an MCP server)
- Visual pipeline editor
- dbt Cloud as an executor backend

## Spec list

In recommended build order:

1. [`01-multi-step-pipelines.md`](./01-multi-step-pipelines.md) — DAG executor with parallelism
2. [`02-sql-step-type.md`](./02-sql-step-type.md) — direct SQL execution
3. [`03-shell-http-steps.md`](./03-shell-http-steps.md) — shell and HTTP step types
4. [`04-mcp-client.md`](./04-mcp-client.md) — consume external MCP servers
5. [`05-quality-agent.md`](./05-quality-agent.md) — split from dbt agent
6. [`06-skills-sdk.md`](./06-skills-sdk.md) — extension API
7. [`07-custom-step-types.md`](./07-custom-step-types.md) — extension API
8. [`08-embedding-search.md`](./08-embedding-search.md) — semantic schema search
9. [`09-web-ui-agent-studio.md`](./09-web-ui-agent-studio.md) — agent configuration UI
10. [`10-web-ui-dbt-run-view.md`](./10-web-ui-dbt-run-view.md) — lineage view
11. [`11-example-projects.md`](./11-example-projects.md) — three working examples
12. [`12-documentation-site.md`](./12-documentation-site.md) — mkdocs-material site
13. [`13-doctor-command.md`](./13-doctor-command.md) — health checks
14. [`14-step-disable-enable.md`](./14-step-disable-enable.md) — per-step `enabled` flag and `carve step` commands

## Definition of done

- All 14 specs implemented
- Acceptance criteria above met
- v0.1.0 tagged and pushed
- Launch blog post written
- Posted to Hacker News, dbt Slack, Locally Optimistic, /r/dataengineering
