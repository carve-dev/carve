"""Unit tests for `cli.orchestrator.review_wiring` — the live review-fan-out glue.

`review_wiring` fills the dormant `review_fan_out` seam: it selects the reviewer
sequence from WHICH engines authored (registry-driven, unioned + de-duped),
renders the authored diff from the engineers' `files_changed`, and delegates each
reviewer at READ_ONLY on a `{diff, goal}`-only context. These tests drive it with
a **stub** `SubagentRunner` (never a live LLM): the registry is the real built-in
one (offline — no network), so the classification→engine→reviewer routing is
exercised against the agents on disk.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from carve.cli.orchestrator.delegation_run import build_registry
from carve.cli.orchestrator.review_wiring import (
    render_authored_diff,
    run_review_fan_out,
    select_reviewer_sequence,
)
from carve.core.agents.delegation import DelegationResult
from carve.core.agents.loop import TokenUsage
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.subagent_registry import SubagentRegistry
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
)

# --------------------------------------------------------------------- fixtures


def _config() -> Config:
    return Config(
        project=ProjectConfig(name="review-wiring-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        connections=ConnectionsConfig(),
        config_hash="deadbeef",
    )


def _registry(project_dir: Path) -> SubagentRegistry:
    return build_registry(project_dir, _config())


def _clean_result() -> DelegationResult:
    return DelegationResult(
        status="succeeded",
        result_summary="reviewed",
        files_changed=[],
        outputs={"findings": []},
        usage=TokenUsage(),
        cost_usd=0.0,
    )


class _RecordingRunner:
    """A fake `SubagentRunner`: records each `run` call, returns a clean verdict."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        return _clean_result()


# ----------------------------------------------------- reviewer-sequence selection


def test_dlt_only_selects_qa_then_security(tmp_path: Path) -> None:
    """A build authored only by the dlt-engineer → (dlt-qa, dlt-security), in order."""
    registry = _registry(tmp_path)
    seq = select_reviewer_sequence(["new_pipeline"], registry=registry)
    assert seq == ["dlt-qa", "dlt-security"]


def test_dbt_only_selects_dbt_qa(tmp_path: Path) -> None:
    """A build authored only by the dbt-engineer → (dbt-qa,) only."""
    registry = _registry(tmp_path)
    seq = select_reviewer_sequence(["new_model"], registry=registry)
    assert seq == ["dbt-qa"]


def test_mixed_dlt_dbt_unions_dedupes_order_stable(tmp_path: Path) -> None:
    """dlt then dbt → the de-duped union in authoring order: qa, security, dbt-qa."""
    registry = _registry(tmp_path)
    seq = select_reviewer_sequence(["new_pipeline", "new_model"], registry=registry)
    assert seq == ["dlt-qa", "dlt-security", "dbt-qa"]


def test_repeated_dlt_classifications_do_not_duplicate_reviewers(tmp_path: Path) -> None:
    """Two dlt sub-goals still run each dlt reviewer exactly once (de-dup)."""
    registry = _registry(tmp_path)
    seq = select_reviewer_sequence(["new_pipeline", "add_resource_to_pipeline"], registry=registry)
    assert seq == ["dlt-qa", "dlt-security"]


def test_dbt_before_dlt_preserves_authoring_order(tmp_path: Path) -> None:
    """Order is stable by FIRST appearance: dbt authored first → dbt-qa leads."""
    registry = _registry(tmp_path)
    seq = select_reviewer_sequence(["new_model", "new_pipeline"], registry=registry)
    assert seq == ["dbt-qa", "dlt-qa", "dlt-security"]


# ----------------------------------------------------------- authored-diff render


def test_render_diff_greenfield_file_diffs_from_empty(tmp_path: Path) -> None:
    """A newly-authored file (no pre-build content) renders as an add-from-empty diff."""
    (tmp_path / "el" / "stripe").mkdir(parents=True)
    authored = tmp_path / "el" / "stripe" / "main.py"
    authored.write_text("import dlt\nprint('hi')\n", encoding="utf-8")

    diff = render_authored_diff(["el/stripe/main.py"], project_dir=tmp_path, pre_build={})

    assert "b/el/stripe/main.py" in diff
    assert "+import dlt" in diff
    assert "+print('hi')" in diff


def test_render_diff_modified_file_shows_delta(tmp_path: Path) -> None:
    """A modified file diffs against its captured pre-build content."""
    (tmp_path / "el" / "stripe").mkdir(parents=True)
    authored = tmp_path / "el" / "stripe" / "main.py"
    authored.write_text("import dlt\nx = 2\n", encoding="utf-8")
    pre_build = {authored.resolve(): "import dlt\nx = 1\n"}

    diff = render_authored_diff(["el/stripe/main.py"], project_dir=tmp_path, pre_build=pre_build)

    assert "-x = 1" in diff
    assert "+x = 2" in diff


