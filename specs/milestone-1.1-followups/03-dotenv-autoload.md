# M1.1-03 — Auto-load `.env` at CLI startup

**Milestone:** 1.1 — M1 follow-ups
**Estimated effort:** 0.25 day
**Dependencies:** M1-01 (CLI foundation), M1-02 (config loader)

## Purpose

`carve init` generates a `.env.example` and the loader interpolates `${VAR}` references from the environment, but nothing currently reads `.env` itself. A user who follows the natural workflow — `cp .env.example .env`, fill in values, run `carve plan ...` — gets a `ConfigError: Environment variable X is not set` because their shell never saw the file.

Close the gap: when the CLI starts up, look for a `.env` in the project root (the directory containing `carve.toml`) and load it into the environment **before** `load_config()` runs. Existing shell-set variables win — `.env` is a default, not an override.

## Scope

### In scope

- A small `_load_dotenv(project_dir: Path) -> None` helper that parses a `.env`-format file and sets any keys not already present in `os.environ`.
- A single hook at the top of every CLI command body that calls it before `load_config()`. The simplest place is a typer callback registered on the `app`; alternatively, in each command. The callback is cleaner.
- A short status line when the file was loaded, gated behind a `--quiet` flag or only printed at the debug level — don't add noise to every command.
- Honor a `--env-file <path>` typer option that overrides the default lookup, useful for CI where the file lives elsewhere.
- Honor `CARVE_NO_DOTENV=1` to disable the auto-load, for users who manage env vars elsewhere (direnv, mise, 1Password CLI, etc.).
- Decide on the parser:
  - **Preferred:** small inline parser (~30 lines) handling `KEY=value`, `KEY="value with spaces"`, `KEY='single'`, `# comments`, blank lines, and `\`-escapes inside double quotes. No multi-line values, no variable expansion inside `.env`. This avoids a new runtime dependency.
  - **Alternative:** add `python-dotenv` to runtime deps and use `dotenv.load_dotenv(override=False)`. Trade-off: one more dep vs. ~30 lines.

  Pick the inline parser; the format we need is small enough to not justify the dep.

### Out of scope

- Multi-file precedence (`.env.local`, `.env.<target>`, etc.). Single `.env` is enough for M1.1.
- Variable expansion inside `.env` (`FOO=${BAR}`). The config loader already handles `${VAR}` interpolation at the TOML level; doing it again inside `.env` would be confusing.
- Nested project-dir discovery. We use the same `project_dir` resolution the config loader does — current dir or `--project-dir`.
- Writing a real `.env` from `carve init`. That's a different feature; for M1.1 the user copies `.env.example` themselves.

## Implementation

### File: `src/carve/cli/dotenv.py`

```python
"""Tiny .env loader. No external deps, intentional shape."""

import os
import re
from pathlib import Path

