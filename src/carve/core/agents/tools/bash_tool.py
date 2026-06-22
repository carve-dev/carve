"""The ``bash`` tool — gated, sandboxed shell execution.

``bash`` runs a real shell command, but only the ones the gate clears
(see `permissions.gate` / `permissions.bash_gate`). Whether or not a
given command is *allowed* is the gate's job; this module owns the
**sandbox floor** the command runs inside, and it reuses the shipped
``LocalVenvRunner`` primitives so there is exactly one subprocess
discipline in the codebase:

* **Scrubbed env (default-deny allowlist)** — the subprocess inherits
  *only* a small set of neutral vars (``PATH``/``HOME``/locale/tool-cache
  dirs); everything else is dropped, and a credential-shape filter
  (``*PASSWORD*``/``*TOKEN*``/``*SECRET*``/``*_KEY``/``*SNOWFLAKE*``/…)
  removes anything that slips through. **No warehouse credential ever
  survives into the env, and none is ever added** — the agent reaches the
  warehouse only through the role-scoped ``sql`` tool, never a bash env.
  (A denylist would always lag the next credential var a connector adds;
  the allowlist only grows for a provably-neutral need.)
* **cwd-pinned** to the project root (or a caller-supplied subdir under
  it), so a command can't operate outside the workspace.
* **Bounded timeout** with a process-group SIGTERM→SIGKILL escalation,
  reusing ``local_venv._killpg`` so forked children die too.
* **Captured + capped** stdout/stderr — output is truncated *before* it
  enters the tool result (and thus the transcript / telemetry), so a
  runaway command can't blow the context window.

The tool itself does **not** re-check permissions — the loop's gate has
already cleared the command by the time the executor runs. Constructing
the tool with the gate is belt-and-braces for direct callers
(`verification.run_check`), which pass the same gate so there is no
second, ungated execution path.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carve.core.agents.permissions.gate import Approver, Outcome, PermissionGate
from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.runners.local_venv import _killpg

# Hard cap on captured output (bytes), applied before the result is
# returned. Keeps a chatty command from flooding the transcript.
_MAX_OUTPUT_BYTES = 16_000
# Floor on the per-command wall-clock timeout (seconds). The model can
# request less but not more than `_MAX_TIMEOUT`.
_DEFAULT_TIMEOUT = 120
_MAX_TIMEOUT = 600

# Default-deny env allowlist: the *only* parent-process variables an
# LLM-authored bash command inherits. Everything else (and in particular
# every warehouse / API credential `connections.toml` interpolated into
# `os.environ`) is dropped. This is the right posture — a denylist would
# always lag the next credential var a connector adds, whereas this list
# only grows when an *allowed tool* provably needs a new neutral var.
#
# Exact names plus a small set of safe prefixes (locale `LC_*`, the
# XDG_*_HOME dirs build tools read). `PATH`/`HOME` are required for any
# program to resolve and run; the git/dbt/dlt/gh tools need
# `HOME`-rooted config and the tool-cache dirs below.
_ENV_ALLOW_EXACT: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "TZ",
        "PWD",
        "COLUMNS",
        "LINES",
        # Tool caches/config the allowed tools (git/dbt/dlt/gh/uv venvs)
        # legitimately read. None of these carry credentials.
        "VIRTUAL_ENV",
        "DBT_PROFILES_DIR",
        "GIT_CONFIG_GLOBAL",
        "GH_CONFIG_DIR",
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)
_ENV_ALLOW_PREFIXES: tuple[str, ...] = ("LC_",)

# Defence-in-depth credential-shape filter, applied *after* the allowlist.
# Even a var that passes the allowlist is dropped if its name looks like a
# credential — so an operator who adds e.g. `DBT_PROFILES_DIR_TOKEN` (or a
# future allowlist entry that turns out to be sensitive) cannot leak it.
# This is the invariant the regression test pins: no credential-shaped var
# survives into the subprocess env, regardless of the allowlist.
_CREDENTIAL_NAME_RE = re.compile(
    r"(PASSWORD|SECRET|TOKEN|_KEY$|APIKEY|API_KEY|PRIVATE|CREDENTIAL|"
    r"PASSWD|PASSPHRASE|SNOWFLAKE|AWS|AZURE|GCP|GOOGLE_APPLICATION|"
    r"ANTHROPIC|OPENAI)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BashResult:
    """Captured outcome of one ``bash`` invocation."""

    exit_code: int
    stdout: str
    truncated: bool
    timed_out: bool


def _env_var_allowed(name: str) -> bool:
    """True iff ``name`` is on the bash env allowlist (exact or prefix)."""
    if name in _ENV_ALLOW_EXACT:
        return True
    return any(name.startswith(prefix) for prefix in _ENV_ALLOW_PREFIXES)


def _scrubbed_env() -> dict[str, str]:
    """The env an LLM-authored bash command inherits — default-deny.

    Two layers, both of which must pass: (1) the var name is on the
    neutral allowlist (``_ENV_ALLOW_EXACT`` / ``_ENV_ALLOW_PREFIXES``),
    and (2) the name does not match the credential-shape filter
    (``_CREDENTIAL_NAME_RE``). The result is that **no credential-shaped
    var — warehouse password, API token, private key — survives into the
    subprocess**, which is the invariant the spec mandates and the
    regression test pins. No warehouse creds are ever *added*: the agent
    reaches the warehouse only through the role-scoped ``sql`` tool.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if _env_var_allowed(k) and not _CREDENTIAL_NAME_RE.search(k)
    }


