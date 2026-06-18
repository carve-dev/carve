# 2026-06 — Specs define capability; a delivery plan defines what to build, when

> **Status:** Decided 2026-06-17 (Nate). Foundational structure/process decision. Separates the **durable design corpus** (what Carve is and how it works) from the **temporal delivery plan** (what we build next, given what's already built). Refines how [`../PROJECT_PLAN.md`](../PROJECT_PLAN.md) and the `capabilities/` spec set are organized.

## The problem

Our specs do two jobs at once: they describe a **capability** (the durable design — behavior, contracts, data model) *and* they act as a **work order** (what to build, scoped to a version: `v0.1/NN-…`). Conflating those entangles the design with the delivery phase, and that entanglement is the source of three recurring failures:

- **Capabilities fractured by version.** dbt is split "manifest-reading is v0.1, authoring is v0.2," so lineage had to reach across a version silo to use the v0.1 half. The capability is one thing; the phasing cut isn't part of its design.
- **Gaps between design and delivery.** CLI commands like `run --watch` / `auth login` are referenced in the design but have no "v0.1 spec body," so they fall into a no-man's-land — defined as capability, unowned as work.
- **Same-truth drift.** The 05↔03 `carve.toml` contradiction happened because two specs encoding the same truth evolved at different phases. One living capability spec can't contradict itself.

The `capabilities/` directory *is* the silo. Version identity baked into a spec's path/ID is the root cause.

## The decision

**Three tiers. Version/phase lives only in the third.**

1. **Product + Architecture — durable.** [`PRD.md`](../PRD.md) (what / why / who) and [`ARCHITECTURE.md`](../ARCHITECTURE.md) (the technical model). Version-independent, living.
2. **Capability specs — durable; the lowest level of *design* detail.** One per capability *area* (runtime, harness, deploy, lineage, SQL…), organized by **domain, not version**. Each describes the whole capability — behavior, contracts, data model, interfaces, decisions — with **phasing as annotations** ("the dbt engineer arrives in a later increment"), never as separate v0.1/v0.2 files. There is exactly one place to read "how deploy works," and you edit it in place as deploy evolves.
3. **Delivery plan — temporal; the artifact we were missing.** [`DELIVERY.md`](../DELIVERY.md): dependency-aware, foundation-first, and **delta-aware** (it knows M1/M1.1 shipped, so it plans *changes + additions*, not from-scratch builds). Increment-structured. Each increment = an ordered set of *slices* across capability specs, with concrete build instructions + exit criteria for that increment. This — not the spec — carries "v0.1 / v0.2 / increment N."

Tiers 1–2 answer **"what is Carve and how does it work."** Tier 3 answers **"what do we build next, given what's already built."**

## Why

- **It kills the silo.** Each capability has *one* spec, so it can't be duplicated across versions or fractured into v0.1/v0.2 halves. Cross-capability dependencies are explicit (the spec's "depends on" + the delivery plan's sequencing) rather than implied by a directory.
- **It matches how the product actually grows.** A reader — human or agent — asking "how does scheduling work?" should get the complete, current truth in one place, not a union of v0.1 + v0.2 + v0.3 fragments. Phase only matters when deciding what to build next.
- **It makes delivery honest about the codebase.** A delta-aware delivery plan plans "modify what M1 built, add Y," instead of pretending every spec is a greenfield unit (which "Files this spec produces" implicitly does today).

## What a "spec" becomes

A capability spec stops being a work order. The clearest work-order part — **"Files this spec produces"** (literally *what to build*) — moves into the delivery plan's increment items (where it can be delta-aware: "modify X, add Y"). The spec keeps the **durable design**: behavior, contracts, data model, interfaces, design decisions, phasing annotations — **and its Acceptance + Tests**, which are the durable definition of "correct" for the capability (the delivery increment points *at* them as the bar, rather than re-stating them).

## The `/build-spec` implication (decide deliberately)

Today `/build-spec` consumes *a spec* as a unit of work. Under this model it consumes *a delivery-plan increment* (a slice of one or more capability specs), with the capability spec as the design reference. This is a real change to the build workflow — it is acknowledged here and will be worked out as the delivery plan and the first increment are built; it is **not** changed implicitly.

## Target structure

```
specs/
  PRD.md · ARCHITECTURE.md          # tier 1 — durable
  capabilities/                     # tier 2 — durable, un-versioned
    runtime.md harness.md deploy.md lineage.md sql.md …
  DELIVERY.md                       # tier 3 — temporal: current state + increments
  _strategy/                        # ADRs (durable decisions; this file)
  reference/                        # cli / config / glossary / governance (durable)
```

## Migration — staged, not big-bang

1. **This ADR** — capture the decision + target structure. ✅ Done.
2. **Stand up `DELIVERY.md`** — port the build order into dependency-ordered, delta-aware increments. ✅ Done.
3. **Migrate specs** `v0.1/NN` → `capabilities/<area>`, in two parts:
   - **3a — structural.** ✅ Done (2026-06-17). All 19 specs moved to [`../capabilities/<area>.md`](../capabilities/); version identity stripped from filenames, titles, and cross-references corpus-wide; the build-order README replaced by a capability index; the landed M1/M1.1 follow-ups archived.
   - **3b — content.** Remaining: lift each spec's **"Files this spec produces"** section into the delivery increment that builds it, and reframe any leftover version-as-scope language as phasing annotations. Capability-by-capability; non-structural polish. (Acceptance + Tests stay in the spec — see *What a "spec" becomes*.)

`DELIVERY.md` is the source of truth for sequencing and scope. [`PROJECT_PLAN.md`](../PROJECT_PLAN.md) is superseded as a build plan by `DELIVERY.md` (its durable "shape of Carve" framing folds into PRD/ARCHITECTURE).

## Impact

- **`DELIVERY.md`** is created as the live delivery plan (step 2).
- **`PROJECT_PLAN.md`** becomes a static precursor — its sequencing role moves to `DELIVERY.md`; keep until the migration settles, then retire/fold.
- **`capabilities/README.md`** is now a capability index (the build-order table moved to `DELIVERY.md`'s increments).
- **Capability specs** lost their version prefix (step 3a). Lifting the "Files this spec produces" sections into delivery increments is step 3b (remaining); Acceptance + Tests stay in the spec as the durable verification bar.
