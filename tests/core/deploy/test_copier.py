"""Unit tests for ``carve.core.deploy.copier``.

The git-status guard is exercised against a real `git init` repo in
``tmp_path``; everything else uses plain filesystem assertions.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from carve.core.deploy.copier import (
    UncommittedChangesError,
    UnsafeArtifactError,
    _git_uncommitted_paths,
    copy_artifact,
    copy_ddl_file,
)

# Skip git-dependent tests if git isn't on PATH (CI guard rail).
_GIT_AVAILABLE = shutil.which("git") is not None
git_required = pytest.mark.skipif(
    not _GIT_AVAILABLE,
    reason="git not available on PATH",
)


def _plant_artifact(project_dir: Path, target: str, name: str) -> Path:
    artifact = project_dir / "targets" / target / "el" / name
    artifact.mkdir(parents=True)
    (artifact / "main.py").write_text("print('hello')\n")
    (artifact / "requirements.txt").write_text("requests\n")
    return artifact


def _plant_ddl(project_dir: Path, target: str, name: str) -> Path:
    snowflake_dir = project_dir / "targets" / target / "snowflake"
    snowflake_dir.mkdir(parents=True)
    sql_path = snowflake_dir / f"{name}.sql"
    sql_path.write_text("CREATE TABLE foo (id INT);")
    return sql_path


def _git_init_clean(project_dir: Path) -> None:
    """Initialize a git repo with all files committed."""
    subprocess.run(
        ["git", "init", "-q"], cwd=project_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=project_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# copy_artifact
# ---------------------------------------------------------------------------


def test_copy_artifact_creates_destination(tmp_path: Path) -> None:
    _plant_artifact(tmp_path, "dev", "iowa")
    files = copy_artifact(
        project_dir=tmp_path,
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        check_git=False,
    )
    dst = tmp_path / "targets" / "prod" / "el" / "iowa"
    assert dst.is_dir()
    assert (dst / "main.py").is_file()
    assert any("targets/prod/el/iowa/main.py" in f for f in files)


def test_copy_artifact_idempotent(tmp_path: Path) -> None:
    _plant_artifact(tmp_path, "dev", "iowa")
    copy_artifact(
        project_dir=tmp_path,
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        check_git=False,
    )
    # Re-run; should overwrite without error.
    copy_artifact(
        project_dir=tmp_path,
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        check_git=False,
    )
    dst = tmp_path / "targets" / "prod" / "el" / "iowa"
    assert (dst / "main.py").read_text() == "print('hello')\n"


def test_copy_artifact_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        copy_artifact(
            project_dir=tmp_path,
            pipeline_name="absent",
            source_target="dev",
            dest_target="prod",
            check_git=False,
        )


@git_required
def test_copy_artifact_refuses_uncommitted_destination(tmp_path: Path) -> None:
    _plant_artifact(tmp_path, "dev", "iowa")
    _plant_artifact(tmp_path, "prod", "iowa")
    _git_init_clean(tmp_path)

    # Modify the destination's main.py so git status flags it.
    (tmp_path / "targets" / "prod" / "el" / "iowa" / "main.py").write_text(
        "print('user edits')\n"
    )

    with pytest.raises(UncommittedChangesError) as excinfo:
        copy_artifact(
            project_dir=tmp_path,
            pipeline_name="iowa",
            source_target="dev",
            dest_target="prod",
        )
    assert any("iowa" in p for p in excinfo.value.paths)


@git_required
def test_copy_artifact_clean_destination_proceeds(tmp_path: Path) -> None:
    _plant_artifact(tmp_path, "dev", "iowa")
    _plant_artifact(tmp_path, "prod", "iowa")
    _git_init_clean(tmp_path)

    # Modify the source file. Destination is clean.
    (tmp_path / "targets" / "dev" / "el" / "iowa" / "main.py").write_text(
        "print('updated source')\n"
    )

    # No exception expected.
    copy_artifact(
        project_dir=tmp_path,
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
    )
    assert (
        (tmp_path / "targets" / "prod" / "el" / "iowa" / "main.py").read_text()
        == "print('updated source')\n"
    )


# ---------------------------------------------------------------------------
# copy_ddl_file
# ---------------------------------------------------------------------------


def test_copy_ddl_file_copies(tmp_path: Path) -> None:
    _plant_ddl(tmp_path, "dev", "iowa")
    rel = copy_ddl_file(
        project_dir=tmp_path,
        pipeline_name="iowa",
        source_target="dev",
        dest_target="prod",
        check_git=False,
    )
    assert rel == "targets/prod/snowflake/iowa.sql"
    dest = tmp_path / "targets" / "prod" / "snowflake" / "iowa.sql"
    assert dest.read_text() == "CREATE TABLE foo (id INT);"


def test_copy_ddl_file_returns_none_when_source_missing(tmp_path: Path) -> None:
    rel = copy_ddl_file(
        project_dir=tmp_path,
        pipeline_name="absent",
        source_target="dev",
        dest_target="prod",
        check_git=False,
    )
    assert rel is None


@git_required
def test_copy_ddl_file_refuses_uncommitted(tmp_path: Path) -> None:
    _plant_ddl(tmp_path, "dev", "iowa")
    _plant_ddl(tmp_path, "prod", "iowa")
    _git_init_clean(tmp_path)
    (tmp_path / "targets" / "prod" / "snowflake" / "iowa.sql").write_text(
        "-- modified after commit\n"
    )
    with pytest.raises(UncommittedChangesError):
        copy_ddl_file(
            project_dir=tmp_path,
            pipeline_name="iowa",
            source_target="dev",
            dest_target="prod",
        )


# ---------------------------------------------------------------------------
# Symlink rejection (security: source artifact tree)
# ---------------------------------------------------------------------------


def test_copier_refuses_symlinks_in_source(tmp_path: Path) -> None:
    """A symlink in the source artifact tree aborts the copy."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / "secret.txt"
    secret.write_text("secret-content\n")

    artifact = tmp_path / "targets" / "dev" / "el" / "iowa"
    artifact.mkdir(parents=True)
    (artifact / "main.py").write_text("print('hi')\n")
    # Place a symlink inside the artifact dir pointing at the secret.
    link = artifact / "linked.txt"
    link.symlink_to(secret)

    with pytest.raises(UnsafeArtifactError) as excinfo:
        copy_artifact(
            project_dir=tmp_path,
            pipeline_name="iowa",
            source_target="dev",
            dest_target="prod",
            check_git=False,
        )
    assert "linked.txt" in str(excinfo.value)
    # Critically, the secret content must NOT have been written to the dest.
    dst = tmp_path / "targets" / "prod" / "el" / "iowa" / "linked.txt"
    assert not dst.exists()


