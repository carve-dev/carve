"""Infer a dbt project's *conventions* — the producer the dbt agent + dbt-qa consume.

The dbt engineer is asked to author "in the project's style," and dbt-qa to flag a
diff that departs from it. Both read **the project's inferred conventions in memory**
(``carve/conventions.md``). This module is the missing PRODUCER: a pure inference
engine over a dbt project's facts → a structured :class:`InferredConventions` record,
a markdown renderer for ``conventions.md``, a deterministic naming-violation check
(the substrate behind dbt-qa's "a model violating the inferred naming convention is
flagged"), and the on-demand callable ``dbt_conventions`` :class:`Tool`.

What facts → what conventions
-----------------------------
The engine reads two complementary inputs and merges them (neither alone is
authoritative — a project may be uncompiled, so the manifest can be absent):

* **The compiled manifest** (``target/manifest.json``), via the shipped
  ``dbt_manifest`` reads — every model's name, ``original_file_path``,
  ``config.materialized``, ``schema``, ``tags``, and the generic tests attached
  to it. The authoritative, fully-resolved view *when the project is compiled*.
* **The raw model tree + ``dbt_project.yml``** — ``models/**/*.sql`` paths and the
  ``models:`` materialization config block. This makes inference work
  **pre-compile** (no ``target/`` yet), and supplies materialization *defaults*
  the manifest only reflects per-resolved-node.

From those facts it infers, **only what is actually present** (never fabricating a
convention the project does not exhibit):

* **naming** — the prefix per layer (``stg_`` for staging, ``int_`` for
  intermediate, ``mart_``/``fct_``/``dim_`` for marts), each detected only if at
  least one model in that layer uses it.
* **layout** — which conventional model folders exist (``models/staging``,
  ``models/intermediate``, ``models/marts``), relative to ``models/``.
* **materialization** — the default materialization per layer, taken from the
  ``dbt_project.yml`` ``models:`` block when set, else the modal materialization
  of that layer's models in the manifest.
* **tests** — which generic tests appear (``unique`` / ``not_null`` /
  ``relationships`` / ``accepted_values``) and the dbt source ``freshness``
  coverage, as observed across the project.

Robustness to *partial* conventions is a first-class requirement: a project that
only uses ``stg_`` and a ``staging/`` folder, with no marts and no tests, infers
exactly that — staging naming + a staging layer, and empty marts/test sections —
not an invented full house.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.config.paths import ProjectPaths
from carve.integrations.component_locator import _detect_dbt_project

# ---------------------------------------------------------------------------
# Layer model: the conventional dbt layers we infer over.
# ---------------------------------------------------------------------------

# Order matters: longer folder names first so "intermediate" is matched before a
# hypothetical "int" and "staging" before "stg" — path classification walks this.
_STAGING = "staging"
_INTERMEDIATE = "intermediate"
_MARTS = "marts"

_LAYERS = (_STAGING, _INTERMEDIATE, _MARTS)

# The folder names (relative to models/) that map to each layer. dbt projects use
# a small, well-known vocabulary; we accept the common variants.
_LAYER_FOLDERS: dict[str, tuple[str, ...]] = {
    _STAGING: ("staging", "stg"),
    _INTERMEDIATE: ("intermediate", "int"),
    _MARTS: ("marts", "mart"),
}

# The prefixes each layer canonically uses. A model "counts" for a layer's prefix
# convention if its name starts with one of these (and, for classification by
# name when a model is not under a known folder, the prefix routes it to a layer).
_LAYER_PREFIXES: dict[str, tuple[str, ...]] = {
    _STAGING: ("stg_",),
    _INTERMEDIATE: ("int_",),
    _MARTS: ("mart_", "fct_", "dim_"),
}

# Generic dbt test kinds we report coverage for.
_GENERIC_TESTS = ("unique", "not_null", "relationships", "accepted_values")


# ---------------------------------------------------------------------------
# The inferred record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerConvention:
    """One layer's inferred conventions (empty when the layer is absent).

    * ``prefixes`` — the naming prefixes observed for this layer's models, most
      common first (e.g. ``("stg_",)`` for staging, ``("fct_", "dim_")`` for
      marts). Empty when no model in the layer carries a known prefix.
    * ``folder`` — the layer's folder under ``models/`` (e.g. ``"staging"``),
      or ``None`` when no such folder exists on disk / in the manifest paths.
    * ``materialization`` — the inferred default materialization for the layer
      (``"view"`` / ``"table"`` / ``"ephemeral"`` / ``"incremental"``), or
      ``None`` when the layer has no models and no ``dbt_project.yml`` default.
    * ``model_count`` — how many models were classified into the layer (0 when
      absent).
    """

    prefixes: tuple[str, ...] = ()
    folder: str | None = None
    materialization: str | None = None
    model_count: int = 0

    @property
    def present(self) -> bool:
        return self.model_count > 0 or self.folder is not None


@dataclass(frozen=True)
class TestConventions:
    """The generic-test coverage observed across the project.

    * ``generic_tests`` — the generic test kinds present, most common first
      (subset of unique/not_null/relationships/accepted_values).
    * ``has_source_freshness`` — whether any source declares a ``freshness:``
      block (dbt source freshness coverage).
    * ``key_columns_tested`` — column names that carry a ``unique`` and/or
      ``not_null`` test on at least one model (the project's de-facto key
      columns), sorted.
    """

    generic_tests: tuple[str, ...] = ()
    has_source_freshness: bool = False
    key_columns_tested: tuple[str, ...] = ()


@dataclass(frozen=True)
class InferredConventions:
    """A dbt project's inferred conventions — the record persisted to memory.

    Inference is **observational**: every field reflects what the project
    actually exhibits. A field is empty/``None`` when the project does not use
    that convention (a partial project infers a partial record, never a
    fabricated full one). ``project_name`` is the ``dbt_project.yml`` ``name``
    (or ``None`` if not found). ``model_count`` is the total models seen.
    """

    project_name: str | None = None
    model_count: int = 0
    layers: dict[str, LayerConvention] = field(default_factory=dict)
    tests: TestConventions = field(default_factory=TestConventions)

    def layer(self, name: str) -> LayerConvention:
        """The convention for ``name`` (an empty :class:`LayerConvention` if absent)."""
        return self.layers.get(name, LayerConvention())

    @property
    def has_any(self) -> bool:
        """Whether *any* convention was inferred (a non-empty project)."""
        return self.model_count > 0 or any(layer.present for layer in self.layers.values())


# ---------------------------------------------------------------------------
# A model fact (the normalized input the inference reduces over)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ModelFact:
    """One model's facts, merged from the manifest and/or the raw tree."""

    name: str
    path: str | None  # original_file_path, e.g. "models/staging/stg_orders.sql"
    materialized: str | None
    tags: tuple[str, ...]


