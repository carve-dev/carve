"""Loader for the merged Carve configuration.

This module is the single filesystem boundary for config. Everything
downstream consumes the returned `Config` object.

Pipeline:
    1. Read `carve.toml` (required) for `[project]` and `[paths]`.
    2. Resolve `config_dir` and read each known sub-file if it exists.
    3. Walk the parsed dict tree and substitute `${VAR_NAME}` from `os.environ`.
    4. Validate the merged dict via pydantic.
    5. Compute a stable hash over the resolved raw dict and attach it.

Env var interpolation rules (M1):
    - `${VAR_NAME}` is replaced with `os.environ["VAR_NAME"]`.
    - `\\${LITERAL}` is preserved as the literal `${LITERAL}`.
    - No `${VAR:-default}` syntax. No nested `${${VAR}}`.
    - Missing env var raises `ConfigError` with the dotted field path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from carve.core.config.exceptions import ConfigError
from carve.core.config.schema import Config

# Files that contribute to the merged config, beyond `carve.toml`. The keys
# are top-level Config sections; the values are filenames inside `config_dir`.
_SUB_FILES: dict[str, str] = {
    "connections": "connections.toml",
    "models": "models.toml",
    "runner": "runner.toml",
    "server": "server.toml",
}

# Matches `${VAR_NAME}` but not `\${VAR_NAME}`. The negative lookbehind keeps
# the escape character in the captured text so we can strip it on substitution.
_ENV_VAR_RE = re.compile(r"(?<!\\)\$\{([^${}]+)\}")
# Detects nested patterns like `${${VAR}}` which we explicitly reject.
_NESTED_ENV_RE = re.compile(r"\$\{[^}]*\$\{")


def load_config(project_dir: Path | None = None) -> Config:
    """Load and validate the merged Carve config.

    Args:
        project_dir: Directory containing `carve.toml`. Defaults to the
            current working directory.

    Returns:
        Fully-validated `Config` with `config_hash` populated.

    Raises:
        ConfigError: For any user-facing config problem (missing file,
            malformed TOML, missing env var, validation failure).
    """
    project_dir = (project_dir or Path.cwd()).resolve()
    main_path = project_dir / "carve.toml"

    if not main_path.is_file():
        raise ConfigError(
            f"`carve.toml` not found in {project_dir}",
            file=main_path,
            hint="Run `carve init` to create a project skeleton, or pass --project-dir.",
        )

    main = _parse_toml(main_path)
    paths_section = main.get("paths", {}) or {}
    config_dir_name = paths_section.get("config_dir", "carve")
    config_dir = project_dir / config_dir_name

    raw: dict[str, Any] = {
        "project": main.get("project", {}) or {},
        "paths": paths_section,
    }
    # `[components.<name>]` blocks live top-level in `carve.toml` alongside
    # `[project]`/`[paths]`. Omitting them entirely is the convention-based
    # simple mode (empty dict). Env-var interpolation below recurses into
    # the nested blocks, so `${VAR}` inside a component works for free.
    components_section = main.get("components", {}) or {}
    if components_section:
        raw["components"] = components_section
    sub_file_paths: dict[str, Path] = {}
    for section, filename in _SUB_FILES.items():
        path = config_dir / filename
        sub_file_paths[section] = path
        raw[section] = _parse_toml(path) if path.is_file() else {}

    raw = _interpolate_env_vars(raw)

    # Combine the file map so validation errors can name the right file.
    file_map: dict[str, Path] = {
        "project": main_path,
        "paths": main_path,
        "components": main_path,
        **sub_file_paths,
    }

    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise _validation_error_to_config_error(exc, file_map) from exc

    config.config_hash = _compute_hash(raw)
    return config


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


def _parse_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Failed to parse TOML: {exc}",
            file=path,
            hint="Check the file for syntax errors (unbalanced quotes, bad escapes, etc.).",
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"Failed to read config file: {exc}",
            file=path,
        ) from exc


# ---------------------------------------------------------------------------
# Env var interpolation
# ---------------------------------------------------------------------------


def _interpolate_env_vars(data: Any, path: tuple[str, ...] = ()) -> Any:
    """Recursively substitute `${VAR}` references inside `data`.

    Walks dicts, lists, and tuples; rewrites only string leaves.
    """
    if isinstance(data, dict):
        return {k: _interpolate_env_vars(v, (*path, str(k))) for k, v in data.items()}
    if isinstance(data, list):
        return [_interpolate_env_vars(v, (*path, str(i))) for i, v in enumerate(data)]
    if isinstance(data, str):
        return _interpolate_string(data, path)
    return data


def _interpolate_string(value: str, path: tuple[str, ...]) -> str:
    if _NESTED_ENV_RE.search(value):
        raise ConfigError(
            "Nested `${...}` env var references are not supported",
            field=".".join(path) if path else None,
            hint="Resolve the inner variable separately and reference a single env var.",
        )

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1).strip()
        try:
            return os.environ[var_name]
        except KeyError:
            raise ConfigError(
                f"Environment variable {var_name} is not set",
                field=".".join(path) if path else None,
                hint=f"Add {var_name} to your .env file or environment.",
            ) from None

    interpolated = _ENV_VAR_RE.sub(_replace, value)
    # Resolve the escape: `\${LITERAL}` becomes `${LITERAL}`.
    return interpolated.replace(r"\${", "${")


# ---------------------------------------------------------------------------
# Pydantic validation -> ConfigError translation
# ---------------------------------------------------------------------------


def _validation_error_to_config_error(
    exc: ValidationError,
    file_map: dict[str, Path],
) -> ConfigError:
    """Pick the first validation error and render it as a ConfigError.

    Pydantic can report many errors at once; for the CLI we surface the
    first one with full context. The user can fix and re-run to see the
    next one.
    """
    errors = exc.errors()
    if not errors:  # pragma: no cover - defensive; pydantic always returns at least one
        return ConfigError(str(exc))

    err = errors[0]
    loc = tuple(str(part) for part in err.get("loc", ()))
    field = ".".join(loc) if loc else None
    section = loc[0] if loc else None
    file = file_map.get(section) if section is not None else None
    err_type = err.get("type", "")
    msg = err.get("msg", "validation failed")

    if err_type == "missing":
        message = f"Required field '{field}' is missing"
        hint = "Set this field in the file above, or provide it via env var interpolation."
    elif err_type == "extra_forbidden":
        message = f"Unknown field '{field}'"
        hint = "Remove the field, or check for a typo against the schema."
    else:
        message = f"Invalid value for '{field}': {msg}"
        hint = None

    return ConfigError(message, file=file, field=field, hint=hint)


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------


def _compute_hash(raw: dict[str, Any]) -> str:
    """Stable 16-hex-char hash over the resolved raw config dict.

    Used by the M2 plan store to detect config drift between when a plan
    was generated and when it is applied.
    """
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:16]
