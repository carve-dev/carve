"""Plan cost + runtime synthesis — the seam Unit 2's live delegation feeds.

A Plan surfaces two synthesised numbers (see `plan-build` spec
§"The Plan entity" / §"Plan synthesis"):

* the **exact LLM cost** — known precisely, summed from each subagent's
  :class:`~carve.core.agents.delegation.DelegationResult` (its ``usage``
  :class:`~carve.core.agents.loop.TokenUsage` + ``cost_usd``); and
* an **estimated runtime** (first run vs. subsequent — e.g. "~25 min
  first load / <1 min incremental"), composed from the engineers'
  ``expected_outputs`` duration hints.

This module is the pure home for both. It is dependency-light (it imports
only the ``DelegationResult`` / ``TokenUsage`` types) and is built to
accept the live ``DelegationResult``s Unit 2 will produce; this unit
exercises it with synthetic ones. The orchestrator's monolithic single
``AgentLoop`` path routes through :func:`roll_up_cost` too — today a
single-element rollup over the one loop's usage, a multi-element rollup
once live per-subagent delegation lands.

**Invariant — no warehouse-dollar figure.** The rollup surfaces LLM cost
(known to the cent) and a runtime *duration* estimate only. It never
emits a warehouse compute-dollar number: warehouse spend depends on data
volume + warehouse size Carve can't predict, so honesty beats a fake
figure (UC1). :class:`CostRollup` has no field that could carry one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from carve.core.agents.delegation import DelegationResult
from carve.core.agents.loop import TokenUsage

# Keys an engineer's ``submit_result`` ``outputs`` may carry to hint at
# runtime. Read leniently — any may be absent (then we degrade), and a
# present value must be a positive number to count.
_FIRST_RUN_KEYS: tuple[str, ...] = (
    "first_run_seconds",
    "first_load_seconds",
    "full_refresh_seconds",
)
_SUBSEQUENT_KEYS: tuple[str, ...] = (
    "subsequent_run_seconds",
    "incremental_seconds",
    "incremental_run_seconds",
)


@dataclass
class RuntimeEstimate:
    """A composed runtime estimate (durations only — never dollars).

    ``first_run_seconds`` / ``subsequent_run_seconds`` are summed across
    the subagents that carried a hint; ``None`` means no subagent
    supplied that half, so the surface omits it (graceful degradation).
    ``has_estimate`` is True when at least one half is known.
    """

    first_run_seconds: float | None = None
    subsequent_run_seconds: float | None = None

    @property
    def has_estimate(self) -> bool:
        return self.first_run_seconds is not None or self.subsequent_run_seconds is not None

    def render(self) -> str | None:
        """Human one-liner, or ``None`` when nothing was estimable.

        e.g. ``"~25 min first load / <1 min subsequent"``. Returns
        ``None`` when no hint was present so the caller omits the line
        entirely rather than printing a hollow "unknown".
        """
        if not self.has_estimate:
            return None
        parts: list[str] = []
        if self.first_run_seconds is not None:
            parts.append(f"~{_human_duration(self.first_run_seconds)} first run")
        if self.subsequent_run_seconds is not None:
            parts.append(f"~{_human_duration(self.subsequent_run_seconds)} subsequent")
        return " / ".join(parts)


@dataclass
class CostRollup:
    """The Plan's synthesised cost + runtime estimate.

    ``usage`` is the summed :class:`TokenUsage` across every subagent;
    ``cost_usd`` is the summed exact LLM cost. ``runtime`` is the
    composed duration estimate. There is **no warehouse-dollar field** —
    by design (see the module docstring invariant).
    """

    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    runtime: RuntimeEstimate = field(default_factory=RuntimeEstimate)


def roll_up_cost(results: Sequence[DelegationResult]) -> CostRollup:
    """Sum the subagents' usage + cost and compose their runtime estimate.

    The Plan's exact LLM cost is the sum of each ``DelegationResult``'s
    ``cost_usd``; the token totals sum each result's ``usage`` field by
    field. An empty sequence yields a zero rollup (no subagents ran).
    Built to accept Unit 2's live ``DelegationResult``s; exercised this
    unit with synthetic ones.
    """
    usage = TokenUsage()
    total_cost = 0.0
    for result in results:
        usage.input_tokens += result.usage.input_tokens
        usage.output_tokens += result.usage.output_tokens
        usage.cache_creation_tokens += result.usage.cache_creation_tokens
        usage.cache_read_tokens += result.usage.cache_read_tokens
        total_cost += result.cost_usd
    runtime = compose_runtime_estimate(results)
    return CostRollup(usage=usage, cost_usd=total_cost, runtime=runtime)


def compose_runtime_estimate(results: Sequence[DelegationResult]) -> RuntimeEstimate:
    """Compose a first-run/subsequent estimate from the engineers' outputs.

    Reads duration hints off each ``DelegationResult.outputs`` (the
    validated ``submit_result`` payload — the engineers' ``expected_outputs``
    in spec terms). Degrades gracefully: a half with no hint from any
    subagent stays ``None`` and the surface omits it. The first-run and
    subsequent halves are summed independently across subagents (a
    pipeline's total first load is the sum of its stages' first loads).
    """
    first_total: float | None = None
    subsequent_total: float | None = None
    for result in results:
        first = _extract_duration(result.outputs, _FIRST_RUN_KEYS)
        if first is not None:
            first_total = (first_total or 0.0) + first
        subsequent = _extract_duration(result.outputs, _SUBSEQUENT_KEYS)
        if subsequent is not None:
            subsequent_total = (subsequent_total or 0.0) + subsequent
    return RuntimeEstimate(
        first_run_seconds=first_total,
        subsequent_run_seconds=subsequent_total,
    )


def _extract_duration(outputs: dict[str, Any], keys: Sequence[str]) -> float | None:
    """Return the first positive numeric duration under ``keys``, else None.

    Bools are rejected (``isinstance(True, int)`` is True in Python, and a
    flag is not a duration). Non-positive values are ignored — a zero or
    negative "duration" is treated as absent rather than counted.
    """
    for key in keys:
        value = outputs.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _human_duration(seconds: float) -> str:
    """Render a duration as a coarse human string (``<1 min`` / ``25 min`` / ``2 h``)."""
    if seconds < 60:
        return "<1 min"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)} min"
    hours = minutes / 60
    return f"{round(hours, 1):g} h"


__all__ = [
    "CostRollup",
    "RuntimeEstimate",
    "compose_runtime_estimate",
    "roll_up_cost",
]
