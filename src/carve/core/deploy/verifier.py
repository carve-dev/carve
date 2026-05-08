"""Post-deploy verification (Phase 7) and standalone ``carve el verify``.

Uses the **runtime role** (the role the user pipelines run as). Three
checks today:

1. Each expected destination table exists with the expected columns.
   Re-uses `preflight._compare_columns` so behavior matches the Phase
   1 drift check.
2. The runtime role has ``SELECT, INSERT, UPDATE, DELETE`` on each
   destination table. Pulled from ``SHOW GRANTS ON TABLE`` filtered by
   ``grantee_name = <runtime>``.
3. (Optional) Smoke test — ``SELECT 1 FROM <db>.<schema>.<table> LIMIT 1``
   against each destination. Skipped when ``smoke_test=False`` (the
   ``--no-smoke-test`` CLI flag).

`run_verify` returns a `VerifyResult` whose `ok` is True iff every
check passed. The caller (CLI / deploy command) decides what to do
on failure — verify itself is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from carve.core.deploy.identifiers import validate_identifier
from carve.core.deploy.preflight import (
    _coerce_str,
    _compare_columns,
    _fetch_existing_columns,
    expected_destinations_from_build,
)

if TYPE_CHECKING:
    from carve.core.connectors.snowflake import SnowflakeConnection
    from carve.core.state.models import Build


_REQUIRED_GRANTS: tuple[str, ...] = ("SELECT", "INSERT", "UPDATE", "DELETE")


@dataclass
class VerifyResult:
    """Outcome of `run_verify`.

    `failures` is a list of human-readable diagnosis strings. `ok` is
    true when the list is empty.
    """

    failures: list[str] = field(default_factory=list)
    checked_destinations: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def run_verify(
    *,
    runtime_connection: SnowflakeConnection,
    build: Build,
    plan_design: dict[str, Any] | None = None,
    runtime_role: str | None = None,
    smoke_test: bool = True,
) -> VerifyResult:
    """Run read-only checks against the destination via the runtime role.

    `runtime_role` is the role name the runtime connection authenticates
    as; used when checking grants. If ``None``, the grants check is
    skipped (callers should pass ``runtime_connection.config.role`` in
    practice).
    """
    result = VerifyResult()

    try:
        expected = expected_destinations_from_build(build, plan_design)
    except ValueError as exc:
        # InvalidSnowflakeIdentifierError (or other ValueErrors from
        # the boundary validator) — surface as a failure, never let
        # an unsafe value reach the SQL.
        result.failures.append(str(exc))
        return result

    if not expected:
        result.failures.append(
            "no destinations found in build manifest or plan design"
        )
        return result

    for database, schema, table, expected_columns in expected:
        # Defense-in-depth: validate before constructing the qualified
        # name. The boundary already validated, but a future caller
        # might bypass it.
        validate_identifier(database, kind="database")
        validate_identifier(schema, kind="schema")
        validate_identifier(table, kind="table")
        qualified = f"{database}.{schema}.{table}"
        result.checked_destinations.append((database, schema, table))

        # 1. Column presence + type comparison.
        try:
            actual = _fetch_existing_columns(
                runtime_connection,
                database=database,
                schema=schema,
                table=table,
            )
        except Exception as exc:
            result.failures.append(
                f"could not list columns for {qualified}: {exc}"
            )
            continue
        if actual is None:
            result.failures.append(
                f"destination {qualified} does not exist"
            )
            continue
        for drift in _compare_columns(
            qualified=qualified,
            expected=expected_columns,
            actual=actual,
        ):
            result.failures.append(drift.detail)

        # 2. Runtime role grants.
        if runtime_role is not None:
            try:
                granted = _fetch_grants(
                    runtime_connection,
                    database=database,
                    schema=schema,
                    table=table,
                    runtime_role=runtime_role,
                )
            except Exception as exc:
                result.failures.append(
                    f"could not check grants on {qualified}: {exc}"
                )
                continue
            for required in _REQUIRED_GRANTS:
                if required not in granted:
                    result.failures.append(
                        f"{qualified}: runtime role {runtime_role!r} missing "
                        f"{required} privilege"
                    )

        # 3. Smoke test — single-row fetch to confirm queryability.
        if smoke_test:
            try:
                runtime_connection.query(
                    f"SELECT 1 AS smoke FROM {qualified} LIMIT 1"
                )
            except Exception as exc:
                result.failures.append(
                    f"{qualified}: smoke test SELECT failed: {exc}"
                )

    return result


def _fetch_grants(
    connection: SnowflakeConnection,
    *,
    database: str,
    schema: str,
    table: str,
    runtime_role: str,
) -> set[str]:
    """Return the set of privileges held by ``runtime_role`` on the table.

    ``SHOW GRANTS ON TABLE`` returns a list of grant rows; we filter
    on ``grantee_name`` and collect ``privilege`` values.
    """
    # Defense-in-depth — also validated at the call site, but this
    # function is callable in isolation (tests, future ad-hoc tools).
    validate_identifier(database, kind="database")
    validate_identifier(schema, kind="schema")
    validate_identifier(table, kind="table")
    qualified = f"{database}.{schema}.{table}"
    rows = connection.query(f"SHOW GRANTS ON TABLE {qualified}")
    granted: set[str] = set()
    runtime_upper = runtime_role.upper()
    for row in rows:
        grantee = _coerce_str(
            row.get("grantee_name") or row.get("GRANTEE_NAME")
        ).upper()
        if grantee != runtime_upper:
            continue
        priv = _coerce_str(row.get("privilege") or row.get("PRIVILEGE")).upper()
        if priv:
            granted.add(priv)
    return granted


__all__ = ["VerifyResult", "run_verify"]
