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
``SubagentRunner.run`` blocks). :func:`run_single_engine` is the single-engine
case: one classification → one route → one ``DelegationResult``. Sub-slice B
adds :func:`run_engines`, which loops the *same* per-engine machinery over an
already-decomposed ordered list of sub-goals (the runner is built once and
threaded across all of them), yielding the N ``DelegationResult``s the planner
merges into one Plan. Decomposition itself lives in the planner — this module
runs an already-decomposed sequence, keeping "decide what" and "run it" apart.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from carve.cli.orchestrator.extensibility_wiring import (
    HookFactory,
    build_extensibility_hook_factory,
)
from carve.cli.orchestrator.extra_tools import assemble_extra_tools
from carve.cli.orchestrator.goal_classifier import classify_goal
from carve.cli.orchestrator.goal_decomposer import SubGoal
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

    # The single-engine route is the N=1 case: one classified slice run through
    # the shared per-engine machinery (route + design-capacity delegate).
    return _delegate_engine(
        SubGoal(sub_goal=goal, classification=classification),
        registry=registry,
        runner=runner,
        parent_mode=parent_mode,
    )


def run_engines(
    sub_goals: Sequence[SubGoal],
    *,
    config: Config,
    project_dir: Path,
    client: Any,
    model: str,
    registry: SubagentRegistry | None = None,
    runner: SubagentRunner | None = None,
    hook_factory: HookFactory | None = None,
    approver: Approver | None = None,
    parent_mode: PermissionMode = PermissionMode.PLAN,
    max_turns: int = 30,
) -> list[DelegationResult]:
    """Route + delegate each already-decomposed ``SubGoal`` in order (sync).

    The multi-engine generalization of :func:`run_single_engine`: it takes an
    **already-decomposed** ordered list of sub-goals (decomposition runs in the
    planner — keeping "decide what" separate from "run it") and, for each,
    :func:`select_agent` resolves the engineer and a SYNC :func:`delegate` at
    ``parent_mode=PLAN`` (design capacity) returns one
    :class:`DelegationResult`. The N results come back **in sub-goal order**.

    Delegation stays **sync / sequential** — the harness invariant. The
    :class:`SubagentRunner` is built **once** (one registry, one hook factory,
    one runner) and threaded across all sub-goals; it is never rebuilt per
    sub-goal. ``run_single_engine`` is exactly this over a 1-element list (it
    classifies the single label first, then delegates through the same helper).

    Args:
        sub_goals: The ordered decomposition. An empty sequence yields ``[]``
            (no engine ran) — the caller treats that as "nothing routed".
        config: The fully-loaded :class:`Config`.
        project_dir: The resolved project root.
        client: A resolved Anthropic client; shared by every delegated child
            loop. Injected for offline tests.
        model: The default model id; the child's fallback when an agent pins no
            ``model:`` tier.
        registry: Optional pre-built registry; built via :func:`build_registry`
            when omitted. The router resolves each sub-goal's classification
            against it.
        runner: Optional pre-built :class:`SubagentRunner` (a test seam). When
            omitted, one is constructed once with the run's approver, the
            extensibility hook factory, the model tiers, and the assembled
            ``extra_tools`` — and reused for every sub-goal.
        hook_factory: Optional pre-built extensibility hook factory. When the
            planner already parsed ``carve/hooks.toml`` upstream (its fail-closed
            boundary, established *before* the decompose LLM call), it threads the
            factory in so the file is parsed exactly once; when omitted (and no
            ``runner`` is injected) it is built here, preserving the same boundary
            for direct callers.
        approver: The run's interactive approver (``None`` headless).
        parent_mode: The mode to delegate at; each child clamps to
            ``min(parent_mode, capability)``. Defaults to ``PLAN`` (design-only).
        max_turns: Cap on each child loop's turns.

    Returns:
        The N :class:`DelegationResult`s, in the sub-goals' order.

    Raises:
        NoAgentMatch: A sub-goal's classification matched no registered agent
            (propagated; the planner falls back to the monolithic M1 path).
    """
    if not sub_goals:
        return []

    if registry is None:
        registry = build_registry(project_dir, config)

    # Build the extensibility hook factory FIRST (only when we'll construct the
    # runner ourselves): it parses carve/hooks.toml eagerly, so a malformed file
    # is fail-closed BEFORE any delegation — the same boundary the M1 plan flow
    # promises. A missing file yields a no-op factory. (When a runner is
    # injected — the test seam — we don't touch hooks here.) A caller that
    # already parsed hooks upstream (the planner, before its decompose call)
    # threads the factory in, so the file is parsed exactly once.
    if runner is None:
        if hook_factory is None:
            hook_factory = build_extensibility_hook_factory(
                project_dir=project_dir,
                paths=config.paths,
                approver=approver,
            )
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

    # Sequential — the harness invariant (`SubagentRunner.run` blocks). One
    # runner, one registry, threaded across every sub-goal; results in order.
    results: list[DelegationResult] = []
    for sub_goal in sub_goals:
        results.append(
            _delegate_engine(
                sub_goal,
                registry=registry,
                runner=runner,
                parent_mode=parent_mode,
            )
        )
    return results


