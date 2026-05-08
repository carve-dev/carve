"""File-copy logic for ``carve el deploy`` (Phase 5).

Promotes ``targets/<source>/el/<name>/`` and
``targets/<source>/snowflake/<name>.sql`` into the equivalent
destination paths. Two safety rails:

1. Refuse if the destination paths already have **uncommitted** git
   changes — overwriting a hand-edit a user forgot to commit is the
   highest-cost regression we can ship. Checked via
   ``git status --porcelain`` against each destination path.
2. ``shutil.copytree`` with ``dirs_exist_ok=True`` so re-running on a
   destination that already mirrors the source is a no-op (idempotent
   per the deploy contract).

The git check uses the project's working tree only — repos that don't
init git get a pass with a logged note. The deploy command in CI
typically runs in a freshly-cloned tree where this guard is a no-op
anyway; the guard exists for the local-dev case.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class UncommittedChangesError(Exception):
    """Raised when the destination has uncommitted git changes."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(
            "Uncommitted changes in destination: "
            + ", ".join(paths)
            + ". Commit or stash before deploying."
        )


class UnsafeArtifactError(Exception):
    """Raised when the source artifact tree contains a symlink.

    Symlinks have no legitimate use in an EL artifact tree;
    ``shutil.copytree``'s default ``symlinks=False`` would *follow*
    the link and copy whatever's behind it (including paths outside
    the project root). We refuse rather than read.
    """


@dataclass
class CopyResult:
    """Files written by `copy_artifact` + `copy_ddl_file`."""

    artifact_files: list[str] = field(default_factory=list)
    ddl_file: str | None = None


def copy_artifact(
    *,
    project_dir: Path,
    pipeline_name: str,
    source_target: str,
    dest_target: str,
    check_git: bool = True,
) -> list[str]:
    """Copy ``targets/<source>/el/<name>/`` into ``targets/<dest>/el/<name>/``.

    Returns the destination-relative file list (POSIX-style) for the
    copy result. Raises:

    * ``FileNotFoundError`` if the source artifact directory is
      missing (this normally fails earlier in deploy validation, but
      defending here keeps the copier callable in isolation).
    * ``UncommittedChangesError`` when ``check_git`` is true and the
      destination tree has uncommitted edits in the artifact dir.
    """
    source_dir = project_dir / "targets" / source_target / "el" / pipeline_name
    dest_dir = project_dir / "targets" / dest_target / "el" / pipeline_name

    if not source_dir.is_dir():
        raise FileNotFoundError(
            f"source artifact directory missing: {source_dir}"
        )

    # Walk the source tree before any copy to ensure no symlinks are
    # present. ``shutil.copytree(symlinks=False)`` (the default) would
    # *follow* a symlink and copy the file behind it — that's how a
    # malicious link to ``~/.aws/credentials`` would land in the dest
    # tree.
    _reject_symlinks_in(source_dir)

    if check_git and dest_dir.exists():
        dirty = _git_uncommitted_paths(project_dir, dest_dir)
        if dirty:
            raise UncommittedChangesError(dirty)

    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, dest_dir, dirs_exist_ok=True)

    # Return the list of files now on disk under the destination, in
    # project-relative POSIX form. Used by the caller for logs and
    # diagnostics; the deploy Run row's manifest is built downstream.
    written: list[str] = []
    for path in sorted(dest_dir.rglob("*")):
        if path.is_file():
            try:
                rel = path.relative_to(project_dir)
            except ValueError:
                # Symlink that escapes the project root; skip rather
                # than fail. The copier itself doesn't follow links
                # but the rglob above does, so keep the safety net.
                continue
            written.append(rel.as_posix())
    return written


