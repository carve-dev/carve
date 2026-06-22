"""Import an MCP server's tools into the namespaced, effects-tagged registry.

Each imported tool becomes ``mcp:<server>:<tool>`` (the ``mcp:`` prefix is
the namespace guarantee — it can't collide with a base tool / ``@skill``
name; the loop's flat-namespace guard still resolves a base-tool-vs-pack
clash to the base tool). The tool's ``effects`` metadata is derived into a
single ``writes`` boolean and surfaced as a
:class:`carve.core.agents.permissions.policy.McpToolSpec` the
policy-registration path consumes.

**Fail-closed default (the security property):** a tool with **no or
incomplete** ``effects`` is treated as ``writes=true``. "Read-only" means
*every* effect tag is a known read-only tag (``read`` / ``readonly`` /
``list`` / ``get`` / ``search`` / ``query``); a single unknown or
write-shaped tag — or an empty list — flips it to a writer. So a sloppy or
malicious server defaults to the safe side: its tools are denied in
``read_only``/``plan`` and prompt-tier in ``build``/``deploy`` until it
*proves* read-only.

This slice imports from a server's **declared** tool manifest
(``[[server.tools]]`` in ``mcp.toml``). A live stdio handshake to discover
tools at runtime is a later increment; the classification — the part the
gate enforces — is identical regardless of how the manifest arrives.
"""

from __future__ import annotations

from dataclasses import dataclass

from carve.core.agents.permissions.policy import McpToolSpec
from carve.core.mcp.config import McpServer, McpToolDecl

# Effect tags that mean "no write". The classification is an allowlist (a
# denylist would lag the next write-shaped verb a server invents): a tool is
# read-only ONLY if it declares at least one effect and EVERY declared
# effect is in this set. Empty effects ⇒ not read-only ⇒ fail-closed writer.
_READ_ONLY_EFFECTS: frozenset[str] = frozenset(
    {"read", "readonly", "read_only", "list", "get", "search", "query"}
)


class McpImportError(Exception):
    """Raised when a server's tools cannot be imported."""


@dataclass(frozen=True)
class ImportedMcpTool:
    """One imported MCP tool: its namespaced name + write classification.

    * ``name`` — ``mcp:<server>:<tool>``.
    * ``server`` / ``tool`` — the components, for display.
    * ``effects`` — the raw declared effect tags (for ``carve skills show``).
    * ``writes`` — the fail-closed derivation (see module docstring).
    """

    name: str
    server: str
    tool: str
    effects: tuple[str, ...]
    writes: bool

    def to_spec(self) -> McpToolSpec:
        """Project onto the policy's :class:`McpToolSpec`."""
        return McpToolSpec(name=self.name, writes=self.writes)


def _derive_writes(effects: list[str]) -> bool:
    """Fail-closed: ``True`` unless every declared effect is read-only.

    An empty/missing effects list ⇒ ``True`` (a writer). A list with any
    tag outside :data:`_READ_ONLY_EFFECTS` ⇒ ``True``.
    """
    if not effects:
        return True
    normalized = {e.strip().lower() for e in effects}
    return not normalized.issubset(_READ_ONLY_EFFECTS)


def _import_tool(server_name: str, decl: McpToolDecl) -> ImportedMcpTool:
    name = f"mcp:{server_name}:{decl.name}"
    return ImportedMcpTool(
        name=name,
        server=server_name,
        tool=decl.name,
        effects=tuple(decl.effects),
        writes=_derive_writes(decl.effects),
    )


def import_server_tools(server: McpServer) -> list[ImportedMcpTool]:
    """Import every declared tool on ``server`` as a namespaced, tagged tool.

    Duplicate tool names within one server are an :class:`McpImportError`
    (the ``mcp:<server>:<tool>`` name would collide). The list preserves
    declaration order.
    """
    seen: set[str] = set()
    imported: list[ImportedMcpTool] = []
    for decl in server.tools:
        if decl.name in seen:
            raise McpImportError(f"Server {server.name!r} declares tool {decl.name!r} twice.")
        seen.add(decl.name)
        imported.append(_import_tool(server.name, decl))
    return imported


def mcp_tool_specs(servers: list[McpServer]) -> frozenset[McpToolSpec]:
    """Import all servers' tools and project them to policy ``McpToolSpec``s.

    The convenience the policy-registration path consumes: hand this set to
    ``build_policy(..., mcp_tools=...)`` and the gate classifies each tool
    (read-only ⇒ permitted from ``read_only``; writer/missing-effects ⇒
    build/deploy + prompt).
    """
    specs: set[McpToolSpec] = set()
    for server in servers:
        for imported in import_server_tools(server):
            specs.add(imported.to_spec())
    return frozenset(specs)


__all__ = [
    "ImportedMcpTool",
    "McpImportError",
    "import_server_tools",
    "mcp_tool_specs",
]
