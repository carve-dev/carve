# M1-01 — CLI foundation

**Milestone:** 1 — Walking skeleton
**Estimated effort:** 1 day
**Dependencies:** none (this is the first spec)

## Purpose

Stand up the project skeleton, install the CLI framework, and create stubbed commands that exit cleanly. By end of day, `carve --help` works, `carve init` creates an empty project, and the development loop (lint, test, run) is in place.

## Scope

### In scope

- `pyproject.toml` with `uv` (preferred) or `poetry`
- Project layout matching `ARCHITECTURE.md`
- CLI framework selection and basic plumbing
- Stubbed commands that print TODO messages: `init`, `plan`, `apply`, `run`, `runs`, `logs`, `serve`, `version`
- Pre-commit hooks: ruff (lint + format), mypy (type checking)
- GitHub Actions CI: lint and test on push to any branch
- LICENSE (Apache 2.0), basic README, basic CONTRIBUTING

### Out of scope

- Any actual command implementation beyond stubs
- The web UI
- The API server
- Real config parsing (next spec)

## Technical decisions

### CLI framework

Use **`typer`**. Reasoning:

- Built on Click, so familiar conventions
- Better type-hint integration than raw Click
- Auto-generates help from docstrings and type annotations
- Active maintenance, good docs, broad adoption in the Python data ecosystem (used by `dbt`, `prefect`, etc.)

Alternatives considered: `click` (more verbose), `cyclopts` (newer, less proven), `argparse` (too low-level for our needs).

### Project layout

```
carve/
├── pyproject.toml
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── CHANGELOG.md
├── .pre-commit-config.yaml
├── .github/workflows/ci.yml
├── .gitignore
├── src/carve/
│   ├── __init__.py
│   ├── __main__.py        # python -m carve
│   ├── version.py
│   └── cli/
│       ├── __init__.py
│       ├── main.py        # typer app
│       └── commands/
│           ├── __init__.py
│           ├── init.py
│           ├── plan.py
│           ├── apply.py
│           ├── run.py
│           ├── runs.py
│           ├── logs.py
│           ├── serve.py
│           └── version.py
└── tests/
    ├── __init__.py
    └── test_cli.py
```

### Python version

Require Python 3.11+. Reasoning:

