# Carve — Product Requirements Document

> Last major revision 2026-06-16, reframed to the control-plane + AI-harness model per [`_strategy/2026-06-control-plane.md`](./_strategy/2026-06-control-plane.md) and [`_strategy/2026-06-ai-harness.md`](./_strategy/2026-06-ai-harness.md). Builds on [`_strategy/2026-05-positioning.md`](./_strategy/2026-05-positioning.md). For the prior version, see [`_archive/PRD-pre-2026-05-positioning.md`](./_archive/PRD-pre-2026-05-positioning.md).

This is the single source of truth for what Carve is.

## 1. Vision

**Carve is a control plane plus an AI harness, over independently-versioned dlt/dbt/sql components — not a project that contains them.** The value proposition: **build, schedule, and monitor pipelines — all with AI.** Carve schedules, orchestrates, and monitors `dlt` (extract & load) + `dbt` (transform) + `sql` pipelines; its AI harness builds and deploys those components for you, **or** you bring your own — built outside Carve — and Carve just orchestrates them.

The control plane is the thing you run (`carve serve`, backed by Postgres). It holds the orchestration entities — pipelines, steps, schedules, jobs, runs, deploys — plus **versioned references** to the code components; it does **not** contain the components' code. Each component (an extract-load/dlt component, a transform/dbt component, plus `sql`/other steps) is independently versioned and follows its own repo / CI-CD / lifecycle. A pipeline references each component **by name** and composes them into a step DAG. A small team can keep the control-plane config and all components in one repo — **simple mode**, the delightful default and the greenfield wedge — without changing the architecture: the control plane still *references* its components, they just happen to be co-located. Components can later graduate to separate repos (pinned by ref) with one guided command and no pipeline rewrites.

You can drive Carve from its CLI, from a chat tool like Claude Desktop or Cursor via MCP, from your own agents over a REST API, or from a local web UI. **Carve is headless by default**; the interface is up to you.

The whole warehouse lifecycle in one place, authored by intent, accessible from anywhere.

Carve combines four things that most data tools keep separate:

1. **An AI harness for data work** — a Claude-Code-style agentic engine that builds and deploys components: a main orchestration loop that delegates to domain subagents (a DLT engineer with qa/security reviewers, a pipeline engineer, a recovery engineer, an explorer; a dbt engineer in v0.2), armed with terminal-grade tools, running behind a permission system, that verifies its work by executing it. Fully extensible — bring your own agents, skills, MCP servers, CLIs, and hooks.
2. **A control plane — a narrow, opinionated runtime** — scheduled execution of `dlt` + `dbt` + `sql` pipelines composed from components referenced by name, with multi-worker job claiming, structured logs, retries, and alerts. Deliberately *not* a general-purpose orchestrator: no asset graphs, no arbitrary DAGs, no plugin operators.
3. **A programmable surface — CLI, REST API, and MCP server** — every action available to Carve's own agents is available to any external agent, chat tool, or script. The local web UI is just one client among many.
4. **A hosted product for teams that don't want to operate it themselves** — managed control plane, polished cloud UI, collaboration, SSO, push-button deploy

Two adoption modes are both first-class: **build-with-Carve** (the AI authors components into their repos, the control plane schedules them) and **orchestration-only** (you bring existing dlt/dbt/sql, Carve references them by version and only composes/schedules/monitors). Orchestration-only is a central path, not a corner case — it is exactly *why* the control plane references components rather than owning them.

The opinion: for the 80% of warehouse work that's well-trodden territory (ingest, model, test, schedule), agent-driven authoring on top of dlt + dbt — composed and run by a narrow control plane — is faster, more consistent, and more maintainable than hand-coding. For the rest, Carve stays out of your way. And Carve stays radically simpler than Dagster by **scope and opinion**, not by being less capable: only `dlt` + `dbt` + `sql` step types, AI-driven authoring, no general asset framework, no "adopt our runtime's worldview."

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
2. **Plan** — Carve's orchestrator (the harness's main loop) classifies and decomposes the goal, delegates to the right domain subagents (DLT engineer, pipeline engineer; dbt engineer in v0.2), gathers component context, and synthesizes a structured plan with file diffs and cost estimates. The plan is reviewable, iterable (`plan --refine`), and a durable artifact in its own right.
3. **Build** — once the plan is approved, the subagents generate the code into the relevant component: dlt sources and resources, dbt models and tests (v0.2), the SQL glue between them, and the `pipelines/<name>.toml` that composes them. They **verify by executing** (`dlt pipeline run`, `dbt build`/`test`) until green. Output lands in the working tree, not committed yet.
4. **Run** — the user (or agent, or scheduler) executes the composed pipeline against the dev target. Iteration is cheap — re-run, refine, re-run, until the rows look right.
5. **Deploy** — once dev iteration is done, deploy promotes the component(s) plus the control-plane composition from dev to prod via a **configurable handoff** (files → commit → push → PR; default PR). A change spanning a separate-repo component and the control-plane repo produces **linked PRs** (ingest-first ordering). For the hosted product, deploy adds a push-button variant with audit log alongside the PR path.
6. **Schedule** — once merged, the control plane fires runs against prod on the configured cadence. The schedule is **data**: a pipeline's optional `[seed_schedule]` block seeds the schedules table at first registration, and thereafter the live schedule is changed instantly via `carve schedule` (CLI/API/UI), audited — not via a code change or PR. Workers claim jobs from the Postgres queue, retry on transient failure, and emit alerts.
7. **Observe** — every run, log line, status transition, and cost number is queryable via API, MCP, CLI, or UI. External agents can subscribe to run-completion webhooks. Failed runs surface for review.

Steps 1–3 are LLM-mediated (the subagents do the reasoning, grounded in real tool output); steps 4–7 are deterministic mechanics. The loop applies whether the goal is a new pipeline, a modification to an existing dbt model, a config change, or a schedule adjustment. The loop applies whether driven by a human at the CLI, an agent via MCP, or a CI workflow via REST. In **orchestration-only mode** (you bring existing dlt/dbt/sql), steps 2–3 compose and register your components by name without generating component code; steps 4–7 are identical.

**A sibling read-only verb outside the loop.** `carve ask` (§6.5) is the **explorer** subagent — handles investigative queries ("Where do we calculate net revenue?", "Which models depend on `stg_orders`?", etc.) read-only, running in the harness's `read_only` permission mode. It uses the same harness and tools as `plan`, but produces an answer rather than a plan, and is strictly side-effect-free. Asks can run concurrently with anything else.

## 4. Scope

### 4.1 In scope for v0.1 (the control plane + the DLT component-engineer + composition + the AI harness)

v0.1 bundles the control plane (the runtime that references components by name), the DLT component-engineer, multi-step composition, and the AI harness into one release so the first version demonstrates the full Carve loop end to end: the AI harness builds and deploys a dlt component, the control plane composes/schedules/executes a multi-step pipeline, observability lands in the local UI / REST API / MCP server. Orchestration-only mode (bring your own dlt/dbt/sql) is fully in scope across all three step types.

