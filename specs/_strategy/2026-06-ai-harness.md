# 2026-06 — The AI harness: a Claude-Code-style agentic engine for data work

> **Status:** Decided 2026-06-16 (Nate). Foundational. Builds on [`2026-06-control-plane.md`](./2026-06-control-plane.md) and refines [`2026-05-positioning.md`](./2026-05-positioning.md). Captures Carve's AI-layer direction; the per-spec plan is at the end. Concrete shapes here are enough to spec against; a fuller reference model is the first spec-out step.

## The decision

Carve's AI layer is a **Claude-Code-style agentic harness, specialized for data engineering**: a main agentic loop that **delegates to domain subagents**, armed with **terminal-grade tools** (file edit, bash, search, web) plus domain skills, running behind a **permission system**, that **verifies its work by executing it** — and is **fully extensible** (bring your own agents, skills, MCP servers, CLIs, hooks). The experience target: *magical* — super smart, accurate (grounded in real tool output), thorough (context-isolated), detail-oriented, and specialized within dlt/dbt/SQL/the warehouse.

This replaces the current shipped reality (a capable reasoning loop + ~5 narrow tools + hardcoded agent dispatch) with the harness model, and resolves three known gaps: the **built-vs-spec agent drift**, the **orphaned recovery agent**, and the **thin skill catalog**.

## The three unlocks (the magic)

1. **Specialists are *subagents*, not a hardcoded dispatch.** The orchestrator is the main loop; it delegates via a `delegate`/Task tool to typed subagents, each a fresh loop with its own context window, tool set, and system prompt, returning a *summary*. Multi-agent specialization **and** context management fall out of this one move.
2. **Terminal-grade tools, not a keyhole.** dlt and dbt *are* CLIs; a data engineer lives at a terminal. The base tool layer is precise **file edit** (string-replace), **permissioned bash** (run the real `dlt`/`dbt`/`git`/`gh`/warehouse CLIs), **glob/grep**, **web fetch/search** — *then* domain skills on top.
3. **Close the loop with execution.** Generate → **run** (`dlt pipeline run`, `dbt build`/`test`) → read the real result → fix, until green. Generation-without-verification is a demo; generate-run-read-fix is a colleague. Also the accuracy story: never let the model invent what a tool can verify.

## The harness (four pillars)

- **Agentic loop** — receive context → decide tool calls → harness executes → repeat until done. Carve's `src/carve/core/agents/loop.py` is already solid; add **subagent delegation** and **steerability** (inject guidance mid-task in chat mode). *(Confirm the loop composes with the async runtime — it currently appears synchronous.)*
- **Tool layer** — terminal-grade base (`edit`, `bash`, `glob`, `grep`, `web_fetch`, `web_search`) + domain skills + extensibility (MCP, subagents, hooks, skills).
- **Permission system** — modes / allowlists / sandbox, mapped onto Carve's plan→build→deploy lifecycle and onto role-scoped warehouse access. Mandatory once agents run bash + touch the warehouse + push git.
- **Context management** — subagent context-isolation (a deep read returns a summary) + compaction for long chat sessions. (Supersedes the manual "pre-scoped context" pattern.)

## The agent taxonomy

The orchestrator is the **main loop**; everything else is a delegated subagent. Per domain, **engineer = architect + build** (a good engineer architects as it builds); **QA and security are separate *review* subagents** (fresh, adversarial context) — i.e. Carve brings the `/build-spec` engineer→parallel-reviewers→fix pattern it uses on *itself* to users' pipelines.

