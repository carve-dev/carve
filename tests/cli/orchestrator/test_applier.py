"""Unit tests for `cli.orchestrator.applier.apply_plan`.

Each test plants a real plan JSON under ``.carve/plans/`` and a real
script under ``pipelines/`` so the full plan -> python step ->
LocalVenvRunner pipeline runs end-to-end. The runner uses
``requirements=[]`` so no pip work happens during the test — we only
exercise the venv-creation step (a couple of seconds) once per test
process via the shared `venv_cache_dir` fixture.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from carve.cli.orchestrator.applier import apply_plan
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
# once across the apply tests.
_VENV_CACHE_TMPDIR: dict[str, Any] = {}


@pytest.fixture(scope="module")
def venv_cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cached = _VENV_CACHE_TMPDIR.get("p")
    if cached is None:
        cached = tmp_path_factory.mktemp("apply-venv-cache")
        _VENV_CACHE_TMPDIR["p"] = cached
    return cached


def _config(*, venv_cache_dir: Path, state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="apply-test"),
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
    config = _config(venv_cache_dir=venv_cache_dir, state_db="sqlite:///.carve/state.db")
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


def _plant_plan(
    project_dir: Path,
    repository: Repository,
    *,
    plan_id: str,
    script_relpath: str,
    script_body: str,
    requirements: list[str] | None = None,
    config_hash: str = "cafef00dbeefcafe",
) -> Plan:
    """Write the plan JSON, the script, and the index row.

    Mirrors what the planner would produce. Tests then call apply_plan
    against the resulting plan_id.
    """
    requirements = requirements or []
    script_path = project_dir / script_relpath
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script_body)

    file_path = project_dir / ".carve" / "plans" / f"{plan_id}.json"
    payload = {
        "id": plan_id,
        "goal": "test",
        "summary": "test summary",
        "pipeline_name": script_path.parent.name,
        "pipeline_dir": str(script_path.parent.relative_to(project_dir)),
        "script_path": script_relpath,
        "requirements_path": str(
            (script_path.parent / "requirements.txt").relative_to(project_dir)
        ),
        "requirements": requirements,
        "files_written": [script_relpath],
        "config_hash": config_hash,
        "carve_version": "0.0.1",
        "tokens_input": 100,
        "tokens_output": 50,
        "cost_usd": 0.001,
        "model": "claude-sonnet-4-5",
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
    }
    file_path.write_text(json.dumps(payload, indent=2))

    plan_row = Plan(
        id=plan_id,
        goal="test",
        config_hash=config_hash,
        carve_version="0.0.1",
        estimates_json=json.dumps({"tokens_input": 100, "tokens_output": 50}),
        task_graph_json=json.dumps(
            {"script_path": script_relpath, "requirements": requirements}
        ),
        file_path=str(file_path),
    )
    repository.save_plan(plan_row)
    return plan_row


# ----------------------------------------------------------------- happy path


def test_apply_runs_step_and_records_success(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
) -> None:
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    plan_id = "plan_20260101_000000_abc123"
    _plant_plan(
        project_dir,
        repository,
        plan_id=plan_id,
        script_relpath="pipelines/ok/main.py",
        script_body="print('ok')\n",
    )

    console = Console(record=True, width=120)
    exit_code = apply_plan(
        plan_id=plan_id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )

    assert exit_code == 0
    output = console.export_text()
    assert "ok" in output  # the printed script output
    assert "Run succeeded" in output

    # Plan was marked applied
    plan_row = repository.get_plan(plan_id)
    assert plan_row is not None
    assert plan_row.applied_at is not None
    assert plan_row.apply_run_id is not None

    # The run row exists and is success
    run = repository.get_run(plan_row.apply_run_id)
    assert run is not None
    assert run.status == "success"
    assert run.kind == "apply"


# ------------------------------------------------------------------ failure


def test_apply_records_failure_for_nonzero_exit(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
) -> None:
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    plan_id = "plan_20260101_000000_def456"
    _plant_plan(
        project_dir,
        repository,
        plan_id=plan_id,
        script_relpath="pipelines/boom/main.py",
        script_body="import sys\nsys.exit(7)\n",
    )

    console = Console(record=True, width=120)
    exit_code = apply_plan(
        plan_id=plan_id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )

    assert exit_code == 1
    output = console.export_text()
    assert "failed" in output

    plan_row = repository.get_plan(plan_id)
    assert plan_row is not None
    assert plan_row.apply_run_id is not None
    run = repository.get_run(plan_row.apply_run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.error_message is not None
    assert "7" in run.error_message


# -------------------------------------------------------------- not found


def test_apply_errors_when_plan_not_found(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
) -> None:
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    console = Console(record=True, width=120)
    exit_code = apply_plan(
        plan_id="plan_20260101_000000_999999",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 1
    assert "not found" in console.export_text().lower()


def test_apply_warns_on_config_hash_mismatch(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
) -> None:
    """A drifted config hash is a warning, not a hard error (M1 is forgiving)."""
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    plan_id = "plan_20260101_000000_aaa111"
    _plant_plan(
        project_dir,
        repository,
        plan_id=plan_id,
        script_relpath="pipelines/ok/main.py",
        script_body="print('ok')\n",
        config_hash="differenthashvalue",
    )

    console = Console(record=True, width=120)
    exit_code = apply_plan(
        plan_id=plan_id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )

    assert exit_code == 0
    output = console.export_text()
    assert "Config has changed" in output


# ---------------------------------------------------------------- replay guard


def test_apply_rejects_already_applied_plan(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
) -> None:
    """Re-running an applied plan must abort with a clear error (no --force in M1)."""
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    plan_id = "plan_20260101_000000_bbb222"
    _plant_plan(
        project_dir,
        repository,
        plan_id=plan_id,
        script_relpath="pipelines/ok/main.py",
        script_body="print('ok')\n",
    )

    first = apply_plan(
        plan_id=plan_id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=Console(record=True, width=120),
    )
    assert first == 0

    console = Console(record=True, width=120)
    second = apply_plan(
        plan_id=plan_id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert second == 1
    output = console.export_text()
    assert "already applied" in output
    assert plan_id in output
    assert "generate a new plan" in output


# --------------------------------------------------------- input validation


def test_apply_rejects_malformed_plan_id_with_exit_2(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
) -> None:
    """Malformed plan_id is rejected at the apply boundary with exit code 2."""
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    console = Console(record=True, width=120)
    exit_code = apply_plan(
        plan_id="not-a-real-plan-id",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 2
    assert "Invalid plan id format" in console.export_text()


def test_apply_escapes_rich_markup_in_plan_id(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
) -> None:
    """Rich markup substrings in the rejection message must render literally.

    Format validation already blocks these in practice; this guards the
    second layer (`rich.markup.escape`) so a future regex relaxation
    can't reintroduce a markup-injection corner.
    """
    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    console = Console(record=True, width=120)
    # Synthesised invalid id with rich-markup-shaped substring.
    exit_code = apply_plan(
        plan_id="plan_[evil]_format",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 2
    text = console.export_text()
    # The escaped markup should render as literal `[evil]` in the
    # plain-text export, not be parsed as a (failed) style tag.
    assert "[evil]" in text


# -------------------------------------------------------- same-tick logs


def test_apply_surfaces_logs_appended_within_same_tick(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two logs sharing a `datetime.now()` tick must both reach stdout.

    Pinning `Log.timestamp`'s default factory to a fixed value
    simulates the worst case (clock resolution coarser than the
    runner's log-line cadence). The id-based cursor must survive that.
    """
    fixed_dt = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(
        "carve.core.state.models._utcnow",
        lambda: fixed_dt,
    )

    config = _config(
        venv_cache_dir=venv_cache_dir,
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
    )
    plan_id = "plan_20260101_000000_ccc333"
    # Script prints two lines back-to-back; the runner streams them as
    # two separate log rows.
    _plant_plan(
        project_dir,
        repository,
        plan_id=plan_id,
        script_relpath="pipelines/twolines/main.py",
        script_body="print('first')\nprint('second')\n",
    )

    console = Console(record=True, width=120)
    exit_code = apply_plan(
        plan_id=plan_id,
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 0
    output = console.export_text()
    assert "first" in output
    assert "second" in output
