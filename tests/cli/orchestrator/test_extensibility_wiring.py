"""Live-wiring tests for the extensibility seams (spec 16, follow-up A).

Three seams, each asserted at the site the run actually wires them:

* **Hooks → the running loop.** A fixture ``carve/hooks.toml`` loaded via
  :func:`build_extensibility_hooks` produces a ``pre_tool`` hook that
  fires *through a constructed* :class:`AgentLoop` and blocks a tool call
  on a non-zero exit (the executor never runs; the result is an error).
* **Skill-pack lookup tool → the agent's tools.** ``build_skill_pack_tool``
  yields the ``lookup_skill_pack`` tool, and a live ``carve plan`` carries
  it into the loop the agent is constructed with.
* **Router → the dispatch site.** ``resolve_agent_or_fallback`` routes a
  fixture user agent on a classification match, and **falls back** (returns
  ``None`` → the M1 hardcoded path) when no declarative agent matches — so
  the existing plan/build behavior is unchanged.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.cli.orchestrator.builder import build_plan
from carve.cli.orchestrator.extensibility_wiring import (
    build_extensibility_hooks,
    build_skill_pack_tool,
    resolve_agent_or_fallback,
)
from carve.cli.orchestrator.planner import PlanArtifact, generate_plan
from carve.core.agents.loop import AgentLoop
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.routing import NoAgentMatch
from carve.core.agents.tools import Tool
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    PathsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
)
from carve.core.hooks.config import HookConfigError
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)

# --------------------------------------------------------------- mock client


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
    """Mock Anthropic client recording each ``messages.create`` kwargs."""
    client = MagicMock()
    snapshots: list[dict[str, Any]] = []
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        snapshots.append(copy.deepcopy(kwargs))
        return next(response_iter)

    client.messages.create.side_effect = _create
    client.calls = snapshots
    return client


# ------------------------------------------------------------ hooks.toml seam


_BLOCKING_HOOKS_TOML = """\
[[hook]]
on = "pre_tool"
match = { tool = "probe" }
run = "false"
"""

# A hooks.toml with a structurally-invalid hook entry (unknown event), so
# `load_hooks_config` raises HookConfigError. Present-but-malformed is the
# fail-closed boundary the wiring module promises — distinct from a *missing*
# file (which is "no hooks").
_MALFORMED_HOOKS_TOML = """\
[[hook]]
on = "not_a_real_event"
run = "true"
"""


def test_hooks_from_fixture_fire_through_a_constructed_loop(
    tmp_path: Path,
) -> None:
    """A fixture hooks.toml pre_tool hook blocks a tool call in a live loop."""
    (tmp_path / "carve").mkdir()
    (tmp_path / "carve" / "hooks.toml").write_text(_BLOCKING_HOOKS_TOML, encoding="utf-8")

    pre_hook, post_hook = build_extensibility_hooks(
        project_dir=tmp_path,
        paths=PathsConfig(),
        mode=PermissionMode.BUILD,
    )
    assert pre_hook is not None  # a hook WAS loaded from the fixture
    assert post_hook is None

    # A probe tool whose executor records whether it ran. The pre_tool hook
    # (`false`, non-zero) must block it before the executor is reached.
    ran: list[bool] = []

    def _probe(_input: dict[str, Any]) -> str:
        ran.append(True)
        return "executed"

    probe = Tool(
        name="probe",
        description="A probe tool.",
        input_schema={"type": "object", "properties": {}},
        executor=_probe,
    )

    client = _client_returning(
        _response(
            content=[_tool_use_block("probe", {}, "tu_1")],
            stop_reason="tool_use",
        ),
        _response(
            content=[SimpleNamespace(type="text", text="done")],
            stop_reason="end_turn",
        ),
    )
    loop = AgentLoop(
        client=client,
        tools=[probe],
        system_prompt="sys",
        model="claude-test",
        pre_tool_hook=pre_hook,
        post_tool_hook=post_hook,
    )
    loop.run("go", max_turns=3)

    # The hook fired and blocked the call: the executor never ran, and the
    # tool result the model saw is an error.
    assert ran == []
    second_call = client.calls[1]
    tool_results = [
        block
        for message in second_call["messages"]
        if message["role"] == "user"
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_results, "expected a tool_result for the blocked probe call"
    assert tool_results[0]["is_error"] is True


def test_missing_hooks_file_yields_no_hooks(tmp_path: Path) -> None:
    """A project with no carve/hooks.toml wires no hooks (the default)."""
    pre_hook, post_hook = build_extensibility_hooks(
        project_dir=tmp_path,
        paths=PathsConfig(),
        mode=PermissionMode.PLAN,
    )
    assert pre_hook is None
    assert post_hook is None


def test_malformed_hooks_file_is_fail_closed(tmp_path: Path) -> None:
    """A present-but-malformed hooks.toml raises HookConfigError (fail-closed)."""
    (tmp_path / "carve").mkdir()
    (tmp_path / "carve" / "hooks.toml").write_text(_MALFORMED_HOOKS_TOML, encoding="utf-8")
    with pytest.raises(HookConfigError):
        build_extensibility_hooks(
            project_dir=tmp_path,
            paths=PathsConfig(),
            mode=PermissionMode.PLAN,
        )


# ----------------------------------------------------- skill-pack lookup seam


def test_build_skill_pack_tool_returns_lookup_tool(tmp_path: Path) -> None:
    tool = build_skill_pack_tool(project_dir=tmp_path, paths=PathsConfig())
    assert isinstance(tool, Tool)
    assert tool.name == "lookup_skill_pack"


# --------------------------------------------------------------- router seam


_USER_AGENT = """\
---
name: fixture-engineer
description: A fixture user agent.
max_mode: build
classifications: [fixture_task]
---
You are the fixture engineer.
"""


def test_router_routes_a_fixture_user_agent(tmp_path: Path) -> None:
    """A user agent matching the classification is selected by the router."""
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "fixture-engineer.md").write_text(_USER_AGENT, encoding="utf-8")

    name = resolve_agent_or_fallback(
        project_dir=tmp_path,
        paths=PathsConfig(),
        classification="fixture_task",
    )
    assert name == "fixture-engineer"


def test_router_falls_back_when_no_agent_matches(tmp_path: Path) -> None:
    """No declarative match → None → the caller's M1 hardcoded path."""
    # builtin/ is empty (until later increments) and there is no user agent,
    # so any classification is a clean no-match → fall back.
    name = resolve_agent_or_fallback(
        project_dir=tmp_path,
        paths=PathsConfig(),
        classification="anything",
    )
    assert name is None


