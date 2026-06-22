"""Name -> code-path resolution for typed components.

This module is the **single place path math happens** for components.
Every runtime call site (the ``dlt``/``dbt`` step executors, the EL and
dbt agents' file-write targets, the manifest/sources readers) resolves a
component's code through :func:`resolve_component` rather than joining
paths itself. Keeping it here means the four resolution rules — and the
``ref``-over-``branch`` precedence — are encoded once.

A component is referenced **by name**. In *multi mode* the name keys a
``[components.<name>]`` block in ``carve.toml``; in *simple mode* (no
blocks) the name is discovered by convention from the on-disk layout
(see :func:`discover_components`). The block's (or convention's) ``type``
tells callers how to run it; ``mode`` (or convention) tells the locator
how to find it.

Resolution is **pure and side-effect-free**: for ``separate-remote``
components it returns the workspace-cache path the clone *will* live at,
but does not clone or sync. The sync triggers (``carve serve`` startup,
before each pipeline run, before ``carve deploy``) are owned by their
capabilities and call :func:`carve.integrations.workspace_cache.sync_workspace`
against the same derived path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

from carve.core.config.schema import ComponentConfig, ComponentMode, ComponentType

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths


class ComponentResolutionError(Exception):
    """Raised when a component name cannot be resolved to a code path.

    Carries a one-line ``message`` plus an optional ``hint`` so the CLI
    can render an actionable error. Distinct from ``ConfigError`` —
    resolution failures are a runtime/startup concern, not a config-parse
    one (the block parsed fine; the *path* it points at is the problem).
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(message if hint is None else f"{message}\n  Hint: {hint}")


@dataclass(frozen=True)
class ResolvedComponent:
    """A component resolved to its on-disk location.

    * ``name`` — the component name (the ``[components.<name>]`` key or
      the convention-discovered name).
    * ``type`` — ``ComponentType.DLT`` or ``ComponentType.DBT``; tells
      callers how to run it.
    * ``code_path`` — absolute path to the component's code. For
      ``same-repo`` dlt this is ``<root>/el/<name>/``; for ``same-repo``
      dbt it is the dir containing ``dbt_project.yml``; for
      ``separate-local`` the recorded ``path``; for ``separate-remote``
      the workspace-cache dir the clone lives at.
    * ``ref`` — the pinned revision (commit SHA or tag) for a pinned
      ``separate-remote`` component, else ``None`` (branch-tracking,
      same-repo, separate-local, and convention components are never
      pinned).
    """

    name: str
    type: ComponentType
    code_path: Path
    ref: str | None


# Characters kept verbatim in a slug; everything else collapses to `-`.
_SLUG_KEEP_RE = re.compile(r"[^a-z0-9]+")
# Strip a leading scheme (`https://`, `git@`, `ssh://git@`) and a trailing
# `.git` so equivalent URLs slugify to the same stable stem.
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://")


def slugify(url: str) -> str:
    """Return a stable, filesystem-safe slug for a git URL.

    The slug is collision-resistant across the URL shapes that denote the
    *same* repo: ``https://host/org/repo.git`` and
    ``git@host:org/repo.git`` and ``ssh://git@host:22/org/repo`` all
    reduce to ``host-org-repo``. It strips the scheme / ``git@`` userinfo,
    the ``.git`` suffix, and any port, then lowercases and replaces every
    run of non-``[a-z0-9]`` characters with a single ``-``.

    Distinct repos stay distinct: the host, org, and repo path segments
    are all retained, so two repos with the same name under different
    orgs or hosts get different slugs.
    """
    text = url.strip().lower()
    text = _SCHEME_RE.sub("", text)  # drop scheme://
    # Drop `user@` userinfo (e.g. `git@github.com:...`). Only the part
    # before the first `/` or `:` can carry it, so this is safe.
    if "@" in text:
        text = text.split("@", 1)[1]
    # Strip a `:port` that follows the host (digits between `:` and `/`),
    # so ssh URLs with explicit ports don't fork the slug. The scp-style
    # `host:org/repo` colon (non-numeric) is left for the generic
    # separator pass below.
    text = re.sub(r":(\d+)(?=/)", "/", text)
    if text.endswith(".git"):
        text = text[: -len(".git")]
    slug = _SLUG_KEEP_RE.sub("-", text).strip("-")
    return slug


