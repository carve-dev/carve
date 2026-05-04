# Milestone 2 — Real product

**Duration:** 2 weeks (days 8-21)
**Goal:** the version you'd publish to GitHub. Multiple agents, plan/apply with PRs, dbt integration, basic web UI, brownfield onboarding.

## Acceptance criteria

A data engineer with an existing dbt project can:

1. Run `carve init` in their repo
2. Have Carve detect their dbt project and generate a `carve/conventions.md`
3. Run `carve plan "make stg_orders incremental"` and see a sensible plan
4. Run `carve apply <plan_id>` and see a PR opened in their GitHub repo
5. Watch the run live in the web UI's workbench
6. See pipeline runs in the pipeline monitor

This is the version that goes on GitHub as `v0.0.5` and gets shared with five trusted reviewers including at least one outside the team.

## What ships in addition to M1

- Persisted plans with `plan_id`, refinement, expiry, config-hash validation
- Orchestration agent + dbt agent + Snowflake agent (split out from M1's combined agent)
- dbt step type and dbt-core integration
- Brownfield `carve init` with existing dbt detection
- Convention inference from existing dbt projects
- Schema retrieval skills (catalog + manifest queries; embeddings deferred to M3)
- FastAPI server with REST + WebSocket
- Workbench and pipeline monitor screens
- GitHub PR creation on apply
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

## Spec list

In recommended build order:

1. [`01-plan-apply-workflow.md`](./01-plan-apply-workflow.md) — plan files, refinement, hash validation
2. [`02-orchestration-agent.md`](./02-orchestration-agent.md) — goal classification, agent selection, task graph
3. [`03-dbt-agent.md`](./03-dbt-agent.md) — dbt model authoring and modification
4. [`04-snowflake-agent.md`](./04-snowflake-agent.md) — DDL, RBAC, warehouse management
5. [`05-dbt-integration.md`](./05-dbt-integration.md) — dbt step type, dbt-core invocation, manifest reading
6. [`06-brownfield-onboarding.md`](./06-brownfield-onboarding.md) — detect existing dbt, integrate without overwriting
7. [`07-convention-inference.md`](./07-convention-inference.md) — analyze project, generate conventions doc
8. [`08-schema-retrieval.md`](./08-schema-retrieval.md) — catalog queries, manifest queries, lineage traversal
9. [`09-fastapi-server.md`](./09-fastapi-server.md) — REST endpoints, auth, static asset serving
10. [`10-websocket-streaming.md`](./10-websocket-streaming.md) — live log and event streaming
11. [`11-web-ui-workbench.md`](./11-web-ui-workbench.md) — goal input, active goal feed, task graph
12. [`12-web-ui-pipeline-monitor.md`](./12-web-ui-pipeline-monitor.md) — pipeline list, status, runs
13. [`13-github-pr-integration.md`](./13-github-pr-integration.md) — branch, commit, PR open
14. [`14-recovery-agent.md`](./14-recovery-agent.md) — autonomous fix loop on `carve run` failures (depends on the specialist agents from 02–05)

## Definition of done

- All 14 specs implemented with tests
- Acceptance criteria above met
- A 5-minute screen recording of the demo flow exists
- Internal tag `v0.0.5`
