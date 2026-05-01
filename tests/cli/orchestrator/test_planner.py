"""Unit tests for `cli.orchestrator.planner.generate_plan`.

The Anthropic client is fully mocked: the `_client_returning` helper
yields a sequence of pre-built responses, and the planner observes the
agent's tool_use blocks just like it would in production. The
`write_file` tool is the real one, so each tool_use ends up writing a
real file under `tmp_path / "pipelines/..."` — which is what the
planner reads back to assemble its `PlanArtifact`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.cli.orchestrator.planner import (
    PlanArtifact,
    PlanGenerationError,
    generate_plan,
)
from carve.core.config import ConfigError
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
)
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


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, input_: dict[str, Any], tool_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _response(
    *,
    content: list[Any],
    stop_reason: str,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=usage if usage is not None else _usage(),
    )


def _client_returning(*responses: Any) -> MagicMock:
    """Mock Anthropic client that records `messages.create` snapshots."""
    client = MagicMock()
    snapshots: list[list[dict[str, Any]]] = []
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        snapshots.append(copy.deepcopy(kwargs.get("messages", [])))
        return next(response_iter)

    client.messages.create.side_effect = _create
    client.messages_per_call = snapshots
    return client


# ---------------------------------------------------------------- Config


def _config(state_db: str = "sqlite:///.carve/state.db") -> Config:
    return Config(
        project=ProjectConfig(name="planner-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(snowflake={}),
        config_hash="0123456789abcdef",
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / "pipelines").mkdir()
    return tmp_path


@pytest.fixture
def repository(project_dir: Path) -> Repository:
    config = _config()
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


# ------------------------------------------------------------------- happy path


def test_plan_generates_plan_artifact_and_persists_row(
    project_dir: Path, repository: Repository
) -> None:
    """The agent writes main.py + requirements.txt; the planner records both."""
    config = _config()

    main_py_content = (
        "import requests\nimport snowflake.connector\nprint('ingest started')\n"
    )
    requirements_content = "requests\nsnowflake-connector-python\n"
    summary_text = "Built an ingestion pipeline that downloads a CSV and writes to Snowflake."

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/csv_ingest/main.py",
                        "content": main_py_content,
                    },
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
            usage=_usage(input_tokens=2000, output_tokens=400),
        ),
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/csv_ingest/requirements.txt",
                        "content": requirements_content,
                    },
                    tool_id="tu_2",
                ),
            ],
            stop_reason="tool_use",
            usage=_usage(input_tokens=2200, output_tokens=100),
        ),
        _response(
            content=[_text_block(summary_text)],
            stop_reason="end_turn",
            usage=_usage(input_tokens=2300, output_tokens=80),
        ),
    )

    artifact = generate_plan(
        goal="ingest a CSV from a public URL into a Snowflake table",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    # Plan artifact basics
    assert isinstance(artifact, PlanArtifact)
    assert artifact.id.startswith("plan_")
    assert artifact.goal.startswith("ingest a CSV")
    assert artifact.summary == summary_text
    assert artifact.pipeline_name == "csv_ingest"
    assert artifact.pipeline_dir == "pipelines/csv_ingest"
    assert artifact.script_path == "pipelines/csv_ingest/main.py"
    assert artifact.requirements_path == "pipelines/csv_ingest/requirements.txt"
    assert "requests" in artifact.requirements
    assert "snowflake-connector-python" in artifact.requirements
    assert artifact.tokens_input == 2000 + 2200 + 2300
    assert artifact.tokens_output == 400 + 100 + 80
    assert artifact.config_hash == config.config_hash
    assert artifact.cost_usd > 0  # Sonnet has positive pricing

    # Files on disk
    assert artifact.file_path.is_file()
    saved = json.loads(artifact.file_path.read_text())
    assert saved["id"] == artifact.id
    assert saved["script_path"] == "pipelines/csv_ingest/main.py"
    assert saved["requirements"] == artifact.requirements
    assert saved["tokens_input"] == artifact.tokens_input

    # Plan row
    plan_row = repository.get_plan(artifact.id)
    assert plan_row is not None
    assert plan_row.goal == artifact.goal
    assert plan_row.config_hash == config.config_hash
    estimates = json.loads(plan_row.estimates_json)
    assert estimates["tokens_input"] == artifact.tokens_input
    task_graph = json.loads(plan_row.task_graph_json)
    assert task_graph["script_path"] == "pipelines/csv_ingest/main.py"
    assert task_graph["requirements_path"] == "pipelines/csv_ingest/requirements.txt"
    assert plan_row.file_path == str(artifact.file_path)


def test_plan_records_zero_cost_for_unknown_model(
    project_dir: Path, repository: Repository
) -> None:
    """An unknown `default_model` yields cost = 0 but still produces a plan."""
    config = _config()
    config.models.default_model = "made-up-model"

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/p/main.py",
                        "content": "print('hi')\n",
                    },
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/p/requirements.txt",
                        "content": "snowflake-connector-python\n",
                    },
                    tool_id="tu_2",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done")], stop_reason="end_turn"),
    )

    artifact = generate_plan(
        goal="g",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    assert artifact.cost_usd == 0.0


# ------------------------------------------------------------------- error path


def test_plan_errors_when_no_main_py_written(
    project_dir: Path, repository: Repository
) -> None:
    """Agent writes only requirements.txt — planner must surface a clear error."""
    config = _config()

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/x/requirements.txt",
                        "content": "snowflake-connector-python\n",
                    },
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done (sort of)")], stop_reason="end_turn"),
    )

    with pytest.raises(PlanGenerationError, match=r"did not write a pipeline `main\.py`"):
        generate_plan(
            goal="g",
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=client,
        )

    # No plan row should exist
    assert repository.list_plans() == []


def test_plan_falls_back_to_default_requirements_when_missing(
    project_dir: Path, repository: Repository
) -> None:
    """If the agent forgets requirements.txt we synthesise a sensible default."""
    config = _config()

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/p/main.py",
                        "content": "print('ok')\n",
                    },
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done")], stop_reason="end_turn"),
    )

    artifact = generate_plan(
        goal="g",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    assert artifact.requirements == ["snowflake-connector-python"]


def test_plan_id_format(
    project_dir: Path, repository: Repository
) -> None:
    """Plan id format: `plan_<UTC-YYYYMMDD-HHMMSS>_<6hex>`."""
    config = _config()
    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {"path": "pipelines/p/main.py", "content": "x\n"},
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("ok")], stop_reason="end_turn"),
    )

    artifact = generate_plan(
        goal="g",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    parts = artifact.id.split("_")
    # ["plan", "YYYYMMDD", "HHMMSS", "<6hex>"]
    assert parts[0] == "plan"
    assert len(parts) == 4
    assert len(parts[1]) == 8 and parts[1].isdigit()
    assert len(parts[2]) == 6 and parts[2].isdigit()
    assert len(parts[3]) == 6 and all(c in "0123456789abcdef" for c in parts[3])


def test_plan_raises_config_error_when_api_key_missing(
    project_dir: Path, repository: Repository
) -> None:
    """`models.anthropic_api_key=None` is allowed at load-time; plan must
    surface a ConfigError pointing the user at `carve/models.toml`. The
    plan command's existing handler maps that to exit code 2."""
    config = _config()
    config.models.anthropic_api_key = None

    with pytest.raises(ConfigError) as exc_info:
        generate_plan(
            goal="g",
            config=config,
            project_dir=project_dir,
            repository=repository,
            # No client provided — forces the planner to consult the config.
        )

    err = exc_info.value
    assert err.field == "models.anthropic_api_key"
    assert err.file is not None and err.file.as_posix() == "carve/models.toml"
    assert err.hint is not None and "ANTHROPIC_API_KEY" in err.hint


