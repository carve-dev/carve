"""Integration tests for the git workspace cache.

Exercises `sync_workspace` / `is_dirty` / `reject_if_dirty` against a real
git remote built from a local **bare** repo in ``tmp_path`` — no network.
Covers: first-call clone, idempotent re-sync, hard-reset after a
force-push, dirty detection, and the reject-if-dirty guard.

*(layout spec Tests: integration bullets 1-2)*
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from carve.core.config.paths import ProjectPaths
from carve.integrations.component_locator import slugify, workspace_dirname
from carve.integrations.workspace_cache import (
    WorkspaceDirtyError,
    WorkspaceSyncError,
    is_dirty,
    reject_if_dirty,
    sync_workspace,
)

# Skip the whole module cleanly if git isn't available on the runner.
pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)

_BRANCH = "main"


def _git(*args: str, cwd: Path) -> str:
    """Run git in ``cwd`` with a deterministic identity; return stdout."""
    env_args = [
        "git",
        "-c",
        "user.email=test@carve.dev",
        "-c",
        "user.name=Carve Test",
        "-c",
        "commit.gpgsign=false",
        "-c",
        f"init.defaultBranch={_BRANCH}",
        *args,
    ]
    proc = subprocess.run(env_args, cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> str:
    """Create a local bare repo with one commit on ``main``; return its URL.

    Builds via a working clone, then pushes into the bare repo so the bare
    repo has the branch ref. The returned URL is the bare repo's path
    (git treats a local path as a valid remote).
    """
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "-b", _BRANCH, str(bare), cwd=tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", _BRANCH, cwd=seed)
    (seed / "README.md").write_text("v1\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "initial", cwd=seed)
    _git("remote", "add", "origin", str(bare), cwd=seed)
    _git("push", "origin", _BRANCH, cwd=seed)
    return str(bare)


@pytest.fixture
def paths(tmp_path: Path) -> ProjectPaths:
    root = tmp_path / "control-plane"
    root.mkdir()
    return ProjectPaths.from_root(root)


def test_first_call_clones(remote: str, paths: ProjectPaths) -> None:
    ws = sync_workspace("analytics", remote, _BRANCH, paths)
    assert ws.is_dir()
    assert (ws / ".git").exists()
    assert (ws / "README.md").read_text() == "v1\n"
    # Lands at the derived cache path.
    assert ws.parent == paths.workspaces_dir
    assert ws.name == f"{slugify(remote)}-{_BRANCH}"


def test_second_call_is_idempotent_and_syncs(remote: str, paths: ProjectPaths) -> None:
    ws1 = sync_workspace("analytics", remote, _BRANCH, paths)
    ws2 = sync_workspace("analytics", remote, _BRANCH, paths)
    assert ws1 == ws2
    assert ws2.is_dir()


def test_sync_pulls_new_commits(remote: str, paths: ProjectPaths, tmp_path: Path) -> None:
    ws = sync_workspace("analytics", remote, _BRANCH, paths)
    assert (ws / "README.md").read_text() == "v1\n"

    # Push a new commit to the remote from a separate working clone.
    work = tmp_path / "work"
    _git("clone", "-b", _BRANCH, remote, str(work), cwd=tmp_path)
    (work / "README.md").write_text("v2\n")
    _git("add", "README.md", cwd=work)
    _git("commit", "-m", "update", cwd=work)
    _git("push", "origin", _BRANCH, cwd=work)

    sync_workspace("analytics", remote, _BRANCH, paths)
    assert (ws / "README.md").read_text() == "v2\n"


def test_hard_reset_after_force_push(remote: str, paths: ProjectPaths, tmp_path: Path) -> None:
    ws = sync_workspace("analytics", remote, _BRANCH, paths)

    # Rewrite history on the remote (force-push a divergent branch).
    work = tmp_path / "work"
    _git("clone", "-b", _BRANCH, remote, str(work), cwd=tmp_path)
    (work / "README.md").write_text("rewritten\n")
    _git("add", "README.md", cwd=work)
    _git("commit", "--amend", "-m", "rewritten initial", cwd=work)
    _git("push", "--force", "origin", _BRANCH, cwd=work)

    sync_workspace("analytics", remote, _BRANCH, paths)
    # Hard sync makes the workspace match the (rewritten) remote exactly.
    assert (ws / "README.md").read_text() == "rewritten\n"


def test_is_dirty_detects_local_modifications(remote: str, paths: ProjectPaths) -> None:
    ws = sync_workspace("analytics", remote, _BRANCH, paths)
    assert is_dirty(ws) is False

    (ws / "README.md").write_text("locally edited\n")
    assert is_dirty(ws) is True


def test_is_dirty_detects_untracked_files(remote: str, paths: ProjectPaths) -> None:
    ws = sync_workspace("analytics", remote, _BRANCH, paths)
    (ws / "scratch.txt").write_text("untracked\n")
    assert is_dirty(ws) is True


def test_reject_if_dirty_blocks_with_message(remote: str, paths: ProjectPaths) -> None:
    ws = sync_workspace("analytics", remote, _BRANCH, paths)
    (ws / "README.md").write_text("dirty\n")
    with pytest.raises(WorkspaceDirtyError) as exc:
        reject_if_dirty(ws)
    msg = str(exc.value)
    assert str(ws) in msg
    assert "commit or discard" in msg.lower()


def test_sync_refuses_to_clobber_dirty_workspace(remote: str, paths: ProjectPaths) -> None:
    ws = sync_workspace("analytics", remote, _BRANCH, paths)
    (ws / "README.md").write_text("uncommitted work\n")
    with pytest.raises(WorkspaceDirtyError):
        sync_workspace("analytics", remote, _BRANCH, paths)
    # The local edit survives — sync didn't blow it away.
    assert (ws / "README.md").read_text() == "uncommitted work\n"


def test_soft_sync_mode_uses_pull(remote: str, paths: ProjectPaths, tmp_path: Path) -> None:
    ws = sync_workspace("analytics", remote, _BRANCH, paths, sync_mode="soft")

    work = tmp_path / "work"
    _git("clone", "-b", _BRANCH, remote, str(work), cwd=tmp_path)
    (work / "NEW.md").write_text("added\n")
    _git("add", "NEW.md", cwd=work)
    _git("commit", "-m", "add new", cwd=work)
    _git("push", "origin", _BRANCH, cwd=work)

    sync_workspace("analytics", remote, _BRANCH, paths, sync_mode="soft")
    assert (ws / "NEW.md").read_text() == "added\n"


# ---------------------------------------------------------------------------
# Security hardening (layout slice security review): git argument injection
# ---------------------------------------------------------------------------


def test_clone_with_option_shaped_url_does_not_execute_payload(
    paths: ProjectPaths, tmp_path: Path
) -> None:
    """An option-shaped ``url`` can never be parsed as a git flag.

    Without the end-of-options ``--`` separator in ``_clone``, a ``url``
    like ``--upload-pack=<cmd>`` could be read as the ``--upload-pack``
    option and run ``<cmd>``. With ``--`` it is forced to be the (bogus)
    positional repo, so git fails to clone and the payload never runs.
    """
    sentinel = tmp_path / "PWNED"
    malicious_url = f"--upload-pack=touch {sentinel}"
    with pytest.raises(WorkspaceSyncError):
        sync_workspace("evil", malicious_url, _BRANCH, paths)
    assert not sentinel.exists(), "git --upload-pack payload executed"


@pytest.mark.parametrize(
    ("branch", "ref"),
    [("--orphan=pwn", None), ("main", "--orphan=pwn")],
    ids=["branch", "ref"],
)
def test_option_shaped_ref_or_branch_rejected_before_checkout(
    paths: ProjectPaths, branch: str, ref: str | None
) -> None:
    """An option-shaped `ref`/`branch` is rejected before any git runs.

    `git checkout --orphan=pwn` exits 0 and creates branch `pwn` (option
    injection — git parses it as a flag, and `git checkout <value> --` does
    NOT neutralize it). `sync_workspace` must raise rather than run the
    checkout and desync the workspace.
    """
    with pytest.raises(WorkspaceSyncError):
        sync_workspace("evil", "https://example.invalid/r.git", branch, paths, ref=ref)


def test_git_subprocess_uses_hardened_env(
    remote: str, paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every git invocation carries the pinned safety env.

    ``GIT_TERMINAL_PROMPT=0`` (never block on a credential prompt) +
    ``GIT_ALLOW_PROTOCOL`` (a transport allow-list git config can't widen),
    while still inheriting the ambient environment (``PATH``).
    """
    import carve.integrations.workspace_cache as wc

    seen: list[dict[str, str]] = []
    real_run = subprocess.run

    def _spy(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "git":
            env = kwargs.get("env")
            seen.append(dict(env) if isinstance(env, dict) else {})
        return real_run(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(wc.subprocess, "run", _spy)
    sync_workspace("analytics", remote, _BRANCH, paths)

    assert seen, "no git subprocess was invoked"
    for env in seen:
        assert env.get("GIT_TERMINAL_PROMPT") == "0"
        assert env.get("GIT_ALLOW_PROTOCOL") == "https:ssh:git:file"
        assert "PATH" in env  # ambient environment still inherited


def test_git_timeout_raises_workspace_sync_error(
    paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git operation that exceeds the timeout is reported as unreachable.

    An unreachable / black-holed remote must not hang the calling thread
    forever: ``_run_git`` passes a bounded ``timeout=`` to ``subprocess.run``
    and turns the resulting ``TimeoutExpired`` into a ``WorkspaceSyncError``.
    """
    import carve.integrations.workspace_cache as wc

    def _timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=0.01)

    monkeypatch.setattr(wc.subprocess, "run", _timeout)
    with pytest.raises(WorkspaceSyncError) as exc:
        sync_workspace("stuck", "https://example.invalid/repo.git", _BRANCH, paths, timeout=0.01)
    msg = str(exc.value).lower()
    assert "timed out" in msg or "unreachable" in msg


def test_clone_with_ref_lands_at_locator_path_and_checks_out_pin(
    remote: str, paths: ProjectPaths
) -> None:
    """A `ref` pin lands where the locator points and checks out the revision.

    Regression for the layout review: `sync_workspace` previously derived the
    dir from the branch only and had no `ref` parameter, so a component with
    both `ref` and `branch` cloned to `<slug>-<branch>` while the locator
    resolved to `<slug>-<ref>` — a silent "code not found". Now both go
    through `workspace_dirname`, and a pin is checked out at the SHA.
    """
    sha = _git("rev-parse", _BRANCH, cwd=Path(remote))
    ws = sync_workspace("analytics", remote, _BRANCH, paths, ref=sha)
    # Lands at the ref-keyed dir the locator resolves to, NOT the branch dir.
    assert ws.name == workspace_dirname(remote, sha, _BRANCH)
    assert ws.name != f"{slugify(remote)}-{_BRANCH}"
    # Checked out at the pinned commit (detached HEAD).
    assert _git("rev-parse", "HEAD", cwd=ws) == sha
