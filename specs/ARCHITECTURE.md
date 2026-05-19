# Carve — Architecture

> Last major revision 2026-05-19, aligned to [`_strategy/2026-05-positioning.md`](./_strategy/2026-05-positioning.md) and [`PRD.md`](./PRD.md). For the prior version, see [`_archive/ARCHITECTURE-pre-2026-05-positioning.md`](./_archive/ARCHITECTURE-pre-2026-05-positioning.md).

## 1. Mental model

Carve is composed of five layers in the OSS, plus a hosted overlay that wraps and extends them.

```
┌──────────────────────────────────────────────────────────────────┐
│  External clients                                                │
│  CLI · Claude Desktop · Cursor · Claude Code · custom agents · CI│
└─────────────────────────────┬────────────────────────────────────┘
                              │ REST + MCP + WebSocket / SSE
┌─────────────────────────────┴────────────────────────────────────┐
│  Interfaces                                                      │
│  FastAPI server · MCP server (stdio + WS) · Static HTML UI       │
└─────────────────────────────┬────────────────────────────────────┘
                              │
┌─────────────────────────────┴────────────────────────────────────┐
│  Core                                                            │
│  Agent layer · Plan/Build store · State store (Postgres)         │
│  Event bus · Skills registry · Conventions · Lineage             │
└──────────┬────────────────────────────────────────┬──────────────┘
           │                                        │
┌──────────┴─────────────────┐         ┌────────────┴─────────────┐
│  Runtime                   │         │  External backends       │
│  Scheduler · Job queue     │ invokes │  dlt · dbt · destination │
│  Workers · Heartbeats      ├────────►│  warehouse (Snowflake,   │
│  Retry · Failure modes     │         │  Postgres, BigQuery, …)  │
└────────────────────────────┘         └──────────────────────────┘
```

The hosted product wraps the above:

```
┌──────────────────────────────────────────────────────────────────┐
│  Hosted control plane (private repo, paid)                       │
│  Multi-tenant routing · SSO / OAuth / RBAC · Audit log           │
│  Cloud UI · Service accounts · Billing · Premium integrations    │
└─────────────────────────────┬────────────────────────────────────┘
                              │ wraps + extends (depends on OSS)
┌─────────────────────────────┴────────────────────────────────────┐
│  OSS core (the five layers above, unchanged)                     │
└──────────────────────────────────────────────────────────────────┘
```

Three properties of this picture worth naming:

1. **External clients are peers.** The CLI and Claude Desktop and a CI workflow all sit at the same level, all talk to the same Interfaces layer through the same protocols. The CLI is not privileged.
2. **The Runtime is a peer of the Agent layer, not a subordinate.** Plans and builds happen in the Agent layer; runs happen in the Runtime. The two share the State store but are otherwise independent — agents can be busy planning while workers are busy running.
3. **External backends are external.** dlt, dbt, and the destination warehouse are not internal Carve modules — they are independent OSS projects that Carve invokes. When dlt fixes a bug in its Snowflake adapter, Carve users benefit on their next `pip install -U dlt` without a Carve release.

## 2. Components

### 2.1 Interfaces

Three interfaces ship in v0.1, all backed by the same FastAPI service:

- **REST API** — `/api/v1/...` with OpenAPI schema at `/api/openapi.json`. Auth via `Authorization: Bearer <token>` header. Errors as `application/problem+json`. WebSocket and SSE for live streams.
- **MCP server** — Standard Anthropic MCP protocol over stdio (default) or WebSocket. Each MCP tool is a thin adapter over a REST endpoint; the tool schema mirrors the endpoint's request schema. No business logic in the MCP layer.
- **Static HTML UI** — Regenerated per run by a template renderer reading from the State store. Modeled on `dbt docs serve`. No live updates; no auth beyond loopback binding.

The **CLI** is a fourth client of the above. It talks to the FastAPI service over HTTP; a small subset of commands (`plan`, `build`) can run in-process without a server for one-shot use.

The hosted overlay adds a **polished cloud UI** as a fourth interface, plus a public REST gateway with multi-tenant routing and SSO-aware auth. The cloud UI is a separate React app in the private repo; the OSS-side static UI does not evolve into it.

### 2.2 Core

The heart of Carve. Six subcomponents:

- **Agent layer** — Anthropic SDK reasoning loop, orchestration agent, specialist agents (extract-load, runtime; dbt specialist in v0.2). Token budget enforcement, skill-call caching, structured plan output.
- **Plan/Build store** — Plans persisted as `.carve/plans/<id>.json` plus index rows in Postgres. Builds persisted as `Build` rows referencing on-disk artifacts (dlt pipelines, dbt models, `pipelines/*.toml`). Both are immutable once created; refinement creates child plans.
- **State store (Postgres)** — SQLAlchemy 2.0 declarative models. Tables: `runs`, `steps`, `logs`, `plans`, `builds`, `pipelines`, `schedules`, `agents_invocations`, `skill_calls`, `events`, `webhooks`. Indexed for the common queries; concurrent writes safe under multi-worker (decision 5.7).
- **Event bus** — In-process publish-subscribe in OSS; replaceable by Redis Streams in the hosted product without changing subscribers. Events: `run.queued`, `run.started`, `step.*`, `run.completed`, `agent.invoked`, `skill.called`.
- **Skills registry** — Catalog of built-in skills (`src/carve/skills/`) plus MCP-imported skills (namespaced `mcp:server:tool`). Each skill declares typed inputs/outputs and a description. Skills receive a `SkillContext` for connections, logging, and event emission.
- **Conventions + lineage** — Convention inference reads existing dbt projects and writes `carve/conventions.md`. Lineage maintains a graph of dlt-resource → destination-table → dbt-source dependencies; queryable via skills but not yet rendered in the v0.1 static UI.

### 2.3 Runtime

A standalone process model that schedules and executes pipelines:

- **Scheduler** — A loop that polls the `schedules` table, computes which pipelines are due, and inserts rows into the job queue. Runs every 30 seconds. Stateless beyond the database.
- **Job queue** — A Postgres table (`jobs`) with status (`queued`, `claimed`, `running`, `succeeded`, `failed`), `claimed_by` (worker ID), `heartbeat_at` (last heartbeat from worker). Claims happen via optimistic `UPDATE ... WHERE status = 'queued'`.
- **Worker** — A process that loops: claim → execute → mark complete → repeat. Each worker handles one job at a time; concurrency comes from running multiple workers. Workers emit heartbeats every 10 seconds while a job is in progress.
- **Crash recovery** — A reaper loop checks for workers with stale heartbeats (> 60 seconds) and resets their jobs to `queued` so another worker can pick them up.
- **Step executors** — One per step type. `dlt`, `dbt`, and `sql` in v0.1. Each is a subprocess invocation (shelling out to `dlt pipeline run`, `dbt build`, or executing SQL against the target connection) with structured log capture and output extraction.

Detailed in §4.

### 2.4 External backends

Three external projects that Carve invokes but does not own:

- **dlt** (`pip install dlt`) — The extract-load runtime. Carve generates dlt code (sources, resources, configs in `.dlt/`) and shells out to `dlt pipeline run` to execute it. dlt owns schema inference, incremental state, type coercion, destination adapters.
- **dbt-core** (`pip install dbt-core` + adapter) — The transform runtime. Carve generates dbt models (post-v0.2) and invokes `dbt build`, `dbt run`, `dbt test`. dbt owns the model DAG, materializations, test framework, manifest.
- **Postgres** — The state store. Bundled via docker-compose for first-run; users override the connection string for managed Postgres in production. Carve manages migrations via Alembic.

### 2.5 The hosted overlay

The hosted product is implemented in a private repo that depends on the OSS repo as a library. It adds:

- **Multi-tenant routing** — A request reaches a tenant's API server based on subdomain, header, or token mapping
- **SSO / OAuth / RBAC** — Identity providers (Google, Okta, Azure AD), service accounts, role enforcement on every request
- **Audit log** — Every API call recorded with actor, timestamp, request body, response status; queryable by admins
- **Plan-approval workflows** — Builds and deploys can be gated on admin approval
- **Cloud UI** — A React app with live monitoring, lineage, cost dashboards, deploy approval flows. Talks to the same OSS REST API plus hosted-only endpoints
- **Premium integrations** — PagerDuty, Datadog, Slack with formatted payloads
- **Hosted secrets** — Vault-backed credential storage shared across an org
- **Billing** — Usage-based metering on agent runs and execution minutes

