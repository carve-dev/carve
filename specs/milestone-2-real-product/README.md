# Milestone 2 — Real product

**Duration:** 2 weeks (days 8-21)
**Goal:** the version you'd publish to GitHub. Multiple agents, plan/deploy with PRs, dbt integration, basic web UI, brownfield onboarding.

## Acceptance criteria

A data engineer with an existing dbt project can:

1. Run `carve init` in their repo
2. Have Carve detect their dbt project and generate a `carve/conventions.md`
3. Run `carve plan "make stg_orders incremental"` and see a sensible plan
4. Run `carve deploy <pipeline_name>` and see a PR opened in their GitHub repo
5. Watch the run live in the web UI's workbench
6. See pipeline runs in the pipeline monitor

This is the version that goes on GitHub as `v0.0.5` and gets shared with five trusted reviewers including at least one outside the team.

## What ships in addition to M1

- Build as a first-class entity (the deployable artifact); Pipeline points to its current Build, not its current Plan
- Multi-task task graph in plans (replaces the single-pipeline design blob from M1.1-06)
- Orchestration agent + extract-load agent + dbt agent + Snowflake agent (split out from M1's combined agent)
- Build agent reshaped into a coordinator that dispatches each task-graph entry to its assigned specialist sub-agent
- dbt step type and dbt-core integration
- Brownfield `carve init` with existing dbt detection
- Convention inference from existing dbt projects
- Schema retrieval skills (catalog + manifest queries; embeddings deferred to M3)
- FastAPI server with REST + WebSocket
- Workbench and pipeline monitor screens
- Deploy orchestration: `carve deploy <pipeline> --target X` runs local pre-flight (with AI recovery), opens a PR with code + DDL + migrations + a generated GitHub Actions workflow; post-merge the workflow applies DDL, runs idempotent migrations, and verifies the deploy
- Recovery agent that auto-fixes failed runs in dev (Claude-Code-style: read error → patch or replan → re-run; bounded by attempts and dollars)

## What is still deferred to M3

- Multi-step pipelines and the `sql`, `shell`, `http` step types
- Embedding-based schema search
- MCP integration
- Quality agent (split from dbt agent in M3)
- Skills SDK (drop-in custom skills)
- Custom step types
- Agent studio screen
- dbt run view screen
- Three example projects
- Documentation site
- `carve doctor`
- Additional destinations beyond Snowflake (Postgres, BigQuery, S3) — M4 or community

## Spec list

In recommended build order:

1. [`01-plan-deploy-workflow.md`](./01-plan-deploy-workflow.md) — task-graph schema, hash validation, build coordinator pattern, real `carve deploy`
2. [`02-orchestration-agent.md`](./02-orchestration-agent.md) — goal classification, agent selection, task graph generation
3. [`03-extract-load-agent.md`](./03-extract-load-agent.md) — Python extract-and-load script authoring (the build-time specialist that owns `pipelines/<name>/main.py`)
4. [`04-dbt-agent.md`](./04-dbt-agent.md) — dbt model authoring and modification
5. [`05-snowflake-agent.md`](./05-snowflake-agent.md) — DDL, RBAC, warehouse management
6. [`06-dbt-integration.md`](./06-dbt-integration.md) — dbt step type, dbt-core invocation, manifest reading
7. [`07-brownfield-onboarding.md`](./07-brownfield-onboarding.md) — detect existing dbt, integrate without overwriting
8. [`08-convention-inference.md`](./08-convention-inference.md) — analyze project, generate conventions doc
9. [`09-schema-retrieval.md`](./09-schema-retrieval.md) — catalog queries, manifest queries, lineage traversal
10. [`10-fastapi-server.md`](./10-fastapi-server.md) — REST endpoints, auth, static asset serving
11. [`11-websocket-streaming.md`](./11-websocket-streaming.md) — live log and event streaming
12. [`12-web-ui-workbench.md`](./12-web-ui-workbench.md) — goal input, active goal feed, task graph
13. [`13-web-ui-pipeline-monitor.md`](./13-web-ui-pipeline-monitor.md) — pipeline list, status, runs
14. [`14-github-pr-integration.md`](./14-github-pr-integration.md) — deploy orchestration: pre-flight, PR + workflow generation, post-merge GitHub Actions for DDL provisioning + idempotent migrations + verification *(spec proposal pending: rename to `14-deploy-orchestration.md` and broader scope)*
15. [`15-recovery-agent.md`](./15-recovery-agent.md) — autonomous fix loop on `carve run` failures (depends on the build-time specialists from 03–05)

## Definition of done

- All 15 specs implemented with tests
- Acceptance criteria above met
- A 5-minute screen recording of the demo flow exists
- Internal tag `v0.0.5`
