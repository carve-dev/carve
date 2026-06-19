"""Integrations: component resolution, workspace cache, provenance.

This package is the seam between Carve's control-plane config and the
typed components it references (dlt pipelines, dbt projects) wherever
they live — same repo, a local path, or a remote git repo.

Public surface:

* :func:`resolve_component` — the single choke point that maps a
  component *name* to its on-disk code path. No path math happens
  anywhere else.
* :func:`discover_components` — convention-based simple-mode discovery
  (``el/<name>/`` dlt dirs + the detected dbt project) for when
  ``carve.toml`` has no ``[components.*]`` blocks.
* :func:`sync_workspace` / :func:`is_dirty` / :func:`reject_if_dirty` —
  the git workspace cache primitives for ``separate-remote`` components.
* :func:`parse_provenance_header` — reads the Carve-generated dlt header
  comment block into structured metadata.
"""

from carve.integrations.component_locator import (
    ResolvedComponent,
    discover_components,
    resolve_component,
    slugify,
    workspace_dirname,
)
from carve.integrations.provenance import ProvenanceHeader, parse_provenance_header
from carve.integrations.workspace_cache import (
    WorkspaceDirtyError,
    WorkspaceSyncError,
    is_dirty,
    reject_if_dirty,
    sync_workspace,
)

__all__ = [
    "ProvenanceHeader",
    "ResolvedComponent",
    "WorkspaceDirtyError",
    "WorkspaceSyncError",
    "discover_components",
    "is_dirty",
    "parse_provenance_header",
    "reject_if_dirty",
    "resolve_component",
    "slugify",
    "sync_workspace",
    "workspace_dirname",
]