Hosted-only endpoints live under `/api/v1/hosted/...` and are unavailable in OSS installations. Everything else is shared.

## 3. Code layout

The OSS lives in one Python package (`src/carve/`) plus tests and docs:

```
src/carve/
├── cli/                       # typer commands; one module per command group
│   ├── plan.py · build.py · run.py · deploy.py · schedule.py
│   ├── pipelines.py · agents.py · skills.py · mcp_servers.py
│   ├── runs.py · logs.py · metrics.py · docs.py
│   ├── serve.py · worker.py · mcp_serve.py
│   └── init.py
├── core/
│   ├── agents/                # agent definitions + reasoning loop
│   │   ├── orchestration.py · extract_load.py · runtime.py
│   │   └── loop.py            # the Anthropic SDK tool-use loop
│   ├── skills/                # built-in skills
│   │   ├── catalog.py · manifest.py · file_io.py · git.py · ...
│   ├── conventions/           # convention inference from dbt projects
│   ├── plan/                  # Plan + Build entities and store
│   ├── state/                 # SQLAlchemy models + repositories
│   ├── events/                # in-process event bus
│   ├── config/                # config loader, validation, hash
│   └── lineage/               # dlt-resource → table → dbt-source graph
├── runtime/                   # scheduler, queue, workers
│   ├── scheduler.py · job_queue.py · worker.py · heartbeat.py · reaper.py
│   └── step_types/
│       └── dlt.py · dbt.py · sql.py
├── api/                       # FastAPI app — see §8
│   ├── main.py · auth.py · streams.py · webhooks.py
│   └── routers/
│       └── plans.py · builds.py · runs.py · deploys.py · schedules.py · …
├── mcp/                       # MCP server (thin adapter over REST)
│   ├── server.py · transports.py
│   └── tools/                 # one tool per REST endpoint
├── ui/                        # static HTML generator
│   ├── generator.py
│   └── templates/
├── integrations/
│   ├── dlt/                   # dlt source generation + invocation helpers
│   └── dbt/                   # dbt project reader, manifest parsing, invoker
├── sources/                   # curated dlt source library
│   └── stripe/ · shopify/ · hubspot/ · salesforce/ · …
└── __main__.py                # `python -m carve` entry point
```

The hosted repo (private) mirrors this structure:

```
carve-hosted/
├── src/carve_hosted/
│   ├── tenancy/               # request routing, tenant isolation
│   ├── auth/                  # SSO, OAuth, RBAC enforcement
│   ├── billing/               # usage metering, invoicing
│   ├── audit/                 # audit log writer + query
│   ├── secrets/               # vault-backed secret store
│   ├── integrations/          # PagerDuty, Datadog, Slack formatters
│   ├── cloud_ui/              # React app source
│   └── api_extensions/        # hosted-only routers (push-button deploy, etc.)
├── deploy/
│   ├── kubernetes/
│   └── terraform/
└── pyproject.toml             # depends on carve (OSS) as a library
```

Three invariants on the layout:

1. **OSS code never imports from `carve_hosted`.** Imports go one direction: hosted depends on OSS.
2. **Every CLI command in `src/carve/cli/` has a corresponding REST router module and an MCP tool module.** The three are kept in lockstep by integration tests.
3. **Step types live together in `src/carve/runtime/step_types/`.** Adding a new step type means one new file there plus its CLI/REST/MCP surface.

## 4. The runtime in detail

### 4.1 Scheduler

The scheduler is a single loop inside `carve serve` (replicated in hosted deployments with leader election). Every 30 seconds it:

1. Queries the `schedules` table for entries due now (cron evaluated against now + skew tolerance)
2. For each due schedule, computes the canonical `scheduled_for` timestamp (the cron-tick time, not now)
3. Attempts to insert a row into the `jobs` table: `(pipeline, target, scheduled_for, status='queued', trigger='scheduled')`
4. Updates the schedule's `last_fired_at`
5. Sleeps until the next tick

Idempotency is enforced primarily by §4.2's partial unique index on `(pipeline) WHERE status='queued'`: at most one queued job per pipeline at any moment. The scheduler insert is therefore conditional:

- No queued job exists for the pipeline → insert normally
- A queued job already exists (scheduled or manual) → no-op; emit a `schedule.skipped` event recording the missed tick

This is the same dedup mechanism manual triggers use (§4.3), so the two paths share one rule rather than two.

Manual triggers (`carve run`, REST, MCP) also write into `jobs` with `trigger='manual'`. Unlike scheduled fires they bypass the cron loop, but they hit the same uniqueness constraint and the same dedup behavior described in §4.3.

### 4.2 Job queue (active + archive)

The active `jobs` table holds queued, claimed, running, and recently-completed rows. The schema:

```sql
jobs (
  id UUID PRIMARY KEY,
  pipeline TEXT NOT NULL,
  target TEXT NOT NULL,
  status TEXT NOT NULL,              -- queued, claimed, running, succeeded, failed, cancelled
  trigger TEXT NOT NULL,             -- scheduled, manual, api, mcp
  scheduled_for TIMESTAMPTZ,         -- cron-tick time for scheduled jobs; NULL otherwise
  claimed_by TEXT,
  claimed_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  run_id UUID,                       -- FK to runs once worker creates one
  created_at TIMESTAMPTZ NOT NULL
);

-- At most one queued job per pipeline
CREATE UNIQUE INDEX ix_jobs_one_queued_per_pipeline
  ON jobs(pipeline) WHERE status = 'queued';

-- At most one running job per pipeline
CREATE UNIQUE INDEX ix_jobs_one_running_per_pipeline
  ON jobs(pipeline) WHERE status = 'running';

-- Claim-loop friendly
CREATE INDEX ix_jobs_status_created_at
  ON jobs(status, created_at) WHERE status IN ('queued', 'claimed');
```

Lifecycle: `queued → claimed → running → succeeded | failed | cancelled`.

**Archive flow.** Completed jobs (`succeeded`, `failed`, `cancelled`) older than a configurable window are moved out to `jobs_archive`. An archiver loop runs hourly:

1. `SELECT` completed rows where `finished_at < now() - INTERVAL '<jobs_window>'`
2. `INSERT INTO jobs_archive (...)` with same schema (partitioned by month in hosted; single table in OSS)
3. Verify count match between selected and inserted
4. `DELETE FROM jobs` only after verification succeeds
5. Emit `archive.batch_completed` with row count

The same archive pattern applies to `runs` (→ `runs_archive`), `logs` (→ `logs_archive`), and `steps` (→ `steps_archive`). Each has its own configurable window:

```toml
[runtime.archive]
jobs_window  = "7d"
runs_window  = "30d"
logs_window  = "30d"
steps_window = "30d"
```

Reads against active tables stay O(active_count). Reporting queries that need history join active + archive via SQL views (`jobs_all`, `runs_all`, etc.). The hosted product can use Postgres native partitioning on the archive tables for faster historical queries.

### 4.3 Optimistic claim and dedup semantics

Workers claim work via:

```sql
UPDATE jobs SET status='claimed', claimed_by=$worker_id, claimed_at=now(), heartbeat_at=now()
WHERE id = (
  SELECT id FROM jobs WHERE status='queued'
  ORDER BY scheduled_for NULLS LAST, created_at ASC
  LIMIT 1 FOR UPDATE SKIP LOCKED
) RETURNING *;
```

`FOR UPDATE SKIP LOCKED` lets concurrent workers race without blocking. Each queued job is claimed by exactly one worker.

**Per-pipeline serialization is enforced by the `ix_jobs_one_running_per_pipeline` partial unique index** (§4.2), not by application-level checks. If a worker tries to transition `claimed → running` for a pipeline that already has a `running` row, the index conflict fires and the worker leaves the job in `claimed`, retrying briefly until the prior run finishes. (Alternative: release the claim back to `queued` — same end state.)

**Manual trigger dedup.** When a manual trigger lands and a queued job already exists for the pipeline, the partial unique index on `(pipeline) WHERE status='queued'` fires. The handler catches this and instead of inserting:

- `UPDATE jobs SET trigger='manual', scheduled_for=NULL WHERE pipeline=$p AND status='queued' RETURNING *;`
- Returns the existing row's `job_id` to the caller

Result: 50 manual requests in rapid succession against a pipeline that's currently running produce 1 running + 1 queued, not 50 queued. Requests 2 through 50 all return the same `job_id` as request 2. This is the same behavior whether requests come from the CLI, REST, or MCP.

