"""Tests for ``carve el verify`` (P1-08)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from carve.cli.commands.el import verify as verify_cmd
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
    SnowflakeConnection,
)
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Plan


class _FakeSnowflake:
    def __init__(
        self,
        *,
        columns: list[dict[str, Any]],
        grants: list[dict[str, Any]],
        smoke_error: Exception | None = None,
    ) -> None:
        self.columns = columns
        self.grants = grants
        self.smoke_error = smoke_error
        self.queries: list[str] = []
        self.config = SnowflakeConnection(
            account="x",
            user="u",
            password="p",
            role="R",
            warehouse="w",
            database="d",
        )

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        del params, limit
        self.queries.append(sql)
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return list(self.columns)
        if "SHOW GRANTS" in sql:
            return list(self.grants)
        if "SELECT 1" in sql:
            if self.smoke_error is not None:
                raise self.smoke_error
            return [{"SMOKE": 1}]
        return []


class _FakePool:
    def __init__(self, by_target: dict[str, _FakeSnowflake]) -> None:
        self._by_target = by_target

    def get(self, target: str) -> _FakeSnowflake:
        return self._by_target[target]


def _design() -> dict[str, Any]:
    return {
        "destination": {
            "database": "ANALYTICS",
            "schema": "RAW",
            "table": "IOWA",
        },
        "columns": [
            {"name": "ID", "type": "NUMBER"},
            {"name": "STORE", "type": "VARCHAR(50)"},
        ],
    }


def _good_columns() -> list[dict[str, Any]]:
    return [
        {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
        {"COLUMN_NAME": "STORE", "DATA_TYPE": "VARCHAR"},
    ]


def _full_grants() -> list[dict[str, Any]]:
    return [{"grantee_name": "R", "privilege": p} for p in ("SELECT", "INSERT", "UPDATE", "DELETE")]


def _make_config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="t", default_target="dev"),
        models=ModelsConfig(anthropic_api_key="x"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(
            snowflake={
                t: SnowflakeConnection(
                    account=f"{t}-a",
                    user=f"{t}-u",
                    password="x",
                    role="R",
                    warehouse="w",
                    database="d",
                )
                for t in ("dev", "prod")
            }
        ),
        config_hash="cafef00dbeefcafe",
    )


@pytest.fixture
def repo_with_build(
    tmp_path: Path, postgres_state_store_url: str
) -> tuple[Repository, Config, Path]:
    config = _make_config(state_db=postgres_state_store_url)
    (tmp_path / ".carve" / "plans").mkdir(parents=True)
    engine = create_engine_from_config(config, project_dir=tmp_path)
    initialize_database(engine)
    repo = Repository(create_session_factory(engine))
    repo.create_or_update_pipeline(name="iowa", description="", pipeline_dir="el/iowa")
    plan = Plan(
        id="plan_1",
        goal="g",
        config_hash=config.config_hash,
        carve_version="0.0.1",
        # v0.1-01: task_graph_json is JSONB; pass a dict, not a string.
        task_graph_json={"design": _design()},
        file_path=".carve/plans/plan_1.json",
    )
    repo.save_plan(plan)
    repo.create_build(
        pipeline_name="iowa",
        plan_id="plan_1",
        target="prod",  # build for prod so verify finds it
        manifest={"files": []},
    )
    return repo, config, tmp_path


def test_verify_passes_on_correct_state(
    repo_with_build: tuple[Repository, Config, Path],
) -> None:
    repo, config, _ = repo_with_build
    pool = _FakePool({"prod": _FakeSnowflake(columns=_good_columns(), grants=_full_grants())})
    console = Console(record=True, width=120)
    code = verify_cmd.run_verify_command(
        pipeline_name="iowa",
        target="prod",
        config=config,
        repository=repo,
        console=console,
        pool=pool,  # type: ignore[arg-type]
    )
    assert code == 0
    assert "verifies clean" in console.export_text()


def test_verify_detects_column_drift(
    repo_with_build: tuple[Repository, Config, Path],
) -> None:
    repo, config, _ = repo_with_build
    pool = _FakePool(
        {
            "prod": _FakeSnowflake(
                columns=[
                    {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
                    {"COLUMN_NAME": "EXTRA", "DATA_TYPE": "VARCHAR"},
                ],
                grants=_full_grants(),
            )
        }
    )
    console = Console(record=True, width=120)
    code = verify_cmd.run_verify_command(
        pipeline_name="iowa",
        target="prod",
        config=config,
        repository=repo,
        console=console,
        pool=pool,  # type: ignore[arg-type]
    )
    assert code == 1
    text = console.export_text()
    assert "STORE" in text  # missing
    assert "EXTRA" in text  # extra


def test_verify_runtime_role_grants_check(
    repo_with_build: tuple[Repository, Config, Path],
) -> None:
    repo, config, _ = repo_with_build
    pool = _FakePool(
        {
            "prod": _FakeSnowflake(
                columns=_good_columns(),
                grants=[{"grantee_name": "R", "privilege": "SELECT"}],
            )
        }
    )
    console = Console(record=True, width=120)
    code = verify_cmd.run_verify_command(
        pipeline_name="iowa",
        target="prod",
        config=config,
        repository=repo,
        console=console,
        pool=pool,  # type: ignore[arg-type]
    )
    assert code == 1
    assert "INSERT" in console.export_text()


def test_verify_no_smoke_test_flag(
    repo_with_build: tuple[Repository, Config, Path],
) -> None:
    repo, config, _ = repo_with_build
    fake = _FakeSnowflake(
        columns=_good_columns(),
        grants=_full_grants(),
        smoke_error=RuntimeError("smoke would fail"),
    )
    pool = _FakePool({"prod": fake})

    code = verify_cmd.run_verify_command(
        pipeline_name="iowa",
        target="prod",
        config=config,
        repository=repo,
        console=Console(record=True, width=120),
        pool=pool,  # type: ignore[arg-type]
        smoke_test=False,
    )
    assert code == 0
    assert not any("SELECT 1" in q for q in fake.queries)


def test_verify_smoke_test_default_runs(
    repo_with_build: tuple[Repository, Config, Path],
) -> None:
    repo, config, _ = repo_with_build
    fake = _FakeSnowflake(columns=_good_columns(), grants=_full_grants())
    pool = _FakePool({"prod": fake})
    code = verify_cmd.run_verify_command(
        pipeline_name="iowa",
        target="prod",
        config=config,
        repository=repo,
        console=Console(record=True, width=120),
        pool=pool,  # type: ignore[arg-type]
    )
    assert code == 0
    assert any("SELECT 1" in q for q in fake.queries)


def test_verify_unknown_target_exits_2(
    repo_with_build: tuple[Repository, Config, Path],
) -> None:
    repo, config, _ = repo_with_build
    code = verify_cmd.run_verify_command(
        pipeline_name="iowa",
        target="ghost",
        config=config,
        repository=repo,
        console=Console(record=True, width=120),
    )
    assert code == 2


@pytest.mark.parametrize(
    "bad_target",
    ["../escape", "with space", "Bad-Name", "1leading", "", ".."],
)
def test_verify_refuses_unsafe_target_name(
    repo_with_build: tuple[Repository, Config, Path],
    bad_target: str,
) -> None:
    repo, config, _ = repo_with_build
    code = verify_cmd.run_verify_command(
        pipeline_name="iowa",
        target=bad_target,
        config=config,
        repository=repo,
        console=Console(record=True, width=120),
    )
    assert code == 2


@pytest.mark.parametrize(
    "bad_name",
    ["../escape", "with space", "Bad-Name", "1leading", "", "foo/bar"],
)
def test_verify_refuses_unsafe_artifact_name(
    repo_with_build: tuple[Repository, Config, Path],
    bad_name: str,
) -> None:
    repo, config, _ = repo_with_build
    code = verify_cmd.run_verify_command(
        pipeline_name=bad_name,
        target="prod",
        config=config,
        repository=repo,
        console=Console(record=True, width=120),
    )
    assert code == 2


def test_verify_no_build_exits_2(tmp_path: Path, postgres_state_store_url: str) -> None:
    config = _make_config(state_db=postgres_state_store_url)
    (tmp_path / ".carve" / "plans").mkdir(parents=True)
    engine = create_engine_from_config(config, project_dir=tmp_path)
    initialize_database(engine)
    repo = Repository(create_session_factory(engine))
    code = verify_cmd.run_verify_command(
        pipeline_name="absent",
        target="prod",
        config=config,
        repository=repo,
        console=Console(record=True, width=120),
    )
    assert code == 2
