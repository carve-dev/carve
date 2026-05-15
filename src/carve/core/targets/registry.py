"""Target registry helpers — single source of truth for adding/removing/listing targets.

The registry mediates two artifacts (P1.1-01 removed the third):

* ``carve/connections.toml`` — one ``[snowflake.<name>]`` section per target.
  Edited via ``tomlkit`` so comments and key order are preserved.
* ``.env.example`` — a tracked template file with a ``# === <name> target ===``
  block per target listing the ``<NAME>_*`` env vars referenced by the section.
  Edited via plain text manipulation (regex / line splitting); ``.env.example``
  isn't TOML.

Pre-P1.1-01 the registry also created ``targets/<name>/el/``; this directory
no longer exists. EL artifacts live under the flat ``el/<name>/`` tree
(created by ``carve init`` / ``carve build``), target-agnostic.

The high-level entry point is :func:`add_target_to_project`, which is called
by both ``carve init`` (for ``dev``) and ``carve target create`` (for any
other target name). Keeping this single helper in one place means both verbs
produce byte-identical output for the section + env-example block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import Table

# Same regex used by M1.1-06 for pipeline names. Lowercase, alphanumeric
# and underscores, must start with a letter.
TARGET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# Header written at the top of a brand-new ``connections.toml`` (when init
# creates the file). Existing files keep whatever header / comments they had.
DEFAULT_CONNECTIONS_TEMPLATE_HEADER = """\
# Connection definitions for Snowflake (and future connectors).
# The key after `[snowflake.<target>]` is the target name, referenced from
# carve.toml's `default_target` (default: "dev").
#
# Use ${VAR_NAME} to interpolate environment variables from .env or your shell.
"""


# Standard field set for a Snowflake target section. Order matches the
# spec sample.
_SNOWFLAKE_FIELDS: tuple[str, ...] = (
    "account",
    "user",
    "password",
    "role",
    "warehouse",
    "database",
    "schema",
)


class InvalidTargetNameError(ValueError):
    """Raised when a target name fails the naming-regex validation."""


class TargetExistsError(ValueError):
    """Raised when a target already exists where one is being created."""


class TargetNotFoundError(ValueError):
    """Raised when a target is expected to exist but doesn't."""


def validate_target_name(name: str) -> None:
    """Validate ``name`` against ``TARGET_NAME_RE``.

    Raises:
        InvalidTargetNameError: If ``name`` does not match the regex.
    """
    if not TARGET_NAME_RE.fullmatch(name):
        raise InvalidTargetNameError(
            f"Target name {name!r} must match {TARGET_NAME_RE.pattern} "
            "(lowercase, alphanumeric and underscores, starting with a letter)."
        )


# ---------------------------------------------------------------------------
# connections.toml — tomlkit edit-in-place
# ---------------------------------------------------------------------------


def _load_connections_doc(conn_path: Path) -> TOMLDocument:
    """Read ``conn_path`` and return a tomlkit document, or empty doc."""
    if not conn_path.is_file():
        return tomlkit.document()
    text = conn_path.read_text(encoding="utf-8")
    return tomlkit.parse(text)


def list_target_sections(conn_path: Path) -> list[str]:
    """Return the list of ``[snowflake.<name>]`` target names defined in the file.

    Returns an empty list if the file doesn't exist or has no
    ``[snowflake.*]`` sections.
    """
    doc = _load_connections_doc(conn_path)
    snowflake = doc.get("snowflake")
    if not isinstance(snowflake, dict):
        return []
    return list(snowflake.keys())


