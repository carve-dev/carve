"""Live single-engine delegation — classify → route → assemble → delegate.

The first leg of plan synthesis (sub-slice A of the plan-build keystone): turn
a natural-language goal into **one** engineer's verified result. The sequence,
kept standalone (not inside ``planner.py``) so it is unit-testable in isolation
and so sub-slice B can extend it — decomposition wraps a *loop* over this
single-engine call:

1. **Build the registry once** (built-ins + the user ``agents_dir``) and thread
   the *same* :class:`SubagentRegistry` into both the classifier and the runner,
   so the candidate set and the route resolve against one source of truth.
2. **Classify** the goal to one registered label (a one-shot LLM call).
3. **Route** the label to one agent name via :func:`select_agent` (a clear
   :class:`NoAgentMatch` on a miss — never a silent default).
4. **Assemble** the engineer's grant-name → bound-Tool map (the ``sql`` /
   ``dlt_library`` / dbt / pipeline readers the harness can't build alone).
5. **Construct** a :class:`SubagentRunner` with the run's approver + the
   extensibility hook factory + the model tiers + the assembled ``extra_tools``.
   The gate is built *inside* ``SubagentRunner.run`` from the agent's grant +
   clamped mode — we pass the approver/hook-factory it clamps, never a gate.
6. **Delegate** the goal SYNC at ``parent_mode=PLAN`` (the child clamps to
   ``min(PLAN, capability)`` — design-only, no writes during a plan).
7. **Return** the :class:`DelegationResult` (its ``files_changed`` / ``outputs``
   / ``usage`` / ``cost_usd`` flow into the Unit-1 ``roll_up_cost`` seam).

Delegation stays **sync / sequential** (the harness invariant —
``SubagentRunner.run`` blocks). Single engine only: exactly one classification
→ one route → one ``DelegationResult``. Multi-goal decomposition + multi-engine
synthesis is sub-slice B.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from carve.cli.orchestrator.extensibility_wiring import (
    HookFactory,
    build_extensibility_hook_factory,
)
from carve.cli.orchestrator.extra_tools import assemble_extra_tools
from carve.cli.orchestrator.goal_classifier import classify_goal
from carve.core.agents.delegation import DelegationResult, SubagentRunner, delegate
from carve.core.agents.discovery import AgentDiscovery
from carve.core.agents.permissions.gate import Approver
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.routing import select_agent
from carve.core.agents.subagent_registry import SubagentRegistry
from carve.core.config import Config
from carve.core.config.paths import ProjectPaths

logger = logging.getLogger(__name__)


def build_registry(project_dir: Path, config: Config) -> SubagentRegistry:
    """Discover + register the project's agents (built-ins + user ``agents_dir``).

    A thin wrapper over ``AgentDiscovery.for_project(...).build_registry()`` that
    resolves the user agents dir from ``config.paths.agents_dir``. Hoisted so the
    orchestrator builds the registry **once** and threads the same instance into
    the classifier and the runner.
    """
    agents_dir = (project_dir / config.paths.agents_dir).resolve()
    return AgentDiscovery.for_project(agents_dir=agents_dir).build_registry()


def run_single_engine(
    goal: str,
    *,
    config: Config,
    project_dir: Path,
    client: Any,
    model: str,
    registry: SubagentRegistry | None = None,
    runner: SubagentRunner | None = None,
    approver: Approver | None = None,
    parent_mode: PermissionMode = PermissionMode.PLAN,
    max_turns: int = 30,
) -> DelegationResult:
    """Classify ``goal``, route it to one engineer, and delegate (sync).

    Args:
        goal: The user's natural-language goal.
        config: The fully-loaded :class:`Config`.
        project_dir: The resolved project root.
        client: A resolved Anthropic client (the planner passes
            ``make_client(config, client)``); shared by the classifier call and
            the delegated child loop. Injected for offline tests.
        model: The default model id (the planner passes
            ``config.models.default_model``); used for classification and as the
            child's fallback model when the agent pins no ``model:`` tier.
        registry: Optional pre-built registry; built via :func:`build_registry`
            when omitted. Threaded into both the classifier and the runner.
        runner: Optional pre-built :class:`SubagentRunner` (a test seam — inject
            a fake/stub runner). When omitted, one is constructed with the
            run's approver, the extensibility hook factory, the model tiers, and
            the assembled ``extra_tools``.
        approver: The run's interactive approver (``None`` headless).
        parent_mode: The mode to delegate at; the child clamps to
            ``min(parent_mode, capability)``. Defaults to ``PLAN`` (design-only).
        max_turns: Cap on the child loop's turns.

    Returns:
        The single :class:`DelegationResult` the routed engineer produced.

    Raises:
        GoalClassificationError: The goal could not be classified (propagated;
            the caller falls back to the monolithic M1 path).
        NoAgentMatch: The classification matched no registered agent
            (propagated; same fallback contract).
    """
    if registry is None:
        registry = build_registry(project_dir, config)

    # Build the extensibility hook factory FIRST (only when we'll construct the
    # runner ourselves): it parses carve/hooks.toml eagerly, so a malformed file
    # is fail-closed BEFORE any model call — the same boundary the M1 plan flow
    # promises. A missing file yields a no-op factory. (When a runner is
    # injected — the test seam — we don't touch hooks here.)
    hook_factory = None
    if runner is None:
        hook_factory = build_extensibility_hook_factory(
            project_dir=project_dir,
            paths=config.paths,
            approver=approver,
        )

    classification = classify_goal(goal, client=client, model=model, registry=registry)
    agent_name = select_agent(registry, classification=classification)
    logger.debug("Routed goal to agent %r via classification %r.", agent_name, classification)

    if runner is None:
        runner = _build_runner(
            registry=registry,
            config=config,
            project_dir=project_dir,
            client=client,
            model=model,
            approver=approver,
            parent_mode=parent_mode,
            max_turns=max_turns,
            hook_factory=hook_factory,
        )

    # Typed context bundle — named keys only, never a transcript (the runner
    # enforces context isolation; this is the orchestrator's deliberate share).
    # `capacity="design"` signals the engineer it runs in DESIGN capacity (this
    # delegation always runs at parent_mode=PLAN — read/design authority, no code
    # authored). The engineer prompts read this key to return a DESIGN via
    # `submit_result.outputs` instead of authoring + verifying files (that is a
    # build-time behavior). `files_changed` is therefore correctly EMPTY at plan.
    context = {
        "goal_slice": goal,
        "classification": classification,
        "capacity": "design",
    }
    return delegate(
        agent_name,
        goal,
        context=context,
        parent_mode=parent_mode,
        runner=runner,
    )


def _build_runner(
    *,
    registry: SubagentRegistry,
    config: Config,
    project_dir: Path,
    client: Any,
    model: str,
    approver: Approver | None,
    parent_mode: PermissionMode,
    max_turns: int,
    hook_factory: HookFactory | None,
) -> SubagentRunner:
    """Construct the live :class:`SubagentRunner` for the routed delegation.

    The gate is NOT passed — ``SubagentRunner.run`` builds it from the agent's
    grant + clamped mode. We pass the approver + the (pre-built, mode-clamping)
    hook *factory* (the runner re-clamps the hooks at ``child_mode``) + the model
    tiers + the assembled ``extra_tools``. The ``sql`` tool is baked to the
    child's mode so its read/write enforcement matches the loop the child runs
    at.
    """
    paths = ProjectPaths.from_root(project_dir)
    # Bake the (shared) sql tool to the parent's mode. The child's clamp is
    # `min(parent_mode, capability) <= parent_mode`, so the sql tool is never
    # *narrower* than the child it serves, while the sql tool's own `role_for`
    # floor (writes/DDL only at `deploy`) and the child's authoritative gate
    # (built from its grant + clamped mode) remain the real boundaries. Under a
    # PLAN parent the tool reads only — design-only, as a plan requires.
    extra_tools = assemble_extra_tools(
        config_components=config.components,
        project_dir=project_dir,
        paths=paths,
        child_mode=parent_mode,
    )
    return SubagentRunner(
        registry=registry,
        paths=paths,
        client=client,
        model=model,
        model_tiers=config.models.tiers,
        approver=approver,
        max_turns=max_turns,
        hook_factory=hook_factory,
        extra_tools=extra_tools,
    )


__all__ = ["build_registry", "run_single_engine"]
