# v0.1-08 — Multi-step pipeline composition: TOML schema, step DAG, dlt/dbt/sql step types

> Plugs the three v0.1 step types (`dlt`, `dbt`, `sql`) into the runtime framework from spec 07; defines the pipeline TOML schema; ships the runtime specialist agent that authors `pipelines/<name>.toml` entries. Per [PRD §6.10 pipeline composition](../PRD.md), [ARCHITECTURE §4.6 step executors](../ARCHITECTURE.md), [ARCHITECTURE §4.7 failure modes](../ARCHITECTURE.md), [ARCHITECTURE §10.2/10.3 dlt and dbt invocation](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 8](../PROJECT_PLAN.md).

## Status

- **Status:** Drafting
- **Depends on:** [v0.1-03 flat-layout](./03-flat-layout.md), [v0.1-04 el-agent-dlt](./04-el-agent-dlt.md), [v0.1-07 runtime](./07-runtime.md)
- **Blocks:** [v0.1-09 rest-api](./09-rest-api.md) (REST surface for pipelines), [v0.1-11 static-html-ui](./11-static-html-ui.md) (UI renders pipeline definitions and step status)
- **Soft depends on:** [v0.1-06 project-memory](./06-project-memory.md) (the runtime specialist agent reads memory files via the spec-06 loader)

## Goal

Bring the runtime to life with real pipelines:

1. **The `pipelines/<name>.toml` schema** — pipeline metadata, `[schedule]` block, ordered `[[steps]]` tables that form a DAG
2. **The step DAG executor** — topological walk with intra-pipeline parallelism, per-step failure mode enforcement, Jinja templating for cross-step outputs
3. **The three concrete step type implementations** — `dlt`, `dbt`, `sql` — each implementing the `StepExecutor` protocol from spec 07
4. **Failure mode framework** — `fail`, `warn`, `continue`, `retry` (with attempts + backoff), `skip_downstream`
5. **The runtime specialist agent** — authors and modifies `pipelines/<name>.toml` files when the orchestrator routes pipeline-composition tasks to it
6. **CLI commands** for pipeline management (`carve pipelines list`, `show`, `validate`, `diff`)

After this spec lands, a user can describe a multi-step pipeline ("ingest Stripe, then run the stg_stripe dbt models, then refresh the search index via SQL"), `carve plan` produces a multi-step composition, `carve build` materializes the dlt code + pipeline TOML, and the runtime schedules and executes it end-to-end.

## Out of scope

- Step types beyond `dlt`, `dbt`, `sql` (`shell`, `http`, `python`, `agent`, `approval` are deferred per [PRD §4.2](../PRD.md))
- Conditional branching, fan-out, or asset-graph features (out per [ARCHITECTURE §5.6 narrow runtime](../ARCHITECTURE.md))
- First-class backfills (out per same; manual `carve run --target prod --param ...` is the workaround)
- The REST/MCP surface for pipelines (lives in spec 09)
- The static UI's pipeline-detail view (lives in spec 11)

## Files this spec produces

```
src/carve/runtime/step_types/__init__.py
src/carve/runtime/step_types/dlt.py                      # NEW — dlt step executor
src/carve/runtime/step_types/dbt.py                      # NEW — dbt step executor
src/carve/runtime/step_types/sql.py                      # NEW — sql step executor
src/carve/runtime/pipeline_dag.py                        # NEW — topological walk + parallelism slots
src/carve/runtime/jinja_context.py                       # NEW — cross-step output rendering
src/carve/runtime/failure_modes.py                       # NEW — fail | warn | continue | retry | skip_downstream
src/carve/runtime/execute_pipeline.py                    # NEW — main entry point called by worker.py from spec 07

src/carve/core/agents/runtime_specialist.py              # NEW — runtime agent class
src/carve/core/agents/prompts/runtime_specialist.md      # NEW — system prompt
carve/agents/runtime.toml                                # NEW — built-in agent definition

src/carve/core/config/pipeline_schema.py                 # NEW — Pydantic models for pipelines/<name>.toml
src/carve/core/skills/pipeline_inspect.py                # NEW — read pipeline TOMLs (for the runtime specialist)

src/carve/cli/pipelines.py                               # NEW — `carve pipelines` Typer command group

migrations/versions/0009_step_runs_outputs.py            # NEW — extends step_runs with outputs JSONB column if not already there

tests/unit/test_pipeline_schema.py                       # NEW
tests/unit/test_pipeline_dag_topological.py              # NEW
tests/unit/test_pipeline_dag_parallelism.py              # NEW
tests/unit/test_failure_modes_each.py                    # NEW — one test per failure mode
tests/unit/test_jinja_context_resolution.py              # NEW
tests/unit/test_step_executor_dlt.py                     # NEW — mock dlt subprocess; verify cmd + output parsing
tests/unit/test_step_executor_dbt.py                     # NEW — same shape for dbt
tests/unit/test_step_executor_sql.py                     # NEW — exec SQL against fixture Postgres
tests/integration/test_pipeline_3_step_end_to_end.py     # NEW — dlt → dbt → sql against fixture infrastructure
tests/integration/test_runtime_agent_authoring.py        # NEW — agent produces a coherent pipelines/<name>.toml from a goal

docs/pipelines.md                                        # NEW — user reference for the TOML schema
docs/step-types.md                                       # NEW — reference for each step type's config
docs/failure-modes.md                                    # NEW — when to use each failure mode
```

