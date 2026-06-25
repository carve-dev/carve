"""``carve pipelines`` CLI — validate / list / show / diff.

Drives the typer app with ``CliRunner`` over a tmp project (``carve.toml`` +
``el/<name>/`` convention components + ``pipelines/*.toml``). ``validate`` is the
real schema+DAG gate (the function the verify loop and the engineer ride):
exit 0 on a good pipeline, exit 1 with the structured ``PipelineError`` on a bad
one (at least two failure classes), exit 2 on an unknown name, exit 0 +
"No pipelines found" on an empty ``pipelines/``. ``list`` / ``show`` are config
views (run-history columns deferred to the runtime); ``diff`` is a deferred stub.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from carve.cli.main import app

runner = CliRunner()


# --- fixtures ---------------------------------------------------------------


def _project(tmp_path: Path) -> Path:
    """A project root: carve.toml + an el/stripe/ dlt component + pipelines/."""
    (tmp_path / "carve.toml").write_text('[project]\nname = "t"\n', encoding="utf-8")
    (tmp_path / "el" / "stripe").mkdir(parents=True)
    (tmp_path / "pipelines").mkdir()
    return tmp_path


def _write(project: Path, name: str, body: str) -> None:
    (project / "pipelines" / f"{name}.toml").write_text(body, encoding="utf-8")


_GOOD = """\
[pipeline]
description = "ingest stripe"

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

_CYCLE = """\
[[steps]]
id = "a"
type = "dlt"
component = "stripe"
depends_on = ["b"]

[[steps]]
id = "b"
type = "dlt"
component = "stripe"
depends_on = ["a"]
"""

_DANGLING = """\
[[steps]]
id = "a"
type = "dlt"
component = "stripe"
depends_on = ["ghost"]
"""

_UNRESOLVABLE = """\
[[steps]]
id = "a"
type = "dlt"
component = "does_not_exist"
"""


# --- validate ---------------------------------------------------------------


def test_validate_good_pipeline_exits_zero(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, "p", _GOOD)
    result = runner.invoke(app, ["pipelines", "validate", "p", "--project-dir", str(project)])
    assert result.exit_code == 0
    assert "valid" in result.stdout


def test_validate_cycle_exits_one_with_structured_error(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, "cyc", _CYCLE)
    result = runner.invoke(app, ["pipelines", "validate", "cyc", "--project-dir", str(project)])
    assert result.exit_code == 1
    assert "cycle" in result.stdout.lower()


def test_validate_dangling_depends_on_exits_one_with_structured_error(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, "dangle", _DANGLING)
    result = runner.invoke(app, ["pipelines", "validate", "dangle", "--project-dir", str(project)])
    assert result.exit_code == 1
    assert "ghost" in result.stdout


def test_validate_unresolvable_component_exits_one(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, "unres", _UNRESOLVABLE)
    result = runner.invoke(app, ["pipelines", "validate", "unres", "--project-dir", str(project)])
    assert result.exit_code == 1
    assert "does_not_exist" in result.stdout


def test_validate_unknown_name_exits_two(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = runner.invoke(app, ["pipelines", "validate", "ghost", "--project-dir", str(project)])
    assert result.exit_code == 2
    assert "No pipeline named" in result.stdout


def test_validate_empty_pipelines_dir_exits_zero_with_note(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = runner.invoke(app, ["pipelines", "validate", "--project-dir", str(project)])
    assert result.exit_code == 0
    assert "No pipelines found" in result.stdout


def test_validate_all_passes_over_only_good_pipelines(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, "p", _GOOD)
    result = runner.invoke(app, ["pipelines", "validate", "--project-dir", str(project)])
    assert result.exit_code == 0
    assert "valid" in result.stdout


def test_validate_path_traversal_name_exits_two(tmp_path: Path) -> None:
    # `validate` is agent-reachable (bash read-allowlist), so a `..`-laden name
    # must not resolve outside the project's pipelines/ dir — exit 2, no read.
    project = _project(tmp_path)
    # A plausible escape target a naive join would reach.
    (tmp_path / "foo.toml").write_text("[pipeline]\n", encoding="utf-8")
    result = runner.invoke(
        app, ["pipelines", "validate", "../../foo", "--project-dir", str(project)]
    )
    assert result.exit_code == 2
    assert "Invalid pipeline name" in result.stdout


# --- list -------------------------------------------------------------------


def test_list_renders_files_and_deferred_last_run_column(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, "daily", _GOOD)
    result = runner.invoke(app, ["pipelines", "list", "--project-dir", str(project)])
    assert result.exit_code == 0
    assert "daily" in result.stdout
    assert "Last run" in result.stdout  # the deferred placeholder column header
    assert "Increment 4" in result.stdout


def test_list_empty_exits_zero(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = runner.invoke(app, ["pipelines", "list", "--project-dir", str(project)])
    assert result.exit_code == 0
    assert "No pipelines found" in result.stdout


# --- show -------------------------------------------------------------------


def test_show_renders_config_detail(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, "daily", _GOOD)
    result = runner.invoke(app, ["pipelines", "show", "daily", "--project-dir", str(project)])
    assert result.exit_code == 0
    assert "daily" in result.stdout
    assert "ingest stripe" in result.stdout  # the description
    assert "ingest" in result.stdout and "transform" in result.stdout  # the steps


def test_show_unknown_name_exits_two(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = runner.invoke(app, ["pipelines", "show", "ghost", "--project-dir", str(project)])
    assert result.exit_code == 2
    assert "No pipeline named" in result.stdout


def test_show_path_traversal_name_exits_two(tmp_path: Path) -> None:
    # Same confinement as `validate`: a `..`-laden name must not escape the
    # pipelines/ dir (this surface is read-only but must stay in the project).
    project = _project(tmp_path)
    (tmp_path / "foo.toml").write_text("[pipeline]\n", encoding="utf-8")
    result = runner.invoke(app, ["pipelines", "show", "../../foo", "--project-dir", str(project)])
    assert result.exit_code == 2
    assert "Invalid pipeline name" in result.stdout


def test_show_normal_name_still_works(tmp_path: Path) -> None:
    # The confinement guard does not break a normal in-dir name.
    project = _project(tmp_path)
    _write(project, "daily", _GOOD)
    result = runner.invoke(app, ["pipelines", "show", "daily", "--project-dir", str(project)])
    assert result.exit_code == 0
    assert "daily" in result.stdout


# --- diff (deferred stub) ---------------------------------------------------


def test_diff_is_a_deferred_stub_exits_one(tmp_path: Path) -> None:
    # `diff` is a deferred stub (no per-pipeline build manifest exists yet), so
    # it does not take --project-dir — it short-circuits before resolving a root.
    result = runner.invoke(app, ["pipelines", "diff", "daily", "--against", "build-123"])
    assert result.exit_code == 1
    assert "manifest" in result.stdout.lower()
