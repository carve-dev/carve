"""``carve component`` / ``carve components`` CLI — graduation + inspection.

* ``carve components show [<name>]`` — the always-on inspection surface over the
  simple-mode convention: convention-discovered components (an ``el/<name>/`` dlt
  + a detected dbt project) with type / mode / resolved path; ``show <name>``
  adds the detail + which pipeline steps reference it; exit 2 on an unknown name.
* ``carve component <name> --separate-local/--separate-remote/--same-repo`` —
  graduation: write (or, ``--same-repo``, drop) the ``[components.<name>]`` block,
  validate it resolves, and backfill omitted dbt-step names. ``--separate-remote``
  clones via the ``sync_workspace`` module-level seam (monkeypatched here — no
  git). Exit 2 when != 1 of the three flags is passed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import carve.cli.commands.component as component_module
from carve.cli.main import app

runner = CliRunner()


# --- fixtures ---------------------------------------------------------------


def _project(tmp_path: Path) -> Path:
    """carve.toml + an el/stripe/ dlt component + a detected dbt project."""
    (tmp_path / "carve.toml").write_text('[project]\nname = "t"\n', encoding="utf-8")
    (tmp_path / "el" / "stripe").mkdir(parents=True)
    (tmp_path / "pipelines").mkdir()
    # A convention dbt project (a <dir>/dbt_project.yml).
    dbt = tmp_path / "analytics"
    dbt.mkdir()
    (dbt / "dbt_project.yml").write_text("name: analytics\n", encoding="utf-8")
    return tmp_path


def _seed_pipeline_referencing_stripe(project: Path) -> None:
    (project / "pipelines" / "p.toml").write_text(
        "[[steps]]\n"
        'id = "ingest"\n'
        'type = "dlt"\n'
        'component = "stripe"\n'
        "\n"
        "[[steps]]\n"
        'id = "build"\n'
        'type = "dbt"\n'
        'command = "run"\n'
        'depends_on = ["ingest"]\n',
        encoding="utf-8",
    )


# --- components show --------------------------------------------------------


def test_components_show_lists_convention_components(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = runner.invoke(app, ["components", "show", "--project-dir", str(project)])
    assert result.exit_code == 0
    # The el/stripe/ dlt + the detected dbt project, with their types.
    assert "stripe" in result.stdout
    assert "analytics" in result.stdout
    assert "dlt" in result.stdout
    assert "dbt" in result.stdout
    assert "convention" in result.stdout


def test_components_show_one_lists_referencing_steps(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _seed_pipeline_referencing_stripe(project)
    result = runner.invoke(app, ["components", "show", "stripe", "--project-dir", str(project)])
    assert result.exit_code == 0
    assert "Component: stripe" in result.stdout
    assert "dlt" in result.stdout
    # The dlt step referencing stripe is surfaced.
    assert "p:ingest" in result.stdout


def test_components_show_unknown_name_exits_two(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = runner.invoke(app, ["components", "show", "ghost", "--project-dir", str(project)])
    assert result.exit_code == 2
    assert "No component named" in result.stdout


# --- component graduation: --separate-local ---------------------------------


def test_graduate_separate_local_writes_block(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _seed_pipeline_referencing_stripe(project)
    # A real local dir for the component to resolve against.
    graduated = tmp_path / "graduated_stripe"
    graduated.mkdir()

    result = runner.invoke(
        app,
        ["component", "stripe", "--separate-local", str(graduated), "--project-dir", str(project)],
    )
    assert result.exit_code == 0, result.stdout

    # carve.toml now parses and carries the [components.stripe] block.
    parsed = tomllib.loads((project / "carve.toml").read_text(encoding="utf-8"))
    block = parsed["components"]["stripe"]
    assert block["type"] == "dlt"
    assert block["mode"] == "separate-local"
    assert block["path"] == str(graduated)
    # `stripe` is a dlt component, so the omitting dbt step is NOT backfilled
    # (that is covered by the dbt-graduation case; see FIX 1).


def test_graduate_separate_local_unknown_component_exits_two(tmp_path: Path) -> None:
    project = _project(tmp_path)
    graduated = tmp_path / "g"
    graduated.mkdir()
    result = runner.invoke(
        app,
        ["component", "ghost", "--separate-local", str(graduated), "--project-dir", str(project)],
    )
    assert result.exit_code == 2
    assert "ghost" in result.stdout


# --- component graduation: --separate-remote (monkeypatched clone) ----------


def test_graduate_separate_remote_uses_sync_workspace_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    # The clone target the fake `sync_workspace` materializes so resolution
    # succeeds — the el/<name>/ shape under the workspace cache.
    calls: list[tuple[str, str]] = []

    def fake_sync_workspace(name, url, branch, paths, *, ref=None):  # type: ignore[no-untyped-def]
        calls.append((name, url))
        # Materialize a resolvable workspace (an el/<name>/ dir) so the
        # post-clone resolve_component validation passes without real git.
        workspace = paths.workspaces_dir / name
        (workspace / "el" / name).mkdir(parents=True, exist_ok=True)
        return workspace

    monkeypatch.setattr(component_module, "sync_workspace", fake_sync_workspace)

    result = runner.invoke(
        app,
        [
            "component",
            "stripe",
            "--separate-remote",
            "https://example.com/stripe.git",
            "--project-dir",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert calls == [("stripe", "https://example.com/stripe.git")]

    parsed = tomllib.loads((project / "carve.toml").read_text(encoding="utf-8"))
    block = parsed["components"]["stripe"]
    assert block["mode"] == "separate-remote"
    assert block["url"] == "https://example.com/stripe.git"


# --- component graduation: --same-repo (reverse) ----------------------------


def test_same_repo_reverses_graduation_dropping_the_block(tmp_path: Path) -> None:
    project = _project(tmp_path)
    graduated = tmp_path / "g"
    graduated.mkdir()
    # Graduate first so there's a block to reverse.
    runner.invoke(
        app,
        ["component", "stripe", "--separate-local", str(graduated), "--project-dir", str(project)],
    )
    assert "components" in tomllib.loads((project / "carve.toml").read_text(encoding="utf-8"))

    result = runner.invoke(
        app, ["component", "stripe", "--same-repo", "--project-dir", str(project)]
    )
    assert result.exit_code == 0
    assert "Reversed" in result.stdout
    # The block is gone (resolves by convention again).
    parsed = tomllib.loads((project / "carve.toml").read_text(encoding="utf-8"))
    assert "components" not in parsed


# --- flag arity -------------------------------------------------------------


def test_no_graduation_flag_exits_two(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = runner.invoke(app, ["component", "stripe", "--project-dir", str(project)])
    assert result.exit_code == 2
    assert "exactly one" in result.stdout


def test_two_graduation_flags_exits_two(tmp_path: Path) -> None:
    project = _project(tmp_path)
    graduated = tmp_path / "g"
    graduated.mkdir()
    result = runner.invoke(
        app,
        [
            "component",
            "stripe",
            "--separate-local",
            str(graduated),
            "--same-repo",
            "--project-dir",
            str(project),
        ],
    )
    assert result.exit_code == 2
    assert "exactly one" in result.stdout


# --- FIX 1: graduating a dlt component must NOT backfill its name into dbt steps


def test_graduate_dlt_component_does_not_backfill_dbt_steps(tmp_path: Path) -> None:
    # A pipeline with a dbt step that OMITS `component` (simple-mode convenience).
    # Graduating the `stripe` *dlt* component must leave that dbt step untouched —
    # backfilling a dlt name into it would point the step at a dlt component and
    # break step-type/component-type validation.
    project = _project(tmp_path)
    _seed_pipeline_referencing_stripe(project)  # `build` is a dbt step omitting component
    graduated = tmp_path / "graduated_stripe"
    graduated.mkdir()

    result = runner.invoke(
        app,
        ["component", "stripe", "--separate-local", str(graduated), "--project-dir", str(project)],
    )
    assert result.exit_code == 0, result.stdout

    # The dbt step's `component` is STILL omitted (not backfilled to a dlt name).
    pipeline_text = (project / "pipelines" / "p.toml").read_text(encoding="utf-8")
    parsed_pipeline = tomllib.loads(pipeline_text)
    dbt_step = next(s for s in parsed_pipeline["steps"] if s["type"] == "dbt")
    assert "component" not in dbt_step
    assert "Backfilled" not in result.stdout

    # And the project still validates — the dbt step did not gain a dlt reference.
    validate = runner.invoke(app, ["pipelines", "validate", "p", "--project-dir", str(project)])
    assert validate.exit_code == 0, validate.stdout


def test_graduate_dbt_component_does_backfill_dbt_steps(tmp_path: Path) -> None:
    # The kept behavior: graduating the *dbt* component DOES backfill its name
    # into omitting dbt steps (those steps must name the now-graduated component).
    project = _project(tmp_path)
    _seed_pipeline_referencing_stripe(project)  # `build` is a dbt step omitting component
    graduated = tmp_path / "graduated_dbt"
    graduated.mkdir()

    result = runner.invoke(
        app,
        [
            "component",
            "analytics",
            "--separate-local",
            str(graduated),
            "--project-dir",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout

    pipeline_text = (project / "pipelines" / "p.toml").read_text(encoding="utf-8")
    assert 'component = "analytics"' in pipeline_text
    assert "Backfilled" in result.stdout


# --- FIX 3: a malformed pipeline file must not abort graduation ---------------


def test_graduate_skips_malformed_pipeline_file(tmp_path: Path) -> None:
    # One valid pipeline (a dbt step omitting component) + one malformed TOML.
    # Graduating the dbt component must backfill the valid file and SKIP the
    # malformed one without crashing (carve.toml is already mutated by then).
    project = _project(tmp_path)
    _seed_pipeline_referencing_stripe(project)  # pipelines/p.toml — valid, dbt step omits component
    (project / "pipelines" / "broken.toml").write_text(
        "this is = = not valid toml [[[", encoding="utf-8"
    )
    graduated = tmp_path / "graduated_dbt"
    graduated.mkdir()

    result = runner.invoke(
        app,
        [
            "component",
            "analytics",
            "--separate-local",
            str(graduated),
            "--project-dir",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout

    # The valid file was backfilled; the malformed one was left untouched.
    assert 'component = "analytics"' in (project / "pipelines" / "p.toml").read_text(
        encoding="utf-8"
    )
    assert (project / "pipelines" / "broken.toml").read_text(encoding="utf-8").startswith("this is")
    # carve.toml carries the graduated block (graduation completed, no traceback).
    parsed = tomllib.loads((project / "carve.toml").read_text(encoding="utf-8"))
    assert parsed["components"]["analytics"]["mode"] == "separate-local"


# --- FIX 4: a `..`-laden component name is rejected before any work ----------


def test_component_name_with_traversal_exits_two_and_leaves_carve_toml(tmp_path: Path) -> None:
    project = _project(tmp_path)
    # A sibling el dir so `el/../evil` would otherwise be a real path.
    (tmp_path / "evil").mkdir()
    before = (project / "carve.toml").read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        ["component", "../evil", "--separate-local", "/tmp/x", "--project-dir", str(project)],
    )
    assert result.exit_code == 2
    assert "Invalid component name" in result.stdout
    # carve.toml is unchanged — no junk [components."../evil"] block written.
    assert (project / "carve.toml").read_text(encoding="utf-8") == before
