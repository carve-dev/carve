"""The new ComponentConfig dbt-execution fields parse + validate."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from carve.core.config.schema import ComponentConfig, ComponentMode, ComponentType
from carve.core.dbt_execution.local import UnsupportedBackendError, build_backend


def _dbt_component(**overrides: object) -> ComponentConfig:
    base: dict[str, object] = {
        "type": ComponentType.DBT,
        "mode": ComponentMode.SAME_REPO,
    }
    base.update(overrides)
    return ComponentConfig(**base)


def test_local_backend_value_accepted() -> None:
    component = _dbt_component(dbt_backend="local", dbt_engine="dbt-core", dbt_version="1.8.0")
    assert component.dbt_backend == "local"
    assert component.dbt_engine == "dbt-core"


def test_unknown_backend_value_rejected() -> None:
    with pytest.raises(ValidationError):
        _dbt_component(dbt_backend="airflow")


def test_unknown_engine_value_rejected() -> None:
    with pytest.raises(ValidationError):
        _dbt_component(dbt_engine="spark")


def test_deferred_backend_loads_but_raises_at_construction(tmp_path) -> None:
    # A config naming a deferred backend LOADS fine...
    component = _dbt_component(dbt_backend="snowflake-native")
    assert component.dbt_backend == "snowflake-native"
    # ...but constructing it raises the "not yet implemented" error.
    with pytest.raises(UnsupportedBackendError):
        build_backend(
            dbt_backend=component.dbt_backend,
            dbt_executable="dbt",
            project_dir=tmp_path,
        )


def test_external_fields_require_external_env() -> None:
    # dbt_path / profiles_dir only valid when dbt_env == "external".
    with pytest.raises(ValidationError):
        _dbt_component(dbt_path="/usr/local/bin/dbt")
    with pytest.raises(ValidationError):
        _dbt_component(dbt_env="bundled", profiles_dir="profiles")

    ok = _dbt_component(dbt_env="external", dbt_path="bin/dbt", profiles_dir="profiles")
    assert ok.dbt_env == "external"
    assert ok.dbt_path == "bin/dbt"


def test_worker_label_accepted_and_stored() -> None:
    component = _dbt_component(worker_label="gpu-pool")
    assert component.worker_label == "gpu-pool"


def test_dbt_path_rejects_option_shaped_and_traversal() -> None:
    with pytest.raises(ValidationError):
        _dbt_component(dbt_env="external", dbt_path="--config=/etc/evil")
    with pytest.raises(ValidationError):
        _dbt_component(dbt_env="external", profiles_dir="../../etc")


def test_unset_dbt_fields_default_to_none() -> None:
    component = _dbt_component()
    assert component.dbt_backend is None
    assert component.dbt_engine is None
    assert component.dbt_env is None
    assert component.worker_label is None