| Subagent | Role | Notes |
|---|---|---|
| **Orchestrator** | classify + decompose goal, delegate, synthesize into a reviewable plan/diff | main loop; doesn't do deep work itself |
| **DLT engineer** | author + run dlt sources/pipelines into a named component | connector skill-library + the customer repo for context; verifies via `dlt pipeline run` |
| **DLT qa / security** (review) | adversarial review of the dlt diff | schema-contract, credential handling, data-loss modes |
| **DBT engineer** (**v0.2**) | author + run dbt models/tests/sources | dlt + dbt repo context; verifies via `dbt build`/`test` |
| **DBT qa** (review, v0.2) | test/coverage/convention review | |
| **Pipeline engineer** | compose components **by name** into `pipelines/<name>.toml` | the control-plane runtime specialist (spec 08) |
| **Recovery engineer** | diagnose a failure (grounded: dlt exception classes, schema diff, run logs), then **delegate the fix** to the DLT/DBT/SQL engineer | the meta-agent that resolves the orphaned recovery POC; drops the dead `el-deploy` invocation contexts |
| **Explorer** | read-only Q&A: how/where/why, lineage, logic, definitions, tests, "where does this data come from" | the `ask` verb (spec 12), elevated; citation-backed |

**SQL is a cross-cutting capability, not a silo:** a **dialect-aware tool layer** every subagent uses (snowflake / duckdb / postgres / bigquery / databricks / sqlserver) — `sqlglot` for transpile/validate, per-dialect `INFORMATION_SCHEMA` introspection, permission-gated execution (read vs write, DDL prompts) — plus a thin **SQL specialist** for "explain / write / modify this query." **Connect/onboarding** (the `carve connect` first-magical-moment) is a capability the orchestrator wields, not a standing agent.

## Extensibility — "bring your own," declarative + discoverable

This *is* the answer to the built-vs-spec agent drift: make everything declarative, exactly like Claude Code.

- **Agents = markdown with frontmatter** (`carve/agents/<name>.md`: `name`, `description`, `model`, `tools`, `allowed_paths`, `classifications`). Drop a file → it's a routable subagent. Hot-reload; `carve agents create`.
- **Skills = capability packs** (`carve/skills/<name>/SKILL.md` + optional scripts/resources; progressive-disclosure, loaded on description-match). **The curated connector library becomes a skill library** — connectors, skills, and bring-your-own unify into one model.
- **Hooks** (`pre/post tool`, `on run.failed`, `pre-deploy`) — inject policy/automation without forking (`sqlfluff` before committing dbt; block writes to prod schema; Slack on deploy).
- **MCP both directions** — agents *consume* the user's MCP servers; Carve *exposes* one (Claude Desktop / Cursor drive Carve).
- **CLIs** — "bring your CLI" = allow it in the bash allowlist.

## The trust layer (magical *and* safe/accurate)

