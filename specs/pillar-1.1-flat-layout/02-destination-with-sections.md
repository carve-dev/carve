# P1.1-02 — `destination.toml` with sections

**Milestone:** Pillar 1.1 — Flat layout + git-based promotion
**Estimated effort:** 0.5 day
**Dependencies:** P1.1-01 (flat layout — files live at `el/<name>/`)
**Lineage:** Supersedes Pillar 1's destination.toml work (commits `e4eb505` stage 1 + `743daab` stage 2). Both stages assumed per-target folders, so each target had its own `destination.toml`. With the flat layout from P1.1-01, one file per artifact holds all per-target destination state via TOML sections.

## Purpose

Move `destination.toml` from a per-target file to a single per-artifact file with `[default]` + `[<target>]` sections. The script at runtime picks the section matching `CARVE_ACTIVE_TARGET`, falls back to `[default]`, falls back to env vars. Mirrors dbt's `profiles.yml` pattern in spirit (one config file with per-target sections; the active target picks which to use).

## File format

```toml
# el/iowa/destination.toml
#
# Destination for the `iowa` EL artifact across targets.
# The script reads the section matching CARVE_ACTIVE_TARGET, falls
# back to [default], then to <TARGET>_SNOWFLAKE_DATABASE/_SCHEMA
# env vars for unset fields.
#
# `table` is always required; it's the artifact's identity.
# `database` and `schema` are optional overrides.

[default]
table = "IOWA_LIQUOR"

[dev]
# dev uses a per-developer schema instead of the connection default
schema = "RAW_DEV"

[prod]
# prod inherits both database and schema from PROD_SNOWFLAKE_*
# (no overrides needed)

[staging]
# staging has a fully-qualified destination different from its connection
database = "STAGING_ANALYTICS"
schema = "EL"
```

### Resolution rule (applied at runtime by `main.py`)

For each field (`database`, `schema`, `table`):

1. If `destination[active_target]` is present and the field is set there → use it.
2. Else if `destination[default]` is present and the field is set there → use it.
3. Else (database / schema only) → `os.environ[f"{active_target.upper()}_SNOWFLAKE_<FIELD>"]`.
4. For `table` specifically: step 3 doesn't apply. `table` must be set in either `[default]` or `[<active_target>]`; absence is a malformed file.

### Canonical pattern emitted into `main.py`

The build agent's connection-context preamble updates with the new pattern:

```python
import os, tomllib
from pathlib import Path

_DEST_CFG = tomllib.loads(
    (Path(__file__).parent / "destination.toml").read_text(encoding="utf-8")
)
_TARGET = os.environ["CARVE_ACTIVE_TARGET"]
_TARGET_LOWER = _TARGET.lower()


def _resolve_destination_field(field: str) -> str:
    """Resolve `field` from per-target section → [default] → env var."""
    target_section = _DEST_CFG.get(_TARGET_LOWER, {})
    if field in target_section:
        return str(target_section[field])
    default_section = _DEST_CFG.get("default", {})
    if field in default_section:
        return str(default_section[field])
    if field == "table":
        raise KeyError(
            "destination.toml: `table` must be set under [default] or "
            f"[{_TARGET_LOWER}]; it's the artifact's identity."
        )
    return os.environ[f"{_TARGET}_SNOWFLAKE_{field.upper()}"]


DEST_DATABASE = _resolve_destination_field("database")
DEST_SCHEMA = _resolve_destination_field("schema")
DEST_TABLE = _resolve_destination_field("table")
DEST_FQN = f"{DEST_DATABASE}.{DEST_SCHEMA}.{DEST_TABLE}"
```

The pattern is rendered into the system prompt by the build flow; the agent copies it verbatim. The skill content in `core/skills/snowflake_destination.md` carries the same pattern under "Reading destination.toml at runtime."

## CLI surface

`carve plan` and `carve build` keep the `--table` / `--database` / `--schema` flags from Pillar 1 stage 2 (`743daab`). The semantics change slightly:

- `--table X` writes `[default].table = X` if the agent's `design.destination.table` would default to X; otherwise writes to `[<active_target>].table = X`. Heuristic: if the table is the same across every target, `[default]` is the right home; otherwise per-target.
- `--database X --target prod` writes `[prod].database = X` (target-scoped override). Without `--target`, writes to `[<default_target>]` since the user's intent is "set this for the target I'm building against."
- The build-time confirmation prompt shows the resolved FQN for the active target with per-field provenance (same shape as Pillar 1 stage 2, just sourced from the new section-based file).

