"""DltStepExecutor: run-mechanism construction, env injection, load-package parse.

The run mechanism is **injected** so the offline layer never spawns a venv: a
fake ``run_fn`` records how it was invoked and (when the test wants a parseable
verdict) writes a load package via the real dlt Python API. One
``importorskip("dlt")``-gated test runs a real DuckDB load through the default
mechanism and parses its package end to end (mirrors
``tests/integrations/dlt/test_verify.py``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import DbtStepConfig, DltStepConfig
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_types.dlt import DltRunOutcome, DltStepExecutor

# A deterministic run start well BEFORE the fixed load_ids the fixtures write
# (1.7e9 / 1.8e9 ≈ 2026). The FIX-D2-residual recency filter only trusts a load
# package whose load_id >= run.started_at; pinning the start far in the past
# keeps the hand-written fixture packages "in-window" for these baseline tests,
# while the cross-run-staleness test below pins the start AFTER a stale package.
_FIXED_START = datetime(2001, 1, 1, tzinfo=UTC)


def _run_at(started_at: datetime = _FIXED_START, **overrides: Any) -> PipelineRun:
    """A PipelineRun with a deterministic started_at (the recency cutoff)."""
    return PipelineRun(pipeline="p", started_at=started_at, **overrides)


@pytest.fixture
def paths(tmp_path: Path) -> ProjectPaths:
    (tmp_path / "el").mkdir()
    (tmp_path / "pipelines").mkdir()
    return ProjectPaths.from_root(tmp_path)


def _component(paths: ProjectPaths, name: str) -> Path:
    """Create a minimal el/<name>/ component dir with a runnable entrypoint."""
    comp = paths.el_dir / name
    (comp / "scripts").mkdir(parents=True)
    (comp / "scripts" / "__init__.py").write_text("def run():\n    pass\n", encoding="utf-8")
    return comp


def _make_load_package(
    pipelines_dir: Path,
    pipeline_name: str,
    *,
    state: str = "loaded",
    tables: tuple[str, ...] = ("items", "_dlt_pipeline_state"),
    failed: tuple[str, ...] = (),
) -> None:
    """Write a minimal dlt load package (the shape parse_dlt_run reads)."""
    pkg = pipelines_dir / pipeline_name / "load" / state / "1782200000.0"
    (pkg / "completed_jobs").mkdir(parents=True)
    for t in tables:
        (pkg / "completed_jobs" / f"{t}.hash.0.insert_values.gz").write_text("")
    (pkg / "applied_schema_updates.json").write_text(
        json.dumps({t: {"columns": {}} for t in tables})
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


class _RecordingRun:
    """A fake run mechanism: records its kwargs, optionally writes a package."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        output: str = "",
        timed_out: bool = False,
        write_package: dict[str, Any] | None = None,
    ) -> None:
        self.returncode = returncode
        self.output = output
        self.timed_out = timed_out
        self._write_package = write_package
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> DltRunOutcome:
        self.calls.append(kwargs)
        if self._write_package is not None:
            # dlt's DLT_DATA_DIR points at <root>/.dlt; the package lands under
            # <DLT_DATA_DIR>/pipelines/<name>/.
            data_dir = Path(kwargs["env"]["DLT_DATA_DIR"])
            _make_load_package(data_dir / "pipelines", **self._write_package)
        return DltRunOutcome(
            returncode=self.returncode,
            output=self.output,
            duration_ms=12,
            timed_out=self.timed_out,
        )


def _dlt_step(**overrides: Any) -> DltStepConfig:
    base: dict[str, Any] = {"id": "ingest", "component": "stripe"}
    base.update(overrides)
    return DltStepConfig(**base)


# --- construction + env injection ------------------------------------------


async def test_runs_the_injected_mechanism_with_entrypoint_cwd_env(paths: ProjectPaths) -> None:
    _component(paths, "stripe")
    run_fn = _RecordingRun(write_package={"pipeline_name": "stripe"})
    executor = DltStepExecutor(run_fn=run_fn)

    result = await executor.execute(step=_dlt_step(), run=_run_at(target="dev"), paths=paths)

    assert result.status == "succeeded"
    assert len(run_fn.calls) == 1
    call = run_fn.calls[0]
    assert call["entrypoint"] == paths.el_dir / "stripe" / "scripts" / "__init__.py"
    assert call["cwd"] == paths.root
    # The dlt data dir is pinned so the load package lands where we can read it.
    assert call["env"]["DLT_DATA_DIR"] == str(paths.dlt_config_dir)


