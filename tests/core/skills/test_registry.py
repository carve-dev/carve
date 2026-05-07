"""Unit tests for `SkillRegistry`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from carve.core.skills.decorator import skill
from carve.core.skills.registry import SkillRegistry


def _build_two_skills() -> tuple[Callable[..., Any], Callable[..., Any]]:
    @skill(name="alpha", description="Alpha.", inputs={})
    def alpha(ctx: object) -> object:  # pragma: no cover
        return None

    @skill(
        name="beta",
        description="Beta with one input.",
        inputs={"q": {"type": "string", "required": True}},
    )
    def beta(ctx: object, q: str) -> object:  # pragma: no cover
        return None

    return alpha, beta


def test_registry_indexes_by_name() -> None:
    """Two registered skills can be looked up by their declared names."""
    alpha, beta = _build_two_skills()
    registry = SkillRegistry()
    registry.register(alpha)
    registry.register(beta)

    assert "alpha" in registry
    assert "beta" in registry
    assert registry["alpha"] is alpha
    assert registry["beta"] is beta
    assert sorted(registry.names()) == ["alpha", "beta"]


def test_registry_rejects_name_collisions() -> None:
    """Registering a different function under an existing name raises."""
    alpha, _ = _build_two_skills()
    registry = SkillRegistry()
    registry.register(alpha)

    @skill(name="alpha", description="Different alpha.", inputs={})
    def alpha_v2(ctx: object) -> object:  # pragma: no cover
        return None

    with pytest.raises(ValueError, match="already registered"):
        registry.register(alpha_v2)


def test_registry_generates_tool_schemas() -> None:
    """`inputs` declarations turn into Anthropic tool schemas correctly."""
    alpha, beta = _build_two_skills()
    registry = SkillRegistry()
    registry.register(alpha)
    registry.register(beta)

    schemas = registry.to_tool_schemas()
    by_name = {s["name"]: s for s in schemas}

    # Alpha has no inputs.
    alpha_schema = by_name["alpha"]
    assert alpha_schema["description"] == "Alpha."
    assert alpha_schema["input_schema"] == {"type": "object", "properties": {}}

    # Beta has one required string input.
    beta_schema = by_name["beta"]
    assert beta_schema["input_schema"]["type"] == "object"
    assert beta_schema["input_schema"]["properties"]["q"] == {"type": "string"}
    assert beta_schema["input_schema"]["required"] == ["q"]


def test_registry_schema_carries_default_value() -> None:
    """A skill with a default-valued input surfaces the default in the schema."""

    @skill(
        name="with_default",
        description="Has a default.",
        inputs={
            "include": {"type": "boolean", "default": True},
        },
    )
    def fn(ctx: object, include: bool = True) -> object:  # pragma: no cover
        return None

    registry = SkillRegistry()
    registry.register(fn)
    schemas = registry.to_tool_schemas()
    assert schemas[0]["input_schema"]["properties"]["include"]["default"] is True
    # `required` should not appear when there are no required inputs.
    assert "required" not in schemas[0]["input_schema"]
