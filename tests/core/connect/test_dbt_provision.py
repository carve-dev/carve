"""The connect provision loop — offline, with injected install/validate seams.

Mirrors the dbt-execution injected-engine test discipline: a fake installer that
touches a fake engine binary and a fake ``dbt --version`` validator, so the
loop's resolve → install → validate → pin runs with no real dbt and no network.

Covers the slice's two load-bearing invariants explicitly:

* **Fail-closed ordering** — a validate that raises leaves ``carve.toml``
  byte-identical (no partial pin written).
* **Two-check idempotence** — a no-op only when pinned AND present-on-disk; a
  pin-without-an-install re-installs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.config.schema import ComponentConfig, ComponentMode, ComponentType
from carve.core.connect.dbt_provision import provision_dbt_engine
from carve.core.connect.installer import engine_executable, engine_install_dir
from carve.core.connect.result import (
    InstalledEngine,
    ProvisionOutcome,
    ValidationFailed,
)
from carve.core.dbt_execution.engine import ENGINE_DBT_CORE, EnginePin


def _write_config(tmp_path: Path, *, component_name: str = "analytics", extra: str = "") -> Path:
    """A carve.toml with one dbt component block (so pin_engine can write into it)."""
    config_path = tmp_path / "carve.toml"
    config_path.write_text(
        '[project]\nname = "t"\n\n'
        f"[components.{component_name}]\n"
        'type = "dbt"\n'
        'mode = "same-repo"\n'
        f"{extra}",
        encoding="utf-8",
    )
    return config_path


class _FakeInstaller:
    """Records calls; materializes the fake engine binary at the pin's venv path."""

    def __init__(self, install_root: Path) -> None:
        self.install_root = install_root
        self.calls: list[tuple[str, str]] = []

    def __call__(self, pin: EnginePin, dialect: str) -> InstalledEngine:
        self.calls.append((pin.dbt_engine, dialect))
        venv_dir = engine_install_dir(pin, install_root=self.install_root)
        exe = engine_executable(venv_dir)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
        return InstalledEngine(engine=pin.dbt_engine, version=pin.dbt_version, executable=exe)


class _RecordingValidator:
    def __init__(self) -> None:
        self.calls: list[InstalledEngine] = []

    def __call__(self, engine: InstalledEngine) -> None:
        self.calls.append(engine)


def _failing_validator(engine: InstalledEngine) -> None:
    raise ValidationFailed("simulated bad engine")


# --- lazy provision (the first-use path) ------------------------------------


