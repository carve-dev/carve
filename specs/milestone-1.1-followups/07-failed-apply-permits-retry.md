# M1.1-07 — Failed runs permit retry

**Milestone:** 1.1 — M1 follow-ups
**Estimated effort:** 0.25 day
**Dependencies:** M1-03 (state store), M1 integration (orchestrator/applier)

## Purpose

Today's `carve apply` (and its renamed successor `carve run`, post-M1.1-06) marks a plan as "applied" the moment a run is dispatched, regardless of whether the run succeeds. The replay guard then blocks a second `apply`/`run` of that plan. The intent of the guard was to prevent silent re-execution of an already-successful plan ("destroy audit history" in the original review note). It overshoots: a 4-second failure that never wrote any data still locks the plan.

Surfaced during the first real M1 smoke test: the user's pipeline failed at `CREATE TABLE` (Snowflake permissions issue), and now the only way to retry the same plan is to either edit SQLite by hand or pay for a fresh `carve plan`. Neither is acceptable.

Make the guard distinguish success from failure: a plan with at least one **successful** run is considered applied; a plan whose only runs all failed remains retriable.

## Scope

### In scope

- A repository helper `plan_has_successful_run(plan_id) -> bool` that joins `plans` to `runs` (via `apply_run_id` / future `run_run_id`, or via a more general `runs.target_id == plan_id`) and checks whether any run reached terminal status `success`.
- Update the apply/run guard in the orchestrator to use that helper instead of the bare `applied_at is not None` check.
- Decide on the semantics of `applied_at` / `apply_run_id` (and post-M1.1-06's `built_at` / `built_run_id`):
  - **Option A:** keep marking on every attempt; the guard switches to "any prior success" via the new helper. `applied_at` becomes "first attempted at," which is misleading. Reject this option.
  - **Option B (recommended):** mark `applied_at` and `apply_run_id` only on **successful** terminal status. Failed runs leave both null. The guard then degenerates to today's logic — `applied_at is not None` means "succeeded at least once." Simplest, no new joins, and the column names stop lying.
  - Pick Option B. It's a behavior change to the existing `mark_plan_applied` helper: move the call from the apply-dispatch path to the post-completion success branch.
- A clear error message when the guard does fire, naming the prior successful run id and timestamp so the user knows what they're hitting.
- Tests: failed-then-retry succeeds; succeeded-then-retry blocks; multiple-fails-then-success works; subsequent retry after success blocks.

### Out of scope

- A `--force` flag to override the guard. The user can fall back to `carve plan` (cheap) if they really want to re-run a successful plan; we don't need a CLI escape hatch yet.
- Pruning the run history. All run rows persist for audit, including failed ones.
- Concurrent-retry guards (two `carve apply` invocations of the same plan racing). Single-user CLI; not a real risk in M1.
- Marking on `cancelled` status as success-equivalent. Cancellations are explicit user actions; treat them as not-applied (retriable). Document the choice.

## Implementation

### `src/carve/core/state/repository.py`

Update `mark_plan_applied(plan_id, run_id)` to be a no-op when called for a non-success terminal state. Or, more cleanly, rename the trigger:

```python
def record_run_completion(self, plan_id: str, run_id: str, status: str) -> None:
    """Record that a run terminated. Sets applied_at only on success."""
    if status != "success":
        return
    with self._session_factory() as session:
        plan = session.get(Plan, plan_id)
        if plan is None:
            raise KeyError(f"Plan {plan_id!r} not found")
        plan.applied_at = _utcnow()
        plan.apply_run_id = run_id
        session.commit()
```

Or keep the method named `mark_plan_applied` and add a `if status != "success": return` guard at the top — same behavior, less churn elsewhere.

The old call site in `applier.py` that fires before `runner.execute` goes away. The new call site is in the post-`runner.wait` path, after we know whether the run succeeded.

### `src/carve/cli/orchestrator/applier.py`

Move the `mark_plan_applied(...)` call from "before dispatch" to "after success" — specifically inside the `if final_status == "success":` branch (or wherever the success status is computed; today it's after the live-tail loop returns and `repo.get_run(run_id)` is read).

Update the replay-attack guard's error message:

```
Plan {plan_id} was already applied successfully at {applied_at} (run_id={apply_run_id}).
Re-running successful plans is not supported in M1.1; generate a new plan with
`carve plan`.
```

(Post-M1.1-06: replace the wording with `carve build` / `carve run` semantics. The guard moves to `runner.py` and the message references "ran" instead of "applied.")

### Tests

`tests/cli/orchestrator/test_applier.py`:

- `test_apply_succeeded_run_blocks_retry` — apply twice on a script that exits 0; second call exits 1 with the new error message naming the first run id.
- `test_apply_failed_run_permits_retry` — apply twice on a script that exits 7; first marks the run failed and leaves the plan unapplied; second call dispatches a fresh run.
- `test_apply_failed_then_succeeded_then_retry_blocks` — apply on a flaky script (use a counter file in `tmp_path`) that fails the first time and succeeds the second; third apply blocks.
- `test_apply_cancelled_run_permits_retry` — runner is forced to cancel mid-run (timeout); cancelled status leaves the plan unapplied; subsequent apply works.

`tests/core/state/test_repository.py`:

- `test_mark_plan_applied_is_no_op_for_failed_status` — call `mark_plan_applied(..., status="failed")`, assert plan.applied_at is None.
- `test_mark_plan_applied_records_success` — call with status="success", assert applied_at set.

## Acceptance criteria

- A plan whose only runs are `failed` or `cancelled` can be re-applied without manual SQLite surgery.
- A plan with at least one `success` run blocks subsequent applies, with an error message that names the successful run.
- The guard's error message tells the user how to proceed (replan).
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover all four state combinations.
- Short `## [Unreleased]` note in `CHANGELOG.md`.

## Files this spec produces

Modified:

- `src/carve/core/state/repository.py` (`mark_plan_applied` only fires on success)
- `src/carve/cli/orchestrator/applier.py` (move the call to the success branch; update error message)
- `tests/cli/orchestrator/test_applier.py`
- `tests/core/state/test_repository.py`
- `CHANGELOG.md`

Post-M1.1-06: same edits in `runner.py` (renamed file) with the new wording.

No new files.

## What this enables

- Smoke-testing M1 stops requiring manual database edits when you hit a Snowflake-permissions issue on the first try.
- The `applied_at` column finally means what its name says: "this plan ran successfully at this time."
- The guard semantics — "block on prior success" — are the right shape for M1.1-06's post-rename `run` command and for M2's prod-PR `apply`.
