# Carve — Project Plan

A six-week build plan to ship Carve v0.1.0 from a green field. The schedule assumes one focused engineer using AI coding tools aggressively. If the work happens nights and weekends, double the timeline. If the team is two engineers, parallel paths exist within each milestone but the critical path doesn't shrink linearly.

## Milestone overview

| Milestone | Duration | Goal |
|---|---|---|
| **M1 — Walking skeleton** | Week 1 | Smallest end-to-end loop that proves the architecture |
| **M2 — Real product** | Weeks 2-3 | Shippable to GitHub, useful for the primary persona |
| **M3 — Polish for adoption** | Weeks 4-6 | Strangers can succeed without help; v0.1.0 release |

Each milestone is a usable product. Demo it, get feedback, then start the next.

## Guiding principles

- **Ship before perfect.** The version that gets feedback in week 2 will be more valuable than the one that ships in month 6 with three more features.
- **Pick boring technology.** FastAPI, SQLite, SQLAlchemy, React + Vite, Tailwind, click or typer, pydantic, jinja. Save the novelty budget for the agent layer.
- **Skip the SaaS scaffolding.** The runner abstraction matters because the SaaS pivot exists later. The implementations don't.
- **Cut the agent count for v0.1.** Five agents is the long-term picture. Start with two: orchestration and a combined "code" agent. Split as needed.
- **Defer extension points.** Hard-code built-in skills and step types until they've stabilized. The SDK comes in milestone 3.

## Milestone 1 — Walking skeleton (week 1)

**Single goal:** prove an agent can take a natural-language request, generate working Python that connects to Snowflake, and execute it end-to-end.

### Day-by-day

**Day 1 — Project skeleton**
- `pyproject.toml` with `uv` (or `poetry`)
- Repo layout from `ARCHITECTURE.md`: `src/carve/`, `tests/`, `docs/`, `examples/`
- Pre-commit hooks: ruff, mypy
- Basic GitHub Actions CI: lint + test on push
- LICENSE (Apache 2.0), CONTRIBUTING.md stub, README.md stub
- Spec: [`milestone-1-walking-skeleton/01-cli-foundation.md`](./milestone-1-walking-skeleton/01-cli-foundation.md)

**Day 2 — Config and state store**
- `carve.toml` parsing with pydantic models
- Multi-file config loader (`carve/connections.toml`, `carve/runner.toml`, etc.)
- Environment variable interpolation
- SQLite state store with three tables: `runs`, `logs`, `plans`
- SQLAlchemy ORM models, basic repository pattern
- Specs: [`02-config-loader.md`](./milestone-1-walking-skeleton/02-config-loader.md), [`03-state-store.md`](./milestone-1-walking-skeleton/03-state-store.md)

**Day 3 — Anthropic client and basic agent loop**
- Anthropic SDK wrapper that handles tool-use turn-taking
- A combined "code" agent with hardcoded tools: read file, write file, run Snowflake query
- System prompt for the code agent (short, focused)
- Spec: [`04-anthropic-agent-loop.md`](./milestone-1-walking-skeleton/04-anthropic-agent-loop.md)

**Day 4 — Snowflake connection and Python step**
- Snowflake connector wrapper using `snowflake-connector-python`
- Connection management from `carve/connections.toml`
- `Step` protocol and `PythonStep` implementation
- `LocalVenvRunner` that creates venvs, installs deps, executes scripts, captures stdout/stderr
- Specs: [`05-python-step-and-runner.md`](./milestone-1-walking-skeleton/05-python-step-and-runner.md), [`06-snowflake-connector.md`](./milestone-1-walking-skeleton/06-snowflake-connector.md)

**Day 5 — End-to-end wiring**
- `carve init` command: scaffolds project layout
- `carve plan "<goal>"` command: invokes agent, returns plan as JSON
- `carve apply <plan_id>` command: executes the plan
- `carve run <pipeline>` command: runs a pipeline through the runner
- Run history persists to state store
- Demo target: `carve plan "ingest a CSV from a public URL into my dev Snowflake schema"` → plan → apply → data lands in Snowflake

