# M2-14 — Recovery agent (autonomous fix loop)

**Milestone:** 2 — Real product
**Estimated effort:** 2 days
**Dependencies:** M1.1-04 (progress observer), M1.1-06 (pipeline-centric lifecycle), M2-01 (plan/apply workflow), M2-02 (orchestration agent), M2-03 / M2-04 / M2-05 / M3-05 (specialist agents — needed for the recovery agent to delegate fixes)

## Purpose

When `carve run <pipeline>` fails, today's flow stops at the user. The user reads the traceback, decides what to do, and either hand-edits, replans, or gives up. That's the same friction Claude Code eliminated for general code work: when a build or test fails, the model reads the error, fixes it, and re-runs — usually multiple times — before bothering the human.

Carve should do the same. Hit a failure, an agent reads the script + run logs + plan, diagnoses, picks a remediation strategy, applies it, re-runs. Loops with a budget. If the budget exhausts or the failure category requires human input (e.g. Snowflake permissions), bubble up a diagnosis the user can act on.

The first M1 smoke test landed exactly the kind of bug this should handle: `sodapy` returns nested dicts for Socrata's location columns, and snowflake-connector-python's `executemany` rejects them. A recovery agent reading the error message ("Binding data in type (dict) is not supported") plus the script could JSON-stringify the offending columns and re-run within a turn or two — no human required.

## Scope

### In scope

- A new **recovery agent** with its own system prompt at `src/carve/core/agents/prompts/recovery_agent.md`. Tools: `read_file`, `write_file` (scoped to `pipelines/<pipeline_name>/`), `read_run_logs`, `run_snowflake_query` (read-only), and a `delegate_to_specialist` tool that hands off to one of the M2 specialist agents (orchestrator, dbt, snowflake, quality) for larger redesigns.
- A failure-classification step that decides between **patch-and-retry** (small code fix; recovery agent handles directly) and **replan-and-rebuild** (larger redesign; recovery agent invokes the orchestrator to refine the plan, then `carve build` is rerun, then `carve run` is rerun).
- A bounded fix loop: configurable `max_fix_attempts` (default 3) and `fix_cost_cap_usd` (default $1.00 per run). Both come from `carve/runner.toml`. CLI overrides: `--max-fix-attempts N` and `--no-auto-fix`.
- Per-attempt visibility via the existing `AgentObserver` protocol from M1.1-04. Each attempt shows its diagnosis, action, and outcome.
- A clean stop-signal model: Ctrl-C interrupts the recovery agent without leaving the database in a broken state.
- A categorical "do not auto-fix" list — failures where automated retry is wrong or wasteful:
  - Authentication failures (`401`, `Invalid OAuth token`, `Authentication failed`).
  - Authorization / permission failures (`SQL access control error`, `Insufficient privileges`, `403`).
  - Resource-exhaustion failures (`out of memory`, warehouse-suspended, account-locked).
  - User-cancellation (the user hit Ctrl-C — never auto-retry).
  - The same failure recurring on consecutive attempts with no progress (loop-detection escape).
- A run-completion summary that distinguishes:
  - **Recovered**: failed N times, fixed, succeeded.
  - **Exhausted**: hit budget, surfacing best-attempt diagnosis.
  - **Refused**: matched a do-not-fix category, bubbled immediately.
  - **Aborted**: user interrupted.
- Persistence on the `runs` table: a new `parent_run_id` column linking auto-fix attempts to the original failed run. `carve runs --pipeline <name>` shows the chain.
- A new `carve runs <run_id> --recovery` view that renders the fix loop as a tree (original failure → attempt 1 diagnosis + action → attempt 2 ... → outcome).

### Out of scope

- **Multi-step pipeline recovery (M3-01).** When pipelines have multiple steps, recovery needs to know which step failed and only re-run from there. M3 — flag in this spec that the fix-loop's interface should already accept "step that failed" as input, even if M2 only ever has single-step pipelines.
- **Schema-drift recovery.** When the source data changes shape (column added/dropped) — that's specialist territory; the recovery agent delegates rather than handling directly.
- **Cost-aware recovery escalation.** "Use Haiku for the first fix attempt, Sonnet for the second, Opus for the third." Future optimization.
- **Cross-run learning.** "This pipeline failed the same way last week; here's what worked." A `recovery_attempts` table is reasonable but training-on-history is M3+.
- **Recovery for `carve build` failures.** Build failures are different (syntax errors in agent-generated code, missing imports, etc.) — they're rare in practice because the build agent generates code that ran in the agent's mind, but a separate spec can address them later.
- **External-action recovery.** "Snowflake says we need to grant CREATE TABLE on the schema." Recovery agent can detect and *describe* this case; it does not run `GRANT` statements.
- **Production recovery (M2 `carve apply`).** Auto-fix in dev is fine; auto-fix in prod with side-effecting writes is risky. `carve apply` always requires human review. Document the asymmetry.

## Architecture

### Loop shape

