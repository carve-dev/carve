"""Per-mode permission policy — the authoritative hardcoded floor.

The policy answers three questions for a given :class:`PermissionMode`:

1. **Which tools are permitted at all** (the mode's permitted tool set).
2. **How is each ``bash`` command tiered** — allow / prompt / deny — by
   ``argv[0]`` (and subcommand, for ``git``/``dbt``/``dlt``/``gh``).
3. **What is the effective policy** once an agent's ``tools:`` grant and
   ``allowed_paths`` and an optional ``[permissions]`` config block are
   layered on — **always tighten, never widen**:
   ``effective = mode-default ∩ config ∩ agent``.

The mode defaults here are *the* security floor — they are Python, not
config, precisely because config and agent files are editable and so can
never be the boundary (a user file overriding a built-in must not be able
to widen authority). The optional ``[permissions]`` ``runtime.toml`` block
parsed by :class:`PermissionsConfig` can only *intersect* the floor; it is
deferred in depth per the spec's open question and ships as a tighten-only
overlay.

This module knows nothing about *executing* anything; it is pure policy
computed up front. The gate (`gate.py`) consumes an :class:`EffectivePolicy`
and decides each call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from carve.core.agents.permissions.modes import PermissionMode

# ---------------------------------------------------------------------------
# Tool taxonomy
# ---------------------------------------------------------------------------

# Tools that mutate the project tree or the warehouse. These are denied
# below `build` *regardless of grant* — the central invariant. `bash` is
# NOT in this set: bash is gated per-command (a read-only `git status` is
# fine in `read_only`); the bash gate (`bash_gate.py`) is what denies
# bash *writes* by tiering write subcommands as deny below build.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "edit",
        "create_file",
        "write_file",
        "run_snowflake_ddl",
    }
)

# The full set of terminal-grade + harness tool names the harness knows.
# A mode's permitted set is a subset of this. `submit_result` and other
# terminator tools are always permitted (they only capture a payload);
# they're added per-agent by the runner, not gated here.
_ALL_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "glob",
        "grep",
        "web_fetch",
        "web_search",
        "todo",
        "run_snowflake_query",
        "bash",
        "edit",
        "create_file",
        "write_file",
        "run_snowflake_ddl",
        "delegate",
    }
)

# Read/inspect tools available in every mode, including `read_only`.
_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "glob",
        "grep",
        "web_fetch",
        "web_search",
        "todo",
        "run_snowflake_query",
        "bash",  # gated per-command; read-only bash is allowed in read_only
        "delegate",
    }
)


# ---------------------------------------------------------------------------
# Bash tiering tables (per mode)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BashRules:
    """Allow / prompt / deny tiering for ``bash`` ``argv[0]`` tokens.

    Each set holds either a bare program name (``"pwd"``) or a
    ``"<prog> <subcommand>"`` pair for the four tools whose subcommand
    determines whether the call writes (``"git status"`` vs
    ``"git push"``). The bash gate (`bash_gate.py`) matches the parsed
    argv against these in deny→prompt→allow precedence after the
    metacharacter screen.

    A command matching nothing falls through to ``default`` (typically
    ``deny`` — the floor is an allowlist, not a denylist).
    """

    allow: frozenset[str]
    prompt: frozenset[str]
    deny: frozenset[str]
    default: str = "deny"


# Programs that are safe to run in any mode. Subcommand-bearing tools
# list only their read subcommands here.
#
# SECURITY — every entry here is reachable in ``read_only`` (the
# explorer/``ask`` floor). The hard rule, in every mode, is that an
# allow-listed ``argv[0]`` must NOT be able to:
#
#   1. run an *attacker-chosen program* (arbitrary-code-execution),
#   2. reach the network (egress), **or**
#   3. read arbitrary file *contents* / write *outside the project root*.
#
# (1) and (2) ruled out interpreters / package managers / ``find`` /
# shell-out multiplexers / ``curl``-family — all in ``_ALWAYS_DENY`` so a
# future allow-edit cannot silently re-admit them.
#
# (3) is the line-78 sandbox floor ("filesystem read-write only under the
# project root … read-only elsewhere") and the line-108 Acceptance clause
# ("``bash`` cannot read secrets from files"). ``bash`` runs the real
# ``dlt``/``dbt``/``git``/``gh`` CLIs; it is **not** a file browser. The
# argv-allowlist alone cannot express "confined to the project root" (an
# absolute path argument escapes the cwd-pin — ``cwd`` is not a chroot),
# so the **only** safe path-touching commands here are the multi-purpose
# tools that confine themselves to the repo/project by construction
# (``git`` operates on the repo at ``cwd``; ``dbt``/``dlt`` on the project
# at ``cwd``) **and** whose repoint/exec/arbitrary-path flags are denied
# by ``DANGEROUS_BASH_FLAGS`` in the bash gate (so they cannot be aimed at
# ``/etc`` or ``--no-index`` two arbitrary files).
#
# That is why the path-taking coreutils were **removed** from this set:
# ``cat``/``head``/``tail``/``wc`` read arbitrary file contents (incl.
# ``cat .env`` and ``cat /abs/.aws/credentials``), ``ls`` lists arbitrary
# directories (``ls /``), ``sort -o``/``uniq f out`` *write* arbitrary
# absolute paths, and ``test``/``[`` are an arbitrary-path existence probe
# (an info leak) — none of them confined by the cwd-pin. File
# reading/listing/search is the dedicated ``read_file``/``glob``/``grep``
# tools, which already enforce the secret deny-list + project-root +
# symlink containment (``m1_tools``/``search_tools``). Reads are not lost;
# they move to the confined tools.
#
# What remains is therefore exactly two kinds of entry:
#   (a) flagless **and** path-less builtins/utilities — ``echo``/``pwd``/
#       ``date``/``true``/``false``/``which``/``printenv`` (``printenv``
#       is safe because the env is credential-scrubbed at the subprocess
#       boundary), and
#   (b) the flag-guarded, project-scoped multi-purpose tools
#       (``git``/``dbt``/``dlt`` read subcommands). ``gh`` is *not* here —
#       it is PR-creation / network and lives in the deploy-tier prompt
#       set only.
_READ_BASH_ALLOW: frozenset[str] = frozenset(
    {
        # (a) flagless, path-less builtins/utilities.
        "pwd",
        "echo",
        "which",
        "true",
        "false",
        "date",
        "printenv",
        # (b) flag-guarded, project-scoped multi-purpose tools (read subs).
        "git status",
        "git diff",
        "git log",
        "git show",
        "git branch",
        "git rev-parse",
        "git ls-files",
        "dbt ls",
        "dbt parse",
        "dbt compile",
        "dbt debug",
        "dlt --version",
    }
)

# Write-tier bash subcommands — only reachable at `build`/`deploy`. These
# are the subcommands that mutate the working tree / warehouse / remote.
_BUILD_BASH_ALLOW: frozenset[str] = frozenset(
    {
        "git add",
        "git commit",
        "git checkout",
        "git switch",
        "git restore",
        "git stash",
        "git merge",
        "git rebase",
        "git reset",
        "git tag",
        "dbt run",
        "dbt build",
        "dbt seed",
        "dbt test",
        "dbt snapshot",
        "dlt pipeline",
        "dlt init",
    }
)

# Deploy-tier subcommands — pushing to a remote / opening PRs. Prompted at
# `deploy` (so a registered approver can gate them), denied below.
_DEPLOY_BASH_PROMPT: frozenset[str] = frozenset(
    {
        "git push",
        "gh pr create",
        "gh pr merge",
        "dlt deploy",
    }
)

# Always-denied programs, every mode. Shell builtins / interpreters that
# re-introduce arbitrary execution, package managers that fetch+run,
# network-egress tools, and destructive primitives. Deny wins over allow
# in the gate, so listing a program here neutralises any accidental
# re-admission to an allow set later. The interpreters/package managers
# are denied explicitly (not merely omitted from allow) so the boundary
# is legible and a future allow-list edit cannot silently re-open ACE.
_ALWAYS_DENY: frozenset[str] = frozenset(
    {
        # Shell interpreters / re-entry (run an arbitrary script).
        "sh",
        "bash",
        "zsh",
        "fish",
        "dash",
        "ksh",
        "csh",
        "tcsh",
        "eval",
        "exec",
        "source",
        ".",
        # Privilege escalation.
        "sudo",
        "su",
        "doas",
        # Language interpreters — ACE via -c / -e / a script argument.
        "python",
        "python2",
        "python3",
        "perl",
        "ruby",
        "node",
        "nodejs",
        "deno",
        "bun",
        "php",
        "lua",
        "Rscript",
        # Package managers — fetch from an attacker index + run build hooks
        # (ACE) and reach the network (egress).
        "pip",
        "pip3",
        "pipx",
        "uv",
        "uvx",
        "poetry",
        "conda",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "gem",
        "cargo",
        # Network egress (exfiltration / fetch-and-run).
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "socat",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "ftp",
        "telnet",
        # Exec multiplexers / env-rewriters — run an arbitrary program
        # through their own argv, defeating the argv[0] allow-list.
        "env",  # `env VAR=v prog` re-exec form
        "xargs",  # `xargs <prog>` runs an arbitrary program per line
        "find",  # -exec/-execdir/-delete/-fprintf run a program / write
        "awk",  # `awk 'BEGIN{system("…")}'` shells out
        "gawk",
        "mawk",
        "sed",  # GNU `sed -e 's//e'` / `w file` can exec/write
        "watch",  # `watch <prog>` runs an arbitrary program on a loop
        "timeout",  # `timeout N <prog>` wraps an arbitrary program
        "nohup",  # `nohup <prog>` detaches an arbitrary program
        "nice",  # `nice <prog>` / `ionice <prog>` wrap a program
        "ionice",
        "setsid",
        "stdbuf",  # `stdbuf <prog>` wraps a program
        "tee",  # `tee file` is an arbitrary-write primitive
        # Destructive / privileged FS primitives.
        "rm",
        "mv",
        "cp",  # `cp src /etc/...` writes outside the tree
        "ln",  # `ln -sf` plants symlinks (TOCTOU / containment escape)
        "dd",
        "chmod",
        "chown",
        "chgrp",
        "mkfifo",
        "mknod",
        "truncate",
        "shred",
        "install",  # `install -m … src dest` is a write primitive
    }
)


# Per-program flag deny-list for the multi-purpose tools that *stay*
# allow-listed (their subcommand decides the tier). Even with an
# allow-listed subcommand, these flags turn the tool into an
# arbitrary-code-execution or arbitrary-write engine, so the bash gate
# denies the whole invocation if any token matches — checked across the
# entire argv, not just argv[1], since a global flag can precede the
# subcommand (``git -c k=v status``).
#
# ``git -c <k=v>`` injects config — ``core.sshCommand``,
#   ``core.fsmonitor``, ``core.pager``, ``diff.external``, ``alias.*`` are
#   all "run this program" sinks; ``--exec-path``/``--upload-pack``/
#   ``--receive-pack`` point git at an attacker binary; ``--ext-diff``
#   runs the gitconfig external diff driver; ``-c``/``--config-env`` and
#   ``-o``/``--output`` write arbitrary paths; ``--no-index`` turns
#   ``git diff`` into an arbitrary two-file reader (``git diff --no-index
#   <secretA> <secretB>`` prints both files' contents, unconfined by the
#   repo).
# ``dbt``/``dlt`` ``--profiles-dir`` / ``--project-dir`` can repoint the
#   tool at an attacker-authored project/profile (which then executes
#   arbitrary Jinja/Python on compile/run); ``--log-path`` /
#   ``--target-path`` write dbt's logs/artifacts to an arbitrary path
#   (an out-of-tree write, parallel to ``git -o``). All are denied here;
#   the tools default to the pinned project root under the cwd. (dlt
#   carries the same two repoint flags and is given the path-write
#   denials too, defensively — harmless if dlt ignores a flag, protective
#   if a future dlt adds it.)
#
# A flag matches if the token equals the flag exactly or begins with
# ``"<flag>="`` (the ``--flag=value`` form). Short combined forms like
# ``-cC`` are not a concern: git's global flags are not stackable.
DANGEROUS_BASH_FLAGS: dict[str, frozenset[str]] = {
    "git": frozenset(
        {
            "-c",
            "--config-env",
            "--exec-path",
            "--upload-pack",
            "--receive-pack",
            "--ext-diff",
            "--no-index",
            "-C",
            "-o",
            "--output",
            "--git-dir",
            "--work-tree",
        }
    ),
    "dbt": frozenset(
        {
            "--profiles-dir",
            "--project-dir",
            "--profile",
            "--log-path",
            "--target-path",
        }
    ),
    "dlt": frozenset(
        {
            "--profiles-dir",
            "--project-dir",
            "--log-path",
            "--target-path",
        }
    ),
}


def _bash_rules_for_mode(mode: PermissionMode) -> BashRules:
    """Build the per-mode bash tiering table from the floor sets."""
    if mode in (PermissionMode.READ_ONLY, PermissionMode.PLAN):
        return BashRules(
            allow=_READ_BASH_ALLOW,
            prompt=frozenset(),
            deny=_ALWAYS_DENY,
        )
    if mode is PermissionMode.BUILD:
        return BashRules(
            allow=_READ_BASH_ALLOW | _BUILD_BASH_ALLOW,
            prompt=frozenset(),
            deny=_ALWAYS_DENY,
        )
    # deploy
    return BashRules(
        allow=_READ_BASH_ALLOW | _BUILD_BASH_ALLOW,
        prompt=_DEPLOY_BASH_PROMPT,
        deny=_ALWAYS_DENY,
    )


def _permitted_tools_for_mode(mode: PermissionMode) -> frozenset[str]:
    """The hardcoded permitted tool set for ``mode`` (the floor)."""
    if mode in (PermissionMode.READ_ONLY, PermissionMode.PLAN):
        return _READ_TOOLS
    if mode is PermissionMode.BUILD:
        # build adds the write/edit tools (warehouse DDL stays deploy-only)
        return _READ_TOOLS | frozenset(
            {"edit", "create_file", "write_file"}
        )
    # deploy: everything, including warehouse DDL.
    return _ALL_TOOLS


# ---------------------------------------------------------------------------
# Optional config overlay (tighten-only)
# ---------------------------------------------------------------------------


class PermissionsConfig(BaseModel):
    """Parsed ``[permissions]`` block from ``runtime.toml`` (optional).

    Every field is a *tightening* overlay: tool names listed in
    ``denied_tools`` are removed from every mode's permitted set, and
    bash tokens in ``bash_deny`` are forced to the deny tier. There is
    deliberately **no field that widens** authority — config can never
    grant a tool the floor withholds (the spec's open question defers a
    richer surface; this is the safe minimum).

    The block is optional and not wired into the top-level ``Config`` in
    this slice; callers that have it pass it to :func:`build_policy`.
    """

    model_config = ConfigDict(extra="forbid")

    denied_tools: frozenset[str] = Field(default_factory=frozenset)
    bash_deny: frozenset[str] = Field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Agent capability + the effective policy the gate consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentPolicy:
    """The per-agent inputs to policy reconciliation.

    * ``tools`` — the agent's ``tools:`` grant (the widest set it *asks*
      for). Intersected with the mode's permitted set; it can only
      narrow, never widen.
    * ``allowed_paths`` — resolved absolute paths the agent's write tools
      may touch. ``None`` means "the agent's own component dir / whatever
      the tool was constructed with"; an empty set means "no writes".
    * ``capability`` — the widest mode this agent may run at. Delegation
      clamps the running mode to ``min(parent_mode, capability)``.
    """

    tools: frozenset[str]
    capability: PermissionMode
    allowed_paths: frozenset[Path] | None = None


@dataclass(frozen=True)
class EffectivePolicy:
    """The reconciled, gate-ready policy for one running agent.

    Produced by :func:`build_policy`. Holds the *intersected* permitted
    tool set, the bash tiering for the running mode, the narrowed
    ``allowed_paths``, and the running ``mode`` itself. The gate reads
    only this object — it never re-derives policy from modes/agents.
    """

    mode: PermissionMode
    permitted_tools: frozenset[str]
    bash: BashRules
    allowed_paths: frozenset[Path] | None = field(default=None)

    def tool_permitted(self, tool_name: str) -> bool:
        """Return True iff ``tool_name`` is in the intersected permitted set."""
        return tool_name in self.permitted_tools


def build_policy(
    mode: PermissionMode,
    *,
    agent: AgentPolicy | None = None,
    config: PermissionsConfig | None = None,
) -> EffectivePolicy:
    """Reconcile ``effective = mode-default ∩ config ∩ agent`` (tighten-only).

    The mode default is the floor. ``config`` (if present) removes tools
    and forces bash tokens to deny. ``agent`` (if present) intersects its
    ``tools:`` grant with what remains and narrows ``allowed_paths``.

    The result is the airtight permitted set the gate enforces — note in
    particular that no input can *add* a tool the mode withholds, so a
    write tool is absent below ``build`` no matter what an agent grants.
    """
    permitted = _permitted_tools_for_mode(mode)
    bash = _bash_rules_for_mode(mode)

    if config is not None:
        permitted = permitted - config.denied_tools
        if config.bash_deny:
            bash = BashRules(
                allow=bash.allow - config.bash_deny,
                prompt=bash.prompt - config.bash_deny,
                deny=bash.deny | config.bash_deny,
                default=bash.default,
            )

    allowed_paths: frozenset[Path] | None = None
    if agent is not None:
        # Intersection: the grant can only narrow the floor.
        permitted = permitted & agent.tools
        allowed_paths = (
            frozenset(p.resolve() for p in agent.allowed_paths)
            if agent.allowed_paths is not None
            else None
        )

    return EffectivePolicy(
        mode=mode,
        permitted_tools=permitted,
        bash=bash,
        allowed_paths=allowed_paths,
    )


__all__ = [
    "DANGEROUS_BASH_FLAGS",
    "WRITE_TOOLS",
    "AgentPolicy",
    "BashRules",
    "EffectivePolicy",
    "PermissionsConfig",
    "build_policy",
]
