"""The dbt verification bridge: a finished run → parse → self-correct to green.

This is the connective-tissue proof for ``integrations/dbt/runner.py`` — the
closure that bridges the substrate ``parse_dbt_run`` (``run_results_path`` +
``manifest_path``) into the harness ``ParseFn`` contract, plus the thin
``run_dbt_check`` / ``make_dbt_verification_loop`` wrappers the agent rides.

FIDELITY (read before trusting this as end-to-end): this proves the
parse → CheckResult → self-correct vertical against a fixture ``run_results.json``
(dbt's real artifact shape) through the **real gated bash tool**. It does NOT run
a live ``dbt build``/``test`` — that live execution is DEFERRED to dbt-execution.
The gated command below is a trivially gate-allowed exit-0 STAND-IN; the verdict
comes from the on-disk ``run_results.json`` (the artifact is the truth — a clean
exit alone is not trusted), exactly as the dlt runner test does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.agents.tools import Tool
from carve.core.agents.tools.bash_tool import make_bash_tool
from carve.core.agents.verification import CheckResult
from carve.integrations.dbt.runner import (
    make_dbt_parse_fn,
    make_dbt_verification_loop,
    run_dbt_check,
)

_FIXTURES = Path(__file__).parent / "fixtures"

# A trivially gate-allowed exit-0 stand-in for the live `dbt build` command —
# live dbt execution is deferred to dbt-execution. The CheckResult comes from
# the on-disk run_results.json, not from this command's exit code.
_RUN_CMD = "true"


def _bash(project_dir: Path) -> Tool:
    """The real gated bash tool in BUILD mode — the single execution path."""
    gate = PermissionGate(build_policy(PermissionMode.BUILD))
    return make_bash_tool(project_dir, gate=gate)


def _seed(name: str, tmp_path: Path) -> Path:
    dest = tmp_path / "run_results.json"
    shutil.copy(_FIXTURES / name, dest)
    return dest


# --- the bridge: parse-fn satisfies the ParseFn contract -------------------


def test_make_dbt_parse_fn_satisfies_the_parsefn_contract(tmp_path: Path) -> None:
    # The closure binds run_results_path; the returned callable takes only a
    # CompletedProcess (the harness ParseFn contract) and reads the on-disk
    # run_results.json the run wrote.
    results = _seed("run_results_green.json", tmp_path)
    parse = make_dbt_parse_fn(run_results_path=results)

    result = parse(subprocess.CompletedProcess(args="dbt", returncode=0, stdout="", stderr=""))
    assert result.passed is True
    assert result.details["failed_nodes"] == []


def test_parse_fn_reports_failure_for_a_failing_fixture(tmp_path: Path) -> None:
    results = _seed("run_results_failing.json", tmp_path)
    parse = make_dbt_parse_fn(run_results_path=results)

    result = parse(subprocess.CompletedProcess(args="dbt", returncode=1, stdout="", stderr=""))
    assert result.passed is False
    assert result.details["failed_nodes"]


# --- run_dbt_check: through the real gated bash ----------------------------


def test_run_dbt_check_parses_a_green_fixture_to_green(tmp_path: Path) -> None:
    results = _seed("run_results_green.json", tmp_path)
    result = run_dbt_check(
        _RUN_CMD,
        run_results_path=results,
        bash_tool=_bash(tmp_path),
    )
    assert result.passed is True
    assert len(result.details["passed_nodes"]) == 4


def test_run_dbt_check_parses_a_failing_fixture_to_red(tmp_path: Path) -> None:
    results = _seed("run_results_failing.json", tmp_path)
    result = run_dbt_check(
        _RUN_CMD,
        run_results_path=results,
        bash_tool=_bash(tmp_path),
    )
    assert result.passed is False
    assert result.details["failed_nodes"][0]["message"].startswith("Got 3 results")


# --- self-correction: a failing artifact, repaired to green ----------------


def test_failing_artifact_self_corrects_to_green(tmp_path: Path) -> None:
    results = _seed("run_results_failing.json", tmp_path)

    loop = make_dbt_verification_loop(
        _RUN_CMD,
        run_results_path=results,
        bash_tool=_bash(tmp_path),
        max_iterations=4,
    )

    fixes = {"n": 0}

    def fix(last: CheckResult) -> bool:
        # Stand-in for the agent's fix action: it repairs the ON-DISK artifact
        # directly (swapping in the green run_results), not a real re-run of dbt
        # (deferred to dbt-execution). The next gated re-check parses green.
        assert last.passed is False
        fixes["n"] += 1
        shutil.copy(_FIXTURES / "run_results_green.json", results)
        return True

    outcome = loop.run(fix)
    assert outcome.status == "passed"
    assert outcome.iterations == 2  # failed once, fixed, passed on re-check
    assert fixes["n"] == 1
    assert outcome.last_result.passed is True


def test_unfixable_artifact_exhausts_to_needs_user_input(tmp_path: Path) -> None:
    results = _seed("run_results_failing.json", tmp_path)
    loop = make_dbt_verification_loop(
        _RUN_CMD,
        run_results_path=results,
        bash_tool=_bash(tmp_path),
        max_iterations=3,
    )
    outcome = loop.run(lambda _last: True)  # "attempts" a fix that never works
    assert outcome.status == "needs_user_input"
    assert outcome.iterations == 3
    assert outcome.last_result.passed is False
