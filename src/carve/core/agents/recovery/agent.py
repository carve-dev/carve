"""Recovery agent — runs through `AgentLoop` per attempt (P1-09).

Public surface:

* :func:`run_recovery_agent` — given an :class:`Invocation`, runs the
  agent through one bounded attempt and returns a
  :class:`RecoveryAttemptResult`.
* :class:`SubmitDiagnosisCapture` — the terminator-tool capture object.

The function mirrors the shape of
``carve.core.agents.extract_load.agent.run_extract_load_agent``: build
tools per invocation, compose the system prompt with a trigger-context
preamble, instantiate `AgentLoop`, drive the loop, capture the
``submit_diagnosis`` payload, return.

The orchestrator (``carve.cli.orchestrator.recovery``) is the layer
that sequences attempts, persists child Run rows, and tracks per-context
budgets. This module owns one attempt at a time — no state outside the
returned dataclass.

Tests inject `client` to skip real Anthropic calls. The LLM-backed
:class:`LLMRecoveryHandler` (defined here, used by the orchestrator)
binds the agent to the deploy command's `RecoveryHandler` Protocol so
P1-08's deploy code can swap the no-op handler for this one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from carve.core.agents.loop import AgentLoop, AgentResult
from carve.core.agents.observer import AgentObserver, NullObserver
from carve.core.agents.recovery.invocation import (
    DeployDdlApplyInvocation,
    DeployPreflightInvocation,
    DeployVerifyInvocation,
    ElRunInvocation,
    Invocation,
    TriggerContext,
)
from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.agents.tools.extract_load_tools import (
    make_read_file_tool,
    make_run_snowflake_query_tool,
    make_write_file_tool,
)
from carve.core.deploy.recovery import (
    RecoveryContext,
    RecoveryHandler,
    RecoveryResult,
    RecoveryStage,
)
from carve.core.state.repository import Repository

logger = logging.getLogger(__name__)


_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def load_recovery_agent_prompt() -> str:
    """Load the recovery-agent system prompt from disk."""
    return (_PROMPTS_DIR / "recovery_agent.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Result / capture
# ---------------------------------------------------------------------------


@dataclass
class RecoveryAttemptResult:
    """Outcome of one recovery agent attempt.

    The orchestrator inspects ``category`` to detect
    ``repeated_identical`` and out-of-scope branches, ``action_taken``
    for telemetry, ``summary`` for the user-facing diagnosis surfaced
    on the run row.
    """

    category: str
    summary: str
    action_taken: str
    refused: bool
    agent_result: AgentResult | None = None


@dataclass
class SubmitDiagnosisCapture:
    """Captures the agent's terminator-tool payload.

    Mirrors :class:`SubmitStepCapture` from extract-load but with the
    recovery-specific schema (``category`` / ``summary`` /
    ``action_taken``). A second ``submit_diagnosis`` call within the
    same capture raises ``ToolExecutionError`` — the loop's terminator
    semantics already prevent this in practice but the guard is cheap
    insurance.
    """

    payload: dict[str, Any] | None = None
    _called: bool = field(default=False, init=False)

    @property
    def submitted(self) -> bool:
        return self.payload is not None

    @property
    def category(self) -> str:
        if self.payload is None:
            return ""
        value = self.payload.get("category")
        return value if isinstance(value, str) else ""

    @property
    def summary(self) -> str:
        if self.payload is None:
            return ""
        value = self.payload.get("summary")
        return value if isinstance(value, str) else ""

    @property
    def action_taken(self) -> str:
        if self.payload is None:
            return ""
        value = self.payload.get("action_taken")
        return value if isinstance(value, str) else ""


SUBMIT_DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "description": (
                "One of: code_fix, auth, permission, resource_exhaustion, "
                "user_cancel, repeated_identical, out_of_scope. The "
                "orchestrator branches on this."
            ),
            "enum": [
                "code_fix",
                "auth",
                "permission",
                "resource_exhaustion",
                "user_cancel",
                "repeated_identical",
                "out_of_scope",
            ],
        },
        "summary": {
            "type": "string",
            "description": (
                "One- to three-sentence diagnosis. Lands on Run.error_message "
                "when the loop bails; otherwise printed to the user."
            ),
        },
        "action_taken": {
            "type": "string",
            "description": (
                "Short imperative description of the fix you applied "
                "(e.g. 'edited el/iowa/main.py to json.dumps the "
                "location field') or 'none' when no fix was applied."
            ),
        },
    },
    "required": ["category", "summary", "action_taken"],
}


def make_submit_diagnosis_tool(capture: SubmitDiagnosisCapture) -> Tool:
    """Build a `submit_diagnosis` tool that records the payload on `capture`."""

    def _execute(input_: ToolInput) -> ToolResult:
        if capture._called:
            raise ToolExecutionError(
                "submit_diagnosis already called; only one terminal "
                "payload may be submitted per attempt."
            )
        if not isinstance(input_, dict):
            raise ToolExecutionError("submit_diagnosis input must be an object.")
        capture.payload = dict(input_)
        capture._called = True
        return {"status": "submitted"}

    return Tool(
        name="submit_diagnosis",
        description=(
            "Finalize this recovery attempt. Call exactly once. Specifies "
            "the failure category (drives orchestrator branching), a "
            "short user-facing summary, and the action you took (or "
            "'none' if you couldn't fix the failure)."
        ),
        input_schema=SUBMIT_DIAGNOSIS_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# Recovery-specific tools
# ---------------------------------------------------------------------------


READ_RUN_LOGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "default": 200,
            "description": "Maximum log lines to return (newest last).",
        },
    },
    "required": [],
}


def make_read_run_logs_tool(
    repository: Repository,
    *,
    default_run_id: str,
) -> Tool:
    """Build `read_run_logs(limit)` bound to ``repository``.

    Reads logs for the failing run only — the tool is pinned to the
    bound ``default_run_id`` and does not accept an arbitrary
    ``run_id`` parameter. This prevents the agent from tailing logs
    of unrelated runs (cross-pipeline log access). The orchestrator
    creates the recovery child run as a descendant of the failing run
    so its own ancestor logs are available implicitly via that chain;
    the agent has no need to fetch sibling runs by id.
    """

    def _execute(input_: ToolInput) -> ToolResult:
        # ``run_id`` is intentionally not part of the input schema; the
        # tool is pinned to the failing run.
        limit_raw = input_.get("limit", 200)
        if not isinstance(limit_raw, int) or isinstance(limit_raw, bool) or limit_raw <= 0:
            limit = 200
        else:
            limit = limit_raw

        run_id = default_run_id
        logs = repository.get_logs(run_id)
        if not logs:
            return {"run_id": run_id, "lines": [], "count": 0}
        # Tail-bias: keep the newest `limit` lines so the agent sees the
        # actual error, not the bootstrap noise.
        if len(logs) > limit:
            logs = logs[-limit:]
        lines = [
            f"[{log.level}] {log.source}: {log.message}"
            for log in logs
        ]
        return {"run_id": run_id, "lines": lines, "count": len(lines)}

    return Tool(
        name="read_run_logs",
        description=(
            "Read the failing run's logs from the Carve state store. "
            "Returns the newest `limit` log lines, tagged with level "
            "and source. Use this to inspect tracebacks or driver "
            "errors that the failure summary truncates. The tool is "
            "pinned to the failing run id; you cannot read logs of "
            "unrelated runs."
        ),
        input_schema=READ_RUN_LOGS_SCHEMA,
        executor=_execute,
    )


RUN_SNOWFLAKE_DDL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": (
                "A single DDL statement to run against the dest target's "
                "deploy role. Multi-statement input is rejected."
            ),
        },
    },
    "required": ["sql"],
}


class _DdlExecutor(Protocol):
    """Minimal protocol for the DDL-apply tool's runtime.

    Implementations satisfy this if they expose ``execute(sql) -> int``.
    The real Snowflake connector and the test fakes both qualify.
    """

    def execute(self, sql: str) -> int:
        ...


def make_run_snowflake_ddl_tool(executor: _DdlExecutor) -> Tool:
    """Build `run_snowflake_ddl(sql)` bound to a deploy-role connection.

    Used only in the DDL-apply trigger context. Multi-statement input
    is rejected — the recovery agent re-runs one statement at a time
    so the orchestrator can track per-statement progress. Statements
    are routed through the same P1-08 allow-list (`validate_ddl_statements`)
    that gates the file-driven DDL apply path, so the agent cannot
    bypass safety rules by issuing destructive DDL directly.
    """

    def _execute(input_: ToolInput) -> ToolResult:
        sql = input_.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ToolExecutionError("`sql` must be a non-empty string.")
        # Reject multi-statement payloads. A `;` is acceptable only as
        # trailing whitespace.
        semi = sql.find(";")
        if semi != -1 and sql[semi + 1 :].strip():
            raise ToolExecutionError(
                "Multi-statement input is not allowed; submit one DDL "
                "statement at a time."
            )
        # Route through the P1-08 allow-list so destructive DDL (DROP
        # DATABASE, CREATE OR REPLACE, DML, etc.) is rejected before it
        # ever reaches the deploy connection. This mirrors what the
        # `apply_ddl` path does for file-driven DDL.
        from carve.core.deploy.ddl_applier import (
            UnsafeDdlError,
            parse_ddl_statements,
            validate_ddl_statements,
        )

        parsed = parse_ddl_statements(sql)
        try:
            validate_ddl_statements(parsed)
        except UnsafeDdlError as exc:
            raise ToolExecutionError(
                f"DDL rejected by allow-list: {exc}. Edit the DDL file and let "
                f"the orchestrator re-apply via the validated path."
            ) from exc
        try:
            executor.execute(sql)
        except Exception as exc:
            raise ToolExecutionError(f"DDL failed: {exc}") from exc
        return {"status": "ok"}

    return Tool(
        name="run_snowflake_ddl",
        description=(
            "Execute a single DDL statement against the destination "
            "target's deploy role. Use this only when you need to "
            "patch destination schema state directly; prefer editing "
            "the DDL file with `write_file` so the change persists. "
            "DDL must be idempotent (CREATE...IF NOT EXISTS, GRANT, "
            "ALTER...ADD COLUMN IF NOT EXISTS); CREATE OR REPLACE / "
            "DROP without IF EXISTS / DML / RENAME are rejected by the "
            "allow-list."
        ),
        input_schema=RUN_SNOWFLAKE_DDL_SCHEMA,
        executor=_execute,
    )


REQUEST_HUMAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "Why human intervention is needed.",
        },
    },
    "required": ["reason"],
}


def make_request_human_tool() -> Tool:
    """A no-op surface that lets the agent flag escape-hatch cases.

    The orchestrator inspects the ``submit_diagnosis`` payload — this
    tool is purely a way for the agent to record ``reason`` mid-loop
    so the eventual diagnosis can refer to it. It never blocks.
    """

    def _execute(input_: ToolInput) -> ToolResult:
        reason = input_.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ToolExecutionError("`reason` must be a non-empty string.")
        return {"acknowledged": True, "reason": reason}

    return Tool(
        name="request_human",
        description=(
            "Flag that this failure needs human intervention (e.g. role "
            "hierarchy change, credential rotation). Records the reason "
            "but does not block; you must still call submit_diagnosis "
            "with category='permission' or 'auth' as appropriate."
        ),
        input_schema=REQUEST_HUMAN_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# Tool-set assembly per invocation
# ---------------------------------------------------------------------------


@dataclass
class _Tools:
    """Tool list + capture, returned by :func:`build_tools_for_invocation`."""

    tools: list[Tool]
    submit_diagnosis_capture: SubmitDiagnosisCapture


def build_tools_for_invocation(
    invocation: Invocation,
    *,
    repository: Repository,
    snowflake_query_runner: Any | None = None,
    snowflake_ddl_executor: _DdlExecutor | None = None,
) -> _Tools:
    """Construct the tools allowed in `invocation`'s trigger context.

    The four contexts share the same skeleton: read_file + read_run_logs
    + submit_diagnosis + request_human. They differ in (a) write
    authority and (b) whether DDL execution is on the table. The
    extract-load-style ``run_snowflake_query`` is included whenever
    the caller passes a runner (i.e. when the invocation has a target
    connection).
    """
    project_dir = invocation.project_dir
    capture = SubmitDiagnosisCapture()
    tools: list[Tool] = []

    tools.append(make_read_file_tool(project_dir))
    tools.append(
        make_read_run_logs_tool(
            repository,
            default_run_id=invocation.failed_run_id,
        )
    )
    tools.append(make_request_human_tool())
    tools.append(make_submit_diagnosis_tool(capture))

    if snowflake_query_runner is not None:
        tools.append(make_run_snowflake_query_tool(snowflake_query_runner))

    allowed_paths = _allowed_write_paths(invocation)
    if allowed_paths is not None and allowed_paths:
        tools.append(make_write_file_tool(project_dir, allowed_paths))

    if (
        invocation.trigger == TriggerContext.DEPLOY_DDL_APPLY
        and snowflake_ddl_executor is not None
    ):
        tools.append(make_run_snowflake_ddl_tool(snowflake_ddl_executor))
    elif (
        invocation.trigger == TriggerContext.DEPLOY_VERIFY
        and snowflake_ddl_executor is not None
    ):
        tools.append(make_run_snowflake_ddl_tool(snowflake_ddl_executor))

    return _Tools(tools=tools, submit_diagnosis_capture=capture)


def _allowed_write_paths(invocation: Invocation) -> set[Path] | None:
    """Resolved-absolute path set passed to the write_file allow-list.

    P1.1-01 flattened the layout: every writable path lives under
    ``el/<name>/`` rather than ``targets/<t>/...``.

    * Phase 1 (preflight) — read-only: returns ``None`` to signal "no
      write_file tool".
    * `el run` failure — the EL script + requirements file.
    * Phase 2 / Phase 3 — same files plus the companion DDL so the
      agent can fix either the SQL or the script.
    """
    pd = invocation.project_dir.resolve()

    if isinstance(invocation, DeployPreflightInvocation):
        return None

    if isinstance(invocation, ElRunInvocation):
        # Spec table row #1: allow-list is `el/<name>/` only. The
        # companion DDL file is NOT writable from el-run context — DDL
        # changes belong to the deploy phase, not to the runtime-
        # failure recovery loop.
        name = invocation.pipeline_name
        return {
            (pd / "el" / name / "main.py").resolve(),
            (pd / "el" / name / "requirements.txt").resolve(),
        }

    if isinstance(invocation, DeployDdlApplyInvocation | DeployVerifyInvocation):
        name = invocation.pipeline_name
        return {
            (pd / "el" / name / "snowflake.sql").resolve(),
            (pd / "el" / name / "main.py").resolve(),
            (pd / "el" / name / "requirements.txt").resolve(),
        }

    return None  # pragma: no cover — exhaustiveness defensive branch


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


_PREAMBLES: dict[TriggerContext, str] = {
    TriggerContext.EL_RUN_FAILURE: (
        "## Trigger context\n\n"
        "`carve el run <name>` failed at runtime. The script ran under the "
        "runtime role for the active target. You can edit the script and "
        "companion DDL via `write_file`, then the orchestrator retries."
    ),
    TriggerContext.DEPLOY_PREFLIGHT: (
        "## Trigger context\n\n"
        "`carve el deploy <name> --from <X> --to <Y>` Phase 1 detected drift "
        "between the destination's existing schema and the deploy plan. "
        "**This is read-only context** — `write_file` is NOT available. "
        "Surface the drift in your diagnosis."
    ),
    TriggerContext.DEPLOY_DDL_APPLY: (
        "## Trigger context\n\n"
        "`carve el deploy` Phase 2 (DDL apply) failed at a specific "
        "statement. The deploy role is connected. You can edit the DDL "
        "file and the orchestrator re-applies idempotently from where "
        "the failure occurred."
    ),
    TriggerContext.DEPLOY_VERIFY: (
        "## Trigger context\n\n"
        "`carve el deploy` Phase 3 (post-DDL smoke verify) failed against "
        "the runtime role. Common causes: missing GRANT on the new table, "
        "warehouse access, schema visibility. Edit the DDL to append the "
        "needed statements and the orchestrator re-applies + re-verifies."
    ),
}


def _render_invocation_block(invocation: Invocation) -> str:
    """Per-invocation factual section appended to the system prompt."""
    lines = [
        "## This attempt",
        f"- **Pipeline:** `{invocation.pipeline_name}`",
        f"- **Failed run id:** `{invocation.failed_run_id}`",
    ]
    if isinstance(invocation, ElRunInvocation):
        lines.append(f"- **Active target:** `{invocation.active_target}`")
    elif isinstance(
        invocation,
        DeployPreflightInvocation | DeployDdlApplyInvocation | DeployVerifyInvocation,
    ):
        lines.append(f"- **Source target:** `{invocation.source_target}`")
        lines.append(f"- **Destination target:** `{invocation.dest_target}`")
        lines.append(f"- **DDL path:** `{invocation.ddl_path}`")
    if isinstance(invocation, DeployDdlApplyInvocation):
        if invocation.failing_statement_index is not None:
            lines.append(
                f"- **Failing statement index:** {invocation.failing_statement_index}"
            )
    if isinstance(invocation, DeployPreflightInvocation) and invocation.drift:
        lines.append("")
        lines.append("### Drift report")
        for item in invocation.drift:
            lines.append(f"- {item}")
    lines.append("")
    lines.append("### Error text")
    lines.append("```")
    lines.append(invocation.error_text or "(no error text provided)")
    lines.append("```")
    return "\n".join(lines)


def _compose_system_prompt(invocation: Invocation) -> str:
    """Stitch the base prompt + trigger preamble + invocation block."""
    parts: list[str] = [
        load_recovery_agent_prompt(),
        _PREAMBLES[invocation.trigger],
        _render_invocation_block(invocation),
    ]
    return "\n\n".join(parts)


def _compose_initial_user_message(invocation: Invocation) -> str:
    """Frame the recovery attempt as a single user message."""
    return (
        f"Diagnose and (if you can) fix the failure of "
        f"`{invocation.pipeline_name}` (run {invocation.failed_run_id}). "
        "Use the tools listed; call `submit_diagnosis(...)` exactly once "
        "to terminate."
    )


# ---------------------------------------------------------------------------
# Public entry: run one attempt
# ---------------------------------------------------------------------------


class RecoveryAgentError(Exception):
    """Raised when the agent loop completes without `submit_diagnosis`."""


def run_recovery_agent(
    invocation: Invocation,
    *,
    repository: Repository,
    run_id: str | None = None,
    client: Any | None = None,
    snowflake_query_runner: Any | None = None,
    snowflake_ddl_executor: _DdlExecutor | None = None,
    observer: AgentObserver | None = None,
    max_turns: int = 12,
    max_tokens: int = 4096,
) -> RecoveryAttemptResult:
    """Run one recovery attempt.

    The caller (the recovery orchestrator) is responsible for:

    * Creating the child `Run` row via ``repository.create_run(...,
      parent_run_id=...)`` before calling this function.
    * Persisting the result on disk (the agent's `write_file` calls
      already land in the working tree).
    * Retrying the original operation after success.

    Returns a :class:`RecoveryAttemptResult` whose ``category`` and
    ``summary`` come straight from the agent's `submit_diagnosis` call.
    """
    config = invocation.config

    bundle = build_tools_for_invocation(
        invocation,
        repository=repository,
        snowflake_query_runner=snowflake_query_runner,
        snowflake_ddl_executor=snowflake_ddl_executor,
    )
    system_prompt = _compose_system_prompt(invocation)
    anthropic_client = _resolve_client(config, client)

    loop = AgentLoop(
        client=anthropic_client,
        tools=bundle.tools,
        system_prompt=system_prompt,
        model=config.models.default_model,
        repository=repository,
        run_id=run_id,
        max_tokens=max_tokens,
        observer=observer if observer is not None else NullObserver(),
        terminator_tool="submit_diagnosis",
    )
    initial_message = _compose_initial_user_message(invocation)
    agent_result = loop.run(initial_message, max_turns=max_turns)

    capture = bundle.submit_diagnosis_capture
    if not capture.submitted:
        raise RecoveryAgentError(
            "Recovery agent finished without calling `submit_diagnosis`. "
            "The orchestrator can't classify this attempt; treating as "
            "an unrecoverable failure."
        )

    return RecoveryAttemptResult(
        category=capture.category,
        summary=capture.summary,
        action_taken=capture.action_taken,
        refused=capture.action_taken.strip().lower() == "none",
        agent_result=agent_result,
    )


def _resolve_client(config: Any, client: Any | None) -> Any:
    """Return ``client`` if provided, else build one from ``config``.

    Mirrors the extract-load agent's helper. Tests pass a fake client
    that records calls; production callers leave this alone.
    """
    from carve.core.agents.client_factory import make_client

    return make_client(config, client)


# ---------------------------------------------------------------------------
# RecoveryHandler (P1-08 Protocol) backed by the LLM agent
# ---------------------------------------------------------------------------


class LLMRecoveryHandler:
    """Concrete `RecoveryHandler` that runs the recovery agent per call.

    Implements the P1-08 Protocol so the deploy command can swap this
    in for `NullRecoveryHandler`. Each ``attempt`` builds an
    :class:`Invocation` from the :class:`RecoveryContext`, runs one
    agent attempt, and translates the
    :class:`RecoveryAttemptResult` back into a
    :class:`RecoveryResult`.

    The unified `run_with_recovery` orchestrator (in
    ``carve.cli.orchestrator.recovery``) doesn't go through this class
    — it calls :func:`run_recovery_agent` directly so it can persist
    child Run rows and track per-context budgets. This handler exists
    purely so the deploy code's existing seam keeps working when the
    orchestrator is itself disabled in tests or pinned to legacy
    behavior.
    """

    def __init__(
        self,
        *,
        config: Any,
        repository: Repository,
        client: Any | None = None,
        deploy_query_runner: Any | None = None,
        deploy_ddl_executor: _DdlExecutor | None = None,
        runtime_query_runner: Any | None = None,
        observer: AgentObserver | None = None,
        max_turns: int = 12,
    ) -> None:
        """Build a handler.

        Two query-runner kwargs are accepted to preserve role discipline:
        ``deploy_query_runner`` runs under the deploy role (used in
        DEPLOY_PREFLIGHT and DEPLOY_DDL_APPLY contexts), and
        ``runtime_query_runner`` runs under the runtime role (used in
        DEPLOY_VERIFY context — verify itself uses the runtime role, so
        the recovery agent must too). ``deploy_ddl_executor`` is the
        only DDL-execution path; the spec puts ``run_snowflake_ddl``
        behind the deploy role for both DDL_APPLY and VERIFY contexts.

        Either runner can be None — the handler falls back to the other
        runner so DEPLOY_VERIFY still has read access during a partial
        misconfiguration. Tests that don't care about role discipline
        can pass a single runner for either slot.
        """
        self._config = config
        self._repository = repository
        self._client = client
        self._deploy_query_runner = deploy_query_runner
        self._deploy_ddl_executor = deploy_ddl_executor
        self._runtime_query_runner = runtime_query_runner
        self._observer = observer
        self._max_turns = max_turns

    def attempt(self, context: RecoveryContext) -> RecoveryResult:
        """Run one recovery agent attempt against ``context``.

        Picks the connection role per-stage to honor the spec's
        privilege-envelope guarantee:

        * PREFLIGHT / DDL_APPLY → deploy-role query runner.
        * VERIFY → runtime-role query runner (matches what verify itself
          uses; recovery doesn't elevate privileges to inspect state).

        ``deploy_ddl_executor`` (deploy role) is wired in DDL_APPLY and
        VERIFY contexts only — preflight is read-only.
        """
        invocation = self._invocation_from_context(context)

        # Pick the right query-runner role for this stage.
        if context.stage == RecoveryStage.VERIFY:
            query_runner = self._runtime_query_runner or self._deploy_query_runner
        else:
            query_runner = self._deploy_query_runner or self._runtime_query_runner

        try:
            result = run_recovery_agent(
                invocation,
                repository=self._repository,
                client=self._client,
                snowflake_query_runner=query_runner,
                snowflake_ddl_executor=self._deploy_ddl_executor,
                observer=self._observer,
                max_turns=self._max_turns,
            )
        except RecoveryAgentError as exc:
            return RecoveryResult(success=False, diagnosis=str(exc))
        # The agent's `category` and `action_taken` together tell us
        # whether the deploy can retry. ``code_fix`` + a non-"none"
        # action means "we edited something; re-run the stage." Other
        # categories surface as `success=False`.
        if result.category == "code_fix" and not result.refused:
            return RecoveryResult(
                success=True,
                diagnosis=result.summary or result.action_taken,
            )
        return RecoveryResult(
            success=False,
            diagnosis=result.summary or "recovery agent surfaced no fix",
        )

    @staticmethod
    def _invocation_from_context(context: RecoveryContext) -> Invocation:
        """Translate P1-08's `RecoveryContext` into a P1-09 `Invocation`.

        Only deploy stages reach this method (the `el run` path uses
        `run_with_recovery` directly with an `ElRunInvocation`).
        """
        # ``config`` isn't carried on RecoveryContext today; the deploy
        # caller holds it. We import it on demand from the project to
        # keep this method a pure translator, but in practice the
        # orchestrator (which has the Config) calls
        # `run_recovery_agent` directly. Fallback: load from disk.
        from carve.core.config import load_config

        config = load_config(context.project_dir)

        if context.stage == RecoveryStage.PREFLIGHT:
            return DeployPreflightInvocation(
                pipeline_name=context.pipeline_name,
                source_target=context.source_target,
                dest_target=context.dest_target,
                project_dir=context.project_dir,
                config=config,
                failed_run_id="",
                error_text=context.error,
                ddl_path=context.ddl_path,
                drift=context.drift,
            )
        if context.stage == RecoveryStage.DDL_APPLY:
            return DeployDdlApplyInvocation(
                pipeline_name=context.pipeline_name,
                source_target=context.source_target,
                dest_target=context.dest_target,
                project_dir=context.project_dir,
                config=config,
                failed_run_id="",
                error_text=context.error,
                ddl_path=context.ddl_path,
                failing_statement_index=context.failing_statement_index,
                failing_sql=context.failing_sql,
            )
        # VERIFY
        return DeployVerifyInvocation(
            pipeline_name=context.pipeline_name,
            source_target=context.source_target,
            dest_target=context.dest_target,
            project_dir=context.project_dir,
            config=config,
            failed_run_id="",
            error_text=context.error,
            ddl_path=context.ddl_path,
        )


# Verify the class structurally satisfies the Protocol at import time —
# mypy will catch a drift but a runtime assertion is cheap insurance.
_protocol_check: RecoveryHandler = LLMRecoveryHandler.__new__(LLMRecoveryHandler)
del _protocol_check


__all__ = [
    "SUBMIT_DIAGNOSIS_SCHEMA",
    "LLMRecoveryHandler",
    "RecoveryAgentError",
    "RecoveryAttemptResult",
    "SubmitDiagnosisCapture",
    "build_tools_for_invocation",
    "load_recovery_agent_prompt",
    "make_read_run_logs_tool",
    "make_request_human_tool",
    "make_run_snowflake_ddl_tool",
    "make_submit_diagnosis_tool",
    "run_recovery_agent",
]