```
def run_with_recovery(pipeline, config, max_attempts):
    original_run_id = execute_run(pipeline, parent_run_id=None)

    if status_of(original_run_id) == "success":
        return Recovered(attempts=0)

    for attempt in 1..max_attempts:
        category = classify_failure(get_run(original_run_id))
        if category in DO_NOT_AUTO_FIX:
            return Refused(category, original_run_id)

        diagnosis = recovery_agent.diagnose(
            pipeline=pipeline,
            failed_run_id=original_run_id if attempt == 1 else previous_attempt_id,
            prior_attempts=attempts_so_far,
            cost_remaining=cost_cap - spent,
        )

        if diagnosis.action == "patch":
            recovery_agent.apply_patch(diagnosis.patch)
        elif diagnosis.action == "replan":
            new_plan_id = orchestrator.refine_plan(
                pipeline=pipeline,
                feedback=diagnosis.feedback,
            )
            build_plan(new_plan_id, ...)

        attempt_run_id = execute_run(pipeline, parent_run_id=original_run_id)
        attempts_so_far.append(...)

        if status_of(attempt_run_id) == "success":
            return Recovered(attempts=attempt)
        if no_progress(attempts_so_far):
            return Exhausted(reason="no progress between attempts", ...)

    return Exhausted(reason="budget exhausted", ...)
```

This lives in `src/carve/cli/orchestrator/recovery.py`. The runner stays simple — it just executes one Run; the recovery wrapper is the new layer.

### Recovery-agent prompt

`src/carve/core/agents/prompts/recovery_agent.md`. Key contents:

- **Role:** "You are Carve's recovery agent. A pipeline run just failed. Your job is to figure out why and fix it — preferably with a small patch to the script, falling back to a full replan if the failure is structural."
- **Inputs you have:** the pipeline's `main.py` and `requirements.txt`; the failed run's logs (via `read_run_logs`); the design that was built; the connection context.
- **Output contract:** call exactly one of:
  - `apply_patch(file, patch_or_full_content, rationale)` — small code change. The script is rewritten; rebuild is skipped (the existing build is reused).
  - `request_replan(feedback)` — larger redesign needed. Returns control to the orchestrator with a feedback string.
  - `give_up(reason, suggested_user_action)` — recovery agent thinks this needs human input.
- **Guidelines:**
  - Read the actual error before diagnosing — the model often gets this wrong if it pattern-matches on the function name.
  - Prefer the smallest change that addresses the error; don't refactor.
  - Do not change destination tables, column names, or transformation strategy without `request_replan`. Those are design decisions.
  - Do not loop on the same fix; if the prior attempt's patch didn't work, try something different.
  - When in doubt, `give_up` rather than burn the budget on guesses.

### Failure classification

`src/carve/cli/orchestrator/failure_taxonomy.py`:

```python
class FailureCategory(StrEnum):
    AUTH = "auth"                    # do-not-auto-fix
    PERMISSION = "permission"        # do-not-auto-fix
    RESOURCE = "resource"            # do-not-auto-fix
    USER_CANCELLED = "user_cancelled" # do-not-auto-fix
    CODE_ERROR = "code_error"        # auto-fix candidate
    DATA_SHAPE = "data_shape"        # auto-fix candidate (often a small patch)
    DEPENDENCY = "dependency"        # auto-fix: pin a different version, etc.
    NETWORK = "network"              # retry with backoff, then auto-fix or give up
    UNKNOWN = "unknown"              # fall through to recovery agent's read
```

A small `classify(run: Run) -> FailureCategory` reads the last few hundred lines of logs and the error_message column and pattern-matches. Imperfect by design — the recovery agent has the LLM-class judgment; classification just gates the do-not-fix shortcuts.

### `runs` table changes