- **The AI harness**: a Claude-Code-style agentic engine — subagent delegation, terminal-grade tools (`edit`, `bash`, `glob`, `grep`, `web_fetch`, `web_search`), permission modes (`read_only`/`plan`/`build`/`deploy`) with allowlists + sandboxed bash, and the verify-by-execution loop
- **v0.1 subagents**: orchestrator (main loop), DLT engineer (+ dlt-qa / dlt-security review subagents, phased in), pipeline engineer, recovery engineer (diagnose-then-delegate), explorer (`ask`). The dbt engineer is v0.2.
- **Declarative extensibility**: bring-your-own agents (`carve/agents/*.md`), skill packs (`SKILL.md`), hooks, and MCP (consume + expose) — all in v0.1
- **AI-driven dlt authoring**: the DLT engineer generates dlt sources/resources/pipelines into a named component, plus `.dlt/secrets.toml` and `.dlt/config.toml`
- **SQL dialect-aware tool layer**: `sqlglot`-backed transpile/validate, per-dialect `INFORMATION_SCHEMA` introspection, permission-gated execution (Snowflake first-class; others via `sqlglot`) — a cross-cutting tool every subagent uses, plus a thin SQL specialist
- **Curated Carve connector/skill library** (`SKILL.md` packs), including ports of popular Airbyte sources rewritten as native dlt
- **Snowflake destination** tested by Carve maintainers; other dlt destinations (Postgres, BigQuery, DuckDB, Databricks, Redshift, filesystem) supported by dlt natively, with user-authored tests
- **Plan/build/run/deploy lifecycle** with durable plan and build artifacts; `plan --refine` for iteration
- **Configurable-handoff deploy** (files → commit → push → PR, default PR) with cross-repo **linked PRs** for promoting components + the control-plane composition from dev to prod
- **Control plane**: scheduler reading the live `schedules` table (seeded from `[seed_schedule]`), multi-worker job queue with optimistic-claim semantics, heartbeats for crash recovery, retry-with-backoff, structured per-run logs, Slack/email alerts on failure
- **Multi-step pipeline composition**: pipelines are DAGs of `dlt`, `dbt`, and `sql` steps referencing components **by name**, with explicit `depends_on` dependencies, parallel execution where the graph allows, per-step failure modes (`fail`, `warn`, `continue`, `retry`, `skip_downstream`), and structured cross-step output passing
- **Step types**: `dlt`, `dbt`, `sql`
- **Control-plane config + component references**: `carve.toml` as the control-plane config; `[components.<name>]` blocks resolve a name to a local path (simple mode) or a remote repo @ pinned ref; per-component graduation (simple → multi)
- **Postgres state store** via bundled `docker-compose.yml`; external Postgres supported via connection string
- **Single-user auth** via API key or local token
- **Local static-HTML UI**: run history, per-run logs, no live updates, no interactivity beyond links
- **REST API + MCP server** with full coverage of every CLI action — Carve is headless by default
- **Brownfield support**: detect existing dbt and dlt components, register without overwriting
- **Convention inference** from existing component structure
- **MCP client integration**: Carve consumes external MCP servers as skills (Snowflake MCP, dbt MCP, GitHub MCP)
- **CLI parity** for every API action
- **One-shot M1-SQLite → Postgres migration tool** for existing walking-skeleton users
- **Three working example projects**
- **Documentation site**
- **Apache 2.0** license, **DCO** sign-off from day one

### 4.2 Out of scope for v0.1

- **The dbt engineer subagent** — comes in v0.2. v0.1 users hand-write dbt models; v0.1's control plane can still *schedule* `dbt build` as a step inside a composed pipeline even though the harness can't yet *author* models.
- **The hosted product** — separate release timeline; v0.1 is OSS-only.
- **Polished cloud UI, multi-user auth, SSO/RBAC, audit log, push-button deploy with approval, premium integrations, hosted secrets** — paid-product features.
- **Multi-LLM-provider support** — Anthropic-only; abstraction prepared but unused.
- **Visual pipeline builder** — TOML/YAML/code authoring, agent-first.
- **Looker, Tableau, other BI integrations**.
- **Reverse-ETL integrations** (Hightouch, Census).
- **Embedding-based semantic schema search** — likely lands in v0.2 or later alongside the dbt engineer's broader context needs.
- **Custom step types beyond `dlt`/`dbt`/`sql`** — `shell`, `http`, `python`, `agent`, `approval` come after v0.1 once the step-type abstraction has hardened against three real consumers.
- **In-process custom-skill SDK and custom step-type SDK** — note this is *only* the in-process Python SDKs (the `@skill`-decorated path, the step-type extension SDK). **Declarative extensibility ships in v0.1**: bring-your-own agents (`carve/agents/*.md`), `SKILL.md` skill packs, hooks, and MCP (both directions) are all in scope (§6.11–6.12). Users who need custom skill behavior in v0.1 expose it via an MCP server or a `SKILL.md` pack; the in-process SDKs land once the built-ins stabilize.

### 4.3 Future, not now

The architecture is designed to support these without rewriting:

- **The hosted product** (v1.x): multi-tenant control plane, polished cloud UI, SSO/OAuth/RBAC, service accounts, audit log, push-button deploy with approval, hosted secrets, premium integrations (PagerDuty, Datadog)
- **Multi-LLM-provider support** (OpenAI, Google, local models)
- **Marketplace of community-contributed dlt sources and skill packs**
- **Federation between multiple Carve control planes**
- **In-process custom step-type SDK** for users to plug their own step types into the control plane (the declarative + MCP extension paths ship in v0.1)
- **In-process custom-skill SDK** (the `@skill`-decorated Python path) for custom agent skills (the `SKILL.md` + MCP skill paths ship in v0.1)
- **Additional first-class destinations** (BigQuery, Databricks, Redshift) elevated from "dlt-supports-it-best-effort" to "Carve-maintainer-tested"

## 5. Key design decisions

These are the decisions that shape everything else. Grouped into five themes: Foundation, Runtime, Surface area, Internal architecture, and Governance.

### Foundation

### 5.1 dlt + dbt are our backends; we don't reinvent ingest or transform

Carve generates dlt code for extract-load and dbt code for transforms. It does not implement its own ingest runtime, its own transformation engine, or its own schema-inference layer. dlt's schema inference, incremental cursors, type coercion, retry semantics, and destination adapters are dlt's job — Carve's job is to author code that uses them well and to run them on a schedule. The same applies to dbt: dbt-core owns the DAG, the test framework, the manifest, the materialization strategies. Carve authors models and tests that fit the user's project conventions, and invokes dbt as a step in the runtime.

The implication: when an edge case in ingest or transform surfaces, the fix lives in dlt or dbt — not in Carve. We contribute upstream where it makes sense. This bet is what lets Carve be small: we own the authoring layer, the runtime layer, and the observability layer, and we own zero infrastructure that dlt or dbt already provide.

### 5.2 Carve meets you where your projects are

Brownfield is the dominant case for both dlt and dbt. Most teams adopting Carve already have either a working dbt project, a working dlt setup, or both — their conventions are load-bearing for the team, and Carve must integrate with them rather than overwrite or compete.

Specifically: Carve never modifies the user's `dbt_project.yml`, `profiles.yml`, existing dlt source code, `.dlt/secrets.toml`, or `.dlt/config.toml` without explicit consent, and never reorganizes the user's `models/` directory. Carve learns from the existing projects — naming, layering, source patterns, destination conventions, write dispositions — and reflects what it learns in a generated `carve/conventions.md` that the agents read on every invocation. The user adds team standards and decision history in companion `carve/standards.md` and `carve/decisions.md` files (see §6.3); agents read those too as durable project memory.

The user can run Carve in three configurations:

1. **Authoring + orchestration** (greenfield or mixed): Carve's agents generate new dlt pipelines and dbt models (when v0.2 ships), and Carve's runtime schedules them.
2. **Orchestration only** (full brownfield): the user authored their own dlt and dbt; Carve detects them, registers them, and lets the user compose them into scheduled pipelines via plan/build without generating any new EL or transform code. This is the natural mode for teams already using dltHub Pro or another dlt-management tool.
3. **Mix**: Carve authors some pipelines and orchestrates user-authored ones alongside them.

