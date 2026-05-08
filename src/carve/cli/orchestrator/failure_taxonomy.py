"""Cheap regex-based failure classifier (P1-09).

The recovery loop calls :func:`classify_failure` on each fresh failure
**before** it ever invokes the LLM. The classifier is deterministic,
side-effect-free, and operates on the failure's text — typically a
Snowflake driver error string, a Python traceback, or a verifier
diagnosis. Its job is two-part:

1. Decide whether the failure is even *worth* an LLM attempt. Auth,
   permission, resource-exhaustion, and user-cancel categories are
   *do-not-fix*: more attempts won't help. The loop bails with a
   ``Refused`` outcome for those.
2. Tag fixable failures so the agent's prompt and tool set can be
   selected with the failure's shape in mind. ``code_fix`` is the
   default for "looks like a bug we can patch."

The patterns are intentionally case-insensitive substring/regex
matches against the error text. False positives matter less than
false negatives — calling a fixable failure ``code_fix`` and letting
the agent surface "GRANT … needed" via diagnosis is fine; calling an
auth failure ``code_fix`` and burning attempts with the LLM is not.
Order of pattern tests matters: more-specific (auth, permission, etc.)
runs before the catch-all ``code_fix``.

The do-not-auto-fix set is exposed as :data:`DO_NOT_AUTO_FIX` so the
recovery loop can branch on it without re-checking strings.
"""

from __future__ import annotations

import re
from enum import StrEnum


class FailureCategory(StrEnum):
    """Coarse buckets the recovery loop branches on."""

    CODE_FIX = "code_fix"
    AUTH = "auth"
    PERMISSION = "permission"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    USER_CANCEL = "user_cancel"
    REPEATED_IDENTICAL = "repeated_identical"
    OUT_OF_SCOPE = "out_of_scope"


# The categories the loop refuses to send to the LLM. Each requires
# user action (rotate creds, grant a privilege, restart Snowflake's
# warehouse, etc.) that the Pillar 1 recovery agent has no authority
# to take.
DO_NOT_AUTO_FIX: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.AUTH,
        FailureCategory.PERMISSION,
        FailureCategory.RESOURCE_EXHAUSTION,
        FailureCategory.USER_CANCEL,
        FailureCategory.REPEATED_IDENTICAL,
        FailureCategory.OUT_OF_SCOPE,
    }
)


# Patterns are intentionally simple — substring matching at compile time
# against a lower-cased copy of the error text. A failure of the
# pattern (i.e. the error doesn't match anything specific) defaults to
# ``code_fix`` because that's the case where the LLM has the best
# chance of helping.
_AUTH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bauthentication failed\b", re.IGNORECASE),
    re.compile(r"\binvalid oauth token\b", re.IGNORECASE),
    re.compile(r"\binvalid credential", re.IGNORECASE),
    re.compile(r"\bincorrect username or password\b", re.IGNORECASE),
    re.compile(r"\b401\s+unauthor", re.IGNORECASE),
    re.compile(r"\bsignature verification failed\b", re.IGNORECASE),
)

_PERMISSION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\binsufficient privileges?\b", re.IGNORECASE),
    re.compile(r"\bsql access control error\b", re.IGNORECASE),
    re.compile(r"\baccess denied\b", re.IGNORECASE),
    re.compile(r"\bnot authorized\b", re.IGNORECASE),
    re.compile(r"\b403\s+forbidden\b", re.IGNORECASE),
    re.compile(
        r"does not have privileges\s+(on|to)\b",
        re.IGNORECASE,
    ),
)

_RESOURCE_EXHAUSTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bout of memory\b", re.IGNORECASE),
    re.compile(r"\bwarehouse .* (suspended|stopped|not running)\b", re.IGNORECASE),
    re.compile(r"\baccount .* locked\b", re.IGNORECASE),
    re.compile(r"\bnetwork (is )?unreachable\b", re.IGNORECASE),
    re.compile(r"\bconnection refused\b", re.IGNORECASE),
    re.compile(r"\bquota exceeded\b", re.IGNORECASE),
    re.compile(r"\bservice unavailable\b", re.IGNORECASE),
    # Numeric HTTP status alone is too noisy (e.g. "503 rows scanned");
    # require an accompanying status phrase.
    re.compile(
        r"\b50[234]\b\s+(bad gateway|service unavailable|gateway timeout)\b",
        re.IGNORECASE,
    ),
)

_USER_CANCEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bkeyboardinterrupt\b", re.IGNORECASE),
    re.compile(r"\binterrupted by user\b", re.IGNORECASE),
    re.compile(r"\bcancelled by user\b", re.IGNORECASE),
    re.compile(r"\bsigint\b", re.IGNORECASE),
)

_OUT_OF_SCOPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bout of scope\b", re.IGNORECASE),
    re.compile(r"\bnot supported by this agent\b", re.IGNORECASE),
)


def classify_failure(error_text: str | None) -> FailureCategory:
    """Bucket ``error_text`` into a :class:`FailureCategory`.

    Empty / ``None`` input returns ``CODE_FIX`` — when there's no text
    to inspect, the recovery loop's best chance is to let the LLM look
    at the run logs directly. Order of checks matters: more specific
    do-not-fix categories shadow the generic ``CODE_FIX`` default.
    """
    if not error_text:
        return FailureCategory.CODE_FIX

    if _any_match(_USER_CANCEL_PATTERNS, error_text):
        return FailureCategory.USER_CANCEL
    if _any_match(_AUTH_PATTERNS, error_text):
        return FailureCategory.AUTH
    if _any_match(_PERMISSION_PATTERNS, error_text):
        return FailureCategory.PERMISSION
    if _any_match(_RESOURCE_EXHAUSTION_PATTERNS, error_text):
        return FailureCategory.RESOURCE_EXHAUSTION
    if _any_match(_OUT_OF_SCOPE_PATTERNS, error_text):
        return FailureCategory.OUT_OF_SCOPE
    return FailureCategory.CODE_FIX


def _any_match(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(p.search(text) for p in patterns)


__all__ = [
    "DO_NOT_AUTO_FIX",
    "FailureCategory",
    "classify_failure",
]
