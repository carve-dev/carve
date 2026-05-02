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
        # The pipelines table exists with a backfilled row.
        pipelines = list(
            conn.execute(text("SELECT name, pipeline_dir, current_plan_id FROM pipelines"))
        )
        assert pipelines == [("legacy_pipe", "pipelines/legacy_pipe", "plan_legacy")]
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
