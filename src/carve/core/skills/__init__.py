"""Skill infrastructure — decorator + registry + executor + context.

Public surface:

- `SkillContext` — per-invocation state passed to every skill call.
- `SkillResult` — uniform return shape (data + truncation + count).
- `SkillRegistry` — name -> function index with tool-schema generation.
- `CachedSkillExecutor` — invocation-scoped cache by (name, kwargs).
- `skill` — decorator that registers a function with metadata.
- `default_registry()` — process-wide registry populated by built-in skills.
- `load_builtin_skills()` — convenience helper that imports the builtin
  module so the default registry has every catalog skill available.
"""

from carve.core.skills.context import SkillContext
from carve.core.skills.decorator import SkillMetadata, get_metadata, skill
from carve.core.skills.executor import CachedSkillExecutor, SkillNotFound
from carve.core.skills.registry import SkillRegistry, default_registry
from carve.core.skills.result import SkillResult


def load_builtin_skills() -> SkillRegistry:
    """Import the built-in skill modules and return the default registry.

    Importing `carve.core.skills.builtin` triggers each `@skill` to
    register itself in the module-level default registry. Returning the
    populated registry keeps callers from needing to know about the
    side-effect import.
    """
    from carve.core.skills import builtin  # noqa: F401

    return default_registry()


__all__ = [
    "CachedSkillExecutor",
    "SkillContext",
    "SkillMetadata",
    "SkillNotFound",
    "SkillRegistry",
    "SkillResult",
    "default_registry",
    "get_metadata",
    "load_builtin_skills",
    "skill",
]
