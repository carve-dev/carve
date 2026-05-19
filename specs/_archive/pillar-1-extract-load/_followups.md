# Pillar 1 — deferred follow-ups

Items deferred during Pillar 1 implementation. None block `v0.1.0`; each is tracked here so they don't get lost.

## delegate_to_specialist tool (P1-09)

**Status:** deferred
**From:** P1-09 build, iter0 engineer judgment call (kept through iter2)

The recovery agent's spec (`09-recovery-agent.md` §"Four trigger contexts", line 18) lists `delegate_to_specialist` as an available tool in context #1 (`carve el run` failure). It would let the recovery agent re-invoke `run_extract_load_agent` to regenerate code from a refined plan when the failure indicates the design itself was wrong (not just a code bug).

The engineer punted because invoking `run_extract_load_agent` mid-attempt requires synthesizing a `Task` from the failed run's pipeline state, which is a separate design problem (which design fields to mutate? how to thread plan refinement through?).

**Today's coverage without it:** the recovery agent's `write_file` tool covers most fix paths (the dict-binding regression, missing imports, type coercion, statement-ordering DDL bugs). Design-level failures surface as `repeated_identical` after 1-2 attempts and exit with a clear diagnosis pointing at `carve plan --refine`.

**When to land:** if real users hit walls where `repeated_identical` triggers on design-level issues that `delegate_to_specialist` would have caught. Track via telemetry from recovery attempt outcomes.

**Estimated effort:** ~0.5 day. Add a `_synthesize_task_from_failed_run(run, plan)` helper, register the tool, update the system prompt.

## Full unification of deploy recovery loops (P1-09)

**Status:** deferred (partial fix landed)
**From:** P1-09 build, iter2

The unified `run_with_recovery` orchestrator (in `carve.cli.orchestrator.recovery`) is used for `el run` failures. Deploy's three-phase recovery uses inline shims (`_apply_ddl_with_recovery`, `_verify_with_recovery`) that call `LLMRecoveryHandler.attempt(...)` directly.

The user-visible gap (recovery attempts during deploy didn't show up in `carve runs <id> --recovery`) was closed by commit `2de1f63` — `_maybe_recover` now persists child `Run` rows linked via `parent_run_id`, and the recovery tree renders correctly across both code paths.

What's still outstanding: the structural duplication (per-phase retry loop + idempotent re-apply logic in two places). Folding the shims into `run_with_recovery` would let deploy share the classify-failure-before-LLM logic, the `RepeatedIdentical` detection, and the four `RecoveryOutcome` types.

**When to land:** when the next deploy-related spec (Pillar 3 pipelines? Pillar 4 schedules?) needs a third recovery loop and the duplication starts to bite.

**Estimated effort:** ~1 day. The deploy phases need their own `Invocation` builder and `execute(parent_run_id) -> ExecutionResult` callable. P1-08's 38 deploy tests need their fixtures updated.

## `_LAST_RUN_ID` module slot (P1-07/P1-09)

**Status:** deferred
**From:** P1-07 implementation, flagged again in P1-09 python review

`src/carve/cli/orchestrator/runner.py` carries a module-level `_LAST_RUN_ID: ContextVar[str | None]` slot used to hand the run id from `_run_pipeline_dir` to `_run_pipeline_with_recovery`. The engineer chose this over refactoring `_run_pipeline_dir` to return a tuple because the latter would have churned P1-07 tests.

Single-threaded by construction; not racy in practice. Python-reviewer flagged it as "hazard under future async expansion."

**When to land:** when the runner gets touched for another reason (e.g., concurrent runs in M3, async expansion). Until then it's pure code smell with no user-visible impact.

**Estimated effort:** ~2 hours. Refactor `_run_pipeline_dir` to return `tuple[int, str | None]` (or accept an output collector). Update P1-07 tests.
