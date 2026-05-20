# v0.1-13 — Reference doc rewrites: CLI reference, config schema, glossary, governance

> Rewrites the four reference docs in `specs/reference/` to match the v0.1 positioning. Per the spec audit's tags (cli-reference REWRITE, config-schema REWRITE, glossary REVISE, governance REVISE) and [PROJECT_PLAN spec set item 13](../PROJECT_PLAN.md).

## Status

- **Status:** Drafting
- **Depends on:** all v0.1 specs 01–12. Reference docs derive from the source-of-truth specs; this spec lands last.
- **Blocks:** nothing (last spec in v0.1)

## Goal

Rewrite four reference docs so the canonical references for the CLI surface, config files, vocabulary, and contribution model are correct under the v0.1 positioning. After this spec lands:

- A user can look up any `carve` command in `cli-reference.md` and see flags, examples, exit codes
- A user can find any file in a Carve project in `config-schema.md` with its schema and defaults
- A new contributor can read `glossary.md` and quickly understand Carve's vocabulary
- A potential contributor or commercial adopter can read `governance.md` and understand the licensing model and OSS/paid relationship

These docs are reference material — pure cross-reference of specs 01–12, not new product surface area.

## Out of scope

- Anything that adds new functionality (these are docs about already-built surface)
- Tutorials or walkthroughs (those live in `docs/` at the repo root; each spec ships its own `docs/*.md`)
- API reference for the REST API (Swagger UI from spec 09 covers that; `cli-reference.md` cross-links)
- MCP tool reference (`docs/mcp-server.md` from spec 10 covers that; this spec doesn't duplicate)

## Files this spec produces

```
specs/reference/cli-reference.md                        # REWRITE (was tagged REWRITE in audit)
specs/reference/config-schema.md                        # REWRITE (was tagged REWRITE in audit)
specs/reference/glossary.md                             # REVISE (was tagged REVISE)
specs/reference/governance.md                           # REVISE (was tagged REVISE)
specs/reference/README.md                               # MODIFY — points at the four refs above + brief overview
tests/unit/test_cli_reference_completeness.py           # NEW — every Typer-registered command appears in cli-reference.md
tests/unit/test_config_schema_completeness.py           # NEW — every config file scaffolded by carve init appears in config-schema.md
```

## Behavior

### `cli-reference.md`

A complete, authoritative reference for the `carve` CLI surface. Structure:

```markdown
# Carve CLI reference

> Generated to match v0.1.0. For programmatic / agent consumption, see the
> auto-generated OpenAPI schema at /api/openapi.json or the MCP tool listing
> via `tools/list`.

## Quick reference

| Command | Description | Spec |
|---|---|---|
| `carve init`                          | Bootstrap a Carve project           | [v0.1-05](../v0.1/05-init-rewrite.md) |
| `carve plan "<goal>"`                 | Generate a reviewable plan          | [v0.1-04](../v0.1/04-el-agent-dlt.md), M1.1 |
| `carve plan --refine <plan_id>`       | Refine a plan                       | M1.1 |
| `carve plan --pipeline <name>`        | Plan against an existing pipeline   | M1.1 |
| `carve ask "<question>"`              | Read-only investigative query       | [v0.1-12](../v0.1/12-ask-verb.md) |
| `carve build <plan_id>`               | Materialize a plan into files       | M1.1 |
| `carve run <pipeline>`                | Execute a pipeline on demand        | M1.1, [v0.1-07/08](../v0.1/07-runtime.md) |
| `carve run --watch <pipeline>`        | Run + stream logs until completion  | [v0.1-09](../v0.1/09-rest-api.md) |
| `carve run --resume <run_id>`         | Resume failed steps from a prior run| [v0.1-08](../v0.1/08-multi-step-pipeline.md) |
| `carve deploy <pipeline>`             | Promote dev → prod via PR           | M1.1 |
| `carve serve`                         | Start the API + scheduler + worker(s) | [v0.1-07](../v0.1/07-runtime.md) |
| `carve worker`                        | Run a standalone worker process     | [v0.1-07](../v0.1/07-runtime.md) |
| `carve mcp-serve`                     | Start the MCP server                | [v0.1-10](../v0.1/10-mcp-server.md) |
| `carve docs serve`                    | Serve the local static HTML UI      | [v0.1-11](../v0.1/11-static-html-ui.md) |
| `carve pipelines list`                | List pipelines                      | [v0.1-08](../v0.1/08-multi-step-pipeline.md) |
| `carve pipelines show <name>`         | Show one pipeline                   | [v0.1-08](../v0.1/08-multi-step-pipeline.md) |
| `carve pipelines validate [name]`     | Schema + DAG check                  | [v0.1-08](../v0.1/08-multi-step-pipeline.md) |
| `carve pipelines diff <name>`         | Diff against an older build         | [v0.1-08](../v0.1/08-multi-step-pipeline.md) |
| `carve runs list`                     | Recent run history                  | M1.1, [v0.1-09](../v0.1/09-rest-api.md) |
| `carve runs show <run_id>`            | Show one run                        | M1.1 |
| `carve runs tail <run_id>`            | Stream logs from a run              | [v0.1-09](../v0.1/09-rest-api.md) |
| `carve logs <run_id>`                 | Print logs                          | M1.1 |
| `carve logs --follow <run_id>`        | Stream logs                         | [v0.1-09](../v0.1/09-rest-api.md) |
| `carve schedule list`                 | Scheduled pipelines                 | [v0.1-07](../v0.1/07-runtime.md) |
| `carve schedule show <pipeline>`      | One schedule detail                 | [v0.1-07](../v0.1/07-runtime.md) |
| `carve schedule pause/resume <pipeline>` | Schedule controls                | [v0.1-07](../v0.1/07-runtime.md) |
| `carve schedule next-fires`           | Upcoming fires                      | [v0.1-07](../v0.1/07-runtime.md) |
| `carve agents list/show/create/edit/remove/test` | Agent management         | [v0.1-04](../v0.1/04-el-agent-dlt.md), M1 |
| `carve skills list/show/test`         | Skill registry                      | M1, [v0.1-04](../v0.1/04-el-agent-dlt.md) |
| `carve mcp-servers list/add/remove`   | External MCP server registration    | [v0.1-04](../v0.1/04-el-agent-dlt.md) |
| `carve memory show/edit/append-decision/refresh` | Project memory                | [v0.1-06](../v0.1/06-project-memory.md) |
| `carve metrics costs/runs/agents`     | Aggregate metrics                   | [v0.1-09](../v0.1/09-rest-api.md) |
| `carve workspaces list/clear`         | Workspace cache for separate-remote | [v0.1-03](../v0.1/03-flat-layout.md) |
| `carve auth login`                    | OAuth login to Claude subscription  | M1.1 |
| `carve auth token mint/rotate/revoke` | API token management                | [v0.1-09](../v0.1/09-rest-api.md) |
| `carve docs open/regen/serve`         | Static HTML UI commands             | [v0.1-11](../v0.1/11-static-html-ui.md) |

## Global flags

- `--output [table|json|yaml]` — output format
- `--config-dir PATH` — override project dir
- `--server-url URL` — REST API URL (default http://127.0.0.1:8765)
- `--verbose` / `--quiet` / `--no-color`
- `--help`, `--version`

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | User error (bad flag, missing arg) |
| 2 | Runtime error (e.g., pipeline failed) |
| 3 | Config error |
| 4 | Drift detected |
| 5 | Server unreachable |

## Per-command sections

For each command in the quick reference, a sub-section gives:

- Full synopsis with all flags
- One or more examples
- The underlying REST endpoint + MCP tool name (for parity verification)
- Common pitfalls + linked troubleshooting docs
```

A completeness test (`tests/unit/test_cli_reference_completeness.py`) iterates the registered Typer commands and asserts each appears in `cli-reference.md` (matched by command name). Catches regressions where a new command ships without a reference entry.

### `config-schema.md`

The authoritative reference for every config file Carve reads or writes. Structure:

```markdown
# Carve config schema reference

## Files Carve reads or writes

| File | Purpose | Owner | Spec |
|---|---|---|---|
| `carve.toml` | Project metadata, default target, dbt+dlt topology | Carve (templated by init) + user-editable | [v0.1-03](../v0.1/03-flat-layout.md), [v0.1-05](../v0.1/05-init-rewrite.md) |
| `carve/connections.toml` | Target definitions + credential references | User-editable | [v0.1-05](../v0.1/05-init-rewrite.md) |
| `carve/runtime.toml` | Scheduler / worker / archive / webhook config | User-editable | [v0.1-07](../v0.1/07-runtime.md) |
| `carve/conventions.md` | Inferred conventions | Carve-generated (refreshable) | [v0.1-06](../v0.1/06-project-memory.md) |
| `carve/standards.md` | Team standards (user-authored) | User-editable | [v0.1-06](../v0.1/06-project-memory.md) |
| `carve/decisions.md` | Append-only decision log | User-authored | [v0.1-06](../v0.1/06-project-memory.md) |
| `carve/agents/*.toml` | Agent definitions (built-in overrides + custom) | User-editable | M1, [v0.1-04](../v0.1/04-el-agent-dlt.md) |
| `pipelines/<name>.toml` | Pipeline composition | Carve-generated (refinable) | [v0.1-08](../v0.1/08-multi-step-pipeline.md) |
| `pipelines/<name>.md` | Per-pipeline notes (optional) | User-authored | [v0.1-06](../v0.1/06-project-memory.md) |
| `el/<name>/__init__.py` | Generated dlt source | Carve-generated (refinable below provenance header) | [v0.1-04](../v0.1/04-el-agent-dlt.md) |
| `el/<name>/requirements.txt` | dlt deps | Carve-generated | [v0.1-04](../v0.1/04-el-agent-dlt.md) |
| `el/<name>/NOTES.md` | EL artifact notes (optional) | User-authored | [v0.1-06](../v0.1/06-project-memory.md) |
| `.dlt/config.toml` | dlt's per-destination config | User-editable | [v0.1-03](../v0.1/03-flat-layout.md), dlt convention |
| `.dlt/secrets.toml` | dlt's credentials | User-editable (gitignored) | [v0.1-03](../v0.1/03-flat-layout.md), dlt convention |
| `dbt_project.yml` | dbt project config (same-repo only) | User (existing or scaffolded by `--with-dbt`) | dbt convention, [v0.1-05](../v0.1/05-init-rewrite.md) |
| `docker-compose.yml` | Bundled Postgres | Carve-templated (user-editable after init) | [v0.1-02](../v0.1/02-oss-packaging.md) |
| `.env.example` | Env var template | Carve-templated | [v0.1-05](../v0.1/05-init-rewrite.md) |
| `.env` | Env vars (gitignored) | User | [v0.1-05](../v0.1/05-init-rewrite.md) |
| `.gitignore` | Carve adds entries | Mixed | [v0.1-05](../v0.1/05-init-rewrite.md) |
| `.carve/token` | OSS API token (gitignored, mode 0600) | Carve-generated | [v0.1-09](../v0.1/09-rest-api.md) |
| `.carve/plans/<id>.json` | Plan files (gitignored) | Carve-generated | M1.1 |
| `.carve/asks/<id>.json` | Ask answers (gitignored) | Carve-generated | [v0.1-12](../v0.1/12-ask-verb.md) |
| `.carve/workspaces/<name>/` | Remote-repo workspace cache (gitignored) | Carve-managed | [v0.1-03](../v0.1/03-flat-layout.md) |
| `.carve/ui/` | Rendered static HTML (gitignored) | Carve-generated | [v0.1-11](../v0.1/11-static-html-ui.md) |

## Per-file schema

For each file above, a sub-section gives:

- Full TOML/YAML/JSON schema with annotated comments
- Example contents
- Defaults
- Which command writes it (init, build, refresh, etc.) and which agents read it
- Cross-link to the controlling spec
```

A completeness test asserts each file scaffolded by `carve init` appears in `config-schema.md`.

### `glossary.md`

Alphabetical terms with one-paragraph definitions. Revise from the existing version:

**New entries to add** (per spec audit):

- **Ask** — A read-only investigative query through Carve's orchestration agent. See [v0.1-12](../v0.1/12-ask-verb.md).
- **Backend** — In Carve terminology, "backend" refers to dlt or dbt (the external tools Carve invokes). NOT a database or service-side application.
- **dlt** — Python library for the extract-load phase. Carve generates dlt code; dlt executes it. See [dlthub.com](https://dlthub.com).
- **dlt source** — A dlt construct: a logical connector (e.g., Stripe). Contains one or more resources.
- **dlt resource** — A dlt construct: one endpoint or table inside a source.
- **Hosted product** — Carve's commercial offering. Multi-tenant, managed, polished cloud UI. Per [positioning #13](../_strategy/2026-05-positioning.md).
- **Job (runtime)** — A row in the `jobs` table representing one queued or executing pipeline invocation. See [v0.1-07](../v0.1/07-runtime.md).
- **Memory (project)** — User-editable + agent-readable markdown files in the project that capture conventions, standards, decisions, and per-artifact notes. See [v0.1-06](../v0.1/06-project-memory.md).
- **Optimistic claim** — The job-queue pattern Carve uses: `UPDATE ... WHERE status='queued' ... FOR UPDATE SKIP LOCKED`. See [v0.1-07](../v0.1/07-runtime.md).
- **OSS edition** — The open-source Carve, this repo. Apache 2.0. Feature-complete for single-team self-hosters.
- **Provenance header** — The comment block in Carve-generated dlt code recording what generated it and from what. See [v0.1-03](../v0.1/03-flat-layout.md).
- **Reaper** — The runtime loop that reclaims jobs from crashed workers via stale-heartbeat detection. See [v0.1-07](../v0.1/07-runtime.md).
- **Repo topology** — Same-repo vs separate-local vs separate-remote configuration of dbt and dlt projects. Per-backend, independent.
- **Runtime** — Carve's scheduler + job queue + worker pool. The deliberately-narrow execution layer. See [v0.1-07](../v0.1/07-runtime.md).
- **Specialist agent** — A role-scoped agent that handles one domain (extract-load, dbt, runtime). Receives pre-scoped context from the orchestrator. See [ARCHITECTURE §5.1](../ARCHITECTURE.md).
- **Static HTML UI** — Carve's minimal local web UI: pages regenerated per event, served by `carve docs serve`. See [v0.1-11](../v0.1/11-static-html-ui.md).
- **Worker** — A process that claims jobs from the queue and executes them. See [v0.1-07](../v0.1/07-runtime.md).

**Entries to remove or rework** (out-of-date):

- **Approval step** — drop (was M3-era; not in v0.1)
- **Capability flow** — drop (outdated mental model)
- **Embedding search** — annotate as "future / post-v0.1"
- **`LocalVenvRunner`** — keep but note it's now wrapped by the runtime worker pool

**Existing entries to keep/update**:

- Agent, Agent loop, Brownfield, Build, Conventions, Convention inference, DAG, dbt, dbt manifest, DCO, Deploy, Destination, Event bus, Greenfield, Guardrail, Idempotency, MCP (server + client), Orchestration agent, Pipeline, Plan, Refine, Schema retrieval, Skill, Step, Target, Token (API), TOML

### `governance.md`

Revised to capture the OSS/paid relationship explicitly. Sections:

```markdown
# Carve governance

## License

The OSS repo (`carve/carve`) is **Apache License 2.0**. Full text at LICENSE.

We deliberately do not use BSL, SSPL, or other source-available licenses for the
OSS code. Those licenses target hyperscaler resellers; that risk is not real for
us at our scale, and Apache 2.0 is what the data ecosystem expects.

## Contributor Certificate of Origin (DCO)

All commits to the OSS repo require a Developer Certificate of Origin sign-off
via `git commit -s`. This preserves the option to dual-license later without
contributor surprise.

## Hosted product relationship

Carve's commercial offering (the "hosted product") lives in a separate, private
repo. The hosted product depends on the OSS repo as a library. The relationship
is one-way: hosted imports from OSS; OSS never imports from hosted.

Per design decision 5.10 in the PRD:

- **No API endpoints or MCP tools are gated behind the hosted product.** The OSS
  REST and MCP surfaces are feature-complete.
- The hosted product earns its price on **operational excellence**: managed
  infrastructure, multi-tenancy, SSO/OAuth/RBAC, audit log, polished cloud UI,
  premium integrations, hosted secrets.
- We explicitly reject the open-core gating anti-pattern.

This is the dbt Labs / Sentry / Posthog model.

## How contributions work

- File an issue first for non-trivial changes (the maintainers can confirm
  scope before you write code)
- For agent-implemented contributions: Carve's own `/build-spec` workflow is
  available; the resulting PR goes through the same review as any other.
- DCO sign-off required on every commit
- CI checks: lint, type-check, unit tests, integration tests, OpenAPI parity,
  CLI/REST/MCP parity (per specs 09 + 10)

## Maintainership

v0.x maintained by Nate Skousen and Claude Code. Once v0.1 ships, additional
maintainers will be added as contributors emerge. The hosted product is
maintained separately by the commercial entity behind Carve.

## Trademark + branding

"Carve" is a trademark of the commercial entity. The OSS may be forked under
Apache 2.0; forks must not use the "Carve" name in a way that implies
endorsement or affiliation with the upstream project.

## Reporting security issues

Security issues should be reported privately to security@carve.dev (or the
equivalent address at v0.1.0 time) rather than via public issues. See SECURITY.md
in the repo root.
```

### Completeness tests

- **`test_cli_reference_completeness.py`**: imports the Typer app from `src/carve/cli/`, walks every registered command, asserts each command name appears in `cli-reference.md` (matched against the quick-reference table).
- **`test_config_schema_completeness.py`**: imports the init scaffolder from `src/carve/init/scaffold.py`, identifies every file it can write, asserts each appears in `config-schema.md`.

Both tests fail CI if a new CLI command or config file ships without a corresponding reference doc entry — keeping the references in lock-step with the implementation.

## Tests

- `test_cli_reference_completeness.py` (described above)
- `test_config_schema_completeness.py` (described above)
- Manual review: a `/build-spec` reviewer reads each reference doc end-to-end and confirms it matches the v0.1 surface (this is a docs-quality check, not a unit test)
- Cross-link integrity: every `(../v0.1/XX-...)` link resolves to an existing file (verified via a simple grep + filesystem check during CI)

## Acceptance

- `cli-reference.md` lists every `carve` command shipped in v0.1 with flags, examples, exit codes
- `config-schema.md` lists every file in a Carve project with schema, defaults, examples
- `glossary.md` includes the new entries listed above; out-of-date entries removed; existing entries updated for v0.1 terminology
- `governance.md` documents the Apache 2.0 + DCO + hosted-product relationship per the v0.1 positioning
- Completeness tests pass; CI catches reference-doc-drift when new commands or config files ship
- A new contributor can read all four refs end-to-end in under 60 minutes and be operational

## Design notes

- **Why reference docs ship last?** Because they derive from the source-of-truth specs. Drafting them first would mean re-drafting them as the specs evolve. Shipping last means they reflect what actually got built.
- **Why a quick-reference table per doc rather than just per-command sections?** Because users scanning for "what command does X?" want a flat list. The per-command sections are the deep dive; the quick reference is the index.
- **Why completeness tests rather than relying on review?** Because reviewers catch some omissions but not all. A test that asserts "every Typer command is documented" catches every regression with zero ongoing reviewer effort. The cost is one extra check; the benefit is reference docs that stay correct.
- **Why isn't `docs/api-reference.md` part of this spec?** Because Swagger UI from spec 09 is the authoritative API reference. `docs/api-reference.md` (from spec 09) is an overview; the actual endpoint-by-endpoint detail lives in the OpenAPI schema. Duplicating that in `cli-reference.md` would mean two sources of truth.
- **Why include the hosted relationship in `governance.md` even though it's not OSS surface?** Because contributors and adopters need to know how the OSS relates to the commercial product. Being explicit about the boundary (no API gating, hosted is private) addresses the "is this just a teaser?" question up front.

## Open questions

- **Whether to auto-generate `cli-reference.md` from Typer's introspection.** *Implementation default.* No in v0.1 — hand-written reference is clearer (examples, pitfalls, cross-links) than what Typer would emit. The completeness test catches drift on the command-name dimension. Revisit if it becomes a maintenance burden.
- **Whether `config-schema.md` should embed actual JSON Schema / Pydantic-derived schemas.** *Implementation default.* No — annotated example contents are clearer for human readers. The Pydantic schemas in code are the executable source of truth; users who want machine-readable schemas read the code or call the OpenAPI endpoint.
- **Whether to translate reference docs to other languages.** *Strategy-required.* English-only for v0.1. Community translations welcome post-v0.1.
- **Whether to ship a "what's new in v0.1" changelog as part of this spec.** *Implementation default.* No — CHANGELOG.md lives at repo root and follows Keep a Changelog conventions. This spec is reference, not history. (Note: the audit didn't tag CHANGELOG; it gets updated alongside `v0.1.0` tagging.)
