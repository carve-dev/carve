# M1.1-06 — Pipeline-centric lifecycle: plan / build / run

**Milestone:** 1.1 — M1 follow-ups
**Estimated effort:** 1.5–2 days
**Dependencies:** M1-04 (agent loop), M1-05 (Python step + runner), M1 integration (orchestrator), M1.1-04 (live progress output — preferred but not strictly required), M1.1-05 (prompt tightening — preferred but not strictly required)

## Purpose

Today's M1 collapses three operations into two commands:

- `carve plan "<goal>"` does design **and** code generation in one shot.
- `carve apply <plan_id>` runs once and refuses to repeat.
- `carve run <pipeline>` is a TODO stub.

Two real problems fall out of that:

1. **The user can't iterate on the design before code is generated.** Refinement requires a fresh plan, paid in full.
2. **Plans are treated as the durable object.** They aren't — pipelines are. A pipeline is a code asset with a name, a directory, an execution history, and (later) a schedule. Plans are biographical artifacts of how it came to exist or change. The CLI should center pipelines.

Refactor the lifecycle to match how operators think:

| Operation | Command | Notes |
|---|---|---|
| Author a new pipeline | `carve plan "<goal>"` → `carve build <plan_id>` | One-time per pipeline; plan is conversational, no files. Build creates the pipeline. |
| Iterate on a draft | `carve plan --refine <plan_id> "<feedback>"` | Pre-build; new plan revision linked via `parent_plan_id`. |
| Modify an existing pipeline | `carve plan --pipeline <name> "<change>"` → `carve build <plan_id>` | Rare. Build replaces the pipeline files. Git is version control. |
| Execute | `carve run <pipeline_name>` | Primary API. No plan involved. Run as many times as you want. |
| List pipelines | `carve pipelines` / `carve pipelines <name>` | Inspect pipeline state, plan history, recent runs. |
| Promote to prod (M2) | `carve apply <pipeline_name>` | Reserved verb. Prints a placeholder in M1.1. |

The plan/build/run/apply split makes verbs honest, and the `<pipeline_name>` argument on `run`/`apply` makes pipelines the operational unit. Plan ids stay queryable for history and debug-replay (`carve run --plan <id>`), but they aren't the primary key for running.

## Scope

### In scope

- A new `pipelines` table tracking each pipeline as a first-class entity.
- `carve plan "<goal>"` produces a **textual design** (no files) and persists a Plan with `phase = "drafted"`.
- `carve plan --refine <plan_id> "<feedback>"` creates a sibling Plan with `parent_plan_id = <plan_id>`.
- `carve plan --pipeline <name> "<change>"` creates a draft plan that targets an existing pipeline; the design context includes the existing files so the agent can propose a delta.
- `carve build <plan_id>` invokes a second agent — the **build agent** — that consumes the design and writes/overwrites `pipelines/<name>/`. On success: creates or updates the `Pipeline` row, sets the plan to `phase = "built"`, points `Pipeline.current_plan_id` at it.
- `carve run <pipeline_name>` executes the pipeline as it exists on disk, via `LocalVenvRunner`. No plan reference required. Re-runnable at will.
- `carve run --plan <plan_id>` (advanced/debug): execute the pipeline that the plan points to. Useful when verifying a specific revision.
- `carve pipelines` lists pipelines (name, description, latest plan id, run count, last run status, last run timestamp).
- `carve pipelines <name>` shows one pipeline's lineage: plan history (parent chain + descendants) + run history.
- `carve apply <pipeline_name>` is a stub reserved for M2: prints "Use `carve run` to execute in dev. `carve apply` will create a prod-deploy PR (M2)."
- The replay guard from the M1 integration is **removed from `carve run`** — re-runs are the expected operation. (The guard moves to `carve apply` in M2 where idempotency matters.) This collapses M1.1-07 to a one-line cleanup; consider it superseded once this lands.

### Out of scope

