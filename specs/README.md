# Carve — design documents

This folder contains the full product and engineering specification for **Carve**, an AI-first open-source framework for data engineering and analytics engineering. It captures every architectural decision, design choice, and build plan from the project's brainstorming phase, organized so an engineering team can pick it up and execute.

## Document map

### Top-level

- [`PRD.md`](./PRD.md) — the master product requirements document. The single source of truth for what Carve is, who it's for, and what it does.
- [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) — the six-week build plan, broken into three milestones, with a day-by-day schedule.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — the technical architecture deep-dive: components, data flow, extension points, and the boundary between OSS and SaaS.

### Milestone 1 — walking skeleton (week 1)

The smallest end-to-end loop that proves the core idea works. CLI only, one agent, one step type, no UI.

- [`milestone-1-walking-skeleton/README.md`](./milestone-1-walking-skeleton/README.md) — overview and acceptance criteria
- [`01-cli-foundation.md`](./milestone-1-walking-skeleton/01-cli-foundation.md)
- [`02-config-loader.md`](./milestone-1-walking-skeleton/02-config-loader.md)
- [`03-state-store.md`](./milestone-1-walking-skeleton/03-state-store.md)
- [`04-anthropic-agent-loop.md`](./milestone-1-walking-skeleton/04-anthropic-agent-loop.md)
- [`05-python-step-and-runner.md`](./milestone-1-walking-skeleton/05-python-step-and-runner.md)
- [`06-snowflake-connector.md`](./milestone-1-walking-skeleton/06-snowflake-connector.md)

### Milestone 2 — real product (weeks 2-3)

The version you'd publish to GitHub and start showing people. Multiple agents, plan/apply workflow, dbt integration, basic web UI, brownfield onboarding.

- [`milestone-2-real-product/README.md`](./milestone-2-real-product/README.md) — overview and acceptance criteria
- [`01-plan-apply-workflow.md`](./milestone-2-real-product/01-plan-apply-workflow.md)
- [`02-orchestration-agent.md`](./milestone-2-real-product/02-orchestration-agent.md)
- [`03-dbt-agent.md`](./milestone-2-real-product/03-dbt-agent.md)
- [`04-snowflake-agent.md`](./milestone-2-real-product/04-snowflake-agent.md)
- [`05-dbt-integration.md`](./milestone-2-real-product/05-dbt-integration.md)
- [`06-brownfield-onboarding.md`](./milestone-2-real-product/06-brownfield-onboarding.md)
- [`07-convention-inference.md`](./milestone-2-real-product/07-convention-inference.md)
- [`08-schema-retrieval.md`](./milestone-2-real-product/08-schema-retrieval.md)
- [`09-fastapi-server.md`](./milestone-2-real-product/09-fastapi-server.md)
- [`10-websocket-streaming.md`](./milestone-2-real-product/10-websocket-streaming.md)
- [`11-web-ui-workbench.md`](./milestone-2-real-product/11-web-ui-workbench.md)
- [`12-web-ui-pipeline-monitor.md`](./milestone-2-real-product/12-web-ui-pipeline-monitor.md)
- [`13-github-pr-integration.md`](./milestone-2-real-product/13-github-pr-integration.md)

### Milestone 3 — polish for adoption (weeks 4-6)

The version that lets a stranger from the internet succeed without your help. Multi-step pipelines, MCP integrations, embeddings, the remaining UI screens, docs, examples.

- [`milestone-3-polish/README.md`](./milestone-3-polish/README.md) — overview and acceptance criteria
- [`01-multi-step-pipelines.md`](./milestone-3-polish/01-multi-step-pipelines.md)
- [`02-sql-step-type.md`](./milestone-3-polish/02-sql-step-type.md)
- [`03-shell-http-steps.md`](./milestone-3-polish/03-shell-http-steps.md)
- [`04-mcp-client.md`](./milestone-3-polish/04-mcp-client.md)
- [`05-quality-agent.md`](./milestone-3-polish/05-quality-agent.md)
- [`06-skills-sdk.md`](./milestone-3-polish/06-skills-sdk.md)
- [`07-custom-step-types.md`](./milestone-3-polish/07-custom-step-types.md)
- [`08-embedding-search.md`](./milestone-3-polish/08-embedding-search.md)
- [`09-web-ui-agent-studio.md`](./milestone-3-polish/09-web-ui-agent-studio.md)
- [`10-web-ui-dbt-run-view.md`](./milestone-3-polish/10-web-ui-dbt-run-view.md)
- [`11-example-projects.md`](./milestone-3-polish/11-example-projects.md)
- [`12-documentation-site.md`](./milestone-3-polish/12-documentation-site.md)
- [`13-doctor-command.md`](./milestone-3-polish/13-doctor-command.md)

### Reference

- [`reference/config-schema.md`](./reference/config-schema.md) — full TOML/YAML schema reference for `carve.toml` and the `carve/` config directory
- [`reference/cli-reference.md`](./reference/cli-reference.md) — every CLI command, flag, and exit code
- [`reference/governance.md`](./reference/governance.md) — open-source governance, contributor model, license choice
- [`reference/glossary.md`](./reference/glossary.md) — definitions of the terms used throughout these docs

## How to use these docs

If you're picking this project up cold:

1. Read [`PRD.md`](./PRD.md) end to end. It's the most important document — everything else is implementation detail.
2. Read [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) to understand the milestone-by-milestone delivery shape.
3. Skim [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the technical model.
4. Open the milestone-1 folder and start building.

If you're contributing to a specific area:

1. Find the relevant spec in the milestone folders.
2. Each spec is self-contained — it lists its dependencies on other specs at the top.
3. Specs include scope, interfaces, file paths, acceptance criteria, and estimated effort.

## Status

These documents represent the design phase. No code has been written yet. The schedule in [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) assumes one focused engineer using AI coding tools aggressively and starts from a green field.