def add_target_section(
    name: str,
    conn_path: Path,
    *,
    force: bool = False,
) -> None:
    """Append a ``[snowflake.<name>]`` section to ``conn_path``.

    The section uses ``${<NAME>_SNOWFLAKE_*}`` placeholders for every field.
    If the file doesn't exist, it's created with the default template header.
    Existing sections are preserved verbatim (tomlkit retains comments,
    blank lines, and key ordering).

    Args:
        name: Target name. Must already pass :func:`validate_target_name`.
        conn_path: Path to ``carve/connections.toml``.
        force: If True, overwrite an existing ``[snowflake.<name>]`` section.

    Raises:
        TargetExistsError: If the section exists and ``force`` is False.
    """
    if conn_path.is_file():
        doc = _load_connections_doc(conn_path)
    else:
        doc = tomlkit.parse(DEFAULT_CONNECTIONS_TEMPLATE_HEADER)

    snowflake = doc.get("snowflake")
    if snowflake is not None and not isinstance(snowflake, dict):
        # Defensive: a top-level "snowflake = ..." scalar would shadow the
        # nested table form. We refuse rather than silently rewrite.
        raise ValueError(
            f"{conn_path} contains a non-table `snowflake` entry; "
            "expected `[snowflake.<target>]` sections."
        )

    # Tomlkit's `.get("snowflake")` returns the table-of-tables when present.
    if isinstance(snowflake, dict) and name in snowflake and not force:
        raise TargetExistsError(
            f"[snowflake.{name}] already exists in {conn_path}"
        )

    table = tomlkit.table()
    upper = name.upper()
    for field in _SNOWFLAKE_FIELDS:
        # Wrap each ${VAR} placeholder as a string item.
        placeholder = f"${{{upper}_SNOWFLAKE_{field.upper()}}}"
        table.add(field, placeholder)

    # Append at the end of the document (tomlkit preserves trailing comments
    # by inserting before them when the table-of-tables already exists; that
    # behaviour is fine for our purposes).
    if "snowflake" in doc:
        existing = doc["snowflake"]
        if isinstance(existing, dict):
            existing[name] = table
    else:
        snowflake_root = tomlkit.table(is_super_table=True)
        snowflake_root[name] = table
        doc["snowflake"] = snowflake_root

    conn_path.parent.mkdir(parents=True, exist_ok=True)
    conn_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def remove_target_section(name: str, conn_path: Path) -> None:
    """Remove the ``[snowflake.<name>]`` section from ``conn_path``.

    Raises:
        TargetNotFoundError: If the section doesn't exist.
    """
    if not conn_path.is_file():
        raise TargetNotFoundError(f"{conn_path} does not exist")
    doc = _load_connections_doc(conn_path)
    snowflake = doc.get("snowflake")
    if not isinstance(snowflake, dict) or name not in snowflake:
        raise TargetNotFoundError(
            f"[snowflake.{name}] not found in {conn_path}"
        )
    del snowflake[name]
    conn_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def rename_target_section(old: str, new: str, conn_path: Path) -> None:
    """Rename a ``[snowflake.<old>]`` section to ``[snowflake.<new>]``.

    Field values are preserved, so existing ``${OLD_*}`` placeholders are
    *not* rewritten — that's the caller's job (typically by also rewriting
    them to ``${NEW_*}`` placeholders via a fresh ``add_target_section`` call,
    after deleting the old one). For a true rename-with-placeholder-rewrite,
    callers should remove + re-add.

    Raises:
        TargetNotFoundError: If ``old`` doesn't exist.
        TargetExistsError: If ``new`` already exists.
    """
    if not conn_path.is_file():
        raise TargetNotFoundError(f"{conn_path} does not exist")
    doc = _load_connections_doc(conn_path)
    snowflake = doc.get("snowflake")
    if not isinstance(snowflake, dict) or old not in snowflake:
        raise TargetNotFoundError(
            f"[snowflake.{old}] not found in {conn_path}"
        )
    if new in snowflake:
        raise TargetExistsError(
            f"[snowflake.{new}] already exists in {conn_path}"
        )

    # tomlkit doesn't support key rename, so we read, delete, and re-insert
    # with the new placeholder values. This preserves the surrounding doc
    # comments / blank lines while rewriting the section's body to the new
    # ${NEW_*} placeholders (the natural meaning of "rename" in our model).
    del snowflake[old]
    new_table = tomlkit.table()
    upper = new.upper()
    for field in _SNOWFLAKE_FIELDS:
        new_table.add(field, f"${{{upper}_SNOWFLAKE_{field.upper()}}}")
    snowflake[new] = new_table

    conn_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class SectionValue:
    """A resolved field within a ``[snowflake.<target>]`` section.

    Attributes:
        key: Field name (e.g. ``"account"``).
        raw: The raw string from the TOML file (e.g. ``"${DEV_..._ACCOUNT}"``).
        env_var: When the raw value is exactly ``${VAR_NAME}``, the bare
            ``VAR_NAME``. Otherwise None.
    """

    key: str
    raw: str
    env_var: str | None


