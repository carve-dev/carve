# Carve OSS commitment

This document is a promise about what is — and will remain — open source under Apache 2.0, and what we reserve for a possible future commercial offering. We're writing it down on day one because the worst time to define this line is after it matters.

## What is forever open source

Everything in the v0.1.0 release, and every feature listed below, is and will remain Apache 2.0 in this repository:

- **The CLI and core engine.** `carve init`, `carve plan`, `carve apply`, `carve run`, `carve runs`, `carve logs`, `carve doctor` and any future first-class commands.
- **The agent runtime.** Agent definitions, the tool-use loop, the skills SDK, custom step types, custom skill discovery.
- **All built-in agents and skills.** Orchestration, dbt, Snowflake, quality, code agents — and every skill they call.
- **All connectors and step types.** Snowflake, dbt, Python, SQL, shell, HTTP — including any new step types we add.
- **The plan/apply workflow.** Plan generation, plan files, diff rendering, apply orchestration, GitHub PR integration.
- **The local web UI.** Workbench, pipeline monitor, agent studio, dbt run view — the single-user UI bundled with the CLI.
- **The state store.** SQLite schema, repository pattern, migration tooling.
- **MCP client.** Consuming MCP servers from Carve.
- **Schema retrieval and embedding search.** Catalog queries, manifest queries, ChromaDB integration.
- **Documentation, examples, and reference projects.** Everything in `docs/`, `examples/`, and the published mkdocs site.

The principle: **everything a single user or single team needs to run Carve end-to-end on their own infrastructure is OSS.** No artificial limits, no "community edition" with crippled features, no time-bombed licenses.

## What is reserved for a possible commercial offering

If a commercial offering happens, it would be a separate product built on top of Carve, not features carved out of it. Specifically:

- **Managed multi-tenancy.** Hosting Carve as a service for many organizations, with isolation between tenants.
- **Enterprise authentication.** SSO, SCIM, role-based access control beyond single-user.
- **Audit and compliance tooling.** Immutable audit logs, SOC 2-grade access reporting, data lineage attestation.
- **Premium support.** SLAs, dedicated channels, priority bug fixes.
- **Hosted infrastructure.** Run Carve without operating it.

These are things a self-hosting individual or team genuinely doesn't need, and that an enterprise reasonably pays for. They are not "the good parts of the OSS held back" — they are operational concerns that only matter at the scale where someone is willing to pay to not handle them.

## What this means in practice

- **No feature gating.** We will not add a "this requires a license key" check to OSS features. If you can clone the repo, you can use everything in it.
- **No relicensing of existing code to a stricter license.** Code that ships under Apache 2.0 stays Apache 2.0. If we ever introduce a non-OSS license for *new* commercial code, it lives in a separate repo.
- **No "open core" sandbagging.** We will not deliberately keep the OSS version primitive to drive commercial sales. The OSS version is the product.
- **Trademark caveat.** "Carve" as a name and any associated logos are not licensed under Apache 2.0. You can fork the code freely; you just can't call your fork "Carve" or imply endorsement.

## How to hold us to this

- This file is part of the repository. Changes to it are reviewed in PRs like any other code.
- If a future change to this document narrows the OSS commitment, that change must be visible, justified, and discussed in the open before it lands.
- If you ever feel a feature was moved from "forever OSS" to "commercial," open an issue. We will either reverse the move or explain it publicly.