# ---------------------------------------------------------------------------
# The inference engine
# ---------------------------------------------------------------------------


def infer_conventions(dbt_root: Path) -> InferredConventions:
    """Infer ``dbt_root``'s conventions from its manifest + raw tree + project yml.

    Pure and read-only: reads ``target/manifest.json`` (if compiled),
    ``models/**/*.sql`` (always), and ``dbt_project.yml`` (for the project name +
    per-layer materialization defaults). An absent/empty/missing-compile project
    yields an empty record (``has_any is False``), never a crash. A malformed
    ``manifest.json`` / ``dbt_project.yml`` fails closed with
    :class:`~carve.core.agents.tools.ToolExecutionError`.
    """
    dbt_root = dbt_root.resolve()
    project_yml = _load_project_yml(dbt_root)
    project_name = _project_name(project_yml)
    manifest = _load_manifest(dbt_root)

    facts = _model_facts(dbt_root, manifest)
    project_materializations = _project_materialization_defaults(project_yml)

    layers: dict[str, LayerConvention] = {}
    for layer in _LAYERS:
        layer_facts = [f for f in facts if _classify_layer(f) == layer]
        layers[layer] = _infer_layer(
            layer,
            layer_facts,
            dbt_root=dbt_root,
            project_default=project_materializations.get(layer),
        )

    tests = _infer_tests(manifest)

    return InferredConventions(
        project_name=project_name,
        model_count=len(facts),
        layers=layers,
        tests=tests,
    )


