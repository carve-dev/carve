# Carve — Architecture

Technical deep-dive complementing the PRD. Read this once you're committed to building Carve and need the full mental model of how the pieces fit together.

## 1. The mental model

Carve is composed of five layers, each with a clear responsibility and a clear interface to its neighbors.

```
┌────────────────────────────────────────────────────────────┐
│  Web UI                                                    │
│  Workbench · Agent studio · Pipeline monitor · dbt runs    │
└─────────────────────────┬──────────────────────────────────┘
                          │ REST + WebSocket
┌─────────────────────────┴──────────────────────────────────┐
│  API server (FastAPI)                                      │
│  Routes, auth, log streaming                               │
└─────────────────────────┬──────────────────────────────────┘
                          │
┌─────────────────────────┴──────────────────────────────────┐
│  Core                                                      │
│  Config · Event bus · Plan store · State store · Lineage   │
└──────┬──────────────────────────────────────────┬──────────┘
       │                                          │
┌──────┴──────────────┐              ┌────────────┴──────────┐
│  Agent layer        │              │  Step + runner layer  │
│  Orchestration,     │              │  Python, SQL, dbt,    │
│  dbt, Snowflake,    │              │  shell, http, agent,  │
│  Quality, Pipeline  │              │  approval             │
│  + skills + MCP     │              │  + LocalVenvRunner    │
└─────────────────────┘              └───────────────────────┘
```

## 2. Components

### 2.1 API server

A FastAPI application that exposes:

- REST endpoints for managing pipelines, runs, plans, agents, and skills
- WebSocket endpoints for live log streaming and run event broadcasting
- Static file serving for the built web UI under `/`

The API server is the single integration point. The CLI talks to it. The web UI talks to it. External MCP clients (when Carve acts as a server) talk to it. There's no direct database access from anywhere else.

### 2.2 Core

The heart of Carve. Five subcomponents:

**Config** — loads `carve.toml` and the files in `carve/`. Validates everything against pydantic models. Resolves environment variable interpolation. Computes a config hash for plan validity. Provides typed accessors that the rest of the system uses.

**Event bus** — an in-process publish-subscribe system. Every interesting state transition emits an event: `run.queued`, `run.started`, `step.started`, `step.completed`, `step.failed`, `run.completed`, `run.failed`, `agent.invoked`, `skill.called`. Subscribers include: the state store (persistence), the WebSocket layer (UI updates), the runner (sequencing), the notification system (Slack alerts). For OSS, the bus is in-process. For SaaS, it can be backed by Redis Streams.

**Plan store** — persists plans to `.carve/plans/<plan_id>.json`. Provides query and diff operations. Plans are immutable once created; refinement creates a child plan with a parent reference.

**State store** — SQLAlchemy ORM over SQLite (default) or Postgres (SaaS). Tables: `runs`, `steps`, `logs`, `plans`, `pipelines`, `schedules`, `artifacts`, `events`. Indexed for the common queries: recent runs, runs by status, logs by run.

**Lineage** — maintains an in-memory representation of the dbt manifest plus Carve's own pipeline-to-source mappings. Exposes graph traversal queries used by the orchestrator's impact analysis.

### 2.3 Agent layer

Five built-in agents, each with a YAML definition and Python class:

**Orchestration agent** — the only agent that knows about other agents. Takes a goal, classifies it, gathers impact context, picks specialist agents, generates the task graph. Outputs a plan.

**Pipeline agent** — generates Python ingestion code for arbitrary source systems.

**dbt agent** — generates, modifies, and refactors dbt models, tests, and documentation.

**Snowflake agent** — manages Snowflake DDL, RBAC, warehouses, grants.

**Quality agent** — generates dbt tests, source freshness checks, anomaly detection rules.