The trade-off: when a manual trigger collides with a previously-scheduled queued job, the queued job's `trigger` gets promoted to `'manual'` (so the user "wins" — their record of "I asked for this" is preserved). The original `scheduled_for` is cleared because the job runs as soon as a worker is free, not at the cron time.

### 4.4 Worker process model

Each worker loops: claim → execute → mark complete. Each worker handles one job at a time; concurrency comes from running more workers.

Stable `worker_id` (typically `<hostname>:<pid>:<startup-uuid>`) registered in a `workers` table on startup; unregistered cleanly on graceful shutdown.

Two deployment shapes:

- `carve serve --workers N` — N worker tasks inside the API server process (OSS default, single-node)
- `carve worker` — standalone worker process (hosted, or scale-out OSS)

Both shapes coordinate via the same Postgres queue.

### 4.5 Heartbeats and the reaper

While a worker holds a job, it updates `heartbeat_at` every 10 seconds. The reaper runs every 30 seconds and reclaims jobs whose heartbeat is stale:

```sql
UPDATE jobs SET status='queued', claimed_by=NULL, claimed_at=NULL, heartbeat_at=NULL
WHERE status IN ('claimed', 'running') AND heartbeat_at < now() - INTERVAL '60 seconds'
RETURNING id;
```

Reclaimed jobs emit `job.reclaimed` with the prior `claimed_by`. The next worker runs the job from scratch; partial state is discarded.

### 4.6 Step executors and the pipeline DAG

Once a worker claims a job, it loads `pipelines/<name>.toml` and the current build's manifest, then walks the step DAG:

1. Compute topological order with intra-level parallelism slots
2. For each ready step (deps complete, free slot), invoke its executor
3. Capture stdout/stderr into structured log lines
4. On completion: record status, extract named outputs, fire `step.completed`
5. On failure: apply the step's failure mode

Step executors are subprocess invocations:

- **`dlt`**: `dlt pipeline run --pipeline <name>` in the project venv; parses dlt's structured trace for outputs
- **`dbt`**: `dbt build --select <selector> --target <target>` (or `run`, `test`); parses `run_results.json` for per-model status
- **`sql`**: opens a destination connection and executes the SQL file, optionally rendering through Jinja with cross-step outputs in scope

Outputs land in `steps.<step_id>.<key>` Jinja namespace for downstream steps.

### 4.7 Failure modes per step

Per PRD §6.9:

- `fail` (default): step fails → run fails immediately, remaining steps not started
- `warn`: step fails → record warning, continue downstream
- `continue`: step fails → record failure, continue downstream
- `retry { max_attempts = N, backoff = "exponential" | "linear" }`: retry up to N times then treat as `fail`
- `skip_downstream`: mark all transitively-dependent steps `skipped`; continue siblings

Failure mode is per-step in the pipeline TOML; the runtime enforces it uniformly across step types.

## 5. The agent layer in detail

### 5.1 Agents and specialization

Carve has one orchestration agent and a small set of specialists:

- **Orchestration agent** — the only agent that knows about other agents. Classifies a goal, gathers impact context, picks specialist(s), pre-scopes their context, produces a plan.
- **Extract-load (EL) specialist** — authors dlt sources/resources/pipelines and `.dlt/secrets.toml` / `.dlt/config.toml`. Knows the dlt API, the curated source library, the user's brownfield dlt conventions.
- **Runtime specialist** — authors `pipelines/<name>.toml`. Knows the step type set, failure modes, scheduling semantics.
- **dbt specialist (v0.2)** — authors dbt models, tests, `sources.yml` entries. Knows the user's brownfield dbt conventions.

Each specialist has a TOML definition in `carve/agents/<name>.toml` declaring: `model`, `system_prompt`, `allowed_skills`, `[guardrails]`. Specialists receive pre-scoped context from the orchestrator — they don't gather their own.

### 5.2 The orchestration agent's job

Given a user goal, the orchestrator:

1. **Classifies the goal** — new pipeline, modification, config change, schedule change, etc. Classification determines which skills to call.
2. **Gathers impact context** — catalog queries, dbt manifest queries, file grep, lineage traversal.
3. **Picks specialist(s)** — "modify stg_orders to be incremental" picks dbt only; "onboard Salesforce" picks EL + dbt + runtime.
4. **Pre-scopes context for each specialist** — minimal bundle: the goal slice, relevant file contents, relevant conventions, impacted dependencies.
5. **Generates a structured Plan** — JSON-schema-validated, with task graph, file diffs, cost estimate.

The orchestration agent never writes code itself. Its outputs are: a Plan, a list of (specialist, scoped context) tuples, and the skill-call trace.

### 5.3 The Anthropic SDK reasoning loop

Every agent invocation runs the same loop:

```python
messages = [{"role": "user", "content": prepare_user_message(goal, scoped_context)}]
while True:
    response = client.messages.create(
        model=agent.model,
        max_tokens=agent.max_tokens,
        tools=skill_schemas_for(agent),
        system=agent.system_prompt,
        messages=messages,
    )
    record_invocation(response.usage)
    if response.stop_reason == "end_turn":
        return parse_final_output(response)
    for tool_use in response.tool_uses:
        validate_against_guardrails(agent, tool_use)
        result = call_skill(tool_use)
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use.id, "content": result}
        ]})
    if iterations > agent.max_iterations:
        raise BudgetExceeded
```

Properties:

- Token usage recorded per invocation via `agents_invocations` rows
- Guardrails validated *before* skill execution — forbidden actions fail without invoking the skill
- Skill results cached within an invocation (identical tool calls return cached)
- Result truncation: outputs over the configured cap (default 50KB) return truncated with a flag

### 5.4 Pre-scoped context

The single most important property of the agent layer: specialists don't gather their own context. A typical pre-scoped bundle for the EL specialist:

```python
{
  "goal_slice": "Generate a dlt pipeline that ingests the Stripe charges API into raw_stripe",
  "memory": {
    "conventions": <relevant subset of carve/conventions.md>,    # inferred from code
    "standards":   <relevant subset of carve/standards.md>,      # user-authored team rules
    "decisions":   <relevant entries from carve/decisions.md>,   # included for `ask`; sparse for other verbs
    "pipeline_notes": <pipelines/<name>.md, if the goal touches a specific pipeline>,
    "el_notes":       <el/<name>/NOTES.md, if the goal touches a specific EL artifact>,
  },
  "destination": {"name": "snowflake", "schema": "raw_stripe"},
  "existing_sources": [<dbt source declarations this pipeline should match>],
  "dlt_library_match": "stripe",          # if a curated source applies
  "dlt_existing_pipelines": [<user-authored pipelines in brownfield mode>],
}
```

Specialists never need to run discovery skills themselves. This keeps their token budgets small and predictable.

**Memory file selection.** The orchestrator picks which memory files to include based on the goal:

- Every invocation includes `conventions.md` and `standards.md`
- Goals touching a specific pipeline include `pipelines/<name>.md` if it exists
- Goals touching a specific EL artifact include `el/<name>/NOTES.md` if it exists
- Investigative goals via `ask` always include `decisions.md` so "why" questions can be answered with citations
- Standards override conventions where they conflict; if `standards.md` says "always merge on PK" but `conventions.md` infers "we usually replace," the specialist follows standards

Memory files are mtime-cached in the same way as the dbt manifest (§6.3). Writes to memory files go through the plan/build flow — agents propose, users review, deploy lands. No autonomous memory writes; see PRD §6.3 for the policy rationale.

### 5.5 Plans as structured outputs

Plans are pydantic-validated JSON, not free-form text:

```python
class Plan(BaseModel):
    id: UUID
    created_at: datetime
    expires_at: datetime
    parent_plan_id: Optional[UUID]
    goal: str
    config_hash: str
    carve_version: str
    task_graph: list[Task]
    estimates: PlanEstimates
    guardrail_check: Literal["passed", "failed"]
    file_diffs: list[FileDiff]

class Task(BaseModel):
    id: str
    specialist: Literal["extract-load", "dbt", "runtime"]
    description: str
    inputs: dict
    expected_outputs: list[ExpectedOutput]
    depends_on: list[str]
```

This shape forces the orchestrator to produce something deterministic that build and deploy can validate. The `config_hash` is the safety net — refuses to run against drifted config.

### 5.6 Hot reload

Agent config files can be edited while `carve serve` is running. The next plan or build invocation re-reads `carve/agents/*.toml`. No separate "reload" command. Agent prompt iteration via `carve agents test` is fast: edit, test, edit, test, no restart in the loop.

### 5.7 User-provided agents and skills

