# v0.1-19 — Lineage: the Carve-owned asset graph + query skills

> Per [`../ARCHITECTURE.md`](../ARCHITECTURE.md) §6.2: **lineage is the one Carve-owned piece of retrieval.** A table/relation-grain asset graph — dlt source/resource → warehouse table → dbt source → dbt model — persisted in `lineage_nodes`/`lineage_edges`, rebuilt on build / dbt-manifest-change / project-sync, and queried by bounded BFS. This spec implements that graph plus the `upstream_of` / `downstream_of` / `impact_of_change` skills the **explorer** (spec 12) needs to answer "where does this data come from" and "what breaks if I change this" — resolving the spec-16 *lineage graph owner* flag. It is a premier payoff of the **orchestration-only / brownfield** adoption path ([`../_strategy/2026-06-control-plane.md`](../_strategy/2026-06-control-plane.md)): a team that brings an existing dbt project gets impact analysis on day one. **Column-level lineage and `sql`-step lineage are deferred to v0.2; the static-UI lineage view stays deferred (spec 11).**

## Status

- **Status:** Drafting
- **Depends on:** [v0.1-01 state-store](./01-state-store-postgres.md) (the repository + migration conventions the graph tables follow), [v0.1-03 flat-layout](./03-flat-layout.md) (`provenance.py` header parsing + the component locator), [v0.1-04 el-agent-dlt](./04-el-agent-dlt.md) (a dlt run produces the stored dlt schema this reads for resource→table), [v0.1-08 multi-step-pipeline](./08-multi-step-pipeline.md) (the pipeline definition + component-by-name + the `carve build` trigger point), [v0.1-16 extensibility](./16-extensibility.md) (the `dbt_manifest` built-in skill this consumes + the `@skill` registration pattern for the new lineage skills), [v0.1-18 sql-layer](./18-sql-layer.md) (dialect-aware relation qualification; `sqlglot` for the deferred `sql`-step lineage). Anchored on ARCHITECTURE **§6.2** (node/edge model), **§6.3** (cache/freshness), **§6.4** (bounded results), **§9.6** (the tables).
- **Blocks:** [v0.1-12 ask-verb](./12-ask-verb.md) — the explorer's `lineage` classification + the `upstream_of`/`downstream_of`/`impact_of_change` skills (currently flagged "deferred"); the hosted cloud-UI lineage view (post-v0.1). Used opportunistically by [v0.1-17 recovery-engineer](./17-recovery-engineer.md) (impact context in a diagnosis).
- **Lineage:** net-new. No M1/M1.1 ancestor; this is the first spec to own ARCHITECTURE §6.2, which had no implementing spec (flagged in [v0.1-16](./16-extensibility.md) open questions).

## Goal

The Carve-owned **asset lineage graph** + the **query layer**, at table/relation grain:

1. **A persisted graph** (`lineage_nodes` / `lineage_edges`) modeling ARCHITECTURE §6.2's node and edge types (reconciled to **five node types, five edge types** — see *Graph model*).
2. **A builder** that (re)computes the graph from the producers available in v0.1: the **dlt stored schema + provenance header** (dlt:source/resource → warehouse:table), the **dbt manifest** (dbt:source/model + their dependency edges), and the **warehouse↔dbt-source stitch** (match on the schema-qualified relation name).
3. **A bounded BFS query layer** (depth-limited, node-capped) + the **skills** `upstream_of`, `downstream_of`, `impact_of_change` — *structural* skills under §6.4 (they never silently truncate; they raise `ResultTooLarge`).
4. **Refresh triggers**: on `carve build`, on dbt-manifest mtime change, and on project sync in separate-repo mode (ARCHITECTURE §6.3) — the graph is **data**, recomputed, never hand-edited.
5. **Explorer wiring**: flip spec 12's "lineage deferred" notes — the `lineage` classification becomes backed, and the three skills join the explorer's read-only grant.

## Out of scope (deferred)

