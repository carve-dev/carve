"""Pydantic schemas for the merged Carve configuration.

The schema is intentionally minimal for M1: project metadata, paths, a
single connection family (Snowflake), the Anthropic model key, runner
defaults, and the embedded server. M2 and M3 will extend it.

`Config.config_hash` is populated by the loader after parsing — it is
declared with a default so model construction in tests doesn't require
threading it through.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from carve.core.config.state_store import DEFAULT_STATE_STORE_URL, StateStoreConfig


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
    targets_dir: str = "targets"
    # Extensibility (spec 16) config-file locations. Both are
    # project-relative paths joined with the project root by callers, and
    # guarded by the same `_project_relative` validator (no `..`, relative
    # POSIX) so a malicious carve.toml can't redirect them out of the tree.
    hooks_file: str = "carve/hooks.toml"
    mcp_file: str = "carve/mcp.toml"

    @field_validator(
        "config_dir",
        "agents_dir",
        "skills_dir",
        "pipelines_dir",
        "targets_dir",
        "hooks_file",
        "mcp_file",
    )
    @classmethod
    def _project_relative(cls, value: str) -> str:
        # Block path-traversal vectors that would let a malicious carve.toml
        # redirect filesystem operations outside the project root. The fields
        # are joined with the project root by callers; here we enforce that
        # the value is a relative POSIX-style path with no `..` segments and
        # no absolute prefix. Empty / whitespace-only values are also refused.
        if not value or value.strip() != value or value.strip() == "":
            raise ValueError("path must be a non-empty, non-whitespace string")
        if value.startswith("/") or value.startswith("\\"):
            raise ValueError(f"path must be relative; got {value!r}")
        path = PurePosixPath(value)
        if path.is_absolute():
            raise ValueError(f"path must be relative; got {value!r}")
        for part in path.parts:
            if part == "..":
                raise ValueError(f"path must not contain '..'; got {value!r}")
            if "\x00" in part:
                raise ValueError(f"path must not contain NUL bytes; got {value!r}")
        return value


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


class DuckDBConnection(BaseModel):
    """A DuckDB target — the first-class local-dev + test substrate.

    ``path`` is the database file (``:memory:`` for an ephemeral in-process
    database). DuckDB needs no credentials, so there's no role/secret surface.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = ":memory:"


class ConnectionsConfig(BaseModel):
    """Top-level container for connection definitions.

    Snowflake + DuckDB are first-class; sub-keys are user-chosen target names
    (e.g. ``dev``, ``prod``). A target's **dialect** is whichever block it
    appears under.
    """

    model_config = ConfigDict(extra="forbid")

    snowflake: dict[str, SnowflakeConnection] = Field(default_factory=dict)
    duckdb: dict[str, DuckDBConnection] = Field(default_factory=dict)


class ModelsConfig(BaseModel):
    """Model-provider configuration: how Carve authenticates to Anthropic
    and which models it uses (Anthropic-only).

    Credential *precedence* and client construction live in one place —
    ``carve.core.agents.client_factory.make_client`` — which reads
    ``auth_mode`` (here) plus the environment. The secret itself never
    lives in ``models.toml``: the API key / OAuth token come from the
    environment (``${ANTHROPIC_API_KEY}`` / ``ANTHROPIC_AUTH_TOKEN``). See
    the model-auth capability spec.
    """

    model_config = ConfigDict(extra="forbid")

    # Credential mode. ``api_key`` uses ``ANTHROPIC_API_KEY``; ``oauth`` uses
    # a Claude-subscription OAuth bearer (``ANTHROPIC_AUTH_TOKEN`` /
    # ``CLAUDE_CODE_OAUTH_TOKEN``). ``None`` -> auto-resolve by precedence
    # (key first, then token). An explicit value forces that one path.
    auth_mode: str | None = None

    # The API key, when ``auth_mode`` is (or resolves to) ``api_key``.
    # Required at *use*-time, not *load*-time: keeping it optional lets
    # ``load_config()`` succeed against a freshly-initialised project whose
    # ``models.toml`` is fully commented; ``client_factory.make_client``
    # raises a ``ConfigError`` when a command actually needs a credential.
    anthropic_api_key: str | None = None

    # The install-default model id every per-agent ``model:`` falls back to.
    default_model: str = "claude-opus-4-8"

    # Optional named tiers a per-agent ``model:`` frontmatter may reference
    # (e.g. ``fast = "claude-haiku-4-5"``). Resolved via ``resolve_model``.
    tiers: dict[str, str] = Field(default_factory=dict)

    @field_validator("auth_mode")
    @classmethod
    def _valid_auth_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in ("api_key", "oauth"):
            raise ValueError(f"auth_mode must be 'api_key' or 'oauth', got {value!r}")
        return value

    def resolve_model(self, ref: str | None) -> str:
        """Resolve a per-agent ``model:`` reference to a concrete model id.

        ``None`` falls back to ``default_model``; a name matching a key in
        ``tiers`` resolves to that tier's model; anything else is treated as
        a literal model id and returned unchanged.
        """
        if ref is None:
            return self.default_model
        return self.tiers.get(ref, ref)


