"""parse_dlt_run / read_latest_load_package against canned + real load packages."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from carve.integrations.dlt.verify import parse_dlt_run, read_latest_load_package


def _proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["dlt"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _make_package(
    root: Path,
    name: str,
    load_id: str,
    *,
    state: str = "loaded",
    tables: tuple[str, ...] = ("items", "_dlt_pipeline_state"),
    schema_changes: tuple[str, ...] = ("items", "_dlt_pipeline_state"),
    failed: tuple[str, ...] = (),
) -> Path:
    pkg = root / name / "load" / state / load_id
    (pkg / "completed_jobs").mkdir(parents=True)
    for t in tables:
        (pkg / "completed_jobs" / f"{t}.hash.0.insert_values.gz").write_text("")
    if state == "loaded":
        (pkg / "package_completed.json").write_text('"loaded"')
    (pkg / "applied_schema_updates.json").write_text(
        json.dumps({t: {"columns": {}} for t in schema_changes})
    )
    (pkg / "load_package_state.json").write_text(
        json.dumps(
            {"load_metrics": {f"{t}.h.gz": {"table_name": t, "state": "completed"} for t in tables}}
        )
    )
    if failed:
        fdir = pkg / "failed_jobs"
        fdir.mkdir()
        for f in failed:
            (fdir / f).write_text("boom")
    return pkg


# --- exit-code gate --------------------------------------------------------


def test_nonzero_exit_surfaces_the_error_line_from_stdout() -> None:
    # run_check routes all output to stdout (stderr=""); the summary must be the
    # real error line (end of the traceback), not the first log-preamble line,
    # and the full output is preserved in details for the fix loop.
    stdout = "INFO: starting pipeline\nINFO: loading\nPipelineStepFailed: boom\n"
    res = parse_dlt_run(_proc(returncode=1, stdout=stdout))
    assert res.passed is False
    assert res.summary == "PipelineStepFailed: boom"
    assert res.details["returncode"] == 1
    assert "PipelineStepFailed: boom" in res.details["output_tail"]


def test_nonzero_exit_falls_back_to_last_line_without_a_marker() -> None:
    res = parse_dlt_run(_proc(returncode=2, stdout="step one\nstep two\n"))
    assert res.passed is False
    assert res.summary == "step two"


def test_exit_zero_without_pipelines_dir_trusts_exit_code() -> None:
    res = parse_dlt_run(_proc(returncode=0))
    assert res.passed is True
    assert "exited 0" in res.summary


# --- on-disk load package --------------------------------------------------


def test_clean_load_reports_user_tables_filtering_internal(tmp_path: Path) -> None:
    _make_package(tmp_path, "p", "1782200000.0")
    res = parse_dlt_run(_proc(), pipelines_dir=tmp_path, pipeline_name="p")
    assert res.passed is True
    assert res.details["tables"] == ["items"]  # _dlt_* filtered
    assert res.details["schema_changes"] == ["items"]
    assert "Loaded 1 table(s) (items)" in res.summary


def test_failed_jobs_make_it_a_failure(tmp_path: Path) -> None:
    _make_package(tmp_path, "p", "1782200000.0", failed=("items.h.gz.exception",))
    res = parse_dlt_run(_proc(), pipelines_dir=tmp_path, pipeline_name="p")
    assert res.passed is False
    assert "failed job" in res.summary
    assert res.details["failed_jobs"] == ["items.h.gz.exception"]


def test_package_not_loaded_is_a_failure(tmp_path: Path) -> None:
    # Stuck in 'normalized' (load step didn't complete).
    _make_package(tmp_path, "p", "1782200000.0", state="normalized")
    res = parse_dlt_run(_proc(), pipelines_dir=tmp_path, pipeline_name="p")
    assert res.passed is False
    assert res.details["completed"] is False


def test_reads_the_newest_package(tmp_path: Path) -> None:
    _make_package(tmp_path, "p", "1782200000.0", tables=("old_table", "_dlt_pipeline_state"))
    _make_package(tmp_path, "p", "1782299999.9", tables=("new_table", "_dlt_pipeline_state"))
    report = read_latest_load_package(tmp_path, "p")
    assert report is not None
    assert report.load_id == "1782299999.9"
    assert report.tables == ("new_table",)


def test_absent_package_returns_none(tmp_path: Path) -> None:
    assert read_latest_load_package(tmp_path, "nope") is None


# --- real dlt run end to end ----------------------------------------------


def test_real_dlt_run_to_duckdb_parses_clean(tmp_path: Path) -> None:
    import dlt

    @dlt.resource(name="orders", write_disposition="replace")
    def orders():  # type: ignore[no-untyped-def]
        yield {"id": 1, "amount": 9.99}
        yield {"id": 2, "amount": 4.50}

    pipelines_dir = tmp_path / "dlt"
    pipe = dlt.pipeline(
        pipeline_name="real",
        destination="duckdb",
        dataset_name="ds",
        pipelines_dir=str(pipelines_dir),
    )
    pipe.run(orders())

    res = parse_dlt_run(_proc(returncode=0), pipelines_dir=pipelines_dir, pipeline_name="real")
    assert res.passed is True
    assert "orders" in res.details["tables"]
    assert "orders" in res.details["schema_changes"]


@pytest.fixture(autouse=True)
def _isolate_duckdb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep dlt's default duckdb file out of the repo working dir.
    monkeypatch.chdir(tmp_path)
