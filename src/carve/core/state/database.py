"""Engine and session helpers for the state store.

The connection string comes from `config.server.state_store` (see
`carve.core.config.schema.ServerConfig`). For SQLite we enable WAL mode
and `synchronous=NORMAL` on every connection so concurrent reads from
the CLI and the future API server don't block each other.

`initialize_database` runs Alembic migrations to ``head`` so a fresh
database lands on the latest schema and existing dev databases get the
new columns when they upgrade across spec boundaries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from carve.core.config import Config

# Repository-relative paths to the Alembic ini file and migrations
# directory. Discovered by walking up from this module — the runtime
# never `cd`s into the repo, so we have to find these deterministically.
# Layout: src/carve/core/state/database.py  → parents[4] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"


def create_engine_from_config(config: Config, *, project_dir: Path | None = None) -> Engine:
    """Build a SQLAlchemy engine from the given Carve config.

    For SQLite URLs of the form ``sqlite:///<relative-path>`` the path is
    resolved against ``project_dir`` (defaulting to the current working
    directory) so the database file lives inside the project, not wherever
    the CLI happens to have been invoked from.
    """
    url = _resolve_sqlite_url(config.server.state_store, project_dir or Path.cwd())
    engine = create_engine(url, echo=False, future=True)

    if engine.url.get_backend_name() == "sqlite":
        _install_sqlite_pragmas(engine)

    return engine


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

    Called from `carve init` and as a best-effort safeguard from each CLI
    command that opens an engine. Idempotent — re-running on an up-to-date
    database is a fast no-op (Alembic checks the `alembic_version` table).

    For an existing M1 database that pre-dates Alembic, the function
    detects the legacy schema (`runs` exists, `alembic_version` does not)
    and stamps the baseline before running `upgrade head` so the
    pipeline-centric migration applies cleanly without trying to recreate
    tables that are already there.
    """
    parent = _sqlite_path_parent(engine)
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)

    cfg = _alembic_config(engine)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    has_baseline_tables = {"runs", "logs", "plans"}.issubset(table_names)
    has_alembic_table = "alembic_version" in table_names

    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        if has_baseline_tables and not has_alembic_table:
            # Pre-Alembic dev DB; jump the version pointer past the
            # baseline so 0001 isn't re-run against existing tables.
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
# SQLite helpers
# ---------------------------------------------------------------------------


def _resolve_sqlite_url(url: str, project_dir: Path) -> str:
    """Resolve a relative ``sqlite:///`` URL against ``project_dir``.

    Postgres and other URLs are returned unchanged. Already-absolute
    SQLite URLs (``sqlite:////abs/path``) are also returned unchanged.
    The in-memory form (``sqlite:///:memory:``) is preserved.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return url

    path_part = url[len(prefix) :]
    if path_part == ":memory:" or path_part == "":
        return url
    if path_part.startswith("/"):
        # Already absolute — `sqlite:////abs/path` arrives here as `/abs/path`.
        return url

    absolute = (project_dir / path_part).resolve()
    return f"{prefix}{absolute}"


def _sqlite_path_parent(engine: Engine) -> Path | None:
    """Return the parent directory of a file-backed SQLite engine, or None."""
    if engine.url.get_backend_name() != "sqlite":
        return None
    db = engine.url.database
    if not db or db == ":memory:":
        return None
    return Path(db).parent


def _install_sqlite_pragmas(engine: Engine) -> None:
    """Apply WAL mode and `synchronous=NORMAL` to every SQLite connection.

    The pragmas have to be set *per connection*; SQLAlchemy raises a
    fresh DB-API connection for each pool checkout, so we hook the
    `connect` event rather than executing once on the engine.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # SQLite ships with FK enforcement off by default; the schema
            # has real foreign keys (Pipeline.current_build_id → Build,
            # Build.plan_id → Plan, Plan.pipeline_name → Pipeline,
            # Run.pipeline_name → Pipeline) that need to be enforced or
            # they're advisory only.
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
