# Agent harness: subagent delegation, terminal-grade tools, permissions, verification

> **Foundation spec** — the Claude-Code-style agentic engine the domain agents (04 DLT, 08 pipeline, 12 explorer, recovery, SQL) run on. Per [`../_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md). Read after [state-store](./state-store.md); it underpins 04/08/12 (numbered after them only for bookkeeping — placement reorg is a noted follow-up).
>
> **Hardened per the 15/16 adversarial review (2026-06-16).** Decisions: **(1) execution is sequential + sync** — `loop.py` stays synchronous (invoked from the async `carve serve` via a threadpool); `delegate` is sync; subagents and review run sequentially; concurrent fan-out is a later increment. **(2) The advanced Claude-Code primitives ship** — interrupt/cancel, a TODO list, context compaction, and `pre_deploy`/`post_build` hooks.

## Status

- **Status:** Drafting
- **Depends on:** M1 agent loop (`src/carve/core/agents/loop.py` — preserved + extended, HISTORICAL; stays sync), [layout](./layout.md) (the component locator subagents resolve against), the M1 subprocess runner (`src/carve/core/runners/local_venv.py` — its env-scrub + cwd-pin + timeout + capture pattern is the `bash`-sandbox floor).
- **Blocks:** [extensibility](./extensibility.md), and the agent revisions/new agents (04, 08, 12, recovery, SQL).

## Goal

Evolve Carve's agent loop into a **Claude-Code-style harness**: a main loop that **delegates to subagents**, armed with **terminal-grade tools**, behind a **permission system**, that **verifies its work by executing it**. The harness ships:

1. **Subagent delegation** — a sync `delegate` tool spawns a typed subagent (fresh loop, own context/tools/prompt) and returns a *structured summary*. Subagents run **sequentially**.
2. **Terminal-grade tools** — `edit`/`create_file`, `bash` (gated + sandboxed; runs the real `dlt`/`dbt`/`git`/`gh`/warehouse CLIs), `glob`/`grep`, `web_fetch`/`web_search`.
3. **Permission system** — modes (`read_only`/`plan`/`build`/`deploy`) + allowlists + bash sandbox, checked at a single **pre-execution gate**; clamped on delegation; plus role-scoped warehouse access.
4. **Verification loop** — bounded generate → run → read → fix.
5. **Interrupt/cancel, a TODO list, and context compaction.**

This spec ships the harness mechanics. The declarative agent/skill *format* is [extensibility](./extensibility.md); the domain agents are their own specs.

## Out of scope

- The **declarative agent/skill format**, hooks config, MCP import — [extensibility](./extensibility.md). (This spec defines the gate/hook *fire-points* and the runtime grant rule; 16 defines the file format.)
- The **specific domain agents** — their own specs.
- **Hosted sandboxing** (gVisor/Firecracker/per-tenant) — hosted; this spec ships the OS-level/allowlist floor below.
- **Concurrent subagent execution** — a later increment (execution is sequential).
- **Format-specific check parsing** (`state.json`/`run_results.json`) — the format owners (04 dlt, 08 dbt) provide the parse callable; this spec's `run_check` is format-agnostic.

## Behavior

### Subagent delegation (sync, sequential)

```python
def delegate(agent: str, task: str, context: dict, *, parent_mode: PermissionMode) -> DelegationResult: ...

@dataclass
class DelegationResult:
    status: Literal["succeeded", "needs_user_input", "failed"]
    result_summary: str                 # the subagent's final text
    files_changed: list[str]            # harness-tracked from the run's edit/create_file log (never self-reported)
    outputs: dict                       # validated payload from the agent's `submit_result` terminator (schema per-agent)
    usage: TokenUsage                   # input/output/cache tokens (mirrors the shipped TokenUsage)
    cost_usd: float

class SubagentError(AgentError): ...    # subclass of the shipped AgentError
```

- `SubagentRunner` builds a **fresh sync `AgentLoop`** with: the agent's system prompt, its **tool set ∩ the mode's permitted tools** (see *Permissions*), the **clamped** permission mode, and a typed **context bundle** (named keys the agent reads — never the parent transcript). It runs to completion (own `max_turns`) and returns a `DelegationResult`.
- **`outputs` is produced via a structured terminator tool** (`submit_result`, reusing the shipped `SubmitPlanCapture`/`SubmitDiagnosisCapture` pattern); **`files_changed` is harness-tracked** (the SubagentRunner reads the run's edit/create_file log — agents can't fabricate it).
- **Context isolation**: the orchestrator's context doesn't grow by the subagent's transcript; a subagent's raw tool output (which may include sensitive strings) never flows back to the parent.
- **Execution is sequential and sync**: `delegate` blocks; the loop stays synchronous and is invoked from the async `carve serve`/REST layer via a threadpool. Concurrent fan-out is a later increment.
- **Delegation graph**: the **orchestrator** owns review fan-out — it runs an engineer, then (sequentially) runs the qa/security **reviewers as sibling subagents** and feeds findings back. A **domain engineer does not call `delegate`** (reviewers are spawned by the orchestrator). `max_delegation_depth = 2` (orchestrator → engineer; orchestrator → reviewer; recovery → engineer all stay ≤ 2).

### Terminal-grade tools

| Tool | Contract |
|---|---|
| `edit` | **Re-reads the file at apply time** and verifies `old_string` still matches the on-disk bytes before writing (closes the read-at-turn-2/edit-at-turn-20 TOCTOU). Resolves symlinks; enforces project-root containment + the agent's `allowed_paths` (reusing the shipped write-tool guards). Exact string replace; non-unique match fails unless `replace_all` (which reports the count). |
| `create_file` | Creates a **new** file (edit cannot string-replace into a nonexistent file). Same path-scoping/allowed_paths as `edit`. (Replaces raw `write_file` for agents.) |
| `bash` | Runs a shell command **only if the gate allows it** (below). **Scrubbed env** (strips `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` + provider tokens — the shipped `local_venv._STRIPPED_ENV_VARS` set; **warehouse creds are never in the bash env** — agents reach the warehouse via the role-scoped `sql` tool, [spec 18](./sql.md)). cwd-pinned, bounded timeout, **captured + capped** stdout/stderr (capped before it enters the transcript/telemetry). |
| `glob`/`grep` | File search, bounded counts; **secret paths deny-listed** (`.env`, `.env.*`, `**/secrets.toml`, `*.pem`, `~/.dlt/secrets.toml`) — returns a tool_error in **all** modes. |
| `read_file` | (shipped, MODIFY) same secret-path deny-list. |
| `web_fetch`/`web_search` | Bounded; for reading dlt/dbt/source-API docs. |
| `todo` | A TodoWrite-style task list the agent maintains to stay on-rails across long runs. |

### Permission system

A single **pre-execution gate** (`permissions.py`) runs in the loop's `_execute_tool_calls` **before** `tool.executor` (added to the `loop.py` MODIFY). Order: **gate first** (deny fast) → then `pre_tool` hooks (spec 16) on allowed calls → execute → `post_tool` hooks.

- **Modes** (a lattice `read_only < plan < build < deploy`). Set by the verb (`ask`→`read_only`, `plan`→`plan`, `build`→`build`, `deploy`→`deploy`) or a chat session.
- **Config:** per-mode policy ships as **hardcoded Python defaults** (the authoritative floor); an optional `[permissions]` block in `runtime.toml` can *tighten* (never widen) bash allow/prompt/deny + tool sets; per-agent `allowed_paths` (spec 16 frontmatter) further narrows writes. Precedence: effective = mode-default ∩ config ∩ agent.
- **Grants are attenuation, not escalation.** An agent's `tools:` grant is intersected with the mode's permitted set: **runtime tool set = grant ∩ mode-permitted**. The runtime gate is the authoritative boundary — write tools (`edit`/`create_file`/`bash`-writes/warehouse-writes) are denied in `read_only`/`plan` **regardless of grant**. A user agent file that overrides a built-in **cannot raise the effective mode or escape `allowed_paths`** (those are clamped by the verb + gate, not the file). Spec 16's load-time grant check is an **advisory lint**, not the boundary.
- **Delegation clamp:** `delegate` carries `parent_mode`; the child runs at `min(parent_mode, agent_capability)` and **never wider**. (So a `read_only` `ask` delegating to a build-capable engineer runs the child `read_only` — no write during an ask, satisfying [spec 12](./ask.md).)
- **The bash gate** (the load-bearing surface): a command is `shlex`-parsed; any command containing sub-execution metacharacters — `$()`, backtick, `;`, `&&`, `||`, `|`, `>`/`>>`, `&`, newline — is **denied** (not prompted) unless the whole command matches a structured allow-entry; otherwise the parsed `argv[0]` (+ subcommand for `git`/`dbt`/`dlt`/`gh`) is matched against the mode's allow/prompt/deny lists.

  > **Updated during implementation (2026-06-19):** the gate shipped more specific than "metacharacter-deny + argv-allowlist" — three deny-by-default refinements were added during the build. The *intent* (the gate is the airtight surface, deny-by-default, allowlist not denylist) is unchanged; the mechanism below is what shipped (`permissions/bash_gate.py` + the floor sets in `permissions/policy.py`).

  Concretely, beyond the metacharacter screen + per-mode argv/subcommand allowlists:
  - **An `_ALWAYS_DENY` set** (denied in *every* mode, deny wins over allow) covers shell interpreters / re-entry (`sh`/`bash`/`eval`/`exec`/`source`), privilege escalation (`sudo`/`su`), language interpreters (`python`/`node`/`perl`/…), package managers (`pip`/`uv`/`npm`/`cargo`/…), network-egress tools (`curl`/`wget`/`ssh`/`scp`/`nc`/…), exec-multiplexers / env-rewriters that run an arbitrary program through their own argv (`env`/`xargs`/`find`/`awk`/`sed`/`timeout`/`tee`/…), and destructive/privileged FS primitives (`rm`/`mv`/`cp`/`ln`/`dd`/`chmod`/…). These are denied *explicitly* (not merely omitted) so a future allow-edit cannot silently re-admit ACE/egress.
  - **A `DANGEROUS_BASH_FLAGS` guard** denies an otherwise-allowlisted invocation of the multi-purpose tools if any argv token (scanned across the whole argv, since a global flag can precede the subcommand) is a config-injection / exec / arbitrary-path flag: `git -c`/`--config-env`/`--exec-path`/`--upload-pack`/`--receive-pack`/`--ext-diff`/`--no-index`/`-C`/`-o`/`--git-dir`/`--work-tree`; `dbt`/`dlt --project-dir`/`--profiles-dir`/`--profile`/`--log-path`/`--target-path`.
  - **The path-taking coreutils were removed** from the read allow-set (`cat`/`head`/`tail`/`wc`/`ls`/`sort`/`uniq`/`test`/`[`): an absolute-path argument escapes the cwd-pin (cwd is not a chroot), so these could read arbitrary file contents (`cat .env`, `cat /abs/.aws/credentials`) or write arbitrary paths. File reads/listing/search go through the confined `read_file`/`glob`/`grep` (secret deny-list + project-root + symlink containment). What remains in the read allow-set is exactly (a) flagless **and** path-less builtins (`echo`/`pwd`/`date`/`true`/`false`/`which`/`printenv`) and (b) the flag-guarded, project-scoped `git`/`dbt`/`dlt` read subcommands.
  - **`gh` is deploy-tier-only** (it is PR-creation / network): `gh pr create`/`gh pr merge` sit in the deploy `prompt` set, not in any read/build allow-set.

  **Sandbox floor:** argv-allowlist + metacharacter-deny + `_ALWAYS_DENY` + `DANGEROUS_BASH_FLAGS` + the shipped `local_venv` restricted-env subprocess (scrubbed env, cwd-pinned, timeout, process-group kill) + filesystem read-write only under the project root + the tool caches (`~/.dlt`, `~/.dbt`); read-only elsewhere. *(This resolves the ADR's bash-sandbox open question to a concrete floor.)*
- **Role-scoped warehouse:** read queries on the **read role**; writes/DDL only on the **deploy/runtime role** (extends the shipped deploy-vs-runtime role model). The `sql` tool selects the role from the mode.
- **Non-interactive = fail-closed:** any invocation without an attached interactive approver (`carve serve`, REST, MCP, CI) resolves **every `prompt`-tier outcome to DENY**, returning a `needs_user_input` status (surfaced on the Plan), never auto-allow. The prompt tier requires a registered approver callback; absent one, prompt == deny.

### Verification loop (bounded)

`run_check(cmd, *, parse: Callable[[CompletedProcess], CheckResult]) -> CheckResult` is **format-agnostic**: it runs `cmd` **through the gated `bash` tool** (same allowlist/scrubbed-env/sandbox — no second execution path) and applies the injected `parse` callable. The format-specific parsers (dlt `state.json`, dbt `run_results.json`) live in the **format owners** (04/08), not here. The agent iterates generate → run → read → fix, bounded by **`max_verification_iterations` (default 4)** and a **per-invocation token/cost ceiling**; on exhaustion it returns `status="needs_user_input"` with the last `CheckResult` rather than looping. Subagent costs aggregate against the parent's ceiling.

### Interrupt/cancel, TODO, compaction

- **Interrupt/cancel:** the loop checks a cancellation signal (`cancel.py`) **between turns**; a user/API cancel sets it, the loop stops cleanly and emits `run.cancelled` (spec 09). In-flight subprocesses get the process-group SIGTERM→SIGKILL the shipped runner already does.
- **TODO list:** the `todo` tool lets an agent track a multi-step task; surfaced in the static UI / stream.
- **Compaction:** applies to the **top-level interactive chat loop only** (subagents are bounded by `max_turns` and don't compact). Trigger: context exceeds a token threshold (default ~75% of the window); policy: summarize oldest turns, keep the system prompt + the active task + recent turns.

### Steerability

In chat mode, guidance injected mid-task is appended to the next turn (the loop checks a steering queue between turns). Batch (`plan`/`build`) is non-interactive.

## Tests

- **Unit (delegate clamp + isolation):** a `read_only` parent delegating to a build-capable agent runs the child `read_only`; a child `edit`/`bash`-write is denied; the parent transcript is not visible to the child; cost/usage roll up.
- **Unit (bash gate):** an allowlisted `git commit` carrying `$(...)`/`;`/backticks is **denied**; an un-allowlisted `argv[0]` is denied; `printenv ANTHROPIC_API_KEY` returns empty (scrubbed env); a non-allowlisted cmd passed to `run_check` is denied.
- **Unit (secret reads):** `read_file('.env')` / `grep` over `**/secrets.toml` are denied in **all** modes (incl. `read_only`).
- **Unit (permission modes + attenuation):** `read_only` blocks `edit`/warehouse-writes; an agent granting `bash` runs with `bash ∩ mode` (no-op in `read_only`); a `prompt`-tier action under a non-interactive invocation → DENY + `needs_user_input`.
- **Unit (edit):** rejects edit to a not-read file; re-reads at apply (TOCTOU); `create_file` makes a new file under `allowed_paths`; outside-`allowed_paths`/symlink-escape denied; `replace_all` reports the count.
- **Integration (verification, no dlt/dbt):** an agent runs a trivial command that writes known JSON, `run_check` parses it via a fixture `parse` fn, a broken artifact triggers a bounded self-correction, and exhausting `max_verification_iterations` returns `needs_user_input`.

## Acceptance

- A subagent delegated a task runs in isolation, **at a mode ≤ the parent's**, and returns a `DelegationResult` with harness-tracked `files_changed` + `usage`; the orchestrator's context doesn't grow by the transcript.
- The `bash` gate denies metacharacter/sub-shell injection and un-allowlisted commands; `bash` cannot read secrets from env or files; the warehouse is reachable only via the role-scoped `sql` tool.

  > **Updated during implementation (2026-06-19):** in this increment the "warehouse only via the `sql` tool" clause is satisfied **negatively** — the bash gate has no warehouse CLI (`psql`/`snowsql`/…) in any allow set (they fall through to deny-by-default) and warehouse creds are scrubbed from the bash env, so no bash path to the warehouse exists. The *positive* role-scoped `sql` tool path (read role vs deploy/runtime role selection) lands in Increment 2 ([sql](./sql.md)); a pre-existing M1 read-only `run_snowflake_query` tool is the only warehouse reach today. Re-confirm this bullet when the `sql` tool ships.
- No write occurs in `read_only`; grants never escalate; a user agent override can't raise the mode or escape `allowed_paths`.
- The verification loop is bounded (iterations + cost) and runs exclusively through the gated `bash` tool.
- Interrupt/cancel stops an in-flight agent and emits `run.cancelled`; compaction keeps a long chat within budget.
- The existing `loop.py` machinery (tool-use, retries, terminator tool, observer, token accounting) is preserved and stays sync.

## Design notes

- **Why sequential + sync?** It keeps `loop.py`'s proven synchronous design (invoked from the async serve via a threadpool), matches spec 07's "no fan-out" and recovery's sync delegate, and removes the only feature (concurrent review) that forced async. Concurrent fan-out is a clean later add.
- **Why the gate is the boundary (not the grant)?** Grants live in editable agent files (a user file overrides built-ins), so they can't be a security boundary. The single runtime pre-execution gate, with grants attenuated to the mode, is the airtight surface — and it's why spec 12 can retire its bespoke `NoWriteSkillsGuardrail`.
- **Why the bash metacharacter-deny + argv-allowlist?** Because `bash` invokes a shell, a prefix-glob over the raw string is ACE-by-construction. The shipped `m1_tools._is_safe_select` learned this for SQL; `bash` adopts the same discipline. The sandbox floor reuses the shipped `local_venv` subprocess sandbox so this isn't invented from scratch.
- **Why scrub the env + deny secret reads?** The shipped `local_venv` already strips Carve secrets from LLM-authored subprocess env; the harness must not regress that, and read-tool secret-deny stops even a `read_only` explorer leaking `.env` into an answer.
- **Why harness-track `files_changed` + terminator `outputs`?** So the most-consumed delegation field can't be fabricated by the model, reusing the shipped `SubmitPlanCapture` pattern.

## Open questions

- **Concurrent fan-out (a later increment).** When it lands, make the loop awaitable / run subagents on a bounded executor. Execution is sequential for now.
- **`[permissions]` config surface depth.** *Implementation default.* Ships hardcoded per-mode defaults + a tighten-only `runtime.toml` block; richer policy is deferred (sequenced in DELIVERY).
- **Compaction quality.** *Implementation default.* Simple oldest-summarize at a token threshold; smarter compaction later.

### Follow-ups (deferred, non-blocking — from the Increment 1 security review)

- **Grow the secret-path deny-list.** `secrets_denylist.py` currently denies `.env`/`.env.*`/`**/secrets.toml`/`*.pem`/`~/.dlt/secrets.toml`. Add `id_rsa*`, `.netrc`, `.pgpass`, `**/profiles.yml` (dbt creds), and `.dlt/credentials` — all in-tree credential exposure. Sequence with the `sql`/connect work (Increment 2), where the warehouse-credential surface lands.
- **Unicode-normalize the secret-name compare.** `_normalize_name` in `secrets_denylist.py` casefolds and trims only ASCII dot/space (`.rstrip(". ")`); a unicode-normalization / homoglyph variant of a secret filename is a latent bypass. Low-likelihood, file it against the deny-list hardening above.
- **Add dedicated unit tests for `web_fetch`/`web_search`/`todo`.** These tools ship but are only exercised indirectly (`web_fetch`/`web_search` via `test_modes_attenuation.py`; `todo` has no test). A thin-coverage gap, not a behavior gap — add focused unit tests for the bounded-fetch / task-list contracts.
