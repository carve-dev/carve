"""``dbt_source_lookup`` — a callable Tool over the user's dbt ``sources.yml``.

The dlt engineer needs to know what raw schemas/tables the user's dbt
project already declares as *sources*, so a generated EL pipeline lands its
output where downstream dbt models expect it. This module reads those
``sources:`` declarations and exposes them as a path-confined, offline-testable
:class:`~carve.core.agents.tools.Tool`, mirroring ``integrations/dlt/skills.py``:
a ``make_dbt_source_lookup_tool(...)`` factory whose dependencies (the resolved
dbt-project dir) are injectable so unit tests run with no live project.

Resolution uses the **shipped** locator
(:func:`carve.integrations.component_locator._detect_dbt_project`, root +
one-level-down) — there is no separate ``integrations/dbt/locator.py``.

dbt source shape (flattened here)::

    sources:
      - name: stripe          # source name; `schema` defaults to this
        schema: raw_stripe    # optional override of the warehouse schema
        tables:
          - name: charges
          - name: customers

``op="list"`` returns every declaration; ``op="match"`` (``schema`` + ``table``)
returns the declaration matching that warehouse schema + table, or a not-found
result. A missing dbt project yields an empty list (not an error), consistent
with ``existing_dlt_inspect``'s empty-``el/`` behavior. Malformed YAML fails
closed with :class:`~carve.core.agents.tools.ToolExecutionError`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.config.paths import ProjectPaths
from carve.integrations.component_locator import _detect_dbt_project

_LOOKUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["list", "match"],
            "description": ("list every dbt source declaration, or match one by schema + table."),
        },
        "schema": {
            "type": "string",
            "description": "Warehouse schema to match (for op=match).",
        },
        "table": {
            "type": "string",
            "description": "Table name to match within the schema (for op=match).",
        },
    },
    "required": ["op"],
}


def make_dbt_source_lookup_tool(
    *,
    paths: ProjectPaths | None = None,
    dbt_root: Path | None = None,
    name: str = "dbt_source_lookup",
) -> Tool:
    """Build the ``dbt_source_lookup`` tool over the user's dbt project.

    Supply exactly one of ``paths`` (the project paths — the dbt project is
    detected via the shipped locator) or ``dbt_root`` (an already-resolved dbt
    project dir — lets unit tests stay offline without a full ``ProjectPaths``).
    The produced ``Tool.name`` equals ``name`` (the grant name) so the binder's
    ``injected.name == grant_name`` precondition holds.
    """
    if (paths is None) == (dbt_root is None):
        raise ValueError("Pass exactly one of `paths` or `dbt_root`.")

    def _resolve_dbt_root() -> Path | None:
        if dbt_root is not None:
            return dbt_root.resolve()
        assert paths is not None  # narrowed by the guard above
        return _detect_dbt_project(paths, required=False)

    def _execute(input_: ToolInput) -> ToolResult:
        op = input_.get("op")
        sources = _read_sources(_resolve_dbt_root())
        if op == "list":
            return {"sources": sources}
        if op == "match":
            schema = input_.get("schema")
            table = input_.get("table")
            if not isinstance(schema, str) or not schema.strip():
                raise ToolExecutionError("op=match requires a 'schema'.")
            if not isinstance(table, str) or not table.strip():
                raise ToolExecutionError("op=match requires a 'table'.")
            return _match(sources, schema.strip(), table.strip())
        raise ToolExecutionError(f"Unknown dbt_source_lookup op {op!r}; use list/match.")

    return Tool(
        name=name,
        description=(
            "Look up the user's dbt project source declarations (from sources.yml): "
            "list every source (name, schema, tables), or match a (schema, table) to "
            "its source config. Use to land an EL pipeline's output where downstream "
            "dbt models already expect a raw source."
        ),
        input_schema=_LOOKUP_SCHEMA,
        executor=_execute,
    )


def _read_sources(dbt_root: Path | None) -> list[dict[str, Any]]:
    """Read + flatten every ``sources:`` block under ``dbt_root``.

    Scans every ``**/*.yml`` (dbt allows ``sources:`` in any schema yaml, not
    only files literally named ``sources.yml``) and flattens dbt's
    ``sources: [{name, schema?, tables: [...]}]`` shape, defaulting a source's
    ``schema`` to its ``name``. Returns ``[]`` when no dbt project is present.
    Raises :class:`ToolExecutionError` on a malformed/unreadable yaml file
    (fail-closed, like the skill-pack loader).
    """
    if dbt_root is None or not dbt_root.is_dir():
        return []

    flattened: list[dict[str, Any]] = []
    for yml in sorted(dbt_root.rglob("*.yml")) + sorted(dbt_root.rglob("*.yaml")):
        if not yml.is_file():
            continue
        try:
            text = yml.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolExecutionError(f"Could not read {yml}: {exc}") from exc
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ToolExecutionError(f"Malformed YAML in {yml}: {exc}") from exc
        if not isinstance(doc, dict):
            continue
        raw_sources = doc.get("sources")
        if raw_sources is None:
            continue
        if not isinstance(raw_sources, list):
            raise ToolExecutionError(f"'sources' in {yml} must be a list.")
        for entry in raw_sources:
            flattened.append(_flatten_source(entry, yml))
    return flattened


def _flatten_source(entry: Any, yml: Path) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ToolExecutionError(f"Each source in {yml} must be a mapping.")
    src_name = entry.get("name")
    if not isinstance(src_name, str) or not src_name.strip():
        raise ToolExecutionError(f"A source in {yml} is missing a string 'name'.")
    src_name = src_name.strip()
    schema = entry.get("schema")
    schema = schema.strip() if isinstance(schema, str) and schema.strip() else src_name

    raw_tables = entry.get("tables") or []
    if not isinstance(raw_tables, list):
        raise ToolExecutionError(f"'tables' for source {src_name!r} in {yml} must be a list.")
    tables: list[dict[str, Any]] = []
    for tbl in raw_tables:
        if not isinstance(tbl, dict):
            raise ToolExecutionError(f"Each table for source {src_name!r} in {yml} must be a map.")
        tbl_name = tbl.get("name")
        if not isinstance(tbl_name, str) or not tbl_name.strip():
            raise ToolExecutionError(f"A table for source {src_name!r} in {yml} has no 'name'.")
        tables.append({**tbl, "name": tbl_name.strip()})

    return {
        "name": src_name,
        "schema": schema,
        "tables": tables,
        "defined_in": str(yml),
    }


def _match(sources: list[dict[str, Any]], schema: str, table: str) -> ToolResult:
    """Return the source declaration owning ``schema``.``table``, or not-found.

    Matches case-sensitively on the source's resolved ``schema`` and a declared
    table ``name``. dbt source/table names are warehouse identifiers and dbt
    treats them case-sensitively when quoted, so we match verbatim.
    """
    for source in sources:
        if source["schema"] != schema:
            continue
        for tbl in source["tables"]:
            if tbl["name"] == table:
                return {"found": True, "source": source, "table": tbl}
    return {"found": False, "schema": schema, "table": table}


__all__ = ["make_dbt_source_lookup_tool"]
