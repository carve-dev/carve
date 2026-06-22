"""`run_extract_load_agent` — the Pillar 1 extract-load specialist.

The build flow (P1-02 / `builder.py`) hands this agent a `Task` from
the plan task graph; the agent writes the three artifact files under
`el/<artifact_name>/` and terminates via `submit_step(...)`.

Inputs (the build flow assembles these from the plan):

- `task` — the dict pulled from `plan.task_graph["tasks"][i]`. Must
  carry `agent="extract_load"`, an `inputs` block (goal / source /
  destination / transformation / columns), and an `expected_outputs`
  list naming the three target paths.
- `active_target` — the resolved target name (e.g. ``"dev"``).
- `config` — fully-loaded `Config`; used for the connection-context
  preamble and the runtime-role lookup.

The agent does *not* re-derive any design decisions. The plan agent
already chose `destination.table`, `destination.primary_key`,
`transformation.strategy`, and the column list. If the design's
shape conflicts with what extract-load can deliver, the agent calls
`submit_step(error=True, summary=...)` and the build flow records a
failed run.

Build-flow integration: this module exposes only `run_extract_load_agent`
and the result dataclass. The current `builder.py` (M1.1-06's
build flow) continues to call `m1_build_agent` for its existing
contract; this new callable is wired in for P1-07 / P1-09 and tested
end-to-end in `tests/integration/test_extract_load_flow.py`. See the
phase doc's "Minimal vs Clean" decision for the rationale.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from carve.core.agents.loop import AgentLoop, AgentResult
from carve.core.agents.observer import AgentObserver, NullObserver
from carve.core.agents.tools import ToolExecutionError
from carve.core.agents.tools.extract_load_tools import (
    ExtractLoadTools,
    build_extract_load_tools,
)
from carve.core.config.schema import Config
from carve.core.connectors.snowflake import SnowflakePool
from carve.core.state.repository import Repository
from carve.core.targets.names import (
    ARTIFACT_NAME_RE,
    InvalidArtifactNameError,
    validate_artifact_name,
)

logger = logging.getLogger(__name__)


_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def load_extract_load_agent_prompt() -> str:
    """Load the extract-load agent system prompt from disk."""
    return (_PROMPTS_DIR / "extract_load_agent.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Result + error
# ---------------------------------------------------------------------------


@dataclass
class ExtractLoadResult:
    """Outcome of `run_extract_load_agent`.

    `success=False` when the agent terminated via `submit_step(error=True)`.
    The build flow turns this into a failed Build row with the agent's
    summary recorded as the error message.
    """

    file_list: list[str]
    summary: str
    error: bool
    agent_result: AgentResult
    tools_invoked: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.error and bool(self.file_list)


class ExtractLoadAgentError(Exception):
    """Raised when the agent loop completes without calling `submit_step`."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


class _UnconfiguredSnowflakeRunner:
    """Stub `SnowflakeQueryRunner` used when no target connection exists.

    Mirrors the planner's stub. When the agent calls
    `run_snowflake_query` against this runner it gets a clear
    actionable error in the tool result; the agent can recover or
    surface the failure via `submit_step(error=True)`.
    """

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
        raise ToolExecutionError(
            "No Snowflake connection is configured for the active target. "
            "Add a [connections.snowflake.<target>] block to "
            "carve/connections.toml and re-run."
        )


