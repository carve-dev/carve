"""Schema-only tests — no filesystem, no env vars.

Verifies pydantic field rules and defaults that the loader relies on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    PathsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
    SnowflakeConnection,
)


class TestProjectConfig:
    def test_minimal_required_fields(self) -> None:
        cfg = ProjectConfig(name="x")
        assert cfg.name == "x"
        assert cfg.version == "0.0.1"
        assert cfg.default_target == "dev"

    def test_name_is_required(self) -> None:
        with pytest.raises(ValidationError):
            ProjectConfig()  # type: ignore[call-arg]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ProjectConfig(name="x", description="oops")  # type: ignore[call-arg]


class TestPathsConfig:
    def test_defaults(self) -> None:
        cfg = PathsConfig()
        assert cfg.config_dir == "carve"
        assert cfg.agents_dir == "carve/agents"
        assert cfg.skills_dir == "carve/skills"
        assert cfg.pipelines_dir == "carve/pipelines"


class TestSnowflakeConnection:
    def test_required_fields(self) -> None:
        conn = SnowflakeConnection(
            account="abc",
            user="u",
            role="r",
            warehouse="w",
            database="d",
        )
        assert conn.account == "abc"
        assert conn.password is None
        assert conn.authenticator == "snowflake"

    def test_schema_alias(self) -> None:
        """`schema` is a TOML key but `schema_` is the python attribute."""
        conn = SnowflakeConnection.model_validate(
            {
                "account": "a",
                "user": "u",
                "role": "r",
                "warehouse": "w",
                "database": "d",
                "schema": "PUBLIC",
            }
        )
        assert conn.schema_ == "PUBLIC"

    def test_missing_required_raises(self) -> None:
        with pytest.raises(ValidationError):
            SnowflakeConnection(account="a", user="u")  # type: ignore[call-arg]


class TestConnectionsConfig:
    def test_default_is_empty(self) -> None:
        assert ConnectionsConfig().snowflake == {}

    def test_multi_target(self) -> None:
        cfg = ConnectionsConfig.model_validate(
            {
                "snowflake": {
                    "dev": {
                        "account": "a",
                        "user": "u",
                        "role": "r",
                        "warehouse": "w",
                        "database": "d",
                    },
                    "prod": {
                        "account": "a2",
                        "user": "u2",
                        "role": "r2",
                        "warehouse": "w2",
                        "database": "d2",
                    },
                }
            }
        )
        assert set(cfg.snowflake.keys()) == {"dev", "prod"}


class TestModelsConfig:
    def test_anthropic_key_required(self) -> None:
        with pytest.raises(ValidationError):
            ModelsConfig()  # type: ignore[call-arg]


class TestRunnerConfig:
    def test_defaults(self) -> None:
        cfg = RunnerConfig()
        assert cfg.type == "local_venv"
        assert cfg.default_timeout_seconds == 1800
        assert cfg.max_concurrent_runs == 4


class TestServerConfig:
    def test_defaults(self) -> None:
        cfg = ServerConfig()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8787
        assert cfg.state_store.startswith("sqlite:///")
        assert cfg.auth_mode == "single_user"


class TestConfig:
    def test_minimal_construct(self) -> None:
        cfg = Config(
            project=ProjectConfig(name="x"),
            models=ModelsConfig(anthropic_api_key="k"),
        )
        assert cfg.project.name == "x"
        assert cfg.models.anthropic_api_key == "k"
        assert cfg.config_hash == ""  # populated by loader

    def test_extra_top_level_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Config.model_validate(
                {
                    "project": {"name": "x"},
                    "models": {"anthropic_api_key": "k"},
                    "unexpected_section": {},
                }
            )
