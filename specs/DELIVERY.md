# Carve — Delivery plan

**What to build, in what order, given what's already built.** This is the temporal layer ([`_strategy/2026-06-spec-structure.md`](./_strategy/2026-06-spec-structure.md)): it sequences work into dependency-ordered, foundation-first increments and carries the phase/increment identity (and the release tag at the end). The durable design lives elsewhere and is **not** organized by phase — [`PRD.md`](./PRD.md) (what/why/who), [`ARCHITECTURE.md`](./ARCHITECTURE.md) (the technical model), and the capability specs (under [`capabilities/`](./capabilities/); they describe *how a capability works*, version-independently).

This is a **living, delta-aware** document. It plans *changes and additions* to the current codebase, not greenfield builds. As increments land, update the *Current state* section and check off exit criteria; as priorities shift, re-sequence increments here — without touching the capability specs. It covers the **whole lifecycle** — the initial foundation build *and* ongoing change (bugs, enhancements, new capabilities) after it — under one structure: **Current state** (the perpetual delta baseline) → **Increments** (the initial build; becomes the build log) → **Backlog** (ongoing work that needs sequencing). How any individual change flows (the bug-vs-change rule, spec-first) is [`_strategy/2026-06-change-lifecycle.md`](./_strategy/2026-06-change-lifecycle.md).

> **Note.** Specs live in [`capabilities/<area>`](./capabilities/) (durable design); increments below reference them by capability name. `DELIVERY.md` — not `capabilities/README.md` — is the source of truth for sequencing and scope. The concrete **file manifest for each slice is not stored** — it is generated at build time (see *How a slice is built*, below).

---

## How to read an increment

Each increment is a shippable, dependency-respecting slice:

- **Goal** — the user-visible capability the increment delivers.
- **In scope** — the capability slices, each pointing at its design spec.
- **Depends on** — increments/code that must exist first.
- **Delta** — what's *new* vs. what *modifies* already-shipped code (the delta-aware part).
- **Exit criteria** — how we know it's done.

