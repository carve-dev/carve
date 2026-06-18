# `carve init` rewrite: greenfield, brownfield, dlt + dbt symmetry

> Rebuilds `carve init` around the v0.1 positioning: Postgres-from-day-one, bundled docker-compose, flat dlt layout, per-backend repo topology, project memory scaffolding, full dlt/dbt symmetry. Per [PRD §6.1](../PRD.md), [PRD §6.2](../PRD.md), [PRD §6.3 project memory](../PRD.md), [ARCHITECTURE §3 code layout](../ARCHITECTURE.md), [ARCHITECTURE §10.5 convention inference](../ARCHITECTURE.md), and [PROJECT_PLAN spec set item 5](../PROJECT_PLAN.md). Replaces the archived [P1-03 init-per-target-layout draft](../_archive/pillar-1-extract-load/03-init-per-target-layout.md), whose premise (`targets/<target>/el/`) is broken by the flat-layout decision in spec 03.

> **Revised for the control-plane model** ([../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md); concrete shapes in [../_strategy/control-plane-reference-model.md](../_strategy/control-plane-reference-model.md)). The rendered `carve.toml` is the **control-plane config**: `[project]` + `[state_store]` + `[components.<name>]` blocks ([layout](./layout.md)). In **simple mode** (same-repo dbt/dlt — detected brownfield *or* a `--with-*` greenfield scaffold), init writes **no** `[components.*]` blocks — components are discovered by convention. A `[components.<name>]` block is written **only** for a component that lives elsewhere (`--dbt-path`/`--dbt-url`/`--dlt-path`/`--dlt-url`, i.e. separate-local / separate-remote). The old singular `[dbt]` / `[dlt]` / `[models]` blocks are **retired** (this reconciles spec 05 to spec 03; agent-model tiers are per-agent frontmatter in [extensibility](./extensibility.md) + the install default, not a `carve.toml` block).

## Status

- **Status:** Drafting
- **Depends on:** [state-store](./state-store.md), [packaging](./packaging.md), [layout](./layout.md)
- **Soft depends on:** [memory](./memory.md) — spec 05 scaffolds the memory files (writes empty templated `standards.md`/`decisions.md`, runs convention inference for `conventions.md`); spec 06 ships the runtime read/edit machinery that consumes them
- **Blocks:** [runtime](./runtime.md) (init must produce a working Postgres connection before `carve serve` runs), [reference-docs](./reference-docs.md) (init writes `.env.example` which the reference docs cover)

## Goal

`carve init` produces a working Carve project in one command, handling four orthogonal axes:

1. **Postgres setup**: bundled docker-compose (default) or `--external-postgres <url>`
2. **dbt topology**: detected brownfield same-repo, `--dbt-path <path>` (separate-local), `--dbt-url <url>` (separate-remote), `--with-dbt` (greenfield scaffold), or absent (no dbt)
3. **dlt topology**: detected brownfield same-repo, `--dlt-path <path>` (separate-local), `--dlt-url <url>` (separate-remote), `--with-dlt` (greenfield scaffold), or absent (no dlt yet)
4. **Memory scaffolding**: writes empty templated `carve/standards.md` and `carve/decisions.md`; runs convention inference against any detected brownfield projects to populate `carve/conventions.md`

The four axes are independent. A user can have `--external-postgres` + brownfield dbt same-repo + greenfield dlt + memory scaffolded — and `carve init` handles that combination cleanly.

**init *detects*, it doesn't *provision*.** For dbt it records *which execution backend exists* (an existing dbt project → `local`; dbt Cloud creds → `dbt-cloud`; dbt-on-Snowflake → `snowflake-native`; none) into the component config — but it does **not** install a dbt engine or force an engine/version choice. Bundled-engine install + pin happens lazily on first dbt use via [connect](./connect.md). A power user may opt into eager install (`--dbt-engine … --dbt-version …`); otherwise it's deferred. See [dbt-execution](./dbt-execution.md) for the backends.

In **interactive mode** (default when stdout is a TTY), missing flags trigger prompts. In **non-interactive mode** (`--non-interactive`, or when stdout is not a TTY), all decisions must be supplied via flags or env vars; missing required input is a clean error.

## Out of scope

