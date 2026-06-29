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
from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import (
    DbtStepConfig,
    DltStepConfig,
    FailureMode,
    Pipeline,
    PipelineError,
    PipelineMeta,
    PipelineStep,
    SeedSchedule,
    SqlStepConfig,
    load_pipeline,
)
from carve.core.config.schema import (
    ArchiveConfig,
    ComponentConfig,
    ComponentMode,
    ComponentType,
    Config,
    ConnectionsConfig,
    ModelsConfig,
    PathsConfig,
    ProjectConfig,
    RunnerConfig,
    RuntimeConfig,
    ServerConfig,
    SnowflakeConnection,
    parse_duration,
)
from carve.core.config.state_store import DEFAULT_STATE_STORE_URL, StateStoreConfig

__all__ = [
    "DEFAULT_STATE_STORE_URL",
    "ArchiveConfig",
    "ComponentConfig",
    "ComponentMode",
    "ComponentType",
    "Config",
    "ConfigError",
    "ConnectionsConfig",
    "DbtStepConfig",
    "DltStepConfig",
    "FailureMode",
    "ModelsConfig",
    "PathsConfig",
    "Pipeline",
    "PipelineError",
    "PipelineMeta",
    "PipelineStep",
    "ProjectConfig",
    "ProjectPaths",
    "RunnerConfig",
    "RuntimeConfig",
    "SeedSchedule",
    "ServerConfig",
    "SnowflakeConnection",
    "SqlStepConfig",
    "StateStoreConfig",
    "load_config",
    "load_pipeline",
    "parse_duration",
]
