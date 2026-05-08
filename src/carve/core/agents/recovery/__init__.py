"""Recovery-agent package (P1-09).

Public surface:

* :class:`Invocation` (and its four concrete dataclasses) — the
  trigger-context discriminator the orchestrator hands the agent.
* :func:`run_recovery_agent` — runs one bounded attempt through
  ``AgentLoop``.
* :class:`LLMRecoveryHandler` — drop-in `RecoveryHandler` for the
  P1-08 deploy seam.
* :class:`RecoveryAttemptResult` / :class:`RecoveryAgentError` — return
  / failure types.
"""

from __future__ import annotations

from carve.core.agents.recovery.agent import (
    SUBMIT_DIAGNOSIS_SCHEMA,
    LLMRecoveryHandler,
    RecoveryAgentError,
    RecoveryAttemptResult,
    SubmitDiagnosisCapture,
    build_tools_for_invocation,
    load_recovery_agent_prompt,
    make_read_run_logs_tool,
    make_request_human_tool,
    make_run_snowflake_ddl_tool,
    make_submit_diagnosis_tool,
    run_recovery_agent,
)
from carve.core.agents.recovery.invocation import (
    DeployDdlApplyInvocation,
    DeployPreflightInvocation,
    DeployVerifyInvocation,
    ElRunInvocation,
    Invocation,
    TriggerContext,
)

__all__ = [
    "SUBMIT_DIAGNOSIS_SCHEMA",
    "DeployDdlApplyInvocation",
    "DeployPreflightInvocation",
    "DeployVerifyInvocation",
    "ElRunInvocation",
    "Invocation",
    "LLMRecoveryHandler",
    "RecoveryAgentError",
    "RecoveryAttemptResult",
    "SubmitDiagnosisCapture",
    "TriggerContext",
    "build_tools_for_invocation",
    "load_recovery_agent_prompt",
    "make_read_run_logs_tool",
    "make_request_human_tool",
    "make_run_snowflake_ddl_tool",
    "make_submit_diagnosis_tool",
    "run_recovery_agent",
]
