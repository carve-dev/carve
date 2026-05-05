# M2-07 — Brownfield onboarding

**Milestone:** 2 — Real product
**Estimated effort:** 1 day
**Dependencies:** M1-01 (CLI), M2-06 (dbt integration), M2-08 (convention inference)

## Purpose

Most users of Carve will have an existing dbt project. The `carve init` flow needs to detect this, integrate without overwriting, and produce a useful state on first run. Greenfield init is the easy case; brownfield is the one that determines whether Carve is adoptable.

## The greenfield case

For comparison: `carve init` in an empty directory produces:

```
.
├── carve.toml
├── carve/
│   ├── connections.toml
│   ├── models.toml
│   ├── runner.toml
│   ├── server.toml
│   └── conventions.md         (default minimal conventions)
├── pipelines/
├── dbt/                        (new dbt project scaffolded)
│   ├── dbt_project.yml
│   ├── profiles.yml.example
│   └── models/
│       └── example/
├── snowflake/
├── .env.example
└── .gitignore
```

This is the simple flow we already have from M1-01.

## The brownfield case

`carve init` in a directory that already has `dbt_project.yml` (or in any parent of it):

1. **Detect** existing dbt project
2. **Locate** the `dbt_project.yml`, `profiles.yml`, manifest
3. **Read** the existing config without modifying it
4. **Generate** Carve's own config files referencing the existing project
5. **Run `dbt parse`** to ensure manifest is current
6. **Run convention inference** to generate `carve/conventions.md`
7. **Add** Carve-specific entries to `.gitignore`
8. **Don't touch** anything else

## Detection logic

In `src/carve/cli/commands/init/detection.py`:

```python
def detect_dbt_project(start_dir: Path) -> DbtProjectInfo | None:
    """Walk up from start_dir looking for dbt_project.yml."""
    current = start_dir.resolve()
    for _ in range(10):  # max 10 levels up
        candidate = current / "dbt_project.yml"
        if candidate.exists():
            return DbtProjectInfo(
                project_dir=current,
                project_yml=candidate,
                profiles_yml=find_profiles_yml(current),
                manifest=find_manifest(current),
            )
        if current.parent == current:
            break
        current = current.parent
    return None

def find_profiles_yml(project_dir: Path) -> Path | None:
    # Order: project_dir/profiles.yml, then ~/.dbt/profiles.yml
    candidates = [
        project_dir / "profiles.yml",
        Path.home() / ".dbt" / "profiles.yml",
    ]
    return next((p for p in candidates if p.exists()), None)
```

## Three brownfield scenarios

### Scenario 1: Carve in same repo as dbt

Most common. User runs `carve init` in their dbt project's repo root (or in a subdirectory, in which case Carve walks up).

```
my-dbt-repo/
├── dbt_project.yml          ← existing
├── models/
├── carve.toml               ← new
└── carve/                   ← new
    ├── connections.toml
    └── ...
```

Carve config references `./` as the dbt project root.

### Scenario 2: Sibling directory

User wants Carve in a separate directory next to the dbt project:

```
~/work/
├── my-dbt/
│   └── dbt_project.yml
└── my-carve/                ← user runs carve init here
    └── carve.toml
```

The init flow asks: "I detected a dbt project at `../my-dbt`. Use it?" If yes, Carve config has:

```toml
[dbt]
project_dir = "../my-dbt"
```

### Scenario 3: Remote git URL

User wants Carve to manage a dbt project they don't have a local clone of. (Less common, mostly relevant for SaaS later.) For OSS v0.1, defer this — the user must clone first.

## Init flow with brownfield detection

```python
def init_command(
    interactive: bool = True,
    dbt_project_path: str | None = None,
):
    # 1. Detect
    detected = detect_dbt_project(Path.cwd())

    if dbt_project_path:
        # User passed --dbt-path
        detected = DbtProjectInfo.from_path(dbt_project_path)
    elif detected and interactive:
        confirm = typer.confirm(
            f"Detected dbt project at {detected.project_dir}. Use it?",
            default=True
        )
        if not confirm:
            detected = None

    # 2. Generate Carve config
    if detected:
        write_brownfield_config(detected)
    else:
        write_greenfield_config()

    # 3. Initialize state store
    initialize_database(...)

    # 4. If brownfield, parse manifest and infer conventions
    if detected:
        run_dbt_parse(detected)
        infer_conventions(detected)

    # 5. Update .gitignore
    update_gitignore()

    # 6. Print next steps
    print_post_init_message(detected)
```

