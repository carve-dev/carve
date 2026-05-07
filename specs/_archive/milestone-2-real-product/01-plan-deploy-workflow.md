# M2-01 — Plan enrichment + Build entity

**Milestone:** 2 — Real product
**Estimated effort:** 1–1.5 days
**Dependencies:** M1-02 (config), M1-03 (state store), M1.1-06 (pipeline-centric lifecycle)

## Update notes (proposal)

This spec was drafted before M1.1-06 landed and has been narrowed further during M2 spec review. M1.1-06 already shipped the `plan / build / run / deploy` verb split, the `plans` table with `phase` / `parent_plan_id` / `pipeline_name`, refinement (`--refine`), pipeline-targeted planning (`--pipeline`), and the `carve deploy` stub.

What this spec **adds** on top of M1.1-06:

1. Replace the free-form `submit_plan(design)` payload with a structured **`TaskGraph`** that supports multiple agents (extract-load, dbt, snowflake) and multiple steps per plan.
2. Promote **Build** to a first-class entity — a durable, deployable artifact with its own row, manifest, target, and (once deployed) `commit_sha` / `pr_url` / `deployed_at`. The Pipeline now points to its current Build, not its current Plan; Plans become biographical, reachable through `Build.plan_id`.
3. Reshape the build agent into a **coordinator** that dispatches each task to a specialist sub-agent (M2-03/04/05) via `invoke_specialist(agent_name, task)`. The coordinator no longer writes pipeline code itself.
4. **`carve deploy` is owned by M2-14 end-to-end** — registration, implementation, and the 5-phase flow. This spec doesn't touch deploy beyond ensuring the Build row carries the data deploy needs.
5. Add **`carve plan diff <p1> <p2>`** for sibling-plan comparison.

What this spec **explicitly defers** (was in the previous proposal, dropped during review):

- Cost / duration / Snowflake-credit estimates — out of M2 scope; revisit in M3+.
- Guardrail check column and validation — concept is underdefined.
- Plan-time `target` column — persist on the Build instead, where it actually matters at deploy time.
- `expires_at` enforcement at deploy time — column stays as inert data.
- `file_diffs_json` previews on Plan — task graph alone is enough for rendering.
- Config-hash validation at deploy time — `plans.config_hash` stays as inert data.
- `carve build "<goal>" --yes` shorthand — `plan → build` stays explicit.
- `carve plan list` / `carve plan show` — covered by `carve pipelines <name>` lineage.

## Scope

### In scope

