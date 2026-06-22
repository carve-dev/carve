"""Scaffold: writes the expected layout, idempotent, correct carve.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from carve.init.plan import ComponentSpec, InitPlan
from carve.init.scaffold import scaffold


def _plan(root: Path, **overrides: object) -> InitPlan:
    base: dict[str, object] = dict(
        root=root,
        project_name=root.name,
        default_target="dev",
        external_postgres_url=None,
        components=(),
        scaffold_dbt=False,
        scaffold_dlt=False,
        dbt_same_repo=False,
        dlt_same_repo=False,
        git_init=False,
        re_init=False,
    )
    base.update(overrides)
    return InitPlan(**base)  # type: ignore[arg-type]


def test_greenfield_writes_core_layout(tmp_path: Path) -> None:
    scaffold(tmp_path, _plan(tmp_path))
    for rel in (
        "carve.toml",
        "carve/runner.toml",
        "carve/models.toml",
        "carve/standards.md",
        "carve/decisions.md",
        "carve/conventions.md",
        ".env.example",
        ".gitignore",
        "docker-compose.yml",  # bundled (no external url)
    ):
        assert (tmp_path / rel).is_file(), rel
    assert (tmp_path / "carve" / "agents").is_dir()
    assert (tmp_path / "el").is_dir()


def test_carve_toml_is_valid_toml_with_project_and_paths(tmp_path: Path) -> None:
    scaffold(tmp_path, _plan(tmp_path, project_name="my proj"))
    data = tomllib.loads((tmp_path / "carve.toml").read_text())
    assert data["project"]["name"] == "my proj"
    assert data["project"]["default_target"] == "dev"
    assert data["paths"]["config_dir"] == "carve"
    assert "components" not in data  # simple mode → no component blocks


def test_separate_components_render_blocks(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        components=(
            ComponentSpec(
                "analytics", "dbt", "separate-remote", url="https://h/a.git", branch="main"
            ),
            ComponentSpec("ingest", "dlt", "separate-local", path="../ingest"),
        ),
    )
    scaffold(tmp_path, plan)
    data = tomllib.loads((tmp_path / "carve.toml").read_text())
    assert data["components"]["analytics"] == {
        "type": "dbt",
        "mode": "separate-remote",
        "url": "https://h/a.git",
        "branch": "main",
    }
    assert data["components"]["ingest"] == {
        "type": "dlt",
        "mode": "separate-local",
        "path": "../ingest",
    }


def test_external_postgres_skips_compose(tmp_path: Path) -> None:
    scaffold(tmp_path, _plan(tmp_path, external_postgres_url="postgresql+psycopg://u:p@h/db"))
    assert not (tmp_path / "docker-compose.yml").exists()
    env = (tmp_path / ".env.example").read_text()
    assert "USER:PASSWORD@HOST" in env  # placeholder, not the real url
    assert "u:p@h" not in env


def test_with_dbt_and_dlt_scaffolds(tmp_path: Path) -> None:
    scaffold(
        tmp_path,
        _plan(
            tmp_path, scaffold_dbt=True, scaffold_dlt=True, dbt_same_repo=True, dlt_same_repo=True
        ),
    )
    assert (tmp_path / "dbt_project.yml").is_file()
    assert (tmp_path / "models").is_dir()
    sample = tmp_path / "el" / "sample" / "__init__.py"
    assert sample.is_file()
    assert "import dlt" in sample.read_text()


def test_non_bmp_project_name_renders_valid_toml(tmp_path: Path) -> None:
    # An emoji in the directory name must not produce a surrogate-pair \u
    # escape (which tomllib rejects) — the carve.toml must stay loadable.
    scaffold(tmp_path, _plan(tmp_path, project_name="rocket🚀proj"))
    data = tomllib.loads((tmp_path / "carve.toml").read_text())
    assert data["project"]["name"] == "rocket🚀proj"


def test_write_refuses_to_follow_dangling_symlink(tmp_path: Path) -> None:
    # A pre-planted dangling symlink at a target must be kept, never written
    # through (which would clobber a file outside the project root).
    outside = tmp_path.parent / "ESCAPE_TARGET.txt"
    link = tmp_path / "carve.toml"
    link.symlink_to(outside)
    result = scaffold(tmp_path, _plan(tmp_path))
    assert link in result.kept
    assert not outside.exists()  # write did not follow the link


def test_scaffold_is_idempotent(tmp_path: Path) -> None:
    first = scaffold(tmp_path, _plan(tmp_path))
    assert first.written  # wrote files
    # Tamper with a user-editable file; second run must NOT overwrite it.
    (tmp_path / "carve.toml").write_text("# user edited\n")
    second = scaffold(tmp_path, _plan(tmp_path))
    assert second.written == []  # nothing rewritten
    assert set(second.kept) >= {tmp_path / "carve.toml", tmp_path / ".gitignore"}
    assert (tmp_path / "carve.toml").read_text() == "# user edited\n"  # preserved
