"""The permission-mode lattice and the verb→mode map.

A `PermissionMode` is the harness's coarsest authority dial. The four
modes form a total order — ``read_only < plan < build < deploy`` — so
two modes can always be compared and a delegation can clamp to the
*narrower* of two with :func:`min_mode`.

The mode is set by the **verb** the user invoked (``ask``/``plan``/
``build``/``deploy``) or by a chat session's current mode. It is never
set by an agent file: an agent declares a *capability* mode (the widest
it is allowed to run at), and delegation runs the child at
``min(parent_mode, agent_capability)`` — see :func:`min_mode`.

The mode drives the permission policy (`policy.py`) and the
pre-execution gate (`gate.py`): write tools (`edit`/`create_file`/
`bash`-writes/warehouse-writes) are denied below ``build`` *regardless*
of any grant. That clamp is the authoritative boundary; this module
just supplies the ordering it relies on.
"""

from __future__ import annotations

from enum import StrEnum


class PermissionMode(StrEnum):
    """The four-rung authority lattice, ordered narrow→wide.

    ``StrEnum`` keeps the wire/config representation a plain lowercase
    string (``"read_only"`` etc.) while the ``_ORDER`` table below gives
    the rungs a total order for comparison and clamping.
    """

    READ_ONLY = "read_only"
    PLAN = "plan"
    BUILD = "build"
    DEPLOY = "deploy"


# Rank table — the single source of truth for the lattice order. Kept as
# a module-level dict (rather than relying on definition order) so the
# ordering is explicit and a reorder of the enum body can't silently
# change comparison results.
_ORDER: dict[PermissionMode, int] = {
    PermissionMode.READ_ONLY: 0,
    PermissionMode.PLAN: 1,
    PermissionMode.BUILD: 2,
    PermissionMode.DEPLOY: 3,
}


def rank(mode: PermissionMode) -> int:
    """Return the lattice rank of ``mode`` (0 = narrowest)."""
    return _ORDER[mode]


def min_mode(a: PermissionMode, b: PermissionMode) -> PermissionMode:
    """Return the narrower (lower-authority) of two modes.

    This is the delegation clamp: a child subagent runs at
    ``min_mode(parent_mode, agent_capability)`` and never wider. Because
    the lattice is a total order, ``min_mode`` is well-defined for any
    pair.
    """
    return a if _ORDER[a] <= _ORDER[b] else b


def mode_permits(mode: PermissionMode, required: PermissionMode) -> bool:
    """Return True iff ``mode`` is at least as wide as ``required``.

    e.g. ``mode_permits(DEPLOY, BUILD)`` is True; ``mode_permits(PLAN,
    BUILD)`` is False. Used by the gate to decide whether a write-tier
    action is in-mode.
    """
    return _ORDER[mode] >= _ORDER[required]


# The verb the user invokes selects the session's mode. ``ask`` is a
# pure read/answer verb (no writes), ``plan`` produces a design without
# applying it, ``build`` writes the project tree, ``deploy`` additionally
# touches the warehouse. This map is the *only* place a verb becomes a
# mode; nothing downstream re-derives it.
_VERB_TO_MODE: dict[str, PermissionMode] = {
    "ask": PermissionMode.READ_ONLY,
    "plan": PermissionMode.PLAN,
    "build": PermissionMode.BUILD,
    "deploy": PermissionMode.DEPLOY,
}


def mode_for_verb(verb: str) -> PermissionMode:
    """Map a CLI verb to its permission mode.

    Raises ``KeyError`` for an unknown verb — callers pass a known verb
    string; an unknown one is a programming error, not a runtime
    condition to paper over with a permissive default (fail closed).
    """
    return _VERB_TO_MODE[verb]


__all__ = [
    "PermissionMode",
    "min_mode",
    "mode_for_verb",
    "mode_permits",
    "rank",
]
