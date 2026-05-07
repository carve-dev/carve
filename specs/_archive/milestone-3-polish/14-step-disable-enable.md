# M3-14 — Step disable / enable

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 0.5 day
**Dependencies:** M3-01 (multi-step pipelines), M3-02 / M3-03 (other step types — to confirm the field works uniformly across types)

## Purpose

M3-01 introduces multi-step pipelines and a pipeline-level `paused = false` flag, but no per-step equivalent. Operators routinely need to skip a single step without removing it from the pipeline:

- "Step 3 is failing intermittently — disable it until I have time to fix the upstream."
- "I'm debugging — skip the notify step on this run; the data load is what I'm verifying."
- "Re-enable now that the upstream is healthy."

Removing the step (via `carve plan --pipeline <name> "remove the freshness check"`) is heavy: it loses the step's history, requires a build, and the user usually wants the step back in days or hours, not gone forever.

Add a per-step `enabled` field to the pipeline TOML, plus imperative CLI commands to flip it without touching the file by hand.

## Scope

### In scope

- A new `enabled: bool = true` field on each step in `carve/pipelines/<name>.toml`.
- The DAG executor (`src/carve/core/runners/dag_executor.py`) treats disabled steps as **success-equivalent** for `depends_on` resolution: their downstream steps still run, the step's own status is recorded as `skipped`, and its outputs are absent (so any downstream Jinja reference like `{{ steps.<id>.outputs.foo }}` raises a clear "step was disabled" error rather than a missing-key trace).
- Imperative CLI commands:
  - `carve step disable <pipeline> <step_id>` — flip `enabled` to `false` in the TOML, validate the resulting pipeline still loads cleanly, write the file.
  - `carve step enable <pipeline> <step_id>` — flip back to `true`.
  - `carve step list <pipeline>` — show the steps with their `enabled` status and most recent run status.
