"""Recovery agent seam for the deploy flow.

P1-09 (the recovery agent) doesn't exist yet. This module defines the
narrow Protocol the deploy command uses to hand off failures, plus a
no-op default (`NullRecoveryHandler`) that always reports the failure
as unrecoverable.

The seam is deliberately small:

* `RecoveryStage` — three failure sites: preflight drift, DDL apply,
  smoke-verify.
* `RecoveryContext` — the structured failure summary that the deploy
  flow hands the recovery agent. P1-09 will read this; tests pass
  fakes that ignore most fields.
* `RecoveryHandler.attempt(context) -> RecoveryResult` — single entry
  point. Either the handler reports success (the deploy retries the
  failing stage from where it left off) or it surfaces a diagnosis
  and the deploy exits with that text.

P1-09's real handler will own its own budget, model selection, and
prompt construction. P1-08 just calls `attempt()` and acts on the
result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class RecoveryStage(StrEnum):
    """Where in the deploy flow the failure occurred."""

    PREFLIGHT = "preflight"
    DDL_APPLY = "ddl_apply"
    VERIFY = "verify"


@dataclass(frozen=True)
class RecoveryContext:
    """Structured failure handed to the recovery agent.

    Fields are populated per-stage; consumers should treat anything
    they don't need as best-effort metadata.

    **Write authority (handler contract):** Implementations MUST only
    write under
    ``project_dir / "targets" / dest_target / "snowflake" / <name>.sql``
    or ``project_dir / "targets" / dest_target / "el" / <name> /``.
    Writes outside this scope are a violation of the contract — even
    though P1-08 ships only the no-op handler and does not enforce
    the boundary, P1-09 will land a path-restricted writer that
    rejects out-of-scope writes. Documenting the constraint here
    means handlers added in the interim do not need rework.

    The deploy command re-reads ``ddl_path`` after a successful
    recovery, so the handler's primary mode is "edit the file in
    place; return ``success=True``".
    """

    stage: RecoveryStage
    pipeline_name: str
    source_target: str
    dest_target: str
    project_dir: Path
    ddl_path: Path
    error: str
    # Stage-specific extras. PREFLIGHT carries a list of drift items;
    # DDL_APPLY carries the failing statement index and the SQL text;
    # VERIFY carries the verifier's diagnosis. P1-09 reads what it
    # needs; P1-08 just plumbs it through.
    failing_statement_index: int | None = None
    failing_sql: str | None = None
    drift: tuple[str, ...] = field(default_factory=tuple)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of a recovery attempt.

    `success=True` means the handler made on-disk changes (or otherwise
    addressed the failure) and the deploy should retry the stage. The
    deploy re-reads the DDL file from disk before retry, so the handler
    can edit ``el/<name>/snowflake.sql`` freely — provided it stays
    within the write-authority scope documented on
    :class:`RecoveryContext`.

    `success=False` means the handler exhausted its budget or judged
    the failure unrecoverable. `diagnosis` is the user-facing summary
    the CLI prints and stamps on the `Run.error_message`.
    """

    success: bool
    diagnosis: str
    # When true, the next retry should restart the DDL apply from
    # statement 0 (e.g. because the handler re-ordered statements).
    # When false (the default), retry from `failing_statement_index`.
    retry_from_start: bool = False


class RecoveryHandler(Protocol):
    """The seam P1-09 implements; tests use a `_FakeRecoveryHandler`."""

    def attempt(self, context: RecoveryContext) -> RecoveryResult:
        """Try to recover from the failure described by ``context``.

        Implementations may make any number of internal LLM / tool
        calls; the deploy flow treats this as a single atomic
        recovery attempt and decides on the resulting `RecoveryResult`.
        """
        ...


class NullRecoveryHandler:
    """No-op handler used until P1-09 lands.

    Always reports the failure as unrecoverable. The deploy flow then
    surfaces the original error to the user. Production code (after
    P1-09) injects the real handler in its place.
    """

    def attempt(self, context: RecoveryContext) -> RecoveryResult:
        """Return a fixed unrecoverable result.

        ``context`` is unused — the handler has no logic to apply.
        """
        del context
        return RecoveryResult(
            success=False,
            diagnosis="recovery agent not enabled",
        )


__all__ = [
    "NullRecoveryHandler",
    "RecoveryContext",
    "RecoveryHandler",
    "RecoveryResult",
    "RecoveryStage",
]
