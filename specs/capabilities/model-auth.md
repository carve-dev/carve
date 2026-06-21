# Model auth: provider credentials for the AI layer

> **How Carve authenticates to its model provider** — the credential subsystem the [harness](./harness.md) needs to run at all. Two paths: an **`ANTHROPIC_API_KEY`** env var, or a **Claude-subscription OAuth** flow via `carve auth login` (browser-based, token stored locally + auto-refreshed). Owns the credential **precedence**, the **OAuth flow + token storage/refresh**, `models.toml`'s `auth_mode` + model tiers, and the **OSS-vs-hosted split**. Distinct from API-token auth for REST/MCP (that's [rest-api](./rest-api.md)) and from warehouse/source credentials (that's [connect](./connect.md)/[sql](./sql.md)). *Build status:* only the **`ANTHROPIC_API_KEY` path is shipped** (the baseline the agent loop uses today, via `${ANTHROPIC_API_KEY}` indirection in `models.toml`). The **OAuth flow, the single precedence resolver, and `models.toml`'s `auth_mode` + model tiers are designed but unbuilt** — this spec is their durable design of record, *not* a description of shipped behavior. Sequencing lives in [DELIVERY](../DELIVERY.md).

## Status

- **Status:** Drafting (durable design — the `ANTHROPIC_API_KEY` path is shipped; the OAuth flow + precedence consolidation are designed-but-unbuilt)
- **Depends on:** [harness](./harness.md) (the consumer — the agent loop calls the provider with these credentials; the bash gate *scrubs* them from tool env), [layout](./layout.md) (`models.toml` lives in the config bundle).
- **Used by:** [harness](./harness.md) (every model call), [connect](./connect.md) (`carve auth login`, once built, is a first-moment onboarding step it can drive).
- **Lineage:** the M1.1-02 follow-up ([`../milestone-1.1-followups/02-claude-code-oauth.md`](../milestone-1.1-followups/02-claude-code-oauth.md)) **planned** the OAuth path (a `client_factory` module + an `auth_mode` field) behind an explicit investigation phase — but it was **never built**: only the `ANTHROPIC_API_KEY` path shipped, with the Anthropic client constructed directly at four call sites and no single precedence resolver. `ModelsConfig` today carries only `anthropic_api_key` + `default_model`. ARCHITECTURE §12.4 specifies the credential model. This capability spec is the durable design home that was missing — it describes the *target*, not the current code.

## Goal

Give the AI layer a credential it can use, acquired the way the user prefers, stored safely, refreshed automatically — and make the precedence + the OSS-vs-hosted difference explicit, so "how does Carve talk to the model" is owned rather than scattered.

## Out of scope

- **API tokens for the REST/MCP surface** — `.carve/token`, `carve auth token …` are [rest-api](./rest-api.md). (Naming overlap is real: *model* auth here vs. *API* auth there.)
- **Warehouse / source credentials** — `connections.toml`, `.dlt/secrets.toml`, `${ENV}`/file indirection are [connect](./connect.md) / [sql](./sql.md) / [layout](./layout.md).
- **Credential *scrubbing* from the bash tool env** — that's the [harness](./harness.md) permission gate (it strips `ANTHROPIC_*` so generated code never sees them); this spec *provides* the credential, the gate *withholds* it from tools.

## Behavior

### Two credential paths + precedence

1. **`ANTHROPIC_API_KEY`** (env var) — the simplest path; CI/headless default.
2. **Claude-subscription OAuth** — `carve auth login` opens a browser flow, exchanges for an OAuth token stored at `.carve/anthropic_oauth.json` (gitignored, mode 0600), **auto-refreshed** on expiry. Lets a user run Carve on their existing Claude subscription without minting an API key.

**Precedence** is explicit (resolved in one place): an explicit `auth_mode` in `models.toml` wins; else `ANTHROPIC_API_KEY` if present; else a stored OAuth token; else a clear "run `carve auth login` or set `ANTHROPIC_API_KEY`" error. (No silent ambiguity.)

### `models.toml`

The model configuration in the [config bundle](./layout.md): `auth_mode` (`api_key` | `oauth`), the install-default model, and per-tier overrides (the default that per-agent `model:` frontmatter — [extensibility](./extensibility.md) — falls back to). Agents pick a model; this resolves how that model is authenticated.

### OSS vs. hosted

In **OSS**, both paths are available; OAuth-from-the-user's-subscription is first-class. In the **hosted** product, OAuth-from-user-subscription is **not** offered (the platform supplies model access under its own billing) — an explicit seam, not an accident.

### `carve auth login` / status

`carve auth login` runs the OAuth flow; `carve auth status` shows the active mode + token validity (no secret values). The browser flow + local token file + refresh are this capability's; `carve auth token …` (REST API tokens) stays with [rest-api](./rest-api.md).

## Tests

- **Unit (precedence):** `auth_mode` in `models.toml` overrides env; env overrides stored OAuth; none → a clear, actionable error.
- **Unit (token storage):** the OAuth token writes to `.carve/anthropic_oauth.json` at mode 0600, gitignored; a near-expiry token auto-refreshes; a revoked token surfaces a re-login prompt.
- **Integration (login flow):** `carve auth login` completes the browser exchange (mocked) and a subsequent agent call authenticates via the stored token.
- **Unit (hosted split):** in hosted mode, OAuth-from-subscription is disabled and the platform credential path is used.

## Acceptance

- The AI layer authenticates via **either** `ANTHROPIC_API_KEY` **or** Claude-subscription OAuth, with a single, explicit precedence and a clear error when neither is present.
- `carve auth login` runs the OAuth browser flow, stores the token at `.carve/anthropic_oauth.json` (0600, gitignored), and auto-refreshes.
- `models.toml` carries `auth_mode` + the install-default model; the OSS-vs-hosted credential split is explicit.
- Model-provider auth is cleanly separated from REST/MCP API-token auth ([rest-api](./rest-api.md)) and warehouse/source creds ([connect](./connect.md)).

## Design notes

- **Why a dedicated capability (vs. a harness section)?** It's a distinct, security-sensitive subsystem — a browser OAuth flow, on-disk token at a specific mode, auto-refresh, precedence resolution, `models.toml`, and an OSS-vs-hosted policy — with its own CLI surface (`carve auth login`). The harness *consumes* the credential and *scrubs* it from tools; acquiring/storing/refreshing it is enough of its own concern (and was genuinely homeless) to spec separately. (If it ever shrinks, it could fold into harness — but today it's substantive and unowned.)
- **Why precedence is owned in one place.** Three credential sources (config, env, stored token) with a silent resolution order is a classic footgun; making it explicit + single-sourced is the point.

## Open questions

- **`carve auth login` exact command shape.** M1.1-02 *proposed* configuring auth via a `models.toml` `auth_mode` field and left the login command an open question; **neither was built** (`ModelsConfig` carries only `anthropic_api_key` + `default_model` today). This spec adopts `carve auth login` — to be confirmed against the OAuth flow when it is implemented.
- **Build status & sequencing.** Only the `ANTHROPIC_API_KEY` path is shipped. The OAuth browser flow, token storage/refresh (`.carve/anthropic_oauth.json`, 0600), the `auth_mode` + model-tiers schema, and the single precedence resolver — collapsing the four current `anthropic.Anthropic(api_key=…)` call sites into one `client_factory` — are **designed but unbuilt**. This is a net-new build (not a "formalize"), gated on the M1.1-02 investigation phase still being open (does `claude-agent-sdk` reuse an existing Claude Code session? what is the login command? does token-usage reporting diverge?). Sequencing lives in [DELIVERY](../DELIVERY.md); further model-provider work (e.g., Bedrock/Vertex proxies via `ANTHROPIC_BASE_URL`) is a later increment.
