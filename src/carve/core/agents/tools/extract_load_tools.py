"""Tool factories for the extract-load specialist agent (P1-04).

Five tools:

1. ``read_file(path)`` — re-read existing files when modifying.
2. ``write_file(path, content)`` — scoped to a per-build allow-list.
   Only the sub-paths under ``el/<artifact>/`` (``main.py``,
   ``requirements.txt``, ``snowflake.sql``) are accepted; anything
   else raises `ToolExecutionError` *before* the file system is
   touched. (Pre-P1.1-01 the allow-list lived under
   ``targets/<active_target>/``; the flat layout collapsed it.)
3. ``lookup_skill(skill_name)`` — returns the markdown body of a named
   skill (`data_engineering` or `snowflake_destination`). Skills are
   *content* the agent appends to the conversation, not callable
   functions — this tool is a deliberately simple "read this markdown
   file and hand it back" operation.
4. ``run_snowflake_query(sql, limit)`` — read-only against the active
   target's Snowflake. Reuses M1.1-06's SELECT/SHOW/DESCRIBE guard via
   `make_run_snowflake_query_tool`.
5. ``submit_step(file_list, summary, error=False)`` — terminator tool.
   Mirrors `submit_plan`'s capture-then-exit pattern. The loop's
   `terminator_tool="submit_step"` ends the conversation as soon as
   the model calls this tool.

Path-allow-list discipline: the `make_write_file_tool` factory binds an
explicit set of allowed *resolved* paths at construction time. Each
runtime call resolves the request and asserts containment in that set
— defense-in-depth alongside M1.1-06's project-root containment check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from carve.core.agents.m1_tools import (
    SnowflakeQueryRunner,
)
from carve.core.agents.m1_tools import (
    make_read_file_tool as _make_read_file_tool,
)
from carve.core.agents.m1_tools import (
    make_run_snowflake_query_tool as _make_run_snowflake_query_tool,
)
from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult

# ---------------------------------------------------------------------------
# read_file (re-export under the same name — same project-root guard)
# ---------------------------------------------------------------------------


def make_read_file_tool(project_dir: Path) -> Tool:
    """Re-export the M1 ``read_file`` factory for clarity at import sites."""
    return _make_read_file_tool(project_dir)


# ---------------------------------------------------------------------------
# write_file (path-allow-listed)
# ---------------------------------------------------------------------------


WRITE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Path relative to the project root. Must be one of the "
                "three allowed sub-paths under el/<artifact>/."
            ),
        },
        "content": {
            "type": "string",
            "description": "Full file contents to write (UTF-8).",
        },
    },
    "required": ["path", "content"],
}


def make_write_file_tool(
    project_dir: Path,
    allowed_paths: set[Path],
) -> Tool:
    """Build a path-allow-listed `write_file` tool.

    ``allowed_paths`` is the set of resolved absolute paths the tool
    will accept. Every resolved candidate is checked against this set
    *before* any disk I/O. A request whose resolved path is not in the
    set raises `ToolExecutionError` with a message naming the
    permitted sub-paths so the agent can recover.

    Defense-in-depth: requests are also resolved relative to
    ``project_dir`` so absolute paths and ``..`` traversal both fail
    the containment check before they reach the allow-list comparison.
    """
    project_root = project_dir.resolve()
    allowed_resolved = {p.resolve() for p in allowed_paths}

    def _execute(input_: ToolInput) -> ToolResult:
        path = input_.get("path")
        content = input_.get("content")
        if not isinstance(path, str) or not path:
            raise ToolExecutionError("`path` must be a non-empty string.")
        if not isinstance(content, str):
            raise ToolExecutionError("`content` must be a string.")

        candidate = (project_root / path).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError as exc:
            raise ToolExecutionError(f"Path {path!r} is outside the project directory.") from exc

        if candidate not in allowed_resolved:
            permitted = sorted(str(p.relative_to(project_root)) for p in allowed_resolved)
            raise ToolExecutionError(
                f"Path {path!r} is not on the write allow-list. Allowed: {permitted}"
            )

        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"Failed to write {path}: {exc}") from exc
        return {"path": path, "bytes_written": len(content.encode("utf-8"))}

    return Tool(
        name="write_file",
        description=(
            "Write contents to one of the three allowed paths under "
            "el/<artifact>/: main.py, requirements.txt, snowflake.sql. "
            "Any other path is rejected. Creates parent directories as "
            "needed; overwrites if the file exists."
        ),
        input_schema=WRITE_FILE_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# lookup_skill (load markdown skill bodies on demand)
# ---------------------------------------------------------------------------


# Skills shipped with Pillar 1's extract-load specialist. The agent
# decides which to load per task; `lookup_skill` is the only way the
# content reaches the conversation. New skills land here as `name ->
# markdown-file-path` entries.
SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

LOOKUP_SKILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill_name": {
            "type": "string",
            "description": (
                "Name of the skill to load. One of: data_engineering, snowflake_destination."
            ),
            "enum": ["data_engineering", "snowflake_destination"],
        },
    },
    "required": ["skill_name"],
}

# Allowed skill names — duplicated as a constant so the executor can
# validate without re-parsing the schema. Matches the enum above.
_ALLOWED_SKILL_NAMES = frozenset({"data_engineering", "snowflake_destination"})


def make_lookup_skill_tool(skills_dir: Path | None = None) -> Tool:
    """Build a `lookup_skill` tool that returns markdown bodies.

    ``skills_dir`` defaults to ``carve/core/skills/`` (where the
    markdown skill files live as siblings to the registry's
    ``builtin/`` subpackage). Tests can pass a temp directory to
    isolate from production content.
    """
    base_dir = (skills_dir or SKILLS_DIR).resolve()

    def _execute(input_: ToolInput) -> ToolResult:
        name = input_.get("skill_name")
        if not isinstance(name, str) or not name:
            raise ToolExecutionError("`skill_name` must be a non-empty string.")
        if name not in _ALLOWED_SKILL_NAMES:
            raise ToolExecutionError(
                f"Unknown skill {name!r}. Allowed: {sorted(_ALLOWED_SKILL_NAMES)}"
            )
        path = base_dir / f"{name}.md"
        if not path.is_file():
            raise ToolExecutionError(f"Skill content not found on disk: {path}")
        return path.read_text(encoding="utf-8")

    return Tool(
        name="lookup_skill",
        description=(
            "Load the markdown body of a named skill into the "
            "conversation. Skills are reference content the agent "
            "consults when a task warrants it (Snowflake MERGE/VARIANT, "
            "complex pagination, watermark logic). Trivial tasks should "
            "not call this tool. Available skills: data_engineering, "
            "snowflake_destination."
        ),
        input_schema=LOOKUP_SKILL_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# run_snowflake_query (re-export under the same name)
# ---------------------------------------------------------------------------


def make_run_snowflake_query_tool(runner: SnowflakeQueryRunner) -> Tool:
    """Re-export the M1 read-only Snowflake query tool."""
    return _make_run_snowflake_query_tool(runner)


# ---------------------------------------------------------------------------
# submit_step (terminator)
# ---------------------------------------------------------------------------


SUBMIT_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_list": {
            "type": "array",
            "description": (
                "Paths (relative to the project root) the agent wrote "
                "this step. Empty when error=True."
            ),
            "items": {"type": "string"},
        },
        "summary": {
            "type": "string",
            "description": (
                "One- to three-sentence summary of what this step "
                "accomplished, or — when error=True — why the task "
                "could not be completed by this agent."
            ),
        },
        "error": {
            "type": "boolean",
            "default": False,
            "description": (
                "Set True when the task is out of scope or otherwise "
                "unfixable from this agent's seat (e.g. dbt-shaped goal "
                "mis-routed to extract-load). The build flow treats this "
                "as a hard failure."
            ),
        },
    },
    "required": ["file_list", "summary"],
}


@dataclass
class SubmitStepCapture:
    """Stateful container for the agent's `submit_step` payload.

    The extract-load agent terminates by calling `submit_step(...)` once.
    The build flow constructs a fresh `SubmitStepCapture`, hands it to
    `make_submit_step_tool`, runs the loop with
    ``terminator_tool="submit_step"``, and reads back ``capture.payload``
    after the loop exits.

    A second invocation within the same capture is rejected as a
    belt-and-braces guard against a model that emits two `submit_step`
    blocks in the same turn.
    """

    payload: dict[str, Any] | None = None
    _called: bool = field(default=False, init=False)

    @property
    def submitted(self) -> bool:
        return self.payload is not None

    @property
    def file_list(self) -> list[str]:
        if self.payload is None:
            return []
        files = self.payload.get("file_list")
        return list(files) if isinstance(files, list) else []

    @property
    def summary(self) -> str:
        if self.payload is None:
            return ""
        summary = self.payload.get("summary")
        return summary if isinstance(summary, str) else ""

    @property
    def error(self) -> bool:
        if self.payload is None:
            return False
        return bool(self.payload.get("error"))


def make_submit_step_tool(capture: SubmitStepCapture) -> Tool:
    """Build a `submit_step` tool that records the payload on `capture`."""

    def _execute(input_: ToolInput) -> ToolResult:
        if capture._called:
            raise ToolExecutionError(
                "submit_step already called; only one terminal payload "
                "may be submitted per agent invocation."
            )
        if not isinstance(input_, dict):
            raise ToolExecutionError("submit_step input must be an object.")
        # Defensive copy: the loop reuses the parsed input dict for
        # logging, and we don't want subsequent mutations to leak into
        # the captured payload.
        capture.payload = dict(input_)
        capture._called = True
        return {"status": "submitted"}

    return Tool(
        name="submit_step",
        description=(
            "Finalize this agent step. Call this once with the list of "
            "files written and a short summary. Set error=True with an "
            "empty file_list when the task is out of scope or otherwise "
            "unfixable from this agent. The loop terminates after this "
            "call."
        ),
        input_schema=SUBMIT_STEP_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# Bundle helper
# ---------------------------------------------------------------------------


@dataclass
class ExtractLoadTools:
    """Convenience bundle of the five tools plus the `submit_step` capture.

    Constructing this dataclass is the single entrypoint the agent
    module uses; it returns the toolset and the capture in one go so
    the build flow doesn't have to know about wiring details.
    """

    tools: list[Tool]
    submit_step_capture: SubmitStepCapture


def build_extract_load_tools(
    *,
    project_dir: Path,
    allowed_paths: set[Path],
    snowflake_runner: SnowflakeQueryRunner,
    skills_dir: Path | None = None,
) -> ExtractLoadTools:
    """Construct the extract-load specialist's full toolset.

    Args:
        project_dir: Resolved project root.
        allowed_paths: Set of absolute paths the `write_file` tool will
            accept (typically the three target sub-paths for the
            artifact under build).
        snowflake_runner: Read-only Snowflake runner for the active
            target. Pass `_UnconfiguredSnowflakeRunner` (or similar)
            when no target is configured.
        skills_dir: Optional override for the skills markdown directory
            (tests use this).
    """
    capture = SubmitStepCapture()
    tools = [
        make_read_file_tool(project_dir),
        make_write_file_tool(project_dir, allowed_paths),
        make_lookup_skill_tool(skills_dir),
        make_run_snowflake_query_tool(snowflake_runner),
        make_submit_step_tool(capture),
    ]
    return ExtractLoadTools(tools=tools, submit_step_capture=capture)


__all__ = [
    "LOOKUP_SKILL_SCHEMA",
    "SKILLS_DIR",
    "SUBMIT_STEP_SCHEMA",
    "WRITE_FILE_SCHEMA",
    "ExtractLoadTools",
    "SubmitStepCapture",
    "build_extract_load_tools",
    "make_lookup_skill_tool",
    "make_read_file_tool",
    "make_run_snowflake_query_tool",
    "make_submit_step_tool",
    "make_write_file_tool",
]
