# P1-07 — `carve el run`

**Milestone:** Pillar 1 — Extract & Load
**Estimated effort:** 0.5 day
**Dependencies:** M1-05 (step + runner protocols), M1.1-03 (root .env autoload), P1-01 (target system), P1-02 (plan/build lifecycle)
**Lineage:** Continues **M1.1-06**'s `carve run <pipeline>` command. `LocalVenvRunner` (M1-05) is unchanged. The replay-guard removal from M1.1-06 carries forward. **M1.1-03**'s root `.env` autoload is also unchanged — under the centralized config model (P1-01), there's no per-target `.env` switching; the active target only selects which `[snowflake.<target>]` section of `connections.toml` is read. Net-new in this spec: the path-resolution lookup (`targets/<active>/el/<name>/main.py`) and the CLI restructure (lives under the `carve el` subcommand). Existing `carve run` becomes a deprecated alias that warns and forwards to `carve el run` for one minor version, then is removed.
**Status:** Stub. Full spec to be drafted.

## Purpose

Execute an EL artifact against the active target. Reads `targets/<active>/el/<name>/main.py` and `requirements.txt`, materializes a venv via `LocalVenvRunner`, runs the script with target-scoped env vars, streams logs back to the user.

## What this introduces

- **`carve el run <name> [--target X]`.** Default target = `default_target` from carve.toml; `--target` overrides.
- **Path resolution.** `pipelines/<name>/main.py` (M1.1-06's old layout) → `targets/<active>/el/<name>/main.py`. The runner is otherwise unchanged.
- **Env var assembly.** Root `.env` is already loaded at CLI startup (M1.1-03). The runner passes the resolved environment (target-prefixed vars + `connections.toml`-derived values from the active target's section) to the venv subprocess.
- **Re-runnable.** Replay guard stays gone; matches M1.1-06.
- **`carve el list`** as a sibling command — lists EL artifacts in the active target.

## Out of scope

- Recovery agent integration (P1-09 wraps this; this spec just runs and reports failure)
- Concurrency limits (the existing `runner.toml` setting still applies, no spec change)
- Run cancellation from the CLI (defer; `Ctrl-C` works via process signals)
- `carve el show <name>` — could come later
