# M3-02 — SQL step type

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 0.5 day
**Dependencies:** M1-06 (Snowflake connector), M3-01 (multi-step pipelines)

## Purpose

A `sql` step type for running ad-hoc SQL against Snowflake without going through dbt. Useful for: stored-procedure calls, table maintenance (`ALTER`, `OPTIMIZE`), one-off data fixes, MERGE statements that don't fit dbt's model abstraction, and Snowflake-managed feature operations (refresh dynamic tables, run tasks).

## Schema

```toml
[[steps]]
id = "refresh_dt"
type = "sql"
target = "prod"  # which connection to use
sql = """
ALTER DYNAMIC TABLE analytics.live_metrics REFRESH;
"""
on_failure = "fail"
timeout_seconds = 600

# Or load from a file:
[[steps]]
id = "monthly_archive"
type = "sql"
target = "prod"
sql_file = "snowflake/operations/archive_old_orders.sql"
```

Either `sql` (inline) or `sql_file` (reference); not both.

## Multiple statements

A SQL file can contain multiple statements separated by `;`. Each is executed in sequence. The step succeeds only if all statements succeed. Output of the *last* statement (rowcount or first 100 rows) is captured as the step's primary output.

## SQL parameter binding

Bind parameters via Jinja:

```toml
sql = """
SET (start_date, end_date) = (DATEADD(day, -30, CURRENT_DATE()), CURRENT_DATE());

DELETE FROM analytics.events
WHERE created_at < $start_date AND processed = TRUE;
"""
```

Or, more cleanly, accept parameters from previous steps' outputs:

```toml
sql = """
INSERT INTO audit_log (run_id, row_count)
VALUES ('{{ run.id }}', {{ steps.extract.outputs.row_count }});
"""
```

## SqlRunner

`src/carve/core/runners/sql.py`:

```python
class SqlRunner:
    def __init__(self, config, repo):
        self.config = config
        self.repo = repo
        self.pool = SnowflakePool(config)

    def execute(self, step: SqlStep, context: RunContext) -> RunHandle:
        run_id = context.run_id

        # Resolve SQL (inline or file)
        sql = self._resolve_sql(step, context)

        # Render Jinja
        sql = render_template(sql, context)

        # Spawn a thread to run (so we can return a handle and stream)
        thread = threading.Thread(
            target=self._run,
            args=(run_id, step, sql, context),
            daemon=True,
        )
        thread.start()

        return RunHandle(run_id=run_id, process_id=0)  # no PID; thread-based

    def _run(self, run_id: str, step: SqlStep, sql: str, context: RunContext):
        try:
            sf = self.pool.get(step.target)
            statements = self._split_statements(sql)
            for i, stmt in enumerate(statements):
                self.repo.append_log(run_id, "info", "runner",
                    f"Executing statement {i+1}/{len(statements)}")
                self.repo.append_log(run_id, "debug", "runner", stmt)
                if self._is_select(stmt):
                    rows = sf.query(stmt, limit=100)
                    last_output = {"rows": rows, "row_count": len(rows)}
                else:
                    affected = sf.execute(stmt)
                    last_output = {"rows_affected": affected}
            self._set_outputs(run_id, step.id, last_output)
            self.repo.update_step_status(run_id, step.id, "success")
        except Exception as e:
            self.repo.update_step_status(run_id, step.id, "failed", error=str(e))
```

### Statement splitting

Splitting on `;` is naive — it breaks for `;` inside strings or comments. Use `sqlglot` to parse and split:

```python
def _split_statements(self, sql: str) -> list[str]:
    return [stmt.sql() for stmt in sqlglot.parse(sql, dialect="snowflake")]
```

`sqlglot` becomes a Carve dependency. It's already useful for the M3 SQL parsing use cases.

## Read-only safety

The `sql` step type allows write statements (it's the whole point — `MERGE`, `INSERT`, `ALTER`). Unlike the agent's `run_snowflake_query` tool which is read-only.

To avoid accidents:

- Steps with mutating SQL must be reviewed in plan/apply (the SQL is part of the file diff)
- Pipelines that include `sql` steps with mutations are flagged in the plan summary: "Pipeline includes 2 SQL steps with INSERT/MERGE/DELETE statements"

## Long-running queries

A SQL step might run for an hour (large MERGE, table rebuild). The `timeout_seconds` config is the kill switch. Default is 1800s (30 min); configurable per step.

While waiting, the runner emits periodic "still running" log lines so the UI doesn't appear frozen.

## Tests

- Single-statement SELECT returns rows in outputs
- Multi-statement script runs all statements
- Failure on statement N stops execution
- Statement splitting handles strings and comments correctly
- Jinja rendering uses upstream step outputs
- Timeout cancellation works

## Acceptance criteria

- SQL steps can run inline or from file
- Multi-statement scripts work
- Output of the last statement is exposed to downstream steps
- Long-running queries can be cancelled

## Files

- `src/carve/core/steps/sql.py`
- `src/carve/core/runners/sql.py`
- `tests/core/runners/test_sql.py`

## What this enables

- Carve handles the long tail of "we just need to run this SQL" use cases
- Stored procedure invocations fit naturally into pipelines
- Dynamic table refreshes and Snowflake task management can be orchestrated
