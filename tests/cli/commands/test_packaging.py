"""Unit tests for the OSS packaging helpers (packaging.py).

No Postgres / Docker required — these exercise the compose template, slug,
URL validation, and env blocks in isolation.
"""

from __future__ import annotations

import pytest
import yaml

from carve.cli.commands import packaging as p


def test_project_slug_basic() -> None:
    assert p.project_slug("My Cool Project") == "my-cool-project"


def test_project_slug_collapses_and_strips() -> None:
    assert p.project_slug("__Foo  Bar!!__") == "foo-bar"


@pytest.mark.parametrize("name", ["!!!", "", "   ", "日本語", "中文"])
def test_project_slug_degenerate_gets_unique_suffix(name: str) -> None:
    s = p.project_slug(name)
    assert s.startswith("carve-")
    assert s != "carve"


def test_project_slug_degenerate_names_dont_collide() -> None:
    # Two all-non-ASCII names must not both collapse to a bare `carve`.
    assert p.project_slug("日本語") != p.project_slug("中文")


def test_render_compose_is_valid_yaml_with_expected_shape() -> None:
    doc = yaml.safe_load(p.render_compose("My Proj"))
    svc = doc["services"]["carve-postgres"]
    assert svc["image"] == "postgres:16"
    assert svc["container_name"] == "carve-postgres-my-proj"
    # Bound to localhost only — never exposed to the network.
    assert svc["ports"] == ["127.0.0.1:${CARVE_POSTGRES_PORT:-5432}:5432"]
    assert doc["volumes"]["carve-postgres-data"]["name"] == "carve-postgres-data-my-proj"
    assert "pg_isready" in svc["healthcheck"]["test"][1]


def test_render_compose_two_projects_dont_collide() -> None:
    a = yaml.safe_load(p.render_compose("alpha"))
    b = yaml.safe_load(p.render_compose("beta"))
    assert (
        a["services"]["carve-postgres"]["container_name"]
        != b["services"]["carve-postgres"]["container_name"]
    )
    assert (
        a["volumes"]["carve-postgres-data"]["name"]
        != b["volumes"]["carve-postgres-data"]["name"]
    )


def test_normalize_url_psycopg_passthrough() -> None:
    url = "postgresql+psycopg://u:x@h:5432/db"
    assert p.normalize_postgres_url(url) == url


def test_normalize_url_upgrades_plain_postgresql() -> None:
    assert (
        p.normalize_postgres_url("postgresql://u:x@h:5432/db")
        == "postgresql+psycopg://u:x@h:5432/db"
    )


@pytest.mark.parametrize(
    "bad", ["mysql://x", "sqlite:///x.db", "not-a-url", "", "postgres://u@h/db"]
)
def test_normalize_url_rejects_malformed(bad: str) -> None:
    with pytest.raises(p.InvalidPostgresUrlError):
        p.normalize_postgres_url(bad)


def test_bundled_env_block_has_default_url_and_overrides() -> None:
    block = p.bundled_env_block()
    assert "DATABASE_URL=postgresql+psycopg://carve:carve@127.0.0.1:5432/carve" in block
    assert "# POSTGRES_PASSWORD=" in block
    assert "# CARVE_POSTGRES_PORT=" in block


def test_external_env_block_is_placeholder_only() -> None:
    block = p.external_env_block()
    assert "USER:PASSWORD@HOST" in block  # commented placeholder, not a real URL
    assert "your responsibility" in block.lower()
    # Any DATABASE_URL line must be commented so the placeholder isn't used.
    for line in block.splitlines():
        if "DATABASE_URL" in line:
            assert line.lstrip().startswith("#")


def test_docker_compose_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(p.shutil, "which", lambda name: "/usr/bin/docker")
    assert p.docker_compose_available() is True
    monkeypatch.setattr(p.shutil, "which", lambda name: None)
    assert p.docker_compose_available() is False
