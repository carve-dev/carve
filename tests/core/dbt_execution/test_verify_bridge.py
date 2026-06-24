"""Unit tests for the DbtRunResult -> CheckResult bridge.

Built from injected :class:`DbtRunResult` fixtures — no real dbt needed. They
assert the adapter (1) maps a ``success`` run to a green :class:`CheckResult`
with a per-model rollup, (2) maps a ``failed`` run to a not-green result that
surfaces the failing node + message, (3) inherits the fail-closed verdict for an
``error`` run (no readable artifact), and (4) threads the captured ``logs`` tail
into the failure summary/details on every non-green return — so a STATUS_ERROR
compilation error (no per-model detail) still surfaces dbt's "Compilation
Error: ..." line rather than a content-free static string.
"""

from __future__ import annotations

from carve.core.dbt_execution.result import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_SUCCESS,
    DbtRunResult,
    PerModelResult,
)
from carve.core.dbt_execution.verify_bridge import dbt_run_result_to_check_result


def _success_result() -> DbtRunResult:
    return DbtRunResult(
        status=STATUS_SUCCESS,
        per_model=[
            PerModelResult(
                unique_id="model.analytics.stg_orders", name="stg_orders", status="success"
            ),
            PerModelResult(
                unique_id="model.analytics.dim_orders", name="dim_orders", status="success"
            ),
            PerModelResult(
                unique_id="test.analytics.not_null_stg_orders_id",
                name="not_null_stg_orders_id",
                status="pass",
            ),
        ],
        run_results_ref="/proj/target/run_results.json",
    )


def _failed_result() -> DbtRunResult:
    return DbtRunResult(
        status=STATUS_FAILED,
        per_model=[
            PerModelResult(
                unique_id="model.analytics.stg_orders", name="stg_orders", status="success"
            ),
            PerModelResult(
                unique_id="test.analytics.not_null_stg_orders_order_id.abc123",
                name="not_null_stg_orders_order_id",
                status="fail",
                message="Got 3 results, configured to fail if != 0",
                failures=3,
            ),
        ],
        run_results_ref="/proj/target/run_results.json",
    )


def _error_result() -> DbtRunResult:
    # `from_report(None, ...)` shape: no readable artifact -> error, empty nodes.
    return DbtRunResult(status=STATUS_ERROR, per_model=[])


def test_success_maps_to_green_check_result_with_rollup() -> None:
    check = dbt_run_result_to_check_result(_success_result())

    assert check.passed is True
    assert "3 node(s) succeeded" in check.summary
    assert check.details["status"] == STATUS_SUCCESS
    assert check.details["failed_nodes"] == []
    assert sorted(check.details["passed_nodes"]) == [
        "dim_orders",
        "not_null_stg_orders_id",
        "stg_orders",
    ]
    assert check.details["run_results_ref"] == "/proj/target/run_results.json"


def test_failed_maps_to_not_green_surfacing_failing_model() -> None:
    check = dbt_run_result_to_check_result(_failed_result())

    assert check.passed is False
    # The failing model + dbt's message are surfaced in the summary.
    assert "not_null_stg_orders_order_id" in check.summary
    assert "Got 3 results" in check.summary

    failed = check.details["failed_nodes"]
    assert isinstance(failed, list)
    assert len(failed) == 1
    node = failed[0]
    assert node["name"] == "not_null_stg_orders_order_id"
    assert node["failures"] == 3
    assert "Got 3 results" in node["message"]
    # The passing build node is bucketed separately.
    assert check.details["passed_nodes"] == ["stg_orders"]


def test_error_status_is_fail_closed_not_green() -> None:
    # No logs captured -> the generic, status-named fallback is used.
    check = dbt_run_result_to_check_result(_error_result())

    assert check.passed is False
    assert check.details["status"] == STATUS_ERROR
    assert check.details["failed_nodes"] == []
    assert "no readable run_results.json" in check.summary
    # Empty logs -> no log tail threaded into details.
    assert "logs_tail" not in check.details


def test_error_status_with_logs_surfaces_compilation_error() -> None:
    # The broken-ref/compilation-error case: dbt wrote no run_results.json (so
    # STATUS_ERROR, no per-model detail), but LocalDbtBackend captured the error
    # line in `logs`. The bridge must surface that line, not the static string.
    logs = (
        "Running with dbt=1.8.0\n"
        "Found 2 models, 1 test\n"
        "Compilation Error in model daily_revenue (models/daily_revenue.sql)\n"
        "  Compilation Error: depends on node 'stg_ordrs' which was not found\n"
    )
    result = DbtRunResult(status=STATUS_ERROR, per_model=[], logs=logs)
    check = dbt_run_result_to_check_result(result)

    assert check.passed is False
    assert check.details["status"] == STATUS_ERROR
    # The compilation error text is surfaced via the summary and the log tail.
    assert "Compilation Error" in check.summary
    assert "stg_ordrs" in check.summary
    assert "logs_tail" in check.details
    assert "Compilation Error" in str(check.details["logs_tail"])


def test_failed_with_no_per_model_detail_still_not_green() -> None:
    # A `failed` verdict that (defensively) carries no per-model detail is still
    # surfaced as not-green rather than silently treated as clean.
    result = DbtRunResult(status=STATUS_FAILED, per_model=[])
    check = dbt_run_result_to_check_result(result)

    assert check.passed is False
    assert check.details["failed_nodes"] == []
    # An artifact WAS read (STATUS_FAILED), so the "no readable run_results.json"
    # wording would be false -> a status-accurate line is emitted instead.
    assert "no readable run_results.json" not in check.summary


def test_failed_with_no_per_model_but_logs_surfaces_status_accurate_summary() -> None:
    # STATUS_FAILED with an empty per-model list but captured logs: the summary is
    # status-accurate (NOT the "no readable run_results.json" wording, which is
    # false here) and the log tail is threaded into details.
    logs = (
        "Running with dbt=1.8.0\n"
        "Database Error in test some_test\n"
        "  Runtime Error: connection refused\n"
    )
    result = DbtRunResult(status=STATUS_FAILED, per_model=[], logs=logs)
    check = dbt_run_result_to_check_result(result)

    assert check.passed is False
    assert "no readable run_results.json" not in check.summary
    assert "no per-model detail" in check.summary
    assert "logs_tail" in check.details
    assert "Runtime Error" in str(check.details["logs_tail"])
