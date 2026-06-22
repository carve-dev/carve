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

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from carve.core.agents.loader import AgentFile
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.tools import Tool
from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import ComponentConfig
from carve.integrations.component_locator import (
    ResolvedComponent,
    discover_components,
    resolve_component,
)

logger = logging.getLogger(__name__)

# A tool factory builds the agent's tool list for one delegation. It is
# handed the resolved project root so file/bash tools can be scoped; the
# runner intersects the resulting names with the mode-permitted set.
ToolFactory = Callable[[ProjectPaths], list[Tool]]


@dataclass(frozen=True)
class AgentSpec:
    """A subagent's resolved definition (built-in *or* declarative file).

    * ``name`` — the delegation key.
    * ``system_prompt`` — the agent's base prompt (the markdown body of a
      declarative agent file).
    * ``capability`` — the widest mode it may run at (clamped on
      delegation). **Field-name reconciliation:** the declarative agent
      frontmatter key is ``max_mode:``; the loader maps it
      ``PermissionMode → capability`` here (the names differ; the meaning
      is identical — "the widest mode this agent ever needs"). The
      *runtime gate* is authoritative; ``capability`` only feeds the
      delegation clamp ``min(parent_mode, capability)``.
    * ``tool_factory`` — builds its tool list per run.
    * ``model`` — optional per-agent model tier (the ``model:``
      frontmatter). ``None`` falls back to the install default
      (``ModelsConfig.default_model``) at delegation time; defaulted so
      existing built-in construction sites stay valid.
    * ``classifications`` — the goal classifications this agent handles
      (the router matches a goal's classification against these). Empty
      for built-ins that the router selects by name only.
    * ``description`` — free-text hint the router may use alongside the
      classification match. Empty for hand-built specs.
    """

    name: str
    system_prompt: str
    capability: PermissionMode
    tool_factory: ToolFactory
    model: str | None = None
    classifications: tuple[str, ...] = ()
    description: str = ""


def _declarative_tool_factory(tool_names: tuple[str, ...]) -> ToolFactory:
    """Build a ``ToolFactory`` that yields name-only ``Tool`` stubs.

    A declarative agent's ``tools:`` grant is a list of **names**. The
    delegation path only reads ``frozenset(t.name for t in factory_tools)``
    to feed the policy intersection (``runtime = grant ∩ mode``), so the
    factory needs to surface exactly those names. The executors for the
    granted tools are supplied by the harness/domain layer that owns each
    name (base tools, ``@skill`` functions, ``mcp:`` imports); this stub
    factory only declares the *grant*, never an executor that could run.
    The stub executor raises if ever called — a declarative file cannot
    smuggle behavior past the harness.
    """

    def _factory(_paths: ProjectPaths) -> list[Tool]:
        return [_grant_stub_tool(name) for name in tool_names]

    return _factory


def _grant_stub_tool(name: str) -> Tool:
    """A name-only ``Tool`` whose executor refuses to run.

    Declarative grants declare *which* tools an agent may use; the real
    executor is bound by the harness when it composes the runtime tool
    set. If this stub's executor is ever invoked it means a name went
    unbound — a wiring bug, not a thing to silently no-op.
    """

    def _refuse(_input: dict[str, object]) -> str:
        raise RuntimeError(
            f"Tool {name!r} was granted declaratively but has no bound "
            "executor; the harness must supply it before invocation."
        )

    return Tool(
        name=name,
        description=f"Declaratively-granted tool {name!r} (executor bound by the harness).",
        input_schema={"type": "object", "properties": {}},
        executor=_refuse,
    )


def spec_from_agent_file(agent: AgentFile) -> AgentSpec:
    """Turn a parsed declarative :class:`AgentFile` into an :class:`AgentSpec`.

    The mapping (documented on :class:`AgentSpec`):

    * body → ``system_prompt``
    * ``max_mode`` → ``capability`` (the field-name reconciliation)
    * ``tools:`` → ``tool_factory`` (name-only grant stubs)
    * ``model:`` → ``model`` (per-agent tier; ``None`` → install default)
    * ``classifications:`` / ``description`` → carried through for the
      router.
    """
    return AgentSpec(
        name=agent.name,
        system_prompt=agent.body,
        capability=agent.max_mode,
        tool_factory=_declarative_tool_factory(agent.tools),
        model=agent.model,
        classifications=agent.classifications,
        description=agent.description,
    )


class SubagentRegistry:
    """Name → :class:`AgentSpec` map for built-in *and* declarative agents.

    Starts empty; the orchestrator / domain specs call :meth:`register`
    to populate it directly, and the declarative loader populates it from
    discovered agent files via :meth:`register_files` (the seam this
    docstring has anticipated since the harness shipped). Resolution is by
    exact name; an unknown name raises ``KeyError`` (delegation surfaces
    it as a ``SubagentError``).
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        """Register (or replace) an agent by name.

        Direct registration is unconditional replacement — it is the
        built-in/orchestrator path where the caller owns the ordering.
        The *file* population path (:meth:`register_files`) is the one
        that enforces the no-silent-overwrite discipline within a root.
        """
        self._agents[spec.name] = spec

    def register_files(self, agents: Iterable[AgentFile], *, root_label: str = "root") -> None:
        """Register a batch of declarative agents with collision discipline.

        Mirrors ``skills/registry.py``: a **duplicate name within this
        batch (root)** is an error (two files claiming one name in the
        same discovery root is ambiguous), while replacing an agent
        already registered from an *earlier* root is a **logged override**
        (user-over-builtin is the intended mechanism). ``root_label`` is
        used only in the error/log messages.

        Raises ``ValueError`` on an intra-root duplicate; on the error,
        nothing from this batch is partially applied — the batch is staged
        and committed atomically.
        """
        staged: dict[str, AgentSpec] = {}
        for agent in agents:
            spec = spec_from_agent_file(agent)
            if spec.name in staged:
                raise ValueError(
                    f"Duplicate agent name {spec.name!r} within {root_label!r} "
                    f"(both {staged[spec.name].name!r} and {agent.source_path}); "
                    "names must be unique within a discovery root."
                )
            staged[spec.name] = spec

        for name, spec in staged.items():
            if name in self._agents:
                logger.info(
                    "Agent %r from %s overrides an earlier registration.",
                    name,
                    root_label,
                )
            self._agents[name] = spec

    def resolve(self, name: str) -> AgentSpec:
        """Return the :class:`AgentSpec` for ``name`` (raises ``KeyError``)."""
        return self._agents[name]

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    def names(self) -> list[str]:
        return sorted(self._agents)

    def specs(self) -> list[AgentSpec]:
        """Return all registered specs (sorted by name) — for routing/CLI."""
        return [self._agents[name] for name in sorted(self._agents)]

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
    "spec_from_agent_file",
]
