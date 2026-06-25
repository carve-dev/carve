# Lean plan/build — the harness owns the interactive gate; git/deploy own durability

- **Status:** Accepted (2026-06-25)
- **Supersedes scope of:** the heavyweight reading of [`../capabilities/plan-build.md`](../capabilities/plan-build.md) (the terraform-`plan`/`apply` framing in its header). The entity, the lifecycle verbs, and the config-hash drift gate stay; the *weight* is trimmed and the *ownership* of "review before code" is clarified.
- **Related:** [`2026-06-ai-harness.md`](./2026-06-ai-harness.md) (the harness this leans on), [`2026-06-control-plane.md`](./2026-06-control-plane.md), [`../capabilities/deploy.md`](../capabilities/deploy.md) (git/PR durability), [`2026-06-change-lifecycle.md`](./2026-06-change-lifecycle.md).

## Context

"Plan/build" was framed on the terraform `plan`/`apply` model: `carve plan` synthesizes a durable, reviewable Plan (no files written); `carve build` materializes it. Building the live wiring (plan-build Unit 2 sub-slice A) surfaced that **"plan/build" bundles two separable things**, and that the **AI harness already provides one of them natively** — raising the honest question of whether the rest earns its weight.

The two things:

1. **The propose → review → approve *interaction*** ("see what will change before any code is written; a human gates it"). The **harness already provides this**: a `plan` permission mode where the agent reasons + inspects but `edit`/write-bash are gated off (it proposes without writing — what sub-slice A implements as the engineer's *design capacity*), followed by the human approving and the agent re-running at `build` authority. The permission gate is the human-in-the-loop boundary.

2. **The durable Plan/Build *entity* + lifecycle** — a persisted, inspectable artifact with cost/runtime/impact estimated up front, a config-hash drift gate, and a non-conversational (REST/MCP/CI/schedule) surface. The harness does **not** provide this; a conversation is ephemeral.

A further observation sharpened it: for a control plane, the *durable diff/review/approval/audit* surface arguably **should be git + the PR** — which the [deploy](../capabilities/deploy.md) capability already owns (files → commit → push → PR; `carve.toml` + git as the drift surface). A bespoke Plan JSON + `plans` table risks **reinventing git+PR** for durability.

## Decision

Adopt **lean plan/build**. Split ownership of the two concerns rather than building a second, heavier mechanism for either:

- **The harness owns the interactive gate.** "Review before code is written" is the harness's `plan`-mode + permission gate (the engineer's *design capacity*), not a duplicate mechanism in plan-build. Most changes — interactive, conversational, small — flow through that gate; they do **not** need a formal Plan entity.
- **git + deploy own durability.** The durable, reviewable, auditable, approvable record of an authored change is the **git diff + the PR** (deploy's files→commit→push→PR). Carve does not reinvent that as a Plan-artifact store.
- **The Plan *entity* is reserved for what neither provides:** (a) **cost / runtime / impact estimated *up front*** (before any code is authored) — the budget + blast-radius gate; (b) the **config-hash drift gate** (already shipped, kept); (c) the **non-conversational "plan now, build later" surface** — exposing a plan over REST/MCP, in CI, or on a schedule, where there is no live conversation to be the gate.
- **Do not build heavyweight plan/build machinery on spec alone.** Refine chains, plan expiry, `plan-and-build`, and heavyweight *multi-engine synthesis-into-one-Plan* are kept only where a concrete use-case demands them — not built out speculatively because the terraform analogy suggested them. (The pieces already shipped in M1.1 + plan-build Unit 1 stay; they are not ripped out.)

## Rationale

- **Don't duplicate the harness.** The harness is *how* the AI works (loop, modes, gate, verify-by-execution, steering). Re-implementing "propose then approve" as a parallel entity-driven flow adds a second mental model ("is this a conversation or a plan/build lifecycle?") for a boundary the gate already draws.
- **Don't reinvent git.** Data engineers already review change as a diff/PR and treat git as the audit trail; deploy already produces that. The Plan entity adds value only where git is silent: a cost/impact *estimate before authoring*, and a surface for *non-interactive* (API/CI/scheduled) plan-then-build.
- **Match the stated intent.** "Does every change need to be planned and reviewed before written? No way." The harness's gate handles the everyday case; the Plan entity is for the changes that warrant a durable, estimated, drift-checked record.
- **Cheaper, clearer, and still safe.** The safety story (review before code, verified-by-execution, the permission gate, git/PR approval) is intact with *less* bespoke lifecycle machinery.

## Consequences

- **Already shipped stays valid.** plan-build Unit 1 (the config-hash drift gate, the cost rollup, the entity formalization) and Unit 2 sub-slice A (the live single-engine routing + the *design capacity* + the harness floor fix) are all **consistent with lean plan/build** — the design capacity *is* the lean interactive gate; the routing + cost rollup are needed regardless. Nothing built is reverted.
- **Re-scope the remaining plan-build sub-slices.** Sub-slice B's **multi-goal decomposition** (one goal → several engines) and the **live review fan-out** (dlt-qa/dlt-security/dbt-qa as the quality gate) stay — they are needed for a real multi-step goal and for quality, independent of plan/build's weight. The heavyweight **synthesis-into-one-Plan** slims toward "estimate cost/impact + hand the diff to git/deploy for review," rather than a bespoke merged-Plan artifact. Sub-slice C (the `post_build` emit) stays (a small lifecycle hook).
- **The durable-review path is git/deploy, not the Plan JSON.** Where a change is authored, the human reviews the **diff/PR** (deploy), not a re-rendered Plan. The Plan surfaces the *estimate* + the *drift gate* + the *API/scheduled* entry point.
- **Spec reconciliation (spec-first).** [`../capabilities/plan-build.md`](../capabilities/plan-build.md) is reconciled to this leaner framing (the interactive gate is the harness's; durability is git/deploy's; the Plan entity is the estimate + drift + non-conversational surface). The terraform-`plan`/`apply` header framing is softened accordingly. PRD/ARCHITECTURE references to plan/build are checked for the same.
- **Revisit triggers.** Build the deferred heavyweight pieces (refine chains, expiry, richer synthesis) only when a concrete use-case (a user workflow, a REST/scheduled requirement) demands them — recorded as a backlog item, not pre-built.
