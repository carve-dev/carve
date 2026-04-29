---
name: build-spec
description: Build a single Carve spec end-to-end — dependency check, phase planning, engineering, parallel review, fix iterations, spec keeping. Use this skill as the primary entry point for implementing any spec in `specs/`. Argument is a spec ID like `M1-04` or a path to a spec file. Produces working code, tests, verification reports, and an updated spec.
---

# /build-spec

The primary entry point for building a Carve spec. This skill orchestrates the full implementation loop: dependency verification, phase planning, specialist engineering, parallel review, fix iteration (up to 3 rounds), and post-implementation spec keeping.

## Argument

- A spec ID like `M1-04`, `M2-07`, `M3-12`, **or**
- A path to a spec file like `specs/milestone-1-walking-skeleton/04-anthropic-agent-loop.md`

## Process

### 1. Resolve the spec

Map the argument to a file path:

- If the argument is a path that exists, use it directly.
- If the argument is an ID matching `M(\d+)-(\d+)`:
  - Find the milestone directory by leading number: `M1` → `specs/milestone-1-walking-skeleton/`, `M2` → `specs/milestone-2-real-product/`, `M3` → `specs/milestone-3-polish/`.
  - Find the spec file by leading number: `M1-04` → `specs/milestone-1-walking-skeleton/04-*.md`.
- If neither resolves, abort with a clear error.

Read the resolved file. Extract:
- The spec title
- The `Dependencies:` line from the header
- The "Files this spec produces" section
- The full content for downstream agents to re-read

### 2. Check dependencies

Invoke the `dependency-checker` agent with the resolved spec ID.

- Wait for the report at `.carve-build/dependencies/{spec-id}.md`.
- If status is `BLOCKED`, **abort the build**. Print a summary of the unsatisfied dependencies and which `/build-spec` runs would resolve them. Do not proceed to step 3.
- If `ALL_SATISFIED`, continue.

### 3. Plan the phase

Invoke the `task-planner` agent in spec-to-phase mode with the spec ID.

- Wait for the phase file at `.carve-build/phases/{spec-id}.md`.
- This is the engineer's working set.

### 4. Route to an engineer

Choose the engineer specialist by reading the spec's "Files this spec produces" section and applying this routing table:

| Spec characteristic | Engineer |
|---|---|
| Files dominated by `src/carve/**/*.py` (no `.sql`, no `_schema.yml`, no `web/`) | `python-engineer` |
| Files include `.sql` and/or `_schema.yml`, and dbt-related Python | `dbt-engineer` |
| Files dominated by `src/carve/connectors/snowflake/**` or generated DDL | `snowflake-engineer` |
| Files dominated by agent TOML, agent prompts, or `src/carve/skills/**` | `agent-author` |
| Files dominated by `web/**` or `src/carve/ui/**` (`.tsx`, `.ts`, `.css`) | `web-engineer` |
| Mixed file types matching multiple specialists | route by *primary* output; if genuinely 50/50, prefer `python-engineer` for the Python half then `web-engineer` or relevant specialist for the other half (sequential) |
| None of the above | `engineer` (generic fallback) |

If the spec ID matches a known M2 web spec (M2-09 server, M2-10 WebSocket, M2-11 workbench, M2-12 monitor) or M3 web spec (M3-09 studio, M3-10 dbt run view), route to `web-engineer` directly even if file paths are ambiguous.

### 5. Engineer implements

Invoke the chosen engineer with:
- The spec ID
- The path to the phase file (`.carve-build/phases/{spec-id}.md`)
- An instruction to follow that agent's documented process

Wait for the engineer to report complete. The engineer's handoff includes the gate results (`ruff`, `mypy`, `pytest`) and a files-list audit.

### 6. Run reviewers in parallel

Determine which reviewers apply based on what files were changed:

- **Always run:** `qa-verifier`, `security-reviewer`
- **Run if any `src/carve/**/*.py` or `tests/**/*.py` changed:** `python-reviewer`
- **Run if any `.sql`, `_schema.yml`, or `src/carve/dbt/**` changed:** `dbt-reviewer`
- **Run if any `src/carve/core/agents/**`, `src/carve/skills/**`, agent TOML, or anything importing `anthropic` changed:** `agent-loop-reviewer`
- **Run if a UI screen changed and the local stack runs:** `manual-tester` (M2+ only)

Invoke the applicable reviewers in parallel. Each writes a report to `.carve-build/verification/{role}-{report}-{spec-id}.md` and returns PASS/FAIL.

### 7. Fix iteration loop

If any reviewer returned FAIL:

a. Increment the iteration counter (starting at 1).
b. If iteration > 3, **abort the build**. Print a summary of the failures and stop. Three iterations is the budget; further work is a human's call.
c. Invoke `task-planner` in fix mode with the spec ID, the iteration number, and the verification reports.
d. Wait for the fix plan at `.carve-build/fixes/{spec-id}-iter{n}.md`.
e. Re-invoke the engineer (same specialist as step 4) with the fix plan.
f. Re-run the applicable reviewers (step 6).
g. Loop back to (a).

### 8. Keep the spec honest

When all reviewers PASS:

- Invoke `spec-keeper` with the spec ID.
- It will either apply minor inline updates to `specs/{milestone-dir}/{spec-file}.md` or write a major-drift proposal next to the spec.

### 9. Print the handoff summary

Final stdout output, formatted for a human:

```
=== /build-spec {spec-id} complete ===

Spec: {spec title}
Engineer: {chosen specialist}
Iterations: {n}

Files changed:
  {list from git status, after the run}

Verification:
  qa-verifier: PASS ({n} tests, {%} coverage)
  security-reviewer: PASS ({n} findings, all informational)
  {role}-reviewer: PASS

Spec drift: {none | minor inline updates applied | major proposal written to <path>}

Next:
  - Review the changes (`git diff`)
  - Run any integration tests not run as part of the build
  - Commit when satisfied
```

## Constraints

- **Sequential phases, parallel within a phase.** Steps 1–4 are sequential. Step 6 (reviewers) runs in parallel. Fix iterations are sequential.
- **Never modify `specs/` from this skill except via `spec-keeper`.** Engineers and reviewers write only to `.carve-build/` for transient artifacts and to source directories for code.
- **Abort cleanly.** Any blocker — failed dependency check, exhausted fix budget, missing engineer — produces a clear error message and exits without leaving the workspace in an unknown state.
- **Don't push to remote.** This skill commits to the local working tree (or doesn't commit at all, depending on user preference). Pushing is the user's call.
