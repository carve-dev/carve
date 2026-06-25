"""Integration tests for `cli.orchestrator.delegation_run.run_single_engine`.

A natural-language goal classifies → `select_agent` resolves the engineer →
live `delegate` returns a `DelegationResult` whose cost rolls up via the Unit-1
`roll_up_cost` seam. The child runs SYNC (the harness invariant) and at the
clamped `min(PLAN, capability)` mode. Two flavors:

* a **fake-runner** test (inject a stub `SubagentRunner`) proving the classify →
  route → delegate wiring + the cost rollup, no model needed for the child; and
* a **scripted-client** test driving the real `SubagentRunner` over a creds-free
  in-memory DuckDB substrate end-to-end (no network), proving the dlt-engineer
  route actually runs a bound tool and submits a result.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from carve.cli.orchestrator.cost_rollup import roll_up_cost
from carve.cli.orchestrator.delegation_run import build_registry, run_single_engine
from carve.cli.orchestrator.extra_tools import assemble_extra_tools
from carve.core.agents.delegation import DelegationResult, SubagentRunner
from carve.core.agents.loop import TokenUsage
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
)

# --------------------------------------------------------------------- fixtures


def _config() -> Config:
    return Config(
        project=ProjectConfig(name="route-test"),
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
    """One Anthropic client that serves a queue of responses in order.

    `run_single_engine` first makes the classify call, then the child loop makes
    one-or-more calls — all through this single injected client, so the queue is
    [classify_response, *child_loop_responses].
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return next(self._responses)


# ----------------------------------------------------- fake-runner route + rollup


def test_goal_routes_to_dlt_engineer_and_rolls_up(tmp_path: Path) -> None:
    """A dlt-shaped goal → dlt-engineer → DelegationResult whose cost rolls up."""
    config = _config()
    captured: dict[str, Any] = {}

    fake_result = DelegationResult(
        status="succeeded",
        result_summary="authored el/stripe",
        files_changed=["el/stripe/__init__.py"],
        outputs={"pipeline_name": "stripe", "first_run_seconds": 120},
        usage=TokenUsage(input_tokens=5000, output_tokens=800),
        cost_usd=0.075,
    )

    class _FakeRunner:
        def run(
            self,
            agent: str,
            task: str,
            context: dict[str, Any],
            *,
            parent_mode: PermissionMode,
            depth: int = 1,
        ) -> DelegationResult:
            captured.update(agent=agent, task=task, context=context, parent_mode=parent_mode)
            return fake_result

    # The classify call resolves to a dlt label; the fake runner short-circuits
    # the child loop, so only the classify response is consumed.
    client = _SequencedClient([_classify_response("new_pipeline")])

    result = run_single_engine(
        "ingest the Stripe API into the warehouse",
        config=config,
        project_dir=tmp_path,
        client=client,
        model="claude-opus-4-8",
        runner=_FakeRunner(),  # type: ignore[arg-type]
    )

    # Routed to the dlt-engineer at the clamped plan mode, sync.
    assert captured["agent"] == "dlt-engineer"
    assert captured["parent_mode"] == PermissionMode.PLAN
    assert captured["context"]["classification"] == "new_pipeline"
    # The context bundle signals DESIGN capacity (parent_mode=PLAN): the engineer
    # proposes a design instead of authoring code.
    assert captured["context"]["capacity"] == "design"
    assert result is fake_result

    # The live DelegationResult rolls up through the Unit-1 seam (non-zero).
    rollup = roll_up_cost([result])
    assert rollup.cost_usd == 0.075
    assert rollup.usage.input_tokens == 5000
    assert rollup.usage.output_tokens == 800
    # The duration hint composes the runtime estimate.
    assert rollup.runtime.first_run_seconds == 120


def test_pipeline_goal_routes_to_pipeline_engineer(tmp_path: Path) -> None:
    """A pipeline-shaped classification resolves the pipeline-engineer."""
    config = _config()
    seen: dict[str, str] = {}

    class _FakeRunner:
        def run(self, agent: str, *a: Any, **k: Any) -> DelegationResult:
            seen["agent"] = agent
            return DelegationResult(
                status="succeeded",
                result_summary="",
                files_changed=[],
                outputs={},
                usage=TokenUsage(),
                cost_usd=0.0,
            )

    client = _SequencedClient([_classify_response("compose_pipeline")])
    run_single_engine(
        "compose the daily ELT pipeline",
        config=config,
        project_dir=tmp_path,
        client=client,
        model="m",
        runner=_FakeRunner(),  # type: ignore[arg-type]
    )
    assert seen["agent"] == "pipeline-engineer"


# ----------------------------------------------------- live creds-free run


