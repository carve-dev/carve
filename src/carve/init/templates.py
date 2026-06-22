"""Static file templates + the `carve.toml` renderer for `carve init`.

Plain-string templates (no Jinja dependency) — consistent with the rest of
the CLI scaffolding. The control-plane `carve.toml` is rendered from the
resolved :class:`~carve.init.plan.InitPlan`: simple-mode writes no
`[components.*]` blocks (same-repo dbt/dlt is convention-discovered); a block
is rendered only for a separate-local / separate-remote component.

Note: no `[state_store]` block is written. The loader raises on an unset
`${VAR}`, so a `url = "${DATABASE_URL}"` line would break `load_config` on a
fresh project before `.env` exists. The state-store URL flows from the
`DATABASE_URL` env (documented in `.env.example`) via the resolve precedence.
"""

from __future__ import annotations

import json

from carve.init.plan import InitPlan


def _toml_str(value: str) -> str:
    """Quote ``value`` as a TOML basic string.

    TOML basic strings share JSON's escaping, so ``json.dumps`` works — but
    ``ensure_ascii=False`` is required: JSON would otherwise escape a non-BMP
    codepoint (e.g. an emoji in the project directory name) as a surrogate
    pair ``\\udXXX``, which TOML rejects (``\\u`` must be a Unicode *scalar*),
    yielding an unparseable ``carve.toml``.
    """
    return json.dumps(value, ensure_ascii=False)


RUNNER_TOML_CONTENT = """\
# Runner configuration. The keys here populate the `runner` section of
# the merged config — write fields at the top level, no header.
# The `local_venv` runner is the only M1 option; Docker / remote runners
# arrive later.

# type = "local_venv"
# venv_cache_dir = ".carve/venvs"
# default_timeout_seconds = 1800
# max_concurrent_runs = 4

# Recovery agent (P1-09). Set `enabled = false` or pass --no-auto-fix on
# the CLI to disable the auto-fix loop. `max_attempts` is the per-failure
# budget — deploy phases each get their own pool.
# [auto_fix]
# enabled = true
# max_attempts = 3
"""

MODELS_TOML_CONTENT = """\
# Anthropic / model configuration. The keys here populate the `models`
# section of the merged config — write fields at the top level, no header.

# How Carve authenticates to Anthropic. Leave `auth_mode` unset to
# auto-resolve (API key first, then a Claude-subscription OAuth token), or
# pin it explicitly:
#   auth_mode = "api_key"   # uses ANTHROPIC_API_KEY
#   auth_mode = "oauth"     # uses a Claude-subscription OAuth token
#                           # (ANTHROPIC_AUTH_TOKEN / CLAUDE_CODE_OAUTH_TOKEN;
#                           #  mint one with `carve auth login`)

# anthropic_api_key = "${ANTHROPIC_API_KEY}"
# default_model = "claude-opus-4-8"

# Optional named model tiers a per-agent `model:` may reference:
# [tiers]
# fast = "claude-haiku-4-5"
"""

ENV_EXAMPLE_HEADER = """\
# Copy this to `.env` and fill in real values. `.env` is gitignored.

# === Project-wide ===
# Model-provider credential — set ONE of these (not both; the API rejects
# requests carrying both). A developer-portal API key:
ANTHROPIC_API_KEY=
# …or a Claude-subscription OAuth token (mint with `carve auth login`):
# ANTHROPIC_AUTH_TOKEN=
# GITHUB_TOKEN=                          # uncomment if using `carve el deploy`
"""

GITIGNORE_CONTENT = """\
.env
.carve/
*.sqlite
*.sqlite3
"""

STANDARDS_MD_CONTENT = """\
# Team standards

> User-authored. Read by agents on every invocation as part of pre-scoped context.
> Standards **override** conventions inferred by Carve (in `conventions.md`) where they conflict.

## Examples

Replace these with your team's rules. The more specific you can be, the more
predictable the agent's output will be.

- "All raw schemas use snake_case table names."
- "Stripe data must always be loaded incrementally, not full-refresh."
- "Use merge dispositions on PK for any pipeline pulling from a SaaS API."
- "All marts must have a `unique` test on the grain column."

(Delete this template content and replace with your actual standards.)
"""

DECISIONS_MD_CONTENT = """\
# Decisions

> Append-only, dated. Records durable choices the team has made, with rationale and reviewers.
> Read by `carve ask` for "why did we do X?" investigations.

## Format

    ## YYYY-MM-DD — Short title

    **Decision:** What we decided.
    **Rationale:** Why.
    **Reviewers:** alice@, bob@
    **Impact:** Which pipelines / models / schemas this affects.

## (No decisions recorded yet)
"""

# Convention inference is deferred (see DELIVERY); init writes this placeholder
# so the file exists. `carve memory refresh` will populate it later.
CONVENTIONS_MD_CONTENT = """\
# Project conventions

> Inferred by Carve from this project's existing code. Convention inference is
> not yet wired up — run `carve memory refresh` once it ships to populate this
> from any detected dbt/dlt projects. User-edited overrides go in
> `carve/standards.md`, which takes precedence over inferred conventions.

## (No conventions inferred yet)
"""

DLT_SAMPLE_INIT_CONTENT = """\
\"\"\"Sample dlt source scaffolded by `carve init --with-dlt`.

Replace this with your real source, or run
`carve plan "ingest <your source>"` to have Carve author one.
\"\"\"

import dlt


@dlt.source
def sample_source():
    @dlt.resource(name="rows", write_disposition="replace")
    def rows():
        yield {"id": 1, "value": "hello"}

    return rows
"""


def render_carve_toml(plan: InitPlan) -> str:
    """Render the control-plane `carve.toml` from a resolved plan."""
    lines = [
        "# Generated by `carve init`. Edit freely.",
        "[project]",
        f"name = {_toml_str(plan.project_name)}",
        f"default_target = {_toml_str(plan.default_target)}",
        "",
        "[paths]",
        'config_dir = "carve"',
        'agents_dir = "carve/agents"',
    ]
    for c in plan.components:
        lines += ["", f"[components.{c.name}]", f'type = "{c.type}"', f'mode = "{c.mode}"']
        if c.mode == "separate-local" and c.path is not None:
            lines.append(f"path = {_toml_str(c.path)}")
        elif c.mode == "separate-remote":
            if c.url is not None:
                lines.append(f"url = {_toml_str(c.url)}")
            if c.branch is not None:
                lines.append(f"branch = {_toml_str(c.branch)}")
    return "\n".join(lines) + "\n"


def render_dbt_project_yml(project_name: str) -> str:
    """A minimal valid `dbt_project.yml` for `--with-dbt` greenfield scaffold."""
    name = _toml_str(project_name)
    return (
        f"name: {name}\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        f"profile: {name}\n"
        'model-paths: ["models"]\n'
    )
