"""``carve connect`` CLI tests — offline (real loop, faked install/validate).

The command calls the real :func:`provision_dbt_engine` (the orchestrator-trigger
seam) without injecting fakes, so the offline injection happens one level down:
the loop's module-level ``install_engine`` and ``_default_validate`` are
monkeypatched to a fake that touches a fake engine binary and a no-op validate.
No real dbt, no PyPI, no subprocess.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import carve.core.connect.dbt_provision as provision_module
from carve.cli.main import app
from carve.core.connect.installer import engine_executable, engine_install_dir
from carve.core.connect.result import InstalledEngine, ValidationFailed
from carve.core.dbt_execution.engine import EnginePin

runner = CliRunner()


def _project(tmp_path: Path, *, component: str = "analytics", extra: str = "") -> Path:
    """A carve.toml with one dbt component block (no engine pin yet)."""
    (tmp_path / "carve.toml").write_text(
        '[project]\nname = "t"\n\n'
        f"[components.{component}]\n"
        'type = "dbt"\n'
        'mode = "same-repo"\n'
        f"{extra}",
        encoding="utf-8",
    )
    return tmp_path


def _fake_install_engine(
    pin: EnginePin, dialect: str, *, install_root: Path, **_kwargs: object
) -> InstalledEngine:
    """Stand-in for the real installer: touch a fake engine binary, no PyPI."""
    exe = engine_executable(engine_install_dir(pin, install_root=install_root))
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    return InstalledEngine(engine=pin.dbt_engine, version=pin.dbt_version, executable=exe)


@pytest.fixture
def _offline_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the loop's install + validate so the command runs fully offline."""
    monkeypatch.setattr(provision_module, "install_engine", _fake_install_engine)
    monkeypatch.setattr(provision_module, "_default_validate", lambda _engine: None)


def test_connect_provisions_and_reports_pin(tmp_path: Path, _offline_engine: None) -> None:
    project = _project(tmp_path)
    result = runner.invoke(app, ["connect", "analytics", "--project-dir", str(project)])

    assert result.exit_code == 0, result.stdout
    assert "Provisioned" in result.stdout
    assert "dbt-core" in result.stdout
    # The pin landed in carve.toml.
    doc = tomllib.loads((project / "carve.toml").read_text(encoding="utf-8"))
    assert doc["components"]["analytics"]["dbt_engine"] == "dbt-core"
    assert doc["components"]["analytics"]["dbt_version"] == "1.8.0"


def test_connect_defaults_to_detected_dbt_component(tmp_path: Path, _offline_engine: None) -> None:
    project = _project(tmp_path)
    # No component arg → the single dbt block is provisioned.
    result = runner.invoke(app, ["connect", "--project-dir", str(project)])

    assert result.exit_code == 0, result.stdout
    doc = tomllib.loads((project / "carve.toml").read_text(encoding="utf-8"))
    assert doc["components"]["analytics"]["dbt_engine"] == "dbt-core"


def test_connect_managed_backend_installs_nothing(tmp_path: Path, _offline_engine: None) -> None:
    project = _project(tmp_path, extra='dbt_backend = "snowflake-native"\n')
    before = (project / "carve.toml").read_text(encoding="utf-8")

    result = runner.invoke(app, ["connect", "analytics", "--project-dir", str(project)])

    assert result.exit_code == 0, result.stdout
    assert "managed backend" in result.stdout
    # No pin written.
    assert (project / "carve.toml").read_text(encoding="utf-8") == before


def test_connect_failed_validate_exits_nonzero_and_writes_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    before = (project / "carve.toml").read_text(encoding="utf-8")

    monkeypatch.setattr(provision_module, "install_engine", _fake_install_engine)

    def _bad_validate(_engine: object) -> None:
        raise ValidationFailed("simulated bad engine")

    monkeypatch.setattr(provision_module, "_default_validate", _bad_validate)

    result = runner.invoke(app, ["connect", "analytics", "--project-dir", str(project)])

    assert result.exit_code == 1
    assert "no config written" in result.stdout
    # Fail-closed: carve.toml is byte-identical.
    assert (project / "carve.toml").read_text(encoding="utf-8") == before


def test_connect_unknown_component_exits_2(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = runner.invoke(app, ["connect", "nope", "--project-dir", str(project)])
    assert result.exit_code == 2
    assert "nope" in result.stdout


def test_connect_dlt_component_rejected(tmp_path: Path) -> None:
    (tmp_path / "carve.toml").write_text(
        '[project]\nname = "t"\n\n[components.stripe]\ntype = "dlt"\nmode = "same-repo"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["connect", "stripe", "--project-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "not dbt" in result.stdout
