# MCP server: auto-generated adapter over REST; stdio + WebSocket transports

> Ships Carve's MCP server as a thin adapter over the REST API from spec 09. Per [PRD §6.13 interfaces](../PRD.md), [ARCHITECTURE §8.3 MCP server](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 10](../PROJECT_PLAN.md). Implements the consumer side of [positioning #13 headless by default](../_strategy/2026-05-positioning.md) — every CLI action is reachable from Claude Desktop, Cursor, Claude Code, or any other MCP client.

## Status

- **Status:** Drafting
- **Depends on:** [rest-api](./rest-api.md) (the REST surface this spec adapts)
- **Blocks:** nothing structurally; MCP is consumer-facing
- **Soft depends on:** v0.1 user experience — once MCP lands, the user can drive Carve from chat tools, which closes the headless-by-default loop

## Goal

Generate an MCP server from the FastAPI REST surface with as little hand-written code as possible. Concretely:

1. **Auto-derivation** of MCP tool definitions from FastAPI's OpenAPI schema
2. **Thin adapter implementation** that translates each MCP `tool_use` to a REST request and the REST response to an MCP `tool_result`
3. **Two transports**: stdio (default; spawned as a subprocess by Claude Desktop / Cursor) and WebSocket (`carve mcp-serve --transport ws`)
4. **Authentication handoff** — the MCP server uses the same bearer token as the REST API
5. **`carve mcp-serve` CLI** with the right defaults for being spawned by an MCP client config
6. **Documentation** showing how to register Carve's MCP server in Claude Desktop, Cursor, and Claude Code

After this spec lands, a user who installs Carve and runs `claude` (or opens Claude Desktop with Carve in their config) can drive Carve's full plan/build/run/deploy lifecycle by chatting with the model.

## Out of scope

