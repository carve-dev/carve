# 2026-06 — Specs define capability; a delivery plan defines what to build, when

> **Status:** Decided 2026-06-17 (Nate). Foundational structure/process decision. Separates the **durable design corpus** (what Carve is and how it works) from the **temporal delivery plan** (what we build next, given what's already built). Refines how the prior `PROJECT_PLAN.md` (now retired to [`_archive/`](../_archive/PROJECT_PLAN-pillars.md)) and the `capabilities/` spec set are organized.

## The problem

Our specs do two jobs at once: they describe a **capability** (the durable design — behavior, contracts, data model) *and* they act as a **work order** (what to build, scoped to a version: `<version>/NN-…`). Conflating those entangles the design with the delivery phase, and that entanglement is the source of three recurring failures:

- **Capabilities fractured by version.** dbt is split "manifest-reading is one version, authoring a later one," so lineage had to reach across a version silo to use the earlier half. The capability is one thing; the phasing cut isn't part of its design.
- **Gaps between design and delivery.** CLI commands like `run --watch` / `auth login` are referenced in the design but have no per-version spec body, so they fall into a no-man's-land — defined as capability, unowned as work.
- **Same-truth drift.** The 05↔03 `carve.toml` contradiction happened because two specs encoding the same truth evolved at different phases. One living capability spec can't contradict itself.

The `capabilities/` directory *is* the silo. Version identity baked into a spec's path/ID is the root cause.

## The decision

**Three tiers. Version/phase lives only in the third.**

1. **Product + Architecture — durable.** [`PRD.md`](../PRD.md) (what / why / who) and [`ARCHITECTURE.md`](../ARCHITECTURE.md) (the technical model). Version-independent, living.
2. **Capability specs — durable; the lowest level of *design* detail.** One per capability *area* (runtime, harness, deploy, lineage, SQL…), organized by **domain, not version**. Each describes the whole capability — behavior, contracts, data model, interfaces, decisions — with **phasing as annotations** ("the dbt engineer arrives in a later increment"), never as separate per-version files. There is exactly one place to read "how deploy works," and you edit it in place as deploy evolves.
3. **Delivery plan — temporal; the artifact we were missing.** [`DELIVERY.md`](../DELIVERY.md): dependency-aware, foundation-first, and **delta-aware** (it knows M1/M1.1 shipped, so it plans *changes + additions*, not from-scratch builds). Increment-structured. Each increment = an ordered set of *slices* across capability specs, with concrete build instructions + exit criteria for that increment. This — not the spec — carries the increment identity ("increment N", plus the release tag at the end).

Tiers 1–2 answer **"what is Carve and how does it work."** Tier 3 answers **"what do we build next, given what's already built."**

## Why

- **It kills the silo.** Each capability has *one* spec, so it can't be duplicated across versions or fractured into per-version halves. Cross-capability dependencies are explicit (the spec's "depends on" + the delivery plan's sequencing) rather than implied by a directory.
- **It matches how the product actually grows.** A reader — human or agent — asking "how does scheduling work?" should get the complete, current truth in one place, not a union of per-version fragments. Phase only matters when deciding what to build next.
- **It makes delivery honest about the codebase.** A delta-aware delivery plan plans "modify what M1 built, add Y," instead of pretending every spec is a greenfield unit (which "Files this spec produces" implicitly does today).

## What a "spec" becomes

A capability spec stops being a work order. The clearest work-order part — **"Files this spec produces"** (literally *what to build*) — is **removed from the design corpus entirely**: it is regenerated at build time (see *The delivery spec*, below), so it is never stored stale. The spec keeps the **durable design**: behavior, contracts, data model, interfaces, design decisions, phasing annotations — **and its Acceptance + Tests**, which are the durable definition of "correct" for the capability (the generated delivery spec points *at* them as the bar, rather than re-stating them).

## The delivery spec — generated at build time

The work order is **computed, not stored.** When the build runs a capability slice within a delivery increment, it **generates a *delivery spec*** by evaluating the capability spec (the design) against (a) the increment's scope + sequencing in `DELIVERY.md` and (b) the **current codebase**. That generated delivery spec is the concrete, **delta-aware** work order: the file manifest (*create X, modify Y*) wired to the current state, plus the increment's slice of the spec's Acceptance + Tests as the bar.

This is *why* no file manifest lives statically in either the capability spec or `DELIVERY.md`: a stored manifest goes stale against the code; a generated one is correct by construction — it reads what's already built and plans only the changes + additions ("recognize what has already been built"). Concretely, this is the role of `/build-spec`'s planning stage — it evolves from "consume a whole spec" to **"consume a delivery increment → read the capability spec as the design reference → inspect the code → emit the delivery spec the engineer builds from."** The capability spec and `DELIVERY.md` are durable inputs; the delivery spec is an ephemeral build artifact.

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
3. **Migrate specs** `<version>/NN` → `capabilities/<area>`, in two parts:
   - **3a — structural.** ✅ Done (2026-06-17). All 19 specs moved to [`../capabilities/<area>.md`](../capabilities/); version identity stripped from filenames, titles, and cross-references corpus-wide; the build-order README replaced by a capability index; the landed M1/M1.1 follow-ups archived.
   - **3b — de-work-order the specs.** ✅ Done (2026-06-17). The static **"Files this spec produces"** section was **removed** from all 19 specs (not moved to `DELIVERY.md`) — the file manifest is now generated at build time per *The delivery spec*. Acceptance + Tests stay in the spec as the durable bar.

`DELIVERY.md` is the source of truth for sequencing and scope. `PROJECT_PLAN.md` is superseded as a build plan by `DELIVERY.md` and has been retired to [`_archive/`](../_archive/PROJECT_PLAN-pillars.md) (its durable "shape of Carve" framing folds into PRD/ARCHITECTURE).

## Impact

- **`DELIVERY.md`** is created as the live delivery plan (step 2).
- **`PROJECT_PLAN.md`** was a static precursor — its sequencing role moved to `DELIVERY.md`; **retired to [`_archive/`](../_archive/PROJECT_PLAN-pillars.md) (2026-06-18)** now that the migration has settled.
- **`capabilities/README.md`** is now a capability index (the build-order table moved to `DELIVERY.md`'s increments).
- **Capability specs** lost their version prefix (3a) and their static "Files this spec produces" section (3b) — the file manifest is generated at build time (*The delivery spec*). Acceptance + Tests stay as the durable verification bar.