_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<key>[A-Za-z_][A-Za-z0-9_]*)
    \s*=\s*
    (?P<value>
        "(?:\\.|[^"\\])*"      # double-quoted, with backslash-escapes
      | '(?:[^'])*'              # single-quoted, no escapes
      | [^\s#]*                   # bare value, stops at whitespace or comment
    )
    \s*(?:\#.*)?$
    """,
    re.VERBOSE,
)


def load_dotenv(path: Path, *, override: bool = False) -> dict[str, str]:
    """Parse `path` as a .env file. Set any keys not already in os.environ.

    Returns the dict of keys actually set this call (for caller logging).
    Missing file is not an error — returns {}.
    """
    if not path.is_file():
        return {}

    set_keys: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue  # silently skip malformed lines; keep this loader forgiving
        key = m.group("key")
        value = _unquote(m.group("value"))
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        set_keys[key] = value
    return set_keys


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        body = value[1:-1]
        if value[0] == '"':
            # Process backslash escapes for double-quoted values.
            return bytes(body, "utf-8").decode("unicode_escape")
        return body  # single-quoted: literal
    return value
```

### Wire-up: `src/carve/cli/main.py`

Add a typer callback that runs before any command:

```python
import os
from pathlib import Path

import typer

from carve.cli.dotenv import load_dotenv

# ... existing imports/app definition ...

@app.callback()
def _main_callback(
    project_dir: Path = typer.Option(
        Path.cwd(),
        "--project-dir",
        help="Project root (the directory containing carve.toml). Defaults to the current directory.",
    ),
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        help="Path to a .env file. Defaults to <project-dir>/.env.",
    ),
) -> None:
    """Load .env if present, then defer to the chosen subcommand."""
    if os.environ.get("CARVE_NO_DOTENV") == "1":
        return
    target = env_file or (project_dir / ".env")
    load_dotenv(target)
```

If `--project-dir` already exists at the top level (it does in the spec for M1-02; check `cli/main.py`), reuse it. Don't add a duplicate option.

The callback fires exactly once per `carve` invocation. Subcommands that already accept `--project-dir` get their copy from typer's option resolution; we just make sure the env is hydrated before `load_config()` runs.

### Don't auto-load in tests

`tests/conftest.py` (create if missing) sets `os.environ["CARVE_NO_DOTENV"] = "1"` at session start, so test runs aren't affected by stray `.env` files in the repo root or CI checkout dir. Document the rationale in a comment.

## Tests

`tests/cli/test_dotenv.py`:

- `test_load_dotenv_sets_unset_keys` — fresh env, `.env` writes `FOO=bar`, function sets `os.environ["FOO"]`.
- `test_load_dotenv_does_not_override_by_default` — `os.environ["FOO"] = "shell"` first; `.env` says `FOO=file`; after the load `os.environ["FOO"] == "shell"`.
- `test_load_dotenv_override_flag` — same setup, `override=True`, `os.environ["FOO"] == "file"`.
- `test_load_dotenv_handles_quotes_and_comments` — covers `KEY="quoted value"`, `KEY='single'`, `KEY=bare`, `KEY= # comment`, blank lines, lines starting with `#`.
- `test_load_dotenv_missing_file_is_noop` — returns `{}`, doesn't raise.
- `test_load_dotenv_skips_malformed_lines` — `not_a_kv_line` and `=missing_key` don't blow up; the rest of the file still loads.
- `test_load_dotenv_handles_backslash_escapes_in_double_quotes` — `KEY="line1\nline2"` becomes `line1\nline2`. (Use the literal `\n` two-char sequence in the source; the unicode_escape decoder turns it into a newline.)

`tests/cli/test_main.py` (or extend an existing CLI test):

- `test_callback_loads_env_before_command` — write a `tmp_path/.env` with `FOO=bar`, invoke `carve runs` via `CliRunner` with `--project-dir tmp_path`, monkeypatch `os.environ` to be empty first, assert the runs command saw `FOO=bar` in `os.environ`. (Use a custom test command or a small probe; can also verify by triggering a `${FOO}`-interpolating config and checking the resolved value.)
- `test_callback_respects_carve_no_dotenv` — same setup, `os.environ["CARVE_NO_DOTENV"] = "1"`, assert `FOO` is **not** set after the command runs.
- `test_env_file_option_overrides_default` — `tmp_path/.env` and `tmp_path/custom.env` differ; `--env-file tmp_path/custom.env` wins.

## Acceptance criteria

- A user who runs `carve init`, edits `.env`, then runs `carve plan ...` from the same directory has the env vars resolved without any extra steps.
- Existing shell vars are never clobbered (`.env` is a default).
- The callback is silent unless something goes wrong; no noise on stdout for every `carve` invocation.
- `CARVE_NO_DOTENV=1` disables the auto-load entirely for power users.
- `ruff` + `mypy --strict` + the full `pytest` suite stay green; new tests cover both the parser and the callback wiring.
- Short `## [Unreleased]` entry in `CHANGELOG.md`.
- README setup section mentions `.env` is auto-loaded.

## Files this spec produces

New:

- `src/carve/cli/dotenv.py`
- `tests/cli/test_dotenv.py`
- `tests/conftest.py` (only if it doesn't exist; otherwise extend it)

Modified:

- `src/carve/cli/main.py` (callback)
- `tests/cli/test_main.py` (or `tests/test_cli.py`, whichever holds the existing CLI tests)
- `CHANGELOG.md`
- `README.md` (one line in the setup section)

## What this enables

- The natural M1 setup flow (`carve init` → edit `.env` → `carve plan`) actually works without a separate `set -a; source .env; set +a` ritual.
- M1.1-01's expanded `.env.example` becomes load-bearing instead of a hint document.
- Future config fields that read from env (M2 secrets, M3 MCP tokens) inherit the same loading behavior for free.