- Replace the plan agent's free-form `design` payload with a typed `TaskGraph` (`Task`, `TaskGraph` Pydantic models) persisted into the existing `plans.task_graph_json` column. No schema change to `plans`.
- Add a **`builds`** table and migration `0004_build_entity.py`. Rename `pipelines.current_plan_id` → `pipelines.current_build_id` in the same migration.
- Reshape the build flow: `carve build <plan_id>` writes a new `Build` row on success and points the Pipeline at it. The build agent becomes a coordinator; specialist dispatch happens via `invoke_specialist`.
- Add `plan_diff(plan_a, plan_b) -> str` rendering helper for `carve plan diff`.
- (No work on `carve deploy`. M2-14 owns it end-to-end. This spec simply guarantees the Build row's contract that M2-14 reads.)
- `--target` semantics tightened so plan/build/run/deploy each treat the flag consistently (see CLI section).

### Out of scope

- The deploy orchestration itself (validation phases, branch creation, file commit, GitHub API calls, run-row recording). M2-14 owns all of it; M2-01 only calls into it.
- The orchestration agent's internals (how it produces a multi-task task graph). M2-02 owns that; this spec defines the schema the agent emits.
- A real DAG executor with parallelism. M3.
- Estimates, guardrails, config-drift detection, plan expiry — all listed under "explicitly deferred" above.

## Data model changes

### `plans` table

No schema changes. The existing columns are reused with widened semantics:

| Column | Status | Notes |
|---|---|---|
| `task_graph_json` | reused | Now stores the structured `TaskGraph` (see schema below), not the legacy `design` blob. |
| `estimates_json` | **dropped** | The cost/duration/credits feature is deferred to M3+; the column is removed in migration `0004` to reduce schema noise. |
| `config_hash` | reused | Already populated at plan time; M2 does *not* enforce a check. |
| `expires_at` | reused | Already defaulted to `+24h`; M2 does *not* enforce. |

### `builds` table (new)

The deployable artifact. A Build is what `carve deploy` ships.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | Primary key. `build_<hex>`. |
| `pipeline_name` | TEXT | Foreign key to `pipelines.name`. NOT NULL. |
| `plan_id` | TEXT | Foreign key to `plans.id`. NOT NULL. Biographical reference to the plan that produced this build. |
| `target` | TEXT | NOT NULL. The Snowflake target the build was designed/built against (inherited from plan time). |
| `created_at` | DATETIME | When the build was produced. |
| `manifest_json` | TEXT | JSON: list of DDL files + migration files this build expects. Computed at build time, consumed by deploy. |
| `commit_sha` | TEXT | Nullable. Set at deploy time — git sha at the moment of deploy. |
| `pr_url` | TEXT | Nullable. Set at deploy time. |
| `deployed_at` | DATETIME | Nullable. Set on successful deploy. |

### `pipelines` table change

- Rename `current_plan_id` → `current_build_id`. Foreign key now points at `builds.id` instead of `plans.id`.
- Plan reference is reachable via `pipeline.current_build → build.plan_id`. No data is lost; lineage just goes through the Build.

### Migration

`migrations/versions/0004_build_entity.py`:

1. Create `builds` table with the columns above.
2. For each existing pipeline with a non-null `current_plan_id`, synthesize a Build row. Target is backfilled from `default_target` (read from `carve.toml` at migration time). Manifest is `{"ddl_files": [], "migration_files": []}` for backfilled rows. `created_at` mirrors the plan's `created_at`.
3. Add `current_build_id` column to `pipelines`, populate from the synthesized Build rows, drop `current_plan_id`.
4. Drop the `plans.estimates_json` column. The cost/duration/credits feature is deferred to M3+ and the column is unused.

Backfill is best-effort: pipelines without a current plan get a NULL `current_build_id`.

### Task-graph schema

Define in `src/carve/core/state/plan_schema.py` (Pydantic, used by both the plan agent's `submit_plan` tool and the build coordinator's reader):

```python
class Task(BaseModel):
    step: int
    agent: str          # "extract_load" | "dbt" | "snowflake"
    action: str         # short identifier, e.g. "generate_extractor"
    inputs: dict[str, Any]

class TaskGraph(BaseModel):
    pipeline_name: str
    description: str
    is_new_pipeline: bool
    tasks: list[Task]
    skipped_agents: list[str] = []
    skipped_reason: dict[str, str] = {}
```

The plan agent's `submit_plan` tool emits this `TaskGraph`. The build coordinator consumes it.

## Plan generation flow (changed shape, same flow)

`carve plan "<goal>"` keeps the M1.1-06 flow:

1. Plan agent runs with `read_file`, `run_snowflake_query`, `submit_plan` tools.
2. Agent calls `submit_plan(task_graph)`; loop terminates.
3. Persist Plan with `phase="drafted"` and the new `TaskGraph` payload in `task_graph_json`.
4. Render summary via `render_plan_to_console(plan)`.

`carve plan --refine` and `carve plan --pipeline` keep their M1.1-06 semantics; they just emit the new task-graph shape now.

## Build flow — coordinator pattern (changed)

`carve build <plan_id>` keeps the M1.1-06 entry point and lifecycle (creates a build-run, marks plan `built`), with two new responsibilities: the agent is reshaped into a **coordinator** that dispatches tasks to specialist sub-agents, and on success the orchestrator writes a new **`Build` row** and points `Pipeline.current_build_id` at it.

The coordinator's tools:
- `invoke_specialist(agent_name, task) -> SpecialistResult` — dispatches to one of `{"extract_load", "dbt", "snowflake"}`. The specialist agent runs in its own loop with its own system prompt, scoped tools, and skill set; calls a `submit_step(file_list, summary)` terminator tool when done. Unknown agent names are rejected.
- `read_file`, `write_file` (scoped to `pipelines/<name>/`) — for stitching shared artifacts in M3 multi-step pipelines; rarely fires in M2.
- `submit_build(file_list, summary)` — the coordinator's terminator tool. After it fires, the orchestrator computes the manifest, writes the Build row (`pipeline_name`, `plan_id`, `target` inherited from plan time, `manifest_json`), and updates `Pipeline.current_build_id`.

Specialists live in their own specs: extract-load (M2-03), dbt (M2-04), snowflake (M2-05).

The coordinator's prompt (rewritten from M1.1-06's `m1_build_agent.md` → `build_coordinator.md`) is short: read the task graph, dispatch each task in order, call `submit_build`. Hard rules from M1.1-05 (no Python defaults for SNOWFLAKE_*, no "How to Run" sections, etc.) move to specialist prompts where the actual code is written.