def run_extract_load_agent(
    task: dict[str, Any],
    active_target: str,
    config: Config,
    *,
    project_dir: Path,
    repository: Repository | None = None,
    run_id: str | None = None,
    client: Any | None = None,
    snowflake_pool: SnowflakePool | None = None,
    observer: AgentObserver | None = None,
    max_turns: int = 30,
    max_tokens: int = 4096,
) -> ExtractLoadResult:
    """Run the extract-load specialist against `task`.

    The agent terminates by calling `submit_step(file_list, summary,
    error=False)`. Returns an `ExtractLoadResult` carrying the captured
    payload plus the underlying `AgentResult` for token / cost
    accounting.
    """
    project_dir = project_dir.resolve()

    if task.get("agent") != "extract_load":
        raise ExtractLoadAgentError(
            "run_extract_load_agent invoked on a task with agent="
            f"{task.get('agent')!r}; expected 'extract_load'."
        )

    artifact_name = _resolve_artifact_name(task)
    allowed_paths = _allow_listed_paths(project_dir, active_target, artifact_name)

    snowflake_runner: Any
    if active_target in config.connections.snowflake:
        pool = snowflake_pool if snowflake_pool is not None else SnowflakePool(config)
        try:
            snowflake_runner = pool.get(active_target)
        except Exception:
            logger.warning(
                "Snowflake target %r is configured but unavailable; "
                "the agent will get an error if it uses run_snowflake_query.",
                active_target,
            )
            snowflake_runner = _UnconfiguredSnowflakeRunner()
    else:
        snowflake_runner = _UnconfiguredSnowflakeRunner()

    tools_bundle: ExtractLoadTools = build_extract_load_tools(
        project_dir=project_dir,
        allowed_paths=allowed_paths,
        snowflake_runner=snowflake_runner,
    )

    system_prompt = _compose_system_prompt(
        config=config,
        active_target=active_target,
        task=task,
        artifact_name=artifact_name,
        project_dir=project_dir,
    )

    anthropic_client = _resolve_client(config, client)

    loop = AgentLoop(
        client=anthropic_client,
        tools=tools_bundle.tools,
        system_prompt=system_prompt,
        model=config.models.default_model,
        repository=repository,
        run_id=run_id,
        max_tokens=max_tokens,
        observer=observer if observer is not None else NullObserver(),
        terminator_tool="submit_step",
    )

    initial_message = _compose_initial_user_message(
        active_target=active_target,
        artifact_name=artifact_name,
        task=task,
    )

    agent_result = loop.run(initial_message, max_turns=max_turns)

    capture = tools_bundle.submit_step_capture
    if not capture.submitted or capture.payload is None:
        raise ExtractLoadAgentError(
            "Extract-load agent finished without calling `submit_step`. "
            "The build flow can't record this run as success or failure; "
            "consider re-running with a tighter task description."
        )

    tools_invoked = _collect_tools_invoked(agent_result)

    return ExtractLoadResult(
        file_list=list(capture.file_list),
        summary=capture.summary,
        error=capture.error,
        agent_result=agent_result,
        tools_invoked=tools_invoked,
    )


# ---------------------------------------------------------------------------
# Task introspection
# ---------------------------------------------------------------------------


# The artifact-name regex (M1.1-06's pipeline-name shape) is hoisted
# into ``carve.core.targets.names`` so the deploy/verify CLIs can
# validate the same way the agent does. The local alias is kept as a
# convenience for code that wants the raw pattern.
_ARTIFACT_NAME_RE = ARTIFACT_NAME_RE


def _resolve_artifact_name(task: dict[str, Any]) -> str:
    """Pull the artifact name out of the task.

    Preference order:
    1. `task["inputs"]["artifact_name"]` (an explicit name from the
       build flow).
    2. The pipeline-name extracted from any of the `expected_outputs`
       paths (`el/<name>/main.py`).

    The resolved name is validated against the snake_case naming regex
    before being interpolated into filesystem paths — this is the only
    user-controllable component in the `write_file` allow-list, so a
    malformed value would otherwise reduce defense-in-depth to one layer.
    """
    inputs = task.get("inputs") or {}
    candidate = inputs.get("artifact_name")
    if isinstance(candidate, str) and candidate:
        return _validated_artifact_name(candidate)

    for entry in task.get("expected_outputs") or []:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str):
            continue
        parts = path.split("/")
        # el/<name>/...
        if len(parts) >= 3 and parts[0] == "el":
            return _validated_artifact_name(parts[1])
        # Legacy (P1-02): targets/<t>/el/<name>/...
        if len(parts) >= 4 and parts[0] == "targets" and parts[2] == "el":
            return _validated_artifact_name(parts[3])
        # Legacy (P1-02): targets/<t>/snowflake/<name>.sql
        if len(parts) == 4 and parts[0] == "targets" and parts[2] == "snowflake":
            return _validated_artifact_name(parts[3].removesuffix(".sql"))

    raise ExtractLoadAgentError(
        "Could not determine artifact_name from task; provide it as "
        "task['inputs']['artifact_name'] or via expected_outputs."
    )