def test_router_no_routing_inputs_falls_back(tmp_path: Path) -> None:
    """No classification and no override → None (fall back), never raise."""
    name = resolve_agent_or_fallback(
        project_dir=tmp_path,
        paths=PathsConfig(),
    )
    assert name is None


def test_router_override_resolves_an_existing_agent(tmp_path: Path) -> None:
    """An explicit override naming a registered agent wins outright."""
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "fixture-engineer.md").write_text(_USER_AGENT, encoding="utf-8")

    name = resolve_agent_or_fallback(
        project_dir=tmp_path,
        paths=PathsConfig(),
        override="fixture-engineer",
    )
    assert name == "fixture-engineer"


def test_router_override_unknown_agent_fails_loud(tmp_path: Path) -> None:
    """An override naming a NONEXISTENT agent fails loud (NoAgentMatch).

    The user asked for an agent by name that is not registered; silently
    falling back to the M1 default would run the wrong agent for an explicit
    request. The classification-miss fallback path must NOT swallow this.
    """
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "fixture-engineer.md").write_text(_USER_AGENT, encoding="utf-8")

    with pytest.raises(NoAgentMatch):
        resolve_agent_or_fallback(
            project_dir=tmp_path,
            paths=PathsConfig(),
            override="does-not-exist",
        )


def test_router_override_unknown_agent_fails_loud_even_with_classification(
    tmp_path: Path,
) -> None:
    """A bad override fails loud even when a classification is also passed.

    `select_agent` checks the override first; an unknown override raises
    before classification is ever considered, so the override error is not
    masked by a classification that *would* have matched.
    """
    agents_dir = tmp_path / "carve" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "fixture-engineer.md").write_text(_USER_AGENT, encoding="utf-8")

    with pytest.raises(NoAgentMatch):
        resolve_agent_or_fallback(
            project_dir=tmp_path,
            paths=PathsConfig(),
            classification="fixture_task",
            override="does-not-exist",
        )


# --------------------------------------------- generate_plan carries the seams


