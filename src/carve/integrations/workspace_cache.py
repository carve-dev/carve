"""Git workspace cache for ``separate-remote`` components.

A ``separate-remote`` component's code lives in a git repo at a ``url``.
Carve clones it into ``<root>/.carve/workspaces/<derived-name>/`` (the
cache is gitignored so users never commit a clone of their dbt/dlt repo
into their control-plane repo) and keeps it synced to the tracked branch.

This module exposes the sync *primitives*; the three sync triggers
(``carve serve`` startup, before each pipeline run, before
``carve deploy``) are wired by their owning capabilities — there are no
trigger call sites here.

Path math (the derived workspace dir name) lives in the component
locator; this module imports :func:`workspace_dirname` from there rather
than recomputing it, so resolution and caching agree on where a
component lands.

All git operations shell out via :mod:`subprocess` with an explicit,
escaped argument list (never ``shell=True``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from carve.integrations.component_locator import workspace_dirname

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths

# Default branch name used when a caller doesn't specify one. The remote's
# real default may differ; callers that care pass an explicit branch.
_DEFAULT_BRANCH = "main"

# Bound on every git subprocess so an unreachable / black-holed remote can't
# hang the calling thread forever; a clone/fetch exceeding this is treated as
# unreachable. The sync triggers (wired by their owning capabilities) will
# source this from ``runner.toml`` and pass it via ``sync_workspace(timeout=…)``;
# until then it is the floor default.
_DEFAULT_GIT_TIMEOUT_SECONDS = 300.0


class WorkspaceSyncError(RuntimeError):
    """A git operation against a workspace failed.

    Wraps the failing command and its stderr so the CLI can show the user
    what went wrong (auth failure, unreachable host, bad branch, …).
    """

    def __init__(self, message: str, *, stderr: str | None = None) -> None:
        self.stderr = stderr
        full = message if not stderr else f"{message}\n{stderr.strip()}"
        super().__init__(full)


class WorkspaceDirtyError(RuntimeError):
    """The workspace has uncommitted or untracked changes.

    Raised by :func:`reject_if_dirty` before a sync would discard them.
    The message points the user at the workspace path and tells them to
    commit/discard or take the workspace out of cache management.
    """


def _reject_option_shaped(field: str, value: str | None) -> None:
    """Reject an option-shaped ``ref``/``branch`` before ``git checkout``.

    A leading ``-`` (e.g. ``--orphan=…``) is parsed by git as a flag, not a
    revision — option injection. ``git checkout <value> --`` does **not**
    neutralize it (git parses the option before the trailing ``--``;
    verified on git 2.52), so the value must be rejected, not escaped. This
    mirrors the config-layer validator (``ComponentConfig._safe_ref_branch``)
    as a cache-layer guard for direct callers of :func:`sync_workspace`.
    """
    if value is not None and value.startswith("-"):
        raise WorkspaceSyncError(
            f"unsafe {field} {value!r}: a revision must not start with '-' "
            "(an option-shaped value would be parsed as a git flag)."
        )


def sync_workspace(
    name: str,
    url: str,
    branch: str | None,
    paths: ProjectPaths,
    *,
    ref: str | None = None,
    sync_mode: str = "hard",
    timeout: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
) -> Path:
    """Clone-or-sync a ``separate-remote`` component's workspace.

    Idempotent. The on-disk dir is
    ``<root>/.carve/workspaces/<workspace_dirname(url, ref, branch)>/`` —
    the **same** path the component locator resolves to, so resolution and
    caching always agree (the dir is keyed on ``ref or branch``).

    ``ref`` (a commit SHA or tag) is a **pin** and wins over ``branch``:

    * **First call** (dir absent): clone, then for a pinned ``ref`` check
      it out (detached HEAD at the SHA/tag); for a branch, clone
      ``--branch <branch>``.
    * **Subsequent calls**, pinned ``ref``: reject if dirty, ``git fetch``
      then re-check-out the pin (immutable — no reset).
    * **Subsequent calls**, branch + ``sync_mode="hard"`` (default): reject
      if dirty, ``git fetch origin`` → ``git checkout <branch>`` →
      ``git reset --hard origin/<branch>``.
    * **Subsequent calls**, branch + ``sync_mode="soft"``: reject if dirty,
      ``git pull`` (preserves local commits; footgun-prone, opt-in).

    Returns the absolute workspace path. Raises :class:`WorkspaceSyncError`
    on any git failure (including a hang against an unreachable remote,
    bounded by ``timeout`` seconds) and :class:`WorkspaceDirtyError` if the
    existing workspace has local modifications. ``name`` is used only in
    error messages.
    """
    _reject_option_shaped("ref", ref)
    _reject_option_shaped("branch", branch)
    workspace_path = paths.workspaces_dir / workspace_dirname(url, ref, branch)

    if not _is_git_repo(workspace_path):
        _clone(name, url, ref, branch, workspace_path, timeout=timeout)
        return workspace_path

    # Existing clone: never blow away local edits silently.
    reject_if_dirty(workspace_path)
    if ref is not None:
        # Pinned revision: immutable, so re-assert the checkout (no reset).
        _run_git(
            ["fetch", "origin", "--tags"],
            cwd=workspace_path,
            name=name,
            timeout=timeout,
        )
        _run_git(["checkout", ref], cwd=workspace_path, name=name, timeout=timeout)
    elif sync_mode == "soft":
        _run_git(["pull"], cwd=workspace_path, name=name, timeout=timeout)
    else:
        effective_branch = branch or _DEFAULT_BRANCH
        _run_git(["fetch", "origin"], cwd=workspace_path, name=name, timeout=timeout)
        _run_git(
            ["checkout", effective_branch],
            cwd=workspace_path,
            name=name,
            timeout=timeout,
        )
        _run_git(
            ["reset", "--hard", f"origin/{effective_branch}"],
            cwd=workspace_path,
            name=name,
            timeout=timeout,
        )
    return workspace_path


def is_dirty(workspace_path: Path) -> bool:
    """Return ``True`` if the workspace has local modifications.

    Uses ``git status --porcelain``: any line of output means uncommitted
    changes or untracked files (the workspace's own ``.gitignore`` already
    suppresses ignored entries, so they don't count as dirty). Returns
    ``False`` for a clean tree. Raises :class:`WorkspaceSyncError` if the
    path isn't a git repo or git fails.
    """
    result = _run_git(["status", "--porcelain"], cwd=workspace_path, name=None)
    return bool(result.strip())


def reject_if_dirty(workspace_path: Path) -> None:
    """Raise :class:`WorkspaceDirtyError` if the workspace is dirty.

    Called before every sync so a hard reset / pull never silently
    discards a user's local edits. The message tells the user exactly
    where the workspace is and how to proceed.
    """
    if is_dirty(workspace_path):
        raise WorkspaceDirtyError(
            f"Workspace at {workspace_path} has uncommitted or untracked "
            "changes; refusing to sync (it would discard them).\n"
            "  Commit or discard the changes, or take the workspace out of "
            "Carve's cache management, then retry."
        )


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _is_git_repo(path: Path) -> bool:
    """True if ``path`` exists and is the top of a git working tree."""
    if not path.is_dir():
        return False
    return (path / ".git").exists()


def _clone(
    name: str,
    url: str,
    ref: str | None,
    branch: str | None,
    workspace_path: Path,
    *,
    timeout: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
) -> None:
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    # `--` ends option parsing throughout: an option-shaped `url`
    # (e.g. `--upload-pack=<cmd>`) can never be read as a git flag.
    if ref is not None:
        # Pin: a full clone (so the SHA/tag is reachable) + a detached
        # checkout of the exact revision.
        _run_git(
            ["clone", "--", url, str(workspace_path)],
            cwd=workspace_path.parent,
            name=name,
            timeout=timeout,
        )
        _run_git(["checkout", ref], cwd=workspace_path, name=name, timeout=timeout)
    else:
        effective_branch = branch or _DEFAULT_BRANCH
        _run_git(
            ["clone", "--branch", effective_branch, "--", url, str(workspace_path)],
            cwd=workspace_path.parent,
            name=name,
            timeout=timeout,
        )


def _git_env() -> dict[str, str]:
    """Hardened environment for every git subprocess.

    Inherits the ambient environment but pins two safety knobs:

    * ``GIT_TERMINAL_PROMPT=0`` — never block on an interactive credential
      prompt; a private/bad ``url`` fails fast instead of hanging a worker.
    * ``GIT_ALLOW_PROTOCOL`` — pin the transport allow-list so a global
      ``protocol.*.allow`` git config can't widen what Carve fetches over
      (belt-and-braces with the ``url`` scheme validation at config load).
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh:git:file"
    return env


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    name: str | None,
    timeout: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
) -> str:
    """Run ``git <args>`` in ``cwd`` and return stdout.

    Raises :class:`WorkspaceSyncError` on a non-zero exit (or if ``git``
    isn't on PATH), wrapping stderr. ``name`` is woven into the error
    message when present. A run exceeding ``timeout`` seconds is killed and
    reported as an unreachable / unresponsive remote.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=_git_env(),
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # git not installed
        raise WorkspaceSyncError(
            "`git` was not found on PATH; it is required for separate-remote "
            "components."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        subject = f"component {name!r}" if name else f"workspace {cwd}"
        raise WorkspaceSyncError(
            f"git {args[0]} for {subject} timed out after {timeout:.0f}s; "
            "the remote is unreachable or unresponsive."
        ) from exc
    if proc.returncode != 0:
        subject = f"component {name!r}" if name else f"workspace {cwd}"
        raise WorkspaceSyncError(
            f"git {args[0]} failed for {subject} (exit {proc.returncode}).",
            stderr=proc.stderr,
        )
    return proc.stdout


__all__ = [
    "WorkspaceDirtyError",
    "WorkspaceSyncError",
    "is_dirty",
    "reject_if_dirty",
    "sync_workspace",
]
