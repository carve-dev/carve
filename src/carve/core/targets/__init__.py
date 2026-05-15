"""Target system — connection-config registry helpers.

A target is an environment (dev, staging, prod, …). Connection structure
lives centrally in ``carve/connections.toml`` (one ``[snowflake.<target>]``
section per target); secrets live in the root ``.env`` with target-prefixed
variable names. P1.1-01 dropped the per-target ``targets/<name>/``
filesystem tree — EL artifacts live in the flat ``el/<name>/`` tree,
target-agnostic.

This package contains:

* ``resolution`` — resolve the active target from CLI flag, env, config.
* ``registry`` — TOML edit-in-place helpers and the high-level
  ``add_target_to_project`` orchestrator used by both ``carve init`` and
  ``carve target create``.
"""

from carve.core.targets.registry import (
    DEFAULT_CONNECTIONS_TEMPLATE_HEADER,
    TARGET_NAME_RE,
    InvalidTargetNameError,
    TargetExistsError,
    TargetNotFoundError,
    add_env_example_block,
    add_target_section,
    add_target_to_project,
    list_target_sections,
    remove_env_example_block,
    remove_target_section,
    rename_env_example_block,
    rename_target_section,
    section_referenced_env_vars,
    show_section_values,
    validate_target_name,
)
from carve.core.targets.resolution import (
    TargetResolutionError,
    resolve_active_target,
)

__all__ = [
    "DEFAULT_CONNECTIONS_TEMPLATE_HEADER",
    "TARGET_NAME_RE",
    "InvalidTargetNameError",
    "TargetExistsError",
    "TargetNotFoundError",
    "TargetResolutionError",
    "add_env_example_block",
    "add_target_section",
    "add_target_to_project",
    "list_target_sections",
    "remove_env_example_block",
    "remove_target_section",
    "rename_env_example_block",
    "rename_target_section",
    "resolve_active_target",
    "section_referenced_env_vars",
    "show_section_values",
    "validate_target_name",
]
