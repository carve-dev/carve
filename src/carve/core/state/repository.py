"""Typed repository — the only module that issues SQL.

CLI commands, agents, and runners construct a `Repository` and call its
methods. They never open a `Session` directly. Each method opens a short
transaction, commits, and returns plain ORM objects (or simple Python
values). `expire_on_commit=False` on the session factory means the
returned instances are safely detached and can be read after commit.

For M1.1-06 the surface adds pipeline-centric helpers
(`create_or_update_pipeline`, `get_pipeline`, `list_pipelines`,
`get_pipeline_lineage`, `record_pipeline_run`) and renames
`mark_plan_applied` -> `mark_plan_built` to fit the plan/build/run split.
The columns previously named ``applied_at`` / ``apply_run_id`` are now
``deployed_at`` / ``deploy_run_id`` (M1.1-06.1) — the verb was renamed
from ``apply`` to ``deploy`` because the M2 use case is "ship to prod
via PR", not Terraform-style immediate execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from carve.core.state.models import Log, Pipeline, Plan, Run

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


@dataclass
class PipelineLineage:
    """Result of `Repository.get_pipeline_lineage`.

    Encapsulates everything `carve pipelines <name>` needs: the parent
    chain leading up to the current plan, the children/refinements of
    that plan, and the most recent runs against the pipeline. Built once
    in a single transaction so the CLI gets a consistent snapshot.
    """

    pipeline: Pipeline
    current_plan: Plan | None
    parent_chain: list[Plan] = field(default_factory=list)
    children: list[Plan] = field(default_factory=list)
    recent_runs: list[Run] = field(default_factory=list)


class Repository:
    """Typed access to the state store.

    Construct once per process (or per request, for the future API
    server) and pass the instance to anything that needs to read or
    write state.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ Runs

    def create_run(
        self,
        kind: str,
        target_id: str,
        *,
        pipeline_name: str | None = None,
    ) -> str:
        """Insert a new `runs` row in the default `queued` state.

        Returns the generated run id (a UUID4 hex string). The id is
        chosen client-side so the caller can stream logs against it
        before the row is committed.
        """
        run_id = uuid.uuid4().hex
        with self._session_factory() as session:
            session.add(
                Run(
                    id=run_id,
                    kind=kind,
                    target_id=target_id,
                    pipeline_name=pipeline_name,
                )
            )
            session.commit()
        return run_id

    def update_run_status(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Move a run to a new lifecycle state.

        Side-effects mirror the spec's lifecycle:
        - Setting `status="running"` populates `started_at` if unset.
        - Any terminal status (`success`, `failed`, `cancelled`, `crashed`)
          populates `completed_at` and computes `duration_ms` from
          `started_at`.
        - `error` is stored verbatim on `error_message`.
        """
        terminal = {"success", "failed", "cancelled", "crashed"}
        with self._session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(f"run {run_id!r} not found")
            run.status = status
            if error is not None:
                run.error_message = error
            now_utc = datetime.now(UTC).replace(tzinfo=None)
            if status == "running" and run.started_at is None:
                run.started_at = now_utc
            if status in terminal:
                run.completed_at = now_utc
                if run.started_at is not None:
                    delta = now_utc - run.started_at
                    run.duration_ms = int(delta.total_seconds() * 1000)
            session.commit()

    def attach_pipeline_to_run(self, run_id: str, pipeline_name: str) -> None:
        """Backfill `Run.pipeline_name` after the Pipeline row exists.

        Build runs are created before the Pipeline they materialize exists
        on disk (the build agent is what writes it), so we can't set the
        FK at create time without violating it. This helper fills the
        column once the Pipeline row has been upserted, so subsequent
        `runs --pipeline <name>` filters pick up build history alongside
        run history.
        """
        with self._session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(f"run {run_id!r} not found")
            run.pipeline_name = pipeline_name
            session.commit()

    def get_run(self, run_id: str) -> Run | None:
        """Fetch a run by id, or `None` if not found."""
        with self._session_factory() as session:
            return session.get(Run, run_id)

    def list_runs(
        self,
        status: str | None = None,
        limit: int = 50,
        *,
        pipeline_name: str | None = None,
    ) -> list[Run]:
        """List runs newest-first, optionally filtered."""
        stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(Run.status == status)
        if pipeline_name is not None:
            stmt = stmt.where(Run.pipeline_name == pipeline_name)
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    # ------------------------------------------------------------------ Logs

    def append_log(
        self,
        run_id: str,
        level: str,
        source: str,
        message: str,
    ) -> None:
        """Append a log line for `run_id`."""
        with self._session_factory() as session:
            session.add(
                Log(
                    run_id=run_id,
                    level=level,
                    source=source,
                    message=message,
                )
            )
            session.commit()

    def get_logs(
        self,
        run_id: str,
        since: datetime | None = None,
        since_id: int | None = None,
    ) -> list[Log]:
        """Return logs for a run in insertion order.

        Callers tailing logs should prefer `since_id` (the autoincrement
        primary key) over `since` (a wall-clock timestamp). Two log lines
        appended within the same `datetime.now()` tick share a timestamp
        on most platforms; filtering by `Log.id > since_id` makes the
        tail loop deterministic regardless of clock resolution.
        """
        stmt = select(Log).where(Log.run_id == run_id).order_by(Log.id.asc())
        if since is not None:
            stmt = stmt.where(Log.timestamp > since)
        if since_id is not None:
            stmt = stmt.where(Log.id > since_id)
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    # ----------------------------------------------------------------- Plans

    def save_plan(self, plan: Plan) -> None:
        """Insert or update a plan row."""
        with self._session_factory() as session:
            session.merge(plan)
            session.commit()

    def get_plan(self, plan_id: str) -> Plan | None:
        """Fetch a plan by id, or `None` if not found."""
        with self._session_factory() as session:
            return session.get(Plan, plan_id)

    def list_plans(
        self,
        limit: int = 50,
        *,
        pipeline_name: str | None = None,
    ) -> list[Plan]:
        """List plans newest-first, optionally filtered by pipeline."""
        stmt = select(Plan).order_by(Plan.created_at.desc()).limit(limit)
        if pipeline_name is not None:
            stmt = stmt.where(Plan.pipeline_name == pipeline_name)
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def list_expired_plans(self, now: datetime | None = None) -> list[Plan]:
        """Return un-deployed plans whose `expires_at` is in the past.

        `now` is injectable for tests; production callers pass `None` and
        get the current UTC time.
        """
        cutoff = now if now is not None else datetime.now(UTC).replace(tzinfo=None)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.astimezone(UTC).replace(tzinfo=None)
        stmt = (
            select(Plan)
            .where(Plan.expires_at < cutoff)
            .where(Plan.deployed_at.is_(None))
            .order_by(Plan.created_at.asc())
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def mark_plan_built(
        self,
        plan_id: str,
        *,
        pipeline_name: str,
        build_run_id: str,
    ) -> None:
        """Stamp a plan as ``phase='built'`` after a successful build.

        Records the pipeline name the build settled on plus the build's
        run id (kept on `deploy_run_id` for now — same column, new
        semantics: "first run that materialized this plan"). `deployed_at`
        is also stamped to make plan history queryable without a JOIN.
        """
        with self._session_factory() as session:
            plan = session.get(Plan, plan_id)
            if plan is None:
                raise KeyError(f"plan {plan_id!r} not found")
            plan.phase = "built"
            plan.pipeline_name = pipeline_name
            plan.deployed_at = datetime.now(UTC).replace(tzinfo=None)
            plan.deploy_run_id = build_run_id
            session.commit()

    def expire_old_plans(self, now: datetime | None = None) -> int:
        """Convenience: count of currently-expired, un-deployed plans."""
        return len(self.list_expired_plans(now=now))

    # -------------------------------------------------------------- Pipelines

    def create_or_update_pipeline(
        self,
        *,
        name: str,
        description: str,
        pipeline_dir: str,
        current_plan_id: str | None,
    ) -> Pipeline:
        """Insert or update a pipeline row keyed by ``name``.

        The first call for a given name sets ``created_at`` and returns
        a fresh row. Subsequent calls update ``description``,
        ``pipeline_dir``, ``current_plan_id``, and ``updated_at``,
        leaving ``created_at`` and the ``last_run_*`` denorms alone —
        rebuilds shouldn't reset the run history.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        with self._session_factory() as session:
            pipeline = session.get(Pipeline, name)
            if pipeline is None:
                pipeline = Pipeline(
                    name=name,
                    description=description,
                    pipeline_dir=pipeline_dir,
                    current_plan_id=current_plan_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(pipeline)
            else:
                pipeline.description = description
                pipeline.pipeline_dir = pipeline_dir
                pipeline.current_plan_id = current_plan_id
                pipeline.updated_at = now
            session.commit()
            session.refresh(pipeline)
            return pipeline

    def get_pipeline(self, name: str) -> Pipeline | None:
        """Fetch a pipeline by name."""
        with self._session_factory() as session:
            return session.get(Pipeline, name)

    def list_pipelines(self, limit: int = 50) -> list[Pipeline]:
        """List pipelines, most-recently-updated first."""
        stmt = select(Pipeline).order_by(Pipeline.updated_at.desc()).limit(limit)
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def get_pipeline_lineage(
        self,
        name: str,
        *,
        run_limit: int = 10,
    ) -> PipelineLineage | None:
        """Build a lineage snapshot for ``carve pipelines <name>``.

        Walks ``parent_plan_id`` from the pipeline's current plan to find
        the parent chain; queries plans whose ``parent_plan_id`` equals
        the current plan to find children. Recent runs are filtered by
        ``pipeline_name`` and capped at ``run_limit``.

        Returns ``None`` if the pipeline doesn't exist.
        """
        with self._session_factory() as session:
            pipeline = session.get(Pipeline, name)
            if pipeline is None:
                return None

            current_plan: Plan | None = None
            if pipeline.current_plan_id is not None:
                current_plan = session.get(Plan, pipeline.current_plan_id)

            parent_chain: list[Plan] = []
            cursor = current_plan
            while cursor is not None and cursor.parent_plan_id is not None:
                parent = session.get(Plan, cursor.parent_plan_id)
                if parent is None:
                    break
                parent_chain.append(parent)
                cursor = parent

            children: list[Plan] = []
            if current_plan is not None:
                child_stmt = (
                    select(Plan)
                    .where(Plan.parent_plan_id == current_plan.id)
                    .order_by(Plan.created_at.asc())
                )
                children = list(session.scalars(child_stmt).all())

            run_stmt = (
                select(Run)
                .where(Run.pipeline_name == name)
                .order_by(Run.created_at.desc())
                .limit(run_limit)
            )
            recent_runs = list(session.scalars(run_stmt).all())

            return PipelineLineage(
                pipeline=pipeline,
                current_plan=current_plan,
                parent_chain=parent_chain,
                children=children,
                recent_runs=recent_runs,
            )

    def record_pipeline_run(
        self,
        *,
        pipeline_name: str,
        run_id: str,
        status: str,
        run_at: datetime | None = None,
    ) -> None:
        """Update the denormalized last-run columns on a pipeline.

        Called once a run reaches a terminal state. Idempotent — the
        last call wins. Silently no-ops if the pipeline row doesn't
        exist (the run row stays valid; we just have nowhere to update).
        """
        timestamp = run_at if run_at is not None else datetime.now(UTC).replace(tzinfo=None)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
        with self._session_factory() as session:
            pipeline = session.get(Pipeline, pipeline_name)
            if pipeline is None:
                return
            pipeline.last_run_id = run_id
            pipeline.last_run_status = status
            pipeline.last_run_at = timestamp
            session.commit()
