"""Engine and session helpers for the state store.

The connection string comes from `config.server.state_store` (see
`carve.core.config.schema.ServerConfig`). For SQLite we enable WAL mode
and `synchronous=NORMAL` on every connection so concurrent reads from
the CLI and the future API server don't block each other.

`initialize_database` creates the schema via
`Base.metadata.create_all`. M1 doesn't ship alembic; the first M2 spec
that needs a schema change will introduce it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from carve.core.config import Config
from carve.core.state.models import Base


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
    """Create all known tables if they don't already exist.

    Called from `carve init`. Idempotent — re-running on an existing
    database is a no-op.
    """
    parent = _sqlite_path_parent(engine)
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


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
        finally:
            cursor.close()