def workspace_dirname(url: str, ref: str | None, branch: str | None) -> str:
    """Derive the workspace-cache directory name for a remote component.

    The name is ``slugify(url) + "-" + (ref or branch)`` per the spec's
    *Path resolution*. The ``ref``-over-``branch`` precedence is applied
    here so the cache dir is keyed by the exact revision selector in
    effect — and so resolution (the locator) and caching (the workspace
    cache's ``sync_workspace``) always agree on where a component lands.
    Falls back to ``default`` when neither ``ref`` nor ``branch`` is set
    (tracking the remote's default branch HEAD).
    """
    selector = ref or branch or "default"
    return f"{slugify(url)}-{_slug_segment(selector)}"


def _slug_segment(value: str) -> str:
    """Slugify a single ref/branch segment (keep it filesystem-safe)."""
    return _SLUG_KEEP_RE.sub("-", value.strip().lower()).strip("-") or "default"


def resolve_component(
    name: str,
    *,
    components: dict[str, ComponentConfig],
    paths: ProjectPaths,
) -> ResolvedComponent:
    """Resolve a component name to its on-disk code path.

    If ``name`` keys a ``[components.<name>]`` block, resolution follows
    that block's ``mode``. Otherwise the component is resolved by
    convention (simple mode): an ``el/<name>/`` dir is a ``dlt``
    component; the detected dbt project resolves as the lone ``dbt``
    component. Raises :class:`ComponentResolutionError` if the name
    matches neither a block nor a convention-discovered component, or if
    the resolved path is required-but-missing.

    Pure: ``separate-remote`` returns the workspace-cache path the clone
    *will* live at without cloning. Callers sync via the workspace cache.
    """
    block = components.get(name)
    if block is not None:
        return _resolve_block(name, block, paths)
    return _resolve_convention(name, paths)


# ---------------------------------------------------------------------------
# Block-driven resolution (multi mode)
# ---------------------------------------------------------------------------


def _resolve_block(
    name: str,
    block: ComponentConfig,
    paths: ProjectPaths,
) -> ResolvedComponent:
    if block.mode is ComponentMode.SAME_REPO:
        if block.type is ComponentType.DLT:
            return ResolvedComponent(name, block.type, paths.el_dir / name, ref=None)
        # same-repo dbt: detect the project dir (root or one level down).
        return ResolvedComponent(name, block.type, _detect_dbt_project(paths), ref=None)

    if block.mode is ComponentMode.SEPARATE_LOCAL:
        assert block.path is not None  # schema guarantees this
        code_path = Path(block.path).expanduser()
        if not code_path.exists():
            raise ComponentResolutionError(
                f"Component {name!r} (separate-local) path does not exist: {code_path}",
                hint="Fix the `path` in the [components."
                f"{name}] block, or check the directory is present.",
            )
        return ResolvedComponent(name, block.type, code_path, ref=None)

    # separate-remote: workspace-cache path; ref pins, else branch tracks.
    assert block.url is not None  # schema guarantees this
    workspace_path = paths.workspaces_dir / workspace_dirname(block.url, block.ref, block.branch)
    return ResolvedComponent(name, block.type, workspace_path, ref=block.ref)


# ---------------------------------------------------------------------------
# Convention-driven resolution (simple mode)
# ---------------------------------------------------------------------------


def _resolve_convention(name: str, paths: ProjectPaths) -> ResolvedComponent:
    """Resolve a name with no block, by the simple-mode conventions."""
    el_path = paths.el_dir / name
    if el_path.is_dir():
        return ResolvedComponent(name, ComponentType.DLT, el_path, ref=None)

    # Maybe it's the convention dbt component. Detect the project and
    # check the name matches the dbt project's conventional name (the dir
    # containing dbt_project.yml, or "dbt" when it's at the root).
    dbt_path = _detect_dbt_project(paths, required=False)
    if dbt_path is not None and name == _dbt_component_name(dbt_path, paths):
        return ResolvedComponent(name, ComponentType.DBT, dbt_path, ref=None)

    raise ComponentResolutionError(
        f"No component named {name!r}: no [components.{name}] block, no "
        f"el/{name}/ directory, and it is not the detected dbt project.",
        hint="Add a [components."
        f"{name}] block in carve.toml, or create el/{name}/ for a dlt component.",
    )


