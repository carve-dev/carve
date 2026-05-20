"""Unit tests for ``carve.core.deploy.preflight``.

Drives `run_preflight` against an in-memory `_FakeSnowflake` that
records SQL and returns canned column rows.
"""

from __future__ import annotations

from typing import Any

import pytest

from carve.core.deploy.identifiers import InvalidSnowflakeIdentifierError
from carve.core.deploy.preflight import (
    PreflightDrift,
    expected_destinations_from_build,
    run_preflight,
)
from carve.core.state.models import Build


class _FakeSnowflake:
    """Minimal fake matching the SnowflakeConnection API surface."""

    def __init__(
        self,
        *,
        column_rows: list[dict[str, Any]] | None = None,
        role_rows: list[dict[str, Any]] | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self._column_rows = column_rows
        self._role_rows = role_rows or []
        self._connect_error = connect_error
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.connected = False

    def connect(self) -> object:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True
        return self

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        del limit
        self.queries.append((sql, params))
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return list(self._column_rows or [])
        if "SHOW ROLES" in sql:
            return list(self._role_rows)
        return []


def _build(plan_id: str = "plan_1") -> Build:
    return Build(
        id="build_1",
        pipeline_name="iowa",
        plan_id=plan_id,
        target="dev",
        manifest_json={"files": []},
    )


def _design() -> dict[str, Any]:
    return {
        "destination": {
            "database": "ANALYTICS",
            "schema": "RAW",
            "table": "IOWA",
        },
        "columns": [
            {"name": "ID", "type": "NUMBER"},
            {"name": "STORE", "type": "VARCHAR(50)"},
        ],
    }


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------


def test_preflight_marks_connected_on_success() -> None:
    fake = _FakeSnowflake(column_rows=[])
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role=None,
        plan_design=_design(),
    )
    assert result.connected
    assert fake.connected


def test_preflight_records_connection_failure_as_drift() -> None:
    fake = _FakeSnowflake(connect_error=RuntimeError("auth nope"))
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role=None,
        plan_design=_design(),
    )
    assert not result.connected
    assert result.drift
    assert any(d.kind == "connection" for d in result.drift)


# ---------------------------------------------------------------------------
# Column drift
# ---------------------------------------------------------------------------


def test_preflight_clean_when_destination_missing() -> None:
    """No columns returned → table doesn't exist; DDL will create it."""
    fake = _FakeSnowflake(column_rows=[])
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role=None,
        plan_design=_design(),
    )
    assert result.connected
    assert result.drift == []


def test_preflight_detects_missing_column() -> None:
    fake = _FakeSnowflake(
        column_rows=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
            # STORE missing
        ]
    )
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role=None,
        plan_design=_design(),
    )
    drifts = [d for d in result.drift if d.kind == "missing_column"]
    assert drifts
    assert "STORE" in drifts[0].detail


def test_preflight_detects_type_mismatch() -> None:
    fake = _FakeSnowflake(
        column_rows=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
            {"COLUMN_NAME": "STORE", "DATA_TYPE": "BOOLEAN"},
        ]
    )
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role=None,
        plan_design=_design(),
    )
    drifts = [d for d in result.drift if d.kind == "type_mismatch"]
    assert drifts


def test_preflight_treats_text_and_varchar_as_compatible() -> None:
    fake = _FakeSnowflake(
        column_rows=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
            {"COLUMN_NAME": "STORE", "DATA_TYPE": "TEXT"},
        ]
    )
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role=None,
        plan_design=_design(),
    )
    assert [d for d in result.drift if d.kind == "type_mismatch"] == []


def test_preflight_detects_extra_column() -> None:
    fake = _FakeSnowflake(
        column_rows=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
            {"COLUMN_NAME": "STORE", "DATA_TYPE": "TEXT"},
            {"COLUMN_NAME": "BONUS", "DATA_TYPE": "TEXT"},
        ]
    )
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role=None,
        plan_design=_design(),
    )
    extras = [d for d in result.drift if d.kind == "extra_column"]
    assert extras


# ---------------------------------------------------------------------------
# Runtime role
# ---------------------------------------------------------------------------


def test_preflight_missing_runtime_role_drift() -> None:
    fake = _FakeSnowflake(column_rows=[], role_rows=[])
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role="TRANSFORMER_PROD",
        plan_design=_design(),
    )
    assert any(d.kind == "missing_role" for d in result.drift)


def test_preflight_present_runtime_role_no_drift() -> None:
    fake = _FakeSnowflake(
        column_rows=[],
        role_rows=[{"name": "TRANSFORMER_PROD"}],
    )
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role="TRANSFORMER_PROD",
        plan_design=_design(),
    )
    assert not any(d.kind == "missing_role" for d in result.drift)


