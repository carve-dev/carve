# Carve — Product Requirements Document

> Carve structure from chaos.

## 1. Vision

Carve is an open-source, AI-first framework that builds and operates the modern data stack. Users describe what they want — a new source onboarded, a model refactored, a quality test added, a stored procedure called after a dbt run — and Carve's agents generate the code, run it, and surface the results for review.

The entire data stack, authored by intent.

Carve combines three things that most data tools keep separate:

1. **AI authoring** — agents that generate Python pipeline code, dbt models, Snowflake DDL, tests, and documentation
2. **A lightweight execution engine** — runs the resulting code on a schedule, no Airflow, no Dagster, no cluster manager
3. **A unified web UI and CLI** — for monitoring, configuring, and intervening

The opinion: for the 80% of data work that's well-trodden territory (ingest, model, test, document), AI-first authoring is faster and more consistent than hand-coding. For the rest, Carve gets out of the way and lets engineers do their job.

## 2. Who Carve is for

### Primary persona — the staff data engineer at a mid-size company

Owns the data platform. Has dbt running on Snowflake. Maintains a few dozen pipelines. Spends most of their week onboarding new sources, refactoring models, fixing failed runs, and answering questions about the pipeline. They're comfortable in Python and SQL, they live in dbt, they tolerate Airflow, and they wish they could delegate the routine 60% of their work.

This person is the buyer. They install Carve themselves, get value within an hour, and decide whether to roll it out to their team.

### Secondary persona — the analytics engineer

Lives in dbt. Doesn't write Python pipelines but maintains a large dbt project with many contributors. Wants AI assistance for model authoring, refactoring, and test generation that respects their team's conventions. Less interested in the execution engine, more interested in the dbt agent and the convention-aware authoring loop.

### Anti-persona — the platform team at a 10,000-person enterprise

Carve is not designed for the long tail of enterprise platform requirements (multi-cluster orchestration, fine-grained RBAC, integration with proprietary identity providers, SLA contracts). Those teams will be better served by Airflow, Dagster, or eventually a Carve enterprise tier. v0.x targets the SMB-to-mid-market segment.

## 3. The core loop

Every interaction with Carve flows through this loop:

```
Intent → Plan → Review → Deploy → Execute → Observe
```

1. **Intent** — the user expresses a goal in natural language ("onboard Salesforce", "make `stg_orders` incremental", "add freshness checks to all marts")
2. **Plan** — Carve's orchestration agent decomposes the goal into a task graph, picks the right specialist agents, gathers context, and produces a structured plan with cost estimates
3. **Review** — the user reviews the plan, refines it, or rejects it
4. **Deploy** — Carve's agents generate code; changes land as a git pull request
5. **Execute** — once merged, the code runs through Carve's execution engine on its configured schedule
6. **Observe** — the web UI surfaces what's happening; the user intervenes when needed

The loop applies whether the user is building a new pipeline, modifying an existing dbt model, configuring an agent, or adjusting a guardrail.

## 4. Scope

### In scope for the v0.1 open-source release

- Authoring of Python ingestion pipelines for arbitrary source systems
- Authoring, modification, and refactoring of dbt models, tests, and documentation
- Authoring of Snowflake DDL: schemas, tables, views, roles, warehouses, grants
- Multi-step pipelines with Python, SQL, dbt, shell, and HTTP steps
- A plan/deploy workflow with persisted plans
- A scheduling layer that triggers pipelines based on cron expressions
- A web UI with four screens: workbench, agent studio, pipeline monitor, dbt run view
- A CLI with parity for every UI action
- Brownfield onboarding for existing dbt projects
- Convention inference from existing dbt projects
- Schema retrieval skills (catalog queries, dbt manifest queries, lineage traversal)
- MCP client integration (consume external MCP servers as skills)
- Single-user authentication
- A skills SDK and custom step type SDK for extension
- Three working example projects
- Documentation site

### Out of scope for v0.1

