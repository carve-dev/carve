"""Skill packs — folder-format capability packs (``SKILL.md`` + bundle).

A **SkillPack** is a folder::

    <skills_dir>/<name>/
        SKILL.md              # frontmatter + instructions
        scripts/  (optional)  # bundled code (e.g. a dlt source)
        resources/ (optional) # bundled data

The ``SKILL.md`` frontmatter declares ``name`` / ``description`` /
``expects_env`` and the body is free-text instructions (how to use the
bundle, conventions). A pack surfaces as **description-matched content
injected into the agent's context** — it is *not* a callable tool (see
``pack_discovery``). The connector library is a skill library: a curated
``src/carve/sources/<name>/`` ships as one of these.

Security (the threat is RCE-on-discovery):

* The frontmatter is parsed with the **same safe loader** the agent
  format uses (:func:`carve.core.agents.loader.read_frontmatter_file` —
  ``yaml.safe_load``, byte-capped). A malformed/oversized ``SKILL.md``
  **fails the load** (``SkillPackError``), no partial register.
* **Bundled ``scripts/``/``resources/`` are never executed or imported at
  load.** Only their *paths* are recorded (the agent may later run them
  via the gated ``bash`` tool). Loading a pack is side-effect-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carve.core.agents.loader import (
    MAX_AGENT_FILE_BYTES,
    AgentLoadError,
    read_frontmatter_file,
)

# A SKILL.md is a prompt like an agent file; reuse the one byte threshold.
MAX_SKILL_FILE_BYTES = MAX_AGENT_FILE_BYTES

SKILL_FILENAME = "SKILL.md"


class SkillPackError(Exception):
    """Raised when a skill pack cannot be loaded (fail-closed, no partial)."""


@dataclass(frozen=True)
class SkillPack:
    """A loaded skill pack — frontmatter + instructions + inert bundle paths.

    * ``name`` / ``description`` — required; the description drives the
      discovery match.
    * ``expects_env`` — env-var names the pack's bundled code expects (the
      agent is told to ensure they're set; the loader does not read them).
    * ``instructions`` — the ``SKILL.md`` body; the content injected on a
      description-match.
    * ``directory`` — the pack folder.
    * ``script_paths`` / ``resource_paths`` — **inert** paths to the
      bundled files, recorded but never executed at load.
    """

    name: str
    description: str
    instructions: str
    directory: Path
    expects_env: tuple[str, ...] = ()
    script_paths: tuple[Path, ...] = ()
    resource_paths: tuple[Path, ...] = ()


def load_skill_pack(directory: Path) -> SkillPack:
    """Load the skill pack rooted at ``directory`` (safe, fail-closed).

    Reads ``<directory>/SKILL.md`` via the safe frontmatter loader,
    validates the required fields, and records the inert bundle paths
    under ``scripts/`` and ``resources/`` **without reading or running
    them**. Raises :class:`SkillPackError` on any problem.
    """
    directory = directory.resolve()
    skill_md = directory / SKILL_FILENAME
    if not skill_md.is_file():
        raise SkillPackError(f"Skill pack {directory} has no {SKILL_FILENAME}.")

    try:
        frontmatter, body = read_frontmatter_file(skill_md, max_bytes=MAX_SKILL_FILE_BYTES)
    except AgentLoadError as exc:
        # Re-wrap so callers catch one pack-specific error type.
        raise SkillPackError(str(exc)) from exc

    name = _require_str(frontmatter, "name", skill_md)
    description = _require_str(frontmatter, "description", skill_md)
    expects_env = _str_list(frontmatter, "expects_env", skill_md)

    return SkillPack(
        name=name,
        description=description,
        instructions=body,
        directory=directory,
        expects_env=expects_env,
        script_paths=_inert_paths(directory / "scripts"),
        resource_paths=_inert_paths(directory / "resources"),
    )


def _inert_paths(subdir: Path) -> tuple[Path, ...]:
    """Record (never read/run) the files under ``subdir``.

    Returns a sorted tuple of regular-file paths if ``subdir`` exists, else
    an empty tuple. This is the *only* contact the loader has with bundled
    code — a directory listing, no read, no import, no exec.
    """
    if not subdir.is_dir():
        return ()
    return tuple(sorted(p.resolve() for p in subdir.rglob("*") if p.is_file()))


def _require_str(frontmatter: dict[str, Any], key: str, path: Path) -> str:
    value = frontmatter.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillPackError(
            f"Skill pack {path}: '{key}' is required and must be a non-empty string."
        )
    return value.strip()


def _str_list(frontmatter: dict[str, Any], key: str, path: Path) -> tuple[str, ...]:
    if key not in frontmatter or frontmatter[key] is None:
        return ()
    value = frontmatter[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SkillPackError(f"Skill pack {path}: '{key}' must be a list of strings.")
    return tuple(item.strip() for item in value)


__all__ = [
    "MAX_SKILL_FILE_BYTES",
    "SKILL_FILENAME",
    "SkillPack",
    "SkillPackError",
    "load_skill_pack",
]