def _infer_layer(
    layer: str,
    facts: list[_ModelFact],
    *,
    dbt_root: Path,
    project_default: str | None,
) -> LayerConvention:
    """Reduce a layer's model facts into its :class:`LayerConvention`."""
    # Prefixes: count which of the layer's canonical prefixes the models use,
    # most common first. A model without a known prefix contributes nothing.
    prefix_counter: Counter[str] = Counter()
    for fact in facts:
        for prefix in _LAYER_PREFIXES[layer]:
            if fact.name.startswith(prefix):
                prefix_counter[prefix] += 1
                break
    prefixes = tuple(p for p, _ in prefix_counter.most_common())

    # Folder: prefer the on-disk/manifest-path folder; fall back to a present
    # directory under models/ even when empty of compiled models.
    folder = _layer_folder(layer, facts, dbt_root)

    # Materialization: the dbt_project.yml default wins (it is the *declared*
    # default); else the modal materialization of the layer's models.
    materialization = project_default or _modal_materialization(facts)

    return LayerConvention(
        prefixes=prefixes,
        folder=folder,
        materialization=materialization,
        model_count=len(facts),
    )


def _modal_materialization(facts: Iterable[_ModelFact]) -> str | None:
    counter: Counter[str] = Counter(
        f.materialized for f in facts if isinstance(f.materialized, str) and f.materialized
    )
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _layer_folder(layer: str, facts: list[_ModelFact], dbt_root: Path) -> str | None:
    """The layer's folder name under ``models/`` (from model paths, else on disk)."""
    # From a model's original_file_path (e.g. models/staging/stg_orders.sql) take
    # the first path segment under models/.
    for fact in facts:
        seg = _models_subdir(fact.path)
        if seg is not None and _folder_to_layer(seg) == layer:
            return seg
    # No models classified here yet — does a conventional folder exist on disk?
    models_dir = dbt_root / "models"
    if models_dir.is_dir():
        for candidate in _LAYER_FOLDERS[layer]:
            if (models_dir / candidate).is_dir():
                return candidate
    return None


# ---------------------------------------------------------------------------
# Model-fact extraction (manifest union raw tree)
# ---------------------------------------------------------------------------


def _model_facts(dbt_root: Path, manifest: dict[str, Any]) -> list[_ModelFact]:
    """Every model's facts, merged manifest-first then filled from the raw tree.

    Manifest models are authoritative (they carry materialization + tags). Raw
    ``.sql`` files not present in the manifest (uncompiled, or compile is stale)
    are added with whatever the path tells us — so inference works pre-compile.
    """
    by_name: dict[str, _ModelFact] = {}

    for node in _nodes(manifest).values():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        name = node.get("name")
        if not isinstance(name, str) or not name:
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        materialized = config.get("materialized") if isinstance(config, dict) else None
        if not isinstance(materialized, str):
            materialized = (
                node.get("materialized") if isinstance(node.get("materialized"), str) else None
            )
        path = node.get("original_file_path") or node.get("path")
        tags = tuple(t for t in node.get("tags", []) if isinstance(t, str))
        by_name[name] = _ModelFact(
            name=name,
            path=path if isinstance(path, str) else None,
            materialized=materialized,
            tags=tags,
        )

    models_dir = dbt_root / "models"
    if models_dir.is_dir():
        for sql in sorted(models_dir.rglob("*.sql")):
            if not sql.is_file():
                continue
            name = sql.stem
            if name in by_name:
                continue
            rel = sql.relative_to(dbt_root).as_posix()
            by_name[name] = _ModelFact(name=name, path=rel, materialized=None, tags=())

    return sorted(by_name.values(), key=lambda f: f.name)


def _classify_layer(fact: _ModelFact) -> str | None:
    """Route a model to a layer — by its folder first, then its name prefix."""
    seg = _models_subdir(fact.path)
    if seg is not None:
        layer = _folder_to_layer(seg)
        if layer is not None:
            return layer
    for layer, prefixes in _LAYER_PREFIXES.items():
        if any(fact.name.startswith(p) for p in prefixes):
            return layer
    return None


def _models_subdir(path: str | None) -> str | None:
    """First path segment under ``models/`` for ``path``, else ``None``.

    ``models/staging/stg_orders.sql`` → ``"staging"``;
    ``staging/stg_orders.sql`` (manifest ``path`` is already models-relative) →
    ``"staging"``; a bare ``models/stg_orders.sql`` (no subdir) → ``None``.
    """
    if not path:
        return None
    parts = Path(path).parts
    if not parts:
        return None
    # Drop a leading "models/" if present (original_file_path includes it;
    # the manifest's bare `path` does not).
    if parts[0] == "models":
        parts = parts[1:]
    # parts now: (<subdir>, ..., <file>.sql) or (<file>.sql)
    if len(parts) >= 2:
        return parts[0]
    return None


