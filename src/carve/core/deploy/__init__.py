"""Deploy primitives for ``carve el deploy`` and ``carve el verify``.

The CLI commands compose these modules into the full deploy flow:

* `preflight` — read-only drift / connectivity checks against the deploy
  role before any writes.
* `copier` — file-tree promotion from the source target's working
  copy to the destination's, with a git-status guard against
  overwriting uncommitted edits.
* `ddl_applier` — parse and execute the destination's DDL file via the
  deploy role, statement by statement, tracking the failing index so
  recovery can retry from there.
* `verifier` — column / grant / smoke-test checks against the runtime
  role.

`recovery` defines the small Protocol seam P1-09 will plug into.
P1-08 ships a `NullRecoveryHandler` that always reports "unrecoverable"
so the deploy command runs end-to-end before the recovery agent lands.
"""

from __future__ import annotations

from carve.core.deploy.copier import (
    CopyResult,
    UncommittedChangesError,
    UnsafeArtifactError,
    copy_artifact,
    copy_ddl_file,
)
from carve.core.deploy.ddl_applier import (
    DdlApplyResult,
    DdlStatementFailure,
    UnsafeDdlError,
    apply_ddl,
    parse_ddl_statements,
    validate_ddl_statements,
)
from carve.core.deploy.identifiers import (
    InvalidSnowflakeIdentifierError,
    validate_identifier,
)
from carve.core.deploy.preflight import (
    PreflightDrift,
    PreflightResult,
    run_preflight,
)
from carve.core.deploy.recovery import (
    NullRecoveryHandler,
    RecoveryContext,
    RecoveryHandler,
    RecoveryResult,
    RecoveryStage,
)
from carve.core.deploy.verifier import (
    VerifyResult,
    run_verify,
)

__all__ = [
    "CopyResult",
    "DdlApplyResult",
    "DdlStatementFailure",
    "InvalidSnowflakeIdentifierError",
    "NullRecoveryHandler",
    "PreflightDrift",
    "PreflightResult",
    "RecoveryContext",
    "RecoveryHandler",
    "RecoveryResult",
    "RecoveryStage",
    "UncommittedChangesError",
    "UnsafeArtifactError",
    "UnsafeDdlError",
    "VerifyResult",
    "apply_ddl",
    "copy_artifact",
    "copy_ddl_file",
    "parse_ddl_statements",
    "run_preflight",
    "run_verify",
    "validate_ddl_statements",
    "validate_identifier",
]
