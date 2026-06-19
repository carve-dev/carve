"""The ``submit_result`` terminator tool for delegated subagents.

A subagent ends its run by calling ``submit_result`` exactly once. The
:class:`SubmitResultCapture` records the structured ``outputs`` payload;
the :class:`SubagentRunner` reads it back into
``DelegationResult.outputs`` after the loop exits. This mirrors the
shipped ``SubmitStepCapture`` / ``SubmitDiagnosisCapture`` pattern
verbatim — a ``payload`` + a ``_called`` re-entrancy guard + a
``make_*_tool`` factory wired via the loop's ``terminator_tool``.

Note the deliberate split of responsibilities: ``outputs`` is the
agent's structured result (whatever its per-agent schema says), but the
files it changed are **not** in ``outputs`` — those are harness-tracked
from the edit/create log so the model cannot fabricate them. ``status``
and a short ``summary`` round out the terminal payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult

# The status the subagent self-reports; the runner maps it onto
# DelegationResult.status (with "failed" as the fallback if absent).
_VALID_STATUS = frozenset({"succeeded", "needs_user_input", "failed"})


SUBMIT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": sorted(_VALID_STATUS),
            "description": (
                "succeeded / needs_user_input / failed — the orchestrator "
                "branches on this."
            ),
        },
        "summary": {
            "type": "string",
            "description": "One- to three-sentence summary of the outcome.",
        },
        "outputs": {
            "type": "object",
            "description": (
                "Structured result payload (per-agent schema). Do NOT list "
                "changed files here — the harness tracks those itself."
            ),
        },
    },
    "required": ["status", "summary"],
}


@dataclass
class SubmitResultCapture:
    """Captures the subagent's ``submit_result`` payload.

    Same shape as ``SubmitStepCapture``: a stored ``payload`` and a
    ``_called`` guard so a second invocation in one turn is rejected.
    """

    payload: dict[str, Any] | None = None
    _called: bool = field(default=False, init=False)

    @property
    def submitted(self) -> bool:
        return self.payload is not None

    @property
    def status(self) -> str:
        if self.payload is None:
            return "failed"
        value = self.payload.get("status")
        return value if value in _VALID_STATUS else "failed"

    @property
    def summary(self) -> str:
        if self.payload is None:
            return ""
        value = self.payload.get("summary")
        return value if isinstance(value, str) else ""

    @property
    def outputs(self) -> dict[str, Any]:
        if self.payload is None:
            return {}
        value = self.payload.get("outputs")
        return dict(value) if isinstance(value, dict) else {}


def make_submit_result_tool(capture: SubmitResultCapture) -> Tool:
    """Build a ``submit_result`` tool that records the payload on ``capture``."""

    def _execute(input_: ToolInput) -> ToolResult:
        if capture._called:
            raise ToolExecutionError(
                "submit_result already called; only one terminal payload "
                "may be submitted per delegation."
            )
        if not isinstance(input_, dict):
            raise ToolExecutionError("submit_result input must be an object.")
        capture.payload = dict(input_)
        capture._called = True
        return {"status": "submitted"}

    return Tool(
        name="submit_result",
        description=(
            "Finalize this delegated task. Call exactly once with the "
            "status, a short summary, and any structured outputs. The loop "
            "terminates after this call. Do not list changed files — they "
            "are tracked for you."
        ),
        input_schema=SUBMIT_RESULT_SCHEMA,
        executor=_execute,
    )


__all__ = [
    "SUBMIT_RESULT_SCHEMA",
    "SubmitResultCapture",
    "make_submit_result_tool",
]
