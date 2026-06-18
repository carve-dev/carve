---
name: build-spec
description: Build a Carve capability end-to-end — resolve it within its DELIVERY increment, dependency-check, generate the delivery spec (the delta-aware build manifest), engineer, parallel review, fix iterations, spec keeping. The primary entry point for implementing any capability in `specs/capabilities/`. Argument is a capability name like `runtime`, optionally `runtime@"Increment 3"`, or a path to a capability spec.
---

# /build-spec

The primary entry point for building a Carve capability. It orchestrates the full loop: resolve the capability within its delivery increment, verify dependencies, **generate the delivery spec** (the delta-aware build manifest — computed at build time, not stored), specialist engineering, parallel review, fix iteration (up to 3 rounds), and post-build spec keeping.

This implements the model in [`specs/_strategy/2026-06-spec-structure.md`](../../../specs/_strategy/2026-06-spec-structure.md): **capability specs are durable design, `DELIVERY.md` is the temporal plan, and the work order — the "delivery spec" — is generated here at build time** by evaluating the capability spec within its increment against the current codebase.

## Argument

- A **capability name** like `runtime`, `harness`, `dlt-engineer` (→ `specs/capabilities/{name}.md`), **or**
- A capability scoped to an increment: `runtime@"Increment 3"` (when a capability appears in more than one increment), **or**
- A **path** to a capability spec like `specs/capabilities/runtime.md`.

(The historical `M1-*`/`M1.1-*` milestone specs are *shipped* and the `_archive/` specs are retired — they are not built through this skill. To re-touch one, pass its path directly; resolution falls back to the path form.)

## Process

### 1. Resolve the capability + its increment

- If the argument is a path that exists, use it; the capability name is the file stem.
- Otherwise treat it as a capability name → `specs/capabilities/{name}.md`. If it doesn't exist, abort with a clear error (list the capabilities in `specs/capabilities/`).
- Open `specs/DELIVERY.md` and find the **increment** whose *In scope* lists this capability. If an `@"Increment N"` qualifier was given, use it; if the capability appears in exactly one increment, use that; if in several and none was specified, abort and ask which.

Read the capability spec. Extract:
- The title and the `Depends on:` line from the Status block.
- The matching increment's **Delta** + **Exit criteria** (for the delivery-spec generator).
- The full content for downstream agents to re-read.

**Do not** look for a "Files this spec produces" section — capability specs don't have one. The file manifest is generated in step 3.

### 1b. Classify the change (the spec-first gate)

Before building, classify this run per the [change-lifecycle ADR](../../../specs/_strategy/2026-06-change-lifecycle.md): an **initial build**, a **bug** (code diverges from a *correct* spec → the build adds a regression test + the fix; the capability spec body is untouched), or a **change** (the desired behavior differs from the spec → the capability spec must be updated **first**). For a change whose spec still describes the old behavior, **stop and update the spec before continuing** — `task-planner` enforces this gate, returning `SPEC-FIRST REQUIRED` instead of a manifest. Bugs and small enhancements are tracked as GitHub issues; new capabilities / large changes get a `DELIVERY.md` backlog entry.

### 2. Check dependencies

Invoke the `dependency-checker` agent with the capability + its increment.

- It verifies the capability spec's declared `Depends on:` and the increment's `Depends on:` are actually implemented in `src/`. Wait for `.carve-build/dependencies/{capability}.md`.
- If `BLOCKED`, **abort the build.** Print which dependencies are unsatisfied and which `/build-spec` runs (or already-shipped M1/M1.1 baseline) would resolve them. Do not proceed.
- If `ALL_SATISFIED`, continue.

### 3. Generate the delivery spec

Invoke the `task-planner` agent in **generate mode** with the capability + increment.

