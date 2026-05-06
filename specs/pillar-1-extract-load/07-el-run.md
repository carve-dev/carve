# P1-07 — `carve el run`

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 0.5 day
**Dependencies:** M1-05 (step + runner protocols), M1.1-03 (root .env autoload), P1-01 (target system), P1-02 (plan/build lifecycle)
**Lineage:** Continues **M1.1-06**'s `carve run <pipeline>` command. `LocalVenvRunner` (M1-05) is unchanged. The replay-guard removal from M1.1-06 carries forward. **M1.1-03**'s root `.env` autoload is also unchanged — under the centralized config model (P1-01), there's no per-target `.env` switching; the active target only selects which `[snowflake.<target>]` section of `connections.toml` is read. Net-new in this spec: the path-resolution lookup (`targets/<active>/el/<name>/main.py`), the CLI restructure (lives under the `carve el` subcommand), and `carve el list` as a sibling listing command. Existing `carve run` becomes a deprecated alias that warns and forwards to `carve el run` for one minor version, then is removed.

## Purpose

Execute an EL artifact against the active target. Reads `targets/<active>/el/<name>/main.py` and `requirements.txt`, materializes a venv via `LocalVenvRunner`, runs the script with target-scoped env vars, streams logs back to the user. Re-runnable; no replay guard.

The CLI restructure — moving from top-level `carve run` to `carve el run` under the `el` subcommand group — is the only externally-visible change for users adopting v0.1 from M1.1.

## CLI surface

```
carve el run <artifact_name> [--target X] [--watch]
```

- `<artifact_name>` — required positional. Matches the directory under `targets/<active>/el/<name>/`.
- `--target X` — overrides the active target. Defaults to `default_target` from `carve.toml`. P1-01's resolution order applies (`--target` → `CARVE_TARGET` env → `default_target` → `"dev"`).
- `--watch` — re-runs whenever any file under `targets/<active>/el/<name>/` changes. Dev-iteration nicety; mirrors `dbt run --watch` and similar tools. Each change triggers a fresh `Run` row. Ctrl-C exits the loop. Each iteration's logs stream live; the loop sits in a "Watching for changes..." prompt between runs.

The M1.1-06 `--plan <plan_id>` flag is **dropped** — Carve doesn't run plans, it runs builds, and the files on disk are the authoritative source. A user wanting to run a historical version of an artifact uses `git checkout <sha>` first; Carve doesn't reconstruct historical content for them.

```
carve el list [--target X]
```

Sibling command, listing the EL artifacts in the active target's `targets/<active>/el/` directory. One row per artifact with last-run-status and last-run-at timestamps.

## Path resolution

The runner looks up the active artifact files in this order:

1. `targets/<active_target>/el/<artifact_name>/main.py`
2. `targets/<active_target>/el/<artifact_name>/requirements.txt`

If `main.py` is missing, exit 2 with: `"No EL artifact named '<name>' in target '<active_target>'. Run `carve el list --target <active_target>` to see what's available, or `carve build <plan_id>` to create it."`

If `requirements.txt` is missing, exit 2 with a hint about re-building (it should always be there if `main.py` is — its absence indicates a hand-edit gone wrong).

The `pipelines/<name>/` legacy path from M1.1-06 is **also checked** as a transitional fallback, with a one-line deprecation warning printed: `"Found legacy 'pipelines/<name>/main.py' at the project root. Migrate to 'targets/<active>/el/<name>/' (see CHANGELOG v0.1.0). Falling back for now."`. The fallback runs the legacy path; it does not auto-migrate. Removed in v0.2.

## Environment variable assembly

Root `.env` is already loaded at CLI startup via M1.1-03. The runner inherits the loaded environment for the venv subprocess; no per-target switching happens at run time because the centralized `.env` (P1-01) has all targets' secrets prefixed. The script reads `os.environ['<TARGET>_SNOWFLAKE_USER']` (etc.) directly.

Plus a small set of `CARVE_*` env vars the runner injects so the script can self-introspect:

- `CARVE_ACTIVE_TARGET` — the resolved active target name, **uppercased** (e.g. `DEV`, `PROD`, `EU_PROD`). The uppercase form matches the env-var-prefix convention so the script's lookup is direct.
- `CARVE_PIPELINE_NAME` — the artifact name (`iowa_liquor_sales`, etc.).
- `CARVE_RUN_ID` — the `Run.id` for this execution; the script can include it in structured log lines.

The script uses `CARVE_ACTIVE_TARGET` to pick its target-prefixed credentials. Concretely: the agent-emitted script does:

```python
target = os.environ['CARVE_ACTIVE_TARGET']  # already uppercased
account = os.environ[f"{target}_SNOWFLAKE_ACCOUNT"]
user    = os.environ[f"{target}_SNOWFLAKE_USER"]
# ...
```

This is the central reason the centralized `.env` model works: the same `main.py` runs against any target by switching the prefix-resolution at run time. No per-target file copies of the script are needed for env-var-handling reasons.

## CLI command structure

The `carve el` typer subgroup houses Pillar 1's operational verbs:

- `carve el run <name> [--target X] [--plan <plan_id>]` — this spec
- `carve el list [--target X]` — this spec
- `carve el deploy <name> --from X --to Y [...]` — P1-08
- `carve el provision <name> --target X` — P1-08
- `carve el verify <name> --target X` — P1-08

`carve run <name>` (M1.1-06's top-level command) becomes a deprecated alias defined in `src/carve/cli/main.py`:

```python
@app.command(name="run", hidden=True, deprecated=True)
def deprecated_run_alias(
    name: str = typer.Argument(...),
    target: str | None = typer.Option(None, "--target"),
    plan: str | None = typer.Option(None, "--plan"),
) -> None:
    rprint("[yellow]`carve run` is deprecated; use `carve el run` instead.[/yellow]")
    rprint("[yellow]This alias will be removed in v0.2.[/yellow]")
    el_run.command(name, target, plan)  # forward to the subcommand
```

The alias prints a deprecation banner, forwards to `carve el run`, exits with that command's exit code. `--help` lists `el` prominently and shows `run` only when the user explicitly runs `carve run --help` (typer's `hidden=True`).

## `carve el list` rendering

`rich`-formatted table:

```
EL artifacts in target "dev"

  Name                      Built        Last run         Status
  ──────────────────────────────────────────────────────────────
  iowa_liquor_sales         2 days ago   2 minutes ago    ✓ success
  salesforce_opps           1 hour ago   never            —
  marketing_attribution     5 days ago   yesterday        ✗ failed
```

Columns:
- **Name** — directory name under `targets/<active>/el/`.
- **Built** — most recent successful build's `created_at` (from the `builds` table; relative time).
- **Last run** — most recent `Run` row with `kind="run"` and `pipeline_name=<name>`; relative time.
- **Status** — that run's terminal status with a glyph (`✓ success`, `✗ failed`, `⊘ cancelled`, `⟳ running`).

Empty state: `"No EL artifacts in target 'dev'. Run carve plan ... to create one."`

Filter: `--target X` shows another target's artifacts.

## Run flow (essentially M1.1-06's runner)