- Modern type hint syntax (`X | Y`, generic builtins) without `from __future__ import annotations` everywhere
- `tomllib` in stdlib (we'll use it for config parsing in the next spec)
- Sufficiently widespread by 2026

### Package manager

Use **`uv`**. Reasoning:

- Fastest installer in the ecosystem
- Drop-in replacement for pip + venv + pip-tools
- Active development by Astral (also makes ruff)
- Handles Python version management

If `uv` is unavailable, fall back to `poetry`. Don't use bare `pip` for dev.

## Implementation details

### `pyproject.toml`

> **Updated during implementation (2026-04-29):** the shipped `pyproject.toml` adds a `[tool.ruff.lint.per-file-ignores]` block that disables `B008` for `src/carve/cli/commands/*.py`. Typer's idiomatic command-signature pattern is `arg: T = typer.Argument(...)`, which flake8-bugbear's B008 ("do not perform function calls in argument defaults") flags by design. The ignore is scoped to CLI command modules where typer drives the signature, so the rule still applies everywhere else. The shipped file also picks up routine packaging extras (`readme`, `license`, `authors`, `keywords`, `classifiers`, `[project.urls]`, `[tool.hatch.build.targets.wheel]`, `[tool.pytest.ini_options]`) that the sample below omitted for brevity.

```toml
[project]
name = "carve"
version = "0.0.1"
description = "AI-first data engineering framework"
requires-python = ">=3.11"
dependencies = [
    "typer>=0.12",
    "pydantic>=2.6",
    "rich>=13.7",  # for CLI output
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "ruff>=0.4",
    "mypy>=1.10",
    "pre-commit>=3.7",
]

[project.scripts]
carve = "carve.cli.main:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "RUF"]

[tool.ruff.lint.per-file-ignores]
# typer's idiomatic pattern is `arg: T = typer.Argument(...)` in the command
# signature. Flake8-bugbear B008 flags it; we intentionally accept the pattern
# in CLI command modules where typer drives the signature.
"src/carve/cli/commands/*.py" = ["B008"]

[tool.mypy]
strict = true
python_version = "3.11"
```

### `src/carve/cli/main.py`

```python
import typer
from carve.cli.commands import init, plan, apply, run, runs, logs, serve, version

app = typer.Typer(
    name="carve",
    help="AI-first data engineering framework. Carve structure from chaos.",
    no_args_is_help=True,
)

app.command(name="init")(init.command)
app.command(name="plan")(plan.command)
app.command(name="apply")(apply.command)
app.command(name="run")(run.command)
app.command(name="runs")(runs.command)
app.command(name="logs")(logs.command)
app.command(name="serve")(serve.command)
app.command(name="version")(version.command)

if __name__ == "__main__":
    app()
```

### Stub command pattern

Each command in `cli/commands/` follows this pattern:

```python
import typer
from rich.console import Console

console = Console()

def command(
    goal: str = typer.Argument(..., help="The goal for the agent"),
):
    """Generate a plan for the given goal."""
    console.print(f"[yellow]TODO[/yellow]: plan command not yet implemented")
    console.print(f"Would generate plan for goal: {goal}")
    raise typer.Exit(code=0)
```

The point is to make the CLI surface complete on day 1 even if the implementations are stubs. This validates the command structure and gives the team something to demo.

### `carve init` minimum

For day 1, `carve init` creates only:

```
.
├── carve.toml         # near-empty placeholder
├── carve/
│   ├── connections.toml
│   ├── runner.toml
│   └── agents/         # empty
├── pipelines/         # empty
├── .env.example
└── .gitignore
```

The `carve.toml` content:

```toml
[project]
name = "my-carve-project"
version = "0.0.1"
default_target = "dev"

[paths]
config_dir = "carve"
```

That's enough to validate the loader in the next spec.

### Output formatting

Use `rich` for CLI output throughout. Conventions:

- Success messages: green checkmark + text (`✓ Created carve.toml`)
- Warnings: yellow text
- Errors: red text, exit code non-zero
- Section headers: bold
- Tables: `rich.table.Table`

Avoid emojis in CLI output (accessibility, terminal compatibility) — use plain ASCII or the rich-provided symbols.

### Exit codes

Standardize early — these will matter for CI integration later:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic failure |
| 2 | Config or usage error |
| 3 | No-op (build had nothing to do) |
| 4 | Guardrail violation |
| 5 | Connection error |

Each command should return one of these. Use `typer.Exit(code=N)`.

### Tests

For day 1, tests verify:

- `carve --help` produces the expected output
- Each command stub exits with code 0
- `carve version` prints something matching the version in `pyproject.toml`
- `carve init` in a fresh tmpdir creates the expected files

Use `typer.testing.CliRunner` for command testing.

## Acceptance criteria

- `uv pip install -e .` succeeds
- `carve --help` shows all eight commands
- `carve init` in an empty directory creates the minimum file layout
- `carve version` prints the version
- `pre-commit run --all-files` passes (lint, format, type-check all clean)
- `pytest` passes
- GitHub Actions CI passes on push

## Files this spec produces

- `pyproject.toml`
- `LICENSE` (Apache 2.0)
- `README.md` (basic project description; full README comes later)
- `CONTRIBUTING.md` (skeleton; full content comes in M3)
- `CHANGELOG.md` (just `## [Unreleased]` for now)
- `.pre-commit-config.yaml`
- `.github/workflows/ci.yml`
- `.gitignore`
- `src/carve/__init__.py`
- `src/carve/__main__.py`
- `src/carve/version.py`
- `src/carve/cli/__init__.py`
- `src/carve/cli/main.py`
- `src/carve/cli/commands/*.py` (eight stub commands)
- `tests/__init__.py`
- `tests/test_cli.py`

## What this enables

- All subsequent specs assume this skeleton exists
- The team has a runnable CLI to anchor demos around
- CI is in place to catch regressions from day 1
- The contribution surface is welcoming from the very first commit
