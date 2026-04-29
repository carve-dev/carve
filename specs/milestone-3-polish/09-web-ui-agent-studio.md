# M3-09 — Web UI: Agent studio

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 1 day
**Dependencies:** M2-11 (workbench scaffolding), M3-04 (MCP client)

## Purpose

A configuration screen for agents, skills, and MCP servers. This is where users tune Carve to their team's needs without editing YAML directly. Edits become git commits, preserving the "code is source of truth" model.

## Layout

Three tabs at the top, each a different facet of configuration:

```
┌────────────────────────────────────────────────────────────┐
│  Agents  |  Skills  |  Settings                            │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  [Agent list on left]    [Editor on right]                 │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

## Agents tab

Left pane: list of agents (orchestration, dbt, snowflake, quality, plus any custom).
Right pane: editor for the selected agent.

The editor surfaces:

- **Identity** — name (read-only for built-ins), description
- **Model** — dropdown of available models (Sonnet, Opus, etc.)
- **System prompt** — large textarea, includes a button "View base prompt" that shows the system prompt with conventions injected
- **Allowed skills** — multi-select list with the registry of all skills, grouped by built-in/custom/MCP
- **Guardrails** — list of guardrail rules with add/edit/delete (M3 starts with simple structured rules; the M3+ work is a richer rule editor)
- **Token budget** — soft cap for context

Save button at the bottom commits the changes:

1. Validates the YAML against the agent schema
2. Writes the YAML file
3. Stages and commits with message `[carve] Update agent: <name>`
4. Shows a toast: "Saved. Commit abc1234"

If the user has uncommitted changes elsewhere in the repo, the save dialog warns: "You have uncommitted changes in <files>. Save will commit only the agent config."

## Skills sub-tab

Read-only in M3 (write support comes later — editing skills via the UI requires safer abstractions).

Lists all skills:

- Built-in skills (catalog queries, manifest queries, etc.)
- Custom skills (from `carve/skills/`)
- MCP skills (namespaced)

Each row: name, description, source (built-in / custom / MCP server), inputs/outputs schema (collapsed JSON viewer), source location ("View source" link opens the file path).

A "Test skill" button opens a modal with the skill's input schema as a form; users can enter args and run, see the output. Same as `carve skill test` but with a UI.

## Settings tab

Three sections:

### MCP servers

Lists configured MCP servers with status:

```
MCP Servers
├── snowflake (Connected)        12 tools         [Disable] [Reload]
├── dbt (Connected)              8 tools           [Disable] [Reload]
├── github (Disconnected)        Connection refused [View logs] [Reconnect]
└── + Add server
```

"Add server" opens a dialog with fields matching `mcp_servers.toml`. On save, appends to the file and reloads the connections.

### Connections

Lists configured Snowflake connections. Read-only in M3 (editing connection passwords through a web UI is a security concern; users edit `connections.toml` directly with env vars).

A "Test connection" button verifies it works (tries `SHOW WAREHOUSES`).

### LLM provider

Shows the configured Anthropic API key (masked) with a "Rotate" button. Token usage and cost in the last 7 days as a small chart.

## Guardrails editor

For M3, guardrails are structured rules in YAML:

```yaml
# carve/agents/dbt_agent.yaml
guardrails:
  - id: no_drop_in_dbt
    type: "regex_block"
    pattern: "DROP TABLE"
    severity: "error"
    message: "DROP TABLE statements are not allowed in dbt models."

  - id: max_files_per_change
    type: "limit"
    field: "files_modified"
    max: 20
    severity: "warn"
```

The UI represents these as a list with type-specific editors:

- `regex_block` — pattern + severity + message
- `limit` — field + max + severity
- `agent_skill_block` — skill_pattern + severity + message
- `path_block` — path_glob + severity + message

Type dropdown at the top of "add new rule." More types added in v0.2 based on user feedback.

## Right-pane editor for agent

```
┌─────────────────────────────────────────┐
│  dbt agent                              │
│  ─────────────────                      │
│  Model: claude-sonnet-4-5  ▼            │
│                                         │
│  System prompt:                         │
│  ┌─────────────────────────────────┐   │
│  │ You are Carve's dbt specialist. │   │
│  │ ...                             │   │
│  └─────────────────────────────────┘   │
│  [View prompt with conventions injected]│
│                                         │
│  Allowed skills (12):                   │
│  ☑ read_file                            │
│  ☑ write_file                           │
│  ☑ run_dbt_command                      │
│  ☑ query_dbt_manifest                   │
│  ☑ run_snowflake_query                  │
│  ☐ get_datadog_metric (custom)          │
│  ☑ mcp:dbt:* (8 tools)                  │
│  ...                                    │
│                                         │
│  Guardrails (2):                        │
│  • no_drop_in_dbt (regex_block)  [edit] │
│  • max_files (limit)              [edit]│
│  + Add rule                             │
│                                         │
│  Token budget: 50000                    │
│                                         │
│  [Save]   [Discard]   [Reset to default]│
└─────────────────────────────────────────┘
```

## Components

- `AgentList` (left pane)
- `AgentEditor` (right pane)
- `SkillsList`
- `SkillTestModal`
- `MCPServersList`
- `MCPServerEditModal`
- `GuardrailsEditor`
- `GuardrailRuleEditor`

All built using the same shadcn primitives from M2.

## API endpoints

In addition to M2-09:

- `GET /api/v1/agents/{name}` — already exists
- `PUT /api/v1/agents/{name}` — update agent (M3 adds this)
- `GET /api/v1/skills` — already exists
- `POST /api/v1/skills/{name}/test` — invoke skill with provided args
- `GET /api/v1/mcp/servers` — list MCP servers with status
- `POST /api/v1/mcp/servers` — add new server
- `PUT /api/v1/mcp/servers/{name}` — update
- `DELETE /api/v1/mcp/servers/{name}` — remove
- `POST /api/v1/mcp/servers/{name}/reconnect` — force reconnect

The `PUT /api/v1/agents/{name}` endpoint is the one that does the git commit. Implementation:

1. Validate the new YAML
2. Write the file
3. `git add` + `git commit` with structured message
4. Return updated agent + commit SHA

## Tests

- Agent edits commit to git
- Validation errors prevent save
- Skill list is correct
- MCP server add/remove updates the config file
- Guardrail rules render and edit correctly

## Acceptance criteria

- Users can browse and edit agent definitions through the UI
- Saved edits result in a git commit with a clear message
- Skills tab shows all available skills with a working test action
- MCP servers can be added, removed, and tested

## Files

- `src/carve/ui/src/pages/AgentStudio.tsx`
- `src/carve/ui/src/components/AgentList.tsx`
- `src/carve/ui/src/components/AgentEditor.tsx`
- `src/carve/ui/src/components/SkillsList.tsx`
- `src/carve/ui/src/components/SkillTestModal.tsx`
- `src/carve/ui/src/components/MCPServersList.tsx`
- `src/carve/ui/src/components/GuardrailsEditor.tsx`
- New endpoints in `src/carve/server/routers/agents.py` and `mcp.py`
- `tests/server/test_agent_updates.py`

## What this enables

- Non-developers on the data team can tune agents
- The configuration surface has a discoverable home
- Edits are still git-tracked and reviewable
