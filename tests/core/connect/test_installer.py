"""Offline tests for the bundled-engine installer.

No PyPI, no real dbt: the ``runner`` seam is a fake that records the argv it's
handed and touches a fake ``bin/dbt`` so the install path runs end-to-end. The
fusion-deferred fence is asserted (never a silent false-success), and the
optional real-dbt-against-DuckDB install is ``importorskip``-gated.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from carve.core.connect.installer import (
    engine_executable,
    engine_install_dir,
    install_engine,
)
from carve.core.connect.result import EngineInstallNotSupported, UnsafeEnginePath
from carve.core.dbt_execution.engine import ENGINE_DBT_CORE, ENGINE_FUSION, EnginePin


class _FakeRunner:
    """Records install argv; on the venv-create step touches a fake ``bin/dbt``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], cwd: Path) -> None:
        self.calls.append(argv)
        # Emulate `python -m venv <dir>` by materializing the fake binary the
        # installer will return, so the real install path's post-condition holds.
        if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "venv":
            venv_dir = Path(argv[3])
            bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
            bin_dir.mkdir(parents=True, exist_ok=True)
            exe = engine_executable(venv_dir)
            exe.write_text("#!/bin/sh\necho fake dbt\n", encoding="utf-8")
            exe.chmod(0o755)


def test_install_dbt_core_builds_pip_argv_and_returns_bin_dbt(tmp_path: Path) -> None:
    runner = _FakeRunner()
    pin = EnginePin(dbt_engine=ENGINE_DBT_CORE, dbt_version="1.8.0")

    installed = install_engine(
        pin, "duckdb", install_root=tmp_path, python_executable="python3", runner=runner
    )

    # Returns the venv's bin/dbt — exactly what LocalDbtBackend(dbt_executable=…) wants.
    expected = engine_executable(engine_install_dir(pin, install_root=tmp_path))
    assert installed.executable == expected
    assert installed.executable.is_file()
    assert installed.engine == ENGINE_DBT_CORE
    assert installed.version == "1.8.0"

    # Two steps: venv create, then a pinned pip install of dbt-core + the adapter.
    assert len(runner.calls) == 2
    venv_call, pip_call = runner.calls
    assert venv_call[1:3] == ["-m", "venv"]
    assert "install" in pip_call
    assert "dbt-core==1.8.0" in pip_call
    assert "dbt-duckdb==1.8.0" in pip_call
    # `--` terminates pip flag parsing before the version specs.
    assert "--" in pip_call
    assert pip_call.index("--") < pip_call.index("dbt-core==1.8.0")


def test_install_snowflake_dialect_picks_snowflake_adapter(tmp_path: Path) -> None:
    runner = _FakeRunner()
    pin = EnginePin(dbt_engine=ENGINE_DBT_CORE, dbt_version="1.8.0")

    install_engine(pin, "snowflake", install_root=tmp_path, runner=runner)

    _, pip_call = runner.calls
    assert "dbt-snowflake==1.8.0" in pip_call


def test_install_is_idempotent_when_binary_present(tmp_path: Path) -> None:
    runner = _FakeRunner()
    pin = EnginePin(dbt_engine=ENGINE_DBT_CORE, dbt_version="1.8.0")

    install_engine(pin, "duckdb", install_root=tmp_path, runner=runner)
    first_call_count = len(runner.calls)
    # Second install with the binary already present → no venv/pip work.
    install_engine(pin, "duckdb", install_root=tmp_path, runner=runner)
    assert len(runner.calls) == first_call_count


def test_install_fusion_raises_not_supported(tmp_path: Path) -> None:
    runner = _FakeRunner()
    pin = EnginePin(dbt_engine=ENGINE_FUSION, dbt_version="2.0.0")

    with pytest.raises(EngineInstallNotSupported):
        install_engine(pin, "snowflake", install_root=tmp_path, runner=runner)
    # The deferred fence never ran an install step.
    assert runner.calls == []


def test_engine_install_dir_confines_to_install_root(tmp_path: Path) -> None:
    """A valid version keys a dir INSIDE install_root."""
    pin = EnginePin(dbt_engine=ENGINE_DBT_CORE, dbt_version="1.8.0")
    d = engine_install_dir(pin, install_root=tmp_path)
    assert tmp_path.resolve() in d.resolve().parents


def test_engine_install_dir_rejects_traversal_version(tmp_path: Path) -> None:
    """A directly-constructed EnginePin with a traversal version can't escape root.

    The config-load validator is the primary guard; this is the defense-in-depth
    sink check, so it must hold even when EnginePin is built directly (bypassing
    ComponentConfig) — the dir is mkdir'd + exec'd, so an escape is code-exec.
    """
    pin = EnginePin(dbt_engine=ENGINE_DBT_CORE, dbt_version="../../../../tmp/evil")
    with pytest.raises(UnsafeEnginePath, match="escapes install_root"):
        engine_install_dir(pin, install_root=tmp_path)


def test_install_unknown_dialect_raises(tmp_path: Path) -> None:
    runner = _FakeRunner()
    pin = EnginePin(dbt_engine=ENGINE_DBT_CORE, dbt_version="1.8.0")

    with pytest.raises(EngineInstallNotSupported):
        install_engine(pin, "oracle", install_root=tmp_path, runner=runner)


# --- optional real-dbt install (availability-gated; excluded from the gate) ---


def test_real_dbt_core_install_to_duckdb(tmp_path: Path) -> None:
    """OPTIONAL: a real `pip install dbt-core dbt-duckdb` into a managed venv.

    Skipped unless ``dbt`` + ``dbt.adapters.duckdb`` import (the ``dbt-test``
    optional-dependencies group). Mirrors ``test_local_backend_real_dbt.py`` —
    not part of the default offline gate.
    """
    pytest.importorskip("dbt")
    pytest.importorskip("dbt.adapters.duckdb")

    pin = EnginePin(dbt_engine=ENGINE_DBT_CORE, dbt_version="1.8.0")
    # Use the running interpreter; real default runner hits the network. This is
    # the gated path, so a slow cold install is acceptable here only.
    installed = install_engine(pin, "duckdb", install_root=tmp_path)
    assert installed.executable.is_file()