## What changes in `destination.py`

The helper module from `e4eb505` (`src/carve/core/targets/destination.py`) gets a new shape:

```python
@dataclass(frozen=True, slots=True)
class DestinationSection:
    """One section of destination.toml — [default] or [<target>]."""

    table: str | None = None
    database: str | None = None
    schema: str | None = None


@dataclass(frozen=True, slots=True)
class DestinationConfig:
    """All sections of a single destination.toml file.

    `default` is the [default] section; `targets` maps target name to
    its section.
    """

    default: DestinationSection = field(default_factory=DestinationSection)
    targets: dict[str, DestinationSection] = field(default_factory=dict)

    def resolve_for(
        self,
        target: str,
        env: Mapping[str, str],
    ) -> tuple[str, str, str]:
        """Return (database, schema, table) for `target` using the
        resolution rule above. Raises KeyError if `table` isn't set
        anywhere, or if an env-var fallback fires for a missing var.
        """
        ...
```

Replaces the `Destination` dataclass + the single-section read/write helpers. `parse_fqn_from_goal` stays — natural-language FQN extraction is unchanged.

New helpers:

- `read_destination_toml(path) -> DestinationConfig` — handles the new section-based shape; raises clearly on malformed input.
- `write_destination_toml(path, config, *, target: str | None, env_defaults: dict[str, str | None]) -> None` — writes a sectioned file. When called by the builder, the active target's overrides go in `[<target>]`; fields that match env defaults are commented out.
- `merge_into_destination_toml(path, target, fields, env_defaults) -> None` — used by the build CLI's `--database` / `--schema` / `--table` flags: read existing file (if any), apply the user's fields under the right section (`[default]` for table when target-agnostic, `[<target>]` otherwise), write back. Preserves other sections verbatim.
- `resolve_at_runtime(...)` is removed from the helper module — the resolution happens inside the generated `main.py` via the canonical pattern. (The helper module is build-time; the script is runtime.)

### Migration of existing per-target destination.toml files

From v0.1.0:
```
targets/dev/el/iowa/destination.toml
targets/prod/el/iowa/destination.toml
```

To v0.1.1:
```
el/iowa/destination.toml      # [default] + [dev] + [prod] sections
```

P1.1-01's migration recipe says "manual merge." We add a helper:

- `carve target merge-destinations <name>` — reads the per-target files (if present under `targets/<X>/el/<name>/`), merges them into a single `el/<name>/destination.toml` with sections. The user runs this once per artifact. Removed in v0.2.

This is a one-shot upgrade verb, not part of the steady-state CLI. It lives at `src/carve/cli/commands/target/merge_destinations.py` and the test suite covers it.

## Implementation

### File-level changes

**Modified:**

