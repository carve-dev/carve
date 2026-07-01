"""Worker placement above the queue: the reducer, the scheduler stamp, the pool.

Three layers:

* **The pure reducer** ``resolve_required_label`` (no DB) — the single source of
  the per-pipeline-run reduction over the referenced components' ``worker_label``s:
  0 → ``None``, 1 → that label, ≥2 distinct → :class:`ConflictingWorkerLabelsError`.
* **The scheduler stamp** — ``run_due_once`` with a ``resolve_label`` resolver
  enqueues a job whose ``required_label`` matches the pipeline's component label;
  with the default ``resolve_label=None`` the job is unlabeled (existing behavior,
  byte-identical).
* **End-to-end 2-worker placement** — two in-process workers (one labeled, one
  unlabeled) over one Postgres: a labeled job runs only on the labeled worker; an
  unlabeled job runs on either. Deterministic (``_execute_job`` stubbed to success
  — no real subprocesses/registry), no flakes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from carve.core.config.paths import ProjectPaths
from carve.core.config.pipeline_schema import (
    ConflictingWorkerLabelsError,
    DbtStepConfig,
    DltStepConfig,
    Pipeline,
    SqlStepConfig,
    resolve_required_label,
)
from carve.core.config.schema import (
    ComponentConfig,
    ComponentMode,
    ComponentType,
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.config.state_store import StateStoreConfig
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.job_queue import JobQueue
from carve.core.state.schedules import Schedules


def _dlt_component(label: str | None) -> ComponentConfig:
    return ComponentConfig(type=ComponentType.DLT, mode=ComponentMode.SAME_REPO, worker_label=label)


def _dbt_component(label: str | None) -> ComponentConfig:
    return ComponentConfig(type=ComponentType.DBT, mode=ComponentMode.SAME_REPO, worker_label=label)


# ---------------------------------------------------------------------------
# The pure reducer: resolve_required_label
# ---------------------------------------------------------------------------


def test_resolve_none_when_no_component_is_labeled() -> None:
    pipeline = Pipeline(
        name="p",
        steps=[
            DltStepConfig(id="ingest", component="stripe"),
            DbtStepConfig(id="stage", component="analytics"),
        ],
    )
    components = {"stripe": _dlt_component(None), "analytics": _dbt_component(None)}
    assert resolve_required_label(pipeline, components) is None


def test_resolve_returns_the_single_label() -> None:
    pipeline = Pipeline(
        name="p",
        steps=[
            DltStepConfig(id="ingest", component="stripe"),
            DbtStepConfig(id="stage", component="analytics"),
        ],
    )
    components = {"stripe": _dlt_component(None), "analytics": _dbt_component("onprem-dbt")}
    assert resolve_required_label(pipeline, components) == "onprem-dbt"


def test_resolve_dedupes_the_same_label_across_steps() -> None:
    # Two components sharing one label is NOT a conflict — it reduces to that label.
    pipeline = Pipeline(
        name="p",
        steps=[
            DltStepConfig(id="ingest", component="stripe"),
            DbtStepConfig(id="stage", component="analytics"),
        ],
    )
    components = {"stripe": _dlt_component("onprem"), "analytics": _dbt_component("onprem")}
    assert resolve_required_label(pipeline, components) == "onprem"


def test_resolve_raises_on_conflicting_labels() -> None:
    pipeline = Pipeline(
        name="p",
        steps=[
            DltStepConfig(id="ingest", component="stripe"),
            DbtStepConfig(id="stage", component="analytics"),
        ],
    )
    components = {
        "stripe": _dlt_component("near-source"),
        "analytics": _dbt_component("onprem-dbt"),
    }
    with pytest.raises(ConflictingWorkerLabelsError) as exc:
        resolve_required_label(pipeline, components)
    # The message names both conflicting labels (sorted, deterministic).
    assert "near-source" in str(exc.value)
    assert "onprem-dbt" in str(exc.value)


def test_resolve_ignores_sql_and_omitted_dbt_and_convention_components() -> None:
    # A sql step (no component), an omitted-component dbt step, and a named
    # component with NO ComponentConfig entry (convention-resolved) all contribute
    # no label — only the one configured, labeled component counts.
    pipeline = Pipeline(
        name="p",
        steps=[
            SqlStepConfig(id="refresh", file="sql/r.sql", connection="local"),
            DbtStepConfig(id="stage", component=None),
            DltStepConfig(id="ingest", component="stripe"),
            DltStepConfig(id="ingest2", component="convention_only"),
        ],
    )
    components = {"stripe": _dlt_component("onprem")}  # convention_only is absent
    assert resolve_required_label(pipeline, components) == "onprem"


# ---------------------------------------------------------------------------
# The scheduler stamp (Postgres-fixture-gated)
# ---------------------------------------------------------------------------


@pytest.fixture
def sched_and_queue(postgres_state_store_url: str) -> tuple[Schedules, JobQueue]:
    config = Config(
        project=ProjectConfig(name="placement-sched-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    factory = create_session_factory(engine)
    return Schedules(factory), JobQueue(factory)


def _force_due(schedules: Schedules, schedule_id: str) -> None:
    from carve.core.state.models import Schedule

    with schedules._session_factory() as session:
        row = session.get(Schedule, schedule_id)
        assert row is not None
        row.next_fires_at = datetime(2020, 1, 1, tzinfo=UTC)
        session.commit()


def _queued_job(job_queue: JobQueue, pipeline: str) -> Any:
    import sqlalchemy as sa

    from carve.core.state.models import Job

    stmt = sa.select(Job).where(Job.pipeline == pipeline, Job.status == "queued")
    with job_queue._session_factory() as session:
        return session.scalars(stmt).one_or_none()


def test_run_due_once_stamps_required_label_from_resolver(
    sched_and_queue: tuple[Schedules, JobQueue],
) -> None:
    from carve.runtime.scheduler import run_due_once

    schedules, job_queue = sched_and_queue
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    _force_due(schedules, sched.id)

    fired = run_due_once(
        schedules,
        job_queue,
        datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
        resolve_label=lambda _pipeline: "onprem-dbt",
    )
    assert fired == 1
    job = _queued_job(job_queue, "sales")
    assert job is not None
    assert job.required_label == "onprem-dbt"


def test_run_due_once_default_resolver_leaves_job_unlabeled(
    sched_and_queue: tuple[Schedules, JobQueue],
) -> None:
    # No resolver (the default) → required_label NULL, exactly today's behavior.
    from carve.runtime.scheduler import run_due_once

    schedules, job_queue = sched_and_queue
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    _force_due(schedules, sched.id)

    fired = run_due_once(schedules, job_queue, datetime(2026, 1, 1, 12, 5, tzinfo=UTC))
    assert fired == 1
    job = _queued_job(job_queue, "sales")
    assert job is not None
    assert job.required_label is None


def test_run_due_once_resolver_receives_the_pipeline_name(
    sched_and_queue: tuple[Schedules, JobQueue],
) -> None:
    from carve.runtime.scheduler import run_due_once

    schedules, job_queue = sched_and_queue
    sched = schedules.seed("sales", "*/5 * * * *", "dev")
    _force_due(schedules, sched.id)

    seen: list[str] = []

    def _resolver(pipeline_name: str) -> str | None:
        seen.append(pipeline_name)
        return None

    run_due_once(
        schedules, job_queue, datetime(2026, 1, 1, 12, 5, tzinfo=UTC), resolve_label=_resolver
    )
    assert seen == ["sales"]


def test_run_due_once_raising_resolver_only_skips_its_own_fire(
    sched_and_queue: tuple[Schedules, JobQueue],
) -> None:
    """A resolver that raises for one pipeline must not abort the other due fires.

    Defense in depth: ``run_due_once`` wraps the resolver call per-schedule, so a
    resolver blowing up on ``bad`` skips only its fire (recovered next boundary)
    while ``good`` still enqueues — and no exception escapes the pass.
    """
    from carve.runtime.scheduler import run_due_once

    schedules, job_queue = sched_and_queue
    bad = schedules.seed("bad", "*/5 * * * *", "dev")
    good = schedules.seed("good", "*/5 * * * *", "dev")
    _force_due(schedules, bad.id)
    _force_due(schedules, good.id)

    def _resolver(pipeline_name: str) -> str | None:
        if pipeline_name == "bad":
            raise RuntimeError("resolver blew up")
        return "onprem-dbt"

    fired = run_due_once(
        schedules, job_queue, datetime(2026, 1, 1, 12, 5, tzinfo=UTC), resolve_label=_resolver
    )
    # Only 'good' fired; 'bad' was skipped (not enqueued) and did not abort the pass.
    assert fired == 1
    good_job = _queued_job(job_queue, "good")
    assert good_job is not None
    assert good_job.required_label == "onprem-dbt"
    assert _queued_job(job_queue, "bad") is None


# ---------------------------------------------------------------------------
# End-to-end 2-worker placement (deterministic, Postgres-fixture-gated)
# ---------------------------------------------------------------------------


def _worker_ctx(factory: Any, tmp_path: Path, worker_id: str, label: str | None) -> Any:
    from carve.runtime.worker import WorkerContext

    return WorkerContext(
        repository=Repository(factory),
        job_queue=JobQueue(factory),
        paths=ProjectPaths.from_root(tmp_path),
        connections=ConnectionsConfig(),
        dbt_executable="dbt",
        worker_id=worker_id,
        label=label,
    )


async def test_two_worker_placement_routes_by_label(
    postgres_state_store_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A labeled job runs only on the labeled worker; an unlabeled job on either.

    Two in-process workers share one Postgres queue: A advertises ``onprem-dbt``, B
    is unlabeled. Execution is stubbed to success (no registry/subprocess) so the
    test is deterministic and exercises the placement claim, not the executor.
    """
    import carve.runtime.worker as worker_mod
    from carve.runtime.execute_pipeline import RunResult
    from carve.runtime.worker import run_once

    config = Config(
        project=ProjectConfig(name="placement-e2e"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    factory = create_session_factory(engine)
    queue = JobQueue(factory)

    async def _ok(c: Any, *, job_pipeline: str, run_id: str, target: str) -> RunResult:
        return RunResult(
            status="succeeded",
            completed=frozenset({job_pipeline}),
            failed=frozenset(),
            skipped=frozenset(),
        )

    monkeypatch.setattr(worker_mod, "_execute_job", _ok)

    # A labeled job (enqueued first, so it is oldest-due) and an unlabeled job.
    labeled = queue.enqueue_scheduled("secure", "dev", required_label="onprem-dbt")
    unlabeled = queue.enqueue_scheduled("open", "dev")

    worker_a = _worker_ctx(factory, tmp_path, "worker-A", "onprem-dbt")
    worker_b = _worker_ctx(factory, tmp_path, "worker-B", None)

    # Worker B (unlabeled) runs first: it must SKIP the older labeled job and take
    # the unlabeled one.
    assert await run_once(worker_b) is True
    ran_open = queue.get_job(unlabeled.id)
    assert ran_open is not None
    assert ran_open.status == "succeeded"
    assert ran_open.claimed_by == "worker-B"

    # The labeled job is still queued — B could not place it.
    still = queue.get_job(labeled.id)
    assert still is not None
    assert still.status == "queued"

    # Worker A (labeled) runs the labeled job.
    assert await run_once(worker_a) is True
    ran_secure = queue.get_job(labeled.id)
    assert ran_secure is not None
    assert ran_secure.status == "succeeded"
    assert ran_secure.claimed_by == "worker-A"


# ---------------------------------------------------------------------------
# The label threads through the two easy-to-miss seams (unit + register write)
# ---------------------------------------------------------------------------


def test_with_worker_id_preserves_label(tmp_path: Path) -> None:
    """``_with_worker_id`` copies ``label`` on the id-rebind (only fires when worker_id='').

    The copy branch runs only when ``ctx.worker_id == ""`` (else ``_with_worker_id``
    returns the same ctx), so no full-run test drives it — deleting the ``label=``
    line would pass every other test. This asserts the reconstruct directly.
    """
    from carve.runtime.worker import WorkerContext, _with_worker_id

    ctx = WorkerContext(
        repository=cast(Any, None),
        job_queue=cast(Any, None),
        paths=ProjectPaths.from_root(tmp_path),
        connections=ConnectionsConfig(),
        dbt_executable="dbt",
        worker_id="",  # empty → the id-rebind reconstructs the dataclass field-by-field
        label="onprem-dbt",
    )
    rebound = _with_worker_id(ctx, "w1")
    assert rebound.worker_id == "w1"
    assert rebound.label == "onprem-dbt"  # copied, not silently dropped


async def test_worker_loop_registers_worker_with_its_label(
    postgres_state_store_url: str, tmp_path: Path
) -> None:
    """A labeled ``worker_loop`` writes its label onto the ``workers`` row.

    The ``register_worker(label=ctx.label)`` write was code-verified but unasserted;
    a pre-set ``shutdown`` makes the loop register-then-exit against an empty queue,
    and the persisted row must carry the label.
    """
    from carve.runtime.worker import worker_loop

    config = Config(
        project=ProjectConfig(name="placement-register"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=postgres_state_store_url),
    )
    engine = create_engine_from_config(config)
    initialize_database(engine)
    factory = create_session_factory(engine)
    ctx = _worker_ctx(factory, tmp_path, "labeled-worker", "onprem-dbt")

    shutdown = asyncio.Event()
    shutdown.set()  # register on entry, then exit immediately (empty queue)
    await worker_loop(ctx, shutdown=shutdown)

    worker = ctx.job_queue.get_worker("labeled-worker")
    assert worker is not None
    assert worker.label == "onprem-dbt"
