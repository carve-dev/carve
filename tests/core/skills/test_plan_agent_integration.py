"""Integration: plan agent invokes a catalog skill via the agent loop.

Reuses the agent-loop test harness pattern from M1.1-06: a mocked
Anthropic client returns scripted responses, and we drive a sequence
where the model first calls `describe_table`, then submits a design
that mirrors the column shape returned by that skill.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.cli.orchestrator.planner import PlanArtifact, generate_plan
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
)
from carve.core.config.schema import SnowflakeConnection as ConnConfig
from carve.core.connectors.snowflake import SnowflakePool
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

# ----------------------------------------------------------- response helpers


def _usage(input_tokens: int = 100, output_tokens: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _tool_use_block(name: str, input_: dict[str, Any], tool_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _response(*, content: list[Any], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=_usage())


def _client_returning(*responses: Any) -> MagicMock:
    client = MagicMock()
    snapshots: list[dict[str, Any]] = []
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        snapshots.append(copy.deepcopy(kwargs))
        return next(response_iter)

    client.messages.create.side_effect = _create
    client.calls = snapshots
    return client


def _design_using_columns(columns: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a `submit_plan` payload referencing the given columns."""
    return {
        "pipeline_name": "events_ingest",
        "description": "Ingest events.",
        "is_new_pipeline": True,
        "source": {"type": "http_csv", "url": "https://example.com/events.csv"},
        "destination": {
            "database": "RAW",
            "schema": "RAW",
            "table": "EVENTS",
            "primary_key": "ID",
        },
        "transformation": {"strategy": "merge_upsert", "rationale": "PK upsert."},
        "columns": columns,
        "requirements": ["snowflake-connector-python", "requests"],
        "estimates": {"rows": 1000},
        "tradeoffs": [],
        "open_questions": [],
    }


# ---------------------------------------------------------------- fakes


class _FakeSnowflake:
    """A minimal `SnowflakeConnection` stub used by the plan flow."""

    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.config = ConnConfig(
            account="acct",
            user="u",
            password="p",
            role="R",
            warehouse="W",
            database="RAW",
        )

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if not self._responses:
            return []
        return self._responses.pop(0)

    # Implements `SnowflakeQueryRunner` protocol for the
    # `run_snowflake_query` tool path; not used here but keeps the stub
    # compatible if the model decides to call it.
    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
        return self.query(sql, limit=limit)


# ---------------------------------------------------------------- config


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="planner-skills"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(
            snowflake={
                "dev": ConnConfig(
                    account="acct",
                    user="u",
                    password="p",
                    role="R",
                    warehouse="W",
                    database="RAW",
                )
            }
        ),
        config_hash="0123456789abcdef",
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / "pipelines").mkdir()
    return tmp_path


@pytest.fixture
def repository(project_dir: Path, postgres_state_store_url: str) -> Repository:
    config = _config(postgres_state_store_url)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


# ---------------------------------------------------------------- the test


def test_plan_agent_can_call_catalog_skill(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan agent calls `describe_table`, then submits a design.

    We verify:

    1. The agent loop accepted the catalog-skill tool schemas in its
       first `messages.create` call.
    2. The skill executed against our fake Snowflake (the `describe_table`
       SQL appears in the recorded calls).
    3. The submitted design — captured by `submit_plan` — is the design
       the planner persisted as the artifact.
    """
    columns = [
        {
            "COLUMN_NAME": "ID",
            "DATA_TYPE": "NUMBER",
            "IS_NULLABLE": "NO",
            "ORDINAL_POSITION": 1,
        },
        {
            "COLUMN_NAME": "EVENT_NAME",
            "DATA_TYPE": "TEXT",
            "IS_NULLABLE": "YES",
            "ORDINAL_POSITION": 2,
        },
    ]
    sf = _FakeSnowflake([columns])

    # Patch `SnowflakePool.get` so every target resolves to our fake.
    def _fake_get(self: SnowflakePool, target: str) -> _FakeSnowflake:
        return sf

    monkeypatch.setattr(SnowflakePool, "get", _fake_get)

    # Two-turn script: turn 1 calls the catalog skill, turn 2 submits the
    # design that mirrors the columns the skill returned.
    design_payload = _design_using_columns(
        [
            {"name": "ID", "type": "NUMBER", "nullable": False},
            {"name": "EVENT_NAME", "type": "TEXT", "nullable": True},
        ]
    )
    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "describe_table",
                    {"database": "RAW", "schema": "RAW", "table": "EVENTS"},
                    tool_id="tu_1",
                )
            ],
            stop_reason="tool_use",
        ),
        _response(
            content=[
                _tool_use_block("submit_plan", design_payload, tool_id="tu_2"),
            ],
            stop_reason="tool_use",
        ),
    )

    artifact = generate_plan(
        goal="Ingest events into RAW.RAW.EVENTS",
        config=_config(postgres_state_store_url),
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    # The plan persisted the design exactly as submitted.
    assert isinstance(artifact, PlanArtifact)
    assert artifact.pipeline_name == "events_ingest"
    assert artifact.design["columns"] == [
        {"name": "ID", "type": "NUMBER", "nullable": False},
        {"name": "EVENT_NAME", "type": "TEXT", "nullable": True},
    ]

    # The catalog skill ran against the fake Snowflake.
    assert any(
        "information_schema.columns" in sql.lower() for sql, _ in sf.calls
    ), f"describe_table SQL not seen; calls={sf.calls}"

    # The first messages.create call advertised the catalog skills as tools.
    first_call_tools = client.calls[0]["tools"]
    tool_names = {t["name"] for t in first_call_tools}
    assert {
        "list_databases",
        "list_schemas",
        "list_tables",
        "describe_table",
        "table_exists",
    }.issubset(tool_names)