def _validated_artifact_name(name: str) -> str:
    """Return ``name`` if it matches the artifact naming regex, else raise.

    Wraps :func:`carve.core.targets.names.validate_artifact_name` so a
    bad name surfaces as :class:`ExtractLoadAgentError` (the agent's
    error type) rather than the generic ``InvalidArtifactNameError``.
    """
    try:
        return validate_artifact_name(name)
    except InvalidArtifactNameError as exc:
        raise ExtractLoadAgentError(str(exc)) from exc


def _allow_listed_paths(
    project_dir: Path,
    active_target: str,
    artifact_name: str,
) -> set[Path]:
    """Compute the three resolved paths the `write_file` tool will accept.

    P1.1-01 flattened the layout: the three writable paths live under
    ``el/<artifact_name>/``, target-agnostic. ``active_target`` is
    accepted for signature parity with the build flow (the connection-
    context block in the system prompt still reflects the target).
    """
    del active_target
    base = project_dir / "el" / artifact_name
    return {
        (base / "main.py").resolve(),
        (base / "requirements.txt").resolve(),
        (base / "snowflake.sql").resolve(),
    }


# ---------------------------------------------------------------------------
# System-prompt assembly
# ---------------------------------------------------------------------------


def _compose_system_prompt(
    *,
    config: Config,
    active_target: str,
    task: dict[str, Any],
    artifact_name: str,
    project_dir: Path,
) -> str:
    """Base prompt + connection context + convention preamble + task /
    design preamble.

    The convention preamble (Pillar 2 / M2-08's `carve/conventions.md`)
    is empty in Pillar 1 unless a user has hand-written one; we still
    pass it through so the M2-08 work doesn't have to retrofit the
    wiring. Absent file → no section.
    """
    sections: list[str] = [load_extract_load_agent_prompt()]
    sections.append(_render_connection_context(config, active_target))
    conventions = _render_conventions_block(project_dir, config)
    if conventions is not None:
        sections.append(conventions)
    sections.append(_render_output_paths_block(active_target, artifact_name))
    sections.append(_render_task_preamble(task))
    existing = _render_existing_files_section(task)
    if existing is not None:
        sections.append(existing)
    return "\n\n".join(sections)


def _render_conventions_block(project_dir: Path, config: Config) -> str | None:
    """Read ``<project_dir>/<config_dir>/conventions.md`` if present.

    Returns ``None`` (and skips the section) when the file is missing,
    empty, or contains only HTML comments. Pillar 1 ships without a
    convention doc — and ``carve init`` scaffolds a comment-only placeholder
    until inference lands — so a file with no real prose must add nothing to
    the prompt (otherwise the agent is told as fact that no conventions
    exist). Pillar 2's M2-08 inference produces real content and the agent
    picks it up automatically.
    """
    candidate = project_dir / config.paths.config_dir / "conventions.md"
    if not candidate.is_file():
        return None
    raw = candidate.read_text(encoding="utf-8")
    # A comment-only doc (e.g. init's placeholder) is treated as empty.
    if not re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL).strip():
        return None
    return f"## Conventions\n\n{raw.strip()}"