- BigQuery, Databricks, Redshift, or other warehouses (Snowflake-only)
- Multi-LLM-provider support (Anthropic-only; abstraction prepared but unused)
- Multi-user authentication, SSO, RBAC
- Docker or Kubernetes runners (`LocalVenvRunner` only)
- Embedding-based semantic search over schema (deferred to Pillar 4 or later)
- MCP server (Carve as MCP server for outside agents)
- Visual pipeline builder
- Looker, Tableau, or other BI integrations
- Reverse-ETL integrations (Hightouch, Census)
- Carve as a hosted SaaS

### Future, not now

The architecture is designed to support these without rewriting:

- A SaaS hosted version with `DockerRunner`, multi-tenancy, SSO, managed scheduling
- BigQuery and Databricks adapters
- Multi-LLM-provider support (OpenAI, Google, local models)
- A marketplace of community-contributed agents and skills
- Federation between multiple Carve instances

## 5. Key design decisions

These are the decisions that shape everything else. Each is recorded with the reasoning behind it.

### 5.1 AI-first authoring, not AI-assisted

The natural way to create or modify anything in Carve is to describe it in natural language. Hand-editing TOML, YAML, or Python is the escape hatch — supported, well-documented, but not the primary path.

This is a stronger position than "AI-assisted." It means the CLI's headline command is `carve plan "<goal>"`, not `carve scaffold pipeline <name>`. It means the agent studio's default editing mode is "describe what you want to change," not "edit the YAML." It means documentation leads with what to ask the agent, not what the schema looks like.

### 5.2 Carve owns the execution layer

Some early designs treated Carve purely as a code generator that handed off to an existing orchestrator (Airflow, Dagster). We rejected this because:

- Most target users don't already have an orchestrator running, and forcing one as a dependency is friction
- Owning execution means Carve can give a unified UX for monitoring, retries, and intervention
- The execution layer is small enough to build (a process runner, not a cluster manager)

The runner abstraction (`LocalVenvRunner`, future `DockerRunner`) is what makes this lightweight. Carve is not competing with Airflow on scale; it's offering a simpler model for teams that don't need that scale.

### 5.3 Steps are the unit of execution, not pipelines

A pipeline is a sequence of steps. Each step has a type (`python`, `sql`, `dbt`, `shell`, `http`, `agent`, `approval`), runs in a DAG with explicit dependencies, has its own retries and failure modes, and produces structured outputs that downstream steps can reference.

This generalizes the original "pipeline = Python script" model to handle the long tail of real data work — call a stored procedure after dbt, refresh a search-optimized table, post to Slack on completion.

### 5.4 Plan/deploy, not build-and-go

Every change goes through a plan/deploy lifecycle, modeled on `terraform plan`/`terraform apply`. The plan is a serializable artifact that includes the task graph, cost estimate, file diffs, and impact analysis. Plans can be saved, refined, diffed, and deployed later.

`carve build` is a convenience that combines plan + interactive confirm + deploy. The underlying primitive is always plan/deploy.

### 5.5 Code is the source of truth, UI is the editor

Agent definitions live in `carve/agents/*.yaml`. Skills live in `carve/skills/*.py`. Pipelines live in `carve/pipelines/*.toml`. Connections live in `carve/connections.toml`. The web UI reads from and writes to these files. Every UI edit becomes a git commit.

This gives version control, reviewability, rollback, disaster recovery, and portability for free. The cost is some implicit complexity — non-engineers see git mechanics. We accept this for the OSS audience (mostly engineers) and hide it in the SaaS version (mostly not).

### 5.6 Config is split across multiple files from day one

`carve.toml` at the root is small and meta. The actual config lives in `carve/connections.toml`, `carve/runner.toml`, `carve/guardrails.toml`, etc. Pipelines are one file each in `carve/pipelines/`. This mirrors how `dbt`, `terraform`, and `kubernetes` already organize themselves and avoids painful future migrations.

