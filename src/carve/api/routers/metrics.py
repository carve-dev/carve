"""``/api/v1/metrics`` — cost / run / agent rollups (``carve metrics``).

Wraps :class:`~carve.core.observability.rollups.MetricsRollups`. ``?since=`` takes
the same duration grammar as the CLI (``7d``/``24h``/``30m``/``2w``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import get_state_store
from carve.api.errors import BadRequest

if TYPE_CHECKING:
    from carve.core.state.store import StateStore

router = APIRouter(prefix="/metrics", tags=["metrics"])


def _parse_since(since: str | None):  # type: ignore[no-untyped-def]
    if since is None:
        return None
    from carve.core.observability.rollups import parse_since

    try:
        return parse_since(since)
    except ValueError as exc:
        raise BadRequest(f"Invalid `since` value: {exc}") from exc


class CostsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invocations: int
    tokens_input: int
    tokens_output: int
    cost_usd: float


class TargetRunsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pipeline_name: str | None
    target: str | None
    total: int
    succeeded: int
    failed: int


class RunsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total: int
    succeeded: int
    failed: int
    median_duration_ms: float | None
    p95_duration_ms: float | None
    by_target: list[TargetRunsOut]


class AgentUsageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_name: str
    invocations: int
    tokens_input: int
    tokens_output: int
    cost_usd: float
    succeeded: int
    skill_calls: int


@router.get("/costs", response_model=CostsOut)
def costs(
    since: str | None = None,
    state_store: StateStore = Depends(get_state_store),
) -> CostsOut:
    """Token→USD cost rollup."""
    return CostsOut.model_validate(state_store.metrics.costs(_parse_since(since)))


@router.get("/runs", response_model=RunsOut)
def runs(
    since: str | None = None,
    state_store: StateStore = Depends(get_state_store),
) -> RunsOut:
    """Run success/failure + duration rollup."""
    return RunsOut.model_validate(state_store.metrics.runs(_parse_since(since)))


@router.get("/agents", response_model=list[AgentUsageOut])
def agents(
    since: str | None = None,
    state_store: StateStore = Depends(get_state_store),
) -> list[AgentUsageOut]:
    """Per-agent usage rollup."""
    rollups = state_store.metrics.agents(_parse_since(since))
    return [AgentUsageOut.model_validate(a) for a in rollups]


__all__ = ["router"]
