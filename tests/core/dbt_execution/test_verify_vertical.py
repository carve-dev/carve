"""The author -> run -> verify -> self-correct vertical over the backend loop.

The **always-on injected-backend half**: a fake :class:`DbtBackend` whose ``run``
returns a deliberately-``failed`` :class:`DbtRunResult` (a broken ``ref``) then,
after a simulated fix, a ``success`` one — proving
:func:`make_dbt_backend_verification_loop` surfaces the failure as
``CheckResult(passed=False)`` and self-corrects to green, with **no real dbt**.
These tests collect + run regardless of dbt availability (no module-level
``importorskip``); the ``importorskip``-gated real-dbt-against-DuckDB arm lives in
``test_verify_vertical_real_dbt.py`` so its gating never skips these.
"""

from __future__ import annotations

from carve.core.agents.verification import CheckResult
from carve.core.dbt_execution.backend import DbtCommand
from carve.core.dbt_execution.result import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    DbtRunResult,
    PerModelResult,
)
from carve.core.dbt_execution.verify_bridge import make_dbt_backend_verification_loop


class _ScriptedBackend:
    """A fake DbtBackend that returns a queued sequence of DbtRunResults.

    Each ``run`` pops the next queued result, simulating the on-disk outcome the
    real backend would normalize after the agent's edit. Records how many times
    it ran so the test can assert the loop iterated.
    """

    def __init__(self, results: list[DbtRunResult]) -> None:
        self._results = list(results)
        self.runs = 0

    def run(self, command: DbtCommand) -> DbtRunResult:
        self.runs += 1
        return self._results.pop(0)


def _broken_ref_result() -> DbtRunResult:
    return DbtRunResult(
        status=STATUS_FAILED,
        per_model=[
            PerModelResult(
                unique_id="model.analytics.daily_revenue",
                name="daily_revenue",
                status="error",
                message="Compilation Error: depends on node 'stg_ordrs' which was not found",
            )
        ],
    )


def _green_result() -> DbtRunResult:
    return DbtRunResult(
        status=STATUS_SUCCESS,
        per_model=[
            PerModelResult(
                unique_id="model.analytics.daily_revenue",
                name="daily_revenue",
                status="success",
            )
        ],
    )


def test_injected_backend_loop_self_corrects_to_green() -> None:
    # First run fails (broken ref); the fix step "edits" the model, then the
    # second run is green — the loop should reach a passing outcome.
    backend = _ScriptedBackend([_broken_ref_result(), _green_result()])
    loop = make_dbt_backend_verification_loop(backend, DbtCommand(command="build"))

    observed: list[CheckResult] = []

    def _fix(last: CheckResult) -> bool:
        observed.append(last)
        # The agent would edit the broken ref here; the next queued result is green.
        return True

    outcome = loop.run(_fix)

    assert outcome.status == "passed"
    assert outcome.iterations == 2
    assert backend.runs == 2
    # The fix step saw exactly the failing check, surfacing the broken model.
    assert len(observed) == 1
    assert observed[0].passed is False
    assert "daily_revenue" in observed[0].summary


def test_injected_backend_loop_surfaces_failure_when_fix_gives_up() -> None:
    backend = _ScriptedBackend([_broken_ref_result()])
    loop = make_dbt_backend_verification_loop(backend, DbtCommand(command="build"))

    outcome = loop.run(lambda _last: False)

    assert outcome.status == "needs_user_input"
    assert outcome.iterations == 1
    assert outcome.last_result.passed is False
    assert backend.runs == 1
