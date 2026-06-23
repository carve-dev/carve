"""parse_dbt_run / read_run_results against fixture run_results.json artifacts.

Offline: the parser reads a fixture ``run_results.json`` (a real dbt artifact
shape) + a stand-in ``CompletedProcess`` — no live ``dbt`` run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from carve.integrations.dbt.verify import parse_dbt_run, read_run_results

_FIXTURES = Path(__file__).parent / "fixtures"


def _proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["dbt", "build"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _fixture(name: str, tmp_path: Path) -> Path:
    dest = tmp_path / "run_results.json"
    shutil.copy(_FIXTURES / name, dest)
    return dest


# --- green run -------------------------------------------------------------


def test_all_pass_run_is_green(tmp_path: Path) -> None:
    results = _fixture("run_results_green.json", tmp_path)
    res = parse_dbt_run(_proc(returncode=0), run_results_path=results)

    assert res.passed is True
    assert res.details["failed_nodes"] == []
    assert len(res.details["passed_nodes"]) == 4
    assert "passed" in res.summary


def test_green_run_resolves_readable_names_via_manifest(tmp_path: Path) -> None:
    results = _fixture("run_results_green.json", tmp_path)
    manifest = _FIXTURES / "manifest.json"
    res = parse_dbt_run(_proc(returncode=0), run_results_path=results, manifest_path=manifest)
    assert res.passed is True
    # unique_ids resolved to readable model/test names
    assert "stg_orders" in res.details["passed_nodes"]


# --- failing run -----------------------------------------------------------


def test_failing_node_makes_it_not_green_surfacing_node_and_message(tmp_path: Path) -> None:
    results = _fixture("run_results_failing.json", tmp_path)
    res = parse_dbt_run(_proc(returncode=1), run_results_path=results)

    assert res.passed is False
    assert "not_null_stg_orders_order_id" in res.summary or any(
        "not_null_stg_orders_order_id" in f["node"] for f in res.details["failed_nodes"]
    )
    failed = res.details["failed_nodes"]
    assert len(failed) == 1
    assert "Got 3 results" in failed[0]["message"]
    # the succeeded + skipped nodes are still recorded as passing
    assert len(res.details["passed_nodes"]) == 2


def test_clean_exit_with_failing_node_is_not_trusted(tmp_path: Path) -> None:
    # Exit 0 but run_results.json has a failing node — the artifact is the truth.
    results = _fixture("run_results_failing.json", tmp_path)
    res = parse_dbt_run(_proc(returncode=0), run_results_path=results)
    assert res.passed is False
    assert res.details["failed_nodes"]


# --- exit-code gate / fail-closed ------------------------------------------


def test_nonzero_exit_without_artifact_surfaces_error_line(tmp_path: Path) -> None:
    stdout = "Running with dbt=1.8\nCompilation Error in model stg_orders\n"
    res = parse_dbt_run(_proc(returncode=1, stdout=stdout))
    assert res.passed is False
    assert "Compilation Error" in res.summary
    assert res.details["returncode"] == 1


def test_clean_exit_without_run_results_path_trusts_exit_code() -> None:
    res = parse_dbt_run(_proc(returncode=0))
    assert res.passed is True
    assert "exited 0" in res.summary


def test_clean_exit_with_missing_artifact_is_not_green(tmp_path: Path) -> None:
    # Exit 0 but the run wrote no readable run_results.json — not trusted.
    res = parse_dbt_run(_proc(returncode=0), run_results_path=tmp_path / "absent.json")
    assert res.passed is False
    assert "run_results.json" in res.summary


def test_malformed_run_results_is_fail_closed(tmp_path: Path) -> None:
    bad = tmp_path / "run_results.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_run_results(bad) is None
    res = parse_dbt_run(_proc(returncode=0), run_results_path=bad)
    assert res.passed is False