**How a slice is built.** The build (`/build-spec`'s planning stage) takes a slice = (capability spec × this increment) and **generates a *delivery spec*** at build time: it reads the spec as the design reference, inspects the **current codebase**, and emits the concrete, delta-aware file manifest (*create / modify*) plus the increment's slice of the spec's Acceptance + Tests as the bar. The manifest is computed, never stored — so it is always correct against what's already built (see [`_strategy/2026-06-spec-structure.md`](./_strategy/2026-06-spec-structure.md) → *The delivery spec*).

---

## Current state (the delta baseline)

Shipped and in `src/` — every increment plans *against* this:

- **M1 — walking skeleton.** CLI foundation (Typer), config loader, state store, the Anthropic agent loop, the Python step + `LocalVenvRunner` subprocess primitive, the Snowflake connector. The smallest end-to-end loop.
- **M1.1 — lifecycle + UX.** `carve init` templates, **API-key model auth** (`${ANTHROPIC_API_KEY}` via `models.toml`), dotenv autoload, plan progress, agent-prompt tightening, the **plan / build / run** separation, run-retry-permits-redo. (The plan/build/run separation is the shipped core of [plan-build](./capabilities/plan-build.md). The M1.1-02 *Claude-subscription OAuth* follow-up was **planned but never built** — only the API-key path of [model-auth](./capabilities/model-auth.md) shipped, with the Anthropic client constructed directly at four call sites; see Increment 1b.)
- **Spec 01 — state store → Postgres.** Landed (SQLite retired; Postgres baseline + the six audited migrations). Followups landed: the M1 test sweep and `DATABASE_URL` precedence. ~300 tests passing. **Increment 0 (state-store formalization) complete 2026-06-18** — spec reconciled to the shipped code.

- **Layout (Increment 1) — control-plane `carve.toml`.** Landed 2026-06-18: the `[components.<name>]` schema (transport-validated `url`/`ref`/`branch`), `ProjectPaths`, the component locator (`resolve_component` / `discover_components` / `workspace_dirname`), the git workspace cache (ref-pin, credential redaction, hardened env, bounded `timeout`), the provenance reader, and the `Workspace` model + migration `0007`. 879 tests passing.

- **Harness (Increment 1) — the Claude-Code-style agentic engine.** Landed 2026-06-19: sync sequential subagent `delegate` (`delegation.py`, `DelegationResult`, mode-clamp to `min(parent, capability)`, context isolation, harness-tracked `files_changed`); terminal-grade tools (`tools/fs_tools.py` edit/create_file with re-read-at-apply TOCTOU, `bash_tool.py`, `search_tools.py`, `web_tools.py`, `todo_tool.py`); the single pre-execution permission gate (`permissions/gate.py`, gate-first in `loop.py:_execute_tool_calls`, `grant ∩ mode` attenuation, fail-closed prompt) over the per-mode policy floor (`permissions/policy.py` with `_ALWAYS_DENY` + `DANGEROUS_BASH_FLAGS`) and the shared secret-path deny-list (`tools/secrets_denylist.py`); the bounded format-agnostic verification loop (`verification.py`); interrupt/cancel (`cancel.py`), steering (`steering.py`), and compaction (`compaction.py`). `loop.py` is a purely-additive MODIFY (sync preserved). Spec reconciled to the shipped code (minor inline drift only). Deferred follow-ups (non-blocking, in the harness spec's Open questions): grow the secret deny-list with the warehouse-cred surface, unicode-normalize the secret-name compare, and add dedicated `web_fetch`/`web_search`/`todo` unit tests.

- **Extensibility (Increment 1) — declarative agents, skill packs, hooks, MCP-consume.** Landed 2026-06-19: safe markdown-frontmatter agent loader (`agents/loader.py`, `yaml.safe_load`, `MAX_AGENT_FILE_BYTES=64 KiB`, fail-closed/no-partial), dispatch-time mtime-cached discovery with user-over-builtin override (`agents/discovery.py`), the classification router `select_agent` replacing the retired `AGENT_REGISTRY` (`agents/routing.py`), the advisory `max_mode` lint (`agents/lint.py`); skill packs as description-matched **content** via the `lookup_skill_pack` tool (`skills/packs.py`, `skills/pack_discovery.py`, added to the read-tools floor); `hooks.toml`-driven gated/mode-clamped/fail-closed hooks (`hooks/{config,events,runner,wiring}.py`) with `pre_tool`/`post_tool` live and `pre_deploy`/`post_build`/`on_run_failed` as wired-but-deferred-emitter seams; MCP-consume namespaced `mcp:<server>:<tool>` + effects-tagged + **fail-closed on missing effects** (`mcp/{config,client}.py`, `McpToolSpec` + `mcp:` prefix precondition + `mcp:<server>:*` wildcard grant expansion in `policy.py`); and the live-wiring seam (`cli/orchestrator/extensibility_wiring.py` — the `HookFactory = (mode) -> (pre, post)` that re-clamps a delegated child's hooks at `child_mode`, the skill-pack tool builder, and `resolve_agent_or_fallback`) plus the `carve agents`/`skills`/`mcp-servers` CLIs and the `hooks_file`/`mcp_file` `PathsConfig` keys. Security + agent-loop reviewers PASS; ruff/mypy --strict/1328 pytest green. Spec reconciled to the shipped code (minor inline drift only — the hook-factory seam and the `lookup_skill_pack` tool name).

- **Model auth (Increment 1b) — credential precedence + SDK-native OAuth.** Landed 2026-06-21: the single `client_factory.make_client` precedence resolver (explicit `auth_mode` wins and suppresses a stray opposite credential via the SDK header-omit sentinel; else `ANTHROPIC_API_KEY`; else a subscription OAuth bearer built with `auth_token=` + the `anthropic-beta: oauth-2025-04-20` header from `ANTHROPIC_AUTH_TOKEN`/`CLAUDE_CODE_OAUTH_TOKEN`; else a clear `ConfigError`; auto mode refuses a both-present env), collapsing the four prior `anthropic.Anthropic(api_key=…)` sites; `ModelsConfig` gained `auth_mode` (validated) + `tiers` + `resolve_model`; `default_model` → `claude-opus-4-8` with `pricing.py` updated to the current models; `carve auth status`/`login` (login wraps `claude setup-token`); per-agent model-tier resolution at delegation. Carve owns **no** browser flow or token store. Security reviewer PASS + an adversarial 27-cell precedence-matrix verification PASS (exactly-one-credential at the wire, OAuth always carries the beta header); ruff/mypy --strict/1367 pytest green. Deferred (non-blocking): thread `config.models.tiers` into the live delegation runner when the orchestrator constructs it.

- **Packaging (Increment 1) — bundled Postgres docker-compose + external-Postgres option.** Landed 2026-06-21: `carve init` renders a Postgres-only `docker-compose.yml` (`postgres:16`, slug-named container/volume so multiple projects don't collide, bound to `127.0.0.1` only, healthcheck) and adds a `DATABASE_URL` block to `.env.example`; `--external-postgres <url>` validates/normalizes the URL, skips the compose bundle, and migrates against the external DB directly (the URL is used as-is so a stray `DATABASE_URL` env can't redirect it). Migration is **graceful**: external is fatal-on-failure, but the bundled path defers with a next-step when Postgres isn't up yet (the real first-run case). Docker-absent + no `--external-postgres` → friendly exit 3. Idempotent (existing `docker-compose.yml`/`.env` left alone). Security note: the external path writes a **commented placeholder** to `.env.example` and prints the real password-bearing URL for the user to paste into the gitignored `.env` — never to a committed file (a leak the adversarial review caught and that's now regression-tested). Helpers in `cli/commands/packaging.py`; 3 docs added. ruff/mypy --strict/1397 pytest green.

- **Init (Increment 2, lean) — the control-plane `carve init` rewrite.** Landed 2026-06-22: the `carve.init` package (`detect` → `resolve` → `scaffold`) and a thin rewritten `carve init` command. **Detection**: dbt at root + one level down, dlt via `.dlt/` or AST-parsed `import dlt` (parse, never exec), git, docker (`shutil.which`), re-init. **Resolution** (`plan.resolve`): non-interactive across the Postgres × dbt × dlt axes, precedence explicit-flag > detected > default, with clean `InitError`s on conflicting flags, ambiguous multi-detect, and dbt/dlt names colliding. **Scaffold**: the control-plane `carve.toml` renderer (simple mode writes **no** `[components.*]` block — same-repo dbt/dlt is convention-discovered; separate-local/-remote get one; no `[state_store]` block, so it loads before `.env` exists), empty memory templates (`standards.md`/`decisions.md`/`conventions.md`), `.env.example` + `.gitignore`, bundled compose (or external-Postgres placeholder with the real URL printed, never committed), `--with-dbt`/`--with-dlt` greenfield scaffolds; idempotent (skip-if-exists, symlink-safe) and re-init-preserving. Then default-target wiring + graceful state-store migration (external fatal, bundled defers) + `git init`. Adversarial correctness+security review (2 BLOCKER + 2 MAJOR + 1 MINOR) fixed and regression-tested: non-BMP `carve.toml` escaping, duplicate-component-name guard, SCP-URL component naming, up-front `--default-target` validation (exit 2, no half-write), dangling-symlink write-through. ruff/mypy --strict/1439 pytest green. **First-run cleanup pass (2026-06-22)** — a completeness assessment found the lean init printed a quickstart that didn't run; fixed: the default-target `[snowflake.<name>]` is now scaffolded **commented** in `connections.toml` so a fresh project loads without warehouse creds (the advertised `carve plan` was dying at config load on unset `${DEV_SNOWFLAKE_*}`); `conventions.md` is now comment-only and the EL agent skips comment-only convention docs (it was being told "no conventions inferred"); printed Next-steps drop the `carve serve` stub; init prints a brownfield-detected acknowledgement; `--destination-kind` (dead flag) cut; spec drift reconciled (exit-2 convention, `carve.toml` shape, file inventory, skip-only re-init). 1441 pytest green. **Deferred** (each tracked, issues #9–#14): convention *inference* (placeholder `conventions.md`), interactive prompts (`--non-interactive` accepted; resolution is always non-interactive), `--migrate-from-targets`, dbt-engine detection/eager-install, auth-token bootstrap, getting-started docs.

- **Memory (Increment 2, lean) — the runtime read/append machinery for project memory.** Landed 2026-06-22: the `carve.core.memory` package. **`MemoryLoader`** — mtime-cached reads of the five file types (`conventions`/`standards`/`decisions` at `carve_dir/*.md`; `pipelines/<name>.md`; `el/<name>/NOTES.md`) over `ProjectPaths`, `None` for absent files, `invalidate()`. **`select_for_task`/`MemoryBundle`** — always conventions+standards; decisions only when `is_investigative`; sidecars when present. **`MemoryWriter.append_decision`** — the one write exempt from the plan/build gate; newest-first anchored to the dated-entry region (not the scaffolded `## Format` docs), dup-by-(title,date) → `DecisionAlreadyExists`, single-line-title guard (no forged headings), atomic temp+`os.replace` write, loader invalidation. **`carve memory show / edit / append-decision`** CLI (edit writes directly + unlinks an abandoned-empty sidecar). **Dormant** orchestrator hook `attach_memory_to_context` (unit-tested; no caller produces a goal classification until plan-build). Adversarial review (2 lenses + verify): 2 MAJOR + several MINOR fixed and regression-tested — the newest-first-vs-`## Format` insert bug, the title-newline forge, the abandoned-empty-sidecar, non-atomic writes. Spec drift reconciled (decision-entry format is freeform-body, not enforced labels). ruff/mypy --strict/1471 pytest green. **Deferred** (each tracked): `carve memory refresh` (needs the convention-inference engine — same blocker as init #9), REST `/api/v1/memory/*` + MCP parity (rest-api/mcp — later increment), the `plan_id`-gated `standards`/sidecar writes (state model can't express the gate), and the live orchestrator wiring (goal classification lands with plan-build).

- **SQL (Increment 2, lean) — a dialect-aware tool layer + DuckDB substrate.** Landed 2026-06-22: the `carve.core.sql` package. A **`sqlglot` classifier** (read/write/DDL/destructive, fail-closed) that catches what the old regex missed — `WITH…INSERT`, `SELECT…INTO`, multi-statement, trailing-comment nodes; **`validate`/`transpile`/`normalize_dialect`** (snowflake+duckdb first-class, postgres/bigquery/databricks/tsql author-only); dialect-dispatched **`introspect`** (`INFORMATION_SCHEMA`, caps + `truncated`, DuckDB scoped to one catalog); and the **`sql` tool** (ops `validate`/`transpile`/`introspect`/`run`) bound to a connection's dialect + the active `PermissionMode`, with write/DDL enforced in-tool via the shipped `warehouse_roles` floor (**deploy-only**, destructive-DDL approval). A first-class **DuckDB connector** (local/test substrate — the whole stack runs creds-free) + a `[duckdb.<target>]` connection type; `sql` registered in the permission policy; a **dormant** `sql-specialist.md` builtin. Adversarial review (2 lenses + verify) caught a **BLOCKER** (`SELECT…INTO` classified READ → a write on the read role in read_only) and a **MAJOR** (DuckDB introspection double-counting attached catalogs); both fixed + regression-tested, along with trailing-comment and exact-cap-truncation minors. Spec reconciled (deploy-only writes; op-set `validate/transpile/introspect/run`; the catalog-skill/`run_snowflake_query` generalization explicitly deferred — M1 paths untouched). ruff/mypy --strict/1533 pytest green. **Deferred** (each tracked): the catalog-skill + `run_snowflake_query` generalization, first-class introspection for the four author-only dialects, the orchestrator wiring of the specialist, and the `sql` step type (Increment 3).

- **Tool binder (Increment 3, foundational) — declarative grants → real executors.** Landed 2026-06-23: the harness seam that makes any declarative subagent able to *run* its tools. `subagent_registry.grant_stub_tool` had been yielding name-only stubs whose executor **raised** — so no declarative agent (including the just-shipped `sql-specialist`) could execute its `tools:` grant. New `carve.core.agents.tool_binding.bind_grant_tools(declared_tools, BindingContext)` maps each grant name to the real harness base tool (`read_file`/`grep`/`glob`/`edit`/`create_file`/`bash`/`web_fetch`/`web_search`/`todo`) from `(project_dir, child-clamped gate, approver)`; names whose dependency the harness doesn't hold (`sql`/`run_snowflake_query`/`mcp:*`/`dlt_*`) are supplied by the caller via `extra_tools` (with a bound-name == grant-name precondition), else stay a raising stub (fail-loud). Only grant **stubs** are bound — a spec/fixture's real tools pass through untouched (a `is_grant_stub` marker distinguishes). `SubagentRunner` builds the gate from the grant then binds at `child_mode`, so a delegated agent's `bash`/`edit` run clamped to its (narrower) authority. This retroactively makes the `sql-specialist` functional (its `sql` grant binds when the orchestrator injects a connection-backed `sql` tool) and unblocks every Increment-3 engineer. Adversarial review (2 lenses): the one real finding (a name-mismatched injected tool failing silently) fixed → fail-loud + regression-tested; a pre-existing loop duplicate-tool-name gap filed (#26). ruff/mypy --strict/1542 pytest green. **Deferred** (the live orchestrator wiring that constructs `SubagentRunner` with `extra_tools` + a goal classification remains, as before).

- **DLT-engineer phase 1 — substrate (Increment 3).** Landed 2026-06-23: the `carve.integrations.dlt` package the engineer runs on. `dlt` pinned as a dependency. **`verify.parse_dlt_run`** turns a `dlt pipeline run` into the verification loop's `CheckResult` by reading dlt's on-disk **load package** (`load/{loaded,normalized,new}/<id>/`) — user tables (internal `_dlt_*` filtered), applied schema changes, and failed jobs — exit-code-gated, with the real error line surfaced from stdout on failure (the harness `run_check` routes output there). **`code_emitter`** writes the Carve provenance header that the existing reader round-trips. Adversarial review ran *real* failing dlt loads and confirmed the false-green invariants hold (terminal/partial job failures, stale-package masking, malformed artifacts all caught); fixed the one MAJOR (error tail read the wrong stream) + hardened load-id ordering to numeric; a provenance-reader round-trip gap filed (#28). 16 substrate tests incl. a real DuckDB run; ruff/mypy --strict/1558 pytest green. **This is phase 1 of building the dlt-engineer toward its full spec** — phases 2 (the four callable skills + `sources/` corpus), 3 (the agent with all four strategies + the author→run→verify vertical), and 4 (the reviewers + the harness review fan-out) follow; only live goal-routing is genuinely blocked (the plan-build classifier).

**Increment 1 is complete** — layout, harness, extensibility, model-auth (as Increment 1b), and packaging are all shipped. **Increment 2 is complete** — the `init`, `memory`, and `sql` lean cores have all landed, with their heavier parts deferred to tracked issues. **Increment 3 is in progress** — the tool binder (the foundational unblock for declarative engineers) and the dlt-engineer's phase-1 substrate have landed; the rest of the dlt-engineer (skills → agent → reviewers) and the other Increment-3 capabilities are building toward their full specs. The full corpus is internally consistent under the control-plane + AI-harness model.

---

## Increment 0 — Baseline: formalize the shipped state store ✅ *(done 2026-06-18)*

**Goal.** Reconcile the shipped Postgres state store with its spec — the foundation every increment plans against.

**In scope**
- Postgres state store — [state-store](./capabilities/state-store.md) *(landed; spec reconciled — verified green against a live Postgres testcontainer)*

> **Re-slotted.** Two capabilities first bucketed here moved out. **plan-build → Increment 3** (its plan-synthesis rolls up the engineers' diffs/costs; formalizing it against the rebuilt foundation — not the soon-to-be-replaced M1 shape — is where it's real). **model-auth → Increment 1b** — and *not* as a formalize: only its `ANTHROPIC_API_KEY` path shipped in M1.1; the OAuth flow and the single credential-precedence resolver were planned (M1.1-02) but never built, so picking it up is a **net-new build**, not reconciliation of shipped code.

**Depends on.** Current state only.

**Delta.** Verify/MODIFY against shipped code (the M1 fixture sweep + the three unit tests already landed). No new code — the spec was made honest (Status → Landed).

**Exit criteria.** ✅ The state-store spec matches the code; the full state/migration test surface is green against Postgres.

---

## Increment 1 — Foundation: control-plane layout + the AI harness ✅ *(complete 2026-06-21)*

**Goal.** The structural + AI substrate everything runs on: a control-plane `carve.toml` that references components by name, and the Claude-Code-style harness (subagents, terminal tools, permission gate, verify-by-execution) with declarative extensibility.

**In scope**
- OSS packaging: bundled docker-compose Postgres + external-Postgres option — [packaging](./capabilities/packaging.md)
- Control-plane flat layout: `carve.toml` `[components.<name>]`, the component locator, repo topology, simple-mode convention discovery, the workspace cache — [layout](./capabilities/layout.md)
- **The agent harness** — subagent `delegate`, terminal tools (edit/bash/grep/web), the permission gate (modes + `allowed_paths` + bash sandbox + secret-deny), verify-by-execution, interrupt/TODO/compaction — [harness](./capabilities/harness.md)
- **Extensibility** — declarative agents (`carve/agents/*.md`), skill packs (`SKILL.md`), hooks (`hooks.toml`), MCP both directions, runtime grant attenuation — [extensibility](./capabilities/extensibility.md)

> *Model auth re-slotted to **Increment 1b** below — only its API-key path shipped in M1.1, so the OAuth + precedence-consolidation work is a net-new build, not a formalize.*

**Depends on.** Increment 0 (the state store) + M1/M1.1 (the agent loop the harness wraps; the M1.1 API-key model auth the harness already uses).

**Delta.** 15 *wraps* the M1 agent loop (adds delegation, the gate, context management); it does not replace it. 16 is net-new. 03 introduces `carve.toml` as control-plane config (supersedes the M1 project-shaped config) + the locator (net-new).

**Exit criteria.** A `carve.toml` with `[components.<name>]` resolves names to code (simple + multi mode); an agent runs under the permission gate with terminal tools and can `delegate` to a subagent that verifies by execution; a user-authored `carve/agents/*.md` overrides a built-in, attenuated to its mode. *(Model auth → Increment 1b.)*

---

## Increment 1b — Model auth: OAuth + credential-precedence consolidation ✅ *(done 2026-06-21)*

**Goal.** Make "how Carve authenticates to its model provider" an owned subsystem: one credential-precedence resolver, the `auth_mode` + model-tiers schema in `models.toml`, the OSS-vs-hosted seam, and the Claude-subscription OAuth path (`carve auth login`) — on top of the API-key path that already ships.

**In scope**
- Model auth — `auth_mode` + model `tiers` on `ModelsConfig` (+ `default_model` fixed to a current id, with the pricing table updated to match); a single `client_factory` / precedence resolver collapsing the four `anthropic.Anthropic(api_key=…)` call sites into one place; SDK-native OAuth via `auth_token=` + the `oauth-2025-04-20` header (sourced from `ANTHROPIC_AUTH_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN`); `carve auth status` + `carve auth login` (a thin wrapper over `claude setup-token`); the OSS-vs-hosted split — [model-auth](./capabilities/model-auth.md)

**Depends on.** Increment 1 (harness — the credential's consumer; layout — `models.toml`'s config-bundle home).

**Delta.** *Net-new, not a formalize.* Only the `ANTHROPIC_API_KEY` path shipped in M1.1. **MODIFY:** `ModelsConfig` (add `auth_mode`/`tiers`, fix `default_model`), the four client-construction sites (→ one `client_factory`), the pricing table (add the current model ids so the new default is priced). **CREATE:** `client_factory` (the precedence resolver + SDK-native OAuth bearer wiring) and the `carve auth` CLI. Carve builds **no** browser flow or token store.

**Investigation (resolved).** The M1.1-02 questions are answered: the `anthropic` SDK authenticates a subscription OAuth bearer natively via `auth_token=` (Bearer) + the `anthropic-beta: oauth-2025-04-20` header — same `messages.create` surface, no adapter, identical usage shape — so **no** `claude-agent-sdk` dependency and **no** Carve-owned browser flow / token store / refresh are needed; tokens are minted by `claude setup-token` / `ant auth login`. `api_key` stays the default.

**Exit criteria.** The agent layer authenticates via **either** `ANTHROPIC_API_KEY` **or** a Claude-subscription OAuth bearer through one precedence resolver (`client_factory`), sending exactly one credential and a clear error when neither is present; the OAuth path attaches the `oauth-2025-04-20` header; `carve auth login` wraps `claude setup-token` and `carve auth status` reports the active mode without leaking secrets; `models.toml` carries `auth_mode` + `tiers` and `default_model` is a current, priced id; the OSS-vs-hosted split is explicit.

---

## Increment 2 — Bootstrap & SQL: a scaffolded project + the dialect-aware tool

**Goal.** Scaffold a real project (greenfield or brownfield) with project memory, and stand up the dialect-aware SQL tool every engineer rides on.

**In scope**
- **`carve init`** — greenfield/brownfield across the Postgres × dbt × dlt × memory axes; renders the control-plane `carve.toml`; convention inference — [init](./capabilities/init.md). *Lean core landed 2026-06-22 (see Current state); the deferred parts — convention inference, interactive prompts, `--migrate-from-targets`, dbt-engine detection/eager-install, auth-token bootstrap, getting-started docs — remain in scope here.*
- **Project memory** — `conventions.md` / `standards.md` / `decisions.md`, sidecars, `carve memory` surface — [memory](./capabilities/memory.md)
- The dialect-aware **SQL tool layer** — sqlglot validate/transpile, per-dialect introspection, role-gated exec (Snowflake + DuckDB first-class) + a thin SQL specialist — [sql](./capabilities/sql.md)

**Depends on.** Increment 1 (layout, harness, extensibility) + Increment 0 (the state store).

**Delta.** 05 *rewrites* the M1.1 init around the control-plane `carve.toml` + the four axes. 06 builds on 05's scaffolded memory files. 18 *generalizes* the M1 Snowflake-only `run_snowflake_query` + catalog skills into a dialect-aware layer (preserves the connector).

**Exit criteria.** `carve init` produces a working project (bundled or external Postgres) with memory scaffolding; brownfield init infers conventions and writes no `[components.*]` blocks in simple mode; the `sql` tool introspects a live warehouse on the read role.

---

## Increment 3 — Components: the AI authors, runs & composes dlt **and** dbt

**Goal.** The AI authors **both** dlt and dbt components (co-equal), runs them, provisions backends on demand, and composes components by name into a runnable pipeline DAG.

**In scope**
- The **DLT engineer** subagent (authors/runs dlt; native/REST/curated-library/MCP paths) + dlt-qa / dlt-security reviewers — [dlt-engineer](./capabilities/dlt-engineer.md)
- The **dbt engineer** subagent — authors/modifies dbt models, tests, sources; verifies via `dbt build`/`test`; + a dbt-qa reviewer — [dbt-engineer](./capabilities/dbt-engineer.md)
- **dbt execution backends** — local (bundled Fusion/dbt-core, or the team's own dbt) + managed (snowflake-native, dbt Cloud, remote), behind one step interface — [dbt-execution](./capabilities/dbt-execution.md)
- **connect** — AI-driven on-demand provisioning: engine install + pin, warehouse/source connect — [connect](./capabilities/connect.md)
- **Multi-step pipeline** composition: `pipelines/<name>.toml`, the step DAG executor (dlt/dbt/sql), `[seed_schedule]`, component-by-name, the definition reconciler, the **pipeline engineer**, `carve component(s)` graduation — [pipelines](./capabilities/pipelines.md)
- **Plan / build** *(formalize M1.1-shipped + complete)* — the Plan/Build entities + `plan`/`build`/`plan-and-build` verbs + `--refine` (shipped), the config-hash drift gate, and the plan synthesis that now rolls up the engineers' verified diffs/costs — [plan-build](./capabilities/plan-build.md)

**Depends on.** Increment 2 (init/memory/sql) + Increment 1 (harness, layout, extensibility).

**Delta.** 04 *replaces/generalizes* the M1 EL agent as a declarative subagent. **dbt-engineer is net-new — the exact parallel to the DLT engineer, co-equal from the start.** dbt-execution is net-new: it implements the `dbt` step against the StepExecutor protocol (the runtime's scheduler + worker-placement *dispatch* it in Increment 4). connect + dbt-execution are co-designed (the bundled-engine provisioning seam). 08 is net-new (the reconciler creates the `pipelines`/`schedules` tables). plan-build formalizes its M1.1-shipped lifecycle core + adds the config-hash drift check + the synthesis rollup, now that the dlt/dbt engineers produce the diffs/costs it composes.

**Exit criteria.** `carve plan "ingest Stripe, then stage it with dbt"` → the **DLT and dbt engineers** author + verify their components (`dlt pipeline run`, `dbt build`/`test`); `carve build` materializes them + a `pipelines/<name>.toml` referencing them by name; `carve pipelines validate` passes; first dbt use provisions + pins the engine via `connect`; `carve plan` rolls up exact LLM cost + a runtime estimate from the engineers' diffs, and `carve build` refuses a drifted plan (exit 3).

---

## Increment 4 — Runtime & telemetry: schedule, run, record

**Goal.** Schedule and run composed pipelines end-to-end on cron, with telemetry.

**In scope**
- The **runtime** — scheduler (reads the `schedules` table), Postgres job queue (optimistic claim), worker pool, heartbeats, reaper, archiver; the live `schedules` table + `carve schedule` mutation surface + `schedule_changes` audit; dispatches dlt/dbt/sql steps (incl. worker placement for dbt-execution's local backend) — [runtime](./capabilities/runtime.md)
- **Observability** — agent/run/step/skill telemetry tables, `carve metrics` rollups (token→$, run success/failure, per-agent usage), OpenTelemetry/OTLP export — [observability](./capabilities/observability.md)

**Depends on.** Increment 3 (08's pipeline definitions + reconciler the scheduler reads; dbt-execution's steps the runtime dispatches; the engineers whose runs observability records).

**Delta.** 07 *wraps* the M1 `LocalVenvRunner` in a scheduler + queue + worker layer; creates the runtime tables (jobs, workers, archives, events, **schedules**, schedule_changes); completes the worker-placement dispatch for dbt-execution's local backend. observability records over runtime's events + the harness's per-agent-invocation telemetry (the instrumentation hook is wired here); the `/metrics` REST surface lands in Increment 5.

**Exit criteria.** `carve serve` schedules + runs a composed dlt→dbt→sql pipeline on cron; `carve schedule pause/resume/set-cron` changes firing instantly, audited; `carve metrics` rolls up cost / runs / per-agent usage.

---

## Increment 5 — Interfaces & investigation: REST, MCP, UI, ask, lineage, search

**Goal.** Drive Carve programmatically, and investigate the project read-only.

**In scope**
- **REST API** — full CLI-surface coverage, auth, errors, pagination, streaming, webhooks (incl. `/metrics/*` onto observability) — [rest-api](./capabilities/rest-api.md)
- **MCP server** — auto-generated from REST; stdio + WebSocket — [mcp-server](./capabilities/mcp-server.md)
- **Static HTML UI** — regenerated per run; `carve docs serve` — [ui](./capabilities/ui.md)
- **The explorer (`ask`)** — read-only investigative subagent; citations — [ask](./capabilities/ask.md)
- **Lineage by investigation** — the `dlt_schema` reader skill; the explorer answers lineage via dbt manifest + dlt schema + code (no Carve store) — [lineage](./capabilities/lineage.md)
- **Semantic search** — the embedding index + `semantic_search` skill + `carve embeddings rebuild` — the fuzzy retrieval layer atop the deterministic ones — [semantic-search](./capabilities/semantic-search.md)

**Depends on.** Increment 4 (surfaces to expose) + Increment 3 (the dbt manifest lineage reads). 12/19/semantic-search need the harness (1) + SQL tool (2); 10/11 need 09; semantic-search needs ask + lineage.

**Delta.** Largely net-new surfaces over the increments 1–4 substrate. 12 subsumes the old ask-only guardrail into the `read_only` mode. 19 adds one skill (`dlt_schema`) + explorer guidance — no graph. semantic-search adds the embedding index + skill + rebuild command.

**Exit criteria.** Every CLI action has a REST + MCP equivalent (parity); `carve ask "where does X come from?"` returns a cited answer via investigation; `carve ask` resolves a fuzzy concept ("churn metrics") via semantic search; the static UI shows run history + per-run logs.

---

## Increment 6 — Deploy & recovery

**Goal.** Promote built code to prod, and auto-diagnose failures.

**In scope**
- **Deploy** — `carve deploy <pipeline>` configurable handoff (files/commit/push/pr, default pr); cross-repo linked PRs; pre-flight drift — [deploy](./capabilities/deploy.md)
- **Recovery engineer** — diagnose-then-delegate on retries-exhausted `run.failed`; the `Investigation` entity; auto-pause/resume gated by pause origin — [recovery](./capabilities/recovery.md)

**Depends on.** Increment 3 (deploy targets) + Increment 4 (07 `run.failed`, schedules auto-pause). 17 needs the harness/delegation (1) + the engineers it delegates to (3) + deploy (this increment, for the resolving-deploy → auto-resume link).

**Delta.** 14 *retires* the `carve el deploy` DDL-apply path; net-new handoff/linked-PR machinery + the `deploys` table. 17 reuses the M1 recovery POC's reconcilable parts; net-new `investigations` table + the diagnose-then-delegate flow (delegating dlt/dbt/sql fixes to the engineers).

**Exit criteria.** `carve deploy` opens a (linked) PR by default, each handoff depth working; a retries-exhausted failure produces a grounded `Investigation` + a reviewable fix Plan, auto-pauses the schedule, and the resolving deploy auto-resumes it (unless a human paused it).

---

## Increment 7 — Reference & initial release

**Goal.** Correct reference docs and tag the initial release — shipping **all 26 capabilities**: the full intent → plan → build → run → deploy → schedule loop for dlt **and** dbt.

**In scope**
- **Reference docs** — cli-reference / config-schema / glossary / governance kept in lock-step via completeness tests — [reference-docs](./capabilities/reference-docs.md) *(content rewritten 2026-06; this increment adds the completeness tests against built code)*
- **Release** — tag the initial release (the semver version is chosen at release time).

**Depends on.** Everything (reference derives from the built surface).

**Delta.** The reference content is already regenerated to the current model; this increment adds the build-time completeness tests (every Typer command in cli-reference; every init-scaffolded file in config-schema) and pins the few **planned** CLI commands by giving each an owning slice or cutting it.

**Exit criteria (initial release).** `carve init → plan → build (dlt + dbt) → run → deploy → scheduled-run-on-cron` works end-to-end against a real Snowflake account, and the same loop works via REST and MCP. Completeness tests green.

---

## Sequencing rationale

```
M1 / M1.1
   │
   ▼
Incr 0  state-store                                      (formalize the shipped state store -- done)
   │
   ▼
Incr 1  packaging · layout · harness · extensibility     (control-plane + AI foundation)
   │
   ▼
Incr 1b model-auth                                       (OAuth + precedence consolidation;
                                                          API-key path already shipped --
                                                          net-new build, not a formalize)
   │
   ▼
Incr 2  init · memory · sql                              (scaffold a project + the SQL tool)
   │
   ▼
Incr 3  dlt-engineer · dbt-engineer · dbt-execution ·    (AI authors / runs / composes
        connect · pipelines · plan-build                  dlt AND dbt; + M1.1 plan-build)
   │
   ▼
Incr 4  runtime · observability                          (schedule / run / record)
   │
   ▼
Incr 5  rest-api · mcp-server · ui · ask · lineage ·     (interfaces + investigation)
        semantic-search
   │
   ▼
Incr 6  deploy · recovery
   │
   ▼
Incr 7  reference-docs + initial release tag             (all 26 capabilities)
```

- **Baseline first.** Incr 0 reconciles the shipped state store with its spec. plan-build (Incr 3) formalizes its M1.1-shipped lifecycle core alongside the rebuilt foundation it integrates with, rather than against the soon-to-be-replaced M1 shape. Model auth is **not** a formalize: only its API-key path shipped, so its OAuth + precedence-consolidation work is a net-new slice (Incr 1b).
- **Foundation before components.** The harness + control-plane layout (1) and the scaffold + SQL tool (2) gate everything AI- and component-shaped.
- **dlt and dbt are co-equal components (3).** Both are authored, verified-by-execution, and composed from the start — dbt is *not* deferred. Execution (dbt-execution) + on-demand provisioning (connect) land with them, before the scheduler, so a dbt step can run the moment it's composed.
- **Capability before interface.** Author / run / compose / schedule (3–4) before exposing over REST/MCP/UI + investigation (5).
- **Deploy + recovery after a pipeline can run** (they act on built/running pipelines).
- **Reference + release last** (derives from the built surface) — the initial release ships the whole loop, dlt and dbt alike.

## Backlog (post-release enhancements)

All 26 capabilities are placed in Increments 0–7 above. This is the living queue for work *after* the initial release — genuine enhancements deliberately scoped out of the first loop. They ride the change lifecycle ([`_strategy/2026-06-change-lifecycle.md`](./_strategy/2026-06-change-lifecycle.md)): a new spec or a spec edit, then `/build-spec`; small ones are GitHub issues, larger ones get an increment here.

- **Concurrent subagent fan-out** — the harness runs sequentially in the initial release; concurrency is a later enhancement ([harness](./capabilities/harness.md)).
- **Column-level lineage** — the explorer reads model SQL on demand today; column-level may arrive via the Fusion dbt engine ([lineage](./capabilities/lineage.md), [dbt-execution](./capabilities/dbt-execution.md)).
- **Custom step-type SDK + in-process custom-skill SDK** — built-in step types + MCP/`SKILL.md` skills ship first ([extensibility](./capabilities/extensibility.md)).
- **First-class Postgres / BigQuery / Databricks / SQL Server** — they author/validate/transpile via sqlglot now; introspection hardening to first-class is later ([sql](./capabilities/sql.md)).
- **Multi-LLM providers** (OpenAI / Google) beyond Anthropic ([model-auth](./capabilities/model-auth.md)).
- **Multiple schedules per pipeline**, Salesforce/SaaS CDC sources, an OAuth side-channel browser flow, opt-in auto-deploy for trivial fixes, and further curated-connector-library waves.
