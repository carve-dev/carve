"""Exception types for the connectors layer.

`SnowflakeError` wraps everything raised by `snowflake-connector-python`
that we want callers to catch by intent rather than by driver-internal
class. The optional `hint` field lets `_format_error` attach actionable
remediation guidance for known error codes (002003, 002140, 001003, …).
"""

from __future__ import annotations


class SnowflakeError(Exception):
    """Wrapped Snowflake error with optional hint context.

    Attributes:
        message: Human-readable summary of the failure.
        hint: Optional remediation hint; populated by `_format_error`
            when the underlying driver error code is known.
        error_code: The Snowflake-driver error code (`errno`) when
            available, otherwise ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.message = message
        self.hint = hint
        self.error_code = error_code
        super().__init__(self._render())

    def _render(self) -> str:
        if self.hint:
            return f"{self.message}\n  Hint: {self.hint}"
        return self.message

    def __str__(self) -> str:
        return self._render()
