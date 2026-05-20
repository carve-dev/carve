"""Unit tests for ``carve.core.deploy.verifier``."""

from __future__ import annotations

from typing import Any

import pytest

from carve.core.deploy.verifier import run_verify
from carve.core.state.models import Build


class _FakeSnowflake:
    """Records SQL; canned column / grants / smoke-test responses."""

    def __init__(
        self,
        *,
        columns: list[dict[str, Any]] | None = None,
        grants: list[dict[str, Any]] | None = None,
        smoke_error: Exception | None = None,
    ) -> None:
        self._columns = columns
        self._grants = grants or []
        self._smoke_error = smoke_error
        self.queries: list[str] = []

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        del params, limit
        self.queries.append(sql)
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return list(self._columns or [])
        if "SHOW GRANTS" in sql:
            return list(self._grants)
        if "SELECT 1" in sql:
            if self._smoke_error is not None:
                raise self._smoke_error
            return [{"SMOKE": 1}]
        return []


def _build() -> Build:
    return Build(
        id="b1",
        pipeline_name="iowa",
        plan_id="p1",
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


_FULL_GRANTS = [
    {"grantee_name": "TRANSFORMER", "privilege": p}
    for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
]


def test_verify_passes_on_correct_state() -> None:
    fake = _FakeSnowflake(
        columns=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
            {"COLUMN_NAME": "STORE", "DATA_TYPE": "VARCHAR"},
        ],
        grants=_FULL_GRANTS,
    )
    result = run_verify(
        runtime_connection=fake,  # type: ignore[arg-type]
        build=_build(),
        plan_design=_design(),
        runtime_role="TRANSFORMER",
    )
    assert result.ok, result.failures


def test_verify_detects_column_drift() -> None:
    fake = _FakeSnowflake(
        columns=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
            # STORE missing
        ],
        grants=_FULL_GRANTS,
    )
    result = run_verify(
        runtime_connection=fake,  # type: ignore[arg-type]
        build=_build(),
        plan_design=_design(),
        runtime_role="TRANSFORMER",
    )
    assert not result.ok
    assert any("STORE" in f for f in result.failures)


def test_verify_runtime_role_grants_check() -> None:
    fake = _FakeSnowflake(
        columns=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
            {"COLUMN_NAME": "STORE", "DATA_TYPE": "VARCHAR"},
        ],
        grants=[
            {"grantee_name": "TRANSFORMER", "privilege": "SELECT"},
            # INSERT, UPDATE, DELETE missing
        ],
    )
    result = run_verify(
        runtime_connection=fake,  # type: ignore[arg-type]
        build=_build(),
        plan_design=_design(),
        runtime_role="TRANSFORMER",
    )
    assert not result.ok
    assert any("INSERT" in f for f in result.failures)


def test_verify_smoke_test_failure_surfaces() -> None:
    fake = _FakeSnowflake(
        columns=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
            {"COLUMN_NAME": "STORE", "DATA_TYPE": "VARCHAR"},
        ],
        grants=_FULL_GRANTS,
        smoke_error=RuntimeError("warehouse suspended"),
    )
    result = run_verify(
        runtime_connection=fake,  # type: ignore[arg-type]
        build=_build(),
        plan_design=_design(),
        runtime_role="TRANSFORMER",
        smoke_test=True,
    )
    assert not result.ok
    assert any("smoke" in f for f in result.failures)


def test_verify_no_smoke_test_flag_skips_smoke_query() -> None:
    fake = _FakeSnowflake(
        columns=[
            {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
            {"COLUMN_NAME": "STORE", "DATA_TYPE": "VARCHAR"},
        ],
        grants=_FULL_GRANTS,
        smoke_error=RuntimeError("network down"),  # would fail smoke
    )
    result = run_verify(
        runtime_connection=fake,  # type: ignore[arg-type]
        build=_build(),
        plan_design=_design(),
        runtime_role="TRANSFORMER",
        smoke_test=False,
    )
    assert result.ok
    assert not any("SELECT 1" in q for q in fake.queries)


def test_verify_destination_missing() -> None:
    fake = _FakeSnowflake(columns=[], grants=_FULL_GRANTS)
    result = run_verify(
        runtime_connection=fake,  # type: ignore[arg-type]
        build=_build(),
        plan_design=_design(),
        runtime_role="TRANSFORMER",
    )
    assert not result.ok
    assert any("does not exist" in f for f in result.failures)


def test_verify_no_design_no_destinations() -> None:
    fake = _FakeSnowflake()
    result = run_verify(
        runtime_connection=fake,  # type: ignore[arg-type]
        build=_build(),
        plan_design=None,
        runtime_role=None,
    )
    assert not result.ok


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
        ("table", "has-dash"),
        ("database", "1LEADING_DIGIT_BAD"),
    ],
)
def test_verifier_refuses_unsafe_destination_identifier(
    field: str, bad: str
) -> None:
    """Malformed db/schema/table values surface as a failure, never reach SQL."""
    design: dict[str, Any] = {
        "destination": {
            "database": "ANALYTICS",
            "schema": "RAW",
            "table": "IOWA",
        },
        "columns": [{"name": "ID", "type": "NUMBER"}],
    }
    design["destination"][field] = bad
    fake = _FakeSnowflake(columns=[], grants=[])
    result = run_verify(
        runtime_connection=fake,  # type: ignore[arg-type]
        build=_build(),
        plan_design=design,
        runtime_role="R",
    )
    assert not result.ok
    # The bad value must NOT have been interpolated into any query.
    assert not any(bad in q for q in fake.queries)
