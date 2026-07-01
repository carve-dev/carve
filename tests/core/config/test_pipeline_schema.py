"""Schema + loader tests for ``pipelines/<name>.toml``.

Covers the *pipelines* spec's Unit (schema) + Unit (seed schedule) bars:
valid TOML loads cleanly; the invalid-TOML matrix (missing fields, a dlt
step without ``component``, unknown step type, duplicate ids, dangling
``depends_on``, cycle, bad cron) raises structured ``PipelineError``s; the
retired ``artifact`` key gets a migration-pointing message; and the
``[seed_schedule]`` parse incl. the ``paused``/``enabled`` rejection and
the missing-block -> unscheduled behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import (
    ConflictingWorkerLabelsError,
    DbtStepConfig,
    DltStepConfig,
    PipelineError,
    SqlStepConfig,
    load_pipeline,
)
from carve.core.config.schema import ComponentConfig


@pytest.fixture
def project(tmp_path: Path) -> ProjectPaths:
    """A bare control-plane root with the flat dirs created."""
    for sub in ("el", "pipelines", "carve", ".carve", ".dlt"):
        (tmp_path / sub).mkdir()
    return ProjectPaths.from_root(tmp_path)


def _write_pipeline(paths: ProjectPaths, name: str, body: str) -> Path:
    path = paths.pipelines_dir / f"{name}.toml"
    path.write_text(body)
    return path


def _make_el_component(paths: ProjectPaths, name: str) -> None:
    d = paths.el_dir / name
    d.mkdir()
    (d / "__init__.py").write_text("# dlt source\n")


def _make_dbt_project(paths: ProjectPaths, subdir: str = "analytics") -> None:
    d = paths.root / subdir
    d.mkdir()
    (d / "dbt_project.yml").write_text("name: analytics\n")


# ---------------------------------------------------------------------------
# Valid load
# ---------------------------------------------------------------------------


def test_valid_pipeline_loads_cleanly(project: ProjectPaths) -> None:
    _make_el_component(project, "stripe_charges")
    _make_dbt_project(project)
    path = _write_pipeline(
        project,
        "stripe",
        """
[pipeline]
description = "Stripe ingest + staging + refresh"
owner = "data-team"

[seed_schedule]
cron = "0 2 * * *"
timezone = "UTC"
target = "prod"

[[steps]]
id = "ingest_stripe"
type = "dlt"
component = "stripe_charges"
depends_on = []
[steps.failure_mode]
mode = "retry"
max_attempts = 3
backoff = "exponential"

[[steps]]
id = "stage_stripe"
type = "dbt"
component = "analytics"
command = "build"
select = "stg_stripe_charges+"
depends_on = ["ingest_stripe"]

[[steps]]
id = "refresh_search"
type = "sql"
file = "sql/refresh.sql"
connection = "prod"
depends_on = ["stage_stripe"]
[steps.failure_mode]
mode = "warn"
""",
    )
    pipeline = load_pipeline(path, components={}, paths=project)

    assert pipeline.name == "stripe"
    assert pipeline.pipeline.owner == "data-team"
    assert pipeline.seed_schedule is not None
    assert pipeline.seed_schedule.cron == "0 2 * * *"
    assert [s.id for s in pipeline.steps] == ["ingest_stripe", "stage_stripe", "refresh_search"]

    ingest, stage, refresh = pipeline.steps
    assert isinstance(ingest, DltStepConfig)
    assert ingest.component == "stripe_charges"
    assert ingest.failure_mode.mode == "retry"
    assert ingest.failure_mode.max_attempts == 3
    assert isinstance(stage, DbtStepConfig)
    assert stage.component == "analytics"
    assert stage.command == "build"
    assert isinstance(refresh, SqlStepConfig)
    assert refresh.file == "sql/refresh.sql"
    assert refresh.failure_mode.mode == "warn"


def test_name_derived_from_filename_not_body(project: ProjectPaths) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "my_pipeline",
        """
[[steps]]
id = "ingest"
type = "dlt"
component = "src"
""",
    )
    pipeline = load_pipeline(path, components={}, paths=project)
    assert pipeline.name == "my_pipeline"


# ---------------------------------------------------------------------------
# Invalid: missing required fields / step config
# ---------------------------------------------------------------------------


def test_dlt_step_missing_component_is_rejected(project: ProjectPaths) -> None:
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
id = "ingest"
type = "dlt"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert "component" in str(exc.value)


def test_sql_step_missing_file_is_rejected(project: ProjectPaths) -> None:
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
id = "q"
type = "sql"
connection = "prod"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert "file" in str(exc.value)


def test_step_missing_id_is_rejected(project: ProjectPaths) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
type = "dlt"
component = "src"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert "id" in str(exc.value)


def test_unknown_step_type_is_rejected(project: ProjectPaths) -> None:
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
id = "x"
type = "shell"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    msg = str(exc.value)
    assert "type" in msg or "shell" in msg


def test_unknown_top_level_field_is_rejected(project: ProjectPaths) -> None:
    path = _write_pipeline(
        project,
        "p",
        """
schedule = "0 2 * * *"
""",
    )
    with pytest.raises(PipelineError):
        load_pipeline(path, components={}, paths=project)


# ---------------------------------------------------------------------------
# Invalid: DAG integrity
# ---------------------------------------------------------------------------


def test_duplicate_step_ids_rejected(project: ProjectPaths) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
id = "dup"
type = "dlt"
component = "src"

[[steps]]
id = "dup"
type = "dlt"
component = "src"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert "Duplicate step id" in str(exc.value)


def test_missing_depends_on_ref_rejected(project: ProjectPaths) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
id = "a"
type = "dlt"
component = "src"
depends_on = ["ghost"]
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert "ghost" in str(exc.value)


def test_cycle_rejected(project: ProjectPaths) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
id = "a"
type = "dlt"
component = "src"
depends_on = ["b"]

[[steps]]
id = "b"
type = "dlt"
component = "src"
depends_on = ["a"]
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert "cycle" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Invalid: cron
# ---------------------------------------------------------------------------


def test_bad_cron_rejected(project: ProjectPaths) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "p",
        """