async def test_write_disposition_override_fails_loud(paths: ProjectPaths) -> None:
    # FIX-D1: the keys Carve invented were never read by dlt, so forwarding an
    # override silently no-op'd and still reported success. The executor now
    # fails loud instead of false-green — and never runs the component.
    _component(paths, "stripe")
    run_fn = _RecordingRun()
    executor = DltStepExecutor(run_fn=run_fn)

    result = await executor.execute(
        step=_dlt_step(write_disposition="replace"),
        run=PipelineRun(pipeline="p"),
        paths=paths,
    )

    assert result.status == "failed"
    assert "write_disposition/resource_select override is not yet supported" in (
        result.error_message or ""
    )
    assert run_fn.calls == []  # never ran the component


async def test_resource_select_override_fails_loud(paths: ProjectPaths) -> None:
    _component(paths, "stripe")
    run_fn = _RecordingRun()

    result = await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(resource_select=["charges", "refunds"]),
        run=PipelineRun(pipeline="p"),
        paths=paths,
    )

    assert result.status == "failed"
    assert "not yet supported" in (result.error_message or "")
    assert run_fn.calls == []


async def test_no_overrides_carries_only_the_data_dir_env(paths: ProjectPaths) -> None:
    # FIX-D1: the dead CARVE_DLT_* keys are gone entirely; the env carries only
    # the pinned DLT_DATA_DIR.
    _component(paths, "stripe")
    run_fn = _RecordingRun()
    await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(), run=PipelineRun(pipeline="p"), paths=paths
    )
    env = run_fn.calls[0]["env"]
    assert "CARVE_DLT_WRITE_DISPOSITION" not in env
    assert "CARVE_DLT_RESOURCE_SELECT" not in env
    assert env == {"DLT_DATA_DIR": str(paths.dlt_config_dir)}


# --- load-package parse -> StepResult --------------------------------------


async def test_clean_load_maps_to_succeeded_with_table_outputs(paths: ProjectPaths) -> None:
    _component(paths, "stripe")
    run_fn = _RecordingRun(write_package={"pipeline_name": "stripe"})

    result = await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(), run=_run_at(), paths=paths
    )

    assert result.status == "succeeded"
    # _dlt_* internal tables are filtered out.
    assert result.outputs["tables"] == ["items"]
    assert result.outputs["schema_changes"] == ["items"]
    assert result.outputs["failed_jobs"] == []
    assert result.duration_ms == 12


async def test_failed_jobs_make_it_failed(paths: ProjectPaths) -> None:
    _component(paths, "stripe")
    run_fn = _RecordingRun(
        write_package={"pipeline_name": "stripe", "failed": ("items.exc",)},
    )

    result = await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(), run=_run_at(), paths=paths
    )

    assert result.status == "failed"
    assert result.outputs["failed_jobs"] == ["items.exc"]
    assert "did not complete cleanly" in (result.error_message or "")


async def test_package_not_loaded_is_failed(paths: ProjectPaths) -> None:
    _component(paths, "stripe")
    run_fn = _RecordingRun(write_package={"pipeline_name": "stripe", "state": "normalized"})

    result = await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(), run=_run_at(), paths=paths
    )

    assert result.status == "failed"
    assert "did not reach 'loaded'" in (result.error_message or "")


async def test_nonzero_exit_is_failed_with_error_tail(paths: ProjectPaths) -> None:
    _component(paths, "stripe")
    run_fn = _RecordingRun(returncode=1, output="INFO: starting\nPipelineStepFailed: boom\n")

    result = await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(), run=PipelineRun(pipeline="p"), paths=paths
    )

    assert result.status == "failed"
    assert result.error_message == "PipelineStepFailed: boom"


async def test_exit_zero_without_any_data_dir_trusts_exit_code(paths: ProjectPaths) -> None:
    # No .dlt/pipelines dir is created at all (the run wrote nowhere we pinned),
    # so there is nothing to introspect — trust the clean exit code.
    _component(paths, "stripe")
    run_fn = _RecordingRun()  # exit 0, writes no package

    result = await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(), run=PipelineRun(pipeline="p"), paths=paths
    )

    assert result.status == "succeeded"
    assert result.outputs == {"tables": [], "schema_changes": [], "failed_jobs": []}


