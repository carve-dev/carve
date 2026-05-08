"""Integration tests for ``carve el deploy`` (P1-08).

Drives ``run_deploy`` directly with an injected `_FakePool` and
`_FakeRecoveryHandler` so the tests exercise the orchestration logic
without spinning up a real Snowflake connection or LLM. The
deprecated alias test goes through the typer harness because that's
the surface contract.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from carve.cli.commands.el import deploy as deploy_cmd
from carve.cli.main import app as carve_app
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
    SnowflakeConnection,
)
from carve.core.deploy.recovery import (
    RecoveryContext,
    RecoveryResult,
    RecoveryStage,
)
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Plan

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeBehavior:
    """Per-target Snowflake behavior knob."""

    columns: list[dict[str, Any]] = ()  # type: ignore[assignment]
    grants: list[dict[str, Any]] = ()  # type: ignore[assignment]
    role_rows: list[dict[str, Any]] = ()  # type: ignore[assignment]
    ddl_fail_index: int | None = None
    ddl_fail_error: str = "ddl boom"
    smoke_error: Exception | None = None
    connect_error: Exception | None = None


class _FakeSnowflake:
    """Records every call; canned responses driven by `_FakeBehavior`."""

    def __init__(self, behavior: _FakeBehavior, role: str = "R") -> None:
        self.behavior = behavior
        self.executed: list[str] = []
        self.queries: list[str] = []
        self.connected = False
        # Mimic the SnowflakeConnection.config attribute the real
        # connector exposes so the deploy command's role lookup works.
        self.config = SnowflakeConnection(
            account="x",
            user="u",
            password="p",
            role=role,
            warehouse="w",
            database="d",
        )

    def connect(self) -> object:
        if self.behavior.connect_error is not None:
            raise self.behavior.connect_error
        self.connected = True
        return self

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        del params, limit
        self.queries.append(sql)
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return list(self.behavior.columns)
        if "SHOW GRANTS" in sql:
            return list(self.behavior.grants)
        if "SHOW ROLES" in sql:
            return list(self.behavior.role_rows)
        if "SELECT 1" in sql:
            if self.behavior.smoke_error is not None:
                raise self.behavior.smoke_error
            return [{"SMOKE": 1}]
        return []

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        del params
        self.executed.append(sql)
        if (
            self.behavior.ddl_fail_index is not None
            and len(self.executed) - 1 == self.behavior.ddl_fail_index
        ):
            raise RuntimeError(self.behavior.ddl_fail_error)
        return 0


class _FakePool:
    def __init__(self, by_target: dict[str, _FakeSnowflake]) -> None:
        self._by_target = by_target

    def get(self, target: str) -> _FakeSnowflake:
        if target not in self._by_target:
            from carve.core.connectors.exceptions import SnowflakeError

            raise SnowflakeError(
                f"No Snowflake connection configured for target {target!r}."
            )
        return self._by_target[target]


class _FakeRecoveryHandler:
    """Records calls; returns canned `RecoveryResult` per stage."""

    def __init__(self, results: dict[RecoveryStage, list[RecoveryResult]]) -> None:
        self.results = {k: list(v) for k, v in results.items()}
        self.calls: list[RecoveryContext] = []

    def attempt(self, context: RecoveryContext) -> RecoveryResult:
        self.calls.append(context)
        bucket = self.results.get(context.stage, [])
        if not bucket:
            return RecoveryResult(
                success=False,
                diagnosis=f"no canned result for {context.stage}",
            )
        return bucket.pop(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snowflake_section(target: str, role: str = "R") -> SnowflakeConnection:
    return SnowflakeConnection(
        account=f"{target}-a",
        user=f"{target}-u",
        password="x",
        role=role,
        warehouse="w",
        database="d",
    )


def _make_config(
    *,
    state_db: str,
    targets: tuple[str, ...] = ("dev", "prod", "prod_deploy"),
    default_target: str = "dev",
) -> Config:
    return Config(
        project=ProjectConfig(name="deploy-test", default_target=default_target),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(default_timeout_seconds=60),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(
            snowflake={t: _snowflake_section(t) for t in targets}
        ),
        config_hash="cafef00dbeefcafe",
    )


def _plant_artifact(project_dir: Path, target: str, name: str) -> Path:
    artifact = project_dir / "targets" / target / "el" / name
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "main.py").write_text("print('hello')\n")
    (artifact / "requirements.txt").write_text("")
    return artifact


def _plant_ddl(project_dir: Path, target: str, name: str, sql: str) -> Path:
    snow = project_dir / "targets" / target / "snowflake"
    snow.mkdir(parents=True, exist_ok=True)
    path = snow / f"{name}.sql"
    path.write_text(sql)
    return path


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


def _full_grants(role: str = "R") -> list[dict[str, Any]]:
    return [
        {"grantee_name": role, "privilege": p}
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
    ]


def _good_columns() -> list[dict[str, Any]]:
    return [
        {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
        {"COLUMN_NAME": "STORE", "DATA_TYPE": "VARCHAR"},
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / "targets" / "dev" / "el").mkdir(parents=True)
    (tmp_path / "targets" / "prod" / "el").mkdir(parents=True)
    (tmp_path / ".carve" / "plans").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def repository_with_build(
    project_dir: Path,
) -> tuple[Repository, Config, str]:
    """Set up state with a Pipeline + Plan + Build (target=dev)."""
    config = _make_config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    repo = Repository(create_session_factory(engine))

    repo.create_or_update_pipeline(
        name="iowa", description="", pipeline_dir="targets/dev/el/iowa"
    )
    plan = Plan(
        id="plan_1",
        goal="iowa goal",
        config_hash=config.config_hash,
        carve_version="0.0.1",
        task_graph_json=json.dumps({"design": _design()}),
        file_path=".carve/plans/plan_1.json",
    )
    repo.save_plan(plan)
    build = repo.create_build(
        pipeline_name="iowa",
        plan_id="plan_1",
        target="dev",
        manifest={"files": []},
    )
    repo.set_pipeline_current_build("iowa", build.id)

    _plant_artifact(project_dir, "dev", "iowa")
    _plant_ddl(
        project_dir,
        "dev",
        "iowa",
        (
            "CREATE TABLE IF NOT EXISTS ANALYTICS.RAW.IOWA "
            "(ID NUMBER, STORE VARCHAR(50));\n"
            "GRANT SELECT, INSERT, UPDATE, DELETE "
            "ON ANALYTICS.RAW.IOWA TO ROLE R;\n"
        ),
    )
    return repo, config, build.id


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


def test_deploy_validates_targets_defined(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, _ = repository_with_build
    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="ghost",  # not defined
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
    )
    assert code == 2
    assert "ghost" in console.export_text()


def test_deploy_refuses_same_source_and_dest(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, _ = repository_with_build
    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="dev",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
    )
    assert code == 2
    assert "differ" in console.export_text()


def test_deploy_no_build_exits_2(project_dir: Path) -> None:
    """Source artifact exists but no Build row → exit 2."""
    config = _make_config(state_db=f"sqlite:///{project_dir}/.carve/state.db")
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    repo = Repository(create_session_factory(engine))
    _plant_artifact(project_dir, "dev", "noplan")

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="noplan",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
    )
    assert code == 2
    assert "Build" in console.export_text() or "build" in console.export_text()


def test_deploy_missing_deploy_connection(
    project_dir: Path,
) -> None:
    """No `prod_deploy` block → exit 2 with doc-link error."""
    config = _make_config(
        state_db=f"sqlite:///{project_dir}/.carve/state.db",
        targets=("dev", "prod"),  # NO prod_deploy
    )
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    repo = Repository(create_session_factory(engine))
    repo.create_or_update_pipeline(
        name="iowa", description="", pipeline_dir="targets/dev/el/iowa"
    )
    plan = Plan(
        id="plan_1",
        goal="g",
        config_hash=config.config_hash,
        carve_version="0.0.1",
        task_graph_json=json.dumps({"design": _design()}),
        file_path=".carve/plans/plan_1.json",
    )
    repo.save_plan(plan)
    repo.create_build(
        pipeline_name="iowa", plan_id="plan_1", target="dev", manifest={"files": []}
    )
    _plant_artifact(project_dir, "dev", "iowa")

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
    )
    assert code == 2
    text = console.export_text()
    assert "prod_deploy" in text
    assert "deploy-roles" in text


# ---------------------------------------------------------------------------
# Recovery handoff tests
# ---------------------------------------------------------------------------


def test_deploy_preflight_drift_invokes_recovery(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, _ = repository_with_build
    # Destination has a column type mismatch on STORE.
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(
            columns=[
                {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
                {"COLUMN_NAME": "STORE", "DATA_TYPE": "BOOLEAN"},  # mismatch
            ],
            role_rows=[{"name": "R"}],
        )
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    handler = _FakeRecoveryHandler(
        {RecoveryStage.PREFLIGHT: [RecoveryResult(False, "drift unrecoverable")]}
    )

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
        pool=pool,  # type: ignore[arg-type]
        recovery=handler,
    )
    assert code == 2
    assert handler.calls and handler.calls[0].stage == RecoveryStage.PREFLIGHT


def test_deploy_ddl_apply_failure_invokes_recovery(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(
            columns=[],  # destination missing — fine, DDL creates
            role_rows=[{"name": "R"}],
            ddl_fail_index=1,  # second statement fails
            ddl_fail_error="GRANT failed: insufficient privileges",
        )
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    # Recovery refuses, so deploy exits 1 after handoff.
    handler = _FakeRecoveryHandler(
        {RecoveryStage.DDL_APPLY: [RecoveryResult(False, "cannot fix grant issue")]}
    )

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
        pool=pool,  # type: ignore[arg-type]
        recovery=handler,
    )
    assert code == 1
    assert any(c.stage == RecoveryStage.DDL_APPLY for c in handler.calls)
    assert handler.calls[0].failing_statement_index == 1


def test_deploy_verify_failure_invokes_recovery(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(columns=[], role_rows=[{"name": "R"}])
    )
    # Runtime has a missing INSERT grant, so verify fails.
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(
            columns=_good_columns(),
            grants=[{"grantee_name": "R", "privilege": "SELECT"}],
        )
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})
    handler = _FakeRecoveryHandler(
        {RecoveryStage.VERIFY: [RecoveryResult(False, "missing grant")]}
    )

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
        pool=pool,  # type: ignore[arg-type]
        recovery=handler,
    )
    assert code == 1
    assert any(c.stage == RecoveryStage.VERIFY for c in handler.calls)


def test_deploy_recovery_unrecoverable_exits_2(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    """Preflight-stage handler reports `success=False` → exit 2."""
    repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(
            columns=[
                {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
                {"COLUMN_NAME": "STORE", "DATA_TYPE": "BOOLEAN"},
            ],
            role_rows=[{"name": "R"}],
        )
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    handler = _FakeRecoveryHandler(
        {RecoveryStage.PREFLIGHT: [RecoveryResult(False, "exhausted budget")]}
    )

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
        pool=pool,  # type: ignore[arg-type]
        recovery=handler,
    )
    assert code == 2
    assert "exhausted" in console.export_text()


def test_deploy_recovery_disabled_with_flag(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    """`--no-auto-fix` skips recovery; first failure exits immediately."""
    repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(
            columns=[
                {"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"},
                {"COLUMN_NAME": "STORE", "DATA_TYPE": "BOOLEAN"},
            ],
            role_rows=[{"name": "R"}],
        )
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    handler = _FakeRecoveryHandler(
        {RecoveryStage.PREFLIGHT: [RecoveryResult(True, "would have fixed")]}
    )

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
        pool=pool,  # type: ignore[arg-type]
        recovery=handler,
        auto_fix=False,
    )
    assert code == 2
    # Handler must not have been called.
    assert handler.calls == []


# ---------------------------------------------------------------------------
# Happy-path / file copy / DDL apply / verify
# ---------------------------------------------------------------------------


def test_deploy_copies_files_to_dest_target(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(columns=[], role_rows=[{"name": "R"}])
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=Console(record=True, width=120),
        yes=True,
        pool=pool,  # type: ignore[arg-type]
    )
    assert code == 0
    dst_main = project_dir / "targets" / "prod" / "el" / "iowa" / "main.py"
    assert dst_main.is_file()
    assert dst_main.read_text() == "print('hello')\n"


def test_deploy_copies_ddl_file(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(columns=[], role_rows=[{"name": "R"}])
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=Console(record=True, width=120),
        yes=True,
        pool=pool,  # type: ignore[arg-type]
    )
    assert code == 0
    assert (
        project_dir / "targets" / "prod" / "snowflake" / "iowa.sql"
    ).is_file()


def test_deploy_applies_ddl_in_order(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(columns=[], role_rows=[{"name": "R"}])
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})
    deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=Console(record=True, width=120),
        yes=True,
        pool=pool,  # type: ignore[arg-type]
    )
    # First the CREATE TABLE then the GRANT.
    assert "CREATE TABLE" in deploy_fake.executed[0]
    assert "GRANT" in deploy_fake.executed[1]


def test_deploy_idempotent(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    """Re-running on an unchanged source produces no further changes."""
    repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(columns=[], role_rows=[{"name": "R"}])
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    code1 = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=Console(record=True, width=120),
        yes=True,
        pool=pool,  # type: ignore[arg-type]
    )
    assert code1 == 0

    # Snapshot destination file mtimes.
    dst_main = project_dir / "targets" / "prod" / "el" / "iowa" / "main.py"
    contents_before = dst_main.read_text()

    code2 = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=Console(record=True, width=120),
        yes=True,
        pool=pool,  # type: ignore[arg-type]
    )
    assert code2 == 0
    assert dst_main.read_text() == contents_before


def test_deploy_dest_uncommitted_changes_refused(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, _ = repository_with_build
    # Plant a destination version, then init git, then dirty it.
    _plant_artifact(project_dir, "prod", "iowa")
    subprocess.run(
        ["git", "init", "-q"], cwd=project_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=project_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    # Dirty the destination after commit.
    (project_dir / "targets" / "prod" / "el" / "iowa" / "main.py").write_text(
        "print('user edits')\n"
    )

    deploy_fake = _FakeSnowflake(
        _FakeBehavior(columns=[], role_rows=[{"name": "R"}])
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
        pool=pool,  # type: ignore[arg-type]
    )
    assert code == 2
    text = console.export_text()
    assert "uncommitted" in text or "Uncommitted" in text


def test_deploy_records_deploy_run_row(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    repo, config, build_id = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(columns=[], role_rows=[{"name": "R"}])
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=Console(record=True, width=120),
        yes=True,
        pool=pool,  # type: ignore[arg-type]
    )
    assert code == 0
    deploy_runs = [
        r
        for r in repo.list_runs(pipeline_name="iowa")
        if r.kind == "deploy"
    ]
    assert len(deploy_runs) == 1
    run = deploy_runs[0]
    assert run.target == "prod"
    assert run.target_id == build_id
    assert run.status == "success"


def test_deploy_smoke_verify_failure_exits_non_zero(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    """DDL succeeds but verify fails → exit non-zero, run.status='failed'."""
    repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(columns=[], role_rows=[{"name": "R"}])
    )
    # Runtime missing the STORE column.
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(
            columns=[{"COLUMN_NAME": "ID", "DATA_TYPE": "NUMBER"}],
            grants=_full_grants(),
        )
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=Console(record=True, width=120),
        yes=True,
        pool=pool,  # type: ignore[arg-type]
        auto_fix=False,
    )
    assert code != 0
    deploy_runs = [r for r in repo.list_runs(pipeline_name="iowa") if r.kind == "deploy"]
    assert deploy_runs[0].status == "failed"


# ---------------------------------------------------------------------------
# Legacy alias
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Input validation (path-traversal / shell-metacharacter rejection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_target",
    [
        "../escape",
        "with space",
        "Bad-Name",
        "1leading_digit",
        "",
        "../../etc",
        "..",
    ],
)
def test_deploy_refuses_unsafe_target_name_from(
    project_dir: Path,
    repository_with_build: tuple[Repository, Config, str],
    bad_target: str,
) -> None:
    """Bad ``--from`` exits 2 before any path is constructed."""
    repo, config, _ = repository_with_build
    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target=bad_target,
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
    )
    assert code == 2


@pytest.mark.parametrize(
    "bad_target",
    ["../escape", "with space", "Bad-Name", "1leading", "", ".."],
)
def test_deploy_refuses_unsafe_target_name_to(
    project_dir: Path,
    repository_with_build: tuple[Repository, Config, str],
    bad_target: str,
) -> None:
    """Bad ``--to`` exits 2 before any path is constructed."""
    repo, config, _ = repository_with_build
    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target=bad_target,
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
    )
    assert code == 2


@pytest.mark.parametrize(
    "bad_name",
    [
        "../escape",
        "with space",
        "Bad-Name",
        "1leading",
        "",
        "../../foo",
        "foo/bar",
    ],
)
def test_deploy_refuses_unsafe_artifact_name(
    project_dir: Path,
    repository_with_build: tuple[Repository, Config, str],
    bad_name: str,
) -> None:
    """Bad ``<name>`` exits 2 before any path is constructed."""
    repo, config, _ = repository_with_build
    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name=bad_name,
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
    )
    assert code == 2


def test_record_terminal_failure_does_not_persist_sql_text(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    """A failing DDL statement's SQL text must NOT land in Run.error_message."""
    repo, config, _ = repository_with_build
    # Plant a DDL whose first line is sensitive (the canonical worry:
    # CREATE USER ... PASSWORD '...').
    _plant_ddl(
        project_dir,
        "dev",
        "iowa",
        (
            "CREATE TABLE IF NOT EXISTS ANALYTICS.RAW.IOWA "
            "(ID NUMBER, STORE VARCHAR(50));\n"
            "GRANT SELECT ON ANALYTICS.RAW.IOWA TO ROLE secret_passw0rd;\n"
        ),
    )
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(
            columns=[],
            role_rows=[{"name": "R"}],
            ddl_fail_index=1,
            ddl_fail_error="boom",
        )
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
        pool=pool,  # type: ignore[arg-type]
        auto_fix=False,
    )
    assert code != 0
    deploy_runs = [
        r for r in repo.list_runs(pipeline_name="iowa") if r.kind == "deploy"
    ]
    assert deploy_runs
    err = deploy_runs[0].error_message or ""
    assert "secret_passw0rd" not in err
    assert "GRANT" not in err
    # Index + driver error should be surfaced; SQL body should not.
    assert "#1" in err
    assert "boom" in err


