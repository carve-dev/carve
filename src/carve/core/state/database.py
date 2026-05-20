"""Engine and session helpers for the state store.

The connection string is resolved from `Config` via
:func:`carve.core.config.state_store.resolve_state_store_url`. Postgres is
the only supported runtime backend; v0.1-01 retired the SQLite path. The
engine factory has a one-way door:

* ``postgresql+psycopg://...`` URLs are accepted everywhere
* ``sqlite:///<path>`` URLs are rejected at runtime with a friendly error
  string that points the user at ``carve migrate-state``

The migration tool itself constructs a *source* SQLite engine via
:func:`create_sqlite_source_engine` — that helper is the only sanctioned
SQLite entry point in the codebase.

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
    """Raised when the runtime is asked to open a non-Postgres state store.

    The migration tool's source-connection path catches this and falls
    back to :func:`create_sqlite_source_engine`; every other caller
    treats this as fatal.
    """


def create_engine_from_config(config: Config, *, project_dir: Path | None = None) -> Engine:
    """Build a SQLAlchemy engine from the given Carve config.

    Only Postgres URLs are accepted. SQLite URLs raise
    :class:`StateStoreBackendError` with a message pointing the user at
    ``carve migrate-state``. ``project_dir`` is accepted for backward
    compatibility but is unused for Postgres.
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


def create_sqlite_source_engine(url: str) -> Engine:
    """Build a read-only SQLAlchemy engine for a SQLite migration source.

    The migration tool (`carve migrate-state`) is the only caller. The
    URL must start with ``sqlite:///``; anything else raises ``ValueError``.
    File-resolution is the caller's responsibility — the migration tool
    accepts already-absolute paths on its CLI flag.
    """
    if not url.startswith("sqlite:///"):
        raise ValueError(f"create_sqlite_source_engine requires a sqlite URL; got {url!r}")
    return create_engine(url, echo=False, future=True)


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

    For an existing M1-era Postgres database that pre-dates Alembic
    (extremely unlikely, but kept defensively to mirror the M1 behavior),
    the function detects the legacy schema and stamps the baseline before
    running ``upgrade head``. SQLite is rejected here too — `carve serve`
    surfaces the friendly error via the engine factory before reaching
    this function, but a direct caller would otherwise see a confusing
    Alembic stack trace.
    """
    if engine.url.get_backend_name() != "postgresql":
        raise StateStoreBackendError(_SQLITE_MESSAGE)

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


_SQLITE_MESSAGE = (
    "Carve no longer runs against SQLite. Migrate your state store with "
    "`carve migrate-state --from sqlite:///<path> --to <postgres-url>` and "
    "set `state_store.url` in runtime.toml to the Postgres connection "
    "string. See docs/upgrade-from-walking-skeleton.md for the full guide."
)


def _reject_non_postgres_url(url: str) -> None:
    """Reject anything that isn't a Postgres URL at runtime.

    The check is intentionally a substring match on the scheme rather
    than a full parse: ``sqlite:///``, ``sqlite://`` and any future
    typo'd variant should all surface the same friendly message.
    """
    if url.startswith("sqlite:"):
        raise StateStoreBackendError(_SQLITE_MESSAGE)
    if not (url.startswith("postgresql://") or url.startswith("postgresql+psycopg://")):
        raise StateStoreBackendError(
            f"Unsupported state store URL {url!r}. Carve requires Postgres "
            f"(postgresql+psycopg://...). See docs/upgrade-from-walking-skeleton.md."
        )