- Validation: disabling a step that other enabled steps depend on must surface a warning. The pipeline still runs, but the user should know they're effectively orphaning a downstream branch. Don't outright reject — there are legit cases (debugging the disabled step's output bridge in isolation).
- Run-row status `skipped` becomes a recognized terminal state alongside `success`, `failed`, `cancelled`, `crashed` (see M1-03's `runs.status` column). Add to the CHECK constraint.
- The web UI's pipeline-monitor (M2-13) and dbt-run-view (M3-10) render disabled steps as visually distinct (greyed/striped/etc.). Out of scope here — flagged for those specs.

### Out of scope

- Per-run override (`carve run <pipeline> --skip <step_id>` — disable for one run only). The TOML edit + commit is the workflow; per-run overrides would create a different audit trail and are M3+ at earliest.
- Time-bounded disable (`carve step disable <pipeline> <step_id> --until 2026-05-15`). Useful but adds clock-handling complexity; revisit later.
- Disable from the agent (`carve plan --pipeline <name> "disable the notify step"`). The agent already can edit the TOML via build, so this works "for free" — just doesn't get a dedicated verb.
- Bulk operations (disable/enable many steps at once). YAGNI for v0.1.0.

## TOML schema

```toml
[[steps]]
id = "freshness_check"
type = "dbt"
command = "source freshness"
select = "source:salesforce"
depends_on = ["extract_salesforce"]
on_failure = "warn"
enabled = false   # <-- new field
```

Defaults to `true` when omitted. The pipeline schema in `src/carve/core/pipeline/schema.py` (created in M3-01) gains the field via Pydantic.

## DAG executor changes

In `src/carve/core/runners/dag_executor.py`:

- When building the graph, mark disabled steps with a sentinel status of `skipped` from the outset. They never enter the `ready` set for execution.
- In the dependency-satisfaction check (currently `all(dep in results and results[dep].status == "success" ...)`), treat `skipped` as success-equivalent.
- After the run, persist a `StepResult(status="skipped", duration_ms=0, outputs={}, error=None)` in `results[step_id]` so the per-step run row records the skip.
- Pipeline final status is `success` if every step ended in `success` or `skipped`; the existing failure-mode logic for non-skipped steps is unchanged.

If a downstream step references `{{ steps.<id>.outputs.<key> }}` where `<id>` was skipped, the Jinja layer raises a `JinjaTemplateError` with a message naming the disabled step and the field — clearer than a missing-key default.

## CLI commands

### `carve step disable <pipeline> <step_id>`

1. Locate `carve/pipelines/<pipeline>.toml`. Reject (exit 2) if not found.
2. Parse the TOML; locate the step with matching `id`. Reject (exit 2) if not found.
3. Set its `enabled = false`. (If the field was absent, add it with explicit `false`.)
4. Validate the resulting pipeline schema (via the M3-01 loader's validator).
5. Warn (yellow text, no error) if any other enabled step has `depends_on` containing this step id.
6. Rewrite the TOML, preserving comments and structure as best the parser allows. Use `tomlkit` for round-tripping (already a transitive dep via snowflake-connector-python).
7. Print confirmation: `✓ Disabled step 'freshness_check' in pipeline 'salesforce_to_marts'.`

### `carve step enable <pipeline> <step_id>`

Mirror of the above; flips to `true`. Warn if the step's `depends_on` references any currently-disabled step (the dependency chain may not be fully ready).

### `carve step list <pipeline>`

Render a rich table:

| Step ID | Type | Enabled | Last run status | Last run at |
|---|---|---|---|---|
| extract_salesforce | python | yes | success | 2026-04-30 17:55 |
| dbt_staging | dbt | yes | success | 2026-04-30 17:56 |
| freshness_check | dbt | **no** | success | 2026-04-29 12:00 |
| notify | http | yes | success | 2026-04-30 17:56 |

The "Last run" data comes from joining the steps to the most recent step-run rows (the per-step run records introduced in M3-01).

## Tests

`tests/core/runners/test_dag_executor.py` — extend:

- `test_disabled_step_is_skipped_and_downstream_runs` — three-step linear pipeline, middle step `enabled=false`, downstream still runs, middle step status is `skipped`.
- `test_disabled_step_with_outputs_reference_raises_clear_error` — downstream Jinja uses `{{ steps.disabled_id.outputs.x }}`; raises a clear `JinjaTemplateError`, not a stack trace.
- `test_pipeline_all_steps_disabled_returns_success_with_zero_runs` — edge case; no real work done, status is `success`.
- `test_disabled_step_status_persisted_to_run_row` — DB row's `status` column reads `skipped`.

`tests/cli/commands/test_step.py` (new):

- `test_step_disable_writes_enabled_false` — happy path; file roundtrip preserves comments; `enabled = false` ends up on the right step.
- `test_step_disable_warns_on_dependent_steps` — disabling a step with downstream consumers produces a yellow warning line.
- `test_step_disable_rejects_missing_step` — exit 2, clear error.
- `test_step_disable_rejects_missing_pipeline` — exit 2.
- `test_step_enable_round_trips` — disable then enable; final TOML matches initial.
- `test_step_list_renders_enabled_status` — table includes the enabled column.

`tests/core/state/test_repository.py` — extend:

- `test_runs_status_check_constraint_accepts_skipped` — insert a row with `status="skipped"`; passes.

## Acceptance criteria

- Setting `enabled = false` on a step in pipeline TOML causes the DAG executor to skip it and continue with downstream steps.
- A skipped step's run-row status is `skipped` and shows up in `carve runs` / `carve logs`.
- `carve step disable <pipeline> <step_id>` and `carve step enable <pipeline> <step_id>` flip the field reliably with a TOML round-trip that preserves comments.
- `carve step list <pipeline>` shows enabled status and last-run summary.
- A downstream step that references a disabled step's outputs raises a clear error, not a missing-key stack trace.
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover both the executor and the CLI commands.
- README walkthrough mentions the new commands; `## [Unreleased]` CHANGELOG entry.

## Files this spec produces

New:

- `src/carve/cli/commands/step.py` (typer command group with `disable` / `enable` / `list`)
- `tests/cli/commands/test_step.py`

Modified:

- `src/carve/core/pipeline/schema.py` (new `enabled` field) — created in M3-01
- `src/carve/core/runners/dag_executor.py` (skip logic) — created in M3-01
- `src/carve/core/runners/jinja.py` (clear error on disabled-step outputs reference) — created in M3-01
- `src/carve/core/state/models.py` (`runs.status` accepts `skipped`)
- `src/carve/cli/main.py` (register `step` subcommand)
- `tests/core/runners/test_dag_executor.py`
- `tests/core/state/test_repository.py`
- `README.md`
- `CHANGELOG.md`

## What this enables

- Operators can take a misbehaving step out of rotation in seconds, without losing the step's history or losing the rest of the pipeline.
- Debug-mode workflows (skip notify, skip cleanup) become a one-line CLI invocation.
- The web UI gets a clean rendering signal: `enabled=false` in the TOML feeds directly into a "disabled" visual state.
- Lays groundwork for time-bounded disables (`--until`) and per-run overrides (`carve run --skip`) in v0.2+ if those use cases get traction.