def test_carve_deploy_legacy_alias_warns_and_forwards(
    project_dir: Path,
    repository_with_build: tuple[Repository, Config, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`carve deploy` (root-level) prints deprecation banner and runs."""
    _repo, config, _ = repository_with_build
    deploy_fake = _FakeSnowflake(
        _FakeBehavior(columns=[], role_rows=[{"name": "R"}])
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    # Patch SnowflakePool to return our fake pool whenever the deploy
    # command instantiates one. Easier than threading through the
    # typer harness.
    import carve.cli.commands.el.deploy as cli_deploy

    def _make_pool(cfg: Config) -> Any:
        del cfg
        return pool

    monkeypatch.setattr(cli_deploy, "SnowflakePool", _make_pool)

    # Patch load_config so the typer harness picks up our test config.
    monkeypatch.setattr(cli_deploy, "load_config", lambda _p: config)

    # Patch state init helpers similarly so the harness doesn't blow
    # up trying to re-create the engine.
    monkeypatch.setattr(
        cli_deploy,
        "create_engine_from_config",
        lambda c, project_dir: create_engine_from_config(c, project_dir=project_dir),
    )

    runner = CliRunner()
    result = runner.invoke(
        carve_app,
        [
            "--project-dir",
            str(project_dir),
            "deploy",
            "iowa",
            "--from",
            "dev",
            "--to",
            "prod",
            "--yes",
        ],
    )
    # Banner should appear regardless of underlying success/fail.
    assert "deprecated" in result.output.lower()
    assert "carve el deploy" in result.output


def test_deploy_recovery_persists_child_run_rows_linked_to_deploy(
    project_dir: Path, repository_with_build: tuple[Repository, Config, str]
) -> None:
    """Recovery attempts during deploy land as child Run rows linked to
    the deploy run via ``parent_run_id``. ``carve runs <id> --recovery``
    reads this chain to render the recovery tree.

    This pins the gap that existed at the end of P1-09: the unified
    `run_with_recovery` loop creates child rows for `el run` failures,
    but deploy's inline shims used to call the handler without
    persisting any chain — so the deploy recovery tree was empty.
    """
    repo, config, _ = repository_with_build

    deploy_fake = _FakeSnowflake(
        _FakeBehavior(
            columns=[],
            role_rows=[{"name": "R"}],
            ddl_fail_index=1,
            ddl_fail_error="missing schema",
        )
    )
    runtime_fake = _FakeSnowflake(
        _FakeBehavior(columns=_good_columns(), grants=_full_grants())
    )
    pool = _FakePool({"prod_deploy": deploy_fake, "prod": runtime_fake})

    handler = _FakeRecoveryHandler(
        {RecoveryStage.DDL_APPLY: [RecoveryResult(False, "out of scope")]}
    )

    console = Console(record=True, width=120)
    code = deploy_cmd.run_deploy(
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        config=config,
        project_dir=project_dir,
        repository=repo,
        console=console,
        yes=True,
        pool=pool,  # type: ignore[arg-type]
        recovery=handler,
    )
    assert code == 1

    # The deploy Run row exists.
    all_runs = repo.list_runs(pipeline_name="iowa")
    deploy_runs = [r for r in all_runs if r.kind == "deploy"]
    assert len(deploy_runs) == 1
    deploy_run = deploy_runs[0]

    # And its recovery attempt is linked via parent_run_id.
    children = repo.get_recovery_children(deploy_run.id)
    assert len(children) >= 1
    assert all(c.parent_run_id == deploy_run.id for c in children)
    # Stage shows up in the kind for tree-rendering clarity.
    assert any("recovery_ddl_apply" in (c.kind or "") for c in children)