class AutoFixConfig(BaseModel):
    """`[runner.auto_fix]` — recovery agent's bounded budget.

    Introduced by P1-09. ``enabled = true`` lets the recovery agent
    wrap ``carve el run`` and ``carve el deploy`` failures; the CLI's
    ``--no-auto-fix`` flag overrides it to ``false`` per invocation.
    ``max_attempts`` is the per-failure-event budget — a single deploy
    can burn the budget independently in each of its three phases.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_attempts: int = Field(default=3, ge=0, le=10)


class RunnerConfig(BaseModel):
    """Pipeline-runner defaults. Local venv runner only for M1."""

    model_config = ConfigDict(extra="forbid")

    type: str = "local_venv"
    venv_cache_dir: str = ".carve/venvs"
    default_timeout_seconds: int = 1800
    # Bound (seconds) on each git workspace-sync subprocess for
    # separate-remote components, so an unreachable remote can't hang a
    # worker. The sync triggers pass it to
    # `carve.integrations.workspace_cache.sync_workspace(timeout=…)`; the
    # cache's own floor default matches this value.
    git_timeout_seconds: int = Field(default=300, ge=1)
    max_concurrent_runs: int = 4
    auto_fix: AutoFixConfig = Field(default_factory=AutoFixConfig)


class ServerConfig(BaseModel):
    """Embedded HTTP server configuration.

    The ``state_store`` field is kept as a string alias for backward
    compatibility with M1 ``server.toml`` files (and tests that pre-date
    v0.1-01). New projects set ``state_store.url`` in ``runtime.toml``
    instead; the loader copies that value over here so the runtime
    engine factory has a single place to look. Defaults to the v0.1
    Postgres URL.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8787
    state_store: str = DEFAULT_STATE_STORE_URL
    auth_mode: str = "single_user"


class ComponentType(StrEnum):
    """The kind of component a `[components.<name>]` block references.

    A component is either a dlt pipeline (`dlt`) or a dbt project
    (`dbt`). The type tells the locator how to resolve and run it — they
    are symmetric in topology but resolved differently (see
    `carve.integrations.component_locator`). The value is a plain string
    so it round-trips cleanly through TOML.
    """

    DLT = "dlt"
    DBT = "dbt"


class ComponentMode(StrEnum):
    """Where a component's code lives — its repo topology.

    Three discrete strings rather than inferring mode from the presence
    of `path`/`url`; explicit is friendlier to validate and less
    surprising (spec *Design notes*).

    * ``same-repo`` — code lives in this control-plane working tree
      (``el/<name>/`` for dlt, the detected dbt project for dbt).
    * ``separate-local`` — code lives at an on-disk ``path`` outside the
      tree; required when this mode is set.
    * ``separate-remote`` — code lives in a git repo at ``url``; cloned
      into the workspace cache and checked out at ``ref``/``branch``.
    """

    SAME_REPO = "same-repo"
    SEPARATE_LOCAL = "separate-local"
    SEPARATE_REMOTE = "separate-remote"


