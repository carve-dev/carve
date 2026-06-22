"""``carve el deploy`` — promote an EL artifact between targets.

Six-phase deploy flow per ``specs/pillar-1-extract-load/08-el-deploy.md``:

1. Validate (target / artifact / build presence).
2. Pre-flight via the deploy role (read-only drift).
3. Confirmation prompt (skipped with ``--yes``).
4. Copy files into the destination's working tree.
5. Apply DDL via the deploy role.
6. Verify via the runtime role.

Every failure mode that can be auto-fixed hands off to the recovery
seam (`RecoveryHandler`); P1-09 will plug a real handler in. P1-08
ships with `NullRecoveryHandler` so the unrecovered path is the
default. Tests inject a fake handler.

The whole flow records exactly one `Run` row of ``kind="deploy"``,
which lets ``carve runs --pipeline <name>`` show deploy history alongside
build / run history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from carve.core.config import ConfigError, load_config
from carve.core.config.schema import Config
from carve.core.connectors.exceptions import SnowflakeError
from carve.core.connectors.snowflake import SnowflakeConnection, SnowflakePool
from carve.core.deploy import (
    NullRecoveryHandler,
    RecoveryContext,
    RecoveryHandler,
    RecoveryStage,
    UncommittedChangesError,
    UnsafeArtifactError,
    apply_ddl,
    copy_artifact,
    copy_ddl_file,
    run_preflight,
    run_verify,
)
from carve.core.deploy.ddl_applier import UnsafeDdlError
from carve.core.deploy.preflight import expected_destinations_from_build
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.models import Build, Plan
from carve.core.targets.names import (
    InvalidArtifactNameError,
    InvalidTargetNameError,
    validate_artifact_name,
    validate_target_name,
)

logger = logging.getLogger(__name__)
console = Console()

# Maximum recovery attempts per failure stage. Mirrors the M1.1-04
# default; P1-09 will read this from ``carve/runner.toml`` instead of
# hard-coding it. P1-08's no-op handler always exhausts in one
# attempt (success=False), so the cap is a soft ceiling.
_DEFAULT_MAX_FIX_ATTEMPTS = 3


def command(
    name: str = typer.Argument(
        ...,
        help="EL artifact name to deploy.",
    ),
    from_target: str = typer.Option(
        ...,
        "--from",
        help="Source target (where the artifact already exists).",
    ),
    to_target: str = typer.Option(
        ...,
        "--to",
        help="Destination target to land the artifact in.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the confirmation prompt before any writes.",
    ),
    no_smoke_test: bool = typer.Option(
        False,
        "--no-smoke-test",
        help="Skip the post-DDL `SELECT 1` queryability check.",
    ),
    no_auto_fix: bool = typer.Option(
        False,
        "--no-auto-fix",
        help="Disable the recovery agent; fail fast on the first error.",
    ),
    max_fix_attempts: int | None = typer.Option(
        None,
        "--max-fix-attempts",
        help=(
            "Override the per-phase recovery attempt budget (default "
            "from carve/runner.toml's [auto_fix] max_attempts)."
        ),
    ),
) -> None:
    """Promote an EL artifact from `<from>` to `<to>`.

    P1.1-01 note: with the flat ``el/<name>/`` layout, files live at
    one location regardless of target. The ``--from / --to`` flags
    retain their P1-08 semantics (catalog inspection runs against
    ``<from>``; DDL apply + verify run against ``<to>``) but the
    file-copy step is a no-op when the source and destination tree
    point at the same path. P1.1-03 rewrites this command around git
    promotion and removes ``--from / --to`` entirely.
    """
    # Validate the name shapes BEFORE any path is constructed. These
    # values flow directly into ``el/<name>/`` paths and a
    # malformed value (``..`` segments, spaces, punctuation) could
    # traverse out of the project tree before the connection lookup
    # would even fire.
    try:
        validate_target_name(from_target)
        validate_target_name(to_target)
        validate_artifact_name(name)
    except (InvalidTargetNameError, InvalidArtifactNameError) as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc

    project_dir = Path.cwd()

    try:
        config = load_config(project_dir)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    repository = Repository(session_factory)

    auto_fix_enabled = (not no_auto_fix) and config.runner.auto_fix.enabled
    attempts_resolved = (
        max_fix_attempts if max_fix_attempts is not None else config.runner.auto_fix.max_attempts
    )

    try:
        exit_code = run_deploy(
            pipeline_name=name,
            source_target=from_target,
            dest_target=to_target,
            config=config,
            project_dir=project_dir,
            repository=repository,
            console=console,
            yes=yes,
            smoke_test=not no_smoke_test,
            auto_fix=auto_fix_enabled,
            max_fix_attempts=attempts_resolved,
        )
    finally:
        engine.dispose()

    raise typer.Exit(code=exit_code)


# ---------------------------------------------------------------------------
# Orchestration entry point (used by tests; the typer command wraps it)
# ---------------------------------------------------------------------------


@dataclass
class DeployContext:
    """Bundle of plumbing that the deploy phases share.

    Pulling this into a dataclass keeps `run_deploy`'s signature
    readable; tests construct it directly to skip the typer harness.
    """

    pipeline_name: str
    source_target: str
    dest_target: str
    project_dir: Path
    config: Config
    repository: Repository
    console: Console
    pool: SnowflakePool
    recovery: RecoveryHandler
    auto_fix: bool
    smoke_test: bool
    yes: bool
    max_fix_attempts: int = _DEFAULT_MAX_FIX_ATTEMPTS
    # Set by `run_deploy` after the deploy Run row is created so the
    # phase-level shims can link recovery-attempt children back to it
    # via `parent_run_id`. `None` until then; the recovery shims no-op
    # the linkage if it's missing.
    deploy_run_id: str | None = None
    target_id: str | None = None


def run_deploy(
    *,
    pipeline_name: str,
    source_target: str,
    dest_target: str,
    config: Config,
    project_dir: Path,
    repository: Repository,
    console: Console,
    yes: bool = False,
    smoke_test: bool = True,
    auto_fix: bool = True,
    pool: SnowflakePool | None = None,
    recovery: RecoveryHandler | None = None,
    confirm: Any = None,
    max_fix_attempts: int | None = None,
) -> int:
    """Execute the six-phase deploy flow.

    Returns a process-style exit code (0 success, 1 runtime failure,
    2 usage error). ``confirm`` is an injectable yes/no prompt; tests
    pass a stub. Production callers leave it `None` and Click's
    ``Confirm.ask`` is used.
    """
    # Defense-in-depth: even though the typer command validates, callers
    # that bypass the typer harness (tests, future programmatic users)
    # land here directly. Validate before constructing any path.
    try:
        validate_target_name(source_target)
        validate_target_name(dest_target)
        validate_artifact_name(pipeline_name)
    except (InvalidTargetNameError, InvalidArtifactNameError) as exc:
        console.print(f"[red]✗[/red] {exc}")
        return 2

    # Track whether we constructed the pool — only close it on exit
    # if we own it. Tests pass a fake pool that has no close_all().
    owns_pool = pool is None
    pool = pool if pool is not None else SnowflakePool(config)
    if recovery is None:
        # P1-09: when auto-fix is on and the caller didn't inject a
        # handler, use the LLM-backed one. With auto-fix off the loop
        # never reaches `recovery.attempt(...)`, so the no-op handler
        # is the cheap default — matches test fixtures that pass
        # `auto_fix=False`.
        if auto_fix:
            recovery = _build_default_recovery_handler(
                config=config,
                project_dir=project_dir,
                repository=repository,
                pool=pool,
                dest_target=dest_target,
            )
        else:
            recovery = NullRecoveryHandler()

    attempts = (
        max_fix_attempts if max_fix_attempts is not None else config.runner.auto_fix.max_attempts
    )

    try:
        return _run_deploy_inner(
            pipeline_name=pipeline_name,
            source_target=source_target,
            dest_target=dest_target,
            config=config,
            project_dir=project_dir,
            repository=repository,
            console=console,
            yes=yes,
            smoke_test=smoke_test,
            auto_fix=auto_fix,
            pool=pool,
            recovery=recovery,
            max_fix_attempts=attempts,
        )
    finally:
        if owns_pool:
            try:
                pool.close_all()
            except Exception:  # pragma: no cover — defensive
                logger.exception("error closing SnowflakePool")


def _run_deploy_inner(
    *,
    pipeline_name: str,
    source_target: str,
    dest_target: str,
    config: Config,
    project_dir: Path,
    repository: Repository,
    console: Console,
    yes: bool,
    smoke_test: bool,
    auto_fix: bool,
    pool: SnowflakePool,
    recovery: RecoveryHandler,
    max_fix_attempts: int = _DEFAULT_MAX_FIX_ATTEMPTS,
) -> int:
    """Inner deploy flow; ``run_deploy`` wraps this to manage pool lifetime."""
    # ---- Phase 1: Validate ------------------------------------------------
    available = sorted(config.connections.snowflake.keys())
    if source_target == dest_target:
        console.print(f"[red]✗[/red] --from and --to must differ; got {source_target!r}.")
        return 2
    for label, target in (("--from", source_target), ("--to", dest_target)):
        if target not in config.connections.snowflake:
            console.print(
                f"[red]✗[/red] {label} target {target!r} not defined in "
                "carve/connections.toml.\n"
                f"  Available targets: {available}"
            )
            return 2

    deploy_target_key = f"{dest_target}_deploy"
    if deploy_target_key not in config.connections.snowflake:
        console.print(
            f"[red]✗[/red] No '{deploy_target_key}' connection in "
            "carve/connections.toml. See docs/deploy-roles.md for the "
            "recommended deploy/runtime role pattern."
        )
        return 2

    # P1.1-01 shim: artifacts live in the flat ``el/<name>/`` tree
    # regardless of source target. The ``--from`` flag still governs
    # catalog-inspection context; the on-disk path no longer encodes
    # it.
    source_artifact = project_dir / "el" / pipeline_name
    if not (source_artifact / "main.py").is_file():
        console.print(
            f"[red]✗[/red] No EL artifact named {pipeline_name!r} on disk "
            f"at el/{pipeline_name}/. Run carve build first."
        )
        return 2

    build = repository.latest_build_for(pipeline_name, source_target)
    if build is None:
        console.print(
            f"[red]✗[/red] No successful Build for pipeline "
            f"{pipeline_name!r} in target {source_target!r}. Run carve build first."
        )
        return 2

    # Resolve the plan design so preflight / verify can compare columns.
    plan_design = _load_plan_design(repository, build.plan_id)

    ctx = DeployContext(
        pipeline_name=pipeline_name,
        source_target=source_target,
        dest_target=dest_target,
        project_dir=project_dir,
        config=config,
        repository=repository,
        console=console,
        pool=pool,
        recovery=recovery,
        auto_fix=auto_fix,
        smoke_test=smoke_test,
        yes=yes,
        max_fix_attempts=max_fix_attempts,
    )

    # Record the deploy run row up front so failures still leave a
    # `kind="deploy"` history entry.
    run_id = repository.create_run(
        kind="deploy",
        target_id=build.id,
        pipeline_name=pipeline_name,
        target=dest_target,
    )
    repository.update_run_status(run_id, "running")

    # Stamp the deploy_run_id on the context so the per-phase recovery
    # shims can persist child Run rows linked via `parent_run_id`. This
    # is what `carve runs <deploy-run-id> --recovery` reads to render
    # the recovery tree.
    ctx.deploy_run_id = run_id
    ctx.target_id = build.id

    try:
        return _execute_phases(
            ctx=ctx,
            build=build,
            plan_design=plan_design,
            run_id=run_id,
        )
    except Exception as exc:
        logger.exception("deploy crashed unexpectedly")
        repository.update_run_status(run_id, "crashed", error=str(exc))
        repository.record_pipeline_run(pipeline_name=pipeline_name, run_id=run_id, status="crashed")
        console.print(f"[red]✗[/red] deploy crashed: {exc}")
        return 1


def _execute_phases(
    *,
    ctx: DeployContext,
    build: Build,
    plan_design: dict[str, Any] | None,
    run_id: str,
) -> int:
    """Run phases 2-6 once `_validate` has succeeded."""
    deploy_target_key = f"{ctx.dest_target}_deploy"

    # ---- Phase 2: Pre-flight ---------------------------------------------
    try:
        deploy_conn: SnowflakeConnection = ctx.pool.get(deploy_target_key)
    except SnowflakeError as exc:
        return _record_terminal_failure(
            ctx,
            run_id,
            f"deploy connection unavailable: {exc}",
            exit_code=2,
        )

    runtime_role: str | None = None
    runtime_section = ctx.config.connections.snowflake.get(ctx.dest_target)
    if runtime_section is not None:
        runtime_role = runtime_section.role

    # P1.1-01 shim: the DDL companion file lives next to main.py in
    # the flat artifact tree (``el/<name>/snowflake.sql``). P1.1-03
    # will rename to ``.sql.j2`` and templatize.
    ddl_path = ctx.project_dir / "el" / ctx.pipeline_name / "snowflake.sql"

    preflight = run_preflight(
        deploy_connection=deploy_conn,
        runtime_role=runtime_role,
        plan_design=plan_design,
    )
    if not preflight.connected:
        diag = "; ".join(d.detail for d in preflight.drift) or "deploy auth failed"
        return _record_terminal_failure(ctx, run_id, diag, exit_code=2)

    if preflight.drift:
        outcome = _maybe_recover(
            ctx=ctx,
            stage=RecoveryStage.PREFLIGHT,
            ddl_path=ddl_path,
            error="; ".join(d.detail for d in preflight.drift),
            drift=tuple(d.detail for d in preflight.drift),
        )
        if outcome is not None:
            return _record_terminal_failure(ctx, run_id, outcome, exit_code=2)

    # ---- Phase 3: Confirmation -------------------------------------------
    if not ctx.yes:
        ctx.console.print(
            f"[bold]Deploy plan[/bold]: {ctx.pipeline_name} "
            f"from {ctx.source_target} → {ctx.dest_target}"
        )
        ctx.console.print(
            f"  Files: el/{ctx.pipeline_name}/ (shared) — "
            f"catalog context: {ctx.source_target} → {ctx.dest_target}"
        )
        if ddl_path.is_file():
            ctx.console.print(f"  DDL:   el/{ctx.pipeline_name}/snowflake.sql")

        # Surface destination.toml overrides — particularly the case
        # where an override differs from the destination target's env
        # default. This is the "promotion mismatch" foot-gun: an override
        # set at plan time in dev silently propagates to prod unless the
        # user notices and edits prod's destination.toml.
        _render_destination_override_warning(ctx)

        # Pass through to typer's confirm; tests bypass with `yes=True`.
        if not typer.confirm("Proceed with deploy?", default=False):
            ctx.console.print("[yellow]Aborted by user.[/yellow]")
            ctx.repository.update_run_status(run_id, "cancelled")
            ctx.repository.record_pipeline_run(
                pipeline_name=ctx.pipeline_name,
                run_id=run_id,
                status="cancelled",
            )
            return 1

    # ---- Phase 4: Copy files ---------------------------------------------
    try:
        copy_artifact(
            project_dir=ctx.project_dir,
            pipeline_name=ctx.pipeline_name,
            source_target=ctx.source_target,
            dest_target=ctx.dest_target,
        )
        copy_ddl_file(
            project_dir=ctx.project_dir,
            pipeline_name=ctx.pipeline_name,
            source_target=ctx.source_target,
            dest_target=ctx.dest_target,
        )
    except UncommittedChangesError as exc:
        return _record_terminal_failure(
            ctx,
            run_id,
            f"uncommitted changes in destination: {', '.join(exc.paths)}",
            exit_code=2,
        )
    except UnsafeArtifactError as exc:
        return _record_terminal_failure(ctx, run_id, str(exc), exit_code=2)
    except FileNotFoundError as exc:
        return _record_terminal_failure(ctx, run_id, str(exc), exit_code=2)

    # ---- Phase 5: Apply DDL ----------------------------------------------
    if not ddl_path.is_file():
        ctx.console.print(f"[yellow]No DDL file at {ddl_path}; skipping DDL apply.[/yellow]")
    else:
        ddl_outcome = _apply_ddl_with_recovery(
            ctx=ctx,
            deploy_conn=deploy_conn,
            ddl_path=ddl_path,
        )
        if ddl_outcome is not None:
            return _record_terminal_failure(ctx, run_id, ddl_outcome, exit_code=1)

    # ---- Phase 6: Verify -------------------------------------------------
    try:
        runtime_conn = ctx.pool.get(ctx.dest_target)
    except SnowflakeError as exc:
        return _record_terminal_failure(
            ctx, run_id, f"runtime connection unavailable: {exc}", exit_code=1
        )

    verify_outcome = _verify_with_recovery(
        ctx=ctx,
        runtime_conn=runtime_conn,
        build=build,
        plan_design=plan_design,
        runtime_role=runtime_role,
        ddl_path=ddl_path,
        deploy_conn=deploy_conn,
    )
    if verify_outcome is not None:
        return _record_terminal_failure(ctx, run_id, verify_outcome, exit_code=1)

    # ---- Success ---------------------------------------------------------
    ctx.repository.update_run_status(run_id, "success")
    ctx.repository.record_pipeline_run(
        pipeline_name=ctx.pipeline_name, run_id=run_id, status="success"
    )
    ctx.console.print(f"[green]✓[/green] deployed {ctx.pipeline_name} to {ctx.dest_target}")
    return 0


# ---------------------------------------------------------------------------
# Recovery wrappers
# ---------------------------------------------------------------------------


def _maybe_recover(
    *,
    ctx: DeployContext,
    stage: RecoveryStage,
    ddl_path: Path,
    error: str,
    drift: tuple[str, ...] = (),
    failing_index: int | None = None,
    failing_sql: str | None = None,
) -> str | None:
    """Hand a single failure to the recovery handler.

    Returns ``None`` on successful recovery (caller proceeds), or the
    user-facing diagnosis string when the failure is unrecoverable
    (caller stops).

    Also persists a child ``Run`` row linked to the deploy run via
    ``parent_run_id``, so ``carve runs <deploy-run-id> --recovery``
    renders the chain. The child row carries
    ``kind="recovery_<stage>"`` (e.g. ``recovery_ddl_apply``); status
    is ``success`` on recovered, ``failed`` on unrecovered/refused.
    """
    if not ctx.auto_fix:
        return error

    context = RecoveryContext(
        stage=stage,
        pipeline_name=ctx.pipeline_name,
        source_target=ctx.source_target,
        dest_target=ctx.dest_target,
        project_dir=ctx.project_dir,
        ddl_path=ddl_path,
        error=error,
        failing_statement_index=failing_index,
        failing_sql=failing_sql,
        drift=drift,
    )

    # Persist the recovery attempt as a child Run row BEFORE calling
    # the handler so the row exists if the handler crashes. We update
    # the status after the attempt completes.
    child_run_id: str | None = None
    if ctx.deploy_run_id is not None:
        try:
            child_run_id = ctx.repository.create_run(
                kind=f"recovery_{stage.value}",
                target_id=ctx.target_id or ctx.deploy_run_id,
                pipeline_name=ctx.pipeline_name,
                target=ctx.dest_target,
                parent_run_id=ctx.deploy_run_id,
            )
            ctx.repository.update_run_status(child_run_id, "running")
        except Exception:  # pragma: no cover — defensive
            logger.exception("failed to persist recovery child Run row")

    result = ctx.recovery.attempt(context)

    if child_run_id is not None:
        try:
            if result.success:
                ctx.repository.update_run_status(child_run_id, "success", error=result.diagnosis)
            else:
                ctx.repository.update_run_status(
                    child_run_id,
                    "failed",
                    error=result.diagnosis or error,
                )
        except Exception:  # pragma: no cover — defensive
            logger.exception("failed to update recovery child Run status")

    if result.success:
        ctx.console.print(
            f"[yellow]recovery agent attempted fix at {stage.value} "
            f"stage: {result.diagnosis}[/yellow]"
        )
        return None
    return result.diagnosis or error


def _apply_ddl_with_recovery(
    *,
    ctx: DeployContext,
    deploy_conn: SnowflakeConnection,
    ddl_path: Path,
) -> str | None:
    """Apply DDL, looping through recovery attempts until success or budget."""
    start_index = 0
    attempts = 0
    last_error: str = ""
    while attempts <= ctx.max_fix_attempts:
        try:
            result = apply_ddl(
                deploy_connection=deploy_conn,
                ddl_path=ddl_path,
                start_index=start_index,
            )
        except UnsafeDdlError as exc:
            # Structural failure — recovery does NOT participate.
            # Surface the index and the rule label only; never the SQL
            # text (the file may embed credentials or other sensitive
            # values, and `Run.error_message` persists across runs).
            ctx.console.print(f"[red]✗[/red] {exc}")
            return str(exc)
        if result.success:
            return None
        failure = result.failure
        if failure is None:
            return None
        # Persisted error message must not contain the SQL text — only
        # the index and the driver's error string. The full SQL is
        # still available in-memory via `RecoveryContext.failing_sql`
        # for the recovery handler's own use.
        last_error = f"DDL statement #{failure.index} failed: {failure.error}"
        if not ctx.auto_fix:
            return last_error
        attempts += 1
        if attempts > ctx.max_fix_attempts:
            break
        recovery_outcome = _maybe_recover(
            ctx=ctx,
            stage=RecoveryStage.DDL_APPLY,
            ddl_path=ddl_path,
            error=last_error,
            failing_index=failure.index,
            failing_sql=failure.sql,
        )
        if recovery_outcome is not None:
            # Handler refused; surface its diagnosis.
            return recovery_outcome
        # Recovery succeeded — re-read the file and retry from the
        # failing statement (the handler is expected to land
        # idempotent edits).
        start_index = failure.index
    return last_error or "DDL apply exhausted recovery budget"


def _verify_with_recovery(
    *,
    ctx: DeployContext,
    runtime_conn: SnowflakeConnection,
    build: Build,
    plan_design: dict[str, Any] | None,
    runtime_role: str | None,
    ddl_path: Path,
    deploy_conn: SnowflakeConnection,
) -> str | None:
    """Verify, looping through recovery attempts until success or budget."""
    attempts = 0
    last_error = ""
    while attempts <= ctx.max_fix_attempts:
        result = run_verify(
            runtime_connection=runtime_conn,
            build=build,
            plan_design=plan_design,
            runtime_role=runtime_role,
            smoke_test=ctx.smoke_test,
        )
        if result.ok:
            return None
        last_error = "verify failed: " + "; ".join(result.failures)
        if not ctx.auto_fix:
            return last_error
        attempts += 1
        if attempts > ctx.max_fix_attempts:
            break
        recovery_outcome = _maybe_recover(
            ctx=ctx,
            stage=RecoveryStage.VERIFY,
            ddl_path=ddl_path,
            error=last_error,
        )
        if recovery_outcome is not None:
            return recovery_outcome
        # On verify recovery, the agent has typically appended a GRANT
        # to the DDL file. Re-apply DDL from the start; idempotent
        # statements are no-ops and the appended one lands.
        try:
            ddl_result = apply_ddl(
                deploy_connection=deploy_conn,
                ddl_path=ddl_path,
                start_index=0,
            )
        except UnsafeDdlError as exc:
            ctx.console.print(f"[red]✗[/red] {exc}")
            return str(exc)
        if not ddl_result.success and ddl_result.failure is not None:
            return f"verify-recovery DDL re-apply failed: {ddl_result.failure.error}"
    return last_error or "verify exhausted recovery budget"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_default_recovery_handler(
    *,
    config: Config,
    project_dir: Path,
    repository: Repository,
    pool: SnowflakePool,
    dest_target: str,
) -> RecoveryHandler:
    """Construct the default `LLMRecoveryHandler` when auto-fix is on.

    Opens BOTH the deploy-role and runtime-role connections to the
    destination target and threads them through to the agent. This
    preserves the spec's privilege-envelope guarantee:

    * PREFLIGHT / DDL_APPLY contexts use the deploy-role connection for
      ``run_snowflake_query`` (matching what those phases of deploy
      themselves use).
    * VERIFY context uses the runtime-role connection for
      ``run_snowflake_query`` (matching what `verify` itself uses).
    * ``run_snowflake_ddl`` (only available in DDL_APPLY / VERIFY
      contexts) always uses the deploy role.

    Without this plumbing the deploy fallback path would either silently
    lose the inspection tools or — worse — leak deploy-role privileges
    into the verify context.

    Tests that want the no-op behavior either inject their own handler
    explicitly or pass ``auto_fix=False``. Production callers leave
    both unset and land here.

    Loading the handler is wrapped to catch ``ConfigError`` (the
    expected failure mode when the Anthropic key is missing) and falls
    back to `NullRecoveryHandler` rather than blocking the deploy from
    running at all.
    """
    del project_dir
    deploy_conn: SnowflakeConnection | None = None
    runtime_conn: SnowflakeConnection | None = None
    try:
        deploy_conn = pool.get(f"{dest_target}_deploy")
    except SnowflakeError:
        logger.warning(
            "deploy-role connection unavailable for recovery handler; "
            "DDL execution and DDL-apply context inspection will not be wired"
        )
    try:
        runtime_conn = pool.get(dest_target)
    except SnowflakeError:
        logger.warning(
            "runtime-role connection unavailable for recovery handler; "
            "verify-context inspection will fall back to the deploy role"
        )
    try:
        from carve.core.agents.recovery import LLMRecoveryHandler

        return LLMRecoveryHandler(
            config=config,
            repository=repository,
            deploy_query_runner=deploy_conn,
            deploy_ddl_executor=deploy_conn,
            runtime_query_runner=runtime_conn,
        )
    except ConfigError:  # pragma: no cover — defensive fallback
        logger.exception("failed to construct LLMRecoveryHandler; falling back to no-op")
        return NullRecoveryHandler()


def _record_terminal_failure(
    ctx: DeployContext,
    run_id: str,
    diagnosis: str,
    *,
    exit_code: int,
) -> int:
    """Stamp the Run as failed and print the diagnosis. Returns ``exit_code``."""
    ctx.repository.update_run_status(run_id, "failed", error=diagnosis)
    ctx.repository.record_pipeline_run(
        pipeline_name=ctx.pipeline_name, run_id=run_id, status="failed"
    )
    ctx.console.print(f"[red]✗[/red] {diagnosis}")
    return exit_code


def _load_plan_design(repository: Repository, plan_id: str) -> dict[str, Any] | None:
    """Read the plan's design block. Returns ``None`` on any parse failure."""
    plan: Plan | None = repository.get_plan(plan_id)
    if plan is None:
        return None
    # v0.1-01: task_graph_json is JSONB; ORM returns dict directly.
    task_graph = plan.task_graph_json
    if not isinstance(task_graph, dict):
        return None
    design = task_graph.get("design")
    return design if isinstance(design, dict) else None


