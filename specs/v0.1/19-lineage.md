# v0.1-19 — Lineage: investigate dbt + dlt native lineage (no Carve store)

> **Decision: Carve maintains no lineage store.** There is no `lineage_nodes`/`lineage_edges` graph, no builder, no recompute/refresh machinery. Lineage is a capability the **explorer** (spec 12) *investigates on demand* — it reads the code and queries the lineage that **dbt** and **dlt** already produce: dbt's `manifest.json` (model-level DAG, sources, tests) and dlt's stored schema (resource → destination table). The one thing this spec ships is a thin **`dlt_schema` reader skill** so dlt's native lineage is as accessible as dbt's manifest already is via `dbt_manifest`. **This reverses the original ARCHITECTURE §6.2 "lineage is the one Carve-owned piece of retrieval" decision** — see *Design notes*. It is consistent with the AI-harness model ([`../_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md)): the agent investigates with tools, grounded in real tool output, rather than Carve maintaining derived state.

## Status

- **Status:** Drafting
- **Depends on:** [v0.1-12 ask-verb](./12-ask-verb.md) (the explorer that does the investigating + its citation model), [v0.1-16 extensibility](./16-extensibility.md) (the `@skill` registration pattern + the existing `dbt_manifest` reader this parallels), [v0.1-04 el-agent-dlt](./04-el-agent-dlt.md) (a dlt run produces the stored dlt schema the `dlt_schema` skill reads), [v0.1-03 flat-layout](./03-flat-layout.md) (component locator → where each dlt component lives), [v0.1-18 sql-layer](./18-sql-layer.md) (the `sql` tool the explorer uses for `INFORMATION_SCHEMA` checks). Reverses part of ARCHITECTURE **§6.2/§6.1/§9.6**.
- **Blocks:** nothing structurally — it *completes* [v0.1-12](./12-ask-verb.md)'s `lineage` classification by adding the missing dlt-side reader. No other spec waits on it.
- **Lineage:** net-new; supersedes the (never-built) ARCHITECTURE §6.2 lineage graph. Resolves the [v0.1-16](./16-extensibility.md) "lineage graph owner" open question by **deciding not to build a graph.**

## Goal

Make the explorer able to answer lineage questions — "where does this data come from," "what reads from this," "what breaks if I change this" — by **investigating dbt's and dlt's native lineage plus the code**, with **no Carve-maintained lineage artifact**. Concretely:

1. **Use what already exists.** dbt's manifest *is* model-level lineage; the `dbt_manifest` skill (spec 16) already exposes it (`model_dependencies`, `depends_on.nodes`/`sources`, `tests_on_model`). `grep`/`read_file` already read `sources.yml`, model SQL, and dlt source code. The `sql` tool already introspects the live warehouse. The explorer already has all of these.
2. **Add the one missing reader: `dlt_schema`.** A thin, read-only skill that surfaces **dlt's own stored schema** — which resource produced which destination table, and the source→resource structure — so the agent gets structured access to dlt's native lineage instead of hand-parsing schema files. This is the dlt parallel to `dbt_manifest`.
3. **Give the explorer an investigation playbook** (in its agent definition): which tool answers which lineage question, and how to correlate a dlt destination table with a dbt source (by relation name + `sql` introspection) **in-context**, citing what it found.

That is the whole spec. No tables, no builder, no graph walk.

## Out of scope

- **Any persisted/precomputed lineage.** Explicitly **not** built: `lineage_nodes`/`lineage_edges`, a lineage builder, refresh-on-build/manifest-change/sync triggers, a stitch step, FQN canonicalization tables, BFS query infrastructure. The agent investigates live; it does not consult a Carve store. (This is the reversal — ARCHITECTURE §6.1 layer 4, §6.2, §6.3 lineage row, and §9.6 are updated to drop the graph.)
- **Column-level lineage.** The agent can read a model's SQL to reason about column derivation when asked, but Carve ships no structured column-lineage feature; deeper column lineage pairs with v0.2 dbt authoring.
- **Static-UI lineage view** — already deferred (spec 11); nothing to render since there's no graph. The hosted cloud UI may build its own visualization post-v0.1.
- **Embedding / semantic search** (ARCHITECTURE §6.1 layer 5) — post-v0.1.
- **A `lineage`/`upstream_of`/`downstream_of`/`impact_of_change` graph-query skill family** — there is no graph to query. Those names are retired.

## Files this spec produces

```
src/carve/core/skills/builtin/dlt_schema.py    # NEW — the `dlt_schema` reader skill: dlt's stored schema → resource→table + source→resource (read-only)
src/carve/core/skills/builtin/__init__.py      # MODIFY — register `dlt_schema` alongside `dbt_manifest`, `memory_read`
src/carve/integrations/dlt/schema_reader.py    # NEW — thin adapter over dlt's stored schema (Python API or exported schema file); the skill's data source
src/carve/core/agents/builtin/explorer.md      # MODIFY — add `dlt_schema` to the grant; add the lineage investigation playbook (which tool for which question; dlt-table↔dbt-source correlation by name)
docs/lineage.md                                 # NEW — "how Carve answers lineage: it investigates dbt + dlt native lineage and the code; it maintains no lineage store"

tests/unit/test_dlt_schema_skill.py                  # NEW — a fixture dlt stored schema → resource→table + source→resource; a never-run component degrades gracefully (source/resource names, no tables)
tests/integration/test_explorer_lineage_investigation.py  # NEW — explorer answers "where does stg_orders come from?" and "what breaks if I change raw_stripe.charges?" by chaining dbt_manifest + dlt_schema + grep, citing real findings; asserts NO lineage table is read or written
```

## Behavior

### The `dlt_schema` reader skill

`dlt_schema` is a **read-only** built-in skill (registered like `dbt_manifest`), surfacing dlt's *own* schema for a resolved dlt component. It reads dlt's stored schema via a thin adapter (`schema_reader.py`) — it does **not** persist or transform anything into a Carve store.

```python
@skill
def dlt_schema(component: str, op: str = "resource_tables", **kw) -> dict | list:
    """Read dlt's native stored schema for a dlt component (resolved via the component locator).
    op ∈ {
      'list_resources':  () -> list[str],                       # resource names in the source
      'resource_tables': () -> dict[str, list[str]],            # resource -> [destination tables it writes]
      'table_resource':  (table: str) -> str | None,            # which resource produced a given table
      'destination':     () -> {"database": str, "schema": str}, # the dataset the component lands in (from the provenance header)
    }
    Degrades gracefully: a component that has never run has source/resource structure
    (from its definition) but no resource->table mappings yet — returns those empty with
    a 'never_run' marker rather than erroring.
    """
```

The resource→table mapping comes from dlt's stored schema (dlt tags each data table with the `resource` that produced it); the destination dataset comes from the generated component's provenance header (spec 03). This is a **structural** skill (ARCHITECTURE §6.4): it returns facts about one component and never truncates — it raises `ResultTooLarge` if a result somehow exceeds the cap, prompting the agent to narrow (`table_resource` for one table rather than the full map).

### The explorer's lineage investigation (playbook in `explorer.md`)

The explorer answers a lineage question by composing the tools it already has, in its isolated context, then citing what it found — no graph, no precomputation:

- **"Where does `<dbt model>` come from?"** → `dbt_manifest` `model_dependencies` walks upstream models + sources (dbt's own DAG). For each terminal `dbt:source`, correlate to the producing dlt resource: resolve the source's relation, then `dlt_schema` `table_resource` (and/or `grep` the dlt source code) to name the resource + component. The agent reasons over these results; the chain (model ← models ← dbt source ← dlt table ← dlt resource ← component) lives in its answer, not in a stored graph.
- **"What reads from `<table>` / `<dlt resource>`?"** → identify the relation, then `dbt_manifest` to find sources/models referencing it (`grep` `sources.yml` + manifest `sources`), enumerate the dependent models from dbt's DAG, and report them.
- **"What breaks if I change `<X>`?"** → the downstream models from dbt's DAG **plus** their guarding tests (`dbt_manifest` `tests_on_model`). dbt already computes the downstream set; the agent surfaces it.
- **"What refreshes `<table>`?"** → `dlt_schema` `table_resource` names the resource/component; the pipeline(s) whose step references that component **by name** (read `pipelines/*.toml`, spec 08) are what refresh it. This is the "pipeline-level lineage" the glossary mentions — answered by reading the pipeline definitions, not a node graph.
- **Cross-boundary correlation (dlt table ↔ dbt source)** is done **by relation name** in-context: dbt's source `{database, schema, identifier}` and dlt's destination table are the same physical relation; the agent matches them (and can confirm with a `sql` `INFORMATION_SCHEMA` check). There is no persisted stitch — the agent does it live for the slice it's investigating.

Because the explorer runs in its own isolated context (spec 15) and returns a **summary with cited entities** (spec 12), the investigation is bounded naturally: it pulls the relevant slice of dbt/dlt lineage, not "the whole graph."

### Citations

Lineage findings cite the **underlying native artifacts**, not Carve nodes: a dbt manifest node (`model.<proj>.<name>` / `source.<proj>.<src>.<tbl>`), a dlt resource (`<component>.<resource>`), a destination relation (`db.schema.table`), or a file:line (`sources.yml`, model SQL, dlt source). This fits spec 12's existing `cited_entities` model — the citation points at the real thing dbt/dlt owns.

## Tests

- **Unit (`dlt_schema`):** a fixture dlt stored schema yields the expected `resource_tables` map and `table_resource` lookups; `destination` reads from the provenance header; a never-run component returns resource/source names with empty table maps + a `never_run` marker (no error).
- **Integration (explorer investigation):** `carve ask "where does stg_orders come from?"` chains `dbt_manifest` (model deps → source) + `dlt_schema`/`grep` (source → dlt resource/component) and cites them; `carve ask "what breaks if I change raw_stripe.charges?"` returns the downstream dbt models **and** their tests via `dbt_manifest`. The test asserts **no `lineage_*` table is created, read, or written** anywhere in the flow (guards the no-store decision).
- **Unit (no store):** a grep-style guard test that the codebase defines no `lineage_nodes`/`lineage_edges` model or migration (prevents regression to a graph).

## Acceptance

- The explorer answers "where from / what reads this / what breaks / what refreshes" against a brownfield dlt+dbt project by **investigating dbt's manifest + dlt's schema + the code**, citing the native artifacts.
- **Carve persists no lineage graph** — no `lineage_nodes`/`lineage_edges`, no builder, no refresh triggers; the no-store guard test passes.
- `dlt_schema` gives the agent structured access to dlt's **own** resource→table lineage, parallel to `dbt_manifest`; both are read-only and compose with the explorer's `read_only` mode (spec 15) with no gate change.
- ARCHITECTURE §6.1 (layer 4), §6.2, §6.3, and §9.6 are updated to drop the Carve-owned lineage graph and describe lineage-by-investigation; the spec-16 "lineage graph owner" flag is resolved as **"no graph — investigate instead."**

## Design notes

- **Why no store (the reversal).** The original §6.2 made lineage "the one Carve-owned piece of retrieval" with persisted `lineage_nodes`/`lineage_edges` rebuilt on build/manifest-change/sync. That means a derived data structure Carve must keep correct: rebuild triggers, staleness, a stitch, FQN canonicalization, transactional replace. dbt's manifest and dlt's schema **already are** that lineage, authoritative and maintained by the tools themselves. Having the agent investigate them on demand removes a whole subsystem and a class of "the graph is stale / the stitch mis-matched" bugs, and is the consistent expression of the AI-harness philosophy: ground the agent in real tool output, don't precompute derived state. *(Decision by Nate, 2026-06-17.)*
- **The tradeoff, honestly.** A persisted graph could answer "impact across a 5,000-model project" in one indexed query; investigation pulls the relevant slice per question instead. For the v0.1 audience and the explorer's bounded-context model, investigating the slice is the right call — and it never goes stale. If a future scale need emerges, dbt's manifest is already a complete graph to load on demand; we still wouldn't need a *Carve* store.
- **dlt parity.** dbt lineage was already first-class (manifest + `dbt_manifest`); dlt's was not exposed structurally. `dlt_schema` closes that gap so "use dlt's built-in lineage" is a real, structured capability rather than ad-hoc grepping — while still being dlt's lineage, not Carve's.
- **Pipeline-level lineage** ("which step refreshes which table," glossary) falls out of `dlt_schema` (`table_resource`) + the pipeline definitions (component-by-name, spec 08) — no separate construct.

## Open questions

- **dlt stored-schema access path.** `schema_reader.py` reads dlt's persisted schema for the resource→table map; confirm the access (dlt Python API `pipeline.default_schema` vs. the exported schema YAML) against spec 04's chosen approach when 04 is implemented. The skill depends on the thin adapter so the path is swappable.
- **Explorer prompt depth.** The investigation playbook lives in `explorer.md`; how prescriptive to make it (explicit tool-chaining recipes vs. trusting the model to compose) is a prompt-tuning question to settle during spec-12 implementation. *(Smallest-reasonable choice: ship the recipes above as guidance, let the model adapt.)*