For v0.1, the pipeline and Snowflake agents may be combined with their parent specialist (dbt agent absorbs Snowflake's role for simple cases) until clear boundaries emerge.

Each agent has access to a curated set of skills. Skills are how agents do anything — read a file, query Snowflake, look up a dbt model, generate SQL, etc. The agent is the reasoning loop; skills are the tools.

### 2.4 Skills

Atomic, testable capabilities. Three types:

- **Built-in skills** — ship with Carve, live in `src/carve/skills/`
- **Custom skills** — Python files in `carve/skills/` of the user's project, decorated with `@skill`
- **MCP skills** — declared in `carve/mcp_servers.toml`, exposed as namespaced skills

Each skill declares typed inputs and outputs (Pydantic models), a description (consumed as the LLM tool schema), and an implementation. Skills receive a `SkillContext` for accessing connections, logging, and event emission.

### 2.5 Step + runner layer

A pipeline is a directed graph of steps. Each step has a type (`python`, `sql`, `dbt`, `shell`, `http`, `agent`, `approval`), a config, a list of dependencies, and a failure mode.

The step DAG executor walks the graph, fires `step.queued` events for ready steps, and waits for `step.completed`/`step.failed` to fire downstream. Steps within a pipeline can run in parallel when their dependencies allow.

Each step type has a runner. The `Runner` protocol:

```python
class Runner(Protocol):
    def execute(self, step: Step, context: RunContext) -> RunHandle: ...
    def stream_logs(self, run_id: str) -> AsyncIterator[LogLine]: ...
    def get_status(self, run_id: str) -> RunStatus: ...
    def cancel(self, run_id: str) -> None: ...
```

For v0.1, only the `LocalVenvRunner` (Python steps), `SqlRunner` (SQL steps), `DbtRunner` (dbt steps), `ShellRunner` (shell steps), and `HttpRunner` (HTTP steps) exist. The `DockerRunner` and `KubernetesRunner` are part of the SaaS / future roadmap.

## 3. The execution flow

A complete run, from goal to data in the warehouse:

1. User submits a goal via the workbench or CLI: `carve plan "make stg_orders incremental"`
2. The orchestration agent receives the goal
3. It runs goal classification (modification, in this case)
4. It calls `analyze_impact` skills: dbt manifest queries reveal `stg_orders` exists, has 4 downstream models, is currently materialized as a view
5. It calls agent selection (deterministic + LLM): picks the dbt agent and the quality agent, skips pipeline and Snowflake
6. It generates a task graph: dbt agent step (modify stg_orders) → quality agent step (add incremental tests) → PR step
7. It computes cost and duration estimates
8. The plan is written to `.carve/plans/plan_xxx.json`
9. The CLI prints the plan summary; the user reviews
10. User runs `carve apply plan_xxx`
11. Apply checks the config hash, refuses if drifted
12. The first step (dbt agent) is invoked with pre-scoped context: the goal, the current `stg_orders` SQL, the downstream model SQLs, and the project's conventions
13. The dbt agent's reasoning loop runs: read SQL, generate modified SQL with incremental config, write the file
14. `step.completed` fires
15. The quality agent runs: reads the new model, generates a uniqueness test on `unique_key`, writes the YAML
16. The PR step runs: commits everything to a feature branch, opens a GitHub PR with the plan attached
17. Run completes; state store records the result; UI updates via WebSocket
18. User reviews the PR, merges
19. Later, the scheduler triggers `salesforce_opps` pipeline because of its cron schedule
20. The pipeline's step DAG executes: extract step → dbt step (which now includes the new incremental `stg_orders`) → notify step
21. Logs stream to UI throughout

## 4. Data flow

### 4.1 Configuration data flow

```
carve.toml + carve/*.toml + carve/agents/*.yaml + carve/skills/*.py
        │
        ▼
   Config loader (validates, interpolates env vars, computes hash)
        │
        ▼
   In-memory Config object (singleton, accessible everywhere)
```

### 4.2 Plan data flow

```
User goal → Orchestration agent → Plan object
                    │                  │
                    │                  ▼
                    │             .carve/plans/plan_xxx.json
                    │
                    ▼
            Skill calls (catalog queries, manifest queries)
            during context gathering
```

### 4.3 Run data flow

```
Plan → carve apply → Step DAG executor → Runners
                          │                  │
                          │                  ▼
                          │          Subprocess execution
                          │          (Python, SQL, dbt, shell)
                          │                  │
                          │                  ▼
                          │            Log lines + status
                          │                  │
                          ▼                  ▼
                   Event bus ←────────────────
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
        State store   WebSocket   Notifications
          (persist)    (UI)         (Slack etc.)
```

## 5. Extension points

Five places outside developers can extend Carve without modifying core:

### 5.1 Custom skills

Drop a Python file in `carve/skills/` with a `@skill`-decorated function. Carve discovers it on startup.

```python
@skill(
    name="check_warehouse_health",
    description="Check Snowflake warehouse queue depth",
    inputs={"warehouse": "str"},
    outputs={"queued": "int", "running": "int"},
)
def check_warehouse_health(ctx, warehouse: str):
    return {"queued": 0, "running": 1}
```

### 5.2 Custom step types

Drop a class file in `carve/steps/` inheriting from `StepType`.

```python
class DatadogMetricStep(StepType):
    name = "datadog_metric"
    config_schema = DatadogMetricConfig

    def execute(self, ctx, config):
        # implementation
        return StepResult(status="success", outputs={...})
```

### 5.3 Custom agents

Less common. Define an agent YAML in `carve/agents/` and (optionally) a Python class in `carve/agents/` for custom logic. Agents are mostly configuration, so most teams won't need a Python class.

### 5.4 Custom runners

For new execution backends (e.g., Lambda, Cloud Run). Implement the `Runner` protocol, register it in `carve/runners.toml`. This is what the SaaS version's `DockerRunner` uses.

### 5.5 MCP servers

Declare an external MCP server in `carve/mcp_servers.toml`. Its tools become namespaced skills.

## 6. The agent reasoning loop

Each agent invocation works the same way:

1. Receive: the goal, pre-scoped context (relevant code, conventions, dependencies), and the list of allowed skills
2. Build the system prompt: agent's base prompt + conventions doc + skills as tool schemas
3. Call the LLM with messages
4. If the LLM requests tool use:
   a. Validate the tool call against the skill's input schema
   b. Validate against guardrails (forbidden actions, etc.)
   c. Execute the skill via `SkillContext`
   d. Add the tool result to messages
   e. Loop
5. If the LLM produces a final answer, return it
6. Track token usage and emit `agent.invoked` event

A few details that matter:

- **Token budget per agent invocation.** A soft cap (configurable, default 50K tokens of skill outputs in context). When approaching the cap, the orchestrator surfaces this and asks if the user wants to refine.
- **Skill call caching within a run.** Identical skill calls in the same run return cached results. Tracked at the agent level, automatic.
- **Result truncation.** A skill returning 1,000 rows truncates with a flag. The agent has to be specific.

## 7. The plan/apply lifecycle

Plans are first-class objects:

```python
class Plan(BaseModel):
    id: str
    created_at: datetime
    expires_at: datetime
    parent_plan_id: Optional[str]
    goal: str
    config_hash: str
    carve_version: str
    task_graph: list[Task]
    estimates: PlanEstimates
    guardrail_check: Literal["passed", "failed"]
    file_diffs: list[FileDiff]
```

Plans support these operations:

- **Create** — generated by `carve plan "<goal>"`
- **Refine** — `carve plan --refine <id> "<adjustment>"` produces a child plan
- **Show** — print or render the plan
- **Diff** — compare two plans
- **Apply** — execute, with config-hash validation
- **Expire** — automatically purged after `expires_at`

The config hash check at apply time is the safety net. If you generate a plan, edit a guardrail, then try to apply, Carve refuses and asks you to re-plan. This prevents stale plans from running against drifted config.

## 8. Schema retrieval

Five layers, in increasing cost:

1. **Catalog queries** — `INFORMATION_SCHEMA` queries against Snowflake. Cheap, deterministic, exact.
2. **dbt manifest queries** — load `target/manifest.json`, expose structured queries (downstream of, columns of, tests on).
3. **File grep** — ripgrep over the dbt repo for exact-match references.
4. **Lineage traversal** — graph queries over the manifest's dependency graph.
5. **Embedding search** — semantic search for fuzzy concepts ("customer churn metrics"). Returns pointers, not full content.

The agent doesn't pick a layer. The agent picks a *skill*; skills are implemented using the appropriate layer. Skills 1-4 ship in M2; skill 5 ships in M3.

## 9. The OSS-to-SaaS architectural seams

Five design decisions made now to keep the SaaS pivot painless later:

### 9.1 SQLAlchemy from day one

The state store uses SQLAlchemy with SQLite. SaaS migrates to Postgres via a connection string change.

### 9.2 The `Runner` protocol

`LocalVenvRunner` is the OSS implementation. `DockerRunner` is the SaaS implementation. The agent layer and event bus never know which is in use.

### 9.3 The event bus abstraction

In-process for OSS. Redis Streams for SaaS. Subscribers don't change.

### 9.4 Auth as a first-class concept even in single-user mode

Every run has an `owner_user_id` field (always `1` in single-user mode). Every action goes through a permission check that returns `true` for everyone in single-user mode. The schema is multi-user-ready.

### 9.5 Telemetry hooks baked in early

Run durations, success rates, row counts, token usage. These power the SaaS dashboards and usage-based billing later.

## 10. Security boundaries

A few security properties Carve maintains:

### 10.1 Secrets never in committed files

All sensitive values come from environment variables via `${VAR_NAME}` interpolation. The config loader validates this and refuses to start if a sensitive field is hardcoded.

### 10.2 Generated code runs isolated

The `LocalVenvRunner` creates a fresh venv per pipeline (cached, but isolated from system Python). The script runs as a subprocess. No access to Carve's process memory or its credentials.

### 10.3 LLM provider keys are scoped

The agent layer is the only component that can see LLM provider keys. They are passed via the SDK, never logged, never echoed to the user.

### 10.4 File system writes are scoped

All Carve writes are scoped to the project directory and `.carve/`. Nothing writes to `/etc`, `~/.bashrc`, or anywhere outside the user's project.

### 10.5 Snowflake credentials follow the role

Generated SQL runs under whatever role Carve is configured to use (`CARVE_DEV` or `CARVE_PROD`). Best practice is to grant Carve a role with only the privileges needed for its work — typically schema-level USAGE, plus CREATE on managed schemas. Carve doesn't need ACCOUNTADMIN.

## 11. Performance characteristics

Some numbers that drive design decisions:

- **State store growth:** ~10KB per run (logs separate). 10,000 runs = 100MB. Easy for SQLite.
- **Log volume:** ~100KB per typical run. Compressed in storage. 10,000 runs = ~1GB. Still SQLite-friendly.
- **Embedding index size:** ~100-300MB for a typical mid-size warehouse. Local-friendly.
- **Plan generation latency:** dominated by LLM calls. Typical: 5-15 seconds for the orchestrator + 10-30 seconds for the specialist agents.
- **Pipeline run startup:** <10s overhead from venv activation, subprocess spawn, log subscription.
- **Concurrent run limit:** configured in `carve/runner.toml`. Default 4 concurrent for OSS.

## 12. Failure modes and recovery

A few categories of failure and how Carve handles them:

### 12.1 Transient failures (network, rate limits)

Automatic retry with exponential backoff at the step level. Configurable per step.

### 12.2 Data failures (schema drift, primary key violations)

Fail loudly, no retry. Surface the actual error in the UI with the SQL or Python traceback. Provide a "rerun from this step" button.

### 12.3 Logic failures (bug in generated code)

Caught by the plan/apply review process or by CI tests on the generated PR. Once in production, surface clearly and let the user trigger a refinement (`carve plan --refine`).

### 12.4 Partial pipeline failures

Each step has independent state. The user can:
- Retry from failure (only the failed step + its dependents)
- Retry full run (start over)
- Skip and continue (mark the failed step as manually resolved)

### 12.5 Crashed Carve process

On startup, the API server scans the state store for runs marked `running` with no recent log activity. These are marked `crashed` and the user is notified. State is consistent because every state transition is committed atomically.

## 13. What's deliberately not in this architecture

A few decisions worth being explicit about:

- **No message broker** for v0.1. The event bus is in-process. Adding Redis or Kafka is a SaaS-stage decision.
- **No separate scheduler process.** The scheduler runs as part of `carve serve`. This is fine for one-engineer Carve usage; obviously not for SaaS.
- **No Kubernetes operator.** Running Carve in K8s is fine (it's just a process), but Carve doesn't ship a K8s-native abstraction.
- **No GraphQL.** REST + WebSocket is enough. GraphQL is overkill for the API surface.
- **No service mesh.** Carve is one process; service mesh concepts don't apply.
- **No data lake.** Carve writes to Snowflake, full stop. Other warehouses come later.
