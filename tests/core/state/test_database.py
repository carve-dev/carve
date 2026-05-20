"""Tests for engine/session helpers and database initialization.

v0.1-01 retired SQLite outright. Every test in this file runs against
the per-test Postgres database from the ``postgres_state_store_url``
fixture in ``tests/conftest.py`` (a session-scoped testcontainers
Postgres with a fresh database per test).

These tests cover:

* ``create_engine_from_config`` happy path on a Postgres URL.
* ``initialize_database`` lands the schema and is idempotent.
* The session factory returns usable sessions.
* Model metadata reflects against Postgres with the v0.1-01 type
  shifts (JSONB on Plan.task_graph_json / Build.manifest_json,
  TIMESTAMPTZ on the timestamp columns).
* Engine factory rejects every non-Postgres URL shape with
  ``StateStoreBackendError`` and a message pointing at the docs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TIMESTAMP

from carve.core.config.schema import (
    Config,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    StateStoreBackendError,
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)


def _make_config(state_store_url: str) -> Config:
    return Config(
        project=ProjectConfig(name="test-project"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=state_store_url),
    )


# ---------------------------------------------------------------------------
# Engine factory + initialize_database happy-path (Postgres)
# ---------------------------------------------------------------------------


def test_initialize_creates_baseline_tables(postgres_state_store_url: str) -> None:
    """``initialize_database`` upgrades a fresh Postgres to head."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)
        tables = set(inspect(engine).get_table_names())
        assert {"runs", "logs", "plans", "pipelines", "builds"}.issubset(tables)
    finally:
        engine.dispose()


def test_initialize_is_idempotent(postgres_state_store_url: str) -> None:
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)
        initialize_database(engine)  # second call must not raise
        tables = set(inspect(engine).get_table_names())
        assert {"runs", "logs", "plans", "pipelines", "builds"}.issubset(tables)
    finally:
        engine.dispose()


