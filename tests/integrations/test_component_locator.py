"""Tests for `resolve_component` + simple-mode discovery.

The locator is the single place path math happens. These tests cover the
four resolution rules (same-repo dlt/dbt, separate-local, separate-remote)
across the dbt-detection edge cases (zero/one/multiple `dbt_project.yml`),
plus the convention-based discovery that powers simple mode.

*(layout spec Tests: unit bullet 2)*
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import ComponentConfig, ComponentType
from carve.integrations.component_locator import (
    ComponentResolutionError,
    discover_components,
    resolve_component,
)


@pytest.fixture
def project(tmp_path: Path) -> ProjectPaths:
    """A bare control-plane root with the flat dirs created."""
    for sub in ("el", "pipelines", "carve", ".carve", ".dlt"):
        (tmp_path / sub).mkdir()
    return ProjectPaths.from_root(tmp_path)


def _make_el(paths: ProjectPaths, name: str) -> Path:
    d = paths.el_dir / name
    d.mkdir()
    (d / "__init__.py").write_text("# dlt source\n")
    return d


def _make_dbt(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "dbt_project.yml").write_text("name: my_dbt\n")
    return parent


# ---------------------------------------------------------------------------
# same-repo dlt
# ---------------------------------------------------------------------------


def test_same_repo_dlt_block_resolves_to_el_dir(project: ProjectPaths) -> None:
    _make_el(project, "stripe_charges")
    block = ComponentConfig(type="dlt", mode="same-repo")
    resolved = resolve_component(
        "stripe_charges", components={"stripe_charges": block}, paths=project
    )
    assert resolved.type is ComponentType.DLT
    assert resolved.code_path == project.el_dir / "stripe_charges"
    assert resolved.ref is None


# ---------------------------------------------------------------------------
# same-repo dbt: zero / one / multiple dbt_project.yml
# ---------------------------------------------------------------------------


def test_same_repo_dbt_at_root_resolves(project: ProjectPaths) -> None:
    _make_dbt(project.root)
    block = ComponentConfig(type="dbt", mode="same-repo")
    resolved = resolve_component("dbt", components={"dbt": block}, paths=project)
    assert resolved.type is ComponentType.DBT
    assert resolved.code_path == project.root


def test_same_repo_dbt_one_level_down_resolves(project: ProjectPaths) -> None:
    _make_dbt(project.root / "analytics")
    block = ComponentConfig(type="dbt", mode="same-repo")
    resolved = resolve_component(
        "analytics", components={"analytics": block}, paths=project
    )
    assert resolved.code_path == project.root / "analytics"


def test_same_repo_dbt_zero_projects_errors(project: ProjectPaths) -> None:
    block = ComponentConfig(type="dbt", mode="same-repo")
    with pytest.raises(ComponentResolutionError, match="No dbt project"):
        resolve_component("dbt", components={"dbt": block}, paths=project)


def test_same_repo_dbt_multiple_projects_errors_with_listing(
    project: ProjectPaths,
) -> None:
    _make_dbt(project.root / "analytics")
    _make_dbt(project.root / "reporting")
    block = ComponentConfig(type="dbt", mode="same-repo")
    with pytest.raises(ComponentResolutionError, match="Multiple dbt projects") as exc:
        resolve_component("dbt", components={"dbt": block}, paths=project)
    # Both candidates are named so the user can pick one.
    assert "analytics" in str(exc.value)
    assert "reporting" in str(exc.value)


def test_dbt_detection_ignores_deeper_than_one_level(project: ProjectPaths) -> None:
    # A dbt_project.yml two levels down must NOT be auto-detected.
    _make_dbt(project.root / "services" / "deep")
    block = ComponentConfig(type="dbt", mode="same-repo")
    with pytest.raises(ComponentResolutionError, match="No dbt project"):
        resolve_component("dbt", components={"dbt": block}, paths=project)


def test_dbt_detection_ignores_control_plane_dirs(project: ProjectPaths) -> None:
    # Stray dbt_project.yml inside el/ or carve/ shouldn't count as the project.
    (project.el_dir / "dbt_project.yml").write_text("name: stray\n")
    block = ComponentConfig(type="dbt", mode="same-repo")
    with pytest.raises(ComponentResolutionError, match="No dbt project"):
        resolve_component("dbt", components={"dbt": block}, paths=project)


# ---------------------------------------------------------------------------
# separate-local: present / missing path
# ---------------------------------------------------------------------------


def test_separate_local_present_path_resolves(
    project: ProjectPaths, tmp_path: Path
) -> None:
    external = tmp_path / "external-ingest"
    external.mkdir()
    block = ComponentConfig(type="dlt", mode="separate-local", path=str(external))
    resolved = resolve_component(
        "ingest", components={"ingest": block}, paths=project
    )
    assert resolved.code_path == external
    assert resolved.ref is None


def test_separate_local_missing_path_errors(project: ProjectPaths) -> None:
    block = ComponentConfig(
        type="dlt", mode="separate-local", path="/nonexistent/ingest"
    )
    with pytest.raises(ComponentResolutionError, match="does not exist"):
        resolve_component("ingest", components={"ingest": block}, paths=project)


# ---------------------------------------------------------------------------
# separate-remote: derived workspace path; ref pins, branch tracks
# ---------------------------------------------------------------------------


def test_separate_remote_resolves_to_workspace_cache_path(
    project: ProjectPaths,
) -> None:
    block = ComponentConfig(
        type="dbt",
        mode="separate-remote",
        url="git@github.com:org/analytics.git",
        branch="main",
    )
    resolved = resolve_component(
        "analytics", components={"analytics": block}, paths=project
    )
    assert resolved.code_path.parent == project.workspaces_dir
    assert resolved.code_path.name.endswith("-main")
    # branch-tracking: not pinned.
    assert resolved.ref is None


def test_separate_remote_ref_is_carried_as_pin(project: ProjectPaths) -> None:
    block = ComponentConfig(
        type="dbt",
        mode="separate-remote",
        url="git@github.com:org/analytics.git",
        ref="9f3a1c7",
        branch="main",
    )
    resolved = resolve_component(
        "analytics", components={"analytics": block}, paths=project
    )
    assert resolved.ref == "9f3a1c7"
    # ref wins over branch in the derived dir name too.
    assert resolved.code_path.name.endswith("-9f3a1c7")


def test_separate_remote_is_pure_no_clone(project: ProjectPaths) -> None:
    block = ComponentConfig(
        type="dbt", mode="separate-remote", url="git@github.com:org/x.git", branch="main"
    )
    resolved = resolve_component("x", components={"x": block}, paths=project)
    # Resolution does not create or clone anything.
    assert not resolved.code_path.exists()


# ---------------------------------------------------------------------------
# Simple-mode (convention) resolution + discovery
# ---------------------------------------------------------------------------


def test_convention_resolves_el_dir_as_dlt(project: ProjectPaths) -> None:
    _make_el(project, "salesforce_accounts")
    resolved = resolve_component(
        "salesforce_accounts", components={}, paths=project
    )
    assert resolved.type is ComponentType.DLT
    assert resolved.code_path == project.el_dir / "salesforce_accounts"


def test_convention_resolves_detected_dbt_project(project: ProjectPaths) -> None:
    _make_dbt(project.root / "analytics")
    resolved = resolve_component("analytics", components={}, paths=project)
    assert resolved.type is ComponentType.DBT
    assert resolved.code_path == project.root / "analytics"


def test_convention_root_dbt_named_dbt(project: ProjectPaths) -> None:
    _make_dbt(project.root)
    resolved = resolve_component("dbt", components={}, paths=project)
    assert resolved.type is ComponentType.DBT
    assert resolved.code_path == project.root


def test_unknown_name_errors(project: ProjectPaths) -> None:
    with pytest.raises(ComponentResolutionError, match="No component named"):
        resolve_component("ghost", components={}, paths=project)


def test_discover_enumerates_el_and_dbt(project: ProjectPaths) -> None:
    _make_el(project, "stripe_charges")
    _make_el(project, "salesforce_accounts")
    _make_dbt(project.root / "analytics")

    discovered = discover_components(project)
    by_name = {c.name: c for c in discovered}

    assert by_name["stripe_charges"].type is ComponentType.DLT
    assert by_name["salesforce_accounts"].type is ComponentType.DLT
    assert by_name["analytics"].type is ComponentType.DBT
    assert set(by_name) == {"stripe_charges", "salesforce_accounts", "analytics"}


def test_discover_dlt_only_when_no_dbt(project: ProjectPaths) -> None:
    _make_el(project, "stripe_charges")
    discovered = discover_components(project)
    assert [c.name for c in discovered] == ["stripe_charges"]


def test_discover_skips_dotfiles_in_el(project: ProjectPaths) -> None:
    _make_el(project, "real")
    (project.el_dir / ".cache").mkdir()
    discovered = discover_components(project)
    assert [c.name for c in discovered] == ["real"]


def test_discover_empty_when_nothing_present(project: ProjectPaths) -> None:
    assert discover_components(project) == []


def test_block_overrides_convention(project: ProjectPaths, tmp_path: Path) -> None:
    # An el/<name>/ dir exists, but a block of the same name takes precedence.
    _make_el(project, "ingest")
    external = tmp_path / "external"
    external.mkdir()
    block = ComponentConfig(type="dlt", mode="separate-local", path=str(external))
    resolved = resolve_component(
        "ingest", components={"ingest": block}, paths=project
    )
    assert resolved.code_path == external
