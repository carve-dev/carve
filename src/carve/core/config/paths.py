"""Fixed control-plane paths — the flat-layout dirs resolved from a root.

`ProjectPaths` is the resolved-paths value object the component locator,
the workspace cache, and (later) the runtime use. It is a *frozen*
dataclass of absolute directory paths, derived once from the
control-plane root (where `carve.toml` lives).

This is distinct from `carve.core.config.schema.PathsConfig`, which is a
pydantic *config section* of configurable dir *names* (`config_dir`,
`targets_dir`, …) for the legacy target surface. The two coexist:
`PathsConfig` is the configurable-names surface; `ProjectPaths` is the
fixed flat-layout dirs (`el/`, `pipelines/`, `.carve/`, `.dlt/`) the
control-plane model locks. Don't conflate them.

The directories named here are conventions, not guarantees that they
exist on disk — `from_root` does no I/O. Callers that need a directory
to exist create it (init) or check for it (the locator's discovery).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    """The fixed control-plane directory paths for one project root.

    Every path is absolute. Attributes mirror the canonical layout
    (spec *Path resolution*):

    * ``root`` — control-plane root (where ``carve.toml`` lives).
    * ``carve_dir`` — ``<root>/carve/`` (carve's own project state).
    * ``el_dir`` — ``<root>/el/`` (dlt components, one dir each).
    * ``pipelines_dir`` — ``<root>/pipelines/`` (multi-step composition).
    * ``scratch_dir`` — ``<root>/.carve/`` (runtime scratch + cache,
      gitignored; the workspace cache lives at
      ``scratch_dir / "workspaces"``).
    * ``dlt_config_dir`` — ``<root>/.dlt/`` (dlt's own config dir).
    """

    root: Path
    carve_dir: Path
    el_dir: Path
    pipelines_dir: Path
    scratch_dir: Path
    dlt_config_dir: Path

    @classmethod
    def from_root(cls, root: Path) -> ProjectPaths:
        """Derive the fixed control-plane paths from a project ``root``.

        ``root`` is resolved to an absolute path so every derived dir is
        absolute regardless of the caller's cwd. No directories are
        created or checked — this is pure path math.
        """
        root = root.resolve()
        return cls(
            root=root,
            carve_dir=root / "carve",
            el_dir=root / "el",
            pipelines_dir=root / "pipelines",
            scratch_dir=root / ".carve",
            dlt_config_dir=root / ".dlt",
        )

    @property
    def workspaces_dir(self) -> Path:
        """``<root>/.carve/workspaces/`` — the separate-remote clone cache."""
        return self.scratch_dir / "workspaces"


__all__ = ["ProjectPaths"]
