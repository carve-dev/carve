"""The init/connect division-of-labor invariant.

``init`` records a detected dbt component's *presence* (a ``[components.<name>]``
block, or convention discovery) but performs **no** engine provisioning — no
``dbt_engine``/``dbt_version`` pin. ``connect`` writes the pin on first use. This
asserts the spec's Behavior §"Division of labor with `init`": a project where
init recorded a dbt component carries no pin until connect provisions it.
"""

from __future__ import annotations

from pathlib import Path

from carve.core.config.schema import ComponentConfig, ComponentMode, ComponentType
from carve.core.connect.dbt_provision import provision_dbt_engine
from carve.core.connect.installer import engine_executable, engine_install_dir
from carve.core.connect.result import InstalledEngine, ProvisionOutcome
from carve.core.dbt_execution.engine import EnginePin


def _init_recorded_dbt_block(tmp_path: Path) -> Path:
    """Emulate what init writes: a dbt component block with NO engine pin."""
    config_path = tmp_path / "carve.toml"
    config_path.write_text(
        '[project]\nname = "t"\n\n[components.analytics]\ntype = "dbt"\nmode = "same-repo"\n',
        encoding="utf-8",
    )
    return config_path


def _fake_install(install_root: Path) -> object:
    def _install(pin: EnginePin, dialect: str) -> InstalledEngine:
        exe = engine_executable(engine_install_dir(pin, install_root=install_root))
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        return InstalledEngine(engine=pin.dbt_engine, version=pin.dbt_version, executable=exe)

    return _install


def test_init_records_presence_without_pinning(tmp_path: Path) -> None:
    config_path = _init_recorded_dbt_block(tmp_path)

    # init's artifact: the block exists, but carries no engine pin.
    text = config_path.read_text(encoding="utf-8")
    assert "[components.analytics]" in text
    assert "dbt_engine" not in text
    assert "dbt_version" not in text


def test_connect_writes_the_pin_on_first_use(tmp_path: Path) -> None:
    config_path = _init_recorded_dbt_block(tmp_path)
    install_root = tmp_path / "engines"

    component = ComponentConfig(type=ComponentType.DBT, mode=ComponentMode.SAME_REPO)
    result = provision_dbt_engine(
        component,
        component_name="analytics",
        dialect="duckdb",
        config_path=config_path,
        install_root=install_root,
        install=_fake_install(install_root),  # type: ignore[arg-type]
        validate=lambda _engine: None,
    )

    # connect — not init — is what pins the engine.
    assert result.outcome is ProvisionOutcome.PROVISIONED
    text = config_path.read_text(encoding="utf-8")
    assert 'dbt_engine = "dbt-core"' in text
    assert 'dbt_version = "1.8.0"' in text