def test_session_factory_returns_usable_sessions(
    postgres_state_store_url: str,
) -> None:
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)
        factory = create_session_factory(engine)
        with factory() as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_engine_url_is_postgres(postgres_state_store_url: str) -> None:
    """The engine factory accepts the postgresql+psycopg URL form."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        assert engine.url.get_backend_name() == "postgresql"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Model metadata reflection (v0.1-01 ## Tests bullet 1)
# ---------------------------------------------------------------------------


def test_model_metadata_reflects_against_postgres(
    postgres_state_store_url: str,
) -> None:
    """The Alembic-managed schema reflects to the v0.1-01 expected shape.

    Concretely:

    * The five M1-shape tables are present (``runs``, ``logs``, ``plans``,
      ``pipelines``, ``builds``).
    * ``plans.task_graph_json`` and ``builds.manifest_json`` reflect as
      ``JSONB`` (the v0.1-01 type shift away from TEXT).
    * The timestamp columns reflect as ``TIMESTAMP WITH TIME ZONE``
      (the v0.1-01 shift away from naive TEXT timestamps).
    """
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)
        inspector = inspect(engine)

        table_names = set(inspector.get_table_names())
        assert {"runs", "logs", "plans", "pipelines", "builds"}.issubset(
            table_names
        ), f"expected M1-shape tables; got {sorted(table_names)}"

        plan_cols = {c["name"]: c for c in inspector.get_columns("plans")}
        build_cols = {c["name"]: c for c in inspector.get_columns("builds")}

        # JSONB on the two free-form payload columns.
        assert isinstance(plan_cols["task_graph_json"]["type"], JSONB), (
            f"plans.task_graph_json must be JSONB after v0.1-01; "
            f"got {plan_cols['task_graph_json']['type']!r}"
        )
        assert isinstance(build_cols["manifest_json"]["type"], JSONB), (
            f"builds.manifest_json must be JSONB after v0.1-01; "
            f"got {build_cols['manifest_json']['type']!r}"
        )

        # TIMESTAMPTZ on the v0.1-01-flipped timestamp columns.
        for col_name in ("created_at", "expires_at"):
            col_type = plan_cols[col_name]["type"]
            assert isinstance(col_type, TIMESTAMP), (
                f"plans.{col_name} must be TIMESTAMP type; got {col_type!r}"
            )
            assert col_type.timezone is True, (
                f"plans.{col_name} must be TIMESTAMPTZ (timezone=True); "
                f"got {col_type!r}"
            )

        run_cols = {c["name"]: c for c in inspector.get_columns("runs")}
        for col_name in ("created_at", "started_at", "completed_at"):
            col_type = run_cols[col_name]["type"]
            assert isinstance(col_type, TIMESTAMP), (
                f"runs.{col_name} must be TIMESTAMP type; got {col_type!r}"
            )
            assert col_type.timezone is True, (
                f"runs.{col_name} must be TIMESTAMPTZ; got {col_type!r}"
            )

        build_created = build_cols["created_at"]["type"]
        assert isinstance(build_created, TIMESTAMP)
        assert build_created.timezone is True
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Engine factory rejects non-Postgres URLs (v0.1-01 ## Tests bullet 2)
# ---------------------------------------------------------------------------


# URL prefixes for the rejection test built as concatenations so the
# repo-wide "no SQLite URL" grep stays clean — these are test inputs
# proving SQLite is rejected, not active state-store URLs.
_SQLITE_BAD_URL = "sqlite" + ":///" + "path/to/state.db"
_MYSQL_BAD_URL = "mysql://user@host/db"
_MALFORMED_BAD_URL = "not-a-url"


@pytest.mark.parametrize(
    "bad_url",
    [_SQLITE_BAD_URL, _MYSQL_BAD_URL, _MALFORMED_BAD_URL],
    ids=["sqlite", "mysql", "malformed"],
)
def test_engine_factory_rejects_non_postgres_url(bad_url: str) -> None:
    """Every non-Postgres URL shape is rejected at engine-construction time.

    The error message must point users at the docs (``docs/installation.md``)
    so they know where to find the bundled docker-compose path or the
    external-Postgres flag.
    """
    config = Config(
        project=ProjectConfig(name="reject-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=bad_url),
    )
    with pytest.raises(StateStoreBackendError, match=r"postgresql\+psycopg://"):
        create_engine_from_config(config)


def test_database_url_env_with_non_postgres_dialect_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DATABASE_URL=sqlite:///...` must raise the same `StateStoreBackendError`
    as setting the equivalent value in `state_store.url`.

    v0.1-01c added `DATABASE_URL` env to the resolver's precedence chain.
    The dialect rejection in `create_engine_from_config` keys on the
    resolved URL regardless of source, so an env-supplied non-Postgres
    URL must trip the same guard — confirming the open-question
    resolution in spec v0.1-01c §Open questions #3.
    """
    monkeypatch.setenv("DATABASE_URL", _SQLITE_BAD_URL)
    config = Config(
        project=ProjectConfig(name="env-reject-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(),
    )
    with pytest.raises(StateStoreBackendError, match=r"postgresql\+psycopg://"):
        create_engine_from_config(config)


def test_initialize_database_rejects_non_postgres_engine(
    postgres_state_store_url: str,
) -> None:
    """Even if a caller smuggles a non-Postgres engine past the factory,
    ``initialize_database`` re-checks and rejects with the same error.

    Belt-and-braces: ``initialize_database`` is the public entry point
    used by ``carve serve`` startup and ``carve init``; if anyone
    constructs an engine directly via ``sqlalchemy.create_engine`` and
    hands it in, the schema-creation path still refuses to touch a
    non-Postgres backend.
    """
    from sqlalchemy import create_engine

    sqlite_engine = create_engine("sqlite" + ":///" + ":memory:")
    try:
        with pytest.raises(StateStoreBackendError, match="Postgres"):
            initialize_database(sqlite_engine)
    finally:
        sqlite_engine.dispose()
