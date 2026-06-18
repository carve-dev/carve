---
name: spec-update
description: Re-sync a capability spec with the current state of the code, applying minor inline updates and proposing major updates for human review. Use this skill when a user has manually edited code outside the `/build-spec` flow and wants the spec brought back in line. Argument is a capability name. Produces inline edits to the capability spec for minor design drift, or a `_spec_update_proposal_{name}.md` file for major drift.
---

# /spec-update

Invokes the `spec-keeper` agent directly, outside the `/build-spec` flow. Use this when code has been edited by hand (or by an agent run that didn't include the `spec-keeper` step), and the spec needs to be re-synced with reality.

## Argument

A capability name like `runtime`, or a path to a capability spec.

## Process

1. **Resolve the capability.** Same resolution as `/build-spec`: a capability name → `specs/capabilities/{name}.md`, or use the path directly.
2. **Invoke `spec-keeper`** with the resolved spec.
3. **Print the result** to stdout:
   - "No drift detected — spec and code agree." (and exit cleanly), or
   - "Minor drift — applied inline updates to `specs/capabilities/{name}.md`. Review with `git diff`.", or
   - "Major drift — wrote proposal to `specs/capabilities/_spec_update_proposal_{name}.md`. Review and apply by hand if accepted."

## Constraints

- **No code modification.** This skill is documentation-only. If the spec keeper notices that the code itself is wrong (rare), it surfaces the observation in the proposal — it does not fix the code.
- **Major drift never edits the spec inline.** A wholesale section change is a design decision; only a human reconciles it.
- **The proposal file is reviewed and either applied (by hand-editing the spec, then deleting the proposal) or rejected (delete the proposal).**