def _folder_to_layer(folder: str) -> str | None:
    folder_l = folder.lower()
    for layer, names in _LAYER_FOLDERS.items():
        if folder_l in names:
            return layer
    return None


# ---------------------------------------------------------------------------
# Test-pattern inference
# ---------------------------------------------------------------------------


def _infer_tests(manifest: dict[str, Any]) -> TestConventions:
    """Generic-test coverage + source-freshness, observed across the manifest."""
    kind_counter: Counter[str] = Counter()
    key_columns: set[str] = set()
    for node in _nodes(manifest).values():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        meta = node.get("test_metadata")
        kind = meta.get("name") if isinstance(meta, dict) else None
        if isinstance(kind, str) and kind in _GENERIC_TESTS:
            kind_counter[kind] += 1
            if kind in ("unique", "not_null"):
                column = node.get("column_name")
                if isinstance(column, str) and column:
                    key_columns.add(column)

    generic_tests = tuple(k for k, _ in kind_counter.most_common())
    has_freshness = _any_source_freshness(manifest)
    return TestConventions(
        generic_tests=generic_tests,
        has_source_freshness=has_freshness,
        key_columns_tested=tuple(sorted(key_columns)),
    )


def _any_source_freshness(manifest: dict[str, Any]) -> bool:
    sources = manifest.get("sources")
    if not isinstance(sources, dict):
        return False
    for src in sources.values():
        if isinstance(src, dict) and isinstance(src.get("freshness"), dict):
            return True
    return False


# ---------------------------------------------------------------------------
# dbt_project.yml reads
# ---------------------------------------------------------------------------


def _load_project_yml(dbt_root: Path) -> dict[str, Any]:
    path = dbt_root / "dbt_project.yml"
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolExecutionError(f"Could not read {path}: {exc}") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ToolExecutionError(f"Malformed YAML in {path}: {exc}") from exc
    return doc if isinstance(doc, dict) else {}


def _project_name(project_yml: dict[str, Any]) -> str | None:
    name = project_yml.get("name")
    return name if isinstance(name, str) and name else None


def _project_materialization_defaults(project_yml: dict[str, Any]) -> dict[str, str]:
    """Per-layer ``+materialized`` defaults from the ``models:`` block.

    dbt nests model config by package then folder::

        models:
          <project>:
            staging:
              +materialized: view
            marts:
              +materialized: table

    We resolve each layer by matching the layer's known folder names anywhere in
    the ``models:`` tree, taking the nearest ``+materialized`` for that folder.
    A top-level ``+materialized`` (project-wide default) seeds every layer that
    has no more-specific value.
    """
    models = project_yml.get("models")
    if not isinstance(models, dict):
        return {}

    defaults: dict[str, str] = {}
    project_wide = _as_materialized(models.get("+materialized"))

    # Walk into each package mapping (and also treat the top level as a package,
    # in case folders are declared without a package nesting).
    package_blocks: list[dict[str, Any]] = [models]
    for value in models.values():
        if isinstance(value, dict):
            package_blocks.append(value)

    for block in package_blocks:
        for key, value in block.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            layer = _folder_to_layer(key)
            if layer is None:
                continue
            mat = _as_materialized(value.get("+materialized"))
            if mat is not None:
                defaults.setdefault(layer, mat)

    if project_wide is not None:
        for layer in _LAYERS:
            defaults.setdefault(layer, project_wide)
    return defaults


