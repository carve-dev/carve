# P1-05 — Schema retrieval (catalog skills)

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 0.5 day
**Dependencies:** M1-06 (Snowflake connector), P1-01 (target system)
**Lineage:** Subset of **M2-09** ([`specs/milestone-2-real-product/09-schema-retrieval.md`](../milestone-2-real-product/09-schema-retrieval.md), not yet formally reviewed but content is current). Pillar 1 ships only the catalog-query layer (Layer 1 from the M2-09 spec). dbt manifest queries (Layer 2), file grep (Layer 3), and lineage traversal (Layer 4) move to **Pillar 2** alongside the dbt agent. Embedding-based search (Layer 5) is far-future.
**Status:** Stub. Full spec to be drafted.

## Purpose

Provide the catalog-query skills the extract-load agent needs at plan time to inspect the active target's existing schemas, tables, and columns — so the AI's generated DDL and load logic match what's actually in Snowflake (or correctly assumes "this destination doesn't exist yet").

## What this introduces

- **Catalog skill** exposing functions agents can call:
  - `list_databases()`
  - `list_schemas(database)`
  - `list_tables(database, schema)`
  - `describe_table(database, schema, table)` — columns + types + nullability
  - `table_exists(database, schema, table) -> bool`
- **Connection scope.** All queries run against the active target's runtime role (read-only by default; the deploy role is reserved for deploy-time DDL).
- **Result caching** within a single agent run (avoid re-querying the same `INFORMATION_SCHEMA` view multiple times in one plan).

## Out of scope

- dbt manifest queries (Pillar 2)
- File grep / lineage traversal (Pillar 2 / Pillar 3)
- Embedding-based search (far future)
- Cost-aware skill selection (defer)
