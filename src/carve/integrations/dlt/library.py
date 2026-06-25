"""``dlt_library`` — list / lookup / copy over the curated source corpus.

Carve ships a curated library of dlt source packs under ``src/carve/sources/``
(each a skill pack: ``SKILL.md`` + an inert bundled ``scripts/`` dlt source).
``dlt_library`` is the callable :class:`~carve.core.agents.tools.Tool` the dlt
engineer uses to discover and *physically copy* one of those packs into the
project's ``el/<component>/`` tree, customized for a destination/schema/creds and
stamped with a Carve provenance header.

It is **distinct** from the extensibility ``lookup_skill_pack`` tool: that one
injects a pack's *instructions* into the conversation; this one enumerates and
*copies a pack's code*. Both ride the shipped ``pack_discovery`` / ``packs.py``
substrate (re-discovered per call, so the requestable-name set is always a fresh
allowlist — a forged/unknown name raises before any read).

Three ops, dispatched by ``op``:

* ``list`` — every curated pack with metadata (name, description, and any
  ``supported_destinations`` / ``last_updated`` the ``SKILL.md`` frontmatter
  carries).
* ``lookup(query)`` — top-5 matches with a high/medium/low confidence band
  layered over the shipped substring ``match()`` (see :func:`_confidence`).
* ``copy(name, dest_path, customization)`` — lay the named pack's ``scripts/``
  into ``dest_path`` (confined to the project ``el/**`` tree), apply
  customization via env-var/literal substitution, write the provenance header
  recording ``library_name`` + ``library_commit``, and return the files written.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from carve.core.agents.permissions.modes import PermissionMode, mode_permits
from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.skills.pack_discovery import SkillPackLibrary
from carve.core.skills.packs import SKILL_FILENAME, SkillPack
from carve.integrations.dlt.code_emitter import with_provenance_header

# Confidence banding over the shipped substring match (spec Open questions:
# token-name similarity + 0.85 "high" threshold). The substring match is binary
# (the shipped substrate), so we band by *where* the query hit: an exact name
# match is high, a name-substring is high, a description-only hit is medium.
_CONFIDENCE_HIGH = "high"
_CONFIDENCE_MEDIUM = "medium"
_CONFIDENCE_LOW = "low"
_LOOKUP_LIMIT = 5

_LIBRARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["list", "lookup", "copy"],
            "description": "list the curated packs, look one up by query, or copy one into el/.",
        },
        "query": {"type": "string", "description": "Free-text query (for op=lookup)."},
        "name": {"type": "string", "description": "Curated pack name (for op=copy)."},
        "dest_path": {
            "type": "string",
            "description": "Destination dir under el/<component>/ (for op=copy).",
        },
        "customization": {
            "type": "object",
            "description": (
                "Optional substitutions applied to the copied source: keys like "
                "destination/schema/credential placeholder names mapped to their values."
            ),
        },
    },
    "required": ["op"],
}


def make_dlt_library_tool(
    sources_dir: Path,
    *,
    project_dir: Path,
    library_commit: str | None = None,
    mode: PermissionMode = PermissionMode.DEPLOY,
    name: str = "dlt_library",
) -> Tool:
    """Build the ``dlt_library`` tool over the curated ``sources_dir`` corpus.

    * ``sources_dir`` — the curated corpus root (``src/carve/sources/``).
    * ``project_dir`` — the user project root; ``copy``'s ``dest_path`` is
      confined to ``project_dir/el/**`` (mirrors ``existing_dlt_inspect``).
    * ``library_commit`` — the corpus commit recorded in provenance; defaults to
      the repo HEAD SHA of ``sources_dir`` (``"unknown"`` if git is unavailable).
      Injectable so tests are deterministic and offline.
    * ``mode`` — the active :class:`PermissionMode` the tool is built at. Like the
      ``sql`` tool, the harness gate admits ``dlt_library`` by *name* in every
      mode and never passes the mode to the executor, so the ``copy`` op's write
      authority is enforced HERE: ``copy`` (which lays a source pack into
      ``el/**``) is **fail-closed below build** — a ``ToolExecutionError`` below
      ``build``, exactly how the ``sql`` tool fail-closes warehouse writes below
      ``deploy``. ``list``/``lookup`` are pure reads and run in every mode.
      Defaults to the most permissive (``deploy``) for back-compat; the
      orchestrator threads the clamped ``child_mode`` so a PLAN child's ``copy``
      is denied while ``list``/``lookup`` stay available.

    The produced ``Tool.name`` equals ``name`` (the grant name), satisfying the
    binder's ``injected.name == grant_name`` precondition.
    """
    sources_root = sources_dir.resolve()
    el_dir = (project_dir / "el").resolve()
    commit = library_commit if library_commit is not None else _detect_commit(sources_root)

    def _library() -> SkillPackLibrary:
        # Re-discover per call so an edited corpus is always picked up and the
        # requestable-name allowlist is fresh (mirrors lookup_skill_pack).
        return SkillPackLibrary([sources_root])

    def _execute(input_: ToolInput) -> ToolResult:
        op = input_.get("op")
        if op == "list":
            return _list(_library())
        if op == "lookup":
            query = input_.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ToolExecutionError("op=lookup requires a non-empty 'query'.")
            return _lookup(_library(), query.strip())
        if op == "copy":
            # Write op: fail-closed below build (the tool is admitted by name in
            # every mode; the copy authority lives here, mirroring `sql`).
            if not mode_permits(mode, PermissionMode.BUILD):
                raise ToolExecutionError(
                    f"dlt_library op=copy writes into el/ and requires build mode; "
                    f"got {mode}. Use op=list / op=lookup to browse the library."
                )
            return _copy(
                _library(),
                input_.get("name"),
                input_.get("dest_path"),
                input_.get("customization"),
                el_dir=el_dir,
                commit=commit,
            )
        raise ToolExecutionError(f"Unknown dlt_library op {op!r}; use list/lookup/copy.")

    return Tool(
        name=name,
        description=(
            "Browse and use Carve's curated dlt source library: list the available "
            "source packs with metadata, look one up by query (with a confidence "
            "signal), or copy a named pack's source code into el/<component>/ "
            "customized for a destination/schema/credentials and provenance-stamped."
        ),
        input_schema=_LIBRARY_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# list / lookup
# ---------------------------------------------------------------------------


def _list(library: SkillPackLibrary) -> ToolResult:
    return {"packs": [_pack_metadata(pack) for pack in library.discover()]}


def _pack_metadata(pack: SkillPack) -> dict[str, Any]:
    """Pack metadata for ``list``: name + description + any extra frontmatter.

    ``SkillPack`` carries only name/description/expects_env structurally; the
    optional ``supported_destinations`` / ``last_updated`` the spec mentions
    live in the raw ``SKILL.md`` frontmatter, so re-read them opportunistically
    (best-effort: a parse hiccup just omits them — discovery already validated
    the pack loads).
    """
    meta: dict[str, Any] = {
        "name": pack.name,
        "description": pack.description,
        "expects_env": list(pack.expects_env),
    }
    extra = _frontmatter_extras(pack.directory / SKILL_FILENAME)
    for key in ("supported_destinations", "last_updated"):
        if key in extra:
            meta[key] = extra[key]
    return meta


def _frontmatter_extras(skill_md: Path) -> dict[str, Any]:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        parsed = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _lookup(library: SkillPackLibrary, query: str) -> ToolResult:
    """Top-``_LOOKUP_LIMIT`` matches for ``query`` with a confidence band.

    Built on the shipped substring ``match()``; :func:`_confidence` bands each
    hit high/medium/low by where it matched. Results are ordered by confidence
    (high → low) then name so the strongest candidate leads.
    """
    needle = query.lower()
    matches = library.match(query)
    banded = [
        {
            "name": m.name,
            "description": m.description,
            "confidence": _confidence(needle, m.name, m.description),
        }
        for m in matches
    ]
    rank = {_CONFIDENCE_HIGH: 0, _CONFIDENCE_MEDIUM: 1, _CONFIDENCE_LOW: 2}
    banded.sort(key=lambda r: (rank[str(r["confidence"])], str(r["name"])))
    return {"query": query, "matches": banded[:_LOOKUP_LIMIT]}


def _confidence(needle: str, pack_name: str, description: str) -> str:
    """Band a substring hit: name-exact/substring → high, description-only → medium.

    The shipped ``match()`` is a binary case-insensitive substring over
    name+description. Lacking an embedding index (a later increment), we
    approximate the spec's 0.85 "high" threshold structurally: a query that
    appears in the pack *name* is a strong signal (high); a query that only
    appears in the *description* is weaker (medium). ``low`` is reserved for
    future fuzzier matching — substring hits are never below medium.
    """
    name_l = pack_name.lower()
    if needle == name_l or needle in name_l:
        return _CONFIDENCE_HIGH
    if needle in description.lower():
        return _CONFIDENCE_MEDIUM
    return _CONFIDENCE_LOW


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def _copy(
    library: SkillPackLibrary,
    name: Any,
    dest_path: Any,
    customization: Any,
    *,
    el_dir: Path,
    commit: str,
) -> ToolResult:
    if not isinstance(name, str) or not name.strip():
        raise ToolExecutionError("op=copy requires a 'name'.")
    if not isinstance(dest_path, str) or not dest_path.strip():
        raise ToolExecutionError("op=copy requires a 'dest_path'.")
    if customization is None:
        customization = {}
    if not isinstance(customization, dict):
        raise ToolExecutionError("'customization' must be an object of substitutions.")

    # Resolve the pack name against the *current* discovered allowlist so an
    # unknown/forged name raises before any read.
    by_name = {p.name: p for p in library.discover()}
    pack = by_name.get(name.strip())
    if pack is None:
        raise ToolExecutionError(
            f"Unknown source pack {name.strip()!r}. Available: {sorted(by_name)}"
        )

    dest = _confine_dest(dest_path.strip(), el_dir=el_dir)
    scripts_dir = pack.directory / "scripts"
    if not scripts_dir.is_dir():
        raise ToolExecutionError(f"Source pack {pack.name!r} has no scripts/ to copy.")

    subs = _string_subs(customization)
    destination = _str_or_none(customization.get("destination"))

    written: list[str] = []
    for src_file in sorted(p for p in scripts_dir.rglob("*") if p.is_file()):
        rel = src_file.relative_to(scripts_dir)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_one(
            src_file,
            target,
            subs=subs,
            pack=pack,
            commit=commit,
            destination=destination,
        )
        written.append(str(target.relative_to(el_dir.parent)))

    return {
        "name": pack.name,
        "dest_path": str(dest.relative_to(el_dir.parent)),
        "library_name": pack.name,
        "library_commit": commit,
        "files_written": written,
    }


def _copy_one(
    src_file: Path,
    target: Path,
    *,
    subs: dict[str, str],
    pack: SkillPack,
    commit: str,
    destination: str | None,
) -> None:
    """Copy one bundled file, customizing + provenance-stamping Python sources.

    Python files get substitution applied and a provenance header prepended
    (``with_provenance_header`` no-ops if the bundled file already carries one).
    Non-Python files (requirements.txt, data) are copied verbatim — they have no
    comment grammar to host a header and shouldn't be substituted.
    """
    if src_file.suffix == ".py":
        try:
            body = src_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolExecutionError(f"Could not read {src_file}: {exc}") from exc
        for placeholder, value in subs.items():
            body = body.replace(placeholder, value)
        body = with_provenance_header(
            body,
            source=f"carve/sources/{pack.name}",
            commit=commit,
            destination=destination,
        )
        target.write_text(body, encoding="utf-8")
    else:
        shutil.copyfile(src_file, target)


def _confine_dest(dest_path: str, *, el_dir: Path) -> Path:
    """Resolve ``dest_path`` and confine it to the project ``el/**`` tree.

    Accepts an absolute path or one relative to the project root. Anything that
    resolves outside ``el/`` (including ``..`` traversal) is rejected — never
    write outside ``el/`` (mirrors ``existing_dlt_inspect``'s confinement).
    """
    candidate = Path(dest_path)
    if not candidate.is_absolute():
        candidate = el_dir.parent / candidate
    resolved = candidate.resolve()
    if resolved != el_dir and el_dir not in resolved.parents:
        raise ToolExecutionError(
            f"dest_path {dest_path!r} is outside the el/ tree; copies are confined to el/**."
        )
    return resolved


def _string_subs(customization: dict[str, Any]) -> dict[str, str]:
    """Build the literal placeholder→value substitution map.

    Each customization key becomes an ``__UPPER__`` placeholder the bundled
    source can reference (e.g. ``schema`` → ``__SCHEMA__``). Only string values
    substitute; non-string values are skipped (they're metadata, not text to
    splice into source).
    """
    subs: dict[str, str] = {}
    for key, value in customization.items():
        if isinstance(value, str):
            subs[f"__{key.upper()}__"] = value
    return subs


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _detect_commit(sources_root: Path) -> str:
    """Best-effort repo HEAD SHA for provenance; ``"unknown"`` if unavailable.

    The SHA is dot-free so it round-trips through the provenance reader (whose
    ``commit`` capture stops at the first period). Never raises — a missing git
    or non-repo just yields ``"unknown"``.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(sources_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unknown"


__all__ = ["make_dlt_library_tool"]