def _as_materialized(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# Manifest loading (shared shape with manifest.py — fail-closed)
# ---------------------------------------------------------------------------


def _load_manifest(dbt_root: Path) -> dict[str, Any]:
    """Load ``<dbt_root>/target/manifest.json``, or ``{}`` when uncompiled."""
    manifest_path = dbt_root / "target" / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolExecutionError(f"Could not read {manifest_path}: {exc}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ToolExecutionError(f"Malformed dbt manifest at {manifest_path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ToolExecutionError(f"dbt manifest at {manifest_path} must be a JSON object.")
    return doc


def _nodes(manifest: dict[str, Any]) -> dict[str, Any]:
    nodes = manifest.get("nodes")
    return nodes if isinstance(nodes, dict) else {}


# ---------------------------------------------------------------------------
# Naming-violation check (the substrate behind dbt-qa's convention flag)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NamingViolation:
    """A model whose name violates the inferred naming convention for its layer."""

    model: str
    layer: str
    expected_prefixes: tuple[str, ...]
    message: str


def check_naming_violation(
    model_name: str,
    *,
    path: str | None,
    conventions: InferredConventions,
) -> NamingViolation | None:
    """Flag ``model_name`` if it violates the inferred naming convention.

    Returns a :class:`NamingViolation` when the model's layer (resolved from
    ``path`` first, else any matching prefix) has an inferred prefix convention
    and ``model_name`` does not carry one of those prefixes; ``None`` otherwise.
    A layer with **no** inferred prefix convention never produces a violation
    (we only flag against conventions the project actually established).

    This is the deterministic core dbt-qa's "a model violating the inferred
    naming convention is flagged" check relies on — the reviewer renders the
    finding; this decides whether one exists.
    """
    layer = _layer_for(model_name, path)
    if layer is None:
        return None
    convention = conventions.layer(layer)
    expected = convention.prefixes
    if not expected:
        return None
    if any(model_name.startswith(prefix) for prefix in expected):
        return None
    return NamingViolation(
        model=model_name,
        layer=layer,
        expected_prefixes=expected,
        message=(
            f"Model {model_name!r} is in the {layer} layer but does not use the "
            f"inferred prefix ({' or '.join(expected)})."
        ),
    )


def _layer_for(model_name: str, path: str | None) -> str | None:
    return _classify_layer(_ModelFact(name=model_name, path=path, materialized=None, tags=()))


# ---------------------------------------------------------------------------
# Markdown rendering (the conventions.md the loader/agent read)
# ---------------------------------------------------------------------------

_LAYER_TITLES = {
    _STAGING: "Staging",
    _INTERMEDIATE: "Intermediate",
    _MARTS: "Marts",
}


def render_conventions_md(conventions: InferredConventions) -> str:
    """Render ``conventions`` as the markdown body of ``carve/conventions.md``.

    The format is deliberately minimal freeform markdown the
    :class:`~carve.core.memory.loader.MemoryLoader` round-trips verbatim (it
    reads the file as opaque text) — labelled sections, no enforced schema. A
    leading provenance line marks the file as Carve-inferred (vs the comment-only
    init placeholder) so the agent treats it as real inferred content.
    """
    lines: list[str] = ["# Inferred project conventions", ""]
    lines.append(
        "> Inferred by `carve memory refresh` from the existing dbt project. "
        "User rules in `standards.md` take precedence where they conflict."
    )
    lines.append("")

    if not conventions.has_any:
        lines.append(
            "_No dbt conventions detected (the project has no models yet)._ "
            "Author models, then re-run `carve memory refresh`."
        )
        return "\n".join(lines) + "\n"

    if conventions.project_name:
        lines.append(
            f"**Project:** `{conventions.project_name}` ({conventions.model_count} model(s))"
        )
        lines.append("")

    # Naming + layout + materialization, per layer.
    lines.append("## Layers")
    lines.append("")
    any_layer = False
    for layer in _LAYERS:
        convention = conventions.layer(layer)
        if not convention.present:
            continue
        any_layer = True
        title = _LAYER_TITLES[layer]
        bits: list[str] = []
        if convention.folder:
            bits.append(f"folder `models/{convention.folder}/`")
        if convention.prefixes:
            bits.append("prefix " + " / ".join(f"`{p}`" for p in convention.prefixes))
        if convention.materialization:
            bits.append(f"materialized as `{convention.materialization}`")
        if convention.model_count:
            bits.append(f"{convention.model_count} model(s)")
        detail = "; ".join(bits) if bits else "present"
        lines.append(f"- **{title}** — {detail}.")
    if not any_layer:
        lines.append("- _No conventional staging/intermediate/marts layers detected._")
    lines.append("")

    # Test patterns.
    lines.append("## Tests")
    lines.append("")
    tests = conventions.tests
    if tests.generic_tests:
        lines.append(
            "- Generic tests in use: " + ", ".join(f"`{t}`" for t in tests.generic_tests) + "."
        )
    else:
        lines.append("- _No generic tests detected._")
    if tests.key_columns_tested:
        lines.append(
            "- Key columns carrying unique/not_null tests: "
            + ", ".join(f"`{c}`" for c in tests.key_columns_tested)
            + "."
        )
    if tests.has_source_freshness:
        lines.append("- Source freshness checks are configured.")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# The callable Tool (on-demand; the dlt-brownfield-parallel mechanism)
# ---------------------------------------------------------------------------

_CONVENTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["infer", "summary"],
            "description": (
                "infer: the structured inferred-conventions record (naming, layout, "
                "materialization, tests). summary: the same rendered as markdown "
                "(the conventions.md body)."
            ),
        },
    },
    "required": ["op"],
}


