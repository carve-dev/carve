# M2-01 — Plan/apply workflow

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M1-02 (config), M1-03 (state store)

## Purpose

Formalize the plan/apply lifecycle. Plans become first-class persisted artifacts with task graphs, cost estimates, file diffs, config-hash validation, and refinement. `carve build` becomes a polite shorthand for plan + interactive confirm + apply.

## Plan schema

`src/carve/core/plan/schema.py`:

```python
from datetime import datetime, timedelta
from pydantic import BaseModel, Field

class FileDiff(BaseModel):
    path: str
    kind: str  # "create" | "modify" | "delete"
    preview: str | None = None  # truncated diff text

class TaskInput(BaseModel):
    """Inputs that will be passed to the agent for this task."""
    pass  # free-form dict in practice

class Task(BaseModel):
    step: int
    agent: str  # "orchestration" | "dbt" | "snowflake" | etc.
    action: str  # short action name like "generate_extractor" | "modify_model"
    inputs: dict
    expected_outputs: list[FileDiff] = []

class PlanEstimates(BaseModel):
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    duration_seconds: int = 0
    snowflake_credits: float = 0.0

class Plan(BaseModel):
    id: str  # "plan_<hex>"
    parent_plan_id: str | None = None
    created_at: datetime
    expires_at: datetime
    goal: str
    config_hash: str
    carve_version: str
    task_graph: list[Task]
    estimates: PlanEstimates
    guardrail_check: str = "passed"  # "passed" | "failed"
    skipped_agents: list[str] = []  # for transparency
    skipped_reason: dict[str, str] = {}  # agent -> why
    file_diffs: list[FileDiff] = []
```

## Plan store

`src/carve/core/plan/store.py`:

```python
class PlanStore:
    def __init__(self, project_dir: Path, repo: Repository):
        self.dir = project_dir / ".carve" / "plans"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.repo = repo

    def save(self, plan: Plan) -> Path:
        path = self.dir / f"{plan.id}.json"
        path.write_text(plan.model_dump_json(indent=2))
        self.repo.save_plan_index(plan, path)
        return path

    def load(self, plan_id: str) -> Plan:
        path = self.dir / f"{plan_id}.json"
        if not path.exists():
            raise PlanNotFoundError(plan_id)
        return Plan.model_validate_json(path.read_text())

    def list_recent(self, limit: int = 50) -> list[Plan]:
        return [self.load(p.id) for p in self.repo.list_plans(limit=limit)]

    def diff(self, plan_a: Plan, plan_b: Plan) -> str:
        # Render a human-readable diff between two plans
        ...
```

## Plan generation flow

The CLI's `carve plan` command:

```python
def plan_command(goal: str):
    config = load_config()
    repo = get_repository(config)

    # 1. Run the orchestration agent
    orchestrator = OrchestrationAgent(config, repo)
    plan = orchestrator.generate_plan(goal)

    # 2. Validate guardrails
    plan.guardrail_check = validate_guardrails(plan, config)

    # 3. Save
    plan_store = PlanStore(config.project_dir, repo)
    plan_store.save(plan)

    # 4. Print summary
    render_plan_to_console(plan)
```

## Plan refinement

`carve plan --refine <plan_id> "<adjustment>"`:

1. Load the parent plan
2. Pass the parent plan + the adjustment to the orchestrator
3. The orchestrator returns a new plan with `parent_plan_id` set
4. Save with a new `plan_id`

The orchestrator's prompt for refinement gets both the original goal and the adjustment, plus the existing task graph as context. Reuse work where possible — don't re-run impact analysis if it hasn't changed.

## Apply with hash validation

`carve apply <plan_id>`:

```python
def apply_command(plan_id: str):
    config = load_config()
    plan_store = PlanStore(config.project_dir, get_repository(config))

    # 1. Load
    plan = plan_store.load(plan_id)

    # 2. Check expiry
    if datetime.utcnow() > plan.expires_at:
        raise PlanExpiredError(plan_id)

    # 3. Check config hash
    if plan.config_hash != config.config_hash:
        raise ConfigDriftError(
            "Config has changed since this plan was generated. Run 'carve plan' again."
        )

    # 4. Check guardrails again (in case they changed)
    if plan.guardrail_check != "passed":
        raise GuardrailViolationError(plan_id)

    # 5. Execute the task graph
    executor = TaskGraphExecutor(config)
    run_id = executor.execute(plan)

    # 6. Open PR (M2-13)
    pr_url = open_pr_for_run(run_id, plan)

    # 7. Update plan as applied
    plan_store.mark_applied(plan_id, run_id)

    return run_id, pr_url
```

## Task graph executor

For M2, the task graph is sequential. The executor walks tasks in order, invokes the right agent for each, captures outputs, and records the run.

```python
class TaskGraphExecutor:
    def __init__(self, config: Config):
        self.config = config
        self.repo = get_repository(config)
        self.agents = {
            "orchestration": OrchestrationAgent(config, self.repo),
            "dbt": DbtAgent(config, self.repo),
            "snowflake": SnowflakeAgent(config, self.repo),
        }

    def execute(self, plan: Plan) -> str:
        run_id = self.repo.create_run(kind="apply", target_id=plan.id)
        try:
            for task in plan.task_graph:
                agent = self.agents[task.agent]
                agent.execute_task(task, run_id=run_id)
            self.repo.update_run_status(run_id, "success")
        except Exception as e:
            self.repo.update_run_status(run_id, "failed", error=str(e))
            raise
        return run_id
```

M3 generalizes this to a real DAG executor with parallelism.

## CLI plan rendering

Render a plan to the terminal using `rich`:

```
Plan: plan_a3f291
Goal: make stg_orders incremental with order_id as the unique key

Goal classification: modification
Scope analysis:
  - Touches: dbt/models/staging/stg_orders.sql
  - Downstream: 4 models
  - No new sources, no permission changes

Agents involved: dbt, quality
Skipped: pipeline (no new sources), snowflake (no DDL changes)

Task graph (3 steps):
  1. dbt           Modify stg_orders to incremental
  2. dbt           Verify downstream compatibility
  3. quality       Add incremental tests

Estimated cost:    $0.18
Estimated duration: 45s

Apply with: carve apply plan_a3f291
```

## CLI commands added

- `carve plan "<goal>"` — generate plan, save, print
- `carve plan show <plan_id>` — re-print
- `carve plan list` — list recent plans
- `carve plan diff <p1> <p2>` — compare
- `carve plan --refine <plan_id> "<adjustment>"` — produce a child plan
- `carve apply <plan_id>` — execute saved plan
- `carve build "<goal>"` — plan + interactive confirm + apply
- `carve build "<goal>" --yes` — plan + apply, no confirm
- `carve build "<goal>" --dry-run` — alias for `carve plan`

## Tests

- Plan generation produces a valid Plan object with task graph
- Save + load round-trip is faithful
- Hash mismatch on apply raises `ConfigDriftError`
- Expired plan on apply raises `PlanExpiredError`
- Refinement creates a child plan with correct `parent_plan_id`
- Guardrail violation blocks apply
- Apply records a run with the plan's task graph

## Acceptance criteria

- A plan generated by the orchestrator can be saved, loaded, applied
- The CLI flow `carve plan → carve apply` works end-to-end
- Config drift is detected and refused
- Refinement chains preserve parent IDs

## Files

- `src/carve/core/plan/__init__.py`
- `src/carve/core/plan/schema.py`
- `src/carve/core/plan/store.py`
- `src/carve/core/plan/executor.py`
- `src/carve/core/plan/render.py`
- `src/carve/core/plan/exceptions.py`
- `src/carve/cli/commands/plan.py` (real impl, replacing M1 stub)
- `src/carve/cli/commands/apply.py` (real impl)
- `src/carve/cli/commands/build.py`
- `tests/core/plan/test_store.py`
- `tests/core/plan/test_executor.py`

## What this enables

- The orchestrator's output has a structured home
- The web UI can render plans
- Refinement gives users iterative control before committing to expensive operations