**Custom agents.** Users can create custom agents alongside the built-ins, in v0.1.

- `carve agents create <name>` scaffolds a new `carve/agents/<name>.toml` with minimal config
- `carve agents create <name> --template <existing>` clones an existing agent's config (often `extract-load` or `runtime` as the starting point)
- Each agent file declares `name`, `model`, `system_prompt` (inline or path), `allowed_skills`, `[guardrails]`, and `[specialization]` — the last block tells the orchestrator which goal classifications this agent handles
- The orchestrator's specialist-picking step considers custom agents alongside built-ins; when a custom agent's `specialization` matches the classified goal, the orchestrator routes work to it
- Custom agents can also be invoked directly via `carve agents test <name> "<prompt>"` for one-shot use bypassing the orchestrator

Custom agents follow the same reasoning loop (§5.3) and the same plan/build/deploy lifecycle as built-ins. They appear in `agents list`, are reachable via REST and MCP equivalents, and respect the same guardrail and token-budget machinery.

**Custom skills (v0.1: via MCP only).** The Python custom-skill SDK (in-process `@skill`-decorated functions) is deferred per PRD §4.2 out-of-scope. The v0.1 supported path for "add a skill that does X" is: **register an external MCP server that provides it**.

- The user runs their own MCP server, written in any language (Python via `mcp-python`, TypeScript via `@modelcontextprotocol/sdk`, Go, Rust, etc.)
- They register it via `carve mcp-servers add <name> --url <url>`
- The server's tools appear in `skills list` as `mcp:<name>:<tool>`
- Any agent whose `allowed_skills` includes the matching pattern (e.g., `"mcp:internal-tools:*"`) can call those tools
- The MCP tool schema becomes the LLM-visible tool schema

This buys users full extensibility — any language, any logic, any backing service — without committing Carve to a Carve-specific custom-skill SDK that would have to be maintained, secured, sandboxed, and versioned. The hosted product can additionally expose private per-tenant MCP servers that are shared across users in an org.

Post-v0.1, an in-process Python skill SDK may ship if real demand emerges. The MCP-server path will remain supported indefinitely either way.

### 5.8 The curated dlt source library: copy on use

When the orchestrator's pre-scoped context includes `dlt_library_match: "<source_name>"`, the EL specialist **copies** the curated source code from `src/carve/sources/<source_name>/` into the user's project directory (e.g., `el/<pipeline_name>/`) and customizes it for the user's specific config (endpoint selection, target schema, credentials, write disposition).

This mirrors dlt's own `dlt init <source>` model: scaffold from a template, then the code is yours.

Properties:

- **Pipelines are self-contained** in the user's repo — there is no runtime dependency on `src/carve/sources/`. The user can `pip uninstall carve` and the generated dlt pipeline still runs against dlt directly.
- **User edits don't propagate back.** Customizations to `el/stripe_charges/` don't affect anyone else or the curated library.
- **Library updates don't auto-propagate.** When `carve/sources/stripe/` gains a bug fix in a future release, existing user pipelines are unaffected — they keep their copy.
- **Opt-in refresh.** Users who want library improvements can re-plan with `carve plan --pipeline stripe_charges "refresh from curated library"` (or equivalently `--from-library stripe --refresh`). The orchestrator generates a plan whose diff shows what would change. Approval and build proceed normally.
- **Provenance tracked.** Each generated pipeline file's header comment records: `# Generated from carve/sources/stripe at commit abc1234, customized for <user destination>`. This makes drift visible at a glance and gives support a debugging starting point.

In Carve's "brownfield dlt" mode (PRD §6.2 mode 2: orchestration only), the curated library is not used at all — Carve never overwrites user-authored dlt code. The library path is exclusive to authoring + orchestration mode.

## 6. Schema retrieval architecture

The agent layer never reads the full warehouse catalog or the full dbt manifest into its context. Instead, agents call typed skills that hit specific layers of a retrieval stack. Each layer has different cost, latency, and freshness characteristics.

### 6.1 The five layers

1. **Catalog queries (`INFORMATION_SCHEMA`)** — Cheap, deterministic, exact. Skills like `list_schemas`, `list_tables`, `describe_table`, `table_exists` map to destination `INFORMATION_SCHEMA` queries. Results are facts at query time, cached briefly (default TTL 60s).
2. **dbt manifest queries** — The dbt project's `target/manifest.json` is the source of truth for dbt structure. Skills like `list_models`, `model_columns`, `model_dependencies`, `tests_on_model` load the manifest (cached by mtime) and answer structured questions.
3. **File grep** — When an agent needs "where is column `customer_id` referenced," a ripgrep-backed skill (`grep_dbt_models`, `grep_dlt_code`) scans the project tree. Bounded by max match count (default 50) and per-match truncation.
4. **Lineage traversal** — Carve maintains its own lineage graph (§6.2). Skills like `downstream_of`, `upstream_of`, `impact_of_change` walk it. Results are entity pointers, not full content.
5. **Embedding search (post-v0.1)** — For fuzzy concepts ("customer churn metrics"). An embedding index over model descriptions, column comments, and pipeline docstrings; returns pointers + similarity scores.

The agent doesn't pick a layer. The agent picks a *skill*; skills are implemented using the appropriate layer. The orchestrator's classification step decides which skills to call.

### 6.2 The lineage graph

Lineage is the one Carve-owned piece of retrieval. Four node types, four edge types:

```
Nodes:
- dlt:source        — a dlt source in el/<name>/
- dlt:resource      — a resource inside that source
- warehouse:table   — a table in the destination
- dbt:source        — a dbt source in <project>/sources.yml
- dbt:model         — a dbt model

Edges:
- dlt:resource ──produces──▶ warehouse:table
- warehouse:table ──consumed_by──▶ dbt:source
- dbt:source ──consumed_by──▶ dbt:model
- dbt:model ──consumed_by──▶ dbt:model     (model-to-model deps)
```

Recomputed on `carve build`, on dbt manifest change, and on project sync in separate-repo mode. Stored in `lineage_nodes` / `lineage_edges` tables. Queries are bounded BFS walks with depth limit.

### 6.3 Caching and freshness

| Layer                 | Cache TTL              | Invalidation                                |
|-----------------------|------------------------|---------------------------------------------|
| Catalog queries       | 60 seconds             | Time-based                                  |
| dbt manifest          | until change           | File mtime watch                            |
| File grep             | per-invocation         | None — re-runs each agent invocation        |
| Lineage               | until change           | On build, manifest change, project sync     |
| Embedding (post-v0.1) | until index rebuild    | Manual `carve embeddings rebuild`           |

In-process cache in the FastAPI server (OSS); Redis-backed in the hosted product so multiple API replicas share.

### 6.4 Skill categories and bounded results

To prevent silent partial context, skills are categorized by retrieval shape, and each category has its own size policy. The agent layer never operates on partially-truncated data without the orchestrator (and ultimately the user) knowing about it.

**Structural / analytical** — queries that return data *about* a specific entity: `describe_table`, `model_dependencies`, `tests_on_model`, `column_lineage`, `pipeline_show`. Results are bounded by the entity's structure (no table has 50KB of column metadata). These skills **never truncate**. If a result would exceed `result_max_chars`, they raise `ResultTooLarge` with the actual size. The orchestrator handles this by refining its query to a narrower entity.

**Discovery** — queries that enumerate entities: `list_tables`, `list_models`, `list_pipelines`, `list_dlt_pipelines`. Results scale with project size, not entity structure. These skills are paginated: return one page (default 100 items) plus a continuation token. Agents that need more pages call the skill again with the token. The orchestrator walks pages as needed; specialist agents don't see continuation tokens — they get a fully-resolved list from pre-scoped context.

**Search** — queries that find matches by pattern: `grep_dbt_models`, `grep_dlt_code`, future embedding search. These are inherently top-N by relevance. Top-N is the feature, not truncation. Agents see the top results plus a count of matches not returned; calling with a more specific filter narrows the set.

**Plan-level surfacing.** The orchestrator owns refinement: if it cannot resolve context fully even after refining (e.g., the user's project has 50,000 tables and discovery queries can't narrow effectively), the resulting Plan is flagged `requires_user_review_for_partial_context: true`. The CLI / REST / MCP plan summary highlights this prominently. Build refuses to proceed until the user explicitly acknowledges (via `--accept-partial-context` or equivalent UI consent).

