# Semantic search: embedding-based retrieval over the project

> **Fuzzy, concept-level retrieval** — "where are our customer-churn metrics?" — that exact catalog/manifest/grep lookups can't answer. An embedding index over model descriptions, column comments, and pipeline/source docstrings, surfaced as a `semantic_search` skill that returns ranked entity pointers. It's the fifth retrieval layer (ARCHITECTURE §6.1), complementing the deterministic layers the [explorer](./ask.md) already uses. The deterministic retrieval (catalog + manifest + dlt schema + grep + investigation) is the base; semantic search is the fuzzy layer on top. Sequenced with the investigation surface ([DELIVERY](../DELIVERY.md) Increment 5).

## Status

- **Status:** Drafting (durable design; sequenced with the investigation surface in DELIVERY)
- **Depends on:** [ask](./ask.md) (the explorer that would call the skill), [extensibility](./extensibility.md) (it ships as a built-in callable skill), [lineage](./lineage.md) / [sql](./sql.md) (the dbt manifest + warehouse catalog it indexes), [state-store](./state-store.md) (where the index/embeddings persist).
- **Used by:** [ask](./ask.md) (the explorer's fuzzy-lookup layer), potentially the [dbt-engineer](./dbt-engineer.md) (finding related models by concept).
- **Lineage:** net-new. The "embedding search" retrieval layer referenced across five specs (ask, lineage, memory, extensibility, reference) but owned by none; this gives it a home.

## Goal

Answer fuzzy, semantic questions over the project's metadata — match intent ("churn", "revenue recognition", "PII columns") to the models/columns/sources that embody it — returning ranked **pointers** (entity refs + similarity), never full content. Bounded like every other retrieval skill (§6.4).

## Out of scope

- **The deterministic retrieval layers** — exact catalog (`INFORMATION_SCHEMA`), dbt manifest queries, dlt schema, grep — are [sql](./sql.md) / [lineage](./lineage.md) / built-in skills. Semantic search is the *fuzzy* layer on top, not a replacement.
- **Lineage** — "what depends on X" is investigation over dbt/dlt native lineage ([lineage](./lineage.md)), not embeddings.
- **A vector database product** — Carve indexes its own project metadata for retrieval; it is not a general vector store.

## Behavior

### The index

An embedding index over: dbt model descriptions + column comments (from the manifest), dlt source/resource docstrings, and pipeline/memory docs. Persisted (in the [state store](./state-store.md) or a local index file), rebuilt on demand. Embeddings via a configurable provider (the model provider or a local embedding model — a config choice).

### The `semantic_search` skill

A built-in callable skill ([extensibility](./extensibility.md)): `semantic_search(query, top_k) → [{entity_ref, score, snippet}]`. A **search**-category skill per §6.4 — top-N by relevance (top-N is the feature, not truncation), returns pointers + similarity + a count of matches not returned. The [explorer](./ask.md) calls it when a question is conceptual rather than exact, then follows the pointers with deterministic skills.

### `carve embeddings rebuild`

The index is rebuilt explicitly (`carve embeddings rebuild`) — it's not auto-invalidated on every change (cost), so the user/agent refreshes it when the catalog has moved materially (ARCHITECTURE §6.3 lists it as manual-invalidation). Incremental re-embedding of changed entities is an optimization.

## Tests

- **Unit (skill):** `semantic_search("customer churn", top_k=5)` returns ranked entity pointers + scores + a not-returned count over a fixture index; never returns full content.
- **Integration (rebuild):** `carve embeddings rebuild` indexes a fixture project's manifest + docstrings; a subsequent search finds a conceptually-related model that grep/catalog miss.
- **Unit (bounded):** results are top-N (search category), not truncated structural data.

## Acceptance

- A `semantic_search` skill answers concept-level queries with ranked entity pointers, bounded per §6.4, available to the explorer.
- The index is built/refreshed via `carve embeddings rebuild` over model/column/source/pipeline metadata.
- Documented as durable design, sequenced with the investigation surface in DELIVERY — no longer a scattered reference owned by none.

## Design notes

- **Why its own (thin) capability vs. an `ask` annotation?** It introduces a real subsystem — an embedding index/store, an embedding-provider config, and a rebuild command — beyond the explorer's deterministic skill calls. That real subsystem earns its own home rather than living as five scattered "embedding search" mentions owned by none.
- **Why manual rebuild.** Auto-reindexing on every catalog change is expensive and rarely worth it; explicit rebuild (or incremental) matches how embedding indexes are kept fresh in practice.

## Open questions

- **Embedding provider.** The model provider's embeddings vs. a bundled local embedding model (cost/offline/privacy trade-off) — a config + dependency decision.
- **Index location.** Postgres (pgvector) vs. a local index file vs. (hosted) a shared index — interacts with the OSS-vs-hosted seam.
- **Phasing.** Settled — the investigation surface ([DELIVERY](../DELIVERY.md) Increment 5).
