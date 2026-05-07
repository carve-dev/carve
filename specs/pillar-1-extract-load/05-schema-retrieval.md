# P1-05 — Schema retrieval (catalog skills)

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 0.5 day
**Dependencies:** M1-06 (Snowflake connector), P1-01 (target system)
**Lineage:** Subset of **M2-09** ([`specs/_archive/milestone-2-real-product/09-schema-retrieval.md`](../_archive/milestone-2-real-product/09-schema-retrieval.md), 210 lines, not yet formally reviewed but content is current). Pillar 1 ships only the **catalog-query layer** (Layer 1 from M2-09's five-layer model). dbt manifest queries (Layer 2), file grep (Layer 3), and lineage traversal (Layer 4) move to **Pillar 2** alongside the dbt agent. Embedding-based search (Layer 5) is far-future. The skill registry, decorator, `SkillContext`, caching, and truncation infrastructure carry forward verbatim — Pillar 2's additional layers reuse it without further structural design.

## Purpose

Give the M1.1-06 plan agent (`m1_plan_agent.md`) a small set of cheap, deterministic catalog skills it can call to inspect the active target's existing schemas, tables, and columns at plan time. The agent's design choices — column types, primary keys, transformation strategy — get grounded in what's actually in Snowflake (or correctly tagged as "destination doesn't exist yet, will be created at deploy").

This spec also seeds the **skill registry infrastructure** (decorator + registry + `SkillContext` + caching + truncation) that Pillar 2 reuses for dbt manifest skills, file grep, and lineage traversal.

## Scope: catalog queries only

| Layer (per ARCHITECTURE.md / M2-09) | Cost | Use case | Ships in Pillar 1? |
|---|---|---|---|
| 1. Catalog queries | Cheap, deterministic | Facts about tables/columns | **✓** |
| 2. dbt manifest queries | Cheap, deterministic | dbt-specific facts | Pillar 2 |
| 3. File grep | Cheap, deterministic | Exact-match references | Pillar 2 |
| 4. Lineage traversal | Cheap, deterministic | Impact analysis | Pillar 2 |
| 5. Embedding search | Expensive, fuzzy | Concept-level lookups | Far-future |

## Catalog skills shipped in Pillar 1

All five hit Snowflake's `INFORMATION_SCHEMA` against the active target's runtime role. Cheap, deterministic, exact.

- `list_databases()` → list of database names + metadata (created_at, owner)
- `list_schemas(database)` → schemas in a database
- `list_tables(database, schema, include_views=True)` → tables/views with row counts and bytes
- `describe_table(database, schema, table)` → columns with types, nullability, ordinal position
- `table_exists(database, schema, table) -> bool` — convenience wrapper for the common "does this destination exist yet?" check

Two omitted from M2-09's catalog set, deferred:

- `sample_table(database, schema, table, limit=10)` — useful but the plan agent rarely needs it (the design comes from the user's prompt, not from sampling rows). Ship in Pillar 2 if the dbt agent wants it.
- `get_recent_queries(table, limit=10)` — relies on `QUERY_HISTORY` (account-level access required). Defer.
- `get_table_size(database, schema, table)` — already covered by `list_tables` returning size info. No separate skill.

Authoritative source of which Snowflake target each query hits: the active target resolved by P1-01 (`--target` flag → `CARVE_TARGET` env → `default_target` → fallback). The skill executor passes `ctx.target` into the connection lookup so different targets in different commands can't bleed into each other.

## Skill registration

Each skill is a Python function decorated with `@skill`. The decorator captures the function plus metadata; the agent loop reads this metadata to build tool schemas for the LLM.

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
        "tables": {"type": "array", "items": "TableSummary"},
    },
)
def list_tables(ctx: SkillContext, database: str, schema: str, include_views: bool = True):
    sf = ctx.snowflake_pool.get(ctx.target)  # connects with [snowflake.<target>] runtime role
    sql = f"""
        SELECT table_name, table_type, row_count, bytes
        FROM {database}.information_schema.tables
        WHERE table_schema = %(schema)s
        {"" if include_views else "AND table_type = 'BASE TABLE'"}
        ORDER BY table_name
    """
    rows = sf.query(sql, {"schema": schema.upper()})
    return SkillResult(data={"tables": rows}, truncated=len(rows) >= 200, total_count=len(rows))
