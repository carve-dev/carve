"""Tests for the orchestrator-owned review fan-out driver.

The driver lands as a dormant seam, so these tests drive it with a
**stub** ``delegate_fn`` (never a live ``SubagentRunner``/LLM). They
verify the driver: (1) sequences dlt-qa before dlt-security, each on a
context of only ``{diff, goal}``; (2) aggregates both reviewers' findings
and computes ``passed`` from severity; (3) fails loud on a malformed
payload; and (4) surfaces an injected credential-leak / risky-write-
disposition problem as a ``passed=False`` finding.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from carve.core.agents.delegation import DelegationResult
from carve.core.agents.loop import TokenUsage
from carve.core.agents.review_fan_out import (
    DBT_QA_REVIEWER,
    DBT_REVIEWER_SEQUENCE,
    QA_REVIEWER,
    SECURITY_REVIEWER,
    Finding,
    ReviewFanOutError,
    ReviewResult,
    Severity,
    review_fan_out,
)


def _result(
    *,
    status: str = "succeeded",
    outputs: dict[str, Any] | None = None,
) -> DelegationResult:
    """A canned DelegationResult with a real (zero-cost) TokenUsage."""
    return DelegationResult(
        status=status,
        result_summary="stub review",
        files_changed=[],
        outputs=outputs if outputs is not None else {"findings": []},
        usage=TokenUsage(),
        cost_usd=0.0,
    )


class _RecordingStub:
    """Records call order + per-call context, returns canned verdicts."""

    def __init__(self, verdicts: dict[str, DelegationResult]) -> None:
        self._verdicts = verdicts
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, agent: str, task: str, context: dict[str, Any]) -> DelegationResult:
        self.calls.append((agent, task, context))
        return self._verdicts[agent]


def test_reviewers_run_qa_then_security_sequentially() -> None:
    stub = _RecordingStub(
        {QA_REVIEWER: _result(), SECURITY_REVIEWER: _result()},
    )

    review_fan_out(diff="--- a/x\n+++ b/x", goal="ingest hn", delegate_fn=stub)

    invoked = [agent for agent, _task, _ctx in stub.calls]
    assert invoked == [QA_REVIEWER, SECURITY_REVIEWER]


def test_each_reviewer_sees_only_diff_and_goal_context() -> None:
    stub = _RecordingStub(
        {QA_REVIEWER: _result(), SECURITY_REVIEWER: _result()},
    )

    review_fan_out(diff="THE-DIFF", goal="THE-GOAL", delegate_fn=stub)

    for _agent, _task, ctx in stub.calls:
        # No engineer transcript leaks in — exactly the two stable keys.
        assert set(ctx) == {"diff", "goal"}
        assert ctx["diff"] == "THE-DIFF"
        assert ctx["goal"] == "THE-GOAL"


def test_aggregation_collects_both_reviewers_findings() -> None:
    stub = _RecordingStub(
        {
            QA_REVIEWER: _result(
                outputs={
                    "findings": [
                        {
                            "severity": "minor",
                            "file": "pipeline.py",
                            "line": 12,
                            "message": "missing provenance header",
                            "suggested_change": None,
                        }
                    ]
                }
            ),
            SECURITY_REVIEWER: _result(
                outputs={
                    "findings": [
                        {
                            "severity": "info",
                            "file": ".dlt/secrets.toml.template",
                            "line": None,
                            "message": "uses ${ENV} placeholder — ok",
                        }
                    ]
                }
            ),
        },
    )

    result = review_fan_out(diff="d", goal="g", delegate_fn=stub)

    assert isinstance(result, ReviewResult)
    assert len(result.findings) == 2
    assert set(result.by_reviewer) == {QA_REVIEWER, SECURITY_REVIEWER}
    assert len(result.by_reviewer[QA_REVIEWER]) == 1
    assert result.by_reviewer[QA_REVIEWER][0].reviewer == QA_REVIEWER
    assert result.by_reviewer[SECURITY_REVIEWER][0].reviewer == SECURITY_REVIEWER
    # No blocker/major → passes.
    assert result.passed is True
    # Raw DelegationResults are preserved for both reviewers.
    assert set(result.raw) == {QA_REVIEWER, SECURITY_REVIEWER}


def test_passed_is_false_when_a_major_finding_present() -> None:
    stub = _RecordingStub(
        {
            QA_REVIEWER: _result(
                outputs={
                    "findings": [
                        {
                            "severity": "major",
                            "file": "pipeline.py",
                            "message": "incremental cursor never advances",
                        }
                    ]
                }
            ),
            SECURITY_REVIEWER: _result(),
        },
    )

    result = review_fan_out(diff="d", goal="g", delegate_fn=stub)

    assert result.passed is False


def test_clean_review_passes() -> None:
    stub = _RecordingStub(
        {QA_REVIEWER: _result(), SECURITY_REVIEWER: _result()},
    )

    result = review_fan_out(diff="d", goal="g", delegate_fn=stub)

    assert result.findings == []
    assert result.passed is True


def test_malformed_payload_missing_findings_key_raises() -> None:
    stub = _RecordingStub(
        {
            QA_REVIEWER: _result(outputs={"notes": "oops, wrong schema"}),
            SECURITY_REVIEWER: _result(),
        },
    )

    with pytest.raises(ReviewFanOutError, match="missing the required 'findings'"):
        review_fan_out(diff="d", goal="g", delegate_fn=stub)


def test_malformed_finding_object_raises_not_silently_dropped() -> None:
    stub = _RecordingStub(
        {
            QA_REVIEWER: _result(
                outputs={
                    "findings": [
                        # severity is not a valid Severity member.
                        {"severity": "catastrophic", "file": "x.py", "message": "?"}
                    ]
                }
            ),
            SECURITY_REVIEWER: _result(),
        },
    )

    with pytest.raises(ReviewFanOutError, match="malformed"):
        review_fan_out(diff="d", goal="g", delegate_fn=stub)


def test_reviewer_needs_user_input_is_surfaced() -> None:
    stub = _RecordingStub(
        {
            QA_REVIEWER: _result(status="needs_user_input", outputs={}),
            SECURITY_REVIEWER: _result(),
        },
    )

    with pytest.raises(ReviewFanOutError, match="needs_user_input"):
        review_fan_out(diff="d", goal="g", delegate_fn=stub)


def test_credential_leak_in_secrets_template_surfaces_as_failed() -> None:
    """Integration slice (a): a secret literal in .dlt/secrets.toml.template
    in the diff → dlt-security returns a credential-leak finding → the driver
    surfaces passed=False."""
    diff = (
        "--- a/.dlt/secrets.toml.template\n"
        "+++ b/.dlt/secrets.toml.template\n"
        '+api_key = "sk-live-abc123realsecret"\n'
    )
    stub = _RecordingStub(
        {
            QA_REVIEWER: _result(),
            SECURITY_REVIEWER: _result(
                outputs={
                    "findings": [
                        {
                            "severity": "blocker",
                            "file": ".dlt/secrets.toml.template",
                            "line": 3,
                            "message": "live credential literal committed to template",
                            "suggested_change": 'api_key = "${API_KEY}"',
                        }
                    ]
                }
            ),
        },
    )

    result = review_fan_out(diff=diff, goal="ingest api", delegate_fn=stub)

    assert result.passed is False
    leak = result.by_reviewer[SECURITY_REVIEWER][0]
    assert leak.severity is Severity.BLOCKER
    assert leak.reviewer == SECURITY_REVIEWER
    assert leak.file == ".dlt/secrets.toml.template"


def test_risky_replace_write_disposition_surfaces_as_failed() -> None:
    """Integration slice (b): a `replace` write-disposition that should be
    `merge` → flagged by the reviewer that owns write-disposition sanity (qa
    per spec line 86) → the driver surfaces passed=False."""
    diff = '--- a/pipeline.py\n+++ b/pipeline.py\n+    write_disposition="replace"\n'
    stub = _RecordingStub(
        {
            QA_REVIEWER: _result(
                outputs={
                    "findings": [
                        {
                            "severity": "major",
                            "file": "pipeline.py",
                            "line": 3,
                            "message": (
                                "write_disposition=replace on an incremental table "
                                "drops history; should be merge"
                            ),
                            "suggested_change": 'write_disposition="merge"',
                        }
                    ]
                }
            ),
            SECURITY_REVIEWER: _result(),
        },
    )

    result = review_fan_out(diff=diff, goal="ingest orders", delegate_fn=stub)

    assert result.passed is False
    flag = result.by_reviewer[QA_REVIEWER][0]
    assert flag.severity is Severity.MAJOR
    assert "merge" in (flag.suggested_change or "")


def test_finding_model_is_frozen() -> None:
    f = Finding(reviewer=QA_REVIEWER, severity=Severity.MINOR, file="x.py", message="m")
    with pytest.raises(ValidationError):
        f.severity = Severity.BLOCKER


# ---------------------------------------------------------------------------
# dbt-qa single-reviewer fan-out (the `reviewers=` generalization).
# ---------------------------------------------------------------------------


def test_default_sequence_is_unchanged_dlt_pair() -> None:
    # Regression guard on the default parameter: with no `reviewers=` the driver
    # runs exactly the dlt pair, in order.
    stub = _RecordingStub({QA_REVIEWER: _result(), SECURITY_REVIEWER: _result()})

    review_fan_out(diff="d", goal="g", delegate_fn=stub)

    assert [agent for agent, _task, _ctx in stub.calls] == [QA_REVIEWER, SECURITY_REVIEWER]


def test_dbt_reviewer_sequence_runs_only_dbt_qa() -> None:
    stub = _RecordingStub({DBT_QA_REVIEWER: _result()})

    review_fan_out(diff="d", goal="g", delegate_fn=stub, reviewers=DBT_REVIEWER_SEQUENCE)

    assert [agent for agent, _task, _ctx in stub.calls] == [DBT_QA_REVIEWER]


def test_dbt_qa_coverage_finding_aggregates_and_stamps_reviewer() -> None:
    """An authored model with no tests -> dbt-qa flags a coverage gap; the driver
    aggregates it, stamps reviewer='dbt-qa', and sets passed per severity."""
    diff = (
        "--- /dev/null\n"
        "+++ b/models/marts/daily_revenue.sql\n"
        "+select order_date, sum(amount) as revenue from {{ ref('stg_orders') }} group by 1\n"
    )
    stub = _RecordingStub(
        {
            DBT_QA_REVIEWER: _result(
                outputs={
                    "findings": [
                        {
                            "severity": "major",
                            "file": "models/marts/daily_revenue.sql",
                            "line": 1,
                            "message": "new mart has no tests; add not_null/unique on grain key",
                            "suggested_change": None,
                        }
                    ]
                }
            )
        },
    )

    result = review_fan_out(
        diff=diff, goal="add a daily revenue mart", delegate_fn=stub, reviewers=("dbt-qa",)
    )

    assert isinstance(result, ReviewResult)
    assert set(result.by_reviewer) == {DBT_QA_REVIEWER}
    finding = result.by_reviewer[DBT_QA_REVIEWER][0]
    assert finding.reviewer == DBT_QA_REVIEWER
    assert finding.severity is Severity.MAJOR
    # A major finding fails the review.
    assert result.passed is False
    assert set(result.raw) == {DBT_QA_REVIEWER}


def test_dbt_qa_convention_finding_below_threshold_passes() -> None:
    # A naming-convention nit at `minor` severity does not fail the review.
    stub = _RecordingStub(
        {
            DBT_QA_REVIEWER: _result(
                outputs={
                    "findings": [
                        {
                            "severity": "minor",
                            "file": "models/orders.sql",
                            "message": "model 'orders' should be prefixed 'stg_' per convention",
                        }
                    ]
                }
            )
        },
    )

    result = review_fan_out(diff="d", goal="g", delegate_fn=stub, reviewers=DBT_REVIEWER_SEQUENCE)

    assert result.passed is True
    assert result.by_reviewer[DBT_QA_REVIEWER][0].reviewer == DBT_QA_REVIEWER
