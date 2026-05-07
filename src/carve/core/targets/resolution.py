"""Resolve the active target from CLI flag, env var, or config.

Resolution order (first hit wins):

1. ``--target`` flag passed on the command line.
2. ``CARVE_TARGET`` environment variable.
3. ``default_target`` from ``carve.toml``.
4. Hard-coded fallback ``"dev"`` (only when no ``Config`` is available —
   e.g. early-init / pre-init scenarios).

The resolved name is *not* validated against ``carve/connections.toml`` here;
that's a separate step performed by ``require_target`` so that read-only
commands (like ``carve target list``) can call ``resolve_active_target``
without erroring out on a missing section.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from carve.core.config import Config
from carve.core.targets.registry import (
    InvalidTargetNameError,
    validate_target_name,
)


class TargetResolutionError(Exception):
    """Raised when the resolved target is not defined in connections.toml."""


def resolve_active_target(
    cli_flag: str | None,
    config: Config | None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve the active target name.

    Args:
        cli_flag: The value passed via ``--target X`` (None if absent).
        config: A loaded ``Config``, or None when ``carve.toml`` is missing.
        env: Override for ``os.environ`` (used by tests).

    Returns:
        The resolved target name. Never empty.

    Raises:
        TargetResolutionError: If the resolved value (from any source) fails
            the target-name regex. This protects downstream filesystem and
            configuration callers from path-traversal-shaped or otherwise
            malformed values supplied via flag, env, or hand-edited config.
    """
    env_map = env if env is not None else os.environ
    if cli_flag:
        resolved, source = cli_flag, "--target"
    else:
        env_value = env_map.get("CARVE_TARGET")
        if env_value:
            resolved, source = env_value, "CARVE_TARGET"
        elif config is not None and config.project.default_target:
            resolved, source = config.project.default_target, "carve.toml default_target"
        else:
            resolved, source = "dev", "fallback"

    try:
        validate_target_name(resolved)
    except InvalidTargetNameError as exc:
        raise TargetResolutionError(
            f"Invalid target name {resolved!r} from {source}: {exc}"
        ) from exc
    return resolved


def require_target(
    name: str,
    available: list[str],
) -> None:
    """Validate that ``name`` is among the configured targets.

    Args:
        name: The resolved target name.
        available: The list of target names defined in
            ``carve/connections.toml`` (one per ``[snowflake.<target>]``
            section).

    Raises:
        TargetResolutionError: If ``name`` is not in ``available``. The
            message lists the available targets so the user can see what
            is defined and what's missing.
    """
    if name in available:
        return
    if available:
        listed = ", ".join(sorted(available))
        msg = (
            f'target "{name}" not defined in carve/connections.toml.\n'
            f"Available targets: {listed}\n"
            f"Create one with: carve target create {name}"
        )
    else:
        msg = (
            f'target "{name}" not defined in carve/connections.toml.\n'
            f"No targets defined yet. Create one with: carve target create {name}"
        )
    raise TargetResolutionError(msg)