- **Column-level lineage** (the `column_lineage` skill named in ARCHITECTURE §6.4). It needs `sqlglot` column resolution through model SQL — genuinely harder, and it lands with the deeper dbt/sql authoring in **v0.2**. v0.1 is table/relation grain only.
- **`sql`-step lineage.** Extracting table references from a `sql` step's SQL file (via `sqlglot`, spec 18) to add producer edges for tables a `sql` step writes. Deferred to **v0.2**: it needs FQN resolution against the connection's default database/schema, and the dlt+dbt graph already serves the premier brownfield demo. **Documented limitation:** a table produced solely by a `sql` step appears as a `warehouse:table` node (if a dbt source references it) but may have **no inbound producer edge** in v0.1; `upstream_of` on such a table returns empty with an `incomplete: "sql-step provenance not tracked in v0.1"` marker rather than implying it is a true source.
- **Static-UI lineage view** — already deferred (spec 11). The graph is captured + queryable via the skills / CLI / REST; it is not rendered in the v0.1 static HTML UI. The hosted cloud UI renders it post-v0.1.
- **Embedding / semantic search** (ARCHITECTURE §6.1 layer 5) — post-v0.1.
- **Incremental / scoped rebuild.** v0.1 does a transactional **full recompute-and-replace** (see *Refresh*). Per-component incremental diffing is a post-v0.1 optimization.

## Files this spec produces

```
src/carve/core/lineage/__init__.py
src/carve/core/lineage/graph.py              # NEW — Node/Edge dataclasses, the in-memory graph, bounded BFS (upstream/downstream, depth-limited, node-capped)
src/carve/core/lineage/builder.py            # NEW — orchestrates producers → (nodes, edges); the recompute-and-replace entry point
src/carve/core/lineage/producers/__init__.py
src/carve/core/lineage/producers/dlt.py      # NEW — dlt stored schema + provenance header → dlt:source/resource nodes + defines/produces edges
src/carve/core/lineage/producers/dbt.py      # NEW — dbt manifest (via the dbt_manifest skill) → dbt:source/model nodes + consumed_by edges
src/carve/core/lineage/stitch.py             # NEW — warehouse:table ↔ dbt:source matching on relation FQN → consumed_by edges
src/carve/core/lineage/fqn.py                # NEW — the per-kind identity/FQN canonicalization (the linchpin of the stitch + idempotent rebuild)
src/carve/core/state/models.py               # MODIFY — add LineageNode, LineageEdge ORM models
src/carve/core/state/repositories/lineage.py # NEW — LineageRepository: replace_graph(), get_node(), upstream(), downstream(), neighbors()
migrations/versions/00NN_lineage_graph.py    # NEW — lineage_nodes + lineage_edges. Set down_revision to the current Alembic head at build time.
src/carve/core/skills/builtin/lineage.py     # NEW — @skill upstream_of / downstream_of / impact_of_change (registered in skills/builtin/__init__.py)
src/carve/runtime/lineage_refresh.py         # NEW — the refresh hook: invoked on build completion (spec 08), on manifest mtime change, on project sync
src/carve/cli/lineage.py                      # NEW — `carve lineage show|upstream|downstream|impact|rebuild`
docs/lineage.md                               # NEW — what the graph models, its v0.1 limits (table grain; no sql-step/column lineage)

tests/unit/test_lineage_fqn.py                       # NEW — per-kind FQN canonicalization; case/quoting normalization
tests/unit/test_lineage_graph_bfs.py                 # NEW — bounded BFS: depth limit, node cap, cycle safety, ResultTooLarge
tests/unit/test_lineage_producer_dlt.py              # NEW — dlt schema + provenance → resource/source nodes + produces edges
tests/unit/test_lineage_producer_dbt.py              # NEW — manifest depends_on.nodes/sources → model/source edges
tests/unit/test_lineage_stitch.py                    # NEW — warehouse:table ↔ dbt:source matched on relation FQN (and schema-qualified mismatch is NOT stitched)
tests/integration/test_lineage_rebuild_on_build.py   # NEW — `carve build` recomputes the graph; transactional replace (readers never see a partial graph)
tests/integration/test_lineage_skills_explorer.py    # NEW — explorer answers "where does X come from" / "what breaks if I change Y" via the skills, citing nodes
```

## Behavior

### Graph model (reconciles ARCHITECTURE §6.2)

ARCHITECTURE §6.2 says "four node types, four edge types" but lists **five** node kinds and omits the dlt source→resource containment edge. This spec is the authoritative reconciliation: **five node kinds, five edge types** (ARCHITECTURE §6.2 is updated to match).

```
Node kinds:
- dlt:source        — a dlt component in el/<name>/        (fqn = component name)
- dlt:resource      — a resource inside that source         (fqn = "<component>.<resource>")
- warehouse:table   — a physical relation in the destination (fqn = "<database>.<schema>.<table>", canonical)
- dbt:source        — a source declared in a dbt project     (fqn = manifest unique_id "source.<proj>.<src>.<tbl>"; attr relation_fqn)
- dbt:model         — a dbt model                            (fqn = manifest unique_id "model.<proj>.<name>"; attr relation_fqn)

Edge types:
- dlt:source     ──defines──▶     dlt:resource     (containment; NEW — reconciles §6.2)
- dlt:resource   ──produces──▶    warehouse:table
- warehouse:table──consumed_by──▶ dbt:source       (the stitch, matched on relation_fqn)
- dbt:source     ──consumed_by──▶ dbt:model
- dbt:model      ──consumed_by──▶ dbt:model         (model-to-model deps)
```

