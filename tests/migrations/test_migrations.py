"""Tests for the Alembic migrations against Postgres.

v0.1-01 retired SQLite outright; these tests now run against the
per-test Postgres database provided by the ``postgres_state_store_url``
fixture in ``tests/conftest.py``.

The test surface covers:

* Fresh upgrade-to-head on an empty Postgres produces the expected
  schema shape (the v0.1-01 ## Tests bullet 4 acceptance check).
* Idempotency of ``initialize_database``.
* Schema shape at intermediate revisions (0003 rename, 0004 builds,
  0005 runs.target, 0006 parent_run_id), exercised by walking the
  migration sequence and inspecting the inspector at each stop.

The legacy "pre-Alembic SQLite" tests from the M1 era have been removed
— there is no pre-existing legacy state to backfill from on a Postgres-
from-day-one deployment.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alembic import command as alembic_command
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TIMESTAMP

from carve.core.config.schema import (
    Config,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    _alembic_config,
    create_engine_from_config,
    initialize_database,
)


def _make_config(state_store_url: str) -> Config:
    return Config(
        project=ProjectConfig(name="migration-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        state_store=StateStoreConfig(url=state_store_url),
    )


# ---------------------------------------------------------------------------
# Fresh upgrade-head (v0.1-01 ## Tests bullet 4)
# ---------------------------------------------------------------------------


def test_alembic_upgrade_head_on_empty_postgres(
    postgres_state_store_url: str,
) -> None:
    """Running ``alembic upgrade head`` on an empty Postgres lands the
    full v0.1-01 schema.

    Schema-shape regression test: the table list and selected column
    types must match the documented v0.1 expected DDL.
    """
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        # The five M1-shape tables, the workspaces table, plus alembic's
        # bookkeeping table.
        assert {
            "runs",
            "logs",
            "plans",
            "pipelines",
            "builds",
            "workspaces",
        }.issubset(tables)
        assert "alembic_version" in tables

        # JSONB on the two free-form payload columns.
        plan_cols = {c["name"]: c for c in inspector.get_columns("plans")}
        build_cols = {c["name"]: c for c in inspector.get_columns("builds")}
        assert isinstance(plan_cols["task_graph_json"]["type"], JSONB)
        assert isinstance(build_cols["manifest_json"]["type"], JSONB)

        # TIMESTAMPTZ on the timestamp columns. We sample one per table
        # rather than enumerate every column — the type-shift is uniform.
        for table_name, cols_to_check in [
            ("runs", ("created_at", "started_at", "completed_at")),
            ("logs", ("timestamp",)),
            ("plans", ("created_at", "expires_at")),
            ("pipelines", ("created_at", "updated_at", "last_run_at")),
            ("builds", ("created_at", "deployed_at")),
        ]:
            cols = {c["name"]: c for c in inspector.get_columns(table_name)}
            for col_name in cols_to_check:
                col_type = cols[col_name]["type"]
                assert isinstance(col_type, TIMESTAMP), (
                    f"{table_name}.{col_name} expected TIMESTAMP; "
                    f"got {col_type!r}"
                )
                assert col_type.timezone is True, (
                    f"{table_name}.{col_name} expected TIMESTAMPTZ; "
                    f"got {col_type!r}"
                )

        # The 0006 parent_run_id index landed.
        run_indexes = {ix["name"] for ix in inspector.get_indexes("runs")}
        assert "ix_runs_parent_run_id" in run_indexes

        # The 0004 builds index landed.
        build_indexes = {ix["name"] for ix in inspector.get_indexes("builds")}
        assert "ix_builds_pipeline_target_created_at" in build_indexes

        # Alembic version row stamps at head.
        with engine.connect() as conn:
            head_rev = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert head_rev == "0007_workspaces"
    finally:
        engine.dispose()


def test_fresh_db_lands_on_pipeline_centric_schema(
    postgres_state_store_url: str,
) -> None:
    """After ``initialize_database``, the pipeline-centric tables/columns
    are present (the M1.1-06 schema shift)."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"runs", "logs", "plans", "pipelines", "alembic_version"}.issubset(
            tables
        )

        plan_cols = {c["name"] for c in inspector.get_columns("plans")}
        assert {"phase", "pipeline_name"}.issubset(plan_cols)

        run_cols = {c["name"] for c in inspector.get_columns("runs")}
        assert "pipeline_name" in run_cols
    finally:
        engine.dispose()


