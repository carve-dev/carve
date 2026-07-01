# Carve's MCP server — drive Carve from chat

Carve ships an [MCP](https://modelcontextprotocol.io) server so you can drive
your data platform by chatting with an AI client — Claude Desktop, Cursor, Claude
Code, or any other MCP client. Ask *"what pipelines do I have?"* or *"plan a
pipeline that loads Stripe into the warehouse, then build it"* and the model
calls Carve's tools for you.

The MCP server is a **thin adapter over Carve's REST API**. Every non-streaming
REST endpoint becomes an MCP tool automatically — so the tool catalog always
mirrors the REST surface, and new Carve features show up as new tools with no
extra work. It has no logic of its own: it translates each tool call into an
HTTP request against the API that `carve serve` exposes, and translates the
response back.

```
┌──────────────┐   MCP (stdio/http)   ┌──────────────┐   HTTP    ┌────────────┐
│ Claude / IDE │ ───────────────────► │ carve        │ ────────► │ carve      │
│ (MCP client) │ ◄─────────────────── │ mcp-serve    │ ◄──────── │ serve (REST)│
└──────────────┘                      └──────────────┘           └────────────┘
```

## Prerequisites

1. **Carve's REST API is running.** Start it with `carve serve` (defaults to
   `http://127.0.0.1:8765`). The MCP server fetches the OpenAPI schema from
   `<server-url>/api/openapi.json` at startup to generate its tools.
2. **A Carve API token.** `carve serve` bootstraps one to `.carve/token` on first
   run; you can also mint/rotate one with `carve auth rotate`. The MCP server
   resolves the token in this order:
   1. `--token <token>` flag
   2. `CARVE_API_TOKEN` environment variable
   3. `.carve/token` file (relative to the working directory)

## The command

```
carve mcp-serve [OPTIONS]

  --transport [stdio|http]  Transport (default: stdio). 'ws' is a deprecated
                            alias for 'http' (MCP uses Streamable HTTP).
  --port INTEGER            Port for the http transport (default: 8766).
  --host TEXT               Host for the http transport (default: 127.0.0.1).
  --server-url TEXT         Carve REST API base URL (default: http://127.0.0.1:8765).
  --token TEXT              Bearer token override (default: discovered, see above).
  --log-level TEXT          Log level to stderr (default: WARNING).
```

- **stdio** (the default) is what Claude Desktop / Cursor / Claude Code spawn as a
  subprocess. In stdio mode `stdout` carries the JSON-RPC protocol stream — all
  logging goes to `stderr`, never `stdout`.
- **http** runs a long-lived [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
  endpoint at `http://<host>:<port>/mcp` for remote agents or programmatic
  clients. (MCP never adopted raw WebSocket; `--transport ws` is accepted as a
  deprecated alias that maps to `http`.)

## Register with an MCP client

### Claude Desktop

Edit the config file and add Carve under `mcpServers`, then quit and reopen
Claude Desktop:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "carve": {
      "command": "carve",
      "args": ["mcp-serve"],
      "env": {
        "CARVE_API_TOKEN": "your-token-here"
      }
    }
  }
}
```

### Cursor

Open **Settings → MCP → Add new server**, and paste the same JSON (or point Cursor
at your `claude_desktop_config.json`). Save; Cursor spawns `carve mcp-serve` on
demand.

### Claude Code

Add the server from the CLI, then verify:

```bash
claude mcp add carve -- carve mcp-serve
# or edit ~/.claude.json directly with the JSON above
claude mcp list          # 'carve' should appear
```

Set `CARVE_API_TOKEN` in the environment Claude Code runs in, or pass
`--token` in the `args`.

## Verify it works

In your chat client, ask:

> what Carve pipelines do I have?

The model should call the `pipelines_list` tool and report your pipelines. From
there you can drive the full lifecycle — `plan_create` → `build_run` →
`run_pipeline` — without touching the CLI.

## Tool naming

Tools are named `<resource>_<verb>`, derived from the REST method and path:

| REST                                   | MCP tool                 |
|----------------------------------------|--------------------------|
| `GET  /api/v1/plans`                   | `plans_list`             |
| `POST /api/v1/plans`                   | `plan_create`            |
| `GET  /api/v1/plans/{plan_id}`         | `plan_show`              |
| `POST /api/v1/builds`                  | `build_run`              |
| `POST /api/v1/runs`                    | `run_pipeline`           |
| `POST /api/v1/runs/{run_id}/resume`    | `run_resume`             |
| `POST /api/v1/memory/decisions`        | `memory_append_decision` |

Streaming endpoints (the run event stream, `GET /api/v1/runs/{run_id}/stream`)
are **not** exposed as tools — MCP `tools/call` is synchronous request/response.
Connect to the REST stream directly if you need live events.

See [troubleshooting](./mcp-server-troubleshooting.md) if a client can't connect.
