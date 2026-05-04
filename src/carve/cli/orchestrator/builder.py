"""`carve build` orchestration.

Consumes a saved `Plan` (produced by `generate_plan` with
``phase='drafted'``) and runs the build agent to materialise
``pipelines/<name>/main.py`` and ``requirements.txt``. On success:

* Inserts/updates the `Pipeline` row keyed by name.
* Marks the plan ``phase='built'``, links it to the pipeline.
* Returns a `BuildArtifact` with the run id and file list.

The build agent is narrower than the plan agent: only `read_file` and
`write_file`. It receives the design as a markdown preamble in the
system prompt so the file generation has every decision pinned.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from carve.core.agents import (
    AgentLoop,
    AgentObserver,
    NullObserver,
    Tool,
    load_m1_build_agent_prompt,
    make_read_file_tool,
    make_write_file_tool,
)
from carve.core.config import Config, ConfigError
from carve.core.state import Plan, Repository

logger = logging.getLogger(__name__)


@dataclass
class BuildArtifact:
    """Result of `build_plan`.

    Captures what the build run produced for the CLI's summary block.
    `success` is False when the build agent finished but didn't write a
    `main.py`; the build run row is marked failed in that case and the
    plan stays drafted.
    """

    plan_id: str
    pipeline_name: str
    pipeline_dir: str
    files_written: list[str]
    summary: str
    run_id: str
    success: bool
    tokens_input: int
    tokens_output: int
    cost_usd: float


class BuildError(Exception):
    """Raised when a build can't proceed (bad plan state, etc.)."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_plan(
    plan_id: str,
    config: Config,
    project_dir: Path,
    *,
    repository: Repository,
    client: Any | None = None,
    max_turns: int = 30,
    observer: AgentObserver | None = None,
    force: bool = False,
) -> BuildArtifact:
    """Build the pipeline files described by ``plan_id``.

    Args:
        plan_id: Id of a previously-generated plan.
        config: Fully-loaded `Config`.
        project_dir: Resolved project root.
        repository: State-store repository.
        client: Optional pre-built Anthropic client (used in tests).
        max_turns: Cap on agent turns.
        observer: Optional progress observer.
        force: When True, allow rebuilding a plan that's already in
            ``phase='built'``. Default False refuses the rebuild and
            tells the user to refine and re-build instead.

    Raises:
        BuildError: Plan doesn't exist or is in the wrong phase without
            ``--force``.
        ConfigError: Anthropic API key missing.
    """
    project_dir = project_dir.resolve()

    plan_row = repository.get_plan(plan_id)
    if plan_row is None:
        raise BuildError(f"Plan {plan_id!r} not found.")

    if plan_row.phase != "drafted" and not force:
        raise BuildError(
            f"Plan {plan_id!r} is already in phase {plan_row.phase!r}. "
            "Use `--force` to rebuild, or refine the plan and try again."
        )

    design = _load_plan_design(plan_row)
    pipeline_name = _pipeline_name_from(plan_row, design)

    anthropic_client = _build_client(config, client)

    pipeline_dir_rel = f"pipelines/{pipeline_name}"
    pipeline_dir_abs = project_dir / pipeline_dir_rel
    snapshot = _snapshot_pipeline_dir(pipeline_dir_abs)

    # The Pipeline row is upserted at the end of a successful build, so it
    # doesn't exist yet at create_run time. Pass pipeline_name=None to keep
    # the runs.pipeline_name FK happy (the column is nullable specifically
    # for this case — see M1.1-06 spec, "runs table changes"). After the
    # pipeline lands we backfill via _attach_pipeline_to_run below.
    run_id = repository.create_run(
        kind="build",
        target_id=plan_id,
        pipeline_name=None,
    )
    repository.update_run_status(run_id, "running")

    tools = _build_tools(project_dir)
    system_prompt = _compose_build_system_prompt(
        config=config,
        project_dir=project_dir,
        design=design,
        pipeline_name=pipeline_name,
        target_pipeline=plan_row.pipeline_name,
    )

    loop = AgentLoop(
        client=anthropic_client,
        tools=tools,
        system_prompt=system_prompt,
        model=config.models.default_model,
        repository=repository,
        run_id=run_id,
        observer=observer if observer is not None else NullObserver(),
    )

    initial_message = (
        f"Build the pipeline `{pipeline_name}`. Write `pipelines/"
        f"{pipeline_name}/main.py` and `pipelines/{pipeline_name}/"
        f"requirements.txt` per the design preamble."
    )

    try:
        agent_result = loop.run(initial_message, max_turns=max_turns)
    except Exception as exc:
        repository.update_run_status(run_id, "failed", error=str(exc))
        raise

    written_files = _changed_files(pipeline_dir_abs, snapshot)
    main_py = next((p for p in written_files if p.name == "main.py"), None)
    requirements_txt = next(
        (p for p in written_files if p.name == "requirements.txt"),
        None,
    )

    if main_py is None:
        repository.update_run_status(
            run_id,
            "failed",
            error="Build agent finished without writing main.py.",
        )
        return BuildArtifact(
            plan_id=plan_id,
            pipeline_name=pipeline_name,
            pipeline_dir=pipeline_dir_rel,
            files_written=sorted(p.relative_to(project_dir).as_posix() for p in written_files),
            summary=agent_result.text,
            run_id=run_id,
            success=False,
            tokens_input=agent_result.token_usage.input_tokens,
            tokens_output=agent_result.token_usage.output_tokens,
            cost_usd=agent_result.token_usage.cost_usd(config.models.default_model),
        )

    # Synthesise requirements.txt if the agent forgot, mirroring the
    # legacy planner's defensive default. The build agent's prompt asks
    # for one explicitly, so this is rare.
    if requirements_txt is None:
        synthesised = pipeline_dir_abs / "requirements.txt"
        synthesised.parent.mkdir(parents=True, exist_ok=True)
        requirements_text = "\n".join(_extract_requirements(design)) + "\n"
        synthesised.write_text(requirements_text, encoding="utf-8")
        written_files.append(synthesised.resolve())
        logger.warning(
            "build agent did not write requirements.txt; synthesised default at %s",
            synthesised,
        )

    repository.create_or_update_pipeline(
        name=pipeline_name,
        description=_design_description(design),
        pipeline_dir=pipeline_dir_rel,
        current_plan_id=plan_id,
    )
    # Pipeline now exists; safe to backfill the build run's FK so this
    # build shows up in `runs --pipeline <name>` filters.
    repository.attach_pipeline_to_run(run_id, pipeline_name)
    repository.mark_plan_built(
        plan_id=plan_id,
        pipeline_name=pipeline_name,
        build_run_id=run_id,
    )
    repository.update_run_status(run_id, "success")

    files_written_rel = sorted(
        p.relative_to(project_dir).as_posix() for p in written_files
    )
    return BuildArtifact(
        plan_id=plan_id,
        pipeline_name=pipeline_name,
        pipeline_dir=pipeline_dir_rel,
        files_written=files_written_rel,
        summary=agent_result.text,
        run_id=run_id,
        success=True,
        tokens_input=agent_result.token_usage.input_tokens,
        tokens_output=agent_result.token_usage.output_tokens,
        cost_usd=agent_result.token_usage.cost_usd(config.models.default_model),
    )