def test_initialize_database_is_idempotent(
    postgres_state_store_url: str,
) -> None:
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)
        initialize_database(engine)  # second call is a no-op
        inspector = inspect(engine)
        assert "pipelines" in inspector.get_table_names()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0003_rename_apply_to_deploy
# ---------------------------------------------------------------------------


def test_rename_apply_to_deploy_renames_columns(
    postgres_state_store_url: str,
) -> None:
    """At revision 0003, ``plans.deployed_at`` and ``deploy_run_id`` exist
    and the legacy ``applied_at`` / ``apply_run_id`` are gone.

    Walks the migration sequence to 0003 (one before 0004 which drops the
    renamed columns).
    """
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        cfg = _alembic_config(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "0003_rename_apply_to_deploy")

        inspector = inspect(engine)
        plan_cols = {c["name"] for c in inspector.get_columns("plans")}
        assert "deployed_at" in plan_cols
        assert "deploy_run_id" in plan_cols
        assert "applied_at" not in plan_cols
        assert "apply_run_id" not in plan_cols
    finally:
        engine.dispose()


def test_rename_apply_to_deploy_rewrites_apply_kind_runs(
    postgres_state_store_url: str,
) -> None:
    """A pre-existing run with kind='apply' is rewritten to kind='deploy'.

    Seeds at revision 0002 (one before the rename), inserts the legacy
    apply-kind run, then upgrades to head. The kind column flips to
    'deploy'.
    """
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        cfg = _alembic_config(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "0002_pipeline_centric")

        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO runs "
                    "(id, kind, target_id, owner_user_id, status, "
                    " tokens_input, tokens_output, cost_usd, created_at) "
                    "VALUES "
                    "(:id, 'apply', :target_id, 1, 'queued', 0, 0, 0.0, :ts)"
                ),
                {"id": "run_legacy_apply", "target_id": "plan_x", "ts": now},
            )

        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "head")

        with engine.begin() as conn:
            kind = conn.execute(
                text("SELECT kind FROM runs WHERE id = 'run_legacy_apply'")
            ).scalar_one()
        assert kind == "deploy"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0004_build_entity
# ---------------------------------------------------------------------------


def test_0004_creates_builds_and_renames_pipeline_fk(
    postgres_state_store_url: str,
) -> None:
    """After head: `builds` table exists; pipelines points at it via
    ``current_build_id`` instead of the legacy ``current_plan_id``."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "builds" in tables

        build_cols = {c["name"] for c in inspector.get_columns("builds")}
        assert build_cols == {
            "id",
            "pipeline_name",
            "plan_id",
            "target",
            "created_at",
            "manifest_json",
            "commit_sha",
            "pr_url",
            "deployed_at",
        }

        pipeline_cols = {c["name"] for c in inspector.get_columns("pipelines")}
        assert "current_build_id" in pipeline_cols
        assert "current_plan_id" not in pipeline_cols

        indexes = {ix["name"] for ix in inspector.get_indexes("builds")}
        assert "ix_builds_pipeline_target_created_at" in indexes
    finally:
        engine.dispose()


def test_0004_drops_vestigial_plan_columns(
    postgres_state_store_url: str,
) -> None:
    """After head: ``plans`` no longer carries estimates_json / deployed_at
    / deploy_run_id (those moved to ``builds``)."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)

        inspector = inspect(engine)
        plan_cols = {c["name"] for c in inspector.get_columns("plans")}
        assert "estimates_json" not in plan_cols
        assert "deployed_at" not in plan_cols
        assert "deploy_run_id" not in plan_cols
        assert {"id", "phase", "pipeline_name", "task_graph_json"}.issubset(
            plan_cols
        )
    finally:
        engine.dispose()


