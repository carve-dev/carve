# M2-08 — Schema retrieval

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M1-06 (Snowflake), M2-05 (manifest reader)

## Purpose

Build the layered retrieval system that gives agents access to schema context without blowing up the LLM context window. Four of the five layers ship in M2; the embedding layer is deferred to M3.

## The five layers (recap from ARCHITECTURE.md)

| Layer | Cost | Use case | M2 |
|---|---|---|---|
| 1. Catalog queries | Cheap, deterministic | Facts about tables/columns | ✓ |
| 2. dbt manifest queries | Cheap, deterministic | dbt-specific facts | ✓ |
| 3. File grep | Cheap, deterministic | Exact-match references | ✓ |
| 4. Lineage traversal | Cheap, deterministic | Impact analysis | ✓ |
| 5. Embedding search | Expensive, fuzzy | Concept-level lookups | M3 |

Each layer is exposed to agents as one or more skills. The agent picks a skill; it never picks a layer directly.

## Skills shipped in M2

### Catalog queries

- `list_databases()` → list of database names + metadata
- `list_schemas(database)` → schemas in a database
- `list_tables(database, schema)` → tables/views with rough sizes
- `describe_table(database, schema, table)` → columns with types
- `sample_table(database, schema, table, limit=10)` → first N rows
- `get_table_size(database, schema, table)` → row count + bytes
- `get_recent_queries(table, limit=10)` → recent queries against this table from QUERY_HISTORY

These all hit Snowflake's `INFORMATION_SCHEMA` or system tables. Cheap, deterministic, exact.

### dbt manifest queries

These wrap `M2-05`'s manifest reader as agent-callable skills:

- `dbt_lookup_model(name)` → full model metadata
- `dbt_downstream_of(model_name)` → list of dependent models
- `dbt_upstream_of(model_name)` → list of source dependencies
- `dbt_columns_of(model_name)` → declared columns
- `dbt_tests_on(model_name)` → tests on this model
- `dbt_models_in_path(path_glob)` → matching models
- `dbt_all_sources()` → all source definitions

### File grep

- `search_repo(pattern, path_glob=None)` → ripgrep over the project files
- `find_references(name)` → finds all `{{ ref('name') }}` and `{{ source('x', 'name') }}` instances

Implemented via `subprocess.run(["rg", ...])` — `ripgrep` must be installed. The `carve doctor` (M3) verifies. For users without ripgrep, fall back to Python `pathlib.glob` + content scan; slower but works.

### Lineage traversal

- `lineage_upstream(model, depth=3)` → upstream lineage tree
- `lineage_downstream(model, depth=3)` → downstream lineage tree
- `impact_of_change(model)` → all downstream models affected

Implemented as graph traversals over the manifest. Returns structured trees (Pydantic) that the agent can reason over.

## Skill registration

Each skill is a Python function decorated with `@skill`. M2 hardcodes built-in skills (the `@skill` SDK ships in M3, but the decorator pattern is the same):

```python
@skill(
    name="list_tables",
    description="List tables and views in a Snowflake schema with row counts and sizes.",
    inputs={
        "database": {"type": "string", "required": True},
        "schema": {"type": "string", "required": True},
        "include_views": {"type": "boolean", "default": True},
    },
    outputs={
        "tables": {"type": "array", "items": "TableSummary"}
    },
)
def list_tables(ctx: SkillContext, database: str, schema: str, include_views: bool = True):
    sf = ctx.snowflake_pool.get(ctx.target)
    sql = f"""
        SELECT
            table_name,
            table_type,
            row_count,
            bytes
        FROM {database}.information_schema.tables
        WHERE table_schema = '{schema.upper()}'
        {"" if include_views else "AND table_type = 'BASE TABLE'"}
        ORDER BY table_name
    """
    return {"tables": sf.query(sql)}
```

The decorator captures the function plus metadata; the agent loop reads this metadata to build tool schemas for the LLM.

## SkillContext

`src/carve/core/skills/context.py`:

```python
class SkillContext:
    def __init__(self, config, repo, run_id, target):
        self.config = config
        self.repo = repo
        self.run_id = run_id
        self.target = target
        self.snowflake_pool = SnowflakePool(config)
        self.dbt_manifest = DbtManifest(config.dbt.manifest_path)

    def log(self, message: str, level: str = "info"):
        self.repo.append_log(self.run_id, level, "skill", message)

    def emit_event(self, event: str, payload: dict):
        # M2 just logs; M3 uses the event bus
        self.log(f"event: {event} {payload}")
```

Every skill takes a `SkillContext` as its first positional argument, plus its declared inputs as keyword arguments.

## Result truncation

Skills that return potentially-large results truncate with a flag:

```python
@dataclass
class SkillResult:
    data: list | dict
    truncated: bool = False
    total_count: int | None = None  # if truncated, how many existed
    next_cursor: str | None = None  # if pagination is supported
```

Example: `sample_table(limit=10)` returns 10 rows always. `list_tables()` is capped at 200 results — if there are more, returns `truncated=True` and the count. The agent sees the truncation and can refine.

This prevents catastrophic context blowups (1,000-table schemas).

## Pre-scoping by the orchestrator

The orchestration agent (M2-02) calls skills to assemble context *before* invoking specialists. For example, given a goal "make stg_orders incremental":

1. Orchestrator calls `dbt_lookup_model("stg_orders")` → gets the model's path, materialization, columns
2. Orchestrator calls `dbt_downstream_of("stg_orders")` → gets list of dependent models
3. For each downstream model, orchestrator calls `dbt_lookup_model(name)` → gets their column references
4. Orchestrator calls `describe_table("RAW", "ORDERS")` → gets source schema
5. The pre-scoped context bundle (the model SQL, the schema yaml, the downstream models, the source schema) is passed to the dbt agent

The dbt agent then has everything it needs to do focused work, without re-discovering it.

## Caching

Skill calls within a single agent invocation are cached:

```python
class CachedSkillExecutor:
    def __init__(self, skills_registry):
        self.skills = skills_registry
        self.cache: dict[tuple, SkillResult] = {}

    def execute(self, name: str, kwargs: dict, ctx: SkillContext) -> SkillResult:
        key = (name, frozenset(kwargs.items()))
        if key not in self.cache:
            self.cache[key] = self.skills[name](ctx, **kwargs)
        return self.cache[key]
```

Cache scope = single agent invocation. New invocations get fresh caches (so changes between invocations are visible).

For some skills (queries to Snowflake), this cache is strict-read-only — the data could change between calls. M3 may add a TTL.

## Tests

- Each catalog query returns expected structure
- Each manifest query returns expected structure
- File grep handles missing ripgrep gracefully
- Lineage traversal handles cycles (returns the cycle, doesn't infinite-loop)
- Truncation flag is set correctly
- Cache hits avoid re-execution within an invocation

Use a fixture Snowflake state (mocked) and the fixture dbt project from earlier specs.

## Acceptance criteria

- Agents can call any of the M2 skills via the agent loop
- Pre-scoping by the orchestrator works end-to-end for typical goals
- Result truncation prevents context blowups
- Cache reduces redundant calls

## Files

- `src/carve/core/skills/__init__.py`
- `src/carve/core/skills/registry.py`
- `src/carve/core/skills/decorator.py`
- `src/carve/core/skills/context.py`
- `src/carve/core/skills/executor.py`
- `src/carve/core/skills/builtin/catalog.py`
- `src/carve/core/skills/builtin/manifest.py`
- `src/carve/core/skills/builtin/grep.py`
- `src/carve/core/skills/builtin/lineage.py`
- `tests/core/skills/test_catalog.py`
- `tests/core/skills/test_manifest.py`

## What this enables

- The orchestrator pre-scopes context for specialists effectively
- Specialists work on focused, relevant inputs
- The skills SDK (M3) inherits this same registry pattern
- Embedding search (M3) is just another layer plugged into the same executor