1. Resolve active target (P1-01).
2. Use the positional `<artifact_name>` directly. Carve runs builds, not plans — the files on disk are the authoritative source.
3. Validate `targets/<active>/el/<name>/main.py` exists; legacy fallback as noted above.
4. Read `requirements.txt`.
5. Build a `PythonStepConfig` (script path, requirements, timeout from `carve/runner.toml`).
6. Create a `Run` row: `kind="run"`, `pipeline_name=<name>`, `target=<active>` (this column lands here per P1-02's lifecycle work — `runs.target` is added in migration `0004_build_entity` because Build needed it; runs use it for filtering too). `target_id` carries the most recent successful Build's id when one exists, else NULL.
7. Build a `LocalVenvRunner`; dispatch with the inherited environment + the `CARVE_*` vars above.
8. Live-tail logs via the M1.1-04 progress observer.
9. On terminal status, update `Pipeline.last_run_*` denorms; print final status; map exit code.

### `--watch` flow

When `--watch` is passed, the run flow loops:

1. Run the artifact once (steps 1-9 above).
2. Print `[watching targets/<active>/el/<name>/ — Ctrl-C to exit]`.
3. Set up a `watchdog`-based filesystem observer on the artifact's directory.
4. On any file-change event (debounced ~300ms), drop back to step 1 (fresh `Run` row, full venv re-resolution if `requirements.txt` changed).
5. On Ctrl-C, exit cleanly with the most recent run's exit code.

The watcher is shallow (single artifact directory only) — it doesn't watch other targets or other artifacts. Cross-target dev work runs `--watch` separately per artifact.

`--watch` is incompatible with explicit `--target` switches mid-loop; the active target is captured at command start.

## Safety rails

- **Project-root containment** check from M1.1-06 carries forward: the resolved `pipeline_dir` must be under the project root. Defense-in-depth against pathological target names like `../../../etc`.
- **Active target must be defined** in `carve/connections.toml` (per P1-01's validation). If missing, exit 2 before any subprocess starts.
- **Re-running succeeds** — no replay guard. M1.1-07 already removed this; the rule is preserved here.

## Implementation

### File-level changes

New files:

- `src/carve/cli/commands/el/__init__.py` — typer subgroup wiring `run`, `list` (and later `deploy`, `provision`, `verify` from P1-08).
- `src/carve/cli/commands/el/run.py` — refactor of M1.1-06's `cli/commands/run.py`, with path resolution + target awareness + legacy fallback.
- `src/carve/cli/commands/el/list.py` — the listing command.
- `tests/cli/commands/el/test_run.py`
- `tests/cli/commands/el/test_list.py`

Modified files:

- `src/carve/cli/main.py` — register `el` subgroup; register hidden `carve run` deprecated alias.
- `src/carve/cli/commands/run.py` — kept temporarily; calls into the new `el.run` after printing the deprecation banner. Removed in v0.2.
- `src/carve/cli/orchestrator/runner.py` — path resolution updated (`pipelines/<name>/` → `targets/<active>/el/<name>/`); legacy-fallback shim added; `CARVE_ACTIVE_TARGET` (uppercased) injection added.
- `tests/cli/orchestrator/test_runner.py` — assertions moved to `targets/<active>/el/<name>/`; legacy-path fallback test added.
- `tests/test_cli.py` — `EXPECTED_COMMANDS` gains `el` group; `run` flagged hidden.
- `pyproject.toml` — add `watchdog>=4.0` runtime dep for `--watch` mode.

No DB migration. The `runs.target` column was added in P1-02's migration `0004_build_entity.py`.

## Tests

- `test_el_run_resolves_artifact_in_active_target` — `carve el run iowa_liquor` reads from `targets/dev/el/iowa_liquor/main.py`.
- `test_el_run_target_flag_overrides_default` — `carve el run iowa_liquor --target prod` reads from `targets/prod/el/iowa_liquor/main.py`.
- `test_el_run_carve_active_target_env_var_uppercase` — subprocess sees `CARVE_ACTIVE_TARGET=DEV` (uppercased) in `os.environ`.
- `test_el_run_legacy_pipelines_fallback_warns_and_runs` — `pipelines/<name>/main.py` exists, `targets/<active>/el/<name>/main.py` does not → fallback fires, deprecation warning printed, script runs.
- `test_el_run_missing_artifact_exits_2` — neither path exists → exit 2 with the listing-of-available-artifacts message.
- `test_el_run_creates_run_row_with_target` — `runs.target` is set to the resolved active target.
- `test_el_run_re_runnable` — running the same artifact twice in succession both succeed (no replay guard).
- `test_el_run_target_id_references_most_recent_build` — when a successful Build exists, `runs.target_id` points at it; when no Build, `runs.target_id` is NULL.
- `test_el_run_watch_reruns_on_file_change` — `--watch` mode triggers a fresh Run when `main.py` changes; debounced.
- `test_el_run_watch_exits_on_ctrl_c` — Ctrl-C in `--watch` exits cleanly with the most recent run's exit code.
- `test_el_run_watch_picks_up_requirements_change` — touching `requirements.txt` triggers a re-run with venv re-resolution.
- `test_el_list_table_format` — `carve el list` renders the documented table with name/built/last-run/status columns.
- `test_el_list_empty_state` — no artifacts in `targets/<active>/el/` shows the empty-state message.
- `test_carve_run_deprecated_alias_warns_and_forwards` — `carve run iowa_liquor` prints the deprecation banner and runs successfully.
- `test_active_target_not_defined_exits_2` — `--target foo` where `[snowflake.foo]` doesn't exist → exit 2 before subprocess starts.
- `test_project_root_containment_enforced` — pathological target name resolving outside the project root → refused.

## Acceptance criteria

- `carve el run <name>` runs the EL artifact at `targets/<default_target>/el/<name>/main.py`.
- `--target X` resolves the artifact under `targets/X/`; project-root containment refuses pathological names.
- The script's environment includes `CARVE_ACTIVE_TARGET` (uppercased), `CARVE_PIPELINE_NAME`, `CARVE_RUN_ID` plus the target-prefixed `<TARGET>_SNOWFLAKE_*` vars from root `.env`.
- The legacy `pipelines/<name>/main.py` path falls through with a deprecation warning when no `targets/<active>/el/<name>/` exists.
- `Runs.target` records the active target; `carve runs --pipeline <name>` (already in M1.1-06) shows runs filterable by target.
- `carve el list` lists artifacts in the active target with built/last-run/status columns.
- `carve el run --watch` re-runs the artifact on filesystem changes under `targets/<active>/el/<name>/`; Ctrl-C exits cleanly.
- `carve run <name>` (the legacy top-level alias) works for one minor version with a deprecation banner; removed in v0.2.
- Re-running an EL artifact is always safe — no replay guard.
- `ruff` + `mypy --strict` + full `pytest` stay green; new tests cover happy path + legacy fallback + target switching + empty state + watch mode.

## Files this spec produces

(Summary of File-level changes section.)

New: typer `el` subgroup, `run` and `list` subcommands, 2 test files.
Modified: `cli/main.py`, `cli/commands/run.py` (deprecated alias), `cli/orchestrator/runner.py` (path resolution + env injection), existing runner tests.
No DB migrations.

## Out of scope

- Recovery agent integration (P1-09 wraps this; this spec just runs and reports failure).
- Concurrency limits (the existing `runner.toml` setting still applies; no spec change).
- Run cancellation from the CLI mid-run (defer; `Ctrl-C` via process signals is the v0.1 mechanism). `--watch` Ctrl-C between runs is in-scope.
- `carve el show <name>` — could come later; `carve target show` covers most adjacent needs.
- Streaming live logs to a remote viewer (defer to a UI milestone if/when one ships).
- Running multiple targets in parallel (`--targets dev,prod`). Defer.
- Per-run cost cap or attempt budget (not relevant for `run`; that's `recovery`'s job).
- `--watch` across multiple artifacts simultaneously (defer; one artifact per `--watch` invocation).
- Historical-version run via `--build <build_id>` or `--plan <plan_id>` — explicitly **dropped from M1.1-06**. Carve runs the current files on disk; users wanting historical versions `git checkout` first.

## What this enables

- The Pillar 1 happy path's `run` step: dev iteration via `carve el run` (default target = dev) until the rows in dev look right.
- Manual prod execution via `carve el run --target prod` from a deployment box, an Airflow DAG, a custom CI job. The user owns the recurring scheduler in v0.1.
- `carve el list` as the daily-driver "what artifacts do I have, and how are they doing" view, suitable for piping into other tools.
- The deprecated alias smooths the migration from M1.1 → v0.1 for any existing users.
