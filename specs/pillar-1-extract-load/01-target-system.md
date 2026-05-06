# P1-01 — Target system

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 1 day
**Dependencies:** M1-02 (config)
**Lineage:** Net-new. The per-target folder model was synthesized during this session's design discussion; no direct M1/M1.1/M2 ancestor. Foundation that the rest of Pillar 1 evolves on top of.
**Status:** Stub. Full spec to be drafted.

## Purpose

Define the per-target folder layout that every Pillar 1+ artifact lives in, plus the lifecycle commands for managing targets. Foundation that every other Pillar 1 spec depends on.

## What this introduces

- **Folder convention.** `targets/<name>/` is the home for everything specific to one environment: connection structure (`connections.toml`), secrets reference (`.env`, gitignored), and pillar-scoped artifact subdirectories (`el/`, `pipelines/`, `schedules/` — only `el/` ships in Pillar 1; the others are reserved).
- **`default_target` in `carve.toml`.** Set to `"dev"` at init. Every Carve verb defaults to it; `--target X` overrides.
- **`carve target` subcommand family.**
  - `carve target create <name>` — scaffolds `targets/<name>/` with empty pillar dirs + commented `connections.toml` + `.env.example`.
  - `carve target list` — table of existing targets.
  - `carve target show <name>` — what's currently deployed in this target (file listings per pillar dir).
  - `carve target rename <old> <new>` — `git mv` the directory; refuse if any open PRs reference the old name.
  - `carve target delete <name>` — removes the directory; refuses if it's `default_target` or has artifacts unless `--force`.
- **`--target` flag** as a top-level option that any subcommand can read.

## Out of scope

- Per-target connection config schemas (defined in P1-03 init layout and `connections.toml`)
- Per-target secrets loading (P1-04 dotenv)
- Cross-target deploy mechanics (P1-09 EL deploy)
- Target-aware artifact resolution (each pillar's run/deploy spec handles its own resolution against the target folder)
