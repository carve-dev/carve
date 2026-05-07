# M3-04 — MCP client integration

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 1.5 days
**Dependencies:** M2-09 (skills registry)

## Purpose

Carve consumes external Model Context Protocol (MCP) servers, exposing their tools as namespaced skills available to agents. This is what plugs Carve into the broader AI tooling ecosystem rather than reinventing every integration.

## Why MCP

MCP is becoming the standard for tool/skill exchange across AI systems (Claude Desktop, Cursor, Claude Code, others). By consuming MCP servers, Carve gets:

- Snowflake's official MCP server (richer Snowflake operations)
- dbt-labs' MCP server (richer dbt operations than what we'd build)
- GitHub's MCP server (issue/PR operations)
- Custom MCP servers an organization has built for their internal tools

Carve also exposes its own functionality as an MCP server in v0.2 (deferred from v0.1).

## Configuration

`carve/mcp_servers.toml`:

```toml
[[server]]
name = "snowflake"
type = "stdio"
command = "snowflake-mcp"
args = ["--config", "/etc/snowflake-mcp/config.json"]
env = { SNOWFLAKE_ACCOUNT = "${SNOWFLAKE_ACCOUNT}" }

[[server]]
name = "dbt"
type = "stdio"
command = "uvx"
args = ["dbt-mcp"]

[[server]]
name = "github"
type = "stdio"
command = "github-mcp"
env = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }

[[server]]
name = "internal_tools"
type = "http"
url = "https://mcp.internal/api"
auth = { type = "bearer", token = "${INTERNAL_MCP_TOKEN}" }
```

Two transport types:

- **stdio**: spawns a subprocess that speaks MCP over stdin/stdout (most servers)
- **http**: connects to an HTTP MCP server (less common but supported)

## Implementation

`src/carve/core/mcp/client.py`:

```python
class MCPClient:
    def __init__(self, server_config: MCPServerConfig):
        self.config = server_config
        self.session = None
        self.tools_cache = None

    async def connect(self):
        if self.config.type == "stdio":
            self.session = await stdio_client(
                command=self.config.command,
                args=self.config.args,
                env={**os.environ, **self.config.env},
            )
        elif self.config.type == "http":
            self.session = await http_client(
                url=self.config.url,
                headers=self._auth_headers(),
            )
        await self.session.initialize()

    async def list_tools(self) -> list[MCPTool]:
        if self.tools_cache is None:
            response = await self.session.list_tools()
            self.tools_cache = response.tools
        return self.tools_cache

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        result = await self.session.call_tool(tool_name, arguments)
        return result.dict()

    async def close(self):
        if self.session:
            await self.session.close()
```

Use the official `mcp` Python SDK from Anthropic; don't reinvent the protocol.

## Tool registration

When Carve starts up:

1. Read `carve/mcp_servers.toml`
2. For each server, spawn the connection
3. Call `list_tools()` on each
4. Register each tool as a namespaced skill: `mcp:{server}:{tool_name}`

`src/carve/core/mcp/registry.py`:

```python
class MCPRegistry:
    def __init__(self, config: Config):
        self.config = config
        self.clients: dict[str, MCPClient] = {}

    async def initialize(self):
        for server_cfg in self.config.mcp_servers:
            client = MCPClient(server_cfg)
            await client.connect()
            self.clients[server_cfg.name] = client

    async def all_skills(self) -> list[SkillSchema]:
        skills = []
        for server_name, client in self.clients.items():
            tools = await client.list_tools()
            for tool in tools:
                skills.append(SkillSchema(
                    name=f"mcp:{server_name}:{tool.name}",
                    description=tool.description,
                    inputs=tool.inputSchema,
                    outputs={},  # MCP doesn't always provide output schema
                    impl=lambda ctx, **kwargs: client.call_tool(tool.name, kwargs),
                ))
        return skills
```

## Per-agent allowlists

Agents may not want every MCP tool exposed. Per-agent config:

```yaml
# carve/agents/dbt_agent.yaml
name: dbt
allowed_skills:
  - dbt_lookup_model
  - dbt_downstream_of
  - read_file
  - write_file
  - run_dbt_command
  - "mcp:dbt:*"           # all dbt MCP tools
  - "mcp:github:create_issue"  # specific tool
```

Glob support: `mcp:server:*` matches all tools from that server.

## Async handling in the agent loop

The agent loop is sync (M1-04). MCP is async. Bridge:

```python
def execute_skill(name: str, kwargs: dict, ctx: SkillContext):
    skill = registry.get(name)
    if skill.is_async:
        # Run in the loop's executor
        return asyncio.run_coroutine_threadsafe(
            skill.impl(ctx, **kwargs), event_loop
        ).result(timeout=skill.timeout)
    else:
        return skill.impl(ctx, **kwargs)
```

A long-lived event loop runs MCP I/O on a dedicated thread.

## Failure handling

- MCP server fails to start: log, continue without that server's skills, surface a warning to the user
- MCP tool call times out: return error to agent (so it can recover or pick another tool)
- MCP tool call fails: return the error message to the agent

Agents can recover gracefully because they see the error like any other tool failure.

## Surfacing in the UI

The settings tab in M3's agent studio (M3-09) shows MCP servers:

```
MCP Servers (3 connected, 1 failed)
├── snowflake (✓)        12 tools
├── dbt (✓)              8 tools
├── github (✓)           23 tools
└── internal_tools (✗)   Connection refused
   [Reconnect] [View logs]
```

## CLI commands

`src/carve/cli/commands/mcp.py`:

- `carve mcp list` — list configured servers and their status
- `carve mcp tools <server>` — list tools provided by a server
- `carve mcp test <server>` — connect, list tools, disconnect (sanity check)
- `carve mcp reload` — reload configuration without restarting the server

## Tests

- A mock MCP server's tools are registered as namespaced skills
- Per-agent allowlist filters correctly
- MCP failure on startup doesn't crash Carve
- Tool call routing works
- Glob matching in allowlists works

Use `mcp` SDK's test utilities or build a minimal in-process MCP server fixture.

## Acceptance criteria

- Carve consumes the official Snowflake MCP and dbt MCP servers in tests
- Tools appear as namespaced skills (`mcp:snowflake:foo`)
- Per-agent allowlists work
- Connection failures degrade gracefully
- The CLI commands work

## Files

- `src/carve/core/mcp/__init__.py`
- `src/carve/core/mcp/client.py`
- `src/carve/core/mcp/registry.py`
- `src/carve/core/mcp/schema.py`
- `src/carve/core/mcp/exceptions.py`
- `src/carve/cli/commands/mcp.py`
- `tests/core/mcp/test_client.py`
- `tests/core/mcp/test_registry.py`

## What this enables

- Carve plugs into the broader AI tooling ecosystem
- Users can leverage existing MCP servers without writing custom skills
- Carve gains capabilities (Snowflake-specific tools, dbt-specific operations) without us having to build them
