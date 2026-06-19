"""Mode→role selection for warehouse access — the role-scoped rule.

The warehouse is reachable only through the role-scoped ``sql`` tool
(spec 18 / Increment 2), never through ``bash`` — and which **role** that
tool connects with is decided by the operation and the current
:class:`PermissionMode`. This module ships that selection rule so the
``sql`` tool can reuse it; the rule generalizes the deploy-role-vs-
runtime-role pick already concrete in ``LLMRecoveryHandler`` (which
chose ``deploy_query_runner`` for preflight/DDL-apply and
``runtime_query_runner`` for verify).

The rule:

* **Reads** (SELECT/SHOW/DESCRIBE) run on the **read role** in every mode.
* **Writes / DDL** run on the **deploy/runtime role** — and are permitted
  **only at ``deploy``** (the gate denies warehouse writes below deploy,
  so this selector is only ever consulted for a write when the mode is
  already ``deploy``).

This slice ships the rule + the gate clamp; the dialect-aware ``sql``
tool that *uses* it lands in Increment 2. The Acceptance bullet
"warehouse reachable only via the role-scoped ``sql`` tool" is satisfied
negatively here (no bash path to the warehouse; creds scrubbed from the
bash env) and positively when 18 ships.
"""

from __future__ import annotations

from enum import StrEnum

from carve.core.agents.permissions.modes import PermissionMode, mode_permits


class WarehouseRole(StrEnum):
    """The two privilege envelopes warehouse access runs under.

    ``READ`` is the least-privilege role for SELECT/SHOW/DESCRIBE;
    ``DEPLOY`` is the elevated role that may run DDL/DML. (The shipped
    recovery handler's ``runtime`` role maps to ``READ`` for query
    purposes; the deploy role maps to ``DEPLOY``.)
    """

    READ = "read"
    DEPLOY = "deploy"


class WarehouseWriteDenied(Exception):
    """Raised when a warehouse write is requested below ``deploy`` mode.

    The gate denies warehouse-write *tools* below deploy already; this is
    the same rule applied at the role-selection layer so a caller that
    reaches the selector with a write + a non-deploy mode fails closed
    rather than silently downgrading to a read role.
    """


def role_for(*, mode: PermissionMode, is_write: bool) -> WarehouseRole:
    """Pick the warehouse role for an operation under ``mode``.

    Reads always use :attr:`WarehouseRole.READ`. Writes require ``deploy``
    (raising :class:`WarehouseWriteDenied` otherwise) and use
    :attr:`WarehouseRole.DEPLOY`.
    """
    if not is_write:
        return WarehouseRole.READ
    if not mode_permits(mode, PermissionMode.DEPLOY):
        raise WarehouseWriteDenied(
            f"Warehouse writes/DDL require deploy mode; got {mode}."
        )
    return WarehouseRole.DEPLOY


__all__ = ["WarehouseRole", "WarehouseWriteDenied", "role_for"]
