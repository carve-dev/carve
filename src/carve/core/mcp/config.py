"""Parse + edit ``carve/mcp.toml`` — the registered MCP servers.

The file format::

    [[server]]
    name = "jira"
    command = "mcp-jira --stdio"     # a stdio command, OR:
    # url = "https://mcp.example.com/sse"

    [[server.tools]]
    name = "search_issues"
    effects = ["read"]               # read-only → permitted from read_only up

    [[server.tools]]
    name = "create_issue"
    effects = ["write"]              # writer → build/deploy + prompt-tier
    # a tool with NO `effects` is fail-closed to writes=true.

A server is registered with either a stdio ``command`` or a remote
``url``. The ``[[server.tools]]`` entries are the **declared** tool
manifest the import (``client.py``) classifies; a live stdio handshake to
*discover* tools at runtime is a later increment — this slice imports from
the declared manifest so the gate classification is verifiable now.

Reads use stdlib :mod:`tomllib` (no code execution); edits use
:mod:`tomlkit` so comments / key order survive (matching the project's
``connections.toml`` discipline).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class McpConfigError(Exception):
    """Raised when ``mcp.toml`` is malformed or an edit is invalid."""


class McpToolDecl(BaseModel):
    """A declared tool on a server: a name + its effects tags."""

    model_config = ConfigDict(extra="forbid")

    name: str
    # `effects` is optional on purpose: its *absence* is the fail-closed
    # signal (the import treats a missing/empty effects list as writes=true).
    effects: list[str] = Field(default_factory=list)


class McpServer(BaseModel):
    """One registered MCP server (stdio command or remote URL)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    command: str | None = None
    url: str | None = None
    tools: list[McpToolDecl] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_has_no_colon(cls, value: str) -> str:
        # The server name becomes the middle segment of the
        # `mcp:<server>:<tool>` namespace. A `:` in it would corrupt that
        # namespacing (the gate splits on `:`), so a name like `a:b` could
        # masquerade as a different server/tool. Reject it at the boundary
        # — both `carve mcp-servers add` and any load path validate through
        # this model. An empty name is equally invalid.
        if not value.strip():
            raise ValueError("MCP server name must be a non-empty string.")
        if ":" in value:
            raise ValueError(
                f"MCP server name {value!r} must not contain ':' — the name "
                "is the middle segment of the mcp:<server>:<tool> namespace."
            )
        return value

    @model_validator(mode="after")
    def _require_endpoint(self) -> McpServer:
        if not self.command and not self.url:
            raise ValueError(f"MCP server {self.name!r} needs either a `command` or a `url`.")
        if self.command and self.url:
            raise ValueError(
                f"MCP server {self.name!r} has both `command` and `url`; set exactly one."
            )
        return self


class McpServersConfig(BaseModel):
    """The parsed ``mcp.toml`` — a list of registered servers."""

    model_config = ConfigDict(extra="forbid")

    server: list[McpServer] = Field(default_factory=list)

    def by_name(self, name: str) -> McpServer | None:
        for srv in self.server:
            if srv.name == name:
                return srv
        return None


def load_mcp_config(path: Path) -> McpServersConfig:
    """Load + validate ``mcp.toml`` at ``path``.

    A missing file means "no servers" (empty config). A malformed file or
    an invalid server entry raises :class:`McpConfigError`.
    """
    if not path.is_file():
        return McpServersConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise McpConfigError(f"Cannot read {path}: {exc}") from exc
    try:
        return McpServersConfig.model_validate(data)
    except ValidationError as exc:
        raise McpConfigError(f"Invalid mcp.toml at {path}: {exc}") from exc


def add_server(
    path: Path,
    *,
    name: str,
    command: str | None = None,
    url: str | None = None,
) -> None:
    """Add (or replace) a server entry in ``mcp.toml`` at ``path``.

    Validates the new entry through :class:`McpServer` before writing, so
    a bad add never lands. Preserves existing content via ``tomlkit``.
    """
    # Validate the candidate before touching disk (fail before write).
    try:
        McpServer(name=name, command=command, url=url)
    except ValidationError as exc:
        raise McpConfigError(str(exc)) from exc

    doc = _read_doc(path)
    servers = doc.get("server")
    if not isinstance(servers, list):
        servers = tomlkit.aot()
        doc["server"] = servers

    # Drop any existing entry with this name (replace semantics).
    kept = [s for s in servers if s.get("name") != name]
    servers.clear()
    for entry in kept:
        servers.append(entry)

    table = tomlkit.table()
    table["name"] = name
    if command is not None:
        table["command"] = command
    if url is not None:
        table["url"] = url
    servers.append(table)

    _write_doc(path, doc)


def remove_server(path: Path, *, name: str) -> bool:
    """Remove the server named ``name`` from ``mcp.toml``.

    Returns ``True`` if an entry was removed, ``False`` if none matched.
    """
    if not path.is_file():
        return False
    doc = _read_doc(path)
    servers = doc.get("server")
    if not isinstance(servers, list):
        return False
    kept = [s for s in servers if s.get("name") != name]
    removed = len(kept) != len(servers)
    servers.clear()
    for entry in kept:
        servers.append(entry)
    if removed:
        _write_doc(path, doc)
    return removed


def _read_doc(path: Path) -> tomlkit.TOMLDocument:
    if not path.is_file():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise McpConfigError(f"Cannot read {path}: {exc}") from exc


def _write_doc(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


__all__ = [
    "McpConfigError",
    "McpServer",
    "McpServersConfig",
    "McpToolDecl",
    "add_server",
    "load_mcp_config",
    "remove_server",
]