def test_lazy_provision_resolves_installs_validates_and_pins(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    install_root = tmp_path / "engines"
    component = ComponentConfig(
        type=ComponentType.DBT, mode=ComponentMode.SAME_REPO, dbt_env="bundled"
    )
    installer = _FakeInstaller(install_root)
    validator = _RecordingValidator()

    result = provision_dbt_engine(
        component,
        component_name="analytics",
        dialect="duckdb",
        config_path=config_path,
        install_root=install_root,
        install=installer,
        validate=validator,
    )

    assert result.outcome is ProvisionOutcome.PROVISIONED
    assert result.pin == EnginePin(dbt_engine=ENGINE_DBT_CORE, dbt_version="1.8.0")
    assert result.validated is True
    # Installed + validated (in that order — validate saw the installed engine).
    assert installer.calls == [(ENGINE_DBT_CORE, "duckdb")]
    assert len(validator.calls) == 1
    # The pin was written into carve.toml.
    text = config_path.read_text(encoding="utf-8")
    assert 'dbt_engine = "dbt-core"' in text
    assert 'dbt_version = "1.8.0"' in text


def test_second_run_is_noop_when_pinned_and_present(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    install_root = tmp_path / "engines"
    installer = _FakeInstaller(install_root)
    validator = _RecordingValidator()

    # First run provisions (writes the pin + materializes the fake engine).
    provision_dbt_engine(
        ComponentConfig(type=ComponentType.DBT, mode=ComponentMode.SAME_REPO, dbt_env="bundled"),
        component_name="analytics",
        dialect="duckdb",
        config_path=config_path,
        install_root=install_root,
        install=installer,
        validate=validator,
    )

    # Second run: the component is now pinned AND the engine is present on disk.
    pinned = ComponentConfig(
        type=ComponentType.DBT,
        mode=ComponentMode.SAME_REPO,
        dbt_env="bundled",
        dbt_engine="dbt-core",
        dbt_version="1.8.0",
    )
    installer.calls.clear()
    validator.calls.clear()
    result = provision_dbt_engine(
        pinned,
        component_name="analytics",
        dialect="duckdb",
        config_path=config_path,
        install_root=install_root,
        install=installer,
        validate=validator,
    )

    assert result.outcome is ProvisionOutcome.NOOP
    # Provisions nothing: neither installer nor validator was called.
    assert installer.calls == []
    assert validator.calls == []


# --- idempotence is TWO checks ----------------------------------------------


def test_pinned_but_missing_reinstalls(tmp_path: Path) -> None:
    """A pin alone is not 'done' — a wiped venv must re-install."""
    config_path = _write_config(tmp_path)
    install_root = tmp_path / "engines"
    installer = _FakeInstaller(install_root)
    validator = _RecordingValidator()

    # Component is pinned, but NO engine was ever installed under install_root.
    pinned = ComponentConfig(
        type=ComponentType.DBT,
        mode=ComponentMode.SAME_REPO,
        dbt_env="bundled",
        dbt_engine="dbt-core",
        dbt_version="1.8.0",
    )
    result = provision_dbt_engine(
        pinned,
        component_name="analytics",
        dialect="duckdb",
        config_path=config_path,
        install_root=install_root,
        install=installer,
        validate=validator,
    )

    # Pinned-but-missing → it RE-INSTALLS (and re-validates).
    assert result.outcome is ProvisionOutcome.PROVISIONED
    assert installer.calls == [(ENGINE_DBT_CORE, "duckdb")]
    assert len(validator.calls) == 1


# --- managed backend: no install --------------------------------------------


@pytest.mark.parametrize("backend", ["snowflake-native", "dbt-cloud", "remote"])
def test_managed_backend_wires_no_install(tmp_path: Path, backend: str) -> None:
    config_path = _write_config(tmp_path)
    install_root = tmp_path / "engines"
    installer = _FakeInstaller(install_root)
    validator = _RecordingValidator()
    before = config_path.read_text(encoding="utf-8")

    component = ComponentConfig(
        type=ComponentType.DBT, mode=ComponentMode.SAME_REPO, dbt_backend=backend
    )
    result = provision_dbt_engine(
        component,
        component_name="analytics",
        dialect="snowflake",
        config_path=config_path,
        install_root=install_root,
        install=installer,
        validate=validator,
    )

    assert result.outcome is ProvisionOutcome.MANAGED
    assert result.pin is None
    # The installer is NEVER called for a managed backend; no pin written.
    assert installer.calls == []
    assert config_path.read_text(encoding="utf-8") == before


def test_external_dbt_installs_nothing(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, extra='dbt_env = "external"\ndbt_path = "dbt"\n')
    install_root = tmp_path / "engines"
    installer = _FakeInstaller(install_root)
    validator = _RecordingValidator()
    before = config_path.read_text(encoding="utf-8")

    component = ComponentConfig(
        type=ComponentType.DBT,
        mode=ComponentMode.SAME_REPO,
        dbt_env="external",
        dbt_path="dbt",
    )
    result = provision_dbt_engine(
        component,
        component_name="analytics",
        dialect="duckdb",
        config_path=config_path,
        install_root=install_root,
        install=installer,
        validate=validator,
    )

    assert result.outcome is ProvisionOutcome.EXTERNAL
    assert installer.calls == []
    assert config_path.read_text(encoding="utf-8") == before


# --- fail-closed: a failed validate writes NO config ------------------------


def test_failed_validate_leaves_config_byte_identical(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    install_root = tmp_path / "engines"
    installer = _FakeInstaller(install_root)
    before = config_path.read_text(encoding="utf-8")

    component = ComponentConfig(
        type=ComponentType.DBT, mode=ComponentMode.SAME_REPO, dbt_env="bundled"
    )

    with pytest.raises(ValidationFailed):
        provision_dbt_engine(
            component,
            component_name="analytics",
            dialect="duckdb",
            config_path=config_path,
            install_root=install_root,
            install=installer,
            validate=_failing_validator,
        )

    # The install ran, but pin_engine was UNREACHABLE — config is byte-identical.
    assert installer.calls == [(ENGINE_DBT_CORE, "duckdb")]
    assert config_path.read_text(encoding="utf-8") == before
    assert "dbt_engine" not in config_path.read_text(encoding="utf-8")
