"""A stubbed backend and the local backend both satisfy the DbtBackend Protocol.

The "Unit (dispatch)" test for this slice: a caller that holds a ``DbtBackend``
dispatches to ``.run(...)`` and gets a uniform ``DbtRunResult`` from either a
stub or the real local backend — never branching on which it holds.
"""

from __future__ import annotations

from pathlib import Path

from carve.core.dbt_execution.backend import DbtBackend, DbtCommand
from carve.core.dbt_execution.local import LocalDbtBackend
from carve.core.dbt_execution.result import STATUS_SUCCESS, DbtRunResult, PerModelResult


class _StubBackend:
    """A minimal in-memory DbtBackend — no subprocess, fixed result."""

    def run(self, command: DbtCommand) -> DbtRunResult:
        return DbtRunResult(
            status=STATUS_SUCCESS,
            per_model=[
                PerModelResult(
                    unique_id="model.demo.stg",
                    name="stg",
                    status="success",
                )
            ],
            logs=f"stub ran {command.command}",
        )


def test_stub_backend_satisfies_protocol() -> None:
    assert isinstance(_StubBackend(), DbtBackend)


def test_local_backend_satisfies_protocol(tmp_path: Path) -> None:
    backend = LocalDbtBackend(dbt_executable="dbt", project_dir=tmp_path)
    assert isinstance(backend, DbtBackend)


def test_caller_dispatches_uniformly_over_any_backend() -> None:
    # A caller typed against the Protocol gets the same result shape from either.
    def run_through(backend: DbtBackend) -> DbtRunResult:
        return backend.run(DbtCommand(command="build"))

    result = run_through(_StubBackend())
    assert isinstance(result, DbtRunResult)
    assert result.status == STATUS_SUCCESS
    assert result.per_model[0].name == "stg"
