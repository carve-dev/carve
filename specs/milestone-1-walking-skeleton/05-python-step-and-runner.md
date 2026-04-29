# M1-05 — Python step and LocalVenvRunner

**Milestone:** 1 — Walking skeleton
**Estimated effort:** 1 day
**Dependencies:** M1-02 (config), M1-03 (state store)

## Purpose

Define the Step protocol that all step types will implement, build the first concrete step type (`PythonStep`), and build the runner that executes Python steps inside isolated virtual environments. This is what turns the agent's generated Python script into actual data movement.

## Scope

### In scope

- `Step` protocol (interface all step types will implement)
- `PythonStep` implementation
- `Runner` protocol
- `LocalVenvRunner` that creates venvs, installs deps, executes scripts, captures logs
- Venv caching to avoid re-installing every run
- Subprocess management with timeout and cancellation
- Log streaming back to the state store via repository

### Out of scope

- SQL, dbt, shell, http step types (M3)
- Step DAG with dependencies (M3 — M1 has single-step pipelines)
- DockerRunner (SaaS / future)
- Step-output passing between steps (M3)
- Approval steps (M3)

## The Step protocol

`src/carve/core/steps/base.py`:

```python
from typing import Protocol, runtime_checkable
from pydantic import BaseModel

class StepConfig(BaseModel):
    """Base for all step type configs."""
    id: str
    timeout_seconds: int = 1800
    retries: int = 0
    retry_backoff_seconds: int = 60

class StepResult(BaseModel):
    status: str  # "success" | "failed" | "cancelled"
    duration_ms: int
    outputs: dict = {}
    error: str | None = None

@runtime_checkable
class Step(Protocol):
    """Every step type implements this."""
    config: StepConfig

    def validate(self, config_dict: dict) -> StepConfig:
        """Validate the config from TOML. Returns parsed config."""
        ...

    @property
    def step_type(self) -> str:
        """The string used in TOML's `type = "..."` field."""
        ...
```

For M1, the M1 demo pipeline is a single Python step, defined directly by the agent's output. There's no pipeline TOML to load yet (that comes in M2).

## PythonStep

`src/carve/core/steps/python.py`:

```python
class PythonStepConfig(StepConfig):
    script: str  # path relative to project root
    requirements: list[str] = []  # pip-installable dependency strings
    env: dict[str, str] = {}  # additional env vars to pass

class PythonStep:
    step_type = "python"

    def __init__(self, config: PythonStepConfig):
        self.config = config

    def validate(self, config_dict: dict) -> PythonStepConfig:
        return PythonStepConfig.model_validate(config_dict)
```

The step itself is just a config holder — actual execution happens in the runner.

## The Runner protocol

`src/carve/core/runners/base.py`:

```python
class RunHandle(BaseModel):
    run_id: str
    process_id: int

class Runner(Protocol):
    def execute(self, step: Step, context: RunContext) -> RunHandle:
        """Start execution. Returns immediately with a handle."""
        ...

    async def stream_logs(self, run_id: str) -> AsyncIterator[LogLine]:
        """Stream logs as they're produced."""
        ...

    def get_status(self, run_id: str) -> str:
        """Returns current run status."""
        ...

    def cancel(self, run_id: str) -> None:
        """Cancel a running step."""
        ...

    def wait(self, run_id: str) -> StepResult:
        """Block until the step completes; return the result."""
        ...
```

Note that `execute()` is non-blocking. `wait()` is blocking. This separation lets the future API server start a step and return a 200 immediately while the run continues in the background.

## LocalVenvRunner

The OSS implementation. `src/carve/core/runners/local_venv.py`:

```python
class LocalVenvRunner:
    def __init__(self, config: RunnerConfig, repo: Repository):
        self.config = config
        self.repo = repo
        self.processes: dict[str, subprocess.Popen] = {}

    def execute(self, step: PythonStep, context: RunContext) -> RunHandle:
        run_id = context.run_id

        # 1. Get or create the venv
        venv_path = self._ensure_venv(step.config.requirements)

        # 2. Build the command
        python = venv_path / "bin" / "python"
        script_abs = context.project_dir / step.config.script

        # 3. Build env
        env = os.environ.copy()
        env.update(step.config.env)
        env.update(self._snowflake_env(context.target))

        # 4. Spawn subprocess
        proc = subprocess.Popen(
            [str(python), str(script_abs)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=context.project_dir,
        )
        self.processes[run_id] = proc

        # 5. Spawn log-streaming thread
        threading.Thread(
            target=self._stream_logs_to_repo,
            args=(run_id, proc),
            daemon=True,
        ).start()

        return RunHandle(run_id=run_id, process_id=proc.pid)
```

