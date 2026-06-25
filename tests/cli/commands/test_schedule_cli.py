"""``carve schedule reseed <pipeline>`` — the deferred code→data re-seed stub.

``reseed`` re-applies a pipeline's ``[seed_schedule]`` block onto the live
``schedules`` row, but that table is the Increment-4 runtime's and does not exist
yet — so the command exits non-zero with a clear "not available yet" message
rather than silently no-opping.
"""

from __future__ import annotations

from typer.testing import CliRunner

from carve.cli.main import app

runner = CliRunner()


def test_reseed_is_deferred_exits_one_with_message() -> None:
    result = runner.invoke(app, ["schedule", "reseed", "daily"])
    assert result.exit_code == 1
    assert "not available yet" in result.stdout
    assert "Increment 4" in result.stdout
    assert "daily" in result.stdout
