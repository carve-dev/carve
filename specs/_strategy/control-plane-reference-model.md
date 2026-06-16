# Control-plane reference model (draft proposal)

> **Status:** Draft proposal for Nate's reaction — not yet a spec. Makes the [`2026-06-control-plane.md`](./2026-06-control-plane.md) decisions concrete: the `carve.toml`-as-control-plane config, named components with topology + pins, pipelines that reference components **by name**, the optional `[schedule]` seed, and the three-tier code/data split. Once accepted, this drives revisions to specs 03 (layout/topology), 04 (EL agent), 08 (pipeline composition), and 14 (deploy).

## The one idea everything hangs on

**Pipelines reference components by NAME. Where a name resolves — a local folder, or a remote repo at a pinned ref — is a separate, per-component setting.** That indirection is what lets simple mode hide all the machinery, lets graduation move code without touching pipelines, and lets the runtime pin versions. (It's Dagster's code-location trick, scoped to dlt/dbt/sql.)

## Three artifacts, three owners

| Artifact | Holds | Owner |
|---|---|---|
| `carve.toml` | control-plane config: project metadata, connections, and **component references** (`[components.*]`) | **code** (version-controlled) |
| `pipelines/<name>.toml` | the **composition**: steps referencing components by name, the DAG (`depends_on`), and an optional `[seed_schedule]` | **code** (version-controlled) |
| state store (Postgres) | the live **schedule**, jobs, runs, deploys, workspace sync state | **data** (set via CLI/API/UI) |

The state store is a *materialized projection* of the code (reconciled on `serve` boot + loop) **for the definition only**. The schedule is seeded from code once, then owned as data.

## Components — `[components.<name>]`

Generalizes today's singular `[dbt]` / `[dlt]` blocks into N named, typed components:

```toml
[components.stripe_charges]
type = "dlt"                 # dlt | dbt   (sql steps reference files, not components)
mode = "separate-remote"     # same-repo | separate-local | separate-remote
url  = "git@github.com:acme/ingest-stripe.git"   # separate-remote only
ref  = "v1.4.2"              # OPTIONAL pin (commit/tag). Omit → track the branch HEAD.
                             # branch = "main"  # when tracking a branch instead of a pin

[components.analytics]
type = "dbt"
mode = "separate-remote"
url  = "git@github.com:acme/analytics.git"
ref  = "9f3a1c7"
```

- **Pin is per-component** (one resolved version per component, used by every pipeline that references it). Per-step version overrides are a possible later addition, not v0.1.
- **Simple mode writes none of this.** By convention: each `el/<name>/` dir is a `dlt` component named `<name>`; the detected dbt project is a `dbt` component. `[components.*]` blocks materialize only when you split a component out.

## Pipelines — reference by name

```toml
name = "stripe"

[[steps]]
id = "ingest"
type = "dlt"
component = "stripe_charges"      # → el/stripe_charges/ (simple) OR the remote repo @ ref (multi)

[[steps]]
id = "stage"
type = "dbt"
depends_on = ["ingest"]           # ingest-before-transform falls out of the DAG
component = "analytics"           # optional in simple mode (single dbt project); see open point
select = "stg_stripe+"

[[steps]]
id = "report"
type = "sql"
depends_on = ["stage"]
file = "sql/daily_report.sql"     # sql steps reference a file + connection, not a component
connection = "prod"

# Optional SEED — applied ONLY at first registration. The live schedule is data thereafter;
# editing this block is a no-op unless you `carve schedule reseed stripe`.
[seed_schedule]
cron     = "0 2 * * *"
timezone = "UTC"
```

`component` replaces today's `artifact` field on dlt steps (unifies dlt + dbt step references under one name-based key).

## Simple mode vs multi mode (same pipeline, different resolution)

- **Simple mode:** one repo. `carve.toml` has *no* `[components.*]`. `component = "stripe_charges"` resolves to `./el/stripe_charges/`; the dbt step resolves to the one detected dbt project. No pins. AI just builds + schedules.
- **Multi mode:** `[components.stripe_charges]` and `[components.analytics]` point at separate-remote repos with pins. **The `pipelines/*.toml` is unchanged** — only the resolution behind the names changed.

## Graduation (simple → multi), concretely

To move the dbt project into the analytics team's own repo:

1. Extract the dbt code to its new repo (a git move/push — user action; Carve can offer a helper).
2. `carve component analytics --separate-remote git@github.com:acme/analytics.git --ref 9f3a1c7`
   — writes the `[components.analytics]` block, clones into `.carve/workspaces/`, validates, and (if simple-mode steps omitted the name) backfills `component = "analytics"` into the dbt steps.
3. Done. Schedules keep firing, run history intact, no re-run, no state migration. Reversible (`--same-repo`), incremental (per component).

"Born multi" (`carve init --dbt-url <url>`) is the *same* machinery, triggered at init instead of later.

## What this changes vs current specs

- **Spec 03 (layout/topology):** generalize singular `[dbt]`/`[dlt]` → `[components.<name>]` (typed, N-of-them); add the optional `ref` pin; `carve.toml` reframed as control-plane config; `[el]` is no longer special (it's just `type = "dlt"` components). Convention-based simple-mode discovery + `carve components show`.
- **Spec 04 (EL agent):** authors *into* a named dlt component (its repo), emits dependency hints by component name.
- **Spec 08 (pipeline composition):** steps reference `component = "<name>"` (rename from `artifact`); `[seed_schedule]` (renamed from `[schedule]`) is a *seed*, not the source of truth; schedule state moves fully to the `schedules` table (drops UC2's override-precedence machinery); add the `carve component`, `carve components show`, `carve schedule reseed` surfaces.
- **Spec 14 (deploy):** name-indirection + per-component pins make per-component deploy + the linked-PR cross-repo flow first-class (revisit the separate-remote deferral).
- **Spec 07 (runtime):** reconciler reconciles the *definition* only; the scheduler reads the `schedules` table as data (seeded once).

## Open points within this proposal (small)

- **Simple-mode component naming:** do simple-mode steps omit `component` (and graduation backfills it), or always name it (zero graduation-churn, slightly less hidden)? Leaning: omit in simple mode, backfill on graduation — keeps simple mode cleanest.
- **`ref` vs `branch`:** allow either on a component (`ref` = pinned, `branch` = track HEAD), default to tracking the repo's default branch when neither is set? Leaning: yes.
- **`sql` components:** sql steps reference files in the control-plane repo today. Do ad-hoc sql files ever become a named/separable component, or stay inline? Leaning: stay inline for v0.1.
