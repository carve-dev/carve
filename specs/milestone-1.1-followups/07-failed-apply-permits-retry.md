# M1.1-07 — Drop the `carve apply` replay guard

**Milestone:** 1.1 — M1 follow-ups
**Estimated effort:** 0.1 day (tiny — one branch + one helper call gets removed)
**Dependencies:** M1 integration (orchestrator/applier)
**Supersedes by:** M1.1-06 if that lands first.

## Purpose

The replay guard added during the M1 integration blocks any second `carve apply` of a plan, regardless of whether the prior run succeeded or failed. Surfaced during the first M1 smoke test: a 4-second Snowflake-permissions failure left the plan locked, forcing the user to either pay for a fresh `carve plan` or hand-edit SQLite.

The guard's premise — "an applied plan shouldn't be silently re-executed" — assumed plans were the operational unit. **They aren't.** Pipelines are. Re-running a pipeline is the normal operation, and the guard was solving a non-problem. Drop it.

If M1.1-06 (pipeline-centric lifecycle) lands first, this spec is **superseded**: that spec already removes the guard from the renamed `carve run` command and reserves `carve apply` for M2 prod-PR deploys, where idempotency genuinely matters.

If M1.1-06 hasn't landed yet, this spec is the minimum viable fix — drop the guard from `carve apply` so smoke-testing isn't blocked by transient failures.

## Scope

### In scope (only if M1.1-06 hasn't landed)

- Remove the `applied_at is not None` check from `apply_plan` in `src/carve/cli/orchestrator/applier.py`.
- Remove the `mark_plan_applied(...)` call. The column stays in the schema for now; nothing reads it after this change. (M1.1-06 will repurpose it as a `last_run_id` denorm.)
- Update the test `test_apply_rejects_already_applied_plan` to invert: applying twice should succeed twice.
- Update the documentation comment in `apply_plan` to explain that re-runs are expected.

### Out of scope

- Anything M1.1-06 covers. If both specs are in play, defer to M1.1-06.
- Cleaning up the unused `applied_at`/`apply_run_id` columns. M1.1-06's migration handles that.
- Adding a different guard for `carve apply` semantics. M2 introduces the prod-PR flow; that's where the guard belongs (e.g., "block apply if the pipeline hasn't successfully run in dev since the last build").

## Implementation

`src/carve/cli/orchestrator/applier.py`:

- Delete the `if plan_row.applied_at is not None: ...` branch and its error message.
- Delete the post-success `repo.mark_plan_applied(...)` call.

`src/carve/core/state/repository.py`:

- `mark_plan_applied` becomes dead code after the call site is removed. Leave the helper in place for one cycle; M1.1-06 deletes it when it renames the orchestrator file.

`tests/cli/orchestrator/test_applier.py`:

- Replace `test_apply_rejects_already_applied_plan` with `test_apply_succeeds_when_run_twice`. Assert: two consecutive `apply_plan(...)` calls both produce success runs and the run history contains two rows for the plan.

## Acceptance criteria

- `carve apply <plan_id>` succeeds on a previously-applied plan.
- The `applied_at` column may or may not be set after the call — its semantics are undefined until M1.1-06 either reuses it or drops it.
- `ruff` + `mypy --strict` + full `pytest` stay green.

## Files this spec produces

Modified:

- `src/carve/cli/orchestrator/applier.py` (drop guard, drop mark call)
- `tests/cli/orchestrator/test_applier.py`
- `CHANGELOG.md` (one-line note)

No new files.

## What this enables

- Smoke-testing M1 stops requiring manual SQLite surgery when a pipeline fails.
- The `applied_at` column is freed up for M1.1-06's repurpose (or removal).
- The replay-guard concept moves to where it belongs — M2's prod-promotion `carve apply`.
