"""`SkillRegistry` — collects `@skill`-decorated functions.

The registry holds skills by name and exposes:

- `register(fn)` — add one skill.
- `__getitem__(name)`, `get(name)`, `names()` — lookup.
- `to_tool_schemas()` — convert each skill's `inputs` declaration into
  the Anthropic-compatible tool schema shape used by the rest of the
  agent loop.

A module-level default registry (`default_registry()`) exists for the
side-effect-driven import pattern that built-in skills use. Production
callers (`generate_plan`) get the default registry after importing the
catalog module; tests that want isolation construct a fresh
`SkillRegistry` and register skills onto it explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from carve.core.skills.decorator import SkillFn, get_metadata


class SkillRegistry:
    """In-memory map from skill name to function."""

    def __init__(self) -> None:
        self._skills: dict[str, SkillFn] = {}

    # ---- registration --------------------------------------------------------

    def register(self, fn: SkillFn) -> SkillFn:
        """Register `fn` keyed on its decorator-supplied name.

        Re-registration with the same function is idempotent. Re-registration
        of a different function under the same name raises — the registry is
        a single source of truth and silent overwrites would mask bugs.
        """
        metadata = get_metadata(fn)
        existing = self._skills.get(metadata.name)
        if existing is not None and existing is not fn:
            raise ValueError(
                f"A different skill is already registered under name "
                f"{metadata.name!r}; cannot overwrite."
            )
        self._skills[metadata.name] = fn
        return fn

    # ---- lookup --------------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills

    def __getitem__(self, name: str) -> SkillFn:
        return self._skills[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._skills)

    def __len__(self) -> int:
        return len(self._skills)

    def get(self, name: str) -> SkillFn | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills.keys())

    def values(self) -> list[SkillFn]:
        return list(self._skills.values())

    # ---- tool-schema generation ---------------------------------------------

    def to_tool_schemas(self) -> list[dict[str, Any]]:
        """Render every skill as an Anthropic tool-schema dict.

        The output matches the shape produced by `Tool.to_schema()` so the
        agent loop can drop these straight into `tools=[...]` alongside the
        non-skill tools.
        """
        return [_tool_schema_for(fn) for fn in self._skills.values()]


# ---------------------------------------------------------------------------
# Tool-schema rendering
# ---------------------------------------------------------------------------


_DEFAULT_TYPE = "string"


def _tool_schema_for(fn: SkillFn) -> dict[str, Any]:
    """Return the Anthropic tool-schema dict for a single skill."""
    metadata = get_metadata(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg_name, declaration in metadata.inputs.items():
        properties[arg_name] = _input_schema(declaration)
        if declaration.get("required"):
            required.append(arg_name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return {
        "name": metadata.name,
        "description": metadata.description,
        "input_schema": schema,
    }


def _input_schema(declaration: dict[str, Any]) -> dict[str, Any]:
    """Convert a single skill input declaration into JSON-schema form.

    The declaration uses the spec's compact shape:
    ``{"type": "string", "required": True, "default": ...}``. We strip
    `required` (it's handled at the parent level) but keep `default`
    when present so the agent sees the implicit value.
    """
    out: dict[str, Any] = {
        "type": declaration.get("type", _DEFAULT_TYPE),
    }
    if "description" in declaration:
        out["description"] = declaration["description"]
    if "default" in declaration:
        out["default"] = declaration["default"]
    if "items" in declaration:
        out["items"] = declaration["items"]
    if "enum" in declaration:
        out["enum"] = declaration["enum"]
    return out


# ---------------------------------------------------------------------------
# Default (module-level) registry
# ---------------------------------------------------------------------------


_default_registry = SkillRegistry()


def default_registry() -> SkillRegistry:
    """Return the process-wide default `SkillRegistry`.

    Built-in skills (under `carve.core.skills.builtin`) register
    themselves here on import. Production callers import the built-in
    module then call `default_registry()` to get the populated instance.
    """
    return _default_registry
