"""Live wiring for the extensibility primitives (spec 16) into the loop.

The extensibility slice shipped the *loaders + config + gate registration*
but deferred the seams that plug them into a running ``carve plan`` /
``carve build``. This module is those seams, in one place so the plan
flow, the build flow, and delegated subagents all wire identically:

* :func:`build_extensibility_hook_factory` — load ``carve/hooks.toml``
  *once* (a missing file is fine → no hooks) and return a **factory**:
  ``(mode) -> (pre_tool_hook, post_tool_hook)`` that builds the
  :class:`HookRunner` (and its bash gate) **at the mode it is called
  with**. The factory is the seam that keeps a hook clamped to the mode of
  *whatever loop fires it* — the top-level run at its own mode, a delegated
  subagent at its (narrower) ``child_mode``.
* :func:`build_extensibility_hooks` — the eager convenience over the
  factory: build the ``(pre, post)`` for a single, fixed mode. The plan
  flow (``plan``) and the build flow (``build``) call this directly because
  their loop runs at exactly one mode and never re-clamps.
* :func:`build_extensibility_post_build_hook` — the **lifecycle** sibling:
  load the same ``hooks.toml`` (same fail-closed boundary) and return the
  single ``post_build`` callable the build flow fires after a ``Build`` is
  recorded, gated at ``BUILD``. This is plan-build's emitter for
  extensibility's ``post_build`` subscription seam.
* :func:`build_skill_pack_tool` — the ``lookup_skill_pack`` content
  -injection tool, so description-matched packs are discoverable at
  runtime.
* :func:`resolve_agent_or_fallback` — route through the classification
  router (``select_agent``) when a declarative agent matches, and **fall
  back** to the caller's hardcoded dispatch (return ``None``) when none
  does. ``builtin/`` is empty until later increments, so the fallback is
  what preserves the M1 extract-load plan/build flow unchanged.

Why the mode is *passed to the factory at fire-build time* rather than
baked once: the run already knows its mode (plan runs at ``plan``, build at
``build``), and the hook runner must be gated at *that* floor — a hook in a
plan run must not reach write/network bash. But a **delegated subagent**
runs at ``min(parent_mode, capability)`` — *narrower* than its parent — and
a hook that fires inside the child must be clamped to the **child's**
authority, never the parent's. Baking a single mode into a pre-built
closure (the old shape) escalated a child hook to the parent's mode; the
factory re-derives the gate at ``child_mode`` instead, so the clamp is
inherited from *whichever* loop fires the hook.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from carve.core.agents.discovery import AgentDiscovery
from carve.core.agents.permissions.gate import Approver, PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.agents.routing import NoAgentMatch, select_agent
from carve.core.agents.tools import Tool
from carve.core.config.schema import PathsConfig
from carve.core.hooks.config import HookConfigError, HookSpec, load_hooks_config
from carve.core.hooks.runner import HookRunner
from carve.core.hooks.wiring import (
    LifecycleHook,
    ToolHook,
    build_on_run_failed_hook,
    build_post_build_hook,
    build_tool_hooks,
)
from carve.core.skills.pack_discovery import discover_pack_roots

logger = logging.getLogger(__name__)

# A hook factory: given a :class:`PermissionMode`, build the loop's
# ``(pre_tool_hook, post_tool_hook)`` clamped to *that* mode. The same
# factory is handed to the top-level loop (called once at the run's mode)
# and to a :class:`~carve.core.agents.delegation.SubagentRunner` (called per
# delegation at the child's clamped mode), so a hook fires at the authority
# of whichever loop fired it — never wider.
HookFactory = Callable[[PermissionMode], tuple[ToolHook | None, ToolHook | None]]


def _load_hook_specs_fail_closed(project_dir: Path, paths: PathsConfig) -> list[HookSpec]:
    """Parse ``carve/hooks.toml`` once, fail-closed — the shared loader boundary.

    Resolves ``hooks.toml`` at ``project_dir / paths.hooks_file`` and parses
    it eagerly with :func:`load_hooks_config`. A **missing file yields no
    hooks** (``[]``, never raises); a **present-but-malformed file is
    fail-closed** (:class:`HookConfigError` propagates). Both the tool-hook
    factory and the ``post_build`` lifecycle builder go through this one
    boundary so the malformed-config abort is identical across them.
    """
    hooks_path = (project_dir / paths.hooks_file).resolve()
    try:
        return load_hooks_config(hooks_path)
    except HookConfigError:
        # Fail-closed: a present-but-malformed hooks.toml is a configuration
        # error the run must surface, not swallow. (A *missing* file already
        # returns [] inside load_hooks_config — that path never raises.)
        logger.error("Malformed hooks config at %s; aborting run.", hooks_path)
        raise


def build_extensibility_hook_factory(
    *,
    project_dir: Path,
    paths: PathsConfig,
    approver: Approver | None = None,
) -> HookFactory:
    """Load ``carve/hooks.toml`` once and return a mode-clamping hook factory.

    Resolves ``hooks.toml`` at ``project_dir / paths.hooks_file`` and parses
    it **eagerly** (so a malformed file is surfaced *here*, fail-closed,
    before any loop runs). A **missing file yields no hooks**: the returned
    factory then produces ``(None, None)`` at every mode.

    The returned factory builds, per call, a :class:`HookRunner` over a
    :class:`PermissionGate` for ``build_policy(mode)`` — so every hook
    command runs through the *same* bash gate a tool call would, clamped to
    **the mode the factory is invoked with**. This is the seam that keeps a
    delegated subagent's hook clamped to its (narrower) ``child_mode``
    rather than its parent's: the runner calls the factory with
    ``child_mode``.

    A malformed ``hooks.toml`` is **fail-closed**: the
    :class:`HookConfigError` propagates so the run surfaces the bad config
    rather than silently dropping the hook set.
    """
    specs = _load_hook_specs_fail_closed(project_dir, paths)

    def _factory(
        mode: PermissionMode,
    ) -> tuple[ToolHook | None, ToolHook | None]:
        if not specs:
            return None, None
        # Rebuild the gate at *this* mode — the clamp the firing loop runs
        # at — so the hook never inherits a wider authority than the loop.
        gate = PermissionGate(build_policy(mode))
        runner = HookRunner(gate=gate, project_dir=project_dir, approver=approver)
        return build_tool_hooks(specs, runner)

    return _factory


def build_extensibility_hooks(
    *,
    project_dir: Path,
    paths: PathsConfig,
    mode: PermissionMode,
    approver: Approver | None = None,
) -> tuple[ToolHook | None, ToolHook | None]:
    """Build ``(pre_tool_hook, post_tool_hook)`` for a single fixed ``mode``.

    The eager convenience over :func:`build_extensibility_hook_factory`: the
    plan/build loops each run at exactly one mode and never re-clamp, so
    they build their hooks once. A **missing file yields no hooks**
    (``(None, None)``); a malformed file is **fail-closed** (the
    :class:`HookConfigError` propagates from the factory build).

    Delegation must NOT use this entry point with the parent's mode — it
    would escalate a child hook to the parent's authority. The
    :class:`~carve.core.agents.delegation.SubagentRunner` takes the factory
    and calls it at ``child_mode`` instead.
    """
    factory = build_extensibility_hook_factory(
        project_dir=project_dir, paths=paths, approver=approver
    )
    return factory(mode)


def build_extensibility_post_build_hook(
    *,
    project_dir: Path,
    paths: PathsConfig,
    mode: PermissionMode = PermissionMode.BUILD,
    approver: Approver | None = None,
) -> LifecycleHook | None:
    """Build the ``post_build`` lifecycle hook from ``carve/hooks.toml``.

    The lifecycle analogue of :func:`build_extensibility_hooks`: load the
    hooks config once (through the same fail-closed boundary the tool-hook
    factory uses), build a :class:`HookRunner` over a
    :class:`PermissionGate` for ``build_policy(mode)`` (``BUILD`` by
    default — the build flow's mode), and return
    :func:`~carve.core.hooks.wiring.build_post_build_hook` over the parsed
    specs. A ``post_build`` hook command therefore runs through the **same
    bash gate** at BUILD a build-flow tool call would.

    A **missing file yields no hook** (``None`` — the build flow skips the
    call); a **malformed file is fail-closed** (the :class:`HookConfigError`
    propagates so the bad config aborts the run rather than silently
    dropping the hook).
    """
    specs = _load_hook_specs_fail_closed(project_dir, paths)
    if not specs:
        return None
    gate = PermissionGate(build_policy(mode))
    runner = HookRunner(gate=gate, project_dir=project_dir, approver=approver)
    return build_post_build_hook(specs, runner)


def build_extensibility_on_run_failed_hook(
    *,
    project_dir: Path,
    paths: PathsConfig,
    mode: PermissionMode = PermissionMode.DEPLOY,
    approver: Approver | None = None,
) -> LifecycleHook | None:
    """Build the ``on_run_failed`` lifecycle hook from ``carve/hooks.toml``.

    The runtime sibling of :func:`build_extensibility_post_build_hook`: load the
    hooks config once (through the same fail-closed boundary), build a
    :class:`HookRunner` over a :class:`PermissionGate` for ``build_policy(mode)``,
    and return :func:`~carve.core.hooks.wiring.build_on_run_failed_hook` over the
    parsed specs. The runtime worker fires the returned callable at its
    ``run.failed`` transition, post-event (a raise is logged, the run stays
    terminal-failed).

    **Mode floor — flagged for the security reviewer.** ``post_build`` gates at
    ``BUILD`` (the build flow's inherited mode). The runtime is **not** an agent
    loop, so there is no inherited mode; ``read_only`` is wrong because the
    archetypal ``on_run_failed`` hook is a **notify** (`notify-slack`, a
    PagerDuty ping) that must reach the network. Raw ``curl``/``wget`` are in
    ``_ALWAYS_DENY`` (every mode), so the **only** network-reaching commands the
    gate ever admits are the deploy-tier prompt set (``gh``/``git push``/``dlt
    deploy``), reachable **only at ``DEPLOY``** and there only via an approver
    prompt. So ``DEPLOY`` is the narrowest mode at which a notify hook can reach
    the network at all — hence the floor here. The CLI passes **no approver**, so
    a deploy-tier prompt fails closed (denied) until a future slice wires a
    runtime approver; the metacharacter screen + argv allowlist apply unchanged.
    The mode is a parameter so the security reviewer can confirm/narrow the floor
    without touching call sites.

    A **missing file yields no hook** (``None`` — the worker skips the call); a
    **malformed file is fail-closed** (the :class:`HookConfigError` propagates).
    """
    specs = _load_hook_specs_fail_closed(project_dir, paths)
    if not specs:
        return None
    gate = PermissionGate(build_policy(mode))
    runner = HookRunner(gate=gate, project_dir=project_dir, approver=approver)
    return build_on_run_failed_hook(specs, runner)


def build_skill_pack_tool(
    *,
    project_dir: Path,
    paths: PathsConfig,
) -> Tool:
    """Build the ``lookup_skill_pack`` content-injection tool.

    Discovers packs under ``project_dir / paths.skills_dir`` and returns
    the on-demand lookup tool the agent is constructed with, so a
    description-matched pack is loadable at runtime. Discovery is inert
    (no bundled script runs at load); the tool reads a pack's instructions
    only when the agent calls it.
    """
    skills_dir = (project_dir / paths.skills_dir).resolve()
    library = discover_pack_roots(skills_dir=skills_dir)
    return library.make_lookup_tool()


def resolve_agent_or_fallback(
    *,
    project_dir: Path,
    paths: PathsConfig,
    classification: str | None = None,
    override: str | None = None,
) -> str | None:
    """Route to a declarative agent name, or ``None`` to use the M1 path.

    Builds the discovery registry (built-ins + the user ``agents_dir``) and
    calls :func:`select_agent`. Returns the chosen agent **name** when a
    declarative agent matches; otherwise behaves per the *kind* of miss
    :func:`select_agent` distinguishes:

    * **A clean classification miss** — no ``override`` was given and no
      agent handles the classification — returns ``None`` so the caller
      falls through to its existing hardcoded dispatch. The fallback is what
      keeps the M1 extract-load plan/build flow green while ``builtin/`` is
      still empty.
    * **An explicit override naming a nonexistent agent** — the user asked
      for an agent by name that is not registered — **fails loud**: the
      :class:`NoAgentMatch` propagates. Silently falling back here would run
      the *wrong* agent (the M1 default) for an explicit user request, the
      exact mis-route ``routing.py``'s contract forbids. The user must learn
      their override is a typo / unknown, not get a surprise default.

    Passing neither ``classification`` nor ``override`` returns ``None``
    (nothing to route on → fall back) rather than raising — the caller's
    hardcoded path is always the safe default here.
    """
    if classification is None and override is None:
        return None
    agents_dir = (project_dir / paths.agents_dir).resolve()
    registry = AgentDiscovery.for_project(agents_dir=agents_dir).build_registry()
    try:
        return select_agent(registry, classification=classification, override=override)
    except NoAgentMatch:
        if override is not None:
            # An explicit override that did not resolve is a user error, not
            # a routing fall-through: fail loud (don't run the M1 default for
            # an agent the user named). This is the one NoAgentMatch case
            # select_agent raises for an override — a present-but-unknown
            # name — so re-raising here cannot mask a classification miss.
            logger.warning(
                "Requested agent override %r is not registered; failing loud "
                "rather than falling back to the built-in dispatch.",
                override,
            )
            raise
        # A clean classification miss (no override): fall back to the
        # caller's hardcoded dispatch (the M1 plan/build flow), preserving
        # existing behavior.
        logger.debug(
            "No declarative agent matched classification %r; falling back "
            "to the built-in dispatch.",
            classification,
        )
        return None


__all__ = [
    "HookFactory",
    "build_extensibility_hook_factory",
    "build_extensibility_hooks",
    "build_extensibility_on_run_failed_hook",
    "build_extensibility_post_build_hook",
    "build_skill_pack_tool",
    "resolve_agent_or_fallback",
]
