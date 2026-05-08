"""Unit tests for ``carve.core.deploy.recovery``.

The recovery seam is small enough that the unit tests just verify
the Protocol shape and `NullRecoveryHandler`'s "always exhausted"
behavior. Real recovery logic lands in P1-09.
"""

from __future__ import annotations

from pathlib import Path

from carve.core.deploy.recovery import (
    NullRecoveryHandler,
    RecoveryContext,
    RecoveryResult,
    RecoveryStage,
)


def _ctx(stage: RecoveryStage = RecoveryStage.PREFLIGHT) -> RecoveryContext:
    return RecoveryContext(
        stage=stage,
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        project_dir=Path("/tmp"),
        ddl_path=Path("/tmp/iowa.sql"),
        error="something went wrong",
    )


def test_null_handler_returns_unrecoverable() -> None:
    handler = NullRecoveryHandler()
    result = handler.attempt(_ctx())
    assert result.success is False
    assert "not enabled" in result.diagnosis


def test_null_handler_consistent_across_stages() -> None:
    handler = NullRecoveryHandler()
    for stage in RecoveryStage:
        result = handler.attempt(_ctx(stage))
        assert result.success is False


def test_recovery_result_is_immutable_dataclass() -> None:
    result = RecoveryResult(success=True, diagnosis="fixed it")
    # Frozen — assigning would raise.
    import dataclasses

    assert dataclasses.is_dataclass(result)


def test_recovery_context_carries_stage_specific_extras() -> None:
    ctx = RecoveryContext(
        stage=RecoveryStage.DDL_APPLY,
        pipeline_name="x",
        source_target="dev",
        dest_target="prod",
        project_dir=Path("/p"),
        ddl_path=Path("/p/x.sql"),
        error="boom",
        failing_statement_index=2,
        failing_sql="GRANT ...",
    )
    assert ctx.failing_statement_index == 2
    assert ctx.failing_sql == "GRANT ..."
