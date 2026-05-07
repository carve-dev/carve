"""Tests for the Alembic migrations introduced in M1.1-06.

These tests exercise:

* Fresh-DB upgrade: 0001_baseline + 0002_pipeline_centric run cleanly.
* Backfill: a pre-existing `applied` plan with `pipeline_dir` in its
  task_graph_json gets a synthesized `Pipeline` row and is marked built.
* Idempotency: running the upgrade twice is safe.
* Pre-Alembic legacy DB: tables exist but no `alembic_version` row →
  `initialize_database` stamps 0001 and applies 0002.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, text

from carve.core.config.schema import (
    Config,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.state.database import (
    create_engine_from_config,
    initialize_database,
)


def _make_config(project_dir: Path) -> Config:
    return Config(
        project=ProjectConfig(name="migration-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(state_store="sqlite:///.carve/state.db"),
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / ".carve").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_fresh_db_lands_on_pipeline_centric_schema(project_dir: Path) -> None:
    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"runs", "logs", "plans", "pipelines", "alembic_version"}.issubset(tables)

    # Plans gained `phase` and `pipeline_name`.
    plan_cols = {c["name"] for c in inspector.get_columns("plans")}
    assert {"phase", "pipeline_name"}.issubset(plan_cols)

    # Runs gained `pipeline_name`.
    run_cols = {c["name"] for c in inspector.get_columns("runs")}
    assert "pipeline_name" in run_cols


def test_initialize_database_is_idempotent(project_dir: Path) -> None:
    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    initialize_database(engine)  # second call is a no-op
    inspector = inspect(engine)
    assert "pipelines" in inspector.get_table_names()


def test_legacy_db_gets_stamped_then_upgraded(project_dir: Path) -> None:
    """A DB that pre-dates Alembic gets stamped at 0001 and upgraded to head.

    Simulates the dev-machine case: someone smoke-tested with the old
    `Base.metadata.create_all()` path and now upgrades to a Carve build
    that uses Alembic. The legacy tables stay; new pipeline_centric
    columns and tables are added.
    """
    # Build a legacy-shaped DB: runs/logs/plans only, no `alembic_version`.
    db_path = project_dir / ".carve" / "state.db"
    legacy_url = f"sqlite:///{db_path}"
    legacy_engine = sa.create_engine(legacy_url)
    md = sa.MetaData()
    runs = sa.Table(
        "runs",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("target_id", sa.String, nullable=False),
        sa.Column("owner_user_id", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String, nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("error_message", sa.String, nullable=True),
        sa.Column("tokens_input", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_output", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    sa.Table(
        "logs",
        md,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String, sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("level", sa.String, nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("message", sa.String, nullable=False),
    )
    plans = sa.Table(
        "plans",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("parent_plan_id", sa.String, nullable=True),
        sa.Column("goal", sa.String, nullable=False),
        sa.Column("config_hash", sa.String, nullable=False),
        sa.Column("carve_version", sa.String, nullable=False),
        sa.Column("estimates_json", sa.String, nullable=False),
        sa.Column("task_graph_json", sa.String, nullable=False),
        sa.Column("file_path", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("applied_at", sa.DateTime, nullable=True),
        sa.Column("apply_run_id", sa.String, sa.ForeignKey("runs.id"), nullable=True),
    )
    md.create_all(legacy_engine)

    now = datetime.now(UTC).replace(tzinfo=None)
    with legacy_engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                id="run_legacy",
                kind="apply",
                target_id="plan_legacy",
                created_at=now,
            )
        )
        conn.execute(
            plans.insert().values(
                id="plan_legacy",
                goal="legacy",
                config_hash="h",
                carve_version="0.0.1",
                estimates_json="{}",
                task_graph_json=json.dumps(
                    {"pipeline_dir": "pipelines/legacy_pipe"}
                ),
                file_path="/x",
                created_at=now,
                expires_at=now,
                applied_at=now,
                apply_run_id="run_legacy",
            )
        )
    legacy_engine.dispose()

    # Now boot through Carve's normal initialize path.
    engine = create_engine_from_config(_make_config(project_dir), project_dir=project_dir)
    initialize_database(engine)

    with engine.begin() as conn:
        # alembic_version is now present.
        assert "alembic_version" in {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        # The pipelines table exists with a backfilled row. After 0004
        # `current_plan_id` is gone; the pipeline now points at a
        # synthesized Build row via `current_build_id`.
        pipelines = list(
            conn.execute(
                text(
                    "SELECT name, pipeline_dir, current_build_id FROM pipelines"
                )
            )
        )
        assert len(pipelines) == 1
        name, pipeline_dir, build_id = pipelines[0]
        assert name == "legacy_pipe"
        assert pipeline_dir == "pipelines/legacy_pipe"
        assert isinstance(build_id, str) and build_id.startswith("build_")

        # The Build row backfilled from the legacy pipeline binds the
        # original plan to the project's default target.
        build_row = conn.execute(
            text(
                "SELECT pipeline_name, plan_id, target, manifest_json "
                "FROM builds WHERE id = :id"
            ),
            {"id": build_id},
        ).one()
        assert build_row[0] == "legacy_pipe"
        assert build_row[1] == "plan_legacy"
        # No carve.toml in the migration tmp dir, so the backfill falls
        # back to "dev".
        assert build_row[2] == "dev"
        assert build_row[3] == '{"files": []}'

        # The plan was marked built and linked to its pipeline.
        plan_row = conn.execute(
            text("SELECT phase, pipeline_name FROM plans WHERE id = 'plan_legacy'")
        ).one()
        assert plan_row == ("built", "legacy_pipe")


def test_legacy_db_skips_backfill_for_malformed_pipeline_dir(
    project_dir: Path,
) -> None:
    """A legacy plan whose `task_graph_json.pipeline_dir` derives an invalid
    pipeline_name (path traversal, hyphens, whitespace) does NOT poison a
    Pipeline row. The plan stays in `phase='drafted'` with no
    `pipeline_name`.
    """
    db_path = project_dir / ".carve" / "state.db"
    legacy_url = f"sqlite:///{db_path}"
    legacy_engine = sa.create_engine(legacy_url)
    md = sa.MetaData()
    runs = sa.Table(
        "runs",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("target_id", sa.String, nullable=False),
        sa.Column("owner_user_id", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String, nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("error_message", sa.String, nullable=True),
        sa.Column("tokens_input", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_output", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    sa.Table(
        "logs",
        md,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String, sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("level", sa.String, nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("message", sa.String, nullable=False),
    )
    plans = sa.Table(
        "plans",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("parent_plan_id", sa.String, nullable=True),
        sa.Column("goal", sa.String, nullable=False),
        sa.Column("config_hash", sa.String, nullable=False),
        sa.Column("carve_version", sa.String, nullable=False),
        sa.Column("estimates_json", sa.String, nullable=False),
        sa.Column("task_graph_json", sa.String, nullable=False),
        sa.Column("file_path", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("applied_at", sa.DateTime, nullable=True),
        sa.Column("apply_run_id", sa.String, sa.ForeignKey("runs.id"), nullable=True),
    )
    md.create_all(legacy_engine)

    now = datetime.now(UTC).replace(tzinfo=None)
    with legacy_engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                id="run_bad",
                kind="apply",
                target_id="plan_bad",
                created_at=now,
            )
        )
        # `pipeline_dir` resolves to "Bad-Name" via rsplit — the
        # backfill must reject it before inserting any Pipeline row.
        conn.execute(
            plans.insert().values(
                id="plan_bad",
                goal="legacy",
                config_hash="h",
                carve_version="0.0.1",
                estimates_json="{}",
                task_graph_json=json.dumps(
                    {"pipeline_dir": "pipelines/Bad-Name"}
                ),
                file_path="/x",
                created_at=now,
                expires_at=now,
                applied_at=now,
                apply_run_id="run_bad",
            )
        )
    legacy_engine.dispose()

    engine = create_engine_from_config(_make_config(project_dir), project_dir=project_dir)
    initialize_database(engine)

    with engine.begin() as conn:
        pipelines = list(conn.execute(text("SELECT name FROM pipelines")))
        assert pipelines == []
        # Plan stays at phase='drafted' (the server_default) because
        # the backfill skipped the row before marking it built.
        plan_row = conn.execute(
            text("SELECT phase, pipeline_name FROM plans WHERE id = 'plan_bad'")
        ).one()
        assert plan_row == ("drafted", None)


# ---------------------------------------------------------------------------
# 0003_rename_apply_to_deploy
# ---------------------------------------------------------------------------


def test_rename_apply_to_deploy_renames_columns_and_rewrites_kinds(
    project_dir: Path,
) -> None:
    """0003 renames the two `plans` columns and rewrites apply-kind run rows.

    Builds a fresh DB through 0001+0002, then upgrades to 0003 (one
    before head, since 0004 drops the renamed columns). Checks both
    `deployed_at` and `deploy_run_id` are present at that revision.
    """
    from alembic import command as alembic_command

    from carve.core.state.database import _alembic_config

    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)

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


def test_rename_apply_to_deploy_round_trip(project_dir: Path) -> None:
    """Upgrade to 0003, downgrade -1, upgrade 0003 again — schema is stable.

    Pinned to revision 0003 because 0004 drops the renamed columns; the
    round-trip we want to exercise here is *just* the rename, not the
    later prune.
    """
    from alembic import command as alembic_command

    from carve.core.state.database import _alembic_config

    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)

    cfg = _alembic_config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "0003_rename_apply_to_deploy")

    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.downgrade(cfg, "-1")

    inspector = inspect(engine)
    plan_cols = {c["name"] for c in inspector.get_columns("plans")}
    # After downgrade, the legacy column names are restored.
    assert "applied_at" in plan_cols
    assert "apply_run_id" in plan_cols
    assert "deployed_at" not in plan_cols
    assert "deploy_run_id" not in plan_cols

    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "0003_rename_apply_to_deploy")

    inspector = inspect(engine)
    plan_cols = {c["name"] for c in inspector.get_columns("plans")}
    assert "deployed_at" in plan_cols
    assert "deploy_run_id" in plan_cols


def test_rename_apply_to_deploy_rewrites_apply_kind_runs(project_dir: Path) -> None:
    """A pre-existing run with kind='apply' is rewritten to kind='deploy'.

    Defensive: M1.1-06 reserved `kind='apply'` for the M1 stub but never
    inserted such rows. Smoke-test DBs from earlier dev cycles may still
    carry them; the migration normalizes them so future filters on
    `kind='deploy'` cover the legacy data.
    """
    from alembic import command as alembic_command

    from carve.core.state.database import _alembic_config

    # Bring the DB up to 0002 (one before head) and seed an apply-kind row.
    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)

    cfg = _alembic_config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "0002_pipeline_centric")

    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs "
                "(id, kind, target_id, owner_user_id, status, "
                " tokens_input, tokens_output, cost_usd, created_at) "
                "VALUES "
                "(:id, :kind, :target_id, 1, 'queued', 0, 0, 0.0, :created_at)"
            ),
            {
                "id": "run_legacy_apply",
                "kind": "apply",
                "target_id": "plan_x",
                "created_at": now,
            },
        )

    # Run 0003.
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "head")

    with engine.begin() as conn:
        kind = conn.execute(
            text("SELECT kind FROM runs WHERE id = 'run_legacy_apply'")
        ).scalar_one()
        assert kind == "deploy"


# ---------------------------------------------------------------------------
# 0004_build_entity
# ---------------------------------------------------------------------------


def test_0004_creates_builds_and_renames_pipeline_fk(project_dir: Path) -> None:
    """After head: `builds` table exists; `pipelines.current_build_id` does too.

    Counterpart: `pipelines.current_plan_id` is gone.
    """
    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
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

    # The builds index supporting "latest build for (pipeline, target)".
    indexes = {ix["name"] for ix in inspector.get_indexes("builds")}
    assert "ix_builds_pipeline_target_created_at" in indexes


def test_0004_backfills_builds_from_existing_pipelines(project_dir: Path) -> None:
    """Pipelines with non-null current_plan_id get a synthesized Build row.

    Constructs a synthetic legacy DB at 0003 (with pipelines having
    current_plan_id), upgrades to head, and asserts every such pipeline
    has a build and a stamped current_build_id.
    """
    from alembic import command as alembic_command

    from carve.core.state.database import _alembic_config

    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
    cfg = _alembic_config(engine)

    # Bring DB up to 0003 (just before 0004) and seed a Plan + Pipeline.
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "0003_rename_apply_to_deploy")

    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plans "
                "(id, goal, config_hash, carve_version, estimates_json, "
                " task_graph_json, file_path, phase, created_at, expires_at, "
                " deployed_at) "
                "VALUES "
                "(:id, 'g', 'h', '0.0.1', '{}', '{}', '/tmp/p.json', 'built', "
                " :now, :now, :deployed)"
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

    # Apply 0004.
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "head")

    with engine.begin() as conn:
        # Pipeline points at a synthesized build.
        build_id = conn.execute(
            text("SELECT current_build_id FROM pipelines WHERE name = 'ingest'")
        ).scalar_one()
        assert isinstance(build_id, str) and build_id.startswith("build_")

        build = conn.execute(
            text(
                "SELECT pipeline_name, plan_id, target, manifest_json "
                "FROM builds WHERE id = :id"
            ),
            {"id": build_id},
        ).one()
        assert build[0] == "ingest"
        assert build[1] == "plan_x"
        assert build[2] == "dev"
        assert build[3] == '{"files": []}'


def test_0004_drops_vestigial_plan_columns(project_dir: Path) -> None:
    """After head: `plans` no longer has estimates_json, deployed_at, deploy_run_id."""
    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)

    inspector = inspect(engine)
    plan_cols = {c["name"] for c in inspector.get_columns("plans")}
    assert "estimates_json" not in plan_cols
    assert "deployed_at" not in plan_cols
    assert "deploy_run_id" not in plan_cols
    # Sanity: the columns we kept are still there.
    assert {"id", "phase", "pipeline_name", "task_graph_json"}.issubset(plan_cols)


def test_0004_round_trip(project_dir: Path) -> None:
    """upgrade → downgrade → upgrade against head — schema is stable across 0004."""
    from alembic import command as alembic_command

    from carve.core.state.database import _alembic_config

    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)

    inspector = inspect(engine)
    plan_cols_after_head = {c["name"] for c in inspector.get_columns("plans")}
    pipeline_cols_after_head = {c["name"] for c in inspector.get_columns("pipelines")}

    cfg = _alembic_config(engine)
    # Walk back to revision 0003 — past 0005 (P1-07's `runs.target`) and
    # 0004 (the build entity). Pinned to a named rev rather than ``"-2"``
    # so future migrations don't change the relative offset.
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.downgrade(cfg, "0003_rename_apply_to_deploy")

    inspector = inspect(engine)
    tables_after_downgrade = set(inspector.get_table_names())
    assert "builds" not in tables_after_downgrade
    plan_cols_after_downgrade = {c["name"] for c in inspector.get_columns("plans")}
    assert "estimates_json" in plan_cols_after_downgrade
    assert "deployed_at" in plan_cols_after_downgrade
    assert "deploy_run_id" in plan_cols_after_downgrade
    pipeline_cols_after_downgrade = {c["name"] for c in inspector.get_columns("pipelines")}
    assert "current_plan_id" in pipeline_cols_after_downgrade
    assert "current_build_id" not in pipeline_cols_after_downgrade

    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "head")

    inspector = inspect(engine)
    plan_cols_after_re_upgrade = {c["name"] for c in inspector.get_columns("plans")}
    pipeline_cols_after_re_upgrade = {c["name"] for c in inspector.get_columns("pipelines")}
    assert plan_cols_after_re_upgrade == plan_cols_after_head
    assert pipeline_cols_after_re_upgrade == pipeline_cols_after_head
    tables_after_re_upgrade = set(inspector.get_table_names())
    assert "builds" in tables_after_re_upgrade


def test_0004_downgrade_repopulates_current_plan_id_from_latest_build(
    project_dir: Path,
) -> None:
    """Downgrade -1 picks up the most recent Build's plan_id per pipeline.

    Builds two Build rows for the same pipeline against different targets;
    the more recent one wins when downgrade re-stamps current_plan_id.
    """
    from alembic import command as alembic_command

    from carve.core.state.database import _alembic_config

    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)

    now = datetime.now(UTC).replace(tzinfo=None)
    earlier = now.replace(microsecond=0)
    later = earlier.replace(second=(earlier.second + 1) % 60)

    with engine.begin() as conn:
        # Two plans, one pipeline, two builds (later one is canonical).
        for pid in ("plan_a", "plan_b"):
            conn.execute(
                text(
                    "INSERT INTO plans (id, goal, config_hash, carve_version, "
                    "task_graph_json, file_path, phase, created_at, "
                    "expires_at) VALUES "
                    "(:id, 'g', 'h', '0.0.1', '{}', :file, 'built', :now, :now)"
                ),
                {"id": pid, "file": f"/tmp/{pid}.json", "now": now},
            )
        conn.execute(
            text(
                "INSERT INTO pipelines (name, description, pipeline_dir, "
                "current_build_id, created_at, updated_at) "
                "VALUES ('ingest', '', 'targets/dev/el/ingest', NULL, :now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO builds (id, pipeline_name, plan_id, target, "
                "created_at, manifest_json) VALUES "
                "('build_a', 'ingest', 'plan_a', 'dev', :ts, '{}')"
            ),
            {"ts": earlier},
        )
        conn.execute(
            text(
                "INSERT INTO builds (id, pipeline_name, plan_id, target, "
                "created_at, manifest_json) VALUES "
                "('build_b', 'ingest', 'plan_b', 'prod', :ts, '{}')"
            ),
            {"ts": later},
        )

    cfg = _alembic_config(engine)
    # Walk back past 0005 → 0004 → 0003 to drop `current_build_id` and
    # restore `current_plan_id`. Pinned to a named revision so later
    # migrations don't shift the offset.
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.downgrade(cfg, "0003_rename_apply_to_deploy")

    with engine.begin() as conn:
        plan_id = conn.execute(
            text("SELECT current_plan_id FROM pipelines WHERE name = 'ingest'")
        ).scalar_one()
        assert plan_id == "plan_b"


# ---------------------------------------------------------------------------
# 0005_runs_target
# ---------------------------------------------------------------------------


def test_0005_adds_runs_target_column(project_dir: Path) -> None:
    """After head: `runs.target` exists as a nullable text column."""
    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)

    inspector = inspect(engine)
    run_cols = {c["name"]: c for c in inspector.get_columns("runs")}
    assert "target" in run_cols
    assert run_cols["target"]["nullable"] is True


def test_0005_round_trip(project_dir: Path) -> None:
    """upgrade → downgrade → upgrade across 0005 — schema is stable."""
    from alembic import command as alembic_command

    from carve.core.state.database import _alembic_config

    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)

    inspector = inspect(engine)
    run_cols_at_head = {c["name"] for c in inspector.get_columns("runs")}
    assert "target" in run_cols_at_head

    cfg = _alembic_config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.downgrade(cfg, "0004_build_entity")

    inspector = inspect(engine)
    run_cols_after_downgrade = {c["name"] for c in inspector.get_columns("runs")}
    assert "target" not in run_cols_after_downgrade

    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "head")

    inspector = inspect(engine)
    run_cols_after_re_upgrade = {c["name"] for c in inspector.get_columns("runs")}
    assert run_cols_after_re_upgrade == run_cols_at_head


def test_0005_backfills_runs_target_from_latest_build(project_dir: Path) -> None:
    """Existing runs inherit their pipeline's most-recent build's target.

    Seeds at revision 0004 (before `runs.target` exists), inserts a
    pipeline + build + a run pointing at the pipeline, then upgrades to
    head. The run's `target` column is populated from the latest
    build's target; runs whose pipeline has no build keep NULL.
    """
    from alembic import command as alembic_command

    from carve.core.state.database import _alembic_config

    config = _make_config(project_dir)
    engine = create_engine_from_config(config, project_dir=project_dir)
    cfg = _alembic_config(engine)

    # Bring DB up to 0004 (just before 0005). seed pipeline + build + runs.
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "0004_build_entity")

    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plans (id, goal, config_hash, carve_version, "
                "task_graph_json, file_path, phase, created_at, expires_at) "
                "VALUES "
                "('plan_a', 'g', 'h', '0.0.1', '{}', '/tmp/p.json', 'built', "
                ":now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO pipelines (name, description, pipeline_dir, "
                "current_build_id, created_at, updated_at) "
                "VALUES ('ingest', '', 'targets/staging/el/ingest', "
                "'build_a', :now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO builds (id, pipeline_name, plan_id, target, "
                "created_at, manifest_json) VALUES "
                "('build_a', 'ingest', 'plan_a', 'staging', :now, '{}')"
            ),
            {"now": now},
        )
        # Run linked to the pipeline → should inherit `staging`.
        conn.execute(
            text(
                "INSERT INTO runs (id, kind, target_id, pipeline_name, "
                "owner_user_id, status, tokens_input, tokens_output, "
                "cost_usd, created_at) VALUES "
                "(:id, 'run', :tid, 'ingest', 1, 'success', 0, 0, 0.0, :now)"
            ),
            {"id": "run_with_pipe", "tid": "build_a", "now": now},
        )
        # Orphan run (no pipeline_name) → stays NULL.
        conn.execute(
            text(
                "INSERT INTO runs (id, kind, target_id, "
                "owner_user_id, status, tokens_input, tokens_output, "
                "cost_usd, created_at) VALUES "
                "(:id, 'plan', :tid, 1, 'success', 0, 0, 0.0, :now)"
            ),
            {"id": "run_no_pipe", "tid": "plan_a", "now": now},
        )

    # Apply 0005.
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "head")

    with engine.begin() as conn:
        target_for_with_pipe = conn.execute(
            text("SELECT target FROM runs WHERE id = 'run_with_pipe'")
        ).scalar_one()
        target_for_no_pipe = conn.execute(
            text("SELECT target FROM runs WHERE id = 'run_no_pipe'")
        ).scalar_one()
        assert target_for_with_pipe == "staging"
        assert target_for_no_pipe is None

