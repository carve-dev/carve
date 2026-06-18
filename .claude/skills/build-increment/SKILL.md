---
name: build-increment
description: Build every capability in a DELIVERY increment in dependency order, by chaining `/build-spec` over the increment's topologically sorted capability list. Use this skill to walk a whole increment (e.g. "Increment 1 — Foundation") end-to-end. Argument is an increment identifier. Produces working code, tests, verification reports, and updated specs for every capability in the increment, plus an increment-level summary.
---

# /build-increment

Builds every capability in a [`specs/DELIVERY.md`](../../../specs/DELIVERY.md) increment in dependency order. Each capability is built via `/build-spec`, with increment-level orchestration providing topological sorting, progress reporting, and stop-on-failure semantics.

## Argument

An increment identifier: a number (`1`), or its title (`Foundation`, `"Increment 1"`). Increments are defined in `DELIVERY.md`.

## Process

### 1. Resolve the increment

Open `specs/DELIVERY.md` and find the increment matching the argument (by number or title). Extract:
- The increment's title and **Goal**.
- Its **In scope** — the list of capabilities (each links to `specs/capabilities/<name>.md`).
- Its **Depends on** (prior increments / shipped baseline) and **Exit criteria**.

If the argument matches no increment, abort and list the increments in `DELIVERY.md`.

### 2. Check the increment's upstream dependencies

Confirm the increment's `Depends on:` (earlier increments, or the M1/M1.1 baseline) is satisfied — spot-check via the `dependency-checker` mindset (is the prior increment's code in `src/`?). If a whole upstream increment is unbuilt, abort and point the user at it (`/build-increment {prior}`).

### 3. Topologically sort the capabilities

For each capability in the increment's *In scope*:

a. Read `specs/capabilities/<name>.md`, parse its `Depends on:` line.
b. Build a directed graph over **the capabilities in this increment** (dependencies on already-shipped capabilities/baseline are pre-satisfied, not nodes).
c. Topologically sort. On a cycle, abort naming it.
d. If the increment's *In scope* order conflicts with the topological sort, prefer the sort and note the divergence in the run log.

The result is the capability build order, e.g. `[layout, harness, extensibility]` for the Foundation increment.

### 4. Build each capability

For each capability in order:

a. Print a banner: `=== Starting {capability}: {title} ({i}/{n}) ===`
b. Invoke `/build-spec` with `{capability}@"{increment title}"` (so the delivery-spec generator scopes to this increment).
c. Capture the result (PASS, FAIL, or ABORTED).
d. If FAIL or ABORTED: **stop the increment build**, print the summary so far, and surface the failure with enough detail for the user to fix-and-retry, skip-and-continue manually, or roll back. Do **not** proceed.
e. If PASS, continue.

### 5. Print the increment summary

```
=== /build-increment {increment} {complete | stopped at {capability}} ===

Increment: {title}
Capabilities completed: {n} / {total}
Total iterations: {sum}

Per-capability results:
  layout:        PASS (1 iteration)
  harness:       PASS (2 iterations)
  extensibility: FAIL — fix budget exhausted on iteration 3
  ...

Files changed across the increment:
  {git diff stat summary}

Exit criteria ({from DELIVERY}):
  {check each increment exit criterion against what was built — met / not met}

Next:
  {if complete: review, run integration tests, commit; if this was the last increment, the initial-release tag criteria in DELIVERY apply}
  {if stopped: address {capability}, then re-run /build-increment {increment} or resume with /build-spec {next-capability}}
```

### 6. Tagging (manual, not automated)

`/build-increment` does **not** create git tags. The initial-release tag criteria live in `DELIVERY.md` (Increment 6 — *carve init → plan → build → run → deploy → scheduled-run works end-to-end against real Snowflake, via REST + MCP*). The user tags by hand after reviewing.

## Constraints

- **One capability at a time.** No parallelism across capabilities within an increment — the dependency graph guarantees ordering, and parallelizing breaks that contract.
- **Stop-on-failure by default.** A failed capability stops the increment; resume with `/build-spec` on the next-up capability after fixing the broken one.
- **Respect the dependency graph.** Topological order is non-negotiable. The increment's *In scope* order is a hint; the capability `Depends on:` graph is the rule.
- **Delta-aware throughout.** Each `/build-spec` generates its delivery spec against the *current* tree — so capabilities built earlier in this increment are visible (as shipped) to the ones built later.
- **Don't push to remote.** Same as `/build-spec`.
