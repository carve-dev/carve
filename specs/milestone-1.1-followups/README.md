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
- `carve plan` prints live progress as the agent calls tools, instead of a frozen terminal followed by a summary.
- The M1 code agent gets a tightened system prompt: connection-context preamble (so the agent stops inventing destination databases), and rules against generating `## How to Run` sections that bypass `carve apply`.
- The lifecycle gets honest verbs: `plan` (conversational design, no files), `plan --refine` (iterate), `build` (generate code), `run` (execute in dev), `apply` (M2 placeholder for prod-PR deployment).
- Failed runs permit retry without manual SQLite surgery — the replay guard distinguishes success from failure.
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
3. [`07-failed-apply-permits-retry.md`](./07-failed-apply-permits-retry.md) — the replay guard distinguishes success from failure; failed runs are retriable. Tiny but unblocks smoke-testing.
4. [`04-plan-progress-output.md`](./04-plan-progress-output.md) — live progress output during `carve plan` (small; addresses the "is it broken?" perception).
5. [`06-plan-build-run-separation.md`](./06-plan-build-run-separation.md) — split today's `plan` into `plan` (design) + `build` (code generation), promote `run` from stub to real, reserve `apply` for M2. Largest spec in the milestone — touches the orchestrator, prompts, schema, and CLI surface.
6. [`05-m1-agent-prompt-tightening.md`](./05-m1-agent-prompt-tightening.md) — connection-context preamble + ban "How to Run" sections in plan summaries. Folds into `06`'s new build agent if shipped after.
7. [`02-claude-code-oauth.md`](./02-claude-code-oauth.md) — add the OAuth auth path (larger, needs SDK investigation).

Ship `01`, `03`, `07`, `04` first — they unblock every new user and are independent. Ship `06` next because it changes the lifecycle the user sees; once it lands, `05`'s prompt rules apply to the new build agent. `02` is the OAuth opt-in and sits alone.

If `06` is built before `05`, fold `05`'s prompt rules into `06`'s new build-agent prompt and close `05` as superseded. `07`'s implementation will need a small tweak after `06` lands (file rename `applier.py` → `runner.py`).

## Definition of done

- Both specs are implemented and have tests.
- `carve init` produces files a non-author can fill in without reading Carve source.
- A user with a Claude Max plan can run `carve plan` end-to-end without ever creating a Console API key.
- Internal tag `v0.1.0-m1.1` (or roll into `v0.0.2` if shipping more frequently).
