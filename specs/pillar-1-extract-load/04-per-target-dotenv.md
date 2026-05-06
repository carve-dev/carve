# P1-04 — Per-target dotenv loading

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 0.25 day
**Dependencies:** M1.1-03 (existing dotenv autoload), P1-01 (target system)
**Lineage:** Continues **M1.1-03** (CLI-startup dotenv autoload). The loader implementation is unchanged; only the path-resolution logic becomes target-aware. Backward compat: root `.env` still loads (with deprecation warning) until users migrate to `targets/<name>/.env`.
**Status:** Stub. Full spec to be drafted.

## Purpose

Refactor M1.1-03's CLI-startup dotenv autoload to load `targets/<active>/.env` based on the resolved active target — instead of a single root-level `.env`.

## What this introduces

- **Resolution order** at CLI startup:
  1. Determine the active target: `--target X` flag if present; else `default_target` from `carve.toml`; else `"dev"`.
  2. Load `targets/<active>/.env` if it exists.
  3. Fall back to root `.env` if it exists (backward compat for users who haven't migrated yet, with a deprecation warning).
- **`connections.toml` resolution** becomes target-aware: read from `targets/<active>/connections.toml` first, then `carve/connections.toml` as fallback.
- **`carve target list`** flags any target whose `.env` is missing (helpful at adoption time).

## Out of scope

- `.env` template generation (P1-03 init)
- Secrets management beyond dotenv (vault integration etc. — far future)
- Per-target config validation rules (covered in P1-01 + P1-03 specs)
