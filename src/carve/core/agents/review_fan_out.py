"""The orchestrator-owned review fan-out driver — a dormant seam.

After the dlt-engineer authors a diff, the **orchestrator** (not the
engineer — the engineer never self-delegates, per the harness depth
model: orchestrator → engineer, then orchestrator → reviewers as
siblings) routes that diff through two read-only adversarial reviewers,
``dlt-qa`` then ``dlt-security``, **sequentially**, on a fresh
context-isolated read. Each reviewer sees only ``{diff, goal}`` — never
the engineer's transcript — and submits its findings as a structured
``submit_result`` payload that the driver parses and aggregates.

This module lands the driver as a **wired-but-dormant seam**: the live
goal-routing that constructs the call in production is the deferred
orchestrator-wiring unit (blocked on the plan-build classifier). Here the
driver is parameterized by an injected :data:`DelegateFn` — in production
the orchestrator supplies a partial over the real
:meth:`SubagentRunner.run`; tests supply a stub. The driver only reads a
reviewer's :class:`~carve.core.agents.delegation.DelegationResult`
``.outputs`` (the validated findings payload) and ``.status`` (to surface
a reviewer that itself returns ``needs_user_input``).

The parser is **fail-loud** on a malformed payload, mirroring the tool
binder's convention: a reviewer's findings are either well-formed or the
driver raises — a malformed verdict is never silently dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from carve.core.agents.delegation import DelegationResult

# The two reviewers, in their fixed sequence (dlt-qa first, then
# dlt-security). Hardcoded names — the driver routes by name through the
# injected delegate; it does not import the reviewer agent files.
QA_REVIEWER = "dlt-qa"
SECURITY_REVIEWER = "dlt-security"
_REVIEWER_SEQUENCE: tuple[str, ...] = (QA_REVIEWER, SECURITY_REVIEWER)


class ReviewFanOutError(Exception):
    """Raised when a reviewer's findings payload is malformed or a reviewer
    itself needs user input — a verdict the driver cannot aggregate. Fail
    loud (mirroring the tool binder) rather than silently drop the finding."""


class Severity(StrEnum):
    """Review-finding severity, narrow→wide impact.

    Aligned to the project's existing review vocabulary
    (BLOCKER/MAJOR/MINOR, plus ``info`` for non-actionable notes). A
    ``blocker`` or ``major`` finding fails the review.
    """

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


# A finding at or above this severity fails the review.
_FAILING_SEVERITIES: frozenset[Severity] = frozenset({Severity.BLOCKER, Severity.MAJOR})


class Finding(BaseModel):
    """A single structured review finding from one reviewer.

    Frozen and ``extra="forbid"``: a reviewer's payload either matches this
    shape exactly or the parser raises. ``line`` is optional (a
    file-level finding has none); ``suggested_change`` is optional (a flag
    without a concrete fix)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer: str
    severity: Severity
    file: str
    line: int | None = None
    message: str
    suggested_change: str | None = None


class ReviewResult(BaseModel):
    """The aggregate verdict across both reviewers.

    ``findings`` is every finding in reviewer order (dlt-qa then
    dlt-security); ``by_reviewer`` is the same findings keyed by reviewer
    name; ``passed`` is ``True`` iff no finding is a ``blocker`` or
    ``major``. ``raw`` keeps each reviewer's full
    :class:`DelegationResult` so the orchestrator can inspect status,
    cost, and summary when it wires the live fix loop."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    findings: list[Finding]
    passed: bool
    by_reviewer: dict[str, list[Finding]]
    raw: dict[str, DelegationResult]


# The injected delegation seam. Matches :meth:`SubagentRunner.run`'s
# call shape as a partial the orchestrator binds in production:
# ``(agent, task, context) -> DelegationResult``. The driver supplies the
# agent name, a task string, and the ``{diff, goal}`` context bundle.
DelegateFn = Callable[[str, str, dict[str, Any]], DelegationResult]


def _parse_findings(reviewer: str, result: DelegationResult) -> list[Finding]:
    """Parse one reviewer's ``DelegationResult.outputs`` into findings.

    Fail-loud (mirroring the tool binder): a reviewer that returned
    ``needs_user_input``, or whose ``outputs`` does not carry a list of
    well-formed findings, raises :class:`ReviewFanOutError` rather than
    being silently treated as a clean pass.
    """
    if result.status == "needs_user_input":
        raise ReviewFanOutError(
            f"Reviewer {reviewer!r} returned needs_user_input; the orchestrator "
            "must resolve the question before the review can aggregate."
        )

    raw_findings = result.outputs.get("findings")
    if raw_findings is None:
        raise ReviewFanOutError(
            f"Reviewer {reviewer!r} payload is missing the required 'findings' key; "
            f"got keys {sorted(result.outputs)}."
        )
    if not isinstance(raw_findings, list):
        raise ReviewFanOutError(
            f"Reviewer {reviewer!r} 'findings' must be a list, got {type(raw_findings).__name__}."
        )

    findings: list[Finding] = []
    for index, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            raise ReviewFanOutError(
                f"Reviewer {reviewer!r} finding #{index} must be an object, "
                f"got {type(item).__name__}."
            )
        # Stamp the reviewer name from the trusted call site, not the payload —
        # a reviewer cannot attribute a finding to a different reviewer.
        try:
            findings.append(Finding(**{**item, "reviewer": reviewer}))
        except ValidationError as exc:
            raise ReviewFanOutError(
                f"Reviewer {reviewer!r} finding #{index} is malformed: {exc}."
            ) from exc
    return findings


def review_fan_out(diff: str, goal: str, delegate_fn: DelegateFn) -> ReviewResult:
    """Route ``diff`` through dlt-qa then dlt-security and aggregate findings.

    The orchestrator-owned driver. Runs the two reviewers **sequentially**
    (dlt-qa first), each on a fresh adversarial context built from
    ``{diff, goal}`` **only** — never the engineer's transcript. Parses
    each reviewer's structured findings (fail-loud on a malformed payload),
    then aggregates into a :class:`ReviewResult` whose ``passed`` is
    ``True`` iff no reviewer raised a ``blocker`` or ``major`` finding.

    ``delegate_fn`` is the injection seam: in production the orchestrator
    binds a partial over the real ``SubagentRunner.run``; tests pass a
    stub. This function constructs **no** live runner and owns **no**
    goal-routing — that is the deferred orchestrator-wiring unit.
    """
    context = {"diff": diff, "goal": goal}

    all_findings: list[Finding] = []
    by_reviewer: dict[str, list[Finding]] = {}
    raw: dict[str, DelegationResult] = {}

    for reviewer in _REVIEWER_SEQUENCE:
        task = (
            f"Adversarially review the engineer's diff for the goal below. "
            f"Report findings as structured outputs; do not edit. Reviewer: {reviewer}."
        )
        # Fresh context per reviewer — the same {diff, goal}, nothing else.
        result = delegate_fn(reviewer, task, dict(context))
        raw[reviewer] = result
        findings = _parse_findings(reviewer, result)
        by_reviewer[reviewer] = findings
        all_findings.extend(findings)

    passed = not any(f.severity in _FAILING_SEVERITIES for f in all_findings)

    return ReviewResult(
        findings=all_findings,
        passed=passed,
        by_reviewer=by_reviewer,
        raw=raw,
    )


__all__ = [
    "QA_REVIEWER",
    "SECURITY_REVIEWER",
    "DelegateFn",
    "Finding",
    "ReviewFanOutError",
    "ReviewResult",
    "Severity",
    "review_fan_out",
]
