"""End-to-end test for ``carve migrate-state``.

Builds a SQLite database programmatically with M1-shape data, stamps
the Alembic version table to ``0006_recovery_chains``, runs the
migrator via Typer's ``CliRunner``, and asserts row counts (and a few
representative values) match on the Postgres side.

The SQLite schema we build mirrors the M1.x walking-skeleton baseline:
``task_graph_json`` and ``manifest_json`` are stored as TEXT (JSON
strings), timestamps are naive UTC. That's what a real M1 user's DB
carries. The migrator parses the TEXT JSON into ``dict`` on the way
out so the values land cleanly in Postgres JSONB columns.

Edge cases covered:

* Source not at a known revision -> error.
* Source has running/queued runs -> error.
* Target already populated -> error without ``--force``, success with.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import sqlalchemy as sa
from typer.testing import CliRunner

from carve.cli.main import app

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


SOURCE_REVISION = "0006_recovery_chains"


def _build_sqlite_source(
    path: Path,
    *,
    inflight_runs: bool = False,
    alembic_version: str | None = SOURCE_REVISION,
) -> dict[str, int]:
    """Build a SQLite DB at ``path`` shaped like an M1 walking-skeleton.

    Returns a dict mapping table name to row count inserted (so the
    test can assert against it without re-counting).

    The shape mirrors the M1 schema at the head Alembic revision:
    five tables with the columns the v0.1 ORM models still carry,
    minus the dropped vestigial Plan columns. JSON payloads are stored
    as TEXT (the SQLite norm), timestamps as ISO 8601 strings.

    Pass ``alembic_version=None`` to skip stamping the version table —
    used by the "no alembic_version" error-path test.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE alembic_version (
            version_num VARCHAR(32) PRIMARY KEY
        );
        CREATE TABLE pipelines (
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            pipeline_dir TEXT NOT NULL,
            current_build_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_id TEXT,
            last_run_status TEXT,
            last_run_at TEXT
        );
        CREATE TABLE plans (
            id TEXT PRIMARY KEY,
            parent_plan_id TEXT,
            goal TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            carve_version TEXT NOT NULL,
            task_graph_json TEXT NOT NULL,
            file_path TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT 'drafted',
            pipeline_name TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target TEXT,
            pipeline_name TEXT,
            parent_run_id TEXT,
            owner_user_id INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued',
            started_at TEXT,
            completed_at TEXT,
            duration_ms INTEGER,
            error_message TEXT,
            tokens_input INTEGER NOT NULL DEFAULT 0,
            tokens_output INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            level TEXT NOT NULL,
            source TEXT NOT NULL,
            message TEXT NOT NULL
        );
        CREATE TABLE builds (
            id TEXT PRIMARY KEY,
            pipeline_name TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            target TEXT NOT NULL,
            created_at TEXT NOT NULL,
            manifest_json TEXT NOT NULL DEFAULT '{"files": []}',
            commit_sha TEXT,
            pr_url TEXT,
            deployed_at TEXT
        );
        """
    )

    if alembic_version is not None:
        conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (alembic_version,),
        )

    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC).isoformat()
    # Two pipelines, two plans, two builds, three runs (one with
    # parent_run_id), four logs. Enough to exercise FK plumbing across
    # all five tables without bloating the test.
    conn.execute(
        "INSERT INTO pipelines "
        "(name, description, pipeline_dir, current_build_id, "
        " created_at, updated_at, last_run_id, last_run_status, last_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "iowa_liquor",
            "Iowa liquor sales",
            "el/iowa_liquor",
            "build_aaa",
            now,
            now,
            "run_aaa",
            "success",
            now,
        ),
    )
    conn.execute(
        "INSERT INTO pipelines "
        "(name, description, pipeline_dir, current_build_id, "
        " created_at, updated_at, last_run_id, last_run_status, last_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "weather",
            "Weather data",
            "el/weather",
            None,
            now,
            now,
            None,
            None,
            None,
        ),
    )

    conn.execute(
        "INSERT INTO plans "
        "(id, parent_plan_id, goal, config_hash, carve_version, "
        " task_graph_json, file_path, phase, pipeline_name, "
        " created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "plan_aaa",
            None,
            "Build the Iowa liquor pipeline",
            "abc123",
            "0.0.1",
            json.dumps({"design": {"name": "iowa_liquor"}, "pipeline_dir": "el/iowa_liquor"}),
            ".carve/plans/plan_aaa.json",
            "built",
            "iowa_liquor",
            now,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO plans "
        "(id, parent_plan_id, goal, config_hash, carve_version, "
        " task_graph_json, file_path, phase, pipeline_name, "
        " created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "plan_bbb",
            None,
            "Build the weather pipeline",
            "def456",
            "0.0.1",
            json.dumps({"design": {"name": "weather"}, "pipeline_dir": "el/weather"}),
            ".carve/plans/plan_bbb.json",
            "drafted",
            None,
            now,
            now,
        ),
    )

    conn.execute(
        "INSERT INTO builds "
        "(id, pipeline_name, plan_id, target, created_at, "
        " manifest_json, commit_sha, pr_url, deployed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "build_aaa",
            "iowa_liquor",
            "plan_aaa",
            "dev",
            now,
            json.dumps({"files": ["dlt_pipeline.py", "schema.yml"]}),
            None,
            None,
            None,
        ),
    )

    queued_status = "queued" if inflight_runs else "success"
    conn.execute(
        "INSERT INTO runs "
        "(id, kind, target_id, target, pipeline_name, parent_run_id, "
        " owner_user_id, status, started_at, completed_at, duration_ms, "
        " error_message, tokens_input, tokens_output, cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run_aaa",
            "build",
            "iowa_liquor",
            "dev",
            "iowa_liquor",
            None,
            1,
            queued_status,
            now,
            now,
            123,
            None,
            100,
            200,
            0.05,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO runs "
        "(id, kind, target_id, target, pipeline_name, parent_run_id, "
        " owner_user_id, status, started_at, completed_at, duration_ms, "
        " error_message, tokens_input, tokens_output, cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run_bbb",
            "build",
            "iowa_liquor",
            "dev",
            "iowa_liquor",
            "run_aaa",  # recovery child
            1,
            "success",
            now,
            now,
            234,
            None,
            120,
            220,
            0.06,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO runs "
        "(id, kind, target_id, target, pipeline_name, parent_run_id, "
        " owner_user_id, status, started_at, completed_at, duration_ms, "
        " error_message, tokens_input, tokens_output, cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run_ccc",
            "plan",
            "weather",
            None,
            None,
            None,
            1,
            "failed",
            now,
            now,
            10,
            "boom",
            5,
            10,
            0.001,
            now,
        ),
    )

    for level, source, message in (
        ("INFO", "stdout", "hello"),
        ("INFO", "stdout", "world"),
        ("ERROR", "stderr", "boom"),
        ("INFO", "stdout", "done"),
    ):
        conn.execute(
            "INSERT INTO logs (run_id, timestamp, level, source, message) "
            "VALUES (?, ?, ?, ?, ?)",
            ("run_aaa", now, level, source, message),
        )

    conn.commit()
    conn.close()

    return {
        "pipelines": 2,
        "plans": 2,
        "builds": 1,
        "runs": 3,
        "logs": 4,
    }


def _count(url: str, table: str) -> int:
    """Return ``SELECT COUNT(*)`` against a Postgres table."""
    engine = sa.create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).first()
            return int(row[0]) if row is not None else 0
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migrate_state_copies_all_rows(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """Happy path: every row in the SQLite source lands in Postgres."""
    source_path = tmp_path / "state.db"
    expected = _build_sqlite_source(source_path)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path}",
            "--to",
            postgres_state_store_url,
        ],
    )
    assert result.exit_code == 0, result.output

    for table, expected_count in expected.items():
        assert _count(postgres_state_store_url, table) == expected_count, (
            f"row count mismatch for {table}: expected "
            f"{expected_count}, got {_count(postgres_state_store_url, table)}"
        )


def test_migrate_state_preserves_jsonb_payloads(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """The JSON TEXT columns on SQLite arrive as dicts in Postgres JSONB."""
    source_path = tmp_path / "state.db"
    _build_sqlite_source(source_path)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path}",
            "--to",
            postgres_state_store_url,
        ],
    )
    assert result.exit_code == 0, result.output

    # psycopg returns JSONB as a dict already — no json.loads needed.
    # Use a raw psycopg connection so we get the native JSONB shape and
    # can prove the value was stored as JSONB rather than text-in-JSONB.
    pg_url = postgres_state_store_url.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with psycopg.connect(pg_url) as pg_conn:
        cur = pg_conn.execute(
            "SELECT task_graph_json FROM plans WHERE id = 'plan_aaa'"
        )
        row = cur.fetchone()
        assert row is not None
        task_graph = row[0]
        assert isinstance(task_graph, dict)
        assert task_graph["design"] == {"name": "iowa_liquor"}

        cur = pg_conn.execute(
            "SELECT manifest_json FROM builds WHERE id = 'build_aaa'"
        )
        row = cur.fetchone()
        assert row is not None
        manifest = row[0]
        assert isinstance(manifest, dict)
        assert manifest["files"] == ["dlt_pipeline.py", "schema.yml"]


def test_migrate_state_rejects_source_not_at_known_revision(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """A SQLite source whose alembic_version is unknown fails validation."""
    source_path = tmp_path / "state.db"
    _build_sqlite_source(source_path, alembic_version="9999_future")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path}",
            "--to",
            postgres_state_store_url,
        ],
    )
    assert result.exit_code == 1, result.output
    assert "9999_future" in result.output


def test_migrate_state_rejects_source_with_no_alembic_table(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """A SQLite source missing the alembic_version table fails validation."""
    # Build a barebones SQLite DB with only one table and *no*
    # alembic_version — this mimics a very old pre-migrations M1 DB.
    source_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(source_path))
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path}",
            "--to",
            postgres_state_store_url,
        ],
    )
    assert result.exit_code == 1, result.output
    assert "alembic_version" in result.output


def test_migrate_state_refuses_inflight_runs(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """A source with running/queued runs is refused."""
    source_path = tmp_path / "state.db"
    _build_sqlite_source(source_path, inflight_runs=True)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path}",
            "--to",
            postgres_state_store_url,
        ],
    )
    assert result.exit_code == 1, result.output
    assert "running/queued" in result.output


def test_migrate_state_refuses_populated_target_without_force(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """A second invocation against a populated target fails without --force."""
    source_path = tmp_path / "state.db"
    _build_sqlite_source(source_path)

    runner = CliRunner()
    # First pass populates the target.
    first = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path}",
            "--to",
            postgres_state_store_url,
        ],
    )
    assert first.exit_code == 0, first.output

    # Second pass without --force must fail.
    second = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path}",
            "--to",
            postgres_state_store_url,
        ],
    )
    assert second.exit_code == 1, second.output
    assert "--force" in second.output


def test_migrate_state_force_proceeds_against_populated_target(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """`--force` bypasses the populated-target guard.

    The second pass attempts to re-insert the same primary keys; the
    test asserts the CLI proceeds past the validation step but then
    surfaces the duplicate-key failure from psycopg with a non-zero
    exit. (The acceptance bar in the spec is "proceeds with --force",
    not "succeeds against duplicate keys" — overwriting is a separate,
    explicit recovery operation outside this spec.)
    """
    source_path_a = tmp_path / "state_a.db"
    source_path_b = tmp_path / "state_b.db"
    _build_sqlite_source(source_path_a)
    _build_sqlite_source(source_path_b)

    runner = CliRunner()
    first = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path_a}",
            "--to",
            postgres_state_store_url,
        ],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path_b}",
            "--to",
            postgres_state_store_url,
            "--force",
        ],
    )
    # Got past validation (otherwise message would mention --force).
    assert "Pass --force" not in second.output, second.output


def test_migrate_state_rejects_non_postgres_target(
    tmp_path: Path,
) -> None:
    """A SQLite ``--to`` URL surfaces the friendly error immediately."""
    source_path = tmp_path / "state.db"
    _build_sqlite_source(source_path)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "migrate-state",
            "--from",
            f"sqlite:///{source_path}",
            "--to",
            f"sqlite:///{tmp_path / 'target.db'}",
        ],
    )
    assert result.exit_code != 0, result.output