def copy_ddl_file(
    *,
    project_dir: Path,
    pipeline_name: str,
    source_target: str,
    dest_target: str,
    check_git: bool = True,
) -> str | None:
    """Copy ``targets/<source>/snowflake/<name>.sql`` to its destination.

    Returns the destination-relative path on success, or ``None`` when
    the source DDL file doesn't exist (some pipelines may legitimately
    not emit one). Raises ``UncommittedChangesError`` if the
    destination DDL has uncommitted edits.
    """
    source_path = (
        project_dir / "targets" / source_target / "snowflake" / f"{pipeline_name}.sql"
    )
    dest_path = (
        project_dir / "targets" / dest_target / "snowflake" / f"{pipeline_name}.sql"
    )

    if not source_path.is_file():
        return None

    if check_git and dest_path.exists():
        dirty = _git_uncommitted_paths(project_dir, dest_path)
        if dirty:
            raise UncommittedChangesError(dirty)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest_path)
    return dest_path.relative_to(project_dir).as_posix()


def _reject_symlinks_in(source_dir: Path) -> None:
    """Walk ``source_dir`` and raise if any path is a symlink.

    Walks the directory entries recursively without following links,
    so a circular symlink can't trap us. Raises
    :class:`UnsafeArtifactError` with the offending path on the first
    symlink encountered.
    """
    # Path.rglob follows symlinks by default in CPython, so walk
    # manually to keep symlinks visible. We use os.scandir for the
    # ``is_symlink`` cheap-check (no stat).
    import os

    stack: list[Path] = [source_dir]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            # If scandir fails the copytree below will too — let the
            # original error surface there with full context.
            continue
        for entry in entries:
            entry_path = Path(entry.path)
            if entry.is_symlink():
                raise UnsafeArtifactError(
                    f"Symlink found in source artifact: {entry_path}. "
                    "Symlinks are not permitted in EL artifact trees."
                )
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry_path)


def _is_inside_git_repo(project_dir: Path) -> bool:
    """Return True if ``project_dir`` (or an ancestor) contains a ``.git``.

    Walks parent directories up to the filesystem root. Repositories
    initialized with worktrees use a ``.git`` *file* rather than a
    directory; either form counts.
    """
    candidate = project_dir
    while True:
        git_marker = candidate / ".git"
        if git_marker.exists():
            return True
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent


def _git_uncommitted_paths(project_dir: Path, target_path: Path) -> list[str]:
    """Return the paths under ``target_path`` that show up in `git status --porcelain`.

    Behavior:

    * If ``project_dir`` is **not** inside a git repository, returns
      ``[]`` and logs a debug note. This is the legitimate "no
      opinion" case (a freshly-cloned tarball, a CI image without
      ``.git``).
    * If git itself is missing on PATH, raises so the caller can
      surface a clear error — fail-closed rather than silently
      bypassing the safety rail.
    * If git **is** installed and the repo exists but git returns
      non-zero (lock file held, corrupted index, etc.), raises with
      the stderr surfaced — also fail-closed.
    """
    if not _is_inside_git_repo(project_dir):
        logger.debug(
            "git uncommitted check skipped: %s is not inside a git repo",
            project_dir,
        )
        return []

    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--", str(target_path)],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        # git is missing or the subprocess itself failed; we're inside
        # a repo, so we shouldn't silently disengage the rail.
        raise RuntimeError(
            f"git status check failed inside a git repo: {exc}. "
            "Ensure git is installed on PATH or run from outside the repo."
        ) from exc

    if completed.returncode != 0:
        # We're inside a repo and git returned non-zero — this is a
        # genuine failure (lock file held, broken index, etc.). Fail
        # closed; the user can stash or commit and re-run.
        raise RuntimeError(
            f"git status --porcelain failed (exit {completed.returncode}): "
            f"{completed.stderr.strip()}"
        )

    dirty: list[str] = []
    for raw_line in completed.stdout.splitlines():
        # Porcelain v1 lines are `XY <path>` where X+Y are status
        # codes. We don't filter further — any non-empty entry
        # under the destination is a reason to refuse.
        if len(raw_line) < 4:
            continue
        path = raw_line[3:].strip()
        if path:
            dirty.append(path)
    return dirty


__all__ = [
    "CopyResult",
    "UncommittedChangesError",
    "UnsafeArtifactError",
    "copy_artifact",
    "copy_ddl_file",
]
