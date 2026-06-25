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

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from carve.core.agents.permissions.modes import PermissionMode, mode_permits

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
        "sql",  # dialect-aware SQL tool (write/DDL gated inside the tool)
        "lookup_skill_pack",
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
        # The dialect-aware `sql` tool spans read + write + DDL under one name.
        # The name-only gate admits it in every mode (its reads must always be
        # reachable); write/DDL enforcement lives INSIDE the tool — it is built
        # with the active PermissionMode closed over and uses `role_for`, which
        # denies warehouse writes below `deploy` (warehouse_roles). So a
        # `sql(op=run)` write in read_only is admitted by name but fail-closed
        # in the executor. (Mirrors how `run_snowflake_query` is read-floored.)
        "sql",
        # `lookup_skill_pack` injects a curated pack's instructions into the
        # conversation; it reads inert on-disk SKILL.md content (no script
        # runs) and writes nothing, so it belongs in the read-only floor.
        # The orchestrator constructs the agent *with* this tool, so without
        # it here a gated loop (e.g. a SubagentRunner with the lookup tool)
        # would DENY a permitted, read-only injection — closed-world denying
        # a tool that should always be reachable.
        "lookup_skill_pack",
        "bash",  # gated per-command; read-only bash is allowed in read_only
        "delegate",
    }
)


# ---------------------------------------------------------------------------
# MCP-imported tools (spec 16 — consume external servers)
# ---------------------------------------------------------------------------
#
# An MCP server's tools are imported dynamically as ``mcp:<server>:<tool>``
# (the ``mcp:`` prefix is the namespace guarantee — it can't collide with a
# base tool or a ``@skill`` name). Each carries an ``effects`` tag the
# import derives into a single ``writes`` boolean. Because these names are
# **not** in any static mode set, the closed-world gate denies them by
# default; this registration path classifies them *into* the policy so they
# can be permitted — without ever touching the base static sets.
#
# FAIL-CLOSED is the whole point: a tool with missing/incomplete effects is
# `writes=True` (decided in ``client.py``), and a writer is denied below
# ``build`` and prompt-tier at ``build``/``deploy``. A read-only tool is
# permitted from ``read_only`` up. An *unregistered* ``mcp:`` name is in no
# set and stays denied in every mode — the closed-world property is
# preserved, only widened by an explicit, effects-derived registration.


# The namespace prefix every imported MCP tool name must carry. It is the
# collision + widening guarantee: `build_policy` only ever *adds*
# `mcp:`-prefixed names to the permitted set, so a name without this prefix
# could shadow a base tool (e.g. a crafted `McpToolSpec(name="edit")`). The
# prefix is enforced as a hard precondition at construction (below) — not
# merely belt-checked against `WRITE_TOOLS` at the widening point — so a
# non-prefixed spec can never be built, let alone reach `permitted_tools`.
MCP_TOOL_PREFIX = "mcp:"


@dataclass(frozen=True)
class McpToolSpec:
    """One imported MCP tool, classified for the policy.

    * ``name`` — the namespaced ``mcp:<server>:<tool>`` identifier. The
      ``mcp:`` prefix is a **hard precondition** (enforced in
      :meth:`__post_init__`): a name lacking it is rejected at
      construction, so a crafted spec can never widen a base-tool name.
    * ``writes`` — derived from the tool's ``effects`` with the
      **fail-closed default**: missing/incomplete effects ⇒ ``True``. A
      ``True`` here means "treat as a write tool" (deny below ``build``,
      prompt-tier at ``build``/``deploy``); ``False`` means "read-only"
      (permitted from ``read_only`` up).
    """

    name: str
    writes: bool

    def __post_init__(self) -> None:
        # The widening-point guard: every McpToolSpec name must be in the
        # `mcp:` namespace. Without this, `build_policy`'s union step could
        # admit a name that collides with a base tool. Rejecting here makes
        # the prefix a precondition of the type, not a downstream check a
        # caller might forget.
        if not self.name.startswith(MCP_TOOL_PREFIX):
            raise ValueError(
                f"McpToolSpec name {self.name!r} must start with "
                f"{MCP_TOOL_PREFIX!r}; an MCP tool name is always namespaced "
                "so it cannot shadow a base tool."
            )


