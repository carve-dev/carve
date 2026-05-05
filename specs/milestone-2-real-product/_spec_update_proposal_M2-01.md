# M2-01 — Plan enrichment + deploy (prod deploy)

**Milestone:** 2 — Real product
**Estimated effort:** 1–1.5 days
**Dependencies:** M1-02 (config), M1-03 (state store), M1.1-06 (pipeline-centric lifecycle)

## Update notes (proposal)

This spec was originally drafted before M1.1-06 landed. M1.1-06 already shipped the lifecycle this spec was sketching:

- **Plans are first-class** persisted rows in the `plans` table (with `phase`, `pipeline_name`, `parent_plan_id`, `expires_at`, `config_hash`, `task_graph_json`, etc.).
- **`carve plan` is design-only** (no files), via a dedicated plan agent that calls `submit_plan(design)`.
- **`carve plan --refine <plan_id> "<feedback>"`** already produces a child plan with `parent_plan_id` set.
- **`carve plan --pipeline <name> "<change>"`** already proposes a delta against an existing pipeline.
- **`carve build <plan_id>`** is the *code generation* step — runs the build agent, writes `pipelines/<name>/`, marks plan `phase="built"`, upserts the `Pipeline` row.
- **`carve run <pipeline_name>`** is dev execution. Re-runnable. No replay guard.
- **`carve deploy <pipeline_name>`** is reserved for prod deploy and currently prints a stub.

What this spec **does** add on top of M1.1-06:

1. Replace today's free-form `submit_plan(design)` payload with a richer **task graph** structure that supports multiple agents (extract-load, dbt, snowflake) and multiple steps per plan.
2. Add **cost / duration / Snowflake-credit estimates** and a **guardrail check** field.
3. Add **file-diff previews** computed at plan time (what `carve build` would write, in summary form).
4. Reshape the **build agent into a coordinator** that dispatches each task graph entry to its assigned specialist sub-agent (extract-load / dbt / snowflake) via a new `invoke_specialist(agent_name, task)` tool, then verifies + stitches results. The coordinator no longer writes code itself.
5. Wire **config-hash validation** and **plan expiry** into `carve deploy <pipeline_name>` — the prod deploy gate where drift actually matters.
6. Make `carve deploy <pipeline_name>` *real*: open a prod-deploy PR (mechanics live in M2-14). It supersedes the M1.1-06 stub.
7. Add **`carve plan diff <p1> <p2>`** for parent/child plan comparison.

What this spec does **not** redo:

- The `carve plan / build / run / deploy` verb split — adopt M1.1-06 verbatim.
- Persisting plans in a JSON-file store under `.carve/plans/`. Plans live in the SQLite repository; we extend the existing schema.
- Refinement (`--refine`) and pipeline-targeted planning (`--pipeline <name>`) — already shipped.
- Removing the replay guard from `carve run` — already done; superseded M1.1-07.

## Scope

### In scope

