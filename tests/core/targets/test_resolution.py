"""Tests for ``carve.core.targets.resolution``."""

from __future__ import annotations

import pytest

from carve.core.config import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    SnowflakeConnection,
)
from carve.core.targets.resolution import (
    TargetResolutionError,
    require_target,
    resolve_active_target,
)


def _make_config(default: str = "dev") -> Config:
    return Config(
        project=ProjectConfig(name="t", default_target=default),
        models=ModelsConfig(),
        connections=ConnectionsConfig(),
    )


def test_resolution_cli_flag_wins() -> None:
    """``--target X`` beats env var, default_target, and fallback."""
    config = _make_config(default="dev")
    env = {"CARVE_TARGET": "from_env"}
    assert resolve_active_target("staging", config, env=env) == "staging"


def test_resolution_env_var() -> None:
    """``CARVE_TARGET`` honored when no CLI flag."""
    config = _make_config(default="dev")
    env = {"CARVE_TARGET": "from_env"}
    assert resolve_active_target(None, config, env=env) == "from_env"


def test_resolution_default_target() -> None:
    """Falls through to ``default_target`` when no flag/env."""
    config = _make_config(default="prod")
    assert resolve_active_target(None, config, env={}) == "prod"


def test_resolution_hardcoded_fallback() -> None:
    """Returns ``"dev"`` when no Config (carve.toml missing)."""
    assert resolve_active_target(None, None, env={}) == "dev"


def test_resolution_blank_cli_flag_falls_through() -> None:
    """An empty-string ``--target`` should not override env/config."""
    config = _make_config(default="prod")
    env = {"CARVE_TARGET": "from_env"}
    # Empty string is falsy → resolution falls through to env.
    assert resolve_active_target("", config, env=env) == "from_env"


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("cli_flag", "../escape"),
        ("env", "Bad-Name"),
        ("default", "with space"),
    ],
)
def test_resolution_rejects_unsafe_target_names(
    source: str, value: str
) -> None:
    """Path-traversal-shaped or otherwise malformed target names are refused."""
    cli = value if source == "cli_flag" else None
    env = {"CARVE_TARGET": value} if source == "env" else {}
    config = _make_config(default=value if source == "default" else "dev")
    with pytest.raises(TargetResolutionError) as excinfo:
        resolve_active_target(cli, config, env=env)
    assert value in str(excinfo.value)


def test_require_target_raises_on_missing() -> None:
    """Missing target raises with the listing-of-existing-targets message."""
    with pytest.raises(TargetResolutionError) as excinfo:
        require_target("stagung", available=["dev", "staging", "prod"])
    msg = str(excinfo.value)
    assert "stagung" in msg
    assert "Available targets:" in msg
    assert "dev" in msg
    assert "staging" in msg
    assert "prod" in msg
    assert "carve target create stagung" in msg


def test_require_target_passes_when_present() -> None:
    """No exception raised when the target exists."""
    require_target("dev", available=["dev", "staging"])


def test_require_target_empty_available() -> None:
    """When no targets exist at all, the message says so explicitly."""
    with pytest.raises(TargetResolutionError) as excinfo:
        require_target("dev", available=[])
    assert "No targets defined yet" in str(excinfo.value)


def _conn_with(name: str) -> ConnectionsConfig:
    return ConnectionsConfig(
        snowflake={
            name: SnowflakeConnection(
                account="acc",
                user="u",
                role="r",
                warehouse="w",
                database="d",
            )
        }
    )
