"""The pipeline-engineer verify vertical: command shape, parse, self-correct.

Two layers, mirroring ``tests/integrations/dlt/test_runner.py``:

* **The bridge composes (pure).** ``pipeline_validate_command`` centralizes the
  ``carve pipelines validate [<name>]`` shape; ``parse_pipeline_validate`` adapts
  a finished ``CompletedProcess`` into a harness ``CheckResult`` (passed from the
  exit code, summary/details from stdout); ``make_pipeline_validate_parse_fn``
  exposes that as a ``ParseFn``. These run on synthetic processes and always run.

* **Self-correction over the REAL gated bash + REAL ``carve`` CLI.** Now that
  ``carve pipelines validate`` is bash-allowlisted, the verify loop runs the real
  ``carve`` subprocess through the same gated bash the agent uses. A broken
  composition (a dangling ``depends_on``) validates non-zero; a stub fix-step
  repairs the TOML on disk and the loop re-checks to green within its ceiling.
  An unfixable break exhausts the bounded loop to ``needs_user_input``.

The single execution path is the gated ``bash`` tool — no second exec path, so
each iteration runs through the same allowlist + scrubbed env + cwd-pin. The
loop tests prepend the venv ``bin`` to ``PATH`` so the gated bash resolves the
working (this-checkout) ``carve``, keeping the real subprocess deterministic
rather than dependent on whatever ``carve`` happens to be first on ``PATH``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.agents.tools import Tool
from carve.core.agents.tools.bash_tool import make_bash_tool
from carve.core.agents.verification import CheckResult, ParseFn
from carve.runtime.pipeline_verify import (
    make_pipeline_validate_parse_fn,
    make_pipeline_verification_loop,
    parse_pipeline_validate,
    pipeline_validate_command,
)

# --- command shape ----------------------------------------------------------


def test_pipeline_validate_command_without_name() -> None:
    assert pipeline_validate_command() == "carve pipelines validate"
    assert pipeline_validate_command(None) == "carve pipelines validate"


def test_pipeline_validate_command_with_name() -> None:
    assert pipeline_validate_command("stripe") == "carve pipelines validate stripe"


def test_pipeline_validate_command_stays_a_single_gate_shaped_command() -> None:
    cmd = pipeline_validate_command("daily")
    assert "&&" not in cmd and ";" not in cmd and "|" not in cmd


# --- parse: CompletedProcess -> CheckResult ---------------------------------


def _proc(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args="carve", returncode=returncode, stdout=stdout, stderr=""
    )


def test_parse_passes_on_exit_zero() -> None:
    result = parse_pipeline_validate(_proc(0, "All 1 pipeline(s) valid.\n"))
    assert result.passed is True
    assert result.summary == "All 1 pipeline(s) valid."
    assert result.details == {"exit_code": 0}


def test_parse_passes_with_default_summary_when_no_output() -> None:
    result = parse_pipeline_validate(_proc(0, ""))
    assert result.passed is True
    assert "passed" in result.summary
    assert result.details == {"exit_code": 0}


def test_parse_fails_on_nonzero_exit_with_output_tail() -> None:
    stdout = (
        "PipelineError: Step 'transform' depends on unknown step 'ghost'\n"
        "  File: pipelines/p.toml\n"
        "  Hint: Every depends_on entry must name an existing step id.\n"
    )
    result = parse_pipeline_validate(_proc(1, stdout))
    assert result.passed is False
    # Summary is the first line, capped at 300 chars.
    assert result.summary.startswith("PipelineError: Step 'transform'")
    assert len(result.summary) <= 300
    assert result.details["exit_code"] == 1
    assert "depends on unknown step 'ghost'" in str(result.details["output_tail"])


def test_parse_caps_a_long_summary_at_300_chars() -> None:
    long_first_line = "PipelineError: " + ("x" * 500)
    result = parse_pipeline_validate(_proc(1, long_first_line + "\n  File: p.toml\n"))
    assert result.passed is False
    assert len(result.summary) == 300


def test_make_pipeline_validate_parse_fn_returns_the_parser() -> None:
    parse_fn: ParseFn = make_pipeline_validate_parse_fn()
    assert parse_fn is parse_pipeline_validate
    # And it satisfies the ParseFn contract (CompletedProcess -> CheckResult).
    assert parse_fn(_proc(0, "ok")).passed is True


# --- the verify loop over the REAL gated bash + REAL carve CLI --------------

_BROKEN = """\
[[steps]]
id = "ingest"
type = "dlt"
component = "stripe"

