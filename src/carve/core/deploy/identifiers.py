"""Snowflake identifier validation for the deploy flow.

The Snowflake driver does not support identifier binding, so any place
``database`` / ``schema`` / ``table`` flows into a query happens via
f-string interpolation. The values originate in the plan's
``task_graph_json`` ``design`` block (LLM-emitted) or in the build's
``manifest_json`` ``destinations`` array — neither is trustworthy
without an explicit shape check.

We enforce the unquoted-identifier grammar
(`^[A-Za-z_][A-Za-z0-9_$]{0,254}$`):

* Starts with a letter or underscore.
* Followed by 0-254 letters / digits / underscores / dollar signs.
* Total length <= 255 (Snowflake's documented maximum).

This rejects every tested injection vector — embedded quotes, semicolons,
spaces, comment markers, dot-segments — without complicating the
deploy flow with quote-escaping.
"""

from __future__ import annotations

import re

# Matches the Snowflake unquoted-identifier grammar. We do not allow
# leading digits (Snowflake does, but the build/plan never emits them
# and rejecting them is one fewer corner case in downstream tools).
_SNOWFLAKE_UNQUOTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")


class InvalidSnowflakeIdentifierError(ValueError):
    """Raised when a SQL identifier fails the unquoted-grammar check."""


def validate_identifier(value: str, *, kind: str) -> str:
    """Return ``value`` if it's a safe Snowflake unquoted identifier.

    ``kind`` is interpolated into the error message — pass
    ``"database"`` / ``"schema"`` / ``"table"`` so the failure is
    actionable. The check applies at every site where an identifier
    crosses an interpolation boundary; calling it twice (once at the
    plan-edge, once at the SQL-edge) is intentional defense-in-depth.

    Raises:
        InvalidSnowflakeIdentifierError: If ``value`` fails the regex
            check or is not a string.
    """
    if not isinstance(value, str) or not _SNOWFLAKE_UNQUOTED.fullmatch(value):
        raise InvalidSnowflakeIdentifierError(
            f"Invalid {kind} identifier {value!r}: must match "
            f"{_SNOWFLAKE_UNQUOTED.pattern} (Snowflake unquoted identifier)."
        )
    return value


__all__ = [
    "InvalidSnowflakeIdentifierError",
    "validate_identifier",
]