- Replace the plan agent's free-form `design` payload with a richer JSON shape that includes a typed `task_graph`, `estimates`, `guardrail_check`, and `file_diffs` previews.
- Persist the new shape into the existing `plans.task_graph_json` and `plans.estimates_json` columns (no schema change to those columns; they're already TEXT JSON).
- Add `plans.guardrail_check`, `plans.file_diffs_json`, and `plans.target` columns via a new Alembic migration `0004_plan_enrichment.py`.
- Add `plan_diff(plan_a, plan_b) -> str` rendering helper for `carve plan diff`.
- Promote `carve deploy <pipeline_name>` from stub to a real prod-deploy verb that:
  - Looks up the pipeline's `current_plan_id`.
  - Re-validates `expires_at`, `config_hash`, and `guardrail_check` against the current config and clock.
  - Hands off to `open_pr_for_pipeline(pipeline_name, plan)` (implemented in M2-14).
  - Records a `deploy` run row.
- Add `carve plan diff <p1> <p2>` command.
- Update `carve plan` rendering to show task graph, estimates, and file-diff previews.

### Out of scope

- The PR mechanics (branch creation, file commit, GitHub API calls). M2-14 owns that; this spec only calls into it via `open_pr_for_pipeline`.
- The orchestration agent's internals (how it produces a multi-agent task graph). M2-02 owns that; this spec defines the schema the agent emits.
- A real DAG executor with parallelism. M3.
- `carve plan list` / `carve plan show` as net-new commands — `carve pipelines <name>` already shows lineage. We add `carve plan diff` because comparing siblings isn't covered by the lineage view.
- `carve build "<goal>"` as a "polite shorthand" for plan + deploy. The lifecycle is `plan → build → run → deploy`; collapsing them re-introduces the original conflation M1.1-06 untangled.

## Data model changes

### `plans` table

Existing columns stay. Three new columns plus widened semantics on existing ones.

| Column | Status | Notes |
|---|---|---|
| `task_graph_json` | reused | Now stores the structured task graph (see schema below), not the legacy `design` blob. The plan agent emits the new shape; build agent reads it. |
| `estimates_json` | reused | Now populated with `PlanEstimates` (cost / duration / credits / tokens). |
| `guardrail_check` | **new** | TEXT, default `'passed'`. CHECK constraint: `IN ('passed', 'failed')`. |
| `file_diffs_json` | **new** | TEXT JSON, nullable. Preview of what the build agent will write (paths + create/modify/delete + truncated diff). |
| `target` | **new** | TEXT NOT NULL. The Snowflake target the plan was generated against (`dev`, `prod`, etc., from `carve/connections.toml`). Captured at plan time so refining or deploying later doesn't silently switch environments. Default backfill: the value of `default_target` from `carve.toml` at migration time. |
| `config_hash` | reused | Already populated at plan time; M2 adds the deploy-time mismatch check. |
| `expires_at` | reused | Already defaulted to `+24h` in `models.py`; M2 adds the deploy-time check. |

Migration: `migrations/versions/0004_plan_enrichment.py` (note: `0003_rename_apply_to_deploy.py` already exists) adds the three columns with a sensible backfill (`guardrail_check='passed'`, `file_diffs_json=NULL`, `target=<default_target>`).

### Task-graph schema

Define in `src/carve/core/state/plan_schema.py` (Pydantic, used by both the plan agent's `submit_plan` tool and the build agent's reader):

```python
class FileDiff(BaseModel):
    path: str           # repo-relative path
    kind: str           # "create" | "modify" | "delete"
    preview: str | None = None   # truncated unified diff

class Task(BaseModel):
    step: int
    agent: str          # "extract_load" | "dbt" | "snowflake"
    action: str         # short identifier, e.g. "generate_extractor"
    inputs: dict[str, Any]
    expected_outputs: list[FileDiff] = []

class PlanEstimates(BaseModel):
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    duration_seconds: int = 0
    snowflake_credits: float = 0.0

class TaskGraph(BaseModel):
    pipeline_name: str          # the pipeline this plan targets/creates
    description: str
    is_new_pipeline: bool
    tasks: list[Task]
    skipped_agents: list[str] = []
    skipped_reason: dict[str, str] = {}
```

The plan agent's `submit_plan` tool's `input_schema` is this `TaskGraph` (M2-02 generates it). The build agent receives it as preamble.

## Plan generation flow (changed shape, same flow)

`carve plan "<goal>"` keeps the M1.1-06 flow:

1. Plan agent runs with `read_file`, `run_snowflake_query`, `submit_plan` tools.
2. Agent calls `submit_plan(task_graph)`; loop terminates.
3. Orchestrator computes `PlanEstimates` from token usage + a credits estimator (M2-05 owns the Snowflake-credit math).
4. Orchestrator computes `file_diffs` previews — for each task with `expected_outputs`, run a quick read-and-diff pass against the current pipeline directory (if `--pipeline` was used) or mark all as `create`.
5. Orchestrator runs `validate_guardrails(task_graph, config) -> "passed" | "failed"`.
6. Persist Plan with `phase="drafted"`, the new fields populated.
7. Render summary via `render_plan_to_console(plan)`.

`carve plan --refine` and `carve plan --pipeline` keep their M1.1-06 semantics; they just emit the new task-graph shape now.

## Build flow — coordinator pattern (changed)

`carve build <plan_id>` keeps the M1.1-06 entry point and lifecycle (creates a build-run, marks plan `built`, upserts `Pipeline` row), but the agent it invokes shifts shape: M1.1-06's `m1_build_agent.md` becomes a **coordinator** that dispatches each task in the plan's task graph to its assigned specialist sub-agent. The coordinator does not write code itself.

```python
# Pseudocode for the coordinator's loop
def build_pipeline(plan: Plan, build_run_id: str):
    pipeline_name = plan.task_graph.pipeline_name
    written_files: list[FileDiff] = []
    for task in plan.task_graph.tasks:
        result = invoke_specialist(task.agent, task)
        # specialist runs to completion, calls submit_step(file_list, summary)
        verify_outputs(task.expected_outputs, result.file_list)
        written_files.extend(result.file_list)
    upsert_pipeline_row(pipeline_name, plan.id)
    mark_plan_built(plan.id, pipeline_name, build_run_id)
```

The coordinator's tools:
- `invoke_specialist(agent_name, task) -> SpecialistResult` — dispatches to one of `{"extract_load", "dbt", "snowflake"}`. The specialist agent runs in its own loop with its own system prompt, scoped tools, and skill set; calls a `submit_step(file_list, summary)` terminator tool when done.
- `read_file`, `write_file` (scoped to `pipelines/<name>/`) — for stitching shared artifacts (e.g. a top-level `pipelines/<name>/main.py` that imports per-step modules in M3 multi-step pipelines; in M2 the typical pipeline is single-step and this rarely fires).
- `submit_build(file_list, summary)` — the coordinator's terminator tool; signals the build run is complete.

Each specialist is its own spec:
- **Extract-load** — M2-03 (`03-extract-load-agent.md`).
- **dbt** — M2-04.
- **Snowflake** — M2-05.

The coordinator's prompt (rewritten from M1.1-06's `m1_build_agent.md` → `build_coordinator.md`) is short: read the task graph, dispatch each task in order, verify outputs, call `submit_build`. The hard rules from M1.1-05 (no Python defaults for SNOWFLAKE_*, no "How to Run" sections, etc.) move to the *specialist* prompts where the actual code is written.