# --- FIX-D2: pipeline_name discovery + false-green guard --------------------


async def test_discovers_package_under_a_differently_named_pipeline_subdir(
    paths: ProjectPaths,
) -> None:
    # The component dir is "stripe" but the script's dlt.pipeline(pipeline_name=)
    # is "hacker_news" (mirroring the reference HN pack: pipeline_name differs
    # from the el/<dir>). The package lands under the *pipeline* name; the
    # executor must discover + parse it rather than fall back to the component
    # name (which has no load/ dir) and report a false green.
    _component(paths, "stripe")
    run_fn = _RecordingRun(write_package={"pipeline_name": "hacker_news"})

    result = await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(), run=_run_at(), paths=paths
    )

    assert result.status == "succeeded"
    assert result.outputs["tables"] == ["items"]
    assert result.outputs["schema_changes"] == ["items"]


async def test_picks_the_newest_package_across_pipeline_subdirs(paths: ProjectPaths) -> None:
    # Two pipeline subdirs, neither named for the component. The newest load id
    # wins (read_latest_load_package's ordering), reused for cross-subdir
    # discovery — so the result reflects the most recent run's package.
    _component(paths, "stripe")

    def _run(**kwargs: Any) -> DltRunOutcome:
        pipelines = Path(kwargs["env"]["DLT_DATA_DIR"]) / "pipelines"
        # An older package under one name, a newer one (higher load id) under
        # another, with a distinguishing table set on the newer.
        _make_load_package(pipelines, "old_pipe", tables=("alpha", "_dlt_pipeline_state"))
        newer = pipelines / "new_pipe" / "load" / "loaded" / "1899999999.0"
        (newer / "completed_jobs").mkdir(parents=True)
        (newer / "completed_jobs" / "beta.h.0.insert_values.gz").write_text("")
        (newer / "applied_schema_updates.json").write_text(json.dumps({"beta": {"columns": {}}}))
        metrics = {"load_metrics": {"beta.h.gz": {"table_name": "beta", "state": "completed"}}}
        (newer / "load_package_state.json").write_text(json.dumps(metrics))
        return DltRunOutcome(returncode=0, output="loaded", duration_ms=4)

    result = await DltStepExecutor(run_fn=_run).execute(
        step=_dlt_step(), run=_run_at(), paths=paths
    )

    assert result.status == "succeeded"
    assert result.outputs["tables"] == ["beta"]


async def test_data_dir_with_no_load_package_is_failed(paths: ProjectPaths) -> None:
    # The data dir exists with a pipeline subdir, but no subdir holds a load/
    # package — the run loaded nothing we can verify. NEVER succeeded-with-empty.
    _component(paths, "stripe")

    def _run(**kwargs: Any) -> DltRunOutcome:
        # Create a pipeline subdir with state but no load/ dir.
        bare = Path(kwargs["env"]["DLT_DATA_DIR"]) / "pipelines" / "stripe"
        bare.mkdir(parents=True)
        (bare / "schema.json").write_text("{}")
        return DltRunOutcome(returncode=0, output="loaded", duration_ms=2)

    result = await DltStepExecutor(run_fn=_run).execute(
        step=_dlt_step(), run=_run_at(), paths=paths
    )

    assert result.status == "failed"
    assert "produced no new load package" in (result.error_message or "")


# --- FIX-D2 residual: cross-run stale-package false-green -------------------


def _write_package_with_load_id(
    pipelines_dir: Path,
    pipeline_name: str,
    load_id: str,
    *,
    tables: tuple[str, ...] = ("items", "_dlt_pipeline_state"),
) -> None:
    """Write a ``loaded`` package with a CONTROLLED load_id (str(unix_time)).

    Lets a test seed a stale (pre-run-start) package vs a fresh (post-start)
    one to exercise the recency filter deterministically.
    """
    pkg = pipelines_dir / pipeline_name / "load" / "loaded" / load_id
    (pkg / "completed_jobs").mkdir(parents=True)
    for t in tables:
        (pkg / "completed_jobs" / f"{t}.hash.0.insert_values.gz").write_text("")
    (pkg / "applied_schema_updates.json").write_text(
        json.dumps({t: {"columns": {}} for t in tables})
    )
    (pkg / "load_package_state.json").write_text(
        json.dumps(
            {"load_metrics": {f"{t}.h.gz": {"table_name": t, "state": "completed"} for t in tables}}
        )
    )


