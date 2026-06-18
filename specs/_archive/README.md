# Archive

Specs that informed Carve's design but are not authoritative anymore. They live here as **historical reference and source material**; the current direction lives in [`specs/pillar-N-*/`](../) directories.

## What's here

| Directory | Status | What it was |
|---|---|---|
| [`milestone-2-real-product/`](milestone-2-real-product/) | Archived (2026-05-07) | The original "real product" milestone, restructured into Pillars 1-4. Five specs were formally accepted (M2-01, M2-02, M2-03, M2-07, M2-10); the rest reached varying degrees of draft. See the README inside for the per-spec disposition into pillars. |
| [`milestone-3-polish/`](milestone-3-polish/) | Archived (2026-05-07) | The original "polish for adoption" milestone, restructured into Pillars 3-4 plus a future UI milestone. Most specs map cleanly into pillar territory; see the README inside for the per-spec disposition. |

## Retired top-level docs

Superseded top-level planning/spec docs, kept for historical trace:

- [`PROJECT_PLAN-pillars.md`](PROJECT_PLAN-pillars.md) — the pillar-based delivery plan (P1–P4 + hosted product), superseded for sequencing by [`../DELIVERY.md`](../DELIVERY.md) and retired 2026-06-18 under the [three-tier spec structure](../_strategy/2026-06-spec-structure.md); its durable "shape of Carve" framing folds into PRD/ARCHITECTURE.
- [`PROJECT_PLAN-pre-2026-05-positioning.md`](PROJECT_PLAN-pre-2026-05-positioning.md), [`PRD-pre-2026-05-positioning.md`](PRD-pre-2026-05-positioning.md), [`ARCHITECTURE-pre-2026-05-positioning.md`](ARCHITECTURE-pre-2026-05-positioning.md) — the pre-positioning originals, archived when the 2026-05 positioning rewrite landed.

## Why archive instead of delete

Three reasons:

1. **Lineage transparency.** Each pillar spec carries a `Lineage` field naming its M2/M3 ancestors. Those references need somewhere to point.
2. **Source material for later pillars.** Pillars 2-4 are planned but not yet specced. The dbt agent (M2-04), dbt integration (M2-06), convention inference (M2-08), and so on are draft material future pillars will draw from.
3. **Honest history.** Carve's design evolved through several conversations and a major restructure. Keeping the archive lets a future contributor read the discussion that produced the current shape.

## What's NOT archived

- [`milestone-1-walking-skeleton/`](../milestone-1-walking-skeleton/) — M1 shipped; the implementation is in `src/`.
- [`milestone-1.1-followups/`](../milestone-1.1-followups/) — M1.1 shipped; same.

These remain as living spec directories for the M1 / M1.1 work that's already in code.

## Working with the archive

- **When citing an M2/M3 spec from a pillar spec's lineage:** use the `_archive/` path. The pillar specs were updated to use these paths after the move.
- **When drafting a new pillar spec:** if you find a relevant ancestor in the archive, add it to the `Lineage` field with an `_archive/` link. Don't move content out of the archive — copy what carries forward into the new spec, with the lineage tag making the trace explicit.
- **When an archived spec stops being useful:** leave it. The archive grows; it doesn't shrink. If clutter becomes a real problem, we can split into per-version subdirectories later.
