"""Shared name validators for targets and EL artifacts.

The deploy and verify CLIs need to validate ``--from`` / ``--to`` /
``--target`` and the artifact ``<name>`` argument before any of those
values are interpolated into a filesystem path. Hoisting the regexes
into one module (rather than re-importing the EL agent's private
helper) gives both CLIs a stable import path and lets the agent share
the same definition.

Both regexes match the project's existing convention:

* Target names are validated at every project-edit verb in
  ``carve.core.targets.registry`` (re-exported here for convenience).
* Artifact names follow the same shape — lowercase letters, digits,
  underscores; must start with a letter — to keep them safe to
  interpolate into ``targets/<t>/el/<artifact>/`` paths.
"""

from __future__ import annotations

import re

from carve.core.targets.registry import (
    InvalidTargetNameError,
    validate_target_name,
)

# Same shape as the EL agent's `_ARTIFACT_NAME_RE` (M1.1-04). Hoisted
# here so callers outside the agent can validate without reaching into
# a private helper.
ARTIFACT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class InvalidArtifactNameError(ValueError):
    """Raised when an artifact name fails the naming-regex validation."""


def validate_artifact_name(name: str) -> str:
    """Return ``name`` if it matches the artifact naming regex, else raise.

    Raises:
        InvalidArtifactNameError: If ``name`` does not match the regex.
    """
    if not isinstance(name, str) or not ARTIFACT_NAME_RE.fullmatch(name):
        raise InvalidArtifactNameError(
            f"Invalid artifact name {name!r}: must match "
            f"{ARTIFACT_NAME_RE.pattern} (lowercase letters, digits, "
            "and underscores; must start with a letter)."
        )
    return name


__all__ = [
    "ARTIFACT_NAME_RE",
    "InvalidArtifactNameError",
    "InvalidTargetNameError",
    "validate_artifact_name",
    "validate_target_name",
]
