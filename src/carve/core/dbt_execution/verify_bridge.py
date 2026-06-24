"""Adapt the runnable dbt backend's structured result into the harness verify loop.

The shipped :mod:`carve.integrations.dbt.runner` bridges the *bash-tool +
``run_results.json``* path — it was built before a runnable backend existed, so
it re-derives the verdict by shelling ``dbt`` through the gated ``bash`` tool and
re-reading the on-disk artifact. Now
:class:`carve.core.dbt_execution.local.LocalDbtBackend` is the **canonical,
concurrency-correct, secret-stripped** run path: ``backend.run(command)`` already
runs the subprocess and normalizes the on-disk artifacts into a uniform
:class:`~carve.core.dbt_execution.result.DbtRunResult`.

This module is the missing connective tissue between that structured result and
the harness verification loop:

* :func:`dbt_run_result_to_check_result` adapts a :class:`DbtRunResult` into a
  :class:`~carve.core.agents.verification.CheckResult` — the same
  ``passed``/``summary``/``details`` shape :func:`parse_dbt_run` builds, but
  consuming the *already-normalized* result rather than re-parsing
  ``run_results.json``. The fail-closed verdict is **inherited**: a
  ``status == "error"`` run (no readable artifact) is ``passed=False``.
* :func:`make_dbt_backend_verification_loop` composes a bounded generate → run →
  read → fix loop that drives the *backend* (not a gated bash command). It is the
  structured analog of :func:`carve.integrations.dbt.runner.make_dbt_verification_loop`.

The **backend path is now preferred** for the agent's live verify: it runs
through :class:`LocalDbtBackend` (one subprocess, scrubbed env, own process
group) rather than re-shelling ``dbt``. The bash/shelled
:func:`~carve.integrations.dbt.runner.make_dbt_verification_loop` remains valid
for recovery/legacy callers and is **not** removed.

The agent does **not** import this directly; the orchestrator composes
``LocalDbtBackend`` + this bridge at live-injection time (the deferred wiring).
This module builds + tests the bridge so the vertical is provably correct in
isolation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from carve.core.agents.verification import (
    MAX_VERIFICATION_ITERATIONS,
    CheckResult,
    VerificationOutcome,
)
from carve.core.dbt_execution.backend import DbtBackend, DbtCommand
from carve.core.dbt_execution.result import (
    STATUS_ERROR,
    STATUS_SUCCESS,
    DbtRunResult,
    PerModelResult,
)
from carve.integrations.dbt.verify import error_summary

# The trailing slice of ``result.logs`` threaded into ``details["logs_tail"]`` on
# a non-green return — same size convention as the bash path's ``_tail`` (1500
# chars) so both verify paths surface a comparably-sized diagnostic tail.
_LOG_TAIL_LIMIT = 1500

# A fix step is the agent action between backend runs — it returns True if it
# attempted a fix (so the loop should re-run the backend) and False if it gave
# up. Mirrors `carve.core.agents.verification.FixStep`, but keyed to the
# backend-driven loop here (no `cmd`/`bash_tool` — the backend owns execution).
FixStep = Callable[[CheckResult], bool]


def dbt_run_result_to_check_result(result: DbtRunResult) -> CheckResult:
    """Adapt a structured :class:`DbtRunResult` into a harness :class:`CheckResult`.

    Green iff ``result.status == STATUS_SUCCESS``; a ``failed`` run surfaces the
    failing model(s) + message, and an ``error`` run (no readable artifact) is
    ``passed=False`` — the fail-closed verdict :class:`DbtRunResult` already
    enforces is inherited unchanged, never undone here.

    The ``summary``/``details`` shape mirrors
    :func:`carve.integrations.dbt.verify.parse_dbt_run` so an agent grounds its
    fix loop on the same fields whichever path produced the result — but this
    consumes the *already-normalized* ``DbtRunResult`` rather than re-reading
    ``run_results.json``.
    """
    if result.status == STATUS_SUCCESS:
        passed_nodes = [pm.name for pm in result.per_model]
        summary = f"dbt run passed: {len(passed_nodes)} node(s) succeeded."
        return CheckResult(
            passed=True,
            summary=summary,
            details={
                "status": result.status,
                "passed_nodes": passed_nodes,
                "failed_nodes": [],
                "run_results_ref": result.run_results_ref,
            },
        )

    failed = _failed_models(result)
    if failed:
        first = failed[0]
        first_msg = first.message or f"status={first.status}"
        suffix = f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""
        summary = f"dbt run failed: {first.name} — {first_msg}{suffix}"
    else:
        # No per-model failure detail. Two distinct shapes land here:
        #   * STATUS_ERROR — the run wrote no readable run_results.json (the
        #     broken-ref/compilation-error case: dbt never produces per-node
        #     detail). `LocalDbtBackend` still captured the "Compilation Error:
        #     ..." line in `result.logs`; derive the summary from that tail so
        #     the agent has something to self-correct on, exactly as the bash
        #     path's `parse_dbt_run` does. Fall back to the static, status-named
        #     line only when there are no logs.
        #   * STATUS_FAILED with an empty per-model list — an artifact WAS read,
        #     so the "no readable run_results.json" wording would be false here;
        #     emit a status-accurate line (+ the log-derived detail when present).
        log_line = error_summary(result.logs) if result.logs else ""
        if result.status == STATUS_ERROR:
            summary = (
                f"dbt run produced no readable run_results.json (not trusted as green): {log_line}"
                if log_line
                else "dbt run produced no readable run_results.json (not trusted as green)."
            )
        else:
            summary = (
                f"dbt run failed with no per-model detail (not trusted as green): {log_line}"
                if log_line
                else "dbt run failed with no per-model detail (not trusted as green)."
            )

    details: dict[str, object] = {
        "status": result.status,
        "passed_nodes": [pm.name for pm in result.per_model if pm.status in _OK_STATUSES],
        "failed_nodes": [
            {
                "node": pm.unique_id,
                "name": pm.name,
                "message": pm.message or f"status={pm.status}",
                "failures": pm.failures,
            }
            for pm in failed
        ],
        "run_results_ref": result.run_results_ref,
    }
    if result.logs:
        details["logs_tail"] = result.logs[-_LOG_TAIL_LIMIT:]

    return CheckResult(
        passed=False,
        summary=summary[:300],
        details=details,
    )


def _failed_models(result: DbtRunResult) -> list[PerModelResult]:
    """The per-model nodes that did not succeed/pass/skip (the failure detail)."""
    return [pm for pm in result.per_model if pm.status not in _OK_STATUSES]


# dbt's per-node statuses that count as a clean / non-failing outcome — the same
# set `carve.integrations.dbt.verify` uses to bucket nodes.
_OK_STATUSES = frozenset({"success", "pass", "skipped"})


@dataclass
class DbtBackendVerificationLoop:
    """Bounded generate → run → read → fix driver over a :class:`DbtBackend`.

    The structured analog of
    :class:`carve.core.agents.verification.VerificationLoop`: instead of running
    a gated ``bash`` command, it calls ``backend.run(command)`` and adapts the
    result via :func:`dbt_run_result_to_check_result`. The backend owns the
    subprocess; this loop only iterates check → fix bounded by ``max_iterations``,
    returning ``needs_user_input`` on exhaustion or when the fix step gives up
    (mirroring the harness loop's terminal semantics exactly).
    """

    backend: DbtBackend
    command: DbtCommand
    max_iterations: int = MAX_VERIFICATION_ITERATIONS

    def __post_init__(self) -> None:
        self.max_iterations = max(1, self.max_iterations)

    def run(self, fix: FixStep) -> VerificationOutcome:
        """Iterate run→adapt→fix up to the ceiling; return the terminal outcome."""
        last = CheckResult(passed=False, summary="not run")
        for iteration in range(1, self.max_iterations + 1):
            last = dbt_run_result_to_check_result(self.backend.run(self.command))
            if last.passed:
                return VerificationOutcome(status="passed", iterations=iteration, last_result=last)
            # Last allowed iteration — don't attempt another fix/re-run.
            if iteration >= self.max_iterations:
                break
            attempted = fix(last)
            if not attempted:
                # The agent couldn't propose a fix — surface to the user.
                return VerificationOutcome(
                    status="needs_user_input",
                    iterations=iteration,
                    last_result=last,
                )
        return VerificationOutcome(
            status="needs_user_input",
            iterations=self.max_iterations,
            last_result=last,
        )


def make_dbt_backend_verification_loop(
    backend: DbtBackend,
    command: DbtCommand,
    *,
    max_iterations: int = MAX_VERIFICATION_ITERATIONS,
) -> DbtBackendVerificationLoop:
    """Build a backend-driven verification loop wired to the structured result.

    The agent rides this loop to author → run ``backend.run(command)`` → read the
    adapted :class:`CheckResult` → self-correct, bounded by ``max_iterations``.
    No new execution path: the backend (a :class:`LocalDbtBackend` in this slice)
    owns the subprocess; this only adapts + iterates. The **preferred** verify
    path for the agent's live loop — the bash-shelled
    :func:`carve.integrations.dbt.runner.make_dbt_verification_loop` stays valid
    for recovery/legacy callers.
    """
    return DbtBackendVerificationLoop(
        backend=backend,
        command=command,
        max_iterations=max_iterations,
    )


__all__ = [
    "DbtBackendVerificationLoop",
    "FixStep",
    "dbt_run_result_to_check_result",
    "make_dbt_backend_verification_loop",
]