The user's dlt and dbt repos can live in the same git repo as Carve or in separate ones; both are first-class (see §6.2).

### 5.3 AI-first authoring, not AI-assisted

The natural way to create or modify anything in Carve is to describe it in natural language. Hand-editing TOML, YAML, or Python is the escape hatch — supported, well-documented, but not the primary path.

This is a stronger position than "AI-assisted." It means the CLI's headline command is `carve plan "<goal>"`, not `carve scaffold pipeline <name>`. It means documentation leads with what to ask the agent, not what the schema looks like. It means the REST API and MCP server have a `plan` endpoint that takes a goal string — external agents drive Carve the same way the CLI does.

### 5.4 Plan/build/run/deploy lifecycle

Every change goes through a lifecycle modeled on `terraform plan`/`apply` but with more granularity. The **plan** is a serializable artifact with task graph, cost estimate, file diffs, and impact analysis. Plans can be saved, refined (`plan --refine`), diffed, and built later. **Build** writes code into the relevant component. **Run** executes against the dev target. **Deploy** promotes the component(s) plus the control-plane composition from dev to prod via a **configurable handoff** (files → commit → push → PR; default PR, push-button in hosted), with cross-repo **linked PRs** when a change spans a separate-repo component and the control plane (§6.8).

The underlying primitive is always the four-stage lifecycle. Every stage is independently invocable via CLI, REST, or MCP — external agents can plan one day, refine the next, build a week later, and deploy after human review.

### 5.5 Code is the source of truth — for definitions; schedules and state are data

Carve's control-plane definitions are **config-as-code reconciled into the state store**: the state store is a materialized projection of the version-controlled definitions, refreshed on `carve serve` boot and a periodic loop. But ownership is *per-concern*, not uniform — three tiers:

- **Pipeline definition** (steps, the DAG, component references and pins) lives in `pipelines/<name>.toml`, reconciled into state; **code wins**. dlt code lives in its component (`el/<name>/` in simple mode). dbt models live in the dbt component. Agent definitions live in `carve/agents/`. Connections live in `carve/connections.toml`. The REST API and the cloud UI read from and write to these definition files; in the hosted product, an API edit can produce an audit-logged change record that is also promotable via PR.
- **Schedule** (cron, cadence, paused/enabled) is **data** in the `schedules` table — set via `carve schedule` over CLI/API/UI, instant and audited (§6.9). Code may carry an optional `[seed_schedule]` block applied only at first registration; the live schedule is never reconciled from code thereafter. There is no "schedule changes go through PR."
- **State** (jobs, runs, history) is **data**, always.

Definitions give version control, reviewability, rollback, disaster recovery, and portability for free. The cost is some implicit complexity — non-engineers see git mechanics — which we accept for the OSS audience (mostly engineers) and the hosted product softens through PR-on-button-click and audit-log workflows. The tradeoff of keeping schedules and state as data is that they reconstitute from the (backed-up) state store plus the code seed, not from `git clone`; in exchange, an on-call operator can pause a runaway schedule instantly without opening a PR.

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

### 5.12 The AI layer is a harness; the orchestrator delegates to subagents

Carve's AI layer is a **Claude-Code-style agentic harness specialized for data work**: a main agentic loop (the **orchestrator**) that **delegates to typed subagents**, armed with terminal-grade tools (`edit`, `bash`, `glob`, `grep`, `web_fetch`, `web_search`) plus domain skills, running behind a permission system, that **verifies its work by executing it**. The orchestrator doesn't do the deep work itself — it classifies and decomposes the goal, delegates to the right subagent(s) via a `delegate`/Task tool, and synthesizes the results into a reviewable plan or diff.

A subagent is a *fresh loop with its own context window, tool set, and system prompt* that returns a **summary**, not its transcript. This one move buys both multi-agent specialization and context management: a deep read inside a subagent returns a summary to the orchestrator rather than flooding the main context. The v0.1 subagents are the **DLT engineer** (engineer = architect + build, with separate **dlt-qa** / **dlt-security** *review* subagents — fresh, adversarial context), the **pipeline engineer** (composes components by name), the **recovery engineer** (diagnose-then-delegate), and the **explorer** (the read-only `ask` verb). The **dbt engineer** is v0.2. This is the same engineer→parallel-reviewers→fix pattern Carve uses on itself, brought to users' pipelines. **SQL is not a subagent** — it is a cross-cutting, dialect-aware *tool layer* every subagent uses (§5.13, §6.12), plus a thin SQL specialist.

Permission modes (`read_only`/`plan`/`build`/`deploy`) line up with the lifecycle, and recovery's fixes flow through the same plan/build/PR path — no autonomous writes to prod. This keeps each subagent focused, independently testable, swappable, and bounded in token budget as the system grows.

### 5.13 Schema context is a retrieval problem

Real warehouses have thousands of tables. Stuffing the catalog into LLM context is impossible and unnecessary. Carve solves this with layered retrieval: structured catalog queries against the destination `INFORMATION_SCHEMA` for facts, dbt manifest queries for dependencies, dlt schema queries for resource→table, grep for exact references, and embedding-based semantic search (post-v0.1) for fuzzy concepts like "customer churn metrics." Lineage and impact are **investigated** across the dbt manifest + dlt schema on demand, not read from a Carve-owned graph (lineage).

The agent doesn't pick a layer. The agent picks a *skill* (or a terminal tool — `grep`, a `sqlglot`-validated query); skills are implemented using the appropriate layer. Context stays bounded through **subagent context-isolation**: a subagent does its own deep retrieval against these layers in its isolated window and returns a *summary* to the orchestrator — so no single context holds "the whole catalog, figure it out." Grounding the LLM in real tool output (introspection, lineage, `sqlglot` validation) over its own guesses is also the accuracy story.

### 5.14 Config follows dlt + dbt conventions where they exist

Carve does not invent a parallel configuration system for things dlt and dbt already configure. dlt's `.dlt/secrets.toml` and `.dlt/config.toml` are the destination/source config files for the EL layer; the agent writes them. dbt's `profiles.yml` is the destination config for the transform layer; the agent honors it. Environment variable conventions follow dlt's (`DESTINATION__SNOWFLAKE__DATASET_NAME`, etc.) for ingest-side config.

Carve's own config is what's left: `carve.toml` at the root is the **control-plane config** — its referenced components (`[components.<name>]` blocks, each resolving a name to a local path in simple mode or a remote repo @ pinned ref), default target, and root-level settings; `carve/connections.toml` for runtime connection definitions (which targets exist, what credentials map to them); `carve/runtime.toml` for scheduler/worker tuning; `pipelines/<name>.toml` for pipeline composition (referencing components by name). These are deliberately small, deliberately distinct from dlt's and dbt's files, and live alongside them — not on top of them. In simple mode the `[components.*]` apparatus is convention-driven and stays out of sight (the `el/` dir is the dlt component, a detected dbt project is the dbt component); it materializes only when a component graduates to its own repo.

A user fluent in dlt or dbt sees familiar files when they look in their workspace. A user familiar with Carve sees Carve's files cleanly separated. Conventions over configuration, where the convention is "match what dlt and dbt already do."

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

Aligned to the core loop from §3: init → backend integration → memory → plan → ask (sibling, read-only) → build → run → deploy → schedule → composition → agents → skills → interfaces → observability.

