# 2026-06 — The change lifecycle: bugs, enhancements, and new capabilities

> **Status:** Decided 2026-06-18 (Nate). Foundational process decision. Builds on [`2026-06-spec-structure.md`](./2026-06-spec-structure.md) (the three tiers): that ADR defined how the **initial** build works; this one defines how **change** works — during the build and forever after. Applies to building Carve itself.

## The problem

The three-tier model — durable [`PRD.md`](../PRD.md)/[`ARCHITECTURE.md`](../ARCHITECTURE.md) → durable capability specs → temporal [`DELIVERY.md`](../DELIVERY.md), with the delivery spec generated at build time — was articulated for the initial, foundation-first build. But software is never done, and two questions had no written answer:

- **During the build:** Foundation ships; while playing with it we find bugs and decide a few things should work differently. How is that handled without derailing the increment sequence?
- **After the build:** Everything's shipped; we want to add a capability, or fix/change an existing one. What's the flow — does the spec get updated, and does some "bug/enhancement delivery spec" get created?

Without a stated rule, every change is an ad-hoc decision about what to edit and in what order — exactly the design/delivery entanglement the three-tier split was meant to remove.

## The decision

Every change is classified by **one question — "Is the capability spec still correct?"** — and the answer picks the flow. Two invariants from [the spec-structure ADR](./2026-06-spec-structure.md) carry the weight: the **capability spec is always the current design truth** (edited in place as the capability evolves), and the **delivery spec is generated at build time, never stored**.

### The decision rule

- **Spec is correct → it's a BUG.** The spec already describes the right behavior; the code deviates. Flow: reproduce → add a regression test to the spec's **Tests** → fix the code to match → green. The spec's design body is untouched. **Bugs grow the Tests.**
- **Spec is wrong or incomplete → it's a CHANGE** (enhancement, correction, or new capability). The durable design itself moves. Flow: **update the spec first** — design body + Acceptance + Tests (or write a *new* capability spec; touch PRD/ARCHITECTURE if the product/architecture story shifts) → then build. **Changes move the design.** This is the **spec-first** rule: *no behavior change is built against a stale spec.*

That rule is the whole answer to "does the spec get updated?": for a real change, **yes — spec-first**; for a pure bug, **no** — the spec was already right (it only gains a test).

### The taxonomy

| Change | Spec? | Tracked as | Build | Release |
|---|---|---|---|---|
| **Bug** | regression test only | GitHub issue | `/build-spec <cap>` | patch |
| **Small enhancement** | update the capability spec | GitHub issue | `/build-spec <cap>` | minor |
| **New capability / large change** | new or updated spec (+ PRD/ARCH if needed) | **`DELIVERY.md` entry** (it needs sequencing) | `/build-spec` or `/build-increment` | minor / major |

**Grain rule:** small, independent work rides a GitHub issue + the spec edit; only work that needs **sequencing or slicing** earns a `DELIVERY.md` entry. Releases follow SemVer — bug → patch, enhancement → minor, breaking → major (see [`../reference/governance.md`](../reference/governance.md)).

### There is no stored "bug/enhancement delivery spec"

A change does **not** get its own saved manifest. `/build-spec` *regenerates* the delta-aware delivery spec from the (now-updated) capability spec × current code — the same generator used for an initial increment slice, just a smaller delta. The persistent record of a change is **the spec edit + the PR + (on release) the changelog/tag**. Storing a per-change manifest would reintroduce exactly the staleness the generated delivery spec was created to avoid.

## During the build vs. after the build — one mechanism

Both reduce to the same move, because **every build plans the delta from `DELIVERY.md`'s *Current state* baseline**:

- **During** (a bug/change in shipped foundation while building a later increment): handle it as an ordinary delta build, interleaved with forward work — no need to wait for "done." A code bug → re-run `/build-spec` on that capability (it inspects what's there and plans only the fix). A design change → edit the spec, then `/build-spec`. Bump *Current state*.
- **After** (everything shipped; add or change a capability): identical flow. The only difference is that the initial increments are now the historical **build log**, and ongoing work lives in the **Backlog**.

So `DELIVERY.md` is a **living** document, not a one-shot plan: **Current state** (the perpetual delta baseline) + **Increments** (the initial build → build log) + **Backlog** (ongoing change that needs sequencing).

## How this is enforced

Stated rules drift; these wire it in:

1. **The build harness executes it.** `task-planner` applies the spec-first gate when generating a manifest for an already-shipped capability (bug → regression test + code fix; change → refuse to plan until the spec reflects the new design). `spec-keeper` reinforces it post-build (bugs must have grown the Tests; undocumented behavior in the code is major drift). `/build-spec` makes the bug-vs-change classification step one of its loop.
2. **A Stop-hook tripwire** (`.claude/hooks/check-no-version-vocab.sh`) guards the phase-free corpus (`capabilities/`, PRD, ARCHITECTURE, use-cases) against version/phase vocabulary re-entering — the one invariant a script can check deterministically.
3. **Governance** maps change-type → SemVer and routes substantive / default-behavior changes through the RFC process ([`../reference/governance.md`](../reference/governance.md)).

## Impact

- **`DELIVERY.md`** gains its living framing (Current state · Increments/build-log · Backlog) and a pointer here.
- **`reference/governance.md`** gains the change-type → SemVer mapping.
- **`task-planner` / `spec-keeper` / `build-spec`** encode the decision rule + the spec-first gate.
- **No new tracking artifact** is introduced — issues + the spec + the PR are the record; the delivery spec stays generated.
