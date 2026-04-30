"""`carve plan` orchestration.

Wires the merged Carve config, the state store, the Anthropic agent
loop, and (optionally) the Snowflake connector into a single
`generate_plan` call. The result is a `PlanArtifact` that captures
everything needed to apply the plan later: the agent's summary, the
script and requirements files written under ``pipelines/``, the token
counts, and the cost estimate.

Design notes:

* Plan generation is *not* a "run" in the state-store sense. Cost is
  recorded on the plan row's ``estimates_json`` field, and no row is
  written to ``runs``.
* The Snowflake tool is optional: if no Snowflake connection is
  configured for the active target, we log a warning and fall back to a
  stub runner whose only job is to surface a clear error if the agent
  invokes the tool. This lets users run ``carve plan`` against a
  partially-configured project without the planner blowing up at tool-
  build time.
* The agent's ``write_file`` tool reaches into the project tree. We
  diff a snapshot of files under ``pipelines/`` taken before vs. after
  the loop to figure out which directory the agent settled on. This
  beats trying to parse the agent's prose for path hints.
* The requirements.txt instruction lives in the M1 system prompt
  itself (``carve.core.agents.prompts.m1_code_agent``); the planner
  loads that prompt verbatim and does not append integration text on
  top. Keep prompt edits in the .md file.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic

from carve.core.agents import (
    AgentLoop,
    Tool,
    ToolExecutionError,
    build_m1_tools,
    load_m1_code_agent_prompt,
    make_read_file_tool,
    make_run_snowflake_query_tool,
    make_write_file_tool,
)
from carve.core.agents.loop import TokenUsage
from carve.core.config import Config, ConfigError
from carve.core.connectors.exceptions import SnowflakeError
from carve.core.connectors.snowflake import SnowflakePool
from carve.core.state import Plan, Repository
from carve.version import __version__ as CARVE_VERSION

logger = logging.getLogger(__name__)


# Canonical plan-id format: `plan_YYYYMMDD_HHMMSS_<6 hex>`. Lives here
# (the producer) and is re-exported to the applier for an explicit
# format check at the apply boundary.
PLAN_ID_RE = re.compile(r"^plan_\d{8}_\d{6}_[0-9a-f]{6}$")


@dataclass
class PlanArtifact:
    """Result of `generate_plan`.

    The fields mirror the on-disk plan JSON shape one-for-one. The
    `to_json()` helper serialises to the canonical format the applier
    reads back later.
    """

    id: str
    goal: str
    summary: str
    pipeline_name: str
    pipeline_dir: str
    script_path: str
    requirements_path: str
    requirements: list[str]
    files_written: list[str]
    config_hash: str
    carve_version: str
    tokens_input: int
    tokens_output: int
    cost_usd: float
    model: str
    created_at: datetime
    expires_at: datetime
    file_path: Path = field(default_factory=lambda: Path())

    def to_json(self) -> dict[str, Any]:
        """Serialise for the on-disk plan JSON file."""
        return {
            "id": self.id,
            "goal": self.goal,
            "summary": self.summary,
            "pipeline_name": self.pipeline_name,
            "pipeline_dir": self.pipeline_dir,
            "script_path": self.script_path,
            "requirements_path": self.requirements_path,
            "requirements": list(self.requirements),
            "files_written": list(self.files_written),
            "config_hash": self.config_hash,
            "carve_version": self.carve_version,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "cost_usd": self.cost_usd,
            "model": self.model,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
        }


class PlanGenerationError(Exception):
    """Raised when plan generation produces no usable script."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_plan(
    goal: str,
    config: Config,
    project_dir: Path,
    *,
    repository: Repository,
    client: Any | None = None,
    max_turns: int = 30,
) -> PlanArtifact:
    """Run the M1 code agent and persist the resulting plan.

    Args:
        goal: Natural-language goal from the user.
        config: Fully-loaded `Config`.
        project_dir: Resolved project root.
        repository: State-store repository (plan row gets saved here).
        client: Optional pre-built Anthropic client (used in tests).
            Production callers pass ``None`` and let us build one from
            ``config.models.anthropic_api_key``.
        max_turns: Cap on agent turns. Same default as `AgentLoop.run`.

    Returns:
        `PlanArtifact` with `file_path` populated.

    Raises:
        PlanGenerationError: Agent didn't write a `main.py`.
    """
    project_dir = project_dir.resolve()
    plan_id = _new_plan_id()
    model = config.models.default_model
    if client is not None:
        anthropic_client: Any = client
    else:
        api_key = config.models.anthropic_api_key
        if api_key is None:
            raise ConfigError(
                "Anthropic API key is required to generate a plan but is unset.",
                file="carve/models.toml",
                field="models.anthropic_api_key",
                hint=(
                    "Uncomment `anthropic_api_key = \"${ANTHROPIC_API_KEY}\"` in "
                    "carve/models.toml and set ANTHROPIC_API_KEY in your "
                    "environment (or .env)."
                ),
            )
        anthropic_client = anthropic.Anthropic(api_key=api_key)

    tools = _build_tools(config, project_dir)

    system_prompt = load_m1_code_agent_prompt()

    pipelines_root = project_dir / "pipelines"
    snapshot = _snapshot_pipelines(pipelines_root)

    # `AgentLoop` types its client as the `_AnthropicLike` Protocol; both
    # the real `anthropic.Anthropic` instance and the test `MagicMock`
    # satisfy it structurally. The local `anthropic_client` is typed
    # `Any` so the call passes mypy's strict mode without an extra cast.
    loop = AgentLoop(
        client=anthropic_client,
        tools=tools,
        system_prompt=system_prompt,
        model=model,
    )
    agent_result = loop.run(goal, max_turns=max_turns)

    written_files = _changed_files(pipelines_root, snapshot)
    main_py, requirements_txt = _identify_pipeline_files(written_files, project_dir)
    if main_py is None:
        raise PlanGenerationError(
            "The agent did not write a pipeline `main.py`. "
            "Try rephrasing the goal or check the agent's response for errors."
        )

    pipeline_dir_abs = main_py.parent
    pipeline_name = pipeline_dir_abs.name
    pipeline_dir_rel = pipeline_dir_abs.relative_to(project_dir).as_posix()
    script_path_rel = main_py.relative_to(project_dir).as_posix()

    if requirements_txt is not None:
        requirements_path_rel = requirements_txt.relative_to(project_dir).as_posix()
        requirements = _parse_requirements(requirements_txt)
    else:
        # The agent occasionally forgets the requirements file. Synthesise
        # a sensible default so apply still works against Snowflake.
        requirements_path_rel = (
            (pipeline_dir_abs / "requirements.txt")
            .relative_to(project_dir)
            .as_posix()
        )
        requirements = ["snowflake-connector-python"]
        logger.warning(
            "agent did not produce requirements.txt; using default: %s",
            requirements,
        )

    files_written_rel = sorted(p.relative_to(project_dir).as_posix() for p in written_files)

    now = _utcnow()
    expires = now + timedelta(hours=24)
    cost = agent_result.token_usage.cost_usd(model)
    artifact = PlanArtifact(
        id=plan_id,
        goal=goal,
        summary=agent_result.text,
        pipeline_name=pipeline_name,
        pipeline_dir=pipeline_dir_rel,
        script_path=script_path_rel,
        requirements_path=requirements_path_rel,
        requirements=requirements,
        files_written=files_written_rel,
        config_hash=config.config_hash,
        carve_version=CARVE_VERSION,
        tokens_input=agent_result.token_usage.input_tokens,
        tokens_output=agent_result.token_usage.output_tokens,
        cost_usd=cost,
        model=model,
        created_at=now,
        expires_at=expires,
    )
    artifact.file_path = _persist_artifact(artifact, project_dir, repository)
    return artifact


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class _UnconfiguredSnowflakeRunner:
    """Stub runner for the case where no Snowflake target is configured.

    Returning a stub (instead of dropping the tool entirely) keeps the
    Anthropic schema stable; the agent gets a clear error if it tries
    to use the tool but isn't kept guessing at why a documented tool is
    missing.
    """

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
        raise ToolExecutionError(
            "No Snowflake connection is configured for the active target. "
            "Add a [connections.snowflake.<target>] block to "
            "carve/connections.toml and re-run."
        )


