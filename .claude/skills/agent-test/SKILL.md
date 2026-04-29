---
name: agent-test
description: Run a Carve runtime agent against a test prompt without persisting state, useful during agent-author workflows to verify a new agent behaves as expected before committing. Use this skill after creating or modifying an agent definition under `carve/agents/`. Arguments are an agent name and a test prompt. Produces a transcript of the agent's tool calls and final response on stdout, plus a written transcript at `.carve-build/agent-tests/{agent-name}-{timestamp}.md`.
---

# /agent-test

Loads a named Carve runtime agent (one of the agents under `carve/agents/` — distinct from the build-time agents in `.claude/agents/`) and runs it against a test prompt. State is not persisted to the run database. Tool calls are executed; if you don't want side effects, run against a sandboxed Snowflake account or use the agent's dry-run mode (when supported).

This skill is intended to be invoked by `agent-author` during agent development, but is also useful manually whenever you want to sanity-check an agent's behavior.

## Arguments

Two positional arguments:

1. **Agent name** — the name of an agent under `carve/agents/`, e.g. `dbt-agent`, `orchestration-agent`. Resolves to `carve/agents/{name}.toml`.
2. **Test prompt** — a string the agent receives as the user message. Quote it if it has spaces.

Example:

```
/agent-test dbt-agent "make stg_orders incremental on order_updated_at"
```

## Process

1. **Resolve the agent.** Look up `carve/agents/{name}.toml`. If missing, abort with a clear error listing available agents.
2. **Load the agent definition.** Parse the TOML. Validate required fields (`name`, `model`, `system_prompt`, `skills`, `max_tokens`). If any are missing or malformed, abort with a clear error.
3. **Set up an in-memory state context.** Use the same agent runtime as `M1-04` (`AgentLoop` and friends), but with a no-op repository — log lines and token usage are captured to memory, not written to the SQLite store.
4. **Run the agent.** Invoke its loop with the test prompt as the initial user message and a `max_turns` of 30 (or whatever the agent definition specifies, capped at 30 for tests).
5. **Capture the transcript.** For each turn, record:
   - The assistant's reasoning text (if streaming wasn't used)
   - Any tool calls made (tool name, arguments)
   - The tool results returned
6. **Print to stdout** as the test runs (live progress) and write the full transcript to `.carve-build/agent-tests/{agent-name}-{ISO-timestamp}.md`:

   ```markdown
   # Agent test: {agent-name}

   **Run at:** {ISO timestamp}
   **Prompt:** {test prompt}
   **Result:** {ended cleanly | hit max turns | error}
   **Turns:** {n}
   **Token usage:** input {n}, output {n}, cost ${n}

   ## Transcript

   ### Turn 1 — assistant

   {response text or summary}

   ### Turn 1 — tool calls

   - **Tool:** `{tool name}`
   - **Input:** ```json
     {input JSON}
     ```
   - **Result:** ```
     {tool result}
     ```

   {repeat per turn}

   ## Final response

   {final assistant message text}
   ```

7. **Exit cleanly.** No state is persisted to the run database. Token usage shown in the summary is from the in-memory tracker.

## Constraints

- **Side effects are real.** Tools the agent calls (e.g. `run_snowflake_query`, file writes) execute against the live environment. Use a sandboxed Snowflake account or set `CARVE_AGENT_TEST_DRY_RUN=1` to make tool calls return a stub instead of executing (when supported by the tool — not all tools support dry-run).
- **No state pollution.** Run records, log entries, plan files — none are written. Token cost is reported but not billed against any persistent counter.
- **Failure is also a result.** If the agent loops forever, errors out, or produces nonsense, that's the test outcome — report it. Don't retry to make it look better.
- **Distinct from build-time agents.** `/agent-test` runs the agents under `carve/agents/` (the runtime agents Carve ships to users). The build-time agents under `.claude/agents/` are loaded by Claude Code itself, not by this skill.