## Generated config for brownfield

```toml
# carve.toml
[project]
name = "<directory_name>"
version = "0.0.1"
default_target = "dev"

[paths]
config_dir = "carve"

[dbt]
project_dir = "<detected_path>"
profiles_dir = "<detected_profiles_path>"
target = "dev"  # picked from profile or first available

[detection]
mode = "brownfield"
detected_at = "<timestamp>"
existing_models = <count>
existing_sources = <count>
```

The `[detection]` section is metadata for the user's reference — what was detected at init time.

## What carve init never modifies

A list of files and patterns Carve will not touch in brownfield mode:

- `dbt_project.yml`
- `profiles.yml`
- `packages.yml`, `dependencies.yml`
- Any file in `models/`, `tests/`, `seeds/`, `macros/`, `snapshots/`, `analyses/`
- `.dbt/`, `target/`, `dbt_packages/`
- The user's existing `.gitignore` (only appends; never overwrites)
- `requirements.txt`, `pyproject.toml`, or any other dependency manifest

If Carve would create a file that already exists, it skips and warns. No clobbering, ever.

## Existing connections detection

If `profiles.yml` exists, Carve reads it (read-only) to suggest connection config:

```yaml
# Detected profiles.yml:
my_project:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: xy12345.us-east-2
      user: dev_user
      role: TRANSFORMER_DEV
      ...
```

Becomes a suggestion in `carve/connections.toml`:

```toml
# Auto-generated from profiles.yml — review and adjust as needed
[snowflake.dev]
account = "xy12345.us-east-2"
user = "${SNOWFLAKE_USER}"        # don't hardcode; use env var
password = "${SNOWFLAKE_PASSWORD}"
role = "TRANSFORMER_DEV"
warehouse = "TRANSFORMER_WH"
database = "ANALYTICS_DEV"
```

Carve never copies passwords or tokens. Every sensitive value becomes a `${ENV_VAR}` placeholder, with the env var name listed in `.env.example`.

## .gitignore additions

Carve appends a clearly-marked block to the user's existing `.gitignore`:

```
# === Carve ===
.carve/
.env
```

If `.gitignore` doesn't exist, it's created with just this block.

## Post-init message

After successful brownfield init, print a useful summary:

```
✓ Carve initialized

Detected dbt project:
  Path: ./
  Models: 47
  Sources: 8
  Test count: 134

Conventions document generated:
  → carve/conventions.md (review before first plan)

Next steps:
  1. Edit carve/connections.toml with your Snowflake credentials
  2. Add ANTHROPIC_API_KEY to .env (copy from .env.example)
  3. Run: carve plan "describe what you want"

Read more: https://docs.carve.dev/getting-started
```

## Tests

- Detection finds dbt_project.yml in current dir
- Detection finds dbt_project.yml in parent dir
- `--dbt-path` overrides detection
- Decline of interactive confirmation falls through to greenfield
- Brownfield mode never overwrites existing files
- profiles.yml is read for connection suggestions
- Sensitive values become env var placeholders
- `.gitignore` is appended to, not replaced

Use temporary directories with various dbt project layouts as fixtures.

## Acceptance criteria

- `carve init` against an existing dbt project produces working Carve config without modifying the dbt project
- Detection handles same-repo and sibling-dir cases
- Convention inference (M2-08) runs as part of brownfield init
- Post-init message is actionable

## Files

- `src/carve/cli/commands/init.py` (replaces M1 stub)
- `src/carve/cli/commands/init/detection.py`
- `src/carve/cli/commands/init/greenfield.py`
- `src/carve/cli/commands/init/brownfield.py`
- `src/carve/cli/commands/init/profiles_reader.py`
- `tests/cli/commands/init/test_detection.py`
- `tests/cli/commands/init/test_brownfield.py`

## What this enables

- The most common adoption path (existing dbt user) just works
- The dbt agent has a manifest to query from minute one
- Conventions are inferred and ready for the first plan