# ---------------------------------------------------------------------------
# Plan/design helpers
# ---------------------------------------------------------------------------


_PIPELINE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _load_plan_design(plan_row: Plan) -> dict[str, Any]:
    """Pull the design dict out of the plan's stored task_graph JSON."""
    try:
        task_graph = json.loads(plan_row.task_graph_json or "{}")
    except (TypeError, ValueError) as exc:
        raise BuildError(
            f"Plan {plan_row.id!r} has malformed task_graph_json: {exc}"
        ) from exc
    design = task_graph.get("design")
    if not isinstance(design, dict):
        raise BuildError(
            f"Plan {plan_row.id!r} is missing a `design` block; "
            "the planner did not store one. Re-plan to recover."
        )
    return design


def _pipeline_name_from(plan_row: Plan, design: dict[str, Any]) -> str:
    """Resolve the canonical pipeline name for the build.

    Preference order:

    1. The plan row's ``pipeline_name`` if set (the planner stamps this
       when ``--pipeline`` was used).
    2. The design's ``pipeline_name``.
    """
    candidate = plan_row.pipeline_name or design.get("pipeline_name")
    if not isinstance(candidate, str) or not _PIPELINE_NAME_RE.match(candidate):
        raise BuildError(
            "Could not resolve a valid pipeline name for this build. "
            f"Got {candidate!r}."
        )
    return candidate


def _design_description(design: dict[str, Any]) -> str:
    raw = design.get("description")
    if isinstance(raw, str):
        return raw
    return ""


def _extract_requirements(design: dict[str, Any]) -> list[str]:
    raw = design.get("requirements")
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    return ["snowflake-connector-python"]


# ---------------------------------------------------------------------------
# System prompt assembly
# ---------------------------------------------------------------------------


def _compose_build_system_prompt(
    *,
    config: Config,
    project_dir: Path,
    design: dict[str, Any],
    pipeline_name: str,
    target_pipeline: str | None,
) -> str:
    """Base prompt + connection context + design block + (optional) existing files."""
    sections: list[str] = [load_m1_build_agent_prompt()]
    sections.append(_render_connection_context(config))
    sections.append(_render_design_preamble(design))
    if target_pipeline is not None:
        existing = _render_existing_pipeline_section(project_dir, pipeline_name)
        if existing is not None:
            sections.append(existing)
    return "\n\n".join(sections)


def _render_connection_context(config: Config) -> str:
    target = config.project.default_target
    snowflake = config.connections.snowflake.get(target)
    lines = [
        "## Connection context",
        f"- **Active target:** `{target}`",
    ]
    if snowflake is None:
        lines.append("- **Snowflake connection:** _(none configured)_")
        return "\n".join(lines)
    lines.append(f"- **Database:** `{snowflake.database}`")
    if snowflake.schema_:
        lines.append(f"- **Schema:** `{snowflake.schema_}`")
    lines.append(f"- **Warehouse:** `{snowflake.warehouse}`")
    lines.append(f"- **Role:** `{snowflake.role}`")
    return "\n".join(lines)


