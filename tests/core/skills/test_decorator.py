"""Unit tests for the `@skill` decorator."""

from __future__ import annotations

import pytest

from carve.core.skills.decorator import SkillMetadata, get_metadata, skill
from carve.core.skills.registry import SkillRegistry


def test_skill_decorator_captures_metadata() -> None:
    """`@skill(...)` stamps a `SkillMetadata` blob on the function."""
    registry = SkillRegistry()

    @skill(
        name="my_skill",
        description="A test skill.",
        inputs={"x": {"type": "string", "required": True}},
        outputs={"y": {"type": "boolean"}},
    )
    def my_skill_fn(ctx: object, x: str) -> object:  # pragma: no cover - body unused
        return None

    # Manually register on a fresh registry too, so we can assert the
    # name -> fn mapping without leaning on the default registry.
    registry.register(my_skill_fn)

    metadata = get_metadata(my_skill_fn)
    assert isinstance(metadata, SkillMetadata)
    assert metadata.name == "my_skill"
    assert metadata.description == "A test skill."
    assert metadata.inputs == {"x": {"type": "string", "required": True}}
    assert metadata.outputs == {"y": {"type": "boolean"}}


def test_get_metadata_raises_on_undecorated_function() -> None:
    """`get_metadata` is explicit when the function isn't a skill."""

    def plain() -> None:  # pragma: no cover - body unused
        return None

    with pytest.raises(ValueError, match="not a @skill"):
        get_metadata(plain)
