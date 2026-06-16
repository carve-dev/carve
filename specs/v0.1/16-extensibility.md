# v0.1-16 — Extensibility: declarative agents, skill packs, hooks, MCP

> **Foundation spec** — the "bring your own agents, skills, MCPs, CLIs, hooks" model that makes the harness extensible. Per [`../_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md). Resolves the built-vs-spec agent drift (the AI-map finding): agents/skills become **declarative + discoverable**, not hardcoded in Python.

## Status

- **Status:** Drafting
- **Depends on:** [v0.1-15 agent-harness](./15-agent-harness.md) (agents/skills/tools are defined against it), [v0.1-06 project-memory](./06-project-memory.md) (same file-discovery + mtime-cache patterns).
- **Blocks:** the domain agent specs (04 DLT, 08 pipeline, 12 explorer, recovery, SQL) — each ships as a **declarative agent** loaded by this registry. Also the connector→skill library.

## Goal

Make the harness extensible without forking. Users (and Carve's own built-ins) define agents, skills, hooks, and MCP imports **declaratively**, hot-reloaded and discoverable:

1. **Agents = markdown + frontmatter** (`carve/agents/<name>.md`). Drop a file → it's a routable subagent. Built-ins and user agents use the same format; user agents override built-ins by name.
2. **Skills = capability packs** (`carve/skills/<name>/SKILL.md` + optional scripts/resources), loaded on demand by description-match (progressive disclosure). **The curated connector library becomes a skill library** — connectors, skills, and bring-your-own unify into one model.
3. **Hooks** (`carve/hooks.toml`) — pre/post-tool and on-event automation/policy (`sqlfluff` before commit; block writes to a prod schema; Slack on deploy).
4. **MCP (consume)** — register external MCP servers; their tools enter the skill registry namespaced. (Carve *exposing* an MCP server is [v0.1-10](./10-mcp-server.md).)

## Out of scope

- The **harness core** (loop, subagents, tools, permissions) — [v0.1-15](./15-agent-harness.md).
- The **specific built-in agents/skills** (DLT/pipeline/explorer/recovery/SQL) — their own specs; this spec ships the *format + loaders* they use.
- **Carve-exposes-MCP** (stdio/WebSocket server) — [v0.1-10](./10-mcp-server.md).
- **In-process custom-skill SDK** (`@skill`-decorated Python from users) and **custom step-type SDK** — post-v0.1 (the MCP + SKILL.md paths cover v0.1 extensibility).

## Files this spec produces

```
src/carve/core/agents/definition.py          # NEW — parse an agent markdown file: frontmatter (name/description/model/tools/allowed_paths/classifications) + system-prompt body
src/carve/core/agents/registry.py             # NEW — AgentRegistry: discover built-in + carve/agents/*.md agents; hot-reload (mtime); name override; routing lookup
src/carve/core/skills/pack.py                 # NEW — SkillPack loader: parse carve/skills/<name>/SKILL.md (frontmatter + body + bundled scripts/resources); progressive disclosure by description-match
src/carve/core/skills/registry.py             # MODIFY — register built-in @skill functions + SkillPacks + the connector library (src/carve/sources/* exposed as packs) + MCP-imported tools
src/carve/core/agents/hooks.py                # NEW — Hook config (carve/hooks.toml), event set (pre_tool/post_tool/on_run_failed/pre_deploy/post_build), runner (ordered, fail-policy)
src/carve/core/mcp/client.py                  # NEW — connect to an external MCP server; import its tools as namespaced skills (mcp:<server>:<tool>) with effects metadata
src/carve/core/mcp/registry.py                # NEW — carve/mcp.toml: registered external MCP servers
src/carve/cli/agents.py                       # NEW — carve agents list/show/create/edit/test
src/carve/cli/skills.py                       # NEW — carve skills list/show/test
src/carve/cli/mcp_servers.py                  # NEW — carve mcp-servers list/add/remove
templates/agent.md.j2                         # NEW — `carve agents create` scaffold
src/carve/sources/_reference_hackernews/SKILL.md  # NEW — the reference connector, now expressed as a skill pack (proves connector==skill)
tests/unit/test_agent_definition.py           # NEW
tests/unit/test_agent_registry_override.py    # NEW
tests/unit/test_skill_pack_discovery.py       # NEW
tests/unit/test_hooks.py                      # NEW
tests/integration/test_mcp_import.py          # NEW
docs/extending-carve.md                       # NEW — author an agent, a skill pack, a hook; register an MCP server
```

## Behavior

### Declarative agents (`carve/agents/<name>.md`)

```markdown
---
name: dlt-engineer
description: Authors and runs dlt sources/pipelines into a named dlt component. Use for ingest / extract-load goals.
model: claude-{LATEST_SONNET} # optional; falls back to the install default. Enables per-agent model tiering.
tools: [edit, bash, grep, glob, web_fetch, dlt_library, schema_introspect, sql]   # base tools (spec 15) + skills (this spec)
allowed_paths: ["el/**", ".dlt/*.template"]   # write scope enforced by the permission gate (spec 15)
classifications: [new_pipeline, modify_pipeline, refactor_to_incremental]
---
<system prompt body…>
```

- **`AgentRegistry`** discovers built-in agents (shipped as the same markdown format, under `src/carve/core/agents/builtin/*.md`) plus user agents under `carve/agents/*.md`. A user file **overrides** a built-in of the same name (clear precedence; surfaced by `carve agents show`).
- **Hot-reload:** mtime-watched; the next invocation re-reads a changed file (no `carve serve` restart) — per ARCHITECTURE §5.6.
- **Routing:** the orchestrator matches a goal's classification against each agent's `classifications` (and `description` for fuzzier matches) to pick the subagent to `delegate` to (spec 15). This replaces the hardcoded `AGENT_REGISTRY` dict.
- **Tool grants are validated against the permission mode** (spec 15): an agent can only be granted tools the active mode permits; an over-broad grant is rejected at load with a clear error.
- **`carve agents create <name> [--template <existing>]`** scaffolds a new agent file (optionally cloning a built-in).

### Skill packs (`carve/skills/<name>/SKILL.md`)

```markdown
---
name: stripe
description: Curated dlt source for the Stripe API. Use when ingesting Stripe data.
expects_env: [STRIPE_API_KEY]
---
<instructions: how to use the bundled dlt source, validation glue, conventions>
```

- A **SkillPack** is a folder: `SKILL.md` (frontmatter + instructions) + optional `scripts/` and `resources/` (e.g. the dlt source code, a validation helper). Loaded by **description-match** (progressive disclosure) — the agent sees only the packs relevant to its task, keeping context small.
- **The connector library is a skill library:** `src/carve/sources/<name>/` ships as skill packs; the EL agent's "copy a curated source" becomes "apply the `<name>` skill pack." The `_reference_hackernews` source is re-expressed as a `SKILL.md` to prove the model.
- Built-in `@skill`-decorated functions (the shipped catalog skills — `list_tables`, `describe_table`, etc.) remain first-class in the same registry; SkillPacks and MCP tools join them.
- **`carve skills list/show/test`** surfaces the catalog (built-ins + packs + MCP), incl. which agent/pack provides each.

### Hooks (`carve/hooks.toml`)

```toml
[[hook]]
on = "pre_tool"; match = { tool = "bash", command = "git commit*" }
run = "sqlfluff lint --dialect snowflake {changed_sql}"   # non-zero exit blocks the tool call

[[hook]]
on = "on_run_failed"; run = "notify-slack {pipeline} {error}"

[[hook]]
on = "pre_deploy"; run = "scripts/policy_check.sh"        # block deploys that violate a team policy
```

- **Events:** `pre_tool` / `post_tool` (gate or react to a tool call), `pre_deploy` / `post_build`, `on_run_failed` (and the other runtime events from spec 07). Ordered; a `pre_*`/`pre_tool` hook with a non-zero exit **blocks** the action (policy enforcement without forking).
- Hooks run via `bash` (spec 15) under the same sandbox; they are the user's escape hatch for policy + automation.

### MCP (consume)

- `carve mcp-servers add <name> --command "<stdio cmd>"` (or URL) registers an external MCP server in `carve/mcp.toml`. `src/carve/core/mcp/client.py` connects, lists the server's tools, and imports them into the skill registry **namespaced** as `mcp:<server>:<tool>`, carrying each tool's `effects` metadata (e.g. `writes=true`) so the permission gate and the `ask` no-write guardrail (spec 12) can reason about them.
- An agent grants MCP tools the same way it grants any skill (`tools: [..., "mcp:jira:*"]`).

## Tests

- **Unit (agent definition):** a markdown agent parses (frontmatter + body); a malformed file raises a clear error; an over-broad `tools` grant for the mode is rejected.
- **Unit (registry override):** a `carve/agents/dlt-engineer.md` overrides the built-in of the same name; `carve agents show` reports the override; hot-reload picks up a changed file.
- **Unit (skill pack discovery):** a `SKILL.md` pack loads and is offered to an agent on a description-match; the `_reference_hackernews` connector loads as a pack.
- **Unit (hooks):** a `pre_tool` hook with non-zero exit blocks the tool call; `on_run_failed` fires on a failed run; ordering respected.
- **Integration (MCP import):** a fixture MCP server's tools appear namespaced in the registry with effects metadata; an agent can call one; a `writes=true` MCP tool is blocked in `read_only`/`ask`.

## Acceptance

- A user drops `carve/agents/my-agent.md` and it is immediately routable (hot-reload), overriding a built-in by name.
- A `carve/skills/<name>/SKILL.md` pack is discovered and applied on a relevant task; the curated connector library is loaded as skill packs (connector == skill).
- A `pre_deploy` hook can block a deploy; an `on_run_failed` hook fires.
- A registered MCP server's tools are usable by agents, namespaced and effects-tagged; the permission gate honors the effects.
- `carve agents/skills/mcp-servers` CLI list/show/create/test work; `carve agents create` scaffolds a working agent.

## Design notes

- **Why declarative (markdown frontmatter)?** It is *the* extensibility unlock and it resolves the built-vs-spec drift: agents stop being hardcoded Python and become files anyone can author, version, and share — exactly the Claude Code subagent/skill model. Human-readable, diffable, hot-reloadable.
- **Why unify connectors + skills?** A curated connector and a user skill are the same shape (a capability pack with instructions + resources). One model means the community library, the built-in catalog, and bring-your-own all flow through one loader — and the connector library stops being a special case.
- **Why hooks?** Teams need to inject policy/automation (linting, schema guards, notifications) without forking Carve. A `pre_*` hook that can block is policy-as-config.
- **Why MCP both directions?** Consume = bring your tools (catalog, Jira, internal APIs); expose (spec 10) = drive Carve from Claude Desktop/Cursor. Standard, no bespoke plugin protocol.
- **Tool grants validated against the permission mode** keeps extensibility safe: a user agent can't grant itself `bash`-in-`read_only`.

## Open questions

- **Skill-pack discovery at scale.** *Implementation default.* Description-match is fine for tens of packs; an embedding index is post-v0.1 if the library grows large (consistent with the deferred embedding-search note).
- **Hook sandboxing + trust.** *Strategy-required.* Hooks run arbitrary commands (`bash`); for v0.1 they run under the same sandbox as agent bash and are repo-committed (so they're code-reviewed). Confirm whether unsigned/third-party hooks need an extra gate.
- **Agent-override precedence + namespacing.** *Implementation default.* User `carve/agents/*` overrides built-ins by name; a future namespacing scheme (org/team packs) is post-v0.1.
- **In-process custom-skill SDK.** *Deferred.* v0.1 ships SKILL.md packs + MCP; the `@skill`-decorated-Python-from-users SDK (with sandboxing/versioning) is post-v0.1.
