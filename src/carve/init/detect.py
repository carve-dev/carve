"""Brownfield + environment detection for `carve init`.

Inspects a directory for existing dbt / dlt projects and the surrounding
environment (git, docker, prior `carve init`). Detection is read-only and
never executes project code — dlt detection parses Python via :mod:`ast`.
"""

from __future__ import annotations

import ast
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Detection:
    """What `carve init` found in the target directory (read-only)."""

    root: Path
    re_init: bool  # an existing carve.toml — idempotent re-init path
    dbt_projects: tuple[Path, ...]  # dbt_project.yml paths (root + one level down)
    dlt_present: bool  # .dlt/ or dlt-importing Python under el/ or the root
    has_git: bool
    has_docker: bool


def detect(root: Path) -> Detection:
    """Inspect ``root`` and return a :class:`Detection`."""
    return Detection(
        root=root,
        re_init=(root / "carve.toml").is_file(),
        dbt_projects=_find_dbt_projects(root),
        dlt_present=_detect_dlt(root),
        has_git=(root / ".git").exists(),
        has_docker=shutil.which("docker") is not None,
    )


def _find_dbt_projects(root: Path) -> tuple[Path, ...]:
    """Find ``dbt_project.yml`` at the root and one level down (per layout)."""
    found: list[Path] = []
    top = root / "dbt_project.yml"
    if top.is_file():
        found.append(top)
    if root.is_dir():
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if child.name.startswith("."):
                continue
            candidate = child / "dbt_project.yml"
            if candidate.is_file():
                found.append(candidate)
    return tuple(found)


def _detect_dlt(root: Path) -> bool:
    """True if the directory holds dlt code (``.dlt/`` or dlt-importing Python)."""
    if (root / ".dlt").is_dir():
        return True
    el = root / "el"
    if el.is_dir():
        for init in el.glob("*/__init__.py"):
            if _imports_dlt(init):
                return True
    for py in root.glob("*.py"):
        if _imports_dlt(py):
            return True
    return False


def _imports_dlt(path: Path) -> bool:
    """True if the Python file imports ``dlt`` (AST parse; never executed)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "dlt" or alias.name.startswith("dlt.") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "dlt" or module.startswith("dlt."):
                return True
    return False