def _config(state_db: str) -> Config:
    return Config(
        project=ProjectConfig(name="wiring-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_db),
        connections=ConnectionsConfig(snowflake={}),
        config_hash="0123456789abcdef",
    )


def _design() -> dict[str, Any]:
    return {
        "pipeline_name": "csv_ingest",
        "description": "Daily CSV ingest.",
        "source": {"type": "http_csv", "url": "https://example.com/data.csv"},
        "destination": {"database": "A", "schema": "R", "table": "T"},
        "requirements": ["requests"],
    }


@pytest.fixture
def repository(tmp_path: Path, postgres_state_store_url: str) -> Repository:
    config = _config(postgres_state_store_url)
    engine = create_engine_from_config(config, project_dir=tmp_path)
    initialize_database(engine)
    return Repository(create_session_factory(engine))


def test_generate_plan_registers_lookup_pack_tool_on_the_loop(
    tmp_path: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A live ``carve plan`` carries lookup_skill_pack into the agent's tools."""
    config = _config(postgres_state_store_url)
    client = _client_returning(
        _response(
            content=[_tool_use_block("submit_plan", _design(), "tu_1")],
            stop_reason="tool_use",
        ),
    )
    generate_plan(
        goal="ingest a CSV",
        config=config,
        project_dir=tmp_path,
        repository=repository,
        client=client,
    )
    # The tool schema the loop sent to the model includes the skill-pack
    # lookup tool — proof the seam reached the constructed agent.
    first_call = client.calls[0]
    tool_names = {tool["name"] for tool in first_call["tools"]}
    assert "lookup_skill_pack" in tool_names


def test_generate_plan_unchanged_when_router_falls_back(
    tmp_path: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """With the seam live but no declarative agent, the M1 plan flow holds."""
    config = _config(postgres_state_store_url)
    client = _client_returning(
        _response(
            content=[_tool_use_block("submit_plan", _design(), "tu_1")],
            stop_reason="tool_use",
        ),
    )
    artifact = generate_plan(
        goal="ingest a CSV",
        config=config,
        project_dir=tmp_path,
        repository=repository,
        client=client,
    )
    assert isinstance(artifact, PlanArtifact)
    assert artifact.pipeline_name == "csv_ingest"
    plan_row = repository.get_plan(artifact.id)
    assert plan_row is not None
    assert plan_row.phase == "drafted"


# ----------------------------- malformed hooks.toml aborts the live run


def test_generate_plan_aborts_on_malformed_hooks_toml(
    tmp_path: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A malformed carve/hooks.toml propagates HookConfigError out of plan.

    The fail-closed boundary the wiring module promises, asserted at the
    *live* edge: `generate_plan` builds the hooks before the loop runs, so a
    bad config aborts the run (the agent never even sees the client).
    """
    config = _config(postgres_state_store_url)
    (tmp_path / "carve").mkdir()
    (tmp_path / "carve" / "hooks.toml").write_text(_MALFORMED_HOOKS_TOML, encoding="utf-8")
    # The client must never be reached — fail-closed happens first.
    client = _client_returning()
    with pytest.raises(HookConfigError):
        generate_plan(
            goal="ingest a CSV",
            config=config,
            project_dir=tmp_path,
            repository=repository,
            client=client,
        )
    client.messages.create.assert_not_called()


def test_build_plan_aborts_on_malformed_hooks_toml(
    tmp_path: Path, repository: Repository, postgres_state_store_url: str
) -> None:
    """A malformed carve/hooks.toml propagates HookConfigError out of build."""
    config = _config(postgres_state_store_url)
    # First produce a clean drafted plan (no hooks file yet).
    plan_client = _client_returning(
        _response(
            content=[_tool_use_block("submit_plan", _design(), "tu_1")],
            stop_reason="tool_use",
        ),
    )
    artifact = generate_plan(
        goal="ingest a CSV",
        config=config,
        project_dir=tmp_path,
        repository=repository,
        client=plan_client,
    )
    # Now drop a malformed hooks.toml and build the saved plan.
    (tmp_path / "carve").mkdir(exist_ok=True)
    (tmp_path / "carve" / "hooks.toml").write_text(_MALFORMED_HOOKS_TOML, encoding="utf-8")
    build_client = _client_returning()
    with pytest.raises(HookConfigError):
        build_plan(
            artifact.id,
            config=config,
            project_dir=tmp_path,
            repository=repository,
            client=build_client,
        )
    build_client.messages.create.assert_not_called()