def make_dbt_conventions_tool(
    *,
    paths: ProjectPaths | None = None,
    dbt_root: Path | None = None,
    name: str = "dbt_conventions",
) -> Tool:
    """Build the ``dbt_conventions`` tool over the user's dbt project.

    On-demand brownfield convention inference, mirroring the ``dbt_manifest`` /
    ``existing_dlt_inspect`` factory shape: supply exactly one of ``paths`` (the
    project paths — the dbt project is detected via the shipped locator) or
    ``dbt_root`` (an already-resolved dbt project dir — lets unit tests run
    offline). ``op="infer"`` returns the structured record; ``op="summary"``
    returns the ``conventions.md`` markdown. A missing dbt project yields an
    empty record (``has_any=False``), not an error.

    The produced ``Tool.name`` equals ``name`` (the grant name) so the binder's
    ``injected.name == grant_name`` precondition holds.
    """
    if (paths is None) == (dbt_root is None):
        raise ValueError("Pass exactly one of `paths` or `dbt_root`.")

    def _resolve_dbt_root() -> Path | None:
        if dbt_root is not None:
            return dbt_root.resolve()
        assert paths is not None  # narrowed by the guard above
        return _detect_dbt_project(paths, required=False)

    def _execute(input_: ToolInput) -> ToolResult:
        op = input_.get("op")
        if op not in ("infer", "summary"):
            raise ToolExecutionError(f"Unknown dbt_conventions op {op!r}; use infer/summary.")
        root = _resolve_dbt_root()
        if root is None:
            conventions = InferredConventions()
        else:
            conventions = infer_conventions(root)
        if op == "summary":
            return {"present": conventions.has_any, "markdown": render_conventions_md(conventions)}
        return {"present": conventions.has_any, "conventions": conventions_to_dict(conventions)}

    return Tool(
        name=name,
        description=(
            "Infer the existing dbt project's conventions on demand — naming prefixes "
            "(stg_/int_/mart_/fct_/dim_), the staging/intermediate/marts folder layout, "
            "per-layer materialization defaults, and generic-test coverage. Use op=infer "
            "for the structured record or op=summary for the conventions.md markdown. "
            "Author in the inferred style; flag a diff that departs from it."
        ),
        input_schema=_CONVENTIONS_SCHEMA,
        executor=_execute,
    )


def conventions_to_dict(conventions: InferredConventions) -> dict[str, Any]:
    """Serialize an :class:`InferredConventions` for the tool result / inspection."""
    return {
        "project_name": conventions.project_name,
        "model_count": conventions.model_count,
        "has_any": conventions.has_any,
        "layers": {
            layer: {
                "prefixes": list(convention.prefixes),
                "folder": convention.folder,
                "materialization": convention.materialization,
                "model_count": convention.model_count,
                "present": convention.present,
            }
            for layer, convention in conventions.layers.items()
        },
        "tests": {
            "generic_tests": list(conventions.tests.generic_tests),
            "has_source_freshness": conventions.tests.has_source_freshness,
            "key_columns_tested": list(conventions.tests.key_columns_tested),
        },
    }


__all__ = [
    "InferredConventions",
    "LayerConvention",
    "NamingViolation",
    "TestConventions",
    "check_naming_violation",
    "conventions_to_dict",
    "infer_conventions",
    "make_dbt_conventions_tool",
    "render_conventions_md",
]