[[steps]]
id = "transform"
type = "dlt"
component = "stripe"
depends_on = ["ghost"]
"""

_REPAIRED = """\
[[steps]]
id = "ingest"
type = "dlt"
component = "stripe"

[[steps]]
id = "transform"
type = "dlt"
component = "stripe"
depends_on = ["ingest"]
"""


def _project(tmp_path: Path) -> Path:
    """A minimal project: carve.toml + an el/<name>/ component + pipelines/."""
    (tmp_path / "carve.toml").write_text('[project]\nname = "t"\n', encoding="utf-8")
    (tmp_path / "el" / "stripe").mkdir(parents=True)
    (tmp_path / "pipelines").mkdir()
    return tmp_path


def _bash(project_dir: Path) -> Tool:
    """The real gated bash tool in BUILD mode — the single execution path."""
    gate = PermissionGate(build_policy(PermissionMode.BUILD))
    return make_bash_tool(project_dir, gate=gate)


@pytest.fixture
def _carve_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put this checkout's ``carve`` first on PATH for the gated subprocess.

    The gated bash inherits ``PATH`` from ``os.environ`` (scrubbed of creds but
    PATH is kept), and resolves ``carve`` through it. A stale global ``carve``
    may sit ahead of the venv on the inherited PATH, so prepend the running
    interpreter's ``bin`` (the venv that has this checkout installed) to make the
    real ``carve pipelines validate`` subprocess deterministic.
    """
    import os

    venv_bin = Path(sys.executable).parent
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), os.environ.get("PATH", "")]))


def _carve_resolves(project_dir: Path) -> bool:
    """True iff ``carve pipelines validate`` runs through the gated bash here.

    Guards the real-subprocess loop tests: if the gated bash can't resolve a
    working ``carve`` (no venv install on PATH), skip rather than fail — the
    pure parse/command tests above carry the always-on coverage.
    """
    out = _bash(project_dir).executor({"command": "carve pipelines validate", "timeout": 60})
    return isinstance(out, dict) and isinstance(out.get("exit_code"), int) and out["exit_code"] == 0


@pytest.mark.usefixtures("_carve_on_path")
def test_broken_composition_self_corrects_to_green(tmp_path: Path) -> None:
    project = _project(tmp_path)
    if not _carve_resolves(project):
        pytest.skip("carve CLI not resolvable through the gated bash in this environment")

    (project / "pipelines" / "p.toml").write_text(_BROKEN, encoding="utf-8")

    loop = make_pipeline_verification_loop(
        bash_tool=_bash(project),
        pipeline_name="p",
        max_iterations=4,
    )

    fixes = {"n": 0}

    def fix(last: CheckResult) -> bool:
        # Stand-in for the agent's fix action: repair the TOML on disk. The next
        # gated re-check runs the real `carve pipelines validate` and parses green.
        assert last.passed is False
        assert "ghost" in last.summary or "ghost" in str(last.details.get("output_tail", ""))
        fixes["n"] += 1
        (project / "pipelines" / "p.toml").write_text(_REPAIRED, encoding="utf-8")
        return True

    outcome = loop.run(fix)

    assert outcome.status == "passed"
    assert outcome.iterations == 2  # failed once, fixed, passed on re-check
    assert fixes["n"] == 1
    assert outcome.last_result.passed is True


@pytest.mark.usefixtures("_carve_on_path")
def test_unfixable_composition_exhausts_to_needs_user_input(tmp_path: Path) -> None:
    project = _project(tmp_path)
    if not _carve_resolves(project):
        pytest.skip("carve CLI not resolvable through the gated bash in this environment")

    (project / "pipelines" / "p.toml").write_text(_BROKEN, encoding="utf-8")

    loop = make_pipeline_verification_loop(
        bash_tool=_bash(project),
        pipeline_name="p",
        max_iterations=3,
    )

    # The fix-step never repairs the TOML, so the bounded loop surfaces to the
    # user rather than looping forever (the harness ceiling holds).
    outcome = loop.run(lambda _last: True)

    assert outcome.status == "needs_user_input"
    assert outcome.iterations == 3
    assert outcome.last_result.passed is False
