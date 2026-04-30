# Milestone 1.1 — M1 follow-ups

**Duration:** as needed (no fixed deadline)
**Goal:** ship UX polish and auth ergonomics that surfaced during M1 smoke testing, before M2 starts in earnest.

## Acceptance criteria

A new user can:

1. Run `carve init` and immediately understand what every generated config file expects, without reading source code.
2. Authenticate against Anthropic via either an API key (Console) **or** their Claude Code subscription (OAuth) by editing `carve/models.toml`.

## What ships

- `carve init` writes commented-out, ready-to-edit templates for `connections.toml`, `models.toml`, `runner.toml`, and an expanded `.env.example`.
- The CLI auto-loads `.env` from the project root at startup, so the natural setup flow (`init` → edit `.env` → `plan`) just works.
- A second auth mode (`claude_code_oauth`) on `ModelsConfig` that uses the Claude Agent SDK instead of the `anthropic` SDK, drawing on the user's Claude Code Max plan credits.

## What is explicitly deferred

- Schema-driven config-template generation (auto-derive templates from Pydantic models). Hand-written templates are fine for now.
- Interactive `carve init` (asking the user for values up front).
- OAuth for non–Claude Code Anthropic flows (Workbench-issued tokens, etc.).
- Multi-tenant / SaaS auth.

## Spec list

In recommended build order:

1. [`01-init-config-templates.md`](./01-init-config-templates.md) — replace the one-line comment placeholders with working templates (small, low-risk).
2. [`03-dotenv-autoload.md`](./03-dotenv-autoload.md) — auto-load `.env` at CLI startup (small; pairs naturally with `01` so the templated `.env.example` becomes a working default).
3. [`02-claude-code-oauth.md`](./02-claude-code-oauth.md) — add the OAuth auth path (larger, needs SDK investigation).

`01` and `03` should ship first because they unblock every new user and are independent of `02`. Build `01` before `03` if you want, or in parallel — they don't depend on each other in code, only in narrative. `02` requires reading the `claude-agent-sdk` Python package and may produce schema/CLI changes worth landing once `01`'s templates are in place to advertise the new auth_mode.

## Definition of done

- Both specs are implemented and have tests.
- `carve init` produces files a non-author can fill in without reading Carve source.
- A user with a Claude Max plan can run `carve plan` end-to-end without ever creating a Console API key.
- Internal tag `v0.1.0-m1.1` (or roll into `v0.0.2` if shipping more frequently).
