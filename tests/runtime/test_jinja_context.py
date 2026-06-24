"""Tests for the sandboxed cross-step Jinja context.

Covers the *pipelines* spec's Unit (Jinja sandbox) bar: a template renders
against the standard ``{steps, run, env}`` namespace; cross-step output
threading (``{{ steps.X.outputs.rows_loaded }}``) resolves; and attempts to
reach the filesystem or import ``os`` raise sandbox errors rather than
escaping the namespace.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from carve.runtime.jinja_context import (
    JinjaRenderError,
    make_jinja_context,
    render_step_vars,
)
from carve.runtime.run_context import PipelineRun
from carve.runtime.step_executor import StepResult


def _run() -> PipelineRun:
    return PipelineRun(
        pipeline="stripe",
        target="prod",
        trigger="scheduled",
        id="run-123",
        started_at=datetime(2026, 6, 23, 2, 0, tzinfo=UTC),
    )


def _context(step_results: dict[str, StepResult] | None = None) -> dict[str, object]:
    # An explicit ``env`` map is the executor-supplied path (a caller may
    # inject any non-secret dict); it is independent of the os.environ
    # allow-list, which is empty by design (see _EXPOSED_ENV_KEYS).
    return make_jinja_context(
        run=_run(),
        step_results=step_results or {},
        env={"region": "us-east-1"},
    )


# ---------------------------------------------------------------------------
# Namespace rendering
# ---------------------------------------------------------------------------


def test_renders_run_namespace() -> None:
    ctx = _context()
    out = render_step_vars(
        step_id="s",
        jinja_vars={"who": "{{ run.pipeline }}@{{ run.target }}"},
        context=ctx,
    )
    assert out == {"who": "stripe@prod"}


def test_renders_env_namespace() -> None:
    ctx = _context()
    out = render_step_vars(
        step_id="s",
        jinja_vars={"region": "{{ env.region }}"},
        context=ctx,
    )
    assert out == {"region": "us-east-1"}


def test_cross_step_output_threading() -> None:
    results = {"ingest_stripe": StepResult(status="succeeded", outputs={"rows_loaded": 4200})}
    ctx = _context(results)
    out = render_step_vars(
        step_id="notify_count",
        jinja_vars={"loaded_rows": "{{ steps.ingest_stripe.outputs.rows_loaded }}"},
        context=ctx,
    )
    assert out == {"loaded_rows": "4200"}


def test_step_status_exposed() -> None:
    results = {"a": StepResult(status="succeeded", outputs={})}
    ctx = _context(results)
    out = render_step_vars(
        step_id="b",
        jinja_vars={"upstream": "{{ steps.a.status }}"},
        context=ctx,
    )
    assert out == {"upstream": "succeeded"}


def test_empty_jinja_vars_returns_empty() -> None:
    assert render_step_vars(step_id="s", jinja_vars={}, context=_context()) == {}


# ---------------------------------------------------------------------------
# Undefined references are errors (StrictUndefined)
# ---------------------------------------------------------------------------


def test_reference_to_missing_output_raises() -> None:
    ctx = _context({"a": StepResult(status="succeeded", outputs={})})
    with pytest.raises(JinjaRenderError):
        render_step_vars(
            step_id="b",
            jinja_vars={"x": "{{ steps.a.outputs.never_emitted }}"},
            context=ctx,
        )


# ---------------------------------------------------------------------------
# Sandbox: filesystem / import access is rejected
# ---------------------------------------------------------------------------


def test_import_os_is_rejected() -> None:
    ctx = _context()
    with pytest.raises(JinjaRenderError):
        render_step_vars(
            step_id="s",
            jinja_vars={"evil": "{% import os %}"},
            context=ctx,
        )


def test_attribute_escape_to_builtins_is_rejected() -> None:
    # The classic sandbox-escape: reach __class__ / __mro__ / __subclasses__.
    ctx = _context()
    with pytest.raises(JinjaRenderError):
        render_step_vars(
            step_id="s",
            jinja_vars={"evil": "{{ ''.__class__.__mro__[1].__subclasses__() }}"},
            context=ctx,
        )


def test_no_filesystem_loader_open_unavailable() -> None:
    # `open` is not in the namespace and not a sandbox builtin -> undefined,
    # which StrictUndefined turns into a render error.
    ctx = _context()
    with pytest.raises(JinjaRenderError):
        render_step_vars(
            step_id="s",
            jinja_vars={"evil": "{{ open('/etc/passwd').read() }}"},
            context=ctx,
        )


# ---------------------------------------------------------------------------
# make_jinja_context env defaulting
# ---------------------------------------------------------------------------


def test_make_context_env_allowlist_is_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The os.environ allow-list is empty by design: nothing in this unit's
    # scope is a non-secret runtime var, so the `env` namespace defaults to
    # empty rather than leaking any process environment.
    monkeypatch.setenv("DATABASE_URL", "postgres://allowed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    ctx = make_jinja_context(run=_run(), step_results={})
    assert ctx["env"] == {}


def test_database_url_not_reachable_from_template(monkeypatch: pytest.MonkeyPatch) -> None:
    # DATABASE_URL is the password-bearing state-store DSN: it is a secret and
    # must never be reachable from a sandboxed template, even when set in the
    # process environment. With the allow-list empty, `env.DATABASE_URL` is an
    # undefined reference -> StrictUndefined render error (not a leaked DSN).
    monkeypatch.setenv("DATABASE_URL", "postgres://user:secret@host/db")
    ctx = make_jinja_context(run=_run(), step_results={})
    assert "DATABASE_URL" not in ctx["env"]
    with pytest.raises(JinjaRenderError):
        render_step_vars(
            step_id="s",
            jinja_vars={"leak": "{{ env.DATABASE_URL }}"},
            context=ctx,
        )
