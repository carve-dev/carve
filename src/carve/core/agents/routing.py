"""The classification router — choose *which* agent name to delegate to.

This replaces the *dispatch role* of the old hardcoded ``AGENT_REGISTRY``
dict. Given a goal's ``classification`` (and an optional free-text
``description``), the router matches it against each loaded agent's
``classifications`` and returns the **agent name** the orchestrator then
hands to ``delegation.delegate(agent, …)`` / ``SubagentRunner.run(agent,
…)``. Those entry points are **unchanged** — they resolve by name; the
router only *chooses* the name.

Two layers, in order:

1. **Explicit override-by-name.** If the caller passes an ``override``
   name and it resolves in the registry, that name short-circuits
   classification matching (the user picked the agent).
2. **Classification match.** Otherwise the goal's classification is
   matched against each agent's ``classifications``. A clean miss is a
   :class:`NoAgentMatch` — never a wrong-agent silent pick.

The router reads a :class:`SubagentRegistry` (built by
``AgentDiscovery.build_registry`` from the discovery roots), so a user
override participates: the registry already resolved user-over-builtin by
the time the router sees it.
"""

from __future__ import annotations

from carve.core.agents.exceptions import AgentError
from carve.core.agents.subagent_registry import SubagentRegistry


class NoAgentMatch(AgentError):
    """Raised when no agent matches a goal's classification.

    A *clear* no-match (the spec's bar): surfacing this is strictly better
    than silently routing to a wrong/default agent. The caller decides
    whether to ask the user, fall back, or abort.
    """


def select_agent(
    registry: SubagentRegistry,
    *,
    classification: str | None = None,
    override: str | None = None,
) -> str:
    """Return the agent name to delegate to.

    * ``override`` — an explicit agent name; if it resolves in
      ``registry`` it wins outright. An override naming an unknown agent
      raises :class:`NoAgentMatch` (the user asked for an agent that does
      not exist — fail loudly, don't fall back to classification).
    * ``classification`` — matched against each agent's
      ``classifications``. Exactly-one match returns that name; multiple
      matches are resolved deterministically (first by sorted name) so the
      pick is stable; no match raises :class:`NoAgentMatch`.

    Passing neither is a programming error (``ValueError``): the router
    needs *something* to route on.
    """
    if override is not None:
        if override in registry:
            return override
        raise NoAgentMatch(
            f"Requested agent {override!r} is not registered. "
            f"Known agents: {registry.names()}."
        )

    if classification is None:
        raise ValueError(
            "select_agent needs a classification or an override name."
        )

    matches = [
        spec.name
        for spec in registry.specs()
        if classification in spec.classifications
    ]
    if not matches:
        raise NoAgentMatch(
            f"No agent handles classification {classification!r}. "
            f"Known agents: {registry.names()}."
        )
    # `specs()` is already name-sorted, so `matches[0]` is the
    # lowest-named agent — a deterministic, legible tie-break.
    return matches[0]


__all__ = ["NoAgentMatch", "select_agent"]
