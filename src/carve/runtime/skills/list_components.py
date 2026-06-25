"""``list_components`` — a callable Tool that enumerates component names.

Before wiring a `component = "<name>"` into a step, the pipeline engineer
needs to know *which* component names exist — the `el/<name>/` dlt dirs and
the detected dbt project discovered by convention (simple mode), plus any
`[components.<name>]` blocks already written (multi mode). This is the
"which component names exist" lookup the engineer greps against; it returns
**names + type + mode only**, never component contents (the engineer
composes by reference, it does not read dlt code / dbt models here — those
are other tools).

Mirrors ``integrations/dlt/skills.py``: an offline-testable
:class:`~carve.core.agents.tools.Tool` whose dependencies (the resolved
:class:`ProjectPaths` + the ``[components.*]`` blocks) are injected so unit
tests run with no live project. Resolution delegates to the **shipped**
:func:`carve.integrations.component_locator.discover_components` (the
convention set) merged with the block-defined names — never re-deriving the
path math.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from carve.core.agents.tools import Tool, ToolInput, ToolResult
from carve.integrations.component_locator import discover_components, resolve_component

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.schema import ComponentConfig

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def make_list_components_tool(
    *,
    paths: ProjectPaths,
    components: dict[str, ComponentConfig] | None = None,
    name: str = "list_components",
) -> Tool:
    """Build the ``list_components`` tool over ``paths`` + the ``[components.*]`` blocks.

    ``paths`` roots convention discovery (`el/<name>/` dirs + the detected dbt
    project); ``components`` supplies any ``[components.<name>]`` blocks
    (defaults to empty == simple mode). The produced ``Tool.name`` equals
    ``name`` (the grant name) so the binder's ``injected.name == grant_name``
    precondition holds.
    """
    blocks = components or {}

    def _execute(_input: ToolInput) -> ToolResult:
        by_name: dict[str, dict[str, Any]] = {}

        # Convention-discovered components (simple mode): el/<name>/ + the
        # detected dbt project. mode="convention" — no block backs them.
        for resolved in discover_components(paths):
            by_name[resolved.name] = {
                "name": resolved.name,
                "type": resolved.type.value,
                "mode": "convention",
            }

        # Block-defined components (multi mode) win — they carry the explicit
        # type/mode the user pinned, overriding a same-named convention entry.
        for block_name, block in blocks.items():
            by_name[block_name] = {
                "name": block_name,
                "type": block.type.value,
                "mode": block.mode.value,
            }

        components_out = [by_name[key] for key in sorted(by_name)]
        return {"components": components_out}

    return Tool(
        name=name,
        description=(
            "List the component names available to reference in a pipeline step "
            '(component = "<name>"). Returns each component\'s name, type (dlt|dbt), '
            "and mode (convention for simple-mode el/<name>/ dirs + the detected dbt "
            "project, or the [components.<name>] block's mode for graduated ones). "
            "Names + type + mode only — no contents. Use to confirm a component name "
            "exists before wiring a step."
        ),
        input_schema=_LIST_SCHEMA,
        executor=_execute,
    )


# Re-export so callers building the engineer's context can reuse the locator's
# single resolution entry point without a second import line.
__all__ = ["make_list_components_tool", "resolve_component"]
