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

P1-02 introduces the `Build` entity and the corresponding helpers
(`create_build`, `get_build`, `latest_build_for`, plus the
``current_build_id`` accessors on `Pipeline`). The dropped Plan columns
(``estimates_json``, ``deployed_at``, ``deploy_run_id``) are no longer
written from `mark_plan_built`; the build row carries that state now.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from carve.core.state.models import Build, Log, Pipeline, Plan, Run

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
        """Return drafted plans whose `expires_at` is in the past.

        `now` is injectable for tests; production callers pass `None` and
        get the current UTC time.

        P1-02 dropped ``Plan.deployed_at``; "drafted" is now identified
        by ``phase == "drafted"`` (built plans are skipped because they
        have a corresponding Build that owns the deploy state).
        """
        cutoff = now if now is not None else datetime.now(UTC).replace(tzinfo=None)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.astimezone(UTC).replace(tzinfo=None)
        stmt = (
            select(Plan)
            .where(Plan.expires_at < cutoff)
            .where(Plan.phase == "drafted")
            .order_by(Plan.created_at.asc())
        )
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def mark_plan_built(
        self,
        plan_id: str,
        *,
        pipeline_name: str,
    ) -> None:
        """Stamp a plan as ``phase='built'`` after a successful build.

        Records the pipeline name the build settled on. P1-02 trimmed
        the body: ``deployed_at``, ``deploy_run_id``, and
        ``estimates_json`` are gone — that state is owned by the
        `Build` row created alongside this transition.
        """
        with self._session_factory() as session:
            plan = session.get(Plan, plan_id)
            if plan is None:
                raise KeyError(f"plan {plan_id!r} not found")
            plan.phase = "built"
            plan.pipeline_name = pipeline_name
            session.commit()

    def expire_old_plans(self, now: datetime | None = None) -> int:
        """Convenience: count of currently-expired, drafted plans."""
        return len(self.list_expired_plans(now=now))

    # -------------------------------------------------------------- Pipelines

    def create_or_update_pipeline(
        self,
        *,
        name: str,
        description: str,
        pipeline_dir: str,
    ) -> Pipeline:
        """Insert or update a pipeline row keyed by ``name``.

        The first call for a given name sets ``created_at`` and returns
        a fresh row with ``current_build_id=None``. Subsequent calls
        update ``description``, ``pipeline_dir``, and ``updated_at``,
        leaving ``created_at``, ``current_build_id``, and the
        ``last_run_*`` denorms alone — rebuilds shouldn't reset the
        run history.

        ``current_build_id`` is set separately by `create_build` (which
        atomically inserts the Build row and stamps the FK).
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        with self._session_factory() as session:
            pipeline = session.get(Pipeline, name)
            if pipeline is None:
                pipeline = Pipeline(
                    name=name,
                    description=description,
                    pipeline_dir=pipeline_dir,
                    current_build_id=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(pipeline)
            else:
                pipeline.description = description
                pipeline.pipeline_dir = pipeline_dir
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
            if pipeline.current_build_id is not None:
                current_build = session.get(Build, pipeline.current_build_id)
                if current_build is not None:
                    current_plan = session.get(Plan, current_build.plan_id)

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

    # ----------------------------------------------------------------- Builds

    def create_build(
        self,
        *,
        pipeline_name: str,
        plan_id: str,
        target: str,
        manifest: dict[str, list[str]] | None = None,
    ) -> Build:
        """Insert a new Build row and return it.

        The id is generated as ``build_<uuid4().hex>`` so the build is
        addressable from the moment it lands. ``manifest`` is serialized
        into ``manifest_json`` (default: ``{"files": []}`` for the empty
        case). The Pipeline FK on ``current_build_id`` is *not* updated
        here — call ``set_pipeline_current_build`` after the insert
        commits so a failed FK update doesn't roll back the build row.
        """
        build_id = "build_" + uuid.uuid4().hex
        manifest_payload = manifest if manifest is not None else {"files": []}
        now = datetime.now(UTC).replace(tzinfo=None)
        build = Build(
            id=build_id,
            pipeline_name=pipeline_name,
            plan_id=plan_id,
            target=target,
            created_at=now,
            manifest_json=json.dumps(manifest_payload, sort_keys=True),
        )
        with self._session_factory() as session:
            session.add(build)
            session.commit()
            session.refresh(build)
            return build

    def get_build(self, build_id: str) -> Build | None:
        """Fetch a build by id, or `None` if not found."""
        with self._session_factory() as session:
            return session.get(Build, build_id)

    def get_pipeline_current_build(self, name: str) -> Build | None:
        """Return the Build pointed at by ``Pipeline.current_build_id``.

        Returns `None` if the pipeline doesn't exist or hasn't built yet.
        """
        with self._session_factory() as session:
            pipeline = session.get(Pipeline, name)
            if pipeline is None or pipeline.current_build_id is None:
                return None
            return session.get(Build, pipeline.current_build_id)

    def set_pipeline_current_build(self, name: str, build_id: str) -> None:
        """Atomically point a Pipeline at a Build via its FK.

        Raises `KeyError` if the pipeline doesn't exist; the build is
        assumed to exist (callers create it via ``create_build`` before
        calling this).
        """
        with self._session_factory() as session:
            pipeline = session.get(Pipeline, name)
            if pipeline is None:
                raise KeyError(f"pipeline {name!r} not found")
            pipeline.current_build_id = build_id
            pipeline.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()

    def latest_build_for(self, name: str, target: str) -> Build | None:
        """Return the most recent Build for ``(pipeline_name, target)``.

        Backed by the ``ix_builds_pipeline_target_created_at`` index;
        used by ``carve el deploy`` and ``carve el run`` to resolve the
        artifact to ship for a given target.
        """
        stmt = (
            select(Build)
            .where(Build.pipeline_name == name)
            .where(Build.target == target)
            .order_by(Build.created_at.desc())
            .limit(1)
        )
        with self._session_factory() as session:
            return session.scalars(stmt).first()