- It reads the capability spec (design) + the DELIVERY increment (scope/delta) + **inspects the current codebase**, and writes the **delivery spec** to `.carve-build/delivery-specs/{capability}.md`: the delta-aware build manifest (CREATE/MODIFY per file, grounded in what's already on disk), plus the Acceptance + Tests slice as the bar.
- Wait for that file. This is the engineer's working set — and the thing that makes the build delta-aware ("recognize what's already built").

### 4. Route to an engineer

Read the **Build manifest** in `.carve-build/delivery-specs/{capability}.md` (the *generated* file list — not the spec) and apply this routing table:

| Manifest characteristic | Engineer |
|---|---|
| Files dominated by `src/carve/**/*.py` (no `.sql`, no `_schema.yml`, no `web/`) | `python-engineer` |
| Files include `.sql` and/or `_schema.yml`, and dbt-related Python | `dbt-engineer` |
| Files dominated by the Snowflake connector / generated DDL | `snowflake-engineer` |
| Files dominated by agent definitions, prompts, or `src/carve/**/skills/**` | `agent-author` |
| Files dominated by `web/**` or `src/carve/ui/**` (`.tsx`, `.ts`, `.css`) | `web-engineer` |
| Mixed | route by *primary* output; if genuinely split, do the larger half first with its specialist, then the other half (sequential) |
| None of the above | `engineer` (generic fallback) |

(UI-shaped capabilities like `ui` route to `web-engineer` even if the manifest looks mixed.)

### 5. Engineer implements

Invoke the chosen engineer with:
- The capability name.
- The path to the delivery spec (`.carve-build/delivery-specs/{capability}.md`) — its working set, including the CREATE/MODIFY manifest.
- An instruction to follow that agent's documented process, and to honor the MODIFY tags (extend existing code; don't re-create shipped modules).

Wait for the engineer to report complete. The handoff includes gate results (`ruff`, `mypy`, `pytest`) and a manifest audit.

### 6. Run reviewers in parallel

Based on what files actually changed (from git):

- **Always:** `qa-verifier` (verifies the delivery spec's *Tests required*, sourced from the capability spec), `security-reviewer`
- **If any `src/carve/**/*.py` or `tests/**/*.py` changed:** `python-reviewer`
- **If any `.sql`, `_schema.yml`, or `src/carve/dbt/**` changed:** `dbt-reviewer`
- **If any `src/carve/core/agents/**`, `src/carve/**/skills/**`, agent definitions, or anything importing `anthropic` changed:** `agent-loop-reviewer`
- **If a UI screen changed and the local stack runs:** `manual-tester`

Invoke the applicable reviewers in parallel. Each writes `.carve-build/verification/{role}-{report}-{capability}.md` and returns PASS/FAIL.

### 7. Fix iteration loop

If any reviewer returned FAIL:

a. Increment the iteration counter (from 1).
b. If iteration > 3, **abort** — print the failures and stop. Three iterations is the budget; more is a human's call.
c. Invoke `task-planner` in **fix mode** with the capability, the iteration number, and the verification reports.
d. Wait for `.carve-build/fixes/{capability}-iter{n}.md`.
e. Re-invoke the same engineer with the fix plan.
f. Re-run the applicable reviewers (step 6).
g. Loop to (a).

### 8. Keep the spec honest

When all reviewers PASS:

- Invoke `spec-keeper` with the capability. It reconciles the **capability spec** (`specs/capabilities/{name}.md`) against the code — applying minor inline design updates or writing a major-drift proposal beside it.
- **It must not re-introduce a "Files this spec produces" section** (the manifest is build-time-generated, by design). Drift it cares about is *design* drift (behavior, interfaces, acceptance), not the file list.
- It may note completion in `DELIVERY.md`'s *Current state* (the delta baseline) so the next build sees this capability as shipped.

### 9. Print the handoff summary

```
=== /build-spec {capability} ({increment}) complete ===

Capability: {title}
Engineer: {chosen specialist}
Iterations: {n}

Files changed:
  {list from git status, after the run}

Verification:
  qa-verifier: PASS ({n} tests)
  security-reviewer: PASS ({n} findings, all informational)
  {role}-reviewer: PASS

Spec drift: {none | minor inline updates applied | major proposal written to <path>}

Next:
  - Review the changes (`git diff`)
  - Run any integration tests not run as part of the build
  - Commit when satisfied
```

## Constraints

- **Spec-first for changes.** A behavior change is reflected in the capability spec *before* it's built (the [change-lifecycle ADR](../../../specs/_strategy/2026-06-change-lifecycle.md)); a bug fix adds a regression test and leaves the spec body alone. `task-planner` gates this.
- **Sequential phases, parallel within a phase.** Steps 1–5 sequential; step 6 (reviewers) parallel; fix iterations sequential.
- **The delivery spec is the unit of work.** It's generated per run from (capability spec × increment × current code) and lives only under `.carve-build/`. Never store a file manifest back into `specs/`.
- **Never modify `specs/` except via `spec-keeper`.** Engineers and reviewers write only to `.carve-build/` (transient) and to source directories (code).
- **Abort cleanly.** Any blocker — failed dependency check, exhausted fix budget, unresolvable capability/increment — produces a clear error and exits without leaving the workspace in an unknown state.
- **Don't push to remote.** Commit to the local tree (or not, per user preference); pushing is the user's call.