**Specialists never see truncated results.** The pre-scoped context handed to specialists (§5.4) is always fully resolved. If the orchestrator can't fully resolve, the plan flags it; specialists are never invoked against partial data. This isolates the partial-context risk to one place (the orchestrator's pre-scoping) and one user-facing acknowledgement, not a thousand silent specialist invocations.

Skills emit two events for observability:

- `skill.too_large` — fired when a structural query raised `ResultTooLarge`
- `skill.page_walked` — fired when a discovery query was paginated (N pages walked)

These let support and ops correlate "agent took many tries" with the underlying retrieval shape.

## 7. The lifecycle in detail

Plan / ask / build / run / deploy as code-level workflows. Complements PRD §6.3–§6.7.

### 7.1 Ask

**Purpose**: read-only investigative queries against the project. A sibling to `plan`; uses the same orchestration agent and skills but with a guardrail forbidding write skills and an output shape that is an answer rather than a plan.

**Inputs**: question string, optional `--pipeline <name>` for pipeline-scoped questions, optional `--target <name>` for target-scoped queries.

**Outputs**: `Ask` row in the state store with question, answer (markdown), cited entities (lineage node references), and skill-call trace. JSON file at `.carve/asks/<ask_id>.json` for durability. The CLI prints the answer plus a one-line citation summary; REST returns the structured response.

**Side effects**: reads project files; may call external read skills (catalog queries, dbt manifest, grep, lineage, MCP servers). **No writes anywhere** — no files modified, no destination warehouse touched, no state-machine transitions on pipelines, no jobs queued.

**Implementation**: same orchestration agent as `plan`, with a different system prompt and a guardrail block that forbids any code-write skill (`write_file`, `git_*`, `pipeline_create`, `agent_create`, etc.). The orchestrator gathers context as usual, then synthesizes a markdown answer + a list of cited entities instead of a Plan.

**Failure modes**: LLM error (caught, error message in Ask row); token budget exceeded (Ask marked `failed`); guardrail violation (Ask marked `failed`).

**Idempotency**: each `carve ask` creates a new Ask row (no dedup). Two asks with identical questions return separate results — answers may differ based on intervening project changes.

**Concurrency**: asks run in parallel with each other and with plans/builds/runs/deploys. No queue lock needed. Multi-tenant safe in hosted.

**Hosted-product additions**: asks recorded in the audit log; sharable via persistent URLs; service accounts can call `ask` via REST for in-tool integrations (Slack bot, ticketing-system query, etc.).

### 7.2 Plan

**Inputs**: goal string, optional pipeline (for refinements against an existing one), optional `parent_plan_id` (for `--refine`), project state read from disk + state store.

**Outputs**: `Plan` row in the state store, JSON file at `.carve/plans/<plan_id>.json`, skill-call trace, agent-invocation rows.

**Side effects**: reads project files only — no writes; may call external skills (catalog queries, dbt manifest, MCP servers); does not call dlt or dbt subprocesses.

**Failure modes**: LLM error (caught, message in plan row); token budget exceeded (plan marked `failed`, agent identified); guardrail violation in the orchestrator's skill call (plan marked `failed`, violation logged).

**Idempotency**: plan creation is not idempotent — each call produces a new plan id. `--refine` always creates a child; parent never modified. Plan files immutable once written.

### 7.3 Build

**Inputs**: plan id, current project state.

**Outputs**: `Build` row with `manifest_json` listing every file written; files on disk (`el/<name>/`, `pipelines/<name>.toml`, `.dlt/*.toml`, dbt models in v0.2); pipeline's `current_build_id` updated.

**Side effects**: reads + writes scoped to allowed paths from the plan; invokes specialist agents with pre-scoped context; records `agents_invocations` and `skill_calls`.

**Failure modes**:

- **Config hash drift**: build refused, exit code 4 — re-plan required.
- **File-write conflict** (file modified since plan): build refused, exit 4.
- **Specialist agent error**: build marked failed; partial files cleaned up; the build row records which step failed.
- **Guardrail violation**: agent's tool use blocked; build fails with violation details.

**Idempotency**: re-running against the same plan + unchanged config produces byte-identical output (modulo LLM nondeterminism in regenerated content). One successful Build per Plan; re-build rejected unless `--force`.

### 7.4 Run

**Inputs**: pipeline name, target (default = project default), optional `--resume <run_id>` for re-running failed steps.

**Outputs**: `Run` row with status/timing/cost/per-step status; `step_runs` rows; `logs` rows streamed during execution; updated lineage graph if any step emitted lineage events.

**Side effects**: reads current build's manifest; invokes step executors (`dlt`, `dbt`, `sql`) which call external systems (destination warehouse, dlt subprocess, dbt subprocess); **may modify the destination warehouse** — this is the actual data movement; emits structured logs, events, webhook payloads; updates the `jobs` table (queued → claimed → running → succeeded|failed).

**Failure modes**: worker crash mid-run (job reclaimed by reaper §4.5, restarts from scratch); step failure (failure mode applied per step); destination connection failure (retried with backoff). dlt's incremental state handles re-runs cleanly; sql steps must be idempotent by author convention.

**Idempotency**: manual re-runs create new Run rows (history preserved). Scheduled fires dedup via §4.1. Resume creates a new Run row with `parent_run_id`; only failed steps and dependents re-execute.

### 7.5 Deploy

**Inputs**: pipeline name, `--dry-run`, `--mode pr|direct` (`direct` is hosted-only).

**Outputs**: `Deploy` row with status (`opened`, `merged`, `failed`, `cancelled`), PR URL(s), file diff summary. Separate-repo mode (PRD §6.2): two linked Deploy rows, one per repo, via `linked_deploy_id`.

**Side effects**: git operations (branch, commits, push, PR open via GitHub MCP). **Does not modify production warehouse state** — that happens on the next scheduled run after the PR merges. **No DDL is applied** — dlt handles destination schema on first prod run.

**Failure modes**: git operation failure (deploy `failed`, no production state changed); config hash drift (refused, exit 4); merge conflict on feature branch (`failed`, user resolves); for `--mode direct` in hosted, audit-log-write failure rolls back the deploy.

**Idempotency**: re-running against the same build with an existing branch reuses it (amending if new changes); existing PR updates description. A deploy can be reopened after closing without merging.

### 7.6 Drift detection (the config hash)

Plans, Builds, and Deploys carry a `config_hash` computed at creation time, over: `carve.toml`, `carve/connections.toml`, `carve/runtime.toml`, `carve/agents/*.toml`, `carve/conventions.md`, and the agent + skill source files in `src/carve/` (plans know which agent version generated them).

| Action                | Hash check vs       | On drift           |
|-----------------------|---------------------|--------------------|
| `carve ask`           | none                | n/a (read-only)    |
| `carve plan`          | current config      | n/a (records hash) |
| `carve plan --refine` | parent plan's hash  | warn, re-record    |
| `carve build`         | plan's hash         | refuse (exit 4)    |
| `carve deploy`        | build's hash        | refuse (exit 4)    |
| `carve run`           | build's hash        | warn (info only)   |

Safety net: if you planned 3 days ago, edited a guardrail, and tried to build — Carve refuses and asks you to re-plan against the new config. Without this, a stale plan could produce code that violates current rules.

### 7.7 The dev/prod target boundary (technical)

Targets define environment-scoped connection details (Snowflake account, database, schema, warehouse, role; equivalent for other destinations). Each verb is target-aware:

| Verb           | Touches data? | Default target              | `--target` flag |
|----------------|---------------|-----------------------------|-----------------|
| `ask`          | reads only    | project default (dev)       | yes             |
| `plan`         | reads only    | project default (dev)       | yes             |
| `build`        | no            | n/a                         | no              |
| `run`          | writes        | project default (dev)       | yes             |
| `deploy`       | no            | n/a                         | no              |
| Scheduled run  | writes        | per-pipeline (typically prod) | configured in pipeline TOML |

The promoted artifact is the per-pipeline file set in `el/<name>/`, `pipelines/<name>.toml`, etc. Promotion is via git PR merge — once merged to main, the scheduler in prod picks up the pipeline. Configuration is not duplicated per-target; `connections.toml` lists all target definitions in one file.

## 8. Interfaces in detail

### 8.1 CLI

Typer-based command tree. Top-level groups: `init`, `plan`, `ask`, `build`, `run`, `deploy`, `pipelines`, `schedule`, `agents`, `skills`, `mcp-servers`, `runs`, `logs`, `metrics`, `serve`, `worker`, `docs`, `mcp-serve`.

