"""Built-in catalog skills — Layer 1 of the skill architecture.

Five cheap, deterministic Snowflake `INFORMATION_SCHEMA` queries the
plan agent uses to ground its design in actual schema data:

- `list_databases()` — accessible databases.
- `list_schemas(database)` — schemas under a database (cap 100).
- `list_tables(database, schema, include_views=True)` — tables/views
  (cap 200).
- `describe_table(database, schema, table)` — column rows.
- `table_exists(database, schema, table)` — boolean convenience.

All skills return a `SkillResult`. Capped queries set `truncated=True`
and `total_count` to the un-truncated row count when the cap is hit.
"""

from __future__ import annotations

import re
from typing import Any

from carve.core.skills.context import SkillContext
from carve.core.skills.decorator import skill
from carve.core.skills.result import SkillResult

# Caps. `list_databases` is uncapped — accounts almost never have more
# than a handful of databases. Schemas and tables can blow up, hence
# the explicit limits.
_LIST_SCHEMAS_CAP = 100
_LIST_TABLES_CAP = 200


# Snowflake unquoted-identifier grammar (letters, digits, underscores,
# starting with a letter or underscore). The driver only supports ``%(x)s``
# binding for *values*, not identifiers — to avoid interpolating an
# attacker- or hallucinated-LLM-controlled `database` name into the
# ``FROM <db>.information_schema.<view>`` clause, we require it to look
# like a normal identifier before letting it through.
_DATABASE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class InvalidDatabaseNameError(ValueError):
    """Raised when a catalog skill is asked to operate on a database
    name that fails the unquoted-identifier validation."""


def _validated_database(database: str) -> str:
    """Return ``database`` if it is a syntactically-valid Snowflake
    unquoted identifier, else raise.

    Snowflake identifiers can be quoted (which permits arbitrary
    characters), but the catalog skills don't need that flexibility —
    every Carve-managed database the agent should ever see is a plain
    identifier. Refusing the quoted form here closes the only
    SQL-injection surface in the catalog layer.
    """
    if not _DATABASE_NAME_RE.fullmatch(database):
        raise InvalidDatabaseNameError(
            f"database name {database!r} is not a valid Snowflake "
            "unquoted identifier (letters, digits, underscores; must "
            "start with a letter or underscore)."
        )
    return database


# ---------------------------------------------------------------------------
# list_databases
# ---------------------------------------------------------------------------


@skill(
    name="list_databases",
    description=(
        "List databases accessible to the runtime role on the active "
        "Snowflake target. Returns name, owner, and creation timestamp."
    ),
    inputs={},
    outputs={"databases": {"type": "array"}},
)
def list_databases(ctx: SkillContext) -> SkillResult:
    sf = ctx.snowflake_pool.get(ctx.target)
    rows = sf.query(
        "SELECT database_name, database_owner, created "
        "FROM information_schema.databases "
        "ORDER BY database_name"
    )
    return SkillResult(data={"databases": rows}, total_count=len(rows))


# ---------------------------------------------------------------------------
# list_schemas
# ---------------------------------------------------------------------------


@skill(
    name="list_schemas",
    description=(
        "List schemas in a Snowflake database accessible to the runtime "
        "role. Capped at 100 rows."
    ),
    inputs={
        "database": {"type": "string", "required": True},
    },
    outputs={"schemas": {"type": "array"}},
)
def list_schemas(ctx: SkillContext, database: str) -> SkillResult:
    database = _validated_database(database)
    sf = ctx.snowflake_pool.get(ctx.target)
    sql = (
        f"SELECT schema_name, schema_owner, created "
        f"FROM {database}.information_schema.schemata "
        f"WHERE schema_name <> 'INFORMATION_SCHEMA' "
        f"ORDER BY schema_name "
        f"LIMIT {_LIST_SCHEMAS_CAP + 1}"
    )
    rows = sf.query(sql)
    truncated = len(rows) > _LIST_SCHEMAS_CAP
    if truncated:
        rows = rows[:_LIST_SCHEMAS_CAP]
    return SkillResult(
        data={"schemas": rows},
        truncated=truncated,
        total_count=len(rows) if not truncated else None,
    )


# ---------------------------------------------------------------------------
# list_tables
# ---------------------------------------------------------------------------


