"""Skill-pack discovery + description-match content injection.

Follows a progressive-disclosure pattern:

* packs are discovered up front (name + description only),
* their instructions are **read from disk on demand** and stay **inert**
  until requested,
* the requestable name set is an **allowlist** — an unknown name raises
  before any read.

The crucial difference from ``skills/registry.py`` (the callable-``@skill``
registry): a pack is **content, not a callable tool**. On a
description-match the pack's instructions are *injected into the agent's
context* via the ``lookup_skill_pack`` tool, keeping the loop's flat
tool/skill namespace clean (packs never enter it).

Discovery is mtime-cached (same ``(mtime, parsed)`` pattern as agent
discovery), keyed on the ``SKILL.md`` mtime so an edited pack is re-read
at the next discovery, never mid-conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.skills.packs import (
    SKILL_FILENAME,
    SkillPack,
    SkillPackError,
    load_skill_pack,
)

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    mtime_ns: int
    pack: SkillPack


@dataclass(frozen=True)
class PackMatch:
    """One description-match hit: the pack name + its description."""

    name: str
    description: str


class SkillPackLibrary:
    """Discovers skill packs under a directory and serves them on demand.

    Construct with the user ``skills_dir`` (and optional extra roots, e.g.
    ``src/carve/sources``); call :meth:`discover` to enumerate, :meth:`match`
    to find description hits, and :meth:`make_lookup_tool` to build the
    content-injection tool the loop exposes.
    """

    def __init__(self, roots: list[Path]) -> None:
        self._roots = [r.resolve() for r in roots]
        self._cache: dict[Path, _CacheEntry] = {}

    def discover(self) -> list[SkillPack]:
        """Enumerate every loadable pack across the roots (mtime-cached).

        A pack folder is any direct subdirectory containing a ``SKILL.md``.
        A pack that fails to load is logged and skipped (one bad pack does
        not break discovery); a name collision across roots logs and the
        first-seen wins (deterministic by root order then sorted name).
        """
        seen: dict[str, SkillPack] = {}
        for root in self._roots:
            if not root.is_dir():
                continue
            for skill_md in sorted(root.glob(f"*/{SKILL_FILENAME}")):
                pack = self._load_cached(skill_md.parent)
                if pack is None:
                    continue
                if pack.name in seen:
                    logger.info(
                        "Skill pack %r at %s shadowed by an earlier root; keeping the first.",
                        pack.name,
                        pack.directory,
                    )
                    continue
                seen[pack.name] = pack
        return [seen[name] for name in sorted(seen)]

    def match(self, query: str) -> list[PackMatch]:
        """Return packs whose name/description match ``query`` (substring).

        The simple description-match the spec's open question calls for
        (an embedding index is a later increment). Case-insensitive
        substring over name + description; empty query matches nothing.
        """
        needle = query.strip().lower()
        if not needle:
            return []
        out: list[PackMatch] = []
        for pack in self.discover():
            haystack = f"{pack.name}\n{pack.description}".lower()
            if needle in haystack:
                out.append(PackMatch(name=pack.name, description=pack.description))
        return out

    def make_lookup_tool(self) -> Tool:
        """Build the ``lookup_skill_pack`` content-injection tool.

        The executor validates the requested name against the **current**
        discovered-pack allowlist (re-discovered per call so an edited
        library is picked up), then reads + returns that pack's
        instructions. An unknown name raises — names are never trusted
        blind. The instructions are read on demand and were inert on disk
        until this call.
        """

        def _execute(input_: ToolInput) -> ToolResult:
            name = input_.get("pack_name")
            if not isinstance(name, str) or not name:
                raise ToolExecutionError("`pack_name` must be a non-empty string.")
            by_name = {p.name: p for p in self.discover()}
            if name not in by_name:
                raise ToolExecutionError(
                    f"Unknown skill pack {name!r}. Available: {sorted(by_name)}"
                )
            return _render_pack(by_name[name])

        return Tool(
            name="lookup_skill_pack",
            description=(
                "Inject a named skill pack's instructions into the "
                "conversation. Skill packs are curated capability bundles "
                "(e.g. a connector source + its conventions); consult one "
                "when a task matches its description. Loads content on "
                "demand — do not call it speculatively."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pack_name": {
                        "type": "string",
                        "description": "The name of the skill pack to load.",
                    }
                },
                "required": ["pack_name"],
            },
            executor=_execute,
        )

    def _load_cached(self, directory: Path) -> SkillPack | None:
        """Load the pack at ``directory``, re-reading only on SKILL.md change."""
        directory = directory.resolve()
        skill_md = directory / SKILL_FILENAME
        try:
            mtime_ns = skill_md.stat().st_mtime_ns
        except OSError as exc:
            logger.warning("Cannot stat %s: %s", skill_md, exc)
            self._cache.pop(directory, None)
            return None

        cached = self._cache.get(directory)
        if cached is not None and cached.mtime_ns == mtime_ns:
            return cached.pack

        try:
            pack = load_skill_pack(directory)
        except SkillPackError as exc:
            logger.warning("Skipping skill pack %s: %s", directory, exc)
            self._cache.pop(directory, None)
            return None

        self._cache[directory] = _CacheEntry(mtime_ns=mtime_ns, pack=pack)
        return pack


def _render_pack(pack: SkillPack) -> str:
    """Render a pack's injected content (instructions + bundle pointers).

    The bundle paths are listed as *pointers* (the agent runs them via the
    gated ``bash`` tool); they are never inlined or executed here.
    """
    lines: list[str] = [f"# Skill pack: {pack.name}", "", pack.description, ""]
    if pack.expects_env:
        lines.append(f"Expects env: {', '.join(pack.expects_env)}")
        lines.append("")
    lines.append(pack.instructions)
    if pack.script_paths:
        lines.append("")
        lines.append("## Bundled scripts (run via the gated bash tool)")
        for path in pack.script_paths:
            lines.append(f"- {path}")
    return "\n".join(lines).rstrip() + "\n"


def discover_pack_roots(
    *,
    skills_dir: Path,
    extra_roots: list[Path] | None = None,
) -> SkillPackLibrary:
    """Build a :class:`SkillPackLibrary` for a project's ``skills_dir``.

    ``skills_dir`` is the resolved ``<root>/<PathsConfig.skills_dir>``;
    ``extra_roots`` lets the connector library (``src/carve/sources``) be
    added when those packs ship (a later spec). User packs take precedence
    (listed first), matching the agent-discovery user-over-builtin shape.
    """
    roots = [skills_dir]
    if extra_roots:
        roots.extend(extra_roots)
    return SkillPackLibrary(roots)


__all__ = [
    "PackMatch",
    "SkillPackLibrary",
    "discover_pack_roots",
]