def _render_design_preamble(design: dict[str, Any]) -> str:
    """Render the design dict as readable markdown for the build agent.

    Tables for the structured fields, fenced JSON for the parts where a
    table would obscure shape (open questions, columns). The build agent
    is told to honor every decision in this block verbatim.
    """
    lines: list[str] = ["## Design", "Honor every decision in this block. Do not redesign."]

    pipeline_name = design.get("pipeline_name", "")
    description = design.get("description", "")
    lines.append("")
    lines.append(f"- **pipeline_name:** `{pipeline_name}`")
    if description:
        lines.append(f"- **description:** {description}")

    destination = design.get("destination")
    if isinstance(destination, dict):
        lines.append("")
        lines.append("### Destination")
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        for key in ("database", "schema", "table", "primary_key"):
            value = destination.get(key)
            if value is not None:
                lines.append(f"| `{key}` | `{value}` |")

    transformation = design.get("transformation")
    if isinstance(transformation, dict):
        lines.append("")
        lines.append("### Transformation")
        strategy = transformation.get("strategy", "")
        rationale = transformation.get("rationale", "")
        if strategy:
            lines.append(f"- **strategy:** `{strategy}`")
        if rationale:
            lines.append(f"- **rationale:** {rationale}")

    source = design.get("source")
    if isinstance(source, dict):
        lines.append("")
        lines.append("### Source")
        lines.append("```json")
        lines.append(json.dumps(source, indent=2, sort_keys=True))
        lines.append("```")

    columns = design.get("columns")
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

    requirements = design.get("requirements")
    if isinstance(requirements, list) and requirements:
        lines.append("")
        lines.append("### Requirements")
        for item in requirements:
            lines.append(f"- {item}")

    tradeoffs = design.get("tradeoffs")
    if isinstance(tradeoffs, list) and tradeoffs:
        lines.append("")
        lines.append("### Tradeoffs (acknowledged)")
        for item in tradeoffs:
            lines.append(f"- {item}")

    return "\n".join(lines)


def _render_existing_pipeline_section(
    project_dir: Path,
    pipeline_name: str,
) -> str | None:
    pipeline_dir = project_dir / "pipelines" / pipeline_name
    main_py = pipeline_dir / "main.py"
    requirements = pipeline_dir / "requirements.txt"
    if not main_py.is_file():
        return None
    parts: list[str] = [
        f"## Existing pipeline `{pipeline_name}` (current state)",
        "Use this for reference; the design above is authoritative for "
        "the new state.",
        "",
        f"### `pipelines/{pipeline_name}/main.py`",
        "```python",
        main_py.read_text(encoding="utf-8").rstrip("\n"),
        "```",
    ]
    if requirements.is_file():
        parts += [
            "",
            f"### `pipelines/{pipeline_name}/requirements.txt`",
            "```",
            requirements.read_text(encoding="utf-8").rstrip("\n"),
            "```",
        ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _build_tools(project_dir: Path) -> list[Tool]:
    """Build-agent toolset: read_file + write_file. No SQL."""
    return [
        make_read_file_tool(project_dir),
        make_write_file_tool(project_dir),
    ]


def _build_client(config: Config, client: Any | None) -> Any:
    if client is not None:
        return client
    api_key = config.models.anthropic_api_key
    if api_key is None:
        raise ConfigError(
            "Anthropic API key is required to build a plan but is unset.",
            file="carve/models.toml",
            field="models.anthropic_api_key",
            hint=(
                "Uncomment `anthropic_api_key = \"${ANTHROPIC_API_KEY}\"` in "
                "carve/models.toml and set ANTHROPIC_API_KEY in your "
                "environment (or .env)."
            ),
        )
    return anthropic.Anthropic(api_key=api_key)


# ---------------------------------------------------------------------------
# Pipeline directory snapshot/diff
# ---------------------------------------------------------------------------


def _snapshot_pipeline_dir(pipeline_dir: Path) -> dict[Path, float]:
    """Return ``{path: mtime}`` for every file under ``pipeline_dir``."""
    if not pipeline_dir.is_dir():
        return {}
    return {
        path.resolve(): path.stat().st_mtime
        for path in pipeline_dir.rglob("*")
        if path.is_file()
    }


def _changed_files(
    pipeline_dir: Path,
    snapshot: dict[Path, float],
) -> list[Path]:
    """Return files added or modified since `snapshot`.

    On rebuild against an existing pipeline, the build agent will
    overwrite both files; both should appear in the result. If the agent
    re-writes byte-identical content the mtime still updates, so the
    diff catches that too.
    """
    if not pipeline_dir.is_dir():
        return []
    changed: list[Path] = []
    for path in pipeline_dir.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        prev_mtime = snapshot.get(resolved)
        cur_mtime = path.stat().st_mtime
        if prev_mtime is None or cur_mtime > prev_mtime:
            changed.append(resolved)
    return changed


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = ["BuildArtifact", "BuildError", "build_plan"]