def _build_tools(config: Config, project_dir: Path) -> list[Tool]:
    """Build the M1 tool set for `generate_plan`."""
    target = config.project.default_target
    if target in config.connections.snowflake:
        pool = SnowflakePool(config)
        try:
            sf_runner = pool.get(target)
        except SnowflakeError:
            logger.warning(
                "Snowflake target %r is configured but unavailable; "
                "the agent will get an error if it uses run_snowflake_query.",
                target,
            )
            return [
                make_read_file_tool(project_dir),
                make_write_file_tool(project_dir),
                make_run_snowflake_query_tool(_UnconfiguredSnowflakeRunner()),
            ]
        return build_m1_tools(project_dir, sf_runner)

    logger.warning(
        "no Snowflake connection configured for target %r; "
        "the agent's run_snowflake_query tool will return an error if used.",
        target,
    )
    return [
        make_read_file_tool(project_dir),
        make_write_file_tool(project_dir),
        make_run_snowflake_query_tool(_UnconfiguredSnowflakeRunner()),
    ]


# ---------------------------------------------------------------------------
# Plan-id generation
# ---------------------------------------------------------------------------


def _new_plan_id() -> str:
    """Build a plan id of the form `plan_YYYYMMDD_HHMMSS_<6hex>`."""
    stamp = _utcnow().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"plan_{stamp}_{suffix}"


