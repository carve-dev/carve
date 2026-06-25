"""The bash gate — the load-bearing tiering surface for shell commands.

`bash` invokes a real shell, so a naive prefix-glob over the raw command
string is arbitrary-code-execution by construction (``git status;
rm -rf /`` would "match" ``git``). The shipped ``m1_tools._is_safe_select``
learned this discipline for SQL; the bash gate applies the same shape:

1. **Reject sub-execution metacharacters outright.** Any of
   ``$( )`` / backtick / ``;`` / ``&&`` / ``||`` / ``|`` / ``>`` / ``>>``
   / ``&`` / newline → **deny** (not prompt), *unless* the entire raw
   command string is a registered structured allow-entry. A shell would
   interpret these as a second command, a substitution, or a redirect —
   none of which the argv allowlist can reason about.
2. **``shlex``-parse** the (metacharacter-free) command into argv.
3. **Match** ``argv[0]`` — and, for ``git``/``dbt``/``dlt``/``gh``, the
   ``"<prog> <subcommand>"`` pair — against the mode's deny / prompt /
   allow tiers in that precedence. No match → the table's ``default``
   (``deny``).

The result is a tier string (``"allow"`` / ``"prompt"`` / ``"deny"``);
the caller (`gate.py`) turns ``prompt`` into deny+``needs_user_input``
when no interactive approver is registered (fail-closed).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from carve.core.agents.permissions.policy import DANGEROUS_BASH_FLAGS, BashRules

# Sub-execution metacharacters. Presence of any of these (outside a
# registered structured allow-entry) is an immediate deny: a shell would
# read them as command separation, substitution, piping, or redirection,
# and the argv allowlist cannot vet what runs after them.
_METACHARACTERS: tuple[str, ...] = (
    "$(",
    "${",
    "`",
    ";",
    "&&",
    "||",
    "|",
    ">",
    "<",
    "&",
    "\n",
    "\r",
)

# Programs whose *subcommand* decides the tier. For these we match the
# two-token ``"<prog> <subcommand>"`` key first, then fall back to the
# bare program (so an unknown subcommand of an otherwise-known tool still
# resolves via the program-level default rather than silently allowing).
_SUBCOMMAND_PROGRAMS: frozenset[str] = frozenset({"git", "dbt", "dlt", "gh", "carve"})


def _dangerous_flag(prog_base: str, argv: list[str]) -> str | None:
    """Return the first ACE/escape flag in ``argv`` for ``prog_base``.

    The multi-purpose tools (``git``/``dbt``/``dlt``) stay allow-listed by
    subcommand, but a handful of their global/exec flags re-introduce
    arbitrary execution or writes (``git -c diff.external=…``,
    ``dbt --project-dir /attacker``). We scan **every** argument token —
    a global flag can precede the subcommand — and match a token that
    equals the flag or begins ``"<flag>="`` (the ``--flag=value`` form).
    Returns the offending flag, or ``None`` if the invocation is clean.
    """
    deny_flags = DANGEROUS_BASH_FLAGS.get(prog_base)
    if not deny_flags:
        return None
    for token in argv[1:]:
        for flag in deny_flags:
            if token == flag or token.startswith(f"{flag}="):
                return flag
    return None


@dataclass(frozen=True)
class BashDecision:
    """Outcome of tiering one bash command.

    ``tier`` is ``"allow"`` / ``"prompt"`` / ``"deny"``. ``reason`` is a
    short human string for the deny/prompt cases (surfaced in the tool
    result so the agent can adapt). ``argv`` is the parsed command (empty
    on a parse failure or metacharacter denial).
    """

    tier: str
    reason: str
    argv: tuple[str, ...] = ()


def _contains_metacharacter(command: str) -> str | None:
    """Return the first sub-execution metacharacter found, or ``None``."""
    for meta in _METACHARACTERS:
        if meta in command:
            return meta
    return None


def tier_bash_command(command: str, rules: BashRules) -> BashDecision:
    """Tier a single ``bash`` command against the mode's ``rules``.

    The whole-command structured allow-list (``rules`` does not carry one
    in this slice — the floor is argv-based) would be consulted before
    the metacharacter screen; absent it, any metacharacter denies.
    """
    if not command or not command.strip():
        return BashDecision("deny", "Empty command.")

    meta = _contains_metacharacter(command)
    if meta is not None:
        return BashDecision(
            "deny",
            (
                f"Command contains the sub-execution metacharacter {meta!r}. "
                "Pipes, redirects, command substitution, and command "
                "chaining are not allowed; run a single program with "
                "plain arguments."
            ),
        )

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return BashDecision("deny", f"Could not parse command: {exc}")

    if not argv:
        return BashDecision("deny", "Command parsed to no program.")

    prog = argv[0]
    # Strip a leading path so `/usr/bin/git` tiers like `git`. We tier on
    # the basename; the sandbox floor (cwd-pin, scrubbed env) constrains
    # *where* it runs regardless.
    prog_base = prog.rsplit("/", 1)[-1]

    keys: list[str] = [prog_base]
    if prog_base in _SUBCOMMAND_PROGRAMS and len(argv) >= 2:
        # Prefer the most-specific key, then fall back to less-specific so
        # an unlisted subcommand still resolves via the program default
        # (most-specific first ⇒ inserted last). ``gh`` is noun-verb
        # (``gh pr create``), so try the three-token key too; ``git``/
        # ``dbt``/``dlt`` are single-subcommand (the three-token key
        # simply never matches their two-token allow entries).
        keys.insert(0, f"{prog_base} {argv[1]}")
        if len(argv) >= 3:
            keys.insert(0, f"{prog_base} {argv[1]} {argv[2]}")

    # Deny → prompt → allow precedence. Deny always wins.
    for key in keys:
        if key in rules.deny:
            return BashDecision(
                "deny",
                f"Command {key!r} is on the deny-list for this mode.",
                tuple(argv),
            )
    # A bare-program deny (e.g. `sh`, `rm`) also covers any subcommand form.
    if prog_base in rules.deny:
        return BashDecision(
            "deny",
            f"Program {prog_base!r} is denied in every mode.",
            tuple(argv),
        )

    # Flag-level deny for the multi-purpose tools that stay allow-listed:
    # an allow-listed subcommand must still be rejected if the argv carries
    # a config-injection / exec / arbitrary-write flag (`git -c …`,
    # `git diff --ext-diff`, `dbt --project-dir …`). Checked before the
    # allow match so deny wins.
    bad_flag = _dangerous_flag(prog_base, argv)
    if bad_flag is not None:
        return BashDecision(
            "deny",
            (
                f"Command uses the disallowed flag {bad_flag!r} for "
                f"{prog_base!r}; it can run an arbitrary program or write "
                "outside the workspace (config injection / external driver "
                "/ repointed project). Run the plain subcommand without it."
            ),
            tuple(argv),
        )

    for key in keys:
        if key in rules.prompt:
            return BashDecision(
                "prompt",
                f"Command {key!r} requires approval in this mode.",
                tuple(argv),
            )

    for key in keys:
        if key in rules.allow:
            return BashDecision("allow", "", tuple(argv))

    # No tier matched → the table default (deny on the floor).
    return BashDecision(
        rules.default,
        (
            f"Command {prog_base!r} is not on the allow-list for this mode. "
            "Only an explicit set of read/build/deploy commands is permitted."
        ),
        tuple(argv),
    )


__all__ = ["BashDecision", "tier_bash_command"]