async def test_stale_package_from_a_prior_run_is_not_false_green(paths: ProjectPaths) -> None:
    # Reproduces the verifier's trace: DLT_DATA_DIR is persistent, so a prior
    # run's `loaded` package sits in the data dir. This run exits 0 but writes
    # NO new package (an empty resource / a run_fn rc=0 that loads nothing). The
    # executor must NOT read the PRIOR run's package and report succeeded-with-
    # stale-tables — it must fail loud because no package is from THIS run.
    _component(paths, "stripe")
    started_at = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
    # A stale `loaded` package whose load_id (epoch seconds) predates the run.
    stale_load_id = str(started_at.timestamp() - 3600.0)  # one hour BEFORE start

    def _run(**kwargs: Any) -> DltRunOutcome:
        # Seed the persistent data dir with the prior run's package, then write
        # NOTHING new (rc=0 with no new package) — the stale-false-green setup.
        pipelines = Path(kwargs["env"]["DLT_DATA_DIR"]) / "pipelines"
        _write_package_with_load_id(pipelines, "stripe", stale_load_id)
        return DltRunOutcome(returncode=0, output="loaded", duration_ms=3)

    result = await DltStepExecutor(run_fn=_run).execute(
        step=_dlt_step(), run=_run_at(started_at), paths=paths
    )

    assert result.status == "failed"
    assert "produced no new load package" in (result.error_message or "")
    assert "stale packages from prior runs" in (result.error_message or "")


async def test_fresh_package_from_this_run_still_succeeds(paths: ProjectPaths) -> None:
    # The other side of the recency filter: a package whose load_id is from THIS
    # run (>= run.started_at) is trusted as before — no regression of the green
    # path. Same data dir may even hold an older stale package; the fresh one
    # wins and the run succeeds.
    _component(paths, "stripe")
    started_at = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
    stale_load_id = str(started_at.timestamp() - 3600.0)  # prior run
    fresh_load_id = str(started_at.timestamp() + 5.0)  # this run

    def _run(**kwargs: Any) -> DltRunOutcome:
        pipelines = Path(kwargs["env"]["DLT_DATA_DIR"]) / "pipelines"
        # A stale package AND a fresh one in the same pipeline dir — the fresh,
        # in-window package is the one trusted (newest-wins + recency).
        _write_package_with_load_id(
            pipelines, "stripe", stale_load_id, tables=("old", "_dlt_pipeline_state")
        )
        _write_package_with_load_id(
            pipelines, "stripe", fresh_load_id, tables=("items", "_dlt_pipeline_state")
        )
        return DltRunOutcome(returncode=0, output="loaded", duration_ms=4)

    result = await DltStepExecutor(run_fn=_run).execute(
        step=_dlt_step(), run=_run_at(started_at), paths=paths
    )

    assert result.status == "succeeded"
    assert result.outputs["tables"] == ["items"]


# --- resolution failures ---------------------------------------------------


async def test_unresolvable_component_is_failed(paths: ProjectPaths) -> None:
    run_fn = _RecordingRun()
    result = await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(component="missing"), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "did not resolve" in (result.error_message or "")
    assert run_fn.calls == []  # never ran


async def test_component_without_entrypoint_is_failed(paths: ProjectPaths) -> None:
    # An el/<name>/ dir with no runnable entrypoint file.
    (paths.el_dir / "stripe").mkdir()
    run_fn = _RecordingRun()
    result = await DltStepExecutor(run_fn=run_fn).execute(
        step=_dlt_step(), run=PipelineRun(pipeline="p"), paths=paths
    )
    assert result.status == "failed"
    assert "no runnable entrypoint" in (result.error_message or "")


async def test_dbt_step_routed_to_dlt_executor_raises(paths: ProjectPaths) -> None:
    # The registry guarantees the right type; a mis-routed step is a hard error
    # (degraded to `failed` by the DAG walk, not silently mis-run).
    executor = DltStepExecutor(run_fn=_RecordingRun())
    with pytest.raises(TypeError):
        await executor.execute(
            step=DbtStepConfig(id="x"), run=PipelineRun(pipeline="p"), paths=paths
        )