### Venv caching

Venvs are expensive to create (~3-10 seconds depending on dependencies). Cache them per unique requirements set:

```python
def _ensure_venv(self, requirements: list[str]) -> Path:
    # Hash the sorted requirements list
    req_hash = hashlib.sha256(
        "\n".join(sorted(requirements)).encode()
    ).hexdigest()[:16]

    venv_dir = Path(self.config.venv_cache_dir) / req_hash

    if not venv_dir.exists():
        # Create venv
        subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
        # Install deps
        pip = venv_dir / "bin" / "pip"
        subprocess.check_call([str(pip), "install", *requirements])

    return venv_dir
```

Cache invalidation is implicit — different requirements produce a different hash. Old venvs accumulate on disk; periodic cleanup is a future task (M3 can add `carve clean` command).

Cap total disk usage at 5GB by default; warn if exceeded.

### Snowflake env injection

The agent generates Python scripts that read Snowflake credentials from environment variables. The runner injects these from the active connection:

```python
def _snowflake_env(self, target: str) -> dict[str, str]:
    sf = self.config.connections.snowflake[target]
    return {
        "SNOWFLAKE_ACCOUNT": sf.account,
        "SNOWFLAKE_USER": sf.user,
        "SNOWFLAKE_PASSWORD": sf.password or "",
        "SNOWFLAKE_ROLE": sf.role,
        "SNOWFLAKE_WAREHOUSE": sf.warehouse,
        "SNOWFLAKE_DATABASE": sf.database,
        "SNOWFLAKE_SCHEMA": sf.schema or "",
    }
```

The agent's M1 system prompt tells it to read from these env vars. M2 may make this more sophisticated (e.g., Snowflake key-pair auth).

### Log streaming

Subprocess output is captured line-by-line and written to the repository:

```python
def _stream_logs_to_repo(self, run_id: str, proc: subprocess.Popen):
    for line in iter(proc.stdout.readline, b""):
        decoded = line.decode("utf-8", errors="replace").rstrip()
        self.repo.append_log(
            run_id=run_id,
            level="info",
            source="runner",
            message=decoded,
        )
    proc.wait()
    self._finalize_run(run_id, proc.returncode)
```

The future WebSocket layer (M2) reads from the repo's logs table to stream to clients.

### Timeout and cancellation

Track run start time. A separate watchdog thread or `psutil`-based check kills runs that exceed the configured timeout:

```python
def _watchdog(self, run_id: str, timeout_seconds: int):
    time.sleep(timeout_seconds)
    if run_id in self.processes:
        self.cancel(run_id)
```

`cancel()` sends SIGTERM, waits 5 seconds, then SIGKILL if needed.

### Finalizing a run

When the subprocess exits:

1. Read final exit code
2. Compute duration
3. Update the run row in the state store with status (`success` or `failed`)
4. Compute token cost from cumulative agent usage
5. Emit `run.completed` event (M2 — for now, just the DB update)

## RunContext

The bundle of information a runner needs:

```python
class RunContext(BaseModel):
    run_id: str
    project_dir: Path
    target: str
    config: Config
```

## Tests

- A simple Python step that writes "hello" to stdout produces a successful run with that log line
- A failing script (non-zero exit) is recorded as `failed`
- Timeout triggers cancellation
- Venv caching: identical requirements produce the same venv path; different ones produce different paths
- Snowflake env vars are injected when target is configured
- Path traversal in `script` field is blocked

For integration testing, use a tiny script that prints the env vars to verify injection.

## Acceptance criteria

- A `PythonStep` configured with a script and requirements runs successfully
- Logs stream to the state store as the script runs
- Failed scripts produce a `failed` run with the exit code in the error message
- Venv cache hit on repeated identical requirements
- Snowflake credentials are passed via env vars, never logged
- Cancellation works (SIGTERM, then SIGKILL)
- Tests pass on Linux and macOS

## Files this spec produces

- `src/carve/core/steps/__init__.py`
- `src/carve/core/steps/base.py`
- `src/carve/core/steps/python.py`
- `src/carve/core/runners/__init__.py`
- `src/carve/core/runners/base.py`
- `src/carve/core/runners/local_venv.py`
- `tests/core/steps/test_python.py`
- `tests/core/runners/test_local_venv.py`

## What this enables

- The M1 demo flow: agent generates a script, runner executes it
- Future step types reuse the `Step` protocol
- Future runners reuse the `Runner` protocol
- Multi-step pipelines (M3) plug into the same runner