# ---------------------------------------------------------------------------
# expected_destinations_from_build
# ---------------------------------------------------------------------------


def test_expected_destinations_from_design() -> None:
    out = expected_destinations_from_build(_build(), _design())
    assert len(out) == 1
    db, schema, table, cols = out[0]
    assert (db, schema, table) == ("ANALYTICS", "RAW", "IOWA")
    assert ("ID", "NUMBER") in cols
    assert ("STORE", "VARCHAR(50)") in cols


def test_expected_destinations_returns_empty_on_no_design() -> None:
    out = expected_destinations_from_build(_build(), None)
    assert out == []


def test_expected_destinations_prefers_manifest_destinations() -> None:
    """If `Build.manifest_json` carries `destinations`, use that.

    Forward-compatible with a future schema that stores destinations
    on the build directly. P1-08 reads only from the plan design, but
    the helper is wired for the upgrade.
    """
    manifest = {
        "files": [],
        "destinations": [
            {
                "database": "FROM_MANIFEST",
                "schema": "S",
                "table": "T",
                "columns": [{"name": "x", "type": "INT"}],
            }
        ],
    }
    build = Build(
        id="build_2",
        pipeline_name="iowa",
        plan_id="plan_1",
        target="dev",
        manifest_json=manifest,
    )
    out = expected_destinations_from_build(build, _design())
    assert len(out) == 1
    assert out[0][0] == "FROM_MANIFEST"


def test_preflight_drift_dataclass_fields() -> None:
    drift = PreflightDrift(kind="missing_column", detail="x")
    assert drift.kind == "missing_column"
    assert drift.detail == "x"


# ---------------------------------------------------------------------------
# Identifier validation (security: agent-emitted plan JSON)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, bad",
    [
        ("database", 'PROD".X; DROP DATABASE OTHER; --'),
        ("schema", "RAW; SELECT 1"),
        ("table", "IOWA WITH SPACE"),
        ("database", "PROD'INJECT"),
        ("database", "1LEADING_DIGIT_BAD"),  # we tighten beyond Snowflake's
        ("schema", "has-dash"),
        ("table", "tab\nbreak"),
    ],
)
def test_preflight_refuses_unsafe_destination_identifier(
    field: str, bad: str
) -> None:
    """Malformed db/schema/table values surface as preflight drift, never SQL."""
    design: dict[str, Any] = {
        "destination": {
            "database": "ANALYTICS",
            "schema": "RAW",
            "table": "IOWA",
        },
        "columns": [{"name": "ID", "type": "NUMBER"}],
    }
    design["destination"][field] = bad
    fake = _FakeSnowflake(column_rows=[])
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        runtime_role=None,
        plan_design=design,
    )
    assert any(d.kind == "invalid_identifier" for d in result.drift)
    # Critically: we must NOT have queried INFORMATION_SCHEMA with the
    # bad value interpolated.
    assert not any(bad in sql for sql, _ in fake.queries)


def test_expected_destinations_from_manifest_validates_identifiers() -> None:
    """The manifest extractor validates at the boundary too."""
    manifest = {
        "files": [],
        "destinations": [
            {
                "database": 'BAD"',
                "schema": "S",
                "table": "T",
                "columns": [{"name": "x", "type": "INT"}],
            }
        ],
    }
    build = Build(
        id="build_x",
        pipeline_name="iowa",
        plan_id="plan_1",
        target="dev",
        manifest_json=manifest,
    )
    with pytest.raises(InvalidSnowflakeIdentifierError):
        expected_destinations_from_build(build, None)


def test_preflight_runtime_role_name_validated() -> None:
    """An invalid runtime role name surfaces as `invalid_identifier` drift
    rather than being interpolated into ``SHOW ROLES LIKE '<value>'``."""
    fake = _FakeSnowflake(
        column_rows=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER", "IS_NULLABLE": "NO"},
        ],
    )
    result = run_preflight(
        deploy_connection=fake,  # type: ignore[arg-type]
        plan_design={
            "destination": {
                "database": "RAW",
                "schema": "PUBLIC",
                "table": "T",
                "columns": [{"name": "id", "type": "INT"}],
            }
        },
        runtime_role="bad'; DROP ROLE foo--",
    )

    drift_kinds = {d.kind for d in result.drift}
    assert "invalid_identifier" in drift_kinds
    # Pre-flight stops before issuing SHOW ROLES — the invalid value is
    # never threaded into a SQL string.
    show_roles_calls = [sql for sql, _ in fake.queries if "SHOW ROLES" in sql]
    assert show_roles_calls == []