class ComponentConfig(BaseModel):
    """A single `[components.<name>]` block in `carve.toml`.

    Records *which* typed component the control plane references and
    *where* it resolves. The block name (the dict key in
    ``Config.components``) is the component name a pipeline step
    references by ``component = "<name>"``; it is not duplicated as a
    field here.

    Cross-field rules (spec *`carve.toml` schema additions* + *`ref` vs
    `branch` precedence*):

    * ``path`` is required iff ``mode == "separate-local"``.
    * ``url`` is required iff ``mode == "separate-remote"``.
    * ``ref`` (a commit SHA or tag) is a **pin**; ``branch`` tracks a
      branch HEAD. ``ref`` always wins when both are present — the
      precedence is encoded in the locator, but the schema accepts both
      so the locator can apply it.

    ``url``/``branch``/``ref`` are only meaningful for
    ``separate-remote``; ``path`` only for ``separate-local``. They are
    left optional rather than split into per-mode models so the TOML
    shape stays a single flat block.
    """

    model_config = ConfigDict(extra="forbid")

    type: ComponentType
    mode: ComponentMode
    url: str | None = None
    branch: str | None = None
    path: str | None = None
    ref: str | None = None
    # Sync semantics for separate-remote (spec *Open questions*): hard
    # sync by default (``git fetch && reset --hard``); ``soft`` opts into
    # ``git pull``. ``sync_before_run`` lets a component skip the
    # before-each-run sync for offline operation. Both ride here so the
    # workspace cache + runtime can read them off the resolved config.
    sync_mode: str = "hard"
    sync_before_run: bool = True

    @field_validator("sync_mode")
    @classmethod
    def _known_sync_mode(cls, value: str) -> str:
        if value not in ("hard", "soft"):
            raise ValueError(f"sync_mode must be 'hard' or 'soft'; got {value!r}")
        return value

    @field_validator("url")
    @classmethod
    def _safe_url(cls, value: str | None) -> str | None:
        # Defense-in-depth for the git subprocess that clones a
        # separate-remote component (carve.integrations.workspace_cache):
        # allow only transports git can fetch over safely, and reject
        # option-shaped or alternate-transport URLs (`--upload-pack=...`,
        # `ext::sh -c ...`) that could smuggle a flag or arbitrary command
        # into `git clone`. Enforced at config load, before any git call.
        if value is None:
            return value
        url = value.strip()
        if not url:
            raise ValueError("url must not be empty")
        if url.startswith("-"):
            raise ValueError(f"url must not start with '-' (option-shaped); got {value!r}")
        if url.startswith(("https://", "ssh://", "git://", "file://")):
            return value
        # scp-style `[user@]host:path`: no scheme, a single `:` splitting a
        # slash-free host from a path. `::` (the `transport::address` form,
        # e.g. `ext::...`) is excluded explicitly.
        if "://" not in url and "::" not in url:
            host, sep, path = url.partition(":")
            if sep and host and path and "/" not in host and host not in (".", ".."):
                return value
        raise ValueError(
            f"url transport not allowed; got {value!r}. Use https://, ssh://, "
            "git://, file://, or scp-style git@host:path."
        )

    @field_validator("ref", "branch")
    @classmethod
    def _safe_ref_branch(cls, value: str | None) -> str | None:
        # `ref`/`branch` flow into `git checkout <value>` as a positional, so
        # an option-shaped value (`--orphan=…`) would be parsed as a git flag
        # — option injection. `git checkout <value> --` does NOT neutralize it
        # (git parses the option before the trailing `--`; verified), so the
        # value must be *rejected*. Reject a leading `-`, and constrain to
        # git's `check-ref-format` charset. Enforced at config load.
        if value is None:
            return value
        if not value or value != value.strip():
            raise ValueError(f"ref/branch must be non-empty and unpadded; got {value!r}")
        if value.startswith("-"):
            raise ValueError(f"ref/branch must not start with '-' (option-shaped); got {value!r}")
        if re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value):
            raise ValueError(f"ref/branch contains an illegal character; got {value!r}")
        if (
            ".." in value
            or "@{" in value
            or "//" in value
            or value == "@"
            or value.startswith("/")
            or value.endswith(("/", ".", ".lock"))
        ):
            raise ValueError(f"ref/branch is not a valid git ref name; got {value!r}")
        return value

    @model_validator(mode="after")
    def _check_mode_fields(self) -> ComponentConfig:
        if self.mode is ComponentMode.SEPARATE_LOCAL and not self.path:
            raise ValueError("`path` is required when mode == 'separate-local'")
        if self.mode is ComponentMode.SEPARATE_REMOTE and not self.url:
            raise ValueError("`url` is required when mode == 'separate-remote'")
        # `path` is meaningless outside separate-local; `url`/`branch`/`ref`
        # outside separate-remote. Reject the contradiction rather than
        # silently ignoring it, so a mis-set block is caught at load time.
        if self.mode is not ComponentMode.SEPARATE_LOCAL and self.path:
            raise ValueError("`path` is only valid when mode == 'separate-local'")
        if self.mode is not ComponentMode.SEPARATE_REMOTE and (self.url or self.branch or self.ref):
            raise ValueError("`url`/`branch`/`ref` are only valid when mode == 'separate-remote'")
        return self


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
    state_store: StateStoreConfig = Field(default_factory=StateStoreConfig)
    # `[components.<name>]` blocks keyed by component name. An empty dict
    # is the convention-based **simple mode**: components are discovered
    # from `el/<name>/` dirs + the detected dbt project (see
    # `carve.integrations.component_locator.discover_components`). Blocks
    # only materialize when a component is split out to a separate repo.
    components: dict[str, ComponentConfig] = Field(default_factory=dict)
    config_hash: str = ""