def _render_destination_override_warning(ctx: DeployContext) -> None:
    """Surface destination.toml overrides before the deploy proceeds.

    The source's ``destination.toml`` will be copied verbatim to the
    destination target. When it carries overrides (database / schema
    set explicitly), the user might be surprised that the override
    propagates — particularly if the override differs from the
    destination target's connection defaults.

    No-op when destination.toml is missing or carries no overrides.
    """
    from carve.core.targets.destination import read_destination_toml

    # P1.1-01 shim: destination.toml lives in the flat artifact tree.
    # P1.1-02 will sectionize it with `[default]` + per-target blocks;
    # for now the file is shared across targets and back-to-back builds
    # with different active targets are last-write-wins.
    src_path = ctx.project_dir / "el" / ctx.pipeline_name / "destination.toml"
    try:
        destination = read_destination_toml(src_path)
    except ValueError as exc:
        ctx.console.print(f"[red]✗[/red] {src_path} is malformed: {exc}")
        return
    if destination is None:
        return  # nothing to surface

    has_db_override = destination.has_database_override
    has_schema_override = destination.has_schema_override
    if not (has_db_override or has_schema_override):
        return  # table-only file, no overrides to flag

    # Pull the destination target's connection defaults so we can show
    # the user what the override differs from (the high-friction case).
    dest_section = ctx.config.connections.snowflake.get(ctx.dest_target)
    dest_env_db = dest_section.database if dest_section is not None else None
    dest_env_schema = dest_section.schema_ if dest_section is not None else None

    ctx.console.print()
    ctx.console.print("[bold yellow]⚠ destination.toml carries overrides:[/bold yellow]")
    ctx.console.print(f"  table:    [bold]{destination.table}[/bold]  [dim](always literal)[/dim]")
    if has_db_override:
        if dest_env_db == destination.database:
            ctx.console.print(
                f"  database: [bold]{destination.database}[/bold]  "
                f"[dim](matches {ctx.dest_target}'s default — no effect)[/dim]"
            )
        else:
            ctx.console.print(
                f"  database: [bold]{destination.database}[/bold]  "
                f"[yellow](override; {ctx.dest_target}'s default is "
                f"{dest_env_db or '<unset>'})[/yellow]"
            )
    if has_schema_override:
        if dest_env_schema == destination.schema:
            ctx.console.print(
                f"  schema:   [bold]{destination.schema}[/bold]  "
                f"[dim](matches {ctx.dest_target}'s default — no effect)[/dim]"
            )
        else:
            ctx.console.print(
                f"  schema:   [bold]{destination.schema}[/bold]  "
                f"[yellow](override; {ctx.dest_target}'s default is "
                f"{dest_env_schema or '<unset>'})[/yellow]"
            )
    ctx.console.print(
        "[dim]To change the override before deploying, edit "
        f"el/{ctx.pipeline_name}/destination.toml "
        "after this confirmation, then re-run.[/dim]"
    )
    ctx.console.print()


# Public re-exports for downstream consumers (the deprecated alias
# at `src/carve/cli/commands/deploy.py` re-imports `command` from here).
__all__ = ["DeployContext", "command", "run_deploy"]


# Keep an inert reference so static analyzers don't drop the import
# used only by tests.
_ = expected_destinations_from_build
