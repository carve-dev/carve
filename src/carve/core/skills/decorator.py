"""`@skill` decorator — captures metadata and registers with the registry.

A skill is a plain Python function whose first positional arg is a
`SkillContext`. The decorator stamps `_skill_metadata` onto the
function so the registry can discover it, and registers it in the
*default* `SkillRegistry` for cases where built-in skills are imported
purely for their side effects.

Tests and callers that want a clean registry construct their own
`SkillRegistry` and use `registry.register(fn)` directly — no global
state required.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillMetadata:
    """Static description of a skill.

    Mirrors the bits the agent loop needs to expose the skill as a
    tool to the model: a name, a description, and an `inputs` dict
    that follows the same shape as the spec example. Outputs are
    declared for documentation only — they don't drive validation in
    Pillar 1.
    """

    name: str
    description: str
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)


SkillFn = Callable[..., Any]


def skill(
    *,
    name: str,
    description: str,
    inputs: dict[str, dict[str, Any]] | None = None,
    outputs: dict[str, Any] | None = None,
) -> Callable[[SkillFn], SkillFn]:
    """Decorate a function as a skill.

    Attaches a `_skill_metadata` attribute. Registration with a
    `SkillRegistry` is explicit at the call site (the built-in skills
    are registered by `carve.core.skills.builtin.__init__`); this keeps
    the decorator side-effect free, which makes the test suite
    deterministic across module re-imports.
    """

    def _wrap(fn: SkillFn) -> SkillFn:
        metadata = SkillMetadata(
            name=name,
            description=description,
            inputs=dict(inputs or {}),
            outputs=dict(outputs or {}),
        )
        fn._skill_metadata = metadata  # type: ignore[attr-defined]
        return fn

    return _wrap


def get_metadata(fn: SkillFn) -> SkillMetadata:
    """Return the metadata stamped onto a `@skill`-decorated function.

    Raises `ValueError` if the function isn't a skill — this keeps the
    error explicit when the registry's `register()` is called with a
    bare callable.
    """
    metadata = getattr(fn, "_skill_metadata", None)
    if not isinstance(metadata, SkillMetadata):
        raise ValueError(
            f"Function {fn.__name__!r} is not a @skill — no metadata attached."
        )
    return metadata