def test_render_diff_binary_file_emits_stub_not_crash(tmp_path: Path) -> None:
    """A non-UTF-8 authored file (e.g. a dbt seed) renders as a stub, never crashes.

    `read_text(encoding="utf-8")` would raise `UnicodeDecodeError` on a binary
    file; the gate must degrade to a git-style "Binary files differ" stub so a
    seed/fixture in the authored set can't crash the review.
    """
    (tmp_path / "el" / "stripe").mkdir(parents=True)
    seed = tmp_path / "el" / "stripe" / "data.bin"
    seed.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01")  # invalid UTF-8

    diff = render_authored_diff(["el/stripe/data.bin"], project_dir=tmp_path, pre_build={})

    assert "Binary files a/el/stripe/data.bin and b/el/stripe/data.bin differ" in diff


# ----------------------------------------------------- the live delegate at READ_ONLY


def test_reviewers_delegated_at_read_only_with_diff_goal_only(tmp_path: Path) -> None:
    """Each reviewer delegates at READ_ONLY, sees only `{diff, goal}` context."""
    (tmp_path / "el" / "stripe").mkdir(parents=True)
    (tmp_path / "el" / "stripe" / "main.py").write_text("import dlt\n", encoding="utf-8")
    registry = _registry(tmp_path)
    runner = _RecordingRunner()

    result = run_review_fan_out(
        classifications=["new_pipeline"],
        files_changed=["el/stripe/main.py"],
        goal="ingest the Stripe API",
        config=_config(),
        project_dir=tmp_path,
        client=object(),  # never used — the stub runner short-circuits
        model="claude-opus-4-8",
        registry=registry,
        runner=runner,  # type: ignore[arg-type]
    )

    # The dlt pair ran, in order.
    assert [c["agent"] for c in runner.calls] == ["dlt-qa", "dlt-security"]
    # Every reviewer delegated at READ_ONLY — never wider; a reviewer never authors.
    assert all(c["parent_mode"] == PermissionMode.READ_ONLY for c in runner.calls)
    # Context is EXACTLY {diff, goal} — no engineer transcript, nothing privileged.
    for call in runner.calls:
        assert set(call["context"]) == {"diff", "goal"}
        assert call["context"]["goal"] == "ingest the Stripe API"
        assert "import dlt" in call["context"]["diff"]
    # A clean review passes.
    assert result.passed is True


def test_mixed_build_runs_three_reviewers_at_read_only(tmp_path: Path) -> None:
    """A dlt+dbt build delegates dlt-qa, dlt-security, dbt-qa — all at READ_ONLY."""
    (tmp_path / "el" / "stripe").mkdir(parents=True)
    (tmp_path / "el" / "stripe" / "main.py").write_text("import dlt\n", encoding="utf-8")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "stg_stripe.sql").write_text("select 1\n", encoding="utf-8")
    registry = _registry(tmp_path)
    runner = _RecordingRunner()

    run_review_fan_out(
        classifications=["new_pipeline", "new_model"],
        files_changed=["el/stripe/main.py", "models/stg_stripe.sql"],
        goal="ingest then stage",
        config=_config(),
        project_dir=tmp_path,
        client=object(),
        model="m",
        registry=registry,
        runner=runner,  # type: ignore[arg-type]
    )

    assert [c["agent"] for c in runner.calls] == ["dlt-qa", "dlt-security", "dbt-qa"]
    assert all(c["parent_mode"] == PermissionMode.READ_ONLY for c in runner.calls)


def test_no_reviewer_family_runs_no_reviewers_and_warns(tmp_path: Path) -> None:
    """An authoring engine with no reviewer family (pipeline) ships ungated, with a warning.

    `compose_pipeline` routes to the pipeline-engineer, which has no review
    fan-out today, so the reviewer sequence is empty: the per-engine skip is
    spec-sanctioned, but an entirely-empty sequence over a real authored diff
    must surface a warning rather than silently pass an unreviewed build.

    The warning is captured by a handler attached directly to the module logger
    (not `caplog`) so the assertion is immune to cross-test log-propagation state.
    """
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "p.toml").write_text("steps = []\n", encoding="utf-8")
    registry = _registry(tmp_path)
    runner = _RecordingRunner()

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    wlogger = logging.getLogger("carve.cli.orchestrator.review_wiring")
    handler = _Capture()
    wlogger.addHandler(handler)
    prior_level = wlogger.level
    prior_disabled = wlogger.disabled
    wlogger.setLevel(logging.WARNING)
    # Another test in this session may have run `logging.config` with the default
    # `disable_existing_loggers=True`, leaving this logger `.disabled` (pytest does
    # not reset that between tests). Clear it so the capture is deterministic.
    wlogger.disabled = False
    try:
        result = run_review_fan_out(
            classifications=["compose_pipeline"],
            files_changed=["pipelines/p.toml"],
            goal="compose a pipeline",
            config=_config(),
            project_dir=tmp_path,
            client=object(),
            model="m",
            registry=registry,
            runner=runner,  # type: ignore[arg-type]
        )
    finally:
        wlogger.removeHandler(handler)
        wlogger.setLevel(prior_level)
        wlogger.disabled = prior_disabled

    # No reviewer ran (the pipeline-engineer has no reviewer family).
    assert runner.calls == []
    # The build is ungated (spec-sanctioned per-engine) but the gap is surfaced.
    assert result.passed is True
    assert any("ships ungated" in r.getMessage() for r in records)