- **Config discovery**: walks up from `cwd` looking for `carve.toml`. If absent, errors with a hint to run `carve init`.
- **Auth**: a token generated by `carve init` and stored in `.carve/token` (gitignored). Passed as `Authorization: Bearer <token>` to the FastAPI server. In server-less mode (`plan`, `build`), the token is unused.
- **Server discovery**: defaults to `http://127.0.0.1:8765`; override via `--server-url` or `CARVE_SERVER_URL`. Hosted CLI points at the tenant URL.
- **Output**: `--output table` (TTY default, rich-formatted), `--output json` (non-TTY default, newline-delimited), `--output yaml`.
- **Exit codes**: 0 success, 1 user error, 2 runtime error, 3 config error, 4 drift detected, 5 server unreachable. Stable across minor releases for CI integration.
- **Help**: auto-generated from Typer's introspection of Pydantic argument models.

### 8.2 REST API

FastAPI app in `src/carve/api/main.py`; sub-routers per command group under `src/carve/api/routers/`.

- **URL structure**: `/api/v1/<resource>/...`. v1 is a versioned contract; breaking changes go to v2, never v1. Hosted adds `/api/v1/hosted/...` for paid-only endpoints.
- **OpenAPI**: auto-generated; served at `/api/openapi.json`, Swagger UI at `/api/docs`. Integration tests verify schema matches endpoint signatures.
- **Auth**: middleware validates `Authorization: Bearer <token>` against hashed token in state store. Hosted tokens carry tenant + RBAC claims; OSS tokens are local-only.
- **Errors**: `application/problem+json` with structured fields (e.g., on drift: `expected_hash`, `actual_hash`, `plan_id`).
- **Pagination**: `?cursor=<opaque>&limit=<n>` on collection endpoints; responses include `next_cursor`. Default limit 50, max 200.
- **Idempotency**: write endpoints accept `Idempotency-Key: <uuid>` header. Same key + same body within 24h → original response; same key + different body → 409.
- **Streaming**: WebSocket on `/api/v1/runs/{id}/stream`; SSE on the same path with `Accept: text/event-stream`. JSON events: `step.started`, `log.line`, `step.completed`, `run.completed`.
- **Webhooks**: declared in `runtime.toml`. POST'd with `X-Carve-Signature: <hmac-sha256>` for replay protection. Retry with exponential backoff up to 6 attempts.

### 8.3 MCP server

Carve's MCP server is a thin adapter over its REST API. `src/carve/mcp/server.py` + `transports.py` (stdio + WebSocket).

- **Tool generation**: one MCP tool per REST endpoint (excluding streaming endpoints). Auto-generated from REST endpoint signatures — input schema = endpoint request body schema, description = endpoint docstring, implementation forwards to REST.
- **Naming**: `<resource>_<action>` (`plan_create`, `build_run`, `pipeline_show`). Tools taking a `pipeline` parameter accept it as a named arg.
- **Per-call flow**: translate MCP `tool_use` args to REST body → call local REST API (or hosted gateway) with user's token → translate REST response to MCP `tool_result` → return.
- **No business logic in MCP layer.** Updates to the REST API flow through to MCP via regeneration.
- **Auth**: same token as REST. For Claude Desktop / Cursor, configured in the MCP server config block in app settings.
- **Transports**: stdio (default, spawned as subprocess); WebSocket (`carve mcp-serve --transport ws --port 8766`).
- **Hosted alternative**: managed MCP endpoint at `wss://<tenant>.carve.dev/mcp` so agents don't need a local subprocess.

### 8.4 Local static HTML UI

Generated by `src/carve/ui/generator.py` from Jinja templates in `src/carve/ui/templates/`. Pages:

- `index.html` — recent runs, top-level metrics
- `runs.html` — full run history with filters
- `run/<id>.html` — single run detail (steps, logs, timings, cost)
- `pipelines.html` — pipeline list
- `pipeline/<name>.html` — single pipeline (config, schedule, recent runs)
- `agents.html` and `skills.html` — registry views

- **Triggers**: regenerated on `carve docs serve` startup, on every run completion, on every plan/build/deploy. Full template regeneration; no incremental updates.
- **Read path**: opens a read-only DB session, queries relevant tables, renders templates. No live connection from rendered HTML back to the server.
- **Serving**: `carve docs serve` runs a small static-file HTTP server on `127.0.0.1:8766`. Loopback-only by default; no auth.
- **Refresh model**: user reloads the page in the browser to see updates. No auto-refresh, no WebSocket. The cloud UI in hosted replaces this with a live React app.

## 9. State store schema

Tables grouped by domain. Postgres features used: partial unique indexes (§4.2), `FOR UPDATE SKIP LOCKED` (§4.3), JSONB for variable-shape data.

### 9.1 Project state

- `pipelines(name PK, current_build_id, default_target, created_at, updated_at)`
- `schedules(pipeline FK, cron, target, paused, last_fired_at, next_fires_at)`
- `tokens(id PK, name, hashed_token, scopes, created_by, created_at, last_used_at)`

### 9.2 Plans, builds, asks

- `plans(id PK UUID, parent_plan_id FK NULL, goal, config_hash, carve_version, status, summary, created_at, expires_at)`
- `builds(id PK UUID, plan_id FK, pipeline FK NULL, status, config_hash, manifest_json JSONB, created_at, completed_at)`
- `asks(id PK UUID, question, answer_md, cited_entities JSONB, status, target, pipeline FK NULL, created_at)`

### 9.3 Runtime: jobs, runs, steps, logs

Active + archive pattern from §4.2:

- `jobs(id PK UUID, pipeline FK, target, status, trigger, scheduled_for, claimed_by, claimed_at, heartbeat_at, started_at, finished_at, run_id FK NULL, created_at)`
  - Partial unique indexes on `(pipeline) WHERE status='queued'` and `(pipeline) WHERE status='running'`
  - Archive: `jobs_archive` (same schema)
- `runs(id PK UUID, job_id FK, pipeline FK, target, parent_run_id FK NULL, status, started_at, finished_at, duration_ms, tokens_input, tokens_output, cost_usd, error_message)`
  - Archive: `runs_archive` (partitioned by month in hosted)
- `step_runs(id PK UUID, run_id FK, step_id, type, status, depends_on JSONB, started_at, finished_at, outputs JSONB, error_message)`
- `logs(id PK BIGSERIAL, run_id FK, step_run_id FK NULL, timestamp, level, source, message)`
  - Archive: `logs_archive`
- `workers(id PK, host, pid, started_at, last_heartbeat_at, status)`

### 9.4 Deploys

- `deploys(id PK UUID, build_id FK, pipeline FK, status, mode, pr_url, linked_deploy_id FK NULL, opened_at, merged_at, file_diffs JSONB)`

`linked_deploy_id` joins paired deploys in separate-repo mode (Carve repo + dbt repo).

### 9.5 Agent telemetry

- `agents(name PK, model, system_prompt_path, allowed_skills JSONB, guardrails JSONB, specialization JSONB, source, created_at, updated_at)`
- `agent_invocations(id PK UUID, agent_name FK, run_id FK NULL, plan_id FK NULL, ask_id FK NULL, build_id FK NULL, tokens_input, tokens_output, cost_usd, duration_ms, status, started_at, finished_at)`
- `skill_calls(id PK UUID, agent_invocation_id FK, skill_name, input_hash, output_size, result_too_large BOOL, pages_walked INT NULL, duration_ms, started_at, finished_at)`

### 9.6 Lineage

- `lineage_nodes(id PK, kind, name, fqn, attributes JSONB)`
- `lineage_edges(from_id FK, to_id FK, edge_type, attributes JSONB, created_at)`

### 9.7 Events and webhooks

- `events(id PK BIGSERIAL, kind, payload JSONB, occurred_at, processed_at NULL)`
- `webhooks(id PK, url, event_filters JSONB, hmac_secret, active, created_at)`
- `webhook_deliveries(id PK, webhook_id FK, event_id FK, attempted_at, response_status, retry_count, status)`

### 9.8 MCP servers

- `mcp_servers(name PK, url, transport, auth_config JSONB, status, last_checked_at)`

Tools imported from registered servers are cached in memory, not in a separate table — refreshed on server reconnect.

### 9.9 Multi-tenancy readiness

Every tenant-scoped table carries a `tenant_id` column. In OSS single-user mode, `tenant_id` is always `1`. In hosted, `tenant_id` is set from the authenticated request context. Indexes include `tenant_id` as the leading column to keep query planner happy when multi-tenancy lands.

## 10. dlt and dbt integration

