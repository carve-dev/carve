"""Resolution of the four axes into an InitPlan."""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.init.detect import Detection
from carve.init.plan import InitError, InitOptions, resolve


def _detection(
    root: Path, *, dbt: tuple[Path, ...] = (), dlt: bool = False, re_init: bool = False
) -> Detection:
    return Detection(
        root=root,
        re_init=re_init,
        dbt_projects=dbt,
        dlt_present=dlt,
        has_git=False,
        has_docker=True,
    )


def test_greenfield_no_components_no_scaffold(tmp_path: Path) -> None:
    plan = resolve(_detection(tmp_path), InitOptions())
    assert plan.components == ()
    assert plan.scaffold_dbt is False and plan.scaffold_dlt is False
    assert plan.dbt_same_repo is False and plan.dlt_same_repo is False
    assert plan.project_name == tmp_path.name
    assert plan.external_postgres_url is None


def test_brownfield_dbt_same_repo_writes_no_component(tmp_path: Path) -> None:
    det = _detection(tmp_path, dbt=(tmp_path / "dbt_project.yml",))
    plan = resolve(det, InitOptions())
    assert plan.dbt_same_repo is True
    assert plan.components == ()  # same-repo = convention discovery, no block


def test_brownfield_dlt_same_repo_writes_no_component(tmp_path: Path) -> None:
    plan = resolve(_detection(tmp_path, dlt=True), InitOptions())
    assert plan.dlt_same_repo is True
    assert plan.components == ()


def test_dbt_path_is_separate_local_component(tmp_path: Path) -> None:
    plan = resolve(_detection(tmp_path), InitOptions(dbt_path="../analytics"))
    (comp,) = plan.components
    assert (comp.name, comp.type, comp.mode, comp.path) == (
        "analytics",
        "dbt",
        "separate-local",
        "../analytics",
    )


def test_dbt_url_is_separate_remote_component(tmp_path: Path) -> None:
    plan = resolve(
        _detection(tmp_path),
        InitOptions(dbt_url="https://github.com/acme/analytics.git", dbt_branch="prod"),
    )
    (comp,) = plan.components
    assert comp.name == "analytics"  # .git stripped + slugged
    assert (comp.type, comp.mode, comp.url, comp.branch) == (
        "dbt",
        "separate-remote",
        "https://github.com/acme/analytics.git",
        "prod",
    )


def test_with_dbt_scaffolds_same_repo(tmp_path: Path) -> None:
    plan = resolve(_detection(tmp_path), InitOptions(with_dbt=True))
    assert plan.scaffold_dbt is True
    assert plan.dbt_same_repo is True
    assert plan.components == ()


def test_dlt_url_is_separate_remote_component(tmp_path: Path) -> None:
    plan = resolve(_detection(tmp_path), InitOptions(dlt_url="git@github.com:acme/ingest.git"))
    (comp,) = plan.components
    assert (comp.name, comp.type, comp.mode) == ("ingest", "dlt", "separate-remote")


def test_scp_url_without_path_slash_names_component_by_repo(tmp_path: Path) -> None:
    # SCP-style URL with no slash after the host must yield "ingest", not
    # "git-host-ingest".
    plan = resolve(_detection(tmp_path), InitOptions(dlt_url="git@host:ingest.git"))
    (comp,) = plan.components
    assert comp.name == "ingest"


def test_dbt_and_dlt_resolving_to_same_name_errors(tmp_path: Path) -> None:
    with pytest.raises(InitError) as exc:
        resolve(_detection(tmp_path), InitOptions(dbt_path="../shared", dlt_path="../shared"))
    assert "shared" in exc.value.message
    assert "unique" in exc.value.message.lower()


def test_conflicting_dbt_flags_error(tmp_path: Path) -> None:
    with pytest.raises(InitError) as exc:
        resolve(_detection(tmp_path), InitOptions(dbt_path="../x", dbt_url="https://h/y.git"))
    assert "conflicting" in exc.value.message.lower()


def test_ambiguous_multiple_dbt_detected_errors(tmp_path: Path) -> None:
    det = _detection(
        tmp_path, dbt=(tmp_path / "dbt_project.yml", tmp_path / "more" / "dbt_project.yml")
    )
    with pytest.raises(InitError) as exc:
        resolve(det, InitOptions())
    assert "2 dbt projects" in exc.value.message


def test_explicit_dbt_path_overrides_ambiguous_detection(tmp_path: Path) -> None:
    det = _detection(
        tmp_path, dbt=(tmp_path / "a" / "dbt_project.yml", tmp_path / "b" / "dbt_project.yml")
    )
    plan = resolve(det, InitOptions(dbt_path="./a"))  # explicit wins, no ambiguity error
    (comp,) = plan.components
    assert comp.mode == "separate-local"


def test_external_postgres_threads_through(tmp_path: Path) -> None:
    url = "postgresql+psycopg://u:p@h:5432/db"
    plan = resolve(_detection(tmp_path), InitOptions(external_postgres_url=url))
    assert plan.external_postgres_url == url


def test_project_name_override(tmp_path: Path) -> None:
    plan = resolve(_detection(tmp_path), InitOptions(project_name="custom"))
    assert plan.project_name == "custom"


def test_git_init_decision(tmp_path: Path) -> None:
    assert resolve(_detection(tmp_path), InitOptions()).git_init is True
    det_git = Detection(tmp_path, False, (), False, has_git=True, has_docker=True)
    assert resolve(det_git, InitOptions()).git_init is False
    assert resolve(_detection(tmp_path), InitOptions(no_git_init=True)).git_init is False
