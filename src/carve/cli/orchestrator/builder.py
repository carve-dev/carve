"""`carve build` orchestration.

Consumes a saved `Plan` (produced by `generate_plan` with
``phase='drafted'``) and runs the build agent to materialise
``el/<name>/main.py`` and ``requirements.txt``. On success:

* Inserts/updates the `Pipeline` row keyed by name.
* Creates a `Build` row binding the plan to the active target.
* Sets ``Pipeline.current_build_id`` to the new build.
* Marks the plan ``phase='built'``, links it to the pipeline.
* Returns a `BuildArtifact` with the run id, build id, and file list.

The build agent is narrower than the plan agent: only `read_file` and
`write_file`. It receives the design as a markdown preamble in the
system prompt so the file generation has every decision pinned.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
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
    Tool,
    load_m1_build_agent_prompt,
    make_read_file_tool,
    make_write_file_tool,
)
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.config import Config
from carve.core.state import Plan, Repository
from carve.core.targets.names import (
    InvalidArtifactNameError,
    validate_artifact_name,
)

logger = logging.getLogger(__name__)


@dataclass
class BuildArtifact:
    """Result of `build_plan`.

    Captures what the build run produced for the CLI's summary block.
    `success` is False when the build agent finished but didn't write a
    `main.py`; the build run row is marked failed in that case and the
    plan stays drafted. ``build_id`` is None on failure (no Build row
    is created) and the build's id otherwise.
    """

    plan_id: str
    pipeline_name: str
    pipeline_dir: str
    target: str
    files_written: list[str]
    summary: str
    run_id: str
    success: bool
    build_id: str | None
    tokens_input: int
    tokens_output: int
    cost_usd: float


class BuildError(Exception):
    """Raised when a build can't proceed (bad plan state, etc.)."""


class ConfigDriftError(BuildError):
    """Raised when a Plan's ``config_hash`` no longer matches current config.

    The Plan was generated against a ``carve.toml``/component-ref snapshot
    that has since moved (the per-verb hash gate, ARCHITECTURE §7.6). The
    build is refused regardless of ``--force`` — ``--force`` overrides
    "already built", never "config moved underneath the plan". The CLI
    maps this to exit code ``3`` and tells the user to re-plan.
    """

    def __init__(self, plan_id: str, *, plan_hash: str, current_hash: str) -> None:
        self.plan_id = plan_id
        self.plan_hash = plan_hash
        self.current_hash = current_hash
        super().__init__(
            f"Plan {plan_id!r} was generated against config_hash "
            f"{plan_hash!r}, but current config hashes to {current_hash!r}. "
            "The config (carve.toml / component refs) drifted since plan "
            "time. Re-plan against current config and rebuild."
        )