**Days 6-7 — Buffer**
- Whatever broke. There will be something.
- Polish error messages and CLI output formatting
- Write a one-page README walkthrough so the demo is repeatable
- Tag `v0.0.1` internally; share with three trusted reviewers

### Acceptance criteria for M1

A new user can clone the repo, run `carve init`, edit a config file with their Snowflake credentials, run `carve plan "<reasonable goal>"`, run `carve apply`, and end up with data in their warehouse. The whole flow takes under 10 minutes, all from the CLI.

## Milestone 2 — Real product (weeks 2-3)

**Goal:** the version you'd publish to GitHub. Multiple agents, plan/apply workflow with PRs, dbt integration, basic web UI, brownfield onboarding.

### Week 2

**Day 8 — Plan/apply formalization**
- Plan files written to `.carve/plans/<plan_id>.json`
- Plan schema with task graph, cost estimates, file diffs, config hash, expiry
- `carve plan show <plan_id>`, `carve plan list`, `carve plan diff`
- Apply checks config hash before executing
- Spec: [`milestone-2-real-product/01-plan-apply-workflow.md`](./milestone-2-real-product/01-plan-apply-workflow.md)

**Day 9 — Orchestration agent**
- Split the M1 "code" agent into orchestration + dbt + Snowflake
- Orchestration agent does goal classification, impact analysis (stub), and agent selection
- Task graph generation
- Specs: [`02-orchestration-agent.md`](./milestone-2-real-product/02-orchestration-agent.md), [`03-dbt-agent.md`](./milestone-2-real-product/03-dbt-agent.md), [`04-snowflake-agent.md`](./milestone-2-real-product/04-snowflake-agent.md)

**Day 10 — dbt integration**
- `dbt` step type
- `DbtRunner` that shells out to `dbt-core` with the right project_dir, profiles_dir, and target
- Manifest reader that loads `target/manifest.json` and exposes structured queries
- Specs: [`05-dbt-integration.md`](./milestone-2-real-product/05-dbt-integration.md)

**Day 11 — Brownfield `carve init`**
- Detect existing `dbt_project.yml`
- Read existing `profiles.yml` rather than overwriting
- Run `dbt parse` to build initial manifest cache
- Spec: [`06-brownfield-onboarding.md`](./milestone-2-real-product/06-brownfield-onboarding.md)

**Day 12 — Convention inference**
- Analyze model names, materializations, test patterns, SQL style
- Generate `carve/conventions.md` from the analysis
- Include conventions doc in agent system prompts
- Spec: [`07-convention-inference.md`](./milestone-2-real-product/07-convention-inference.md)

**Days 13-14 — Schema retrieval and end-to-end test**
- Implement structured catalog query skills
- Implement dbt manifest query skills
- Wire skills into the orchestrator's pre-scoping step
- Test against a real existing dbt project; fix what breaks
- Spec: [`08-schema-retrieval.md`](./milestone-2-real-product/08-schema-retrieval.md)

### Week 3

**Day 15 — FastAPI server**
- REST endpoints for: list pipelines, list runs, trigger run, get run status, list plans
- Auth: single API key from env var
- Spec: [`09-fastapi-server.md`](./milestone-2-real-product/09-fastapi-server.md)

**Day 16 — WebSocket log streaming**
- WebSocket endpoint for run events and log lines
- The CLI's `carve logs --follow` uses the same WebSocket
- Spec: [`10-websocket-streaming.md`](./milestone-2-real-product/10-websocket-streaming.md)

**Days 17-18 — Workbench screen**
- React + Vite + Tailwind + shadcn project under `src/carve/ui/`
- Workbench: goal input, active goal feed with task graphs, artifact preview
- Live updates via WebSocket
- FastAPI serves the built `dist/` assets
- Spec: [`11-web-ui-workbench.md`](./milestone-2-real-product/11-web-ui-workbench.md)

**Day 19 — Pipeline monitor screen**
- Pipeline list with status, last run, schedule
- Click-through to run history
- Spec: [`12-web-ui-pipeline-monitor.md`](./milestone-2-real-product/12-web-ui-pipeline-monitor.md)