class _ToolResultCapture:
    """An :class:`AgentObserver` that records each tool's (ok, summary).

    The loop fires ``on_tool_result(name, ok, ...)`` after every tool/gate
    decision: ``ok=False`` for a gate-denied (``is_error``) call, ``ok=True``
    for a real executor return. Capturing it is how this test proves the design
    tool actually EXECUTED rather than being silently gate-denied — the masking
    the old assertion (only ``submit_result`` outputs) let through.
    """

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def on_turn_start(self, turn: int) -> None:
        return None

    def on_tool_call(self, name: str, input: dict[str, Any]) -> None:
        return None

    def on_tool_result(self, name: str, ok: bool, summary: str, duration_ms: int) -> None:
        self.results.append((name, ok, summary))

    def on_turn_complete(self, turn: int, input_tokens: int, output_tokens: int) -> None:
        return None

    def on_done(
        self,
        total_turns: int,
        total_tool_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        return None


def test_live_dlt_route_runs_a_bound_tool_over_duckdb(tmp_path: Path) -> None:
    """End-to-end (no network): the engineer's design read skill actually RUNS.

    The scripted client drives the classify call, then the dlt-engineer child
    loop: one `existing_dlt_inspect` call (a bound, creds-free read skill), then
    `submit_result`. A capturing observer records the tool_result so the test
    ASSERTS the read skill executed (`ok=True`, real inspect output) rather than
    being gate-denied. This is the floor-bug net: before the read-floor fix the
    skill was denied at every mode (`ok=False`, is_error), yet `submit_result`
    fired regardless, so the old outputs-only assertion stayed green while the
    tool was blocked. This test FAILS against the pre-fix floor and PASSES after.
    """
    config = _config()
    registry = build_registry(tmp_path, config)

    # Give the inspect tool real content to return: a Carve-generated el/ pipeline
    # so op=list yields a non-empty `pipelines` list (proves real read output, not
    # an empty fallback).
    pipeline_dir = tmp_path / "el" / "stripe"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "__init__.py").write_text("# carve-generated\nimport dlt\n", encoding="utf-8")

    def _child_tool_call(name: str, input_: dict[str, Any], tool_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)],
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=100,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )

    client = _SequencedClient(
        [
            _classify_response("new_pipeline"),
            _child_tool_call("existing_dlt_inspect", {"op": "list"}, "t1"),
            _child_tool_call(
                "submit_result",
                {
                    "status": "succeeded",
                    "summary": "inspected el/",
                    "outputs": {"pipeline_name": "stripe"},
                },
                "t2",
            ),
        ]
    )

    # Build the real runner with the assembled extra_tools so the engineer's
    # grant binds to executors over the creds-free DuckDB substrate. The
    # capturing observer records each tool_result for the execution assertion.
    capture = _ToolResultCapture()
    runner = SubagentRunner(
        registry=registry,
        paths=ProjectPaths.from_root(tmp_path),
        client=client,
        model="claude-opus-4-8",
        model_tiers=config.models.tiers,
        observer=capture,
        extra_tools=assemble_extra_tools(
            config_components={},
            project_dir=tmp_path,
            child_mode=PermissionMode.PLAN,
        ),
    )

    result = run_single_engine(
        "ingest the Stripe API into the warehouse",
        config=config,
        project_dir=tmp_path,
        client=client,
        model="claude-opus-4-8",
        registry=registry,
        runner=runner,
    )

    # The design read skill actually EXECUTED — not gate-denied. (This is the
    # assertion that fails against the pre-fix floor: there the loop fired
    # `on_tool_result(ok=False, summary="deny: ...")` because the gate denied the
    # tool at every mode, yet `submit_result` still ran, masking the block.)
    inspect_results = [r for r in capture.results if r[0] == "existing_dlt_inspect"]
    assert inspect_results, "existing_dlt_inspect was never called"
    _name, ok, summary = inspect_results[0]
    assert ok, f"existing_dlt_inspect was gate-denied (not executed): {summary}"
    # The success summary is not a permission-denial string (the gate-blocked
    # path sets `summary='deny: ...'`); a real executor return summarizes "ok".
    assert not summary.startswith("deny:"), f"gate-denied: {summary!r}"

    # And the bound tool carries REAL inspect output: re-run the same assembled
    # executor and confirm it reports the el/stripe component we seeded — proving
    # the loop's `existing_dlt_inspect` call returned real data, not an error.
    inspect_tool = assemble_extra_tools(
        config_components={},
        project_dir=tmp_path,
        child_mode=PermissionMode.PLAN,
    )["existing_dlt_inspect"]
    listing = inspect_tool.executor({"op": "list"})
    assert isinstance(listing, dict)
    assert any(p.get("name") == "stripe" for p in listing.get("pipelines", [])), (
        f"expected the seeded el/stripe component in inspect output: {listing!r}"
    )

    assert result.status == "succeeded"
    assert result.outputs == {"pipeline_name": "stripe"}
    # Cost rolled up from the child loop's real usage (non-zero).
    rollup = roll_up_cost([result])
    assert rollup.usage.input_tokens > 0
