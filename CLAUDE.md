# Carve — guide for Claude Code sessions

Carve is an AI-first, open-source data-engineering framework: a **control plane** plus a **Claude-Code-style AI harness** over independently-versioned `dlt` / `dbt` / `sql` components. Full product definition: [`specs/PRD.md`](specs/PRD.md).

## ⚠️ Two layers — do not confuse them

This repo contains **both** the tooling that *builds* Carve **and** *Carve itself*. They are separate trees with opposite purposes:

**Layer A — building Carve (our dev process).** Lives in **`.claude/`**. Standard Claude Code config: build-time subagents (`task-planner`, the engineers `python-engineer`/`dbt-engineer`/`snowflake-engineer`/`web-engineer`/`agent-author`, the `*-reviewer`s, `qa-verifier`, `spec-keeper`, `dependency-checker`) and skills (`/build-spec`, `/build-increment`, `/spec-update`). This is how *we* turn specs into shipped code. **Nothing here ships to Carve's users.**

**Layer B — Carve's product (what ships).** Lives in **`src/carve/`**. The runtime harness (`src/carve/core/agents/`: the agent loop, `delegate`, the permission gate, terminal tools, the verification loop) plus Carve's *own* runtime agents (`src/carve/core/agents/builtin/*.md` as they land — DLT engineer, dbt engineer, pipeline engineer, explorer, recovery) and runtime skills (`src/carve/core/skills/`). These run inside `carve serve` to build `dlt`/`dbt`/`sql`/pipelines **for Carve's users**.

**The line:** `.claude/` *builds* Carve; `src/carve/` *is* Carve. Note the deliberate name overlap — a `dbt-engineer` exists in **both** layers: `.claude/agents/dbt-engineer.md` changes how *we build Carve's* dbt features; the `dbt-engineer` capability (`specs/capabilities/dbt-engineer.md` → `src/carve/.../builtin/dbt-engineer.md`) is the *product* that writes *users'* dbt. Before editing an "engineer"/"reviewer", check which tree you're in.

## Where things are

| You want… | Look at |
|---|---|
| **Current state + what to build next** | [`specs/DELIVERY.md`](specs/DELIVERY.md) — the living, dependency-ordered plan. *Current state* is the source of truth for what's shipped. |
| **Durable design** | [`specs/PRD.md`](specs/PRD.md), [`specs/ARCHITECTURE.md`](specs/ARCHITECTURE.md), `specs/capabilities/<area>.md` (one per capability, phase-free) |
| **How we work (process decisions)** | `specs/_strategy/` ADRs — esp. `2026-06-spec-structure.md` (three-tier model) and `2026-06-change-lifecycle.md` (bug vs change, spec-first) |
| **Build tooling (Layer A)** | `.claude/agents/`, `.claude/skills/` |
| **Carve's product (Layer B)** | `src/carve/` |

## How to build a capability

1. Check [`specs/DELIVERY.md`](specs/DELIVERY.md) → *Current state* + the next increment's *In scope*.
2. Run **`/build-spec <capability>`** (e.g. `/build-spec extensibility`). It dependency-checks, generates the delta-aware *delivery spec* (the file manifest is computed at build time, never stored), routes to a specialist engineer, runs the parallel reviewer fan-out (including an adversarial security pass), fixes to green, then reconciles the spec via `spec-keeper`. Use **`/build-increment "<increment>"`** to walk a whole increment in dependency order.
3. Review the diff; commit when satisfied.

## Guardrails

- **The durable corpus is phase-free.** No `v0.1`/`v0.2`/`post-v0.1` vocabulary in `capabilities/`, PRD, ARCHITECTURE, or use-cases — phasing lives **only** in `DELIVERY.md`. A Stop-hook (`.claude/hooks/check-no-version-vocab.sh`) enforces this.
- **Classify every change** (change-lifecycle ADR): a **bug** = fix + regression test, the capability spec untouched; a **change** = update the capability spec *first* (spec-first), then build. Capability specs are edited only via `spec-keeper` or a deliberate spec-first change.
- **Transient build artifacts** live in `.carve-build/` (gitignored, regenerated each build). `.claude/settings.local.json` is per-machine (gitignored).
- **Commit only when asked.** End commit messages with the project's `Co-Authored-By` trailer.

## Status (pointer, not source of truth — see DELIVERY.md)

Shipped: M1 + M1.1 baseline; state-store (Postgres); **Increment 1** so far = `layout` + `harness`. Next in Increment 1: `extensibility`, `model-auth`, `packaging`.
