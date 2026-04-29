"""Pydantic schemas for the merged Carve configuration.

The schema is intentionally minimal for M1: project metadata, paths, a
single connection family (Snowflake), the Anthropic model key, runner
defaults, and the embedded server. M2 and M3 will extend it.

`Config.config_hash` is populated by the loader after parsing — it is
declared with a default so model construction in tests doesn't require
threading it through.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProjectConfig(BaseModel):
    """`[project]` section of `carve.toml`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = "0.0.1"
    default_target: str = "dev"


class PathsConfig(BaseModel):
    """`[paths]` section of `carve.toml`."""

    model_config = ConfigDict(extra="forbid")

    config_dir: str = "carve"
    agents_dir: str = "carve/agents"
    skills_dir: str = "carve/skills"
    pipelines_dir: str = "carve/pipelines"


class SnowflakeConnection(BaseModel):
    """A single Snowflake connection definition.

    `schema` is a reserved attribute name in pydantic v1, but in v2 it is
    fine — pydantic-2 allows arbitrary field names that don't shadow
    `BaseModel`'s methods.
    """

    model_config = ConfigDict(extra="forbid")

    account: str
    user: str
    password: str | None = None
    private_key_path: str | None = None
    authenticator: str = "snowflake"
    role: str
    warehouse: str
    database: str
    schema_: str | None = Field(default=None, alias="schema")


class ConnectionsConfig(BaseModel):
    """Top-level container for connection definitions.

    For M1 only Snowflake is supported. Sub-keys are user-chosen target
    names (e.g. ``dev``, ``prod``).
    """

    model_config = ConfigDict(extra="forbid")

    snowflake: dict[str, SnowflakeConnection] = Field(default_factory=dict)


class ModelsConfig(BaseModel):
    """Model-provider credentials. Anthropic-only for M1."""

    model_config = ConfigDict(extra="forbid")

    anthropic_api_key: str
    default_model: str = "claude-sonnet-4-5-20250929"


class RunnerConfig(BaseModel):
    """Pipeline-runner defaults. Local venv runner only for M1."""

    model_config = ConfigDict(extra="forbid")

    type: str = "local_venv"
    venv_cache_dir: str = ".carve/venvs"
    default_timeout_seconds: int = 1800
    max_concurrent_runs: int = 4


class ServerConfig(BaseModel):
    """Embedded HTTP server configuration."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8787
    state_store: str = "sqlite:///.carve/state.db"
    auth_mode: str = "single_user"


class Config(BaseModel):
    """Fully-merged, validated Carve configuration.

    Produced by `carve.core.config.load_config`. Downstream code accepts
    this object instead of touching the filesystem itself.
    """

    model_config = ConfigDict(extra="forbid")

    project: ProjectConfig
    paths: PathsConfig = Field(default_factory=PathsConfig)
    connections: ConnectionsConfig = Field(default_factory=ConnectionsConfig)
    models: ModelsConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    config_hash: str = ""