- **Permission modes** mapped to the lifecycle: *explorer/ask* = read-only; *plan* = no writes; *build* = writes to allowed component paths; *deploy* = git/PR. Plus allowlists (auto-allow `dbt build`/read queries; prompt on `DROP`/DDL, `git push`, `gh pr create`, out-of-scope writes) and **sandboxed bash**.
- **Role-scoped warehouse access** — explore/qa on a *read* role; writes only via the deploy/runtime role (extends Carve's existing deploy-vs-runtime-role model).
- **Grounding for accuracy** — deterministic code for the mechanical (introspection, lineage, `sqlglot` validation, DAG exec); the LLM reasons + authors, never invents a schema a tool can return.

## How it reconciles with the control-plane model

- Subagents resolve component code via the **component locator** (spec 03, name → path @ pinned ref).
- Permission modes line up with **plan → build → deploy** (and the linked-PR cross-repo deploy).
- Recovery's delegated fixes flow through the same **plan/build/PR** path — no autonomous writes to prod survives.
- The schedule stays **data**; agents change it via `carve schedule`, not by editing code.

## Concrete shapes (enough to spec against)

```
# carve/agents/dlt-engineer.md
---
name: dlt-engineer
description: Authors and runs dlt sources/pipelines into a named dlt component. Use for ingest/extract-load goals.
model: claude-sonnet            # per-agent model tiering (haiku classify / sonnet build / opus hard)
tools: [edit, bash, grep, glob, web_fetch, dlt_library, schema_introspect, sql]
allowed_paths: ["el/**", ".dlt/*.template"]
classifications: [new_pipeline, modify_pipeline, refactor_to_incremental]
---
<system prompt body…>
```

```
# carve/skills/stripe/SKILL.md   (a capability pack; the connector library IS the skill library)
---
name: stripe
description: Curated dlt source for the Stripe API. Use when ingesting Stripe.
---
<instructions + reference to the dlt source + validation glue>
```

```
# carve/hooks.toml
[[hook]]
on = "pre-commit"; run = "sqlfluff lint --dialect snowflake {changed_sql}"
[[hook]]
on = "run.failed"; run = "notify-slack"
```

- **Delegation tool:** `delegate(agent: str, task: str, context: {...}) -> {result, files_changed, summary, cost}` — spawns the subagent loop; returns a summary, not the transcript.
- **Permission modes (enum):** `read_only | plan | build | deploy`, each with an allowlist + a bash sandbox policy; the active mode is set by the verb (`ask`→read_only, `plan`→plan, …) or by the chat-driven session.
- **Base tools:** `edit` (string-replace), `bash` (allowlisted, sandboxed), `glob`, `grep`, `web_fetch`, `web_search`.

## v0.1 cut (staged — the full vision ships over v0.1 → v0.2)

- **Harness (v0.1, foundation):** subagent delegation + terminal toolset (`edit`/`bash`/`grep`/`web`) + permission modes/allowlists/sandbox + the verification loop. Build this first.
- **Agents (v0.1, declarative):** orchestrator, DLT engineer, pipeline engineer, explorer (`ask`), recovery engineer (delegating). QA/security review subagents phased in (start: security-on-deploy, qa-on-build).
- **DBT engineer:** **v0.2** (matches the existing dbt-authoring deferral).
- **Extensibility (v0.1):** declarative agents + skills + MCP-consume. Hooks + MCP-expose close behind.
- **SQL (v0.1):** dialect-aware tool layer (Snowflake first; others via `sqlglot`) + thin specialist.
- **Per-agent model tiering (v0.1):** pricing support already exists; wire selection.

## Spec plan (the spec-out)

New specs (numbering provisional; some v0.1, some v0.2):

1. **Agent harness** — the subagent loop, delegation tool, terminal tool layer, permission modes/allowlists/sandbox, the verification loop. (Foundational; everything else builds on it. Revises the M1 loop rather than replacing `loop.py`.)
2. **Extensibility** — declarative agents (`carve/agents/*.md`) + skills (`SKILL.md` packs, incl. the connector→skill library) + hooks + MCP (both directions) + `carve agents/skills` CLI.
3. **Recovery engineer** — diagnose-then-delegate; reconcile the orphaned POC; drop the dead deploy invocation contexts; the `Investigation` entity (control-plane-era model, UC4/UC5). *(The v0.1 recovery decision the AI-map flagged.)*
4. **SQL dialect-aware layer** — `sqlglot`-backed transpile/validate, per-dialect introspection, permission-gated exec, thin SQL specialist.

Revisions:

5. **spec 04** (EL agent → DLT engineer subagent + qa/security reviewers + terminal tools + verification).
6. **spec 08** (runtime specialist → pipeline engineer, already control-plane-revised; align to the subagent/tool model).
7. **spec 12** (ask → explorer; the read-only mode of the harness).
8. **ARCHITECTURE §5 (agents) + §6 (skills)** — reconcile to the subagent + terminal-tool + declarative-extensibility model.

## Open questions

- **Interactive (Claude-Code-style chat) vs batch (plan/build/PR) modes** — both ship; the chat mode is the magical experience, the batch mode the audited/CI path. Pin how the permission model spans both.
- **Sync vs async loop** — confirm `loop.py` composes with the async `carve serve` runtime (subagents may want concurrency).
- **Review fan-out scope for v0.1** — which reviewers (security/qa/dbt/snowflake) ship when; full fan-out vs a staged start.
- **Sandboxing depth for bash** — OS sandbox vs container vs allowlist-only for v0.1.
