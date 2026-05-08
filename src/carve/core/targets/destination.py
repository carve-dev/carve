"""Per-artifact, per-target destination configuration.

`destination.toml` lives next to `main.py` under
``targets/<target>/el/<artifact>/``. It pins the **table identity**
(always literal) and optionally **overrides** the database / schema
that would otherwise be inherited from the target's connection
defaults.

Why a separate file rather than env vars or a hardcoded literal in
``main.py``:

* The script is target-agnostic. Promotion via ``carve el deploy
  --from X --to Y`` doesn't require editing the script — the runtime
  database/schema follow the destination target's env vars.
* The table name IS artifact-specific, so it stays a literal — but
  outside the script body, so the agent doesn't need to weave it
  through string formatting.
* Per-target overrides are explicit and reviewable. A user who needs
  ``ANALYTICS.staging.iowa_sales`` in dev but ``ANALYTICS.prod.iowa_sales``
  in prod just edits prod's ``destination.toml`` post-deploy (or sets
  no override and lets prod's env vars do the work).

Resolution rule applied at runtime by ``main.py``:

* ``database`` = ``destination.toml.database`` if set, else
  ``os.environ['<TARGET>_SNOWFLAKE_DATABASE']``.
* ``schema`` = ``destination.toml.schema`` if set, else
  ``os.environ['<TARGET>_SNOWFLAKE_SCHEMA']``.
* ``table`` = ``destination.toml.table`` (always literal).

The build flow generates ``destination.toml`` (not the agent), so the
agent only needs to know where to read it from at runtime.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Destination:
    """Resolved destination triple plus override provenance.

    ``database`` and ``schema`` are ``None`` when the artifact should
    inherit them from the target's connection at runtime (i.e. they
    are NOT written as overrides in ``destination.toml``). ``table``
    is always set; an artifact without a table name is malformed.

    The two ``has_*_override`` flags duplicate the ``X is not None``
    checks but make call-site reads more readable when computing diffs
    against env defaults.
    """

    table: str
    database: str | None = None
    schema: str | None = None

    @property
    def has_database_override(self) -> bool:
        return self.database is not None

    @property
    def has_schema_override(self) -> bool:
        return self.schema is not None


# ---------------------------------------------------------------------------
# Parse FQN-like patterns out of natural-language goal text
# ---------------------------------------------------------------------------


# Conservative pattern. Matches identifiers like ``foo``, ``foo.bar``,
# ``foo.bar.baz`` — case-insensitive, must start with a letter or
# underscore, can contain letters/digits/underscores. Avoids false
# matches on URLs (``data.iowa.gov``) by requiring the FQN to follow
# common destination-naming preposition phrases.
_FQN_TRIGGER_RE = re.compile(
    r"""
    \b
    (?:into|to\ table|at\ table|in\ table|destination(?:\ table)?|table)
    \s* :? \s*
    (?P<fqn>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2})
    \b
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_fqn_from_goal(goal: str) -> Destination | None:
    """Best-effort extraction of a destination from free-form goal text.

    Returns ``None`` when no FQN-like phrase is found. The returned
    :class:`Destination` carries:

    * 1-segment match (``IOWA_SALES``) → ``table`` only; database and
      schema inherit from connection defaults.
    * 2-segment match (``sales.iowa_sales``) → ``schema`` + ``table``;
      database inherits.
    * 3-segment match (``analytics.sales.iowa_sales``) → all three set.

    The match is anchored to a small set of preposition phrases
    (``into``, ``to table``, ``destination``, etc.) so URLs and other
    dotted identifiers in the goal text don't false-match. The agent
    is still the final authority on the destination — this function
    pre-seeds ``design.destination`` to honor explicit user intent in
    the goal.
    """
    match = _FQN_TRIGGER_RE.search(goal)
    if match is None:
        return None
    fqn = match.group("fqn")
    parts = fqn.split(".")
    if len(parts) == 3:
        return Destination(database=parts[0], schema=parts[1], table=parts[2])
    if len(parts) == 2:
        return Destination(schema=parts[0], table=parts[1])
    if len(parts) == 1:
        return Destination(table=parts[0])
    return None  # pragma: no cover — regex caps at 3 segments


# ---------------------------------------------------------------------------
# Read / write destination.toml
# ---------------------------------------------------------------------------


def read_destination_toml(path: Path) -> Destination | None:
    """Load a ``destination.toml`` file. Returns ``None`` if missing.

    Raises ``ValueError`` for a malformed file (missing ``table`` field
    or non-string values). The deploy command surfaces these errors so
    the user can edit before promoting.
    """
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    table = data.get("table")
    if not isinstance(table, str) or not table:
        raise ValueError(
            f"{path}: missing required string field `table`. Every artifact's "
            "destination.toml must name the target table; database/schema "
            "are optional overrides."
        )
    database = data.get("database")
    schema = data.get("schema")
    if database is not None and (not isinstance(database, str) or not database):
        raise ValueError(f"{path}: `database` must be a non-empty string when set.")
    if schema is not None and (not isinstance(schema, str) or not schema):
        raise ValueError(f"{path}: `schema` must be a non-empty string when set.")
    return Destination(table=table, database=database, schema=schema)


