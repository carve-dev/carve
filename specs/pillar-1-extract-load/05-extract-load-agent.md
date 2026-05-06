# P1-05 — Extract-load agent

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1 day
**Dependencies:** M1-04 (agent loop), M1-06 (Snowflake connector), P1-01 (target system), P1-02 (plan/build lifecycle), P1-06 (schema retrieval)
**Lineage:** Carries content from **accepted M2-03** ([`specs/milestone-2-real-product/03-extract-load-agent.md`](../milestone-2-real-product/03-extract-load-agent.md)) almost verbatim. The system prompt structure, tool set, both skills (`data_engineering.md`, `snowflake_destination.md`), the hard rules from **M1.1-05** (no `os.environ.get` defaults; pass `role=` explicitly; idempotency), and the regression test for the Iowa-liquor `dict`-binding bug all carry forward. The only delta is the output path: writes to `targets/<active_target>/el/<artifact_name>/` instead of `pipelines/<name>/`. `m1_plan_agent.md`'s contract is unchanged.
**Status:** Stub. Full spec to be drafted.

## Purpose

The AI specialist that authors Python extract-and-load scripts. Given a plan/design, writes `targets/<active>/el/<name>/main.py` and `requirements.txt`. Uses two skills loaded on demand: a universal data-engineering skill (pagination, retries, idempotent writes, type coercion) and a Snowflake destination skill.

## What this introduces

- **`src/carve/core/agents/extract_load/`** — agent module + tools + system prompt (`extract_load_agent.md`).
- **Tools:** `read_file`, `write_file` scoped to `targets/<active>/el/<artifact_name>/`, `lookup_skill`, `run_snowflake_query` (read-only against the active target), `submit_step` (terminator).
- **`src/carve/skills/data_engineering.md`** — universal skill: pagination patterns, retry/backoff, watermarks, idempotent writes (MERGE / DELETE+INSERT / append), JSON-ish type coercion, structured logging, env-var connection wiring.
- **`src/carve/skills/snowflake_destination.md`** — Snowflake-specific: `executemany`, `write_pandas`, `COPY INTO`, MERGE upsert, role/warehouse propagation.
- **Hard rules** (carry from M1.1-05): no `os.environ.get(..., default)` patterns; pass `role=` explicitly; no "How to Run" section; idempotent re-runs.
- **Output path** writes to `targets/<active_target>/el/<artifact_name>/` (changed from M2-03's `pipelines/<name>/`).

## Out of scope

- Multi-step pipelines (Pillar 3)
- Quality / test generation (Pillar 2 split or M3 quality agent)
- Sources beyond HTTP/REST, Socrata, S3/GCS, files, paginated DBs
- Destinations beyond Snowflake (M4 or community)
- Streaming sources (Kafka, Kinesis) — indefinite defer
