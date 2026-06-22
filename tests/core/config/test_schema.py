"""Schema-only tests — no filesystem, no env vars.

Verifies pydantic field rules and defaults that the loader relies on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from carve.core.config.schema import (
    AutoFixConfig,
    Config,
    ConnectionsConfig,
    ModelsConfig,
    PathsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
    SnowflakeConnection,
)
from carve.core.config.state_store import (
    DEFAULT_STATE_STORE_URL,
    StateStoreConfig,
    resolve_state_store_url,
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
        assert cfg.targets_dir == "targets"

    def test_relative_nested_paths_allowed(self) -> None:
        cfg = PathsConfig(targets_dir="data/envs", config_dir="cfg")
        assert cfg.targets_dir == "data/envs"
        assert cfg.config_dir == "cfg"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            " ",
            "/abs/targets",
            "\\abs\\targets",
            "..",
            "../escape",
            "ok/../escape",
            "with\x00nul",
        ],
    )
    def test_unsafe_paths_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            PathsConfig(targets_dir=bad)


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
    def test_anthropic_key_optional_at_load_time(self) -> None:
        """The API key is required at *use*-time (plan/build), not load-time.

        Keeping it optional lets `load_config()` succeed against a freshly-
        initialised project whose `models.toml` is fully commented out.
        """
        cfg = ModelsConfig()
        assert cfg.anthropic_api_key is None
        assert cfg.default_model == "claude-opus-4-8"

    def test_anthropic_key_accepted_when_provided(self) -> None:
        cfg = ModelsConfig(anthropic_api_key="sk-foo")
        assert cfg.anthropic_api_key == "sk-foo"


class TestRunnerConfig:
    def test_defaults(self) -> None:
        cfg = RunnerConfig()
        assert cfg.type == "local_venv"
        assert cfg.default_timeout_seconds == 1800
        assert cfg.git_timeout_seconds == 300
        assert cfg.max_concurrent_runs == 4

    def test_git_timeout_seconds_override_and_floor(self) -> None:
        assert RunnerConfig(git_timeout_seconds=600).git_timeout_seconds == 600
        with pytest.raises(ValidationError):
            RunnerConfig(git_timeout_seconds=0)


class TestAutoFixConfig:
    def test_defaults(self) -> None:
        cfg = AutoFixConfig()
        assert cfg.enabled is True
        assert cfg.max_attempts == 3

    def test_max_attempts_lower_bound(self) -> None:
        # Zero is allowed (disables retries) but negatives are not.
        AutoFixConfig(max_attempts=0)
        with pytest.raises(ValidationError):
            AutoFixConfig(max_attempts=-1)

    def test_max_attempts_upper_bound(self) -> None:
        AutoFixConfig(max_attempts=10)
        with pytest.raises(ValidationError):
            AutoFixConfig(max_attempts=11)
        with pytest.raises(ValidationError):
            AutoFixConfig(max_attempts=999)


class TestServerConfig:
    def test_defaults(self) -> None:
        cfg = ServerConfig()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8787
        # v0.1-01: state_store defaults to the Postgres connection string;
        # the legacy SQLite default is gone.
        assert cfg.state_store == DEFAULT_STATE_STORE_URL
        assert cfg.state_store.startswith("postgresql+psycopg://")
        assert cfg.auth_mode == "single_user"


class TestStateStoreConfig:
    """Schema rules for the v0.1-01 ``[state_store]`` runtime.toml section."""

    def test_defaults(self) -> None:
        cfg = StateStoreConfig()
        assert cfg.url == DEFAULT_STATE_STORE_URL
        assert cfg.url.startswith("postgresql+psycopg://")
        assert cfg.pool_size == 10
        assert cfg.max_overflow == 20

    def test_override_url(self) -> None:
        cfg = StateStoreConfig(
            url="postgresql+psycopg://u:p@db.example.com:5432/carve"
        )
        assert cfg.url == "postgresql+psycopg://u:p@db.example.com:5432/carve"

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            StateStoreConfig.model_validate(
                {
                    "url": DEFAULT_STATE_STORE_URL,
                    "unknown_key": True,
                }
            )

    @pytest.mark.parametrize("size", [0, -1, 101])
    def test_pool_size_bounds(self, size: int) -> None:
        with pytest.raises(ValidationError):
            StateStoreConfig(pool_size=size)

    @pytest.mark.parametrize("overflow", [-1, 201])
    def test_max_overflow_bounds(self, overflow: int) -> None:
        with pytest.raises(ValidationError):
            StateStoreConfig(max_overflow=overflow)


class TestResolveStateStoreUrl:
    """``resolve_state_store_url`` precedence: state_store.url, then the
    legacy ``server.state_store`` alias, then the default."""

    def test_default_when_neither_overridden(self) -> None:
        cfg = Config(
            project=ProjectConfig(name="x"),
            models=ModelsConfig(anthropic_api_key="k"),
        )
        assert resolve_state_store_url(cfg) == DEFAULT_STATE_STORE_URL

    def test_state_store_url_wins_over_default(self) -> None:
        custom = "postgresql+psycopg://u:p@db.example.com/carve"
        cfg = Config(
            project=ProjectConfig(name="x"),
            models=ModelsConfig(anthropic_api_key="k"),
            state_store=StateStoreConfig(url=custom),
        )
        assert resolve_state_store_url(cfg) == custom

    def test_legacy_server_state_store_alias_used_when_state_store_default(
        self,
    ) -> None:
        """Legacy M1 projects set ``server.state_store`` in server.toml; the
        loader falls back to that value when ``state_store.url`` is the
        module default. New projects that drift away from the default win.
        """
        legacy_url = "postgresql+psycopg://legacy:legacy@db/carve"
        cfg = Config(
            project=ProjectConfig(name="x"),
            models=ModelsConfig(anthropic_api_key="k"),
            server=ServerConfig(state_store=legacy_url),
        )
        assert resolve_state_store_url(cfg) == legacy_url

    def test_state_store_url_wins_over_legacy_alias(self) -> None:
        new_url = "postgresql+psycopg://new:new@db/carve"
        legacy_url = "postgresql+psycopg://legacy:legacy@db/carve"
        cfg = Config(
            project=ProjectConfig(name="x"),
            models=ModelsConfig(anthropic_api_key="k"),
            server=ServerConfig(state_store=legacy_url),
            state_store=StateStoreConfig(url=new_url),
        )
        assert resolve_state_store_url(cfg) == new_url


class TestConfig:
    def test_minimal_construct(self) -> None:
        cfg = Config(
            project=ProjectConfig(name="x"),
            models=ModelsConfig(anthropic_api_key="k"),
        )
        assert cfg.project.name == "x"
        assert cfg.models.anthropic_api_key == "k"
        assert cfg.config_hash == ""  # populated by loader
        # The new state_store subsection is populated with defaults.
        assert cfg.state_store.url == DEFAULT_STATE_STORE_URL

    def test_extra_top_level_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Config.model_validate(
                {
                    "project": {"name": "x"},
                    "models": {"anthropic_api_key": "k"},
                    "unexpected_section": {},
                }
            )

    def test_state_store_section_parsed(self) -> None:
        custom = "postgresql+psycopg://u:p@db.example.com/carve"
        cfg = Config.model_validate(
            {
                "project": {"name": "x"},
                "models": {"anthropic_api_key": "k"},
                "state_store": {"url": custom, "pool_size": 5},
            }
        )
        assert cfg.state_store.url == custom
        assert cfg.state_store.pool_size == 5