For M2's typical single-task plans (one `extract_load` task; or one `extract_load` + one `dbt` task), the coordinator's job is light — one or two dispatches, one verify. The pattern's set up cleanly for M3 multi-step pipelines where the dispatch fan-out matters.

This change touches only the build agent prompt + the coordinator implementation in `builder.py`. The plan agent (M1.1-06's `m1_plan_agent.md`) is unaffected; it continues to emit a task graph (now with the new shape) and the coordinator consumes it.

## Deploy with hash validation (the real `carve deploy`)

`carve deploy <pipeline_name>` replaces the M1.1-06 stub:

```python
def deploy_command(pipeline_name: str):
    config = load_config()
    repo = get_repository(config)

    # 1. Resolve pipeline -> current plan
    pipeline = repo.get_pipeline(pipeline_name)
    if pipeline is None:
        raise PipelineNotFoundError(pipeline_name)
    if pipeline.current_plan_id is None:
        raise NotBuiltError(pipeline_name)

    plan = repo.get_plan(pipeline.current_plan_id)

    # 2. Expiry
    if datetime.utcnow() > plan.expires_at:
        raise PlanExpiredError(plan.id)

    # 3. Config hash
    if plan.config_hash != config.config_hash:
        raise ConfigDriftError(
            "Config has changed since this pipeline was built. "
            "Run `carve plan --pipeline <name> '<rebuild reason>'` then "
            "`carve build` to regenerate."
        )

    # 4. Guardrails
    if plan.guardrail_check != "passed":
        raise GuardrailViolationError(plan.id)

    # 5. Record deploy run + open PR (M2-14)
    run_id = repo.create_run(
        kind="deploy", target_id=plan.id, pipeline_name=pipeline_name
    )
    try:
        pr_url = open_pr_for_pipeline(pipeline_name, plan, config)
        repo.update_run_status(run_id, "success")
    except Exception as e:
        repo.update_run_status(run_id, "failed", error=str(e))
        raise

    return run_id, pr_url
```

Notable differences from the original spec:

- Keyed by `<pipeline_name>`, not `<plan_id>`. The plan id is recovered from `Pipeline.current_plan_id`. This matches M1.1-06.
- No `TaskGraphExecutor` runs here. Code is *already* on disk (built by `carve build`); deploy's job is to ship it through review, not re-execute it. The "execute the task graph" responsibility from the original M2-01 is already covered by the existing `carve build` (code gen) + `carve run` (execute) split.
- The PR-opening details live in M2-14; this spec only calls the boundary function.

## Plan rendering

Update `src/carve/cli/orchestrator/listing.py` to render the task graph and estimates:

```
Plan: plan_a3f291  (drafted, expires in 23h)
Pipeline: stg_orders   (modification)
Goal: make stg_orders incremental with order_id as the unique key

Task graph (3 steps):
  1. dbt           modify_model           stg_orders.sql
  2. dbt           verify_downstream      4 models touched
  3. quality       add_incremental_test   stg_orders

File previews:
  M  dbt/models/staging/stg_orders.sql       (+18 / -4)
  +  dbt/tests/staging/stg_orders_unique.yml

Estimated cost:    $0.18      Estimated duration: 45s
Snowflake credits: 0.02       Guardrails: passed

Build with:  carve build plan_a3f291
Refine with: carve plan --refine plan_a3f291 "<feedback>"
```

## CLI surface

New / modified commands in this spec:

- `carve plan "<goal>" [--target <name>]` — unchanged externally; richer rendering. `--target` is preserved from M1.1-06 and is what the plan agent reads to know which Snowflake schema to inspect; defaults to `default_target` from `carve.toml`. The chosen target is recorded on the Plan (so refining a plan later doesn't silently switch environments).
- `carve plan --refine <plan_id> "<feedback>"` — unchanged externally. Inherits the parent plan's target; the user re-plans with a fresh `carve plan` if they need to switch targets.
- `carve plan --pipeline <name> "<change>" [--target <name>]` — unchanged externally.
- `carve plan diff <plan_id_a> <plan_id_b>` — **new**. Renders a side-by-side textual diff of two plans (task graph, estimates, file-diff previews). Diffs across targets surface the target on each side.
- `carve deploy <pipeline_name>` — **promoted from stub** to the real prod-deploy entry point. Calls into M2-14 for PR mechanics. **No `--target` flag.** Deploy ships code via PR; the target the merged code runs against is determined later by whatever scheduler invokes `carve run --target <name>` against the merged pipeline.

### `--target` semantics across the lifecycle

For clarity (cross-referenced by [ARCHITECTURE.md §7.1](../ARCHITECTURE.md)):

| Command | `--target` flag? | Why |
|---|---|---|
| `carve plan` | **Yes** | Plan agent reads schemas/samples from a specific target; the chosen target is persisted on the Plan row. |
| `carve build <plan_id>` | **No** | Build writes pipeline files; the code is target-agnostic and resolves connections at run time. |
| `carve run <pipeline>` | **Yes** | Already implemented in M1.1-06; defaults to `default_target`. The manual prod-execution path is `carve run --target prod`. |
| `carve deploy <pipeline>` | **No** | Deploy is a PR operation against the user's repo, not a data operation. |

This spec is responsible for *enforcing* the table above — concretely:

- `carve build` must reject `--target` with a clear error: "build does not take a target; targets are chosen at run time via `carve run --target <name>`."
- `carve deploy` must reject `--target` similarly: "deploy ships code via PR; the target is chosen by whoever runs the merged pipeline."
- The Plan row must persist the target chosen at plan time (new `target: str` column on `plans`, defaulting to `default_target` for backfill). The deploy-time guardrail check uses this persisted target — *not* the current `default_target` — so a plan generated against `dev` doesn't quietly deploy expecting prod conventions.

Deferred / not added:

- `carve plan list`, `carve plan show <plan_id>` — covered well enough by `carve pipelines <name>` (lineage). Add only if usability testing in M2 demands it.
- `carve build "<goal>" --yes` shorthand — out of scope; `carve build` is code-gen, not a meta-pipeline.
- Cross-target plan diffing as a first-class feature — `carve plan diff` works mechanically, but a "what's different about deploying this to prod" view is M3 territory.

## Implementation

File-level changes (additions and modifications, no new top-level `core/plan/` module):

New:

- `src/carve/core/state/plan_schema.py` — Pydantic `TaskGraph`, `Task`, `FileDiff`, `PlanEstimates`.
- `src/carve/core/state/exceptions.py` — `PlanExpiredError`, `ConfigDriftError`, `GuardrailViolationError`, `NotBuiltError`, `PipelineNotFoundError` (or extend existing exceptions module if present).
- `src/carve/cli/orchestrator/deployer.py` — the real `deploy_pipeline(pipeline_name, ...)` flow described above. (Note: the M1 file `applier.py` was renamed to `runner.py` in M1.1-06.)
- `src/carve/cli/orchestrator/diffing.py` — `compute_file_diffs(task_graph, project_dir)` and `render_plan_diff(plan_a, plan_b)`.
- `src/carve/cli/orchestrator/guardrails.py` — `validate_guardrails(task_graph, config)`.
- `migrations/versions/0004_plan_enrichment.py` — adds `guardrail_check`, `file_diffs_json`, and `target` columns.
- `tests/cli/orchestrator/test_deployer.py` — coverage for the new deploy flow.
- `tests/cli/orchestrator/test_diffing.py`
- `tests/cli/orchestrator/test_guardrails.py`
- `tests/core/state/test_plan_schema.py`

Modified:

- `src/carve/core/state/models.py` — add `guardrail_check`, `file_diffs_json`, `target` columns to `Plan`.
- `src/carve/core/state/repository.py` — `get_pipeline_current_plan(name)`, `mark_plan_deployed(plan_id, run_id, pr_url)` updates `deployed_at` + `deploy_run_id`.
- `src/carve/core/agents/m1_tools.py` (or its M2 successor) — update `make_submit_plan_tool`'s `input_schema` to the new `TaskGraph` shape.
- `src/carve/core/agents/prompts/m1_plan_agent.md` — instruct the agent to emit a multi-task graph; cross-reference M2-02 for orchestration agent details.
- `src/carve/cli/orchestrator/planner.py` — populate `estimates`, `file_diffs`, `guardrail_check` after `submit_plan` fires; persist into the new columns.
- `src/carve/cli/orchestrator/builder.py` — read the new task-graph shape from `plan.task_graph_json`; implement the coordinator dispatch loop (`invoke_specialist` → verify → next task); manage shared state across dispatches.
- `src/carve/core/agents/prompts/m1_build_agent.md` — rewrite as `build_coordinator.md`: short prompt focused on dispatch, verify, and `submit_build` rather than direct code authoring. Hard rules from M1.1-05 (no SNOWFLAKE_* defaults, no "How to Run") move to specialist prompts.
- `src/carve/core/agents/m1_tools.py` — add `make_invoke_specialist_tool(registry)` and `make_submit_build_tool()`.
- `src/carve/cli/orchestrator/listing.py` — `render_plan_summary` shows task graph + estimates + file-diff previews.
- `src/carve/cli/commands/deploy.py` — replace the placeholder with the real flow.
- `src/carve/cli/commands/plan.py` — wire `diff` subcommand; refresh rendering.
- `src/carve/cli/main.py` — register `carve plan diff`.

## Tests

- `submit_plan` with the new schema persists a Plan whose round-trip parse matches.
- Plan rendering shows task graph + estimates + file-diff previews.
- `carve plan diff <a> <b>` renders a sane textual diff for: same parent / child refinement / pipeline rebuild cases.
- Guardrail violation at plan time sets `guardrail_check='failed'` and is surfaced in rendering.
- Coordinator dispatches a single `extract_load` task via `invoke_specialist` (specialist mocked); verifies output file list; emits `submit_build` with the combined file list.
- Coordinator with two tasks (`extract_load` + `dbt`) dispatches in order; second task runs only after first completes.
- Coordinator rejects an unknown `agent` value in a task with a clear error.
- `carve deploy <pipeline_name>` happy path: opens PR (mocked), records deploy run, marks plan deployed.
- `carve deploy` raises `PlanExpiredError` when `expires_at` is in the past.
- `carve deploy` raises `ConfigDriftError` when current `config_hash` differs from the plan's.
- `carve deploy` raises `GuardrailViolationError` when `guardrail_check='failed'`.
- `carve deploy` raises `NotBuiltError` when the pipeline has no `current_plan_id`.
- `carve deploy` raises `PipelineNotFoundError` for an unknown pipeline.
- Migration `0003` adds columns and backfills defaults; existing plans remain queryable.

## Acceptance criteria

- `carve plan "<goal>"` produces a Plan whose `task_graph_json` parses into a `TaskGraph` with `tasks`, `estimates`, and (when applicable) `file_diffs` previews. No files written under `pipelines/`.
- `carve plan --refine` and `carve plan --pipeline` continue to work and emit the new shape.
- `carve build <plan_id>` consumes the new `TaskGraph` shape and produces the same on-disk result M1.1-06 already produces (no externally visible change to build).
- `carve deploy <pipeline_name>` is no longer a stub: it validates expiry, config hash, and guardrails, then hands off to M2-14's PR opener and records a `deploy` run.
- `carve plan diff <p1> <p2>` renders a readable comparison of two plans.
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover the new commands and helpers.
- README walkthrough updated to mention `deploy` is now real and `plan diff` is available. CHANGELOG entry under `## [Unreleased]`.

## Files this spec produces

New:

- `src/carve/core/state/plan_schema.py`
- `src/carve/core/state/exceptions.py`
- `src/carve/cli/orchestrator/deployer.py`
- `src/carve/cli/orchestrator/diffing.py`
- `src/carve/cli/orchestrator/guardrails.py`
- `migrations/versions/0004_plan_enrichment.py`
- `tests/cli/orchestrator/test_deployer.py`
- `tests/cli/orchestrator/test_diffing.py`
- `tests/cli/orchestrator/test_guardrails.py`
- `tests/core/state/test_plan_schema.py`

Modified:

- `src/carve/core/state/models.py` (add `guardrail_check`, `file_diffs_json`, `target`)
- `src/carve/core/state/repository.py` (deploy-flow helpers)
- `src/carve/core/agents/m1_tools.py` (`submit_plan` input schema)
- `src/carve/core/agents/prompts/m1_plan_agent.md` (multi-task graph)
- `src/carve/cli/orchestrator/planner.py` (estimates, diffs, guardrails wiring)
- `src/carve/cli/orchestrator/builder.py` (read new task-graph shape)
- `src/carve/cli/orchestrator/listing.py` (richer rendering)
- `src/carve/cli/commands/deploy.py` (real impl)
- `src/carve/cli/commands/plan.py` (`diff` subcommand)
- `src/carve/cli/main.py`
- `README.md`
- `CHANGELOG.md`

## What this enables

- The orchestration agent (M2-02) has a structured contract to emit against.
- The web UI (M2-12/12) can render plans with task graphs and file previews.
- Refinement gives users iterative control before paying for code generation.
- `carve deploy` becomes the real prod-deploy gate — drift detection lives where it matters (prod ship), not where it doesn't (dev re-runs).
- M2-14 plugs cleanly into the deploy flow via `open_pr_for_pipeline`.
