# M3-07 — Custom step types

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 0.5 day
**Dependencies:** M1-05 (step protocol), M3-01 (DAG executor)

## Purpose

Mirror the skills SDK pattern for step types: users can drop a Python file in `carve/steps/` defining a new step type, and Carve registers and uses it like a built-in.

## Why now

Same reason as the skills SDK: by M3, the built-in step types have stabilized, and we know what shape the extension should take.

## The base class

`src/carve/steps/base.py` (already exists from M1-05; we add the public extension API):

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel
from carve.steps import StepConfig, StepResult

class StepType(ABC):
    """Subclass this to define a new step type."""

    name: str  # the string used in TOML's `type = "..."` field
    config_schema: type[StepConfig]  # Pydantic model for this type's config

    @abstractmethod
    def execute(self, ctx: StepContext, config: StepConfig) -> StepResult:
        """Execute the step. Implement this with your logic."""
        ...

    def validate(self, config_dict: dict) -> StepConfig:
        """Default validation uses the config_schema."""
        return self.config_schema.model_validate(config_dict)
```

## User-facing example

`carve/steps/datadog_metric.py`:

```python
from carve.steps import StepType, StepConfig, StepResult, StepContext
import requests

class DatadogMetricConfig(StepConfig):
    metric: str
    threshold: float
    operator: str = "gt"  # gt, lt, eq

class DatadogMetricStep(StepType):
    name = "datadog_metric"
    config_schema = DatadogMetricConfig

    def execute(self, ctx: StepContext, config: DatadogMetricConfig) -> StepResult:
        ctx.log(f"Checking {config.metric} {config.operator} {config.threshold}")

        api_key = os.environ["DATADOG_API_KEY"]
        # ... fetch metric

        passed = self._compare(value, config.threshold, config.operator)
        return StepResult(
            status="success" if passed else "failed",
            duration_ms=elapsed_ms,
            outputs={"value": value, "passed": passed},
            error=None if passed else f"Metric {config.metric} = {value}, expected {config.operator} {config.threshold}",
        )
```

Now in a pipeline TOML:

```toml
[[steps]]
id = "verify_pipeline_health"
type = "datadog_metric"
metric = "etl.row_count"
threshold = 10000
operator = "gt"
on_failure = "fail"
depends_on = ["extract", "load"]
```

## Discovery

Same pattern as skills:

```python
def discover_step_types(steps_dir: Path) -> list[type[StepType]]:
    if not steps_dir.exists():
        return []
    types = []
    for py_file in steps_dir.rglob("*.py"):
        if py_file.name.startswith("_"):
            continue
        module = import_user_module(py_file)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, StepType) and attr is not StepType:
                types.append(attr)
    return types
```

Discovered types are registered in the runners dictionary so the DAG executor finds them:

```python
runners = {
    "python": LocalVenvRunner(...),
    "sql": SqlRunner(...),
    "dbt": DbtRunner(...),
    "shell": ShellRunner(...),
    "http": HttpRunner(...),
    "datadog_metric": CustomStepRunner(DatadogMetricStep()),  # discovered
}
```

## CustomStepRunner

A thin runner that wraps user-defined StepTypes — they don't subprocess; they just run their `execute()` in a thread:

```python
class CustomStepRunner(Runner):
    def __init__(self, step_type: StepType):
        self.step_type = step_type

    def execute(self, step, context):
        thread = threading.Thread(
            target=self._run,
            args=(step, context),
            daemon=True,
        )
        thread.start()
        return RunHandle(run_id=context.run_id, process_id=0)

    def _run(self, step, context):
        try:
            ctx = StepContext(
                run_id=context.run_id,
                project_dir=context.project_dir,
                target=context.target,
                # ... logger, output writer, etc.
            )
            result = self.step_type.execute(ctx, step.config)
            self._record_result(context.run_id, step.id, result)
        except Exception as e:
            self._record_failure(context.run_id, step.id, str(e))
```

User code runs in the Carve process (no subprocess isolation). Different from `python` step type, which spawns subprocess for isolation. Custom step types are for short-running, pure-Python operations.

If a user needs subprocess isolation, they can wrap their logic in a Python step.

## StepContext

What user code receives:

```python
class StepContext:
    run_id: str
    project_dir: Path
    target: str
    config: Config

    def log(self, message: str, level: str = "info"): ...
    def get_upstream_output(self, step_id: str, key: str) -> any: ...
    def set_output(self, key: str, value: any): ...
```

Lets user code participate in the same logging, output passing, and config access as built-ins.

## Type stubs

Same approach as skills SDK:

```python
# src/carve/steps/__init__.pyi
class StepType:
    name: str
    config_schema: type[StepConfig]
    def execute(self, ctx: StepContext, config: StepConfig) -> StepResult: ...

class StepConfig(BaseModel):
    id: str
    timeout_seconds: int
    retries: int

class StepResult(BaseModel):
    status: str
    duration_ms: int
    outputs: dict
    error: str | None
```

## CLI

- `carve step list` — list all step types (built-in + custom)
- `carve step show <type>` — print config schema and source location

## Tests

- A custom step type file is discovered
- Custom step types are usable in pipeline TOML
- Config validation works against the user's schema
- Outputs from custom steps pass to downstream steps

## Acceptance criteria

- A user can drop a Python file in `carve/steps/` defining a `StepType` subclass and have it work
- Custom step type appears in `carve step list`
- Pipeline TOML can use the custom type
- Pipeline runs use the custom step's `execute()` method

## Files

- `src/carve/steps/__init__.py` (extends M1 with public API)
- `src/carve/steps/__init__.pyi`
- `src/carve/steps/discovery.py`
- `src/carve/core/runners/custom.py`
- `src/carve/cli/commands/step.py`
- `tests/core/steps/test_custom.py`
- `examples/custom-step/` (example)

## What this enables

- Domain-specific step types for organizations (Snowflake-managed task triggers, Spark job submissions, etc.)
- Carve becomes a generic orchestrator for the user's stack, not just dbt + Snowflake
