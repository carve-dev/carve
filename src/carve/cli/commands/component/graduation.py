"""Component graduation: write/remove the ``[components.<name>]`` block.

Graduation moves a component from simple-mode convention (an ``el/<name>/``
dir or the detected dbt project) into its own repo, a one-command control-plane
edit that touches ``carve.toml`` (and backfills omitted dbt-step names) but
**not** the pipeline steps' wiring. This module owns the pure, testable core:

* :func:`infer_component_type` — ``el/<name>/`` -> ``dlt``; the detected dbt
  project -> ``dbt`` (via the shipped locator).
* :func:`write_component_block` — write the ``[components.<name>]`` block into
  ``carve.toml`` via ``tomlkit`` (comment-preserving, mirroring
  ``dbt_execution.engine.pin_engine``).
* :func:`remove_component_block` — the ``--same-repo`` reverse: drop the block.
* :func:`backfill_dbt_step_components` — write ``component = "<name>"`` into any
  ``dbt`` step in ``pipelines/*.toml`` that omitted it (also tomlkit).

The CLI (``component/__init__.py``) composes these with the **shipped**
``workspace_cache.sync_workspace`` (clone) + ``component_locator.resolve_component``
(validate) — those side-effecting calls stay in the command so this module is
filesystem-pure-by-injection and offline-testable.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import tomlkit

from carve.core.config.schema import ComponentMode, ComponentType
from carve.integrations.component_locator import _detect_dbt_project

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths


class GraduationError(Exception):
    """Raised when a component cannot be graduated (or reversed).

    Carries a one-line ``message`` plus an optional ``hint`` so the CLI renders
    an actionable error (the name doesn't exist, the block is already present,
    etc.).
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(message if hint is None else f"{message}\n  Hint: {hint}")


def infer_component_type(name: str, paths: ProjectPaths) -> ComponentType:
    """Infer a component's ``type`` from the simple-mode convention.

    An ``el/<name>/`` directory is a ``dlt`` component; otherwise, if ``name``
    matches the detected dbt project's conventional name, it's a ``dbt``
    component. Raises :class:`GraduationError` if the name matches neither —
    graduation only moves a component that already exists by convention.
    """
    el_path = paths.el_dir / name
    if el_path.is_dir():
        return ComponentType.DLT

    dbt_path = _detect_dbt_project(paths, required=False)
    if dbt_path is not None:
        dbt_name = dbt_path.name if dbt_path != paths.root else "dbt"
        if name == dbt_name:
            return ComponentType.DBT

    raise GraduationError(
        f"No convention-discovered component named {name!r}: no el/{name}/ directory, "
        "and it is not the detected dbt project.",
        hint="Graduation moves an existing simple-mode component into its own repo; "
        "author the component first (e.g. `carve plan 'ingest X'`).",
    )


def write_component_block(
    name: str,
    *,
    config_path: Path,
    component_type: ComponentType,
    mode: ComponentMode,
    url: str | None = None,
    ref: str | None = None,
    branch: str | None = None,
    path: str | None = None,
) -> None:
    """Write the ``[components.<name>]`` block into ``carve.toml`` (tomlkit).

    Comment-preserving round-trip, mirroring
    :func:`carve.core.dbt_execution.engine.pin_engine`. Only the fields
    meaningful for ``mode`` are written (``url``/``ref``/``branch`` for
    separate-remote, ``path`` for separate-local), so the block round-trips back
    through :class:`carve.core.config.schema.ComponentConfig`'s cross-field
    validation. Raises :class:`GraduationError` if the block already exists.
    """
    text = config_path.read_text(encoding="utf-8")
    doc = tomlkit.parse(text)

    components = doc.get("components")
    if components is None:
        components = tomlkit.table(is_super_table=True)
        doc["components"] = components
    if not isinstance(components, dict):
        raise GraduationError(
            f"[components] in {config_path} is not a table; cannot graduate {name!r}."
        )
    if name in components:
        raise GraduationError(
            f"[components.{name}] already exists in {config_path}.",
            hint="The component is already graduated; use `--same-repo` to reverse it.",
        )

    block = tomlkit.table()
    block["type"] = component_type.value
    block["mode"] = mode.value
    if mode is ComponentMode.SEPARATE_REMOTE:
        assert url is not None  # guaranteed by the command's flag validation
        block["url"] = url
        if ref is not None:
            block["ref"] = ref
        if branch is not None:
            block["branch"] = branch
    elif mode is ComponentMode.SEPARATE_LOCAL:
        assert path is not None
        block["path"] = path

    components[name] = block
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def remove_component_block(name: str, *, config_path: Path) -> None:
    """Remove the ``[components.<name>]`` block from ``carve.toml`` (``--same-repo``).

    The reverse of :func:`write_component_block`: drops the block so the name
    resolves back to its simple-mode convention. Comment-preserving. Raises
    :class:`GraduationError` if the block isn't present.
    """
    text = config_path.read_text(encoding="utf-8")
    doc = tomlkit.parse(text)

    components = doc.get("components")
    if not isinstance(components, dict) or name not in components:
        raise GraduationError(
            f"[components.{name}] not found in {config_path}; nothing to reverse.",
            hint="The component is not graduated (it already resolves by convention).",
        )
    del components[name]
    # Drop a now-empty [components] table so carve.toml stays clean.
    if isinstance(components, dict) and len(components) == 0:
        del doc["components"]
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def backfill_dbt_step_components(name: str, *, pipelines_dir: Path) -> list[str]:
    """Backfill ``component = "<name>"`` into omitting ``dbt`` steps.

    A graduated dbt component must be named explicitly (the omitted-``component``
    convenience only works in simple mode). This writes ``component = "<name>"``
    into any ``dbt`` step across ``pipelines/*.toml`` that omitted it — via
    ``tomlkit`` so comments/formatting survive. Returns the list of
    ``"<pipeline>:<step_id>"`` it backfilled (empty when nothing needed it).

    Only ever fills a *missing* ``component``; a dbt step that already names a
    component is left untouched (it might reference a different one).
    """
    backfilled: list[str] = []
    if not pipelines_dir.is_dir():
        return backfilled

    for toml_path in sorted(pipelines_dir.glob("*.toml")):
        if toml_path.name.startswith("."):
            continue
        try:
            text = toml_path.read_text(encoding="utf-8")
            doc = tomlkit.parse(text)
        except (OSError, tomlkit.exceptions.TOMLKitError):
            # A malformed (or unreadable) pipeline file must not abort graduation
            # after carve.toml is already mutated — skip it (it is surfaced by
            # `carve pipelines validate`), mirroring `_steps_referencing`.
            continue
        steps = doc.get("steps")
        if not isinstance(steps, list):
            continue
        changed = False
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("type") == "dbt" and "component" not in step:
                step["component"] = name
                backfilled.append(f"{toml_path.stem}:{step.get('id', '?')}")
                changed = True
        if changed:
            toml_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    return backfilled


__all__ = [
    "GraduationError",
    "backfill_dbt_step_components",
    "infer_component_type",
    "remove_component_block",
    "write_component_block",
]
