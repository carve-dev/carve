"""LocalDbtBackend orchestration with an injected fake engine — no real dbt.

The "engine binary" is a tiny Python script we generate: it records the argv +
env it was invoked with, drops a fixture ``target/run_results.json`` (+
``manifest.json``), and exits 0 or non-0 on demand. That lets us assert the
backend (1) builds the right argv, (2) runs in its own process group, (3) scrubs
``ANTHROPIC_API_KEY``, (4) normalizes per-model status via the substrate, (5) is
fail-closed on exit-0-no-artifact, (6) runs concurrent invocations in separate
processes.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import threading
from pathlib import Path

from carve.core.dbt_execution.backend import DbtCommand
from carve.core.dbt_execution.local import LocalDbtBackend
from carve.core.dbt_execution.result import STATUS_ERROR, STATUS_FAILED, STATUS_SUCCESS

_FIXTURES = Path(__file__).resolve().parents[2] / "integrations" / "dbt" / "fixtures"


def _read_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _make_fake_engine(
    tmp_path: Path,
    *,
    run_results_fixture: str | None,
    exit_code: int = 0,
    write_manifest: bool = True,
) -> Path:
    """Write a fake-dbt script that records its invocation and drops artifacts."""
    record_path = tmp_path / "invocation.json"
    run_results_json = _read_fixture(run_results_fixture) if run_results_fixture else ""
    manifest_json = _read_fixture("manifest.json") if write_manifest else ""
    script = tmp_path / "fake_dbt.py"
    script.write_text(
        textwrap.dedent(f"""
        import json, os, sys
        from pathlib import Path

        # Record how we were invoked.
        Path({str(record_path)!r}).write_text(json.dumps({{
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "pgid": os.getpgrp(),
            "has_anthropic_key": "ANTHROPIC_API_KEY" in os.environ,
            "has_anthropic_token": "ANTHROPIC_AUTH_TOKEN" in os.environ,
        }}), encoding="utf-8")

        # dbt writes artifacts under <project-dir>/target. Find --project-dir.
        argv = sys.argv[1:]
        project_dir = Path(os.getcwd())
        if "--project-dir" in argv:
            project_dir = Path(argv[argv.index("--project-dir") + 1])
        target = project_dir / "target"
        target.mkdir(parents=True, exist_ok=True)
        rr = {run_results_json!r}
        if rr:
            (target / "run_results.json").write_text(rr, encoding="utf-8")
        mf = {manifest_json!r}
        if mf:
            (target / "manifest.json").write_text(mf, encoding="utf-8")

        sys.exit({exit_code})
        """),
        encoding="utf-8",
    )
    return script


def _engine_argv_executable(script: Path) -> str:
    # The backend takes one executable string (the injected engine binary). We
    # can't pass `[python, script]` through a single arg, so wrap the
    # interpreter+script in a one-line shim script marked executable.
    shim = script.parent / "dbt"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return str(shim)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: analytics\n", encoding="utf-8")
    return project


def test_builds_correct_argv_and_normalizes_success(tmp_path: Path) -> None:
    script = _make_fake_engine(tmp_path, run_results_fixture="run_results_green.json")
    engine = _engine_argv_executable(script)
    project = _project(tmp_path)

    backend = LocalDbtBackend(dbt_executable=engine, project_dir=project)
    result = backend.run(
        DbtCommand(command="build", select=("stg_orders",), target="dev", full_refresh=True)
    )

    invocation = json.loads((tmp_path / "invocation.json").read_text())
    argv = invocation["argv"]
    assert argv[0] == "build"
    assert "--select" in argv and "stg_orders" in argv
    assert argv[argv.index("--target") + 1] == "dev"
    assert "--full-refresh" in argv
    assert argv[argv.index("--project-dir") + 1] == str(project.resolve())

    assert result.status == STATUS_SUCCESS
    assert {pm.name for pm in result.per_model} >= {"stg_orders", "dim_orders"}


def test_runs_in_own_process_group(tmp_path: Path) -> None:
    script = _make_fake_engine(tmp_path, run_results_fixture="run_results_green.json")
    engine = _engine_argv_executable(script)
    project = _project(tmp_path)

    LocalDbtBackend(dbt_executable=engine, project_dir=project).run(DbtCommand(command="build"))

    invocation = json.loads((tmp_path / "invocation.json").read_text())
    # The child is its own session leader (start_new_session=True) -> its pgid
    # differs from the test process's group.
    assert invocation["pgid"] != os.getpgrp()


def test_scrubs_anthropic_key_from_child_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-secret")
    script = _make_fake_engine(tmp_path, run_results_fixture="run_results_green.json")
    engine = _engine_argv_executable(script)
    project = _project(tmp_path)

    LocalDbtBackend(dbt_executable=engine, project_dir=project).run(DbtCommand(command="build"))

    invocation = json.loads((tmp_path / "invocation.json").read_text())
    assert invocation["has_anthropic_key"] is False
    assert invocation["has_anthropic_token"] is False


def test_failing_run_normalizes_to_failed(tmp_path: Path) -> None:
    script = _make_fake_engine(
        tmp_path, run_results_fixture="run_results_failing.json", exit_code=1
    )
    engine = _engine_argv_executable(script)
    project = _project(tmp_path)

    result = LocalDbtBackend(dbt_executable=engine, project_dir=project).run(
        DbtCommand(command="build")
    )

    assert result.status == STATUS_FAILED
    failing = next(pm for pm in result.per_model if pm.status == "fail")
    assert failing.failures == 3


def test_fail_closed_on_exit_zero_no_artifact(tmp_path: Path) -> None:
    # Engine exits 0 but writes NO run_results.json -> not trusted as green.
    script = _make_fake_engine(
        tmp_path, run_results_fixture=None, exit_code=0, write_manifest=False
    )
    engine = _engine_argv_executable(script)
    project = _project(tmp_path)

    result = LocalDbtBackend(dbt_executable=engine, project_dir=project).run(
        DbtCommand(command="build")
    )
    assert result.status == STATUS_ERROR


def test_external_env_passes_profiles_dir(tmp_path: Path) -> None:
    script = _make_fake_engine(tmp_path, run_results_fixture="run_results_green.json")
    engine = _engine_argv_executable(script)
    project = _project(tmp_path)
    profiles = tmp_path / "profiles"
    profiles.mkdir()

    LocalDbtBackend(
        dbt_executable=engine,
        project_dir=project,
        env="external",
        profiles_dir=profiles,
    ).run(DbtCommand(command="build"))

    invocation = json.loads((tmp_path / "invocation.json").read_text())
    argv = invocation["argv"]
    assert argv[argv.index("--profiles-dir") + 1] == str(profiles.resolve())


def test_concurrent_invocations_run_in_separate_processes(tmp_path: Path) -> None:
    # Two backends pointed at two projects; each fake engine records its pid via
    # pgid. Run concurrently and assert two distinct process groups.
    results: dict[str, int] = {}

    def _run(tag: str) -> None:
        sub = tmp_path / tag
        sub.mkdir()
        script = _make_fake_engine(sub, run_results_fixture="run_results_green.json")
        engine = _engine_argv_executable(script)
        project = _project(sub)
        LocalDbtBackend(dbt_executable=engine, project_dir=project).run(DbtCommand(command="build"))
        invocation = json.loads((sub / "invocation.json").read_text())
        results[tag] = invocation["pgid"]

    t1 = threading.Thread(target=_run, args=("a",))
    t2 = threading.Thread(target=_run, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    assert results["a"] != results["b"]
