"""Recovery-agent invocation dataclasses (P1-09).

Each of the four trigger contexts the recovery agent serves gets its
own :class:`Invocation` subclass. The ``trigger`` enum tag lets the
orchestrator branch on context without isinstance-walking the union;
the dataclass fields carry the per-context details the prompt needs
(the failing run id, the failing DDL statement, the drift report,
etc.).

The agent module reads ``Invocation`` for two things:

1. To pick the right tool set — see
   ``carve.core.agents.recovery.agent.build_tools_for_invocation``.
2. To render the trigger-context preamble that gets stitched into the
   recovery agent's system prompt.

Construction is shallow on purpose: invocations are passed by value
and rebuilt per attempt. There is no inheritance from a common
abstract base; the union type alias :data:`Invocation` is sufficient
for the orchestrator's needs and keeps mypy happy without runtime
isinstance checks beyond the trigger tag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from carve.core.config import Config


class TriggerContext(StrEnum):
    """Which command surface invoked the recovery agent.

    The four contexts share most of their tool surface but differ in
    write authority and connection role:

    * ``EL_RUN_FAILURE`` — runtime role; can edit ``targets/<active>/el/<name>/``.
    * ``DEPLOY_PREFLIGHT`` — deploy role; read-only on the DDL file.
    * ``DEPLOY_DDL_APPLY`` — deploy role; writes DDL + can run DDL stmts.
    * ``DEPLOY_VERIFY`` — runtime role; same write authority as DDL apply.
    """

    EL_RUN_FAILURE = "el_run_failure"
    DEPLOY_PREFLIGHT = "deploy_preflight"
    DEPLOY_DDL_APPLY = "deploy_ddl_apply"
    DEPLOY_VERIFY = "deploy_verify"


@dataclass(frozen=True)
class ElRunInvocation:
    """``carve el run`` failed at runtime.

    The agent has runtime-role access (the same connection the script
    used). It can edit the script, requirements file, and companion
    DDL under ``targets/<active>/`` and re-run.
    """

    trigger: TriggerContext = field(
        default=TriggerContext.EL_RUN_FAILURE,
        init=False,
    )
    pipeline_name: str
    active_target: str
    project_dir: Path
    config: Config
    failed_run_id: str
    error_text: str


@dataclass(frozen=True)
class DeployPreflightInvocation:
    """``carve el deploy`` Phase 1 detected drift.

    Read-only context: the agent can inspect the DDL file but cannot
    edit it. The fix path is "edit the DDL file in the *source* target
    so a future build cycles drift back to zero" — but in Pillar 1 we
    surface the diagnosis and let the user act.
    """

    trigger: TriggerContext = field(
        default=TriggerContext.DEPLOY_PREFLIGHT,
        init=False,
    )
    pipeline_name: str
    source_target: str
    dest_target: str
    project_dir: Path
    config: Config
    failed_run_id: str
    error_text: str
    ddl_path: Path
    drift: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeployDdlApplyInvocation:
    """``carve el deploy`` Phase 2 — a DDL statement failed mid-apply.

    The agent runs under the deploy role, can edit the destination DDL
    file (``targets/<dest>/snowflake/<name>.sql``), and can re-run
    individual DDL statements via :func:`run_snowflake_ddl`.
    """

    trigger: TriggerContext = field(
        default=TriggerContext.DEPLOY_DDL_APPLY,
        init=False,
    )
    pipeline_name: str
    source_target: str
    dest_target: str
    project_dir: Path
    config: Config
    failed_run_id: str
    error_text: str
    ddl_path: Path
    failing_statement_index: int | None = None
    failing_sql: str | None = None


@dataclass(frozen=True)
class DeployVerifyInvocation:
    """``carve el deploy`` Phase 3 — post-DDL verify failed.

    Runtime role (matching what verify uses). The agent can append
    GRANT statements to the DDL file and trigger a re-apply.
    """

    trigger: TriggerContext = field(
        default=TriggerContext.DEPLOY_VERIFY,
        init=False,
    )
    pipeline_name: str
    source_target: str
    dest_target: str
    project_dir: Path
    config: Config
    failed_run_id: str
    error_text: str
    ddl_path: Path


# Discriminated union the orchestrator and agent share. Each branch
# carries its own field set; the ``trigger`` enum tag makes branch
# selection cheap without isinstance walks.
Invocation = (
    ElRunInvocation
    | DeployPreflightInvocation
    | DeployDdlApplyInvocation
    | DeployVerifyInvocation
)


__all__ = [
    "DeployDdlApplyInvocation",
    "DeployPreflightInvocation",
    "DeployVerifyInvocation",
    "ElRunInvocation",
    "Invocation",
    "TriggerContext",
]
