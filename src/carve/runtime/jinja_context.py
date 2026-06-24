"""The sandboxed cross-step Jinja context.

A step's ``jinja_vars`` (e.g. ``loaded_rows = "{{ steps.ingest.outputs.rows
}}"``) are rendered against a fixed ``{steps, run, env}`` namespace at
*launch time* — after the step's dependencies complete and before its
executor runs — so a value can reference an upstream step's outputs. The
rendered map is passed to the executor as part of the resolved step config.

Rendering uses Jinja's :class:`~jinja2.sandbox.SandboxedEnvironment`: only
the exposed namespace is reachable. There is **no** filesystem loader and
**no** import access — a template attempting ``{% import os %}`` or any
unsafe attribute access raises a sandbox/Jinja error rather than escaping
the namespace.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from jinja2 import StrictUndefined
from jinja2.exceptions import TemplateError
from jinja2.sandbox import SandboxedEnvironment

if TYPE_CHECKING:
    from carve.runtime.run_context import PipelineRun
    from carve.runtime.step_executor import StepResult


class JinjaRenderError(Exception):
    """Raised when a step's ``jinja_vars`` fail to render.

    Wraps the underlying Jinja error (a sandbox violation, an undefined
    reference, a syntax error) with the offending step id + var name so the
    failure is actionable.
    """


# Env vars exposed to templates. Deliberately a *small allow-list* of
# non-secret, runtime-relevant vars — never the raw environment, so a
# template can't read credentials out of `env`. The runtime may widen this
# via config later; secrets never belong here (spec §"Jinja context").
#
# Empty by design in this unit: nothing in scope needs an env var in a
# template, and the obvious candidate (``DATABASE_URL``) is the password-
# bearing Postgres state-store DSN — a secret, which the spec forbids from
# the ``env`` namespace. The allow-list *mechanism* is retained so a
# genuinely non-secret runtime var can be added here later.
_EXPOSED_ENV_KEYS: tuple[str, ...] = ()


def build_sandbox() -> SandboxedEnvironment:
    """Construct the sandboxed environment used for every render.

    ``StrictUndefined`` makes a reference to a missing key (a Jinja var
    pointing at an output the upstream step never emitted) a render error
    rather than silently producing an empty string — surfacing the
    composition bug the spec calls out.

    Shared so every Jinja render in the runtime (the launch-time
    ``jinja_vars`` here, the ``sql`` step's file body) uses the identical
    sandbox configuration.
    """
    return SandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def make_jinja_context(
    *,
    run: PipelineRun,
    step_results: dict[str, StepResult],
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the ``{steps, run, env}`` namespace for cross-step rendering.

    * ``steps`` maps each *completed* step id to ``{outputs, status,
      started_at, finished_at}`` — the surface a downstream template reads
      (``{{ steps.X.outputs.rows_loaded }}``).
    * ``run`` carries the run identity/dispatch fields.
    * ``env`` is the small exposed-env allow-list (never secrets).
    """
    steps_ns: dict[str, dict[str, Any]] = {}
    for step_id, result in step_results.items():
        steps_ns[step_id] = {
            "outputs": result.outputs,
            "status": result.status,
            "started_at": result.started_at.isoformat() if result.started_at else None,
            "finished_at": result.finished_at.isoformat() if result.finished_at else None,
        }

    if env is None:
        env = {k: os.environ[k] for k in _EXPOSED_ENV_KEYS if k in os.environ}

    return {
        "steps": steps_ns,
        "run": {
            "id": run.id,
            "pipeline": run.pipeline,
            "target": run.target,
            "trigger": run.trigger,
            "started_at": run.started_at.isoformat(),
        },
        "env": env,
    }


def render_step_vars(
    *,
    step_id: str,
    jinja_vars: dict[str, str],
    context: dict[str, Any],
) -> dict[str, str]:
    """Render a step's ``jinja_vars`` against ``context`` in the sandbox.

    Returns the rendered ``{name: value}`` map (every value a string —
    Jinja renders to text). Raises :class:`JinjaRenderError` on any sandbox
    violation, undefined reference, or syntax error, naming the step + var.
    """
    if not jinja_vars:
        return {}

    sandbox = build_sandbox()
    rendered: dict[str, str] = {}
    for name, template_str in jinja_vars.items():
        try:
            template = sandbox.from_string(template_str)
            rendered[name] = template.render(context)
        except TemplateError as exc:
            raise JinjaRenderError(
                f"Failed to render jinja_var {name!r} on step {step_id!r}: {exc}"
            ) from exc
    return rendered


__all__ = [
    "JinjaRenderError",
    "build_sandbox",
    "make_jinja_context",
    "render_step_vars",
]
