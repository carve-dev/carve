"""``carve metrics`` — the DB-backed rollup service (cost / runs / agents).

:class:`MetricsRollups` is the aggregation behind ``carve metrics costs|runs|
agents`` (and, in Increment 5, the ``GET /metrics/*`` routers that wire onto the
same service — this is their reusable seam). It reads three sources:

* **costs** — token→USD over ``agent_invocations``. The per-model price is
  applied at *record* time (``DelegationResult.cost_usd`` = ``compute_cost_usd``
  via :class:`~carve.core.agents.loop.TokenUsage`), so the rollup sums the stored
  ``cost_usd`` — no parallel price table (the ``cost_rollup.py`` "no fake figure"
  honesty precedent). Warehouse-credit accounting (Snowflake ``QUERY_HISTORY``,
  ties to dbt-execution/sql) is a deliberate later extension — tokens→USD only
  this slice.
* **runs** — success/failure counts + median/p95 duration + a by-pipeline/target
  breakdown over the **existing** ``runs`` rows (no new table; ``Run`` already
  carries ``status``/``duration_ms``/``pipeline_name``/``target``).
* **agents** — per-agent invocation counts + token/cost totals + success rate +
  skill-call mix (``agent_invocations`` ⋈ ``skill_calls``).

This is distinct from ``carve.cli.orchestrator.cost_rollup.CostRollup``, which is
the in-memory, per-Plan cost synthesis for the plan-build surface; the two are
not merged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa

from carve.core.state.models import AgentInvocation, Run, SkillCall

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

# The terminal ``Run.status`` for a successful run (the M1 run vocabulary is
# "success", NOT the delegation "succeeded" — see the archiver's note).
_SUCCESS = "success"
# The statuses ``Run.status`` reaches that count as terminal failure (the
# ``Repository.update_run_status`` terminal set minus ``success``).
_FAILURE_STATUSES = ("failed", "crashed", "cancelled")
# The delegation "succeeded" status (``DelegationResult.status``).
_SUCCEEDED = "succeeded"

_SINCE_UNITS: dict[str, int] = {
    "m": 60,
    "h": 60 * 60,
    "d": 60 * 60 * 24,
    "w": 60 * 60 * 24 * 7,
}
_SINCE_RE = re.compile(r"^(\d+)\s*([mhdw])$")


def parse_since(value: str) -> datetime:
    """Parse a ``--since`` window (e.g. ``7d``/``24h``/``30m``/``2w``) into a cutoff.

    Returns an aware UTC datetime ``now - delta``. Raises :class:`ValueError` on a
    malformed value so the CLI can exit cleanly.
    """
    match = _SINCE_RE.match(value.strip().lower())
    if match is None:
        raise ValueError(f"invalid --since window {value!r}; use e.g. 7d, 24h, 30m, or 2w")
    amount = int(match.group(1))
    seconds = amount * _SINCE_UNITS[match.group(2)]
    return datetime.now(UTC) - timedelta(seconds=seconds)


@dataclass(frozen=True)
class CostsRollup:
    """Token→USD rollup over ``agent_invocations`` in a window."""

    invocations: int
    tokens_input: int
    tokens_output: int
    cost_usd: float


@dataclass(frozen=True)
class TargetRuns:
    """Run counts for one ``(pipeline_name, target)`` group."""

    pipeline_name: str | None
    target: str | None
    total: int
    succeeded: int
    failed: int


@dataclass(frozen=True)
class RunsRollup:
    """Success/failure + duration rollup over the ``runs`` table in a window."""

    total: int
    succeeded: int
    failed: int
    median_duration_ms: float | None
    p95_duration_ms: float | None
    by_target: list[TargetRuns] = field(default_factory=list)


@dataclass(frozen=True)
class AgentUsage:
    """Per-agent usage rollup over ``agent_invocations`` (+ its skill calls)."""

    agent_name: str
    invocations: int
    tokens_input: int
    tokens_output: int
    cost_usd: float
    succeeded: int
    skill_calls: int

    @property
    def success_rate(self) -> float:
        """Fraction of invocations that reached ``status='succeeded'`` (0.0 if none)."""
        return self.succeeded / self.invocations if self.invocations else 0.0


class MetricsRollups:
    """DB-backed aggregation behind ``carve metrics``.

    Construct once from the same ``sessionmaker`` as the other state-store repos;
    each method opens a short read transaction. ``since`` filters the window
    (``None`` = all time).
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def costs(self, since: datetime | None = None) -> CostsRollup:
        """Sum tokens + USD over ``agent_invocations`` since ``since``."""
        stmt = sa.select(
            sa.func.count(AgentInvocation.id),
            sa.func.coalesce(sa.func.sum(AgentInvocation.tokens_input), 0),
            sa.func.coalesce(sa.func.sum(AgentInvocation.tokens_output), 0),
            sa.func.coalesce(sa.func.sum(AgentInvocation.cost_usd), 0.0),
        )
        if since is not None:
            stmt = stmt.where(AgentInvocation.started_at >= since)
        with self._session_factory() as session:
            invocations, tokens_input, tokens_output, cost_usd = session.execute(stmt).one()
        return CostsRollup(
            invocations=int(invocations),
            tokens_input=int(tokens_input),
            tokens_output=int(tokens_output),
            cost_usd=float(cost_usd),
        )

    def runs(self, since: datetime | None = None) -> RunsRollup:
        """Success/failure + median/p95 duration + by-target over ``runs``.

        Aggregated **in Postgres** (counts via ``count`` / ``sum(case…)``,
        median/p95 via ``percentile_cont(…).within_group(duration_ms)``,
        by-target via ``GROUP BY``) — the service default is unbounded
        (``since=None``) and the Increment-5 ``/metrics/runs`` REST seam calls
        this directly, so a growing ``runs`` table must never be pulled fully
        into memory. ``percentile_cont`` **interpolates** (nearest-rank differs
        on small even-N sets) and, like every SQL aggregate, ignores ``NULL``
        durations — matching the old Python filter.
        """
        succeeded_expr = sa.func.coalesce(
            sa.func.sum(sa.case((Run.status == _SUCCESS, 1), else_=0)), 0
        )
        failed_expr = sa.func.coalesce(
            sa.func.sum(sa.case((Run.status.in_(_FAILURE_STATUSES), 1), else_=0)), 0
        )
        median_expr = sa.func.percentile_cont(0.5).within_group(Run.duration_ms.asc())
        p95_expr = sa.func.percentile_cont(0.95).within_group(Run.duration_ms.asc())

        summary_stmt = sa.select(
            sa.func.count(), succeeded_expr, failed_expr, median_expr, p95_expr
        )
        group_stmt = sa.select(
            Run.pipeline_name, Run.target, sa.func.count(), succeeded_expr, failed_expr
        ).group_by(Run.pipeline_name, Run.target)
        if since is not None:
            summary_stmt = summary_stmt.where(Run.created_at >= since)
            group_stmt = group_stmt.where(Run.created_at >= since)

        with self._session_factory() as session:
            total, succeeded, failed, median, p95 = session.execute(summary_stmt).one()
            group_rows = session.execute(group_stmt).all()

        # Preserve the (pipeline_name, target) ordering with None treated as "".
        by_target = [
            TargetRuns(
                pipeline_name=pipeline_name,
                target=target,
                total=int(g_total),
                succeeded=int(g_succeeded),
                failed=int(g_failed),
            )
            for pipeline_name, target, g_total, g_succeeded, g_failed in sorted(
                group_rows, key=lambda r: (r[0] or "", r[1] or "")
            )
        ]

        return RunsRollup(
            total=int(total),
            succeeded=int(succeeded),
            failed=int(failed),
            median_duration_ms=float(median) if median is not None else None,
            p95_duration_ms=float(p95) if p95 is not None else None,
            by_target=by_target,
        )

    def agents(self, since: datetime | None = None) -> list[AgentUsage]:
        """Per-agent invocation counts + token/cost totals + success rate + skill mix."""
        inv_stmt = sa.select(
            AgentInvocation.agent_name,
            sa.func.count(AgentInvocation.id),
            sa.func.coalesce(sa.func.sum(AgentInvocation.tokens_input), 0),
            sa.func.coalesce(sa.func.sum(AgentInvocation.tokens_output), 0),
            sa.func.coalesce(sa.func.sum(AgentInvocation.cost_usd), 0.0),
            sa.func.sum(sa.case((AgentInvocation.status == _SUCCEEDED, 1), else_=0)),
        ).group_by(AgentInvocation.agent_name)

        # Skill-call mix: count skill_calls joined back to their invocation's
        # agent, over the same window.
        skill_stmt = (
            sa.select(
                AgentInvocation.agent_name,
                sa.func.count(SkillCall.id),
            )
            .select_from(SkillCall)
            .join(AgentInvocation, SkillCall.agent_invocation_id == AgentInvocation.id)
            .group_by(AgentInvocation.agent_name)
        )
        if since is not None:
            inv_stmt = inv_stmt.where(AgentInvocation.started_at >= since)
            skill_stmt = skill_stmt.where(AgentInvocation.started_at >= since)

        with self._session_factory() as session:
            inv_rows = session.execute(inv_stmt).all()
            skill_rows = session.execute(skill_stmt).all()

        skill_counts = {name: int(count) for name, count in skill_rows}
        usages = [
            AgentUsage(
                agent_name=agent_name,
                invocations=int(invocations),
                tokens_input=int(tokens_input),
                tokens_output=int(tokens_output),
                cost_usd=float(cost_usd),
                succeeded=int(succeeded or 0),
                skill_calls=skill_counts.get(agent_name, 0),
            )
            for (
                agent_name,
                invocations,
                tokens_input,
                tokens_output,
                cost_usd,
                succeeded,
            ) in inv_rows
        ]
        # Busiest agents first, then name for stable ordering.
        usages.sort(key=lambda u: (-u.invocations, u.agent_name))
        return usages


__all__ = [
    "AgentUsage",
    "CostsRollup",
    "MetricsRollups",
    "RunsRollup",
    "TargetRuns",
    "parse_since",
]
