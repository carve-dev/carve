"""Alembic environment for Carve's state store.

Two paths are supported:

* **Programmatic** — `carve.core.state.database.initialize_database(engine)`
  pins the runtime connection onto ``config.attributes['connection']`` and
  calls `command.upgrade(cfg, "head")`. The env reads the connection off
  ``config.attributes`` and reuses it (the conventional Alembic pattern).
* **CLI** — ``alembic upgrade head`` (rare in normal use; mostly for repo
  maintainers writing new migrations). Falls back to ``sqlalchemy.url``
  from ``alembic.ini``, overridden by the ``DATABASE_URL`` env var if set.

v0.1-01 retired SQLite as a runtime backend. Postgres is the only
supported target — the env no longer drops to batch-rewrite mode for
DDL operations and no longer toggles SQLite FK PRAGMAs.

Offline mode (`alembic upgrade --sql`) is supported but not used by the
runtime.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Alembic's `Config` is the ini object plus runtime attributes. The
# runtime call sets ``connection`` (an SQLAlchemy `Connection`) on
# ``config.attributes`` and we honor it; the CLI path falls back to the
# ini's `sqlalchemy.url`, with ``DATABASE_URL`` taking precedence so
# contributors can point alembic at their own Postgres without editing
# the ini.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_database_url_env = os.environ.get("DATABASE_URL")
if _database_url_env:
    config.set_main_option("sqlalchemy.url", _database_url_env)

# We don't autogenerate against `Base.metadata`; migrations are written
# by hand. Setting `target_metadata = None` keeps `--autogenerate` honest
# (it will refuse to run, surfacing a clear error if anyone tries).
target_metadata = None


def run_migrations_offline() -> None:
    """Generate SQL without binding to a live engine."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live engine.

    Honors a pre-built ``connection`` on ``config.attributes`` (used by
    the runtime path so we don't open a second connection just to run
    migrations). Falls back to building one from the ini URL when invoked
    via the Alembic CLI.
    """
    connectable = config.attributes.get("connection")

    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


def _run(connection: object) -> None:
    """Configure context and dispatch the migration scripts.

    Postgres handles ALTER TABLE natively, so we no longer pass
    ``render_as_batch=True`` — that mode was a SQLite concession. The
    migrations still use ``op.batch_alter_table`` blocks in places; on
    Postgres those compile down to direct ALTER statements automatically.
    """
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
