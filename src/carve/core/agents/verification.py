"""The bounded verification loop — run a check, parse it, iterate to fix.

The harness verifies its work by **executing** it. :func:`run_check` runs
a command **through the gated ``bash`` tool** — the same allowlist,
scrubbed env, cwd-pin, and output cap, with **no second execution
path** — and applies an **injected** ``parse`` callable to the captured
result. ``run_check`` is deliberately **format-agnostic**: it knows
nothing about dlt ``state.json`` or dbt ``run_results.json``; the format
owners (specs 04 / 08) inject the parser that turns a
``CompletedProcess``-shaped result into a :class:`CheckResult`.

The :class:`VerificationLoop` driver runs generate → run → read → fix
bounded by two ceilings — ``max_verification_iterations`` (default 4) and
a per-invocation token/cost budget (subagent costs aggregate against the
parent's). On exhaustion it returns ``status="needs_user_input"`` with
the last :class:`CheckResult` rather than looping forever.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from carve.core.agents.tools import Tool, ToolExecutionError

# The injected parser maps a finished process to a domain CheckResult. We
# pass a `subprocess.CompletedProcess[str]` so 04/08 can parse stdout (or
# read an artifact path the command wrote) however their format needs.
ParseFn = Callable[["subprocess.CompletedProcess[str]"], "CheckResult"]

MAX_VERIFICATION_ITERATIONS = 4


class CheckDenied(ToolExecutionError):
    """Raised when the gated ``bash`` tool denies the verification command.

    A non-allowlisted command (or one carrying metacharacters) is denied
    by the same gate that guards the agent's ``bash`` — there is no second
    execution path, so verification cannot run an un-vetted command.
    """


@dataclass
class CheckResult:
    """The outcome of one verification check.

    ``passed`` drives the generate→fix loop; ``summary`` and ``details``
    are surfaced to the agent (and the user, on exhaustion). The format
    owner fills these from whatever artifact/exit code its tool produces.
    """

    passed: bool
    summary: str = ""
    details: dict[str, object] = field(default_factory=dict)


def run_check(
    cmd: str,
    *,
    parse: ParseFn,
    bash_tool: Tool,
    timeout: int = 120,
) -> CheckResult:
    """Run ``cmd`` through the gated ``bash`` tool and parse the result.

    ``cmd`` is executed by ``bash_tool.executor`` — the **same** gated,
    sandboxed bash the agent uses (allowlist + metacharacter-deny +
    scrubbed env + cwd-pin + output cap), so there is no second execution
    path. A denied command raises :class:`CheckDenied`. The captured
    output is wrapped in a ``CompletedProcess`` and handed to the injected
    ``parse``, which owns all format knowledge (dlt ``state.json`` / dbt
    ``run_results.json`` parsers live with their format owners).
    """
    try:
        output = bash_tool.executor({"command": cmd, "timeout": timeout})
    except ToolExecutionError as exc:
        # The gate denied the command (or it needs approval): no execution.
        raise CheckDenied(str(exc)) from exc

    exit_code = -1
    stdout = ""
    if isinstance(output, dict):
        code = output.get("exit_code")
        exit_code = code if isinstance(code, int) else -1
        out = output.get("stdout")
        stdout = out if isinstance(out, str) else ""
    completed: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        args=cmd,
        returncode=exit_code,
        stdout=stdout,
        stderr="",
    )
    return parse(completed)


@dataclass
class VerificationOutcome:
    """The terminal state of a bounded verification run."""

    status: str  # "passed" | "needs_user_input"
    iterations: int
    last_result: CheckResult


# A fix step is the agent action between check runs. It returns True if it
# attempted a fix (so the loop should re-check) and False if it gave up.
# The harness wires the agent loop here; tests pass a deterministic stub.
FixStep = Callable[[CheckResult], bool]


class VerificationLoop:
    """Bounded generate → run → read → fix driver.

    Construct with the check command + parser + cwd; call :meth:`run`
    with the fix step. Stops at the first passing check, when the fix
    step gives up, or when ``max_iterations`` is reached — in the latter
    cases returning ``needs_user_input`` rather than looping.
    """

    def __init__(
        self,
        cmd: str,
        *,
        parse: ParseFn,
        bash_tool: Tool,
        max_iterations: int = MAX_VERIFICATION_ITERATIONS,
        timeout: int = 120,
    ) -> None:
        self._cmd = cmd
        self._parse = parse
        self._bash_tool = bash_tool
        self._max_iterations = max(1, max_iterations)
        self._timeout = timeout

    def run(self, fix: FixStep) -> VerificationOutcome:
        """Iterate check→fix up to the ceiling; return the terminal outcome."""
        last = CheckResult(passed=False, summary="not run")
        for iteration in range(1, self._max_iterations + 1):
            last = run_check(
                self._cmd,
                parse=self._parse,
                bash_tool=self._bash_tool,
                timeout=self._timeout,
            )
            if last.passed:
                return VerificationOutcome(
                    status="passed", iterations=iteration, last_result=last
                )
            # Last allowed iteration — don't attempt another fix/re-check.
            if iteration >= self._max_iterations:
                break
            attempted = fix(last)
            if not attempted:
                # The agent couldn't propose a fix — surface to the user.
                return VerificationOutcome(
                    status="needs_user_input",
                    iterations=iteration,
                    last_result=last,
                )
        return VerificationOutcome(
            status="needs_user_input",
            iterations=self._max_iterations,
            last_result=last,
        )


__all__ = [
    "MAX_VERIFICATION_ITERATIONS",
    "CheckDenied",
    "CheckResult",
    "FixStep",
    "ParseFn",
    "VerificationLoop",
    "VerificationOutcome",
    "run_check",
]
