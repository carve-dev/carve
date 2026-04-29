# M3-08 — Embedding-based schema search

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 1.5 days
**Dependencies:** M2-08 (skills), M2-05 (manifest reader)

## Purpose

Add the fifth schema retrieval layer: semantic search over dbt model docs, source descriptions, column comments, and SQL bodies. Returns *pointers* (model names + relevance scores), not full content. The agent then loads the pointed-to artifacts via the existing manifest queries.

This handles the "fuzzy concept" case: "find models related to customer churn" → returns `mart_customer_churn`, `int_customer_lifetime_value`, `dim_customer_segments` even though none of those names contain "churn."

## Why pointers, not content

Three reasons:

1. **Token efficiency** — pointers are 10x smaller than the underlying SQL/docs
2. **Determinism** — once the agent knows the pointers, exact retrieval gives the source of truth
3. **Up-to-date** — the manifest is always current; embeddings can lag

## Storage

**ChromaDB** as the local vector store. Reasons:

- Embedded mode (no server to run; just a SQLite-backed file)
- Active maintenance, good Python API
- Reasonable performance for our scale (10K-100K models max)
- Simple to swap out if needed

The embedding store lives at `.carve/embeddings/`. Initialized on first use.

## What gets embedded

For each artifact, generate an embedding for:

- **Models**: `<name>: <description>\n<columns with descriptions>\n<key SQL phrases>`
- **Sources**: `<source_name>.<table_name>: <description>\n<columns with descriptions>`
- **Column docs**: `<model>.<column>: <description>` (only if the column has a description)
- **Doc blocks**: full text of `{% docs %}` blocks

Each embedding is associated with metadata: artifact type, name, path, last_updated.

## Embedding model choice

For local-first: `all-MiniLM-L6-v2` from `sentence-transformers`. Reasons:

- Small (~80MB), runs on CPU
- Quality is solid for technical text
- No API key required
- Permissive license

Alternative: OpenAI's `text-embedding-3-small` for users who want better quality and don't mind the dependency. Make this configurable:

```toml
[embeddings]
provider = "local"  # or "openai"
local_model = "sentence-transformers/all-MiniLM-L6-v2"
# openai_model = "text-embedding-3-small"
# api_key = "${OPENAI_API_KEY}"
```

Default is local. OpenAI is a one-line config change.

## Indexing

`src/carve/core/embeddings/indexer.py`:

```python
class EmbeddingIndexer:
    def __init__(self, config, manifest):
        self.config = config
        self.manifest = manifest
        self.client = chromadb.PersistentClient(path=str(config.embeddings_path))
        self.collection = self.client.get_or_create_collection("artifacts")
        self.model = self._load_model(config)

    def index_all(self):
        """Full reindex. Run on dbt-build or scheduled refresh."""
        documents = []
        metadatas = []
        ids = []

        for model in self.manifest.all_models():
            text = self._model_text(model)
            documents.append(text)
            metadatas.append({"type": "model", "name": model.name, "path": model.path})
            ids.append(f"model:{model.unique_id}")

        for source in self.manifest.all_sources():
            text = self._source_text(source)
            documents.append(text)
            metadatas.append({"type": "source", "name": f"{source.source_name}.{source.name}"})
            ids.append(f"source:{source.unique_id}")

        # ... columns, doc blocks

        embeddings = self.model.encode(documents).tolist()
        self.collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
```

Full indexing takes seconds for typical projects (~50ms per artifact on CPU). Incremental indexing skips artifacts whose `last_updated` matches the cached value.

## When indexing runs

- On `carve init` (brownfield) — initial index after manifest is parsed
- On `carve dbt build` — automatic incremental refresh after build
- On `carve embeddings refresh` — manual full refresh
- On scheduled refresh — daily by default (configurable)

The CLI runs indexing in the foreground for `init` and `refresh`, in the background for the post-`dbt-build` case.

## Search skill

Exposed as a skill the agent can call:

```python
@skill(
    name="semantic_search_schema",
    description="Find dbt models, sources, or columns by semantic similarity to a description. Returns pointers (names) ranked by relevance, not full content.",
    tags=["retrieval", "schema"],
)
def semantic_search_schema(
    ctx: SkillContext,
    query: str,
    limit: int = 10,
    artifact_types: list[str] = None,  # ["model", "source", "column"]
):
    indexer = ctx.get_embedding_indexer()
    results = indexer.search(query, limit=limit, artifact_types=artifact_types)
    return {
        "results": [
            {
                "type": r.metadata["type"],
                "name": r.metadata["name"],
                "path": r.metadata.get("path"),
                "score": r.distance,
                "snippet": r.document[:200],
            }
            for r in results
        ]
    }
```

The agent uses the result to decide which artifacts to load fully:

```
agent:  semantic_search_schema(query="customer churn metrics", limit=5)
result: [
  {"type": "model", "name": "mart_customer_churn", "score": 0.42},
  {"type": "model", "name": "int_customer_lifetime_value", "score": 0.51},
  ...
]

agent:  dbt_lookup_model(name="mart_customer_churn")
result: <full model definition>
```

## Result quality

A few patterns improve results:

- **Boost named matches**: if the query exactly matches a model name, it's the top result regardless of embedding distance
- **Filter by type**: agents often know what they want ("find a source matching X" filters to sources only)
- **Combine with grep**: for queries that look like exact names, run grep first; semantic search as backup

## Failure modes

If embeddings are not available (model not installed, indexing failed), the skill returns:

```python
{"error": "Embedding search is not available. Run `carve embeddings refresh` to enable."}
```

The agent can fall back to manifest queries and grep. Never hard-fails the run; degrades gracefully.

## CLI

- `carve embeddings refresh` — full reindex
- `carve embeddings stats` — count of indexed artifacts, last refresh time
- `carve embeddings search "<query>"` — manual search for debugging

## Tests

- Indexing produces vectors for all artifacts
- Semantic search returns relevant results (with a fixture project)
- Exact name match boosts to top
- Type filtering works
- Missing embeddings degrade to a clear error, not crash

Use a fixture dbt project with deliberately diverse documentation.

## Acceptance criteria

- Embeddings index a typical dbt project in under 1 minute on CPU
- Search returns relevant artifacts for semantic queries
- Storage stays under 300MB for typical projects
- Failure modes don't crash agents

## Files

- `src/carve/core/embeddings/__init__.py`
- `src/carve/core/embeddings/indexer.py`
- `src/carve/core/embeddings/store.py`
- `src/carve/core/embeddings/model.py`
- `src/carve/core/skills/builtin/semantic_search.py`
- `src/carve/cli/commands/embeddings.py`
- `tests/core/embeddings/test_indexer.py`

## What this enables

- "Find models related to X" goals work without exact name knowledge
- Agents can navigate large warehouses confidently
- The five-layer retrieval architecture is complete
