"""Config loading and validation.

Public surface:

- `load_config(project_dir=None) -> Config` — the only filesystem entry point.
- `Config` — the merged, validated configuration object.
- `ConfigError` — single exception type used for every config failure mode.

Everything downstream (CLI commands, agent runtime, server) accepts a
`Config` instance rather than re-reading TOML files.
"""

from carve.core.config.exceptions import ConfigError
from carve.core.config.loader import load_config
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

__all__ = [
    "Config",
    "ConfigError",
    "ConnectionsConfig",
    "ModelsConfig",
    "PathsConfig",
    "ProjectConfig",
    "RunnerConfig",
    "ServerConfig",
    "SnowflakeConnection",
    "load_config",
]