def _mcp_permitted_for_mode(
    mode: PermissionMode, mcp_tools: Iterable[McpToolSpec]
) -> tuple[frozenset[str], frozenset[str]]:
    """Classify imported MCP tools into ``(permitted, prompt)`` for ``mode``.

    * A **read-only** MCP tool (``writes=False``) is permitted in every
      mode (read_only → deploy).
    * A **writer** MCP tool (``writes=True``, incl. the missing-effects
      fail-closed default) is permitted **only** at ``build``/``deploy``,
      and there it is **prompt-tier** (held for approval / fail-closed when
      non-interactive). Below ``build`` it is absent from ``permitted`` →
      the gate denies it.

    Returns the set of permitted MCP names and the subset of those that
    must route to the prompt tier in this mode.
    """
    permitted: set[str] = set()
    prompt: set[str] = set()
    is_build_or_deploy = mode_permits(mode, PermissionMode.BUILD)
    for spec in mcp_tools:
        if not spec.writes:
            permitted.add(spec.name)
            continue
        # Writer (or missing-effects fail-closed): build/deploy only, prompt.
        if is_build_or_deploy:
            permitted.add(spec.name)
            prompt.add(spec.name)
    return frozenset(permitted), frozenset(prompt)


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
        # Carve's own read-only schema+DAG validation — the pipeline engineer's
        # verify loop runs `carve pipelines validate [<name>]` (no writes, no
        # network, project-scoped), so it sits on the read floor in every mode.
        "carve pipelines validate",
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
    # `carve` is allow-listed only for the read-only `pipelines validate`
    # subcommand, which operates on the project at the pinned `cwd`. Its
    # `--project-dir` flag would repoint it at an attacker-authored project
    # (whose pipelines/components are then loaded + validated, executing
    # arbitrary Jinja/Python on parse), so it gets the same repoint denial as
    # `dbt`/`dlt`. The verify loop runs the flagless `carve pipelines validate`
    # (defaulting to cwd), so this guard never bites legitimate use.
    "carve": frozenset({"--project-dir"}),
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
        return _READ_TOOLS | frozenset({"edit", "create_file", "write_file"})
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

    ``mcp_prompt_tools`` is the subset of permitted MCP-imported tools
    that must route to the **prompt tier** in the running mode (writer /
    missing-effects MCP tools at ``build``/``deploy``). The gate consults
    it the way it consults the bash prompt tier: a prompt outcome needs an
    interactive approver, else it is held (fail-closed). It is a subset of
    ``permitted_tools`` — a name absent from ``permitted_tools`` is denied
    regardless of this set.
    """

    mode: PermissionMode
    permitted_tools: frozenset[str]
    bash: BashRules
    allowed_paths: frozenset[Path] | None = field(default=None)
    mcp_prompt_tools: frozenset[str] = field(default_factory=frozenset)

    def tool_permitted(self, tool_name: str) -> bool:
        """Return True iff ``tool_name`` is in the intersected permitted set."""
        return tool_name in self.permitted_tools

    def mcp_requires_prompt(self, tool_name: str) -> bool:
        """Return True iff a *permitted* MCP tool must route to the prompt tier."""
        return tool_name in self.mcp_prompt_tools


def _grant_admits(mcp_names: frozenset[str], grant: frozenset[str]) -> frozenset[str]:
    """Filter classified MCP names to those the agent's ``grant`` admits.

    A grant entry admits an ``mcp:<server>:<tool>`` name when either:

    * it equals the name exactly (``mcp:jira:search`` grants that tool), or
    * it is the ``mcp:<server>:*`` wildcard for that name's server
      (``mcp:jira:*`` grants every imported ``mcp:jira:`` tool).

    The wildcard is **server-scoped** by construction — it must carry a
    concrete ``<server>`` segment and the literal ``*`` tool segment, so a
    grant cannot widen across servers (there is no ``mcp:*`` form: it would
    not match the three-segment ``mcp:<server>:<tool>`` shape). The result
    is still a subset of ``mcp_names`` (already mode-classified), so a
    wildcard never escapes the effects/mode floor.
    """
    if not grant:
        return frozenset()
    # Precompute the server-wildcard prefixes the grant authorises:
    # `mcp:jira:*` -> the prefix `mcp:jira:` that an imported tool must
    # start with. Only well-formed `mcp:<server>:*` entries qualify.
    wildcard_prefixes = {
        entry[: -len("*")]  # drop the trailing `*`, keep `mcp:<server>:`
        for entry in grant
        if entry.startswith(MCP_TOOL_PREFIX) and entry.endswith(":*") and entry.count(":") == 2
    }
    admitted: set[str] = set()
    for name in mcp_names:
        if name in grant:
            admitted.add(name)
            continue
        if any(name.startswith(prefix) for prefix in wildcard_prefixes):
            admitted.add(name)
    return frozenset(admitted)


def build_policy(
    mode: PermissionMode,
    *,
    agent: AgentPolicy | None = None,
    config: PermissionsConfig | None = None,
    mcp_tools: Iterable[McpToolSpec] | None = None,
) -> EffectivePolicy:
    """Reconcile ``effective = mode-default ∩ config ∩ agent`` (tighten-only),
    then **widen** by the explicit, effects-derived MCP registration.

    The mode default is the floor. ``config`` (if present) removes tools
    and forces bash tokens to deny. ``agent`` (if present) intersects its
    ``tools:`` grant with what remains and narrows ``allowed_paths``.

    No input can *add* a **base** tool the mode withholds, so a base write
    tool is absent below ``build`` no matter what an agent grants — the
    static sets are untouched.

    ``mcp_tools`` is the only *widening* input, and it widens **only** the
    ``mcp:<server>:<tool>`` namespace, never a base tool: each registered
    MCP tool is classified by :func:`_mcp_permitted_for_mode`
    (read-only ⇒ from ``read_only`` up; writer / missing-effects ⇒
    ``build``/``deploy`` only, prompt-tier there). When an ``agent`` is
    present, the registered MCP permitted set is **also** intersected with
    the agent's grant (an agent only gets the MCP tools it asked for); an
    *unregistered* ``mcp:`` name is in no set and stays denied in every
    mode (closed-world preserved).
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
    grant: frozenset[str] | None = None
    if agent is not None:
        grant = agent.tools
        # Intersection: the grant can only narrow the (base) floor.
        permitted = permitted & grant
        allowed_paths = (
            frozenset(p.resolve() for p in agent.allowed_paths)
            if agent.allowed_paths is not None
            else None
        )

    mcp_prompt: frozenset[str] = frozenset()
    if mcp_tools is not None:
        mcp_permitted, mcp_prompt = _mcp_permitted_for_mode(mode, mcp_tools)
        if grant is not None:
            # An agent only gets the MCP tools it actually granted. The
            # grant may name a tool exactly (`mcp:jira:search`) or use the
            # `mcp:<server>:*` wildcard (the spec's `tools: ["mcp:jira:*"]`
            # syntax), which admits every imported tool on that server. The
            # admitted set is still ∩ the mode-classified permitted set
            # above (a writer stays build/deploy-only) and the prompt subset
            # is intersected the same way.
            mcp_permitted = _grant_admits(mcp_permitted, grant)
            mcp_prompt = _grant_admits(mcp_prompt, grant)
        # Widen the permitted set with the classified MCP names. This is
        # additive over the `mcp:`-prefixed namespace only — `mcp_permitted`
        # can never contain a base tool name (every entry is a registered
        # McpToolSpec.name, all `mcp:`-prefixed by construction).
        permitted = permitted | mcp_permitted
        # A `config.denied_tools` entry still removes an MCP tool (config is
        # tighten-only and applies last over the union).
        if config is not None and config.denied_tools:
            permitted = permitted - config.denied_tools
            mcp_prompt = mcp_prompt - config.denied_tools

    return EffectivePolicy(
        mode=mode,
        permitted_tools=permitted,
        bash=bash,
        allowed_paths=allowed_paths,
        mcp_prompt_tools=mcp_prompt,
    )


__all__ = [
    "DANGEROUS_BASH_FLAGS",
    "MCP_TOOL_PREFIX",
    "WRITE_TOOLS",
    "AgentPolicy",
    "BashRules",
    "EffectivePolicy",
    "McpToolSpec",
    "PermissionsConfig",
    "build_policy",
]
