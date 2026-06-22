# Project memory: standards, decisions, conventions, sidecars

> Ships the runtime read/edit/refresh machinery for the memory files that [`init`](./init.md) scaffolds. Per [PRD §5.2](../PRD.md), [PRD §6.3 project memory](../PRD.md), and [ARCHITECTURE §5.4 pre-scoped context](../ARCHITECTURE.md).

## Status

- **Status:** Partially landed — lean core (Increment 2). The read/append machinery shipped; the heavier parts are deferred (see below). This spec is the durable design *target*; the list below records the current gap.
- **Depends on:** [layout](./layout.md), [init](./init.md) (the file scaffolding + convention-inference engine)
- **Blocks:** [ask](./ask.md) (`carve ask "why did we do X?"` cites `decisions.md`)
- **Coordinated with:** [dlt-engineer](./dlt-engineer.md), [runtime](./runtime.md), [pipelines](./pipelines.md) — these all read memory files via the loader this spec ships
- **Landed (lean, Increment 2):** the `carve.core.memory` package — `MemoryLoader` (mtime-cached reads of the five file types over `ProjectPaths`, `invalidate()`), `select_for_task`/`MemoryBundle` (always conventions+standards; decisions when `is_investigative`; sidecars when present), `MemoryWriter.append_decision` (newest-first anchored to the dated-entry region, dup-by-(title,date) → `DecisionAlreadyExists`, atomic temp+`os.replace` write, loader invalidation), the `carve memory show / edit / append-decision` CLI, and the **dormant** orchestrator hook `attach_memory_to_context` (unit-tested; not yet wired — no caller produces a goal classification until plan-build).
- **Deferred (not yet built; each tracked):** `carve memory refresh` (needs the convention-inference engine — same blocker as init's deferred `conventions.md` inference); the REST `/api/v1/memory/*` surface (no API app exists — owned by rest-api, a later increment); the MCP memory tools (auto-generated from REST — blocked behind it); the `plan_id`-gated `standards`/sidecar **writes** (the Plan/Build state model can't express the "built, not deployed" gate, and no flow produces a valid `plan_id` for a memory edit yet — so `carve memory edit` writes the file directly); and the live orchestrator **wiring** of the hook (goal classification lands with plan-build).

## Goal

Provide the runtime side of project memory:

1. **A loader** that reads memory files with mtime-based caching, used by the orchestrator and every specialist agent
2. **A selection layer** the orchestrator uses to pick which memory files belong in a given task's pre-scoped context
3. **The `carve memory` CLI command group** with full REST + MCP parity (per PRD's mandatory parity rule, [§6 intro](../PRD.md))
4. **The `carve memory refresh` convention re-inference** that calls into spec 05's inference engine to regenerate `conventions.md` on demand
5. **The `carve memory append-decision` helper** that does the right thing for the common "record a decision" workflow without forcing the user through the full plan/build cycle for an append-only addition
6. **The write policy enforcement**: memory file modifications (other than append-decision) go through the standard plan/build flow; agents never autonomously rewrite memory

After this spec lands, every other spec that needs memory access uses the loader provided here; no spec implements its own memory reading.

## Out of scope

- The initial scaffolding of memory files (lives in spec 05)
- The convention-inference engine itself (lives in spec 05 — this spec calls into it)
- Embedding-based semantic search over memory files
- A web UI for editing memory (the static HTML UI from spec 11 renders memory read-only; edits happen via CLI, REST, MCP, or `$EDITOR`)
- A separate "memory index" in Postgres for fast queries (file-based with mtime cache is sufficient — see Design notes)

## Behavior

### File catalog

This spec governs five file types (all defined in spec 03's layout):

| File                                | Owner            | Mutability                          | Read on every invocation? |
|-------------------------------------|------------------|-------------------------------------|---------------------------|
| `carve/conventions.md`              | Carve (refresh)  | Regenerable; never user-edited      | Yes                       |
| `carve/standards.md`                | User             | User-editable via $EDITOR or plan   | Yes                       |
| `carve/decisions.md`                | User (append)    | Append-only; entries are immutable  | Conditional (see selector)|
| `pipelines/<name>.md`               | User             | User-editable                       | When goal touches `<name>` |
| `el/<name>/NOTES.md`                | User             | User-editable                       | When goal touches `<name>` |

"Owner" is the canonical writer. "Mutability" describes the expected edit pattern; the file-write guardrail enforces that agents never autonomously modify any of these — writes happen only via the plan/build flow or the explicit CLI/REST/MCP commands this spec ships (which themselves emit a Plan in the case of non-append changes).

### Loader

`src/carve/core/memory/loader.py` exposes:

```python
@dataclass(frozen=True)
class MemoryFile:
    path: Path
    contents: str
    mtime: datetime
    size_bytes: int

class MemoryLoader:
    def __init__(self, paths: ProjectPaths): ...           # from spec 03

    def load_conventions(self) -> Optional[MemoryFile]: ...
    def load_standards(self) -> Optional[MemoryFile]: ...
    def load_decisions(self) -> Optional[MemoryFile]: ...
    def load_pipeline_sidecar(self, name: str) -> Optional[MemoryFile]: ...
    def load_el_sidecar(self, name: str) -> Optional[MemoryFile]: ...

    def invalidate(self, path: Optional[Path] = None) -> None: ...
```

Caching: in-process dict keyed by `path` with `(mtime, MemoryFile)` tuples. On each load, `os.stat(path).st_mtime` is compared against the cache; on mismatch, the file is re-read. This is the same mtime-watch pattern used for dbt manifest caching (ARCHITECTURE §6.3).

In the hosted product, the loader is swapped for a Redis-backed variant so multiple API replicas share the cache. The subscriber API is the same.

### Selector

`src/carve/core/memory/selector.py`:

```python
@dataclass(frozen=True)
class MemoryBundle:
    conventions: Optional[MemoryFile]
    standards: Optional[MemoryFile]
    decisions: Optional[MemoryFile]                       # often None for plan/build; populated for ask
    pipeline_sidecars: dict[str, MemoryFile]              # keyed by pipeline name
    el_sidecars: dict[str, MemoryFile]                    # keyed by el artifact name

def select_for_task(
    *,
    classification: str,                                   # goal classification from orchestrator
    pipeline_targets: list[str],                          # pipelines this goal touches
    el_targets: list[str],                                 # el artifacts this goal touches
    is_investigative: bool,                                # True for `carve ask` invocations
    loader: MemoryLoader,
) -> MemoryBundle: ...
```

Selection rules:

- `conventions` and `standards` are **always** included
- `decisions` is included when `is_investigative=True` (i.e., `carve ask` invocations) so "why did we do X?" questions can cite from it. For `plan` invocations, it's only included when the goal classification suggests decision-relevant context (e.g., `classification = "modify_pipeline"` and the pipeline has decision entries citing it). The selector keeps a `[classification → include-decisions?]` mapping that's easy to tune.
- `pipeline_sidecars` includes every entry in `pipeline_targets`
- `el_sidecars` includes every entry in `el_targets`

The bundle is what gets handed to the orchestrator's pre-scoping logic and ultimately serialized into specialists' context.

### Writer

`src/carve/core/memory/writer.py`:

```python
class MemoryWriter:
    def __init__(self, paths: ProjectPaths, state_store: StateStore): ...

    def append_decision(self, *, date: date, title: str, body: str, reviewers: list[str]) -> Path: ...
    """Append a formatted entry to carve/decisions.md. Idempotency: two appends with the same
    title on the same date raise DecisionAlreadyExists unless force=True."""

    def write_standards(self, contents: str, *, plan_id: UUID) -> Path: ...
    """Replace carve/standards.md contents. Requires a plan_id (writes go through plan/build)."""

    def write_pipeline_sidecar(self, name: str, contents: str, *, plan_id: UUID) -> Path: ...
    def write_el_sidecar(self, name: str, contents: str, *, plan_id: UUID) -> Path: ...
```

The key invariant: **non-append memory writes require a `plan_id`**. The writer validates that the plan exists and is in a state that authorizes the write (built, not yet deployed). This is how the plan/build flow becomes the write gate for `standards.md` and sidecar files — agents can't bypass it.

`append_decision` is the exception: it's an append, so it's safe to do directly without a plan. This makes `carve memory append-decision "..."` a one-shot UX rather than a forced plan/build cycle for a common low-risk action.

### Decision entry format

`decisions.md` entries are formatted consistently:

```markdown
## 2026-04-12 — Stripe retention policy

**Decision:** Keep Stripe charges in `raw_stripe` for 18 months, not 24.
**Rationale:** Storage cost vs analytics utility tradeoff; 18 months covers all reporting cycles we care about.
**Reviewers:** alice@, bob@
**Impact:** `el/stripe_charges/`, downstream `stg_stripe_charges` model.
```

The `append_decision` helper takes `date`, `title`, `body`, `reviewers`. As shipped, the helper enforces only the **heading** (`## YYYY-MM-DD — <title>`) and an optional trailing `**Reviewers:** …` line; the `body` is rendered as-is (markdown supported), so the **Decision/Rationale/Impact** bold labels shown above are a recommended convention the user writes in the body, not labels the helper injects. (The helper rejects a multi-line `title`, which would otherwise forge extra headings.)

### CLI

```
carve memory show                              # list memory files with sizes + mtime
carve memory show <file>                       # print one file's contents (e.g., `carve memory show standards`)
carve memory show --pipeline <name>            # print the scoped memory bundle for that pipeline
carve memory edit <file>                       # open in $EDITOR (e.g., `carve memory edit standards`)
                                               #   for files requiring plan/build (standards, sidecars), this is
                                               #   equivalent to: carve plan "update memory file X", carve build, carve deploy
                                               #   (or with --direct, bypasses plan/build — see below)
carve memory append-decision <title>           # interactive prompt for body + reviewers; appends directly
carve memory append-decision <title> --body "..." --reviewers alice@,bob@
                                               # non-interactive
carve memory refresh                           # re-run convention inference (writes carve/conventions.md)
carve memory refresh --backend dbt             # only re-infer dbt conventions
carve memory refresh --backend dlt             # only re-infer dlt conventions
```

`--direct` on `carve memory edit`: bypasses the plan/build flow for an interactive editing session. The user opens the file in `$EDITOR`, makes changes, saves; the file is written directly (no plan). This is the "I just want to type into my standards.md right now" escape hatch. Behind the scenes, `--direct` still produces a Plan + Build record so the change appears in run history — but it doesn't open a PR. PR-based promotion remains available via the plan/build path.

### REST

```
GET    /api/v1/memory                          # list with metadata
GET    /api/v1/memory/{kind}                   # kind ∈ {conventions, standards, decisions}; returns contents
GET    /api/v1/memory/pipelines/{name}         # pipeline sidecar contents
GET    /api/v1/memory/el/{name}                # el sidecar contents
GET    /api/v1/memory/bundle                   # query params: classification, pipelines[], els[], is_investigative
                                               # returns the MemoryBundle that would be selected
PUT    /api/v1/memory/{kind}                   # body: { contents: "...", plan_id: "..." }
                                               # validates plan_id, writes
PUT    /api/v1/memory/pipelines/{name}         # body: { contents: "...", plan_id: "..." }
PUT    /api/v1/memory/el/{name}                # body: { contents: "...", plan_id: "..." }
POST   /api/v1/memory/decisions                # body: { date, title, body, reviewers }
                                               # appends directly; no plan_id required
POST   /api/v1/memory/refresh                  # body: { backend?: "dbt" | "dlt" }
                                               # triggers convention re-inference
```

### MCP

Auto-generated from REST per spec 10's MCP-from-REST pattern:

```
memory_list()
memory_show(kind)                              # kind ∈ {conventions, standards, decisions}
memory_show_pipeline(name)
memory_show_el(name)
memory_bundle(classification, pipelines=[], els=[], is_investigative=False)
memory_write(kind, contents, plan_id)
memory_write_pipeline(name, contents, plan_id)
memory_write_el(name, contents, plan_id)
memory_append_decision(date, title, body, reviewers)
memory_refresh(backend=None)
```

### Orchestrator integration

`src/carve/core/agents/orchestrator_hooks/memory.py` exposes:

```python
def attach_memory_to_context(task_context: dict, *, classification, pipeline_targets, el_targets, is_investigative, loader) -> dict:
    bundle = select_for_task(...)
    task_context["memory"] = {
        "conventions": _slice_or_full(bundle.conventions, classification),
        "standards":   bundle.standards.contents if bundle.standards else None,
        "decisions":   bundle.decisions.contents if bundle.decisions else None,
        "pipeline_notes": {n: f.contents for n, f in bundle.pipeline_sidecars.items()},
        "el_notes":       {n: f.contents for n, f in bundle.el_sidecars.items()},
    }
    return task_context
```

`_slice_or_full` returns the full conventions document by default. If a future version of the orchestrator wants to slice conventions to only the relevant sections (e.g., dbt sections for a dbt-only goal), the hook is the place to do it; for now it returns the full document since conventions are not large.

This hook is invoked by the orchestrator after classification + impact-context gathering, before specialist dispatch.

### Write policy: standards vs append-only entry

The two write paths are deliberately different:

- **Standards / sidecars**: replacing or substantially modifying a `.md` file. Requires plan/build because:
  - The change might conflict with downstream expectations (an agent reading the new standards might need to refine its output)
  - It's a deliberate team decision worth reviewing
  - PR-based promotion gives the rest of the team a chance to weigh in
- **Decision append**: appending a dated entry to `decisions.md`. Allowed without plan/build because:
  - It's a record of a decision that's already been made (the team decided; this is just writing it down)
  - Append-only is structurally safe (no conflicts, no overwrites)
  - Forcing plan/build for "record this decision" is high friction for low value

This is the rationale users see in `docs/project-memory.md`.

### Convention refresh

`carve memory refresh` calls into spec 05's `convention_inference` modules:

```python
def refresh_conventions(paths: ProjectPaths, *, backend: Optional[str] = None) -> Path:
    sections = []
    if backend is None or backend == "dbt":
        sections.append(infer_dbt_conventions(paths))
    if backend is None or backend == "dlt":
        sections.append(infer_dlt_conventions(paths))
    merged = combine_inferences(sections)
    paths.carve_dir.joinpath("conventions.md").write_text(merged)
    return paths.carve_dir / "conventions.md"
```

Refresh is fast (no LLM calls; pure code analysis) — under 60 seconds for most projects. It's safe to run repeatedly. Users typically run it after adding new dbt models or dlt pipelines to make sure the agent's understanding is current.

### Caching invalidation

When `MemoryWriter` writes a file (via any path), it calls `loader.invalidate(path)` so the next read picks up the new contents. The orchestrator-side cache miss is on the order of milliseconds for typical memory file sizes.

In the hosted product's Redis-backed cache, invalidation is a pub/sub message that all API replicas subscribe to.

## Tests

- **Unit**: loader correctly caches by mtime; modifying a file's mtime causes a re-read; cache invalidation works
- **Unit**: selector picks the right files for each task classification; `is_investigative` flips decisions inclusion
- **Unit**: writer's `append_decision` formats entries consistently; rejects duplicates by title+date
- **Unit**: writer rejects non-append writes without a valid `plan_id`
- **Integration (CLI)**: `carve memory show standards` prints the file; `carve memory edit --direct standards` opens `$EDITOR`, accepts edits, writes, surfaces a Build record
- **Integration (CLI)**: `carve memory append-decision "Test decision" --body "..." --reviewers alice@` appends an entry; running it again with the same title same day raises `DecisionAlreadyExists`
- **Integration (REST)**: `GET /api/v1/memory/bundle?classification=modify_pipeline&pipelines=stripe&is_investigative=false` returns the expected bundle
- **Integration (REST)**: `PUT /api/v1/memory/standards` without `plan_id` returns 422; with a valid `plan_id` succeeds and the file is updated
- **Integration (orchestrator)**: a `carve plan "modify stripe pipeline"` invocation receives a context bundle with `pipeline_notes["stripe"]` populated; verifying the orchestrator hook is wired correctly
- **Integration (refresh)**: `carve memory refresh` against a fixture project regenerates `conventions.md` with both dbt and dlt sections in deterministic markdown

## Acceptance

- The five memory file types are all readable through one loader API
- Memory bundles are correctly selected per goal classification; `decisions` inclusion follows the `is_investigative` flag
- `carve memory append-decision` adds a dated entry directly (no plan required)
- All other memory writes require a valid `plan_id`; bypass attempts return clean errors
- `carve memory refresh` regenerates `conventions.md` in under 60 seconds for a typical project
- REST + MCP coverage is full per PRD's parity rule
- The orchestrator hook attaches memory to context on every plan/build/ask invocation
- Cache invalidation is timely: a memory edit is visible to the next agent invocation immediately (no stale-cache window)
- `docs/project-memory.md` walks a user through writing their first standards entry and recording their first decision in under 15 minutes

## Design notes

- **Why file-based memory instead of a Postgres table?** Three reasons. (1) Git-versioned: changes show up in PRs alongside code changes, with the team's existing review process. (2) User-editable in any text editor; no Carve-specific tools required to read or edit. (3) Portable: a Carve project can be cloned without Carve being installed, and the memory files are immediately readable as plain markdown.
- **Why a Postgres table for memory writes (via the `plan_id` requirement) but not for memory reads?** Because writes need an audit trail and a review gate; reads need to be fast. Plan/Build rows in Postgres provide the audit + review gate without needing memory itself in the database.
- **Why is `append_decision` exempt from the plan/build requirement?** Because the cost-of-friction calculus is different. Recording a decision is low-risk (it's information, not code); requiring plan/build would push users away from recording decisions at all, which is worse than recording them with slightly less ceremony. Standards changes get the heavier process because they directly change agent behavior.
- **Why mtime caching instead of a real cache invalidation protocol?** Simplicity. mtime is what dbt does for its manifest cache; it's well-understood and reliable for single-process OSS. The hosted product's Redis-backed variant uses real pub/sub invalidation because mtime breaks down across replicas.
- **Why no embedding search inside the memory capability?** Memory is small (rare for a single file to exceed 50KB; full bundle stays well under the agent's context budget), so the memory loader hands the whole bundle to context rather than retrieving slices. Embedding-based semantic search is an in-scope retrieval layer, owned by the dedicated [semantic-search](./semantic-search.md) capability, not duplicated here.
- **Why isn't there a "memory query" skill that agents can call?** Because we want memory in pre-scoped context, not as a discovery skill that agents can choose to call. Forcing memory into the bundle every time means agents can never miss it; making it a skill creates the risk that agents skip it and ignore the team's standards. Pre-scoping is the safer default.

## Open questions

- **Should `decisions.md` entries be reverse-chronological (newest first) or chronological?** *Implementation default.* Newest first — matches changelog conventions and means recently-relevant entries are easier to find when reading top-down. The `append_decision` helper inserts at the top of the entries section (below the file's header).
- **Should `standards.md` have any imposed structure (e.g., section headers per backend)?** *Implementation default.* No imposed structure for now; the template provides examples but doesn't enforce. The agent reads the whole file as free-form text. If users start writing very large standards files, we can add structure later.
- **What happens if a user-edited file has malformed markdown?** *Implementation default.* The loader returns the file as-is; agents receive the raw text and the LLM is robust to malformed markdown. No validation step. Worth revisiting if it causes issues in practice.
- **Should `carve memory show --pipeline <name>` print the full bundle that would be loaded, or just the memory subset?** *Implementation default.* Just the memory subset (conventions + standards + that pipeline's sidecar + any decisions mentioning the pipeline). Full pre-scoped context including catalog queries etc. is much larger and noisier; users who want it can call the REST `/bundle` endpoint with `?full=true` (which is a different consumer's concern).
