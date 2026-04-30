# M1.1-06 — Separate plan / build / run

**Milestone:** 1.1 — M1 follow-ups
**Estimated effort:** 1.5 days
**Dependencies:** M1-04 (agent loop), M1-05 (Python step + runner), M1 integration (orchestrator), M1.1-04 (live progress output — preferred but not strictly required), M1.1-05 (prompt tightening — preferred but not strictly required)

## Purpose

Today's `carve plan` does two jobs in one invocation: it designs the pipeline **and** writes the code. That collapse is the source of two real failures observed during M1 smoke testing:

1. The user can't iterate on the design before code is generated. By the time `plan` returns, the agent has already committed to a structure (truncate-vs-merge, schema, columns, requirements). To change it, you re-run `plan` from scratch and pay the full token cost again.
2. The lifecycle verbs lie. "Plan" implies preview; the command actually writes files. "Apply" sounds like execution; in M2 it's supposed to mean "ship to prod." The Terraform analogy doesn't fit.

Split the M1 flow into honest phases that match how a human + AI actually collaborate on a pipeline: **plan → (iterate) → build → run → (M2) apply**. Each verb does what its name says.

## Scope

### In scope

- `carve plan "<goal>"` produces a **textual** plan describing what would be built, **without writing pipeline code**. The plan covers: source, destination (drawn from connection context), data shape, transformation strategy, scale/runtime estimates, key tradeoffs, and any open questions. Persisted as a Plan row + JSON file like today, but with `phase = "planned"` and no `pipelines/<name>/` artifacts.
- `carve plan --refine <plan_id> "<feedback>"` (or simply `carve plan <plan_id> "<feedback>"`) creates a **new** plan row with `parent_plan_id = <plan_id>` and a refined design. The feedback is treated as an additional user message in a fresh agent turn. The previous plan stays on disk for history.
- `carve build <plan_id>` invokes a second agent run that consumes the plan as context and writes the actual `pipelines/<name>/main.py` + `requirements.txt`. Updates the plan row to `phase = "built"`. Idempotent — re-running on a built plan re-runs the build (with a confirmation prompt unless `--force`).
- `carve run <plan_id>` executes the built pipeline via `LocalVenvRunner`. Same logic as today's `apply`, just renamed. Validates `phase == "built"` first; refuses to run an unbuilt plan with a clear error.
- `carve apply` becomes a **stub reserved for M2**. It prints "Use `carve run` to execute in dev. `carve apply` will create a deployment PR (M2)." and exits 0. No removal — keeping the slot in the CLI surface so users who try it don't see a "no such command."
- A `phase` column on the `Plan` model: `planned | built | run`. Plus `built_at`, `built_run_id` columns for parity with the existing `applied_at`/`apply_run_id` (which we keep but rename in semantics: `applied_at` now means "ran in dev").
- `Plan.parent_plan_id` already exists from M1-03 — wire the refine path to populate it.
- `carve plans <plan_id>` (new subcommand under existing `runs`/`logs` shape) renders the lineage of a plan and its parent chain, so `--refine` history is visible. Keep it small — this is mostly for the user to confirm "yes I'm refining the right plan."

### Out of scope

- M2 prod-PR deployment via `carve apply`. This spec only reserves the verb.
- Caching the agent's reasoning across plan→build to avoid the second invocation. Two distinct agent calls is fine for M1.1.
- Cross-plan dependencies (build A then plan B with A's output as input).
- Mid-build interactivity (the build agent runs to completion; if it goes off the rails, you `carve plan --refine` and re-build).
- Diffing two plan revisions visually. `parent_plan_id` is enough for now; a `carve plan diff <a> <b>` command can come later.
- Streaming the plan text as the agent writes it. Today's "wait for the loop to finish" is fine for plan output (it's prose, not files); M1.1-04's progress observer covers the perceived-frozen problem.

## The plan document

The plan moves from "the json describes which files were written" to "the json **is** the design intent." Shape:

