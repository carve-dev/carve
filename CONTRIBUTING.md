# Contributing to Carve

Carve is in early development. PRs, issues, and discussions are welcome. Before sending a substantial change, please open an issue or discussion so we can talk about whether it fits the current milestone.

## Developer Certificate of Origin (DCO)

All commits must be signed off under the [Developer Certificate of Origin 1.1](https://developercertificate.org/). This is a per-commit assertion that you wrote the change yourself, or otherwise have the right to contribute it under the project's license.

To sign off a commit, add `-s` to your `git commit`:

```bash
git commit -s -m "your message"
```

This appends a `Signed-off-by: Your Name <you@example.com>` line to the commit message. The line must use the same name and email as your `git config user.name` and `user.email`.

The DCO check runs automatically on pull requests. PRs with unsigned commits will be blocked until you amend or rebase to add sign-off.

## Dev setup

> Detailed setup will arrive in milestone 1 (`M1-01 — CLI foundation`). For now, the repo is mostly specs.

When the codebase exists, the expected workflow is:

```bash
# clone
git clone https://github.com/carve-dev/carve.git
cd carve

# install (Python 3.11+, uv recommended)
uv sync

# tests
uv run pytest

# lint and types
uv run ruff check
uv run mypy
```

## PR expectations

- **One concern per PR.** A spec-driven repo lends itself to small, focused PRs. Resist scope creep.
- **Tests included.** Every spec lists its tests; PRs implementing a spec must include those tests passing.
- **Link to a spec or issue.** PRs touching `src/carve/` should reference the spec ID they implement (e.g. "implements `M1-04`"). PRs that don't fit any spec should explain why.
- **DCO sign-off on every commit.** No exceptions, including merge commits and amends.
- **Pass CI.** Lint, type checks, and tests all green before review.
- **Small reviewers, fast turnaround.** Aim for a PR that reviews in under 15 minutes.

## Reporting bugs and proposing features

- **Bugs:** open an issue using the bug template.
- **Features:** open a discussion first if it's substantial; an issue with the feature template if it's a clear, small enhancement.
- **Security issues:** see [`SECURITY.md`](./SECURITY.md). Do not open public issues for vulnerabilities.

## Code of conduct

Participation in this project is governed by the [Contributor Covenant 2.1](./CODE_OF_CONDUCT.md). Be excellent to each other.