def discover_components(paths: ProjectPaths) -> list[ResolvedComponent]:
    """Enumerate the convention-discovered components for simple mode.

    Returns one :class:`ResolvedComponent` per ``el/<name>/`` directory
    (as ``dlt`` components) plus the detected dbt project (as a single
    ``dbt`` component) if one is present. This is the implicit set that
    ``carve components show`` (pipelines, spec 08) surfaces and that
    runtime callers iterate when no ``[components.*]`` blocks are
    written. Sorted by name for stable output. Never raises on a missing
    dbt project — discovery is best-effort.
    """
    found: list[ResolvedComponent] = []
    if paths.el_dir.is_dir():
        for child in sorted(paths.el_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                found.append(ResolvedComponent(child.name, ComponentType.DLT, child, ref=None))

    dbt_path = _detect_dbt_project(paths, required=False)
    if dbt_path is not None:
        found.append(
            ResolvedComponent(
                _dbt_component_name(dbt_path, paths),
                ComponentType.DBT,
                dbt_path,
                ref=None,
            )
        )
    return found


# ---------------------------------------------------------------------------
# dbt project detection (root + one level down)
# ---------------------------------------------------------------------------


@overload
def _detect_dbt_project(paths: ProjectPaths, *, required: Literal[True] = ...) -> Path: ...


@overload
def _detect_dbt_project(paths: ProjectPaths, *, required: Literal[False]) -> Path | None: ...


def _detect_dbt_project(
    paths: ProjectPaths,
    *,
    required: bool = True,
) -> Path | None:
    """Find the same-repo dbt project: ``<root>/dbt_project.yml`` then
    ``<root>/*/dbt_project.yml`` (one level down only).

    Exactly one match → its containing dir. Zero → ``None`` (brownfield
    dbt absent) when ``required`` is ``False``, else a structured error.
    Multiple → always a structured error listing the candidates (the user
    must pin one via a ``[components.<name>]`` block). Discovery looks at
    root and one level down only, per the spec's *Open questions*.
    """
    candidates: list[Path] = []
    root_marker = paths.root / "dbt_project.yml"
    if root_marker.is_file():
        candidates.append(paths.root)
    for child in sorted(paths.root.iterdir()) if paths.root.is_dir() else []:
        # Skip the control-plane dirs and any dotted dirs — a dbt project
        # never lives in `.carve/`, `el/`, etc. Looking one level down
        # means we check `<root>/<child>/dbt_project.yml`.
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in ("el", "pipelines", "carve"):
            continue
        if (child / "dbt_project.yml").is_file():
            candidates.append(child)

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        if required:
            raise ComponentResolutionError(
                "No dbt project found (looked for dbt_project.yml at the root and one level down).",
                hint="Run `carve init --with-dbt` to scaffold one, or add a "
                "[components.<name>] block (type='dbt', mode='separate-local').",
            )
        return None
    listing = ", ".join(str(c / "dbt_project.yml") for c in candidates)
    raise ComponentResolutionError(
        f"Multiple dbt projects found: {listing}",
        hint="Pin one with a [components.<name>] block "
        "(type='dbt', mode='separate-local', path=...).",
    )


def _dbt_component_name(dbt_path: Path, paths: ProjectPaths) -> str:
    """The conventional name for the detected dbt component.

    A dbt project at the control-plane root is named ``dbt`` (there is no
    directory to borrow a name from); a project one level down takes its
    containing directory's name.
    """
    if dbt_path == paths.root:
        return "dbt"
    return dbt_path.name


__all__ = [
    "ComponentResolutionError",
    "ResolvedComponent",
    "discover_components",
    "resolve_component",
    "slugify",
    "workspace_dirname",
]