```

`@skill` lives in `src/carve/core/skills/decorator.py`. The registry (`src/carve/core/skills/registry.py`) collects all decorated functions at import time and exposes a lookup keyed on the skill name.

## `SkillContext`

```python
class SkillContext:
    def __init__(self, config: Config, repo: Repository, run_id: str, target: str):
        self.config = config
        self.repo = repo
        self.run_id = run_id
        self.target = target
        self.snowflake_pool = SnowflakePool(config)

    def log(self, message: str, level: str = "info") -> None:
        self.repo.append_log(self.run_id, level, "skill", message)

    def emit_event(self, event: str, payload: dict) -> None:
        # Pillar 1 just logs; Pillar 4's monitoring layer adds an event bus
        self.log(f"event: {event} {payload}")
```

Every skill takes a `SkillContext` as its first positional argument plus its declared inputs as keyword arguments. The `target` field is set by the agent loop based on `--target`/`default_target` resolution (P1-01) and used by `snowflake_pool.get(...)` to connect to the right account.

## Result truncation

Skills that return potentially-large results truncate with a flag:

```python
@dataclass
class SkillResult:
    data: list | dict
    truncated: bool = False
    total_count: int | None = None  # if truncated, how many existed
    next_cursor: str | None = None  # if pagination is supported (Pillar 2+)
```

Caps that ship in Pillar 1:

- `list_tables()` capped at 200 rows
- `list_schemas()` capped at 100 rows
- `list_databases()` typically returns < 10; uncapped

Truncation prevents catastrophic context blowups on accounts with thousands of tables. The agent sees `truncated=True` and can refine the call (filter to a specific schema, etc.).

## Caching within an agent invocation

Skill calls within a single agent invocation are cached by `(name, frozen kwargs)`:

```python
class CachedSkillExecutor:
    def __init__(self, skills_registry: SkillRegistry):
        self.skills = skills_registry
        self.cache: dict[tuple, SkillResult] = {}

    def execute(self, name: str, kwargs: dict, ctx: SkillContext) -> SkillResult:
        key = (name, frozenset(kwargs.items()))
        if key not in self.cache:
            self.cache[key] = self.skills[name](ctx, **kwargs)
        return self.cache[key]
