"""OPTIONAL real-dbt-against-DuckDB verify vertical (availability-gated).

The real arm of the author -> run -> verify -> self-correct vertical: author a
trivial model, build it green, break a ``ref``, confirm not-green, fix it, confirm
green — through the real :class:`LocalDbtBackend` + the structured bridge. Skipped
unless the heavy adapter stack (``dbt-core`` + ``dbt-duckdb``, the ``dbt-test``
optional-dependencies group) is installed. The always-on injected-backend half of
this vertical lives in ``test_verify_vertical.py`` and collects regardless of dbt
availability; this module is split out (mirroring ``test_local_backend_real_dbt``)
so its module-level ``importorskip`` never skips the injected tests.
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
from carve.core.dbt_execution.verify_bridge import dbt_run_result_to_check_result


def _dbt_executable() -> str:
    exe = shutil.which("dbt")
    if exe is None:  # pragma: no cover - env-dependent
        pytest.skip("dbt executable not on PATH")
    return exe


def _scaffold_project(tmp_path: Path) -> tuple[Path, Path]:
    """A creds-free dbt+DuckDB project with a base model. Returns (project, models)."""
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
    # The base model the mart will ref.
    (models / "base.sql").write_text("select 1 as id\n", encoding="utf-8")

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
    return project, models


def test_real_dbt_vertical_self_corrects_broken_ref_to_green(tmp_path: Path) -> None:
    project, models = _scaffold_project(tmp_path)
    profiles = tmp_path / "profiles"

    backend = LocalDbtBackend(
        dbt_executable=_dbt_executable(),
        project_dir=project,
        env="external",
        profiles_dir=profiles,
    )
    mart = models / "daily_revenue.sql"

    # 1) Author a correct mart that refs the base model -> build is green.
    mart.write_text("select id from {{ ref('base') }}\n", encoding="utf-8")
    green = dbt_run_result_to_check_result(backend.run(DbtCommand(command="build", target="dev")))
    assert green.passed is True, green.summary

    # 2) Break the ref -> the next build is NOT trusted as green.
    mart.write_text("select id from {{ ref('does_not_exist') }}\n", encoding="utf-8")
    broken = dbt_run_result_to_check_result(backend.run(DbtCommand(command="build", target="dev")))
    assert broken.passed is False

    # 3) Self-correct the ref -> build is green again (the author->run->verify loop).
    mart.write_text("select id from {{ ref('base') }}\n", encoding="utf-8")
    fixed = dbt_run_result_to_check_result(backend.run(DbtCommand(command="build", target="dev")))
    assert fixed.passed is True, fixed.summary
