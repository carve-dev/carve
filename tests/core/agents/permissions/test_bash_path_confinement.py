"""Regression tests for the bash sandbox floor (harness iter-2 Highs).

These pin the two High findings closed in iteration 2:

* **No arbitrary file reads via bash.** The path-taking coreutils
  (``cat``/``head``/``tail``/``wc``/``ls``) were removed from the bash
  allow-list — they read arbitrary file *contents* / list arbitrary
  directories, unconfined by the cwd-pin (an absolute path escapes it).
  ``cat .env`` and ``cat /etc/passwd`` must now **deny** in every mode;
  file reads live in the confined ``read_file``/``glob``/``grep`` tools.
* **No arbitrary writes via bash.** ``sort -o <abs>`` and GNU
  ``uniq <in> <out>`` write an operator-chosen path; ``sort``/``uniq``
  were removed too, so they deny in every mode (incl. ``read_only``).
* **``git diff --no-index`` is denied** (it turns ``git diff`` into an
  arbitrary two-file reader) while plain ``git diff``/``status``/
  ``commit`` and ``dbt compile`` stay allowed.

Together these enforce harness.md line 78 ("filesystem read-write only
under the project root … read-only elsewhere") and the line-108
Acceptance clause ("``bash`` cannot read secrets from … files").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.core.agents.m1_tools import make_read_file_tool
from carve.core.agents.permissions.bash_gate import tier_bash_command
from carve.core.agents.permissions.gate import Outcome, PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import (
    _READ_BASH_ALLOW,
    DANGEROUS_BASH_FLAGS,
    build_policy,
)

_ALL_MODES = (
    PermissionMode.READ_ONLY,
    PermissionMode.PLAN,
    PermissionMode.BUILD,
    PermissionMode.DEPLOY,
)


def _rules(mode: PermissionMode):  # type: ignore[no-untyped-def]
    return build_policy(mode).bash


class TestPathTakingCoreutilsDenied:
    """``cat``/``head``/``tail``/``wc``/``ls`` read/list arbitrary paths.

    They are un-allow-listed (not merely omitted): with no path
    confinement on bash, an absolute argument escapes the cwd-pin, so
    these would read any file the process user can. Reads moved to the
    confined ``read_file``/``glob``/``grep`` tools. Denied in **every**
    mode, including the most-privileged ``deploy``.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "cat .env",
            "cat /etc/passwd",
            "cat /Users/someone/.aws/credentials",
            "head -c 100 .env",
            "head /etc/passwd",
            "tail -n 5 .env",
            "tail /etc/hosts",
            "wc -l .env",
            "wc /etc/passwd",
            "ls",
            "ls /",
            "ls -la /etc",
        ],
    )
    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_read_coreutil_denied_every_mode(self, command: str, mode: PermissionMode) -> None:
        decision = tier_bash_command(command, _rules(mode))
        assert decision.tier == "deny", command

    def test_cat_env_denied_end_to_end_read_only(self) -> None:
        # Through the full gate (read_only, no approver): the secret-file
        # read the iter-2 report reproduced now denies.
        gate = PermissionGate(build_policy(PermissionMode.READ_ONLY))
        decision = gate.check("bash", {"command": "cat .env"})
        assert decision.outcome is Outcome.DENY


