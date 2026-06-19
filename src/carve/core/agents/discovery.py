"""Agent discovery + the ``(mtime, parsed)`` cache.

Two discovery roots, built-in first then user:

1. **Built-ins** — ``src/carve/core/agents/builtin/*.md`` (ship with
   Carve).
2. **User** — ``<root>/<PathsConfig.agents_dir>/*.md`` (override built-ins
   by name).

A user file **overrides** a built-in of the same name (logged); a
**duplicate within one root** is an error (mirrors ``skills/registry.py``
collision discipline, applied per-root in
``SubagentRegistry.register_files``).

**Hot-reload at dispatch time only.** :class:`AgentDiscovery` caches
``(mtime, AgentFile)`` per path and re-parses a file *only* when its mtime
changed — and only when :meth:`build_registry` is called (the orchestrator
calls it right before it builds a ``SubagentRunner`` to ``delegate``).
Nothing re-reads mid-conversation. This is the spec-06 mtime-cache
*pattern*, implemented here to be shared/refactored when ``memory``
(Increment 2) lands its own cache.

Loading is inert: parsing reads only the ``.md`` text via the safe
:func:`load_agent_file`; no bundled script/resource is executed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from carve.core.agents.loader import AgentFile, AgentLoadError, load_agent_file
from carve.core.agents.subagent_registry import SubagentRegistry

logger = logging.getLogger(__name__)

# Where built-in agent .md files live (a sibling subpackage of this module).
BUILTIN_AGENTS_DIR = Path(__file__).resolve().parent / "builtin"


@dataclass
class _CacheEntry:
    """One cached parse: the file's mtime_ns at parse time + the result."""

    mtime_ns: int
    parsed: AgentFile


@dataclass(frozen=True)
class AgentRoot:
    """One discovery root: a directory and a human label for diagnostics."""

    directory: Path
    label: str


class AgentDiscovery:
    """Discovers + caches declarative agents across the discovery roots.

    Construct with the ordered roots (built-in first, user last so user
    overrides win), then call :meth:`build_registry` at each dispatch.
    The per-path ``(mtime, parsed)`` cache makes repeated dispatches cheap
    and pins the "re-read only a changed file, only at dispatch" rule.
    """

    def __init__(self, roots: list[AgentRoot]) -> None:
        self._roots = roots
        self._cache: dict[Path, _CacheEntry] = {}

    @classmethod
    def for_project(
        cls,
        *,
        agents_dir: Path,
        builtin_dir: Path | None = None,
    ) -> AgentDiscovery:
        """Build discovery for a project's user ``agents_dir`` (+ built-ins).

        ``agents_dir`` is the resolved ``<root>/<PathsConfig.agents_dir>``;
        ``builtin_dir`` defaults to the shipped :data:`BUILTIN_AGENTS_DIR`
        (overridable in tests).
        """
        builtin = builtin_dir if builtin_dir is not None else BUILTIN_AGENTS_DIR
        return cls(
            [
                AgentRoot(directory=builtin.resolve(), label="builtin"),
                AgentRoot(directory=agents_dir.resolve(), label="user"),
            ]
        )

    def build_registry(self) -> SubagentRegistry:
        """Enumerate all roots and return a freshly-populated registry.

        Each root is parsed (with the mtime cache) and registered in order
        via :meth:`SubagentRegistry.register_files`, so a duplicate name
        *within* a root raises and a user file overriding a built-in logs.
        A file that fails to load is logged and skipped — one bad user file
        does not take down discovery of the rest (but it never partially
        registers: ``load_agent_file`` is all-or-nothing per file).
        """
        registry = SubagentRegistry()
        for root in self._roots:
            agents = self._parse_root(root)
            registry.register_files(agents, root_label=root.label)
        return registry

    def discover(self) -> list[AgentFile]:
        """Return every successfully-parsed agent across all roots.

        A flat list for the CLI (``carve agents list``); ordering is
        builtin-then-user so a caller can detect overrides by later-wins.
        """
        out: list[AgentFile] = []
        for root in self._roots:
            out.extend(self._parse_root(root))
        return out

    def _parse_root(self, root: AgentRoot) -> list[AgentFile]:
        """Parse every ``*.md`` in one root, using the mtime cache.

        In the **built-in** root we ship non-agent docs alongside the agent
        files (``builtin/README.md`` documents the discovery root itself).
        Those have no ``---`` frontmatter fence, so the loader would reject
        them and ``_parse_cached`` would log a warning on *every*
        ``build_registry()`` call (``carve agents list/show`` runs it). We
        skip frontmatter-less files in this root quietly: a built-in doc is
        ours, not a user drop-in, so silently ignoring it is correct (and
        keeps the registry build quiet). The **user** root keeps the
        skip-and-*log* discipline — a malformed user agent file is surfaced,
        not hidden.
        """
        directory = root.directory
        if not directory.is_dir():
            return []
        is_builtin = root.label == "builtin"
        agents: list[AgentFile] = []
        for path in sorted(directory.glob("*.md")):
            if not path.is_file():
                continue
            if is_builtin and not _has_frontmatter_fence(path):
                # A built-in non-agent doc (e.g. README.md): skip quietly.
                continue
            parsed = self._parse_cached(path)
            if parsed is not None:
                agents.append(parsed)
        return agents

    def _parse_cached(self, path: Path) -> AgentFile | None:
        """Return the parsed agent for ``path``, re-reading only on change.

        Compares the file's current ``st_mtime_ns`` to the cached one; an
        unchanged file returns the cached parse (no disk read of the body,
        no re-parse). A changed file is re-parsed and the cache updated.
        A load failure is logged, the stale cache entry (if any) dropped,
        and ``None`` returned.
        """
        path = path.resolve()
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError as exc:
            logger.warning("Cannot stat agent file %s: %s", path, exc)
            self._cache.pop(path, None)
            return None

        cached = self._cache.get(path)
        if cached is not None and cached.mtime_ns == mtime_ns:
            return cached.parsed

        try:
            parsed = load_agent_file(path)
        except AgentLoadError as exc:
            logger.warning("Skipping agent file %s: %s", path, exc)
            self._cache.pop(path, None)
            return None

        self._cache[path] = _CacheEntry(mtime_ns=mtime_ns, parsed=parsed)
        return parsed


# The frontmatter fence an agent file must open with (mirrors
# ``loader._FENCE``). Used to quietly skip built-in docs (README.md) that
# are intentionally *not* agent files, before they reach the loader.
_FRONTMATTER_FENCE = "---"


def _has_frontmatter_fence(path: Path) -> bool:
    """Return True iff ``path``'s first non-blank line is a ``---`` fence.

    A cheap pre-check (reads only the file's head) so a non-agent doc in the
    built-in root is skipped *quietly* — never handed to the loader, which
    would otherwise reject it and log a warning on every registry build. A
    read error is treated as "no fence" (skip): the loader would fail it
    anyway, and the built-in root never holds files we must surface.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    return stripped == _FRONTMATTER_FENCE
    except OSError:
        return False
    return False  # empty file: no fence.


__all__ = [
    "BUILTIN_AGENTS_DIR",
    "AgentDiscovery",
    "AgentRoot",
]