def write_destination_toml(
    path: Path,
    destination: Destination,
    *,
    target: str,
    env_database: str | None,
    env_schema: str | None,
) -> None:
    """Render ``destination.toml`` for a per-target artifact directory.

    Fields that match the target's connection defaults are written as
    commented-out placeholders (so the user can see what's available
    to override but isn't currently). Fields that differ from the
    defaults are written as live overrides. ``table`` is always live.

    ``env_database`` / ``env_schema`` come from the target's
    ``[snowflake.<target>]`` section in ``connections.toml`` (the
    resolved values, after env-var interpolation). They're used to
    decide override-vs-inherit; passing ``None`` means "no default
    available," in which case any value on ``destination`` is treated
    as an override.

    The file is overwritten if present. Callers that want to preserve
    user edits should read first and merge.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    body_lines = [
        f"# Destination for `{path.parent.name}` in target=`{target}`.",
        "# `table` is always required (artifact-specific, target-independent).",
        "# `database` / `schema` are optional overrides — when commented out,",
        "# the script inherits them from the target's [snowflake.<target>]",
        "# section in carve/connections.toml at runtime.",
        "",
        f'table = "{_escape_toml_string(destination.table)}"',
        "",
    ]

    body_lines += _render_optional_field(
        "database", destination.database, env_database
    )
    body_lines += _render_optional_field("schema", destination.schema, env_schema)

    path.write_text("\n".join(body_lines).rstrip() + "\n", encoding="utf-8")


def _render_optional_field(
    name: str, value: str | None, env_default: str | None
) -> list[str]:
    """One block per optional field — always two lines.

    * No value, no env default: comment-only, no example.
    * No value, env default present: comment showing what would be
      inherited, no live line.
    * Value matches env default: comment + commented-out line so the
      user can see what's available without it being active.
    * Value differs from env default: live override line + comment
      explaining what it overrides.
    """
    if value is None:
        if env_default:
            return [
                f"# {name}: inherits `{env_default}` from "
                f"`os.environ` at runtime",
                f'# {name} = "{_escape_toml_string(env_default)}"',
                "",
            ]
        return [
            f"# {name}: no override; inherits from "
            f"`<TARGET>_SNOWFLAKE_{name.upper()}` at runtime",
            "",
        ]
    if env_default == value:
        return [
            f"# {name}: matches the connection default; left commented "
            "for clarity",
            f'# {name} = "{_escape_toml_string(value)}"',
            "",
        ]
    if env_default:
        return [
            f"# {name}: OVERRIDE. Connection default is `{env_default}`; "
            "this artifact uses the value below.",
            f'{name} = "{_escape_toml_string(value)}"',
            "",
        ]
    return [
        f"# {name}: override (no connection default to compare against).",
        f'{name} = "{_escape_toml_string(value)}"',
        "",
    ]


def _escape_toml_string(value: str) -> str:
    """Escape a value for use inside a TOML basic string literal.

    Mirrors the escape grammar TOML basic strings use: ``\\`` →
    ``\\\\``, ``"`` → ``\\"``, ``\\n`` → ``\\\\n``. The validator on
    inputs (build CLI flag, plan CLI flag, FQN parser) refuses anything
    weirder; this is belt-and-braces for hand-edited values.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


# ---------------------------------------------------------------------------
# Runtime resolution — used by the canonical pattern emitted into main.py
# ---------------------------------------------------------------------------


def resolve_at_runtime(
    destination: Destination,
    env: dict[str, str],
    target: str,
) -> tuple[str, str, str]:
    """Resolve the (database, schema, table) triple at runtime.

    The script's canonical pattern calls this (or an inline
    equivalent) to compute the FQN it will write to. ``target`` is
    the value of ``CARVE_ACTIVE_TARGET``; the env-var keys are derived
    from it.

    Raises ``KeyError`` when a field isn't overridden AND the
    corresponding ``<TARGET>_SNOWFLAKE_*`` env var is unset — surfaces
    misconfiguration loudly rather than landing in the wrong place.
    """
    upper = target.upper()
    database = destination.database or env[f"{upper}_SNOWFLAKE_DATABASE"]
    schema = destination.schema or env[f"{upper}_SNOWFLAKE_SCHEMA"]
    return database, schema, destination.table


__all__ = [
    "Destination",
    "parse_fqn_from_goal",
    "read_destination_toml",
    "resolve_at_runtime",
    "write_destination_toml",
]