class TestArbitraryWriteCoreutilsDenied:
    """``sort -o``/``uniq f out`` write an operator-chosen absolute path.

    A write capability in ``read_only`` (the most locked-down mode)
    violates the line-78 floor "read-write only under the project root."
    ``sort``/``uniq`` were removed from the allow-list, so every form
    denies in every mode.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "sort -o /tmp/out f",
            "sort --output=/tmp/out f",
            "sort -o /tmp/out .env",
            "sort f",
            "uniq f /tmp/out",
            "uniq f.txt /tmp/owned.txt",
            "uniq f",
        ],
    )
    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_sort_uniq_denied_every_mode(self, command: str, mode: PermissionMode) -> None:
        decision = tier_bash_command(command, _rules(mode))
        assert decision.tier == "deny", command

    def test_sort_output_denied_end_to_end_read_only(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.READ_ONLY))
        decision = gate.check("bash", {"command": "sort -o /tmp/owned f"})
        assert decision.outcome is Outcome.DENY


class TestTestBuiltinDenied:
    """``test``/``[`` are an arbitrary-path existence probe (info leak)."""

    @pytest.mark.parametrize(
        "command",
        ["test -f /etc/passwd", "test -e /root/.ssh/id_rsa", "[ -f .env ]"],
    )
    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_test_builtin_denied_every_mode(self, command: str, mode: PermissionMode) -> None:
        assert tier_bash_command(command, _rules(mode)).tier == "deny", command


class TestGitNoIndexDenied:
    """``git diff --no-index <a> <b>`` reads two arbitrary files."""

    @pytest.mark.parametrize(
        "command",
        [
            "git diff --no-index a b",
            "git diff --no-index /etc/passwd /etc/hosts",
            "git diff --no-index .env other",
        ],
    )
    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_no_index_denied_every_mode(self, command: str, mode: PermissionMode) -> None:
        # Allow-listed in read tiers via `git diff`, but the flag guard
        # must reject it regardless of mode.
        assert tier_bash_command(command, _rules(mode)).tier == "deny", command

    def test_no_index_in_dangerous_flags(self) -> None:
        assert "--no-index" in DANGEROUS_BASH_FLAGS["git"]

    def test_no_index_denied_end_to_end(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.DEPLOY))
        decision = gate.check("bash", {"command": "git diff --no-index /etc/passwd /etc/hosts"})
        assert decision.outcome is Outcome.DENY


class TestLegitimateCommandsStillAllowed:
    """The fix must not over-block the real CLI surface."""

    def test_plain_git_read_subcommands_allowed(self) -> None:
        rules = _rules(PermissionMode.READ_ONLY)
        for cmd in ("git diff", "git status", "git log", "git show"):
            assert tier_bash_command(cmd, rules).tier == "allow", cmd

    def test_git_commit_allowed_at_build(self) -> None:
        rules = _rules(PermissionMode.BUILD)
        assert tier_bash_command("git commit -m x", rules).tier == "allow"

    def test_git_commit_denied_at_read_only(self) -> None:
        rules = _rules(PermissionMode.READ_ONLY)
        assert tier_bash_command("git commit -m x", rules).tier == "deny"

    def test_dbt_compile_still_allowed(self) -> None:
        rules = _rules(PermissionMode.READ_ONLY)
        assert tier_bash_command("dbt compile", rules).tier == "allow"

    def test_pathless_builtins_allowed(self) -> None:
        rules = _rules(PermissionMode.READ_ONLY)
        for cmd in ("pwd", "echo hello", "date", "which git", "true", "false"):
            assert tier_bash_command(cmd, rules).tier == "allow", cmd


class TestGhIsDeployTierOnly:
    """``gh`` is PR-creation / network — never in the read/plan/build
    allow-lists; only its deploy-tier prompt subcommands are reachable."""

    @pytest.mark.parametrize(
        "command",
        ["gh pr view", "gh pr list", "gh repo view", "gh pr status"],
    )
    @pytest.mark.parametrize(
        "mode",
        [PermissionMode.READ_ONLY, PermissionMode.PLAN, PermissionMode.BUILD],
    )
    def test_gh_read_denied_below_deploy(self, command: str, mode: PermissionMode) -> None:
        assert tier_bash_command(command, _rules(mode)).tier == "deny", command

    def test_gh_pr_create_prompts_at_deploy(self) -> None:
        assert tier_bash_command("gh pr create", _rules(PermissionMode.DEPLOY)).tier == "prompt"

    def test_gh_pr_create_denied_below_deploy(self) -> None:
        assert tier_bash_command("gh pr create", _rules(PermissionMode.BUILD)).tier == "deny"


# Tokens that must never be in the read allow-list (the removed
# path-taking coreutils, the existence probe, and the network ``gh``
# read subcommands). Module-level so it is visible to ``parametrize`` at
# class-body evaluation time.
_FORBIDDEN_READ_ENTRIES = (
    "cat",
    "head",
    "tail",
    "wc",
    "ls",
    "sort",
    "uniq",
    "test",
    "[",
    "gh pr view",
    "gh pr list",
    "gh repo view",
)


class TestAllowListInvariant:
    """Belt-and-braces: pin the *contents* of the read allow-list so a
    future edit can't silently re-admit a path-taking coreutil."""

    @pytest.mark.parametrize("entry", _FORBIDDEN_READ_ENTRIES)
    def test_forbidden_entry_absent_from_read_allow(self, entry: str) -> None:
        assert entry not in _READ_BASH_ALLOW

    def test_every_read_entry_is_pathless_or_flag_guarded(self) -> None:
        # Each entry must be either a known path-less builtin or a
        # subcommand of a flag-guarded multi-purpose tool.
        pathless = {"pwd", "echo", "which", "true", "false", "date", "printenv"}
        guarded = set(DANGEROUS_BASH_FLAGS)  # {"git", "dbt", "dlt", "carve"}
        for entry in _READ_BASH_ALLOW:
            prog = entry.split(" ", 1)[0]
            assert entry in pathless or prog in guarded, (
                f"{entry!r} is neither path-less nor a flag-guarded tool"
            )


class TestConfinedReadStillWorks:
    """Reads weren't broken — they moved off bash onto ``read_file``,
    which enforces the secret deny-list + project-root containment."""

    def test_read_file_under_allowed_paths_succeeds(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
        tool = make_read_file_tool(tmp_path)
        assert tool.executor({"path": "main.py"}) == "print('hi')\n"

    def test_read_file_secret_still_denied(self, tmp_path: Path) -> None:
        from carve.core.agents.tools import ToolExecutionError

        (tmp_path / ".env").write_text("X=secret\n", encoding="utf-8")
        tool = make_read_file_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match=r"credentials|not allowed"):
            tool.executor({"path": ".env"})
