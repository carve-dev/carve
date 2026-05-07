"""`CachedSkillExecutor` — invocation-scoped skill executor with caching.

Skill calls within a single agent invocation are cached by
``(name, json.dumps(kwargs, sort_keys=True))``. Cache scope is the
executor instance; new invocations construct a fresh executor and
therefore get a fresh cache.

JSON serialization (rather than ``frozenset(kwargs.items())``) is used
so structured inputs — Pillar 2's dbt-manifest skills are likely to
declare object/array inputs — cache correctly without raising on
non-hashable values.

No TTL — Snowflake catalog data doesn't change mid-invocation in
normal use, and a TTL would complicate the cache without helping.
"""

from __future__ import annotations

import json
from typing import Any

from carve.core.skills.context import SkillContext
from carve.core.skills.registry import SkillRegistry
from carve.core.skills.result import SkillResult


class SkillNotFound(KeyError):
    """Raised when `execute()` is called for an unregistered skill."""


class CachedSkillExecutor:
    """Cache-by-arguments wrapper around a `SkillRegistry`.

    The cache is a plain dict keyed on
    ``(skill_name, json.dumps(kwargs, sort_keys=True, default=str))``.
    Anthropic tool inputs are JSON-native (scalars, arrays, objects);
    JSON serialization handles every shape uniformly and produces a
    deterministic key. ``default=str`` prevents pathological input
    types (datetimes, etc.) from breaking the cache.
    """

    def __init__(self, skills: SkillRegistry) -> None:
        self.skills = skills
        self.cache: dict[tuple[str, str], SkillResult] = {}

    def execute(
        self,
        name: str,
        kwargs: dict[str, Any],
        ctx: SkillContext,
    ) -> SkillResult:
        """Run skill `name` with `kwargs` against `ctx`, caching by inputs."""
        key = (name, json.dumps(kwargs, sort_keys=True, default=str))
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        skill_fn = self.skills.get(name)
        if skill_fn is None:
            raise SkillNotFound(name)
        result = skill_fn(ctx, **kwargs)
        if not isinstance(result, SkillResult):
            # A skill that returns raw data (instead of wrapping in a
            # SkillResult) is a programming error — surface it here.
            raise TypeError(
                f"Skill {name!r} must return a SkillResult; got {type(result).__name__}."
            )
        self.cache[key] = result
        return result