- The bundled docker-compose template itself (lives in spec 02)
- The flat directory layout itself (lives in spec 03)
- The Postgres engine config (lives in spec 01)
- The `carve memory` CLI commands and read/edit machinery (lives in spec 06; this spec only scaffolds the files)
- The EL agent that authors dlt pipelines (lives in spec 04; init does not generate any pipelines, only the empty `el/` directory)
- The runtime / scheduler / worker (spec 07); init produces a project that's ready for `carve serve`, but `serve` itself is its own spec

## Behavior

### Top-level command surface

```
carve init [OPTIONS]

OPTIONS:
  --non-interactive            Disable prompts; fail if required input missing
  --external-postgres URL      Use external Postgres; skip docker-compose scaffold
  --postgres-bundled           Force the bundled compose path even if --external-postgres-looking
                                env vars are set (escape hatch)

  --with-dbt                   Scaffold a new dbt project at the root (greenfield)
  --dbt-path PATH              Use existing dbt project at this filesystem path
  --dbt-url URL                Use existing dbt repo at this git URL
  --dbt-branch BRANCH          Branch for --dbt-url (default: "main")

  --with-dlt                   Scaffold a sample dlt source at el/sample/ (greenfield)
  --dlt-path PATH              Use existing dlt project at this filesystem path
  --dlt-url URL                Use existing dlt repo at this git URL
  --dlt-branch BRANCH          Branch for --dlt-url (default: "main")

  --migrate-from-targets       Migrate an existing targets/<target>/el/ layout to flat el/ (per spec 03)

  --project-name NAME          Override project name (default: directory name slugified)
  --default-target NAME        Target name to make the default (default: "dev")
  --destination-kind KIND      Destination type for the default target (default: "snowflake";
                                supports any dlt destination: postgres, bigquery, duckdb, etc.)

  --skip-postgres-bootstrap    Don't try to connect to Postgres during init (defers to first carve serve)
  --no-git-init                Don't run git init even if no git repo present
```

### Flow

The init orchestrator runs roughly as:

```
1. Detect environment
   - cwd has carve.toml → re-init mode (idempotent path; preserves user edits)
   - Look for: dbt_project.yml (one level deep), .dlt/, el/, *.py with dlt decorators
   - Check: git repo present, docker installed, python venv active
   - Record findings as a Detection object

2. Resolve decisions
   - Each axis (postgres, dbt, dlt, memory) resolves to one of its valid values
   - Resolution priority: explicit flag > env var > detected value > prompt (interactive) > error (non-interactive)
   - Output: a fully-resolved InitPlan

3. Confirm with user (interactive mode only)
   - Print the InitPlan summary
   - "About to write the following files, in this layout, against this Postgres. Proceed? [Y/n]"
   - Single confirmation; no per-file granularity (too noisy)

4. Execute scaffold
   - Write files in dependency order: carve.toml → carve/ → .env.example → .gitignore → docker-compose.yml (or skip) → .dlt/ templates → el/ → pipelines/ → dbt scaffold (if --with-dbt)
   - Each write is idempotent: if file exists, leave it (and print "kept existing X")
   - Exception: convention-inferred carve/conventions.md is overwritten on every init (it's regenerable; never user-edited per the spec 06 convention)

5. Run convention inference (brownfield mode only)
   - For dbt brownfield: parse dbt_project.yml, walk models/, write summary to carve/conventions.md
   - For dlt brownfield: scan el/ (or resolved dlt path), .dlt/, write summary additively
   - Time-budget: 5 minutes per PRD §7.1; longer projects emit warnings but don't fail

6. Bootstrap auth
   - Generate a random 32-byte token, base64-url-encode
   - Write to .carve/token (mode 0600, gitignored)
   - If Postgres reachable: hash + insert into tokens table
   - If not reachable (deferred bootstrap): the token sits in .carve/token; first carve serve writes it to the DB on startup

7. Git init (if --no-git-init not set and no .git/ exists)
   - git init
   - No initial commit; user controls when to commit

8. Print next steps
   - For bundled compose: "Run `docker compose up -d` then `carve serve`."
   - For external Postgres: "Run `carve serve`."
   - "Then try `carve plan 'ingest the Hacker News top stories'`."
   - For orchestration-only mode (PRD §6.2 mode 2): different next steps, focused on `carve plan 'schedule my existing X pipeline daily'`
```

### Brownfield detection details

`src/carve/init/detect.py` implements:

- **dbt detection**: search `<cwd>/dbt_project.yml`, then `<cwd>/*/dbt_project.yml` (one level down only — per spec 03's resolution rules). On match: parse to confirm it's valid YAML with a `name` key. If multiple matches, list them all and require an explicit `--dbt-path` to disambiguate.
- **dlt detection**:
  - `<cwd>/.dlt/` exists → brownfield dlt
  - `<cwd>/el/<name>/__init__.py` files with `import dlt` or `@dlt.` decorators (parse via AST, no execution) → brownfield dlt
  - `<cwd>/*.py` with `@dlt.source`/`@dlt.resource`/`@dlt.pipeline` decorators at the project root → brownfield dlt (less common shape)
- **Provenance distinction**: detected dlt files are scanned for the Carve provenance header (per spec 03). Those *with* the header are Carve-generated artifacts from a prior install; those *without* are user-authored (PRD §6.2 mode 2). This distinction lives in the **files themselves** (header present/absent), read on demand by later phases — it is **not** recorded in `carve.toml` (simple-mode components aren't enumerated there). E.g., the orchestrator routes "schedule my X pipeline" to the pipeline engineer if X is user-authored, or to the DLT engineer if it's Carve-generated and needs modification.

### Interactive prompts

When stdout is a TTY and `--non-interactive` is not set, init prompts for unresolved decisions. Prompts use a TUI library (e.g., `questionary` or `rich.prompt`) so they look polished but degrade cleanly in dumb terminals.

Prompt examples:

```
✓ Detected dbt project at ./dbt_project.yml (carve will integrate, not modify)
✗ No dlt code detected.

How would you like Carve to handle dlt?
  > Scaffold a sample (--with-dlt)
    Use an existing dlt repo at a local path (--dlt-path)
    Use an existing dlt repo at a git URL (--dlt-url)
    Skip dlt for now

Postgres setup:
  > Use the bundled docker-compose (recommended; Docker required)
    Connect to an external Postgres (managed RDS, Cloud SQL, etc.)

Default target name [dev]:
Destination kind for the default target [snowflake]:
```

When the user has provided flags for some but not all decisions, only the unresolved ones get prompted.

### Idempotency on re-init

Re-running `carve init` in a directory with an existing `carve.toml`:

- Re-runs detection — surfaces any new brownfield projects (e.g., user added a dbt subdirectory after the first init)
- Does **not** overwrite any of: `carve.toml`, `carve/connections.toml`, `carve/runtime.toml`, `carve/standards.md`, `carve/decisions.md`, `docker-compose.yml`, `.env`, `dbt_project.yml`, `.dlt/config.toml` (if it exists), user-authored files in `el/`
- **Does** refresh: `carve/conventions.md` (re-runs inference; this file is owned by Carve), `.env.example` (template), `.gitignore` (Carve's section, identified by marker comments)
- Prints a summary of what was kept, what was refreshed, and any new detections

### Non-interactive (CI) mode

`--non-interactive` requires all decisions to be supplied via flags or env vars. Each unresolved decision triggers a clean error pointing at the relevant flag. The exit code is 3 (config error per spec 01).

Example: `carve init --non-interactive --external-postgres "${DATABASE_URL}" --dbt-path ./dbt --default-target dev` is sufficient for a CI pipeline.

### Convention inference

`src/carve/integrations/dbt/convention_inference.py` and the dlt counterpart each produce a markdown section. The writer combines them into `carve/conventions.md` with deterministic structure:

```markdown
# Project conventions

> Inferred by Carve from this project's existing code. Re-run inference with
> `carve memory refresh` (spec 06). User-edited overrides should go in
> `carve/standards.md`, which takes precedence over inferred conventions.

## dbt conventions

- Model naming: stg_*, int_*, fct_*, dim_* (inferred from 47 models)
- Layering: models/staging/, models/intermediate/, models/marts/
- Default materializations: views in staging, tables in marts (3 tables, 12 views)
- Common tests: not_null, unique on primary keys; relationships on foreign keys
- Sources: 5 sources declared in models/staging/sources.yml; raw_* schema convention

## dlt conventions

- No dlt code detected yet (run `carve plan "ingest X"` to author the first pipeline)

# (or, if dlt code is present:)

- Destinations: snowflake (raw_*), duckdb (test_*)
- Write dispositions: 6 merge, 2 append, 1 replace
- Source naming: snake_case, ends in _source (12 sources)
- Schema contracts: 4 sources use strict schema_contract
```

Inference is bounded by file count (default cap 5000 files scanned) and time (5 minutes total per PRD §7.1). On hitting limits, partial inference is written with a clearly-marked "PARTIAL INFERENCE" header so users know to investigate or re-run with raised limits.

### Templates

The Jinja templates render with:

- Project metadata (name, slug, created_at)
- Resolved decisions (postgres URL, target name, destination kind, etc.)
- Detected brownfield info (paths, names)

Examples:

`carve.toml.j2`:
```toml
# Generated by `carve init` on {{ created_at }}. Edit freely.
[project]
name = "{{ project_name }}"
default_target = "{{ default_target }}"
carve_version = "{{ carve_version }}"

[state_store]
url = "${DATABASE_URL}"

# SIMPLE MODE writes NO [components.*] blocks: same-repo dbt/dlt (detected or
# scaffolded) is discovered by convention (each el/<name>/ is a dlt component; the
# detected dbt project is a dbt component — spec 03). A block is rendered ONLY for a
# component that lives elsewhere (--dbt-path/--dbt-url/--dlt-path/--dlt-url).
{% for c in components if c.mode != "same-repo" %}
[components.{{ c.name }}]
type = "{{ c.type }}"                 # "dlt" | "dbt"
mode = "{{ c.mode }}"                 # "separate-local" | "separate-remote"
{% if c.mode == "separate-local" %}path = "{{ c.path }}"{% endif %}
{% if c.mode == "separate-remote" %}url = "{{ c.url }}"{% endif %}
{% if c.mode == "separate-remote" and c.ref %}ref = "{{ c.ref }}"{% endif %}
{% if c.mode == "separate-remote" and not c.ref %}branch = "{{ c.branch }}"{% endif %}
{% endfor %}
```

`standards.md.j2` (the empty-with-template version):
```markdown
# Team standards

> User-authored. Read by agents on every invocation as part of pre-scoped context.
> Standards **override** conventions inferred by Carve (in `conventions.md`) where they conflict.

## Examples

Replace these with your team's rules. The more specific you can be, the more
predictable the agent's output will be.

- "All raw schemas use snake_case table names."
- "Stripe data must always be loaded incrementally, not full-refresh."
- "Use merge dispositions on PK for any pipeline pulling from a SaaS API."
- "All marts must have a `unique` test on the grain column."

(Delete this template content and replace with your actual standards.)
```

`decisions.md.j2`:
```markdown
# Decisions

> Append-only, dated. Records durable choices the team has made, with rationale and reviewers.
> Read by `carve ask` (spec 12) for "why did we do X?" investigations.

## Format

```
## YYYY-MM-DD — Short title

**Decision:** What we decided.
**Rationale:** Why.
**Reviewers:** alice@, bob@
**Impact:** Which pipelines / models / schemas this affects.
```

## (No decisions recorded yet)
```

### Backwards compat: `--migrate-from-targets`

For users who started with the pre-positioning `targets/<target>/el/<artifact>/` layout (a small set of M1.1 / pre-Pillar-1.1 users), this flag triggers the migration described in [spec 03's *Migration from M1.1*](./layout.md) section. Init creates a git commit "Pre-Carve-v0.1-layout-migration" first so the user can revert. The migration is one-shot — re-running is a no-op.

## Tests

- **Unit**: detection module identifies dbt, dlt, git, docker presence correctly across representative directory shapes
- **Unit**: scaffold module is idempotent (running scaffold twice produces identical output)
- **Unit**: convention inference produces deterministic markdown given identical input
- **Unit**: prompts module degrades cleanly on non-TTY stdin (raises non-interactive error)
- **Integration (greenfield)**: fresh tempdir + `carve init` (with interactive prompts mocked) → expected layout per spec 03
- **Integration (brownfield dbt, simple mode)**: tempdir with `dbt_project.yml` + `models/staging/stg_orders.sql` etc. → init writes **no** `[components.*]` block (same-repo = convention discovery, spec 03); `carve components show` lists the discovered dbt component; conventions.md mentions the `stg_*` pattern
- **Integration (brownfield dlt, simple mode)**: tempdir with `el/stripe_charges/__init__.py` (user-authored, no Carve provenance) → init writes **no** `[components.*]` block; the dlt component is convention-discovered; conventions.md mentions dlt patterns; the user-authored-vs-generated distinction is the (absent) provenance header in the file, **not** a `carve.toml` record
- **Integration (mixed brownfield)**: dbt + dlt both present same-repo → both convention-discovered (no `[components.*]` blocks written); conventions covers both
- **Integration (separate-remote)**: `--dbt-url <fixture-url>` writes a `[components.analytics]` block (`type = "dbt"`, `mode = "separate-remote"`) and triggers the initial workspace clone via the cache from spec 03
- **Integration (idempotency)**: running `carve init` twice in the same directory leaves user-editable files unchanged; only `conventions.md` and `.env.example` get refreshed
- **Integration (non-interactive)**: `carve init --non-interactive --external-postgres ... --default-target dev` in CI completes without prompting; missing required flags produces exit code 3 with a helpful message
- **Integration (migrate-from-targets)**: synthetic `targets/dev/el/iowa_liquor/` tree → `--migrate-from-targets` produces the flat layout, with a pre-migration git commit recorded

## Acceptance

- `carve init` in a fresh tempdir completes in under 30 seconds (greenfield) and produces a project that immediately accepts `carve plan "test goal"`
- `carve init` in a brownfield dbt project (10–100 models) completes in under 5 minutes including convention inference
- All four orthogonal axes (postgres × dbt × dlt × memory) compose cleanly — any combination of valid choices produces a working project
- Re-running `carve init` is non-destructive: no user-editable files are clobbered
- `--non-interactive` mode works in CI with explicit flags
- `--migrate-from-targets` migrates an existing M1.1-shape project without data loss
- Brownfield detection correctly distinguishes Carve-generated vs user-authored dlt artifacts via provenance headers
- `docs/getting-started-*.md` (three variants) each walk a user from `carve init` to a successful `carve plan` in under 15 minutes

## Design notes

- **Why orthogonal axes instead of a unified "mode" flag?** Because the four axes really are independent. A user can have managed Postgres but greenfield dlt; or bundled Postgres but separate-remote dbt; or any other combination. Forcing them into a fixed set of "modes" creates a combinatorial explosion of flag names. Independent axes are simpler to document and easier to extend.
- **Why one big confirmation prompt instead of per-decision confirmation?** Per-decision confirmation is high-friction and pushes the user into "yes-mode" by the third prompt. A single end-of-resolution confirmation lets the user see the full picture and reject as one unit if anything looks wrong.
- **Why scaffold convention inference into spec 05 even though `carve memory refresh` lives in spec 06?** Because brownfield init needs to produce `carve/conventions.md` immediately — the user expects a usable project after one `carve init`. The inference engine itself is independent of the memory CLI surface; splitting them across two specs lets spec 05 ship a complete init flow and spec 06 ship the read/edit/refresh CLI commands on top.
- **Why not write a real `.env` on init?** Because real `.env` files contain real secrets. Init writes `.env.example` with placeholders + comments; the user copies it to `.env` and fills in values. This matches the convention of most modern projects (Django, Rails, Next.js, etc.) and avoids creating a file that pretends to have credentials.
- **Why does init bootstrap an API token but `carve serve` may still need to bootstrap it again?** Because Postgres may not be reachable at init time (the user hasn't run `docker compose up -d` yet). Init writes the token to `.carve/token` (mode 0600) immediately so the CLI can use it; the hash insert into the `tokens` table is deferred to whichever process first reaches the DB.
- **Why is `--migrate-from-targets` a flag on `init` and not a separate `carve migrate-layout` command?** Because the legacy layout is detected during init's detection phase; folding the migration into init makes the user's mental model simpler ("I run init and Carve figures out what needs to happen"). A separate command would split the same logic across two entry points.

## Open questions

- **TUI library choice (`questionary` vs `rich.prompt` vs raw Typer prompts).** *Implementation default.* Use `questionary` for prompts that need richer UX (multi-select, conditional follow-ups); fall back to `rich.prompt` for simple yes/no. Both are well-maintained and not heavyweight.
- **Whether `carve init` should auto-run `docker compose up -d` for the user.** *Implementation default.* No — explicit user action keeps the model simple ("init writes files; you start services"). The next-steps message clearly says to run it. Auto-running adds surprise behavior and complicates the case where the user wants to inspect the compose file first.
- **Convention inference scope when separate-remote dbt repo is unreachable at init time.** *Implementation default.* Init emits a warning and proceeds without inference; `carve memory refresh` (spec 06) can be run later once the remote is reachable. Init does not block on remote sync failures.
- **First-run telemetry opt-in prompt.** *Strategy-required.* Should `carve init` prompt the user to opt into anonymous usage telemetry? If yes, what data, opt-in default? This is a community-trust decision worth thinking about deliberately, but it doesn't block v0.1.0 — defer to a follow-up.