**Day 20 — GitHub PR integration**
- After `carve apply`, generated artifacts are committed to a feature branch
- A PR is opened against the configured default branch
- PR description includes the plan summary, file diffs, impact analysis
- Spec: [`13-github-pr-integration.md`](./milestone-2-real-product/13-github-pr-integration.md)

**Day 21 — Buffer and brownfield dogfooding**
- Point Carve at three different test repos. Fix everything that breaks.
- Tag `v0.0.5` and share with five trusted reviewers including at least one outside the team

### Acceptance criteria for M2

A data engineer with an existing dbt project can:

1. Run `carve init` in their repo
2. Have Carve detect their dbt project and generate a conventions doc
3. Run `carve plan "make stg_orders incremental"` and see a sensible plan
4. Run `carve apply` and see a PR opened in their GitHub repo
5. Watch the run live in the web UI's workbench
6. See pipeline runs in the pipeline monitor

## Milestone 3 — Polish for adoption (weeks 4-6)

**Goal:** remove friction so the first hundred users you don't know personally can succeed on their own. v0.1.0 release at the end of week 6.

### Week 4

**Days 22-23 — Multi-step pipelines**
- Step DAG executor with `depends_on`, parallel execution, failure modes
- New step types: `sql`, `shell`, `http`
- Pipeline TOML schema with `[[steps]]`
- Specs: [`milestone-3-polish/01-multi-step-pipelines.md`](./milestone-3-polish/01-multi-step-pipelines.md), [`02-sql-step-type.md`](./milestone-3-polish/02-sql-step-type.md), [`03-shell-http-steps.md`](./milestone-3-polish/03-shell-http-steps.md)

**Days 24-25 — MCP client**
- MCP server config in `carve/mcp_servers.toml`
- MCP tools registered as namespaced skills (`mcp:server:tool`)
- Per-agent allowlists
- Test with the official Snowflake MCP server and the dbt MCP server
- Spec: [`04-mcp-client.md`](./milestone-3-polish/04-mcp-client.md)

**Day 26 — Quality agent**
- Split out from the dbt agent
- Generates dbt tests, source freshness checks, anomaly detection rules
- Spec: [`05-quality-agent.md`](./milestone-3-polish/05-quality-agent.md)

**Days 27-28 — Skills SDK**
- `@skill` decorator
- `SkillContext` interface
- Custom skill discovery from `carve/skills/*.py`
- Type stubs for IDE support
- Spec: [`06-skills-sdk.md`](./milestone-3-polish/06-skills-sdk.md)

### Week 5

**Day 29 — Custom step types**
- `StepType` base class
- Custom step type discovery from `carve/steps/*.py`
- Documentation and example
- Spec: [`07-custom-step-types.md`](./milestone-3-polish/07-custom-step-types.md)

**Days 30-31 — Embedding-based schema search**
- Embedding pipeline for dbt model docs, source docs, column comments
- Local ChromaDB store
- Semantic search skill that returns pointers, not full content
- Refresh on dbt build + scheduled
- Spec: [`08-embedding-search.md`](./milestone-3-polish/08-embedding-search.md)

**Days 32-33 — Agent studio screen**
- Agent list, agent editor with system prompt, model selection, skills, guardrails
- Skills tab
- Settings tab with MCP server management
- Edits commit to git
- Spec: [`09-web-ui-agent-studio.md`](./milestone-3-polish/09-web-ui-agent-studio.md)

**Days 34-35 — dbt run view screen**
- Lineage DAG with status colors
- Run summary, test results, failure investigation flow
- "Investigate" button that hands the failure back to the dbt agent
- Spec: [`10-web-ui-dbt-run-view.md`](./milestone-3-polish/10-web-ui-dbt-run-view.md)

### Week 6

**Days 36-37 — Example projects**
- Three working examples: `salesforce-mart`, `stripe-revenue`, `postgres-sync`
- Each example has a README walkthrough, sample data, expected outputs
- CI runs the examples end-to-end on every PR
- Spec: [`11-example-projects.md`](./milestone-3-polish/11-example-projects.md)

