# Reference — Governance

How decisions get made, how releases happen, how contributors join the project.

## Project status

Carve is open-source under Apache 2.0, with the Developer Certificate of Origin (DCO) required on every commit. The project is sponsored by a single company in its early phase, with an explicit intent to broaden governance as it grows.

## License and contribution

- **License:** Apache 2.0. Permissive; allows commercial use, modification, distribution, with attribution.
- **DCO:** All commits must include `Signed-off-by:` (use `git commit -s`). The DCO certifies the contributor has the right to submit. We chose DCO over a CLA to lower contribution friction while preserving the ability to defend the project legally.
- **Dual licensing:** The project leadership reserves the right to offer commercial licenses for a future hosted version. Apache 2.0 + DCO permits this; CLA-bearing projects (e.g. Apache Foundation projects) often have stricter rules. Contributors should be aware that contributions may be incorporated into both OSS and commercial offerings.

## Roles

### Users
Anyone running Carve. No formal status; voice through GitHub issues, Discord, and community office hours.

### Contributors
Anyone who has had a PR merged. Listed in `CONTRIBUTORS.md`. May propose RFCs, vote in informal polls.

### Maintainers
Have merge rights on `main`. Initially appointed by project leadership. Maintainers:
- Review and merge PRs
- Triage issues
- Cut releases
- Vote on RFCs

Path to maintainer: 5+ substantive merged PRs, sustained engagement over 3+ months, nomination by an existing maintainer, lazy consensus from the maintainer group (no objections in 1 week).

### Project leadership
A small group (1-3 people in v0, expandable) that:
- Sets strategic direction
- Has final say on contested RFCs
- Manages the release schedule
- Manages security disclosures
- Decides on commercial-licensing matters

Leadership decisions are reversible by a 2/3 vote of all maintainers. This is a check, not a routine.

## Decision-making

### Day-to-day
- **Code changes:** PR with at least one maintainer approval. Trivial fixes (typos, doc corrections) can self-merge after a brief delay.
- **Bug triage:** Maintainers tag and assign. No formal process.

### Change lifecycle (bugs, enhancements, new capabilities)

How a change flows from "found" to "shipped" is the [change-lifecycle ADR](../_strategy/2026-06-change-lifecycle.md). The rule: **is the capability spec still correct?** If yes, it's a **bug** — add a regression test, fix the code, the spec body is untouched. If no, it's a **change** — update the capability spec *first* (spec-first), then build; a new capability gets a new spec. Change type maps to the SemVer bump:

| Change | Spec change? | SemVer |
|---|---|---|
| Bug fix | regression test only | patch |
| Enhancement | update the capability spec | minor |
| New capability / breaking change | spec (+ PRD/ARCH) | minor (pre-1.0) / major |

Substantive changes (public API, config schema, default behavior, architecture, security) additionally go through the **RFC process** below.

### Substantive changes — RFC process

Any change that affects:
- Public API (CLI, REST, Python module surface)
- Configuration schema
- Default behavior
- Architectural direction
- Security model

…requires an RFC. RFCs live in `rfcs/` in the main repo as numbered markdown files (`rfcs/0007-multi-region-state.md`).

**RFC template:**
```markdown
# RFC NNNN — Title

**Status:** draft | accepted | rejected | superseded
**Author:** @handle
**Created:** YYYY-MM-DD
**Discussion:** link to PR or issue

## Summary
One-paragraph description.

## Motivation
What problem does this solve? What are the goals?

## Detailed design
The meat — APIs, schemas, behavior, examples.

## Alternatives considered
What else did we look at? Why not those?

## Drawbacks
What's worse about Carve after this RFC?

## Migration / compatibility
Breaking? How do existing users adapt?

## Unresolved questions
What's not decided yet?
```

**RFC flow:**
1. Author opens a PR adding `rfcs/NNNN-slug.md` with status `draft`.
2. Discussion happens on the PR. Two-week minimum comment period.
3. Author iterates based on feedback.
4. Maintainers vote: at least 2/3 approval to advance.
5. If contested, leadership has the casting vote.
6. Status becomes `accepted` (PR merged) or `rejected` (PR closed; file kept as historical record).
7. Implementation happens via separate code PRs that reference the RFC.

### Lazy consensus
Most decisions are made by lazy consensus: a proposal stands unless someone objects within a stated comment period. This is the default for non-RFC changes.

