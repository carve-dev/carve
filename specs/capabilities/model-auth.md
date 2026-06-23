# Model auth: provider credentials for the AI layer

> **How Carve authenticates to its model provider** — the credential subsystem the [harness](./harness.md) needs to run at all. Two paths: an **`ANTHROPIC_API_KEY`** env var, or a **Claude-subscription OAuth** flow via `carve auth login` (browser-based, token stored locally + auto-refreshed). Owns the credential **precedence**, the **OAuth flow + token storage/refresh**, `models.toml`'s `auth_mode` + model tiers, and the **OSS-vs-hosted split**. Distinct from API-token auth for REST/MCP (that's [rest-api](./rest-api.md)) and from warehouse/source credentials (that's [connect](./connect.md)/[sql](./sql.md)). Carve owns **no** browser OAuth flow or token store — it leans on the `anthropic` SDK + `claude setup-token` for the subscription-OAuth path.

## Status

- **Status:** Drafting
- **Depends on:** [harness](./harness.md) (the consumer — the agent loop calls the provider with these credentials; the bash gate *scrubs* them from tool env), [layout](./layout.md) (`models.toml` lives in the config bundle).
- **Used by:** [harness](./harness.md) (every model call), [connect](./connect.md) (`carve auth login` is a first-moment onboarding step it can drive).
- **Lineage:** ARCHITECTURE §12.4 specifies the credential model; this capability spec is its durable design home. The M1.1-02 follow-up ([`../milestone-1.1-followups/02-claude-code-oauth.md`](../milestone-1.1-followups/02-claude-code-oauth.md)) is the design ancestor of the OAuth path.

## Goal

Give the AI layer a credential it can use, acquired the way the user prefers, stored safely, refreshed automatically — and make the precedence + the OSS-vs-hosted difference explicit, so "how does Carve talk to the model" is owned rather than scattered.

## Out of scope

- **API tokens for the REST/MCP surface** — `.carve/token`, `carve auth token …` are [rest-api](./rest-api.md). (Naming overlap is real: *model* auth here vs. *API* auth there.)
- **Warehouse / source credentials** — `connections.toml`, `.dlt/secrets.toml`, `${ENV}`/file indirection are [connect](./connect.md) / [sql](./sql.md) / [layout](./layout.md).
- **Credential *scrubbing* from the bash tool env** — that's the [harness](./harness.md) permission gate (it strips `ANTHROPIC_*` so generated code never sees them); this spec *provides* the credential, the gate *withholds* it from tools.

## Behavior

### Two credential paths + precedence

1. **`ANTHROPIC_API_KEY`** (env var) — the simplest path; CI/headless default. The client is built with `anthropic.Anthropic(api_key=…)`.
2. **Claude-subscription OAuth** — a Claude Pro/Max/Team subscription **OAuth token**, supplied via `ANTHROPIC_AUTH_TOKEN` (or `CLAUDE_CODE_OAUTH_TOKEN`). The client is built with `anthropic.Anthropic(auth_token=…)` **plus** the `anthropic-beta: oauth-2025-04-20` header the Messages API requires for an OAuth bearer. Lets a user run Carve on their existing Claude subscription without minting an API key. **Carve implements no browser flow and stores no token of its own** — the token is minted by Claude Code's `claude setup-token` (long-lived) or `ant auth login`, both of which already own the browser exchange and refresh.

All client construction goes through **one resolver** (`carve.core.agents.client_factory.make_client`), so precedence lives in exactly one place:

**Precedence** — an explicit `auth_mode` in `models.toml` **wins**: `api_key` requires a key, `oauth` requires a token, and when it wins it **suppresses any stray opposite credential in the environment** (via the SDK's header-omit sentinel) so a globally-exported token (e.g. `CLAUDE_CODE_OAUTH_TOKEN`) can't break a pinned mode. With `auth_mode` unset, the resolver auto-selects `ANTHROPIC_API_KEY` if present, else an OAuth token (`ANTHROPIC_AUTH_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN`); if *both* are present in auto mode there is no signal to disambiguate, so it refuses with a clear error (pin `auth_mode` to choose). Either way the resolver puts **exactly one** credential on the wire — never both an API key and an auth token (the SDK sends both headers and the API 400s when both are set).

### `models.toml`

The model configuration in the [config bundle](./layout.md): `auth_mode` (`api_key` | `oauth`), the install-default model (`default_model`), and optional per-tier overrides (`tiers` — the labels a per-agent `model:` frontmatter — [extensibility](./extensibility.md) — may name, each falling back to `default_model`). The **secret itself never lives in `models.toml`** — the API key / OAuth token come from the environment; `models.toml` only selects the mode and the models.

### OSS vs. hosted

In **OSS**, both paths are available; OAuth-from-the-user's-subscription is first-class. In the **hosted** product, OAuth-from-user-subscription is **not** offered (the platform supplies model access under its own billing) — the resolver refuses the OAuth path under `CARVE_HOSTED`, an explicit seam, not an accident.

### `carve auth login` / status

`carve auth login` is a **thin wrapper over `claude setup-token`** (Claude Code's subscription-OAuth minting): it runs that command when `claude` is on `PATH`, then tells the user to put the printed token in `.env` as `ANTHROPIC_AUTH_TOKEN`; when `claude` is absent it prints how to obtain a token or set `ANTHROPIC_API_KEY`. `carve auth status` shows the resolved active mode and whether a credential is present (no secret values). Carve owns no browser flow or token file. `carve auth token …` (REST API tokens) stays with [rest-api](./rest-api.md).

## Tests

- **Unit (precedence):** `auth_mode` in `models.toml` overrides env; `ANTHROPIC_API_KEY` overrides an OAuth token; an OAuth token is used when no key is present; neither → a clear, actionable error; an explicit `auth_mode` wins over (and suppresses) a stray opposite credential, while *auto* mode refuses when both are present; the resolver never yields a two-credential client.
- **Unit (oauth wiring):** `auth_mode = oauth` (or an env token under auto-resolution) builds the client with `auth_token=…` and the `anthropic-beta: oauth-2025-04-20` header, and **no** `api_key`.
- **Unit (hosted split):** under `CARVE_HOSTED`, the OAuth-from-subscription path is refused with a clear error and the API-key/platform path is used.
- **Unit (models):** `default_model` resolves to a priced, current model id; `tiers` labels resolve via `resolve_model`, unknown refs pass through unchanged, `None` falls back to `default_model`.
- **CLI (status):** `carve auth status` reports the active mode without printing any secret value.

## Acceptance

- The AI layer authenticates via **either** `ANTHROPIC_API_KEY` **or** a Claude-subscription OAuth token, through **one** resolver (`client_factory.make_client`) with a single explicit precedence — sending exactly one credential, with a clear error when neither is present.
- OAuth uses the SDK-native bearer path (`auth_token=` + the `oauth-2025-04-20` beta header); Carve mints, stores, and refreshes **no** token of its own — `carve auth login` wraps `claude setup-token`.
- `models.toml` carries `auth_mode` + `default_model` (+ optional `tiers`); the OSS-vs-hosted credential split is an explicit resolver seam.
- Model-provider auth is cleanly separated from REST/MCP API-token auth ([rest-api](./rest-api.md)) and warehouse/source creds ([connect](./connect.md)).

## Design notes

- **Why a dedicated capability (vs. a harness section)?** Credential *precedence* across three sources (config `auth_mode`, env API key, env OAuth token), the OSS-vs-hosted policy, the `models.toml` schema, and the `carve auth` CLI are a distinct, security-sensitive concern with its own surface — even though Carve leans on the SDK for the OAuth mechanics. The harness *consumes* the credential and *scrubs* it from tools; resolving *which* credential to use is this capability's.
- **Why lean on the SDK, not a Carve-owned OAuth flow.** The `anthropic` SDK already accepts a subscription OAuth bearer (`auth_token=` + the `oauth-2025-04-20` beta header), and `claude setup-token` / `ant auth login` already own the browser exchange, storage, and refresh. A Carve-owned browser flow + `.carve/anthropic_oauth.json` + refresh loop would re-implement that with extra security surface for no gain — so Carve resolves and wires the credential and delegates acquisition.
- **Why precedence is owned in one place.** Three credential sources with a silent resolution order is a classic footgun — made worse here because the SDK 400s if both an API key and an auth token are set. A single resolver that emits exactly one credential is the point.

## Open questions

- **`carve auth login` shape — resolved.** It wraps `claude setup-token` (Claude Code's subscription-OAuth minting) and directs the user to set `ANTHROPIC_AUTH_TOKEN`; Carve runs no browser exchange of its own.
