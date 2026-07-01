"""``/api/v1/runs`` — run listing/detail/logs + the live event stream.

Wraps ``Repository.list_runs``/``get_run``/``get_logs`` (``carve runs`` /
``carve logs``). The stream is served two ways on ``/runs/{run_id}/stream``: an
SSE ``GET`` here, and a WebSocket route registered in ``main.py`` pointing at
:data:`stream_handler`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import (
    get_config,
    get_project_paths,
    get_state_store,
    pagination_params,
)
from carve.api.errors import Conflict, ResourceNotFound
from carve.api.pagination import CANDIDATE_CEILING, PageParams, order_candidates, paginate
from carve.api.schemas import Page
from carve.api.streams import run_stream_sse, run_stream_websocket

if TYPE_CHECKING:
    from carve.core.config import Config
    from carve.core.config.paths import ProjectPaths
    from carve.core.state.store import StateStore

router = APIRouter(prefix="/runs", tags=["runs"])

#: WebSocket handler for ``/api/v1/runs/{run_id}/stream`` (registered in main.py).
stream_handler = run_stream_websocket

#: Run states from which a re-run may be triggered (failed/crashed only).
_RESUMABLE_STATUSES = frozenset({"failed", "crashed"})


class RunTriggerIn(BaseModel):
    """Body for ``POST /runs`` — trigger a pipeline run."""

    pipeline_name: str
    target: str | None = None


class RunAcceptedOut(BaseModel):
    """A queued run trigger.

    Returns the **job id**, not a run id: the ``run_...`` row does not exist until
    a worker claims the job. Clients poll ``GET /api/v1/jobs/{job_id}`` for the
    claim, then ``GET /api/v1/runs`` for the run.
    """

    job_id: str
    pipeline: str
    target: str
    status: str


def _pipeline_is_runnable(name: str, paths: ProjectPaths, state_store: StateStore) -> bool:
    """Light existence pre-check for a run trigger (``enqueue_manual`` doesn't validate).

    Path-confined: a name with a separator / ``..`` / NUL or other control byte is
    never runnable (and never reaches a filesystem touch — a raw ``\\x00`` would
    otherwise raise ``ValueError: embedded null byte`` at ``.exists()`` and surface
    as a 500 instead of a clean 404). Runnable iff a ``pipelines`` row exists, or a
    ``pipelines/<name>.toml`` composition, or an ``el/<name>/`` component is present.
    """
    if "/" in name or "\\" in name or name in (".", "..") or not name:
        return False
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in name):
        return False
    if state_store.repository.get_pipeline(name) is not None:
        return True
    if (paths.pipelines_dir / f"{name}.toml").exists():
        return True
    return (paths.el_dir / name).is_dir()


def _enqueue_run(
    pipeline: str,
    target: str,
    *,
    state_store: StateStore,
    config: Config,
    paths: ProjectPaths,
) -> RunAcceptedOut:
    """Resolve ``required_label`` and enqueue a manual job (the shared trigger path).

    **Load-bearing (worker-placement integrity):** ``required_label`` MUST be
    resolved (mirroring ``carve serve``'s scheduler) — ``enqueue_manual`` upserts
    ``required_label = EXCLUDED.required_label``, so omitting it (default ``None``)
    would *unlabel* a queued row this trigger coalesces onto (e.g. a labeled
    scheduled job), letting it run on the wrong worker.
    """
    # Reuse serve.py's path-confined, unit-tested resolver so a manual trigger
    # derives the SAME label the scheduler would for this pipeline.
    from carve.cli.commands.serve import resolve_worker_label

    required_label = resolve_worker_label(
        pipeline, project_paths=paths, components=config.components
    )
    # ``enqueue_manual`` is an idempotent upsert (ON CONFLICT (pipeline, tenant_id)
    # WHERE status='queued' DO UPDATE): two concurrent identical triggers coalesce
    # onto ONE queued job and return the same id — no double-enqueue. It does not
    # raise QueuedJobAlreadyExists.
    job = state_store.jobs.enqueue_manual(
        pipeline, target, trigger="api", required_label=required_label
    )
    return RunAcceptedOut(
        job_id=job.id, pipeline=job.pipeline, target=job.target, status=job.status
    )


class RunOut(BaseModel):
    """A run row on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    target_id: str
    target: str | None
    pipeline_name: str | None
    parent_run_id: str | None
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error_message: str | None
    tokens_input: int
    tokens_output: int
    cost_usd: float
    created_at: datetime


class LogOut(BaseModel):
    """A log line on the wire."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: str
    timestamp: datetime
    level: str
    source: str
    message: str


@router.get("", response_model=Page[RunOut])
def list_runs(
    status: str | None = None,
    pipeline_name: str | None = None,
    params: PageParams = Depends(pagination_params),
    state_store: StateStore = Depends(get_state_store),
) -> Page[RunOut]:
    """List runs newest-first (cursor-paginated), optionally filtered."""
    candidates = state_store.repository.list_runs(
        status=status, pipeline_name=pipeline_name, limit=CANDIDATE_CEILING
    )
    ordered = order_candidates(candidates, id_of=lambda r: r.id, created_of=lambda r: r.created_at)
    result = paginate(ordered, params, id_of=lambda r: r.id, created_of=lambda r: r.created_at)
    return Page[RunOut].build(result, [RunOut.model_validate(r) for r in result.items])


@router.post("", response_model=RunAcceptedOut, status_code=202)
def run_pipeline(
    body: RunTriggerIn,
    state_store: StateStore = Depends(get_state_store),
    config: Config = Depends(get_config),
    paths: ProjectPaths = Depends(get_project_paths),
) -> RunAcceptedOut:
    """Trigger a pipeline run — enqueue a job (202); a worker runs it.

    Async by architecture: this enqueues onto the durable ``jobs`` queue and
    returns the job id. Honors ``Idempotency-Key`` (the middleware engages on
    POST); the domain-level job coalescing also dedupes concurrent triggers.
    """
    if not _pipeline_is_runnable(body.pipeline_name, paths, state_store):
        raise ResourceNotFound(f"Pipeline {body.pipeline_name!r} not found or not runnable.")
    target = body.target or config.project.default_target
    return _enqueue_run(
        body.pipeline_name, target, state_store=state_store, config=config, paths=paths
    )


@router.post(
    "/{run_id}/resume",
    response_model=RunAcceptedOut,
    status_code=202,
    summary="Re-run a failed run's pipeline from the start (not a checkpoint resume)",
    description=(
        "Enqueues a **fresh** run of the failed run's pipeline from the beginning "
        "— Carve has no mid-pipeline checkpoint/resume, so this does not continue "
        "the failed run where it stopped. Allowed only from a terminal "
        "failed/crashed state (else 409). Returns 202 + the queued job id."
    ),
)
def run_resume(
    run_id: str,
    state_store: StateStore = Depends(get_state_store),
    config: Config = Depends(get_config),
    paths: ProjectPaths = Depends(get_project_paths),
) -> RunAcceptedOut:
    """Re-run the pipeline of a failed run — enqueue a fresh job (202).

    There is no standalone "resume run" entrypoint in the runtime (run-retry is the
    internal auto-fix loop inside one CLI session); this maps to a fresh manual
    enqueue of the failed run's pipeline. Allowed only from a failed/crashed
    terminal state (else 409).

    NOTE(rest-api): see issue — parent-run lineage (``new_run.parent_run_id =
    run_id``) is not threadable today (``enqueue_manual`` → ``worker.create_run``
    takes no ``parent_run_id`` from a job); this ships as "re-run the pipeline",
    with lineage a small worker-side follow-up.
    """
    run = state_store.repository.get_run(run_id)
    if run is None:
        raise ResourceNotFound(f"Run {run_id!r} not found.")
    if run.status not in _RESUMABLE_STATUSES:
        raise Conflict(
            f"Run {run_id!r} is not resumable (status {run.status!r}); "
            f"only {sorted(_RESUMABLE_STATUSES)} may be re-run."
        )
    if run.pipeline_name is None:
        raise Conflict(f"Run {run_id!r} has no pipeline to re-run.")
    target = run.target or config.project.default_target
    return _enqueue_run(
        run.pipeline_name, target, state_store=state_store, config=config, paths=paths
    )


@router.get("/{run_id}", response_model=RunOut)
def get_run(
    run_id: str,
    state_store: StateStore = Depends(get_state_store),
) -> RunOut:
    """Fetch one run by id."""
    run = state_store.repository.get_run(run_id)
    if run is None:
        raise ResourceNotFound(f"Run {run_id!r} not found.")
    return RunOut.model_validate(run)


@router.get("/{run_id}/logs", response_model=list[LogOut])
def get_run_logs(
    run_id: str,
    since_id: int | None = None,
    state_store: StateStore = Depends(get_state_store),
) -> list[LogOut]:
    """Return a run's logs in insertion order (optionally after ``since_id``)."""
    run = state_store.repository.get_run(run_id)
    if run is None:
        raise ResourceNotFound(f"Run {run_id!r} not found.")
    logs = state_store.repository.get_logs(run_id, since_id=since_id)
    return [LogOut.model_validate(log) for log in logs]


@router.get("/{run_id}/stream")
async def stream_run(run_id: str, request: Request):  # type: ignore[no-untyped-def]
    """SSE event stream for a run (``Accept: text/event-stream``).

    The WebSocket transport for the same path is registered in ``main.py``.
    """
    return await run_stream_sse(request)


__all__ = ["router", "stream_handler"]