- Add `parent_run_id: TEXT NULL`, FK to `runs.id`. Set on auto-fix attempts.
- Add `recovery_attempt: INT NULL`. Counts within a chain (1, 2, 3, ...). Null on the original failure.
- Add `recovery_outcome: TEXT NULL`. One of `recovered | exhausted | refused | aborted | null` (null on attempts that haven't terminated the chain).

Migration `0003_recovery_metadata.py`.

### CLI surface

```
carve run <pipeline_name>                        # auto-fix on by default
carve run <pipeline_name> --no-auto-fix          # one-shot, original behavior
carve run <pipeline_name> --max-fix-attempts 5
carve runs --pipeline <name>                     # shows chains; auto-fix attempts indented under parent
carve runs <run_id> --recovery                   # tree view of one chain
```

Output during a recovery run uses M1.1-04's observer protocol so the user sees each agent invocation and outcome live:

```
✗ Run failed (11.2s) — script exited with code 1
  → Binding data in type (dict) is not supported.
🔧 Auto-fix attempt 1/3:
  → reading run logs and pipelines/iowa_liquor_sales/main.py...
  → diagnosis: sodapy returns dicts for location columns; insert binds dicts
  → patch: stringify dict values via json.dumps before binding
  → re-running pipeline...
✓ Recovered after 1 attempt (28.4s total, $0.04 in fix-loop tokens)
  Inserted 10000 rows.
```

### Configuration

`carve/runner.toml` gains:

```toml
[runner.auto_fix]
enabled = true
max_attempts = 3
cost_cap_usd = 1.00
```

`Config.runner.auto_fix` becomes a sub-model.

## Tests

`tests/cli/orchestrator/test_recovery.py` (new):

- `test_recovery_succeeds_on_first_patch` — mocked recovery agent emits `apply_patch`; second run succeeds; chain is `[failed → succeeded]`, outcome `recovered`.
- `test_recovery_replan_path_invokes_orchestrator` — recovery agent emits `request_replan`; the orchestrator's `refine_plan` is called, build runs, run re-runs; outcome `recovered`.
- `test_recovery_exhausts_budget` — three patches all fail; outcome `exhausted`, original failure surfaced.
- `test_recovery_refuses_permission_failures` — failed run with `Insufficient privileges`; classifier hits `PERMISSION`; no recovery attempt; outcome `refused`.
- `test_recovery_refuses_repeated_identical_failure` — two attempts produce the exact same error; loop-detection trips.
- `test_recovery_user_cancellation_aborts_chain` — Ctrl-C during a recovery attempt; chain marked `aborted`; database state is consistent.
- `test_recovery_off_via_flag` — `--no-auto-fix` skips the wrapper entirely.
- `test_recovery_off_via_config` — `[runner.auto_fix] enabled = false` same effect.
- `test_recovery_observer_emits_attempt_events` — the observer sees the per-attempt lifecycle events.

`tests/cli/orchestrator/test_failure_taxonomy.py`:
- One parametrized test per category: log fragment in, expected category out. Cover the documented patterns plus a few near-miss ones.

`tests/core/state/test_repository.py`:
- New columns roundtrip; chain-walk via `parent_run_id`; recovery_outcome CHECK if you constrain it.

`tests/migrations/test_migrations.py`:
- 0003 migration adds the columns and is idempotent.

`tests/core/agents/test_recovery_agent.py`:
- Mocked client emits `apply_patch` → tool fires correctly.
- Mocked client emits `request_replan` → returns the feedback string.
- Mocked client emits `give_up` → terminates the loop with the documented outcome.

## Acceptance criteria

- A pipeline that fails with the dict-binding error from the M1 smoke test is automatically recovered within 1–2 attempts and a few seconds of agent work.
- A pipeline that fails with `Insufficient privileges` does *not* trigger a fix loop; it surfaces the actionable diagnosis ("Your role lacks CREATE on schema X. Run: GRANT CREATE TABLE ON SCHEMA X TO ROLE Y") immediately.
- The fix loop is bounded by both attempts and dollars; the user can adjust both via config or flags.
- Live progress is shown via the same observer pattern M1.1-04 established.
- The `runs` table records the chain so post-hoc audit is possible.
- `--no-auto-fix` exists for users who want to inspect failures themselves.
- `ruff` + `mypy --strict` + full `pytest` stay green.

## Files this spec produces

New:
- `src/carve/cli/orchestrator/recovery.py`
- `src/carve/cli/orchestrator/failure_taxonomy.py`
- `src/carve/core/agents/prompts/recovery_agent.md`
- `migrations/versions/0003_recovery_metadata.py`
- `tests/cli/orchestrator/test_recovery.py`
- `tests/cli/orchestrator/test_failure_taxonomy.py`
- `tests/core/agents/test_recovery_agent.py`

Modified:
- `src/carve/cli/commands/run.py` (auto-fix on by default, `--no-auto-fix` and `--max-fix-attempts` flags)
- `src/carve/cli/commands/runs.py` (`--recovery` tree view)
- `src/carve/cli/orchestrator/runner.py` (delegate to `recovery.run_with_recovery` when auto_fix.enabled)
- `src/carve/cli/orchestrator/listing.py` (chain rendering)
- `src/carve/core/state/models.py` (Run column adds)
- `src/carve/core/state/repository.py` (chain-walk helper, attempt insert)
- `src/carve/core/config/schema.py` (`RunnerConfig.auto_fix` sub-model)
- `src/carve/core/agents/__init__.py` (export recovery agent factories)
- `tests/core/state/test_repository.py`
- `tests/migrations/test_migrations.py`
- `README.md` (auto-fix section in walkthrough)
- `CHANGELOG.md`

## What this enables

- The user-visible jump from M1.1's "Carve generates and runs" to M2's "Carve generates, runs, and self-corrects." This is the moment Carve starts feeling like Claude Code applied to data.
- Specialist agents (M2-02 / M2-03 / M2-04 / M3-05) get a coordination layer at runtime, not just at plan time.
- The recovery wrapper's per-attempt persistence becomes the foundation for the M2 web UI's "Pipeline Monitor" view (M2-12) — chain rendering plus live status.
- M3-01's multi-step pipelines plug into the same wrapper; the recovery agent learns to scope fixes to the failed step.