[seed_schedule]
cron = "not a cron"

[[steps]]
id = "a"
type = "dlt"
component = "src"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert "cron" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# The retired `artifact` key
# ---------------------------------------------------------------------------


def test_artifact_key_rejected_with_migration_message(project: ProjectPaths) -> None:
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
id = "ingest"
type = "dlt"
artifact = "stripe_charges"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    message = str(exc.value)
    assert "artifact" in message
    assert "component" in message  # the migration pointer


# ---------------------------------------------------------------------------
# Seed schedule
# ---------------------------------------------------------------------------


def test_seed_schedule_parses(project: ProjectPaths) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "p",
        """
[seed_schedule]
cron = "*/15 * * * *"
timezone = "America/New_York"
target = "staging"

[[steps]]
id = "a"
type = "dlt"
component = "src"
""",
    )
    pipeline = load_pipeline(path, components={}, paths=project)
    assert pipeline.seed_schedule is not None
    assert pipeline.seed_schedule.cron == "*/15 * * * *"
    assert pipeline.seed_schedule.timezone == "America/New_York"
    assert pipeline.seed_schedule.target == "staging"


def test_missing_seed_schedule_is_unscheduled(project: ProjectPaths) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
id = "a"
type = "dlt"
component = "src"
""",
    )
    pipeline = load_pipeline(path, components={}, paths=project)
    assert pipeline.seed_schedule is None


@pytest.mark.parametrize("key", ["paused", "enabled"])
def test_seed_schedule_rejects_live_data_keys(project: ProjectPaths, key: str) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "p",
        f"""
[seed_schedule]
cron = "0 2 * * *"
{key} = true

[[steps]]
id = "a"
type = "dlt"
component = "src"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert key in str(exc.value)


# ---------------------------------------------------------------------------
# `target` is seedable (open question -> kept, default "prod")
# ---------------------------------------------------------------------------


def test_seed_schedule_target_defaults_to_prod(project: ProjectPaths) -> None:
    _make_el_component(project, "src")
    path = _write_pipeline(
        project,
        "p",
        """
[seed_schedule]
cron = "0 2 * * *"

[[steps]]
id = "a"
type = "dlt"
component = "src"
""",
    )
    pipeline = load_pipeline(path, components={}, paths=project)
    assert pipeline.seed_schedule is not None
    assert pipeline.seed_schedule.target == "prod"


def test_multi_mode_component_block_resolves(project: ProjectPaths) -> None:
    """A separate-local dlt component block resolves at load time."""
    external = project.root.parent / "stripe_repo"
    external.mkdir()
    (external / "__init__.py").write_text("# dlt\n")
    block = ComponentConfig(type="dlt", mode="separate-local", path=str(external))
    path = _write_pipeline(
        project,
        "p",
        """
[[steps]]
id = "a"
type = "dlt"
component = "stripe_charges"
""",
    )
    pipeline = load_pipeline(path, components={"stripe_charges": block}, paths=project)
    assert pipeline.steps[0].component == "stripe_charges"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Worker-placement labels: author-time conflict reject
# ---------------------------------------------------------------------------


def _labeled_dlt(label: str) -> ComponentConfig:
    # A same-repo dlt block resolves without an on-disk dir (el_dir/<name>), so it
    # is the lightest way to attach a worker_label that also passes resolution.
    return ComponentConfig(type="dlt", mode="same-repo", worker_label=label)


def test_conflicting_worker_labels_rejected_at_load(project: ProjectPaths) -> None:
    """Two steps whose components require different labels fail to load (author-time).

    One job = one whole-DAG run on one worker, so a mis-labeled pipeline can't be
    placed — ``load_pipeline`` rejects it up front with the typed
    ``ConflictingWorkerLabelsError`` (a ``PipelineError``), never enqueuing it.
    """
    path = _write_pipeline(
        project,
        "conflict",
        """
[[steps]]
id = "ingest"
type = "dlt"
component = "near"

[[steps]]
id = "load"
type = "dlt"
component = "onprem"
""",
    )
    components = {"near": _labeled_dlt("near-source"), "onprem": _labeled_dlt("onprem-dbt")}
    with pytest.raises(ConflictingWorkerLabelsError) as exc:
        load_pipeline(path, components=components, paths=project)
    # It is a PipelineError with the file attached and both labels named.
    assert isinstance(exc.value, PipelineError)
    assert exc.value.file == path
    assert "near-source" in str(exc.value)
    assert "onprem-dbt" in str(exc.value)


def test_single_worker_label_pipeline_loads_cleanly(project: ProjectPaths) -> None:
    """A pipeline whose components agree on one label loads without error."""
    path = _write_pipeline(
        project,
        "agree",
        """
[[steps]]
id = "ingest"
type = "dlt"
component = "near"

[[steps]]
id = "load"
type = "dlt"
component = "also_near"
""",
    )
    components = {"near": _labeled_dlt("onprem"), "also_near": _labeled_dlt("onprem")}
    pipeline = load_pipeline(path, components=components, paths=project)
    assert [s.id for s in pipeline.steps] == ["ingest", "load"]
