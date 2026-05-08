"""Pre-flight checks for ``carve el deploy``.

Phase 1 of the deploy flow. Read-only against the destination's deploy
role. Surfaces drift before any writes happen so the recovery agent
has a chance to fix the build (or surface a manual remediation) while
the destination is still in its original state.

Three checks today:

1. Connectivity — can the deploy role connect at all?
2. Destination column drift — for each table the build expects to
   land, do the columns already in Snowflake (if any) match? Missing
   tables are tolerated (the DDL will create them); existing tables
   with mismatched column sets emit a `PreflightDrift` entry.
3. Runtime role existence — `SHOW ROLES LIKE '<runtime>'`.

The verifier's column comparison logic lives in `verifier.py`; this
module reuses the same helpers via `_compare_columns` so behavior
stays consistent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from carve.core.deploy.identifiers import (
    InvalidSnowflakeIdentifierError,
    validate_identifier,
)

if TYPE_CHECKING:
    from carve.core.connectors.snowflake import SnowflakeConnection
    from carve.core.state.models import Build


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightDrift:
    """One drift item surfaced from pre-flight."""

    kind: str  # "missing_column", "extra_column", "type_mismatch", "missing_role"
    detail: str


@dataclass
class PreflightResult:
    """Outcome of `run_preflight`.

    `connected` is true once the deploy role has authenticated.
    `drift` lists everything that diverges from the build's manifest.
    `expected_destinations` is the parsed list of (database, schema,
    table) triples used by the verifier to know what to check.
    """

    connected: bool = False
    drift: list[PreflightDrift] = field(default_factory=list)
    expected_destinations: list[tuple[str, str, str]] = field(default_factory=list)
    expected_columns: dict[tuple[str, str, str], list[tuple[str, str]]] = field(
        default_factory=dict
    )
    runtime_role: str | None = None

    @property
    def ok(self) -> bool:
        """True iff the connection succeeded and no drift was found."""
        return self.connected and not self.drift


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_preflight(
    *,
    deploy_connection: SnowflakeConnection,
    runtime_role: str | None,
    plan_design: dict[str, Any] | None = None,
) -> PreflightResult:
    """Run read-only checks against the destination via the deploy role.

    ``deploy_connection`` is the wrapped Snowflake connection for the
    destination's deploy role. ``plan_design`` is the parsed plan
    ``task_graph_json["design"]`` block (the build binds to a plan
    and the plan carries the destination spec).

    The function never raises — failures are surfaced as drift items
    so the caller (deploy CLI) can hand them to the recovery agent.
    """
    result = PreflightResult()

    # 1. Connect. A connection failure short-circuits the rest of the
    # checks; the caller will treat this as exit-2 with no recovery
    # (auth failures aren't agent-fixable).
    try:
        deploy_connection.connect()
        result.connected = True
    except Exception as exc:  # SnowflakeError or anything from the driver
        result.drift.append(
            PreflightDrift(kind="connection", detail=f"deploy role auth failed: {exc}")
        )
        return result

    # 2. Discover expected destinations from the plan design.
    try:
        expected = _expected_destinations_from_design(plan_design)
    except InvalidSnowflakeIdentifierError as exc:
        result.drift.append(
            PreflightDrift(kind="invalid_identifier", detail=str(exc))
        )
        return result
    result.expected_destinations = [
        (d, s, t) for (d, s, t, _cols) in expected
    ]
    result.expected_columns = {
        (d, s, t): list(cols) for (d, s, t, cols) in expected
    }

    # 3. Per-destination column comparison. Tables that don't exist
    # yet are fine (DDL will create them); existing tables with a
    # different column set surface as drift.
    for database, schema, table, expected_columns in expected:
        try:
            actual = _fetch_existing_columns(
                deploy_connection,
                database=database,
                schema=schema,
                table=table,
            )
        except Exception as exc:
            result.drift.append(
                PreflightDrift(
                    kind="lookup_failed",
                    detail=(
                        f"could not query columns for "
                        f"{database}.{schema}.{table}: {exc}"
                    ),
                )
            )
            continue
        if actual is None:
            # Table doesn't exist yet — DDL will create it. No drift.
            continue
        for drift in _compare_columns(
            qualified=f"{database}.{schema}.{table}",
            expected=expected_columns,
            actual=actual,
        ):
            result.drift.append(drift)

    # 4. Runtime role existence — only meaningful if a runtime role
    # was provided. If it's missing, surface as drift; recovery agent
    # may suggest a CREATE ROLE the user runs by hand.
    result.runtime_role = runtime_role
    if runtime_role is not None:
        # Validate the role name against the unquoted-identifier grammar
        # before interpolating it. Same defense-in-depth pattern as the
        # destination identifiers (database/schema/table). Role names
        # come from connections.toml — typically user-trusted but the
        # validator closes the loophole for hand-edits / future code
        # paths that thread agent-emitted text through.
        try:
            validate_identifier(runtime_role, kind="role")
        except InvalidSnowflakeIdentifierError as exc:
            result.drift.append(
                PreflightDrift(
                    kind="invalid_identifier",
                    detail=str(exc),
                )
            )
            return result
        try:
            rows = deploy_connection.query(
                f"SHOW ROLES LIKE '{runtime_role}'"
            )
        except Exception as exc:
            result.drift.append(
                PreflightDrift(
                    kind="lookup_failed",
                    detail=f"could not check for runtime role {runtime_role!r}: {exc}",
                )
            )
        else:
            if not rows:
                result.drift.append(
                    PreflightDrift(
                        kind="missing_role",
                        detail=(
                            f"runtime role {runtime_role!r} does not exist in the "
                            "destination Snowflake account."
                        ),
                    )
                )

    return result


# ---------------------------------------------------------------------------
# Helpers (also used by the verifier)
# ---------------------------------------------------------------------------


def _expected_destinations_from_design(
    design: dict[str, Any] | None,
) -> list[tuple[str, str, str, list[tuple[str, str]]]]:
    """Extract `(database, schema, table, columns)` from a plan design.

    M1's plan emits a single `destination` block with database/schema/
    table fields plus a `columns` list of `{name, type}` entries. The
    schema is forward-compatible with future multi-destination plans —
    if we ever see a `destinations` (plural) list, we'll iterate it.
    """
    if not isinstance(design, dict):
        return []
    out: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    plural = design.get("destinations")
    candidates: list[dict[str, Any]] = []
    if isinstance(plural, list):
        candidates.extend(d for d in plural if isinstance(d, dict))
    single = design.get("destination")
    if isinstance(single, dict):
        candidates.append(single)
    for dest in candidates:
        database = _coerce_str(dest.get("database"))
        schema = _coerce_str(dest.get("schema"))
        table = _coerce_str(dest.get("table"))
        if not (database and schema and table):
            continue
        # Validate at the boundary — these values are about to be
        # f-string interpolated into Snowflake queries with no
        # binding support.
        validate_identifier(database, kind="database")
        validate_identifier(schema, kind="schema")
        validate_identifier(table, kind="table")
        cols_raw = dest.get("columns")
        # Columns can live on the destination or alongside it; the
        # design tree typically nests them under the top-level
        # `columns` key as well. Pick the destination's own list when
        # present, else fall back to the top-level.
        if not isinstance(cols_raw, list):
            cols_raw = design.get("columns")
        columns = _normalize_columns(cols_raw)
        out.append((database, schema, table, columns))
    return out


def _normalize_columns(cols_raw: Any) -> list[tuple[str, str]]:
    """Coerce a columns-list-of-dicts into `[(name_upper, type_upper)]`.

    Comparisons are case-insensitive on Snowflake side, so we
    normalize once on the way in and compare upper-case throughout.
    """
    if not isinstance(cols_raw, list):
        return []
    out: list[tuple[str, str]] = []
    for entry in cols_raw:
        if not isinstance(entry, dict):
            continue
        name = _coerce_str(entry.get("name"))
        col_type = _coerce_str(entry.get("type"))
        if not name:
            continue
        out.append((name.upper(), col_type.upper()))
    return out


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _fetch_existing_columns(
    connection: SnowflakeConnection,
    *,
    database: str,
    schema: str,
    table: str,
) -> list[tuple[str, str]] | None:
    """Return the live column list, or `None` if the table doesn't exist.

    Uses ``INFORMATION_SCHEMA.COLUMNS`` so it works against the deploy
    role's view as well as the runtime role's. Bind parameters guard
    against injection on the schema / table literals; the
    ``database`` identifier must be interpolated (Snowflake doesn't
    support binding it) and is validated here for defense in depth —
    the public boundary helpers also validate, but this guard lets
    the function be safely invoked standalone in tests.
    """
    validate_identifier(database, kind="database")
    rows = connection.query(
        f"SELECT column_name, data_type "
        f"FROM {database}.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE table_schema = %(schema)s AND table_name = %(table)s "
        "ORDER BY ordinal_position",
        params={"schema": schema.upper(), "table": table.upper()},
    )
    if not rows:
        return None
    out: list[tuple[str, str]] = []
    for row in rows:
        name = _coerce_str(row.get("COLUMN_NAME") or row.get("column_name"))
        dtype = _coerce_str(row.get("DATA_TYPE") or row.get("data_type"))
        out.append((name.upper(), dtype.upper()))
    return out


def _compare_columns(
    *,
    qualified: str,
    expected: list[tuple[str, str]],
    actual: list[tuple[str, str]],
) -> list[PreflightDrift]:
    """Diff two `(name, type)` lists into drift entries.

    Type comparison normalizes Snowflake's type aliases (TEXT/VARCHAR,
    NUMBER/DECIMAL/INT*) so a build that says ``VARCHAR(50)`` and a
    table that reports ``TEXT`` doesn't trip a false positive.
    """
    drift: list[PreflightDrift] = []
    expected_map = {name: type_str for name, type_str in expected}
    actual_map = {name: type_str for name, type_str in actual}

    for name, expected_type in expected_map.items():
        if name not in actual_map:
            drift.append(
                PreflightDrift(
                    kind="missing_column",
                    detail=f"{qualified}: column {name!r} expected but missing",
                )
            )
            continue
        if not _types_compatible(expected_type, actual_map[name]):
            drift.append(
                PreflightDrift(
                    kind="type_mismatch",
                    detail=(
                        f"{qualified}: column {name!r} expected {expected_type!r} "
                        f"but is {actual_map[name]!r}"
                    ),
                )
            )

    for name in actual_map:
        if name not in expected_map:
            drift.append(
                PreflightDrift(
                    kind="extra_column",
                    detail=(
                        f"{qualified}: column {name!r} present in destination "
                        "but not in build manifest"
                    ),
                )
            )

    return drift


_TYPE_FAMILIES: tuple[frozenset[str], ...] = (
    # Snowflake reports VARCHAR variants as TEXT in INFORMATION_SCHEMA;
    # the build emits VARCHAR(N) and that's a compatible match.
    frozenset({"TEXT", "VARCHAR", "STRING", "CHAR", "CHARACTER"}),
    # Numerics — INT* are NUMBER aliases. DECIMAL == NUMERIC == NUMBER.
    frozenset(
        {
            "NUMBER",
            "DECIMAL",
            "NUMERIC",
            "INT",
            "INTEGER",
            "BIGINT",
            "SMALLINT",
            "TINYINT",
            "BYTEINT",
        }
    ),
    frozenset({"FLOAT", "DOUBLE", "REAL", "FLOAT8", "FLOAT4", "DOUBLE PRECISION"}),
    frozenset({"BOOLEAN", "BOOL"}),
    frozenset({"DATE"}),
    frozenset({"TIME"}),
    frozenset({"TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "DATETIME"}),
)


def _types_compatible(expected: str, actual: str) -> bool:
    """Return True if `expected` and `actual` are equivalent Snowflake types.

    The build emits ``VARCHAR(N)`` style with parameters; INFORMATION_SCHEMA
    reports the unparameterized base. Strip parameters and compare against
    the family table.
    """
    norm_expected = _strip_type_params(expected).upper()
    norm_actual = _strip_type_params(actual).upper()
    if norm_expected == norm_actual:
        return True
    for family in _TYPE_FAMILIES:
        if norm_expected in family and norm_actual in family:
            return True
    return False


def _strip_type_params(type_str: str) -> str:
    """``VARCHAR(50)`` → ``VARCHAR``."""
    paren = type_str.find("(")
    return type_str[:paren].strip() if paren > 0 else type_str.strip()



# `expected_destinations_from_build` — small helper exported for the
# verifier so it can avoid duplicating manifest-reading logic.

def expected_destinations_from_build(
    build: Build,
    plan_design: dict[str, Any] | None,
) -> list[tuple[str, str, str, list[tuple[str, str]]]]:
    """Public re-export. Same payload as the private helper.

    Today the manifest only stores `files`, so we read destinations
    out of the plan design instead. If we ever extend
    `Build.manifest_json` to carry destinations directly, we'll
    consult it first and fall back to the plan.
    """
    # Manifest doesn't currently carry destinations; future-proof
    # by checking it first.
    try:
        manifest = json.loads(build.manifest_json)
    except (TypeError, ValueError):
        manifest = {}
    destinations = manifest.get("destinations")
    if isinstance(destinations, list) and destinations:
        out: list[tuple[str, str, str, list[tuple[str, str]]]] = []
        for entry in destinations:
            if not isinstance(entry, dict):
                continue
            database = _coerce_str(entry.get("database"))
            schema = _coerce_str(entry.get("schema"))
            table = _coerce_str(entry.get("table"))
            if not (database and schema and table):
                continue
            # Same boundary validation as the design extractor —
            # manifest values are also written by an LLM-emitted
            # plan ultimately and flow into f-string interpolation.
            validate_identifier(database, kind="database")
            validate_identifier(schema, kind="schema")
            validate_identifier(table, kind="table")
            cols = _normalize_columns(entry.get("columns"))
            out.append((database, schema, table, cols))
        return out
    return _expected_destinations_from_design(plan_design)


__all__ = [
    "PreflightDrift",
    "PreflightResult",
    "expected_destinations_from_build",
    "run_preflight",
]
