# M3-06 — Skills SDK

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 1 day
**Dependencies:** M2-08 (skills registry)

## Purpose

Let users define custom skills in their own Python files without modifying Carve's source. A user drops a `.py` file in `carve/skills/`, decorates a function with `@skill`, and Carve discovers and registers it on startup.

## Why now

By M3, the built-in skills have stabilized and we know what shape they should take. Earlier extension API design risks shipping the wrong abstraction. Now we ship one based on real patterns.

## The decorator

`src/carve/skills/decorator.py`:

```python
from typing import Callable
from pydantic import BaseModel

def skill(
    name: str,
    description: str,
    timeout_seconds: int = 60,
    cacheable: bool = True,
    tags: list[str] = None,
):
    """Decorator to register a function as a Carve skill.

    The function's type-annotated parameters become the skill's input schema.
    The function's return type annotation becomes the output schema.
    The first parameter must be a SkillContext.
    """
    def decorator(func: Callable):
        sig = inspect.signature(func)
        input_schema = _build_input_schema(sig)
        output_schema = _build_output_schema(sig.return_annotation)

        skill_obj = Skill(
            name=name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            timeout_seconds=timeout_seconds,
            cacheable=cacheable,
            tags=tags or [],
            impl=func,
            module=func.__module__,
        )
        _global_registry.register(skill_obj)
        return func
    return decorator
```

## User-facing API

A user writes `carve/skills/datadog.py`:

```python
from carve.skills import skill, SkillContext
from pydantic import BaseModel
import requests

class DatadogMetric(BaseModel):
    metric: str
    value: float
    timestamp: str

@skill(
    name="get_datadog_metric",
    description="Fetch the latest value for a Datadog metric",
    timeout_seconds=30,
    cacheable=True,
    tags=["observability", "datadog"],
)
def get_datadog_metric(
    ctx: SkillContext,
    metric: str,
    window_minutes: int = 60,
) -> DatadogMetric:
    """Returns the latest value of the named Datadog metric within the window."""
    api_key = os.environ["DATADOG_API_KEY"]
    response = requests.get(
        f"https://api.datadoghq.com/api/v1/query",
        headers={"DD-API-KEY": api_key},
        params={
            "query": f"avg:{metric}",
            "from": (datetime.now() - timedelta(minutes=window_minutes)).timestamp(),
            "to": datetime.now().timestamp(),
        }
    )
    series = response.json()["series"]
    latest = series[0]["pointlist"][-1]
    return DatadogMetric(
        metric=metric,
        value=latest[1],
        timestamp=str(latest[0]),
    )
```

Result: agents can now use `get_datadog_metric` like any built-in skill.

## Discovery

On startup (or on `carve serve` reload):

```python
def discover_skills(skills_dir: Path) -> list[Skill]:
    if not skills_dir.exists():
        return []

    for py_file in skills_dir.rglob("*.py"):
        if py_file.name.startswith("_"):
            continue
        module_name = f"_carve_user_skills.{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Decorator registers in _global_registry as a side effect

    return _global_registry.user_skills()
```

User code is loaded with limited safety (it's their code, in their project, run with their permissions). Carve doesn't sandbox.

## Schema generation from annotations

The `@skill` decorator inspects the function signature:

```python
def get_datadog_metric(
    ctx: SkillContext,        # excluded from schema
    metric: str,              # required string
    window_minutes: int = 60, # optional integer with default
) -> DatadogMetric:           # output schema
```

Becomes the skill's tool schema for the LLM:

```json
{
  "name": "get_datadog_metric",
  "description": "Fetch the latest value for a Datadog metric",
  "input_schema": {
    "type": "object",
    "properties": {
      "metric": {"type": "string"},
      "window_minutes": {"type": "integer", "default": 60}
    },
    "required": ["metric"]
  }
}
```

Pydantic models are flattened into JSON schema; primitive types map directly. Unsupported types (custom classes that aren't BaseModels) raise a clear error at registration.

## Per-skill timeout

Skills declare their own timeout. The executor enforces it:

```python
def execute_skill(skill, args, ctx):
    try:
        return func_timeout(skill.timeout_seconds, skill.impl, args=(ctx,), kwargs=args)
    except FunctionTimedOut:
        return {"error": f"Skill {skill.name} timed out after {skill.timeout_seconds}s"}
```

Use `func_timeout` library or `concurrent.futures` with a thread pool.

## Per-agent allowlists

Existing pattern from M3-04 — agents declare which skills they can use:

```yaml
# carve/agents/dbt_agent.yaml
name: dbt
allowed_skills:
  - read_file
  - write_file
  - run_dbt_command
  - get_datadog_metric  # custom skill, allowed for this agent
```

Skills not in the allowlist are not exposed in that agent's tool schema.

## Testing custom skills

`carve skill test <name>` runs the skill with provided args:

```bash
carve skill test get_datadog_metric --metric system.cpu.user --window-minutes 30
```

Outputs the result or error. Useful for users to debug their skills before invoking via an agent.

## CLI

- `carve skill list` — list all registered skills (built-in + custom)
- `carve skill show <name>` — print full definition (input/output schema, source location)
- `carve skill test <name> --arg value` — invoke directly

## Type stubs

Ship type stubs for IDE support:

```python
# src/carve/skills/__init__.pyi
from typing import Callable, TypeVar

T = TypeVar("T")

def skill(
    name: str,
    description: str,
    timeout_seconds: int = 60,
    cacheable: bool = True,
    tags: list[str] | None = None,
) -> Callable[[T], T]: ...

class SkillContext:
    config: Config
    repo: Repository
    run_id: str
    target: str
    snowflake_pool: SnowflakePool
    dbt_manifest: DbtManifest

    def log(self, message: str, level: str = "info") -> None: ...
    def emit_event(self, event: str, payload: dict) -> None: ...
```

## Tests

- A user file with `@skill` is discovered
- Type annotations correctly produce schemas
- Pydantic models in returns are flattened
- Allowlist filters work
- Timeout enforcement works
- Skills can use connections via SkillContext

## Acceptance criteria

- A user can drop a Python file in `carve/skills/` and have it work
- Type-annotated functions produce correct JSON schemas
- Per-agent allowlists work for custom skills
- IDE autocompletion works (via type stubs)

## Files

- `src/carve/skills/__init__.py`
- `src/carve/skills/__init__.pyi`
- `src/carve/skills/decorator.py`
- `src/carve/skills/discovery.py`
- `src/carve/skills/schema_gen.py`
- `src/carve/cli/commands/skill.py`
- `tests/core/skills/test_decorator.py`
- `tests/core/skills/test_discovery.py`
- `examples/custom-skill/` (an example user skill)

## What this enables

- Users extend Carve without forking
- Internal-tool integrations (Datadog, PagerDuty, custom APIs) are first-class
- The contributor surface for the project doesn't have to absorb every integration
