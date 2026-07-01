# Troubleshooting Carve's MCP server

`carve mcp-serve` is a thin adapter over Carve's REST API. Almost every failure
is one of four things: no token, no REST API, a version mismatch, or a transport
misconfiguration. Run with `--log-level debug` (logs go to **stderr**, never
stdout) to see what is happening.

## "No Carve API token found"

The server could not resolve a bearer token. It looks in this order:

1. `--token <token>`
2. `CARVE_API_TOKEN` environment variable
3. `.carve/token` file (relative to the process working directory)

Fixes:

- Make sure `carve serve` has run at least once (it bootstraps `.carve/token`),
  or mint one with `carve auth rotate`.
- In a client config, set the token under `env.CARVE_API_TOKEN`. Note that the
  working directory of a client-spawned subprocess may not be your project root,
  so the `.carve/token` fallback can miss — prefer the env var or `--token`.
- The token is a **secret**: Carve never logs it, and neither should your client
  config in shared repos.

## "Could not reach the Carve REST API"

The server fetches `<server-url>/api/openapi.json` at startup. If the REST API is
not running (or is on a different host/port), startup fails.

Fixes:

- Start the API: `carve serve` (default `http://127.0.0.1:8765`).
- If the API is elsewhere, pass `--server-url http://host:port`.
- Confirm reachability: `curl http://127.0.0.1:8765/healthz` should return
  `{"status": "ok"}`.

## A tool call returns an error

Tool errors are surfaced structurally: the error text is the REST
`problem+json` `detail`/`title`, and the full payload is attached as structured
content. Common cases:

- **401 Unauthorized** — the token is wrong or revoked. Re-check token discovery
  above; mint a fresh one with `carve auth rotate`.
- **404 Not Found** — the resource (plan, run, pipeline) doesn't exist. Ask the
  model to list first (`plans_list`, `runs_list`, `pipelines_list`).
- **409 Conflict** — e.g. resuming a run that isn't in a failed/crashed state.

Because the MCP layer is pure translation, the *behavior* lives in REST — the
same call over `curl` against `carve serve` reproduces the error identically.

## A tool you expect is missing

The tool catalog is generated from the **live** REST OpenAPI schema, so:

- If the MCP server is **older** than the REST API, brand-new endpoints won't have
  tools yet — restart `carve mcp-serve` (and upgrade Carve) to pick them up.
- Streaming endpoints are intentionally excluded (`GET /runs/{id}/stream`); MCP
  `tools/call` is synchronous.
- `deploy_pipeline` is not yet available (the REST deploy write-surface lands in a
  later increment); the read side (`deploys_list`) is present.

## Transport issues

- **stdio (default):** the client spawns `carve mcp-serve` and talks over
  stdin/stdout. If the handshake fails, ensure nothing in your shell profile
  prints to stdout when `carve` starts — in stdio mode **stdout must be pure
  JSON-RPC**. Check the client's MCP logs and run the command by hand to inspect
  stderr.
- **http:** `carve mcp-serve --transport http --port 8766` serves at
  `http://127.0.0.1:8766/mcp`. Binding `--host 0.0.0.0` exposes it on all
  interfaces (you'll get a warning) — only do that behind a trusted network.
- `--transport ws` is a deprecated alias for `http` (MCP standardized on
  Streamable HTTP, not WebSocket); it prints a one-line notice and uses `http`.

## Carve version skew

The MCP server and the REST API should be the same Carve version. The tool
catalog always follows the REST OpenAPI, so a mismatch degrades gracefully (an
older server just lacks the newest tools). After upgrading Carve, restart both
`carve serve` and any long-lived `carve mcp-serve --transport http` process.
