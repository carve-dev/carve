"""Exception type for configuration failures.

A single `ConfigError` carries enough context (file, field path, hint) for
the CLI to render the multi-line, actionable error format described in the
M1-02 spec. The CLI catches `ConfigError` and exits with code 2.
"""

from __future__ import annotations

from pathlib import Path


class ConfigError(Exception):
    """Raised for any user-facing configuration error.

    Attributes:
        message: One-line summary of the problem.
        file: Optional path to the file the error originated in.
        field: Optional dotted field path (e.g. ``connections.snowflake.dev.account``).
        hint: Optional remediation hint shown after the main message.
    """

    def __init__(
        self,
        message: str,
        *,
        file: Path | str | None = None,
        field: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.message = message
        self.file = Path(file) if file is not None else None
        self.field = field
        self.hint = hint
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [f"ConfigError: {self.message}"]
        if self.file is not None:
            lines.append(f"  File: {self.file}")
        if self.field is not None:
            lines.append(f"  Field: {self.field}")
        if self.hint is not None:
            lines.append(f"  Hint: {self.hint}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self._render()
