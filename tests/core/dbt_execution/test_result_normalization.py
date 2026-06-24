"""DbtRunResult normalizes a green and a failing run identically, whoever fed it.

Rides the shipped ``run_results.json`` fixtures + ``read_run_results`` seam:
``from_report`` produces the same uniform shape whether the report came from a
stub or the local backend, surfaces per-model status, and carries the per-node
``failures`` count.
"""

from __future__ import annotations

from pathlib import Path

from carve.core.dbt_execution.result import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_SUCCESS,
    DbtRunResult,
)
from carve.integrations.dbt.verify import read_run_results

_FIXTURES = Path(__file__).resolve().parents[2] / "integrations" / "dbt" / "fixtures"
_GREEN = _FIXTURES / "run_results_green.json"
_FAILING = _FIXTURES / "run_results_failing.json"
_MANIFEST = _FIXTURES / "manifest.json"


def test_green_run_normalizes_to_success() -> None:
    report = read_run_results(_GREEN, manifest_path=_MANIFEST)
    result = DbtRunResult.from_report(report, returncode=0)

    assert result.status == STATUS_SUCCESS
    statuses = {pm.name: pm.status for pm in result.per_model}
    # Build nodes report `success`, tests report `pass` — raw dbt status surfaces.
    assert statuses["stg_orders"] == "success"
    assert statuses["dim_orders"] == "success"
    assert any(pm.status == "pass" for pm in result.per_model)


def test_failing_run_normalizes_to_failed_with_failures_count() -> None:
    report = read_run_results(_FAILING, manifest_path=_MANIFEST)
    result = DbtRunResult.from_report(report, returncode=1)

    assert result.status == STATUS_FAILED
    by_id = {pm.unique_id: pm for pm in result.per_model}
    failing_test = by_id["test.analytics.not_null_stg_orders_order_id.abc123"]
    assert failing_test.status == "fail"
    # The per-node failures count the dbt-engineer unit flagged is surfaced here.
    assert failing_test.failures == 3
    assert failing_test.message == "Got 3 results, configured to fail if != 0"


def test_clean_exit_no_artifact_is_fail_closed_error() -> None:
    # read_run_results returns None for a missing artifact; a clean exit code
    # alone is NOT trusted as green.
    report = read_run_results(_FIXTURES / "does_not_exist.json")
    assert report is None
    result = DbtRunResult.from_report(report, returncode=0)
    assert result.status == STATUS_ERROR
    assert result.per_model == []


def test_stub_and_backend_paths_normalize_identically() -> None:
    # Whether the report is produced by a "stub" caller or a backend, the same
    # report yields the same DbtRunResult shape — the single normalization seam.
    report = read_run_results(_GREEN, manifest_path=_MANIFEST)
    from_stub = DbtRunResult.from_report(report, returncode=0, logs="stub")
    from_backend = DbtRunResult.from_report(report, returncode=0, logs="backend")

    assert from_stub.status == from_backend.status
    assert [pm.model_dump() for pm in from_stub.per_model] == [
        pm.model_dump() for pm in from_backend.per_model
    ]
