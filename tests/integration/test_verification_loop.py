"""Integration test for the bounded verification loop (no dlt/dbt).

Exercises the spec's verification bullet end-to-end with the real gated
``bash`` tool and a real subprocess, but a fixture ``parse`` callable
(the format owners 04/08 inject the dlt/dbt parsers; here it's a trivial
JSON-from-stdout parser):

* an agent runs a trivial command that writes known JSON;
* ``run_check`` parses it via the fixture parser;
* a broken artifact triggers a bounded self-correction (the fix step
  flips the command to a good one and the loop re-checks to a pass);
* exhausting ``max_verification_iterations`` returns ``needs_user_input``
  with the last :class:`CheckResult` rather than looping forever.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.agents.tools.bash_tool import make_bash_tool
from carve.core.agents.verification import (
    CheckResult,
    VerificationLoop,
    run_check,
)


def _bash(tmp_path: Path):  # type: ignore[no-untyped-def]
    gate = PermissionGate(build_policy(PermissionMode.BUILD))
    return make_bash_tool(tmp_path, gate=gate)


def _parse_status_json(proc: subprocess.CompletedProcess[str]) -> CheckResult:
    """Fixture parser: expect a JSON object with {"status": "ok"} on stdout."""
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return CheckResult(passed=False, summary="output was not valid JSON")
    ok = payload.get("status") == "ok"
    return CheckResult(
        passed=ok,
        summary="status ok" if ok else f"status was {payload.get('status')!r}",
        details=payload,
    )


_OK_JSON = """echo '{"status":"ok","rows":42}'"""
_ERR_JSON = """echo '{"status":"error"}'"""


class TestRunCheckParsesKnownJson:
    def test_trivial_command_writes_and_parses(self, tmp_path: Path) -> None:
        result = run_check(
            _OK_JSON,
            parse=_parse_status_json,
            bash_tool=_bash(tmp_path),
        )
        assert result.passed
        assert result.details["rows"] == 42

    def test_broken_artifact_parses_as_failed(self, tmp_path: Path) -> None:
        result = run_check(
            _ERR_JSON,
            parse=_parse_status_json,
            bash_tool=_bash(tmp_path),
        )
        assert not result.passed
        assert "error" in result.summary


class TestBoundedSelfCorrection:
    def test_fix_step_drives_loop_to_pass(self, tmp_path: Path) -> None:
        # The first command emits a broken artifact; the fix step swaps in
        # a good command and the loop re-checks to a pass. This models the
        # real harness re-running the same check after the agent edits
        # files — here the "edit" is the command swap.
        state = {"cmd": _ERR_JSON}
        bash_tool = _bash(tmp_path)
        attempts = {"n": 0}

        def fix(_last: CheckResult) -> bool:
            attempts["n"] += 1
            state["cmd"] = _OK_JSON  # the "fix"
            return True

        last = CheckResult(passed=False, summary="not run")
        for _iteration in range(1, 5):
            last = run_check(
                state["cmd"], parse=_parse_status_json, bash_tool=bash_tool
            )
            if last.passed:
                break
            if not fix(last):
                break
        assert last.passed
        assert attempts["n"] == 1


class TestExhaustionReturnsNeedsUserInput:
    def test_loop_exhausts_to_needs_user_input(self, tmp_path: Path) -> None:
        # A command that always emits a broken artifact; the fix step keeps
        # trying but never makes it pass. After max_iterations the loop
        # returns needs_user_input with the last result.
        loop = VerificationLoop(
            _ERR_JSON,
            parse=_parse_status_json,
            bash_tool=_bash(tmp_path),
            max_iterations=3,
        )
        fix_calls = {"n": 0}

        def fix(_last: CheckResult) -> bool:
            fix_calls["n"] += 1
            return True  # always "attempts" a fix, but it never works

        outcome = loop.run(fix)
        assert outcome.status == "needs_user_input"
        assert outcome.iterations == 3
        assert not outcome.last_result.passed
        # Fix is attempted between iterations 1→2 and 2→3 (not after the
        # final iteration), so 2 attempts for 3 iterations.
        assert fix_calls["n"] == 2

    def test_fix_giving_up_returns_needs_user_input_early(self, tmp_path: Path) -> None:
        loop = VerificationLoop(
            _ERR_JSON,
            parse=_parse_status_json,
            bash_tool=_bash(tmp_path),
            max_iterations=4,
        )

        def fix(_last: CheckResult) -> bool:
            return False  # the agent can't propose a fix

        outcome = loop.run(fix)
        assert outcome.status == "needs_user_input"
        assert outcome.iterations == 1

    def test_loop_passes_when_check_passes(self, tmp_path: Path) -> None:
        loop = VerificationLoop(
            _OK_JSON,
            parse=_parse_status_json,
            bash_tool=_bash(tmp_path),
            max_iterations=4,
        )
        outcome = loop.run(lambda _r: True)
        assert outcome.status == "passed"
        assert outcome.iterations == 1
