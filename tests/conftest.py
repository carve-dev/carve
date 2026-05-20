"""Session-wide pytest setup.

Two responsibilities:

* **Disable the CLI's ``.env`` auto-loader for the duration of the test
  run.** A stray ``.env`` in the repo root or a CI checkout dir would
  otherwise leak values into the process environment and produce hard-to-
  debug, environment-dependent test failures. Tests that exercise the
  auto-loader directly clear this flag in a fixture and restore it on
  teardown.

* **Provide a Postgres state-store fixture.** v0.1-01 retired SQLite as
  a runtime backend, so every test that touches the state store needs a
  live Postgres. The session-scoped ``_postgres_container`` fixture
  brings up one container per pytest run; the function-scoped
  ``postgres_state_store_url`` fixture creates a fresh database inside
  that container for each test (so tests stay isolated without paying
  the per-test container-startup cost).

The fixtures skip cleanly when Docker / testcontainers isn't available,
which keeps the test suite runnable on developer machines without Docker
(at the cost of every state-store-touching test being skipped).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

os.environ["CARVE_NO_DOTENV"] = "1"


# ---------------------------------------------------------------------------
# Postgres state-store fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _postgres_container() -> Generator[PostgresContainer, None, None]:
    """Spin up one Postgres container for the entire pytest session.

    Per-test isolation is provided by ``postgres_state_store_url``, which
    creates a fresh database inside this single container — that's much
    cheaper than starting a new container per test (a fresh container is
    5-10s; a fresh database is <100ms).

    Skips if testcontainers isn't installed or Docker is unreachable.
    """
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import (
        PostgresContainer,
    )

    container = PostgresContainer("postgres:16-alpine")
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Docker not available for testcontainers: {exc}")

    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
def postgres_state_store_url(
    _postgres_container: PostgresContainer,
) -> Generator[str, None, None]:
    """Return a SQLAlchemy URL for a fresh, isolated Postgres database.

    Creates a database named ``carve_test_<uuid>`` inside the shared
    session-scoped container, yields its URL, then drops the database on
    teardown. Each test gets its own schema-clean database so tests can
    share the container without contaminating one another.

    The URL uses the ``postgresql+psycopg://`` driver (psycopg v3, our
    runtime dependency) so engines created against it behave identically
    to the runtime engine.
    """
    pytest.importorskip("psycopg")
    import psycopg

    container = _postgres_container
    db_name = f"carve_test_{uuid.uuid4().hex[:12]}"
    admin_url = container.get_connection_url()
    # testcontainers returns psycopg2-flavoured URLs; we want psycopg v3
    # everywhere our runtime engine touches. Build the psycopg v3 URL
    # too — same DSN, different driver suffix.
    pg_admin_url = admin_url.replace("postgresql+psycopg2://", "postgresql://", 1).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )

    # Connect to the default `postgres` admin DB to issue CREATE/DROP
    # DATABASE — those statements can't run inside the target database.
    with psycopg.connect(pg_admin_url, autocommit=True) as admin_conn:
        admin_conn.execute(f'CREATE DATABASE "{db_name}"')

    # Build a per-test URL pointing at the freshly created database,
    # using the runtime's psycopg-v3 driver string.
    parts = pg_admin_url.rsplit("/", 1)
    base_url = parts[0]
    runtime_url = base_url.replace("postgresql://", "postgresql+psycopg://", 1) + f"/{db_name}"

    try:
        yield runtime_url
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin_conn:
            # Terminate any lingering connections so DROP DATABASE doesn't
            # block on an open client.
            admin_conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


@pytest.fixture
def postgres_config(postgres_state_store_url: str) -> Any:
    """Return a minimal `Config` pointing at the per-test Postgres database.

    Tests that previously built a `Config` with ``ServerConfig(state_store=...)``
    can replace that with this fixture — the resolved URL flows through
    the same engine factory the runtime uses.
    """
    # Local import — keeps the conftest fast to import for tests that
    # don't touch the state store at all.
    from carve.core.config.schema import (
        Config,
        ModelsConfig,
        ProjectConfig,
        ServerConfig,
    )
    from carve.core.config.state_store import StateStoreConfig

    return Config(
        project=ProjectConfig(name="test-project"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