For M2's typical single-task plans, the coordinator's job is light. The pattern is set up cleanly for M3 multi-step pipelines where dispatch fan-out matters.

## Targets across the lifecycle

| Command | `--target` flag? | Behaviour |
|---|---|---|
| `carve plan "<goal>" [--target X]` | **Yes** | Plan agent reads schemas/samples from target X; defaults to `default_target`. The chosen target travels with the plan in-process to the build, where it's persisted on the `Build` row. |
| `carve build <plan_id> [--target X]` | **Allowed** | Inherits from plan time. Rejects with a clear error if the user passes a `--target` that *differs* from what the plan was generated against. |
| `carve run <pipeline> [--target Y]` | **Yes** | Already implemented in M1.1-06. Defaults to the current Build's `target` if `--target` not given (was: `default_target`). |
| `carve deploy <pipeline> [--target Y]` | **Yes** | Owned by **M2-14** end-to-end (registration + implementation). Defaults to the current Build's `target` if `--target` not given. |

The Build row is the durable carrier of the target choice. Plans don't need their own column for it.

## CLI surface

New / modified commands in this spec:

- `carve plan "<goal>" [--target X]` — already shipped in M1.1-06; this spec only changes the persisted shape (`TaskGraph` instead of free-form `design`). CLI flag set is unchanged.
- `carve plan --refine <plan_id> "<feedback>"` — already shipped in M1.1-06; refinement chain via `parent_plan_id`. Unchanged.
- `carve plan --pipeline <name> "<change>" [--target X]` — already shipped in M1.1-06. Used to *modify an existing pipeline*: the plan agent loads the pipeline's existing files as context and produces a delta plan, rather than starting fresh. M2 reuses the flag without changing it.
- `carve plan diff <plan_id_a> <plan_id_b>` — **new**. Renders a textual diff of two plans (task graph side-by-side).
- `carve build <plan_id> [--target X]` — externally similar; this spec adds a `Build` row to the side effect (in addition to writing files). Rejects a `--target` that conflicts with the plan's recorded target.
- `carve deploy <pipeline_name> [--target Y]` — owned by **M2-14**, not this spec. Listed here only because it's part of the lifecycle.

Deferred / not added: `carve plan list`, `carve plan show`, `carve build "<goal>" --yes` shorthand. See "explicitly deferred" above.

## Implementation

File-level changes:

New:

- `src/carve/core/state/plan_schema.py` — Pydantic `TaskGraph`, `Task`.
- `src/carve/core/state/exceptions.py` — `NotBuiltError`, `PipelineNotFoundError`, `UnknownSpecialistError` (or extend existing exceptions module if present).
- `src/carve/cli/orchestrator/diffing.py` — `render_plan_diff(plan_a, plan_b)`.
- `src/carve/core/agents/prompts/build_coordinator.md` — replaces `m1_build_agent.md`.
- `migrations/versions/0004_build_entity.py` — adds `builds` table; renames `pipelines.current_plan_id` → `pipelines.current_build_id`.
- `tests/cli/orchestrator/test_diffing.py`
- `tests/cli/orchestrator/test_builder_coordinator.py`
- `tests/core/state/test_plan_schema.py`
- `tests/core/state/test_builds.py`

Modified:

- `src/carve/core/state/models.py` — add `Build` model; replace `Pipeline.current_plan_id` with `current_build_id`.
- `src/carve/core/state/repository.py` — `create_build`, `get_build`, `set_pipeline_current_build`. (Deploy-time writes — `commit_sha`, `pr_url`, `deployed_at` — are owned by M2-14's `update_build`.)
- `src/carve/core/agents/m1_tools.py` — update `make_submit_plan_tool`'s `input_schema` to the new `TaskGraph` shape; add `make_invoke_specialist_tool(registry)` and `make_submit_build_tool()`.
- `src/carve/core/agents/prompts/m1_plan_agent.md` — instruct the agent to emit a multi-task graph; cross-reference M2-02.
- `src/carve/cli/orchestrator/builder.py` — read the new task-graph shape; implement the coordinator dispatch loop; write the new `Build` row on success; update `Pipeline.current_build_id`.
- `src/carve/cli/orchestrator/listing.py` — `render_plan_summary` shows the task graph; pipeline detail walks lineage through `current_build → plan`.
- `src/carve/cli/commands/plan.py` — wire `diff` subcommand.
- `src/carve/cli/commands/build.py` — accept `--target`; reject conflict with plan target.
- `src/carve/cli/main.py` — register `carve plan diff`.
- Delete `src/carve/core/agents/prompts/m1_build_agent.md`.

## Tests

- `submit_plan` with the new schema persists a Plan whose round-trip parse matches.
- Plan rendering shows the task graph.
- `carve plan diff <a> <b>` renders a sane textual diff for refinement (parent / child) and pipeline-rebuild cases.
- Coordinator dispatches a single `extract_load` task via `invoke_specialist` (specialist mocked); emits `submit_build`; a `Build` row is created.
- Coordinator with two tasks (`extract_load` + `dbt`) dispatches in order; second task runs only after first completes.
- Coordinator rejects an unknown `agent` value in a task with a clear error.
- On successful build, `Pipeline.current_build_id` points at the new Build; the Build's `plan_id` references the plan that produced it.
- `carve deploy <pipeline_name>` happy path: routes to the orchestrator with the loaded `Build` and resolved target (mock the orchestrator).
- `carve deploy <pipeline_name>` raises `PipelineNotFoundError` for an unknown pipeline.
- `carve deploy <pipeline_name>` raises `NotBuiltError` when `current_build_id` is NULL.
- `carve build` rejects a `--target` that differs from the plan's recorded target.
- Migration `0004` adds `builds`, renames `current_plan_id` → `current_build_id`, backfills synthesized Build rows for existing pipelines.

## Acceptance criteria

- `carve plan "<goal>"` produces a Plan whose `task_graph_json` parses into a `TaskGraph` with `tasks`. No files written under `pipelines/`.
- `carve plan --refine` and `carve plan --pipeline` continue to work and emit the new shape.
- `carve build <plan_id>` consumes the new `TaskGraph`, dispatches via the coordinator, writes the same on-disk pipeline files M1.1-06 already produces, **and** writes a new `Build` row pointed at by `Pipeline.current_build_id`.
- `carve plan diff <p1> <p2>` renders a readable comparison of two plans.
- `carve deploy <pipeline_name>` is no longer a stub: it loads the Pipeline + current Build, resolves the target, and delegates to M2-14's deploy orchestrator. (The orchestrator itself is M2-14.)
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover the new commands and helpers.
- README walkthrough updated to mention `plan diff` and the existence of `Build` as a first-class concept. CHANGELOG entry under `## [Unreleased]`.

## Files this spec produces

New:

- `src/carve/core/state/plan_schema.py`
- `src/carve/core/state/exceptions.py`
- `src/carve/cli/orchestrator/diffing.py`
- `src/carve/core/agents/prompts/build_coordinator.md`
- `migrations/versions/0004_build_entity.py`
- `tests/cli/orchestrator/test_diffing.py`
- `tests/cli/orchestrator/test_builder_coordinator.py`
- `tests/core/state/test_plan_schema.py`
- `tests/core/state/test_builds.py`

Modified:

- `src/carve/core/state/models.py` (add `Build`, swap `current_plan_id` → `current_build_id`)
- `src/carve/core/state/repository.py` (Build helpers; pipeline-current-build helpers)
- `src/carve/core/agents/m1_tools.py` (`submit_plan` input schema; `invoke_specialist`; `submit_build`)
- `src/carve/core/agents/prompts/m1_plan_agent.md` (multi-task graph)
- `src/carve/cli/orchestrator/builder.py` (coordinator loop; Build row write)
- `src/carve/cli/orchestrator/listing.py` (richer plan rendering; lineage through Build)
- `src/carve/cli/commands/plan.py` (`diff` subcommand)
- `src/carve/cli/commands/build.py` (`--target` handling)
- `src/carve/cli/main.py`
- `README.md`
- `CHANGELOG.md`

Deleted:

- `src/carve/core/agents/prompts/m1_build_agent.md` (replaced by `build_coordinator.md`)

## What this enables

- M2-02 (orchestration agent) has a structured `TaskGraph` contract to emit against.
- M2-03/04/05 (specialist sub-agents) plug into the coordinator via `invoke_specialist`.
- M2-14 (deploy orchestrator) has a clean Build to ship — manifest, target, and plan lineage all on one row.
- M3 scheduling slots in cleanly: schedules attach to the Pipeline; the scheduler runs `current_build`.
