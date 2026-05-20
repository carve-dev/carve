"""Engine and session helpers for the state store.

The connection string is resolved from `Config` via
:func:`carve.core.config.state_store.resolve_state_store_url`. Postgres is
the only supported backend; v0.1-01 retired SQLite outright. Non-Postgres
URLs raise :class:`StateStoreBackendError` with a friendly message.

`initialize_database` runs Alembic migrations to ``head`` so a fresh
database lands on the latest schema (the OSS auto-migrate-on-startup
default; the hosted product overrides via its own startup flow).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from carve.core.config import Config
from carve.core.config.state_store import resolve_state_store_url

# Repository-relative paths to the Alembic ini file and migrations
# directory. Discovered by walking up from this module — the runtime
# never `cd`s into the repo, so we have to find these deterministically.
# Layout: src/carve/core/state/database.py  → parents[4] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"


class StateStoreBackendError(RuntimeError):
    """Raised when a non-Postgres state store URL is provided.

    Carve has no migration tool — v0.1+ is Postgres-only from day one.
    The error message points users at the bundled docker-compose path
    (or an external-Postgres connection string) as the fix.
    """


def create_engine_from_config(config: Config, *, project_dir: Path | None = None) -> Engine:
    """Build a SQLAlchemy engine from the given Carve config.

    Only Postgres URLs are accepted. Anything else raises
    :class:`StateStoreBackendError`. ``project_dir`` is accepted for
    backward compatibility but is unused for Postgres.
    """
    del project_dir  # unused for Postgres; kept for API compatibility
    url = resolve_state_store_url(config)
    _reject_non_postgres_url(url)
    return create_engine(
        url,
        echo=False,
        future=True,
        pool_size=config.state_store.pool_size,
        max_overflow=config.state_store.max_overflow,
        pool_pre_ping=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a `sessionmaker` configured for short, explicit transactions.

    `autoflush=False` keeps state predictable in repository methods that
    write a row and immediately read it back; `expire_on_commit=False`
    lets callers keep using returned ORM instances after `session.commit()`
    without triggering a re-load.
    """
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def initialize_database(engine: Engine) -> None:
    """Bring the database to the latest migration revision.

    Called from `carve init` and `carve serve` startup. Idempotent —
    re-running on an up-to-date database is a fast no-op (Alembic checks
    the `alembic_version` table).

    Non-Postgres engines are rejected with a friendly error; the engine
    factory normally catches this earlier but `initialize_database`
    surfaces the same message if called directly.
    """
    if engine.url.get_backend_name() != "postgresql":
        raise StateStoreBackendError(_NON_POSTGRES_MESSAGE)

    cfg = _alembic_config(engine)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    has_baseline_tables = {"runs", "logs", "plans"}.issubset(table_names)
    has_alembic_table = "alembic_version" in table_names

    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        if has_baseline_tables and not has_alembic_table:
            alembic_command.stamp(cfg, "0001_baseline")
        alembic_command.upgrade(cfg, "head")


def _alembic_config(engine: Engine) -> AlembicConfig:
    """Construct an Alembic `Config` pointing at the runtime engine.

    The ini file is loaded for `script_location` and logging settings;
    the URL is overridden so migrations target the same database the
    runtime engine is bound to. The connection itself is passed via
    `config.attributes` so env.py reuses it instead of opening a second.
    """
    cfg = AlembicConfig(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    return cfg


# ---------------------------------------------------------------------------
# URL-shape gating
# ---------------------------------------------------------------------------


_NON_POSTGRES_MESSAGE = (
    "Carve requires Postgres for its state store. Set DATABASE_URL or "
    "`state_store.url` in runtime.toml to a Postgres connection string "
    "(postgresql+psycopg://user:pass@host:port/db). For local development, "
    "the bundled `docker-compose.yml` brings up Postgres with sensible "
    "defaults — see docs/installation.md."
)


def _reject_non_postgres_url(url: str) -> None:
    """Reject any URL that isn't a Postgres connection string."""
    if not (url.startswith("postgresql://") or url.startswith("postgresql+psycopg://")):
        raise StateStoreBackendError(_NON_POSTGRES_MESSAGE)