- M2 prod-PR deployment via `carve apply`. Reserve the verb only.
- Caching agent reasoning across plan→build to avoid the second invocation. Two distinct agent calls is fine for M1.1.
- Cross-pipeline dependencies (run B after A succeeds). M3 territory.
- Mid-build interactivity. Build agent runs to completion; if it goes off the rails, you `carve plan --refine` and rebuild.
- Soft-build / atomic swap (write to temp dir, swap on success). Git is the safety net — if a build clobbers a working pipeline, the user reverts.
- Pipeline rename / pipeline delete commands. Manual rename of `pipelines/<name>/` + a state-store update is fine for now.
- Scheduling. M3.

## Data model

### `pipelines` table (new)

| Column | Type | Notes |
|---|---|---|
| `name` | TEXT | Primary key. Matches the directory name under `pipelines/<name>/`. |
| `description` | TEXT | One-line summary, derived from the design's `goal`. Updatable on rebuild. |
| `pipeline_dir` | TEXT | Relative path; almost always `pipelines/<name>`. Stored explicitly so a future spec can support custom layouts. |
| `current_plan_id` | TEXT | The plan whose build produced the current state on disk. Foreign key to `plans.id`. |
| `created_at` | DATETIME | First successful build. |
| `updated_at` | DATETIME | Last successful build. |
| `last_run_id` | TEXT | The most recent run's id, regardless of status. Foreign key to `runs.id`. Nullable. |
| `last_run_status` | TEXT | Denormalized for cheap listing. Nullable. |
| `last_run_at` | DATETIME | Denormalized. Nullable. |

`schedule` and related fields are M3.

### `plans` table changes