@skill(
    name="list_tables",
    description=(
        "List tables and views in a Snowflake schema with row counts and "
        "byte sizes. Capped at 200 rows."
    ),
    inputs={
        "database": {"type": "string", "required": True},
        "schema": {"type": "string", "required": True},
        "include_views": {"type": "boolean", "default": True},
    },
    outputs={"tables": {"type": "array"}},
)
def list_tables(
    ctx: SkillContext,
    database: str,
    schema: str,
    include_views: bool = True,
) -> SkillResult:
    database = _validated_database(database)
    sf = ctx.snowflake_pool.get(ctx.target)
    base_filter = "WHERE table_schema = %(schema)s"
    if not include_views:
        base_filter = f"{base_filter} AND table_type = 'BASE TABLE'"
    sql = (
        f"SELECT table_name, table_type, row_count, bytes "
        f"FROM {database}.information_schema.tables "
        f"{base_filter} "
        f"ORDER BY table_name "
        f"LIMIT {_LIST_TABLES_CAP + 1}"
    )
    # Pull cap+1 to detect truncation cheaply, then issue a count query
    # only when the cap was hit (the common case is a small schema).
    rows = sf.query(sql, {"schema": schema.upper()})
    total_count: int | None = len(rows)
    truncated = len(rows) > _LIST_TABLES_CAP
    if truncated:
        # We over-fetched; report the actual full count by issuing a
        # COUNT(*) so the caller can see how many tables truly exist.
        count_rows = sf.query(
            f"SELECT COUNT(*) AS n FROM {database}.information_schema.tables "
            f"{base_filter}",
            {"schema": schema.upper()},
        )
        total_count = _coerce_count(count_rows)
        rows = rows[:_LIST_TABLES_CAP]
    return SkillResult(
        data={"tables": rows},
        truncated=truncated,
        total_count=total_count,
    )


def _coerce_count(rows: list[dict[str, Any]]) -> int | None:
    """Pull the integer count from a `SELECT COUNT(*) AS n` result."""
    if not rows:
        return None
    value = rows[0].get("N", rows[0].get("n"))
    if value is None:
        # Some drivers normalize differently; fall back to first value.
        for v in rows[0].values():
            value = v
            break
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# describe_table
# ---------------------------------------------------------------------------


@skill(
    name="describe_table",
    description=(
        "Return column metadata for a Snowflake table: name, type, "
        "nullability, and ordinal position."
    ),
    inputs={
        "database": {"type": "string", "required": True},
        "schema": {"type": "string", "required": True},
        "table": {"type": "string", "required": True},
    },
    outputs={"columns": {"type": "array"}},
)
def describe_table(
    ctx: SkillContext,
    database: str,
    schema: str,
    table: str,
) -> SkillResult:
    database = _validated_database(database)
    sf = ctx.snowflake_pool.get(ctx.target)
    sql = (
        f"SELECT column_name, data_type, is_nullable, ordinal_position "
        f"FROM {database}.information_schema.columns "
        f"WHERE table_schema = %(schema)s AND table_name = %(table)s "
        f"ORDER BY ordinal_position"
    )
    rows = sf.query(sql, {"schema": schema.upper(), "table": table.upper()})
    return SkillResult(data={"columns": rows}, total_count=len(rows))


# ---------------------------------------------------------------------------
# table_exists
# ---------------------------------------------------------------------------


@skill(
    name="table_exists",
    description=(
        "Return True if the named table or view exists in the given "
        "Snowflake schema. Useful before designing a destination table."
    ),
    inputs={
        "database": {"type": "string", "required": True},
        "schema": {"type": "string", "required": True},
        "table": {"type": "string", "required": True},
    },
    outputs={"exists": {"type": "boolean"}},
)
def table_exists(
    ctx: SkillContext,
    database: str,
    schema: str,
    table: str,
) -> SkillResult:
    database = _validated_database(database)
    sf = ctx.snowflake_pool.get(ctx.target)
    sql = (
        f"SELECT COUNT(*) AS n "
        f"FROM {database}.information_schema.tables "
        f"WHERE table_schema = %(schema)s AND table_name = %(table)s"
    )
    rows = sf.query(sql, {"schema": schema.upper(), "table": table.upper()})
    count = _coerce_count(rows) or 0
    return SkillResult(data={"exists": count > 0})