def _render_connection_context(config: Config, active_target: str) -> str:
    """Render the system-prompt connection-context block.

    The block has two parts. The script-side rows show **env-var
    references**, not resolved values — by the time `Config` reaches
    this code, the loader has already substituted `${VAR}` placeholders
    against `os.environ`, so any concrete value rendered here would
    invite the agent to inline it as a literal. The DDL-side rows show
    the resolved identifiers because P1-06's per-EL `.sql` file is a
    target-specific snapshot that does NOT use `${VAR}` substitution.

    Dogfooding surfaced the bug this rendering closes: the agent saw
    resolved `account` / `database` values in the prompt and faithfully
    hardcoded them in `main.py`, which then broke when the same script
    was promoted to a different target.
    """
    upper = active_target.upper()
    snowflake = config.connections.snowflake.get(active_target)
    lines = [
        "## Connection context",
        "",
        f"Active target: `{active_target}`. The script and the DDL file "
        "consume connection state differently — pay attention to which "
        "block applies.",
        "",
        "**For `main.py` (Python script — runtime):**",
        "Read every connection field from env vars. NEVER inline a "
        "resolved account / database / role / warehouse value as a "
        "Python literal. The agent MUST emit the references below "
        "verbatim; the script is meant to run against any target by "
        "switching the env-var prefix.",
        "",
        f"- account:   `os.environ['{upper}_SNOWFLAKE_ACCOUNT']`",
        f"- user:      `os.environ['{upper}_SNOWFLAKE_USER']`",
        f"- password:  `os.environ['{upper}_SNOWFLAKE_PASSWORD']`",
        f"- role:      `os.environ['{upper}_SNOWFLAKE_ROLE']`",
        f"- warehouse: `os.environ['{upper}_SNOWFLAKE_WAREHOUSE']`",
        f"- database:  `os.environ['{upper}_SNOWFLAKE_DATABASE']`",
    ]
    # Schema is optional in `connections.toml` (pydantic field is
    # `str | None`). Only show the env-var reference when the target
    # actually has a schema configured — otherwise existing projects
    # without `<TARGET>_SNOWFLAKE_SCHEMA` in their .env would see the
    # agent emit a KeyError-prone reference.
    if snowflake is not None and snowflake.schema_:
        lines.append(f"- schema:    `os.environ['{upper}_SNOWFLAKE_SCHEMA']`")
    if snowflake is None:
        lines.append("")
        lines.append(
            "_(No `[snowflake.<target>]` section is configured for this "
            "target; the DDL file's concrete identifiers below come from "
            "the design's `destination` block.)_"
        )
        return "\n".join(lines)

    lines += [
        "",
        "**For the DDL file (`<artifact>.sql` — build-time snapshot):**",
        "Concrete identifiers go straight into the SQL. No `${VAR}` "
        "substitution; this file is a target-specific snapshot.",
        "",
        f"- Database: `{snowflake.database}`",
    ]
    if snowflake.schema_:
        lines.append(f"- Schema: `{snowflake.schema_}`")
    lines += [
        f"- Warehouse: `{snowflake.warehouse}`",
        f"- Runtime role (used in GRANT): `{snowflake.role}`",
        f"- Account: `{snowflake.account}`",
    ]
    return "\n".join(lines)


def _render_output_paths_block(active_target: str, artifact_name: str) -> str:
    del active_target  # flat layout; signature kept for parity.
    base = f"el/{artifact_name}"
    return (
        "## Output paths (this build)\n"
        f"- `{base}/main.py`\n"
        f"- `{base}/requirements.txt`\n"
        f"- `{base}/snowflake.sql`\n"
        "Writing any other path raises an error."
    )