# ---------------------------------------------------------------------------
# Filesystem snapshot / diff
# ---------------------------------------------------------------------------


def _snapshot_pipelines(pipelines_root: Path) -> dict[Path, float]:
    """Return ``{path: mtime}`` for every file under `pipelines/`.

    Used to detect both *new* files and *modified* files after the
    agent runs.
    """
    if not pipelines_root.is_dir():
        return {}
    snapshot: dict[Path, float] = {}
    for path in pipelines_root.rglob("*"):
        if path.is_file():
            snapshot[path.resolve()] = path.stat().st_mtime
    return snapshot


def _changed_files(
    pipelines_root: Path,
    snapshot: dict[Path, float],
) -> list[Path]:
    """Return absolute paths of files added or modified since `snapshot`."""
    if not pipelines_root.is_dir():
        return []
    changed: list[Path] = []
    for path in pipelines_root.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        prev_mtime = snapshot.get(resolved)
        cur_mtime = path.stat().st_mtime
        if prev_mtime is None or cur_mtime > prev_mtime:
            changed.append(resolved)
    return changed


def _identify_pipeline_files(
    files: list[Path],
    project_dir: Path,
) -> tuple[Path | None, Path | None]:
    """Pick the most-recently-written ``main.py`` and its sibling requirements.

    Returns ``(main_py, requirements_txt)``. Either may be ``None`` if
    the agent didn't write that file.
    """
    project_resolved = project_dir.resolve()
    mains = [
        p for p in files
        if p.name == "main.py"
        and _is_under(p, project_resolved / "pipelines")
    ]
    if not mains:
        return None, None
    mains.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    main_py = mains[0]
    requirements_txt = main_py.parent / "requirements.txt"
    if not requirements_txt.is_file():
        # Maybe the agent wrote it under a different sibling location; try
        # to find one in the same directory among the changed files.
        candidates = [
            p for p in files
            if p.parent == main_py.parent and p.name == "requirements.txt"
        ]
        requirements_txt = candidates[0] if candidates else None  # type: ignore[assignment]
    return main_py, requirements_txt


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _parse_requirements(path: Path) -> list[str]:
    """Read a requirements.txt and return non-empty, non-comment lines.

    Strips any line that looks like a flag (starts with ``-``) since the
    M1 step config rejects those at validation time. We could let it
    fail later, but a clear filter here makes the output more useful.
    """
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            logger.warning(
                "skipping flag-shaped requirement %r from %s; M1 only "
                "accepts plain package specs.",
                line,
                path,
            )
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_artifact(
    artifact: PlanArtifact,
    project_dir: Path,
    repository: Repository,
) -> Path:
    """Write the plan JSON to disk and insert the index row.

    Returns the absolute path to the JSON file, suitable for storing on
    `Plan.file_path`.
    """
    plans_dir = project_dir / ".carve" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    file_path = plans_dir / f"{artifact.id}.json"

    payload = artifact.to_json()
    file_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    estimates = {
        "tokens_input": artifact.tokens_input,
        "tokens_output": artifact.tokens_output,
        "cost_usd": artifact.cost_usd,
        "model": artifact.model,
    }
    task_graph = {
        "pipeline_dir": artifact.pipeline_dir,
        "script_path": artifact.script_path,
        "requirements_path": artifact.requirements_path,
        "requirements": list(artifact.requirements),
        "files_written": list(artifact.files_written),
    }

    plan_row = Plan(
        id=artifact.id,
        goal=artifact.goal,
        config_hash=artifact.config_hash,
        carve_version=artifact.carve_version,
        estimates_json=json.dumps(estimates, sort_keys=True),
        task_graph_json=json.dumps(task_graph, sort_keys=True),
        file_path=str(file_path),
        created_at=artifact.created_at.replace(tzinfo=None),
        expires_at=artifact.expires_at.replace(tzinfo=None),
    )
    repository.save_plan(plan_row)
    return file_path


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Aware UTC `now()` — model-friendly for tests that compare timestamps."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    """ISO-8601 with explicit UTC suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


# Used by `TokenUsage` test sanity but not directly imported here.
__all__ = [
    "PLAN_ID_RE",
    "PlanArtifact",
    "PlanGenerationError",
    "TokenUsage",
    "generate_plan",
]
