# Milestone 3 — Polish for adoption

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

## Definition of done

- All 13 specs implemented
- Acceptance criteria above met
- v0.1.0 tagged and pushed
- Launch blog post written
- Posted to Hacker News, dbt Slack, Locally Optimistic, /r/dataengineering
