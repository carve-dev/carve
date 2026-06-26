"""Integration tests for `cli.orchestrator.delegation_run.run_engines`.

`run_engines` takes an already-decomposed ordered list of `SubGoal`s and, for
each, `select_agent` resolves the engineer and a SYNC `delegate` at
`parent_mode=PLAN` (design capacity) returns one `DelegationResult`. The N
results come back **in sub-goal order**, each routed to the right agent, with
the `SubagentRunner` built **once** and threaded across all sub-goals (the
harness invariant — sequential, blocking). The N=1 case mirrors
`run_single_engine`'s per-engine behavior.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from carve.cli.orchestrator.delegation_run import run_engines, run_single_engine
from carve.cli.orchestrator.goal_decomposer import SubGoal
from carve.core.agents.delegation import DelegationResult
from carve.core.agents.loop import TokenUsage
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
)

# --------------------------------------------------------------------- fixtures


def _config() -> Config:
    return Config(
        project=ProjectConfig(name="run-engines-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        connections=ConnectionsConfig(),
        config_hash="deadbeef",
    )


def _classify_response(label: str) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", id="c1", name="classify_goal", input={"label": label})
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=2,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class _SequencedClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return next(self._responses)


class _RecordingRunner:
    """A fake `SubagentRunner`: records each `run` call and returns a canned result.

    One instance is threaded across all sub-goals, so the recorded `agent`
    sequence proves both the per-sub-goal routing AND that the runner is built
    once (the fixture constructs exactly one).
    """

    def __init__(self, results_by_agent: dict[str, DelegationResult]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._results_by_agent = results_by_agent

    def run(
        self,
        agent: str,
        task: str,
        context: dict[str, Any],
        *,
        parent_mode: PermissionMode,
        depth: int = 1,
    ) -> DelegationResult:
        self.calls.append(
            {"agent": agent, "task": task, "context": context, "parent_mode": parent_mode}
        )
        return self._results_by_agent[agent]


def _result(summary: str, *, cost: float = 0.0) -> DelegationResult:
    return DelegationResult(
        status="succeeded",
        result_summary=summary,
        files_changed=[],
        outputs={},
        usage=TokenUsage(),
        cost_usd=cost,
    )


# --------------------------------------------------------------- multi-engine run


def test_run_engines_routes_each_sub_goal_in_order(tmp_path: Path) -> None:
    """N sub-goals → N DelegationResults in order, each routed to the right agent."""
    config = _config()
    runner = _RecordingRunner(
        {
            "dlt-engineer": _result("designed stripe", cost=0.05),
            "dbt-engineer": _result("designed staging", cost=0.03),
        }
    )
    sub_goals = [
        SubGoal(sub_goal="ingest the Stripe API", classification="new_pipeline"),
        SubGoal(sub_goal="stage it with dbt", classification="new_model"),
    ]

    # No model call is needed: the sub-goals are already decomposed, and the
    # fake runner short-circuits each child loop.
    client = _SequencedClient([])

    results = run_engines(
        sub_goals,
        config=config,
        project_dir=tmp_path,
        client=client,
        model="claude-opus-4-8",
        runner=runner,  # type: ignore[arg-type]
    )

    # N results, IN ORDER, one per sub-goal.
    assert [r.result_summary for r in results] == ["designed stripe", "designed staging"]

    # Routed to the right engineer per sub-goal, in order — sequential (the
    # harness invariant): dlt first, dbt second.
    assert [c["agent"] for c in runner.calls] == ["dlt-engineer", "dbt-engineer"]

    # Each child ran at PLAN in DESIGN capacity, with its own goal slice.
    for call, sub_goal in zip(runner.calls, sub_goals, strict=True):
        assert call["parent_mode"] == PermissionMode.PLAN
        assert call["context"]["capacity"] == "design"
        assert call["context"]["classification"] == sub_goal.classification
        assert call["context"]["goal_slice"] == sub_goal.sub_goal
        assert call["task"] == sub_goal.sub_goal


def test_run_engines_empty_list_returns_empty(tmp_path: Path) -> None:
    """An empty decomposition yields no results (nothing routed)."""
    config = _config()
    runner = _RecordingRunner({})
    client = _SequencedClient([])

    results = run_engines(
        [],
        config=config,
        project_dir=tmp_path,
        client=client,
        model="m",
        runner=runner,  # type: ignore[arg-type]
    )

    assert results == []
    assert runner.calls == []


# --------------------------------------------------------------- N=1 equivalence


def test_single_element_run_engines_matches_run_single_engine(tmp_path: Path) -> None:
    """A 1-element `run_engines` routes exactly like `run_single_engine`.

    `run_single_engine` classifies the goal then delegates through the same
    per-engine helper `run_engines` uses; given an equivalent `SubGoal`, the two
    route to the same agent at the same mode/capacity with the same context.
    """
    config = _config()
    fake_result = _result("designed stripe", cost=0.075)

    # run_single_engine: needs a classify call (it classifies first), then the
    # fake runner short-circuits the child loop.
    single_runner = _RecordingRunner({"dlt-engineer": fake_result})
    single_client = _SequencedClient([_classify_response("new_pipeline")])
    single = run_single_engine(
        "ingest the Stripe API into the warehouse",
        config=config,
        project_dir=tmp_path,
        client=single_client,
        model="m",
        runner=single_runner,  # type: ignore[arg-type]
    )

    # run_engines over the equivalent 1-element decomposition: no model call.
    engines_runner = _RecordingRunner({"dlt-engineer": fake_result})
    engines_client = _SequencedClient([])
    engines = run_engines(
        [
            SubGoal(
                sub_goal="ingest the Stripe API into the warehouse", classification="new_pipeline"
            )
        ],
        config=config,
        project_dir=tmp_path,
        client=engines_client,
        model="m",
        runner=engines_runner,  # type: ignore[arg-type]
    )

    assert single is fake_result
    assert engines == [fake_result]
    # Both routed to the dlt-engineer at PLAN in design capacity, same context.
    assert single_runner.calls[0]["agent"] == engines_runner.calls[0]["agent"] == "dlt-engineer"
    assert single_runner.calls[0]["context"] == engines_runner.calls[0]["context"]
    assert (
        single_runner.calls[0]["parent_mode"]
        == engines_runner.calls[0]["parent_mode"]
        == PermissionMode.PLAN
    )
