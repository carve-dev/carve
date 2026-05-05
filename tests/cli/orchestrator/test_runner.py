"""Unit tests for `cli.orchestrator.runner` (M1.1-06).

Each test plants a real script under ``pipelines/<name>/main.py`` and
the corresponding `Pipeline` row, then runs the script through
`LocalVenvRunner`. Requirements lists are empty so no pip work happens
during the test — only venv creation, which is amortised through a
module-scoped cache fixture.

Replay-guard tests from the M1 applier are gone; the new contract is:
re-runs are first-class.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from carve.cli.orchestrator.runner import (
    run_pipeline_by_name,
    run_pipeline_by_plan,
)
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
)
from carve.core.state import Plan, Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

# Module-scoped venv cache so the slow `python -m venv` call only fires
# once across the runner tests.
_VENV_CACHE_TMPDIR: dict[str, Any] = {}


@pytest.fixture(scope="module")
def venv_cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cached = _VENV_CACHE_TMPDIR.get("p")
    if cached is None:
        cached = tmp_path_factory.mktemp("runner-venv-cache")
        _VENV_CACHE_TMPDIR["p"] = cached
    return cached


def _config(*, venv_cache_dir: Path, state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="runner-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(venv_cache_dir=str(venv_cache_dir), default_timeout_seconds=60),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(snowflake={}),
        config_hash="cafef00dbeefcafe",
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / "pipelines").mkdir()
    (tmp_path / ".carve" / "plans").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def repository(project_dir: Path, venv_cache_dir: Path) -> Repository:
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


def _plant_pipeline(
    project_dir: Path,
    repository: Repository,
    *,
    pipeline_name: str,
    script_body: str,
    plan_id: str | None = None,
) -> str:
    """Write `pipelines/<name>/main.py` plus the corresponding rows."""
    pipeline_dir = project_dir / "pipelines" / pipeline_name
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    (pipeline_dir / "main.py").write_text(script_body)

    if plan_id is not None:
        plan = Plan(
            id=plan_id,
            goal="seed",
            config_hash="cafef00dbeefcafe",
            carve_version="0.0.1",
            estimates_json="{}",
            task_graph_json="{}",
            file_path=f".carve/plans/{plan_id}.json",
            phase="built",
            pipeline_name=pipeline_name,
            deployed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        repository.save_plan(plan)

    repository.create_or_update_pipeline(
        name=pipeline_name,
        description="",
        pipeline_dir=f"pipelines/{pipeline_name}",
        current_plan_id=plan_id,
    )
    return pipeline_name


# ---------------------------------------------------------------- happy path


def test_run_by_name_executes_and_records_success(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    _plant_pipeline(
        project_dir,
        repository,
        pipeline_name="ok_pipeline",
        script_body="print('hello from pipeline')\n",
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="ok_pipeline",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 0
    output = console.export_text()
    assert "hello from pipeline" in output
    assert "Run succeeded" in output

    runs = repository.list_runs(pipeline_name="ok_pipeline")
    assert len(runs) == 1
    assert runs[0].status == "success"

    pipeline = repository.get_pipeline("ok_pipeline")
    assert pipeline is not None
    assert pipeline.last_run_status == "success"
    assert pipeline.last_run_id is not None


def test_run_by_name_unknown_pipeline_exits_2(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="not_real",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 2
    assert "not found" in console.export_text().lower()


def test_run_by_name_failed_subprocess_returns_1(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    _plant_pipeline(
        project_dir,
        repository,
        pipeline_name="boom",
        script_body="import sys\nsys.exit(7)\n",
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="boom",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 1
    pipeline = repository.get_pipeline("boom")
    assert pipeline is not None
    assert pipeline.last_run_status == "failed"


def test_re_running_a_successful_pipeline_succeeds(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    """No replay guard — re-runs are the expected operation."""
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    _plant_pipeline(
        project_dir,
        repository,
        pipeline_name="rerun",
        script_body="print('first')\n",
    )
    first = run_pipeline_by_name(
        pipeline_name="rerun",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=Console(record=True, width=120),
    )
    assert first == 0
    second = run_pipeline_by_name(
        pipeline_name="rerun",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=Console(record=True, width=120),
    )
    assert second == 0
    runs = repository.list_runs(pipeline_name="rerun")
    assert len(runs) == 2
    assert all(r.status == "success" for r in runs)


def test_run_by_plan_resolves_to_pipeline(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    """`carve run --plan <id>` runs the pipeline that the plan built."""
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    plan_id = "plan_20260101_000000_aaaaaa"
    _plant_pipeline(
        project_dir,
        repository,
        pipeline_name="from_plan",
        script_body="print('from plan')\n",
        plan_id=plan_id,
    )
    repository.create_or_update_pipeline(
        name="from_plan",
        description="",
        pipeline_dir="pipelines/from_plan",
        current_plan_id=plan_id,
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_plan(
        plan_id=plan_id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 0
    assert "from plan" in console.export_text()


def test_run_by_plan_unknown_plan_exits_2(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_plan(
        plan_id="plan_20260101_000000_999999",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 2


def test_run_by_plan_invalid_format_exits_2(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_plan(
        plan_id="not-a-real-plan-id",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 2
    assert "Invalid plan id format" in console.export_text()


def test_run_by_plan_drafted_plan_exits_2(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    """A plan still in `drafted` phase has no pipeline_name; we refuse to run."""
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    plan = Plan(
        id="plan_20260101_000000_dadbed",
        goal="g",
        config_hash="h",
        carve_version="0.0.1",
        estimates_json="{}",
        task_graph_json="{}",
        file_path=".carve/plans/x.json",
        phase="drafted",
    )
    repository.save_plan(plan)
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_plan(
        plan_id=plan.id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 2
    assert "has not been built" in console.export_text()


def test_run_by_name_missing_main_py_exits_1(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    """Pipeline row exists but the on-disk main.py is gone."""
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    repository.create_or_update_pipeline(
        name="orphan",
        description="",
        pipeline_dir="pipelines/orphan",
        current_plan_id=None,
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="orphan",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 1
    assert "main.py" in console.export_text()


def test_run_by_name_pipeline_dir_escapes_project_root_exits_1(
    project_dir: Path, repository: Repository, venv_cache_dir: Path
) -> None:
    """A Pipeline row with `pipeline_dir="../escape"` is refused with exit 1.

    Defense-in-depth: even if a malformed pipeline_dir somehow lands in
    the state store (legacy migration backfill, hand-edited DB) the
    runner refuses to execute anything that resolves outside
    ``project_dir``.
    """
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    repository.create_or_update_pipeline(
        name="escapee",
        description="",
        pipeline_dir="../escape",
        current_plan_id=None,
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="escapee",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 1
    assert "escapes project root" in console.export_text()


def test_runner_module_does_not_export_apply_plan() -> None:
    """The replay-gated `apply_plan` API is gone in M1.1-06."""
    import carve.cli.orchestrator.runner as runner_mod

    assert hasattr(runner_mod, "run_pipeline_by_name")
    assert hasattr(runner_mod, "run_pipeline_by_plan")
    assert not hasattr(runner_mod, "apply_plan")


# ----------------------------------------------------- same-tick log streaming


def test_run_surfaces_logs_appended_within_same_tick(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two logs sharing a `datetime.now()` tick must both reach stdout."""
    fixed_dt = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(
        "carve.core.state.models._utcnow",
        lambda: fixed_dt,
    )

    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    _plant_pipeline(
        project_dir,
        repository,
        pipeline_name="twolines",
        script_body="print('first')\nprint('second')\n",
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="twolines",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 0
    output = console.export_text()
    assert "first" in output
    assert "second" in output
