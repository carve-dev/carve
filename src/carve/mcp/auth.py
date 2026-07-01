"""Bearer-token discovery for ``carve mcp-serve``.

The MCP server is a REST client on behalf of whoever owns the token; it has no
identity model of its own (RBAC, scopes, and audit live on the REST side). This
module only *finds* the token, in the documented order, and never logs its value.
"""

from __future__ import annotations

import os
from pathlib import Path


class MCPAuthError(Exception):
    """No usable Carve API token could be discovered.

    The message is deliberately actionable and secret-free — it is safe to print
    to stderr and never contains a token value.
    """


#: Env var checked after ``--token`` and before the ``.carve/token`` file.
TOKEN_ENV_VAR = "CARVE_API_TOKEN"


def resolve_token(cli_token: str | None, *, token_path: Path) -> str:
    """Resolve the bearer token: ``--token`` → env → ``.carve/token`` → error.

    Args:
        cli_token: value of the ``--token`` flag (highest priority), or ``None``.
        token_path: the ``.carve/token`` file to fall back to (usually
            ``<project>/.carve/token``, minted by ``carve serve`` / ``auth rotate``).

    Returns:
        The resolved token string (never logged by this module).

    Raises:
        MCPAuthError: nothing usable was found, with a friendly next-step message.
    """
    if cli_token:
        return cli_token

    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        return env_token

    if token_path.is_file():
        file_token = token_path.read_text(encoding="utf-8").strip()
        if file_token:
            return file_token

    raise MCPAuthError(
        "No Carve API token found. Set CARVE_API_TOKEN, pass --token, or run "
        "`carve auth login` / `carve auth rotate` to mint one (written to .carve/token)."
    )


__all__ = ["TOKEN_ENV_VAR", "MCPAuthError", "resolve_token"]
