"""Component-name resolvability validation in ``load_pipeline``.

Covers the *pipelines* spec's Unit (component resolution) bar (the
simple-mode + omitted-dbt + unresolvable cases this unit owns; the
multi-mode workspace-clone parity rides the shipped locator and is proven
in the locator's own tests). The locator does the path math; this tests
that ``load_pipeline`` surfaces an unresolvable name as a ``PipelineError``
and accepts the resolvable ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import PipelineError, load_pipeline


@pytest.fixture
def project(tmp_path: Path) -> ProjectPaths:
    for sub in ("el", "pipelines", "carve", ".carve", ".dlt"):
        (tmp_path / sub).mkdir()
    return ProjectPaths.from_root(tmp_path)


def _make_el(paths: ProjectPaths, name: str) -> None:
    d = paths.el_dir / name
    d.mkdir()
    (d / "__init__.py").write_text("# dlt source\n")


def _make_dbt(paths: ProjectPaths, subdir: str = "analytics") -> None:
    d = paths.root / subdir
    d.mkdir()
    (d / "dbt_project.yml").write_text("name: analytics\n")


def _write(paths: ProjectPaths, body: str) -> Path:
    path = paths.pipelines_dir / "p.toml"
    path.write_text(body)
    return path


def test_dlt_component_resolves_to_el_dir_simple_mode(project: ProjectPaths) -> None:
    _make_el(project, "stripe_charges")
    path = _write(
        project,
        """
[[steps]]
id = "ingest"
type = "dlt"
component = "stripe_charges"
""",
    )
    pipeline = load_pipeline(path, components={}, paths=project)
    assert pipeline.steps[0].component == "stripe_charges"  # type: ignore[union-attr]


def test_omitted_dbt_component_resolves_to_detected_project(project: ProjectPaths) -> None:
    _make_dbt(project)
    path = _write(
        project,
        """
[[steps]]
id = "stage"
type = "dbt"
command = "build"
""",
    )
    pipeline = load_pipeline(path, components={}, paths=project)
    # The dbt step kept its omitted component (None) but still validated.
    assert pipeline.steps[0].component is None  # type: ignore[union-attr]


def test_named_dbt_component_resolves(project: ProjectPaths) -> None:
    _make_dbt(project, "analytics")
    path = _write(
        project,
        """
[[steps]]
id = "stage"
type = "dbt"
component = "analytics"
""",
    )
    pipeline = load_pipeline(path, components={}, paths=project)
    assert pipeline.steps[0].component == "analytics"  # type: ignore[union-attr]


def test_dbt_step_referencing_dlt_component_fails_type_check(project: ProjectPaths) -> None:
    # A dbt step that names a dlt component (an el/ dir) must FAIL validation —
    # it resolves, but to the wrong type, and the executor would dispatch the
    # wrong engine at run time. (Regression for the review's type-agreement gap.)
    _make_el(project, "stripe_charges")
    path = _write(
        project,
        """
[[steps]]
id = "stage"
type = "dbt"
component = "stripe_charges"
""",
    )
    with pytest.raises(PipelineError, match="dlt component"):
        load_pipeline(path, components={}, paths=project)


def test_unresolvable_dlt_component_fails_validation(project: ProjectPaths) -> None:
    path = _write(
        project,
        """
[[steps]]
id = "ingest"
type = "dlt"
component = "does_not_exist"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert "does_not_exist" in str(exc.value)


def test_omitted_dbt_component_with_no_project_fails(project: ProjectPaths) -> None:
    # No dbt project anywhere -> the omitted component can't resolve.
    path = _write(
        project,
        """
[[steps]]
id = "stage"
type = "dbt"
""",
    )
    with pytest.raises(PipelineError) as exc:
        load_pipeline(path, components={}, paths=project)
    assert "dbt" in str(exc.value).lower()


def test_sql_step_needs_no_component_resolution(project: ProjectPaths) -> None:
    # A sql step references a file + connection inline; no component to resolve,
    # so the pipeline loads even with no el/ dirs or dbt project present.
    path = _write(
        project,
        """
[[steps]]
id = "refresh"
type = "sql"
file = "sql/refresh.sql"
connection = "prod"
""",
    )
    pipeline = load_pipeline(path, components={}, paths=project)
    assert pipeline.steps[0].id == "refresh"
