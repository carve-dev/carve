# v0.1-15 — Agent harness: subagent delegation, terminal-grade tools, permissions, verification

> **Foundation spec** — the Claude-Code-style agentic engine the domain agents (04 DLT, 08 pipeline, 12 explorer, recovery, SQL) run on. Per [`../_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md). Read after [v0.1-01](./01-state-store-postgres.md); it underpins 04/08/12 (which it numbers after only for bookkeeping — placement reorg is a noted follow-up).

## Status

- **Status:** Drafting
- **Depends on:** M1 agent loop (`src/carve/core/agents/loop.py` — preserved + extended, HISTORICAL), [v0.1-03 flat-layout](./03-flat-layout.md) (the component locator subagents resolve against).
- **Blocks:** [v0.1-16 extensibility](./16-extensibility.md), and the agent revisions/new agents (04, 08, 12, recovery, SQL) — all run on this harness.

## Goal

Evolve Carve's agent loop into a **Claude-Code-style harness**: a main loop that **delegates to subagents**, armed with **terminal-grade tools**, behind a **permission system**, that **verifies its work by executing it**. Concretely:

1. **Subagent delegation** — a `delegate` tool spawns a typed subagent (fresh loop, own context/tools/prompt) and returns a *summary*, not the transcript. This is how the orchestrator routes domain work and how Carve manages context.
2. **Terminal-grade tools** — `edit` (string-replace), `bash` (permissioned + sandboxed; runs the real `dlt`/`dbt`/`git`/`gh`/warehouse CLIs), `glob`/`grep`, `web_fetch`/`web_search`.
3. **Permission system** — modes (`read_only`/`plan`/`build`/`deploy`) + allowlists + bash sandbox, checked *before* each tool call; plus role-scoped warehouse access.
4. **Verification loop** — a harness primitive for generate → run → read → fix (run `dlt pipeline run`/`dbt build`, parse the real result, feed back).
5. **Steerability + context management** — inject guidance mid-task (chat mode); subagent context-isolation + compaction for long sessions.

This spec ships the harness mechanics. It does **not** ship the declarative agent/skill *format* (that's [v0.1-16](./16-extensibility.md)) or the specific domain agents.

## Out of scope

- The **declarative agent/skill definition format** (`carve/agents/*.md`, `SKILL.md`), hooks, and MCP import — [v0.1-16](./16-extensibility.md).
- The **specific domain agents** (DLT/pipeline/explorer/recovery/SQL) — their own specs.
- **Hosted sandboxing** (gVisor/Firecracker/per-tenant isolation) — hosted concern; v0.1 ships an OS-level/allowlist sandbox.
- The **REST/MCP surface** for driving agents (specs 09/10).

## Files this spec produces

```
src/carve/core/agents/loop.py                 # MODIFY — add a delegation hook + steerability injection point; keep the existing tool-use/retry/terminator/observer machinery
src/carve/core/agents/subagent.py             # NEW — SubagentRunner: builds a scoped AgentLoop (system prompt + tool set + permission mode + context bundle), runs it, captures a structured result
src/carve/core/agents/delegate.py             # NEW — the `delegate` tool: delegate(agent, task, context) -> DelegationResult; spawns a SubagentRunner; returns a summary, not the transcript
src/carve/core/agents/tools/edit.py           # NEW — precise string-replace edit (read-before-edit invariant; unique-match or replace_all)
src/carve/core/agents/tools/bash.py           # NEW — permissioned, sandboxed shell: allowlist eval, timeout, cwd-pinned, captured stdout/stderr/exit
src/carve/core/agents/tools/search.py         # NEW — glob (path globs) + grep (ripgrep-style content search), bounded result counts
src/carve/core/agents/tools/web.py            # NEW — web_fetch (URL -> text, bounded) + web_search (query -> results); for reading dlt/dbt/API docs
src/carve/core/agents/m1_tools.py             # MODIFY — keep read_file/run_snowflake_query; fold write_file into edit (deprecate raw whole-file write for agents)
src/carve/core/agents/permissions.py          # NEW — PermissionMode enum + Allowlist + per-tool gate (checked before execution); bash sandbox policy; warehouse role selection
src/carve/core/agents/verification.py         # NEW — run_check(cmd) -> CheckResult; the generate->run->read->fix primitive (parses dlt state.json / dbt run_results.json)
src/carve/core/agents/tools/__init__.py       # MODIFY — register the new base tools alongside the Tool primitive
tests/unit/test_delegate_isolation.py         # NEW
tests/unit/test_tool_edit.py                  # NEW
tests/unit/test_tool_bash_permissions.py      # NEW
tests/unit/test_permission_modes.py           # NEW
tests/integration/test_verification_loop.py   # NEW — generate a trivial dlt/dbt artifact, run it, parse result, self-correct
docs/agent-harness.md                         # NEW — the harness model for contributors
```

## Behavior

### Subagent delegation

The main loop exposes a `delegate` tool:

```python
delegate(agent: str, task: str, context: dict) -> DelegationResult
# DelegationResult: { result_summary, files_changed: [..], outputs: {..}, cost_usd, status }
```

- `SubagentRunner` builds a **fresh `AgentLoop`** with: the agent's system prompt, its scoped tool set, its permission mode, and a **context bundle** (the only context it sees — not the parent's transcript). It runs to completion (its own `max_turns`) and returns a **summary**, not the full transcript.
- **Context isolation** is the point: a subagent can burn 50 tool calls engineering a connector while the orchestrator's context stays small. This supersedes the manual "pre-scoped context" pattern — the orchestrator delegates a *task*, the subagent gathers what it needs within its own window.
- Subagents may themselves delegate (e.g. recovery → DLT engineer), one level deep by default (configurable cap to bound fan-out).
- Cost + tokens roll up to the parent invocation's telemetry (`agent_invocations`).

### Terminal-grade tools

| Tool | Contract |
|---|---|
| `edit` | **Read-before-edit invariant** (the file must have been read this session). Exact string replacement; fails on non-unique match unless `replace_all`. Returns the applied diff. Replaces raw `write_file` for agents (better diffs, fewer clobbers). |
| `bash` | Runs a shell command **only if the permission gate allows it** (below). Sandboxed (OS-level for v0.1), `cwd` pinned to the project/component root, bounded timeout, captured stdout/stderr/exit. This is how agents run `dlt pipeline run`, `dbt build`, `git`, `gh`, `sqlfluff`, etc. |
| `glob` | Path-glob the repo (`el/**/*.py`, `models/**/*.sql`); bounded count. |
| `grep` | Content search (ripgrep-style), bounded matches, returns file:line + excerpt. |
| `web_fetch` | URL → readable text, size-bounded. For reading dlt/dbt/source-API docs live. |
| `web_search` | Query → ranked results. Bounded. |

Domain skills (schema introspection, the connector library, lineage, dbt manifest) layer on top via the skill registry (spec 16); they are not duplicated here.

### Permission system

Every tool call passes a **pre-execution gate** (`permissions.py`), mirroring Claude Code's permission check:

- **Modes:** `read_only` | `plan` | `build` | `deploy`. The active mode is set by the verb (`ask`→`read_only`, `plan`→`plan`, `build`→`build`, `deploy`→`deploy`) or by an interactive chat session.
- **Per-mode policy:** which tools are allowed, which paths are writable, and which bash commands run vs prompt vs deny.
  - `read_only`: `read_file`/`grep`/`glob`/`web_*`/read-only SQL only. No `edit`, no writing bash, no warehouse writes.
  - `plan`: read + design; no `edit`, no destructive bash.
  - `build`: `edit`/`bash` within the agent's `allowed_paths`; auto-allow `dbt build`/`dlt pipeline run`/read queries; **prompt** on `DROP`/DDL, `git push`, writes outside `allowed_paths`.
  - `deploy`: + `git`/`gh` (the linked-PR flow, spec 14).
- **Allowlist eval:** a command/path is matched against the mode's allow / prompt / deny lists. Unknown → prompt (interactive) or deny (headless/CI) — fail-closed.
- **Role-scoped warehouse access:** explore/qa/read queries run on a **read role**; writes (DDL, loads) only via the **deploy/runtime role** (extends Carve's existing deploy-vs-runtime role model). The SQL tool layer (its own spec) selects the role from the mode.

### Verification loop

`verification.run_check(cmd, *, parse) -> CheckResult` is the generate→run→read→fix primitive: an agent authors code, then runs a check via `bash` (`dlt pipeline run --pipeline x`, `dbt build --select y`), and the harness **parses the real result** (dlt `state.json`, dbt `run_results.json`) into a structured `CheckResult` the agent reads and acts on. The agent iterates until green (bounded attempts). This is the accuracy/magic primitive — the agent closes the loop on execution, not generation.

### Steerability + context management

- **Steerability:** in chat-driven mode, user guidance injected mid-loop is appended to the next turn's context (the loop checks a steering queue between turns). Batch (`plan`/`build`) mode is non-interactive.
- **Context management:** subagent isolation (above) is the primary mechanism; long interactive sessions compact (summarize prior turns) when approaching the context window. The loop exposes a compaction hook; the policy is conservative (summarize oldest, keep the system prompt + recent turns + the active task).

## Tests

- **Unit (delegation):** `delegate(...)` runs a subagent with a *fresh* context (asserts the parent transcript is not visible to the subagent), returns a summary + `files_changed`, and rolls cost up to the parent.
- **Unit (edit):** rejects an edit to a not-yet-read file; does exact string replace; fails on non-unique match without `replace_all`; returns the diff.
- **Unit (bash permissions):** an allowlisted command runs; a `DROP TABLE` / `git push` is gated (prompt interactive, deny headless); commands run cwd-pinned with a timeout; output captured.
- **Unit (permission modes):** `read_only` blocks `edit` and warehouse writes; `build` allows `edit` within `allowed_paths` but prompts outside; mode is set correctly per verb.
- **Integration (verification loop):** an agent authors a trivial dlt pipeline, runs it via `bash`, the harness parses `state.json` into a `CheckResult`, and a deliberately-broken artifact triggers a self-correction iteration.

## Acceptance

- A subagent delegated a task runs in isolation and returns a summary; the orchestrator's context does not grow by the subagent's transcript.
- Agents edit via string-replace (read-before-edit enforced) and run real CLIs via sandboxed, allowlisted `bash`.
- The permission gate blocks/those-prompts the right actions per mode; no write occurs in `read_only`; warehouse writes use the write role only.
- An agent can author → run (`dlt`/`dbt`) → read the parsed result → fix, end-to-end, against fixture infrastructure.
- The existing `loop.py` machinery (tool-use, retries, terminator tool, observer, token accounting) is preserved.

## Design notes

- **Why subagents (vs the current hardcoded dispatch)?** Context isolation + true specialization fall out of one mechanism — and it's exactly how Claude Code achieves multi-agent work. The orchestrator delegates a *task* and gets a *summary*; it never drowns in a specialist's transcript. This is the single highest-leverage move in the harness.
- **Why terminal-grade tools (vs narrow domain ops)?** dlt and dbt *are* CLIs; a data engineer works at a terminal. `bash` + `edit` let agents use the *real* tools the way a human does (run, inspect outputs, `git diff`, `gh pr create`) — far more capable than a fixed set of Python wrappers, and the source of the "it actually works" feeling.
- **Why permission-before-execution?** Once agents run bash + touch the warehouse + push git, a pre-execution gate is the only safe design. It's also the trust story: powerful but never free to do arbitrary things. Maps cleanly onto Carve's plan→build→deploy lifecycle.
- **Why verify by execution?** Generation-without-verification is a demo. Running `dlt`/`dbt` and reading the real result is what makes the agent accurate and self-correcting — and grounds it so it can't ship a hallucinated schema.
- **Sync vs async.** `loop.py` is currently synchronous; `carve serve` is async. The harness runs subagents potentially concurrently (a review fan-out), so the loop should be made awaitable (or run in a worker thread/executor). Pinned as an open question.
- **Keep `loop.py`.** This spec *extends* the proven loop (retries, terminator-tool pattern, observer, token accounting), it does not rewrite it.

## Open questions

- **Sandbox depth.** *Strategy-required.* OS-level sandbox + allowlist for v0.1; container/microVM isolation is a hosted concern. Confirm the v0.1 floor (e.g., restricted subprocess env + path/network limits vs. a full sandbox).
- **Sync → async loop.** *Implementation default.* Make the loop awaitable; run subagents on the event loop (or a bounded executor) so a review fan-out can be concurrent. Confirm before build.
- **Steering in batch mode.** *Implementation default.* Batch (`plan`/`build`) is non-interactive; steering is chat-mode only. Revisit if CI flows want mid-run injection.
- **Delegation depth cap.** *Implementation default.* One level by default (orchestrator → specialist; recovery → specialist is the exception, two levels). Bound to prevent runaway fan-out.
