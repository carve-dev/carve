"""Integration: the `carve memory` CLI (show / edit / append-decision)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import carve.cli.commands.memory as memory_cmd
from carve.cli.main import app
from carve.init.templates import (
    CONVENTIONS_MD_CONTENT,
    DECISIONS_MD_CONTENT,
    STANDARDS_MD_CONTENT,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    carve = tmp_path / "carve"
    carve.mkdir(parents=True)
    (carve / "conventions.md").write_text(CONVENTIONS_MD_CONTENT, encoding="utf-8")
    (carve / "standards.md").write_text(STANDARDS_MD_CONTENT, encoding="utf-8")
    (carve / "decisions.md").write_text(DECISIONS_MD_CONTENT, encoding="utf-8")
    return tmp_path


def test_show_lists_memory_files(runner: CliRunner, project: Path) -> None:
    res = runner.invoke(app, ["memory", "show", "--project-dir", str(project)])
    assert res.exit_code == 0, res.output
    assert "conventions" in res.output
    assert "standards" in res.output
    assert "decisions" in res.output


def test_show_prints_one_file(runner: CliRunner, project: Path) -> None:
    res = runner.invoke(app, ["memory", "show", "standards", "--project-dir", str(project)])
    assert res.exit_code == 0, res.output
    assert "Team standards" in res.output


def test_show_unknown_kind_exits_2(runner: CliRunner, project: Path) -> None:
    res = runner.invoke(app, ["memory", "show", "bogus", "--project-dir", str(project)])
    assert res.exit_code == 2, res.output
    assert "unknown memory file" in res.output


def test_show_pipeline_bundle(runner: CliRunner, project: Path) -> None:
    (project / "pipelines").mkdir()
    (project / "pipelines" / "stripe.md").write_text("stripe sidecar notes\n")
    res = runner.invoke(
        app, ["memory", "show", "--pipeline", "stripe", "--project-dir", str(project)]
    )
    assert res.exit_code == 0, res.output
    assert "stripe sidecar notes" in res.output
    assert "# conventions" in res.output  # bundle includes conventions + standards


def test_append_decision_then_duplicate_then_force(runner: CliRunner, project: Path) -> None:
    base = [
        "memory",
        "append-decision",
        "Retention",
        "--body",
        "18 months",
        "--project-dir",
        str(project),
    ]
    first = runner.invoke(app, [*base, "--reviewers", "alice@,bob@", "--date", "2026-04-12"])
    assert first.exit_code == 0, first.output
    text = (project / "carve" / "decisions.md").read_text()
    assert "## 2026-04-12 — Retention" in text
    assert "**Reviewers:** alice@, bob@" in text

    dup = runner.invoke(app, [*base, "--date", "2026-04-12"])
    assert dup.exit_code == 2, dup.output
    assert "already" in dup.output.lower()

    forced = runner.invoke(app, [*base, "--date", "2026-04-12", "--force"])
    assert forced.exit_code == 0, forced.output


def test_edit_writes_directly(
    runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[Path] = []
    monkeypatch.setattr(memory_cmd, "_launch_editor", lambda p: launched.append(p))
    res = runner.invoke(app, ["memory", "edit", "standards", "--project-dir", str(project)])
    assert res.exit_code == 0, res.output
    assert launched == [project / "carve" / "standards.md"]
    assert "wrote" in res.output.lower()


def test_edit_creates_sidecar_when_saved(
    runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        memory_cmd, "_launch_editor", lambda p: p.write_text("pipeline notes\n", encoding="utf-8")
    )
    res = runner.invoke(
        app, ["memory", "edit", "--pipeline", "newpipe", "--project-dir", str(project)]
    )
    assert res.exit_code == 0, res.output
    sidecar = project / "pipelines" / "newpipe.md"
    assert sidecar.is_file() and sidecar.read_text() == "pipeline notes\n"


def test_edit_abandoned_leaves_no_empty_sidecar(
    runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Editor quits without saving → the brand-new file must not persist empty.
    monkeypatch.setattr(memory_cmd, "_launch_editor", lambda p: None)
    res = runner.invoke(
        app, ["memory", "edit", "--pipeline", "ghost", "--project-dir", str(project)]
    )
    assert res.exit_code == 0, res.output
    assert not (project / "pipelines" / "ghost.md").exists()


def test_edit_requires_exactly_one_target(runner: CliRunner, project: Path) -> None:
    none = runner.invoke(app, ["memory", "edit", "--project-dir", str(project)])
    assert none.exit_code == 2, none.output
    both = runner.invoke(
        app,
        ["memory", "edit", "standards", "--pipeline", "x", "--project-dir", str(project)],
    )
    assert both.exit_code == 2, both.output
