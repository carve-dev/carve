"""Integration tests for `carve init`'s OSS packaging behavior (Increment 1).

The migrate cases use the per-test Postgres testcontainer; the
defer/error/slug cases use an unreachable URL (fast, no container) and
monkeypatch the Docker check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, inspect
from typer.testing import CliRunner

from carve.cli.main import app

# An address that refuses fast — stands in for "Postgres isn't up yet".
_UNREACHABLE = "postgresql+psycopg://carve:carve@127.0.0.1:1/carve"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_bundled_init_renders_compose_and_migrates(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    # Bundled path: Docker is on the real PATH; DATABASE_URL (cli_env) points
    # at the per-test Postgres, so init renders the compose AND migrates.
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    doc = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert doc["services"]["carve-postgres"]["image"] == "postgres:16"
    env_example = (tmp_path / ".env.example").read_text()
    assert "DATABASE_URL=postgresql+psycopg://carve:carve@127.0.0.1:5432/carve" in env_example
    assert "schema initialized" in result.output

    engine = create_engine(cli_env["DATABASE_URL"])
    assert "runs" in set(inspect(engine).get_table_names())
    engine.dispose()


def test_external_postgres_skips_compose_and_migrates(
    runner: CliRunner,
    tmp_path: Path,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # external pins the URL via state_store.url (precedence 1) — a stray
    # DATABASE_URL must not interfere.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = runner.invoke(
        app, ["init", str(tmp_path), "--external-postgres", postgres_state_store_url]
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "docker-compose.yml").exists()
    env_example = (tmp_path / ".env.example").read_text()
    # The real, password-bearing URL is NEVER written to the committed .env.example.
    assert postgres_state_store_url not in env_example
    assert "USER:PASSWORD@HOST" in env_example  # commented placeholder only
    # The real line is printed for the user to paste into their gitignored .env.
    assert postgres_state_store_url in result.output
    assert "External Postgres" in result.output
    assert "schema initialized" in result.output

    engine = create_engine(postgres_state_store_url)
    assert "runs" in set(inspect(engine).get_table_names())
    engine.dispose()


def test_external_postgres_never_writes_password_to_env_example(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the secret-leak: .env.example is written before the
    # (here failing) migration, and must never contain the real password.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    secret = "postgresql+psycopg://user:SUPERSECRET@127.0.0.1:1/db"
    result = runner.invoke(app, ["init", str(tmp_path), "--external-postgres", secret])
    assert result.exit_code == 3, result.output  # unreachable -> fatal
    assert "SUPERSECRET" not in (tmp_path / ".env.example").read_text()


def test_external_postgres_rejects_malformed_url(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--external-postgres", "mysql://nope"])
    assert result.exit_code == 2, result.output
    assert not (tmp_path / "docker-compose.yml").exists()
    assert "postgresql" in result.output.lower()


def test_rerun_does_not_overwrite_existing_compose(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("# my custom compose\n")
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output
    assert compose.read_text() == "# my custom compose\n"
    assert "already exists" in result.output


def test_bundled_init_defers_migration_when_postgres_unreachable(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Docker present, but Postgres not up yet -> init still succeeds (exit 0)
    # with a next-step, and the compose file is rendered.
    monkeypatch.setattr("carve.cli.commands.init.docker_compose_available", lambda: True)
    monkeypatch.setenv("DATABASE_URL", _UNREACHABLE)
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "docker-compose.yml").is_file()
    assert "isn't running yet" in result.output


def test_external_postgres_unreachable_is_fatal(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = runner.invoke(app, ["init", str(tmp_path), "--external-postgres", _UNREACHABLE])
    assert result.exit_code == 3, result.output
    assert "external postgres" in result.output.lower()


def test_no_docker_and_no_external_errors(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("carve.cli.commands.init.docker_compose_available", lambda: False)
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 3, result.output
    assert "Docker not detected" in result.output


def test_two_projects_get_noncolliding_slugs(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("carve.cli.commands.init.docker_compose_available", lambda: True)
    monkeypatch.setenv("DATABASE_URL", _UNREACHABLE)  # defer migration (don't need Postgres)
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    assert runner.invoke(app, ["init", str(alpha)]).exit_code == 0
    assert runner.invoke(app, ["init", str(beta)]).exit_code == 0
    a = yaml.safe_load((alpha / "docker-compose.yml").read_text())
    b = yaml.safe_load((beta / "docker-compose.yml").read_text())
    assert a["services"]["carve-postgres"]["container_name"] == "carve-postgres-alpha"
    assert b["services"]["carve-postgres"]["container_name"] == "carve-postgres-beta"
