# M2-06 — dbt integration

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M1-02 (config), M1-05 (step + runner protocols)

## Purpose

Integrate dbt-core as a first-class step type and provide the manifest-reading layer that the dbt agent (M2-04) and orchestration agent (M2-02) depend on for context. This is what makes Carve a useful tool for analytics engineers.

## Two distinct concerns

This spec covers two related but separate things:

1. **`dbt` step type** — execute dbt commands as part of a pipeline
2. **Manifest reader** — load `target/manifest.json` and expose structured queries

Both depend on having dbt-core installed and a working dbt project.

## The dbt step type

`src/carve/core/steps/dbt.py`:

```python
class DbtStepConfig(StepConfig):
    project_dir: str = "dbt"
    profiles_dir: str | None = None  # defaults to ~/.dbt/
    target: str = "dev"
    select: str | None = None  # e.g., "+stg_orders+"
    exclude: str | None = None
    command: str = "build"  # "run" | "test" | "build" | "compile" | "parse" | "seed"
    full_refresh: bool = False
    threads: int | None = None  # overrides profile

class DbtStep:
    step_type = "dbt"
    config: DbtStepConfig
```

## The DbtRunner

A specialized runner for dbt steps. `src/carve/core/runners/dbt.py`:

```python
class DbtRunner:
    def __init__(self, config: Config, repo: Repository):
        self.config = config
        self.repo = repo

    def execute(self, step: DbtStep, context: RunContext) -> RunHandle:
        run_id = context.run_id

        cmd = self._build_command(step.config, context)
        env = self._build_env(context)

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=context.project_dir,
        )

        # Stream stdout to logs (parse dbt's structured output where possible)
        threading.Thread(
            target=self._stream_dbt_logs,
            args=(run_id, proc),
            daemon=True,
        ).start()

        return RunHandle(run_id=run_id, process_id=proc.pid)

    def _build_command(self, cfg: DbtStepConfig, ctx: RunContext) -> list[str]:
        cmd = ["dbt", cfg.command]
        if cfg.select:
            cmd.extend(["--select", cfg.select])
        if cfg.exclude:
            cmd.extend(["--exclude", cfg.exclude])
        if cfg.full_refresh:
            cmd.append("--full-refresh")
        if cfg.threads:
            cmd.extend(["--threads", str(cfg.threads)])
        cmd.extend(["--target", cfg.target])
        cmd.extend(["--project-dir", cfg.project_dir])
        if cfg.profiles_dir:
            cmd.extend(["--profiles-dir", cfg.profiles_dir])
        return cmd
```

### dbt installation requirement

dbt-core is not a Carve dependency — Carve assumes it's installed and available. This is the right boundary because:

- Users have specific dbt-core versions tied to their project
- Forcing Carve's dbt version causes conflict
- dbt has many adapter packages (`dbt-snowflake`, `dbt-bigquery`, etc.) — Carve shouldn't pick

The `carve doctor` command (M3) verifies dbt is installed and on the PATH.

If `dbt` isn't found when a dbt step runs, the runner returns a clear error: "dbt is not installed or not on the PATH. Install with `pip install dbt-snowflake` (or your adapter)."

### Output parsing

dbt produces structured logs in JSON-lines format when `DBT_LOG_FORMAT=json` is set. Use this for structured data:

```python
def _stream_dbt_logs(self, run_id: str, proc):
    for line in iter(proc.stdout.readline, b""):
        decoded = line.decode("utf-8", errors="replace").rstrip()
        try:
            event = json.loads(decoded)
            self._handle_dbt_event(run_id, event)
        except json.JSONDecodeError:
            # Plain log line
            self.repo.append_log(run_id, "info", "runner", decoded)
    self._finalize(run_id, proc.wait())
```

Structured events include node start/finish, test results, run summaries. We can extract per-model timing and test pass/fail counts, surface in the UI's dbt run view (M3).

For M2, just log everything plus track summary statistics (models run, tests passed/failed) in the run row.

### dbt Cloud as an alternative executor (stretch)

If time permits in M2 (it likely won't — leave for M3+), the `dbt` step type could target dbt Cloud's API instead of shelling out to dbt-core. The config would specify:

```toml
[runner.dbt]
mode = "cloud"
account_id = "..."
project_id = "..."
job_id = "..."
api_token = "${DBT_CLOUD_API_TOKEN}"
```

This is on the roadmap, not in M2's scope.

## The manifest reader

`src/carve/core/dbt/manifest.py`:

```python
class DbtManifest:
    def __init__(self, manifest_path: Path):
        self.path = manifest_path
        self._raw = None

    def load(self) -> dict:
        if self._raw is None:
            self._raw = json.loads(self.path.read_text())
        return self._raw

    def model_by_name(self, name: str) -> ModelInfo | None: ...
    def downstream_of(self, model_name: str) -> list[str]: ...
    def upstream_of(self, model_name: str) -> list[str]: ...
    def columns_of(self, model_name: str) -> list[ColumnInfo]: ...
    def tests_on(self, model_name: str) -> list[TestInfo]: ...
    def models_in_path(self, path_glob: str) -> list[str]: ...
    def all_sources(self) -> list[SourceInfo]: ...
    def materialization_of(self, model_name: str) -> str: ...
```

Each method returns Pydantic models. Internal lookups walk the manifest's graph and node dictionaries.

### Manifest freshness

The manifest is generated by `dbt parse` or any other dbt command. Carve doesn't generate it.

A lightweight check on manifest age:

```python
def is_stale(self, project_dir: Path) -> bool:
    """Returns True if any .sql or .yml file is newer than the manifest."""
    manifest_mtime = self.path.stat().st_mtime
    for sql in (project_dir / "models").rglob("*.sql"):
        if sql.stat().st_mtime > manifest_mtime:
            return True
    return False
```

If the manifest is stale, Carve runs `dbt parse` automatically before reading it. This costs a few seconds but ensures the agent works against current data.

### Caching

The manifest is large (often 10-50 MB). Cache the parsed dict in the manifest object's lifetime; reload on file change.

For the agent's `query_dbt_manifest` skill, the loader is shared across the run — one parse per agent invocation.

## CLI command

`carve dbt <command>` — passthrough wrapper that runs dbt with Carve's resolved project_dir, profiles_dir, target:

```python
def dbt_command(args: list[str]):
    config = load_config()
    cmd = ["dbt"] + args + [
        "--project-dir", config.dbt.project_dir,
        "--target", config.dbt.target,
    ]
    subprocess.run(cmd, check=True)
```

This is convenience — users can always run `dbt` directly. The wrapper just saves them remembering paths.

## Tests

- A dbt step config produces the correct command-line invocation
- Missing dbt binary returns a clear error
- Manifest reader returns correct results for fixture project
- Stale manifest triggers re-parse
- Pass-through CLI invokes dbt with correct flags

Use the same fixture dbt project as the dbt agent.

## Acceptance criteria

- Pipelines can include `dbt` steps that run successfully
- The manifest reader exposes the queries the agent needs
- Logs from dbt are captured and persisted
- `carve dbt run` works as a passthrough

## Files

- `src/carve/core/steps/dbt.py`
- `src/carve/core/runners/dbt.py`
- `src/carve/core/dbt/__init__.py`
- `src/carve/core/dbt/manifest.py`
- `src/carve/core/dbt/types.py`
- `src/carve/cli/commands/dbt.py`
- `tests/core/dbt/test_manifest.py`
- `tests/core/runners/test_dbt_runner.py`

## What this enables

- The dbt agent can verify its output by running dbt commands
- Pipelines can include dbt steps mixed with other step types
- Manifest queries power schema retrieval (M2-09)