Mirrors PRD §6.2 from the technical side. Both backends are treated symmetrically.

### 10.1 Repo topology resolution

`carve.toml` records the topology choice for each backend:

```toml
[dbt]
mode = "same-repo"          # or "separate-local" + path; or "separate-remote" + url + branch

[dlt]
mode = "same-repo"          # same shape
```

Runtime resolution per invocation:

- **Same-repo**: project root + conventional location (`dbt_project.yml` in cwd or subdirectory; `el/` or detected dlt directory)
- **Separate-local**: the recorded filesystem path
- **Separate-remote**: `.carve/workspaces/<backend-name>/` (cached clone, synced before invocation)

The resolved path is what step executors invoke against.

### 10.2 dlt invocation

The `dlt` step type invokes `dlt pipeline run --pipeline <name>` via subprocess. The subprocess inherits the user's project venv and dlt config; Carve injects per-target credentials via env vars matching dlt's convention (`DESTINATION__SNOWFLAKE__CREDENTIALS__*`, etc.).

Generated dlt code lands at `el/<pipeline_name>/`:

- `__init__.py` — dlt source/resource definitions
- `requirements.txt` — pinned deps (typically `dlt[snowflake]` + SaaS API client)
- `.dlt/secrets.toml.template`, `.dlt/config.toml.template` — config templates (real `.dlt/` files are user-provided per environment)

For orchestration-only mode (PRD §6.2 mode 2), no generation occurs. The pipeline TOML references the user's existing dlt artifact by path; the step executor invokes against it.

Output extraction: parses `.dlt/pipelines/<name>/state.json` for rows loaded per resource, schema changes, errors. These become the step's structured outputs.

### 10.3 dbt invocation

The `dbt` step type invokes `dbt build` / `dbt run` / `dbt test` via subprocess. Common flags:

- `--target <target>` — picks the dbt profile target (resolved from Carve's target config)
- `--select <selector>` — passes through
- `--vars '<json>'` — passes through

Subprocess runs in the dbt project's directory (resolved per §10.1); dbt manages its own state under `<dbt_project>/target/`.

Output extraction: reads `<dbt_project>/target/run_results.json` for per-model status, timings, error messages. These become the step's structured outputs.

For orchestration-only mode, no model generation occurs. The dbt step executes against user-authored models.

### 10.4 Workspace cache (separate-remote mode)

```
.carve/workspaces/
├── <dbt-name>/         # cloned dbt repo
└── <dlt-name>/         # cloned dlt repo
```

Sync semantics:

- On `carve serve` startup: `git fetch` + `git checkout <branch>` for each cached repo
- Before each pipeline run: `git pull` (configurable; can be disabled for offline operation)
- Before `carve deploy`: `git pull` to ensure the deploy is against the latest

Sync conflicts (local modifications to a remote-cached workspace) are rejected with an error pointing the user at the workspace directory.

### 10.5 Convention inference details

Runs on `carve init` and on demand via `carve conventions refresh`.

**dbt**: reads `dbt_project.yml`, walks `models/` tree.

- Naming: detects prefix patterns (`stg_*`, `int_*`, `dim_*`, `fct_*`) by frequency analysis
- Layering: detects `staging/`, `marts/`, `intermediate/` directories
- Materializations: counts `+materialized:` directives by directory
- Tests: counts `tests:` blocks per model and common test names
- Sources: parses `sources.yml` files

**dlt**: scans `el/` (or configured dlt directory) for Python files using dlt decorators.

- Detects existing destinations from `.dlt/config.toml` and `.dlt/secrets.toml`
- Detects write-disposition patterns from resource decorators
- Detects source naming conventions
- Detects schema-contract usage (`dlt.mark.SchemaContract`)

Inferred patterns written to `carve/conventions.md` as a markdown document. Agents read this on every invocation as part of pre-scoped context.

### 10.6 Source coupling between dlt and dbt

When the EL agent generates a dlt pipeline whose output should feed an existing dbt source:

1. Orchestrator queries the dbt manifest for matching sources by name/schema
2. If match exists: EL specialist's pre-scoped context includes the source's table conventions; generated dlt code targets the same schema/table
3. If no match: EL specialist generates a stub `sources.yml` entry alongside the dlt pipeline

In separate-repo mode, the `sources.yml` addition becomes a linked deploy PR against the dbt repo (per §7.5). PR descriptions cross-link so reviewers see both halves of the change.

### 10.7 Version management

Carve supports a range of dlt and dbt versions:

- dlt: 1.0+ (tested against 1.0, 1.1, latest)
- dbt-core: 1.7+ (tested against 1.7, 1.8, 1.9, latest)

The runtime detects the installed version on startup and emits a warning if it's outside the tested range. Agents adapt their generated code based on the detected version (e.g., dbt 1.8 introduced new materializations Carve uses when available).

## 11. OSS-to-hosted seams

Each seam below is an abstraction in OSS that the hosted layer extends or replaces — no `if hosted:` sprinkled through the codebase.

### 11.1 Tenant scoping

All OSS code accepts a `tenant_id` (default `1` in OSS) on every state-store read/write. Every Postgres query that touches tenant-scoped tables includes `WHERE tenant_id = ?` (enforced by repository methods, not application code). Hosted sets the value from the auth middleware's bearer token claims.

### 11.2 Auth and identity

Auth is a middleware layer (`src/carve/api/auth.py`) returning an `Identity` (user_id, tenant_id, scopes). OSS validates a bearer token against the `tokens` table. Hosted validates SSO/OAuth tokens, JWT claims, service-account credentials, and applies RBAC. The rest of the app uses `Identity` regardless of source.

### 11.3 Event bus

OSS event bus is in-process (`src/carve/core/events/bus.py`) — pub/sub backed by an asyncio queue. Hosted swaps it for Redis Streams so events flow across API replicas and worker processes. The subscriber API doesn't change.

### 11.4 State store

SQLAlchemy 2.0 declarative. OSS uses a single Postgres database (bundled docker-compose). Hosted uses managed Postgres with read replicas for cloud UI's heavier queries. The repository layer is the only code that knows about pools or read-replica routing.

### 11.5 Audit log

OSS emits `api.request_received` and `api.response_sent` events. The hosted layer subscribes and writes them to a dedicated audit log table in a separate database. OSS doesn't ship the audit log table.

### 11.6 Rate limiting

OSS has none. Hosted adds middleware enforcing per-tenant and per-token quotas. Lives entirely in the hosted repo.

### 11.7 Cloud UI integration

Cloud UI talks to the same REST API as the OSS CLI, plus `/api/v1/hosted/...`. No separate API. External agents driving Carve via REST have identical capabilities — modulo hosted-only endpoints under `/hosted/`. The OSS static HTML UI does not evolve into the cloud UI; independent products with shared backend semantics.

### 11.8 Premium integrations

PagerDuty, Datadog, formatted-Slack — live in the hosted repo, subscribed to the OSS event bus. OSS users can roll their own via webhooks + custom formatters.

### 11.9 Secret storage

OSS stores secrets via env vars referenced in `connections.toml`. Hosted adds a Vault-backed store; users reference secrets by name, hosted control plane injects values at runtime. Resolver is abstracted (`src/carve/core/config/secrets.py`); OSS resolves from env, hosted from Vault.

## 12. Security boundaries

### 12.1 Secret handling

Sensitive values in any committed file must be `${VAR}` env-var references. The config loader refuses to start if it detects sensitive-looking fields (`*_password`, `*_secret`, `*_key`, `*_token`) hardcoded. Destination credentials are passed via env vars matching dlt/dbt conventions; never logged, never echoed, never in webhook payloads.

### 12.2 Subprocess isolation

Generated dlt code runs in a subprocess (`dlt pipeline run`) with a controlled env (only credentials it needs). No shared Python memory with Carve's process. dbt subprocess invocations follow the same pattern. A bug in generated code can't access Carve's internal state directly — only what env vars and target connections expose.

### 12.3 Generated code review

Plan/build surfaces what will be generated before generation. Deploy surfaces what's about to land in prod *as a PR* before merge. CI checks on the PR run `dlt pipeline check`, `dbt parse`, `dbt test --target dev`, lint. Semantic correctness is the human reviewer's job; baseline correctness is automated.

### 12.4 LLM provider credentials

Carve's agent layer accepts two credential types for Anthropic, picked in this precedence order:

1. **`ANTHROPIC_API_KEY` env var** — a developer-portal API key with pay-per-token billing. Used by most server installs, CI workflows, and shared deployments.
2. **OAuth token from a Claude subscription** — obtained via `carve auth login`, which opens a browser flow to Anthropic and returns an OAuth token bound to the user's Claude Pro / Team / Enterprise subscription. Stored locally at `.carve/anthropic_oauth.json` (gitignored, mode 0600). Token refresh is handled automatically by the SDK. This is the path for individual users and small teams who already pay for Claude and don't want a separate Anthropic-API billing relationship. (Inherited from the M1.1 `claude-code-oauth` work; same flow Claude Code itself uses.)

Whichever credential is in use, it stays scoped to the agent process — never logged, never echoed in CLI output, never in webhook payloads, never passed to subprocess executors, never persisted in the state store.

In the **hosted product**, OAuth-from-user-subscription is not offered (multi-tenant semantics break "whose subscription pays for this run?"). Hosted offers either BYO API key per tenant or a hosted-billing tier where Anthropic costs are bundled into the subscription price. The OAuth path remains an OSS-only feature.

### 12.5 File system writes

All Carve writes are scoped to the project directory and its subdirectories, plus `.carve/` and `.dlt/`. Writes outside these paths are rejected by the file-write guardrail at the skill layer. Agents cannot write to `/etc`, `~/.bashrc`, `/usr/local/`, etc.

### 12.6 Token storage

API tokens stored hashed (Argon2id) in the `tokens` table. Plaintext shown to user once at creation, never thereafter. Revocation deletes the row. OSS bootstraps one token at `carve init`, written to `.carve/token` (gitignored, mode 0600). Hosted manages tokens via SSO + service accounts; no plaintext storage.

### 12.7 Network boundaries

OSS binds to `127.0.0.1` by default. Binding to all interfaces requires explicit `--host 0.0.0.0` with a warning. Workers connect outbound to: Postgres, Anthropic API, dlt/dbt subprocesses (which connect to the destination), registered MCP servers. They do not initiate connections to user-provided URLs except via the HMAC-signed webhook subscriber list.

### 12.8 Webhook signing

Outgoing webhooks include `X-Carve-Signature: sha256=<hmac>` over the JSON body using a per-installation secret. Subscribers verify to prevent replay or forgery. Signing secret rotated via `carve webhooks rotate-secret`.

### 12.9 Generated SQL safety

The `sql` step type executes user-authored SQL files (not LLM-generated SQL at runtime). The EL agent generates dlt code using dlt's parameterized destination adapters — never ad-hoc SQL run against production. This keeps the LLM's output one removed from arbitrary SQL execution.

## 13. Performance characteristics

### 13.1 Operation budgets

Match PRD §7.1 with implementation context:

| Operation                | Budget                  | Where time goes                                    |
|--------------------------|-------------------------|----------------------------------------------------|
| `carve init` greenfield  | < 30s                   | Mostly Postgres bootstrap via docker-compose       |
| `carve init` brownfield  | < 5 min                 | Manifest parse + convention inference              |
| `carve plan` typical     | < 15s + LLM time        | 3–8 skill calls, orchestrator reasoning            |
| `carve build` typical    | < 60s + LLM time        | 1–4 specialist invocations, file writes            |
| `carve run` startup      | < 10s                   | Worker claim, target connection, subprocess spawn  |
| `carve deploy` typical   | < 60s                   | Git ops + PR open via GitHub MCP                   |
| REST read median         | < 200ms                 | Single-row or paginated queries                    |
| WebSocket log latency    | < 500ms                 | Log write → subscriber                             |
| Scheduler latency        | < 30s after cron tick   | Next scheduler loop                                |

### 13.2 State store sizing

Per typical OSS install (single team, ~20 pipelines, daily runs):

- `runs` + archive: ~5KB/run × 7,000/year = ~35MB
- `logs` + archive: ~100KB/run compressed × 7,000 = ~700MB
- `step_runs`: ~2KB × 3 steps × 7,000 = ~42MB
- `agent_invocations`: ~15MB/year

Total ~1GB after a year. Comfortably fits a small managed Postgres. Hosted partitions archive tables by month for query performance.

### 13.3 Concurrency limits

OSS default: 1 worker; max workers per `carve serve` process: 8 (past 8, optimistic-claim contention starts mattering — use separate `carve worker` processes). Hosted: per-tenant worker pools sized by usage tier; Postgres via PgBouncer in transaction-pool mode.

Concurrent agent invocations within a single run: 1 (orchestrator-then-specialist sequencing). Concurrent steps within a pipeline run: bounded by worker count + intra-pipeline parallelism slots.

### 13.4 LLM token costs

Typical (Claude Sonnet baseline):

- `carve plan` simple modification: 5K–15K input, 1K–3K output → ~$0.10–$0.30
- `carve plan` complex new pipeline: 20K–60K input, 5K–15K output → ~$0.40–$1.50
- `carve build` typical: 10K–30K input, 3K–10K output → ~$0.20–$0.75
- `carve ask` typical: 5K–20K input, 500–2K output → ~$0.05–$0.25

Per project: typically tens of dollars per month. Skill-call caching, result truncation, and pre-scoped context all reduce token use. Users on the OAuth-with-Claude-subscription path (§12.4) pay nothing per-run — costs are absorbed by their existing subscription, subject to its rate limits.

## 14. Failure modes and recovery

### 14.1 Transient failures

Examples: LLM rate limit, Snowflake connection timeout, GitHub API 503. Recovery: automatic retry with exponential backoff at the layer that knows it's transient (agent layer retries LLM calls with jitter; Snowflake connector retries; runtime's `retry` failure mode retries entire steps).

