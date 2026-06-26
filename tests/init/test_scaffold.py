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


def test_with_dbt_scaffolds_staging_marts_layout_and_tests(tmp_path: Path) -> None:
    """The greenfield scaffold writes the spec'd staging/marts + sample models + tests."""
    scaffold(tmp_path, _plan(tmp_path, scaffold_dbt=True, dbt_same_repo=True))

    models = tmp_path / "models"
    stg = models / "staging" / "stg_orders.sql"
    stg_schema = models / "staging" / "stg_orders_schema.yml"
    mart = models / "marts" / "mart_orders.sql"
    mart_schema = models / "marts" / "mart_orders_schema.yml"
    for f in (stg, stg_schema, mart, mart_schema):
        assert f.is_file(), f

    # ref()/source() only — no hardcoded table names.
    assert "{{ source('raw', 'orders') }}" in stg.read_text()
    assert "{{ ref('stg_orders') }}" in mart.read_text()
    # Tests on the grain in the schema files.
    assert "not_null" in stg_schema.read_text()
    assert "unique" in stg_schema.read_text()
    assert "unique" in mart_schema.read_text()


def test_with_dbt_project_yml_declares_layer_materializations(tmp_path: Path) -> None:
    import yaml

    scaffold(tmp_path, _plan(tmp_path, scaffold_dbt=True, dbt_same_repo=True))
    doc = yaml.safe_load((tmp_path / "dbt_project.yml").read_text())
    slug = doc["name"]
    layers = doc["models"][slug]
    assert layers["staging"]["+materialized"] == "view"
    assert layers["marts"]["+materialized"] == "table"


def test_scaffolded_dbt_project_round_trips_through_inference(tmp_path: Path) -> None:
    """Inference reads back exactly the conventions the greenfield scaffold wrote."""
    from carve.integrations.dbt.conventions import infer_conventions

    scaffold(tmp_path, _plan(tmp_path, scaffold_dbt=True, dbt_same_repo=True))
    conv = infer_conventions(tmp_path)
    assert conv.layer("staging").prefixes == ("stg_",)
    assert conv.layer("staging").materialization == "view"
    assert conv.layer("marts").prefixes == ("mart_",)
    assert conv.layer("marts").materialization == "table"


def test_with_dbt_keeps_conventions_md_comment_only(tmp_path: Path) -> None:
    """The guardrail: greenfield scaffold must NOT fill conventions.md — it stays
    comment-only so the agent isn't falsely told conventions were inferred."""
    from carve.init.templates import CONVENTIONS_MD_CONTENT

    scaffold(tmp_path, _plan(tmp_path, scaffold_dbt=True, dbt_same_repo=True))
    conv = (tmp_path / "carve" / "conventions.md").read_text()
    assert conv == CONVENTIONS_MD_CONTENT
    # Comment-only: no inferred prose, no real heading.
    assert "Inferred project conventions land here" in conv
    assert "# Inferred project conventions" not in conv


def test_dbt_project_name_slug_is_dbt_safe(tmp_path: Path) -> None:
    """A display name with spaces/dashes yields a dbt-identifier-safe project name."""
    import yaml

    scaffold(tmp_path, _plan(tmp_path, project_name="My Cool Shop", scaffold_dbt=True))
    doc = yaml.safe_load((tmp_path / "dbt_project.yml").read_text())
    assert doc["name"] == "my_cool_shop"
    assert doc["profile"] == "my_cool_shop"


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
