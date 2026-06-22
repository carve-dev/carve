"""Unit tests for `cli.orchestrator.planner.generate_plan` (M1.1-06).

The Anthropic client is fully mocked: the `_client_returning` helper
yields a sequence of pre-built responses, and the planner observes the
agent's tool_use blocks. The plan agent terminates by calling
`submit_plan(...)`; we drive that through the mock.
"""

from __future__ import annotations

import copy
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
    snapshots: list[dict[str, Any]] = []
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        snapshots.append(copy.deepcopy(kwargs))
        return next(response_iter)

    client.messages.create.side_effect = _create
    client.calls = snapshots
    return client


def _design(**overrides: Any) -> dict[str, Any]:
    """Default design payload the agent might submit."""
    base: dict[str, Any] = {
        "pipeline_name": "csv_ingest",
        "description": "Daily CSV ingest.",
        "is_new_pipeline": True,
        "source": {"type": "http_csv", "url": "https://example.com/data.csv"},
        "destination": {
            "database": "ANALYTICS",
            "schema": "RAW",
            "table": "RAW_CSV_DATA",
            "primary_key": "ID",
        },
        "transformation": {
            "strategy": "merge_upsert",
            "rationale": "Idempotent upserts on PK.",
        },
        "columns": [{"name": "ID", "type": "VARCHAR(50)", "nullable": False}],
        "requirements": ["snowflake-connector-python", "requests"],
        "estimates": {"rows": 10000},
        "tradeoffs": ["Row-by-row MERGE is slow at scale."],
        "open_questions": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- Config


def _config(state_db: str) -> Config:
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
def repository(project_dir: Path, postgres_state_store_url: str) -> Repository:
    config = _config(postgres_state_store_url)
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


# ------------------------------------------------------------------- happy path


def test_plan_emits_design_via_submit_plan_and_persists_drafted_row(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """submit_plan tool emission → Plan row with phase='drafted', no files written."""
    config = _config(postgres_state_store_url)

    # The plan agent's `submit_plan` call is now the loop terminator —
    # exactly one `messages.create` is made, so only one usage block
    # contributes to the totals.
    client = _client_returning(
        _response(
            content=[
                _tool_use_block("submit_plan", _design(), tool_id="tu_1"),
            ],
            stop_reason="tool_use",
            usage=_usage(input_tokens=2000, output_tokens=400),
        ),
    )

    artifact = generate_plan(
        goal="ingest a CSV from a public URL into Snowflake",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    assert isinstance(artifact, PlanArtifact)
    assert artifact.id.startswith("plan_")
    assert artifact.pipeline_name == "csv_ingest"
    assert artifact.description == "Daily CSV ingest."
    assert artifact.requirements == ["snowflake-connector-python", "requests"]
    assert artifact.tokens_input == 2000
    assert artifact.tokens_output == 400

    # No files under pipelines/.
    pipelines_files = list((project_dir / "pipelines").rglob("*"))
    assert pipelines_files == []

    # Plan row exists with phase=drafted.
    plan_row = repository.get_plan(artifact.id)
    assert plan_row is not None
    assert plan_row.phase == "drafted"
    assert plan_row.parent_plan_id is None
    # task_graph stores the design dict under "design". v0.1-01 changed
    # the column from TEXT to JSONB, so the ORM returns a dict directly.
    task_graph = plan_row.task_graph_json
    assert task_graph["design"]["pipeline_name"] == "csv_ingest"


def test_plan_records_zero_cost_for_unknown_model(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """An unknown `default_model` yields cost = 0 but still produces a plan."""
    config = _config(postgres_state_store_url)
    config.models.default_model = "made-up-model"

    client = _client_returning(
        _response(
            content=[_tool_use_block("submit_plan", _design(), tool_id="tu_1")],
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


# ------------------------------------------------------------------- error paths


def test_plan_errors_when_agent_does_not_call_submit_plan(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """Agent ends turn without calling submit_plan → clear error, no plan row."""
    config = _config(postgres_state_store_url)

    client = _client_returning(
        _response(
            content=[_text_block("Here is some prose.")],
            stop_reason="end_turn",
        ),
    )

    with pytest.raises(PlanGenerationError, match=r"submit_plan"):
        generate_plan(
            goal="g",
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=client,
        )

    assert repository.list_plans() == []


def test_plan_rejects_invalid_pipeline_name(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A pipeline_name with hyphens or weird chars is rejected."""
    config = _config(postgres_state_store_url)

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "submit_plan",
                    _design(pipeline_name="Bad-Name"),
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done")], stop_reason="end_turn"),
    )

    with pytest.raises(PlanGenerationError, match=r"Invalid artifact name"):
        generate_plan(
            goal="g",
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=client,
        )


def test_plan_raises_config_error_when_api_key_missing(
    project_dir: Path,
    repository: Repository,
    postgres_state_store_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential at all surfaces an actionable ConfigError.

    The unified resolver (`client_factory.make_client`) reports a missing
    *credential* — an API key or a Claude-subscription OAuth token — anchored
    at models.toml, rather than the old api-key-only error.
    """
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CARVE_HOSTED",
    ):
        monkeypatch.delenv(var, raising=False)
    config = _config(postgres_state_store_url)
    config.models.anthropic_api_key = None

    with pytest.raises(ConfigError) as exc_info:
        generate_plan(
            goal="g",
            config=config,
            project_dir=project_dir,
            repository=repository,
        )

    err = exc_info.value
    assert "credential" in err.message.lower()
    assert str(err.file) == "carve/models.toml"
    assert err.hint is not None and "ANTHROPIC_API_KEY" in err.hint


# ---------------------------------------------------------------- refine path


def test_refine_links_parent_and_threads_design_into_context(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """Refine path: parent_plan_id set; agent context includes the prior design."""
    config = _config(postgres_state_store_url)

    client = _client_returning(
        _response(
            content=[
                _tool_use_block("submit_plan", _design(), tool_id="tu_1"),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("ok")], stop_reason="end_turn"),
    )

    parent = generate_plan(
        goal="ingest the csv",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    refined_design = _design(description="Now hourly, not daily.")
    refine_client = _client_returning(
        _response(
            content=[
                _tool_use_block("submit_plan", refined_design, tool_id="tu_2"),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("refined")], stop_reason="end_turn"),
    )

    child = generate_plan(
        goal="make it hourly",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=refine_client,
        parent_plan_id=parent.id,
    )

    assert child.parent_plan_id == parent.id
    child_row = repository.get_plan(child.id)
    assert child_row is not None
    assert child_row.parent_plan_id == parent.id

    # The refine-mode initial user message should reference the parent id.
    initial_messages = refine_client.calls[0]["messages"]
    assert initial_messages[0]["role"] == "user"
    assert parent.id in initial_messages[0]["content"]

    # The system prompt sent to the API includes the prior design.
    system_prompt = refine_client.calls[0]["system"]
    assert "Refining plan" in system_prompt
    assert "Prior design" in system_prompt


def test_refine_refuses_built_plan(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """Refining a plan in phase='built' raises a clear error."""
    config = _config(postgres_state_store_url)

    # Plant a plan and mark it built.
    client = _client_returning(
        _response(
            content=[_tool_use_block("submit_plan", _design(), tool_id="tu_1")],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("done")], stop_reason="end_turn"),
    )
    plan = generate_plan(
        goal="g",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )
    # Pipeline must exist before stamping plans.pipeline_name (Postgres
    # enforces the FK that SQLite ignored in the M1 fixture flow).
    repository.create_or_update_pipeline(
        name="csv_ingest", description="", pipeline_dir="el/csv_ingest"
    )
    repository.mark_plan_built(plan_id=plan.id, pipeline_name="csv_ingest")

    with pytest.raises(PlanGenerationError, match=r"already in phase"):
        generate_plan(
            goal="more changes",
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),  # never reached
            parent_plan_id=plan.id,
        )


# ---------------------------------------------------------------- pipeline path


def test_pipeline_mode_includes_existing_files_in_context(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """`--pipeline <name>` reads on-disk files and inlines them into context."""
    config = _config(postgres_state_store_url)

    # Plant an existing pipeline directory and row under the per-target
    # layout. The planner inlines existing files from
    # `targets/<active>/el/<name>/` first.
    pipeline_dir = project_dir / "targets" / "dev" / "el" / "existing_pl"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "main.py").write_text("# previously generated\nprint('old')\n")
    (pipeline_dir / "requirements.txt").write_text("snowflake-connector-python\n")
    repository.save_plan(_dummy_plan_row("plan_seed"))
    repository.create_or_update_pipeline(
        name="existing_pl",
        description="seed",
        pipeline_dir="el/existing_pl",
    )

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "submit_plan",
                    _design(pipeline_name="existing_pl"),
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("ok")], stop_reason="end_turn"),
    )

    artifact = generate_plan(
        goal="add a column",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
        pipeline_name="existing_pl",
    )

    # System prompt embeds the existing main.py content.
    system_prompt = client.calls[0]["system"]
    assert "previously generated" in system_prompt
    assert "Existing pipeline `existing_pl`" in system_prompt

    # Plan row pinned the target pipeline.
    plan_row = repository.get_plan(artifact.id)
    assert plan_row is not None
    assert plan_row.pipeline_name == "existing_pl"


def test_pipeline_mode_rejects_unknown_pipeline(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """`--pipeline <name>` against a pipeline that doesn't exist errors out."""
    config = _config(postgres_state_store_url)
    with pytest.raises(PlanGenerationError, match=r"not found"):
        generate_plan(
            goal="g",
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=_client_returning(),
            pipeline_name="missing",
        )


def test_pipeline_mode_locks_pipeline_name_in_design(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """When --pipeline is set, the design's pipeline_name must match."""
    config = _config(postgres_state_store_url)

    pipeline_dir = project_dir / "targets" / "dev" / "el" / "existing_pl"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "main.py").write_text("print('x')")
    repository.save_plan(_dummy_plan_row("plan_seed"))
    repository.create_or_update_pipeline(
        name="existing_pl",
        description="",
        pipeline_dir="el/existing_pl",
    )

    client = _client_returning(
        _response(
            content=[
                _tool_use_block(
                    "submit_plan",
                    _design(pipeline_name="something_else"),
                    tool_id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _response(content=[_text_block("oops")], stop_reason="end_turn"),
    )
    with pytest.raises(PlanGenerationError, match=r"does not match the --pipeline"):
        generate_plan(
            goal="g",
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=client,
            pipeline_name="existing_pl",
        )


# ----------------------------------------------------------- double submit_plan


def test_submit_plan_called_twice_rejects_second_call(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A second `submit_plan` in the same turn surfaces as a tool error.

    The first call wins: the captured design corresponds to the first
    invocation, and the second tool_result block carries the executor's
    error message. With `terminator_tool="submit_plan"` the loop also
    exits after the turn, so the second design never overwrites the
    first.
    """
    config = _config(postgres_state_store_url)

    first = _design(description="first design")
    second = _design(description="overwrite attempt")

    client = _client_returning(
        _response(
            content=[
                _tool_use_block("submit_plan", first, tool_id="tu_a"),
                _tool_use_block("submit_plan", second, tool_id="tu_b"),
            ],
            stop_reason="tool_use",
        ),
        # Should not be reached — terminator_tool exits the loop after
        # the user-message-with-tool-results is appended.
        _response(content=[_text_block("late")], stop_reason="end_turn"),
    )

    artifact = generate_plan(
        goal="g",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    # The captured design matches the first call, not the second.
    assert artifact.description == "first design"
    assert artifact.design["description"] == "first design"

    # Only one messages.create was made — the terminator fired after
    # the first turn, so the second mock response was never consumed.
    assert client.messages.create.call_count == 1


# ---------------------------------------------------------- observer threading


def test_plan_forwards_observer_events(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """`generate_plan` must thread `observer` through to `AgentLoop`."""
    config = _config(postgres_state_store_url)

    class _Recorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def on_turn_start(self, turn: int) -> None:
            self.events.append(f"turn_start:{turn}")

        def on_tool_call(self, name: str, input: dict[str, Any]) -> None:
            self.events.append(f"tool_call:{name}")

        def on_tool_result(self, name: str, ok: bool, summary: str, duration_ms: int) -> None:
            self.events.append(f"tool_result:{name}:{ok}")

        def on_turn_complete(self, turn: int, input_tokens: int, output_tokens: int) -> None:
            self.events.append(f"turn_complete:{turn}")

        def on_done(
            self,
            total_turns: int,
            total_tool_calls: int,
            input_tokens: int,
            output_tokens: int,
            cost_usd: float,
        ) -> None:
            self.events.append(f"done:{total_turns}:{total_tool_calls}")

    client = _client_returning(
        _response(
            content=[_tool_use_block("submit_plan", _design(), tool_id="tu_1")],
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

    assert isinstance(artifact, PlanArtifact)
    assert "turn_start:1" in observer.events
    assert "tool_call:submit_plan" in observer.events
    assert "tool_result:submit_plan:True" in observer.events
    assert any(e.startswith("done:") for e in observer.events)


# ----------------------------------------------------------- helpers for tests


def _dummy_plan_row(plan_id: str) -> Any:
    """Build a pre-existing plan row so we can seed pipelines."""
    from carve.core.state.models import Plan

    return Plan(
        id=plan_id,
        goal="seed",
        config_hash="h",
        carve_version="0.0.1",
        task_graph_json={},
        file_path=f".carve/plans/{plan_id}.json",
    )


# ---------------------------------------------------------------------------
# Destination hint enforcement (CLI flags + goal-text FQN parsing)
# ---------------------------------------------------------------------------


def test_destination_hint_overrides_agent_choice(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """CLI flags via destination_hint override what the agent submits.

    The agent might pick `RAW_CSV_DATA` for the destination table; the
    user passed `--table FORCED_TABLE` on `carve plan`. The post-submit
    enforcement step must replace the agent's choice with the flag's.
    """
    config = _config(postgres_state_store_url)
    client = _client_returning(
        _response(
            content=[
                _tool_use_block("submit_plan", _design(), tool_id="tu_1"),
            ],
            stop_reason="tool_use",
            usage=_usage(),
        ),
    )

    artifact = generate_plan(
        goal="ingest a CSV from a public URL into Snowflake",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
        destination_hint={"table": "FORCED_TABLE", "schema": "FORCED_SCHEMA"},
    )

    # Enforced fields override the agent's design.
    dest = artifact.design["destination"]
    assert dest["table"] == "FORCED_TABLE"
    assert dest["schema"] == "FORCED_SCHEMA"
    # Database wasn't in the hint → agent's value preserved.
    assert dest["database"] == "ANALYTICS"


def test_goal_text_fqn_seeds_destination_when_no_flags(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """An FQN parsed from goal text becomes the destination when no
    CLI flags are provided. CLI flags would still take precedence — see
    the test above — but here we exercise the goal-only path."""
    config = _config(postgres_state_store_url)
    client = _client_returning(
        _response(
            content=[
                _tool_use_block("submit_plan", _design(), tool_id="tu_1"),
            ],
            stop_reason="tool_use",
            usage=_usage(),
        ),
    )

    artifact = generate_plan(
        goal=(
            "Daily ingest of Iowa liquor sales data into "
            "ANALYTICS.SALES.IOWA_LIQUOR — keep it incremental."
        ),
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
    )

    dest = artifact.design["destination"]
    assert dest["database"] == "ANALYTICS"
    assert dest["schema"] == "SALES"
    assert dest["table"] == "IOWA_LIQUOR"


def test_cli_flags_beat_goal_text_fqn(
    project_dir: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """When BOTH the goal text and a CLI flag specify a field, the
    flag wins. Goal-text parse is a fallback for unset flags."""
    config = _config(postgres_state_store_url)
    client = _client_returning(
        _response(
            content=[
                _tool_use_block("submit_plan", _design(), tool_id="tu_1"),
            ],
            stop_reason="tool_use",
            usage=_usage(),
        ),
    )

    artifact = generate_plan(
        goal="ingest the iowa data into ANALYTICS.SALES.IOWA",
        config=config,
        project_dir=project_dir,
        repository=repository,
        client=client,
        destination_hint={"table": "FLAG_WIN"},
    )

    dest = artifact.design["destination"]
    # Flag wins on table.
    assert dest["table"] == "FLAG_WIN"
    # Goal text picked schema and database.
    assert dest["database"] == "ANALYTICS"
    assert dest["schema"] == "SALES"