def _capacity_for(parent_mode: PermissionMode) -> str:
    """Map the delegation ``parent_mode`` to the engineer's *capacity* signal.

    The engineers' "Plan vs Build capacity" sections key off the ``capacity``
    context key: ``"design"`` ⇒ read/design only (return a DESIGN via
    ``submit_result.outputs``, author nothing); ``"build"`` (or the key absent)
    ⇒ author and verify real files. The orchestrator derives that signal from
    the mode it is delegating at:

    * ``PLAN`` ⇒ ``"design"`` — the plan-side B1 contract, unchanged: a plan is
      design-only, no code authored, so ``files_changed`` stays EMPTY.
    * ``BUILD`` ⇒ ``"build"`` — the build-side B2 contract: the engineer authors
      its real slice, and ``files_changed`` is the harness-tracked authored set.

    Any other parent_mode falls back to ``"design"`` (the conservative read-only
    signal): a narrower-than-PLAN delegation never grants authoring, so signalling
    DESIGN matches the actual clamp. This is the single place the B1 design-capacity
    wiring generalizes for build — PLAN callers are byte-identical to before.
    """
    return "build" if parent_mode == PermissionMode.BUILD else "design"


def _delegate_engine(
    sub_goal: SubGoal,
    *,
    registry: SubagentRegistry,
    runner: SubagentRunner,
    parent_mode: PermissionMode,
) -> DelegationResult:
    """Route one ``SubGoal`` to its engineer and delegate (sync, mode-derived capacity).

    The shared per-engine leg both :func:`run_single_engine` and
    :func:`run_engines` call: :func:`select_agent` resolves the classification
    to an agent name, the typed context is assembled, and the goal slice is
    delegated SYNC at ``parent_mode``. Extracting it keeps the single-engine and
    multi-engine paths one implementation — ``run_single_engine`` is the N=1 case.
    """
    agent_name = select_agent(registry, classification=sub_goal.classification)
    logger.debug(
        "Routed sub-goal %r to agent %r via classification %r.",
        _truncate(sub_goal.sub_goal),
        agent_name,
        sub_goal.classification,
    )

    # Typed context bundle — named keys only, never a transcript (the runner
    # enforces context isolation; this is the orchestrator's deliberate share).
    # `capacity` is derived from `parent_mode` (PLAN ⇒ "design", BUILD ⇒ "build")
    # so the SAME helper serves both halves of plan/build: at PLAN the engineer
    # returns a DESIGN via `submit_result.outputs` and authors nothing
    # (`files_changed` is correctly EMPTY); at BUILD it authors + verifies its
    # real slice and `files_changed` is the harness-tracked authored set. The
    # mode clamp inside `SubagentRunner.run` remains the authoritative boundary;
    # `capacity` only tells the engineer which job it is doing.
    context = {
        "goal_slice": sub_goal.sub_goal,
        "classification": sub_goal.classification,
        "capacity": _capacity_for(parent_mode),
    }
    return delegate(
        agent_name,
        sub_goal.sub_goal,
        context=context,
        parent_mode=parent_mode,
        runner=runner,
    )


def _truncate(text: str, limit: int = 120) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


__all__ = ["build_registry", "run_engines", "run_single_engine"]
