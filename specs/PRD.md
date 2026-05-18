# Carve — Product Requirements Document (rewrite draft, 2026-05-14)

> Draft. Builds up section-by-section against [`2026-05-positioning.md`](./2026-05-positioning.md). Replaces `specs/PRD.md` when complete.

## 1. Vision

Carve is an open-source, agent-driven layer that **authors, runs, and observes data pipelines** on top of two beloved OSS tools: **dlt** (extract & load) and **dbt** (transform). You describe what you want — a new source onboarded, a model refactored, a quality test added — and Carve's agents generate the code, schedule it, run it, and surface the results. You can drive Carve from its CLI, from a chat tool like Claude Desktop or Cursor via MCP, from your own agents over a REST API, or from a local web UI. **Carve is headless by default**; the interface is up to you.

The whole warehouse lifecycle in one place, authored by intent, accessible from anywhere.

Carve combines four things that most data tools keep separate:

1. **AI authoring across the warehouse** — agents that generate dlt sources/resources, dbt models and tests, and the SQL glue between them
2. **A narrow, opinionated runtime** — scheduled execution of dlt + dbt + SQL pipelines, with multi-worker job claiming, structured logs, retries, and alerts. Deliberately *not* a general-purpose orchestrator: no asset graphs, no arbitrary DAGs, no plugin operators.
3. **A programmable surface — CLI, REST API, and MCP server** — every action available to Carve's own agents is available to any external agent, chat tool, or script. The local web UI is just one client among many.
4. **A hosted product for teams that don't want to operate it themselves** — managed runtime, polished cloud UI, collaboration, SSO, push-button deploy

The opinion: for the 80% of warehouse work that's well-trodden territory (ingest, model, test, schedule), agent-driven authoring on top of dlt + dbt is faster, more consistent, and more maintainable than hand-coding. For the rest, Carve stays out of your way.

## 2. Who Carve is for

