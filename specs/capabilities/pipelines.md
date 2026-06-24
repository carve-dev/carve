# Multi-step pipeline composition: TOML schema, step DAG, dlt/dbt/sql step types

> **Revised for the control-plane model** ([../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md), concrete shapes in [../_strategy/control-plane-reference-model.md](../_strategy/control-plane-reference-model.md)). This spec is the **binding contract**: pipeline steps reference components **by name** (`component = "<name>"`), name-based indirection resolves a name to a local path (simple mode) or a remote repo @ pinned ref (multi mode), the `[schedule]` block becomes a `[seed_schedule]` *seed* (the live schedule is data, owned by the runtime per spec 07), and the spec gains the `carve component` / `carve components show` / `carve schedule reseed` surfaces.

> **Also revised for the AI-harness model** ([../_strategy/2026-06-ai-harness.md](../_strategy/2026-06-ai-harness.md)). The control-plane runtime specialist is reframed as the **pipeline engineer**: a declarative **subagent** (`src/carve/core/agents/builtin/pipeline-engineer.md`, the spec-16 markdown-frontmatter format) running on the harness (spec 15), armed with terminal-grade tools (`edit` scoped to `pipelines/**`, `grep`, plus the `pipeline_inspect` / `list_components` skills), under the `build` permission mode, that the orchestrator `delegate`s pipeline-composition tasks to and that **verifies** a composition by executing it (`carve pipelines validate` + a dev run). All control-plane content below — `component = "<name>"`, name indirection, `[seed_schedule]`, graduation, the definition reconciler, the step DAG/executors — is unchanged.

> Plugs the three step types (`dlt`, `dbt`, `sql`) into the runtime framework from spec 07; defines the pipeline TOML schema; ships the **pipeline engineer** subagent that authors `pipelines/<name>.toml` entries. Per [PRD §6.10 pipeline composition](../PRD.md), [ARCHITECTURE §4.6 step executors](../ARCHITECTURE.md), [ARCHITECTURE §4.7 failure modes](../ARCHITECTURE.md), and [ARCHITECTURE §10.2/10.3 dlt and dbt invocation](../ARCHITECTURE.md).

## Status

