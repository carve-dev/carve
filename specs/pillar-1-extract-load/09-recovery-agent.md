# P1-09 — Recovery agent

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1.5 days
**Dependencies:** M1.1-04 (progress observer), P1-02 (plan/build lifecycle), P1-04 (extract-load agent), P1-07 (`carve el run`), P1-08 (`carve el deploy`)
**Lineage:** Carries forward from **M2-15** ([`specs/milestone-2-real-product/15-recovery-agent.md`](../milestone-2-real-product/15-recovery-agent.md)) which evolved continuously during M2 review (see the "Two trigger contexts" section added during the SDLC discussion). The system prompt, do-not-auto-fix categories, bounded-budget config, `parent_run_id` linking, and `AgentObserver` integration from M1.1-04 all carry forward unchanged. **Scope expansion vs M2-15:** Pillar 1's recovery agent operates in **four trigger contexts** — `carve el run` failures plus three contexts inside `carve el deploy` (pre-flight drift, DDL-apply failures, post-DDL verify failures). The "Phase 1 only" framing in M2-15 was tightened during P1-08's review; AI-editing-DDL-and-retrying is safe because the DDL contract (P1-06) is idempotent.

## Purpose

Auto-fix loop for failures across Pillar 1's two operational verbs (`carve el run`, `carve el deploy`). When a failure occurs in one of the supported trigger contexts, the recovery agent reads the failure context, diagnoses the cause, applies a fix (either to the source files in the working tree or directly via Snowflake DDL where appropriate), and retries — all within a bounded budget. If the budget exhausts or the failure category is in the do-not-fix list, surface a clear diagnosis and let the user act manually.

The Pillar 1 ambition: most transient, fixable issues self-resolve without user intervention. Users see the recovery agent's attempts as nested `Run` rows in `carve runs --pipeline <name>`, with diagnosis + action + outcome per attempt.

## Four trigger contexts

| # | Triggered by | Side-effect risk | Recovery agent's tool set |
|---|---|---|---|
| 1 | `carve el run <name>` script failure | Dev-target writes (already attempted by the script; partial state may exist) | `read_file`, `write_file` (scoped to `targets/<active>/el/<name>/`), `read_run_logs`, `run_snowflake_query` (read-only against active target), `delegate_to_specialist` (extract-load), `request_replan` |
| 2 | `carve el deploy` Phase 1 (pre-flight drift) | None — read-only validation | Same as #1, plus `read_file` on the DDL file |
| 3 | `carve el deploy` Phase 2 (DDL-apply statement failure) | Partial DDL applied to dest target | Same as #2, plus `write_file` on the DDL file (`targets/<dest>/snowflake/<name>.sql`); plus `run_snowflake_ddl(stmt)` against the dest target via the deploy role |
| 4 | `carve el deploy` Phase 3 (post-DDL smoke verify) | DDL fully applied; verifying state | Same as #3 |

The agent's prompt receives the trigger context as a structured preamble. The agent decides which tools to use based on the failure shape.

### Why context #3 (DDL apply) involves writes

The earlier framing was "recovery only in read-only contexts." User feedback (P1-08 review) extended this: **DDL is idempotent by P1-06's contract**, so the recovery agent editing the DDL file in the working tree and re-applying is safe — re-running the same DDL is a no-op for already-applied statements (`IF NOT EXISTS`, `GRANT`, etc.). The agent's edits land in the user's working tree (visible via `git status`) and Snowflake account. Failures during recovery surface through the same budget-exhaust path.

### Connection role used by recovery

The recovery agent uses **the role of the operation being recovered** — same connection, same privileges as the original failing call. No elevated role:

- Context #1 (`carve el run` failure) → runtime role (`[snowflake.<target>]`).
- Context #2 (deploy Phase 1 drift) → deploy role (`[snowflake.<target>_deploy]`).
- Context #3 (DDL apply failure) → deploy role.
- Context #4 (post-DDL verify) → runtime role (matching what `verify` itself uses).

This keeps recovery within the privilege envelope the user already accepted for the original operation. Recovery can't escalate; if a failure is "this needs a more privileged role," recovery surfaces that diagnosis and the user runs the suggested SQL manually.

## Bounded budget

- **`max_fix_attempts`** — default 3 per failure event. Configurable via `carve/runner.toml` (`[runner.auto_fix] max_attempts = 3`).
- CLI overrides per invocation: `--max-fix-attempts N`, `--no-auto-fix`.

