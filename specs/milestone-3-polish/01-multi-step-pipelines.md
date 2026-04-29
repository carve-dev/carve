# M3-01 — Multi-step pipelines

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 1.5 days
**Dependencies:** M1-05 (single-step runner), M2-01 (plan/apply)

## Purpose

Generalize the single-step pipeline from M1 into a real DAG: multiple steps with explicit dependencies, parallel execution where the graph allows, configurable failure modes, and inter-step output passing.

## Pipeline TOML schema

`carve/pipelines/<name>.toml`:

```toml
[pipeline]
name = "salesforce_to_marts"
description = "Pull Salesforce opportunities and refresh related dbt marts"
schedule = "0 */4 * * *"  # cron
target = "prod"
paused = false
notification_channels = ["#data-alerts"]

# Step 1: extract from Salesforce
[[steps]]
id = "extract_salesforce"
type = "python"
script = "pipelines/salesforce_to_marts/extract.py"
requirements = ["simple-salesforce>=1.12", "snowflake-connector-python"]
timeout_seconds = 1800
retries = 2
retry_backoff_seconds = 60
on_failure = "fail"

# Step 2: dbt run for staging models (depends on extract)
[[steps]]
id = "dbt_staging"
type = "dbt"
command = "build"
select = "tag:salesforce+"  # all salesforce-tagged + downstream
target = "{{ pipeline.target }}"
depends_on = ["extract_salesforce"]
on_failure = "fail"

# Step 3: source freshness check (parallel with dbt_staging)
[[steps]]
id = "freshness_check"
type = "dbt"
command = "source freshness"
select = "source:salesforce"
depends_on = ["extract_salesforce"]
on_failure = "warn"

# Step 4: notify on completion (depends on both)
[[steps]]
id = "notify"
type = "http"
method = "POST"
url = "{{ env.SLACK_WEBHOOK_URL }}"
body_json = { text = "Pipeline {{ pipeline.name }} completed: {{ run.status }}" }
depends_on = ["dbt_staging", "freshness_check"]
on_failure = "continue"  # if notify fails, run is still success
```

## Step DAG executor

`src/carve/core/runners/dag_executor.py`:

```python
class DAGExecutor:
    def __init__(self, config, repo, runners: dict):
        self.config = config
        self.repo = repo
        self.runners = runners  # type -> Runner instance

    def execute(self, pipeline: Pipeline, context: RunContext) -> PipelineResult:
        run_id = context.run_id

        # Build the dependency graph
        graph = self._build_graph(pipeline.steps)
        self._validate_acyclic(graph)

        # Execute in topological order with parallelism
        results: dict[str, StepResult] = {}
        ready: set[str] = self._initial_ready(graph)
        in_flight: dict[str, RunHandle] = {}

        while ready or in_flight:
            # Start ready steps
            while ready and len(in_flight) < self.config.runner.max_concurrent_steps:
                step_id = ready.pop()
                step = pipeline.get_step(step_id)
                runner = self.runners[step.type]
                handle = runner.execute(step, context)
                in_flight[step_id] = handle

            # Wait for any in-flight to complete
            done_id, result = self._wait_any(in_flight)
            results[done_id] = result
            del in_flight[done_id]

            # Process result
            if result.status == "failed":
                action = self._handle_failure(pipeline.get_step(done_id), result, results, ready)
                if action == "abort":
                    self._cancel_all(in_flight)
                    return PipelineResult(status="failed", step_results=results)

            # Find newly-ready steps
            for next_id in graph.successors(done_id):
                if all(dep in results and results[dep].status == "success"
                       for dep in graph.predecessors(next_id)):
                    ready.add(next_id)

        return PipelineResult(status="success", step_results=results)
```

## Failure modes per step

- **`fail`** (default): step failure aborts the pipeline; downstream steps don't run
- **`warn`**: step failure marks the step as warned, downstream proceeds
- **`continue`**: step failure is ignored, downstream proceeds (status reflects the warning)
- **`retry`**: configured retries kick in; if all exhaust, fall back to `fail`
- **`skip_downstream`**: downstream of this step is skipped, but parallel branches continue

## Inter-step output passing

Steps can produce outputs that downstream steps reference:

```toml
[[steps]]
id = "extract"
type = "python"
script = "pipelines/x/extract.py"
outputs = ["row_count", "extracted_at"]  # named outputs the script writes

[[steps]]
id = "validate"
type = "python"
script = "pipelines/x/validate.py"
depends_on = ["extract"]
env = {
    EXPECTED_ROWS = "{{ steps.extract.outputs.row_count }}",
    EXTRACTED_AT = "{{ steps.extract.outputs.extracted_at }}"
}
```

Mechanism: a step writes outputs to a known file (`/tmp/carve-outputs/<run_id>/<step_id>.json`) before exit. The DAG executor reads it and exposes via Jinja templating.

For Python steps, a helper:

```python
# in user's script
from carve import outputs

outputs.set("row_count", 1234)
outputs.set("extracted_at", "2026-01-15T12:00:00Z")
```

Implemented as `carve.outputs` package shipped with Carve, available in user scripts via `pip install carve` (or by adding to step requirements).

## Jinja templating

Steps support Jinja in string fields. Available variables:

- `pipeline.*` — pipeline metadata
- `run.*` — current run metadata (id, started_at, target)
- `steps.*.outputs.*` — outputs from prior steps
- `env.*` — environment variables (limited set for safety)

Sandboxing: Jinja's environment is restricted; no `{% set %}`, no `{% include %}`, only attribute/key access. Prevents accidental complexity in TOML files.

## Validation

Before running a pipeline:

- All step IDs are unique within the pipeline
- All `depends_on` references exist
- The graph is acyclic
- All step types are registered
- Each step's config validates against its type's schema
- All Jinja templates parse

Validation runs at config-load time (at startup) and at apply time. Failures produce clear errors.

## Tests

- A simple two-step pipeline runs successfully
- Parallel steps run concurrently
- Cyclic dependency is rejected at load time
- `on_failure = "warn"` continues the pipeline
- Output passing works between steps
- Cancellation propagates to in-flight steps

## Acceptance criteria

- Multi-step pipelines run with correct ordering and parallelism
- Failure modes behave as documented
- Output passing works between steps
- The CLI's `carve run <pipeline>` runs through the DAG executor

## Files

- `src/carve/core/pipeline/__init__.py`
- `src/carve/core/pipeline/schema.py`
- `src/carve/core/pipeline/loader.py`
- `src/carve/core/runners/dag_executor.py`
- `src/carve/core/runners/jinja.py`
- `src/carve/outputs/__init__.py` (helper package)
- `tests/core/runners/test_dag_executor.py`
- `tests/core/pipeline/test_loader.py`

## What this enables

- Real-world pipelines (extract → transform → test → notify) can be expressed naturally
- The future M3 step types (sql, shell, http) plug into the same executor
- Pipelines become observable graphs in the UI