- **Status:** Drafting
- **Depends on:** [layout](./layout.md), [dlt-engineer](./dlt-engineer.md), [runtime](./runtime.md), [harness](./harness.md) (the pipeline engineer is a subagent on this harness — `delegate`, terminal tools, permission modes, the verification loop), [extensibility](./extensibility.md) (the declarative agent format `builtin/pipeline-engineer.md` loads through)
- **Blocks:** [rest-api](./rest-api.md) (REST surface for pipelines), [ui](./ui.md) (UI renders pipeline definitions and step status)
- **Soft depends on:** [memory](./memory.md) (the pipeline engineer reads memory files via the spec-06 loader), [sql](./sql.md) (the `sql` tool layer the `sql` step type and the engineer's schema checks ride on)

## Goal

Bring the runtime to life with real pipelines:

1. **The `pipelines/<name>.toml` schema** — pipeline metadata, an optional `[seed_schedule]` block (the schedule *seed*, not the source of truth), ordered `[[steps]]` tables that form a DAG, where `dlt` and `dbt` steps reference a component **by name** (`component = "<name>"`)
2. **The step DAG executor** — topological walk with intra-pipeline parallelism, per-step failure mode enforcement, Jinja templating for cross-step outputs
3. **The three concrete step type implementations** — `dlt`, `dbt`, `sql` — each implementing the `StepExecutor` protocol from spec 07. `dlt`/`dbt` steps resolve their component name through the spec-03 resolver (local path in simple mode, remote workspace @ pinned ref in multi mode); `sql` steps stay inline (`file` + `connection`).
4. **Failure mode framework** — `fail`, `warn`, `continue`, `retry` (with attempts + backoff), `skip_downstream`
5. **The pipeline engineer subagent** — a declarative agent (`builtin/pipeline-engineer.md`) the orchestrator `delegate`s pipeline-composition tasks to; it authors/modifies `pipelines/<name>.toml` with `edit` (scoped to `pipelines/**`) and **verifies** the result by execution (`carve pipelines validate` + a dev run)
6. **CLI commands** for pipeline management (`carve pipelines list`, `show`, `validate`, `diff`), component graduation/inspection (`carve component`, `carve components show`), and re-seeding the schedule from code (`carve schedule reseed`)

After this spec lands, a user can describe a multi-step pipeline ("ingest Stripe, then run the stg_stripe dbt models, then refresh the search index via SQL"), `carve plan` produces a multi-step composition, `carve build` materializes the dlt component code + pipeline TOML, and the runtime schedules and executes it end-to-end. The same `pipelines/<name>.toml` is **identical across simple and multi mode** — only the resolution behind the component names changes.

## Out of scope

- Step types beyond `dlt`, `dbt`, `sql` (`shell`, `http`, `python`, `agent`, `approval` are deferred per [PRD §4.2](../PRD.md))
- Conditional branching, fan-out, or asset-graph features (out per [ARCHITECTURE §5.6 narrow runtime](../ARCHITECTURE.md))
- First-class backfills (out per same; manual `carve run --target prod --param ...` is the workaround)
- The REST/MCP surface for pipelines (lives in spec 09)
- The static UI's pipeline-detail view (lives in spec 11)
- **The `[components.<name>]` schema, `carve.toml` control-plane reframe, and the topology/locator resolution itself** — those live in [layout](./layout.md). This spec *consumes* the resolver and references components by name; it does not define the `[components.*]` block or the simple-mode discovery convention.
- **The schedule as live data** (`schedules` table, `carve schedule list/show/pause/resume`, the scheduler that reads it) — that lives in [runtime](./runtime.md). This spec ships only the `[seed_schedule]` *seed* applied at first registration and the `carve schedule reseed` command that re-applies it.
- **Deploy behavior** (`carve deploy`, per-component promotion, the cross-repo linked-PR flow) — unchanged here and pending the Wave 2 deploy revision of [deploy](./deploy.md). Graduation (`carve component <name> --separate-remote …`) writes the component block and validates it (this spec); promoting that component's repo through an environment is a deploy concern (spec 14).

## Behavior

### Pipeline TOML schema

`pipelines/<name>.toml`:

```toml
# Pipeline metadata
[pipeline]
description = "Stripe charges ingest + staging transforms + search refresh"
owner = "data-team"

# Schedule SEED — applied ONLY at first registration (spec 07 owns the live schedule as data).
# Editing this block is a no-op thereafter unless you run `carve schedule reseed <pipeline>`.
[seed_schedule]
cron = "0 2 * * *"               # 2am daily
timezone = "UTC"                  # reference-model field
target = "prod"                   # which target the seeded schedule runs against (see open questions)

# Steps (DAG)
[[steps]]
id = "ingest_stripe"
type = "dlt"
component = "stripe_charges"      # → el/stripe_charges/ (simple) OR the remote repo @ ref (multi)
depends_on = []
[steps.failure_mode]
mode = "retry"
max_attempts = 3
backoff = "exponential"

[[steps]]
id = "stage_stripe"
type = "dbt"
component = "analytics"           # optional in simple mode (single detected dbt project); backfilled on graduation
command = "build"
select = "stg_stripe_charges+"    # dbt selector syntax
depends_on = ["ingest_stripe"]
[steps.failure_mode]
mode = "fail"                     # default; included here for clarity

[[steps]]
id = "refresh_search"
type = "sql"
file = "sql/refresh_charges_search.sql"   # sql steps reference a file + connection, NOT a named component
connection = "prod"
depends_on = ["stage_stripe"]
[steps.failure_mode]
mode = "warn"                     # bad search refresh shouldn't fail the whole run

# Cross-step Jinja example
[[steps]]
id = "notify_count"
type = "sql"
file = "sql/notify_loaded_count.sql"
connection = "prod"
depends_on = ["ingest_stripe"]
[steps.jinja_vars]
# A dlt step's outputs are {tables, schema_changes, failed_jobs} — the on-disk load
# package's keys. (Per-resource `rows_loaded` is NOT among them: it lives only in the
# in-process dlt trace, not the persisted package — see "Step executor: dlt".)
loaded_tables = "{{ steps.ingest_stripe.outputs.tables | join(', ') }}"
```

> **Updated during implementation (2026-06-24):** this example originally referenced `{{ steps.ingest_stripe.outputs.rows_loaded }}`, but a `dlt` step's shipped `outputs` are `{tables, schema_changes, failed_jobs}` (the on-disk load-package keys) — `rows_loaded` is not among them (it lives only in the in-process dlt trace). Under `StrictUndefined` the old example would be a render error, so it now references a real output key (`tables`).

**This TOML is identical across simple and multi mode.** In simple mode, `carve.toml` has no `[components.*]` blocks: `component = "stripe_charges"` resolves by convention to `./el/stripe_charges/`, and the dbt step's `component` may be **omitted** (it resolves to the single detected dbt project). In multi mode, `[components.stripe_charges]` and `[components.analytics]` (defined in `carve.toml` per spec 03) point those names at separate-remote repos pinned to a `ref` — but the pipeline file above does not change. Graduation backfills the omitted dbt-step `component` name (see *Component resolution & graduation* below).

Pydantic schema (in `src/carve/core/config/pipeline_schema.py`):

```python
class PipelineMeta(BaseModel):
    description: str = ""
    owner: str = ""

class SeedSchedule(BaseModel):
    # SEED only — applied to the `schedules` table at first registration (spec 07).
    # NOT the live source of truth; editing this is a no-op unless `carve schedule reseed`.
    # `paused`/`enabled` is deliberately absent: pause/resume is live data, set via CLI/API/UI.
    cron: str                      # validated via croniter on load
    timezone: str = "UTC"          # reference-model field
    target: str = "prod"           # target the seeded schedule runs against (see open questions)

class FailureMode(BaseModel):
    mode: Literal["fail", "warn", "continue", "retry", "skip_downstream"] = "fail"
    max_attempts: int = 1          # only relevant when mode == "retry"
    backoff: Literal["exponential", "linear", "fixed"] = "exponential"
    initial_delay_s: float = 5.0
    max_delay_s: float = 300.0

class PipelineStep(BaseModel):
    id: str                        # unique within pipeline
    type: Literal["dlt", "dbt", "sql"]
    depends_on: list[str] = []
    failure_mode: FailureMode = Field(default_factory=FailureMode)
    jinja_vars: dict[str, str] = {}    # rendered against the cross-step Jinja context
    # Type-specific config goes in subclasses; see below

class DltStepConfig(BaseModel):
    type: Literal["dlt"] = "dlt"
    component: str                 # NAME of a dlt component; resolves to el/<name>/ (simple) or remote @ ref (multi)
    write_disposition: Optional[Literal["append", "replace", "merge"]] = None  # override config.toml
    resource_select: Optional[list[str]] = None    # subset of resources to run

class DbtStepConfig(BaseModel):
    type: Literal["dbt"] = "dbt"
    component: Optional[str] = None  # NAME of a dbt component; OPTIONAL in simple mode (single detected dbt project), backfilled on graduation
    command: Literal["build", "run", "test", "snapshot", "seed"] = "build"
    select: Optional[str] = None
    exclude: Optional[str] = None
    vars: dict[str, Any] = {}
    full_refresh: bool = False

class SqlStepConfig(BaseModel):
    type: Literal["sql"] = "sql"   # sql stays inline: a file + connection, NOT a named component
    file: str                      # path relative to project root
    connection: str                # target name from carve/connections.toml

class Pipeline(BaseModel):
    name: str                      # derived from filename, not in TOML
    pipeline: PipelineMeta = Field(default_factory=PipelineMeta)
    seed_schedule: Optional[SeedSchedule] = None
    steps: list[PipelineStep] = []
```

The `component` field on `dlt`/`dbt` steps **replaces the old `artifact` field** (which lived only on dlt steps), unifying dlt + dbt step references under one name-based key — per the control-plane reference model. A `dlt` step's `component` is required; a `dbt` step's `component` is optional (omitting it means "the single detected dbt project" in simple mode). `sql` steps are unchanged: they reference a file + connection inline.

> **Updated during implementation (2026-06-24):** the loader also enforces **step-type / component-type agreement** — a `dbt` step may not reference a `dlt` component (and vice versa), since the executor would otherwise dispatch the wrong engine at run time. An adversarial review surfaced this gap; it is now a `load_pipeline` validation error alongside the resolvability check.

Loading validates: unique step ids, valid `depends_on` refs (all referenced ids exist), no cycles, valid cron (if `[seed_schedule]` present), valid type-specific configs, and — for `dlt`/`dbt` steps — that the referenced `component` name **resolves** (via the spec-03 resolver: an `el/<name>/` directory or a `[components.<name>]` block; the omitted dbt name resolves to the single detected dbt project) **and that the resolved component's type matches the step's type** (a `dlt` step resolves to a `dlt` component, a `dbt` step to a `dbt` component). An unresolvable component name — or a step-type/component-type mismatch — is a validation error surfaced by `carve pipelines validate`.

### Component resolution (name-based indirection)

A step's `component = "<name>"` is a **name**, not a path. Resolution is a separate per-component concern owned by spec 03's locator and the `[components.<name>]` block — this spec consumes it:

- **Simple mode** (`carve.toml` has no `[components.*]`): the name resolves by convention. A `dlt` step's `component = "stripe_charges"` → `paths.el_dir / "stripe_charges"`. A `dbt` step's `component` (named or omitted) → the single detected dbt project (`paths.dbt_project_path`). No pins.
- **Multi mode** (`[components.<name>]` present in `carve.toml`): the name resolves through the locator to a workspace clone at the component's pinned `ref` (`.carve/workspaces/<name>/`, per spec 03), with the same `type`/`mode`/`url`/`ref`/`branch` fields the reference model defines.

The step executors below ask the resolver for a concrete path at execution time; the pipeline TOML is unchanged between modes. Because the pin is **per-component** (one resolved version used by every pipeline that references it), two pipelines referencing `component = "analytics"` always run the same pinned dbt code.

### Component graduation (simple → multi)

Moving a component into its own repo is a one-command operation that touches `carve.toml`, not the pipeline TOMLs:

```
carve component <name> --separate-remote <url> [--ref <pin>] [--branch <name>]
carve component <name> --separate-local <path>
carve component <name> --same-repo                 # reverse graduation
```

`carve component <name> --separate-remote …` (per the reference model's graduation flow):

1. Writes the `[components.<name>]` block into `carve.toml` (`type` inferred from the existing convention — a `dlt` component for an `el/<name>/` dir, a `dbt` component for the detected dbt project; `mode = "separate-remote"`, `url`, optional `ref`/`branch`).
2. Clones the repo into `.carve/workspaces/<name>/` (via spec 03's `sync_workspace`) and validates it resolves.
3. **Backfills** `component = "<name>"` into any `dbt` steps that had omitted it (simple-mode convenience), so the now-multi project still resolves by name.

This is a control-plane edit only — **no pipeline rewrites beyond the backfill, no state migration, no re-runs; schedules keep firing and run history is intact.** It is reversible (`--same-repo`) and incremental (per component). "Born multi" (`carve init --dbt-url <url>`, spec 03/05) is the same machinery triggered at init. Extracting the component's code to its new repo is a user git action; Carve may offer a helper but does not own the move. Promoting the new component repo through an environment is **deploy** (spec 14, Wave 2) — out of scope here.

### Schedule seed semantics

`[seed_schedule]` is a **seed**, not the source of truth. Per the three-tier ownership in [../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md): the pipeline *definition* (steps, DAG, component refs) is code reconciled into state; the *schedule* (cron, cadence, enabled/paused) is **data** in the `schedules` table (spec 07), set via CLI/API/UI.

- At **first registration** of a pipeline (the definition reconciler — `src/carve/runtime/reconciler.py`, shipped by this spec and invoked by `carve serve` on boot + loop — sees a `pipelines/<name>.toml` with no corresponding `schedules` row), `[seed_schedule]`'s `cron`/`timezone`/`target` are written into the `schedules` table once. The same reconciler keeps the pipeline *definition* (steps/DAG/component refs) in sync with the TOML; it never touches an existing `schedules` row.
- **Thereafter the live schedule is data.** Editing `[seed_schedule]` in the TOML is a **no-op** — the reconciler never overwrites the schedule from code. Pause/resume and cron changes go through `carve schedule pause/resume`/the API (spec 07), audited via the `schedule_changes` log + the `schedule` RBAC scope, not git.
- `carve schedule reseed <pipeline>` is the explicit escape hatch: it re-applies the current `[seed_schedule]` block to the `schedules` row for that pipeline, overwriting the live values. Without it, `[seed_schedule]` edits stay inert.

This **reverses UC2's prior decision** that schedule changes flow through plan/build/deploy/PR, and **deletes UC2's code-vs-override TTL-precedence machinery** — the reconciler reconciles the definition only and never touches the schedule. Tradeoff: schedules reconstitute from the (backed-up) state store + the code seed, not from `git clone`. A pipeline with **no** `[seed_schedule]` registers unscheduled (manual/API-triggered only) until a schedule is set as data.

### Step DAG execution

`src/carve/runtime/pipeline_dag.py`:

```python
class PipelineDAG:
    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        self._validate_no_cycles()
        self._topological_order = self._compute_topological_order()

    def ready_steps(self, completed: set[str], failed: set[str], skipped: set[str]) -> list[PipelineStep]:
        """Steps whose dependencies are all completed (success or fall-through per failure mode)
        AND that aren't themselves completed/failed/skipped."""
        ...

    def downstream_of(self, step_id: str) -> set[str]:
        """All transitively-dependent step ids."""
        ...
```

`src/carve/runtime/execute_pipeline.py` is the function the worker (spec 07) calls:

```python
async def execute_pipeline(run: Run, *, paths: ProjectPaths, registry: StepExecutorRegistry) -> RunResult:
    pipeline = load_pipeline(paths.pipelines_dir / f"{run.pipeline}.toml")
    dag = PipelineDAG(pipeline)

    completed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    outputs: dict[str, dict] = {}   # step_id → outputs dict

    while True:
        ready = dag.ready_steps(completed, failed, skipped)
        if not ready and not still_running(): break

        # Launch ready steps in parallel up to the worker's intra-pipeline slot count
        tasks = [run_step(step, run, paths, registry, outputs) for step in ready[:available_slots]]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        for step, result in zip(ready, results):
            apply_failure_mode(step, result, dag, completed, failed, skipped)
            if result.status == "succeeded":
                outputs[step.id] = result.outputs

    return RunResult.from_step_results(completed, failed, skipped, outputs)
```

`run_step` renders Jinja vars against the running `outputs` dict, dispatches to the appropriate executor, captures logs, writes a `step_runs` row, and emits `step.started`/`step.completed`/`step.failed` events (spec 07).

### Failure modes

`src/carve/runtime/failure_modes.py` translates each mode into the runtime's behavior:

| Mode             | On step failure                                                  | On step succeed |
|------------------|------------------------------------------------------------------|------------------|
| `fail` (default) | Mark pipeline run failed; don't start any unstarted steps        | Add to completed |
| `warn`           | Record warning + the error; continue scheduling downstream       | Add to completed |
| `continue`       | Record failure; continue scheduling downstream                   | Add to completed |
| `retry`          | Retry up to `max_attempts` with backoff; if all retries fail, treat as `fail` | Add to completed |
| `skip_downstream`| Mark all transitively-dependent steps as `skipped`; continue siblings (steps that don't depend on this) | Add to completed |

The pipeline-level run status is:

- `succeeded` — all non-skipped steps succeeded
- `failed` — at least one step failed under `fail` mode OR exhausted `retry`
- `partial` — completed but with `warn` or `continue` failures, or with `skip_downstream` skips

### Jinja context

`src/carve/runtime/jinja_context.py` exposes a sandboxed Jinja environment with the following namespace:

```python
{
  "steps": {
    "<step_id>": {
      "outputs": {...},            # the outputs dict from the step's StepResult
      "status": "succeeded",       # or other
      "started_at": "2026-...",
      "finished_at": "2026-...",
    },
    ...
  },
  "run": {
    "id": "<uuid>",
    "pipeline": "<name>",
    "target": "<name>",
    "trigger": "scheduled",
    "started_at": "2026-...",
  },
  "env": {
    # An allow-list of selected NON-secret env vars; never secrets
    # (per spec 12.4-style scoping). EMPTY in the composition core — see callout.
  },
}
```

> **Updated during implementation (2026-06-24):** the `env` example originally showed `"DATABASE_URL": "..."`, which contradicts "never secrets" — `DATABASE_URL` is the password-bearing Postgres state-store DSN. An adversarial review flagged that a sandboxed template could read the DB password out of `env`, so the shipped allow-list (`jinja_context._EXPOSED_ENV_KEYS`) is **empty by design**: nothing in the composition core needs an env var in a template, and the obvious candidate is a secret. The allow-list *mechanism* is retained so a genuinely non-secret runtime var can be added later; secrets never belong here.

Jinja is sandboxed via the `SandboxedEnvironment` from `jinja2.sandbox` — no filesystem access, no arbitrary code execution. Only the namespace above is reachable. Rendering uses `StrictUndefined`, so a Jinja var pointing at an output the upstream step never emitted is a render error (surfacing the composition bug) rather than a silent empty string.

Templating happens at step launch time (after deps complete, before executor runs); the rendered values are passed to the executor as part of the step config.

### Step executor: `dlt`

> **Updated during implementation (2026-06-24):** the original sketch ran `dlt pipeline run --pipeline <name>` and parsed `state.json` for `rows_loaded per resource`. **Neither is real.** dlt has **no** `dlt pipeline run` CLI (the same defect class corrected in the dlt-engineer), so the executor runs the component's **Python entrypoint** via the shipped `Subprocess` primitive; and the persisted **load package** (`read_latest_load_package`) structurally cannot produce per-resource row counts (those live only in the in-process dlt trace, never the on-disk package). The shipped verdict + outputs come from the load package. Prose + pseudocode below reflect the real mechanism.

`src/carve/runtime/step_types/dlt.py`:

```python
class DltStepExecutor:
    step_type = "dlt"

    async def execute(self, *, step, run, paths) -> StepResult:
        # Name-based indirection: resolve the component name to a concrete dir.
        # Simple mode → el/<name>/; multi mode → the workspace clone @ pinned ref (spec 03 resolver).
        resolved = resolve_dlt_component(step.component, paths, components=self._components)
        code_dir = resolved.code_path
        if not code_dir.exists():
            return StepResult(status="failed", error_message=f"dlt component dir not found: {code_dir}")

        entrypoint = _find_entrypoint(code_dir)   # scripts/__init__.py → pipeline.py → __init__.py → main.py
        env = _build_dlt_env(paths)               # pins DLT_DATA_DIR so the load package is readable
        outcome = await asyncio.to_thread(        # run <python> <entrypoint> via the Subprocess primitive
            self._run_fn, entrypoint=entrypoint, code_dir=code_dir,
            cwd=paths.root, env=env, timeout_seconds=14400,   # 4hr default
        )
        # Verdict + outputs come from the on-disk LOAD PACKAGE, not the exit code alone,
        # and only from a package THIS run wrote (load_id >= run.started_at) — never a stale one.
        return _outcome_to_step_result(outcome, pipelines_dir=paths.dlt_config_dir / "pipelines",
                                       fallback_name=resolved.name, min_load_id=run.started_at.timestamp())
```

- `resolve_dlt_component(name, paths, components=…)` delegates to spec 03's dlt locator: in simple mode it returns `paths.el_dir / name`; in multi mode (`[components.<name>]` present) it returns the workspace clone at the pinned `ref`. The executor does no path math itself.
- The component is **its Python module**, run as `<python> <entrypoint>` via the shipped `Subprocess` primitive (own process group, Carve secrets stripped, wall-clock watchdog) — there is no `dlt pipeline run` CLI. The run mechanism is **injected** (`DltRunFn`) so the offline test layer never spawns a real venv. Destination-cred injection (`DESTINATION__SNOWFLAKE__CREDENTIALS__*` per ARCHITECTURE §10.2) and venv materialization from `requirements.txt` are **deferred to live wiring** — the default run uses `Subprocess` directly (not `LocalVenvRunner`), so only `DLT_DATA_DIR` reaches the child (fine for the creds-free DuckDB substrate). Per-step `write_disposition`/`resource_select` overrides **fail loud** today (the keys Carve invented were never read by dlt); honoring them is deferred to live wiring.
- The verdict comes from the on-disk **load package** (`read_latest_load_package`): a clean exit **plus** a `loaded` package with no `failed_jobs`. A clean exit that wrote no new package — or only a **stale** package from a prior run (`DLT_DATA_DIR` is persistent) — is `failed`, never false-green.
- `_outcome_to_step_result` returns the load-package fields as `outputs`: **`{tables, schema_changes, failed_jobs}`**. Per-resource `rows_loaded` is **not** available — it lives only in the in-process dlt trace, not the persisted package, so it is omitted here and is a live-wiring follow-up if needed.

### Step executor: `dbt`

`src/carve/runtime/step_types/dbt.py` — **backend-agnostic**. It resolves the dbt component and **dispatches to that component's configured execution backend** ([dbt-execution](./dbt-execution.md)); it does *not* assume dbt-core/subprocess.

- Resolves the dbt project/component **by name** via the [layout](./layout.md) locator: `component = "analytics"` → the workspace clone @ pinned ref in multi mode, or the local detected project in simple mode; an **omitted** `component` resolves to the single detected dbt project (`ProjectPaths.dbt_project_path`). A named component that doesn't resolve is a step failure with a clear message.
- Calls `DbtBackend.run(command, select, exclude, vars, target, full_refresh)` on the component's backend — `local` shells out to the bundled/external engine (Fusion or dbt-core, subprocess); `managed` triggers snowflake-native / dbt Cloud / remote. The backend normalizes results so this executor stays uniform.
- Surfaces per-model status, timings, and errors from the normalized `DbtRunResult` (sourced from local `target/run_results.json`, Snowflake `QUERY_HISTORY`, or the Cloud artifacts API per backend); `command="test"` surfaces failing tests as `error_message`.
- Default timeout: 3600s (1 hour); configurable per step.

### Step executor: `sql`

`src/carve/runtime/step_types/sql.py`:

- Opens a connection **by name** via the `resolve_connection` factory (`src/carve/runtime/step_types/connections.py`, a new seam) — it looks the `connection` name up across `[connections.duckdb.*]`/`[connections.snowflake.*]` and returns a `ResolvedConnection` (the live connector + its dialect). The factory is **injectable** (DuckDB-default) so the sql path runs creds-free in tests.
- Reads the SQL file (path relative to project root, **path-confined** under the root — an escaping/symlinked path is a clean `failed`, never a read outside the tree), then renders the **file body** through the sandboxed Jinja environment against the full `{steps, run, env}` namespace (the same one launch-time `jinja_vars` use) **plus** a `vars` key carrying the step's already-resolved `jinja_vars` (cross-step outputs threaded upstream at launch). So a body may reference `{{ steps.<id>.outputs.X }}` / `{{ env.X }}` directly as well as `{{ vars.<name> }}`; `StrictUndefined` makes a missing reference a render error.
- Executes the SQL as a single transaction (one connection, one execute call; for multi-statement files, the user can use the destination's batch mechanism — Snowflake's `EXECUTE IMMEDIATE`, Postgres's `\;` separator, etc.). A multi-statement file is classified by its *most-privileged* statement (the shipped `classify` takes the `max`), so a file mixing a write with a trailing `SELECT` runs down the write path and does not capture the trailing rows.
- Captures returned rows as outputs (`{rows, row_count, truncated}` — first 100 rows, with a truncation flag if more; every value coerced to a JSON primitive for JSONB persistence)
- Default timeout: 300s (5 min); configurable per step. Enforced via `asyncio.wait_for` → `failed`; a `to_thread` worker can't be force-cancelled, so a runaway query keeps running until it finishes (the worker closes the connection in its own `finally` — leak-free), and a driver-side query timeout is the deeper Increment-4 fix.

The `sql` step type is deliberately limited to single-file, single-target execution — no multi-file SQL "pipelines" within a step. That's what step composition is for.

### Run flags: `--resume` and `--refresh`

`carve run` (M1.1 + [runtime](./runtime.md)) takes two flags whose semantics this spec's step DAG defines:

- **`--resume <run_id>`** re-runs only the **failed steps and their dependents** from a prior run — the subgraph computed via `PipelineDAG.downstream_of` over the failed set, executed as a new *resuming* run (the `Run` row is [runtime](./runtime.md)'s; the failed-subgraph computation is this spec's). Steps that already succeeded are skipped.
- **`--refresh <mode>`** maps **1:1 to dlt's refresh modes** (`drop_data` | `drop_resources` | `drop_sources`) and is passed through to the `dlt` step executor (no effect on `dbt`/`sql` steps). Against a **prod** target it requires a typed-name confirmation (`--yes` for admins/CI) and is **audited** (actor + reason); that confirmation/audit pattern is [runtime](./runtime.md)'s. This is the supported "reload history" path — first-class backfills remain out of scope.

(Live log streaming for `carve run --watch` is the [rest-api](./rest-api.md)'s WS/SSE surface.)

### Pipeline engineer subagent

The pipeline-composition specialist is a **declarative subagent** on the harness (spec 15), defined in the spec-16 markdown-frontmatter format and shipped as a built-in at `src/carve/core/agents/builtin/pipeline-engineer.md`. The orchestrator (the harness main loop) `delegate`s pipeline-composition tasks to it; it runs as a fresh, context-isolated loop and returns a summary (the new/changed `pipelines/<name>.toml` + a validation result), not its full transcript. A user may override it by dropping a `carve/agents/pipeline-engineer.md` of the same name (spec 16).

`src/carve/core/agents/builtin/pipeline-engineer.md`:

```markdown
---
name: pipeline-engineer
description: Composes existing dlt/dbt/sql components into a pipelines/<name>.toml — by referencing components by name. Use for pipeline composition, step-DAG edits, and seeding a new pipeline's schedule. Does NOT author dlt code or dbt models.
model: claude-{LATEST_SONNET}        # per-agent model tier; falls back to the install default
tools: [edit, grep, pipeline_inspect, list_components, list_dbt_models, sql, web_fetch]
allowed_paths: ["pipelines/**"]      # write scope enforced by the permission gate (spec 15)
classifications:
  - compose_pipeline                 # new pipelines/<name>.toml
  - modify_pipeline_steps            # change step order, add/remove steps, update failure modes
  - seed_schedule                    # set the [seed_schedule] block on a NEW pipeline (a seed, not a live edit)
  - schedule_existing_component      # orchestration-only mode (PRD §6.2 mode 2): compose a TOML against an existing user-authored dlt/dbt component
---
<system prompt body — see "System prompt" below>
```

- **Tools (terminal-grade base + skills, spec 15/16).** `edit` is the precise string-replace tool (read-before-edit invariant), the only write tool the engineer holds, scoped by `allowed_paths` to `pipelines/**`; `grep` reads existing TOMLs/components for context; `pipeline_inspect` (spec-08 skill) reads existing `pipelines/<name>.toml`; `list_components` enumerates which component names exist (`el/<name>/` dirs + `[components.*]` blocks; names only, no contents); `list_dbt_models` reads the dbt manifest (HISTORICAL — M2-era manifest reader); `sql` is the dialect-aware tool (spec 18) used to confirm a target schema/relation exists before wiring a `sql` step; `web_fetch` reads dlt/dbt docs live. The engineer is **not** granted `bash` for arbitrary use — its only execution is the bounded verification path below (run through the harness verification primitive, not free-form shell). MCP-imported skills the user has allowed (`mcp:*`) are available per spec 16.
- **Permission mode.** The engineer runs in **`build`** mode (spec 15): `edit` is allowed within `allowed_paths` (`pipelines/**`); a write outside that scope **prompts** (or is denied headless), exactly the boundary the old `forbidden_write_paths = ["el/**", "carve/**", …]` guardrail expressed. In `read_only`/`plan` mode (e.g. an `ask`/`plan` verb that routes here for a dry composition) `edit` is gated off and the engineer produces a proposed TOML diff without writing. Per spec 16, the engineer's tool grants are validated against the active mode at load — an over-broad grant is rejected.
- **Classifications.** Unchanged from the control-plane revision — the orchestrator routes a goal to this subagent by matching its classification against this list (spec 16 routing, replacing the old hardcoded dispatch).

> **Schedule changes are not a TOML edit.** Changing a *live* schedule's cron or pausing/resuming is data, handled by `carve schedule …` (spec 07) — the pipeline engineer does **not** rewrite `[seed_schedule]` to change a running schedule (editing it is a no-op without reseed). The engineer only sets `[seed_schedule]` when first composing a pipeline; the `seed_schedule` classification covers that authoring case. A request like "pause the stripe pipeline" routes to the schedule CLI/API, not to this subagent.

#### Verification loop (verify by execution)

The pipeline engineer closes the loop on its own output rather than handing the orchestrator an unverified TOML. After writing (or editing) a `pipelines/<name>.toml`, it runs the harness verification primitive (spec 15, `run_check`):

1. **`carve pipelines validate <name>`** — schema + DAG check (unique step ids, valid `depends_on`, no cycles, valid cron, resolvable `component` names). This is the cheap gate; an unresolvable component name or a cycle comes back as a structured failure the engineer reads and fixes (e.g. it grep'd the wrong component name; it re-checks via `list_components` and edits).
2. **An optional dev run** — when the task warrants and the mode permits, a single execution against the dev target (the same generate→run→read→fix path the harness gives dlt/dbt agents). For composition, "run" is the DAG executor over the dev target; the engineer reads the real step results (`step_runs`, parsed `outputs`) and corrects the composition (a wrong `depends_on`, a missing `select`, a Jinja var referencing an output the upstream step doesn't emit) until green.

The engineer iterates until validation (and, where run, the dev execution) is green, bounded by the harness's attempt cap. It then returns the verified TOML + the validation/run result as its delegation summary. This is the accuracy story: the engineer never reports a composition the warehouse/validator hasn't confirmed.

#### System prompt highlights

1. **Role** — author/modify `pipelines/<name>.toml` files via `edit`. The dlt code and dbt models live in their components (authored by other subagents — the DLT engineer, spec 04; the DBT engineer); the pipeline engineer's job is to compose them by **referencing components by name**.
2. **Inputs** — the delegation `context` bundle (spec 15) carries: the goal, the **component names** to reference (with their resolved paths/outputs), conventions + standards from memory (spec 06), and pointers to existing pipeline TOMLs. The subagent gathers further detail itself within its own window (`grep`, `pipeline_inspect`, `list_components`) rather than relying on a fully pre-scoped context — context-isolation supersedes the old "pre-scoped context" pattern.
3. **Output** — a delegation summary that emits the new/modified TOML file (with `component = "<name>"` on dlt/dbt steps) plus the verification result.
4. **Verify before returning** — always run `carve pipelines validate`; do a dev run when the task warrants; fix and re-run until green (the loop above).
5. **Schedule semantics** — when to seed a cron via `[seed_schedule]` on a new pipeline vs leave unscheduled; that `[seed_schedule]` is a *seed* only (live schedule changes go through the schedule CLI/API, not the TOML).
6. **Step ordering** — dlt before dbt; transforms before notifications/exports; SQL post-steps last.
7. **Failure mode picking** — `retry` for transient-prone (ingest); `fail` for hard transforms; `warn` for nice-to-have post-steps.
8. **Cross-step outputs** — when to use Jinja templating to pass values.
9. **Component naming** — reference dlt/dbt components by name; in simple mode the dbt step's `component` may be omitted (single detected dbt project). The engineer does not write `[components.*]` blocks or set pins (that's `carve component` / spec 03).

### Pipeline engineer's role in orchestration-only mode

Per [PRD §6.2](../PRD.md) mode 2, users with existing dlt/dbt code want Carve to orchestrate without authoring. The pipeline engineer handles this:

- The orchestrator detects (via the dispatch logic from spec 04) that the user's goal touches a user-authored component, and `delegate`s to the pipeline engineer with the goal classified as `schedule_existing_component`.
- The delegation `context` carries the existing component's name + a structured summary of what it does (extracted by the `existing_dlt_inspect` / `existing_dbt_inspect` skills, spec 04); the engineer can also `grep` / `list_components` to confirm.
- The engineer writes a `pipelines/<name>.toml` that references the existing component **by name** (`component = "<name>"`) and verifies it (validate, optional dev run). In simple mode that name resolves to the existing `el/<name>/` dir or detected dbt project; if the user already split the component out, its `[components.<name>]` block (spec 03) resolves the same name to the remote repo @ ref — the pipeline TOML is the same either way.
- No DLT engineer is delegated to; no dlt code generation.

This is what makes mode 2 work end-to-end: the user keeps their dlt code, gets Carve's runtime scheduling, observability, and composition.

### CLI: `carve pipelines`

```
carve pipelines list                      # all pipelines with last-run summary
carve pipelines list --status running     # filter
carve pipelines show <name>               # full config + recent run history
carve pipelines validate <name>           # schema-check + DAG check (cycles, missing depends_on refs)
carve pipelines validate                  # validate all pipelines
carve pipelines diff <name> --against <build_id>
                                          # diff current pipelines/<name>.toml against an older build's manifest_json
```

Authoring of pipeline TOMLs is via `carve plan` / `carve build` (per PRD §6.10 and design decision 5.3). The CLI doesn't expose direct edit commands for pipelines beyond the standard `$EDITOR` flow.

REST/MCP coverage of this CLI surface lands in spec 09; this spec ships only the CLI implementation.

### CLI: `carve component` / `carve components show`

```
carve component <name> --separate-remote <url> [--ref <pin>] [--branch <name>]   # graduate to a remote repo
carve component <name> --separate-local <path>                                   # graduate to a local path
carve component <name> --same-repo                                               # reverse graduation
carve components show                       # list every component: name, type (dlt|dbt), mode, url, resolved ref/branch, resolved path/workspace
carve components show <name>                # one component's full resolution detail + which pipelines/steps reference it
```

`carve component <name> --separate-remote …` performs the graduation flow described under *Component graduation* above (write the `[components.<name>]` block, clone+validate the workspace, backfill omitted dbt-step names). `carve components show` is the always-on inspection surface that makes the otherwise-hidden simple-mode convention legible (it lists convention-discovered components too, even when `carve.toml` has no `[components.*]` blocks).

> The `[components.<name>]` block schema and the locator/sync that back these commands are defined in [layout](./layout.md); this spec ships the `carve component` / `carve components show` CLI surface on top of that resolver. Deploying a graduated component's repo through an environment is [deploy](./deploy.md) (Wave 2), out of scope here.

### CLI: `carve schedule reseed`

```
carve schedule reseed <pipeline>            # re-apply pipelines/<pipeline>.toml's [seed_schedule] to the schedules table
```

Re-applies the current `[seed_schedule]` block to the live `schedules` row for that pipeline, overwriting the live `cron`/`timezone`/`target`. This is the **only** path by which an edited `[seed_schedule]` block takes effect — absent this command, `[seed_schedule]` edits are inert (the reconciler never touches the schedule; see *Schedule seed semantics*). It errors if the pipeline has no `[seed_schedule]` block. The everyday schedule controls (`carve schedule list/show/pause/resume`) are spec 07's, operating on the `schedules` table as data; this command is the narrow code→data re-seed bridge.

REST/MCP coverage of these surfaces lands in spec 09; this spec ships only the CLI implementation.

## Tests

- **Unit (schema):** valid TOML loads cleanly (`component = "<name>"` on dlt/dbt steps; `[seed_schedule]`); invalid TOMLs (missing required fields, a dlt step missing `component`, unknown step types, duplicate step ids, missing depends_on refs, cycles, bad cron) raise structured errors; an `artifact = ...` key on a dlt step (the old name) is rejected with a migration-pointing message
- **Unit (component resolution):** a `component` name resolves to `el/<name>/` in simple mode and to the workspace clone @ pinned ref in multi mode for the **same** pipeline TOML; an omitted dbt-step `component` resolves to the single detected dbt project; an unresolvable name fails validation
- **Unit (seed schedule):** `[seed_schedule]` parses (`cron`/`timezone`/`target`); a missing block yields an unscheduled pipeline; `paused`/`enabled` keys in the block are rejected (live data, not seedable — this is why the `schedules.paused_by` origin is only `user`/`recovery`, never `code`; see spec 07 / ARCHITECTURE §9.1)
- **Unit (DAG):** topological order is correct for representative DAGs (linear, fan-out, fan-in, diamond); ready_steps correctly accounts for completed/failed/skipped sets
- **Unit (failure modes each):** one test per mode, exercising the transition rules from the table above
- **Unit (Jinja sandbox):** template renders against the standard namespace; attempts to access filesystem or import os raise sandbox errors
- **Unit (dlt executor):** mock subprocess; verifies command construction, env-var injection, state.json output parsing
- **Unit (dbt executor):** the executor calls `DbtBackend.run` and normalizes `DbtRunResult` identically across a stubbed `local` (subprocess) and a stubbed `managed` (snowflake-native / dbt-cloud) backend — it never assumes dbt-core/subprocess ([dbt-execution](./dbt-execution.md))
- **Unit (sql executor):** real connection to a fixture Postgres; verifies single-transaction semantics + output capture
- **Integration (3-step pipeline):** a synthetic `pipelines/stripe.toml` with dlt → dbt → sql; fixture Stripe-like mock API; runs end-to-end; rows land in fixture warehouse; step_runs table has the right shape
- **Integration (parallel steps):** a pipeline with two independent dlt steps and one dbt that depends on both; both dlt steps run concurrently; dbt waits for both
- **Integration (failure modes in practice):** a pipeline where step 2 fails under `warn`; pipeline run completes as `partial`; step 3 runs; the warning surfaces in logs
- **Integration (skip_downstream):** step 2 fails under `skip_downstream`; step 3 (depends on 2) is marked skipped; step 4 (sibling) runs
- **Integration (retry):** step that fails twice then succeeds under `mode=retry max_attempts=3`; pipeline succeeds; step_runs table shows three attempts with the third succeeding
- **Integration (pipeline engineer):** `carve plan "schedule the stripe ingest to run nightly at 2am and then build the staging models"` `delegate`s to the pipeline engineer, which produces a coherent `pipelines/stripe.toml` referencing the existing dlt component by name (`component = "stripe_charges"`) + a dbt step, with a `[seed_schedule]` cron, and **self-verifies** via `carve pipelines validate` (a deliberately-broken composition — e.g. a bad `depends_on` — triggers a fix iteration before the summary returns)
- **Integration (orchestration-only mode):** existing user-authored dlt at `el/legacy_salesforce/` (no provenance header); `carve plan "schedule legacy_salesforce daily"` produces a TOML with `component = "legacy_salesforce"` without invoking the EL agent
- **Integration (component graduation):** a simple-mode project with a dbt step whose `component` is omitted; `carve component analytics --separate-remote <test-git-url> --ref <sha>` writes the `[components.analytics]` block, clones the workspace, validates, and backfills `component = "analytics"` into the dbt step; the same pipeline runs unchanged afterward (no re-run, schedules intact); `--same-repo` reverses it
- **Integration (multi-mode resolution parity):** the identical `pipelines/stripe.toml` runs end-to-end in simple mode and in multi mode (with `[components.*]` pointing at separate-remote repos @ ref); both produce equivalent results — proving name indirection
- **Integration (seed schedule first-registration):** registering a pipeline with `[seed_schedule]` writes one `schedules` row; editing the block and re-reconciling does **not** change the live schedule; `carve schedule reseed <pipeline>` re-applies it and the `schedules` row updates
- **Integration (carve components show):** in a simple-mode project with no `[components.*]` blocks, `carve components show` lists the convention-discovered dlt + dbt components with their resolved paths; after graduation it shows the graduated component's `mode`/`url`/`ref`

## Acceptance

- A 3-step pipeline (`dlt` → `dbt` → `sql`) executes end-to-end in correct topological order against fixture infrastructure
- Parallel steps execute concurrently when the DAG permits
- Each of the five failure modes behaves per the table above
- Cross-step output references resolve via the sandboxed Jinja context
- Cycle detection rejects invalid DAGs at `carve pipelines validate` time, before the runtime ever sees them
- The pipeline engineer subagent (delegated to by the orchestrator) authors a working `pipelines/<name>.toml` from a natural-language goal, referencing components by name, and verifies it by execution (`carve pipelines validate` + an optional dev run) before returning its summary
- **The same `pipelines/<name>.toml` runs in simple mode and multi mode** — `component = "<name>"` resolves to a local path or a remote repo @ pinned ref with no edit to the pipeline file
- **Graduation works without rewrites:** `carve component <name> --separate-remote …` writes the block, clones+validates, backfills omitted dbt-step names; schedules keep firing and run history is intact; `--same-repo` reverses it
- **`[seed_schedule]` is a seed, not the source of truth:** it seeds the `schedules` row at first registration only; editing it is inert until `carve schedule reseed`; `carve components show` makes the simple-mode convention legible
- Orchestration-only mode (mode 2) end-to-end: a user with existing user-authored dlt code can compose a scheduled pipeline via `carve plan` (referencing the existing component by name) without the EL agent running
- All three step executors invoke the correct subprocess command with the right env vars and parse their outputs into structured step `outputs`
- `carve pipelines validate` catches schema errors, cycles, missing references, and **unresolvable component names** with clear messages
- The full plan→build→run→deploy→schedule loop works end-to-end against real Snowflake (this is the acceptance bar). *(The deploy leg is unchanged by this revision and pending the Wave 2 deploy revision of spec 14.)*

## Design notes

- **Why a fixed three step types (`dlt`, `dbt`, `sql`) instead of pluggable types from day one?** Per [PRD §4.2 out of scope](../PRD.md) and design decision [5.9 steps as unit of execution](../ARCHITECTURE.md). A custom-step-type SDK requires hardening the abstraction against arbitrary executors, and the abstraction matures fastest when stressed by concrete implementations. Three real consumers from day one keep the abstraction honest; the custom-step SDK lands in a later increment.
- **Why Jinja for cross-step values instead of native Python expressions?** Because step authors (users via standards.md, and the pipeline engineer) work in TOML, not Python. Jinja is the universal templating language for TOML/YAML config files (Ansible, dbt itself). Sandboxed Jinja keeps the surface limited to the namespace we expose.
- **Why does the `sql` step type only support single-file single-target execution?** Because anything richer pushes back toward "carve has its own SQL engine," which we explicitly aren't building. Users who need multi-statement SQL with conditional logic should put that in a dbt model. The `sql` step is for thin operational glue (refresh a materialized view, post a row count to an analytics table).
- **Why `skip_downstream` instead of more elaborate conditional logic?** Per [ARCHITECTURE §15](../ARCHITECTURE.md), we don't ship general conditional branching. `skip_downstream` is the one form of conditionality we allow because it falls out naturally from the failure-mode framework — "if this step failed, the next steps don't apply" is a common, easily-explained pattern.
- **Why a separate pipeline engineer subagent rather than letting the orchestrator write pipeline TOMLs directly?** Because the orchestrator (the harness main loop) does classification + decomposition + `delegate` + synthesis — it doesn't do deep work itself. Folding "also write pipeline TOMLs" into it would bloat its prompt and reduce its specialism, and it would lose the context-isolation win (a composition that takes a dozen `grep`/`pipeline_inspect`/validate iterations stays in the subagent's window, not the orchestrator's). A focused pipeline engineer with a small tool set (`edit` scoped to `pipelines/**`, `grep`, `pipeline_inspect`, `list_components`, `sql`) is easier to reason about, test, and override (it's just a markdown file, spec 16).
- **Why scope the pipeline engineer's `edit` to `pipelines/**` (read but never write dlt/dbt components)?** Same separation of concerns, now enforced by the permission gate (spec 15) rather than a bespoke guardrail block: the engineer's `allowed_paths` is `pipelines/**`, so any `edit` outside it prompts/denies. The DLT engineer (spec 04) owns dlt code; the DBT engineer owns dbt models. The pipeline engineer composes them by reference. If a goal requires both new dlt code AND new pipeline composition, the orchestrator `delegate`s to both subagents and merges their summaries into one Plan.
- **Why a declarative markdown agent (`builtin/pipeline-engineer.md`) instead of a `runtime_specialist.py` class + a `runtime.toml`?** Per the AI-harness model ([../_strategy/2026-06-ai-harness.md](../_strategy/2026-06-ai-harness.md)) and spec 16: every agent is a markdown file with frontmatter, loaded by the `AgentRegistry`, hot-reloadable, and overridable by a same-named user file. Folding the prompt + the tool/path/classification config into one file (and dropping the bespoke Python class) resolves the built-vs-spec agent drift and makes the pipeline engineer the same shape as every other subagent. The old `[guardrails]` block's intent (writable `pipelines/**`, forbidden `el/**`/`carve/**`) is now just `allowed_paths` enforced by the permission gate (spec 15).
- **Why does the pipeline engineer verify by execution?** Per the harness model, generation-without-verification is a demo. A composition can be syntactically fine but wrong (a cycle, a dangling `depends_on`, a Jinja var referencing an output the upstream step never emits, a `component` name that doesn't resolve). Running `carve pipelines validate` (and, where warranted, a dev run that reads real `step_runs`/`outputs`) and fixing until green is what makes the engineer a colleague rather than a code generator — and it grounds the engineer in the real component graph rather than a guessed one.
- **Why is `[seed_schedule]` a seed instead of the schedule source of truth?** Because pausing a pipeline or nudging a cron is an operational act that should be instant and audited (CLI/API/UI + the `schedule_changes` log), not a code change requiring plan/build/deploy/PR. Per the three-tier ownership in [../_strategy/2026-06-control-plane.md](../_strategy/2026-06-control-plane.md), the pipeline *definition* is code (reconciled) but the *schedule* is data. This **reverses UC2's earlier decision** and **deletes its code-vs-override TTL-precedence machinery** — the reconciler reconciles the definition only and never touches the schedule. The code still carries a seed so a fresh registration has a sensible default and the schedule can reconstitute from (backed-up) state + code; `carve schedule reseed` is the deliberate bridge back from code to data.
- **Why do `sql` steps stay inline (file + connection) instead of becoming named components?** A `sql` step is thin operational glue authored in the control-plane repo; there's no independent lifecycle to version or deploy separately, so the name-indirection apparatus would be overhead with no payoff now. (Whether ad-hoc sql ever graduates to a named/separable component is an open point in the reference model; left inline for now.)

## Open questions

- **Per-step timeout defaults (4h dlt, 1h dbt, 5min sql).** *Implementation default.* Inherited from ARCHITECTURE §14.6. Configurable per step via the TOML (`timeout_s = ...`). These defaults are conservative for real-world cases; can lower in `runtime.toml` if a user wants tighter SLAs.
- **Intra-pipeline parallelism slot count.** *Implementation default.* Default 4 slots per worker. Tunable in `runtime.toml`. The cap matters when a pipeline has many independent dlt resources fanned out at one level.
- **How `partial` pipeline-run status surfaces in retries/scheduling.** *Implementation default.* A `partial` run is *not* automatically retried by the scheduler — it's treated as completed. Users who want auto-retry on partial use `mode=fail` on the warning-emitting step instead. Documented in `docs/failure-modes.md`.
- **Whether `step.outputs` is size-capped.** *Implementation default.* 64KB per step's outputs JSONB column. If a step produces more (huge row counts as outputs, etc.), it's truncated with a flag — agents reading the outputs see partial data, which is acceptable for downstream Jinja but visible. Users authoring sql steps with large output dicts should structure them down.
- **Behavior when the pipeline engineer is asked to compose a pipeline involving a component that doesn't exist.** *Implementation default.* The engineer confirms via `list_components`, then returns a `needs_user_input` status in its delegation summary with a message: "The component `<name>` doesn't exist. Either author it first (e.g., `carve plan 'ingest X'`) or reference an existing component." The orchestrator surfaces this in the plan summary; the user decides. (`carve pipelines validate` would also fail an unresolvable name — but the engineer catches it before writing.)
- **Whether `target` belongs in `[seed_schedule]`.** *Needs human confirmation.* The reference model's `[seed_schedule]` example shows only `cron` + `timezone`, but a scheduled job needs a target (spec 07's `jobs.target` is NOT NULL). The smallest reasonable choice taken here: keep `target` as a seedable field (`default = "prod"`) alongside `cron`/`timezone`, since it is part of *what gets seeded into the schedules row*, not a live-mutated control like pause. If the runtime instead derives target from `carve.toml`'s `default_target` at registration, drop `target` from the block. Flagged for the spec-07 owner to confirm the `schedules` row's target source.
- **Simple-mode dbt-step `component`: omit-and-backfill vs always-name.** *Following the reference model's lean.* The reference model leans "omit in simple mode, backfill on graduation" (cleanest simple mode) but lists it as an open point. This spec implements that lean: a `dbt` step's `component` is optional and graduation backfills it. If the project later prefers zero graduation-churn, switch to always-naming the dbt component in simple mode — the schema already permits a name.
- **`ref` vs `branch` on a graduated component.** *Deferred to spec 03.* `carve component … --separate-remote` accepts `--ref` (pin) or `--branch` (track HEAD), mirroring the reference model's per-component fields; the exact precedence/default (track default branch when neither is set) is defined by spec 03's `[components.<name>]` schema, not here.
