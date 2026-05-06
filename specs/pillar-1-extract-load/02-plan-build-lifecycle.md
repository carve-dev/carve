# P1-02 — Plan / Build lifecycle (per-target)

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1 day
**Dependencies:** M1-03 (state store), M1.1-06 (existing plan/build/run/deploy lifecycle), P1-01 (target system)
**Lineage:** Continues **M1.1-06** (plan/build/run/deploy verbs, `Pipeline` table, plan-as-design contract). Reuses **accepted M2-01** (Build entity, migration `0004_build_entity.py`, `Pipeline.current_plan_id` → `current_build_id`). Net-new in this spec: per-target output paths. The multi-task task graph and build-coordinator pattern from accepted M2-01 are explicitly **deferred to Pillar 2** (only meaningful with multiple specialists).

## Purpose

Adapt M1.1-06's plan / build / run / deploy lifecycle to the per-target folder model from P1-01, and introduce **Build** as the durable, deployable unit. In Pillar 1 the lifecycle has one specialist (the extract-load agent, P1-05), so the multi-task task graph and build-coordinator pattern from accepted M2-01 are deferred — Pillar 1's build flow invokes the extract-load specialist directly.

## What this introduces

1. **`builds` table.** New SQLAlchemy model, new repository methods, new Alembic migration.
2. **`Pipeline.current_plan_id` → `Pipeline.current_build_id`** rename.
3. **Drops `plans.estimates_json`, `plans.deployed_at`, `plans.deploy_run_id`** — vestigial after Build becomes first-class.
4. **Build flow refactor** so `carve build <plan_id>` writes to `targets/<active_target>/el/<name>/` (was `pipelines/<name>/`) and creates a Build row.
5. **Plan agent unchanged externally.** `m1_plan_agent.md`'s contract stays; only the `--target` flag's role becomes well-defined (it tells the plan agent which target's schemas to inspect).

## Data model

### `builds` table (new)

The deployable artifact. Every successful `carve build` produces one row.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | Primary key. `build_<hex>` (uuid4().hex). |
| `pipeline_name` | TEXT NOT NULL | FK to `pipelines.name`. (Pipeline-table misnomer noted below.) |
| `plan_id` | TEXT NOT NULL | FK to `plans.id`. Biographical reference to the plan that produced this build. |
| `target` | TEXT NOT NULL | The target the build was generated against (`dev`, `prod`, etc.). Set from the active target at build time. |
| `created_at` | DATETIME | When the build run completed successfully. |
| `manifest_json` | TEXT | JSON listing the files this build wrote (relative paths under `targets/<target>/`). Consumed by `carve el deploy` (P1-09). Example: `{"files": ["el/iowa_liquor/main.py", "el/iowa_liquor/requirements.txt", "snowflake/iowa_liquor.sql"]}`. |
| `commit_sha` | TEXT NULL | Set by `carve el deploy` Phase 2 — the git sha the deploy commit landed on. |
| `pr_url` | TEXT NULL | Set by `carve el deploy` — PR url for the deploy. |
| `deployed_at` | DATETIME NULL | Set on successful `carve el deploy verify` post-merge. |

Indices: `(pipeline_name, target, created_at DESC)` — supports "latest build of <name> for <target>" lookup.

### `pipelines` table changes

- **Drop** `current_plan_id` column.
- **Add** `current_build_id` column (TEXT NULL, FK to `builds.id`).
- Existing columns (`name`, `description`, `pipeline_dir`, `created_at`, `updated_at`, `last_run_id`, `last_run_status`, `last_run_at`) are unchanged.

`pipeline_dir` semantics evolve: in M1.1-06 it was always `pipelines/<name>`. Post-Pillar-1 it becomes `targets/<active_target>/el/<name>` for EL artifacts. Stored explicitly so a future spec (or hand-edits) can support custom layouts. The existing column type and validation are unchanged; only the value written changes.

### `plans` table changes

Three columns dropped in migration `0004` because Build now owns the corresponding state:

- **Drop `estimates_json`.** Cost / duration / Snowflake-credit estimates are deferred per the accepted M2-01 review.
- **Drop `deployed_at`.** Was misleadingly named after the `0003` apply→deploy rename — actually held the *first build* timestamp, not a real deploy. Build's `created_at` carries the same information now.
- **Drop `deploy_run_id`.** Same reasoning — pointed at the first build run; reachable now via `builds.plan_id` reverse lookup.

Plan retains: `id`, `parent_plan_id`, `goal`, `config_hash`, `carve_version`, `task_graph_json`, `file_path`, `phase`, `pipeline_name`, `created_at`, `expires_at`. No new columns.

The `phase` CHECK constraint stays (`drafted | built`).

### Migration `0004_build_entity.py`

Steps in order:

1. **Create `builds` table** with the columns above.
2. **Backfill builds rows** from existing pipelines:
   - For each `pipelines` row with non-null `current_plan_id`, synthesize a Build row:
     - `id = "build_" + uuid4().hex`
     - `pipeline_name = pipelines.name`
     - `plan_id = pipelines.current_plan_id`
     - `target` = `default_target` from `carve.toml` at migration time (best-effort; fall back to `"dev"` if config missing)
     - `created_at` = the original plan's `deployed_at` if set, else `pipelines.updated_at`, else `_utcnow()`
     - `manifest_json` = `{"files": []}` (empty for backfilled rows; deploy will fail-loud for these until rebuilt, which is fine — backfill targets dev-built pipelines that haven't been deployed)
     - `commit_sha`, `pr_url`, `deployed_at` = NULL
3. **Add `current_build_id` column** to `pipelines`. Backfill from the synthesized builds (one Build per Pipeline). Drop `current_plan_id` afterward.
4. **Drop `estimates_json`, `deployed_at`, `deploy_run_id`** from `plans`.

Use SQLAlchemy's `batch_alter_table` for the SQLite drops/renames (needed for SQLite). Migration env already disables FK during migrations per M1.1-06's note.

Downgrade reverses each step in inverse order. Rebuilding `current_plan_id` on downgrade picks the most recent build's `plan_id` per pipeline.

Tests in `tests/migrations/test_migrations.py`:

- `test_0004_creates_builds_and_renames_pipeline_fk` — schema after upgrade matches expected.
- `test_0004_backfills_builds_from_existing_pipelines` — synthetic legacy DB is constructed; after upgrade, every pipeline has a `current_build_id` and a corresponding Build row.
- `test_0004_drops_vestigial_plan_columns` — `estimates_json`, `deployed_at`, `deploy_run_id` no longer present.
- `test_0004_round_trip` — upgrade → downgrade → upgrade preserves the schema shape.

### Pipeline-table misnomer (carry-over)

The `pipelines` table in M1.1-06 was named when "pipeline" meant "the user's named code unit." In the four-pillar model, **Pipeline (Pillar 3)** has a different meaning (a definition that orchestrates EL + dbt + ad-hoc steps). Renaming `pipelines` → `artifacts` (or splitting into per-pillar tables) is not in scope for Pillar 1; we accept the misnomer locally and revisit when Pillar 3 lands. The CLI surface (`carve el run`, `carve el list`) hides the misnomer from users.

## Plan flow (per-target)

`carve plan "<goal>" [--target X]` keeps the M1.1-06 flow:

1. Resolve active target (P1-01: `--target` → `CARVE_TARGET` → `default_target` → `"dev"`).
2. Load connection context for the active target (`targets/<active>/connections.toml` + `.env` via P1-04).
3. Run the plan agent (`src/carve/core/agents/prompts/m1_plan_agent.md`) with tools `read_file`, `run_snowflake_query` (read-only against the active target), `submit_plan(design)`.
4. Plan agent calls `submit_plan(design)`; loop terminates.
5. Persist Plan row with `phase="drafted"`, `pipeline_name=design.pipeline_name`, `task_graph_json=<design as JSON>`, `parent_plan_id=<refine parent or NULL>`. **Target is NOT persisted on the Plan row.**
6. Render summary; suggest `carve build <plan_id>` and `carve plan --refine <plan_id> "<feedback>"`.

`carve plan --refine` and `carve plan --pipeline` keep their M1.1-06 semantics. `--target` resolution applies the same way for all three forms.

**Why no `plans.target` column.** A Plan is a *design*; the target it was inspected against affects the design's choices (column types, sample sizes) but doesn't bind the plan to a specific target. A single plan can drive builds against multiple targets (rare in practice for v0.1 but architecturally clean) — the Build row carries the target, not the Plan.

## Build flow (per-target)

`carve build <plan_id> [--target X] [--force]`:

1. Look up plan by id; refuse with `PlanNotFoundError` if missing.
2. Refuse if `phase != "drafted"` unless `--force`.
3. Resolve active target (P1-01).
4. Determine `pipeline_name` from `design.pipeline_name`. Validate the naming regex (`^[a-z][a-z0-9_]*$`).
5. Resolve `pipeline_dir` to `targets/<active_target>/el/<pipeline_name>`. Ensure parent directories exist.
6. Create build-run row (`kind="build"`, `target_id=plan_id`, `pipeline_name=pipeline_name`, `target=<active_target>`).
7. **Run the extract-load specialist (P1-05) directly.** No coordinator wrapper in Pillar 1 — the extract-load agent is invoked with the design as preamble, scoped to write into `pipeline_dir`. Tools: `read_file`, `write_file` scoped to `pipeline_dir`, `lookup_skill`, `run_snowflake_query` (read-only against the active target), `submit_step(file_list, summary)` (terminator).
8. The specialist writes the files; calls `submit_step` with the list. Files include `main.py`, `requirements.txt`, and (per P1-07) `targets/<active_target>/snowflake/<pipeline_name>.sql`.
9. Snapshot/diff `pipeline_dir`; refuse build success if no `main.py` was written (regression guard from M1.1-06).
10. Upsert `Pipeline` row (insert if new, update `pipeline_dir`, `updated_at` if existing).
11. **Create `Build` row** (`id=build_<hex>`, `pipeline_name`, `plan_id`, `target=<active_target>`, `created_at=now`, `manifest_json=<file list>`).
12. Set `Pipeline.current_build_id = new_build.id`.
13. Mark plan `phase="built"`, `pipeline_name=<name>` (regression guard: should match step 4).
14. Print summary; suggest `carve el run <pipeline_name>` and `carve el deploy <pipeline_name> --to <target>`.

**No coordinator, no specialist dispatch.** Pillar 1 has one specialist; the build flow calls it directly. When Pillar 2 lands, this spec gets a follow-up that swaps the direct call for the coordinator's `invoke_specialist(agent_name, task)` dispatch.

**`--target` on `carve build`.** Allowed; lands the build in `targets/<X>/el/<name>/`. There's no plan-vs-build target conflict check — Plans aren't bound to a target (see "Why no `plans.target` column" above). A user who plans against `dev`'s schema and then builds against `prod` is responsible for ensuring the plan's design fits prod's schema; if it doesn't, the recovery agent (P1-10) catches the mismatch at run time and offers to refine the plan.

## What stays from M1.1-06 unchanged

- Plan agent prompt (`m1_plan_agent.md`) — same contract.
- Plan refinement (`carve plan --refine <id> "<feedback>"`).
- Pipeline-targeted planning (`carve plan --pipeline <name> "<change>"`).
- Plan persistence model (a row + `.carve/plans/<id>.json` on disk).
- The `Plan.phase` CHECK constraint and lifecycle.
- Re-runnable `carve run` (no replay guard); the deploy-rename + migration `0003`.
- All existing tests around plan/build flow continue to pass after the path-resolution refactor (tests update their assertions to expect `targets/<active>/el/<name>` instead of `pipelines/<name>`).

## What changes from M1.1-06

| | M1.1-06 | P1-02 |
|---|---|---|
| Build output path | `pipelines/<name>/main.py` | `targets/<active>/el/<name>/main.py` |
| Pipeline reference | `Pipeline.current_plan_id` | `Pipeline.current_build_id` |
| First-build-time tracking | `Plan.deployed_at`, `Plan.deploy_run_id` | `Build.created_at`, `Build.plan_id` reverse |
| Estimates | `Plan.estimates_json` (dropped) | Deferred entirely |
| Build → File write | Build agent writes inline | Build flow calls extract-load specialist (P1-05); coordinator is deferred |
| Plan target | Implicit (active target at plan time) | Explicit at *build* time via `--target`; not persisted on Plan |

## Implementation

### File-level changes

New:

- `src/carve/core/state/models.py` — add `Build` model (already partially specced in accepted M2-01; this spec is the implementation).
- `migrations/versions/0004_build_entity.py` — the migration described above.
- `src/carve/core/state/repository.py` — `create_build(...)`, `get_build(build_id)`, `get_pipeline_current_build(name)`, `set_pipeline_current_build(name, build_id)`, `latest_build_for(name, target)`.
- `tests/core/state/test_repository.py` — Build CRUD; backfill behavior.

Modified:

- `src/carve/cli/orchestrator/planner.py` — same flow; rendering touches up the build hint (`carve el deploy` instead of `carve apply`).
- `src/carve/cli/orchestrator/builder.py` — write to `targets/<active>/el/<name>/` (not `pipelines/<name>/`); create Build row on success; set `Pipeline.current_build_id`; drop `mark_plan_built` plan-column writes (moved to Build).
- `src/carve/cli/commands/plan.py` — `--target` flag plumbing per P1-01.
- `src/carve/cli/commands/build.py` — `--target` flag plumbing per P1-01.
- `tests/cli/orchestrator/test_planner.py`, `test_builder.py` — assert per-target paths.
- `tests/migrations/test_migrations.py` — add `0004` tests listed above.

Removed:

- `Plan.estimates_json`, `Plan.deployed_at`, `Plan.deploy_run_id` (via migration). Repository's `mark_plan_built` no longer writes these columns; renames to `mark_plan_built_simple` or similar (or stays the same name and just doesn't touch dropped columns).

## Tests

- `test_build_writes_to_active_target_path` — `carve build <id> --target staging` lands files under `targets/staging/el/<name>/`.
- `test_build_creates_build_row` — Build has correct `pipeline_name`, `plan_id`, `target`, `manifest_json`.
- `test_build_sets_current_build_id` — `Pipeline.current_build_id` points at the new Build.
- `test_build_marks_plan_built` — Plan transitions to `phase="built"`.
- `test_build_refuses_built_plan_without_force` — exits 2; with `--force`, succeeds.
- `test_build_default_target` — no `--target` falls through to `default_target`.
- `test_build_invalid_pipeline_name_rejected` — naming regex enforced.
- `test_build_no_main_py_fails` — specialist returns without writing `main.py` → build run marked failed; no Build row created.
- `test_two_builds_against_different_targets` — `carve build <plan> --target dev` + `--target prod` produces two Build rows; Pipeline's `current_build_id` ends pointing at the most recent.
- Migration tests as listed above.

## Acceptance criteria

- `carve plan "<goal>" [--target X]` returns a design summary and a plan id; **no files** under `targets/`.
- `carve build <plan_id> [--target X]` writes `targets/<active_target>/el/<name>/{main.py, requirements.txt, ...}` plus the per-EL DDL file from P1-07. Creates a `Build` row. Updates `Pipeline.current_build_id`.
- `Pipeline.current_plan_id` is gone; lookup goes through `Build.plan_id`.
- `Plan.estimates_json`, `Plan.deployed_at`, `Plan.deploy_run_id` are gone from the schema.
- A second build against a different target produces a second Build row without disturbing the first.
- All existing M1.1-06 plan/build tests pass after path-resolution updates.
- `ruff` + `mypy --strict` + `pytest` stay green; new tests cover Build CRUD and the per-target build flow.

## Files this spec produces

(Summary of File-level changes section.)

New: `Build` model addition, migration `0004_build_entity.py`, repository helpers, ~5 test files.
Modified: `planner.py`, `builder.py`, `plan.py`, `build.py` (CLI), `models.py`, `repository.py`, plus existing build/plan tests retargeted to per-target paths.

## Out of scope

- **Multi-task task graphs.** Deferred to Pillar 2 alongside the orchestration-agent work. Pillar 1 plans have one task; the build flow doesn't iterate.
- **Build coordinator pattern** with `invoke_specialist(agent_name, task)`. Same reason — only useful with multiple specialists.
- **`carve plan diff`.** Not load-bearing for v0.1; defer.
- **Cost / duration / Snowflake-credit estimates.** Confirmed dropped in the accepted M2-01 review.
- **Guardrail check, plan expiry enforcement, file-diff previews, config-hash validation at deploy.** All confirmed deferred in the accepted M2-01 review.
- **Renaming the `pipelines` table** to a less-misleading name. Defer to Pillar 3 when actual pipeline definitions arrive.
- **`carve build --target X` rejecting a target that "differs from the plan's target."** Plans don't have a target; the user is trusted at build time. The recovery agent (P1-10) catches schema mismatches at run time.

## What this enables

- **`carve el run` and `carve el deploy`** (P1-08, P1-09) have a clean Build row to anchor on. Run reads the build's path; deploy ships the build's manifest.
- **The first credible Build entity in the codebase.** Future pillars can produce non-EL Builds (dbt builds, pipeline-definition builds, schedule builds) via the same shape.
- **Per-target dev/prod state** is reachable without a separate registry — the filesystem under `targets/<target>/el/` answers "what's currently in this target," and the `builds` table answers "what built it and when."