# --- real dlt load end to end (importorskip-gated) -------------------------


async def test_real_dlt_load_parses_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("dlt")
    import dlt

    (tmp_path / "el").mkdir()
    (tmp_path / "pipelines").mkdir()
    paths = ProjectPaths.from_root(tmp_path)
    comp = paths.el_dir / "orders"
    (comp / "scripts").mkdir(parents=True)
    (comp / "scripts" / "__init__.py").write_text("def run():\n    pass\n", encoding="utf-8")

    def real_run(**kwargs: Any) -> DltRunOutcome:
        # Run a real, creds-free DuckDB load into the pinned DLT_DATA_DIR so the
        # executor's package discovery + parse runs against a genuine package.
        data_dir = Path(kwargs["env"]["DLT_DATA_DIR"])

        @dlt.resource(name="orders", write_disposition="replace")
        def orders():  # type: ignore[no-untyped-def]
            yield {"id": 1, "amount": 9.99}
            yield {"id": 2, "amount": 4.50}

        pipe = dlt.pipeline(
            pipeline_name="orders",
            destination="duckdb",
            dataset_name="ds",
            pipelines_dir=str(data_dir / "pipelines"),
        )
        pipe.run(orders())
        return DltRunOutcome(returncode=0, output="loaded", duration_ms=5)

    # Keep dlt's default duckdb file out of the repo working dir.
    monkeypatch.chdir(tmp_path)

    result = await DltStepExecutor(run_fn=real_run).execute(
        step=DltStepConfig(id="ingest", component="orders"),
        run=PipelineRun(pipeline="p"),
        paths=paths,
    )

    assert result.status == "succeeded"
    assert "orders" in result.outputs["tables"]
    assert "orders" in result.outputs["schema_changes"]


# A scripts/__init__.py that mirrors the SHIPPED reference HN pack's convention
# (the ``if __name__ == "__main__": run()`` guard calling ``run()``, which does a
# real dlt load) — but loads a creds-free local resource instead of the live HN
# API, so the test stays deterministic and offline. Critically, its
# pipeline_name ("reference_news") differs from its el/<dir> name, mirroring the
# real pack (pipeline_name "hacker_news" vs its dir), exercising the FIX-D2
# cross-subdir discovery through the genuine default mechanism.
_REFERENCE_CONVENTION_ENTRYPOINT = """\
import dlt


@dlt.resource(name="stories", write_disposition="replace")
def stories():
    yield {"id": 1, "title": "a"}
    yield {"id": 2, "title": "b"}


def run() -> None:
    pipeline = dlt.pipeline(
        pipeline_name="reference_news",
        destination="duckdb",
        dataset_name="news",
    )
    pipeline.run(stories())


if __name__ == "__main__":
    run()
"""


async def test_default_run_mechanism_runs_the_reference_convention_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FIX-D4: every other dlt test injects run_fn, so the DEFAULT
    # _default_run_component (python <entrypoint> via Subprocess) is never
    # exercised. This runs it for real — no run_fn injection — against a
    # component following the reference pack's scripts/__init__.py + __main__
    # convention, proving the default mechanism actually executes a load and the
    # executor discovers + parses the resulting package. Offline + creds-free.
    pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    (tmp_path / "el").mkdir()
    (tmp_path / "pipelines").mkdir()
    paths = ProjectPaths.from_root(tmp_path)
    comp = paths.el_dir / "news"
    (comp / "scripts").mkdir(parents=True)
    (comp / "scripts" / "__init__.py").write_text(
        _REFERENCE_CONVENTION_ENTRYPOINT, encoding="utf-8"
    )

    # Keep dlt's default duckdb file out of the repo working dir.
    monkeypatch.chdir(tmp_path)

    # No run_fn → the real _default_run_component runs `python scripts/__init__.py`.
    result = await DltStepExecutor().execute(
        step=DltStepConfig(id="ingest", component="news"),
        run=PipelineRun(pipeline="p"),
        paths=paths,
    )

    assert result.status == "succeeded", result.error_message
    assert "stories" in result.outputs["tables"]
    assert "stories" in result.outputs["schema_changes"]
