# P1-03 — Init for per-target layout

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 0.5 day
**Dependencies:** M1.1-01 (existing init templates), P1-01 (target system)
**Lineage:** Continues **M1.1-01** (init config templates). The templated content (`connections.toml`, `runner.toml`, `models.toml`, `.env.example`) is preserved verbatim — only the directory destinations change to slot under `targets/dev/`. Existing `_write_if_missing` helper from M1-01 is reused. Brownfield dbt detection from accepted **M2-07** explicitly does **not** carry forward to Pillar 1; it lives in Pillar 2 alongside the dbt agent.
**Status:** Stub. Full spec to be drafted.

## Purpose

Refactor `carve init` so a fresh project comes up with the per-target folder layout that Pillar 1 needs. Existing templated content (`connections.toml`, `models.toml`, `runner.toml`, `.env.example`) is preserved but moved into `targets/dev/`.

## What this introduces

- **Greenfield `carve init`** produces:
  ```
  carve.toml                          # default_target = "dev", paths config
  targets/dev/
    .env.example                      # template; user copies to .env
    connections.toml                  # commented template
    el/                               # empty
    pipelines/                        # reserved (Pillar 3); not used in Pillar 1
    schedules/                        # reserved (Pillar 4); not used in Pillar 1
  carve/
    models.toml                       # commented template (Anthropic API key)
    runner.toml                       # commented template
  .gitignore                          # includes targets/*/.env, .carve/, *.sqlite
  ```
- **Refactor of `carve init` body** so the M1.1-01 templated content becomes `write_target_skeleton(target_name)` — reusable from `carve target create <name>` (P1-01).
- **`carve.toml`** gets `default_target = "dev"` written at init.

## Out of scope

- Brownfield dbt detection (Pillar 2; M2-07 carries forward)
- Convention inference (Pillar 2)
- `carve init` interactive prompts (defer)
