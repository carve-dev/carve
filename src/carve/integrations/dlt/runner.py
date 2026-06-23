"""The dlt verification runner — bridge ``parse_dlt_run`` into the harness loop.

A dlt component is *executed* by running its Python module; in Carve that
execution goes through the venv runner (``LocalVenvRunner``), not freeform bash
(``python`` is gate-denied). Wiring that live execution into this loop is
deferred to the later orchestrator-wiring unit — this module is the parse →
:class:`~carve.core.agents.verification.CheckResult` bridge plus the
verification-loop composition, nothing more.

The substrate :func:`carve.integrations.dlt.verify.parse_dlt_run` *parses* a
finished dlt load (executed via Carve's venv runner) into a
:class:`~carve.core.agents.verification.CheckResult` by reading the on-disk
load package — but its signature takes ``pipelines_dir`` + ``pipeline_name``
keyword arguments, so it does **not** match the harness ``ParseFn`` contract
(``CompletedProcess -> CheckResult``) that
:func:`carve.core.agents.verification.run_check` injects.

This module is the missing connective tissue: a closure that binds those two
paths into a ``ParseFn`` (:func:`make_dlt_parse_fn`), plus thin wrappers
(:func:`run_dlt_check`, :func:`make_dlt_verification_loop`) that compose the
harness ``run_check`` / :class:`VerificationLoop` with the dlt parser so the
agent — and, later, recovery — can bridge a finished dlt load into a real
:class:`CheckResult` without re-implementing the bridge.

**Single execution path (hard invariant).** This module opens **no** new
bash/exec path. Every command runs through the injected, gated ``bash`` tool
exactly as :func:`run_check` does — the gate's allowlist, scrubbed env,
cwd-pin, and output cap apply unchanged. The runner only *parses*; it never
spawns a subprocess of its own.

**Substrate caveat (already handled — confirmed, not re-handled here):**
``run_check`` routes all captured output to ``stdout`` with ``stderr=""``, and
``parse_dlt_run`` reads ``proc.stdout or proc.stderr`` — so the closure
composes. The on-disk **load package** (``state.json`` and friends) the load
wrote is the source of truth ``parse_dlt_run`` reads, and the verdict comes
from that package, not from a CLI command; a clean exit code alone is not
trusted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from carve.core.agents.tools import Tool
from carve.core.agents.verification import (
    MAX_VERIFICATION_ITERATIONS,
    CheckResult,
    ParseFn,
    VerificationLoop,
    run_check,
)
from carve.integrations.dlt.verify import parse_dlt_run


def make_dlt_parse_fn(pipelines_dir: Path, pipeline_name: str) -> ParseFn:
    """Bind ``parse_dlt_run`` into the harness ``ParseFn`` contract.

    ``parse_dlt_run`` needs ``pipelines_dir`` + ``pipeline_name`` to find the
    on-disk load package; ``run_check`` injects a ``ParseFn`` that takes only a
    finished ``CompletedProcess``. This closes over the two paths and returns
    the ``ParseFn`` the harness expects — the whole bridge, in one place.
    """
    resolved = Path(pipelines_dir)

    def _parse(proc: subprocess.CompletedProcess[str]) -> CheckResult:
        return parse_dlt_run(proc, pipelines_dir=resolved, pipeline_name=pipeline_name)

    return _parse


def dlt_inspect_command(pipeline_name: str) -> str:
    """Build the read-only ``dlt pipeline <name> info`` inspection command.

    ``dlt`` 1.28 has **no** ``dlt pipeline run`` / ``dlt pipeline check`` CLI;
    ``dlt pipeline <name>`` only *inspects* a pipeline's local state
    (``info``/``trace``/``show``). This helper centralizes the inspection
    command shape so callers don't hand-build ``dlt pipeline`` strings — it
    does **not** run a load. Actual component execution is via Carve's venv
    runner (``LocalVenvRunner``); that live wiring is deferred to the later
    orchestrator-wiring unit. The harness ``bash`` gate **denies command
    chaining** (``&&``/``;``/pipes), so this stays a single gate-shaped command.
    """
    return f"dlt pipeline {pipeline_name} info"


def run_dlt_check(
    cmd: str,
    *,
    pipelines_dir: Path,
    pipeline_name: str,
    bash_tool: Tool,
    timeout: int = 120,
) -> CheckResult:
    """Run ``cmd`` through the gated ``bash`` tool and bridge a finished load.

    A one-call convenience over :func:`carve.core.agents.verification.run_check`
    with the dlt parse-fn already bound. The dlt load itself is executed via
    Carve's venv runner (deferred wiring); the verdict's truth comes from the
    on-disk load package at ``pipelines_dir/pipeline_name`` that
    ``parse_dlt_run`` reads — not from ``cmd``'s exit code.
    """
    return run_check(
        cmd,
        parse=make_dlt_parse_fn(pipelines_dir, pipeline_name),
        bash_tool=bash_tool,
        timeout=timeout,
    )


def make_dlt_verification_loop(
    cmd: str,
    *,
    pipelines_dir: Path,
    pipeline_name: str,
    bash_tool: Tool,
    max_iterations: int = MAX_VERIFICATION_ITERATIONS,
    timeout: int = 120,
) -> VerificationLoop:
    """Build a :class:`VerificationLoop` wired to the dlt parse-fn.

    The agent rides this loop to bridge a finished dlt load (executed via
    Carve's venv runner — deferred wiring) into the parsed
    :class:`CheckResult`, and self-correct (author → load → read → fix) bounded
    by the loop's ceilings (``max_iterations`` + the per-invocation cost
    budget). No new execution path: the loop runs ``cmd`` through ``bash_tool``,
    the same gated bash the agent uses.
    """
    return VerificationLoop(
        cmd,
        parse=make_dlt_parse_fn(pipelines_dir, pipeline_name),
        bash_tool=bash_tool,
        max_iterations=max_iterations,
        timeout=timeout,
    )


__all__ = [
    "dlt_inspect_command",
    "make_dlt_parse_fn",
    "make_dlt_verification_loop",
    "run_dlt_check",
]