### 5.7 The orchestration agent is the only agent that knows about other agents

Specialist agents (dbt, Snowflake, pipeline, quality) don't coordinate with each other. They each work on pre-scoped context handed to them by the orchestrator. This keeps each agent focused, independently testable, and swappable.

### 5.8 Schema context is a retrieval problem, not a context-window problem

Real warehouses have thousands of tables. Stuffing the catalog into LLM context is impossible and unnecessary. Carve solves this with layered retrieval: structured catalog queries for facts, dbt manifest queries for dependencies, embedding-based semantic search for fuzzy concepts, grep for exact references, and lineage traversal for impact. The orchestrator pre-scopes context before invoking specialist agents.

### 5.9 SQLite first, Postgres later

The state store starts on SQLite, accessed through SQLAlchemy. This is zero-config, ships as a single file, and handles the load of a small data team easily. The SaaS version migrates to Postgres via a connection string change, no code rewrite.

### 5.10 MCP both ways

Carve consumes external MCP servers as skills (Snowflake's official MCP server, dbt-labs' MCP server, GitHub's MCP server), and Carve exposes itself as an MCP server so other agents (Claude Desktop, Cursor, Claude Code) can drive it. This is what plugs Carve into the broader AI tooling ecosystem instead of trying to be an island.

### 5.11 Roles: Viewer, Creator, Admin

Three roles cover ~99% of real use cases. Viewers see; Creators build and own their own work; Admins can intervene in others' work. Resist adding more roles until real customers ask.

### 5.12 Apache 2.0 license, DCO from day one

Apache 2.0 is the license the data ecosystem expects. A Developer Certificate of Origin (DCO) — signed commits, no separate document — preserves the option to dual-license for SaaS later without scaring contributors away.

## 6. Functional requirements

### 6.1 Project initialization

- `carve init` creates a working project skeleton in the current directory
- Detects existing dbt projects and integrates rather than overwrites
- Generates a `carve/conventions.md` from existing project patterns when present
- Creates `.env.example` and adds Carve-specific entries to `.gitignore`
- Initializes a git repo if one doesn't exist

### 6.2 Plan generation

- `carve plan "<goal>"` produces a saved plan with task graph, cost estimate, expected file diffs, and impact analysis
- Plans include a config hash to detect drift before deploy
- Plans expire after 24 hours by default (configurable)
- `carve plan --refine <plan_id> "<adjustment>"` produces a refined plan with parent lineage

### 6.3 Plan deployment

- `carve deploy <pipeline_name>` executes a saved plan
- Deploy checks the config hash and refuses to run against drifted config
- Generated artifacts are committed to a feature branch and opened as a pull request
- The PR description includes the plan summary, file diffs, and impact analysis

### 6.4 Pipeline execution

- `carve run <pipeline>` executes a pipeline on demand
- Each step in a pipeline executes in dependency order, in parallel when possible
- Step failures are configurable per step: `fail`, `warn`, `continue`, `retry`, `skip_downstream`
- Logs stream live through the API server's WebSocket and to stdout for CLI invocations
- Failed runs can be retried at the step level via UI or CLI

### 6.5 Scheduling

- Pipelines declare cron-style schedules in their TOML files
- An internal scheduler reads `carve/pipelines/*.toml` and triggers runs at the configured times
- The scheduler runs as part of `carve serve`; it does not require a separate process

### 6.6 Agent configuration

- Each agent has a YAML definition with system prompt, model selection, allowed skills, and guardrails
- Agent definitions are versioned (committed to git); the agent studio UI generates commits on edits
- Guardrails are validated before execution, not as suggestions in the prompt

### 6.7 Skills

- Built-in skills ship with Carve and live in the source tree
- Custom skills are Python files in `carve/skills/` that use a `@skill` decorator
- Skills declare typed inputs/outputs and a description (consumed by the LLM as a tool schema)
- Skills receive a `SkillContext` providing access to connections, the current run's logger, and event emission

### 6.8 Steps

- Built-in step types: `python`, `sql`, `dbt`, `shell`, `http`, `agent`, `approval`
- Custom step types are Python classes inheriting from `StepType`
- Steps declare a config schema (Pydantic models) validated before invocation
- Steps support Jinja templating for cross-step parameter passing

### 6.9 Web UI

- **Workbench** — goal input, active goal feed, task graph view, artifact preview
- **Agent studio** — agent configuration, skills tab, settings tab
- **Pipeline monitor** — pipeline list, summary metrics, status pills, run history
- **dbt run view** — lineage DAG, run summary, test results, failure investigation

### 6.10 CLI

- Every UI action has a CLI equivalent
- Output formats: table (terminal default), JSON (piping default), YAML
- Exit codes are documented and stable for CI integration
- Log streaming via `carve logs <run_id> --follow`

### 6.11 MCP integration

- Servers declared in `carve/mcp_servers.toml`
- MCP tools appear as namespaced skills (`mcp:server:tool`)
- Per-agent allowlists for which MCP tools each agent may use
- A passthrough mode exposes Carve's own capabilities as MCP tools

### 6.12 Observability

- Every run records: duration, cost (LLM tokens + Snowflake credits), step-level status, structured logs
- Optional OpenTelemetry export for traces
- Optional webhook emission of run events for Slack, PagerDuty, etc.

## 7. Non-functional requirements

### 7.1 Performance

- `carve init` completes in under 30 seconds for a greenfield project, under 5 minutes for a brownfield project with manifest analysis
- `carve plan` for a typical modification goal completes in under 15 seconds (excluding LLM latency)
- `carve deploy` for a typical modification produces a PR within 60 seconds
- Pipeline run startup overhead is under 10 seconds
- Web UI feels live: status updates within 500ms of state changes

### 7.2 Reliability

- Failed runs leave the state store in a consistent state
- Crashed processes can be detected and runs marked as failed
- Plan files include a hash of the config they were generated against; deploy validates this hash

### 7.3 Security

- No secrets in `carve.toml` or any committed file; all secrets via environment variable interpolation
- Generated pipeline code runs in an isolated venv
- Snowflake credentials never logged
- LLM provider API keys never appear in run logs
- All file system writes are scoped to the project directory and `.carve/`

### 7.4 Compatibility

- Python 3.11+
- dbt-core 1.7+
- Snowflake (any current version)
- Linux and macOS supported; Windows on best-effort

### 7.5 Resource consumption

- Idle Carve server uses under 200MB RAM
- SQLite state store stays under 500MB even after a year of typical use
- Generated venvs are cached and reused; total disk usage capped at 5GB by default

## 8. Success metrics for v0.1.0

These are the indicators that Carve has product-market fit at the OSS layer:

### 8.1 Adoption metrics

- 1,000+ GitHub stars within the first 90 days
- 100+ unique installations confirmed via opt-in telemetry
- 20+ external pull requests within 90 days

### 8.2 Engagement metrics

- 50% of installations result in at least one successful `carve deploy`
- Median time from `carve init` to first successful pipeline run: under 30 minutes
- 30% of brownfield installations result in at least one merged PR within a week

### 8.3 Quality metrics

- Generated dbt models pass `dbt parse` 99% of the time
- Generated Python pipelines run successfully on first attempt 80% of the time
- Plan/deploy matches plan output 95% of the time (no surprise behavior at deploy)

### 8.4 Community signals

- A working community Slack or Discord with 500+ members
- At least three blog posts from external users about adopting Carve
- One conference talk proposal accepted (dbt Coalesce, Data Council, Apache Conference)

## 9. Risks and mitigations

### 9.1 Risk: agents generate plausible-looking but wrong code

This is the canonical AI-for-code risk. Mitigations:

- The plan/deploy workflow surfaces what will happen before it happens
- Generated code lands as PRs with CI checks (dbt parse, dbt test on dev, lint)
- Convention inference grounds output in the team's existing patterns
- Skills are deterministic where possible (catalog queries return facts, not LLM guesses)

### 9.2 Risk: schema context blows up the LLM context window

Mitigated by the layered retrieval architecture (see decision 5.8). The orchestrator pre-scopes context. Specialist agents work on small focused inputs. Embedding search is bounded; results are pointers, not full content.

### 9.3 Risk: the OSS execution engine is a long-term maintenance burden

This is real. Building Carve's own execution engine means owning the long tail of "what if dbt-core 1.9 changes its CLI interface" or "what if a venv install fails on this niche Linux distro." Mitigations:

- Keep the engine deliberately thin — process runner with logging, not a cluster manager
- Lean on dbt-core's own DAG management instead of reimplementing
- Invest in a strong test suite of integration scenarios
- Set clear boundaries: Carve doesn't try to outdo Dagster on scale or Airflow on operators

### 9.4 Risk: positioning conflict with dbt Cloud / Dagster / Prefect

Carve overlaps with each of these. Mitigations:

- Be explicit about positioning: AI-first authoring is the wedge, not orchestration
- Integrate cleanly with dbt Cloud (the `dbt` step type can target dbt Cloud's API)
- Don't compete on features these tools do well; compete on the authoring experience

### 9.5 Risk: SaaS pivot is harder than expected

The OSS-to-SaaS pivot is a real path but historically tricky. Mitigations:

- Design the runner abstraction, auth, and event bus from day one to support multi-tenancy
- Use SQLAlchemy from the start for SQLite → Postgres portability
- Avoid OSS-specific design choices (SQLite-only assumptions, single-process state) that would block scaling

### 9.6 Risk: contributor burnout from a single maintainer

If the project takes off, one maintainer can't keep up. Mitigations:

- Strong contributor documentation and clear extension points (skills, custom step types) so contributors can land features without core changes
- Triage issue templates that pre-filter low-quality reports
- A roadmap document that says "no" to off-roadmap features by default
- Plan for a second maintainer by month 6 if traction is real

## 10. Out-of-scope clarifications

A few things explicitly *not* part of Carve, to prevent scope creep:

- Carve is **not a data quality tool** in the Great Expectations / Soda sense. It generates tests; it doesn't run a separate quality monitoring product.
- Carve is **not a data catalog**. It indexes schema for retrieval; it doesn't provide a discoverability product for analysts.
- Carve is **not a BI tool**. It builds the data warehouse; it doesn't visualize it.
- Carve is **not a reverse-ETL tool**. It writes to the warehouse; it doesn't sync to operational systems.
- Carve is **not a notebook environment**. Pipelines are scripts and SQL files; not interactive notebooks.

These are healthy adjacent product spaces, all of which Carve might integrate with later. None are part of the core.

## 11. Open questions

These are decisions deferred to implementation or to later releases:

- Final choice of CLI framework (`click` vs `typer` vs `cyclopts`) — already settled in M1: `typer`.
- Final choice of frontend framework (React + Vite + Tailwind + shadcn is the working assumption) — implementation decision when the UI milestone arrives (post-Pillar-4).
- Whether to support `dbt` 1.6 or only 1.7+ — minor compatibility decision; resolved in Pillar 2.
- Whether the documentation site uses `mkdocs-material` or `docusaurus` — implementation decision when the docs site lands (likely alongside or after Pillar 4).

## 12. Appendix — naming and brand

- Project name: **Carve**
- GitHub org: `carve-data` (placeholder; check availability before claiming)
- Tagline: "Carve structure from chaos" or "AI-powered data engineering, from raw to refined"
- Brand vocabulary: agents *chisel*, skills are *blades*, workflow definitions are *cuts*, output artifacts are *carvings*. Use sparingly — don't overdose on the metaphor.
- Color: defer to a UI / branding pass (post-Pillar-4 or alongside).
