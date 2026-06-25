"""``pipeline_inspect`` — a callable Tool over ``pipelines/<name>.toml``.

The pipeline engineer reasons about the project's existing pipelines — what
steps each one has, the DAG (`depends_on`), each step's failure mode and
component reference, and the optional `[seed_schedule]` — by reading the
**parsed, validated** shape from the **shipped**
:func:`carve.core.config.pipeline_schema.load_pipeline`, not by re-parsing
TOML by hand. ``load_pipeline`` *is* the validation engine, so a `read`
that the loader rejects (a cycle, a dangling `depends_on`, an unresolvable
component name) surfaces the structured :class:`PipelineError` message to the
engineer — the same gate ``carve pipelines validate`` runs.

This mirrors ``integrations/dlt/skills.py`` / ``integrations/dbt/sources.py``:
an op-dispatch :class:`~carve.core.agents.tools.Tool` with its dependencies
(the resolved :class:`ProjectPaths` + the ``[components.*]`` blocks
``load_pipeline`` needs) injected so unit tests run offline with no live
project. The tool is **path-confined to ``pipelines/**``** — a `read` of a
name that would escape the pipelines dir is a clean error, never a read
outside the tree.

- ``op="list"`` → the ``pipelines/*.toml`` filenames (names only, no parse).
- ``op="read"`` (``name``) → that pipeline's structured shape: metadata,
  ``[seed_schedule]``, and the ordered steps (id / type / component /
  depends_on / failure mode), parsed via ``load_pipeline``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.config.pipeline_schema import (
    DbtStepConfig,
    DltStepConfig,
    Pipeline,
    PipelineError,
    SqlStepConfig,
    load_pipeline,
)

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.schema import ComponentConfig

_INSPECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["list", "read"],
            "description": (
                "list the pipelines/*.toml files, or read one pipeline's parsed "
                "shape (steps/DAG/seed_schedule)."
            ),
        },
        "name": {
            "type": "string",
            "description": "Pipeline name (the file stem, for op=read).",
        },
    },
    "required": ["op"],
}


def make_pipeline_inspect_tool(
    *,
    paths: ProjectPaths,
    components: dict[str, ComponentConfig] | None = None,
    name: str = "pipeline_inspect",
) -> Tool:
    """Build the ``pipeline_inspect`` tool over ``paths.pipelines_dir``.

    ``paths`` roots the (path-confined) ``pipelines/**`` reads and is the
    :class:`ProjectPaths` ``load_pipeline`` needs to resolve component names;
    ``components`` supplies the ``[components.*]`` blocks for that resolution
    (defaults to empty == simple mode). The produced ``Tool.name`` equals
    ``name`` (the grant name) so the binder's ``injected.name == grant_name``
    precondition holds.
    """
    pipelines_dir = paths.pipelines_dir.resolve()
    blocks = components or {}

    def _list() -> ToolResult:
        if not pipelines_dir.is_dir():
            return {"pipelines": []}
        names = sorted(
            child.stem
            for child in pipelines_dir.iterdir()
            if child.is_file() and child.suffix == ".toml" and not child.name.startswith(".")
        )
        return {"pipelines": names}

    def _read(pipeline_name: str) -> ToolResult:
        toml_path = (pipelines_dir / f"{pipeline_name}.toml").resolve()
        # Path-confinement: a `name` carrying separators / traversal must not
        # escape pipelines/**. The resolved file must sit directly under it.
        if toml_path.parent != pipelines_dir:
            raise ToolExecutionError(f"Pipeline {pipeline_name!r} is outside the pipelines/ tree.")
        if not toml_path.is_file():
            raise ToolExecutionError(f"No pipeline named {pipeline_name!r}.")
        try:
            pipeline = load_pipeline(toml_path, components=blocks, paths=paths)
        except PipelineError as exc:
            # `load_pipeline` IS the validate gate — surface its structured
            # message so the engineer can self-correct (e.g. a cycle, a bad
            # depends_on, an unresolvable component name).
            raise ToolExecutionError(str(exc)) from exc
        return _pipeline_to_shape(pipeline)

    def _execute(input_: ToolInput) -> ToolResult:
        op = input_.get("op")
        if op == "list":
            return _list()
        if op == "read":
            pipeline_name = input_.get("name")
            if not isinstance(pipeline_name, str) or not pipeline_name.strip():
                raise ToolExecutionError("op=read requires a 'name'.")
            return _read(pipeline_name.strip())
        raise ToolExecutionError(f"Unknown pipeline_inspect op {op!r}; use list/read.")

    return Tool(
        name=name,
        description=(
            "Inspect the project's pipelines under pipelines/: list them (list), or "
            "read one pipeline's parsed shape (read) — metadata, [seed_schedule], and "
            "the ordered steps (id, type, component, depends_on, failure mode). Reading "
            "runs the same schema + DAG validation `carve pipelines validate` does, so a "
            "malformed pipeline surfaces its structured error. Use to understand an "
            "existing composition before changing it."
        ),
        input_schema=_INSPECT_SCHEMA,
        executor=_execute,
    )


def _pipeline_to_shape(pipeline: Pipeline) -> dict[str, Any]:
    """Render a parsed :class:`Pipeline` into a JSON-serializable shape."""
    seed: dict[str, Any] | None = None
    if pipeline.seed_schedule is not None:
        seed = {
            "cron": pipeline.seed_schedule.cron,
            "timezone": pipeline.seed_schedule.timezone,
            "target": pipeline.seed_schedule.target,
        }
    return {
        "name": pipeline.name,
        "description": pipeline.pipeline.description,
        "owner": pipeline.pipeline.owner,
        "seed_schedule": seed,
        "steps": [_step_to_shape(step) for step in pipeline.steps],
    }


def _step_to_shape(step: Any) -> dict[str, Any]:
    """Render one step into {id, type, component, depends_on, failure_mode, …}."""
    shape: dict[str, Any] = {
        "id": step.id,
        "type": step.type,
        "depends_on": list(step.depends_on),
        "failure_mode": step.failure_mode.mode,
    }
    if isinstance(step, DltStepConfig):
        shape["component"] = step.component
    elif isinstance(step, DbtStepConfig):
        shape["component"] = step.component
        shape["command"] = step.command
        if step.select is not None:
            shape["select"] = step.select
    elif isinstance(step, SqlStepConfig):
        shape["file"] = step.file
        shape["connection"] = step.connection
    return shape


__all__ = ["make_pipeline_inspect_tool"]