**API and MCP parity is mandatory, not optional.** Every CLI command and every CLI flag has a corresponding REST endpoint (or request-body field) and a corresponding MCP tool (or tool argument). This is the operational expression of design decision 5.10 (headless by default): an external agent driving Carve via MCP, or a CI workflow driving Carve via REST, must be able to do everything a human can do at the CLI. The acceptance criteria for every subsection below implicitly include "every CLI behavior is also reachable via REST and MCP." When a new flag is added to the CLI, the corresponding REST/MCP surface must ship in the same release.

### 6.1 Project initialization

`carve init` scaffolds a working Carve control plane in the current directory. In the default **simple mode** the control-plane config and all components live in one repo (single working tree) — the delightful default; components can later graduate to separate repos with one guided command (§6.2), without pipeline rewrites. The resulting layout includes:

- `carve.toml` — the **control-plane config**: referenced components (`[components.<name>]`, written only when a component is split out — implicit in simple mode), default target, root-level settings
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

- If a `dbt_project.yml` is detected in the current directory or one level down, Carve enters brownfield-dbt mode and runs the dbt side of the integration flow (§6.2). No files in the existing dbt project are modified.
- If a `.dlt/` directory or Python files using `@dlt.source` / `@dlt.resource` / `@dlt.pipeline` decorators are detected, Carve enters brownfield-dlt mode and runs the dlt side of the integration flow (§6.2). No files in the existing dlt project are modified.
- If `--with-dbt` is passed and no existing dbt project is detected, Carve scaffolds a greenfield dbt project (§6.2).
- If `--with-dlt` is passed and no existing dlt project is detected, Carve scaffolds a greenfield dlt project (§6.2).
- `carve init --dbt-path <path>` and `carve init --dbt-url <git_url>` enter dbt separate-repo mode (§6.2).
- `carve init --dlt-path <path>` and `carve init --dlt-url <git_url>` enter dlt separate-repo mode (§6.2).
- The user can mix per-backend topology: e.g., dbt same-repo + dlt separate-repo, or vice versa.
- If no git repo exists, `carve init` runs `git init` for the new (or wrapping) repo.
- A single-user API token is generated and stored locally for CLI authentication.

Acceptance:

- `carve init` in a greenfield directory completes in under 30 seconds and produces a project that `carve plan "test goal"` can run against
- `carve init` in a brownfield directory completes in under 5 minutes (manifest analysis included) and produces a project that integrates with the existing dbt project

### 6.2 Backend project integration (dlt + dbt)

The most common Carve install is on top of one or both of: an existing dbt project, an existing dlt setup. Carve must integrate with them cleanly — reference them as components by name, read their conventions, target their resources and sources, invoke their build commands — without overwriting or surprising the user. This is exactly *why* the control plane references components rather than owning them. dlt and dbt are treated symmetrically: each has greenfield, brownfield-same-repo, brownfield-local-path, and brownfield-remote-URL modes, independently selected.

**Operating modes.** Per design decision 5.2, three configurations are supported — and **orchestration-only is a central adoption path, not a corner case**:

1. **Authoring + orchestration** (build-with-Carve) — Carve's AI authors new dlt and dbt code into their components, and the control plane composes/schedules/monitors them.
2. **Orchestration only** (bring-your-own) — the user wrote their own dlt and dbt; Carve references them by name and only composes them into scheduled, monitored pipelines, generating no new EL or transform code. The natural mode for the brownfield org with an existing dbt repo (its own CI/CD, its own team), and for teams using dltHub Pro or other dlt-management tools — a first-class path across all three step types from v0.1.
3. **Mix** — Carve authors some components and orchestrates user-authored ones alongside them.

**Repo topology.** Three modes per backend, independently chosen:

- **Same-repo (default).** `carve init` from within a repo containing existing `dbt_project.yml` and/or `.dlt/`. Carve files (`carve.toml`, `carve/`, `el/`, `pipelines/`, `docker-compose.yml`) live alongside them. Single git history.
- **Separate-repo, local path.** `carve init --dbt-path /path/to/dbt` and/or `--dlt-path /path/to/dlt`. Carve repo is separate; `carve.toml` records each filesystem path.
- **Separate-repo, remote URL.** `carve init --dbt-url git@github.com:myorg/dbt.git` and/or `--dlt-url git@github.com:myorg/dlt.git`. Carve clones each repo into a workspace cache (`.carve/workspaces/<name>/`) and syncs before runtime invocations. Each component is referenced at a **pinned ref** (commit/tag) for reproducibility; simple/single-repo mode defaults to branch-HEAD for zero friction. Cross-repo deploys produce linked PRs via the GitHub MCP server.

A user can mix per-backend topology — for example, dbt same-repo + dlt separate-repo — without restriction.

**Graduation (simple → multi).** Because pipelines reference each component **by name**, a component can graduate from co-located (simple mode) to its own repo without touching any pipeline. Resolution (local path vs remote repo @ ref) is a per-component setting: `carve component <name> --separate-remote <url> [--ref <pin>]` extracts the code, writes the `[components.<name>]` block, clones it to the workspace cache, and validates — no pipeline rewrites, no state migration, no re-runs. It is incremental (per-component), reversible, and symmetric with the brownfield "born multi" path (`carve init --dbt-url`). `carve components show` makes the resolution inspectable at any time.

**Brownfield detection.** On `carve init`:

- **dbt:** search the current directory and one level down for `dbt_project.yml`.
- **dlt:** search for a `.dlt/` directory and/or for Python files containing `@dlt.source`, `@dlt.resource`, or `@dlt.pipeline` decorators in a designated directory (defaults: `./`, `el/`, `dlt/`).
- The CLI prompts the user to confirm detected projects (and individual detected dlt pipelines) before registration.
- No files in the detected projects are modified by `carve init`.

**Convention inference.** Carve writes `carve/conventions.md` summarizing what it found in each backend:

- **dbt conventions:** model naming (`stg_*`, `int_*`, `dim_*`, `fct_*`), staging/marts layering, default materializations, common test patterns, source schema conventions.
- **dlt conventions:** destination types and configurations, source naming patterns, write-disposition defaults (append / replace / merge), schema-contract defaults, credential/secret conventions.

Agents read this file as part of their context on every invocation. Users can hand-edit `conventions.md` to override or correct anything Carve inferred.

**Authoring split — when subagents generate code and when they stay out.**

- **The DLT engineer generates** new dlt sources/pipelines into a named dlt component in modes 1 and 3 (authoring + orchestration). Generated pipelines respect the brownfield dlt component's conventions (matching destinations, schema names, write-disposition defaults).
- **The DLT engineer stays out** in mode 2 (orchestration only). Plan/build for "schedule my-existing-stripe-pipeline" creates a `pipelines/*.toml` entry that references the existing dlt component **by name** — no new dlt code is generated.
- **The dbt engineer (v0.2)** follows the same split: authors models in modes 1 and 3; stays out in mode 2. (v0.1's control plane can still *schedule* `dbt build` against an existing dbt component even though the harness can't yet *author* models.)
- **Cross-backend source coupling**: when the DLT engineer generates a pipeline that should feed an existing dbt source, it consults the brownfield dbt component's `sources.yml` and matches conventions. If the source doesn't exist, the engineer generates a stub `sources.yml` entry alongside the dlt pipeline; in separate-repo mode this becomes a linked PR against the dbt repo.

**Greenfield scaffolding.**

