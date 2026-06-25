"""``list_dbt_models`` — an alias over the shipped ``dbt_manifest`` reader.

The pipeline engineer's frontmatter grants ``list_dbt_models``; the shipped
manifest reader is :func:`carve.integrations.dbt.manifest.make_dbt_manifest_tool`,
a ``dbt_manifest`` tool with a ``list_models`` op (plus three per-model ops
the engineer doesn't need). This module **aliases** that reader — it does
**not** re-implement a second manifest parser.

The alias mechanism: build the shipped ``dbt_manifest`` tool, then re-expose
it as a single-op :class:`~carve.core.agents.tools.Tool` named
``list_dbt_models`` whose executor calls the wrapped tool with a pinned
``op="list_models"``. The wrapper's ``Tool.name`` equals the grant name
(``list_dbt_models``) so the binder's ``injected.name == grant_name``
precondition holds, while every byte of manifest reading stays in the shipped
reader. The wrapper takes **no** ``op`` input (it's pinned), so the engineer
sees a clean "list the dbt models" tool rather than the four-op manifest
surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from carve.core.agents.tools import Tool, ToolInput, ToolResult
from carve.integrations.dbt.manifest import make_dbt_manifest_tool

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths

_LIST_MODELS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def make_list_dbt_models_tool(
    *,
    paths: ProjectPaths | None = None,
    dbt_root: Path | None = None,
    target_path: Path | None = None,
    name: str = "list_dbt_models",
) -> Tool:
    """Build ``list_dbt_models`` as a thin alias over the shipped ``dbt_manifest`` tool.

    Supply exactly one resolution source — ``paths`` (the dbt project is
    detected via the shipped locator), ``dbt_root`` (an already-resolved dbt
    project dir), or ``target_path`` (the ``target/`` dir directly, for offline
    unit tests) — exactly as :func:`make_dbt_manifest_tool` expects; the
    arguments pass straight through. The produced ``Tool.name`` equals ``name``
    (the grant name) so the binder's ``injected.name == grant_name``
    precondition holds.
    """
    # Build the shipped reader (it validates the "exactly one source" contract)
    # and pin its `list_models` op behind a no-`op`-input alias.
    manifest_tool = make_dbt_manifest_tool(
        paths=paths,
        dbt_root=dbt_root,
        target_path=target_path,
        name="dbt_manifest",
    )

    def _execute(_input: ToolInput) -> ToolResult:
        # Delegate to the shipped manifest reader with the op pinned — never a
        # second manifest parse.
        return manifest_tool.executor({"op": "list_models"})

    return Tool(
        name=name,
        description=(
            "List every model in the user's compiled dbt manifest "
            "(target/manifest.json): each model's name, file path, materialization, "
            "schema, and tags. Use to confirm which dbt models exist before wiring a "
            "dbt step's `select`. (Aliases the dbt_manifest list_models op.)"
        ),
        input_schema=_LIST_MODELS_SCHEMA,
        executor=_execute,
    )


__all__ = ["make_list_dbt_models_tool"]
