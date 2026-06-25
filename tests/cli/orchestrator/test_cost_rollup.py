"""Unit tests for `cli.orchestrator.cost_rollup`.

The Plan's cost is the sum of the subagents' `DelegationResult` usage +
cost; the runtime estimate composes from the engineers' `expected_outputs`
(read off `DelegationResult.outputs`); and **no warehouse-dollar figure**
is ever emitted. Exercised here with synthetic `DelegationResult`s — Unit
2's live delegation will feed real ones through the same seam.
"""

from __future__ import annotations

import dataclasses

from carve.cli.orchestrator.cost_rollup import (
    CostRollup,
    RuntimeEstimate,
    compose_runtime_estimate,
    roll_up_cost,
)
from carve.core.agents.delegation import DelegationResult
from carve.core.agents.loop import TokenUsage


def _result(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
    cost_usd: float = 0.0,
    outputs: dict[str, object] | None = None,
) -> DelegationResult:
    return DelegationResult(
        status="succeeded",
        result_summary="ok",
        files_changed=[],
        outputs=outputs or {},
        usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
        ),
        cost_usd=cost_usd,
    )


# --------------------------------------------------------------------- cost sum


def test_cost_equals_sum_of_delegation_usage() -> None:
    """The rollup cost + tokens equal the sum across every subagent."""
    results = [
        _result(input_tokens=1000, output_tokens=200, cost_usd=0.012),
        _result(input_tokens=3000, output_tokens=500, cost_usd=0.040),
        _result(input_tokens=50, output_tokens=10, cost_usd=0.001),
    ]
    rollup = roll_up_cost(results)

    assert rollup.usage.input_tokens == 4050
    assert rollup.usage.output_tokens == 710
    assert rollup.cost_usd == 0.012 + 0.040 + 0.001


def test_cache_token_fields_are_summed() -> None:
    """All four TokenUsage fields aggregate, not just input/output."""
    results = [
        _result(cache_creation=100, cache_read=200),
        _result(cache_creation=5, cache_read=7),
    ]
    rollup = roll_up_cost(results)
    assert rollup.usage.cache_creation_tokens == 105
    assert rollup.usage.cache_read_tokens == 207


def test_empty_results_yield_zero_rollup() -> None:
    """No subagents ran → a zero rollup with no estimate."""
    rollup = roll_up_cost([])
    assert rollup.cost_usd == 0.0
    assert rollup.usage.input_tokens == 0
    assert rollup.runtime.has_estimate is False


# --------------------------------------------------------------- no warehouse $


def test_no_warehouse_dollar_figure_is_emitted() -> None:
    """The rollup surfaces LLM cost + runtime only — never a warehouse figure.

    Enforced structurally: CostRollup carries exactly usage / cost_usd /
    runtime, and RuntimeEstimate carries only durations. Even if a
    subagent tries to smuggle a warehouse number through `outputs`, it
    never lands on the rollup.
    """
    cost_fields = {f.name for f in dataclasses.fields(CostRollup)}
    assert cost_fields == {"usage", "cost_usd", "runtime"}

    runtime_fields = {f.name for f in dataclasses.fields(RuntimeEstimate)}
    assert runtime_fields == {"first_run_seconds", "subsequent_run_seconds"}
    assert not any("warehouse" in name or "dollar" in name for name in runtime_fields)

    # A subagent emitting a warehouse-dollar hint does not surface it.
    rollup = roll_up_cost([_result(cost_usd=0.05, outputs={"warehouse_cost_usd": 999.0})])
    assert rollup.cost_usd == 0.05  # LLM cost only — the 999 is ignored


# ----------------------------------------------------------------- runtime est


def test_runtime_estimate_composes_from_expected_outputs() -> None:
    """First-run + subsequent durations are read off `outputs` and summed."""
    results = [
        _result(outputs={"first_run_seconds": 1200, "subsequent_run_seconds": 30}),
        _result(outputs={"first_run_seconds": 300, "subsequent_run_seconds": 10}),
    ]
    estimate = compose_runtime_estimate(results)
    assert estimate.first_run_seconds == 1500
    assert estimate.subsequent_run_seconds == 40
    assert estimate.has_estimate is True
    rendered = estimate.render()
    assert rendered is not None
    assert "first run" in rendered
    assert "subsequent" in rendered


def test_runtime_estimate_degrades_when_hints_absent() -> None:
    """No duration hints → no estimate; the surface omits the line."""
    results = [_result(outputs={"rows": 100}), _result(outputs={})]
    estimate = compose_runtime_estimate(results)
    assert estimate.first_run_seconds is None
    assert estimate.subsequent_run_seconds is None
    assert estimate.has_estimate is False
    assert estimate.render() is None


def test_runtime_estimate_partial_hint_keeps_present_half() -> None:
    """Only a first-run hint present → subsequent stays None, estimate renders."""
    estimate = compose_runtime_estimate([_result(outputs={"first_load_seconds": 1500})])
    assert estimate.first_run_seconds == 1500
    assert estimate.subsequent_run_seconds is None
    rendered = estimate.render()
    assert rendered is not None
    assert "subsequent" not in rendered


def test_non_positive_and_bool_durations_are_ignored() -> None:
    """Zero / negative / bool 'durations' don't count as estimates."""
    results = [
        _result(outputs={"first_run_seconds": 0}),
        _result(outputs={"subsequent_run_seconds": -5}),
        _result(outputs={"first_run_seconds": True}),
    ]
    estimate = compose_runtime_estimate(results)
    assert estimate.has_estimate is False


def test_roll_up_includes_runtime_estimate() -> None:
    """`roll_up_cost` composes the runtime estimate alongside the cost."""
    rollup = roll_up_cost([_result(cost_usd=0.02, outputs={"first_run_seconds": 600})])
    assert rollup.cost_usd == 0.02
    assert rollup.runtime.first_run_seconds == 600