The budget is **per failure event**, not per command. A single `carve el deploy` invocation can hit pre-flight drift (recovery), then DDL apply failure (recovery), then verify failure (recovery) — three separate budget pools, each capped at the same `max_fix_attempts`. Users wanting tighter limits lower the config; users wanting to disable recovery entirely pass `--no-auto-fix`.

Cost-cap-by-dollar (which existed in the M2-15 draft) is **dropped** for v0.1 — attempts-only is the simpler control. A user concerned about runaway LLM cost can set `max_attempts = 1` or `--no-auto-fix`. We add cost-aware budgeting later if real users hit walls (per-attempt LLM cost is small in practice — typical recovery is 1-3 attempts of Sonnet at sub-$0.10 each).

A run-completion summary (printed at end of the triggering command) reports per-context attempts used:

```
Recovery summary:
  Phase 1 (pre-flight drift):  Recovered (1 attempt)
  Phase 2 (DDL apply):          Recovered (2 attempts)
  Phase 3 (smoke verify):       Skipped (no failure)
```

## Do-not-auto-fix categories

Failures the recovery agent surfaces immediately without attempting fix:

- **Authentication failures** (`401`, `Invalid OAuth token`, `Authentication failed`) — wrong credentials; user must fix `.env` or the Snowflake user/password.
- **Authorization / permission failures** (`SQL access control error`, `Insufficient privileges`, `403`) — agent could in theory grant privileges, but role hierarchy changes are out of Pillar 1's scope (Pillar 2's broader Snowflake agent will handle role management). Surface "GRANT … needed" diagnosis; user runs the SQL.
- **Resource exhaustion** (`out of memory`, warehouse-suspended, account-locked, network unreachable) — non-deterministic; auto-retry pointless.
- **User-cancellation** (Ctrl-C → `KeyboardInterrupt` → never auto-retry).
- **Repeated identical failure** on consecutive attempts — loop-detection escape. The agent's diagnosis hasn't changed between attempts, so further attempts won't help.
- **Out-of-scope tasks** — when the agent's `delegate_to_specialist` returns `submit_step(error=True)`, the failure is fundamentally outside Pillar 1's scope (e.g., the user's pipeline references a dbt model — Pillar 2 territory).

## Run-state persistence

Each recovery attempt creates a child `Run` row linked to the original failed Run via `parent_run_id` (column added in M2-15's schema work; carries forward unchanged). The chain is reachable via:

```sql
SELECT * FROM runs
WHERE parent_run_id = <failed_run_id>
ORDER BY created_at;
```

`carve runs <run_id> --recovery` (a sibling to the existing M1.1-06 `carve runs` listing) renders the chain as a tree:

```
run_a3f29 (failed, kind=run, target=dev)
├─ recovery attempt 1: diagnosis="binding dict in column LOCATION"
│  └─ action: edited targets/dev/el/iowa_liquor/main.py to json.dumps the field
│  └─ retry: run_b7c12 (success)
└─ recovery summary: Recovered (1 attempt)
```

Run-completion summaries distinguish four outcomes (carries from M2-15):

- **Recovered** — failed N times, fixed, succeeded
- **Exhausted** — hit budget, surfacing best-attempt diagnosis
- **Refused** — matched a do-not-fix category, bubbled immediately
- **Aborted** — user interrupted

## Loop shape

```python
def run_with_recovery(
    invocation: Invocation,  # encapsulates the triggering command + context
    config: Config,
    max_attempts: int,
) -> RecoveryOutcome:
    failed_run_id = execute(invocation, parent_run_id=None)

    if status_of(failed_run_id) == "success":
        return Recovered(attempts=0)

    for attempt in range(1, max_attempts + 1):
        category = classify_failure(get_run(failed_run_id))
        if category in DO_NOT_AUTO_FIX:
            return Refused(category, failed_run_id)

        if budget_exceeded(invocation, attempt):
            return Exhausted(attempt - 1, last_diagnosis(failed_run_id))

        diagnosis = recovery_agent.diagnose(
            invocation=invocation,
            failed_run_id=failed_run_id,
            previous_attempts=load_attempts(failed_run_id),
        )
        result = recovery_agent.act(
            invocation=invocation,
            diagnosis=diagnosis,
        )
        if result.outcome == "give_up":
            return Exhausted(attempt, diagnosis)

        # Retry the original invocation; create a child Run linked via parent_run_id
        retry_run_id = execute(invocation, parent_run_id=failed_run_id)
        if status_of(retry_run_id) == "success":
            return Recovered(attempt)
        failed_run_id = retry_run_id  # next attempt's "failed" is this one

    return Exhausted(max_attempts, last_diagnosis(failed_run_id))
```

Lives in `src/carve/cli/orchestrator/recovery.py`. Wraps the runner (`carve el run`'s execution) and the deploy orchestrator (`carve el deploy`'s phases). Each context provides its own `Invocation` shape; the recovery agent dispatches on it.

## System prompt structure

`src/carve/core/agents/prompts/recovery_agent.md` (carries from M2-15 verbatim except the trigger-context preamble):

1. **Role.** "You are Carve's recovery agent. When a Pillar 1 command (`carve el run` or `carve el deploy`) fails, you read the failure, diagnose it, apply a fix, and retry. Bounded by a max-attempts budget."
2. **Trigger-context preamble.** One of:
   - "`carve el run <name>` failed at runtime. Failure logs and the script are below."
   - "`carve el deploy <name> --from X --to Y` Phase 1 (pre-flight drift) detected drift. Drift report below."
   - "`carve el deploy <name> --from X --to Y` Phase 2 (DDL apply) failed at statement <N>. Failing SQL and Snowflake error below."
   - "`carve el deploy <name> --from X --to Y` Phase 3 (verify) failed. Verification diff below."
3. **Diagnosis rules.** Enumerate the do-not-auto-fix categories explicitly; instruct the agent to bail with `request_human` when it sees them.
4. **Available actions.** Describe the tools (varies by trigger context per the table above).
5. **Hard rules.** Don't loop on identical failures; surface real-world side effects (e.g., "I'm about to apply DDL against prod") so the user sees them in `carve runs --recovery`; respect the budget.

## Implementation

### File-level changes

New files:

- `src/carve/cli/orchestrator/recovery.py` — `run_with_recovery`, classifier, budget tracker, dispatch.
- `src/carve/cli/orchestrator/failure_taxonomy.py` — `classify_failure(error_text) -> Category`. Pattern matching on error messages.
- `src/carve/core/agents/prompts/recovery_agent.md` — system prompt.
- `src/carve/core/agents/recovery/__init__.py`
- `src/carve/core/agents/recovery/agent.py` — agent module.
- `src/carve/core/agents/recovery/invocation.py` — `Invocation` dataclasses for each trigger context.
- `tests/cli/orchestrator/test_recovery.py`
- `tests/cli/orchestrator/test_failure_taxonomy.py`

Modified files:

- `src/carve/cli/orchestrator/runner.py` (P1-07) — wraps execution in `run_with_recovery` when `[runner.auto_fix] enabled = true` (default).
- `src/carve/cli/commands/el/deploy.py` (P1-08) — wraps each of three phase-failure points in `run_with_recovery`.
- `src/carve/cli/commands/runs.py` — `--recovery` flag for the recovery-chain tree view.
- `src/carve/core/state/models.py` — `Run.parent_run_id: str | None` column (FK to runs.id). Migration `0005_recovery_chains.py`.
- `tests/cli/commands/test_runs.py` — `--recovery` rendering.

DB migration `0005_recovery_chains.py`:

1. Add `parent_run_id` TEXT column to `runs`, FK to `runs.id`, default NULL.
2. Add index on `(parent_run_id)` for the lookup pattern.

## Tests

- `test_classify_failure_dict_binding_pattern` — Iowa-liquor `dict`-binding failure → category `code_fix`.
- `test_classify_failure_auth_pattern` — `Authentication failed` → category `auth` (do-not-auto-fix).
- `test_classify_failure_permission_pattern` — `Insufficient privileges` → category `permission` (do-not-auto-fix).
- `test_recovery_run_failure_recovered` — fixture: script fails with dict-binding bug; recovery agent edits the script; retry succeeds. Run chain has 1 child run.
- `test_recovery_deploy_phase1_drift_recovered` — fixture: drift detected; recovery agent edits the DDL; retry succeeds.
- `test_recovery_deploy_phase2_ddl_failure_recovered` — fixture: DDL statement 3 of 5 fails; recovery agent edits the DDL; partial-failure-aware retry succeeds.
- `test_recovery_deploy_phase3_verify_failure_recovered` — fixture: missing grant detected; recovery agent appends GRANT to DDL file; retry against deploy role succeeds.
- `test_recovery_budget_exhausted` — agent's first attempt fails; second fails; third fails → `Exhausted` outcome with last diagnosis.
- `test_recovery_refuses_auth_failure` — auth failure → `Refused` outcome immediately, no LLM call.
- `test_recovery_refuses_repeated_identical_failure` — two attempts produce the same error → loop-detection trips → `Exhausted`.
- `test_recovery_aborted_on_ctrl_c` — Ctrl-C mid-recovery → `Aborted`, no orphaned state.
- `test_recovery_no_auto_fix_flag` — `--no-auto-fix` skips the recovery loop entirely; failures exit immediately.
- `test_recovery_chain_persisted_via_parent_run_id` — child runs link to parent via `parent_run_id`; `runs --recovery` renders the tree.
- `test_recovery_per_context_budgets_independent` — single deploy invocation hits drift (1 attempt) + DDL fail (2 attempts) + verify fail (1 attempt) → all three contexts get their own budget pools.

## Acceptance criteria

- Recovery agent operates in all four trigger contexts: `el run` failure, deploy Phase 1, Phase 2, Phase 3.
- Bounded by `max_fix_attempts` per failure event; configurable via `runner.toml`. Cost-cap-by-dollar is explicitly dropped for v0.1; attempts-only is the simpler control.
- Do-not-auto-fix categories surface immediately without LLM call.
- Each recovery attempt creates a child `Run` linked via `parent_run_id`; `carve runs <id> --recovery` renders the tree.
- Run-completion summary distinguishes Recovered / Exhausted / Refused / Aborted outcomes per context.
- `--no-auto-fix` flag skips the recovery loop entirely.
- The Iowa-liquor `dict`-binding regression is auto-recovered within 1-2 attempts (smoke-test acceptance criterion).
- DDL-apply failures during deploy Phase 2 are auto-recoverable when the failure is fixable (missing pre-req, statement ordering); the agent edits the DDL file in the working tree and retries from the failing statement.
- `ruff` + `mypy --strict` + `pytest` stay green.

## Files this spec produces

(Summary of File-level changes section.)

New: `recovery.py`, `failure_taxonomy.py`, recovery agent module + prompt + invocation dataclasses, 2 test files.
Modified: `cli/orchestrator/runner.py`, `cli/commands/el/deploy.py`, `cli/commands/runs.py`, `core/state/models.py` (parent_run_id column).
DB migration `0005_recovery_chains.py` adds `runs.parent_run_id` + index.

## Out of scope

- **Auto-fix in CI** (recovery agent running inside the user's GitHub Actions / Airflow / etc. without local Anthropic credentials). High complexity (CI auth, secret management, the agent making decisions in an unattended context). Defer to M3+ once we have telemetry on need.
- **Multi-step pipeline recovery** (Pillar 3's pipelines have multiple steps; recovery needs to know which step failed and re-run from there). Pillar 3 — flag in this spec that the fix-loop's interface should already accept "step that failed" as input, even if Pillar 1 only ever has single-step EL artifacts.
- **Schema-drift recovery beyond the documented categories.** When the source data changes shape (column added/dropped at the SOURCE), Pillar 1 surfaces a clear diagnosis but doesn't auto-fix the upstream. User refines the plan and rebuilds.
- **Cost-aware recovery escalation** ("use Haiku for the first fix attempt, Sonnet for the second, Opus for the third"). Future optimization.
- **Cross-run learning** ("this artifact failed the same way last week; here's what worked"). A `recovery_attempts` table is reasonable but training-on-history is M3+.
- **Recovery for `carve build` failures.** Build failures are rare in practice (the build agent's output ran in its own conversation context); a separate spec can address them later.
- **External-action recovery.** "Snowflake says we need to grant CREATE TABLE on the schema." Recovery agent can detect and *describe* this case; it does not run `GRANT` statements that change role hierarchies (out of Pillar 1's scope; Pillar 2's broader Snowflake agent owns role management).
- **Reviewer-comment-driven autonomous fixes** ("the PR reviewer asked for an index; let me push that"). Far future.

## What this enables

- **Most fixable failures self-resolve.** The Iowa-liquor smoke-test failure mode (which kicked off the M2-14 / recovery discussion in the first place) is auto-recovered without user intervention. Same for missing-pre-req DDL failures, transient verify drift, and the long tail of "AI-generated SQL had a typo."
- **Failed runs / deploys aren't dead-ends.** The recovery loop keeps Pillar 1 viable for users running deploys from CI without an attentive human watching every output.
- **The chain of attempts is preserved and visible.** Users debugging "why did this take three tries last week?" run `carve runs <id> --recovery` and see exactly what the agent did.
- **The pattern extends.** Pillar 2's dbt-build failures, Pillar 3's pipeline-step failures, Pillar 4's schedule failures all plug into the same recovery loop with their own `Invocation` shape and trigger-context preamble.