### Voting
When voting is needed:
- Issue/PR comments: `+1` for support, `-1` for objection (with rationale required for `-1`)
- Quorum: 2/3 of active maintainers
- Active = at least one merged PR in the last 90 days
- Tie: leadership breaks

## Code of conduct

The project follows the [Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). Reports go to `conduct@carve.dev` and are handled by leadership privately.

Violations result in (in escalating severity):
1. Private warning
2. Public warning + temporary ban
3. Permanent ban

Leadership decisions on conduct cases are final; the goal is the safety of contributors, not procedural perfection.

## Releases

### Versioning
Carve follows SemVer:
- `0.x.y` — pre-1.0, breaking changes possible at minor (0.2 vs 0.1) but not patch (0.1.1 vs 0.1.0)
- `1.0+` — full SemVer; breaking only at major

### Cadence
- **Patch (0.x.y):** as needed; bugfixes only; auto-released on tag
- **Minor (0.x):** every 6-8 weeks; new features
- **Major (x):** when warranted; signaled by deprecation warnings 1 minor cycle in advance

### Release process
1. Release captain (rotating) opens a release PR: bumps version, updates `CHANGELOG.md`
2. CI runs full test matrix (multiple Python versions, multiple OSes, Snowflake integration)
3. Maintainer approval
4. Tag `v0.x.y` on `main`
5. CI builds wheels, uploads to PyPI, deploys docs to versioned URL
6. GitHub release with notes

### Long-term support
Pre-1.0: only the latest minor receives bugfixes.
Post-1.0: latest two minors receive bugfixes for 6 months each.

## Security

### Disclosure
Vulnerabilities reported privately to `security@carve.dev` (or via GitHub Security Advisory). Public issues are not appropriate.

Process:
1. Acknowledge within 48 hours
2. Develop and test a fix in a private branch
3. Coordinate disclosure date with reporter
4. Release patched version
5. Publish CVE (when applicable) and advisory

### Embargo
Reporters get attribution and a 90-day max embargo. Leadership extends only with strong justification.

### Threat model
Carve documents its threat model in `SECURITY.md`. Highlights:
- Carve runs trusted code from a trusted git repo. Protect repo access.
- Carve has access to whatever credentials are configured. Treat the host as sensitive.
- LLM-generated code is reviewed before merge — agents propose, humans (or CI) dispose.

## Contributing

`CONTRIBUTING.md` in the main repo covers:
- Development setup
- Test conventions
- Commit message format
- DCO sign-off
- PR review expectations

The TL;DR:
1. Fork
2. Create a branch
3. Make changes; add tests
4. `make check` (lint + type + test)
5. Open PR; sign off (`git commit -s`)
6. Address review feedback
7. Maintainer merges (squash by default)

## Trademark

The "Carve" name and logo are property of the sponsoring company. Use guidelines:
- Anyone may use the name to refer to the project
- Anyone may use the name and logo in promotional content for the project (talks, blog posts, articles)
- Forks must rename or use clear "based on Carve" attribution
- Commercial offerings using "Carve" in their name require permission

This is intentionally a small surface — most projects need this clarity.

## Forking and divergence

The license permits forking. If a fork develops momentum:
- We'll engage in good faith on technical disagreements
- We'll consider merging fork-specific changes if they fit Carve's direction
- We'll not block contributors from fork-only contributions
- Leadership may move the project's home or stewardship if the community votes to do so by 2/3

The goal is not to lock in users or contributors — it's to build something durable.

## Sustainability

Carve is initially funded by a sponsoring company that intends to build a hosted offering. The OSS core remains complete and self-hostable; commercial features are additive (managed infrastructure, multi-tenancy, RBAC, audit log, the polished cloud UI, premium integrations).

The line between OSS and commercial is committed in writing in `OSS_COMMITMENT.md`:
- Authoring (the AI harness, CLI, agents, skills, the dlt/dbt/sql component model, the REST + MCP surfaces): always OSS
- Single-team self-hosted execution (the scheduler + worker runtime, the Postgres state store, the local static UI): always OSS
- Managed / multi-tenant execution: commercial
- Enterprise auth (SSO/OAuth/RBAC) and audit log: commercial
- Anything that ships in `0.1` is forever OSS

This protects against the "open-core bait and switch" pattern, where features are moved from OSS to commercial after adoption.

## Amending this document

Changes to governance follow the RFC process. Leadership cannot amend governance unilaterally; structural changes require maintainer 2/3 approval.