_SINGLE_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def show_section_values(name: str, conn_path: Path) -> list[SectionValue]:
    """Return the field values of ``[snowflake.<name>]`` for display.

    Each value is wrapped in a ``SectionValue`` so the renderer can decide
    whether to print the raw value (literal) or ``<from VAR>`` (env-var).
    """
    if not conn_path.is_file():
        raise TargetNotFoundError(f"{conn_path} does not exist")
    doc = _load_connections_doc(conn_path)
    snowflake = doc.get("snowflake")
    if not isinstance(snowflake, dict) or name not in snowflake:
        raise TargetNotFoundError(f"[snowflake.{name}] not found in {conn_path}")
    section = snowflake[name]
    if not isinstance(section, (dict, Table)):
        raise TargetNotFoundError(f"[snowflake.{name}] is not a table")

    values: list[SectionValue] = []
    for key, raw in section.items():
        raw_str = str(raw)
        match = _SINGLE_VAR_RE.match(raw_str)
        env_var = match.group(1) if match else None
        values.append(SectionValue(key=str(key), raw=raw_str, env_var=env_var))
    return values


def section_referenced_env_vars(name: str, conn_path: Path) -> list[str]:
    """Return every distinct ``${VAR}`` env var referenced by the section."""
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    if not conn_path.is_file():
        return []
    doc = _load_connections_doc(conn_path)
    snowflake = doc.get("snowflake")
    if not isinstance(snowflake, dict) or name not in snowflake:
        return []
    section = snowflake[name]
    if not isinstance(section, (dict, Table)):
        return []

    seen: list[str] = []
    seen_set: set[str] = set()
    for raw in section.values():
        for match in pattern.finditer(str(raw)):
            var = match.group(1)
            if var not in seen_set:
                seen_set.add(var)
                seen.append(var)
    return seen


# ---------------------------------------------------------------------------
# .env.example — text manipulation
# ---------------------------------------------------------------------------


def _env_example_block_lines(name: str) -> list[str]:
    """Return the canonical ``# === <name> target ===`` block lines."""
    upper = name.upper()
    return [
        f"# === {name} target ===",
        f"{upper}_SNOWFLAKE_ACCOUNT=",
        f"{upper}_SNOWFLAKE_USER=",
        f"{upper}_SNOWFLAKE_PASSWORD=",
        f"{upper}_SNOWFLAKE_ROLE=",
        f"{upper}_SNOWFLAKE_WAREHOUSE=",
        f"{upper}_SNOWFLAKE_DATABASE=",
        f"{upper}_SNOWFLAKE_SCHEMA=",
    ]


def add_env_example_block(name: str, env_example_path: Path) -> None:
    """Append a ``# === <name> target ===`` block to ``.env.example``.

    Creates the file if missing. Each call appends; this function does not
    detect or refuse duplicates (callers should check via
    :func:`env_example_has_block`).
    """
    block = "\n".join(_env_example_block_lines(name))
    env_example_path.parent.mkdir(parents=True, exist_ok=True)
    if env_example_path.is_file():
        existing = env_example_path.read_text(encoding="utf-8")
        # Ensure exactly one blank line between previous content and the
        # new block. If the file ends with a newline, we add one more so
        # the visible separation is one blank line.
        sep = "\n" if existing.endswith("\n") else "\n\n"
        env_example_path.write_text(existing + sep + block + "\n", encoding="utf-8")
    else:
        env_example_path.write_text(block + "\n", encoding="utf-8")


