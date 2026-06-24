"""The five failure modes + the pipeline-run status derivation.

This module owns the *classification* half of failure handling: given a
step's :class:`StepResult` and the run's live completed/failed/skipped
sets, it mutates those sets per the spec's failure-mode table and records
enough context to derive the final pipeline-run status. The *orchestration*
half of ``retry`` (the attempt loop + backoff) lives where the executor is
launched — :func:`carve.runtime.execute_pipeline.execute_pipeline` — but the
classification of an exhausted retry as a ``fail`` is here.

The table (spec §"Failure modes")::

    Mode             On failure                                  On success
    fail (default)   run failed; don't start unstarted steps     -> completed
    warn             record warning + error; schedule downstream -> completed
    continue         record failure; schedule downstream         -> completed
    retry            exhausted -> treat as fail                  -> completed
    skip_downstream  mark transitive dependents skipped;         -> completed
                     siblings continue

Run status::

    succeeded   all non-skipped steps succeeded
    failed      a step failed under `fail` (or exhausted `retry`)
    partial     completed, but with warn/continue failures or skips
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from carve.core.config.pipeline_schema import PipelineStep
    from carve.runtime.pipeline_dag import PipelineDAG
    from carve.runtime.step_executor import StepResult


RunStatus = Literal["succeeded", "failed", "partial"]


@dataclass
class RunState:
    """The mutable bookkeeping the DAG walk threads through failure modes.

    The three sets are the readiness inputs ``PipelineDAG.ready_steps``
    consumes; the boolean/list fields capture the context needed to derive
    the final run status (a ``fail``/exhausted-``retry`` failure halts the
    run; warn/continue failures and skips degrade ``succeeded`` to
    ``partial``).
    """

    completed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    # True once a `fail`-mode (or exhausted-`retry`) step has failed: the
    # run is doomed and no further unstarted steps should launch.
    halted: bool = False
    # Step ids whose failure was tolerated (warn/continue) — they degrade
    # the run to `partial` without failing it.
    tolerated_failures: list[str] = field(default_factory=list)
    # Warning messages recorded for `warn`-mode failures (surfaced in logs).
    warnings: list[str] = field(default_factory=list)


def apply_failure_mode(
    step: PipelineStep,
    result: StepResult,
    dag: PipelineDAG,
    state: RunState,
) -> None:
    """Fold one step's result into the run state per its failure mode.

    On success the step is added to ``completed`` (every mode behaves the
    same on success). On failure the step's ``failure_mode.mode`` decides
    how the run state mutates. A ``retry``-mode step that reaches here with
    a failing result has already exhausted its attempts (the loop lives in
    ``execute_pipeline``), so it is classified as ``fail``.
    """
    if result.status == "succeeded":
        state.completed.add(step.id)
        return

    if result.status == "skipped":
        # Defensive: a step that arrives already-skipped (e.g. re-applied)
        # just records the skip; it never counts as a failure.
        state.skipped.add(step.id)
        return

    mode = step.failure_mode.mode

    if mode in ("fail", "retry"):
        # `fail`, and `retry` with attempts exhausted: the run fails and no
        # further unstarted steps launch.
        state.failed.add(step.id)
        state.halted = True
        return

    if mode == "warn":
        state.failed.add(step.id)
        state.tolerated_failures.append(step.id)
        message = result.error_message or "(no error message)"
        state.warnings.append(f"step {step.id!r} failed under mode=warn: {message}")
        return

    if mode == "continue":
        state.failed.add(step.id)
        state.tolerated_failures.append(step.id)
        return

    # skip_downstream: the step itself failed; its transitive dependents are
    # skipped, but siblings (steps that don't depend on it) keep running.
    state.failed.add(step.id)
    state.tolerated_failures.append(step.id)
    state.skipped.update(dag.downstream_of(step.id))


def derive_run_status(state: RunState) -> RunStatus:
    """Derive the pipeline-run status from the final run state.

    * ``failed`` — a step failed under ``fail`` (or an exhausted ``retry``),
      i.e. the run halted.
    * ``partial`` — completed, but with tolerated (warn/continue) failures
      or skipped steps.
    * ``succeeded`` — all non-skipped steps succeeded with no tolerated
      failures.
    """
    if state.halted:
        return "failed"
    if state.tolerated_failures or state.skipped:
        return "partial"
    return "succeeded"


__all__ = [
    "RunState",
    "RunStatus",
    "apply_failure_mode",
    "derive_run_status",
]
