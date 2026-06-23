"""The dlt verification vertical: author → run → parse → self-correct to green.

This is the connective-tissue proof for ``integrations/dlt/runner.py`` — the
closure that bridges the substrate ``parse_dlt_run`` (``pipelines_dir`` +
``pipeline_name``) into the harness ``ParseFn`` contract, plus the thin
``run_dlt_check`` / ``make_dlt_verification_loop`` wrappers the agent rides.

Two layers, both real:

* **The bridge composes.** ``make_dlt_parse_fn`` turns ``parse_dlt_run`` into a
  ``ParseFn`` and ``run_dlt_check`` runs a command through the **real gated
  ``bash`` tool** (mirroring ``tests/integration/test_verification_loop.py``),
  parsing a **real DuckDB load package** written via dlt's Python API
  (mirroring ``tests/integrations/dlt/test_verify.py``) into a green
  ``CheckResult``. The load package is real; the gated *command* is a stand-in
  (see ``_RUN_CMD``) — live component execution via the venv runner is deferred.
* **Self-correction.** A deliberately-broken load artifact parses as failed; a
  stub fix-step repairs the **on-disk package** (it does not re-author or re-run
  dlt) and ``make_dlt_verification_loop`` re-checks (through the same gated
  bash) to green within its iteration ceiling.

The single execution path is the gated ``bash`` tool — the runner opens no
second exec path, so each iteration's command runs through the same allowlist,
scrubbed env, and cwd-pin the agent uses.

Out of scope here (structural-only / deferred): the ``sql``-tool schema-confirm
step that closes the loop lands with the orchestrator-injected ``sql`` tool —
the runner stops at the parsed ``CheckResult``; confirming the loaded schema via
``sql`` is a later orchestrator-wiring unit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.agents.tools import Tool
from carve.core.agents.tools.bash_tool import make_bash_tool
from carve.core.agents.verification import CheckResult
from carve.integrations.dlt.runner import (
    dlt_inspect_command,
    make_dlt_parse_fn,
    make_dlt_verification_loop,
    run_dlt_check,
)

# FIDELITY OF THIS TEST (read before trusting it as end-to-end):
# This proves the parse -> CheckResult -> self-correct vertical against a REAL
# DuckDB load package (written via dlt's Python API, as test_verify.py does).
# It does NOT execute a dlt component. In Carve a component is run by executing
# its Python module via the venv runner (`LocalVenvRunner`) — `python` is
# gate-denied, so there is no `dlt pipeline run` CLI to invoke. Wiring that live
# execution into this loop is DEFERRED to the orchestrator-wiring unit; until
# then the command below is a trivially gate-allowed exit-0 STAND-IN, not a real
# load run. That is intentional and sufficient here: `parse_dlt_run` reads the
# *on-disk load package* the load wrote (a script can exit 0 with a partial
# load; the package is truth), so the stand-in command exercises the real
# gated-bash path without depending on a real load run or on `dlt` being on the
# scrubbed-env PATH. The CheckResult comes from the package, as designed.
_RUN_CMD = "true"


def _bash(project_dir: Path) -> Tool:
    """The real gated bash tool in BUILD mode — the single execution path."""
    gate = PermissionGate(build_policy(PermissionMode.BUILD))
    return make_bash_tool(project_dir, gate=gate)


def _real_dlt_load(pipelines_dir: Path, pipeline_name: str) -> None:
    """Run a real, creds-free DuckDB load that writes a clean load package."""
    import dlt

    @dlt.resource(name="orders", write_disposition="replace")
    def orders():  # type: ignore[no-untyped-def]
        yield {"id": 1, "amount": 9.99}
        yield {"id": 2, "amount": 4.50}

    pipe = dlt.pipeline(
        pipeline_name=pipeline_name,
        destination="duckdb",
        dataset_name="ds",
        pipelines_dir=str(pipelines_dir),
    )
    pipe.run(orders())


# --- the bridge: parse-fn + run_dlt_check over a real load -----------------


def test_make_dlt_parse_fn_satisfies_the_parsefn_contract(tmp_path: Path) -> None:
    # The closure binds pipelines_dir + pipeline_name; the returned callable
    # takes only a CompletedProcess (the harness ParseFn contract) and reads the
    # on-disk load package a real run wrote.
    import subprocess

    pipelines_dir = tmp_path / "dlt"
    _real_dlt_load(pipelines_dir, "real")

    parse = make_dlt_parse_fn(pipelines_dir, "real")
    result = parse(subprocess.CompletedProcess(args="dlt", returncode=0, stdout="", stderr=""))

    assert result.passed is True
    assert "orders" in result.details["tables"]


def test_run_dlt_check_parses_a_real_load_to_green(tmp_path: Path) -> None:
    # Full vertical: a real DuckDB load + a real gated-bash command, parsed into
    # a green CheckResult through run_dlt_check (no second exec path).
    pipelines_dir = tmp_path / "dlt"
    _real_dlt_load(pipelines_dir, "real")

    result = run_dlt_check(
        _RUN_CMD,
        pipelines_dir=pipelines_dir,
        pipeline_name="real",
        bash_tool=_bash(tmp_path),
    )

    assert result.passed is True
    assert "orders" in result.details["tables"]
    assert "orders" in result.details["schema_changes"]


def test_dlt_inspect_command_centralizes_the_inspect_shape() -> None:
    # `dlt pipeline <name> info` is read-only INSPECTION — it does not run a
    # load (dlt 1.28 has no `dlt pipeline run`/`check` CLI; live execution is via
    # the venv runner, deferred). The helper keeps the inspect shape in one place
    # as a single gate-shaped command (no `&&` — the gate denies chaining).
    cmd = dlt_inspect_command("hackernews")
    assert "hackernews" in cmd
    assert "info" in cmd
    assert "&&" not in cmd and ";" not in cmd and "|" not in cmd


# --- self-correction: a broken artifact, repaired to green -----------------


def _break_load_package(pipelines_dir: Path, pipeline_name: str) -> Path:
    """Demote the newest 'loaded' package to 'normalized' — a failed load.

    A real run wrote a clean package; moving it out of `loaded/` models a run
    whose load step did not complete (`parse_dlt_run` reports completed=False,
    passed=False). Returns the demoted package dir so the fix-step can repair it.
    """
    loaded = pipelines_dir / pipeline_name / "load" / "loaded"
    pkg = next(p for p in loaded.iterdir() if p.is_dir())
    broken_parent = pipelines_dir / pipeline_name / "load" / "normalized"
    broken_parent.mkdir(parents=True, exist_ok=True)
    target = broken_parent / pkg.name
    pkg.rename(target)
    # Drop the completion marker so it can't be read as a finished load.
    marker = target / "package_completed.json"
    if marker.exists():
        marker.unlink()
    return target


def _repair_load_package(broken_pkg: Path, pipelines_dir: Path, pipeline_name: str) -> None:
    """Move the package back to 'loaded' and restore the completion marker."""
    loaded = pipelines_dir / pipeline_name / "load" / "loaded"
    loaded.mkdir(parents=True, exist_ok=True)
    fixed = loaded / broken_pkg.name
    broken_pkg.rename(fixed)
    (fixed / "package_completed.json").write_text(json.dumps("loaded"), encoding="utf-8")


def test_broken_artifact_self_corrects_to_green(tmp_path: Path) -> None:
    pipelines_dir = tmp_path / "dlt"
    _real_dlt_load(pipelines_dir, "real")

    # Deliberately break the load artifact: the first check will parse as failed.
    broken_pkg = _break_load_package(pipelines_dir, "real")

    loop = make_dlt_verification_loop(
        _RUN_CMD,
        pipelines_dir=pipelines_dir,
        pipeline_name="real",
        bash_tool=_bash(tmp_path),
        max_iterations=4,
    )

    fixes = {"n": 0}

    def fix(last: CheckResult) -> bool:
        # Stand-in for the agent's fix action between check runs. It repairs the
        # ON-DISK load package directly; it does NOT re-author or re-run dlt. So
        # this proves the parse -> CheckResult -> self-correct loop mechanics,
        # not end-to-end dlt self-correction (real re-loads are deferred to the
        # orchestrator-wiring unit). The next gated re-check then parses green.
        assert last.passed is False
        fixes["n"] += 1
        _repair_load_package(broken_pkg, pipelines_dir, "real")
        return True

    outcome = loop.run(fix)

    assert outcome.status == "passed"
    assert outcome.iterations == 2  # failed once, fixed, passed on re-check
    assert fixes["n"] == 1
    assert outcome.last_result.passed is True
    assert "orders" in outcome.last_result.details["tables"]


def test_unfixable_broken_artifact_exhausts_to_needs_user_input(tmp_path: Path) -> None:
    # When the fix-step never repairs the artifact, the bounded loop surfaces to
    # the user rather than looping forever (the harness ceiling holds).
    pipelines_dir = tmp_path / "dlt"
    _real_dlt_load(pipelines_dir, "real")
    _break_load_package(pipelines_dir, "real")

    loop = make_dlt_verification_loop(
        _RUN_CMD,
        pipelines_dir=pipelines_dir,
        pipeline_name="real",
        bash_tool=_bash(tmp_path),
        max_iterations=3,
    )

    outcome = loop.run(lambda _last: True)  # "attempts" a fix that never works

    assert outcome.status == "needs_user_input"
    assert outcome.iterations == 3
    assert outcome.last_result.passed is False


@pytest.fixture(autouse=True)
def _isolate_duckdb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep dlt's default duckdb file out of the repo working dir (mirrors
    # tests/integrations/dlt/test_verify.py).
    monkeypatch.chdir(tmp_path)