def _cap(text: str) -> tuple[str, bool]:
    """Truncate ``text`` to the output cap, returning (text, truncated)."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_OUTPUT_BYTES:
        return text, False
    clipped = encoded[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return clipped + "\n…[output truncated]", True


def run_bash(
    command: str,
    *,
    cwd: Path,
    timeout: int = _DEFAULT_TIMEOUT,
) -> BashResult:
    """Run ``command`` through the sandbox floor and capture output.

    This is the single subprocess path. ``command`` is run via the
    shell (the gate has already vetted it is metacharacter-free and
    allow-listed), in its own process group, with a scrubbed env and a
    cwd pin. On timeout the whole group is SIGTERM→SIGKILLed.
    """
    cwd = cwd.resolve()
    bounded_timeout = max(1, min(timeout, _MAX_TIMEOUT))
    proc = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(cwd),
        env=_scrubbed_env(),
        start_new_session=True,
        text=False,
    )
    timed_out = False
    try:
        raw, _ = proc.communicate(timeout=bounded_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _killpg(proc, signal.SIGTERM)
        try:
            raw, _ = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            _killpg(proc, signal.SIGKILL)
            try:
                raw, _ = proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                raw = b""
    decoded = (raw or b"").decode("utf-8", errors="replace")
    capped, truncated = _cap(decoded)
    return BashResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=capped,
        truncated=truncated,
        timed_out=timed_out,
    )


BASH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": (
                "A single shell command (no pipes, redirects, ;, &&, ||, "
                "or command substitution). Runs in the project root with "
                "a scrubbed environment and a bounded timeout."
            ),
        },
        "timeout": {
            "type": "integer",
            "description": ("Wall-clock timeout in seconds (default 120, max 600)."),
            "default": _DEFAULT_TIMEOUT,
        },
    },
    "required": ["command"],
}


def make_bash_tool(
    project_dir: Path,
    *,
    gate: PermissionGate,
    approver: Approver | None = None,
) -> Tool:
    """Build the ``bash`` tool bound to ``project_dir`` and the run's gate.

    The executor re-runs the gate as defense in depth, then routes the
    command through :func:`run_bash`. Direct callers (the verification
    loop) get the same gate + sandbox, so there is no ungated bash path.
    """
    project_root = project_dir.resolve()

    def _execute(input_: ToolInput) -> ToolResult:
        command = input_.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolExecutionError("`command` must be a non-empty string.")
        timeout_raw = input_.get("timeout", _DEFAULT_TIMEOUT)
        timeout = (
            timeout_raw
            if isinstance(timeout_raw, int) and not isinstance(timeout_raw, bool)
            else _DEFAULT_TIMEOUT
        )

        decision = gate.check("bash", dict(input_), approver=approver)
        if decision.outcome is Outcome.DENY:
            raise ToolExecutionError(f"bash denied: {decision.reason}")
        if decision.outcome is Outcome.NEEDS_USER_INPUT:
            raise ToolExecutionError(f"bash needs approval: {decision.reason}")

        result = run_bash(command, cwd=project_root, timeout=timeout)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "truncated": result.truncated,
            "timed_out": result.timed_out,
        }

    return Tool(
        name="bash",
        description=(
            "Run a single shell command in the project root. The command "
            "must be one program with plain arguments — no pipes, "
            "redirects, command chaining, or substitution (those are "
            "rejected). Use this for git, dbt, dlt, and standard read "
            "tools. The environment is scrubbed of secrets; the warehouse "
            "is not reachable from here. Output is captured and capped."
        ),
        input_schema=BASH_SCHEMA,
        executor=_execute,
    )


__all__ = ["BASH_SCHEMA", "BashResult", "make_bash_tool", "run_bash"]