- Add `phase: TEXT` with values `drafted | built`. Default `drafted`. CHECK constraint enforced.
- Add `pipeline_name: TEXT` — nullable on draft plans (we don't always know the pipeline name yet on first plan; the agent proposes one in the design). Set during build to the name the build agent landed on. Foreign key to `pipelines.name`.
- `parent_plan_id` already exists from M1-03. Wire the refine flow to populate it.
- Existing `applied_at` / `apply_run_id` columns: rename in semantics, not schema. They become "first run that materialized this plan" — useful for debug-replay queries. The replay guard that read them goes away.

### `runs` table changes

- Add `pipeline_name: TEXT`. Foreign key to `pipelines.name`. Nullable for build-runs (which don't target a pipeline by name yet at the moment they start).
- `target_id` already exists from M1-03 — keep it as the loose generic target reference, but `pipeline_name` is the canonical lookup column for `carve run`/`carve logs`/`carve runs --pipeline <name>`.
- Run rows for `kind="build"` reference the `Plan.id` they were building, plus the `Pipeline.name` once known.

### Migrations

> **Updated during implementation (2026-04-29):** Alembic layout is the standard `migrations/versions/` directory tree, not flat `migrations/*.py`. Migration env disables foreign keys only during migrations to permit `batch_alter_table`; runtime keeps `PRAGMA foreign_keys=ON` via the SQLite connect listener. Backfill validates the derived `pipeline_name` against `^[a-z][a-z0-9_]*$` and skips with an INFO log on mismatch.

The same path as M1.1-06's earlier draft: introduce Alembic now. Two migrations:

- `migrations/versions/0001_baseline.py` — capture the M1 schema as-is.
- `migrations/versions/0002_pipeline_centric.py` — add `pipelines` table, add columns, backfill `pipelines.name` rows from existing applied plans (for the dev who's been smoke-testing today).

Supporting infrastructure: `alembic.ini` at the project root, `migrations/env.py`, `migrations/script.py.mako`. `alembic>=1.13` is added to `pyproject.toml` as a runtime dependency.

Backfill logic: for each plan in the existing `plans` table where `applied_at IS NOT NULL`, attempt to derive `pipeline_name` from the plan's `task_graph_json.pipeline_dir`, validate it matches `^[a-z][a-z0-9_]*$`, and synthesize a `Pipeline` row. Best-effort; if the JSON is missing fields or the derived name fails validation, skip with an INFO log line.

## Plan agent

`src/carve/core/agents/prompts/m1_plan_agent.md`. The plan agent's job is **design**, not code.

Prompt covers:

- Tools: `read_file`, `run_snowflake_query`, `submit_plan(design)`. The `submit_plan` tool's `input_schema` is the `design` document below.
- Connection context preamble (from M1.1-05's pattern: target, database, schema, role, warehouse).
- Pipeline context preamble: when invoked with `--pipeline <name>`, include the existing `pipelines/<name>/main.py` and `requirements.txt` contents so the agent can propose a delta. Otherwise the section is omitted.
- Rules: design only; do not write any files; surface tradeoffs and open questions instead of guessing; if the user's goal is ambiguous (which connection target? which time window?), ASK in `open_questions`.

> **Updated during implementation (2026-04-29):** `AgentLoop` gained a `terminator_tool: str | None = None` kwarg; the planner instantiates the loop with `terminator_tool="submit_plan"`. The loop returns its `AgentResult` immediately after the terminator tool fires (no extra `messages.create` round trip). `make_submit_plan_tool` rejects a second invocation so the contract holds even if the model retries.

The agent calls `submit_plan(...)` once and the loop terminates.

### Design document shape

```json
{
  "pipeline_name": "iowa_liquor_sales",
  "description": "Daily ingest of the most recent Iowa liquor sales rows.",
  "is_new_pipeline": true,
  "source": {
    "type": "socrata_api",
    "url": "https://data.iowa.gov/resource/m3tr-qhgy.csv",
    "row_limit": 10000,
    "ordering": "date DESC"
  },
  "destination": {
    "database": "<from connection context>",
    "schema": "<from connection context>",
    "table": "IOWA_LIQUOR_SALES",
    "primary_key": "INVOICE_LINE_NO"
  },
  "transformation": {
    "strategy": "merge_upsert",
    "rationale": "Bounded row count from prompt; MERGE on PK keeps re-runs idempotent without destructive truncate."
  },
  "columns": [
    {"name": "INVOICE_LINE_NO", "type": "VARCHAR(50)", "nullable": false},
    ...
  ],
  "requirements": ["snowflake-connector-python", "sodapy"],
  "estimates": {
    "rows": 10000,
    "approx_runtime_minutes": 10
  },
  "tradeoffs": [
    "Row-by-row MERGE is slow at scale; acceptable at 10k.",
    "PRIMARY KEY in Snowflake is informational only.",
    "Script will pass `role=` to connect() so SNOWFLAKE_ROLE is honored."
  ],
  "open_questions": []
}
```

`pipeline_name` is the agent's proposal for what to call the pipeline. The orchestrator validates it (kebab/snake_case, doesn't conflict with existing pipelines unless `--pipeline` was passed, doesn't escape `pipelines/`).

## Build agent

`src/carve/core/agents/prompts/m1_build_agent.md`. Narrower job: write the code.

Prompt covers:

- Tools: `read_file`, `write_file`. No `run_snowflake_query` — exploration happened in plan.
- Connection context preamble (same as plan agent).
- Pipeline context preamble: existing files when `--pipeline` was used; the build agent must respect destination/schema/transformation choices from the design.
- Design preamble: the full `design` JSON, formatted as markdown tables for readability, included at the top of the system prompt.
- Rules from M1.1-05: no Python defaults for `SNOWFLAKE_*` env vars; pass `role=` to the connection; no "How to Run" section in the final response (the runner handles execution).
- Final response: a short summary naming the files written.

The build agent has narrower context and should converge in fewer turns than the M1 code agent does today.

## CLI surface

### `carve plan`

```
carve plan "<goal>"                                # new pipeline
carve plan --pipeline <name> "<change>"            # modify existing pipeline
carve plan --refine <plan_id> "<feedback>"         # iterate a draft
carve plan --target dev "<goal>"                   # explicit target (M1 default_target stays default)
```

Output: rich-formatted summary derived from the design (destination, transformation strategy, requirements, estimates, tradeoffs, open questions). Plan id at the bottom plus next-step suggestions: `carve build <id>` and `carve plan --refine <id>`.

### `carve build`

```
carve build <plan_id>             # build the plan; create or update the pipeline
carve build <plan_id> --force     # rebuild even if already built
```

Output: file list written, build agent's brief summary, the build's run id. Suggests `carve run <pipeline_name>` next.

### `carve run`

```
carve run <pipeline_name>         # primary API
carve run --plan <plan_id>        # debug/replay: run the version pointed to by this plan
```

Output: live log tail, final status. Re-run as often as you want.

### `carve pipelines`

```
carve pipelines                   # table of pipelines
carve pipelines <name>            # detail view: plan history + recent runs
```

### `carve apply` (M2 placeholder)

```
carve apply <pipeline_name>
```

Prints:
```
carve apply will create a prod-deploy PR for this pipeline (arrives in M2).
For dev execution, use:  carve run <pipeline_name>
```
Exits 0.

### Other unchanged commands

`carve runs`, `carve logs <run_id>`, `carve init`, `carve version` — unchanged. `carve runs` gains an optional `--pipeline <name>` filter as a small bonus.

## Implementation

### File-level changes

Approximate file map. Engineers can rearrange:

- `src/carve/core/state/models.py` — add `Pipeline` model; add `phase`, `pipeline_name` to `Plan`; add `pipeline_name` to `Run`.
- `src/carve/core/state/repository.py` — `create_or_update_pipeline`, `get_pipeline`, `list_pipelines`, `get_pipeline_lineage`, `record_pipeline_run` (touches `last_run_*` denorms), and the renamed plan helpers (`mark_plan_built`, drop `mark_plan_applied`).
- `src/carve/core/agents/prompts/m1_plan_agent.md` — new file; replaces `m1_code_agent.md`.
- `src/carve/core/agents/prompts/m1_build_agent.md` — new file.
- `src/carve/core/agents/prompts/m1_code_agent.md` — delete or repurpose; the file-writing role moves entirely to the build agent.
- `src/carve/cli/orchestrator/planner.py` — drop the file-snapshot logic; add `submit_plan` tool; produce a Plan with `phase="drafted"`.
- `src/carve/cli/orchestrator/builder.py` — new module; consumes a plan, runs the build agent, materializes the pipeline directory, upserts the `Pipeline` row.
- `src/carve/cli/orchestrator/runner.py` — renamed from `applier.py`; runs by pipeline name (default) or by plan id (via `--plan`). Replay guard removed.
- `src/carve/cli/orchestrator/listing.py` — extend with `render_pipelines_table` and `render_pipeline_detail`.
- `src/carve/cli/commands/plan.py` — add `--refine` and `--pipeline`; wire to planner.
- `src/carve/cli/commands/build.py` — new typer command.
- `src/carve/cli/commands/run.py` — real implementation, replaces stub.
- `src/carve/cli/commands/apply.py` — print the M2 placeholder; accept a pipeline name argument.
- `src/carve/cli/commands/pipelines.py` — new typer command.
- `src/carve/cli/main.py` — register the new subcommands.
- `migrations/versions/0001_baseline.py`, `migrations/versions/0002_pipeline_centric.py` — Alembic migration scripts. Plus `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako` for Alembic infrastructure.

### Refine flow

`carve plan --refine <plan_id> "<feedback>"`:

1. Look up parent plan; refuse if `phase != "drafted"` (you can't refine a built plan; modify the pipeline instead via `--pipeline`).
2. Construct initial messages: parent plan's goal + design as agent context, then user `feedback` as a new user message.
3. Run the plan agent. It calls `submit_plan` with refined design.
4. Persist new plan with `parent_plan_id` set. Print both ids and a brief diff (field-by-field).

### Build flow

`carve build <plan_id>`:

1. Look up plan; refuse if `phase != "drafted"` unless `--force`.
2. Determine `pipeline_name` from `design.pipeline_name`. If `--pipeline` was passed during planning, that name is already locked in.
3. Construct build-agent messages: design as system-prompt preamble, plus existing files if modifying.
4. Create a build-run row (`kind="build"`, `target_id=plan_id`).
5. Run the build agent. Capture the file list it wrote.
6. Snapshot/diff `pipelines/<name>/` to confirm what changed; refuse build success if no `main.py` was written.
7. Upsert `Pipeline` row (create if new, update `current_plan_id` and `updated_at` if existing).
8. Mark plan `phase="built"`, `pipeline_name=<name>`, `built_at=<now>`, `built_run_id=<build run id>`.
9. Print summary; suggest `carve run <pipeline_name>`.

### Run flow

`carve run <pipeline_name>`:

> **Updated during implementation (2026-04-29):** Runner additionally enforces project-root containment of `pipeline_dir` as defense in depth, rejecting any pipeline whose resolved directory escapes the project root.

1. Look up pipeline by name. Reject with exit 2 if not found.
2. Read `pipelines/<name>/requirements.txt` to pick up any drift (user may have hand-edited).
3. Build `PythonStepConfig` (script, requirements, timeout from `config.runner.default_timeout_seconds`, env={}).
4. Create a run row (`kind="run"`, `target_id=<plan_id>` for traceability if `Pipeline.current_plan_id` is set, `pipeline_name=<name>`).
5. Build `LocalVenvRunner`, dispatch, live-tail logs (using the M1.1 fix: `since_id` not `since` timestamp).
6. On terminal status, update `Pipeline.last_run_*` denorms, print final status, exit code mapping.

`carve run --plan <plan_id>`:

1. Look up plan, then its pipeline.
2. Same as above, but verify the on-disk script matches the build's expected file list (warn if drift).

### Pipelines flow

`carve pipelines`: list rows from `pipelines` table, joined to `runs` for last-run details. Rich table.

`carve pipelines <name>`: pipeline detail (description, pipeline_dir, current plan, created/updated, last 10 runs) + plan lineage tree (parent chain + refinement children of the current plan).

## Tests

Substantial test work; outline:

- `tests/cli/orchestrator/test_planner.py` — rewritten:
  - `submit_plan` tool emission → Plan row with `phase="drafted"`, no files written.
  - Refine path → parent_plan_id set correctly, design merged.
  - `--pipeline <existing>` path → existing files included in agent context.
  - Plan agent emits text-only (no `submit_plan` call) → clear error.
- `tests/cli/orchestrator/test_builder.py` — new:
  - Build agent writes both files → pipeline row created, plan marked built.
  - Build agent fails to write `main.py` → plan stays drafted, build run marked failed.
  - Build a plan that targets an existing pipeline → existing pipeline row updated, files replaced.
  - `--force` rebuild on a built plan succeeds.
- `tests/cli/orchestrator/test_runner.py` — refactored from `test_applier.py`:
  - `carve run <pipeline_name>` happy path.
  - `carve run` of a nonexistent pipeline → exit 2, clear error.
  - Re-running a successful pipeline succeeds (no replay guard).
  - `carve run --plan <plan_id>` resolves to the right pipeline.
- `tests/cli/orchestrator/test_pipelines.py` — new:
  - `carve pipelines` empty state and populated table.
  - `carve pipelines <name>` shows lineage and recent runs.
- `tests/test_cli.py`:
  - `carve apply` prints the M2 placeholder.
- `tests/core/state/test_repository.py`:
  - `Pipeline` CRUD, lineage walk, denorm updates.
  - Phase CHECK constraint enforcement.
- Migration tests at `tests/migrations/test_migrations.py` (new test directory): `0002` adds the new tables, backfills correctly, leaves the old plans queryable.

## Acceptance criteria

- `carve plan "<goal>"` returns a design summary and a plan id; **no files** under `pipelines/`.
- `carve plan --refine <id> "<feedback>"` produces a child plan with a sane diff printed.
- `carve plan --pipeline <name> "<change>"` proposes a modification design with the existing files in context.
- `carve build <plan_id>` writes the pipeline files and creates/updates the pipeline row.
- `carve run <pipeline_name>` runs the pipeline as it stands on disk. Re-runnable any number of times.
- `carve run --plan <plan_id>` runs the version a specific plan built.
- `carve pipelines` lists pipelines and `carve pipelines <name>` shows lineage.
- `carve apply <pipeline_name>` prints the M2 placeholder and exits 0.
- The replay guard is gone from `carve run`. (M1.1-07 superseded.)
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover the new commands and helpers.
- README walkthrough updated to show the pipeline-centric flow. CHANGELOG entry under `## [Unreleased]`.

## Files this spec produces

> **Updated during implementation (2026-04-29):** Alembic uses the standard `migrations/versions/` layout (not flat `migrations/*.py`); added supporting Alembic infrastructure files; added `tests/migrations/test_migrations.py` in a new `tests/migrations/` directory; recorded touches to `pyproject.toml` (alembic dependency), the agent loop (`terminator_tool` kwarg), `m1_tools.py` (submit_plan tool), `database.py` (FK pragma listener), and observers/loop module renames revealed by `git status`.

New:

- `src/carve/cli/orchestrator/builder.py`
- `src/carve/core/agents/prompts/m1_plan_agent.md`
- `src/carve/core/agents/prompts/m1_build_agent.md`
- `src/carve/cli/commands/build.py`
- `src/carve/cli/commands/pipelines.py`
- `tests/cli/orchestrator/test_builder.py`
- `tests/cli/orchestrator/test_pipelines.py`
- `migrations/versions/0001_baseline.py`
- `migrations/versions/0002_pipeline_centric.py`
- `migrations/env.py`
- `migrations/script.py.mako`
- `alembic.ini`
- `tests/migrations/__init__.py`
- `tests/migrations/test_migrations.py`

Modified:

- `pyproject.toml` (add `alembic>=1.13` runtime dep)
- `src/carve/cli/commands/plan.py` (`--refine`, `--pipeline`; markup escape on agent-supplied strings)
- `src/carve/cli/commands/apply.py` (M2 placeholder, accepts pipeline name)
- `src/carve/cli/commands/run.py` (real impl)
- `src/carve/cli/commands/runs.py` (`--pipeline` filter)
- `src/carve/cli/main.py`
- `src/carve/cli/orchestrator/__init__.py`
- `src/carve/cli/orchestrator/planner.py`
- `src/carve/cli/orchestrator/applier.py` → renamed to `runner.py`; replay guard removed; project-root containment check added
- `src/carve/cli/orchestrator/listing.py` (markup escape on agent-supplied strings; pipeline rendering helpers)
- `src/carve/core/agents/__init__.py`
- `src/carve/core/agents/loop.py` (add `terminator_tool` kwarg; early-return on terminator tool)
- `src/carve/core/agents/m1_tools.py` (`make_submit_plan_tool`; rejects second call)
- `src/carve/core/state/__init__.py`
- `src/carve/core/state/database.py` (SQLite connect listener: `PRAGMA foreign_keys=ON`)
- `src/carve/core/state/models.py`
- `src/carve/core/state/repository.py`
- `src/carve/core/agents/prompts/m1_code_agent.md` — delete
- `tests/cli/orchestrator/test_planner.py` (rewritten)
- `tests/cli/orchestrator/test_applier.py` → renamed to `tests/cli/orchestrator/test_runner.py`
- `tests/core/agents/test_loop.py`
- `tests/core/state/test_repository.py`
- `tests/test_cli.py`
- `README.md`
- `CHANGELOG.md`

## What this enables

- The CLI finally matches how operators think: pipelines are durable; plans are how they got there.
- Re-running a pipeline is the cheap, expected operation — no plan needed.
- Iteration on a draft is cheap (no code generation between turns).
- The build agent is narrower than today's combined agent and could shift to Haiku in a later spec for cost savings, while the plan agent stays on Sonnet for design quality.
- Plan history (parent_plan_id chain) is preserved and visible — a foundation for the M2 web UI's plan-comparison view.
- M2's `carve apply` slot is reserved with the right semantics: deploy this pipeline to prod via PR. The lifecycle reads: `plan` → `build` → `run` (dev) → `apply` (prod).
- M3 scheduling slots in cleanly: schedule fields go on the `Pipeline` row, the scheduler keys on `pipeline_name`.
