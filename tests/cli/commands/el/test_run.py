"""Tests for `carve el run` (P1-07).

The bulk of the path-resolution / target-injection / Run-row coverage
drives the orchestrator's `run_pipeline_by_name` directly so the tests
don't need to spin up a typer harness for every assertion. The
deprecated alias and watch-mode tests do go through the typer runner /
high-level API where the behavior is the surface contract.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from carve.cli.commands.el import run as el_run
from carve.cli.orchestrator.runner import run_pipeline_by_name
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
    SnowflakeConnection,
)
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

# Module-scoped venv cache so the slow `python -m venv` call only fires
# once across these tests.
_VENV_CACHE: dict[str, Any] = {}


@pytest.fixture(scope="module")
def venv_cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cached = _VENV_CACHE.get("p")
    if cached is None:
        cached = tmp_path_factory.mktemp("el-run-venv-cache")
        _VENV_CACHE["p"] = cached
    return cached


def _snowflake_section(target: str) -> SnowflakeConnection:
    return SnowflakeConnection(
        account=f"{target}-account",
        user=f"{target}-user",
        password="x",
        role="r",
        warehouse="w",
        database="d",
    )


def _make_config(
    *,
    venv_cache_dir: Path,
    state_db: str,
    targets: tuple[str, ...] = ("dev",),
    default_target: str = "dev",
) -> Config:
    return Config(
        project=ProjectConfig(name="el-test", default_target=default_target),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(
            venv_cache_dir=str(venv_cache_dir), default_timeout_seconds=60
        ),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(
            snowflake={t: _snowflake_section(t) for t in targets}
        ),
        config_hash="cafef00dbeefcafe",
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    # Flat layout — P1.1-01.
    (tmp_path / "el").mkdir(parents=True)
    # `pipelines/` dir survives as the legacy-removed sentinel target.
    (tmp_path / "pipelines").mkdir()
    (tmp_path / ".carve" / "plans").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def repository(
    project_dir: Path,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> Repository:
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev", "prod"),
    )
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plant_target_artifact(
    project_dir: Path,
    *,
    target: str,
    name: str,
    body: str,
) -> Path:
    """Plant an artifact under the flat `el/<name>/` tree (P1.1-01).

    ``target`` is accepted for signature parity with pre-P1.1 tests;
    the on-disk path is target-agnostic now.
    """
    del target
    artifact_dir = project_dir / "el" / name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "main.py").write_text(body)
    (artifact_dir / "requirements.txt").write_text("")
    return artifact_dir


def _plant_legacy_pipelines_artifact(
    project_dir: Path,
    *,
    name: str,
    body: str,
) -> Path:
    pipeline_dir = project_dir / "pipelines" / name
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    (pipeline_dir / "main.py").write_text(body)
    (pipeline_dir / "requirements.txt").write_text("")
    return pipeline_dir


# ---------------------------------------------------------------------------
# Path resolution + target awareness
# ---------------------------------------------------------------------------


def test_el_run_resolves_artifact_in_active_target(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """`carve el run iowa_liquor` reads from `el/iowa_liquor/main.py`
    (P1.1-01 flat layout)."""
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    _plant_target_artifact(
        project_dir,
        target="dev",
        name="iowa_liquor",
        body="print('iowa from dev')\n",
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="iowa_liquor",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 0
    assert "iowa from dev" in console.export_text()


def test_el_run_target_flag_stamps_run_row(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """`--target prod` does NOT change the on-disk path (flat layout)
    but still flows through to the runtime: the run row records
    `target=prod` and the subprocess sees `CARVE_ACTIVE_TARGET=PROD`."""
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev", "prod"),
    )
    _plant_target_artifact(
        project_dir,
        target="dev",
        name="iowa_liquor",
        body=(
            "import os\n"
            "print('CARVE_ACTIVE_TARGET=' + os.environ['CARVE_ACTIVE_TARGET'])\n"
        ),
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="iowa_liquor",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
        target="prod",
    )
    assert exit_code == 0
    output = console.export_text()
    assert "CARVE_ACTIVE_TARGET=PROD" in output

    runs = repository.list_runs(pipeline_name="iowa_liquor")
    assert len(runs) == 1
    assert runs[0].target == "prod"


def test_el_run_carve_active_target_env_var_uppercase(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """The subprocess sees `CARVE_ACTIVE_TARGET=DEV` (uppercased).

    Asserts the user script's printout matches so this is a true
    end-to-end check of the env-var injection.
    """
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    _plant_target_artifact(
        project_dir,
        target="dev",
        name="env_check",
        body=(
            "import os\n"
            "print('CARVE_ACTIVE_TARGET=' + os.environ['CARVE_ACTIVE_TARGET'])\n"
            "print('CARVE_PIPELINE_NAME=' + os.environ['CARVE_PIPELINE_NAME'])\n"
            "assert os.environ['CARVE_RUN_ID']\n"
        ),
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="env_check",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 0
    output = console.export_text()
    assert "CARVE_ACTIVE_TARGET=DEV" in output
    assert "CARVE_PIPELINE_NAME=env_check" in output


def test_el_run_legacy_pipelines_fallback_removed(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """P1.1-01: the M1.1-06 `pipelines/<name>/` fallback is GONE.

    A script planted only under `pipelines/<name>/` is not recognized;
    the runner exits 2 with "no such artifact".
    """
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    _plant_legacy_pipelines_artifact(
        project_dir,
        name="legacy_only",
        body="print('should never run')\n",
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="legacy_only",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 2
    output = console.export_text()
    assert "should never run" not in output
    assert "No EL artifact" in output


def test_el_run_missing_artifact_exits_2(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="absent",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 2
    output = console.export_text()
    assert "No EL artifact" in output
    assert "carve el list" in output


# ---------------------------------------------------------------------------
# Run-row semantics
# ---------------------------------------------------------------------------


def test_el_run_creates_run_row_with_target(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """`runs.target` is stamped with the resolved active target."""
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev", "prod"),
    )
    _plant_target_artifact(
        project_dir,
        target="prod",
        name="ingest",
        body="print('ok')\n",
    )
    exit_code = run_pipeline_by_name(
        pipeline_name="ingest",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=Console(record=True, width=120),
        target="prod",
    )
    assert exit_code == 0
    runs = repository.list_runs(pipeline_name="ingest")
    assert len(runs) == 1
    assert runs[0].target == "prod"


def test_el_run_re_runnable(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """No replay guard — running an artifact twice is the expected operation."""
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    _plant_target_artifact(
        project_dir,
        target="dev",
        name="rerun",
        body="print('one')\n",
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


def test_el_run_target_id_references_most_recent_build(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """When a successful Build exists, `runs.target_id` points at it.

    Spec phrases this as "NULL when no Build", but `Run.target_id` is
    a non-null column on the schema (used as a free-form key), so the
    no-build branch falls back to the pipeline name. The asserted
    contract: when a build exists, target_id == build.id.
    """
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    _plant_target_artifact(
        project_dir,
        target="dev",
        name="with_build",
        body="print('ok')\n",
    )
    # Seed a Plan + Build so `latest_build_for` returns something.
    # Pipeline first — Postgres enforces the plans.pipeline_name FK that
    # SQLite ignored.
    repository.create_or_update_pipeline(
        name="with_build", description="", pipeline_dir="el/with_build"
    )
    repository.save_plan(_make_plan("plan_target_id", "with_build"))
    build = repository.create_build(
        pipeline_name="with_build", plan_id="plan_target_id", target="dev"
    )
    repository.set_pipeline_current_build("with_build", build.id)

    exit_code = run_pipeline_by_name(
        pipeline_name="with_build",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=Console(record=True, width=120),
    )
    assert exit_code == 0
    runs = repository.list_runs(pipeline_name="with_build")
    assert runs[0].target_id == build.id

    # No-build artifact: target_id falls back to pipeline name.
    _plant_target_artifact(
        project_dir,
        target="dev",
        name="no_build",
        body="print('ok')\n",
    )
    exit_code = run_pipeline_by_name(
        pipeline_name="no_build",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=Console(record=True, width=120),
    )
    assert exit_code == 0
    runs_no_build = repository.list_runs(pipeline_name="no_build")
    if runs_no_build:
        # Pipeline row may or may not have been created; the run.target_id
        # is "no_build" (the fallback) regardless.
        assert runs_no_build[0].target_id == "no_build"


def _make_plan(plan_id: str, pipeline_name: str) -> Any:
    """Build a Plan ORM row for tests without importing it at module load.

    Returns ``Any`` so the seed-block type-check doesn't complain about
    ORM internals leaking into the test signature.
    """
    from carve.core.state import Plan as _Plan

    return _Plan(
        id=plan_id,
        goal="g",
        config_hash="h",
        carve_version="0.0.1",
        # v0.1-01: task_graph_json is JSONB; expects a dict, not a string.
        task_graph_json={},
        file_path="x",
        phase="built",
        pipeline_name=pipeline_name,
    )


# ---------------------------------------------------------------------------
# Safety rails
# ---------------------------------------------------------------------------


def test_active_target_not_defined_exits_2(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """`--target foo` without a `[snowflake.foo]` section exits 2 pre-spawn."""
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="anything",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
        target="not_defined",
    )
    assert exit_code == 2
    assert "not defined" in console.export_text()


def test_project_root_containment_enforced(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """A pathological pipeline name that escapes the project root is refused."""
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    console = Console(record=True, width=120)
    exit_code = run_pipeline_by_name(
        pipeline_name="../../escape",
        config=config,
        project_dir=project_dir,
        repository=repository,
        console=console,
    )
    assert exit_code == 1
    assert "escapes project root" in console.export_text()


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------


def test_el_run_watch_reruns_on_file_change(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """`--watch` triggers a fresh Run when a file in the artifact dir changes.

    Uses `_SyncObserver` + a ``stop_event`` to drive the watch loop
    deterministically: run once eagerly, fire a synthetic file event,
    wait for the second run, then signal stop.
    """
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    _plant_target_artifact(
        project_dir,
        target="dev",
        name="watched",
        body="print('watched')\n",
    )

    sync_observer = el_run._SyncObserver()
    stop = threading.Event()
    state: dict[str, Any] = {"exit_code": None, "exc": None}

    def runner() -> None:
        try:
            state["exit_code"] = el_run._run_with_watch(
                name="watched",
                target=None,
                config=config,
                project_dir=project_dir,
                repository=repository,
                observer_factory=lambda: sync_observer,
                stop_event=stop,
            )
        except BaseException as exc:  # pragma: no cover - debugging
            state["exc"] = exc

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()

    # Poll until the first run has landed in the DB.
    waited = 0.0
    while waited < 5.0:
        if len(repository.list_runs(pipeline_name="watched")) >= 1:
            break
        threading.Event().wait(0.05)
        waited += 0.05
    assert len(repository.list_runs(pipeline_name="watched")) >= 1

    # Fire a change event; the debounced handler will set `trigger`,
    # waking the loop for a second run.
    class _FakeEvent:
        is_directory = False

    sync_observer.fire(_FakeEvent())

    waited = 0.0
    while waited < 5.0:
        if len(repository.list_runs(pipeline_name="watched")) >= 2:
            break
        threading.Event().wait(0.05)
        waited += 0.05
    assert len(repository.list_runs(pipeline_name="watched")) >= 2

    stop.set()
    worker.join(timeout=5.0)
    assert state["exc"] is None
    assert state["exit_code"] == 0


def test_el_run_watch_exits_on_ctrl_c(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """Stop-signal between runs terminates the watch loop with the last exit code.

    Tests can't safely raise ``KeyboardInterrupt`` in pytest's main
    thread, so we model "Ctrl-C" with the same ``stop_event`` escape
    hatch the production loop ignores. The contract under test is "the
    loop exits cleanly between runs and returns the most recent exit
    code".
    """
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    _plant_target_artifact(
        project_dir,
        target="dev",
        name="watched_exit",
        body="print('watched_exit')\n",
    )

    sync_observer = el_run._SyncObserver()
    stop = threading.Event()
    state: dict[str, Any] = {"exit_code": None, "exc": None}

    def runner() -> None:
        try:
            state["exit_code"] = el_run._run_with_watch(
                name="watched_exit",
                target=None,
                config=config,
                project_dir=project_dir,
                repository=repository,
                observer_factory=lambda: sync_observer,
                stop_event=stop,
            )
        except BaseException as exc:  # pragma: no cover
            state["exc"] = exc

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()

    waited = 0.0
    while waited < 5.0:
        if len(repository.list_runs(pipeline_name="watched_exit")) >= 1:
            break
        threading.Event().wait(0.05)
        waited += 0.05
    assert len(repository.list_runs(pipeline_name="watched_exit")) >= 1

    stop.set()
    worker.join(timeout=5.0)
    assert state["exit_code"] == 0


def test_el_run_watch_picks_up_requirements_change(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """A change to `requirements.txt` triggers a re-run; venv re-resolves.

    Asserts the second run picks up the new requirements via the
    cache key. Since the test artifact has empty requirements both
    times, what we really check is that the watcher fires for any
    file in the directory — `requirements.txt` modification included.
    """
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    _plant_target_artifact(
        project_dir,
        target="dev",
        name="reqs_watched",
        body="print('hello')\n",
    )

    sync_observer = el_run._SyncObserver()
    stop = threading.Event()
    state: dict[str, Any] = {"exit_code": None}

    def runner() -> None:
        state["exit_code"] = el_run._run_with_watch(
            name="reqs_watched",
            target=None,
            config=config,
            project_dir=project_dir,
            repository=repository,
            observer_factory=lambda: sync_observer,
            stop_event=stop,
        )

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()

    waited = 0.0
    while waited < 5.0:
        if len(repository.list_runs(pipeline_name="reqs_watched")) >= 1:
            break
        threading.Event().wait(0.05)
        waited += 0.05
    assert len(repository.list_runs(pipeline_name="reqs_watched")) >= 1

    # Change requirements.txt and fire a synthetic event referring to it.
    (project_dir / "el" / "reqs_watched" / "requirements.txt").write_text(
        "# bumped\n"
    )

    class _FakeEvent:
        is_directory = False
        src_path = str(
            project_dir / "el" / "reqs_watched" / "requirements.txt"
        )

    sync_observer.fire(_FakeEvent())

    waited = 0.0
    while waited < 5.0:
        if len(repository.list_runs(pipeline_name="reqs_watched")) >= 2:
            break
        threading.Event().wait(0.05)
        waited += 0.05
    assert len(repository.list_runs(pipeline_name="reqs_watched")) >= 2

    stop.set()
    worker.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


# The deprecated `carve run <name>` alias was removed in dogfooding
# (it silently swallowed the top-level `--target` flag because typer
# rebuilt the option in the alias's own signature). The replacement is
# `carve el run` and only `carve el run`. The top-level removal is
# pinned by `tests/test_cli.py::test_top_level_run_command_is_gone`.


# Sanity check — the in-process os.environ shouldn't be polluted by our
# subprocesses (they set CARVE_ACTIVE_TARGET in the *child* env only).
def test_carve_active_target_not_set_in_parent_after_run() -> None:
    assert "CARVE_ACTIVE_TARGET" not in os.environ


def test_el_run_watch_returns_sentinel_when_stopped_before_first_run(
    project_dir: Path,
    repository: Repository,
    venv_cache_dir: Path,
    postgres_state_store_url: str,
) -> None:
    """If `stop_event` is set before any iteration runs, the sentinel
    return value (-1) is returned — distinguishing 'watch never ran' from
    'watch ran and the last run succeeded' (0)."""
    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )
    _plant_target_artifact(
        project_dir, target="dev", name="never_runs", body="print('x')\n"
    )

    sync_observer = el_run._SyncObserver()
    stop = threading.Event()
    stop.set()  # signal BEFORE invoking

    exit_code = el_run._run_with_watch(
        name="never_runs",
        target=None,
        config=config,
        project_dir=project_dir,
        repository=repository,
        observer_factory=lambda: sync_observer,
        stop_event=stop,
    )
    assert exit_code == -1
    # Confirms no Run row was created either.
    assert repository.list_runs(pipeline_name="never_runs") == []


def test_el_run_watch_refuses_symlinked_artifact_dir_outside_project(
    repository: Repository,
    venv_cache_dir: Path,
    tmp_path_factory: pytest.TempPathFactory, postgres_state_store_url: str
) -> None:
    """A symlinked artifact directory pointing outside the project root is
    refused with exit 2 before the watcher schedules. Defense-in-depth:
    each loop iteration re-validates via ``run_pipeline_by_name``, but
    the watcher would still fire on out-of-tree file events.

    This test deliberately uses two separate tmp roots so the symlink
    target really is outside the project — the project_dir fixture
    aliases tmp_path, so we can't reuse it for the "outside" target.
    """
    project_root = tmp_path_factory.mktemp("project")
    (project_root / "el").mkdir(parents=True)
    (project_root / ".carve" / "plans").mkdir(parents=True)

    # A second tmp root, truly outside the project root.
    outside_root = tmp_path_factory.mktemp("outside")
    outside = outside_root / "evil"
    outside.mkdir(parents=True)
    (outside / "main.py").write_text("print('escape')\n", encoding="utf-8")
    (outside / "requirements.txt").write_text("", encoding="utf-8")

    el_root = project_root / "el"
    link = el_root / "escape"
    link.symlink_to(outside)

    config = _make_config(
        venv_cache_dir=venv_cache_dir,
        state_db=postgres_state_store_url,
        targets=("dev",),
    )

    sync_observer = el_run._SyncObserver()
    stop = threading.Event()

    exit_code = el_run._run_with_watch(
        name="escape",
        target=None,
        config=config,
        project_dir=project_root,
        repository=repository,
        observer_factory=lambda: sync_observer,
        stop_event=stop,
    )
    assert exit_code == 2
