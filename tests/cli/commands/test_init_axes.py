"""Integration: `carve init`'s control-plane axes (detection, components, re-init).

Uses an unreachable DATABASE_URL + a patched Docker check so these exercise
the detect→resolve→scaffold flow without a real Postgres (state-store
migration defers on the bundled path). Migration-against-Postgres is covered
by tests/cli/commands/test_init_packaging.py.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.main import app

_UNREACHABLE = "postgresql+psycopg://carve:carve@127.0.0.1:1/carve"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _bundled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Docker present (so the bundled path is allowed) but Postgres unreachable
    # (so the state-store migration defers instead of needing a live DB).
    monkeypatch.setattr("carve.cli.commands.init.docker_compose_available", lambda: True)
    monkeypatch.setenv("DATABASE_URL", _UNREACHABLE)


def test_greenfield_writes_no_component_blocks(runner: CliRunner, tmp_path: Path) -> None:
    res = runner.invoke(app, ["init", str(tmp_path)])
    assert res.exit_code == 0, res.output
    data = tomllib.loads((tmp_path / "carve.toml").read_text())
    assert "components" not in data
    assert (tmp_path / "carve" / "standards.md").is_file()
    assert (tmp_path / "carve" / "decisions.md").is_file()
    assert (tmp_path / "carve" / "conventions.md").is_file()


def test_brownfield_dbt_simple_mode_no_component_block(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text("name: analytics\nprofile: analytics\n")
    models = tmp_path / "models" / "staging"
    models.mkdir(parents=True)
    (models / "stg_orders.sql").write_text("select 1 as id\n")

    res = runner.invoke(app, ["init", str(tmp_path)])
    assert res.exit_code == 0, res.output
    # Same-repo dbt is convention-discovered → NO [components.*] block.
    data = tomllib.loads((tmp_path / "carve.toml").read_text())
    assert "components" not in data


def test_separate_remote_dbt_writes_component_block(runner: CliRunner, tmp_path: Path) -> None:
    res = runner.invoke(
        app,
        [
            "init",
            str(tmp_path),
            "--dbt-url",
            "https://github.com/acme/analytics.git",
            "--dbt-branch",
            "prod",
        ],
    )
    assert res.exit_code == 0, res.output
    data = tomllib.loads((tmp_path / "carve.toml").read_text())
    assert data["components"]["analytics"] == {
        "type": "dbt",
        "mode": "separate-remote",
        "url": "https://github.com/acme/analytics.git",
        "branch": "prod",
    }
    assert not (tmp_path / "models").exists()  # separate, not scaffolded here


def test_with_dbt_scaffolds_same_repo_no_block(runner: CliRunner, tmp_path: Path) -> None:
    res = runner.invoke(app, ["init", str(tmp_path), "--with-dbt"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "dbt_project.yml").is_file()
    assert (tmp_path / "models").is_dir()
    data = tomllib.loads((tmp_path / "carve.toml").read_text())
    assert "components" not in data  # same-repo scaffold = convention discovery


def test_conflicting_dbt_flags_exit_2(runner: CliRunner, tmp_path: Path) -> None:
    res = runner.invoke(
        app, ["init", str(tmp_path), "--dbt-path", "./x", "--dbt-url", "https://h/y.git"]
    )
    assert res.exit_code == 2, res.output
    assert "conflicting" in res.output.lower()


def test_invalid_default_target_exits_2_without_scaffolding(
    runner: CliRunner, tmp_path: Path
) -> None:
    res = runner.invoke(app, ["init", str(tmp_path), "--default-target", "Bad-Name"])
    assert res.exit_code == 2, res.output
    # Validated up front → nothing written.
    assert not (tmp_path / "carve.toml").exists()


def test_reinit_preserves_user_edits(runner: CliRunner, tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    edited = '# my edited carve.toml\n[project]\nname = "x"\n'
    (tmp_path / "carve.toml").write_text(edited)
    res = runner.invoke(app, ["init", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "carve.toml").read_text() == edited  # untouched
    assert "re-init" in res.output.lower() or "already exists" in res.output.lower()