```

Cache scope = single agent invocation. New invocations get fresh caches (so changes between invocations are visible). Warehouse query cost is real; the cache is mandatory for non-trivial agent runs that revisit the same table multiple times.

For Pillar 1, no TTL on the cache — Snowflake catalog data doesn't change mid-invocation in normal use. If a user runs DDL against the target between two skill calls in the same invocation (rare), they get stale results until the next invocation. Acceptable.

## Plan agent integration

P1-02's plan flow exposes catalog skills as tools to the M1.1-06 `m1_plan_agent`. The plan agent's tool set in Pillar 1:

1. `read_file(path)` — already from M1.1-06
2. `run_snowflake_query(sql)` — already from M1.1-06; raw escape hatch for queries the catalog skills don't cover
3. **`list_databases`, `list_schemas`, `list_tables`, `describe_table`, `table_exists`** — added by this spec
4. `submit_plan(design)` — terminator from M1.1-06

The decorator-driven registry handles tool schema generation: each `@skill`-decorated function becomes a tool with its declared `inputs` schema. The plan agent doesn't see the registry directly — it sees tools.

The build-time extract-load agent (P1-04) does **not** use these skills as separate tools. It has `run_snowflake_query` for occasional verification (e.g. "does this column already have the type I expect?"). Pillar 2's dbt agent will use the catalog skills more heavily.

## Implementation

### File-level changes

New files:

- `src/carve/core/skills/__init__.py` — package init; re-exports the registry, decorator, `SkillContext`, `SkillResult`.
- `src/carve/core/skills/decorator.py` — `@skill` decorator implementation.
- `src/carve/core/skills/registry.py` — `SkillRegistry` (collects decorated functions, indexes by name, exposes tool-schema generation for the agent loop).
- `src/carve/core/skills/context.py` — `SkillContext` class.
- `src/carve/core/skills/executor.py` — `CachedSkillExecutor`.
- `src/carve/core/skills/result.py` — `SkillResult` dataclass.
- `src/carve/core/skills/builtin/__init__.py`
- `src/carve/core/skills/builtin/catalog.py` — the five catalog skills above.
- `tests/core/skills/test_registry.py` — registration + lookup + tool-schema generation.
- `tests/core/skills/test_decorator.py`
- `tests/core/skills/test_executor.py` — cache behavior, target wiring.
- `tests/core/skills/test_catalog.py` — each catalog skill against a fixture/mocked Snowflake.

Modified files:

- `src/carve/core/agents/loop.py` — accept a `SkillRegistry` so skill calls land in the loop's tool-call dispatch alongside the agent's own tools.
- `src/carve/cli/orchestrator/planner.py` — wire the catalog skills into the plan agent's tool set (P1-02 already touches this file; this spec adds the skill registration call).
- `src/carve/core/agents/m1_tools.py` — no change to existing tools; just imports the skill registry for tool-schema generation.

### Connection pool

`SnowflakePool` (in `src/carve/core/snowflake/pool.py` from M1-06) gains a `get(target_name)` method that:

1. Looks up `[snowflake.<target_name>]` from `carve/connections.toml` (centralized, P1-01).
2. Resolves `${<TARGET>_*}` env var references against the loaded `.env` (M1.1-03 autoload).
3. Connects with the resolved credentials + `role=` explicitly set.
4. Returns a `SnowflakeConnection` wrapper that exposes `.query(sql, params)`.

Connection pooling within an invocation is `dict[str, SnowflakeConnection]` keyed by target. Reuse keeps catalog calls cheap (no per-call connect overhead).

## Tests

- `test_skill_decorator_captures_metadata` — `@skill(...)` attaches the metadata accessibly.
- `test_registry_indexes_by_name` — multiple skills register; lookup-by-name returns the right one.
- `test_registry_generates_tool_schemas` — `inputs` declarations turn into Anthropic tool schemas correctly.
- `test_executor_caches_within_invocation` — second call with same args hits cache; new executor instance does not.
- `test_executor_does_not_cache_across_invocations` — different `SkillContext` instances get fresh caches.
- `test_list_databases` — fixture: 3 databases; skill returns all three with correct shape.
- `test_list_tables_truncates_at_200` — fixture has 250 tables; skill returns 200 with `truncated=True, total_count=250`.
- `test_list_tables_excludes_views_when_requested` — `include_views=False` filters correctly.
- `test_describe_table_returns_typed_columns` — column rows include name, type, nullability, ordinal.
- `test_table_exists_true_false_paths` — both branches.
- `test_skill_uses_active_target` — calling a skill with `ctx.target = "prod"` connects via `[snowflake.prod]`, not `[snowflake.dev]`.
- `test_plan_agent_can_call_catalog_skill` — integration: plan agent invocation that uses `describe_table` produces a design that references the actual columns.

Use a fixture Snowflake (mocked `INFORMATION_SCHEMA` results) and the standard agent-loop test harness.

## Acceptance criteria

- The five catalog skills are callable by the plan agent via the agent loop's tool-call mechanism.
- Each skill connects to Snowflake with the active target's runtime role (`[snowflake.<active>]` from centralized `carve/connections.toml`).
- `SkillResult.truncated` is set correctly when caps are hit.
- `CachedSkillExecutor` reduces redundant calls within a single agent invocation; cache resets across invocations.
- Plan agent can ground its design in real schema data (regression: a goal that says "ingest the foo API into RAW.FOO" produces a design whose columns match the existing RAW.FOO table when one exists, or correctly tags the destination as new when it doesn't).
- `ruff` + `mypy --strict` + `pytest` stay green; new tests cover registry, executor, each catalog skill, and plan-agent integration.

## Files this spec produces

(Summary of File-level changes section.)

New: 7 source files (skills package skeleton + 5 catalog skills + result/decorator/registry/context/executor + builtin), 4 test files.
Modified: `agents/loop.py`, `cli/orchestrator/planner.py`, `agents/m1_tools.py` (import only).
No DB migrations.

## Out of scope

- dbt manifest queries (Pillar 2; M2-06's manifest reader plus M2-09's manifest layer carry forward).
- File grep (Pillar 2 alongside dbt).
- Lineage traversal (Pillar 2).
- Embedding-based fuzzy lookup (far future).
- The skills SDK that lets users author their own skills outside the codebase (M3).
- TTL caching (defer; Pillar 1's per-invocation cache is enough).
- Account-level Snowflake skills (`USAGE` views, query history, `account_usage` schema). Defer to a later pillar that needs them.
- Sampling rows (`sample_table`). Plan agent rarely needs it; defer.
- A user-facing CLI for catalog inspection (`carve target inspect <db>` etc.). Plan agent and `carve target show` cover the v0.1 needs.

## What this enables

- The plan agent grounds its designs in real schema data instead of guessing column types from the user's prompt.
- Pillar 2's additional skill layers (dbt manifest, file grep, lineage) reuse the registry, decorator, executor, and `SkillContext` shipped here without further structural work.
- The Pillar 1 happy path stays cheap — catalog queries are deterministic and fast, no LLM cost.
- Truncation prevents context blowups on accounts with thousands of tables, keeping Pillar 1 viable on large Snowflake accounts.