def test_0004_backfills_builds_from_existing_pipelines(
    postgres_state_store_url: str,
) -> None:
    """Pipelines with non-null ``current_plan_id`` at revision 0003 get a
    synthesized Build row when 0004 runs.

    Walks the migration to 0003, seeds a Plan + Pipeline with a
    `current_plan_id`, then upgrades to head and asserts the pipeline
    points at a fresh build that binds the plan to a default target.
    """
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        cfg = _alembic_config(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "0003_rename_apply_to_deploy")

        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO plans "
                    "(id, goal, config_hash, carve_version, estimates_json, "
                    " task_graph_json, file_path, phase, created_at, "
                    " expires_at, deployed_at) "
                    "VALUES "
                    "(:id, 'g', 'h', '0.0.1', '{}', '{}', '/tmp/p.json', "
                    " 'built', :now, :now, :deployed)"
                ),
                {"id": "plan_x", "now": now, "deployed": now},
            )
            conn.execute(
                text(
                    "INSERT INTO pipelines "
                    "(name, description, pipeline_dir, current_plan_id, "
                    " created_at, updated_at) "
                    "VALUES (:name, '', :dir, :plan, :now, :now)"
                ),
                {
                    "name": "ingest",
                    "dir": "pipelines/ingest",
                    "plan": "plan_x",
                    "now": now,
                },
            )

        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "head")

        with engine.begin() as conn:
            build_id = conn.execute(
                text(
                    "SELECT current_build_id FROM pipelines WHERE name = 'ingest'"
                )
            ).scalar_one()
            assert isinstance(build_id, str) and build_id.startswith("build_")

            build = conn.execute(
                text(
                    "SELECT pipeline_name, plan_id, target "
                    "FROM builds WHERE id = :id"
                ),
                {"id": build_id},
            ).one()
            assert build[0] == "ingest"
            assert build[1] == "plan_x"
            assert build[2] == "dev"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0005_runs_target
# ---------------------------------------------------------------------------


def test_0005_adds_runs_target_column(postgres_state_store_url: str) -> None:
    """After head: ``runs.target`` exists as a nullable column."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)

        inspector = inspect(engine)
        run_cols = {c["name"]: c for c in inspector.get_columns("runs")}
        assert "target" in run_cols
        assert run_cols["target"]["nullable"] is True
    finally:
        engine.dispose()


def test_0005_backfills_runs_target_from_latest_build(
    postgres_state_store_url: str,
) -> None:
    """Existing runs inherit their pipeline's most recent build's target.

    Seeds at revision 0004 (before `runs.target` exists), inserts a
    pipeline + build + a run pointing at the pipeline, then upgrades to
    head. The run's `target` column is populated from the latest build's
    target; runs whose pipeline has no build keep NULL.
    """
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        cfg = _alembic_config(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "0004_build_entity")

        now = datetime.now(UTC)
        with engine.begin() as conn:
            # Order matters on Postgres (FKs are enforced):
            #   pipelines (no FK) → plans (FK to pipelines) → builds (FK
            #   to pipelines+plans) → pipelines.current_build_id update.
            conn.execute(
                text(
                    "INSERT INTO pipelines (name, description, pipeline_dir, "
                    "created_at, updated_at) "
                    "VALUES ('ingest', '', 'targets/staging/el/ingest', "
                    ":now, :now)"
                ),
                {"now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO plans (id, goal, config_hash, carve_version, "
                    "task_graph_json, file_path, phase, pipeline_name, "
                    "created_at, expires_at) VALUES "
                    "('plan_a', 'g', 'h', '0.0.1', '{}', '/tmp/p.json', "
                    "'built', 'ingest', :now, :now)"
                ),
                {"now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO builds (id, pipeline_name, plan_id, target, "
                    "created_at, manifest_json) VALUES "
                    "('build_a', 'ingest', 'plan_a', 'staging', :now, "
                    "'{}'::jsonb)"
                ),
                {"now": now},
            )
            conn.execute(
                text(
                    "UPDATE pipelines SET current_build_id = 'build_a' "
                    "WHERE name = 'ingest'"
                ),
            )
            # Run linked to the pipeline -> should inherit `staging`.
            conn.execute(
                text(
                    "INSERT INTO runs (id, kind, target_id, pipeline_name, "
                    "owner_user_id, status, tokens_input, tokens_output, "
                    "cost_usd, created_at) VALUES "
                    "(:id, 'run', :tid, 'ingest', 1, 'success', 0, 0, 0.0, "
                    ":now)"
                ),
                {"id": "run_with_pipe", "tid": "build_a", "now": now},
            )
            # Orphan run (no pipeline_name) -> stays NULL.
            conn.execute(
                text(
                    "INSERT INTO runs (id, kind, target_id, "
                    "owner_user_id, status, tokens_input, tokens_output, "
                    "cost_usd, created_at) VALUES "
                    "(:id, 'plan', :tid, 1, 'success', 0, 0, 0.0, :now)"
                ),
                {"id": "run_no_pipe", "tid": "plan_a", "now": now},
            )

        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "head")

        with engine.begin() as conn:
            target_with_pipe = conn.execute(
                text("SELECT target FROM runs WHERE id = 'run_with_pipe'")
            ).scalar_one()
            target_no_pipe = conn.execute(
                text("SELECT target FROM runs WHERE id = 'run_no_pipe'")
            ).scalar_one()
        assert target_with_pipe == "staging"
        assert target_no_pipe is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0006_recovery_chains
# ---------------------------------------------------------------------------


def test_0006_adds_runs_parent_run_id_column(
    postgres_state_store_url: str,
) -> None:
    """After head: ``runs.parent_run_id`` exists as nullable + index."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)

        inspector = inspect(engine)
        run_cols = {c["name"]: c for c in inspector.get_columns("runs")}
        assert "parent_run_id" in run_cols
        assert run_cols["parent_run_id"]["nullable"] is True
        indexes = {ix["name"] for ix in inspector.get_indexes("runs")}
        assert "ix_runs_parent_run_id" in indexes
    finally:
        engine.dispose()