def test_copier_refuses_symlinked_subdir_in_source(tmp_path: Path) -> None:
    """A symlinked *directory* in the source tree also aborts."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("x")

    artifact = tmp_path / "targets" / "dev" / "el" / "iowa"
    artifact.mkdir(parents=True)
    (artifact / "main.py").write_text("print('hi')\n")
    (artifact / "linked_dir").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(UnsafeArtifactError):
        copy_artifact(
            project_dir=tmp_path,
            pipeline_name="iowa",
            source_target="dev",
            dest_target="prod",
            check_git=False,
        )


# ---------------------------------------------------------------------------
# Git status guard (distinguishes "no repo" from "git failure")
# ---------------------------------------------------------------------------


def test_git_guard_no_repo_returns_empty(tmp_path: Path) -> None:
    """Outside a repo the guard returns ``[]`` (no opinion)."""
    # tmp_path is fresh — no .git anywhere.
    paths = _git_uncommitted_paths(tmp_path, tmp_path / "anything")
    assert paths == []


@git_required
def test_git_guard_repo_with_git_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If git itself returns non-zero inside a repo, raise — fail closed."""
    # Need at least one file for the initial commit.
    (tmp_path / "README").write_text("test\n")
    _git_init_clean(tmp_path)
    # Stub subprocess.run to simulate git breaking.
    from typing import Any

    def _fake_run(
        *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else []
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=128,
            stdout="",
            stderr="fatal: index file is corrupt",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(RuntimeError) as excinfo:
        _git_uncommitted_paths(tmp_path, tmp_path / "targets")
    assert "git status" in str(excinfo.value)
    assert "corrupt" in str(excinfo.value)
