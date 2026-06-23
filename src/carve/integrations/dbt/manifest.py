"""``dbt_manifest`` — a callable Tool over dbt's compiled ``target/manifest.json``.

The dbt engineer reasons about the user's dbt project — its models, each model's
declared columns, a model's upstream/downstream dependencies, and the data tests
attached to a model — by reading dbt's **compiled artifact**, ``target/manifest.json``,
not by re-parsing ``.sql``/``_schema.yml`` by hand. dbt writes ``manifest.json`` on
every ``dbt parse``/``compile``/``build``; it is the authoritative, fully-resolved
description of the project.

This module exposes those reads as a single, path-confined, offline-testable
op-dispatch :class:`~carve.core.agents.tools.Tool`, mirroring
``integrations/dbt/sources.py`` and ``integrations/dlt/skills.py``: a
``make_dbt_manifest_tool(...)`` factory whose dependency (the resolved dbt-project
dir, or the ``target/`` dir directly) is injectable so unit tests run with no live
project and no live ``dbt`` run.

Resolution uses the **shipped** locator
(:func:`carve.integrations.component_locator._detect_dbt_project`, root +
one-level-down) — there is no separate ``integrations/dbt/locator.py``.

The four ops read the *same* loaded manifest, so they share one Tool (cleaner than
four factories). Grounded in dbt's real ``manifest.json`` schema — top-level
``nodes`` (keyed by ``unique_id`` like ``model.<pkg>.<name>`` / ``test.<pkg>.<name>``),
``sources``, ``parent_map``, ``child_map``; per-node ``resource_type``, ``columns``,
``depends_on.nodes``, ``config.materialized``, ``tags``, ``schema``/``database`` —
no invented fields.

- ``op="list_models"`` → every model node (name, resource path, materialization,
  schema, tags) from ``nodes`` where ``resource_type == "model"``.
- ``op="model_columns"`` (``model``) → that model's declared ``columns`` (name +
  description/data_type + the tests attached to the column).
- ``op="model_dependencies"`` (``model``) → upstream + downstream from the manifest's
  ``parent_map`` / ``child_map`` (unique_ids resolved to readable names).
- ``op="tests_on_model"`` (``model``) → the ``test`` nodes that depend on the model
  (via ``depends_on.nodes`` / ``child_map``): test name + kind + the column it targets.

A missing dbt project (or no ``manifest.json``) yields an empty/not-found result, not
a crash — consistent with ``dbt_source_lookup``'s missing-project behavior. A
malformed manifest fails closed with
:class:`~carve.core.agents.tools.ToolExecutionError`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.config.paths import ProjectPaths
from carve.integrations.component_locator import _detect_dbt_project

_MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": [
                "list_models",
                "model_columns",
                "model_dependencies",
                "tests_on_model",
            ],
            "description": (
                "list_models: every model (name, path, materialization, schema, tags). "
                "model_columns: a model's declared columns. "
                "model_dependencies: a model's upstream + downstream. "
                "tests_on_model: the data tests attached to a model."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Model name (e.g. 'stg_orders'), for the per-model ops. The fully "
                "qualified unique_id (model.<pkg>.<name>) is also accepted."
            ),
        },
    },
    "required": ["op"],
}


def make_dbt_manifest_tool(
    *,
    paths: ProjectPaths | None = None,
    dbt_root: Path | None = None,
    target_path: Path | None = None,
    name: str = "dbt_manifest",
) -> Tool:
    """Build the ``dbt_manifest`` tool over the user's compiled dbt manifest.

    Supply exactly one resolution source: ``paths`` (the project paths — the dbt
    project is detected via the shipped locator), ``dbt_root`` (an already-resolved
    dbt project dir — ``manifest.json`` is read from ``<dbt_root>/target``), or
    ``target_path`` (the ``target/`` dir directly — lets unit tests point straight
    at a fixtures dir, no project resolution). The produced ``Tool.name`` equals
    ``name`` (the grant name) so the binder's ``injected.name == grant_name``
    precondition holds.
    """
    if sum(src is not None for src in (paths, dbt_root, target_path)) != 1:
        raise ValueError("Pass exactly one of `paths`, `dbt_root`, or `target_path`.")

    def _resolve_target() -> Path | None:
        if target_path is not None:
            return target_path.resolve()
        if dbt_root is not None:
            return (dbt_root / "target").resolve()
        assert paths is not None  # narrowed by the guard above
        root = _detect_dbt_project(paths, required=False)
        return (root / "target").resolve() if root is not None else None

    def _execute(input_: ToolInput) -> ToolResult:
        op = input_.get("op")
        manifest = _load_manifest(_resolve_target())
        if op == "list_models":
            return {"models": _list_models(manifest)}
        if op in ("model_columns", "model_dependencies", "tests_on_model"):
            model = input_.get("model")
            if not isinstance(model, str) or not model.strip():
                raise ToolExecutionError(f"op={op} requires a 'model'.")
            model_name = model.strip()
            node_id = _resolve_model_id(manifest, model_name)
            if node_id is None:
                return {"found": False, "model": model_name}
            if op == "model_columns":
                columns = _model_columns(manifest, node_id)
                return {"found": True, "model": model_name, "columns": columns}
            if op == "model_dependencies":
                return {
                    "found": True,
                    "model": model_name,
                    **_model_dependencies(manifest, node_id),
                }
            return {
                "found": True,
                "model": model_name,
                "tests": _tests_on_model(manifest, node_id),
            }
        raise ToolExecutionError(
            f"Unknown dbt_manifest op {op!r}; use "
            "list_models/model_columns/model_dependencies/tests_on_model."
        )

    return Tool(
        name=name,
        description=(
            "Read the user's compiled dbt manifest (target/manifest.json): list every "
            "model (list_models), a model's declared columns (model_columns), a model's "
            "upstream/downstream dependencies (model_dependencies), or the data tests "
            "attached to a model (tests_on_model). Use to understand the existing dbt "
            "project before authoring or changing a model."
        ),
        input_schema=_MANIFEST_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# Manifest loading (fail-closed)
# ---------------------------------------------------------------------------


def _load_manifest(target_dir: Path | None) -> dict[str, Any]:
    """Load ``manifest.json`` from ``target_dir``, or an empty manifest if absent.

    A missing project / target / manifest yields an empty manifest (the ops then
    return empty/not-found results, never a crash). A present-but-malformed
    ``manifest.json`` fails closed with :class:`ToolExecutionError`, mirroring
    ``sources.py``'s fail-closed YAML handling.
    """
    if target_dir is None or not target_dir.is_dir():
        return {}
    manifest_path = target_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolExecutionError(f"Could not read {manifest_path}: {exc}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ToolExecutionError(f"Malformed dbt manifest at {manifest_path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ToolExecutionError(f"dbt manifest at {manifest_path} must be a JSON object.")
    return doc


def _nodes(manifest: dict[str, Any]) -> dict[str, Any]:
    nodes = manifest.get("nodes")
    return nodes if isinstance(nodes, dict) else {}


def _sources(manifest: dict[str, Any]) -> dict[str, Any]:
    sources = manifest.get("sources")
    return sources if isinstance(sources, dict) else {}


# ---------------------------------------------------------------------------
# op implementations
# ---------------------------------------------------------------------------


def _list_models(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``resource_type == "model"`` node, as a readable summary."""
    models: list[dict[str, Any]] = []
    for node_id, node in _nodes(manifest).items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        materialized = config.get("materialized") if isinstance(config, dict) else None
        models.append(
            {
                "name": node.get("name") or node_id,
                "unique_id": node_id,
                "path": node.get("original_file_path") or node.get("path"),
                "materialized": materialized or node.get("materialized"),
                "schema": node.get("schema"),
                "database": node.get("database"),
                "tags": list(node["tags"]) if isinstance(node.get("tags"), list) else [],
            }
        )
    models.sort(key=lambda m: str(m["name"]))
    return models


