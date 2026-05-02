"""Alembic environment for Carve's state store.

Two paths are supported:

* **Programmatic** — `carve.core.state.database.run_migrations(engine)` calls
  `command.upgrade(cfg, "head")` after pinning the URL on a synthesized
  `Config`. The env then reads the URL straight off `config.attributes`
  via the ``connection`` key (the conventional Alembic pattern).
* **CLI** — `alembic upgrade head` (rare in normal use; mostly for repo
  maintainers writing new migrations). Falls back to ``sqlalchemy.url``
  from ``alembic.ini``.

Both paths converge on the same ``run_migrations_online`` body. Offline
mode (`alembic upgrade --sql`) is supported but not used by the runtime.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Alembic's `Config` is the ini object plus runtime attributes. The
# runtime call sets ``connection`` (an SQLAlchemy `Connection`) on
# ``config.attributes`` and we honor it; the CLI path falls back to the
# ini's `sqlalchemy.url`.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

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
        render_as_batch=True,
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

    `render_as_batch=True` makes ALTER TABLE work on SQLite, which doesn't
    support most ALTER operations natively. The "batch" mode rewrites the
    table in a copy. Our migrations stick to additive operations, but the
    flag is the right default for a project that ships a SQLite default.

    SQLite FK enforcement is disabled for the duration of the migration:
    `batch_alter_table` rewrites tables via DROP+CREATE, which fails when
    foreign keys point at the table being dropped. The runtime listener
    re-enables FK checks on every fresh connection, so this only affects
    the migration session.
    """
    is_sqlite = False
    if hasattr(connection, "dialect"):
        is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")  # type: ignore[attr-defined]

    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()

    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
