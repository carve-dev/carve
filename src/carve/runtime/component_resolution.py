"""Component-name -> code-path resolution for pipeline steps.

Thin wrappers over the shipped locator
(:mod:`carve.integrations.component_locator`) that the executor (Unit 2)
and ``load_pipeline``'s resolvability validation share, so the path math
lives in exactly one place. The executors do **no** path math themselves —
they ask here for a concrete directory.

* :func:`resolve_dlt_component` — a ``dlt`` step's required ``component``
  name -> ``el/<name>/`` (simple mode) or the workspace clone @ pinned ref
  (multi mode).
* :func:`resolve_dbt_component` — a ``dbt`` step's ``component``: a name
  resolves like a dlt one; an **omitted** name (``None``) resolves to the
  single detected dbt project (``ProjectPaths.dbt_project_path``-equivalent
  via the locator's detection).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from carve.core.config.schema import ComponentType
from carve.integrations.component_locator import (
    ComponentResolutionError,
    _detect_dbt_project,
    resolve_component,
)

if TYPE_CHECKING:
    from pathlib import Path

    from carve.core.config.paths import ProjectPaths
    from carve.core.config.schema import ComponentConfig
    from carve.integrations.component_locator import ResolvedComponent


def resolve_dlt_component(
    name: str,
    paths: ProjectPaths,
    *,
    components: dict[str, ComponentConfig],
) -> ResolvedComponent:
    """Resolve a ``dlt`` step's ``component`` name to its code location.

    Delegates to the shipped locator: simple mode returns ``el/<name>/``;
    multi mode returns the workspace clone at the component's pinned ref.
    Raises :class:`ComponentResolutionError` (and asserts the resolved
    component is in fact a ``dlt`` one — a ``dbt`` name on a ``dlt`` step
    is a composition error worth surfacing).
    """
    resolved = resolve_component(name, components=components, paths=paths)
    if resolved.type is not ComponentType.DLT:
        raise ComponentResolutionError(
            f"Component {name!r} is a {resolved.type.value} component, not dlt.",
            hint="A `dlt` step must reference a dlt component.",
        )
    return resolved


def resolve_dbt_component(
    name: str | None,
    paths: ProjectPaths,
    *,
    components: dict[str, ComponentConfig],
) -> ResolvedComponent:
    """Resolve a ``dbt`` step's ``component`` to its code location.

    A named component resolves like a dlt one (and must be a ``dbt``
    component). An **omitted** name (``None``) resolves to the single
    detected dbt project — the simple-mode convenience the schema permits
    (graduation backfills the name). Raises
    :class:`ComponentResolutionError` when the name doesn't resolve, isn't
    a dbt component, or no single dbt project can be detected.
    """
    if name is None:
        dbt_path: Path = _detect_dbt_project(paths, required=True)
        # The locator names a root project "dbt", else the dir name.
        detected_name = "dbt" if dbt_path == paths.root else dbt_path.name
        from carve.integrations.component_locator import ResolvedComponent

        return ResolvedComponent(detected_name, ComponentType.DBT, dbt_path, ref=None)

    resolved = resolve_component(name, components=components, paths=paths)
    if resolved.type is not ComponentType.DBT:
        raise ComponentResolutionError(
            f"Component {name!r} is a {resolved.type.value} component, not dbt.",
            hint="A `dbt` step must reference a dbt component (or omit `component` "
            "in simple mode to use the single detected dbt project).",
        )
    return resolved


__all__ = ["resolve_dbt_component", "resolve_dlt_component"]
