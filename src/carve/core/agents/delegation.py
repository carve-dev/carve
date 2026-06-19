"""Subagent delegation — sync, sequential, mode-clamped, isolated.

:func:`delegate` (and the :class:`SubagentRunner` behind it) spawns a
**fresh, synchronous** :class:`AgentLoop` for a named built-in agent and
returns a structured :class:`DelegationResult`. The four guarantees the
harness spec puts on this path:

1. **Mode clamp.** The child runs at ``min(parent_mode, agent_capability)``
   and never wider. A ``read_only`` parent delegating to a build-capable
   engineer runs the child ``read_only`` — no write during an ``ask``.
2. **Tool attenuation.** The child's tool set is its factory's tools
   intersected with the clamped mode's permitted set; the same gate that
   guards the parent guards the child, so a write tool is simply absent
   below ``build``.
3. **Context isolation.** The child sees a **typed context bundle**
   (named keys the runner passes), *never* the parent's transcript. Its
   raw tool output never flows back to the parent — only the structured
   ``DelegationResult`` does. The orchestrator's context therefore does
   not grow by the subagent's turns.
4. **Harness-tracked ``files_changed``.** Read from the child loop's own
   edit/create log, not from the model's ``submit_result`` (which carries
   ``outputs``, not the file list).

Execution is **sequential and sync** — ``delegate`` blocks until the
child loop returns. Concurrent fan-out is a later increment.
``max_delegation_depth = 2`` is enforced so an engineer a parent spawned
cannot itself spawn another engineer ad infinitum.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from carve.core.agents.exceptions import AgentError
from carve.core.agents.loop import AgentLoop, TokenUsage
from carve.core.agents.observer import AgentObserver, NullObserver
from carve.core.agents.permissions.gate import Approver, PermissionGate
from carve.core.agents.permissions.modes import PermissionMode, min_mode
from carve.core.agents.permissions.policy import AgentPolicy, build_policy
from carve.core.agents.subagent_registry import AgentSpec, SubagentRegistry
from carve.core.agents.tools import Tool
from carve.core.agents.tools.submit_result import (
    SubmitResultCapture,
    make_submit_result_tool,
)
from carve.core.config.paths import ProjectPaths

MAX_DELEGATION_DEPTH = 2


class SubagentError(AgentError):
    """Raised when a delegation cannot run (unknown agent, depth exceeded,
    or the child finished without calling ``submit_result``)."""


@dataclass
class DelegationResult:
    """The structured outcome the parent receives from a subagent.

    Mirrors the spec's dataclass. ``usage`` reuses the loop's
    :class:`TokenUsage` directly (no separate state type), and ``cost_usd``
    is the child's cost so the parent can aggregate it against its own
    ceiling. ``files_changed`` is harness-tracked; ``outputs`` is the
    validated ``submit_result`` payload.
    """

    status: str  # "succeeded" | "needs_user_input" | "failed"
    result_summary: str
    files_changed: list[str]
    outputs: dict[str, Any]
    usage: TokenUsage
    cost_usd: float


class SubagentRunner:
    """Builds and runs a single subagent loop with the harness guarantees.

    Construct one with the registry, the project paths, an Anthropic
    client, and the model id; call :meth:`run` per delegation. Stateless
    across calls apart from the injected collaborators.
    """

    def __init__(
        self,
        *,
        registry: SubagentRegistry,
        paths: ProjectPaths,
        client: Any,
        model: str,
        observer: AgentObserver | None = None,
        approver: Approver | None = None,
        max_turns: int = 30,
        max_tokens: int = 4096,
    ) -> None:
        self._registry = registry
        self._paths = paths
        self._client = client
        self._model = model
        self._observer = observer if observer is not None else NullObserver()
        self._approver = approver
        self._max_turns = max_turns
        self._max_tokens = max_tokens

    def run(
        self,
        agent: str,
        task: str,
        context: dict[str, Any],
        *,
        parent_mode: PermissionMode,
        depth: int = 1,
    ) -> DelegationResult:
        """Run ``agent`` against ``task`` at ``min(parent_mode, capability)``."""
        if depth > MAX_DELEGATION_DEPTH:
            raise SubagentError(
                f"Delegation depth {depth} exceeds max ({MAX_DELEGATION_DEPTH}); "
                "a subagent cannot spawn further subagents past this limit."
            )
        try:
            spec: AgentSpec = self._registry.resolve(agent)
        except KeyError as exc:
            raise SubagentError(f"Unknown subagent: {agent!r}.") from exc

        # 1. Clamp the mode — never wider than the parent.
        child_mode = min_mode(parent_mode, spec.capability)

        # 2. Build the agent's tools, then attenuate to the clamped mode.
        factory_tools: list[Tool] = spec.tool_factory(self._paths)
        capture = SubmitResultCapture()
        submit_tool = make_submit_result_tool(capture)
        tool_names = frozenset(t.name for t in factory_tools)
        policy = build_policy(
            child_mode,
            agent=AgentPolicy(tools=tool_names, capability=spec.capability),
        )
        gate = PermissionGate(policy)
        # The terminator is always available (it only captures a payload).
        tools = [*factory_tools, submit_tool]

        # 3. Compose the prompt with the typed context bundle — NOT the
        #    parent transcript. Named keys only.
        system_prompt = _compose_subagent_prompt(spec.system_prompt, context)

        loop = AgentLoop(
            client=self._client,
            tools=tools,
            system_prompt=system_prompt,
            model=self._model,
            max_tokens=self._max_tokens,
            observer=self._observer,
            terminator_tool="submit_result",
            gate=gate,
            approver=self._approver,
        )
        agent_result = loop.run(task, max_turns=self._max_turns)

        # 4. files_changed is read from the loop's harness-tracked log.
        files_changed = list(loop.files_changed)
        cost = agent_result.token_usage.cost_usd(self._model)

        if not capture.submitted:
            # The child never called submit_result — a failed delegation.
            return DelegationResult(
                status="failed",
                result_summary=(
                    agent_result.text
                    or "Subagent finished without calling submit_result."
                ),
                files_changed=files_changed,
                outputs={},
                usage=agent_result.token_usage,
                cost_usd=cost,
            )

        return DelegationResult(
            status=capture.status,
            result_summary=capture.summary or agent_result.text,
            files_changed=files_changed,
            outputs=capture.outputs,
            usage=agent_result.token_usage,
            cost_usd=cost,
        )


def _compose_subagent_prompt(base_prompt: str, context: dict[str, Any]) -> str:
    """Append the typed context bundle to the agent's base prompt.

    Only the named keys the caller passed are rendered — the parent
    transcript is never included. Keeps the subagent's view to exactly
    what the orchestrator chose to share.
    """
    if not context:
        return base_prompt
    lines = ["## Context", ""]
    for key in sorted(context):
        lines.append(f"### {key}")
        value = context[key]
        lines.append(str(value))
        lines.append("")
    return base_prompt + "\n\n" + "\n".join(lines).rstrip()


def delegate(
    agent: str,
    task: str,
    context: dict[str, Any],
    *,
    parent_mode: PermissionMode,
    runner: SubagentRunner,
    depth: int = 1,
) -> DelegationResult:
    """Delegate ``task`` to the named built-in ``agent`` (sync, blocking).

    Thin wrapper over :meth:`SubagentRunner.run`; the explicit ``runner``
    argument keeps the entry point pure (no global state) and matches the
    spec's signature ``delegate(agent, task, context, *, parent_mode)``
    with the runner supplied by the orchestrator.
    """
    return runner.run(agent, task, context, parent_mode=parent_mode, depth=depth)


__all__ = [
    "MAX_DELEGATION_DEPTH",
    "DelegationResult",
    "SubagentError",
    "SubagentRunner",
    "delegate",
]
