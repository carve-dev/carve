"""`carve plan` orchestration — design only, no files written.

Wires the merged Carve config, the state store, the Anthropic agent
loop, and (optionally) the Snowflake connector into a single
`generate_plan` call. The result is a `PlanArtifact` capturing the
design document the agent submitted plus the bookkeeping needed for the
plan row (token counts, cost, hashes).

M1.1-06 split the original "plan = design + code" agent into two:

- `generate_plan` (this module) — runs the plan agent. Tools:
  `read_file`, `run_snowflake_query`, `submit_plan`. Produces a Plan
  row with `phase="drafted"`. **No files are written under
  ``el/<name>/`` (or any other artifact path).**
- `build_plan` (`builder.py`) — runs the build agent against a saved
  draft plan to materialise the pipeline directory.

The planner also handles two refinement modes triggered from the CLI:

- ``parent_plan_id`` set on `generate_plan` injects the parent plan's
  goal + design as agent context; the new user message is the user's
  feedback. The new plan is persisted with ``parent_plan_id``.
- ``pipeline_name`` (without ``parent_plan_id``) loads existing
  ``el/<name>/main.py`` and ``requirements.txt`` into the agent
  context so the design proposes a delta consistent with the live
  code (with a one-version fallback to the legacy P1-02
  ``targets/<active>/el/<name>/`` layout — removed in v0.2).
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

from carve.cli.orchestrator.extensibility_wiring import (
    build_extensibility_hooks,
    build_skill_pack_tool,
    resolve_agent_or_fallback,
)
from carve.core.agents import (
    AgentLoop,
    AgentObserver,
    NullObserver,
    SubmitPlanCapture,
    Tool,
    ToolExecutionError,
    load_m1_plan_agent_prompt,
    make_read_file_tool,
    make_run_snowflake_query_tool,
    make_submit_plan_tool,
)
from carve.core.agents.loop import TokenUsage
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.config import Config
from carve.core.connectors.exceptions import SnowflakeError
from carve.core.connectors.snowflake import SnowflakePool
from carve.core.skills import (
    CachedSkillExecutor,
    SkillContext,
    SkillRegistry,
    load_builtin_skills,
)
from carve.core.state import Plan, Repository
from carve.core.targets.names import (
    InvalidArtifactNameError,
    validate_artifact_name,
)
from carve.version import __version__ as CARVE_VERSION

logger = logging.getLogger(__name__)


# Canonical plan-id format: `plan_YYYYMMDD_HHMMSS_<6 hex>`. Lives here
# (the producer) and is re-exported to the runner for an explicit
# format check at the run-by-plan boundary.
PLAN_ID_RE = re.compile(r"^plan_\d{8}_\d{6}_[0-9a-f]{6}$")


@dataclass
class PlanArtifact:
    """Result of `generate_plan`.

    Captures the design the plan agent submitted plus persistence
    metadata. The on-disk JSON file is written to
    ``.carve/plans/<id>.json`` and round-trips through the planner's
    `to_json()` helper.
    """

    id: str
    goal: str
    design: dict[str, Any]
    pipeline_name: str
    description: str
    requirements: list[str]
    parent_plan_id: str | None
    target_pipeline: str | None
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
            "design": self.design,
            "pipeline_name": self.pipeline_name,
            "description": self.description,
            "requirements": list(self.requirements),
            "parent_plan_id": self.parent_plan_id,
            "target_pipeline": self.target_pipeline,
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
    """Raised when plan generation does not produce a valid design."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_plan(
    goal: str,
    config: Config,
    project_dir: Path,
    *,
    repository: Repository,
    target: str | None = None,
    client: Any | None = None,
    max_turns: int = 30,
    observer: AgentObserver | None = None,
    parent_plan_id: str | None = None,
    pipeline_name: str | None = None,
    destination_hint: dict[str, str] | None = None,
) -> PlanArtifact:
    """Run the plan agent and persist the resulting design.

    Args:
        goal: Natural-language goal from the user. For ``--refine`` this
            is the new user feedback; for ``--pipeline`` it's the change
            the user wants applied.
        config: Fully-loaded `Config`.
        project_dir: Resolved project root.
        repository: State-store repository.
        client: Optional pre-built Anthropic client (used in tests).
        max_turns: Cap on agent turns.
        observer: Optional progress observer.
        parent_plan_id: When set, the new plan is recorded as a refinement
            of this parent. The parent's goal + design are pulled into the
            agent context.
        pipeline_name: When set (without ``parent_plan_id``), the design
            targets an existing pipeline; existing files are loaded into
            the agent's context so the proposed change stays consistent.

    Raises:
        PlanGenerationError: Agent didn't call ``submit_plan`` or the
            submitted design failed validation.
        ConfigError: The Anthropic API key is missing (load-time-optional,
            use-time-required).
    """
    # Local imports to avoid a circular dependency with `carve.cli.main`.
    from carve.cli.main import ACTIVE_TARGET_FLAG
    from carve.core.targets.resolution import resolve_active_target

    project_dir = project_dir.resolve()
    plan_id = _new_plan_id()
    model = config.models.default_model
    anthropic_client = _build_client(config, client)

    cli_flag = target if target is not None else ACTIVE_TARGET_FLAG
    active_target = resolve_active_target(cli_flag, config)

    parent_plan_row: Plan | None = None
    if parent_plan_id is not None:
        parent_plan_row = repository.get_plan(parent_plan_id)
        if parent_plan_row is None:
            raise PlanGenerationError(
                f"Parent plan {parent_plan_id!r} not found; "
                "cannot refine a plan that doesn't exist."
            )
        if parent_plan_row.phase != "drafted":
            raise PlanGenerationError(
                f"Plan {parent_plan_id!r} is already in phase "
                f"{parent_plan_row.phase!r}. Refinement is only valid for "
                "drafted plans; modify the pipeline directly via "
                "`carve plan --pipeline <name>`."
            )

    target_pipeline = pipeline_name
    if pipeline_name is not None and parent_plan_row is None:
        existing = repository.get_pipeline(pipeline_name)
        if existing is None:
            raise PlanGenerationError(
                f"Pipeline {pipeline_name!r} not found. Use "
                '`carve plan "<goal>"` to design a new pipeline.'
            )

    capture = SubmitPlanCapture()
    # Build one SnowflakePool per invocation; share it with both the
    # run_snowflake_query tool and the SkillContext so they hit the same
    # per-target connection cache (one connect per target, not two).
    snowflake_pool = SnowflakePool(config)
    tools = _build_tools(
        config,
        project_dir,
        capture,
        active_target=active_target,
        snowflake_pool=snowflake_pool,
    )
    # Extensibility (spec 16): make discovered skill packs loadable at
    # runtime by adding the content-injection lookup tool to the agent's
    # tool list. Discovery is inert (no bundled script runs at load).
    tools.append(build_skill_pack_tool(project_dir=project_dir, paths=config.paths))

    # Extensibility (spec 16): route through the classification router. With
    # no classification the router falls back to None — the M1 plan flow is
    # preserved unchanged — but the seam is live, so a declarative agent
    # that matches would be selected here before delegating.
    _routed_agent = resolve_agent_or_fallback(
        project_dir=project_dir,
        paths=config.paths,
        classification=None,
    )
    if _routed_agent is not None:
        logger.debug("Router selected agent %r for plan flow.", _routed_agent)

    # Extensibility (spec 16): load carve/hooks.toml and clamp the hook
    # runner to the plan flow's mode. A missing file yields no hooks. Hooks
    # fire at the loop's pre/post-tool seam, after the gate admits a call.
    pre_tool_hook, post_tool_hook = build_extensibility_hooks(
        project_dir=project_dir,
        paths=config.paths,
        mode=PermissionMode.PLAN,
    )

    # Resolve user-specified destination: CLI flags first, then
    # FQN parsed from the goal text. The combined dict is the
    # "instruction" the system prompt advertises to the agent and
    # the floor we enforce after submit_plan.
    user_destination = _resolve_user_destination(goal=goal, destination_hint=destination_hint)

    system_prompt = _compose_plan_system_prompt(
        config=config,
        project_dir=project_dir,
        parent_plan_row=parent_plan_row,
        target_pipeline=target_pipeline,
        active_target=active_target,
        user_destination=user_destination,
    )

    skills_registry, skill_executor, skill_context = _build_skills(
        config=config,
        repository=repository,
        active_target=active_target,
        snowflake_pool=snowflake_pool,
    )

    loop = AgentLoop(
        client=anthropic_client,
        tools=tools,
        system_prompt=system_prompt,
        model=model,
        observer=observer if observer is not None else NullObserver(),
        terminator_tool="submit_plan",
        skills=skills_registry,
        skill_executor=skill_executor,
        skill_context=skill_context,
        pre_tool_hook=pre_tool_hook,
        post_tool_hook=post_tool_hook,
    )
    initial_user_message = _compose_initial_user_message(
        goal=goal,
        parent_plan_row=parent_plan_row,
        target_pipeline=target_pipeline,
    )
    agent_result = loop.run(initial_user_message, max_turns=max_turns)

    if not capture.submitted or capture.design is None:
        raise PlanGenerationError(
            "The plan agent finished without calling `submit_plan`. "
            "Re-run `carve plan` and consider rephrasing the goal."
        )

    design = capture.design

    # Enforce user-specified destination fields. The agent saw them
    # in the system prompt and is told to honor them; this is the
    # belt-and-braces enforcement that runs even if the agent
    # ignored or "improved" them.
    if user_destination:
        dest_block = design.get("destination")
        if not isinstance(dest_block, dict):
            dest_block = {}
        for key, value in user_destination.items():
            dest_block[key] = value
        design["destination"] = dest_block

    submitted_name = _validate_pipeline_name(
        design.get("pipeline_name"),
        target_pipeline=target_pipeline,
    )
    description = _coerce_str(design.get("description"), default="")
    requirements = _coerce_str_list(
        design.get("requirements"),
        fallback=["snowflake-connector-python"],
    )

    now = _utcnow()
    expires = now + timedelta(hours=24)
    cost = agent_result.token_usage.cost_usd(model)
    artifact = PlanArtifact(
        id=plan_id,
        goal=goal,
        design=design,
        pipeline_name=submitted_name,
        description=description,
        requirements=requirements,
        parent_plan_id=parent_plan_id,
        target_pipeline=target_pipeline,
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
# System prompt + initial message
# ---------------------------------------------------------------------------


def _compose_plan_system_prompt(
    *,
    config: Config,
    project_dir: Path,
    parent_plan_row: Plan | None,
    target_pipeline: str | None,
    active_target: str,
    user_destination: dict[str, str] | None = None,
) -> str:
    """Assemble the plan agent's system prompt: base + connection + pipeline.

    The base prompt describes the agent's role and the `submit_plan`
    contract. We append a connection-context preamble (M1.1-05 pattern)
    and, if applicable, an existing-pipeline preamble so the agent has
    the context the user already implicitly assumed.

    ``user_destination`` carries CLI-flag-supplied + goal-parsed
    destination fields; it's surfaced as a separate "Honor these
    fields" block so the agent doesn't try to be clever.
    """
    sections: list[str] = [load_m1_plan_agent_prompt()]

    sections.append(_render_connection_context(config, active_target))

    if user_destination:
        sections.append(_render_user_destination_block(user_destination))

    if target_pipeline is not None:
        existing = _render_existing_pipeline_section(
            project_dir,
            target_pipeline,
            active_target=active_target,
        )
        if existing is not None:
            sections.append(existing)

    if parent_plan_row is not None:
        sections.append(_render_parent_plan_section(parent_plan_row))

    return "\n\n".join(sections)


def _render_user_destination_block(user_destination: dict[str, str]) -> str:
    """Tell the agent which destination fields the user fixed.

    Surfaces both CLI flags and goal-parsed identifiers as a single
    "user-fixed" set; the agent doesn't need to distinguish.
    """
    lines = [
        "## User-specified destination",
        "",
        "The user has fixed the destination fields below. Honor them "
        "verbatim in `design.destination` — do NOT pick different "
        "values, even if your catalog inspection suggests something "
        "else looks better. Fields not listed here remain your call.",
        "",
    ]
    for key in ("database", "schema", "table"):
        value = user_destination.get(key)
        if value is not None:
            lines.append(f"- **{key}:** `{value}`")
    return "\n".join(lines)


def _resolve_user_destination(
    *,
    goal: str,
    destination_hint: dict[str, str] | None,
) -> dict[str, str]:
    """Combine CLI-flag hint + goal-text FQN into one destination dict.

    Precedence: CLI flags win over goal-parsed values. Returns an
    empty dict when no field is set (no flags AND no FQN parsed from
    the goal); callers treat that as "agent picks freely."
    """
    from carve.core.targets.destination import parse_fqn_from_goal

    out: dict[str, str] = {}
    parsed = parse_fqn_from_goal(goal)
    if parsed is not None:
        if parsed.database is not None:
            out["database"] = parsed.database
        if parsed.schema is not None:
            out["schema"] = parsed.schema
        out["table"] = parsed.table
    if destination_hint:
        for key, value in destination_hint.items():
            out[key] = value  # CLI flags overwrite goal-parsed
    return out


def _render_connection_context(config: Config, active_target: str) -> str:
    """Render the active target's connection context as a markdown block.

    The agent uses these as defaults for `destination.database` and
    `destination.schema`. Missing targets render as ``(none configured)``
    so the agent flags the gap in `open_questions`.
    """
    snowflake = config.connections.snowflake.get(active_target)
    lines = [
        "## Connection context",
        f"- **Active target:** `{active_target}`",
    ]
    if snowflake is None:
        lines.append("- **Snowflake connection:** _(none configured)_")
        return "\n".join(lines)
    lines.append(f"- **Database:** `{snowflake.database}`")
    if snowflake.schema_:
        lines.append(f"- **Schema:** `{snowflake.schema_}`")
    lines.append(f"- **Warehouse:** `{snowflake.warehouse}`")
    lines.append(f"- **Role:** `{snowflake.role}`")
    lines.append(f"- **Account:** `{snowflake.account}`")
    return "\n".join(lines)


def _render_existing_pipeline_section(
    project_dir: Path,
    pipeline_name: str,
    *,
    active_target: str,
) -> str | None:
    """Inline existing ``main.py`` / ``requirements.txt`` for delta mode.

    Reads from ``el/<name>/`` (P1.1-01's flat layout). Falls back to
    the per-target ``targets/<active_target>/el/<name>/`` layout from
    P1-02 as a one-version compatibility shim — to be removed in v0.2.
    The older ``pipelines/<name>/`` fallback from M1.1-06 is gone.
    """
    rel_dir = f"el/{pipeline_name}"
    pipeline_dir = project_dir / rel_dir
    main_py = pipeline_dir / "main.py"
    requirements = pipeline_dir / "requirements.txt"
    if not main_py.is_file():
        legacy_dir = project_dir / "targets" / active_target / "el" / pipeline_name
        legacy_main = legacy_dir / "main.py"
        if not legacy_main.is_file():
            return None
        logger.warning(
            "Pipeline %r has no files under %s; falling back to legacy "
            "targets/%s/el/%s/. Run `git mv targets/%s/el el && rm -rf targets/` "
            "to migrate (removed in v0.2).",
            pipeline_name,
            rel_dir,
            active_target,
            pipeline_name,
            active_target,
        )
        rel_dir = f"targets/{active_target}/el/{pipeline_name}"
        pipeline_dir = legacy_dir
        main_py = legacy_main
        requirements = legacy_dir / "requirements.txt"
    parts: list[str] = [
        f"## Existing pipeline `{pipeline_name}`",
        "The user wants to modify this pipeline. Propose a delta-consistent design.",
        "",
        f"### `{rel_dir}/main.py`",
        "```python",
        main_py.read_text(encoding="utf-8").rstrip("\n"),
        "```",
    ]
    if requirements.is_file():
        parts += [
            "",
            f"### `{rel_dir}/requirements.txt`",
            "```",
            requirements.read_text(encoding="utf-8").rstrip("\n"),
            "```",
        ]
    return "\n".join(parts)


def _render_parent_plan_section(parent: Plan) -> str:
    """Inline the parent plan's goal + design for refinement context."""
    # v0.1-01: task_graph_json is JSONB; ORM returns dict directly.
    raw = parent.task_graph_json
    parent_task_graph = raw if isinstance(raw, dict) else {}
    parent_design = parent_task_graph.get("design")
    parts = [
        f"## Refining plan `{parent.id}`",
        "The user provided feedback on a prior draft. Adjust the design accordingly.",
        "",
        f"### Original goal\n{parent.goal}",
    ]
    if parent_design is not None:
        parts += [
            "",
            "### Prior design",
            "```json",
            json.dumps(parent_design, indent=2, sort_keys=True),
            "```",
        ]
    return "\n".join(parts)


def _compose_initial_user_message(
    *,
    goal: str,
    parent_plan_row: Plan | None,
    target_pipeline: str | None,
) -> str:
    """Frame the goal so the agent can tell which mode it's in."""
    if parent_plan_row is not None:
        return f"User feedback on plan {parent_plan_row.id}:\n\n{goal}"
    if target_pipeline is not None:
        return f"Modify pipeline `{target_pipeline}`. Change requested:\n\n{goal}"
    return goal


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class _UnconfiguredSnowflakeRunner:
    """Stub runner used when no Snowflake target is configured."""

    def run_query(self, sql: str, *, limit: int) -> list[dict[str, Any]]:
        raise ToolExecutionError(
            "No Snowflake connection is configured for the active target. "
            "Add a [connections.snowflake.<target>] block to "
            "carve/connections.toml and re-run."
        )


def _build_tools(
    config: Config,
    project_dir: Path,
    capture: SubmitPlanCapture,
    *,
    active_target: str,
    snowflake_pool: SnowflakePool,
) -> list[Tool]:
    """Plan-agent toolset: read_file + run_snowflake_query + submit_plan.

    ``active_target`` selects which ``[snowflake.<target>]`` connection
    the agent inspects. ``snowflake_pool`` is shared with the
    ``SkillContext`` so the run_snowflake_query tool and the catalog
    skills reuse the same per-target connection cache.
    """
    snowflake_runner: Any
    if active_target in config.connections.snowflake:
        try:
            snowflake_runner = snowflake_pool.get(active_target)
        except SnowflakeError:
            logger.warning(
                "Snowflake target %r is configured but unavailable; "
                "the agent will get an error if it uses run_snowflake_query.",
                active_target,
            )
            snowflake_runner = _UnconfiguredSnowflakeRunner()
    else:
        logger.warning(
            "no Snowflake connection configured for target %r; "
            "the agent's run_snowflake_query tool will return an error if used.",
            active_target,
        )
        snowflake_runner = _UnconfiguredSnowflakeRunner()

    return [
        make_read_file_tool(project_dir),
        make_run_snowflake_query_tool(snowflake_runner),
        make_submit_plan_tool(capture),
    ]


def _build_skills(
    *,
    config: Config,
    repository: Repository,
    active_target: str,
    snowflake_pool: SnowflakePool,
) -> tuple[SkillRegistry, CachedSkillExecutor, SkillContext]:
    """Wire the catalog skills into a registry + executor + context.

    The plan flow has no `Run` row, so `run_id=None` is passed through;
    `SkillContext.log` no-ops when run_id is unset. The skill registry
    is the process-wide default populated by `load_builtin_skills()`.
    The ``snowflake_pool`` is shared with the run_snowflake_query tool
    so both connection paths reuse the same per-target driver.
    """
    registry = load_builtin_skills()
    executor = CachedSkillExecutor(registry)
    context = SkillContext(
        config=config,
        repo=repository,
        run_id=None,
        target=active_target,
        snowflake_pool=snowflake_pool,
    )
    return registry, executor, context


def _build_client(config: Config, client: Any | None) -> Any:
    """Return the Anthropic client, building one from config if needed.

    All credential precedence (API key vs. Claude-subscription OAuth) lives
    in :func:`carve.core.agents.client_factory.make_client`.
    """
    from carve.core.agents.client_factory import make_client

    return make_client(config, client)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_pipeline_name(
    candidate: Any,
    *,
    target_pipeline: str | None,
) -> str:
    """Reject malformed pipeline names; lock to ``target_pipeline`` if set."""
    if not isinstance(candidate, str) or not candidate:
        raise PlanGenerationError("design.pipeline_name is missing or not a string.")
    try:
        validate_artifact_name(candidate)
    except InvalidArtifactNameError as exc:
        raise PlanGenerationError(f"design.pipeline_name {candidate!r} is invalid: {exc}") from exc
    if target_pipeline is not None and candidate != target_pipeline:
        raise PlanGenerationError(
            f"design.pipeline_name {candidate!r} does not match the "
            f"--pipeline target {target_pipeline!r}; the agent must keep "
            "the existing pipeline name when modifying."
        )
    return candidate


def _coerce_str(value: Any, *, default: str) -> str:
    if isinstance(value, str):
        return value
    return default


def _coerce_str_list(value: Any, *, fallback: list[str]) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        cleaned = [item for item in value if item and not item.startswith("-")]
        if cleaned:
            return cleaned
    return list(fallback)


# ---------------------------------------------------------------------------
# Plan-id generation
# ---------------------------------------------------------------------------


def _new_plan_id() -> str:
    """Build a plan id of the form `plan_YYYYMMDD_HHMMSS_<6hex>`."""
    stamp = _utcnow().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"plan_{stamp}_{suffix}"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_artifact(
    artifact: PlanArtifact,
    project_dir: Path,
    repository: Repository,
) -> Path:
    """Write the plan JSON to disk and insert the index row."""
    plans_dir = project_dir / ".carve" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    file_path = plans_dir / f"{artifact.id}.json"

    payload = artifact.to_json()
    file_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    # P1-02 dropped `Plan.estimates_json`; estimates that were once
    # surfaced for the planner now ride on the on-disk plan JSON
    # alongside the design (cost, tokens, model). Build owns the
    # deploy-state columns previously on Plan.
    task_graph = {
        "design": artifact.design,
        "pipeline_name": artifact.pipeline_name,
        "requirements": list(artifact.requirements),
    }

    plan_row = Plan(
        id=artifact.id,
        parent_plan_id=artifact.parent_plan_id,
        goal=artifact.goal,
        config_hash=artifact.config_hash,
        carve_version=artifact.carve_version,
        # v0.1-01: task_graph_json is JSONB; pass dict, not str.
        task_graph_json=task_graph,
        file_path=str(file_path),
        phase="drafted",
        pipeline_name=artifact.target_pipeline,
        created_at=artifact.created_at,
        expires_at=artifact.expires_at,
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


__all__ = [
    "PLAN_ID_RE",
    "PlanArtifact",
    "PlanGenerationError",
    "TokenUsage",
    "generate_plan",
]