def _render_task_preamble(task: dict[str, Any]) -> str:
    """Render the task / design as readable markdown for the agent."""
    inputs = task.get("inputs") or {}
    lines: list[str] = [
        "## Task",
        f"- **action:** `{task.get('action', 'generate_extractor')}`",
    ]
    goal = inputs.get("goal")
    if isinstance(goal, str) and goal:
        lines.append(f"- **goal:** {goal}")

    destination = inputs.get("destination")
    if isinstance(destination, dict):
        lines.append("")
        lines.append("### Destination")
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        for key in ("database", "schema", "table", "primary_key"):
            value = destination.get(key)
            if value is not None:
                lines.append(f"| `{key}` | `{value}` |")

    transformation = inputs.get("transformation")
    if isinstance(transformation, dict):
        lines.append("")
        lines.append("### Transformation")
        strategy = transformation.get("strategy", "")
        rationale = transformation.get("rationale", "")
        if strategy:
            lines.append(f"- **strategy:** `{strategy}`")
        if rationale:
            lines.append(f"- **rationale:** {rationale}")

    source = inputs.get("source")
    if isinstance(source, dict):
        lines.append("")
        lines.append("### Source")
        lines.append("```json")
        lines.append(json.dumps(source, indent=2, sort_keys=True))
        lines.append("```")

    columns = inputs.get("columns")
    if isinstance(columns, list) and columns:
        lines.append("")
        lines.append("### Columns")
        lines.append("| Name | Type | Nullable |")
        lines.append("| --- | --- | --- |")
        for column in columns:
            if not isinstance(column, dict):
                continue
            name = column.get("name", "")
            type_ = column.get("type", "")
            nullable = column.get("nullable", True)
            lines.append(f"| `{name}` | `{type_}` | {nullable} |")

    tradeoffs = inputs.get("tradeoffs")
    if isinstance(tradeoffs, list) and tradeoffs:
        lines.append("")
        lines.append("### Tradeoffs (approved at plan time)")
        for item in tradeoffs:
            lines.append(f"- {item}")

    expected = task.get("expected_outputs")
    if isinstance(expected, list) and expected:
        lines.append("")
        lines.append("### Expected outputs")
        for entry in expected:
            if isinstance(entry, dict) and "path" in entry:
                kind = entry.get("kind", "")
                lines.append(f"- `{entry['path']}` ({kind})")

    return "\n".join(lines)


def _render_existing_files_section(task: dict[str, Any]) -> str | None:
    """Inline existing files for `modify_extractor` actions."""
    inputs = task.get("inputs") or {}
    existing = inputs.get("existing_files")
    if not isinstance(existing, dict) or not existing:
        return None
    parts: list[str] = [
        "## Existing files (current state)",
        "Use these for reference; the design above is authoritative for the new state.",
    ]
    main_py = existing.get("main.py")
    if isinstance(main_py, str) and main_py.strip():
        parts += ["", "### `main.py`", "```python", main_py.rstrip("\n"), "```"]
    requirements = existing.get("requirements.txt")
    if isinstance(requirements, str) and requirements.strip():
        parts += [
            "",
            "### `requirements.txt`",
            "```",
            requirements.rstrip("\n"),
            "```",
        ]
    snowflake_sql = existing.get("snowflake_sql")
    if isinstance(snowflake_sql, str) and snowflake_sql.strip():
        parts += ["", "### Companion DDL", "```sql", snowflake_sql.rstrip("\n"), "```"]
    return "\n".join(parts)


def _compose_initial_user_message(
    *,
    active_target: str,
    artifact_name: str,
    task: dict[str, Any],
) -> str:
    """Frame the task as a single user message kicking off the agent."""
    action = task.get("action", "generate_extractor")
    return (
        f"{action} for artifact `{artifact_name}` against target "
        f"`{active_target}`. Write the three allow-listed files and "
        "call `submit_step(file_list, summary)` when done."
    )


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _resolve_client(config: Config, client: Any | None) -> Any:
    """Return `client` if provided; else build one from `config`.

    Credential precedence (API key vs. Claude-subscription OAuth) lives in
    :func:`carve.core.agents.client_factory.make_client`.
    """
    from carve.core.agents.client_factory import make_client

    return make_client(config, client)


def _collect_tools_invoked(agent_result: AgentResult) -> list[str]:
    """Pull the ordered list of tool names invoked across all assistant turns.

    Used by tests that assert on whether `lookup_skill` was called and
    by the build flow's metadata block.
    """
    invoked: list[str] = []
    for message in agent_result.messages:
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    invoked.append(name)
    return invoked


__all__ = [
    "ExtractLoadAgentError",
    "ExtractLoadResult",
    "load_extract_load_agent_prompt",
    "run_extract_load_agent",
]
