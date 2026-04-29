# Reference — Glossary

Terms used throughout the Carve documentation, codebase, and UI. Where a term has a specific meaning in Carve different from its general meaning, the Carve definition takes precedence.

---

**Agent**
A configured LLM with a system prompt and a set of skills. Carve ships four built-in agents (orchestration, dbt-engineer, snowflake-engineer, quality) and supports user-defined ones. An agent is a TOML file in `carve/agents/` plus optional Python customization.

**Agent loop**
The conversational pattern where an agent receives a goal, calls skills (tools), reads results, and continues until it produces a final answer or hits a limit. Implemented in `src/carve/agents/loop.py`.

**Apply**
The verb for executing a plan. After `carve plan` produces a plan, `carve apply <plan-id>` executes it. Modeled on Terraform's `terraform apply`.

**Approval step**
A pipeline step type that pauses execution and waits for a human to confirm before proceeding. Used for production deploys, destructive operations, or anything else where humans should be in the loop.

**Backfill**
Re-running a pipeline (or subset of steps) for a range of dates in the past. Used when source data was missing or wrong, and downstream tables need recomputation.

**Brownfield**
Onboarding into an existing dbt project. Carve detects the project structure, infers conventions, and generates configuration without modifying existing files. Contrast with greenfield.

**Capability flow**
Carve's mental model: user goal → agent decomposes → specialist agents execute → code is written → code runs → state changes. Documented in `ARCHITECTURE.md`.

**Carve runner**
The execution backend. M1 ships `LocalVenvRunner` (subprocess in a managed venv); future backends include Docker, Kubernetes, and managed cloud runners.

**CEL**
Common Expression Language — Google's expression language used for Carve's `if` conditions on steps. Chosen for its small, sandboxed footprint vs full Python eval.

**Conventions**
The house style for a project, expressed in `carve/conventions.md`. Naming, layout, tagging, test patterns. Loaded into agent context so generated code matches existing code.

**Convention inference**
The brownfield process where Carve scans an existing dbt project and produces a `conventions.md` reflecting current practice. The user can edit; Carve doesn't claim the inference is correct.

**DAG**
Directed acyclic graph. Used in two places: pipelines (steps depending on other steps) and dbt models (refs forming a graph).

**dbt**
[Data build tool](https://docs.getdbt.com/) — the SQL transformation framework Carve integrates with deeply. Carve treats dbt-core as a runtime dependency and assumes dbt projects are the primary modeling format.

**dbt manifest**
The compiled representation of a dbt project, produced by `dbt parse`. JSON file at `target/manifest.json`. Carve reads this to understand model relationships, descriptions, and tests.

**DCO**
Developer Certificate of Origin — a sign-off mechanism (`git commit -s`) certifying the contributor has the right to submit the contribution. Used instead of a Contributor License Agreement.

**Embedding search**
Semantic search over indexed content (model descriptions, column docs, etc.) using vector embeddings. Carve indexes the dbt manifest and convention files for the schema-search skill.

**Event bus**
The internal pub/sub for run events (step started, step completed, log line). Implemented as in-process async broadcast; consumed by the WebSocket bridge for real-time UI.

**Event-driven UI**
The UI updates in response to events from the event bus, not polling. A pipeline run's state flows live to all connected viewers.

**Fixture**
A test artifact: a sample dbt project, a synthetic Snowflake response, a recorded MCP server interaction. Lives under `tests/fixtures/`.

**Greenfield**
Initializing a new project from scratch (no existing dbt). Carve scaffolds `dbt_project.yml`, sample models, and configuration. Contrast with brownfield.

**Guardrails**
Configurable rules the orchestration agent enforces before applying changes: approval requirements, cost limits, forbidden operations, schema restrictions. Defined in `carve/guardrails.toml`.

**Lineage**
The relationship graph between data assets. dbt provides model-level lineage. Carve also tracks pipeline-level lineage (which step produced which artifact).

**MCP**
[Model Context Protocol](https://modelcontextprotocol.io/) — Anthropic's standard for connecting LLMs to external tools. Carve consumes external MCP servers as namespaced skills (`mcp:server:tool`) and exposes itself as an MCP server.

**Orchestration agent**
The "general manager" agent. Has access to other agents (not raw skills). Decomposes user goals, delegates to specialists, enforces guardrails.

**Pipeline**
A named DAG of steps, scheduled or triggered manually. Defined in `carve/pipelines/<name>.toml`. The unit users think about; "run my daily revenue pipeline."

**Plan**
A persisted, hash-validated representation of intended changes. Produced by `carve plan` (or implicitly by `carve build`). Contains: file edits, pipeline changes, expected effects, cost estimate. Lives under `.carve/plans/`.

**Plan/apply**
The two-phase workflow for changes: plan first, review, apply only if accepted. Modeled on Terraform; gives users a chance to catch problems before they happen.

**Profile** (dbt)
A connection configuration for dbt, in `~/.dbt/profiles.yml` or in-project. Carve reads but does not write profiles by default.

**Profile** (Carve)
A named environment configuration (`dev`, `staging`, `prod`) selected via `--profile`. Determines which connections, runner, and guardrails apply.

**Quality agent**
The specialist for testing and data quality. Generates tests from data shape, identifies anomalies, recommends test coverage improvements.

**Run**
A single execution of a pipeline (or step). Has a unique ID, status, start/end times, and step-level subrecords. Persisted in the state DB.

**Schema retrieval**
The pattern of letting agents query the schema (catalog, manifest, lineage) on demand rather than memorizing it in the prompt. Implemented via the `schema.*` family of skills.

**Skill**
A function an agent can call. Skills can be Python callables (in `src/carve/skills/`), user code (in `carve/skills/`), or MCP tools from external servers. Skills have typed parameters validated by pydantic.

**Skills SDK**
The Python API for authoring custom skills. `from carve.skills import skill, SkillContext`. Documented in `M3-06`.

**Snowflake agent**
The specialist for Snowflake-specific work: warehouse sizing, role grants, query optimization, DDL crafting. Has connection-aware skills.

**Source of truth**
The authoritative location of state. In Carve, the source of truth for definitions is the git repo; the source of truth for run history is the state DB.

**State DB**
The SQLite (or Postgres) database that persists run history, plan store, agent versions, and audit log. Default: `carve/.carve/state.db`. Schema in `src/carve/db/`.

**Step**
The unit of execution. A step has a type (`python`, `sql`, `dbt`, `shell`, `http`, `agent`, `approval`), parameters, and dependencies. Steps compose into pipelines.

**Step type**
One of the seven built-in types, plus user-registered custom types. Each step type is a Python class implementing the `Step` protocol in `src/carve/steps/base.py`.

**Sub-agent**
A specialist agent invoked by orchestration. Sub-agents don't directly receive user goals; they receive scoped tasks from the orchestrator.

**Telemetry**
Anonymous usage data collected to understand how Carve is used. Opt-out via `CARVE_NO_TELEMETRY`. Documented in `docs/privacy.md`.

**Workbench**
The primary UI screen for daily use: goal input, active goals, task graph, artifact preview. Documented in `M2-11`.

**WebSocket bridge**
The component that translates internal event-bus events to WebSocket frames for the UI. Implemented in `src/carve/server/ws.py`.
