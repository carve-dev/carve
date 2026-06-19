"""MCP (consume) — register external servers; import their tools.

Carve *consumes* MCP servers here: ``carve mcp-servers add`` records a
server in ``carve/mcp.toml``, and :mod:`carve.core.mcp.client` imports its
tools into the registry as ``mcp:<server>:<tool>``, carrying each tool's
``effects`` metadata. The ``mcp:`` prefix is the namespace guarantee (it
can't collide with a base tool or a ``@skill`` name).

**Fail-closed default:** an imported tool with no/incomplete ``effects`` is
treated as ``writes=true`` and is registered into the permission policy as
a writer (denied in ``read_only``/``plan``, prompt-tier in
``build``/``deploy``). See :mod:`carve.core.agents.permissions.policy`
(``McpToolSpec`` / ``build_policy``) — that is where the classification is
*enforced*, inside the gate, not beside it.

Out of scope here: Carve *exposing* an MCP server (that is the separate
mcp-server spec).
"""

from carve.core.mcp.client import (
    ImportedMcpTool,
    McpImportError,
    import_server_tools,
    mcp_tool_specs,
)
from carve.core.mcp.config import (
    McpConfigError,
    McpServer,
    McpServersConfig,
    add_server,
    load_mcp_config,
    remove_server,
)

__all__ = [
    "ImportedMcpTool",
    "McpConfigError",
    "McpImportError",
    "McpServer",
    "McpServersConfig",
    "add_server",
    "import_server_tools",
    "load_mcp_config",
    "mcp_tool_specs",
    "remove_server",
]
