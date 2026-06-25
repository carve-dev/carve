"""The pipeline engineer's callable skills + the ``extra_tools`` factory.

The pipeline-engineer subagent (``builtin/pipeline-engineer.md``) declares
``tools: [edit, grep, pipeline_inspect, list_components, list_dbt_models,
sql, web_fetch, "mcp:*"]``. The harness base grants (``edit``/``grep``/
``web_fetch``) bind automatically off the :class:`BindingContext`; ``"mcp:*"``
keeps the raising stub. The three domain skills here — plus the optional
warehouse-coupled ``sql`` tool — must be passed through the binder's
``extra_tools`` keyed by their grant names, because their dependencies (the
resolved :class:`ProjectPaths`, the ``[components.*]`` blocks, and — for
``sql`` — a live warehouse connection) live outside what the binder can
construct.

:func:`build_pipeline_engineer_extra_tools` is that factory. The (deferred)
live orchestrator and the tests both call it; the factory is the single place
the engineer's grant names map to real, dependency-injected tools. ``sql`` is
optional: the live orchestrator injects a warehouse-backed ``sql`` tool, but
an offline caller omits it and the binder keeps the raising stub for ``sql``
(a grant that's surfaced but fails loud if called) — exactly the binder's
unbindable-name contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from carve.core.agents.tools import Tool
from carve.runtime.skills.list_components import make_list_components_tool
from carve.runtime.skills.list_dbt_models import make_list_dbt_models_tool
from carve.runtime.skills.pipeline_inspect import make_pipeline_inspect_tool

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.schema import ComponentConfig


def build_pipeline_engineer_extra_tools(
    *,
    paths: ProjectPaths,
    components: dict[str, ComponentConfig] | None = None,
    sql_tool: Tool | None = None,
) -> dict[str, Tool]:
    """Build the pipeline engineer's ``extra_tools`` map (grant name -> tool).

    Returns the three domain skills keyed by their grant names
    (``pipeline_inspect``, ``list_components``, ``list_dbt_models``), each
    dependency-injected from ``paths`` + ``components`` so they run offline.
    When ``sql_tool`` is supplied (the live orchestrator builds it with a real
    warehouse connection), it is included under the ``sql`` grant; otherwise
    ``sql`` is omitted and the binder keeps its raising stub.

    Each tool's ``Tool.name`` equals its key, satisfying the binder's
    ``injected.name == grant_name`` precondition.
    """
    blocks = components or {}
    extra: dict[str, Tool] = {
        "pipeline_inspect": make_pipeline_inspect_tool(paths=paths, components=blocks),
        "list_components": make_list_components_tool(paths=paths, components=blocks),
        "list_dbt_models": make_list_dbt_models_tool(paths=paths),
    }
    if sql_tool is not None:
        if sql_tool.name != "sql":
            raise ValueError(
                f"sql_tool must bind under the grant name 'sql'; got {sql_tool.name!r}."
            )
        extra["sql"] = sql_tool
    return extra


__all__ = [
    "build_pipeline_engineer_extra_tools",
    "make_list_components_tool",
    "make_list_dbt_models_tool",
    "make_pipeline_inspect_tool",
]