def _resolve_model_id(manifest: dict[str, Any], model: str) -> str | None:
    """Map a model name (or full unique_id) to its ``model.*`` unique_id."""
    nodes = _nodes(manifest)
    if model in nodes and _is_model(nodes[model]):
        return model
    for node_id, node in nodes.items():
        if _is_model(node) and node.get("name") == model:
            return node_id
    return None


def _is_model(node: Any) -> bool:
    return isinstance(node, dict) and node.get("resource_type") == "model"


def _model_columns(manifest: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    """The model's declared columns + the tests attached to each column."""
    node = _nodes(manifest).get(node_id)
    columns = node.get("columns") if isinstance(node, dict) else None
    if not isinstance(columns, dict):
        return []
    tests_by_column = _tests_by_column(manifest, node_id)
    out: list[dict[str, Any]] = []
    for col_name, col in columns.items():
        col = col if isinstance(col, dict) else {}
        out.append(
            {
                "name": col.get("name") or col_name,
                "description": col.get("description") or "",
                "data_type": col.get("data_type"),
                "tests": tests_by_column.get(col_name, []),
            }
        )
    return out


def _model_dependencies(manifest: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Upstream (``parent_map``) + downstream (``child_map``), names resolved.

    Models/sources are surfaced as upstream/downstream dependencies; ``test`` and
    other non-model/source children (e.g. ``unit_test``) are excluded — tests are
    reported by ``tests_on_model``, not as downstream dependencies.
    """
    parent_map = manifest.get("parent_map")
    child_map = manifest.get("child_map")
    parents = parent_map.get(node_id, []) if isinstance(parent_map, dict) else []
    children = child_map.get(node_id, []) if isinstance(child_map, dict) else []
    upstream = [_dep(manifest, p) for p in _as_id_list(parents)]
    downstream = [_dep(manifest, c) for c in _as_id_list(children)]
    return {
        "upstream": upstream,
        "downstream": [d for d in downstream if d["resource_type"] not in ("test", "unit_test")],
    }


def _tests_on_model(manifest: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    """The ``test`` nodes that depend on the model — name, kind, target column.

    dbt models a data test as a ``test.<pkg>.*`` node whose ``depends_on.nodes``
    references the tested model; the generic kind (``unique``/``not_null``/
    ``relationships``/``accepted_values``) and the targeted column live on the
    test node's ``test_metadata`` / ``column_name``.
    """
    tests: list[dict[str, Any]] = []
    for test_id, node in _nodes(manifest).items():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        depends_on = node.get("depends_on")
        dep_nodes = depends_on.get("nodes") if isinstance(depends_on, dict) else None
        if not isinstance(dep_nodes, list) or node_id not in dep_nodes:
            continue
        meta = node.get("test_metadata")
        kind = meta.get("name") if isinstance(meta, dict) else None
        tests.append(
            {
                "name": node.get("name") or test_id,
                "unique_id": test_id,
                "kind": kind,
                "column": node.get("column_name"),
            }
        )
    tests.sort(key=lambda t: str(t["name"]))
    return tests


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tests_by_column(manifest: dict[str, Any], model_id: str) -> dict[str, list[str]]:
    """Map column name → the kinds of test attached to it on this model."""
    by_column: dict[str, list[str]] = {}
    for test in _tests_on_model(manifest, model_id):
        column = test.get("column")
        if isinstance(column, str) and column:
            by_column.setdefault(column, []).append(str(test.get("kind") or test.get("name")))
    return by_column


def _as_id_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _dep(manifest: dict[str, Any], unique_id: str) -> dict[str, Any]:
    """Resolve a parent/child ``unique_id`` to a readable {name, resource_type}.

    Looks the id up in ``nodes`` then ``sources`` (a ``source.*`` parent is a real
    upstream). An unresolvable id is still surfaced (name derived from the id) so
    nothing silently vanishes.
    """
    node = _nodes(manifest).get(unique_id) or _sources(manifest).get(unique_id)
    resource_type = unique_id.split(".", 1)[0]
    if isinstance(node, dict):
        resource_type = node.get("resource_type") or resource_type
        name = node.get("name") or unique_id
    else:
        name = unique_id.rsplit(".", 1)[-1]
    return {"name": name, "unique_id": unique_id, "resource_type": resource_type}


__all__ = ["make_dbt_manifest_tool"]
