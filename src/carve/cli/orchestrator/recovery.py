"""Unified recovery loop wrapping `el run` and the deploy phases (P1-09).

`run_with_recovery` is the single entry point for both
``carve el run`` and ``carve el deploy``'s three failure points. The
caller hands it:

* an :class:`Invocation` describing the trigger context,
* an ``execute`` callable that runs the original operation and
  returns ``(run_id, outcome)``,
* a budget,

and gets back a :class:`RecoveryOutcome` ADT distinguishing
:class:`Recovered`, :class:`Exhausted`, :class:`Refused`, and
:class:`Aborted`.

The loop's responsibilities:

1. Run the operation once. If success → ``Recovered(attempts=0)``.
2. Classify the failure with :func:`classify_failure`. Do-not-fix
   categories bail with ``Refused`` immediately — no LLM call.
3. Up to ``max_attempts``: run :func:`run_recovery_agent`, then re-run
   the operation. Each attempt creates a child Run row linked to the
   parent via ``parent_run_id``.
4. Detect repeated-identical failures (loop guard).
5. Catch ``KeyboardInterrupt`` cleanly → :class:`Aborted`.

The loop never throws. Anything that would surface as an exception
gets folded into the outcome — the caller's job is then to print the
diagnosis and pick an exit code.

Per-context budget independence: callers that need three budgets
(deploy's three phases) call `run_with_recovery` three times, once
per phase. Each call manages its own budget.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from carve.cli.orchestrator.failure_taxonomy import (
    DO_NOT_AUTO_FIX,
    FailureCategory,
    classify_failure,
)
from carve.core.agents.observer import AgentObserver
from carve.core.agents.recovery import (
    Invocation,
    RecoveryAgentError,
    RecoveryAttemptResult,
    run_recovery_agent,
)
from carve.core.state.repository import Repository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outcome ADT
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OutcomeBase:
    attempts: int
    diagnosis: str = ""


@dataclass(frozen=True)
class Recovered(_OutcomeBase):
    """The operation succeeded — possibly after `attempts` retries.

    ``attempts == 0`` is the no-failure happy path.
    """


@dataclass(frozen=True)
class Exhausted(_OutcomeBase):
    """Budget burned without success; ``diagnosis`` carries the last summary."""

    last_category: str = ""


@dataclass(frozen=True)
class Refused(_OutcomeBase):
    """Failure matched a do-not-auto-fix category; no LLM call was made."""

    category: str = ""


@dataclass(frozen=True)
class Aborted(_OutcomeBase):
    """User pressed Ctrl-C mid-recovery."""


RecoveryOutcome = Recovered | Exhausted | Refused | Aborted


# ---------------------------------------------------------------------------
# Execute callable contract
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """What the caller's `execute` returns each invocation.

    ``run_id`` is required so the loop can link the child Run rows to
    their predecessor. ``success`` is the boolean outcome; ``error``
    is the failure text the classifier inspects.
    """

    run_id: str
    success: bool
    error: str = ""


# `execute` takes the `parent_run_id` of the last failed attempt
# (None on the first call) and returns an ExecutionResult. The caller
# is responsible for stamping `parent_run_id` on the new Run row when
# it creates one.
ExecuteFn = Callable[[str | None], ExecutionResult]


# ---------------------------------------------------------------------------
# Recovery loop
# ---------------------------------------------------------------------------


@dataclass
class _AttemptHistory:
    """Per-loop history used for the `repeated_identical` guard."""

    diagnoses: list[str] = field(default_factory=list)


def run_with_recovery(
    invocation: Invocation,
    *,
    execute: ExecuteFn,
    repository: Repository,
    max_attempts: int,
    auto_fix: bool = True,
    client: Any | None = None,
    snowflake_query_runner: Any | None = None,
    snowflake_ddl_executor: Any | None = None,
    observer: AgentObserver | None = None,
) -> RecoveryOutcome:
    """Drive the original operation through up to ``max_attempts`` retries.

    See module docstring for the semantics. ``auto_fix=False`` short-
    circuits: the loop runs the operation once, returns
    ``Recovered(0)`` on success, ``Refused`` on failure (no LLM call).
    """
    try:
        result = execute(None)
    except KeyboardInterrupt:
        return Aborted(attempts=0, diagnosis="interrupted by user")
    if result.success:
        return Recovered(attempts=0)
    if not auto_fix:
        return Refused(
            attempts=0,
            diagnosis=result.error or "recovery disabled (--no-auto-fix)",
            category="user_cancel",
        )

    category = classify_failure(result.error)
    if category in DO_NOT_AUTO_FIX:
        return Refused(
            attempts=0,
            diagnosis=result.error,
            category=str(category.value),
        )

    failed_run_id = result.run_id
    last_error = result.error
    last_diagnosis = result.error
    last_category = str(category.value)
    history = _AttemptHistory()

    for attempt in range(1, max_attempts + 1):
        try:
            attempt_result = _run_one_attempt(
                invocation=_with_failed_run_id(invocation, failed_run_id, last_error),
                repository=repository,
                client=client,
                snowflake_query_runner=snowflake_query_runner,
                snowflake_ddl_executor=snowflake_ddl_executor,
                observer=observer,
            )
        except KeyboardInterrupt:
            return Aborted(
                attempts=attempt - 1,
                diagnosis=last_diagnosis,
            )
        except RecoveryAgentError as exc:
            return Exhausted(
                attempts=attempt,
                diagnosis=str(exc),
                last_category=last_category,
            )

        last_diagnosis = attempt_result.summary or last_diagnosis
        last_category = attempt_result.category or last_category

        if attempt_result.category == FailureCategory.REPEATED_IDENTICAL.value:
            return Exhausted(
                attempts=attempt,
                diagnosis=last_diagnosis,
                last_category=last_category,
            )
        if attempt_result.category in {
            FailureCategory.AUTH.value,
            FailureCategory.PERMISSION.value,
            FailureCategory.RESOURCE_EXHAUSTION.value,
            FailureCategory.OUT_OF_SCOPE.value,
        }:
            return Refused(
                attempts=attempt,
                diagnosis=last_diagnosis,
                category=attempt_result.category,
            )

        if attempt_result.refused or attempt_result.category != FailureCategory.CODE_FIX.value:
            # Agent declined to apply a fix.
            return Exhausted(
                attempts=attempt,
                diagnosis=last_diagnosis,
                last_category=last_category,
            )

        # Loop-detection: if this diagnosis matches the previous one
        # exactly, the agent is spinning.
        normalized = (last_diagnosis or "").strip()
        if normalized and normalized in history.diagnoses:
            return Exhausted(
                attempts=attempt,
                diagnosis=last_diagnosis,
                last_category=FailureCategory.REPEATED_IDENTICAL.value,
            )
        history.diagnoses.append(normalized)

        # Re-run the original operation against the most recent failure.
        try:
            retry = execute(failed_run_id)
        except KeyboardInterrupt:
            return Aborted(
                attempts=attempt,
                diagnosis=last_diagnosis,
            )
        if retry.success:
            return Recovered(
                attempts=attempt,
                diagnosis=last_diagnosis,
            )

        # Loop-detection on the *failure* string, too: identical errors
        # across consecutive retries are a sure sign the fix isn't
        # landing.
        if retry.error and retry.error == last_error:
            return Exhausted(
                attempts=attempt,
                diagnosis=last_diagnosis,
                last_category=FailureCategory.REPEATED_IDENTICAL.value,
            )

        # Bail early if the retry surfaces a do-not-fix failure now.
        retry_category = classify_failure(retry.error)
        if retry_category in DO_NOT_AUTO_FIX:
            return Refused(
                attempts=attempt,
                diagnosis=retry.error,
                category=str(retry_category.value),
            )

        failed_run_id = retry.run_id
        last_error = retry.error

    return Exhausted(
        attempts=max_attempts,
        diagnosis=last_diagnosis,
        last_category=last_category,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _run_one_attempt(
    *,
    invocation: Invocation,
    repository: Repository,
    client: Any | None,
    snowflake_query_runner: Any | None,
    snowflake_ddl_executor: Any | None,
    observer: AgentObserver | None,
) -> RecoveryAttemptResult:
    """Thin wrapper around :func:`run_recovery_agent` for testing isolation."""
    return run_recovery_agent(
        invocation,
        repository=repository,
        client=client,
        snowflake_query_runner=snowflake_query_runner,
        snowflake_ddl_executor=snowflake_ddl_executor,
        observer=observer,
    )


def _with_failed_run_id(
    invocation: Invocation,
    failed_run_id: str,
    error_text: str,
) -> Invocation:
    """Return a copy of ``invocation`` with ``failed_run_id`` / ``error_text`` set.

    The Invocation dataclasses are frozen, so we use
    :func:`dataclasses.replace`. Each branch carries the same two
    fields, so a single ``replace`` per path is enough.
    """
    from dataclasses import replace

    return replace(
        invocation,
        failed_run_id=failed_run_id,
        error_text=error_text,
    )


__all__ = [
    "Aborted",
    "ExecuteFn",
    "ExecutionResult",
    "Exhausted",
    "Recovered",
    "RecoveryOutcome",
    "Refused",
    "run_with_recovery",
]