### 14.2 Data failures

Examples: column that was nullable suddenly NOT NULL; primary key conflict on MERGE; dlt schema contract violation. Recovery: **fail loudly, no retry.** Surface the actual error in the run's logs with full traceback. UI run-detail offers "rerun from this step." dlt's schema contracts give explicit control over fail-vs-evolve on schema changes.

### 14.3 Logic failures (bug in generated code)

Ideally caught by plan/deploy review; if not, by CI on the deploy PR (`dlt pipeline check`, `dbt parse`, `dbt test`). If it lands in prod, surface clearly and let the user `carve plan --refine` against the broken pipeline.

### 14.4 Partial pipeline failures

Per-step failure modes (§4.7) determine behavior. `carve run --resume <run_id>` re-runs failed steps and dependents.

### 14.5 Process crashes

Worker crash: heartbeat (§4.5) + reaper reclaims the job after 60s. Next worker runs the job from scratch.

API server crash: workers keep running; manual triggers blocked until the API is back; scheduled runs continue.

Postgres crash: workers retry connections with backoff; in-flight jobs remain `running`, their heartbeats stop, the reaper eventually reclaims.

### 14.6 dlt / dbt subprocess failures

Step executor captures stderr, marks the step failed, applies the step's failure mode. Per-step-type timeouts (default 4h for dlt, 1h for dbt, 5min for sql); exceeding the timeout kills the subprocess and marks `timed_out` (treated as `failed`).

## 15. What's deliberately not in this architecture

Decisions to prevent scope creep and keep the architecture honest:

- **No general-purpose orchestration features.** No asset-graph reactivity, no conditional branching, no fan-out beyond intra-pipeline step parallelism, no cross-pipeline triggers, no first-class backfills.
- **No connector framework.** dlt is the connector framework.
- **No transformation engine.** dbt is the transformation engine.
- **No message broker in OSS.** Event bus is in-process. Hosted uses Redis Streams.
- **No separate scheduler process.** Scheduler runs inside `carve serve`.
- **No K8s operator.** OSS users running Carve in Kubernetes use Helm charts or raw manifests. We don't ship a Carve-specific CRD + controller — that's a substantial separate product. Hosted runs Carve in K8s but with internal orchestration, not a publishable operator. Reconsider if there's real demand from K8s-heavy users.
- **No GraphQL.** REST + WebSocket/SSE + MCP cover the surface.
- **No notebook environment.** Pipelines are TOML, authored by agents.
- **No data quality monitoring product.** Carve generates dbt tests but isn't a separate quality SaaS.
- **No data catalog product.** Carve indexes schema for retrieval, not for analyst discoverability.
- **No BI tooling.** Carve builds the warehouse; doesn't visualize it.
- **No reverse-ETL.** Carve writes to the warehouse; doesn't sync to operational systems.
- **No custom step type SDK in v0.1.** Built-ins only. Likely post-v0.1.
- **No in-process custom skill SDK in v0.1.** Built-ins + MCP-imported skills only. Likely post-v0.1; the MCP path remains supported indefinitely.
