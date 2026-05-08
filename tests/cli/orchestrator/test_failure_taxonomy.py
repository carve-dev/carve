"""Pattern-match tests for the recovery classifier (P1-09).

The classifier is a pure function — no fixtures, no mocking. We assert
that the most-load-bearing strings each Pillar 1 failure mode produces
land in the right category. Calibrated against real Snowflake driver
strings, the Iowa-liquor smoke-test traceback, and the do-not-fix
families flagged in the spec.
"""

from __future__ import annotations

import pytest

from carve.cli.orchestrator.failure_taxonomy import (
    DO_NOT_AUTO_FIX,
    FailureCategory,
    classify_failure,
)


def test_classify_failure_dict_binding_pattern() -> None:
    """Iowa-liquor regression: dict-binding traceback → code_fix."""
    error = (
        'snowflake.connector.errors.ProgrammingError: 100096: Failed to bind '
        "parameter LOCATION: argument of type 'dict' is not JSON serializable"
    )
    assert classify_failure(error) == FailureCategory.CODE_FIX


def test_classify_failure_auth_pattern() -> None:
    """Authentication failure → auth (do-not-fix)."""
    error = "snowflake.connector.errors.DatabaseError: 250001: Authentication failed."
    assert classify_failure(error) == FailureCategory.AUTH
    assert FailureCategory.AUTH in DO_NOT_AUTO_FIX


def test_classify_failure_invalid_oauth_token_is_auth() -> None:
    error = "Invalid OAuth token: token has expired"
    assert classify_failure(error) == FailureCategory.AUTH


def test_classify_failure_permission_pattern() -> None:
    """Permission failure → permission (do-not-fix)."""
    error = (
        "snowflake.connector.errors.ProgrammingError: 003001 (42501): SQL "
        "access control error: Insufficient privileges to operate on table 'IOWA'"
    )
    assert classify_failure(error) == FailureCategory.PERMISSION
    assert FailureCategory.PERMISSION in DO_NOT_AUTO_FIX


def test_classify_failure_resource_exhaustion_warehouse_suspended() -> None:
    error = "Warehouse 'COMPUTE_WH' is suspended"
    assert classify_failure(error) == FailureCategory.RESOURCE_EXHAUSTION
    assert FailureCategory.RESOURCE_EXHAUSTION in DO_NOT_AUTO_FIX


def test_classify_failure_user_cancel_keyboardinterrupt() -> None:
    error = "KeyboardInterrupt"
    assert classify_failure(error) == FailureCategory.USER_CANCEL
    assert FailureCategory.USER_CANCEL in DO_NOT_AUTO_FIX


def test_classify_failure_out_of_scope() -> None:
    error = "this task is out of scope for the extract-load agent"
    assert classify_failure(error) == FailureCategory.OUT_OF_SCOPE
    assert FailureCategory.OUT_OF_SCOPE in DO_NOT_AUTO_FIX


def test_classify_failure_empty_or_none_defaults_to_code_fix() -> None:
    assert classify_failure(None) == FailureCategory.CODE_FIX
    assert classify_failure("") == FailureCategory.CODE_FIX


def test_classify_failure_unknown_falls_through_to_code_fix() -> None:
    error = "ProgrammingError: invalid identifier 'BADCOL'"
    assert classify_failure(error) == FailureCategory.CODE_FIX


@pytest.mark.parametrize(
    "error",
    [
        "Authentication failed: incorrect username or password",
        "401 Unauthorized",
        "Signature verification failed",
    ],
)
def test_classify_failure_auth_variants(error: str) -> None:
    assert classify_failure(error) == FailureCategory.AUTH


@pytest.mark.parametrize(
    "error",
    [
        "Insufficient privileges to operate on schema RAW",
        "SQL access control error: object does not exist",
        "403 Forbidden",
        "User does not have privileges on warehouse COMPUTE_WH",
    ],
)
def test_classify_failure_permission_variants(error: str) -> None:
    assert classify_failure(error) == FailureCategory.PERMISSION


@pytest.mark.parametrize(
    "error",
    [
        "Network unreachable",
        "Connection refused",
        "Quota exceeded",
        "503 service unavailable",
    ],
)
def test_classify_failure_resource_variants(error: str) -> None:
    assert classify_failure(error) == FailureCategory.RESOURCE_EXHAUSTION
