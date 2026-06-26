# Extensibility: declarative agents, skill packs, hooks, MCP

> **Foundation spec** — the "bring your own agents, skills, MCPs, CLIs, hooks" model that makes the harness extensible. Per [`../_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md). Resolves the built-vs-spec agent drift: agents/skills become **declarative + discoverable**.
>
> **Hardened per the 15/16 adversarial review (2026-06-16):** grant validation is **runtime attenuation** (the [spec 15](./harness.md) gate is the boundary, not load-time); MCP/load defaults are **fail-closed**; the hook event set has named **emission points** (incl. `pre_deploy`/`post_build`, per the advanced-primitives decision).

## Status

- **Status:** Drafting
- **Depends on:** [harness](./harness.md) (the gate/hook fire-points, the runtime grant rule, the tool set), [memory](./memory.md) (the mtime-cache discovery pattern), [runtime](./runtime.md) (the `run.failed` event the `on_run_failed` hook subscribes to).
- **Blocks:** the domain agent specs (04/08/12/recovery/SQL) — each ships as a **declarative agent** loaded by this registry.

## Goal

Users (and Carve's built-ins) define agents, skills, hooks, and MCP imports **declaratively**, hot-reloaded and discoverable:

1. **Agents = markdown + frontmatter** (built-ins at `src/carve/core/agents/builtin/<name>.md`; user agents at `carve/agents/<name>.md`, overriding built-ins by name).
2. **Skills = capability packs** (`carve/skills/<name>/SKILL.md`), loaded on description-match. **The connector library is a skill library.**
3. **Hooks** (`carve/hooks.toml`) — pre/post-tool + lifecycle automation/policy.
4. **MCP (consume)** — register external servers; their tools enter the registry namespaced + effects-tagged.

## Out of scope

- The **harness core** (loop, gate, subagents, tools) — [harness](./harness.md). This spec defines the *format + loaders* + the hook *config*; spec 15 owns the gate, the tool-set intersection, and the hook fire-points.
- **Carve-exposes-MCP** (server) — [mcp-server](./mcp-server.md).
- **In-process custom-skill SDK** (`@skill`-decorated Python from users) + **custom step-type SDK** — a later increment.

## Behavior

### Declarative agents

```markdown
---
name: dlt-engineer
description: Authors and runs dlt sources/pipelines into a named dlt component. Use for ingest / extract-load goals.
model: claude-{LATEST_SONNET}      # optional; per-agent tiering; falls back to the install default
tools: [edit, create_file, bash, grep, glob, web_fetch, dlt_library, sql]   # base tools (spec 15) + skills (this spec)
allowed_paths: ["el/**", ".dlt/*.template"]
max_mode: build                    # the highest permission mode this agent ever needs (advisory lint input + clamp; the runtime gate is authoritative)
classifications: [new_pipeline, modify_pipeline, refactor_to_incremental]
---
<system prompt body…>
```

- **Discovery roots:** built-ins at `src/carve/core/agents/builtin/*.md`; user agents at `carve/agents/*.md` (`PathsConfig.agents_dir`). A user file **overrides** a built-in of the same name — surfaced by `carve agents show`, with a **no-silent-overwrite** load discipline (mirroring the shipped `skills/registry.py` collision handling): a duplicate within the same root is an error, a user-over-builtin override is logged.
- **Hot-reload at dispatch time only:** the registry re-reads a changed file when the orchestrator is about to `delegate` (build a SubagentRunner) — **never mid-conversation** — using spec 06's `(mtime, parsed)` cache. No `carve serve` restart.
- **Loading is inert:** frontmatter is parsed with a **safe** loader (no arbitrary object construction); bundled scripts/resources are **never executed at load** (only later, if the agent invokes them via gated `bash`); a malformed/oversized file **fails the load** with a clear error rather than partially registering.
- **Routing:** the orchestrator matches a goal's classification against each agent's `classifications` (+ `description`) to pick the subagent to `delegate` to. Replaces the hardcoded `AGENT_REGISTRY` dict (`agents/__init__.py`).
- **`max_mode` is advisory:** a load-time **lint** warns if an agent grants a tool its `max_mode` could never use (e.g. `bash` with `max_mode: read_only`). It is **not** a security boundary — the runtime gate (spec 15) attenuates `runtime tools = grant ∩ mode-permitted` on every call, and a user override file cannot raise the effective mode or escape `allowed_paths`.
- **`carve agents create <name> [--template <existing>]`** scaffolds a new agent file.

### Skill packs

```markdown
---
name: stripe
description: Curated dlt source for the Stripe API. Use when ingesting Stripe data.
expects_env: [STRIPE_API_KEY]
---
<instructions: how to use the bundled dlt source, validation glue, conventions>
```

- A **SkillPack** is a folder: `SKILL.md` (frontmatter + instructions) + optional `scripts/`/`resources/` (e.g. the dlt source code). It surfaces as **description-matched content injected into the agent's context** (the shipped `lookup_skill` progressive-disclosure pattern) — **not** as a callable tool — keeping context small and avoiding the loop's flat tool/skill namespace.

  > **Updated during implementation (2026-06-19):** the injection is delivered by a dedicated **`lookup_skill_pack`** tool (`core/skills/pack_discovery.py` → `SkillPackLibrary.make_lookup_tool`; built at the orchestrator via `build_skill_pack_tool`). It mirrors the pre-existing `lookup_skill` disclosure pattern but is a *separate* tool so a pack stays content, not a callable skill. `lookup_skill_pack` is added to the **read-tools floor** in `permissions/policy.py` (it only reads inert on-disk `SKILL.md` content and writes nothing), so a gated loop never DENYs a permitted, read-only pack injection.
- **The connector library is a skill library:** `src/carve/sources/<name>/` ships as skill packs; "copy a curated source" = apply the pack. *(The real `_reference_hackernews` is created by spec 04; this spec's discovery test uses a self-contained `tests/fixtures/skill_packs/_example/` pack so it's verifiable at 16's build time.)*
- **Built-in callable skills** come in two shapes that both surface to an agent through its `tools:` grant. **Warehouse-coupled** skills that need a live `SkillContext` (a Snowflake pool, the repository) stay first-class `@skill` functions registered in `skills/builtin/__init__.py` — the catalog skills + the readers the explorer needs (`dbt_manifest`, `dlt_schema` ([lineage](./lineage.md)), `memory_read`). **Domain skills that need only the project tree or the network** (e.g. the DLT engineer's `existing_dlt_inspect` / `dbt_source_lookup` / `rest_api_explore` / `dlt_library`) are plain callable **Tools** bound through the grant→executor binder — no `SkillContext` to thread into a delegated subagent. Both are granted by name; the binder/registry resolves the executor. *(There is no lineage graph or `upstream_of`/`downstream_of` skill family — lineage is investigated on demand via `dbt_manifest` + `dlt_schema` + `grep`; see [lineage](./lineage.md).)*
- **Namespace:** callable tools (base tools + `@skill` functions + `mcp:<server>:<tool>`) share the one namespace the loop guards (`loop.py` raises on collision). MCP names are namespaced (`mcp:`) so they can't collide; SkillPacks are content (not in the tool namespace); a user agent granting a name that's both a base tool and a pack resolves to the base tool (logged).

  > **Grant → executor binding.** A declarative `tools:` grant is a list of *names*; binding turns each granted name into the real executor when the runner composes a delegated agent's toolset — harness base tools built from `(project_dir, the child-clamped gate, approver)`, plus tools whose dependency lives outside the harness (e.g. a connection-backed `sql`) injected by the caller under a bound-name == grant-name precondition. An unbound name fails loud (never a silent no-op). The gate is built from the grant names and attenuates every call at the child's mode, so binding never widens authority.
- **`carve skills list/show/test`** surfaces the catalog (built-ins + packs + MCP), with the provider of each.

### Hooks

```toml
[[hook]]
on = "pre_tool"; match = { tool = "bash", command = "git commit*" }
run = "sqlfluff lint --dialect snowflake {changed_sql}"   # non-zero exit blocks the tool call

[[hook]]
on = "on_run_failed"; run = "notify-slack {pipeline} {error}"   # subscribes to spec 07's run.failed event

[[hook]]
on = "pre_deploy"; run = "scripts/policy_check.sh"        # block deploys that violate a policy
```

- **Events + emission points:**
  - `pre_tool` / `post_tool` — fire at the loop's tool-execution seam ([spec 15](./harness.md)), **after** the permission gate admits the call (so a `pre_tool` hook can only further-restrict, never enable a denied call).
  - `pre_deploy` — emitted by `carve deploy` ([spec 14](./deploy.md)) before promotion.
  - `post_build` — emitted by `carve build` after materialization. **The emitter is owned by [plan-build](./plan-build.md)'s builder** (the `carve build` verb lives there, not in [pipelines](./pipelines.md)) and is **now live** — `POST_BUILD` sits in `EMITTED_EVENTS`, fired by the builder after the `Build` is durably recorded. Extensibility ships only the *subscription* seam (`HookRegistry`/`events.py`); the post-commit fail-closed contract (a raising hook is surfaced, the Build stands) lives on plan-build's side. *(Reference corrected from spec 08; see the callout below.)*
  - `on_run_failed` — a **subscriber on spec 07's `run.failed` event** (reconciles the naming; the runtime fires `run.failed` from the async worker, the hook runner subscribes via the events table).

> **Updated during implementation (2026-06-26):** the `post_build` **subscription seam shipped here** (Increment 1 extensibility), and its **emitter — owned by [plan-build](./plan-build.md)'s builder — is now live**: `POST_BUILD` was lifted from `DEFERRED_EMITTER_EVENTS` into `EMITTED_EVENTS` and is fired by the builder (in both the M1 `build_plan` path and `_build_multi_engine`) after the `Build` is durably recorded. So `post_build` is **no longer a deferred-emitter event** — only `pre_deploy` (deploy) and `on_run_failed` (runtime `run.failed`) remain in `DEFERRED_EMITTER_EVENTS`. The **subscription seam this spec owns is unchanged**; the emit point + its post-commit fail-closed contract live on plan-build's side. This corrects the stale "spec 08 / pipelines" attribution above — `carve build` is plan-build's verb. The cross-spec ownership decision is recorded in [DELIVERY.md](../DELIVERY.md) → *Current state*.
- **Hook execution is itself gated + clamped:** a hook command runs via the **same `bash` gate** (no bypass — same metachar-deny/allowlist/scrubbed-env/sandbox) and is **mode-clamped** (no network/git in `read_only`). A `pre_*` hook does **not** re-enter the `pre_tool` pipeline (no recursion). A hook that errors/times out is **fail-closed** (blocks the action), matching the gate.

  > **Updated during implementation (2026-06-19):** the mode-clamp is realized by a **hook factory** seam, `HookFactory = (mode) -> (pre_tool, post_tool)` (`cli/orchestrator/extensibility_wiring.py`), which rebuilds the `HookRunner`'s bash gate at *the mode it is invoked with*. The top-level plan/build loops use the eager single-mode `build_extensibility_hooks`; **delegation** instead hands `SubagentRunner` the *factory* and calls it at `child_mode`, so a hook firing inside a narrower child is clamped to the **child's** authority (a `read_only` child's hook cannot run write/network bash) rather than inheriting the parent's pre-built closure. The "mode-clamped" property is unchanged; this is the mechanism that backs it.

### MCP (consume)

- `carve mcp-servers add <name> --command "<stdio cmd>"` (or URL) registers a server in `carve/mcp.toml`; `mcp/client.py` imports its tools as `mcp:<server>:<tool>`, carrying each tool's `effects` metadata.
- **Fail-closed default:** an imported MCP tool with **no/incomplete `effects`** is treated as **`writes=true`** — denied in `read_only`/`plan`, prompted in `build`/`deploy`. (So the `ask`/explorer no-write guarantee holds even for a sloppy or malicious server.)
- An agent grants MCP tools like any skill (`tools: [..., "mcp:jira:*"]`).

## Tests

- **Unit (agent definition):** a markdown agent parses (safe loader); a malformed/oversized file fails the load; a duplicate-name within a root errors, a user-over-builtin override is logged.
- **Unit (registry override + reload):** a user `dlt-engineer.md` overrides the built-in; hot-reload picks up a changed file **at dispatch**, not mid-conversation.
- **Unit (skill pack):** the self-contained fixture pack loads and is offered on a description-match; bundled scripts are **not** executed at load.
- **Unit (hooks):** a `pre_tool` hook with non-zero exit blocks the call; it fires **after** the gate; a hook that errors is fail-closed; a hook command with `$()`/`;` is denied by the bash gate.
- **Integration (MCP):** a fixture server's tools appear namespaced + effects-tagged; a tool **omitting effects** is treated as `writes=true` and denied in `read_only`.

## Acceptance

- A user drops `carve/agents/my-agent.md` and it is routable (dispatch-time hot-reload), overriding a built-in by name — but cannot raise its effective mode or escape `allowed_paths` (the gate clamps it).
- A `SKILL.md` pack (the fixture) is discovered and applied; loading any agent/pack is side-effect-free.
- A `pre_deploy` hook can block a deploy; `pre_tool` hooks run after the gate and can only further-restrict; a hook itself passes the bash gate.
- An MCP tool with missing effects is treated as a writer (denied in `read_only`); namespaced MCP tools don't collide with the base namespace.
- `carve agents/skills/mcp-servers` CLI work; `carve agents create` scaffolds a working agent.

## Design notes

- **Why grants are runtime attenuation, not a load-time boundary?** The active mode is per-invocation (spec 15), and a user file overrides built-ins — so "reject an over-broad grant at load" can neither know the mode nor be a boundary. The runtime gate (`runtime tools = grant ∩ mode-permitted`) is the airtight surface; `max_mode` is a helpful lint, not the control.
- **Why fail-closed MCP/load defaults?** A missing-`effects` MCP tool defaulting permissive would slip a writer past `read_only`; auto-running a pack's bundled scripts at load would be RCE-on-discovery. Both default to the safe side.
- **Why SkillPacks as content, not callable tools?** The loop guards a flat tool/skill namespace and raises on collision; packs as description-matched content (the shipped `lookup_skill` pattern) keep the namespace clean and context small, and unify connectors + skills.
- **Why hooks pass the same gate + are fail-closed?** A `pre_tool` hook runs on every call and runs arbitrary `bash`; without the same gate it'd be an escalation. Fail-closed-on-error matches the gate's stance.

## Open questions

- **Lineage graph owner.** *Resolved — no graph.* Carve maintains no `lineage_nodes`/`lineage_edges` store (the original ARCHITECTURE §6.2 graph is retired). [lineage](./lineage.md) reframes lineage as **investigation**: the explorer reads dbt's manifest + dlt's schema (the new `dlt_schema` skill) + the code on demand. Column-level lineage is a later increment.
- **Skill-pack discovery at scale.** *Implementation default.* Description-match for tens of packs; an embedding index is a later increment.
- **Org/team agent namespacing.** *Implementation default.* User-overrides-builtin by name for now; richer namespacing is a later increment.
