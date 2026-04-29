---
name: build-milestone
description: Build every spec in a milestone in dependency order, by chaining `/build-spec` over the topologically sorted spec list. Use this skill to walk a whole milestone (M1, M2, or M3) end-to-end. Argument is a milestone ID. Produces working code, tests, verification reports, and updated specs for every spec in the milestone, plus a milestone-level summary.
---

# /build-milestone

Builds every spec in a milestone in dependency order. Each spec is built via `/build-spec`, with the milestone-level orchestration providing topological sorting, progress reporting, and stop-on-failure semantics.

## Argument

A milestone ID: `M1`, `M2`, or `M3`.

## Process

### 1. Resolve the milestone

Map the argument to a directory:

- `M1` → `specs/milestone-1-walking-skeleton/`
- `M2` → `specs/milestone-2-real-product/`
- `M3` → `specs/milestone-3-polish/`

If the argument doesn't match, abort with a clear error.

### 2. Read the milestone README

Open `specs/{milestone-dir}/README.md` and extract:
- Milestone title and goal
- Acceptance criteria
- The recommended build order from the "Spec list" section, if present

The README's spec list is the human-curated build order. Use it as the primary ordering signal.

### 3. Topologically sort the specs

For every spec file in the milestone directory (excluding the README):

a. Parse the `Dependencies:` line from the header. Extract the list of dep IDs.
b. Build a directed graph: spec → its dependencies.
c. Topologically sort.
d. If a dependency cycle is detected, abort with a clear error naming the cycle.
e. If the README's order conflicts with the topological sort (e.g. a dep is listed before its dependent), prefer the topological sort and note the divergence in the run log.

The result is a list of spec IDs in build order: `[M1-01, M1-02, M1-03, M1-04, M1-05, M1-06]` or similar.

### 4. Build each spec

For each spec ID in order:

a. Print a banner: `=== Starting {spec-id}: {spec title} ({i}/{n}) ===`
b. Invoke `/build-spec` with the spec ID.
c. Capture the result (PASS, FAIL, or ABORTED).
d. If FAIL or ABORTED:
   - Stop the milestone build.
   - Print a summary up to this point.
   - Surface the failure with enough detail for the user to decide whether to fix and retry, skip the failed spec and continue manually, or roll back.
   - Do **not** proceed to the next spec.
e. If PASS, continue to the next spec.

### 5. Print the milestone summary

After all specs complete (or one failed):

```
=== /build-milestone {milestone-id} {complete | stopped at {spec-id}} ===

Milestone: {milestone title}
Specs completed: {n} / {total}
Total iterations: {sum across all specs}

Per-spec results:
  M1-01: PASS (1 iteration)
  M1-02: PASS (2 iterations)
  M1-03: PASS (1 iteration)
  M1-04: FAIL — fix budget exhausted on iteration 3 (qa-verifier kept failing)
  M1-05: not started
  M1-06: not started

Files changed across milestone:
  {summary of git diff stats}

Next:
  {if complete: review the milestone, run integration tests, commit, tag}
  {if stopped: address {spec-id} failure, then re-run /build-milestone {milestone-id} or resume with /build-spec {next-spec-id}}
```

### 6. Tagging (manual, not automated)

`/build-milestone` does **not** create git tags. After M1 completes, the project plan calls for `v0.0.1`; after M2, `v0.0.5`; after M3, `v0.1.0`. The user creates those tags by hand after reviewing the milestone work.

## Constraints

- **One spec at a time.** No parallelism across specs within a milestone — the dependency graph guarantees one was supposed to be done before the next, and parallelizing breaks that contract.
- **Stop-on-failure by default.** A failed spec stops the whole milestone. The user can resume with `/build-spec` on the next-up spec after they've fixed the broken one.
- **Respect the dependency graph.** Topological order is non-negotiable. The README's order is a hint; the dependency graph is the rule.
- **Don't push to remote.** Same as `/build-spec`.