## Behavior

### Pipeline TOML schema

`pipelines/<name>.toml`:

```toml
# Pipeline metadata
[pipeline]
description = "Stripe charges ingest + staging transforms + search refresh"
owner = "data-team"

# Scheduling (spec 07)
[schedule]
cron = "0 2 * * *"               # 2am daily
target = "prod"                   # which target this pipeline runs against in scheduled mode
paused = false

# Steps (DAG)
[[steps]]
id = "ingest_stripe"
type = "dlt"
artifact = "stripe_charges"       # resolves to el/stripe_charges/
depends_on = []
[steps.failure_mode]
mode = "retry"
max_attempts = 3
backoff = "exponential"

[[steps]]
id = "stage_stripe"
type = "dbt"
command = "build"
select = "stg_stripe_charges+"    # dbt selector syntax
depends_on = ["ingest_stripe"]
[steps.failure_mode]
mode = "fail"                     # default; included here for clarity

[[steps]]
id = "refresh_search"
type = "sql"
file = "sql/refresh_charges_search.sql"
connection = "prod"
depends_on = ["stage_stripe"]
[steps.failure_mode]
mode = "warn"                     # bad search refresh shouldn't fail the whole run

# Cross-step Jinja example
[[steps]]
id = "notify_count"
type = "sql"
file = "sql/notify_loaded_count.sql"
connection = "prod"
depends_on = ["ingest_stripe"]
[steps.jinja_vars]
loaded_rows = "{{ steps.ingest_stripe.outputs.rows_loaded }}"
```

Pydantic schema (in `src/carve/core/config/pipeline_schema.py`):

```python
class PipelineMeta(BaseModel):
    description: str = ""
    owner: str = ""

class ScheduleBlock(BaseModel):
    cron: str                      # validated via croniter on load
    target: str = "prod"
    paused: bool = False

class FailureMode(BaseModel):
    mode: Literal["fail", "warn", "continue", "retry", "skip_downstream"] = "fail"
    max_attempts: int = 1          # only relevant when mode == "retry"
    backoff: Literal["exponential", "linear", "fixed"] = "exponential"
    initial_delay_s: float = 5.0
    max_delay_s: float = 300.0

class PipelineStep(BaseModel):
    id: str                        # unique within pipeline
    type: Literal["dlt", "dbt", "sql"]
    depends_on: list[str] = []
    failure_mode: FailureMode = Field(default_factory=FailureMode)
    jinja_vars: dict[str, str] = {}    # rendered against the cross-step Jinja context
    # Type-specific config goes in subclasses; see below

class DltStepConfig(BaseModel):
    type: Literal["dlt"] = "dlt"
    artifact: str                  # name of el/<artifact>/ directory
    write_disposition: Optional[Literal["append", "replace", "merge"]] = None  # override config.toml
    resource_select: Optional[list[str]] = None    # subset of resources to run

class DbtStepConfig(BaseModel):
    type: Literal["dbt"] = "dbt"
    command: Literal["build", "run", "test", "snapshot", "seed"] = "build"
    select: Optional[str] = None
    exclude: Optional[str] = None
    vars: dict[str, Any] = {}
    full_refresh: bool = False

class SqlStepConfig(BaseModel):
    type: Literal["sql"] = "sql"
    file: str                      # path relative to project root
    connection: str                # target name from carve/connections.toml

class Pipeline(BaseModel):
    name: str                      # derived from filename, not in TOML
    pipeline: PipelineMeta = Field(default_factory=PipelineMeta)
    schedule: Optional[ScheduleBlock] = None
    steps: list[PipelineStep] = []
```