def test_plan_forwards_observer_events(
    project_dir: Path, repository: Repository
) -> None:
    """`generate_plan` must thread `observer` through to `AgentLoop`."""
    config = _config()

    class _Recorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def on_turn_start(self, turn: int) -> None:
            self.events.append(f"turn_start:{turn}")

        def on_tool_call(self, name: str, input: dict[str, Any]) -> None:
            self.events.append(f"tool_call:{name}")

        def on_tool_result(
            self, name: str, ok: bool, summary: str, duration_ms: int
        ) -> None:
            self.events.append(f"tool_result:{name}:{ok}")

        def on_turn_complete(
            self, turn: int, input_tokens: int, output_tokens: int
        ) -> None:
            self.events.append(f"turn_complete:{turn}")

        def on_done(
            self,
            total_turns: int,
            total_tool_calls: int,
            input_tokens: int,
            output_tokens: int,
            cost_usd: float,
        ) -> None:
            self.events.append(
                f"done:{total_turns}:{total_tool_calls}"
            )

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {"path": "pipelines/p/main.py", "content": "print('x')\n"},
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done")], stop_reason="end_turn"),
    )
    observer = _Recorder()

    artifact = generate_plan(
        goal="g",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
        observer=observer,
    )

    # Plan still persists normally
    assert isinstance(artifact, PlanArtifact)
    assert repository.get_plan(artifact.id) is not None

    # Observer received turn / tool / done events
    assert "turn_start:1" in observer.events
    assert "tool_call:write_file" in observer.events
    assert "tool_result:write_file:True" in observer.events
    assert any(e.startswith("done:") for e in observer.events)


def test_plan_skips_flag_shaped_requirements(
    project_dir: Path, repository: Repository
) -> None:
    """`-r ...`, `--index-url=...` etc. are filtered before being recorded."""
    config = _config()

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/p/main.py",
                        "content": "x\n",
                    },
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(
            content=[
                _tool_use_block(
                    "write_file",
                    {
                        "path": "pipelines/p/requirements.txt",
                        "content": (
                            "# top of file\n"
                            "snowflake-connector-python\n"
                            "-r other.txt\n"
                            "--index-url=https://example.com\n"
                            "requests\n"
                            "\n"
                        ),
                    },
                    tool_id="tu_2",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done")], stop_reason="end_turn"),
    )

    artifact = generate_plan(
        goal="g",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    assert artifact.requirements == ["snowflake-connector-python", "requests"]
