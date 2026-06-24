"""OPTIONAL real-dbt-against-DuckDB integration test (availability-gated).

Skipped unless the heavy adapter stack (``dbt-core`` + ``dbt-duckdb``, the
``dbt-test`` optional-dependencies group) is installed. Mirrors the dlt
precedent's real-DuckDB load: a tiny creds-free dbt project (one model) is built
through the real :class:`LocalDbtBackend`, and per-model status is parsed from
the real ``run_results.json`` the subprocess wrote.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("dbt")
pytest.importorskip("dbt.adapters.duckdb")

from carve.core.dbt_execution.backend import DbtCommand
from carve.core.dbt_execution.local import LocalDbtBackend
from carve.core.dbt_execution.result import STATUS_SUCCESS


def _dbt_executable() -> str:
    exe = shutil.which("dbt")
    if exe is None:  # pragma: no cover - env-dependent
        pytest.skip("dbt executable not on PATH")
    return exe


def test_real_dbt_build_to_duckdb_parses_clean(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    models = project / "models"
    models.mkdir(parents=True)

    (project / "dbt_project.yml").write_text(
        textwrap.dedent("""
        name: demo
        version: "1.0.0"
        profile: demo
        model-paths: ["models"]
        """),
        encoding="utf-8",
    )
    (models / "one.sql").write_text("select 1 as id\n", encoding="utf-8")

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(
        textwrap.dedent(f"""
        demo:
          target: dev
          outputs:
            dev:
              type: duckdb
              path: "{project / "demo.duckdb"}"
        """),
        encoding="utf-8",
    )

    backend = LocalDbtBackend(
        dbt_executable=_dbt_executable(),
        project_dir=project,
        env="external",
        profiles_dir=profiles,
    )
    result = backend.run(DbtCommand(command="build", target="dev"))

    assert result.status == STATUS_SUCCESS
    assert any(pm.name == "one" for pm in result.per_model)