def test_0006_existing_runs_get_null_parent_run_id(
    postgres_state_store_url: str,
) -> None:
    """Pre-existing runs end up with parent_run_id == NULL after the upgrade."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        cfg = _alembic_config(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "0005_runs_target")

        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO runs (id, kind, target_id, owner_user_id, "
                    "status, tokens_input, tokens_output, cost_usd, created_at) "
                    "VALUES (:id, 'run', 'tid', 1, 'success', 0, 0, 0.0, :now)"
                ),
                {"id": "run_pre_existing", "now": now},
            )

        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "head")

        with engine.begin() as conn:
            parent = conn.execute(
                text(
                    "SELECT parent_run_id FROM runs WHERE id = 'run_pre_existing'"
                )
            ).scalar_one()
        assert parent is None
    finally:
        engine.dispose()


def test_0006_fk_constraint_declared_against_runs_id(
    postgres_state_store_url: str,
) -> None:
    """FK constraint on parent_run_id references runs.id."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)

        inspector = inspect(engine)
        fks = inspector.get_foreign_keys("runs")
        parent_fks = [
            fk
            for fk in fks
            if fk.get("constrained_columns") == ["parent_run_id"]
        ]
        assert len(parent_fks) == 1, parent_fks
        fk = parent_fks[0]
        assert fk["referred_table"] == "runs"
        assert fk["referred_columns"] == ["id"]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0007_workspaces
# ---------------------------------------------------------------------------


def test_0007_creates_workspaces_table_with_expected_columns(
    postgres_state_store_url: str,
) -> None:
    """After head: the ``workspaces`` table exists with the documented
    columns and TIMESTAMPTZ on ``last_synced_at``."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)

        inspector = inspect(engine)
        assert "workspaces" in set(inspector.get_table_names())

        cols = {c["name"]: c for c in inspector.get_columns("workspaces")}
        assert set(cols) == {
            "name",
            "url",
            "branch",
            "last_synced_commit",
            "last_synced_at",
            "status",
        }
        synced_type = cols["last_synced_at"]["type"]
        assert isinstance(synced_type, TIMESTAMP)
        assert synced_type.timezone is True

        pk = inspector.get_pk_constraint("workspaces")
        assert pk["constrained_columns"] == ["name"]
    finally:
        engine.dispose()


def test_0007_status_check_constraint_enforced(
    postgres_state_store_url: str,
) -> None:
    """The status CHECK rejects values outside clean/dirty/unreachable."""
    from sqlalchemy.exc import IntegrityError

    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)

        now = datetime.now(UTC)
        # A valid status inserts fine.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO workspaces (name, url, status, last_synced_at) "
                    "VALUES ('w1', 'git@h:o/r.git', 'clean', :now)"
                ),
                {"now": now},
            )
        # An invalid status is rejected by ck_workspaces_status.
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO workspaces (name, url, status) "
                        "VALUES ('w2', 'git@h:o/r.git', 'bogus')"
                    )
                )
            raise AssertionError("expected the status CHECK to reject 'bogus'")
        except IntegrityError:
            pass
    finally:
        engine.dispose()


def test_0007_downgrade_drops_workspaces(
    postgres_state_store_url: str,
) -> None:
    """Downgrading 0007 -> 0006 drops the ``workspaces`` table."""
    config = _make_config(postgres_state_store_url)
    engine = create_engine_from_config(config)
    try:
        initialize_database(engine)
        assert "workspaces" in set(inspect(engine).get_table_names())

        cfg = _alembic_config(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            alembic_command.downgrade(cfg, "0006_recovery_chains")

        assert "workspaces" not in set(inspect(engine).get_table_names())

        with engine.connect() as conn:
            head_rev = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert head_rev == "0006_recovery_chains"
    finally:
        engine.dispose()
