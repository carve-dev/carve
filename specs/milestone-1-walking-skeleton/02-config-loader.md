# M1-02 — Config loader

**Milestone:** 1 — Walking skeleton
**Estimated effort:** 0.5 day
**Dependencies:** M1-01 (CLI foundation)

## Purpose

Load `carve.toml` and the files in `carve/`, validate them against typed schemas, resolve environment variable interpolation, and provide a typed `Config` object accessible everywhere else in the codebase.

## Scope

### In scope

- Reading `carve.toml` from the current directory (or a `--project-dir` flag)
- Reading additional TOML files from `carve/` per the `[paths]` config
- Pydantic schemas for the file contents
- `${VAR_NAME}` environment variable interpolation
- Computing a content hash for the resolved config (used for plan validity checks later)
- Clear error messages for misconfiguration

### Out of scope

- Agent YAML loading (M2)
- Skill discovery (M3)
- Pipeline TOML loading (M2 — single Python step is hardcoded for M1)
- MCP server config (M3)

## Schema

For M1, the minimum config schema:

```python
from pydantic import BaseModel, Field

class ProjectConfig(BaseModel):
    name: str
    version: str = "0.0.1"
    default_target: str = "dev"

class PathsConfig(BaseModel):
    config_dir: str = "carve"
    agents_dir: str = "carve/agents"
    skills_dir: str = "carve/skills"
    pipelines_dir: str = "carve/pipelines"

class SnowflakeConnection(BaseModel):
    account: str
    user: str
    password: str | None = None
    private_key_path: str | None = None
    authenticator: str = "snowflake"
    role: str
    warehouse: str
    database: str
    schema: str | None = None

class ConnectionsConfig(BaseModel):
    snowflake: dict[str, SnowflakeConnection] = Field(default_factory=dict)

class ModelsConfig(BaseModel):
    anthropic_api_key: str

class RunnerConfig(BaseModel):
    type: str = "local_venv"
    venv_cache_dir: str = ".carve/venvs"
    default_timeout_seconds: int = 1800
    max_concurrent_runs: int = 4

class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    state_store: str = "sqlite:///.carve/state.db"
    auth_mode: str = "single_user"

class Config(BaseModel):
    project: ProjectConfig
    paths: PathsConfig
    connections: ConnectionsConfig
    models: ModelsConfig
    runner: RunnerConfig
    server: ServerConfig
    config_hash: str  # computed
```

For M1 the schema is minimal. M2 adds dbt-related fields, observability, guardrails. M3 adds MCP, skills, etc.

## File layout

After loading, these files contribute to the merged `Config`:

| File | Section in Config |
|---|---|
| `carve.toml` | `project`, `paths` |
| `carve/connections.toml` | `connections` |
| `carve/models.toml` | `models` |
| `carve/runner.toml` | `runner` |
| `carve/server.toml` | `server` |

Missing files use the schema defaults. Missing required fields (e.g., no Snowflake account) produce a clear validation error.

## Loader implementation

### File: `src/carve/core/config/loader.py`

Pseudocode:

```python
def load_config(project_dir: Path | None = None) -> Config:
    project_dir = project_dir or Path.cwd()

    # 1. Load carve.toml
    main = parse_toml(project_dir / "carve.toml")

    # 2. Resolve config_dir from main file
    config_dir = project_dir / main.get("paths", {}).get("config_dir", "carve")

    # 3. Load each known sub-file if it exists
    raw = {
        "project": main.get("project", {}),
        "paths": main.get("paths", {}),
        "connections": parse_toml(config_dir / "connections.toml") or {},
        "models": parse_toml(config_dir / "models.toml") or {},
        "runner": parse_toml(config_dir / "runner.toml") or {},
        "server": parse_toml(config_dir / "server.toml") or {},
    }

    # 4. Recursively interpolate env vars
    raw = interpolate_env_vars(raw)

    # 5. Validate via pydantic
    config = Config.model_validate(raw)

    # 6. Compute hash
    config.config_hash = compute_hash(raw)

    return config
```

### Environment variable interpolation

Pattern: `${VAR_NAME}` is replaced with the value of `os.environ["VAR_NAME"]`. If the env var is missing, raise a clear error pointing at the offending field path:

```
ConfigError: Environment variable SNOWFLAKE_ACCOUNT is not set
  → at connections.snowflake.dev.account
```

Implementation: walk the dict tree post-`tomllib` parse, re-string-format any value matching the pattern.

Edge cases:

- Nested env vars (`${${VAR_NAME}}`) — not supported, fail with clear error
- Default values (`${VAR:-default}`) — not supported in M1, can add later if asked for
- Escaping — `\${LITERAL}` is supported to write a literal `${LITERAL}` in the output

### Hash computation

`compute_hash(raw_dict) → str`:

- Canonicalize the dict (sorted keys, no whitespace)
- SHA-256 hash of the canonical JSON
- Return first 16 hex chars

Used later by the plan store to validate plans against the config they were generated against.

## Error handling

Configuration errors are user errors — they need to be helpful, not just truthful.

Examples of the error format:

```
ConfigError: Required field 'connections.snowflake.dev.account' is missing
  File: carve/connections.toml
  Hint: Add an [snowflake.dev] section with at least 'account', 'user', 'role', 'warehouse', and 'database'.

ConfigError: Environment variable ANTHROPIC_API_KEY is not set
  File: carve/models.toml
  Field: models.anthropic_api_key
  Hint: Add ANTHROPIC_API_KEY to your .env file or environment.
```

Use a custom `ConfigError` exception that carries enough context to render messages like the above. The CLI should catch `ConfigError` and exit with code 2.

## Tests

- Loading a valid full config returns a populated `Config`
- Missing optional files use defaults
- Missing required field raises `ConfigError` with helpful message
- Env interpolation works for nested values
- Missing env var raises `ConfigError` pointing at the field path
- Hash is deterministic for the same input
- Hash differs when any field changes
- Loading from a different `--project-dir` works

Use `pytest` fixtures with `tmp_path` to create test config trees on disk.

## Acceptance criteria

- `Config` can be loaded from a project created by `carve init`
- All field validation errors produce clear, actionable error messages
- Env var interpolation works for all nested fields
- The loader is the only component that touches the filesystem for config; the rest of the system uses the returned `Config` object
- Hash is stable, computed at load time, exposed as `config.config_hash`

## Files this spec produces

- `src/carve/core/__init__.py`
- `src/carve/core/config/__init__.py`
- `src/carve/core/config/loader.py`
- `src/carve/core/config/schema.py`
- `src/carve/core/config/exceptions.py`
- `tests/core/config/test_loader.py`
- `tests/core/config/test_schema.py`
- `tests/core/config/fixtures/` (sample valid/invalid configs)

## What this enables

- Every subsequent component takes a `Config` as input and is testable in isolation
- `carve init` can produce config files and trust the loader to validate them
- Plan validity checks (M2) have a hash to compare against
