"""CLI exit-code mapping for `carve build` drift / expiry / no-op paths.

The orchestrator-level behaviour (drift raised before the force gate,
expiry rejection, idempotent no-op) is covered under
``tests/cli/orchestrator``. This module pins the thin `build.py`
mapping: ConfigDriftError -> exit 3 with a re-plan message,
PlanExpiredError -> exit 2, and an idempotent no-op artifact -> exit 0.

The heavy collaborators (`load_config`, the Postgres engine, `build_plan`)
are monkeypatched so the test exercises only the typer command's
exception-to-exit-code translation, not the agent or the database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from typer.testing import CliRunner

import carve.cli.commands.build as build_cmd
from carve.cli.main import app
from carve.cli.orchestrator.builder import (
    BuildArtifact,
    ConfigDriftError,
    PlanExpiredError,
)

runner = CliRunner()


def _patch_heavy_collaborators(monkeypatch: Any) -> None:
    """Stub config load + DB setup + destination confirm so only the
    command's exit-code mapping runs."""
    monkeypatch.setattr(build_cmd, "load_config", lambda project_dir: object())
    monkeypatch.setattr(
        build_cmd, "create_engine_from_config", lambda config, project_dir: _Engine()
    )
    monkeypatch.setattr(build_cmd, "initialize_database", lambda engine: None)
    monkeypatch.setattr(build_cmd, "create_session_factory", lambda engine: object())
    monkeypatch.setattr(build_cmd, "Repository", lambda session_factory: object())
    # Skip the interactive destination prompt entirely.
    monkeypatch.setattr(
        build_cmd,
        "_confirm_or_override_destination",
        lambda **kwargs: None,
    )


class _Engine:
    def dispose(self) -> None:
        return None


def test_drift_maps_to_exit_3(monkeypatch: Any) -> None:
    """ConfigDriftError -> exit 3 with a re-plan-against-current-config message."""
    _patch_heavy_collaborators(monkeypatch)

    def _raise(**kwargs: Any) -> BuildArtifact:
        raise ConfigDriftError("plan_x", plan_hash="aaaa", current_hash="bbbb")

    monkeypatch.setattr(build_cmd, "build_plan", _raise)

    result = runner.invoke(app, ["build", "plan_x", "--yes"])
    assert result.exit_code == 3
    assert "drift" in result.output.lower()
    assert "re-plan" in result.output.lower()


def test_drift_exit_3_even_with_force(monkeypatch: Any) -> None:
    """`--force` does not change the drift exit code — still 3."""
    _patch_heavy_collaborators(monkeypatch)

    def _raise(**kwargs: Any) -> BuildArtifact:
        raise ConfigDriftError("plan_x", plan_hash="aaaa", current_hash="bbbb")

    monkeypatch.setattr(build_cmd, "build_plan", _raise)

    result = runner.invoke(app, ["build", "plan_x", "--yes", "--force"])
    assert result.exit_code == 3


def test_expiry_maps_to_exit_2(monkeypatch: Any) -> None:
    """PlanExpiredError -> the generic plan-state exit code 2 (not 3)."""
    _patch_heavy_collaborators(monkeypatch)

    def _raise(**kwargs: Any) -> BuildArtifact:
        raise PlanExpiredError("plan_x", expires_at=datetime(2020, 1, 1, tzinfo=UTC))

    monkeypatch.setattr(build_cmd, "build_plan", _raise)

    result = runner.invoke(app, ["build", "plan_x", "--yes"])
    assert result.exit_code == 2
    assert "expired" in result.output.lower()


def test_noop_artifact_maps_to_exit_0(monkeypatch: Any) -> None:
    """An idempotent no-op artifact (empty run_id) -> exit 0 with a no-op note."""
    _patch_heavy_collaborators(monkeypatch)

    artifact = BuildArtifact(
        plan_id="plan_x",
        pipeline_name="csv_ingest",
        pipeline_dir="el/csv_ingest",
        target="dev",
        files_written=["el/csv_ingest/main.py"],
        summary="reused build (no-op).",
        run_id="",  # the no-op sentinel
        success=True,
        build_id="build_existing",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
    )
    monkeypatch.setattr(build_cmd, "build_plan", lambda **kwargs: artifact)

    result = runner.invoke(app, ["build", "plan_x", "--yes"])
    assert result.exit_code == 0
    assert "nothing to do" in result.output.lower()
    assert "no-op" in result.output.lower()