`edge_type ∈ {defines, produces, consumed_by}`. Direction is always **producer → consumer** (upstream → downstream): `upstream_of(X)` walks edges *into* X; `downstream_of(X)` walks edges *out of* X.

A `dbt:model`'s materialized output is represented by the `dbt:model` node itself (its `relation_fqn` attribute records where it lands) — v0.1 does **not** mint a separate `warehouse:table` node for a model's output. `warehouse:table` nodes are the **raw landing relations** (what dlt writes / what dbt sources read). This keeps the graph faithful to §6.2 (dbt:model is a terminal consumer) and avoids double-noding.

### Node identity & FQN (`fqn.py`)

Node identity is `(kind, fqn)`; the builder is **idempotent** because a re-run produces the same `(kind, fqn)` keys. The relation FQN is the **canonical join key** for the stitch, so canonicalization is strict:

- A relation FQN is `"<database>.<schema>.<table>"`, **case-folded to the dialect's identity rules** (Snowflake: upper→fold to lower for the key; DuckDB/Postgres: lower) and **unquoted**. `fqn.py` owns this via the dialect adapters (spec 18) so dlt's destination naming and dbt's `{database, schema, identifier}` resolve to the *same* key.
- `dlt:resource` fqn is `"<component>.<resource>"` — the component name (from the provenance header / locator), not the destination dataset.
- `dbt:source` / `dbt:model` fqn is the manifest **unique_id** (stable across rebuilds), with the resolved `relation_fqn` stored in `attributes` for the stitch.

Mismatched case or a missing database qualifier is the most likely stitch failure; `test_lineage_stitch.py` pins the canonicalization, and the builder logs a `lineage.unstitched_source` event for any `dbt:source` whose `relation_fqn` matched no `warehouse:table` (surfaced by `carve lineage rebuild --explain`).

### The builder (`builder.py` + `producers/`)

`build_graph(paths, *, dbt_manifest, dlt_schemas) -> Graph` composes three producers into one in-memory `Graph`, then hands it to the repository for a transactional replace:

1. **dlt producer (`producers/dlt.py`).** For each dlt component (resolved via the component locator, spec 03): read the **dlt stored schema** (dlt persists a schema whose data tables each carry a `resource` hint) to map `resource → table`, and read the **provenance header** (`provenance.py`, spec 03) of the generated component for the `source` component name + `destination` dataset binding. Emits `dlt:source` + `dlt:resource` nodes, `defines` edges (source→resource), and `produces` edges (resource→`warehouse:table`, the table FQN qualified by the destination database/schema). If a component has never run (no stored schema yet), it contributes its `dlt:source`/`dlt:resource` nodes from the source definition but **no `produces` edges** (no observed tables) — logged, not an error.
2. **dbt producer (`producers/dbt.py`).** Calls the `dbt_manifest` skill (spec 16) — **read-only, the manifest is the source of truth.** For each model node: emit a `dbt:model` node and, from `depends_on.nodes`, `consumed_by` edges (upstream model → this model); from `depends_on.sources`, `consumed_by` edges (`dbt:source` → this model). For each source: emit a `dbt:source` node with `relation_fqn` from `{database, schema, identifier}`. Tests are **not** nodes; `impact_of_change` fetches `tests_on_model` from the manifest on demand. Absent manifest (no dbt project, or never compiled) → the dbt producer contributes nothing (the dlt-only graph is valid).
3. **stitch (`stitch.py`).** For each `dbt:source`, look up the `warehouse:table` whose `fqn` equals the source's `relation_fqn`; if found, add `warehouse:table ──consumed_by──▶ dbt:source`. If not found (the dbt source points at a relation no tracked dlt resource produced — e.g. an externally-loaded table), **mint the `warehouse:table` node anyway** (so the source has an upstream anchor) and mark it `attributes.origin = "external"` — its `upstream_of` is empty with the `incomplete` marker. This is also where a `sql`-step-produced table would land as `external` in v0.1 (see *Out of scope*).

The builder is pure (no DB writes); the repository owns persistence. This keeps producers unit-testable against fixtures.

### Refresh (`lineage_refresh.py`)

The graph is **data, recomputed** — never hand-edited — on the three triggers from ARCHITECTURE §6.3:

- **On `carve build` completion** — the build path (spec 08) calls `refresh_lineage(pipeline)` after a successful build, since a build is what (re)generates dlt components + (re)reads the manifest. This is the primary trigger.
- **On dbt-manifest mtime change** — the manifest cache (spec 16, mtime-keyed) firing an invalidation triggers a dbt-producer-only refresh.
- **On project sync** (separate-repo / multi mode) — after `carve` syncs a component's pinned ref into the workspace cache, lineage for that component is recomputed.

**Atomicity.** v0.1 recomputes the **whole** graph (tenant-scoped) and replaces it in **one transaction** (`LineageRepository.replace_graph(nodes, edges, tenant_id)` deletes the tenant's rows and inserts the new set within a single transaction). Readers therefore always see a complete prior graph or the complete new one — never a half-built graph. Full-replace is correct and simple at v0.1 project sizes; incremental rebuild is deferred. A `lineage.rebuilt` event fires with node/edge counts + duration.

`carve lineage rebuild` forces a full refresh out of band; `--explain` prints unstitched sources + node/edge counts per producer.

### Query layer + skills (`graph.py`, `skills/builtin/lineage.py`)

Bounded BFS over the persisted graph, served by `LineageRepository.upstream(node, depth)` / `.downstream(node, depth)` (recursive CTE in Postgres, depth-limited). Three **structural** skills (ARCHITECTURE §6.4 — *they never silently truncate*):

- **`upstream_of(entity, depth=10)`** — "where does this data come from." Resolves `entity` (a model name, a `db.schema.table`, a `component.resource`, or a fully-qualified node ref) to a node, walks edges inbound to depth, returns the upstream node set as **entity pointers** (kind + fqn + a one-line label), not content.
- **`downstream_of(entity, depth=10)`** — "what reads from this." Walks outbound.
- **`impact_of_change(entity, depth=10)`** — the downstream closure **plus** the dbt tests guarding each affected model (joined from the manifest's `tests_on_model`). This is the "what breaks if I change this" answer the explorer and recovery surface.

Per §6.4, these are *structural*: if a closure would exceed `result_max_chars` or the node cap (`lineage_max_nodes`, default 500), the skill raises **`ResultTooLarge`** with the actual size — it does **not** truncate. The orchestrator narrows (lower `depth`, or pick a more specific entity). BFS is cycle-safe (a visited set); dbt models can't legally cycle, but the walk is defensive. Entity resolution that matches **zero** nodes returns a structured `not_found` (with the closest fqns by prefix) so the explorer can disambiguate rather than hallucinate.

### CLI + explorer / REST wiring

- **CLI** (`carve lineage`): `show <entity>` (the node + immediate neighbors), `upstream <entity> [--depth N]`, `downstream <entity> [--depth N]`, `impact <entity>`, `rebuild [--explain]`. Output is a compact tree; `--json` for machines.
- **Explorer (spec 12).** The three skills join the explorer's read-only grant; the `lineage` classification (already enumerated in spec 12) becomes **backed**. The skills are read-only and carry no write capability, so they compose cleanly with the explorer's `read_only` permission mode (spec 15) — no gate change needed. Spec 12's "lineage deferred — spec 16" notes are flipped to point here.
- **REST** (spec 09): a `GET /lineage/{entity}?direction=&depth=` endpoint wraps the repository (read-only); wired when spec 09 is next touched. Not required for the explorer (which calls the skill in-process).

## Tests

- **Unit (FQN):** per-kind canonicalization — Snowflake `RAW_STRIPE.CHARGES` and dbt's `{database, schema, identifier}` for the same relation fold to one key; quoting/case stripped; a `dlt:resource` fqn is `component.resource`, not the dataset.
- **Unit (BFS):** depth limit honored; node cap → `ResultTooLarge` with the real size (never truncated, never silently capped); a synthetic cycle terminates; `upstream`/`downstream` directions are correct inverses.
- **Unit (dlt producer):** a fixture dlt stored schema + provenance header yields the expected `dlt:source`→`dlt:resource` `defines` edges and `resource`→`warehouse:table` `produces` edges; a never-run component yields nodes but no `produces` edges (logged).
- **Unit (dbt producer):** a fixture `manifest.json` yields `dbt:model` nodes with `consumed_by` edges from `depends_on.nodes`, and `dbt:source`→model edges from `depends_on.sources`; an absent manifest contributes nothing.
- **Unit (stitch):** a `dbt:source` and a dlt-produced `warehouse:table` with the same relation FQN are stitched; a case/schema mismatch is **not** stitched (and logs `lineage.unstitched_source`); a dbt source with no producing resource mints an `origin="external"` table node.
- **Integration (rebuild on build):** `carve build` recomputes the graph; an in-flight reader sees the prior complete graph until the replace transaction commits (no partial state); `lineage.rebuilt` fires with counts.
- **Integration (explorer):** `carve ask "where does stg_orders come from?"` walks `upstream_of` and cites the dlt source/resource + warehouse table + dbt source; `carve ask "what breaks if I change raw_stripe.charges?"` returns the downstream models **and** their guarding tests via `impact_of_change`.

## Acceptance

- The graph models ARCHITECTURE §6.2 (reconciled to five nodes / five edges) at table/relation grain, persisted in `lineage_nodes`/`lineage_edges`, and is **rebuilt on build / manifest-change / sync** — never hand-edited.
- `upstream_of` / `downstream_of` / `impact_of_change` answer "where does this come from" / "what reads this" / "what breaks if I change this" as **entity pointers**, are **bounded** (depth + node cap), and **raise `ResultTooLarge` rather than truncate** (§6.4).
- The **explorer** (spec 12) answers a lineage question end-to-end against a brownfield dlt+dbt project, citing real nodes — the spec-16 "lineage graph owner" flag is resolved.
- A rebuild is **transactional** (no reader sees a partial graph) and **idempotent** (re-running on unchanged inputs produces an identical node/edge set).
- v0.1 limits are explicit and non-misleading: no column-level lineage, no `sql`-step producer edges (such tables are marked `external`, and `upstream_of` returns the `incomplete` marker rather than implying a true source), no static-UI rendering.

## Design notes

- **Why a persisted graph, not compute-on-demand?** ARCHITECTURE §6.2/§6.3 commit to persisted `lineage_nodes`/`lineage_edges` rebuilt on triggers, with the lineage cache invalidated "on build, manifest change, project sync." Recomputing a full closure from the manifest + dlt schema on every `ask` would be slower and would not survive the explorer's bounded-BFS contract. The graph is cheap to store and the BFS is a recursive CTE.
- **Why table grain in v0.1 (column-level deferred)?** Table/relation lineage answers the two questions that matter for the brownfield demo — provenance ("where from") and impact ("what breaks") — using producers that already exist (dlt schema, dbt manifest). Column-level needs `sqlglot` column resolution through every model's SQL, which is the genuinely hard part and pairs naturally with the v0.2 dbt-authoring work.
- **Why is `sql`-step lineage deferred?** It needs `sqlglot` table extraction *and* FQN resolution against the connection's default database/schema (a `sql` step's SQL is often unqualified). The cost/uncertainty isn't worth blocking v0.1; the dlt+dbt graph stands alone, and such tables are honestly marked `external` rather than silently dropped or mislabeled as sources. The `sqlglot` table-extractor is a small, well-scoped v0.2 addition.
- **Why the stitch on relation FQN (not name heuristics)?** A dbt source's `{database, schema, identifier}` and a dlt resource's destination table resolve to the *same* physical relation; matching on the canonicalized FQN is exact. Name-only matching would mis-stitch across schemas. The one fragility — dialect case-folding — is isolated in `fqn.py` and pinned by tests.
- **Relationship to pipeline-step lineage (glossary).** The glossary notes "pipeline-level lineage (which step produced which artifact)." In v0.1 that question is answered by **joining** this asset graph with the pipeline definition (spec 08): a `dlt:resource`'s component is referenced **by name** by some pipeline step, so "what refreshes `raw_stripe.charges`?" = the pipeline(s) whose step references the component that `produces` it. No separate `pipeline`/`step` node type is introduced.

## Open questions

- **REST endpoint ownership.** This spec defines `GET /lineage/{entity}` but defers wiring to spec 09 (REST). Confirm the route + response shape when 09 is next revised. *(Smallest-reasonable choice: read-only wrapper over `LineageRepository`, mirroring the skill output.)*
- **Manifest-change refresh granularity.** A manifest mtime change triggers a dbt-producer-only recompute today, but the stitch still runs full (it needs the dlt-side `warehouse:table` set). For v0.1 the whole-graph transactional replace makes this moot; revisit if incremental rebuild lands.
- **dlt stored-schema access path.** The dlt producer reads dlt's persisted schema for the `resource → table` map. Confirm against spec 04's chosen access (dlt Python API `pipeline.default_schema` vs. the exported schema file) when 04 is implemented; `producers/dlt.py` should depend on a thin adapter so the access path is swappable.
