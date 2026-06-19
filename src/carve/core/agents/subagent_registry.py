"""Resolve a built-in subagent *name* to its runnable definition.

The orchestrator delegates by **name** — ``"engineer"``, ``"qa"``,
``"security"`` — and the registry maps that name to an :class:`AgentSpec`:
the system prompt, the tool factory, and the agent's **capability mode**
(the widest :class:`PermissionMode` it may run at; delegation clamps to
``min(parent_mode, capability)``).

Two scope boundaries to keep straight:

* The **declarative agent file format** (frontmatter ``tools:`` /
  ``mode:`` / ``allowed_paths:``, ``hooks.toml``, MCP import) is
  *extensibility* (spec 16). This registry resolves only the **built-in**
  agents and exposes the seam 16 plugs a file loader into — it does not
  parse agent files.
* Resolving the *components* a subagent operates on is delegated to
  ``layout``'s :func:`resolve_component` / :func:`discover_components`
  (re-exported here), so path math stays in one place.

The built-in specs ship with conservative defaults; the domain-agent
specs (04/08/12/recovery/SQL, later increments) register richer prompts
and tool factories against the same names via :meth:`register`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.tools import Tool
from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import ComponentConfig
from carve.integrations.component_locator import (
    ResolvedComponent,
    discover_components,
    resolve_component,
)

# A tool factory builds the agent's tool list for one delegation. It is
# handed the resolved project root so file/bash tools can be scoped; the
# runner intersects the resulting names with the mode-permitted set.
ToolFactory = Callable[[ProjectPaths], list[Tool]]


@dataclass(frozen=True)
class AgentSpec:
    """A built-in subagent's resolved definition.

    * ``name`` — the delegation key.
    * ``system_prompt`` — the agent's base prompt.
    * ``capability`` — the widest mode it may run at (clamped on
      delegation).
    * ``tool_factory`` — builds its tool list per run.
    """

    name: str
    system_prompt: str
    capability: PermissionMode
    tool_factory: ToolFactory


class SubagentRegistry:
    """Name → :class:`AgentSpec` map for the built-in agents.

    Starts empty; the orchestrator / domain specs call :meth:`register`
    to populate it. Resolution is by exact name; an unknown name raises
    ``KeyError`` (delegation surfaces it as a ``SubagentError``).
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        """Register (or replace) a built-in agent by name."""
        self._agents[spec.name] = spec

    def resolve(self, name: str) -> AgentSpec:
        """Return the :class:`AgentSpec` for ``name`` (raises ``KeyError``)."""
        return self._agents[name]

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    def names(self) -> list[str]:
        return sorted(self._agents)

    # --- component resolution (delegated to layout's locator) ---

    @staticmethod
    def resolve_component(
        name: str,
        *,
        components: dict[str, ComponentConfig],
        paths: ProjectPaths,
    ) -> ResolvedComponent:
        """Resolve a component name a subagent operates on (layout locator)."""
        return resolve_component(name, components=components, paths=paths)

    @staticmethod
    def discover_components(paths: ProjectPaths) -> list[ResolvedComponent]:
        """Enumerate convention-discovered components (layout locator)."""
        return discover_components(paths)


__all__ = [
    "AgentSpec",
    "SubagentRegistry",
    "ToolFactory",
]