- The REST API itself (lives in spec 09; this spec is purely an adapter)
- Carve consuming *other* MCP servers as skills (lives in spec 04's `mcp:*` allowed_skills + the `mcp-servers` router from spec 09; this spec is about Carve being the *server*, not the client)
- MCP server in hosted with multi-tenant routing (hosted concern)
- Specific tool curation or per-tool prompt engineering — every REST endpoint becomes an MCP tool; the LLM picks
- A polished MCP-server UI for managing tool exposure (out for v0.1)

## Behavior

### MCP protocol overview

Carve implements the standard Anthropic MCP (Model Context Protocol) over JSON-RPC 2.0. The messages exchanged are:

- **`initialize`** (client → server): client announces protocol version + capabilities; server responds with its protocol version + capabilities (we declare `tools` capability; not `resources`, `prompts`, or `sampling` in v0.1)
- **`tools/list`** (client → server): client asks for the tool catalog; server returns the auto-generated list (one entry per REST endpoint)
- **`tools/call`** (client → server): client invokes a tool; server adapts to REST; response is the tool result
- **`notifications/*`** (server → client, optional): server-pushed events for long-running operations; v0.1 doesn't use these for tool calls (responses are synchronous), but the WebSocket transport keeps the channel open for future use

### Tool generation

`src/carve/mcp/tool_generator.py`:

```python
def generate_tools_from_openapi(openapi_schema: dict) -> list[MCPTool]:
    tools = []
    for path, methods in openapi_schema["paths"].items():
        for method, operation in methods.items():
            if _is_streaming_endpoint(path, method):
                continue                # WebSocket/SSE endpoints don't fit synchronous tool_use
            tool_name = _derive_tool_name(path, method)
            tools.append(MCPTool(
                name=tool_name,
                description=operation.get("description", operation.get("summary", "")),
                input_schema=_derive_input_schema(operation, openapi_schema),
            ))
    return tools
```

Tool naming convention (per ARCHITECTURE §8.3):

| REST method + path                              | MCP tool name                |
|-------------------------------------------------|------------------------------|
| `POST /api/v1/plans`                            | `plan_create`                |
| `POST /api/v1/plans/{id}/refine`                | `plan_refine`                |
| `GET  /api/v1/plans/{id}`                       | `plan_show`                  |
| `GET  /api/v1/plans`                            | `plans_list`                 |
| `POST /api/v1/builds`                           | `build_run`                  |
| `GET  /api/v1/builds/{id}`                      | `build_show`                 |
| `POST /api/v1/runs`                             | `run_pipeline`               |
| `POST /api/v1/runs/{run_id}/resume`             | `run_resume`                 |
| `POST /api/v1/asks`                             | `ask`                        |
| `GET  /api/v1/memory/{kind}`                    | `memory_show`                |
| `POST /api/v1/memory/decisions`                 | `memory_append_decision`     |
| `POST /api/v1/deploys`                          | `deploy_pipeline`            |
| (...and so on for every endpoint)               |                              |

The naming scheme: `<resource>_<verb>`, where verb is derived from the HTTP method + path shape (`POST /collection` → `_create`, `POST /collection/{id}/<action>` → `_<action>`, `GET /collection/{id}` → `_show`, `GET /collection` → `<resource>_list`, etc.). The generator includes a hand-overridable mapping for cases where the convention produces awkward names (e.g., `POST /api/v1/asks` → `ask` rather than `asks_create` because "ask" is the natural verb).

Input schema derivation:

- Path parameters become required string fields
- Query parameters become optional fields with the documented type
- Request body schemas (from `requestBody.content."application/json".schema`) become the body of the input schema
- All merge into a single MCP `input_schema` (JSON Schema object)

### Adapter

`src/carve/mcp/adapter.py`:

```python
class RESTAdapter:
    def __init__(self, *, base_url: str, token: str):
        self.client = httpx.AsyncClient(base_url=base_url, headers={"authorization": f"Bearer {token}"})
        self.routing_table = build_routing_table()    # tool_name → (method, path_template)

    async def call(self, tool_name: str, args: dict) -> dict:
        method, path_template = self.routing_table[tool_name]
        path = format_path(path_template, args)        # substitutes path params
        body = extract_body(args, method, path_template)
        query = extract_query(args, method, path_template)
        response = await self.client.request(method, path, params=query, json=body)
        if response.status_code >= 400:
            raise self._convert_error(response)
        return response.json()

    def _convert_error(self, response):
        """Convert problem+json error to MCP tool error."""
        problem = response.json()
        return MCPToolError(
            code=problem.get("type", "unknown"),
            message=problem.get("detail", problem.get("title", "Unknown error")),
            data=problem,
        )
```

The adapter is the *only* place that talks to the REST API. All other MCP server code is protocol-shape (tool listing, message routing, transport).

### Token discovery

`src/carve/mcp/auth.py`:

When the MCP server starts (via `carve mcp-serve`), it needs a bearer token. Resolution order:

1. `--token <token>` CLI flag (highest priority)
2. `CARVE_API_TOKEN` env var
3. `.carve/token` file (the default OSS token from spec 05)
4. Error: "No Carve API token found. Run `carve auth login` (OAuth) or `carve auth token mint` (API key)."

For Claude Desktop / Cursor / Claude Code integration, the user's MCP server config usually points at the local `.carve/token` via the env-var pattern:

```json
{
  "mcpServers": {
    "carve": {
      "command": "carve",
      "args": ["mcp-serve"],
      "env": {
        "CARVE_API_TOKEN": "${file:.carve/token}"
      }
    }
  }
}
```

### Transports

#### stdio (default)

`src/carve/mcp/transports/stdio.py`:

- Read JSON-RPC messages from stdin (one per line, or framed per MCP convention — TBD by MCP spec)
- Write responses to stdout
- All logging goes to stderr (so it doesn't corrupt the JSON-RPC stream)

The stdio transport is what Claude Desktop spawns: it runs `carve mcp-serve` as a subprocess and communicates over the pipes. This is the default because it's the most common deployment shape.

#### WebSocket

`src/carve/mcp/transports/websocket.py`:

- `carve mcp-serve --transport ws --port 8766`
- Listens for WebSocket connections; each connection is one MCP session
- Useful for remote agents (a server-side Carve install that an external agent talks to) or for local testing where stdio is awkward
- Default bind: `127.0.0.1:8766` with a warning on `0.0.0.0`

### `carve mcp-serve` CLI

```
carve mcp-serve [OPTIONS]

OPTIONS:
  --transport [stdio|ws]   Transport (default: stdio)
  --port INTEGER           Port for WebSocket (default: 8766; ignored for stdio)
  --host TEXT              Host for WebSocket (default: 127.0.0.1)
  --server-url TEXT        Carve REST API base URL (default: http://127.0.0.1:8765)
  --token TEXT             Override token (default: discovered per auth.py)
  --log-level TEXT         Log level (default: WARNING; INFO/DEBUG useful for setup debugging)
```

For stdio mode, the command runs until stdin closes (the subprocess parent exited). For WebSocket mode, it runs until SIGTERM.

### Tool listing on first connect

When a client sends `initialize` followed by `tools/list`, the server:

1. Fetches the OpenAPI schema from the Carve REST API at `<server_url>/api/openapi.json`
2. Generates MCP tool definitions via the tool_generator module
3. Returns the list

Schema fetch is cached for the lifetime of the MCP session — the OpenAPI schema only changes between Carve releases.

### Coverage

Every non-streaming REST endpoint becomes an MCP tool. Streaming endpoints (`GET /api/v1/runs/{id}/stream`) are excluded because MCP `tool_use`/`tool_result` is synchronous. Clients that want live streaming connect to the WebSocket/SSE endpoint directly via the REST API, not through MCP.

The CLI-REST parity test from spec 09 is extended here: every REST endpoint also has a corresponding MCP tool. The full-coverage integration test fails CI if a REST endpoint exists without a generated MCP tool.

### Hosted alternative

Per ARCHITECTURE §8.3, the hosted product offers a managed MCP endpoint at `wss://<tenant>.carve.dev/mcp` so agents don't need to spawn a local subprocess. That's a hosted-side concern; the OSS spec just ships the stdio + WebSocket transports here. Hosted reuses the adapter and tool generator without modification.

### Documentation

`docs/mcp-server.md` covers:

- What the MCP server does and why (the "drive Carve from chat" use case)
- Quickstart for each of the three flagship clients:
  - **Claude Desktop**: edit `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) / `%APPDATA%\Claude\claude_desktop_config.json` (Windows) to add the Carve MCP server entry; quit + reopen
  - **Cursor**: open Settings → MCP → Add new server; paste the JSON config; save
  - **Claude Code**: edit `~/.claude.json` or use `claude mcp add`; verify with `claude mcp list`
- How to verify the connection: in chat, ask "what Carve pipelines do I have?" and see the model invoke `pipelines_list`
- How to scope which agents can call which tools (Claude Desktop's MCP config supports tool allow-lists)

`docs/mcp-server-troubleshooting.md` covers:

- Token discovery failures
- Carve API not running (`carve serve` not started)
- Schema mismatch (Carve version skew between MCP server and REST server)
- Transport-specific debugging tips

## Tests

- **Unit (tool generation):** representative OpenAPI fragments produce expected MCP tool definitions; naming convention matches the table above
- **Unit (adapter):** `tool_use` with path params + query params + body produces the right HTTP request
- **Unit (adapter errors):** REST `problem+json` 4xx/5xx convert to structured MCP tool errors
- **Unit (protocol conformance):** `initialize`/`tools/list`/`tools/call` messages parse and round-trip per the MCP spec
- **Integration (stdio e2e):** spawn `carve mcp-serve` as a subprocess in a fixture; send `initialize` over stdin; receive expected response on stdout; send `tools/call` for `plans_list`; receive expected JSON-RPC response; close stdin; verify subprocess exits cleanly
- **Integration (WebSocket e2e):** start `carve mcp-serve --transport ws --port <random>`; connect via `websockets`; exercise the same flow as stdio
- **Integration (full coverage):** every endpoint in `/api/openapi.json` appears as an MCP tool (modulo streaming endpoints); test fails CI if a new REST endpoint is added without MCP coverage
- **Integration (auth):** missing `CARVE_API_TOKEN` → server exits with clear error message; invalid token → REST 401 surfaces as a structured MCP tool error
- **Integration (registration walkthrough):** the Claude Desktop / Cursor / Claude Code config snippets in the docs actually work against a freshly-installed Carve

## Acceptance

- `carve mcp-serve` running over stdio is discoverable by Claude Desktop, Cursor, and Claude Code via standard MCP config
- Every non-streaming REST endpoint is callable as an MCP tool
- Tool schemas mirror the corresponding REST request schemas
- An external agent driving Carve over MCP can complete the full plan → build → run → deploy loop without ever touching the CLI
- The adapter never has business logic — all Carve behavior happens via REST; the MCP layer is purely translation
- Token discovery follows the documented resolution order; failures produce friendly error messages
- The full-coverage parity test passes (every REST endpoint has an MCP tool; CI catches regressions)
- The docs walk-throughs for Claude Desktop, Cursor, and Claude Code each work end-to-end in under 5 minutes

## Design notes

- **Why auto-generate from OpenAPI rather than hand-write tool definitions?** Three reasons. (1) Every REST endpoint added in the future gets a free MCP tool — zero per-endpoint maintenance. (2) The descriptions, schemas, and types stay in sync with REST by construction; drift is structurally impossible. (3) The MCP layer becomes trivial to test (mostly translation logic), keeping the high-touch testing on REST where the business logic lives.
- **Why is the MCP server a separate process (`carve mcp-serve`) rather than baked into `carve serve`?** Because stdio MCP servers are spawned by their client (Claude Desktop, Cursor) on demand — they're not long-running services. The user's chat tool starts the process when they open a chat session and kills it when they close it. WebSocket mode is the long-running variant for users who want a persistent endpoint.
- **Why stdio as the default transport?** Because the dominant MCP clients (Claude Desktop, Cursor, Claude Code) spawn MCP servers as subprocesses by default. WebSocket is for advanced cases (remote MCP servers, programmatic clients). Defaulting to the common case keeps the docs simple.
- **Why doesn't the MCP server have its own auth/identity model?** Because identity is already established by the bearer token. The MCP server is conceptually a client of the REST API on behalf of whichever user owns the token. RBAC, scopes, audit log — all happen on the REST side. The MCP server adds nothing.
- **Why exclude streaming endpoints from the MCP tool surface rather than build a streaming-tool abstraction?** Because MCP `tool_use` is synchronous request/response. Streaming over MCP requires the `notifications/*` channel, which most clients don't yet expose well. Users who want streaming connect directly to the REST WebSocket — this is rare in practice for agent-driven workflows (agents typically poll status rather than subscribe to streams).
- **Why per-session schema cache rather than per-call?** Because the OpenAPI schema changes only between Carve releases; refetching it on every tool call would be wasteful. Per-session cache is the right granularity — a long-running WebSocket session might span hours, but a Carve release in the middle of it is rare enough that "restart the MCP session after upgrading Carve" is acceptable.

## Open questions

- **MCP protocol version pinning.** *Implementation default.* Pin to the latest stable MCP spec version at the time of `/build-spec` execution; declare it in the `initialize` response. Upgrade when the MCP spec releases a new version with non-breaking improvements.
- **Tool description quality.** *Implementation default.* The auto-generator uses the OpenAPI `description` field as the MCP tool description. The REST routers in spec 09 should write clear endpoint descriptions in their FastAPI route definitions; spec 09's reviewers should pay attention to description quality since it becomes LLM-visible context here.
- **Handling Carve version skew between MCP server and REST server.** *Implementation default.* MCP server fetches OpenAPI from REST; if schemas reflect different endpoints, the tool catalog reflects the REST side. If the MCP server is older than the REST API, new endpoints simply won't have tools — graceful degradation. If the MCP server is newer than REST, the test for "every REST endpoint has a tool" still passes because we're driven by what's in the REST OpenAPI. Both directions degrade safely.
- **Whether to support MCP `resources` capability for read-only resources like `pipelines/<name>.toml` contents.** *Implementation default.* No in v0.1; `tools/call` to `pipeline_show` returns the same content. Resources are a slightly different abstraction (LLM-discoverable, statically addressable) and aren't load-bearing for v0.1's use cases. Revisit if a client's UX would meaningfully improve with `resources` exposure.