class PlanExpiredError(BuildError):
    """Raised when a Plan's ``expires_at`` is in the past.

    Plans expire (default 24h) so a stale "plan now, build much later"
    can't silently materialise an out-of-date design. A plain plan-state
    error — the CLI maps it to the generic exit code ``2`` (not the
    drift-specific ``3``).
    """

    def __init__(self, plan_id: str, *, expires_at: datetime) -> None:
        self.plan_id = plan_id
        self.expires_at = expires_at
        super().__init__(
            f"Plan {plan_id!r} expired at {expires_at.isoformat()}. "
            "Re-plan to build a fresh design."
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_plan(
    plan_id: str,
    config: Config,
    project_dir: Path,
    *,
    repository: Repository,
    target: str | None = None,
    client: Any | None = None,
    max_turns: int = 30,
    observer: AgentObserver | None = None,
    force: bool = False,
    destination_override: dict[str, str] | None = None,
    now: datetime | None = None,
) -> BuildArtifact:
    """Build the pipeline files described by ``plan_id``.

    Args:
        plan_id: Id of a previously-generated plan.
        config: Fully-loaded `Config`.
        project_dir: Resolved project root.
        repository: State-store repository.
        target: Optional target override. When None, falls through to
            ``CARVE_TARGET`` then ``config.project.default_target``
            (per `resolve_active_target`). The resolved value is
            persisted on the Build row (recording which target's
            catalog this build was inspected against); files always
            land in the flat ``el/<name>/`` tree.
        client: Optional pre-built Anthropic client (used in tests).
        max_turns: Cap on agent turns.
        observer: Optional progress observer.
        force: When True, allow re-running the build agent against a plan
            that's already in ``phase='built'`` (a true rebuild). Default
            False makes an unchanged-config rebuild a no-op (see below)
            and otherwise refuses. ``--force`` never bypasses the drift
            gate — it overrides "already built", not "config moved".
        now: Injectable current time for the expiry check. Production
            callers pass ``None`` (current UTC); tests force a time to
            make an expired plan deterministic. Mirrors
            ``repository.list_expired_plans(now=...)``.

    Raises:
        ConfigDriftError: The plan's ``config_hash`` no longer matches
            current config — refused regardless of ``--force`` (exit 3).
        PlanExpiredError: The plan's ``expires_at`` is in the past.
        BuildError: Plan doesn't exist or is in the wrong phase without
            ``--force``.
        ConfigError: Anthropic API key missing.

    The gate order is deliberate: **drift is checked before the
    force/phase gate** so ``--force`` cannot smuggle a build past a moved
    config. Idempotency short-circuits a no-op rebuild before the agent
    ever runs.
    """
    # Local import to avoid a circular dependency: `carve.cli.main`
    # imports the command modules, which import this module.
    from carve.cli.main import ACTIVE_TARGET_FLAG
    from carve.core.targets.resolution import resolve_active_target

    project_dir = project_dir.resolve()

    plan_row = repository.get_plan(plan_id)
    if plan_row is None:
        raise BuildError(f"Plan {plan_id!r} not found.")

    # 1. Config-hash drift gate — BEFORE the force/phase gate. The plan
    #    carries the config_hash it was generated against; if current
    #    config has moved, the design may reference connections/components
    #    that no longer exist. `--force` does NOT bypass this — it
    #    overrides "already built", never "config drifted".
    if plan_row.config_hash != config.config_hash:
        raise ConfigDriftError(
            plan_id,
            plan_hash=plan_row.config_hash,
            current_hash=config.config_hash,
        )

    # 2. Expiry gate — a plan past its `expires_at` is refused so a stale
    #    "plan now, build much later" can't materialise an out-of-date
    #    design. `now` is injectable for deterministic tests.
    current_time = now if now is not None else datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    plan_expires_at = _aware_utc(plan_row.expires_at)
    if plan_expires_at < current_time:
        raise PlanExpiredError(plan_id, expires_at=plan_expires_at)

    # 3. Idempotent rebuild — re-building an already-built plan against
    #    UNCHANGED config (drift already cleared above) with its recorded
    #    file set still present on disk is a no-op: return the existing
    #    build without re-running the agent or creating a duplicate Build
    #    row. `--force` opts out and forces a true rebuild.
    if plan_row.phase == "built" and not force:
        existing = _idempotent_noop_artifact(plan_row, repository, project_dir)
        if existing is not None:
            logger.info(
                "Plan %r already built against unchanged config; build is a no-op.",
                plan_id,
            )
            return existing

    # 4. Phase gate — a built plan with no reusable build (files missing,
    #    no current build) still needs `--force` to re-run the agent.
    if plan_row.phase != "drafted" and not force:
        raise BuildError(
            f"Plan {plan_id!r} is already in phase {plan_row.phase!r}. "
            "Use `--force` to rebuild, or refine the plan and try again."
        )

    design = _load_plan_design(plan_row)

    # Apply CLI-flag overrides (from `carve build --table/--database/--schema`)
    # to the design's destination BEFORE the agent runs. The agent sees
    # the overridden values in its design preamble; destination.toml
    # then reflects them. Empty-string sentinel means "clear the field"
    # (used by the prompt-edit flow when the user blanks out a value
    # to fall back to env inherit).
    if destination_override:
        dest_block = design.get("destination")
        if not isinstance(dest_block, dict):
            dest_block = {}
        for key, value in destination_override.items():
            if value == "":
                dest_block.pop(key, None)
            else:
                dest_block[key] = value
        design["destination"] = dest_block

    pipeline_name = _pipeline_name_from(plan_row, design)

    cli_flag = target if target is not None else ACTIVE_TARGET_FLAG
    active_target = resolve_active_target(cli_flag, config)

    anthropic_client = _build_client(config, client)

    pipeline_dir_rel = f"el/{pipeline_name}"
    pipeline_dir_abs = project_dir / pipeline_dir_rel
    snapshot = _snapshot_pipeline_dir(pipeline_dir_abs)

    # The Pipeline row is upserted at the end of a successful build, so it
    # doesn't exist yet at create_run time. Pass pipeline_name=None to keep
    # the runs.pipeline_name FK happy (the column is nullable specifically
    # for this case — see M1.1-06 spec, "runs table changes"). After the
    # pipeline lands we backfill via attach_pipeline_to_run below.
    run_id = repository.create_run(
        kind="build",
        target_id=plan_id,
        pipeline_name=None,
    )
    repository.update_run_status(run_id, "running")

    tools = _build_tools(project_dir)
    # Extensibility (spec 16): expose discovered skill packs at runtime via
    # the content-injection lookup tool (discovery is inert at load).
    tools.append(build_skill_pack_tool(project_dir=project_dir, paths=config.paths))

    # Extensibility (spec 16): route through the classification router; no
    # classification → None → the M1 build flow is preserved unchanged, but
    # the seam is live for declarative agents.
    _routed_agent = resolve_agent_or_fallback(
        project_dir=project_dir,
        paths=config.paths,
        classification=None,
    )
    if _routed_agent is not None:
        logger.debug("Router selected agent %r for build flow.", _routed_agent)

    # Extensibility (spec 16): load carve/hooks.toml clamped to the build
    # flow's mode. A missing file yields no hooks; hooks fire after the gate
    # at the loop's pre/post-tool seam.
    pre_tool_hook, post_tool_hook = build_extensibility_hooks(
        project_dir=project_dir,
        paths=config.paths,
        mode=PermissionMode.BUILD,
    )

    system_prompt = _compose_build_system_prompt(
        config=config,
        project_dir=project_dir,
        design=design,
        pipeline_name=pipeline_name,
        target_pipeline=plan_row.pipeline_name,
        active_target=active_target,
    )

    loop = AgentLoop(
        client=anthropic_client,
        tools=tools,
        system_prompt=system_prompt,
        model=config.models.default_model,
        repository=repository,
        run_id=run_id,
        observer=observer if observer is not None else NullObserver(),
        pre_tool_hook=pre_tool_hook,
        post_tool_hook=post_tool_hook,
    )

    initial_message = (
        f"Build the pipeline `{pipeline_name}`. Write "
        f"`{pipeline_dir_rel}/main.py` and `{pipeline_dir_rel}/"
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
            target=active_target,
            files_written=sorted(p.relative_to(project_dir).as_posix() for p in written_files),
            summary=agent_result.text,
            run_id=run_id,
            success=False,
            build_id=None,
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

    # Write destination.toml — the per-artifact, per-target destination
    # config the script reads at runtime. The build flow owns this file
    # (not the agent) so we can apply the override-vs-inherit rule
    # consistently against the resolved env defaults.
    destination_path = _write_destination_toml_for_build(
        pipeline_dir_abs=pipeline_dir_abs,
        design=design,
        config=config,
        active_target=active_target,
    )
    if destination_path is not None:
        written_files.append(destination_path)

    files_written_rel = sorted(p.relative_to(project_dir).as_posix() for p in written_files)

    repository.create_or_update_pipeline(
        name=pipeline_name,
        description=_design_description(design),
        pipeline_dir=pipeline_dir_rel,
    )
    # Pipeline now exists; safe to backfill the build run's FK so this
    # build shows up in `runs --pipeline <name>` filters.
    repository.attach_pipeline_to_run(run_id, pipeline_name)

    # Create the Build row and point the Pipeline at it.
    build = repository.create_build(
        pipeline_name=pipeline_name,
        plan_id=plan_id,
        target=active_target,
        manifest={"files": files_written_rel},
    )
    repository.set_pipeline_current_build(pipeline_name, build.id)

    repository.mark_plan_built(
        plan_id=plan_id,
        pipeline_name=pipeline_name,
    )
    repository.update_run_status(run_id, "success")

    return BuildArtifact(
        plan_id=plan_id,
        pipeline_name=pipeline_name,
        pipeline_dir=pipeline_dir_rel,
        target=active_target,
        files_written=files_written_rel,
        summary=agent_result.text,
        run_id=run_id,
        success=True,
        build_id=build.id,
        tokens_input=agent_result.token_usage.input_tokens,
        tokens_output=agent_result.token_usage.output_tokens,
        cost_usd=agent_result.token_usage.cost_usd(config.models.default_model),
    )


# ---------------------------------------------------------------------------
# Drift / expiry / idempotency helpers
# ---------------------------------------------------------------------------


def _aware_utc(dt: datetime) -> datetime:
    """Coerce a possibly-naive datetime to aware UTC for comparison.

    Postgres ``TIMESTAMPTZ`` round-trips aware datetimes, but a row built
    in a test with a naive ``expires_at`` would otherwise raise on the
    ``<`` comparison. Treat naive as UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _idempotent_noop_artifact(
    plan_row: Plan,
    repository: Repository,
    project_dir: Path,
) -> BuildArtifact | None:
    """Return the existing build as a no-op artifact, or ``None`` to rebuild.

    The rebuild is a no-op only when the plan already settled on a
    pipeline whose ``current_build_id`` Build manifest lists files that
    all still exist on disk. Any gap (no pipeline, no current build, a
    missing manifest file) returns ``None`` so the caller falls through
    to a real rebuild. Drift was already cleared by the caller, so an
    unchanged-config rebuild that reaches here is genuinely redundant.
    """
    pipeline_name = plan_row.pipeline_name
    if pipeline_name is None:
        return None
    pipeline = repository.get_pipeline(pipeline_name)
    if pipeline is None or pipeline.current_build_id is None:
        return None
    build = repository.get_build(pipeline.current_build_id)
    if build is None or build.plan_id != plan_row.id:
        # The current build belongs to a different plan (e.g. another plan
        # rebuilt this pipeline). Re-running keeps this plan authoritative.
        return None

    manifest = build.manifest_json if isinstance(build.manifest_json, dict) else {}
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return None
    rel_files: list[str] = []
    for entry in files:
        if not isinstance(entry, str):
            return None
        if not (project_dir / entry).is_file():
            # A manifest file is gone — the build is not reproducible as a
            # no-op; fall through to a real rebuild.
            return None
        rel_files.append(entry)

    pipeline_dir_rel = pipeline.pipeline_dir or f"el/{pipeline_name}"
    return BuildArtifact(
        plan_id=plan_row.id,
        pipeline_name=pipeline_name,
        pipeline_dir=pipeline_dir_rel,
        target=build.target,
        files_written=sorted(rel_files),
        summary=(
            f"Plan {plan_row.id} already built against unchanged config; "
            f"reused build {build.id} (no-op)."
        ),
        run_id="",
        success=True,
        build_id=build.id,
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# Plan/design helpers
# ---------------------------------------------------------------------------


def _load_plan_design(plan_row: Plan) -> dict[str, Any]:
    """Pull the design dict out of the plan's stored task_graph JSON."""
    # v0.1-01: task_graph_json is JSONB; ORM returns dict directly.
    raw = plan_row.task_graph_json
    if raw is None:
        task_graph: dict[str, Any] = {}
    elif isinstance(raw, dict):
        task_graph = raw
    else:
        raise BuildError(
            f"Plan {plan_row.id!r} has non-dict task_graph_json (type={type(raw).__name__})"
        )
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
    if not isinstance(candidate, str):
        raise BuildError(
            f"Could not resolve a valid pipeline name for this build. Got {candidate!r}."
        )
    try:
        validate_artifact_name(candidate)
    except InvalidArtifactNameError as exc:
        raise BuildError(
            f"Pipeline name {candidate!r} is not a valid artifact name: {exc}"
        ) from exc
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


def _write_destination_toml_for_build(
    *,
    pipeline_dir_abs: Path,
    design: dict[str, Any],
    config: Config,
    active_target: str,
) -> Path | None:
    """Emit the per-artifact ``destination.toml`` next to ``main.py``.

    Reads the design's ``destination`` block and the active target's
    connection defaults; writes a ``destination.toml`` whose
    ``database`` / ``schema`` are commented out when they match the
    target's defaults, and live overrides when they differ. ``table``
    is always a literal.

    Returns the path written, or ``None`` if the design lacks a
    destination block (no destination → nothing to write; the run
    flow surfaces the missing destination at runtime).
    """
    from carve.core.targets.destination import (
        Destination,
        write_destination_toml,
    )

    destination_block = design.get("destination")
    if not isinstance(destination_block, dict):
        return None
    table = destination_block.get("table")
    if not isinstance(table, str) or not table:
        return None

    db = destination_block.get("database")
    schema = destination_block.get("schema")
    destination = Destination(
        table=table,
        database=db if isinstance(db, str) and db else None,
        schema=schema if isinstance(schema, str) and schema else None,
    )

    # Default db/schema for the active target (post env-var
    # interpolation).
    target_section = config.connections.snowflake.get(active_target)
    env_db = target_section.database if target_section is not None else None
    env_schema = target_section.schema_ if target_section is not None else None

    path = pipeline_dir_abs / "destination.toml"
    write_destination_toml(
        path,
        destination,
        target=active_target,
        env_database=env_db,
        env_schema=env_schema,
    )
    return path.resolve()


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
    active_target: str,
) -> str:
    """Base prompt + connection context + design block + (optional) existing files."""
    sections: list[str] = [load_m1_build_agent_prompt()]
    sections.append(_render_connection_context(config, active_target))
    sections.append(_render_output_path_block(active_target, pipeline_name))
    sections.append(_render_design_preamble(design))
    if target_pipeline is not None:
        existing = _render_existing_pipeline_section(
            project_dir,
            pipeline_name,
            active_target=active_target,
        )
        if existing is not None:
            sections.append(existing)
    return "\n\n".join(sections)


def _render_output_path_block(active_target: str, pipeline_name: str) -> str:
    """Pin the build agent to the flat output directory.

    P1.1-01 moved the build output from
    ``targets/<target>/el/<name>/`` to the flat ``el/<name>/`` tree
    (one artifact tree per pipeline, target-agnostic). The block pins
    the agent to those paths.
    """
    del active_target  # retained in signature for parity with the
    # build-flow caller; the flat layout no longer encodes target.
    base = f"el/{pipeline_name}"
    return (
        "## Output paths\n"
        f"- Write `{base}/main.py` and `{base}/requirements.txt`. "
        "Do not write to any other location."
    )


def _render_connection_context(config: Config, active_target: str) -> str:
    snowflake = config.connections.snowflake.get(active_target)
    upper = active_target.upper()
    lines = [
        "## Connection context",
        "",
        f"Active target: `{active_target}`. The script reads the connection "
        "from env vars at runtime — NEVER inline a resolved account / "
        "database / role / warehouse value as a Python literal. The same "
        "`main.py` must run against any target by switching the prefix.",
        "",
        "**`main.py` MUST read these via `os.environ['<KEY>']`:**",
        "",
        f"- account:   `os.environ['{upper}_SNOWFLAKE_ACCOUNT']`",
        f"- user:      `os.environ['{upper}_SNOWFLAKE_USER']`",
        f"- password:  `os.environ['{upper}_SNOWFLAKE_PASSWORD']`",
        f"- role:      `os.environ['{upper}_SNOWFLAKE_ROLE']`",
        f"- warehouse: `os.environ['{upper}_SNOWFLAKE_WAREHOUSE']`",
        f"- database:  `os.environ['{upper}_SNOWFLAKE_DATABASE']`",
    ]
    # Schema env-var ref only when the target's connections.toml has a
    # `schema = "${...}"` entry — otherwise the agent would emit an
    # `os.environ['DEV_SNOWFLAKE_SCHEMA']` reference that KeyErrors at
    # runtime against a project that doesn't set the var.
    if snowflake is not None and snowflake.schema_:
        lines.append(f"- schema:    `os.environ['{upper}_SNOWFLAKE_SCHEMA']`")

    lines += [
        "",
        "**`main.py` MUST read the destination from `destination.toml`** "
        "(written by the build flow, lives next to your `main.py`). "
        "Database/schema fall back to the env vars above when not set in "
        "destination.toml; table is always literal in destination.toml.",
        "",
        "Canonical pattern:",
        "```python",
        "import os, tomllib",
        "from pathlib import Path",
        "",
        "_dest_cfg = tomllib.loads(",
        "    (Path(__file__).parent / 'destination.toml').read_text(encoding='utf-8')",
        ")",
        "_target = os.environ['CARVE_ACTIVE_TARGET']",
        "DEST_DATABASE = _dest_cfg.get('database') or os.environ[",
        "    f'{_target}_SNOWFLAKE_DATABASE'",
        "]",
        "DEST_SCHEMA = _dest_cfg.get('schema') or os.environ[",
        "    f'{_target}_SNOWFLAKE_SCHEMA'",
        "]",
        "DEST_TABLE = _dest_cfg['table']  # always literal",
        "DEST_FQN = f'{DEST_DATABASE}.{DEST_SCHEMA}.{DEST_TABLE}'",
        "```",
    ]

    if snowflake is None:
        lines.append("")
        lines.append(
            "_(No `[snowflake.<target>]` section is configured for this "
            "target; the DDL identifiers below come from the design.)_"
        )
        return "\n".join(lines)

    lines += [
        "",
        "**For the DDL file (`<artifact>.sql`)** — the per-target "
        "snapshot. Concrete identifiers go in directly; no `${VAR}` "
        "substitution.",
        "",
        f"- Database: `{snowflake.database}`",
    ]
    if snowflake.schema_:
        lines.append(f"- Schema: `{snowflake.schema_}`")
    lines += [
        f"- Warehouse: `{snowflake.warehouse}`",
        f"- Runtime role (used in GRANT): `{snowflake.role}`",
    ]
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
    *,
    active_target: str,
) -> str | None:
    del active_target  # signature retained for parity; flat layout
    # is target-agnostic.
    rel_dir = f"el/{pipeline_name}"
    pipeline_dir = project_dir / rel_dir
    main_py = pipeline_dir / "main.py"
    requirements = pipeline_dir / "requirements.txt"
    if not main_py.is_file():
        return None
    parts: list[str] = [
        f"## Existing pipeline `{pipeline_name}` (current state)",
        "Use this for reference; the design above is authoritative for the new state.",
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
    """Return the Anthropic client; credential precedence lives in one place."""
    from carve.core.agents.client_factory import make_client

    return make_client(config, client)


# ---------------------------------------------------------------------------
# Pipeline directory snapshot/diff
# ---------------------------------------------------------------------------


def _snapshot_pipeline_dir(pipeline_dir: Path) -> dict[Path, float]:
    """Return ``{path: mtime}`` for every file under ``pipeline_dir``."""
    if not pipeline_dir.is_dir():
        return {}
    return {
        path.resolve(): path.stat().st_mtime for path in pipeline_dir.rglob("*") if path.is_file()
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


__all__ = [
    "BuildArtifact",
    "BuildError",
    "ConfigDriftError",
    "PlanExpiredError",
    "build_plan",
]
