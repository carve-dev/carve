"""Tests for engine/session helpers and database initialization.

The Postgres path is exercised via `testcontainers` if available; the
fixture is skipped cleanly otherwise so CI doesn't fail on machines
without Docker.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from carve.core.config.schema import (
    Config,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)


def _make_config(state_store: str) -> Config:
    return Config(
        project=ProjectConfig(name="test-project"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(state_store=state_store),
    )


def test_initialize_creates_three_tables(tmp_path: Path) -> None:
    config = _make_config("sqlite:///.carve/state.db")
    engine = create_engine_from_config(config, project_dir=tmp_path)
    try:
        initialize_database(engine)
        tables = set(inspect(engine).get_table_names())
        assert {"runs", "logs", "plans"}.issubset(tables)
    finally:
        engine.dispose()


def test_init_creates_carve_dir_if_missing(tmp_path: Path) -> None:
    """`.carve/` must be created so SQLite can open the file."""
    config = _make_config("sqlite:///.carve/state.db")
    assert not (tmp_path / ".carve").exists()

    engine = create_engine_from_config(config, project_dir=tmp_path)
    try:
        initialize_database(engine)
    finally:
        engine.dispose()

    assert (tmp_path / ".carve" / "state.db").is_file()


def test_relative_sqlite_url_resolves_against_project_dir(tmp_path: Path) -> None:
    """A relative sqlite URL must land inside the project, not the cwd."""
    config = _make_config("sqlite:///.carve/state.db")
    engine = create_engine_from_config(config, project_dir=tmp_path)
    try:
        db_path = engine.url.database
        assert db_path is not None
        assert Path(db_path) == (tmp_path / ".carve" / "state.db").resolve()
    finally:
        engine.dispose()


def test_wal_mode_is_enabled(tmp_path: Path) -> None:
    config = _make_config("sqlite:///.carve/state.db")
    engine = create_engine_from_config(config, project_dir=tmp_path)
    try:
        initialize_database(engine)
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
            sync = conn.execute(text("PRAGMA synchronous")).scalar_one()
        assert str(mode).lower() == "wal"
        # synchronous=NORMAL is integer 1 per SQLite docs
        assert int(sync) == 1
    finally:
        engine.dispose()


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    config = _make_config("sqlite:///.carve/state.db")
    engine = create_engine_from_config(config, project_dir=tmp_path)
    try:
        initialize_database(engine)
        initialize_database(engine)  # second call must not raise
        tables = set(inspect(engine).get_table_names())
        assert {"runs", "logs", "plans"}.issubset(tables)
    finally:
        engine.dispose()


def test_session_factory_returns_usable_sessions(tmp_path: Path) -> None:
    config = _make_config("sqlite:///.carve/state.db")
    engine = create_engine_from_config(config, project_dir=tmp_path)
    try:
        initialize_database(engine)
        factory = create_session_factory(engine)
        with factory() as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Postgres (optional)
# ---------------------------------------------------------------------------


@pytest.fixture
def postgres_url() -> Generator[str, None, None]:
    """Spin up a throwaway Postgres container, or skip if unavailable."""
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Docker not available for testcontainers: {exc}")

    try:
        yield container.get_connection_url()
    finally:
        container.stop()


def test_initialize_works_against_postgres(postgres_url: str) -> None:  # pragma: no cover
    """The same `initialize_database` call must succeed on Postgres."""
    config = _make_config(postgres_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)
        tables = set(inspect(engine).get_table_names())
        assert {"runs", "logs", "plans"}.issubset(tables)
    finally:
        engine.dispose()