Loading validates: unique step ids, valid `depends_on` refs (all referenced ids exist), no cycles, valid cron (if schedule present), valid type-specific configs.

### Step DAG execution

`src/carve/runtime/pipeline_dag.py`:

```python
class PipelineDAG:
    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        self._validate_no_cycles()
        self._topological_order = self._compute_topological_order()

    def ready_steps(self, completed: set[str], failed: set[str], skipped: set[str]) -> list[PipelineStep]:
        """Steps whose dependencies are all completed (success or fall-through per failure mode)
        AND that aren't themselves completed/failed/skipped."""
        ...

    def downstream_of(self, step_id: str) -> set[str]:
        """All transitively-dependent step ids."""
        ...
```

`src/carve/runtime/execute_pipeline.py` is the function the worker (spec 07) calls:

```python
async def execute_pipeline(run: Run, *, paths: ProjectPaths, registry: StepExecutorRegistry) -> RunResult:
    pipeline = load_pipeline(paths.pipelines_dir / f"{run.pipeline}.toml")
    dag = PipelineDAG(pipeline)

    completed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    outputs: dict[str, dict] = {}   # step_id → outputs dict

    while True:
        ready = dag.ready_steps(completed, failed, skipped)
        if not ready and not still_running(): break

        # Launch ready steps in parallel up to the worker's intra-pipeline slot count
        tasks = [run_step(step, run, paths, registry, outputs) for step in ready[:available_slots]]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        for step, result in zip(ready, results):
            apply_failure_mode(step, result, dag, completed, failed, skipped)
            if result.status == "succeeded":
                outputs[step.id] = result.outputs

    return RunResult.from_step_results(completed, failed, skipped, outputs)
```

`run_step` renders Jinja vars against the running `outputs` dict, dispatches to the appropriate executor, captures logs, writes a `step_runs` row, and emits `step.started`/`step.completed`/`step.failed` events (spec 07).

### Failure modes

`src/carve/runtime/failure_modes.py` translates each mode into the runtime's behavior:

| Mode             | On step failure                                                  | On step succeed |
|------------------|------------------------------------------------------------------|------------------|
| `fail` (default) | Mark pipeline run failed; don't start any unstarted steps        | Add to completed |
| `warn`           | Record warning + the error; continue scheduling downstream       | Add to completed |
| `continue`       | Record failure; continue scheduling downstream                   | Add to completed |
| `retry`          | Retry up to `max_attempts` with backoff; if all retries fail, treat as `fail` | Add to completed |
| `skip_downstream`| Mark all transitively-dependent steps as `skipped`; continue siblings (steps that don't depend on this) | Add to completed |

The pipeline-level run status is:

- `succeeded` — all non-skipped steps succeeded
- `failed` — at least one step failed under `fail` mode OR exhausted `retry`
- `partial` — completed but with `warn` or `continue` failures, or with `skip_downstream` skips

### Jinja context

`src/carve/runtime/jinja_context.py` exposes a sandboxed Jinja environment with the following namespace:

```python
{
  "steps": {
    "<step_id>": {
      "outputs": {...},            # the outputs dict from the step's StepResult
      "status": "succeeded",       # or other
      "started_at": "2026-...",
      "finished_at": "2026-...",
    },
    ...
  },
  "run": {
    "id": "<uuid>",
    "pipeline": "<name>",
    "target": "<name>",
    "trigger": "scheduled",
    "started_at": "2026-...",
  },
  "env": {
    # selected env vars; never secrets (per spec 12.4-style scoping)
    "DATABASE_URL": "...",
  },
}
```

Jinja is sandboxed via the `SandboxedEnvironment` from `jinja2.sandbox` — no filesystem access, no arbitrary code execution. Only the namespace above is reachable.

Templating happens at step launch time (after deps complete, before executor runs); the rendered values are passed to the executor as part of the step config.

### Step executor: `dlt`

`src/carve/runtime/step_types/dlt.py`:

```python
class DltStepExecutor:
    step_type = "dlt"

    async def execute(self, *, step, run, paths) -> StepResult:
        artifact_dir = paths.el_dir / step.config.artifact
        if not artifact_dir.exists():
            return StepResult(status="failed", error_message=f"dlt artifact not found: {step.config.artifact}", ...)

        env = self._build_env(run.target, step.config)
        cmd = self._build_command(artifact_dir, step.config)
        result = await self._run_subprocess(cmd, env, cwd=paths.root, timeout_s=14400)  # 4hr default

        outputs = self._extract_outputs(artifact_dir, result)
        return StepResult(
            status="succeeded" if result.returncode == 0 else "failed",
            outputs=outputs,
            log_lines=result.log_lines,
            error_message=result.stderr_tail if result.returncode != 0 else None,
            duration_ms=result.duration_ms,
        )
```

- `_build_env` injects `DESTINATION__SNOWFLAKE__CREDENTIALS__*` and similar dlt-convention env vars from the resolved target config (per ARCHITECTURE §10.2)
- `_build_command` is `dlt pipeline run --pipeline <name>` plus optional `--resources` for `resource_select`
- `_extract_outputs` parses `.dlt/pipelines/<name>/state.json` for rows_loaded per resource, schema changes, errors; returns a structured dict

### Step executor: `dbt`

`src/carve/runtime/step_types/dbt.py`:

- Resolves the dbt project path via `ProjectPaths.dbt_project_path` (spec 03's resolver)
- Invokes `dbt {command} --target {target} --select {select} --vars '{vars_json}'`
- For `command="build"`, parses `<dbt_project>/target/run_results.json` for per-model status, timings, error messages
- For `command="test"`, surfaces test failures as the step's `error_message` (one entry per failing test)
- Default timeout: 3600s (1 hour); configurable per step

### Step executor: `sql`

`src/carve/runtime/step_types/sql.py`:

- Opens a connection to the configured target via `carve/connections.toml`
- Reads the SQL file (path relative to project root), renders through Jinja (with the cross-step namespace)
- Executes the SQL as a single transaction (one connection, one execute call; for multi-statement files, the user can use the destination's batch mechanism — Snowflake's `EXECUTE IMMEDIATE`, Postgres's `\;` separator, etc.)
- Captures returned rows as outputs (first 100 rows, with truncation flag if more)
- Default timeout: 300s (5 min); configurable per step

The `sql` step type is deliberately limited to single-file, single-target execution — no multi-file SQL "pipelines" within a step. That's what step composition is for.

### Runtime specialist agent

`carve/agents/runtime.toml`:

```toml
name = "runtime"
model = "claude-{LATEST_SONNET}"
system_prompt_path = "src/carve/core/agents/prompts/runtime_specialist.md"
max_tokens = 16384
max_iterations = 30

allowed_skills = [
  "read_file",
  "write_file",                 # scoped to pipelines/**
  "list_files",
  "pipeline_inspect",           # spec-08 skill: read existing pipelines/<name>.toml
  "list_el_artifacts",          # which el/<name>/ dirs exist? (filename listing only, no contents)
  "list_dbt_models",            # via the dbt manifest (HISTORICAL — uses M2-era manifest reader)
  "destination_schema_query",   # to verify target schemas exist
  "mcp:*",
]

[guardrails]
allowed_write_paths = ["pipelines/**"]
forbidden_write_paths = ["/", "~/", "/etc/", "/usr/", "/var/", "/opt/", "el/**", "carve/**"]
max_skill_calls_per_invocation = 30

[specialization]
classifications = [
  "compose_pipeline",                  # new pipelines/<name>.toml
  "modify_pipeline_steps",             # change step order, add/remove steps, update failure modes
  "change_schedule",                   # change [schedule] cron or pause
  "schedule_existing_artifact",        # orchestration-only mode (PRD §6.2 mode 2): compose a TOML against existing user-authored dlt/dbt
]
```

System prompt highlights:

1. **Role** — author/modify `pipelines/<name>.toml` files. The dlt code and dbt models exist (or will exist via other specialists); the runtime specialist's job is to compose them.
2. **Inputs** — pre-scoped context includes: goal, target dlt/dbt artifacts (their paths and outputs), conventions + standards from memory (spec 06), existing pipeline TOMLs for reference
3. **Output** — a structured Task result that emits the new/modified TOML file
4. **Schedule semantics** — when to suggest cron vs leave unscheduled; how to pick reasonable defaults
5. **Step ordering** — dlt before dbt; transforms before notifications/exports; SQL post-steps last
6. **Failure mode picking** — `retry` for transient-prone (ingest); `fail` for hard transforms; `warn` for nice-to-have post-steps
7. **Cross-step outputs** — when to use Jinja templating to pass values

### Runtime specialist's role in orchestration-only mode

Per [PRD §6.2](../PRD.md) mode 2, users with existing dlt/dbt code want Carve to orchestrate without authoring. The runtime specialist handles this:

- Orchestrator detects (via the el-agent dispatch logic from spec 04) that the user's goal touches a user-authored artifact
- Routes directly to the runtime specialist with the goal classified as `schedule_existing_artifact`
- Runtime specialist's pre-scoped context includes: the user's existing artifact path + a structured summary of what it does (extracted by the `existing_dlt_inspect` / `existing_dbt_inspect` skills)
- The specialist writes a `pipelines/<name>.toml` that references the existing artifact by path
- No EL agent invocation; no dlt code generation

This is what makes mode 2 work end-to-end: the user keeps their dlt code, gets Carve's runtime scheduling, observability, and composition.

### CLI: `carve pipelines`

```
carve pipelines list                      # all pipelines with last-run summary
carve pipelines list --status running     # filter
carve pipelines show <name>               # full config + recent run history
carve pipelines validate <name>           # schema-check + DAG check (cycles, missing depends_on refs)
carve pipelines validate                  # validate all pipelines
carve pipelines diff <name> --against <build_id>
                                          # diff current pipelines/<name>.toml against an older build's manifest_json
```

Authoring of pipeline TOMLs is via `carve plan` / `carve build` (per PRD §6.10 and design decision 5.3). The CLI doesn't expose direct edit commands for pipelines beyond the standard `$EDITOR` flow.

REST/MCP coverage of this CLI surface lands in spec 09; this spec ships only the CLI implementation.

## Tests

- **Unit (schema):** valid TOML loads cleanly; invalid TOMLs (missing required fields, unknown step types, duplicate step ids, missing depends_on refs, cycles, bad cron) raise structured errors
- **Unit (DAG):** topological order is correct for representative DAGs (linear, fan-out, fan-in, diamond); ready_steps correctly accounts for completed/failed/skipped sets
- **Unit (failure modes each):** one test per mode, exercising the transition rules from the table above
- **Unit (Jinja sandbox):** template renders against the standard namespace; attempts to access filesystem or import os raise sandbox errors
- **Unit (dlt executor):** mock subprocess; verifies command construction, env-var injection, state.json output parsing
- **Unit (dbt executor):** mock subprocess; verifies run_results.json parsing, per-model status extraction
- **Unit (sql executor):** real connection to a fixture Postgres; verifies single-transaction semantics + output capture
- **Integration (3-step pipeline):** a synthetic `pipelines/stripe.toml` with dlt → dbt → sql; fixture Stripe-like mock API; runs end-to-end; rows land in fixture warehouse; step_runs table has the right shape
- **Integration (parallel steps):** a pipeline with two independent dlt steps and one dbt that depends on both; both dlt steps run concurrently; dbt waits for both
- **Integration (failure modes in practice):** a pipeline where step 2 fails under `warn`; pipeline run completes as `partial`; step 3 runs; the warning surfaces in logs
- **Integration (skip_downstream):** step 2 fails under `skip_downstream`; step 3 (depends on 2) is marked skipped; step 4 (sibling) runs
- **Integration (retry):** step that fails twice then succeeds under `mode=retry max_attempts=3`; pipeline succeeds; step_runs table shows three attempts with the third succeeding
- **Integration (runtime agent):** `carve plan "schedule the stripe ingest to run nightly at 2am and then build the staging models"` produces a coherent `pipelines/stripe.toml` referencing the existing dlt artifact + a dbt step
- **Integration (orchestration-only mode):** existing user-authored dlt at `el/legacy_salesforce/` (no provenance header); `carve plan "schedule legacy_salesforce daily"` produces a TOML referencing the existing artifact without invoking the EL agent

## Acceptance

- A 3-step pipeline (`dlt` → `dbt` → `sql`) executes end-to-end in correct topological order against fixture infrastructure
- Parallel steps execute concurrently when the DAG permits
- Each of the five failure modes behaves per the table above
- Cross-step output references resolve via the sandboxed Jinja context
- Cycle detection rejects invalid DAGs at `carve pipelines validate` time, before the runtime ever sees them
- The runtime specialist agent authors a working `pipelines/<name>.toml` from a natural-language goal
- Orchestration-only mode (mode 2) end-to-end: a user with existing user-authored dlt code can compose a scheduled pipeline via `carve plan` without the EL agent running
- All three step executors invoke the correct subprocess command with the right env vars and parse their outputs into structured step `outputs`
- `carve pipelines validate` catches schema errors, cycles, and missing references with clear messages
- The full v0.1 plan→build→run→deploy→schedule loop works end-to-end against real Snowflake (this is the v0.1.0 acceptance bar)

## Design notes

- **Why a fixed three step types (`dlt`, `dbt`, `sql`) instead of pluggable types from day one?** Per [PRD §4.2 out of scope](../PRD.md) and design decision [5.9 steps as unit of execution](../ARCHITECTURE.md). A custom-step-type SDK requires hardening the abstraction against arbitrary executors, and the abstraction matures fastest when stressed by concrete implementations. Three real consumers from day one keep the abstraction honest; the custom-step SDK lands post-v0.1.
- **Why Jinja for cross-step values instead of native Python expressions?** Because step authors (users via standards.md, and the agent) work in TOML, not Python. Jinja is the universal templating language for TOML/YAML config files (Ansible, dbt itself). Sandboxed Jinja keeps the surface limited to the namespace we expose.
- **Why does the `sql` step type only support single-file single-target execution?** Because anything richer pushes back toward "carve has its own SQL engine," which we explicitly aren't building. Users who need multi-statement SQL with conditional logic should put that in a dbt model. The `sql` step is for thin operational glue (refresh a materialized view, post a row count to an analytics table).
- **Why `skip_downstream` instead of more elaborate conditional logic?** Per [ARCHITECTURE §15](../ARCHITECTURE.md), we don't ship general conditional branching. `skip_downstream` is the one form of conditionality we allow because it falls out naturally from the failure-mode framework — "if this step failed, the next steps don't apply" is a common, easily-explained pattern.
- **Why the runtime specialist agent rather than letting the orchestrator write pipeline TOMLs directly?** Because the orchestration agent's job is classification + impact context + dispatch. Adding "also write pipeline TOMLs" to its remit would bloat its prompt and reduce its specialism. A focused runtime specialist with a small skill set (read pipelines, write to pipelines/, list artifacts) is easier to reason about and easier to test.
- **Why allow the runtime specialist to read but never modify dlt/dbt artifacts?** Same separation of concerns. The EL agent (spec 04) owns dlt code; the dbt specialist (v0.2) will own dbt models. Runtime specialist composes them. If a goal requires both new dlt code AND new pipeline composition, the orchestrator routes to both specialists and merges their Task results into one Plan.

## Open questions

- **Per-step timeout defaults (4h dlt, 1h dbt, 5min sql).** *Implementation default.* Inherited from ARCHITECTURE §14.6. Configurable per step via the TOML (`timeout_s = ...`). These defaults are conservative for real-world cases; can lower in `runtime.toml` if a user wants tighter SLAs.
- **Intra-pipeline parallelism slot count.** *Implementation default.* Default 4 slots per worker. Tunable in `runtime.toml`. The cap matters when a pipeline has many independent dlt resources fanned out at one level.
- **How `partial` pipeline-run status surfaces in retries/scheduling.** *Implementation default.* A `partial` run is *not* automatically retried by the scheduler — it's treated as completed. Users who want auto-retry on partial use `mode=fail` on the warning-emitting step instead. Documented in `docs/failure-modes.md`.
- **Whether `step.outputs` is size-capped.** *Implementation default.* 64KB per step's outputs JSONB column. If a step produces more (huge row counts as outputs, etc.), it's truncated with a flag — agents reading the outputs see partial data, which is acceptable for downstream Jinja but visible. Users authoring sql steps with large output dicts should structure them down.
- **Behavior when the runtime specialist agent is asked to compose a pipeline involving an artifact that doesn't exist.** *Implementation default.* The specialist returns `status="needs_user_input"` in its Task result with a message: "The artifact `<name>` doesn't exist. Either author it first (e.g., `carve plan 'ingest X'`) or reference an existing artifact." The orchestrator surfaces this in the plan summary; the user decides.