- `src/carve/core/targets/destination.py` — `Destination` → `DestinationConfig` + `DestinationSection`. New read/write/merge helpers. Drop `resolve_at_runtime` (moved to the script's inline pattern).
- `src/carve/cli/orchestrator/builder.py` — `_write_destination_toml_for_build` writes the section-based file. Builder still computes whether the design's destination differs from env defaults; the override either goes under `[<active_target>]` (per-target) or `[default]` (if it's the only target with overrides — heuristic: when the file doesn't exist yet, `[default]` plus an empty `[<active_target>]` is the cleanest emit).
- `src/carve/cli/commands/build.py` — `_confirm_or_override_destination` reads the section-based file; the prompt shows per-target provenance.
- `src/carve/cli/orchestrator/planner.py` — `_resolve_user_destination` is unchanged; the destination_hint dict still has flat `database`/`schema`/`table` keys. The destination *file* is sectioned; the *design's destination block* stays flat.
- `src/carve/core/agents/prompts/m1_build_agent.md` — canonical "read destination.toml" pattern updated to the section-based shape.
- `src/carve/core/skills/snowflake_destination.md` — same pattern reflected in the skill content.

**Added:**

- `src/carve/cli/commands/target/merge_destinations.py` — one-shot upgrade verb.
- `tests/core/targets/test_destination.py` — tests for the new section-based read/write/resolve. The Pillar 1 tests for the flat-format file get replaced.
- `tests/cli/commands/target/test_merge_destinations.py` — covers the upgrade verb.

**Deleted:**

- The per-target `destination.toml` test fixtures from Pillar 1 stage-1 tests are consolidated.

## Tests

- `test_section_based_read_round_trip` — write a config with `[default]` + `[dev]` + `[prod]` sections; read back; assert structure preserved.
- `test_resolve_for_target_section_wins_over_default` — `[dev].schema = "X"`, `[default].schema = "Y"`; resolve for `dev` → `"X"`.
- `test_resolve_for_default_wins_over_env_var` — `[default].database = "Y"`, env `DEV_SNOWFLAKE_DATABASE = "Z"`; resolve for `dev` → `"Y"`.
- `test_resolve_for_env_var_fallback` — neither section sets database; env var fills in.
- `test_resolve_for_table_must_be_set` — table absent everywhere → `KeyError` (loud failure).
- `test_merge_into_destination_toml_creates_section_if_absent` — file has `[default]` and `[dev]`; merge in `--database` for `staging` adds a new `[staging]` section without disturbing the others.
- `test_merge_into_destination_toml_promotes_to_default_when_single_target` — first build against a project (no destination.toml yet) writes a `[default]` section + an empty `[<active_target>]` placeholder.
- `test_build_writes_destination_toml_with_default_section` — `carve build` from a fresh project writes the section-based file.
- `test_build_overrides_under_active_target_section` — `--schema CURATED` on `carve build --target dev` writes `[dev].schema = "CURATED"`, leaves `[default]` untouched.
- `test_build_prompt_resolves_fqn_for_active_target` — confirmation prompt shows the resolved FQN for the build's `--target`, not for an unrelated target.
- `test_generated_main_py_reads_section_based_config` — fixture: the build agent's emitted `main.py` includes the canonical `_resolve_destination_field` pattern.
- `test_target_merge_destinations_command` — fixture project has two per-target files; the command merges them into one sectioned file; the per-target files are deleted afterward.

## Acceptance criteria

- One `destination.toml` per artifact, with `[default]` + per-target sections.
- The script's runtime resolution applies the precedence rule (target section → default → env vars).
- `carve build --table / --database / --schema` writes the right section based on whether the value applies to all targets or just the active one.
- The build-time confirmation prompt shows the resolved FQN with per-field provenance for the active target.
- `carve target merge-destinations <name>` consolidates per-target files into one sectioned file (one-shot upgrade verb).
- All Pillar 1 stage 1 + stage 2 tests retargeted to the new shape pass.
- `ruff` + `mypy --strict` + `pytest` stay green.

## Files this spec produces

New: `src/carve/cli/commands/target/merge_destinations.py`, `tests/cli/commands/target/test_merge_destinations.py`.

Modified: `src/carve/core/targets/destination.py`, `src/carve/cli/orchestrator/{builder,planner}.py`, `src/carve/cli/commands/build.py`, `src/carve/core/agents/prompts/m1_build_agent.md`, `src/carve/core/skills/snowflake_destination.md`, `tests/core/targets/test_destination.py`, `tests/cli/orchestrator/test_builder.py`, `tests/cli/orchestrator/test_planner.py`.

No DB migrations.

## Out of scope

- DDL templating — that's P1.1-03. The DDL file still references concrete identifiers in this spec; P1.1-03 makes it Jinja-rendered.
- Removing the `--from X --to Y` deploy semantics — that's P1.1-03.
- Recovery agent path updates for the new layout — that's P1.1-04 (alongside the CI/CD docs).
- Multi-target destination resolution in a single command — defer.
- Environment-variable substitution inside `destination.toml` values (e.g. `table = "${PREFIX}_iowa"`). Defer; the `[default]` + per-target shape is enough for v0.1.1.

## What this enables

- A user with three environments (dev/staging/prod) sees their full destination config in one file. Diffing across targets is one open.
- The `[default]` section captures the common case (table name) so per-target sections stay minimal.
- The fall-through to env vars means most users have an empty `[prod]` section — prod inherits everything from `PROD_SNOWFLAKE_*`. The override is opt-in.
- The shape generalizes to Pillar 2 (dbt model destinations), Pillar 3 (pipeline step destinations) without further design.