**Days 38-39 — Documentation site**
- mkdocs-material site under `docs/`
- Concepts (steps, agents, plan-and-apply, schema-context)
- Guides (first pipeline, brownfield setup, writing a custom skill)
- Reference (CLI, config schema, glossary)
- Auto-deploy via GitHub Actions
- Spec: [`12-documentation-site.md`](./milestone-3-polish/12-documentation-site.md)

**Day 40 — Doctor command**
- `carve doctor` runs a checklist: LLM provider, Snowflake, dbt installed, state store, venvs cached, MCP servers
- Spec: [`13-doctor-command.md`](./milestone-3-polish/13-doctor-command.md)

**Days 41-42 — Release prep and launch**
- Final dogfooding pass against five different real-world projects
- Write the launch blog post
- Polish the README (the most-read piece of marketing the project has)
- Set up `carve-data/carve` GitHub org and repo if not already
- Tag `v0.1.0`
- Post to Hacker News, the dbt Slack, the Locally Optimistic Slack, /r/dataengineering

### Acceptance criteria for M3

A stranger from the internet can:

1. Read the README, decide it's worth trying
2. Clone Carve, run `carve init`, configure their connections
3. Try the first example project successfully
4. Generate their first PR against their own dbt project
5. All within 20 minutes, without messaging anyone for help

## Risk timeline

Risks that materialize at specific points in the schedule:

- **Week 1 risk:** the agent loop is harder than expected. Mitigation: the M1 demo is intentionally narrow. If the agent struggles with broad goals, narrow the M1 demo target further until something works.
- **Week 2 risk:** brownfield detection has edge cases. Mitigation: have at least three real dbt projects ready to test against during week 2. Don't theorize — measure.
- **Week 3 risk:** the web UI takes longer than budgeted. Mitigation: ship just two screens for M2. The agent studio and dbt run view are explicitly deferred to M3.
- **Week 4-5 risk:** MCP integration discovers protocol issues with specific servers. Mitigation: pick two MCP servers known to work well (Snowflake's official, dbt's) for the initial integration. Don't try to be compatible with all servers simultaneously.
- **Week 6 risk:** docs and polish always take longer than expected. Mitigation: docs writing starts in week 5. Don't leave it for the final week.

## Schedule realism

The schedule above is achievable for an experienced engineer using AI coding tools aggressively. Specifically:

- Most of the code is well-specified enough that Claude Code can produce solid first drafts
- The engineer's time goes to architecture decisions, integration debugging, prompt engineering for the agents, and the genuinely tricky bits (orchestration, brownfield detection, embedding pipeline)
- Code review and integration testing are the bottleneck, not raw typing speed

If the schedule slips, the slip almost always falls in one of three places:
1. Brownfield edge cases (week 2)
2. Web UI polish (weeks 3, 5)
3. Documentation completeness (week 6)

Plan for those slips. If you have to cut, cut the embedding-based search from M3 (defer to v0.2) and ship without it. The catalog and manifest queries cover most use cases.

## What this plan deliberately defers

- **Multi-LLM-provider support** — Anthropic-only for v0.1. OpenAI etc. comes when someone asks.
- **Docker runner** — `LocalVenvRunner` only for v0.1.
- **Multi-user authentication** — single user only.
- **MCP server (Carve as MCP server)** — consumed but not exposed in v0.1.
- **Visual pipeline editor** — TOML-only authoring, AI-first.
- **BigQuery, Databricks, Redshift adapters** — Snowflake-only.
- **dbt Cloud as an executor backend** — supported as a stretch goal in M3 if time permits, otherwise deferred.

## What happens after v0.1.0

The first 30 days post-launch are about listening, not building. The roadmap is shaped by real user feedback. Early candidates for v0.2:

- BigQuery adapter
- OpenAI / multi-provider support
- Docker runner
- Multi-user authentication
- Embedding search if cut from M3

But these are guesses. The actual v0.2 priorities come from the issues and PRs that arrive in the first month.