**Primary persona — the staff data engineer at a mid-size company.** Owns the data platform. Has dbt running on Snowflake (or BigQuery, Postgres, Databricks — increasingly Carve doesn't care). Maintains a few dozen pipelines. Spends most of their week onboarding new sources, refactoring models, fixing failed runs, and answering questions about the pipeline. They're comfortable in Python and SQL, they live in dbt, they tolerate Airflow, and they wish they could delegate the routine 60% of their work. This person is the buyer. They install Carve themselves, get value within an hour, and decide whether to roll it out to their team — first self-hosted via `docker compose up`, then often migrating to the hosted product once their team grows.

**Secondary persona — the analytics engineer.** Lives in dbt. Doesn't write Python pipelines but maintains a large dbt project with many contributors. Wants agent assistance for model authoring, refactoring, and test generation that respects their team's conventions. Less interested in the runtime, more interested in the dbt agent and the convention-aware authoring loop.

**Tertiary persona — the agent developer.** Building their own data-stack agent or platform. Wants a programmable backend that can plan/build/run pipelines via API or MCP, so they don't have to reinvent the dlt + dbt code generation, the runtime, and the observability. They consume Carve via the REST API or MCP server, not the CLI or web UI. This persona didn't exist in the old PRD; it does now because Carve is headless by default.

**Anti-persona — the platform team at a 10,000-person enterprise.** Carve is not designed for the long tail of enterprise platform requirements (multi-cluster orchestration, fine-grained RBAC across thousands of resources, integration with proprietary identity providers, SLA contracts beyond what the hosted product offers, custom asset-graph orchestration). Those teams will be better served by Airflow, Dagster, or — eventually — a Carve enterprise tier. v0.x targets the SMB-to-mid-market segment.

## 3. The core loop

Every interaction with Carve flows through this loop:

```
Intent → Plan → Build → Run → Deploy → Schedule → Observe
```

1. **Intent** — a user or external agent expresses a goal in natural language ("onboard Salesforce", "make `stg_orders` incremental", "add freshness checks to all marts"). Intent enters via CLI, REST API, MCP server, or web UI.
2. **Plan** — Carve's orchestration agent decomposes the goal, picks specialist agents (extract-load, dbt, runtime), gathers project context, and produces a structured plan with file diffs and cost estimates. The plan is reviewable, iterable (`plan --refine`), and a durable artifact in its own right.
3. **Build** — once the plan is approved, agents generate the code: dlt sources and resources, dbt models and tests, the SQL glue between them. Output lands in the project's working tree, not committed yet.
4. **Run** — the user (or agent, or scheduler) executes the new code against the dev target. Iteration is cheap — re-run, refine, re-run, until the rows look right.
5. **Deploy** — once dev iteration is done, deploy promotes the code from dev to prod. For OSS self-hosters, deploy opens a git PR. For the hosted product, deploy is push-button with audit log alongside the PR option.
6. **Schedule** — once merged, Carve's runtime picks up the pipeline's cron expression and fires runs against prod on the configured cadence. Workers claim jobs from the Postgres queue, retry on transient failure, and emit alerts.
7. **Observe** — every run, log line, status transition, and cost number is queryable via API, MCP, CLI, or UI. External agents can subscribe to run-completion webhooks. Failed runs surface for review.

Steps 1–3 are LLM-mediated (the agent does the reasoning); steps 4–7 are deterministic mechanics. The loop applies whether the goal is a new pipeline, a modification to an existing dbt model, a config change, or a guardrail adjustment. The loop applies whether driven by a human at the CLI, an agent via MCP, or a CI workflow via REST.

## 4. Scope

### 4.1 In scope for v0.1 (Pillars 1 + 2 + 4)

v0.1 bundles three pillars into one release so the first version demonstrates the full Carve loop end to end: agent authors code, runtime schedules and executes a multi-step pipeline, observability lands in the local UI / REST API / MCP server.

- **Agent-driven dlt authoring**: agents generate dlt sources/resources/pipelines, plus `.dlt/secrets.toml` and `.dlt/config.toml`
- **Curated Carve source library** at `src/carve/sources/`, including ports of popular Airbyte sources rewritten as native dlt
- **Snowflake destination** tested by Carve maintainers; other dlt destinations (Postgres, BigQuery, DuckDB, Databricks, Redshift, filesystem) supported by dlt natively, with user-authored tests
- **Plan/build/run/deploy lifecycle** with durable plan and build artifacts; `plan --refine` for iteration
- **PR-based deploy** for promoting from dev to prod
- **Runtime**: scheduler reading per-pipeline cron expressions, multi-worker job queue with optimistic-claim semantics, heartbeats for crash recovery, retry-with-backoff, structured per-run logs, Slack/email alerts on failure
- **Multi-step pipeline composition**: pipelines are DAGs of `dlt`, `dbt`, and `sql` steps with explicit `depends_on` dependencies, parallel execution where the graph allows, per-step failure modes (`fail`, `warn`, `continue`, `retry`, `skip_downstream`), and structured cross-step output passing
- **Step types**: `dlt`, `dbt`, `sql`
- **Postgres state store** via bundled `docker-compose.yml`; external Postgres supported via connection string
- **Single-user auth** via API key or local token
- **Local static-HTML UI**: run history, per-run logs, no live updates, no interactivity beyond links
- **REST API + MCP server** with full coverage of every CLI action — Carve is headless by default
- **Brownfield support**: detect existing dbt projects, integrate without overwriting
- **Convention inference** from existing project structure
- **Schema retrieval skills** (catalog queries against destination `INFORMATION_SCHEMA`; Snowflake first-class)
- **MCP client integration**: Carve consumes external MCP servers as skills (Snowflake MCP, dbt MCP, GitHub MCP)
- **CLI parity** for every API action
- **One-shot M1-SQLite → Postgres migration tool** for existing walking-skeleton users
- **Three working example projects**
- **Documentation site**
- **Apache 2.0** license, **DCO** sign-off from day one

### 4.2 Out of scope for v0.1

- **The dbt agent** — comes in v0.2 (Pillar 3). v0.1 users hand-write dbt models; v0.1's runtime can still *schedule* `dbt build` as a step inside a composed pipeline even though it can't *author* models.
- **The hosted product** — separate release timeline; v0.1 is OSS-only.
- **Polished cloud UI, multi-user auth, SSO/RBAC, audit log, push-button deploy with approval, premium integrations, hosted secrets** — paid-product features.
- **Multi-LLM-provider support** — Anthropic-only; abstraction prepared but unused.
- **Visual pipeline builder** — TOML/YAML/code authoring, agent-first.
- **Looker, Tableau, other BI integrations**.
- **Reverse-ETL integrations** (Hightouch, Census).
- **Embedding-based semantic schema search** — likely lands in v0.2 or later alongside the dbt agent's broader context needs.
- **Custom step types beyond `dlt`/`dbt`/`sql`** — `shell`, `http`, `python`, `agent`, `approval` come after v0.1 once the step-type abstraction has hardened against three real consumers.
- **Skills SDK and custom step type SDK for extension** — built-ins only in v0.1.

### 4.3 Future, not now

The architecture is designed to support these without rewriting:

- **The hosted product** (v1.x): multi-tenant control plane, polished cloud UI, SSO/OAuth/RBAC, service accounts, audit log, push-button deploy with approval, hosted secrets, premium integrations (PagerDuty, Datadog)
- **Multi-LLM-provider support** (OpenAI, Google, local models)
- **Marketplace of community-contributed dlt sources and skills**
- **Federation between multiple Carve instances**
- **Custom step type SDK** for users to plug their own step types into the runtime
- **Skills SDK** for custom agent skills
- **Additional first-class destinations** (BigQuery, Databricks, Redshift) elevated from "dlt-supports-it-best-effort" to "Carve-maintainer-tested"

## 5. Key design decisions

These are the decisions that shape everything else. Grouped into five themes: Foundation, Runtime, Surface area, Internal architecture, and Governance.

### Foundation

### 5.1 dlt + dbt are our backends; we don't reinvent ingest or transform

Carve generates dlt code for extract-load and dbt code for transforms. It does not implement its own ingest runtime, its own transformation engine, or its own schema-inference layer. dlt's schema inference, incremental cursors, type coercion, retry semantics, and destination adapters are dlt's job — Carve's job is to author code that uses them well and to run them on a schedule. The same applies to dbt: dbt-core owns the DAG, the test framework, the manifest, the materialization strategies. Carve authors models and tests that fit the user's project conventions, and invokes dbt as a step in the runtime.

The implication: when an edge case in ingest or transform surfaces, the fix lives in dlt or dbt — not in Carve. We contribute upstream where it makes sense. This bet is what lets Carve be small: we own the authoring layer, the runtime layer, and the observability layer, and we own zero infrastructure that dlt or dbt already provide.

### 5.2 Carve meets you where your dbt project is

Brownfield is the dominant case, not the edge case. Most teams adopting Carve already have a working dbt project — its conventions are load-bearing for the team, and Carve must integrate with it rather than overwrite or compete with it. The opposite case (greenfield, no existing dbt) is also supported, but the default assumption is brownfield.

Specifically: Carve never modifies a user's `dbt_project.yml` or `profiles.yml` without explicit consent, never reorganizes the user's `models/` directory, and never replaces conventions it observes with its own preferences. Carve learns from the existing project — naming, layering, test patterns, materialization defaults — and reflects what it learns in a generated `carve/conventions.md` file that the agents read on every invocation. The user's dbt repo can live in the same git repo as Carve or in a separate one; both are first-class (see §6.2).

### 5.3 AI-first authoring, not AI-assisted

The natural way to create or modify anything in Carve is to describe it in natural language. Hand-editing TOML, YAML, or Python is the escape hatch — supported, well-documented, but not the primary path.

This is a stronger position than "AI-assisted." It means the CLI's headline command is `carve plan "<goal>"`, not `carve scaffold pipeline <name>`. It means documentation leads with what to ask the agent, not what the schema looks like. It means the REST API and MCP server have a `plan` endpoint that takes a goal string — external agents drive Carve the same way the CLI does.

### 5.4 Plan/build/run/deploy lifecycle

Every change goes through a lifecycle modeled on `terraform plan`/`apply` but with more granularity. The **plan** is a serializable artifact with task graph, cost estimate, file diffs, and impact analysis. Plans can be saved, refined (`plan --refine`), diffed, and built later. **Build** writes code to disk. **Run** executes against the dev target. **Deploy** promotes from dev to prod (PR-based for OSS, push-button for hosted).

The underlying primitive is always the four-stage lifecycle. Every stage is independently invocable via CLI, REST, or MCP — external agents can plan one day, refine the next, build a week later, and deploy after human review.

### 5.5 Code is the source of truth

Pipeline definitions live in `pipelines/<name>.toml`. dlt code lives in `el/<name>/`. dbt models live in the user's dbt project. Agent definitions live in `carve/agents/`. Connections live in `carve/connections.toml`. The REST API and the cloud UI read from and write to these files. Every API edit produces a git commit (or, in the hosted product, an audit-logged change record that can also be promoted via PR).

This gives version control, reviewability, rollback, disaster recovery, and portability for free. The cost is some implicit complexity — non-engineers see git mechanics. We accept this for the OSS audience (mostly engineers) and the hosted product softens it through PR-on-button-click and audit-log workflows.

### Runtime

### 5.6 Narrow, opinionated runtime — not a general orchestrator

Carve's runtime schedules and executes pipelines made of `dlt`, `dbt`, and `sql` steps. It deliberately does *not* support arbitrary Python operators, asset-graph reactivity, fan-out/fan-in beyond intra-pipeline parallelism, conditional branching, cross-pipeline triggers, backfills as a first-class concept, or any of the other features that make Dagster and Airflow large and complex.

This narrowness is a feature. Most warehouse pipelines don't need a general DAG framework — they need "run dlt, then dbt, then a SQL file, on a cron, with retries and alerts." That's what Carve does. Teams that need conditional branching or arbitrary DAGs are better served by Dagster or Airflow.

If users hit a wall on these capabilities, the answer is "use a real orchestrator alongside Carve" — Carve exposes its own runs via webhooks and a REST API, so it composes cleanly with whatever orchestrator a user already runs. We may add some capabilities post-v0.1, but each one eats the simplicity dividend, and we treat that cost as real.

### 5.7 Multi-worker from day one

The runtime ships with multi-worker semantics on day one, even though the OSS default is a single worker. The worker count is a one-line config change (`--workers N` or `runtime.toml`), and additional workers can run as separate processes (`carve worker`) coordinating via the shared Postgres queue.

This matters because adding multi-worker semantics later is much more expensive than building them in upfront. Optimistic-claim job table semantics (`UPDATE jobs SET claimed_by = ?, status = 'running' WHERE id = ? AND status = 'queued'`), per-worker heartbeats for crashed-worker reclamation, and per-pipeline serialization (so a pipeline doesn't race itself) are foundational design choices. We make them now so the hosted product scales without an architecture rewrite, and so OSS users with growth needs can scale up by changing one config.

### 5.8 Postgres from day one

The state store is Postgres from v0.1, not SQLite. Three reasons:

1. **Multi-worker (decision 5.7) requires concurrent writes**, which SQLite's WAL mode handles only at small worker counts. Postgres is built for this from the start.
2. **The OSS-to-hosted migration becomes free**: hosted Carve runs on Postgres, and OSS Carve already runs on Postgres, so the migration is "change the connection string."
3. **The bundled docker-compose makes Postgres zero-config for first-run**: `git clone carve && docker compose up` brings up a Postgres container alongside Carve. Production users override the connection string to point at managed Postgres (RDS, Cloud SQL, Supabase).

The cost is that Carve isn't quite as zero-config as a SQLite-backed CLI tool. We pay that cost once at install time in exchange for an architecture that scales.

### 5.9 Steps as the unit of execution

A pipeline is a directed acyclic graph of steps. Each step has a type (`dlt`, `dbt`, `sql` in v0.1), a config, a list of dependencies, and a failure mode. Failure modes: `fail` (default — fail the pipeline), `warn` (record a warning, continue), `continue` (record the failure, continue), `retry` (retry N times then fail), `skip_downstream` (skip dependents, continue siblings).

Steps within a pipeline run in parallel when their dependencies allow. Each step's output is structured (logs + status + named outputs) and can be referenced by downstream steps via Jinja templating.

This generalizes the "pipeline = Python script" model to handle the common case of "run dlt, then dbt, then a SQL post-step that refreshes a search index." The step type set is intentionally small (3 types in v0.1) and grows slowly — adding a new type is a real commitment because it expands what the runtime claims to do.

### Surface area

### 5.10 Headless by default

Carve is a backend with a programmable surface. The CLI, the local static-HTML UI, the polished cloud UI (paid), and external chat tools / agents are all clients of the same REST API and MCP server. There is no functionality that only the CLI can do, no functionality that only the cloud UI can do.

This decision has two practical consequences. First, the REST API and MCP server are first-class deliverables in v0.1, not retrofits — every CLI command has a corresponding API endpoint and MCP tool, and they ship together. Second, Carve is consumable by external agents (Claude Desktop, Cursor, Claude Code, custom agents) on equal footing with humans. An agent driving Carve via MCP can plan a pipeline, refine it, build it, run it against dev, and open a deploy PR — the same loop a human runs at the CLI.

MCP also goes the other direction: Carve consumes external MCP servers as skills. The Snowflake MCP server, the dbt MCP server, the GitHub MCP server — all appear to Carve's agents as namespaced skills (`mcp:snowflake:query`, `mcp:github:open_pr`). This is what plugs Carve into the broader AI-tooling ecosystem instead of trying to be an island.

### 5.11 OSS feature-complete; hosted operationally distinct

The OSS version is feature-complete for a single-team self-hoster. It includes the full agent layer, the full runtime, the full REST API, the full MCP server, the curated dlt source library, and the local static-HTML UI. There are no API endpoints or MCP tools gated behind the hosted product. The OSS is Apache 2.0 with no source-available restrictions.

The hosted product earns its price on operational excellence, not feature exclusivity. It adds: managed infra (we run the workers, you don't operate Postgres or docker-compose), multi-tenancy with SSO/OAuth/RBAC, service accounts with scoped permissions, audit log on every API call, plan-approval workflows, rate limiting and per-team quotas, premium integrations (PagerDuty, Datadog with payload formatters), a polished cloud UI with live monitoring and lineage, push-button deploy with audit trail alongside PR-based deploy, and hosted secrets management.

This is the dbt Labs / Sentry / Posthog model. We explicitly reject the open-core gating anti-pattern: no individual API endpoints are paid-only, no "advanced" agent features are paid-only, no source-available license tricks (BSL, SSPL) to prevent self-hosters from running at scale. The hosted product is what teams pay for when they don't want to operate Carve themselves.

### Internal architecture

### 5.12 Orchestration agent is the only meta-agent

Specialist agents (extract-load, dbt, runtime, quality) don't coordinate with each other. They each work on pre-scoped context handed to them by the orchestration agent. This keeps each agent focused, independently testable, and swappable.

The orchestration agent's job is to take a goal, classify it, gather impact context, pick the right specialist(s), pre-scope the context for each, and produce a plan. The specialists' job is to do the focused work — author dlt code, modify a dbt model, schedule a pipeline — given a tight context window. This separation is what keeps any single agent's prompt and token budget manageable as the system grows.

### 5.13 Schema context is a retrieval problem

Real warehouses have thousands of tables. Stuffing the catalog into LLM context is impossible and unnecessary. Carve solves this with layered retrieval: structured catalog queries against the destination `INFORMATION_SCHEMA` for facts, dbt manifest queries for dependencies, grep for exact references, lineage traversal for impact, and embedding-based semantic search (post-v0.1) for fuzzy concepts like "customer churn metrics."

The agent doesn't pick a layer. The agent picks a *skill*; skills are implemented using the appropriate layer. The orchestrator pre-scopes context before invoking specialist agents — specialist agents work on small focused inputs, not "here's the whole catalog, figure it out."

### 5.14 Config follows dlt + dbt conventions where they exist

Carve does not invent a parallel configuration system for things dlt and dbt already configure. dlt's `.dlt/secrets.toml` and `.dlt/config.toml` are the destination/source config files for the EL layer; the agent writes them. dbt's `profiles.yml` is the destination config for the transform layer; the agent honors it. Environment variable conventions follow dlt's (`DESTINATION__SNOWFLAKE__DATASET_NAME`, etc.) for ingest-side config.

Carve's own config is what's left: `carve.toml` at the root for project metadata, `carve/connections.toml` for runtime connection definitions (which targets exist, what credentials map to them), `carve/runtime.toml` for scheduler/worker tuning, `pipelines/<name>.toml` for pipeline composition. These are deliberately small, deliberately distinct from dlt's and dbt's files, and live alongside them — not on top of them.

A user fluent in dlt or dbt sees familiar files when they look in their project. A user familiar with Carve sees Carve's files cleanly separated. Conventions over configuration, where the convention is "match what dlt and dbt already do."

### Governance

### 5.15 Roles: Viewer, Creator, Admin (forward-looking)

v0.1 is single-user; there is no role concept in OSS yet. When multi-user lands (in the hosted product, and possibly in OSS later), three roles cover ~99% of real use cases:

- **Viewer** — read-only access to pipelines, runs, logs
- **Creator** — can plan, build, run, and deploy their own pipelines; cannot modify others'
- **Admin** — can intervene in any pipeline, edit org-level settings, manage users

Resist adding more roles until real customers ask. Fine-grained RBAC is an enterprise concern, not a v0.1 or v0.2 concern. The state store carries an `owner_user_id` field even in single-user mode (always `1`), so the schema is multi-user-ready when it lands.

### 5.16 Apache 2.0 + DCO + private hosted repo

The OSS repo is Apache 2.0. The hosted product lives in a separate, private repo. All contributors to the OSS repo sign off with a Developer Certificate of Origin (DCO) on every commit, preserving the option to dual-license later without surprising contributors.

We explicitly do not use BSL, SSPL, or other source-available licenses for the OSS code. Those licenses are aimed at preventing hyperscaler resellers; that's not a real risk until adoption is much larger. Apache 2.0 is what the data ecosystem expects, and it removes friction for contributors, downstream consumers, and corporate adopters.

The OSS/hosted split is enforced by the two-repo structure: the OSS repo cannot import private code, and the hosted repo can import OSS code freely. Shared interfaces (APIs, MCP tools, config schemas) are defined in the OSS repo; hosted-specific implementations (multi-tenant control plane, billing, RBAC enforcement) live in the private repo.

## 6. Functional requirements

Aligned to the core loop from §3: init → dbt integration → plan → build → run → deploy → schedule → composition → agents → skills → interfaces → observability.

**API and MCP parity is mandatory, not optional.** Every CLI command and every CLI flag has a corresponding REST endpoint (or request-body field) and a corresponding MCP tool (or tool argument). This is the operational expression of design decision 5.10 (headless by default): an external agent driving Carve via MCP, or a CI workflow driving Carve via REST, must be able to do everything a human can do at the CLI. The acceptance criteria for every subsection below implicitly include "every CLI behavior is also reachable via REST and MCP." When a new flag is added to the CLI, the corresponding REST/MCP surface must ship in the same release.

### 6.1 Project initialization

`carve init` scaffolds a working Carve project in the current directory. The resulting layout includes:

- `carve.toml` — project metadata, default target, root-level settings
- `carve/connections.toml` — target definitions (dev, prod) and their credentials (via env-var interpolation)
- `carve/runtime.toml` — scheduler and worker tuning (worker count, retry defaults, alert webhooks)
- `carve/conventions.md` — generated convention inference (populated by §6.2 when a dbt project is detected)
- `el/` — empty directory for dlt pipelines
- `pipelines/` — empty directory for pipeline composition files
- `.dlt/` — dlt's own config directory, with templated `secrets.toml` and `config.toml`
- `.env.example` — template for `ANTHROPIC_API_KEY`, target credentials, dlt env vars
- `.gitignore` — Carve-specific entries
- `docker-compose.yml` — bundled Postgres for OSS first-run (override via `--external-postgres <url>`)

Behaviors:

- If a `dbt_project.yml` is detected in the current directory or one level down, Carve enters brownfield mode and runs the dbt-integration flow (§6.2). No files in the existing dbt project are modified.
- If `--with-dbt` is passed and no existing dbt project is detected, Carve scaffolds a greenfield dbt project (§6.2).
- If no git repo exists, `carve init` runs `git init` for the new (or wrapping) repo.
- A single-user API token is generated and stored locally for CLI authentication.
- `carve init --dbt-path <path>` and `carve init --dbt-url <git_url>` enter separate-repo mode (§6.2).

Acceptance:

- `carve init` in a greenfield directory completes in under 30 seconds and produces a project that `carve plan "test goal"` can run against
- `carve init` in a brownfield directory completes in under 5 minutes (manifest analysis included) and produces a project that integrates with the existing dbt project

### 6.2 dbt project integration

The most common Carve install is on top of an existing dbt project. Carve must integrate with it cleanly — read its conventions, target its sources, invoke its build commands — without overwriting or surprising the user.

**Repo topology.** Three modes:

- **Same-repo (default).** `carve init` from within an existing dbt repo. Carve files (`carve.toml`, `carve/`, `el/`, `pipelines/`, `docker-compose.yml`) live alongside `dbt_project.yml`. Single git history. Cross-cutting changes (new dlt pipeline + new dbt source) land in one PR.
- **Separate-repo, local path.** `carve init --dbt-path /path/to/dbt`. Carve repo is separate; `carve.toml` records the filesystem path to the dbt project. Useful for monorepos with `dbt/` and `carve/` as sibling directories.
- **Separate-repo, remote URL.** `carve init --dbt-url git@github.com:myorg/dbt.git`. Carve clones the dbt repo into a workspace cache (`.carve/workspaces/<dbt-name>/`) and syncs it before runtime invocations. Cross-repo deploys produce two linked PRs — one to the Carve repo (EL + pipeline changes) and one to the dbt repo (new `sources.yml` entries) via the GitHub MCP server.

**Brownfield detection.** On `carve init`, Carve searches the current directory and one level down for `dbt_project.yml`. If found, Carve registers the existing dbt project: notes its location, reads its target profile, indexes its `sources.yml` files. No files in the dbt project are modified by `carve init`.

**Convention inference.** Carve analyzes the brownfield project's structure and writes `carve/conventions.md` summarizing what it found: model naming (`stg_*`, `int_*`, `fct_*`), staging-vs-marts layering, default materializations, common test patterns, source schema conventions. Agents read this file as part of their pre-scoped context on every invocation. Users can hand-edit `conventions.md` to override or correct anything Carve inferred.

**Source coupling.** When the EL agent generates a dlt pipeline, it consults the brownfield dbt project's `sources.yml` files. If the user wants the pipeline to feed an existing dbt source, the agent matches the source's schema/table conventions. If the source doesn't exist yet, the agent generates a stub `sources.yml` entry alongside the dlt pipeline. In separate-repo mode this becomes a linked PR.

**Greenfield scaffolding.** `carve init --with-dbt` (or selecting "scaffold new dbt project" in interactive mode) scaffolds a new dbt project alongside Carve config using a blessed default layout: `models/staging/`, `models/marts/`, `models/intermediate/`, a starter `dbt_project.yml`, a `profiles.yml` template. This is the path for users who don't already have dbt. Brownfield detection is still the default behavior.

**Ongoing integration.** The runtime invokes `dbt build`, `dbt test`, `dbt run --select`, etc., as step types against the registered dbt project. dlt artifacts land data into schemas dbt's `sources.yml` declares — so the dbt → dlt boundary is explicit and inspectable. Cross-pillar references (a `dbt` step inside a pipeline that depends on a `dlt` step) resolve at runtime to the right paths regardless of repo topology.

Acceptance:

- Brownfield onboarding produces a `conventions.md` within 5 minutes of `carve init` on a real-world dbt project
- The EL agent generates dlt pipelines that target existing dbt sources without modification in 80% of cases on first attempt
- Both same-repo and separate-repo modes (local path and remote URL) are supported from v0.1

### 6.3 Plan generation

- `carve plan "<goal>"` produces a saved plan with task graph, file diffs, impact analysis, and cost estimate. Plans are durable artifacts persisted to `.carve/plans/<plan_id>.json` and the state store.
- Plans include a config hash computed at generation time, used to detect drift before `build` runs.
- Plans expire after 24 hours by default (configurable in `runtime.toml`).
- `carve plan --refine <plan_id> "<adjustment>"` produces a refined plan with a `parent_plan_id` reference. Refinement chains are unbounded.
- `carve plan --pipeline <name> "<change>"` produces a plan against an existing pipeline (the live files are inlined into the agent's context).
- Equivalent REST endpoints: `POST /plans` (body accepts an optional `pipeline` field — present for existing-pipeline plans, absent for new-pipeline plans), `POST /plans/{id}/refine`, `GET /plans/{id}`.
- Equivalent MCP tools: `plan_create(goal, pipeline=None)` (covers both new and existing pipelines via the optional argument), `plan_refine`, `plan_show`.

**Acceptance:** `carve plan` for a typical modification goal completes in under 15 seconds excluding LLM latency.

### 6.4 Build

- `carve build <plan_id>` materializes a plan's task graph into files on disk: dlt sources/resources/pipeline configs, dbt models (when the dbt agent ships in v0.2), `pipelines/<name>.toml` entries.
- Build checks the plan's config hash against current config; refuses to run against drifted config and prompts for re-plan.
- Build is idempotent: re-running against the same plan produces byte-identical output (modulo LLM nondeterminism in regenerated content).
- Builds are recorded in the state store as `Build` rows with a `manifest_json` listing every file written, line range, and file hash.
- A pipeline's `current_build_id` points at the most recent successful build; older builds are kept for diff and rollback.
- `carve plan-and-build "<goal>"` combines plan + interactive confirm + build for users who want one command.
- Equivalent REST: `POST /builds` (body `{"plan_id": "..."}` for `build`; body `{"goal": "...", "pipeline": "<name>?"}` for `plan-and-build`), `GET /builds/{id}`, `GET /pipelines/{name}/builds` (list builds for a pipeline).
- Equivalent MCP: `build_run(plan_id)`, `build_plan_and_build(goal, pipeline=None)`, `build_show(build_id)`, `pipeline_builds_list(pipeline)`.

**Acceptance:** typical build (one dlt pipeline + one `pipelines/*.toml`) completes in under 60 seconds; generated files pass `dlt pipeline check` / `dbt parse` 99% of the time.

### 6.5 Run

- `carve run <pipeline>` executes a pipeline on demand against the default target (typically dev).
- `carve run <pipeline> --target <name>` runs against an explicit target.
- Multi-step pipelines execute as DAGs: each step's runner is invoked in dependency order with parallelism where the graph allows.
- Failure modes per step honor `pipelines/<name>.toml` config (`fail`, `warn`, `continue`, `retry`, `skip_downstream`).
- Logs stream live: stdout for CLI, WebSocket / SSE for API/UI consumers.
- Each run produces a `Run` row with structured per-step status, duration, tokens, cost, and link to logs.
- Manual `carve run` while the scheduler is running joins the same Postgres queue; treated as a normal job.
- `carve run --watch <pipeline>` is a CLI ergonomic that composes "create run" + "stream logs"; on the API and MCP side an agent composes the same two primitives.
- `carve run --resume <run_id>` reruns only the failed steps and their dependents from a prior run.
- Equivalent REST:
  - `POST /runs` with body `{"pipeline": "<name>", "target": "<name>?"}` — covers `carve run <pipeline>` and `--target`
  - `GET /runs/{id}` — covers `carve runs show`
  - `GET /runs/{id}/logs` — covers `carve logs <run_id>` (non-streaming)
  - `GET /runs/{id}/stream` (WebSocket / SSE) — covers `carve run --watch` and `carve logs --follow`
  - `POST /runs/{run_id}/resume` — covers `carve run --resume <run_id>`; creates a new run that resumes from the prior run's failed steps and dependents
  - `GET /pipelines/{name}/runs` — covers `carve runs --pipeline <name>`
- Equivalent MCP:
  - `run_pipeline(pipeline, target=None)` — covers create-run + target flag
  - `run_show(run_id)`
  - `run_logs_tail(run_id, follow=False)` — covers both static-log fetch and follow mode; agents compose `run_pipeline` + `run_logs_tail(follow=True)` to mirror `--watch`
  - `run_resume(run_id)` — covers `--resume`
  - `pipeline_runs_list(pipeline)`

**Acceptance:** run startup overhead under 10 seconds; failed runs retry-from-step via CLI / API / MCP / UI.

### 6.6 Deploy

Deploy promotes a built pipeline from dev to prod. There is no DDL phase (dlt handles destination schema management; see decision 5.1). Deploy is a git-mediated operation.

- `carve deploy <pipeline>` opens a pull request against the project's default branch with the pipeline's files staged. PR description includes plan summary, file diffs, build manifest, and impact analysis.
- For separate-repo mode (§6.2), deploy may produce two linked PRs (Carve repo for EL/pipeline changes; dbt repo for `sources.yml` changes), coordinated via the GitHub MCP server.
- `carve deploy --dry-run <pipeline>` previews the PR without opening it.
- Carve does not modify production state directly; the PR merge triggers whatever CI the user has wired up. Carve does not own CI integration in v0.1 — users wire it via their existing GitHub Actions / GitLab CI / similar.
- After merge, the scheduler picks up the pipeline based on its declared cron and starts firing runs against prod (§6.7).
- The hosted product adds a push-button deploy variant (via `POST /deploys` with `mode: "direct"`) that records the deploy in the audit log and supports plan-approval workflows. PR-based deploy remains supported in hosted.
- Equivalent REST:
  - `POST /deploys` with body `{"pipeline": "<name>", "dry_run": bool, "mode": "pr"|"direct"}` — covers `carve deploy <pipeline>`, `--dry-run`, and (hosted) push-button mode
  - `GET /deploys/{id}` — covers `carve deploys show`
  - `GET /pipelines/{name}/deploys` — covers `carve deploys --pipeline <name>`
- Equivalent MCP:
  - `deploy_pipeline(pipeline, dry_run=False, mode="pr")`
  - `deploy_show(deploy_id)`
  - `pipeline_deploys_list(pipeline)`

### 6.7 Scheduling

Once a pipeline is deployed and merged, its cron schedule (declared in `pipelines/<name>.toml`) takes effect. The scheduler fires runs against the prod target on the configured cadence. Workers claim those runs from the Postgres queue using the optimistic-claim semantics from decision 5.7.

- Each pipeline declares its schedule inline in its TOML: `[schedule] cron = "0 2 * * *"` plus optional `target = "prod"` and `paused = false`.
- The scheduler runs as part of `carve serve`; it does not require a separate process.
- Schedules are read from the database on startup and refreshed when `pipelines/*.toml` files change.
- Pause/resume controls let users disable a schedule without removing the cron expression; resuming picks up at the next normal fire time, not a backfill.
- Manual triggers via `carve run` (§6.5) enqueue alongside scheduled runs and use the same worker pool.
- **Backfills are explicitly not supported in v0.1.** Per design decision 5.6 (narrow runtime), users who need to backfill historical periods do it manually via `carve run --target prod --param start_date=...` for each period.

CLI commands and flags:

- `carve schedule list` — list all scheduled pipelines and their next fire time
- `carve schedule show <pipeline>` — show a single pipeline's schedule
- `carve schedule pause <pipeline>` — pause; `carve schedule resume <pipeline>` — resume
- `carve schedule next-fires --within 24h` — show what's about to fire in a time window

Equivalent REST:

- `GET /schedules` — covers `schedule list`; supports query params `?paused=true|false`, `?target=<name>`
- `GET /schedules/{pipeline}` — covers `schedule show`
- `POST /schedules/{pipeline}/pause` — covers `schedule pause`
- `POST /schedules/{pipeline}/resume` — covers `schedule resume`
- `GET /schedules/next-fires?within=<duration>` — covers `schedule next-fires`

Equivalent MCP:

- `schedule_list(paused=None, target=None)`
- `schedule_show(pipeline)`
- `schedule_pause(pipeline)`
- `schedule_resume(pipeline)`
- `schedule_next_fires(within="24h")`

**Acceptance:** the scheduler fires runs within 30 seconds of their cron time; pause/resume takes effect within 30 seconds; schedule state survives a `carve serve` restart.

### 6.8 Pipeline composition (steps + multi-step)

A pipeline is declared in `pipelines/<name>.toml`. Each pipeline has a `[schedule]` block (§6.7) and an ordered set of `[[steps]]` tables that form a DAG.

**Step types in v0.1**: `dlt`, `dbt`, `sql`.

- `dlt` step config: `artifact = "<name>"` (resolves to `el/<name>/`), optional `write_disposition` override, optional resource selector
- `dbt` step config: `command = "build" | "run" | "test"`, `select = "<dbt-selector>"`, optional `vars`
- `sql` step config: `file = "sql/<name>.sql"`, `connection = "<target-name>"`, optional Jinja context

**Step DAG semantics:**

- Each step has `depends_on = ["other_step_id"]` (empty for root steps)
- Each step declares a failure mode: `fail` (default), `warn`, `continue`, `retry` (with `max_attempts`), `skip_downstream`
- Steps without dependencies, or whose dependencies are complete, run in parallel up to the worker pool's available slots
- Each step's structured outputs (named values it emits) are referenceable by downstream steps via Jinja: `{{ steps.dlt_load.row_count }}`

CLI commands and flags:

- `carve pipelines list` — list all pipelines (with status, schedule, last-run summary)
- `carve pipelines show <name>` — show a single pipeline's full config + recent run history
- `carve pipelines validate <name>` — parse and schema-check the TOML; validate step references and the DAG (no cycles, all `depends_on` IDs resolve)
- `carve pipelines diff <name> --against <build_id>` — diff the current `pipelines/<name>.toml` against an older build

Equivalent REST:

- `GET /pipelines` — covers `pipelines list`
- `GET /pipelines/{name}` — covers `pipelines show`
- `POST /pipelines/{name}/validate` — covers `pipelines validate`
- `GET /pipelines/{name}/diff?against=<build_id>` — covers `pipelines diff`

Equivalent MCP:

- `pipelines_list()`
- `pipeline_show(pipeline)`
- `pipeline_validate(pipeline)`
- `pipeline_diff(pipeline, against_build_id)`

Authoring of pipeline TOML files is via plan/build (§6.3, §6.4) — not direct CLI editing, per design decision 5.3 (AI-first authoring). Hand-edits are supported but not the primary path.

**Acceptance:** a 3-step pipeline (`dlt` → `dbt` → `sql`) executes end-to-end in correct dependency order; parallel steps execute concurrently when the graph allows; cross-step output references resolve via Jinja templating; cycle detection rejects invalid DAGs at `validate` time.

### 6.9 Agent configuration

Each agent has a TOML definition with system prompt, model selection, allowed skills, and guardrails. v0.1 ships three agents: orchestration, extract-load (dlt-specialist), runtime. v0.2 adds the dbt agent.

- Definitions live in `carve/agents/*.toml`, versioned in git
- Each agent file declares: `name`, `model`, `system_prompt` (inline or `system_prompt_path` pointing to a markdown file), `allowed_skills = [...]`, `[guardrails]` block (token budget, forbidden actions, max iterations)
- Guardrails are validated programmatically before execution, not just suggested in the prompt
- Hot reload: changes to an agent file take effect on the next plan/build invocation without restarting `carve serve`
- Agent invocations are recorded in the state store with token usage and cost

**Built-in vs custom agents.** v0.1's built-in agents (orchestration, extract-load, runtime) cannot be removed — they're load-bearing for the core lifecycle — but their TOML config (system prompt, model, allowed skills, guardrails) can be edited freely. Users can also create custom agents alongside the built-ins; the agent registry treats them as peers, and the orchestration agent can route to them when they declare matching specializations.

CLI commands and flags:

- `carve agents list` — list all agents with their current config summary
- `carve agents show <name>` — show full agent config (TOML rendered)
- `carve agents create <name>` — scaffold a new agent TOML with minimal config
- `carve agents create <name> --template <existing-name>` — clone an existing agent's config as the starting point
- `carve agents remove <name>` — remove a custom agent; refuses with an error if the target is a built-in or if other config references it
- `carve agents edit <name>` — open the TOML in `$EDITOR`
- `carve agents test <name> "<prompt>"` — run a test invocation against the agent in isolation, without persisting state; useful for prompt iteration
- `carve agents test <name> "<prompt>" --save-transcript <path>` — save the full tool-call transcript to disk

Equivalent REST:

- `GET /agents` — covers `agents list`
- `GET /agents/{name}` — covers `agents show`
- `POST /agents` (body: name + optional `template`) — covers `agents create`
- `PUT /agents/{name}` (body: full TOML or JSON-encoded config) — covers external-editor edits via API
- `DELETE /agents/{name}` — covers `agents remove`; returns 409 if target is a built-in or has dependents
- `POST /agents/{name}/test` (body `{"prompt": "...", "save_transcript": bool}`) — covers `agents test`

Equivalent MCP:

- `agents_list()`
- `agent_show(name)`
- `agent_create(name, template=None)`
- `agent_update(name, config)`
- `agent_remove(name)`
- `agent_test(name, prompt, save_transcript=False)`

Creation of new agents is also reachable via plan/build (`carve plan "create a new agent for X"`) per design decision 5.3 (AI-first authoring) — the direct CLI/API/MCP commands above are the escape hatch.

**Acceptance:** agent config changes take effect on the next invocation within the same `carve serve` process; `agents test` returns a transcript without writing to the state store; guardrail violations are caught before any skill is invoked; built-in agents cannot be removed.

### 6.10 Skills

Skills are how agents do things. Three sources of skill in v0.1:

- **Built-in skills** ship with Carve, live in `src/carve/skills/`. They cannot be removed but their availability per agent is controlled by each agent's `allowed_skills` list.
- **MCP skills** come from external MCP servers declared in `carve/mcp_servers.toml`. Tools exposed by each MCP server appear as namespaced skills (`mcp:server:tool`). Adding or removing an MCP server adds or removes the corresponding skills.
- **Custom skills SDK is deferred** (per §4.2 out-of-scope). In v0.1 there is no "user writes a Python file with a `@skill` decorator" path. Users who need custom skill behavior expose it via an external MCP server and register that server with Carve.

Skills declare:

- Typed inputs and outputs (Pydantic models, surfaced over MCP as JSON schema)
- A description (consumed by the LLM as the tool schema)
- An implementation
- A `SkillContext` parameter providing access to connections, the run's logger, and event emission

Skills receive automatic result caching within a run: identical skill calls in the same run return cached results. Results exceeding a configurable size cap are truncated with a flag (agents are expected to be specific in follow-up calls).

**Creating and removing skills in v0.1.** Built-in skills are added or removed by Carve maintainers via Carve releases — users do not create or remove them. MCP-provided skills are added by adding an MCP server (`mcp-servers add`) and removed by removing the server (`mcp-servers remove`); the namespaced skills appear/disappear in `skills list` accordingly.

CLI commands and flags:

- `carve skills list` — list all skills (built-in + MCP-imported), filterable by `--source built-in|mcp` and `--agent <name>` (skills allowed for that agent)
- `carve skills show <name>` — show a skill's input/output schema and description
- `carve skills test <name> --input '<json>'` — invoke a skill in isolation with the provided input; bypasses agent loop, useful for debugging
- `carve mcp-servers list` — list configured external MCP servers
- `carve mcp-servers add <name> --url <url>` — register an external MCP server (writes to `carve/mcp_servers.toml`); skills it provides appear in `skills list` afterward
- `carve mcp-servers remove <name>` — remove an MCP server and its skills

Equivalent REST:

- `GET /skills` — covers `skills list`; supports `?source=`, `?agent=` query params
- `GET /skills/{name}` — covers `skills show`
- `POST /skills/{name}/test` (body: input JSON) — covers `skills test`
- `GET /mcp-servers` — covers `mcp-servers list`
- `POST /mcp-servers` (body: name + url) — covers `mcp-servers add`
- `DELETE /mcp-servers/{name}` — covers `mcp-servers remove`

Equivalent MCP:

- `skills_list(source=None, agent=None)`
- `skill_show(name)`
- `skill_test(name, input)`
- `mcp_servers_list()`
- `mcp_server_add(name, url)`
- `mcp_server_remove(name)`

**Acceptance:** built-in skills are discoverable via `skills list`; MCP-imported skills appear in the same list with `mcp:` prefix; `skills test` invokes the skill in isolation and returns the result without writing to the state store; adding an MCP server makes its tools immediately available to authorized agents without restart.

### 6.11 Interfaces

Per design decision 5.10, the CLI, REST API, MCP server, and local Web UI are all clients of the same backend. This subsection specifies how each interface operates as a system; the specific commands they expose are covered in §6.1–6.10.

**CLI (`carve`).** Built on `typer`. Output formats: `--output table` (default), `--output json` (piping default when stdout isn't a TTY), `--output yaml`. Global flags: `--config-dir`, `--verbose`, `--quiet`, `--no-color`. Stable exit codes for CI: `0` success, `1` user error, `2` runtime error, `3` config error, `4` drift detected. The CLI talks to `carve serve` over HTTP; a subset of commands (`carve plan`, `carve build`) can run standalone for one-shot use. Server lifecycle: `carve serve`, `carve serve --workers N`, `carve serve --port`, `carve serve --host`, `carve worker`, `carve worker --workers N`, `carve docs serve`.

**REST API.** FastAPI; OpenAPI schema at `/api/openapi.json`, Swagger UI at `/api/docs`. Endpoints under `/api/v1/...`; v0.1 commits to v1 stability. Auth: API key in `Authorization: Bearer <token>`. v0.1 single-user token generated by `carve init` and stored in `.carve/token`; hosted adds SSO/OAuth/RBAC (§5.16). Errors follow `application/problem+json`. Write endpoints support `Idempotency-Key`. Live streams via WebSocket and SSE. Webhooks signed with HMAC.

**MCP server.** Standard Anthropic MCP protocol over stdio (default) or WebSocket (`--transport ws`). Auto-discovered by Claude Desktop and Cursor when registered. Exposes one tool per CLI command; tool schemas mirror REST request schemas. Same token as REST. The MCP server is a thin adapter over the REST API — no business logic lives in the MCP layer.

CLI: `carve mcp-serve`, `carve mcp-serve --transport ws --port 8766`. The MCP server itself doesn't have a REST sub-resource — it's a sibling interface.

**Local Web UI.** Pure static HTML regenerated per run (decision 5.10, modeled on `dbt docs`). No live updates, no auth beyond local-host binding. Rendered pages: index (run history), per-run detail (logs + step graph + cost), pipelines list. Lineage view deferred (per audit decision). Served by `carve docs serve` on `127.0.0.1:8766` by default. The polished cloud UI is part of the hosted product.

**Acceptance:** every CLI command has a corresponding REST endpoint and MCP tool; the OpenAPI spec at `/api/openapi.json` is complete and accurate; CLI exit codes are stable across minor releases.

### 6.12 Observability

Every run, every step, every agent invocation, and every skill call is recorded in the state store and surfaced through the same API/MCP/CLI/UI clients.

**Recorded per run:**

- Duration (start, end, total)
- Cost (LLM input/output tokens, USD, warehouse credits when available)
- Step-level status (queued, running, succeeded, failed, skipped) with timestamps
- Structured per-step logs with level, source, message
- Trigger source (manual, scheduled, API, MCP)
- Owner user ID (always `1` in v0.1)
- For agent invocations: model, prompt tokens, completion tokens, tool calls made
- For skill calls: skill name, input hash, output (subject to size cap), duration

**OpenTelemetry export.** Optional, configured in `runtime.toml`. When enabled, every run emits a trace with one span per step. Exporters: OTLP/gRPC, OTLP/HTTP. Useful for teams already running Honeycomb / Tempo / Datadog.

**Webhooks.** Declared in `runtime.toml` per-event-type. Events: `run.queued`, `run.started`, `run.succeeded`, `run.failed`, `step.failed`, `plan.created`, `build.completed`, `deploy.opened`. Payloads are versioned and documented.

**Alerts.** Slack and email webhooks ship in v0.1 as canned event-to-message mappers. PagerDuty / Datadog with payload formatters are paid-product integrations (§5.11).

CLI commands and flags:

- `carve runs list` — recent runs; filters `--pipeline`, `--status`, `--since`, `--target`, `--limit`
- `carve runs show <run_id>` — show full run detail
- `carve logs <run_id>` — print run logs to stdout
- `carve logs <run_id> --follow` — stream live (alias `carve runs tail`)
- `carve logs <run_id> --step <step_id>` — filter to a single step's logs
- `carve metrics costs --since 7d` — token + warehouse cost rollup
- `carve metrics runs --since 7d` — success/failure counts, median duration
- `carve metrics agents --since 7d` — per-agent token usage and call counts

Equivalent REST: `GET /runs` (with the same filters), `GET /runs/{id}`, `GET /runs/{id}/logs?step=<id>`, `GET /runs/{id}/stream` (WS/SSE), `GET /metrics/costs?since=`, `GET /metrics/runs?since=`, `GET /metrics/agents?since=`.

Equivalent MCP: `runs_list(pipeline, status, since, target, limit)`, `run_show(run_id)`, `run_logs(run_id, step, follow)`, `metrics_costs(since)`, `metrics_runs(since)`, `metrics_agents(since)`.

**Acceptance:** every run, step, agent invocation, and skill call is queryable via the API within 5 seconds of completion; webhooks fire within 5 seconds of the event; OpenTelemetry traces, when enabled, contain one span per step with proper parent-child relationships.

## 7. Non-functional requirements

### 7.1 Performance

- `carve init` greenfield: under 30 seconds. Brownfield (with dbt manifest analysis): under 5 minutes.
- `carve plan` for a typical modification: under 15 seconds excluding LLM latency.
- `carve build` for a typical change: under 60 seconds including LLM latency.
- `carve run` startup overhead: under 10 seconds (worker claim, target connection check, subprocess spawn).
- `carve deploy` producing a PR: under 60 seconds.
- REST API median latency for read endpoints: under 200ms (state-store-backed queries).
- WebSocket / SSE log streaming: status updates within 500ms of state changes.
- Scheduler latency: jobs fire within 30 seconds of their cron time.

### 7.2 Reliability

- Failed runs leave the state store in a consistent state; partial-step state is recoverable.
- Crashed workers are detected via heartbeat timeout and their jobs are reclaimed by other workers within 60 seconds.
- Plan files include a config hash; build and deploy validate this hash before proceeding.
- The runtime survives Postgres restarts: workers reconnect with exponential backoff; in-flight jobs are picked up after reconnection.
- The bundled docker-compose can be restarted (`docker compose restart`) without state loss.

### 7.3 Security

- No secrets in `carve.toml`, `pipelines/*.toml`, or any committed file; all sensitive values via `${VAR}` env-var interpolation.
- The config loader refuses to start if a sensitive field is hardcoded.
- dlt and dbt destination credentials never appear in logs, run output, or webhook payloads.
- LLM provider API keys are scoped to the agent layer; never logged, never echoed in CLI output.
- REST API tokens are stored hashed in the state store; never returned in plaintext after creation.
- All file-system writes are scoped to the project directory and `.carve/`.
- Generated dlt pipeline code runs in an isolated subprocess; no access to Carve's process memory.
- Webhook payloads are HMAC-signed with a per-installation secret.

### 7.4 Compatibility

- Python 3.11+
- Postgres 14+ (15+ recommended)
- dlt 1.0+
- dbt-core 1.7+
- Docker 20+ (for bundled docker-compose path)
- Linux and macOS supported; Windows on best-effort
- Anthropic SDK with current Claude models (Sonnet, Opus, Haiku); other LLM providers post-v0.1

### 7.5 Resource consumption

- Idle `carve serve` uses under 300MB RAM (FastAPI + scheduler + 1 worker).
- Postgres state store stays under 2GB even after a year of typical use (10K runs).
- Generated workspaces (cloned dbt repos in separate-repo mode) are cached and capped at 5GB by default; oldest evicted first.
- Disk usage for logs: ~100KB per run after compression; 10K runs = ~1GB.

## 8. Success metrics for v0.1.0

### 8.1 Adoption metrics

- 1,000+ GitHub stars within 90 days of v0.1.0
- 200+ confirmed installations via opt-in telemetry
- 50+ external pull requests within 90 days
- 30+ external MCP integrations confirmed (Claude Desktop / Cursor / Claude Code users driving Carve)

### 8.2 Engagement metrics

- 50% of installations result in at least one successful `carve deploy`
- 30% of installations have at least one scheduled pipeline running daily after 7 days
- Median time from `carve init` to first successful scheduled run: under 60 minutes (greenfield), under 2 hours (brownfield)
- 40% of brownfield installations result in at least one merged PR within a week

### 8.3 Quality metrics

- Generated dlt pipelines pass `dlt pipeline check` 99% of the time
- Generated dbt models (once v0.2 ships) pass `dbt parse` 99% of the time
- Plan/build matches plan preview 95% of the time (no surprise behavior at build)
- Multi-step pipelines complete successfully on first scheduled run 80% of the time

### 8.4 Community signals

- A working community Slack or Discord with 500+ members
- 3+ blog posts from external users about adopting Carve
- 1+ conference talk proposal accepted (dbt Coalesce, Data Council, PyData)
- 5+ public examples of agents driving Carve via MCP (Claude Desktop / Cursor demos, custom-agent posts)

## 9. Risks and mitigations

### 9.1 Agents generate plausible-looking but wrong code

The canonical AI-for-code risk. Mitigations:

- Plan/build workflow surfaces what will happen before it happens
- Generated code lands as PRs with CI checks (`dlt pipeline check`, `dbt parse`, `dbt test` on dev, lint)
- Convention inference grounds output in the team's existing patterns (§6.2)
- Skills are deterministic where possible (catalog queries return facts, not LLM guesses)
- `carve plan --refine` lets users iterate before committing to code

### 9.2 Schema context blows up the LLM context window

Mitigated by the layered retrieval architecture (decision 5.13). The orchestrator pre-scopes context. Specialist agents work on small focused inputs. Catalog queries are bounded; embedding search (post-v0.1) returns pointers, not full content.

### 9.3 dlt or dbt-core ship breaking changes

A real risk now that Carve is built on both. Mitigations:

- Pin to specific minor versions in dependencies; users opt in to upgrades
- Test against multiple dlt and dbt versions in CI
- Contribute upstream to surface concerns early
- Build a thin abstraction layer where reasonable so a breaking dlt change can be absorbed in one place
- Ship a Carve patch within 2 weeks of any breaking change that lands in our supported versions

### 9.4 OSS and hosted code drift

New risk from the OSS/hosted split: as both evolve, they can drift. Mitigations:

- Shared interfaces (config schemas, REST endpoints, MCP tools) live in the OSS repo; hosted depends on OSS as a library
- Integration tests run against both OSS and hosted in CI before any release
- Hosted-only features are additive on top of OSS, never replacing
- Quarterly review: walk the OSS surface and confirm hosted still matches

### 9.5 Positioning conflict with dltHub Pro

dltHub is shipping agentic scaffolding for dlt pipelines — our closest competitor for the "agent writes dlt code" use case. Mitigations:

- Lead with the cross-cutting story (ingest + transform + runtime + deploy) that dltHub can't tell as an ingest-only company
- Keep the relationship friendly: we generate their code, we drive adoption
- Pursue partnership conversations once we have traction
- Avoid framing dltHub as a competitor in public communication

### 9.6 Operational complexity from Postgres + Docker requirements

Postgres-from-day-one and the bundled docker-compose add friction vs. a SQLite-backed CLI. Mitigations:

- The bundled docker-compose makes first-run one command
- Friendly errors when Docker is missing, pointing to external-Postgres alternatives
- Documentation walks through managed-Postgres setup (RDS, Cloud SQL, Supabase)
- The hosted product is the natural answer for users who don't want to operate Postgres themselves

### 9.7 Contributor burnout from a single maintainer

If the project takes off, one maintainer can't keep up. Mitigations:

- Strong contributor documentation and clear extension points (MCP servers, custom agents)
- Triage issue templates that pre-filter low-quality reports
- A roadmap document that says "no" to off-roadmap features by default
- Plan for a second maintainer by month 6 if traction is real

## 10. Out-of-scope clarifications

A few things explicitly *not* part of Carve, to prevent scope creep:

- Carve is **not a connector framework**. dlt is the connector framework; we generate code that uses it, and we contribute back. Building a parallel connector framework is anti-positioning.
- Carve is **not a general-purpose orchestrator**. Dagster and Airflow occupy that space; we are deliberately narrower (dlt + dbt + sql, no arbitrary DAGs).
- Carve is **not a data quality tool** in the Great Expectations / Soda sense. It generates dbt tests; it doesn't run a separate quality monitoring product.
- Carve is **not a data catalog**. It indexes schema for retrieval; it doesn't provide a discoverability product for analysts.
- Carve is **not a BI tool**. It builds and operates the data warehouse; it doesn't visualize it.
- Carve is **not a reverse-ETL tool**. It writes to the warehouse; it doesn't sync to operational systems.
- Carve is **not a notebook environment**. Pipelines are declared in TOML and authored by agents; not interactive notebooks.

These are healthy adjacent product spaces, all of which Carve might integrate with later via MCP or webhooks. None are part of the core.

## 11. Open questions

Most major design questions are resolved in [`specs/_strategy/2026-05-positioning.md`](./2026-05-positioning.md) and [`specs/_strategy/spec-audit.md`](./spec-audit.md). What remains is implementation-level and resolves at spec-writing time:

- The default Carve documentation site framework (mkdocs-material vs docusaurus vs starlight)
- Whether the local static HTML UI uses pure file regeneration or a tiny read-only FastAPI app reading from the state store
- The exact set of webhook event payload schemas (will firm up as we write the runtime spec)
- Whether `carve serve` in OSS auto-detects a `DATABASE_URL` env var (Heroku-style) for external Postgres
- The MCP transport default for v0.1 (stdio-only first vs. stdio + WebSocket from day one)

## 12. Appendix — naming and brand

- Project name: **Carve**
- GitHub org: `carve-data` (placeholder; check availability before claiming)
- Tagline candidates: "Agent-native data engineering," "The warehouse lifecycle, authored by intent," "dlt + dbt with agents and a runtime"
- Brand vocabulary: agents *chisel*, skills are *blades*, builds are *carvings*. Use sparingly — don't overdose on the metaphor.
- Color, logo, marketing site: deferred to a branding pass closer to v0.1 launch.

**Acceptance:** `carve deploy` produces a PR within 60 seconds for a typical pipeline; refuses to run against drifted config.