def env_example_has_block(name: str, env_example_path: Path) -> bool:
    """Check whether ``.env.example`` already contains a block for ``name``."""
    if not env_example_path.is_file():
        return False
    text = env_example_path.read_text(encoding="utf-8")
    return f"# === {name} target ===" in text


def remove_env_example_block(name: str, env_example_path: Path) -> None:
    """Remove the ``# === <name> target ===`` block from ``.env.example``.

    The block runs from its header line to (but not including) the next
    ``# ===`` header or EOF. Surrounding blank lines around the block are
    collapsed so we don't leave double blank lines behind.
    """
    if not env_example_path.is_file():
        return
    lines = env_example_path.read_text(encoding="utf-8").splitlines()
    header = f"# === {name} target ==="
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == header:
            # Skip to next "# ===" header or EOF.
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("# ==="):
                i += 1
            # Trim trailing blank line in `out` so we don't leave a doubled
            # blank between the previous content and what follows.
            while out and out[-1].strip() == "":
                out.pop()
            continue
        out.append(lines[i])
        i += 1

    # Re-join with trailing newline.
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    env_example_path.write_text(text, encoding="utf-8")


def rename_env_example_block(
    old: str,
    new: str,
    env_example_path: Path,
) -> None:
    """Rewrite ``<OLD>_*`` lines + the header for the block named ``old``.

    Lines outside the block are left untouched. If the file or block is
    missing, this is a no-op.
    """
    if not env_example_path.is_file():
        return
    upper_old = old.upper()
    upper_new = new.upper()
    header_old = f"# === {old} target ==="
    header_new = f"# === {new} target ==="

    lines = env_example_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == header_old:
            out.append(header_new)
            in_block = True
            continue
        if in_block and stripped.startswith("# ==="):
            in_block = False
        if in_block:
            # Rewrite leading <OLD>_ to <NEW>_ at line start.
            if line.startswith(f"{upper_old}_"):
                out.append(f"{upper_new}_" + line[len(upper_old) + 1 :])
                continue
            if line.startswith(f"# {upper_old}_"):
                out.append(f"# {upper_new}_" + line[len(upper_old) + 3 :])
                continue
        out.append(line)

    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    env_example_path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# High-level orchestrator
# ---------------------------------------------------------------------------


def add_target_to_project(
    name: str,
    root: Path,
    *,
    config_dir: str = "carve",
    force: bool = False,
) -> None:
    """Add a target to the project's connection config.

    1. Append a ``[snowflake.<name>]`` section to ``carve/connections.toml``.
    2. Append a ``# === <name> target ===`` block to ``.env.example``.

    Used by both ``carve init`` (for ``dev``) and ``carve target create``
    (for any other target name) — the single helper guarantees that both
    verbs produce byte-identical artifacts. P1.1-01 dropped the
    ``targets/<name>/el/`` directory creation: EL artifacts live in the
    flat ``el/<name>/`` tree, target-agnostic.

    Args:
        name: Target name. Validated against :data:`TARGET_NAME_RE`.
        root: Project root (the directory containing ``carve.toml``).
        config_dir: Override for the ``carve/`` config directory name.
        force: Pass through to :func:`add_target_section`. The
            ``.env.example`` block is appended only when missing
            (duplicate blocks are visually obvious to the user, but the
            helper guards against an idempotent re-run producing
            doubles).

    Raises:
        InvalidTargetNameError: If ``name`` fails validation.
        TargetExistsError: If ``[snowflake.<name>]`` already exists and
            ``force`` is False.
    """
    validate_target_name(name)

    conn_path = root / config_dir / "connections.toml"
    add_target_section(name, conn_path, force=force)

    env_example_path = root / ".env.example"
    if not env_example_has_block(name, env_example_path):
        add_env_example_block(name, env_example_path)