```json
{
  "id": "plan_20260430_174201_746f8c",
  "parent_plan_id": null,
  "phase": "planned",
  "goal": "ingest the most recent 10000 rows of Iowa liquor sales...",
  "design": {
    "source": {
      "type": "socrata_api",
      "url": "https://data.iowa.gov/resource/m3tr-qhgy.csv",
      "row_limit": 10000,
      "ordering": "date DESC"
    },
    "destination": {
      "database": "<from connection>",
      "schema": "<from connection>",
      "table": "IOWA_LIQUOR_SALES",
      "primary_key": "INVOICE_LINE_NO"
    },
    "transformation": {
      "strategy": "merge_upsert",
      "rationale": "User specified bounded row count; MERGE on PK keeps re-runs idempotent without destructive truncate."
    },
    "columns": [
      {"name": "INVOICE_LINE_NO", "type": "VARCHAR(50)"},
      {"name": "DATE", "type": "DATE"},
      ...
    ],
    "requirements": ["snowflake-connector-python", "sodapy"],
    "estimates": {
      "rows": 10000,
      "approx_runtime_minutes": 10,
      "approx_cost_usd": 0
    },
    "tradeoffs": [
      "Row-by-row MERGE is slow (~10k roundtrips). Acceptable at 10k rows; would need staging+COPY at full scale.",
      "PRIMARY KEY constraint in Snowflake is informational only.",
      "Default-role write — script doesn't pass `role=` to connect()."
    ],
    "open_questions": []
  },
  "summary_text": "<the human-readable summary the agent emits>",
  "config_hash": "...",
  "carve_version": "...",
  "tokens_input": 6785,
  "tokens_output": 3518,
  "cost_usd": 0.07,
  "model": "claude-sonnet-4-5-20250929",
  "created_at": "2026-04-30T17:42:01Z",
  "expires_at": "2026-05-01T17:42:01Z"
}
```

The `design` object is the load-bearing field — it's what `carve build` consumes. The `summary_text` is for the user. Other fields are bookkeeping.

The plan agent's prompt (separate from the build agent's prompt) is restructured to **emit a structured design** rather than write files. Implementation can use either:

- **Tool-based:** the plan agent has a single `submit_plan(design: Design)` tool. The agent returns when it calls this tool with a valid design. The schema for `design` is the plan JSON shape above.
- **Final-response parsing:** the agent's final response is a markdown document with sections matching the design shape; the orchestrator parses it into the JSON.

Tool-based is more reliable; pick that.

## The build agent

A second agent loop, with a different system prompt at `src/carve/core/agents/prompts/m1_build_agent.md`. Its prompt:

- Receives the plan's `design` object as part of its system context (not as a tool call — as a `Plan: ...` preamble).
- Receives the connection context (same as M1.1-05).
- Has only `read_file` and `write_file` tools (no `run_snowflake_query` — exploration happened during plan).
- Is told to honor the design exactly: same destination, same transformation strategy, same requirements, same column shape. Only depart from the design if it's literally impossible.
- Final response is short — names the files written, period. No "How to Run" (M1.1-05's rule still applies).

This is a much narrower agent than the M1 code agent. It should converge faster (4-6 turns) and cost less per build.

## CLI changes

### `carve plan`

```
carve plan "<goal>"                          # new plan
carve plan --refine <plan_id> "<feedback>"   # refine an existing plan
carve plan --target dev "<goal>"             # explicit target (M1 already supports default_target)
```

Output: the design as a rich-formatted summary (similar to today's plan summary, but pulled from the structured design rather than the agent's prose). Plan id printed at the bottom with both `carve build <id>` and `carve plan --refine <id>` as next-step suggestions.

### `carve build`

```
carve build <plan_id>           # build the plan, refuse if already built
carve build <plan_id> --force   # rebuild
```

Output: file list written, build agent's brief summary, run id of the build (the build itself is recorded as a Run with `kind="build"`). Suggests `carve run <plan_id>` next.

### `carve run`

```
carve run <plan_id>             # execute in dev
```

Output: same as today's `carve apply` — live log tail, final status.

Refuses to run a plan with `phase != "built"` and tells the user to `carve build <plan_id>` first.

### `carve apply`

```
carve apply <plan_id>           # M2 placeholder
```

Prints:
```
carve apply will create a deployment PR for prod (arrives in M2).
For dev execution, use:  carve run <plan_id>
```
Exits 0.

### `carve plans`

```
carve plans                     # list recent plans, top-level only
carve plans <plan_id>           # show this plan's lineage (parent chain) and refinement children
```

Renders a small tree of plan ids with their goals, phases, and timestamps. No need to be fancy — even a flat list with parent indentation is fine.

## Implementation

### Schema changes

`src/carve/core/state/models.py`:

- Add `phase: Mapped[str]` to `Plan` with a default of `"planned"`. CHECK constraint: `IN ('planned', 'built', 'run')`.
- Add `built_at: Mapped[datetime | None]` and `built_run_id: Mapped[str | None]`.
- Existing `applied_at`/`apply_run_id` — repurpose as "ran" timestamps. Or rename to `ran_at`/`run_id`; renaming is cleaner. Add a one-line migration note in the spec keeper output.

Repository helpers:
- `mark_plan_built(plan_id, run_id)` — sets phase + built_at + built_run_id.
- `mark_plan_ran(plan_id, run_id)` — replaces `mark_plan_applied`. Keep both for one release if needed for backward compat, or just rename and update callers.
- `list_plans_by_lineage(plan_id)` — walks `parent_plan_id` upward and recursively gathers descendants.

Migration: M1-03 promised "no separate migration tool" until M2. The simplest path here is to write a small Alembic baseline now (since we're changing schema) and ship a `0001_baseline.py` + `0002_plan_phase.py` pair. Alternative: keep `Base.metadata.create_all()` and have the repository quietly add the columns via `ALTER TABLE` if missing, on first connect. The Alembic path is the right answer; introducing the migration framework here is a small extra cost that pays off the next time we change schema.

### Plan-agent system prompt

`src/carve/core/agents/prompts/m1_plan_agent.md`. Roughly:

```markdown
You are Carve's planning agent. Your job is to design a data pipeline before
any code is written.

You have these tools:
- `read_file` — inspect the user's project for existing pipelines or conventions.
- `run_snowflake_query` — inspect schemas, tables, and sample rows in Snowflake.
- `submit_plan(design)` — finalize your plan. Once you call this, the planning
  phase ends.

Design dimensions to cover before submitting:

- **Source.** What is being ingested, from where, in what format.
- **Destination.** Database / schema / table — by default these come from the
  Connection context above; override only if the user's goal explicitly says
  otherwise.
- **Transformation strategy.** How rows land (truncate-and-reload, MERGE upsert,
  incremental by date, etc.). State why.
- **Columns.** The schema you'll create.
- **Requirements.** Pip packages your script will need.
- **Estimates.** Approximate row count, runtime, and (where relevant) cost.
- **Tradeoffs.** Honest list of things you're choosing not to optimize.
- **Open questions.** Anything the user should clarify before you build.

If the goal is ambiguous (which connection target? which schema? which time
window?), ASK in `open_questions` rather than guess. The user will refine.

Do not write any pipeline files. Do not include "How to Run" instructions.
Code generation happens in a separate phase.
```

The `submit_plan` tool's `input_schema` matches the `design` field of the plan JSON.

### Refine flow

When `carve plan --refine <plan_id> "<feedback>"` is invoked:

1. Look up the parent plan.
2. Construct the agent's initial messages as: the parent plan's goal + design (as context), the user's `feedback`. The system prompt is unchanged.
3. Run the plan agent. It calls `submit_plan` with the refined design.
4. Persist the new plan with `parent_plan_id = <parent>`. Print both ids and the diff highlights ("destination changed from X to Y" — naive field-by-field diff is fine).

### Build flow

`carve build <plan_id>`:

1. Look up plan; refuse if `phase != "planned"` (unless `--force`).
2. Read the build agent's system prompt.
3. Construct initial message: "Build the pipeline described in this design. Plan id: <id>." plus the `design` JSON serialized into the system prompt.
4. Create a run row with `kind="build"`, `target_id=<plan_id>`. The build is itself a tracked run.
5. Run the build agent loop (with `read_file` and `write_file` tools only).
6. On success: snapshot what was written under `pipelines/`, attach the file list to the plan's `task_graph_json`, mark `phase="built"`. Mark the build run as `success`.
7. On failure: mark the build run as `failed`. Plan stays in `phase="planned"`.

### Run flow

`carve run <plan_id>`:

- Same as today's `apply_plan`, with a phase-precondition check at the top: `phase` must equal `"built"`.
- On success: `mark_plan_ran(plan_id, run_id)`. Phase becomes `"run"` (or stays as `"built"` if you prefer multiple runs of the same plan; my call is to allow multiple runs and use phase loosely — the run row is the source of truth for "did it execute").

### Removed file-snapshot logic in plan

`generate_plan` in `src/carve/cli/orchestrator/planner.py` no longer snapshots `pipelines/`, no longer scans for changed files, no longer parses `requirements.txt`. All of that moves into `build_plan` in a new `src/carve/cli/orchestrator/builder.py`. The snapshot pattern stays the same — just lives in `builder.py` now.

## Tests

Heavier than the other M1.1 specs. Roughly:

- `tests/cli/orchestrator/test_planner.py` — rewritten:
  - Plan agent emits a `submit_plan(design)` call → orchestrator persists a Plan with phase="planned", JSON file with the design, no files under `pipelines/`.
  - Refine path: parent plan exists, agent emits a refined `submit_plan`, new plan row has the right `parent_plan_id`.
  - Plan agent emits text-only (no `submit_plan` call) → orchestrator surfaces a clear error.
- `tests/cli/orchestrator/test_builder.py` — new:
  - Build agent writes `main.py` and `requirements.txt` → orchestrator marks plan built, attaches file list to plan row.
  - Build agent fails to write `main.py` → orchestrator surfaces error, plan stays "planned".
  - Build idempotency: rebuilding a built plan without `--force` errors.
- `tests/cli/orchestrator/test_runner.py` — refactored from today's `test_applier.py`:
  - Run a `built` plan → executes, marks plan ran, returns success.
  - Run a `planned` plan → refuses with "build first" error, exit 2.
- `tests/test_cli.py` — `test_carve_apply_prints_m2_placeholder`.
- `tests/core/state/test_repository.py` — new tests for `mark_plan_built`, `list_plans_by_lineage`, the phase-CHECK constraint.
- `tests/test_cli_lineage.py` (or extend an existing test file) — `carve plans <id>` renders parent chain.

The plan/build agents both get mocked in unit tests; the integration tests for M1 stay as a single end-to-end gated test that exercises plan → build → run.

## Acceptance criteria

- `carve plan "<goal>"` returns a design summary and a plan id, with no files under `pipelines/`.
- `carve plan --refine <id> "<feedback>"` produces a new plan with `parent_plan_id` set, and prints what changed.
- `carve build <plan_id>` writes the pipeline files and marks the plan built. Costs noticeably less than today's plan because the build agent has a narrower context.
- `carve run <plan_id>` executes a built plan and refuses to run an unbuilt one.
- `carve apply <plan_id>` prints the M2-placeholder message and exits 0.
- `carve plans` and `carve plans <id>` render plan history.
- README walkthrough updated to show the new lifecycle. CHANGELOG entry under `## [Unreleased]`.
- `ruff` + `mypy --strict` + full `pytest` stay green.

## Files this spec produces

New:

- `src/carve/cli/orchestrator/builder.py` (the build phase)
- `src/carve/core/agents/prompts/m1_plan_agent.md`
- `src/carve/core/agents/prompts/m1_build_agent.md`
- `src/carve/cli/commands/build.py` (new typer command)
- `src/carve/cli/commands/plans.py` (new typer command for lineage listing)
- `tests/cli/orchestrator/test_builder.py`
- `migrations/0001_baseline.py` and `0002_plan_phase.py` (or equivalent if we go non-Alembic)

Modified:

- `src/carve/cli/commands/plan.py` (refine support, no more file snapshots)
- `src/carve/cli/commands/apply.py` (M2 placeholder text)
- `src/carve/cli/commands/run.py` (real implementation, replaces the stub)
- `src/carve/cli/orchestrator/planner.py` (plan-only behavior, design schema, refine path)
- `src/carve/cli/orchestrator/applier.py` → split into `runner.py` for the run flow; the M2 apply lives elsewhere when it lands
- `src/carve/cli/main.py` (register `build`, `plans` subcommands)
- `src/carve/core/state/models.py` (`phase`, `built_at`, `built_run_id`, rename apply→ran)
- `src/carve/core/state/repository.py` (new helpers)
- `src/carve/core/agents/prompts/m1_code_agent.md` — delete or repurpose; the file-writing role moves to `m1_build_agent.md`
- `tests/cli/orchestrator/test_planner.py` (rewritten)
- `tests/cli/orchestrator/test_runner.py` (renamed from `test_applier.py`)
- `tests/core/state/test_repository.py`
- `tests/test_cli.py`
- `README.md` (walkthrough)
- `CHANGELOG.md`

## What this enables

- The user-facing flow finally matches what people expect: design → review → build → test → ship. No more "plan that secretly writes files."
- Plan iteration is cheap. The user can say "use MERGE instead of TRUNCATE" without paying for a full code regeneration.
- The build agent has a narrower job and can be a smaller, faster, cheaper model in a future spec (Haiku for build? Sonnet for plan?).
- M2's `carve apply` slot is reserved with the right semantics: deploy this plan to prod via PR. The verbs read coherently: `plan` (design) → `build` (code) → `run` (dev) → `apply` (prod).
- Plan history (parent_plan_id chain) becomes visible via `carve plans <id>`, which is a foundation for the M2 web UI's plan-comparison view.
