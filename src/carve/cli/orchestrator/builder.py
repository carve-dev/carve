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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carve.cli.orchestrator.cost_rollup import CostRollup, roll_up_cost
from carve.cli.orchestrator.delegation_run import build_registry, run_engines
from carve.cli.orchestrator.extensibility_wiring import (
    build_extensibility_hook_factory,
    build_extensibility_hooks,
    build_skill_pack_tool,
    resolve_agent_or_fallback,
)
from carve.cli.orchestrator.goal_decomposer import SubGoal
from carve.cli.orchestrator.review_wiring import run_review_fan_out
from carve.core.agents import (
    AgentLoop,
    AgentObserver,
    NullObserver,
    Tool,
    load_m1_build_agent_prompt,
    make_read_file_tool,
    make_write_file_tool,
)
from carve.core.agents.delegation import DelegationResult
from carve.core.agents.exceptions import AgentError
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.review_fan_out import (
    _FAILING_SEVERITIES,
    Finding,
    ReviewResult,
)
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

    The review-gate fields carry the quality-gate verdict (B2) to the CLI /
    return surface. ``review_passed`` is ``True`` when no ``blocker``/``major``
    finding was raised (the gate's pass condition); it is ``True`` on the
    single-agent / M1 path and on a no-op rebuild, where no live review runs.
    ``review_findings`` is a compact, JSON-friendly summary of every finding
    (each ``{reviewer, severity, file, line, message}``), and
    ``review_blocking_count`` is how many were ``blocker``/``major`` — the
    findings that BLOCK the build. On a blocked build ``success`` is ``False``,
    ``review_passed`` is ``False``, ``build_id`` is ``None`` (no Build row),
    and ``review_findings`` carries the blockers so the CLI can render them.
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
    review_passed: bool = True
    review_findings: list[dict[str, Any]] = field(default_factory=list)
    review_blocking_count: int = 0


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

    # B2 fork: a Plan produced by multi-engine decomposition (B1) carries a
    # non-empty `planned_by_engine` — the ordered per-engine slices. Reconstruct
    # the `list[SubGoal]` from it and AUTHOR via the multi-engine path: each
    # engineer runs in BUILD capacity (`run_engines` at `parent_mode=BUILD`),
    # writes its real files, then the authored diff is gated by the live review
    # fan-out. A design WITHOUT `planned_by_engine` (a single-engine / M1 plan)
    # falls through to the unchanged M1 single-`AgentLoop` build path below —
    # the same fallback discipline B1 used at plan time.
    sub_goals = _reconstruct_sub_goals(design)
    if sub_goals:
        return _build_multi_engine(
            plan_id=plan_id,
            plan_row=plan_row,
            sub_goals=sub_goals,
            design=design,
            config=config,
            project_dir=project_dir,
            repository=repository,
            client=anthropic_client,
            pipeline_name=pipeline_name,
            active_target=active_target,
            observer=observer,
            max_turns=max_turns,
        )

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
# Multi-engine build authoring + the live review gate (B2)
# ---------------------------------------------------------------------------


def _reconstruct_sub_goals(design: dict[str, Any]) -> list[SubGoal]:
    """Rebuild the ordered ``list[SubGoal]`` from a design's ``planned_by_engine``.

    B1's multi-engine planner stamps ``design["planned_by_engine"]`` — an ordered
    ``[{sub_goal, classification, files}]`` — recording which engine designed
    which slice, in order. B2 reconstructs the decomposition from it so the SAME
    engineers author at BUILD that designed at PLAN. A non-empty result selects
    the multi-engine build path; an EMPTY result (the key is absent, not a list,
    or carries no well-formed entry) means a single-engine / M1 plan and selects
    the unchanged M1 build path.

    Malformed entries are skipped defensively (a non-dict entry, or one missing a
    non-empty string ``sub_goal``/``classification``); the routing off
    ``classification`` is the same the planner validated against the registry, so
    a well-formed B1 plan reconstructs cleanly here.
    """
    raw = design.get("planned_by_engine")
    if not isinstance(raw, list):
        return []
    sub_goals: list[SubGoal] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sub_goal = entry.get("sub_goal")
        classification = entry.get("classification")
        if not isinstance(sub_goal, str) or not sub_goal.strip():
            continue
        if not isinstance(classification, str) or not classification.strip():
            continue
        sub_goals.append(SubGoal(sub_goal=sub_goal.strip(), classification=classification.strip()))
    return sub_goals


def _build_multi_engine(
    *,
    plan_id: str,
    plan_row: Plan,
    sub_goals: list[SubGoal],
    design: dict[str, Any],
    config: Config,
    project_dir: Path,
    repository: Repository,
    client: Any,
    pipeline_name: str,
    active_target: str,
    observer: AgentObserver | None,
    max_turns: int,
) -> BuildArtifact:
    """Author a multi-engine Plan with N engineers, then gate on the review fan-out.

    The B2 build path. Each reconstructed ``SubGoal`` is authored by its engineer
    in BUILD capacity (:func:`run_engines` at ``parent_mode=BUILD`` — sequential,
    one runner) so it writes its real slice (dlt → ``el/**``, dbt → ``models/**``).
    The authored file set is the UNION of every engine's harness-tracked
    ``DelegationResult.files_changed`` — NOT a single ``el/<name>/`` dir snapshot,
    which can't see both trees. That union is the diff handed to the live review
    fan-out, whose verdict GATES the build:

    * any engine that did not ``succeeded`` (out of turns, no ``submit_result``,
      self-reported ``failed``/``needs_user_input``) fails the build cleanly — the
      run row goes ``failed`` and NO ``Build`` row is written (mirroring B1's "do
      not persist a partial");
    * a ``blocker``/``major`` review finding BLOCKS — no ``Build`` row / no
      ``current_build_id`` advance / the plan stays drafted; the artifact carries
      ``success=False`` + ``review_passed=False`` + the blocking findings;
    * a clean (or only ``minor``/``info``) review proceeds — the ``Build`` row is
      written with a ``review`` block in its manifest, ``current_build_id``
      advances, the plan is marked built, and warnings surface on the artifact.

    A build fix iteration is deliberately deferred (B2 surfaces a blocked build;
    it does not auto-fix).
    """
    goal = plan_row.goal

    run_id = repository.create_run(kind="build", target_id=plan_id, pipeline_name=None)
    repository.update_run_status(run_id, "running")

    # Snapshot the pre-build content of every PLANNED file so the review diff
    # renders authored slices against their prior version (greenfield files
    # diff from empty). The planned set is the union of `planned_by_engine`'s
    # file lists — a superset of what actually gets authored, which is fine:
    # the diff is keyed on the authored `files_changed`, and a planned-but-
    # unauthored file simply never appears.
    pre_build = _snapshot_files(_planned_files(design), project_dir)

    # One registry, threaded into both the engine run and the reviewer routing,
    # so the routes (engine + reviewer) resolve against one source of truth.
    registry = build_registry(project_dir, config)

    # Fail-closed hooks boundary BEFORE any delegation (the build flow's
    # promise): parse carve/hooks.toml eagerly so a malformed file aborts before
    # an engineer or reviewer runs. The factory is threaded into both runs so the
    # file is parsed once.
    hook_factory = build_extensibility_hook_factory(
        project_dir=project_dir,
        paths=config.paths,
        approver=None,
    )

    try:
        results = run_engines(
            sub_goals,
            config=config,
            project_dir=project_dir,
            client=client,
            model=config.models.default_model,
            registry=registry,
            hook_factory=hook_factory,
            parent_mode=PermissionMode.BUILD,
            max_turns=max_turns,
        )
    except Exception as exc:
        repository.update_run_status(run_id, "failed", error=str(exc))
        raise

    rollup = roll_up_cost(results)

    # The authored manifest is the UNION of each engine's harness-tracked
    # `files_changed` — order-stable, de-duped — never a dir snapshot.
    authored = _union_files_changed(results)

    # Any engine that did not succeed → fail the build cleanly; no Build row.
    failed = next((r for r in results if r.status != "succeeded"), None)
    if failed is not None:
        repository.update_run_status(
            run_id,
            "failed",
            error=(
                f"A build engineer returned status={failed.status!r} "
                f"(not a usable authored slice): {failed.result_summary}"
            ),
        )
        return _multi_engine_artifact(
            plan_id=plan_id,
            pipeline_name=pipeline_name,
            active_target=active_target,
            files_written=authored,
            summary=failed.result_summary,
            run_id=run_id,
            success=False,
            build_id=None,
            rollup=rollup,
            review=None,
        )

    # The live review fan-out over the authored diff — the quality gate. The
    # reviewer sequence is selected from which engines authored (registry-driven,
    # unioned + de-duped); each reviewer delegates at READ_ONLY on `{diff, goal}`.
    # The fan-out + persistence run under the same fail-the-run guard as the
    # engines above: the engineers have already authored real files and the run
    # row is `running`, so an exception here (a malformed reviewer payload →
    # ReviewFanOutError, a routing miss → NoAgentMatch, a DB error on persist)
    # must mark the run terminal — never leave an orphaned `running` row — and
    # surface as a clean BuildError rather than an uncaught traceback. The gate
    # stays fail-CLOSED: any such exception propagates BEFORE a Build row is
    # written, so nothing ships.
    try:
        review = run_review_fan_out(
            classifications=[s.classification for s in sub_goals],
            files_changed=authored,
            goal=goal,
            config=config,
            project_dir=project_dir,
            client=client,
            model=config.models.default_model,
            registry=registry,
            pre_build=pre_build,
            hook_factory=hook_factory,
            max_turns=max_turns,
        )

        if not review.passed:
            # A blocker/major finding BLOCKS — surface it, do not silently ship.
            # No Build row, no current_build_id advance, the plan stays drafted.
            blockers = _blocking_findings(review)
            repository.update_run_status(
                run_id,
                "failed",
                error=(
                    f"Review gate blocked the build: {len(blockers)} "
                    f"blocker/major finding(s). {_summarize_findings(blockers)}"
                ),
            )
            logger.warning(
                "Build of plan %r blocked by %d blocker/major review finding(s).",
                plan_id,
                len(blockers),
            )
            return _multi_engine_artifact(
                plan_id=plan_id,
                pipeline_name=pipeline_name,
                active_target=active_target,
                files_written=authored,
                summary=_summarize_findings(blockers),
                run_id=run_id,
                success=False,
                build_id=None,
                rollup=rollup,
                review=review,
            )

        # Clean (or warnings-only) review → persist the build. The verdict lands
        # on the Build manifest (a `review` block) AND the returned artifact.
        repository.create_or_update_pipeline(
            name=pipeline_name,
            description=_design_description(design),
            pipeline_dir=f"el/{pipeline_name}",
        )
        repository.attach_pipeline_to_run(run_id, pipeline_name)
        build = repository.create_build(
            pipeline_name=pipeline_name,
            plan_id=plan_id,
            target=active_target,
            manifest={"files": authored, "review": _review_manifest_block(review)},
        )
        repository.set_pipeline_current_build(pipeline_name, build.id)
        repository.mark_plan_built(plan_id=plan_id, pipeline_name=pipeline_name)
        repository.update_run_status(run_id, "success")
    except Exception as exc:
        # Mark the run terminal so it never orphans as `running`. Re-raise a
        # BuildError/AgentError as-is (the CLI renders each cleanly with its own
        # exit code); wrap anything else (e.g. ReviewFanOutError, which the CLI
        # would otherwise let through as a raw traceback) in BuildError.
        repository.update_run_status(run_id, "failed", error=str(exc))
        if isinstance(exc, (BuildError, AgentError)):
            raise
        raise BuildError(
            f"Build of plan {plan_id!r} failed during review/persistence: {exc}"
        ) from exc

    return _multi_engine_artifact(
        plan_id=plan_id,
        pipeline_name=pipeline_name,
        active_target=active_target,
        files_written=authored,
        summary=_summarize_findings(review.findings) or "Built; review clean.",
        run_id=run_id,
        success=True,
        build_id=build.id,
        rollup=rollup,
        review=review,
    )


def _multi_engine_artifact(
    *,
    plan_id: str,
    pipeline_name: str,
    active_target: str,
    files_written: list[str],
    summary: str,
    run_id: str,
    success: bool,
    build_id: str | None,
    rollup: CostRollup,
    review: ReviewResult | None,
) -> BuildArtifact:
    """Assemble the multi-engine path's :class:`BuildArtifact`, verdict included."""
    findings = list(review.findings) if review is not None else []
    return BuildArtifact(
        plan_id=plan_id,
        pipeline_name=pipeline_name,
        pipeline_dir=f"el/{pipeline_name}",
        target=active_target,
        files_written=files_written,
        summary=summary,
        run_id=run_id,
        success=success,
        build_id=build_id,
        tokens_input=rollup.usage.input_tokens,
        tokens_output=rollup.usage.output_tokens,
        cost_usd=rollup.cost_usd,
        # No review ran (an engine failed before the gate) ⇒ the gate did not
        # reject anything ⇒ `review_passed` reflects the verdict if present,
        # else True. The blocking `success=False` is carried by `success`.
        review_passed=review.passed if review is not None else True,
        review_findings=[_finding_summary(f) for f in findings],
        review_blocking_count=len(_blocking_findings(review)) if review is not None else 0,
    )


def _planned_files(design: dict[str, Any]) -> list[str]:
    """The union of every engine's planned file paths from ``planned_by_engine``."""
    raw = design.get("planned_by_engine")
    files: list[str] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            entry_files = entry.get("files")
            if isinstance(entry_files, list):
                files.extend(f for f in entry_files if isinstance(f, str))
    # Back-compat: a flat `planned_files` list (B1 keeps one alongside).
    flat = design.get("planned_files")
    if isinstance(flat, list):
        files.extend(f for f in flat if isinstance(f, str))
    return list(dict.fromkeys(files))


def _snapshot_files(rel_paths: list[str], project_dir: Path) -> dict[Path, str]:
    """Capture ``{abs_path: text}`` for each existing file (pre-build content).

    Missing files are omitted — they diff from empty (greenfield) at render time.
    """
    snapshot: dict[Path, str] = {}
    for rel in rel_paths:
        abs_path = (project_dir / rel).resolve()
        if abs_path.is_file():
            try:
                snapshot[abs_path] = abs_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # A binary pre-build file (e.g. a dbt seed `.csv`, a fixture):
                # not text-diffable, so omit it from the snapshot. The render
                # emits a "Binary files differ" stub from the authored side
                # rather than crashing the build's review gate.
                continue
    return snapshot


def _union_files_changed(results: list[DelegationResult]) -> list[str]:
    """Union every engine's harness-tracked ``files_changed``, order-stable + de-duped."""
    out: list[str] = []
    seen: set[str] = set()
    for result in results:
        for path in result.files_changed:
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _blocking_findings(review: ReviewResult | None) -> list[Finding]:
    """The ``blocker``/``major`` findings — the ones that gate the build."""
    if review is None:
        return []
    # Single-source the gating severities from the driver, so "what blocks" can
    # never drift between `review_fan_out.passed` and this build-side gate.
    return [f for f in review.findings if f.severity in _FAILING_SEVERITIES]


def _finding_summary(finding: Finding) -> dict[str, Any]:
    """A compact, JSON-friendly view of one finding for the artifact / manifest."""
    return {
        "reviewer": finding.reviewer,
        "severity": finding.severity.value,
        "file": finding.file,
        "line": finding.line,
        "message": finding.message,
    }


def _review_manifest_block(review: ReviewResult) -> dict[str, Any]:
    """The ``review`` block nested into the Build manifest (``manifest_json``).

    ``manifest_json`` is the Build's only free-form JSONB column, so the verdict
    nests inside it alongside ``files`` — ``{passed, findings}`` — making the
    review queryable from ``/builds`` later without a schema change.
    """
    return {
        "passed": review.passed,
        "findings": [_finding_summary(f) for f in review.findings],
    }


def _summarize_findings(findings: list[Finding]) -> str:
    """A one-line-per-finding human summary for run errors / artifact summaries."""
    return " | ".join(f"[{f.severity.value}] {f.reviewer} {f.file}: {f.message}" for f in findings)


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