- `carve init --with-dbt` scaffolds a new dbt project: `models/staging/`, `models/marts/`, `models/intermediate/`, starter `dbt_project.yml`, `profiles.yml` template.
- `carve init --with-dlt` scaffolds a new dlt project: `el/` directory, templated `.dlt/secrets.toml` and `.dlt/config.toml`, a starter pipeline file showing the dlt patterns Carve will author against.
- The flags compose: `carve init --with-dbt --with-dlt` is a clean greenfield-for-both install.

**Ongoing integration.** The runtime runs `dlt pipeline run` (subprocess) and **dbt via its execution backend** — Carve-run for `local` (bundled Fusion/dbt-core or the team's own env) or *triggered* for `managed` (dbt Cloud, dbt-on-Snowflake-native) — as step types against the registered projects (same-repo, local-path, or remote-URL). dlt artifacts land data into schemas dbt's `sources.yml` declares — so the dbt → dlt boundary is explicit and inspectable regardless of repo topology *or* how dbt runs.

Acceptance:

- Brownfield onboarding produces a `conventions.md` within 5 minutes of `carve init` on a real-world dlt and/or dbt project
- The DLT engineer generates dlt pipelines that target existing dbt sources without modification in 80% of cases on first attempt
- All three operating modes (authoring + orchestration, orchestration only, mix) are supported from v0.1
- Same-repo, local-path, and remote-URL modes are supported symmetrically for both dlt and dbt from v0.1
- Mixed-topology installs (e.g., dbt same-repo + dlt remote-URL) work without restriction

### 6.3 Project memory

A place where the data engineer says "we always do X" and has it stick across agent invocations — separate from "what we currently do" (inferred conventions, §6.2) and "what the agent should know how to do" (agent system prompts, §6.11). Lives in git, versioned alongside the control-plane config, read by agents as part of their context on every invocation.

**File types:**

- **`carve/conventions.md`** — agent-generated, refreshed by convention inference (§6.2). Captures patterns observed in your code (model naming, layering, write dispositions). User-editable to correct misdetections, but the canonical source is the code.
- **`carve/standards.md`** — user-authored. Team rules that aren't inferable from code:
  - "All raw schemas use snake_case table names"
  - "Stripe data must always be loaded incrementally, not full-refresh"
  - "Use merge dispositions on PK for any pipeline pulling from a SaaS API"
  - "All marts must have a `unique` test on the grain column"

  Treated as authoritative by agents — overrides conventions where they conflict.
- **`carve/decisions.md`** — append-only, dated, with rationale and reviewers. Example: *"2026-04-12: Decided to keep Stripe data 18 months in staging, not 24, because storage costs. Reviewed by alice@, bob@."* Read by agents when relevant; the first-class place to answer "why did we do X?" questions via `carve ask` (§6.5).
- **Per-pipeline sidecars** — optional `pipelines/<name>.md` next to `pipelines/<name>.toml`. Pipeline-specific notes: "Stripe API has a 1000 req/min rate limit, we throttle to 500"; "depends on the daily Salesforce refresh, schedule after 6am UTC".
- **Per-component sidecars** — optional `el/<name>/NOTES.md` next to a dlt component. Source-specific quirks: "Stripe's `charges` endpoint occasionally returns 502; the dlt source has retry logic."

**Context bundling.** Memory files are loaded into a subagent's context based on the goal — the orchestrator includes the project-wide files when it delegates, and a subagent reads the component-scoped files itself within its isolated window:

- Every invocation: `conventions.md`, `standards.md`
- Goals touching a specific pipeline: that pipeline's sidecar
- Goals touching a specific dlt component: that component's `NOTES.md`
- Investigative goals via `ask`: `decisions.md` is included so "why" questions can be answered with citations

**Write policy.** Agents can **propose** memory additions via plan/build alongside code changes (`carve plan "we've decided to keep Stripe data 18 months; record that decision"`). The proposal lands as a file diff in the plan; the build writes it; deploy lands it as a PR. **Memory writes never bypass user review.** Agents have no autonomous memory writes — this is deliberate, because LLMs hallucinate, and self-updating memory would calcify hallucinations into the team's recorded history.

**Scaffolding.** `carve init` creates empty `standards.md` and `decisions.md` with comment-only templates explaining what goes in each. `conventions.md` is populated by convention inference on first run.

CLI commands and flags:

- `carve memory show` — list memory files with a summary of each
- `carve memory edit <file>` — open in `$EDITOR`
- `carve memory append-decision "<text>"` — convenience for the common "add a dated decision" workflow (auto-prefixes today's date; opens editor for the body)
- `carve memory show --pipeline <name>` — show the pipeline-scoped memory bundle (conventions + standards + this pipeline's sidecar)

Equivalent REST:

- `GET /memory` — list memory files
- `GET /memory/{file}` — show contents
- `PUT /memory/{file}` — replace contents (subject to the write policy: typically used by the build step, not user-direct)
- `POST /memory/decisions` (body: `{"text": "..."}`) — append a dated decision

Equivalent MCP:

- `memory_list()`
- `memory_show(file)`
- `memory_update(file, content)`
- `memory_append_decision(text)`

**Acceptance:**

- `carve init` scaffolds the three core files (`conventions.md` populated, `standards.md` and `decisions.md` empty with templated comments)
- Agents include the appropriate memory files in context on every invocation
- `carve ask "why did we do X?"` surfaces relevant entries from `decisions.md` with citations
- Memory additions via plan/build land in the same PR as the code change that motivated them
- No memory file is written outside the plan/build review gate

### 6.4 Plan generation

- `carve plan "<goal>"` produces a saved plan with task graph, file diffs, impact analysis, and cost estimate. Plans are durable artifacts persisted to `.carve/plans/<plan_id>.json` and the state store.
- Plans include a config hash computed at generation time, used to detect drift before `build` runs.
- Plans expire after 24 hours by default (configurable in `runtime.toml`).
- `carve plan --refine <plan_id> "<adjustment>"` produces a refined plan with a `parent_plan_id` reference. Refinement chains are unbounded.
- `carve plan --pipeline <name> "<change>"` produces a plan against an existing pipeline (the live files are inlined into the agent's context).
- Equivalent REST endpoints: `POST /plans` (body accepts an optional `pipeline` field — present for existing-pipeline plans, absent for new-pipeline plans), `POST /plans/{id}/refine`, `GET /plans/{id}`.
- Equivalent MCP tools: `plan_create(goal, pipeline=None)` (covers both new and existing pipelines via the optional argument), `plan_refine`, `plan_show`.

**Acceptance:** `carve plan` for a typical modification goal completes in under 15 seconds excluding LLM latency.

### 6.5 Ask — investigative queries

`carve ask` is a read-only sibling to `plan`. It runs the **explorer** subagent in the harness's `read_only` permission mode — the same harness, tools, and skills as `plan`, but its output is an answer (citation-backed) rather than a plan with file diffs, and it is strictly side-effect-free. Used for investigative questions like "Where do we calculate net revenue and what is the formula?", "Which models depend on `stg_orders`?", "What's the freshness of our Stripe data?", or "Show me every pipeline that writes to the `analytics` schema."

- `carve ask "<question>"` produces an Answer with markdown text, cited entities, and a skill-call trace. Saved as `.carve/asks/<ask_id>.json` and indexed in the state store.
- `carve ask "<question>" --pipeline <name>` scopes the question to a single pipeline's context.
- `carve ask "<question>" --target <name>` scopes the question to a single target's destination.
- `carve asks list` — list recent asks (with filters `--since`, `--limit`)
- `carve asks show <ask_id>` — show a previous ask's full answer + skill-call trace

The verb is strictly read-only: no files are modified, no warehouse state is touched, no plans or builds are created, no jobs are queued. The harness's `read_only` permission mode forbids any code-write tool or skill from being called during an Ask — enforced programmatically, not just by prompt.

Equivalent REST:

- `POST /asks` with body `{"question": "...", "pipeline": "<name>?", "target": "<name>?"}` — covers `carve ask`
- `GET /asks/{id}` — covers `carve asks show`
- `GET /asks` — covers `carve asks list`; supports `?since=`, `?limit=`, `?pipeline=`

Equivalent MCP:

- `ask(question, pipeline=None, target=None)`
- `ask_show(ask_id)`
- `asks_list(since=None, limit=50, pipeline=None)`

**Acceptance:** `carve ask` returns an answer with cited entities within 15 seconds (excluding LLM latency) for a typical investigative question; no write skills are invoked during an Ask; asks can run concurrently with each other and with plans/builds/runs/deploys.

### 6.6 Build

- `carve build <plan_id>` materializes a plan's task graph into files on disk: dlt sources/resources/pipeline configs, dbt models (when the dbt agent ships in v0.2), `pipelines/<name>.toml` entries.
- Build checks the plan's config hash against current config; refuses to run against drifted config and prompts for re-plan.
- Build is idempotent: re-running against the same plan produces byte-identical output (modulo LLM nondeterminism in regenerated content).
- Builds are recorded in the state store as `Build` rows with a `manifest_json` listing every file written, line range, and file hash.
- A pipeline's `current_build_id` points at the most recent successful build; older builds are kept for diff and rollback.
- `carve plan-and-build "<goal>"` combines plan + interactive confirm + build for users who want one command.
- Equivalent REST: `POST /builds` (body `{"plan_id": "..."}` for `build`; body `{"goal": "...", "pipeline": "<name>?"}` for `plan-and-build`), `GET /builds/{id}`, `GET /pipelines/{name}/builds` (list builds for a pipeline).
- Equivalent MCP: `build_run(plan_id)`, `build_plan_and_build(goal, pipeline=None)`, `build_show(build_id)`, `pipeline_builds_list(pipeline)`.

**Acceptance:** typical build (one dlt pipeline + one `pipelines/*.toml`) completes in under 60 seconds; generated files pass `dlt pipeline check` / `dbt parse` 99% of the time.

### 6.7 Run

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

### 6.8 Deploy

Deploy promotes a built pipeline — the component(s) plus the control-plane composition — from dev to prod. There is no DDL-apply phase: dlt owns destination schema management (decision 5.1), so the old `carve el deploy` DDL-apply step **retires** (it shrinks at most to a thin target-readiness `verify`). Deploy is a **configurable handoff** — a spectrum of git mechanics, not a single fixed action.

- `carve deploy <pipeline>` runs the configured handoff, which is one of **files** (write to the target tree only), **commit**, **push**, or **pr** — the default is **pr**, which opens a pull request against the default branch with the changed files staged. PR description includes plan summary, file diffs, build manifest, and impact analysis. The handoff mode is set per project/target (and overridable per invocation).
- **Cross-repo linked PRs are first-class in v0.1.** When a change spans a separate-repo component (§6.2) and the control plane, `carve deploy` produces **linked PRs** (e.g. the dlt component repo for source/pipeline changes, the dbt repo for `sources.yml`, the control-plane repo for the composition), coordinated via the GitHub MCP server with **ingest-first ordering** so the destination is ready before transforms reference it.
- `carve deploy --dry-run <pipeline>` previews the handoff (the PR set / commits) without performing it.
- Carve does not modify production state directly; a merged PR triggers whatever CI the user has wired up. Carve does not own CI integration in v0.1 — users wire it via their existing GitHub Actions / GitLab CI / similar.
- After merge, the control plane picks up the pipeline definition on reconcile; runs fire against prod on the **live schedule** (§6.9) — note the schedule itself is data, seeded once and thereafter changed via `carve schedule`, not by this deploy.
- The hosted product adds a push-button deploy variant (via `POST /deploys` with `mode: "direct"`) that records the deploy in the audit log and supports plan-approval workflows. The PR handoff remains supported in hosted.
- Equivalent REST:
  - `POST /deploys` with body `{"pipeline": "<name>", "dry_run": bool, "mode": "files"|"commit"|"push"|"pr"|"direct"}` — covers `carve deploy <pipeline>`, `--dry-run`, the handoff spectrum, and (hosted) push-button mode
  - `GET /deploys/{id}` — covers `carve deploys show` (a deploy record may reference multiple linked PRs)
  - `GET /pipelines/{name}/deploys` — covers `carve deploys --pipeline <name>`
- Equivalent MCP:
  - `deploy_pipeline(pipeline, dry_run=False, mode="pr")`
  - `deploy_show(deploy_id)`
  - `pipeline_deploys_list(pipeline)`

### 6.9 Scheduling

**The schedule is data, not code.** A pipeline's live schedule lives in the `schedules` table, set via the `carve schedule` command (CLI/API/UI) — instant and audited. It is **not** reconciled from `pipelines/*.toml` and changing a schedule does **not** go through plan/build/deploy/PR. The scheduler fires runs against the prod target on the configured cadence; workers claim those runs from the Postgres queue using the optimistic-claim semantics from decision 5.7.

- **Seeding.** A pipeline may carry an optional `[seed_schedule]` block in `pipelines/<name>.toml`: `[seed_schedule] cron = "0 2 * * *"` plus optional `target = "prod"` and `paused = false`. This is applied **only at first registration** of the pipeline. Thereafter the live schedule is data; editing `[seed_schedule]` is a **no-op** unless `carve schedule set <pipeline> --reseed` is invoked. A pipeline with no `[seed_schedule]` registers unscheduled (manual-run only) until a schedule is set.
- **Changing a schedule** (cron, target, pause/resume) is done through `carve schedule` and takes effect immediately, reconcile-free — the reconciler never touches the schedule. Every change is written to a `schedule_changes` audit log (actor + before/after), and is gated by the `schedule` RBAC scope (in the hosted product). This is the auditability story; git is not in the loop.
- The scheduler runs as part of `carve serve`; it does not require a separate process. It reads the live `schedules` table on startup and on its periodic loop.
- Pause/resume disables a schedule without discarding the cron expression; resuming picks up at the next normal fire time, not a backfill.
- Manual triggers via `carve run` (§6.7) enqueue alongside scheduled runs and use the same worker pool.
- **Backfills are explicitly not supported in v0.1.** Per design decision 5.6 (narrow runtime), users who need to backfill historical periods do it manually via `carve run --target prod --param start_date=...` for each period.

The tradeoff (decision 5.5): schedules reconstitute from the (backed-up) state store plus the code seed rather than from `git clone`. In exchange, an operator can pause a runaway pipeline in one command without opening a PR, and there is **no** code-vs-override TTL-precedence machinery — the live schedule is simply the single source of truth.

CLI commands and flags:

- `carve schedule list` — list all scheduled pipelines and their next fire time
- `carve schedule show <pipeline>` — show a single pipeline's live schedule
- `carve schedule set <pipeline> --cron "<expr>" [--target <name>]` — set/replace the live schedule (audited); `--reseed` re-applies the `[seed_schedule]` block
- `carve schedule pause <pipeline>` — pause; `carve schedule resume <pipeline>` — resume
- `carve schedule next-fires --within 24h` — show what's about to fire in a time window

Equivalent REST:

- `GET /schedules` — covers `schedule list`; supports query params `?paused=true|false`, `?target=<name>`
- `GET /schedules/{pipeline}` — covers `schedule show`
- `PUT /schedules/{pipeline}` (body `{"cron": "...", "target": "<name>?", "reseed": bool}`) — covers `schedule set`; the change is audited in `schedule_changes`
- `POST /schedules/{pipeline}/pause` — covers `schedule pause`
- `POST /schedules/{pipeline}/resume` — covers `schedule resume`
- `GET /schedules/next-fires?within=<duration>` — covers `schedule next-fires`

Equivalent MCP:

- `schedule_list(paused=None, target=None)`
- `schedule_show(pipeline)`
- `schedule_set(pipeline, cron, target=None, reseed=False)`
- `schedule_pause(pipeline)`
- `schedule_resume(pipeline)`
- `schedule_next_fires(within="24h")`

**Acceptance:** the scheduler fires runs within 30 seconds of their cron time; a `carve schedule set`/pause/resume takes effect within 30 seconds without a redeploy and is recorded in the `schedule_changes` audit log; `[seed_schedule]` is applied at first registration and ignored thereafter (absent `--reseed`); schedule state survives a `carve serve` restart.

### 6.10 Pipeline composition (steps + multi-step)

A pipeline is declared in `pipelines/<name>.toml` — Carve's **binding contract**: it references each component **by name**, composes them into a step DAG, and may carry an optional `[seed_schedule]` block (§6.9, applied at first registration only — the live schedule is data). The body is an ordered set of `[[steps]]` tables that form the DAG.

**Step types in v0.1**: `dlt`, `dbt`, `sql`. A `dlt` or `dbt` step references its component **by name**; the component locator resolves that name to a local path (simple mode) or a remote repo @ pinned ref (§6.2). Cross-component ordering (ingest-before-transform) falls out of the DAG.

- `dlt` step config: `component = "<name>"` (resolved by the component locator; in simple mode the name maps to `el/<name>/`), optional `write_disposition` override, optional resource selector
- `dbt` step config: `component = "<name>"`, `command = "build" | "run" | "test"`, `select = "<dbt-selector>"`, optional `vars`
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

Authoring of pipeline TOML files is via plan/build (§6.4, §6.6) — not direct CLI editing, per design decision 5.3 (AI-first authoring). Hand-edits are supported but not the primary path.

**Acceptance:** a 3-step pipeline (`dlt` → `dbt` → `sql`) executes end-to-end in correct dependency order; parallel steps execute concurrently when the graph allows; cross-step output references resolve via Jinja templating; cycle detection rejects invalid DAGs at `validate` time.

### 6.11 Agents — the subagent taxonomy + declarative extensibility

Carve's AI layer is the harness from decision 5.12: a main **orchestrator** loop that delegates to typed **subagents**. Agents are **declarative — markdown with frontmatter**, exactly like Claude Code: drop a `carve/agents/<name>.md` file and it becomes a routable subagent. This *is* the answer to keeping built and specified agents from drifting: there is no hardcoded dispatch to drift against.

**v0.1 subagents** (the orchestrator is the main loop, not a file):

- **DLT engineer** — authors + runs dlt sources/pipelines into a named dlt component; verifies via `dlt pipeline run`.
- **dlt-qa** / **dlt-security** (review) — fresh, adversarial review of the dlt diff (schema-contract, credential handling, data-loss modes), phased in (security-on-deploy, qa-on-build first).
- **Pipeline engineer** — composes components by name into `pipelines/<name>.toml`; the control-plane runtime specialist.
- **Recovery engineer** — diagnoses a failure (grounded in dlt exception classes, schema diff, run logs), then **delegates the fix** to the DLT or SQL engineer (the dbt engineer arrives in v0.2) through the same plan/build/PR path.
- **Explorer** — the read-only `ask` verb (§6.5), elevated; citation-backed.
- **dbt engineer** — **v0.2** (authors + runs dbt models/tests/sources), with a dbt-qa reviewer.

SQL is **not** an agent — it is a cross-cutting dialect-aware tool layer every subagent uses (§6.12).

**Agent file format.** Each `carve/agents/<name>.md` is markdown with YAML frontmatter declaring: `name`, `description` (what it's for — used for routing), `model` (per-agent model tiering — haiku to classify, sonnet to build, opus for hard work), `tools` (the terminal tools + skills it may call), `allowed_paths` (the component paths it may write), and `classifications` (the goal types it handles). The markdown body is the system prompt. Example:

```
# carve/agents/dlt-engineer.md
---
name: dlt-engineer
description: Authors and runs dlt sources/pipelines into a named dlt component. Use for ingest/extract-load goals.
model: claude-sonnet
tools: [edit, bash, grep, glob, web_fetch, dlt_library, schema_introspect, sql]
allowed_paths: ["el/**", ".dlt/*.template"]
classifications: [new_pipeline, modify_pipeline, refactor_to_incremental]
---
<system prompt body…>
```

- Definitions live in `carve/agents/*.md`, versioned in git.
- `allowed_paths` and the active **permission mode** (`read_only`/`plan`/`build`/`deploy`) are enforced programmatically before any tool runs, not merely suggested in the prompt.
- Hot reload: dropping in or editing an agent file takes effect on the next invocation without restarting `carve serve`.
- Subagent invocations are recorded in the state store with token usage and cost; a `delegate` call returns a *summary*, not the full transcript.

**Built-in vs custom agents.** The v0.1 built-in subagents (DLT engineer, pipeline engineer, recovery engineer, explorer, and the review subagents) cannot be removed — they're load-bearing for the core lifecycle — but their `.md` definition (system prompt, model, tools, allowed paths, classifications) can be edited freely. Users can also create custom agents alongside the built-ins; the registry treats them as peers, and the orchestrator routes to them when their `description`/`classifications` match the goal.

CLI commands and flags:

- `carve agents list` — list all agents with their current config summary
- `carve agents show <name>` — show full agent definition (the `.md` rendered)
- `carve agents create <name>` — scaffold a new agent `.md` with minimal frontmatter + a prompt stub
- `carve agents create <name> --template <existing-name>` — clone an existing agent's definition as the starting point
- `carve agents remove <name>` — remove a custom agent; refuses with an error if the target is a built-in or if other config references it
- `carve agents edit <name>` — open the `.md` in `$EDITOR`
- `carve agents test <name> "<prompt>"` — run a test invocation against the agent in isolation, without persisting state; useful for prompt iteration
- `carve agents test <name> "<prompt>" --save-transcript <path>` — save the full tool-call transcript to disk

Equivalent REST:

- `GET /agents` — covers `agents list`
- `GET /agents/{name}` — covers `agents show`
- `POST /agents` (body: name + optional `template`) — covers `agents create`
- `PUT /agents/{name}` (body: the full `.md` content or a JSON-encoded equivalent) — covers external-editor edits via API
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

### 6.12 Skills

Skills are how agents do things. Beneath them sits the terminal-grade base tool layer every subagent shares — `edit`, `bash` (allowlisted, sandboxed), `glob`, `grep`, `web_fetch`, `web_search` — plus the **dialect-aware SQL tool layer** (`sqlglot` transpile/validate, per-dialect `INFORMATION_SCHEMA` introspection, permission-gated execution; Snowflake first-class, others via `sqlglot`), which is a cross-cutting capability rather than a standalone skill. Skills are the domain capabilities on top, from **four sources in v0.1**:

- **Built-in skills** ship with Carve, live in `src/carve/skills/`. They cannot be removed but their availability per agent is controlled by each agent's `tools` list.
- **`SKILL.md` skill packs (in v0.1)** — declarative capability packs (`carve/skills/<name>/SKILL.md` + optional scripts/resources), progressive-disclosure, loaded on description-match. **The curated connector library *is* a skill library**: connectors, skills, and bring-your-own unify into one model. Drop a pack in `carve/skills/` and it's available; `carve skills` manages them.
- **MCP skills** come from external MCP servers declared in `carve/mcp_servers.toml`. Tools exposed by each MCP server appear as namespaced skills (`mcp:server:tool`). Adding or removing an MCP server adds or removes the corresponding skills. Carve also *exposes* an MCP server (the other direction, §6.13).
- **In-process custom-skill SDK is deferred** (per §4.2 out-of-scope). In v0.1 there is no "user writes a Python file with a `@skill` decorator" path; that lands once the built-ins stabilize. Users who need custom skill behavior in v0.1 write a `SKILL.md` pack or expose it via an external MCP server — both fully supported.

**Hooks (in v0.1).** Policy/automation injected without forking Carve, declared in `carve/hooks.toml`: `pre/post tool`, `pre-commit`, `on run.failed`, `pre-deploy`. Examples — run `sqlfluff lint` before committing dbt SQL, block writes to a prod schema, notify Slack on deploy or on `run.failed`. Hooks are how a team enforces its standards across every agent invocation.

Skills declare:

- Typed inputs and outputs (Pydantic models, surfaced over MCP as JSON schema)
- A description (consumed by the LLM as the tool schema)
- An implementation
- A `SkillContext` parameter providing access to connections, the run's logger, and event emission

Skills receive automatic result caching within a run: identical skill calls in the same run return cached results. Results exceeding a configurable size cap are truncated with a flag (agents are expected to be specific in follow-up calls).

**Creating and removing skills in v0.1.** Built-in skills are added or removed by Carve maintainers via Carve releases — users do not create or remove them. **`SKILL.md` packs** are created by dropping a `carve/skills/<name>/SKILL.md` directory into the project (also reachable via plan/build per decision 5.3) and removed by deleting it; they appear/disappear in `skills list` on hot reload. MCP-provided skills are added by adding an MCP server (`mcp-servers add`) and removed by removing the server (`mcp-servers remove`); the namespaced skills appear/disappear in `skills list` accordingly. The only deferred path is the in-process `@skill` Python SDK (§4.2).

CLI commands and flags:

- `carve skills list` — list all skills (built-in + `SKILL.md` packs + MCP-imported), filterable by `--source built-in|pack|mcp` and `--agent <name>` (skills available to that agent)
- `carve skills show <name>` — show a skill's input/output schema and description
- `carve skills test <name> --input '<json>'` — invoke a skill in isolation with the provided input; bypasses agent loop, useful for debugging
- `carve mcp-servers list` — list configured external MCP servers
- `carve mcp-servers add <name> --url <url>` — register an external MCP server (writes to `carve/mcp_servers.toml`); skills it provides appear in `skills list` afterward
- `carve mcp-servers remove <name>` — remove an MCP server and its skills
- `carve hooks list` — list configured hooks (from `carve/hooks.toml`) with their trigger events

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

**Acceptance:** built-in skills, `SKILL.md` packs, and MCP-imported skills (the latter with an `mcp:` prefix) are all discoverable via `skills list`; dropping in a `SKILL.md` pack or a `carve/hooks.toml` entry takes effect on hot reload without restart; `skills test` invokes the skill in isolation and returns the result without writing to the state store; adding an MCP server makes its tools immediately available to authorized agents without restart.

### 6.13 Interfaces

Per design decision 5.10, the CLI, REST API, MCP server, and local Web UI are all clients of the same backend. This subsection specifies how each interface operates as a system; the specific commands they expose are covered in §6.1–6.12.

**CLI (`carve`).** Built on `typer`. Output formats: `--output table` (default), `--output json` (piping default when stdout isn't a TTY), `--output yaml`. Global flags: `--config-dir`, `--verbose`, `--quiet`, `--no-color`. Stable exit codes for CI: `0` success, `1` user error, `2` runtime error, `3` config error, `4` drift detected. The CLI talks to `carve serve` over HTTP; a subset of commands (`carve plan`, `carve build`) can run standalone for one-shot use. Server lifecycle: `carve serve`, `carve serve --workers N`, `carve serve --port`, `carve serve --host`, `carve worker`, `carve worker --workers N`, `carve docs serve`.

**REST API.** FastAPI; OpenAPI schema at `/api/openapi.json`, Swagger UI at `/api/docs`. Endpoints under `/api/v1/...`; v0.1 commits to v1 stability. Auth: API key in `Authorization: Bearer <token>`. v0.1 single-user token generated by `carve init` and stored in `.carve/token`; hosted adds SSO/OAuth/RBAC (§5.16). Errors follow `application/problem+json`. Write endpoints support `Idempotency-Key`. Live streams via WebSocket and SSE. Webhooks signed with HMAC.

**MCP server.** Standard Anthropic MCP protocol over stdio (default) or WebSocket (`--transport ws`). Auto-discovered by Claude Desktop and Cursor when registered. Exposes one tool per CLI command; tool schemas mirror REST request schemas. Same token as REST. The MCP server is a thin adapter over the REST API — no business logic lives in the MCP layer.

CLI: `carve mcp-serve`, `carve mcp-serve --transport ws --port 8766`. The MCP server itself doesn't have a REST sub-resource — it's a sibling interface.

**Local Web UI.** Pure static HTML regenerated per run (decision 5.10, modeled on `dbt docs`). No live updates, no auth beyond local-host binding. Rendered pages: index (run history), per-run detail (logs + step graph + cost), pipelines list. Lineage view deferred (per audit decision). Served by `carve docs serve` on `127.0.0.1:8766` by default. The polished cloud UI is part of the hosted product.

**Acceptance:** every CLI command has a corresponding REST endpoint and MCP tool; the OpenAPI spec at `/api/openapi.json` is complete and accurate; CLI exit codes are stable across minor releases.

### 6.14 Observability

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

Most major design questions are resolved in [`specs/_strategy/2026-06-control-plane.md`](./_strategy/2026-06-control-plane.md), [`specs/_strategy/2026-06-ai-harness.md`](./_strategy/2026-06-ai-harness.md), and the prior [`specs/_strategy/2026-05-positioning.md`](./_strategy/2026-05-positioning.md) (with the now-historical [`spec-audit.md`](./_strategy/spec-audit.md)). What remains is implementation-level and resolves at spec-writing time:

- The default Carve documentation site framework (mkdocs-material vs docusaurus vs starlight)
- Whether the local static HTML UI uses pure file regeneration or a tiny read-only FastAPI app reading from the state store
- The exact set of webhook event payload schemas (will firm up as we write the runtime spec)
- Whether `carve serve` in OSS auto-detects a `DATABASE_URL` env var (Heroku-style) for external Postgres
- The MCP transport default for v0.1 (stdio-only first vs. stdio + WebSocket from day one)

## 12. Appendix — naming and brand

- Project name: **Carve**
- GitHub org: `carve-data` (placeholder; check availability before claiming)
- Canonical tagline: **"Build, schedule, and monitor pipelines — all with AI."** Earlier candidates (kept for reference): "Agent-native data engineering," "The warehouse lifecycle, authored by intent," "dlt + dbt with agents and a runtime"
- Brand vocabulary: agents *chisel*, skills are *blades*, builds are *carvings*. Use sparingly — don't overdose on the metaphor.
- Color, logo, marketing site: deferred to a branding pass closer to v0.1 launch.

**Acceptance:** `carve deploy` produces a PR within 60 seconds for a typical pipeline; refuses to run against drifted config.
